"""As operações das imagens dos formulários: `list_assets`, `get_asset`, `upload_asset`,
`replace_asset` e `remove_asset`.

Chamam o serviço que as páginas `/admin/assets` chamam (`app.forms.assets`) e leem o arquivo pela
mesma função (`app.admin.actions.assets`): a regra é uma só, e a API a traduz em JSON e status. A
auditoria sai do serviço, com o autor `chave:<nome>` da chave que chamou.

O envio e a troca são `multipart/form-data` (o arquivo vai numa parte do formulário) e são o único corpo
grande da API: o `BodySizeLimit` lhes dá o limite de imagem (`app.api.main.UPLOAD_ROUTES`), e a rota
autentica antes de ler o corpo (`app.admin.api.operations.OperationRoute`). Nada aqui traz os bytes da
imagem: as leituras são metadados.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import Depends, File, Path

from app.admin.actions import assets as actions
from app.admin.api.common import (
    ConfirmQuery,
    ExpectedVersionQuery,
    PageParams,
    page_params,
    paginate,
)
from app.admin.api.errors import ApiError, ErrorItem
from app.admin.api.operations import OperationRouter
from app.admin.api.schemas.assets import (
    Asset,
    AssetPage,
    ReplaceAssetRequest,
    ReplacedAsset,
    UploadAssetRequest,
    UploadedAsset,
)
from app.admin.api.security import CurrentKey
from app.deps import Deps, get_deps
from app.errors import RetryableError, TerminalError
from app.forms import assets as form_assets

logger = logging.getLogger(__name__)

TAG = "Imagens dos formulários"
router = OperationRouter(
    tag=TAG,
    description=(
        "As imagens que os templates do PDF e da prévia de um formulário pedem por `asset:<nome>`: a "
        "biblioteca geral (que todo formulário enxerga) e as de cada formulário. Ler é de `leitura`; "
        "enviar, trocar e remover são só de `total`. O envio e a troca são `multipart/form-data` com o "
        "arquivo (PNG, JPEG ou WebP, até 8 MB), que a API otimiza antes de guardar; a resposta traz "
        "o que a otimização fez (tamanho, tipo e dimensões antes e depois). Remover exige "
        '`"confirm": true`.'
    ),
)

ASSET = Annotated[
    str,
    Path(description="O id da imagem (`id`), como `list_assets` o mostra."),
]
NOT_FOUND = "Imagem não encontrada."
CHANGED = (
    "A imagem mudou desde que você a leu (outra chamada ou outra pessoa). Nada foi alterado: leia "
    "de novo com `get_asset` e refaça a mudança."
)
REFUSED = "A imagem não foi gravada; veja o motivo em `errors`."


def _asset_id(value: str) -> uuid.UUID:
    """O id do caminho; um que nem é um UUID é uma imagem que não existe, como na página."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ApiError("not_found", NOT_FOUND) from None


def _refused(refused: form_assets.AssetRefused) -> ApiError:
    return ApiError(
        "validation_failed",
        REFUSED,
        errors=[ErrorItem(field=refused.field, message=refused.message)],
    )


def _storage_failed(failed: RetryableError | TerminalError) -> ApiError:
    return ApiError("upstream_failed", form_assets.storage_failure(failed))


def _changed(deps: Deps, asset_id: uuid.UUID) -> ApiError:
    """O `409 version_conflict`, com a versão de agora; `404` se a imagem saiu no meio do caminho."""
    with deps.sessions() as session:
        now = form_assets.find(session, asset_id)
    if now is None:
        return ApiError("not_found", NOT_FOUND)
    return ApiError("version_conflict", CHANGED, current_version=now.ref.sha256)


def _exists(deps: Deps, asset_id: uuid.UUID) -> None:
    """`404` se não há a imagem. A troca confere o arquivo antes da imagem, e a página responde `404`
    a quem troca uma imagem que não existe, qualquer que seja o arquivo: a API faz o mesmo."""
    with deps.sessions() as session:
        if form_assets.find(session, asset_id) is None:
            raise ApiError("not_found", NOT_FOUND)


# --- ler ----------------------------------------------------------------------------------------


@router.operation(
    "GET",
    "/assets",
    operation_id="list_assets",
    role="leitura",
    summary="Listar as imagens dos formulários",
    description=(
        "Lista todas as imagens: a biblioteca geral primeiro (`form_slug` nulo), depois as de cada "
        "formulário, por nome, `limit` por página (de 1 a 200; padrão 50), com o cursor da seguinte "
        "em `next_cursor`. Cada item traz o nome (e como um template o pede, `asset:<nome>`), onde "
        "vale, o tipo, as dimensões, o tamanho enviado e o guardado (antes e depois da otimização), "
        "quanto encolheu, a versão (o sha256 do arquivo, para trocar ou remover), quem enviou e "
        "trocou, e as versões publicadas dos formulários que a usam (`uses`). Não traz os bytes da "
        "imagem. Não grava nada."
    ),
    returns="Uma página de imagens, com `next_cursor` para a seguinte (nulo na última).",
    response_model=AssetPage,
)
def list_assets(page: Annotated[PageParams, Depends(page_params)]) -> AssetPage:
    with get_deps().sessions() as session:
        found = form_assets.listing(session)
        chunk, next_cursor = paginate(found, page, key=lambda asset: str(asset.id))
        uses = form_assets.uses_of(session, [asset.id for asset in chunk])
    return AssetPage(
        items=[Asset.of(asset, uses.get(asset.id, [])) for asset in chunk], next_cursor=next_cursor
    )


