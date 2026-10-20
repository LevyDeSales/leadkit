"""Destino do webhook pela costura do pipeline: a entrega resolve o destino na hora do envio (o do
formulário, senão o padrão, senão nenhum), com o segredo vigente, contra um receptor local."""

from __future__ import annotations

import json
import threading
import uuid

import pytest
import sqlalchemy as sa
from conftest import Receiver
from cryptography.fernet import Fernet
from test_admin_destinations import app_logs
from test_pipeline import run_until_finalize

from app import destinations, jobs, pipeline
from app.db import make_engine, make_session_factory
from app.deps import Deps
from app.destinations import SecretCipher
from app.errors import FailureCode, RetryableError
from app.models import AuditLog, Delivery, Job, WebhookDestination, utcnow
from app.settings import Settings


def configure(deps: Deps, form_slug: str | None, url: str) -> str:
    """Cria o destino como o admin cria e devolve o segredo gerado (mostrado uma vez)."""
    with deps.sessions.begin() as session:
        saved = destinations.save(
            session,
            deps.cipher,
            form_slug=form_slug,
            url=url,
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
            purpose="internal",
        )
    assert saved.secret is not None
    return saved.secret


def test_a_delivery_goes_to_the_default_destination_signed_with_its_secret(
    deps: Deps, receiver: Receiver
) -> None:
    secret = configure(deps, None, receiver.url("/padrao"))
    _, deliveries = run_until_finalize(deps)

    assert pipeline.deliver(deps, deliveries[0]) == "succeeded"

    (request,) = receiver.requests
    assert request.path == "/padrao"
    assert request.headers["X-MCC-Event"] == "plano.concluido"
    assert request.headers["Idempotency-Key"] == str(deliveries[0])
    assert request.signed_with(secret)
    assert request.json()["lead"]["email"] == "contadora@example.test"
    with deps.sessions() as session:
        delivery = session.get_one(Delivery, deliveries[0])
    assert (delivery.destination_scope, delivery.destination_host) == ("default", receiver.host)


def test_the_form_destination_takes_the_place_of_the_default(
    deps: Deps, receiver: Receiver
) -> None:
    configure(deps, None, receiver.url("/padrao"))
    demo_secret = configure(deps, "demo", receiver.url("/demo"))
    _, deliveries = run_until_finalize(deps)  # um envio do demo

    pipeline.deliver(deps, deliveries[0])

    (request,) = receiver.requests
    assert request.path == "/demo" and request.signed_with(demo_secret)
    with deps.sessions() as session:
        delivery = session.get_one(Delivery, deliveries[0])
    assert delivery.destination_scope == "form"


def test_a_form_without_its_own_destination_or_a_default_gets_no_delivery(
    deps: Deps, receiver: Receiver
) -> None:
    configure(deps, "mcc", receiver.url("/mcc"))  # só o MCC tem destino

    job_id, deliveries = run_until_finalize(deps)  # um envio do demo

    assert deliveries == []
    with deps.sessions() as session:
        assert session.get_one(Job, job_id).status == "completed"
    assert receiver.requests == []


def test_a_delivery_being_retried_goes_out_with_the_corrected_url_and_the_current_secret(
    deps: Deps, receiver: Receiver
) -> None:
    old_secret = configure(deps, None, receiver.url("/antigo"))
    job_id, deliveries = run_until_finalize(deps)
    receiver.status = 503
    with pytest.raises(RetryableError):
        pipeline.deliver(deps, deliveries[0])

    with deps.sessions.begin() as session:  # a operadora corrige o destino e troca o segredo
        destinations.save(
            session,
            deps.cipher,
            form_slug=None,
            url=receiver.url("/novo"),
            actor="operadora",
            expected_version=1,
            production=False,
            http_confirmed=False,
            purpose="internal",
        )
    with deps.sessions.begin() as session:
        new_secret = destinations.rotate(
            session, deps.cipher, form_slug=None, actor="operadora", expected_version=2
        )
    with pytest.raises(RetryableError):
        pipeline.deliver(deps, deliveries[0])
    pipeline.mark_delivery_failed(deps, deliveries[0], "delivery_unconfigured")
    with deps.sessions.begin() as session:
        assert jobs.resend_delivery(
            session,
            job_id,
            deliveries[0],
            actor="operadora",
            expected_config_version=3,
            expected_attempt=1,
        )
    receiver.status = 204
    assert pipeline.deliver(deps, deliveries[0], dispatch_no=2) == "succeeded"

    first, second = receiver.requests
    assert (first.path, second.path) == ("/antigo", "/novo")
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]
    assert second.signed_with(new_secret) and not second.signed_with(old_secret)


