"""O que os testes do MCP de admin dividem: o `openapi.json` do repositório, uma API de mentira em
memória (um `httpx.MockTransport`, sem rede) e a chave sorteada.

A chave é sorteada na execução, nunca literal: o GitGuardian do PR acusa valor com cara de segredo, e
nada com essa cara entra no repositório. Não é um arquivo de teste (o pytest não o coleta); os
arquivos `test_admin_mcp_*.py` o importam. O pacote `app.admin_mcp` não importa nada daqui: o
`FakeApi` mora no lado do teste.
"""

from __future__ import annotations

import copy
import functools
import importlib
import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.admin_mcp.config import API_PREFIX, Config

ROOT = Path(__file__).resolve().parent.parent
OPENAPI_FILE = ROOT / "docs" / "api" / "openapi.json"
ORIGIN = "https://planos.exemplo.test"

Reply = httpx.Response | Callable[[httpx.Request], httpx.Response]


def optional(name: str, *, module: bool = False) -> Any:
    """Importa um módulo do extra `mcp` (o SDK e o que ele traz, como o `jsonschema`). Sem o extra, o
    teste é pulado (`module=True`: o arquivo inteiro, para quem chama no topo dele); no CI (variável
    `CI`), a falta dele reprova, para o teste não passar em silêncio. Instalar:
    `uv sync --locked --extra dev --extra mcp`."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        if os.environ.get("CI"):
            raise
        pytest.skip(
            f"{name} não está instalado (uv sync --locked --extra dev --extra mcp)",
            allow_module_level=module,
        )


@functools.cache
def _document_text() -> str:
    return OPENAPI_FILE.read_text(encoding="utf-8")


def openapi_document() -> dict[str, Any]:
    """O `docs/api/openapi.json`, numa cópia nova a cada chamada (o teste pode mudá-la)."""
    found: dict[str, Any] = json.loads(_document_text())
    return found


def operation_ids(document: dict[str, Any]) -> set[str]:
    """Os `operationId` do documento, lidos do JSON cru (a fonte independente do gerador)."""
    return {
        operation["operationId"]
        for item in document["paths"].values()
        for method, operation in item.items()
        if method in ("get", "put", "post", "delete", "patch")
    }


def new_key() -> str:
    """Uma chave no formato da API (`gp_<id>_<segredo>`), sorteada na execução."""
    return f"gp_{secrets.token_hex(4)}_{secrets.token_urlsafe(32)}"


def make_config(key: str | None = None, **changes: Any) -> Config:
    config = Config(base_url=ORIGIN + API_PREFIX, api_key=key or new_key())
    return replace(config, **changes)


def answer(status: int = 200, body: Any = None, **headers: str) -> httpx.Response:
    """Uma resposta JSON (ou sem corpo) para o `FakeApi`."""
    if body is None:
        return httpx.Response(status, headers=headers)
    return httpx.Response(status, json=copy.deepcopy(body), headers=headers)


def error(status: int, code: str, detail: str, **more: Any) -> httpx.Response:
    return answer(status, {"code": code, "detail": detail, **more})


def pdf(content: bytes = b"%PDF-1.7 teste") -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": "application/pdf"})


def html(content: str = "<html><body>previa</body></html>") -> httpx.Response:
    return httpx.Response(200, text=content, headers={"content-type": "text/html; charset=utf-8"})


class FakeApi:
    """A API de admin de mentira: serve o `openapi.json` do repositório e o `/me`, confere o
    `Authorization: Bearer` de toda requisição e responde o que o teste combinou com `when`.

    `requests` guarda tudo o que chegou (o teste olha o método, o caminho, a query e o corpo). Uma
    requisição sem rota combinada é um `404`, e uma sem a chave certa, um `401`, como na API.
    """

    def __init__(
        self, key: str, role: str = "total", *, document: dict[str, Any] | None = None
    ) -> None:
        self.key = key
        self.role = role
        self.document = document if document is not None else openapi_document()
        self.requests: list[httpx.Request] = []
        self._routes: list[tuple[str, re.Pattern[str], Reply]] = []

    def when(self, method: str, path: str, reply: Reply) -> None:
        """Responde `reply` a `method path`. O `{nome}` do caminho vale por um segmento."""
        pattern = re.compile("^" + re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(path)) + "$")
        self._routes.insert(0, (method.upper(), pattern, reply))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def calls(self, method: str | None = None, path: str | None = None) -> list[httpx.Request]:
        """As requisições que chegaram, sem a do `openapi.json` e a do `/me` (a subida)."""
        found = [r for r in self.requests if _path_of(r) not in ("/openapi.json", "/me")]
        if method:
            found = [r for r in found if r.method == method.upper()]
        if path:
            found = [r for r in found if _path_of(r) == path]
        return found

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.key}":
            return error(401, "invalid_credentials", "Chave inválida.")
        path = _path_of(request)
        for method, pattern, reply in self._routes:
            if method == request.method and pattern.match(path):
                return reply(request) if callable(reply) else reply
        if request.method == "GET" and path == "/openapi.json":
            return answer(200, self.document)
        if request.method == "GET" and path == "/me":
            return answer(
                200,
                {
                    "key_id": "a1b2c3d4",
                    "name": "chave-de-teste",
                    "role": self.role,
                    "state": "active",
                    "created_by": "operadora",
                    "created_at": "2026-10-01T12:00:00Z",
                },
            )
        return error(404, "not_found", "Não há operação neste endereço.")


def _path_of(request: httpx.Request) -> str:
    """O caminho da requisição sem o prefixo da API e sem a query, como o servidor o enviou."""
    raw = request.url.raw_path.decode().split("?")[0]
    assert raw.startswith(API_PREFIX), raw
    return raw[len(API_PREFIX) :]


def body_of(request: httpx.Request) -> Any:
    """O corpo JSON de uma requisição."""
    return json.loads(request.content)


def query_of(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)
