"""Captura sem geração nas costuras públicas de autoria e envio."""

from app.forms.definition import compile_definition
from app.forms.starter import definition


def capture_document():
    document = definition(slug="capture-test", title="Pesquisa de exemplo")
    for key in ("instructions", "prompt", "plan", "pdf", "preview"):
        document.pop(key)
    document["examples"].pop("plan")
    document["generation"] = "none"
    document["completion"] = {"mode": "confirmation"}
    return document


def test_capture_compiles_without_generation_content():
    spec = compile_definition(capture_document())
    assert spec.generation == "none"
    assert spec.plan_model is None
    assert spec.templates is None
    assert spec.example_plan is None
    assert spec.instructions is None
    assert spec.completion.mode == "confirmation"


def test_capture_creation_and_publication_need_no_generation(deps, monkeypatch):
    from app.forms import drafts

    def forbidden(*args, **kwargs):
        raise AssertionError("geração inaplicável")

    monkeypatch.setattr(deps.pdf, "render", forbidden)
    slug = drafts.create(
        deps, title="Pesquisa", slug="capture-test", actor="operadora", generation="none"
    )
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
    assert "prompt" not in draft.document
    simulated = drafts.publish(
        deps, slug, actor="operadora", note="Capturar respostas", expected=draft.token, dry_run=True
    )
    assert simulated.ready
    published = drafts.publish(
        deps, slug, actor="operadora", note="Capturar respostas", expected=draft.token
    )
    assert published.number == 1
    assert deps.forms.current(slug).generation == "none"


def test_capture_http_accepts_and_replays_without_pdf(deps):
    import json

    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.forms.stored import import_definition

    document = capture_document()
    import_definition(deps, json.dumps(document), actor="operadora")

    async def forbidden(*args):
        raise AssertionError("captura não dispara geração")

    payload = {
        "answers": document["examples"]["answers"],
        "consent": {"privacy": True, "text_version": "capture-test-v1"},
    }
    with TestClient(create_app(trigger=forbidden), follow_redirects=False) as client:
        first = client.post(
            "/v1/forms/capture-test/submissions",
            json=payload,
            headers={"Idempotency-Key": "capture"},
        )
        assert first.status_code == 202
        body = first.json()
        assert body["kind"] == "capture"
        assert body["status"] == "captured"
        assert set(body) == {
            "kind",
            "status",
            "submission_id",
            "receipt_token",
            "receipt_url",
            "replay",
            "completion",
        }
        replay = client.post(
            "/v1/forms/capture-test/submissions",
            json=payload,
            headers={"Idempotency-Key": "capture"},
        )
        assert replay.json() == {**body, "replay": True}
        receipt = client.get(body["receipt_url"])
        assert receipt.status_code == 303
        page = client.get(receipt.headers["location"])
        assert page.status_code == 200
        assert "Obrigada" in page.text
        assert payload["answers"]["email"] not in page.text


def test_capture_editor_roundtrip_does_not_inject_pdf_content():

    from dataclasses import replace

    from app.admin.form_editor import FormEdit
    from app.forms.drafts import document_of, texts_of

    document = capture_document()
    edit = FormEdit.of_document(document)
    texts = replace(
        texts_of(document), screen=edit.screen(), example_answers=edit.example_answers()
    )
    saved = document_of(texts, document)
    assert compile_definition(saved).generation == "none"
    assert not set(saved).intersection({"plan", "prompt", "instructions", "pdf", "preview"})


def test_capture_is_consultable_without_disclosing_lead_data(deps):
    import json

    from admin_api_support import api_client, url_of
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.forms.stored import import_definition

    document = capture_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    payload = {
        "answers": document["examples"]["answers"],
        "consent": {"privacy": True, "text_version": "capture-test-v1"},
    }
    with TestClient(create_app()) as client:
        sent = client.post("/v1/forms/capture-test/submissions", json=payload).json()
    api = api_client(deps)
    listing = api.get(url_of("list_captures"))
    assert listing.status_code == 200
    item = listing.json()["items"][0]
    assert item["status"] == "captured"
    assert item["lead_data"] is None
    detail = api.get(
        url_of("get_capture", submission_id=sent["submission_id"]),
        params={"include_lead_data": True},
    )
    assert detail.json()["lead_data"]["identity"]["email"] == "marina@example.test"


