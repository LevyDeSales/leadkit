"""Fronteira comum antes de guardar os formatos da capacidade composta 2."""

from collections.abc import Mapping

from sqlalchemy.orm import Session

from app.forms.definition.document import DefinitionError, Problem
from app.integration_readiness import configuration_lock
from app.models import HandoffReceiver


def assert_definition_contract(session: Session, document: Mapping) -> None:
    """Confere o documento original, antes de omitir padrões ou adquirir locks de formulário."""
    completion = document.get("completion")
    has_handoff = isinstance(completion, Mapping) and "handoff" in completion
    if "release" not in document and not has_handoff:
        return
    from app.deployment_contract import assert_contract

    configuration_lock(session, shared=True)
    assert_contract(session, required=2)
    handoff = completion.get("handoff") if isinstance(completion, Mapping) else None
    if not handoff:
        return
    import uuid

    import sqlalchemy as sa

    try:
        receiver_id = uuid.UUID(str(handoff["receiver_id"]))
    except (ValueError, KeyError, TypeError):
        raise DefinitionError(
            [Problem("completion.handoff", "informe o receptor autorizado")]
        ) from None
    receiver = session.scalar(sa.select(HandoffReceiver).where(HandoffReceiver.id == receiver_id))
    if (
        receiver is None
        or receiver.form_slug != document.get("slug")
        or receiver.revision != handoff.get("receiver_revision")
        or receiver.entry_url != completion.get("url")
    ):
        raise DefinitionError(
            [
                Problem(
                    "completion.handoff",
                    "o receptor, revisão e endereço devem corresponder ao formulário",
                )
            ]
        )
