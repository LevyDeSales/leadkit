"""`python -m app.admin_mcp`: o servidor MCP stdio da API de admin.

Lê `GERADOR_API_URL`, `GERADOR_API_KEY` (e, se quiser, `GERADOR_MCP_LEAD_DATA=1` e
`GERADOR_MCP_DOWNLOAD_DIR`) do ambiente, que o cliente MCP (Claude Code, Claude Desktop) monta do bloco
`env` da configuração. Os avisos e os erros vão para **stderr**, sem a chave: o stdout é do protocolo.

Sai com 0 quando o cliente fecha a conexão, 1 se a subida falha (a API não responde, a chave não vale)
e 2 se a configuração ou a instalação está errada.
"""

from __future__ import annotations

import logging
import os
import sys

from app.admin_mcp.config import Config, ConfigError
from app.admin_mcp.discovery import StartupError
from app.admin_mcp.redaction import RedactingFilter, Redactor

PROGRAM = "gerador-mcp"


def configure_logging(redact: Redactor) -> None:
    """O log vai para stderr, e cada registro passa pelo filtro que tira a chave."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(redact))
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    logging.getLogger("gerador_mcp").setLevel(logging.INFO)


def main() -> int:
    try:
        config = Config.from_env(os.environ)
    except ConfigError as problem:
        print(f"{PROGRAM}: {problem}", file=sys.stderr)
        return 2
    redact = Redactor(config.api_key)
    configure_logging(redact)
    try:
        from app.admin_mcp.server import run
    except ModuleNotFoundError as problem:
        if (problem.name or "").split(".")[0] != "mcp":
            raise
        print(
            f"{PROGRAM}: falta o SDK do MCP. Instale o extra: uv sync --locked --extra mcp",
            file=sys.stderr,
        )
        return 2
    try:
        run(config)
    except StartupError as problem:
        print(f"{PROGRAM}: {redact(str(problem))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
