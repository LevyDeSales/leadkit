"""As operações da integração de um formulário com o Baserow: `get_form_integration`,
`connect_form_integration`, `save_form_integration_mapping`, `activate_form_integration`,
`deactivate_form_integration`, `remove_form_integration`, `send_form_integration_test` e
`resend_failed_form_integration_syncs`.

Leem pela mesma ação e pela mesma consulta que a página `/admin/forms/{slug}/baserow`
(`app.admin.actions.integrations`, `app.admin.queries.integrations`): a conferência de cada coisa é
uma só, e a API a traduz em JSON e status. A auditoria sai dos serviços, com o autor `chave:<nome>`
da chave que chamou.

**O token do Baserow só entra.** Nenhuma resposta, log, auditoria nem mensagem de erro o traz: a leitura
diz `token_set` e `token_unreadable`, e as mensagens citam o status e o código de erro do Baserow.

**Ativar, testar e reenviar escrevem na planilha do cliente** (`x-external-effect`) e remover apaga o
token e o estado das sincronizações (`x-destructive`): as quatro exigem `"confirm": true` (na query, em
`DELETE`). Ativar só liga com o relatório do mapeamento limpo contra a tabela de agora, e a versão
esperada se compara antes de falar com o Baserow. **O reenvio em lote dispara trabalho no Hatchet, e um
disparo que não chega ao engine não é erro:** a ação vale (as sincronizações voltaram para a fila), a
resposta diz `launched: false` e o reconciliador dispara. O envio de teste que o Baserow recusou também
não é erro: é um resultado (`ok` falso).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Path
from fastapi.concurrency import run_in_threadpool

from app.admin import launching
from app.admin.actions import integrations as actions
from app.admin.api.common import ConfirmQuery, ExpectedVersionQuery
from app.admin.api.errors import ApiError, ErrorItem
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.integrations import (
    ActivateFormIntegrationRequest,
    BaserowConnection,
    BaserowIntegration,
    BaserowMapping,
    ConnectFormIntegrationRequest,
    DeactivateFormIntegrationRequest,
    RemovedFormIntegration,
    ResendFailedFormIntegrationSyncsRequest,
    ResentFailedFormIntegrationSyncs,
    SaveFormIntegrationMappingRequest,
    SendFormIntegrationTestRequest,
    SentFormIntegrationTest,
)
from app.admin.api.security import CurrentKey
from app.admin.queries import forms as form_queries
from app.deps import get_deps
from app.integrations import config as integration_config
from app.integrations import mapping as integration_mapping

TAG = "Integração com o Baserow"
router = OperationRouter(
    tag=TAG,
    description=(
        "A integração de um formulário com uma tabela do Baserow: cada pedido concluído vira uma "
        "linha na tabela, com os campos que o mapeamento escolhe. Ler a integração (a tabela é lida "
        "agora no Baserow) é de `leitura`; conectar, mapear, ativar, desativar, remover, testar e "
        "reenviar são de `total`. O **token do Baserow só entra** (ao conectar): nenhuma resposta o "
        "devolve. Ativar, testar e reenviar escrevem na planilha do cliente, e remover apaga o "
        "token e o estado das sincronizações: as quatro exigem `confirm`."
    ),
)

FORM = Annotated[
    str,
    Path(description="O endereço do formulário (`slug`), como `list_forms` o mostra."),
]
NOT_FOUND = "Formulário não encontrado."


@router.operation(
    "GET",
    "/forms/{slug}/integrations/baserow",
    operation_id="get_form_integration",
    role="leitura",
    heavy=True,
    summary="Ler a integração de um formulário com o Baserow",
    description=(
        "A integração do formulário `slug` com a tabela do Baserow: a conexão guardada (endereço, "
        "número da tabela, `version`, se está ativa e **se há um token e se ele abre**, nunca o "
        "token), o mapeamento guardado, os campos da tabela **lidos agora no Baserow** com as "
        "fontes que servem a cada um e a sugerida pelo nome, o relatório do mapeamento contra a "
        "tabela de agora (o que impede ativar), as sincronizações por estado e as que falharam. "
        "`version` é o que as escritas pedem em `expected_version`. Um formulário sem integração "
        "devolve `connected` falso e `connection` nulo (para conectar, `connect_form_integration`); "
        "um formulário sem versão publicada não pode ser mapeado (`published` falso). **Uma tabela "
        "que não se leu não derruba a resposta**: o Baserow fora do ar ou o token recusado, ou o "
        "token guardado que não abre com a `WEBHOOK_ENCRYPTION_KEY` atual, deixam `fields` e "
        "`report` nulos e dizem o motivo em `fields_error`; o mapeamento guardado e as "
        "sincronizações vêm do banco e estão sempre aqui. **Chama o Baserow**, por isso custa 5 "
        "fichas do limite de uso. O texto do erro de cada sincronização que falhou é livre e fica "
        "de fora (`get_job`, com o bloco `lead_data`, o traz). Não grava nada."
    ),
    returns="A integração, com a tabela lida agora, o mapeamento, o relatório e as sincronizações.",
    response_model=BaserowIntegration,
)
def get_form_integration(
    slug: FORM, profile: Literal["pdf", "capture"] | None = None
) -> BaserowIntegration:
    detail = actions.read_detail(get_deps(), slug, profile=profile)
    if detail is None:
        raise ApiError("not_found", NOT_FOUND)
    return BaserowIntegration.of(detail)


# --- conectar -----------------------------------------------------------------------------------

INTEGRATION_CHANGED = (
    "A integração mudou desde que você a leu (outra pessoa ou outra chamada). Nada foi gravado: leia "
    "de novo com `get_form_integration` e refaça a mudança."
)
INTEGRATION_GONE = (
    "A integração não existe (foi removida, ou o formulário nunca foi conectado). Nada foi gravado: "
    'conecte o Baserow com `connect_form_integration`, mandando `expected_version` `"0"`.'
)


def _ensure_form(slug: str) -> None:
    """`404` se o formulário não existe."""
    with get_deps().sessions() as session:
        found = form_queries.form_title(session, slug)
    if found is None:
        raise ApiError("not_found", NOT_FOUND)


def _version_conflict(conflict: integration_config.Conflict) -> ApiError:
    """O `409 version_conflict`, com a versão de agora (`0` se a integração não existe: é a versão
    da criação)."""
    if conflict.current is None:
        return ApiError("version_conflict", INTEGRATION_GONE, current_version="0")
    return ApiError(
        "version_conflict", INTEGRATION_CHANGED, current_version=str(conflict.current.version)
    )


CONNECT_TEXT = (
    "Conectar o formulário a uma tabela do Baserow, ou mudar a conexão, apaga o mapeamento e "
    "desativa a integração se o endereço ou a tabela mudam: os pedidos que ficarem prontos "
    "deixam de virar linha na planilha até alguém mapear e ativar de novo."
)


@router.operation(
    "PUT",
    "/forms/{slug}/integrations/baserow/connection",
    operation_id="connect_form_integration",
    role="total",
    destructive=True,
    confirmation_text=CONNECT_TEXT,
    heavy=True,
    errors={
        409: "A integração mudou desde que você a leu (`version_conflict`, com a versão de agora; "
        "`0` se ela não existe), ou o formulário ainda não tem versão publicada "
        "(`state_conflict`): nada é gravado. A versão é conferida **antes** de ler o Baserow. Leia "
        "de novo com `get_form_integration` e refaça a mudança.",
        422: "Falta `confirm: true` (`confirmation_required`), ou o corpo não vale "
        "(`validation_failed`): o endereço, o número da tabela, o token (obrigatório ao criar, e "
        "ao mudar de servidor) ou o token guardado, que não abre. `errors` traz o motivo de cada "
        "campo, e nada é lido nem gravado.",
        502: "O Baserow não leu a tabela com o token (`upstream_failed`): não respondeu, recusou o "
        "token ou não achou a tabela. A mensagem diz o status; nada é gravado.",
    },
    summary="Conectar o formulário a uma tabela do Baserow",
    description=(
        "Cria a integração do formulário `slug` com uma tabela do Baserow (`expected_version` "
        '`"0"`; o `token` é obrigatório) ou muda a conexão da que existe (`expected_version` é a '
        "`version` que `get_form_integration` devolveu). **Antes de gravar, lê os campos da tabela "
        "no Baserow com o token** (custa 5 fichas do limite de uso): se o Baserow não respondeu, "
        "recusou o token ou não achou a tabela, a resposta é `502 upstream_failed` e **nada é "
        "gravado**. O formulário precisa ter versão publicada (senão, `409 state_conflict`). O "
        "endereço é conferido como na página: `https` (em produção só `https`), sem usuário e "
        "senha, sem `?` nem `#` e sem IP link-local. **O token só entra**: é guardado cifrado e "
        "nunca volta em resposta, log, auditoria nem mensagem de erro (a leitura diz só "
        "`token_set` e `token_unreadable`). Numa integração que existe, `token` em branco (ausente "
        "ou nulo) **mantém o guardado**, e mandar um token novo é a rotação; se o guardado não "
        "abre mais com a `WEBHOOK_ENCRYPTION_KEY`, mande o token de novo. **O token guardado "
        "nunca vai a outro servidor**: mudar o servidor (o host, a porta ou o `https`) com o "
        "`token` em branco é `422` no campo `token`; mande o token de novo. **Mudar o endereço ou a tabela apaga o mapeamento e desativa a "
        "integração**: os campos eram de outra tabela. Por isso a operação é marcada e exige "
        '`"confirm": true` em toda chamada, inclusive na criação (sem ele, `422 '
        "confirmation_required` e nada acontece). Uma integração nasce desativada. Se a versão "
        "mudou desde que você a leu (ou a integração foi removida, versão `0`), a resposta é `409 "
        "version_conflict` e nada muda, mesmo com o Baserow fora do ar: a versão se confere antes "
        "de ler a tabela. A mudança fica "
        "na auditoria em nome da chave (`integration.create` ou `integration.connect`), com o host "
        "e o número da tabela, sem o token."
    ),
    returns="A conexão como ficou, com a versão nova e sem o token.",
    response_model=BaserowConnection,
)
def connect_form_integration(
    slug: FORM, body: ConnectFormIntegrationRequest, key: CurrentKey
) -> BaserowConnection:
    _ensure_form(slug)
    try:
        done = actions.connect(
            get_deps(),
            slug,
            base_url=body.base_url,
            table_id=body.table_id,
            token=body.token.get_secret_value() if body.token is not None else "",
            expected_version=actions.version_number(body.expected_version),
            actor=key.actor,
        )
    except actions.NotPublished as unpublished:
        raise ApiError("state_conflict", str(unpublished)) from None
    except actions.InvalidConnection as invalid:
        raise ApiError(
            "validation_failed",
            "A conexão não vale; veja o motivo de cada campo em `errors`.",
            errors=[
                ErrorItem(field=name, message=message) for name, message in invalid.errors.items()
            ],
        ) from None
    except actions.TableNotRead as unread:
        raise ApiError("upstream_failed", str(unread)) from None
    except integration_config.Conflict as conflict:
        raise _version_conflict(conflict) from None
    return BaserowConnection.of(done.integration, token_unreadable=False)


# --- mapear -------------------------------------------------------------------------------------


@router.operation(
    "PUT",
    "/forms/{slug}/integrations/baserow/mapping",
    operation_id="save_form_integration_mapping",
    role="total",
    heavy=True,
    errors={
        409: "A integração mudou desde que você a leu, ou já não existe (`version_conflict`, com a "
        "versão de agora; `0` se não existe), ou o formulário não tem versão publicada ou o token "
        "guardado não abre (`state_conflict`): nada é gravado. A versão é conferida **antes** de ler "
        "o Baserow.",
        422: "O mapeamento não vale (`validation_failed`, com `field` `sources`): uma fonte que o "
        "formulário não tem ou que não serve ao tipo do campo, um campo que não se preenche "
        "(somente leitura ou tipo sem suporte), ou o código do pedido que falta ou se repete. Cada "
        "item de `errors` diz o que corrigir, e nada é gravado. Um id que a tabela não tem é "
        "**ignorado**, e não recusado.",
        502: "O Baserow não leu a tabela (`upstream_failed`): não respondeu ou recusou o token. A "
        "mensagem diz o status; nada é gravado.",
    },
    summary="Guardar o mapeamento da integração com o Baserow",
    description=(
        "Troca o mapeamento inteiro da integração do formulário `slug`: `sources` diz a fonte que "
        "preenche cada campo da tabela, pelo id do campo (ver `fields` de `get_form_integration`, "
        "que também lista as fontes que servem a cada um e a sugerida pelo nome). `expected_version` "
        "é a `version` que a leitura devolveu; se mudou, ou se a integração foi removida (versão "
        "`0`), `409 version_conflict` e nada é gravado, mesmo com o Baserow fora do ar: a versão se "
        "confere antes de ler a tabela. Um formulário sem versão publicada, ou cujo token guardado "
        "não abre, é `409 state_conflict`. **Lê a tabela no Baserow agora** (custa 5 fichas do "
        "limite de uso) e confere o mapeamento contra ela e contra as perguntas da versão publicada, "
        "como a página: cada campo mandado se preenche (não é somente leitura nem de tipo sem "
        "suporte), cada fonte serve ao tipo do campo, e há um campo, e só um, de texto com o código "
        "do pedido (`job.public_id`). **Um id que a tabela não tem é ignorado**, e não recusado. "
        "Se o Baserow não leu a tabela, `502` e nada é gravado. **Com "
        "pendências no relatório** (uma opção do formulário sem par no Baserow, um campo que sumiu), "
        "o mapeamento é guardado mesmo assim, mas **uma integração ativa é desativada**: ela não pode "
        "seguir enviando com elas (`deactivated`); corrija a tabela ou o mapeamento e ative de novo. "
        "A resposta traz o mapeamento guardado e o relatório. A mudança fica na auditoria em nome da "
        "chave (`integration.mapping`), com a contagem de campos e se desativou."
    ),
    returns="O mapeamento como ficou, o relatório contra a tabela de agora e a versão nova.",
    response_model=BaserowMapping,
)
def save_form_integration_mapping(
    slug: FORM, body: SaveFormIntegrationMappingRequest, key: CurrentKey
) -> BaserowMapping:
    _ensure_form(slug)
    try:
        saved = actions.save_mapping(
            get_deps(),
            slug,
            chosen=body.chosen(),
            profile=body.profile,
            expected_version=actions.version_number(body.expected_version),
            actor=key.actor,
        )
    except actions.NotPublished as unpublished:
        raise ApiError("state_conflict", str(unpublished)) from None
    except actions.TokenUnreadable as unreadable:
        raise ApiError("state_conflict", str(unreadable)) from None
    except actions.ReadFailed as unread:
        raise ApiError("upstream_failed", str(unread)) from None
    except integration_mapping.InvalidMapping as invalid:
        raise ApiError(
            "validation_failed",
            "O mapeamento não vale; veja o que corrigir em `errors`.",
            errors=[ErrorItem(field="sources", message=problem) for problem in invalid.problems],
        ) from None
    except integration_config.Conflict as conflict:
        raise _version_conflict(conflict) from None
    return BaserowMapping.of(saved)


# --- ativar e desativar -------------------------------------------------------------------------

ACTIVATE_TEXT = (
    "Ativar a integração faz cada pedido que ficar pronto daqui em diante virar uma linha na planilha "
    "do cliente no Baserow."
)


@router.operation(
    "POST",
    "/forms/{slug}/integrations/baserow/activate",
    operation_id="activate_form_integration",
    role="total",
    external_effect=True,
    confirmation_text=ACTIVATE_TEXT,
    heavy=True,
    errors={
        409: "A integração mudou desde que você a leu, ou já não existe (`version_conflict`, com a "
        "versão de agora; `0` se não existe), ou o formulário não tem versão publicada ou o token "
        "guardado não abre (`state_conflict`): nada muda. A versão é conferida **antes** de ler o "
        "Baserow.",
        422: "Falta `confirm: true` (`confirmation_required`), o corpo não vale ou o relatório do "
        "mapeamento tem pendências, ou nada está mapeado (`validation_failed`; com pendências, "
        "`errors` traz uma frase por pendência, no campo `mapping`). Nada muda.",
        502: "O Baserow não leu a tabela (`upstream_failed`): não respondeu ou recusou o token. A "
        "mensagem diz o status; nada muda.",
    },
    summary="Ativar a integração de um formulário com o Baserow",
    description=(
        "Liga a integração do formulário `slug`: daqui em diante, cada pedido que ficar pronto vira "
        "uma linha na tabela do Baserow (os que já estavam prontos não são sincronizados; para isso, "
        "reprocesse o pedido). **Lê a tabela no Baserow agora** (custa 5 fichas do limite de uso) e "
        "**só liga se o relatório do mapeamento guardado contra ela está limpo**: há campos mapeados, "
        "nenhum problema (um campo que sumiu ou mudou de tipo, uma fonte que o formulário já não "
        "tem) e toda opção do formulário tem par no Baserow (é o `report` de "
        "`get_form_integration`). Com pendências a resposta é `422 validation_failed`, uma frase por "
        "pendência em `errors` (campo `mapping`), e nada muda: corrija a tabela ou o mapeamento "
        "(`save_form_integration_mapping`) e tente de novo. Passa a escrever na planilha do cliente, "
        'por isso a operação é marcada e exige `"confirm": true` em toda chamada (sem ele, `422 '
        "confirmation_required` e nada acontece). `expected_version` é a `version` que "
        "`get_form_integration` devolveu; se mudou, ou se a integração foi removida (versão `0`), "
        "`409 version_conflict` e nada muda, mesmo com o Baserow fora do ar: a versão se confere "
        "antes de ler a tabela. Um formulário sem versão publicada, ou cujo token guardado não abre "
        "com a `WEBHOOK_ENCRYPTION_KEY` atual, é `409 state_conflict`. Se o Baserow não leu a "
        "tabela, `502` e nada muda. A ativação fica na auditoria em nome da chave "
        "(`integration.activate`). Para desligar, `deactivate_form_integration`."
    ),
    returns="A conexão como ficou (ativa), com a versão nova e sem o token.",
    response_model=BaserowConnection,
)
def activate_form_integration(
    slug: FORM, body: ActivateFormIntegrationRequest, key: CurrentKey
) -> BaserowConnection:
    _ensure_form(slug)
    try:
        activated = actions.activate(
            get_deps(),
            slug,
            expected_version=actions.version_number(body.expected_version),
            actor=key.actor,
        )
    except actions.NotPublished as unpublished:
        raise ApiError("state_conflict", str(unpublished)) from None
    except actions.IntegrationMissing:  # como no mapeamento: sem integração, a versão é `0`
        raise _version_conflict(integration_config.Conflict(None)) from None
    except actions.TokenUnreadable as unreadable:
        raise ApiError("state_conflict", str(unreadable)) from None
    except actions.ReadFailed as unread:
        raise ApiError("upstream_failed", str(unread)) from None
    except actions.ReportPending as pending:
        raise ApiError(
            "validation_failed",
            "A integração só liga com o relatório do mapeamento limpo, e ele tem pendências; veja "
            "cada uma em `errors`. Nada mudou.",
            errors=[ErrorItem(field="mapping", message=reason) for reason in pending.reasons()],
        ) from None
    except integration_config.Conflict as conflict:
        raise _version_conflict(conflict) from None
    return BaserowConnection.of(activated, token_unreadable=False)


@router.operation(
    "POST",
    "/forms/{slug}/integrations/baserow/deactivate",
    operation_id="deactivate_form_integration",
    role="total",
    errors={
        409: "A integração mudou desde que você a leu, ou já não existe (`version_conflict`, com a "
        "versão de agora; `0` se não existe): nada muda. Repetir a chamada depois de uma resposta "
        "perdida cai aqui, sem efeito duplicado."
    },
    summary="Desativar a integração de um formulário com o Baserow",
    description=(
        "Desliga a integração do formulário `slug`: os pedidos que ficarem prontos deixam de virar "
        "linha na tabela, e as sincronizações que ainda estavam repetindo esperam a reativação (não "
        "se perdem). **Não fala com o Baserow** e funciona mesmo com o token guardado que não abre; "
        "por isso a operação não tem marca nem pede `confirm`. `expected_version` é a `version` que "
        "`get_form_integration` devolveu; se mudou, ou se a integração foi removida (versão `0`), "
        "`409 version_conflict` e nada muda. A desativação fica na auditoria em nome da chave "
        "(`integration.deactivate`). Para ligar de novo, `activate_form_integration`."
    ),
    returns="A conexão como ficou (desativada), com a versão nova e sem o token.",
    response_model=BaserowConnection,
)
def deactivate_form_integration(
    slug: FORM, body: DeactivateFormIntegrationRequest, key: CurrentKey
) -> BaserowConnection:
    _ensure_form(slug)
    deps = get_deps()
    try:
        deactivated = actions.deactivate(
            deps,
            slug,
            expected_version=actions.version_number(body.expected_version),
            actor=key.actor,
        )
    except integration_config.Conflict as conflict:
        raise _version_conflict(conflict) from None
    with deps.sessions() as session:
        unreadable = integration_config.token_unreadable(session, deps.cipher, slug)
    return BaserowConnection.of(deactivated, token_unreadable=unreadable)


# --- remover ------------------------------------------------------------------------------------

REMOVE_TEXT = (
    "Remover a integração apaga o token do Baserow e o estado das sincronizações (as que falharam "
    "deixam de poder ser reenviadas). As linhas que já estão na tabela ficam. Não tem volta: para "
    "sincronizar de novo é preciso conectar, mapear e ativar outra vez."
)


@router.operation(
    "DELETE",
    "/forms/{slug}/integrations/baserow",
    operation_id="remove_form_integration",
    role="total",
    destructive=True,
    confirmation_text=REMOVE_TEXT,
    errors={
        409: "A integração mudou desde que você a leu, ou já não existe (`version_conflict`, com a "
        "versão de agora; `0` se não existe): nada é removido. Repetir a chamada depois de uma "
        "resposta perdida cai aqui, sem efeito duplicado."
    },
    summary="Remover a integração de um formulário com o Baserow",
    description=(
        "Remove a integração do formulário `slug`: apaga o **token** do Baserow e o **estado das "
        "sincronizações** (o que já foi sincronizado, o que está pendente e o que falhou: as falhas "
        "deixam de poder ser reenviadas). **As linhas que já estão na tabela do Baserow ficam**: o "
        "app não apaga nada lá. Não tem volta: para sincronizar de novo é preciso conectar "
        "(`connect_form_integration`, com o token de novo), mapear e ativar outra vez, por isso a "
        "operação é marcada e exige `confirm=true` na query (sem ele, `422 confirmation_required` e "
        "nada acontece). `expected_version` é a `version` que `get_form_integration` devolveu, "
        "também na query; se mudou, ou se a integração já foi removida (versão `0`), `409 "
        "version_conflict` e nada é removido. Não fala com o Baserow. A remoção fica na auditoria em "
        "nome da chave (`integration.remove`), sem o token."
    ),
    returns="A integração que saiu, como estava, e o estado das sincronizações que saíram com ela.",
    response_model=RemovedFormIntegration,
)
def remove_form_integration(
    slug: FORM,
    expected_version: ExpectedVersionQuery,
    key: CurrentKey,
    confirm: ConfirmQuery = False,
) -> RemovedFormIntegration:
    _ensure_form(slug)
    try:
        removed = actions.remove(
            get_deps(),
            slug,
            expected_version=actions.version_number(expected_version),
            actor=key.actor,
        )
    except integration_config.Conflict as conflict:
        raise _version_conflict(conflict) from None
    return RemovedFormIntegration.of(removed)


# --- testar -------------------------------------------------------------------------------------

TEST_TEXT = (
    "O envio de teste escreve (ou atualiza) uma linha de exemplo na planilha do cliente no Baserow."
)


@router.operation(
    "POST",
    "/forms/{slug}/integrations/baserow/test",
    operation_id="send_form_integration_test",
    role="total",
    external_effect=True,
    confirmation_text=TEST_TEXT,
    heavy=True,
    errors={
        409: "O formulário ainda não tem integração, ou o token guardado não abre com a "
        "`WEBHOOK_ENCRYPTION_KEY` atual (`state_conflict`): nada é mandado ao Baserow."
    },
    summary="Mandar a linha de teste da integração com o Baserow",
    description=(
        "Manda para a tabela do Baserow a linha de teste do formulário `slug`: uma lead de exemplo "
        "(`Contadora Exemplo`, no domínio `example.test`, com respostas inventadas), montada pelo "
        "mapeamento guardado e escrita pelo mesmo caminho de um pedido de verdade, com o código do "
        "pedido `teste-<slug>`. **Não leva dado de lead de verdade, não passa pelo Hatchet e não "
        "mexe nas sincronizações.** A linha fica na tabela para você conferi-la lá, e **chamar de "
        "novo atualiza a mesma linha** (`outcome` `updated`): o app não a apaga. Chama o Baserow "
        "(custa 5 fichas do limite de uso) e escreve na planilha do cliente, por isso a operação é "
        'marcada e exige `"confirm": true` em toda chamada (sem ele, `422 confirmation_required` e '
        "nada acontece). **Um teste que o Baserow recusou não é um erro da chamada**: a resposta é "
        "`200` com `ok` falso e o motivo em `error` (o token foi recusado, a tabela mudou, o "
        "mapeamento está vazio ou sem o código do pedido, o formulário não tem versão publicada), e "
        "nenhuma linha é gravada. Sem integração, ou com o token guardado que não abre, é `409 "
        "state_conflict`. O resultado, passando ou não, fica na auditoria em nome da chave "
        "(`integration.test`), sem dado de lead. Não pede versão: repetir é inofensivo."
    ),
    returns="O resultado do teste: se passou, o que aconteceu com a linha e o motivo se não passou.",
    response_model=SentFormIntegrationTest,
)
def send_form_integration_test(
    slug: FORM, body: SendFormIntegrationTestRequest, key: CurrentKey
) -> SentFormIntegrationTest:
    _ensure_form(slug)
    try:
        result = actions.send_test(get_deps(), slug, actor=key.actor, profile=body.profile)
    except actions.IntegrationMissing as missing:
        raise ApiError("state_conflict", str(missing)) from None
    except actions.TokenUnreadable as unreadable:
        raise ApiError("state_conflict", str(unreadable)) from None
    return SentFormIntegrationTest.of(result)


# --- reenviar as que falharam -------------------------------------------------------------------

RESEND_TEXT = (
    "Reenviar põe de volta na fila até 200 sincronizações que falharam e as dispara: cada uma escreve "
    "uma linha na planilha do cliente no Baserow."
)


@router.operation(
    "POST",
    "/forms/{slug}/integrations/baserow/resend-failed",
    operation_id="resend_failed_form_integration_syncs",
    role="total",
    external_effect=True,
    confirmation_text=RESEND_TEXT,
    errors={409: "O formulário ainda não tem integração (`state_conflict`): nada acontece."},
    summary="Reenviar as sincronizações que falharam da integração com o Baserow",
    description=(
        "Põe de volta na fila as sincronizações do formulário `slug` que **falharam** (`failed`) e "
        "as dispara de novo: o que se faz depois de corrigir o token, a tabela ou o mapeamento. "
        "Reenvia **até 200 por chamada, as mais antigas primeiro** (`limit`); se `still_failed` não "
        "é zero, repita a chamada. Cada uma, ao rodar, escreve (ou atualiza) uma linha na planilha "
        "do cliente, com a conexão e o mapeamento que valem agora, por isso a operação é marcada e "
        'exige `"confirm": true` em toda chamada (sem ele, `422 confirmation_required` e nada '
        "acontece: nem fila, nem auditoria, nem disparo). As sincronizações voltam para `pending` "
        "na hora, e o disparo segue em segundo plano, uma a uma. A resposta diz quantas voltaram à "
        "fila (`resent`) e `launched`: `true` se o motor de execução recebeu o disparo da primeira, "
        "`false` se não respondeu a tempo. **`false` não é erro**: elas já estão na fila e o "
        "reconciliador as dispara em até poucos minutos; não reenvie de novo. Sem nenhuma falhada, "
        "`resent` é zero e nada é gravado, auditado nem disparado, o que torna seguro repetir a "
        "chamada depois de uma resposta perdida. A auditoria guarda uma entrada em nome da chave "
        "(`sync.resend_failed`) com a contagem. Para reenviar a sincronização de um pedido só, "
        "`resend_job_sync`."
    ),
    returns=(
        "Quantas sincronizações voltaram à fila, quantas seguem falhadas e se o disparo chegou ao "
        "motor."
    ),
    response_model=ResentFailedFormIntegrationSyncs,
)
async def resend_failed_form_integration_syncs(
    slug: FORM, body: ResendFailedFormIntegrationSyncsRequest, key: CurrentKey
) -> ResentFailedFormIntegrationSyncs:
    deps = get_deps()
    await run_in_threadpool(_ensure_form, slug)
    try:
        done = await run_in_threadpool(actions.resend_failed, deps, slug, actor=key.actor)
    except actions.IntegrationMissing as missing:
        raise ApiError("state_conflict", str(missing)) from None
    launched = await launching.launch_syncs(deps, done.sync_ids)  # falha: o reconciliador
    return ResentFailedFormIntegrationSyncs.of(done, launched=launched)
