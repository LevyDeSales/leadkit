"""O teste do rascunho na API de admin (`/admin/api/v1`): `assemble_form_draft_prompt`,
`score_form_draft`, `list_form_draft_tests`, `start_form_draft_test`, `get_form_draft_test` e
`download_form_draft_test_pdf`, pela costura HTTP.

Agem pelas mesmas ações das páginas do teste (`app.admin.actions.draft_tests`): o que a página junta, o
disparo da task `draft_test` no Hatchet, a busca de um teste e o nome do PDF são uma regra só, e a
paridade com a página (o mesmo prompt, as mesmas recusas, o mesmo estado, a mesma auditoria, o mesmo
disparo e o mesmo que vai ao LLM) está no fim de cada operação. **Montar o prompt, pontuar e listar não
gravam nada e não chamam o LLM**: o LLM destes testes é um que falha se for chamado. O LLM de verdade
fica fora (o falso do `conftest.py` e o `ScriptedLlm` de `test_admin_drafts.py`), e o disparador da task
`draft_test` é o falso da página (`TestTrigger`, que guarda o id que recebe e roda o passo da task quando
o teste pede). As respostas dos testes são fictícias, e as chaves são sorteadas na execução.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_support import (
    API,
    ApiClient,
    assert_same_effect,
    audited,
    make_api,
    snapshot,
    url_of,
)
from conftest import DEMO_ANSWERS, record_webhook
from test_admin import FakeClock, login
from test_admin_drafts import ScriptedLlm, TestTrigger, post, problems_shown
from test_admin_forms import DEFINITION, SLUG, section_of, store_version
from test_admin_phases import SLUG as PHASED
from test_admin_phases import definition as phased_definition
from test_admin_scoring import SCORING, scored_definition
from test_admin_scoring import SLUG as SCORED
from test_intake import count
from test_public import text

from app.adapters.llm import FakePlanGenerator
from app.admin.actions import draft_tests as draft_test_actions
from app.deps import Deps
from app.errors import FailureCode, RetryableError
from app.forms import draft_tests, drafts, stored
from app.models import Artifact, Delivery, DraftTest, Job, Plan, Submission

PAGE = "/admin"
SENTINEL = "ZZ_SENT_" + secrets.token_hex(8).upper()
Client = Callable[..., ApiClient]
EXAMPLE = json.loads(DEFINITION)["examples"]["answers"]
OTHER = {**EXAMPLE, "escritorio": "Outra Contadora", "tamanho_equipe": "mais_de_15"}
# O que a chamada da API muda e a página não (o último uso da chave que chama) e o que a ação carimba
# com o relógio da máquina.
IGNORE = {"api_keys": {"last_used_at"}, "*": {"created_at", "updated_at"}}
UNKNOWN_TEST = "6f1d3c9e-0b7a-4c52-9d8e-2a4f5b6c7d80"


class NoLlm:
    """O LLM destes testes: se alguém o chamar, o teste falha. Montar o prompt, pontuar, listar e ler
    não o chamam (só o teste, no worker)."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, spec: Any, answers: Any) -> Any:
        self.calls += 1
        raise AssertionError("o LLM não pode ser chamado fora do teste")