def test_capture_retention_counts_and_deletes_without_pdf(deps):
    import json
    from datetime import timedelta

    from admin_api_support import api_client
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.forms.stored import import_definition
    from app.models import utcnow
    from app.retention import due_for_form, purge_expired

    document = capture_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    with TestClient(create_app()) as client:
        sent = client.post(
            "/v1/forms/capture-test/submissions",
            json={
                "answers": document["examples"]["answers"],
                "consent": {"privacy": True, "text_version": "capture-test-v1"},
            },
        ).json()
    future = utcnow() + timedelta(days=deps.settings.retention_days + 1)
    with deps.sessions() as session:
        due = due_for_form(
            session,
            "capture-test",
            pdf_days=30,
            request_days=deps.settings.retention_days,
            now=future,
        )
    assert due.captures == 1
    assert due.requests == due.pdfs == 0
    report = purge_expired(deps, now=future)
    assert report.captures_deleted == 1
    assert report.leads_deleted == 1
    api = api_client(deps)
    assert api.get(f"/admin/api/v1/captures/{sent['submission_id']}").status_code == 404


def test_capture_local_test_and_inapplicable_generation_operations(deps):
    from admin_api_support import api_client

    from app.forms import drafts

    slug = drafts.create(
        deps, title="Pesquisa", slug="capture-test", actor="operadora", generation="none"
    )
    api = api_client(deps)
    local = api.post(
        f"/admin/api/v1/forms/{slug}/draft/capture-test", json={"simulated_date": "2026-10-20"}
    )
    assert local.status_code == 200
    assert local.json()["completion"]["mode"] == "confirmation"
    prompt = api.post(f"/admin/api/v1/forms/{slug}/draft/prompt", json={})
    assert prompt.status_code == 409
    assert prompt.json()["code"] == "state_conflict"
    preview = api.post(f"/admin/api/v1/forms/{slug}/draft/pdf-preview", json={})
    assert preview.status_code == 409


def test_capture_rejects_pdf_result_and_preserves_legacy_fingerprint():
    import pytest

    from app.forms.definition import DefinitionError
    from app.forms.definition.changes import llm_fingerprint
    from app.forms.definition.normal import same_meaning

    document = capture_document()
    document["completion"] = {"mode": "result"}
    with pytest.raises(DefinitionError) as error:
        compile_definition(document)
    assert error.value.problems[0].path == "completion.mode"
    pdf = definition(slug="pdf-test", title="PDF")
    assert compile_definition(pdf).generation == "pdf"
    assert llm_fingerprint(pdf) == llm_fingerprint({**pdf, "generation": "pdf"})
    assert same_meaning(pdf, {**pdf, "generation": "pdf"})


def test_capture_html_has_own_cookie_and_exact_destination(deps):
    import json

    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.forms.stored import import_definition

    document = capture_document()
    document["completion"] = {
        "mode": "button",
        "url": "https://example.test/next?source=survey",
        "message": "Obrigada <b>participante</b>",
    }
    import_definition(deps, json.dumps(document), actor="operadora")
    data = {
        **document["examples"]["answers"],
        "_idem": "html-capture",
        "_privacy": "1",
        "_consent_version": "capture-test-v1",
    }
    with TestClient(create_app(), follow_redirects=False) as client:
        form_page = client.get("/p/capture-test")
        assert "Enviar minhas respostas" in form_page.text
        assert "O plano fica pronto em PDF" not in form_page.text
        sent = client.post("/p/capture-test", data=data)
        assert sent.status_code == 303
        assert sent.headers["location"].startswith("/s/")
        assert "HttpOnly" in sent.headers["set-cookie"]
        page = client.get(sent.headers["location"])
        assert page.status_code == 200
        assert 'href="https://example.test/next?source=survey"' in page.text
        assert "Obrigada &lt;b&gt;participante&lt;/b&gt;" in page.text
        assert document["examples"]["answers"]["email"] not in page.text
        assert page.headers["Cache-Control"] == "no-store"


def test_capture_accepts_full_pdf_queue_but_keeps_submission_limit(deps):
    import json

    from admin_api_support import api_client
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.forms.stored import import_definition

    document = capture_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    deps.settings.submission_limit_queue = 0
    deps.settings.submission_limit_per_email = 1
    payload = {
        "answers": document["examples"]["answers"],
        "consent": {"privacy": True, "text_version": "capture-test-v1"},
    }
    with TestClient(create_app()) as client:
        assert (
            client.post(
                "/v1/forms/capture-test/submissions",
                json=payload,
                headers={"Idempotency-Key": "first"},
            ).status_code
            == 202
        )
        assert (
            client.post(
                "/v1/forms/capture-test/submissions",
                json=payload,
                headers={"Idempotency-Key": "second"},
            ).status_code
            == 429
        )
    assert api_client(deps).get("/admin/api/v1/jobs").json()["items"] == []


