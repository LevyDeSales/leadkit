"""O modelo do plano e os dados do formulário na definição, pela costura do compilador: as
validações declarativas do plano (expressões proibidas, padrão proibido, mínimo de parágrafos), as
opções tiradas de uma lista dos dados e os blocos de dados com tipo (listas, janelas e edição).
O plano de exemplo e os templates passam pelo mesmo modelo da própria versão."""

from __future__ import annotations

import copy
import json
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from conftest import DEMO_ANSWERS
from pydantic import ValidationError
from test_form_definition import answers_of, changed, compiled

from app.adapters.openai_llm import repair_prompt, repair_reasons, strict_schema
from app.forms import FormSpec
from app.forms.definition import DefinitionError, compile_definition
from app.forms.definition.changes import llm_changes, llm_fingerprint

PLAN: dict[str, Any] = {
    "fields": [
        {"name": "titulo", "kind": "text", "min_length": 3, "max_length": 120},
        {"name": "resumo", "kind": "text", "max_length": 1200},
        {
            "name": "fases",
            "kind": "list",
            "min_items": 1,
            "max_items": 3,
            "items": {
                "kind": "group",
                "fields": [
                    {"name": "nome", "kind": "text", "max_length": 60},
                    {"name": "foco", "kind": "text", "max_length": 600},
                ],
            },
        },
        {"name": "acoes", "kind": "list", "items": {"kind": "text", "max_length": 220}},
    ]
}
EXAMPLE: dict[str, Any] = {
    "titulo": "Carteira organizada",
    "resumo": "O escritório tem demanda, mas não acompanha números.",
    "fases": [{"nome": "Base", "foco": "Organizar a carteira."}],
    "acoes": ["Listar os clientes ativos."],
}


def with_plan(plan: dict[str, Any], example: dict[str, Any] | None = None, **more: Any) -> Any:
    """A definição do demo com o plano dado e o exemplo que passa por ele."""
    return changed(
        {
            "plan": plan,
            "examples.plan": EXAMPLE if example is None else example,
            "pdf.html": "<h1>{{ plan.titulo }}</h1>",
            "preview": "<h2>{{ plan.titulo }}</h2>",
            **more,
        }
    )


def rules(*items: dict[str, Any]) -> dict[str, Any]:
    return {**PLAN, "forbidden": list(items)}


def planned(plan: dict[str, Any], **more: Any) -> FormSpec:
    return compiled(with_plan(plan, **more))


def plan_with(**changes: Any) -> dict[str, Any]:
    return {**EXAMPLE, **changes}


def refusals(spec: FormSpec, plan: dict[str, Any]) -> list[tuple[str, str]]:
    """Onde o plano é recusado e por quê, como a rodada de reparo os lê: caminho e mensagem."""
    try:
        spec.validate_plan(json.dumps(plan), answers_of(spec, DEMO_ANSWERS))
    except ValidationError as exc:
        return [
            (".".join(str(part) for part in item["loc"]), item["msg"])
            for item in exc.errors(include_input=False)
        ]
    return []


def problems_of(document: dict[str, Any]) -> list[str]:
    """Os problemas da definição, como `caminho: mensagem`."""
    with pytest.raises(DefinitionError) as raised:
        compile_definition(document)
    return [f"{problem.path}: {problem.message}" for problem in raised.value.problems]


# --- expressões proibidas -----------------------------------------------------------------------

PROMISE = {"expressions": ["economia garantida", "única IA do mercado"]}


def test_a_forbidden_expression_is_refused_in_every_text_of_the_plan() -> None:
    spec = planned(rules(PROMISE))

    assert refusals(spec, EXAMPLE) == []
    assert refusals(spec, plan_with(titulo="Plano de economia garantida")) == [
        ("titulo", "contém a expressão proibida: economia garantida")
    ]
    assert refusals(spec, plan_with(fases=[{"nome": "Base", "foco": "A única IA do mercado."}]))
    assert refusals(spec, plan_with(acoes=["Sem rodeios: ECONOMIA  garantida."])) == [
        ("acoes.0", "contém a expressão proibida: economia garantida")
    ]


def test_the_expression_matches_without_regard_to_case_accents_or_spacing() -> None:
    spec = planned(rules({"expressions": ["única IA"]}))

    assert refusals(spec, plan_with(resumo="Somos a UNICA   ia que ajuda."))
    assert refusals(spec, plan_with(resumo="Somos a única\nIA que ajuda."))
    assert not refusals(spec, plan_with(resumo="Somos uma IA única que ajuda."))


