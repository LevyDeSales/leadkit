"""As operações das chaves de API: `get_current_api_key`, `list_api_keys`, `create_api_key` e
`revoke_api_key`. Chamam o serviço `app.admin.api.keys`, o mesmo da página `/admin/api-keys`: a regra
é uma só, e a API só a traduz em JSON e status. A auditoria sai do serviço, com o autor
`chave:<nome>` da chave que chamou.

Como uma chave `total` cria outras chaves `total`, `create_api_key` e `revoke_api_key` pedem `total`.
Pela API, criar uma chave é uma ação marcada (`x-external-effect`) e exige `"confirm": true` em toda
chamada: uma chave `total` nas mãos de um agente, levada por injeção de prompt, criaria outra sem
ninguém ver. A página não pede, porque quem a usa é a operadora logada, que vê o que está criando.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Path, Request

from app.admin.api import keys
from app.admin.api.common import PageParams, now, page_params, paginate
from app.admin.api.errors import ApiError, ErrorItem
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.api_keys import (
    ApiKey,
    ApiKeyPage,
    CreateApiKeyRequest,
    CreatedApiKey,
    RevokeApiKeyRequest,
)
from app.admin.api.security import CurrentKey
from app.deps import get_deps

logger = logging.getLogger(__name__)

TAG = "Chaves de API"
router = OperationRouter(
    tag=TAG,
    description=(
        "As chaves que autenticam esta API: quem está chamando, e listar, criar e revogar "
        "chaves. Os segredos saem só na criação."
    ),
)


@router.operation(
    "GET",
    "/me",
    operation_id="get_current_api_key",
    role="leitura",
    summary="Quem está chamando",
    description=(
        "Devolve a chave que fez a chamada: nome, papel, validade e último uso (nunca o segredo). "
        "É o primeiro teste de uma chave nova: se responde, a chave vale e o papel é o que está "
        "no campo `role`. Não grava nada."
    ),
    returns="A chave que fez a chamada.",
    response_model=ApiKey,
)
def get_current_api_key(key: CurrentKey, request: Request) -> ApiKey:
    return ApiKey.of(key, now(request))


@router.operation(
    "GET",
    "/api-keys",
    operation_id="list_api_keys",
    role="leitura",
    summary="Listar as chaves de API",
    description=(
        "Lista todas as chaves, as revogadas também, da mais nova para a mais antiga: nome, papel, "
        "quem criou, validade, último uso e situação (`active`, `expired` ou `revoked`). Nunca traz "
        "o segredo, que só aparece na criação. Não grava nada."
    ),
    returns="Uma página de chaves, com `next_cursor` para a seguinte.",
    response_model=ApiKeyPage,
)
def list_api_keys(
    request: Request, page: Annotated[PageParams, Depends(page_params)]
) -> ApiKeyPage:
    with get_deps().sessions() as session:
        infos = keys.listing(session)
    moment = now(request)
    chunk, next_cursor = paginate(infos, page, key=lambda info: info.key_id)
    return ApiKeyPage(items=[ApiKey.of(info, moment) for info in chunk], next_cursor=next_cursor)


@router.operation(
    "POST",
    "/api-keys",
    operation_id="create_api_key",
    role="total",
    external_effect=True,
    status_code=201,
    summary="Criar uma chave de API",
    description=(
        "Cria uma chave com nome e papel (`leitura` só consulta; `total` faz tudo o que o admin "
        "logado faz) e, se quiser, uma validade. A chave inteira (`key`) aparece **só nesta "
        "resposta**: guarde-a, porque não há como recuperá-la; se a resposta se perder, revogue "
        "a chave e crie outra. A criação fica na auditoria em nome da chave que chamou. "
        "Uma chave `total` cria outras chaves `total`; por isso a operação é marcada e exige "
        '`"confirm": true` em toda chamada, de qualquer papel. Sem ele, nada é criado. '
        "O nome não muda nem se reaproveita, nem o de uma chave revogada."
    ),
    returns="A chave criada e, só desta vez, a chave inteira em `key`.",
    response_model=CreatedApiKey,
)
def create_api_key(body: CreateApiKeyRequest, key: CurrentKey, request: Request) -> CreatedApiKey:
    moment = now(request)
    try:
        with get_deps().sessions.begin() as session:
            created = keys.create(
                session,
                name=body.name,
                role=body.role,
                actor=key.actor,
                expires_at=body.expires_at,
                now=moment,
            )
    except keys.KeyRefused as refused:
        raise ApiError(
            "validation_failed",
            "A chave não foi criada; veja o motivo em `errors`.",
            errors=[ErrorItem(field=refused.field, message=refused.message)],
        ) from None
    logger.info(
        "api: chave de API criada nome=%s papel=%s por=%s",
        created.info.name,
        created.info.role,
        key.actor,
    )
    return CreatedApiKey.from_created(created, moment)


@router.operation(
    "POST",
    "/api-keys/{key_id}/revoke",
    operation_id="revoke_api_key",
    role="total",
    destructive=True,
    errors={
        409: (
            "A chave já estava revogada (`state_conflict`): a primeira revogação segue valendo. "
            "Esta operação não pede versão."
        )
    },
    summary="Revogar uma chave de API",
    description=(
        "Revoga a chave: ela deixa de valer na próxima chamada, sem volta (para ter uma chave "
        "igual, crie outra). Vale também para a chave que está chamando: ela se revoga, e a "
        "chamada seguinte dela responde `401 key_revoked`. A revogação fica na auditoria em nome "
        "da chave que chamou. Uma chave `total` pode ter criado outras: revogar uma não revoga as "
        "que ela criou, e a auditoria (`api_key.create`) diz quais foram. Exige "
        '`"confirm": true`. Se a chave já estava revogada, a resposta é `409 state_conflict` e a '
        "primeira revogação segue valendo."
    ),
    returns="A chave como ficou: `state` revoked, com `revoked_at` e `revoked_by`.",
    response_model=ApiKey,
)
def revoke_api_key(
    key_id: Annotated[
        str, Path(description="O id público da chave (`key_id`), como `list_api_keys` o mostra.")
    ],
    body: RevokeApiKeyRequest,
    key: CurrentKey,
    request: Request,
) -> ApiKey:
    moment = now(request)
    try:
        with get_deps().sessions.begin() as session:
            revoked = keys.revoke(session, key_id, actor=key.actor, now=moment)
    except keys.UnknownKey:
        raise ApiError("not_found", "Chave não encontrada.") from None
    except keys.AlreadyRevoked as already:
        raise ApiError(
            "state_conflict",
            f"A chave «{already.key.name}» já estava revogada (por {already.key.revoked_by}); "
            "a primeira revogação segue valendo.",
        ) from None
    logger.info("api: chave de API revogada nome=%s por=%s", revoked.name, key.actor)
    return ApiKey.of(revoked, moment)