def test_capture_read_role_and_mcp_opt_in_protect_participant(deps):
    import json

    from admin_api_support import api_client, built_openapi
    from fastapi.testclient import TestClient

    from app.admin_mcp import catalog
    from app.api.main import create_app
    from app.forms.stored import import_definition

    document = capture_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    with TestClient(create_app()) as client:
        sent = client.post(
            "/v1/forms/capture-test/submissions",
            json={
                "answers": document["examples"]["answers"],
                "consent": {"privacy": True, "text_version": "capture-test-v1"},
            },
        ).json()
    api = api_client(deps, role="leitura")
    assert api.get("/admin/api/v1/captures?include_lead_data=true").status_code == 403
    detail = api.get(f"/admin/api/v1/captures/{sent['submission_id']}")
    assert detail.json()["lead_data"] is None
    assert "marina@example.test" not in detail.text
    tools = catalog.load(built_openapi()).tools
    selected = catalog.select(tools.values(), role="total", lead_data=False)
    capture_tool = next(tool for tool in selected if tool.name == "get_capture")
    assert "include_lead_data" not in capture_tool.input_schema["properties"]


def test_capture_admin_html_is_authenticated_and_local_test_checks_csrf(deps):
    from admin_api_support import api_client
    from test_admin import login

    from app.forms import drafts

    slug = drafts.create(
        deps, title="Pesquisa", slug="capture-test", actor="operadora", generation="none"
    )
    api = api_client(deps)
    assert api.get("/admin/captures").status_code == 303
    assert login(api).status_code == 303
    listing = api.get("/admin/captures")
    assert listing.status_code == 200
    assert api.get(f"/admin/forms/{slug}/draft").status_code == 200
    page = api.get(f"/admin/forms/{slug}/draft/tests")
    assert page.status_code == 200
    assert "Validar captura" in page.text
    assert api.post(f"/admin/forms/{slug}/draft/capture-test", data={}).status_code == 403
    from test_admin_drafts import hidden

    token = hidden(page.text, "csrf_token")
    tested = api.post(
        f"/admin/forms/{slug}/draft/capture-test",
        data={"csrf_token": token},
        headers={"Origin": "http://testserver"},
    )
    assert tested.status_code == 200
    assert "Respostas válidas" in tested.text


def test_capture_replay_keeps_original_mode_after_pdf_publication(deps):
    import json
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.forms import drafts
    from app.forms.stored import import_definition

    document = capture_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    payload = {
        "answers": document["examples"]["answers"],
        "consent": {"privacy": True, "text_version": "capture-test-v1"},
    }
    app = create_app()

    def send():
        with TestClient(app) as client:
            return client.post(
                "/v1/forms/capture-test/submissions",
                json=payload,
                headers={"Idempotency-Key": "race"},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: send(), range(2)))
    assert all(response.status_code == 202 for response in responses)
    assert responses[0].json()["submission_id"] == responses[1].json()["submission_id"]
    assert sorted(response.json()["replay"] for response in responses) == [False, True]
    with deps.sessions() as session:
        draft = drafts.load(session, "capture-test")
    pdf = definition(slug="capture-test", title="Pesquisa PDF")
    saved = drafts.save_document(deps, "capture-test", pdf, actor="operadora", expected=draft.token)
    # Importação publica PDF sintético sem exigir gate pago; aqui simulamos versão válida pelo registro.
    from conftest import publish

    publish(deps, compile_definition(document), compile_definition(pdf, version_number=2))
    again = send()
    assert again.status_code == 202
    assert again.json()["kind"] == "capture"
    assert again.json()["submission_id"] == responses[0].json()["submission_id"]
    assert saved.document.get("generation", "pdf") == "pdf"


