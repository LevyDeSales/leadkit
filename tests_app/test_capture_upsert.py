"""Transporte de captura usa a chave da submissão e a trava do upsert existente."""

import uuid
from dataclasses import replace

import pytest
from conftest import BASEROW_TABLE, BASEROW_TOKEN, BASEROW_URL, FakeBaserow, legacy_spec

from app.deps import Deps
from app.integrations import mapping, push
from app.integrations.baserow import Connection

CONNECTION = Connection(BASEROW_URL, BASEROW_TABLE, BASEROW_TOKEN)
ENTRIES = [
    mapping.Entry(104, "Código da submissão", "text", "submission.id"),
    mapping.Entry(101, "Nome", "text", "lead.name"),
]


def test_capture_upsert_retries_by_explicit_submission_key_and_keeps_other_submissions(
    deps: Deps,
) -> None:
    baserow = FakeBaserow().install(deps)
    integration_id = uuid.uuid4()
    data = mapping.sample_data(legacy_spec("demo"), str(uuid.uuid4()), profile="capture")
    arguments = {
        "integration_id": integration_id,
        "key_field_id": 104,
        "entries": ENTRIES,
        "profile": "capture",
    }
    first = push.upsert(deps, CONNECTION, **arguments, data=data, record_key=data.submission_id)
    again = push.upsert(
        deps,
        CONNECTION,
        **arguments,
        data=replace(data, lead_name="Contadora Atualizada"),
        record_key=data.submission_id,
    )
    another_id = str(uuid.uuid4())
    other = push.upsert(
        deps,
        CONNECTION,
        **arguments,
        data=replace(data, submission_id=another_id),
        record_key=another_id,
    )
    assert (first.outcome, again.outcome, other.outcome) == ("created", "updated", "created")
    assert first.row_id == again.row_id != other.row_id
    assert baserow.row_where(104, data.submission_id)["field_101"] == "Contadora Atualizada"
    assert len(baserow.rows) == 2


def test_capture_retry_after_ambiguous_timeout_updates_the_row_already_written(deps: Deps) -> None:
    import httpx
    import pytest

    from app.errors import RetryableError
    from app.integrations.baserow import BaserowTransport

    baserow = FakeBaserow()

    def timeout_after_write(request: httpx.Request) -> httpx.Response:
        response = baserow(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("synthetic timeout after remote commit", request=request)
        return response

    deps.baserow = BaserowTransport(
        httpx.Client(transport=httpx.MockTransport(timeout_after_write))
    )
    data = mapping.sample_data(legacy_spec("demo"), str(uuid.uuid4()), profile="capture")
    arguments = {
        "integration_id": uuid.uuid4(),
        "key_field_id": 104,
        "entries": ENTRIES,
        "data": data,
        "record_key": data.submission_id,
        "profile": "capture",
    }
    with pytest.raises(RetryableError):
        push.upsert(deps, CONNECTION, **arguments)
    result = push.upsert(deps, CONNECTION, **arguments)
    assert result.outcome == "updated"
    assert len(baserow.rows) == 1
    assert baserow.row_where(104, data.submission_id)["id"] == result.row_id


def test_duplicate_capture_workers_serialize_the_same_submission_key(deps: Deps) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    baserow = FakeBaserow().install(deps)
    barrier = threading.Barrier(2)
    data = mapping.sample_data(legacy_spec("demo"), str(uuid.uuid4()), profile="capture")
    integration_id = uuid.uuid4()

    def fields_together(request):
        if "/fields/" in request.url.path:
            barrier.wait(timeout=5)

    baserow.before = fields_together

    def deliver():
        return push.upsert(
            deps,
            CONNECTION,
            integration_id=integration_id,
            key_field_id=104,
            entries=ENTRIES,
            data=data,
            record_key=data.submission_id,
            profile="capture",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: deliver(), range(2)))
    assert sorted(result.outcome for result in results) == ["created", "updated"]
    assert results[0].row_id == results[1].row_id
    assert len(baserow.rows) == 1


def test_capture_refuses_invalid_mapping_or_explicit_key_without_writing(deps: Deps) -> None:
    import pytest

    from app.errors import FailureCode, TerminalError

    baserow = FakeBaserow().install(deps)
    data = mapping.sample_data(legacy_spec("demo"), str(uuid.uuid4()), profile="capture")
    arguments = {
        "integration_id": uuid.uuid4(),
        "key_field_id": 104,
        "data": data,
        "profile": "capture",
    }
    variants = [
        {"entries": ENTRIES},
        {"entries": ENTRIES, "record_key": "wrong-record"},
        {"entries": ENTRIES, "record_key": ""},
        {"entries": ENTRIES, "record_key": data.submission_id, "key_field_id": 101},
        {
            "entries": ENTRIES + [mapping.Entry(106, "PDF", "url", "job.pdf_url")],
            "record_key": data.submission_id,
        },
        {
            "entries": ENTRIES
            + [mapping.Entry(108, "Equipe", "single_select", "answer:removed_question")],
            "record_key": data.submission_id,
        },
        {"entries": ENTRIES[1:], "record_key": data.submission_id},
    ]
    for variant in variants:
        with pytest.raises(TerminalError) as caught:
            push.upsert(deps, CONNECTION, **(arguments | variant))
        assert caught.value.code is FailureCode.INTEGRATION_MAPPING
    assert baserow.rows == {}
    assert not baserow.calls_to("POST") and not baserow.calls_to("PATCH")


