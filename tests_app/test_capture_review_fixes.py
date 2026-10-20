"""Régressões de captura nas interfaces HTTP e MCP, com dados sintéticos."""

import pytest
from admin_api_support import api_client
from test_admin import login


@pytest.mark.parametrize("published", [False, True])
def test_capture_detail_shows_no_generation_before_and_after_publication(deps, published):
    from app.forms import drafts

    slug = drafts.create(
        deps, title="Pesquisa", slug="capture-detail", actor="operadora", generation="none"
    )
    api = api_client(deps)
    if published:
        draft = api.get(f"/admin/api/v1/forms/{slug}/draft").json()
        response = api.post(
            f"/admin/api/v1/forms/{slug}/draft/publish",
            json={"confirm": True, "expected_version": draft["version"], "note": "Captura"},
        )
        assert response.status_code == 200, response.text
    assert login(api).status_code == 303
    page = api.get(f"/admin/forms/{slug}")
    assert page.status_code == 200
    assert "Somente captura" in page.text
    assert "Respostas de exemplo" in page.text
    assert 'id="modelo-do-plano"' not in page.text
    assert 'id="prompt"' not in page.text
    assert 'id="templates"' not in page.text
    assert "Plano de exemplo" not in page.text


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("surface", ["http", "mcp"])
def test_copy_capture_as_pdf_prepares_generation_and_keeps_questions(
    deps, tmp_path, surface, published
):
    from admin_mcp_live import Live

    from app.forms import drafts

    drafts.create(
        deps, title="Pesquisa", slug="source-capture", actor="operadora", generation="none"
    )
    api = api_client(deps)
    draft = api.get("/admin/api/v1/forms/source-capture/draft").json()
    source = draft["document"]
    source["steps"][1]["questions"].append(
        {
            "name": "preferencia_extra",
            "kind": "textarea",
            "label": "Qual é sua preferência?",
            "max_length": 1000,
        }
    )
    source["examples"]["answers"]["preferencia_extra"] = "Organizar minhas atividades de exemplo."
    saved = api.put(
        "/admin/api/v1/forms/source-capture/draft",
        json={"document": source, "expected_version": draft["version"]},
    )
    assert saved.status_code == 200, saved.text
    if published:
        response = api.post(
            "/admin/api/v1/forms/source-capture/draft/publish",
            json={"confirm": True, "expected_version": saved.json()["version"], "note": "Captura"},
        )
        assert response.status_code == 200, response.text
    payload = {
        "title": "Pesquisa com plano",
        "slug": "copy-pdf",
        "source": "source-capture",
        "generation": "pdf",
    }
    if surface == "http":
        response = api.post("/admin/api/v1/forms", json=payload)
        assert response.status_code == 201, response.text
    else:
        live = Live(api, tmp_path)
        try:
            live.ok("create_form", payload)
        finally:
            live.close()
    copied = api.get("/admin/api/v1/forms/copy-pdf/draft").json()["document"]
    assert copied["generation"] == "pdf"
    assert copied["steps"] == source["steps"]
    assert copied["examples"]["answers"] == source["examples"]["answers"]
    assert copied["identity"] == source["identity"]
    assert copied["plan"]["fields"]
    assert copied["pdf"]["html"]
    prompt = api.post("/admin/api/v1/forms/copy-pdf/draft/prompt", json={})
    assert prompt.status_code == 200, prompt.text
    assert "Organizar minhas atividades de exemplo." in prompt.json()["prompt"]
    original = api.get("/admin/api/v1/forms/source-capture/draft").json()["document"]
    assert original == source
    assert "plan" not in original


def test_capture_html_conflict_preserves_answers_without_offering_a_plan(deps):
    import json

    from fastapi.testclient import TestClient
    from test_admin_drafts import hidden
    from test_capture import capture_document

    from app.api.main import create_app
    from app.forms.stored import import_definition

    document = capture_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    data = {
        **document["examples"]["answers"],
        "_idem": "same-capture",
        "_privacy": "1",
        "_consent_version": "capture-test-v1",
    }
    with TestClient(create_app(), follow_redirects=False) as client:
        assert client.post("/p/capture-test", data=data).status_code == 303
        changed = {**data, "nome": "Outra participante"}
        result = client.post("/p/capture-test", data=changed)
        assert result.status_code == 409
        assert "Se quiser um plano" not in result.text
        assert "Outra participante" in result.text
        assert hidden(result.text, "_idem") != data["_idem"]
        retry = {**changed, "_idem": hidden(result.text, "_idem")}
        assert client.post("/p/capture-test", data=retry).status_code == 303


