"""As ações do admin sobre os pedidos: entregar o PDF de uma tentativa (`pdf_file`), reprocessar um
pedido (`reprocess`) e reenviar uma entrega (`resend_delivery`) ou uma sincronização (`resend_sync`).

A página e a API chamam as mesmas funções (ADR 0015, "A página e a API dividem os serviços"): elas não
conhecem `Request`, HTML nem aviso de tela. Recebem o autor (`actor`: `ADMIN_USERNAME` pela página,
`chave:<nome>` pela API), devolvem um resultado ou levantam uma exceção de domínio, e cada lado a
traduz (a página, em aviso e redirecionamento; a API, em JSON e status). A regra do reprocessamento (o
motivo de 10 a 300 caracteres, a confirmação, a tentativa que a pessoa viu) e o disparo no Hatchet
(`app.admin.launching`) moravam dentro das rotas HTML; moveram-se para cá sem mudar.

**O disparo que não chega ao engine não é erro:** a ação vale (a tentativa nova existe, a entrega ou a
sincronização voltou para pendente), `launched` diz `False`, e o reconciliador dispara o que ficou sem
run id. Nada aqui leva dado da lead para o Hatchet: as tasks recebem só ids, e as mensagens de erro
nunca citam o motivo digitado.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import sqlalchemy as sa
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app import jobs
from app.admin import launching
from app.deps import Deps
from app.errors import FailureCode, RetryableError
from app.integration_readiness import configuration_lock
from app.integrations import syncs as integration_syncs
from app.internal_links import (
    AdminPdfAccess,
    InternalPdfAccess,
    ParticipantPdfAccess,
    PdfAccess,
    validate_internal_access,
)
from app.models import Artifact, Delivery, IntegrationSync, Job, utcnow
from app.pdf_availability import artifact_availability, verified_content, verify_completion
from app.plan_release import ReleaseCommit, ReleasePdfCommand, require_participant_access
from app.tokens import TokenError

logger = logging.getLogger(__name__)

SIGNED_URL_TTL_SECONDS = 300
REASON_MIN_LENGTH = 10
REASON_MAX_LENGTH = 300
REASON_INVALID = (
    f"Descreva o motivo com pelo menos {REASON_MIN_LENGTH} caracteres "
    f"(no máximo {REASON_MAX_LENGTH})."
)
CONFIRM_MISSING = "Confirme que entende que isso gera uma nova tentativa."


class NoPdf(Exception):
    """O pedido não existe, ou ainda não tem PDF (ou não tem o da tentativa pedida)."""


class PdfRemoved(Exception):
    """O PDF existia e a retenção já apagou o arquivo (`app.retention`)."""


@dataclass(frozen=True)
class PdfFile:
    """O PDF a entregar: ou `url`, a URL assinada de curta duração do storage (a página manda o
    navegador buscá-lo), ou `content`, os bytes. Nunca os dois."""

    filename: str
    url: str | None = None
    content: bytes | None = None


def pdf_file(
    deps: Deps,
    public_id: str,
    version: int | None,
    *,
    signed_url: bool,
    filename: str | None = None,
    access: PdfAccess,
) -> PdfFile:
    """O PDF da tentativa `version` do pedido (a atual, sem `version`).

    Com `signed_url`, e se o storage assina (o S3, e não o disco local), devolve a URL de 300 s em
    vez dos bytes: é o que a página faz. Sem ele, devolve sempre os bytes: a API entrega o arquivo, e
    uma URL assinada seria um segundo segredo, fora da chave e da auditoria.

    Levanta `NoPdf` (404) e `PdfRemoved` (410). Um storage que falha levanta o erro dele."""
    with deps.sessions.begin() as session:
        configuration_lock(session, shared=True)
        if isinstance(access, InternalPdfAccess):
            internal_job, _ = validate_internal_access(session, deps, access.claims)
            if internal_job.public_id != public_id or version not in (None, access.claims.attempt):
                raise NoPdf
        job = session.scalar(sa.select(Job).where(Job.public_id == public_id).with_for_update())
        if job is None or (version is None and job.status != "completed"):
            raise NoPdf
        if isinstance(access, ParticipantPdfAccess) and (
            job.id != access.job_id
            or job.attempt != access.attempt
            or job.token_version != access.token_version
            or job.expires_at <= utcnow()
            or version not in (None, access.attempt)
        ):
            raise NoPdf
        if not isinstance(access, (AdminPdfAccess, ParticipantPdfAccess, InternalPdfAccess)):
            raise NoPdf
        attempt = job.attempt if version is None else version
        artifact = session.scalar(
            sa.select(Artifact)
            .where(Artifact.job_id == job.id, Artifact.kind == "pdf", Artifact.version == attempt)
            .with_for_update()
        )
        if artifact is None:
            raise NoPdf
        if isinstance(access, ParticipantPdfAccess):
            require_participant_access(session, job, artifact)
        state = artifact_availability(deps.storage, artifact)
        if state.state == "removed":
            raise PdfRemoved
        if state.state == "unknown" and state.exists is True:
            # Históricos só ganham prova depois de ler e validar os bytes, nunca por backfill.
            content = verify_completion(session, deps.storage, job, attempt, artifact.id)
        elif state.available:
            content, _ = verified_content(deps.storage, artifact)
        else:
            raise RetryableError(FailureCode.STORAGE_FAILED, "PDF indisponível ou não verificado")
        filename = filename or f"plano-{public_id}-v{artifact.version}.pdf"
        if signed_url:
            ttl_seconds = SIGNED_URL_TTL_SECONDS
            if not isinstance(access, AdminPdfAccess):
                now = utcnow().timestamp()
                deadline = job.expires_at.timestamp()
                if isinstance(access, InternalPdfAccess):
                    deadline = min(deadline, access.claims.expires_at)
                ttl_seconds = min(ttl_seconds, int(deadline - now))
                if ttl_seconds < 1:
                    raise TokenError("expired")
            try:
                url = deps.storage.presigned_url(
                    artifact.storage_key, ttl_seconds=ttl_seconds, filename=filename
                )
            except Exception:  # noqa: BLE001 — fronteira de storage com erro seguro
                raise RetryableError(
                    FailureCode.STORAGE_FAILED, "PDF indisponível no storage"
                ) from None
            if url:
                return PdfFile(filename, url=url)
        return PdfFile(filename, content=content)


# --- reprocessar e reenviar ---------------------------------------------------------------------


class JobNotFound(Exception):
    """O pedido não existe."""


class InvalidReprocess(Exception):
    """O reprocessamento não vale: `errors` mapeia o campo (`reason`, `confirm`) ao motivo, em
    português. Nada foi feito. A mensagem da exceção só diz os campos, nunca o texto do motivo (que
    pode citar a lead)."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("reprocessamento recusado: " + ", ".join(errors))
        self.errors = errors


