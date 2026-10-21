"""Credenciais de serviço independentes dos papéis e chaves administrativas."""

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.deployment_contract import assert_contract
from app.handoff_receivers import receiver_row
from app.handoff_types import (
    CreatedIntegrationCredential,
    HandoffError,
    IntegrationCredentialInfo,
    confirmed,
    version_matches,
)
from app.integration_readiness import configuration_lock
from app.models import AuditLog, HandoffReceiver, IntegrationCredential

CREDENTIAL_PATTERN = re.compile(r"^lki_([a-f0-9]{32})_([A-Za-z0-9_-]{43})$")


@dataclass(frozen=True)
class IntegrationCredentialPrincipal:
    credential_id: UUID
    receiver_id: UUID
    receiver_revision: int
    form_slug: str


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def credential_info(row: IntegrationCredential, now: datetime) -> IntegrationCredentialInfo:
    values = {
        key: getattr(row, key)
        for key in IntegrationCredentialInfo.model_fields
        if key not in ("version", "state")
    }
    return IntegrationCredentialInfo(
        **values,
        version=str(row.version),
        state="revoked"
        if row.revoked_at
        else "expired"
        if row.expires_at is not None and now >= row.expires_at
        else "active",
    )


def _command(session, command, actor, operation, target):
    key_hash = digest(str(command.idempotency_key))
    content = command.model_dump(mode="json", exclude={"confirm"}) | {
        "actor": actor,
        "operation": operation,
        "target": str(target),
    }
    fingerprint = digest(json.dumps(content, sort_keys=True, separators=(",", ":")))
    prior = session.scalar(
        sa.select(IntegrationCredential).where(
            IntegrationCredential.created_by == actor,
            IntegrationCredential.operation == operation,
            IntegrationCredential.idempotency_hash == key_hash,
        )
    )
    if prior:
        if prior.command_fingerprint != fingerprint:
            raise HandoffError("idempotency_conflict")
        raise HandoffError("credential_secret_already_issued", credential_id=prior.id)
    return key_hash, fingerprint


def _expiry(value, now):
    if value is not None and value <= now:
        raise HandoffError("invalid_expiry", status=422, field="expires_at")


def _issue(session, receiver, *, command, actor, operation, name, now, idem, fingerprint):
    _expiry(command.expires_at, now)
    raw = secrets.token_urlsafe(32)
    key_id = secrets.token_hex(16)
    row = IntegrationCredential(
        key_id=key_id,
        name=name,
        secret_hash=digest(raw),
        receiver_id=receiver.id,
        receiver_revision=receiver.revision,
        form_slug=receiver.form_slug,
        created_by=actor,
        created_at=now,
        expires_at=command.expires_at,
        operation=operation,
        idempotency_hash=idem,
        command_fingerprint=fingerprint,
    )
    session.add(row)
    session.flush()
    session.add(
        AuditLog(
            actor=actor,
            action=f"integration_credential.{operation}",
            target_type="integration_credential",
            target_id=str(row.id),
            details={"receiver_id": str(receiver.id), "revision": receiver.revision},
        )
    )
    return row, CreatedIntegrationCredential(
        credential=credential_info(row, now), secret=SecretStr(f"lki_{key_id}_{raw}")
    )


def create_credential(session: Session, slug: str, *, command, actor: str, now: datetime):
    confirmed(command)
    configuration_lock(session)
    idem, fingerprint = _command(session, command, actor, "create", slug)
    assert_contract(session, required=2)
    receiver = receiver_row(session, slug, lock=True)
    version_matches(receiver.version, command.expected_receiver_version)
    if receiver.revision != command.receiver_revision:
        raise HandoffError("state_conflict")
    return _issue(
        session,
        receiver,
        command=command,
        actor=actor,
        operation="create",
        name=command.name.strip(),
        now=now,
        idem=idem,
        fingerprint=fingerprint,
    )[1]


