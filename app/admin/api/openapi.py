"""O `docs/api/openapi.json`: o OpenAPI da API de admin, versionado no repositório (ADR 0015, "O
arquivo `openapi.json`").

    python -m app.admin.api.openapi            # confere: sai com erro se o arquivo estiver velho
    python -m app.admin.api.openapi --write    # atualiza o arquivo

O arquivo é o `openapi()` da sub-aplicação em JSON, com as chaves ordenadas, recuo de 2 e uma linha
final (`app.admin.api.document.render`, o mesmo texto que `GET /admin/api/v1/openapi.json` serve:
a imagem do app não leva `docs/`, então a rota gera o texto, e o teste confere que é o arquivo). É o
mesmo em qualquer máquina: a sub-aplicação se constrói sem banco nem configuração, o
`servers` é fixo e relativo (`/admin/api/v1`, resolvido contra a origem em que o documento foi
servido) e nada vem do ambiente. Mudar a API sem atualizá-lo reprova a suíte (`tests_app/
test_admin_api_openapi.py`), que mostra o diff e o comando acima; o diff do PR vira o histórico da
API, e subir o FastAPI ou o Pydantic também aparece nele.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.admin.api.application import create_admin_api
from app.admin.api.document import render

PATH = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.json"
FILE = "docs/api/openapi.json"
COMMAND = "python -m app.admin.api.openapi --write"
MAX_DIFF_LINES = 120  # o diff de uma mudança grande é cortado; o comando que atualiza vem junto


def build() -> dict[str, Any]:
    """O OpenAPI da API de admin, como a sub-aplicação o gera."""
    return create_admin_api().openapi()


def check(path: Path = PATH) -> str | None:
    """Nada (`None`) se o arquivo é o que a API gera; senão, o que dizer a quem mudou a API: o diff,
    cortado se for grande, e o comando que atualiza o arquivo."""
    generated = render(build())
    if not path.exists():
        return f"{FILE} não existe. Para criá-lo: {COMMAND}"
    versioned = path.read_text(encoding="utf-8")
    if versioned == generated:
        return None
    lines = list(
        difflib.unified_diff(
            versioned.splitlines(),
            generated.splitlines(),
            fromfile=f"{FILE} (versionado)",
            tofile=f"{FILE} (gerado agora)",
            lineterm="",
        )
    )
    shown = lines[:MAX_DIFF_LINES]
    if len(lines) > MAX_DIFF_LINES:
        shown.append(f"... e mais {len(lines) - MAX_DIFF_LINES} linhas a mais no diff")
    return (
        f"{FILE} não é o que a API de admin gera agora. Se a mudança é de propósito, atualize-o "
        f"e inclua o diff no PR:\n\n    {COMMAND}\n\n" + "\n".join(shown)
    )


def main(argv: Sequence[str] | None = None, *, path: Path = PATH) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.admin.api.openapi",
        description=f"Confere (ou, com --write, atualiza) o {FILE}.",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--write", action="store_true", help="atualiza o arquivo")
    modes.add_argument(
        "--check", action="store_true", help="só confere e sai com erro se diferir (o padrão)"
    )
    arguments = parser.parse_args(argv)
    if arguments.write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(build()), encoding="utf-8", newline="\n")
        print(f"{FILE} atualizado.")
        return 0
    problem = check(path)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    print(f"{FILE} está em dia.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
