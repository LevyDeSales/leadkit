"""Cópias conservam a imagem efetiva da abertura, sem resolver outro escopo por engano."""

import pytest
from fastapi.testclient import TestClient
from test_form_assets import picture, send

from app.admin.actions.previews import public_page
from app.api.main import create_app
from app.forms import assets, drafts

ACTOR = "operadora"


def load(deps, slug):
    with deps.sessions() as session:
        return drafts.load(session, slug)


def source_with_logo(deps, *, collision=False, published=False, generation="none"):
    if published and generation == "pdf":
        slug = "demo"
    else:
        slug = drafts.create(
            deps, title="Origem sintética", slug="source-logo", actor=ACTOR, generation=generation
        )
    if collision:
        send(deps, "opening", content=picture((220, 20, 20)))
    image = send(deps, "opening", scope=slug, content=picture((20, 20, 220)))
    draft = load(deps, slug)
    document = {**draft.document, "opening_logo": {"src": "asset:opening", "alt": "Imagem azul"}}
    draft = drafts.replace_document(deps, slug, document, actor=ACTOR, expected=draft.token)
    if published:
        drafts.publish(deps, slug, actor=ACTOR, expected=draft.token, note="Abertura azul")
    return slug, image.asset


@pytest.mark.parametrize("generation", ["none", "pdf"])
@pytest.mark.parametrize("collision", [False, True], ids=["local-only", "global-homonym"])
def test_copy_keeps_its_own_opening_asset_and_can_be_saved_unchanged(deps, collision, generation):
    source, original = source_with_logo(deps, collision=collision, generation=generation)
    source_document = load(deps, source).document
    slug = drafts.create(
        deps, title="Cópia sintética", slug="copy-logo", source=source, actor=ACTOR
    )

    assert load(deps, source).document == source_document
    draft = load(deps, slug)
    preview = public_page(deps, slug, 1)
    assert preview.spec.generation == generation
    assert preview.screen.opening_logo.url == original.ref.url
    assert preview.screen.opening_logo.alt == "Imagem azul"
    assert draft.document["opening_logo"] == {"src": "asset:opening", "alt": "Imagem azul"}
    saved = drafts.replace_document(deps, slug, draft.document, actor=ACTOR, expected=draft.token)
    assert saved.document == draft.document
    with deps.sessions() as session:
        copied = assets.visible(session, slug)["opening"]
    assert copied.form_slug == slug and copied.id != original.id
    assert copied.ref == original.ref
    client = TestClient(create_app())
    content = client.get(original.ref.url).content
    assets.remove(deps, original.id, actor=ACTOR, expected=original.ref.sha256)
    assert client.get(copied.ref.url).content == content
    assert public_page(deps, slug, 1).screen.opening_logo.url == original.ref.url


@pytest.mark.parametrize("generation", ["none", "pdf"])
@pytest.mark.parametrize("collision", [False, True], ids=["local-only", "global-homonym"])
def test_copy_uses_published_logo_bytes_and_alt_after_the_source_draft_changes(
    deps, generation, collision
):
    source, original = source_with_logo(
        deps, collision=collision, published=True, generation=generation
    )
    client = TestClient(create_app())
    original_bytes = client.get(original.ref.url).content
    replacement = assets.replace(
        deps,
        original.id,
        content=picture((20, 220, 20), size=(120, 240)),
        actor=ACTOR,
        expected=original.ref.sha256,
    ).asset
    source_draft = load(deps, source)
    changed = {
        **source_draft.document,
        "opening_logo": {"src": "asset:opening", "alt": "Imagem verde no rascunho"},
    }
    drafts.replace_document(deps, source, changed, actor=ACTOR, expected=source_draft.token)

    slug = drafts.create(
        deps, title="Cópia histórica", slug="copy-history", source=source, actor=ACTOR
    )

    preview = public_page(deps, slug, 1)
    assert preview.screen.opening_logo.url == original.ref.url
    assert preview.screen.opening_logo.alt == "Imagem azul"
    assert preview.spec.generation == generation
    with deps.sessions() as session:
        copied = assets.visible(session, slug)["opening"]
    assert copied.ref == original.ref and copied.ref != replacement.ref
    assert (copied.width, copied.height, copied.byte_size) == (
        original.width,
        original.height,
        len(original_bytes),
    )
    assert client.get(copied.ref.url).content == original_bytes
    draft = load(deps, slug)
    assert (
        drafts.replace_document(
            deps, slug, draft.document, actor=ACTOR, expected=draft.token
        ).document
        == draft.document
    )
    with pytest.raises(assets.AssetInUse):
        assets.remove(deps, original.id, actor=ACTOR, expected=replacement.ref.sha256)
    assert public_page(deps, source, 1).screen.opening_logo.url == replacement.ref.url


