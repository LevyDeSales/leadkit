"""Eventos declarados nas interfaces públicas, com PostgreSQL e dados sintéticos."""

from fastapi.testclient import TestClient

from app.api.handoffs import create_integration_api


def test_event_authentication_precedes_malformed_json():
    with TestClient(create_integration_api()) as client:
        response = client.post(
            "/journey/events", content=b"{", headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 401
    assert response.json() == {"code": "invalid_credentials"}
    assert response.headers["Cache-Control"] == "no-store"


def test_paused_source_specific_credential_and_capture_ingestion(deps, api_client):
    from uuid import uuid4

    import sqlalchemy as sa
    from test_handoffs import NOW, accept, configured

    from app.models import HandoffReceiver, HandoffReference

    with deps.sessions.begin() as session:
        spec, _ = configured(session)
        accepted = accept(session, spec, deps)
        receiver = session.scalar(sa.select(HandoffReceiver))
        reference = session.scalar(sa.select(HandoffReference))
        receiver_id, reference_id, submission_id = receiver.id, reference.id, accepted.submission.id
    writer = api_client("total")
    created = writer.post(
        "/admin/api/v1/forms/synthetic/event-sources",
        json={
            "name": "Agenda sintética",
            "receiver_id": str(receiver_id),
            "receiver_revision": 1,
            "namespace": "synthetic",
            "expected_version": "0",
            "idempotency_key": str(uuid4()),
            "confirm": True,
            "policy": {
                "types": ["meeting.state_reported"],
                "channels": ["system"],
                "evidence_kinds": ["calendar_record"],
            },
        },
    )
    assert created.status_code == 201, created.text
    source = created.json()
    assert source["enabled"] is False
    issued = writer.post(
        f"/admin/api/v1/event-sources/{source['id']}/credentials",
        json={
            "name": "Sintética",
            "source_revision": 1,
            "expected_source_version": "1",
            "expires_at": None,
            "idempotency_key": str(uuid4()),
            "confirm": True,
        },
    )
    assert issued.status_code == 201, issued.text
    headers = {"Authorization": "Bearer " + issued.json()["secret"]}
    event = {
        "schema_version": 1,
        "source_revision": 1,
        "event_id": "synthetic-1",
        "reference_id": str(reference_id),
        "type": "meeting.state_reported",
        "entity": {"kind": "meeting", "id": "meeting-1", "revision": 1},
        "occurred_at": NOW.isoformat(),
        "evidence": {
            "channel": "system",
            "kind": "calendar_record",
            "id": "record-1",
            "author": {"kind": "service", "id": "adapter-1"},
        },
        "data": {
            "status": "scheduled",
            "starts_at": "2026-10-25T10:00:00-03:00",
            "ends_at": None,
            "timezone": "America/Sao_Paulo",
            "change": "created",
        },
    }
    with TestClient(create_integration_api(clock=lambda: NOW.timestamp())) as client:
        assert client.post("/journey/events", json=event, headers=headers).status_code == 401
        enabled = writer.post(
            f"/admin/api/v1/event-sources/{source['id']}/enabled",
            json={"enabled": True, "expected_version": "1", "confirm": True},
        )
        assert enabled.status_code == 200, enabled.text
        first = client.post("/journey/events", json=event, headers=headers)
        assert first.status_code == 201, first.text
        replay = client.post("/journey/events", json=event, headers=headers)
        assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    journey = writer.get(f"/admin/api/v1/submissions/{submission_id}/journey")
    assert journey.status_code == 200, journey.text
    assert journey.json()["job"] is None
    assert "meeting-1" not in journey.text and "adapter-1" not in journey.text
    assert journey.json()["external_declared"][0]["starts_at"] == "2026-10-25T13:00:00Z"


from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from handoff_support import isolated_handoff_contract  # noqa: F401
from test_handoffs import NOW, accept, configured

from app.models import HandoffReceiver, HandoffReference


class SyntheticWorld(dict):
    def __repr__(self):
        return "<interfaces e credenciais sintéticas omitidas>"


@pytest.fixture
def world(deps, api_client):
    with deps.sessions.begin() as session:
        spec, resolver = configured(session)
        accepted = accept(session, spec, deps)
        reference = session.scalar(sa.select(HandoffReference))
        receiver = session.scalar(sa.select(HandoffReceiver))
        ids = (reference.id, accepted.submission.id, receiver.id)
    writer, reader = api_client("total"), api_client("leitura")
    body = {
        "name": "Fonte sintética",
        "receiver_id": str(ids[2]),
        "receiver_revision": 1,
        "namespace": "synthetic",
        "expected_version": "0",
        "idempotency_key": str(uuid4()),
        "confirm": True,
        "policy": {
            "types": [
                "correlation.linked",
                "invitation.sent",
                "meeting.state_reported",
                "meeting.attendance_recorded",
                "notification.delivery_reported",
                "participant.receipt_acknowledged",
                "entity.withdrawn",
            ],
            "channels": ["system", "chat"],
            "evidence_kinds": [
                "correlation_record",
                "dispatch_record",
                "calendar_record",
                "attendance_record",
                "provider_delivery_receipt",
                "participant_ack",
                "operator_correction",
            ],
        },
    }
    created = writer.post("/admin/api/v1/forms/synthetic/event-sources", json=body)
    assert created.status_code == 201, created.text
    source = created.json()
    credential_body = {
        "name": "Adaptadora",
        "source_revision": 1,
        "expected_source_version": source["version"],
        "expires_at": None,
        "idempotency_key": str(uuid4()),
        "confirm": True,
    }
    issued = writer.post(
        f"/admin/api/v1/event-sources/{source['id']}/credentials", json=credential_body
    )
    assert issued.status_code == 201, issued.text
    issued = issued.json()
    enabled = writer.post(
        f"/admin/api/v1/event-sources/{source['id']}/enabled",
        json={"expected_version": source["version"], "enabled": True, "confirm": True},
    )
    assert enabled.status_code == 200, enabled.text
    with TestClient(create_integration_api(clock=lambda: NOW.timestamp())) as service:
        yield SyntheticWorld(
            {
                "writer": writer,
                "reader": reader,
                "service": service,
                "headers": {"Authorization": "Bearer " + issued["secret"]},
                "source": enabled.json(),
                "issued": issued,
                "create_body": body,
                "credential_body": credential_body,
                "reference": str(ids[0]),
                "submission": str(ids[1]),
                "resolver": resolver,
            }
        )


def event(world, revision=1, *, kind="meeting", identifier="meeting-1"):
    types = {
        "meeting": "meeting.state_reported",
        "attendance": "meeting.attendance_recorded",
        "context": "correlation.linked",
        "invitation": "invitation.sent",
        "notification": "notification.delivery_reported",
        "receipt": "participant.receipt_acknowledged",
    }
    evidence = {
        "meeting": "calendar_record",
        "attendance": "attendance_record",
        "context": "correlation_record",
        "invitation": "dispatch_record",
        "notification": "provider_delivery_receipt",
        "receipt": "participant_ack",
    }
    data = {
        "meeting": {
            "status": "scheduled",
            "starts_at": "2026-10-25T10:00:00-03:00",
            "ends_at": None,
            "timezone": "America/Sao_Paulo",
            "change": "created",
        },
        "attendance": {
            "meeting_id": "meeting-1",
            "meeting_revision": 1,
            "status": "attended",
            "change": "attendance",
        },
        "context": {
            "contact_id": "contact-personal-marker",
            "conversation_id": None,
            "assignee_id": None,
        },
        "invitation": {"conversation_id": None, "notification_id": "notification-1"},
        "notification": {"status": "confirmed", "conversation_id": None},
        "receipt": {"notification_id": "notification-1", "conversation_id": None},
    }
    return {
        "schema_version": 1,
        "source_revision": 1,
        "event_id": f"{kind}-{revision}-{identifier}",
        "reference_id": world["reference"],
        "type": types[kind],
        "entity": {"kind": kind, "id": identifier, "revision": revision},
        "occurred_at": NOW.isoformat(),
        "evidence": {
            "channel": "system",
            "kind": evidence[kind],
            "id": "evidence-personal-marker",
            "author": {
                "kind": "participant" if kind == "receipt" else "service",
                "id": "author-personal-marker",
            },
        },
        "data": data[kind],
    }


def post(world, body):
    return world["service"].post("/journey/events", json=body, headers=world["headers"])


def read(world, **params):
    response = world["writer"].get(
        f"/admin/api/v1/submissions/{world['submission']}/journey", params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_out_of_order_calendar_and_attendance_are_independent(world):
    attendance = event(world, kind="attendance", identifier="attendance-1")
    assert post(world, attendance).status_code == 201
    assert read(world)["external_declared"][0]["meeting_event_id"] is None
    one = post(world, event(world))
    three = event(world, 3)
    three["data"]["starts_at"] = "2026-10-26T11:00:00-03:00"
    assert post(world, three).status_code == 201
    two = post(world, event(world, 2))
    assert two.status_code == 201 and two.json()["projection_effect"] == "historical"
    projected = {row["entity_kind"]: row for row in read(world)["external_declared"]}
    assert projected["meeting"]["entity_revision"] == 3
    assert projected["attendance"]["meeting_event_id"] == one.json()["id"]
    withdrawal = event(world, 4)
    withdrawal.update(type="entity.withdrawn", data={"reason_code": "incorrect"})
    withdrawal["evidence"]["kind"] = "operator_correction"
    assert post(world, withdrawal).json()["projection_effect"] == "withdrawn"
    assert {row["entity_kind"]: row for row in read(world)["external_declared"]}["meeting"][
        "state"
    ] == "withdrawn"
    assert {row["entity_kind"]: row for row in read(world)["external_declared"]}["attendance"][
        "status"
    ] == "attended"


def test_replays_conflicts_and_reference_collisions_are_distinct(world, deps):
    first = post(world, event(world))
    conflict = event(world)
    conflict["data"]["timezone"] = "UTC"
    assert post(world, conflict).json() == {"code": "event_conflict"}
    conflict = event(world)
    conflict["event_id"] = "different-event"
    assert post(world, conflict).json() == {"code": "entity_revision_conflict"}
    with deps.sessions.begin() as session:
        from app.forms.definition import compile_definition
        from app.models import FormVersion

        spec = compile_definition(
            session.scalar(
                sa.select(FormVersion).where(
                    FormVersion.form_id
                    == session.scalar(
                        sa.select(__import__("app.models", fromlist=["Form"]).Form.id).where(
                            __import__("app.models", fromlist=["Form"]).Form.slug == "synthetic"
                        )
                    )
                )
            ).definition
        )
        accept(session, spec, deps, key="second")
        reference = session.scalar(
            sa.select(HandoffReference).where(HandoffReference.id != UUID(world["reference"]))
        )
        reference_id = str(reference.id)
    crossed = event(world)
    crossed["reference_id"] = reference_id
    assert post(world, crossed).json() == {"code": "event_conflict"}
    assert post(world, event(world)).json()["id"] == first.json()["id"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(occurred_at="2026-10-20T12:00:00"),
        lambda body: body.update(occurred_at="2026-10-20T12:06:00Z"),
        lambda body: body["data"].update(timezone="Invalid/Zone"),
        lambda body: body["data"].update(ends_at="2026-10-25T09:00:00-03:00"),
        lambda body: body["data"].update(message="PII-DO-NOT-ECHO"),
        lambda body: body["evidence"].update(url="https://example.test/PII-DO-NOT-ECHO"),
    ],
)
def test_closed_schema_and_times_refuse_without_echo(world, mutate):
    body = event(world)
    mutate(body)
    response = post(world, body)
    assert response.status_code == 422, response.text
    assert "PII-DO-NOT-ECHO" not in response.text and "input" not in response.text
    assert read(world)["external_declared"] == []


def test_external_ids_are_opt_in_and_erasure_revokes_atomically(world):
    assert post(world, event(world, kind="context", identifier="context")).status_code == 201
    journey = read(world)
    assert "personal-marker" not in str(journey)
    allowed = read(world, include_lead_data=True)
    assert "contact-personal-marker" in str(allowed)
    path = f"/admin/api/v1/submissions/{world['submission']}/journey"
    assert world["reader"].get(path, params={"include_lead_data": True}).status_code == 403
    command = {
        "expected_journey_version": journey["journey_version"],
        "expected_handoff_version": journey["reference_version"],
        "confirm": True,
    }
    assert (
        world["writer"]
        .post(path + "/erase", json={**command, "expected_journey_version": "stale"})
        .status_code
        == 409
    )
    erased = world["writer"].post(path + "/erase", json=command)
    assert erased.status_code == 200, erased.text
    assert "personal-marker" not in str(read(world, include_lead_data=True))
    assert read(world)["external_declared"][0]["state"] == "erased"
    assert world["writer"].post(path + "/erase", json=command).status_code == 200
    assert post(world, event(world, 2, kind="context", identifier="context")).status_code == 404


def test_expired_bearer_cleared_ciphertext_keeps_historical_authority(world, deps):
    with deps.sessions.begin() as session:
        reference = session.get(HandoffReference, UUID(world["reference"]))
        reference.expires_at = NOW - timedelta(seconds=1)
        reference.created_at = NOW - timedelta(days=1)
        reference.token_ciphertext = None
        receiver = session.get(HandoffReceiver, reference.receiver_id)
        receiver.revision += 1
        receiver.enabled = False
    assert post(world, event(world)).status_code == 201


def test_purge_does_not_resurrect_on_replay(world, deps):
    assert post(world, event(world)).status_code == 201
    with deps.sessions.begin() as session:
        from app.models import Submission

        session.get(Submission, UUID(world["submission"])).created_at = NOW - timedelta(days=400)
    from app.retention import purge_expired

    purge_expired(deps, now=NOW)
    assert post(world, event(world)).status_code == 404


def test_same_event_concurrently_commits_one_receipt(world):
    from concurrent.futures import ThreadPoolExecutor

    def send():
        with TestClient(create_integration_api(clock=lambda: NOW.timestamp())) as client:
            return client.post("/journey/events", json=event(world), headers=world["headers"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: send(), range(2)))
    assert sorted(response.status_code for response in results) == [200, 201]
    assert results[0].json()["id"] == results[1].json()["id"]


def test_mcp_projection_confirmations_and_sensitive_opt_in(world, tmp_path):
    from admin_mcp_live import Live
    from admin_mcp_world import Asker

    assert post(world, event(world, kind="context", identifier="context")).status_code == 201
    live = Live(world["writer"], tmp_path)
    try:
        assert "get_submission_journey" in live.tools
        assert (
            "include_lead_data"
            not in live.tools["get_submission_journey"].input_schema["properties"]
        )
        result = live.ok("get_submission_journey", {"submission_id": world["submission"]})
        assert "personal-marker" not in str(result)
        assert live.tools["erase_submission_journey"].annotations["destructiveHint"] is True
        declined = live.call(
            "set_event_source_enabled",
            {
                "source_id": world["source"]["id"],
                "expected_version": world["source"]["version"],
                "enabled": False,
            },
            Asker(False),
        )
        assert declined.is_error
    finally:
        live.close()
