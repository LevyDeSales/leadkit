"""Capturas em admin/API/MCP com projeção por papel antes da leitura dos dados pessoais."""

from typing import Annotated, Any, Literal

from fastapi import Depends, Query
from pydantic import AwareDatetime, BaseModel

from app.admin.api.common import Page, PageParams, decode_keyset, encode_keyset, page_params
from app.admin.api.errors import ApiError
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.capture_deliveries import (
    CaptureDeliveryState,
    RetryCaptureDeliveryRequest,
)
from app.admin.api.security import CurrentKey, ensure_role
from app.admin.queries import captures
from app.deps import get_deps

router = OperationRouter(
    tag="Capturas",
    description="Respostas confirmadas sem geração de plano ou PDF. Estado é de leitura; dados da participante exigem total e opt-in.",
)


class CaptureState(BaseModel):
    submission_id: str
    status: Literal["captured"]
    form_slug: str
    form_version: str
    form_version_number: int
    created_at: AwareDatetime
    lead_data: dict[str, Any] | None = None
    deliveries: list[CaptureDeliveryState] = []


class CapturePage(Page[CaptureState]):
    pass


@router.operation(
    "GET",
    "/captures",
    operation_id="list_captures",
    role="leitura",
    lead_data=True,
    summary="Listar capturas confirmadas",
    description="Lista capturas por chegada. Sem include_lead_data, lê somente estado. Dados históricos da participante exigem papel total e include_lead_data=true; sem opt-in não há busca pessoal.",
    returns="Capturas e cursor da próxima página; sem credenciais do recibo.",
    response_model=CapturePage,
)
def list_captures(
    key: CurrentKey,
    page: Annotated[PageParams, Depends(page_params)],
    include_lead_data: bool = False,
    form: Annotated[str | None, Query(max_length=64)] = None,
    form_version: Annotated[int | None, Query(ge=1)] = None,
) -> CapturePage:
    if include_lead_data:
        ensure_role(key, "total", what="Ler dados da participante")
    deps = get_deps()
    after = decode_keyset(page.cursor) if page.cursor else None
    with deps.sessions() as session:
        items = captures.listing(
            session,
            deps.forms,
            with_lead=include_lead_data,
            form=form,
            version=form_version,
            after=after,
            limit=page.limit + 1,
        )
    more = len(items) > page.limit
    items = items[: page.limit]
    cursor = encode_keyset(items[-1]["created_at"], items[-1]["submission_id"]) if more else None
    return CapturePage(items=items, next_cursor=cursor)


@router.operation(
    "GET",
    "/captures/{submission_id}",
    operation_id="get_capture",
    role="leitura",
    lead_data=True,
    summary="Consultar uma captura",
    description="Consulta estado por referência da submissão. Respostas, contato histórico, consentimento, tracking e pontuação só são lidos com papel total e include_lead_data=true. Não expõe token ou URL de recibo.",
    returns="Captura confirmada e dados pessoais apenas quando autorizados e pedidos.",
    response_model=CaptureState,
    errors={404: "Captura não encontrada."},
)
def get_capture(
    submission_id: str, key: CurrentKey, include_lead_data: bool = False
) -> CaptureState:
    if include_lead_data:
        ensure_role(key, "total", what="Ler dados da participante")
    deps = get_deps()
    with deps.sessions() as session:
        found = captures.detail(session, deps.forms, submission_id, with_lead=include_lead_data)
    if found is None:
        raise ApiError("not_found", "Captura não encontrada.")
    return CaptureState(**found)


@router.operation(
    "POST",
    "/captures/{submission_id}/deliveries/{delivery_id}/retry",
    operation_id="retry_capture_delivery",
    role="total",
    external_effect=True,
    summary="Repetir uma entrega de captura",
    description="Reautoriza a mesma entrega e o mesmo destino na revisão confirmada, após validar o perfil histórico. Só falhas voltam para pendente; sucesso não se repete. Exige confirmação e versões esperadas; não gera PDF nem recria submissão.",
    returns="Estado seguro da entrega; intenção persistida é recuperada pelo reconciliador.",
    response_model=CaptureDeliveryState,
    errors={
        404: "Captura ou entrega não encontrada.",
        409: "Entrega ou configuração mudou (`state_conflict`); confira a revisão e a finalidade.",
    },
)
def retry_capture_delivery(
    submission_id: str, delivery_id: str, body: RetryCaptureDeliveryRequest, key: CurrentKey
) -> CaptureDeliveryState:
    from app.admin.actions import captures as actions

    try:
        result = actions.retry(
            get_deps(),
            submission_id,
            delivery_id,
            expected_dispatch=body.expected_dispatch,
            expected_config_version=body.expected_config_version,
            actor=key.actor,
        )
    except actions.Missing:
        raise ApiError("not_found", "Captura ou entrega não encontrada.") from None
    except actions.Conflict:
        raise ApiError(
            "state_conflict",
            "Entrega ou configuração mudou; confira a finalidade e a revisão do mesmo destino.",
        ) from None
    return CaptureDeliveryState(**result)