def test_every_forbidden_expression_found_in_a_field_is_named_once() -> None:
    spec = planned(rules(PROMISE))

    found = refusals(spec, plan_with(resumo="Economia garantida, a única IA do mercado."))

    assert found == [
        (
            "resumo",
            "contém as expressões proibidas: economia garantida, única IA do mercado",
        )
    ]


def test_forbidden_expressions_can_be_limited_to_chosen_fields() -> None:
    spec = planned(rules({**PROMISE, "scope": "only", "fields": ["resumo", "fases.foco"]}))

    assert refusals(spec, plan_with(titulo="Economia garantida")) == []
    assert refusals(spec, plan_with(resumo="Economia garantida")) == [
        ("resumo", "contém a expressão proibida: economia garantida")
    ]
    focus = [{"nome": "Base", "foco": "Economia garantida."}]
    assert [path for path, _ in refusals(spec, plan_with(fases=focus))] == ["fases.0.foco"]


def test_forbidden_expressions_can_skip_chosen_fields() -> None:
    spec = planned(rules({**PROMISE, "scope": "except", "fields": ["titulo"]}))

    assert refusals(spec, plan_with(titulo="Economia garantida")) == []
    assert [path for path, _ in refusals(spec, plan_with(resumo="Economia garantida"))] == [
        "resumo"
    ]
    assert [path for path, _ in refusals(spec, plan_with(acoes=["Economia garantida"]))] == [
        "acoes.0"
    ]


def test_several_rules_add_up() -> None:
    spec = planned(
        rules(
            {"expressions": ["economia garantida"], "scope": "only", "fields": ["titulo"]},
            {"expressions": ["sem juros"], "scope": "except", "fields": ["titulo"]},
        )
    )

    assert refusals(spec, plan_with(titulo="Sem juros")) == []
    assert refusals(spec, plan_with(titulo="Economia garantida"))
    assert refusals(spec, plan_with(resumo="Sem juros para você"))
    assert refusals(spec, plan_with(resumo="Economia garantida")) == []


def test_the_repair_round_gets_the_position_and_the_rule_never_the_text() -> None:
    spec = planned(rules(PROMISE))
    secret = "Contadora Exemplo prometeu economia garantida."

    with pytest.raises(ValidationError) as raised:
        spec.validate_plan(json.dumps(plan_with(resumo=secret)), answers_of(spec, DEMO_ANSWERS))

    prompt = repair_prompt(raised.value)
    assert "- resumo: contém a expressão proibida: economia garantida" in prompt
    assert secret not in prompt and "Contadora Exemplo" not in prompt
    assert repair_reasons(raised.value, spec.plan_model) == ["resumo:forbidden_expression"]


# --- padrão proibido e parágrafos ---------------------------------------------------------------

NO_DIGITS = {"pattern": r"\d", "message": "O nome da fase é um tema, sem números."}


def with_field(plan: dict[str, Any], path: tuple[int | str, ...], **changes: Any) -> dict[str, Any]:
    """Uma cópia do plano com mudanças no nó do caminho (`(2, "items", "fields", 0)`)."""
    found = copy.deepcopy(plan)
    node: Any = found["fields"]
    for step in path:
        node = node[step]
    node.update(changes)
    return found


def test_a_forbidden_pattern_refuses_the_matching_text_of_that_field_only() -> None:
    plan = with_field(PLAN, (2, "items", "fields", 0), forbidden_pattern=NO_DIGITS)
    spec = planned(plan)

    assert refusals(spec, plan_with(fases=[{"nome": "Fase 2", "foco": "Foco 2"}])) == [
        ("fases.0.nome", "O nome da fase é um tema, sem números.")
    ]
    assert refusals(spec, plan_with(fases=[{"nome": "Base", "foco": "Foco 2"}])) == []
    assert refusals(spec, plan_with(titulo="Plano de 90 dias")) == []


def test_a_forbidden_pattern_ignores_case_and_has_a_default_message() -> None:
    plan = with_field(PLAN, (0,), forbidden_pattern={"pattern": r"\bdias?\b"})
    spec = planned(plan, example=plan_with(titulo="Carteira organizada"))

    assert [path for path, _ in refusals(spec, plan_with(titulo="Plano de DIAS"))] == ["titulo"]
    [(_, message)] = refusals(spec, plan_with(titulo="Plano de dias"))
    assert message == "contém um trecho que este campo não aceita"