def test_capture_upsert_keeps_the_callers_session_transaction_and_lock_until_rollback(
    deps: Deps,
) -> None:
    import sqlalchemy as sa

    baserow = FakeBaserow().install(deps)
    data = mapping.sample_data(legacy_spec("demo"), str(uuid.uuid4()), profile="capture")
    integration_id = uuid.uuid4()
    lock = f"integration:{integration_id}:{data.submission_id}"
    attempts = []

    def record_checkout(*args):
        attempts.append(True)

    with deps.sessions() as session, deps.sessions() as observer:
        session.begin()
        connection = session.connection()
        observer.connection()
        transaction = session.get_transaction()
        sa.event.listen(connection.engine, "checkout", record_checkout)
        try:
            result = push.upsert(
                deps,
                CONNECTION,
                integration_id=integration_id,
                key_field_id=104,
                entries=ENTRIES,
                data=data,
                record_key=data.submission_id,
                profile="capture",
                session=session,
            )
        finally:
            sa.event.remove(connection.engine, "checkout", record_checkout)
        assert result.outcome == "created"
        assert session.get_transaction() is transaction and transaction.is_active
        assert session.connection() is connection
        assert attempts == []
        assert (
            observer.scalar(
                sa.text("select pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": lock},
            )
            is False
        )
        session.rollback()
        assert (
            observer.scalar(
                sa.text("select pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": lock},
            )
            is True
        )
        observer.rollback()
        assert session.scalar(sa.text("select 1")) == 1
    assert baserow.row_where(104, data.submission_id)["id"] == result.row_id
    retry = push.upsert(
        deps,
        CONNECTION,
        integration_id=integration_id,
        key_field_id=104,
        entries=ENTRIES,
        data=data,
        record_key=data.submission_id,
        profile="capture",
    )
    assert retry.outcome == "updated" and retry.row_id == result.row_id


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_capture_workers_wait_until_the_callers_transaction_finishes(
    deps: Deps, finish: str
) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue

    import sqlalchemy as sa

    baserow = FakeBaserow().install(deps)
    data = mapping.sample_data(legacy_spec("demo"), str(uuid.uuid4()), profile="capture")
    arguments = {
        "integration_id": uuid.uuid4(),
        "key_field_id": 104,
        "entries": ENTRIES,
        "data": data,
        "record_key": data.submission_id,
        "profile": "capture",
    }
    worker_pid = Queue()

    def deliver():
        with deps.sessions.begin() as worker:
            worker_pid.put(worker.scalar(sa.text("select pg_backend_pid()")))
            result = push.upsert(deps, CONNECTION, **arguments, session=worker)
            assert worker.in_transaction()
            return result

    with deps.sessions() as caller, deps.sessions() as observer:
        caller.begin()
        caller_pid = caller.scalar(sa.text("select pg_backend_pid()"))
        first = push.upsert(deps, CONNECTION, **arguments, session=caller)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(deliver)
            try:
                pid = worker_pid.get(timeout=5)
                deadline = time.monotonic() + 5
                while True:
                    blockers = observer.scalar(
                        sa.text("select pg_blocking_pids(:pid)"), {"pid": pid}
                    )
                    if caller_pid in blockers:
                        break
                    assert time.monotonic() < deadline, (
                        "A segunda entrega não esperou a transação da chamadora."
                    )
                    time.sleep(0.01)
                assert not future.done()
            finally:
                getattr(caller, finish)()
            second = future.result(timeout=5)
        assert (first.outcome, second.outcome) == ("created", "updated")
        assert first.row_id == second.row_id
        assert len(baserow.rows) == 1


def test_capture_transport_failure_leaves_the_received_transaction_to_the_caller(
    deps: Deps,
) -> None:
    import sqlalchemy as sa

    from app.errors import RetryableError

    baserow = FakeBaserow().install(deps)
    baserow.fail("POST", 503)
    data = mapping.sample_data(legacy_spec("demo"), str(uuid.uuid4()), profile="capture")
    with deps.sessions() as caller:
        caller.begin()
        transaction = caller.get_transaction()
        connection = caller.connection()
        with pytest.raises(RetryableError):
            push.upsert(
                deps,
                CONNECTION,
                integration_id=uuid.uuid4(),
                key_field_id=104,
                entries=ENTRIES,
                data=data,
                record_key=data.submission_id,
                profile="capture",
                session=caller,
            )
        assert caller.get_transaction() is transaction and transaction.is_active
        assert caller.connection() is connection
        assert caller.scalar(sa.text("select 1")) == 1
        caller.rollback()
    assert baserow.rows == {}
