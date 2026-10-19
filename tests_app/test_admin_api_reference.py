"""A referência da API de admin (ADR 0015, "A referência Scalar"): `GET /admin/api/v1/reference` (a
página) e `GET /admin/api/v1/openapi.json` (o arquivo que ela lê), servidas sem nenhum terceiro.

Pela costura HTTP, com o app inteiro. O que se prova:

- o bundle do Scalar está versionado em `app/web/static/vendor/`, com a licença e o sha256 no
  `NOTICE.txt`, e o sha confere com o arquivo;
- o acesso: abertas em `development` e `test`; em produção, sessão do admin ou chave de qualquer papel
  (página `303` ao login com o `next`, JSON `401`); e nenhuma operação da API fica aberta em ambiente
  nenhum;
- a página não tem script nem estilo embutido, não aponta para outra origem, carrega o bundle por
  `static_url` e configura o Scalar sem proxy, telemetria nem fontes de fora;
- a política de conteúdo é própria da página, e a das outras páginas do admin não mudou;
- o JSON servido é o `docs/api/openapi.json`;
- as duas rotas ficam fora do próprio esquema, e o `/docs` e o `/openapi.json` do app principal
  seguem desligados em produção.

As chaves são sorteadas na execução, nunca literais. O que só um navegador mostra (que o Scalar roda
sob essa política e que nenhuma requisição sai da origem) não cabe na suíte: está no relatório do PR.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from admin_api_support import API, ORIGIN, admin_api, bearer, make_api, new_key
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from test_admin import FakeClock, login

from app.admin import routes as admin_routes
from app.admin.api import keys
from app.admin.api.application import create_admin_api
from app.admin.api.operations import OpenRoute
from app.admin.auth import COOKIE_NAME, instant
from app.deps import Deps
from app.web.templating import STATIC_DIR, static_url

ROOT = Path(__file__).resolve().parent.parent
OPENAPI_FILE = ROOT / "docs" / "api" / "openapi.json"
NOTICE = STATIC_DIR / "NOTICE.txt"
VENDOR = STATIC_DIR / "vendor"
REFERENCE = f"{API}/reference"
OPENAPI = f"{API}/openapi.json"
OPEN_ENVIRONMENTS = ("development", "test")
# A política de conteúdo das páginas do admin hoje (`app.admin.routes.CSP`), escrita por extenso: se
# a da referência vazar para as outras, ou a delas mudar sem querer, o teste mostra.
ADMIN_PAGES_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; "
    "base-uri 'none'; object-src 'none'"
)


def scalar_bundles() -> list[Path]:
    return sorted(VENDOR.glob("scalar-*.js"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def production(deps: Deps, clock: FakeClock) -> TestClient:
    """O app em produção: a referência só abre com sessão do admin ou com chave."""
    return make_api(deps, clock, APP_ENV="production")


def in_environment(deps: Deps, clock: FakeClock, environment: str) -> TestClient:
    return make_api(deps, clock, APP_ENV=environment)


def wrong_key() -> str:
    """Uma chave bem formada que o banco não conhece."""
    return keys.format_key(secrets.token_hex(4), secrets.token_urlsafe(32))


def logged_in(client: TestClient) -> TestClient:
    assert login(client).status_code == 303
    return client


# --- o bundle vendorizado ------------------------------------------------------------------------


def test_there_is_exactly_one_scalar_bundle_and_its_name_carries_the_version() -> None:
    (bundle,) = scalar_bundles()

    assert re.fullmatch(r"scalar-\d+\.\d+\.\d+\.js", bundle.name)
    assert bundle.stat().st_size > 1_000_000  # o arquivo único do navegador, não um esboço


def test_the_notice_has_the_version_the_source_the_license_and_the_sha256_of_the_bundle() -> None:
    (bundle,) = scalar_bundles()
    version = bundle.stem.removeprefix("scalar-")
    notice = NOTICE.read_text(encoding="utf-8")

    line = next(item for item in notice.splitlines() if item.startswith("Scalar API Reference"))
    assert version in line
    assert f"https://registry.npmjs.org/@scalar/api-reference/-/api-reference-{version}.tgz" in line
    assert "dist/browser/standalone.js" in line
    assert "MIT" in line
    sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert f"sha256 {sha}" in line, "o sha256 do NOTICE.txt não é o do arquivo vendorizado"
    assert "Permission is hereby granted, free of charge" in notice  # o texto da licença MIT


def test_the_htmx_line_of_the_notice_is_still_there() -> None:
    notice = NOTICE.read_text(encoding="utf-8")

    assert "htmx 2.0.11" in notice
    assert not (NOTICE.parent / "fonts" / "montserrat-latin-600-800.woff2").exists()


# --- o acesso: por ambiente e por credencial -----------------------------------------------------


@pytest.mark.parametrize("environment", OPEN_ENVIRONMENTS)
def test_in_development_and_test_both_routes_are_open(
    deps: Deps, clock: FakeClock, environment: str
) -> None:
    client = in_environment(deps, clock, environment)

    page = client.get(REFERENCE)
    document = client.get(OPENAPI)

    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert document.status_code == 200 and document.json()["info"]["title"]


def test_in_production_without_a_credential_the_page_goes_to_the_login_and_the_json_is_a_401(
    production: TestClient,
) -> None:
    page = production.get(REFERENCE)
    document = production.get(OPENAPI)

    assert page.status_code == 303 and page.headers["cache-control"] == "no-store"
    target = urlsplit(page.headers["location"])
    assert target.path == "/admin/login"
    assert parse_qs(target.query) == {"next": [REFERENCE]}  # volta à referência depois do login
    assert document.status_code == 401
    assert document.json()["code"] == "invalid_credentials"
    assert document.headers["WWW-Authenticate"] == "Bearer"
    assert document.headers["cache-control"] == "no-store"


def test_after_the_login_the_operator_lands_on_the_reference(production: TestClient) -> None:
    sent_to = production.get(REFERENCE).headers["location"]
    next_path = parse_qs(urlsplit(sent_to).query)["next"][0]

    assert production.get(sent_to).status_code == 200  # a página de login
    back = login(production, next=next_path)
    assert back.status_code == 303 and back.headers["location"] == REFERENCE
    page = production.get(back.headers["location"])

    assert page.status_code == 200 and "api-reference" in page.text


def test_in_production_a_session_of_the_admin_opens_both_routes(production: TestClient) -> None:
    logged_in(production)

    page = production.get(REFERENCE)
    document = production.get(OPENAPI)

    assert page.status_code == 200 and document.status_code == 200
    assert document.json()["info"]["title"]


def test_reading_the_reference_does_not_renew_the_session(production: TestClient) -> None:
    """A sessão é do painel: quem a renova são as páginas dele. Ler a documentação não mexe no cookie."""
    logged_in(production)

    assert "set-cookie" not in production.get(REFERENCE).headers
    assert "set-cookie" not in production.get(OPENAPI).headers


@pytest.mark.parametrize("role", ["leitura", "total"])
def test_in_production_a_key_of_any_role_opens_both_routes(
    deps: Deps, clock: FakeClock, production: TestClient, role: str
) -> None:
    created = new_key(deps, clock, role)

    page = production.get(REFERENCE, headers=bearer(created.key))
    document = production.get(OPENAPI, headers=bearer(created.key))

    assert page.status_code == 200 and "api-reference" in page.text
    assert document.status_code == 200
    assert not production.cookies  # a chave não abre sessão nenhuma


@pytest.mark.parametrize("path", [REFERENCE, OPENAPI])
def test_in_production_a_key_that_does_not_hold_is_refused_for_the_reason_it_deserves(
    deps: Deps, clock: FakeClock, production: TestClient, path: str
) -> None:
    """Mandou o cabeçalho e ele não vale: é `401` em JSON nas duas rotas (a página só redireciona ao
    login quando não veio credencial nenhuma, que é o caso de quem abre no navegador)."""
    expired = new_key(deps, clock, "leitura", expires_at=instant(clock) + timedelta(days=1))
    revoked = new_key(deps, clock, "leitura")
    with deps.sessions.begin() as session:
        keys.revoke(session, revoked.info.key_id, actor="testes", now=instant(clock))
    clock.advance(2 * 86_400)

    reasons = {
        wrong_key(): "invalid_credentials",
        expired.key: "key_expired",
        revoked.key: "key_revoked",
    }
    for key, code in reasons.items():
        response = production.get(path, headers=bearer(key))
        assert response.status_code == 401, key
        assert response.json()["code"] == code
        assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("path", [REFERENCE, OPENAPI])
def test_in_production_a_cookie_that_is_not_a_live_session_opens_nothing(
    clock: FakeClock, production: TestClient, path: str
) -> None:
    refused = 303 if path == REFERENCE else 401  # a página vai ao login; o JSON é um 401

    forged = production.get(path, headers={"Cookie": f"{COOKIE_NAME}=isto-nao-e-uma-sessao"})
    assert forged.status_code == refused

    logged_in(production)
    assert production.get(path).status_code == 200
    clock.advance(9 * 3600)  # passou da ociosidade de 8 h
    assert production.get(path).status_code == refused


def test_wrong_keys_on_the_reference_lock_the_ip_as_they_do_on_the_operations(
    deps: Deps, clock: FakeClock, production: TestClient
) -> None:
    good = new_key(deps, clock, "leitura")

    answers = [production.get(OPENAPI, headers=bearer(wrong_key())).status_code for _ in range(5)]

    assert answers == [401, 401, 401, 401, 429]  # a 5ª falha seguida bloqueia
    blocked = production.get(OPENAPI, headers=bearer(good.key))
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    assert production.get(f"{API}/me", headers=bearer(good.key)).status_code == 429
    # O contador é da API: o login do painel segue aberto para quem está no mesmo escritório.
    assert login(production).status_code == 303


@pytest.mark.parametrize("environment", ["development", "test", "production"])
def test_no_operation_is_open_in_any_environment_and_the_session_does_not_open_one(
    deps: Deps, clock: FakeClock, environment: str
) -> None:
    client = logged_in(in_environment(deps, clock, environment))
    paths = admin_api(client).openapi()["paths"]
    operations = [(method, path) for path, methods in paths.items() for method in methods]
    assert len(operations) >= 4  # as das chaves, ao menos

    for method, path in operations:
        url = API + path.replace("{key_id}", "0" * 8)
        response = client.request(method, url, json={}, headers=ORIGIN)
        assert response.status_code == 401, f"{method.upper()} {path} respondeu sem chave"
        assert response.json()["code"] == "invalid_credentials"


@pytest.mark.parametrize("path", [REFERENCE, OPENAPI])
def test_only_get_is_served(deps: Deps, clock: FakeClock, path: str) -> None:
    client = in_environment(deps, clock, "development")

    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)(path, headers=ORIGIN)
        assert response.status_code == 405 and response.headers["Allow"] == "GET"
        assert response.json()["code"] == "method_not_allowed"


def test_by_default_the_sub_application_is_closed_and_only_a_key_opens_it(
    deps: Deps, clock: FakeClock
) -> None:
    """Quem monta a sub-aplicação sem dizer o ambiente nem a sessão não abre nada por esquecimento."""
    closed = TestClient(create_admin_api(clock=clock), follow_redirects=False)
    opened = TestClient(create_admin_api(clock=clock, environment="development"))
    created = new_key(deps, clock, "leitura")

    assert closed.get("/openapi.json").status_code == 401
    assert closed.get("/reference").status_code == 303
    assert closed.get("/openapi.json", headers=bearer(created.key)).status_code == 200
    assert opened.get("/openapi.json").status_code == 200


# --- a página ------------------------------------------------------------------------------------


class Page(HTMLParser):
    """A página como o navegador a lê: as marcas com os atributos e o texto de cada `<script>`."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.scripts: list[dict[str, Any]] = []
        self._open: dict[str, Any] | None = None
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self._open = {"attrs": dict(attrs), "text": ""}
            self.scripts.append(self._open)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._open = None

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self._open["text"] += data

    def one(self, tag: str, **attrs: str) -> dict[str, str | None]:
        found = [
            found_attrs
            for name, found_attrs in self.tags
            if name == tag and all(found_attrs.get(k) == v for k, v in attrs.items())
        ]
        assert len(found) == 1, f"esperava uma <{tag} {attrs}>, achei {len(found)}"
        return found[0]


