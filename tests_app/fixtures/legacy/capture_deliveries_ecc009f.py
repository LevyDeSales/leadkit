"""Outbox de capturas persistidas, independente do pipeline de PDF."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from app.models import CaptureDelivery, Integration, Submission, WebhookDestination

EVENT = "submissao.persistida"


def enqueue(session, submission: Submission) -> None:
    """Fixa destinos existentes no aceite, incluindo configurações incompatíveis rastreáveis."""
    from app.integration_readiness import configuration_lock

    configuration_lock(session, shared=True)
    integrations = session.scalars(
        sa.select(Integration).where(
            Integration.form_slug == submission.form_slug, Integration.active.is_(True)
        )
    )
    for integration in integrations:
        session.execute(
            insert(CaptureDelivery)
            .values(
                submission_id=submission.id,
                integration_id=integration.id,
                event=EVENT,
                config_version=integration.version,
            )
            .on_conflict_do_nothing()
        )
    destination = session.scalar(
        sa.select(WebhookDestination)
        .where(
            sa.or_(
                WebhookDestination.form_slug == submission.form_slug,
                WebhookDestination.form_slug.is_(None),
            )
        )
        .order_by(WebhookDestination.form_slug.is_(None))
        .limit(1)
    )
    if destination:
        session.execute(
            insert(CaptureDelivery)
            .values(
                submission_id=submission.id,
                webhook_destination_id=destination.id,
                event=EVENT,
                config_version=destination.version,
            )
            .on_conflict_do_nothing()
        )


def pending_for_submission(session, submission_id):
    """IDs e epochs suficientes para disparar depois do commit."""
    return session.execute(
        sa.select(CaptureDelivery.id, CaptureDelivery.dispatch_no).where(
            CaptureDelivery.submission_id == submission_id,
            CaptureDelivery.status == "pending",
            CaptureDelivery.hatchet_run_id.is_(None),
        )
    ).all()


def _locked(session, delivery_id):
    """Ordem única: submissão, entrega e configuração; a purga usa a mesma primeira trava."""
    from app.integration_readiness import configuration_lock

    configuration_lock(session, shared=True)
    submission_id = session.scalar(
        sa.select(CaptureDelivery.submission_id).where(CaptureDelivery.id == delivery_id)
    )
    if submission_id is None:
        return None, None
    submission = session.scalar(
        sa.select(Submission).where(Submission.id == submission_id).with_for_update()
    )
    if submission is None:
        return None, None
    delivery = session.scalar(
        sa.select(CaptureDelivery).where(CaptureDelivery.id == delivery_id).with_for_update()
    )
    return submission, delivery


def configuration(session, delivery, spec):
    """Valida a revisão e finalidade locais sem escolher outro destino."""
    if delivery.integration_id:
        from app.integrations.mapping import catalog, compatible, entries_of, key_entry

        row = session.scalar(
            sa.select(Integration)
            .where(Integration.id == delivery.integration_id)
            .with_for_update()
        )
        if row is None or not row.active:
            return row, "configuration_inactive"
        if row.purpose != "internal":
            return row, "configuration_incompatible"
        if row.version != delivery.config_version:
            return row, "configuration_changed"
        entries = entries_of(row.capture_mapping)
        try:
            sources = catalog(spec, profile="capture")
            if any(
                e.source not in sources or not compatible(sources[e.source], e.type)
                for e in entries
            ):
                return row, "configuration_incompatible"
            key = key_entry(entries, profile="capture")
            if key is None or key.field_id != row.capture_key_field_id:
                return row, "configuration_incompatible"
        except Exception:  # noqa: BLE001 — falha isolada, sem mensagem bruta.
            return row, "configuration_incompatible"
        return row, None
    row = session.scalar(
        sa.select(WebhookDestination)
        .where(WebhookDestination.id == delivery.webhook_destination_id)
        .with_for_update()
    )
    if row is None:
        return row, "configuration_removed"
    if row.version != delivery.config_version:
        return row, "configuration_changed"
    if row.purpose != "internal":
        return row, "configuration_incompatible"
    if EVENT not in row.events:
        return row, "configuration_incompatible"
    return row, None


def dispatch(deps, delivery_id, dispatch_no):
    """Entrega com transporte limitado e trava até a liquidação; duplicatas e epochs antigos não enviam."""
    from dataclasses import asdict
    from datetime import timedelta

    from app.errors import FailureCode, RetryableError, TerminalError
    from app.models import Form, utcnow
    from app.submission_identity import identity_from_answers, submission_form

    retry = False
    with deps.sessions.begin() as session:
        session.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        submission, delivery = _locked(session, delivery_id)
        if delivery is None or delivery.status == "succeeded":
            return "already_succeeded"
        if delivery.dispatch_no != dispatch_no or delivery.status != "pending":
            return "obsolete"
        delivery.attempts += 1
        delivery.updated_at = utcnow()
        days = (
            session.scalar(sa.select(Form.retention_days).where(Form.slug == submission.form_slug))
            or deps.settings.retention_days
        )
        if submission.created_at + timedelta(days=days) <= utcnow():
            delivery.status, delivery.last_error = "failed", "retention_expired"
            return "failed"
        try:
            spec = submission_form(deps.forms, submission)
            row, problem = configuration(session, delivery, spec)
            if problem:
                delivery.status, delivery.last_error = "failed", problem
                return "failed"
            identity = identity_from_answers(submission, spec)
            if delivery.webhook_destination_id:
                if deps.cipher is None:
                    raise TerminalError(
                        FailureCode.DELIVERY_UNCONFIGURED, "configuração indisponível"
                    )
                payload = {
                    "event": EVENT,
                    "event_id": str(delivery.id),
                    "occurred_at": submission.created_at.isoformat(),
                    "form": {
                        "slug": submission.form_slug,
                        "version": submission.form_version,
                        "version_number": submission.form_version_number,
                    },
                    "submission": {"id": str(submission.id)},
                    "lead": asdict(identity),
                    "answers": submission.answers,
                    "consent": submission.consent,
                    "tracking": submission.tracking,
                    "scores": submission.scores,
                    "priority": submission.priority,
                    "persona": submission.persona,
                }
                delivery.response_status = deps.webhook.send(
                    row.url, deps.cipher.open(row.secret_sealed), EVENT, str(delivery.id), payload
                )
                if (
                    not isinstance(delivery.response_status, int)
                    or not 200 <= delivery.response_status < 300
                ):
                    raise TerminalError(
                        FailureCode.DELIVERY_REJECTED, "transporte sem evidência de entrega"
                    )
            else:
                from app.integrations.config import open_connection
                from app.integrations.mapping import CaptureRowData, entries_of, questions_of
                from app.integrations.push import upsert

                data = CaptureRowData(
                    submission_id=str(submission.id),
                    form_version=submission.form_version,
                    lead_name=identity.name,
                    lead_email=identity.email,
                    lead_phone=identity.phone,
                    priority=submission.priority,
                    persona=submission.persona,
                    answers=submission.answers,
                    consent=submission.consent,
                    tracking=submission.tracking,
                    scores=submission.scores,
                    questions={question.name: question for question in questions_of(spec)},
                )
                written = upsert(
                    deps,
                    open_connection(deps.cipher, row),
                    integration_id=row.id,
                    key_field_id=row.capture_key_field_id,
                    entries=entries_of(row.capture_mapping),
                    data=data,
                    record_key=str(submission.id),
                    profile="capture",
                    session=session,
                )
                if (
                    not isinstance(written.row_id, int)
                    or written.row_id < 1
                    or written.outcome not in ("created", "updated")
                ):
                    raise TerminalError(
                        FailureCode.INTEGRATION_REJECTED, "transporte sem evidência de gravação"
                    )
                delivery.row_id, delivery.outcome = written.row_id, written.outcome
        except Exception as exc:  # noqa: BLE001 — fronteira de provedor; somente código seguro sai.
            # Só códigos definidos pelo domínio: nem mensagens, nomes remotos ou respostas do provedor.
            delivery.last_error = (
                exc.code.value
                if isinstance(exc, RetryableError | TerminalError)
                else "transport_failed"
            )
            retry = not isinstance(exc, TerminalError)
            delivery.status = "pending" if retry else "failed"
        else:
            delivery.status = "succeeded"
            delivery.last_error = None
            delivery.delivered_at = utcnow()
    if retry:
        raise RetryableError(
            FailureCode.DELIVERY_UNAVAILABLE, "entrega de captura pendente"
        ) from None
    return delivery.status


def set_run_id(session, delivery_id, dispatch_no, run_id):
    """Callback do trigger condicionado ao epoch; nunca ressuscita sucesso ou dado excluído."""
    session.execute(
        sa.update(CaptureDelivery)
        .where(
            CaptureDelivery.id == delivery_id,
            CaptureDelivery.dispatch_no == dispatch_no,
            CaptureDelivery.status == "pending",
            CaptureDelivery.hatchet_run_id.is_(None),
        )
        .values(hatchet_run_id=run_id)
    )


def mark_failed(deps, delivery_id, dispatch_no):
    """Falha terminal do engine, sem sobrescrever outro dispatch nem transporte concluído."""
    with deps.sessions.begin() as session:
        _, delivery = _locked(session, delivery_id)
        if delivery and delivery.dispatch_no == dispatch_no and delivery.status == "pending":
            delivery.status = "failed"
            delivery.last_error = delivery.last_error or "engine_failed"
            return 1
    return 0


def reconcile(deps, *, run_state, launch, unlaunched_after, in_flight_after, limit):
    """Recupera inclusive run perdido; a observação de engine não prova recebimento."""
    from app.models import utcnow

    with deps.sessions() as session:
        pending = session.execute(
            sa.select(
                CaptureDelivery.id,
                CaptureDelivery.dispatch_no,
                CaptureDelivery.hatchet_run_id,
                CaptureDelivery.updated_at,
            )
            .where(
                CaptureDelivery.status == "pending",
                CaptureDelivery.updated_at < utcnow() - unlaunched_after,
            )
            .order_by(CaptureDelivery.updated_at)
            .limit(limit)
        ).all()
    launched = relaunched = failed = errors = 0
    for delivery_id, epoch, run_id, updated_at in pending:
        if run_id and updated_at > utcnow() - in_flight_after:
            continue
        try:
            state = run_state(run_id) if run_id else None
        except Exception:  # noqa: BLE001 — falha isolada, sem mensagem bruta.
            errors += 1
            continue
        if state == "active":
            continue
        with deps.sessions.begin() as session:
            _, delivery = _locked(session, delivery_id)
            if (
                delivery is None
                or delivery.status != "pending"
                or delivery.dispatch_no != epoch
                or delivery.hatchet_run_id != run_id
            ):
                continue
            if state in ("succeeded", "failed"):
                delivery.status, delivery.last_error = "failed", "engine_unsettled"
                failed += 1
                continue
            # Também cerca o run perdido entre o SDK e a gravação de seu ID.
            delivery.dispatch_no += 1
            delivery.hatchet_run_id = None
            delivery.updated_at = utcnow()
            epoch = delivery.dispatch_no
        if launch(delivery_id, epoch) is None:
            errors += 1
        elif run_id:
            relaunched += 1
        else:
            launched += 1
    return launched, relaunched, failed, errors


def listing(session, *, submission_id=None, integration_id=None, limit=100):
    """Estado seguro por captura/integração, sem tocar respostas nem segredo do destino."""
    query = (
        sa.select(
            CaptureDelivery,
            sa.func.coalesce(Integration.version, WebhookDestination.version).label(
                "current_config_version"
            ),
        )
        .outerjoin(Integration, Integration.id == CaptureDelivery.integration_id)
        .outerjoin(
            WebhookDestination, WebhookDestination.id == CaptureDelivery.webhook_destination_id
        )
    )
    if submission_id is not None:
        query = query.where(CaptureDelivery.submission_id == submission_id)
    if integration_id is not None:
        query = query.where(CaptureDelivery.integration_id == integration_id)
    return [
        state(row, current)
        for row, current in session.execute(
            query.order_by(CaptureDelivery.created_at, CaptureDelivery.id).limit(limit)
        )
    ]


def state(row, current_config_version):
    return {
        "delivery_id": str(row.id),
        "submission_id": str(row.submission_id),
        "destination_id": str(row.integration_id or row.webhook_destination_id),
        "kind": "baserow" if row.integration_id else "webhook",
        "event": row.event,
        "config_version": row.config_version,
        "current_config_version": current_config_version,
        "status": row.status,
        "attempts": row.attempts,
        "dispatch_no": row.dispatch_no,
        "error_code": row.last_error,
        "response_status": row.response_status,
        "row_id": row.row_id,
        "outcome": row.outcome,
        "delivered_at": row.delivered_at,
    }


@dataclass(frozen=True)
class Counts:
    """Intenções locais por estado, sem ler respostas da captura."""

    pending: int = 0
    succeeded: int = 0
    failed: int = 0

    @property
    def total(self):
        return self.pending + self.succeeded + self.failed


def counts(session, *, integration_id=None, webhook_destination_id=None):
    """Inventário de uma conexão antes de removê-la; a ação serializa com novos aceites."""
    if integration_id is None and webhook_destination_id is None:
        return Counts()
    column, identifier = (
        (CaptureDelivery.integration_id, integration_id)
        if integration_id
        else (CaptureDelivery.webhook_destination_id, webhook_destination_id)
    )
    values = dict(
        session.execute(
            sa.select(CaptureDelivery.status, sa.func.count())
            .where(column == identifier)
            .group_by(CaptureDelivery.status)
        ).all()
    )
    return Counts(**values)
