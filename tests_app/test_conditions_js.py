"""O avaliador de condições do navegador (`app/web/static/js/conditions.js`) contra o do servidor
(`Conditions.evaluate`): os casos que as duas pontas precisam resolver igual
(`fixtures/conditions_cases.json`) e respostas sorteadas, com o mesmo resultado nas duas.

O JavaScript roda no `node`, sem navegador: o arquivo é uma função pura sobre as perguntas que a
página recebe (`Conditions.page_data`). Sem `node` no ambiente, os testes são pulados; no CI
(variável `CI`), a falta dele reprova, para o teste não passar em silêncio."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_form_conditions import CASES, EVALUATOR, compiled

from app.forms.definition.conditions import Conditions

HARNESS = Path(__file__).parent / "js" / "conditions_harness.cjs"


def run_js(conditions: Conditions, samples: list[Any]) -> list[dict[str, Any]]:
    """O resultado do avaliador do navegador para cada amostra, sobre as perguntas que a página
    recebe (passadas por JSON, como no HTML)."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("o CI precisa do node para conferir o avaliador do navegador")
        pytest.skip("node não está instalado")
    payload = json.dumps({"questions": conditions.page_data, "samples": samples})
    done = subprocess.run(
        [node, str(HARNESS)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,  # o código de saída é conferido abaixo, com a mensagem do node
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_browser_evaluator_resolves_the_shared_cases() -> None:
    cases = CASES["cases"]
    names = [question["name"] for question in CASES["questions"]]

    found = run_js(EVALUATOR, [case["answers"] for case in cases])

    for case, seen in zip(cases, found, strict=True):
        hidden = [name for name in names if name not in case["visible"]]
        assert seen["visible"] == case["visible"], case["name"]
        assert seen["options"] == case["options"], case["name"]
        assert seen["hidden"] == hidden, case["name"]


def _drawn_answers(conditions: Conditions, draws: int, seed: int) -> list[dict[str, Any]]:
    """Respostas sorteadas (mesma semente, mesmas respostas) para as perguntas das condições: em
    branco, com espaços, opções válidas e inválidas, trechos dos valores das regras, listas onde
    cabe texto e tipos errados."""
    rng = random.Random(seed)
    questions = conditions.page_data
    values = [
        value
        for question in questions
        for rule in (question.get("show_when") or {}).get("rules", ())
        for value in (rule["value"] if isinstance(rule["value"], list) else [rule["value"]])
    ] or ["x"]
    padding = ["", " ", "  ", "\t", "　", " ", "﻿", "\x1f", "\x85"]
    samples = []
    for _ in range(draws):
        answers: dict[str, Any] = {}
        for question in questions:
            options = [option["value"] for option in question.get("options", ())]
            pick = [rng.choice(options) for _ in range(rng.randint(0, 3))] if options else []
            word = rng.choice(options or values)
            drawn = rng.choice(
                [
                    None,  # nem chega no envio
                    "",
                    "   ",
                    [],
                    [" "],
                    word,
                    pick,
                    f"{rng.choice(padding)}{word}{rng.choice(padding)}",
                    [f"{rng.choice(padding)}{item}{rng.choice(padding)}" for item in pick],
                    f"{rng.choice(values)} {rng.choice(values).upper()}",
                    f"Ana {rng.choice(values)} Souza",
                    rng.choice(values).upper(),
                    5,
                    True,
                    {"a": 1},
                    [1, None, word],
                ]
            )
            if drawn is not None or rng.random() < 0.5:
                answers[question["name"]] = drawn
        samples.append(answers)
    return samples


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_browser_evaluator_agrees_with_the_server_on_drawn_answers(seed: int) -> None:
    for conditions in (EVALUATOR, compiled().conditions):
        assert conditions is not None
        samples = _drawn_answers(conditions, draws=300, seed=seed)

        found = run_js(conditions, samples)

        for answers, seen in zip(samples, found, strict=True):
            expected = conditions.evaluate(answers)
            assert seen["visible"] == list(expected.visible), answers
            assert seen["hidden"] == list(expected.hidden), answers
            assert seen["options"] == {
                name: list(values) for name, values in expected.options.items()
            }, answers