def test_capture_retention_preserves_shared_valid_submission_and_requires_accurate_confirmation(
    deps,
):
    import json
    from datetime import timedelta

    import pytest
    from fastapi.testclient import TestClient

    from app.api.main import create_app
    from app.forms import retention as policy
    from app.forms.stored import import_definition
    from app.models import utcnow
    from app.retention import Due, purge_expired

    document = capture_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    payload = {
        "answers": document["examples"]["answers"],
        "consent": {"privacy": True, "text_version": "capture-test-v1"},
    }
    with TestClient(create_app()) as client:
        client.post("/v1/forms/capture-test/submissions", json=payload)
        later = {
            **document,
            "slug": "second-capture",
            "consent": {**document["consent"], "version": "second-capture-v1"},
        }
        import_definition(deps, json.dumps(later), actor="operadora")
        client.post(
            "/v1/forms/second-capture/submissions",
            json={**payload, "consent": {"privacy": True, "text_version": "second-capture-v1"}},
        )
    future = utcnow() + timedelta(days=45)
    with deps.sessions() as session:
        periods = policy.periods_of(session, "capture-test")
    with pytest.raises(policy.ConfirmationNeeded) as refused:
        policy.save(
            deps.sessions,
            deps.settings,
            "capture-test",
            new=policy.Periods(30, 40),
            actor="operadora",
            expected=periods.token,
            confirmed=Due(0, 0),
            now=future,
        )
    assert refused.value.due.captures == 1
    policy.save(
        deps.sessions,
        deps.settings,
        "capture-test",
        new=policy.Periods(30, 40),
        actor="operadora",
        expected=periods.token,
        confirmed=Due(0, 0, 1),
        now=future,
    )
    report = purge_expired(deps, now=future)
    assert report.captures_deleted == 1
    assert report.leads_deleted == 0


def test_capture_pagination_is_available_in_api_and_admin(deps):
    import json

    from admin_api_support import api_client
    from fastapi.testclient import TestClient
    from test_admin import login

    from app.api.main import create_app
    from app.forms.stored import import_definition

    document = capture_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    payload = {
        "answers": document["examples"]["answers"],
        "consent": {"privacy": True, "text_version": "capture-test-v1"},
    }
    with TestClient(create_app()) as client:
        client.post("/v1/forms/capture-test/submissions", json=payload)
        client.post("/v1/forms/capture-test/submissions", json=payload)
    api = api_client(deps)
    first = api.get("/admin/api/v1/captures?limit=1").json()
    second = api.get(
        "/admin/api/v1/captures", params={"limit": 1, "cursor": first["next_cursor"]}
    ).json()
    assert first["items"][0]["submission_id"] != second["items"][0]["submission_id"]
    assert second["next_cursor"] is None
    assert login(api).status_code == 303
    page = api.get("/admin/captures?limit=1")
    assert page.status_code == 200
    assert "Próxima página" in page.text
    assert api.get("/admin/captures?cursor=invalid").status_code == 422


def test_editor_can_enable_pdf_again_after_capture_without_losing_answers():
    from dataclasses import replace

    from app.admin.form_editor import FormEdit
    from app.forms.drafts import document_of, texts_of

    original = definition(slug="capture-test", title="Pesquisa")
    edit = FormEdit.of_document(original)
    edit.generation = "none"
    edit.completion.mode = "confirmation"
    captured = document_of(
        replace(texts_of(original), screen=edit.screen(), example_answers=edit.example_answers()),
        original,
    )
    assert compile_definition(captured).templates is None
    edit = FormEdit.of_document(captured)
    edit.generation = "pdf"
    restored = document_of(
        replace(texts_of(captured), screen=edit.screen(), example_answers=edit.example_answers()),
        captured,
    )
    spec = compile_definition(restored)
    assert spec.generation == "pdf"
    assert spec.templates is not None
    assert restored["examples"]["answers"] == captured["examples"]["answers"]


def test_admin_editor_can_switch_pdf_to_capture_and_back_explicitly(deps):
    from admin_api_support import api_client
    from test_admin import login
    from test_admin_drafts import save

    from app.forms import drafts

    slug = drafts.create(deps, title="Pesquisa", slug="capture-test", actor="operadora")
    api = api_client(deps)
    assert login(api).status_code == 303
    path = f"/admin/forms/{slug}/draft"

    def capture(fields):
        fields["generation"] = "none"
        fields["completion.mode"] = "confirmation"

    refused = save(api, capture, draft=path)
    assert refused.status_code == 422
    assert "Confirme a retirada da geração" in refused.text
    assert api.get(f"/admin/forms/{slug}/draft/pdf").status_code == 200

    def confirmed_capture(fields):
        capture(fields)
        fields["confirm_generation_removal"] = "on"

    assert save(api, confirmed_capture, draft=path).status_code == 303
    assert api.get(f"/admin/forms/{slug}/draft/pdf").status_code == 409

    def pdf(fields):
        fields["generation"] = "pdf"

    restored = save(api, pdf, draft=path)
    assert restored.status_code == 303
    assert api.get(f"/admin/forms/{slug}/draft/pdf").status_code == 200
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
    assert compile_definition(draft.document).generation == "pdf"
