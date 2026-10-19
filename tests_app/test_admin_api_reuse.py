"""A sub-aplicação da API de admin que os testes reaproveitam (`lend_admin_api`, no `conftest.py`).

Montar a sub-aplicação custa de 40 a 80 ms de CPU, e mais de mil testes criam o app. Os testes montam
algumas poucas por processo e religam o estado a cada `create_app`. Estes testes provam que o
reaproveitamento não muda o que um teste vê: o estado religado é o de uma sub-aplicação nova, dois
apps no mesmo teste não dividem nada, e o que um teste mudou na montagem não chega ao seguinte.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from admin_api_support import API, ApiClient, add_routes, admin_api
from conftest import _IDLE_ADMIN_APIS, lend_admin_api, take_back_admin_apis
from fastapi import APIRouter, FastAPI
from fastapi.routing import iter_route_contexts
from test_admin import FakeClock

from app.admin.api.application import bind_admin_api, build_admin_api, create_admin_api
from app.admin.api.operations import OperationRouter
from app.deps import Deps

STATE = (
    "clock",
    "trigger",
    "test_trigger",
    "admin_auth",
    "environment",
    "throttle",
    "limiter",
)


def paths_of(api: FastAPI) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (context.path, tuple(sorted(context.methods or ())))
        for context in iter_route_contexts(api.routes)
    ]


def state_of(api: FastAPI) -> dict[str, Any]:
    return dict(api.state._state)


def test_binding_the_state_leaves_a_built_api_as_create_admin_api_would() -> None:
    clock = FakeClock()

    async def trigger(job_id: Any, attempt: int) -> str | None:
        return None

    created = create_admin_api(clock=clock, trigger=trigger, environment="test")
    bound = bind_admin_api(build_admin_api(), clock=clock, trigger=trigger, environment="test")

    assert set(state_of(created)) == set(state_of(bound)) == set(STATE)
    for name in ("clock", "trigger", "test_trigger", "admin_auth", "environment"):
        assert getattr(bound.state, name) is getattr(created.state, name), name
    assert bound.state.limiter.capacity == created.state.limiter.capacity
    assert bound.version == created.version
    assert paths_of(bound) == paths_of(created) != []


def test_a_lent_api_has_the_state_of_the_call_that_borrowed_it() -> None:
    first_clock, second_clock = FakeClock(), FakeClock()
    first = lend_admin_api(clock=first_clock, environment="test")
    first_throttle, first_limiter = first.state.throttle, first.state.limiter
    take_back_admin_apis()

    second = lend_admin_api(clock=second_clock)

    assert second is first  # a mesma sub-aplicação, montada uma vez
    assert second.state.clock is second_clock
    assert (
        second.state.environment == "production"
    )  # o padrão, e não o "test" do empréstimo anterior
    assert second.state.limiter is not first_limiter  # o balde de uso também
    assert second.state.throttle is not first_throttle  # o bloqueio nasce novo a cada empréstimo


def test_two_apps_in_one_test_do_not_share_the_sub_application(
    api_client: Callable[..., ApiClient],
) -> None:
    reader, writer = api_client("leitura"), api_client("total")

    assert admin_api(reader) is not admin_api(writer)
    assert admin_api(reader).state.clock is reader.clock
    assert admin_api(writer).state.clock is writer.clock
    assert reader.clock is not writer.clock
    reader.clock.advance(3600)  # o relógio de um não anda o do outro
    assert admin_api(writer).state.clock() != admin_api(reader).state.clock()
    assert reader.get(f"{API}/me").status_code == 200
    assert writer.get(f"{API}/me").status_code == 200


def probe_router() -> APIRouter:
    router = OperationRouter(
        tag="Prova", description="Operação de prova do teste de reaproveitamento."
    )

    @router.operation(
        "GET",
        "/probe/reuse",
        operation_id="probe_reuse",
        role="leitura",
        summary="Prova",
        description="Uma operação que só existe neste teste.",
        returns="Nada.",
        response_model=dict[str, bool],
    )
    def probe() -> dict[str, bool]:
        return {"ok": True}

    return router


def test_an_api_with_a_probe_route_is_not_lent_again(
    api_client: Callable[..., ApiClient], deps: Deps
) -> None:
    client = api_client("total")
    probed = admin_api(client)
    add_routes(client, probe_router())
    assert client.get(f"{API}/probe/reuse").status_code == 200
    idle_before = len(_IDLE_ADMIN_APIS)

    take_back_admin_apis()  # o que o fim do teste faz

    assert (
        len(_IDLE_ADMIN_APIS) == idle_before
    )  # a que ganhou a rota de prova não voltou para a fila
    assert all(api is not probed for api, _ in _IDLE_ADMIN_APIS)
    fresh = lend_admin_api(clock=time.time)
    assert fresh is not probed
    assert "/probe/reuse" not in fresh.openapi()["paths"]
    assert client.get(f"{API}/probe/reuse").status_code == 200  # o app do teste segue inteiro


def test_the_document_a_test_changed_is_not_kept_for_the_next_one() -> None:
    api = lend_admin_api(clock=time.time)
    api.openapi()["paths"].clear()  # o que um teste poderia fazer com o dicionário guardado
    take_back_admin_apis()

    again = lend_admin_api(clock=time.time)

    assert again is api
    assert again.openapi()["paths"]


def test_what_a_route_kept_in_the_state_does_not_reach_the_next_test(
    api_client: Callable[..., ApiClient],
) -> None:
    """A rota do `/openapi.json` guarda o texto do documento em `app.state`: se a sub-aplicação voltasse
    com ele, o teste seguinte leria o documento do anterior."""
    client = api_client("total")
    api = admin_api(client)
    assert client.get(f"{API}/openapi.json").status_code == 200
    assert "openapi_text" in api.state._state  # sem isto, o teste não provaria nada

    take_back_admin_apis()  # o que o fim do teste faz
    again = lend_admin_api(clock=time.time)

    assert again is api  # a mesma sub-aplicação, montada uma vez
    assert set(again.state._state) == set(create_admin_api(clock=time.time).state._state)
    assert set(again.state._state) == set(STATE)
