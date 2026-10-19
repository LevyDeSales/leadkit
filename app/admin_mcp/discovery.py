"""A subida: ler o `openapi.json` da API viva, descobrir o papel da chave e escolher as ferramentas.

Duas chamadas, as duas com a chave: `GET /openapi.json` (o contrato que a API serve agora, e não uma
cópia velha) e `get_current_api_key` (`GET /me`, para o papel). O papel filtra o catálogo: uma chave
`leitura` registra só as ferramentas `leitura`. Qualquer falha aqui é um `StartupError` com uma
mensagem em português que diz o que conferir, sem a chave.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.admin_mcp import catalog
from app.admin_mcp.catalog import ToolSpec
from app.admin_mcp.client import ApiClient, ApiUnavailable, InputError, prepare
from app.admin_mcp.config import Config
from app.admin_mcp.results import render_error

logger = logging.getLogger("gerador_mcp")

OPENAPI_PATH = "/openapi.json"
ME = "get_current_api_key"


class StartupError(Exception):
    """O servidor não conseguiu começar. A mensagem diz o que conferir e nunca leva a chave."""


@dataclass(frozen=True)
class Discovery:
    """O que a subida descobriu: o papel da chave e as ferramentas que ele permite."""

    role: str
    tools: list[ToolSpec]
    skipped: dict[str, str]


async def discover(client: ApiClient, config: Config) -> Discovery:
    document = await _fetch_document(client)
    try:
        found = catalog.load(document)
    except catalog.CatalogError as problem:
        raise StartupError(f"O openapi.json da API não é o que o MCP espera: {problem}") from None
    for label, reason in found.skipped.items():
        # `%r`: o rótulo e o motivo podem citar texto do documento da API, com quebra de linha ou controle.
        logger.warning("Sem ferramenta para %r: %r", label, reason)
    me = found.tools.get(ME)
    if me is None:
        raise StartupError(
            f"O openapi.json da API não tem a operação `{ME}`: não sei o papel da chave."
        )
    role = await _role(client, me)
    tools = catalog.select(found.tools.values(), role=role, lead_data=config.lead_data)
    return Discovery(role=role, tools=tools, skipped=dict(found.skipped))


async def _fetch_document(client: ApiClient) -> dict[str, Any]:
    try:
        response = await client.get(OPENAPI_PATH)
    except ApiUnavailable as problem:
        raise StartupError(str(problem)) from None
    if not response.ok:
        raise StartupError(_refusal(render_error(response), response.status))
    try:
        document = json.loads(response.content)
    except ValueError:
        raise StartupError(
            "A resposta de /openapi.json não é JSON: confira se GERADOR_API_URL é o endereço da API."
        ) from None
    if not isinstance(document, dict):
        raise StartupError("A resposta de /openapi.json não é um documento OpenAPI.")
    return document


async def _role(client: ApiClient, me: ToolSpec) -> str:
    try:
        response = await client.send(prepare(me, {}))
    except (ApiUnavailable, InputError) as problem:
        raise StartupError(str(problem)) from None
    if not response.ok:
        raise StartupError(_refusal(render_error(response), response.status))
    try:
        role = json.loads(response.content).get("role")
    except (ValueError, AttributeError):
        role = None
    if role not in catalog.ROLES:
        raise StartupError("A API não disse o papel da chave (`role` em /me).")
    return str(role)


def _refusal(text: str, status: int) -> str:
    hint = {
        401: " Confira GERADOR_API_KEY: a chave pode estar errada, vencida ou revogada.",
        403: " A chave não tem permissão para ler o contrato da API.",
        404: " Confira GERADOR_API_URL: este endereço não tem a API de admin.",
    }.get(status, "")
    return f"Não consegui começar. {text}{hint}"