@pytest.fixture
def page(deps: Deps, clock: FakeClock) -> Page:
    response = in_environment(deps, clock, "test").get(REFERENCE)
    assert response.status_code == 200
    return Page(response.text)


def test_the_page_has_no_inline_script_style_or_handler(page: Page) -> None:
    for script in page.scripts:
        if script["attrs"].get("src"):
            assert script["text"].strip() == ""
        else:
            # O elemento que o Scalar lê (`data-url` e `data-configuration`) não leva código.
            assert script["attrs"].get("id") == "api-reference" and script["text"].strip() == ""
    tags = [name for name, _ in page.tags]
    assert "style" not in tags and "iframe" not in tags and "object" not in tags
    for name, attrs in page.tags:
        assert "style" not in attrs, f"<{name}> com atributo style"
        assert not [key for key in attrs if key.startswith("on")], f"<{name}> com manipulador"


def test_the_page_points_only_to_its_own_origin(deps: Deps, clock: FakeClock) -> None:
    html = in_environment(deps, clock, "test").get(REFERENCE).text
    page = Page(html)

    assert not re.search(r"https?://|(?<![\w:])//", html), "a página cita outra origem"
    for name, attrs in page.tags:
        for key in ("src", "href", "action", "data-url", "poster", "srcset"):
            value = attrs.get(key)
            if value is None:
                continue
            parts = urlsplit(value)
            assert not parts.scheme and not parts.netloc, f"<{name} {key}={value!r}> sai da origem"
            assert value.startswith("/") and not value.startswith("//"), value


