"""Prova integrada: outbox real, HMAC real e recibo durável do receptor sintético."""

import hashlib

import httpx
import pytest
import sqlalchemy as sa
from persisted_webhook_support import accept_v2, configure_v2
from persisted_webhook_support import v2_ready as v2_ready  # noqa: PLC0414

from app import capture_deliveries
from app.adapters.webhook import WebhookTransport
from app.errors import RetryableError
from app.models import CaptureDelivery, WebhookDestination


@pytest.mark.parametrize("mode", ["normal", "timeout_before_commit", "timeout_after_commit"])
def test_durable_receiver_recovers_ambiguous_timeout_without_duplicate_effect(
    v2_ready, tmp_path, mode
):
    from persisted_receiver_support import DurableReceiver

    document, destination_id = configure_v2(v2_ready, "pdf")
    accept_v2(v2_ready, document)
    with v2_ready.sessions() as session:
        row = session.scalar(sa.select(CaptureDelivery))
        destination = session.get_one(WebhookDestination, destination_id)
        secret = v2_ready.cipher.open(destination.secret_sealed)
    path = tmp_path / "receipts.sqlite3"
    receiver = DurableReceiver(path, secret)
    receiver.mode = mode
    requests = []

    def receive(request):
        requests.append((request.headers["Idempotency-Key"], request.content))
        return receiver(request)

    with httpx.Client(transport=httpx.MockTransport(receive)) as client:
        v2_ready.webhook = WebhookTransport(client)
        if mode == "normal":
            assert capture_deliveries.dispatch(v2_ready, row.id, 1) == "succeeded"
        else:
            with pytest.raises(RetryableError):
                capture_deliveries.dispatch(v2_ready, row.id, 1)
            assert len(receiver.receipts()) == (mode == "timeout_after_commit")
            # Outra instância abre o mesmo arquivo; recibo aceito não depende da memória anterior.
            receiver = DurableReceiver(path, secret)
            assert capture_deliveries.dispatch(v2_ready, row.id, 1) == "succeeded"
        assert capture_deliveries.dispatch(v2_ready, row.id, 1) == "already_succeeded"
    receipts = DurableReceiver(path, secret).receipts()
    assert len(receipts) == 1
    assert receipts[0]["issuer"] == row.payload["issuer"]
    assert receipts[0]["event_id"] == str(row.id)
    assert receipts[0]["submission_id"] == str(row.submission_id)
    assert receipts[0]["body_sha256"] == hashlib.sha256(requests[0][1]).hexdigest()
    assert len(requests) == (1 if mode == "normal" else 2)
    assert all(key == str(row.id) and body == requests[0][1] for key, body in requests)
