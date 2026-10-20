"""O fato PDF conserva receptor, revisão, audiência e tentativa autorizados."""

import pytest
from conftest import record_webhook
from test_pipeline import run_until_finalize

from app import destinations, pipeline
from app.errors import RetryableError
from app.models import Delivery


def test_new_pdf_intent_pins_authorization_and_revision_changes_stop_network(deps):
    webhook = record_webhook(deps)
    _, ids = run_until_finalize(deps)
    with deps.sessions() as session:
        row = session.get_one(Delivery, ids[0])
        target = destinations.listing(session)[None]
        assert (
            row.webhook_destination_id,
            row.config_version,
            row.audience,
            row.authorized_attempt,
        ) == (target.id, target.version, "internal", 1)
        assert row.authorization_origin == "explicit"
    with deps.sessions.begin() as session:
        destinations.rotate(
            session, deps.cipher, form_slug=None, actor="operadora", expected_version=1
        )
    with pytest.raises(RetryableError):
        pipeline.deliver(deps, ids[0])
    assert webhook.calls == []


def test_old_failure_callback_cannot_overwrite_success(deps):
    record_webhook(deps)
    _, ids = run_until_finalize(deps)
    pipeline.deliver(deps, ids[0])
    pipeline.mark_delivery_failed(deps, ids[0], "old_failure", dispatch_no=1)
    with deps.sessions() as session:
        assert session.get_one(Delivery, ids[0]).status == "succeeded"


def test_retry_explicitly_reauthorizes_same_audience_and_old_epoch_is_obsolete(deps):
    from app import jobs
    from app.errors import FailureCode

    webhook = record_webhook(deps)
    job_id, ids = run_until_finalize(deps)
    pipeline.mark_delivery_failed(deps, ids[0], FailureCode.DELIVERY_UNAVAILABLE.value)
    with deps.sessions.begin() as session:
        destinations.rotate(
            session, deps.cipher, form_slug=None, actor="operadora", expected_version=1
        )
        assert not jobs.resend_delivery(session, job_id, ids[0], actor="operadora")
        assert jobs.resend_delivery(
            session,
            job_id,
            ids[0],
            actor="operadora",
            expected_config_version=2,
            expected_attempt=1,
        )
    assert pipeline.deliver(deps, ids[0], dispatch_no=1) == "obsolete"
    pipeline.mark_delivery_failed(deps, ids[0], "old", dispatch_no=1)
    assert pipeline.deliver(deps, ids[0], dispatch_no=2) == "succeeded"
    assert len(webhook.calls) == 1


def test_expired_request_cannot_be_sent_before_the_purge_cron_runs(deps):
    from datetime import timedelta

    from app.errors import TerminalError
    from app.models import Job, utcnow

    webhook = record_webhook(deps)
    job_id, ids = run_until_finalize(deps)
    with deps.sessions.begin() as session:
        session.get_one(Job, job_id).updated_at = utcnow() - timedelta(
            days=deps.settings.retention_days + 1
        )
    with pytest.raises(TerminalError):
        pipeline.deliver(deps, ids[0])
    assert webhook.calls == []


def test_delivery_does_not_wait_for_a_response_body_after_receiving_status():
    import httpx

    from app.adapters.webhook import WebhookSender

    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("O contrato de transporte usa somente o status, não o corpo.")
            yield b""

    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(204, stream=UnreadBody()))
    ) as client:
        sender = WebhookSender("https://example.test/hook", "synthetic", client=client)
        assert sender.send("plano.concluido", "synthetic-id", {}) == 204


def test_finalize_replay_never_adopts_a_receiver_created_after_completion(deps):
    import sqlalchemy as sa

    from app.admin.queries.jobs import read_job
    from app.models import Artifact, Job, utcnow

    job_id, deliveries = run_until_finalize(deps)
    assert deliveries == []
    with deps.sessions() as session:
        job = session.get_one(Job, job_id)
        artifact = session.scalar(sa.select(Artifact).where(Artifact.job_id == job_id))
    record_webhook(deps)
    assert pipeline.finalize(deps, job_id, 1, artifact.id) == []
    with deps.sessions() as session:
        detail = read_job(
            session,
            job.public_id,
            forms=deps.forms,
            now=utcnow(),
            with_lead=False,
            storage=deps.storage,
        )
    assert detail.state.deliveries == ()


def test_finalize_replay_never_enqueues_an_integration_activated_after_completion(deps):
    import sqlalchemy as sa
    from conftest import FakeBaserow
    from test_integrations import connect_form

    from app.models import Artifact

    job_id, deliveries = run_until_finalize(deps)
    assert deliveries == []
    with deps.sessions() as session:
        artifact = session.scalar(sa.select(Artifact).where(Artifact.job_id == job_id))
    FakeBaserow().install(deps)
    connect_form(deps)
    assert pipeline.finalize(deps, job_id, 1, artifact.id) == []
    assert pipeline.pending_syncs(deps, artifact.id) == []


def test_finalize_replay_still_rejects_an_artifact_outside_the_authorized_attempt(deps):
    from uuid import uuid4

    import sqlalchemy as sa

    from app.errors import TerminalError
    from app.models import Artifact

    record_webhook(deps)
    job_id, deliveries = run_until_finalize(deps)
    with pytest.raises(TerminalError):
        pipeline.finalize(deps, job_id, 1, uuid4())
    with deps.sessions() as session:
        artifact = session.scalar(sa.select(Artifact).where(Artifact.job_id == job_id))
    assert pipeline.finalize(deps, job_id, 1, artifact.id) == deliveries
