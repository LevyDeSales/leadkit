"""Intenção e resolução de encaminhamento, sem transporte externo ou acesso ao PDF."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.deployment_contract import DeploymentContractError, assert_contract
from app.destinations import SecretCipher, UnreadableSecret
from app.handoff_receivers import valid_credentials
from app.handoff_types import HandoffError, HandoffInfo, ResolvedHandoff, confirmed, version_matches
from app.integration_credentials import digest, touch_credential, validate_credential
from app.integration_readiness import configuration_lock
from app.models import (
    AuditLog,
    Form,
    FormVersion,
    HandoffReceiver,
    HandoffReference,
    IntegrationCredential,
    Submission,
)


@dataclass(frozen=True)
class CompletionView:
    mode: str
    message: str
    button_label: str
    url: str | None = field(repr=False)
    handoff_status: str = "not_configured"


def _base(spec, *, status="not_configured", url=None, unavailable=False, ended=False):
    return CompletionView(
        spec.completion.mode,
        spec.completion.message
        + (
            (
                "\nEste encaminhamento não está mais disponível. Suas respostas continuam salvas."
                if ended
                else "\nO próximo passo está temporariamente indisponível. Suas respostas continuam salvas."
            )
            if unavailable
            else ""
        ),
        spec.completion.button_label,
        url,
        status,
    )


def prepare_handoff(
    session: Session,
    submission: Submission,
    spec,
    *,
    receipt_expires_at: datetime,
    cipher: SecretCipher | None,
    now: datetime,
) -> HandoffReference | None:
    """Somente na criação, dentro da transação de aceite; nunca em GET ou replay."""
    import secrets

    binding = spec.completion.handoff
    if binding is None:
        return None
    configuration_lock(session, shared=True)
    receiver = session.scalar(
        sa.select(HandoffReceiver)
        .where(HandoffReceiver.id == binding.receiver_id)
        .with_for_update()
    )
    # Não há operação de exclusão de receptor. Ausência física não pode perder respostas válidas.
    if receiver is None:
        return None
    code = None
    try:
        assert_contract(session, required=2)
    except DeploymentContractError:
        code = "contract_unavailable"
    if code is None:
        if not receiver.enabled:
            code = "receiver_paused"
        elif (
            receiver.form_slug != submission.form_slug
            or receiver.revision != binding.receiver_revision
            or receiver.entry_url != spec.completion.url
        ):
            code = "receiver_changed"
        elif cipher is None:
            code = "cipher_unavailable"
        elif session.scalar(valid_credentials(session, receiver, now)) is None:
            code = "credential_unavailable"
    expires_at = min(receipt_expires_at, now + timedelta(seconds=receiver.reference_ttl_seconds))
    raw = secrets.token_urlsafe(32) if code is None else None
    token = f"lkh_{raw}" if raw else None
    row = HandoffReference(
        submission_id=submission.id,
        receiver_id=receiver.id,
        receiver_revision=binding.receiver_revision,
        purpose="support",
        projection="correlation.v1",
        entry_url=spec.completion.url,
        created_at=now,
        expires_at=expires_at,
        issuance_state="unavailable" if code else "ready",
        unavailable_code=code,
        token_hash=digest(token) if token else None,
        token_ciphertext=cipher.seal(token) if token and cipher else None,
    )
    session.add(row)
    session.flush()
    return row


def _locked_reference(session, submission_id):
    configuration_lock(session, shared=True)
    submission = session.scalar(
        sa.select(Submission)
        .where(Submission.id == submission_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if submission is None:
        raise HandoffError("handoff_unavailable", status=404)
    context = session.execute(
        sa.select(HandoffReference.id, HandoffReference.receiver_id).where(
            HandoffReference.submission_id == submission_id
        )
    ).first()
    if context is None:
        return submission, None, None
    receiver = session.scalar(
        sa.select(HandoffReceiver)
        .where(HandoffReceiver.id == context.receiver_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return submission, receiver, context.id


def _reference(session, reference_id):
    return session.scalar(
        sa.select(HandoffReference)
        .where(HandoffReference.id == reference_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _usable(row, receiver, now):
    return (
        row is not None
        and receiver is not None
        and row.issuance_state == "ready"
        and row.revoked_at is None
        and now < row.expires_at
        and receiver.enabled
        and row.receiver_id == receiver.id
        and row.receiver_revision == receiver.revision
        and row.purpose == receiver.purpose == "support"
        and row.projection == receiver.projection == "correlation.v1"
        and row.entry_url == receiver.entry_url
    )


def completion_view(
    session: Session, submission: Submission, spec, *, cipher: SecretCipher | None, now: datetime
) -> CompletionView:
    if spec.completion.handoff is None:
        return _base(spec, url=spec.completion.url)
    unavailable = _base(spec, status="unavailable", unavailable=True)
    try:
        assert_contract(session, required=2)
    except DeploymentContractError:
        return unavailable
    _, receiver, reference_id = _locked_reference(session, submission.id)
    if reference_id is None:
        return unavailable
    credential = (
        session.scalar(valid_credentials(session, receiver, now).with_for_update())
        if receiver
        else None
    )
    row = _reference(session, reference_id)
    if row is not None and (row.revoked_at is not None or now >= row.expires_at):
        return _base(spec, status="unavailable", unavailable=True, ended=True)
    if (
        not _usable(row, receiver, now)
        or credential is None
        or not row.token_ciphertext
        or cipher is None
    ):
        return unavailable
    try:
        token = cipher.open(row.token_ciphertext)
    except UnreadableSecret:
        return unavailable
    if not re.fullmatch(r"lkh_[A-Za-z0-9_-]{43}", token) or digest(token) != row.token_hash:
        return unavailable
    return _base(spec, status="ready", url=f"{row.entry_url}?leadkit_ref={token}")


def resolve_handoff(
    session: Session, credential, reference: SecretStr, *, now: datetime
) -> ResolvedHandoff:
    """Autenticação já ocorreu; revalida autorização dentro da transação do DTO."""
    assert_contract(session, required=2)
    raw = reference.get_secret_value()
    if not re.fullmatch(r"lkh_[A-Za-z0-9_-]{43}", raw):
        raise HandoffError("invalid_reference", status=422, field="reference")
    submission_id = session.scalar(
        sa.select(HandoffReference.submission_id).where(
            HandoffReference.token_hash == digest(raw),
            HandoffReference.receiver_id == credential.receiver_id,
        )
    )
    if submission_id is None:
        raise HandoffError("handoff_unavailable", status=404)
    submission, receiver, reference_id = _locked_reference(session, submission_id)
    key = session.scalar(
        sa.select(IntegrationCredential)
        .where(IntegrationCredential.id == credential.credential_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    validate_credential(key, receiver, now=now)
    row = _reference(session, reference_id)
    if (
        not _usable(row, receiver, now)
        or key.receiver_revision != row.receiver_revision
        or key.form_slug != submission.form_slug
        or credential.receiver_revision != row.receiver_revision
    ):
        raise HandoffError("handoff_unavailable", status=404)
    document = session.scalar(
        sa.select(FormVersion.definition)
        .join(Form, FormVersion.form_id == Form.id)
        .where(
            Form.slug == submission.form_slug, FormVersion.number == submission.form_version_number
        )
    )
    if document is None:
        raise HandoffError("handoff_unavailable", status=404)
    touch_credential(key, now)
    session.add(
        AuditLog(
            actor=f"servico:{key.id}",
            action="handoff.resolved",
            target_type="handoff_reference",
            target_id=str(row.id),
            details={"credential_id": str(key.id), "projection": "correlation.v1"},
        )
    )
    return ResolvedHandoff(
        reference_id=row.id,
        submission_id=submission.id,
        form_slug=submission.form_slug,
        form_version_number=submission.form_version_number,
        generation=document.get("generation", "pdf"),
        receiver_id=receiver.id,
        receiver_revision=row.receiver_revision,
        submitted_at=submission.created_at,
        expires_at=row.expires_at,
    )


def handoff_info(row) -> HandoffInfo:
    return HandoffInfo(
        **{key: getattr(row, key) for key in HandoffInfo.model_fields if key != "version"},
        version=str(row.version),
    )


def get_handoff(session, submission_id):
    row = session.scalar(
        sa.select(HandoffReference).where(HandoffReference.submission_id == submission_id)
    )
    if row is None:
        raise HandoffError("not_found", status=404)
    return handoff_info(row)


def revoke_handoff(session, submission_id, *, command, actor: str, now: datetime):
    confirmed(command)
    _, _, reference_id = _locked_reference(session, submission_id)
    row = _reference(session, reference_id) if reference_id else None
    if row is None:
        raise HandoffError("not_found", status=404)
    if row.revoked_at is None:
        version_matches(row.version, command.expected_version)
        row.revoked_at, row.revoked_by, row.token_ciphertext = now, actor, None
        row.version += 1
        session.add(
            AuditLog(
                actor=actor,
                action="handoff.revoke",
                target_type="handoff_reference",
                target_id=str(row.id),
                details={},
            )
        )
        session.flush()
    return handoff_info(row)


def expire_ciphertexts(session, *, now: datetime) -> int:
    """Limpa somente cifra vencida/revogada, sem renovar o prazo dos dados pessoais."""
    return session.execute(
        sa.update(HandoffReference)
        .where(
            HandoffReference.token_ciphertext.is_not(None),
            sa.or_(HandoffReference.expires_at <= now, HandoffReference.revoked_at.is_not(None)),
        )
        .values(token_ciphertext=None)
    ).rowcount
