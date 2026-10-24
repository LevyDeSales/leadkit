"""Contrato HTTP do receptor sintético durável, sem conexão externa."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import httpx
import pytest
from persisted_receiver_support import DurableReceiver

from app.adapters.webhook import WebhookTransport
from app.errors import FailureCode, RetryableError

SECRET = "segredo-sintetico-do-receptor"
URL = "https://receiver.example.test/events"
EVENT = "submissao.persistida"
ISSUER = "leadkit:00000000-0000-4000-8000-000000000001"
EVENT_ID = "00000000-0000-4000-8000-000000000002"
SUBMISSION_ID = "00000000-0000-4000-8000-000000000003"


@pytest.fixture
def payload() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "event": EVENT,
        "event_id": EVENT_ID,
        "issuer": ISSUER,
        "occurred_at": "2026-10-24T12:00:00+00:00",
        "form": {"slug": "diagnostico-sintetico", "version": "v1", "version_number": 1},
        "submission": {"id": SUBMISSION_ID, "submitted_at": "2026-10-24T12:00:00+00:00"},
        "generation": "pdf",
        "delivery_authorization": {
            "destination_id": "00000000-0000-4000-8000-000000000004",
            "revision": 1,
            "purpose": "internal",
        },
        "answers": {"objetivo": "Organização 💜", "equipe": {"cidades": ["São Paulo", "Belém"]}},
        "consent": {"privacy": True, "text_version": "sintetica-v1"},
    }


def signed_request(
    payload: Any, *, timestamp: int | str | None = None, secret: str = SECRET
) -> httpx.Request:
    """Assina os bytes independentemente do adaptador de produção."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    signature = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256)
    return httpx.Request(
        "POST",
        URL,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-MCC-Event": EVENT,
            "X-MCC-Timestamp": stamp,
            "X-MCC-Signature": "v1=" + signature.hexdigest(),
            "Idempotency-Key": EVENT_ID,
        },
    )


def test_receiver_authenticates_real_signature_before_durable_accept(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    path = tmp_path / "receiver.sqlite3"
    receiver = DurableReceiver(path, SECRET)
    with httpx.Client(transport=httpx.MockTransport(receiver)) as client:
        status = WebhookTransport(client).send(URL, SECRET, EVENT, EVENT_ID, payload)
        # A resposta já exige que outra conexão consiga observar recibo e efeito.
        assert status == 200
        receipts = DurableReceiver(path, SECRET).receipts()
    assert len(receipts) == 1
    assert receipts[0]["issuer"] == ISSUER
    assert receipts[0]["event_id"] == EVENT_ID
    assert receipts[0]["submission_id"] == SUBMISSION_ID
    assert receipts[0]["receipt_id"]
    assert receipts[0]["effect_id"]
    assert receipts[0]["body_sha256"] == hashlib.sha256(signed_request(payload).content).hexdigest()


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_secret",
        "missing_signature",
        "bad_signature",
        "tampered_body",
        "old",
        "future",
        "bad_time",
    ],
)
def test_receiver_rejects_unauthenticated_requests_without_receipts(
    tmp_path: Path, payload: dict[str, Any], fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000)
    receiver = DurableReceiver(tmp_path / "receiver.sqlite3", SECRET)
    request = signed_request(payload)
    if fault == "wrong_secret":
        request = signed_request(payload, secret="outro-segredo-sintetico")
    elif fault == "missing_signature":
        del request.headers["X-MCC-Signature"]
    elif fault == "bad_signature":
        request.headers["X-MCC-Signature"] = "v2=invalid"
    elif fault == "tampered_body":
        request = httpx.Request(
            "POST",
            URL,
            headers=request.headers,
            content=request.content + b" ",
        )
    else:
        timestamp = {
            "old": int(time.time()) - 301,
            "future": int(time.time()) + 301,
            "bad_time": "oops",
        }
        request = signed_request(payload, timestamp=timestamp[fault])
    assert receiver(request).status_code == 401
    assert receiver.receipts() == []


