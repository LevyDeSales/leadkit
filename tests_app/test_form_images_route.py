"""A rota pública das imagens (`/img/<sha256>.<extensão>`): cache longo pelo conteúdo, só o que o
admin ou uma versão publicada tem, e o bucket sem aparecer."""

from __future__ import annotations

import hashlib
import io
import uuid

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from PIL import Image
from test_admin import FakeClock
from test_admin_forms import DEFINITION, SLUG
from test_form_images_publication import PDF_HTML, PREVIEW

from app.adapters.assets import SHARED_CACHE, AssetStore
from app.api.main import create_app
from app.deps import Deps
from app.errors import FailureCode, RetryableError
from app.forms import assets, drafts, stored
from app.forms.definition.images import AssetRef
from app.models import FormVersionAsset

ACTOR = "operadora"


def picture(color: tuple[int, int, int] = (200, 30, 60), form: str = "PNG") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (300, 200), color).save(out, form)
    return out.getvalue()


@pytest.fixture
def client(deps: Deps) -> TestClient:
    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    return TestClient(create_app(trigger=fake_trigger, clock=FakeClock()), follow_redirects=False)


def sent(deps: Deps, color: tuple[int, int, int] = (200, 30, 60), name: str = "logo-black"):
    return assets.upload(deps, scope="", name=name, content=picture(color), actor=ACTOR).asset


def test_an_image_of_the_admin_is_served_with_its_type_and_a_cache_that_never_expires(
    client: TestClient, deps: Deps
) -> None:
    asset = sent(deps)

    response = client.get(asset.ref.url)

    assert response.status_code == 200
    assert response.headers["content-type"] == asset.ref.content_type
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["etag"] == f'"{asset.ref.sha256}"'
    assert Image.open(io.BytesIO(response.content)).size == (asset.width, asset.height)


def test_the_image_comes_with_headers_that_keep_it_only_an_image(
    client: TestClient, deps: Deps
) -> None:
    response = client.get(sent(deps).ref.url)

    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert "set-cookie" not in response.headers


def test_a_browser_that_already_has_the_image_gets_a_304_without_the_body(
    client: TestClient, deps: Deps
) -> None:
    asset = sent(deps)

    response = client.get(asset.ref.url, headers={"If-None-Match": f'"{asset.ref.sha256}"'})

    assert response.status_code == 304 and response.content == b""
    assert response.headers["cache-control"].endswith("immutable")


@pytest.mark.parametrize("etag", ['W/"{sha}"', '"outro", "{sha}"', "*"])
def test_a_weak_or_listed_etag_also_counts_as_having_the_image(
    client: TestClient, deps: Deps, etag: str
) -> None:
    asset = sent(deps)

    response = client.get(
        asset.ref.url, headers={"If-None-Match": etag.format(sha=asset.ref.sha256)}
    )

    assert response.status_code == 304


def test_the_route_needs_no_session(client: TestClient, deps: Deps) -> None:
    asset = sent(deps)

    assert TestClient(client.app).get(asset.ref.url).status_code == 200


@pytest.mark.parametrize(
    "filename",
    ["x.png", f"{'0' * 64}.png", f"{'a' * 63}.png", f"{'a' * 64}.gif", f"{'a' * 64}.svg"],
)
def test_anything_that_is_not_a_content_name_is_a_404_without_cache(
    client: TestClient, deps: Deps, filename: str
) -> None:
    sent(deps)

    response = client.get(f"/img/{filename}")

    assert response.status_code == 404 and response.headers["cache-control"] == "no-store"


def test_a_file_in_the_storage_that_no_image_or_version_has_is_not_served(
    client: TestClient, deps: Deps
) -> None:
    """O sha256 de um arquivo qualquer do storage não abre nada: só o que o admin ou uma versão
    publicada tem."""
    content = picture((5, 5, 5))
    ref = AssetRef(hashlib.sha256(content).hexdigest(), "image/png")
    AssetStore(deps.storage).put(ref, content)

    assert client.get(ref.url).status_code == 404


def test_the_extension_has_to_be_the_type_of_the_image(client: TestClient, deps: Deps) -> None:
    asset = sent(deps)
    other = AssetRef(
        asset.ref.sha256, "image/webp" if asset.ref.extension != "webp" else "image/png"
    )

    assert client.get(other.url).status_code == 404


def test_an_image_that_was_replaced_is_still_served_while_a_version_pins_it(
    client: TestClient, deps: Deps
) -> None:
    """O resultado de um pedido antigo ainda mostra a imagem que a versão dele fixou."""
    old = sent(deps)
    stored.import_definition(deps, DEFINITION, actor=ACTOR)
    with deps.sessions() as session:
        draft = drafts.load(session, SLUG)
    assert draft is not None
    texts = drafts.template_texts_of(draft.document)
    drafts.save_templates(
        deps,
        SLUG,
        drafts.TemplateTexts(PDF_HTML, texts.pdf_css, PREVIEW, texts.scores),
        actor=ACTOR,
        expected=draft.token,
    )
    with deps.sessions() as session:
        draft = drafts.load(session, SLUG)
    assert draft is not None
    drafts.publish(deps, SLUG, actor=ACTOR, note="Com imagem", expected=draft.token)

    new = assets.replace(
        deps, old.id, picture((9, 9, 9)), expected=old.ref.sha256, actor=ACTOR
    ).asset

    assert client.get(new.ref.url).status_code == 200
    assert client.get(old.ref.url).status_code == 200  # fixada pela versão 2
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(FormVersionAsset)) == 1


def test_an_image_that_left_the_system_is_a_404(client: TestClient, deps: Deps) -> None:
    asset = sent(deps)
    assets.remove(deps, asset.id, expected=asset.ref.sha256, actor=ACTOR)

    assert client.get(asset.ref.url).status_code == 404


def test_a_missing_file_is_a_404_and_a_passing_storage_failure_is_a_503(
    client: TestClient, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = sent(deps)
    deps.storage.delete(asset.ref.key)
    SHARED_CACHE.clear()
    assert client.get(asset.ref.url).status_code == 404

    def blip(key: str) -> bytes:
        raise RetryableError(FailureCode.STORAGE_FAILED, "get_object: SlowDown (HTTP 503)")

    monkeypatch.setattr(deps.storage, "get", blip)
    response = client.get(asset.ref.url)
    assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
    assert "Retry-After" in response.headers
