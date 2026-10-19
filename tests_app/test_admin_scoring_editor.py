"""O editor em campos da pontuação (as notas, as faixas e as regras de prioridade e de persona), pela
costura HTTP (TestClient), como o navegador o usa: incluir, remover e reordenar voltam a página
inteira sem JavaScript e só o trecho do editor com o HTMX; o salvar mostra cada problema do
compilador no campo dele, com a coluna da fórmula; e a nota, a faixa e a regra feitas no editor
passam pelo painel, pelo teste com outras respostas e pela publicação."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx
import pytest
from conftest import DEMO_ANSWERS
from fastapi.testclient import TestClient
from test_admin import ORIGIN, FakeClock, csrf_token, login
from test_admin_destinations import ADMIN_ENV
from test_admin_drafts import (
    Fields,
    TestTrigger,
    editor,
    hidden,
    invalid_fields,
    post,
    problems_shown,
    save,
)
from test_admin_form_editor import HTMX, autofocused, form_of, pending
from test_admin_form_editor import stored as stored_in
from test_admin_forms import import_form, section_of
from test_admin_scoring import DEFINITION, DRAFT, PUBLISH, SLUG, stored
from test_public import form_fields, tags, text
from test_scoring import definition

from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.api.main import create_app
from app.deps import Deps
from app.forms.definition.formulas import FUNCTIONS
from app.settings import Settings

EDIT = f"{DRAFT}/edit"
NOTE = "Pontuação feita no editor"
PLAIN = "sem-pontuacao"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def worker(deps: Deps, clock: FakeClock) -> TestTrigger:
    return TestTrigger(deps, clock)


@pytest.fixture
def client(deps: Deps, clock: FakeClock, worker: TestTrigger) -> TestClient:
    """A operadora no admin, com o formulário de pontuação importado (versão 1)."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, test_trigger=worker, clock=clock),
        follow_redirects=False,
    )
    assert login(current).status_code == 303
    assert import_form(current, DEFINITION).status_code == 303
    # O PDF de verdade só depois da importação: ela renderiza o PDF de exemplo, o que o editor
    # não precisa provar a cada teste (`test_the_scoring_form_imports_with_the_real_pdf`).
    deps.pdf = WeasyPrintRenderer()  # o PDF de verdade: ele lê as notas
    return current


def operate(
    client: TestClient, fields: Fields, op: str, *, htmx: bool = False, draft: str = DRAFT
) -> httpx.Response:
    """Um botão do editor, como o navegador o manda: o formulário inteiro com a operação."""
    headers = {**ORIGIN, **(HTMX if htmx else {})}
    data = {**fields, "csrf_token": csrf_token(client), "op": op}
    return client.post(f"{draft}/edit", data=data, headers=headers)


def step(client: TestClient, fields: Fields, op: str) -> Fields:
    """Uma operação do editor, e o formulário que a página devolve."""
    return form_of(operate(client, fields, op))


def error_on(response: httpx.Response, name: str) -> str:
    """O problema que a página mostra embaixo do campo; vazio se não há."""
    field_id = "f-" + name.replace(".", "-")
    found = re.search(rf'<p class="error" id="{field_id}-erro">(.*?)</p>', response.text, re.DOTALL)
    return text(found.group(1)) if found else ""


def invalid_controls(response: httpx.Response) -> list[str]:
    """Os campos de entrada e as listas marcados com problema (os de texto longo, à parte)."""
    found = [*tags(response.text, "input"), *tags(response.text, "select")]
    return [str(item["name"]) for item in found if item.get("aria-invalid") == "true"]


def scoring_of(deps: Deps) -> dict[str, Any]:
    return stored(deps)["scoring"]


# --- o editor mostra a pontuação em campos ------------------------------------------------------


