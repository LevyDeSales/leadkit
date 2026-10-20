"""Ações compartilhadas HTML/API/MCP sobre entregas de captura."""

import uuid

from app.capture_deliveries import _locked, configuration, state
from app.models import AuditLog, utcnow
from app.submission_identity import submission_form


class Missing(Exception):
    """Captura ou entrega inexistente."""


class Conflict(Exception):
    """Estado, revisão ou finalidade mudou; confira a entrega antes de repetir."""


def retry(deps, submission_id, delivery_id, *, expected_dispatch, expected_config_version, actor):
    """Reautoriza somente o mesmo destino, na revisão confirmada, sem backfill ou geração."""
    try:
        submission_id, delivery_id = uuid.UUID(str(submission_id)), uuid.UUID(str(delivery_id))
    except ValueError:
        raise Missing from None
    with deps.sessions.begin() as session:
        submission, delivery = _locked(session, delivery_id)
        if delivery is None or submission.id != submission_id:
            raise Missing
        if delivery.dispatch_no != expected_dispatch:
            raise Conflict
        if delivery.status == "succeeded":
            return state(delivery, delivery.config_version)
        if delivery.status != "failed":
            raise Conflict
        # Conferir a revisão solicitada antes de aplicar; configuration revalida o perfil histórico.
        delivery.config_version = expected_config_version
        row, problem = configuration(session, delivery, submission_form(deps.forms, submission))
        if problem or row is None:
            raise Conflict
        delivery.dispatch_no += 1
        delivery.hatchet_run_id = None
        delivery.status = "pending"
        delivery.last_error = None
        delivery.updated_at = utcnow()
        session.add(
            AuditLog(
                actor=actor,
                action="capture_delivery.retry",
                target_type="capture_delivery",
                target_id=str(delivery.id),
                details={
                    "submission_id": str(submission.id),
                    "dispatch_no": delivery.dispatch_no,
                    "config_version": delivery.config_version,
                },
            )
        )
        return state(delivery, row.version)
