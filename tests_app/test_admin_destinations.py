"""Destinos do webhook no painel admin, pela costura HTTP (TestClient), contra um receptor local:
a lista, a criação com o segredo mostrado uma vez, a troca do segredo, o evento de teste e a
auditoria."""

from __future__ import annotations

import json
import logging
import re
import socket

import httpx
import pytest
import sqlalchemy as sa
from conftest import Receiver
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_admin import ORIGIN, PASSWORD_HASH, FakeClock, csrf_token, finish, login, section
from test_intake import submit
from test_retention import LEAD_TRACES

from app import destinations, pipeline
from app.api.main import create_app
from app.db import make_engine, make_session_factory
from app.deps import Deps
from app.destinations import SecretCipher, StartupError
from app.models import AuditLog, Base
from app.settings import Settings

ADMIN_ENV = {
    "ADMIN_USERNAME": "operadora",
    "ADMIN_PASSWORD_HASH": PASSWORD_HASH,
    "ADMIN_SESSION_SECRET": "segredo-da-sessao-admin-0123456789abcdef",
}
DEFAULT = "/admin/destinations/default"


def admin_client(deps: Deps, **env: str) -> TestClient:
    deps.settings = Settings(_env_file=None, **ADMIN_ENV, **env)  # type: ignore[call-arg]
    client = TestClient(create_app(trigger=None, clock=FakeClock()), follow_redirects=False)
    assert login(client).status_code == 303
    return client


@pytest.fixture
def admin(deps: Deps) -> TestClient:
    return admin_client(deps)


def post(
    client: TestClient, path: str, *, headers: dict[str, str] | None = None, **form: str
) -> httpx.Response:
    data = {"csrf_token": csrf_token(client), **form}
    return client.post(path, data=data, headers={**ORIGIN, **(headers or {})})


def shown_secret(html: str) -> str:
    """O segredo como a operadora o vê: no campo de copiar, logo depois de gerado."""
    match = re.search(r'id="segredo-novo"[^>]*value="([^"]+)"', html)
    assert match is not None, "o segredo novo não apareceu"
    return match.group(1)


def create(client: TestClient, path: str, url: str) -> str:
    response = post(client, f"{path}/url", url=url, version="0")
    assert response.status_code == 200, response.text
    return shown_secret(response.text)


def stored_text(deps: Deps) -> str:
    """Todas as linhas de todas as tabelas, como JSON (onde um segredo vazado apareceria)."""
    with deps.sessions() as session:
        return "\n".join(
            row
            for table in Base.metadata.sorted_tables
            for row in session.scalars(sa.text(f"select row_to_json(t)::text from {table.name} t"))
        )


def test_the_destinations_page_warns_about_the_forms_left_without_destination(
    admin: TestClient, receiver: Receiver
) -> None:
    page = admin.get("/admin/destinations")

    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    warning = section(page.text, "sem-destino")
    assert all(slug in warning for slug in ("demo", "mcc", "reforma-2027"))
    assert "não são entregues" in warning
    forms = section(page.text, "formularios")
    assert "Calendário de atividades" in forms
    assert forms.count("Sem destino") == 3
    assert "Sem destino do webhook" in admin.get("/admin").text  # o aviso aparece nos pedidos

    create(admin, DEFAULT, receiver.url("/padrao"))

    page = admin.get("/admin/destinations").text
    assert 'id="sem-destino"' not in page
    forms = section(page, "formularios")
    assert forms.count("Padrão") == 3 and receiver.host in section(page, "padrao")
    assert "Sem destino do webhook" not in admin.get("/admin").text


