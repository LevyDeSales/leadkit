"""Máquina de estados do job por UPDATE condicional (compare-and-set): seguro sob concorrência e
sob execuções duplicadas, porque a condição está no WHERE e não em Python."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import AuditLog, Delivery, Job, utcnow


class ClaimResult(StrEnum):
    PROCEED = "proceed"
    ALREADY_DONE = "already_done"
    SUPERSEDED = "superseded"
    MISSING = "missing"


def _update(
    session: Session, job_id: uuid.UUID, *conditions: sa.ColumnElement[bool], **values: object
) -> bool:
    statement = (
        sa.update(Job)
        .where(Job.id == job_id, *conditions)
        .values(updated_at=utcnow(), **values)
        .execution_options(synchronize_session=False)
    )
    return session.execute(statement).rowcount == 1


def claim(session: Session, job_id: uuid.UUID, attempt: int, run_id: str | None) -> ClaimResult:
    """`queued|running -> running` desta tentativa. Uma nova execução da mesma tentativa (retry da
    task, reentrega do Hatchet) também prossegue: os passos seguintes reaproveitam o que já existe."""
    if _update(
        session,
        job_id,
        Job.attempt == attempt,
        Job.status.in_(("queued", "running")),
        status="running",
        hatchet_run_id=sa.func.coalesce(Job.hatchet_run_id, run_id),
    ):
        return ClaimResult.PROCEED
    job = session.get(Job, job_id)
    if job is None:
        return ClaimResult.MISSING
    if job.attempt == attempt and job.status == "completed":
        return ClaimResult.ALREADY_DONE
    return ClaimResult.SUPERSEDED


def set_stage(session: Session, job_id: uuid.UUID, attempt: int, stage: str) -> bool:
    return _update(session, job_id, Job.attempt == attempt, Job.status == "running", stage=stage)


def set_run_id(session: Session, job_id: uuid.UUID, attempt: int, run_id: str) -> bool:
    return _update(session, job_id, Job.attempt == attempt, hatchet_run_id=run_id)


def complete(session: Session, job_id: uuid.UUID, attempt: int) -> bool:
    return _update(
        session,
        job_id,
        Job.attempt == attempt,
        Job.status == "running",
        status="completed",
        stage="completed",
        completed_at=utcnow(),
    )


def fail(session: Session, job_id: uuid.UUID, attempt: int, code: str, detail: str) -> bool:
    return _update(
        session,
        job_id,
        Job.attempt == attempt,
        Job.status.in_(("queued", "running")),
        status="failed",
        stage="failed",
        failure_code=code,
        failure_detail=detail[:500],
    )


def reprocess(
    session: Session,
    job_id: uuid.UUID,
    *,
    actor: str,
    reason: str,
    expected_attempt: int | None = None,
) -> int | None:
    """Nova tentativa do mesmo job (ação de admin). Planos e PDFs anteriores ficam no histórico.
    Devolve o número da nova tentativa ou None se o job não está concluído nem falho, ou se já
    não está na tentativa `expected_attempt` (duplo clique, outra pessoa reprocessou antes).
    A auditoria guarda como a tentativa anterior terminou, que o job deixa de guardar."""
    conditions = [Job.id == job_id, Job.status.in_(("completed", "failed"))]
    if expected_attempt is not None:
        conditions.append(Job.attempt == expected_attempt)
    previous = (
        sa.select(Job.id, Job.status, Job.failure_code)
        .where(*conditions)
        .with_for_update()
        .cte("previous")
    )
    statement = (
        sa.update(Job)
        .where(Job.id == previous.c.id)
        .values(
            status="queued",
            stage="queued",
            attempt=Job.attempt + 1,
            hatchet_run_id=None,
            failure_code=None,
            failure_detail=None,
            completed_at=None,
            updated_at=utcnow(),
        )
        .returning(Job.attempt, previous.c.status, previous.c.failure_code)
    )
    row = session.execute(statement).one_or_none()
    if row is None:
        return None
    new_attempt, previous_status, failure_code = row
    session.add(
        AuditLog(
            actor=actor,
            action="job.reprocess",
            target_type="job",
            target_id=str(job_id),
            details={
                "reason": reason[:300],
                "attempt": new_attempt,
                "previous_status": previous_status,
                "failure_code": failure_code,
            },
        )
    )
    return new_attempt


def resend_delivery(
    session: Session, job_id: uuid.UUID, delivery_id: uuid.UUID, *, actor: str
) -> bool:
    """Entrega falha volta para `pending` sem run id (ação de admin, auditada). Quem chama
    dispara o envio depois do commit; se o disparo falhar, `unlaunched_deliveries` a encontra."""
    statement = (
        sa.update(Delivery)
        .where(Delivery.id == delivery_id, Delivery.job_id == job_id, Delivery.status == "failed")
        .values(status="pending", hatchet_run_id=None)
        .execution_options(synchronize_session=False)
    )
    if session.execute(statement).rowcount != 1:
        return False
    session.add(
        AuditLog(
            actor=actor,
            action="delivery.resend",
            target_type="delivery",
            target_id=str(delivery_id),
            details={"job_id": str(job_id)},
        )
    )
    return True


def public_status(job: Job, now: datetime | None = None) -> str:
    """`expired` é derivado: o link venceu e o plano não ficou pronto."""
    if job.status != "completed" and (now or datetime.now(UTC)) > job.expires_at:
        return "expired"
    return job.status


# --- consultas do reconciliador ---------------------------------------------------------------


def unlaunched_jobs(session: Session, *, older_than: timedelta, limit: int) -> list[Job]:
    """Jobs gravados cujo disparo no Hatchet não aconteceu (falha entre o commit e o trigger)."""
    cutoff = utcnow() - older_than
    statement = (
        sa.select(Job)
        .where(Job.status == "queued", Job.hatchet_run_id.is_(None), Job.updated_at < cutoff)
        .order_by(Job.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.scalars(statement))


def in_flight_jobs(session: Session, *, older_than: timedelta, limit: int) -> list[Job]:
    """Jobs com execução registrada que não andam há algum tempo: o reconciliador pergunta ao
    Hatchet o que aconteceu com a execução."""
    cutoff = utcnow() - older_than
    statement = (
        sa.select(Job)
        .where(
            Job.status.in_(("queued", "running")),
            Job.hatchet_run_id.is_not(None),
            Job.updated_at < cutoff,
        )
        .order_by(Job.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.scalars(statement))


def unlaunched_deliveries(session: Session, *, older_than: timedelta, limit: int) -> list[Delivery]:
    cutoff = utcnow() - older_than
    statement = (
        sa.select(Delivery)
        .where(
            Delivery.status == "pending",
            Delivery.hatchet_run_id.is_(None),
            Delivery.created_at < cutoff,
        )
        .order_by(Delivery.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.scalars(statement))
