"""O PDF abre as imagens do formulário (`asset:<nome>`) do storage, pelo conteúdo que a versão
fixou, e recusa a que não existe ou que o storage não entrega."""

from __future__ import annotations

import io
import random
import socket
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pypdf import PdfReader
from test_weasyprint_pdf import EXAMPLE_PLAN, context, form_with_template, text_of

from app.adapters.assets import AssetStore, _Cache
from app.adapters.storage import LocalStorage
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.errors import FailureCode, RetryableError, TerminalError
from app.forms import FormSpec
from app.forms.definition.images import AssetRef
from app.forms.image_optimizer import optimize


def picture(color: tuple[int, int, int], form: str, size: tuple[int, int] = (120, 80)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, form)
    return out.getvalue()


class Library:
    """Imagens guardadas num storage de teste, como o admin as guarda: otimizadas e pelo sha256."""

    def __init__(self, root: Path) -> None:
        self.storage = LocalStorage(root)
        self.store = AssetStore(self.storage, _Cache())

    def add(self, content: bytes) -> AssetRef:
        image = optimize(content)
        ref = AssetRef(image.sha256, image.content_type)
        self.store.put(ref, image.content)
        return ref

    def renderer(self) -> WeasyPrintRenderer:
        return WeasyPrintRenderer(self.store)


@pytest.fixture
def library(tmp_path: Path) -> Library:
    return Library(tmp_path / "bucket")


def spec_with(html: str, assets: dict[str, AssetRef], css: str | None = None) -> FormSpec:
    return form_with_template(html, css).with_assets(assets)


def images_of(pdf: bytes) -> int:
    return sum(len(page.images) for page in PdfReader(BytesIO(pdf)).pages)


@pytest.mark.parametrize("form", ["PNG", "JPEG", "WEBP"])
def test_the_pdf_shows_an_image_of_the_form_by_its_name(library: Library, form: str) -> None:
    logo = library.add(picture((200, 30, 60), form))
    spec = spec_with('<p>Plano</p><img src="asset:logo-black">', {"logo-black": logo})

    rendered = library.renderer().render(spec, EXAMPLE_PLAN, context())

    assert images_of(rendered.content) == 1 and "Plano" in text_of(rendered.content)


def test_a_pdf_larger_than_the_ipc_buffer_is_received_before_waiting_for_the_engine(
    library: Library,
) -> None:
    """Ruído sintético incompressível faz o recurso e o PDF excederem o buffer do socket."""
    output = io.BytesIO()
    pixels = random.Random(17).randbytes(800 * 800 * 3)
    Image.frombytes("RGB", (800, 800), pixels).save(output, "PNG")
    ref = library.add(output.getvalue())
    spec = spec_with('<p>Plano grande</p><img src="asset:large">', {"large": ref})

    rendered = library.renderer().render(spec, EXAMPLE_PLAN, context())

    assert len(rendered.content) > 512 * 1024
    assert images_of(rendered.content) == 1
    assert "Plano grande" in text_of(rendered.content)


def test_the_css_can_ask_for_an_image_too(library: Library) -> None:
    capa = library.add(picture((20, 20, 120), "PNG"))
    spec = spec_with(
        '<div class="capa">Capa</div>',
        {"capa": capa},
        css=".capa { height: 100px; background: url(asset:capa) }",
    )

    rendered = library.renderer().render(spec, EXAMPLE_PLAN, context())

    assert rendered.page_count == 1 and "Capa" in text_of(rendered.content)


def test_two_names_for_the_same_content_are_one_file_in_the_storage(library: Library) -> None:
    same = picture((10, 120, 30), "PNG")
    one, two = library.add(same), library.add(same)
    spec = spec_with('<img src="asset:a"><img src="asset:b">', {"a": one, "b": two})

    rendered = library.renderer().render(spec, EXAMPLE_PLAN, context())

    assert one == two and len(list((library.storage.root / "assets").iterdir())) == 1
    assert images_of(rendered.content) >= 1


