"""Leitura histórica de capturas nas interfaces autorizadas, com dados sintéticos."""

import json
from copy import deepcopy
from datetime import datetime

import pytest
from admin_api_support import api_client
from fastapi.testclient import TestClient
from test_capture import capture_document

from app.api.main import create_app
from app.forms.definition import fingerprint
from app.forms.stored import import_definition
from app.models import Form, FormVersion


def stored_capture(deps, document=None, *, consent=None):
    document = document or reading_document()
    import_definition(deps, json.dumps(document), actor="operadora")
    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/forms/capture-test/submissions",
            json={
                "answers": document["examples"]["answers"],
                "consent": consent or {"privacy": True, "text_version": "capture-test-v1"},
            },
        )
    assert response.status_code == 202
    return response.json()["submission_id"]


def reading_document():
    document = capture_document()
    document["steps"][-1]["questions"].extend(
        [
            {
                "name": "preferencias",
                "kind": "choices",
                "label": "Quais caminhos?",
                "options": [
                    {"value": "a", "label": "Primeiro caminho"},
                    {"value": "b", "label": "Segundo caminho"},
                ],
            },
            {
                "name": "observacao",
                "kind": "textarea",
                "label": "Sua observação",
                "required": False,
                "max_length": 1000,
            },
        ]
    )
    document["examples"]["answers"]["preferencias"] = ["b", "a"]
    return document


def test_capture_labels_use_submitted_version(deps):
    document = reading_document()
    submission_id = stored_capture(deps, document)
    changed = deepcopy(document)
    changed["steps"][-1]["questions"] = [
        q for q in changed["steps"][-1]["questions"] if q["name"] != "observacao"
    ]
    question = changed["steps"][-1]["questions"][-1]
    question["label"] = "Pergunta atual"
    question["options"] = [{"value": "b", "label": "Alternativa atual"}]
    changed["examples"]["answers"]["preferencias"] = ["b"]
    import sqlalchemy as sa

    with deps.sessions.begin() as session:
        form_id = session.scalar(sa.select(Form.id).where(Form.slug == "capture-test"))
        session.add(
            FormVersion(
                form_id=form_id,
                number=2,
                definition=changed,
                fingerprint=fingerprint(changed),
                note="Nova versão sintética",
                published_by="operadora",
            )
        )
    assert deps.forms.current("capture-test").version_number == 2
    response = api_client(deps).get(
        f"/admin/api/v1/captures/{submission_id}?include_lead_data=true"
    )
    assert response.status_code == 200
    lead = response.json()["lead_data"]
    assert [row["name"] for row in lead["answer_rows"]] == [
        question["name"] for step in document["steps"] for question in step["questions"]
    ]
    rows = {row["name"]: row for row in lead["answer_rows"]}
    assert rows["preferencias"] == {
        "name": "preferencias",
        "label": "Quais caminhos?",
        "value_text": "Segundo caminho; Primeiro caminho",
        "historical_label_available": True,
    }
    assert rows["observacao"]["value_text"] == "Não respondida"
    assert lead["answers"]["preferencias"] == ["b", "a"]
    assert "Pergunta atual" not in response.text
    assert "Alternativa atual" not in response.text


def test_capture_reading_preserves_zero_and_missing():
    from app.admin.capture_reading import answer_rows
    from app.forms.definition import compile_definition

    spec = compile_definition(reading_document())
    rows = {
        row["name"]: row
        for row in answer_rows(
            spec,
            {
                "nome": 0,
                "email": False,
                "preferencias": [],
                "objetivo": "",
                "unknown": ["b", "a"],
            },
        )
    }
    assert rows["nome"]["value_text"] == "0"
    assert rows["email"]["value_text"] == "false"
    assert rows["observacao"]["value_text"] == "Não respondida"
    assert rows["preferencias"]["value_text"] == "Nenhuma alternativa selecionada"
    assert rows["objetivo"]["value_text"] == "Resposta vazia"
    assert rows["unknown"] == {
        "name": "unknown",
        "label": "unknown",
        "value_text": '["b", "a"]',
        "historical_label_available": False,
    }


