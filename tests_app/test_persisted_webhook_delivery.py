"""Transporte de respostas conserva identidade e autoridade histórica."""

from copy import deepcopy

import pytest
import sqlalchemy as sa
from conftest import record_webhook
from persisted_webhook_support import accept_v2, configure_v2
from persisted_webhook_support import v2_ready as v2_ready  # noqa: PLC0414

from app import capture_deliveries
from app.admin.actions import captures
from app.models import CaptureDelivery, Job, Submission, WebhookDestination


@pytest.mark.parametrize("generation", ["none", "pdf"])
def test_dispatch_delivers_persisted_snapshot_independent_of_pdf(v2_ready, generation):
    document, _ = configure_v2(v2_ready, generation)
    accepted = accept_v2(v2_ready, document)
    with v2_ready.sessions() as session:
        row = session.scalar(sa.select(CaptureDelivery))
        payload = deepcopy(row.payload)
    if generation == "pdf":
        with v2_ready.sessions.begin() as session:
            session.get_one(Job, accepted.job.id).status = "failed"
    transport = record_webhook(v2_ready)
    assert capture_deliveries.dispatch(v2_ready, row.id, 1) == "succeeded"
    assert capture_deliveries.dispatch(v2_ready, row.id, 1) == "already_succeeded"
    assert transport.calls == [("submissao.persistida", str(row.id), payload)]


def test_retry_cannot_reassign_snapshot_revision(v2_ready):
    document, destination_id = configure_v2(v2_ready)
    accepted = accept_v2(v2_ready, document)
    with v2_ready.sessions.begin() as session:
        row = session.scalar(sa.select(CaptureDelivery))
        row.status = "failed"
        session.get_one(WebhookDestination, destination_id).version = 2
    with pytest.raises(captures.Conflict):
        captures.retry(
            v2_ready,
            accepted.submission.id,
            row.id,
            expected_dispatch=1,
            expected_config_version=2,
            actor="operadora",
        )
    with v2_ready.sessions() as session:
        assert session.get_one(CaptureDelivery, row.id).config_version == 1


def test_database_blocks_snapshot_change_but_allows_purge(v2_ready):
    document, _ = configure_v2(v2_ready)
    accepted = accept_v2(v2_ready, document)
    with pytest.raises(sa.exc.DBAPIError), v2_ready.sessions.begin() as session:
        session.execute(sa.update(CaptureDelivery).values(config_version=2))
    with v2_ready.sessions.begin() as session:
        row_id = session.scalar(sa.select(CaptureDelivery.id))
        session.execute(sa.delete(Submission).where(Submission.id == accepted.submission.id))
    transport = record_webhook(v2_ready)
    capture_deliveries.dispatch(v2_ready, row_id, 1)
    assert transport.calls == []


def test_database_preserves_issuer_on_update_and_delete(v2_ready):
    from uuid import uuid4

    from app.models import WebhookIssuer

    with v2_ready.sessions() as session:
        original = session.get_one(WebhookIssuer, 1).namespace
    for change in (sa.update(WebhookIssuer).values(namespace=uuid4()), sa.delete(WebhookIssuer)):
        with pytest.raises(sa.exc.DBAPIError), v2_ready.sessions.begin() as session:
            session.execute(change)
    with v2_ready.sessions() as session:
        assert session.get_one(WebhookIssuer, 1).namespace == original


def test_original_dispatcher_refuses_new_selection(v2_ready, tmp_path):
    import hashlib
    import importlib.util
    from pathlib import Path

    document, _ = configure_v2(v2_ready)
    accept_v2(v2_ready, document)
    fixture = Path(__file__).parent / "fixtures/legacy/capture_deliveries_ecc009f.py"
    assert (
        hashlib.sha256(fixture.read_bytes()).hexdigest()
        == "217a5b7a2330b25b297cc93929d11e94a867a01d599b1a05007120c696bfcc56"
    )
    module_spec = importlib.util.spec_from_file_location("capture_legacy55", fixture)
    module = importlib.util.module_from_spec(module_spec)
    import sys

    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    transport = record_webhook(v2_ready)
    with v2_ready.sessions() as session:
        row = session.scalar(sa.select(CaptureDelivery))
    assert module.dispatch(v2_ready, row.id, 1) == "failed"
    assert transport.calls == []
    with v2_ready.sessions() as session:
        assert session.get_one(CaptureDelivery, row.id).last_error == "configuration_incompatible"


def test_committed_pdf_without_trigger_recovers_and_old_callback_is_ignored(v2_ready):
    from datetime import timedelta

    from app.crons import RunState, reconcile_once
    from app.models import utcnow

    document, _ = configure_v2(v2_ready, "pdf")
    accept_v2(v2_ready, document)
    with v2_ready.sessions.begin() as session:
        row = session.scalar(sa.select(CaptureDelivery))
        original = deepcopy(row.payload)
        row.updated_at = utcnow() - timedelta(minutes=20)
    launched = []
    report = reconcile_once(
        v2_ready,
        launch_job=lambda *_: "synthetic-job",
        run_state=lambda _: RunState.MISSING,
        launch_capture_fn=lambda identifier, epoch: (
            launched.append((identifier, epoch)) or "synthetic-run"
        ),
    )
    assert report.captures_launched == 1 and launched == [(row.id, 2)]
    with v2_ready.sessions.begin() as session:
        capture_deliveries.set_run_id(session, row.id, 1, "obsolete-run")
        current = session.get_one(CaptureDelivery, row.id)
        assert current.hatchet_run_id is None and current.payload == original
    assert capture_deliveries.mark_failed(v2_ready, row.id, 1) == 0


def test_expired_answers_and_revised_destination_never_send_or_retarget(v2_ready):
    from datetime import timedelta

    from app.models import utcnow

    document, destination_id = configure_v2(v2_ready, "pdf")
    accepted = accept_v2(v2_ready, document)
    with v2_ready.sessions.begin() as session:
        row = session.scalar(sa.select(CaptureDelivery))
        session.get_one(Submission, accepted.job.submission_id).created_at = utcnow() - timedelta(
            days=1000
        )
        destination = session.get_one(WebhookDestination, destination_id)
        destination.version = 2
        destination.url = "https://example.test/replaced"
    transport = record_webhook(v2_ready)
    assert capture_deliveries.dispatch(v2_ready, row.id, 1) == "failed"
    assert transport.calls == []
    with v2_ready.sessions() as session:
        assert session.get_one(CaptureDelivery, row.id).last_error == "retention_expired"


def test_v2_workflow_outputs_only_ids_and_counts(v2_ready, monkeypatch, caplog):
    import json

    from app import workflows

    document, _ = configure_v2(v2_ready, "pdf")
    accept_v2(v2_ready, document)
    with v2_ready.sessions() as session:
        row = session.scalar(sa.select(CaptureDelivery))
    transport = record_webhook(v2_ready)
    input = workflows.CaptureInput.for_delivery(row.id, 1)
    output = workflows.send_capture.mock_run(input=input)
    assert output.delivered_count == 1
    encoded = json.dumps(input.model_dump(mode="json")) + json.dumps(output.model_dump(mode="json"))
    assert "answers" not in encoded and "consent" not in encoded and "payload" not in encoded
    assert transport.calls[0][2]["schema_version"] == 2
    assert "answers" not in caplog.text and "consent" not in caplog.text
