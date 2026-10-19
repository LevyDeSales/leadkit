"""Assets da marca da instalação, lidos de uma raiz privada sem acesso à rede.

O operador monta LEADKIT_BRAND_DIR como volume somente leitura. Nenhum arquivo é copiado para
storage, exportações de formulário ou repositório. Templates publicados conservam suas macros.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ROOT = Path(__file__).resolve().parent / "assets" / "brands" / "leadkit"


class BrandProblem(ValueError):
    """Configuração ou recurso inválido; a mensagem não revela o caminho privado."""


def _file(root: Path, name: str) -> Path:
    decoded = unquote(name)
    parts = urlsplit(decoded)
    path = PurePosixPath(decoded)
    if (
        parts.scheme
        or parts.netloc
        or parts.query
        or parts.fragment
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in decoded
    ):
        raise BrandProblem("recurso da marca fora da raiz")
    resolved = (root / decoded).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise BrandProblem("recurso da marca indisponível")
    return resolved


@dataclass(frozen=True)
class Brand:
    name: str = "Leadkit"
    root: Path = DEFAULT_ROOT
    logos: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path | None) -> Brand:
        if root is None:
            return cls()
        if not root.is_absolute() or not root.is_dir():
            raise BrandProblem("a raiz da marca precisa ser um diretório absoluto")
        root = root.resolve()
        try:
            config = json.loads(_file(root, "brand.json").read_text(encoding="utf-8"))
            if not isinstance(config, dict) or set(config) - {"name", "logos"}:
                raise ValueError
            name = config.get("name", "Leadkit")
            logos = config.get("logos", {})
            if not isinstance(name, str) or not name.strip() or len(name) > 120:
                raise ValueError
            if not isinstance(logos, dict) or set(logos) - {"preta", "branca"}:
                raise ValueError
            if any(not isinstance(value, str) for value in logos.values()):
                raise ValueError
            return cls(name, root, logos)
        except (OSError, ValueError, TypeError):
            raise BrandProblem("configuração da marca inválida") from None

    def read(self, resource: str) -> bytes:
        for variant, target in self.logos.items():
            if resource == f"logo-{variant}.svg":
                resource = target
                break
        try:
            # Recursos padrão ausentes no overlay usam o kit livre. Um symlink existente,
            # mesmo quebrado, continua sendo validado e nunca dispara fallback.
            local = self.root / resource
            if (
                self.root != DEFAULT_ROOT
                and resource in {"print.css", "logo-preta.svg", "logo-branca.svg"}
                and not local.exists()
                and not local.is_symlink()
            ):
                return _file(DEFAULT_ROOT, resource).read_bytes()
            return _file(self.root, resource).read_bytes()
        except OSError:
            raise BrandProblem("recurso da marca indisponível") from None

    def components(self) -> Any:
        # Importação tardia evita ciclo: a biblioteca padrão também usa a marca.
        from app.forms.definition.components import LIBRARY
        from app.forms.sandbox import SandboxTemplate

        source = (
            "{% set brand_name = " + json.dumps(self.name, ensure_ascii=False) + " %}" + LIBRARY
        )
        return SandboxTemplate(source, name="brand", html=True).module()


class _BrandSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    root: str | None = Field(default=None, alias="LEADKIT_BRAND_DIR")


def installation_brand() -> Brand:
    root = _BrandSettings().root
    return Brand.load(Path(root) if root else None)
