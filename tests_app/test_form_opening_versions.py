"""Logo de abertura nas fronteiras de rascunho, publicação e leitura pública histórica."""

import json

import pytest
from fastapi.testclient import TestClient
from test_capture import capture_document
from test_form_assets import picture, send

from app.adapters.assets import AssetStore
from app.api.main import create_app
from app.forms import assets, drafts, stored
from app.forms.definition import DefinitionError

ACTOR = "operadora"


def load(deps, slug="capture-test"):
    with deps.sessions() as session:
        return drafts.load(session, slug)


@pytest.mark.parametrize("size", [(1800, 200), (120, 1800)])
def test_capture_opening_pins_history_and_preview_uses_current_image(deps, size, api_client):
    logo = send(deps, "opening", content=picture(size=size))
    document = capture_document()
    document["intro"] = (
        "Primeiro bloco da pesquisa.\n\nSegundo <script>bloco</script>.\n\nTerceiro & último bloco."
    )
    document["opening_logo"] = {"src": " asset:opening ", "alt": "Pesquisa original"}
    stored.import_definition(deps, json.dumps(document), actor=ACTOR)
    first = deps.forms.current("capture-test")
    assert first.screen.opening_logo.url == logo.asset.ref.url
    client = TestClient(create_app(), follow_redirects=False)
    assert f'src="{logo.asset.ref.url}"' in client.get("/p/capture-test").text
    original_bytes = client.get(logo.asset.ref.url).content
    assert original_bytes == AssetStore(deps.storage).read(logo.asset.ref)

    updated = assets.replace(
        deps,
        logo.asset.id,
        content=picture((20, 80, 210)),
        actor=ACTOR,
        expected=logo.asset.ref.sha256,
    )
    draft = load(deps)
    changed = {**draft.document, "opening_logo": {"src": "asset:opening", "alt": "Pesquisa nova"}}
    draft = drafts.replace_document(
        deps, "capture-test", changed, actor=ACTOR, expected=draft.token
    )
    assert draft.pending
    with deps.sessions() as session:
        refs = assets.live_refs(session, draft.document)
    from app.forms.definition import compile_definition

    preview = compile_definition(draft.document).with_assets(refs)
    assert preview.screen.opening_logo.url == updated.asset.ref.url
    assert preview.screen.opening_logo.alt == "Pesquisa nova"
    assert (
        stored.StoredRegistry(deps.sessions).get("capture-test", 1).screen.opening_logo.url
        == logo.asset.ref.url
    )
    assert client.get(logo.asset.ref.url).content == original_bytes
    drafts.publish(deps, "capture-test", actor=ACTOR, note="Logo atualizado", expected=draft.token)
    assert deps.forms.current("capture-test").screen.opening_logo.url == updated.asset.ref.url
    assert deps.forms.current("capture-test").screen.opening_logo.alt == "Pesquisa nova"
    from admin_api_support import url_of

    api = api_client("leitura")
    historical = api.get(url_of("get_form_version", slug="capture-test", number="1")).json()
    assert historical["definition"]["opening_logo"]["alt"] == "Pesquisa original"
    assert (
        stored.StoredRegistry(deps.sessions).get("capture-test", 1).screen.opening_logo.alt
        == "Pesquisa original"
    )
    with pytest.raises(assets.AssetInUse):
        assets.remove(deps, updated.asset.id, actor=ACTOR, expected=updated.asset.ref.sha256)


def test_missing_opening_logo_is_refused_on_import_and_draft_save(deps):
    document = capture_document()
    document["opening_logo"] = {"src": "asset:missing", "alt": "Pesquisa"}
    with pytest.raises(DefinitionError) as refused:
        stored.import_definition(deps, json.dumps(document), actor=ACTOR)
    assert refused.value.problems[0].path == "opening_logo.src"
    document.pop("opening_logo")
    stored.import_definition(deps, json.dumps(document), actor=ACTOR)
    draft = load(deps)
    document["opening_logo"] = {"src": "asset:missing", "alt": "Pesquisa"}
    with pytest.raises(DefinitionError):
        drafts.replace_document(deps, "capture-test", document, actor=ACTOR, expected=draft.token)
    assert "opening_logo" not in load(deps).document


