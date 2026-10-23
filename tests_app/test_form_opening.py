"""A abertura compartilhada preserva texto puro e a apresentação da introdução."""

from pathlib import Path

import pytest
from conftest import legacy_definition

from app.forms.definition import compile_definition
from app.web.templating import templates


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_opening_keeps_paragraphs_as_escaped_text(newline: str) -> None:
    document = legacy_definition("demo")
    document["intro"] = newline.join(
        ["Primeiro bloco", "", "Segundo <script>bloco</script>", "", "Terceiro & último"]
    )
    spec = compile_definition(document)
    html = templates.env.get_template("public/_hero.html").render(spec=spec, screen=spec.screen)
    assert "Segundo &lt;script&gt;bloco&lt;/script&gt;" in html
    assert "Terceiro &amp; último" in html
    assert "<script>bloco</script>" not in html
    assert f"Primeiro bloco{newline}{newline}Segundo" in html
    css = Path("app/web/static/css/public.css").read_text()
    rule = css.split(".hero .lead{", 1)[1].split("}", 1)[0]
    assert "white-space:pre-line" in rule


@pytest.mark.parametrize("generation", ["pdf", "none"])
def test_opening_logo_binds_to_version_assets_independently_of_pdf(generation: str) -> None:
    from test_capture import capture_document

    from app.forms.definition.images import AssetRef

    document = capture_document() if generation == "none" else legacy_definition("demo")
    document["opening_logo"] = {"src": "asset:opening", "alt": "Pesquisa <sintética>"}
    original = compile_definition(document)
    ref = AssetRef("ab" * 32, "image/png")
    spec = original.with_assets({"opening": ref})
    html = templates.env.get_template("public/_hero.html").render(spec=spec, screen=spec.screen)
    assert f'src="{ref.url}"' in html
    assert 'alt="Pesquisa &lt;sintética&gt;"' in html
    assert html.index('class="hero__logo"') < html.index("<h1")
    assert original.screen.opening_logo.url is None
    assert (spec.templates is None) == (generation == "none")


def test_editor_roundtrip_and_normalization_preserve_logo_without_llm_changes() -> None:
    from starlette.datastructures import FormData

    from app.admin.form_editor import FormEdit
    from app.forms.definition.changes import compare, llm_fingerprint
    from app.forms.definition.normal import same_meaning

    document = legacy_definition("demo")
    logo = {"src": "asset:opening", "alt": "Pesquisa sintética"}
    edited = {**document, "opening_logo": logo}
    assert FormEdit.of_document(edited).screen()["opening_logo"] == logo
    form = FormEdit.of_form(
        FormData({"opening_logo.src": "asset:opening", "opening_logo.alt": "Pesquisa sintética"})
    )
    assert form.screen()["opening_logo"] == logo
    form.opening_logo_src = form.opening_logo_alt = ""
    assert "opening_logo" not in form.screen()
    assert same_meaning(document, {**document, "opening_logo": None})
    assert not same_meaning(document, edited)
    assert llm_fingerprint(document) == llm_fingerprint(edited)
    assert [section.key for section in compare(document, edited) if section.changed] == ["pagina"]


@pytest.mark.parametrize(
    "src",
    [
        "https://example.test/logo.png",
        "/img/logo.png",
        "asset:{{ logo }}",
        "asset:../logo",
        "asset:Logo",
        "asset:",
    ],
)
def test_opening_logo_rejects_nonliteral_or_invalid_asset_names(src: str) -> None:
    from app.forms.definition import DefinitionError

    document = legacy_definition("demo")
    document["opening_logo"] = {"src": src, "alt": "Pesquisa"}
    with pytest.raises(DefinitionError) as refused:
        compile_definition(document)
    assert refused.value.problems[0].path == "opening_logo.src"


def test_legacy_editor_does_not_add_an_empty_opening_logo() -> None:
    from app.admin.form_editor import FormEdit

    document = legacy_definition("demo")
    spec = compile_definition(document)
    assert spec.screen.opening_logo is None
    assert "opening_logo" not in FormEdit.of_document(document).screen()
    html = templates.env.get_template("public/_hero.html").render(spec=spec, screen=spec.screen)
    assert 'class="hero__logo"' not in html
