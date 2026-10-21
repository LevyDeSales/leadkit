"""Navegação da operadora pela costura HTTP, sem executar geração ou integrações."""

from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from test_admin import FakeClock, login
from test_admin_destinations import ADMIN_ENV
from test_public import tags, text

from app.api.main import create_app
from app.settings import Settings


@pytest.fixture
def client(deps):
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Navegação não dispara geração")

    return TestClient(create_app(trigger=forbidden, clock=FakeClock()), follow_redirects=False)


@pytest.mark.parametrize("path", ["/admin/responses", "/admin/advanced"])
def test_navigation_hubs_require_a_session(client, path):
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login?" + urlencode({"next": path})


def test_response_total_opens_both_historical_modalities(client, deps):
    from conftest import legacy_definition, publish
    from test_intake import submit

    from app.forms.definition import compile_definition

    pdf = submit(deps)
    document = legacy_definition("demo")
    document["generation"] = "none"
    document["completion"] = {"mode": "confirmation"}
    for key in ("instructions", "prompt", "plan", "pdf", "preview"):
        document.pop(key, None)
    document["examples"].pop("plan", None)
    capture_spec = compile_definition(document, version_number=2)
    publish(deps, capture_spec)
    captured = submit(deps, key="captured-history", form=capture_spec)
    assert login(client).status_code == 303

    forms = client.get("/admin/forms")
    assert tags(forms.text, "a", href="/admin/responses?form=demo")
    assert "2 respostas" in text(forms.text)
    hub = client.get("/admin/responses?form=demo")
    assert hub.status_code == 200
    assert "Projeto de demonstração" in text(hub.text)
    assert "demo" in text(hub.text)
    assert tags(hub.text, "a", href="/admin?form=demo")
    assert tags(hub.text, "a", href="/admin/captures?form=demo")
    assert str(pdf.job.public_id) in client.get("/admin?form=demo").text
    assert str(captured.submission.id) in client.get("/admin/captures?form=demo").text


def test_main_navigation_and_advanced_use_plain_links(client, deps, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Navegação não gera PDF")

    monkeypatch.setattr(deps.pdf, "render", forbidden)
    assert login(client).headers["location"] == "/admin/forms"
    forms = client.get("/admin/forms")
    assert tags(forms.text, "a", **{"class": "brand", "href": "/admin/forms"})
    for path, current in (
        ("/admin/forms", "/admin/forms"),
        ("/admin/responses", "/admin/responses"),
        ("/admin", "/admin/responses"),
        ("/admin/captures", "/admin/responses"),
        ("/admin/advanced", "/admin/advanced"),
        ("/admin/api-keys", "/admin/advanced"),
        ("/admin/mcp", "/admin/advanced"),
    ):
        page = client.get(path)
        assert page.status_code == 200
        assert tags(page.text, "a", href=current, **{"aria-current": "page"})
        assert 'role="tab"' not in page.text
    advanced = client.get("/admin/advanced")
    for path in ("/admin/api-keys", "/admin/mcp", "/admin/api/v1/reference"):
        assert tags(advanced.text, "a", href=path)
    guide = tags(
        forms.text,
        "a",
        href="https://github.com/LevyDeSales/leadkit/blob/develop/docs/guia-da-operadora/README.md",
    )
    assert guide and "Guia de uso" in text(forms.text)


@pytest.mark.parametrize(
    "target",
    ["/administrator", "/admin\\evil", "https://example.test/admin", "//example.test/admin"],
)
def test_invalid_login_targets_return_to_forms(client, target):
    assert login(client, next=target).headers["location"] == "/admin/forms"


def test_authenticated_login_preserves_an_explicit_internal_destination(client):
    assert login(client).status_code == 303
    assert client.get("/admin/login?next=/admin/captures").headers["location"] == "/admin/captures"
