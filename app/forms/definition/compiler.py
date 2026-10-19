"""A compilação: o documento conferido vira o contrato do formulário (`FormSpec`).

A conferência segue uma ordem e para no primeiro grupo de problemas, com todos os problemas do
grupo: a estrutura do documento; as regras entre os campos (nomes repetidos, identidade,
limites, condições, a pontuação); com os modelos montados, os exemplos (as respostas, o plano e a
pontuação das respostas de exemplo); e, por fim, os templates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from app.forms import FIRST_VERSION, FormSpec
from app.forms.definition import plan as plan_tree
from app.forms.definition import questions
from app.forms.definition.conditions import Conditions, condition_problems
from app.forms.definition.data import data_problems
from app.forms.definition.document import (
    DefinitionError,
    FormDocument,
    Problem,
    parse,
    validation_problems,
)
from app.forms.definition.notices import notices_of
from app.forms.definition.phases import build_calendar, phases_problems
from app.forms.definition.scoring import compile_scoring, example_problems, scoring_problems
from app.forms.definition.templates import compile_templates, released_problems

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stop_on(problems: list[Problem]) -> None:
    if problems:
        raise DefinitionError(problems)


def _rule_problems(definition: FormDocument) -> list[Problem]:
    return (
        questions.question_problems(definition)
        + condition_problems(definition)
        + plan_tree.plan_problems(definition.plan, definition.data)
        + data_problems(definition.data)
        + phases_problems(definition)
        + scoring_problems(definition)
        + released_problems(definition)
    )


def _example(
    model: type[BaseModel], value: Mapping[str, Any], path: str, problems: list[Problem]
) -> BaseModel | None:
    """O exemplo validado pelo modelo da própria versão, ou None com os problemas anotados."""
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        problems += validation_problems(exc, path)
        return None


def compile_definition(
    document: Mapping[str, Any] | str | bytes,
    *,
    version_number: int = FIRST_VERSION,
    clock: Clock = utc_now,
) -> FormSpec:
    """O contrato do formulário da definição. Levanta `DefinitionError` (formato e parâmetros em
    `app.forms.definition`)."""
    definition = parse(document)
    _stop_on(_rule_problems(definition))
    conditions = Conditions(definition.steps)
    answers = questions.answers_model(definition.steps, conditions)
    plan = plan_tree.plan_model(definition.plan, definition.data)
    problems: list[Problem] = []
    example_answers = _example(answers, definition.examples.answers, "examples.answers", problems)
    example_plan = _example(plan, definition.examples.plan, "examples.plan", problems)
    _stop_on(problems)
    assert example_answers is not None and example_plan is not None
    label = f"{definition.slug}-v{version_number}"
    scoring = compile_scoring(definition, label=label)
    _stop_on(example_problems(scoring, example_answers))
    identity = questions.identity_of(definition.identity)
    calendar = build_calendar(definition)
    templates, instructions = compile_templates(
        definition,
        plan_model=plan,
        example_answers=example_answers,
        example_plan=example_plan,
        lead_name=identity(example_answers).name,
        clock=clock,
        scoring=scoring,
        calendar=calendar,
    )
    return FormSpec(
        slug=definition.slug,
        version=label,
        title=definition.title,
        consent_text_version=definition.consent.version,
        answers_model=answers,
        conditions=conditions,
        plan_model=plan,
        plan_labels=plan_tree.plan_labels(definition.plan.fields),
        identity=identity,
        instructions=instructions,
        prompt_template=definition.prompt,
        prompt_version=label,
        example_plan=example_plan.model_dump(mode="json"),
        version_number=version_number,
        score=scoring.score if scoring is not None else None,
        scoring=scoring,
        check_plan=(calendar.checker(clock) if calendar is not None and calendar.fields else None),
        screen=replace(
            questions.screen_of(
                intro=definition.intro,
                minutes=definition.minutes,
                steps=definition.steps,
                consent=definition.consent,
                identity=definition.identity,
            ),
            notices=notices_of(definition.notices),  # os avisos da espera e da falha
        ),
        templates=templates,
    )