class RecordingFake(FakePlanGenerator):
    """O LLM falso do `conftest.py` (o de `LLM_MODE=fake`), que também guarda o prompt que recebe: o
    que vai ao LLM, para comparar o teste que a página começou com o que a API começou."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, spec: Any, answers: Any) -> Any:
        self.prompts.append(spec.render_prompt(answers))
        return super().generate(spec, answers)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def worker(deps: Deps, clock: FakeClock) -> TestTrigger:
    """O disparo da task `draft_test`: guarda o id que recebe e roda o passo da task quando o teste
    pedir (`worker.run()`)."""
    return TestTrigger(deps, clock)


@pytest.fixture
def world(deps: Deps) -> None:
    """O formulário demo-definicao (sem pontuação), o de pontuação e o de fases, importados (versão 1)."""
    for definition in (json.loads(DEFINITION), scored_definition(), phased_definition()):
        stored.import_definition(deps, json.dumps(definition), actor="testes")


@pytest.fixture
def writer(api_client: Client, clock: FakeClock, worker: TestTrigger, world: None) -> ApiClient:
    return api_client("total", clock=clock, test_trigger=worker)


@pytest.fixture
def reader(api_client: Client, clock: FakeClock, worker: TestTrigger, world: None) -> ApiClient:
    return api_client("leitura", clock=clock, test_trigger=worker)


def draft_of(deps: Deps, slug: str = SLUG) -> drafts.Draft:
    with deps.sessions() as session:
        found = drafts.load(session, slug)
    assert found is not None
    return found


def save_draft(deps: Deps, slug: str = SLUG, **changes: Any) -> None:
    """Salva o rascunho com as mudanças, como a API salva."""
    document = {**draft_of(deps, slug).document, **changes}
    drafts.replace_document(
        deps, slug, document, actor="chave:testes", expected=draft_of(deps, slug).token
    )


def unchanged(deps: Deps) -> Callable[[], bool]:
    """Um fotógrafo do banco que diz se algo mudou, menos o último uso da chave que chama."""

    def without_keys() -> dict[str, Any]:
        return {name: rows for name, rows in snapshot().items() if name != "api_keys"}

    before = without_keys()
    return lambda: without_keys() == before


def begin(deps: Deps, now: datetime, *, slug: str = SLUG, **options: Any) -> uuid.UUID:
    """Um teste na fila, pelo serviço (a API que o inicia é de outra operação)."""
    return draft_tests.start(deps, slug, actor="chave:testes", now=now, **options)


def moment(clock: FakeClock) -> datetime:
    return datetime.fromtimestamp(clock(), UTC)


def finish(deps: Deps, test_id: uuid.UUID, clock: FakeClock) -> str:
    return draft_tests.run(deps, test_id, now=lambda: moment(clock))


def fields_of(response: httpx.Response) -> dict[str, str]:
    """Os `errors` de um `422 validation_failed`, por campo."""
    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    return {item["field"]: item["message"] for item in response.json()["errors"]}


def prompt_url(slug: str = SLUG) -> str:
    return url_of("assemble_form_draft_prompt", slug=slug)


def score_url(slug: str = SLUG) -> str:
    return url_of("score_form_draft", slug=slug)


def list_url(slug: str = SLUG) -> str:
    return url_of("list_form_draft_tests", slug=slug)


def result_url(test_id: Any, slug: str = SLUG) -> str:
    return url_of("get_form_draft_test", slug=slug, test_id=str(test_id))


def pdf_url(test_id: Any, slug: str = SLUG) -> str:
    return url_of("download_form_draft_test_pdf", slug=slug, test_id=str(test_id))


def start_url(slug: str = SLUG) -> str:
    return url_of("start_form_draft_test", slug=slug)


# --- a chave e o papel --------------------------------------------------------------------------

REQUESTS: dict[str, Callable[[], tuple[str, str, Any]]] = {
    "assemble_form_draft_prompt": lambda: ("POST", prompt_url(), {}),
    "score_form_draft": lambda: ("POST", score_url(), {}),
    "list_form_draft_tests": lambda: ("GET", list_url(), None),
    "get_form_draft_test": lambda: ("GET", result_url(UNKNOWN_TEST), None),
    "download_form_draft_test_pdf": lambda: ("GET", pdf_url(UNKNOWN_TEST), None),
    "start_form_draft_test": lambda: ("POST", start_url(), {"confirm": True}),
}


def call(client: Any, operation_id: str) -> httpx.Response:
    method, url, body = REQUESTS[operation_id]()
    return client.request(method, url, json=body)


@pytest.mark.parametrize("operation_id", list(REQUESTS))
def test_every_draft_test_operation_needs_a_key(
    deps: Deps, clock: FakeClock, world: None, operation_id: str
) -> None:
    response = call(make_api(deps, clock), operation_id)

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    ("operation_id", "status"),
    [
        ("assemble_form_draft_prompt", 200),
        ("score_form_draft", 200),
        ("list_form_draft_tests", 200),
        ("get_form_draft_test", 404),  # o teste não existe, mas a chave pode ler: não é 403
        ("download_form_draft_test_pdf", 404),
    ],
)
def test_a_read_key_reads_everything_about_a_draft_test(
    reader: ApiClient, operation_id: str, status: int
) -> None:
    response = call(reader, operation_id)

    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"


# --- assemble_form_draft_prompt -----------------------------------------------------------------

PAGE_DRAFT = f"{PAGE}/forms/{SLUG}/draft"


def dated_prompt(deps: Deps) -> None:
    """Um rascunho cujo prompt cita a data de hoje, para a data simulada aparecer nele."""
    save_draft(deps, prompt="Hoje é {{ today|long_date }}.\n" + draft_of(deps).document["prompt"])


def test_assemble_form_draft_prompt_gives_the_prompt_the_page_shows_with_the_example_answers(
    writer: ApiClient, deps: Deps
) -> None:
    assert login(writer).status_code == 303
    shown = text(section_of(writer.get(f"{PAGE_DRAFT}/tests"), "prompt-montado"))

    response = writer.post(prompt_url(), json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "instructions",
        "prompt",
        "prompt_version",
        "today",
        "simulated_date",
        "uses_example_answers",
        "answers",
        "phases",
    }
    assert body["uses_example_answers"] is True and body["simulated_date"] is None
    assert body["answers"] == EXAMPLE and body["today"] == "2027-01-15"
    assert body["prompt_version"] == f"{SLUG}-v2"  # a que a publicação gravaria nos planos
    assert "Nome do escritório: Escritório Exemplo" in body["prompt"]
    for part in ("instructions", "prompt"):  # a página mostra o mesmo texto, linha a linha
        for line in body[part].splitlines():
            assert line.strip() in shown, line
    assert body["instructions"].strip() and body["phases"] == []


def test_assemble_form_draft_prompt_uses_the_answers_and_the_simulated_date_it_is_given(
    writer: ApiClient, deps: Deps
) -> None:
    dated_prompt(deps)

    response = writer.post(prompt_url(), json={"answers": OTHER, "simulated_date": "2027-03-01"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert "Hoje é 1º de março de 2027." in body["prompt"]  # a data simulada
    assert "Tamanho da equipe: mais_de_15" in body["prompt"] and "Outra Contadora" in body["prompt"]
    assert (body["today"], body["simulated_date"]) == ("2027-03-01", "2027-03-01")
    assert body["uses_example_answers"] is False and body["answers"] == OTHER


def test_assemble_form_draft_prompt_reads_the_saved_draft_and_not_the_published_version(
    writer: ApiClient, deps: Deps
) -> None:
    line = "Considere também o momento do mercado contábil."
    save_draft(deps, prompt=draft_of(deps).document["prompt"] + "\n" + line)

    body = writer.post(prompt_url(), json={}).json()

    assert line in body["prompt"]
    assert line not in draft_of(deps).current.definition["prompt"]  # type: ignore[union-attr]


def test_assemble_form_draft_prompt_gives_the_calendar_of_the_phases_on_the_simulated_date(
    writer: ApiClient,
) -> None:
    body = writer.post(prompt_url(PHASED), json={"simulated_date": "2026-10-08"}).json()

    first, second = body["phases"][:2]
    assert (first["number"], first["first"], first["last"]) == (1, "2026-10-08", "2026-11-06")
    assert (second["number"], second["first"]) == (2, "2026-11-07")
    states = {window["label"]: window["state"] for window in first["windows"]}
    assert states["Opção"] == "closes" and states["Desistência"] == "opens"
    assert {item["field"]: item["values"] for item in first["allowed"]}["tipo"] == [
        "decisao",
        "desistencia",
        "proxima",
        "outra",
    ]
    assert "Fase 1 (8 de outubro a 6 de novembro de 2026)" in body["prompt"]


def test_assemble_form_draft_prompt_writes_nothing_and_never_calls_the_llm(
    writer: ApiClient, deps: Deps
) -> None:
    deps.llm = llm = NoLlm()
    nothing_changed = unchanged(deps)

    for body in ({}, {"answers": OTHER, "simulated_date": "2027-03-01"}, {"answers": {}}):
        writer.post(prompt_url(), json=body)  # a última é recusada, e também não grava

    assert nothing_changed() and llm.calls == 0
    assert audited(deps, "form.draft.test") == []


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({"answers": {**EXAMPLE, "email": "sem-arroba"}}, "answers.email", "e-mail inválido"),
        ({"answers": {}}, "answers.nome", "campo obrigatório"),
        (
            {"answers": {**EXAMPLE, "nome": "Outra\u0000Contadora"}},
            "answers.nome",
            "o texto tem um caractere que não vale",
        ),
        ({"simulated_date": "2027-02-30"}, "simulated_date", "use uma data válida"),
        ({"simulated_date": "1999-01-01"}, "simulated_date", "use uma data entre 2000 e 2100"),
    ],
    ids=["answer", "empty-object", "null-character", "date", "date-out-of-range"],
)
def test_assemble_form_draft_prompt_refuses_what_the_page_refuses_with_the_path_of_the_field(
    writer: ApiClient, body: dict[str, Any], field: str, message: str
) -> None:
    assert login(writer).status_code == 303
    page = post(
        writer,
        f"{PAGE_DRAFT}/prompt",
        answers=json.dumps(body["answers"]) if "answers" in body else "",
        date=body.get("simulated_date", ""),
    )

    response = writer.post(prompt_url(), json=body)

    assert response.status_code == page.status_code == 422
    assert message in fields_of(response)[field]
    assert f"{'date' if field == 'simulated_date' else field}: {message}" in problems_shown(page)
    assert "sem-arroba" not in response.text  # nunca o valor recebido


def test_assemble_form_draft_prompt_says_the_path_of_a_body_that_is_not_right(
    writer: ApiClient,
) -> None:
    not_an_object = writer.post(prompt_url(), json={"answers": ["a"]})
    unknown_field = writer.post(prompt_url(), json={"date": "2027-03-01"})  # o nome da página
    not_a_text = writer.post(prompt_url(), json={"simulated_date": 20270301})

    assert "answers" in fields_of(not_an_object)
    assert fields_of(unknown_field) == {"date": "Campo desconhecido."}
    assert "simulated_date" in fields_of(not_a_text)


def test_assemble_form_draft_prompt_of_a_form_that_does_not_exist_is_not_found(
    writer: ApiClient,
) -> None:
    response = writer.post(prompt_url("nao-existe"), json={})

    assert response.status_code == 404 and response.json()["code"] == "not_found"


def test_assemble_form_draft_prompt_echoes_no_free_text_it_was_sent(writer: ApiClient) -> None:
    """O eco geral do contrato para no `404` do endereço sentinela, antes de conferir as respostas e a
    data: com o endereço de um formulário que existe, o texto livre chega à conferência."""
    sent = [
        {"answers": {name: SENTINEL for name in EXAMPLE}, "simulated_date": SENTINEL},
        {"answers": {**EXAMPLE, "email": SENTINEL, "tamanho_equipe": SENTINEL}},
        {"answers": {**EXAMPLE, "nome": SENTINEL * 400}},
        {"answers": {**EXAMPLE, SENTINEL: SENTINEL}},  # a chave, e não só o valor
        {"simulated_date": SENTINEL * 8},
    ]

    for body in sent:
        response = writer.post(prompt_url(), json=body)

        assert response.status_code == 422, response.text
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


# --- score_form_draft ---------------------------------------------------------------------------

SCORE_OTHER = {
    **DEMO_ANSWERS,
    "tamanho_equipe": "mais_de_15",
    "principal_desafio": "vendas",
    "canais": ["a", "b", "c", "d"],
}


def test_score_form_draft_gives_each_score_the_priority_and_the_persona_with_the_rule(
    writer: ApiClient,
) -> None:
    example = writer.post(score_url(SCORED), json={})
    mine = writer.post(score_url(SCORED), json={"answers": SCORE_OTHER})

    assert example.status_code == mine.status_code == 200, mine.text
    first, second = example.json(), mine.json()
    assert (first["uses_example_answers"], first["scored"]) == (True, True)
    assert (first["priority"], first["persona"]) == ("P2", "Em crescimento")
    assert second["uses_example_answers"] is False
    assert second["scores"] == {
        "porte": 100,
        "nivel_porte": "grande",
        "rotulo_porte": "Grande",
        "alcance": 50,
    }
    assert (second["priority"], second["priority_rule"]) == ("P1", 1)
    assert (second["persona"], second["persona_rule"]) == ("Comercial", 1)
    assert second["has_priority_rules"] is True and second["has_persona_rules"] is True


def test_score_form_draft_gives_the_same_scores_the_page_shows(writer: ApiClient) -> None:
    assert login(writer).status_code == 303
    page = post(writer, f"{PAGE}/forms/{SCORED}/draft/score", answers=json.dumps(SCORE_OTHER))

    body = writer.post(score_url(SCORED), json={"answers": SCORE_OTHER}).json()

    shown = text(section_of(page, "pontuacao-calculada"))
    assert page.status_code == 200
    for value in (*body["scores"].values(), body["priority"], body["persona"]):
        assert str(value) in shown, value


def test_score_form_draft_of_a_draft_without_scoring_says_it_has_none(writer: ApiClient) -> None:
    response = writer.post(score_url(), json={})

    assert response.status_code == 200
    assert response.json() == {
        "uses_example_answers": True,
        "scored": False,
        "scores": {},
        "priority": None,
        "priority_rule": None,
        "persona": None,
        "persona_rule": None,
        "has_priority_rules": False,
        "has_persona_rules": False,
    }


def test_score_form_draft_says_which_formula_does_not_calculate_with_these_answers(
    writer: ApiClient, deps: Deps
) -> None:
    scoring = copy.deepcopy(SCORING)
    scoring["scores"][1]["formula"] = "10 / (pontos(tamanho_equipe) - 25)"
    save_draft(deps, SCORED, scoring=scoring)  # com o exemplo (50), a conta não zera

    response = writer.post(
        score_url(SCORED),
        json={"answers": {**SCORE_OTHER, "tamanho_equipe": "so_eu", "canais": ["a"]}},
    )

    problems = fields_of(response)
    assert list(problems) == ["scoring.scores.1.formula"]
    assert problems["scoring.scores.1.formula"].startswith("com estas respostas, divisão por zero")


def test_score_form_draft_refuses_answers_that_do_not_validate_without_their_value(
    writer: ApiClient,
) -> None:
    response = writer.post(
        score_url(SCORED), json={"answers": {**SCORE_OTHER, "email": "sem-arroba"}}
    )

    assert fields_of(response) == {"answers.email": "e-mail inválido"}
    assert "sem-arroba" not in response.text


def test_score_form_draft_has_no_date_because_scoring_only_reads_the_answers(
    writer: ApiClient,
) -> None:
    response = writer.post(score_url(SCORED), json={"simulated_date": "2027-03-01"})

    assert fields_of(response) == {"simulated_date": "Campo desconhecido."}


def test_score_form_draft_writes_nothing_never_calls_the_llm_and_reads_no_form_that_is_not_there(
    writer: ApiClient, deps: Deps
) -> None:
    deps.llm = llm = NoLlm()
    nothing_changed = unchanged(deps)

    ok = writer.post(score_url(SCORED), json={"answers": SCORE_OTHER})
    missing = writer.post(score_url("nao-existe"), json={})

    assert ok.status_code == 200 and nothing_changed() and llm.calls == 0
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"


def test_score_form_draft_echoes_no_free_text_it_was_sent(writer: ApiClient) -> None:
    for answers in (
        {name: SENTINEL for name in SCORE_OTHER},
        {**SCORE_OTHER, "email": SENTINEL, "tamanho_equipe": SENTINEL, "canais": [SENTINEL]},
        {**SCORE_OTHER, SENTINEL: SENTINEL},  # a chave, e não só o valor
    ):
        response = writer.post(score_url(SCORED), json={"answers": answers})

        assert response.status_code == 422, response.text
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


# --- list_form_draft_tests ----------------------------------------------------------------------

TESTS_PAGE = f"{PAGE_DRAFT}/tests"


def test_list_form_draft_tests_gives_the_example_answers_the_prompt_and_no_tests_at_first(
    writer: ApiClient,
) -> None:
    response = writer.get(list_url())

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "items",  # o envelope das listas
        "next_cursor",
        "example_answers",
        "prompt",
        "problems",
        "keep_latest",
        "keep_days",
    }
    assert body["next_cursor"] is None
    assert body["example_answers"] == EXAMPLE
    assert body["prompt"]["uses_example_answers"] is True
    assert "Nome do escritório: Escritório Exemplo" in body["prompt"]["prompt"]
    assert (body["problems"], body["items"]) == ([], [])
    assert (body["keep_latest"], body["keep_days"]) == (
        draft_tests.KEEP_TESTS,
        draft_tests.TEST_DAYS,
    )


def test_list_form_draft_tests_lists_the_recent_tests_newest_first_and_keeps_only_a_few(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    ids = []
    for _ in range(draft_tests.KEEP_TESTS + 1):
        test_id = begin(deps, moment(clock))
        finish(deps, test_id, clock)
        ids.append(str(test_id))
        clock.advance(31)  # a pausa entre os testes
    last = begin(deps, moment(clock))  # o último segue na fila

    body = writer.get(list_url()).json()

    # ficam os 5 mais recentes: o novo e os 4 que terminaram por último (`start` abre a vaga)
    assert [item["id"] for item in body["items"]] == [str(last), *reversed(ids[2:])]
    assert len(body["items"]) == draft_tests.KEEP_TESTS and body["next_cursor"] is None
    newest, older = body["items"][0], body["items"][1]
    assert (newest["status"], newest["finished"], newest["finished_at"]) == ("queued", False, None)
    assert (older["status"], older["finished"]) == ("succeeded", True)
    assert older["started_at"].endswith("Z") and older["started_by"] == "chave:testes"
    assert set(newest) == {
        "id",
        "status",
        "finished",
        "started_by",
        "started_at",
        "finished_at",
        "simulated_date",
        "failure_code",
    }  # o resumo não traz o plano nem o prompt


def test_list_form_draft_tests_lists_the_same_tests_in_the_same_order_as_the_page(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    for _ in range(3):
        finish(deps, begin(deps, moment(clock)), clock)
        clock.advance(31)
    assert login(writer).status_code == 303
    page = writer.get(TESTS_PAGE).text

    body = writer.get(list_url()).json()

    on_the_page = re.findall(rf'href="{TESTS_PAGE}/([0-9a-f-]{{36}})"', page)
    assert [item["id"] for item in body["items"]] == on_the_page and len(on_the_page) == 3


def test_list_form_draft_tests_says_why_the_prompt_does_not_assemble_and_lists_the_tests_anyway(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    test_id = begin(deps, moment(clock))
    broken = json.loads(DEFINITION)
    broken["steps"] = []  # um deploy mais rígido já não compila a versão
    store_version(deps, 2, broken)

    response = writer.get(list_url())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["prompt"] is None and body["problems"]
    assert all(item["field"] and item["message"] for item in body["problems"])
    assert [item["id"] for item in body["items"]] == [str(test_id)]


def test_list_form_draft_tests_of_a_form_that_does_not_exist_is_not_found(
    writer: ApiClient,
) -> None:
    response = writer.get(list_url("nao-existe"))

    assert response.status_code == 404 and response.json()["code"] == "not_found"


def test_list_form_draft_tests_writes_nothing_and_never_calls_the_llm(
    writer: ApiClient, deps: Deps
) -> None:
    deps.llm = llm = NoLlm()
    nothing_changed = unchanged(deps)

    assert writer.get(list_url()).status_code == 200

    assert nothing_changed() and llm.calls == 0


# --- get_form_draft_test ------------------------------------------------------------------------

DETAIL = {
    "id",
    "status",
    "finished",
    "started_by",
    "started_at",
    "finished_at",
    "simulated_date",
    "failure_code",
    "draft_version",
    "publication",
    "answers",
    "instructions",
    "prompt",
    "plan",
    "model",
    "usage",
    "llm_ms",
    "pdf_ms",
    "pdf_pages",
    "has_pdf",
    "failure_detail",
}


def test_get_form_draft_test_of_a_test_still_running_brings_the_state_and_what_went_to_the_llm(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    test_id = begin(deps, moment(clock), answers=OTHER, day="2027-03-01")

    queued = writer.get(result_url(test_id))

    assert queued.status_code == 200, queued.text
    body = queued.json()
    assert set(body) == DETAIL
    assert (body["id"], body["status"], body["finished"]) == (str(test_id), "queued", False)
    assert body["started_by"] == "chave:testes" and body["simulated_date"] == "2027-03-01"
    assert body["answers"]["escritorio"] == "Outra Contadora"
    assert "Outra Contadora" in body["prompt"] and body["instructions"]
    assert body["draft_version"] == draft_of(deps).token
    for empty in ("plan", "model", "llm_ms", "pdf_ms", "pdf_pages", "failure_code", "publication"):
        assert body[empty] is None, empty
    assert body["has_pdf"] is False and body["usage"]["total_tokens"] is None


def test_get_form_draft_test_of_a_test_that_finished_brings_the_plan_the_times_and_the_tokens(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    deps.llm = ScriptedLlm()
    test_id = begin(deps, moment(clock))
    assert finish(deps, test_id, clock) == "succeeded"

    body = writer.get(result_url(test_id)).json()

    assert (body["status"], body["finished"], body["has_pdf"]) == ("succeeded", True, True)
    assert body["plan"]["titulo"] == "Organizar atividades do projeto"
    assert body["model"] == "gpt-teste" and body["pdf_pages"] == 1
    assert isinstance(body["llm_ms"], int) and isinstance(body["pdf_ms"], int)
    assert body["finished_at"] is not None and body["finished_at"].endswith("Z")
    assert body["usage"] == {
        "input_tokens": 3120,
        "output_tokens": 980,
        "total_tokens": 4100,
        "repairs": 1,
        "repair_reasons": ["acoes:too_short"],
        "fallback": False,
        "fake": False,
    }
    assert (body["failure_code"], body["failure_detail"]) == (None, None)


def test_get_form_draft_test_shows_the_usage_of_the_fake_llm_of_the_test_mode(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    deps.llm = FakePlanGenerator()
    test_id = begin(deps, moment(clock))
    finish(deps, test_id, clock)

    body = writer.get(result_url(test_id)).json()

    assert body["model"] == "fake" and body["usage"]["fake"] is True
    assert body["usage"]["total_tokens"] is None and body["usage"]["repairs"] is None


def test_get_form_draft_test_sends_the_llm_the_same_prompt_it_shows(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    deps.llm = llm = ScriptedLlm()
    test_id = begin(deps, moment(clock), answers=OTHER, day="2027-03-01")
    finish(deps, test_id, clock)

    body = writer.get(result_url(test_id)).json()

    (sent,) = llm.prompts
    assert sent == body["prompt"] and "Outra Contadora" in sent


def test_get_form_draft_test_of_a_test_that_failed_says_why_without_the_content(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    detail = "plano inválido depois do reparo: 2 erro(s) (too_short)"
    deps.llm = ScriptedLlm(error=RetryableError(FailureCode.PLAN_INVALID, detail))
    test_id = begin(deps, moment(clock))
    assert finish(deps, test_id, clock) == "failed"

    body = writer.get(result_url(test_id)).json()

    assert (body["status"], body["finished"]) == ("failed", True)
    assert (body["failure_code"], body["failure_detail"]) == ("plan_invalid", detail)
    assert (body["plan"], body["has_pdf"], body["publication"]) == (None, False, None)


def test_get_form_draft_test_of_a_test_the_engine_did_not_take_is_not_started(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    test_id = begin(deps, moment(clock))
    draft_tests.not_started(deps, test_id)

    body = writer.get(result_url(test_id)).json()

    assert (body["status"], body["finished"], body["failure_code"]) == (
        "failed",
        True,
        "not_started",
    )
    assert "o worker não recebeu o teste" in body["failure_detail"]


def test_get_form_draft_test_of_a_test_that_never_ends_is_interrupted_after_the_deadline(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    test_id = begin(deps, moment(clock))
    assert writer.get(result_url(test_id)).json()["status"] == "queued"

    clock.advance(7 * 60)
    body = writer.get(result_url(test_id)).json()

    assert (body["status"], body["finished"], body["failure_code"]) == (
        "interrupted",
        True,
        "interrupted",
    )


def test_get_form_draft_test_says_what_the_test_means_for_the_publication(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    test_id = begin(deps, moment(clock))
    finish(deps, test_id, clock)

    def publication() -> str | None:
        return writer.get(result_url(test_id)).json()["publication"]  # type: ignore[no-any-return]

    assert publication() == "current"  # testou o rascunho como está
    save_draft(deps, intro="Uma introdução nova, que o LLM não lê.")
    assert publication() == "unlocks"  # só mudou o que não vai ao LLM
    save_draft(deps, prompt=draft_of(deps).document["prompt"] + "\nMais uma linha.")
    assert publication() == "stale"  # o que vai ao LLM mudou depois do teste


def test_get_form_draft_test_is_not_found_for_what_is_not_a_test_of_this_form(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    other = begin(deps, moment(clock), slug=SCORED)

    for slug, test_id in (
        (SLUG, UNKNOWN_TEST),
        (SLUG, other),  # o teste é de outro formulário
        ("nao-existe", UNKNOWN_TEST),
    ):
        response = writer.get(result_url(test_id, slug))

        assert response.status_code == 404 and response.json()["code"] == "not_found", slug
    malformed = writer.get(f"{API}/forms/{SLUG}/draft/tests/nao-e-um-id")
    assert malformed.status_code == 404 and malformed.json()["code"] == "not_found"
    assert writer.get(result_url(other, SCORED)).status_code == 200


def test_get_form_draft_test_gives_what_the_status_fragment_of_the_page_says(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    assert login(writer).status_code == 303
    test_id = begin(deps, moment(clock))
    status = f"{TESTS_PAGE}/{test_id}/status"

    running = (writer.get(status).status_code, writer.get(result_url(test_id)).json()["finished"])
    finish(deps, test_id, clock)
    done = (writer.get(status).status_code, writer.get(result_url(test_id)).json()["finished"])

    assert running == (200, False)  # o polling continua
    assert done == (286, True)  # o polling para
    assert writer.get(f"{TESTS_PAGE}/{UNKNOWN_TEST}/status").status_code == 404


def test_get_form_draft_test_writes_nothing_and_never_calls_the_llm(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    test_id = begin(deps, moment(clock))
    deps.llm = llm = NoLlm()
    nothing_changed = unchanged(deps)

    assert writer.get(result_url(test_id)).status_code == 200

    assert nothing_changed() and llm.calls == 0


def test_get_form_draft_test_echoes_no_id_it_was_sent(writer: ApiClient) -> None:
    response = writer.get(f"{API}/forms/{SLUG}/draft/tests/{SENTINEL}")

    assert response.status_code == 404
    assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


# --- download_form_draft_test_pdf ---------------------------------------------------------------


def test_download_form_draft_test_pdf_gives_the_file_the_page_downloads(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    test_id = begin(deps, moment(clock))
    finish(deps, test_id, clock)
    assert login(writer).status_code == 303
    by_the_page = writer.get(f"{TESTS_PAGE}/{test_id}/pdf")

    response = writer.get(pdf_url(test_id))

    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF") and response.content == by_the_page.content
    assert response.headers["content-disposition"] == by_the_page.headers["content-disposition"]
    assert re.fullmatch(
        rf'attachment; filename="teste-{SLUG}-\d{{8}}-\d{{4}}\.pdf"',
        response.headers["content-disposition"],
    )
    assert response.headers["cache-control"] == "no-store"


def test_download_form_draft_test_pdf_is_readable_by_a_read_key_because_the_data_is_the_operators(
    reader: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    test_id = begin(deps, moment(clock))
    finish(deps, test_id, clock)

    assert reader.get(pdf_url(test_id)).status_code == 200


def test_download_form_draft_test_pdf_is_not_found_while_the_test_has_no_pdf(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    queued = begin(deps, moment(clock))
    page = writer.get(pdf_url(queued))
    assert login(writer).status_code == 303
    page_says = writer.get(f"{TESTS_PAGE}/{queued}/pdf")

    deps.llm = ScriptedLlm(error=RetryableError(FailureCode.PLAN_INVALID, "plano inválido"))
    finish(deps, queued, clock)
    failed = writer.get(pdf_url(queued))

    for response in (page, failed, page_says):
        assert response.status_code == 404
    assert page.json()["code"] == "not_found" and "não chegou até lá" in page.json()["detail"]


def test_download_form_draft_test_pdf_is_not_found_for_what_is_not_a_test_of_this_form(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    other = begin(deps, moment(clock), slug=SCORED)
    finish(deps, other, clock)

    for slug, test_id in ((SLUG, UNKNOWN_TEST), (SLUG, other), ("nao-existe", UNKNOWN_TEST)):
        response = writer.get(pdf_url(test_id, slug))

        assert response.status_code == 404 and response.json()["code"] == "not_found", slug
    assert writer.get(pdf_url(other, SCORED)).status_code == 200


def test_download_form_draft_test_pdf_costs_five_tokens_of_the_usage_limit(
    api_client: Client, world: None, deps: Deps, clock: FakeClock
) -> None:
    client = api_client(
        "leitura", clock=clock, API_RATE_LIMIT_PER_MINUTE="5"
    )  # o balde tem 5 fichas
    test_id = begin(deps, moment(clock))
    finish(deps, test_id, clock)

    first = client.get(pdf_url(test_id))
    second = client.get(pdf_url(test_id))

    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["code"] == "rate_limited"
    assert int(second.headers["retry-after"]) >= 1


def test_download_form_draft_test_pdf_writes_nothing_and_echoes_no_id(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    test_id = begin(deps, moment(clock))
    finish(deps, test_id, clock)
    nothing_changed = unchanged(deps)

    ok = writer.get(pdf_url(test_id))
    bad = writer.get(f"{API}/forms/{SLUG}/draft/tests/{SENTINEL}/pdf")

    assert ok.status_code == 200 and nothing_changed()
    assert bad.status_code == 404
    assert SENTINEL not in bad.text and SENTINEL not in str(bad.headers)


# --- start_form_draft_test ----------------------------------------------------------------------


def begin_via_api(client: ApiClient, slug: str = SLUG, **changes: Any) -> httpx.Response:
    return client.post(start_url(slug), json={"confirm": True, **changes})


def stored_tests(deps: Deps) -> list[DraftTest]:
    with deps.sessions() as session:
        return list(session.scalars(sa.select(DraftTest).order_by(DraftTest.started_at)))


def test_a_read_key_cannot_start_a_draft_test_and_nothing_happens(
    reader: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    deps.llm = llm = NoLlm()
    nothing_changed = unchanged(deps)

    response = begin_via_api(reader)

    assert response.status_code == 403 and response.json()["code"] == "forbidden_role"
    assert nothing_changed() and worker.queued == [] and llm.calls == 0
    assert audited(deps, "form.draft.test") == []


@pytest.mark.parametrize(
    "confirm",
    [None, False, "true", 1, "sim"],
    ids=["absent", "false", "text-true", "one", "yes"],
)
def test_start_form_draft_test_without_a_confirm_true_is_refused_and_nothing_happens(
    writer: ApiClient, deps: Deps, worker: TestTrigger, confirm: Any
) -> None:
    deps.llm = llm = NoLlm()
    nothing_changed = unchanged(deps)
    body = {} if confirm is None else {"confirm": confirm}

    response = writer.post(start_url(), json=body)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "chamada paga ao LLM" in response.json()["detail"]
    assert nothing_changed() and worker.queued == [] and llm.calls == 0
    assert audited(deps, "form.draft.test") == []


def test_start_form_draft_test_asks_for_the_confirmation_before_it_looks_at_the_body(
    writer: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    response = writer.post(
        start_url(), json={"answers": {"nome": 1}, "simulated_date": "amanhã", "extra": 1}
    )

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert stored_tests(deps) == [] and worker.queued == []


def test_start_form_draft_test_queues_the_test_and_launches_the_task_with_only_its_id(
    writer: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    response = begin_via_api(writer, answers=OTHER, simulated_date="2027-03-01")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {*DETAIL, "launched"}
    assert body["launched"] is True and (body["status"], body["finished"]) == ("queued", False)
    assert body["started_by"] == writer.actor and writer.actor.startswith("chave:")
    assert body["simulated_date"] == "2027-03-01"
    assert body["answers"]["escritorio"] == "Outra Contadora"
    assert "Outra Contadora" in body["prompt"] and body["plan"] is None
    # a task do Hatchet recebe o id do teste, e nada mais
    assert [type(item) for item in worker.queued] == [uuid.UUID]
    assert str(worker.queued[0]) == body["id"]
    (row,) = stored_tests(deps)
    assert (str(row.id), row.status, row.started_by) == (body["id"], "queued", writer.actor)
    assert body["draft_version"] == draft_of(deps).token == row.definition_fingerprint


def test_start_form_draft_test_is_audited_in_the_name_of_the_key_without_the_answers(
    writer: ApiClient, deps: Deps
) -> None:
    body = begin_via_api(writer, answers=OTHER, simulated_date="2027-03-01").json()

    (entry,) = audited(deps, "form.draft.test")
    assert entry.actor == writer.actor
    assert entry.details == {
        "form_slug": SLUG,
        "test": body["id"],
        "fingerprint": draft_of(deps).token,
        "simulated_date": "2027-03-01",
    }
    assert "Outra Contadora" not in json.dumps(entry.details)


def test_the_worker_sends_the_llm_the_prompt_the_start_answered_with_and_the_result_can_be_read(
    writer: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    deps.llm = llm = ScriptedLlm()
    started = begin_via_api(writer, answers=OTHER, simulated_date="2027-03-01").json()
    assert llm.prompts == []  # a chamada não chama o LLM: quem chama é o worker

    worker.run()

    (sent,) = llm.prompts
    assert sent == started["prompt"] and "Outra Contadora" in sent
    done = writer.get(result_url(started["id"])).json()
    assert (done["status"], done["finished"], done["has_pdf"]) == ("succeeded", True, True)
    assert done["publication"] == "current" and done["usage"]["total_tokens"] == 4100
    assert done["plan"]["titulo"] == "Organizar atividades do projeto"


def test_start_form_draft_test_uses_the_example_answers_and_today_when_the_body_has_neither(
    writer: ApiClient, deps: Deps
) -> None:
    body = begin_via_api(writer).json()

    assert body["answers"] == EXAMPLE and body["simulated_date"] is None
    assert stored_tests(deps)[0].simulated_date is None


def test_start_form_draft_test_never_creates_a_submission_a_request_a_plan_a_file_or_a_delivery(
    writer: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    webhook = record_webhook(deps)  # há destino: um pedido de verdade seria entregue

    begin_via_api(writer)
    worker.run()

    for model in (Submission, Job, Plan, Artifact, Delivery):
        assert count(deps, model) == 0, model.__name__
    assert webhook.calls == []


def test_an_engine_that_does_not_receive_the_task_ends_the_test_as_not_started_and_blocks_nothing(
    writer: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    deps.llm = llm = NoLlm()
    worker.accepts = False

    response = begin_via_api(writer)

    assert response.status_code == 200, response.text  # um disparo que não chega não é erro
    body = response.json()
    assert body["launched"] is False
    assert (body["status"], body["finished"], body["failure_code"]) == (
        "failed",
        True,
        "not_started",
    )
    assert "o worker não recebeu o teste" in body["failure_detail"]
    assert worker.queued == [] and llm.calls == 0
    (entry,) = audited(deps, "form.draft.test")  # a partida existiu e fica na auditoria
    assert entry.details["test"] == body["id"]
    worker.accepts = True
    assert begin_via_api(writer).status_code == 200  # sem pausa: o LLM não foi chamado


def test_a_test_in_progress_refuses_the_next_one_with_a_state_conflict_and_makes_no_second_test(
    writer: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    first = begin_via_api(writer).json()
    deps.llm = llm = NoLlm()

    second = begin_via_api(writer)

    assert second.status_code == 409 and second.json()["code"] == "state_conflict"
    assert second.json()["detail"] == draft_tests.BUSY
    assert "retry-after" not in second.headers
    assert [str(item) for item in worker.queued] == [first["id"]]  # só o primeiro foi disparado
    assert [str(row.id) for row in stored_tests(deps)] == [first["id"]]
    assert len(audited(deps, "form.draft.test")) == 1 and llm.calls == 0


def test_the_pause_after_a_test_refuses_the_next_with_a_retry_after_and_ends_in_thirty_seconds(
    writer: ApiClient, deps: Deps, worker: TestTrigger, clock: FakeClock
) -> None:
    begin_via_api(writer)
    worker.run()

    soon = begin_via_api(writer)
    clock.advance(10)
    later = begin_via_api(writer)

    assert soon.status_code == later.status_code == 429
    assert soon.json()["code"] == "test_paused" and "Espere 30 s" in soon.json()["detail"]
    assert (soon.headers["retry-after"], later.headers["retry-after"]) == ("30", "20")
    assert len(stored_tests(deps)) == 1 and len(audited(deps, "form.draft.test")) == 1
    clock.advance(21)
    assert begin_via_api(writer).status_code == 200


def test_repeating_start_form_draft_test_after_a_lost_response_makes_no_second_test(
    writer: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    """A resposta se perdeu e o cliente repete a mesma chamada: com o teste ainda na fila é `409`, e
    com ele terminado, dentro da pausa, é `429`. Nos dois casos o LLM é chamado uma vez só."""
    deps.llm = llm = ScriptedLlm()
    body = {"confirm": True, "answers": OTHER, "simulated_date": "2027-03-01"}
    first = writer.post(start_url(), json=body)
    repeated_while_queued = writer.post(start_url(), json=body)
    worker.run()
    repeated_after_it_ended = writer.post(start_url(), json=body)

    assert first.status_code == 200
    assert repeated_while_queued.status_code == 409
    assert repeated_after_it_ended.status_code == 429
    assert len(stored_tests(deps)) == 1 and len(llm.prompts) == 1


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"answers": {**EXAMPLE, "email": "sem-arroba"}}, "answers.email", "e-mail inválido"),
        ({"answers": {}}, "answers.nome", "campo obrigatório"),
        ({"simulated_date": "2027-02-30"}, "simulated_date", "use uma data válida"),
        ({"simulated_date": "2101-01-01"}, "simulated_date", "use uma data entre 2000 e 2100"),
    ],
    ids=["answer", "empty-object", "date", "date-out-of-range"],
)
def test_start_form_draft_test_refuses_what_does_not_validate_and_starts_and_spends_nothing(
    writer: ApiClient,
    deps: Deps,
    worker: TestTrigger,
    changes: dict[str, Any],
    field: str,
    message: str,
) -> None:
    deps.llm = llm = NoLlm()
    nothing_changed = unchanged(deps)

    response = begin_via_api(writer, **changes)

    assert message in fields_of(response)[field]
    assert nothing_changed() and worker.queued == [] and llm.calls == 0
    assert "sem-arroba" not in response.text  # nunca o valor recebido


def test_start_form_draft_test_does_not_know_the_date_by_the_name_the_page_gives_it(
    writer: ApiClient, worker: TestTrigger
) -> None:
    response = begin_via_api(writer, date="2027-03-01")

    assert fields_of(response) == {"date": "Campo desconhecido."} and worker.queued == []


def test_start_form_draft_test_of_a_form_that_does_not_exist_is_not_found(
    writer: ApiClient, worker: TestTrigger
) -> None:
    response = begin_via_api(writer, "nao-existe")

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert worker.queued == []


def test_start_form_draft_test_echoes_no_free_text_it_was_sent(
    writer: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    sent = [
        {"answers": {name: SENTINEL for name in EXAMPLE}, "simulated_date": SENTINEL},
        {"answers": {**EXAMPLE, "email": SENTINEL, "tamanho_equipe": SENTINEL}},
        {"answers": {**EXAMPLE, SENTINEL: SENTINEL}},  # a chave, e não só o valor
        {"simulated_date": SENTINEL * 8},
    ]

    for changes in sent:
        response = begin_via_api(writer, **changes)

        assert response.status_code == 422, response.text
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)
    assert stored_tests(deps) == [] and worker.queued == []


def test_an_answer_for_a_question_the_form_does_not_have_is_refused_without_the_key_by_all_three(
    writer: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    """A chave de uma resposta é texto de quem chama, e a API não devolve o que recebeu: o `422` é um
    só, no campo `answers`, e os outros problemas das respostas saem junto, cada um no seu caminho."""
    body = {"answers": {**EXAMPLE, SENTINEL: "x", "email": "sem-arroba"}}
    expected = {
        "answers": "resposta para uma pergunta que o formulário não tem",
        "answers.email": "e-mail inválido",
    }

    for response in (
        writer.post(prompt_url(), json=body),
        writer.post(score_url(), json=body),
        begin_via_api(writer, **body),
    ):
        assert fields_of(response) == expected
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)
    assert stored_tests(deps) == [] and worker.queued == []


def test_a_test_started_by_the_api_logs_only_ids_and_never_the_answers_the_prompt_or_the_plan(
    writer: ApiClient,
    worker: TestTrigger,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # O `fileConfig` do alembic, que a suíte roda na subida, desliga os loggers que já existiam:
    # religa os da ação e do serviço, ou a ausência no log não provaria nada.
    for logger in (draft_test_actions.logger, draft_tests.logger):
        monkeypatch.setattr(logger, "disabled", False)
    with caplog.at_level(logging.DEBUG):
        body = begin_via_api(writer, answers=OTHER).json()
        worker.run()
        result = writer.get(result_url(body["id"])).json()

    assert body["id"] in caplog.text and writer.actor in caplog.text
    for secret in ("Outra Contadora", body["prompt"], result["plan"]["titulo"], "mais_de_15"):
        assert secret not in caplog.text, secret


# --- a paridade de start_form_draft_test com a página --------------------------------------------

Seen = list[dict[str, Any]]
# O que a partida carimba e a auditoria guarda com o id do teste, que muda a cada execução.
PARITY_IGNORE = {**IGNORE, "audit_log": {"details"}}


def seen_now(deps: Deps, worker: TestTrigger, llm: RecordingFake | None = None) -> dict[str, Any]:
    """O que o Hatchet e o LLM receberam desde a última vez, e a auditoria da partida (com o id do
    teste trocado por `<teste>`): o que a página e a API têm de deixar igual, além do banco."""
    launched, worker.queued = list(worker.queued), []
    rows = {str(row.id) for row in stored_tests(deps)}
    audit = [
        {key: "<teste>" if value in rows else value for key, value in entry.details.items()}
        for entry in audited(deps, "form.draft.test")
    ]
    prompts: list[str] = []
    if llm is not None:
        prompts, llm.prompts = list(llm.prompts), []
    return {
        "launched_a_test_of_the_database": [str(item) in rows for item in launched],
        "audit": audit,
        "prompts": prompts,
    }


def page_starts(
    client: ApiClient,
    deps: Deps,
    worker: TestTrigger,
    seen: Seen,
    *,
    run: RecordingFake | None = None,
    **form: str,
) -> Callable[[], httpx.Response]:
    """A operadora inicia o teste pela página (com o login dela, `pagina`); com `run`, o worker roda
    o teste em seguida e o que foi ao LLM entra no que se compara."""

    def call() -> httpx.Response:
        assert login(client, "pagina").status_code == 303
        response = post(client, TESTS_PAGE, **form)
        if run is not None:
            worker.run()
        seen.append(seen_now(deps, worker, run))
        return response

    return call


def api_starts(
    client: ApiClient,
    deps: Deps,
    worker: TestTrigger,
    seen: Seen,
    *,
    run: RecordingFake | None = None,
    **changes: Any,
) -> Callable[[], httpx.Response]:
    def call() -> httpx.Response:
        response = begin_via_api(client, **changes)
        if run is not None:
            worker.run()
        seen.append(seen_now(deps, worker, run))
        return response

    return call


@pytest.fixture
def both(api_client: Client, world: None, clock: FakeClock, worker: TestTrigger) -> ApiClient:
    """Um cliente que é a operadora na página (`pagina`) e uma chave `total` na API: o autor da página
    é diferente do `operadora` do mundo de teste, para as linhas que o mundo gravou não se confundirem."""
    return api_client("total", clock=clock, test_trigger=worker, ADMIN_USERNAME="pagina")


def test_start_form_draft_test_leaves_the_same_state_audit_and_launch_as_the_page(
    both: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    seen: Seen = []

    page, api = assert_same_effect(
        page_starts(both, deps, worker, seen, answers=json.dumps(OTHER), date="2027-03-01"),
        api_starts(both, deps, worker, seen, answers=OTHER, simulated_date="2027-03-01"),
        api_actor=both.actor,
        page_actor="pagina",
        ignore=PARITY_IGNORE,
    )

    assert (page.status_code, api.status_code) == (303, 200)
    assert seen[0] == seen[1]
    assert seen[0]["launched_a_test_of_the_database"] == [
        True
    ]  # um disparo, do teste que foi gravado
    (entry,) = seen[0]["audit"]
    assert set(entry) == {"form_slug", "test", "fingerprint", "simulated_date"}


def test_the_worker_does_the_same_with_the_test_the_page_started_and_the_one_the_api_started(
    both: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    deps.llm = llm = RecordingFake()
    seen: Seen = []

    assert_same_effect(
        page_starts(
            both, deps, worker, seen, run=llm, answers=json.dumps(OTHER), date="2027-03-01"
        ),
        api_starts(both, deps, worker, seen, run=llm, answers=OTHER, simulated_date="2027-03-01"),
        api_actor=both.actor,
        page_actor="pagina",
        ignore={**PARITY_IGNORE, "draft_tests": {"llm_ms", "pdf_ms"}},  # o que o relógio mediu
    )

    assert seen[0] == seen[1] and len(seen[0]["prompts"]) == 1  # o mesmo prompt foi ao LLM, uma vez
    assert "Outra Contadora" in seen[0]["prompts"][0]


def test_an_engine_that_does_not_receive_the_task_ends_the_test_the_same_way_on_the_page_and_the_api(
    both: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    worker.accepts = False
    seen: Seen = []

    page, api = assert_same_effect(
        page_starts(both, deps, worker, seen),
        api_starts(both, deps, worker, seen),
        api_actor=both.actor,
        page_actor="pagina",
        ignore=PARITY_IGNORE,
    )

    assert (page.status_code, api.status_code) == (303, 200)
    assert seen[0] == seen[1] and seen[0]["launched_a_test_of_the_database"] == []
    assert stored_tests(deps)[0].failure_code == "not_started"


def test_a_test_in_progress_is_refused_by_the_page_and_the_api_alike_and_nothing_is_launched(
    both: ApiClient, deps: Deps, worker: TestTrigger, clock: FakeClock
) -> None:
    begin(deps, moment(clock))
    seen: Seen = []

    page, api = assert_same_effect(
        page_starts(both, deps, worker, seen),
        api_starts(both, deps, worker, seen),
        api_actor=both.actor,
        page_actor="pagina",
        ignore=PARITY_IGNORE,
        expect_no_change=True,
    )

    assert (page.status_code, api.status_code) == (409, 409)
    assert draft_tests.BUSY in text(page.text) and api.json()["detail"] == draft_tests.BUSY
    assert [item["launched_a_test_of_the_database"] for item in seen] == [[], []]


def test_the_pause_is_refused_by_the_page_and_the_api_alike_with_the_same_retry_after(
    both: ApiClient, deps: Deps, worker: TestTrigger, clock: FakeClock
) -> None:
    finish(deps, begin(deps, moment(clock)), clock)
    clock.advance(10)
    seen: Seen = []

    page, api = assert_same_effect(
        page_starts(both, deps, worker, seen),
        api_starts(both, deps, worker, seen),
        api_actor=both.actor,
        page_actor="pagina",
        ignore=PARITY_IGNORE,
        expect_no_change=True,
    )

    assert (page.status_code, api.status_code) == (429, 429)
    assert page.headers["retry-after"] == api.headers["retry-after"] == "20"
    assert "Espere 20 s" in text(page.text) and "Espere 20 s" in api.json()["detail"]
    assert api.json()["code"] == "test_paused"
    assert [item["launched_a_test_of_the_database"] for item in seen] == [[], []]


def test_answers_that_do_not_validate_start_nothing_on_the_page_or_on_the_api(
    both: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    wrong = {**EXAMPLE, "email": "sem-arroba"}
    seen: Seen = []

    page, api = assert_same_effect(
        page_starts(both, deps, worker, seen, answers=json.dumps(wrong)),
        api_starts(both, deps, worker, seen, answers=wrong),
        api_actor=both.actor,
        page_actor="pagina",
        ignore=PARITY_IGNORE,
        expect_no_change=True,
    )

    assert (page.status_code, api.status_code) == (422, 422)
    assert "answers.email: e-mail inválido" in problems_shown(page)
    assert fields_of(api) == {"answers.email": "e-mail inválido"}
    assert [item["launched_a_test_of_the_database"] for item in seen] == [[], []]


def test_a_form_that_does_not_exist_starts_nothing_on_the_page_or_on_the_api(
    both: ApiClient, deps: Deps, worker: TestTrigger
) -> None:
    seen: Seen = []

    def on_the_page() -> httpx.Response:
        assert login(both, "pagina").status_code == 303
        response = post(both, f"{PAGE}/forms/nao-existe/draft/tests")
        seen.append(seen_now(deps, worker))
        return response

    page, api = assert_same_effect(
        on_the_page,
        api_starts_in(both, deps, worker, seen, "nao-existe"),
        api_actor=both.actor,
        page_actor="pagina",
        ignore=PARITY_IGNORE,
        expect_no_change=True,
    )

    assert (page.status_code, api.status_code) == (404, 404)
    assert [item["launched_a_test_of_the_database"] for item in seen] == [[], []]


def api_starts_in(
    client: ApiClient, deps: Deps, worker: TestTrigger, seen: Seen, slug: str
) -> Callable[[], httpx.Response]:
    def call() -> httpx.Response:
        response = begin_via_api(client, slug)
        seen.append(seen_now(deps, worker))
        return response

    return call


@pytest.mark.parametrize(
    "operation", ["assemble_form_draft_prompt", "score_form_draft", "start_form_draft_test"]
)
def test_expected_version_conflict_is_compatible_and_has_no_effects(
    writer, deps, worker, operation
):
    before = unchanged(deps)
    body = (
        {"expected_version": "stale", "confirm": True}
        if operation == "start_form_draft_test"
        else {"expected_version": "stale"}
    )
    response = writer.post(url_of(operation, slug=SLUG), json=body)
    assert response.status_code == 409
    assert response.json()["code"] == "state_conflict"
    assert before()
    assert worker.queued == []


@pytest.mark.parametrize(
    "operation",
    [
        "assemble_form_draft_prompt",
        "score_form_draft",
        "test_capture_form_draft",
        "start_form_draft_test",
    ],
)
def test_mcp_forwards_optional_expected_version_and_refuses_stale_origin(
    writer, deps, worker, tmp_path, operation
):
    from admin_mcp_live import Live
    from admin_mcp_world import Asker

    slug = SLUG
    if operation == "test_capture_form_draft":
        slug = drafts.create(
            deps,
            title="Pesquisa sintética",
            slug="version-capture",
            actor="testes",
            generation="none",
        )
    live = Live(writer, tmp_path)
    try:
        assert "expected_version" in live.tools[operation].input_schema["properties"]
        before = unchanged(deps)
        result = live.call(
            operation,
            {"slug": slug, "expected_version": "stale", "confirm": True}
            if operation == "start_form_draft_test"
            else {"slug": slug, "expected_version": "stale"},
            Asker(True),
        )
        assert result.is_error
        assert "409 state_conflict" in result.text
        assert before()
        assert worker.queued == []
    finally:
        live.close()