def test_creating_a_destination_shows_its_secret_once_and_keeps_it_sealed(
    admin: TestClient, deps: Deps, receiver: Receiver
) -> None:
    response = post(admin, f"{DEFAULT}/url", url=receiver.url("/padrao"), version="0")

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    secret = shown_secret(response.text)
    assert "não aparece de novo" in section(response.text, "novo-segredo")
    assert secret not in admin.get(DEFAULT).text  # recarregar a página não mostra de novo
    assert secret not in admin.get("/admin/destinations").text
    assert secret not in stored_text(deps)  # nem no banco, nem na auditoria: só cifrado
    page = admin.get(DEFAULT).text
    assert receiver.url("/padrao") in section(page, "endereco")


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://hooks.example.test/planos", "https://"),
        ("hooks.example.test/planos", "https://"),
        ("https://nome-de-usuario@hooks.example.test/planos", "usuário e a senha"),
        ("https://hooks.example.test/" + "a" * 2000, "2000 caracteres"),
        ("https:///planos", "endereço do servidor"),
        ("https://hooks.example.test/pla nos", "espaços"),
        ("https://hooks.example.test/pla\x07nos", "caracteres de controle"),
        ("https://hooks.example.test:99999/planos", "porta"),
        ("https://hooks.example.test/planos#trecho", "#"),
        ("http://169.254.169.254/latest/meta-data", "link-local"),
        ("http://[fe80::1]/planos", "link-local"),
        ("http://[::ffff:169.254.169.254]/planos", "link-local"),
    ],
)
def test_only_an_http_or_https_url_without_credentials_becomes_a_destination(
    admin: TestClient, url: str, message: str
) -> None:
    response = post(admin, f"{DEFAULT}/url", url=url, version="0")

    assert response.status_code == 422 and message in section(response.text, "endereco")
    assert 'id="segredo-novo"' not in response.text
    assert "Sem destino" in section(admin.get("/admin/destinations").text, "formularios")


def test_in_production_an_http_destination_needs_a_confirmation(
    deps: Deps, receiver: Receiver
) -> None:
    client = admin_client(deps, APP_ENV="production")

    refused = post(client, f"{DEFAULT}/url", url=receiver.url("/padrao"), version="0")

    assert refused.status_code == 422
    form = section(refused.text, "endereco")
    assert "sem criptografia" in form and 'name="confirm_http"' in form
    assert receiver.url("/padrao") in form  # a URL digitada não se perde

    # O esquema vale também em maiúsculas.
    uppercase = receiver.url("/padrao").replace("http://", "HTTP://")
    accepted = post(client, f"{DEFAULT}/url", url=uppercase, version="0", confirm_http="on")
    assert accepted.status_code == 200 and shown_secret(accepted.text)
    with deps.sessions() as session:
        (created,) = session.scalars(sa.select(AuditLog))
    assert created.details["http_confirmed"] is True  # a auditoria guarda a confirmação
    assert https_needs_nothing(client)


def https_needs_nothing(client: TestClient) -> bool:
    response = post(
        client, "/admin/destinations/forms/mcc/url", url="https://n8n.example.test/w", version="0"
    )
    return response.status_code == 200 and 'id="segredo-novo"' in response.text


def test_the_test_event_is_signed_like_plano_concluido_and_carries_no_lead_data(
    admin: TestClient, deps: Deps, receiver: Receiver
) -> None:
    submit(deps)  # há uma lead no banco, e nada dela pode ir no teste
    secret = create(admin, DEFAULT, receiver.url("/padrao"))
    receiver.status = 202
    receiver.body = b'{"recebido": true, "fila": "planos-de-acao"}'

    response = post(admin, f"{DEFAULT}/test")

    assert response.status_code == 200
    result = section(response.text, "teste")
    assert re.search(r"HTTP 202 em \d+ ms", result)
    assert "conta como entregue" in result and "planos-de-acao" in result
    (request,) = receiver.requests
    assert request.path == "/padrao" and request.signed_with(secret)
    assert request.headers["X-MCC-Event"] == "webhook.teste"
    assert request.headers["Content-Type"] == "application/json"
    payload = request.json()
    assert (payload["event"], payload["test"]) == ("webhook.teste", True)
    assert request.headers["Idempotency-Key"] == payload["event_id"]
    assert not any(trace in request.body.decode() for trace in LEAD_TRACES)


@pytest.mark.parametrize(
    ("status", "verdict"),
    [(401, "recusado de vez"), (503, "repetido"), (302, "Redirecionamentos não são seguidos")],
)
def test_the_test_event_says_what_a_delivery_would_do_with_that_answer(
    admin: TestClient, receiver: Receiver, status: int, verdict: str
) -> None:
    create(admin, DEFAULT, receiver.url("/padrao"))
    receiver.status = status
    receiver.headers = {"Location": receiver.url("/outro")} if status == 302 else {}

    result = section(post(admin, f"{DEFAULT}/test").text, "teste")

    assert f"HTTP {status}" in result and verdict in result
    assert [request.path for request in receiver.requests] == ["/padrao"]  # nada de seguir o 302