def test_the_editor_shows_the_scoring_as_fields_and_no_longer_as_json(client: TestClient) -> None:
    fields = editor(client, DRAFT)

    assert (fields["scoring.scores.0.name"], fields["scoring.scores.1.name"]) == (
        "porte",
        "alcance",
    )
    assert fields["scoring.scores.0.formula"] == "pontos(tamanho_equipe)"
    assert (
        fields["scoring.scores.1.formula"] == "arredondar_meio_para_cima(contagem(canais) * 12.5)"
    )
    assert fields["scoring.scores.0.bands.key"] == "nivel_porte"
    assert fields["scoring.scores.0.bands.label"] == "rotulo_porte"
    levels = "scoring.scores.0.bands.levels"
    assert [fields[f"{levels}.{n}.min"] for n in range(3)] == ["90", "60", ""]
    assert [fields[f"{levels}.{n}.key"] for n in range(3)] == ["grande", "medio", "pequeno"]
    assert [fields[f"{levels}.{n}.label"] for n in range(3)] == ["Grande", "Médio", "Pequeno"]
    assert "scoring.scores.1.bands.key" not in fields  # a nota sem faixas não tem os campos
    assert fields["scoring.priority.0.when"] == "porte >= 100"
    assert [fields[f"scoring.priority.{n}.value"] for n in range(3)] == ["P1", "P2", "P3"]
    assert fields["scoring.priority.2.when"] == ""  # o "senão"
    assert fields["scoring.persona.0.result"] == "value"
    assert fields["scoring.persona.0.value"] == "Comercial"
    assert fields["scoring.persona.0.when"] == 'principal_desafio == "vendas"'
    assert "scoring" not in json.loads(fields["definition"])  # saiu do JSON do resto


def test_saving_the_scoring_editor_unchanged_leaves_no_pending_draft(
    client: TestClient, deps: Deps
) -> None:
    response = save(client, draft=DRAFT)

    assert response.status_code == 303
    assert not pending(client, SLUG)
    assert stored(deps) == json.loads(DEFINITION)


def plain_form(client: TestClient) -> str:
    """Um formulário sem pontuação, ao lado do outro, e o endereço do rascunho dele."""
    plain = definition()
    plain["slug"] = PLAIN
    plain["consent"]["version"] = f"{PLAIN}-v1"
    assert import_form(client, json.dumps(plain)).status_code == 303
    return f"/admin/forms/{PLAIN}/draft"


def test_a_form_without_scoring_shows_the_editor_empty_and_a_save_does_not_invent_the_key(
    client: TestClient, deps: Deps
) -> None:
    draft = plain_form(client)

    page = client.get(draft)

    assert "Este formulário não tem pontuação" in text(section_of(page, "f-scoring"))
    assert tags(page.text, "button", id="op-scoring-add-score")
    assert not any(name.startswith("scoring.") for name in editor(client, draft))
    assert save(client, draft=draft).status_code == 303
    assert "scoring" not in stored_in(deps, PLAIN)
    assert not pending(client, PLAIN)


def test_the_first_note_of_a_form_without_scoring_is_made_in_the_editor(
    client: TestClient, deps: Deps
) -> None:
    draft = plain_form(client)
    fields = form_of(operate(client, editor(client, draft), "scoring-add-score", draft=draft))
    fields["scoring.scores.0.name"] = "canais_usados"
    fields["scoring.scores.0.formula"] = "contagem(canais)"

    response = post(client, draft, **fields)

    assert response.status_code == 303, text(response.text)
    assert stored_in(deps, PLAIN)["scoring"] == {
        "scores": [{"name": "canais_usados", "formula": "contagem(canais)"}]
    }
    assert pending(client, PLAIN)


