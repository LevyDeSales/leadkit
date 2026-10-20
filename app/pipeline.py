"""Passos do pipeline, independentes do Hatchet (as tasks em `app.workflows` só os chamam).

Todos são idempotentes por tentativa: o Hatchet entrega pelo menos uma vez, então qualquer passo
pode rodar de novo depois de já ter gravado o resultado.
"""

from __future__ import annotations

import hashlib
import re
import uuid
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
from app.models import Artifact, Delivery, Job, Plan, Submission, WebhookDestination, utcnow
from app.pdf_availability import current_pdf, verify_completion
from app.pdf_validation import validate_pdf
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
    validate_pdf(pdf.content)
    sha256 = hashlib.sha256(pdf.content).hexdigest()
    key = f"plans/{job_id}/v{attempt}.pdf"
    with deps.sessions.begin() as session:
        # Serializa gravações da chave fixa com conclusão/reprocessamento. Uma duplicata que
        # terminou de renderizar depois da vencedora nunca sobrescreve os bytes já registrados.
        job = session.scalar(sa.select(Job).where(Job.id == job_id).with_for_update())
        existing = _artifact_id(session, job_id, attempt)
        if existing is not None:
            return existing, True
        if job is None or job.attempt != attempt or job.status != "running":
            raise TerminalError(FailureCode.RENDER_FAILED, "tentativa não está em processamento")
        deps.storage.put(key, pdf.content, "application/pdf")
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
        from app.integration_readiness import configuration_lock

        configuration_lock(session, shared=True)
        metadata = session.execute(
            sa.select(Submission.form_slug, Job.status, Job.attempt)
            .join(Job, Job.submission_id == Submission.id)
            .where(Job.id == job_id)
        ).first()
        replay = (
            metadata is not None and metadata.status == "completed" and metadata.attempt == attempt
        )
        if metadata is not None and not replay:
            configured = destinations.serving(destinations.listing(session), metadata.form_slug)
            if configured is not None and configured.purpose == "participant":
                from app.destination_purposes import require_participant_capability

                require_participant_capability(session)
        job = session.scalar(sa.select(Job).where(Job.id == job_id).with_for_update())
        if job is None or job.attempt != attempt or job.status not in ("running", "completed"):
            return []
        artifact = session.get(Artifact, artifact_id)
        plan = session.get(Plan, artifact.plan_id) if artifact is not None else None
        if (
            artifact is None
            or artifact.job_id != job_id
            or artifact.version != attempt
            or artifact.kind != "pdf"
            or plan is None
            or plan.job_id != job_id
            or plan.version != attempt
        ):
            raise TerminalError(FailureCode.RENDER_FAILED, "artefato incompatível com a tentativa")
        if job.status == "completed":
            # A conclusão e suas intenções originais são atômicas; ausência também é definitiva.
            return _pending_delivery_ids(session, artifact_id)
        verify_completion(session, deps.storage, job, attempt, artifact_id)
        jobs.complete(session, job_id, attempt)
        session.refresh(job)
        if job.status != "completed" or job.attempt != attempt:
            return []
        submission = session.get_one(Submission, job.submission_id)
        syncs.enqueue(session, submission.form_slug, job_id, artifact_id)
        destination = session.scalar(
            sa.select(WebhookDestination)
            .where(destinations._serving(submission.form_slug))
            .order_by(WebhookDestination.form_slug.is_(None))
            .limit(1)
        )
        if destination is None:
            return []
        if destination.purpose == "participant":
            from app.destination_purposes import require_participant_capability

            require_participant_capability(session)
        session.execute(
            insert(Delivery)
            .values(
                job_id=job_id,
                artifact_id=artifact_id,
                webhook_destination_id=destination.id,
                config_version=destination.version,
                audience=destination.purpose,
                authorization_origin="explicit",
                authorized_attempt=attempt,
                destination=WEBHOOK_DESTINATION,
                event=PLAN_COMPLETED_EVENT,
            )
            .on_conflict_do_nothing(constraint="uq_deliveries_artifact_destination")
        )
        return _pending_delivery_ids(session, artifact_id)


def _pending_delivery_ids(session: Any, artifact_id: uuid.UUID) -> list[uuid.UUID]:
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


