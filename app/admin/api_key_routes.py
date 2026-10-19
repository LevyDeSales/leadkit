"""As rotas das chaves de API no admin (`/admin/api-keys`): listar, criar e revogar. A regra de
negócio é de `app.admin.api.keys`; a tela, de `app.admin.api_key_pages`.

Mesmo guarda do resto do admin: sessão, token CSRF e origem nos POST, cabeçalhos de privacidade
(`AdminRoute`, que inclui o `Cache-Control: no-store`). A chave inteira aparece só na resposta do
POST que a cria: não há redirecionamento, que a deixaria na lista, e recarregar a página reenvia o
formulário, que o nome repetido recusa. A página monta-se em `app.admin.install`, ao lado do
roteador principal.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.admin import api_key_pages as pages
from app.admin.api import keys
from app.admin.auth import AdminSession, instant
from app.admin.routes import AdminRoute, NotFound, _error, _page, require_csrf, require_session
from app.admin.views import moment
from app.deps import get_deps

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", route_class=AdminRoute, dependencies=[Depends(require_csrf)])


def _now(request: Request) -> datetime:
    """O instante pelo relógio do app (o dos testes é trocável), como a sessão e o bloqueio."""
    return instant(request.app.state.admin_clock)


def _keys_page(
    request: Request,
    *,
    status_code: int = 200,
    name: str = "",
    role: str = "leitura",
    validity_days: str = str(pages.DEFAULT_VALIDITY_DAYS),
    errors: dict[str, str] | None = None,
    revoke_errors: dict[str, str] | None = None,
    new_key: pages.NewKey | None = None,
) -> Response:
    """A página das chaves. Num pedido do HTMX, uma recusa sai com 200: o HTMX não troca o trecho da
    página numa resposta 4xx, e a pessoa não veria o motivo."""
    with get_deps().sessions() as db:
        infos = keys.listing(db)
    page = pages.keys_page(
        infos,
        _now(request),
        name=name,
        role=role,
        validity_days=validity_days,
        errors=errors,
        revoke_errors=revoke_errors,
        new_key=new_key,
    )
    if request.headers.get("hx-request") == "true":
        status_code = 200
    return _page(request, "admin/api_keys.html", {"page": page}, status_code=status_code)


@router.get("/api-keys")
def api_key_list(request: Request) -> Response:
    return _keys_page(request)


def create_from_form(
    request: Request, session: AdminSession, *, name: str, role: str, validity_days: str
) -> pages.NewKey | dict[str, str]:
    """Cria a chave que a operadora pediu num formulário. A página das chaves e a tela do MCP passam
    por aqui, e por isso a regra, a auditoria e o log são os mesmos. Devolve a chave nova, com o
    segredo, ou, se a criação foi recusada, o problema de cada campo (`name`, `role`,
    `validity_days`); recusada, nada é gravado."""
    now = _now(request)
    try:
        expires = pages.expires_at(validity_days.strip(), now)
        with get_deps().sessions.begin() as db:
            created = keys.create(
                db,
                name=name.strip(),
                role=role,
                actor=session.username,
                expires_at=expires,
                now=now,
            )
    except keys.KeyRefused as refused:
        field = "validity_days" if refused.field == "expires_at" else refused.field
        return {field: refused.message}
    logger.info("admin: chave de API criada nome=%s papel=%s", created.info.name, created.info.role)
    return pages.new_key(created)


@router.post("/api-keys")
def create_api_key(
    request: Request,
    session: Annotated[AdminSession, Depends(require_session)],
    name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    validity_days: Annotated[str, Form()] = "",
) -> Response:
    """Cria a chave e responde com a página, que mostra a chave inteira uma única vez. Recusada,
    a página volta com o que a operadora digitou e o problema de cada campo, e nada é gravado."""
    created = create_from_form(request, session, name=name, role=role, validity_days=validity_days)
    if isinstance(created, dict):
        return _keys_page(
            request,
            status_code=422,
            name=name.strip(),
            role=role or "leitura",
            validity_days=validity_days.strip(),
            errors=created,
        )
    return _keys_page(request, new_key=created)


@router.post("/api-keys/{key_id}/revoke")
def revoke_api_key(
    request: Request,
    key_id: str,
    session: Annotated[AdminSession, Depends(require_session)],
    confirm: Annotated[str, Form()] = "",
) -> Response:
    """Revoga a chave, com a confirmação de quem revoga: ela deixa de valer na próxima chamada."""
    if re.fullmatch(r"[0-9a-f]{8}", key_id) is None:
        raise NotFound(pages.KEY_NOT_FOUND)
    if not confirm:
        return _keys_page(request, status_code=422, revoke_errors={key_id: pages.CONFIRM_REVOKE})
    try:
        with get_deps().sessions.begin() as db:
            revoked = keys.revoke(db, key_id, actor=session.username, now=_now(request))
    except keys.UnknownKey:
        return _error(request, 404, pages.KEY_NOT_FOUND)
    except keys.AlreadyRevoked as already:
        when = moment(already.key.revoked_at)
        session.flash = (
            "err",
            pages.ALREADY_REVOKED.format(
                name=already.key.name,
                by=already.key.revoked_by,
                when=when.text if when else "—",
            ),
        )
        return RedirectResponse(pages.KEYS_URL, status_code=303)
    logger.info("admin: chave de API revogada nome=%s", revoked.name)
    session.flash = ("ok", pages.REVOKED.format(name=revoked.name))
    return RedirectResponse(pages.KEYS_URL, status_code=303)