class ReprocessRefused(Exception):
    """O pedido não está concluído nem falho (está na fila ou gerando), ou já não está na tentativa
    que a pessoa viu (duplo clique, outra pessoa reprocessou antes). Nada foi feito."""


class ResendRefused(Exception):
    """A entrega ou a sincronização não pode ser reenviada. `missing` diz por quê: `True`, ela não
    existe neste pedido; `False`, existe mas não está em `failed` (já voltou para a fila, ou já saiu).
    Nada foi feito."""

    def __init__(self, *, missing: bool) -> None:
        super().__init__(
            "não existe neste pedido"
            if missing
            else "estado, revisão, audiência ou tentativa não autorizados"
        )
        self.missing = missing


@dataclass(frozen=True)
class Reprocessed:
    """A tentativa nova de um pedido. `launched`: o engine recebeu o disparo (`False`: a tentativa
    existe e o reconciliador a dispara)."""

    job_id: uuid.UUID
    attempt: int
    launched: bool


@dataclass(frozen=True)
class Resent:
    """A entrega ou a sincronização voltou para pendente. `launched`: o engine recebeu o disparo
    (`False`: ela está pendente sem run id e o reconciliador a dispara)."""

    launched: bool


def _job_id(session: Session, public_id: str) -> uuid.UUID | None:
    return session.scalar(sa.select(Job.id).where(Job.public_id == public_id))


async def reprocess(
    deps: Deps,
    trigger: launching.Trigger,
    public_id: str,
    *,
    actor: str,
    reason: str,
    confirm: bool,
    expected_attempt: int | None,
) -> Reprocessed:
    """Nova tentativa auditada em nome de `actor`, disparada uma única vez.

    O `reason` (depois de aparado) tem de ter de 10 a 300 caracteres e `confirm` tem de ser verdadeiro;
    os dois problemas saem juntos em `InvalidReprocess`, antes de qualquer consulta. `expected_attempt`
    é a tentativa que a pessoa viu: se o pedido já está noutra (duplo clique, outra pessoa, uma resposta
    que se perdeu e foi repetida), `ReprocessRefused`, sem segunda tentativa; `None` não confere a
    tentativa, só o estado (concluído ou falho). `trigger` é o `app.state.trigger`.

    Levanta `InvalidReprocess`, `JobNotFound` e `ReprocessRefused`."""
    reason = reason.strip()
    errors = {}
    if not REASON_MIN_LENGTH <= len(reason) <= REASON_MAX_LENGTH:
        errors["reason"] = REASON_INVALID
    if not confirm:
        errors["confirm"] = CONFIRM_MISSING
    if errors:
        raise InvalidReprocess(errors)

    def create() -> tuple[uuid.UUID, int | None] | None:
        with deps.sessions.begin() as session:
            job_id = _job_id(session, public_id)
            if job_id is None:
                return None
            return job_id, jobs.reprocess(
                session, job_id, actor=actor, reason=reason, expected_attempt=expected_attempt
            )

    created = await run_in_threadpool(create)
    if created is None:
        raise JobNotFound
    job_id, attempt = created
    if attempt is None:
        raise ReprocessRefused
    launched = await launching.launch_attempt(trigger, job_id, attempt)  # falha: o reconciliador
    logger.info(
        "admin: reprocessamento job=%s tentativa=%s por=%s launched=%s",
        job_id,
        attempt,
        actor,
        launched,
    )
    return Reprocessed(job_id=job_id, attempt=attempt, launched=launched)