@pytest.mark.parametrize("surface", ["http", "mcp", "html"])
def test_prompt_mode_guard_uses_the_same_snapshot_as_assembly(deps, tmp_path, surface):
    import copy

    import sqlalchemy as sa
    from admin_mcp_live import Live

    from app.forms import drafts

    drafts.create(deps, title="Pesquisa", slug="mode-switch", actor="operadora")
    api = api_client(deps)
    before = api.get("/admin/api/v1/forms/mode-switch/draft").json()
    document = copy.deepcopy(before["document"])
    for key in ("instructions", "prompt", "plan", "pdf", "preview", "phases"):
        document.pop(key, None)
    document["generation"] = "none"
    document["completion"] = {"mode": "confirmation"}
    document["examples"].pop("plan", None)
    if surface == "html":
        from test_admin_drafts import hidden

        assert login(api).status_code == 303
        page = api.get("/admin/forms/mode-switch/draft/tests")
        csrf = hidden(page.text, "csrf_token")
    switched = False
    live = Live(api, tmp_path) if surface == "mcp" else None

    def switch_after_pdf_snapshot(conn, cursor, statement, parameters, context, executemany):
        nonlocal switched
        if not switched and statement.lstrip().startswith("SELECT form_drafts."):
            # A consulta já fixou PDF; outra requisição confirma captura antes da próxima leitura.
            switched = True
            saved = api.put(
                "/admin/api/v1/forms/mode-switch/draft",
                json={"document": document, "expected_version": before["version"]},
            )
            assert saved.status_code == 200, saved.text

    engine = deps.sessions.kw["bind"]
    sa.event.listen(engine, "after_cursor_execute", switch_after_pdf_snapshot)
    try:
        if live:
            output = live.call("assemble_form_draft_prompt", {"slug": "mode-switch"})
            assert not output.is_error, output.text
        elif surface == "html":
            response = api.post(
                "/admin/forms/mode-switch/draft/prompt",
                data={"csrf_token": csrf},
                headers={"Origin": "http://testserver"},
            )
            assert response.status_code == 200, response.text
            assert "Diagnóstico do projeto" in response.text
        else:
            response = api.post("/admin/api/v1/forms/mode-switch/draft/prompt", json={})
            assert response.status_code == 200, response.text
            assert response.json()["prompt"]
    finally:
        sa.event.remove(engine, "after_cursor_execute", switch_after_pdf_snapshot)
        if live:
            live.close()
    assert switched
    # A próxima operação já usa captura e recusa geração de modo controlado.
    response = api.post("/admin/api/v1/forms/mode-switch/draft/prompt", json={})
    assert response.status_code == 409
    assert response.json()["code"] == "state_conflict"


def test_capture_local_test_rejects_pdf_equally_in_html_http_and_mcp(deps, tmp_path):
    from admin_mcp_live import Live
    from test_admin_drafts import hidden

    from app.forms import drafts

    drafts.create(deps, title="Pesquisa", slug="pdf-source", actor="operadora")
    api = api_client(deps)
    response = api.post("/admin/api/v1/forms/pdf-source/draft/capture-test", json={})
    assert response.status_code == 409
    assert response.json()["code"] == "state_conflict"
    live = Live(api, tmp_path)
    try:
        output = live.call("test_capture_form_draft", {"slug": "pdf-source"})
        assert output.is_error and "409 state_conflict" in output.text
    finally:
        live.close()
    assert login(api).status_code == 303
    page = api.get("/admin/forms/pdf-source/draft/tests")
    response = api.post(
        "/admin/forms/pdf-source/draft/capture-test",
        data={"csrf_token": hidden(page.text, "csrf_token")},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 409


def test_stale_capture_test_page_rejects_conversion_to_pdf(deps):
    from test_admin_drafts import hidden

    api = api_client(deps)
    created = api.post(
        "/admin/api/v1/forms",
        json={"title": "Pesquisa", "slug": "stale-capture", "generation": "none"},
    )
    assert created.status_code == 201
    assert login(api).status_code == 303
    page = api.get("/admin/forms/stale-capture/draft/tests")
    assert page.status_code == 200
    assert "Validar captura" in page.text
    source = api.post("/admin/api/v1/forms", json={"title": "PDF", "slug": "source-pdf"})
    assert source.status_code == 201
    document = api.get("/admin/api/v1/forms/source-pdf/draft").json()["document"]
    old = api.get("/admin/api/v1/forms/stale-capture/draft").json()
    document["slug"] = "stale-capture"
    saved = api.put(
        "/admin/api/v1/forms/stale-capture/draft",
        json={"document": document, "expected_version": old["version"]},
    )
    assert saved.status_code == 200, saved.text
    response = api.post(
        "/admin/forms/stale-capture/draft/capture-test",
        data={"csrf_token": hidden(page.text, "csrf_token")},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 409
    assert "finalidade atual" in response.text
    assert "Este formulário apenas captura respostas" not in response.text
