"""Gestão equivalente a HTML e MCP, sempre pelos mesmos serviços transacionais."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from app import handoff_receivers, handoffs, integration_credentials
from app.admin.api.common import PageParams, now, page_params, paginate
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.handoffs import (
    CreateIntegrationCredentialRequest,
    IntegrationCredentialPage,
    RevokeIntegrationCredentialRequest,
    RevokeSubmissionHandoffRequest,
    RotateIntegrationCredentialRequest,
    SaveHandoffReceiverRequest,
    SetHandoffReceiverEnabledRequest,
)
from app.admin.api.security import CurrentKey
from app.deployment_contract import DeploymentContractError
from app.deps import get_deps
from app.destinations import SecretCipher
from app.handoff_types import (
    CreatedIntegrationCredential,
    HandoffError,
    HandoffInfo,
    HandoffReceiverInfo,
    IntegrationCredentialInfo,
)

router = OperationRouter(
    tag="Encaminhamentos",
    description="Receptores e credenciais de serviço para continuidade opcional, sem acesso a respostas ou PDF.",
)


def execute(action):
    try:
        with get_deps().sessions.begin() as session:
            result = action(session)
        return result
    except HandoffError as error:
        content = {
            "code": error.code,
            "detail": "A operação não foi aplicada. Confira a versão e a configuração.",
        }
        if error.field:
            content["errors"] = [{"field": error.field, "message": "Confira este campo."}]
        if error.current_version is not None:
            content["current_version"] = error.current_version
        if error.credential_id:
            content["credential_id"] = str(error.credential_id)
        return JSONResponse(content, status_code=error.status)
    except DeploymentContractError:
        return JSONResponse(
            {"code": "unavailable", "detail": "A atualização ainda não habilitou esta operação."},
            status_code=503,
            headers={"Retry-After": "5"},
        )


@router.operation(
    "GET",
    "/forms/{slug}/handoff-receiver",
    operation_id="get_handoff_receiver",
    role="leitura",
    summary="Consultar receptor de encaminhamento",
    description="Consulta configuração operacional, sem emitir referência.",
    returns="O receptor configurado.",
    response_model=HandoffReceiverInfo,
)
def get_handoff_receiver(slug: str):
    return execute(lambda session: handoff_receivers.get_receiver(session, slug))


@router.operation(
    "PUT",
    "/forms/{slug}/handoff-receiver",
    operation_id="save_handoff_receiver",
    role="total",
    external_effect=True,
    summary="Salvar receptor de encaminhamento",
    description="Salva e pausa a configuração. Uma mudança de endereço ou prazo cria revisão nova; não reendereça referências históricas nem publica formulário.",
    returns="A configuração pausada.",
    response_model=HandoffReceiverInfo,
    errors={409: "version_conflict: a versão observada mudou; nada foi salvo."},
)
def save_handoff_receiver(
    slug: str, body: SaveHandoffReceiverRequest, key: CurrentKey, request: Request
):
    return execute(
        lambda session: handoff_receivers.save_receiver(
            session, slug, command=body, actor=key.actor, now=now(request)
        )
    )


@router.operation(
    "POST",
    "/forms/{slug}/handoff-receiver/enabled",
    operation_id="set_handoff_receiver_enabled",
    role="total",
    external_effect=True,
    summary="Habilitar ou pausar encaminhamento",
    description="Autoriza uso futuro da revisão quando há cifra e credencial válidas; não faz transporte externo.",
    returns="O estado confirmado.",
    response_model=HandoffReceiverInfo,
    errors={
        409: "version_conflict: a versão mudou; state_conflict: o receptor ainda não está pronto."
    },
)
def set_handoff_receiver_enabled(
    slug: str, body: SetHandoffReceiverEnabledRequest, key: CurrentKey, request: Request
):
    return execute(
        lambda session: handoff_receivers.set_receiver_enabled(
            session,
            slug,
            command=body,
            actor=key.actor,
            cipher=SecretCipher.from_settings(get_deps().settings),
            now=now(request),
        )
    )


@router.operation(
    "GET",
    "/forms/{slug}/handoff-receiver/credentials",
    operation_id="list_integration_credentials",
    role="leitura",
    summary="Listar credenciais do receptor",
    description="Lista metadados de credenciais, inclusive revogadas, sem segredo ou hash.",
    returns="Uma página de credenciais.",
    response_model=IntegrationCredentialPage,
)
def list_integration_credentials(
    slug: str, request: Request, page: Annotated[PageParams, Depends(page_params)]
):
    def action(session):
        infos = integration_credentials.list_credentials(session, slug, now=now(request))
        items, cursor = paginate(infos, page, key=lambda item: str(item.id))
        return IntegrationCredentialPage(items=items, next_cursor=cursor)

    return execute(action)


@router.operation(
    "POST",
    "/forms/{slug}/handoff-receiver/credentials",
    operation_id="create_integration_credential",
    role="total",
    external_effect=True,
    summary="Criar credencial de serviço",
    description="Mostra o segredo utilizável uma única vez. expires_at é obrigatório e nullable: null permite sem vencimento. Repetir idempotency_key devolve409 credential_secret_already_issued com ID seguro, sem segredo; conteúdo divergente conflita. Se perder a resposta, revogue ou gire explicitamente.",
    returns="Credencial e segredo uma única vez.",
    response_model=CreatedIntegrationCredential,
    status_code=201,
    errors={
        409: "version_conflict: versão obsoleta; state_conflict: revisão incompatível; idempotency_conflict ou credential_secret_already_issued: comando já registrado."
    },
)
def create_integration_credential(
    slug: str, body: CreateIntegrationCredentialRequest, key: CurrentKey, request: Request
):
    return execute(
        lambda session: integration_credentials.create_credential(
            session, slug, command=body, actor=key.actor, now=now(request)
        )
    )


@router.operation(
    "POST",
    "/integration-credentials/{credential_id}/rotate",
    operation_id="rotate_integration_credential",
    role="total",
    external_effect=True,
    summary="Girar credencial de serviço",
    description="Cria sucessora na mesma revisão e revoga a anterior no mesmo commit. O segredo aparece uma vez; repetição nunca o reemite. Escolha o vencimento novamente, inclusive null.",
    returns="Sucessora e segredo uma vez.",
    response_model=CreatedIntegrationCredential,
    errors={
        409: "version_conflict: versão obsoleta; state_conflict: revisão incompatível; idempotency_conflict ou credential_secret_already_issued: comando já registrado."
    },
)
def rotate_integration_credential(
    credential_id: UUID, body: RotateIntegrationCredentialRequest, key: CurrentKey, request: Request
):
    return execute(
        lambda session: integration_credentials.rotate_credential(
            session, credential_id, command=body, actor=key.actor, now=now(request)
        )
    )


@router.operation(
    "POST",
    "/integration-credentials/{credential_id}/revoke",
    operation_id="revoke_integration_credential",
    role="total",
    destructive=True,
    external_effect=True,
    summary="Revogar credencial de serviço",
    description="Revoga permanentemente a capacidade de serviço; confirmação é obrigatória. Repetição devolve a revogação já registrada.",
    returns="Metadados sem segredo.",
    response_model=IntegrationCredentialInfo,
    errors={409: "version_conflict: a versão observada mudou."},
)
def revoke_integration_credential(
    credential_id: UUID, body: RevokeIntegrationCredentialRequest, key: CurrentKey, request: Request
):
    return execute(
        lambda session: integration_credentials.revoke_credential(
            session, credential_id, command=body, actor=key.actor, now=now(request)
        )
    )


@router.operation(
    "GET",
    "/submissions/{submission_id}/handoff",
    operation_id="get_submission_handoff",
    role="leitura",
    summary="Consultar intenção de encaminhamento",
    description="Metadados operacionais; não recupera referência, endereço completo, contato ou respostas.",
    returns="A intenção histórica sem segredo.",
    response_model=HandoffInfo,
)
def get_submission_handoff(submission_id: UUID):
    return execute(lambda session: handoffs.get_handoff(session, submission_id))


@router.operation(
    "POST",
    "/submissions/{submission_id}/handoff/revoke",
    operation_id="revoke_submission_handoff",
    role="total",
    destructive=True,
    external_effect=True,
    summary="Revogar encaminhamento",
    description="Revoga a referência permanentemente e remove a cifra. Não apaga respostas nem altera autorização do PDF.",
    returns="Metadados da intenção revogada.",
    response_model=HandoffInfo,
    errors={409: "version_conflict: a versão observada mudou."},
)
def revoke_submission_handoff(
    submission_id: UUID, body: RevokeSubmissionHandoffRequest, key: CurrentKey, request: Request
):
    return execute(
        lambda session: handoffs.revoke_handoff(
            session, submission_id, command=body, actor=key.actor, now=now(request)
        )
    )
