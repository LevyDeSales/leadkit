"""Rascunho, teste e publicação de um formulário do banco, pela costura HTTP (TestClient), com o LLM
falso: a operadora edita o rascunho, vê o prompt montado, testa com o LLM, publica a versão
seguinte com uma nota e volta a uma versão anterior pelo histórico."""

from __future__ import annotations

import html
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from conftest import record_webhook
from fastapi.testclient import TestClient
from test_admin import ORIGIN, FakeClock, csrf_token, login, section
from test_admin_destinations import ADMIN_ENV
from test_admin_form_pages import rows_of
from test_admin_forms import DEFINITION, SLUG, import_form, section_of
from test_intake import count
from test_public import form_fields, tags, text
from test_weasyprint_pdf import squash, text_of

from app import retention
from app.adapters.llm import LlmResult
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.api.main import create_app
from app.deps import Deps
from app.errors import FailureCode, RetryableError
from app.forms import draft_tests
from app.models import Artifact, AuditLog, Delivery, DraftTest, Job, Plan, Submission
from app.settings import Settings

DRAFT = f"/admin/forms/{SLUG}/draft"
NEW_PROMPT_LINE = "Considere também o momento do mercado contábil."


class TestTrigger:
    """O disparo da task `draft_test` no Hatchet, trocado nos testes: guarda o id e roda o mesmo
    passo da task (`draft_tests.run`) quando o teste pedir, com o relógio do teste."""

    __test__ = False  # não é uma classe de testes do pytest

    def __init__(self, deps: Deps, clock: FakeClock) -> None:
        self.deps = deps
        self.clock = clock
        self.queued: list[uuid.UUID] = []
        self.accepts = True  # False: o engine fora do ar

    async def __call__(self, test_id: uuid.UUID) -> str | None:
        if not self.accepts:
            return None
        self.queued.append(test_id)
        return "run-fake"

    def run(self) -> None:
        queued, self.queued = self.queued, []
        for test_id in queued:
            draft_tests.run(self.deps, test_id, now=self.now)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def worker(deps: Deps, clock: FakeClock) -> TestTrigger:
    return TestTrigger(deps, clock)


