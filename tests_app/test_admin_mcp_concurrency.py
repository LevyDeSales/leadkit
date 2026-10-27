"""Uso único da confirmação no runner real, com HTTP inerte suspenso antes da resposta."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from admin_mcp_support import body_of, error
from admin_mcp_world import World

from app.admin_mcp.client import ApiClient
from app.admin_mcp.confirmation import ConfirmationOutcome
from app.admin_mcp.runner import NeverAsks, ToolRunner


class SuspendedApi:
    """Conta os envios, mas só responde quando o cenário liberar explicitamente."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.entered = asyncio.Event()
        self.second = asyncio.Event()
        self.release = asyncio.Event()
        self.failure: Exception | None = None
        self.response = httpx.Response(200, json={"removed": True})

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        self.entered.set()
        if len(self.calls) == 2:
            self.second.set()
        await self.release.wait()
        if self.failure is not None:
            raise self.failure
        return self.response


@pytest.fixture
def suspended(tmp_path: Path) -> Iterator[tuple[World, SuspendedApi]]:
    world = World(tmp_path)
    world.close()
    api = SuspendedApi()
    world.client = ApiClient(world.config, transport=httpx.MockTransport(api))
    world.runner = ToolRunner(world.client, world.config, pending=world.runner.pending)
    yield world, api
    world.close()


