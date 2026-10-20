"""Projeções pela versão histórica e representações destinadas à participante."""

import copy
import json
from typing import Any

import pytest
from conftest import legacy_definition

from app.forms.definition import compile_definition

SENTINEL = "INTERNAL_SYNTHETIC_SENTINEL"


def projection_definition() -> dict[str, Any]:
    document = legacy_definition("demo")
    document["plan"] = {
        "fields": [
            {"name": "titulo", "kind": "text"},
            {"name": "secret", "kind": "text", "internal": True},
            {
                "name": "group",
                "kind": "group",
                "fields": [
                    {"name": "empty", "kind": "text"},
                    {"name": "secret", "kind": "text", "internal": True},
                ],
            },
            {
                "name": "items",
                "kind": "list",
                "items": {
                    "kind": "group",
                    "fields": [
                        {"name": "public", "kind": "text"},
                        {"name": "secret", "kind": "text", "internal": True},
                    ],
                },
            },
            {
                "name": "parent",
                "kind": "group",
                "internal": True,
                "fields": [
                    {"name": "child", "kind": "text"},
                ],
            },
            {"name": "empty_list", "kind": "list", "items": {"kind": "text"}},
        ]
    }
    document["examples"]["plan"] = {
        "titulo": "Plano público",
        "secret": SENTINEL,
        "group": {"empty": "", "secret": SENTINEL},
        "items": [{"public": "0", "secret": SENTINEL}],
        "parent": {"child": SENTINEL},
        "empty_list": [],
    }
    document["pdf"]["html"] = document["preview"] = "<p>{{ plan.titulo }}</p>"
    return document


def test_projection_is_recursive_json_and_preserves_internal_document() -> None:
    definition = projection_definition()
    spec = compile_definition(definition, version_number=7)
    document = definition["examples"]["plan"]
    original = copy.deepcopy(document)
    projected = spec.project_plan(document)
    assert projected == {
        "titulo": "Plano público",
        "group": {"empty": ""},
        "items": [{"public": "0"}],
        "empty_list": [],
    }
    assert SENTINEL not in json.dumps(projected)
    assert spec.project_plan(document, audience="internal") == original
    projected["items"][0]["public"] = "Alterada"
    assert document == original


def test_invalid_documents_and_missing_classification_fail_without_echo() -> None:
    import traceback
    from dataclasses import replace

    import pytest

    from app.forms import PlanProjectionError

    definition = projection_definition()
    spec = compile_definition(definition)
    document = definition["examples"]["plan"]
    for invalid in (
        None,
        SENTINEL,
        [],
        {**document, "titulo": {"secret": SENTINEL}},
        {**document, "extra": SENTINEL},
        {"titulo": SENTINEL},
    ):
        with pytest.raises(PlanProjectionError) as raised:
            spec.project_plan(invalid)
        assert raised.value.code == "plan_projection_invalid"
        assert SENTINEL not in "".join(traceback.format_exception(raised.value))
    for unavailable in (
        replace(spec, plan_projector=None),
        replace(spec, plan_model=None),
        replace(spec, generation="none"),
        replace(spec, version=""),
        replace(spec, version_number=0),
    ):
        for audience in ("internal", "participant"):
            with pytest.raises(PlanProjectionError) as raised:
                unavailable.project_plan(document, audience=audience)
            assert raised.value.code == "plan_projection_unavailable"
    with pytest.raises(PlanProjectionError) as raised:
        spec.project_plan(document, audience=SENTINEL)
    assert SENTINEL not in "".join(traceback.format_exception(raised.value))


def test_historical_classification_is_used_and_result_is_independent() -> None:
    definition = projection_definition()
    historical = compile_definition(definition, version_number=1)
    newer_definition = copy.deepcopy(definition)
    newer_definition["plan"]["fields"][1]["internal"] = False
    newer = compile_definition(newer_definition, version_number=2)
    document = definition["examples"]["plan"]
    assert "secret" not in historical.project_plan(document)
    assert newer.project_plan(document)["secret"] == SENTINEL
    internal = historical.project_plan(document, audience="internal")
    internal["items"][0]["secret"] = "Alterada"
    assert document["items"][0]["secret"] == SENTINEL


def test_pdf_preview_and_hidden_html_receive_only_participant_projection() -> None:
    from io import BytesIO

    from pypdf import PdfReader
    from test_form_definition import pdf_context

    from app.adapters.weasyprint_pdf import WeasyPrintRenderer

    definition = projection_definition()
    definition["pdf"]["html"] = definition["preview"] = (
        "<h1>{{ plan.titulo }}</h1><p>{{ plan.items[0].public }}</p>"
        "<div hidden>{{ plan }}</div><script type='application/json'>{{ plan.group }}</script>"
        "<p>{{ brand.running_header() }}</p>"
    )
    spec = compile_definition(definition)
    document = definition["examples"]["plan"]
    context = pdf_context(spec)
    page = {key: value for key, value in context.items() if key != "generated_at"}
    page["generated_date"] = "20 de outubro de 2026"
    html = spec.templates.render_pdf(spec.plan_model.model_validate(document), **page)
    preview = spec.templates.render_preview(document, **page)
    for representation in (html, preview):
        assert SENTINEL not in representation
        assert "secret" not in representation
        assert "parent" not in representation
        assert "Plano público" in representation
    rendered = WeasyPrintRenderer().render(spec, document, context)
    text = "".join(page.extract_text() for page in PdfReader(BytesIO(rendered.content)).pages)
    assert "Plano público" in text
    assert SENTINEL not in text
    assert spec.project_plan(document, audience="internal") == document


@pytest.mark.parametrize(
    "expression",
    [
        "plan['secret']",
        "plan.group['secret']",
        "plan.items[0]['secret']",
        "plan['parent']['child']",
        "plan.group|attr('secret')",
    ],
)
def test_alternative_internal_access_is_refused_without_echo(expression: str) -> None:
    import traceback

    from test_form_definition import pdf_context

    from app.forms.sandbox import SandboxTemplate, TemplateProblem

    spec = compile_definition(projection_definition())
    # Simula um template histórico; o compilador também recusa acessos diretos na autoria.
    from dataclasses import replace

    with pytest.raises(TemplateProblem) as raised:
        templates = replace(
            spec.templates,
            preview=SandboxTemplate(
                "{{ " + expression + " }}",
                name="preview",
                html=True,
            ),
        )
        context = pdf_context(spec)
        context.pop("generated_at")
        templates.render_preview(spec.example_plan, generated_date="", **context)
    assert SENTINEL not in "".join(traceback.format_exception(raised.value))


def test_pdf_template_revalidates_constructed_model_without_serialization_echo() -> None:
    import traceback
    import warnings

    from test_form_definition import pdf_context

    from app.forms.sandbox import TemplateProblem

    spec = compile_definition(projection_definition())
    forged = spec.plan_model.model_construct(
        **{**spec.example_plan, "titulo": {"secret": SENTINEL}}
    )
    context = pdf_context(spec)
    context.pop("generated_at")
    with warnings.catch_warnings(record=True) as recorded, pytest.raises(TemplateProblem) as raised:
        spec.templates.render_pdf(forged, generated_date="", **context)
    assert SENTINEL not in "".join(str(item.message) for item in recorded)
    assert SENTINEL not in "".join(traceback.format_exception(raised.value))
