"""Eventos declarados nas interfaces públicas, com PostgreSQL e dados sintéticos."""

import copy
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from admin_api_support import url_of
from fastapi.testclient import TestClient
from handoff_support import isolated_handoff_contract  # noqa: F401
from test_handoffs import NOW, accept, configured
from test_plan_release import manual_pdf as manual_pdf  # noqa: PLC0414

from app.api.handoffs import create_integration_api
from app.models import HandoffReceiver, HandoffReference


def test_event_authentication_precedes_malformed_json():
    with TestClient(create_integration_api()) as client:
        response = client.post(
            "/journey/events", content=b"{", headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 401
    assert response.json() == {"code": "invalid_credentials"}
    assert response.headers["Cache-Control"] == "no-store"


def test_paused_source_specific_credential_and_capture_ingestion(deps, api_client):
    with deps.sessions.begin() as session:
        spec, _ = configured(session)
        accepted = accept(session, spec, deps)
        receiver = session.scalar(sa.select(HandoffReceiver))
        reference = session.scalar(sa.select(HandoffReference))
        receiver_id, reference_id, submission_id = receiver.id, reference.id, accepted.submission.id
    writer = api_client("total")
    created = writer.post(
        url_of("create_event_source", slug="synthetic"),
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
        url_of("create_event_credential", source_id=source["id"]),
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


class SyntheticWorld(dict):
    def __repr__(self):
        return "<interfaces e credenciais sintéticas omitidas>"


@pytest.fixture
def world(deps, api_client, request):
    with deps.sessions.begin() as session:
        spec, resolver = configured(session, **getattr(request, "param", {}))
        accept(session, spec, deps)
        reference = session.scalar(sa.select(HandoffReference))
        receiver = session.scalar(sa.select(HandoffReceiver))
        ids = (reference.id, reference.submission_id, receiver.id)
    from test_admin import FakeClock

    clock = FakeClock()
    clock.now = NOW.timestamp()
    writer, reader = api_client("total", clock=clock), api_client("leitura", clock=clock)
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
                "spec": spec,
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
    assert projected["attendance"]["meeting_revision"] == 1
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
        accept(session, world["spec"], deps, key="second")
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


def test_html_pages_native_controls_csrf_safe_retry_and_journey(world):
    from admin_api_support import ORIGIN
    from test_admin import csrf_token, login

    client = world["writer"]
    assert (
        client.get(url_of("list_event_sources", slug="synthetic")).json()["items"][0]["name"]
        == "Fonte sintética"
    )
    assert (
        client.get(url_of("get_event_source", source_id=world["source"]["id"])).json()["enabled"]
        is True
    )
    assert login(client).status_code == 303
    csrf = csrf_token(client)
    source_path = "/admin/event-sources/" + world["source"]["id"]
    page = client.get(source_path)
    assert page.status_code == 200, page.text
    assert "<fieldset>" in page.text and 'type="checkbox"' in page.text
    assert "Fonte sintética" in page.text and world["issued"]["secret"] not in page.text
    refused = client.post(
        source_path + "/credential",
        data={
            "csrf_token": csrf,
            "expected_version": "2",
            "name": "Credencial preservada",
            "source_revision": "1",
            "expiry_choice": "date",
            "expires_at": "2026-10-20T00:00",
            "idempotency_key": str(uuid4()),
            "confirm": "true",
        },
        headers=ORIGIN,
    )
    assert refused.status_code == 422, refused.text
    assert 'value="Credencial preservada"' in refused.text
    assert 'value="2026-10-20T00:00"' in refused.text
    assert world["issued"]["secret"] not in refused.text
    assert post(world, event(world)).status_code == 201
    path = "/admin/submissions/" + world["submission"] + "/journey"
    journey = client.get(path)
    assert journey.status_code == 200, journey.text
    assert "25/10/2026 às 10:00" in journey.text
    assert "America/Sao_Paulo" in journey.text
    assert "author-personal-marker" not in journey.text
    assert "Captura sem geração de PDF" in journey.text
    assert 'method="get"' in journey.text
    form = client.get("/admin/forms/synthetic")
    assert "/event-sources" in form.text
    capture = client.get("/admin/captures/" + world["submission"])
    assert path in capture.text
    assert client.post(path + "/erase", data={"confirm": "true"}, headers=ORIGIN).status_code == 403


def test_credential_expiry_rotation_replay_and_policy_revision(world):
    writer = world["writer"]
    path = url_of("get_event_source", source_id=world["source"]["id"])
    listing = writer.get(url_of("list_event_credentials", source_id=world["source"]["id"]))
    assert listing.status_code == 200 and listing.json()["items"][0]["name"] == "Adaptadora"
    assert "secret_hash" not in listing.text
    repeated = writer.post(path + "/credentials", json=world["credential_body"])
    assert (
        repeated.status_code == 409
        and repeated.json()["code"] == "credential_secret_already_issued"
    )
    assert "secret" not in repeated.json() and world["issued"]["secret"] not in repeated.text
    rotate = {
        "expected_version": "1",
        "expires_at": None,
        "idempotency_key": str(uuid4()),
        "confirm": True,
    }
    rotated = writer.post(
        "/admin/api/v1/event-credentials/" + world["issued"]["credential"]["id"] + "/rotate",
        json=rotate,
    )
    assert rotated.status_code == 200, rotated.text
    assert post(world, event(world)).status_code == 401
    world["headers"] = {"Authorization": "Bearer " + rotated.json()["secret"]}
    assert post(world, event(world)).status_code == 201
    assert (
        writer.post(
            "/admin/api/v1/event-credentials/" + world["issued"]["credential"]["id"] + "/rotate",
            json=rotate,
        ).status_code
        == 409
    )
    source = world["source"]
    policy = copy.deepcopy(source["policy"])
    policy["types"].remove("meeting.attendance_recorded")
    changed = writer.put(
        url_of("update_event_source", source_id=source["id"]),
        json={
            "expected_version": source["version"],
            "name": source["name"],
            "policy": policy,
            "confirm": True,
        },
    )
    assert (
        changed.status_code == 200
        and changed.json()["revision"] == 2
        and changed.json()["enabled"] is False
    )
    assert post(world, event(world, 2)).status_code == 401
    # Replay de criação não reativa nem reverte a política posteriormente alterada.
    replay = writer.post("/admin/api/v1/forms/synthetic/event-sources", json=world["create_body"])
    assert replay.json()["revision"] == 2 and replay.json()["enabled"] is False


def test_source_permissions_and_namespace_credentials_never_substitute(world):
    service = world["service"]
    for raw in (world["resolver"].secret.get_secret_value(), "gp_fake", "lkh_fake", "lke_invalid"):
        response = service.post(
            "/journey/events",
            content=b"{",
            headers={"Authorization": "Bearer " + raw, "Content-Type": "application/json"},
        )
        assert response.status_code == 401
    body = event(world)
    body["evidence"]["kind"] = "dispatch_record"
    assert post(world, body).status_code == 403
    assert post(world, {**event(world), "source_revision": 2}).status_code == 409
    assert post(world, {**event(world), "reference_id": str(uuid4())}).status_code == 404
    response = service.post(
        "/handoffs/resolve", json={"reference": "lkh_" + "x" * 43}, headers=world["headers"]
    )
    assert response.status_code == 401
    assert (
        service.post(
            "/journey/events",
            content=b"x",
            headers={**world["headers"], "Content-Type": "text/plain"},
        ).status_code
        == 415
    )
    assert (
        service.post(
            "/journey/events",
            content=b" " * 32769,
            headers={**world["headers"], "Content-Type": "application/json"},
        ).status_code
        == 413
    )


def test_history_paginates_and_multiple_sources_do_not_merge(world):
    for revision in (1, 3, 2):
        assert post(world, event(world, revision)).status_code == 201
    path = f"/admin/api/v1/submissions/{world['submission']}/journey/events"
    first = world["writer"].get(path, params={"limit": 2})
    assert first.status_code == 200
    page = first.json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    following = world["writer"].get(path, params={"limit": 2, "cursor": page["next_cursor"]}).json()
    assert len(following["items"]) == 1 and following["next_cursor"] is None
    assert {row["id"] for row in page["items"]}.isdisjoint(row["id"] for row in following["items"])
    assert world["writer"].get(path, params={"type": "invalid"}).status_code == 422
    assert world["writer"].get(path, params={"cursor": "invalid"}).status_code == 422
    body = copy.deepcopy(world["create_body"])
    body.update(namespace="second", idempotency_key=str(uuid4()))
    source = world["writer"].post("/admin/api/v1/forms/synthetic/event-sources", json=body).json()
    credential_body = {**world["credential_body"], "idempotency_key": str(uuid4())}
    issued = (
        world["writer"]
        .post(f"/admin/api/v1/event-sources/{source['id']}/credentials", json=credential_body)
        .json()
    )
    assert (
        world["writer"]
        .post(
            f"/admin/api/v1/event-sources/{source['id']}/enabled",
            json={"expected_version": "1", "enabled": True, "confirm": True},
        )
        .status_code
        == 200
    )
    response = world["service"].post(
        "/journey/events",
        json=event(world),
        headers={"Authorization": "Bearer " + issued["secret"]},
    )
    assert response.status_code == 201
    assert len(read(world)["external_declared"]) == 2


def test_blocked_gate_refuses_ingestion_and_enable_but_allows_pause_revoke(world, deps):
    from app.models import DeploymentContract

    with deps.sessions.begin() as session:
        session.get(DeploymentContract, 1).phase = "blocked"
    assert post(world, event(world)).status_code == 503
    path = "/admin/api/v1/event-sources/" + world["source"]["id"] + "/enabled"
    assert (
        world["writer"]
        .post(path, json={"expected_version": "2", "enabled": True, "confirm": True})
        .status_code
        == 503
    )
    assert (
        world["writer"]
        .post(path, json={"expected_version": "2", "enabled": False, "confirm": True})
        .status_code
        == 200
    )
    revoke = "/admin/api/v1/event-credentials/" + world["issued"]["credential"]["id"] + "/revoke"
    assert (
        world["writer"].post(revoke, json={"expected_version": "1", "confirm": True}).status_code
        == 200
    )


def test_synthetic_complete_flow_declares_facts_without_generating_job(world, deps):
    from urllib.parse import parse_qs, urlsplit

    from app.forms import starter
    from app.forms.definition import compile_definition

    definition = starter.definition(slug="synthetic", title="Sintético", generation="none")
    intake = world["writer"].post(
        "/v1/forms/synthetic/submissions",
        json={
            "answers": definition["examples"]["answers"],
            "consent": {
                "privacy": True,
                "text_version": compile_definition(definition).consent_text_version,
            },
        },
        headers={"Idempotency-Key": "full-flow-http"},
    )
    assert intake.status_code == 202, intake.text
    bearer = parse_qs(urlsplit(intake.json()["completion"]["url"]).query)["leadkit_ref"][0]
    resolved = world["service"].post(
        "/handoffs/resolve",
        json={"reference": bearer},
        headers={"Authorization": "Bearer " + world["resolver"].secret.get_secret_value()},
    )
    assert resolved.status_code == 200, resolved.text
    world["reference"], world["submission"] = (
        resolved.json()["reference_id"],
        resolved.json()["submission_id"],
    )
    for kind, identifier in [
        ("context", "context"),
        ("invitation", "invitation-1"),
        ("meeting", "meeting-1"),
    ]:
        assert post(world, event(world, kind=kind, identifier=identifier)).status_code == 201
    assert {row["entity_kind"] for row in read(world)["external_declared"]} == {
        "context",
        "invitation",
        "meeting",
    }
    for revision in (3, 2):
        assert post(world, event(world, revision)).status_code == 201
    for kind in ("attendance", "notification", "receipt"):
        assert post(world, event(world, kind=kind, identifier=kind + "-1")).status_code == 201
    assert read(world)["job"] is None
    assert len(read(world)["external_declared"]) == 6
    with deps.sessions.begin() as session:
        reference = session.get(HandoffReference, UUID(world["reference"]))
        reference.created_at, reference.expires_at = (
            NOW - timedelta(days=2),
            NOW - timedelta(days=1),
        )
        reference.token_ciphertext = None
    assert post(world, event(world, 4)).status_code == 201


@pytest.mark.parametrize("world", [{"generation": "pdf", "release_mode": "manual"}], indirect=True)
def test_commercial_events_do_not_release_pdf_or_extend_job_retention(world, deps):
    from app.models import Job, Submission

    with deps.sessions() as session:
        job = session.scalar(sa.select(Job).where(Job.submission_id == UUID(world["submission"])))
        original = (
            job.updated_at,
            job.expires_at,
            session.get(Submission, job.submission_id).created_at,
        )
    for kind in ("invitation", "meeting", "attendance", "receipt"):
        assert post(world, event(world, kind=kind, identifier=kind + "-1")).status_code == 201
    view = read(world)
    assert (
        next(fact for fact in view["local_facts"] if fact["type"] == "current_plan_authorization")[
            "state"
        ]
        == "held"
    )
    with deps.sessions() as session:
        job = session.scalar(sa.select(Job).where(Job.submission_id == UUID(world["submission"])))
        assert (
            job.updated_at,
            job.expires_at,
            session.get(Submission, job.submission_id).created_at,
        ) == original


def test_query_without_reference_returns_local_facts_without_creating_link(world, deps):
    with deps.sessions.begin() as session:
        session.execute(
            sa.delete(HandoffReference).where(HandoffReference.id == UUID(world["reference"]))
        )
    view = read(world)
    assert view["reference_id"] is None and view["external_declared"] == []
    assert view["local_facts"][0]["state"] == "persisted"
    assert post(world, event(world)).status_code == 404


def test_same_entity_revision_concurrently_conflicts_without_overwrite(world):
    from concurrent.futures import ThreadPoolExecutor

    first, second = event(world), event(world)
    second["event_id"] = "another-id"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda body: post(world, body), [first, second]))
    assert sorted(response.status_code for response in results) == [201, 409]
    assert next(response for response in results if response.status_code == 409).json() == {
        "code": "entity_revision_conflict"
    }


