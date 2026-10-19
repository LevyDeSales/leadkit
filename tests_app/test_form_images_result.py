"""De ponta a ponta: a lead envia o formulário de uma versão que usa uma imagem, o PDF sai com ela e a
página de resultado a carrega pela rota do app; trocar a imagem depois não muda o pedido dela."""

from __future__ import annotations

import io
import uuid
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader
from test_public import complete, enter, tags

from app.adapters.assets import AssetStore
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.api.main import create_app
from app.deps import Deps
from app.forms import assets, drafts

ACTOR = "operadora"
SLUG = "demo"


def picture(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    image = Image.new("RGB", (240, 120), color)
    image.paste(Image.new("RGB", (60, 60), (255, 255, 255)), (20, 20))
    image.save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def client(deps: Deps) -> TestClient:
    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    deps.pdf = WeasyPrintRenderer(AssetStore(deps.storage))
    return TestClient(create_app(trigger=fake_trigger))


def publish_with_image(deps: Deps) -> assets.Asset:
    """A versão 2 do demo, com a imagem `logo-black` no PDF e na prévia."""
    logo = assets.upload(
        deps, scope="", name="logo-black", content=picture((220, 20, 40)), actor=ACTOR
    ).asset
    with deps.sessions() as session:
        draft = drafts.load(session, SLUG)
    assert draft is not None
    texts = drafts.template_texts_of(draft.document)
    image = '<img class="logo" src="asset:logo-black" alt="Logo">'
    saved = drafts.save_templates(
        deps,
        SLUG,
        drafts.TemplateTexts(
            texts.pdf_html + image, texts.pdf_css, texts.preview + image, texts.scores
        ),
        actor=ACTOR,
        expected=draft.token,
    )
    drafts.publish(deps, SLUG, actor=ACTOR, note="Com o logo", expected=saved.token)
    return logo


def images_in(pdf: bytes) -> int:
    return sum(len(page.images) for page in PdfReader(BytesIO(pdf)).pages)


def test_the_pdf_and_the_result_page_of_a_request_keep_the_image_of_its_version(
    client: TestClient, deps: Deps
) -> None:
    logo = publish_with_image(deps)
    page = enter(client)
    complete(deps, page)

    result = client.get(page)
    pdf = client.get(f"{page}/pdf")

    assert result.status_code == 200
    (shown,) = tags(result.text, "img", class_="logo")
    assert shown["src"] == logo.ref.url
    image = client.get(logo.ref.url)
    assert image.status_code == 200 and image.headers["cache-control"].endswith("immutable")
    assert pdf.status_code == 200 and images_in(pdf.content) == 1

    # A imagem é trocada: o pedido que já existe segue com a que a versão dele fixou.
    new = assets.replace(
        deps, logo.id, picture((20, 40, 220)), expected=logo.ref.sha256, actor=ACTOR
    ).asset
    deps.forms = type(deps.forms)(deps.sessions)  # sem o cache das versões compiladas
    again = client.get(page)
    (still,) = tags(again.text, "img", class_="logo")
    assert still["src"] == logo.ref.url != new.ref.url
    assert client.get(logo.ref.url).status_code == 200
    assert client.get(new.ref.url).status_code == 200


def test_a_request_sent_after_the_version_is_republished_gets_the_new_image(
    client: TestClient, deps: Deps
) -> None:
    logo = publish_with_image(deps)
    new = assets.replace(
        deps, logo.id, picture((20, 40, 220)), expected=logo.ref.sha256, actor=ACTOR
    ).asset
    with deps.sessions() as session:
        draft = drafts.load(session, SLUG)
    assert draft is not None and draft.stale_assets == ("logo-black",)
    drafts.publish(deps, SLUG, actor=ACTOR, note="Logo novo", expected=draft.token)

    page = enter(client)
    complete(deps, page)

    (shown,) = tags(client.get(page).text, "img", class_="logo")
    assert shown["src"] == new.ref.url
