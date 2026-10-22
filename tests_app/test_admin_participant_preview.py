"""Prévia real pela tentativa observada, sem dados internos nem efeitos de geração."""

import json

import pytest
import sqlalchemy as sa
from test_admin_plan_projection import generated
from test_plan_projections import SENTINEL

from app.models import Job


def test_result_preview_renders_required_internal_fields_without_exposing_them(deps):
    from app.admin.participant_preview import render_job_result

    job = generated(deps)
    result = render_job_result(deps, job.public_id)
    assert result is not None
    assert "Plano público" in result.html
    assert SENTINEL not in result.html + json.dumps(result.projection.document)
    assert result.projection.attempt == 1
    assert result.pdf_state.state == "missing"
    assert 'aria-disabled="true"' in result.html
    assert 'href="/d/' not in result.html
    assert not hasattr(result, "raw")


def test_result_preview_never_falls_back_to_previous_attempt(deps):
    import pytest

    from app.admin.participant_preview import PreviewUnavailable, render_job_result

    job = generated(deps)
    with deps.sessions.begin() as session:
        session.get_one(Job, job.id).attempt = 2
    with pytest.raises(PreviewUnavailable):
        render_job_result(deps, job.public_id)
    assert render_job_result(deps, "missing") is None


def test_result_preview_api_is_personal_html_with_safe_errors(deps, api_client):
    job = generated(deps)
    path = f"/admin/api/v1/jobs/{job.public_id}/participant-preview"
    assert api_client("leitura").get(path).status_code == 403
    response = api_client("total").get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["content-disposition"] == 'inline; filename="previa-participante.html"'
    assert "sandbox" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "script-src" not in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "<script" not in response.text
    assert SENTINEL not in response.text
    assert (
        api_client("total").get("/admin/api/v1/jobs/missing/participant-preview").status_code == 404
    )
    with deps.sessions.begin() as session:
        session.execute(sa.update(Job).where(Job.id == job.id).values(attempt=2))
    refused = api_client("total").get(path)
    assert refused.status_code == 409
    assert refused.json()["code"] == "state_conflict"


def test_result_preview_uses_historical_template_identity_scores_and_date(deps):
    from datetime import UTC, datetime

    from conftest import publish
    from test_intake import submit
    from test_plan_projections import projection_definition

    from app import jobs, pipeline
    from app.admin.participant_preview import render_job_result
    from app.forms.definition import compile_definition
    from app.models import Plan

    definition = projection_definition()
    definition["slug"] = "historical-preview"
    definition["preview"] = (
        "<p>Histórico {{ lead.name }} {{ generated_date }} {{ plan.titulo }}</p>"
    )
    v1 = compile_definition(definition, version_number=1)
    publish(deps, v1)
    job = submit(deps, form=v1).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic-run")
    pipeline.generate_plan(deps, job.id, 1)
    with deps.sessions.begin() as session:
        plan = session.scalar(sa.select(Plan).where(Plan.job_id == job.id))
        plan.created_at = datetime(2024, 2, 10, 15, tzinfo=UTC)
    definition["preview"] = "<p>Versão nova</p>"
    v2 = compile_definition(definition, version_number=2)
    publish(deps, v1, v2)
    result = render_job_result(deps, job.public_id)
    assert "Histórico" in result.html
    assert "10 de fevereiro de 2024" in result.html
    assert "Versão nova" not in result.html
    assert result.projection.form_version_number == 1


def test_result_preview_fails_closed_without_history_or_template(deps):
    from dataclasses import replace

    import pytest
    from conftest import FixedRegistry

    from app.admin.participant_preview import PreviewUnavailable, render_job_result

    job = generated(deps)
    historical = deps.forms.get("demo", 2)
    for registry in (FixedRegistry([]), FixedRegistry([replace(historical, templates=None)])):
        deps.forms = registry
        with pytest.raises(PreviewUnavailable) as error:
            render_job_result(deps, job.public_id)
        assert SENTINEL not in str(error.value)


