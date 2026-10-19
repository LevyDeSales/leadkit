"""Os modelos das imagens dos formulários: o que `list_assets`, `get_asset`, `upload_asset`,
`replace_asset` e `remove_asset` recebem e devolvem.

Dados, não telas: os tipos como `image/png`, os tamanhos em bytes, as datas em ISO 8601 UTC e nenhum
rótulo. O que a otimização fez ao arquivo enviado sai como números (`Optimization`: tamanho, tipo e
dimensões antes e depois), os mesmos de que a página escreve a frase dela. Nenhum modelo traz os
bytes da imagem nem uma URL do storage.

O envio e a troca são `multipart/form-data`: o arquivo vai numa parte do formulário, ao lado dos
outros campos, e por isso os dois pedidos são modelos de formulário (o arquivo é um `UploadFile`).
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import UploadFile
from pydantic import AwareDatetime, BaseModel, Field

from app.admin.api.common import ApiInput, Page, VersionedInput, utc
from app.forms import assets as form_assets

ContentType = Literal["image/png", "image/jpeg", "image/webp"]  # `app.forms.image_optimizer`

FILE_TEXT = (
    "O arquivo da imagem, numa parte `multipart/form-data` chamada `file`: PNG, JPEG ou WebP, até "
    "8 MB. Não vale SVG, GIF nem imagem animada. A API tira os metadados, limita o lado maior a "
    "2000 pixels e guarda no formato que dá o menor arquivo."
)


class AssetUse(BaseModel):
    """Uma versão publicada que usa a imagem."""

    form_slug: str = Field(
        description="O endereço (`slug`) do formulário cuja versão publicada usa a imagem.",
        examples=["diagnostico-rapido"],
    )
    form_version: int = Field(
        description="O número da versão publicada que usa a imagem.", examples=[2]
    )


class Asset(BaseModel):
    """Uma imagem como a API a mostra. Os bytes não vêm: o arquivo é o da imagem guardada, e o
    `version` (o sha256 dele) identifica o conteúdo."""

    id: uuid.UUID = Field(
        description="O id da imagem: o que identifica a imagem em toda operação. Não muda quando o "
        "conteúdo é trocado.",
        examples=["0b0a8f3e-6f0b-4c2e-9d57-3f1d1c1f2a10"],
    )
    name: str = Field(
        description="O nome curto da imagem, único no escopo dela. Os templates do PDF e da prévia "
        "a pedem por `asset:<nome>`. Não muda.",
        examples=["logo-black"],
    )
    reference: str = Field(
        description="Como um template pede a imagem: `asset:<nome>`.", examples=["asset:logo-black"]
    )
    form_slug: str | None = Field(
        description="O endereço (`slug`) do formulário a que a imagem pertence e onde só ele a "
        "enxerga. Nulo: a biblioteca geral, que todo formulário enxerga (um formulário que tem uma "
        "imagem de mesmo nome usa a dele).",
        examples=["diagnostico-rapido"],
    )
    content_type: ContentType = Field(description="O tipo do arquivo guardado, já otimizado.")
    width: int = Field(description="A largura do arquivo guardado, em pixels.", examples=[2000])
    height: int = Field(description="A altura do arquivo guardado, em pixels.", examples=[667])
    byte_size: int = Field(
        description="O tamanho do arquivo guardado, em bytes.", examples=[524288]
    )
    source_size: int = Field(
        description="O tamanho do arquivo enviado, antes da otimização, em bytes.",
        examples=[2621440],
    )
    saved_percent: int | None = Field(
        description="Quanto o arquivo encolheu na otimização, em porcentagem arredondada. Nulo: não "
        "encolheu.",
        examples=[80],
    )
    version: str = Field(
        description="A versão da imagem, um texto opaco (o sha256 do arquivo guardado): quem troca "
        "ou remove a imagem devolve o que viu em `expected_version`. Muda quando o conteúdo é "
        "trocado."
    )
    uses: list[AssetUse] = Field(
        description="As versões publicadas dos formulários que usam a imagem, por formulário e "
        "número. Enquanto houver uma, a imagem não pode ser removida; trocá-la não muda o que elas "
        "mostram (cada versão guarda o conteúdo que usou). Vazio: nenhuma a usa."
    )
    created_by: str = Field(
        description="Quem enviou: o usuário do admin, ou `chave:<nome>` se foi uma chave.",
        examples=["operadora"],
    )
    created_at: AwareDatetime = Field(description="Quando foi enviada (ISO 8601, UTC).")
    updated_by: str = Field(
        description="Quem trocou o conteúdo por último (quem enviou, se nunca foi trocada)."
    )
    updated_at: AwareDatetime = Field(
        description="Quando o conteúdo foi trocado por último (igual a `created_at`, se nunca foi)."
    )

    @classmethod
    def of(cls, asset: form_assets.Asset, uses: list[form_assets.Use]) -> Asset:
        return cls(**cls._fields(asset, uses))

    @staticmethod
    def _fields(asset: form_assets.Asset, uses: list[form_assets.Use]) -> dict[str, object]:
        return {
            "id": asset.id,
            "name": asset.name,
            "reference": f"asset:{asset.name}",
            "form_slug": asset.form_slug,
            "content_type": asset.ref.content_type,
            "width": asset.width,
            "height": asset.height,
            "byte_size": asset.byte_size,
            "source_size": asset.source_size,
            "saved_percent": form_assets.saved_percent(asset.source_size, asset.byte_size),
            "version": asset.ref.sha256,
            "uses": [AssetUse(form_slug=use.form_slug, form_version=use.number) for use in uses],
            "created_by": asset.created_by,
            "created_at": utc(asset.created_at),
            "updated_by": asset.updated_by,
            "updated_at": utc(asset.updated_at),
        }


class AssetPage(Page[Asset]):
    """Uma página de imagens: a biblioteca geral primeiro, depois as de cada formulário, por nome."""


class FileMeasure(BaseModel):
    """O tamanho, o tipo e as dimensões de um arquivo de imagem."""

    size: int = Field(description="O tamanho do arquivo, em bytes.", examples=[2621440])
    content_type: ContentType = Field(description="O tipo do arquivo.")
    width: int = Field(description="A largura, em pixels.", examples=[3000])
    height: int = Field(description="A altura, em pixels.", examples=[1000])

    @classmethod
    def of(cls, measure: form_assets.Measure) -> FileMeasure:
        return cls(
            size=measure.size,
            content_type=measure.content_type,  # type: ignore[arg-type]
            width=measure.width,
            height=measure.height,
        )


class Optimization(BaseModel):
    """O que a otimização fez ao arquivo enviado: como ele era e como ficou."""

    before: FileMeasure = Field(description="O arquivo que foi enviado.")
    after: FileMeasure = Field(description="O arquivo que foi guardado.")
    saved_percent: int | None = Field(
        description="Quanto o arquivo encolheu, em porcentagem arredondada. Nulo: não encolheu.",
        examples=[80],
    )

    @classmethod
    def of(cls, change: form_assets.Optimization) -> Optimization:
        return cls(
            before=FileMeasure.of(change.before),
            after=FileMeasure.of(change.after),
            saved_percent=change.saved_percent,
        )


class UploadedAsset(Asset):
    """A imagem enviada, como ficou, e o que a otimização fez ao arquivo."""

    optimization: Optimization = Field(
        description="O que a otimização fez ao arquivo enviado (tamanho, tipo e dimensões antes e "
        "depois)."
    )

    @classmethod
    def from_upload(cls, done: form_assets.Upload, uses: list[form_assets.Use]) -> UploadedAsset:
        return cls(**cls._fields(done.asset, uses), optimization=Optimization.of(done.optimization))


class ReplacedAsset(UploadedAsset):
    """A imagem depois da troca, e se a troca mudou alguma coisa."""

    changed: bool = Field(
        description="`true`: o conteúdo foi trocado. `false`: o arquivo enviado deu na mesma imagem "
        "que já estava guardada, e nada foi gravado nem auditado."
    )

    @classmethod
    def from_replacement(
        cls, done: form_assets.Upload, uses: list[form_assets.Use]
    ) -> ReplacedAsset:
        return cls(
            **cls._fields(done.asset, uses),
            optimization=Optimization.of(done.optimization),
            changed=done.changed,
        )


class UploadAssetRequest(ApiInput):
    """O envio de uma imagem nova: um formulário `multipart/form-data`."""

    name: str = Field(
        description="O nome curto da imagem, único no escopo dela: minúsculas, números e hífen. Os "
        "templates a pedem por `asset:<nome>`, e o nome não muda.",
        examples=["logo-black"],
    )
    scope: str = Field(
        default="",
        description="O endereço (`slug`) do formulário a que a imagem pertence. Ausente ou vazio: a "
        "biblioteca geral, que todo formulário enxerga.",
        examples=["diagnostico-rapido"],
    )
    file: UploadFile = Field(description=FILE_TEXT)


class ReplaceAssetRequest(VersionedInput):
    """A troca do conteúdo de uma imagem: um formulário `multipart/form-data`."""

    file: UploadFile = Field(description=FILE_TEXT)
