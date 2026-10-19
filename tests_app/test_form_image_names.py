"""O formato das imagens de formulário: o nome, a referência pelo conteúdo e a troca na prévia."""

from __future__ import annotations

import pytest

from app.forms.definition.images import (
    NAME_MAX_LENGTH,
    AssetRef,
    asset_name,
    name_problem,
    ref_of_filename,
    rewrite,
)

SHA = "ab" * 32


@pytest.mark.parametrize("name", ["logo", "oito-pilares", "capa-2026", "a", "x" * NAME_MAX_LENGTH])
def test_a_lowercase_name_with_single_hyphens_is_valid(name: str) -> None:
    assert name_problem(name) is None


@pytest.mark.parametrize(
    "name",
    ["", "Logo", "logo preto", "logo_preto", "-logo", "logo-", "logo--preto", "pilares/um", "á"],
)
def test_anything_else_is_not_a_name(name: str) -> None:
    assert name_problem(name)


def test_a_name_over_the_limit_says_the_limit() -> None:
    assert str(NAME_MAX_LENGTH) in (name_problem("x" * (NAME_MAX_LENGTH + 1)) or "")


def test_a_reference_knows_its_key_in_the_storage_and_its_public_address() -> None:
    ref = AssetRef(SHA, "image/jpeg")

    assert ref.key == f"assets/{SHA}.jpg"
    assert ref.url == f"/img/{SHA}.jpg"


def test_a_filename_gives_back_the_reference_it_came_from() -> None:
    ref = AssetRef(SHA, "image/webp")

    assert ref_of_filename(ref.filename) == ref
    assert ref_of_filename(f"{SHA}.gif") is None
    assert ref_of_filename(f"{SHA[:-1]}.png") is None
    assert ref_of_filename(f"../{SHA}.png") is None


def test_an_asset_address_gives_its_name_and_whether_it_is_literal() -> None:
    assert asset_name("asset:logo") == ("logo", True)
    assert asset_name("  ASSET:logo ") == ("logo", True)
    assert asset_name("asset:logo-{{ x }}") == ("logo-{{ x }}", False)
    assert asset_name("brands/logo.svg") is None


def test_the_preview_swaps_a_known_name_for_the_public_address() -> None:
    html = '<img src="asset:logo"> <img src=\'asset:capa\'> <i style="x:url(asset:logo)">'
    assets = {"logo": AssetRef(SHA, "image/png"), "capa": AssetRef("cd" * 32, "image/webp")}

    swapped = rewrite(html, assets)

    assert f'src="/img/{SHA}.png"' in swapped
    assert f"src='/img/{'cd' * 32}.webp'" in swapped
    assert f"url(/img/{SHA}.png)" in swapped
    assert "asset:" not in swapped


def test_the_preview_leaves_alone_a_name_it_does_not_know_and_plain_text() -> None:
    html = '<img src="asset:outra"> <p>escreva asset:logo no template</p>'

    assert rewrite(html, {"logo": AssetRef(SHA, "image/png")}) == html
