"""Contrato durável pela interface comum, com persistência PostgreSQL real."""

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from test_admin_forms import DEFINITION, SLUG

from app.admin.actions import draft_tests as actions
from app.forms import draft_test_commands as commands
from app.forms import stored
from app.models import AuditLog, DraftTest, DraftTestCommand
from app.retention import purge_expired

NOW = datetime(2027, 1, 15, 12, tzinfo=UTC)
PRINCIPAL = commands.Principal("admin", "testes")


@pytest.fixture
def context(deps):
    stored.import_definition(deps, DEFINITION, actor="testes")
    return commands.issue_context(deps, SLUG, now=NOW)


def commit(deps, context, **options):
    parameters = {
        "principal": PRINCIPAL,
        "actor": "testes",
        "command": commands.CommandInput(context.expected_version),
        "confirmed": True,
        "channel": "admin",
        "clock": lambda: NOW,
    }
    parameters.update(options)
    return commands.commit(deps, SLUG, context.command_id, **parameters)


def test_commit_and_historical_replay_have_the_same_test_and_audit(deps, context):
    first = commit(deps, context)
    again = commands.resolve(
        deps,
        SLUG,
        context.command_id,
        principal=PRINCIPAL,
        command=commands.CommandInput(context.expected_version),
        now=NOW + timedelta(minutes=1),
    )
    assert first.created is True
    assert again.test_id == first.receipt.test_id
    assert again.audit_id == first.receipt.audit_id
    assert again.command_id == context.command_id
    assert again.dispatch_state == "committed"
    assert again.result_state == "retained"


def test_only_winner_dispatches_and_timeout_remains_uncertain(deps, context):
    sent = []

    async def trigger(test_id):
        sent.append(test_id)

    options = {
        "actor": "testes",
        "answers": None,
        "day": "",
        "now": NOW,
        "expected_version": context.expected_version,
        "command_id": context.command_id,
        "principal": PRINCIPAL,
        "confirmed": True,
        "clock": lambda: NOW,
    }
    first = asyncio.run(actions.start(deps, trigger, SLUG, **options))
    options["replay_only"] = True
    options["confirmed"] = False
    second = asyncio.run(actions.start(deps, trigger, SLUG, **options))
    assert sent == [first.test_id]
    assert second.test_id == first.test_id
    assert second.dispatch_state == "uncertain"
    assert second.test_status == "queued"


def test_retention_discards_receipt_and_old_identity_never_creates_again(deps, context):
    commit(deps, context)
    later = NOW + timedelta(days=3, seconds=1)
    purge_expired(deps, now=later)
    with deps.sessions() as session:
        assert session.get(DraftTestCommand, context.command_id) is None
    with pytest.raises(commands.CommandError, match="expirou") as caught:
        commands.resolve(deps, SLUG, context.command_id, principal=PRINCIPAL, now=later)
    assert caught.value.code == "command_expired"


def test_expiration_is_checked_immediately_before_persistence(deps, context):
    times = iter([NOW, NOW, NOW + timedelta(days=3)])
    with pytest.raises(commands.CommandError) as caught:
        commands.commit(
            deps,
            SLUG,
            context.command_id,
            principal=PRINCIPAL,
            actor="testes",
            command=commands.CommandInput(context.expected_version),
            confirmed=True,
            channel="admin",
            clock=lambda: next(times),
        )
    assert caught.value.code == "command_expired"
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(DraftTest)) == 0


@pytest.mark.parametrize(
    "changed,code",
    [
        ({"expected_version": "different"}, "command_conflict"),
        ({"answers": {}}, "command_conflict"),
        ({"day": "2027-01-16"}, "command_conflict"),
    ],
)
def test_changed_arguments_conflict_with_historical_commit(deps, context, changed, code):
    commit(deps, context)
    command = dataclasses.replace(commands.CommandInput(context.expected_version), **changed)
    with pytest.raises(commands.CommandError) as caught:
        commands.resolve(
            deps, SLUG, context.command_id, principal=PRINCIPAL, command=command, now=NOW
        )
    assert caught.value.code == code


def test_other_principal_is_not_a_creation_eligible_miss(deps, context):
    commit(deps, context)
    with pytest.raises(commands.CommandError) as caught:
        commands.resolve(
            deps, SLUG, context.command_id, principal=commands.Principal("admin", "outra"), now=NOW
        )
    assert caught.value.code == "command_forbidden"


