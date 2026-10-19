"""A auditoria: a leitura `list_audit_log`.

Lê pela consulta `app.admin.queries.audit`, que é nova: o painel só mostra a auditoria de um pedido, na
linha do tempo (`GET /admin/jobs/{public_id}`), e a API ganha a leitura dela inteira. É a última
operação da matriz (ADR 0015, fase 8): com ela, a cobertura deixa de ser meta e vira regra da suíte.

**Só leitura, e nenhuma leitura é auditada** (decisão 2 da revisão da ADR 0015): ler a auditoria não
grava nada nela. O papel é `leitura`, porque a consulta devolve as entradas sem o que uma chave que só
consulta não pode ver: os `details` passam por uma lista de permissão **de chaves** (`PUBLIC_DETAIL_KEYS`),
e o valor sob uma chave permitida sai como foi escrito (a lista não olha o conteúdo). Ficam de fora o texto
livre que a operadora digita ao reprocessar um pedido (`reason`), que pode citar a lead, e o texto de erro
de fora do teste de uma integração (`error`), que o projeto trata como só do `total`. O segredo, o hash de
uma chave e o caminho da URL de um destino não são gravados na auditoria pelos serviços de hoje.

**A página é por chave (`at`, `id`), não por número**: a entrada que chega durante a leitura é mais nova
que o cursor, fica fora das páginas seguintes, e nenhuma é pulada nem repetida. Os filtros são
igualdades, e se somam.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Query

from app.admin.api.common import PageParams, decode_keyset, encode_keyset, page_params
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.audit import AuditEntry, AuditLogPage
from app.admin.queries import audit as audit_queries
from app.deps import get_deps

TAG = "Auditoria"
router = OperationRouter(
    tag=TAG,
    description=(
        "A auditoria do sistema: o registro de quem fez o quê, quando e em que alvo (uma pessoa no "
        "painel, uma chave de API ou a retenção automática). Só as escritas ficam registradas; ler, "
        "inclusive esta lista, não grava nada. É de `leitura`: os `details` de cada entrada passam por "
        "uma lista de chaves, e o motivo digitado ao reprocessar um pedido e o texto de erro de fora "
        "(só do `total`) não vêm."
    ),
)

FILTER_MAX_LENGTH = 200


def _filter(description: str) -> Any:
    """Um filtro de texto: igualdade, de 1 a `FILTER_MAX_LENGTH` caracteres (o vazio é `422` no campo,
    e não "qualquer": quem não quer filtrar não manda o parâmetro)."""
    return Annotated[
        str | None,
        Query(min_length=1, max_length=FILTER_MAX_LENGTH, description=description),
    ]


TARGET_TYPE = _filter(
    "Só as entradas deste tipo de alvo, como `job`, `delivery`, `integration_sync`, `form`, "
    "`asset`, `webhook_destination`, `integration` ou `api_key`. Igualdade exata, de 1 a 200 "
    "caracteres. Ausente: qualquer."
)
TARGET_ID = _filter(
    "Só as entradas deste alvo, pelo id que a auditoria guarda (o `target_id` de uma entrada): o "
    "id interno de um pedido, formulário, imagem, entrega ou destino, ou o `key_id` de uma "
    "chave. Igualdade exata, de 1 a 200 caracteres. Ausente: qualquer."
)
ACTOR = _filter(
    "Só as entradas deste autor, como o usuário do painel (`operadora`) ou `chave:<nome>`. "
    "Igualdade exata, de 1 a 200 caracteres. Ausente: qualquer."
)
ACTION = _filter(
    "Só as entradas desta ação, como `job.reprocess`, `form.publish` ou `api_key.revoke`. "
    "Igualdade exata, de 1 a 200 caracteres. Ausente: qualquer."
)


def _cursor_of(entry: audit_queries.AuditEntry) -> str:
    """O cursor da página seguinte: a chave de ordenação (`at`, `id`) da última entrada."""
    return encode_keyset(entry.at, entry.id)


def _after(cursor: str | None) -> audit_queries.AuditCursor | None:
    if cursor is None:
        return None
    return audit_queries.AuditCursor(*decode_keyset(cursor))


@router.operation(
    "GET",
    "/audit-log",
    operation_id="list_audit_log",
    role="leitura",
    summary="Listar a auditoria",
    description=(
        "Lê a auditoria do sistema, da entrada mais nova para a mais antiga, `limit` por página (de 1 "
        "a 200; padrão 50), com o cursor da seguinte em `next_cursor`: a entrada que chega durante a "
        "leitura não pula nem repete nenhuma linha. Cada entrada diz **quem** (`actor`: o usuário do "
        "painel, `chave:<nome>` para uma chave de API, `retention` ou `ambiente`), **o quê** "
        "(`action`: `job.reprocess`, `form.publish`, `api_key.create`...), **em quê** (`target_type` "
        "e `target_id`, nulos nas ações sem alvo) e **quando** (`at`), com os `details` que a ação "
        "guardou. Filtros, todos por igualdade e somados: `target_type` e `target_id` (o alvo), "
        "`actor` e `action`. **Os `details` passam por uma lista de chaves** (o `slug` do "
        "formulário, a versão, o host de um destino, a tentativa, o nome e o papel de uma chave...), "
        "e o valor sob uma chave permitida sai como foi escrito. Os serviços não gravam o segredo, o "
        "hash de uma chave nem o caminho da URL de um destino. O motivo que a operadora digita ao "
        "reprocessar um pedido (`reason`) e o texto de erro de fora do teste de uma integração "
        "(`error`) são gravados e **não vêm**: só a chave `total` os lê, em `get_job` (`lead_data`) "
        "e na resposta da própria ação. O `target_id` é o id interno do alvo, que a auditoria guarda; "
        "o de um pedido ou de um formulário não é o código nem o endereço que o resto da API "
        "usa. Só as escritas são auditadas: esta leitura não grava nada, nem na auditoria."
    ),
    returns="Uma página de entradas da auditoria, com `next_cursor` para a seguinte (nulo na última).",
    response_model=AuditLogPage,
)
def list_audit_log(
    page: Annotated[PageParams, Depends(page_params)],
    target_type: TARGET_TYPE = None,
    target_id: TARGET_ID = None,
    actor: ACTOR = None,
    action: ACTION = None,
) -> AuditLogPage:
    after = _after(page.cursor)
    filters = audit_queries.AuditFilter(
        target_type=target_type, target_id=target_id, actor=actor, action=action
    )
    with get_deps().sessions() as session:
        entries, more = audit_queries.entries_after(session, filters, limit=page.limit, after=after)
    return AuditLogPage(
        items=[AuditEntry.of(entry) for entry in entries],
        next_cursor=_cursor_of(entries[-1]) if more else None,
    )
