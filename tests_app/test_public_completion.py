"""Conclusão após commit pelas fronteiras HTML e JSON, sem serviços externos."""

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
import sqlalchemy as sa
from conftest import DEMO_ANSWERS, DEMO_CONSENT, legacy_definition, publish
from fastapi.testclient import TestClient
from test_public import VALID_FORM, tags

from app import crons
from app.api.main import create_app
from app.deps import Deps
from app.forms.definition import compile_definition


def configured(deps: Deps, mode: str = "confirmation", **values) -> None:
    publish(
        deps,
        compile_definition(
            {**legacy_definition("demo"), "completion": {"mode": mode, **values}}, version_number=2
        ),
    )


async def trigger(job_id: uuid.UUID, attempt: int) -> str:
    return "fake-run"


def test_html_confirmation_is_a_receipt_without_waiting_for_the_pdf(deps: Deps) -> None:
    configured(deps, message="Recebemos <b>suas respostas</b> {{ sem_template }}")
    with TestClient(create_app(trigger=trigger), follow_redirects=False) as client:
        response = client.post("/p/demo", data=VALID_FORM)
        assert response.status_code == 303
        assert response.headers["location"].endswith("/conclusao")
        receipt = client.get(response.headers["location"])
    assert receipt.status_code == 200
    assert "Recebemos &lt;b&gt;suas respostas&lt;/b&gt; {{ sem_template }}" in receipt.text
    assert "hx-trigger" not in receipt.text
    assert not tags(receipt.text, "form")