def test_capture_without_history_does_not_use_current_labels(deps):
    from test_admin import login

    from app.models import Submission

    submission_id = stored_capture(deps)
    with deps.sessions.begin() as session:
        import uuid

        submission = session.get_one(Submission, uuid.UUID(submission_id))
        submission.form_version_number = 99
    api = api_client(deps)
    response = api.get(f"/admin/api/v1/captures/{submission_id}?include_lead_data=true")
    assert response.status_code == 200
    lead = response.json()["lead_data"]
    assert lead["identity"] is None
    assert lead["identity_unavailable"] is True
    assert lead["answers"]["preferencias"] == ["b", "a"]
    assert all(row["historical_label_available"] is False for row in lead["answer_rows"])
    assert "Quais caminhos?" not in response.text
    assert "Segundo caminho" not in response.text
    assert login(api).status_code == 303
    page = api.get(f"/admin/captures/{submission_id}")
    assert page.status_code == 200
    assert "Identidade histórica indisponível" in page.text
    assert "Rótulo histórico indisponível" in page.text
    assert "Quais caminhos?" not in page.text


def test_capture_unknown_option_keeps_original_value_without_guessing():
    from app.admin.capture_reading import answer_rows
    from app.forms.definition import compile_definition

    rows = {
        row["name"]: row
        for row in answer_rows(
            compile_definition(reading_document()), {"preferencias": ["b", "removed", {"raw": 0}]}
        )
    }
    assert rows["preferencias"]["historical_label_available"] is False
    assert rows["preferencias"]["value_text"] == (
        'Segundo caminho; removed (rótulo indisponível); {"raw": 0} (rótulo indisponível)'
    )


def test_capture_reading_escapes_untrusted_text(deps):
    from test_admin import login

    from app.admin.capture_reading import answer_rows
    from app.forms.definition import compile_definition

    document = reading_document()
    question = document["steps"][-1]["questions"][-1]
    question["label"] = 'Observação <img src=x onerror="alert(1)"> & detalhes'
    document["steps"][-1]["questions"][-2]["options"][0]["label"] = "<b>Primeiro & caminho</b>"
    document["examples"]["answers"]["observacao"] = '<script>alert("sintético")</script> & texto'
    rows = answer_rows(compile_definition(document), document["examples"]["answers"])
    row = next(row for row in rows if row["name"] == "observacao")
    assert type(row["label"]) is str
    assert type(row["value_text"]) is str
    assert row["value_text"] == '<script>alert("sintético")</script> & texto'
    submission_id = stored_capture(deps, document)
    api = api_client(deps)
    assert api.get(f"/admin/captures/{submission_id}").status_code == 303
    assert login(api).status_code == 303
    page = api.get(f"/admin/captures/{submission_id}")
    assert page.status_code == 200
    assert "&lt;script&gt;alert(&#34;sintético&#34;)&lt;/script&gt; &amp; texto" in page.text
    assert "Observação &lt;img src=x onerror=&#34;alert(1)&#34;&gt; &amp; detalhes" in page.text
    assert "&lt;b&gt;Primeiro &amp; caminho&lt;/b&gt;" in page.text
    assert "<script>alert" not in page.text
    assert "<img src=x" not in page.text
    assert "&amp;lt;" not in page.text
    assert "<h3>Consentimentos registrados</h3>" in page.text
    assert "<h3>Pontuação registrada</h3>" in page.text
    assert "<summary>Detalhes técnicos do envio · uso interno</summary>" in page.text


def test_capture_invalid_identity_keeps_historical_answers(deps):
    import uuid

    from test_admin import login

    from app.models import Lead, Submission

    submission_id = stored_capture(deps)
    with deps.sessions.begin() as session:
        submission = session.get_one(Submission, uuid.UUID(submission_id))
        submission.answers = {
            **submission.answers,
            "email": "invalid",
            "unknown<script>": "<b>raw</b>",
        }
        lead = session.get_one(Lead, submission.lead_id)
        lead.name = "Cadastro compartilhado substituto"
    api = api_client(deps)
    response = api.get(f"/admin/api/v1/captures/{submission_id}?include_lead_data=true")
    assert response.status_code == 200
    lead_data = response.json()["lead_data"]
    assert lead_data["identity"] is None
    assert lead_data["identity_unavailable"] is True
    assert lead_data["answers"]["email"] == "invalid"
    assert lead_data["answer_rows"][-1] == {
        "name": "unknown<script>",
        "label": "unknown<script>",
        "value_text": "<b>raw</b>",
        "historical_label_available": False,
    }
    assert login(api).status_code == 303
    page = api.get(f"/admin/captures/{submission_id}")
    assert page.status_code == 200
    assert "unknown&lt;script&gt;" in page.text
    assert "&lt;b&gt;raw&lt;/b&gt;" in page.text
    assert "Cadastro compartilhado substituto" not in page.text
    assert "Identidade histórica indisponível" in page.text


