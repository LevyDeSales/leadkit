"""Confirmação pelo adaptador real e pelo runner, sem rede ou efeitos externos."""

import asyncio
import json
import logging
import socket
from types import SimpleNamespace
from typing import Any

import pytest
from admin_mcp_support import answer, body_of, error, optional
from admin_mcp_world import World

types = optional("mcp.types", module=True)
server = optional("app.admin_mcp.server", module=True)
exceptions = optional("mcp.shared.exceptions", module=True)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid(*_: Any, **__: Any) -> None:
        raise AssertionError("Este ensaio não usa rede")

    monkeypatch.setattr(socket.socket, "connect", forbid)


class Session:
    """Respostas na interface externa de elicitation; o adaptador do produto é real."""

    def __init__(self, *replies: Any, supported: bool = True) -> None:
        self.client_params = SimpleNamespace(
            capabilities=SimpleNamespace(
                elicitation=types.ElicitationCapability() if supported else None
            )
        )
        self.replies = list(replies)

    async def elicit_form(self, **_: Any) -> Any:
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def world(tmp_path: Any) -> Any:
    found = World(tmp_path)
    yield found
    found.close()


def remove(world: World, session: Session) -> Any:
    return asyncio.run(
        world.runner.call(
            world.tools["remove_asset"],
            {"asset_id": "synthetic", "expected_version": "2", "confirm": True},
            server.SessionPrompter(session, 1),
        )
    )


def test_repeated_timeouts_never_create_or_consume_fallback(world: World) -> None:
    world.api.when("DELETE", "/assets/{asset_id}", answer(200, {"removed": True}))

    for _ in range(2):
        result = remove(world, Session(TimeoutError("falha sintética")))
        assert result.is_error and not result.awaiting
        assert world.api.calls() == []

    # Uma falha também não deixa uma autorização para um cliente sem formulário.
    assert remove(world, Session(supported=False)).awaiting
    assert world.api.calls() == []


def test_a_composite_mapping_gate_blocks_timeout_and_invalidates_fallback(world: World) -> None:
    state = {
        "published": True,
        "connected": True,
        "connection": {
            "version": "2",
            "base_url": "https://baserow.example.test",
            "table_id": 7,
            "active": True,
            "purpose": "internal",
        },
        "mapping": [{"field_id": 104, "source": "job.public_id"}],
        "fields": [],
    }
    world.api.when("GET", "/forms/{slug}/integrations/baserow", answer(200, state))
    arguments = {
        "slug": "demo",
        "base_url": "https://baserow.example.test",
        "table_id": 7,
        "sources": {"101": "lead.name"},
        "confirm": True,
    }

    def mapping(session: Session) -> Any:
        return asyncio.run(
            world.composites["connect_baserow_integration"].invoke(
                world.runner, world.tools, arguments, server.SessionPrompter(session, 1)
            )
        )

    assert mapping(Session(supported=False)).awaiting
    failed = mapping(Session(TimeoutError("falha sintética")))
    assert failed.is_error and not failed.awaiting
    assert failed.confirmation.value == "timed_out"
    assert "tempo de espera" in failed.text
    assert mapping(Session(supported=False)).awaiting
    assert all(request.method == "GET" for request in world.api.calls())


NEGATIVE = [
    types.ElicitResult(action="decline"),
    types.ElicitResult(action="cancel"),
    types.ElicitResult(action="accept", content={"confirmar": False}),
    types.ElicitResult(action="accept", content={}),
    types.ElicitResult(action="accept"),
    types.ElicitResult(action="accept", content={"confirmar": "true"}),
    types.ElicitResult(action="accept", content={"confirmar": 1}),
    types.ElicitResult(action="accept", content={"confirmar": None}),
]


@pytest.mark.parametrize("reply", NEGATIVE)
@pytest.mark.parametrize("later", ["unsupported", "timeout"])
@pytest.mark.parametrize("initial", ["unsupported", "timeout"])
def test_negative_erases_all_previous_authority(
    world: World, reply: Any, later: str, initial: str
) -> None:
    world.api.when("DELETE", "/assets/{asset_id}", answer(200, {"removed": True}))
    first = remove(
        world, Session(supported=False) if initial == "unsupported" else Session(TimeoutError())
    )
    assert first.awaiting if initial == "unsupported" else first.is_error
    denied = remove(world, Session(reply))
    assert denied.is_error and not denied.awaiting
    result = remove(
        world, Session(supported=False) if later == "unsupported" else Session(TimeoutError())
    )
    assert result.awaiting if later == "unsupported" else result.is_error
    assert world.api.calls() == []