def test_a_persona_taken_from_an_answer_and_an_explicit_empty_list_round_trip_unchanged(
    client: TestClient, deps: Deps
) -> None:
    """Uma definição importada pode trazer o padrão por extenso (`"persona": []`) e a persona como a
    resposta de uma pergunta: salvar sem mudança não cria rascunho pendente."""
    other = definition(
        {
            "scores": [{"name": "porte", "formula": "pontos(tamanho_equipe)"}],
            "priority": [{"when": "porte >= 50", "value": "P1"}, {"value": "P3"}],
            "persona": [{"answer": "principal_desafio"}],
        }
    )
    other["slug"] = "persona-resposta"
    other["consent"]["version"] = "persona-resposta-v1"
    assert import_form(client, json.dumps(other)).status_code == 303
    draft = "/admin/forms/persona-resposta/draft"
    fields = editor(client, draft)
    assert fields["scoring.persona.0.result"] == "answer"
    assert fields["scoring.persona.0.answer"] == "principal_desafio"
    assert fields["scoring.persona.0.when"] == ""

    assert post(client, draft, **fields).status_code == 303

    assert not pending(client, "persona-resposta")
    assert stored_in(deps, "persona-resposta")["scoring"] == other["scoring"]


def test_the_scoring_in_the_json_of_the_rest_is_sent_to_the_editor(client: TestClient) -> None:
    fields = editor(client, DRAFT)
    fields["definition"] = json.dumps(
        {**json.loads(fields["definition"]), "scoring": json.loads(DEFINITION)["scoring"]}
    )

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    assert "Pontuação: fica no editor estruturado, mais acima; tire do JSON" in problems_shown(
        response
    )


def test_every_scoring_field_has_a_label(client: TestClient) -> None:
    page = client.get(DRAFT).text
    labelled = {str(label["for"]) for label in tags(page, "label") if label.get("for")}
    controls = [*tags(page, "input"), *tags(page, "select"), *tags(page, "textarea")]
    named = [
        control
        for control in controls
        if str(control.get("name", "")).startswith("scoring.")
        and control.get("type") not in ("hidden", "radio", "checkbox")
    ]

    assert len(named) >= 20  # duas notas, três níveis, três regras de prioridade, três de persona
    for control in named:
        assert control.get("id") in labelled or control.get("aria-label"), control["name"]
    ids = [str(control["id"]) for control in named]
    assert len(ids) == len(set(ids))


# --- os nomes que a fórmula pode usar -----------------------------------------------------------


def test_the_names_a_formula_can_use_are_in_sight_next_to_the_fields(client: TestClient) -> None:
    names = text(section_of(client.get(DRAFT), "scoring-nomes"))

    for note in ("porte", "alcance", "nivel_porte", "rotulo_porte"):  # as notas e os níveis
        assert note in names, note
    assert "tamanho_equipe" in names and "so_eu" in names and "mais_de_15" in names
    assert "canais" in names and "principal_desafio" in names
    assert "vendas" in names  # os valores que uma comparação aceita
    for (
        function,
        description,
    ) in FUNCTIONS.items():  # a linguagem inteira, com o que cada função faz
        _, _, explanation = description.partition(": ")
        assert f"{function}(" in names and explanation in names, function


def test_the_names_panel_says_which_choice_questions_have_no_points_yet(
    client: TestClient,
) -> None:
    names = text(section_of(client.get(DRAFT), "scoring-nomes"))

    # `principal_desafio` não tem pontos em nenhuma opção: dá para comparar, mas não para pontos().
    assert "faltam pontos em" in names
    assert "captacao" in names  # as opções que ainda não têm


def test_the_names_follow_what_is_on_the_page_before_saving(client: TestClient) -> None:
    fields = step(client, editor(client, DRAFT), "scoring-add-score")
    fields["scoring.scores.2.name"] = "risco"
    fields["scoring.scores.0.bands.key"] = "nivel_do_porte"

    response = operate(client, fields, "scoring-add-rule:priority", htmx=True)

    names = text(section_of(response, "scoring-nomes"))
    assert "risco" in names and "nivel_do_porte" in names and "nivel_porte" not in names


# --- incluir, remover e reordenar ---------------------------------------------------------------


def test_adding_a_note_brings_the_page_back_with_it_and_saves_nothing(
    client: TestClient, deps: Deps
) -> None:
    fields = editor(client, DRAFT)

    response = operate(client, fields, "scoring-add-score")

    after = form_of(response)
    assert "<html" in response.text  # a página inteira, sem JavaScript
    assert (after["scoring.scores.2.name"], after["scoring.scores.2.formula"]) == ("", "")
    assert autofocused(response) == ["scoring.scores.2.name"]
    assert after["expected"] == fields["expected"]
    assert not pending(client, SLUG) and stored(deps) == json.loads(DEFINITION)


