"""A cola com o SDK do MCP: registra as ferramentas e fala por stdio.

Este é o único módulo que importa o SDK (`mcp`, o extra opcional do `pyproject.toml`); o resto do
pacote roda e se testa sem ele. A lógica de cada chamada está em `runner` (e, nas ferramentas compostas,
em `compose` e `composites*`, que chamam as geradas pelo `runner`); aqui ficam só a tradução
`ToolSpec` e `Composite` -> `types.Tool`, a pergunta à pessoa pela *elicitation* do cliente e o laço
stdio.

Pelo stdio passa o protocolo, então **nada escreve em stdout** além do SDK: o log vai para stderr.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from app.admin_mcp.catalog import ToolSpec
from app.admin_mcp.client import ApiClient
from app.admin_mcp.compose import Composite, without_clashes
from app.admin_mcp.composites import for_tools
from app.admin_mcp.config import Config
from app.admin_mcp.discovery import discover
from app.admin_mcp.redaction import Redactor
from app.admin_mcp.runner import ToolRunner

logger = logging.getLogger("gerador_mcp")

SERVER_NAME = "gerador-de-planos-admin"
INSTRUCTIONS = (
    "Ferramentas da API de admin do Leadkit, uma por operação, geradas do contrato dela. "
    "As que apagam ou sobrescrevem algo, ou que mudam o que a lead vê, para onde os dados dela vão "
    "ou chamam um serviço de fora, dependem da confirmação da pessoa: pergunte a ela e nunca "
    "confirme por ela (`confirm: true` só depois do sim dela). Dados pessoais de leads ficam "
    "desligados por padrão. Uma resposta que não é JSON (PDF, página HTML) é salva num arquivo, e a "
    "ferramenta devolve o caminho. Há também poucas ferramentas compostas (`attention_overview`, "
    "`diagnose_order`, `edit_form_draft`, `publish_form_with_review`, `connect_baserow_integration`): "
    "cada uma só chama ferramentas geradas, que seguem disponíveis, e a regra da confirmação vale "
    "dentro dela do mesmo jeito (uma ação marcada por vez, e a pessoa confirma a que o texto mostra)."
)
# O formulário da pergunta: uma caixa a marcar. A pessoa responde pelo cliente (aceitar, recusar ou
# fechar), e só o `accept` com a caixa marcada vale como sim.
CONFIRM_FORM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirmar": {
            "type": "boolean",
            "title": "Confirmo",
            "description": "Marque para executar.",
        }
    },
    "required": ["confirmar"],
}


class SessionPrompter:
    """Pergunta à pessoa pela *elicitation* do cliente MCP, quando ele a tem."""

    def __init__(self, session: Any, request_id: Any) -> None:
        self._session = session
        self._request_id = request_id

    def can_ask(self) -> bool:
        """O cliente declarou a *elicitation* em formulário. Um `elicitation: {}` vazio vale como
        formulário (a regra de compatibilidade da especificação); só `{url: {}}` não vale."""
        params = self._session.client_params
        found = params.capabilities.elicitation if params is not None else None
        return found is not None and (found.form is not None or found.url is None)

    async def ask(self, message: str) -> bool | None:
        if not self.can_ask():
            return None
        try:
            result = await self._session.elicit_form(
                message=message, requestedSchema=CONFIRM_FORM, related_request_id=self._request_id
            )
        except Exception as problem:  # noqa: BLE001 - o cliente recusou, caiu ou estourou o tempo
            logger.warning("Não consegui perguntar à pessoa (%s)", type(problem).__name__)
            return None
        return result.action == "accept" and (result.content or {}).get("confirmar") is True


def to_tool(spec: ToolSpec) -> types.Tool:
    """A ferramenta do MCP de uma `ToolSpec`: o nome é o `operation_id`, e as marcas viram dicas."""
    return types.Tool(
        name=spec.name,
        title=spec.summary,
        description=spec.description,
        inputSchema=dict(spec.input_schema),
        annotations=types.ToolAnnotations(title=spec.summary, **spec.annotations),
    )


def to_composite_tool(composite: Composite) -> types.Tool:
    """A ferramenta do MCP de uma composta: as dicas saem das marcas das operações que ela chama."""
    return types.Tool(
        name=composite.name,
        title=composite.summary,
        description=composite.description,
        inputSchema=dict(composite.input_schema),
        annotations=types.ToolAnnotations(title=composite.summary, **composite.annotations),
    )


def build_server(
    runner: ToolRunner, tools: Iterable[ToolSpec], composites: Iterable[Composite] = ()
) -> Server[Any, Any]:
    """O servidor MCP com as `tools` registradas (as que o papel da chave permite) e, ao lado delas, as
    `composites` (as que têm todas as operações que chamam entre as `tools`: `for_tools`); a que tem o nome
    de uma ferramenta gerada é pulada, com um aviso no log."""
    by_name = {spec.name: spec for spec in tools}
    # Uma composta com o nome de uma operação da API (que pode ser mais nova que o MCP) fica de fora: a
    # gerada vale, e o log avisa.
    composed = {c.name: c for c in without_clashes(by_name, composites)}
    server: Server[Any, Any] = Server(SERVER_NAME, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            *(to_tool(spec) for spec in by_name.values()),
            *(to_composite_tool(composite) for composite in composed.values()),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        spec = by_name.get(name)
        composite = composed.get(name)
        if spec is None and composite is None:
            return _result(f"A ferramenta `{name}` não existe neste servidor.", is_error=True)
        context = server.request_context
        prompter = SessionPrompter(context.session, context.request_id)
        if composite is not None:
            output = await composite.invoke(runner, by_name, arguments, prompter)
        else:
            assert spec is not None
            output = await runner.call(spec, arguments, prompter)
        return _result(output.text, is_error=output.is_error)

    return server


def _result(text: str, *, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], isError=is_error
    )


async def serve(config: Config) -> None:
    """Sobe o servidor stdio e roda até o cliente fechar a conexão."""
    redact = Redactor(config.api_key)
    async with ApiClient(config, redact=redact) as client:
        discovery = await discover(client, config)
        composites = for_tools({spec.name: spec for spec in discovery.tools})
        logger.info(
            "%d ferramentas para a chave de papel %s%s, mais %d compostas",
            len(discovery.tools),
            discovery.role,
            " (dados da lead ligados)" if config.lead_data and discovery.role == "total" else "",
            len(composites),
        )
        server = build_server(ToolRunner(client, config), discovery.tools, composites)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def run(config: Config) -> None:
    anyio.run(serve, config)
