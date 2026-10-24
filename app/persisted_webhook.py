"""Contrato histórico do webhook de respostas, independente de geração ou acesso ao PDF."""

from copy import deepcopy
from typing import Any, Literal
from uuid import UUID

from app.models import HandoffReference, Submission

EVENT = "submissao.persistida"
V2_SELECTION = "submissao.persistida.v2"


def build_payload(
    *,
    delivery_id: UUID,
    issuer: UUID,
    submission: Submission,
    generation: Literal["none", "pdf"],
    destination_id: UUID,
    destination_revision: int,
    correlation: HandoffReference | None,
) -> dict[str, Any]:
    """Copia fatos do aceite; não consulta estado atual nem concede capacidade de PDF."""
    payload = {
        "schema_version": 2,
        "event": EVENT,
        "event_id": str(delivery_id),
        "issuer": f"leadkit:{issuer}",
        "occurred_at": submission.created_at.isoformat(),
        "form": {
            "slug": submission.form_slug,
            "version": submission.form_version,
            "version_number": submission.form_version_number,
        },
        "submission": {"id": str(submission.id), "submitted_at": submission.created_at.isoformat()},
        "generation": generation,
        "delivery_authorization": {
            "destination_id": str(destination_id),
            "revision": destination_revision,
            "purpose": "internal",
        },
        "answers": deepcopy(submission.answers),
        "consent": deepcopy(submission.consent),
    }
    if correlation is not None:
        payload["correlation"] = {
            "reference_id": str(correlation.id),
            "receiver_id": str(correlation.receiver_id),
            "receiver_revision": correlation.receiver_revision,
        }
    return payload
