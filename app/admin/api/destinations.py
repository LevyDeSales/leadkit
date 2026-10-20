"""As operações dos destinos do webhook: `list_destinations`, `get_destination`, `save_destination`,
`rotate_destination_secret`, `remove_destination` e `send_destination_test_event`.

São uma família só, com o escopo no caminho (`{scope}`): a palavra `default` (o destino padrão) ou o
endereço do formulário. O admin repete cada ação para os dois, com a mesma função; aqui há uma operação
por ação. Elas leem pela mesma consulta (`app.admin.queries.destinations`) e agem pelas mesmas ações
(`app.admin.actions.destinations`) que as páginas `/admin/destinations/...`: a conferência de cada coisa
é uma só, e a API a traduz em JSON e status. A auditoria sai do serviço (`app.destinations`), com o autor
`chave:<nome>` da chave que chamou.

**O segredo só sai na criação e na troca**, na resposta daquela chamada (`Cache-Control: no-store`, como
em toda resposta da API). Nenhuma leitura, log, auditoria nem mensagem de erro o traz. **A URL** do destino
vai inteira para a chave `total`; a `leitura` recebe só o esquema e o host, porque o caminho costuma valer
como senha.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Path

from app import destinations
from app.admin.actions import destinations as actions
from app.admin.actions.integrations import version_number
from app.admin.api import keys
from app.admin.api.common import (
    ConfirmQuery,
    ExpectedVersionQuery,
    PageParams,
    page_params,
    paginate,
)
from app.admin.api.errors import ApiError, ErrorItem
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.destinations import (
    SCOPE_TEXT,
    DestinationDetail,
    DestinationPage,
    DestinationSummary,
    DestinationTestResult,
    RemovedDestination,
    RotatedDestination,
    RotateDestinationSecretRequest,
    SavedDestination,
    SaveDestinationRequest,
    SendDestinationTestEventRequest,
)
from app.admin.api.security import CurrentKey
from app.admin.queries import destinations as queries
from app.deps import Deps, get_deps

logger = logging.getLogger(__name__)

TAG = "Destinos do webhook"
router = OperationRouter(
    tag=TAG,
    description=(
        "Para onde vai o evento `plano.concluido` de cada formulário: o destino padrão e o destino "
        "próprio de um formulário, numa família só, com o escopo no caminho (`default` ou o endereço "
        "do formulário). Um formulário com destino próprio usa o dele; os outros usam o padrão; sem "
        "nenhum, o plano não é entregue. Ler é de `leitura` (que vê só o esquema e o host da URL, "
        "porque o caminho costuma valer como senha); criar, mudar, trocar o segredo, remover e mandar o "
        'evento de teste são de `total` e exigem `"confirm": true`. **O segredo do destino só aparece '
        "na criação e na troca**, na resposta daquela chamada: nenhuma leitura o devolve."
    ),
)

SCOPE = Annotated[str, Path(description=SCOPE_TEXT)]
NOT_FOUND = (
    "Escopo não encontrado: use `default` ou o endereço de um formulário que existe (`list_forms`)."
)


def resolve(deps: Deps, scope: str) -> actions.Target:
    """O escopo do caminho, ou `404` se não é `default` nem um formulário com destino possível. A
    mensagem nunca cita o que veio no caminho."""
    try:
        with deps.sessions() as session:
            return actions.target_of_scope(session, scope)
    except actions.UnknownScope:
        raise ApiError("not_found", NOT_FOUND) from None


def full_url(key: keys.KeyInfo) -> bool:
    """A URL inteira só para a chave `total`: o caminho de um webhook costuma valer como senha."""
    return keys.allows(key.role, "total")


# --- ler ----------------------------------------------------------------------------------------


@router.operation(
    "GET",
    "/destinations",
    operation_id="list_destinations",
    role="leitura",
    summary="Listar os destinos do webhook",
    description=(
        "Lista o escopo `default` (o destino padrão) e depois um escopo por formulário (os do "
        "registro, na ordem dele, e os que saíram dele mas ainda têm destino guardado), `limit` por "
        "página (de 1 a 200; padrão 50), com o cursor da seguinte em `next_cursor`. Cada item diz se "
        "o escopo tem destino próprio (`stored`), a `version` dele (`0` se não tem), **quem entrega "
        "os eventos dele** (`serving`: `form`, `default` ou `none`) e o host, e se o segredo dele não "
        "abre com a chave atual (`secret_unreadable`). `unserved` lista os formulários **sem destino "
        "nenhum**, cujos planos não são entregues, e `unreadable` os escopos cujo segredo não abre "
        "(as entregas para eles falham até alguém gerar um segredo novo): as duas listas são "
        "inteiras, qualquer que seja a página. Nunca traz a URL nem o segredo (`get_destination` traz "
        "a URL, conforme o papel). Não grava nada."
    ),
    returns="Uma página de escopos com quem entrega cada um, e os avisos de sem destino e de segredo que não abre.",
    response_model=DestinationPage,
)
def list_destinations(page: Annotated[PageParams, Depends(page_params)]) -> DestinationPage:
    deps = get_deps()
    with deps.sessions() as session:
        found = queries.overview(session, deps.forms.current_forms(), deps.cipher)
    unreadable = set(found.unreadable)
    rows = [
        DestinationSummary.of_default(found.default, unreadable=None in unreadable),
        *(
            DestinationSummary.of_form(row, unreadable=row.slug in unreadable)
            for row in found.forms
        ),
    ]
    chunk, next_cursor = paginate(rows, page, key=lambda row: row.scope)
    return DestinationPage.of(found, chunk, next_cursor)


@router.operation(
    "GET",
    "/destinations/{scope}",
    operation_id="get_destination",
    role="leitura",
    summary="Ler o destino do webhook de um escopo",
    description=(
        "O destino do escopo `scope`: `default` (o destino padrão) ou o endereço de um formulário. "
        "Traz o endereço, a `version`, a autoria e as datas, e se o segredo não abre com a chave "
        "atual (`secret_unreadable`); **nunca o segredo**, que só aparece na criação e na troca. "
        "**A chave `total` recebe a URL inteira; a `leitura`, só o esquema e o host** "
        "(`https://host`; `url_redacted` diz qual foi o caso), porque o caminho de um webhook "
        "costuma valer como senha. `serving` diz quem entrega os eventos do escopo (`form`: o destino "
        "próprio; `default`: o destino padrão; `none`: nenhum, os planos não são entregues). Um "
        'escopo sem destino próprio devolve `destination` nulo e `version` `"0"`, a que a criação '
        "manda em `expected_version`. Não grava nada."
    ),
    returns="O destino do escopo, com a URL conforme o papel, e quem entrega os eventos dele.",
    response_model=DestinationDetail,
)
def get_destination(scope: SCOPE, key: CurrentKey) -> DestinationDetail:
    deps = get_deps()
    target = resolve(deps, scope)
    with deps.sessions() as session:
        found = queries.detail(session, target.form_slug, target.title, deps.cipher)
    return DestinationDetail.of(found, full_url=full_url(key))


# --- criar ou mudar o endereço ------------------------------------------------------------------

DESTINATION_CHANGED = (
    "O destino mudou desde que você o leu (outra pessoa ou outra chamada). Nada foi gravado: leia de "
    "novo com `get_destination` e refaça a mudança."
)
DESTINATION_GONE = (
    "O destino não existe (foi removido, ou este escopo nunca teve um). Nada foi gravado: para criá-lo, "
    'mande `save_destination` com `expected_version` `"0"`.'
)
SECRET_TAKEN = (
    "O segredo já foi gerado logo antes por esta mesma chave, numa chamada repetida, e só apareceu na "
    "resposta daquela chamada: ele não volta. Nada foi gravado agora. Para ter um segredo que você "
    "veja, gere outro com `rotate_destination_secret` (o anterior deixa de valer)."
)
NO_CIPHER = (
    "O servidor está sem `WEBHOOK_ENCRYPTION_KEY`, e sem ela não há onde guardar um segredo. Nada foi "
    "gravado: quem administra o servidor precisa definir a chave no ambiente e reiniciar a API e o "
    "worker (como gerar a chave: app/README.md)."
)
HTTP_NEEDS_CONFIRMATION = (
    "Em produção, um endereço `http` manda os dados das leads sem criptografia. Prefira `https`, ou "
    "mande `confirm_http: true` para dizer que o receptor só aceita http. Nada foi feito."
)
URL_REFUSED = "O endereço não vale; veja o motivo em `errors`. Nada foi gravado."


def version_conflict(conflict: destinations.Conflict) -> ApiError:
    """O `409 version_conflict`, com a versão de agora (`0` se o destino não existe: é a versão da
    criação)."""
    if conflict.current is None:
        return ApiError("version_conflict", DESTINATION_GONE, current_version="0")
    return ApiError(
        "version_conflict", DESTINATION_CHANGED, current_version=str(conflict.current.version)
    )


def conflict_of(
    conflict: destinations.Conflict, key: keys.KeyInfo, expected: int, *, makes_secret: bool
) -> ApiError:
    """O conflito de uma escrita. Se a ação gera um segredo e o conflito é o da própria chave, logo
    depois da versão que ela viu (a chamada repetida), o segredo já existe e não volta: `409
    state_conflict`. Senão, `409 version_conflict`."""
    if actions.secret_already_shown(
        conflict, actor=key.actor, expected_version=expected, makes_secret=makes_secret
    ):
        return ApiError("state_conflict", SECRET_TAKEN)
    return version_conflict(conflict)


SAVE_TEXT = (
    "Gravar o receptor e sua finalidade define quem recebe os dados. Finalidade interna envia "
    "o plano integral, respostas e dados comerciais; participante envia somente o plano público "
    "e referências permitidas. Captura e Baserow exigem finalidade interna."
)


@router.operation(
    "PUT",
    "/destinations/{scope}",
    operation_id="save_destination",
    role="total",
    external_effect=True,
    confirmation_text=SAVE_TEXT,
    errors={
        409: "O destino mudou desde que você o leu, ou ainda não existe e a versão esperada não é `0` "
        "(`version_conflict`, com a versão de agora; `0` se ele não existe), ou o destino acabou de "
        "ser criado por esta mesma chave, numa chamada repetida (`state_conflict`: o segredo já foi "
        "gerado e não volta), ou o servidor está sem a `WEBHOOK_ENCRYPTION_KEY` e não guarda um "
        "segredo novo (`state_conflict`): nada é gravado.",
        422: "Falta `confirm: true` (`confirmation_required`), ou o endereço é `http` em produção "
        "sem `confirm_http: true` (`confirmation_required`), ou o corpo não vale "
        "(`validation_failed`, com o campo `url` quando o endereço não serve): nada é gravado.",
    },
    summary="Criar o destino do webhook de um escopo, ou mudar o endereço dele",
    description=(
        "Grava o endereço do receptor do escopo `scope` (`default` ou o endereço de um formulário). "
        '**Com `expected_version` `"0"`, cria o destino**: o servidor gera o segredo, e ele aparece '
        "**só nesta resposta** (`secret`, com `created` `true`), junto com `Cache-Control: no-store`; "
        "guarde-o e ponha-o no receptor, que confere com ele a assinatura (`X-MCC-Signature`) de cada "
        "evento. Se a resposta se perder, o segredo não volta: gere outro com "
        "`rotate_destination_secret`, como a página manda fazer. **Com a `version` que "
        "`get_destination` devolveu, muda o endereço** do destino que existe, e o segredo "
        "continua o mesmo (`secret` nulo). Se a versão mudou, `409 version_conflict` com a versão de "
        "agora; uma chamada repetida que cria o destino de novo é `409 state_conflict`, sem repetir o "
        "segredo. O endereço é `https` (ou `http`: em produção só passa com `confirm_http: true`, "
        "porque manda os dados das leads sem criptografia), com host, sem usuário e senha, sem `#` e "
        "sem IP link-local. **O destino define para onde vão os dados das leads**, por isso a "
        'operação é marcada e exige `"confirm": true` em toda chamada, inclusive na criação (sem ele, '
        "`422 confirmation_required` e nada acontece). Criar o destino padrão faz os formulários sem "
        "destino próprio passarem a entregar nele. A mudança fica na auditoria em nome da chave "
        "(`webhook_destination.create` ou `webhook_destination.update`), com o host e sem o segredo "
        "nem o caminho da URL. Sem `WEBHOOK_ENCRYPTION_KEY` no servidor, criar é `409 state_conflict`."
    ),
    returns="O destino como ficou, com a URL inteira e, só na criação, o segredo.",
    response_model=SavedDestination,
)
def save_destination(
    scope: SCOPE, body: SaveDestinationRequest, key: CurrentKey
) -> SavedDestination:
    deps = get_deps()
    target = resolve(deps, scope)
    expected = version_number(body.expected_version)
    try:
        saved = actions.save(
            deps,
            target.form_slug,
            url=body.url,
            actor=key.actor,
            expected_version=expected,
            http_confirmed=body.confirm_http,
            events=body.events,
            purpose=body.purpose,
        )
    except destinations.InvalidPurpose as invalid:
        raise ApiError(
            "validation_failed",
            invalid.message,
            errors=[ErrorItem(field="purpose", message=invalid.message)],
        ) from None
    except destinations.InvalidEvents as invalid:
        raise ApiError(
            "validation_failed",
            "Confira os eventos aceitos pelo receptor.",
            errors=[ErrorItem(field="events", message=invalid.message)],
        ) from None
    except destinations.InvalidUrl as invalid:
        if invalid.needs_confirmation:
            raise ApiError("confirmation_required", HTTP_NEEDS_CONFIRMATION) from None
        raise ApiError(
            "validation_failed",
            URL_REFUSED,
            errors=[ErrorItem(field="url", message=invalid.message)],
        ) from None
    except destinations.NoCipher:
        raise ApiError("state_conflict", NO_CIPHER) from None
    except destinations.Conflict as conflict:
        raise conflict_of(conflict, key, expected, makes_secret=expected == 0) from None
    logger.info(
        "api: destino do webhook %s escopo=%s host=%s por=%s",
        "criado" if saved.secret is not None else "alterado",
        target.scope,
        saved.destination.host,
        key.actor,
    )
    with deps.sessions() as session:
        found = queries.detail(session, target.form_slug, target.title, deps.cipher)
    return SavedDestination.of_saved(saved, unreadable=found.unreadable)


# --- trocar o segredo ---------------------------------------------------------------------------

ROTATE_TEXT = (
    "Trocar o segredo faz o atual deixar de valer na hora: o receptor que ainda confere a assinatura "
    "de cada evento com ele passa a recusá-los até receber o novo."
)


@router.operation(
    "POST",
    "/destinations/{scope}/rotate-secret",
    operation_id="rotate_destination_secret",
    role="total",
    destructive=True,
    external_effect=True,
    confirmation_text=ROTATE_TEXT,
    errors={
        409: "O destino mudou desde que você o leu, ou já não existe "
        "(`version_conflict`, com a versão de agora; `0` se ele não existe), ou o segredo acabou de "
        "ser trocado por esta mesma chave, numa chamada repetida (`state_conflict`: o segredo novo "
        "já foi gerado e não volta), ou o servidor está sem a `WEBHOOK_ENCRYPTION_KEY` "
        "(`state_conflict`): nada é trocado.",
    },
    summary="Trocar o segredo do destino do webhook de um escopo",
    description=(
        "Gera um segredo novo para o destino do escopo `scope` (`default` ou o endereço de um "
        "formulário). **O novo aparece só nesta resposta** (`secret`, com `Cache-Control: "
        "no-store`): guarde-o e ponha-o no receptor agora. **O antigo deixa de valer na hora**: o "
        "envio seguinte, inclusive o de uma entrega em repetição, já sai assinado com o novo, e um "
        "receptor que ainda confere com o antigo passa a recusar os eventos. É também o que repara um "
        "destino cujo segredo não abre com a chave atual (`secret_unreadable`). `expected_version` é a "
        "`version` que `get_destination` devolveu; se mudou, `409 version_conflict` e o segredo não "
        "muda. Uma chamada repetida, com a versão de antes, é `409 state_conflict` e não repete o "
        "segredo novo: se você o perdeu, gere outro com a versão de agora. Por isso a operação é "
        'marcada e exige `"confirm": true` (sem ele, `422 confirmation_required` e nada acontece). A '
        "troca fica na auditoria em nome da chave (`webhook_destination.rotate_secret`), sem o segredo. "
        "Sem `WEBHOOK_ENCRYPTION_KEY` no servidor, é `409 state_conflict`."
    ),
    returns="O destino com a versão nova e, só desta vez, o segredo novo.",
    response_model=RotatedDestination,
)
def rotate_destination_secret(
    scope: SCOPE, body: RotateDestinationSecretRequest, key: CurrentKey
) -> RotatedDestination:
    deps = get_deps()
    target = resolve(deps, scope)
    expected = version_number(body.expected_version)
    try:
        rotated = actions.rotate(deps, target.form_slug, actor=key.actor, expected_version=expected)
    except destinations.NoCipher:
        raise ApiError("state_conflict", NO_CIPHER) from None
    except destinations.Conflict as conflict:
        raise conflict_of(conflict, key, expected, makes_secret=True) from None
    logger.info(
        "api: segredo do destino do webhook trocado escopo=%s por=%s", target.scope, key.actor
    )
    return RotatedDestination.of_rotated(rotated)


# --- remover ------------------------------------------------------------------------------------

REMOVE_TEXT = (
    "Remover o destino o apaga, com o segredo, e muda para onde vão os dados das leads: os formulários "
    "que dependiam dele passam a usar o destino padrão para novos eventos e retries PDF, se houver. "
    "Os registros de entrega de respostas vinculados serão apagados, inclusive os pendentes e já concluídos; as contagens aparecem na leitura e no retorno. Pendentes de captura não são reenviados nem transferidos ao padrão; as respostas salvas permanecem."
)


@router.operation(
    "DELETE",
    "/destinations/{scope}",
    operation_id="remove_destination",
    role="total",
    destructive=True,
    external_effect=True,
    confirmation_text=REMOVE_TEXT,
    errors={
        409: "O destino mudou desde que você o leu, ou já não existe "
        "(`version_conflict`, com a versão de agora; `0` se ele não existe): nada é removido.",
    },
    summary="Remover o destino do webhook de um escopo",
    description=(
        "Apaga o destino do escopo `scope` (`default` ou o endereço de um formulário), com o "
        "segredo. **Os formulários que dependiam dele deixam de ser entregues ali**: ao remover o "
        "destino de um formulário, ele passa a usar o destino padrão, se houver; ao remover o "
        "padrão, os formulários sem destino próprio **deixam de entregar** (os planos ficam sem "
        "destino até alguém criar outro). A resposta diz quem entrega o escopo agora (`serving`: "
        "`default` ou `none`) e traz o destino que saiu (`removed`), como estava. Não tem volta: para "
        "ter o destino de novo, crie-o com `save_destination` (e um segredo novo). Por isso a operação "
        "é marcada e exige `confirm=true` na query (sem ele, `422 confirmation_required` e nada "
        "acontece). `expected_version` é a `version` que `get_destination` devolveu, também na query; "
        "se o destino mudou ou já foi removido, `409 version_conflict` e nada é removido. A remoção "
        "fica na auditoria em nome da chave (`webhook_destination.remove`), com o host, sem o "
        "segredo nem o caminho da URL. Intenções de captura vinculadas são apagadas por cascade; a resposta informa as contagens, e pendentes não são enviadas nem transferidas ao padrão. Remove também o destino guardado de um formulário que saiu do "
        "código."
    ),
    returns="O destino removido, o fallback para novos eventos/PDF e as intenções de captura apagadas por estado; capturas antigas não são redirecionadas.",
    response_model=RemovedDestination,
)
def remove_destination(
    scope: SCOPE,
    expected_version: ExpectedVersionQuery,
    key: CurrentKey,
    confirm: ConfirmQuery = False,
) -> RemovedDestination:
    deps = get_deps()
    target = resolve(deps, scope)
    try:
        removed = actions.remove(
            deps,
            target.form_slug,
            actor=key.actor,
            expected_version=version_number(expected_version),
        )
    except destinations.Conflict as conflict:
        raise version_conflict(conflict) from None
    logger.info(
        "api: destino do webhook removido escopo=%s host=%s por=%s",
        target.scope,
        removed.destination.host,
        key.actor,
    )
    return RemovedDestination.of_removed(removed)


# --- o evento de teste --------------------------------------------------------------------------

NOTHING_TO_TEST = (
    "Este escopo não tem destino próprio, e o evento de teste só sai para o destino do próprio "
    "escopo (o padrão não entra no lugar do de um formulário). Nada foi enviado: crie o destino com "
    "`save_destination`, ou mande o teste ao escopo `default`."
)
UNREADABLE_SECRET = (
    "O segredo deste destino não abre com a `WEBHOOK_ENCRYPTION_KEY` atual, e o evento não sai sem "
    "assinatura. Nada foi enviado: confira a chave no ambiente do app ou, se ela se perdeu, gere um "
    "segredo novo com `rotate_destination_secret` e ponha-o no receptor."
)
TEST_TEXT = (
    "Mandar o evento de teste faz uma chamada de verdade ao receptor do destino: ele recebe um "
    "`webhook.teste`, assinado com o segredo, no endereço que está configurado."
)


@router.operation(
    "POST",
    "/destinations/{scope}/test-event",
    operation_id="send_destination_test_event",
    role="total",
    external_effect=True,
    heavy=True,
    confirmation_text=TEST_TEXT,
    errors={
        409: "O escopo não tem destino próprio (um formulário que usa o padrão testa o escopo "
        "`default`), ou o segredo do destino não abre com a `WEBHOOK_ENCRYPTION_KEY` atual "
        "(`state_conflict`): nada é enviado. Um receptor que recusa ou não responde **não** é "
        "erro: a resposta é `200` e `outcome` diz o que houve.",
    },
    summary="Mandar um evento de teste ao destino do webhook de um escopo",
    description=(
        "Manda um evento `webhook.teste` ao receptor do destino do escopo `scope` (`default` ou o "
        "endereço de um formulário), assinado com o segredo vigente como o `plano.concluido` (os "
        "cabeçalhos `X-MCC-Event`, `X-MCC-Timestamp`, `X-MCC-Signature` e `Idempotency-Key`), **sem "
        "nenhum dado de lead**, e devolve o que o receptor respondeu: o status, o tempo, o começo do "
        "corpo (até 500 caracteres) e `outcome`, o que um `plano.concluido` faria com essa resposta "
        "(`delivered`, `retry`, `rejected` ou `no_response`). Só testa o destino do próprio escopo: um "
        "formulário que usa o destino padrão testa o escopo `default`. Um receptor que responde "
        "erro, recusa ou não responde é um `200` com o resultado, e não um erro da API: o objetivo é "
        "ver o que ele diz. **A chamada sai da API, e as entregas saem do worker**, que pode não "
        "alcançar o que a API alcança: um teste que passa não garante que o worker chega ao endereço. "
        "Pode levar até uns 20 s com o receptor mudo, e custa 5 fichas do limite de uso. Por isso a "
        'operação é marcada e exige `"confirm": true` (sem ele, `422 confirmation_required` e nada é '
        "enviado). Fica na auditoria em nome da chave (`webhook_destination.test`), com o status e o "
        "tempo, sem a resposta, o segredo nem o caminho da URL. Repetir manda outro evento de teste."
    ),
    returns="O que o receptor respondeu ao evento de teste e o que uma entrega faria com essa resposta.",
    response_model=DestinationTestResult,
)
def send_destination_test_event(
    scope: SCOPE, body: SendDestinationTestEventRequest, key: CurrentKey
) -> DestinationTestResult:
    deps = get_deps()
    target = resolve(deps, scope)
    try:
        probe = destinations.send_test(deps, target.form_slug, actor=key.actor, event=body.event)
    except destinations.UnreadableSecret:
        raise ApiError("state_conflict", UNREADABLE_SECRET) from None
    except destinations.InvalidPurpose as exc:
        raise ApiError(
            "validation_failed",
            exc.message,
            errors=[ErrorItem(field="purpose", message=exc.message)],
        ) from None
    except destinations.UnsupportedEvent:
        raise ApiError(
            "state_conflict", "O destino não declarou aceitar o evento de captura."
        ) from None
    if probe is None:
        raise ApiError("state_conflict", NOTHING_TO_TEST)
    logger.info(
        "api: evento de teste do webhook escopo=%s status=%s erro=%s por=%s",
        target.scope,
        probe.status,
        probe.error,
        key.actor,
    )
    return DestinationTestResult.of(target.scope, probe)
