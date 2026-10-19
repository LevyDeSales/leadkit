"""A pontuação declarativa no admin, pela costura HTTP (TestClient): os pontos de cada opção no
editor, a pontuação com as respostas de exemplo ou com outras, a leitura da versão publicada, o que
a publicação barra e o envio de uma lead até o webhook, com a pontuação nos mesmos campos de
sempre."""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from conftest import DEMO_ANSWERS, Receiver
from fastapi.testclient import TestClient
from pydantic import BaseModel
from test_admin import FakeClock, finish, login
from test_admin_destinations import ADMIN_ENV, create
from test_admin_drafts import (
    EDITOR_FORM,
    Fields,
    TestTrigger,
    editor,
    hidden,
    post,
    problems_shown,
    textareas,
)
from test_admin_drafts import save as save_in
from test_admin_forms import import_form, section_of
from test_public import form_fields, job_id_of, tags, text
from test_scoring import definition
from test_weasyprint_pdf import text_of

from app import pipeline
from app.adapters.llm import FakePlanGenerator, LlmResult
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.api.main import create_app
from app.deps import Deps
from app.forms import FormSpec, drafts
from app.models import FormDraft
from app.settings import Settings

SLUG = "pontuacao-demo"
DRAFT = f"/admin/forms/{SLUG}/draft"
TESTS = f"{DRAFT}/tests"
PUBLISH = f"{DRAFT}/publish"
CONSENT_VERSION = f"{SLUG}-v1"
SCORING: dict[str, Any] = {
    "scores": [
        {
            "name": "porte",
            "formula": "pontos(tamanho_equipe)",
            "bands": {
                "key": "nivel_porte",
                "label": "rotulo_porte",
                "levels": [
                    {"min": 90, "key": "grande", "label": "Grande"},
                    {"min": 60, "key": "medio", "label": "Médio"},
                    {"key": "pequeno", "label": "Pequeno"},
                ],
            },
        },
        {"name": "alcance", "formula": "arredondar_meio_para_cima(contagem(canais) * 12.5)"},
    ],
    "priority": [
        {"when": "porte >= 100", "value": "P1"},
        {"when": "porte >= 50", "value": "P2"},
        {"value": "P3"},
    ],
    "persona": [
        {"when": 'principal_desafio == "vendas"', "value": "Comercial"},
        {"when": "porte >= 50", "value": "Em crescimento"},
        {"value": "Iniciante"},
    ],
}
# O PDF mostra uma nota: as notas chegam ao template como no envio de verdade.
PDF_LINE = "<p>Porte {{ scores.porte }} nível {{ scores.rotulo_porte }}</p>"


def scored_definition(**changes: Any) -> dict[str, Any]:
    document = definition(copy.deepcopy(SCORING))
    document["slug"] = SLUG
    document["consent"]["version"] = CONSENT_VERSION
    document["pdf"]["html"] = document["pdf"]["html"].replace("</body>", PDF_LINE + "</body>")
    document["pdf"]["scores"] = ["porte"]  # a nota e o rótulo da faixa dela chegam ao PDF
    return {**document, **changes}


DEFINITION = json.dumps(scored_definition())


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def worker(deps: Deps, clock: FakeClock) -> TestTrigger:
    return TestTrigger(deps, clock)


