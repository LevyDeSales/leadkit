"""Capturas em admin/API/MCP com projeção por papel antes da leitura dos dados pessoais."""

from typing import Annotated, Any, Literal

from fastapi import Depends, Query
from pydantic import AwareDatetime, BaseModel

from app.admin.api.common import Page, PageParams, decode_keyset, encode_keyset, page_params
from app.admin.api.errors import ApiError
from app.admin.api.operations import OperationRouter
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