def test_a_forbidden_pattern_also_guards_the_items_of_a_list() -> None:
    plan = with_field(
        PLAN, (3, "items"), forbidden_pattern={"pattern": r"R\$", "message": "Sem valores."}
    )
    spec = planned(plan)

    assert refusals(spec, plan_with(acoes=["Cobrar R$ 500 por mês."])) == [
        ("acoes.0", "Sem valores.")
    ]


# O padrão é escrito no admin e roda sobre o texto do LLM, que a lead influencia pelas respostas: um
# padrão com backtracking catastrófico não pode prender o processo do worker.
SLOW_TEXT = "a" * 50 + "b"
GAVE_UP = "o padrão proibido deste campo demorou demais para conferir o texto: simplifique-o"


class Hung(Exception):
    """Do alarme de `within`. Não é `AssertionError` nem `ValueError`, que o pydantic converteria
    em recusa do plano dentro do validador."""


@contextmanager
def within(seconds: int) -> Iterator[None]:
    """Reprova o teste, em vez de travá-lo, se o bloco passar de `seconds`: o `re` confere sinais
    enquanto casa, então o alarme também interrompe um padrão catastrófico."""

    def expired(signum: int, frame: Any) -> None:
        raise Hung

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    except Hung:
        pytest.fail(f"o bloco passou de {seconds}s: o padrão travou o processo")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_the_textbook_catastrophic_pattern_does_not_hang_the_plan_check() -> None:
    plan = with_field(PLAN, (1,), forbidden_pattern={"pattern": r"(a+)+$"})
    spec = planned(plan)

    with within(10):
        refusals(spec, plan_with(resumo=SLOW_TEXT))


def test_a_forbidden_pattern_that_takes_too_long_refuses_the_text_and_says_why() -> None:
    plan = with_field(PLAN, (1,), forbidden_pattern={"pattern": r"(a|aa)+$"})
    spec = planned(plan)

    with within(10):
        found = refusals(spec, plan_with(resumo=SLOW_TEXT))

    assert found == [("resumo", GAVE_UP)]
    assert refusals(spec, plan_with(resumo="Texto curto.")) == []


def test_the_repair_round_names_the_slow_pattern_and_never_the_text() -> None:
    plan = with_field(PLAN, (1,), forbidden_pattern={"pattern": r"(a|aa)+$"})
    spec = planned(plan)

    with within(10), pytest.raises(ValidationError) as raised:
        spec.validate_plan(json.dumps(plan_with(resumo=SLOW_TEXT)), answers_of(spec, DEMO_ANSWERS))

    assert f"- resumo: {GAVE_UP}" in repair_prompt(raised.value)
    assert SLOW_TEXT not in repair_prompt(raised.value)
    assert repair_reasons(raised.value, spec.plan_model) == ["resumo:forbidden_pattern_timeout"]


def test_a_slow_pattern_is_refused_when_the_example_plan_trips_it() -> None:
    plan = with_field(PLAN, (1,), forbidden_pattern={"pattern": r"(a|aa)+$"})

    with within(10):
        problems = problems_of(with_plan(plan, plan_with(resumo=SLOW_TEXT)))

    assert f"examples.plan.resumo: {GAVE_UP}" in problems


def test_a_minimum_of_paragraphs_is_refused_and_written_in_the_description() -> None:
    plan = with_field(PLAN, (1,), min_paragraphs=3, description="A mensagem final")
    spec = planned(plan, example=plan_with(resumo="Um.\n\nDois.\n\nTrês."))
    three = "Um.\n\nDois.\n  \nTrês."

    assert refusals(spec, plan_with(resumo=three)) == []
    assert refusals(spec, plan_with(resumo="Um parágrafo só.\nCom duas linhas.")) == [
        ("resumo", "escreva pelo menos 3 parágrafos, separados por linha em branco")
    ]
    assert refusals(spec, plan_with(resumo="Um.\n\n\n\nDois.")) != []
    description = strict_schema(spec.plan_model)["properties"]["resumo"]["description"]
    assert (
        description
        == "A mensagem final. Até 1200 caracteres. Pelo menos 3 parágrafos, separados por linha em branco."
    )