def operator(
    deps: Deps, clock: FakeClock, worker: TestTrigger, *, real_pdf_on_import: bool
) -> TestClient:
    """A operadora no admin, com o formulário de pontuação importado (versão 1) e o PDF de verdade
    (ele lê as notas) depois dele. A importação renderiza o PDF de exemplo, e o WeasyPrint de verdade
    leva uns 150 ms: só o teste que prova isso (`real_pdf_on_import`) paga por ele; nos outros, a
    importação usa o PDF falso."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, test_trigger=worker, clock=clock),
        follow_redirects=False,
    )
    assert login(current).status_code == 303
    if real_pdf_on_import:
        deps.pdf = WeasyPrintRenderer()
    assert import_form(current, DEFINITION).status_code == 303
    deps.pdf = WeasyPrintRenderer()  # o PDF de verdade: ele lê as notas
    return current


@pytest.fixture
def client(deps: Deps, clock: FakeClock, worker: TestTrigger) -> TestClient:
    return operator(deps, clock, worker, real_pdf_on_import=False)


def test_the_scoring_form_imports_with_the_real_pdf(
    deps: Deps, clock: FakeClock, worker: TestTrigger
) -> None:
    """A importação renderiza o PDF de exemplo no WeasyPrint, com as notas das respostas de exemplo:
    o formulário de pontuação, que os editores usam, passa por isso."""
    current = operator(deps, clock, worker, real_pdf_on_import=True)

    assert current.get(f"/admin/forms/{SLUG}").status_code == 200


def save(client: TestClient, change: Callable[[Fields], Any] | None = None) -> httpx.Response:
    """Salva o rascunho deste formulário pelo editor, com a mudança aplicada aos campos."""
    return save_in(client, change, DRAFT)


def stored(deps: Deps) -> dict[str, Any]:
    with deps.sessions() as session:
        found = drafts.load(session, SLUG)
    assert found is not None
    return found.document


def scoring_fields(scoring: dict[str, Any]) -> Fields:
    """O bloco `scoring` como o editor em campos o manda: um campo para cada nota, nível e regra
    (`app.admin.scoring_editor`)."""
    found: Fields = {}
    for i, score in enumerate(scoring["scores"]):
        found[f"scoring.scores.{i}.name"] = score["name"]
        found[f"scoring.scores.{i}.formula"] = score["formula"]
        if bands := score.get("bands"):
            found[f"scoring.scores.{i}.bands.key"] = bands["key"]
            found[f"scoring.scores.{i}.bands.label"] = bands["label"]
            for k, level in enumerate(bands["levels"]):
                at = f"scoring.scores.{i}.bands.levels.{k}"
                found[f"{at}.min"] = str(level.get("min", ""))
                found[f"{at}.key"] = level["key"]
                found[f"{at}.label"] = level["label"]
    for kind in ("priority", "persona"):
        for i, rule in enumerate(scoring.get(kind, ())):
            at = f"scoring.{kind}.{i}"
            found[f"{at}.when"] = rule.get("when", "")
            if "answer" in rule:
                found[f"{at}.result"] = "answer"
                found[f"{at}.answer"] = rule["answer"]
            else:
                found[f"{at}.value"] = rule["value"]
                if kind == "persona":
                    found[f"{at}.result"] = "value"
    return found


def edit_scoring(change: Callable[[dict[str, Any]], None]) -> Callable[[Fields], None]:
    """Muda a pontuação do formulário (a de `SCORING`) e troca os campos dela no editor."""

    def apply(fields: Fields) -> None:
        scoring = copy.deepcopy(SCORING)
        change(scoring)
        for name in [name for name in fields if name.startswith("scoring.")]:
            del fields[name]
        fields.update(scoring_fields(scoring))

    return apply


def shown(response: httpx.Response, section_id: str) -> str:
    return text(section_of(response, section_id))


# --- os pontos de cada opção no editor ----------------------------------------------------------


def test_the_editor_shows_the_points_of_each_option_and_saving_keeps_them(
    client: TestClient, deps: Deps
) -> None:
    fields = editor(client, DRAFT)

    assert fields["steps.1.questions.0.options.1.value"] == "2_a_5"
    assert fields["steps.1.questions.0.options.1.points"] == "50"
    assert fields["steps.1.questions.0.options.3.points"] == "100"
    assert fields["steps.1.questions.2.options.2.points"] == "4"  # a escolha múltipla também
    assert "steps.1.questions.1.options.0.points" in fields  # sem pontos: o campo vem em branco
    assert fields["steps.1.questions.1.options.0.points"] == ""

    assert save(client).status_code == 303
    assert stored(deps) == json.loads(DEFINITION)  # nada se perdeu e nada ficou pendente


def test_every_points_field_has_a_label(client: TestClient) -> None:
    page = client.get(DRAFT).text
    labelled = {str(label["for"]) for label in tags(page, "label") if label.get("for")}
    points = [t for t in tags(page, "input") if str(t.get("name", "")).endswith(".points")]

    assert len(points) == 12  # as opções da equipe, dos desafios e dos canais
    for control in points:
        assert control.get("id") in labelled, control["name"]


def test_the_operator_changes_and_clears_the_points_of_an_option(
    client: TestClient, deps: Deps
) -> None:
    def change(fields: Fields) -> None:
        fields["steps.1.questions.0.options.0.points"] = "30"
        fields["steps.1.questions.2.options.0.points"] = "1.5"
        fields["steps.1.questions.2.options.1.points"] = " 2,5 "  # a vírgula vale como ponto

    assert save(client, change).status_code == 303

    options = stored(deps)["steps"][1]["questions"][0]["options"]
    assert options[0]["points"] == 30 and options[1]["points"] == 50
    canais = stored(deps)["steps"][1]["questions"][2]["options"]
    assert (canais[0]["points"], canais[1]["points"]) == (1.5, 2.5)


def test_an_option_that_loses_its_points_breaks_the_formula_that_reads_them(
    client: TestClient, deps: Deps
) -> None:
    def clear(fields: Fields) -> None:
        fields["steps.1.questions.0.options.2.points"] = ""

    response = save(client, clear)

    assert response.status_code == 422
    assert "pontos(tamanho_equipe): faltam os pontos das opções 6_a_15" in problems_shown(response)
    assert stored(deps) == json.loads(DEFINITION)


def test_points_that_are_not_a_number_show_the_problem_on_the_field(
    client: TestClient, deps: Deps
) -> None:
    name = "steps.1.questions.0.options.1.points"

    def change(fields: Fields) -> None:
        fields[name] = "muito"

    response = save(client, change)

    assert response.status_code == 422
    assert "precisa ser um número" in problems_shown(response)
    (control,) = tags(response.text, "input", name=name)
    assert control["aria-invalid"] == "true" and control["value"] == "muito"
    assert stored(deps) == json.loads(DEFINITION)


# --- a pontuação no editor e no teste -----------------------------------------------------------


def test_the_editor_shows_each_score_the_priority_and_the_persona_of_the_example_answers(
    client: TestClient,
) -> None:
    panel = shown(client.get(DRAFT), "pontuacao")

    assert "respostas de exemplo" in panel
    for expected in ("porte", "50", "nivel_porte", "pequeno", "rotulo_porte", "Pequeno"):
        assert expected in panel, expected
    assert "alcance" in panel and "25" in panel  # 2 canais x 12,5, meio para cima
    assert "Prioridade" in panel and "P2" in panel and "regra 2" in panel
    assert "Persona" in panel and "Em crescimento" in panel
    assert "só no admin e no webhook" in panel  # nunca para a lead


def test_the_editor_without_scoring_says_so(client: TestClient) -> None:
    plain = definition()
    plain["slug"] = "sem-pontuacao"
    plain["consent"]["version"] = "sem-pontuacao-v1"
    assert import_form(client, json.dumps(plain)).status_code == 303

    page = client.get("/admin/forms/sem-pontuacao/draft")

    assert 'id="pontuacao"' not in page.text
    assert "Este formulário não tem pontuação" in text(page.text)  # o editor convida a começar


def test_the_test_page_calculates_the_score_with_the_example_answers_or_with_others(
    client: TestClient,
) -> None:
    page = client.get(TESTS)
    assert page.status_code == 200
    assert "Calcular a pontuação" in text(page.text)

    other = {
        **DEMO_ANSWERS,
        "tamanho_equipe": "mais_de_15",
        "principal_desafio": "vendas",
        "canais": ["a", "b", "c", "d"],
    }
    example = post(client, f"{DRAFT}/score")
    mine = post(client, f"{DRAFT}/score", answers=json.dumps(other))

    assert example.status_code == 200 and mine.status_code == 200
    first, second = shown(example, "pontuacao-calculada"), shown(mine, "pontuacao-calculada")
    assert "Com as respostas de exemplo" in first and "P2" in first and "Em crescimento" in first
    assert "Com as suas respostas" in second
    assert "100" in second and "grande" in second and "Grande" in second
    assert "50" in second  # 4 canais x 12,5
    assert "P1" in second and "regra 1" in second
    assert "Comercial" in second and "regra 1" in second
    assert json.loads(textareas(mine.text)["answers"]) == other


def test_a_score_that_divides_by_zero_with_the_given_answers_says_which_formula(
    client: TestClient,
) -> None:
    def divide(scoring: dict[str, Any]) -> None:
        scoring["scores"][1]["formula"] = "10 / (pontos(tamanho_equipe) - 25)"

    assert save(client, edit_scoring(divide)).status_code == 303  # com o exemplo (50), não zera

    response = post(
        client,
        f"{DRAFT}/score",
        answers=json.dumps({**DEMO_ANSWERS, "tamanho_equipe": "so_eu", "canais": ["a"]}),
    )

    assert response.status_code == 422
    assert "scoring.scores.1.formula: com estas respostas, divisão por zero" in problems_shown(
        response
    )


@pytest.mark.parametrize(
    ("form", "problem"),
    [
        (
            {"answers": json.dumps({**DEMO_ANSWERS, "email": "sem-arroba"})},
            "answers.email: e-mail inválido",
        ),
        ({"answers": "{ sem aspas }"}, "answers, linha 1: JSON inválido"),
    ],
)
def test_answers_that_do_not_validate_show_the_problem_when_scoring(
    client: TestClient, form: dict[str, str], problem: str
) -> None:
    response = post(client, f"{DRAFT}/score", **form)

    assert response.status_code == 422
    assert problem in problems_shown(response)
    assert "sem-arroba" not in problems_shown(response)


def test_scoring_changes_nothing_in_the_database(client: TestClient, deps: Deps) -> None:
    before = stored(deps)

    post(client, f"{DRAFT}/score", answers=json.dumps({**DEMO_ANSWERS, "canais": ["a"]}))

    assert stored(deps) == before
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(FormDraft)) == 0


def test_the_score_route_needs_the_session_and_the_csrf_token(client: TestClient) -> None:
    anonymous = TestClient(client.app, follow_redirects=False)

    refused = [
        anonymous.post(f"{DRAFT}/score", data={"csrf_token": "x"}),
        client.post(f"{DRAFT}/score", data={"csrf_token": "errado"}),
    ]

    assert [response.status_code for response in refused] == [303, 403]


# --- o que a publicação barra -------------------------------------------------------------------


def cycle(scoring: dict[str, Any]) -> None:
    scoring["scores"][0]["formula"] = "alcance + 1"
    scoring["scores"][1]["formula"] = "porte + 1"


def missing_reference(scoring: dict[str, Any]) -> None:
    scoring["scores"][1]["formula"] = "contagem(canais) * fator_que_nao_existe"


def no_priority_for_the_example(scoring: dict[str, Any]) -> None:
    scoring["priority"] = [{"when": "porte >= 100", "value": "P1"}]


def invalid_priority(scoring: dict[str, Any]) -> None:
    scoring["priority"][0]["value"] = "P9"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (cycle, "referência circular: porte → alcance → porte"),
        (missing_reference, "fator_que_nao_existe: não é uma nota nem uma pergunta de escolha"),
        (
            no_priority_for_the_example,
            "nenhuma regra de prioridade vale para as respostas de exemplo",
        ),
        (invalid_priority, "use P1, P2 ou P3"),
    ],
    ids=["cycle", "missing-reference", "no-priority", "invalid-priority"],
)
def test_a_draft_with_a_broken_scoring_is_not_saved_and_the_problem_shows_where_it_is(
    client: TestClient, deps: Deps, change: Callable[[dict[str, Any]], None], problem: str
) -> None:
    response = save(client, edit_scoring(change))

    assert response.status_code == 422
    assert problem in problems_shown(response)
    assert re.search(r'href="#f-scoring-[^"]+"', response.text)  # o link leva até o campo
    assert stored(deps) == json.loads(DEFINITION)


@pytest.mark.parametrize(
    "change", [cycle, missing_reference, no_priority_for_the_example, invalid_priority]
)
def test_a_definition_with_a_broken_scoring_is_not_imported(
    client: TestClient, change: Callable[[dict[str, Any]], None]
) -> None:
    broken = scored_definition()
    change(broken["scoring"])
    broken["slug"] = "pontuacao-quebrada"
    broken["consent"]["version"] = "pontuacao-quebrada-v1"

    response = import_form(client, json.dumps(broken))

    assert response.status_code == 422
    assert client.get("/p/pontuacao-quebrada").status_code == 404


def test_the_publication_is_refused_when_the_saved_draft_no_longer_compiles(
    client: TestClient, deps: Deps
) -> None:
    """Um rascunho guardado antes de uma regra mais rígida, ou mexido por fora do app: a
    publicação confere de novo e não publica o que não compila."""
    assert (
        save(client, edit_scoring(lambda scoring: scoring.update(priority=[]))).status_code == 303
    )
    broken = stored(deps)
    cycle(broken["scoring"])
    with deps.sessions.begin() as session:
        session.execute(sa.update(FormDraft).values(definition=broken))
    page = client.get(PUBLISH)
    assert "referência circular" in text(page.text)

    response = post(
        client,
        PUBLISH,
        note="Tenta publicar",
        expected=hidden(page.text, "expected"),
    )

    assert response.status_code == 422
    assert "Corrija o rascunho: ele não compila" in text(response.text)
    assert deps.forms.current(SLUG).version_number == 1


# --- a leitura e a comparação -------------------------------------------------------------------


def test_the_form_page_reads_the_scoring_of_the_published_version(client: TestClient) -> None:
    panel = shown(client.get(f"/admin/forms/{SLUG}"), "pontuacao")

    assert "porte" in panel and "pontos(tamanho_equipe)" in panel
    assert "arredondar_meio_para_cima(contagem(canais) * 12.5)" in panel
    assert "nivel_porte" in panel and "Grande" in panel and "a partir de 90" in panel
    assert "porte >= 100" in panel and "P1" in panel
    assert 'principal_desafio == "vendas"' in panel and "Comercial" in panel
    assert "calculada no código" not in panel


def test_the_publication_compares_the_scoring_section_of_the_two_versions(
    client: TestClient,
) -> None:
    def raise_the_bar(scoring: dict[str, Any]) -> None:
        scoring["priority"][1]["when"] = "porte >= 75"

    assert save(client, edit_scoring(raise_the_bar)).status_code == 303

    changes = text(section_of(client.get(PUBLISH), "mudancas"))

    assert "Pontuação" in changes and "porte >= 75" in changes


# --- do envio ao webhook ------------------------------------------------------------------------


def send(client: TestClient, **changes: Any) -> str:
    """Envia a página pública (sem JavaScript), como a lead, e devolve o caminho do pedido."""
    controls = {"_privacy": "1", "_consent_version": CONSENT_VERSION, "_idem": uuid.uuid4().hex}
    answers = {**DEMO_ANSWERS, "canais": ["a", "c", "d"], **changes}
    response = client.post(f"/p/{SLUG}", data={**answers, **controls})
    assert response.status_code == 303, response.text
    return response.headers["location"]


def test_the_webhook_carries_the_scores_the_priority_and_the_persona_in_the_same_fields(
    client: TestClient, deps: Deps, receiver: Receiver
) -> None:
    secret = create(client, f"/admin/destinations/forms/{SLUG}", receiver.url("/pontuacao"))

    page = send(client, tamanho_equipe="6_a_15", principal_desafio="vendas")
    (delivery_id,) = finish(deps, job_id_of(deps, page))
    assert pipeline.deliver(deps, delivery_id) == "succeeded"

    (request,) = receiver.requests
    assert request.signed_with(secret)
    event = request.json()
    assert event["event"] == "plano.concluido"
    assert event["scores"] == {
        "porte": 75,
        "nivel_porte": "medio",
        "rotulo_porte": "Médio",
        "alcance": 38,  # 3 canais x 12,5 = 37,5, meio para cima
    }
    assert (event["priority"], event["persona"]) == ("P2", "Comercial")
    assert event["form"] == {"slug": SLUG, "version": "pontuacao-demo-v1"}


def test_the_priority_and_the_persona_never_reach_the_lead_but_the_pdf_reads_the_scores(
    client: TestClient, deps: Deps
) -> None:
    page = send(client, tamanho_equipe="mais_de_15", principal_desafio="vendas")
    finish(deps, job_id_of(deps, page))

    shown_to_the_lead = text(client.get(page).text)
    pdf = text_of(client.get(f"{page}/pdf").content)

    for internal in ("P1", "Comercial", "priority", "persona"):
        assert internal not in shown_to_the_lead, internal
    assert "Porte 100 nível Grande" in pdf  # o template do PDF leu as notas do envio


def test_the_admin_request_page_shows_the_scores_of_a_declared_scoring(
    client: TestClient, deps: Deps
) -> None:
    page = send(client, tamanho_equipe="so_eu")
    finish(deps, job_id_of(deps, page))

    detail = text(client.get(f"/admin/jobs/{page.rsplit('/', 1)[-1]}").text)

    assert "P3" in detail and "Iniciante" in detail
    assert (
        "Porte 25" in detail
        and "Nivel porte pequeno" in detail
        and "Rotulo porte Pequeno" in detail
    )


class CountingLlm(FakePlanGenerator):
    """O LLM falso que conta as chamadas."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, spec: FormSpec, answers: BaseModel) -> LlmResult:
        self.calls += 1
        return super().generate(spec, answers)


