"""O editor das condições (`show_when`) de uma pergunta e de uma opção, pela costura HTTP
(TestClient), como o navegador o usa: a condição em campos (sem JSON), a ida e volta entre a
definição e a edição, adicionar e remover condição e regra sem JavaScript, os operadores e os
valores que cada pergunta aceita, os problemas ao lado da regra e do campo certos e o aviso de uma
pergunta renomeada ou removida. A definição e o avaliador estão em `test_form_conditions.py`."""

from __future__ import annotations

import copy
import html
import json
import re
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_admin import ORIGIN, FakeClock, csrf_token, login
from test_admin_destinations import ADMIN_ENV
from test_admin_drafts import EDITOR_FORM, Fields, TestTrigger, save
from test_admin_drafts import editor as editor_of
from test_admin_form_editor import HTMX, changed_lines, error_of, pending
from test_admin_form_editor import stored as stored_in
from test_admin_forms import import_form, section_of
from test_form_conditions import DEFINITION, DEFINITION_TEXT, SLUG, compiled, edited, rule_of
from test_public import form_fields, tags, text

from app.api.main import create_app
from app.deps import Deps
from app.forms.definition import DefinitionError
from app.forms.definition.changes import compare
from app.settings import Settings

DRAFT = f"/admin/forms/{SLUG}/draft"
EDIT = f"{DRAFT}/edit"

# As perguntas da definição de teste que os testes usam (etapa.pergunta):
SITUACAO = "steps.0.questions.3"  # escolha única; abre a árvore de condições
CLIENTES = "steps.0.questions.4"  # a condição `all` com uma regra `in` na situação
CARTEIRA = "steps.0.questions.5"  # a condição `any` com duas regras; a opção `zero` tem condição
ESCRITORIO = "steps.0.questions.6"  # texto, com condição
DOR = "steps.1.questions.0"  # escolha única, sem condição
DORES = "steps.1.questions.1"  # escolha múltipla, sem condição
OUTRA_DOR = "steps.1.questions.2"  # `any`: equals na dor e contains nas dores
RESULTADO = "steps.2.questions.0"  # sem condição, a última de escolha única


