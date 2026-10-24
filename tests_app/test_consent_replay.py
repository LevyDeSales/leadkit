"""Conflitos de consentimento em replay discriminado pela interface HTTP pública."""

from copy import deepcopy

import pytest
from conftest import DEMO_ANSWERS, publish
from fastapi.testclient import TestClient
from test_consent_methods import definition

from app.api.main import create_app
from app.forms.definition import compile_definition


def declaration(method, text_version):
    consent = {
        "method": method,
        "action": "confirm" if method == "checkbox" else "submit",
        "text_version": text_version,
    }
    if method == "checkbox":
        consent["privacy"] = True
    return consent


def configured(deps, method, generation):
    document = definition(acquisition_method=method)
    if generation == "none":
        document["generation"] = "none"
        document["completion"] = {"mode": "confirmation"}
        for key in ("instructions", "prompt", "plan", "pdf", "preview"):
            document.pop(key, None)
        document["examples"].pop("plan", None)
    spec = compile_definition(document, version_number=2)
    publish(deps, spec)

    async def trigger(*args):
        return "synthetic-run"

    return TestClient(create_app(trigger=trigger)), spec


@pytest.mark.parametrize("generation", ["pdf", "none"])
@pytest.mark.parametrize("method", ["checkbox", "submit"])
@pytest.mark.parametrize("change", ["method", "text_version", "marketing_email", "unoffered"])
def test_schema2_replay_conflicts_before_new_submission_policy_checks(
    deps, api_client, generation, method, change
):
    client, spec = configured(deps, method, generation)
    body = {"answers": DEMO_ANSWERS, "consent": declaration(method, spec.consent_text_version)}
    path = "/v1/forms/demo/submissions"
    headers = {"Idempotency-Key": "same-submission"}
    first = client.post(path, json=body, headers=headers)
    assert first.status_code == 202
    identity = first.json()["submission_id" if generation == "none" else "public_id"]
    protected = f"/admin/api/v1/{'captures' if generation == 'none' else 'jobs'}/{identity}"
    admin = api_client("total")
    original = admin.get(protected, params={"include_lead_data": True}).json()["lead_data"][
        "consent"
    ]
    changed = deepcopy(body)
    expected_new = 422
    if change == "method":
        changed["consent"] = declaration(
            "submit" if method == "checkbox" else "checkbox", spec.consent_text_version
        )
    elif change == "text_version":
        changed["consent"]["text_version"] = "another-version"
        expected_new = 409
    else:
        changed["consent"]["marketing_whatsapp" if change == "unoffered" else "marketing_email"] = (
            True
        )
        expected_new = 202 if change == "marketing_email" else 422
    conflict = client.post(path, json=changed, headers=headers)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json() == {"code": "idempotency_conflict"}
    replay = client.post(path, json=body, headers=headers)
    assert replay.status_code == 202 and replay.json()["replay"] is True
    assert replay.json()["submission_id" if generation == "none" else "public_id"] == identity
    assert (
        admin.get(protected, params={"include_lead_data": True}).json()["lead_data"]["consent"]
        == original
    )
    new = client.post(path, json=changed, headers={"Idempotency-Key": "new-submission"})
    assert new.status_code == expected_new, new.text
    if change == "text_version":
        assert new.json()["code"] == "consent_text_outdated"


@pytest.mark.parametrize("method", ["checkbox", "submit"])
@pytest.mark.parametrize("invalid", [{"action": "unknown"}, {"source": "public_html"}])
def test_schema2_replay_still_rejects_structurally_invalid_declarations(deps, method, invalid):
    client, spec = configured(deps, method, "pdf")
    body = {"answers": DEMO_ANSWERS, "consent": declaration(method, spec.consent_text_version)}
    path = "/v1/forms/demo/submissions"
    headers = {"Idempotency-Key": "same-submission"}
    assert client.post(path, json=body, headers=headers).status_code == 202
    changed = {**body, "consent": {**body["consent"], **invalid}}
    assert client.post(path, json=changed, headers=headers).status_code == 422


@pytest.mark.parametrize("change", ["method", "text_version", "unoffered", "marketing_email"])
def test_historical_records_keep_legacy_replay_policy(deps, change):
    import sqlalchemy as sa

    from app.models import Submission

    client, spec = configured(deps, "checkbox", "pdf")
    path = "/v1/forms/demo/submissions"
    headers = {"Idempotency-Key": "historical-submission"}
    body = {
        "answers": DEMO_ANSWERS,
        "consent": {"privacy": True, "text_version": spec.consent_text_version},
    }
    assert client.post(path, json=body, headers=headers).status_code == 202
    with deps.sessions.begin() as session:
        row = session.scalars(sa.select(Submission)).one()
        historical = {
            key: value
            for key, value in row.consent.items()
            if key not in {"evidence_schema", "method", "action", "source"}
        }
        row.consent = historical
    changed = deepcopy(body)
    if change == "method":
        changed["consent"] = declaration("submit", spec.consent_text_version)
        expected = 422
    elif change == "text_version":
        changed["consent"]["text_version"] = "old-text"
        expected = 409
    else:
        changed["consent"]["marketing_whatsapp" if change == "unoffered" else "marketing_email"] = (
            True
        )
        expected = 422 if change == "unoffered" else 202
    response = client.post(path, json=changed, headers=headers)
    assert response.status_code == expected
    if change == "text_version":
        assert response.json()["code"] == "consent_text_outdated"
    elif change == "marketing_email":
        assert response.json()["replay"] is True
    with deps.sessions() as session:
        assert session.scalars(sa.select(Submission)).one().consent == historical