def test_one_pending_cannot_dispatch_two_concurrent_requests(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = World(tmp_path)
        calls: list[httpx.Request] = []
        entered = asyncio.Event()
        observed_second = asyncio.Event()
        release = asyncio.Event()

        async def reply(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            entered.set()
            if len(calls) == 2:
                observed_second.set()
            await release.wait()
            return httpx.Response(200, json={"removed": True})

        client = ApiClient(world.config, transport=httpx.MockTransport(reply))
        runner = ToolRunner(client, world.config)
        spec = world.tools["remove_asset"]
        arguments = {"asset_id": "synthetic", "expected_version": "2"}
        tasks = []
        try:
            first = await runner.call(spec, arguments, NeverAsks())
            assert first.awaiting and calls == []
            confirmed = {**arguments, "confirm": True}
            tasks.append(asyncio.create_task(runner.call(spec, confirmed, NeverAsks())))
            await asyncio.wait_for(entered.wait(), 2)
            tasks.append(asyncio.create_task(runner.call(spec, confirmed, NeverAsks())))
            tasks[1].add_done_callback(lambda _: observed_second.set())
            # A segunda tarefa ou devolveu o bloqueio, ou entrou no transporte: ambas permitem
            # observar a concorrência sem liberar a primeira resposta nem depender de sleep.
            await asyncio.wait_for(observed_second.wait(), 2)
            assert len(calls) == 1, "uma pendência despachou dois HTTP antes da primeira resposta"
            concurrent = await tasks[1]
            assert concurrent.is_error and not concurrent.awaiting
            assert "em andamento" in concurrent.text
            assert "nada foi" not in concurrent.text.lower()
            assert "não foi executada" not in concurrent.text
            assert not tasks[0].done()
            release.set()
            assert (await tasks[0]).status == 200
            replay = await runner.call(spec, confirmed, NeverAsks())
            assert replay.awaiting and len(calls) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
            await world.client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["timeout", "unexpected", "file_error", "cancelled"])
def test_ending_an_in_flight_attempt_releases_it_without_restoring_authority(
    suspended: tuple[World, SuspendedApi], ending: str
) -> None:
    async def scenario() -> None:
        world, api = suspended
        spec = world.tools["remove_asset"]
        arguments = {"asset_id": "synthetic", "expected_version": "2", "confirm": True}
        assert (await world.runner.call(spec, arguments, NeverAsks())).awaiting
        task = asyncio.create_task(world.runner.call(spec, arguments, NeverAsks()))
        try:
            await asyncio.wait_for(api.entered.wait(), 2)
            if ending == "cancelled":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                api.failure = {
                    "timeout": httpx.ReadTimeout("timeout sintético"),
                    "unexpected": RuntimeError("falha sintética"),
                    "file_error": OSError("falha sintética"),
                }[ending]
                api.release.set()
                output = await task
                assert output.is_error
                if ending == "timeout":
                    assert "PODE ter sido executada" in output.text
                    assert "confira o estado" in output.text
            assert len(api.calls) == 1  # nenhum retry automático
            api.failure = None
            api.release.set()
            replay = await world.runner.call(spec, arguments, NeverAsks())
            assert replay.awaiting and len(api.calls) == 1
            # Nova apresentação, depois nova confirmação: reserva livre e autorização renovada.
            assert (await world.runner.call(spec, arguments, NeverAsks())).status == 200
            assert len(api.calls) == 2
        finally:
            api.release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_different_call_can_be_presented_and_confirmed_while_one_is_in_flight(
    suspended: tuple[World, SuspendedApi],
) -> None:
    async def scenario() -> None:
        world, api = suspended
        spec = world.tools["remove_asset"]
        arguments = {"asset_id": "synthetic", "expected_version": "2", "confirm": True}
        other = {**arguments, "asset_id": "independent"}
        assert (await world.runner.call(spec, arguments, NeverAsks())).awaiting
        tasks = [asyncio.create_task(world.runner.call(spec, arguments, NeverAsks()))]
        try:
            await asyncio.wait_for(api.entered.wait(), 2)
            world.clock.now += 901
            # A expiração da pending não libera uma tentativa que ainda espera resposta.
            duplicate = await world.runner.call(spec, arguments, NeverAsks())
            assert duplicate.is_error and "em andamento" in duplicate.text
            assert (await world.runner.call(spec, other, NeverAsks())).awaiting
            assert len(api.calls) == 1
            tasks.append(asyncio.create_task(world.runner.call(spec, other, NeverAsks())))
            await asyncio.wait_for(api.second.wait(), 2)
            assert [r.url.path.rsplit("/", 1)[-1] for r in api.calls] == [
                "synthetic",
                "independent",
            ]
            assert all(not task.done() for task in tasks)
        finally:
            api.release.set()
            outputs = await asyncio.gather(*tasks)
        assert all(output.status == 200 for output in outputs)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("list_forms", {}),
        ("publish_form_draft", {"slug": "demo", "expected_version": "3", "dry_run": True}),
    ],
)
def test_reads_and_dry_runs_keep_their_concurrent_dispatch_without_confirmation(
    suspended: tuple[World, SuspendedApi], name: str, arguments: dict[str, object]
) -> None:
    async def scenario() -> None:
        world, api = suspended
        tasks = [
            asyncio.create_task(world.runner.call(world.tools[name], arguments, NeverAsks()))
            for _ in range(2)
        ]
        try:
            await asyncio.wait_for(api.second.wait(), 2)
            assert len(api.calls) == 2
            assert all("confirm" not in body_of(r) for r in api.calls if r.content)
        finally:
            api.release.set()
            outputs = await asyncio.gather(*tasks)
        assert all(output.status == 200 for output in outputs)

    asyncio.run(scenario())