def test_future_identity_is_rejected_without_effect(deps, context):
    future = commands.issue_context(deps, SLUG, now=NOW + timedelta(milliseconds=1))
    with pytest.raises(commands.CommandError) as caught:
        commit(deps, future)
    assert caught.value.code == "command_in_future"


def test_failed_transaction_leaves_neither_test_command_nor_audit(deps, context, monkeypatch):
    from sqlalchemy.orm import Session

    def refuse(session, flush_context, instances):
        if any(isinstance(row, DraftTestCommand) for row in session.new):
            raise RuntimeError("falha sintética antes do commit")

    sa.event.listen(Session, "before_flush", refuse)
    try:
        with pytest.raises(RuntimeError, match="sintética"):
            commit(deps, context)
    finally:
        sa.event.remove(Session, "before_flush", refuse)
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(DraftTest)) == 0
        assert session.get(DraftTestCommand, context.command_id) is None
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "form.draft.test")
            )
            == 0
        )
    assert commit(deps, context).created


def test_eviction_preserves_receipt_without_answers_and_fixed_retention(deps, context):
    first = commit(deps, context)
    from app.forms import draft_tests

    for n in range(1, 6):
        moment = NOW + timedelta(minutes=11 * n)
        draft_tests.start(deps, SLUG, actor="testes", now=moment)
    receipt = commands.resolve(deps, SLUG, context.command_id, principal=PRINCIPAL, now=moment)
    assert receipt.test_id == first.receipt.test_id
    assert receipt.result_state == "expired"
    output = receipt.model_dump_json()
    assert all(word not in output for word in ("answers", "prompt", "plan", "digest"))


def test_receipt_expiry_retains_the_original_result_under_an_extended_policy(
    deps, context, monkeypatch
):
    from app import retention
    from app.forms import draft_tests

    first = commit(deps, context)
    test_id = first.receipt.test_id
    assert draft_tests.run(deps, test_id, now=lambda: NOW) == "succeeded"
    monkeypatch.setattr(draft_tests, "TEST_DAYS", 30)
    monkeypatch.setattr(retention, "TEST_DAYS", 30)
    later = NOW + timedelta(days=3, seconds=1)
    purge_expired(deps, now=later)
    # A partida também consome a política de resultado; nenhum dos dois consumidores o apaga.
    draft_tests.start(deps, SLUG, actor="testes", now=later)
    with deps.sessions() as session:
        assert session.get(DraftTestCommand, context.command_id) is None
        assert session.get(DraftTest, test_id) is not None
    with pytest.raises(commands.CommandError) as caught:
        commit(deps, context, clock=lambda: later)
    assert caught.value.code == "command_expired"


def test_unrelated_integrity_failure_is_not_reinterpreted_as_replay(deps, context):
    def invalid_state(mapper, connection, row):
        row.dispatch_state = "invalid"

    sa.event.listen(DraftTestCommand, "before_insert", invalid_state)
    try:
        with pytest.raises(sa.exc.IntegrityError):
            commit(deps, context)
    finally:
        sa.event.remove(DraftTestCommand, "before_insert", invalid_state)


@pytest.mark.parametrize("state", ["not_started", "uncertain", "exception", "completed_uncertain"])
def test_dispatch_facts_do_not_restore_the_right_to_start(deps, context, state):
    from app.admin.launching import DraftTestDispatch
    from app.forms import draft_tests

    sent = []

    async def trigger(test_id):
        sent.append(test_id)
        if state == "exception":
            raise RuntimeError("falha sintética de transporte")
        if state == "completed_uncertain":
            assert draft_tests.run(deps, test_id, now=lambda: NOW) == "succeeded"
            return None
        return DraftTestDispatch(state)

    options = {
        "actor": "testes",
        "answers": None,
        "day": "",
        "now": NOW,
        "command_id": context.command_id,
        "expected_version": context.expected_version,
        "principal": PRINCIPAL,
        "confirmed": True,
        "clock": lambda: NOW,
    }
    first = asyncio.run(actions.start(deps, trigger, SLUG, **options))
    second = asyncio.run(actions.start(deps, trigger, SLUG, **options))
    assert first.test_id == second.test_id
    assert sent == [first.test_id]
    assert second.dispatch_state == ("not_started" if state == "not_started" else "uncertain")
    if state == "not_started":
        assert second.failure_code == "not_started"
    if state == "completed_uncertain":
        assert second.test_status == "succeeded"