def test_ingestion_racing_erasure_never_rebuilds_external_data(world):
    from concurrent.futures import ThreadPoolExecutor

    assert post(world, event(world)).status_code == 201
    view = read(world)
    command = {
        "expected_journey_version": view["journey_version"],
        "expected_handoff_version": view["reference_version"],
        "confirm": True,
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        ingest = pool.submit(post, world, event(world, 2))
        erase = pool.submit(
            world["writer"].post,
            f"/admin/api/v1/submissions/{world['submission']}/journey/erase",
            json=command,
        )
        ingested, erased = ingest.result(), erase.result()
    assert ingested.status_code in (201, 404)
    assert erased.status_code in (200, 409)
    if erased.status_code == 409:
        view = read(world)
        command.update(
            expected_journey_version=view["journey_version"],
            expected_handoff_version=view["reference_version"],
        )
        assert (
            world["writer"]
            .post(f"/admin/api/v1/submissions/{world['submission']}/journey/erase", json=command)
            .status_code
            == 200
        )
    assert all(
        item["state"] == "erased"
        for item in read(world, include_lead_data=True)["external_declared"]
    )
    assert post(world, event(world, 3)).status_code == 404


def test_authenticated_event_rate_limit_has_retry_after(world):
    for _ in range(60):
        response = post(world, event(world))
        assert response.status_code in (200, 201)
    response = post(world, event(world))
    assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0


@pytest.mark.parametrize(
    "world", [{}, {"generation": "pdf", "release_mode": "manual"}], indirect=True
)
def test_prior_runtime_two_retains_and_purges_additive_children(world, deps, tmp_path):
    """Executa código2 anterior isolado: ignora eventos, recusa migração e purga por cascata."""
    import json
    import os
    import subprocess
    import sys
    import tarfile

    from app.models import Job, Submission

    assert post(world, event(world)).status_code == 201
    with deps.sessions.begin() as session:
        submission = session.get(Submission, UUID(world["submission"]))
        submission.created_at = NOW - timedelta(days=400)
        job = session.scalar(sa.select(Job).where(Job.submission_id == submission.id))
        if job:
            job.updated_at, job.expires_at = NOW - timedelta(days=400), NOW - timedelta(days=1)
    revision = "37f0755b8f97d5f9ccc0e96a0e8a36185fc9e42e"
    archive, runtime = tmp_path / "runtime2.tar", tmp_path / "runtime2"
    archived = subprocess.run(
        ["git", "archive", "--format=tar", "-o", str(archive), revision, "app"],
        capture_output=True,
        check=False,
    )
    assert archived.returncode == 0, (
        "A referência pública do runtime2 deve estar disponível no checkout completo."
    )
    runtime.mkdir()
    with tarfile.open(archive) as files:
        files.extractall(runtime, filter="data")
    settings = tmp_path / "synthetic-settings.json"
    settings.write_text(deps.settings.model_dump_json())
    settings.chmod(0o600)
    script = """
import json, sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from app.db import make_engine, make_session_factory
from app.settings import Settings
from app.deployment_contract import assert_contract, BINARY_CONTRACT
from app.migrations.compatibility import validate_database
from app.migrations.contracts import SIGNATURES
from app.retention import purge_expired
assert BINARY_CONTRACT == 2
assert '0018_journey_events' not in SIGNATURES
settings = Settings.model_validate_json(Path(sys.argv[2]).read_text())
engine = make_engine(settings.database_url)
sessions = make_session_factory(engine)
with sessions() as session:
    assert_contract(session, required=2)
    try:
        validate_database(session.connection(), SIGNATURES)
    except RuntimeError:
        pass
    else:
        raise AssertionError('O migrador anterior deve recusar o head novo.')
report = purge_expired(SimpleNamespace(settings=settings, sessions=sessions), now=datetime.fromisoformat(sys.argv[3]))
assert report.captures_deleted + report.requests_deleted == 1
engine.dispose()
print(json.dumps({'runtime': 2, 'purged': 1, 'old_migrator_refused': True}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(runtime), str(settings), NOW.isoformat()],
        cwd=runtime,
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "A execução isolada do runtime2 falhou: " + result.stderr[-1200:]
    assert json.loads(result.stdout)["purged"] == 1
    assert post(world, event(world)).status_code == 404
    with deps.sessions() as session:
        from app.models import JourneyEvent

        assert session.scalar(sa.select(sa.func.count()).select_from(JourneyEvent)) == 0


@pytest.mark.parametrize("value", [True, 1.5, "1", 2147483648])
def test_event_entity_revision_requires_a_bounded_json_integer(world, value):
    body = event(world)
    body["entity"]["revision"] = value
    assert post(world, body).status_code == 422


@pytest.mark.parametrize("field,value", [("occurred_at", 1792497600), ("schema_version", True)])
def test_event_contract_does_not_coerce_numeric_time_or_boolean_version(world, field, value):
    body = event(world)
    body[field] = value
    assert post(world, body).status_code == 422


@pytest.mark.parametrize(
    "world", [{}, {"generation": "pdf", "release_mode": "manual"}], indirect=True
)
def test_ingestion_racing_retention_leaves_no_recreated_diagnosis(world, deps):
    from concurrent.futures import ThreadPoolExecutor

    from app.models import Job, Submission
    from app.retention import purge_expired

    assert post(world, event(world)).status_code == 201
    with deps.sessions.begin() as session:
        submission = session.get(Submission, UUID(world["submission"]))
        submission.created_at = NOW - timedelta(days=400)
        job = session.scalar(sa.select(Job).where(Job.submission_id == submission.id))
        if job:
            job.updated_at, job.expires_at = NOW - timedelta(days=400), NOW - timedelta(days=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ingest = pool.submit(post, world, event(world, 2))
        purge = pool.submit(purge_expired, deps, now=NOW)
        ingested = ingest.result()
        purge.result()
    assert ingested.status_code in (201, 404, 503)
    purge_expired(deps, now=NOW)
    assert post(world, event(world)).status_code == 404
    assert (
        world["writer"].get(f"/admin/api/v1/submissions/{world['submission']}/journey").status_code
        == 404
    )


def test_mcp_manages_its_own_source_and_credential_with_real_confirmation(world, tmp_path):
    from admin_mcp_live import Live
    from admin_mcp_world import Asker

    live = Live(world["writer"], tmp_path, lead_data=True)
    try:
        arguments = {
            **world["create_body"],
            "namespace": "mcp-synthetic",
            "idempotency_key": str(uuid4()),
        }
        arguments.pop("confirm")
        arguments["slug"] = "synthetic"
        source = live.ok("create_event_source", arguments, Asker(True))
        assert source["enabled"] is False
        issued = live.ok(
            "create_event_credential",
            {
                "source_id": source["id"],
                "name": "MCP sintética",
                "source_revision": 1,
                "expected_source_version": source["version"],
                "expires_at": None,
                "idempotency_key": str(uuid4()),
            },
            Asker(True),
        )
        assert issued["secret"].startswith("lke_")
        enabled = live.ok(
            "set_event_source_enabled",
            {"source_id": source["id"], "expected_version": source["version"], "enabled": True},
            Asker(True),
        )
        assert enabled["enabled"] is True
        response = world["service"].post(
            "/journey/events",
            json=event(world, kind="context", identifier="context"),
            headers={"Authorization": "Bearer " + issued["secret"]},
        )
        assert response.status_code == 201
        viewed = live.ok(
            "get_submission_journey",
            {"submission_id": world["submission"], "include_lead_data": True},
        )
        assert "contact-personal-marker" in str(viewed)
        safe = live.ok("get_submission_journey", {"submission_id": world["submission"]})
        assert "personal-marker" not in str(safe)
        erased = live.ok(
            "erase_submission_journey",
            {
                "submission_id": world["submission"],
                "expected_journey_version": safe["journey_version"],
                "expected_handoff_version": safe["reference_version"],
            },
            Asker(True),
        )
        assert erased["erased"] is True
        assert post(world, event(world)).status_code == 404
    finally:
        live.close()


def test_credential_explicit_nullable_expiry_is_enforced_and_expired_key_is_refused(world):
    path = url_of("create_event_credential", source_id=world["source"]["id"])
    body = {
        **world["credential_body"],
        "expected_source_version": world["source"]["version"],
        "idempotency_key": str(uuid4()),
    }
    body.pop("expires_at")
    assert world["writer"].post(path, json=body).status_code == 422
    body["expires_at"] = (NOW + timedelta(seconds=1)).isoformat()
    created = world["writer"].post(path, json=body)
    assert created.status_code == 201
    with TestClient(
        create_integration_api(clock=lambda: (NOW + timedelta(seconds=2)).timestamp())
    ) as client:
        expired = client.post(
            "/journey/events",
            content=b"{",
            headers={
                "Authorization": "Bearer " + created.json()["secret"],
                "Content-Type": "application/json",
            },
        )
    assert expired.status_code == 401 and expired.json() == {"code": "invalid_credentials"}


def test_withdrawal_before_snapshot_never_resurrects_older_revisions(world):
    withdrawn = event(world, 3)
    withdrawn.update(type="entity.withdrawn", data={"reason_code": "incorrect"})
    withdrawn["evidence"]["kind"] = "operator_correction"
    assert post(world, withdrawn).status_code == 201
    for revision in (1, 2):
        accepted = post(world, event(world, revision))
        assert accepted.status_code == 201 and accepted.json()["projection_effect"] == "historical"
    assert read(world)["external_declared"][0]["state"] == "withdrawn"
    assert read(world)["external_declared"][0]["entity_revision"] == 3


def test_other_form_reference_is_indistinguishable_from_missing_correlation(world, deps):
    from app.models import Lead, Submission

    with deps.sessions.begin() as session:
        receiver = HandoffReceiver(
            form_slug="demo",
            name="Outro receptor sintético",
            entry_url="https://example.test/other",
            reference_ttl_seconds=3600,
            created_by="teste",
            updated_by="teste",
        )
        lead = Lead(email="outra@example.test")
        session.add_all([receiver, lead])
        session.flush()
        submission = Submission(
            lead_id=lead.id,
            form_slug="demo",
            form_version="synthetic",
            form_version_number=1,
            idempotency_key="other-form",
            answers={},
            consent={},
        )
        session.add(submission)
        session.flush()
        reference = HandoffReference(
            submission_id=submission.id,
            receiver_id=receiver.id,
            receiver_revision=1,
            entry_url=receiver.entry_url,
            token_hash="synthetic-other-reference",
            issuance_state="ready",
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        session.add(reference)
        session.flush()
        reference_id = str(reference.id)
    refused = post(world, {**event(world), "reference_id": reference_id})
    missing = post(world, {**event(world), "reference_id": str(uuid4())})
    assert refused.status_code == missing.status_code == 404
    assert refused.json() == missing.json() == {"code": "correlation_unavailable"}


def test_same_source_event_id_across_two_references_conflicts_concurrently(world, deps):
    from concurrent.futures import ThreadPoolExecutor

    with deps.sessions.begin() as session:
        accepted = accept(session, world["spec"], deps, key="other-reference-concurrent")
        reference = session.scalar(
            sa.select(HandoffReference).where(
                HandoffReference.submission_id == accepted.submission.id
            )
        )
        other_id = str(reference.id)
    bodies = [event(world), {**event(world), "reference_id": other_id}]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda body: post(world, body), bodies))
    assert sorted(response.status_code for response in results) == [201, 409]
    assert next(response for response in results if response.status_code == 409).json() == {
        "code": "event_conflict"
    }


def test_capture_transport_is_local_and_never_implies_participant_receipt(world, deps):
    from app.models import CaptureDelivery, WebhookDestination

    with deps.sessions.begin() as session:
        destination = WebhookDestination(
            form_slug="synthetic",
            url="https://example.test/synthetic",
            secret_sealed="synthetic-unusable",
            purpose="internal",
            purpose_origin="explicit",
            created_by="teste",
            updated_by="teste",
        )
        session.add(destination)
        session.flush()
        session.add(
            CaptureDelivery(
                submission_id=UUID(world["submission"]),
                webhook_destination_id=destination.id,
                config_version=1,
                status="succeeded",
                delivered_at=NOW,
                created_at=NOW,
            )
        )
    view = read(world)
    assert view["job"] is None
    assert any(
        fact["type"] == "capture_transport"
        and fact["state"] == "succeeded"
        and fact["origin"] == "local"
        for fact in view["local_facts"]
    )
    assert view["external_declared"] == []


def test_journey_preserves_local_release_facts_without_external_declarations(
    deps, api_client, manual_pdf
):
    from test_plan_release import command_for

    from app.models import Job
    from app.plan_release import release_pdf

    job_id, artifact_id = manual_pdf
    with deps.sessions.begin() as session:
        job = session.get(Job, job_id)
        submission_id, public_id = job.submission_id, job.public_id
        release_pdf(session, deps, public_id, actor="operadora", command=command_for(artifact_id))
    response = api_client("leitura").get(
        url_of("get_submission_journey", submission_id=submission_id)
    )
    assert response.status_code == 200, response.text
    facts = response.json()["local_facts"]
    assert any(
        fact["type"] == "plan_authorization"
        and fact["state"] == "released"
        and fact["attempt"] == 1
        for fact in facts
    )
    assert response.json()["external_declared"] == [] and response.json()["reference_id"] is None


def test_html_missing_resources_and_invalid_filters_are_safe_errors(world):
    from test_admin import login

    client = world["writer"]
    assert login(client).status_code == 303
    assert client.get("/admin/event-sources/" + str(uuid4())).status_code == 404
    assert client.get("/admin/forms/missing-synthetic/event-sources").status_code == 404
    assert client.get("/admin/submissions/" + str(uuid4()) + "/journey").status_code == 404
    path = "/admin/submissions/" + world["submission"] + "/journey"
    for parameter in ("cursor", "type", "source_id"):
        response = client.get(path, params={parameter: "PRIVATE-INVALID-MARKER"})
        assert response.status_code == 422 and "PRIVATE-INVALID-MARKER" not in response.text
