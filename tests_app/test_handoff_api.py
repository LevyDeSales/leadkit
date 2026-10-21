"""Autenticação antes do corpo e projeção mínima na interface HTTP de serviço."""

import pytest
from fastapi.testclient import TestClient
from handoff_support import isolated_handoff_contract  # noqa: F401
from test_handoffs import NOW, configured

from app.api.handoffs import create_integration_api


@pytest.mark.parametrize("body", [b"{", b'{"reference":"PII-SYNTHETIC-DO-NOT-ECHO"}'])
def test_missing_authentication_is_refused_before_body(body):
    with TestClient(create_integration_api()) as client:
        response = client.post(
            "/handoffs/resolve", content=body, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 401
    assert response.json() == {"code": "invalid_credentials"}
    assert "PII-SYNTHETIC" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_authenticated_malformed_body_has_no_input_echo(deps):
    with deps.sessions.begin() as session:
        _, created = configured(session)
    with TestClient(create_integration_api(clock=lambda: NOW.timestamp())) as client:
        response = client.post(
            "/handoffs/resolve",
            content=b"{",
            headers={
                "Authorization": f"Bearer {created.secret.get_secret_value()}",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 422
    assert "input" not in response.text


def test_service_schema_is_separate_and_contains_no_administrative_tools():
    import json
    from pathlib import Path

    schema = create_integration_api().openapi()
    assert schema == json.loads(
        (Path(__file__).resolve().parents[1] / "docs/api/integrations-openapi.json").read_text()
    )
    assert list(schema["paths"]) == ["/handoffs/resolve", "/journey/events"]
    assert schema["paths"]["/handoffs/resolve"]["post"]["security"] == [
        {"IntegrationCredentialAuth": []}
    ]


@pytest.mark.parametrize("generation", ["none", "pdf"])
def test_submission_commit_replay_and_distinct_submission_keep_their_own_reference(
    deps, generation
):
    from app.api.main import create_app
    from app.forms import starter

    with deps.sessions.begin() as session:
        spec, created = configured(session, generation=generation)
    document = starter.definition(slug="synthetic", title="Sintético", generation=generation)
    body = {
        "answers": document["examples"]["answers"],
        "consent": {"privacy": True, "text_version": spec.consent_text_version},
    }

    async def trigger(*args):
        return "synthetic"

    with TestClient(
        create_app(trigger=trigger, clock=lambda: NOW.timestamp()), follow_redirects=True
    ) as client:
        first = client.post(
            "/v1/forms/synthetic/submissions", json=body, headers={"Idempotency-Key": "one"}
        )
        assert first.status_code == 202, first.text
        second = client.post(
            "/v1/forms/synthetic/submissions", json=body, headers={"Idempotency-Key": "one"}
        )
        third = client.post(
            "/v1/forms/synthetic/submissions", json=body, headers={"Idempotency-Key": "two"}
        )
        assert second.json()["replay"] is True
        url = first.json()["completion"]["url"]
        assert first.json()["completion"]["handoff_status"] == "ready"
        assert second.json()["completion"]["url"] == url
        assert third.json()["completion"]["url"] != url
        assert "handoff" not in first.json()["completion"]
        receipt = client.get(first.json()["completion"]["receipt_url"])
        assert receipt.status_code == 200
        assert url in receipt.text
        assert "data-completion-redirect" in receipt.text
        resolved = client.post(
            "/integrations/v1/handoffs/resolve",
            json={"reference": url.split("leadkit_ref=")[1]},
            headers={"Authorization": f"Bearer {created.secret.get_secret_value()}"},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["generation"] == generation
        assert set(resolved.json()) == {
            "schema_version",
            "reference_id",
            "submission_id",
            "form_slug",
            "form_version_number",
            "generation",
            "purpose",
            "receiver_id",
            "receiver_revision",
            "projection",
            "submitted_at",
            "expires_at",
        }


def test_unknown_reference_and_admin_key_cannot_cross_the_service_boundary(deps, api_client):
    import secrets

    with deps.sessions.begin() as session:
        _, created = configured(session)
    writer = api_client("total")
    # O mesmo corpo opaco é indistinguível de uma referência fora do escopo.
    raw = "lkh_" + secrets.token_urlsafe(32)
    assert (
        writer.post("/integrations/v1/handoffs/resolve", json={"reference": raw}).status_code == 401
    )
    with TestClient(create_integration_api(clock=lambda: NOW.timestamp())) as client:
        unknown = client.post(
            "/handoffs/resolve",
            json={"reference": raw},
            headers={"Authorization": f"Bearer {created.secret.get_secret_value()}"},
        )
        assert unknown.status_code == 404 and unknown.json() == {"code": "handoff_unavailable"}
        assert raw not in unknown.text
