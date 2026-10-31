"""Duas escritoras independentes usam o mesmo PostgreSQL e somente a vencedora despacha."""

import asyncio
import json
import multiprocessing
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker
from test_admin_forms import DEFINITION, SLUG
from test_draft_test_commands import NOW, PRINCIPAL

from app.admin.actions import draft_tests as actions
from app.forms import draft_test_commands as commands
from app.forms import stored
from app.models import AuditLog, DraftTest, DraftTestCommand, Form


def writer(url, identity, revision, queue, trigger_release, crash=None, slug=SLUG):
    engine = sa.create_engine(url)
    deps = SimpleNamespace(sessions=sessionmaker(engine, expire_on_commit=False))

    async def trigger(test_id):
        queue.put(("dispatch", str(test_id)))
        if crash == "after_trigger":
            os._exit(0)
        if not trigger_release.wait(20):
            raise RuntimeError("barreira de ensaio expirou")
        return "synthetic-run"

    try:
        if crash == "after_commit":
            commands.commit(
                deps,
                slug,
                identity,
                principal=PRINCIPAL,
                actor="testes",
                command=commands.CommandInput(revision),
                confirmed=True,
                channel="admin",
                clock=lambda: NOW,
            )
            os._exit(0)
        result = asyncio.run(
            actions.start(
                deps,
                trigger,
                slug,
                actor="testes",
                answers=None,
                day="",
                now=NOW,
                clock=lambda: NOW,
                expected_version=revision,
                command_id=identity,
                principal=PRINCIPAL,
                confirmed=True,
            )
        )
        queue.put(("receipt", result.model_dump(mode="json")))
    except commands.CommandError as error:
        queue.put(("refused", error.code))
    finally:
        engine.dispose()


def prepare(deps):
    stored.import_definition(deps, DEFINITION, actor="testes")
    return commands.issue_context(deps, SLUG, now=NOW)


def test_two_processes_observe_one_commit_and_only_creator_dispatches(deps):
    context = prepare(deps)
    ctx = multiprocessing.get_context("spawn")
    queue, release = ctx.Queue(), ctx.Event()
    url = deps.sessions.kw["bind"].url.render_as_string(hide_password=False)
    processes = [
        ctx.Process(
            target=writer, args=(url, context.command_id, context.expected_version, queue, release)
        )
        for _ in range(2)
    ]
    # Suspende ambas antes da obtenção do lock; depois suspende a vencedora dentro do trigger.
    try:
        with deps.sessions.begin() as session:
            session.execute(sa.select(Form.id).where(Form.slug == SLUG).with_for_update()).one()
            for process in processes:
                process.start()
        first = queue.get(timeout=25)
        assert first[0] == "dispatch"
        observed = queue.get(timeout=25)
        assert observed[0] == "receipt"
        assert observed[1]["test_id"] == first[1]
        release.set()
        winner = queue.get(timeout=25)
        assert winner[0] == "receipt"
        assert winner[1]["test_id"] == first[1]
        assert winner[1]["audit_id"] == observed[1]["audit_id"]
        for process in processes:
            process.join(25)
            assert process.exitcode == 0
        with deps.sessions() as session:
            assert session.scalar(sa.select(sa.func.count()).select_from(DraftTest)) == 1
            assert session.scalar(sa.select(sa.func.count()).select_from(DraftTestCommand)) == 1
            assert (
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == "form.draft.test")
                )
                == 1
            )
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
        queue.close()


@pytest.mark.parametrize(
    "crash,dispatch", [("after_commit", "committed"), ("after_trigger", "uncertain")]
)
@pytest.mark.parametrize("replay_only", [True, False], ids=["lookup", "normal_retry"])
def test_process_loss_does_not_grant_replay_another_dispatch(deps, crash, dispatch, replay_only):
    context = prepare(deps)
    ctx = multiprocessing.get_context("spawn")
    queue, release = ctx.Queue(), ctx.Event()
    process = ctx.Process(
        target=writer,
        args=(
            deps.sessions.kw["bind"].url.render_as_string(hide_password=False),
            context.command_id,
            context.expected_version,
            queue,
            release,
            crash,
        ),
    )
    process.start()
    process.join(25)
    try:
        assert process.exitcode == 0
        original = commands.resolve(deps, SLUG, context.command_id, principal=PRINCIPAL, now=NOW)
        sent = []

        async def trigger(test_id):
            sent.append(test_id)
            return "synthetic-run"

        receipt = asyncio.run(
            actions.start(
                deps,
                trigger,
                SLUG,
                actor="testes",
                answers=None,
                day="",
                now=NOW + timedelta(seconds=31),
                command_id=context.command_id,
                expected_version=context.expected_version,
                principal=PRINCIPAL,
                replay_only=replay_only,
                confirmed=not replay_only,
            )
        )
        assert receipt.command_id == original.command_id == context.command_id
        assert receipt.test_id == original.test_id
        assert receipt.audit_id == original.audit_id
        assert receipt.dispatch_state == dispatch
        assert not sent
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        queue.close()


def test_global_identity_race_between_two_forms_commits_only_one_test(deps):
    first = prepare(deps)
    other = json.loads(DEFINITION)
    other["slug"] = "outro-diagnostico"
    stored.import_definition(deps, json.dumps(other), actor="testes")
    second = commands.issue_context(deps, other["slug"], now=NOW)
    ctx = multiprocessing.get_context("spawn")
    queue, release = ctx.Queue(), ctx.Event()
    url = deps.sessions.kw["bind"].url.render_as_string(hide_password=False)
    processes = [
        ctx.Process(
            target=writer, args=(url, first.command_id, revision, queue, release, None, slug)
        )
        for slug, revision in (
            (SLUG, first.expected_version),
            (other["slug"], second.expected_version),
        )
    ]
    try:
        with deps.sessions.begin() as session:
            session.execute(
                sa.select(Form.id)
                .where(Form.slug.in_((SLUG, other["slug"])))
                .order_by(Form.id)
                .with_for_update()
            ).all()
            for process in processes:
                process.start()
        facts = [queue.get(timeout=25), queue.get(timeout=25)]
        assert sorted(fact[0] for fact in facts) == ["dispatch", "refused"]
        assert next(fact[1] for fact in facts if fact[0] == "refused") == "command_conflict"
        release.set()
        assert queue.get(timeout=25)[0] == "receipt"
        for process in processes:
            process.join(25)
            assert process.exitcode == 0
        with deps.sessions() as session:
            assert session.scalar(sa.select(sa.func.count()).select_from(DraftTest)) == 1
            assert session.scalar(sa.select(sa.func.count()).select_from(DraftTestCommand)) == 1
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
        queue.close()
