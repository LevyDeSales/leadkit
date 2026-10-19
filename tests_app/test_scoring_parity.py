"""Pontuação declarativa: paridade com o código que ela substituiu. Pela costura do compilador, os
casos de pontuação do MCC e os cinco perfis do reforma-2027 dão os mesmos números, a mesma
prioridade e a mesma persona de antes, e o demo também.

Os esperados do MCC vêm do JS legado e os do reforma-2027 de perfis calculados à mão
(`mcc_scoring_cases.json`, `reforma_2027_perfis.json`). As definições são as dos três formulários
que viveram em código (`app/migrations/legacy_forms/`, ticket 14). A pontuação em Python que elas
substituíram foi comparada com a declarada em todas as combinações das perguntas que pontuam e em
1500 respostas sorteadas, antes de sair do código (PR da paridade).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import DEMO_ANSWERS, legacy_spec

from app.forms import FormSpec

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


MCC_CASES = load("mcc_scoring_cases.json")
REFORMA_PROFILES = load("reforma_2027_perfis.json")
# A prioridade fica gravada pelo código (a coluna guarda até 16 caracteres).
PRIORITY_CODE = {
    "P1 - ação imediata": "P1",
    "P2 - abordagem prioritária": "P2",
    "P3 - nutrição": "P3",
}
# O rótulo de cada nível, como a lead e o admin os leem.
READINESS_LABELS = {
    "pronta": "Pronta para 2027",
    "avancada": "Avançada",
    "em_construcao": "Em construção",
    "inicial": "Inicial",
}
EXPOSURE_LABELS = {"alta": "Alta", "media": "Média", "baixa": "Baixa"}


DEMO = legacy_spec("demo")
MCC = legacy_spec("mcc")
REFORMA = legacy_spec("reforma-2027")


def scored(spec: FormSpec, answers: dict[str, Any]) -> Any:
    assert spec.score is not None
    return spec.score(spec.answers_model.model_validate(answers))


# --- MCC ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", MCC_CASES, ids=[case["caso"] for case in MCC_CASES])
def test_mcc_scores_priority_and_persona_match_the_legacy_n8n(case: dict[str, Any]) -> None:
    expected = case["esperado_legado"]

    result = scored(MCC, case["respostas"])

    assert (result.priority, result.persona) == (
        PRIORITY_CODE[expected["priority_cluster"]],
        expected["persona_cluster"],
    )
    assert result.scores == {
        key: value for key, value in expected.items() if key.endswith("_score")
    }


def test_mcc_scores_come_out_as_whole_numbers_like_the_code() -> None:
    result = scored(MCC, MCC_CASES[0]["respostas"])

    assert all(type(value) is int for value in result.scores.values())


# --- reforma-2027 -------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", REFORMA_PROFILES, ids=[p["perfil"] for p in REFORMA_PROFILES])
def test_reforma_profiles_get_the_same_scores_priority_and_persona(
    profile: dict[str, Any],
) -> None:
    expected = profile["esperado"]

    result = scored(REFORMA, profile["respostas"])

    assert result.scores == {
        "prontidao": expected["prontidao"],
        "nivel_prontidao": expected["nivel_prontidao"],
        "rotulo_prontidao": READINESS_LABELS[expected["nivel_prontidao"]],
        "exposicao": expected["exposicao"],
        "nivel_exposicao": expected["nivel_exposicao"],
        "rotulo_exposicao": EXPOSURE_LABELS[expected["nivel_exposicao"]],
        "necessidade": expected["necessidade"],
    }
    assert (result.priority, result.persona) == (expected["prioridade"], expected["persona"])


# --- demo ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("team", "porte", "priority"),
    [("so_eu", 25, "P3"), ("2_a_5", 50, "P2"), ("6_a_15", 75, "P1"), ("mais_de_15", 100, "P1")],
)
def test_demo_scores_the_team_size_and_takes_the_challenge_as_persona(
    team: str, porte: int, priority: str
) -> None:
    for challenge in ("captacao", "vendas", "operacao", "gestao"):
        answers = {**DEMO_ANSWERS, "tamanho_equipe": team, "principal_desafio": challenge}

        result = scored(DEMO, answers)

        assert (result.scores, result.priority, result.persona) == (
            {"porte": porte},
            priority,
            challenge,
        )
