"""Comandos nomeados de gestão; os DTOs operacionais não contêm dados da participante."""

from app.admin.api.common import Page
from app.handoff_types import (
    CreateCredential,
    IntegrationCredentialInfo,
    Revoke,
    RotateCredential,
    SaveReceiver,
    SetReceiverEnabled,
)


class SaveHandoffReceiverRequest(SaveReceiver):
    """Salvar pausa o receptor; não envia dados ou publica formulário."""


class SetHandoffReceiverEnabledRequest(SetReceiverEnabled):
    """Habilitar autoriza somente referências futuras da revisão observada."""


class CreateIntegrationCredentialRequest(CreateCredential):
    """Escolha expires_at explicitamente; null significa sem vencimento."""


class RotateIntegrationCredentialRequest(RotateCredential):
    """Revoga a anterior atomicamente e mostra a sucessora uma vez."""


class RevokeIntegrationCredentialRequest(Revoke):
    """Revogação permanente da capacidade de serviço."""


class RevokeSubmissionHandoffRequest(Revoke):
    """Revoga a referência e remove sua cópia cifrada."""


class IntegrationCredentialPage(Page[IntegrationCredentialInfo]):
    """Uma página de credenciais, sem hashes ou segredos."""
