"""Ambiente Jinja2 compartilhado pelas páginas públicas e pelo admin.

Autoescape ligado (padrão do Starlette para .html) e variáveis indefinidas viram erro. Os templates
chamam `static_url(caminho)` para um arquivo de `static/`: o endereço leva a versão do conteúdo
(`?v=`), então o navegador busca de novo o arquivo que mudou e guarda o que não mudou.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import StrictUndefined

from app.branding import installation_brand

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
TEMPLATE_DIRS = [WEB_DIR / "templates", WEB_DIR.parent / "admin" / "templates"]


@lru_cache(maxsize=64)
def _digest(file: Path, mtime_ns: int) -> str:
    """Os 12 primeiros caracteres do sha256 do arquivo. O `mtime_ns` só chave o cache: editar o
    arquivo (no desenvolvimento) recalcula, sem reiniciar."""
    return hashlib.sha256(file.read_bytes()).hexdigest()[:12]


def static_url(path: str) -> str:
    """O endereço de um arquivo de `static/` com a versão do conteúdo: `/static/js/x.js?v=3fa9…`.
    Levanta `ValueError` para um caminho fora de `static/` e `FileNotFoundError` para um arquivo que
    não existe: o erro aparece na renderização, não no navegador de quem usa."""
    root = STATIC_DIR.resolve()
    file = (root / path).resolve()
    if not file.is_relative_to(root):
        raise ValueError(f"{path!r} fica fora de static/")
    version = _digest(file, file.stat().st_mtime_ns)
    return f"/static/{file.relative_to(root).as_posix()}?v={version}"


templates = Jinja2Templates(directory=[str(d) for d in TEMPLATE_DIRS])
templates.env.undefined = StrictUndefined
templates.env.globals["static_url"] = static_url

templates.env.globals["site_brand"] = lambda: installation_brand().name
