"""Passos do pipeline, independentes do Hatchet (as tasks em `app.workflows` só os chamam).

Todos são idempotentes por tentativa: o Hatchet entrega pelo menos uma vez, então qualquer passo
pode rodar de novo depois de já ter gravado o resultado.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app import destinations, jobs
from app.deps import Deps
from app.errors import FailureCode, RetryableError, TerminalError
from app.forms import FormSpec
from app.integrations import syncs
from app.models import Artifact, Delivery, Job, Plan, Submission, utcnow
from app.submission_identity import identity_from_answers, resolve_identity, submission_form

WEBHOOK_DESTINATION = "webhook"
PLAN_COMPLETED_EVENT = "plano.concluido"
DIRECT_URL_TTL = timedelta(hours=24)


def _form_of(deps: Deps, submission: Submission) -> FormSpec:
    """A versão do formulário que a lead respondeu. Reprocessar e o reconciliador só disparam
    estes passos de novo, então toda tentativa de um pedido segue a mesma versão."""
    return submission_form(deps.forms, submission)


def _load_answers(deps: Deps, submission: Submission) -> tuple[FormSpec, BaseModel]:
    spec = _form_of(deps, submission)
    try:
        return spec, spec.answers_model.model_validate(submission.answers)
    except ValidationError:
        raise TerminalError(
            FailureCode.INTERNAL, "respostas não validam com a versão do formulário do envio"
        ) from None


def generate_plan(deps: Deps, job_id: uuid.UUID, attempt: int) -> tuple[uuid.UUID, bool]:
    """Chama o LLM e grava o plano validado. Se esta tentativa já tem plano, reaproveita
    (não paga o LLM de novo). Devolve (plan_id, reaproveitado)."""
    with deps.sessions.begin() as session:
        existing = _plan_id(session, job_id, attempt)
        if existing is not None:
            return existing, True
        submission = _submission_of(session, job_id)
        jobs.set_stage(session, job_id, attempt, "generating")
    spec, answers = _load_answers(deps, submission)

    result = deps.llm.generate(spec, answers)  # fora de transação: pode levar minutos

    with deps.sessions.begin() as session:
        try:
            with session.begin_nested():
                plan = Plan(
                    job_id=job_id,
                    version=attempt,
                    document=result.document,
                    model=result.model,
                    prompt_version=spec.prompt_version,
                    usage=result.usage,
                )
                session.add(plan)
                session.flush()
                return plan.id, False
        except IntegrityError:
            existing = _plan_id(session, job_id, attempt)
            if existing is None:
                raise
            return existing, True


def render_pdf(
    deps: Deps, job_id: uuid.UUID, attempt: int, plan_id: uuid.UUID
) -> tuple[uuid.UUID, bool]:
    """Gera o PDF, guarda no storage e registra o artefato. A chave é só job e tentativa, sem o hash:
    o renderizador não é determinístico (cada chamada devolve bytes diferentes), e uma chave com o
    hash deixaria no bucket, sem linha em `artifacts`, o arquivo de uma tentativa que morreu entre o
    `put` e o INSERT. Com a chave fixa, a nova tentativa sobrescreve o mesmo objeto."""
    with deps.sessions.begin() as session:
        existing = _artifact_id(session, job_id, attempt)
        if existing is not None:
            return existing, True
        plan = session.get_one(Plan, plan_id)
        submission = _submission_of(session, job_id)
        jobs.set_stage(session, job_id, attempt, "rendering")
    spec = _form_of(deps, submission)
    lead = identity_from_answers(submission, spec)
    context = {
        "lead": {"name": lead.name},
        "form": {"slug": spec.slug, "title": spec.title},
        "scores": submission.scores or {},
        "generated_at": datetime.now(UTC),
    }

    pdf = deps.pdf.render(spec, plan.document, context)
    if not pdf.content.startswith(b"%PDF"):
        raise TerminalError(
            FailureCode.RENDER_FAILED, "renderizador devolveu conteúdo que não é PDF"
        )
    sha256 = hashlib.sha256(pdf.content).hexdigest()
    key = f"plans/{job_id}/v{attempt}.pdf"
    deps.storage.put(key, pdf.content, "application/pdf")

    with deps.sessions.begin() as session:
        try:
            with session.begin_nested():
                artifact = Artifact(
                    job_id=job_id,
                    plan_id=plan_id,
                    kind="pdf",
                    version=attempt,
                    storage_key=key,
                    sha256=sha256,
                    byte_size=len(pdf.content),
                    renderer=pdf.renderer,
                )
                session.add(artifact)
                session.flush()
                return artifact.id, False
        except IntegrityError:
            winner = _artifact_of(session, job_id, attempt)
            if winner is None:
                raise
            winner_id, winner_key = winner
    # Outra execução da mesma tentativa registrou o artefato primeiro. A chave costuma ser a mesma;
    # difere se o vencedor veio de um código com outra chave (o hash no nome). Aí o arquivo desta
    # execução não tem linha e a retenção nunca o apagaria. Com a mesma chave, apagar levaria o
    # arquivo do vencedor.
    if winner_key != key:
        deps.storage.delete(key)
    return winner_id, True


def finalize(
    deps: Deps, job_id: uuid.UUID, attempt: int, artifact_id: uuid.UUID
) -> list[uuid.UUID]:
    """Conclui o job e cria as entregas pendentes na mesma transação. Devolve as entregas a
    disparar. As linhas de `deliveries` são a "outbox" do envio: o reconciliador dispara as que
    ficarem para trás. Sem destino do webhook para o formulário, não nasce entrega. As sincronizações
    com as integrações ativas do formulário (`app.integrations.syncs`) nascem na mesma transação, com
    ou sem destino do webhook: são outra saída do pedido, e `pending_syncs` devolve as que a task
    dispara."""
    with deps.sessions.begin() as session:
        jobs.complete(session, job_id, attempt)
        job = session.get_one(Job, job_id)
        if job.status != "completed" or job.attempt != attempt:
            return []
        submission = session.get_one(Submission, job.submission_id)
        syncs.enqueue(session, submission.form_slug, job_id, artifact_id)
        if not destinations.configured(session, submission.form_slug):
            return []
        session.execute(
            insert(Delivery)
            .values(
                job_id=job_id,
                artifact_id=artifact_id,
                destination=WEBHOOK_DESTINATION,
                event=PLAN_COMPLETED_EVENT,
            )
            .on_conflict_do_nothing(constraint="uq_deliveries_artifact_destination")
        )
        return list(
            session.scalars(
                sa.select(Delivery.id).where(
                    Delivery.artifact_id == artifact_id, Delivery.status == "pending"
                )
            )
        )


def pending_syncs(deps: Deps, artifact_id: uuid.UUID) -> list[uuid.UUID]:
    """As sincronizações deste PDF que ainda não foram disparadas no Hatchet."""
    with deps.sessions() as session:
        return syncs.pending_for_artifact(session, artifact_id)


def deliver(deps: Deps, delivery_id: uuid.UUID) -> str:
    """Envia uma entrega ao destino do webhook, resolvido agora (o do formulário, senão o padrão)
    e com o segredo vigente. Devolve "succeeded" ou "already_succeeded". Sem destino, ou com um
    segredo que não abre, a entrega espera nas repetições do Hatchet, com o motivo registrado."""
    try:
        outgoing = _outgoing(deps, delivery_id)
    except (RetryableError, TerminalError) as exc:
        _record_error(deps, delivery_id, exc)
        raise
    if outgoing is None:
        return "already_succeeded"

    destination = outgoing.destination
    try:
        status = deps.webhook.send(
            destination.url, destination.secret, outgoing.event, outgoing.event_id, outgoing.payload
        )
    except Exception as exc:
        _record_error(deps, delivery_id, exc)
        raise

    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(
                status="succeeded", response_status=status, delivered_at=utcnow(), last_error=None
            )
        )
    return "succeeded"


@dataclass(frozen=True)
class _Outgoing:
    destination: destinations.Destination
    event: str
    event_id: str
    payload: dict[str, Any]


def _outgoing(deps: Deps, delivery_id: uuid.UUID) -> _Outgoing | None:
    """O envio desta tentativa, com o destino registrado na entrega; None se já foi entregue."""
    with deps.sessions.begin() as session:
        delivery = session.get_one(Delivery, delivery_id)
        if delivery.status == "succeeded":
            return None
        destination = _destination_of(session, deps, delivery)
        payload = build_payload(session, deps, delivery)
        delivery.attempts += 1
        delivery.destination_scope = destination.scope
        delivery.destination_host = destination.host
        return _Outgoing(destination, delivery.event, str(delivery.id), payload)


def _destination_of(session: Any, deps: Deps, delivery: Delivery) -> destinations.Destination:
    """Sem destino, ou com um segredo que não abre, a falha é retentável: corrigir o destino no
    admin salva as entregas que ainda estão em repetição."""
    submission = _submission_of(session, delivery.job_id)
    try:
        destination = destinations.resolve(session, deps.cipher, submission.form_slug)
    except destinations.UnreadableSecret:
        raise RetryableError(
            FailureCode.DELIVERY_UNCONFIGURED,
            "o segredo do destino não abre com WEBHOOK_ENCRYPTION_KEY",
        ) from None
    if destination is None:
        raise RetryableError(
            FailureCode.DELIVERY_UNCONFIGURED, "o formulário está sem destino do webhook"
        )
    return destination


def _record_error(deps: Deps, delivery_id: uuid.UUID, exc: Exception) -> None:
    """O motivo da falha fica na entrega (o admin mostra), sem dados da lead: só códigos."""
    known = isinstance(exc, RetryableError | TerminalError)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(last_error=(str(exc) if known else type(exc).__name__)[:300])
        )


def set_delivery_run_id(deps: Deps, delivery_id: uuid.UUID, run_id: str) -> None:
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Delivery).where(Delivery.id == delivery_id).values(hatchet_run_id=run_id)
        )


def mark_delivery_failed(deps: Deps, delivery_id: uuid.UUID, code: str) -> None:
    """A entrega esgotou as tentativas. O motivo que o último envio registrou ("[código]
    detalhe", como o segredo que não abre) fica para o admin; sem ele, fica o código."""
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Delivery)
            .where(Delivery.id == delivery_id, Delivery.status == "pending")
            .values(status="failed", last_error=sa.func.coalesce(Delivery.last_error, code))
        )


