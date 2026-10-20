"""Finalidade do receptor: a escolha interna nunca nasce de uma omissão."""

from typing import Literal

Purpose = Literal["internal", "participant"]


class InvalidPurpose(ValueError):
    """Erro corrigível na finalidade do receptor, sem endereço ou dados da participante."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def selected(purpose: str | None, *, current=None, moved=False) -> tuple[str, str]:
    if purpose is not None and purpose not in ("internal", "participant"):
        raise InvalidPurpose("Selecione a finalidade interna ou participante.")
    if current is not None and purpose is None:
        if moved:
            raise InvalidPurpose(
                "Ao mudar a URL, selecione explicitamente a finalidade do receptor."
            )
        return current.purpose, current.purpose_origin
    return purpose or "participant", "explicit" if purpose is not None else "default"


def require_internal(purpose: str, *, receiver: str) -> None:
    if purpose != "internal":
        raise InvalidPurpose(
            f"{receiver} exige finalidade interna: envia respostas e dados operacionais. "
            "Escolha um destino interno autorizado ou remova a integração efetiva, conferindo o padrão herdado."
        )


def require_participant_capability(session) -> None:
    """Consulta o piso e a atestação persistidos na mesma transação da autorização."""
    from app.deployment_contract import DeploymentContractError, assert_participant_contract

    try:
        assert_participant_contract(session)
    except DeploymentContractError:
        raise InvalidPurpose(
            "A finalidade participante aguarda habilitação de todas as versões da API e das workers. "
            "Peça à administradora para concluir a verificação de compatibilidade."
        ) from None