@pytest.fixture
def client(deps: Deps, clock: FakeClock, worker: TestTrigger) -> TestClient:
    """A operadora no admin, com o formulário do banco já importado (versão 1)."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, test_trigger=worker, clock=clock),
        follow_redirects=False,
    )
    assert login(current).status_code == 303
    assert import_form(current, DEFINITION).status_code == 303
    return current


def textareas(page: str) -> dict[str, str]:
    """Os campos de texto de uma página, pelo `name`, como o navegador os manda de volta: sem a
    quebra de linha logo depois da abertura da tag, que o HTML descarta."""
    found = re.findall(r'<textarea[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>', page, re.DOTALL)
    return {name: html.unescape(value).removeprefix("\n") for name, value in found}


def hidden(page: str, name: str) -> str:
    found = re.search(rf'<input type="hidden" name="{name}" value="([^"]*)"', page)
    assert found is not None, name
    return found.group(1)


EDITOR_FORM = "editor-form"  # o id do formulário do editor do rascunho
Fields = dict[str, Any]  # os campos de um formulário, como `form_fields` os lê


def editor(client: TestClient, draft: str = DRAFT) -> Fields:
    """O formulário do rascunho como o navegador o mandaria sem mudança nenhuma: o editor das
    etapas e das perguntas, os textos, o JSON do resto e a impressão digital da página."""
    page = client.get(draft)
    assert page.status_code == 200, page.text
    return form_fields(page.text, EDITOR_FORM)


def post(client: TestClient, path: str, **form: str | list[str]) -> httpx.Response:
    return client.post(path, data={"csrf_token": csrf_token(client), **form}, headers=ORIGIN)


def save(
    client: TestClient, change: Callable[[Fields], Any] | None = None, draft: str = DRAFT
) -> httpx.Response:
    """Salva o rascunho pelo formulário do editor, com a mudança aplicada aos campos."""
    fields = editor(client, draft)
    if change is not None:
        change(fields)
    return post(client, draft, **fields)


def add_prompt_line(fields: Fields) -> None:
    fields["prompt"] += NEW_PROMPT_LINE + "\n"


def test_the_first_save_creates_the_draft_from_the_published_version_without_touching_it(
    client: TestClient,
) -> None:
    fields = editor(client)
    assert fields["instructions"] == json.loads(DEFINITION)["instructions"]
    assert fields["prompt"] == json.loads(DEFINITION)["prompt"]
    assert fields["steps.0.questions.0.example"] == "Contadora Exemplo"  # resposta de exemplo
    rest = json.loads(fields["definition"])
    assert "prompt" not in rest and "answers" not in rest["examples"]
    # As etapas, a identidade e o consentimento têm o editor deles; o endereço não muda, e a
    # versão do texto do consentimento sobe sozinha.
    for key in ("slug", "title", "intro", "minutes", "steps", "identity", "consent"):
        assert key not in rest, key
    # O PDF e a prévia da página de resultado têm o editor do PDF.
    assert "pdf" not in rest and "preview" not in rest

    response = save(client, add_prompt_line)

    assert response.status_code == 303 and response.headers["location"] == DRAFT
    assert NEW_PROMPT_LINE in editor(client)["prompt"]
    assert "Rascunho pendente" in text(rows_of(client.get("/admin/forms"))[SLUG])
    assert "Rascunho pendente" in text(section_of(client.get(f"/admin/forms/{SLUG}"), "rascunho"))
    # O que está no ar não muda: a leitura e a página pública seguem a versão 1.
    assert NEW_PROMPT_LINE not in client.get(f"/admin/forms/{SLUG}").text
    assert client.get(f"/p/{SLUG}").status_code == 200


def problems_shown(response: httpx.Response) -> str:
    """Os problemas que a página mostra, como a operadora os lê."""
    found = re.search(r'<div[^>]*id="problemas".*?</ul>', response.text, re.DOTALL)
    assert found is not None, "a página não mostrou os problemas"
    return text(found.group(0))


def invalid_fields(response: httpx.Response) -> list[str]:
    return re.findall(r'<textarea[^>]*name="([^"]+)"[^>]*aria-invalid="true"', response.text)


def unknown_answer_in_prompt(fields: Fields) -> None:
    fields["prompt"] = fields["prompt"].replace("answers.tamanho_equipe", "answers.equipe")


def broken_json(fields: Fields) -> None:
    fields["definition"] = '{\n  "format": 1\n  "plan": "Sem vírgula"\n}'


def moved_slug(fields: Fields) -> None:
    rest = json.loads(fields["definition"])
    fields["definition"] = json.dumps({**rest, "slug": "outro-endereco"})


@pytest.mark.parametrize(
    ("change", "problem", "field"),
    [
        (
            unknown_answer_in_prompt,
            "prompt, linha 3: answers.equipe não é uma pergunta do formulário",
            "prompt",
        ),
        (broken_json, "definição, linha 3: JSON inválido: falta uma vírgula", "definition"),
        (moved_slug, "slug: o endereço do formulário não muda", "definition"),
    ],
    ids=["template", "json", "slug"],
)
def test_a_draft_that_does_not_compile_is_not_saved_and_each_problem_shows_where_it_is(
    client: TestClient, change: Callable[[Fields], None], problem: str, field: str
) -> None:
    fields = editor(client)
    change(fields)

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    assert problem in problems_shown(response)
    assert invalid_fields(response) == [field]
    assert textareas(response.text)[field] == fields[field]  # o texto volta para corrigir
    assert "Rascunho pendente" not in text(rows_of(client.get("/admin/forms"))[SLUG])
    assert editor(client)["prompt"] == json.loads(DEFINITION)["prompt"]


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        (
            "definition",
            '"proximo_passo": NaN',
            "examples.plan.proximo_passo: NaN e Infinity não são números em JSON",
        ),
        ("prompt", "\u0000", "prompt: o texto tem um caractere que não vale"),
    ],
    ids=["nan", "null-character"],
)
def test_what_the_database_cannot_keep_is_refused_before_it_gets_there(
    client: TestClient, field: str, value: str, problem: str
) -> None:
    fields = editor(client)
    if field == "definition":
        fields["definition"] = re.sub(
            r'"proximo_passo": "[^"]*"', value, fields["definition"], count=1
        )
    else:
        fields[field] += value

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    assert problem in problems_shown(response)


def test_a_save_from_a_page_opened_before_the_last_change_changes_nothing(
    client: TestClient,
) -> None:
    stale = editor(client)  # outra aba, aberta antes
    assert save(client, add_prompt_line).status_code == 303
    stale["prompt"] += "Outra linha, de outra aba.\n"

    response = post(client, DRAFT, **stale)

    assert response.status_code == 409
    assert "O rascunho mudou desde que você abriu esta página" in text(response.text)
    assert textareas(response.text)["prompt"] == stale["prompt"]  # o texto dela não se perde
    current = editor(client)["prompt"]
    assert NEW_PROMPT_LINE in current and "Outra linha" not in current


def test_saving_the_published_content_back_leaves_no_pending_draft(client: TestClient) -> None:
    save(client, add_prompt_line)

    def undo(fields: Fields) -> None:
        fields["prompt"] = fields["prompt"].replace(NEW_PROMPT_LINE + "\n", "")

    response = save(client, undo)

    assert response.status_code == 303
    assert "igual à versão publicada" in text(client.get(DRAFT).text)
    assert "Rascunho pendente" not in text(rows_of(client.get("/admin/forms"))[SLUG])


def test_each_save_is_audited_in_the_operator_name_without_the_content(
    client: TestClient, deps: Deps
) -> None:
    save(client, add_prompt_line)

    with deps.sessions() as session:
        (entry,) = session.scalars(sa.select(AuditLog).where(AuditLog.action == "form.draft.save"))
    assert (entry.actor, entry.target_type) == ("operadora", "form")
    assert set(entry.details) == {"form_slug", "fingerprint"}
    assert entry.details["form_slug"] == SLUG
    assert NEW_PROMPT_LINE not in json.dumps(entry.details)


TESTS = f"{DRAFT}/tests"
OTHER_ANSWERS = {
    "nome": "Outra Contadora",
    "email": "outra@example.test",
    "escritorio": "Escritório Fictício",
    "tamanho_equipe": "mais_de_15",
    "principal_desafio": "gestao",
    "objetivo_90_dias": "Montar a rotina de fechamento mensal com a equipe nova.",
}


def dated_prompt(fields: Fields) -> None:
    fields["prompt"] = "Hoje é {{ today|long_date }}.\n" + fields["prompt"]


def assembled(response: httpx.Response) -> str:
    return text(section_of(response, "prompt-montado"))


def test_the_test_page_shows_the_prompt_assembled_with_the_example_answers(
    client: TestClient,
) -> None:
    page = client.get(TESTS, follow_redirects=True)

    assert page.status_code == 200
    shown = assembled(page)
    assert "Com as respostas de exemplo" in shown
    assert "Você é consultora de gestão de escritórios contábeis" in shown  # as instruções
    assert "Nome do escritório: Escritório Exemplo" in shown
    assert "Tamanho da equipe: 2_a_5" in shown
    assert (
        json.loads(textareas(page.text)["answers"]) == json.loads(DEFINITION)["examples"]["answers"]
    )


def test_the_prompt_is_assembled_with_answers_the_operator_fills_in_and_a_simulated_date(
    client: TestClient,
) -> None:
    save(client, dated_prompt)

    response = post(client, f"{DRAFT}/prompt", answers=json.dumps(OTHER_ANSWERS), date="2027-03-01")

    assert response.status_code == 200
    shown = assembled(response)
    assert "Hoje é 1º de março de 2027." in shown  # a data simulada
    assert "Tamanho da equipe: mais_de_15" in shown and "Escritório Fictício" in shown
    assert "Com as suas respostas" in shown
    assert json.loads(textareas(response.text)["answers"]) == OTHER_ANSWERS
    assert 'value="2027-03-01"' in response.text


@pytest.mark.parametrize(
    ("form", "problem"),
    [
        (
            {"answers": json.dumps({**OTHER_ANSWERS, "email": "sem-arroba"})},
            "answers.email: e-mail inválido",
        ),
        ({"answers": "{ sem aspas }"}, "answers, linha 1: JSON inválido"),
        ({"date": "2027-02-30"}, "date: use uma data válida"),
        (
            # O banco não guarda o caractere nulo num JSONB: sem a conferência, o teste quebraria
            # ao gravar, com as respostas na mensagem do erro.
            {"answers": json.dumps({**OTHER_ANSWERS, "nome": "Outra\u0000Contadora"})},
            "answers.nome: o texto tem um caractere que não vale",
        ),
    ],
    ids=["answer", "json", "date", "null-character"],
)
def test_answers_or_a_date_that_do_not_validate_show_the_problem(
    client: TestClient, form: dict[str, str], problem: str
) -> None:
    response = post(client, f"{DRAFT}/prompt", **form)

    assert response.status_code == 422
    assert problem in problems_shown(response)
    assert "sem-arroba" not in problems_shown(response)  # nada do que veio volta na mensagem


def test_the_editor_lists_the_variables_and_the_answers_the_prompt_cites(
    client: TestClient,
) -> None:
    variables = text(section_of(client.get(DRAFT), "variaveis"))

    for name in ("answers.nome", "answers.tamanho_equipe", "labels.tamanho_equipe", "today"):
        assert name in variables, name
    cited = re.search(r"O prompt cita: (.*?) \.", variables)
    assert cited is not None
    assert cited.group(1) == "escritorio, tamanho_equipe, principal_desafio, objetivo_90_dias"


# --- o teste do rascunho ------------------------------------------------------------------------

EXAMPLE_TITLE = "Organizar atividades do projeto"  # o plano de exemplo da definição
# O uso como o adaptador da OpenAI o grava (`app.adapters.openai_llm`), com uma rodada de reparo.
OPENAI_USAGE = {
    "model": "gpt-teste",
    "repairs": 1,
    "repair_reasons": ["acoes:too_short"],
    "fallback": False,
    "input_tokens": 3120,
    "output_tokens": 980,
    "total_tokens": 4100,
}


class ScriptedLlm:
    """O LLM do teste: devolve o plano de exemplo com o uso que a OpenAI devolveria, ou a falha
    que o adaptador levantaria. Guarda os prompts que recebeu."""

    def __init__(self, usage: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.usage = usage if usage is not None else OPENAI_USAGE
        self.error = error
        self.prompts: list[str] = []

    def generate(self, spec: Any, answers: Any) -> LlmResult:
        self.prompts.append(spec.render_prompt(answers))
        if self.error is not None:
            raise self.error
        plan = spec.plan_model.model_validate(spec.example_plan)
        return LlmResult(document=plan.model_dump(mode="json"), model="gpt-teste", usage=self.usage)


def start_test(client: TestClient, **form: str) -> httpx.Response:
    return post(client, TESTS, **form)


def result_path(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert re.fullmatch(rf"{TESTS}/[0-9a-f-]{{36}}", location), location
    return location


def test_a_draft_test_runs_in_the_worker_and_shows_the_plan_the_pdf_the_time_and_the_tokens(
    client: TestClient, deps: Deps, worker: TestTrigger
) -> None:
    deps.llm = llm = ScriptedLlm()
    deps.pdf = WeasyPrintRenderer()
    save(client, dated_prompt)

    result = result_path(start_test(client, date="2027-03-01"))

    (queued,) = worker.queued  # a task recebe só o id do teste
    assert result.endswith(str(queued))
    waiting = client.get(result)
    assert "Na fila" in text(section_of(waiting, "resultado"))
    assert tags(waiting.text, "div", id="estado", **{"hx-trigger": "every 2s"})
    assert '<meta http-equiv="refresh" content="5">' in waiting.text  # sem JavaScript
    assert client.get(f"{result}/status").status_code == 200
    worker.run()
    done = client.get(f"{result}/status")
    assert done.status_code == 286 and done.headers["hx-refresh"] == "true"
    page = client.get(result)
    shown = text(section_of(page, "resultado"))
    assert "Concluído" in shown and EXAMPLE_TITLE in text(section_of(page, "plano"))
    for fact in ("3.120 de entrada", "980 de saída", "4.100 no total", "1 rodada de reparo"):
        assert fact in shown, fact
    assert "acoes:too_short" in shown and "gpt-teste" in shown
    for stage in ("LLM", "PDF"):
        assert re.search(rf"{stage} em (?:\d+ ms|\d+,\d s)\b", shown), shown
    assert "Hoje é 1º de março de 2027." in llm.prompts[0]  # a data simulada chega ao LLM
    pdf = client.get(f"{result}/pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert squash(EXAMPLE_TITLE) in squash(text_of(pdf.content))
    assert "1º de março de 2027" in text_of(pdf.content)  # o PDF também sai na data simulada
    assert re.search(r"PDF de \d+ páginas?", shown)


def test_a_draft_test_creates_no_submission_request_plan_file_or_delivery(
    client: TestClient, deps: Deps, worker: TestTrigger
) -> None:
    webhook = record_webhook(deps)  # há destino: um pedido de verdade seria entregue

    start_test(client)
    worker.run()

    for model in (Submission, Job, Plan, Artifact, Delivery):
        assert count(deps, model) == 0, model.__name__
    assert webhook.calls == []


def test_one_test_runs_at_a_time_and_the_next_waits_a_pause(
    client: TestClient, worker: TestTrigger, clock: FakeClock
) -> None:
    result_path(start_test(client))

    busy = start_test(client)
    assert busy.status_code == 409
    assert "Já há um teste deste formulário em andamento" in text(busy.text)
    worker.run()
    pause = start_test(client)
    assert pause.status_code == 429 and pause.headers["retry-after"] == "30"
    assert "Espere 30 s para testar de novo" in text(pause.text)
    clock.advance(31)
    result_path(start_test(client))
    assert len(worker.queued) == 1  # só o último saiu para o worker


def test_a_failed_test_shows_why_without_the_content(
    client: TestClient, deps: Deps, worker: TestTrigger
) -> None:
    deps.llm = ScriptedLlm(
        error=RetryableError(
            FailureCode.PLAN_INVALID, "plano inválido depois do reparo: 2 erro(s) (too_short)"
        )
    )
    result = result_path(start_test(client))

    worker.run()

    shown = text(section_of(client.get(result), "resultado"))
    assert "Falhou: plano inválido" in shown
    assert "plano inválido depois do reparo: 2 erro(s) (too_short)" in shown
    assert client.get(f"{result}/pdf").status_code == 404


def test_a_test_the_engine_did_not_take_says_so_and_blocks_nothing(
    client: TestClient, worker: TestTrigger
) -> None:
    worker.accepts = False

    result = result_path(start_test(client))

    assert "o worker não recebeu o teste" in text(section_of(client.get(result), "resultado"))
    worker.accepts = True
    result_path(start_test(client))  # sem pausa: o LLM não foi chamado


def test_a_test_that_never_ends_is_interrupted_and_frees_the_next_one(
    client: TestClient, worker: TestTrigger, clock: FakeClock
) -> None:
    result = result_path(start_test(client))
    worker.queued.clear()  # o worker caiu: a task nunca roda

    clock.advance(7 * 60)

    assert "Interrompido" in text(section_of(client.get(result), "resultado"))
    assert client.get(f"{result}/status").status_code == 286
    result_path(start_test(client))
    # Depois que outro teste tomou a vez, o interrompido continua dizendo o mesmo.
    page = client.get(result)
    assert "Interrompido" in text(section_of(page, "resultado"))
    assert "Falhou" not in text(page.text)


def test_the_worker_runs_the_test_at_the_moment_of_the_click(
    client: TestClient, deps: Deps, worker: TestTrigger, clock: FakeClock
) -> None:
    deps.llm = llm = ScriptedLlm()
    save(client, dated_prompt)
    result = result_path(start_test(client))  # sem data simulada: hoje, 15 de janeiro de 2027

    clock.advance(20 * 3600)  # o worker só pega o teste no dia seguinte
    worker.run()

    (sent,) = llm.prompts
    assert "Hoje é 15 de janeiro de 2027." in sent
    assert login(client).status_code == 303  # a sessão caiu nessas 20 h
    shown = text(section_of(client.get(result), "enviado"))
    assert "Hoje é 15 de janeiro de 2027." in shown  # a página mostra o que foi


def test_a_task_that_fails_for_good_ends_the_test_at_once(client: TestClient, deps: Deps) -> None:
    from app import workflows

    result = result_path(start_test(client))
    test_id = workflows.DraftTestInput.for_test(uuid.UUID(result.rsplit("/", 1)[1]))

    output = workflows.draft_test_failed.mock_run(input=test_id)

    assert output.model_dump() == {"code": "internal", "failed_task": None}
    shown = text(section_of(client.get(result), "resultado"))
    assert "Falhou: erro interno" in shown and "a task do teste falhou (internal)" in shown
    result_path(start_test(client))  # libera a vez na hora, sem esperar o prazo


def test_only_the_latest_tests_stay_and_retention_removes_them_after_a_few_days(
    client: TestClient, deps: Deps, worker: TestTrigger, clock: FakeClock
) -> None:
    for _ in range(6):
        result_path(start_test(client))
        worker.run()
        clock.advance(31)

    recent = client.get(TESTS, follow_redirects=True).text
    assert len(re.findall(rf'href="{TESTS}/[0-9a-f-]{{36}}"', section(recent, "recentes"))) == 5
    report = retention.purge_expired(deps, now=worker.now() + timedelta(days=4))
    assert report.draft_tests_deleted == 5
    assert "Nenhum teste" in text(
        section(client.get(TESTS, follow_redirects=True).text, "recentes")
    )


def test_each_test_is_audited_without_the_answers(
    client: TestClient, deps: Deps, worker: TestTrigger
) -> None:
    start_test(client, answers=json.dumps(OTHER_ANSWERS), date="2027-03-01")

    with deps.sessions() as session:
        (entry,) = session.scalars(sa.select(AuditLog).where(AuditLog.action == "form.draft.test"))
    assert entry.actor == "operadora"
    assert set(entry.details) == {"form_slug", "test", "fingerprint", "simulated_date"}
    assert entry.details["test"] == str(worker.queued[0])
    assert entry.details["simulated_date"] == "2027-03-01"
    assert "Outra Contadora" not in json.dumps(entry.details)


def test_the_draft_test_task_takes_and_returns_only_ids(client: TestClient, deps: Deps) -> None:
    from app import workflows

    result_path(start_test(client))
    with deps.sessions() as session:
        (test_id,) = session.scalars(sa.select(DraftTest.id))

    output = workflows.run_draft_test.mock_run(input=workflows.DraftTestInput.for_test(test_id))

    assert output.model_dump() == {"status": "succeeded"}
    assert set(workflows.DraftTestInput.model_fields) == {"test_id", "run_key"}


# --- a publicação -------------------------------------------------------------------------------

PUBLISH = f"{DRAFT}/publish"
NOTE = "Prompt pede o momento do mercado."


def review(client: TestClient) -> httpx.Response:
    page = client.get(PUBLISH)
    assert page.status_code == 200, page.text
    return page


def publish(
    client: TestClient, note: str = NOTE, *, confirm_contact: bool = False, expected: str = ""
) -> httpx.Response:
    """Publica pela página da publicação, como o navegador, com a nota e a confirmação."""
    form = {"note": note, "expected": expected or hidden(review(client).text, "expected")}
    if confirm_contact:
        form["confirm_contact"] = "on"
    return post(client, PUBLISH, **form)


def tested(client: TestClient, worker: TestTrigger, clock: FakeClock) -> None:
    """Um teste do rascunho com sucesso, como agora."""
    result_path(start_test(client))
    worker.run()
    clock.advance(31)


def reasons_shown(response: httpx.Response) -> str:
    return text(section_of(response, "falta"))


def test_publishing_with_a_note_creates_the_next_version_which_new_submissions_use(
    client: TestClient, deps: Deps, worker: TestTrigger, clock: FakeClock
) -> None:
    save(client, add_prompt_line)
    tested(client, worker, clock)
    shown = text(review(client).text)
    assert "Publicar a versão 2" in shown and "Prompt" in shown
    assert "teste com sucesso" in shown

    response = publish(client)

    assert response.status_code == 303 and response.headers["location"] == f"/admin/forms/{SLUG}"
    page = client.get(response.headers["location"])
    assert "Versão 2 publicada" in text(page.text)
    version = text(section_of(page, "versao"))
    assert "Versão 2" in version and f"Nota {NOTE}" in version and "Publicado operadora" in version
    assert NEW_PROMPT_LINE in text(section_of(page, "prompt"))  # a leitura já é da versão 2
    assert "Rascunho pendente" not in text(rows_of(client.get("/admin/forms"))[SLUG])
    assert "Nenhuma mudança pendente" in text(section_of(page, "rascunho"))
    with deps.sessions() as session:
        (entry,) = session.scalars(sa.select(AuditLog).where(AuditLog.action == "form.publish"))
    assert entry.details["form_slug"] == SLUG and entry.details["version"] == 2
    assert set(entry.details) == {"form_slug", "version", "fingerprint", "tested_at"}
    assert deps.forms.current(SLUG).prompt_version == "demo-definicao-v2"  # sem reiniciar


def test_publishing_needs_a_note(client: TestClient, worker: TestTrigger, clock: FakeClock) -> None:
    save(client, add_prompt_line)
    tested(client, worker, clock)

    response = publish(client, note="   ")

    assert response.status_code == 422
    assert "Escreva uma nota do que mudou" in reasons_shown(response)
    assert "Versão 1" in text(section_of(client.get(f"/admin/forms/{SLUG}"), "versao"))


def test_a_change_to_what_goes_to_the_llm_needs_a_successful_test_after_it(
    client: TestClient, deps: Deps, worker: TestTrigger, clock: FakeClock
) -> None:
    save(client, add_prompt_line)

    untested = publish(client)
    assert untested.status_code == 422
    assert "Teste o rascunho com o LLM antes de publicar: mudou o prompt" in reasons_shown(untested)
    deps.llm = ScriptedLlm(error=RetryableError(FailureCode.LLM_UNAVAILABLE, "HTTP 503"))
    tested(client, worker, clock)
    assert publish(client).status_code == 422  # um teste que falhou não vale
    deps.llm = ScriptedLlm()
    tested(client, worker, clock)

    def change_instructions(fields: Fields) -> None:
        fields["instructions"] += "Use frases curtas.\n"

    save(client, change_instructions)  # mudou de novo depois do teste
    again = publish(client)
    assert again.status_code == 422
    assert "mudaram o prompt e as instruções" in reasons_shown(again)
    tested(client, worker, clock)
    assert publish(client).status_code == 303


# O prompt da demonstração cita as respostas de tamanho_equipe e principal_desafio (os valores).
TEAM_SIZE = "steps.1.questions.0"
CHALLENGE = "steps.1.questions.1"


def change_cited_value(fields: Fields) -> None:
    fields[f"{TEAM_SIZE}.options.0.value"] = "sozinha"


def change_cited_kind(fields: Fields) -> None:
    fields[f"{CHALLENGE}.kind"] = "choices"


@pytest.mark.parametrize("change", [change_cited_value, change_cited_kind], ids=["valor", "tipo"])
def test_a_change_to_a_question_the_prompt_cites_needs_a_test(
    client: TestClient, worker: TestTrigger, clock: FakeClock, change: Callable[[Fields], None]
) -> None:
    """O valor de uma opção e o tipo da pergunta mudam o que chega ao LLM na resposta citada."""
    assert save(client, change).status_code == 303

    refused = publish(client)

    assert refused.status_code == 422
    assert "antes de publicar: mudaram as perguntas que o prompt cita" in reasons_shown(refused)
    tested(client, worker, clock)
    assert publish(client).status_code == 303


def test_an_option_label_the_prompt_does_not_read_needs_no_test(client: TestClient) -> None:
    def relabel(fields: Fields) -> None:
        fields[f"{TEAM_SIZE}.options.0.label"] = "Trabalho sozinha"

    assert save(client, relabel).status_code == 303

    assert "não precisa de teste" in text(review(client).text)
    assert publish(client, note="Rótulo de uma opção.").status_code == 303


def change_screen_and_consent(fields: Fields) -> None:
    fields["intro"] = "Uma introdução nova para a página."
    fields["consent.processing"] = "Concordo com o uso das minhas respostas para gerar o plano."
    fields["steps.0.questions.0.label"] = "Seu nome completo"


def change_screen_consent_and_pdf(client: TestClient) -> None:
    """Muda a página e o consentimento no editor e o CSS do PDF no editor do PDF."""
    save(client, change_screen_and_consent)
    fields = form_fields(client.get(f"{DRAFT}/pdf").text, "pdf-form")
    fields["pdf_css"] += "\nh2 { color: #7E1620; }\n"
    assert post(client, f"{DRAFT}/pdf", **fields).status_code == 303


def test_changes_only_to_screen_texts_the_consent_or_the_pdf_need_no_test(
    client: TestClient, deps: Deps
) -> None:
    change_screen_consent_and_pdf(client)
    assert "não precisa de teste" in text(review(client).text)

    response = publish(client, note="Textos da página e do consentimento.")

    assert response.status_code == 303
    consent = text(section_of(client.get(f"/admin/forms/{SLUG}"), "consentimento"))
    # O texto mudou: a versão dele sobe sozinha, e quem tinha a página aberta concorda de novo.
    assert "demo-definicao-v2" in consent and "gerar o plano." in consent
    with deps.sessions() as session:
        (entry,) = session.scalars(sa.select(AuditLog).where(AuditLog.action == "form.publish"))
    assert entry.details["consent_version"] == "demo-definicao-v2"


def with_data(fields: Fields) -> None:
    """Dois blocos de valor livre nos dados, pelos campos do editor, e o prompt lendo um deles."""
    blocks = {
        "edicao": {"nome": "Edição de outubro"},
        "outro": {"nota": "Não vai ao prompt."},
    }
    for i, (name, value) in enumerate(blocks.items()):
        fields[f"data.{i}.name"] = name
        fields[f"data.{i}.kind"] = "value"
        fields[f"data.{i}.value"] = json.dumps(value, ensure_ascii=False)
    fields["prompt"] += "Edição: {{ data.edicao.nome }}\n"


def change_block(name: str, value: dict[str, Any]) -> Callable[[Fields], None]:
    def change(fields: Fields) -> None:
        (index,) = [
            key.split(".")[1]
            for key, found in fields.items()
            if found == name and re.fullmatch(r"data\.\d+\.name", key)
        ]
        fields[f"data.{index}.value"] = json.dumps(value, ensure_ascii=False)

    return change


def test_a_change_to_data_the_prompt_reads_needs_a_test_and_other_data_does_not(
    client: TestClient, worker: TestTrigger, clock: FakeClock
) -> None:
    save(client, with_data)
    tested(client, worker, clock)
    assert publish(client, note="Dados da edição.").status_code == 303

    save(client, change_block("outro", {"nota": "Outra nota, que o prompt não lê."}))
    assert publish(client, note="Só o bloco que o prompt não lê.").status_code == 303
    save(client, change_block("edicao", {"nome": "Edição de novembro"}))
    refused = publish(client, note="A edição que o prompt cita.")

    assert refused.status_code == 422
    assert "mudaram os dados do formulário que o prompt lê" in reasons_shown(refused)


@pytest.mark.parametrize(
    "line",
    [
        "Dados: {{ data }}\n",
        "{% for nome, bloco in data.items() %}{{ nome }} {% endfor %}\n",
        '{% set bloco = "edi" ~ "cao" %}{{ data[bloco].nome }}\n',
        "{% set dados = data %}{{ dados.edicao.nome }}\n",
    ],
    ids=["inteiro", "metodo", "chave-calculada", "redefinida"],
)
def test_data_read_in_ways_that_cannot_be_traced_counts_as_read_whole(
    client: TestClient, worker: TestTrigger, clock: FakeClock, line: str
) -> None:
    def dynamic(fields: Fields) -> None:
        with_data(fields)
        fields["prompt"] += line

    save(client, dynamic)
    tested(client, worker, clock)
    assert publish(client, note="Dados lidos de outro jeito.").status_code == 303

    save(client, change_block("outro", {"nota": "Uma nota que o prompt pode ler."}))
    refused = publish(client, note="Só o outro bloco.")

    assert refused.status_code == 422
    assert "mudaram os dados do formulário que o prompt lê" in reasons_shown(refused)


CONTACT_LINE = "Escritório de {{ answers.nome }}.\n"


def test_publishing_asks_for_a_confirmation_when_the_prompt_cites_contact_data(
    client: TestClient, deps: Deps, worker: TestTrigger, clock: FakeClock
) -> None:
    def cite_name(fields: Fields) -> None:
        fields["prompt"] = CONTACT_LINE + fields["prompt"]

    save(client, cite_name)
    tested(client, worker, clock)

    asked = publish(client)

    assert asked.status_code == 422
    assert "O prompt manda ao LLM o nome da lead" in text(section_of(asked, "contato"))
    assert tags(asked.text, "input", name="confirm_contact")
    assert publish(client, confirm_contact=True).status_code == 303
    with deps.sessions() as session:
        (entry,) = session.scalars(sa.select(AuditLog).where(AuditLog.action == "form.publish"))
    assert entry.details["contact_in_prompt"] == ["name"]


@pytest.mark.parametrize(
    "line",
    ["{% set lead = answers %}Contato: {{ lead.email }}\n", "Respostas: {{ answers }}\n"],
    ids=["set", "inteiro"],
)
def test_a_prompt_that_reads_the_answers_indirectly_counts_every_contact_data(
    client: TestClient, worker: TestTrigger, clock: FakeClock, line: str
) -> None:
    def indirect(fields: Fields) -> None:
        fields["prompt"] = line + fields["prompt"]

    assert save(client, indirect).status_code == 303
    tested(client, worker, clock)

    asked = publish(client)

    assert asked.status_code == 422
    assert "O prompt manda ao LLM o nome e o e-mail da lead" in text(section_of(asked, "contato"))


def test_the_publication_renders_the_example_pdf_like_the_import(
    client: TestClient, deps: Deps
) -> None:
    deps.pdf = WeasyPrintRenderer()

    # O endereço montado em tempo de execução passa pela conferência do texto, que só lê o que está
    # escrito: quem o recusa é o adaptador do PDF, na renderização do exemplo.
    fields = form_fields(client.get(f"{DRAFT}/pdf").text, "pdf-form")
    fields["pdf_html"] = fields["pdf_html"].replace(
        "<h1>{{ plan.titulo }}</h1>",
        "<h1>{{ plan.titulo }}</h1><img src=\"{{ 'https' }}://cdn.example.test/logo.png\">",
    )
    assert post(client, f"{DRAFT}/pdf", **fields).status_code == 303
    response = publish(client, note="Logo nova.")

    assert response.status_code == 422
    assert "pdf: o PDF de exemplo não foi gerado: template do PDF pediu um recurso fora de" in text(
        response.text
    )
    assert "Versão 1" in text(section_of(client.get(f"/admin/forms/{SLUG}"), "versao"))


def test_without_a_pending_draft_there_is_nothing_to_publish(client: TestClient) -> None:
    assert "Nada para publicar" in text(review(client).text)

    response = publish(client, expected=editor(client)["expected"])

    assert response.status_code == 422
    assert "Não há o que publicar" in reasons_shown(response)


def test_publishing_keeps_the_status_and_the_admin_name_follows_the_new_title(
    client: TestClient,
) -> None:
    assert post(client, f"/admin/forms/{SLUG}/deactivate", confirm="on").status_code == 303

    def new_title(fields: Fields) -> None:
        fields["title"] = "Diagnóstico rápido, segunda edição"

    save(client, new_title)
    assert publish(client, note="Título novo.").status_code == 303

    row = text(rows_of(client.get("/admin/forms"))[SLUG])
    assert "Diagnóstico rápido, segunda edição" in row and "Inativo" in row
    assert client.get(f"/p/{SLUG}").status_code == 410  # segue inativo


def test_a_publication_from_a_page_opened_before_the_last_change_publishes_nothing(
    client: TestClient,
) -> None:
    change_screen_consent_and_pdf(client)
    stale = hidden(review(client).text, "expected")

    def another_change(fields: Fields) -> None:
        fields["intro"] = "Outra introdução, salva em outra aba."

    save(client, another_change)
    response = publish(client, expected=stale)

    assert response.status_code == 409
    assert "O rascunho mudou desde que você abriu esta página" in text(response.text)
    assert "Versão 1" in text(section_of(client.get(f"/admin/forms/{SLUG}"), "versao"))


# --- histórico, comparação e volta a uma versão -------------------------------------------------

COMPARE = f"/admin/forms/{SLUG}/versions/compare"
NEW_INTRO = "Uma introdução nova para a página."
FIRST_INTRO = json.loads(DEFINITION)["intro"]


def second_version(client: TestClient) -> None:
    """A versão 2, com outra introdução (sem teste: é texto da página)."""
    change_screen_consent_and_pdf(client)
    assert publish(client, note="Textos da página e do consentimento.").status_code == 303


def test_the_history_shows_the_date_the_author_and_the_note_of_each_version(
    client: TestClient,
) -> None:
    second_version(client)

    history = section(client.get(f"/admin/forms/{SLUG}").text, "historico")

    shown = text(history)
    assert shown.index("Versão 2") < shown.index("Versão 1")  # a mais recente primeiro
    assert "Textos da página e do consentimento." in shown and "operadora" in shown
    assert "Importação da definição." in shown  # a nota da versão 1
    assert re.search(r"Versão 2 .*\d{2}/\d{2}/\d{4} \d{2}:\d{2}", shown)
    assert tags(history, "a", href=f"{COMPARE}?from=1&to=2")
    assert tags(history, "a", href=f"/admin/forms/{SLUG}/versions/1/export")


def test_two_versions_are_compared_section_by_section(client: TestClient) -> None:
    second_version(client)

    page = client.get(COMPARE, params={"from": "1", "to": "2"})

    assert page.status_code == 200
    changed = text(section_of(page, "pagina"))
    assert f'- "intro": "{FIRST_INTRO}"' in changed and f'+ "intro": "{NEW_INTRO}"' in changed
    assert "+h2 { color: #7E1620; }" in text(section_of(page, "pdf-css"))
    unchanged = section(page.text, "sem-mudanca")
    assert tags(unchanged, "li", id="prompt") and tags(unchanged, "li", id="modelo-do-plano")
    assert not tags(unchanged, "li", id="pagina")


def test_the_draft_is_compared_with_a_published_version(client: TestClient) -> None:
    save(client, add_prompt_line)

    page = client.get(COMPARE, params={"from": "1", "to": "rascunho"})

    assert page.status_code == 200
    assert f"+{NEW_PROMPT_LINE}" in text(section_of(page, "prompt"))


def test_lines_that_look_like_diff_headers_still_show_in_the_comparison(
    client: TestClient,
) -> None:
    def markdown(fields: Fields) -> None:
        fields["prompt"] = fields["prompt"].replace("Diagnóstico do escritório:\n", "---\n", 1)
        fields["prompt"] += "++ destaque\n"

    save(client, markdown)

    shown = text(section_of(client.get(COMPARE, params={"to": "rascunho"}), "prompt"))

    assert "-Diagnóstico do escritório:" in shown and "+---" in shown
    assert "+++ destaque" in shown


@pytest.mark.parametrize("params", [{"from": "1", "to": "9"}, {"from": "um", "to": "1"}])
def test_comparing_a_version_that_does_not_exist_says_so(
    client: TestClient, params: dict[str, str]
) -> None:
    response = client.get(COMPARE, params=params)

    assert response.status_code == 404 and "Versão não encontrada" in text(response.text)


def revert(client: TestClient, number: int, *, confirm: bool = True) -> httpx.Response:
    form = {"expected": editor(client)["expected"]}
    if confirm:
        form["confirm"] = "on"
    return post(client, f"/admin/forms/{SLUG}/versions/{number}/revert", **form)


def test_going_back_copies_an_old_version_into_the_draft_which_publishes_as_a_new_version(
    client: TestClient, deps: Deps
) -> None:
    second_version(client)

    response = revert(client, 1)

    assert response.status_code == 303 and response.headers["location"] == DRAFT
    page = client.get(DRAFT)
    assert form_fields(page.text, EDITOR_FORM)["intro"] == FIRST_INTRO
    assert "a partir da versão 1" in text(page.text)
    assert "Rascunho pendente" in text(rows_of(client.get("/admin/forms"))[SLUG])
    assert publish(client, note="Volta à versão 1.").status_code == 303
    reading = client.get(f"/admin/forms/{SLUG}")
    assert "Versão 3" in text(section_of(reading, "versao"))
    assert FIRST_INTRO in text(section_of(reading, "pagina"))
    with deps.sessions() as session:
        (entry,) = session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "form.draft.revert")
        )
    assert entry.details["version"] == 1 and entry.details["form_slug"] == SLUG


def test_going_back_asks_for_a_confirmation_and_keeps_the_draft_without_it(
    client: TestClient,
) -> None:
    second_version(client)
    save(client, add_prompt_line)

    response = revert(client, 1, confirm=False)

    assert response.status_code == 422
    assert "Confirme que o rascunho passa a ser uma cópia da versão 1" in text(response.text)
    assert NEW_PROMPT_LINE in editor(client)["prompt"]


# --- acesso -------------------------------------------------------------------------------------

NEW_POSTS = [
    DRAFT,
    f"{DRAFT}/prompt",
    TESTS,
    PUBLISH,
    f"/admin/forms/{SLUG}/versions/1/revert",
]


@pytest.mark.parametrize("path", NEW_POSTS)
def test_every_new_post_needs_the_session_and_the_csrf_token(
    client: TestClient, deps: Deps, worker: TestTrigger, path: str
) -> None:
    anonymous = TestClient(client.app, follow_redirects=False)
    form = {"note": NOTE, "expected": editor(client)["expected"], "confirm": "on"}

    refused = [
        anonymous.post(path, data={"csrf_token": "x", **form}, headers=ORIGIN),
        client.post(path, data={"csrf_token": "errado", **form}, headers=ORIGIN),
        client.post(path, data={"csrf_token": csrf_token(client), **form}),  # sem a origem
    ]

    assert [response.status_code for response in refused] == [303, 403, 403]
    assert refused[0].headers["location"].startswith("/admin/login")
    assert worker.queued == []
    with deps.sessions() as session:
        actions = set(session.scalars(sa.select(AuditLog.action)))
    assert actions == {"form.import"}  # nada mudou


@pytest.mark.parametrize("page", ["", "/tests", "/publish"])
def test_the_draft_pages_need_the_session(client: TestClient, page: str) -> None:
    anonymous = TestClient(client.app, follow_redirects=False)

    response = anonymous.get(f"{DRAFT}{page}")

    assert response.status_code == 303 and response.headers["location"].startswith("/admin/login")


def test_the_draft_never_reaches_the_public_page_or_the_api(client: TestClient, deps: Deps) -> None:
    def new_intro(fields: Fields) -> None:
        fields["intro"] = NEW_INTRO

    save(client, new_intro)

    assert NEW_INTRO not in client.get(f"/p/{SLUG}").text
    assert deps.forms.current(SLUG).version == "demo-definicao-v1"


def test_a_form_that_does_not_exist_has_no_draft(client: TestClient) -> None:
    response = client.get("/admin/forms/nao-existe/draft")

    assert response.status_code == 404 and "não tem rascunho" in text(response.text)


def test_editor_keeps_values_after_invalid_save(client: TestClient) -> None:
    fields = editor(client)
    broken_json(fields)
    fields["title"] = "Proposta sintética preservada"
    response = post(client, DRAFT, **fields)
    assert response.status_code == 422
    assert "Edição ainda não salva" in text(response.text)
    assert form_fields(response.text, EDITOR_FORM)["title"] == fields["title"]
    assert textareas(response.text)["definition"] == fields["definition"]
    assert hidden(response.text, "expected") == fields["expected"]
    assert "location" not in response.headers


def test_editor_rejects_stale_token(client: TestClient) -> None:
    stale = editor(client)
    assert save(client, add_prompt_line).status_code == 303
    stale["title"] = "Edição de outra aba"
    response = post(client, DRAFT, **stale)
    assert response.status_code == 409
    assert "Edição ainda não salva" in text(response.text)
    assert "Abra o rascunho atual em outra aba" in text(response.text)
    assert "compare e reaplique" in text(response.text)
    assert "salvar de novo substitui" not in text(response.text)
    assert form_fields(response.text, EDITOR_FORM)["title"] == stale["title"]
    assert hidden(response.text, "expected") == stale["expected"]
    assert post(client, DRAFT, **form_fields(response.text, EDITOR_FORM)).status_code == 409


def test_save_and_preview_redirects_only_after_commit(client: TestClient, deps: Deps) -> None:
    fields = editor(client)
    fields["title"] = "Título salvo antes da prévia"
    response = post(client, DRAFT, **fields, after_save="preview")
    assert response.status_code == 303
    assert response.headers["location"] == f"{DRAFT}/preview"
    assert editor(client)["title"] == fields["title"]
    preview = client.get(response.headers["location"])
    assert "rascunho salvo" in text(preview.text)
    assert deps.forms.current(SLUG).title != fields["title"]


@pytest.mark.parametrize("destination", ["https://example.test/escape", "", "PREVIEW"])
def test_editor_rejects_invalid_after_save_before_writing(
    client: TestClient, destination: str
) -> None:
    fields = editor(client)
    original_title = fields["title"]
    fields["title"] = "Não deve ser gravado"
    response = post(client, DRAFT, **fields, after_save=destination)
    assert response.status_code == 422
    assert "Edição ainda não salva" in text(response.text)
    assert "location" not in response.headers
    assert editor(client)["title"] == original_title
    assert form_fields(response.text, EDITOR_FORM)["title"] == fields["title"]


def test_editor_preserves_entries_when_commit_fails(client: TestClient, deps: Deps) -> None:
    fields = editor(client)
    fields["title"] = "Edição preservada na falha"

    def reject_commit(session) -> None:
        raise sa.exc.OperationalError("commit", {}, RuntimeError("synthetic_transaction_failure"))

    sa.event.listen(deps.sessions, "before_commit", reject_commit)
    try:
        response = post(client, DRAFT, **fields, after_save="preview")
    finally:
        sa.event.remove(deps.sessions, "before_commit", reject_commit)
    assert response.status_code == 503
    assert "location" not in response.headers
    assert "Edição ainda não salva" in text(response.text)
    assert "synthetic_transaction_failure" not in response.text
    assert form_fields(response.text, EDITOR_FORM)["title"] == fields["title"]
    assert editor(client)["title"] != fields["title"]
    assert "Rascunho salvo." not in text(client.get(DRAFT).text)


def test_save_and_preview_uses_saved_slug_after_rename(client: TestClient) -> None:
    created = post(
        client, "/admin/forms/new", title="Formulário sintético", slug="antes", source=""
    )
    assert created.status_code == 303, created.text
    fields = editor(client, "/admin/forms/antes/draft")
    fields["slug"] = "depois"
    response = post(client, "/admin/forms/antes/draft", **fields, after_save="preview")
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/admin/forms/depois/draft/preview"
    assert client.get(response.headers["location"]).status_code == 200
    assert client.get("/admin/forms/antes/draft").status_code == 404


def test_editor_task_index_keeps_one_save_form_and_published_version(client: TestClient) -> None:
    response = client.get(DRAFT)
    for anchor, label in [
        ("perguntas", "Perguntas"),
        ("dados-consentimentos", "Dados e consentimentos"),
        ("apos-envio", "Após o envio"),
        ("plano-apresentacao", "Plano e apresentação"),
        ("avancado", "Avançado"),
    ]:
        assert f'href="#{anchor}"' in response.text
        assert f'id="{anchor}"' in response.text
        assert label in text(response.text)
    assert response.text.count('id="editor-form"') == 1
    assert "Versão publicada: 1" in text(response.text)
    assert "Rascunho salvo" in text(response.text)
    assert hidden(response.text, "expected") not in text(response.text)


def test_editor_preserves_published_version_and_question_ids_when_cancelling_capture(
    client: TestClient,
    deps: Deps,
) -> None:
    fields = editor(client)
    fields["title"] = "Edição mantida ao cancelar"
    fields["generation"] = "none"
    fields["completion.mode"] = "confirmation"
    response = post(client, DRAFT, **fields)
    assert response.status_code == 422
    assert "Confirme a retirada da geração" in text(response.text)
    recovered = form_fields(response.text, EDITOR_FORM)
    for name in fields:
        if name.startswith(("steps.", "plan.")) or name in ("prompt", "instructions", "title"):
            assert recovered[name] == fields[name], name
    # Cancelar a troca conserva a configuração que ainda está no formulário integral.
    recovered["generation"] = "pdf"
    assert post(client, DRAFT, **recovered).status_code == 303
    saved = editor(client)
    for name in fields:
        if name.startswith(("steps.", "plan.")) or name in ("prompt", "instructions", "title"):
            assert saved[name] == fields[name], name
    assert deps.forms.current(SLUG).title == json.loads(DEFINITION)["title"]
    # Confirmar remove geração só do rascunho, sem renomear perguntas ou valores.
    saved["generation"] = "none"
    saved["confirm_generation_removal"] = "on"
    assert post(client, DRAFT, **saved).status_code == 303
    capture = editor(client)
    assert capture["generation"] == "none"
    for name in fields:
        if name.startswith("steps."):
            assert capture[name] == fields[name], name
    assert deps.forms.current(SLUG).generation == "pdf"


def test_command_action_url_survives_reload_and_repost(client, deps, worker):
    entry = client.get(TESTS)
    assert entry.status_code == 303
    action_url = entry.headers["location"]
    page = client.get(action_url)
    assert page.status_code == 200
    identity = re.search(r'name="command_id" value="([^"]+)"', page.text).group(1)
    assert f'action="{action_url}"' in page.text
    expected = re.search(r'name="expected" value="([^"]+)"', page.text).group(1)
    result = post(
        client,
        TESTS,
        command_id=identity,
        expected=expected,
        input_mode="json",
        answers="",
        confirm="on",
    )
    assert result.status_code == 303
    recovery = client.get(action_url)
    assert recovery.status_code == 200
    assert "Recibo do teste" in recovery.text
    assert identity in recovery.text
    again = post(
        client, TESTS, command_id=identity, expected=expected, input_mode="json", answers=""
    )
    assert again.status_code == 303
    assert again.headers["location"] == result.headers["location"]
    assert len(worker.queued) == 1


def test_identified_fields_replay_does_not_read_new_schema(client, deps, worker, clock):
    page = client.get(TESTS, follow_redirects=True)
    identity, expected = hidden(page.text, "command_id"), hidden(page.text, "expected")
    example = json.loads(DEFINITION)["examples"]["answers"]
    data = {"_simulation_" + name: value for name, value in example.items()}
    first = post(
        client,
        TESTS,
        **data,
        input_mode="fields",
        expected=expected,
        command_id=identity,
        confirm="on",
    )
    assert first.status_code == 303
    from app.forms import drafts

    with deps.sessions() as session:
        current = drafts.load(session, SLUG)
    changed = json.loads(DEFINITION)
    changed["examples"]["answers"]["escritorio"] = "Outro exemplo"
    drafts.replace_document(deps, SLUG, changed, actor="testes", expected=current.token)
    clock.advance(86400)
    assert login(client).status_code == 303
    again = post(client, TESTS, **data, input_mode="fields", expected=expected, command_id=identity)
    assert again.status_code == 303
    assert again.headers["location"] == first.headers["location"]
    assert len(worker.queued) == 1


def test_identified_form_rejects_unknown_input_mode(client, worker):
    page = client.get(TESTS, follow_redirects=True)
    response = post(
        client,
        TESTS,
        input_mode="unknown",
        answers="",
        command_id=hidden(page.text, "command_id"),
        expected=hidden(page.text, "expected"),
        confirm="on",
    )
    assert response.status_code == 422
    assert not worker.queued


def test_failed_execution_is_shown_even_when_dispatch_is_uncertain(client, deps, clock):
    from app.forms import draft_test_commands as commands

    page = client.get(TESTS, follow_redirects=True)
    identity = hidden(page.text, "command_id")
    calls = []

    class Fails:
        def generate(self, spec, answers):
            raise RuntimeError("falha sintética da geração")

    deps.llm = Fails()

    async def delivered_then_response_lost(test_id):
        calls.append(test_id)
        assert (
            draft_tests.run(deps, test_id, now=lambda: datetime.fromtimestamp(clock(), UTC))
            == "failed"
        )

    client.app.state.test_trigger = delivered_then_response_lost
    response = post(
        client,
        TESTS,
        command_id=identity,
        expected=hidden(page.text, "expected"),
        input_mode="json",
        answers="",
        confirm="on",
    )
    assert response.status_code == 303
    receipt_page = client.get(response.headers["location"])
    receipt = commands.resolve(
        deps,
        SLUG,
        identity,
        principal=commands.Principal("admin", "operadora"),
        now=datetime.fromtimestamp(clock(), UTC),
    )
    assert receipt.test_status == "failed" and receipt.dispatch_state == "uncertain"
    assert "O teste falhou" in receipt_page.text
    assert "Despacho incerto" in receipt_page.text
    assert len(calls) == 1


def test_receipt_preserves_an_explicitly_not_started_dispatch(client):
    from app.admin.launching import DraftTestDispatch

    async def not_started(test_id):
        return DraftTestDispatch("not_started")

    client.app.state.test_trigger = not_started
    page = client.get(TESTS, follow_redirects=True)
    response = post(
        client,
        TESTS,
        command_id=hidden(page.text, "command_id"),
        expected=hidden(page.text, "expected"),
        input_mode="json",
        answers="",
        confirm="on",
    )
    assert response.status_code == 303
    receipt_page = client.get(response.headers["location"])
    assert "Teste não iniciado" in receipt_page.text
    assert "O teste falhou" not in receipt_page.text