@pytest.mark.parametrize("reply", [*NEGATIVE, TimeoutError(), RuntimeError()])
def test_companion_never_inherits_old_fallback_flags(world: World, reply: Any) -> None:
    effects: list[bool] = []

    def publish(request: Any) -> Any:
        if body_of(request).get("confirm_contact") is True:
            effects.append(True)
            return answer(200, {"version": 4})
        return error(422, "confirmation_required", "Confirmação sintética de contato")

    world.api.when("POST", "/forms/{slug}/draft/publish", publish)
    arguments = {
        "slug": "demo",
        "expected_version": "3",
        "note": "Ensaio",
        "confirm": True,
        "confirm_contact": True,
    }

    def call(session: Session) -> Any:
        return asyncio.run(
            world.runner.call(
                world.tools["publish_form_draft"], arguments, server.SessionPrompter(session, 1)
            )
        )

    assert call(Session(supported=False)).awaiting
    assert call(Session(supported=False)).awaiting
    denied = call(Session(types.ElicitResult(action="accept", content={"confirmar": True}), reply))
    assert denied.is_error and not denied.awaiting
    assert call(Session(supported=False)).awaiting
    assert effects == []


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (types.ElicitResult(action="accept", content={"confirmar": True}), "approved"),
        (NEGATIVE[0], "declined"),
        (NEGATIVE[1], "cancelled"),
        (NEGATIVE[2], "unchecked"),
        *[(reply, "invalid") for reply in NEGATIVE[3:]],
        (TimeoutError(), "timed_out"),
        (RuntimeError(), "unavailable"),
        (
            exceptions.McpError(types.ErrorData(code=408, message="timeout sintético do SDK")),
            "timed_out",
        ),
    ],
)
def test_adapter_preserves_the_observed_outcome(reply: Any, expected: str) -> None:
    result = asyncio.run(server.SessionPrompter(Session(reply), 1).ask("Confirma?"))
    assert result.value == expected


def test_uninitialized_is_unavailable_and_declared_absence_is_unsupported() -> None:
    uninitialized = Session()
    uninitialized.client_params = None
    result = asyncio.run(server.SessionPrompter(uninitialized, 1).ask("Confirma?"))
    assert result.value == "unavailable"
    result = asyncio.run(server.SessionPrompter(Session(supported=False), 1).ask("Confirma?"))
    assert result.value == "unsupported"


def test_diagnostics_record_only_allowlisted_structure(caplog: Any, monkeypatch: Any) -> None:
    # O fileConfig das migrations de outros testes desliga loggers já existentes.
    monkeypatch.setattr(logging.getLogger("gerador_mcp"), "disabled", False)
    marker = "conteudo-privado-sintetico"
    session = Session(
        types.ElicitResult(action="accept", content={"confirmar": marker, "other": marker})
    )
    session.client_params.protocolVersion = "2025-11-25"
    session.client_params.clientInfo = SimpleNamespace(name=marker, version=marker)
    with caplog.at_level(logging.INFO, logger="gerador_mcp"):
        asyncio.run(server.SessionPrompter(session, marker).ask(marker))
        asyncio.run(server.SessionPrompter(Session(RuntimeError(marker)), marker).ask(marker))
    records = [
        json.loads(record.message.removeprefix("mcp_confirmation "))
        for record in caplog.records
        if record.message.startswith("mcp_confirmation ")
    ]
    assert len(records) == 2
    assert set(records[0]) == {
        "attempt",
        "outcome",
        "action",
        "content_type",
        "confirmation_present",
        "confirmation_type",
        "failure",
        "form_capability",
        "url_capability",
        "initialized",
        "protocol_requested",
        "protocol_negotiated",
        "client_reported_version",
        "sdk_package_version",
        "server_package_version",
    }
    assert records[0]["outcome"] == "invalid"
    assert records[0]["action"] == "accept"
    assert records[0]["confirmation_type"] == "string"
    assert records[0]["protocol_requested"] == "2025-11-25"
    assert records[0]["protocol_negotiated"] == "unknown"
    assert records[0]["client_reported_version"] == "unknown"
    assert records[1]["failure"] == "client_error"
    assert marker not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
