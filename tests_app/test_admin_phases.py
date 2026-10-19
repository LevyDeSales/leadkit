"""As fases com datas no admin, pela costura HTTP (TestClient): o teste do rascunho aceita uma data
simulada e mostra o calendário que o prompt recebe, e o teste com o LLM confere o plano no dia
simulado."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_admin import FakeClock, login
from test_admin_destinations import ADMIN_ENV
from test_admin_drafts import Fields, TestTrigger, editor, post, problems_shown, save
from test_admin_form_editor import pending, stored
from test_admin_forms import DEFINITION as DEMO_DEFINITION
from test_admin_forms import SLUG as DEMO_SLUG
from test_admin_forms import import_form, section_of
from test_form_phases import EXAMPLE, PHASES, phase, phased, plan_of
from test_openai_llm import FakeOpenAI, generator, reply
from test_public import tags, text

from app.api.main import create_app
from app.deps import Deps
from app.settings import Settings

SLUG = "demo-fases"
DRAFT = f"/admin/forms/{SLUG}/draft"
TESTS = f"{DRAFT}/tests"


def definition() -> dict[str, Any]:
    document = phased(slug=SLUG)
    document["consent"]["version"] = f"{SLUG}-v1"
    return document


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def worker(deps: Deps, clock: FakeClock) -> TestTrigger:
    return TestTrigger(deps, clock)


@pytest.fixture
def client(deps: Deps, clock: FakeClock, worker: TestTrigger) -> TestClient:
    """A operadora no admin, com o formulário de fases importado (versão 1)."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, test_trigger=worker, clock=clock),
        follow_redirects=False,
    )
    assert login(current).status_code == 303
    assert import_form(current, json.dumps(definition())).status_code == 303
    return current


def start_test(client: TestClient, **form: str) -> httpx.Response:
    return post(client, TESTS, **form)


