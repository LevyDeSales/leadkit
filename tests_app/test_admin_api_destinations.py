"""Os destinos do webhook na API de admin (`/admin/api/v1/destinations`): `list_destinations`,
`get_destination`, `save_destination`, `rotate_destination_secret`, `remove_destination` e
`send_destination_test_event`, pela costura HTTP.

São uma família só, com o escopo `{scope}` (a palavra `default` ou o endereço do formulário). Agem pelas
mesmas ações e pela mesma consulta que as páginas `/admin/destinations/...`
(`app.admin.actions.destinations`, `app.admin.queries.destinations`): as conferências, a decisão do clique
duplo e a auditoria são uma só, e a paridade com a página (o mesmo estado do banco e a mesma auditoria,
menos o autor) está no fim de cada operação que escreve. O receptor é o `Receiver` do `conftest`, com
HTTP de verdade em 127.0.0.1. As chaves destes testes são sorteadas na execução.

**O segredo só sai na criação e na troca.** Cada teste que lê ou escreve confere que ele não está na
resposta de uma leitura, no log, na auditoria nem numa mensagem de erro; e o caminho da URL, que costuma
valer como senha, não chega a uma chave `leitura`.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_support import ORIGIN, ApiClient, assert_same_effect, make_api, snapshot, url_of
from conftest import Receiver
from cryptography.fernet import Fernet
from test_admin import FakeClock, csrf_token, login, section
from test_admin_destinations import shown_secret, stored_text
from test_intake import submit
from test_retention import LEAD_TRACES

from app import destinations
from app.admin.actions import destinations as actions
from app.admin.api.schemas.destinations import RotatedDestination, SavedDestination
from app.deps import Deps
from app.destinations import SecretCipher
from app.models import AuditLog

PAGE = "/admin/destinations"
SENTINEL = "ZZ_SENT_" + secrets.token_hex(8).upper()
# O caminho de uma URL de webhook costuma valer como senha: sorteado, nunca literal.
PATH_SECRET = "caminho-" + secrets.token_hex(8)
LEGACY = ["demo", "mcc", "reforma-2027"]
Client = Callable[..., ApiClient]


def hook(host: str = "hooks.example.test", path: str = PATH_SECRET) -> str:
    return f"https://{host}/{path}"


def store(
    deps: Deps, form_slug: str | None, url: str | None = None, *, actor: str = "operadora"
) -> str:
    """Cria o destino como o admin cria e devolve o segredo gerado (mostrado uma vez)."""
    with deps.sessions.begin() as session:
        saved = destinations.save(
            session,
            deps.cipher,
            form_slug=form_slug,
            url=url or hook(),
            actor=actor,
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="internal",
        )
    assert saved.secret is not None
    return saved.secret


def read_one(client: ApiClient, scope: str) -> httpx.Response:
    return client.get(url_of("get_destination", scope=scope))


def read_all(client: ApiClient, **query: object) -> httpx.Response:
    return client.get(url_of("list_destinations"), params=query)  # type: ignore[arg-type]


def scopes(body: dict[str, object]) -> list[str]:
    return [item["scope"] for item in body["items"]]  # type: ignore[index, union-attr]


# --- list_destinations --------------------------------------------------------------------------


def test_list_destinations_needs_a_key_and_a_read_key_is_enough(
    deps: Deps, api_client: Client
) -> None:
    anonymous = make_api(deps, FakeClock()).get(url_of("list_destinations"))

    reader = read_all(api_client("leitura"))

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 200 and reader.headers["cache-control"] == "no-store"


def test_list_destinations_with_none_gives_the_default_first_and_every_form_unserved(
    api_client: Client,
) -> None:
    response = read_all(api_client("leitura"))

    body = response.json()
    assert response.status_code == 200 and body["next_cursor"] is None
    assert scopes(body) == ["default", *LEGACY]
    default, *forms = body["items"]
    assert default == {
        "scope": "default",
        "title": None,
        "stored": False,
        "version": "0",
        "serving": "none",
        "serving_host": None,
        "secret_unreadable": False,
    }
    assert all(item["title"] for item in forms)  # o título do registro, e não um rótulo de tela
    assert {(item["stored"], item["serving"], item["serving_host"]) for item in forms} == {
        (False, "none", None)
    }
    assert body["unserved"] == LEGACY and body["unreadable"] == []


def test_list_destinations_says_who_serves_each_form_and_who_is_still_unserved(
    deps: Deps, api_client: Client
) -> None:
    store(deps, None, hook("padrao.example.test:8443"))
    store(deps, "mcc", hook("mcc.example.test"))

    body = read_all(api_client("leitura")).json()

    items = {item["scope"]: item for item in body["items"]}
    assert (items["default"]["stored"], items["default"]["serving"]) == (True, "default")
    assert items["default"]["serving_host"] == "padrao.example.test:8443"
    assert (items["mcc"]["stored"], items["mcc"]["serving"], items["mcc"]["version"]) == (
        True,
        "form",
        "1",
    )
    assert items["mcc"]["serving_host"] == "mcc.example.test"
    for slug in ("demo", "reforma-2027"):
        assert (items[slug]["stored"], items[slug]["serving"], items[slug]["version"]) == (
            False,
            "default",
            "0",
        )
        assert items[slug]["serving_host"] == "padrao.example.test:8443"
    assert body["unserved"] == []


def test_list_destinations_lists_a_destination_kept_for_a_form_that_left_the_code(
    deps: Deps, api_client: Client
) -> None:
    store(deps, "formulario-antigo")

    body = read_all(api_client("leitura")).json()

    assert scopes(body) == ["default", *LEGACY, "formulario-antigo"]
    old = body["items"][-1]
    assert (old["title"], old["stored"], old["serving"]) == (None, True, "form")


def test_list_destinations_names_the_scopes_whose_secret_does_not_open(
    deps: Deps, api_client: Client
) -> None:
    store(deps, None)
    store(deps, "mcc")
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))  # a chave mudou sem a antiga
    store(deps, "demo")  # este, cifrado com a nova, abre

    body = read_all(api_client("leitura")).json()

    assert sorted(body["unreadable"]) == ["default", "mcc"]
    flagged = {item["scope"] for item in body["items"] if item["secret_unreadable"]}
    assert flagged == {"default", "mcc"}


def test_list_destinations_is_an_envelope_paged_by_scope_with_the_warnings_on_every_page(
    deps: Deps, api_client: Client
) -> None:
    client = api_client("leitura")
    first = read_all(client, limit=2).json()
    second = read_all(client, limit=2, cursor=first["next_cursor"]).json()

    assert scopes(first) == ["default", "demo"] and first["next_cursor"]
    assert scopes(second) == ["mcc", "reforma-2027"] and second["next_cursor"] is None
    for page in (first, second):
        assert page["unserved"] == LEGACY  # inteiras, qualquer que seja a página


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 201}, {"cursor": "!!"}])
def test_list_destinations_refuses_a_page_that_makes_no_sense(
    api_client: Client, query: dict[str, object]
) -> None:
    response = read_all(api_client("leitura"), **query)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert response.json()["errors"][0]["field"] in query


def test_list_destinations_never_gives_the_url_path_nor_the_secret_to_any_role(
    deps: Deps, api_client: Client
) -> None:
    secret = store(deps, None)
    store(deps, "mcc", hook(path=PATH_SECRET + "-mcc"))

    for role in ("leitura", "total"):
        text = read_all(api_client(role)).text
        assert PATH_SECRET not in text and secret not in text


def test_list_destinations_writes_nothing(deps: Deps, api_client: Client) -> None:
    store(deps, None)
    client = api_client("leitura")
    before = snapshot()

    read_all(client)
    read_all(client)

    after = snapshot()
    # a chamada grava o último uso da chave, e só isso (ler não audita)
    assert {t: rows for t, rows in after.items() if t != "api_keys"} == {
        t: rows for t, rows in before.items() if t != "api_keys"
    }


def test_list_destinations_warns_of_what_the_page_warns_of_for_the_same_data(
    deps: Deps, api_client: Client
) -> None:
    """Paridade de leitura: a página e a API avisam dos mesmos formulários sem destino e dos mesmos
    segredos que não abrem."""
    client = api_client("leitura")
    assert login(client).status_code == 303
    store(deps, "mcc")
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))

    page = client.get(PAGE)
    body = read_all(client).json()

    assert page.status_code == 200
    assert body["unserved"] == ["demo", "reforma-2027"] and body["unreadable"] == ["mcc"]
    unserved = section(page.text, "sem-destino")
    assert all(slug in unserved for slug in body["unserved"]) and "mcc" not in unserved
    assert "mcc" in section(page.text, "segredo-ilegivel")
    forms = section(page.text, "formularios")
    for item in body["items"][1:]:
        assert item["title"] in forms and item["scope"] in forms


# --- get_destination ----------------------------------------------------------------------------


def test_get_destination_needs_a_key_and_a_read_key_is_enough(
    deps: Deps, api_client: Client
) -> None:
    anonymous = make_api(deps, FakeClock()).get(url_of("get_destination", scope="default"))

    reader = read_one(api_client("leitura"), "default")

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 200 and reader.headers["cache-control"] == "no-store"


def test_get_destination_of_a_scope_that_does_not_exist_is_a_404_that_echoes_nothing(
    api_client: Client,
) -> None:
    response = read_one(api_client("leitura"), SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


def test_get_destination_of_the_default_with_none_says_to_create_it(api_client: Client) -> None:
    response = read_one(api_client("leitura"), "default")

    assert response.status_code == 200
    assert response.json() == {
        "scope": "default",
        "title": None,
        "version": "0",
        "destination": None,
        "serving": "none",
        "serving_host": None,
        "serving_events": [],
        "serving_purpose": None,
        "capture_delivery_counts": {"pending": 0, "succeeded": 0, "failed": 0},
    }


def test_get_destination_gives_the_total_key_the_whole_url_and_never_the_secret(
    deps: Deps, api_client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    secret = store(deps, None, hook("padrao.example.test:8443"))

    with caplog.at_level(logging.DEBUG):
        response = read_one(api_client("total"), "default")

    assert response.status_code == 200
    body = response.json()
    destination = body["destination"]
    assert destination["url"] == hook("padrao.example.test:8443")
    assert (destination["url_redacted"], destination["host"]) == (
        False,
        "padrao.example.test:8443",
    )
    assert (destination["scope"], destination["version"]) == ("default", "1")
    assert (body["version"], body["serving"], body["serving_host"]) == (
        "1",
        "default",
        "padrao.example.test:8443",
    )
    assert destination["created_by"] == destination["updated_by"] == "operadora"
    for stamp in ("created_at", "updated_at", "secret_created_at"):
        assert destination[stamp].endswith("Z")
    assert destination["secret_unreadable"] is False
    assert not {"secret", "secret_sealed"} & set(destination)
    for place in (response.text, caplog.text):
        assert secret not in place


def test_get_destination_gives_the_read_key_only_the_scheme_and_the_host(
    deps: Deps, api_client: Client
) -> None:
    secret = store(deps, None, hook("padrao.example.test:8443"))

    response = read_one(api_client("leitura"), "default")

    destination = response.json()["destination"]
    assert destination["url"] == "https://padrao.example.test:8443"
    assert (destination["url_redacted"], destination["host"]) == (
        True,
        "padrao.example.test:8443",
    )
    assert PATH_SECRET not in response.text and secret not in response.text


def test_get_destination_of_a_form_without_its_own_says_that_the_default_serves_it(
    deps: Deps, api_client: Client
) -> None:
    client = api_client("leitura")
    assert read_one(client, "mcc").json()["serving"] == "none"  # nem próprio, nem padrão

    store(deps, None, hook("padrao.example.test"))
    body = read_one(client, "mcc").json()

    assert body["scope"] == "mcc" and body["title"] and body["destination"] is None
    assert (body["version"], body["serving"], body["serving_host"]) == (
        "0",
        "default",
        "padrao.example.test",
    )


def test_get_destination_of_a_form_with_its_own_gives_that_one_and_not_the_default(
    deps: Deps, api_client: Client
) -> None:
    store(deps, None, hook("padrao.example.test"))
    store(deps, "mcc", hook("mcc.example.test"))

    mcc = read_one(api_client("total"), "mcc").json()
    default = read_one(api_client("total"), "default").json()

    assert mcc["destination"]["url"] == hook("mcc.example.test")
    assert (mcc["destination"]["scope"], mcc["serving"]) == ("mcc", "form")
    assert default["destination"]["url"] == hook("padrao.example.test")
    assert default["destination"]["scope"] == "default"


def test_get_destination_of_a_form_that_left_the_code_works_while_it_keeps_a_destination(
    deps: Deps, api_client: Client
) -> None:
    store(deps, "formulario-antigo")
    client = api_client("total")

    body = read_one(client, "formulario-antigo").json()
    assert (body["title"], body["serving"]) == (None, "form")

    with deps.sessions.begin() as session:
        destinations.remove(
            session, form_slug="formulario-antigo", actor="operadora", expected_version=1
        )
    assert read_one(client, "formulario-antigo").status_code == 404


def test_get_destination_says_when_the_secret_does_not_open(deps: Deps, api_client: Client) -> None:
    store(deps, None)
    store(deps, "mcc")
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    store(deps, "demo")
    client = api_client("leitura")

    flags = {
        scope: read_one(client, scope).json()["destination"]["secret_unreadable"]
        for scope in ("default", "mcc", "demo")
    }

    assert flags == {"default": True, "mcc": True, "demo": False}


def test_get_destination_writes_nothing(deps: Deps, api_client: Client) -> None:
    store(deps, None)
    client = api_client("leitura")
    before = snapshot()

    read_one(client, "default")
    read_one(client, "default")

    after = snapshot()
    assert {t: rows for t, rows in after.items() if t != "api_keys"} == {
        t: rows for t, rows in before.items() if t != "api_keys"
    }


def test_get_destination_shows_what_the_page_shows_for_the_same_data(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    """Paridade de leitura: a página e a API mostram o mesmo endereço, a mesma autoria e quem serve."""
    store(deps, None, receiver.url("/padrao"))
    store(deps, "mcc", receiver.url("/mcc"), actor="outra-pessoa")
    client = api_client("total")
    assert login(client).status_code == 303

    for scope, path, author in (
        ("default", f"{PAGE}/default", "operadora"),
        ("mcc", f"{PAGE}/forms/mcc", "outra-pessoa"),
    ):
        body = read_one(client, scope).json()
        page = client.get(path).text
        address = section(page, "endereco")
        assert body["destination"]["url"] in address
        assert f"{author} ·" in address and body["destination"]["created_by"] == author
    no_own = client.get(f"{PAGE}/forms/demo").text
    served_by = read_one(client, "demo").json()
    assert served_by["destination"] is None and served_by["serving_host"] == receiver.host
    assert f"usa o destino padrão ({receiver.host})" in section(no_own, "endereco")


def test_the_word_default_is_the_default_destination_and_never_a_form(
    deps: Deps, api_client: Client
) -> None:
    store(deps, "mcc", hook("mcc.example.test"))
    client = api_client("total")

    default = read_one(client, "default").json()

    assert default["scope"] == "default" and default["destination"] is None
    assert default["serving"] == "none"  # o destino do mcc não é o padrão
    assert read_one(client, "mcc").json()["destination"]["scope"] == "mcc"
    with deps.sessions() as session:
        assert list(destinations.listing(session)) == ["mcc"]  # o banco só tem o destino do mcc


# --- save_destination ---------------------------------------------------------------------------

OMIT = object()  # tira o campo do corpo
HUGE = "9" * 5000  # `int()` de tanto algarismo levanta ValueError (500, se ninguém o segura)
PAGE_USER = "pagina"  # o autor da página, diferente do `operadora` que o mundo do teste grava


def audits(deps: Deps, action: str | None = None) -> list[AuditLog]:
    with deps.sessions() as session:
        statement = sa.select(AuditLog).order_by(AuditLog.at)
        if action is not None:
            statement = statement.where(AuditLog.action == action)
        return list(session.scalars(statement))


def current(deps: Deps, form_slug: str | None) -> destinations.StoredDestination | None:
    with deps.sessions() as session:
        return destinations.listing(session).get(form_slug)


def opened(deps: Deps, form_slug: str | None) -> str:
    """O segredo guardado, aberto com a chave do ambiente: o que a entrega usaria. Só serve para o
    escopo que tem destino próprio (`resolve` cai no padrão onde o formulário não tem)."""
    with deps.sessions() as session:
        resolved = destinations.resolve(session, deps.cipher, form_slug or "demo")
    assert resolved is not None
    return resolved.secret


def save_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "url": hook(),
        "purpose": "internal",
        "expected_version": "0",
        "confirm": True,
        **changes,
    }
    return {key: value for key, value in body.items() if value is not OMIT}


def save(client: ApiClient, scope: str = "default", **changes: Any) -> httpx.Response:
    return client.put(url_of("save_destination", scope=scope), json=save_body(**changes))


def errors_of(response: httpx.Response) -> dict[str, str]:
    return {item["field"]: item["message"] for item in response.json()["errors"]}


def test_save_destination_needs_a_key_and_a_read_key_is_refused_and_nothing_happens(
    deps: Deps, api_client: Client
) -> None:
    url = url_of("save_destination", scope="default")
    anonymous = make_api(deps, FakeClock()).put(url, json=save_body())

    reader = save(api_client("leitura"))

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert current(deps, None) is None and audits(deps, "webhook_destination.create") == []
    assert PATH_SECRET not in anonymous.text + reader.text


def test_save_destination_of_a_scope_that_does_not_exist_is_a_404_that_echoes_nothing(
    deps: Deps, api_client: Client
) -> None:
    response = save(api_client("total"), scope=SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text and SENTINEL not in str(response.headers)
    with deps.sessions() as session:
        assert destinations.listing(session) == {}


def test_save_destination_creates_with_a_secret_that_shows_once_and_never_in_log_audit_or_reads(
    deps: Deps, api_client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    client = api_client("total")

    with caplog.at_level(logging.DEBUG):
        response = save(client)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    secret = body["secret"]
    assert secret and body["created"] is True
    assert (body["scope"], body["url"], body["url_redacted"]) == ("default", hook(), False)
    assert (body["version"], body["host"], body["secret_unreadable"]) == (
        "1",
        "hooks.example.test",
        False,
    )
    assert body["created_by"] == body["updated_by"] == client.actor
    assert body["created_at"].endswith("Z") and body["secret_created_at"].endswith("Z")
    assert opened(deps, None) == secret  # cifrado no banco, aberto aqui
    (entry,) = audits(deps, "webhook_destination.create")
    assert entry.actor == client.actor and entry.details["host"] == "hooks.example.test"
    for place in (caplog.text, stored_text(deps), str(entry.details)):
        assert secret not in place
    assert PATH_SECRET not in str(entry.details) and PATH_SECRET not in caplog.text
    for role in ("leitura", "total"):  # nenhuma leitura o mostra de novo
        assert secret not in read_one(api_client(role), "default").text
        assert secret not in read_all(api_client(role)).text


def test_save_destination_creates_the_destination_of_a_form_and_not_the_default(
    deps: Deps, api_client: Client
) -> None:
    response = save(api_client("total"), scope="mcc", url=hook("mcc.example.test"))

    body = response.json()
    assert response.status_code == 200 and body["created"] and body["scope"] == "mcc"
    assert current(deps, None) is None and current(deps, "mcc") is not None
    (entry,) = audits(deps, "webhook_destination.create")
    assert entry.details["form_slug"] == "mcc"


def test_save_destination_changes_the_destination_of_a_form_that_left_the_code_only_if_it_has_one(
    deps: Deps, api_client: Client
) -> None:
    client = api_client("total")
    store(deps, "formulario-antigo")

    changed = save(client, scope="formulario-antigo", expected_version="1")
    unknown = save(client, scope="outro-que-saiu")

    assert changed.status_code == 200 and changed.json()["created"] is False
    assert unknown.status_code == 404


def test_save_destination_changes_the_address_and_keeps_the_secret(
    deps: Deps, api_client: Client
) -> None:
    secret = store(deps, None, hook(path="antigo"))
    client = api_client("total")

    response = save(client, url=hook("novo.example.test"), expected_version="1")

    body = response.json()
    assert response.status_code == 200
    assert (body["created"], body["secret"], body["version"]) == (False, None, "2")
    assert body["url"] == hook("novo.example.test") and body["updated_by"] == client.actor
    assert body["created_by"] == "operadora"  # quem criou segue sendo quem criou
    assert opened(deps, None) == secret  # o segredo continua o mesmo
    (entry,) = audits(deps, "webhook_destination.update")
    assert entry.actor == client.actor
    assert entry.details["previous_host"] == "hooks.example.test"
    assert entry.details["host"] == "novo.example.test"
    assert secret not in response.text + str(entry.details) + stored_text(deps)


URLS_THAT_DO_NOT_SERVE = [
    ("ftp://hooks.example.test/planos", "https://"),
    ("hooks.example.test/planos", "https://"),
    ("https://nome-de-usuario@hooks.example.test/planos", "usuário e a senha"),
    ("https:///planos", "endereço do servidor"),
    ("https://hooks.example.test/pla nos", "espaços"),
    ("https://hooks.example.test/pla\x07nos", "caracteres de controle"),
    ("https://hooks.example.test:99999/planos", "porta"),
    ("https://hooks.example.test/planos#trecho", "#"),
    ("http://169.254.169.254/latest/meta-data", "link-local"),
    ("http://[fe80::1]/planos", "link-local"),
    ("http://[::ffff:169.254.169.254]/planos", "link-local"),
    ("https://[::1", "não é válida"),
    ("https://hooks.example.test℀/planos", "não é válida"),
    ("   ", "Informe a URL"),
]


@pytest.mark.parametrize(
    ("url", "message"),
    URLS_THAT_DO_NOT_SERVE,
    ids=[f"u{n}" for n in range(len(URLS_THAT_DO_NOT_SERVE))],
)
def test_save_destination_refuses_an_address_that_does_not_serve_on_the_field_url(
    deps: Deps, api_client: Client, url: str, message: str
) -> None:
    response = save(api_client("total"), url=url)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert message in errors_of(response)["url"]
    assert current(deps, None) is None and audits(deps, "webhook_destination.create") == []
    assert "secret" not in response.json()


def test_save_destination_refuses_an_address_over_the_limit_without_echoing_it(
    deps: Deps, api_client: Client
) -> None:
    response = save(api_client("total"), url=hook(path=SENTINEL + "a" * 2000))

    assert response.status_code == 422 and "2000" in errors_of(response)["url"]
    assert SENTINEL not in response.text and current(deps, None) is None


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"url": OMIT}, "url", "obrigatório"),
        ({"url": 7}, "url", "texto"),
        ({"expected_version": OMIT}, "expected_version", "obrigatório"),
        ({"expected_version": ""}, "expected_version", "no mínimo"),
        ({"confirm_http": "talvez"}, "confirm_http", "true ou false"),
        ({"segredo": "x" * 8}, "segredo", "desconhecido"),
        ({"secret": "x" * 8}, "secret", "desconhecido"),  # o segredo nasce no servidor
    ],
)
def test_save_destination_refuses_a_body_that_does_not_serve_with_the_path_of_the_field(
    deps: Deps, api_client: Client, changes: dict[str, Any], field: str, message: str
) -> None:
    response = save(api_client("total"), **changes)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert message in errors_of(response)[field]
    assert current(deps, None) is None


@pytest.mark.parametrize("confirm", [OMIT, False, "true", 1])
def test_save_destination_needs_confirm_true_on_every_call_and_does_nothing_without_it(
    deps: Deps, api_client: Client, confirm: Any
) -> None:
    client = api_client("total")
    created = save(client, confirm=confirm)  # criar também pede a confirmação
    store(deps, "mcc")
    before = current(deps, "mcc")
    trail = [entry.id for entry in audits(deps)]

    changed = save(client, scope="mcc", confirm=confirm, expected_version="1", url=hook("x.com"))

    for response in (created, changed):
        assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
        assert (
            "Finalidade interna" in response.json()["detail"]
            and "participante" in response.json()["detail"]
        )
    assert current(deps, None) is None and current(deps, "mcc") == before
    assert [entry.id for entry in audits(deps)] == trail  # nada gravado nem auditado


def test_save_destination_asks_for_the_confirmation_before_it_looks_at_the_body(
    api_client: Client,
) -> None:
    response = save(api_client("total"), confirm=OMIT, url="x", expected_version="")

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "errors" not in response.json()


def test_in_production_an_http_address_needs_confirm_http_and_the_audit_keeps_who_confirmed(
    deps: Deps, api_client: Client
) -> None:
    client = api_client("total", APP_ENV="production")
    http = "http://hooks.example.test/planos"

    refused = save(client, url=http)
    accepted = save(client, url=http, confirm_http=True)

    assert refused.status_code == 422 and refused.json()["code"] == "confirmation_required"
    detail = refused.json()["detail"]
    assert "confirm_http" in detail and "sem criptografia" in detail
    assert "errors" not in refused.json()
    assert accepted.status_code == 200 and accepted.json()["url"] == http
    (entry,) = audits(deps, "webhook_destination.create")
    assert entry.details["http_confirmed"] is True


def test_in_production_an_https_address_and_outside_production_an_http_one_need_nothing(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    secure = save(api_client("total", APP_ENV="production"), scope="mcc")
    plain = save(api_client("total"), url=receiver.url("/padrao"))

    assert secure.status_code == plain.status_code == 200
    assert not any("http_confirmed" in entry.details for entry in audits(deps))


def test_save_destination_without_the_encryption_key_cannot_create_but_can_still_change(
    deps: Deps, api_client: Client
) -> None:
    client = api_client("total")
    cipher = deps.cipher
    deps.cipher = None

    created = save(client)
    assert created.status_code == 409 and created.json()["code"] == "state_conflict"
    assert "WEBHOOK_ENCRYPTION_KEY" in created.json()["detail"]
    assert current(deps, None) is None

    deps.cipher = cipher  # a chave volta, cria; e some de novo
    store(deps, None)
    deps.cipher = None
    changed = save(client, url=hook("novo.example.test"), expected_version="1")
    assert changed.status_code == 200 and changed.json()["created"] is False


def test_save_destination_with_a_stale_version_is_a_409_with_the_version_of_now(
    deps: Deps, api_client: Client
) -> None:
    store(deps, "mcc", hook("antigo.example.test"))
    client = api_client("total")
    before = current(deps, "mcc")

    stale = save(client, scope="mcc", url=hook("x.example.test"), expected_version="5")
    creating = save(client, scope="mcc", url=hook("x.example.test"))  # "0": criar o que existe

    for response in (stale, creating):
        assert response.status_code == 409 and response.json()["code"] == "version_conflict"
        assert response.json()["current_version"] == "1"
    assert current(deps, "mcc") == before  # nada mudou
    assert audits(deps, "webhook_destination.update") == []


def test_save_destination_expecting_a_version_of_a_destination_that_is_gone_says_zero(
    deps: Deps, api_client: Client
) -> None:
    response = save(api_client("total"), expected_version="3")

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "0"  # a criação espera "0"
    assert "save_destination" in response.json()["detail"]
    assert current(deps, None) is None


NOT_CANONICAL = ["00", "002", "01", "-1", "1.0", "٣", "12345678901", HUGE, "abc", " 1"]


@pytest.mark.parametrize("version", NOT_CANONICAL, ids=[f"v{n}" for n in range(len(NOT_CANONICAL))])
def test_a_version_that_is_not_a_canonical_number_is_a_409_and_not_a_500(
    deps: Deps, api_client: Client, version: str
) -> None:
    store(deps, None)
    before = current(deps, None)

    response = save(api_client("total"), expected_version=version, url=hook("x.example.test"))

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "1" and current(deps, None) == before


def test_a_second_creation_by_the_same_key_is_a_409_state_conflict_that_never_repeats_the_secret(
    deps: Deps, api_client: Client
) -> None:
    client = api_client("total")
    first = save(client)  # o primeiro clique
    secret = first.json()["secret"]

    second = save(client)  # o segundo, com a leitura de antes

    assert first.status_code == 200 and second.status_code == 409
    assert second.json()["code"] == "state_conflict" and "current_version" not in second.json()
    assert "rotate_destination_secret" in second.json()["detail"]
    assert secret not in second.text and opened(deps, None) == secret  # o primeiro segredo vale
    assert len(audits(deps, "webhook_destination.create")) == 1


def test_a_creation_that_lost_to_another_author_is_a_version_conflict_not_a_repeated_click(
    deps: Deps, api_client: Client
) -> None:
    store(deps, None)  # criado por `operadora`

    response = save(api_client("total"))

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "1"


def test_a_second_address_change_with_the_old_version_is_a_version_conflict_not_a_secret_one(
    deps: Deps, api_client: Client
) -> None:
    store(deps, None)
    client = api_client("total")
    first = save(client, url=hook("a.example.test"), expected_version="1")

    second = save(client, url=hook("b.example.test"), expected_version="1")  # o clique duplo

    assert first.status_code == 200
    assert second.status_code == 409 and second.json()["code"] == "version_conflict"
    assert second.json()["current_version"] == "2"  # só a criação gera o segredo


def test_save_destination_echoes_no_free_text_it_refuses(deps: Deps, api_client: Client) -> None:
    """O eco da operação: os outros campos valem e só o texto livre (o endereço, a versão e o escopo)
    é o sentinela."""
    client = api_client("total")
    store(deps, "mcc")
    refused = [
        save(client, url=f"ftp://{SENTINEL}.example.test"),
        save(client, url=f"https://{SENTINEL}@example.test"),
        save(client, url=f"https://example.test/{SENTINEL} x"),
        save(client, url=f"https://example.test#{SENTINEL}"),
        save(client, scope="mcc", expected_version=SENTINEL),
        save(client, scope=SENTINEL),
    ]

    assert [r.status_code for r in refused] == [422, 422, 422, 422, 409, 404]
    for response in refused:
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


# --- a mesma ação, pela página e pela API -------------------------------------------------------

# Os efeitos que a API tem e a página não, ou o contrário, e o que a chamada sorteia ou carimba na
# hora: o último uso da chave, o segredo cifrado (o Fernet sorteia a cada vez; o teste confere que os
# dois abrem no segredo que cada resposta mostrou), o id do destino novo (a auditoria o cita) e as horas.
IGNORE = {
    "api_keys": {"last_used_at"},
    "webhook_destinations": {"secret_sealed", "secret_created_at"},
    "audit_log": {"target_id"},
    "*": {"created_at", "updated_at"},
}


def page_post(client: ApiClient, path: str, **form: str) -> Callable[[], httpx.Response]:
    """A ação pela página, como o navegador: entra, lê o token CSRF e manda o formulário."""

    def call() -> httpx.Response:
        assert login(client, PAGE_USER).status_code == 303
        data = {"csrf_token": csrf_token(client), "purpose": "internal", **form}
        return client.post(f"{PAGE}/{path}", data=data, headers=ORIGIN)

    return call


@pytest.mark.parametrize(
    ("scope", "path", "slug"),
    [("default", "default", None), ("mcc", "forms/mcc", "mcc")],
    ids=["default", "form"],
)
def test_creating_leaves_the_same_state_and_audit_as_the_page_in_both_scopes(
    deps: Deps, api_client: Client, scope: str, path: str, slug: str | None
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    secrets_shown: list[str] = []
    opened_secrets: list[str] = []

    def after(
        call: Callable[[], httpx.Response], shown: Callable[[httpx.Response], str]
    ) -> Callable[[], httpx.Response]:
        def run() -> httpx.Response:
            response = call()
            secrets_shown.append(shown(response))
            opened_secrets.append(opened(deps, slug or "demo"))
            return response

        return run

    page, api = assert_same_effect(
        after(
            page_post(client, f"{path}/url", url=hook(), version="0"),
            lambda response: shown_secret(response.text),
        ),
        after(lambda: save(client, scope=scope), lambda response: response.json()["secret"]),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 200 and api.status_code == 200
    assert secrets_shown[0] != secrets_shown[1]  # cada criação sorteou o seu
    assert secrets_shown == opened_secrets  # e cada uma guardou o que mostrou
    assert all(secret not in stored_text(deps) for secret in secrets_shown)


@pytest.mark.parametrize(
    ("scope", "path", "slug"),
    [("default", "default", None), ("mcc", "forms/mcc", "mcc")],
    ids=["default", "form"],
)
def test_changing_the_address_leaves_the_same_state_and_audit_as_the_page_in_both_scopes(
    deps: Deps, api_client: Client, scope: str, path: str, slug: str | None
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    secret = store(deps, slug, hook(path="antigo"))
    new = hook("novo.example.test")

    page, api = assert_same_effect(
        page_post(client, f"{path}/url", url=new, version="1"),
        lambda: save(client, scope=scope, url=new, expected_version="1"),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200
    assert opened(deps, slug or "demo") == secret  # nenhuma das duas mexeu no segredo


def test_in_production_an_http_address_leaves_the_same_state_and_audit_as_the_page(
    deps: Deps, api_client: Client
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER, APP_ENV="production")
    http = "http://hooks.example.test/planos"

    page, api = assert_same_effect(
        page_post(client, "default/url", url=http, version="0", confirm_http="on"),
        lambda: save(client, url=http, confirm_http=True),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 200 and api.status_code == 200
    (entry,) = audits(deps, "webhook_destination.create")
    assert entry.details["http_confirmed"] is True


@pytest.mark.parametrize(
    ("url", "environment"),
    [
        ("ftp://hooks.example.test/planos", {}),
        ("http://hooks.example.test/planos", {"APP_ENV": "production"}),
    ],
    ids=["bad-scheme", "http-not-confirmed"],
)
def test_an_address_that_is_refused_leaves_the_same_nothing_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client, url: str, environment: dict[str, str]
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER, **environment)

    page, api = assert_same_effect(
        page_post(client, "default/url", url=url, version="0"),
        lambda: save(client, url=url),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert page.status_code == api.status_code == 422


# --- rotate_destination_secret ------------------------------------------------------------------


def rotate_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"expected_version": "1", "confirm": True, **changes}
    return {key: value for key, value in body.items() if value is not OMIT}


def rotate(client: ApiClient, scope: str = "default", **changes: Any) -> httpx.Response:
    url = url_of("rotate_destination_secret", scope=scope)
    return client.post(url, json=rotate_body(**changes))


def test_rotate_destination_secret_needs_a_key_and_a_read_key_is_refused_and_nothing_happens(
    deps: Deps, api_client: Client
) -> None:
    secret = store(deps, None)
    url = url_of("rotate_destination_secret", scope="default")
    anonymous = make_api(deps, FakeClock()).post(url, json=rotate_body())

    reader = rotate(api_client("leitura"))

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert opened(deps, None) == secret and audits(deps, "webhook_destination.rotate_secret") == []


def test_rotate_destination_secret_of_a_scope_that_does_not_exist_is_a_404_that_echoes_nothing(
    api_client: Client,
) -> None:
    response = rotate(api_client("total"), scope=SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


def test_rotate_destination_secret_gives_a_new_secret_once_and_the_old_one_stops_at_once(
    deps: Deps, api_client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    old = store(deps, None)
    client = api_client("total")

    with caplog.at_level(logging.DEBUG):
        response = rotate(client)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    new = body["secret"]
    assert new and new != old
    assert (body["scope"], body["version"], body["url"]) == ("default", "2", hook())
    assert body["updated_by"] == client.actor and body["created_by"] == "operadora"
    assert body["secret_unreadable"] is False and body["secret_created_at"].endswith("Z")
    assert opened(deps, None) == new  # o segredo antigo já não abre nada
    (entry,) = audits(deps, "webhook_destination.rotate_secret")
    assert entry.actor == client.actor and entry.details["host"] == "hooks.example.test"
    for place in (caplog.text, stored_text(deps), str(entry.details)):
        assert new not in place and old not in place
    for role in ("leitura", "total"):  # nenhuma leitura o mostra
        assert new not in read_one(api_client(role), "default").text
        assert new not in read_all(api_client(role)).text


def test_rotate_destination_secret_changes_only_the_secret_of_its_own_scope(
    deps: Deps, api_client: Client
) -> None:
    default = store(deps, None)
    mcc = store(deps, "mcc", hook("mcc.example.test"))

    response = rotate(api_client("total"), scope="mcc")

    assert response.status_code == 200 and response.json()["scope"] == "mcc"
    assert opened(deps, None) == default and opened(deps, "mcc") != mcc


def test_rotate_destination_secret_repairs_a_secret_that_the_key_cannot_open(
    deps: Deps, api_client: Client
) -> None:
    store(deps, None)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))  # a chave mudou sem a antiga
    client = api_client("total")
    assert read_one(client, "default").json()["destination"]["secret_unreadable"] is True

    response = rotate(client)

    assert response.status_code == 200 and response.json()["secret_unreadable"] is False
    assert read_one(client, "default").json()["destination"]["secret_unreadable"] is False
    assert opened(deps, None) == response.json()["secret"]


@pytest.mark.parametrize("confirm", [OMIT, False, "true", 1])
def test_rotate_destination_secret_needs_confirm_true_and_does_nothing_without_it(
    deps: Deps, api_client: Client, confirm: Any
) -> None:
    secret = store(deps, None)
    client = api_client("total")  # criar a chave também é auditado: antes da fotografia
    before = current(deps, None)
    trail = [entry.id for entry in audits(deps)]

    response = rotate(client, confirm=confirm)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "deixar de valer na hora" in response.json()["detail"]  # o texto desta operação
    assert opened(deps, None) == secret and current(deps, None) == before
    assert [entry.id for entry in audits(deps)] == trail  # nada gravado nem auditado


def test_rotate_destination_secret_asks_for_the_confirmation_before_it_looks_at_the_body(
    api_client: Client,
) -> None:
    response = rotate(api_client("total"), confirm=OMIT, expected_version="")

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "errors" not in response.json()


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"expected_version": OMIT}, "expected_version", "obrigatório"),
        ({"expected_version": ""}, "expected_version", "no mínimo"),
        ({"expected_version": 1}, "expected_version", "texto"),
        ({"secret": "x" * 8}, "secret", "desconhecido"),  # o segredo novo nasce no servidor
        ({"url": hook()}, "url", "desconhecido"),
    ],
)
def test_rotate_destination_secret_refuses_a_body_that_does_not_serve_with_the_path_of_the_field(
    deps: Deps, api_client: Client, changes: dict[str, Any], field: str, message: str
) -> None:
    secret = store(deps, None)

    response = rotate(api_client("total"), **changes)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert message in errors_of(response)[field]
    assert opened(deps, None) == secret


def test_rotate_destination_secret_with_a_stale_version_is_a_409_with_the_version_of_now(
    deps: Deps, api_client: Client
) -> None:
    secret = store(deps, None)
    before = current(deps, None)

    response = rotate(api_client("total"), expected_version="5")

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "1"
    assert opened(deps, None) == secret and current(deps, None) == before
    assert audits(deps, "webhook_destination.rotate_secret") == []


def test_rotate_destination_secret_of_a_destination_that_is_gone_says_zero(
    deps: Deps, api_client: Client
) -> None:
    response = rotate(api_client("total"), scope="mcc")  # o mcc existe, mas não tem destino

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "0"
    assert "save_destination" in response.json()["detail"]
    assert current(deps, "mcc") is None


@pytest.mark.parametrize("version", NOT_CANONICAL, ids=[f"v{n}" for n in range(len(NOT_CANONICAL))])
def test_rotate_destination_secret_with_a_version_that_is_not_a_number_is_a_409_not_a_500(
    deps: Deps, api_client: Client, version: str
) -> None:
    secret = store(deps, None)

    response = rotate(api_client("total"), expected_version=version)

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "1" and opened(deps, None) == secret


def test_a_second_rotation_by_the_same_key_is_a_409_state_conflict_that_never_repeats_the_secret(
    deps: Deps, api_client: Client
) -> None:
    store(deps, None)
    client = api_client("total")
    first = rotate(client)  # o primeiro clique
    new = first.json()["secret"]

    second = rotate(client)  # o segundo, com a leitura de antes

    assert first.status_code == 200 and second.status_code == 409
    assert second.json()["code"] == "state_conflict" and "current_version" not in second.json()
    assert "rotate_destination_secret" in second.json()["detail"]
    assert new not in second.text and opened(deps, None) == new  # o segredo mostrado segue valendo
    assert len(audits(deps, "webhook_destination.rotate_secret")) == 1


def test_a_rotation_that_lost_to_another_author_is_a_version_conflict_not_a_repeated_click(
    deps: Deps, api_client: Client
) -> None:
    store(deps, None)
    first = rotate(api_client("total"))  # outra chave passou na frente
    assert first.status_code == 200

    second = rotate(api_client("total"))

    assert second.status_code == 409 and second.json()["code"] == "version_conflict"
    assert second.json()["current_version"] == "2"


def test_rotate_destination_secret_without_the_encryption_key_is_a_409_and_changes_nothing(
    deps: Deps, api_client: Client
) -> None:
    secret = store(deps, None)
    client = api_client("total")
    cipher = deps.cipher
    deps.cipher = None

    response = rotate(client)
    deps.cipher = cipher

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "WEBHOOK_ENCRYPTION_KEY" in response.json()["detail"]
    assert opened(deps, None) == secret and current(deps, None).version == 1  # type: ignore[union-attr]


def test_rotate_destination_secret_echoes_no_free_text_it_refuses(
    deps: Deps, api_client: Client
) -> None:
    store(deps, "mcc")
    client = api_client("total")
    refused = [
        rotate(client, scope="mcc", expected_version=SENTINEL),
        rotate(client, scope=SENTINEL),
    ]

    assert [r.status_code for r in refused] == [409, 404]
    for response in refused:
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


@pytest.mark.parametrize(
    ("scope", "path", "slug"),
    [("default", "default", None), ("mcc", "forms/mcc", "mcc")],
    ids=["default", "form"],
)
def test_rotating_leaves_the_same_state_and_audit_as_the_page_in_both_scopes(
    deps: Deps, api_client: Client, scope: str, path: str, slug: str | None
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    old = store(deps, slug)
    shown: list[str] = []
    stored_after: list[str] = []

    def after(
        call: Callable[[], httpx.Response], secret_of: Callable[[httpx.Response], str]
    ) -> Callable[[], httpx.Response]:
        def run() -> httpx.Response:
            response = call()
            shown.append(secret_of(response))
            stored_after.append(opened(deps, slug or "demo"))
            return response

        return run

    page, api = assert_same_effect(
        after(
            page_post(client, f"{path}/secret", version="1", confirm="on"),
            lambda response: shown_secret(response.text),
        ),
        after(lambda: rotate(client, scope=scope), lambda response: response.json()["secret"]),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 200 and api.status_code == 200
    assert shown[0] != shown[1] and old not in shown  # cada troca sorteou o seu
    assert shown == stored_after  # e cada uma guardou o que mostrou
    assert all(secret not in stored_text(deps) for secret in (old, *shown))


def test_a_rotation_without_the_confirmation_leaves_the_same_nothing_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    store(deps, None)

    page, api = assert_same_effect(
        page_post(client, "default/secret", version="1"),
        lambda: rotate(client, confirm=False),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert page.status_code == api.status_code == 422


def test_a_rotation_with_a_stale_version_leaves_the_same_nothing_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    store(deps, None)
    with deps.sessions.begin() as session:  # outra pessoa mudou o endereço: a versão agora é a 2
        destinations.save(
            session,
            deps.cipher,
            form_slug=None,
            url=hook("novo.example.test"),
            actor="outra-pessoa",
            expected_version=1,
            production=False,
            http_confirmed=False,
            purpose="internal",
        )

    page, api = assert_same_effect(
        page_post(client, "default/secret", version="1", confirm="on"),
        lambda: rotate(client, expected_version="1"),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert page.status_code == 303  # a página volta ao destino, com o aviso
    assert api.status_code == 409 and api.json()["current_version"] == "2"


# --- remove_destination -------------------------------------------------------------------------


def remove(
    client: ApiClient, scope: str = "default", *, version: Any = "1", confirm: Any = True
) -> httpx.Response:
    """O `DELETE` leva a versão e a confirmação na query (sem corpo)."""
    params: dict[str, Any] = {}
    if version is not OMIT:
        params["expected_version"] = version
    if confirm is not OMIT:
        params["confirm"] = confirm
    return client.delete(url_of("remove_destination", scope=scope), params=params)


def test_remove_destination_needs_a_key_and_a_read_key_is_refused_and_nothing_happens(
    deps: Deps, api_client: Client
) -> None:
    store(deps, None)
    url = url_of("remove_destination", scope="default")
    anonymous = make_api(deps, FakeClock()).delete(url, params={"expected_version": "1"})

    reader = remove(api_client("leitura"))

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert current(deps, None) is not None and audits(deps, "webhook_destination.remove") == []


def test_remove_destination_of_a_scope_that_does_not_exist_is_a_404_that_echoes_nothing(
    api_client: Client,
) -> None:
    response = remove(api_client("total"), scope=SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


def test_remove_destination_deletes_it_and_says_who_delivers_the_scope_now(
    deps: Deps, api_client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    secret = store(deps, "mcc", hook("mcc.example.test"))
    store(deps, None, hook("padrao.example.test"))
    client = api_client("total")

    with caplog.at_level(logging.DEBUG):
        response = remove(client, scope="mcc")

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["scope"], body["serving"], body["serving_host"]) == (
        "mcc",
        "default",
        "padrao.example.test",
    )
    removed = body["removed"]
    assert removed["url"] == hook("mcc.example.test") and removed["url_redacted"] is False
    assert (removed["scope"], removed["version"], removed["created_by"]) == (
        "mcc",
        "1",
        "operadora",
    )
    assert current(deps, "mcc") is None and current(deps, None) is not None
    (entry,) = audits(deps, "webhook_destination.remove")
    assert entry.actor == client.actor and entry.details["form_slug"] == "mcc"
    assert PATH_SECRET not in str(entry.details) and PATH_SECRET not in caplog.text
    for place in (response.text, caplog.text, stored_text(deps), str(entry.details)):
        assert secret not in place
    # o formulário passa a ser servido pelo padrão, e o aviso de "sem destino" não aparece
    assert read_one(client, "mcc").json()["serving"] == "default"
    assert read_all(client).json()["unserved"] == []


def test_removing_the_default_leaves_the_forms_that_depended_on_it_without_delivery(
    deps: Deps, api_client: Client
) -> None:
    store(deps, None)
    client = api_client("total")
    with deps.sessions() as session:
        assert destinations.configured(session, "demo")

    response = remove(client)

    body = response.json()
    assert response.status_code == 200 and body["scope"] == "default"
    assert (body["serving"], body["serving_host"]) == ("none", None)
    with deps.sessions() as session:
        assert not destinations.configured(session, "demo")  # o plano deixa de ser entregue
    assert read_all(client).json()["unserved"] == LEGACY


def test_remove_destination_removes_the_destination_of_a_form_that_left_the_code(
    deps: Deps, api_client: Client
) -> None:
    store(deps, "formulario-antigo")
    client = api_client("total")

    response = remove(client, scope="formulario-antigo")

    assert (
        response.status_code == 200 and response.json()["removed"]["scope"] == "formulario-antigo"
    )
    assert response.json()["serving"] == "none"
    assert read_one(client, "formulario-antigo").status_code == 404  # sem destino, o escopo some
    assert remove(client, scope="formulario-antigo").status_code == 404


@pytest.mark.parametrize("confirm", [OMIT, "false", "yes", "0", ""])
def test_remove_destination_needs_confirm_true_in_the_query_and_does_nothing_without_it(
    deps: Deps, api_client: Client, confirm: Any
) -> None:
    store(deps, None)
    client = api_client("total")
    before = current(deps, None)
    trail = [entry.id for entry in audits(deps)]

    response = remove(client, confirm=confirm)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "(`confirm=true`)" in response.json()["detail"]  # na query, e não no corpo
    assert "passam a usar o destino padrão" in response.json()["detail"]  # o texto desta operação
    assert current(deps, None) == before and [e.id for e in audits(deps)] == trail


@pytest.mark.parametrize("confirm", ["true", "TRUE", "1"])
def test_remove_destination_takes_true_and_1_in_the_query(
    deps: Deps, api_client: Client, confirm: str
) -> None:
    store(deps, None)

    response = remove(api_client("total"), confirm=confirm)

    assert response.status_code == 200 and current(deps, None) is None


def test_remove_destination_asks_for_the_confirmation_before_it_looks_at_the_parameters(
    api_client: Client,
) -> None:
    response = remove(api_client("total"), version=OMIT, confirm=OMIT)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "errors" not in response.json()


def test_remove_destination_needs_the_version_it_expects(deps: Deps, api_client: Client) -> None:
    store(deps, None)

    missing = remove(api_client("total"), version=OMIT)
    blank = remove(api_client("total"), version="")

    for response in (missing, blank):
        assert response.status_code == 422 and response.json()["code"] == "validation_failed"
        assert response.json()["errors"][0]["field"] == "expected_version"
    assert current(deps, None) is not None


def test_remove_destination_with_a_stale_version_is_a_409_with_the_version_of_now(
    deps: Deps, api_client: Client
) -> None:
    store(deps, None)
    before = current(deps, None)

    response = remove(api_client("total"), version="5")

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "1"
    assert current(deps, None) == before and audits(deps, "webhook_destination.remove") == []


def test_remove_destination_of_a_destination_that_is_gone_says_zero(
    deps: Deps, api_client: Client
) -> None:
    response = remove(api_client("total"), scope="mcc")  # o mcc existe, mas não tem destino

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "0"


@pytest.mark.parametrize("version", NOT_CANONICAL, ids=[f"v{n}" for n in range(len(NOT_CANONICAL))])
def test_remove_destination_with_a_version_that_is_not_a_number_is_a_409_not_a_500(
    deps: Deps, api_client: Client, version: str
) -> None:
    store(deps, None)

    response = remove(api_client("total"), version=version)

    assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    assert response.json()["current_version"] == "1" and current(deps, None) is not None


def test_a_second_removal_is_a_409_and_the_destination_stays_removed(
    deps: Deps, api_client: Client
) -> None:
    store(deps, None)
    client = api_client("total")
    first = remove(client)

    second = remove(client)  # o clique duplo

    assert first.status_code == 200 and second.status_code == 409
    assert second.json()["code"] == "version_conflict" and second.json()["current_version"] == "0"
    assert current(deps, None) is None and len(audits(deps, "webhook_destination.remove")) == 1


def test_remove_destination_echoes_no_free_text_it_refuses(deps: Deps, api_client: Client) -> None:
    store(deps, "mcc")
    client = api_client("total")
    refused = [
        remove(client, scope="mcc", version=SENTINEL),
        remove(client, scope=SENTINEL),
    ]

    assert [r.status_code for r in refused] == [409, 404]
    for response in refused:
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


@pytest.mark.parametrize(
    ("scope", "path", "slug"),
    [("default", "default", None), ("mcc", "forms/mcc", "mcc")],
    ids=["default", "form"],
)
def test_removing_leaves_the_same_state_and_audit_as_the_page_in_both_scopes(
    deps: Deps, api_client: Client, scope: str, path: str, slug: str | None
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    store(deps, None, hook("padrao.example.test"))
    store(deps, "mcc", hook("mcc.example.test"))

    page, api = assert_same_effect(
        page_post(client, f"{path}/remove", version="1", confirm="on"),
        lambda: remove(client, scope=scope),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 303 and page.headers["location"] == "/admin/destinations"
    assert api.status_code == 200 and current(deps, slug) is None


def test_removing_a_destination_of_a_form_that_left_the_code_leaves_the_same_as_the_page(
    deps: Deps, api_client: Client
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    store(deps, "formulario-antigo")

    page, api = assert_same_effect(
        page_post(client, "forms/formulario-antigo/remove", version="1", confirm="on"),
        lambda: remove(client, scope="formulario-antigo"),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
    )

    assert page.status_code == 303 and api.status_code == 200


def test_a_removal_without_the_confirmation_leaves_the_same_nothing_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    store(deps, None)

    page, api = assert_same_effect(
        page_post(client, "default/remove", version="1"),
        lambda: remove(client, confirm=False),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert page.status_code == api.status_code == 422


def test_a_removal_with_a_stale_version_leaves_the_same_nothing_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    store(deps, None)

    page, api = assert_same_effect(
        page_post(client, "default/remove", version="9", confirm="on"),
        lambda: remove(client, version="9"),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert page.status_code == 303 and api.status_code == 409


# --- send_destination_test_event ----------------------------------------------------------------


def event_body(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"confirm": True, **changes}
    return {key: value for key, value in body.items() if value is not OMIT}


def send_test(client: ApiClient, scope: str = "default", **changes: Any) -> httpx.Response:
    url = url_of("send_destination_test_event", scope=scope)
    return client.post(url, json=event_body(**changes))


def test_send_destination_test_event_needs_a_key_and_a_read_key_is_refused_and_sends_nothing(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    store(deps, None, receiver.url("/padrao"))
    url = url_of("send_destination_test_event", scope="default")
    anonymous = make_api(deps, FakeClock()).post(url, json=event_body())

    reader = send_test(api_client("leitura"))

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert receiver.requests == [] and audits(deps, "webhook_destination.test") == []


def test_send_destination_test_event_of_a_scope_that_does_not_exist_is_a_404_and_sends_nothing(
    api_client: Client, receiver: Receiver
) -> None:
    response = send_test(api_client("total"), scope=SENTINEL)

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert SENTINEL not in response.text and receiver.requests == []


def test_the_test_event_goes_to_the_receiver_signed_with_the_secret_and_carries_no_lead_data(
    deps: Deps, api_client: Client, receiver: Receiver, caplog: pytest.LogCaptureFixture
) -> None:
    submit(deps)  # há uma lead no banco, e nada dela pode ir no teste
    secret = store(deps, None, receiver.url("/padrao"))
    receiver.status = 202
    receiver.body = b'{"recebido": true, "fila": "planos-de-acao"}'
    client = api_client("total")

    with caplog.at_level(logging.DEBUG):
        response = send_test(client)

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["scope"], body["outcome"], body["status"]) == ("default", "delivered", 202)
    assert isinstance(body["elapsed_ms"], int) and body["error"] is None
    assert "planos-de-acao" in body["body_start"]
    (request,) = receiver.requests
    assert request.path == "/padrao" and request.signed_with(secret)
    assert request.headers["X-MCC-Event"] == "webhook.teste"
    assert request.headers["Content-Type"] == "application/json"
    payload = request.json()
    assert (payload["event"], payload["test"], payload["form"]) == ("webhook.teste", True, None)
    assert request.headers["Idempotency-Key"] == payload["event_id"]
    assert not any(trace in request.body.decode() for trace in LEAD_TRACES)
    (entry,) = audits(deps, "webhook_destination.test")
    assert entry.actor == client.actor and entry.details["status"] == 202
    assert entry.details["error"] is None and isinstance(entry.details["elapsed_ms"], int)
    for place in (response.text, caplog.text, stored_text(deps), str(entry.details)):
        assert secret not in place
    assert "planos-de-acao" not in str(entry.details)  # a auditoria guarda o status, não a resposta
    assert "/padrao" not in response.text + caplog.text + str(entry.details)  # nem o caminho da URL


@pytest.mark.parametrize(
    ("status", "outcome"),
    [(204, "delivered"), (401, "rejected"), (404, "rejected"), (503, "retry"), (429, "retry")],
)
def test_the_test_event_says_what_a_delivery_would_do_with_that_answer(
    deps: Deps, api_client: Client, receiver: Receiver, status: int, outcome: str
) -> None:
    store(deps, None, receiver.url("/padrao"))
    receiver.status = status

    body = send_test(api_client("total")).json()

    assert (body["outcome"], body["status"]) == (outcome, status)
    assert body["body_start"] is None  # o receptor não mandou corpo
    assert [request.path for request in receiver.requests] == ["/padrao"]


def test_a_redirect_is_not_followed_and_counts_as_a_refusal(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    store(deps, None, receiver.url("/padrao"))
    receiver.status = 302
    receiver.headers = {"Location": receiver.url("/outro")}

    body = send_test(api_client("total")).json()

    assert (body["outcome"], body["status"]) == ("rejected", 302)
    assert [request.path for request in receiver.requests] == ["/padrao"]  # nada de seguir o 302


def test_a_receiver_that_does_not_answer_is_a_200_that_says_so_without_the_address(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    store(deps, None, receiver.url("/" + SENTINEL))
    receiver.server.shutdown()
    receiver.server.server_close()  # a porta fecha: a conexão é recusada

    response = send_test(api_client("total"))

    body = response.json()
    assert response.status_code == 200 and body["outcome"] == "no_response"
    assert (body["status"], body["error"], body["body_start"]) == (None, "ConnectError", None)
    assert SENTINEL not in response.text and receiver.host not in response.text
    (entry,) = audits(deps, "webhook_destination.test")
    assert entry.details["error"] == "ConnectError" and entry.details["status"] is None
    assert SENTINEL not in str(entry.details)


def test_the_start_of_the_answer_is_cut_to_the_same_length_the_page_shows(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    store(deps, None, receiver.url("/padrao"))
    receiver.status = 200
    receiver.body = b"x" * 3000

    body = send_test(api_client("total")).json()

    assert body["body_start"] == "x" * 500


def test_the_test_event_of_a_form_goes_to_its_own_destination_and_not_to_the_default(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    store(deps, None, receiver.url("/padrao"))
    secret = store(deps, "mcc", receiver.url("/mcc"))

    response = send_test(api_client("total"), scope="mcc")

    assert response.status_code == 200 and response.json()["scope"] == "mcc"
    (request,) = receiver.requests
    assert request.path == "/mcc" and request.signed_with(secret)
    assert request.json()["form"] == {"slug": "mcc"}


def test_a_form_without_its_own_destination_has_nothing_to_test_even_if_the_default_serves_it(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    store(deps, None, receiver.url("/padrao"))

    response = send_test(api_client("total"), scope="mcc")

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert (
        "default" in response.json()["detail"] and "save_destination" in response.json()["detail"]
    )
    assert receiver.requests == [] and audits(deps, "webhook_destination.test") == []


def test_a_secret_the_key_cannot_open_stops_the_test_before_sending_anything(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    store(deps, None, receiver.url("/padrao"))
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))  # a chave mudou sem a antiga

    response = send_test(api_client("total"))

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "rotate_destination_secret" in response.json()["detail"]
    assert receiver.requests == [] and audits(deps, "webhook_destination.test") == []


@pytest.mark.parametrize("confirm", [OMIT, False, "true", 1])
def test_send_destination_test_event_needs_confirm_true_and_sends_nothing_without_it(
    deps: Deps, api_client: Client, receiver: Receiver, confirm: Any
) -> None:
    store(deps, None, receiver.url("/padrao"))
    client = api_client("total")
    trail = [entry.id for entry in audits(deps)]

    response = send_test(client, confirm=confirm)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "chamada de verdade ao receptor" in response.json()["detail"]
    assert receiver.requests == [] and [e.id for e in audits(deps)] == trail


def test_send_destination_test_event_refuses_a_body_that_is_not_the_confirmation(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    store(deps, None, receiver.url("/padrao"))

    response = send_test(api_client("total"), url=receiver.url("/outro"))

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert "desconhecido" in errors_of(response)["url"]
    assert receiver.requests == []  # o endereço do teste é o do destino, e não o do corpo


def test_the_test_event_is_signed_with_the_secret_of_the_last_rotation(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    old = store(deps, None, receiver.url("/padrao"))
    client = api_client("total")
    rotated = rotate(client, expected_version="1")
    new = rotated.json()["secret"]

    send_test(client)

    (request,) = receiver.requests
    assert request.signed_with(new) and not request.signed_with(old)


def test_changing_the_address_sends_the_next_test_event_there_with_the_same_secret(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    secret = store(deps, None, receiver.url("/antigo"))
    client = api_client("total")
    assert save(client, url=receiver.url("/novo"), expected_version="1").status_code == 200

    send_test(client)

    (request,) = receiver.requests
    assert request.path == "/novo" and request.signed_with(secret)


def test_send_destination_test_event_costs_five_tokens_of_the_usage_limit(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    store(deps, None, receiver.url("/padrao"))
    client = api_client("total", API_RATE_LIMIT_PER_MINUTE="5")  # o balde tem 5 fichas

    first = send_test(client)
    second = send_test(client)

    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["code"] == "rate_limited"
    assert len(receiver.requests) == 1  # a recusa não chegou ao receptor


def test_send_destination_test_event_echoes_no_free_text_it_refuses(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    store(deps, None, receiver.url("/padrao"))
    client = api_client("total")
    refused = [
        send_test(client, scope=SENTINEL),
        send_test(client, url=SENTINEL),  # um campo que a operação não tem, com o valor sentinela
    ]

    assert [r.status_code for r in refused] == [404, 422]
    for response in refused:
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)
    assert receiver.requests == []


def sent_by(receiver: Receiver, sink: list[Any], call: Callable[[], Any]) -> Callable[[], Any]:
    """`call`, anotando em `sink` o que o receptor recebeu (o caminho, os cabeçalhos do contrato e o
    corpo sem o que cada envio sorteia: o id do evento e a hora) e como a auditoria do envio ficou
    (sem o tempo, que muda a cada vez)."""

    def run() -> Any:
        start = len(receiver.requests)
        result = call()
        for request in receiver.requests[start:]:
            payload = request.json()
            payload.pop("event_id"), payload.pop("occurred_at")
            sink.append(
                (
                    request.path,
                    request.headers["X-MCC-Event"],
                    request.headers["Content-Type"],
                    request.headers["Idempotency-Key"] == request.json()["event_id"],
                    payload,
                )
            )
        return result

    return run


@pytest.mark.parametrize(
    ("scope", "path", "slug"),
    [("default", "default", None), ("mcc", "forms/mcc", "mcc")],
    ids=["default", "form"],
)
def test_sending_the_test_event_leaves_the_same_audit_and_the_same_traffic_as_the_page(
    deps: Deps, api_client: Client, receiver: Receiver, scope: str, path: str, slug: str | None
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    secret = store(deps, slug, receiver.url("/hook"))
    receiver.status = 202
    by_page: list[Any] = []
    by_api: list[Any] = []
    details: list[dict[str, Any]] = []

    def kept(call: Callable[[], Any]) -> Callable[[], Any]:
        def run() -> Any:
            result = call()
            details.append(dict(audits(deps, "webhook_destination.test")[-1].details))
            return result

        return run

    page, api = assert_same_effect(
        kept(sent_by(receiver, by_page, page_post(client, f"{path}/test"))),
        kept(sent_by(receiver, by_api, lambda: send_test(client, scope=scope))),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        # o tempo do envio muda a cada vez: o resto da auditoria é o mesmo
        ignore={**IGNORE, "audit_log": {"target_id", "details"}},
    )

    assert page.status_code == 200 and api.status_code == 200
    assert by_page == by_api and len(by_page) == 1  # o mesmo evento, uma vez
    assert all(request.signed_with(secret) for request in receiver.requests)
    page_details, api_details = details
    for entry in (page_details, api_details):
        assert isinstance(entry.pop("elapsed_ms"), int)
    assert (
        page_details
        == api_details
        == {
            "form_slug": slug,
            "purpose": "internal",
            "purpose_origin": "explicit",
            "host": receiver.host,
            "status": 202,
            "error": None,
        }
    )


def test_a_scope_with_nothing_to_test_sends_nothing_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    store(deps, None, receiver.url("/padrao"))

    page, api = assert_same_effect(
        page_post(client, "forms/mcc/test"),
        lambda: send_test(client, scope="mcc"),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert page.status_code == 303 and api.status_code == 409
    assert receiver.requests == []


def test_a_secret_that_does_not_open_sends_nothing_on_the_page_and_in_the_api(
    deps: Deps, api_client: Client, receiver: Receiver
) -> None:
    client = api_client("total", ADMIN_USERNAME=PAGE_USER)
    store(deps, None, receiver.url("/padrao"))
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))

    page, api = assert_same_effect(
        page_post(client, "default/test"),
        lambda: send_test(client),
        api_actor=client.actor,
        page_actor=PAGE_USER,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert page.status_code == api.status_code == 409
    assert receiver.requests == []


def test_the_repr_of_what_carries_a_secret_never_shows_it(deps: Deps) -> None:
    """Um log, um traceback ou um `assert` que imprime o objeto não leva o segredo: o resultado do
    serviço, o da ação e os dois modelos de resposta o escondem do `repr`."""
    with deps.sessions.begin() as session:
        created = destinations.save(
            session,
            deps.cipher,
            form_slug="mcc",
            url=hook(),
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="internal",
        )
    assert created.secret is not None
    rotated = actions.rotate(deps, "mcc", actor="operadora", expected_version=1)
    shown = {
        "SaveResult": (created, created.secret),
        "SavedDestination": (SavedDestination.of_saved(created, unreadable=False), created.secret),
        "Rotated": (rotated, rotated.secret),
        "RotatedDestination": (RotatedDestination.of_rotated(rotated), rotated.secret),
    }

    for name, (found, secret) in shown.items():
        assert secret not in repr(found) and secret not in str(found), name
    assert "secret=" not in repr(shown["SavedDestination"][0])
    assert "secret=" not in repr(shown["RotatedDestination"][0])
