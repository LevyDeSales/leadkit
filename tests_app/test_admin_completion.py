"""Autoria e prévia segura da conclusão nas fronteiras administrativas."""

import json
from pathlib import Path

from admin_api_support import url_of
from admin_mcp_live import Live
from test_admin import login
from test_admin_drafts import editor, post
from test_admin_forms import DEFINITION, SLUG
from test_public import tags


def test_completion_preview_is_read_only_and_cannot_navigate_outside(api_client) -> None:
    client = api_client()
    document = json.loads(DEFINITION)
    document["completion"] = {
        "mode": "redirect",
        "url": "https://example.test/next",
        "message": "Obrigada <script>sentinela</script>",
        "button_label": "Próximo passo",
    }
    imported = client.post(url_of("import_form"), json={"definition": document, "confirm": True})
    assert imported.status_code == 201, imported.text
    reader = api_client("leitura")
    response = reader.get(f"/admin/api/v1/forms/{SLUG}/draft/completion-preview")
    assert response.status_code == 200, response.text
    assert "Próximo passo" in response.text
    assert "Obrigada &lt;script&gt;sentinela&lt;/script&gt;" in response.text
    assert not tags(response.text, "a", href="https://example.test/next")
    assert not tags(response.text, "script")
    assert not tags(response.text, "form")
    (button,) = tags(response.text, "button")
    assert "disabled" in button
    assert login(client).status_code == 303
    page = client.get(f"/admin/forms/{SLUG}/draft/completion-preview")
    assert page.status_code == 200
    assert "Próximo passo" in page.text


def test_editor_and_api_publish_the_same_completion_without_a_paid_test(api_client) -> None:
    client = api_client()
    assert (
        client.post(
            url_of("import_form"), json={"definition": json.loads(DEFINITION), "confirm": True}
        ).status_code
        == 201
    )
    assert login(client).status_code == 303
    draft_path = f"/admin/forms/{SLUG}/draft"
    fields = editor(client, draft_path)
    fields.update(
        {
            "completion.mode": "button",
            "completion.message": "Tudo salvo.",
            "completion.url": "https://example.test/continue",
            "completion.button_label": "Ver próximos passos",
        }
    )
    saved = post(client, draft_path, **fields)
    assert saved.status_code == 303, saved.text
    draft = client.get(url_of("get_form_draft", slug=SLUG)).json()
    assert draft["document"]["completion"] == {
        "mode": "button",
        "message": "Tudo salvo.",
        "url": "https://example.test/continue",
        "button_label": "Ver próximos passos",
    }
    review = client.get(url_of("review_form_draft_publication", slug=SLUG)).json()
    assert review["test"]["status"] == "not_needed"
    assert review["test"]["llm_changes"] == []
    published = client.post(
        url_of("publish_form_draft", slug=SLUG),
        json={"expected_version": draft["version"], "note": "Conclusão com botão", "confirm": True},
    )
    assert published.status_code == 200, published.text
    version = client.get(url_of("get_form_version", slug=SLUG, number="2")).json()
    assert version["definition"]["completion"] == draft["document"]["completion"]


def test_completion_configuration_has_safe_field_errors_and_role_version_checks(api_client) -> None:
    client = api_client()
    assert (
        client.post(
            url_of("import_form"), json={"definition": json.loads(DEFINITION), "confirm": True}
        ).status_code
        == 201
    )
    path = url_of("get_form_draft", slug=SLUG)
    draft = client.get(path).json()
    document = {
        **draft["document"],
        "completion": {"mode": "redirect", "url": "javascript:unsafe_sentinel"},
    }
    refused = client.put(path, json={"document": document, "expected_version": draft["version"]})
    assert refused.status_code == 422
    assert any(item["field"] == "completion.url" for item in refused.json()["errors"])
    assert "unsafe_sentinel" not in refused.text
    document["completion"] = {"mode": "confirmation"}
    reader = api_client("leitura")
    assert (
        reader.put(
            path, json={"document": document, "expected_version": draft["version"]}
        ).status_code
        == 403
    )
    assert (
        client.put(path, json={"document": document, "expected_version": "stale"}).status_code
        == 409
    )


def test_mcp_edits_and_previews_completion_through_the_live_api(api_client, tmp_path) -> None:
    client = api_client()
    assert (
        client.post(
            url_of("import_form"), json={"definition": json.loads(DEFINITION), "confirm": True}
        ).status_code
        == 201
    )
    live = Live(client, tmp_path, pace=True)
    try:
        draft = live.ok("get_form_draft", {"slug": SLUG})
        completion = {
            "mode": "redirect",
            "url": "https://example.test/mcp",
            "message": "Recebido pelo MCP.",
            "button_label": "Continuar",
        }
        edited = live.ok(
            "patch_form_draft",
            {
                "slug": SLUG,
                "expected_version": draft["version"],
                "patch": [{"op": "add", "path": "/completion", "value": completion}],
            },
        )
        assert edited["document"]["completion"] == completion
        output = live.call("render_form_draft_completion_preview", {"slug": SLUG})
        assert not output.is_error
        saved = Path(output.text.split("caminho: ", 1)[1].splitlines()[0])
        html = saved.read_text()
        assert "Recebido pelo MCP." in html
        assert not tags(html, "a", href="https://example.test/mcp")
        assert not tags(html, "script")
    finally:
        live.close()