def test_the_same_pinned_content_gives_the_same_pdf_and_other_content_gives_another(
    library: Library,
) -> None:
    """A versão fixa o conteúdo de cada nome: o PDF só muda se o que ela fixou mudar."""
    red = library.add(picture((255, 0, 0), "PNG"))
    blue = library.add(picture((0, 0, 255), "PNG"))
    html = '<img src="asset:logo">'

    first = library.renderer().render(spec_with(html, {"logo": red}), EXAMPLE_PLAN, context())
    again = library.renderer().render(spec_with(html, {"logo": red}), EXAMPLE_PLAN, context())
    other = library.renderer().render(spec_with(html, {"logo": blue}), EXAMPLE_PLAN, context())

    assert first.content == again.content
    assert other.content != first.content


def test_an_image_the_form_does_not_have_fails_the_pdf_without_saying_the_name(
    library: Library,
) -> None:
    spec = spec_with('<img src="asset:{{ plan.titulo }}">', {})

    with pytest.raises(TerminalError) as caught:
        library.renderer().render(spec, EXAMPLE_PLAN, context())

    assert caught.value.code is FailureCode.RENDER_FAILED
    assert "imagem do formulário" in caught.value.detail
    assert EXAMPLE_PLAN["titulo"] not in str(caught.value)


def test_a_renderer_without_a_store_cannot_open_an_image() -> None:
    ref = AssetRef("ab" * 32, "image/png")
    spec = spec_with('<img src="asset:logo">', {"logo": ref})

    with pytest.raises(TerminalError, match="imagem do formulário"):
        WeasyPrintRenderer().render(spec, EXAMPLE_PLAN, context())


def test_an_image_missing_from_the_storage_fails_the_pdf(library: Library) -> None:
    ref = library.add(picture((1, 2, 3), "PNG"))
    library.storage.delete(ref.key)
    library.store._cache.clear()  # type: ignore[attr-defined]
    spec = spec_with('<img src="asset:logo">', {"logo": ref})

    with pytest.raises(TerminalError, match="imagem do formulário indisponível"):
        library.renderer().render(spec, EXAMPLE_PLAN, context())


def test_a_storage_blip_while_reading_an_image_is_retried_not_final(library: Library) -> None:
    ref = library.add(picture((1, 2, 3), "PNG"))
    library.store._cache.clear()  # type: ignore[attr-defined]

    def blip(key: str) -> bytes:
        raise RetryableError(FailureCode.STORAGE_FAILED, "get_object: SlowDown (HTTP 503)")

    library.storage.get = blip  # type: ignore[method-assign]
    spec = spec_with('<img src="asset:logo">', {"logo": ref})

    with pytest.raises(RetryableError) as caught:
        library.renderer().render(spec, EXAMPLE_PLAN, context())

    assert caught.value.code is FailureCode.STORAGE_FAILED


def test_a_file_in_the_storage_that_is_not_the_pinned_content_is_refused(
    library: Library,
) -> None:
    ref = library.add(picture((9, 9, 9), "PNG"))
    library.storage.put(ref.key, picture((1, 1, 1), "PNG"), ref.content_type)
    library.store._cache.clear()  # type: ignore[attr-defined]
    spec = spec_with('<img src="asset:logo">', {"logo": ref})

    with pytest.raises(TerminalError, match="indisponível"):
        library.renderer().render(spec, EXAMPLE_PLAN, context())


@pytest.mark.parametrize(
    "address", ["asset://example.test/logo", "asset:../logo", "asset:logo/../../x", "ASSET:outro"]
)
def test_only_a_plain_name_of_the_form_resolves(
    library: Library, monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    attempts: list[Any] = []
    monkeypatch.setattr(socket.socket, "connect", lambda *args: attempts.append(args))
    ref = library.add(picture((1, 2, 3), "PNG"))
    spec = spec_with(f'<img src="{address}">', {"logo": ref})

    with pytest.raises(TerminalError):
        library.renderer().render(spec, EXAMPLE_PLAN, context())

    assert attempts == []