def deliver(deps: Deps, delivery_id: uuid.UUID, dispatch_no: int = 1) -> str:
    """Revalida sob trava e mantém autorização estável até o transporte limitado concluir."""
    from app.integration_readiness import configuration_lock

    failure = None
    with deps.sessions.begin() as session:
        configuration_lock(session, shared=True)
        metadata = session.execute(
            sa.select(Delivery.audience, Delivery.status, Delivery.dispatch_no).where(
                Delivery.id == delivery_id
            )
        ).first()
        if metadata is None or metadata.status == "succeeded":
            return "already_succeeded"
        if metadata.dispatch_no != dispatch_no or metadata.status != "pending":
            return "obsolete"
        if metadata.audience == "participant":
            from app.destination_purposes import InvalidPurpose, require_participant_capability

            try:
                require_participant_capability(session)
            except InvalidPurpose:
                raise RetryableError(
                    FailureCode.DELIVERY_UNCONFIGURED,
                    "finalidade participante indisponível por compatibilidade",
                ) from None
        job_id = session.scalar(sa.select(Delivery.job_id).where(Delivery.id == delivery_id))
        if job_id is None:
            return "already_succeeded"
        job = session.scalar(sa.select(Job).where(Job.id == job_id).with_for_update())
        delivery = session.scalar(
            sa.select(Delivery).where(Delivery.id == delivery_id).with_for_update()
        )
        if job is None or delivery is None or delivery.status == "succeeded":
            return "already_succeeded"
        if delivery.dispatch_no != dispatch_no or delivery.status != "pending":
            return "obsolete"
        try:
            if jobs.delivery_retention_expired(session, job, deps.settings.retention_days):
                raise TerminalError(
                    FailureCode.DELIVERY_REJECTED, "prazo de retenção do pedido encerrado"
                )
            destination = _destination_of(session, deps, delivery)
            payload = build_payload(session, deps, delivery)
            delivery.attempts += 1
            delivery.destination_scope = destination.scope
            delivery.destination_host = destination.host
            status = deps.webhook.send(
                destination.url, destination.secret, delivery.event, str(delivery.id), payload
            )
            if not isinstance(status, int) or not 200 <= status < 300:
                raise TerminalError(
                    FailureCode.DELIVERY_REJECTED, "transporte sem evidência de entrega"
                )
            delivery.status, delivery.response_status = "succeeded", status
            delivery.delivered_at, delivery.last_error = utcnow(), None
        except Exception as exc:  # noqa: BLE001 - registra somente código e relança após commit
            delivery.last_error = _delivery_error(exc)
            failure = exc
    if failure is not None:
        raise failure
    return "succeeded"


def _delivery_error(exc: Exception) -> str:
    """Persiste só códigos, status HTTP e descrições fixas do domínio; nunca detalhe do provedor."""
    if not isinstance(exc, RetryableError | TerminalError) or not isinstance(exc.code, FailureCode):
        return "[internal] falha inesperada na entrega PDF"
    safe = {
        "o segredo do destino não abre com WEBHOOK_ENCRYPTION_KEY",
        "destino, revisão ou finalidade não autorizados; confira a configuração e solicite repetição explícita",
        "finalidade participante indisponível por compatibilidade",
        "prazo de retenção do pedido encerrado",
        "transporte sem evidência de entrega",
        "entrega de tentativa anterior não é atual",
        "projeção participante indisponível para a versão histórica",
    }
    detail = exc.detail
    if not isinstance(detail, str) or (
        detail not in safe and re.fullmatch(r"HTTP [1-5][0-9]{2}", detail) is None
    ):
        detail = "falha no transporte da entrega PDF"
    return f"[{exc.code.value}] {detail}"


def _destination_of(session: Any, deps: Deps, delivery: Delivery) -> destinations.Destination:
    """Legados só resolvem internos; fatos novos nunca adotam outro receptor ou revisão."""
    if delivery.authorization_origin == "legacy":
        submission = _submission_of(session, delivery.job_id)
        row = session.scalar(
            sa.select(WebhookDestination)
            .where(destinations._serving(submission.form_slug))
            .order_by(WebhookDestination.form_slug.is_(None))
            .limit(1)
            .with_for_update()
        )
        valid = row is not None and row.purpose == "internal"
    else:
        row = session.scalar(
            sa.select(WebhookDestination)
            .where(WebhookDestination.id == delivery.webhook_destination_id)
            .with_for_update()
        )
        valid = (
            row is not None
            and row.version == delivery.config_version
            and row.purpose == delivery.audience
        )
    if not valid or "plano.concluido" not in row.events:
        raise RetryableError(
            FailureCode.DELIVERY_UNCONFIGURED,
            "destino, revisão ou finalidade não autorizados; confira a configuração e solicite repetição explícita",
        )
    if deps.cipher is None:
        raise RetryableError(
            FailureCode.DELIVERY_UNCONFIGURED,
            "o segredo do destino não abre com WEBHOOK_ENCRYPTION_KEY",
        )
    try:
        secret = deps.cipher.open(row.secret_sealed)
    except destinations.UnreadableSecret:
        raise RetryableError(
            FailureCode.DELIVERY_UNCONFIGURED,
            "o segredo do destino não abre com WEBHOOK_ENCRYPTION_KEY",
        ) from None
    return destinations.Destination(
        scope="default" if row.form_slug is None else "form", url=row.url, secret=secret
    )