def test_with_htmx_only_the_scoring_editor_comes_back(client: TestClient) -> None:
    response = operate(client, editor(client, DRAFT), "scoring-add-score", htmx=True)

    assert response.status_code == 200 and "<html" not in response.text
    assert tags(response.text, "div", id="pontuacao-editor")
    assert tags(response.text, "input", name="scoring.scores.2.name")
    assert autofocused(response) == ["scoring.scores.2.name"]
    assert not tags(response.text, "textarea", name="prompt")  # o resto da página fica
    assert not tags(response.text, "input", name="steps.0.title")


def test_a_note_with_bands_and_a_rule_are_added_in_the_editor_and_saved(
    client: TestClient, deps: Deps
) -> None:
    fields = step(client, editor(client, DRAFT), "scoring-add-score")
    fields["scoring.scores.2.name"] = "risco"
    fields["scoring.scores.2.formula"] = "contagem(canais) * 10"

    fields = step(client, fields, "scoring-add-bands:2")
    assert fields["scoring.scores.2.bands.key"] == "nivel_risco"  # sugeridos pelo nome da nota
    assert fields["scoring.scores.2.bands.label"] == "rotulo_risco"
    assert fields["scoring.scores.2.bands.levels.0.min"] == ""  # o nível que vale para o resto
    levels = "scoring.scores.2.bands.levels"

    fields = step(client, fields, "scoring-add-level:2")
    # O novo nível entra antes do último, que segue valendo para o resto.
    fields[f"{levels}.0.min"] = "30"
    fields[f"{levels}.0.key"] = "alto"
    fields[f"{levels}.0.label"] = "Risco alto"
    fields[f"{levels}.1.key"] = "baixo"
    fields[f"{levels}.1.label"] = "Risco baixo"

    fields = step(client, fields, "scoring-add-rule:priority")
    # A regra nova entra antes do "senão", e ele continua por último.
    assert (fields["scoring.priority.2.when"], fields["scoring.priority.2.value"]) == ("", "")
    assert (fields["scoring.priority.3.when"], fields["scoring.priority.3.value"]) == ("", "P3")
    fields["scoring.priority.2.when"] = "risco >= 30"
    fields["scoring.priority.2.value"] = "P1"
    fields = step(client, fields, "scoring-up-rule:priority:2")
    fields = step(client, fields, "scoring-up-rule:priority:1")
    assert fields["scoring.priority.0.when"] == "risco >= 30"

    response = post(client, DRAFT, **fields)

    assert response.status_code == 303, text(response.text)
    saved = scoring_of(deps)
    assert saved["scores"][2] == {
        "name": "risco",
        "formula": "contagem(canais) * 10",
        "bands": {
            "key": "nivel_risco",
            "label": "rotulo_risco",
            "levels": [
                {"min": 30, "key": "alto", "label": "Risco alto"},
                {"key": "baixo", "label": "Risco baixo"},
            ],
        },
    }
    assert [rule.get("when") for rule in saved["priority"]] == [
        "risco >= 30",
        "porte >= 100",
        "porte >= 50",
        None,
    ]
    assert saved["priority"][0] == {"when": "risco >= 30", "value": "P1"}
    assert pending(client, SLUG)


