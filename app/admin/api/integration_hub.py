"""Leitura local das integrações; nenhuma consulta de rede ou comando mutante."""

from typing import Annotated

from fastapi import Depends, Query

from app.admin.api.common import PageParams, page_params, paginate
from app.admin.api.errors import ApiError
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.integration_hub import IntegrationHubPage
from app.admin.queries import integration_hub as queries
from app.deps import get_deps

router = OperationRouter(
    tag="Integrações",
    description="Central de Baserow e Webhook por formulário. Configuração, herança e histórico local, sem consultar provedores nem expor dados da participante ou credenciais.",
)


@router.operation(
    "GET",
    "/integrations",
    operation_id="list_integrations",
    role="leitura",
    summary="Listar as integrações e associações dos formulários",
    description=(
        "Consulta somente o banco local: Baserow e Webhook, inclusive rascunhos, inativos e "
        "configurações órfãs conhecidas. Filtra por conector e formulário; um filtro de formulário "
        "não inclui a linha global do padrão. Ordena por conector, padrão primeiro e slug crescente. "
        "scope_kind e form_slug distinguem padrão de formulário; key é a identidade de paginação. "
        "receiver descreve o receptor efetivo, com revisão própria: não use a revisão herdada para "
        "criar um receptor de formulário. Contadores do catálogo são globais. Ativa indica apenas "
        "a configuração Baserow; mapeamento salvo não prova compatibilidade com a tabela atual. "
        "provider_verification é sempre not_checked. Histórico correlaciona identidade e formulário, "
        "separa captura/PDF e qualifica a revisão; Baserow PDF tem revisão unknown. "
        "O resumo do padrão não deve ser somado às linhas herdadas. Sucesso confirma transporte, "
        "sem provar leitura, atendimento ou liberação. Históricos legados PDF sem receptor ficam "
        "no pedido. Nunca expõe URL completa, token, segredo, payload ou texto bruto de erro, "
        "mesmo para total. Não consulta provedores, não dispara automação nem exige confirmação. "
        "Escritas continuam nas operações existentes de integração e destinos."
    ),
    returns="Catálogo global e página de associações com fatos locais seguros e cursor da próxima página.",
    response_model=IntegrationHubPage,
)
def list_integrations(
    page: Annotated[PageParams, Depends(page_params)],
    connector: Annotated[
        queries.Connector | None, Query(description="Conector; omitido lista ambos.")
    ] = None,
    form_slug: Annotated[
        str | None,
        Query(
            description="Slug persistido ou configuração órfã conhecida; inexistente retorna 404."
        ),
    ] = None,
) -> IntegrationHubPage:
    deps = get_deps()
    with deps.sessions() as session:
        try:
            found = queries.overview(
                session, cipher=deps.cipher, connector=connector, form_slug=form_slug
            )
        except queries.UnknownFormFilter:
            raise ApiError("not_found", "Formulário não encontrado.") from None
    chunk, next_cursor = paginate(found.items, page, key=lambda row: row.key)
    return IntegrationHubPage(catalog=found.catalog, items=chunk, next_cursor=next_cursor)
