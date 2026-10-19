"""As notas que o PDF e a prévia podem mostrar: a operadora libera nota por nota (`pdf.scores`), e o
que não foi liberado nunca chega ao template. A prioridade e a persona não chegam nunca. Os testes
passam pelo compilador e pelos templates compilados."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from test_form_definition import DEFINITION as DEMO
from test_scoring import BANDED, definition, notes

from app.forms import FormSpec
from app.forms.definition import DefinitionError, compile_definition
from app.forms.definition.changes import compare, llm_changes

# Duas notas: `porte`, com faixas (cria `nivel_porte` e `rotulo_porte`), e `alcance`, sem.
SCORING = {
    "scores": [
        *BANDED["scores"],
        {"name": "alcance", "formula": "contagem(canais) * 12.5"},
    ],
    "priority": [{"value": "P2"}],
    "persona": [{"value": "Iniciante"}],
}
ALL_SCORES = {
    "porte": 75,
    "nivel_porte": "medio",
    "rotulo_porte": "Médio",
    "alcance": 25,
}
LIST_SCORES = "{% for name in scores %}[{{ name }}={{ scores[name] }}]{% endfor %}"


def released(*names: str, pdf: str = LIST_SCORES, preview: str = LIST_SCORES) -> dict[str, Any]:
    """O demo com a pontuação acima e estas notas liberadas para o PDF e a prévia."""
    document = definition(copy.deepcopy(SCORING))
    document["pdf"] = {**document["pdf"], "html": pdf, "scores": list(names)}
    document["preview"] = preview
    return document


def printed(spec: FormSpec, *, scores: dict[str, Any] | None = None, where: str = "pdf") -> str:
    assert spec.templates is not None
    plan = spec.plan_model.model_validate(DEMO["examples"]["plan"])
    page: dict[str, Any] = {
        "lead": {"name": "Ana"},
        "form": {"slug": spec.slug, "title": spec.title},
        "scores": ALL_SCORES if scores is None else scores,
        "generated_date": "8 de outubro de 2026",
    }
    if where == "pdf":
        return spec.templates.render_pdf(plan, **page)
    return spec.templates.render_preview(plan.model_dump(mode="json"), **page)


def problems_of(document: dict[str, Any]) -> list[tuple[str, str, int | None]]:
    with pytest.raises(DefinitionError) as raised:
        compile_definition(document)
    return [(p.path, p.message, p.line) for p in raised.value.problems]


@pytest.mark.parametrize("where", ["pdf", "preview"])
def test_only_the_released_notes_reach_the_template(where: str) -> None:
    spec = compile_definition(released("alcance"))

    assert printed(spec, where=where) == "[alcance=25]"


@pytest.mark.parametrize("where", ["pdf", "preview"])
def test_a_released_note_brings_its_band_level_and_label(where: str) -> None:
    spec = compile_definition(released("porte"))

    html = printed(spec, where=where)

    assert html == "[porte=75][nivel_porte=medio][rotulo_porte=Médio]"


@pytest.mark.parametrize("where", ["pdf", "preview"])
def test_without_a_choice_no_note_reaches_the_template(where: str) -> None:
    document = released()
    del document["pdf"]["scores"]

    spec = compile_definition(document)

    assert printed(spec, where=where) == ""


def test_priority_and_persona_never_reach_the_pdf_or_the_preview() -> None:
    spec = compile_definition(released("porte", "alcance"))

    html = printed(spec, scores={**ALL_SCORES, "priority": "P1", "persona": "X"})

    assert "P1" not in html and "persona" not in html and "priority" not in html
    for slot, source in (("pdf.html", "{{ priority }}"), ("preview", "{{ persona }}")):
        found = problems_of(
            released("porte", **{"pdf" if slot == "pdf.html" else "preview": source})
        )
        assert any(path == slot and "variável inexistente" in text for path, text, _ in found)


def test_a_template_that_reads_a_note_nobody_released_is_refused_with_its_line() -> None:
    markup = "<p>Porte {{ scores.porte }}</p>\n<p>Alcance {{ scores.alcance }}</p>"

    found = problems_of(released("porte", pdf=markup, preview=markup))

    assert [(path, line) for path, _, line in found] == [("pdf.html", 2), ("preview", 2)]
    assert all("scores.alcance" in message and "PDF" in message for _, message, _ in found)


def test_the_band_label_of_a_note_that_was_not_released_is_refused_too() -> None:
    found = problems_of(released("alcance", pdf="{{ scores.rotulo_porte }}"))

    assert [path for path, _, _ in found] == ["pdf.html"]


def test_a_template_can_read_a_released_note_and_its_band() -> None:
    spec = compile_definition(
        released(
            "porte",
            pdf="{{ scores.porte }} {{ scores.rotulo_porte }}",
            preview="{{ scores.nivel_porte }}",
        )
    )

    assert printed(spec, where="pdf") == "75 Médio"
    assert printed(spec, where="preview") == "medio"


def test_the_prompt_still_reads_every_note_whatever_the_pdf_releases() -> None:
    document = released("alcance")
    document["prompt"] = "Porte {{ scores.porte }} ({{ scores.rotulo_porte }})\n"

    spec = compile_definition(document)

    assert spec.templates is not None and spec.templates.reads_scores


@pytest.mark.parametrize(
    ("names", "path", "message"),
    [
        (["ausente"], "pdf.scores.0", "não é uma nota da pontuação"),
        (["porte", "ausente"], "pdf.scores.1", "não é uma nota da pontuação"),
        (["porte", "porte"], "pdf.scores.1", "nota repetida"),
        (["rotulo_porte"], "pdf.scores.0", "não é uma nota da pontuação"),
        (
            ["Porte"],
            "pdf.scores.0",
            "use letras minúsculas sem acento, números e _, começando por uma letra",
        ),
    ],
)
def test_the_list_of_released_notes_is_checked_against_the_scoring(
    names: list[str], path: str, message: str
) -> None:
    found = problems_of(released(*names))

    assert (path, message) in [(found_path, text) for found_path, text, _ in found]


def test_a_form_without_scoring_has_no_note_to_release() -> None:
    document = copy.deepcopy(DEMO)
    document["pdf"]["scores"] = ["porte"]

    found = problems_of(document)

    assert ("pdf.scores.0", "o formulário não tem pontuação") in [(p, m) for p, m, _ in found]


def test_a_definition_without_the_key_compiles_as_before() -> None:
    spec = compile_definition(copy.deepcopy(DEMO))

    assert spec.templates is not None and spec.templates.released == frozenset()


def test_releasing_a_note_is_a_change_of_the_pdf_that_needs_no_llm_test() -> None:
    before, after = released("porte"), released("porte", "alcance")

    assert llm_changes(before, after) == ()
    changed = [section.title for section in compare(before, after) if section.changed]
    assert changed == ["Notas que o PDF e a prévia podem mostrar"]


def test_the_notes_helper_still_builds_a_plain_scoring() -> None:
    document = definition(notes("1"))
    document["pdf"]["scores"] = ["nota"]

    assert compile_definition(document).templates.released == frozenset({"nota"})  # type: ignore[union-attr]