def test_result_preview_held_plan_is_reviewable_without_release_or_writes(deps, monkeypatch):
    from conftest import publish
    from test_intake import submit
    from test_plan_projections import projection_definition

    from app import jobs, pipeline
    from app.admin.participant_preview import render_job_result
    from app.forms.definition import compile_definition
    from app.models import AuditLog, Delivery, Plan, PlanRelease

    definition = projection_definition()
    definition["slug"] = "held-preview"
    definition["release"] = {"mode": "manual"}
    spec = compile_definition(definition, version_number=1)
    publish(deps, spec)
    job = submit(deps, form=spec).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic-run")
    pipeline.generate_plan(deps, job.id, 1)

    def counts():
        with deps.sessions() as session:
            return tuple(
                session.scalar(sa.select(sa.func.count()).select_from(model))
                for model in (AuditLog, Delivery, Plan, PlanRelease)
            )

    def forbidden(*args, **kwargs):
        raise AssertionError("A prévia não pode executar efeitos externos ou gerar arquivos.")

    monkeypatch.setattr(deps.llm, "generate", forbidden)
    monkeypatch.setattr(deps.pdf, "render", forbidden)
    monkeypatch.setattr(deps.webhook, "send", forbidden)
    monkeypatch.setattr(deps.storage, "presigned_url", forbidden)
    monkeypatch.setattr(deps.storage, "put", forbidden)
    before = counts()
    result = render_job_result(deps, job.public_id)
    assert result.access_state.state == "held"
    assert "Plano retido" in result.html and "Plano público" in result.html
    assert counts() == before


def test_result_preview_mcp_contract_requires_opt_in_and_is_read_only():
    from admin_api_support import built_openapi

    from app.admin_mcp.catalog import load, select

    tools = load(built_openapi()).tools
    operation = tools["render_job_participant_preview"]
    assert operation.annotations == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
    }
    assert set(operation.input_schema["properties"]) == {"public_id"}
    for role, opt_in, present in (
        ("leitura", True, False),
        ("total", False, False),
        ("total", True, True),
    ):
        assert (
            operation.name
            in {tool.name for tool in select(tools.values(), role=role, lead_data=opt_in)}
        ) is present


def test_participant_template_embeds_escaped_sandboxed_document(deps):
    from dataclasses import asdict

    from jinja2 import ChoiceLoader, DictLoader
    from test_public import tags

    from app.admin.participant_preview import render_job_result
    from app.web.templating import templates

    job = generated(deps)
    result = render_job_result(deps, job.public_id)
    environment = templates.env.overlay(
        loader=ChoiceLoader(
            [
                DictLoader({"admin/layout.html": "{% block content %}{% endblock %}"}),
                templates.env.loader,
            ]
        )
    )
    rendered = environment.get_template("admin/participant_plan.html").render(
        projection=asdict(result.projection),
        result=result,
    )
    frames = tags(rendered, "iframe")
    assert len(frames) == 1
    assert frames[0]["sandbox"] == ""
    assert frames[0]["srcdoc"] == result.html
    assert "src" not in frames[0]
    assert "<main" not in rendered
    assert "Detalhes do plano em JSON" in rendered