def test_one_paragraph_is_asked_in_the_singular() -> None:
    plan = with_field(PLAN, (1,), min_paragraphs=1)
    spec = planned(plan)

    assert strict_schema(spec.plan_model)["properties"]["resumo"]["description"].endswith(
        "Pelo menos 1 parágrafo."
    )
    assert refusals(spec, plan_with(resumo="   ")) != []


# --- o plano de exemplo passa pelo modelo da própria versão -------------------------------------


def test_the_example_plan_is_checked_by_the_declarative_rules_of_its_own_version() -> None:
    document = with_plan(rules(PROMISE), plan_with(titulo="Plano de economia garantida"))

    assert problems_of(document) == [
        "examples.plan.titulo: contém a expressão proibida: economia garantida"
    ]


def test_the_example_plan_must_follow_the_minimum_of_paragraphs_and_the_pattern() -> None:
    plan = with_field(
        with_field(PLAN, (1,), min_paragraphs=2),
        (2, "items", "fields", 0),
        forbidden_pattern=NO_DIGITS,
    )
    example = plan_with(fases=[{"nome": "Fase 2", "foco": "Foco."}])

    assert problems_of(with_plan(plan, example)) == [
        "examples.plan.resumo: escreva pelo menos 2 parágrafos, separados por linha em branco",
        "examples.plan.fases.0.nome: O nome da fase é um tema, sem números.",
    ]


# --- a conferência da definição -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        (
            rules({"expressions": ["a"], "scope": "only"}),
            "plan.forbidden.0.fields: escolha ao menos um campo",
        ),
        (
            rules({"expressions": ["a"], "scope": "all", "fields": ["titulo"]}),
            "plan.forbidden.0.fields: a regra vale para o plano inteiro: tire os campos ou mude o escopo",
        ),
        (
            rules({"expressions": ["a"], "scope": "except", "fields": ["fases"]}),
            "plan.forbidden.0.fields.0: não é um campo de texto do plano",
        ),
        (
            rules({"expressions": ["a"], "scope": "only", "fields": ["nao_existe"]}),
            "plan.forbidden.0.fields.0: não é um campo de texto do plano",
        ),
        (
            rules({"expressions": ["a"], "scope": "only", "fields": ["titulo", "titulo"]}),
            "plan.forbidden.0.fields.1: campo repetido na regra",
        ),
        (rules({"expressions": []}), "plan.forbidden.0.expressions: precisa de pelo menos 1 item"),
        (
            rules({"expressions": ["a", "A "]}),
            "plan.forbidden.0.expressions.1: expressão repetida na regra",
        ),
        (
            rules({"expressions": ["a"], "scope": "tudo"}),
            "plan.forbidden.0.scope: use um destes valores: 'all', 'only' ou 'except'",
        ),
        (
            with_field(PLAN, (0,), forbidden_pattern={"pattern": "(aberto"}),
            "plan.fields.0.forbidden_pattern.pattern: expressão regular inválida (posição 7)",
        ),
        (
            with_field(PLAN, (2,), forbidden_pattern={"pattern": "x"}),
            "plan.fields.2.forbidden_pattern: não vale para campos do tipo list",
        ),
        (
            with_field(PLAN, (2,), min_paragraphs=2),
            "plan.fields.2.min_paragraphs: não vale para campos do tipo list",
        ),
        (
            with_field(PLAN, (0,), values_from="aulas"),
            "plan.fields.0.values_from: não vale para campos do tipo text",
        ),
    ],
    ids=[
        "only-without-fields",
        "all-with-fields",
        "list-is-not-text",
        "unknown-field",
        "repeated-field",
        "no-expression",
        "repeated-expression",
        "unknown-scope",
        "broken-regex",
        "pattern-on-list",
        "paragraphs-on-list",
        "list-source-on-text",
    ],
)
def test_declarative_rules_are_checked_when_the_definition_is_compiled(
    plan: dict[str, Any], expected: str
) -> None:
    assert expected in problems_of(with_plan(plan))


def test_the_regex_of_a_forbidden_pattern_has_a_size_ceiling() -> None:
    plan = with_field(PLAN, (0,), forbidden_pattern={"pattern": "a" * 301})

    assert any("pattern" in problem for problem in problems_of(with_plan(plan)))


# --- rótulos dos campos do plano ----------------------------------------------------------------


def labelled_plan() -> dict[str, Any]:
    """O plano com rótulo (com acento) em três níveis: um campo de texto, um campo dentro dos itens
    de uma lista de grupos e uma lista de textos."""
    plan = copy.deepcopy(PLAN)
    plan["fields"][0]["label"] = "Título do plano"
    plan["fields"][2]["items"]["fields"][1]["label"] = "Foco da fase"
    plan["fields"][3]["label"] = "Ações"
    return plan


