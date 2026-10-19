"""A forma normal de uma definição (`app.forms.definition.normal`): o que duas definições que dizem
a mesma coisa têm em comum, e o que ainda as distingue. O efeito no rascunho e na comparação está
em `test_admin_form_editor.py`."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from app.forms.definition.normal import normalized, same_meaning

FIXTURES = Path(__file__).parent / "fixtures"
DEMO: dict[str, Any] = json.loads((FIXTURES / "demo_definition.json").read_text(encoding="utf-8"))


def edited(**changes: Any) -> dict[str, Any]:
    """Uma cópia do demo com as partes de primeiro nível trocadas."""
    return {**copy.deepcopy(DEMO), **changes}


def test_the_empty_optional_parts_are_absent_in_the_normal_form() -> None:
    """Nulas ou vazias, as partes opcionais dizem o mesmo que ausentes: nada."""
    empty = edited(scoring=None, phases=None, notices={}, data={})

    assert not {"scoring", "phases", "notices", "data"} & normalized(empty).keys()
    assert same_meaning(empty, DEMO)


def test_an_optional_part_with_content_is_not_absent() -> None:
    notices = {"waiting": {"title": "Aguarde", "text": "Estamos preparando o seu plano."}}

    assert normalized(edited(notices=notices))["notices"] == notices
    assert not same_meaning(edited(notices=notices), DEMO)


def test_blank_example_answers_say_the_same_as_missing_ones() -> None:
    answers = dict(DEMO["examples"]["answers"])
    blank = edited(
        examples={**DEMO["examples"], "answers": {**answers, "x": None, "y": "", "z": []}}
    )

    assert normalized(blank)["examples"]["answers"] == answers
    assert same_meaning(blank, DEMO)


def test_example_answers_that_are_falsy_but_filled_in_still_count() -> None:
    """Só o em branco (nulo, texto vazio e lista vazia) sai: o zero e o falso são respostas."""
    given = {**DEMO["examples"], "answers": {**DEMO["examples"]["answers"], "n": 0, "b": False}}

    answers = normalized(edited(examples=given))["examples"]["answers"]

    assert answers["n"] == 0 and answers["b"] is False
    assert not same_meaning(edited(examples=given), DEMO)


def test_the_format_defaults_written_out_say_the_same_as_leaving_them_out() -> None:
    verbose = copy.deepcopy(DEMO)
    for step in verbose["steps"]:
        for question in step["questions"]:
            question.setdefault("required", True)
            question.setdefault("contact", False)

    assert verbose != DEMO
    assert same_meaning(verbose, DEMO)


def test_a_change_of_behaviour_is_not_a_default() -> None:
    changed = copy.deepcopy(DEMO)
    changed["steps"][0]["questions"][0]["required"] = False

    assert not same_meaning(changed, DEMO)


def test_normalizing_does_not_touch_the_definition_it_reads() -> None:
    original = edited(scoring=None, data={})
    snapshot = copy.deepcopy(original)

    normalized(original)

    assert original == snapshot


def test_a_part_the_format_no_longer_reads_stays_as_it_is() -> None:
    """Uma versão antiga que não compila mais ainda se compara: a parte ilegível fica como está."""
    old = edited(steps="lixo", consent=[])

    assert normalized(old)["steps"] == "lixo" and normalized(old)["consent"] == []
    assert same_meaning(old, edited(steps="lixo", consent=[]))
    assert not same_meaning(old, edited(steps="outro lixo", consent=[]))