def test_the_persona_result_is_a_text_or_the_answer_of_a_single_choice_question(
    client: TestClient, deps: Deps
) -> None:
    fields = editor(client, DRAFT)
    fields["scoring.persona.1.result"] = "answer"

    fields = step(client, fields, "scoring-refresh-rule:persona:1")

    assert "scoring.persona.1.value" not in fields  # o texto saiu: o resultado é uma resposta
    assert fields["scoring.persona.1.answer"] == ""  # a operadora escolhe a pergunta
    choices = form_options(client, fields, "scoring.persona.1.answer")
    assert choices == ["", "tamanho_equipe", "principal_desafio"]  # só as de escolha única
    fields["scoring.persona.1.answer"] = "principal_desafio"

    response = post(client, DRAFT, **fields)

    assert response.status_code == 303, text(response.text)
    assert scoring_of(deps)["persona"][1] == {"when": "porte >= 50", "answer": "principal_desafio"}
    page = form_of(client.get(DRAFT))
    assert page["scoring.persona.1.result"] == "answer"
    assert page["scoring.persona.1.answer"] == "principal_desafio"


def form_options(client: TestClient, fields: Fields, name: str) -> list[str]:
    """Os valores que a lista de escolhas do campo oferece, na página que a operação devolveu."""
    response = operate(client, fields, "scoring-refresh-rule:persona:1")
    found = re.search(
        rf'<select[^>]*name="{re.escape(name)}".*?</select>', response.text, re.DOTALL
    )
    assert found is not None, name
    return re.findall(r'<option value="([^"]*)"', found.group(0))


def test_the_priority_result_is_p1_p2_or_p3(client: TestClient) -> None:
    page = client.get(DRAFT)

    (select,) = [
        found
        for found in re.findall(
            r'<select[^>]*name="scoring\.priority\.0\.value".*?</select>', page.text, re.DOTALL
        )
    ]
    assert re.findall(r'<option value="([^"]*)"', select) == ["", "P1", "P2", "P3"]
    assert 'value="P1" selected' in select


def test_notes_are_removed_and_moved_and_the_new_order_is_saved(
    client: TestClient, deps: Deps
) -> None:
    fields = step(client, editor(client, DRAFT), "scoring-up-score:1")
    assert (fields["scoring.scores.0.name"], fields["scoring.scores.1.name"]) == (
        "alcance",
        "porte",
    )
    assert fields["scoring.scores.1.bands.levels.0.key"] == "grande"  # a nota leva as faixas

    response = post(client, DRAFT, **fields)

    assert response.status_code == 303, text(response.text)
    assert [score["name"] for score in scoring_of(deps)["scores"]] == ["alcance", "porte"]

    fields = step(client, editor(client, DRAFT), "scoring-down-score:0")
    assert fields["scoring.scores.0.name"] == "porte"
    fields = step(client, fields, "scoring-remove-score:0")  # `porte` sai
    assert fields["scoring.scores.0.name"] == "alcance" and "scoring.scores.1.name" not in fields


def test_a_note_other_things_read_cannot_be_saved_after_it_is_removed(
    client: TestClient, deps: Deps
) -> None:
    fields = step(client, editor(client, DRAFT), "scoring-remove-score:0")  # `porte`

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    shown = problems_shown(response)
    assert "porte" in shown and "não é uma nota nem uma pergunta de escolha" in shown
    assert stored(deps) == json.loads(DEFINITION)


def test_bands_and_levels_are_removed_and_moved(client: TestClient) -> None:
    levels = "scoring.scores.0.bands.levels"
    fields = step(client, editor(client, DRAFT), "scoring-down-level:0:0")
    assert [fields[f"{levels}.{n}.key"] for n in range(3)] == ["medio", "grande", "pequeno"]

    fields = step(client, fields, "scoring-up-level:0:2")
    assert [fields[f"{levels}.{n}.key"] for n in range(3)] == ["medio", "pequeno", "grande"]

    fields = step(client, fields, "scoring-remove-level:0:1")
    assert [fields[f"{levels}.{n}.key"] for n in range(2)] == ["medio", "grande"]
    assert f"{levels}.2.key" not in fields

    fields = step(client, fields, "scoring-remove-bands:0")
    assert "scoring.scores.0.bands.key" not in fields and f"{levels}.0.key" not in fields
    assert fields["scoring.scores.0.name"] == "porte"