def test_the_test_event_reports_a_receiver_that_does_not_answer(
    admin: TestClient, receiver: Receiver
) -> None:
    create(admin, DEFAULT, receiver.url("/padrao"))
    receiver.server.shutdown()
    receiver.server.server_close()  # a porta fecha: a conexão é recusada

    result = section(post(admin, f"{DEFAULT}/test").text, "teste")

    assert "Não deu para falar com o receptor" in result and "ConnectError" in result


def test_a_new_secret_replaces_the_old_one_at_once_and_appears_only_once(
    admin: TestClient, deps: Deps, receiver: Receiver
) -> None:
    old = create(admin, DEFAULT, receiver.url("/padrao"))

    unconfirmed = post(admin, f"{DEFAULT}/secret", version="1")
    rotated = post(admin, f"{DEFAULT}/secret", version="1", confirm="on")

    assert unconfirmed.status_code == 422 and "Confirme" in section(unconfirmed.text, "segredo")
    assert rotated.status_code == 200
    new = shown_secret(rotated.text)
    assert new != old and new not in admin.get(DEFAULT).text
    assert new not in stored_text(deps) and old not in stored_text(deps)
    post(admin, f"{DEFAULT}/test")
    (request,) = receiver.requests
    assert request.signed_with(new) and not request.signed_with(old)

    resubmitted = post(admin, f"{DEFAULT}/secret", version="1", confirm="on")  # F5 no POST
    assert resubmitted.status_code == 303
    assert "seu outro clique" in admin.get(resubmitted.headers["location"]).text
    post(admin, f"{DEFAULT}/test")
    assert receiver.requests[-1].signed_with(new)  # o segredo mostrado continua valendo


def test_changing_the_url_keeps_the_secret(admin: TestClient, receiver: Receiver) -> None:
    secret = create(admin, DEFAULT, receiver.url("/antigo"))

    changed = post(admin, f"{DEFAULT}/url", url=receiver.url("/novo"), version="1")

    assert changed.status_code == 303
    page = admin.get(changed.headers["location"]).text
    assert "Endereço salvo" in page and 'id="segredo-novo"' not in page
    post(admin, f"{DEFAULT}/test")
    (request,) = receiver.requests
    assert request.path == "/novo" and request.signed_with(secret)


def test_a_form_destination_takes_the_place_of_the_default_until_it_is_removed(
    admin: TestClient, receiver: Receiver
) -> None:
    mcc = "/admin/destinations/forms/mcc"
    create(admin, DEFAULT, receiver.url("/padrao"))
    assert "usa o destino padrão" in section(admin.get(mcc).text, "endereco")

    create(admin, mcc, receiver.url("/mcc"))

    forms = section(admin.get("/admin/destinations").text, "formularios")
    assert (forms.count("Próprio"), forms.count("Padrão")) == (1, 2)
    post(admin, f"{mcc}/test")
    assert receiver.requests[-1].path == "/mcc"

    assert post(admin, f"{mcc}/remove", version="1").status_code == 422  # sem confirmar
    removed = post(admin, f"{mcc}/remove", version="1", confirm="on")
    assert removed.status_code == 303 and removed.headers["location"] == "/admin/destinations"
    page = admin.get("/admin/destinations").text
    assert "Destino removido" in page
    assert section(page, "formularios").count("Padrão") == 3


def test_a_destination_kept_for_a_form_that_left_the_code_can_still_be_removed(
    admin: TestClient, deps: Deps, receiver: Receiver
) -> None:
    with deps.sessions.begin() as session:  # o formulário saiu do registro, o destino ficou
        destinations.save(
            session,
            deps.cipher,
            form_slug="formulario-antigo",
            url=receiver.url("/antigo"),
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
        )
    path = "/admin/destinations/forms/formulario-antigo"

    assert "formulario-antigo" in section(admin.get("/admin/destinations").text, "formularios")
    assert "saiu do código" in admin.get(path).text
    removed = post(admin, f"{path}/remove", version="1", confirm="on")

    assert removed.status_code == 303
    assert "formulario-antigo" not in admin.get("/admin/destinations").text
    assert admin.get(path).status_code == 404


def test_a_form_that_does_not_exist_has_no_destination_page(admin: TestClient) -> None:
    path = "/admin/destinations/forms/nao-existe"

    assert admin.get(path).status_code == 404
    created = post(admin, f"{path}/url", url="https://hooks.example.test/x", version="0")
    assert created.status_code == 404
    assert "nao-existe" not in admin.get("/admin/destinations").text


