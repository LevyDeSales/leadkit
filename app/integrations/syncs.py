"""O estado de cada sincronização com uma integração (`integration_syncs`), o par da entrega do
webhook.

Um pedido concluído de um formulário com a integração ativa ganha uma linha `pending` por
integração, na mesma transação que o conclui (`app.pipeline.finalize`): é a "outbox" da
sincronização. O Hatchet a leva a `succeeded` ou `failed` (`app.integrations.push`), e o
reconciliador (`app.crons`) dispara as que ficarem para trás. A linha não guarda dado da lead, só o
estado da sincronização, o motivo da última falha (código, etapa e status) e o id da linha na
planilha.

Uma linha por (integração, PDF): reprocessar um pedido gera um PDF novo e uma sincronização nova,
que atualiza a mesma linha da planilha, achada pelo código do pedido. Ativar uma integração não
sincroniza os pedidos que já estavam prontos: só reprocessar o pedido o faz.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.destinations import host_of
from app.integrations import config
from app.models import Artifact, AuditLog, Integration, IntegrationSync, Job, utcnow


def enqueue(session: Session, form_slug: str, job_id: uuid.UUID, artifact_id: uuid.UUID) -> None:
    """Cria a sincronização de um pedido concluído com cada integração ativa do formulário, na
    transação de quem chama. Idempotente: o mesmo PDF não gera uma segunda sincronização."""
    for integration_id in config.active_ids(session, form_slug):
        session.execute(
            insert(IntegrationSync)
            .values(integration_id=integration_id, job_id=job_id, artifact_id=artifact_id)
            .on_conflict_do_nothing(constraint="uq_integration_syncs_artifact")
        )


def pending_for_artifact(session: Session, artifact_id: uuid.UUID) -> list[uuid.UUID]:
    """As sincronizações deste PDF que ainda não foram disparadas: o que a task de concluir o pedido
    dispara depois do commit."""
    return list(
        session.scalars(
            sa.select(IntegrationSync.id).where(
                IntegrationSync.artifact_id == artifact_id,
                IntegrationSync.status == "pending",
                IntegrationSync.hatchet_run_id.is_(None),
            )
        )
    )


def set_run_id(session: Session, sync_id: uuid.UUID, run_id: str) -> None:
    session.execute(
        sa.update(IntegrationSync)
        .where(IntegrationSync.id == sync_id)
        .values(hatchet_run_id=run_id)
    )


def unlaunched(session: Session, *, older_than: timedelta, limit: int) -> list[IntegrationSync]:
    """Sincronizações gravadas cujo disparo no Hatchet não aconteceu (falha entre o commit e o
    trigger)."""
    cutoff = utcnow() - older_than
    statement = (
        sa.select(IntegrationSync)
        .where(
            IntegrationSync.status == "pending",
            IntegrationSync.hatchet_run_id.is_(None),
            IntegrationSync.created_at < cutoff,
        )
        .order_by(IntegrationSync.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.scalars(statement))


def mark_failed(session: Session, sync_id: uuid.UUID, code: str) -> None:
    """A sincronização esgotou as tentativas. O motivo que a última tentativa registrou
    ("[código] detalhe") fica para o admin; sem ele, fica o código."""
    session.execute(
        sa.update(IntegrationSync)
        .where(IntegrationSync.id == sync_id, IntegrationSync.status == "pending")
        .values(status="failed", last_error=sa.func.coalesce(IntegrationSync.last_error, code))
    )


def resend(session: Session, job_id: uuid.UUID, sync_id: uuid.UUID, *, actor: str) -> bool:
    """Uma sincronização que falhou volta para `pending`, sem run id (ação de admin, auditada). Quem
    chama a dispara depois do commit; se o disparo falhar, `unlaunched` a encontra."""
    result = session.execute(
        sa.update(IntegrationSync)
        .where(
            IntegrationSync.id == sync_id,
            IntegrationSync.job_id == job_id,
            IntegrationSync.status == "failed",
        )
        .values(status="pending", hatchet_run_id=None)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    session.add(
        AuditLog(
            actor=actor,
            action="sync.resend",
            target_type="integration_sync",
            target_id=str(sync_id),
            details={"job_id": str(job_id)},
        )
    )
    return True


def resend_failed(
    session: Session, integration_id: uuid.UUID, *, actor: str, limit: int
) -> list[uuid.UUID]:
    """Todas as sincronizações que falharam desta integração (até `limit`) voltam para `pending`: o
    que se faz depois de corrigir o token, a tabela ou o mapeamento. Devolve os ids, para
    disparar."""
    failed = list(
        session.scalars(
            sa.select(IntegrationSync.id)
            .where(
                IntegrationSync.integration_id == integration_id, IntegrationSync.status == "failed"
            )
            .order_by(IntegrationSync.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    if not failed:
        return []
    session.execute(
        sa.update(IntegrationSync)
        .where(IntegrationSync.id.in_(failed))
        .values(status="pending", hatchet_run_id=None)
        .execution_options(synchronize_session=False)
    )
    session.add(
        AuditLog(
            actor=actor,
            action="sync.resend_failed",
            target_type="integration",
            target_id=str(integration_id),
            details={"count": len(failed)},
        )
    )
    return failed


# --- o que o admin mostra ----------------------------------------------------------------------


@dataclass(frozen=True)
class SyncCounts:
    pending: int = 0
    succeeded: int = 0
    failed: int = 0


def counts(session: Session, integration_id: uuid.UUID) -> SyncCounts:
    """Quantas sincronizações da integração estão em cada estado."""
    rows = dict(
        session.execute(
            sa.select(IntegrationSync.status, sa.func.count())
            .where(IntegrationSync.integration_id == integration_id)
            .group_by(IntegrationSync.status)
        ).all()
    )
    return SyncCounts(
        pending=rows.get("pending", 0),
        succeeded=rows.get("succeeded", 0),
        failed=rows.get("failed", 0),
    )


@dataclass(frozen=True)
class SyncRow:
    """Uma sincronização, com o pedido a que pertence. `job_attempt` é a tentativa do pedido que
    gerou o PDF sincronizado (a do PDF dela, e não a tentativa atual do pedido)."""

    id: uuid.UUID
    job_public_id: str
    form_slug: str
    kind: str  # o tipo da integração (`app.models.INTEGRATION_KINDS`: hoje, `baserow`)
    host: str
    table_id: int
    status: str
    attempts: int
    last_error: str | None
    outcome: str | None
    row_id: int | None
    synced_at: datetime | None
    created_at: datetime
    job_attempt: int


def _rows(session: Session, *conditions: sa.ColumnElement[bool], limit: int) -> list[SyncRow]:
    found = session.execute(
        sa.select(IntegrationSync, Job.public_id, Artifact.version, Integration)
        .join(Job, Job.id == IntegrationSync.job_id)
        .join(Artifact, Artifact.id == IntegrationSync.artifact_id)
        .join(Integration, Integration.id == IntegrationSync.integration_id)
        .where(*conditions)
        .order_by(IntegrationSync.created_at.desc())
        .limit(limit)
    ).all()
    return [
        SyncRow(
            id=sync.id,
            job_public_id=public_id,
            form_slug=integration.form_slug,
            kind=integration.kind,
            host=host_of(integration.base_url),
            table_id=integration.table_id,
            status=sync.status,
            attempts=sync.attempts,
            last_error=sync.last_error,
            outcome=sync.outcome,
            row_id=sync.row_id,
            synced_at=sync.synced_at,
            created_at=sync.created_at,
            job_attempt=job_attempt,
        )
        for sync, public_id, job_attempt, integration in found
    ]


def for_job(session: Session, job_id: uuid.UUID) -> list[SyncRow]:
    """As sincronizações de um pedido, da mais recente à mais antiga."""
    return _rows(session, IntegrationSync.job_id == job_id, limit=50)


def recent(
    session: Session, integration_id: uuid.UUID, *, status: str | None = None, limit: int = 20
) -> list[SyncRow]:
    """As sincronizações mais recentes de uma integração (só os de `status`, se vier)."""
    conditions = [IntegrationSync.integration_id == integration_id]
    if status is not None:
        conditions.append(IntegrationSync.status == status)
    return _rows(session, *conditions, limit=limit)