def test_a_plan_field_may_have_a_label_by_the_path_of_its_name() -> None:
    spec = planned(labelled_plan())

    assert spec.plan_labels == {
        "titulo": "Título do plano",
        "fases.foco": "Foco da fase",
        "acoes": "Ações",
    }


def test_a_plan_without_labels_has_none_and_keeps_the_name_of_the_field() -> None:
    assert planned(PLAN).plan_labels == {}


def test_the_label_is_for_the_admin_only_and_never_reaches_the_llm_or_the_plan() -> None:
    spec = planned(labelled_plan())

    assert "Título do plano" not in json.dumps(strict_schema(spec.plan_model), ensure_ascii=False)
    assert "Foco da fase" not in json.dumps(spec.plan_model.model_json_schema(), ensure_ascii=False)
    validated = spec.validate_plan(json.dumps(EXAMPLE), answers_of(spec, DEMO_ANSWERS))
    assert set(validated.model_dump()) == set(EXAMPLE)


def test_labelling_a_field_leaves_the_fingerprint_of_what_goes_to_the_llm_as_it_was() -> None:
    plain, labelled = with_plan(PLAN), with_plan(labelled_plan())

    assert llm_fingerprint(plain) == llm_fingerprint(labelled)
    assert llm_changes(plain, labelled) == ()


def test_the_items_of_a_list_take_no_label_because_the_list_field_has_it() -> None:
    plan = copy.deepcopy(PLAN)
    plan["fields"][3]["items"]["label"] = "Cada ação"

    assert "plan.fields.3.items.label: dê o rótulo ao campo da lista, não aos itens" in problems_of(
        with_plan(plan)
    )


# --- opções tiradas de uma lista dos dados ------------------------------------------------------

LESSONS = ["MÓDULO 1 — A1: Boas Vindas", "MÓDULO 2 — A1: Proposta de Valor", "MÓDULO 3 — A1: Venda"]
CATALOG = {"aulas": {"kind": "list", "items": LESSONS}}
CHOSEN: dict[str, Any] = {
    "fields": [
        {"name": "titulo", "kind": "text", "max_length": 120},
        {"name": "aula", "kind": "options", "values_from": "aulas", "description": "A aula."},
        {
            "name": "fases",
            "kind": "list",
            "items": {
                "kind": "group",
                "fields": [
                    {
                        "name": "aulas",
                        "kind": "list",
                        "min_items": 1,
                        "items": {"kind": "options", "values_from": "aulas"},
                    }
                ],
            },
        },
    ]
}
CHOSEN_EXAMPLE = {
    "titulo": "Plano",
    "aula": LESSONS[0],
    "fases": [{"aulas": [LESSONS[1], LESSONS[2]]}],
}


def catalogued(plan: dict[str, Any] = CHOSEN, data: Any = CATALOG, **more: Any) -> dict[str, Any]:
    return with_plan(plan, CHOSEN_EXAMPLE, data=data, **more)


def test_a_field_takes_its_options_from_a_list_of_the_form_data() -> None:
    spec = compiled(catalogued())

    schema = strict_schema(spec.plan_model)
    assert schema["properties"]["aula"]["enum"] == LESSONS
    assert schema["$defs"]["FasesItem"]["properties"]["aulas"]["items"]["enum"] == LESSONS
    assert spec.example_plan == CHOSEN_EXAMPLE
    assert not refusals(spec, CHOSEN_EXAMPLE)
    assert refusals(spec, {**CHOSEN_EXAMPLE, "aula": "módulo 1 — a1: boas vindas"}) != []
    assert refusals(spec, {**CHOSEN_EXAMPLE, "fases": [{"aulas": ["Outra aula"]}]}) != []


def test_the_options_follow_the_list_in_the_order_of_the_list() -> None:
    reordered = {"aulas": {"kind": "list", "items": [LESSONS[2], LESSONS[0], LESSONS[1]]}}

    spec = compiled(catalogued(data=reordered))

    assert strict_schema(spec.plan_model)["properties"]["aula"]["enum"] == [
        LESSONS[2],
        LESSONS[0],
        LESSONS[1],
    ]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({}, "plan.fields.1.values_from: não há uma lista chamada aulas nos dados do formulário"),
        (
            {"aulas": {"kind": "value", "value": LESSONS}},
            "plan.fields.1.values_from: o bloco aulas dos dados do formulário não é uma lista",
        ),
    ],
    ids=["missing", "not-a-list"],
)
def test_the_list_of_the_options_must_exist_in_the_form_data(data: Any, expected: str) -> None:
    assert expected in problems_of(catalogued(data=data))


