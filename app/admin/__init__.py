"""Painel administrativo (Jinja2 + HTMX seletivo): login, lista e detalhe de pedidos, reprocessar
e reenviar entregas, formulários, destinos do webhook, imagens dos formulários, chaves de API e a tela
de configurar o MCP."""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI

from app.admin.api_key_routes import router as api_key_router
from app.admin.asset_routes import router as asset_router
from app.admin.auth import AdminAuth, Clock
from app.admin.mcp_routes import router as mcp_router
from app.admin.routes import router
from app.settings import Settings

__all__ = ["install", "router"]


class _HideAdminQuery(logging.Filter):
    """O log de acesso do uvicorn traz a query string, e a busca do admin (`?q=`) pode ter nome ou
    e-mail de lead: nas rotas do admin ela sai como `?[removido]`."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) > 2 and isinstance(args[2], str):
            path, query, _ = args[2].partition("?")
            if query and (path == "/admin" or path.startswith("/admin/")):
                record.args = (*args[:2], f"{path}?[removido]", *args[3:])
        return True


_HIDE_ADMIN_QUERY = _HideAdminQuery()


def install(app: FastAPI, settings: Settings, *, clock: Clock = time.time) -> None:
    """Monta `/admin` no app. Sessão e bloqueio de login são deste app (os testes trocam o relógio)."""
    app.state.admin_auth = AdminAuth.from_settings(settings, clock=clock)
    app.state.admin_clock = clock
    app.include_router(router)
    app.include_router(asset_router)
    app.include_router(api_key_router)
    app.include_router(mcp_router)
    logging.getLogger("uvicorn.access").addFilter(_HIDE_ADMIN_QUERY)  # não duplica