@pytest.mark.parametrize("offset", [-300, 300])
def test_receiver_accepts_exact_timestamp_window_boundary(
    tmp_path: Path, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch, offset: int
) -> None:
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000)
    receiver = DurableReceiver(tmp_path / "receiver.sqlite3", SECRET)
    assert receiver(signed_request(payload, timestamp=1_800_000_000 + offset)).status_code == 200
    assert len(receiver.receipts()) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 1),
        ("schema_version", "2"),
        ("event", "plano.concluido"),
        ("event_id", ""),
        ("event_id", "outro-evento"),
        ("issuer", ""),
        ("issuer", "leadkit:invalido"),
        ("issuer", "outro:00000000-0000-4000-8000-000000000001"),
        ("submission", None),
        ("submission", {}),
        ("submission", {"id": "email@example.test"}),
    ],
)
def test_receiver_refuses_incoherent_v2_identity(
    tmp_path: Path, payload: dict[str, Any], field: str, value: Any
) -> None:
    receiver = DurableReceiver(tmp_path / "receiver.sqlite3", SECRET)
    payload[field] = value
    assert receiver(signed_request(payload)).status_code == 400
    assert receiver.receipts() == []


@pytest.mark.parametrize("header", ["X-MCC-Event", "Idempotency-Key"])
@pytest.mark.parametrize("value", [None, "divergente"])
def test_receiver_requires_headers_matching_signed_envelope(
    tmp_path: Path, payload: dict[str, Any], header: str, value: str | None
) -> None:
    receiver = DurableReceiver(tmp_path / "receiver.sqlite3", SECRET)
    request = signed_request(payload)
    if value is None:
        del request.headers[header]
    else:
        request.headers[header] = value
    assert receiver(request).status_code == 400
    assert receiver.receipts() == []


@pytest.mark.parametrize("body", [None, [], "texto", {"schema_version": 2}])
def test_receiver_rejects_incomplete_json_envelope(tmp_path: Path, body: Any) -> None:
    receiver = DurableReceiver(tmp_path / "receiver.sqlite3", SECRET)
    assert receiver(signed_request(body)).status_code == 400
    assert receiver.receipts() == []


def test_receiver_authenticates_before_parsing_body(tmp_path: Path) -> None:
    receiver = DurableReceiver(tmp_path / "receiver.sqlite3", SECRET)
    request = httpx.Request("POST", URL, content=b"invalid json")
    assert receiver(request).status_code == 401
    assert receiver.receipts() == []


