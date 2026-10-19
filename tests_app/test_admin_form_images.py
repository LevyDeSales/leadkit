"""As imagens do formulário no editor do PDF e na publicação, pela costura HTTP: o painel das
imagens que o formulário enxerga, o `asset:<nome>` que não existe voltando com a linha, as prévias
com a imagem e a troca de uma imagem que deixa o formulário com o que publicar."""

from __future__ import annotations

import io
import re
import uuid
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader
from test_admin import FakeClock, login
from test_admin_destinations import ADMIN_ENV
from test_admin_drafts import TestTrigger, hidden, post
from test_admin_forms import import_form, section_of
from test_admin_pdf_editor import PAGE, fields_of, problems_shown, with_html
from test_admin_scoring import DEFINITION, SLUG
from test_public import tags, text

from app.adapters.assets import AssetStore
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.api.main import create_app
from app.deps import Deps
from app.forms import assets, drafts
from app.settings import Settings

DRAFT = f"/admin/forms/{SLUG}/draft"
PUBLISH = f"{DRAFT}/publish"
ACTOR = "operadora"
HTML = '<h1>Plano</h1>\n<img src="asset:logo-black" alt="">'


def picture(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    image = Image.new("RGB", (300, 150), color)
    image.paste(Image.new("RGB", (100, 60), (255, 255, 255)), (10, 10))
    image.save(out, "PNG")
    return out.getvalue()


RED, BLUE = picture((220, 20, 40)), picture((20, 40, 220))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(deps: Deps, clock: FakeClock) -> TestClient:
    """A operadora no admin, com o formulário de pontuação importado e o PDF de verdade, que abre as
    imagens do storage do teste."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]
    deps.pdf = WeasyPrintRenderer(AssetStore(deps.storage))

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, test_trigger=TestTrigger(deps, clock), clock=clock),
        follow_redirects=False,
    )
    assert login(current).status_code == 303
    assert import_form(current, DEFINITION).status_code == 303
    return current


def upload(deps: Deps, content: bytes, name: str = "logo-black", scope: str = "") -> assets.Asset:
    return assets.upload(deps, scope=scope, name=name, content=content, actor=ACTOR).asset


def images_in(pdf: bytes) -> int:
    return sum(len(page.images) for page in PdfReader(BytesIO(pdf)).pages)


def load(deps: Deps) -> drafts.Draft:
    with deps.sessions() as session:
        found = drafts.load(session, SLUG)
    assert found is not None
    return found


def save_with_image(client: TestClient) -> httpx.Response:
    fields = fields_of(client)
    with_html(HTML)(fields)
    return post(client, PAGE, **fields)


def publish(client: TestClient, note: str = "Com imagem") -> httpx.Response:
    page = client.get(PUBLISH)
    return post(client, PUBLISH, expected=hidden(page.text, "expected"), note=note)


# --- o painel ------------------------------------------------------------------------------------


def test_the_editor_lists_the_images_the_form_sees_and_marks_the_ones_in_use(
    client: TestClient, deps: Deps
) -> None:
    library = upload(deps, RED, "logo-black")
    upload(deps, BLUE, "capa", scope=SLUG)
    upload(deps, picture((1, 2, 3)), "de-outro-formulario", scope="mcc")

    page = client.get(PAGE).text
    panel = section_of(client.get(PAGE), "imagens")

    assert "asset:logo-black" in panel and "asset:capa" in panel
    assert "de-outro-formulario" not in panel
    assert "em uso aqui" not in panel
    thumbnails = {tag["src"] for tag in tags(panel, "img", class_="thumb")}
    assert library.ref.url in thumbnails and len(thumbnails) == 2
    assert tags(page, "a", href="/admin/assets")

    assert save_with_image(client).status_code == 303
    used = text(section_of(client.get(PAGE), "imagens"))
    assert used.count("em uso aqui") == 1 and "asset:logo-black em uso aqui" in used


def test_without_images_the_panel_says_so_and_points_to_the_images_page(
    client: TestClient,
) -> None:
    panel = text(section_of(client.get(PAGE), "imagens"))

    assert "Nenhuma imagem disponível" in panel and "Gerenciar as imagens" in panel


# --- salvar --------------------------------------------------------------------------------------


def test_saving_the_pdf_with_an_image_that_does_not_exist_shows_the_name_and_the_line(
    client: TestClient, deps: Deps
) -> None:
    before = load(deps).document

    response = save_with_image(client)

    assert response.status_code == 422
    shown = problems_shown(response)
    assert "PDF (HTML)" in shown and "linha 2" in shown
    assert "a imagem «logo-black» não existe" in shown and "envie-a em Imagens" in shown
    assert load(deps).document == before


def test_saving_the_pdf_with_an_image_that_exists_keeps_it_in_the_draft(
    client: TestClient, deps: Deps
) -> None:
    upload(deps, RED)

    response = save_with_image(client)

    assert response.status_code == 303
    assert "asset:logo-black" in load(deps).document["pdf"]["html"]


# --- as prévias ----------------------------------------------------------------------------------


def test_the_pdf_preview_shows_the_image_before_saving_and_the_result_page_loads_it_by_the_route(
    client: TestClient, deps: Deps
) -> None:
    logo = upload(deps, RED)
    fields = fields_of(client)
    fields["pdf_html"] = HTML
    fields["preview"] = '<p>{{ plan.titulo }}</p><img src="asset:logo-black" alt="">'

    pdf = post(client, f"{PAGE}/preview", **fields)
    result = post(client, f"{PAGE}/result", **fields)

    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert images_in(pdf.content) == 1
    assert result.status_code == 200
    assert f'src="{logo.ref.url}"' in result.text and "asset:" not in result.text
    assert client.get(logo.ref.url).status_code == 200  # o quadro do celular acha a imagem


def test_the_preview_with_an_image_that_does_not_exist_names_it_in_the_frame(
    client: TestClient,
) -> None:
    fields = fields_of(client)
    fields["pdf_html"] = HTML

    response = post(client, f"{PAGE}/preview", **fields)

    assert response.status_code == 422
    assert "a imagem «logo-black» não existe" in text(response.text)


# --- trocar e publicar ---------------------------------------------------------------------------


@pytest.fixture
def published(client: TestClient, deps: Deps) -> assets.Asset:
    """O formulário na versão 2, que usa a imagem `logo-black` enviada em vermelho."""
    logo = upload(deps, RED)
    assert save_with_image(client).status_code == 303
    assert publish(client).status_code == 303
    assert load(deps).current.number == 2  # type: ignore[union-attr]
    return logo


def test_replacing_a_published_image_leaves_the_form_with_something_to_publish(
    client: TestClient, deps: Deps, published: assets.Asset
) -> None:
    assert not load(deps).pending

    assets.replace(deps, published.id, BLUE, expected=published.ref.sha256, actor=ACTOR)

    form_page = client.get(f"/admin/forms/{SLUG}")
    rascunho = text(section_of(form_page, "rascunho"))
    assert "logo-black" in rascunho and "foi trocada desde a versão 2" in rascunho
    assert "Publicar…" in rascunho
    listed = text(client.get("/admin/forms").text)
    assert "Rascunho pendente" in listed
    editor_page = client.get(PAGE)
    assert "imagem trocada: logo-black" in text(editor_page.text)
    assert tags(editor_page.text, "a", href=PUBLISH)


def test_the_publish_page_says_which_images_the_new_version_takes_and_publishes_them(
    client: TestClient, deps: Deps, published: assets.Asset
) -> None:
    swapped = assets.replace(
        deps, published.id, BLUE, expected=published.ref.sha256, actor=ACTOR
    ).asset

    page = client.get(PUBLISH)

    assert page.status_code == 200
    notice = text(section_of(page, "mudancas"))
    assert "logo-black foi trocada desde a versão 2" in notice
    assert "A versão 3 fixa a nova" in notice
    response = publish(client, "Logo novo")
    assert response.status_code == 303
    with deps.sessions() as session:
        pinned = assets.version_refs(session, SLUG, 3)
        old = assets.version_refs(session, SLUG, 2)
    assert pinned == {"logo-black": swapped.ref} and old == {"logo-black": published.ref}
    assert not load(deps).pending


def test_the_form_without_the_replaced_image_is_not_asked_to_publish(
    client: TestClient, deps: Deps
) -> None:
    other = upload(deps, RED, "outra")

    assets.replace(deps, other.id, BLUE, expected=other.ref.sha256, actor=ACTOR)

    assert not load(deps).pending
    assert "Rascunho pendente" not in text(client.get("/admin/forms").text)


def test_the_page_of_a_form_that_was_not_published_with_images_is_untouched(
    client: TestClient,
) -> None:
    page = client.get(f"/admin/forms/{SLUG}")

    assert not re.search(r"foi trocada|foram trocadas", page.text)