def lead_links(deps: Deps, job: Job) -> dict[str, str]:
    token = deps.signer.issue(job.public_id, job.token_version)
    base = deps.settings.public_base_url.rstrip("/")
    return {
        "status_url": f"{base}/v1/jobs/{job.public_id}?t={token}",
        "pdf_url": f"{base}/v1/jobs/{job.public_id}/pdf?t={token}",
        # Página do lead; o `t` vira cookie no primeiro acesso (app.web.public).
        "page_url": f"{base}/d/{job.public_id}?t={token}",
    }


def build_payload(session: Any, deps: Deps, delivery: Delivery) -> dict[str, Any]:
    job = session.get_one(Job, delivery.job_id)
    submission = session.get_one(Submission, job.submission_id)
    lead = resolve_identity(deps.forms, submission)
    artifact = session.get_one(Artifact, delivery.artifact_id)
    plan = session.get_one(Plan, artifact.plan_id)
    links = lead_links(deps, job)
    direct_url = (
        None  # a retenção já apagou o arquivo (reenvio feito muito depois)
        if artifact.purged_at is not None
        else deps.storage.presigned_url(
            artifact.storage_key,
            ttl_seconds=int(DIRECT_URL_TTL.total_seconds()),
            filename=f"plano-{job.public_id}.pdf",
        )
    )
    return {
        "event": delivery.event,
        "event_id": str(delivery.id),
        "occurred_at": utcnow().isoformat(),
        "form": {"slug": submission.form_slug, "version": submission.form_version},
        "job": {
            "public_id": job.public_id,
            "attempt": job.attempt,
            "status_url": links["status_url"],
            "page_url": links["page_url"],
        },
        "lead": {"email": lead.email, "name": lead.name, "phone": lead.phone},
        "answers": submission.answers,
        "consent": submission.consent,
        "tracking": submission.tracking,
        "scores": submission.scores,
        "priority": submission.priority,
        "persona": submission.persona,
        "plan": plan.document,
        "pdf": {
            "url": links["pdf_url"],
            "expires_at": job.expires_at.isoformat(),
            "direct_url": direct_url,
            "direct_url_expires_in": int(DIRECT_URL_TTL.total_seconds()) if direct_url else None,
            "sha256": artifact.sha256,
            "size_bytes": artifact.byte_size,
        },
    }


def _submission_of(session: Any, job_id: uuid.UUID) -> Submission:
    job = session.get_one(Job, job_id)
    return session.get_one(Submission, job.submission_id)


def _plan_id(session: Any, job_id: uuid.UUID, attempt: int) -> uuid.UUID | None:
    return session.scalar(sa.select(Plan.id).where(Plan.job_id == job_id, Plan.version == attempt))


def _artifact_id(session: Any, job_id: uuid.UUID, attempt: int) -> uuid.UUID | None:
    return session.scalar(
        sa.select(Artifact.id).where(
            Artifact.job_id == job_id, Artifact.kind == "pdf", Artifact.version == attempt
        )
    )


def _artifact_of(session: Any, job_id: uuid.UUID, attempt: int) -> tuple[uuid.UUID, str] | None:
    """O ID e a chave no storage do PDF desta tentativa, se já foi registrado."""
    row = session.execute(
        sa.select(Artifact.id, Artifact.storage_key).where(
            Artifact.job_id == job_id, Artifact.kind == "pdf", Artifact.version == attempt
        )
    ).one_or_none()
    return None if row is None else (row.id, row.storage_key)
