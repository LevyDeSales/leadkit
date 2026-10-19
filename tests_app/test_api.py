from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
import sqlalchemy as sa
from conftest import DEMO_ANSWERS, DEMO_CONSENT, publish
from fastapi.testclient import TestClient

from app import jobs, pipeline
from app.api.main import MAX_BODY_BYTES, create_app
from app.deps import Deps
from app.forms import FormSpec
from app.models import Job, Lead, utcnow


@pytest.fixture
def triggered() -> list[tuple[uuid.UUID, int]]:
    return []


@pytest.fixture
def client(deps: Deps, triggered: list[tuple[uuid.UUID, int]]) -> TestClient:
    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        triggered.append((job_id, attempt))
        return "run-fake"

    return TestClient(create_app(trigger=fake_trigger))


def post(client: TestClient, key: str | None = "chave-1", **body: object) -> object:
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        "/v1/forms/demo/submissions",
        json={"answers": DEMO_ANSWERS, "consent": DEMO_CONSENT, **body},
        headers=headers,
    )


def test_health_and_readiness(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200


def test_submission_returns_token_and_triggers_once(client: TestClient, triggered: list) -> None:
    response = post(client)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued" and not body["replay"]
    assert body["status_url"].startswith("http://testserver/v1/jobs/")
    assert response.headers["cache-control"] == "no-store"
    assert len(triggered) == 1

    again = post(client)
    assert again.json()["replay"] and again.json()["token"] == body["token"]
    assert len(triggered) == 1  # reenvio não dispara de novo


def test_unknown_form_and_validation_errors_do_not_echo_input(client: TestClient) -> None:
    assert client.post("/v1/forms/inexistente/submissions", json={}).status_code == 404
    response = post(client, answers={**DEMO_ANSWERS, "email": "segredo-nao-ecoar"})
    assert response.status_code == 422
    assert "segredo-nao-ecoar" not in response.text
    assert any(error["field"] == "email" for error in response.json()["errors"])
    assert post(client, consent={"privacy": False, "text_version": "demo-v1"}).status_code == 422
    conflict = post(client, consent={"privacy": True, "text_version": "velha"})
    assert conflict.status_code == 409 and conflict.json()["current_version"] == "demo-v1"


def test_marketing_channel_the_form_does_not_ask_for_is_a_validation_error(
    client: TestClient, triggered: list
) -> None:
    asked = post(client, key="a", consent={**DEMO_CONSENT, "marketing_email": True})
    assert asked.status_code == 202

    response = post(client, key="b", consent={**DEMO_CONSENT, "marketing_whatsapp": True})

    assert response.status_code == 422  # o demo não pede o canal WhatsApp
    assert response.json() == {
        "errors": [
            {
                "field": "consent.marketing_whatsapp",
                "message": "Este formulário não solicita autorização de marketing por este canal.",
            }
        ]
    }
    assert len(triggered) == 1


def test_submission_is_validated_by_the_current_version_of_the_form(
    client: TestClient, deps: Deps, newer_demo: FormSpec
) -> None:
    publish(deps, replace(newer_demo, consent_text_version="demo-v2"))

    outdated = post(client)  # com o texto do consentimento da versão 1
    assert outdated.status_code == 409 and outdated.json()["current_version"] == "demo-v2"
    assert post(client, consent={**DEMO_CONSENT, "text_version": "demo-v2"}).status_code == 202


def test_idempotency_conflict(client: TestClient) -> None:
    post(client, key="k")
    response = post(client, key="k", answers={**DEMO_ANSWERS, "principal_desafio": "vendas"})
    assert response.status_code == 409


def test_status_requires_valid_token(client: TestClient) -> None:
    body = post(client).json()
    public_id, token = body["public_id"], body["token"]

    by_header = client.get(f"/v1/jobs/{public_id}", headers={"Authorization": f"Bearer {token}"})
    assert by_header.status_code == 200 and by_header.json()["status"] == "queued"
    assert client.get(f"/v1/jobs/{public_id}", params={"t": token}).status_code == 200
    assert client.get(f"/v1/jobs/{public_id}").status_code == 401
    assert (
        client.get(f"/v1/jobs/{public_id}", params={"t": f"{public_id}.errado"}).status_code == 401
    )
    assert client.get(f"/v1/jobs/{public_id}", params={"t": f"{public_id}.é"}).status_code == 401
    assert client.get("/v1/jobs/naoexiste", params={"t": "naoexiste.x"}).status_code == 401


def test_expired_link_is_gone(client: TestClient, deps: Deps) -> None:
    body = post(client).json()
    with deps.sessions.begin() as session:
        session.execute(sa.update(Job).values(expires_at=utcnow() - timedelta(minutes=1)))
    response = client.get(f"/v1/jobs/{body['public_id']}", params={"t": body["token"]})
    assert response.status_code == 410


def test_pdf_download_after_completion(client: TestClient, deps: Deps) -> None:
    body = post(client).json()
    public_id, token = body["public_id"], body["token"]
    assert client.get(f"/v1/jobs/{public_id}/pdf", params={"t": token}).status_code == 409

    with deps.sessions() as session:
        job_id = session.scalar(sa.select(Job.id).where(Job.public_id == public_id))
    with deps.sessions.begin() as session:
        jobs.claim(session, job_id, 1, "run-fake")
    plan_id, _ = pipeline.generate_plan(deps, job_id, 1)
    artifact_id, _ = pipeline.render_pdf(deps, job_id, 1, plan_id)
    pipeline.finalize(deps, job_id, 1, artifact_id)

    status = client.get(f"/v1/jobs/{public_id}", params={"t": token}).json()
    assert status["status"] == "completed" and status["pdf_url"].endswith(token)
    pdf = client.get(f"/v1/jobs/{public_id}/pdf", headers={"Authorization": f"Bearer {token}"})
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF") and "attachment" in pdf.headers["content-disposition"]
    assert pdf.headers["referrer-policy"] == "no-referrer"


def test_body_size_is_limited_even_without_content_length(client: TestClient) -> None:
    big = b"{" + b" " * (MAX_BODY_BYTES + 10) + b"}"
    declared = client.post(
        "/v1/forms/demo/submissions", content=big, headers={"Content-Type": "application/json"}
    )
    assert declared.status_code == 413

    def chunks():  # sem Content-Length (transfer-encoding chunked)
        yield b"{"
        for _ in range(40):
            yield b" " * 8192
        yield b"}"

    chunked = client.post(
        "/v1/forms/demo/submissions", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert chunked.status_code == 413


# --- limites de envio ---------------------------------------------------------------------------


def post_from(client: TestClient, number: int, key: str | None = None) -> object:
    """Um envio de uma lead diferente a cada número."""
    return client.post(
        "/v1/forms/demo/submissions",
        json={
            "answers": {**DEMO_ANSWERS, "email": f"lead{number}@example.test"},
            "consent": DEMO_CONSENT,
        },
        headers={"Idempotency-Key": key or f"lead-{number}"},
    )


def count_rows(deps: Deps, model: type) -> int:
    with deps.sessions() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(model)) or 0


def test_fourth_submission_from_the_same_email_is_refused_with_retry_after(
    client: TestClient, deps: Deps, triggered: list
) -> None:
    for number in range(3):
        assert post(client, key=f"chave-{number}").status_code == 202

    response = post(client, key="chave-3")

    assert response.status_code == 429
    assert response.json()["code"] == "submission_limit_exceeded"
    assert 0 < int(response.headers["retry-after"]) <= 24 * 3600
    assert response.headers["cache-control"] == "no-store"
    assert "contadora" not in response.text.lower()  # nada da lead volta na resposta
    assert len(triggered) == 3 and count_rows(deps, Job) == 3  # o recusado não gerou pedido


def test_other_emails_are_not_affected_by_one_emails_limit(client: TestClient) -> None:
    for number in range(3):
        post(client, key=f"chave-{number}")

    assert post(client, key="chave-3").status_code == 429
    assert post_from(client, 1).status_code == 202


def test_replaying_a_counted_submission_returns_the_same_result_at_the_limit(
    client: TestClient, triggered: list
) -> None:
    first = post(client, key="chave-0").json()
    post(client, key="chave-1")
    post(client, key="chave-2")

    again = post(client, key="chave-0")

    assert again.status_code == 202
    assert again.json()["replay"] and again.json()["token"] == first["token"]
    assert len(triggered) == 3  # o replay não dispara de novo
    assert post(client, key="chave-3").status_code == 429


def test_submission_is_refused_with_retry_after_while_the_queue_is_full(
    client: TestClient, deps: Deps, triggered: list
) -> None:
    deps.settings = deps.settings.model_copy(update={"submission_limit_queue": 2})
    assert post_from(client, 1).status_code == 202
    assert post_from(client, 2).status_code == 202

    full = post_from(client, 3)

    assert full.status_code == 503
    assert full.json()["code"] == "queue_full"
    assert full.headers["retry-after"] == "300"
    assert full.headers["cache-control"] == "no-store"
    assert len(triggered) == 2 and count_rows(deps, Job) == 2
    assert count_rows(deps, Lead) == 2
    replay = post_from(client, 2)  # o replay passa, mesmo com a fila cheia
    assert replay.status_code == 202 and replay.json()["replay"]