def test_capture_reading_requires_lead_opt_in(deps, tmp_path):
    from admin_mcp_live import Live

    submission_id = stored_capture(deps)
    path = f"/admin/api/v1/captures/{submission_id}"
    total = api_client(deps)
    for endpoint in ("/admin/api/v1/captures", path):
        response = total.get(endpoint)
        assert response.status_code == 200
        assert "marina@example.test" not in response.text
        assert '"answer_rows"' not in response.text
        assert '"answers"' not in response.text
        item = response.json()["items"][0] if endpoint.endswith("captures") else response.json()
        assert item["lead_data"] is None
        allowed = total.get(endpoint, params={"include_lead_data": True})
        assert allowed.status_code == 200
        assert '"answer_rows"' in allowed.text
    reader = api_client(deps, role="leitura")
    for endpoint in ("/admin/api/v1/captures", path):
        assert reader.get(endpoint).status_code == 200
        refused = reader.get(endpoint, params={"include_lead_data": True})
        assert refused.status_code == 403
        assert '"answer_rows"' not in refused.text
        assert total.get(endpoint, headers={"Authorization": ""}).status_code == 401
    for client, opt_in in ((total, False), (reader, True), (total, True)):
        live = Live(client, tmp_path, lead_data=opt_in)
        try:
            arguments = {"submission_id": submission_id}
            state = live.ok("get_capture", arguments)
            assert state["lead_data"] is None
            if opt_in and client.role == "total":
                state = live.ok("get_capture", {**arguments, "include_lead_data": True})
                assert state["lead_data"]["answer_rows"]
                assert state["lead_data"]["answers"]["preferencias"] == ["b", "a"]
            else:
                refused = live.call("get_capture", {**arguments, "include_lead_data": True})
                assert refused.is_error
                assert "marina@example.test" not in refused.text
        finally:
            live.close()


def test_capture_reading_does_not_preserve_html_safe_string_subclasses():
    from markupsafe import Markup

    from app.admin.capture_reading import answer_rows

    row = answer_rows(None, {Markup("<b>unknown</b>"): Markup("<img src=x>")})[0]
    assert type(row["name"]) is str
    assert type(row["label"]) is str
    assert type(row["value_text"]) is str
    assert row["name"] == "<b>unknown</b>"
    assert row["value_text"] == "<img src=x>"


@pytest.mark.parametrize("method", ["checkbox", "submit"])
def test_capture_consent_is_json_safe_in_html_and_preserves_validated_facts(deps, method):
    from test_admin import login

    document = reading_document()
    document["consent"]["acquisition_method"] = method
    declaration = {
        "method": method,
        "action": "confirm" if method == "checkbox" else "submit",
        "text_version": "capture-test-v1",
    }
    if method == "checkbox":
        declaration["privacy"] = True
    submission_id = stored_capture(deps, document, consent=declaration)
    api = api_client(deps)
    path = f"/admin/api/v1/captures/{submission_id}?include_lead_data=true"
    response = api.get(path)
    assert response.status_code == 200
    consent = response.json()["lead_data"]["consent"]
    assert consent["privacy"] is True and consent["method"] == method
    assert consent["source"] == "public_api_declaration"
    assert datetime.fromisoformat(consent["privacy_at"]).utcoffset().total_seconds() == 0
    assert login(api).status_code == 303
    page = api.get(f"/admin/captures/{submission_id}")
    assert page.status_code == 200
    assert "JSON técnico do envio" in page.text
    assert consent["privacy_at"] in page.text
    assert (
        "Confirmação pela caixa" if method == "checkbox" else "Concordância ao enviar"
    ) in page.text


@pytest.mark.parametrize(
    "invalid", [{"action": "unknown"}, {"privacy_at": "invalid"}, {"marketing_email": "yes"}]
)
def test_invalid_capture_consent_never_becomes_an_authorization_during_serialization(deps, invalid):
    import uuid

    from test_admin import login

    from app.models import Submission

    submission_id = stored_capture(deps)
    with deps.sessions.begin() as session:
        row = session.get_one(Submission, uuid.UUID(submission_id))
        row.consent = {**row.consent, **invalid}
    api = api_client(deps)
    consent = api.get(f"/admin/api/v1/captures/{submission_id}?include_lead_data=true").json()[
        "lead_data"
    ]["consent"]
    assert consent["privacy"] is False
    assert consent["privacy_at"] is None and consent["method"] is None
    assert consent["marketing_email"] is False
    assert login(api).status_code == 303
    assert api.get(f"/admin/captures/{submission_id}").status_code == 200