def set_delivery_run_id(
    deps: Deps, delivery_id: uuid.UUID, run_id: str, dispatch_no: int = 1
) -> None:
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.dispatch_no == dispatch_no,
                Delivery.status == "pending",
            )
            .values(hatchet_run_id=run_id)
        )


def mark_delivery_failed(
    deps: Deps, delivery_id: uuid.UUID, code: str, dispatch_no: int = 1
) -> int:
    """Callback antigo nunca altera uma repetição nova nem um sucesso."""
    with deps.sessions.begin() as session:
        result = session.execute(
            sa.update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "pending",
                Delivery.dispatch_no == dispatch_no,
            )
            .values(status="failed", last_error=sa.func.coalesce(Delivery.last_error, code))
        )

        return result.rowcount


def lead_links(deps: Deps, job: Job, *, pdf_attempt: int | None = None) -> dict[str, str]:
    token = deps.signer.issue(job.public_id, job.token_version)
    base = deps.settings.public_base_url.rstrip("/")
    suffix = f"&attempt={pdf_attempt}" if pdf_attempt is not None else ""
    return {
        "status_url": f"{base}/v1/jobs/{job.public_id}?t={token}",
        "pdf_url": f"{base}/v1/jobs/{job.public_id}/pdf?t={token}{suffix}",
        # Página do lead; o `t` vira cookie no primeiro acesso (app.web.public).
        "page_url": f"{base}/d/{job.public_id}?t={token}",
    }


def build_payload(session: Any, deps: Deps, delivery: Delivery) -> dict[str, Any]:
    job = session.get_one(Job, delivery.job_id)
    submission = session.get_one(Submission, job.submission_id)
    artifact = session.get_one(Artifact, delivery.artifact_id)
    if (
        artifact.job_id != job.id
        or artifact.version != job.attempt
        or (
            delivery.authorization_origin == "explicit"
            and artifact.version != delivery.authorized_attempt
        )
    ):
        raise TerminalError(
            FailureCode.DELIVERY_REJECTED, "entrega de tentativa anterior não é atual"
        )
    plan = session.get_one(Plan, artifact.plan_id)
    links = lead_links(deps, job, pdf_attempt=artifact.version)
    direct_url = (
        None  # a retenção já apagou o arquivo (reenvio feito muito depois)
        if artifact.version != job.attempt or not current_pdf(session, deps.storage, job).available
        else deps.storage.presigned_url(
            artifact.storage_key,
            ttl_seconds=int(DIRECT_URL_TTL.total_seconds()),
            filename=f"plano-{job.public_id}.pdf",
        )
    )
    common = {
        "event": delivery.event,
        "event_id": str(delivery.id),
        "occurred_at": utcnow().isoformat(),
        "form": {"slug": submission.form_slug, "version": submission.form_version},
        "job": {
            "public_id": job.public_id,
            "attempt": artifact.version,
            "status_url": links["status_url"],
            "page_url": links["page_url"],
        },
        "pdf": {
            "url": links["pdf_url"],
            "expires_at": job.expires_at.isoformat(),
            "direct_url": direct_url,
            "direct_url_expires_in": int(DIRECT_URL_TTL.total_seconds()) if direct_url else None,
            "sha256": artifact.sha256,
            "size_bytes": artifact.byte_size,
        },
    }
    if delivery.audience == "participant":
        from app.forms import PlanProjectionError

        try:
            common["plan"] = _form_of(deps, submission).project_plan(
                plan.document, audience="participant"
            )
        except (PlanProjectionError, TerminalError):
            raise TerminalError(
                FailureCode.DELIVERY_REJECTED,
                "projeção participante indisponível para a versão histórica",
            ) from None
        common["audience"] = "participant"
        common["form"]["version_number"] = submission.form_version_number
        return common
    lead = resolve_identity(deps.forms, submission)
    common.update(
        {
            "lead": {"email": lead.email, "name": lead.name, "phone": lead.phone},
            "answers": submission.answers,
            "consent": submission.consent,
            "tracking": submission.tracking,
            "scores": submission.scores,
            "priority": submission.priority,
            "persona": submission.persona,
            "plan": plan.document,
        }
    )
    return common


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
