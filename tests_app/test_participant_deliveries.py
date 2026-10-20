"""Habilitação, envelope e concorrência pela costura real de entrega, sem provedores."""

import json
import threading
from uuid import uuid4

import pytest
from conftest import record_webhook
from test_admin_plan_projection import generated
from test_deployment_contract import evidence
from test_plan_projections import SENTINEL

from app import destinations, pipeline
from app.deployment_contract import attest_rollout, begin_rollout
from app.errors import RetryableError
from app.models import Delivery, Plan


@pytest.fixture
def enabled(deps):
    epoch = uuid4()
    with deps.sessions.begin() as session:
        begin_rollout(session, epoch=epoch)
        attest_rollout(session, epoch=epoch, evidence=evidence())
    try:
        yield
    finally:
        with deps.sessions.begin() as session:
            begin_rollout(session, epoch=uuid4())


def configure(deps, purpose, version=0):
    with deps.sessions.begin() as session:
        return destinations.save(
            session,
            deps.cipher,
            form_slug=None,
            url="https://example.test/hook",
            actor="operadora",
            expected_version=version,
            production=False,
            http_confirmed=False,
            purpose=purpose,
        )


def finalized(deps):
    job = generated(deps)
    with deps.sessions() as session:
        import sqlalchemy as sa

        plan = session.scalar(sa.select(Plan).where(Plan.job_id == job.id))
    artifact, _ = pipeline.render_pdf(deps, job.id, 1, plan.id)
    return job, pipeline.finalize(deps, job.id, 1, artifact)[0]


def test_participant_configuration_is_refused_without_persisted_attestation(deps):
    with pytest.raises(destinations.InvalidPurpose, match="compatibilidade"):
        configure(deps, "participant")
    with deps.sessions() as session:
        assert destinations.listing(session) == {}


def test_enabled_default_is_participant_and_transport_contains_only_public_projection(
    deps, enabled
):
    created = configure(deps, None)
    assert (created.destination.purpose, created.destination.purpose_origin) == (
        "participant",
        "default",
    )
    transport = record_webhook(deps)
    _, delivery_id = finalized(deps)
    assert pipeline.deliver(deps, delivery_id) == "succeeded"
    payload = transport.calls[0][2]
    assert SENTINEL not in json.dumps(payload)
    assert set(payload) == {
        "event",
        "event_id",
        "occurred_at",
        "audience",
        "form",
        "job",
        "plan",
        "pdf",
    }
    with deps.sessions.begin() as session:
        begin_rollout(session, epoch=uuid4())
    assert pipeline.deliver(deps, delivery_id) == "already_succeeded"
    assert len(transport.calls) == 1


def test_legacy_delivery_never_falls_back_to_participant_even_when_enabled(deps, enabled):
    configure(deps, "participant")
    transport = record_webhook(deps)
    _, delivery_id = finalized(deps)
    with deps.sessions.begin() as session:
        row = session.get_one(Delivery, delivery_id)
        row.authorization_origin = "legacy"
        row.audience = "internal"
        row.webhook_destination_id = row.config_version = row.authorized_attempt = None
    with pytest.raises(RetryableError):
        pipeline.deliver(deps, delivery_id)
    assert transport.calls == []


def test_purpose_change_waits_for_authorized_transport_to_finish(deps, enabled):
    configure(deps, "internal")
    transport = record_webhook(deps)
    _, delivery_id = finalized(deps)
    entered, release, changed = threading.Event(), threading.Event(), threading.Event()
    errors = []
    original = transport.send

    def send(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    transport.send = send

    def dispatch():
        try:
            pipeline.deliver(deps, delivery_id)
        except Exception as exc:  # noqa: BLE001 - a thread devolve somente o tipo seguro
            errors.append(type(exc).__name__)

    def change():
        try:
            configure(deps, "participant", version=1)
        except Exception as exc:  # noqa: BLE001 - a thread devolve somente o tipo seguro
            errors.append(type(exc).__name__)
        finally:
            changed.set()

    sending = threading.Thread(target=dispatch)
    editing = threading.Thread(target=change)
    sending.start()
    try:
        assert entered.wait(3)
        editing.start()
        assert not changed.wait(0.15)
    finally:
        release.set()
        sending.join(5)
        if editing.ident is not None:
            editing.join(5)
    assert errors == [] and changed.is_set()
    assert SENTINEL in json.dumps(transport.calls[0][2]["plan"])
    assert pipeline.deliver(deps, delivery_id) == "already_succeeded"
