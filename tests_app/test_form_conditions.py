"""Perguntas e opções condicionais (`show_when`) na definição: o avaliador contra os casos que a
página pública resolve igual, a conferência ao salvar e publicar, o modelo das respostas (com a
pontuação e o prompt) e a impressão digital do que vai ao LLM. A gravação, o webhook, o envio pela
página e o editor estão em `test_form_conditions_flow.py`. Os valores esperados são escritos à mão
nas fixtures (`conditions_cases.json`) e nos testes."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.forms import FormSpec
from app.forms.definition import DefinitionError, compile_definition
from app.forms.definition.changes import llm_changes, llm_parts
from app.forms.definition.conditions import Conditions
from app.forms.definition.document import StepDoc

FIXTURES = Path(__file__).parent / "fixtures"
CASES = json.loads((FIXTURES / "conditions_cases.json").read_text(encoding="utf-8"))
DEFINITION_TEXT = (FIXTURES / "conditions_definition.json").read_text(encoding="utf-8")
DEFINITION: dict[str, Any] = json.loads(DEFINITION_TEXT)
SLUG = DEFINITION["slug"]
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
UNAVAILABLE = "esta opção não está disponível para as respostas anteriores"
CONSENT = {"privacy": True, "text_version": DEFINITION["consent"]["version"]}


def compiled(document: dict[str, Any] | None = None) -> FormSpec:
    return compile_definition(DEFINITION if document is None else document, clock=lambda: NOW)


def edited(*changes: tuple[str, Any]) -> dict[str, Any]:
    """Uma cópia da definição com mudanças por caminho (`steps.0.questions.5.show_when`)."""
    document = copy.deepcopy(DEFINITION)
    for path, value in changes:
        *parents, last = path.split(".")
        target: Any = document
        for key in parents:
            target = target[int(key)] if isinstance(target, list) else target[key]
        target[int(last) if isinstance(target, list) else last] = value
    return document


def problems_of(document: dict[str, Any]) -> list[tuple[str, str]]:
    with pytest.raises(DefinitionError) as raised:
        compiled(document)
    return [(problem.path, problem.message) for problem in raised.value.problems]


# --- o avaliador ------------------------------------------------------------------------------


def _as_step(questions: list[dict[str, Any]]) -> StepDoc:
    """As perguntas das fixtures (só o que as condições leem) como uma etapa da definição."""
    completed = []
    for question in questions:
        found = {**question, "label": question["name"]}
        if question["kind"] in ("text", "textarea"):
            found["max_length"] = 100
        found["options"] = [{**o, "label": o["value"]} for o in question.get("options", ())] or None
        completed.append({k: v for k, v in found.items() if v is not None})
    return StepDoc.model_validate({"title": "Etapa", "questions": completed})


EVALUATOR = Conditions([_as_step(CASES["questions"])])
NAMES = [question["name"] for question in CASES["questions"]]


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda case: case["name"])
def test_the_evaluator_resolves_the_cases_shared_with_the_public_page(case: dict[str, Any]) -> None:
    seen = EVALUATOR.evaluate(case["answers"])

    assert list(seen.visible) == case["visible"]
    assert {name: list(values) for name, values in seen.options.items()} == case["options"]
    assert list(seen.hidden) == [name for name in NAMES if name not in case["visible"]]


def test_the_evaluator_reads_validated_answers_with_none_for_the_unanswered() -> None:
    seen = EVALUATOR.evaluate(
        {"situacao": "clt", "clientes": None, "carteira": None, "dores": None}
    )

    assert list(seen.visible) == ["situacao", "clientes", "nome", "dores", "canal"]


def test_answers_of_the_wrong_type_count_as_not_answered() -> None:
    seen = EVALUATOR.evaluate({"situacao": 5, "nome": {"a": 1}, "dores": [1, None], "canal": True})

    assert list(seen.visible) == ["situacao", "nome", "dores", "canal"]


def test_a_form_without_conditions_has_nothing_to_evaluate() -> None:
    demo = compile_definition((FIXTURES / "demo_definition.json").read_text(encoding="utf-8"))

    assert demo.conditions is not None and not demo.conditions.present
    assert EVALUATOR.present


def test_the_page_data_has_the_format_of_the_shared_cases() -> None:
    """A página recebe as perguntas como a fixture as escreve: é o que o avaliador do navegador lê."""
    assert EVALUATOR.page_data == CASES["questions"]
    assert json.loads(json.dumps(EVALUATOR.page_data)) == CASES["questions"]


def test_the_page_data_of_a_compiled_form_keeps_the_order_the_conditions_and_the_options() -> None:
    data = compiled().conditions.page_data  # type: ignore[union-attr]

    names = [question["name"] for question in data]
    assert names[:6] == ["nome", "email", "whatsapp", "situacao", "clientes_proprios", "carteira"]
    carteira = next(question for question in data if question["name"] == "carteira")
    assert carteira["show_when"]["mode"] == "any"
    assert [option["value"] for option in carteira["options"]][:2] == ["zero", "um_a_dez"]
    assert carteira["options"][0]["show_when"]["rules"][0]["field"] == "situacao"
    assert "show_when" not in carteira["options"][1]
    nome = next(question for question in data if question["name"] == "nome")
    assert nome == {"name": "nome", "kind": "text"}  # só o que o avaliador usa


def test_a_required_question_with_a_condition_is_required_when_it_shows_up() -> None:
    conditions = compiled().conditions
    assert conditions is not None

    # carteira e equipe são obrigatórias com condição; nome_escritorio tem condição e é opcional.
    assert {"carteira", "equipe"} <= conditions.required_when_shown
    assert "nome_escritorio" not in conditions.required_when_shown
    assert "nome" not in conditions.required_when_shown  # obrigatória sem condição: sempre aparece


# --- a conferência ----------------------------------------------------------------------------

# O caminho da pergunta da definição de teste que cada caso muda (etapa 0, pergunta 5: `carteira`;
# etapa 1, pergunta 2: `outra_dor`).
CARTEIRA = "steps.0.questions.5"
OUTRA_DOR = "steps.1.questions.2"
RULE = {"field": "situacao", "operator": "equals", "value": "autonomo"}


def rule_of(**changes: Any) -> dict[str, Any]:
    return {"mode": "all", "rules": [{**RULE, **changes}]}


@pytest.mark.parametrize(
    ("path", "condition", "where", "message"),
    [
        (
            CARTEIRA,
            rule_of(field="nao_existe"),
            f"{CARTEIRA}.show_when.rules.0.field",
            "não há pergunta com este nome",
        ),
        (
            "steps.0.questions.3",  # `situacao` olhando a que vem depois dela
            rule_of(field="equipe"),
            "steps.0.questions.3.show_when.rules.0.field",
            "uma regra só olha respostas anteriores",
        ),
        (
            CARTEIRA,
            rule_of(field="carteira", value="zero"),  # a própria pergunta
            f"{CARTEIRA}.show_when.rules.0.field",
            "uma regra só olha respostas anteriores",
        ),
        (
            CARTEIRA,
            rule_of(operator="in"),  # in pede uma lista
            f"{CARTEIRA}.show_when.rules.0.value",
            "o operador in compara com uma lista de valores",
        ),
        (
            CARTEIRA,
            rule_of(value=["autonomo"]),  # equals pede um texto
            f"{CARTEIRA}.show_when.rules.0.value",
            "o operador equals compara com um texto",
        ),
        (
            CARTEIRA,
            rule_of(operator="contains"),  # situacao é escolha única
            f"{CARTEIRA}.show_when.rules.0.operator",
            "contains serve em texto e em escolha múltipla",
        ),
        (
            OUTRA_DOR,
            {
                "mode": "all",
                "rules": [{"field": "dores_adicionais", "operator": "equals", "value": "outra"}],
            },
            f"{OUTRA_DOR}.show_when.rules.0.operator",
            "equals não serve numa escolha múltipla",
        ),
        (
            CARTEIRA,
            rule_of(value="outro"),  # não é uma opção de situacao
            f"{CARTEIRA}.show_when.rules.0.value",
            "«outro» não é uma opção da pergunta situacao",
        ),
        (
            CARTEIRA,
            rule_of(operator="in", value=["autonomo", "outro"]),
            f"{CARTEIRA}.show_when.rules.0.value.1",
            "«outro» não é uma opção da pergunta situacao",
        ),
        (
            CARTEIRA,
            rule_of(field="nome", value=" Ana"),  # o texto é comparado sem espaços nas pontas
            f"{CARTEIRA}.show_when.rules.0.value",
            "tire os espaços do começo e do fim",
        ),
        (
            "steps.0.questions.1",  # o e-mail da identidade
            rule_of(),
            "steps.0.questions.1.show_when",
            "o e-mail da lead a identifica e sempre aparece",
        ),
        (
            "steps.0.questions.0",  # o nome da identidade
            rule_of(field="situacao"),
            "steps.0.questions.0.show_when",
            "o nome da lead a identifica e sempre aparece",
        ),
        (
            "steps.0.questions.2",  # o telefone da identidade
            rule_of(field="situacao"),
            "steps.0.questions.2.show_when",
            "o telefone da lead a identifica e sempre aparece",
        ),
    ],
)
def test_a_condition_the_definition_cannot_keep_is_refused_with_the_path_of_the_field(
    path: str, condition: dict[str, Any], where: str, message: str
) -> None:
    found = problems_of(edited((f"{path}.show_when", condition)))

    assert any(p == where and message in m for p, m in found), found


def test_the_rules_of_an_option_look_at_earlier_questions_too() -> None:
    zero = f"{CARTEIRA}.options.0.show_when"
    found = problems_of(
        edited((zero, {"mode": "all", "rules": [{**RULE, "field": "equipe", "value": "so_eu"}]}))
    )

    assert (
        f"{zero}.rules.0.field",
        "a pergunta é esta ou vem depois dela: uma regra só olha respostas anteriores",
    ) in found


def test_every_problem_of_the_conditions_comes_back_together() -> None:
    found = problems_of(
        edited(
            (f"{CARTEIRA}.show_when", rule_of(field="nao_existe")),
            (f"{OUTRA_DOR}.show_when", rule_of(value="outro")),
        )
    )

    assert [path for path, _ in found] == [
        f"{CARTEIRA}.show_when.rules.0.field",
        f"{OUTRA_DOR}.show_when.rules.0.value",
    ]


@pytest.mark.parametrize(
    ("condition", "where", "message"),
    [
        ({"mode": "xor", "rules": [RULE]}, "mode", "use um destes valores"),
        ({"mode": "all", "rules": []}, "rules", "precisa de pelo menos 1 item"),
        ({"mode": "all"}, "rules", "campo obrigatório"),
        (
            {"mode": "all", "rules": [{**RULE, "operator": "starts"}]},
            "rules.0.operator",
            "use um destes valores",
        ),
        (
            {"mode": "all", "rules": [{**RULE, "value": ""}]},
            "rules.0.value",
            "não pode ficar vazio",
        ),
        (
            {"mode": "all", "rules": [{**RULE, "value": "  "}]},
            "rules.0.value",
            "não pode ficar vazio",
        ),
        (
            {"mode": "all", "rules": [{**RULE, "value": []}]},
            "rules.0.value",
            "informe pelo menos um valor",
        ),
        (
            {"mode": "all", "rules": [{**RULE, "value": 5}]},
            "rules.0.value",
            "use um texto ou uma lista de textos",
        ),
        ({"mode": "all", "rules": [{**RULE, "value": ["a", 5]}]}, "rules.0.value", "use só textos"),
        ({"mode": "all", "rules": [{**RULE, "extra": 1}]}, "rules.0.extra", "campo desconhecido"),
        (
            {"mode": "all", "rules": [{**RULE, "field": "Situacao"}]},
            "rules.0.field",
            "use letras minúsculas",
        ),
        ({"mode": "all", "rules": [RULE] * 21}, "rules", "aceita no máximo 20 itens"),
        ({"mode": "all", "rules": [RULE], "extra": 1}, "extra", "campo desconhecido"),
        ("sempre", "", "precisa ser um objeto"),
    ],
)
def test_the_shape_of_a_condition_is_checked_before_anything_else(
    condition: Any, where: str, message: str
) -> None:
    found = problems_of(edited((f"{CARTEIRA}.show_when", condition)))

    prefix = f"{CARTEIRA}.show_when"
    assert [(p, m) for p, m in found if p == (f"{prefix}.{where}" if where else prefix)] != []
    assert any(message in m for _, m in found), found


def test_the_definition_of_the_tests_and_its_chain_of_conditions_compile() -> None:
    spec = compiled()

    assert spec.conditions is not None and spec.conditions.present


# --- o modelo das respostas -------------------------------------------------------------------

EMPLOYEE = {
    "nome": "Contadora Exemplo",
    "email": "contadora@example.test",
    "situacao": "clt_escritorio",
    "clientes_proprios": "sim",
    "carteira": "um_a_dez",
    "equipe": "so_eu",
    "dor_principal": "retrabalho",
    "resultado": "rotina",
}
SELF_EMPLOYED = {
    "nome": "Contadora Exemplo",
    "email": "contadora@example.test",
    "situacao": "autonomo",
    "carteira": "zero",
    "dor_principal": "retrabalho",
    "resultado": "rotina",
}


def invalid(answers: dict[str, Any]) -> list[tuple[tuple[Any, ...], str, str]]:
    """Os erros do modelo: (campo, tipo, mensagem do validador, se houver)."""
    with pytest.raises(ValidationError) as raised:
        compiled().answers_model.model_validate(answers)
    return [
        (tuple(e["loc"]), e["type"], str((e.get("ctx") or {}).get("error", "")))
        for e in raised.value.errors(include_input=False, include_url=False)
    ]


def test_the_answer_of_a_hidden_question_is_discarded() -> None:
    spec = compiled()

    answers = spec.answers_model.model_validate(
        {**EMPLOYEE, "clientes_proprios": "nao", "pausa_7_dias": "trava"}
    )

    # `nao` esconde a carteira, e a carteira escondida esconde a equipe e a pausa de 7 dias.
    assert (answers.carteira, answers.equipe, answers.pausa_7_dias) == (None, None, None)  # type: ignore[attr-defined]


def test_a_question_of_a_hidden_branch_is_not_demanded_but_one_that_appeared_is() -> None:
    spec = compiled()

    spec.answers_model.model_validate(SELF_EMPLOYED)  # clientes_proprios não aparece
    assert invalid({k: v for k, v in EMPLOYEE.items() if k != "clientes_proprios"}) == [
        (("clientes_proprios",), "missing", "")
    ]
    assert invalid({**EMPLOYEE, "clientes_proprios": "  "}) == [
        (("clientes_proprios",), "missing", "")
    ]
    assert invalid({**EMPLOYEE, "clientes_proprios": None}) == [
        (("clientes_proprios",), "missing", "")
    ]


def test_a_required_question_that_appears_in_a_chain_is_demanded_too() -> None:
    assert invalid({**SELF_EMPLOYED, "carteira": "um_a_dez"}) == [(("equipe",), "missing", "")]


def test_an_optional_conditional_question_that_appears_may_stay_blank() -> None:
    answers = compiled().answers_model.model_validate(
        {**SELF_EMPLOYED, "carteira": "um_a_dez", "equipe": "so_eu"}
    )

    assert answers.nome_escritorio is None and answers.pausa_7_dias is None  # type: ignore[attr-defined]


def test_a_hidden_option_cannot_be_chosen_and_the_refusal_names_the_field() -> None:
    assert invalid({**EMPLOYEE, "carteira": "zero"}) == [
        (("carteira",), "value_error", UNAVAILABLE)
    ]
    spec = compiled()
    assert spec.answers_model.model_validate(SELF_EMPLOYED).carteira == "zero"  # type: ignore[attr-defined]


def test_a_hidden_option_among_the_marked_ones_is_refused_as_well() -> None:
    document = edited(
        (
            "steps.1.questions.1.options.1.show_when",
            {
                "mode": "all",
                "rules": [{"field": "situacao", "operator": "equals", "value": "autonomo"}],
            },
        )
    )

    spec = compiled(document)
    with pytest.raises(ValidationError) as raised:
        spec.answers_model.model_validate(
            {**EMPLOYEE, "dores_adicionais": ["retrabalho", "captacao"]}
        )

    assert [(tuple(e["loc"]), e["type"]) for e in raised.value.errors()] == [
        (("dores_adicionais",), "value_error")
    ]
    ok = spec.answers_model.model_validate({**EMPLOYEE, "dores_adicionais": ["retrabalho"]})
    assert ok.dores_adicionais == ["retrabalho"]  # type: ignore[attr-defined]


def test_an_option_that_does_not_exist_is_still_the_ordinary_error() -> None:
    assert invalid({**EMPLOYEE, "carteira": "nenhuma"}) == [(("carteira",), "literal_error", "")]


def test_the_errors_of_every_field_come_back_together() -> None:
    errors = invalid({**EMPLOYEE, "carteira": "zero", "email": "sem-arroba", "equipe": None})

    assert [loc for loc, _, _ in errors] == [("email",), ("carteira",)]


def test_an_answer_not_in_the_definition_is_still_refused_but_a_hidden_one_is_known() -> None:
    assert [loc for loc, _, _ in invalid({**EMPLOYEE, "cpf": "x"})] == [("cpf",)]
    compiled().answers_model.model_validate({**SELF_EMPLOYED, "clientes_proprios": "sim"})


def test_answers_that_are_not_an_object_get_the_ordinary_error() -> None:
    with pytest.raises(ValidationError):
        compiled().answers_model.model_validate(["clt"])


def test_the_scoring_reads_a_hidden_question_as_not_answered() -> None:
    spec = compiled()
    assert spec.score is not None

    full = spec.score(spec.answers_model.model_validate(EMPLOYEE))
    hidden = spec.score(spec.answers_model.model_validate({**EMPLOYEE, "clientes_proprios": "nao"}))

    # carteira (1) + equipe (0) pontos; escondidas valem 0 ponto, e a persona segue a situação.
    assert (full.scores["porte"], full.priority) == (1, "P3")
    assert (hidden.scores["porte"], hidden.priority, hidden.persona) == (0, "P3", "clt_escritorio")
    big = spec.score(
        spec.answers_model.model_validate(
            {**EMPLOYEE, "carteira": "mais_de_trinta", "equipe": "quatro_mais"}
        )
    )
    assert (big.scores["porte"], big.priority, big.persona) == (9, "P1", "carteira grande")


def test_the_prompt_reads_a_hidden_question_as_none() -> None:
    spec = compiled()

    prompt = spec.render_prompt(
        spec.answers_model.model_validate({**EMPLOYEE, "clientes_proprios": "nao"})
    )

    assert "Atende clientes próprios: Não" in prompt
    assert "Carteira: não perguntado" in prompt and "Equipe: não perguntado" in prompt


# --- a impressão digital do que vai ao LLM ----------------------------------------------------


def test_a_change_in_a_condition_counts_as_a_change_that_goes_to_the_llm() -> None:
    moved = edited((f"{CARTEIRA}.show_when", rule_of(value="escritorio_proprio")))
    option = edited((f"{CARTEIRA}.options.1.show_when", rule_of(value="escritorio_proprio")))

    assert llm_changes(DEFINITION, moved) == ("as condições das perguntas e das opções",)
    # O prompt cita o rótulo da carteira, e as opções dela levam a condição: as duas partes mudam.
    assert "as condições das perguntas e das opções" in llm_changes(DEFINITION, option)
    assert llm_changes(DEFINITION, copy.deepcopy(DEFINITION)) == ()


def test_the_llm_parts_of_a_form_without_conditions_stay_as_they_were() -> None:
    demo = json.loads((FIXTURES / "demo_definition.json").read_text(encoding="utf-8"))

    assert "conditions" not in llm_parts(demo)
    assert "conditions" in llm_parts(DEFINITION)