ACTIONS = [
    f"{base}/{action}"
    for base in (DEFAULT, "/admin/destinations/forms/mcc")
    for action in ("url", "secret", "test", "remove")
]


@pytest.mark.parametrize("path", ACTIONS)
def test_each_destination_change_needs_the_session_and_the_csrf_token(
    admin: TestClient, deps: Deps, receiver: Receiver, path: str
) -> None:
    secret = create(admin, path.rsplit("/", 1)[0], receiver.url("/hook"))
    form = {"url": receiver.url("/golpe"), "version": "1", "confirm": "on"}
    anonymous = TestClient(create_app(trigger=None, clock=FakeClock()), follow_redirects=False)

    refused = [
        anonymous.post(path, data={"csrf_token": "x", **form}, headers=ORIGIN),
        admin.post(path, data={"csrf_token": "token-errado", **form}, headers=ORIGIN),
        admin.post(path, data={"csrf_token": csrf_token(admin), **form}),  # sem Origin
    ]

    assert [response.status_code for response in refused] == [303, 403, 403]
    assert refused[0].headers["location"].startswith("/admin/login")
    assert receiver.requests == []  # nenhum evento de teste saiu
    post(admin, f"{path.rsplit('/', 1)[0]}/test")
    (request,) = receiver.requests  # destino, URL e segredo ficaram como estavam
    assert request.path == "/hook" and request.signed_with(secret)


def test_each_change_is_audited_in_the_operator_name_without_the_secret_or_the_url_path(
    admin: TestClient, deps: Deps, receiver: Receiver
) -> None:
    first = create(admin, DEFAULT, receiver.url("/caminho-que-vale-senha"))
    post(admin, f"{DEFAULT}/url", url=receiver.url("/outro-caminho-secreto"), version="1")
    second = shown_secret(post(admin, f"{DEFAULT}/secret", version="2", confirm="on").text)
    post(admin, f"{DEFAULT}/test")
    post(admin, f"{DEFAULT}/remove", version="3", confirm="on")

    with deps.sessions() as session:
        trail = list(session.scalars(sa.select(AuditLog).order_by(AuditLog.at)))

    assert [audit.action.removeprefix("webhook_destination.") for audit in trail] == [
        "create",
        "update",
        "rotate_secret",
        "test",
        "remove",
    ]
    assert {audit.actor for audit in trail} == {"operadora"}
    assert {audit.details["host"] for audit in trail} == {receiver.host}
    assert trail[3].details["status"] == 204 and "elapsed_ms" in trail[3].details
    recorded = json.dumps([audit.details for audit in trail])
    for leaked in (first, second, "caminho-que-vale-senha", "outro-caminho-secreto"):
        assert leaked not in recorded


def test_the_job_detail_shows_where_each_delivery_went(
    admin: TestClient, deps: Deps, receiver: Receiver
) -> None:
    create(admin, DEFAULT, receiver.url("/padrao"))
    job = submit(deps).job
    (delivery_id,) = finish(deps, job.id)
    pipeline.deliver(deps, delivery_id)

    deliveries = section(admin.get(f"/admin/jobs/{job.public_id}").text, "entregas")

    assert "destino padrão" in deliveries and receiver.host in deliveries


def test_without_the_encryption_key_no_destination_is_created(
    admin: TestClient, deps: Deps, receiver: Receiver
) -> None:
    deps.cipher = None  # WEBHOOK_ENCRYPTION_KEY ausente

    assert "Falta WEBHOOK_ENCRYPTION_KEY" in admin.get("/admin/destinations").text
    response = post(admin, f"{DEFAULT}/url", url=receiver.url("/padrao"), version="0")

    assert response.status_code == 422
    assert "defina WEBHOOK_ENCRYPTION_KEY" in section(response.text, "endereco")
    assert section(admin.get("/admin/destinations").text, "formularios").count("Sem destino") == 3


def test_a_secret_the_key_cannot_open_is_reported_without_sending(
    admin: TestClient, deps: Deps, receiver: Receiver
) -> None:
    create(admin, DEFAULT, receiver.url("/padrao"))
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))  # outra chave, sem a antiga

    plain = post(admin, f"{DEFAULT}/test")
    htmx = post(admin, f"{DEFAULT}/test", headers={"HX-Request": "true"})

    assert plain.status_code == 409
    assert htmx.status_code == 200  # o HTMX só troca o trecho da página com 2xx
    assert "não abre com WEBHOOK_ENCRYPTION_KEY" in section(htmx.text, "teste")
    assert receiver.requests == []


