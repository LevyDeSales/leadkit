"""O servidor de ferramentas de mentira dos testes do MCP de admin: uma chave, uma API em memória
(`admin_mcp_support.FakeApi`), o `ToolRunner` ligado a ela, as ferramentas que o papel da chave permite e
as compostas que cabem nelas. Não é um arquivo de teste (o pytest não o coleta)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from admin_mcp_support import FakeApi, make_config, new_key

from app.admin_mcp import catalog
from app.admin_mcp.client import ApiClient
from app.admin_mcp.compose import Composite
from app.admin_mcp.composites import for_tools
from app.admin_mcp.confirmation import ConfirmationOutcome
from app.admin_mcp.results import ToolOutput
from app.admin_mcp.runner import NeverAsks, PendingConfirmations, ToolRunner


class Asker:
    """Traduz os atalhos de cenário em desfechos explícitos: True aprova, False recusa e None
    declara ausência de formulário. Guarda as perguntas, sem simular falhas como ausência."""

    def __init__(self, *answers: bool | None) -> None:
        self.answers = list(answers)
        self.messages: list[str] = []

    async def ask(self, message: str) -> ConfirmationOutcome:
        self.messages.append(message)
        answer = self.answers.pop(0) if self.answers else None
        return {
            True: ConfirmationOutcome.APPROVED,
            False: ConfirmationOutcome.DECLINED,
            None: ConfirmationOutcome.UNSUPPORTED,
        }[answer]


class Clock:
    """O relógio do teste, para a confirmação que expira."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class World:
    """Uma chave, uma API de mentira, um `ToolRunner` e as ferramentas que o papel da chave permite."""

    def __init__(
        self, tmp_path: Path, role: str = "total", *, lead_data: bool = False, **config: Any
    ) -> None:
        self.key = new_key()
        self.api = FakeApi(self.key, role)
        # A pasta de onde o servidor pode ler uma imagem (`upload_dir=None` testa a variável ausente).
        self.uploads = tmp_path / "uploads"
        self.uploads.mkdir(exist_ok=True)
        config.setdefault("upload_dir", self.uploads)
        self.config = make_config(
            self.key, lead_data=lead_data, download_dir=tmp_path / "downloads", **config
        )
        self.client = ApiClient(self.config, transport=self.api.transport)
        self.clock = Clock()
        self.runner = ToolRunner(
            self.client, self.config, pending=PendingConfirmations(clock=self.clock)
        )
        loaded = catalog.load(self.api.document)
        self.tools = {
            tool.name: tool
            for tool in catalog.select(loaded.tools.values(), role=role, lead_data=lead_data)
        }
        self.composites: dict[str, Composite] = {c.name: c for c in for_tools(self.tools)}

    def call(
        self, name: str, arguments: dict[str, Any] | None = None, asker: Asker | None = None
    ) -> ToolOutput:
        return asyncio.run(
            self.runner.call(self.tools[name], arguments or {}, asker or NeverAsks())
        )

    def compose(
        self, name: str, arguments: dict[str, Any] | None = None, asker: Asker | None = None
    ) -> ToolOutput:
        """Chama uma composta, como o servidor MCP a chama."""
        return asyncio.run(
            self.composites[name].invoke(
                self.runner, self.tools, arguments or {}, asker or NeverAsks()
            )
        )

    def close(self) -> None:
        asyncio.run(self.client.aclose())
