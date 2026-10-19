"""O MCP de admin ligado à API de verdade, para os testes: a sub-aplicação FastAPI do app no mesmo
processo do teste, com o Postgres de teste, atrás de um `httpx.ASGITransport` (sem rede). Não é um
arquivo de teste (o pytest não o coleta); `test_admin_mcp_live_api.py` e
`test_admin_mcp_composites.py` o importam.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from admin_api_support import ApiClient as KeyClient
from admin_mcp_support import ORIGIN
from admin_mcp_world import Asker

from app.admin_mcp.client import ApiClient
from app.admin_mcp.compose import Composite
from app.admin_mcp.composites import for_tools
from app.admin_mcp.config import API_PREFIX, Config
from app.admin_mcp.discovery import discover
from app.admin_mcp.results import ToolOutput
from app.admin_mcp.runner import NeverAsks, ToolRunner


class Recording(httpx.AsyncBaseTransport):
    """O transporte do app, anotando cada requisição que o MCP manda."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


class Live:
    """O MCP ligado à API do app: o `discover` da subida de verdade, um `ToolRunner` e as compostas
    que a chave pode ter."""

    def __init__(
        self, keyed: KeyClient, tmp_path: Path, *, lead_data: bool = False, pace: bool = False
    ) -> None:
        self.key = keyed.key.key
        # `pace`: o relógio do app avança dois minutos antes de cada chamada, para o balde do limite de
        # uso (30 fichas de rajada) encher e os testes não dependerem de quantas fichas os outros
        # gastaram. Um teste do limite o deixa desligado.
        self.pace = pace
        self.clock = keyed.clock
        self.name = keyed.key.info.actor
        self.uploads = tmp_path / "uploads"
        self.uploads.mkdir(exist_ok=True)
        self.config = Config(
            base_url=ORIGIN + API_PREFIX,
            api_key=self.key,
            lead_data=lead_data,
            download_dir=tmp_path / "downloads",
            upload_dir=self.uploads,
        )
        self.wire = Recording(httpx.ASGITransport(app=keyed.app))
        self.client = ApiClient(self.config, transport=self.wire)
        found = asyncio.run(discover(self.client, self.config))
        self.started = len(self.wire.requests)  # o `openapi.json` e o `/me`
        self.role = found.role
        self.tools = {tool.name: tool for tool in found.tools}
        self.composites: dict[str, Composite] = {c.name: c for c in for_tools(self.tools)}
        self.runner = ToolRunner(self.client, self.config)
        self.outputs: list[str] = []

    def call(
        self, name: str, arguments: dict[str, Any] | None = None, asker: Asker | None = None
    ) -> ToolOutput:
        self.refill()
        output = asyncio.run(
            self.runner.call(self.tools[name], arguments or {}, asker or NeverAsks())
        )
        self.outputs.append(output.text)
        return output

    def refill(self) -> None:
        if self.pace:
            self.clock.advance(120)

    def compose(
        self, name: str, arguments: dict[str, Any] | None = None, asker: Asker | None = None
    ) -> ToolOutput:
        """Chama uma composta, como o servidor MCP a chama."""
        self.refill()
        output = asyncio.run(
            self.composites[name].invoke(
                self.runner, self.tools, arguments or {}, asker or NeverAsks()
            )
        )
        self.outputs.append(output.text)
        return output

    def ok(
        self, name: str, arguments: dict[str, Any] | None = None, asker: Asker | None = None
    ) -> Any:
        """A resposta JSON de uma chamada que tem de dar certo."""
        output = self.call(name, arguments, asker)
        assert not output.is_error, output.text
        return json.loads(output.text)

    def sent(self) -> list[httpx.Request]:
        """As requisições que as chamadas mandaram à API, sem as da subida."""
        return self.wire.requests[self.started :]

    def close(self) -> None:
        asyncio.run(self.client.aclose())
