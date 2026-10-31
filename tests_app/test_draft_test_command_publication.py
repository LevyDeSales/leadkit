"""Publicação e partida concorrentes; adaptação do probe independente da guardiã API V2."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from admin_api_support import api_client, url_of
from test_plan_release import manual_pdf  # noqa: F401 — fixture do contrato de liberação.

from app import deployment_contract, integration_readiness
from app.admin.actions import draft_tests as actions
from app.forms import draft_test_commands as commands
from app.forms import draft_tests, drafts
from app.models import DraftTest, DraftTestCommand


@pytest.mark.parametrize("surface", ["service", "api"])
@pytest.mark.usefixtures("manual_pdf")
def test_publication_and_identified_start_serialize_without_deadlock(deps, monkeypatch, surface):
    slug = "manual"
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
    draft = drafts.replace_document(
        deps,
        slug,
        {**draft.document, "intro": "Texto sintético para publicação concorrente."},
        actor="operadora",
        expected=draft.token,
    )
    now = datetime.now(UTC)
    context = commands.issue_context(deps, slug, now=now)
    sent = []

    async def trigger(test_id):
        sent.append(test_id)
        return "synthetic-run"

    client = (
        api_client(deps, clock=lambda: now.timestamp(), role="total", test_trigger=trigger)
        if surface == "api"
        else None
    )
    has_configuration, release_publication = threading.Event(), threading.Event()
    configuration_lock = integration_readiness.configuration_lock
    command_backend = {}
    engine = deps.sessions.kw["bind"]

    def observe_backend(connection, cursor, statement, parameters, context, executemany):
        if threading.current_thread().name.startswith(("command", "AnyIO")):
            command_backend["pid"] = connection.connection.driver_connection.info.backend_pid

    def hold_publisher_configuration(session, *, shared=False):
        configuration_lock(session, shared=shared)
        if not shared and threading.current_thread().name.startswith("publisher"):
            has_configuration.set()
            assert release_publication.wait(10), "Publicadora não foi liberada pelo ensaio."

    def start():
        if client is not None:
            return client.post(
                url_of("start_form_draft_test", slug=slug),
                json={
                    "command_id": context.command_id,
                    "expected_version": context.expected_version,
                    "confirm": True,
                },
            )
        try:
            return commands.commit(
                deps,
                slug,
                context.command_id,
                principal=commands.Principal("admin", "operadora"),
                actor="operadora",
                command=commands.CommandInput(context.expected_version),
                confirmed=True,
                channel="admin",
                clock=lambda: now,
            )
        except draft_tests.DraftVersionConflict:
            return None  # A publicação pode vencer e invalidar a revisão apresentada.

    def publish():
        return drafts.publish(
            deps,
            slug,
            actor="operadora",
            expected=draft.token,
            note="Publicação sintética concorrente",
            contact_confirmed=True,
        )

    monkeypatch.setattr(integration_readiness, "configuration_lock", hold_publisher_configuration)
    sa.event.listen(engine, "before_cursor_execute", observe_backend)
    try:
        with (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="publisher") as publisher_pool,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="command") as command_pool,
        ):
            publication = publisher_pool.submit(publish)
            assert has_configuration.wait(10)
            command = command_pool.submit(start)
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if backend := command_backend.get("pid"):
                        with deps.sessions() as session:
                            waiting = session.execute(
                                sa.text(
                                    "select wait_event_type, wait_event "
                                    "from pg_stat_activity where pid=:pid"
                                ),
                                {"pid": backend},
                            ).one_or_none()
                        if waiting and tuple(waiting) == ("Lock", "advisory"):
                            break
                    threading.Event().wait(0.01)
                else:
                    pytest.fail("O comando não chegou à espera de configuração real no PG.")
            finally:
                release_publication.set()
            # Não há retry de deadlock: qualquer exceção da transação reprova aqui.
            publication.result(timeout=15)
            result = command.result(timeout=15)
        if client is not None:
            assert result.status_code in (200, 409), result.text
            if result.status_code == 409:
                assert result.json()["code"] == "state_conflict"
        else:
            assert result is None or result.created
        with deps.sessions() as session:
            count = session.scalar(sa.select(sa.func.count()).select_from(DraftTestCommand))
            assert session.scalar(sa.select(sa.func.count()).select_from(DraftTest)) == count
        assert len(sent) == (count if client is not None else 0)
    finally:
        release_publication.set()
        sa.event.remove(engine, "before_cursor_execute", observe_backend)
        if client is not None:
            client.close()


@pytest.mark.usefixtures("manual_pdf")
def test_historical_retry_precedes_changed_revision_and_blocked_definition_contract(deps):
    now = datetime.now(UTC)
    principal = commands.Principal("admin", "operadora")
    context = commands.issue_context(deps, "manual", now=now)

    def commit(context):
        return commands.commit(
            deps,
            "manual",
            context.command_id,
            principal=principal,
            actor="operadora",
            command=commands.CommandInput(context.expected_version),
            confirmed=True,
            channel="admin",
            clock=lambda: now,
        )

    original = commit(context).receipt
    with deps.sessions() as session:
        draft = drafts.load(session, "manual")
    drafts.replace_document(
        deps,
        "manual",
        {**draft.document, "intro": "Outra revisão sintética."},
        actor="operadora",
        expected=draft.token,
    )
    fresh = commands.issue_context(deps, "manual", now=now)
    with deps.sessions.begin() as session:
        deployment_contract.begin_rollout(session, epoch=uuid4())
    sent = []

    async def trigger(test_id):
        sent.append(test_id)
        return "synthetic-run"

    receipt = asyncio.run(
        actions.start(
            deps,
            trigger,
            "manual",
            actor="operadora",
            answers=None,
            day="",
            now=now,
            clock=lambda: now,
            command_id=context.command_id,
            expected_version=context.expected_version,
            principal=principal,
            confirmed=True,
        )
    )
    assert receipt == original
    assert sent == []
    # A criação nova continua sujeita ao guard; só a leitura histórica o antecede.
    with pytest.raises(deployment_contract.DeploymentContractError):
        commit(fresh)
