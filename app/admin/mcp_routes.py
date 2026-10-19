"""As rotas da tela de configurar o MCP no admin (`/admin/mcp`): abrir a tela e criar uma chave ali
mesmo. A tela em si (os comandos, os avisos, a conferência) é de `app.admin.mcp_pages`; a regra das
chaves, de `app.admin.api.keys`, pelo mesmo caminho da página das chaves
(`app.admin.api_key_routes.create_from_form`): a validação, a auditoria e o log são os mesmos.

Mesmo guarda do resto do admin: sessão, token CSRF e origem nos POST, cabeçalhos de privacidade
(`AdminRoute`, que inclui o `Cache-Control: no-store`). O segredo da chave nova aparece só na
resposta do POST que a cria: sem redirecionamento, e recarregar a página mostra a tela sem ele.
A escolha (cliente, pastas, caminho do Python) viaja na query string do `GET`, que nunca leva o
segredo: ele fica no navegador, no `mcp.js`. A tela monta-se em `app.admin.install`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response

from app.admin import api_key_pages as key_pages
from app.admin import mcp_pages as pages
from app.admin.api import keys
from app.admin.api_key_routes import _now, create_from_form
from app.admin.auth import AdminSession
from app.admin.routes import AdminRoute, _page, require_csrf, require_session
from app.deps import get_deps

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", route_class=AdminRoute, dependencies=[Depends(require_csrf)])


def _counts(request: Request) -> pages.ToolCounts | None:
    """Quantas ferramentas cada papel recebe, contadas do `openapi.json` desta API na primeira vez e
    guardadas no app (o documento só muda com um deploy). Se a contagem falhar, a tela segue sem os
    números: é uma ajuda, e não pode derrubar a configuração."""
    state = request.app.state
    cached = getattr(state, "mcp_tool_counts", None)
    if cached is not None:
        return cached
    try:
        counts = pages.tool_counts(state.admin_api.openapi())
    except Exception:
        logger.warning("admin: não deu para contar as ferramentas do MCP", exc_info=True)
        return None
    state.mcp_tool_counts = counts
    return counts


def _mcp_page(
    request: Request,
    params: Mapping[str, Any],
    *,
    status_code: int = 200,
    new_key: key_pages.NewKey | None = None,
    name: str = "",
    role: str = "leitura",
    validity_days: str = str(key_pages.DEFAULT_VALIDITY_DAYS),
    errors: dict[str, str] | None = None,
) -> Response:
    """A tela. Num pedido do HTMX, uma recusa sai com 200: o HTMX não troca o trecho da página numa
    resposta 4xx, e a pessoa não veria o motivo."""
    deps = get_deps()
    with deps.sessions() as db:
        infos = keys.listing(db)
    page = pages.page(
        infos,
        _now(request),
        public_base_url=deps.settings.public_base_url,
        params={k: v for k, v in params.items() if isinstance(v, str)},
        counts=_counts(request),
        new_key=new_key,
        name=name,
        role=role,
        validity_days=validity_days,
        errors=errors,
    )
    if request.headers.get("hx-request") == "true":
        status_code = 200
    return _page(request, "admin/mcp.html", {"page": page}, status_code=status_code)


@router.get("/mcp")
def mcp_setup(request: Request) -> Response:
    return _mcp_page(request, request.query_params)


@router.post("/mcp/keys")
def create_mcp_key(
    request: Request,
    session: Annotated[AdminSession, Depends(require_session)],
    name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    validity_days: Annotated[str, Form()] = "",
    client: Annotated[str, Form()] = "",
    lead_data: Annotated[str, Form()] = "",
    upload_dir: Annotated[str, Form()] = "",
    download_dir: Annotated[str, Form()] = "",
    python: Annotated[str, Form()] = "",
) -> Response:
    """Cria a chave e responde com a tela, que a mostra inteira uma única vez e já a escolhe, com o
    segredo nos comandos. Recusada, a tela volta com o que a operadora digitou e o problema de cada
    campo, e nada é gravado."""
    choice = {
        "client": client,
        "lead_data": lead_data,
        "upload_dir": upload_dir,
        "download_dir": download_dir,
        "python": python,
    }
    created = create_from_form(request, session, name=name, role=role, validity_days=validity_days)
    if isinstance(created, dict):
        return _mcp_page(
            request,
            choice,
            status_code=422,
            name=name.strip(),
            role=role or "leitura",
            validity_days=validity_days.strip(),
            errors=created,
        )
    return _mcp_page(request, {**choice, "key": created.key_id}, new_key=created)