def test_rules_are_removed_and_moved_and_the_order_is_the_one_saved(
    client: TestClient, deps: Deps
) -> None:
    fields = step(client, editor(client, DRAFT), "scoring-down-rule:persona:0")
    assert fields["scoring.persona.0.value"] == "Em crescimento"
    assert fields["scoring.persona.1.value"] == "Comercial"

    fields = step(client, fields, "scoring-remove-rule:persona:2")  # o "senão"
    assert "scoring.persona.2.value" not in fields

    response = post(client, DRAFT, **fields)

    assert response.status_code == 303, text(response.text)
    assert [rule["value"] for rule in scoring_of(deps)["persona"]] == [
        "Em crescimento",
        "Comercial",
    ]


def test_removing_every_rule_of_a_kind_saves_the_scoring_without_that_key(
    client: TestClient, deps: Deps
) -> None:
    fields = editor(client, DRAFT)
    for _ in range(3):
        fields = step(client, fields, "scoring-remove-rule:persona:0")
    assert "scoring.persona.0.value" not in fields

    response = post(client, DRAFT, **fields)

    assert response.status_code == 303, text(response.text)
    assert "persona" not in scoring_of(deps)


def test_the_first_item_up_or_the_last_down_changes_nothing(client: TestClient) -> None:
    fields = editor(client, DRAFT)

    for op in (
        "scoring-up-score:0",
        "scoring-down-score:1",
        "scoring-up-level:0:0",
        "scoring-down-level:0:2",
        "scoring-up-rule:priority:0",
        "scoring-down-rule:persona:2",
    ):
        assert step(client, fields, op) == fields, op


@pytest.mark.parametrize(
    "op",
    [
        "scoring-remove-score:9",
        "scoring-up-score:9",
        "scoring-add-bands:9",
        "scoring-remove-bands:1",  # a nota 1 não tem faixas
        "scoring-add-level:1",
        "scoring-remove-level:0:9",
        "scoring-remove-level:9:0",
        "scoring-up-level:0:9",
        "scoring-remove-rule:priority:9",
        "scoring-remove-rule:other:0",
        "scoring-add-rule:other",
        "scoring-refresh-rule:priority:0",  # a prioridade não tem o que trocar
        "scoring-refresh-rule:persona:9",
        "scoring-remove-score",
        "scoring-add-score:3",
        "scoring-remove-score:-1",
        "scoring-remove-score:../etc",
    ],
)
def test_an_operation_pointing_at_nothing_changes_nothing(client: TestClient, op: str) -> None:
    fields = editor(client, DRAFT)

    assert step(client, fields, op) == fields


def test_an_operation_needs_the_session_and_the_csrf_token(client: TestClient) -> None:
    fields = editor(client, DRAFT)
    anonymous = TestClient(client.app, follow_redirects=False)

    refused = [
        anonymous.post(EDIT, data={**fields, "op": "scoring-add-score", "csrf_token": "x"}),
        client.post(EDIT, data={**fields, "op": "scoring-add-score", "csrf_token": "errado"}),
    ]

    assert [response.status_code for response in refused] == [303, 403]


# --- os problemas do compilador no campo deles --------------------------------------------------


def break_formula(fields: Fields) -> None:
    fields["scoring.scores.1.formula"] = "contagem(canais) * "


def put_a_level_out_of_order(fields: Fields) -> None:
    fields["scoring.scores.0.bands.levels.1.min"] = "95"


def name_a_note_after_a_question(fields: Fields) -> None:
    fields["scoring.scores.1.name"] = "tamanho_equipe"


def reference_a_missing_name(fields: Fields) -> None:
    fields["scoring.priority.1.when"] = "fator_que_nao_existe >= 50"


def leave_a_rule_without_condition_before_the_last(fields: Fields) -> None:
    fields["scoring.priority.1.when"] = ""


def repeat_a_level_key(fields: Fields) -> None:
    fields["scoring.scores.0.bands.levels.1.key"] = "grande"


def point_the_persona_at_a_text_question(fields: Fields) -> None:
    fields["scoring.persona.1.result"] = "answer"
    fields["scoring.persona.1.answer"] = "canais"


