"""Acesso do lead ao próprio pedido: um token por job, derivado por HMAC, sem tabela.

- O token dá uma única capacidade: ver o status e baixar o PDF daquele job.
- Validade = `jobs.expires_at`; revogar = `jobs.token_version += 1`.
- Como é derivável, o worker monta o link do webhook sem guardar segredo e o reenvio de uma
  submissão devolve o mesmo token.
- Rotação de chave: o primeiro segredo assina, todos verificam.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class JobLike(Protocol):
    public_id: str
    token_version: int
    expires_at: datetime


class TokenError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # "malformed" | "invalid" | "expired"


def new_public_id() -> str:
    return secrets.token_urlsafe(12)


@dataclass(frozen=True)
class TokenSigner:
    secrets_: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.secrets_ or any(len(s) < 32 for s in self.secrets_):
            raise ValueError("use segredos aleatórios com 32+ caracteres")

    @staticmethod
    def _sig(secret: str, public_id: str, version: int) -> str:
        message = f"mcc-job:{public_id}:{version}".encode()
        mac = hmac.new(secret.encode(), message, hashlib.sha256).digest()[:18]
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def issue(self, public_id: str, token_version: int) -> str:
        return f"{public_id}.{self._sig(self.secrets_[0], public_id, token_version)}"

    @staticmethod
    def public_id_of(token: str) -> str:
        public_id, dot, sig = token.partition(".")
        if not dot or not public_id or not sig or len(token) > 128:
            raise TokenError("malformed")
        return public_id

    def verify(self, token: str, job: JobLike, now: datetime | None = None) -> None:
        """Chamar depois de carregar o job por `public_id_of(token)`. Levanta TokenError."""
        if self.public_id_of(token) != job.public_id:
            raise TokenError("invalid")
        given = token.partition(".")[2].encode()  # bytes: compare_digest(str) falha com não-ASCII
        candidates = (
            self._sig(s, job.public_id, job.token_version).encode() for s in self.secrets_
        )
        if not any(hmac.compare_digest(given, candidate) for candidate in candidates):
            raise TokenError("invalid")
        if (now or datetime.now(UTC)) > job.expires_at:
            raise TokenError("expired")