def test_a_draft_test_whose_scoring_fails_with_the_answers_stops_before_the_llm(
    client: TestClient, deps: Deps, worker: TestTrigger
) -> None:
    llm = deps.llm = CountingLlm()

    def divide(scoring: dict[str, Any]) -> None:
        scoring["scores"][1]["formula"] = "10 / (pontos(tamanho_equipe) - 25)"

    assert save(client, edit_scoring(divide)).status_code == 303
    zero = {**DEMO_ANSWERS, "tamanho_equipe": "so_eu", "canais": ["a"]}
    started = post(client, TESTS, answers=json.dumps(zero))
    assert started.status_code == 303
    worker.run()

    page = text(client.get(started.headers["location"]).text)

    assert "a pontuação não calculou com estas respostas" in page
    assert "scoring.scores.1.formula" in page and "divisão por zero" in page
    assert llm.calls == 0


def test_the_draft_test_pdf_gets_the_scores_of_the_test_answers(
    client: TestClient, deps: Deps, worker: TestTrigger
) -> None:
    other = {**DEMO_ANSWERS, "tamanho_equipe": "6_a_15", "canais": ["a"]}
    started = post(client, TESTS, answers=json.dumps(other))
    assert started.status_code == 303
    worker.run()

    result = client.get(started.headers["location"] + "/pdf")

    assert result.status_code == 200
    assert "Porte 75 nível Médio" in text_of(result.content)


def test_form_fields_of_the_editor_still_carry_the_points_after_adding_an_option(
    client: TestClient,
) -> None:
    """Incluir uma opção não grava nada, mas não pode perder os pontos que já estão na página."""
    fields = editor(client, DRAFT)
    fields["op"] = "add-option:1:0"

    response = post(client, f"{DRAFT}/edit", **fields)

    assert response.status_code == 200
    kept = form_fields(response.text, EDITOR_FORM)
    assert kept["steps.1.questions.0.options.1.points"] == "50"
    assert kept["steps.1.questions.0.options.4.points"] == ""  # a nova, sem pontos
