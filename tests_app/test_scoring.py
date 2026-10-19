"""Pontuação declarativa: a linguagem de fórmulas, as faixas e as regras de prioridade e de persona,
pela costura do compilador (`compile_definition`). A definição de partida é a do demo, com pontos
nas opções da equipe, uma escolha múltipla (`canais`) e o bloco `scoring` de cada caso."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from conftest import DEMO_ANSWERS

from app.forms import FormSpec
from app.forms.definition import DefinitionError, compile_definition
from app.forms.definition.changes import llm_changes, llm_parts
from app.forms.sandbox import TemplateProblem

FIXTURES = Path(__file__).parent / "fixtures"
BASE: dict[str, Any] = json.loads((FIXTURES / "demo_definition.json").read_text(encoding="utf-8"))
TEAM_POINTS = {"so_eu": 25, "2_a_5": 50, "6_a_15": 75, "mais_de_15": 100}
CHANNELS = {"a": 1, "b": 2, "c": 4, "d": 8}


def definition(scoring: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
    """O demo com pontos na equipe, a escolha múltipla `canais` (pontos 1, 2, 4 e 8) e o bloco
    `scoring`. `changes` troca chaves do topo."""
    document = copy.deepcopy(BASE)
    questions = document["steps"][1]["questions"]
    for option in questions[0]["options"]:
        option["points"] = TEAM_POINTS[option["value"]]
    questions.append(
        {
            "name": "canais",
            "kind": "choices",
            "label": "Onde você capta clientes?",
            "options": [
                {"value": value, "label": value.upper(), "points": points}
                for value, points in CHANNELS.items()
            ],
        }
    )
    document["examples"]["answers"]["canais"] = ["a", "c"]
    if scoring is not None:
        document["scoring"] = scoring
    return {**document, **changes}


def notes(*formulas: str) -> dict[str, Any]:
    """Um bloco `scoring` com uma nota por fórmula: `nota` (a primeira), `nota_2`..."""
    names = ["nota", *(f"nota_{n}" for n in range(2, len(formulas) + 1))]
    return {"scores": [{"name": n, "formula": f} for n, f in zip(names, formulas, strict=False)]}


def compiled(scoring: dict[str, Any], **changes: Any) -> FormSpec:
    return compile_definition(definition(scoring, **changes))


def answers_of(spec: FormSpec, **changes: Any) -> Any:
    return spec.answers_model.model_validate({**DEMO_ANSWERS, "canais": ["a", "c"], **changes})


def value_of(formula: str, **answers: Any) -> Any:
    """A nota que a fórmula dá com as respostas do demo e as que o teste trocar."""
    spec = compiled(notes(formula))
    assert spec.score is not None
    return spec.score(answers_of(spec, **answers)).scores["nota"]


def problems_of(document: dict[str, Any]) -> list[tuple[str, str]]:
    with pytest.raises(DefinitionError) as raised:
        compile_definition(document)
    return [(problem.path, problem.message) for problem in raised.value.problems]


# --- a linguagem --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("42", 42),
        ("2 + 3 * 4", 14),
        ("(2 + 3) * 4", 20),
        ("10 - 2 - 3", 5),
        ("2 * 3 / 4", 1.5),
        ("10 / 4", 2.5),
        ("-3 + 5", 2),
        ("- (2 + 3)", -5),
        ("0.5 * 100", 50),
        ("1 - 0.25", 0.75),
    ],
)
def test_numbers_and_the_four_operations_follow_the_usual_precedence(
    formula: str, expected: float
) -> None:
    assert value_of(formula) == expected


@pytest.mark.parametrize(
    ("team", "points"), [("so_eu", 25), ("2_a_5", 50), ("6_a_15", 75), ("mais_de_15", 100)]
)
def test_points_are_those_of_the_chosen_option(team: str, points: int) -> None:
    assert value_of("pontos(tamanho_equipe)", tamanho_equipe=team) == points


def test_points_of_a_multiple_choice_add_up_the_chosen_options() -> None:
    assert value_of("pontos(canais)", canais=["a", "c"]) == 5
    assert value_of("pontos(canais)", canais=["d"]) == 8


def test_count_is_the_number_of_chosen_options() -> None:
    assert value_of("contagem(canais)", canais=["a", "b", "d"]) == 3
    assert value_of("contagem(canais)", canais=["a"]) == 1


def test_an_unanswered_optional_choice_counts_zero_points_and_zero_choices() -> None:
    document = definition(notes("pontos(canais) + contagem(canais)"))
    document["steps"][1]["questions"][2]["required"] = False
    del document["examples"]["answers"]["canais"]
    spec = compile_definition(document)

    result = spec.score(spec.answers_model.model_validate({**DEMO_ANSWERS}))  # type: ignore[misc]

    assert result.scores["nota"] == 0


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("minimo(3, 1, 2)", 1),
        ("maximo(3, 1, 2)", 3),
        ("soma(1, 2, 3)", 6),
        ("media(1, 2, 6)", 3),
        ("media(1, 2)", 1.5),
        ("minimo(7)", 7),
        ("maximo(pontos(tamanho_equipe), 80)", 80),
        ("soma(pontos(tamanho_equipe), contagem(canais), 1)", 53),
    ],
)
def test_minimum_maximum_sum_and_mean(formula: str, expected: float) -> None:
    assert value_of(formula) == expected


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("se(pontos(tamanho_equipe) >= 50, 1, 0)", 1),
        ("se(pontos(tamanho_equipe) > 50, 1, 0)", 0),
        ('se(principal_desafio == "captacao", 10, 20)', 10),
        ('se(principal_desafio != "captacao", 10, 20)', 20),
        ('se(principal_desafio == "captacao" e pontos(tamanho_equipe) < 50, 1, 2)', 2),
        ('se(principal_desafio == "vendas" ou pontos(tamanho_equipe) == 50, 1, 2)', 1),
        ('se(nao principal_desafio == "vendas", 1, 2)', 1),
        ('se(nao (principal_desafio == "captacao" e pontos(tamanho_equipe) == 50), 1, 2)', 2),
        ("se(1 < 2 e 2 <= 2 e 3 > 2 e 3 >= 3, 1, 0)", 1),
        ("se(1 > 2 ou 2 > 3 ou 3 > 4, 1, se(2 > 1, 5, 6))", 5),
    ],
)
def test_conditions_compare_answers_and_points_and_combine_and_or_not(
    formula: str, expected: float
) -> None:
    assert value_of(formula) == expected


def test_and_binds_tighter_than_or() -> None:
    # verdadeiro ou (falso e falso) = verdadeiro; (verdadeiro ou falso) e falso seria falso.
    assert value_of("se(1 < 2 ou 1 > 2 e 1 > 2, 1, 0)") == 1


def test_a_comparison_with_an_unanswered_choice_is_false() -> None:
    formula = 'se(principal_desafio == "vendas", 1, 2) + se(principal_desafio != "vendas", 10, 20)'
    document = definition(notes(formula))
    document["steps"][1]["questions"][1]["required"] = False
    del document["examples"]["answers"]["principal_desafio"]
    spec = compile_definition(document)

    result = spec.score(answers_of(spec, principal_desafio=""))  # type: ignore[misc]

    assert result.scores["nota"] == 12


@pytest.mark.parametrize(
    ("formula", "team", "expected"),
    [
        ("faixa(pontos(tamanho_equipe), 100, 4, 75, 3, 50, 2, 1)", "mais_de_15", 4),
        ("faixa(pontos(tamanho_equipe), 100, 4, 75, 3, 50, 2, 1)", "6_a_15", 3),
        ("faixa(pontos(tamanho_equipe), 100, 4, 75, 3, 50, 2, 1)", "2_a_5", 2),
        ("faixa(pontos(tamanho_equipe), 100, 4, 75, 3, 50, 2, 1)", "so_eu", 1),
        ("faixa(contagem(canais), 4, 100, 3, 75, 2, 50, 25)", "so_eu", 50),
    ],
)
def test_a_band_picks_the_result_of_the_first_limit_the_value_reaches(
    formula: str, team: str, expected: int
) -> None:
    assert value_of(formula, tamanho_equipe=team) == expected


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("arredondar_meio_para_cima(2.5)", 3),
        ("arredondar_meio_para_o_par(2.5)", 2),
        ("arredondar_meio_para_cima(3.5)", 4),
        ("arredondar_meio_para_o_par(3.5)", 4),
        ("arredondar_meio_para_cima(62.5)", 63),
        ("arredondar_meio_para_o_par(62.5)", 62),
        ("arredondar_meio_para_cima(2.4)", 2),
        ("arredondar_meio_para_cima(2.6)", 3),
        ("arredondar_meio_para_cima(-2.5)", -2),
        ("arredondar_meio_para_o_par(-2.5)", -2),
        ("arredondar_meio_para_cima(0.4 * 25 + 0.6 * 75)", 55),
    ],
)
def test_rounding_is_explicit_half_up_or_half_to_even(formula: str, expected: int) -> None:
    result = value_of(formula)

    assert result == expected and isinstance(result, int)


def test_a_note_without_rounding_keeps_its_decimals() -> None:
    assert value_of("10 / 3") == pytest.approx(10 / 3)


def test_a_note_can_use_other_notes_and_the_result_follows_the_declared_order() -> None:
    spec = compiled(
        {
            "scores": [
                {"name": "total", "formula": "base + bonus"},
                {"name": "base", "formula": "pontos(tamanho_equipe)"},
                {"name": "bonus", "formula": "contagem(canais) * 5"},
            ]
        }
    )

    result = spec.score(answers_of(spec))  # type: ignore[misc]

    assert result.scores == {"total": 60, "base": 50, "bonus": 10}
    assert list(result.scores) == ["total", "base", "bonus"]


def test_a_whole_float_note_comes_out_as_a_whole_number() -> None:
    result = value_of("media(40, 60)")

    assert result == 50 and isinstance(result, int)


# --- construções fora da lista ------------------------------------------------------------------


@pytest.mark.parametrize(
    "formula",
    [
        "2 ** 3",
        "2 // 3",
        "7 % 3",
        '__import__("os")',
        "pontos(tamanho_equipe).real",
        "tamanho_equipe.__class__",
        "pontos(tamanho_equipe)[0]",
        "[1, 2]",
        '{"a": 1}',
        "lambda: 1",
        "abs(1)",
        "round(2.5)",
        "print(1)",
        "eval('1')",
        "open('x')",
        "len(canais)",
        "1; 2",
        "1 if 2 else 3",
        "x := 1",
        "nota_2 = 1",
        "`1`",
        "$x",
        "1 +",
        "(1 + 2",
        "1 + 2)",
        "soma()",
        "se(1, 2)",
        "faixa(1, 2, 3)",
        "arredondar_meio_para_cima(1, 2)",
        "pontos()",
        "pontos(1)",
        "1 2",
        "",
        "   ",
    ],
)
def test_constructs_outside_the_list_are_refused_with_the_formula_path(formula: str) -> None:
    found = problems_of(definition(notes(formula)))

    assert found and all(path == "scoring.scores.0.formula" for path, _ in found), found
    assert all(message for _, message in found)


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("abs(1)", "função"),
        ("2 ** 3", "potência"),
        ("nota_que_nao_existe + 1", "nota_que_nao_existe"),
        ("1 +", "esperava"),
        ('"a" + 1', "texto"),
        ("pontos(escritorio)", "escritorio"),
        ("pontos(tamanho_equipe_x)", "tamanho_equipe_x"),
        ("contagem(tamanho_equipe)", "múltipla"),
        ("canais + 1", "contagem"),
        ("se(1, 2, 3)", "verdadeiro ou falso"),
        ("1 < 2", "número"),
        ('tamanho_equipe == "so_eu"', "número"),
    ],
)
def test_a_refused_formula_says_what_is_wrong_in_portuguese(formula: str, expected: str) -> None:
    messages = " ".join(message for _, message in problems_of(definition(notes(formula))))

    assert expected in messages


def test_a_formula_that_is_too_long_or_too_deep_is_refused_without_crashing() -> None:
    long_formula = " + ".join(["1"] * 3000)
    deep_formula = "(" * 500 + "1" + ")" * 500

    assert problems_of(definition(notes(long_formula)))
    assert problems_of(definition(notes(deep_formula)))


def test_the_formula_language_never_runs_python(tmp_path: Path) -> None:
    marker = tmp_path / "formula-ran"  # nada disto pode ser criado
    formula = f"__import__('pathlib').Path('{marker}').touch()"

    problems_of(definition(notes(formula)))

    assert not marker.exists()


# --- referências, ciclos e divisão por zero -----------------------------------------------------


def test_a_reference_to_a_note_that_does_not_exist_is_refused() -> None:
    found = problems_of(definition(notes("pontos(tamanho_equipe) + ausente")))

    assert found == [
        (
            "scoring.scores.0.formula",
            "ausente: não é uma nota nem uma pergunta de escolha (coluna 26)",
        )
    ]


def test_a_circular_reference_between_notes_is_refused() -> None:
    document = definition(
        {
            "scores": [
                {"name": "a", "formula": "b + 1"},
                {"name": "b", "formula": "c + 1"},
                {"name": "c", "formula": "a + 1"},
            ]
        }
    )

    found = problems_of(document)

    assert any("circular" in message for _, message in found)
    assert any(path.startswith("scoring.scores.") for path, _ in found)


def test_a_note_that_uses_itself_is_a_circular_reference() -> None:
    found = problems_of(definition({"scores": [{"name": "a", "formula": "a + 1"}]}))

    assert any("circular" in message for _, message in found)


def test_division_by_zero_in_the_example_answers_is_refused_on_the_formula() -> None:
    found = problems_of(definition(notes("10 / (pontos(tamanho_equipe) - 50)")))

    assert [path for path, _ in found] == ["scoring.scores.0.formula"]
    assert "zero" in found[0][1]


def test_division_by_zero_with_other_answers_gives_no_score_and_logs_without_the_answers(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # O alembic (`fileConfig`) desliga os loggers que já existiam: religa o do módulo.
    monkeypatch.setattr(logging.getLogger("app.forms.definition.scoring"), "disabled", False)
    spec = compiled(notes("10 / (pontos(tamanho_equipe) - 25)"))
    assert spec.score is not None

    with caplog.at_level(logging.ERROR, logger="app.forms.definition.scoring"):
        result = spec.score(answers_of(spec, tamanho_equipe="so_eu"))

    assert (result.scores, result.priority, result.persona) == ({}, None, None)
    assert "demo-definicao" in caplog.text and "nota" in caplog.text
    assert "Contadora Exemplo" not in caplog.text


# --- nomes e pontos -----------------------------------------------------------------------------


def test_note_names_cannot_repeat_collide_with_questions_or_be_reserved() -> None:
    names = ["nota", "nota", "tamanho_equipe", "items", "e", "se"]
    found = problems_of(definition({"scores": [{"name": n, "formula": "1"} for n in names]}))

    assert [path for path, _ in found] == [f"scoring.scores.{i}.name" for i in (1, 2, 3, 4, 5)]


def test_a_note_name_in_capitals_is_refused_by_the_format() -> None:
    found = problems_of(definition({"scores": [{"name": "Nota", "formula": "1"}]}))

    assert [path for path, _ in found] == ["scoring.scores.0.name"]


def test_points_in_a_formula_need_a_point_on_every_option_of_the_question() -> None:
    document = definition(notes("pontos(tamanho_equipe)"))
    del document["steps"][1]["questions"][0]["options"][2]["points"]

    found = problems_of(document)

    assert any(path == "scoring.scores.0.formula" and "tamanho_equipe" in m for path, m in found)


@pytest.mark.parametrize("bad", ["muito", True, [1], {"a": 1}, 100001, -100001])
def test_points_must_be_a_number_within_the_limits(bad: Any) -> None:
    document = definition(notes("1"))
    document["steps"][1]["questions"][0]["options"][0]["points"] = bad

    paths = [path for path, _ in problems_of(document)]

    assert paths == ["steps.1.questions.0.options.0.points"]


def test_an_option_without_points_or_with_null_points_is_valid() -> None:
    document = definition(notes("1"))
    options = document["steps"][1]["questions"][0]["options"]
    options[0]["points"] = None
    del options[1]["points"]

    assert compile_definition(document).score is not None


def test_a_definition_without_scoring_has_no_score() -> None:
    assert compile_definition(definition()).score is None


# --- faixas -------------------------------------------------------------------------------------


BANDED = {
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
        }
    ]
}


@pytest.mark.parametrize(
    ("team", "key", "label"),
    [
        ("mais_de_15", "grande", "Grande"),
        ("6_a_15", "medio", "Médio"),
        ("2_a_5", "pequeno", "Pequeno"),
        ("so_eu", "pequeno", "Pequeno"),
    ],
)
def test_bands_give_the_note_a_key_and_a_label_next_to_it(team: str, key: str, label: str) -> None:
    spec = compiled(BANDED)

    result = spec.score(answers_of(spec, tamanho_equipe=team))  # type: ignore[misc]

    assert result.scores == {
        "porte": TEAM_POINTS[team],
        "nivel_porte": key,
        "rotulo_porte": label,
    }
    assert list(result.scores) == ["porte", "nivel_porte", "rotulo_porte"]


def test_a_band_key_can_be_used_in_conditions() -> None:
    scoring = {
        **BANDED,
        "priority": [{"when": 'nivel_porte == "grande"', "value": "P1"}, {"value": "P3"}],
    }
    spec = compiled(scoring)

    big = spec.score(answers_of(spec, tamanho_equipe="mais_de_15"))  # type: ignore[misc]
    small = spec.score(answers_of(spec, tamanho_equipe="so_eu"))  # type: ignore[misc]

    assert (big.priority, small.priority) == ("P1", "P3")


@pytest.mark.parametrize(
    ("change", "path"),
    [
        (
            {"levels": [{"min": 90, "key": "a", "label": "A"}]},
            "scoring.scores.0.bands.levels.0.min",
        ),
        (
            {"levels": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]},
            "scoring.scores.0.bands.levels.0.min",
        ),
        (
            {
                "levels": [
                    {"min": 10, "key": "a", "label": "A"},
                    {"min": 20, "key": "b", "label": "B"},
                    {"key": "c", "label": "C"},
                ]
            },
            "scoring.scores.0.bands.levels.1.min",
        ),
        (
            {
                "levels": [
                    {"min": 10, "key": "a", "label": "A"},
                    {"min": 5, "key": "a", "label": "B"},
                    {"key": "c", "label": "C"},
                ]
            },
            "scoring.scores.0.bands.levels.1.key",
        ),
        (
            {
                "levels": [
                    {"min": 10, "key": "a", "label": "A"},
                    {"min": 5, "key": "b", "label": "B", "extra": 1},
                    {"key": "c", "label": "C"},
                ]
            },
            "scoring.scores.0.bands.levels.1.extra",
        ),
        ({"key": "porte"}, "scoring.scores.0.bands.key"),
        ({"label": "nivel_porte"}, "scoring.scores.0.bands.label"),
        ({"key": "tamanho_equipe"}, "scoring.scores.0.bands.key"),
    ],
)
def test_bands_with_a_wrong_structure_are_refused_on_the_band(
    change: dict[str, Any], path: str
) -> None:
    scoring = copy.deepcopy(BANDED)
    scoring["scores"][0]["bands"].update(change)

    assert path in [found for found, _ in problems_of(definition(scoring))]


# --- prioridade e persona -----------------------------------------------------------------------


RULES = {
    **notes("pontos(tamanho_equipe)"),
    "priority": [
        {"when": "nota >= 100", "value": "P1"},
        {"when": "nota >= 50", "value": "P2"},
        {"value": "P3"},
    ],
    "persona": [
        {"when": 'principal_desafio == "vendas"', "value": "Comercial"},
        {"when": "nota >= 75", "value": "Estruturada"},
        {"when": "nota >= 50", "value": "Em crescimento"},
        {"value": "Iniciante"},
    ],
}


@pytest.mark.parametrize(
    ("team", "challenge", "priority", "persona"),
    [
        ("mais_de_15", "captacao", "P1", "Estruturada"),
        ("6_a_15", "captacao", "P2", "Estruturada"),
        ("2_a_5", "captacao", "P2", "Em crescimento"),
        ("so_eu", "captacao", "P3", "Iniciante"),
        ("mais_de_15", "vendas", "P1", "Comercial"),
        ("so_eu", "vendas", "P3", "Comercial"),
    ],
)
def test_the_first_rule_that_matches_decides_the_priority_and_the_persona(
    team: str, challenge: str, priority: str, persona: str
) -> None:
    spec = compiled(RULES)

    result = spec.score(answers_of(spec, tamanho_equipe=team, principal_desafio=challenge))  # type: ignore[misc]

    assert (result.priority, result.persona) == (priority, persona)


def test_rules_in_order_beat_a_later_rule_that_would_also_match() -> None:
    scoring = {
        **notes("pontos(tamanho_equipe)"),
        "priority": [{"when": "nota >= 25", "value": "P3"}, {"when": "nota >= 100", "value": "P1"}],
        "persona": [{"when": "nota >= 25", "value": "primeira"}, {"value": "segunda"}],
    }
    spec = compiled(scoring)

    result = spec.score(answers_of(spec, tamanho_equipe="mais_de_15"))  # type: ignore[misc]

    assert (result.priority, result.persona) == ("P3", "primeira")


def test_the_persona_can_be_the_value_of_an_answer() -> None:
    scoring = {
        **notes("1"),
        "priority": [{"value": "P2"}],
        "persona": [{"answer": "principal_desafio"}],
    }
    spec = compiled(scoring)

    result = spec.score(answers_of(spec, principal_desafio="gestao"))  # type: ignore[misc]

    assert result.persona == "gestao"


def test_a_persona_rule_can_return_an_answer_under_a_condition() -> None:
    scoring = {
        **notes("pontos(tamanho_equipe)"),
        "persona": [{"when": "nota >= 75", "answer": "principal_desafio"}, {"value": "outras"}],
    }
    spec = compiled(scoring)

    big = spec.score(answers_of(spec, tamanho_equipe="mais_de_15", principal_desafio="vendas"))  # type: ignore[misc]
    small = spec.score(answers_of(spec, tamanho_equipe="so_eu", principal_desafio="vendas"))  # type: ignore[misc]

    assert (big.persona, small.persona) == ("vendas", "outras")


def test_no_rule_for_the_persona_leaves_it_empty_and_the_priority_too() -> None:
    spec = compiled(notes("1"))

    result = spec.score(answers_of(spec))  # type: ignore[misc]

    assert (result.priority, result.persona) == (None, None)


def test_a_persona_with_no_matching_rule_stays_empty() -> None:
    scoring = {**notes("1"), "persona": [{"when": "nota > 5", "value": "Rara"}]}
    spec = compiled(scoring)

    assert spec.score(answers_of(spec)).persona is None  # type: ignore[misc]


@pytest.mark.parametrize(
    ("rules", "path"),
    [
        ([{"when": "nota >= 1", "value": "P4"}, {"value": "P3"}], "scoring.priority.0.value"),
        ([{"when": "nota >= 1", "value": "p1"}, {"value": "P3"}], "scoring.priority.0.value"),
        ([{"when": "nota >= 1", "value": "P1"}, {"value": "Urgente"}], "scoring.priority.1.value"),
        ([{"when": "nota >= 1", "answer": "principal_desafio"}], "scoring.priority.0.answer"),
        ([{"value": "P1"}, {"value": "P2"}], "scoring.priority.0"),
        ([{"when": "nota", "value": "P1"}, {"value": "P3"}], "scoring.priority.0.when"),
        ([{"when": "nota >= ausente", "value": "P1"}, {"value": "P3"}], "scoring.priority.0.when"),
        ([{"when": "nota >= 1 e", "value": "P1"}, {"value": "P3"}], "scoring.priority.0.when"),
        ([{"when": "nota >= 1"}, {"value": "P3"}], "scoring.priority.0"),
        ([{"when": "nota >= 1", "value": "P1", "x": 1}, {"value": "P3"}], "scoring.priority.0.x"),
    ],
)
def test_priority_rules_with_a_wrong_value_condition_or_shape_are_refused(
    rules: list[dict[str, Any]], path: str
) -> None:
    scoring = {**notes("1"), "priority": rules}

    assert path in [found for found, _ in problems_of(definition(scoring))]


def test_the_publication_checks_that_the_example_answers_reach_a_valid_priority() -> None:
    scoring = {**notes("1"), "priority": [{"when": "nota > 5", "value": "P1"}]}

    found = problems_of(definition(scoring))

    assert [path for path, _ in found] == ["scoring.priority"]
    assert "respostas de exemplo" in found[0][1]


@pytest.mark.parametrize(
    ("rules", "path"),
    [
        ([{"when": "nota >= 1", "value": "A", "answer": "principal_desafio"}], "scoring.persona.0"),
        ([{"when": "nota >= 1"}], "scoring.persona.0"),
        ([{"answer": "ausente"}], "scoring.persona.0.answer"),
        ([{"answer": "canais"}], "scoring.persona.0.answer"),
        ([{"answer": "objetivo_90_dias"}], "scoring.persona.0.answer"),
        ([{"value": "x" * 65}], "scoring.persona.0.value"),
        ([{"value": "A"}, {"value": "B"}], "scoring.persona.0"),
    ],
)
def test_persona_rules_with_a_wrong_shape_are_refused(
    rules: list[dict[str, Any]], path: str
) -> None:
    scoring = {**notes("1"), "persona": rules}

    assert path in [found for found, _ in problems_of(definition(scoring))]


def test_a_rule_can_use_the_points_and_the_count_of_answers_directly() -> None:
    scoring = {
        **notes("1"),
        "priority": [
            {"when": "pontos(canais) >= 5 e contagem(canais) == 2", "value": "P1"},
            {"value": "P3"},
        ],
    }
    spec = compiled(scoring)

    hit = spec.score(answers_of(spec, canais=["a", "c"]))  # type: ignore[misc]
    miss = spec.score(answers_of(spec, canais=["a"]))  # type: ignore[misc]

    assert (hit.priority, miss.priority) == ("P1", "P3")


# --- a estrutura do bloco -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scoring", "path"),
    [
        ({"scores": []}, "scoring.scores"),
        ({}, "scoring.scores"),
        ({"scores": [{"name": "a"}]}, "scoring.scores.0.formula"),
        ({"scores": [{"formula": "1"}]}, "scoring.scores.0.name"),
        ({"scores": [{"name": "a", "formula": "1", "x": 1}]}, "scoring.scores.0.x"),
        ({"scores": [{"name": "a", "formula": 1}]}, "scoring.scores.0.formula"),
        ({"scores": [{"name": "a", "formula": "1"}], "extra": []}, "scoring.extra"),
        (
            {"scores": [{"name": "a", "formula": "1"}], "priority": {"when": "x"}},
            "scoring.priority",
        ),
        ("nota = 1", "scoring"),
        ([], "scoring"),
    ],
)
def test_the_scoring_block_with_a_wrong_structure_is_refused_on_its_path(
    scoring: Any, path: str
) -> None:
    document = definition()
    document["scoring"] = scoring

    assert path in [found for found, _ in problems_of(document)]


def test_there_is_a_limit_on_the_number_of_notes_and_rules() -> None:
    many_notes = {"scores": [{"name": f"n_{i}", "formula": "1"} for i in range(65)]}
    many_rules = {
        **notes("1"),
        "priority": [{"when": "nota > 0", "value": "P1"}] * 64 + [{"value": "P3"}],
    }

    assert problems_of(definition(many_notes))
    assert problems_of(definition(many_rules))


# --- as notas nos templates e no que vai ao LLM -------------------------------------------------


def test_the_prompt_reads_the_scores_by_name() -> None:
    document = definition(
        {**BANDED, "priority": [{"value": "P2"}]},
        prompt="Porte: {{ scores.porte }} ({{ scores.rotulo_porte }}).\n",
    )
    spec = compile_definition(document)

    prompt = spec.render_prompt(answers_of(spec, tamanho_equipe="6_a_15"))

    assert prompt == "Porte: 75 (Médio).\n"


def test_the_prompt_cannot_read_a_score_that_does_not_exist() -> None:
    document = definition(notes("1"), prompt="{{ scores.ausente }}\n")

    found = problems_of(document)

    assert any("scores.ausente" in message for _, message in found)


def test_the_pdf_and_the_preview_receive_the_scores_of_the_example_answers() -> None:
    document = definition(notes("pontos(tamanho_equipe)"))
    document["preview"] += "\n<p>Nota {{ scores.nota }}</p>"
    document["pdf"]["html"] = document["pdf"]["html"].replace(
        "</body>", "<p>Nota {{ scores.nota }}</p></body>"
    )
    document["pdf"]["scores"] = ["nota"]  # o PDF e a prévia só leem as notas liberadas

    spec = compile_definition(document)  # renderiza os dois com os exemplos: não pode falhar

    assert spec.templates is not None
    html = spec.templates.render_pdf(
        spec.plan_model.model_validate(spec.example_plan),
        lead={"name": "Ana"},
        form={"slug": "x", "title": "Y"},
        scores={"nota": 50},
        generated_date="8 de outubro de 2026",
    )
    assert "Nota 50" in html


def test_a_change_in_the_scoring_goes_to_the_llm_only_when_the_prompt_reads_the_scores() -> None:
    old = definition(notes("1"))
    new = definition(notes("2"))
    reading = definition(notes("1"), prompt="Nota {{ scores.nota }}\n")
    reading_new = definition(notes("2"), prompt="Nota {{ scores.nota }}\n")

    assert llm_changes(old, new) == ()
    assert llm_changes(reading, reading_new) != ()


def test_a_change_in_the_points_of_an_option_goes_to_the_llm_when_the_prompt_reads_the_scores() -> (
    None
):
    reading = definition(notes("pontos(tamanho_equipe)"), prompt="Nota {{ scores.nota }}\n")
    other = copy.deepcopy(reading)
    other["steps"][1]["questions"][0]["options"][0]["points"] = 26
    silent = definition(notes("pontos(tamanho_equipe)"))
    silent_other = copy.deepcopy(silent)
    silent_other["steps"][1]["questions"][0]["options"][0]["points"] = 26

    assert llm_changes(reading, other) != ()
    assert llm_changes(silent, silent_other) == ()


def test_a_definition_whose_prompt_ignores_the_scores_keeps_the_llm_parts_it_had_before() -> None:
    """A impressão digital do que vai ao LLM não muda para quem não lê a pontuação: os testes
    guardados antes dela continuam valendo."""
    plain = llm_parts(definition())
    scored = llm_parts(definition(notes("pontos(tamanho_equipe)")))
    reading = llm_parts(definition(notes("1"), prompt="{{ scores.nota }}\n"))

    assert "scoring" not in plain and "scoring" not in scored
    assert "scoring" in reading
    assert {key: value for key, value in scored.items() if key != "data"} == {
        key: value for key, value in plain.items() if key != "data"
    }


def test_a_prompt_that_ignores_the_scores_renders_even_when_the_scoring_fails() -> None:
    """A pontuação que falha com certas respostas não pode tirar o plano de quem não a usa."""
    spec = compiled(notes("10 / (pontos(tamanho_equipe) - 25)"))

    prompt = spec.render_prompt(answers_of(spec, tamanho_equipe="so_eu"))

    assert "Tamanho da equipe: so_eu" in prompt


def test_a_prompt_that_reads_the_scores_cannot_render_when_the_scoring_fails() -> None:
    document = definition(
        notes("10 / (pontos(tamanho_equipe) - 25)"), prompt="Nota {{ scores.nota }}\n"
    )
    spec = compile_definition(document)

    with pytest.raises(TemplateProblem) as raised:
        spec.render_prompt(answers_of(spec, tamanho_equipe="so_eu"))

    assert raised.value.detail == "a pontuação não pôde ser calculada com estas respostas"