def make_a_cycle(fields: Fields) -> None:
    fields["scoring.scores.0.formula"] = "alcance + 1"
    fields["scoring.scores.1.formula"] = "porte + 1"


def reject_every_example(fields: Fields) -> None:
    fields["scoring.priority.1.when"] = "porte >= 1000"  # o exemplo (50) bateria aqui
    fields["scoring.priority.2.when"] = "porte >= 1000"  # e o "senão" ganha uma condição


@pytest.mark.parametrize(
    ("change", "field", "problem"),
    [
        (break_formula, "scoring.scores.1.formula", "mas achei o fim da fórmula (coluna 19)"),
        (
            put_a_level_out_of_order,
            "scoring.scores.0.bands.levels.1.min",
            "os níveis vão do maior min para o menor: use menos de 90",
        ),
        (
            name_a_note_after_a_question,
            "scoring.scores.1.name",
            "tamanho_equipe já é o nome de uma pergunta",
        ),
        (
            reference_a_missing_name,
            "scoring.priority.1.when",
            "fator_que_nao_existe: não é uma nota nem uma pergunta de escolha",
        ),
        (
            leave_a_rule_without_condition_before_the_last,
            "scoring.priority.1",
            "esta regra não tem condição e sempre vale",
        ),
        (
            repeat_a_level_key,
            "scoring.scores.0.bands.levels.1.key",
            "outro nível da faixa já usa esta chave",
        ),
        (
            point_the_persona_at_a_text_question,
            "scoring.persona.1.answer",
            "precisa ser uma pergunta de escolha única",
        ),
        (make_a_cycle, "scoring.scores.0.formula", "referência circular: porte → alcance → porte"),
        (
            reject_every_example,
            "scoring.priority",
            "nenhuma regra de prioridade vale para as respostas de exemplo",
        ),
    ],
    ids=[
        "formula-column",
        "level-order",
        "note-name",
        "missing-name",
        "rule-without-condition",
        "level-key",
        "persona-answer",
        "cycle",
        "no-priority",
    ],
)
def test_a_problem_of_the_compiler_shows_on_its_field_and_in_the_list_at_the_top(
    client: TestClient, deps: Deps, change: Any, field: str, problem: str
) -> None:
    response = save(client, change, DRAFT)

    assert response.status_code == 422
    assert problem in error_on(response, field)
    assert problem in problems_shown(response)
    assert f'href="#f-{field.replace(".", "-")}"' in response.text  # o link leva até o campo
    assert "definition" not in invalid_fields(response)  # o JSON do resto não tem nada com isso
    assert stored(deps) == json.loads(DEFINITION)


def test_a_formula_problem_says_where_it_is_with_the_column(client: TestClient) -> None:
    response = save(client, break_formula, DRAFT)

    assert response.status_code == 422
    assert "scoring.scores.1.formula" in invalid_controls_by_name(response)
    list_at_the_top = problems_shown(response)
    assert "Pontuação · nota alcance · fórmula:" in list_at_the_top
    assert "coluna 19" in list_at_the_top  # o fim da fórmula, sem o espaço que sobrou
    assert form_fields(response.text, "editor-form")["scoring.scores.1.formula"] == (
        "contagem(canais) * "
    )  # o que a operadora escreveu fica no campo


def invalid_controls_by_name(response: httpx.Response) -> list[str]:
    found = [*tags(response.text, "input"), *tags(response.text, "textarea")]
    return [str(item["name"]) for item in found if item.get("aria-invalid") == "true"]


def test_the_boxes_with_a_problem_are_open_and_the_others_are_not(client: TestClient) -> None:
    response = save(client, put_a_level_out_of_order, DRAFT)

    opened = [found.get("id") for found in tags(response.text, "details") if "open" in found]
    assert "f-scoring-scores-0" in opened and "f-scoring-scores-1" not in opened


