"""Adapta campos administrativos sem consentimento nem efeitos do envio público."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from starlette.datastructures import FormData

from app.forms import FormSpec
from app.forms.definition import DefinitionError, Problem, compile_definition
from app.forms.drafts import Draft
from app.web.formview import FormPost, conditions_data, read_post, steps_view

FIELD_PREFIX = "_simulation_"


def read_fields(spec: FormSpec, form: FormData) -> FormPost:
    """Separa respostas dos controles administrativos antes da leitura pública sem efeitos."""
    if spec.screen is None:
        raise DefinitionError([Problem("answers", "O rascunho não tem campos para simular.")])
    answers = FormData(
        [
            (name.removeprefix(FIELD_PREFIX), value)
            for name, value in form.multi_items()
            if name.startswith(FIELD_PREFIX)
        ]
    )
    return read_post(spec.screen, answers, spec.conditions)


def read_answers(spec: FormSpec, form: FormData) -> dict[str, Any]:
    """Lê os seis tipos existentes; o serviço valida inclusive alternativas adulteradas."""
    return read_fields(spec, form).answers


def context(
    draft: Draft,
    *,
    values: Mapping[str, list[str]] | None = None,
    problems: Sequence[Problem] = (),
    input_mode: str = "fields",
    expected: str | None = None,
    json_id: str = "f-answers",
) -> dict[str, Any]:
    """Preserva valores permitidos mesmo com erro, sem exigir consentimento público."""
    errors = {
        problem.path.removeprefix("answers."): problem.message
        for problem in problems
        if problem.path.startswith("answers.")
    }
    try:
        spec = compile_definition(draft.document, version_number=draft.next_number)
        if spec.screen is None:
            raise DefinitionError([Problem("answers", "O rascunho não tem campos para simular.")])
        steps = steps_view(spec, spec.screen, values)
        steps = tuple(
            replace(
                step,
                questions=tuple(
                    replace(
                        view,
                        error=errors.get(view.question.name),
                        question=replace(view.question, name=FIELD_PREFIX + view.question.name),
                    )
                    for view in step.questions
                ),
            )
            for step in steps
        )
        conditions = conditions_data(spec)
    except DefinitionError:
        steps, conditions = (), None
    return {
        "steps": steps,
        "conditions": conditions,
        "input_mode": input_mode,
        "expected": draft.token if expected is None else expected,
        "current_version": draft.token,
        "json_id": json_id,
        "date_errors": [p.message for p in problems if p.path == "date"],
        "json_errors": [p.message for p in problems if p.path != "date"]
        if input_mode == "json"
        else [],
    }
