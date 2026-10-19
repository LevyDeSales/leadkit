"""Envio do resultado para o webhook do cliente.

Contrato com o receptor:
- `POST` JSON com `Content-Type: application/json`;
- `Idempotency-Key`: o `event_id` (o mesmo evento pode chegar mais de uma vez; deduplique por ele);
- `X-MCC-Event`, `X-MCC-Timestamp` (unix) e `X-MCC-Signature: v1=<hex>`, onde
  hex = HMAC-SHA256(segredo do destino, f"{timestamp}.{corpo}");
- 2xx = entregue; 408, 425, 429 e 5xx = tentamos de novo com backoff; outros 4xx = definitivo.
- Redirecionamentos não são seguidos: um 3xx conta como recusa definitiva.

A URL e o segredo vêm do destino do webhook resolvido na hora do envio (`app.destinations`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.errors import FailureCode, RetryableError, TerminalError

RETRYABLE_STATUS = frozenset({408, 425, 429})
TIMEOUT = httpx.Timeout(15.0, connect=5.0)
PROBE_BODY_BYTES = 2048  # do corpo da resposta, o evento de teste lê só o começo

Outcome = Literal["delivered", "retry", "rejected"]


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256)
    return f"v1={mac.hexdigest()}"


def outcome(status: int) -> Outcome:
    """O que a entrega faz com a resposta: conta como entregue, repete ou desiste de vez."""
    if 200 <= status < 300:
        return "delivered"
    if status >= 500 or status in RETRYABLE_STATUS:
        return "retry"
    return "rejected"


def new_client() -> httpx.Client:
    """O cliente HTTP do contrato: os timeouts acima e sem seguir redirecionamentos."""
    return httpx.Client(timeout=TIMEOUT, follow_redirects=False)


@dataclass(frozen=True)
class Probe:
    """O que o receptor respondeu a um evento mandado só para ver a resposta (o evento de
    teste). `status` None: não houve resposta, e `error` diz o tipo da falha."""

    status: int | None
    elapsed_ms: int
    body_start: str = ""
    error: str | None = None


class WebhookSender:
    def __init__(self, url: str, secret: str, *, client: httpx.Client | None = None) -> None:
        self.url = url
        self.secret = secret
        self.client = client or new_client()

    def _signed(
        self, event: str, event_id: str, payload: dict[str, Any]
    ) -> tuple[bytes, dict[str, str]]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = int(time.time())
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": event_id,
            "X-MCC-Event": event,
            "X-MCC-Timestamp": str(timestamp),
            "X-MCC-Signature": sign(self.secret, timestamp, body),
        }
        return body, headers

    def send(self, event: str, event_id: str, payload: dict[str, Any]) -> int:
        body, headers = self._signed(event, event_id, payload)
        try:
            response = self.client.post(self.url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise RetryableError(FailureCode.DELIVERY_UNAVAILABLE, type(exc).__name__) from exc
        status = response.status_code
        verdict = outcome(status)
        if verdict == "delivered":
            return status
        if verdict == "retry":
            raise RetryableError(FailureCode.DELIVERY_UNAVAILABLE, f"HTTP {status}")
        raise TerminalError(FailureCode.DELIVERY_REJECTED, f"HTTP {status}")

    def probe(self, event: str, event_id: str, payload: dict[str, Any]) -> Probe:
        """Manda o evento assinado como `send` e devolve a resposta sem classificar: o status, o
        tempo até ela chegar e o começo do corpo. Nunca levanta por falha de rede."""
        body, headers = self._signed(event, event_id, payload)
        started = time.monotonic()
        try:
            with self.client.stream("POST", self.url, content=body, headers=headers) as response:
                elapsed_ms = round((time.monotonic() - started) * 1000)
                start = bytearray()
                for chunk in response.iter_bytes():
                    start += chunk
                    if len(start) >= PROBE_BODY_BYTES:
                        break
        except httpx.HTTPError as exc:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            return Probe(status=None, elapsed_ms=elapsed_ms, error=type(exc).__name__)
        text = bytes(start[:PROBE_BODY_BYTES]).decode("utf-8", errors="replace")
        return Probe(status=response.status_code, elapsed_ms=elapsed_ms, body_start=text)


class WebhookTransport:
    """Manda eventos assinados a qualquer destino, com um cliente HTTP só para todos. É a costura
    que os testes trocam por um transporte que só grava."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or new_client()

    def send(
        self, url: str, secret: str, event: str, event_id: str, payload: dict[str, Any]
    ) -> int:
        return WebhookSender(url, secret, client=self.client).send(event, event_id, payload)

    def probe(
        self, url: str, secret: str, event: str, event_id: str, payload: dict[str, Any]
    ) -> Probe:
        return WebhookSender(url, secret, client=self.client).probe(event, event_id, payload)