def test_when_the_api_starts_the_env_webhook_becomes_the_default_destination(
    deps: Deps, receiver: Receiver
) -> None:
    deps.settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        WEBHOOK_URL=receiver.url("/env"),
        WEBHOOK_SECRET="segredo-falso-que-o-receptor-ja-conhece",
        **ADMIN_ENV,
    )

    with TestClient(create_app(trigger=None, clock=FakeClock()), follow_redirects=False) as client:
        login(client)
        page = client.get(DEFAULT).text

    assert receiver.url("/env") in section(page, "endereco") and "ambiente" in page


def test_in_production_the_api_does_not_start_without_the_key_when_a_destination_exists(
    admin: TestClient, deps: Deps, receiver: Receiver
) -> None:
    create(admin, DEFAULT, receiver.url("/padrao"))
    deps.settings = Settings(_env_file=None, APP_ENV="production", **ADMIN_ENV)  # type: ignore[call-arg]
    deps.cipher = None

    app = create_app(trigger=None, clock=FakeClock())
    with pytest.raises(StartupError, match="WEBHOOK_ENCRYPTION_KEY"), TestClient(app):
        pass


def test_the_api_starts_with_the_database_down_as_before(deps: Deps) -> None:
    """Sem banco na subida, a API sobe e o /readyz diz 503; a importação fica com o worker."""
    with socket.socket() as probe:  # uma porta livre, que ninguém atende
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    nowhere = make_engine(f"postgresql+psycopg://mcc@127.0.0.1:{port}/fora_do_ar_test")
    deps.sessions = make_session_factory(nowhere)
    try:
        with TestClient(create_app(trigger=None, clock=FakeClock())) as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/readyz").status_code == 503
    finally:
        nowhere.dispose()


