"""As rotas das imagens dos formulários no admin (`/admin/assets`): listar, enviar, ver, trocar e
remover. A regra de negócio é de `app.forms.assets`; as telas, de `app.admin.asset_pages`.

Mesmo guarda do resto do admin: sessão, token CSRF e origem nos POST, cabeçalhos de privacidade
(`AdminRoute`). Os POST de envio e de troca são multipart, com o arquivo da imagem; o corpo deles
pode passar do limite dos outros (`app.api.main.UPLOAD_ROUTES`), por isso a sessão e a origem dos POST
são conferidas antes de o corpo ser lido (`AssetRoute`). A página monta-se em `app.admin.install`, ao
lado do roteador principal.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from app.admin import asset_pages
from app.admin.actions import assets as actions
from app.admin.auth import AdminSession
from app.admin.routes import (
    AdminRoute,
    NotFound,
    _error,
    _page,
    require_csrf,
    require_same_origin,
    require_session,
)
from app.deps import get_deps
from app.errors import RetryableError, TerminalError
from app.forms import assets as form_assets

logger = logging.getLogger(__name__)

ASSET_NOT_FOUND = "Imagem não encontrada. Confira o endereço e tente de novo."
CONFIRM_REMOVE = "Confirme que entende que a imagem sai do sistema."


class AssetRoute(AdminRoute):
    """O envio e a troca levam um corpo de até 8 MiB, que o FastAPI lê inteiro antes de rodar as
    dependências. Por isso a sessão e a origem são conferidas aqui, antes do corpo: quem não tem
    sessão ou vem de outra origem é recusado sem que o arquivo seja lido. O token CSRF viaja no
    próprio formulário, então só se confere depois (`require_csrf`)."""

    async def refuse_before_body(self, request: Request) -> None:
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return
        require_session(request)
        require_same_origin(request)


router = APIRouter(prefix="/admin", route_class=AssetRoute, dependencies=[Depends(require_csrf)])


def _list_page(
    request: Request,
    *,
    status_code: int = 200,
    scope: str = "",
    name: str = "",
    errors: dict[str, str] | None = None,
) -> Response:
    with get_deps().sessions() as db:
        page = asset_pages.assets_page(db, scope=scope, name=name, errors=errors)
    return _page(request, "admin/assets.html", {"page": page}, status_code=status_code)


@router.get("/assets")
def asset_list(request: Request) -> Response:
    return _list_page(request)


@router.post("/assets")
def upload_asset(
    request: Request,
    session: Annotated[AdminSession, Depends(require_session)],
    scope: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Envia uma imagem nova: otimiza, guarda no storage e abre o detalhe dela, com o que a
    otimização fez. Recusada, a lista volta com o que a operadora digitou e o problema de cada
    campo, e nada é gravado."""
    scope, name = scope.strip(), name.strip()
    try:
        done = form_assets.upload(
            get_deps(),
            scope=scope,
            name=name,
            content=actions.content_of(file),
            actor=session.username,
        )
    except form_assets.AssetRefused as refused:
        errors = {refused.field: refused.message}
        return _list_page(request, status_code=422, scope=scope, name=name, errors=errors)
    except (RetryableError, TerminalError) as failed:
        errors = {"file": form_assets.storage_failure(failed)}
        return _list_page(request, status_code=502, scope=scope, name=name, errors=errors)
    logger.info(
        "admin: imagem enviada nome=%s escopo=%s", done.asset.name, done.asset.form_slug or "geral"
    )
    result = asset_pages.optimization(done.optimization)
    session.flash = ("ok", asset_pages.UPLOADED.format(name=done.asset.name, result=result))
    return RedirectResponse(asset_pages.asset_url(done.asset.id), status_code=303)


def _asset_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise NotFound(ASSET_NOT_FOUND) from None


def _detail_page(
    request: Request,
    asset_id: uuid.UUID,
    *,
    status_code: int = 200,
    errors: dict[str, str] | None = None,
    message: str | None = None,
) -> Response:
    with get_deps().sessions() as db:
        asset = form_assets.find(db, asset_id)
        if asset is None:
            return _error(request, 404, ASSET_NOT_FOUND)
        page = asset_pages.asset_page(db, asset, errors=errors, message=message)
    return _page(request, "admin/asset.html", {"page": page}, status_code=status_code)


@router.get("/assets/{asset_id}")
def asset_detail(request: Request, asset_id: str) -> Response:
    return _detail_page(request, _asset_id(asset_id))


@router.post("/assets/{asset_id}/replace")
def replace_asset(
    request: Request,
    asset_id: str,
    session: Annotated[AdminSession, Depends(require_session)],
    expected: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Troca o conteúdo da imagem, mantendo o nome. Recusada, a página dela volta com o motivo,
    e nada é gravado. As versões publicadas seguem com o conteúdo antigo."""
    found = _asset_id(asset_id)
    try:
        done = form_assets.replace(
            get_deps(), found, actions.content_of(file), expected=expected, actor=session.username
        )
    except form_assets.UnknownAsset:
        return _error(request, 404, ASSET_NOT_FOUND)
    except form_assets.AssetChanged:
        return _detail_page(request, found, status_code=409, message=form_assets.CHANGED)
    except form_assets.AssetRefused as refused:
        return _detail_page(request, found, status_code=422, errors={"file": refused.message})
    except (RetryableError, TerminalError) as failed:
        errors = {"file": form_assets.storage_failure(failed)}
        return _detail_page(request, found, status_code=502, errors=errors)
    if done.changed:
        logger.info("admin: imagem trocada nome=%s", done.asset.name)
        result = asset_pages.optimization(done.optimization)
        session.flash = ("ok", asset_pages.REPLACED.format(name=done.asset.name, result=result))
    else:
        session.flash = ("ok", asset_pages.REPLACED_SAME)
    return RedirectResponse(asset_pages.asset_url(found), status_code=303)


@router.post("/assets/{asset_id}/remove")
def remove_asset(
    request: Request,
    asset_id: str,
    session: Annotated[AdminSession, Depends(require_session)],
    expected: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    """Remove a imagem, com a confirmação de quem remove. Uma versão publicada que a usa recusa."""
    found = _asset_id(asset_id)
    if not confirm:
        return _detail_page(request, found, status_code=422, errors={"remove": CONFIRM_REMOVE})
    try:
        removed = form_assets.remove(get_deps(), found, expected=expected, actor=session.username)
    except form_assets.UnknownAsset:
        return _error(request, 404, ASSET_NOT_FOUND)
    except form_assets.AssetChanged:
        return _detail_page(request, found, status_code=409, message=form_assets.CHANGED)
    except form_assets.AssetInUse as refused:
        errors = {"remove": refused.message()}
        return _detail_page(request, found, status_code=409, errors=errors)
    logger.info("admin: imagem removida nome=%s", removed.name)
    session.flash = ("ok", asset_pages.REMOVED.format(name=removed.name))
    return RedirectResponse(asset_pages.ASSETS_URL, status_code=303)