def test_the_bundle_comes_in_by_static_url_and_is_the_vendored_file(
    deps: Deps, clock: FakeClock, page: Page
) -> None:
    (bundle,) = scalar_bundles()
    sources = [s["attrs"]["src"] for s in page.scripts if s["attrs"].get("src")]

    assert sources == [static_url(f"vendor/{bundle.name}")]
    served = in_environment(deps, clock, "test").get(sources[0])
    assert served.status_code == 200
    assert served.headers["content-type"].split(";")[0] in (
        "text/javascript",
        "application/javascript",
    )
    assert (
        hashlib.sha256(served.content).hexdigest()
        == hashlib.sha256(bundle.read_bytes()).hexdigest()
    )


def test_the_page_reads_the_openapi_of_the_api_from_its_own_origin(page: Page) -> None:
    holder = page.one("script", id="api-reference")

    assert holder["data-url"] == OPENAPI
    assert json.loads(holder["data-configuration"] or "")["url"] == OPENAPI


def test_the_scalar_configuration_turns_off_what_reaches_out(page: Page) -> None:
    configuration = json.loads(page.one("script", id="api-reference")["data-configuration"] or "")

    assert configuration["proxyUrl"] == ""  # sem o proxy do Scalar
    assert configuration["telemetry"] is False
    assert configuration["withDefaultFonts"] is False  # nada de Google Fonts
    assert configuration["hideClientButton"] is True  # o botão que abre o client.scalar.com
    assert configuration["agent"] == {"disabled": True}
    assert configuration["mcp"] == {"disabled": True}
    assert configuration["persistAuth"] is False  # a chave digitada não vai para o localStorage
    assert configuration["showDeveloperTools"] == "never"
    assert configuration["hideTestRequestButton"] is False  # testar uma operação segue possível
    for value in configuration.values():
        assert not re.search(r"https?://", json.dumps(value)), "a configuração cita outra origem"


