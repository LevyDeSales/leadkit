"""A integração com o Baserow de ponta a ponta no motor, contra o Postgres de teste e o Baserow de
mentira: a configuração, o envio nascendo com o pedido concluído, o upsert, o replay, a falha e a
nova tentativa, o reenvio e o token que nunca aparece."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from conftest import (
    BASEROW_TABLE,
    BASEROW_TOKEN,
    BASEROW_URL,
    DEMO_ANSWERS,
    FakeBaserow,
    baserow_field,
    demo_baserow_fields,
)
from cryptography.fernet import Fernet
from hatchet_sdk.exceptions import TaskRunError
from test_admin import finish
from test_intake import submit
from test_integration_mapping import FULL_MAPPING
from test_retention import LEAD_TRACES, lead_traces, stored_rows

import app.workflows as wf
from app import crons, jobs, pipeline, retention
from app.deps import Deps
from app.destinations import NoCipher, SecretCipher, UnreadableSecret
from app.errors import FailureCode, RetryableError, TerminalError
from app.integrations import config, push, syncs
from app.integrations.mapping import Entry
from app.models import Artifact, AuditLog, Delivery, Integration, IntegrationSync, Job, utcnow

SLUG = "demo"


def connect_form(
    deps: Deps, *, slug: str = SLUG, entries: list[Entry] | None = None, active: bool = True
) -> config.StoredIntegration:
    """O que a operadora faz no admin, direto no banco: conecta, mapeia e ativa."""
    with deps.sessions.begin() as session:
        stored = config.connect(
            session,
            deps.cipher,
            form_slug=slug,
            base_url=BASEROW_URL,
            table_id=BASEROW_TABLE,
            token=BASEROW_TOKEN,
            actor="operadora",
            expected_version=0,
            production=False,
            purpose="internal",
        )
        stored = config.save_mapping(
            session,
            form_slug=slug,
            entries=FULL_MAPPING if entries is None else entries,
            actor="operadora",
            expected_version=stored.version,
            deactivate=False,
        )
        if active:
            stored = config.set_active(
                session,
                form_slug=slug,
                active=True,
                actor="operadora",
                expected_version=stored.version,
            )
    return stored


def sync_ids(deps: Deps, job_id: uuid.UUID | None = None) -> list[uuid.UUID]:
    """Os envios, do mais antigo ao mais novo (os de um pedido, se vier)."""
    statement = sa.select(IntegrationSync.id).order_by(IntegrationSync.created_at)
    if job_id is not None:
        statement = statement.where(IntegrationSync.job_id == job_id)
    with deps.sessions() as session:
        return list(session.scalars(statement))


def sync_state(deps: Deps, sync_id: uuid.UUID) -> IntegrationSync:
    with deps.sessions() as session:
        found = session.get_one(IntegrationSync, sync_id)
        session.expunge(found)
        return found


def completed_job(deps: Deps, key: str = "k1", **answers: Any) -> Job:
    """Um pedido do demo concluído até o fim do pipeline (o que cria os envios)."""
    email = f"{key}@example.test"
    job = submit(deps, key=key, answers={**DEMO_ANSWERS, **answers, "email": email}).job
    finish(deps, job.id)
    return job


@pytest.fixture
def baserow(deps: Deps) -> FakeBaserow:
    return FakeBaserow().install(deps)


# --- a configuração ---------------------------------------------------------------------------


def test_the_token_is_sealed_with_the_webhook_key_and_never_leaves_the_database_whole(
    deps: Deps,
) -> None:
    stored = connect_form(deps, active=False)

    with deps.sessions() as session:
        sealed = session.scalar(sa.select(Integration.token_sealed))
        opened = config.stored_connection(session, deps.cipher, SLUG)
    assert sealed is not None and sealed != BASEROW_TOKEN
    assert opened is not None and opened.token == BASEROW_TOKEN
    assert BASEROW_TOKEN not in repr(opened) and BASEROW_TOKEN not in repr(stored)
    assert not hasattr(stored, "token_sealed") and not hasattr(stored, "token")
    everything = "\n".join(row for rows in stored_rows(deps).values() for row in rows)
    assert BASEROW_TOKEN not in everything  # nem no banco, nem na auditoria


def test_every_change_is_audited_in_the_name_of_who_made_it_without_the_token_or_the_path(
    deps: Deps,
) -> None:
    connect_form(deps)

    with deps.sessions() as session:
        audited = list(session.scalars(sa.select(AuditLog).order_by(AuditLog.at)))
    assert [row.action for row in audited] == [
        "integration.create",
        "integration.mapping",
        "integration.activate",
    ]
    assert {row.actor for row in audited} == {"operadora"}
    assert audited[0].details == {
        "form_slug": SLUG,
        "kind": "baserow",
        "host": "baserow.example.test",
        "table_id": BASEROW_TABLE,
        "token_changed": True,
    }
    assert audited[1].details["fields"] == len(FULL_MAPPING)
    assert BASEROW_TOKEN not in json.dumps([row.details for row in audited])


def test_connecting_again_keeps_the_token_when_none_is_given_and_resets_the_mapping_on_a_new_table(
    deps: Deps,
) -> None:
    stored = connect_form(deps)

    def reconnect(version: int, **changes: Any) -> config.StoredIntegration:
        values: dict[str, Any] = {
            "base_url": BASEROW_URL,
            "table_id": BASEROW_TABLE,
            "token": "",
            **changes,
        }
        with deps.sessions.begin() as session:
            return config.connect(
                session,
                deps.cipher,
                form_slug=SLUG,
                actor="outra-operadora",
                expected_version=version,
                production=False,
                **values,
                purpose="internal",
            )

    same = reconnect(stored.version)
    assert same.entries == stored.entries and same.active and same.key_field_id == 104
    with deps.sessions() as session:
        kept = config.stored_connection(session, deps.cipher, SLUG)
    assert kept is not None and kept.token == BASEROW_TOKEN

    new_token = "token-novo-so-do-teste"
    swapped = reconnect(same.version, token=new_token)
    assert swapped.token_changed_at > stored.token_changed_at and swapped.entries == stored.entries
    with deps.sessions() as session:
        replaced = config.stored_connection(session, deps.cipher, SLUG)
    assert replaced is not None and replaced.token == new_token

    moved = reconnect(swapped.version, table_id=99)
    assert (moved.entries, moved.key_field_id, moved.active, moved.table_id) == (
        (),
        None,
        False,
        99,
    )
    with deps.sessions() as session:
        details = [
            row.details for row in session.scalars(sa.select(AuditLog).order_by(AuditLog.at))
        ][-3:]
    assert [d.get("token_changed") for d in details] == [False, True, False]
    assert [d.get("mapping_reset") for d in details] == [False, False, True]


def test_a_page_opened_before_a_change_is_refused(deps: Deps) -> None:
    stored = connect_form(deps)

    with deps.sessions.begin() as session, pytest.raises(config.Conflict) as raised:
        config.set_active(
            session, form_slug=SLUG, active=False, actor="x", expected_version=stored.version - 1
        )

    assert raised.value.current is not None and raised.value.current.version == stored.version


def test_the_first_connection_needs_the_token_and_the_key(deps: Deps) -> None:
    with deps.sessions.begin() as session, pytest.raises(config.MissingToken):
        config.connect(
            session,
            deps.cipher,
            form_slug=SLUG,
            base_url=BASEROW_URL,
            table_id=1,
            token="  ",
            actor="x",
            expected_version=0,
            production=False,
            purpose="internal",
        )
    deps.cipher = None
    with deps.sessions.begin() as session, pytest.raises(NoCipher):
        config.connect(
            session,
            deps.cipher,
            form_slug=SLUG,
            base_url=BASEROW_URL,
            table_id=1,
            token="x",
            actor="x",
            expected_version=0,
            production=False,
            purpose="internal",
        )


@pytest.mark.parametrize(
    ("url", "production", "message"),
    [
        ("", False, "Informe"),
        ("baserow.example.test", False, "https://"),
        ("ftp://baserow.example.test", False, "https://"),
        ("https://usuario:senha@baserow.example.test", False, "usuário e a senha"),
        ("https:///x", False, "endereço do servidor"),
        ("https://baserow.example.test/?a=1", False, "só o endereço"),
        ("https://baserow.example.test/#x", False, "só o endereço"),
        ("https://baserow.example.test:99999", False, "porta"),
        ("https://baserow .example.test", False, "espaços"),
        ("http://169.254.169.254", False, "link-local"),
        ("http://[fe80::1]", False, "link-local"),
        ("http://baserow.example.test", True, "Em produção, use https"),
        ("https://baserow.example.test/" + "a" * 2000, False, "2000 caracteres"),
        # `urlsplit` levanta ValueError nestes: era um 500, e agora é uma recusa como as outras
        ("https://[::1", False, "não é válido"),
        ("https://[fe80::1", False, "não é válido"),
        ("https://[::1]]", False, "não é válido"),
        ("https://baserow.example.test℀", False, "não é válido"),  # NFKC vira `a/c`
    ],
)
def test_only_a_plain_https_address_without_credentials_is_accepted(
    url: str, production: bool, message: str
) -> None:
    with pytest.raises(config.InvalidBaseUrl, match=message):
        config.check_base_url(url, production=production)


def test_a_malformed_address_message_does_not_repeat_the_address() -> None:
    with pytest.raises(config.InvalidBaseUrl) as refused:
        config.check_base_url("https://[::1-segredo-do-teste", production=False)

    assert "segredo-do-teste" not in str(refused.value)


def test_the_address_is_kept_without_the_trailing_slash_and_http_works_outside_production() -> None:
    assert config.check_base_url(" https://baserow.example.test/ ", production=True) == (
        "https://baserow.example.test"
    )
    assert config.check_base_url("http://localhost:8080", production=False) == (
        "http://localhost:8080"
    )


def test_a_token_sealed_with_the_old_key_is_sealed_again_with_the_first_one_at_startup(
    deps: Deps,
) -> None:
    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    deps.cipher = SecretCipher((old_key,))
    connect_form(deps)
    # A chave nova na frente, a antiga ainda aceita: o segundo deploy da troca.
    deps.cipher = SecretCipher((new_key, old_key))

    config.prepare(deps)

    with deps.sessions() as session:
        sealed = session.scalar(sa.select(Integration.token_sealed))
    assert sealed is not None
    assert SecretCipher((new_key,)).open(sealed) == BASEROW_TOKEN  # abre só com a nova


def test_a_token_no_key_opens_is_logged_by_slug_only(
    deps: Deps, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_form(deps)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))
    monkeypatch.setattr(logging.getLogger("app.integrations.config"), "disabled", False)

    with caplog.at_level(logging.INFO, logger="app.integrations.config"):
        config.prepare(deps)

    assert f"formulario={SLUG}" in caplog.text and "não abre" in caplog.text
    assert BASEROW_TOKEN not in caplog.text
    with deps.sessions() as session:
        assert config.token_unreadable(session, deps.cipher, SLUG)


# --- o envio nasce com o pedido concluído -----------------------------------------------------


def test_a_completed_request_creates_one_pending_sync_for_the_active_integration(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)

    job = completed_job(deps)

    (sync_id,) = sync_ids(deps)
    state = sync_state(deps, sync_id)
    assert (state.job_id, state.status, state.attempts) == (job.id, "pending", 0)
    assert state.hatchet_run_id is None and baserow.calls == []  # nada vai à planilha ainda


def test_the_sync_exists_without_a_webhook_destination(deps: Deps, baserow: FakeBaserow) -> None:
    connect_form(deps)

    completed_job(deps)

    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Delivery)) == 0
    assert len(sync_ids(deps)) == 1


def test_an_inactive_integration_and_another_form_get_no_sync(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    connect_form(deps, slug="mcc")

    completed_job(deps)  # o demo: integração desligada; a ativa é a do mcc

    assert sync_ids(deps) == []


def test_finalizing_twice_does_not_create_a_second_sync(deps: Deps, baserow: FakeBaserow) -> None:
    connect_form(deps)
    job = completed_job(deps)
    with deps.sessions() as session:
        artifact_id = session.scalars(sa.select(Artifact.id)).one()

    pipeline.finalize(deps, job.id, 1, artifact_id)

    assert len(sync_ids(deps)) == 1


def test_the_finalize_task_launches_the_sync_and_reports_only_counts(
    deps: Deps, baserow: FakeBaserow, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_form(deps)
    launched: list[uuid.UUID] = []
    monkeypatch.setattr(wf, "launch_sync", lambda sync_id: launched.append(sync_id) or "run-s")
    job = submit(deps).job
    payload = wf.GeneratePlanInput.for_attempt(job.id, 1)
    claimed = wf.claim.mock_run(input=payload)
    planned = wf.plan.mock_run(
        input=payload, parent_outputs={"claim": claimed.model_dump(mode="json")}
    )
    rendered = wf.render.mock_run(
        input=payload,
        parent_outputs={
            "claim": claimed.model_dump(mode="json"),
            "plan": planned.model_dump(mode="json"),
        },
    )

    finalized = wf.finalize.mock_run(
        input=payload,
        parent_outputs={
            "claim": claimed.model_dump(mode="json"),
            "render": rendered.model_dump(mode="json"),
        },
    )

    assert launched == sync_ids(deps)
    assert (finalized.syncs, finalized.syncs_triggered) == (1, 1)
    assert set(finalized.model_dump()) == {
        "completed",
        "deliveries",
        "deliveries_triggered",
        "syncs",
        "syncs_triggered",
    }


# --- o upsert ---------------------------------------------------------------------------------


def test_the_sync_creates_the_row_with_each_value_where_the_mapping_puts_it(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    job = completed_job(deps)
    (sync_id,) = sync_ids(deps)

    assert push.push(deps, sync_id) == "succeeded"

    state = sync_state(deps, sync_id)
    assert (state.status, state.outcome, state.attempts, state.last_error) == (
        "succeeded",
        "created",
        1,
        None,
    )
    row = baserow.row_where(104, job.public_id)
    assert state.row_id == row["id"] and state.synced_at is not None
    assert row["field_101"] == "Contadora Exemplo"
    assert row["field_102"] == "k1@example.test"
    assert row["field_103"] == ""  # o demo não pede telefone
    assert json.loads(row["field_105"])["tamanho_equipe"] == "2_a_5"
    assert row["field_106"].startswith(f"http://testserver/v1/jobs/{job.public_id}/pdf?t=")
    assert row["field_107"] is True
    assert row["field_108"] == 10802  # "De 2 a 5 pessoas", pelo rótulo
    assert row["field_109"] == 10901  # "Atrair novos clientes"
    assert baserow.calls_to("POST") and "field_110" not in row  # o campo somente leitura fica fora


def test_a_second_sync_of_the_same_request_updates_the_row_instead_of_duplicating_it(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    job = completed_job(deps)
    (first,) = sync_ids(deps)
    push.push(deps, first)
    with deps.sessions.begin() as session:  # a operadora reprocessa o pedido
        assert jobs.reprocess(session, job.id, actor="operadora", reason="plano refeito do zero")
    finish(deps, job.id, 2)
    second = next(item for item in sync_ids(deps, job.id) if item != first)

    assert push.push(deps, second) == "succeeded"

    assert len(baserow.rows) == 1
    assert sync_state(deps, second).outcome == "updated"
    assert sync_state(deps, second).row_id == sync_state(deps, first).row_id


def test_each_sync_of_a_request_says_the_attempt_of_its_own_pdf(deps: Deps) -> None:
    """Reprocessar gera um PDF e uma sincronização novos: a primeira segue sendo da tentativa 1, e
    não da tentativa atual do pedido."""
    connect_form(deps)
    job = completed_job(deps)
    with deps.sessions.begin() as session:
        assert jobs.reprocess(session, job.id, actor="operadora", reason="plano refeito do zero")
    finish(deps, job.id, 2)

    with deps.sessions() as session:
        rows = syncs.for_job(session, job.id)  # da mais recente à mais antiga

    assert [row.job_attempt for row in rows] == [2, 1]


def test_pushing_a_sync_that_already_succeeded_does_nothing(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)
    push.push(deps, sync_id)
    calls = len(baserow.calls)

    assert push.push(deps, sync_id) == "already_succeeded"

    assert len(baserow.calls) == calls and sync_state(deps, sync_id).attempts == 1


def test_a_replayed_task_run_after_the_row_exists_updates_it(
    deps: Deps, baserow: FakeBaserow
) -> None:
    """O Hatchet entrega pelo menos uma vez: o envio que escreveu a linha mas não gravou o estado
    roda de novo e encontra a linha."""
    connect_form(deps)
    job = completed_job(deps)
    (sync_id,) = sync_ids(deps)
    push.push(deps, sync_id)
    with deps.sessions.begin() as session:  # o estado não foi gravado antes da queda
        session.execute(
            sa.update(IntegrationSync).values(status="pending", outcome=None, row_id=None)
        )

    push.push(deps, sync_id)

    assert len(baserow.rows) == 1 and baserow.row_where(104, job.public_id)
    assert sync_state(deps, sync_id).outcome == "updated"


def test_a_failure_is_recorded_on_the_sync_and_the_next_attempt_succeeds(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    job = completed_job(deps)
    (sync_id,) = sync_ids(deps)
    baserow.fail("POST", 503)

    with pytest.raises(RetryableError) as raised:
        push.push(deps, sync_id)

    assert raised.value.code is FailureCode.INTEGRATION_UNAVAILABLE
    state = sync_state(deps, sync_id)
    assert (state.status, state.attempts) == ("pending", 1)
    assert state.last_error == "[integration_unavailable] criar a linha: HTTP 503"
    assert baserow.rows == {}

    assert push.push(deps, sync_id) == "succeeded"  # o Hatchet tenta de novo

    state = sync_state(deps, sync_id)
    assert (state.status, state.attempts, state.last_error) == ("succeeded", 2, None)
    assert len(baserow.rows) == 1 and baserow.row_where(104, job.public_id)


def test_a_connection_failure_is_retried_and_recorded_with_the_error_type_only(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)
    baserow.fail("GET", httpx.ReadTimeout("demorou demais"), where="fields")

    with pytest.raises(RetryableError):
        push.push(deps, sync_id)

    assert (
        sync_state(deps, sync_id).last_error
        == "[integration_unavailable] ler os campos: ReadTimeout"
    )


def test_an_option_without_a_pair_fails_the_sync_without_writing_and_resending_after_the_fix_works(
    deps: Deps, baserow: FakeBaserow
) -> None:
    fields = demo_baserow_fields()
    fields[7] = baserow_field(
        108, "Equipe", "single_select", options=("Só eu", "De 6 a 15 pessoas")
    )
    baserow.fields = fields
    connect_form(deps)
    job = completed_job(deps)  # "De 2 a 5 pessoas": a tabela não tem
    (sync_id,) = sync_ids(deps)

    with pytest.raises(TerminalError) as raised:
        push.push(deps, sync_id)

    assert raised.value.code is FailureCode.INTEGRATION_MAPPING
    message = sync_state(deps, sync_id).last_error or ""
    assert "«Equipe»" in message and "Quantas pessoas" in message
    assert "2_a_5" not in message and "De 2 a 5" not in message  # nem a opção da lead
    assert baserow.calls_to("POST") == [] and baserow.calls_to("PATCH") == []

    baserow.fields = demo_baserow_fields()  # a operadora acrescentou a opção no Baserow
    with deps.sessions.begin() as session:
        syncs.mark_failed(session, sync_id, "integration_mapping")  # o Hatchet desistiu
    assert sync_state(deps, sync_id).status == "failed"
    with deps.sessions.begin() as session:
        assert syncs.resend(session, job.id, sync_id, actor="operadora")
    assert push.push(deps, sync_id) == "succeeded"
    assert len(baserow.rows) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_fails_the_sync_for_good_without_exposing_it(
    deps: Deps, baserow: FakeBaserow, status: int
) -> None:
    connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)
    baserow.fail("GET", status, where="fields", body={"detail": f"Token {BASEROW_TOKEN}"})

    with pytest.raises(TerminalError) as raised:
        push.push(deps, sync_id)

    assert raised.value.code is FailureCode.INTEGRATION_REJECTED
    assert BASEROW_TOKEN not in (sync_state(deps, sync_id).last_error or "")


def test_a_field_that_left_the_table_fails_the_sync_naming_the_field(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)
    baserow.fields = [item for item in baserow.fields if item["id"] != 106]

    with pytest.raises(TerminalError, match="«PDF» não existe mais"):
        push.push(deps, sync_id)

    assert baserow.rows == {}


def test_a_value_baserow_validates_against_fails_with_the_field_ids_and_no_values(
    deps: Deps, baserow: FakeBaserow
) -> None:
    # Uma fonte de texto pode ser mapeada para e-mail, mas o Baserow ainda valida seu conteúdo.
    entries = [
        replace(entry, source="lead.name") if entry.field_id == 102 else entry
        for entry in FULL_MAPPING
    ]
    connect_form(deps, entries=entries)
    completed_job(deps, nome="sem-arroba")
    (sync_id,) = sync_ids(deps)

    with pytest.raises(TerminalError):
        push.push(deps, sync_id)

    error = sync_state(deps, sync_id).last_error or ""
    assert "ERROR_REQUEST_BODY_VALIDATION" in error and "field_102" in error
    assert "sem-arroba" not in error


def test_an_inactive_integration_makes_the_sync_wait_and_reactivating_it_saves_the_sync(
    deps: Deps, baserow: FakeBaserow
) -> None:
    stored = connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)
    with deps.sessions.begin() as session:
        stored = config.set_active(
            session,
            form_slug=SLUG,
            active=False,
            actor="operadora",
            expected_version=stored.version,
        )

    with pytest.raises(RetryableError) as raised:
        push.push(deps, sync_id)

    assert raised.value.code is FailureCode.INTEGRATION_UNCONFIGURED
    assert baserow.calls == []
    with deps.sessions.begin() as session:
        config.set_active(
            session, form_slug=SLUG, active=True, actor="operadora", expected_version=stored.version
        )
    assert push.push(deps, sync_id) == "succeeded"


def test_a_token_the_key_does_not_open_makes_the_sync_wait_without_calling_baserow(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))

    with pytest.raises(RetryableError, match="token da integração não abre"):
        push.push(deps, sync_id)

    assert baserow.calls == []
    deps.cipher = None
    with pytest.raises(RetryableError, match="token da integração não abre"):
        push.push(deps, sync_id)


def test_the_mapping_without_the_key_stops_the_sync(deps: Deps, baserow: FakeBaserow) -> None:
    connect_form(deps, entries=[], active=True)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)

    with pytest.raises(TerminalError, match="sem o código do pedido"):
        push.push(deps, sync_id)


def test_two_syncs_of_the_same_request_never_create_two_rows(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    job = completed_job(deps)
    with deps.sessions.begin() as session:
        jobs.reprocess(session, job.id, actor="operadora", reason="plano refeito do zero")
    finish(deps, job.id, 2)
    first, second = sync_ids(deps, job.id)
    gate, entered, finds = threading.Event(), threading.Event(), []

    def hold_the_first_search(request: httpx.Request) -> None:
        if request.method == "GET" and "rows/table" in request.url.path:
            finds.append(threading.current_thread().name)
            if not entered.is_set():
                entered.set()
                assert gate.wait(10)

    baserow.before = hold_the_first_search
    threads = [threading.Thread(target=push.push, args=(deps, item)) for item in (first, second)]
    threads[0].start()
    assert entered.wait(10)
    threads[1].start()
    time.sleep(0.5)  # o segundo já leu os campos e espera a trava do primeiro
    assert len(finds) == 1
    gate.set()
    for thread in threads:
        thread.join(10)

    assert len(baserow.rows) == 1
    assert sorted(sync_state(deps, item).outcome or "" for item in (first, second)) == [
        "created",
        "updated",
    ]


def test_a_multiple_select_and_the_labels_of_the_version_the_lead_answered(
    deps: Deps, baserow: FakeBaserow
) -> None:
    """Uma versão nova do formulário muda um rótulo: o pedido antigo ainda casa pelo que a lead viu."""
    from conftest import legacy_definition, publish
    from test_integration_mapping import SOURCES_QUESTION

    from app.forms.definition import compile_definition

    baserow.fields = [
        *demo_baserow_fields(),
        baserow_field(111, "Fontes", "multiple_select", options=("Indicação", "Instagram")),
    ]
    entries = [*FULL_MAPPING, Entry(111, "Fontes", "multiple_select", "answer:fontes")]
    connect_form(deps, entries=entries)
    document = legacy_definition("demo")
    document["steps"][1]["questions"].append(dict(SOURCES_QUESTION))
    v2 = compile_definition(document, version_number=2)
    publish(deps, v2)
    job = submit(
        deps,
        key="fontes",
        form=v2,
        answers={
            "email": "fontes@example.test",
            "nome": "Contadora Exemplo",
            "escritorio": "Escritório Exemplo",
            "tamanho_equipe": "2_a_5",
            "principal_desafio": "captacao",
            "objetivo_90_dias": "Organizar a captação de clientes e medir a conversão.",
            "fontes": ["indicacao", "instagram"],
        },
    ).job
    finish(deps, job.id)
    (sync_id,) = sync_ids(deps)

    push.push(deps, sync_id)

    assert baserow.row_where(104, job.public_id)["field_111"] == [11101, 11102]


# --- o teste ----------------------------------------------------------------------------------


def test_the_sample_send_writes_one_synthetic_row_that_testing_again_updates(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)  # dá para testar antes de ativar
    submit(deps)  # há uma lead de verdade no banco, e nada dela pode ir no teste

    first = push.send_sample(deps, SLUG, actor="operadora")
    second = push.send_sample(deps, SLUG, actor="operadora")

    assert first is not None and first.ok and first.outcome == "created"
    assert second is not None and second.ok and second.outcome == "updated"
    assert first.row_id == second.row_id and len(baserow.rows) == 1
    row = baserow.row_where(104, "teste-demo")
    assert row["field_102"] == "teste@example.test" and row["field_101"] == "Contadora Exemplo"
    assert row["field_108"] == 10801  # a primeira opção da pergunta
    assert not any(trace in json.dumps(row) for trace in LEAD_TRACES[:1])
    with deps.sessions() as session:
        tests = list(
            session.scalars(sa.select(AuditLog).where(AuditLog.action == "integration.test"))
        )
    assert [row.details["outcome"] for row in tests] == ["created", "updated"]
    assert sync_ids(deps) == []  # o teste não cria envio


def test_the_sample_send_reports_the_failure_and_audits_it(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps, active=False)
    baserow.fail("GET", 401, where="fields")

    result = push.send_sample(deps, SLUG, actor="operadora")

    assert result is not None and not result.ok and "HTTP 401" in (result.error or "")
    with deps.sessions() as session:
        audited = session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "integration.test")
        ).one()
    assert audited.details["ok"] is False and "HTTP 401" in audited.details["error"]


def test_the_sample_send_without_an_integration_or_with_an_unreadable_token(deps: Deps) -> None:
    assert push.send_sample(deps, SLUG, actor="operadora") is None
    connect_form(deps, active=False)
    deps.cipher = SecretCipher((Fernet.generate_key().decode(),))

    with pytest.raises(UnreadableSecret):
        push.send_sample(deps, SLUG, actor="operadora")


# --- reenvio e reconciliador ------------------------------------------------------------------


def test_launching_a_sync_sends_only_its_id_and_stores_the_run_id(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)
    seen: list[tuple[Any, dict[str, Any]]] = []

    class Ref:
        workflow_run_id = "run-9"

    def run(payload: Any, **kwargs: Any) -> Ref:
        seen.append((payload, kwargs))
        return Ref()

    monkeypatch.setattr(wf.sync_integration, "run", run)

    assert wf.launch_sync(sync_id) == "run-9"

    payload, kwargs = seen[0]
    assert payload == wf.SyncInput.for_sync(sync_id)
    assert kwargs["additional_metadata"] == {"sync_id": str(sync_id)}  # só o id
    assert sync_state(deps, sync_id).hatchet_run_id == "run-9"
    with deps.sessions() as session:  # disparada, não aparece mais como pendente de disparo
        assert syncs.unlaunched(session, older_than=timedelta(0), limit=10) == []


def test_launching_a_sync_with_the_engine_down_returns_none_and_leaves_it_for_the_reconciler(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)

    def down(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError("engine fora do ar")

    monkeypatch.setattr(wf.sync_integration, "run", down)

    assert wf.launch_sync(sync_id) is None
    assert sync_state(deps, sync_id).hatchet_run_id is None


def test_only_a_failed_sync_can_be_resent_and_it_goes_back_to_pending(deps: Deps) -> None:
    connect_form(deps)
    job = completed_job(deps)
    (sync_id,) = sync_ids(deps)

    with deps.sessions.begin() as session:
        assert not syncs.resend(session, job.id, sync_id, actor="operadora")  # ainda pendente
        syncs.mark_failed(session, sync_id, "integration_unavailable")
    assert sync_state(deps, sync_id).status == "failed"
    assert sync_state(deps, sync_id).last_error == "integration_unavailable"
    with deps.sessions.begin() as session:
        assert syncs.resend(session, job.id, sync_id, actor="operadora")
        assert not syncs.resend(session, uuid.uuid4(), sync_id, actor="operadora")  # outro pedido
    state = sync_state(deps, sync_id)
    assert (state.status, state.hatchet_run_id) == ("pending", None)
    with deps.sessions() as session:
        audited = session.scalars(sa.select(AuditLog).where(AuditLog.action == "sync.resend")).one()
    assert (audited.actor, audited.target_id) == ("operadora", str(sync_id))


def test_a_failure_keeps_the_reason_the_last_attempt_recorded(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)
    baserow.fail("POST", 503)
    with pytest.raises(RetryableError):
        push.push(deps, sync_id)

    with deps.sessions.begin() as session:
        syncs.mark_failed(session, sync_id, "integration_unavailable")

    state = sync_state(deps, sync_id)
    assert state.status == "failed" and "HTTP 503" in (state.last_error or "")


def test_the_reconciler_launches_the_syncs_whose_trigger_was_lost(deps: Deps) -> None:
    connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)
    launched: list[object] = []

    def run() -> crons.ReconcileReport:
        return crons.reconcile_once(
            deps,
            launch_job=lambda j, a: "x",
            run_state=lambda run_id: crons.RunState.ACTIVE,
            launch_delivery_fn=lambda d: "x",
            launch_sync_fn=lambda s: launched.append(s) or "run-s",
        )

    assert run().syncs_launched == 0  # recém-criado: o disparo normal ainda pode chegar
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(IntegrationSync).values(created_at=utcnow() - timedelta(minutes=5))
        )
    report = run()

    assert launched == [sync_id] and report.syncs_launched == 1


def test_the_reconciler_stops_when_the_engine_is_unreachable_and_skips_launched_syncs(
    deps: Deps,
) -> None:
    connect_form(deps)
    completed_job(deps, key="a")
    completed_job(deps, key="b")
    first, second = sync_ids(deps)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(IntegrationSync).values(created_at=utcnow() - timedelta(minutes=5))
        )
        session.execute(
            sa.update(IntegrationSync)
            .where(IntegrationSync.id == second)
            .values(hatchet_run_id="r")
        )
    calls: list[object] = []

    report = crons.reconcile_once(
        deps,
        launch_job=lambda j, a: "x",
        run_state=lambda run_id: crons.RunState.ACTIVE,
        launch_delivery_fn=lambda d: "x",
        launch_sync_fn=lambda s: calls.append(s),  # devolve None: o engine não recebeu
    )

    assert calls == [first] and report.errors == 1


# --- a task do Hatchet ------------------------------------------------------------------------


def test_the_task_runs_the_push_and_returns_only_the_status(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)

    out = wf.upsert_row.mock_run(input=wf.SyncInput.for_sync(sync_id))

    assert out.model_dump() == {"status": "succeeded"}
    assert wf.SyncInput.for_sync(sync_id).run_key == wf.SyncInput.for_sync(sync_id).run_key
    assert set(wf.SyncInput.for_sync(sync_id).model_dump()) == {"sync_id", "run_key"}  # só ids


def test_the_task_failure_reaches_hatchet_without_the_cause_or_lead_data(
    deps: Deps, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(logging.getLogger("app.workflows"), "disabled", False)

    def leaky(*args: object, **kwargs: object) -> None:
        try:
            raise KeyError("fulano@example.test")
        except KeyError as cause:
            raise ValueError("resposta inválida: fulano@example.test") from cause

    monkeypatch.setattr(push, "push", leaky)

    with (
        caplog.at_level(logging.INFO, logger="app.workflows"),
        pytest.raises(RetryableError) as raised,
    ):
        wf.upsert_row.mock_run(input=wf.SyncInput.for_sync(uuid.uuid4()))

    assert str(raised.value) == "[internal] ValueError"
    assert "fulano" not in TaskRunError.from_exception(raised.value, None).trace
    assert "fulano" not in caplog.text and "upsert_row" in caplog.text


def test_a_terminal_error_of_the_push_becomes_a_non_retryable_failure(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hatchet_sdk import NonRetryableException

    def refuse(*args: object, **kwargs: object) -> None:
        raise TerminalError(FailureCode.INTEGRATION_REJECTED, "ler os campos: HTTP 401")

    monkeypatch.setattr(push, "push", refuse)

    with pytest.raises(NonRetryableException, match="integration_rejected"):
        wf.upsert_row.mock_run(input=wf.SyncInput.for_sync(uuid.uuid4()))


# --- o token e os dados da lead não vazam ------------------------------------------------------


def test_the_token_and_the_lead_never_appear_in_logs_errors_or_stored_state(
    deps: Deps, baserow: FakeBaserow, caplog: pytest.LogCaptureFixture
) -> None:
    connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)
    baserow.fail("POST", 500, body={"detail": f"Token {BASEROW_TOKEN} fulano"})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RetryableError) as raised:
            push.push(deps, sync_id)
        push.push(deps, sync_id)
        push.send_sample(deps, SLUG, actor="operadora")

    assert BASEROW_TOKEN not in caplog.text and BASEROW_TOKEN not in str(raised.value)
    assert BASEROW_TOKEN not in TaskRunError.from_exception(raised.value, None).trace
    everything = "\n".join(row for rows in stored_rows(deps).values() for row in rows)
    assert BASEROW_TOKEN not in everything
    state_rows = "\n".join(stored_rows(deps)["integration_syncs"])
    assert not any(trace in state_rows for trace in LEAD_TRACES)  # só ids e estado


# --- retenção ---------------------------------------------------------------------------------


def test_the_retention_deletes_the_syncs_with_the_request_and_keeps_the_integration(
    deps: Deps, baserow: FakeBaserow
) -> None:
    connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)
    push.push(deps, sync_id)

    report = retention.purge_expired(deps, now=utcnow() + timedelta(days=61))

    assert report.requests_deleted == 1
    assert sync_ids(deps) == []
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Integration)) == 1
    assert lead_traces(deps) == []


def test_removing_the_integration_removes_the_state_of_its_syncs_but_not_the_rows_in_baserow(
    deps: Deps, baserow: FakeBaserow
) -> None:
    stored = connect_form(deps)
    completed_job(deps)
    (sync_id,) = sync_ids(deps)
    push.push(deps, sync_id)

    with deps.sessions.begin() as session:
        config.remove(session, form_slug=SLUG, actor="operadora", expected_version=stored.version)

    assert sync_ids(deps) == [] and len(baserow.rows) == 1
    with deps.sessions() as session:
        assert config.load(session, SLUG) is None
        removed = session.scalars(
            sa.select(AuditLog).where(AuditLog.action == "integration.remove")
        ).one()
    assert removed.details["host"] == "baserow.example.test"
