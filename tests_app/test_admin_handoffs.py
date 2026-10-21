"""Gestão HTML pelo mesmo serviço, com sessão, CSRF, erros recuperáveis e segredo único."""

import re
from uuid import uuid4

from admin_api_support import ORIGIN
from handoff_support import isolated_handoff_contract  # noqa: F401
from test_admin import csrf_token, login
from test_handoffs import configured


def test_html_handoff_configuration_requires_session_confirmation_and_keeps_safe_input(
    deps, api_client
):
    with deps.sessions.begin() as session:
        configured(session)
    client = api_client("total")
    page = "/admin/forms/synthetic/handoff"
    assert client.get(page).status_code == 303
    assert login(client).status_code == 303
    csrf = csrf_token(client)
    assert client.get(page).status_code == 200
    save = {
        "csrf_token": csrf,
        "expected_version": "2",
        "name": "Atendimento sintético",
        "entry_url": "https://example.test/continuar",
        "reference_ttl_seconds": "3600",
    }
    refused = client.post(page + "/save", data=save, headers=ORIGIN)
    assert refused.status_code == 422 and "Confirme a ação" in refused.text
    assert 'value="Atendimento sintético"' in refused.text
    saved = client.post(page + "/save", data={**save, "confirm": "true"}, headers=ORIGIN)
    assert saved.status_code == 303
    creation = {
        "csrf_token": csrf,
        "expected_version": "3",
        "receiver_revision": "1",
        "name": "HTML sintético",
        "expiry_choice": "none",
        "idempotency_key": str(uuid4()),
        "confirm": "true",
    }
    created = client.post(page + "/create", data=creation, headers=ORIGIN)
    assert created.status_code == 200
    secret = re.search(r"<code>(lki_[^<]+)</code>", created.text)
    assert secret is not None
    assert secret[1] not in client.get(page).text
    replay = client.post(page + "/create", data=creation, headers=ORIGIN)
    assert replay.status_code == 409 and secret[1] not in replay.text
    assert created.headers["Cache-Control"] == "no-store"


def test_editor_selects_observed_receiver_without_manual_uuid_and_preserves_saved_binding(
    deps, api_client
):
    from starlette.datastructures import FormData

    from app.admin.form_editor import FormEdit
    from app.forms import drafts

    with deps.sessions.begin() as session:
        spec, _ = configured(session)
    client = api_client("total")
    login(client)
    page = client.get("/admin/forms/synthetic/draft")
    assert page.status_code == 200
    assert 'name="completion.handoff.selection"' in page.text
    assert 'type="hidden" name="completion.handoff.current.receiver_id"' in page.text
    with deps.sessions() as session:
        draft = drafts.load(session, spec.slug)
    original = FormEdit.of_document(draft.document).completion
    assert original.receiver_revision == "1"
    selected = FormEdit.of_form(
        FormData(
            {
                "completion.mode": "redirect",
                "completion.url": "https://example.test/typed",
                "completion.handoff.selection": "current",
                "completion.handoff.current.entry_url": "https://example.test/observed",
                "completion.handoff.current.receiver_id": original.receiver_id,
                "completion.handoff.current.receiver_revision": "2",
            }
        )
    ).completion
    assert selected.url == "https://example.test/observed"
    assert selected.receiver_revision == "2"
    assert original.receiver_revision == "1"


def test_credential_expiry_uses_brasilia_picker_and_preserves_invalid_input(deps, api_client):
    from datetime import UTC, datetime

    import sqlalchemy as sa

    from app.models import IntegrationCredential

    with deps.sessions.begin() as session:
        configured(session)
    client = api_client("total")
    login(client)
    csrf = csrf_token(client)
    page = "/admin/forms/synthetic/handoff"
    shown = client.get(page)
    assert 'type="datetime-local"' in shown.text and "Brasília" in shown.text
    body = {
        "csrf_token": csrf,
        "expected_version": "2",
        "receiver_revision": "1",
        "name": "Horário de Brasília",
        "expiry_choice": "date",
        "expires_at": "2030-01-02T10:30",
        "idempotency_key": str(uuid4()),
        "confirm": "true",
    }
    invalid = client.post(
        page + "/create", data={**body, "expires_at": "data-inválida"}, headers=ORIGIN
    )
    assert invalid.status_code == 422
    assert 'value="data-inválida"' in invalid.text and 'value="Horário de Brasília"' in invalid.text
    created = client.post(page + "/create", data=body, headers=ORIGIN)
    assert created.status_code == 200
    with deps.sessions() as session:
        expiry = session.scalar(
            sa.select(IntegrationCredential.expires_at).where(
                IntegrationCredential.name == body["name"]
            )
        )
        assert expiry == datetime(2030, 1, 2, 13, 30, tzinfo=UTC)


def test_receiver_duration_uses_human_options_without_rounding_api_values(deps, api_client):
    from test_handoffs import NOW

    from app import handoff_receivers
    from app.handoff_types import SaveReceiver

    with deps.sessions.begin() as session:
        configured(session)
        handoff_receivers.save_receiver(
            session,
            "synthetic",
            actor="teste",
            now=NOW,
            command=SaveReceiver(
                expected_version="2",
                name="Atendimento",
                entry_url="https://example.test/continuar",
                reference_ttl_seconds=61,
                confirm=True,
            ),
        )
    client = api_client("total")
    login(client)
    page = "/admin/forms/synthetic/handoff"
    shown = client.get(page)
    assert "<select" in shown.text and "7 dias" in shown.text
    assert 'value="61" selected' in shown.text
    assert "1 minuto e 1 segundo" in shown.text
    body = {
        "csrf_token": csrf_token(client),
        "expected_version": "3",
        "name": "Atendimento",
        "entry_url": "https://example.test/continuar",
        "reference_ttl_seconds": "61",
        "confirm": "true",
    }
    assert client.post(page + "/save", data=body, headers=ORIGIN).status_code == 303
    with deps.sessions() as session:
        assert handoff_receivers.get_receiver(session, "synthetic").reference_ttl_seconds == 61