def test_receiver_deduplicates_across_reopen_and_concurrency(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    path = tmp_path / "receiver.sqlite3"
    receivers = [DurableReceiver(path, SECRET) for _ in range(8)]
    barrier = Barrier(len(receivers))

    def send(receiver: DurableReceiver) -> str:
        with httpx.Client(transport=httpx.MockTransport(receiver)) as client:
            barrier.wait(timeout=5)
            response = client.send(signed_request(payload))
            assert response.status_code == 200
            return response.json()["receipt_id"]

    with ThreadPoolExecutor(max_workers=len(receivers)) as executor:
        receipt_ids = list(executor.map(send, receivers))
    reopened = DurableReceiver(path, SECRET)
    original = reopened.receipts()
    assert len(original) == 1
    assert receipt_ids == [original[0]["receipt_id"]] * 8
    assert reopened(signed_request(payload)).json()["receipt_id"] == original[0]["receipt_id"]
    assert DurableReceiver(path, SECRET).receipts() == original


@pytest.mark.parametrize("change", ["answers", "submission", "bytes"])
def test_receiver_rejects_same_issuer_event_with_different_body(
    tmp_path: Path, payload: dict[str, Any], change: str
) -> None:
    path = tmp_path / "receiver.sqlite3"
    receiver = DurableReceiver(path, SECRET)
    assert receiver(signed_request(payload)).status_code == 200
    original = receiver.receipts()
    if change == "answers":
        payload["answers"]["equipe"]["cidades"].append("Brasília")
    elif change == "submission":
        payload["submission"]["id"] = "00000000-0000-4000-8000-000000000099"
    request = signed_request(payload)
    if change == "bytes":
        body = request.content + b" "
        request = httpx.Request("POST", URL, headers=request.headers, content=body)
        stamp = request.headers["X-MCC-Timestamp"].encode()
        digest = hmac.new(SECRET.encode(), stamp + b"." + body, hashlib.sha256).hexdigest()
        request.headers["X-MCC-Signature"] = "v1=" + digest
    assert DurableReceiver(path, SECRET)(request).status_code == 409
    assert DurableReceiver(path, SECRET).receipts() == original


def test_receiver_scopes_transport_identity_by_issuer(
    tmp_path: Path, payload: dict[str, Any]
) -> None:
    receiver = DurableReceiver(tmp_path / "receiver.sqlite3", SECRET)
    assert receiver(signed_request(payload)).status_code == 200
    payload["issuer"] = "leadkit:00000000-0000-4000-8000-000000000099"
    assert receiver(signed_request(payload)).status_code == 200
    receipts = receiver.receipts()
    assert len(receipts) == 2
    assert {row["event_id"] for row in receipts} == {EVENT_ID}
    assert len({row["receipt_id"] for row in receipts}) == 2
    assert len({row["effect_id"] for row in receipts}) == 2


@pytest.mark.parametrize("mode", ["timeout_before_commit", "timeout_after_commit"])
def test_retry_after_timeout_before_and_after_accept_preserves_one_logical_effect(
    tmp_path: Path, payload: dict[str, Any], mode: str
) -> None:
    path = tmp_path / "receiver.sqlite3"
    receiver = DurableReceiver(path, SECRET)
    receiver.mode = mode
    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return receiver(request)

    with httpx.Client(transport=httpx.MockTransport(receive)) as client:
        sender = WebhookTransport(client)
        with pytest.raises(RetryableError) as failure:
            sender.send(URL, SECRET, EVENT, EVENT_ID, payload)
        assert failure.value.code == FailureCode.DELIVERY_UNAVAILABLE
        reopened = DurableReceiver(path, SECRET)
        after_timeout = reopened.receipts()
        assert len(after_timeout) == (1 if mode == "timeout_after_commit" else 0)
        receiver = reopened
        assert receiver.mode == "normal"
        assert sender.send(URL, SECRET, EVENT, EVENT_ID, payload) == 200
    receipts = DurableReceiver(path, SECRET).receipts()
    assert len(requests) == 2
    assert [request.headers["Idempotency-Key"] for request in requests] == [EVENT_ID, EVENT_ID]
    assert requests[0].content == requests[1].content
    assert len(receipts) == 1
    assert receipts[0]["event_id"] == EVENT_ID
    if mode == "timeout_after_commit":
        assert receipts == after_timeout


def test_receiver_never_acknowledges_failed_commit(tmp_path: Path, payload: dict[str, Any]) -> None:
    path = tmp_path / "receiver.sqlite3"
    receiver = DurableReceiver(path, SECRET)
    # Um leitor SQLite real permite INSERTs, mas impede COMMIT no modo de journal padrão.
    reader = sqlite3.connect(path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM sqlite_master").fetchall()
        with httpx.Client(transport=httpx.MockTransport(receiver)) as client:
            sender = WebhookTransport(client)
            with pytest.raises(RetryableError) as failure:
                sender.send(URL, SECRET, EVENT, EVENT_ID, payload)
            assert failure.value.code == FailureCode.DELIVERY_UNAVAILABLE
        assert DurableReceiver(path, SECRET).receipts() == []
    finally:
        reader.rollback()
        reader.close()
    with httpx.Client(transport=httpx.MockTransport(DurableReceiver(path, SECRET))) as client:
        assert WebhookTransport(client).send(URL, SECRET, EVENT, EVENT_ID, payload) == 200
    assert len(receiver.receipts()) == 1
