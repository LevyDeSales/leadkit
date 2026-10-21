"""Capacidade interna revogável, isolada dos tokens públicos e conferida no banco a cada uso."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app import jobs
from app.deployment_contract import assert_contract
from app.integration_readiness import configuration_lock
from app.models import Artifact, Integration, Job, Plan, Submission, WebhookDestination, utcnow
from app.tokens import TokenError


class InternalLinkClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    job_public_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    attempt: int = Field(ge=1)
    artifact_id: UUID
    token_version: int = Field(ge=1)
    receiver_kind: Literal["webhook", "integration"]
    receiver_id: UUID
    receiver_version: int = Field(ge=1)
    expires_at: int = Field(ge=1)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _document(claims: InternalLinkClaims) -> bytes:
    return json.dumps(
        claims.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


@dataclass(frozen=True)
class InternalLinkSigner:
    secrets_: tuple[str, ...]

    def __post_init__(self):
        if not self.secrets_ or any(len(secret) < 32 for secret in self.secrets_):
            raise ValueError("use segredos com 32+ caracteres")

    @staticmethod
    def _signature(secret: str, payload: str) -> str:
        return _encode(
            hmac.new(
                secret.encode(), b"leadkit-internal-pdf-v1:" + payload.encode(), hashlib.sha256
            ).digest()
        )

    def issue(self, claims: InternalLinkClaims) -> str:
        payload = _encode(_document(claims))
        return "i1." + payload + "." + self._signature(self.secrets_[0], payload)

    def verify(self, token: str, *, now: datetime) -> InternalLinkClaims:
        try:
            if not isinstance(token, str) or len(token) > 1024:
                raise ValueError
            prefix, payload, given = token.split(".")
            if prefix != "i1" or not any(
                hmac.compare_digest(given.encode(), self._signature(secret, payload).encode())
                for secret in self.secrets_
            ):
                raise ValueError
            raw = base64.b64decode(
                payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True
            )
            claims = InternalLinkClaims.model_validate_json(raw)
            if _document(claims) != raw:
                raise ValueError
            if now.timestamp() >= claims.expires_at:
                raise TokenError("expired")
            return claims
        except (ValueError, TypeError, ValidationError, UnicodeError):
            raise TokenError("invalid") from None


@dataclass(frozen=True)
class ParticipantPdfAccess:
    job_id: UUID
    attempt: int
    token_version: int


@dataclass(frozen=True)
class InternalPdfAccess:
    claims: InternalLinkClaims


@dataclass(frozen=True)
class AdminPdfAccess:
    """Construído somente depois da autenticação administrativa no servidor."""


PdfAccess = ParticipantPdfAccess | InternalPdfAccess | AdminPdfAccess


def validate_internal_access(
    session: Session, deps, claims: InternalLinkClaims
) -> tuple[Job, Artifact]:
    """A chamadora mantém esta sessão protegida até ler bytes ou assinar a URL."""
    configuration_lock(session, shared=True)
    assert_contract(session, required=2)
    job = session.scalar(
        sa.select(Job).where(Job.public_id == claims.job_public_id).with_for_update()
    )
    if (
        job is None
        or job.attempt != claims.attempt
        or job.token_version != claims.token_version
        or job.status != "completed"
        or utcnow().timestamp() >= claims.expires_at
        or job.expires_at <= utcnow()
        or jobs.delivery_retention_expired(session, job, deps.settings.retention_days)
    ):
        raise TokenError("invalid")
    artifact = session.scalar(
        sa.select(Artifact).where(Artifact.id == claims.artifact_id).with_for_update()
    )
    if (
        artifact is None
        or artifact.job_id != job.id
        or artifact.version != claims.attempt
        or artifact.kind != "pdf"
        or artifact.purged_at is not None
    ):
        raise TokenError("invalid")
    plan = session.get(Plan, artifact.plan_id)
    if plan is None or plan.job_id != job.id or plan.version != claims.attempt:
        raise TokenError("invalid")
    model = WebhookDestination if claims.receiver_kind == "webhook" else Integration
    receiver = session.scalar(
        sa.select(model).where(model.id == claims.receiver_id).with_for_update()
    )
    submission = session.get_one(Submission, job.submission_id)
    if (
        receiver is None
        or receiver.form_slug
        not in (
            {submission.form_slug, None}
            if claims.receiver_kind == "webhook"
            else {submission.form_slug}
        )
        or receiver.version != claims.receiver_version
        or receiver.purpose != "internal"
        or (claims.receiver_kind == "integration" and not receiver.active)
    ):
        raise TokenError("invalid")
    return job, artifact


def internal_links(
    session: Session,
    deps,
    job: Job,
    artifact: Artifact,
    *,
    receiver_kind: Literal["webhook", "integration"],
    receiver_id: UUID,
    receiver_version: int,
) -> dict[str, str]:
    """Emite apenas depois da mesma revalidação usada no consumo da capacidade."""
    claims = InternalLinkClaims(
        job_public_id=job.public_id,
        attempt=artifact.version,
        artifact_id=artifact.id,
        token_version=job.token_version,
        receiver_kind=receiver_kind,
        receiver_id=receiver_id,
        receiver_version=receiver_version,
        expires_at=int(min(job.expires_at, utcnow() + timedelta(hours=24)).timestamp()),
    )
    validate_internal_access(session, deps, claims)
    token = InternalLinkSigner(deps.signer.secrets_).issue(claims)
    base = deps.settings.public_base_url.rstrip("/") + "/i/jobs/" + job.public_id
    query = "?" + urlencode({"k": token})
    return {
        "status_url": base + "/status" + query,
        "page_url": base + query,
        "pdf_url": base + "/pdf" + query,
    }
