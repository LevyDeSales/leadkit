"""As imagens de um formulário, que os templates do PDF e da prévia pedem pelo nome.

Imagem de formulário é conteúdo: mora no storage (o bucket), fora do repositório, e a operadora a
envia no admin (`app.forms.assets`). O template a chama por `asset:<nome>`, no lugar do caminho de
um arquivo:

    <img src="asset:oito-pilares">
    .capa { background-image: url(asset:fundo-capa) }

A mesma escrita vale no HTML do PDF, no CSS dele e na prévia da página de resultado. A forma
`asset:<nome>` foi escolhida, e não uma função dos componentes (`brand.asset("nome")`), porque:
- funciona no CSS do PDF, que não é Jinja, e no atributo `style`, onde uma função não chega;
- a conferência ao salvar (`app.forms.definition.resources`) lê o texto como está e enxerga o nome
  de uma imagem como enxerga o endereço de um arquivo; um endereço que começa por Jinja ela não
  consegue conferir;
- o WeasyPrint trata `asset:` como um esquema de URL qualquer, que o buscador do app resolve
  (`app.adapters.weasyprint_pdf.AssetsOnlyFetcher`); na página de resultado, que roda no navegador
  da lead, `rewrite` troca o nome pelo endereço da rota que serve a imagem.

Este módulo é só o formato: o nome, a referência a um conteúdo (`AssetRef`) e a troca. Nada aqui
fala com o banco nem com o storage.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

SCHEME = "asset:"
NAME_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
NAME_MAX_LENGTH = 48
NAME_HINT = "use letras minúsculas sem acento, números e hífens entre as palavras, como logo-black"
# O que o admin aceita: o tipo de cada formato e a extensão que entra na chave e no endereço.
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
CONTENT_TYPES = {extension: content_type for content_type, extension in EXTENSIONS.items()}
# A rota pública que serve a imagem pelo conteúdo (`app.web.images`).
SERVE_PREFIX = "/img/"
STORAGE_PREFIX = "assets/"

_NAME = re.compile(NAME_PATTERN)
_FILENAME = re.compile(r"([0-9a-f]{64})\.(png|jpg|webp)")
# O nome de uma imagem como a prévia o escreve: depois de uma aspa ou de um parêntese, até a aspa
# ou o parêntese que fecham, ou um espaço. Fora de uma tag (no texto corrido), não conta.
_IN_PREVIEW = re.compile(rf"(?<=[\"'(])asset:({NAME_PATTERN})(?=[\"')\s])")


def name_problem(name: str) -> str | None:
    """Por que `name` não serve de nome de imagem; None se serve."""
    if not name:
        return "dê um nome à imagem"
    if len(name) > NAME_MAX_LENGTH:
        return f"use no máximo {NAME_MAX_LENGTH} caracteres"
    if not _NAME.fullmatch(name):
        return NAME_HINT
    return None


@dataclass(frozen=True)
class AssetRef:
    """O conteúdo de uma imagem: o sha256 dos bytes guardados e o tipo. Com isso se acha o arquivo
    no storage (`key`) e se monta o endereço dele na rota pública (`url`). É o que uma versão
    publicada fixa para cada imagem que usa."""

    sha256: str
    content_type: str

    @property
    def extension(self) -> str:
        return EXTENSIONS[self.content_type]

    @property
    def filename(self) -> str:
        return f"{self.sha256}.{self.extension}"

    @property
    def key(self) -> str:
        """A chave no storage: pelo conteúdo, então nunca muda nem se repete."""
        return f"{STORAGE_PREFIX}{self.filename}"

    @property
    def url(self) -> str:
        return f"{SERVE_PREFIX}{self.filename}"


def ref_of_filename(filename: str) -> AssetRef | None:
    """A referência de `<sha256>.<extensão>`, o final do endereço da rota; None se não é um."""
    found = _FILENAME.fullmatch(filename)
    if found is None:
        return None
    return AssetRef(found[1], CONTENT_TYPES[found[2]])


def asset_name(address: str) -> tuple[str, bool] | None:
    """Se o endereço é de uma imagem (`asset:<nome>`): o nome que ele escreve e se é literal. None:
    não é. Um nome que mistura Jinja (`asset:{{ x }}`, `asset:logo-{{ x }}`) não é literal, e o
    nome que vale só se sabe na renderização."""
    text = address.strip()
    if not text.lower().startswith(SCHEME):
        return None
    name = text[len(SCHEME) :]
    return name, "{" not in name


def rewrite(html: str, assets: Mapping[str, AssetRef]) -> str:
    """O HTML da prévia com cada `asset:<nome>` trocado pelo endereço da imagem na rota pública. Um
    nome que `assets` não conhece fica como está."""

    def swap(found: re.Match[str]) -> str:
        ref = assets.get(found[1])
        return ref.url if ref is not None else found[0]

    return _IN_PREVIEW.sub(swap, html)
