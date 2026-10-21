"""Gestão transacional da autoridade de fontes e credenciais de eventos."""

import hmac
import json
import re
import secrets
from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from pydantic import SecretStr

from app.deployment_contract import assert_contract
from app.handoff_types import HandoffError, confirmed, version_matches
from app.integration_credentials import digest
from app.integration_readiness import configuration_lock
from app.journey_types import CreatedEventCredential, EventCredentialInfo, EventSourceInfo
from app.models import AuditLog, EventCredential, EventSource, Form, HandoffReceiver

PATTERN = re.compile(r"^lke_([a-f0-9]{32})_([A-Za-z0-9_-]{43})$")


def audit(session, actor, action, row):
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            target_type=row.__tablename__,
            target_id=str(row.id),
            details={},
        )
    )


def fingerprint(command, actor, operation, target):
    return digest(
        json.dumps(
            command.model_dump(mode="json", exclude={"confirm"})
            | {"actor": actor, "operation": operation, "target": str(target)},
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def source_info(row):
    return EventSourceInfo(
        **{
            key: str(row.version) if key == "version" else getattr(row, key)
            for key in EventSourceInfo.model_fields
        }
    )


def credential_info(row, now):
    values = {
        key: getattr(row, key)
        for key in EventCredentialInfo.model_fields
        if key not in ("state", "version")
    }
    return EventCredentialInfo(
        **values,
        version=str(row.version),
        state="revoked"
        if row.revoked_at
        else "expired"
        if row.expires_at and row.expires_at <= now
        else "active",
    )


def source_row(session, source_id, *, lock=False):
    query = sa.select(EventSource).where(EventSource.id == source_id)
    row = session.scalar(
        query.with_for_update().execution_options(populate_existing=True) if lock else query
    )
    if row is None:
        raise HandoffError("not_found", status=404)
    return row


def list_sources(session, slug):
    if session.scalar(sa.select(Form.id).where(Form.slug == slug)) is None:
        raise HandoffError("not_found", status=404)
    return [
        source_info(row)
        for row in session.scalars(
            sa.select(EventSource).where(EventSource.form_slug == slug).order_by(EventSource.id)
        )
    ]


def create_source(session, slug, *, command, actor, now):
    confirmed(command)
    configuration_lock(session)
    key_hash = digest(str(command.idempotency_key))
    command_hash = fingerprint(command, actor, "create_source", slug)
    prior = session.scalar(
        sa.select(EventSource).where(
            EventSource.created_by == actor, EventSource.idempotency_hash == key_hash
        )
    )
    if prior:
        if prior.command_fingerprint != command_hash:
            raise HandoffError("idempotency_conflict")
        return source_info(prior)
    assert_contract(session, required=2)
    version_matches(0, command.expected_version)
    receiver = session.scalar(
        sa.select(HandoffReceiver)
        .where(HandoffReceiver.id == command.receiver_id, HandoffReceiver.form_slug == slug)
        .with_for_update()
    )
    if receiver is None:
        raise HandoffError("not_found", status=404)
    if not receiver.enabled or receiver.revision != command.receiver_revision:
        raise HandoffError("state_conflict")
    if session.scalar(
        sa.select(EventSource.id).where(
            EventSource.receiver_id == receiver.id,
            EventSource.receiver_revision == receiver.revision,
            EventSource.namespace == command.namespace,
        )
    ):
        raise HandoffError("namespace_conflict")
    row = EventSource(
        receiver_id=receiver.id,
        receiver_revision=receiver.revision,
        form_slug=slug,
        namespace=command.namespace,
        name=command.name.strip(),
        policy=command.policy.model_dump(),
        created_by=actor,
        updated_by=actor,
        created_at=now,
        updated_at=now,
        idempotency_hash=key_hash,
        command_fingerprint=command_hash,
    )
    session.add(row)
    session.flush()
    audit(session, actor, "event_source.create", row)
    return source_info(row)


def update_source(session, source_id, *, command, actor, now):
    confirmed(command)
    configuration_lock(session)
    assert_contract(session, required=2)
    row = source_row(session, source_id, lock=True)
    version_matches(row.version, command.expected_version)
    policy = command.policy.model_dump()
    if row.policy != policy:
        row.revision += 1
        row.enabled = False
    row.policy, row.name = policy, command.name.strip()
    row.version += 1
    row.updated_at, row.updated_by = now, actor
    audit(session, actor, "event_source.update", row)
    session.flush()
    return source_info(row)


def set_enabled(session, source_id, *, command, actor, now):
    confirmed(command)
    configuration_lock(session)
    if command.enabled:
        assert_contract(session, required=2)
    row = source_row(session, source_id, lock=True)
    version_matches(row.version, command.expected_version)
    active = session.scalar(
        sa.select(EventCredential.id).where(
            EventCredential.source_id == row.id,
            EventCredential.source_revision == row.revision,
            EventCredential.revoked_at.is_(None),
            sa.or_(EventCredential.expires_at.is_(None), EventCredential.expires_at > now),
        )
    )
    if command.enabled and (not row.policy["types"] or active is None):
        raise HandoffError("source_not_ready")
    row.enabled = command.enabled
    row.version += 1
    row.updated_at, row.updated_by = now, actor
    audit(session, actor, "event_source.enabled", row)
    session.flush()
    return source_info(row)


def list_credentials(session, source_id, *, now):
    source_row(session, source_id)
    return [
        credential_info(row, now)
        for row in session.scalars(
            sa.select(EventCredential)
            .where(EventCredential.source_id == source_id)
            .order_by(EventCredential.id)
        )
    ]


def issue_credential(session, source_id, *, command, actor, now, credential_id=None):
    confirmed(command)
    configuration_lock(session)
    operation = "rotate" if credential_id else "create"
    key_hash = digest(str(command.idempotency_key))
    command_hash = fingerprint(command, actor, operation, credential_id or source_id)
    prior = session.scalar(
        sa.select(EventCredential).where(
            EventCredential.created_by == actor,
            EventCredential.operation == operation,
            EventCredential.idempotency_hash == key_hash,
        )
    )
    if prior:
        if prior.command_fingerprint != command_hash:
            raise HandoffError("idempotency_conflict")
        raise HandoffError("credential_secret_already_issued", credential_id=prior.id)
    assert_contract(session, required=2)
    source = source_row(session, source_id, lock=True)
    previous = None
    if credential_id:
        previous = session.get(EventCredential, credential_id, with_for_update=True)
        if previous is None or previous.source_id != source.id:
            raise HandoffError("not_found", status=404)
        version_matches(previous.version, command.expected_version)
        if previous.revoked_at or previous.source_revision != source.revision:
            raise HandoffError("state_conflict")
        name = previous.name
    else:
        version_matches(source.version, command.expected_source_version)
        if source.revision != command.source_revision:
            raise HandoffError("source_revision_conflict")
        name = command.name.strip()
    if command.expires_at is not None and command.expires_at <= now:
        raise HandoffError("invalid_expiry", status=422, field="expires_at")
    raw, key_id = secrets.token_urlsafe(32), secrets.token_hex(16)
    row = EventCredential(
        source_id=source.id,
        source_revision=source.revision,
        name=name,
        key_id=key_id,
        secret_hash=digest(raw),
        created_by=actor,
        created_at=now,
        expires_at=command.expires_at,
        operation=operation,
        idempotency_hash=key_hash,
        command_fingerprint=command_hash,
    )
    session.add(row)
    session.flush()
    if previous:
        previous.revoked_at, previous.revoked_by = now, actor
        previous.version += 1
        previous.successor_id = row.id
    audit(session, actor, "event_credential." + operation, row)
    return CreatedEventCredential(
        credential=credential_info(row, now), secret=SecretStr(f"lke_{key_id}_{raw}")
    )


def rotate_credential(session, credential_id, **kwargs):
    source_id = session.scalar(
        sa.select(EventCredential.source_id).where(EventCredential.id == credential_id)
    )
    if source_id is None:
        raise HandoffError("not_found", status=404)
    return issue_credential(session, source_id, credential_id=credential_id, **kwargs)


def revoke_credential(session, credential_id, *, command, actor, now):
    confirmed(command)
    configuration_lock(session)
    source_id = session.scalar(
        sa.select(EventCredential.source_id).where(EventCredential.id == credential_id)
    )
    if source_id is None:
        raise HandoffError("not_found", status=404)
    source_row(session, source_id, lock=True)
    row = session.get(EventCredential, credential_id, with_for_update=True)
    version_matches(row.version, command.expected_version)
    if not row.revoked_at:
        row.revoked_at, row.revoked_by = now, actor
        row.version += 1
        audit(session, actor, "event_credential.revoke", row)
    session.flush()
    return credential_info(row, now)


@dataclass(frozen=True)
class EventPrincipal:
    credential_id: UUID
    source_id: UUID
    secret_hash: str


def validate_authority(source, credential, now):
    if (
        not source.enabled
        or credential.revoked_at is not None
        or (credential.expires_at is not None and now >= credential.expires_at)
        or source.revision != credential.source_revision
        or credential.scope != "journey:events:write"
    ):
        raise HandoffError("invalid_credentials", status=401)


def authenticate(session, secret, *, now):
    match = PATTERN.fullmatch(secret.get_secret_value())
    if match is None:
        raise HandoffError("invalid_credentials", status=401)
    credential = session.scalar(
        sa.select(EventCredential).where(EventCredential.key_id == match[1])
    )
    candidate = digest(match[2])
    if credential is None or not hmac.compare_digest(candidate, credential.secret_hash):
        raise HandoffError("invalid_credentials", status=401)
    source = source_row(session, credential.source_id)
    validate_authority(source, credential, now)
    return EventPrincipal(credential.id, source.id, candidate)