@pytest.mark.parametrize("surface", ["http", "mcp"])
@pytest.mark.parametrize("published", [False, True], ids=["draft-source", "published-source"])
@pytest.mark.parametrize("collision", [False, True], ids=["local-only", "global-homonym"])
def test_http_and_mcp_copy_preserve_the_bound_logo_and_accept_an_unchanged_put(
    deps, api_client, tmp_path, surface, published, collision
):
    from admin_api_support import url_of
    from admin_mcp_live import Live

    source, original = source_with_logo(deps, collision=collision, published=published)
    client = api_client("total")
    slug = "copy-interface"
    arguments = {"source": source, "slug": slug, "title": "Cópia por interface"}
    if surface == "mcp":
        live = Live(client, tmp_path)
        try:
            assert live.ok("create_form", arguments)["slug"] == slug
            draft = live.ok("get_form_draft", {"slug": slug})
            saved = live.ok(
                "save_form_draft",
                {"slug": slug, "expected_version": draft["version"], "document": draft["document"]},
            )
        finally:
            live.close()
    else:
        response = client.post(url_of("create_form"), json=arguments)
        assert response.status_code == 201, response.text
        draft = client.get(url_of("get_form_draft", slug=slug)).json()
        response = client.put(
            url_of("save_form_draft", slug=slug),
            json={"expected_version": draft["version"], "document": draft["document"]},
        )
        assert response.status_code == 200, response.text
        saved = response.json()
    assert saved["document"]["opening_logo"] == {"src": "asset:opening", "alt": "Imagem azul"}
    preview = client.get(url_of("render_form_draft_public_page", slug=slug))
    assert preview.status_code == 200, preview.text
    assert f'src="{original.ref.url}"' in preview.text
    assert 'alt="Imagem azul"' in preview.text
    expected_bytes = client.get(original.ref.url).content
    assert expected_bytes
    replacement = assets.replace(
        deps, original.id, content=picture((20, 220, 20)), actor=ACTOR, expected=original.ref.sha256
    ).asset
    assert (
        f'src="{original.ref.url}"'
        in client.get(url_of("render_form_draft_public_page", slug=slug)).text
    )
    assert client.get(original.ref.url).content == expected_bytes
    if not published:
        assets.remove(deps, original.id, actor=ACTOR, expected=replacement.ref.sha256)
        assert client.get(original.ref.url).content == expected_bytes


def test_published_copy_keeps_its_pin_when_its_image_changes_and_source_is_removed(deps):
    from app.forms.stored import StoredRegistry

    source, original = source_with_logo(deps)
    slug = drafts.create(
        deps, title="Cópia para publicar", slug="copy-pinned", source=source, actor=ACTOR
    )
    draft = load(deps, slug)
    drafts.publish(deps, slug, actor=ACTOR, expected=draft.token, note="Logo copiado")
    with deps.sessions() as session:
        copied = assets.visible(session, slug)["opening"]
    client = TestClient(create_app())
    content = client.get(original.ref.url).content
    replacement = assets.replace(
        deps, copied.id, content=picture((220, 20, 20)), actor=ACTOR, expected=copied.ref.sha256
    ).asset
    assets.remove(deps, original.id, actor=ACTOR, expected=original.ref.sha256)

    historical = StoredRegistry(deps.sessions).get(slug, 1)
    assert historical.screen.opening_logo.url == original.ref.url
    assert historical.screen.opening_logo.alt == "Imagem azul"
    assert client.get(original.ref.url).content == content
    assert public_page(deps, slug, 1).screen.opening_logo.url == replacement.ref.url
    with pytest.raises(assets.AssetInUse):
        assets.remove(deps, copied.id, actor=ACTOR, expected=replacement.ref.sha256)


def test_logo_missing_between_review_and_pin_names_the_opening_field_without_pdf(
    deps, api_client, monkeypatch
):
    from admin_api_support import url_of

    source, image = source_with_logo(deps)
    draft = load(deps, source)
    original_check = drafts.publication_problems

    def removed_after_check(*args, **kwargs):
        problems = original_check(*args, **kwargs)
        assets.remove(deps, image.id, actor=ACTOR, expected=image.ref.sha256)
        return problems

    monkeypatch.setattr(drafts, "publication_problems", removed_after_check)
    client = api_client("total")
    response = client.post(
        url_of("publish_form_draft", slug=source),
        json={"expected_version": draft.token, "note": "Abertura sintética", "confirm": True},
    )
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["field"] == "opening_logo.src"
    assert response.json()["reasons"] == [drafts.OPENING_LOGO_MISSING]
    unchanged = load(deps, source)
    assert unchanged.current is None and unchanged.document == draft.document


def test_unavailable_historical_logo_refuses_copy_without_leaving_a_form(deps, api_client):
    from admin_api_support import url_of

    from app.adapters.assets import AssetStore

    source, original = source_with_logo(deps, published=True)
    assets.replace(
        deps, original.id, content=picture((20, 220, 20)), actor=ACTOR, expected=original.ref.sha256
    )
    # Simula arquivo histórico perdido no storage de teste, incluindo a perda do cache.
    AssetStore(deps.storage).discard(original.ref)
    client = api_client("total")
    response = client.post(
        url_of("create_form"),
        json={"title": "Cópia indisponível", "slug": "copy-unavailable", "source": source},
    )
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["field"] == "source"
    assert load(deps, "copy-unavailable") is None
    assert load(deps, source).current is not None


def test_import_logo_missing_between_review_and_pin_names_the_opening_field(deps, monkeypatch):
    import json

    from test_capture import capture_document

    from app.forms import stored
    from app.forms.definition import DefinitionError

    image = send(deps, "opening").asset
    document = capture_document()
    document["opening_logo"] = {"src": "asset:opening", "alt": "Abertura sintética"}
    original_check = stored.publication_problems

    def removed_after_check(*args, **kwargs):
        problems = original_check(*args, **kwargs)
        assets.remove(deps, image.id, actor=ACTOR, expected=image.ref.sha256)
        return problems

    monkeypatch.setattr(stored, "publication_problems", removed_after_check)
    with pytest.raises(DefinitionError) as refused:
        stored.import_definition(deps, json.dumps(document), actor=ACTOR)
    assert refused.value.problems[0].path == "opening_logo.src"
    assert load(deps, document["slug"]) is None