def test_the_page_is_a_document_a_screen_reader_can_name(page: Page) -> None:
    assert page.one("html")["lang"] == "pt-BR"
    assert page.one("meta", name="viewport")["content"]
    assert page.one("meta", name="robots")["content"] == "noindex, nofollow, noarchive"


# --- a política de conteúdo ----------------------------------------------------------------------


def policy_of(response: httpx.Response) -> dict[str, list[str]]:
    directives = [part.strip() for part in response.headers["content-security-policy"].split(";")]
    return {name: sources for name, *sources in (part.split() for part in directives if part)}


def test_the_page_has_a_content_policy_of_its_own_with_no_external_origin(
    deps: Deps, clock: FakeClock
) -> None:
    csp = policy_of(in_environment(deps, clock, "test").get(REFERENCE))

    assert csp["script-src"] == ["'self'"]  # nenhum script embutido, de fora nem eval
    assert csp["connect-src"] == ["'self'"]
    assert csp["default-src"] == ["'self'"]
    assert csp["img-src"] == ["'self'", "data:"] and csp["font-src"] == ["'self'"]
    assert csp["frame-ancestors"] == ["'none'"]
    assert csp["base-uri"] == ["'none'"] and csp["object-src"] == ["'none'"]
    assert csp["form-action"] == ["'none'"]  # a página não tem formulário
    for directive, sources in csp.items():
        for source in sources:
            assert not re.match(r"(https?:|wss?:|\*)", source), f"{directive} abre {source}"
        assert "'unsafe-eval'" not in sources
    assert "'unsafe-inline'" not in csp["script-src"]


