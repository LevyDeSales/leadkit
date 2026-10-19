"""As telas das imagens dos formulários no admin: a lista com o envio, o detalhe de uma imagem com
a troca e a remoção, e as mensagens do que o envio e a troca fizeram (a otimização, antes e depois).

O que cada tela mostra vem pronto daqui, com os rótulos em português e as datas no horário de
Brasília; os templates (`admin/assets.html` e `admin/asset.html`) só dispõem. A regra de negócio é
de `app.forms.assets`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.admin.views import Moment, form_url, moment
from app.forms import assets as form_assets
from app.forms import stored as stored_forms
from app.forms.definition.images import NAME_HINT, NAME_MAX_LENGTH
from app.forms.image_optimizer import MAX_DIMENSION, MAX_UPLOAD_BYTES
from app.web.jobview import file_size

ASSETS_URL = "/admin/assets"
ACCEPT = "image/png,image/jpeg,image/webp"
TYPE_LABELS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WebP"}
LIBRARY_LABEL = "Biblioteca geral (todos os formulários)"
LIMITS = (
    f"PNG, JPEG ou WebP, até {MAX_UPLOAD_BYTES // (1024 * 1024)} MB. O app tira os metadados, "
    f"limita o lado maior a {MAX_DIMENSION} pixels e guarda no formato que dá o menor arquivo. "
    "SVG e GIF não são aceitos."
)


def asset_url(asset_id: uuid.UUID | str) -> str:
    return f"{ASSETS_URL}/{asset_id}"


def describe(size: int, content_type: str, width: int, height: int) -> str:
    """ "512 KB (PNG, 1284 × 725)"."""
    return f"{file_size(size)} ({TYPE_LABELS[content_type]}, {width} × {height})"


def saving(before: int, after: int) -> str:
    """O quanto o arquivo encolheu, ou que ficou do mesmo tamanho."""
    percent = form_assets.saved_percent(before, after)
    return "sem ganho de tamanho" if percent is None else f"{percent}% menor"


def optimization(change: form_assets.Optimization) -> str:
    """O que a otimização fez, antes e depois, para a mensagem do envio e da troca. Escreve os
    números que a API devolve (`form_assets.Optimization`)."""
    before = describe(
        change.before.size, change.before.content_type, change.before.width, change.before.height
    )
    after = describe(
        change.after.size, change.after.content_type, change.after.width, change.after.height
    )
    return f"Antes: {before}. Depois: {after}, {saving(change.before.size, change.after.size)}."


UPLOADED = "Imagem «{name}» enviada. {result}"
REPLACED = (
    "Imagem «{name}» trocada. {result} As versões já publicadas seguem com a imagem anterior: "
    "publique o formulário para os envios novos usarem esta."
)
REPLACED_SAME = "A imagem já era esta: nada mudou."
REMOVED = "Imagem «{name}» removida."


@dataclass(frozen=True)
class AssetRow:
    """Uma imagem na lista."""

    id: str
    name: str
    scope: str
    library: bool
    reference: str  # como o template a pede
    thumbnail: str
    type_label: str
    size: str
    width: int
    height: int
    dimensions: str
    saved: str | None  # o quanto encolheu na otimização; None se não encolheu
    created_by: str
    created: Moment | None
    url: str


def row_of(asset: form_assets.Asset) -> AssetRow:
    return AssetRow(
        id=str(asset.id),
        name=asset.name,
        scope=asset.scope,
        library=asset.library,
        reference=f"asset:{asset.name}",
        thumbnail=asset.ref.url,
        type_label=TYPE_LABELS[asset.ref.content_type],
        size=file_size(asset.byte_size),
        width=asset.width,
        height=asset.height,
        dimensions=f"{asset.width} × {asset.height}",
        saved=saving(asset.source_size, asset.byte_size)
        if form_assets.saved_percent(asset.source_size, asset.byte_size) is not None
        else None,
        created_by=asset.created_by,
        created=moment(asset.created_at),
        url=asset_url(asset.id),
    )


@dataclass(frozen=True)
class ScopeOption:
    value: str  # o slug, ou vazio para a biblioteca geral
    label: str


@dataclass(frozen=True)
class AssetsPage:
    """A lista das imagens e o formulário de envio, com o que a operadora digitou e o problema de
    cada campo (`scope`, `name`, `file`), se o envio foi recusado."""

    rows: list[AssetRow]
    scopes: list[ScopeOption]
    scope: str
    name: str
    errors: Mapping[str, str] = field(default_factory=dict)
    action: str = ASSETS_URL
    accept: str = ACCEPT
    limits: str = LIMITS
    name_max_length: int = NAME_MAX_LENGTH
    name_hint: str = NAME_HINT


def assets_page(
    session: Session,
    *,
    scope: str = "",
    name: str = "",
    errors: Mapping[str, str] | None = None,
) -> AssetsPage:
    scopes = [ScopeOption("", LIBRARY_LABEL)]
    scopes += [
        ScopeOption(form.slug, f"{form.title} ({form.slug})")
        for form in stored_forms.listing(session)
    ]
    return AssetsPage(
        rows=[row_of(asset) for asset in form_assets.listing(session)],
        scopes=scopes,
        scope=scope,
        name=name,
        errors=errors or {},
    )


@dataclass(frozen=True)
class UseRow:
    """Uma versão publicada que usa a imagem."""

    label: str
    url: str


@dataclass(frozen=True)
class AssetPage:
    """O detalhe de uma imagem: o que ela é, onde vale, o que a otimização fez, quem a usa e os
    formulários da troca e da remoção."""

    row: AssetRow
    asset_id: str
    sha256: str
    expected: str  # o sha256 que a página viu: troca e remoção se recusam se ele mudou
    original: str  # o arquivo enviado, antes da otimização
    stored: str  # o arquivo guardado
    created_by: str
    created: Moment | None
    updated_by: str
    updated: Moment | None
    replaced: bool
    uses: list[UseRow]
    errors: Mapping[str, str]
    message: str | None
    replace_action: str
    remove_action: str
    accept: str = ACCEPT
    limits: str = LIMITS

    @property
    def removable(self) -> bool:
        return not self.uses


def asset_page(
    session: Session,
    asset: form_assets.Asset,
    *,
    errors: Mapping[str, str] | None = None,
    message: str | None = None,
) -> AssetPage:
    uses = form_assets.uses(session, asset.id)
    row = row_of(asset)
    return AssetPage(
        row=row,
        asset_id=str(asset.id),
        sha256=asset.ref.sha256,
        expected=asset.ref.sha256,
        original=file_size(asset.source_size),
        stored=describe(asset.byte_size, asset.ref.content_type, asset.width, asset.height),
        created_by=asset.created_by,
        created=moment(asset.created_at),
        updated_by=asset.updated_by,
        updated=moment(asset.updated_at),
        replaced=asset.updated_at != asset.created_at,
        uses=[
            UseRow(f"{use.form_slug}, versão {use.number}", form_url(use.form_slug)) for use in uses
        ],
        errors=errors or {},
        message=message,
        replace_action=f"{row.url}/replace",
        remove_action=f"{row.url}/remove",
    )