def test_a_note_without_a_name_or_formula_is_refused_with_the_problem_on_each_field(
    client: TestClient, deps: Deps
) -> None:
    fields = step(client, editor(client, DRAFT), "scoring-add-score")

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    assert error_on(response, "scoring.scores.2.name")  # a estrutura primeiro: sem nome, nada mais
    fields["scoring.scores.2.name"] = "risco"

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    assert "fórmula" in error_on(response, "scoring.scores.2.formula")
    assert stored(deps) == json.loads(DEFINITION)


def test_rules_without_any_note_are_refused_at_the_notes(client: TestClient) -> None:
    def keep_only_the_rules(fields: Fields) -> None:
        for name in [n for n in fields if n.startswith("scoring.scores.")]:
            del fields[name]
        fields["scoring.priority.0.when"] = 'tamanho_equipe == "so_eu"'

    response = save(client, keep_only_the_rules, DRAFT)

    assert response.status_code == 422
    assert "precisa de pelo menos 1 item" in error_on(response, "scoring.scores")


# --- a pontuação feita no editor, do painel à publicação ----------------------------------------


def test_a_note_a_band_and_a_rule_made_in_the_editor_reach_the_panel_the_test_and_the_publication(
    client: TestClient, deps: Deps, worker: TestTrigger, clock: FakeClock
) -> None:
    fields = step(client, editor(client, DRAFT), "scoring-add-score")
    fields["scoring.scores.2.name"] = "urgencia"
    fields["scoring.scores.2.formula"] = "arredondar_meio_para_cima(pontos(tamanho_equipe) / 2)"
    fields = step(client, fields, "scoring-add-bands:2")
    fields = step(client, fields, "scoring-add-level:2")
    levels = "scoring.scores.2.bands.levels"
    fields[f"{levels}.0.min"] = "40"
    fields[f"{levels}.0.key"] = "urgente"
    fields[f"{levels}.0.label"] = "Urgente"
    fields[f"{levels}.1.key"] = "calma"
    fields[f"{levels}.1.label"] = "Com calma"
    fields = step(client, fields, "scoring-add-rule:persona")
    fields["scoring.persona.2.when"] = "urgencia >= 40"
    fields["scoring.persona.2.value"] = "Apressada"
    fields = step(client, fields, "scoring-up-rule:persona:2")
    fields = step(client, fields, "scoring-up-rule:persona:1")
    fields = step(client, fields, "scoring-up-rule:persona:0")  # a primeira que bater

    assert post(client, DRAFT, **fields).status_code == 303

    # O painel ao lado mostra o que está salvo, com as respostas de exemplo (2 a 5 pessoas = 50).
    panel = text(section_of(client.get(DRAFT), "pontuacao"))
    assert "urgencia" in panel and "25" in panel  # 50 / 2
    assert "nivel_urgencia" in panel and "calma" in panel and "Com calma" in panel
    assert "Apressada" not in panel  # 25 < 40: a regra não bate

    # Outras respostas, no teste do rascunho: o painel do teste vê a regra nova decidir.
    other = {**DEMO_ANSWERS, "tamanho_equipe": "mais_de_15", "canais": ["a"]}
    scored = post(client, f"{DRAFT}/score", answers=json.dumps(other))
    assert scored.status_code == 200
    calculated = text(section_of(scored, "pontuacao-calculada"))
    assert "urgencia" in calculated and "50" in calculated  # 100 / 2
    assert "urgente" in calculated and "Urgente" in calculated
    assert "Apressada" in calculated and "regra 1" in calculated

    # A publicação nota a diferença, publica, e o webhook leva a nota nova.
    changes = text(section_of(client.get(PUBLISH), "mudancas"))
    assert "Pontuação" in changes and "urgencia" in changes
    published = post(
        client, PUBLISH, note=NOTE, expected=hidden(client.get(PUBLISH).text, "expected")
    )
    assert published.status_code == 303, text(published.text)
    assert deps.forms.current(SLUG).version_number == 2
    read = text(section_of(client.get(f"/admin/forms/{SLUG}"), "pontuacao"))
    assert "urgencia" in read and "Apressada" in read and "Urgente" in read
    assert not pending(client, SLUG)