@router.operation(
    "GET",
    "/assets/{asset_id}",
    operation_id="get_asset",
    role="leitura",
    summary="Ler uma imagem",
    description=(
        "A imagem de id `asset_id`: o que `list_assets` mostra de cada item. `version` é o sha256 do "
        "arquivo guardado e é o que `replace_asset` e `remove_asset` pedem em `expected_version`. "
        "`uses` diz quais versões publicadas a usam: enquanto houver uma, a imagem não pode ser "
        "removida. Não traz os bytes da imagem. Não grava nada."
    ),
    returns="A imagem, com onde vale, os tamanhos antes e depois da otimização e onde é usada.",
    response_model=Asset,
)
def get_asset(asset_id: ASSET) -> Asset:
    found = _asset_id(asset_id)
    with get_deps().sessions() as session:
        asset = form_assets.find(session, found)
        if asset is None:
            raise ApiError("not_found", NOT_FOUND)
        uses = form_assets.uses(session, found)
    return Asset.of(asset, uses)


# --- enviar e trocar ----------------------------------------------------------------------------

BODY_TOO_LARGE = (
    "O corpo passa do limite do envio de imagem (o arquivo de até 8 MB mais os campos do formulário) "
    "(`payload_too_large`): nada é gravado."
)
STORAGE_FAILED = (
    "O storage não guardou a imagem (`upstream_failed`): nada foi gravado, e repetir a chamada é "
    "seguro."
)


@router.operation(
    "POST",
    "/assets",
    operation_id="upload_asset",
    role="total",
    heavy=True,
    status_code=201,
    errors={
        413: BODY_TOO_LARGE,
        422: (
            "O formulário não vale (`validation_failed`; `errors` traz o campo): falta o arquivo "
            "(`file`), o arquivo não é uma imagem PNG, JPEG ou WebP aceita ou passa de 8 MB (`file`), "
            "o nome não vale ou já existe no escopo (`name`), ou o formulário de `scope` não existe. "
            "Nada é gravado."
        ),
        502: STORAGE_FAILED,
    },
    summary="Enviar uma imagem",
    description=(
        "Envia uma imagem nova, como `multipart/form-data` com três campos: `name` (o nome curto, "
        "único no escopo: minúsculas, números e hífen), `scope` (o endereço do formulário a que a "
        "imagem pertence; ausente ou vazio, a biblioteca geral) e `file` (o arquivo: PNG, JPEG ou "
        "WebP, até 8 MB). A API otimiza o arquivo (tira os metadados, limita o lado maior a 2000 "
        "pixels e guarda no formato que dá o menor arquivo), guarda no storage e devolve a imagem "
        "com o que a otimização fez (`optimization`: tamanho, tipo e dimensões antes e depois). O "
        "arquivo é guardado pelo conteúdo: a mesma imagem sob outro nome é outra imagem para os "
        "templates, mas um arquivo só no storage; sob o mesmo nome no mesmo escopo, a resposta é "
        "`422` (para mudar o conteúdo de uma imagem, use `replace_asset`). Um template a usa por "
        "`asset:<nome>`; um formulário só passa a mostrá-la numa versão publicada depois do envio. "
        "O envio fica na auditoria em nome da chave. Se o storage falhar, a resposta é `502 "
        "upstream_failed` e nada é gravado."
    ),
    returns="A imagem como ficou, com o que a otimização fez ao arquivo enviado.",
    response_model=UploadedAsset,
)
def upload_asset(body: Annotated[UploadAssetRequest, File()], key: CurrentKey) -> UploadedAsset:
    deps = get_deps()
    scope = body.scope.strip()
    try:
        done = form_assets.upload(
            deps,
            scope=scope,
            name=body.name,
            content=actions.content_of(body.file),
            actor=key.actor,
        )
    except form_assets.AssetRefused as refused:
        raise _refused(refused) from None
    except (RetryableError, TerminalError) as failed:
        raise _storage_failed(failed) from None
    logger.info(
        "api: imagem enviada nome=%s escopo=%s por=%s",
        done.asset.name,
        done.asset.form_slug or "geral",
        key.actor,
    )
    return UploadedAsset.from_upload(done, [])