def app_logs(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Captura tudo, inclusive os loggers do app: o `fileConfig` do alembic, que a suíte roda na
    subida, desliga os loggers que já existiam (a API e o worker não rodam o alembic)."""
    for name in ("app.admin.routes", "app.destinations"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    caplog.set_level(logging.DEBUG)


def test_no_secret_reaches_the_logs(
    admin: TestClient,
    receiver: Receiver,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_logs(monkeypatch, caplog)

    first = create(admin, DEFAULT, receiver.url("/padrao"))
    second = shown_secret(post(admin, f"{DEFAULT}/secret", version="1", confirm="on").text)
    post(admin, f"{DEFAULT}/test")

    assert "admin: destino do webhook criado" in caplog.text  # os logs existem, só sem o segredo
    assert "admin: evento de teste" in caplog.text
    assert first not in caplog.text and second not in caplog.text


def test_every_secret_the_key_cannot_open_is_flagged_in_the_admin(
    admin: TestClient, deps: Deps, receiver: Receiver
) -> None:
    create(admin, DEFAULT, receiver.url("/padrao"))  # cifrado com a chave de antes
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))  # a chave mudou sem a antiga
    create(admin, "/admin/destinations/forms/mcc", receiver.url("/mcc"))  # cifrado com a nova

    for path in ("/admin", "/admin/destinations"):
        warning = section(admin.get(path).text, "segredo-ilegivel")
        assert "padrão" in warning and "não abre" in warning, path
        assert "mcc" not in warning, path
    assert "não abre" in section(admin.get(DEFAULT).text, "segredo")
    assert "não abre" not in section(admin.get("/admin/destinations/forms/mcc").text, "segredo")


def test_a_second_click_on_create_or_rotate_says_the_other_click_took_the_secret(
    admin: TestClient, receiver: Receiver
) -> None:
    def second_click(path: str, **form: str) -> str:
        assert post(admin, path, **form).status_code == 200  # o primeiro clique
        repeated = post(admin, path, **form)  # o segundo, com a página de antes
        assert repeated.status_code == 303
        return admin.get(repeated.headers["location"]).text

    created = second_click(f"{DEFAULT}/url", url=receiver.url("/padrao"), version="0")
    rotated = second_click(f"{DEFAULT}/secret", version="1", confirm="on")

    for message in (created, rotated):
        assert "seu outro clique" in message and "gere um segredo novo" in message
        assert "Nada foi alterado" not in message


def test_a_change_made_by_someone_else_is_still_refused_as_before(
    admin: TestClient, deps: Deps, receiver: Receiver
) -> None:
    with deps.sessions.begin() as session:  # outra pessoa criou o destino do MCC antes
        destinations.save(
            session,
            deps.cipher,
            form_slug="mcc",
            url=receiver.url("/mcc"),
            actor="outra-pessoa",
            expected_version=0,
            production=False,
            http_confirmed=False,
        )

    repeated = post(admin, "/admin/destinations/forms/mcc/url", url=receiver.url(), version="0")

    message = admin.get(repeated.headers["location"]).text
    assert "O destino mudou" in message and "seu outro clique" not in message


def test_create_and_rotate_lock_their_button_while_htmx_sends_them(
    admin: TestClient, receiver: Receiver
) -> None:
    def form_tag(action: str) -> str:
        match = re.search(rf'<form[^>]*action="{action}"[^>]*>', admin.get(DEFAULT).text)
        assert match is not None, action
        return match.group(0)

    for action in (f"{DEFAULT}/url", f"{DEFAULT}/secret"):
        if action.endswith("/secret"):
            create(admin, DEFAULT, receiver.url("/padrao"))
        tag = form_tag(action)
        assert f'hx-post="{action}"' in tag
        assert 'hx-sync="this:drop"' in tag and 'hx-disabled-elt="find button"' in tag


def test_htmx_gets_refusals_with_status_200_so_it_shows_them(admin: TestClient) -> None:
    plain = post(admin, f"{DEFAULT}/url", url="ftp://hooks.example.test/x", version="0")
    htmx = post(
        admin,
        f"{DEFAULT}/url",
        url="ftp://hooks.example.test/x",
        version="0",
        headers={"HX-Request": "true"},
    )

    assert plain.status_code == 422
    assert htmx.status_code == 200  # o HTMX não troca o trecho da página com 4xx
    assert "https://" in section(htmx.text, "endereco")


def test_the_page_says_the_worker_must_reach_the_url(admin: TestClient) -> None:
    address = section(admin.get(DEFAULT).text, "endereco")

    assert "o worker" in address and "gerador-planos_internal" in address


def test_the_no_destination_warning_says_how_a_ready_plan_gets_delivered(
    admin: TestClient,
) -> None:
    warning = section(admin.get("/admin").text, "sem-destino")

    assert "reprocesse o pedido" in warning


def test_without_a_destination_there_is_nothing_to_test(admin: TestClient) -> None:
    response = post(admin, f"{DEFAULT}/test")

    assert response.status_code == 303
    assert "Crie o destino antes" in admin.get(response.headers["location"]).text


# Um valor que o servidor nunca deu como versão: um sobrescrito (`isdigit`, mas o `int()` levanta), um
# número sem fim, um zero à esquerda, outro alfabeto e o que nem é um número.
NOT_A_VERSION = ["²", "9" * 5000, "01", "00", "٣", " 1", "1e3", "-1", "", "0.0"]


@pytest.mark.parametrize("version", NOT_A_VERSION, ids=[f"v{n}" for n in range(len(NOT_A_VERSION))])
def test_a_version_the_page_never_gave_is_the_changed_notice_and_never_a_500(
    admin: TestClient, deps: Deps, receiver: Receiver, version: str
) -> None:
    secret = create(admin, DEFAULT, receiver.url("/padrao"))  # a versão 1
    with deps.sessions() as session:
        before = destinations.listing(session)[None]

    for action, form in (
        ("url", {"url": receiver.url("/novo")}),
        ("secret", {"confirm": "on"}),
        ("remove", {"confirm": "on"}),
    ):
        response = post(admin, f"{DEFAULT}/{action}", version=version, **form)

        assert response.status_code == 303, action
        assert "O destino mudou" in admin.get(response.headers["location"]).text, action
        assert "seu outro clique" not in admin.get(response.headers["location"]).text, action

    with deps.sessions() as session:
        assert destinations.listing(session)[None] == before  # nada mudou
    assert secret not in stored_text(deps)


@pytest.mark.parametrize("version", NOT_A_VERSION, ids=[f"v{n}" for n in range(len(NOT_A_VERSION))])
def test_a_version_the_page_never_gave_does_not_create_a_destination_either(
    admin: TestClient, deps: Deps, receiver: Receiver, version: str
) -> None:
    response = post(admin, f"{DEFAULT}/url", url=receiver.url("/padrao"), version=version)

    assert response.status_code == 303
    assert "O destino mudou" in admin.get(response.headers["location"]).text
    with deps.sessions() as session:
        assert destinations.listing(session) == {}