def test_options_come_from_fixed_values_or_from_a_list_never_both_or_neither() -> None:
    both = with_field(CHOSEN, (1,), values=["a"])
    neither = with_field(CHOSEN, (1,))
    neither["fields"][1].pop("values_from")

    assert "plan.fields.1.values_from: use os valores fixos ou a lista, não os dois" in (
        problems_of(catalogued(both))
    )
    assert (
        "plan.fields.1.values: as opções fixas precisam dos valores ou de uma lista dos dados do formulário"
        in problems_of(catalogued(neither))
    )


# --- opções tiradas de várias listas (a união) --------------------------------------------------

COMBO = ["Plano A", "Plano B"]
ECOSYSTEM = ["Grupo A", "Plano B", "Grupo C"]  # "Plano B" também está no combo
MATERIALS = ["Guia"]
UNION = ["Plano A", "Plano B", "Grupo A", "Grupo C", "Guia"]
GROUPS = {
    "combo": {"kind": "list", "items": COMBO},
    "ecossistema": {"kind": "list", "items": ECOSYSTEM},
    "materiais": {"kind": "list", "items": MATERIALS},
}
MANY: dict[str, Any] = {
    "fields": [
        {"name": "titulo", "kind": "text", "max_length": 120},
        {
            "name": "entregavel",
            "kind": "options",
            "values_from": ["combo", "ecossistema", "materiais"],
        },
        {
            "name": "fases",
            "kind": "list",
            "items": {
                "kind": "group",
                "fields": [
                    {
                        "name": "entregaveis",
                        "kind": "list",
                        "min_items": 1,
                        "items": {"kind": "options", "values_from": ["materiais", "combo"]},
                    }
                ],
            },
        },
    ]
}
MANY_EXAMPLE = {
    "titulo": "Plano",
    "entregavel": "Grupo C",  # só a segunda lista tem
    "fases": [{"entregaveis": ["Guia", "Plano A"]}],
}


def unioned(plan: dict[str, Any] = MANY, data: Any = GROUPS) -> dict[str, Any]:
    return with_plan(plan, MANY_EXAMPLE, data=data)


def test_a_field_takes_its_options_from_the_union_of_several_lists_in_the_order_of_the_names() -> (
    None
):
    spec = compiled(unioned())

    schema = strict_schema(spec.plan_model)
    # Cada item aparece uma vez, na primeira posição: "Plano B" fica onde o combo o põe.
    assert schema["properties"]["entregavel"]["enum"] == UNION
    # A ordem é a dos nomes, e não a dos blocos: materiais antes do combo.
    assert schema["$defs"]["FasesItem"]["properties"]["entregaveis"]["items"]["enum"] == [
        "Guia",
        "Plano A",
        "Plano B",
    ]
    assert spec.example_plan == MANY_EXAMPLE
    assert not refusals(spec, MANY_EXAMPLE)


def test_the_union_accepts_a_value_of_any_list_and_nothing_else() -> None:
    spec = compiled(unioned())

    for value in UNION:
        assert not refusals(spec, {**MANY_EXAMPLE, "entregavel": value})
    assert refusals(spec, {**MANY_EXAMPLE, "entregavel": "Outro"}) != []
    # "Grupo A" é do ecossistema, que a lista dos itens das fases não cita.
    assert refusals(spec, {**MANY_EXAMPLE, "fases": [{"entregaveis": ["Grupo A"]}]}) != []


