"""O disparo no Hatchet pelo admin (`app.admin.launching`): o executor próprio, a espera limitada por
`HATCHET_TRIGGER_TIMEOUT_SECONDS` e a tentativa nova do reprocessamento.

É o módulo único que as páginas e a API de admin usam para disparar trabalho: um disparo que não chega
ao engine não é erro (devolve `False`), porque o reconciliador encontra o que ficou sem run id. O
Hatchet de verdade fica fora: `app.workflows.launch_delivery` e `launch_sync` (a fronteira) são
trocados por falsos, e o disparador da tentativa é um falso assíncrono, como o do app.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from app import workflows
from app.admin import launching
from app.deps import Deps

Launcher = Callable[[uuid.UUID], str | None]
KINDS = ["delivery", "sync"]


def launch_of(kind: str) -> Callable[[Deps, uuid.UUID], Coroutine[Any, Any, bool]]:
    """A função de `launching` de cada tipo de disparo."""
    return launching.launch_delivery if kind == "delivery" else launching.launch_sync


def patch_launcher(monkeypatch: pytest.MonkeyPatch, kind: str, fake: Launcher) -> None:
    monkeypatch.setattr(workflows, f"launch_{kind}", fake)


def with_timeout(deps: Deps, seconds: float) -> Deps:
    deps.settings = deps.settings.model_copy(update={"hatchet_trigger_timeout_seconds": seconds})
    return deps


@pytest.mark.parametrize("kind", KINDS)
def test_a_launch_that_reaches_the_engine_is_launched_and_runs_off_the_event_loop(
    deps: Deps, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    target, seen = uuid.uuid4(), []

    def fake(launched: uuid.UUID) -> str | None:
        seen.append((launched, threading.current_thread().name))
        return "run-1"

    patch_launcher(monkeypatch, kind, fake)

    launched = asyncio.run(launch_of(kind)(deps, target))

    assert launched is True
    assert [item for item, _ in seen] == [target]
    assert seen[0][1].startswith("admin-launch")  # o SDK é síncrono: nunca no laço de eventos


@pytest.mark.parametrize("kind", KINDS)
def test_a_launch_the_engine_did_not_receive_is_not_launched_and_is_not_an_error(
    deps: Deps, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    patch_launcher(monkeypatch, kind, lambda launched: None)  # o SDK devolve None sem engine

    assert asyncio.run(launch_of(kind)(deps, uuid.uuid4())) is False


@pytest.mark.parametrize("kind", KINDS)
def test_the_wait_ends_at_the_trigger_timeout_and_the_started_launch_goes_on_in_the_background(
    deps: Deps, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    with_timeout(deps, 0.2)
    release, finished = threading.Event(), threading.Event()

    def stuck(launched: uuid.UUID) -> str | None:
        release.wait(timeout=10)  # o SDK insiste por dezenas de segundos sem engine
        finished.set()
        return None

    patch_launcher(monkeypatch, kind, stuck)

    started = time.monotonic()
    launched = asyncio.run(launch_of(kind)(deps, uuid.uuid4()))
    elapsed = time.monotonic() - started
    assert not finished.is_set()  # voltou sem esperar o disparo terminar
    release.set()

    assert launched is False and elapsed < 5
    assert finished.wait(timeout=5)  # o que já tinha começado seguiu em segundo plano


@pytest.mark.parametrize(
    ("timeout", "expected"),
    [(0.1, False), (5.0, True)],
    ids=["short_wait_gives_up", "long_wait_receives_the_run"],
)
def test_the_wait_is_the_one_in_the_settings_not_a_fixed_one(
    deps: Deps, monkeypatch: pytest.MonkeyPatch, timeout: float, expected: bool
) -> None:
    with_timeout(deps, timeout)

    def slow(launched: uuid.UUID) -> str | None:
        time.sleep(0.5)  # demora mais que a espera curta e menos que a longa
        return "run-1"

    patch_launcher(monkeypatch, "delivery", slow)

    assert asyncio.run(launching.launch_delivery(deps, uuid.uuid4())) is expected


def test_a_new_attempt_is_launched_when_the_trigger_answers_with_a_run() -> None:
    job_id, calls = uuid.uuid4(), []

    async def trigger(launched_job: uuid.UUID, attempt: int) -> str | None:
        calls.append((launched_job, attempt))
        return "run-2"

    assert asyncio.run(launching.launch_attempt(trigger, job_id, 2)) is True
    assert calls == [(job_id, 2)]


def test_a_new_attempt_the_trigger_could_not_start_is_left_to_the_reconciler() -> None:
    async def trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return None  # o disparador do app já engole a falha e devolve None

    assert asyncio.run(launching.launch_attempt(trigger, uuid.uuid4(), 2)) is False


def test_a_draft_test_is_launched_with_only_its_id_when_the_trigger_answers_with_a_run() -> None:
    test_id, calls = uuid.uuid4(), []

    async def trigger(launched_test: uuid.UUID) -> str | None:
        calls.append(launched_test)
        return "run-3"

    assert asyncio.run(launching.launch_draft_test(trigger, test_id)) is True
    assert calls == [test_id]


def test_a_draft_test_the_trigger_could_not_start_is_not_launched_and_is_not_an_error() -> None:
    async def trigger(test_id: uuid.UUID) -> str | None:
        return None  # o disparador do app já engole a falha e devolve None

    assert asyncio.run(launching.launch_draft_test(trigger, uuid.uuid4())) is False


def test_the_background_launch_of_many_syncs_goes_one_by_one_and_stops_at_the_first_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, third = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    calls: list[uuid.UUID] = []

    def fake(sync_id: uuid.UUID) -> str | None:
        calls.append(sync_id)
        return None if sync_id == second else "run-s"  # o engine sai do ar na segunda

    patch_launcher(monkeypatch, "sync", fake)

    launching.launch_syncs_later([first, second, third]).result(timeout=5)

    assert calls == [first, second]  # a terceira fica para o reconciliador


def test_the_limit_of_a_batch_resend_is_two_hundred_syncs() -> None:
    assert launching.RESEND_FAILED_LIMIT == 200


def test_a_batch_launch_waits_only_for_the_first_and_the_rest_follow_in_the_background(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [uuid.uuid4() for _ in range(3)]
    calls: list[uuid.UUID] = []
    all_launched = threading.Event()
    release = threading.Event()

    def fake(sync_id: uuid.UUID) -> str | None:
        calls.append(sync_id)
        if sync_id != ids[0]:
            release.wait(timeout=5)  # a resposta não espera as outras
        if len(calls) == len(ids):
            all_launched.set()
        return "run-s"

    patch_launcher(monkeypatch, "sync", fake)

    launched = asyncio.run(launching.launch_syncs(deps, ids))
    still_going = not all_launched.is_set()
    release.set()

    assert launched is True and still_going
    assert all_launched.wait(timeout=5) and calls == ids  # a mesma fila, na mesma ordem


def test_a_batch_launch_the_engine_did_not_receive_is_false_and_is_not_an_error(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [uuid.uuid4() for _ in range(3)]
    calls: list[uuid.UUID] = []

    def fake(sync_id: uuid.UUID) -> str | None:
        calls.append(sync_id)
        return None  # o SDK devolve None sem engine

    patch_launcher(monkeypatch, "sync", fake)

    launched = asyncio.run(launching.launch_syncs(deps, ids))

    # a primeira foi tentada; a fila para nela (como em `launch_syncs_later`), e o reconciliador
    # dispara o resto
    assert launched is False and calls[0] == ids[0]


def test_a_batch_launch_waits_no_longer_than_the_trigger_timeout_and_the_queue_goes_on_alone(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_timeout(deps, 0.2)
    ids = [uuid.uuid4() for _ in range(2)]
    release, finished = threading.Event(), threading.Event()
    calls: list[uuid.UUID] = []

    def stuck(sync_id: uuid.UUID) -> str | None:
        calls.append(sync_id)
        if sync_id == ids[0]:
            release.wait(timeout=10)  # o SDK insiste por dezenas de segundos sem engine
        else:
            finished.set()
        return "run-s"

    patch_launcher(monkeypatch, "sync", stuck)

    started = time.monotonic()
    launched = asyncio.run(launching.launch_syncs(deps, ids))
    elapsed = time.monotonic() - started
    release.set()

    assert launched is False and elapsed < 5
    assert finished.wait(timeout=5) and calls == ids  # a fila andou sem quem esperava


def test_a_batch_launch_of_nothing_launches_nothing(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_launcher(monkeypatch, "sync", lambda sync_id: pytest.fail("não deveria"))

    assert asyncio.run(launching.launch_syncs(deps, [])) is False


def test_a_batch_launch_that_raises_before_the_first_is_counted_does_not_wait_for_the_timeout(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O SDK não levanta (`launch_sync` devolve `None`), mas se a fila quebrar por outro motivo, a
    resposta é `launched: false` na hora, e não depois do prazo inteiro."""
    with_timeout(deps, 30)
    ids = [uuid.uuid4() for _ in range(2)]

    def broken(sync_id: uuid.UUID) -> str | None:
        raise RuntimeError("o disparo quebrou")

    patch_launcher(monkeypatch, "sync", broken)

    started = time.monotonic()
    launched = asyncio.run(launching.launch_syncs(deps, ids))
    elapsed = time.monotonic() - started

    assert launched is False and elapsed < 5


def test_a_batch_launch_that_raises_after_the_first_keeps_the_answer_of_the_first(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [uuid.uuid4() for _ in range(2)]
    seen = threading.Event()

    def second_breaks(sync_id: uuid.UUID) -> str | None:
        if sync_id == ids[1]:
            seen.set()
            raise RuntimeError("o segundo disparo quebrou")
        return "run-s"

    patch_launcher(monkeypatch, "sync", second_breaks)

    assert asyncio.run(launching.launch_syncs(deps, ids)) is True  # a primeira chegou ao engine
    assert seen.wait(timeout=5)
