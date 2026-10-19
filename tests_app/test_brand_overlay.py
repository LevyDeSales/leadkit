"""Marca privada fictícia: leitura confinada e compatibilidade das macros."""

import json
from pathlib import Path

import pytest

from app.branding import Brand, BrandProblem


def overlay(root: Path, **config: object) -> Path:
    root.mkdir()
    (root / "brand.json").write_text(json.dumps(config), encoding="utf-8")
    return root


def test_default_brand_is_neutral() -> None:
    brand = Brand.load(None)
    assert brand.name == "Leadkit"
    assert b"Leadkit" in brand.read("logo-preta.svg")


def test_private_brand_is_read_only_and_escapes_its_name(tmp_path: Path) -> None:
    root = overlay(tmp_path / "brand", name="<Ateliê & exemplo>", logos={"preta": "mark.svg"})
    (root / "mark.svg").write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    brand = Brand.load(root)
    assert brand.read("logo-preta.svg") == before["mark.svg"]
    assert "&lt;Ateliê &amp; exemplo&gt;" in str(brand.components().logo())
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before


@pytest.mark.parametrize(
    "resource",
    ["../outside", "/etc/passwd", "https://example.test/a", "%2e%2e/outside", "a/../../outside"],
)
def test_resources_cannot_escape_root(tmp_path: Path, resource: str) -> None:
    brand = Brand.load(overlay(tmp_path / "brand", name="Exemplo"))
    with pytest.raises(BrandProblem):
        brand.read(resource)


def test_symlinks_cannot_escape_root(tmp_path: Path) -> None:
    root = overlay(tmp_path / "brand", name="Exemplo", logos={"preta": "mark.svg"})
    outside = tmp_path / "outside.svg"
    outside.write_bytes(b"private")
    (root / "mark.svg").symlink_to(outside)
    with pytest.raises(BrandProblem):
        Brand.load(root).read("logo-preta.svg")


def test_configuration_cannot_be_loaded_through_external_symlink(tmp_path: Path) -> None:
    root = tmp_path / "brand"
    root.mkdir()
    outside = tmp_path / "config.json"
    outside.write_text('{"name":"outside"}')
    (root / "brand.json").symlink_to(outside)
    with pytest.raises(BrandProblem):
        Brand.load(root)


def test_private_overlay_renders_a_real_pdf_with_neutral_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from test_weasyprint_pdf import text_of

    from app.adapters.weasyprint_pdf import WeasyPrintRenderer
    from app.forms.definition import compile_definition
    from app.forms.starter import definition

    root = overlay(
        tmp_path / "brand",
        name="Ateliê fictício",
        logos={"preta": "mark.svg", "branca": "mark.svg"},
    )
    (root / "mark.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="180" height="40"><text x="2" y="28">Atelie ficticio</text></svg>'
    )
    monkeypatch.setenv("LEADKIT_BRAND_DIR", str(root))
    spec = compile_definition(definition(slug="synthetic", title="Projeto fictício"))
    rendered = WeasyPrintRenderer().render(
        spec,
        spec.example_plan,
        {
            "lead": {"name": "Participante Exemplo"},
            "form": {"slug": spec.slug, "title": spec.title},
            "generated_at": datetime(2026, 10, 19, tzinfo=UTC),
        },
    )
    assert rendered.page_count >= 2
    assert "Atelie ficticio" in text_of(rendered.content)


def test_site_brand_uses_the_overlay_name_with_html_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.web.templating import templates

    root = overlay(tmp_path / "brand", name="<Ateliê fictício>")
    monkeypatch.setenv("LEADKIT_BRAND_DIR", str(root))
    html = templates.env.from_string("<header>{{ site_brand() }}</header>").render()
    assert html == "<header>&lt;Ateliê fictício&gt;</header>"


@pytest.mark.parametrize("folder", ["leadkit", "academia-de-contadores"])
def test_private_print_stylesheet_is_used_by_new_and_immutable_templates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    from app.adapters.weasyprint_pdf import ASSETS_DIR, AssetsOnlyFetcher

    root = overlay(tmp_path / "brand", name="Ateliê fictício")
    content = b"/* synthetic private stylesheet */ body { color: #234567; }"
    (root / "print.css").write_bytes(content)
    monkeypatch.setenv("LEADKIT_BRAND_DIR", str(root))
    response = AssetsOnlyFetcher().fetch((ASSETS_DIR / "brands" / folder / "print.css").as_uri())
    assert response.read() == content