def test_companion_authority_is_preserved_and_reserved_until_the_response(tmp_path: Path) -> None:
    async def scenario() -> None:
        world = World(tmp_path)
        calls: list[httpx.Request] = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def reply(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if not body_of(request).get("confirm_contact"):
                return error(422, "confirmation_required", "Confirme contato sintético.")
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"version": 4})

        client = ApiClient(world.config, transport=httpx.MockTransport(reply))
        runner = ToolRunner(client, world.config)
        spec = world.tools["publish_form_draft"]
        arguments = {"slug": "demo", "expected_version": "3", "confirm": True}
        confirmed = {**arguments, "confirm_contact": True}
        tasks = []
        try:
            assert (await runner.call(spec, confirmed, NeverAsks())).awaiting
            contact = await runner.call(spec, confirmed, NeverAsks())
            assert contact.awaiting and "confirm_contact" in contact.text
            assert len(calls) == 1 and "confirm_contact" not in body_of(calls[0])
            tasks.append(asyncio.create_task(runner.call(spec, confirmed, NeverAsks())))
            await asyncio.wait_for(entered.wait(), 2)
            tasks.append(asyncio.create_task(runner.call(spec, confirmed, NeverAsks())))
            duplicate = await asyncio.wait_for(asyncio.shield(tasks[1]), 2)
            assert duplicate.is_error and "em andamento" in duplicate.text
            assert len(calls) == 2 and not tasks[0].done()
            assert body_of(calls[1]) == {
                "expected_version": "3",
                "confirm": True,
                "confirm_contact": True,
            }
            release.set()
            assert (await tasks[0]).status == 200
            assert (await runner.call(spec, confirmed, NeverAsks())).awaiting
            assert len(calls) == 2
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.aclose()
            await world.client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["primary", "companion"])
def test_cancellation_during_a_question_clears_all_authority_and_releases_the_call(
    tmp_path: Path, stage: str
) -> None:
    async def scenario() -> None:
        world = World(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()

        class PausedQuestion:
            def __init__(self) -> None:
                self.count = 0

            async def ask(self, message: str) -> ConfirmationOutcome:
                self.count += 1
                if stage == "companion" and self.count == 1:
                    return ConfirmationOutcome.UNSUPPORTED
                entered.set()
                await release.wait()
                return ConfirmationOutcome.UNSUPPORTED

        world.api.when(
            "POST",
            "/forms/{slug}/draft/publish",
            error(422, "confirmation_required", "Confirme contato sintético."),
        )
        spec = world.tools["publish_form_draft"]
        arguments = {
            "slug": "demo",
            "expected_version": "3",
            "confirm": True,
            "confirm_contact": True,
        }
        task = None
        try:
            assert (await world.runner.call(spec, arguments, NeverAsks())).awaiting
            task = asyncio.create_task(world.runner.call(spec, arguments, PausedQuestion()))
            await asyncio.wait_for(entered.wait(), 2)
            duplicate = await world.runner.call(spec, arguments, NeverAsks())
            assert duplicate.is_error and "em andamento" in duplicate.text
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            count = len(world.api.calls())
            replay = await world.runner.call(spec, arguments, NeverAsks())
            assert replay.awaiting and len(world.api.calls()) == count
            # A nova apresentação não herda confirm_contact da tentativa cancelada.
            contact = await world.runner.call(spec, arguments, NeverAsks())
            assert contact.awaiting and "confirm_contact" in contact.text
            assert len(world.api.calls()) == count + 1
            assert "confirm_contact" not in body_of(world.api.calls()[-1])
        finally:
            release.set()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await world.client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "code"),
    [(409, "version_conflict"), (422, "validation_failed"), (502, "upstream_failed")],
)
def test_known_api_refusals_release_the_reservation_and_keep_explicit_retry_semantics(
    suspended: tuple[World, SuspendedApi], status: int, code: str
) -> None:
    async def scenario() -> None:
        world, api = suspended
        spec = world.tools["remove_asset"]
        arguments = {"asset_id": "synthetic", "expected_version": "2", "confirm": True}
        api.response = error(status, code, "Recusa sintética.", current_version="3")
        api.release.set()
        assert (await world.runner.call(spec, arguments, NeverAsks())).awaiting
        refused = await world.runner.call(spec, arguments, NeverAsks())
        assert refused.is_error and refused.status == status and code in refused.text
        assert len(api.calls) == 1  # a recusa não dispara retry automático
        # Preserva o contrato anterior de uma nova chamada explicitamente confirmada após recusa.
        api.response = httpx.Response(200, json={"removed": True})
        retried = await world.runner.call(spec, arguments, NeverAsks())
        assert retried.status == 200 and not retried.awaiting
        assert len(api.calls) == 2

    asyncio.run(scenario())