def test_a_list_of_one_name_gives_the_same_options_as_the_name_alone() -> None:
    alone = compiled(catalogued())
    listed = compiled(catalogued(plan=with_field(CHOSEN, (1,), values_from=["aulas"])))

    assert strict_schema(listed.plan_model) == strict_schema(alone.plan_model)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            {"values_from": ["combo", "inventada"]},
            "plan.fields.1.values_from.1: não há uma lista chamada inventada nos dados do formulário",
        ),
        (
            {"values_from": ["combo", "ecossistema", "unica"]},
            "plan.fields.1.values_from.2: o bloco unica dos dados do formulário não é uma lista",
        ),
        ({"values_from": []}, "plan.fields.1.values_from: precisa de pelo menos 1 item"),
        (
            {"values_from": ["combo", "materiais", "combo"]},
            "plan.fields.1.values_from.2: lista repetida",
        ),
        (
            {"values_from": ["combo", "materiais"], "values": ["a"]},
            "plan.fields.1.values_from: use os valores fixos ou a lista, não os dois",
        ),
        (
            {"values_from": ["combo", "Materiais"]},
            (
                "plan.fields.1.values_from.1: use letras minúsculas sem acento, números e _, "
                "começando por uma letra"
            ),
        ),
        ({"values_from": ["combo", 5]}, "plan.fields.1.values_from.1: precisa ser um texto"),
    ],
    ids=["missing", "not-a-list", "empty", "repeated-name", "with-values", "bad-name", "not-text"],
)
def test_the_compiler_refuses_a_bad_list_of_names_at_the_name_that_breaks(
    change: dict[str, Any], expected: str
) -> None:
    data = {**GROUPS, "unica": {"kind": "value", "value": MATERIALS}}

    assert expected in problems_of(unioned(with_field(MANY, (1,), **change), data))


# --- blocos de dados com tipo --------------------------------------------------------------------


def datas(**blocks: Any) -> dict[str, Any]:
    return changed({"data": blocks})


def windows(*items: tuple[str, str, str], **more: Any) -> dict[str, Any]:
    return {
        "kind": "windows",
        "windows": [
            {"name": name, "label": f"Janela {name}", "start": start, "end": end}
            for name, start, end in items
        ],
        **more,
    }


OPTION = ("opcao", "2026-09-25", "2026-10-30")
WITHDRAWAL = ("desistencia", "2026-11-03", "2026-12-20")


def test_a_list_block_gives_the_template_the_list() -> None:
    prompt = "{% for item in data.aulas %}- {{ item }}\n{% endfor %}{{ data.aulas|length }} aulas\n"
    spec = compiled(
        datas(aulas={"kind": "list", "items": ["Boas vindas", "Venda"]}) | {"prompt": prompt}
    )

    rendered = spec.render_prompt(answers_of(spec, DEMO_ANSWERS))

    assert rendered == "- Boas vindas\n- Venda\n2 aulas\n"


def test_a_list_block_refuses_repeated_names_wherever_they_differ_only_in_looks() -> None:
    document = datas(aulas={"kind": "list", "items": ["Venda", "Boas vindas", "  venda ", "Venda"]})

    assert problems_of(document) == [
        "data.aulas.items.2: outro item da lista tem o mesmo nome",
        "data.aulas.items.3: outro item da lista tem o mesmo nome",
    ]


def test_a_list_block_needs_at_least_one_item() -> None:
    assert problems_of(datas(aulas={"kind": "list", "items": []})) == [
        "data.aulas.items: precisa de pelo menos 1 item"
    ]


def test_a_windows_block_gives_the_template_the_windows_by_name_with_real_dates() -> None:
    prompt = (
        "A opção vai de {{ data.janelas.opcao.start|short_date }} a "
        "{{ data.janelas.opcao.end|long_date }} ({{ data.janelas.opcao.label }}).\n"
        "{% for janela in data.janelas.values() %}{{ janela.name }} {% endfor %}\n"
    )
    document = datas(janelas=windows(OPTION, WITHDRAWAL)) | {"prompt": prompt}
    spec = compiled(document)

    rendered = spec.render_prompt(answers_of(spec, DEMO_ANSWERS))

    assert rendered == (
        "A opção vai de 25/09/2026 a 30 de outubro de 2026 (Janela opcao).\nopcao desistencia \n"
    )


def test_a_window_without_a_label_shows_its_name() -> None:
    block = {
        "kind": "windows",
        "windows": [{"name": "opcao", "start": "2026-09-25", "end": "2026-10-30"}],
    }
    spec = compiled(datas(janelas=block) | {"prompt": "{{ data.janelas.opcao.label }}"})

    assert spec.render_prompt(answers_of(spec, DEMO_ANSWERS)) == "opcao"


