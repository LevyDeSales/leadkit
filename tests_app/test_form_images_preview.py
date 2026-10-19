"""A prévia da página de resultado troca `asset:<nome>` pelo endereço da imagem na rota pública."""

from __future__ import annotations

from conftest import legacy_definition

from app.forms import FormSpec
from app.forms.definition import compile_definition
from app.forms.definition.images import AssetRef

LOGO = AssetRef("ab" * 32, "image/webp")
PREVIEW = '<p>{{ plan.titulo }}</p><img src="asset:logo-black" alt="">'


def demo_with_image_in_the_preview() -> FormSpec:
    document = legacy_definition("demo")
    document["preview"] = PREVIEW
    return compile_definition(document)


def render(spec: FormSpec) -> str:
    return spec.templates.render_preview(
        spec.example_plan,
        lead={"name": "Contadora Exemplo"},
        form={"slug": spec.slug, "title": spec.title},
        scores={},
        generated_date="8 de outubro de 2026",
    )


def test_the_preview_compiles_with_an_image_name_and_keeps_it_until_the_images_are_known() -> None:
    assert 'src="asset:logo-black"' in render(demo_with_image_in_the_preview())


def test_with_the_images_of_the_version_the_name_becomes_the_public_address() -> None:
    spec = demo_with_image_in_the_preview().with_assets({"logo-black": LOGO})

    html = render(spec)

    assert f'src="/img/{LOGO.sha256}.webp"' in html and "asset:" not in html


def test_with_assets_leaves_the_original_contract_as_it_was() -> None:
    spec = demo_with_image_in_the_preview()

    spec.with_assets({"logo-black": LOGO})

    assert spec.templates.assets == {} and 'src="asset:logo-black"' in render(spec)
