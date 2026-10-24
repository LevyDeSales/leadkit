"""Operadora e clientes consultam a mesma sincronização, sem carregar respostas."""

import json

import httpx
import pytest
import sqlalchemy as sa
from persisted_webhook_support import accept_v2, configure_v2
from persisted_webhook_support import v2_ready as v2_ready  # noqa: PLC0414

from app.adapters.webhook import WebhookTransport
from app.models import CaptureDelivery, Submission


@pytest.mark.parametrize("generation", ["none", "pdf"])
def test_delivery_status_reads_no_payload(v2_ready, api_client, generation):
    document, _ = configure_v2(v2_ready, generation)
    accepted = accept_v2(v2_ready, document)
    path = (
        f"/captures/{accepted.submission.id}"
        if generation == "none"
        else f"/jobs/{accepted.job.public_id}"
    )
    statements = []
    engine = v2_ready.sessions.kw["bind"]

    def observe(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    with api_client("leitura") as client:
        sa.event.listen(engine, "before_cursor_execute", observe)
        try:
            response = client.get("/admin/api/v1" + path)
        finally:
            sa.event.remove(engine, "before_cursor_execute", observe)
    assert response.status_code == 200
    body = response.json()
    rows = body["deliveries" if generation == "none" else "persisted_deliveries"]
    assert len(rows) == 1 and rows[0]["schema_version"] == 2
    assert rows[0]["status"] == "pending"
    assert not any("capture_deliveries.payload" in statement for statement in statements)
    assert "answers" not in response.text and "consent" not in response.text


def test_configuration_and_hub_accept_v2_schema(v2_ready, api_client):
    document, _ = configure_v2(v2_ready, "pdf")
    with api_client("leitura") as client:
        destination = client.get(f"/admin/api/v1/destinations/{document['slug']}")
        assert destination.status_code == 200
        assert destination.json()["serving_events"] == ["submissao.persistida.v2"]
        assert client.get("/admin/api/v1/integrations").status_code == 200


def test_pdf_retry_requires_confirmation_and_preserves_revision(v2_ready, api_client):
    document, _ = configure_v2(v2_ready, "pdf")
    accepted = accept_v2(v2_ready, document)
    with v2_ready.sessions.begin() as session:
        row = session.scalar(sa.select(CaptureDelivery))
        row.status = "failed"
    from admin_api_support import url_of

    path = url_of(
        "retry_job_persisted_delivery", public_id=accepted.job.public_id, delivery_id=str(row.id)
    )
    body = {"expected_dispatch": 1, "expected_config_version": 1}
    with api_client("leitura") as client:
        assert client.post(path, json={**body, "confirm": True}).status_code == 403
    with api_client() as client:
        assert client.post(path, json=body).status_code == 422
        response = client.post(path, json={**body, "confirm": True})
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        assert response.json()["dispatch_no"] == 2


def test_probe_follows_selected_contract_without_real_submission(v2_ready, api_client):
    document, _ = configure_v2(v2_ready, "pdf")
    received = []

    def receive(request):
        received.append(json.loads(request.content))
        return httpx.Response(204)

    v2_ready.webhook = WebhookTransport(httpx.Client(transport=httpx.MockTransport(receive)))
    with api_client() as client:
        response = client.post(
            f"/admin/api/v1/destinations/{document['slug']}/test-event",
            json={"confirm": True, "event": "submissao.persistida.v2"},
        )
    assert response.status_code == 200
    assert received[0]["schema_version"] == 2 and received[0]["test"] is True
    assert received[0]["event"] == "submissao.persistida"
    with v2_ready.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Submission)) == 0


def test_html_shows_saved_responses_and_retry_uses_csrf(v2_ready, api_client):
    from admin_api_support import ORIGIN
    from test_admin import csrf_token, login

    document, _ = configure_v2(v2_ready, "pdf")
    accepted = accept_v2(v2_ready, document)
    with v2_ready.sessions.begin() as session:
        row = session.scalar(sa.select(CaptureDelivery))
        row.status = "failed"
    with api_client(ADMIN_USERNAME="operadora") as client:
        assert login(client).status_code == 303
        page = client.get(f"/admin/jobs/{accepted.job.public_id}")
        assert page.status_code == 200 and "Respostas salvas" in page.text
        path = f"/admin/jobs/{accepted.job.public_id}/persisted-deliveries/{row.id}/retry"
        data = {"confirm": "on", "expected_dispatch": "1", "expected_config_version": "1"}
        assert client.post(path, data=data, headers=ORIGIN).status_code == 403
        response = client.post(
            path, data={**data, "csrf_token": csrf_token(client)}, headers=ORIGIN
        )
        assert response.status_code == 303


def test_mcp_derives_response_retry_and_v2_selection():
    from app.admin.api.openapi import build
    from app.admin_mcp.catalog import load

    catalog = load(build())
    tools = catalog.tools
    retry = tools["retry_job_persisted_delivery"]
    assert retry.role == "total" and retry.external_effect
    assert "confirm" in retry.input_schema["properties"]
    assert "submissao.persistida.v2" in json.dumps(tools["save_destination"].input_schema)