def result_path(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert re.fullmatch(rf"{TESTS}/[0-9a-f-]{{36}}", location), location
    return location


def calendar_of(response: httpx.Response) -> str:
    """O calendário da página como se lê, sem o espaço que o HTML deixa antes dos dois-pontos."""
    return re.sub(r"\s+:", ":", text(section_of(response, "calendario")))


def test_the_test_page_shows_the_calendar_the_prompt_gets_on_the_simulated_date(
    client: TestClient,
) -> None:
    response = post(client, f"{DRAFT}/prompt", date="2026-10-08")

    assert response.status_code == 200
    shown = calendar_of(response)
    assert "na data simulada: 8 de outubro de 2026" in shown
    assert "Fase 1 8 de outubro a 6 de novembro de 2026" in shown
    assert "Opção: está aberta e fecha nesta fase" in shown
    assert "Desistência: abre nesta fase" in shown
    assert "tipo: decisao, desistencia, proxima, outra" in shown
    assert "Fase 2 7 de novembro a 6 de dezembro de 2026" in shown
    assert "Opção: já fechou" in shown
    assert "tipo: desistencia, proxima, outra" in shown
    # O calendário da página é o do prompt montado, linha a linha.
    prompt = text(section_of(response, "prompt-montado"))
    for line in ("Fase 1 (8 de outubro a 6 de novembro de 2026)", "Início: 8 de outubro de 2026"):
        assert line in prompt, line


def test_another_simulated_date_gives_another_calendar(client: TestClient) -> None:
    response = post(client, f"{DRAFT}/prompt", date="2026-11-01")

    shown = calendar_of(response)
    assert "Fase 1 1º de novembro a 30 de novembro de 2026" in shown
    assert "Opção: já fechou" in shown
    assert "tipo: desistencia, proxima, outra" in shown
    assert "decisao" not in shown


def test_without_a_simulated_date_the_calendar_starts_today(client: TestClient) -> None:
    # O relógio do teste marca 15 de janeiro de 2027, e a página já abre com o prompt montado.
    for response in (client.get(TESTS), post(client, f"{DRAFT}/prompt")):
        assert response.status_code == 200
        shown = calendar_of(response)
        assert "hoje: 15 de janeiro de 2027" in shown
        assert "Fase 1 15 de janeiro a 13 de fevereiro de 2027" in shown
        assert "Opção: já fechou" in shown and "Desistência: já fechou" in shown
        assert "tipo: proxima, outra" in shown


def test_a_form_without_phases_shows_no_calendar(client: TestClient) -> None:
    assert import_form(client, DEMO_DEFINITION).status_code == 303

    response = post(client, f"/admin/forms/{DEMO_SLUG}/draft/prompt", date="2026-10-08")

    assert response.status_code == 200
    assert 'id="calendario"' not in response.text


def test_the_draft_test_checks_the_plan_on_the_simulated_date(
    client: TestClient, deps: Deps, worker: TestTrigger
) -> None:
    late = plan_of(phase("decisao", "outra"), phase("decisao"), phase("proxima", "outra"))
    server = FakeOpenAI(reply(late), reply(EXAMPLE))
    deps.llm = generator(server)

    start_test(client, date="2026-10-08")
    worker.run()

    # No dia simulado a opção já fechou na fase 2: o primeiro plano volta para o reparo.
    assert len(server.requests) == 2
    feedback = server.bodies[1]["input"][-1]["content"]
    assert "fases.1.acoes.0.tipo (fase 2): o valor decisao não cabe nesta fase" in feedback
    assert "Ação do tipo decisao" not in feedback  # só a posição e a regra, nunca o texto do plano


def test_a_plan_that_does_not_fit_the_simulated_date_fails_the_test_after_the_repair(
    client: TestClient, deps: Deps, worker: TestTrigger
) -> None:
    # No dia simulado a opção já fechou na fase 1, e o plano de exemplo decide a opção nela.
    server = FakeOpenAI(reply(EXAMPLE), reply(EXAMPLE))
    deps.llm = generator(server)

    result = result_path(start_test(client, date="2026-11-01"))
    worker.run()

    shown = text(section_of(client.get(result), "resultado"))
    assert "Falhou" in shown and "plano inválido" in shown
    assert (
        "Fase 1 (1º de novembro a 30 de novembro de 2026)"
        in server.bodies[0]["input"][0]["content"]
    )


# --- o editor em campos -------------------------------------------------------------------------

LINKS_FIELD = "plan.fields.1.items.fields.1.items.fields.0.in_window"


def fields_of(client: TestClient, draft: str = DRAFT) -> Fields:
    return editor(client, draft)


def options_of(page: str, name: str) -> list[tuple[str, bool]]:
    """As opções de uma lista do editor, como `(valor, escolhida)`."""
    found = re.search(rf'<select[^>]*name="{re.escape(name)}".*?</select>', page, re.DOTALL)
    assert found is not None, name
    return [
        (value, " selected" in attrs)
        for value, attrs in re.findall(r'<option value="([^"]*)"([^>]*)>', found.group(0))
    ]


def stored_draft(deps: Deps) -> dict[str, Any]:
    return stored(deps, SLUG)


def test_the_editor_shows_the_phases_and_the_links_as_fields_and_not_as_json(
    client: TestClient,
) -> None:
    page = client.get(DRAFT)
    fields = fields_of(client)

    assert fields["phases.days"] == "30, 30, 30"
    assert fields["phases.plan_field"] == "fases"
    assert fields["phases.windows"] == "janelas"
    assert fields[LINKS_FIELD] == "decisao = opcao\ndesistencia = desistencia"
    assert "phases" not in json.loads(fields["definition"])
    assert options_of(page.text, "phases.plan_field") == [("", False), ("fases", True)]
    assert options_of(page.text, "phases.windows") == [("", False), ("janelas", True)]


def test_saving_the_phases_unchanged_leaves_the_published_version_as_it_is(
    client: TestClient, deps: Deps
) -> None:
    response = save(client, draft=DRAFT)

    assert response.status_code == 303
    assert not pending(client, SLUG)
    assert stored_draft(deps) == definition()


def test_the_phases_are_edited_and_saved_in_the_definition(client: TestClient, deps: Deps) -> None:
    def change(fields: Fields) -> None:
        fields["phases.days"] = "15,45 30"
        fields[LINKS_FIELD] = "desistencia = desistencia\n\ndecisao=opcao\n"

    assert save(client, change, draft=DRAFT).status_code == 303

    document = stored_draft(deps)
    assert document["phases"] == {**PHASES, "days": [15, 45, 30]}
    tipo = document["plan"]["fields"][1]["items"]["fields"][1]["items"]["fields"][0]
    assert tipo["in_window"] == {"desistencia": "desistencia", "decisao": "opcao"}
    assert pending(client, SLUG)


def test_an_edited_value_that_has_no_link_is_saved_without_one(
    client: TestClient, deps: Deps
) -> None:
    def change(fields: Fields) -> None:
        fields[LINKS_FIELD] = "decisao = opcao"

    assert save(client, change, draft=DRAFT).status_code == 303

    tipo = stored_draft(deps)["plan"]["fields"][1]["items"]["fields"][1]["items"]["fields"][0]
    assert tipo["in_window"] == {"decisao": "opcao"}


def test_a_form_without_phases_has_empty_fields_and_saving_adds_nothing(
    client: TestClient, deps: Deps
) -> None:
    assert import_form(client, DEMO_DEFINITION).status_code == 303
    demo = f"/admin/forms/{DEMO_SLUG}/draft"

    fields = fields_of(client, demo)
    assert (fields["phases.days"], fields["phases.plan_field"], fields["phases.windows"]) == (
        "",
        "",
        "",
    )
    assert save(client, draft=demo).status_code == 303
    assert "phases" not in stored(deps, DEMO_SLUG)
    assert not pending(client, DEMO_SLUG)


@pytest.mark.parametrize(
    ("change", "place", "message"),
    [
        (
            {"phases.days": "30, abc, 30"},
            "phases.days",
            "Fases com datas · dias de cada fase: precisa ser um número inteiro",
        ),
        (
            {"phases.days": "30, 0, 30"},
            "phases.days",
            "Fases com datas · dias de cada fase: precisa ser pelo menos 1",
        ),
        (
            {"phases.days": ""},
            "phases.days",
            "Fases com datas · dias de cada fase: precisa de pelo menos 1 item",
        ),
        (
            {"phases.days": "30, 30"},
            "phases.plan_field",
            (
                "Fases com datas · lista de fases do plano: a lista fases do plano precisa ter "
                "exatamente 2 itens, um por fase: use 2 no mínimo e no máximo de itens dela"
            ),
        ),
        (
            {"phases.windows": ""},
            LINKS_FIELD,
            (
                "Modelo do plano · fases › acoes › tipo · janela de cada valor: defina em phases o "
                "bloco de janelas (windows) de onde saem as janelas"
            ),
        ),
        (
            {"phases.plan_field": ""},
            LINKS_FIELD,
            (
                "Modelo do plano · fases › acoes › tipo · janela de cada valor: defina em phases a "
                "lista do plano com um item por fase (plan_field)"
            ),
        ),
        (
            {LINKS_FIELD: "decisao = nada"},
            LINKS_FIELD,
            (
                "Modelo do plano · fases › acoes › tipo · janela de cada valor: não há uma janela "
                "chamada nada no bloco janelas"
            ),
        ),
        (
            {LINKS_FIELD: "inventado = opcao"},
            LINKS_FIELD,
            (
                "Modelo do plano · fases › acoes › tipo · janela de cada valor: este valor não está "
                "nas opções do campo"
            ),
        ),
        (
            {LINKS_FIELD: "decisao opcao"},
            LINKS_FIELD,
            (
                "Modelo do plano · fases › acoes › tipo · janela de cada valor: use uma linha por "
                "valor, no formato valor = janela"
            ),
        ),
        (
            {LINKS_FIELD: "decisao = opcao\ndecisao = desistencia"},
            LINKS_FIELD,
            (
                "Modelo do plano · fases › acoes › tipo · janela de cada valor: o valor decisao "
                "aparece em mais de uma linha"
            ),
        ),
    ],
    ids=[
        "dias-em-texto",
        "zero-dias",
        "sem-dias",
        "contagem-da-lista",
        "sem-bloco-de-janelas",
        "sem-lista-de-fases",
        "janela-que-nao-existe",
        "valor-que-nao-existe",
        "linha-sem-igual",
        "valor-repetido",
    ],
)
def test_a_problem_with_the_phases_stops_the_save_and_shows_up_in_its_field(
    client: TestClient, deps: Deps, change: dict[str, str], place: str, message: str
) -> None:
    before = stored_draft(deps)
    fields = fields_of(client)
    fields.update(change)

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    assert message in problems_shown(response)
    invalid = [
        str(control["name"])
        for kind in ("input", "select", "textarea")
        for control in tags(response.text, kind)
        if control.get("aria-invalid") == "true"
    ]
    assert invalid == [place]
    assert stored_draft(deps) == before  # nada foi gravado


def test_the_phases_cannot_be_written_in_the_json_of_the_rest(client: TestClient) -> None:
    def change(fields: Fields) -> None:
        rest = json.loads(fields["definition"])
        fields["definition"] = json.dumps({**rest, "phases": PHASES})

    response = save(client, change, draft=DRAFT)

    assert response.status_code == 422
    assert (
        "Fases com datas: fica no editor estruturado, mais acima; tire do JSON"
        in problems_shown(response)
    )


def test_a_list_of_the_plan_renamed_in_the_editor_is_still_offered_with_a_warning(
    client: TestClient,
) -> None:
    fields = fields_of(client)
    fields["plan.fields.1.name"] = "etapas"

    response = post(client, f"{DRAFT}/edit", **fields, op="plan-refresh:plan.fields.1")

    assert options_of(response.text, "phases.plan_field") == [
        ("", False),
        ("etapas", False),
        ("fases", True),
    ]
    assert "fases (não existe mais no plano)" in response.text


def test_every_field_of_the_phases_has_a_label(client: TestClient) -> None:
    page = client.get(DRAFT).text
    labelled = {str(label["for"]) for label in tags(page, "label") if label.get("for")}
    controls = [*tags(page, "input"), *tags(page, "select"), *tags(page, "textarea")]
    named = [
        control
        for control in controls
        if str(control.get("name", "")).startswith("phases.") or control.get("name") == LINKS_FIELD
    ]

    assert len(named) == 4
    for control in named:
        assert control.get("id") in labelled, control["name"]


# --- a leitura da versão publicada --------------------------------------------------------------


def test_the_reading_of_the_published_version_lists_the_phases_among_the_plan_checks(
    client: TestClient,
) -> None:
    shown = text(section_of(client.get(f"/admin/forms/{SLUG}"), "modelo-do-plano"))

    assert "Conferência do plano" in shown
    assert (
        "O plano tem 3 fases de 30, 30 e 30 dias, a primeira a partir do dia em que o plano é "
        "gerado, em São Paulo. As janelas vêm do bloco janelas." in shown
    )
    assert (
        "fases.acoes.tipo: decisao só cabe nas fases em que a janela Opção está aberta; "
        "desistencia só cabe nas fases em que a janela Desistência está aberta. Os outros "
        "valores cabem em qualquer fase." in shown
    )


def test_the_reading_does_not_say_the_check_is_in_the_code_for_a_stored_form(
    client: TestClient,
) -> None:
    page = client.get(f"/admin/forms/{SLUG}")

    assert "no código (check_plan)" not in page.text


def test_the_editor_lists_the_phases_among_the_variables_of_the_prompt(client: TestClient) -> None:
    variables = text(section_of(client.get(DRAFT), "variaveis"))

    assert "phases" in variables and "uma entrada por fase" in variables
