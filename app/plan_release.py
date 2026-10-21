"""Autorização da participante por tentativa, separada da geração e do transporte."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import destinations, jobs
from app.deployment_contract import assert_contract
from app.forms.definition import DefinitionError, compile_definition
from app.integration_readiness import configuration_lock
from app.models import (
    Artifact,
    AuditLog,
    Delivery,
    Form,
    FormVersion,
    Job,
    Plan,
    PlanRelease,
    Submission,
    WebhookDestination,
    utcnow,
)
from app.pdf_availability import artifact_availability


class ReleaseRefused(Exception):
    """Recusa segura; não incorpora respostas, documento ou erro de fornecedor."""

    def __init__(self, code: str = "state_conflict", *, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(code)


class PlanHeld(ReleaseRefused):
    def __init__(self):
        super().__init__("plan_held", status=403)


@dataclass(frozen=True)
class ParticipantAccess:
    policy: Literal["automatic", "manual"]
    state: Literal["automatic", "held", "released"]
    release_version: int = 0
    release_id: UUID | None = None
    released_at: datetime | None = None

    @property
    def allowed(self) -> bool:
        return self.state in {"automatic", "released"}


class ReleasePdfCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_attempt: int = Field(ge=1)
    expected_artifact_id: UUID
    expected_form_version_number: int = Field(ge=1)
    expected_release_version: int = Field(ge=0)
    idempotency_key: UUID
    mode: Literal["standard", "alternative"]
    reason_code: Literal["operator_review", "participant_request", "alternative_channel", "other"]
    confirm: Literal[True]


@dataclass(frozen=True)
class ReleaseCommit:
    release_id: UUID
    attempt: int
    artifact_id: UUID
    release_version: int
    occurred_at: datetime
    mode: str
    reason_code: str
    notification_decision: str
    delivery_ids: tuple[UUID, ...]
    replay: bool
    current: bool


def historical_policy(session: Session, job: Job):
    """Resolve só a definição publicada respondida, sem fallback para a versão vigente."""
    submission = session.get(Submission, job.submission_id)
    if submission is None:
        raise ReleaseRefused("release_policy_unavailable")
    definition = session.scalar(
        sa.select(FormVersion.definition)
        .join(Form, Form.id == FormVersion.form_id)
        .where(
            Form.slug == submission.form_slug, FormVersion.number == submission.form_version_number
        )
    )
    if definition is None:
        raise ReleaseRefused("release_policy_unavailable")
    try:
        spec = compile_definition(definition, version_number=submission.form_version_number)
    except DefinitionError:
        raise ReleaseRefused("release_policy_unavailable") from None
    if spec.version != submission.form_version or spec.slug != submission.form_slug:
        raise ReleaseRefused("release_policy_unavailable")
    return submission, spec


def participant_access(session: Session, job: Job, artifact: Artifact | None) -> ParticipantAccess:
    """A política histórica não é prova de disponibilidade nem renova a retenção."""
    submission, spec = historical_policy(session, job)
    if spec.generation != "pdf":
        raise ReleaseRefused("generation_unavailable", status=422)
    if spec.release_mode == "automatic":
        return ParticipantAccess("automatic", "automatic")
    release = session.scalar(
        sa.select(PlanRelease).where(
            PlanRelease.job_id == job.id, PlanRelease.attempt == job.attempt
        )
    )
    if (
        artifact is not None
        and release is not None
        and (
            release.artifact_id == artifact.id
            and artifact.job_id == job.id
            and artifact.version == job.attempt
            and release.form_slug == submission.form_slug
            and release.form_version_number == submission.form_version_number
            and release.form_version == submission.form_version
        )
    ):
        return ParticipantAccess(
            "manual", "released", release.version, release.id, release.occurred_at
        )
    return ParticipantAccess("manual", "held")


def require_participant_access(
    session: Session, job: Job, artifact: Artifact | None
) -> ParticipantAccess:
    access = participant_access(session, job, artifact)
    if access.policy == "manual":
        assert_contract(session, required=2)
    if not access.allowed:
        raise PlanHeld
    return access


def _result(session: Session, job: Job, release: PlanRelease, *, replay: bool) -> ReleaseCommit:
    ids = tuple(session.scalars(sa.select(Delivery.id).where(Delivery.release_id == release.id)))
    return ReleaseCommit(
        release.id,
        release.attempt,
        release.artifact_id,
        release.version,
        release.occurred_at,
        release.mode,
        release.reason_code,
        release.notification_decision,
        ids,
        replay,
        job.attempt == release.attempt
        and session.scalar(
            sa.select(Artifact.id).where(
                Artifact.job_id == job.id, Artifact.kind == "pdf", Artifact.version == job.attempt
            )
        )
        == release.artifact_id,
    )


def release_pdf(
    session: Session, deps, public_id: str, *, actor: str, command: ReleasePdfCommand
) -> ReleaseCommit:
    """Persiste fato/intenção juntos; replay aceito precede piso, tentativa e storage atuais."""
    if not command.confirm:
        raise ReleaseRefused("confirmation_required", status=422)
    configuration_lock(session, shared=True)
    job = session.scalar(sa.select(Job).where(Job.public_id == public_id).with_for_update())
    if job is None:
        raise ReleaseRefused("not_found", status=404)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "actor": actor,
                "job_id": str(job.id),
                "submission_id": str(job.submission_id),
                "command": command.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    previous = session.scalar(
        sa.select(PlanRelease).where(
            PlanRelease.job_id == job.id, PlanRelease.idempotency_key == command.idempotency_key
        )
    )
    if previous is not None:
        if previous.command_fingerprint != fingerprint:
            raise ReleaseRefused("idempotency_conflict")
        return _result(session, job, previous, replay=True)
    assert_contract(session, required=2)
    submission, spec = historical_policy(session, job)
    if spec.generation != "pdf":
        raise ReleaseRefused("generation_unavailable", status=422)
    if spec.release_mode != "manual":
        raise ReleaseRefused("release_not_required")
    artifact = session.scalar(
        sa.select(Artifact).where(Artifact.id == command.expected_artifact_id).with_for_update()
    )
    plan = session.get(Plan, artifact.plan_id) if artifact is not None else None
    if (
        job.status != "completed"
        or job.attempt != command.expected_attempt
        or submission.form_version_number != command.expected_form_version_number
        or command.expected_release_version != 0
        or artifact is None
        or artifact.job_id != job.id
        or artifact.version != job.attempt
        or artifact.kind != "pdf"
        or plan is None
        or plan.job_id != job.id
        or plan.version != job.attempt
        or job.expires_at <= utcnow()
        or jobs.delivery_retention_expired(session, job, deps.settings.retention_days)
    ):
        raise ReleaseRefused()
    if participant_access(session, job, artifact).state != "held":
        raise ReleaseRefused()
    if not artifact_availability(deps.storage, artifact).available:
        raise ReleaseRefused("pdf_unavailable")
    receiver = session.scalar(
        sa.select(WebhookDestination)
        .where(destinations._serving(submission.form_slug))
        .order_by(WebhookDestination.form_slug.is_(None))
        .limit(1)
    )
    if receiver is None or receiver.purpose != "participant":
        decision = "no_participant_channel"
    elif "plano.liberado" not in receiver.events:
        decision = "event_not_selected"
    else:
        decision = "outbox"
    release = PlanRelease(
        job_id=job.id,
        artifact_id=artifact.id,
        attempt=job.attempt,
        form_slug=submission.form_slug,
        form_version_number=submission.form_version_number,
        form_version=submission.form_version,
        actor=actor,
        mode=command.mode,
        reason_code=command.reason_code,
        idempotency_key=command.idempotency_key,
        command_fingerprint=fingerprint,
        notification_decision=decision,
    )
    session.add(release)
    session.flush()
    if decision == "outbox":
        session.add(
            Delivery(
                job_id=job.id,
                artifact_id=artifact.id,
                webhook_destination_id=receiver.id,
                config_version=receiver.version,
                audience="participant",
                authorization_origin="explicit",
                authorized_attempt=job.attempt,
                destination="webhook.release",
                event="plano.liberado",
                release_id=release.id,
                occurred_at=release.occurred_at,
            )
        )
    session.add(
        AuditLog(
            actor=actor,
            action="job.pdf_release",
            target_type="job",
            target_id=str(job.id),
            details={
                "release_id": str(release.id),
                "artifact_id": str(artifact.id),
                "attempt": job.attempt,
                "version": release.version,
                "occurred_at": release.occurred_at.isoformat(),
                "mode": release.mode,
                "reason_code": release.reason_code,
                "notification_decision": decision,
            },
        )
    )
    session.flush()
    return _result(session, job, release, replay=False)


def delivery_eligible(session: Session, delivery: Delivery, *, job: Job | None = None) -> bool:
    """Predicado comum: retenção manual não é falha nem trabalho para a fila."""
    from app.deployment_contract import DeploymentContractError

    if delivery.audience != "participant":
        return delivery.release_id is None and delivery.event != "plano.liberado"
    job = job or session.get(Job, delivery.job_id)
    artifact = session.get(Artifact, delivery.artifact_id)
    if (
        job is None
        or artifact is None
        or job.attempt != artifact.version
        or job.expires_at <= utcnow()
    ):
        return False
    try:
        access = require_participant_access(session, job, artifact)
        if not access.allowed:
            return False
        if delivery.event == "plano.liberado":
            assert_contract(session, required=2)
            release = session.get(PlanRelease, delivery.release_id) if delivery.release_id else None
            return (
                release is not None
                and release.job_id == job.id
                and release.artifact_id == artifact.id
                and release.attempt == job.attempt
                and delivery.destination == "webhook.release"
                and delivery.authorized_attempt == job.attempt
            )
        return delivery.destination != "webhook.release" and delivery.release_id is None
    except (ReleaseRefused, DeploymentContractError):
        return False


def release_snapshot(session: Session, job: Job) -> dict:
    """Leitura segura do acesso, suficiente para observar a versão do próximo comando."""
    from dataclasses import asdict

    artifact = session.scalar(
        sa.select(Artifact).where(
            Artifact.job_id == job.id, Artifact.version == job.attempt, Artifact.kind == "pdf"
        )
    )
    try:
        access = asdict(participant_access(session, job, artifact))
    except ReleaseRefused:
        access = {
            "policy": "unavailable",
            "state": "unavailable",
            "release_version": 0,
            "release_id": None,
            "released_at": None,
        }
    submission = session.get(Submission, job.submission_id)
    return {
        **access,
        "form_version_number": submission.form_version_number if submission else None,
        "attempt": job.attempt,
        "artifact_id": artifact.id if artifact else None,
    }
