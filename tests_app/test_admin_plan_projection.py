"""Consulta por audiência nas fronteiras HTTP/admin/MCP e payload de entrega."""

import json

import sqlalchemy as sa
from admin_api_support import built_openapi, url_of
from conftest import publish
from test_admin import login
from test_intake import submit
from test_plan_projections import SENTINEL, projection_definition

from app import jobs, pipeline
from app.admin_mcp.catalog import load, select
from app.forms.definition import compile_definition
from app.models import Plan


def generated(deps):
    spec = compile_definition(projection_definition(), version_number=2)
    publish(deps, spec)
    job = submit(deps, form=spec).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic-run")
    pipeline.generate_plan(deps, job.id, 1)
    return job


def test_projection_http_and_html_have_no_hidden_internal_document(deps, api_client):
    job = generated(deps)
    path = url_of("get_job_plan_projection", public_id=job.public_id)
    reader = api_client("leitura")
    assert reader.get(path).status_code == 403
    client = api_client("total")
    public = client.get(path)
    assert public.status_code == 200, public.text
    assert public.json()["lead_data"]["document"] == {
        "titulo": "Plano público",
        "group": {"empty": ""},
        "items": [{"public": "0"}],
        "empty_list": [],
    }
    assert SENTINEL not in public.text
    internal = client.get(path, params={"audience": "internal"})
    assert internal.status_code == 200 and SENTINEL in internal.text
    assert "answers" not in internal.json() and "lead" not in internal.json()
    assert login(client).status_code == 303
    page = client.get(f"/admin/jobs/{job.public_id}/participant")
    assert page.status_code == 200 and "Plano da participante" in page.text
    assert SENTINEL not in page.text
    with deps.sessions() as session:
        assert SENTINEL in json.dumps(
            session.scalar(sa.select(Plan).where(Plan.job_id == job.id)).document
        )


def test_projection_mcp_requires_total_and_explicit_lead_data_opt_in():
    tools = load(built_openapi()).tools.values()
    for role, opt_in, present in (
        ("leitura", True, False),
        ("total", False, False),
        ("total", True, True),
    ):
        assert (
            "get_job_plan_projection"
            in {tool.name for tool in select(tools, role=role, lead_data=opt_in)}
        ) is present


def test_projection_unavailable_and_invalid_audience_echo_no_persisted_values(deps, api_client):
    job = generated(deps)
    with deps.sessions.begin() as session:
        plan = session.scalar(sa.select(Plan).where(Plan.job_id == job.id))
        plan.document = {"titulo": SENTINEL}
    client = api_client("total")
    path = url_of("get_job_plan_projection", public_id=job.public_id)
    refused = client.get(path)
    assert refused.status_code == 409 and SENTINEL not in refused.text
    invalid = client.get(path, params={"audience": SENTINEL})
    assert invalid.status_code == 422 and SENTINEL not in invalid.text


def test_participant_payload_contains_only_projected_document_and_allowed_references(deps):
    from app.models import Artifact, Delivery

    job = generated(deps)
    with deps.sessions() as session:
        plan = session.scalar(sa.select(Plan).where(Plan.job_id == job.id))
    artifact_id, _ = pipeline.render_pdf(deps, job.id, 1, plan.id)
    pipeline.finalize(deps, job.id, 1, artifact_id)
    with deps.sessions() as session:
        artifact = session.get_one(Artifact, artifact_id)
        delivery = Delivery(
            id=job.id,
            job_id=job.id,
            artifact_id=artifact.id,
            event="plano.concluido",
            audience="participant",
            authorization_origin="explicit",
            authorized_attempt=1,
        )
        payload = pipeline.build_payload(session, deps, delivery)
    assert SENTINEL not in json.dumps(payload)
    assert set(payload) == {
        "event",
        "event_id",
        "occurred_at",
        "form",
        "job",
        "plan",
        "pdf",
        "audience",
    }
    assert set(payload["job"]) == {"public_id", "attempt", "status_url", "page_url"}


def test_reading_marks_parent_inheritance_without_changing_authored_classification():
    from app.admin.reading import reading_of
    from app.forms.definition.document import parse

    document = projection_definition()
    view = reading_of(compile_definition(document), parse(document))
    parent = next(field for field in view.plan if field.name == "parent")
    assert parent.internal is True
    assert parent.fields[0].internal is False
    assert parent.fields[0].inherited_internal is True
    assert "internal" not in document["plan"]["fields"][4]["fields"][0]