def test_new_delivery_never_falls_back_after_its_pinned_destination_was_removed(
    deps: Deps, receiver: Receiver
) -> None:
    configure(deps, None, receiver.url("/padrao"))
    _, deliveries = run_until_finalize(deps)
    with deps.sessions.begin() as session:
        destinations.remove(session, form_slug=None, actor="operadora", expected_version=1)

    with pytest.raises(RetryableError) as raised:
        pipeline.deliver(deps, deliveries[0])

    assert raised.value.code is FailureCode.DELIVERY_UNCONFIGURED
    with deps.sessions() as session:
        waiting = session.get_one(Delivery, deliveries[0])
    assert (waiting.status, waiting.attempts) == ("pending", 0)
    assert "destino, revisão ou finalidade não autorizados" in waiting.last_error
    assert receiver.requests == []

    configure(deps, None, receiver.url("/novo"))
    with pytest.raises(RetryableError):
        pipeline.deliver(deps, deliveries[0])
    assert receiver.requests == []


def test_a_secret_that_the_key_cannot_open_stops_the_send(deps: Deps, receiver: Receiver) -> None:
    configure(deps, None, receiver.url())
    _, deliveries = run_until_finalize(deps)
    other_key = Fernet.generate_key().decode()
    deps.cipher = SecretCipher((other_key,))  # a chave foi trocada sem manter a antiga

    with pytest.raises(RetryableError) as raised:
        pipeline.deliver(deps, deliveries[0])

    assert raised.value.code is FailureCode.DELIVERY_UNCONFIGURED
    reason = "[delivery_unconfigured] o segredo do destino não abre com WEBHOOK_ENCRYPTION_KEY"
    with deps.sessions() as session:
        waiting = session.get_one(Delivery, deliveries[0])
    assert waiting.last_error == reason
    assert receiver.requests == []

    pipeline.mark_delivery_failed(deps, deliveries[0], "delivery_unconfigured")  # esgotou

    with deps.sessions() as session:
        failed = session.get_one(Delivery, deliveries[0])
    assert (failed.status, failed.last_error) == ("failed", reason)  # o motivo fica


def test_a_resolved_destination_does_not_print_its_secret(deps: Deps, receiver: Receiver) -> None:
    secret = configure(deps, None, receiver.url())

    with deps.sessions() as session:
        destination = destinations.resolve(session, deps.cipher, "demo")

    assert destination is not None and destination.secret == secret
    assert secret not in repr(destination)


def test_database_errors_do_not_carry_the_values_that_caused_them(deps: Deps) -> None:
    """Um erro do banco vai para o log e, numa task, para o Hatchet: sem os parâmetros, ele não
    leva o caminho da URL de um destino, um segredo cifrado ou dado da lead."""
    values = {
        "form_slug": None,
        "url": "https://hooks.example.test/caminho-que-vale-senha",
        "secret_sealed": "segredo-cifrado-de-mentira",
        "version": 1,
        "created_by": "testes",
        "created_at": utcnow(),
        "updated_by": "testes",
        "updated_at": utcnow(),
        "secret_created_at": utcnow(),
    }
    with deps.sessions.begin() as session:
        session.execute(sa.insert(WebhookDestination).values(id=uuid.uuid4(), **values))

    with pytest.raises(sa.exc.IntegrityError) as raised, deps.sessions.begin() as session:
        session.execute(sa.insert(WebhookDestination).values(id=uuid.uuid4(), **values))

    assert "caminho-que-vale-senha" not in str(raised.value)
    assert "segredo-cifrado-de-mentira" not in str(raised.value)


# --- subida da API e do worker ----------------------------------------------------------------

ENV_SECRET = "segredo-falso-que-o-receptor-ja-conhece"


def with_env_webhook(deps: Deps, url: str, **env: str) -> None:
    """O ambiente de antes do destino no admin: WEBHOOK_URL e WEBHOOK_SECRET no app.env."""
    deps.settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        WEBHOOK_URL=url,
        WEBHOOK_SECRET=ENV_SECRET,
        WEBHOOK_PURPOSE="internal",
        **env,
    )


def default_url(deps: Deps) -> str | None:
    with deps.sessions() as session:
        default = destinations.listing(session).get(None)
    return default.url if default else None


