"""Os formulários na API de admin (`/admin/api/v1`): listar, ler, ativar, desativar, ler e mudar a
retenção, e ler e comparar versões, pela costura HTTP.

Elas leem pela mesma consulta e pela mesma ação que as páginas (`app.admin.queries.forms`,
`app.admin.actions.versions`, os serviços `stored` e `retention`); a paridade com a página está no
fim de cada operação. As chaves destes testes são sorteadas na execução.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_support import (
    ORIGIN,
    ApiClient,
    assert_same_effect,
    audited,
    built_openapi,
    make_api,
    url_of,
)
from conftest import TABLES
from test_admin import FakeClock, csrf_token, login
from test_admin_form_pages import rows_of
from test_admin_forms import DEFINITION, SLUG, store_version
from test_form_retention import aged, order, set_periods
from test_intake import submit
from test_public import text

from app import destinations, retention
from app.deps import Deps
from app.forms import drafts, stored
from app.forms.definition import fingerprint
from app.integrations import config as integration_config
from app.models import AuditLog, Form, FormDraft, utcnow
from app.settings import RETENTION_PDF_MARGIN_DAYS

Client = Callable[..., ApiClient]
PAGE = "/admin"
BROKEN = "quebrado"  # o formulário cuja versão atual não compila mais
SCRATCH = "gestao"  # o criado do zero, sem versão publicada
SECRET_PATH = "/entrada-que-vale-como-senha"  # o caminho de um webhook costuma valer como senha
SECRET_HOST = "receptor.example.test"
BASEROW_TOKEN = "token-de-teste-do-baserow"
NEWER = "A segunda versão."
OTHER = "Uma introdução só do rascunho."
# A chamada da API grava o último uso da chave que chama; a página, não: é o que a comparação tira.
CALLER_USE = {"api_keys": {"last_used_at"}}


def document(**changes: Any) -> dict[str, Any]:
    return {**json.loads(DEFINITION), **changes}


def import_document(deps: Deps, definition: dict[str, Any]) -> None:
    stored.import_definition(deps, json.dumps(definition), actor="testes")


def by_a_key(deps: Deps, action: str) -> list[AuditLog]:
    """As entradas da auditoria de uma ação feitas por uma chave de API (`chave:<nome>`)."""
    return [entry for entry in audited(deps, action) if entry.actor.startswith("chave:")]


def pending_draft(deps: Deps, slug: str, **changes: Any) -> None:
    """Um rascunho que difere da versão atual, gravado como o editor o grava."""
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
    assert draft is not None
    drafts.save(
        deps,
        slug,
        drafts.texts_of({**draft.document, **changes}),
        actor="testes",
        expected=draft.token,
    )


def save_destination(deps: Deps, slug: str | None, host: str = SECRET_HOST) -> None:
    with deps.sessions.begin() as session:
        destinations.save(
            session,
            deps.cipher,
            form_slug=slug,
            url=f"https://{host}{SECRET_PATH}",
            actor="testes",
            expected_version=0,
            production=False,
            http_confirmed=False,
        )


def connect_integration(deps: Deps, slug: str) -> None:
    with deps.sessions.begin() as session:
        integration_config.connect(
            session,
            deps.cipher,
            form_slug=slug,
            base_url="https://baserow.example.test",
            table_id=77,
            token=BASEROW_TOKEN,
            actor="testes",
            expected_version=0,
            production=False,
        )


@pytest.fixture
def world(deps: Deps) -> None:
    """Os formulários do teste, além do demo, do mcc e do reforma-2027 (ativos, na versão 1):

    - `demo-definicao`: a versão 1 importada, uma versão 2 e um rascunho pendente;
    - `quebrado`: uma versão 2 que o compilador deste deploy não aceita mais;
    - `gestao`: criado do zero, sem versão publicada;
    - `mcc`: desativado."""
    import_document(deps, document())
    store_version(deps, 2, document(intro=NEWER))
    pending_draft(deps, SLUG, intro=OTHER)
    import_document(deps, document(slug=BROKEN))
    store_version(deps, 2, document(slug=BROKEN, minutes=0))
    drafts.create(deps, title="Gestão do escritório", slug=SCRATCH, actor="testes")
    stored.set_active(deps.sessions, "mcc", False, actor="testes")


def items_by_slug(response: httpx.Response) -> dict[str, dict[str, Any]]:
    assert response.status_code == 200, response.text
    return {item["slug"]: item for item in response.json()["items"]}


# --- list_forms ---------------------------------------------------------------------------------

SUMMARY_FIELDS = {
    "slug",
    "title",
    "active",
    "public_url",
    "published_version",
    "published_at",
    "version_compiles",
    "draft_pending",
    "data_notices",
    "submissions",
}


def test_list_forms_needs_a_key(deps: Deps, world: None) -> None:
    response = make_api(deps, FakeClock()).get(url_of("list_forms"))

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"
    assert response.headers["www-authenticate"] == "Bearer"


def test_list_forms_is_an_envelope_by_slug_with_the_state_of_each_form(
    api_client: Client, world: None
) -> None:
    response = api_client("leitura").get(url_of("list_forms"))

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"items", "next_cursor"} and body["next_cursor"] is None
    assert [item["slug"] for item in body["items"]] == sorted(
        ["demo", SLUG, BROKEN, SCRATCH, "mcc", "reforma-2027"]
    )
    by_slug = items_by_slug(response)
    assert all(set(item) == SUMMARY_FIELDS for item in by_slug.values())
    demo = by_slug["demo"]
    assert (demo["title"], demo["active"], demo["published_version"]) == (
        "Projeto de demonstração",
        True,
        1,
    )
    assert demo["public_url"] == "http://testserver/p/demo"
    assert (demo["version_compiles"], demo["draft_pending"], demo["submissions"]) == (
        True,
        False,
        0,
    )
    assert demo["data_notices"] == [] and demo["published_at"].endswith("Z")
    assert by_slug["mcc"]["active"] is False


def test_list_forms_flags_a_pending_draft_a_version_that_does_not_compile_and_no_version(
    api_client: Client, world: None
) -> None:
    by_slug = items_by_slug(api_client("leitura").get(url_of("list_forms")))

    assert (by_slug[SLUG]["published_version"], by_slug[SLUG]["draft_pending"]) == (2, True)
    assert (by_slug[BROKEN]["published_version"], by_slug[BROKEN]["version_compiles"]) == (2, False)
    scratch = by_slug[SCRATCH]
    assert (scratch["published_version"], scratch["published_at"]) == (None, None)
    assert (scratch["version_compiles"], scratch["draft_pending"]) == (None, True)


def test_list_forms_counts_the_submissions_of_each_form(
    api_client: Client, deps: Deps, world: None
) -> None:
    submit(deps, key="um")
    submit(deps, key="dois")

    by_slug = items_by_slug(api_client("leitura").get(url_of("list_forms")))

    assert (by_slug["demo"]["submissions"], by_slug["mcc"]["submissions"]) == (2, 0)


def test_list_forms_paginates_by_slug_and_neither_skips_nor_repeats_when_a_form_arrives(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("leitura")
    url = url_of("list_forms")
    everything = [item["slug"] for item in client.get(url).json()["items"]]

    first = client.get(url, params={"limit": 2})
    drafts.create(deps, title="Entrou no meio", slug="aaa-entrou", actor="testes")  # vem antes
    second = client.get(url, params={"limit": 2, "cursor": first.json()["next_cursor"]})
    third = client.get(url, params={"limit": 2, "cursor": second.json()["next_cursor"]})

    seen = [item["slug"] for page in (first, second, third) for item in page.json()["items"]]
    assert seen == everything
    assert first.json()["next_cursor"] and second.json()["next_cursor"]
    assert third.json()["next_cursor"] is None


def test_list_forms_a_bad_limit_or_cursor_names_the_field(api_client: Client, world: None) -> None:
    client = api_client("leitura")
    url = url_of("list_forms")

    for params, field in (({"limit": "0"}, "limit"), ({"cursor": "lixo"}, "cursor")):
        refused = client.get(url, params=params)
        assert refused.status_code == 422 and refused.json()["code"] == "validation_failed"
        assert [e["field"] for e in refused.json()["errors"]] == [field]


def test_list_forms_shows_the_same_forms_and_states_as_the_page(
    api_client: Client, world: None
) -> None:
    client = api_client("leitura")
    assert login(client).status_code == 303
    rows = rows_of(client.get(f"{PAGE}/forms"))

    by_slug = items_by_slug(client.get(url_of("list_forms")))

    assert list(rows) == list(by_slug)
    for slug, row in rows.items():
        shown, item = text(row), by_slug[slug]
        assert ("Ativo" in shown) == item["active"], slug
        assert ("Rascunho pendente" in shown) == item["draft_pending"], slug
        assert ("Não compila" in shown) == (item["version_compiles"] is False), slug
        version = item["published_version"]
        assert (f"Versão {version}" if version else "Nenhuma versão publicada") in shown, slug
        assert item["public_url"] in shown, slug
        assert re.search(rf"\b{item['submissions']} envios?\b", shown), slug


# --- get_form -----------------------------------------------------------------------------------

DETAIL_FIELDS = {
    "integration_issues",
    *SUMMARY_FIELDS,
    "version_label",
    "versions",
    "draft",
    "destination",
    "retention",
    "integration",
}


def form_url(slug: str) -> str:
    return url_of("get_form", slug=slug)


def test_get_form_needs_a_key(deps: Deps, world: None) -> None:
    response = make_api(deps, FakeClock()).get(form_url(SLUG))

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"


def test_get_form_gives_the_state_the_versions_and_the_draft(
    api_client: Client, world: None
) -> None:
    response = api_client("leitura").get(form_url(SLUG))

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == DETAIL_FIELDS
    assert (body["slug"], body["active"], body["published_version"]) == (SLUG, True, 2)
    assert body["title"] == "Projeto de demonstração"
    assert body["version_label"].endswith("v2")
    assert body["public_url"] == f"http://testserver/p/{SLUG}"
    assert [(v["number"], v["current"]) for v in body["versions"]] == [(2, True), (1, False)]
    assert all(v["published_at"].endswith("Z") and v["fingerprint"] for v in body["versions"])
    assert body["versions"][0]["published_by"] == "operadora"
    draft = body["draft"]
    assert (draft["pending"], draft["saved_by"], draft["base_version"]) == (True, "testes", 2)
    assert draft["current_version"] == 2 and draft["version"] and draft["stale_assets"] == []
    assert draft["saved_at"].endswith("Z")


def test_get_form_of_a_form_without_a_pending_draft_has_no_saved_by(
    api_client: Client, world: None
) -> None:
    body = api_client("leitura").get(form_url("mcc")).json()

    assert body["draft"]["pending"] is False
    assert (body["draft"]["saved_by"], body["draft"]["saved_at"]) == (None, None)
    assert (body["draft"]["base_version"], body["draft"]["current_version"]) == (1, 1)
    assert body["active"] is False and body["destination"] is None


def test_get_form_of_a_form_without_a_published_version(api_client: Client, world: None) -> None:
    response = api_client("leitura").get(form_url(SCRATCH))

    assert response.status_code == 200
    body = response.json()
    assert (body["published_version"], body["published_at"], body["version_label"]) == (
        None,
        None,
        None,
    )
    assert (body["version_compiles"], body["versions"], body["data_notices"]) == (None, [], [])
    draft = body["draft"]
    assert (draft["pending"], draft["current_version"], draft["base_version"]) == (True, None, None)
    assert body["draft_pending"] is True and body["active"] is True


def test_get_form_of_a_form_whose_current_version_no_longer_compiles(
    api_client: Client, world: None
) -> None:
    response = api_client("leitura").get(form_url(BROKEN))

    assert response.status_code == 200
    body = response.json()
    assert (body["published_version"], body["version_compiles"], body["version_label"]) == (
        2,
        False,
        None,
    )
    assert [v["number"] for v in body["versions"]] == [2, 1]  # o histórico segue legível


def test_get_form_that_does_not_exist_is_a_404(api_client: Client, world: None) -> None:
    response = api_client("leitura").get(form_url("nao-existe"))

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert "nao-existe" not in response.text


def test_get_form_says_which_destination_serves_it_and_only_the_host(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("leitura")
    none = client.get(form_url("demo")).json()["destination"]
    save_destination(deps, None, "padrao.example.test")
    default = client.get(form_url("demo"))
    save_destination(deps, "demo")
    own = client.get(form_url("demo"))

    assert none is None
    assert default.json()["destination"] == {
        "scope": "default",
        "host": "padrao.example.test",
        "events": ["plano.concluido"],
    }
    assert own.json()["destination"] == {
        "scope": "form",
        "host": SECRET_HOST,
        "events": ["plano.concluido"],
    }
    for response in (default, own, client.get(url_of("list_forms"))):
        assert SECRET_PATH not in response.text and "https://" not in response.text


def test_get_form_gives_the_retention_with_the_defaults_and_the_version(
    api_client: Client, deps: Deps, world: None
) -> None:
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Form)
            .where(Form.slug == "mcc")
            .values(retention_pdf_days=20, retention_days=45)
        )
    client = api_client("leitura")

    own = client.get(form_url("mcc")).json()["retention"]
    default = client.get(form_url("demo")).json()["retention"]

    settings = deps.settings
    environment = {"pdf_days": settings.retention_pdf_days, "request_days": settings.retention_days}
    assert own == {
        "own": {"pdf_days": 20, "request_days": 45},
        "default": environment,
        "in_force": {"pdf_days": 20, "request_days": 45},
        "version": "20:45",
    }
    assert default == {
        "own": {"pdf_days": None, "request_days": None},
        "default": environment,
        "in_force": environment,
        "version": ":",
    }


def test_get_form_gives_the_integration_and_never_the_token(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("leitura")
    none = client.get(form_url("demo")).json()["integration"]
    connect_integration(deps, "demo")

    response = client.get(form_url("demo"))

    assert none == {
        "configured": False,
        "active": None,
        "host": None,
        "table_id": None,
        "pending_syncs": 0,
        "failed_syncs": 0,
    }
    assert response.json()["integration"] == {
        "configured": True,
        "active": False,
        "host": "baserow.example.test",
        "table_id": 77,
        "pending_syncs": 0,
        "failed_syncs": 0,
    }
    assert BASEROW_TOKEN not in response.text


def test_get_form_carries_the_submissions_of_the_form(
    api_client: Client, deps: Deps, world: None
) -> None:
    submit(deps, key="um")

    body = api_client("leitura").get(form_url("demo")).json()

    assert body["submissions"] == 1 and body["data_notices"] == []


def test_get_form_shows_the_same_state_as_the_page(api_client: Client, world: None) -> None:
    client = api_client("leitura")
    assert login(client).status_code == 303

    for slug in (SLUG, "mcc", BROKEN, SCRATCH):
        page = text(client.get(f"{PAGE}/forms/{slug}").text)
        body = client.get(form_url(slug)).json()

        assert ("Inativo" in page) == (not body["active"]), slug
        assert ("A versão publicada não compila" in page) == (body["version_compiles"] is False)
        for version in body["versions"]:
            assert f"Versão {version['number']}" in page, slug


# --- activate_form e deactivate_form ------------------------------------------------------------

ACTIONS = {"activate_form": True, "deactivate_form": False}


def status_url(operation_id: str, slug: str) -> str:
    return url_of(operation_id, slug=slug)


def page_set_active(client: ApiClient, slug: str, active: bool) -> Callable[[], httpx.Response]:
    """A ação pela página, como o navegador: entra, lê o token e manda o formulário da situação."""

    def call() -> httpx.Response:
        assert login(client).status_code == 303
        data = {"csrf_token": csrf_token(client), "confirm": "on"}
        action = "activate" if active else "deactivate"
        return client.post(f"{PAGE}/forms/{slug}/{action}", data=data, headers=ORIGIN)

    return call


def api_set_active(client: ApiClient, slug: str, active: bool) -> Callable[[], httpx.Response]:
    operation_id = "activate_form" if active else "deactivate_form"
    return lambda: client.post(status_url(operation_id, slug), json={"confirm": True})


@pytest.mark.parametrize("operation_id", ACTIONS)
def test_changing_the_status_of_a_form_needs_a_key(
    deps: Deps, world: None, operation_id: str
) -> None:
    response = make_api(deps, FakeClock()).post(
        status_url(operation_id, "demo"), json={"confirm": True}
    )

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"


@pytest.mark.parametrize("operation_id", ACTIONS)
def test_a_read_key_cannot_change_the_status_of_a_form(
    api_client: Client, deps: Deps, world: None, operation_id: str
) -> None:
    response = api_client("leitura").post(status_url(operation_id, "demo"), json={"confirm": True})

    assert response.status_code == 403 and response.json()["code"] == "forbidden_role"
    assert by_a_key(deps, "form.deactivate") == [] and by_a_key(deps, "form.activate") == []


@pytest.mark.parametrize(
    ("operation_id", "slug"), [("deactivate_form", "demo"), ("activate_form", "mcc")]
)
def test_changing_the_status_without_confirm_is_a_422_and_nothing_happens(
    api_client: Client, deps: Deps, world: None, operation_id: str, slug: str
) -> None:
    client = api_client("total")
    before = client.get(form_url(slug)).json()["active"]

    for sent in ({}, {"confirm": False}, {"confirm": "true"}, None):
        refused = client.post(status_url(operation_id, slug), json=sent)

        assert refused.status_code == 422, sent
        assert refused.json()["code"] == "confirmation_required"
        assert "Nada foi feito" in refused.json()["detail"]
    assert client.get(form_url(slug)).json()["active"] is before
    assert by_a_key(deps, "form.deactivate") == [] and by_a_key(deps, "form.activate") == []


def test_the_confirmation_of_each_status_change_says_what_that_change_does(
    api_client: Client, world: None
) -> None:
    client = api_client("total")

    off = client.post(status_url("deactivate_form", "demo"), json={}).json()["detail"]
    on = client.post(status_url("activate_form", "mcc"), json={}).json()["detail"]

    assert "tira o formulário do ar para as leads" in off and "indisponível" in off
    assert "volta a receber envios" in on and "tira" not in on


def test_deactivating_takes_the_form_off_the_air_for_the_leads(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    assert client.get(f"/p/{SLUG}").status_code == 200

    response = client.post(status_url("deactivate_form", SLUG), json={"confirm": True})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == DETAIL_FIELDS and body["slug"] == SLUG and body["active"] is False
    page = client.get(f"/p/{SLUG}")
    assert page.status_code == 410 and "Este formulário não está disponível" in text(page.text)
    refused = client.post(
        f"/v1/forms/{SLUG}/submissions",
        json={"answers": {}, "consent": {"privacy": True, "text_version": "x"}},
    )
    assert refused.status_code == 410 and refused.json()["code"] == "form_unavailable"
    assert client.get(form_url(SLUG)).json()["active"] is False
    (entry,) = by_a_key(deps, "form.deactivate")
    assert (entry.actor, entry.target_type, entry.details) == (
        client.actor,
        "form",
        {"form_slug": SLUG},
    )


def test_activating_puts_the_form_back_on_the_air(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    assert client.get("/p/mcc").status_code == 410  # o mcc está desativado

    response = client.post(status_url("activate_form", "mcc"), json={"confirm": True})

    assert response.status_code == 200 and response.json()["active"] is True
    assert client.get("/p/mcc").status_code == 200
    (entry,) = by_a_key(deps, "form.activate")
    assert (entry.actor, entry.details) == (client.actor, {"form_slug": "mcc"})


@pytest.mark.parametrize(
    ("operation_id", "slug"), [("activate_form", "demo"), ("deactivate_form", "mcc")]
)
def test_repeating_a_status_change_is_a_409_and_is_not_audited_again(
    api_client: Client, deps: Deps, world: None, operation_id: str, slug: str
) -> None:
    response = api_client("total").post(status_url(operation_id, slug), json={"confirm": True})

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "Nada mudou" in response.json()["detail"]
    assert by_a_key(deps, "form.deactivate") == [] and by_a_key(deps, "form.activate") == []


@pytest.mark.parametrize("operation_id", ACTIONS)
def test_changing_the_status_of_a_form_that_does_not_exist_is_a_404(
    api_client: Client, world: None, operation_id: str
) -> None:
    response = api_client("total").post(
        status_url(operation_id, "nao-existe"), json={"confirm": True}
    )

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert "nao-existe" not in response.text


@pytest.mark.parametrize("operation_id", ACTIONS)
def test_an_unknown_field_in_the_status_change_is_a_422_on_that_field(
    api_client: Client, world: None, operation_id: str
) -> None:
    response = api_client("total").post(
        status_url(operation_id, "demo"), json={"confirm": True, "motivo": "teste"}
    )

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert [item["field"] for item in response.json()["errors"]] == ["motivo"]


@pytest.mark.parametrize("active", [False, True])
def test_changing_the_status_leaves_the_same_state_and_audit_as_the_page(
    api_client: Client, world: None, active: bool
) -> None:
    client = api_client("total")
    slug = "mcc" if active else "demo"  # o mcc está desativado; o demo, ativo

    page, api = assert_same_effect(
        page_set_active(client, slug, active),
        api_set_active(client, slug, active),
        api_actor=client.actor,
        ignore=CALLER_USE,
    )

    assert page.status_code == 303 and api.status_code == 200
    assert api.json()["active"] is active


def test_the_status_change_that_changes_nothing_is_the_same_on_the_page_and_in_the_api(
    api_client: Client, world: None
) -> None:
    client = api_client("total")

    page, api = assert_same_effect(
        page_set_active(client, "demo", True),  # o demo já está ativo
        api_set_active(client, "demo", True),
        api_actor=client.actor,
        ignore=CALLER_USE,
        expect_no_change=True,
    )

    assert page.status_code == 303 and api.status_code == 409


# --- get_form_retention e save_form_retention ---------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    """O relógio do app no dia de hoje: os pedidos do teste nascem agora, e a contagem do que a
    retenção apagaria olha o relógio do app."""
    current = FakeClock()
    current.now = utcnow().timestamp()
    return current


def retention_url(slug: str = SLUG) -> str:
    return url_of("get_form_retention", slug=slug)


def save_url(slug: str = SLUG) -> str:
    return url_of("save_form_retention", slug=slug)


def periods(deps: Deps, slug: str = SLUG) -> tuple[int | None, int | None]:
    """Os prazos próprios do formulário no banco: (PDF, pedido); None é o padrão do ambiente."""
    with deps.sessions() as session:
        row = session.execute(
            sa.select(Form.retention_pdf_days, Form.retention_days).where(Form.slug == slug)
        ).one()
    return row[0], row[1]


def save(
    client: ApiClient,
    pdf: Any,
    request: Any,
    *,
    expected: str = ":",
    slug: str = SLUG,
    confirm: bool = True,
    **more: Any,
) -> httpx.Response:
    body = {"pdf_days": pdf, "request_days": request, "expected_version": expected, **more}
    if confirm:
        body["confirm"] = True
    return client.put(save_url(slug), json=body)


def page_save_retention(
    client: ApiClient,
    pdf: str,
    request: str,
    *,
    expected: str = ":",
    seen: str = "",
    confirm: bool = False,
    slug: str = SLUG,
) -> Callable[[], httpx.Response]:
    """A ação pela página, como o navegador: entra, lê o token e manda o formulário dos prazos."""

    def call() -> httpx.Response:
        assert login(client).status_code == 303
        data = {
            "csrf_token": csrf_token(client),
            "pdf_days": pdf,
            "request_days": request,
            "expected": expected,
        }
        if seen:
            data["seen"] = seen
        if confirm:
            data["confirm"] = "on"
        return client.post(f"{PAGE}/forms/{slug}/retention", data=data, headers=ORIGIN)

    return call


def retention_audit(deps: Deps) -> list[AuditLog]:
    return by_a_key(deps, "form.retention")


def old_requests(deps: Deps) -> None:
    """Dois pedidos do formulário com 70 dias, um com 10, e um do demo com 65 (que o padrão de 60 dias
    apaga, mas não conta para o formulário): reduzir o prazo do pedido para 60 apaga 2."""
    for key, slug, days in (
        ("a1", SLUG, 70),
        ("a2", SLUG, 70),
        ("a3", SLUG, 10),
        ("d1", "demo", 65),
    ):
        aged(deps, order(deps, slug, key), days)


def test_get_form_retention_needs_a_key(deps: Deps, world: None) -> None:
    response = make_api(deps, FakeClock()).get(retention_url())

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"


def test_get_form_retention_gives_the_periods_the_defaults_the_limits_and_the_version(
    api_client: Client, deps: Deps, world: None
) -> None:
    set_periods(deps, SLUG, pdf=45, request=90)
    settings = deps.settings

    response = api_client("leitura").get(retention_url())

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    environment = {"pdf_days": settings.retention_pdf_days, "request_days": settings.retention_days}
    assert response.json() == {
        "slug": SLUG,
        "title": "Projeto de demonstração",
        "own": {"pdf_days": 45, "request_days": 90},
        "default": environment,
        "in_force": {"pdf_days": 45, "request_days": 90},
        "version": "45:90",
        "link_ttl_days": settings.link_ttl_days,
        "max_days": 3650,
    }


def test_get_form_retention_of_a_form_without_its_own_periods_follows_the_environment(
    api_client: Client, deps: Deps, world: None
) -> None:
    body = api_client("leitura").get(retention_url("mcc")).json()

    assert body["own"] == {"pdf_days": None, "request_days": None} and body["version"] == ":"
    assert body["in_force"] == body["default"]


def test_get_form_retention_of_a_form_that_does_not_exist_is_a_404(
    api_client: Client, world: None
) -> None:
    response = api_client("leitura").get(retention_url("nao-existe"))

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert "nao-existe" not in response.text


def test_get_form_retention_shows_the_same_periods_as_the_page(
    api_client: Client, deps: Deps, world: None
) -> None:
    set_periods(deps, SLUG, pdf=45, request=90)
    client = api_client("leitura")
    assert login(client).status_code == 303

    page = text(client.get(f"{PAGE}/forms/{SLUG}/retention").text)
    body = client.get(retention_url()).json()

    assert f"{body['in_force']['pdf_days']} dias depois de gerado" in page
    assert f"{body['in_force']['request_days']} dias depois da última mudança" in page
    assert page.count("prazo deste formulário") == 2 and body["own"]["pdf_days"] == 45


def test_a_read_key_reads_the_retention_but_cannot_save_it(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("leitura")

    assert client.get(retention_url()).status_code == 200
    refused = save(client, 50, 120)

    assert refused.status_code == 403 and refused.json()["code"] == "forbidden_role"
    assert periods(deps) == (None, None) and retention_audit(deps) == []


def test_saving_the_retention_needs_a_key(deps: Deps, world: None) -> None:
    response = make_api(deps, FakeClock()).put(
        save_url(), json={"pdf_days": 50, "request_days": 120, "expected_version": ":"}
    )

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"


def test_saving_the_retention_without_confirm_is_a_422_and_nothing_is_saved(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    body = {"pdf_days": 50, "request_days": 120, "expected_version": ":"}

    for extra in ({}, {"confirm": False}, {"confirm": "true"}):
        refused = client.put(save_url(), json={**body, **extra})

        assert refused.status_code == 422, extra
        assert refused.json()["code"] == "confirmation_required"
        assert "apaga dado de lead" in refused.json()["detail"]
        assert "Nada foi feito" in refused.json()["detail"]
    assert periods(deps) == (None, None) and retention_audit(deps) == []


def test_raising_the_periods_saves_them_and_audits_the_change(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")

    response = save(client, 50, 120)

    assert response.status_code == 200 and periods(deps) == (50, 120)
    body = response.json()
    assert body["own"] == {"pdf_days": 50, "request_days": 120} and body["version"] == "50:120"
    assert body == client.get(retention_url()).json()  # o estado novo, o mesmo da leitura
    (entry,) = retention_audit(deps)
    assert (entry.actor, entry.target_type) == (client.actor, "form")
    assert entry.details["form_slug"] == SLUG
    assert (entry.details["previous_pdf_days"], entry.details["pdf_days"]) == (None, 50)
    assert "confirmed_requests" not in entry.details  # só uma redução leva a contagem confirmada


def test_a_blank_period_goes_back_to_the_environment_default(
    api_client: Client, deps: Deps, world: None
) -> None:
    set_periods(deps, SLUG, pdf=50, request=120)
    client = api_client("total")
    assert client.get(retention_url()).json()["version"] == "50:120"

    # Voltar ao padrão (30 e 60 dias) reduz os prazos: a recusa traz a contagem, e ela é confirmada.
    asked = save(client, None, None, expected="50:120")
    assert asked.status_code == 422 and asked.json()["code"] == "confirmation_required"
    seen = re.search(r'"seen": "(\d+:\d+)"', asked.json()["detail"])
    assert seen is not None

    response = save(client, None, None, expected="50:120", seen=seen.group(1))

    assert response.status_code == 200 and periods(deps) == (None, None)
    assert response.json()["own"] == {"pdf_days": None, "request_days": None}


def test_saving_the_same_periods_changes_nothing_and_audits_nothing(
    api_client: Client, deps: Deps, world: None
) -> None:
    set_periods(deps, SLUG, pdf=50, request=120)

    response = save(api_client("total"), 50, 120, expected="50:120")

    assert response.status_code == 200 and response.json()["version"] == "50:120"
    assert periods(deps) == (50, 120) and retention_audit(deps) == []


def test_a_reduction_without_the_count_asks_for_it_and_saves_nothing(
    api_client: Client, deps: Deps, world: None, clock: FakeClock
) -> None:
    old_requests(deps)
    set_periods(deps, SLUG, pdf=45, request=90)
    client = api_client("total", clock=clock)

    asked = save(client, 45, 60, expected="45:90")

    assert asked.status_code == 422 and asked.json()["code"] == "confirmation_required"
    detail = asked.json()["detail"]
    assert "A próxima retenção vai apagar 2 pedidos inteiros." in detail  # os do demo não contam
    assert '"seen": "2:0"' in detail and "Nada foi gravado" in detail
    assert periods(deps) == (45, 90) and retention_audit(deps) == []


def test_a_reduction_with_the_count_it_asked_for_saves_and_audits_the_count(
    api_client: Client, deps: Deps, world: None, clock: FakeClock
) -> None:
    old_requests(deps)
    set_periods(deps, SLUG, pdf=45, request=90)
    client = api_client("total", clock=clock)

    response = save(client, 45, 60, expected="45:90", seen="2:0")

    assert response.status_code == 200 and periods(deps) == (45, 60)
    (entry,) = retention_audit(deps)
    assert (entry.details["confirmed_requests"], entry.details["confirmed_pdfs"]) == (2, 0)
    assert (entry.details["previous_request_days"], entry.details["request_days"]) == (90, 60)
    report = retention.purge_expired(deps)
    assert report.requests_deleted == 3  # os dois da contagem, mais o do demo (o padrão de 60 dias)


def test_a_count_that_grew_since_it_was_read_asks_again(
    api_client: Client, deps: Deps, world: None, clock: FakeClock
) -> None:
    for key in ("a1", "a2"):
        aged(deps, order(deps, SLUG, key), 70)
    recent = order(deps, SLUG, "a3")  # ainda não venceu
    set_periods(deps, SLUG, pdf=45, request=90)
    client = api_client("total", clock=clock)
    first = save(client, 45, 60, expected="45:90")
    assert '"seen": "2:0"' in first.json()["detail"]
    aged(deps, recent, 70)  # outro pedido vence enquanto a pessoa decide

    again = save(client, 45, 60, expected="45:90", seen="2:0")

    assert again.status_code == 422 and again.json()["code"] == "confirmation_required"
    assert (
        "A contagem mudou" in again.json()["detail"] and '"seen": "3:0"' in again.json()["detail"]
    )
    assert periods(deps) == (45, 90)
    assert save(client, 45, 60, expected="45:90", seen="3:0").status_code == 200


def test_reducing_the_pdf_period_counts_the_files_of_the_requests_that_stay(
    api_client: Client, deps: Deps, world: None, clock: FakeClock
) -> None:
    set_periods(deps, SLUG, pdf=45, request=90)
    aged(deps, order(deps, SLUG, "a1", delivered=True), 40)
    aged(deps, order(deps, SLUG, "a2", delivered=True), 20)
    client = api_client("total", clock=clock)

    asked = save(client, 35, 90, expected="45:90")

    assert "vai apagar 1 arquivo de PDF dos pedidos que ficam" in asked.json()["detail"]
    assert '"seen": "0:1"' in asked.json()["detail"]
    assert save(client, 35, 90, expected="45:90", seen="0:1").status_code == 200


def test_a_reduction_that_deletes_nothing_still_asks_for_the_count(
    api_client: Client, deps: Deps, world: None, clock: FakeClock
) -> None:
    set_periods(deps, SLUG, pdf=45, request=90)
    client = api_client("total", clock=clock)

    asked = save(client, 45, 80, expected="45:90")

    assert asked.status_code == 422 and asked.json()["code"] == "confirmation_required"
    assert "não vai apagar nada deste formulário" in asked.json()["detail"]
    assert '"seen": "0:0"' in asked.json()["detail"] and periods(deps) == (45, 90)
    assert save(client, 45, 80, expected="45:90", seen="0:0").status_code == 200


def test_a_confirmation_that_is_not_a_count_is_a_422_on_the_seen_field(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")

    for seen in ("2", "a:b", "2:", ":3", "-1:0", "1:2:3:4", " ", "9" * 40):
        refused = save(client, 50, 120, seen=seen)

        assert refused.status_code == 422, seen
        assert refused.json()["code"] == "validation_failed"
        assert [item["field"] for item in refused.json()["errors"]] == ["seen"]
    assert periods(deps) == (None, None)


def test_a_period_the_rule_refuses_is_a_422_on_the_field_with_the_message_of_the_page(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")
    assert login(client).status_code == 303

    refused = save(client, 5, 120)
    page = page_save_retention(client, "5", "120")()

    assert refused.status_code == 422 and refused.json()["code"] == "validation_failed"
    (item,) = refused.json()["errors"]
    assert item["field"] == "pdf_days" and item["message"]
    assert item["message"] in text(page.text) and page.status_code == 422
    assert periods(deps) == (None, None) and retention_audit(deps) == []


def test_the_pdf_period_cannot_be_shorter_than_the_lead_link_plus_the_margin(
    api_client: Client, deps: Deps, world: None
) -> None:
    minimum = deps.settings.link_ttl_days + RETENTION_PDF_MARGIN_DAYS
    client = api_client("total")

    below = save(client, minimum - 1, 120)
    exact = save(client, minimum, 120)

    assert below.status_code == 422 and exact.status_code == 200
    assert periods(deps) == (minimum, 120)


@pytest.mark.parametrize("days", [0, -1, 3651, 1.5, "30", "trinta", True])
def test_a_period_out_of_range_or_not_a_number_is_a_422_on_the_field(
    api_client: Client, deps: Deps, world: None, days: Any
) -> None:
    client = api_client("total")

    for field, body in (("pdf_days", (days, 120)), ("request_days", (50, days))):
        refused = save(client, *body)

        assert refused.status_code == 422, (field, days)
        assert refused.json()["code"] == "validation_failed"
        assert [item["field"] for item in refused.json()["errors"]] == [field]
    assert periods(deps) == (None, None)


def test_both_periods_are_required_and_null_is_the_default(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")

    missing = client.put(
        save_url(), json={"request_days": 120, "expected_version": ":", "confirm": True}
    )
    unknown = client.put(
        save_url(),
        json={
            "pdf_days": 50,
            "request_days": 120,
            "expected_version": ":",
            "confirm": True,
            "x": 1,
        },
    )

    assert missing.status_code == 422
    assert [item["field"] for item in missing.json()["errors"]] == ["pdf_days"]
    assert unknown.status_code == 422
    assert [item["field"] for item in unknown.json()["errors"]] == ["x"]
    assert periods(deps) == (None, None)


def test_the_limits_of_a_period_are_the_same_on_the_page_and_in_the_api(
    api_client: Client, deps: Deps, world: None
) -> None:
    """Os extremos valem (ou não) igual nos dois: o prazo é de 1 a 3650 dias, e o do PDF ainda não
    pode ser menor que a validade do link da lead mais a folga."""
    client = api_client("total")
    for days in (0, 1, 30, 31, 3650, 3651):
        set_periods(deps, SLUG)
        by_api = save(client, days, 3650).status_code
        set_periods(deps, SLUG)
        by_page = page_save_retention(client, str(days), "3650")().status_code

        assert (by_api == 422) == (by_page == 422), days


def test_a_stale_version_is_a_409_with_the_version_of_now_and_nothing_is_saved(
    api_client: Client, deps: Deps, world: None
) -> None:
    set_periods(deps, SLUG, pdf=45, request=90)

    response = save(api_client("total"), 50, 120, expected=":")

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "45:90"
    assert periods(deps) == (45, 90) and retention_audit(deps) == []


def test_saving_the_retention_of_a_form_that_does_not_exist_is_a_404(
    api_client: Client, world: None
) -> None:
    response = save(api_client("total"), 50, 120, slug="nao-existe")

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert "nao-existe" not in response.text


def test_saving_the_retention_leaves_the_same_state_and_audit_as_the_page(
    api_client: Client, deps: Deps, world: None
) -> None:
    client = api_client("total")

    page, api = assert_same_effect(
        page_save_retention(client, "50", "120"),
        lambda: save(client, 50, 120),
        api_actor=client.actor,
        ignore=CALLER_USE,
    )

    assert page.status_code == 303 and api.status_code == 200


def test_a_confirmed_reduction_leaves_the_same_state_and_audit_as_the_page(
    api_client: Client, deps: Deps, world: None, clock: FakeClock
) -> None:
    old_requests(deps)
    set_periods(deps, SLUG, pdf=45, request=90)
    client = api_client("total", clock=clock)

    page, api = assert_same_effect(
        page_save_retention(client, "45", "60", expected="45:90", seen="2:0", confirm=True),
        lambda: save(client, 45, 60, expected="45:90", seen="2:0"),
        api_actor=client.actor,
        ignore=CALLER_USE,
    )

    assert page.status_code == 303 and api.status_code == 200


def test_a_reduction_nobody_confirmed_leaves_the_same_state_on_the_page_and_in_the_api(
    api_client: Client, deps: Deps, world: None, clock: FakeClock
) -> None:
    old_requests(deps)
    set_periods(deps, SLUG, pdf=45, request=90)
    client = api_client("total", clock=clock)

    page, api = assert_same_effect(
        page_save_retention(client, "45", "60", expected="45:90"),
        lambda: save(client, 45, 60, expected="45:90"),
        api_actor=client.actor,
        ignore=CALLER_USE,
        expect_no_change=True,
    )

    assert page.status_code == 422 and api.status_code == 422


# --- get_form_version ---------------------------------------------------------------------------

VERSION_FIELDS = {
    "slug",
    "number",
    "published_by",
    "published_at",
    "note",
    "fingerprint",
    "definition",
}


def version_url(number: Any, slug: str = SLUG) -> str:
    return url_of("get_form_version", slug=slug, number=str(number))


def export_path(number: int, slug: str = SLUG) -> str:
    return f"{PAGE}/forms/{slug}/versions/{number}/export"


def stored_definition(deps: Deps, number: int, slug: str = SLUG) -> dict[str, Any]:
    with deps.sessions() as session:
        found = stored.stored_version(session, slug, number)
    assert found is not None
    return found.definition


def test_get_form_version_needs_a_key(deps: Deps, world: None) -> None:
    response = make_api(deps, FakeClock()).get(version_url(1))

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"


def test_get_form_version_gives_the_definition_exactly_as_the_database_keeps_it(
    api_client: Client, deps: Deps, world: None
) -> None:
    response = api_client("leitura").get(version_url(1))

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == VERSION_FIELDS
    assert (body["slug"], body["number"], body["note"]) == (SLUG, 1, "Importação da definição.")
    assert body["published_by"] == "testes" and body["published_at"].endswith("Z")
    assert body["definition"] == stored_definition(deps, 1) == json.loads(DEFINITION)
    second = api_client("leitura").get(version_url(2)).json()
    assert second["definition"]["intro"] == NEWER and second["number"] == 2
    assert body["fingerprint"] == fingerprint(body["definition"])


def test_get_form_version_keeps_what_the_database_keeps_even_if_it_no_longer_compiles(
    api_client: Client, deps: Deps, world: None
) -> None:
    """A definição não passa pelo modelo do compilador na resposta: um campo que o formato de hoje
    não conhece, ou um valor que ele recusa, sai como está."""
    response = api_client("leitura").get(version_url(2, BROKEN))

    assert response.status_code == 200
    assert response.json()["definition"]["minutes"] == 0  # o compilador de hoje a recusa
    assert response.json()["definition"] == stored_definition(deps, 2, BROKEN)


def test_get_form_version_gives_the_same_definition_as_the_page_export(
    api_client: Client, world: None
) -> None:
    client = api_client("leitura")
    assert login(client).status_code == 303

    for slug, number in ((SLUG, 1), (SLUG, 2), (BROKEN, 2), ("mcc", 1), ("reforma-2027", 1)):
        by_api = client.get(version_url(number, slug)).json()["definition"]
        by_page = json.loads(client.get(export_path(number, slug)).content)

        assert by_api == by_page, (slug, number)


def test_the_definition_of_get_form_version_imports_in_an_empty_database_as_the_same_form(
    api_client: Client, deps: Deps, world: None
) -> None:
    body = api_client("leitura").get(version_url(1)).json()
    original = stored_definition(deps, 1)
    with deps.sessions.begin() as session:  # outro ambiente: um banco sem o formulário
        session.execute(sa.text(f"TRUNCATE {TABLES} CASCADE"))

    stored.import_definition(deps, json.dumps(body["definition"]), actor="testes")

    imported = stored_definition(deps, 1)
    assert imported == original == body["definition"]
    with deps.sessions() as session:
        again = stored.stored_version(session, SLUG, 1)
    assert again is not None and again.fingerprint == body["fingerprint"]


def test_get_form_version_of_a_version_that_does_not_exist_is_a_404(
    api_client: Client, world: None
) -> None:
    client = api_client("leitura")

    for number, slug in ((3, SLUG), (1, "nao-existe"), (1, SCRATCH)):  # SCRATCH não tem versão
        response = client.get(version_url(number, slug))

        assert response.status_code == 404 and response.json()["code"] == "not_found", (
            number,
            slug,
        )
        assert "nao-existe" not in response.text


@pytest.mark.parametrize("number", ["0", "-1", "um", "1.5", "1" * 10])
def test_get_form_version_of_a_number_that_is_not_one_is_a_422_on_the_number(
    api_client: Client, world: None, number: str
) -> None:
    response = api_client("leitura").get(version_url(number))

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert [item["field"] for item in response.json()["errors"]] == ["number"]


def test_the_contract_documents_the_definition_with_the_form_document() -> None:
    schemas = built_openapi()["components"]["schemas"]

    assert {
        branch["$ref"].split("/")[-1]
        for branch in schemas["FormVersion"]["properties"]["definition"]["anyOf"]
    } == {"PdfFormDocument", "CaptureFormDocument"}
    assert "definition" in schemas["FormVersion"]["required"]
    required = schemas["QuestionDoc"]["properties"]["required"]["description"]
    assert "show_when" in required and "required_when_shown" in required
    assert "só é exigida quando a pergunta aparece" in required
    assert schemas["PdfFormDocument"]["properties"]["generation"]  # o modelo diz o que é


# --- compare_form_versions ----------------------------------------------------------------------


def compare_url(slug: str = SLUG) -> str:
    return url_of("compare_form_versions", slug=slug)


def changed_keys(body: dict[str, Any]) -> list[str]:
    return [section["key"] for section in body["sections"] if section["changed"]]


def test_compare_form_versions_needs_a_key(deps: Deps, world: None) -> None:
    response = make_api(deps, FakeClock()).get(compare_url())

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"


def test_compare_form_versions_reads_two_versions_section_by_section(
    api_client: Client, world: None
) -> None:
    response = api_client("leitura").get(compare_url(), params={"from": "1", "to": "2"})

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {
        "slug",
        "from",
        "to",
        "versions",
        "draft_pending",
        "changed_sections",
        "sections",
    }
    assert body["slug"] == SLUG and body["from"] == {"kind": "version", "number": 1}
    assert body["to"] == {"kind": "version", "number": 2}
    assert body["versions"] == [2, 1] and body["draft_pending"] is True
    assert changed_keys(body) == ["pagina"] and body["changed_sections"] == 1
    page_section = next(item for item in body["sections"] if item["key"] == "pagina")
    assert page_section["title"] == "Página pública: título, introdução e tempo"
    lines = [(line["kind"], line["text"][1:].strip()) for line in page_section["lines"]]
    assert ("add", f'"intro": "{NEWER}",') in lines
    assert ("del", f'"intro": "{json.loads(DEFINITION)["intro"]}",') in lines
    assert any(line["kind"] == "hunk" for line in page_section["lines"])
    unchanged = [s for s in body["sections"] if not s["changed"]]
    assert unchanged and all(section["lines"] == [] for section in unchanged)
    assert len(body["sections"]) == 19 and body["sections"][-1]["key"] == "outras"
    completion = next(item for item in body["sections"] if item["key"] == "conclusao")
    assert completion["title"] == "Conclusão após salvar as respostas"
    assert completion["changed"] is False


def test_compare_form_versions_compares_in_the_order_given(api_client: Client, world: None) -> None:
    client = api_client("leitura")

    forward = client.get(compare_url(), params={"from": "1", "to": "2"}).json()
    backward = client.get(compare_url(), params={"from": "2", "to": "1"}).json()

    def removed_and_added(body: dict[str, Any]) -> tuple[str, str]:
        (page_section,) = [s for s in body["sections"] if s["key"] == "pagina"]
        (gone,) = [
            x["text"] for x in page_section["lines"] if x["kind"] == "del" and "intro" in x["text"]
        ]
        (new,) = [
            x["text"] for x in page_section["lines"] if x["kind"] == "add" and "intro" in x["text"]
        ]
        return gone, new

    assert NEWER in removed_and_added(forward)[1] and NEWER in removed_and_added(backward)[0]
    assert (backward["from"]["number"], backward["to"]["number"]) == (2, 1)


def test_compare_form_versions_compares_a_version_with_the_draft(
    api_client: Client, world: None
) -> None:
    response = api_client("leitura").get(compare_url(), params={"from": "2", "to": "draft"})

    body = response.json()
    assert response.status_code == 200 and body["to"] == {"kind": "draft", "number": None}
    assert changed_keys(body) == ["pagina"]
    page_section = next(item for item in body["sections"] if item["key"] == "pagina")
    assert any(OTHER in line["text"] for line in page_section["lines"])


def test_compare_form_versions_without_a_choice_meets_the_draft_when_there_is_one_pending(
    api_client: Client, world: None
) -> None:
    body = api_client("leitura").get(compare_url()).json()

    assert (body["from"], body["to"]) == (
        {"kind": "version", "number": 2},
        {"kind": "draft", "number": None},
    )
    assert body["draft_pending"] is True and changed_keys(body) == ["pagina"]


def test_compare_form_versions_without_a_choice_and_without_a_draft_compares_the_last_two(
    api_client: Client, world: None
) -> None:
    body = api_client("leitura").get(compare_url(BROKEN)).json()

    assert (body["from"]["number"], body["to"]["number"]) == (1, 2)
    assert body["draft_pending"] is False and changed_keys(body) == ["pagina"]  # minutes: 0


def test_compare_form_versions_one_side_chosen_and_the_other_left_to_the_default(
    api_client: Client, world: None
) -> None:
    client = api_client("leitura")

    only_from = client.get(compare_url(), params={"from": "1"}).json()
    only_to = client.get(compare_url(), params={"to": "1"}).json()

    assert (only_from["from"]["number"], only_from["to"]["kind"]) == (1, "draft")
    assert (only_to["from"]["number"], only_to["to"]["number"]) == (2, 1)


def test_the_default_choice_looks_at_the_pending_draft_not_at_a_fingerprint(
    api_client: Client, deps: Deps, world: None
) -> None:
    """Um rascunho que só escreve por extenso um padrão do formato tem outra impressão digital, mas diz
    o mesmo que a versão atual: não está pendente, e a escolha padrão é a das duas últimas versões."""
    document_v2 = document(intro=NEWER)
    document_v2["steps"][0]["questions"][0]["required"] = True  # o padrão do formato, por extenso
    with deps.sessions.begin() as session:
        draft = session.get(FormDraft, session.scalar(sa.select(Form.id).where(Form.slug == SLUG)))
        assert draft is not None
        draft.definition = document_v2
        draft.fingerprint = fingerprint(document_v2)

    body = api_client("leitura").get(compare_url()).json()

    assert body["draft_pending"] is False
    assert (body["from"]["number"], body["to"]["number"]) == (1, 2)


def test_the_comparison_reads_the_normal_form_so_it_differs_from_get_form_version(
    api_client: Client, deps: Deps, world: None
) -> None:
    """O padrão do formato escrito por extenso não conta como mudança, e uma condição (`show_when`)
    sai na frase, e não nas regras: por isso a comparação não é a diferença entre as duas
    definições que `get_form_version` devolve."""
    third = document(slug=SLUG)
    third["steps"][0]["questions"][1]["required"] = True  # o padrão, por extenso
    question = third["steps"][1]["questions"][1]
    question["show_when"] = {
        "mode": "all",
        "rules": [{"field": "tamanho_equipe", "operator": "equals", "value": "2_a_5"}],
    }
    store_version(deps, 3, {**third, "intro": NEWER})
    client = api_client("leitura")

    comparison = client.get(compare_url(), params={"from": "2", "to": "3"}).json()
    raw = client.get(version_url(3)).json()["definition"]

    assert raw["steps"][0]["questions"][1]["required"] is True  # a definição guarda por extenso
    assert changed_keys(comparison) == ["etapas"]  # e a comparação só vê a condição
    lines = [
        line["text"] for s in comparison["sections"] if s["key"] == "etapas" for line in s["lines"]
    ]
    assert any(
        "Aparece quando" in line and "De 2 a 5 pessoas" in line for line in lines
    )  # a frase em português
    assert not any('"operator"' in line or '"required": true' in line for line in lines)


def test_compare_form_versions_of_a_form_that_does_not_exist_is_a_404(
    api_client: Client, world: None
) -> None:
    response = api_client("leitura").get(compare_url("nao-existe"))

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert "nao-existe" not in response.text


def test_a_form_without_a_published_version_has_nothing_to_compare(
    api_client: Client, world: None
) -> None:
    response = api_client("leitura").get(compare_url(SCRATCH))

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert "ainda não tem versão publicada" in response.json()["detail"]


def test_a_side_that_is_not_a_published_version_is_a_404(api_client: Client, world: None) -> None:
    client = api_client("leitura")

    for params in ({"from": "7"}, {"to": "7"}, {"from": "0"}):
        response = client.get(compare_url(), params=params)

        assert response.status_code == 404 and response.json()["code"] == "not_found", params
        assert "7" not in response.json()["detail"]


@pytest.mark.parametrize("side", ["", "abc", "1.5", "-1", "rascunho", "1" * 10, "draft "])
def test_a_side_that_is_neither_a_number_nor_the_draft_is_a_422_on_that_field(
    api_client: Client, world: None, side: str
) -> None:
    client = api_client("leitura")

    for field in ("from", "to"):
        response = client.get(compare_url(), params={field: side})

        assert response.status_code == 422, (field, side)
        assert response.json()["code"] == "validation_failed"
        assert [item["field"] for item in response.json()["errors"]] == [field]


def test_compare_form_versions_shows_the_same_changes_as_the_page(
    api_client: Client, world: None
) -> None:
    client = api_client("leitura")
    assert login(client).status_code == 303

    for old, new in (("1", "2"), ("2", "1"), ("1", "draft"), ("2", "draft"), (None, None)):
        params = {k: v for k, v in (("from", old), ("to", new)) if v is not None}
        on_the_page = {
            "to": "rascunho" if new == "draft" else new,
            "from": "rascunho" if old == "draft" else old,
        }
        page = client.get(
            f"{PAGE}/forms/{SLUG}/versions/compare",
            params={k: v for k, v in on_the_page.items() if v is not None},
        )
        body = client.get(compare_url(), params=params).json()

        boxes = re.findall(r'<section class="box" id="([a-z-]+)"', page.text)
        assert [key for key in boxes if key != "sem-mudanca"] == changed_keys(body), (old, new)
        shown = text(page.text)
        for section in body["sections"]:
            for line in section["lines"]:
                if line["kind"] in ("add", "del"):
                    assert " ".join(line["text"].split()) in shown, (old, new, line)


# --- os papéis ----------------------------------------------------------------------------------


def test_a_read_key_reads_every_read_of_the_forms_and_none_of_the_writes(
    api_client: Client, world: None
) -> None:
    client = api_client("leitura")
    body = {"confirm": True}

    reads = [
        client.get(url_of("list_forms")),
        client.get(url_of("get_form", slug=SLUG)),
        client.get(url_of("get_form_retention", slug=SLUG)),
        client.get(url_of("get_form_version", slug=SLUG, number="1")),
        client.get(url_of("compare_form_versions", slug=SLUG)),
    ]
    writes = [
        client.post(url_of("activate_form", slug="mcc"), json=body),
        client.post(url_of("deactivate_form", slug=SLUG), json=body),
        save(client, 50, 120),
    ]

    assert [response.status_code for response in reads] == [200] * 5
    assert [response.status_code for response in writes] == [403] * 3
    assert {response.json()["code"] for response in writes} == {"forbidden_role"}


# --- o eco por operação -------------------------------------------------------------------------

SENTINEL = "ZZ_SENT_" + secrets.token_hex(8).upper()


def echoed(response: httpx.Response) -> bool:
    return SENTINEL in response.text or any(
        SENTINEL in value for value in response.headers.values()
    )


def test_the_free_text_of_the_retention_is_never_echoed_by_the_operation(
    api_client: Client, deps: Deps, world: None
) -> None:
    """O eco geral do contrato para no `422` do Pydantic, antes de o serviço rodar. Aqui os outros
    campos são válidos e só o texto livre leva o sentinela: a versão esperada chega ao serviço (o
    `409`), e a contagem confirmada chega à conferência do formato (o `422`)."""
    client = api_client("total")

    stale = save(client, 50, 120, expected=SENTINEL)
    count = save(client, 50, 120, seen=SENTINEL)

    assert stale.status_code == 409 and count.status_code == 422
    assert not echoed(stale) and not echoed(count)
    assert periods(deps) == (None, None)


def test_the_slug_and_the_sides_of_the_forms_are_never_echoed_by_the_operations(
    api_client: Client, world: None
) -> None:
    client = api_client("total")
    body = {"confirm": True}

    responses = [
        client.get(url_of("get_form", slug=SENTINEL)),
        client.get(url_of("get_form_retention", slug=SENTINEL)),
        client.get(url_of("get_form_version", slug=SENTINEL, number="1")),
        client.get(url_of("compare_form_versions", slug=SENTINEL)),
        client.get(compare_url(), params={"from": SENTINEL}),
        client.get(compare_url(), params={"to": SENTINEL}),
        client.post(url_of("activate_form", slug=SENTINEL), json=body),
        client.post(url_of("deactivate_form", slug=SENTINEL), json=body),
        save(client, 50, 120, slug=SENTINEL),
    ]

    assert [response.status_code for response in responses] == [404] * 4 + [422] * 2 + [404] * 3
    assert not any(echoed(response) for response in responses)