def test_api_uses_the_same_completion_and_keeps_the_persisted_request_on_trigger_failure(
    deps: Deps,
) -> None:
    configured(deps, message="Tudo salvo.")

    async def failed_trigger(job_id: uuid.UUID, attempt: int) -> str:
        raise RuntimeError("falha sintética do trigger")

    with TestClient(create_app(trigger=failed_trigger), follow_redirects=False) as client:
        response = client.post(
            "/v1/forms/demo/submissions",
            json={"answers": DEMO_ANSWERS, "consent": DEMO_CONSENT},
            headers={"Idempotency-Key": "capture-receipt"},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["completion"]["mode"] == "confirmation"
        assert body["completion"]["message"] == "Tudo salvo."
        assert body["status"] == "queued"
        again = client.post(
            "/v1/forms/demo/submissions",
            json={"answers": DEMO_ANSWERS, "consent": DEMO_CONSENT},
            headers={"Idempotency-Key": "capture-receipt"},
        )
        assert again.json()["replay"]
        assert again.json()["completion"] == body["completion"]


def test_html_does_not_wait_for_a_slow_trigger_after_commit(deps: Deps) -> None:
    configured(deps)

    async def slow_trigger(job_id: uuid.UUID, attempt: int) -> str:
        await asyncio.sleep(3)
        return "fake-run"

    with TestClient(create_app(trigger=slow_trigger), follow_redirects=False) as client:
        started = time.monotonic()
        response = client.post("/p/demo", data=VALID_FORM)
        assert time.monotonic() - started < 1.5
        assert response.status_code == 303


@pytest.mark.parametrize("mode", ["button", "redirect"])
def test_receipt_has_an_exact_safe_button_without_posting_answers_to_the_destination(
    deps: Deps, mode: str
) -> None:
    destination = "https://example.test/next?source=diagnostico#continue"
    configured(deps, mode, url=destination, button_label="Seguir <em>agora</em>")
    with TestClient(create_app(trigger=trigger), follow_redirects=False) as client:
        sent = client.post("/p/demo?url=https://other.example.test", data=VALID_FORM)
        receipt = client.get(sent.headers["location"])
    (button,) = tags(receipt.text, "a", href=destination)
    assert ("data-completion-redirect" in button) == (mode == "redirect")
    assert "Seguir &lt;em&gt;agora&lt;/em&gt;" in receipt.text
    assert not tags(receipt.text, "form")
    assert "https://other.example.test" not in receipt.text
    assert DEMO_ANSWERS["email"] not in receipt.text
    assert "?t=" not in receipt.text


def test_replay_keeps_the_original_completion_after_publishing_a_different_version(
    deps: Deps,
) -> None:
    first = compile_definition(
        {
            **legacy_definition("demo"),
            "completion": {"mode": "confirmation", "message": "Primeiro recibo."},
        },
        version_number=2,
    )
    publish(deps, first)
    calls = []

    async def track(job_id, attempt):
        calls.append((job_id, attempt))
        return "fake-run"

    with TestClient(create_app(trigger=track), follow_redirects=False) as client:
        payload = {"answers": DEMO_ANSWERS, "consent": DEMO_CONSENT}
        original = client.post(
            "/v1/forms/demo/submissions", json=payload, headers={"Idempotency-Key": "same-request"}
        )
        document = legacy_definition("demo")
        document["consent"]["version"] = "demo-v3"
        document["completion"] = {"mode": "button", "url": "https://example.test/new"}
        publish(deps, first, compile_definition(document, version_number=3))
        replay = client.post(
            "/v1/forms/demo/submissions", json=payload, headers={"Idempotency-Key": "same-request"}
        )
        assert replay.status_code == 202
        assert replay.json()["completion"] == original.json()["completion"]
        assert replay.json()["public_id"] == original.json()["public_id"]
        assert replay.json()["replay"]
        assert len(calls) == 1
        bad = client.post(
            "/v1/forms/demo/submissions",
            json={**payload, "answers": {**DEMO_ANSWERS, "email": "different@example.test"}},
            headers={"Idempotency-Key": "same-request"},
        )
        assert bad.status_code == 409


def test_concurrent_api_replay_does_not_duplicate_the_submission_or_dispatch(deps: Deps) -> None:
    configured(deps)
    calls = []

    async def track(job_id, attempt):
        calls.append((job_id, attempt))
        return "fake-run"

    with TestClient(create_app(trigger=track), follow_redirects=False) as client:

        def send(_):
            return client.post(
                "/v1/forms/demo/submissions",
                json={"answers": DEMO_ANSWERS, "consent": DEMO_CONSENT},
                headers={"Idempotency-Key": "concurrent-receipt"},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(send, range(2))
        assert first.status_code == second.status_code == 202
        assert first.json()["public_id"] == second.json()["public_id"]
        assert sorted([first.json()["replay"], second.json()["replay"]]) == [False, True]
        assert len(calls) == 1


def test_a_failed_commit_returns_a_recoverable_form_without_success_or_dispatch(
    deps: Deps, monkeypatch
) -> None:
    configured(deps, "redirect", url="https://example.test/next")
    calls = []

    async def track(job_id, attempt):
        calls.append((job_id, attempt))
        return "fake-run"

    def unavailable():
        raise sa.exc.OperationalError("statement", {}, RuntimeError("falha sintética"))

    with TestClient(
        create_app(trigger=track), follow_redirects=False, raise_server_exceptions=False
    ) as client:
        with monkeypatch.context() as patch:
            patch.setattr(deps.sessions, "begin", unavailable)
            response = client.post("/p/demo", data=VALID_FORM)
        assert response.status_code == 503
        assert "Não conseguimos enviar o formulário" in response.text
        assert tags(response.text, "form")
        assert "location" not in response.headers
        assert calls == []
        retry = client.post("/p/demo", data=VALID_FORM)
        assert retry.status_code == 303


def test_an_interrupted_post_commit_trigger_is_recovered_by_the_existing_reconciler(
    deps: Deps, monkeypatch
) -> None:
    configured(deps)

    async def interrupted(job_id, attempt):
        raise asyncio.CancelledError

    with TestClient(create_app(trigger=interrupted), follow_redirects=False) as client:
        response = client.post(
            "/v1/forms/demo/submissions", json={"answers": DEMO_ANSWERS, "consent": DEMO_CONSENT}
        )
        assert response.status_code == 202
        status = client.get(response.json()["status_url"])
        assert status.json()["status"] == "queued"
    recovered = []

    def launch(job_id, attempt):
        recovered.append((job_id, attempt))
        return "recovered-run"

    monkeypatch.setattr(crons, "UNLAUNCHED_AFTER", timedelta(0))
    report = crons.reconcile_once(deps, launch_job=launch)
    assert report.jobs_launched == 1 and len(recovered) == 1


def test_html_replay_retains_the_old_receipt_after_consent_and_completion_change(
    deps: Deps,
) -> None:
    first = compile_definition(
        {
            **legacy_definition("demo"),
            "completion": {"mode": "confirmation", "message": "Recibo anterior."},
        },
        version_number=2,
    )
    publish(deps, first)
    with TestClient(create_app(trigger=trigger), follow_redirects=False) as client:
        sent = client.post("/p/demo", data=VALID_FORM)
        document = legacy_definition("demo")
        document["consent"]["version"] = "demo-v3"
        publish(deps, first, compile_definition(document, version_number=3))
        again = client.post("/p/demo", data=VALID_FORM)
        assert again.status_code == 303
        assert again.headers["location"] == sent.headers["location"]
        assert "Recibo anterior." in client.get(again.headers["location"]).text