@pytest.mark.parametrize("generation", ["pdf", "none"])
def test_admin_editor_and_preview_keep_logo_until_explicit_publication(deps, generation):
    from conftest import legacy_definition
    from test_admin import login
    from test_admin_destinations import ADMIN_ENV
    from test_admin_drafts import editor, post
    from test_public import tags

    from app.settings import Settings

    document = capture_document() if generation == "none" else legacy_definition("demo")
    document["slug"] = "opening-" + generation
    document["intro"] = (
        "Primeiro bloco da pesquisa.\r\n\r\nSegundo <script>bloco</script>.\r\n\r\nTerceiro & último bloco."
    )
    slug = document["slug"]
    stored.import_definition(deps, json.dumps(document), actor=ACTOR)
    logo = send(
        deps,
        "opening",
        scope=slug,
        content=picture(size=(1800, 200) if generation == "pdf" else (120, 1800)),
    )
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)
    client = TestClient(create_app(), follow_redirects=False)
    assert login(client).status_code == 303
    path = f"/admin/forms/{slug}/draft"
    page = client.get(path)
    assert tags(page.text, "select", name="opening_logo.src")
    assert ">Sem logo</option>" in page.text
    assert ">opening · " in page.text
    fields = editor(client, path)
    fields["opening_logo.src"] = "asset:opening"
    fields["opening_logo.alt"] = "Pesquisa sintética"
    assert post(client, path, **fields).status_code == 303
    assert editor(client, path)["opening_logo.alt"] == "Pesquisa sintética"
    operation = post(client, path + "/edit", **{**editor(client, path), "op": "add-step"})
    assert operation.status_code == 200
    assert ">opening · " in operation.text
    html = client.get(path + "/preview/frame").text
    assert f'src="{logo.asset.ref.url}"' in html
    assert 'alt="Pesquisa sintética"' in html
    assert 'class="hero__logo"' not in client.get(f"/p/{slug}").text
    fields = editor(client, path)
    fields["opening_logo.alt"] = ""
    refused = post(client, path, **fields)
    assert refused.status_code == 422
    assert any(
        tag.get("aria-invalid") == "true"
        for tag in tags(refused.text, "input", name="opening_logo.alt")
    )
    assert editor(client, path)["opening_logo.alt"] == "Pesquisa sintética"
    fields = editor(client, path)
    fields["opening_logo.src"] = ""
    assert fields["opening_logo.alt"] == "Pesquisa sintética"
    assert post(client, path, **fields).status_code == 303
    assert "opening_logo" not in load(deps, slug).document


def test_http_and_mcp_roundtrip_logo_with_conflict_scope_and_removal(deps, api_client, tmp_path):
    from admin_api_support import url_of
    from admin_mcp_live import Live

    document = capture_document()
    slug = document["slug"]
    stored.import_definition(deps, json.dumps(document), actor=ACTOR)
    send(deps, "opening", scope=slug)
    send(deps, "other-form", scope="demo")
    client = api_client("total")
    url = url_of("get_form_draft", slug=slug)
    original = client.get(url).json()
    logo = {"src": "asset:opening", "alt": "Pesquisa sintética"}
    document["opening_logo"] = logo
    changed = client.put(url, json={"expected_version": original["version"], "document": document})
    assert changed.status_code == 200, changed.text
    assert changed.json()["document"]["opening_logo"] == logo
    assert (
        client.put(
            url, json={"expected_version": original["version"], "document": document}
        ).status_code
        == 409
    )
    reader = api_client("leitura")
    assert (
        reader.put(
            url, json={"expected_version": changed.json()["version"], "document": document}
        ).status_code
        == 403
    )
    for src in ["asset:other-form", "asset:missing", "https://example.test/logo.png"]:
        refused = client.patch(
            url,
            json={
                "expected_version": changed.json()["version"],
                "patch": [{"op": "replace", "path": "/opening_logo/src", "value": src}],
            },
        )
        assert refused.status_code == 422, refused.text
        assert client.get(url).json()["document"]["opening_logo"] == logo
    live = Live(client, tmp_path)
    try:
        draft = live.ok("get_form_draft", {"slug": slug})
        same = live.ok(
            "save_form_draft",
            {"slug": slug, "expected_version": draft["version"], "document": draft["document"]},
        )
        assert same["document"]["opening_logo"] == logo
        removed = live.ok(
            "patch_form_draft",
            {
                "slug": slug,
                "expected_version": same["version"],
                "patch": [{"op": "remove", "path": "/opening_logo"}],
            },
        )
        assert "opening_logo" not in removed["document"]
    finally:
        live.close()


def test_opening_removed_after_save_blocks_publication_and_rolls_back(deps):
    logo = send(deps, "opening")
    document = capture_document()
    stored.import_definition(deps, json.dumps(document), actor=ACTOR)
    draft = load(deps)
    document["opening_logo"] = {"src": "asset:opening", "alt": "Pesquisa sintética"}
    saved = drafts.replace_document(
        deps, "capture-test", document, actor=ACTOR, expected=draft.token
    )
    assets.remove(deps, logo.asset.id, actor=ACTOR, expected=logo.asset.ref.sha256)
    simulation = drafts.publish(
        deps, "capture-test", actor=ACTOR, note="Logo ausente", expected=saved.token, dry_run=True
    )
    assert simulation.reasons == (drafts.OPENING_LOGO_MISSING,)
    with pytest.raises(drafts.PublishRefused) as refused:
        drafts.publish(deps, "capture-test", actor=ACTOR, note="Logo ausente", expected=saved.token)
    assert refused.value.problems[0].path == "opening_logo.src"
    assert deps.forms.current("capture-test").version_number == 1
    assert deps.forms.current("capture-test").screen.opening_logo is None
    assert load(deps).document["opening_logo"] == document["opening_logo"]