@router.operation(
    "PUT",
    "/assets/{asset_id}/content",
    operation_id="replace_asset",
    role="total",
    heavy=True,
    errors={
        409: (
            "A imagem mudou desde que você a leu (`version_conflict`, com a versão de agora): nada é "
            "gravado. Leia de novo com `get_asset` e refaça a troca."
        ),
        413: BODY_TOO_LARGE,
        422: (
            "O formulário não vale (`validation_failed`; `errors` traz o campo): falta a versão "
            "esperada (`expected_version`) ou o arquivo, ou o arquivo não é uma imagem PNG, JPEG ou "
            "WebP aceita ou passa de 8 MB (`file`). Nada é gravado."
        ),
        502: STORAGE_FAILED,
    },
    summary="Trocar o conteúdo de uma imagem",
    description=(
        "Troca o arquivo da imagem `asset_id`, mantendo o id e o nome, como `multipart/form-data` "
        "com `expected_version` (a `version` que `get_asset` devolveu) e `file` (PNG, JPEG ou WebP, "
        "até 8 MB, otimizado como no envio). Se a imagem mudou desde que você a leu, a resposta é "
        "`409 version_conflict` e nada é gravado. **As versões já publicadas seguem com o conteúdo "
        "antigo** (cada uma guarda o que usou, e o PDF de um pedido antigo sai igual): publique o "
        "rascunho do formulário para os envios novos usarem a imagem nova. Mandar o mesmo conteúdo "
        "que já está guardado não muda nada: a resposta traz `changed: false`, sem gravação nem "
        "auditoria. A troca fica na auditoria em nome da chave. Se o storage falhar, a resposta é "
        "`502 upstream_failed` e a imagem fica como estava."
    ),
    returns="A imagem como ficou, o que a otimização fez ao arquivo e se a troca mudou algo.",
    response_model=ReplacedAsset,
)
def replace_asset(
    asset_id: ASSET, body: Annotated[ReplaceAssetRequest, File()], key: CurrentKey
) -> ReplacedAsset:
    found = _asset_id(asset_id)
    deps = get_deps()
    _exists(deps, found)
    try:
        done = form_assets.replace(
            deps,
            found,
            actions.content_of(body.file),
            expected=body.expected_version,
            actor=key.actor,
        )
    except form_assets.UnknownAsset:
        raise ApiError("not_found", NOT_FOUND) from None
    except form_assets.AssetChanged:
        raise _changed(deps, found) from None
    except form_assets.AssetRefused as refused:
        raise _refused(refused) from None
    except (RetryableError, TerminalError) as failed:
        raise _storage_failed(failed) from None
    if done.changed:
        logger.info("api: imagem trocada nome=%s por=%s", done.asset.name, key.actor)
    with deps.sessions() as session:
        uses = form_assets.uses(session, found)
    return ReplacedAsset.from_replacement(done, uses)


# --- remover ------------------------------------------------------------------------------------

REMOVE_TEXT = (
    "Remover a imagem a tira do sistema e apaga o arquivo do storage, se nenhuma outra imagem nem "
    "versão publicada o usa."
)


@router.operation(
    "DELETE",
    "/assets/{asset_id}",
    operation_id="remove_asset",
    role="total",
    destructive=True,
    confirmation_text=REMOVE_TEXT,
    errors={
        409: (
            "A imagem mudou desde que você a leu (`version_conflict`, com a versão de agora), ou uma "
            "versão publicada a usa (`state_conflict`, e o texto diz quais versões): nada é removido. "
            "Para mudar uma imagem em uso, troque o arquivo com `replace_asset`."
        )
    },
    summary="Remover uma imagem",
    description=(
        "Remove a imagem `asset_id`: ela sai do sistema, e o arquivo sai do storage se nenhuma outra "
        "imagem nem versão publicada aponta para o mesmo conteúdo. Não tem volta (para ter a imagem "
        "de novo, envie o arquivo outra vez), por isso a operação é marcada e exige `confirm=true` "
        "na query (sem ele, `422 confirmation_required` e nada acontece). `expected_version` é a "
        "`version` que `get_asset` devolveu, também na query; se a imagem mudou, a resposta é `409 "
        "version_conflict`. **Uma imagem que uma versão publicada usa não sai**: a resposta é `409 "
        "state_conflict`, e o texto diz quais versões a usam (a lista também está em `uses` de "
        "`get_asset`). A remoção fica na auditoria em nome da chave."
    ),
    returns="A imagem que saiu, como estava antes de ser removida.",
    response_model=Asset,
)
def remove_asset(
    asset_id: ASSET,
    expected_version: ExpectedVersionQuery,
    key: CurrentKey,
    confirm: ConfirmQuery = False,
) -> Asset:
    found = _asset_id(asset_id)
    deps = get_deps()
    try:
        removed = form_assets.remove(deps, found, expected=expected_version, actor=key.actor)
    except form_assets.UnknownAsset:
        raise ApiError("not_found", NOT_FOUND) from None
    except form_assets.AssetChanged:
        raise _changed(deps, found) from None
    except form_assets.AssetInUse as refused:
        raise ApiError("state_conflict", refused.message()) from None
    logger.info("api: imagem removida nome=%s por=%s", removed.name, key.actor)
    return Asset.of(removed, [])