def test_on_the_first_start_the_env_webhook_becomes_the_default_destination(
    deps: Deps, receiver: Receiver
) -> None:
    with_env_webhook(deps, receiver.url("/env"))

    destinations.prepare(deps)

    assert default_url(deps) == receiver.url("/env")
    _, deliveries = run_until_finalize(deps)
    pipeline.deliver(deps, deliveries[0])
    (request,) = receiver.requests
    assert request.signed_with(ENV_SECRET)  # o receptor de hoje continua valendo
    with deps.sessions() as session:
        (audit,) = session.scalars(sa.select(AuditLog))
    assert (audit.actor, audit.action) == ("ambiente", "webhook_destination.create")
    assert ENV_SECRET not in json.dumps(audit.details)


def test_after_the_first_start_the_env_is_ignored(deps: Deps, receiver: Receiver) -> None:
    with_env_webhook(deps, receiver.url("/env"))
    destinations.prepare(deps)
    with deps.sessions.begin() as session:  # o dono tira o destino padrão no admin
        destinations.remove(session, form_slug=None, actor="operadora", expected_version=1)

    destinations.prepare(deps)  # a próxima subida, com o WEBHOOK_URL ainda no app.env

    assert default_url(deps) is None


def test_a_first_start_without_the_env_webhook_also_leaves_it_ignored(deps: Deps) -> None:
    destinations.prepare(deps)  # subiu sem WEBHOOK_URL
    with_env_webhook(deps, "https://hooks.example.test/planos")

    destinations.prepare(deps)

    assert default_url(deps) is None


def test_a_default_set_in_the_admin_wins_over_the_env(deps: Deps, receiver: Receiver) -> None:
    # A API subiu antes da migration, e o dono configurou o padrão no admin.
    configure(deps, None, receiver.url("/admin"))
    with_env_webhook(deps, receiver.url("/env"))

    destinations.prepare(deps)

    assert default_url(deps) == receiver.url("/admin")


def test_the_worker_takes_the_env_webhook_before_taking_tasks(
    deps: Deps, receiver: Receiver
) -> None:
    from app.worker import ready_deps

    with_env_webhook(deps, receiver.url("/env"))

    assert ready_deps() is deps
    assert default_url(deps) == receiver.url("/env")


def test_the_api_and_the_worker_starting_together_seed_the_default_once(
    deps: Deps, receiver: Receiver
) -> None:
    with_env_webhook(deps, receiver.url("/env"))
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def start() -> None:
        barrier.wait()
        try:
            destinations.prepare(deps)
        except BaseException as exc:  # noqa: BLE001 (o teste conta qualquer falha)
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert default_url(deps) == receiver.url("/env")
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(AuditLog)) == 1


def test_the_env_webhook_without_the_key_stops_the_first_start(
    deps: Deps, receiver: Receiver
) -> None:
    with_env_webhook(deps, receiver.url("/env"))
    cipher, deps.cipher = deps.cipher, None

    with pytest.raises(destinations.StartupError, match="WEBHOOK_ENCRYPTION_KEY"):
        destinations.prepare(deps)

    deps.cipher = cipher  # com a chave no ambiente, a subida seguinte importa
    destinations.prepare(deps)
    assert default_url(deps) == receiver.url("/env")


def test_an_env_url_the_admin_would_refuse_stops_the_first_start(deps: Deps) -> None:
    with_env_webhook(deps, "https://nome-de-usuario-falso@hooks.example.test/planos")

    with pytest.raises(destinations.StartupError, match="WEBHOOK_URL") as raised:
        destinations.prepare(deps)

    assert "nome-de-usuario-falso" not in str(raised.value)
    assert default_url(deps) is None


def test_in_production_a_key_that_opens_no_secret_stops_the_start(
    deps: Deps, receiver: Receiver
) -> None:
    configure(deps, None, receiver.url("/padrao"))
    configure(deps, "mcc", receiver.url("/mcc"))
    deps.settings = Settings(_env_file=None, APP_ENV="production")  # type: ignore[call-arg]
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))  # a chave errada

    with pytest.raises(destinations.StartupError, match="nenhum"):
        destinations.prepare(deps)


