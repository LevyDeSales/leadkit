"""Gestão transacional de receptores; salvar configura, habilitar autoriza uso futuro."""

from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.deployment_contract import assert_contract
from app.handoff_types import HandoffError, HandoffReceiverInfo, confirmed, version_matches
from app.integration_readiness import configuration_lock
from app.models import AuditLog, Form, HandoffReceiver, IntegrationCredential


def normalize_entry_url(value: str) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and "?" not in value
            and "#" not in value
            and not any(ord(c) <= 32 or ord(c) == 127 for c in value)
            and len(value) <= 2000
        )
        port = parsed.port
        if not valid:
            raise ValueError
        host = parsed.hostname.encode("idna").decode().lower()
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{port}" if port and port != 443 else "")
        return urlunsplit(("https", netloc, parsed.path or "/", "", ""))
    except (ValueError, UnicodeError):
        raise HandoffError("invalid_entry_url", status=422, field="entry_url") from None


def receiver_row(session: Session, slug: str, *, lock: bool = False) -> HandoffReceiver:
    query = (
        sa.select(HandoffReceiver)
        .where(HandoffReceiver.form_slug == slug)
        .execution_options(populate_existing=True)
    )
    row = session.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HandoffError("not_found", status=404)
    return row


def receiver_info(row: HandoffReceiver) -> HandoffReceiverInfo:
    return HandoffReceiverInfo(
        **{key: getattr(row, key) for key in HandoffReceiverInfo.model_fields if key != "version"},
        version=str(row.version),
    )


def get_receiver(session: Session, slug: str) -> HandoffReceiverInfo:
    return receiver_info(receiver_row(session, slug))


def save_receiver(
    session: Session, slug: str, *, command, actor: str, now: datetime
) -> HandoffReceiverInfo:
    confirmed(command)
    address = normalize_entry_url(command.entry_url)
    configuration_lock(session)
    assert_contract(session, required=2)
    if session.scalar(sa.select(Form).where(Form.slug == slug).with_for_update()) is None:
        raise HandoffError("not_found", status=404)
    row = session.scalar(
        sa.select(HandoffReceiver).where(HandoffReceiver.form_slug == slug).with_for_update()
    )
    version_matches(row.version if row else 0, command.expected_version)
    if row is None:
        row = HandoffReceiver(
            form_slug=slug,
            name=command.name.strip(),
            entry_url=address,
            reference_ttl_seconds=command.reference_ttl_seconds,
            created_by=actor,
            updated_by=actor,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        if (row.entry_url, row.reference_ttl_seconds) != (address, command.reference_ttl_seconds):
            row.revision += 1
        row.name, row.entry_url, row.reference_ttl_seconds = (
            command.name.strip(),
            address,
            command.reference_ttl_seconds,
        )
        row.enabled = False
        row.version += 1
        row.updated_at, row.updated_by = now, actor
    session.flush()
    session.add(
        AuditLog(
            actor=actor,
            action="handoff_receiver.save",
            target_type="handoff_receiver",
            target_id=str(row.id),
            details={"revision": row.revision},
        )
    )
    return receiver_info(row)


def valid_credentials(session: Session, row: HandoffReceiver, now: datetime):
    return sa.select(IntegrationCredential).where(
        IntegrationCredential.receiver_id == row.id,
        IntegrationCredential.receiver_revision == row.revision,
        IntegrationCredential.form_slug == row.form_slug,
        IntegrationCredential.revoked_at.is_(None),
        sa.or_(IntegrationCredential.expires_at.is_(None), IntegrationCredential.expires_at > now),
    )


def set_receiver_enabled(
    session: Session, slug: str, *, command, actor: str, cipher, now: datetime
) -> HandoffReceiverInfo:
    confirmed(command)
    configuration_lock(session)
    assert_contract(session, required=2)
    row = receiver_row(session, slug, lock=True)
    version_matches(row.version, command.expected_version)
    if command.enabled and (
        cipher is None or session.scalar(valid_credentials(session, row, now)) is None
    ):
        raise HandoffError("receiver_not_ready")
    row.enabled = command.enabled
    row.version += 1
    row.updated_at, row.updated_by = now, actor
    session.add(
        AuditLog(
            actor=actor,
            action="handoff_receiver.enabled",
            target_type="handoff_receiver",
            target_id=str(row.id),
            details={"enabled": row.enabled, "revision": row.revision},
        )
    )
    session.flush()
    return receiver_info(row)
