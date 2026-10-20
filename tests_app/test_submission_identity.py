"""Cada pedido usa o contato do próprio envio, mesmo com e-mail compartilhado."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from admin_api_support import api_client
from conftest import FakeBaserow, record_webhook
from fastapi.testclient import TestClient
from test_integrations import connect_form, sync_ids

from app import jobs, pipeline
from app.admin.queries import jobs as queries
from app.api.main import create_app
from app.deps import Deps
from app.errors import FailureCode, RetryableError, TerminalError
from app.forms import FormSpec, starter
from app.forms.definition import compile_definition, fingerprint
from app.forms.stored import import_definition
from app.intake import Accepted, SubmissionIn, accept_submission
from app.integrations import push
from app.integrations.mapping import Entry
from app.models import Artifact, Form, FormVersion, Lead, Submission, utcnow

EMAIL = "contato@example.com"
ORIGINAL = {"email": EMAIL, "name": "Titular Exemplo", "phone": "11900000001"}
LATER = {"email": EMAIL, "name": "Outra Exemplo", "phone": "11900000002"}


def contact_form(
    deps: Deps, slug: str, *, phone: bool = True, optional_phone: bool = False
) -> FormSpec:
    document = starter.definition(slug=slug, title="Diagnóstico de exemplo")
    if not phone:
        document["identity"]["phone"] = None
        document["consent"]["marketing"]["whatsapp"] = None
    if optional_phone:
        for step in document["steps"]:
            for question in step["questions"]:
                if question["name"] == "whatsapp":
                    question["required"] = False
    import_definition(deps, json.dumps(document), actor="testes")
    return deps.forms.current(slug)


def contact_submission(
    deps: Deps, spec: FormSpec, key: str, contact: dict[str, Any], *, renamed: bool = False
) -> Accepted:
    document = starter.definition(slug=spec.slug, title=spec.title)
    answers = {**document["examples"]["answers"], "nome": contact["name"], "email": EMAIL}
    answers["whatsapp"] = contact["phone"]
    if renamed:
        for key_name in ("nome", "email", "whatsapp"):
            answers[f"new_{key_name}"] = answers.pop(key_name)
    with deps.sessions.begin() as session:
        return accept_submission(
            session,
            spec,
            SubmissionIn(
                answers=answers,
                consent={"privacy": True, "text_version": spec.consent_text_version},
            ),
            idempotency_key=key,
            signer=deps.signer,
            link_ttl=deps.link_ttl,
            limits=deps.submission_limits,
        )


def complete(deps: Deps, accepted: Accepted) -> tuple[uuid.UUID, list[uuid.UUID]]:
    job = accepted.job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "test-run")
    plan_id, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact_id, _ = pipeline.render_pdf(deps, job.id, 1, plan_id)
    return artifact_id, pipeline.finalize(deps, job.id, 1, artifact_id)


@pytest.mark.parametrize("cross_form", [False, True])
@pytest.mark.parametrize("consumer", ["webhook", "pdf", "sync", "public", "admin", "api", "search"])
def test_later_anonymous_submission_cannot_change_an_older_order(
    deps: Deps, cross_form: bool, consumer: str
) -> None:
    spec = contact_form(deps, "contact-original")
    first = contact_submission(deps, spec, "original", ORIGINAL)
    second_spec = contact_form(deps, "contact-later") if cross_form else spec
    second = contact_submission(deps, second_spec, "later", LATER)
    with deps.sessions() as session:
        old = session.get_one(Submission, first.job.submission_id)
        new = session.get_one(Submission, second.job.submission_id)
        assert old.lead_id == new.lead_id
        assert old.answers["whatsapp"] == ORIGINAL["phone"]

    if consumer == "webhook":
        webhook = record_webhook(deps)
        for accepted, expected in ((first, ORIGINAL), (second, LATER)):
            _, deliveries = complete(deps, accepted)
            assert pipeline.deliver(deps, deliveries[0]) == "succeeded"
            payload = webhook.calls[-1][2]
            assert payload["lead"] == expected
            assert payload["answers"]["whatsapp"] == expected["phone"]
            assert payload["job"]["public_id"] == accepted.job.public_id
    elif consumer == "pdf":
        contexts = []
        renderer = deps.pdf

        class CapturePdf:
            def render(self, spec, document, context):
                contexts.append(context)
                return renderer.render(spec, document, context)

        deps.pdf = CapturePdf()
        for accepted, expected in ((first, ORIGINAL), (second, LATER)):
            complete(deps, accepted)
            assert contexts[-1]["lead"]["name"] == expected["name"]
    elif consumer == "sync":
        baserow = FakeBaserow().install(deps)
        entries = [
            Entry(101, "Nome", "text", "lead.name"),
            Entry(102, "E-mail", "email", "lead.email"),
            Entry(103, "Telefone", "phone_number", "lead.phone"),
            Entry(104, "Pedido", "text", "job.public_id"),
        ]
        for slug in {spec.slug, second_spec.slug}:
            connect_form(deps, slug=slug, entries=entries)
        for accepted, expected in ((first, ORIGINAL), (second, LATER)):
            complete(deps, accepted)
            (sync_id,) = sync_ids(deps, accepted.job.id)
            assert push.push(deps, sync_id) == "succeeded"
            row = baserow.row_where(104, accepted.job.public_id)
            assert [row[f"field_{number}"] for number in (101, 102, 103)] == [
                expected["name"],
                EMAIL,
                expected["phone"],
            ]
    elif consumer == "public":
        for accepted, expected in ((first, ORIGINAL), (second, LATER)):
            complete(deps, accepted)
            with TestClient(create_app()) as client:
                response = client.get(f"/d/{accepted.job.public_id}?t={accepted.token}")
            assert response.status_code == 200
            assert f"Seu plano está pronto, {expected['name'].split()[0]}" in response.text
    elif consumer == "admin":
        with deps.sessions() as session:
            detail = queries.read_job(
                session, first.job.public_id, forms=deps.forms, now=utcnow(), with_lead=True
            )
            assert detail.lead is not None
            assert (detail.lead.name, detail.lead.email, detail.lead.phone) == (
                ORIGINAL["name"],
                EMAIL,
                ORIGINAL["phone"],
            )
            rows = queries.jobs_by_page(
                session, [], forms=deps.forms, limit=25, offset=0, with_lead=True
            )
            names = {row.state.public_id: row.lead.name for row in rows}
            assert names == {
                first.job.public_id: ORIGINAL["name"],
                second.job.public_id: LATER["name"],
            }
    elif consumer == "api":
        with api_client(deps) as client:
            response = client.get(
                f"/admin/api/v1/jobs/{first.job.public_id}?include_lead_data=true"
            )
            assert response.status_code == 200
            assert response.json()["lead_data"]["name"] == ORIGINAL["name"]
    else:
        with deps.sessions() as session:
            for expected, accepted in ((ORIGINAL, first), (LATER, second)):
                conditions = queries.JobFilters(q=expected["name"]).conditions()
                rows = queries.jobs_by_page(
                    session, conditions, forms=deps.forms, limit=25, offset=0, with_lead=True
                )
                assert [row.state.public_id for row in rows] == [accepted.job.public_id]


def test_a_form_without_a_phone_identity_does_not_inherit_another_forms_phone(deps: Deps) -> None:
    first_spec = contact_form(deps, "with-phone")
    contact_submission(deps, first_spec, "original", ORIGINAL)
    second_spec = contact_form(deps, "without-phone", phone=False)
    second = contact_submission(deps, second_spec, "later", LATER)
    webhook = record_webhook(deps)
    _, deliveries = complete(deps, second)
    pipeline.deliver(deps, deliveries[0])
    assert webhook.calls[-1][2]["lead"] == {**LATER, "phone": None}
    with deps.sessions() as session:
        detail = queries.read_job(
            session, second.job.public_id, forms=deps.forms, now=utcnow(), with_lead=True
        )
    assert detail.lead.phone is None


@pytest.mark.parametrize("resend", [False, True])
def test_stored_pdf_is_reused_and_delivery_retry_keeps_the_original_contact(
    deps: Deps, resend: bool
) -> None:
    spec = contact_form(deps, "contact-retry")
    first = contact_submission(deps, spec, "original", ORIGINAL)
    webhook = record_webhook(
        deps, error=RetryableError(FailureCode.DELIVERY_UNAVAILABLE, "HTTP 503")
    )
    artifact_id, deliveries = complete(deps, first)
    with pytest.raises(RetryableError):
        pipeline.deliver(deps, deliveries[0])
    if resend:
        pipeline.mark_delivery_failed(deps, deliveries[0], "delivery_unavailable")
    with deps.sessions() as session:
        artifact = session.get_one(Artifact, artifact_id)
        before = deps.storage.get(artifact.storage_key)
    contact_submission(deps, spec, "later", LATER)
    assert pipeline.render_pdf(deps, first.job.id, 1, artifact.plan_id) == (artifact_id, True)
    assert deps.storage.get(artifact.storage_key) == before
    webhook.error = None
    if resend:
        with deps.sessions.begin() as session:
            assert jobs.resend_delivery(
                session,
                first.job.id,
                deliveries[0],
                actor="testes",
                expected_config_version=1,
                expected_attempt=1,
            )
    pipeline.deliver(deps, deliveries[0], dispatch_no=2 if resend else 1)
    assert webhook.calls[-1][2]["lead"] == ORIGINAL


def test_anonymous_submissions_preserve_the_deduplicated_profile(deps: Deps) -> None:
    spec = contact_form(deps, "contact-profile")
    first = contact_submission(deps, spec, "original", ORIGINAL)
    contact_submission(deps, spec, "later", LATER)
    with deps.sessions() as session:
        submission = session.get_one(Submission, first.job.submission_id)
        lead = session.get_one(Lead, submission.lead_id)
        assert (lead.name, lead.phone) == (ORIGINAL["name"], ORIGINAL["phone"])


def test_unanswered_optional_phone_is_not_inherited(deps: Deps) -> None:
    spec = contact_form(deps, "optional-phone", optional_phone=True)
    contact_submission(deps, spec, "original", ORIGINAL)
    second = contact_submission(deps, spec, "later", {**LATER, "phone": None})
    webhook = record_webhook(deps)
    _, deliveries = complete(deps, second)
    pipeline.deliver(deps, deliveries[0])
    assert webhook.calls[-1][2]["lead"]["phone"] is None


def test_new_identity_mapping_cannot_change_the_contact_of_the_previous_version(deps: Deps) -> None:
    spec = contact_form(deps, "contact-version")
    first = contact_submission(deps, spec, "original", ORIGINAL)
    document = starter.definition(slug=spec.slug, title=spec.title)
    for role, key in document["identity"].items():
        document["identity"][role] = f"new_{key}"
        document["examples"]["answers"][f"new_{key}"] = document["examples"]["answers"].pop(key)
        for step in document["steps"]:
            for question in step["questions"]:
                if question["name"] == key:
                    question["name"] = f"new_{key}"
    compile_definition(document, version_number=2)
    with deps.sessions.begin() as session:
        form_id = session.scalar(sa.select(Form.id).where(Form.slug == spec.slug))
        session.add(
            FormVersion(
                form_id=form_id,
                number=2,
                definition=document,
                fingerprint=fingerprint(document),
                note="Nova identidade de exemplo",
                published_by="testes",
            )
        )
    second = contact_submission(deps, deps.forms.current(spec.slug), "later", LATER, renamed=True)
    webhook = record_webhook(deps)
    for accepted, expected in ((first, ORIGINAL), (second, LATER)):
        _, deliveries = complete(deps, accepted)
        pipeline.deliver(deps, deliveries[0])
        assert webhook.calls[-1][2]["lead"] == expected
        with deps.sessions() as session:
            conditions = queries.JobFilters(q=expected["name"]).conditions()
            rows = queries.jobs_by_page(
                session, conditions, forms=deps.forms, limit=25, offset=0, with_lead=True
            )
            assert [row.state.public_id for row in rows] == [accepted.job.public_id]


@pytest.mark.parametrize("broken", ["version", "answers"])
def test_unresolvable_contact_stops_delivery_and_sync_without_guessing(
    deps: Deps, broken: str
) -> None:
    spec = contact_form(deps, "contact-unavailable")
    first = contact_submission(deps, spec, "original", ORIGINAL)
    baserow = FakeBaserow().install(deps)
    connect_form(deps, slug=spec.slug, entries=[Entry(104, "Pedido", "text", "job.public_id")])
    webhook = record_webhook(deps)
    artifact_id, deliveries = complete(deps, first)
    with deps.sessions.begin() as session:
        submission = session.get_one(Submission, first.job.submission_id)
        if broken == "version":
            submission.form_version_number = 99
        else:
            submission.answers = {**submission.answers, "email": "invalid-example"}
    with pytest.raises(TerminalError) as failure:
        pipeline.deliver(deps, deliveries[0])
    assert ORIGINAL["name"] not in str(failure.value) and EMAIL not in str(failure.value)
    (sync_id,) = sync_ids(deps, first.job.id)
    with pytest.raises(TerminalError):
        push.push(deps, sync_id)
    assert not webhook.calls and not baserow.requests
    with deps.sessions() as session:
        assert queries.read_job(
            session, first.job.public_id, forms=deps.forms, now=utcnow(), with_lead=False
        )
        detail = queries.read_job(
            session, first.job.public_id, forms=deps.forms, now=utcnow(), with_lead=True
        )
        assert detail.lead is not None
        assert (detail.lead.name, detail.lead.email, detail.lead.phone) == (None, None, None)
        assert detail.lead.answers == session.get_one(Submission, first.job.submission_id).answers
        artifact = session.get_one(Artifact, artifact_id)
    assert pipeline.render_pdf(deps, first.job.id, 1, artifact.plan_id) == (artifact_id, True)


def test_pdf_without_the_historical_version_fails_before_rendering(deps: Deps) -> None:
    spec = contact_form(deps, "render-unavailable")
    first = contact_submission(deps, spec, "original", ORIGINAL)
    with deps.sessions.begin() as session:
        jobs.claim(session, first.job.id, 1, "test-run")
    plan_id, _ = pipeline.generate_plan(deps, first.job.id, 1)
    with deps.sessions.begin() as session:
        submission = session.get_one(Submission, first.job.submission_id)
        submission.form_version_number = 99
    with pytest.raises(TerminalError):
        pipeline.render_pdf(deps, first.job.id, 1, plan_id)
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Artifact)) == 0