def test_in_production_a_key_that_opens_some_secrets_only_warns(
    deps: Deps,
    receiver: Receiver,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_logs(monkeypatch, caplog)
    configure(deps, None, receiver.url("/padrao"))  # na chave de antes
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    configure(deps, "mcc", receiver.url("/mcc"))  # na chave de agora
    deps.settings = Settings(_env_file=None, APP_ENV="production")  # type: ignore[call-arg]

    destinations.prepare(deps)

    assert "formulario=(padrão): o segredo não abre" in caplog.text


def test_in_production_a_destination_without_the_key_stops_the_start(
    deps: Deps, receiver: Receiver
) -> None:
    deps.settings = Settings(_env_file=None, APP_ENV="production")  # type: ignore[call-arg]
    deps.cipher = None
    destinations.prepare(deps)  # sem destino, a chave não faz falta
    deps.cipher = SecretCipher.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]
    configure(deps, None, receiver.url())
    deps.cipher = None

    with pytest.raises(destinations.StartupError, match="WEBHOOK_ENCRYPTION_KEY"):
        destinations.prepare(deps)


def test_after_a_new_key_comes_first_the_secrets_are_sealed_with_it(
    deps: Deps, receiver: Receiver
) -> None:
    secret = configure(deps, None, receiver.url())
    (old_key,) = deps.settings.encryption_keys
    new_key = Fernet.generate_key().decode()
    deps.cipher = SecretCipher((new_key, old_key))  # WEBHOOK_ENCRYPTION_KEY=nova,antiga

    destinations.prepare(deps)  # a subida recifra com a chave nova

    deps.cipher = SecretCipher((new_key,))  # a antiga já pode sair do ambiente
    _, deliveries = run_until_finalize(deps)
    pipeline.deliver(deps, deliveries[0])
    assert receiver.requests[0].signed_with(secret)


def test_the_start_logs_say_what_happened_without_any_secret(
    deps: Deps,
    receiver: Receiver,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_logs(monkeypatch, caplog)
    with_env_webhook(deps, receiver.url("/env"))
    destinations.prepare(deps)  # a primeira subida importa o WEBHOOK_URL
    mcc_secret = configure(deps, "mcc", receiver.url("/mcc"))

    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))  # outra chave, sem a antiga
    destinations.prepare(deps)
    deps.cipher = None  # em desenvolvimento, sem a chave, a subida só avisa
    destinations.prepare(deps)

    assert f"o WEBHOOK_URL virou o destino padrão host={receiver.host}" in caplog.text
    assert "formulario=mcc: o segredo não abre com WEBHOOK_ENCRYPTION_KEY" in caplog.text
    assert "formulario=(padrão): o segredo não abre" in caplog.text
    assert "sem WEBHOOK_ENCRYPTION_KEY, nada é entregue" in caplog.text
    assert ENV_SECRET not in caplog.text and mcc_secret not in caplog.text


def test_a_start_before_the_migration_leaves_the_seed_for_the_next_one(
    deps: Deps, database_url: str
) -> None:
    """No primeiro deploy da stack do Swarm, a API e o worker sobem antes das migrations."""
    engine = make_engine(database_url)
    with engine.begin() as connection:
        connection.execute(sa.text("create schema if not exists t03_antes_da_migration"))
    before = sa.create_engine(
        database_url,
        connect_args={"options": "-csearch_path=t03_antes_da_migration", "prepare_threshold": None},
    )
    try:
        with_env_webhook(deps, "https://hooks.example.test/planos")
        deps.sessions = make_session_factory(before)
        destinations.prepare(deps)  # sem as tabelas: não quebra a subida
    finally:
        before.dispose()
        with engine.begin() as connection:
            connection.execute(sa.text("drop schema t03_antes_da_migration cascade"))
        engine.dispose()


@pytest.mark.parametrize(
    "url",
    ["https://[::1", "https://hooks.example.test℀/planos", "http://[fe80::1/planos"],
    ids=["open-bracket", "nfkc-netloc", "unclosed-ipv6"],
)
def test_a_url_the_parser_cannot_split_is_a_refusal_and_not_a_crash(url: str) -> None:
    with pytest.raises(destinations.InvalidUrl) as refused:
        destinations.check_url(url, production=False, http_confirmed=False)

    assert refused.value.message == "A URL não é válida." and not refused.value.needs_confirmation


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        (
            "https://hooks.example.test/planos/caminho-que-vale-senha?chave=1",
            "https://hooks.example.test",
        ),
        ("http://127.0.0.1:8080/hook", "http://127.0.0.1:8080"),
        ("https://[2001:db8::1]:8443/x", "https://[2001:db8::1]:8443"),
        ("HTTPS://hooks.example.test", "https://hooks.example.test"),
        ("https://hooks.example.test", "https://hooks.example.test"),
    ],
)
def test_the_origin_of_a_url_is_the_scheme_and_the_host_without_the_path(
    url: str, origin: str
) -> None:
    assert destinations.origin_of(url) == origin
    assert "caminho-que-vale-senha" not in destinations.origin_of(url)
