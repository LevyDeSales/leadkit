"""Ativar e desativar um formulário no admin, pela costura HTTP (TestClient). Inativo, ele diz à
lead que está indisponível, e a API recusa envios novos sem criar envio; os pedidos que já existem
seguem a versão deles até o PDF."""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa
from conftest import DEMO_ANSWERS, Receiver
from fastapi.testclient import TestClient
from test_admin import ORIGIN, FakeClock, csrf_token, finish, login, reprocess, section
from test_admin_destinations import ADMIN_ENV, create
from test_admin_form_pages import rows_of
from test_admin_forms import (
    CONSENT_VERSION,
    DEFINITION,
    EXAMPLE_TITLE,
    SLUG,
    admin_text,
    import_form,
    send_form,
)
from test_public import assert_private, job_id_of, text

from app.api.main import create_app
from app.deps import Deps
from app.models import AuditLog
from app.settings import Settings


@pytest.fixture
def triggered() -> list[tuple[uuid.UUID, int]]:
    return []


@pytest.fixture
def client(deps: Deps, triggered: list[tuple[uuid.UUID, int]]) -> TestClient:
    """O navegador da operadora, já no admin, e o da lead: o mesmo app, sem o Hatchet."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        triggered.append((job_id, attempt))
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, clock=FakeClock()), follow_redirects=False
    )
    assert login(current).status_code == 303
    return current


def set_active(
    client: TestClient, slug: str, active: bool, *, confirm: bool = True
) -> httpx.Response:
    """Ativa ou desativa pelo formulário da página do formulário, como o navegador."""
    data = {"csrf_token": csrf_token(client)}
    if confirm:
        data["confirm"] = "on"
    action = "activate" if active else "deactivate"
    return client.post(f"/admin/forms/{slug}/{action}", data=data, headers=ORIGIN)


def api_body(consent_version: str = CONSENT_VERSION) -> dict[str, object]:
    return {"answers": DEMO_ANSWERS, "consent": {"privacy": True, "text_version": consent_version}}


def page_answers() -> dict[str, str]:
    """O formulário da página, preenchido e com o consentimento."""
    return {**DEMO_ANSWERS, "_privacy": "1", "_consent_version": CONSENT_VERSION, "_idem": "um"}


def test_a_deactivated_form_tells_the_lead_it_is_unavailable_and_takes_no_submission(
    client: TestClient, triggered: list[tuple[uuid.UUID, int]]
) -> None:
    import_form(client, DEFINITION)

    response = set_active(client, SLUG, False)

    assert response.status_code == 303 and response.headers["location"] == f"/admin/forms/{SLUG}"
    page = client.get(f"/p/{SLUG}")
    assert page.status_code == 410 and "Este formulário não está disponível" in text(page.text)
    assert "não é um problema com o seu aparelho" in text(page.text)
    assert_private(page)
    sent = client.post(f"/p/{SLUG}", data=page_answers())
    assert sent.status_code == 410 and "Suas respostas não foram enviadas" in text(sent.text)
    api = client.post(f"/v1/forms/{SLUG}/submissions", json=api_body())
    assert api.status_code == 410
    assert api.json() == {"code": "form_unavailable", "detail": "formulário indisponível"}
    assert triggered == []
    row = text(rows_of(client.get("/admin/forms"))[SLUG])
    assert "Inativo" in row and "0 envios" in row


def test_a_form_in_code_is_deactivated_and_activated_again_without_touching_the_others(
    client: TestClient, triggered: list[tuple[uuid.UUID, int]]
) -> None:
    assert set_active(client, "demo", False).status_code == 303

    assert client.get("/p/demo").status_code == 410
    refused = client.post("/v1/forms/demo/submissions", json=api_body("demo-v1"))
    assert refused.status_code == 410 and triggered == []
    assert client.get("/p/mcc").status_code == 200  # os outros seguem no ar
    situation = text(section(client.get("/admin/forms/demo").text, "situacao"))
    assert "Inativo" in situation and "Ativar o formulário" in situation

    assert set_active(client, "demo", True).status_code == 303

    page = client.get("/admin/forms/demo").text
    assert "Formulário ativado" in text(page) and "Desativar…" in text(section(page, "situacao"))
    assert client.get("/p/demo").status_code == 200
    accepted = client.post("/v1/forms/demo/submissions", json=api_body("demo-v1"))
    assert accepted.status_code == 202 and len(triggered) == 1


def test_requests_made_before_the_deactivation_keep_their_version_to_the_end(
    client: TestClient, deps: Deps, triggered: list[tuple[uuid.UUID, int]]
) -> None:
    import_form(client, DEFINITION)
    page = send_form(client)  # o pedido entra com o formulário ativo
    public_id = page.rsplit("/", 1)[1]

    set_active(client, SLUG, False)
    finish(deps, job_id_of(deps, page))  # o worker segue com a versão do envio

    result = client.get(page)
    assert result.status_code == 200 and EXAMPLE_TITLE in text(result.text)
    assert client.get(f"{page}/pdf").status_code == 200
    assert "prompt demo-definicao-v1" in admin_text(client, page)
    again = reprocess(client, public_id, csrf_token(client), reason="Plano revisado pela equipe")
    assert again.status_code == 303 and triggered[-1][1] == 2
    finish(deps, job_id_of(deps, page), attempt=2)
    assert "tentativa 2 · modelo fake · prompt demo-definicao-v1" in admin_text(client, page)


def test_activation_and_deactivation_are_audited_in_the_operator_name_once_per_change(
    client: TestClient, deps: Deps
) -> None:
    set_active(client, "demo", False)
    repeated = set_active(client, "demo", False)  # outra aba, ou um clique repetido
    assert repeated.status_code == 303
    assert "o formulário já estava inativo" in text(client.get(repeated.headers["location"]).text)
    set_active(client, "demo", True)

    with deps.sessions() as session:
        entries = list(session.scalars(sa.select(AuditLog).order_by(AuditLog.at)))
    assert [(e.actor, e.action, e.target_type, e.details) for e in entries] == [
        ("operadora", "form.deactivate", "form", {"form_slug": "demo"}),
        ("operadora", "form.activate", "form", {"form_slug": "demo"}),
    ]


def test_deactivating_needs_the_session_the_csrf_token_and_a_confirmation(
    client: TestClient,
) -> None:
    anonymous = TestClient(client.app, follow_redirects=False)
    form = {"confirm": "on"}
    path = "/admin/forms/demo/deactivate"

    activate = "/admin/forms/demo/activate"
    refused = [
        anonymous.post(path, data={"csrf_token": "x", **form}, headers=ORIGIN),
        anonymous.post(activate, data={"csrf_token": "x"}, headers=ORIGIN),
        client.post(path, data={"csrf_token": "errado", **form}, headers=ORIGIN),
        client.post(path, data={"csrf_token": csrf_token(client), **form}),  # sem Origin
        client.post(activate, data={"csrf_token": "errado"}, headers=ORIGIN),
    ]
    unconfirmed = set_active(client, "demo", False, confirm=False)

    assert [response.status_code for response in refused] == [303, 303, 403, 403, 403]
    assert all(r.headers["location"].startswith("/admin/login") for r in refused[:2])
    assert unconfirmed.status_code == 422
    assert "Confirme que entende que o formulário sai do ar" in text(
        section(unconfirmed.text, "situacao")
    )
    assert client.get("/p/demo").status_code == 200
    assert set_active(client, "nao-existe", False).status_code == 404


@pytest.mark.parametrize("slug", ["demo", SLUG])
def test_the_destination_of_an_inactive_form_can_still_be_set(
    client: TestClient, receiver: Receiver, slug: str
) -> None:
    import_form(client, DEFINITION)
    set_active(client, slug, False)

    page = client.get(f"/admin/destinations/forms/{slug}")

    assert page.status_code == 200
    assert "Recebe os eventos de “Projeto de demonstração”" in text(page.text)
    create(client, f"/admin/destinations/forms/{slug}", receiver.url("/inativo"))
    listed = text(section(client.get("/admin/destinations").text, "formularios"))
    assert receiver.host in listed