def test_dates_out_of_order_are_refused_where_they_are() -> None:
    backwards = datas(janelas=windows(("opcao", "2026-10-30", "2026-09-25")))
    overlapping = datas(janelas=windows(OPTION, ("desistencia", "2026-10-30", "2026-12-20")))
    swapped = datas(janelas=windows(WITHDRAWAL, OPTION))

    assert problems_of(backwards) == ["data.janelas.windows.0.end: termina antes de começar"]
    assert problems_of(overlapping) == [
        "data.janelas.windows.1.start: precisa começar depois do fim da janela anterior (opcao)"
    ]
    assert problems_of(swapped) == [
        "data.janelas.windows.1.start: precisa começar depois do fim da janela anterior (desistencia)"
    ]


def test_windows_can_overlap_when_the_block_says_they_are_not_a_sequence() -> None:
    document = datas(
        janelas=windows(OPTION, ("outra", "2026-10-01", "2026-12-20"), sequential=False)
    )

    assert compiled(document) is not None


def test_a_window_may_start_and_end_on_the_same_day() -> None:
    compiled(datas(janelas=windows(("dia", "2026-10-30", "2026-10-30"))))


def test_windows_cannot_repeat_a_name() -> None:
    document = datas(janelas=windows(OPTION, ("opcao", "2026-11-03", "2026-12-20")))

    assert problems_of(document) == [
        "data.janelas.windows.1.name: outra janela do bloco já usa este nome"
    ]


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (
            windows(("opcao", "30/10/2026", "2026-12-20")),
            "data.janelas.windows.0.start: use uma data no formato AAAA-MM-DD, como 2026-10-30",
        ),
        (
            windows(("opcao", "2026-02-30", "2026-12-20")),
            "data.janelas.windows.0.start: use uma data no formato AAAA-MM-DD, como 2026-10-30",
        ),
        (
            {
                "kind": "windows",
                "windows": [{"name": "opcao", "start": 20261030, "end": "2026-12-20"}],
            },
            "data.janelas.windows.0.start: use uma data no formato AAAA-MM-DD, como 2026-10-30",
        ),
        (
            {"kind": "windows", "windows": [{"name": "opcao", "start": "2026-10-30"}]},
            "data.janelas.windows.0.end: campo obrigatório",
        ),
        ({"kind": "windows", "windows": []}, "data.janelas.windows: precisa de pelo menos 1 item"),
        (
            {"kind": "calendario"},
            "data.janelas.kind: use um destes tipos: value, list, windows, edition",
        ),
        ({"windows": []}, "data.janelas.kind: campo obrigatório"),
    ],
    ids=["slashes", "impossible-day", "number", "missing-end", "empty", "unknown-kind", "no-kind"],
)
def test_a_data_block_with_a_malformed_date_or_kind_is_refused_where_it_is(
    block: dict[str, Any], expected: str
) -> None:
    assert expected in problems_of(datas(janelas=block))


def test_an_edition_block_gives_the_template_the_name_and_the_dates() -> None:
    prompt = (
        "{{ data.edicao.name }} até {{ data.edicao.ends|short_date }} ({{ data.edicao.starts }})\n"
    )
    block = {"kind": "edition", "name": "Ciclo demonstrativo 2027", "ends": "2027-05-31"}
    spec = compiled(datas(edicao=block) | {"prompt": prompt})

    rendered = spec.render_prompt(answers_of(spec, DEMO_ANSWERS))

    assert rendered == "Ciclo demonstrativo 2027 até 31/05/2027 (None)\n"


def test_an_edition_cannot_end_before_it_starts() -> None:
    block = {
        "kind": "edition",
        "name": "Ciclo demonstrativo 2027",
        "starts": "2027-06-01",
        "ends": "2027-05-31",
    }

    assert problems_of(datas(edicao=block)) == ["data.edicao.ends: termina antes de começar"]


def test_an_edition_may_have_only_a_name() -> None:
    compiled(datas(edicao={"kind": "edition", "name": "Ciclo demonstrativo 2027"}))


def test_a_value_block_stays_as_free_json() -> None:
    spec = compiled(
        datas(edicao={"kind": "value", "value": {"nome": "IRPF"}})
        | {"prompt": "{{ data.edicao.nome }}"}
    )

    assert spec.render_prompt(answers_of(spec, DEMO_ANSWERS)) == "IRPF"


def test_the_template_checks_the_fields_of_a_typed_block_when_it_is_compiled() -> None:
    document = datas(janelas=windows(OPTION)) | {"prompt": "{{ data.janelas.opcao.inicio }}"}

    with pytest.raises(DefinitionError) as raised:
        compile_definition(document)

    assert "variável ou campo inexistente" in str(raised.value)