def _credential_context(session, credential_id):
    receiver_id = session.scalar(
        sa.select(IntegrationCredential.receiver_id).where(
            IntegrationCredential.id == credential_id
        )
    )
    if receiver_id is None:
        raise HandoffError("not_found", status=404)
    receiver = session.scalar(
        sa.select(HandoffReceiver).where(HandoffReceiver.id == receiver_id).with_for_update()
    )
    row = session.scalar(
        sa.select(IntegrationCredential)
        .where(IntegrationCredential.id == credential_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or receiver is None:
        raise HandoffError("not_found", status=404)
    return receiver, row


def rotate_credential(session: Session, credential_id: UUID, *, command, actor: str, now: datetime):
    confirmed(command)
    configuration_lock(session)
    idem, fingerprint = _command(session, command, actor, "rotate", credential_id)
    assert_contract(session, required=2)
    receiver, old = _credential_context(session, credential_id)
    version_matches(old.version, command.expected_version)
    if old.revoked_at or receiver.revision != old.receiver_revision:
        raise HandoffError("state_conflict")
    row, created = _issue(
        session,
        receiver,
        command=command,
        actor=actor,
        operation="rotate",
        name=old.name,
        now=now,
        idem=idem,
        fingerprint=fingerprint,
    )
    old.revoked_at, old.revoked_by, old.successor_id = now, actor, row.id
    old.version += 1
    session.flush()
    return created


def revoke_credential(session: Session, credential_id: UUID, *, command, actor: str, now: datetime):
    confirmed(command)
    configuration_lock(session)
    _, row = _credential_context(session, credential_id)
    if row.revoked_at is None:
        version_matches(row.version, command.expected_version)
        row.revoked_at, row.revoked_by = now, actor
        row.version += 1
        session.add(
            AuditLog(
                actor=actor,
                action="integration_credential.revoke",
                target_type="integration_credential",
                target_id=str(row.id),
                details={},
            )
        )
        session.flush()
    return credential_info(row, now)


def list_credentials(session: Session, slug: str, *, now: datetime):
    receiver = receiver_row(session, slug)
    return [
        credential_info(row, now)
        for row in session.scalars(
            sa.select(IntegrationCredential)
            .where(IntegrationCredential.receiver_id == receiver.id)
            .order_by(IntegrationCredential.created_at.desc(), IntegrationCredential.id.desc())
        )
    ]


def validate_credential(row, receiver, *, now):
    if (
        row is None
        or receiver is None
        or row.revoked_at is not None
        or (row.expires_at is not None and now >= row.expires_at)
        or not receiver.enabled
        or row.receiver_id != receiver.id
        or row.receiver_revision != receiver.revision
        or row.form_slug != receiver.form_slug
        or row.scope != "handoffs:resolve"
        or row.purpose != "support"
        or row.projection != "correlation.v1"
    ):
        raise HandoffError("invalid_credentials", status=401)


def authenticate_credential(
    session: Session, value: SecretStr, *, now: datetime
) -> IntegrationCredentialPrincipal:
    raw = value.get_secret_value()
    match = CREDENTIAL_PATTERN.fullmatch(raw)
    if not match:
        raise HandoffError("invalid_credentials", status=401)
    configuration_lock(session, shared=True)
    assert_contract(session, required=2)
    row = session.scalar(
        sa.select(IntegrationCredential).where(IntegrationCredential.key_id == match[1])
    )
    if row is None or not hmac.compare_digest(row.secret_hash, digest(match[2])):
        raise HandoffError("invalid_credentials", status=401)
    receiver = session.get(HandoffReceiver, row.receiver_id)
    validate_credential(row, receiver, now=now)
    return IntegrationCredentialPrincipal(
        row.id, row.receiver_id, row.receiver_revision, row.form_slug
    )


def touch_credential(row, now):
    if row.last_used_at is None or now - row.last_used_at >= timedelta(minutes=1):
        row.last_used_at = now