def test_result_preview_and_reprocess_serialize_one_attempt(deps, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import pytest

    from app import jobs, pipeline
    from app.admin.participant_preview import PreviewUnavailable, render_job_result
    from app.models import Plan

    job = generated(deps)
    with deps.sessions() as session:
        plan = session.scalar(sa.select(Plan).where(Plan.job_id == job.id))
    artifact_id, _ = pipeline.render_pdf(deps, job.id, 1, plan.id)
    pipeline.finalize(deps, job.id, 1, artifact_id)
    entered, resume, started = Event(), Event(), Event()
    head = deps.storage.head

    def paused_head(key):
        entered.set()
        assert resume.wait(5)
        return head(key)

    monkeypatch.setattr(deps.storage, "head", paused_head)

    def reprocess():
        started.set()
        with deps.sessions.begin() as session:
            return jobs.reprocess(
                session, job.id, actor="operadora", reason="Teste concorrente", expected_attempt=1
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        preview = pool.submit(render_job_result, deps, job.public_id)
        try:
            assert entered.wait(5)
            changed = pool.submit(reprocess)
            assert started.wait(5)
            # A escritora não pode trocar a tentativa enquanto a observação está em preparo.
            with pytest.raises(TimeoutError):
                changed.result(timeout=0.2)
        finally:
            resume.set()
        observed = preview.result(timeout=5)
        assert changed.result(timeout=5) == 2
    assert observed.projection.attempt == observed.pdf_state.attempt == 1
    assert "Plano público" in observed.html
    with pytest.raises(PreviewUnavailable):
        render_job_result(deps, job.public_id)


def test_result_preview_mcp_saves_personal_html_privately_without_body(tmp_path):
    import hashlib
    import stat
    from pathlib import Path

    from admin_mcp_support import html
    from admin_mcp_world import World

    world = World(tmp_path, lead_data=True)
    content = "<html><p>Participante sintética</p></html>"
    try:
        world.api.when("GET", "/jobs/{public_id}/participant-preview", html(content))
        result = world.call("render_job_participant_preview", {"public_id": "synthetic-id"})
        assert not result.is_error
        fields = dict(line.split(": ", 1) for line in result.text.splitlines()[1:])
        path = Path(fields["caminho"])
        assert path.read_text() == content
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert fields["sha256"] == hashlib.sha256(content.encode()).hexdigest()
        assert "Participante sintética" not in result.text
        assert result.data is None
    finally:
        world.close()


def test_result_preview_api_authentication_rate_and_openapi(deps, api_client):
    from admin_api_support import built_openapi, make_api
    from test_admin import FakeClock

    job = generated(deps)
    path = f"/admin/api/v1/jobs/{job.public_id}/participant-preview"
    anonymous = make_api(deps, FakeClock())
    assert anonymous.get(path).status_code == 401
    client = api_client("total", API_RATE_LIMIT_PER_MINUTE="10")
    assert [client.get(path).status_code for _ in range(3)] == [200, 200, 429]
    assert client.get("/admin/api/v1/jobs/missing/participant-preview").status_code == 429
    operation = built_openapi()["paths"]["/jobs/{public_id}/participant-preview"]["get"]
    assert "requestBody" not in operation
    assert [(parameter["in"], parameter["name"]) for parameter in operation["parameters"]] == [
        ("path", "public_id")
    ]
    assert {"401", "403", "404", "409", "422", "429"} <= set(operation["responses"])
    assert operation["x-required-role"] == "total" and operation["x-lead-data"] is True


def test_result_preview_keeps_historical_images_after_asset_replacement(deps, api_client):
    from test_form_images_result import picture, publish_with_image
    from test_intake import submit

    from app import jobs, pipeline
    from app.admin.participant_preview import render_job_result
    from app.forms import assets

    logo = publish_with_image(deps)
    spec = deps.forms.current("demo")
    job = submit(deps, form=spec).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic-run")
    pipeline.generate_plan(deps, job.id, 1)
    replacement = assets.replace(
        deps, logo.id, picture((20, 40, 220)), expected=logo.ref.sha256, actor="operadora"
    ).asset
    deps.forms = type(deps.forms)(deps.sessions)
    observed = render_job_result(deps, job.public_id)
    assert logo.ref.url in observed.html
    assert replacement.ref.url not in observed.html


def test_result_preview_does_not_announce_an_artifact_from_another_plan(deps):
    from app import pipeline
    from app.admin.participant_preview import render_job_result
    from app.models import Artifact, Plan

    job = generated(deps)
    with deps.sessions() as session:
        plan = session.scalar(sa.select(Plan).where(Plan.job_id == job.id))
    artifact_id, _ = pipeline.render_pdf(deps, job.id, 1, plan.id)
    pipeline.finalize(deps, job.id, 1, artifact_id)
    with deps.sessions.begin() as session:
        previous = Plan(
            job_id=job.id,
            version=2,
            document=plan.document,
            model="synthetic",
            prompt_version="synthetic",
        )
        session.add(previous)
        session.flush()
        session.get_one(Artifact, artifact_id).plan_id = previous.id
    result = render_job_result(deps, job.public_id)
    assert not result.pdf_state.available
    assert "PDF da tentativa atual disponível" not in result.html


def test_result_preview_uses_only_persisted_released_scores(deps):
    from conftest import publish
    from test_intake import submit
    from test_pdf_scores import released

    from app import jobs, pipeline
    from app.admin.participant_preview import render_job_result
    from app.forms.definition import compile_definition
    from app.models import Submission

    definition = released("alcance")
    spec = compile_definition(definition, version_number=2)
    publish(deps, spec)
    job = submit(
        deps,
        form=spec,
        answers=definition["examples"]["answers"],
        consent={"privacy": True, "text_version": spec.consent_text_version},
    ).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic-run")
    pipeline.generate_plan(deps, job.id, 1)
    with deps.sessions.begin() as session:
        submission = session.get_one(Submission, job.submission_id)
        submission.scores = {"alcance": 123, "porte": SENTINEL, "persona": SENTINEL}
    result = render_job_result(deps, job.public_id)
    assert "[alcance=123]" in result.html
    assert SENTINEL not in result.html


def test_required_internal_summary_and_unsafe_template_fail_without_internal_fallback(deps):
    from dataclasses import replace

    import pytest
    from conftest import FixedRegistry, publish
    from test_intake import submit
    from test_plan_projections import projection_definition

    from app import jobs, pipeline
    from app.admin.participant_preview import PreviewUnavailable, render_job_result
    from app.forms.definition import compile_definition
    from app.forms.sandbox import SandboxTemplate

    definition = projection_definition()
    definition["plan"]["fields"][1]["name"] = "resumo"
    definition["examples"]["plan"]["resumo"] = definition["examples"]["plan"].pop("secret")
    spec = compile_definition(definition, version_number=2)
    publish(deps, spec)
    job = submit(deps, form=spec).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic-run")
    pipeline.generate_plan(deps, job.id, 1)
    result = render_job_result(deps, job.public_id)
    assert "Plano público" in result.html
    assert SENTINEL not in result.html + json.dumps(result.projection.document)
    unavailable = replace(
        spec.templates, preview=SandboxTemplate("{{ plan.resumo }}", name="preview", html=True)
    )
    deps.forms = FixedRegistry([replace(spec, templates=unavailable)])
    with pytest.raises(PreviewUnavailable) as error:
        render_job_result(deps, job.public_id)
    assert SENTINEL not in str(error.value)


@pytest.mark.parametrize("state", ["missing", "removed", "unknown", "unavailable", "previous"])
def test_result_preview_distinguishes_pdf_availability_from_access(deps, monkeypatch, state):
    from app import jobs, pipeline
    from app.admin.participant_preview import render_job_result
    from app.models import Artifact, Plan, utcnow

    job = generated(deps)
    with deps.sessions() as session:
        plan = session.scalar(sa.select(Plan).where(Plan.job_id == job.id))
    artifact_id, _ = pipeline.render_pdf(deps, job.id, 1, plan.id)
    pipeline.finalize(deps, job.id, 1, artifact_id)
    with deps.sessions.begin() as session:
        artifact = session.get_one(Artifact, artifact_id)
        if state == "missing":
            deps.storage.delete(artifact.storage_key)
        elif state == "removed":
            artifact.purged_at = utcnow()
        elif state == "unknown":
            artifact.verified_at = None
        elif state == "previous":
            jobs.reprocess(
                session,
                job.id,
                actor="operadora",
                reason="Teste da tentativa atual",
                expected_attempt=1,
            )
    if state == "unavailable":

        def refuse(key):
            raise OSError(SENTINEL)

        monkeypatch.setattr(deps.storage, "head", refuse)
    if state == "previous":
        pipeline.generate_plan(deps, job.id, 2)
    result = render_job_result(deps, job.public_id)
    assert result.pdf_state.state == ("missing" if state == "previous" else state)
    assert result.access_state.state == "automatic"
    assert not result.pdf_state.available
    assert SENTINEL not in result.html
    assert 'aria-disabled="true"' in result.html


def test_participant_page_and_api_serve_the_same_observed_document(deps, api_client):
    from test_admin import login
    from test_public import tags

    job = generated(deps)
    client = api_client("total")
    assert login(client).status_code == 303
    page = client.get(f"/admin/jobs/{job.public_id}/participant")
    assert page.status_code == 200, page.text
    response = client.get(f"/admin/api/v1/jobs/{job.public_id}/participant-preview")
    assert response.status_code == 200
    (frame,) = tags(page.text, "iframe")
    assert frame["srcdoc"] == response.text
    assert frame["sandbox"] == "" and "src" not in frame
    assert SENTINEL not in page.text
    assert "&lt;!doctype html&gt;" in page.text