async def resend_delivery(
    deps: Deps,
    public_id: str,
    delivery_id: uuid.UUID,
    *,
    actor: str,
    expected_config_version: int | None = None,
    expected_attempt: int | None = None,
) -> Resent:
    """Uma entrega que falhou volta para pendente (auditado em nome de `actor`) e o `deliver` é
    disparado de novo. Sem engine, o SDK insiste por minutos: a espera é limitada
    (`app.admin.launching`), e o reconciliador encontra a entrega pendente sem run id.

    Levanta `JobNotFound` e `ResendRefused`."""
    await _requeue(
        deps,
        public_id,
        Delivery,
        delivery_id,
        lambda session, job_id: jobs.resend_delivery(
            session,
            job_id,
            delivery_id,
            actor=actor,
            expected_config_version=expected_config_version,
            expected_attempt=expected_attempt,
            retention_days=deps.settings.retention_days,
        ),
    )
    logger.info("admin: reenvio delivery=%s por=%s", delivery_id, actor)
    return Resent(launched=await launching.launch_delivery(deps, delivery_id))


async def resend_sync(deps: Deps, public_id: str, sync_id: uuid.UUID, *, actor: str) -> Resent:
    """Uma sincronização com a planilha que falhou volta para pendente (auditado em nome de `actor`) e
    é disparada de novo, como o reenvio de uma entrega.

    Levanta `JobNotFound` e `ResendRefused`."""
    await _requeue(
        deps,
        public_id,
        IntegrationSync,
        sync_id,
        lambda session, job_id: integration_syncs.resend(session, job_id, sync_id, actor=actor),
    )
    logger.info("admin: reenvio da sincronização sync=%s por=%s", sync_id, actor)
    return Resent(launched=await launching.launch_sync(deps, sync_id))


async def _requeue(
    deps: Deps,
    public_id: str,
    model: type[Delivery | IntegrationSync],
    target: uuid.UUID,
    requeue: Callable[[Session, uuid.UUID], bool],
) -> None:
    """Volta a linha `target` (de `model`) do pedido `public_id` para pendente, numa transação. Se não
    voltou, descobre por quê: não existe neste pedido (`missing`) ou existe e não está em `failed`."""

    def run() -> Literal["no_job", "requeued", "refused", "missing"]:
        with deps.sessions.begin() as session:
            job_id = _job_id(session, public_id)
            if job_id is None:
                return "no_job"
            if requeue(session, job_id):
                return "requeued"
            exists = session.scalar(
                sa.select(model.id).where(model.id == target, model.job_id == job_id)
            )
            return "refused" if exists is not None else "missing"

    outcome = await run_in_threadpool(run)
    if outcome == "no_job":
        raise JobNotFound
    if outcome != "requeued":
        raise ResendRefused(missing=outcome == "missing")


@dataclass(frozen=True)
class ReleasedPdf:
    commit: ReleaseCommit
    notifications: tuple[dict, ...]
    launched: bool


async def release_pdf(
    deps: Deps, trigger, public_id: str, *, actor: str, command: ReleasePdfCommand
) -> ReleasedPdf:
    """Autoriza uma vez e dispara somente novas intenções depois do commit."""
    from app.plan_release import release_pdf as commit_release

    def create():
        with deps.sessions.begin() as session:
            return commit_release(session, deps, public_id, actor=actor, command=command)

    committed = await run_in_threadpool(create)
    launched = False
    if not committed.replay:
        for delivery_id in committed.delivery_ids:
            launched = await launching.launch_delivery(deps, delivery_id) or launched

    def read():
        with deps.sessions() as session:
            return tuple(
                {"id": row.id, "event": row.event, "status": row.status}
                for row in session.scalars(
                    sa.select(Delivery).where(
                        sa.or_(
                            Delivery.release_id == committed.release_id,
                            Delivery.id.in_(committed.delivery_ids),
                        )
                    )
                )
            )

    return ReleasedPdf(committed, await run_in_threadpool(read), launched)
