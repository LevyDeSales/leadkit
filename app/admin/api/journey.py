"""Gestão e consulta de eventos compartilhadas com o painel e projetadas para MCP."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Query, Request

from app import event_sources, journey
from app.admin.api.common import Page, PageParams, now, page_params, paginate
from app.admin.api.handoffs import execute
from app.admin.api.jobs import INCLUDE_LEAD_DATA
from app.admin.api.operations import OperationRouter
from app.admin.api.security import CurrentKey, ensure_role
from app.handoff_types import Revoke
from app.journey_types import (
    CreatedEventCredential,
    CreateEventCredential,
    CreateEventSource,
    ErasedJourney,
    EraseJourney,
    EventCredentialInfo,
    EventSourceInfo,
    EventType,
    JourneyEventInfo,
    RotateEventCredential,
    SaveEventSource,
    SetEventSourceEnabled,
    SubmissionJourney,
)

router = OperationRouter(
    tag="Jornada externa",
    description="Autoridade de fontes e declarações externas; não libera PDF nem opera agenda ou mensagens.",
)


class EventSourcePage(Page[EventSourceInfo]):
    pass


class EventCredentialPage(Page[EventCredentialInfo]):
    pass


class JourneyEventPage(Page[JourneyEventInfo]):
    pass


@router.operation(
    "GET",
    "/forms/{slug}/event-sources",
    operation_id="list_event_sources",
    role="leitura",
    summary="Listar fontes de eventos",
    description="Lista fontes e suas permissões históricas, inclusive pausadas.",
    returns="Uma página de fontes.",
    response_model=EventSourcePage,
)
def list_event_sources(slug: str, page: Annotated[PageParams, Depends(page_params)]):
    def action(session):
        items, cursor = paginate(
            event_sources.list_sources(session, slug), page, key=lambda item: str(item.id)
        )
        return EventSourcePage(items=items, next_cursor=cursor)

    return execute(action)


@router.operation(
    "GET",
    "/event-sources/{source_id}",
    operation_id="get_event_source",
    role="leitura",
    summary="Consultar fonte de eventos",
    description="Consulta vínculo histórico e autoridade concedida; não ativa a fonte.",
    returns="A fonte.",
    response_model=EventSourceInfo,
)
def get_event_source(source_id: UUID):
    return execute(
        lambda session: event_sources.source_info(event_sources.source_row(session, source_id))
    )


@router.operation(
    "POST",
    "/forms/{slug}/event-sources",
    operation_id="create_event_source",
    role="total",
    external_effect=True,
    summary="Criar fonte de eventos",
    description="Cria pausada para a revisão atual do receptor. expected_version deve ser0; idempotency_key no corpo recupera criação exata, sem habilitar. O namespace e vínculo são imutáveis.",
    returns="A fonte pausada criada ou sua configuração atual em replay.",
    response_model=EventSourceInfo,
    status_code=201,
    errors={409: "version_conflict, namespace_conflict ou idempotency_conflict."},
)
def create_event_source(slug: str, body: CreateEventSource, key: CurrentKey, request: Request):
    return execute(
        lambda session: event_sources.create_source(
            session, slug, command=body, actor=key.actor, now=now(request)
        )
    )


@router.operation(
    "PUT",
    "/event-sources/{source_id}",
    operation_id="update_event_source",
    role="total",
    external_effect=True,
    summary="Alterar autoridade da fonte",
    description="Mudar permissões incrementa a revisão, pausa a fonte e invalida credenciais anteriores. Renomear conserva a revisão. Não transfere referências históricas.",
    returns="A configuração confirmada.",
    response_model=EventSourceInfo,
    errors={409: "version_conflict: releia a fonte."},
)
def update_event_source(source_id: UUID, body: SaveEventSource, key: CurrentKey, request: Request):
    return execute(
        lambda session: event_sources.update_source(
            session, source_id, command=body, actor=key.actor, now=now(request)
        )
    )


@router.operation(
    "POST",
    "/event-sources/{source_id}/enabled",
    operation_id="set_event_source_enabled",
    role="total",
    external_effect=True,
    summary="Habilitar ou pausar fonte",
    description="Habilitar exige autoridade completa e credencial válida na revisão atual, com capacidade2 verificada. Pausar permanece disponível durante manutenção.",
    returns="O estado confirmado.",
    response_model=EventSourceInfo,
    errors={409: "version_conflict ou source_not_ready."},
)
def set_event_source_enabled(
    source_id: UUID, body: SetEventSourceEnabled, key: CurrentKey, request: Request
):
    return execute(
        lambda session: event_sources.set_enabled(
            session, source_id, command=body, actor=key.actor, now=now(request)
        )
    )


@router.operation(
    "GET",
    "/event-sources/{source_id}/credentials",
    operation_id="list_event_credentials",
    role="leitura",
    summary="Listar credenciais de eventos",
    description="Metadados sem segredo ou hash, inclusive revogadas.",
    returns="Uma página de credenciais.",
    response_model=EventCredentialPage,
)
def list_event_credentials(
    source_id: UUID, request: Request, page: Annotated[PageParams, Depends(page_params)]
):
    def action(session):
        items, cursor = paginate(
            event_sources.list_credentials(session, source_id, now=now(request)),
            page,
            key=lambda item: str(item.id),
        )
        return EventCredentialPage(items=items, next_cursor=cursor)

    return execute(action)


@router.operation(
    "POST",
    "/event-sources/{source_id}/credentials",
    operation_id="create_event_credential",
    role="total",
    external_effect=True,
    summary="Criar credencial de eventos",
    description="Escopo exclusivo journey:events:write. expires_at é obrigatório e nullable; null escolhe sem vencimento. Segredo aparece uma vez. Repetição idempotente responde409 credential_secret_already_issued, sem reemitir segredo.",
    returns="Metadados e segredo uma vez.",
    response_model=CreatedEventCredential,
    status_code=201,
    errors={
        409: "version_conflict, source_revision_conflict, idempotency_conflict ou credential_secret_already_issued."
    },
)
def create_event_credential(
    source_id: UUID, body: CreateEventCredential, key: CurrentKey, request: Request
):
    return execute(
        lambda session: event_sources.issue_credential(
            session, source_id, command=body, actor=key.actor, now=now(request)
        )
    )


@router.operation(
    "POST",
    "/event-credentials/{credential_id}/rotate",
    operation_id="rotate_event_credential",
    role="total",
    external_effect=True,
    summary="Girar credencial de eventos",
    description="Revoga a anterior e emite sucessora na mesma revisão, atomicamente. expires_at obrigatório nullable; segredo uma vez e idempotency_key no corpo, sem recuperação em replay.",
    returns="A sucessora e seu segredo uma vez.",
    response_model=CreatedEventCredential,
    errors={
        409: "version_conflict, state_conflict, idempotency_conflict ou credential_secret_already_issued."
    },
)
def rotate_event_credential(
    credential_id: UUID, body: RotateEventCredential, key: CurrentKey, request: Request
):
    return execute(
        lambda session: event_sources.rotate_credential(
            session, credential_id, command=body, actor=key.actor, now=now(request)
        )
    )


@router.operation(
    "POST",
    "/event-credentials/{credential_id}/revoke",
    operation_id="revoke_event_credential",
    role="total",
    destructive=True,
    external_effect=True,
    summary="Revogar credencial de eventos",
    description="Encerra a autenticação desta credencial; disponível durante manutenção. Não apaga fatos já declarados.",
    returns="Metadados da credencial revogada.",
    response_model=EventCredentialInfo,
    errors={409: "version_conflict: releia a credencial."},
)
def revoke_event_credential(credential_id: UUID, body: Revoke, key: CurrentKey, request: Request):
    return execute(
        lambda session: event_sources.revoke_credential(
            session, credential_id, command=body, actor=key.actor, now=now(request)
        )
    )


@router.operation(
    "GET",
    "/submissions/{submission_id}/journey",
    operation_id="get_submission_journey",
    role="leitura",
    lead_data=True,
    summary="Consultar jornada do diagnóstico",
    description="Separa fatos locais e declarações por fonte e entidade, inclusive captura sem pedido. IDs externos e evidência somente em lead_data com papel total e opt-in. Consulta não acessa provedores ou storage.",
    returns="Jornada segura; sem vínculo retorna fatos locais e lista externa vazia.",
    response_model=SubmissionJourney,
)
def get_submission_journey(
    submission_id: UUID, key: CurrentKey, include_lead_data: INCLUDE_LEAD_DATA = False
):
    if include_lead_data:
        ensure_role(key, "total", what="Consultar dados externos da participante")
    return execute(
        lambda session: journey.read_journey(session, submission_id, with_lead=include_lead_data)
    )


@router.operation(
    "GET",
    "/submissions/{submission_id}/journey/events",
    operation_id="list_submission_journey_events",
    role="leitura",
    lead_data=True,
    summary="Listar histórico da jornada",
    description="Página por recebimento e identificador local decrescentes, com filtros por fonte e tipo. Dados externos exigem papel total e include_lead_data; não busca por contato ou ID externo.",
    returns="Página de declarações e retiradas, sem inferir presença ou recebimento.",
    response_model=JourneyEventPage,
)
def list_submission_journey_events(
    submission_id: UUID,
    key: CurrentKey,
    page: Annotated[PageParams, Depends(page_params)],
    include_lead_data: INCLUDE_LEAD_DATA = False,
    source_id: UUID | None = None,
    type: Annotated[EventType | None, Query()] = None,
):
    if include_lead_data:
        ensure_role(key, "total", what="Consultar dados externos da participante")

    def action(session):
        items, cursor = journey.list_events(
            session,
            submission_id,
            page=page,
            source_id=source_id,
            event_type=type,
            with_lead=include_lead_data,
        )
        return JourneyEventPage(items=items, next_cursor=cursor)

    return execute(action)


@router.operation(
    "POST",
    "/submissions/{submission_id}/journey/erase",
    operation_id="erase_submission_journey",
    role="total",
    destructive=True,
    external_effect=True,
    summary="Apagar dados externos da jornada",
    description="Apaga dados e evidências externas e revoga o encaminhamento individual na mesma transação. Não apaga respostas nem revoga PDF. Metadados mínimos duram até a retenção; cópias remotas dependem da fonte. Exige versões da jornada e referência.",
    returns="A jornada após o apagamento, sem dados externos recuperáveis.",
    response_model=ErasedJourney,
    errors={409: "version_conflict: releia jornada e referência antes de confirmar."},
)
def erase_submission_journey(
    submission_id: UUID, body: EraseJourney, key: CurrentKey, request: Request
):
    return execute(
        lambda session: journey.erase_journey(
            session, submission_id, command=body, actor=key.actor, now=now(request)
        )
    )
