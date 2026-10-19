"""As imagens dos formulários no admin, pela costura HTTP: enviar com a otimização à vista, listar,
ver, trocar e remover, com a sessão, o CSRF e a auditoria do resto do painel."""

from __future__ import annotations

import io
import random
import uuid

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from PIL import Image
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from test_admin import ORIGIN, FakeClock, csrf_token, login
from test_admin_destinations import ADMIN_ENV
from test_admin_forms import DEFINITION, SLUG
from test_public import tags, text

from app.adapters.assets import AssetStore, AssetUnavailable
from app.api.main import MAX_BODY_BYTES, create_app
from app.deps import Deps
from app.forms import assets, drafts, stored
from app.forms.definition.images import AssetRef
from app.models import AuditLog, FormAsset
from app.settings import Settings

ASSETS = "/admin/assets"
ACTOR = "operadora"


def picture(
    color: tuple[int, int, int] = (200, 30, 60),
    form: str = "PNG",
    size: tuple[int, int] = (3000, 1000),
) -> bytes:
    out = io.BytesIO()
    image = Image.new("RGB", size, color)
    image.paste(Image.new("RGB", (size[0] // 2, size[1] // 2), (255, 255, 255)), (10, 10))
    image.save(out, form)
    return out.getvalue()


@pytest.fixture
def client(deps: Deps) -> TestClient:
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, clock=FakeClock()), follow_redirects=False
    )
    assert login(current).status_code == 303
    return current


def send(
    client: TestClient,
    name: str = "logo-black",
    content: bytes | None = None,
    *,
    scope: str = "",
    filename: str = "logo.png",
    token: bool = True,
    headers: dict[str, str] | None = ORIGIN,
) -> httpx.Response:
    data = {"name": name, "scope": scope}
    if token:
        data["csrf_token"] = csrf_token(client)
    files = {"file": (filename, content if content is not None else picture(), "image/png")}
    return client.post(ASSETS, data=data, files=files, headers=headers)


def sent(deps: Deps, name: str = "logo-black", scope: str = "", **kwargs: bytes) -> assets.Asset:
    content = kwargs.get("content", picture())
    return assets.upload(deps, scope=scope, name=name, content=content, actor=ACTOR).asset


def open_page(client: TestClient, response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    return page.text


def count(deps: Deps) -> int:
    with deps.sessions() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(FormAsset)) or 0


def audited(deps: Deps, action: str) -> list[AuditLog]:
    with deps.sessions() as session:
        return list(session.scalars(sa.select(AuditLog).where(AuditLog.action == action)))


def posted(client: TestClient, url: str, **fields: str) -> httpx.Response:
    return client.post(url, data={"csrf_token": csrf_token(client), **fields}, headers=ORIGIN)


def replace(
    client: TestClient, asset: assets.Asset, content: bytes, expected: str | None = None
) -> httpx.Response:
    return client.post(
        f"{ASSETS}/{asset.id}/replace",
        data={"csrf_token": csrf_token(client), "expected": expected or asset.ref.sha256},
        files={"file": ("nova.png", content, "image/png")},
        headers=ORIGIN,
    )


# --- a sessão e a lista --------------------------------------------------------------------------


def test_the_pages_need_a_session(client: TestClient, deps: Deps) -> None:
    asset = sent(deps)
    anonymous = TestClient(client.app, follow_redirects=False)

    for url in (ASSETS, f"{ASSETS}/{asset.id}"):
        response = anonymous.get(url)
        assert response.status_code == 303 and response.headers["location"].startswith(
            "/admin/login"
        )


def test_the_top_navigation_has_the_images_section(client: TestClient) -> None:
    page = client.get(ASSETS).text

    links = tags(page, "a", href=ASSETS)
    assert links and links[0].get("aria-current") == "page"
    assert tags(client.get("/admin/forms").text, "a", href=ASSETS)


def test_the_empty_list_offers_the_upload_form_with_the_library_and_every_form(
    client: TestClient,
) -> None:
    page = client.get(ASSETS).text

    assert "Nenhuma imagem enviada ainda" in text(page)
    options = [option["value"] for option in tags(page, "option")]
    assert options[0] == "" and {"demo", "mcc", "reforma-2027"} <= set(options)
    (file_input,) = tags(page, "input", type="file")
    assert "image/webp" in (file_input["accept"] or "")
    assert "SVG" in text(page)


def test_the_list_shows_each_image_with_its_scope_and_how_to_ask_for_it(
    client: TestClient, deps: Deps
) -> None:
    library = sent(deps, "logo-black")
    own = sent(deps, "capa", scope="demo", content=picture((1, 2, 3)))

    page = client.get(ASSETS).text

    assert "asset:logo-black" in page and "asset:capa" in page
    assert "Biblioteca geral" in text(page) and "Projeto de demonstração" in text(page)
    thumbnails = {tag["src"] for tag in tags(page, "img", class_="thumb")}
    assert thumbnails == {library.ref.url, own.ref.url}
    assert tags(page, "a", href=f"{ASSETS}/{library.id}")


# --- enviar --------------------------------------------------------------------------------------


def test_sending_an_image_shows_what_the_optimization_did_before_and_after(
    client: TestClient, deps: Deps
) -> None:
    content = picture()

    response = send(client, content=content)

    assert response.status_code == 303
    page = text(open_page(client, response))
    assert "Imagem «logo-black» enviada" in page
    assert "Antes:" in page and "PNG, 3000 × 1000" in page
    assert "Depois:" in page and "2000 × 667" in page and "menor" in page
    assert count(deps) == 1
    (entry,) = audited(deps, "asset.upload")
    assert entry.actor == "operadora" and entry.details["name"] == "logo-black"


def test_the_image_is_stored_optimized_by_content(client: TestClient, deps: Deps) -> None:
    send(client)

    with deps.sessions() as session:
        (asset,) = assets.listing(session)
    stored_bytes = AssetStore(deps.storage).read(asset.ref)
    assert asset.ref.key == f"assets/{asset.ref.sha256}.{asset.ref.extension}"
    assert Image.open(io.BytesIO(stored_bytes)).size == (asset.width, asset.height) == (2000, 667)


def test_an_image_can_be_sent_to_one_form(client: TestClient, deps: Deps) -> None:
    send(client, scope="mcc")

    with deps.sessions() as session:
        (asset,) = assets.listing(session)
    assert asset.form_slug == "mcc"


@pytest.mark.parametrize(
    ("kwargs", "field", "reason"),
    [
        (
            {"content": b"<svg xmlns='http://www.w3.org/2000/svg'/>", "filename": "a.svg"},
            "file",
            "SVG",
        ),
        ({"content": b"nao e imagem"}, "file", "PNG, JPEG ou WebP"),
        ({"content": b""}, "file", "escolha o arquivo"),
        ({"name": "Logo Preto"}, "name", "minúsculas"),
        ({"name": ""}, "name", "dê um nome"),
        ({"scope": "nao-existe"}, "scope", "formulário não encontrado"),
    ],
)
def test_a_refused_upload_comes_back_with_the_reason_in_its_field_and_stores_nothing(
    client: TestClient, deps: Deps, kwargs: dict[str, object], field: str, reason: str
) -> None:
    response = send(client, **kwargs)  # type: ignore[arg-type]

    assert response.status_code == 422
    errors = [tag.get("id") for tag in tags(response.text, "p", class_="error")]
    assert f"f-{field}-erro" in errors and reason in text(response.text)
    assert count(deps) == 0 and audited(deps, "asset.upload") == []
    assert not (deps.storage.root / "assets").exists()  # type: ignore[attr-defined]


def test_a_refused_upload_keeps_what_the_operator_typed(client: TestClient) -> None:
    response = send(client, "Logo Preto", scope="mcc")

    (name,) = tags(response.text, "input", id="f-name")
    assert name["value"] == "Logo Preto"
    selected = [option for option in tags(response.text, "option") if "selected" in option]
    assert [option["value"] for option in selected] == ["mcc"]


def test_a_name_that_is_taken_in_the_scope_is_refused_pointing_to_the_replacement(
    client: TestClient, deps: Deps
) -> None:
    sent(deps, "logo-black")

    response = send(client, "logo-black", picture((9, 9, 9)))

    assert response.status_code == 422 and "troque a imagem que existe" in text(response.text)
    assert count(deps) == 1


@pytest.mark.parametrize("variant", ["no-token", "no-origin", "other-origin"])
def test_a_post_without_the_token_or_from_another_origin_changes_nothing(
    client: TestClient, deps: Deps, variant: str
) -> None:
    kwargs: dict[str, object] = {
        "no-token": {"token": False},
        "no-origin": {"headers": None},
        "other-origin": {"headers": {"Origin": "https://outro.example.test"}},
    }[variant]

    response = send(client, **kwargs)  # type: ignore[arg-type]

    assert response.status_code == 403 and count(deps) == 0


class CountingBody:
    """Envolve o app e conta quantas vezes o corpo do pedido foi lido (`receive`)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.reads = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def counting() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                self.reads += 1
            return message

        await self.app(scope, counting, send)


@pytest.fixture
def watched(client: TestClient) -> tuple[TestClient, CountingBody]:
    counter = CountingBody(client.app)
    return TestClient(counter, follow_redirects=False), counter


UPLOADS = [ASSETS, f"{ASSETS}/0b0a8f3e-6f0b-4c2e-9d57-3f1d1c1f2a10/replace"]


@pytest.mark.parametrize("url", UPLOADS)
def test_an_upload_without_a_session_is_refused_before_the_body_is_read(
    watched: tuple[TestClient, CountingBody], deps: Deps, url: str
) -> None:
    anonymous, counter = watched

    response = anonymous.post(
        url, files={"file": ("a.png", picture(), "image/png")}, headers=ORIGIN
    )

    assert response.status_code == 303 and response.headers["location"].startswith("/admin/login")
    assert counter.reads == 0 and count(deps) == 0


@pytest.mark.parametrize("url", UPLOADS)
def test_an_upload_from_another_origin_is_refused_before_the_body_is_read(
    watched: tuple[TestClient, CountingBody], deps: Deps, url: str
) -> None:
    operator, counter = watched
    assert login(operator).status_code == 303
    token = csrf_token(operator)
    counter.reads = 0

    response = operator.post(
        url,
        data={"csrf_token": token},
        files={"file": ("a.png", picture(), "image/png")},
        headers={"Origin": "https://outro.example.test"},
    )

    assert response.status_code == 403 and counter.reads == 0 and count(deps) == 0


def test_an_upload_with_a_session_and_the_right_origin_reads_the_body_and_is_stored(
    watched: tuple[TestClient, CountingBody], deps: Deps
) -> None:
    operator, counter = watched
    assert login(operator).status_code == 303
    token = csrf_token(operator)
    counter.reads = 0

    response = operator.post(
        ASSETS,
        data={"csrf_token": token, "name": "logo-black", "scope": ""},
        files={"file": ("logo.png", picture(), "image/png")},
        headers=ORIGIN,
    )

    assert response.status_code == 303 and counter.reads >= 1 and count(deps) == 1


def test_the_body_of_an_image_may_pass_the_limit_of_the_other_posts_but_not_its_own(
    client: TestClient, deps: Deps
) -> None:
    rng = random.Random(3)
    noise = Image.frombytes(
        "RGB", (400, 400), bytes(rng.randrange(256) for _ in range(400 * 400 * 3))
    )
    out = io.BytesIO()
    noise.save(out, "PNG")
    big = out.getvalue()
    assert len(big) > MAX_BODY_BYTES

    accepted = send(client, content=big)
    too_big = client.post(
        ASSETS,
        data={"csrf_token": csrf_token(client), "name": "enorme", "scope": ""},
        files={"file": ("a.png", b"0" * (9 * 1024 * 1024), "image/png")},
        headers=ORIGIN,
    )
    elsewhere = client.post(
        "/admin/forms/import",
        data={"csrf_token": csrf_token(client)},
        files={"file": ("a.json", b"0" * (MAX_BODY_BYTES + 1), "application/json")},
        headers=ORIGIN,
    )

    assert accepted.status_code == 303 and count(deps) == 1
    assert too_big.status_code == 413 and elsewhere.status_code == 413


# --- ver -----------------------------------------------------------------------------------------


def test_the_detail_shows_the_image_where_it_is_used_and_how_the_file_came_out(
    client: TestClient, deps: Deps
) -> None:
    content = picture()
    asset = sent(deps, content=content)

    page = client.get(f"{ASSETS}/{asset.id}").text

    (shown,) = tags(page, "img", class_="asset-preview")
    assert shown["src"] == asset.ref.url
    visible = text(page)
    assert "asset:logo-black" in visible and asset.ref.sha256 in visible
    assert "Nenhuma versão publicada usa esta imagem" in visible
    assert "a otimização deixou o arquivo" in visible


def test_the_detail_lists_the_published_versions_that_use_the_image(
    client: TestClient, deps: Deps
) -> None:
    asset = sent(deps)
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    with deps.sessions() as session:  # fechada ao sair: aberta, seguraria um lock contra o TRUNCATE
        texts = drafts.template_texts_of(
            drafts.load(session, SLUG).document  # type: ignore[union-attr, arg-type]
        )
    new = drafts.TemplateTexts(
        texts.pdf_html + '<img src="asset:logo-black">', texts.pdf_css, texts.preview, texts.scores
    )
    with deps.sessions() as session:
        token = drafts.load(session, SLUG).token  # type: ignore[union-attr]
    drafts.save_templates(deps, SLUG, new, actor=ACTOR, expected=token)
    with deps.sessions() as session:
        token = drafts.load(session, SLUG).token  # type: ignore[union-attr]
    drafts.publish(deps, SLUG, actor=ACTOR, note="Com imagem", expected=token)

    page = client.get(f"{ASSETS}/{asset.id}").text

    assert f"{SLUG}, versão 2" in text(page)
    assert tags(page, "a", href=f"/admin/forms/{SLUG}")
    assert "não pode ser removida" in text(page)


def test_an_image_that_does_not_exist_is_a_404(client: TestClient) -> None:
    for url in (f"{ASSETS}/{uuid.uuid4()}", f"{ASSETS}/nao-e-um-id"):
        assert client.get(url).status_code == 404


# --- trocar --------------------------------------------------------------------------------------


def test_replacing_an_image_keeps_the_name_and_says_the_published_versions_keep_the_old_one(
    client: TestClient, deps: Deps
) -> None:
    old = sent(deps)

    response = replace(client, old, picture((9, 9, 9), size=(800, 400)))

    page = text(open_page(client, response))
    assert "Imagem «logo-black» trocada" in page and "800 × 400" in page
    assert "seguem com a imagem anterior" in page and "publique o formulário" in page
    with deps.sessions() as session:
        (now,) = assets.listing(session)
    assert now.id == old.id and now.ref != old.ref
    (entry,) = audited(deps, "asset.replace")
    assert entry.details["previous_sha256"] == old.ref.sha256
    assert "Trocada por" in page


def test_a_replacement_body_may_pass_the_limit_of_the_other_posts_but_not_its_own(
    client: TestClient, deps: Deps
) -> None:
    """A troca é um POST em `/admin/assets/<id>/replace`: leva o arquivo, e por isso o limite de
    imagem (o envio, em `/admin/assets`, está em outro teste). A remoção, que não leva arquivo, não."""
    rng = random.Random(5)
    noise = Image.frombytes(
        "RGB", (400, 400), bytes(rng.randrange(256) for _ in range(400 * 400 * 3))
    )
    out = io.BytesIO()
    noise.save(out, "PNG")
    big = out.getvalue()
    assert len(big) > MAX_BODY_BYTES
    old = sent(deps)

    accepted = replace(client, old, big)
    with deps.sessions() as session:
        (replaced,) = assets.listing(session)
    too_big = client.post(
        f"{ASSETS}/{old.id}/replace",
        data={"csrf_token": csrf_token(client), "expected": replaced.ref.sha256},
        files={"file": ("a.png", b"0" * (9 * 1024 * 1024), "image/png")},
        headers=ORIGIN,
    )
    removal = client.post(
        f"{ASSETS}/{old.id}/remove",
        data={
            "csrf_token": csrf_token(client),
            "expected": replaced.ref.sha256,
            "confirm": "on",
            "rascunho": "x" * (MAX_BODY_BYTES + 1),
        },
        headers=ORIGIN,
    )

    assert accepted.status_code == 303 and replaced.ref != old.ref
    assert too_big.status_code == 413 and removal.status_code == 413
    with deps.sessions() as session:
        assert assets.find(session, old.id) == replaced


def test_replacing_with_the_same_image_changes_nothing(client: TestClient, deps: Deps) -> None:
    content = picture()
    old = sent(deps, content=content)

    response = replace(client, old, content)

    assert "A imagem já era esta" in text(open_page(client, response))
    assert audited(deps, "asset.replace") == []


def test_a_bad_replacement_comes_back_with_the_reason_and_the_image_untouched(
    client: TestClient, deps: Deps
) -> None:
    old = sent(deps)

    response = replace(client, old, b"<svg/>")

    assert response.status_code == 422 and "SVG" in text(response.text)
    with deps.sessions() as session:
        assert assets.find(session, old.id) == old


def test_a_replacement_from_a_page_opened_before_another_one_is_refused(
    client: TestClient, deps: Deps
) -> None:
    old = sent(deps)
    assert replace(client, old, picture((9, 9, 9))).status_code == 303

    response = replace(client, old, picture((7, 7, 7)))  # `old` ainda traz o sha256 de antes

    assert response.status_code == 409 and "A imagem mudou desde que você abriu" in text(
        response.text
    )
    assert len(audited(deps, "asset.replace")) == 1


# --- remover -------------------------------------------------------------------------------------


def test_removing_needs_the_confirmation(client: TestClient, deps: Deps) -> None:
    asset = sent(deps)

    response = posted(client, f"{ASSETS}/{asset.id}/remove", expected=asset.ref.sha256)

    assert response.status_code == 422 and "Confirme que entende" in text(response.text)
    assert count(deps) == 1


def test_removing_an_image_goes_back_to_the_list_and_deletes_the_file(
    client: TestClient, deps: Deps
) -> None:
    asset = sent(deps)

    response = posted(
        client, f"{ASSETS}/{asset.id}/remove", expected=asset.ref.sha256, confirm="on"
    )

    assert response.status_code == 303 and response.headers["location"] == ASSETS
    assert "Imagem «logo-black» removida" in text(client.get(ASSETS).text)
    assert count(deps) == 0
    with pytest.raises(AssetUnavailable):
        AssetStore(deps.storage).read(asset.ref)
    assert audited(deps, "asset.remove")


def test_removing_an_image_a_published_version_pins_is_refused_and_says_which(
    client: TestClient, deps: Deps
) -> None:
    asset = sent(deps)
    with deps.sessions.begin() as session:
        from app.models import Form, FormVersion, FormVersionAsset

        version_id = session.scalar(
            sa.select(FormVersion.id)
            .join(Form, Form.id == FormVersion.form_id)
            .where(Form.slug == "mcc")
        )
        session.add(
            FormVersionAsset(
                form_version_id=version_id,
                name="logo-black",
                asset_id=asset.id,
                sha256=asset.ref.sha256,
                content_type=asset.ref.content_type,
            )
        )

    response = posted(
        client, f"{ASSETS}/{asset.id}/remove", expected=asset.ref.sha256, confirm="on"
    )

    assert response.status_code == 409
    assert "mcc v1" in text(response.text) and "não pode ser removida" in text(response.text)
    assert count(deps) == 1 and audited(deps, "asset.remove") == []


def test_removing_from_a_page_opened_before_a_replacement_is_refused(
    client: TestClient, deps: Deps
) -> None:
    old = sent(deps)
    assert replace(client, old, picture((9, 9, 9))).status_code == 303

    response = posted(client, f"{ASSETS}/{old.id}/remove", expected=old.ref.sha256, confirm="on")

    assert response.status_code == 409 and count(deps) == 1


def test_the_other_forms_of_the_page_need_the_token_too(client: TestClient, deps: Deps) -> None:
    asset = sent(deps)
    files = {"file": ("a.png", picture(), "image/png")}

    replacing = client.post(
        f"{ASSETS}/{asset.id}/replace",
        data={"expected": asset.ref.sha256},
        files=files,
        headers=ORIGIN,
    )
    removing = client.post(
        f"{ASSETS}/{asset.id}/remove",
        data={"expected": asset.ref.sha256, "confirm": "on"},
        headers=ORIGIN,
    )

    assert (replacing.status_code, removing.status_code) == (403, 403)
    with deps.sessions() as session:
        assert assets.find(session, asset.id) == asset
    assert AssetRef(asset.ref.sha256, asset.ref.content_type) == asset.ref