def logged_in(deps: Deps, definition: str) -> TestClient:
    """A operadora no admin, com o formulário importado (versão 1)."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]
    clock = FakeClock()

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, test_trigger=TestTrigger(deps, clock), clock=clock),
        follow_redirects=False,
    )
    assert login(current).status_code == 303
    assert import_form(current, definition).status_code == 303
    return current


@pytest.fixture
def client(deps: Deps) -> TestClient:
    return logged_in(deps, DEFINITION_TEXT)


def stored(deps: Deps) -> dict[str, Any]:
    return stored_in(deps, SLUG)


def editor(client: TestClient) -> Fields:
    """O formulário do rascunho do formulário de teste, como o navegador o mandaria sem mudança."""
    return editor_of(client, DRAFT)


def operate(client: TestClient, fields: Fields, op: str, *, htmx: bool = False) -> httpx.Response:
    """Um botão do editor, como o navegador o manda: o formulário inteiro com a operação."""
    headers = {**ORIGIN, **(HTMX if htmx else {})}
    data = {**fields, "csrf_token": csrf_token(client), "op": op}
    return client.post(EDIT, data=data, headers=headers)


def many(fields: Fields, name: str) -> list[str]:
    """Os valores de um campo que pode ter vários (as caixas marcadas)."""
    found = fields.get(name, [])
    return found if isinstance(found, list) else [found]


def rule(prefix: str, index: int = 0) -> str:
    return f"{prefix}.show_when.rules.{index}"


# --- a ida e volta entre a definição e a edição -------------------------------------------------


def test_the_editor_shows_a_condition_as_fields_and_no_json(client: TestClient) -> None:
    page = client.get(DRAFT).text
    fields = editor(client)

    assert fields[f"{CLIENTES}.show_when.mode"] == "all"
    assert fields[f"{rule(CLIENTES)}.field"] == "situacao"
    assert fields[f"{rule(CLIENTES)}.operator"] == "in"
    assert many(fields, f"{rule(CLIENTES)}.value") == ["clt_escritorio", "clt_fora", "estudante"]
    assert fields[f"{CARTEIRA}.show_when.mode"] == "any"
    assert fields[f"{rule(CARTEIRA, 1)}.field"] == "clientes_proprios"
    assert fields[f"{rule(CARTEIRA, 1)}.operator"] == "equals"
    assert fields[f"{rule(CARTEIRA, 1)}.value"] == "sim"
    option = f"{CARTEIRA}.options.0"  # a opção `zero`
    assert many(fields, f"{rule(option)}.value") == ["autonomo", "escritorio_proprio"]
    assert fields[f"{rule(OUTRA_DOR, 1)}.operator"] == "contains"
    assert fields[f"{rule(OUTRA_DOR, 1)}.value"] == "outra"
    assert not any(name.endswith(".show_when") for name in fields)  # nenhum JSON escondido
    assert 'name="steps.0.questions.4.show_when"' not in page


def test_saving_the_editor_unchanged_keeps_every_condition(client: TestClient, deps: Deps) -> None:
    response = save(client, draft=DRAFT)

    assert response.status_code == 303
    assert stored(deps)["steps"] == DEFINITION["steps"]
    assert not pending(client, SLUG)


def every_kind_of_rule() -> dict[str, Any]:
    """A definição de teste com uma regra de cada operador em cada tipo de pergunta, e a ordem
    das opções que a lista `in` guarda."""
    document = copy.deepcopy(DEFINITION)
    steps = document["steps"]
    steps[1]["questions"][3]["show_when"] = {  # pausa_7_dias
        "mode": "all",
        "rules": [
            {"field": "nome_escritorio", "operator": "equals", "value": "Escritório Ltda"},
            {"field": "nome_escritorio", "operator": "in", "value": ["Ltda", "ME", "Eireli"]},
            {"field": "nome_escritorio", "operator": "contains", "value": "Ltda "},
            {"field": "situacao", "operator": "equals", "value": "autonomo"},
            {"field": "dor_principal", "operator": "in", "value": ["retrabalho", "outra"]},
            {"field": "dores_adicionais", "operator": "in", "value": ["captacao"]},
            {"field": "dores_adicionais", "operator": "contains", "value": "retrabalho"},
        ],
    }
    steps[1]["questions"][3]["options"][0]["show_when"] = {
        "mode": "any",
        "rules": [{"field": "equipe", "operator": "equals", "value": "so_eu"}],
    }
    steps[2]["questions"][0]["show_when"] = {
        "mode": "any",
        "rules": [{"field": "pausa_7_dias", "operator": "in", "value": ["trava", "segue"]}],
    }
    return document


def test_a_definition_with_every_operator_goes_through_the_editor_and_back_unchanged(
    deps: Deps,
) -> None:
    document = every_kind_of_rule()
    client = logged_in(deps, json.dumps(document))

    response = save(client, draft=DRAFT)

    assert response.status_code == 303
    assert stored(deps)["steps"] == document["steps"]
    assert not pending(client, SLUG)


# --- incluir e remover condições e regras -------------------------------------------------------


def form_of(response: httpx.Response) -> Fields:
    assert response.status_code == 200, response.text
    return form_fields(response.text, EDITOR_FORM)


def select_of(page: str, name: str) -> list[tuple[str, str, bool]]:
    """As opções de uma lista da página: o valor, o texto e se está escolhida."""
    found = re.search(rf'<select[^>]*name="{re.escape(name)}"[^>]*>(.*?)</select>', page, re.DOTALL)
    assert found is not None, name
    options = re.findall(
        r'<option value="([^"]*)"( selected)?>(.*?)</option>', found.group(1), re.DOTALL
    )
    return [(value, text(html.unescape(label)), bool(chosen)) for value, chosen, label in options]


def autofocused(response: httpx.Response) -> list[str]:
    """Os campos e botões com `autofocus`: o nome do campo, ou o id do botão de operação."""
    found = [
        item
        for tag in ("input", "select", "textarea", "button")
        for item in tags(response.text, tag)
        if "autofocus" in item
    ]
    return [str(item["id"] if item.get("name") == "op" else item.get("name")) for item in found]


def test_adding_a_condition_brings_one_empty_rule_with_the_earlier_questions_and_saves_nothing(
    client: TestClient, deps: Deps
) -> None:
    response = operate(client, editor(client), "add-condition:1:0")

    after = form_of(response)
    first = rule(DOR)
    assert after[f"{DOR}.show_when.mode"] == "all"
    assert after[f"{first}.field"] == "" and after[f"{first}.operator"] == "equals"
    assert after[f"{first}.value"] == ""
    assert autofocused(response) == [f"{first}.field"]
    offered = select_of(response.text, f"{first}.field")  # só as perguntas anteriores, pelo rótulo
    assert [(value, label) for value, label, _ in offered] == [
        ("", "Escolha a pergunta"),
        ("nome", "Nome"),
        ("email", "E-mail"),
        ("whatsapp", "WhatsApp"),
        ("situacao", "Situação profissional"),
        ("clientes_proprios", "Atende clientes próprios?"),
        ("carteira", "Quantos clientes?"),
        ("nome_escritorio", "Nome do escritório"),
        ("equipe", "Quantas pessoas na equipe?"),
    ]
    assert "ainda não foram salvas" in text(response.text)
    assert not pending(client, SLUG) and stored(deps)["steps"] == DEFINITION["steps"]


def test_rules_are_added_and_removed_and_the_last_one_takes_the_condition_with_it(
    client: TestClient,
) -> None:
    added = form_of(operate(client, editor(client), "add-rule:0:4"))
    assert added[f"{rule(CLIENTES, 1)}.field"] == ""  # a regra nova, em branco, depois da que havia
    assert added[f"{rule(CLIENTES, 0)}.field"] == "situacao"

    removed = form_of(operate(client, editor(client), "remove-rule:0:5:0"))
    assert removed[f"{rule(CARTEIRA)}.field"] == "clientes_proprios"  # a segunda regra subiu
    assert f"{rule(CARTEIRA, 1)}.field" not in removed
    assert removed[f"{CARTEIRA}.show_when.mode"] == "any"

    last = operate(client, editor(client), "remove-rule:0:4:0")
    assert f"{CLIENTES}.show_when.mode" not in form_of(last)
    assert autofocused(last) == ["f-steps-0-questions-4-show_when-add_condition"]


def test_a_condition_is_taken_out_whole(client: TestClient) -> None:
    response = operate(client, editor(client), "remove-condition:0:5")

    after = form_of(response)
    assert not [name for name in after if name.startswith(f"{CARTEIRA}.show_when")]
    assert f"{CARTEIRA}.options.0.show_when.mode" in after  # a das opções fica
    assert autofocused(response) == ["f-steps-0-questions-5-show_when-add_condition"]
    assert "open" in tags(response.text, "details", id="f-steps-0-questions-5")[0]  # segue aberta


def test_the_condition_of_an_option_is_added_extended_and_removed_the_same_way(
    client: TestClient,
) -> None:
    option = f"{CARTEIRA}.options.1"  # `um_a_dez`, sem condição
    page = client.get(DRAFT).text
    button = tags(page, "button", id="f-steps-0-questions-5-options-1-show_when-add_condition")
    assert button and button[0]["value"] == "add-condition:0:5:1"  # só o botão, sem a caixa vazia
    assert "Mostrar esta opção quando…" in text(page)
    assert f"{option}.show_when.mode" not in page
    added = form_of(operate(client, editor(client), "add-condition:0:5:1"))
    assert added[f"{option}.show_when.mode"] == "all"
    assert added[f"{rule(option)}.field"] == ""

    more = form_of(operate(client, added, "add-rule:0:5:1"))
    assert more[f"{rule(option, 1)}.field"] == ""
    assert f"{rule(CARTEIRA, 2)}.field" not in more  # a condição da pergunta ficou como estava

    fewer = form_of(operate(client, more, "remove-rule:0:5:1:0"))
    assert f"{rule(option, 1)}.field" not in fewer and f"{rule(option)}.field" in fewer

    gone = form_of(operate(client, fewer, "remove-condition:0:5:1"))
    assert not [name for name in gone if name.startswith(f"{option}.show_when")]
    zero = form_of(
        operate(client, editor(client), "remove-rule:0:5:0:0")
    )  # a única regra de `zero`
    assert f"{CARTEIRA}.options.0.show_when.mode" not in zero


def test_a_condition_takes_at_most_twenty_rules(client: TestClient) -> None:
    fields = editor(client)
    for index in range(1, 20):
        fields[f"{rule(CLIENTES, index)}.field"] = "situacao"
        fields[f"{rule(CLIENTES, index)}.operator"] = "equals"
        fields[f"{rule(CLIENTES, index)}.value"] = "estudante"

    response = operate(client, fields, "add-rule:0:4")

    after = form_of(response)
    assert f"{rule(CLIENTES, 19)}.field" in after and f"{rule(CLIENTES, 20)}.field" not in after
    assert not tags(response.text, "button", id="f-steps-0-questions-4-show_when-add_rule")


def test_an_operation_that_points_to_nothing_changes_nothing(client: TestClient) -> None:
    fields = editor(client)
    for op in (
        "add-rule:1:0",  # a pergunta não tem condição
        "remove-rule:1:0:0",
        "remove-rule:0:4:5",  # a regra não existe
        "refresh-rule:0:4:9",
        "refresh-operator:1:0:0",
        "add-condition:9:9",
        "add-condition:0:4",  # a condição já existe
        "remove-condition:0:4:9",  # a opção não existe
        "add-rule:0:4:1:1:1",
    ):
        response = operate(client, fields, op)
        assert response.status_code == 200 and form_of(response) == fields, op


def test_with_htmx_only_the_steps_come_back(client: TestClient) -> None:
    response = operate(client, editor(client), "add-rule:0:4", htmx=True)

    assert response.status_code == 200 and "<html" not in response.text
    assert tags(response.text, "div", id="estrutura")
    assert tags(response.text, "select", name=f"{rule(CLIENTES, 1)}.field")
    assert not tags(response.text, "textarea", name="prompt")  # o resto da página fica


# --- o operador e o valor seguem a pergunta -----------------------------------------------------

FIRST = rule(RESULTADO)  # a regra que os testes montam na última pergunta de escolha única


def retargeted(
    client: TestClient,
    question: str,
    operator: str = "equals",
    value: str | list[str] = "",
    op: str = "refresh-rule:2:0:0",
) -> httpx.Response:
    """Uma regra nova na pergunta `resultado`, com o que a operadora escolheu, e a operação de
    desenhar a regra de novo (o `onchange` com o HTMX; o botão sem JavaScript)."""
    fields = form_of(operate(client, editor(client), "add-condition:2:0"))
    fields[f"{FIRST}.field"] = question
    fields[f"{FIRST}.operator"] = operator
    if value:
        fields[f"{FIRST}.value"] = value
    else:
        fields.pop(f"{FIRST}.value", None)
    return operate(client, fields, op)


def operators_offered(response: httpx.Response) -> list[tuple[str, str]]:
    return [(value, label) for value, label, _ in select_of(response.text, f"{FIRST}.operator")]


@pytest.mark.parametrize(
    ("question", "operators", "chosen"),
    [
        ("situacao", [("equals", "é"), ("in", "é uma destas")], "equals"),
        ("dores_adicionais", [("contains", "contém"), ("in", "inclui alguma destas")], "contains"),
        (
            "nome_escritorio",
            [("equals", "é"), ("in", "é uma destas"), ("contains", "contém")],
            "equals",
        ),
    ],
)
def test_only_the_operators_the_chosen_question_accepts_are_offered(
    client: TestClient, question: str, operators: list[tuple[str, str]], chosen: str
) -> None:
    response = retargeted(client, question)

    assert operators_offered(response) == operators
    assert form_of(response)[f"{FIRST}.operator"] == chosen
    assert autofocused(response) == [f"{FIRST}.field"]


def accepted_by_the_check(question: str, operator: str, value: str | list[str]) -> bool:
    """Se a conferência do núcleo aceita o operador numa regra sobre a pergunta (o valor escrito
    aqui é válido para ela)."""
    document = edited(
        (f"{RESULTADO}.show_when", rule_of(field=question, operator=operator, value=value))
    )
    try:
        compiled(document)
    except DefinitionError as error:
        return not any(problem.path.endswith(".operator") for problem in error.problems)
    return True


@pytest.mark.parametrize(
    ("question", "value"),
    [("situacao", "autonomo"), ("dores_adicionais", "outra"), ("nome_escritorio", "Ltda")],
)
def test_the_operators_offered_are_the_ones_the_core_check_accepts(
    client: TestClient, question: str, value: str
) -> None:
    offered = {operator for operator, _ in operators_offered(retargeted(client, question))}

    accepted = {
        operator
        for operator in ("equals", "in", "contains")
        if accepted_by_the_check(question, operator, [value] if operator == "in" else value)
    }
    assert offered == accepted


def test_a_choice_question_gives_a_list_of_its_options_for_the_value(client: TestClient) -> None:
    response = retargeted(client, "situacao", "equals", "autonomo")

    assert select_of(response.text, f"{FIRST}.value") == [
        ("", "Escolha a opção", False),
        ("clt_escritorio", "CLT em escritório", False),
        ("clt_fora", "CLT fora da contabilidade", False),
        ("estudante", "Estudante", False),
        ("autonomo", "Autônomo", True),
        ("escritorio_proprio", "Escritório próprio", False),
    ]


def test_a_list_of_options_for_the_in_operator_is_a_group_of_boxes(client: TestClient) -> None:
    response = retargeted(client, "situacao", "in", ["estudante", "autonomo"])

    boxes = tags(response.text, "input", type="checkbox", name=f"{FIRST}.value")
    assert [box["value"] for box in boxes] == [
        "clt_escritorio",
        "clt_fora",
        "estudante",
        "autonomo",
        "escritorio_proprio",
    ]
    assert [box["value"] for box in boxes if "checked" in box] == ["estudante", "autonomo"]
    assert many(form_of(response), f"{FIRST}.value") == ["estudante", "autonomo"]


def test_a_text_question_takes_a_text_or_one_value_per_line(client: TestClient) -> None:
    single = retargeted(client, "nome_escritorio", "equals", "Escritório Ltda")
    lines = retargeted(client, "nome_escritorio", "in", "Ltda\nME")

    assert tags(single.text, "input", type="text", name=f"{FIRST}.value")
    assert form_of(single)[f"{FIRST}.value"] == "Escritório Ltda"
    assert not tags(single.text, "textarea", name=f"{FIRST}.value")
    assert form_of(lines)[f"{FIRST}.value"] == "Ltda\nME"
    assert "Um valor por linha" in text(lines.text)


def test_changing_the_question_adjusts_the_operator_and_drops_the_values_it_does_not_have(
    client: TestClient,
) -> None:
    # `situacao` -> `dores_adicionais`: o `equals` não vale numa escolha múltipla
    choices = form_of(retargeted(client, "dores_adicionais", "equals", "autonomo"))
    assert (choices[f"{FIRST}.operator"], choices[f"{FIRST}.value"]) == ("contains", "")
    # os valores que a pergunta nova não tem saem, e o `in` fica
    listed = form_of(retargeted(client, "dores_adicionais", "in", ["autonomo", "outra"]))
    assert (listed[f"{FIRST}.operator"], many(listed, f"{FIRST}.value")) == ("in", ["outra"])
    # num texto, o `contains` vale e o valor segue como texto
    typed = form_of(retargeted(client, "nome_escritorio", "contains", "outra"))
    assert (typed[f"{FIRST}.operator"], typed[f"{FIRST}.value"]) == ("contains", "outra")
    # de uma escolha múltipla para uma única: o `contains` não vale mais
    single = form_of(retargeted(client, "situacao", "contains", "outra"))
    assert (single[f"{FIRST}.operator"], single[f"{FIRST}.value"]) == ("equals", "")


def test_changing_the_operator_gives_the_value_the_shape_of_the_new_one(
    client: TestClient,
) -> None:
    refresh = "refresh-operator:2:0:0"
    one = retargeted(client, "situacao", "equals", ["estudante", "autonomo"], op=refresh)
    assert form_of(one)[f"{FIRST}.value"] == "estudante"  # um só fora do `in`
    assert autofocused(one) == [f"{FIRST}.operator"]
    listed = form_of(retargeted(client, "situacao", "in", "autonomo", op=refresh))
    assert many(listed, f"{FIRST}.value") == ["autonomo"]  # o `in` guarda a lista
    lines = form_of(retargeted(client, "nome_escritorio", "equals", "Ltda\nME", op=refresh))
    assert lines[f"{FIRST}.value"] == "Ltda"


def test_the_rule_gets_a_refresh_button_for_browsers_without_javascript(
    client: TestClient,
) -> None:
    page = retargeted(client, "situacao").text

    assert "refresh-rule:2:0:0" in page and "refresh-operator:2:0:0" in page  # com o HTMX
    assert "Mostrar os valores da regra 1" in text(page)  # sem JavaScript


# --- o que o salvar grava -----------------------------------------------------------------------


def test_a_condition_built_in_the_editor_is_saved_in_the_definition_format(
    client: TestClient, deps: Deps
) -> None:
    fields = form_of(operate(client, editor(client), "add-condition:2:0"))
    fields[f"{RESULTADO}.show_when.mode"] = "any"
    fields[f"{FIRST}.field"] = "situacao"
    fields[f"{FIRST}.operator"] = "in"
    fields[f"{FIRST}.value"] = ["estudante", "autonomo"]
    fields = form_of(operate(client, fields, "add-rule:2:0"))
    second = rule(RESULTADO, 1)
    fields[f"{second}.field"] = "clientes_proprios"
    fields[f"{second}.operator"] = "equals"
    fields[f"{second}.value"] = "sim"

    response = client.post(DRAFT, data={**fields, "csrf_token": csrf_token(client)}, headers=ORIGIN)

    assert response.status_code == 303
    assert stored(deps)["steps"][2]["questions"][0]["show_when"] == {
        "mode": "any",
        "rules": [
            {"field": "situacao", "operator": "in", "value": ["estudante", "autonomo"]},
            {"field": "clientes_proprios", "operator": "equals", "value": "sim"},
        ],
    }
    assert pending(client, SLUG)


def test_text_values_lose_the_spaces_at_the_edges_except_in_contains(
    client: TestClient, deps: Deps
) -> None:
    def typed(fields: Fields) -> None:
        fields[f"{RESULTADO}.show_when.mode"] = "all"
        for index, (operator, value) in enumerate(
            [
                ("in", " Ltda \r\n\r\n  ME\r\n"),
                ("equals", "  Escritório Ltda "),
                ("contains", " Ltda "),
            ]
        ):
            fields[f"{rule(RESULTADO, index)}.field"] = "nome_escritorio"
            fields[f"{rule(RESULTADO, index)}.operator"] = operator
            fields[f"{rule(RESULTADO, index)}.value"] = value

    assert save(client, typed, draft=DRAFT).status_code == 303

    assert stored(deps)["steps"][2]["questions"][0]["show_when"]["rules"] == [
        {"field": "nome_escritorio", "operator": "in", "value": ["Ltda", "ME"]},
        {"field": "nome_escritorio", "operator": "equals", "value": "Escritório Ltda"},
        {"field": "nome_escritorio", "operator": "contains", "value": " Ltda "},
    ]


def test_a_list_over_the_options_of_a_question_comes_back_in_the_order_of_the_options(
    deps: Deps,
) -> None:
    document = copy.deepcopy(DEFINITION)
    document["steps"][0]["questions"][4]["show_when"]["rules"][0]["value"] = [
        "estudante",
        "clt_fora",
    ]
    client = logged_in(deps, json.dumps(document))

    assert save(client, draft=DRAFT).status_code == 303

    rule_ = stored(deps)["steps"][0]["questions"][4]["show_when"]["rules"][0]
    assert rule_["value"] == ["clt_fora", "estudante"]  # a mesma regra: `in` não tem ordem


# --- os problemas ao lado da regra e do campo certos --------------------------------------------


def broken(owner: str, **rule_fields: str | list[str]) -> Callable[[Fields], None]:
    """Uma condição escrita à mão no formulário, no dono (`steps.2.questions.0` ou a opção)."""

    def change(fields: Fields) -> None:
        fields[f"{owner}.show_when.mode"] = "all"
        for key, value in {"field": "", "operator": "equals", "value": "x", **rule_fields}.items():
            fields[f"{owner}.show_when.rules.0.{key}"] = value

    return change


PROBLEMS = [
    pytest.param(
        RESULTADO,
        {"field": "nao_existe"},
        "field",
        "não há pergunta com este nome",
        "Etapa 3 · pergunta 1 · condição · regra 1 · pergunta",
        id="a question that does not exist",
    ),
    pytest.param(
        SITUACAO,
        {"field": "equipe", "value": "so_eu"},
        "field",
        "uma regra só olha respostas anteriores",
        "Etapa 1 · pergunta 4 · condição · regra 1 · pergunta",
        id="a question that comes later",
    ),
    pytest.param(
        RESULTADO,
        {"field": "situacao", "operator": "contains", "value": "autonomo"},
        "operator",
        "contains serve em texto e em escolha múltipla",
        "Etapa 3 · pergunta 1 · condição · regra 1 · operador",
        id="an operator the question does not accept",
    ),
    pytest.param(
        RESULTADO,
        {"field": "situacao", "value": "nao_existe"},
        "value",
        "«nao_existe» não é uma opção da pergunta situacao",
        "Etapa 3 · pergunta 1 · condição · regra 1 · valor",
        id="a value that is not an option",
    ),
    pytest.param(
        RESULTADO,
        {"field": "situacao", "operator": "in", "value": ["autonomo", "zzz"]},
        "value",
        "«zzz» não é uma opção da pergunta situacao",
        "Etapa 3 · pergunta 1 · condição · regra 1 · valor",
        id="a value in a list that is not an option",
    ),
    pytest.param(
        RESULTADO,
        {"field": "situacao", "value": ""},
        "value",
        "não pode ficar vazio",
        "Etapa 3 · pergunta 1 · condição · regra 1 · valor",
        id="a value left blank",
    ),
    pytest.param(
        RESULTADO,
        {"field": "nome_escritorio", "operator": "in", "value": ""},
        "value",
        "informe pelo menos um valor",
        "Etapa 3 · pergunta 1 · condição · regra 1 · valor",
        id="a list left empty",
    ),
    pytest.param(
        RESULTADO,
        {"field": ""},
        "field",
        "escolha a pergunta que a regra olha",
        "Etapa 3 · pergunta 1 · condição · regra 1 · pergunta",
        id="no question chosen",
    ),
    pytest.param(
        f"{CARTEIRA}.options.1",
        {"field": "nao_existe"},
        "field",
        "não há pergunta com este nome",
        "Etapa 1 · pergunta 6 · opção 2 · condição · regra 1 · pergunta",
        id="a question that does not exist, in an option",
    ),
    pytest.param(
        f"{CARTEIRA}.options.1",
        {"field": "equipe", "value": "so_eu"},
        "field",
        "uma regra só olha respostas anteriores",
        "Etapa 1 · pergunta 6 · opção 2 · condição · regra 1 · pergunta",
        id="a question that comes later, in an option",
    ),
]


@pytest.mark.parametrize(("owner", "rule_fields", "part", "message", "where"), PROBLEMS)
def test_a_problem_of_the_check_shows_next_to_its_rule_and_field(
    client: TestClient,
    deps: Deps,
    owner: str,
    rule_fields: dict[str, str | list[str]],
    part: str,
    message: str,
    where: str,
) -> None:
    response = save(client, broken(owner, **rule_fields), draft=DRAFT)

    assert response.status_code == 422
    name = f"{owner}.show_when.rules.0.{part}"
    assert message in error_of(response, name)
    assert where in text(section_of(response, "problemas"))
    question = ".".join(owner.split(".")[:4])
    assert "open" in tags(response.text, "details", id=f"f-{question.replace('.', '-')}")[0]
    assert stored(deps)["steps"] == DEFINITION["steps"]  # nada foi salvo


def test_the_condition_of_the_lead_identity_is_refused_on_its_condition(client: TestClient) -> None:
    email = "steps.0.questions.1"
    response = save(client, broken(email, field="nome"), draft=DRAFT)

    assert response.status_code == 422
    assert "o e-mail da lead a identifica e sempre aparece" in error_of(
        response, f"{email}.show_when"
    )
    assert "Etapa 1 · pergunta 2 · condição" in text(section_of(response, "problemas"))


def test_an_operator_chosen_for_another_kind_of_question_stays_as_the_operator_wrote_it(
    client: TestClient,
) -> None:
    # sem JavaScript, a operadora troca a pergunta e salva sem "Mostrar os valores": nada é trocado
    # sem ela saber, e o problema aparece no operador que ela deixou
    response = save(
        client, broken(RESULTADO, field="situacao", operator="contains", value="autonomo"), DRAFT
    )

    assert response.status_code == 422
    chosen = [
        label
        for value, label, selected in select_of(response.text, f"{FIRST}.operator")
        if selected
    ]
    assert chosen == ["contém (não serve nesta pergunta)"]


# --- uma pergunta renomeada, movida ou removida -------------------------------------------------

REVIEW = "condicoes-a-revisar"


def test_a_form_that_is_fine_has_nothing_to_review(client: TestClient) -> None:
    assert REVIEW not in client.get(DRAFT).text


def test_a_renamed_question_warns_where_it_was_used_and_keeps_what_the_rule_pointed_to(
    client: TestClient, deps: Deps
) -> None:
    fields = editor(client)
    fields[f"{SITUACAO}.name"] = "situacao_profissional"

    response = operate(client, fields, "refresh-question:0:3")  # qualquer operação do editor

    review = text(section_of(response, REVIEW))
    assert "Não há mais uma pergunta com o nome «situacao»: ela foi renomeada ou removida" in review
    assert "Etapa 1 · pergunta 5 · regra 1" in review  # clientes_proprios
    assert "Etapa 1 · pergunta 6 · regra 1" in review  # carteira
    assert "Etapa 1 · pergunta 6 · opção 1 · regra 1" in review  # a opção `zero` da carteira
    # a regra mantém a pergunta que tinha, marcada, em vez de trocar por outra
    assert ("situacao", "«situacao» (não existe mais)", True) in select_of(
        response.text, f"{rule(CLIENTES)}.field"
    )
    # a pergunta que depende da renomeada abre, mesmo sem problema salvo
    assert "open" in tags(response.text, "details", id="f-steps-0-questions-4")[0]
    assert "foi renomeada ou removida" in text(response.text.split('id="f-steps-0-questions-4"')[1])

    saved = client.post(
        DRAFT, data={**form_of(response), "csrf_token": csrf_token(client)}, headers=ORIGIN
    )
    assert saved.status_code == 422
    assert "não há pergunta com este nome" in error_of(saved, f"{rule(CLIENTES)}.field")
    assert ("situacao", "«situacao» (não existe mais)", True) in select_of(
        saved.text, f"{rule(CLIENTES)}.field"
    )
    assert stored(deps)["steps"] == DEFINITION["steps"]


def test_a_removed_question_warns_in_the_rules_that_looked_at_it(client: TestClient) -> None:
    response = operate(client, editor(client), "remove-question:0:3")

    review = text(section_of(response, REVIEW))
    assert "Não há mais uma pergunta com o nome «situacao»" in review
    assert "Etapa 1 · pergunta 4 · regra 1" in review  # clientes_proprios subiu uma posição


def test_a_question_moved_after_the_rule_that_looks_at_it_warns(client: TestClient) -> None:
    response = operate(client, editor(client), "down-question:0:3")  # `situacao` desce uma

    review = text(section_of(response, REVIEW))
    assert "A pergunta «Situação profissional» agora é esta ou vem depois dela" in review
    assert "uma regra só olha perguntas anteriores" in review
    assert ("situacao", "Situação profissional (vem depois)", True) in select_of(
        response.text, f"{rule('steps.0.questions.3')}.field"
    )


def test_a_renamed_option_warns_in_the_rules_that_used_its_value(
    client: TestClient, deps: Deps
) -> None:
    fields = editor(client)
    fields[f"{SITUACAO}.options.3.value"] = "autonomo_pj"  # era `autonomo`

    response = operate(client, fields, "refresh-question:0:3")

    review = text(section_of(response, REVIEW))
    assert "«autonomo» não é mais uma opção de «Situação profissional»" in review
    assert "Etapa 1 · pergunta 6 · regra 1" in review  # carteira: autonomo e escritorio_proprio
    boxes = tags(response.text, "input", type="checkbox", name=f"{rule(CARTEIRA)}.value")
    assert [box["value"] for box in boxes if "checked" in box] == [
        "escritorio_proprio",
        "autonomo",
    ]
    saved = client.post(
        DRAFT, data={**form_of(response), "csrf_token": csrf_token(client)}, headers=ORIGIN
    )
    assert saved.status_code == 422
    assert "«autonomo» não é uma opção da pergunta situacao" in error_of(
        saved, f"{rule(CARTEIRA)}.value"
    )
    assert "Etapa 1 · pergunta 6 · condição · regra 1 · valor" in text(
        section_of(saved, "problemas")
    )
    assert stored(deps)["steps"] == DEFINITION["steps"]


# --- a condição em linguagem natural ------------------------------------------------------------


def summary_of(page: str, question: str) -> str:
    """O resumo (o que se vê com a pergunta fechada) da pergunta no editor."""
    found = re.search(
        rf'<details[^>]*id="f-{question.replace(".", "-")}"[^>]*>\s*<summary>(.*?)</summary>',
        page,
        re.DOTALL,
    )
    assert found is not None, question
    return text(html.unescape(found.group(1)))


def test_the_summary_of_a_conditional_question_says_when_it_appears(client: TestClient) -> None:
    page = client.get(DRAFT).text

    assert (
        "Aparece quando Situação profissional é uma destas: CLT em escritório, "
        "CLT fora da contabilidade, Estudante"
    ) in summary_of(page, CLIENTES)
    assert (
        "Aparece quando qualquer uma destas regras vale: Situação profissional é uma destas: "
        "Autônomo, Escritório próprio; Atende clientes próprios? é Sim"
    ) in summary_of(page, CARTEIRA)
    assert "1 opção com condição" in summary_of(page, CARTEIRA)  # a opção `zero`
    assert (
        "Aparece quando qualquer uma destas regras vale: O que mais pesa? é Outra situação; "
        "Outras situações contém Outra situação"
    ) in summary_of(page, OUTRA_DOR)
    assert "Aparece quando" not in summary_of(page, DOR)  # sem condição, nada a dizer


def test_the_sentence_names_texts_between_quotes_and_the_multiple_choice_by_what_it_does(
    deps: Deps,
) -> None:
    client = logged_in(deps, json.dumps(every_kind_of_rule()))

    pausa = summary_of(client.get(DRAFT).text, "steps.1.questions.3")

    assert "Aparece quando todas estas regras valem: " in pausa
    assert "Nome do escritório é “Escritório Ltda”;" in pausa
    assert "Nome do escritório é uma destas: “Ltda”, “ME”, “Eireli”;" in pausa
    assert "Nome do escritório contém “Ltda ”;" in pausa
    assert "O que mais pesa? é uma destas: Retrabalho, Outra situação;" in pausa
    assert "Outras situações inclui alguma destas: Captação;" in pausa
    assert "Outras situações contém Retrabalho" in pausa


def test_the_summary_follows_the_page_while_the_operator_edits(client: TestClient) -> None:
    fields = editor(client)
    fields[f"{rule(CLIENTES)}.value"] = ["estudante"]
    fields[f"{SITUACAO}.options.2.label"] = "Estudante de contábeis"

    response = operate(client, fields, "refresh-question:0:4")

    assert (
        "Aparece quando Situação profissional é uma destas: Estudante de contábeis"
        in summary_of(response.text, CLIENTES)
    )


def test_a_new_rule_has_no_summary_until_it_says_something(client: TestClient) -> None:
    """Escolher a pergunta e o valor de uma regra nova não manda nada ao servidor até a próxima
    operação: o resumo não pode mostrar "é …" no lugar do valor. Sem a regra completa ele fica em
    branco, e volta com a frase quando a regra está completa."""
    response = operate(client, editor(client), "add-condition:1:0")  # a dor principal
    assert "Aparece quando" not in summary_of(response.text, DOR)  # a regra ainda sem pergunta

    fields = form_of(response)
    fields[f"{rule(DOR)}.field"] = "situacao"
    response = operate(client, fields, "refresh-rule:1:0:0")
    assert "Aparece quando" not in summary_of(response.text, DOR)  # a pergunta, sem valor
    assert "…" not in summary_of(response.text, DOR)

    fields = form_of(response)
    fields[f"{rule(DOR)}.value"] = "autonomo"
    response = operate(client, fields, "refresh-rule:1:0:0")
    assert summary_of(response.text, DOR).endswith(
        "Aparece quando Situação profissional é Autônomo"
    )


def test_the_reading_of_the_form_says_when_each_question_and_option_appears(
    client: TestClient,
) -> None:
    page = text(client.get(f"/admin/forms/{SLUG}").text)

    assert (
        "Aparece quando Situação profissional é uma destas: CLT em escritório, "
        "CLT fora da contabilidade, Estudante"
    ) in page
    # a opção «Nenhum» da pergunta «Quantos clientes?»
    assert (
        "zero Nenhum · Aparece quando Situação profissional é uma destas: Autônomo, "
        "Escritório próprio"
    ) in page
    # a pergunta sem condição não ganha frase
    assert page.count("Aparece quando") == 7


def requirement_of(page: str, name: str) -> str:
    """A linha "tipo · obrigatória" da pergunta na leitura do formulário."""
    found = re.search(
        rf'<span class="mono">{name}</span></p>\s*<p class="muted">(.*?)</p>', page, re.DOTALL
    )
    assert found is not None, name
    return text(html.unescape(found.group(1)))


def test_the_reading_says_a_required_question_with_a_condition_is_required_when_it_appears(
    client: TestClient,
) -> None:
    """O modelo das respostas só exige a pergunta com condição quando ela aparece, e a leitura não
    pode chamá-la de opcional por isso."""
    page = client.get(f"/admin/forms/{SLUG}").text

    for name in ("clientes_proprios", "carteira", "equipe"):  # obrigatórias, com condição
        assert requirement_of(page, name).endswith("obrigatória quando aparece"), name
    nome = requirement_of(page, "nome")  # obrigatória, sem condição: aparece sempre
    assert nome.startswith("texto · obrigatória · ") and "quando aparece" not in nome
    assert " · opcional" in requirement_of(page, "nome_escritorio")  # opcional, com condição
    assert " · opcional" in requirement_of(page, "whatsapp")


def test_the_comparison_says_a_condition_in_words_not_in_json(client: TestClient) -> None:
    def another_answer(fields: Fields) -> None:
        fields[f"{rule(CARTEIRA, 1)}.value"] = "nao"  # a segunda regra olhava o "sim"

    assert save(client, another_answer, draft=DRAFT).status_code == 303

    page = client.get(f"/admin/forms/{SLUG}/versions/compare", params={"to": "rascunho"})

    steps = section_of(page, "etapas")
    sentence = (
        "Aparece quando qualquer uma destas regras vale: Situação profissional é uma destas: "
        "Autônomo, Escritório próprio; Atende clientes próprios? é "
    )
    lines = changed_lines(steps)
    assert [line for line in lines if line.startswith("-") and sentence + "Sim" in line]
    assert [line for line in lines if line.startswith("+") and sentence + "Não" in line]
    assert len(lines) == 2
    assert '"rules"' not in steps and '"operator"' not in steps  # nem no contexto do diff


def test_the_comparison_says_the_condition_of_an_option_in_words_too(client: TestClient) -> None:
    def fewer_situations(fields: Fields) -> None:
        # A opção «Nenhum» da carteira aparecia para autônomo e escritório próprio.
        fields[f"{rule(f'{CARTEIRA}.options.0')}.value"] = ["autonomo"]

    assert save(client, fewer_situations, draft=DRAFT).status_code == 303

    page = client.get(f"/admin/forms/{SLUG}/versions/compare", params={"to": "rascunho"})

    steps = section_of(page, "etapas")
    sentence = "Aparece quando Situação profissional é uma destas: Autônomo"
    lines = changed_lines(steps)
    assert [line for line in lines if line.startswith("-") and sentence + ", Escritório" in line]
    added = [line for line in lines if line.startswith("+")]
    assert len(added) == 1 and sentence in added[0] and "Escritório" not in added[0]
    assert len(lines) == 2
    assert '"rules"' not in steps and '"operator"' not in steps  # nem no contexto do diff


def test_a_condition_the_sentence_cannot_say_stays_as_json_in_the_comparison() -> None:
    """Uma versão antiga pode guardar uma regra que o formato de hoje não lê (sem pergunta): a
    comparação mostra a condição como está, em vez de esconder a mudança."""
    question = {
        "name": "pausa",
        "kind": "text",
        "label": "Pausa",
        "max_length": 10,
        "show_when": {"mode": "all", "rules": [{"field": "", "operator": "equals", "value": "x"}]},
    }

    sections = compare({}, {"steps": [{"title": "A", "questions": [question]}]})

    (steps,) = [section for section in sections if section.key == "etapas"]
    shown = "\n".join(line.text for line in steps.lines)
    assert '"show_when": {' in shown and "Aparece quando" not in shown


# --- acessibilidade -----------------------------------------------------------------------------


def test_every_field_of_the_condition_editor_has_a_label(client: TestClient) -> None:
    page = client.get(DRAFT).text
    labelled = {str(label["for"]) for label in tags(page, "label") if label.get("for")}
    controls = [*tags(page, "input"), *tags(page, "select"), *tags(page, "textarea")]
    named = [
        control
        for control in controls
        if ".show_when." in str(control.get("name"))
        and control.get("type") not in ("hidden", "radio", "checkbox")
    ]
    assert len(named) > 20  # o modo, a pergunta, o operador e o valor de cada regra
    for control in named:
        assert control.get("id") in labelled or control.get("aria-label"), control["name"]
    # as caixas de uma lista de opções ficam num grupo com legenda, cada uma dentro do rótulo dela
    group = re.search(
        r'<fieldset class="edit-values"[^>]*>\s*<legend>(.*?)</legend>.*?</fieldset>',
        page,
        re.DOTALL,
    )
    assert group is not None and "Valores da regra 1" in group.group(1)
    assert group.group(0).count("<label") == group.group(0).count('type="checkbox"') > 0
