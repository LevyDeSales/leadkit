"""A intenção conserva o aceite inteiro e não depende da geração."""

from datetime import datetime

import pytest
import sqlalchemy as sa
from persisted_webhook_support import accept_v2, configure_v2
from persisted_webhook_support import v2_ready as v2_ready  # noqa: PLC0414

from app.models import CaptureDelivery, CaptureReceipt, Job, Submission, WebhookIssuer


@pytest.mark.parametrize("generation", ["none", "pdf"])
def test_capture_and_pdf_commit_one_historical_snapshot_and_replay(v2_ready, generation):
    document, destination_id = configure_v2(v2_ready, generation)
    accepted = accept_v2(v2_ready, document)
    submission_id = accepted.submission.id if generation == "none" else accepted.job.submission_id
    assert accept_v2(v2_ready, document).replay
    with v2_ready.sessions() as session:
        deliveries = session.scalars(sa.select(CaptureDelivery)).all()
        assert len(deliveries) == 1
        row = deliveries[0]
        submission = session.get_one(Submission, submission_id)
        assert row.schema_version == 2
        assert row.payload["schema_version"] == 2
        assert row.payload["event_id"] == str(row.id)
        assert row.payload["issuer"] == "leadkit:" + str(
            session.get_one(WebhookIssuer, 1).namespace
        )
        assert row.payload["submission"]["id"] == str(submission_id)
        assert (
            datetime.fromisoformat(row.payload["submission"]["submitted_at"])
            == submission.created_at
        )
        assert row.payload["answers"] == submission.answers
        assert row.payload["consent"] == submission.consent
        assert row.payload["generation"] == generation
        assert row.payload["delivery_authorization"] == {
            "destination_id": str(destination_id),
            "revision": 1,
            "purpose": "internal",
        }
        assert session.scalar(sa.select(sa.func.count()).select_from(Job)) == (generation == "pdf")
        assert session.scalar(sa.select(sa.func.count()).select_from(CaptureReceipt)) == (
            generation == "none"
        )
        assert not {"pdf", "plan", "token", "lead", "tracking"}.intersection(row.payload)


@pytest.mark.parametrize("generation", ["none", "pdf"])
def test_failure_after_receipt_or_job_rolls_back_intention(v2_ready, monkeypatch, generation):
    from app import handoffs

    document, _ = configure_v2(v2_ready, generation)

    def fail(*args, **kwargs):
        raise RuntimeError("falha sintética após vínculo")

    monkeypatch.setattr(handoffs, "prepare_handoff", fail)
    with pytest.raises(RuntimeError):
        accept_v2(v2_ready, document)
    with v2_ready.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Submission)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(CaptureDelivery)) == 0


def test_concurrent_accept_keeps_one_snapshot_and_new_diagnosis_is_distinct(v2_ready):
    from concurrent.futures import ThreadPoolExecutor

    document, _ = configure_v2(v2_ready, "pdf")
    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted = list(executor.map(lambda _: accept_v2(v2_ready, document), range(2)))
    assert sorted(row.replay for row in accepted) == [False, True]
    assert accepted[0].job.submission_id == accepted[1].job.submission_id
    accept_v2(v2_ready, document, key="diagnóstico-novo")
    with v2_ready.sessions() as session:
        rows = session.scalars(sa.select(CaptureDelivery)).all()
        assert len(rows) == 2
        assert len({row.payload["submission"]["id"] for row in rows}) == 2
        assert len({row.payload["event_id"] for row in rows}) == 2


def test_enable_does_not_backfill_replay(v2_ready):
    import json

    from test_capture import capture_document

    from app import destinations
    from app.forms.stored import import_definition

    document = capture_document()
    import_definition(v2_ready, json.dumps(document), actor="operadora")
    original = accept_v2(v2_ready, document)
    with v2_ready.sessions.begin() as session:
        destinations.save(
            session,
            v2_ready.cipher,
            form_slug=document["slug"],
            url="https://example.test/persisted",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="internal",
            events=["submissao.persistida.v2"],
        )
    replay = accept_v2(v2_ready, document)
    assert replay.replay and replay.submission.id == original.submission.id
    with v2_ready.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(CaptureDelivery)) == 0


def test_pdf_http_launches_response_intention_only_after_commit(v2_ready, monkeypatch):
    from concurrent.futures import Future
    from threading import Event

    from fastapi.testclient import TestClient

    from app.admin import launching
    from app.api.main import create_app

    document, _ = configure_v2(v2_ready, "pdf")
    observed = []
    reached = Event()

    def launch(submission_id):
        with v2_ready.sessions() as session:
            observed.extend(
                session.scalars(
                    sa.select(CaptureDelivery.id).where(
                        CaptureDelivery.submission_id == submission_id
                    )
                ).all()
            )
        reached.set()
        done = Future()
        done.set_result(None)
        return done

    monkeypatch.setattr(launching, "launch_capture_later", launch)

    async def generate(*args):
        return "synthetic-run"

    spec = v2_ready.forms.current(document["slug"])
    with TestClient(create_app(trigger=generate)) as client:
        response = client.post(
            f"/v1/forms/{document['slug']}/submissions",
            json={
                "answers": document["examples"]["answers"],
                "consent": {"privacy": True, "text_version": spec.consent_text_version},
            },
            headers={"Idempotency-Key": "post-commit-pdf"},
        )
        assert response.status_code == 202
        assert reached.wait(2) and len(observed) == 1