def test_the_style_policy_opens_only_what_the_bundle_was_measured_to_ask_for(
    deps: Deps, clock: FakeClock
) -> None:
    """Num navegador, o Scalar 1.74.0 injeta quatro `<style>` (só o maior aceitaria um nonce;
    os três das notificações e do tema não) e põe `style` em elementos. Por isso o `style-src` é o
    único mais largo que o das outras páginas, com `'unsafe-inline'` e nenhuma origem a mais. Se uma
    versão nova do bundle pedir menos (ou mais), esta é a política a rever: ver o texto de
    `app.admin.api.reference`."""
    csp = policy_of(in_environment(deps, clock, "test").get(REFERENCE))

    assert csp["style-src"] == ["'self'", "'unsafe-inline'"]
    # Em tudo o mais a política é a das outras páginas, menos o `form-action`, que aqui é mais estrito.
    different = {key for key, value in policy_of_admin_pages().items() if csp.get(key) != value}
    assert different == {"style-src", "form-action"}


def policy_of_admin_pages() -> dict[str, list[str]]:
    directives = [part.strip() for part in ADMIN_PAGES_CSP.split(";")]
    return {name: sources for name, *sources in (part.split() for part in directives if part)}


def test_the_page_keeps_the_privacy_headers_of_the_admin(deps: Deps, clock: FakeClock) -> None:
    headers = in_environment(deps, clock, "test").get(REFERENCE).headers

    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert headers["cross-origin-opener-policy"] == "same-origin"


def test_the_other_admin_pages_keep_the_current_policy(production: TestClient) -> None:
    assert production.get("/admin/login").headers["content-security-policy"] == ADMIN_PAGES_CSP
    logged_in(production)
    for path in ("/admin", "/admin/api-keys"):
        csp = production.get(path).headers["content-security-policy"]
        assert csp == ADMIN_PAGES_CSP == admin_routes.CSP, path


# --- o JSON --------------------------------------------------------------------------------------


def test_the_json_served_is_the_versioned_openapi_file(deps: Deps, clock: FakeClock) -> None:
    response = in_environment(deps, clock, "test").get(OPENAPI)

    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == "application/json"
    assert response.text == OPENAPI_FILE.read_text(encoding="utf-8")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_json_served_in_production_is_the_same_file(
    deps: Deps, clock: FakeClock, production: TestClient
) -> None:
    created = new_key(deps, clock, "leitura")

    response = production.get(OPENAPI, headers=bearer(created.key))

    assert response.text == OPENAPI_FILE.read_text(encoding="utf-8")


# --- as duas rotas ficam fora do esquema ---------------------------------------------------------


def test_the_two_routes_are_open_routes_and_stay_out_of_the_schema(
    deps: Deps, clock: FakeClock
) -> None:
    client = in_environment(deps, clock, "test")
    api = admin_api(client)

    document = api.openapi()
    assert "/reference" not in document["paths"] and "/openapi.json" not in document["paths"]
    open_paths = {
        context.path
        for context in iter_route_contexts(api.routes)
        if isinstance(context.route, OpenRoute)
    }
    assert open_paths == {"/reference", "/openapi.json"}
    # O contrato versionado não os conhece, e o arquivo é o mesmo que a referência mostra.
    versioned = json.loads(OPENAPI_FILE.read_text(encoding="utf-8"))
    assert set(versioned["paths"]) == set(document["paths"])


@pytest.mark.parametrize("environment", ["development", "test", "production"])
def test_the_automatic_docs_of_the_sub_application_stay_off(
    deps: Deps, clock: FakeClock, environment: str
) -> None:
    client = in_environment(deps, clock, environment)

    key = new_key(deps, clock, "total")

    for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        assert client.get(f"{API}{path}").status_code == 404, path
        assert client.get(f"{API}{path}", headers=bearer(key.key)).status_code == 404, path


def test_in_production_the_docs_and_the_openapi_of_the_main_app_stay_off(
    deps: Deps, clock: FakeClock, production: TestClient
) -> None:
    created = new_key(deps, clock, "total")
    logged_in(production)

    for path in ("/docs", "/redoc", "/openapi.json"):
        assert production.get(path).status_code == 404, path
        assert production.get(path, headers=bearer(created.key)).status_code == 404, path


# --- a página das chaves leva à referência -------------------------------------------------------


def test_the_keys_page_links_to_the_reference(deps: Deps, clock: FakeClock) -> None:
    client = logged_in(in_environment(deps, clock, "test"))

    html = client.get("/admin/api-keys").text

    Page(html).one("a", href=REFERENCE)  # exatamente um link para a referência
    assert "referência da API" in html
