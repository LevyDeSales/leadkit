"""Desfechos de confirmação, independentes do protocolo e do SDK MCP."""

from enum import Enum


class ConfirmationOutcome(Enum):
    APPROVED = "approved"
    UNCHECKED = "unchecked"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed_out"
    IN_PROGRESS = "in_progress"


BLOCKED_MESSAGES = {
    ConfirmationOutcome.UNCHECKED: "O cliente não confirmou: devolveu a caixa desmarcada.",
    ConfirmationOutcome.DECLINED: "O cliente não confirmou: informou recusa (`decline`).",
    ConfirmationOutcome.CANCELLED: "O cliente não confirmou: informou cancelamento (`cancel`).",
    ConfirmationOutcome.INVALID: "O cliente não confirmou: devolveu uma resposta inválida ou incompleta.",
    ConfirmationOutcome.UNAVAILABLE: "A confirmação pelo cliente está indisponível.",
    ConfirmationOutcome.TIMED_OUT: "O tempo de espera da confirmação pelo cliente terminou.",
}


def blocked_message(outcome: ConfirmationOutcome) -> str:
    """Descreve somente o resultado observado, sem atribuir interação humana ao cliente."""
    if outcome is ConfirmationOutcome.IN_PROGRESS:
        return (
            "Já há uma tentativa desta mesma chamada em andamento. Aguarde seu resultado e confira "
            "o estado antes de repetir; o efeito da tentativa em andamento ainda não é conhecido."
        )
    return BLOCKED_MESSAGES[outcome] + " A operação solicitada não foi executada."
