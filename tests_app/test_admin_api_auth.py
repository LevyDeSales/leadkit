"""A autenticação da API de admin (`/admin/api/v1`): `Authorization: Bearer`, o papel de cada
operação, o bloqueio por IP da chave errada e o balde de uso de cada chave.

Pela costura HTTP, com as operações reais (`GET /me`, que pede `leitura`, e `POST /api-keys`, que
pede `total`) e com rotas de prova para o que as operações reais ainda não mostram. As chaves são
sorteadas na execução, nunca literais.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from admin_api_support import (
    API,
    ORIGIN,
    add_routes,
    admin_api,
    audited,
    bearer,
    make_api,
    new_key,
)
from fastapi import APIRouter
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from test_admin import FakeClock, csrf_token, login

from app.admin.api import keys
from app.admin.api.limits import HEAVY_COST, RateLimiter
from app.admin.api.operations import OpenRouter, OperationRouter, refuse_unguarded_routes
from app.admin.api.security import current_key, requires
from app.admin.auth import instant
from app.deps import Deps
from app.models import ApiKey

ME = f"{API}/me"
KEYS = f"{API}/api-keys"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def api(deps: Deps, clock: FakeClock) -> TestClient:
    return make_api(deps, clock)


def wrong_secret(key: str) -> str:
    """A mesma chave com outro segredo: o id acha a linha, o segredo não confere."""
    return keys.format_key(key.split("_")[1], secrets.token_urlsafe(32))


def unknown_key() -> str:
    return keys.format_key(secrets.token_hex(4), secrets.token_urlsafe(32))


def create_body(name: str | None = None, role: str = "leitura") -> dict[str, Any]:
    return {"name": name or f"nova-{secrets.token_hex(3)}", "role": role, "confirm": True}


def key_count(deps: Deps) -> int:
    with deps.sessions() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(ApiKey)) or 0


def stored(deps: Deps, key_id: str) -> ApiKey:
    with deps.sessions() as session:
        return session.scalars(sa.select(ApiKey).where(ApiKey.key_id == key_id)).one()


def revoke(deps: Deps, clock: FakeClock, created: keys.CreatedKey) -> None:
    with deps.sessions.begin() as session:
        keys.revoke(session, created.info.key_id, actor="testes", now=instant(clock))


# --- a matriz de autenticação -------------------------------------------------------------------


def test_without_a_key_it_is_a_401_that_asks_for_a_bearer(api: TestClient) -> None:
    response = api.get(ME)

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_valid_key_is_let_in_and_the_call_marks_its_last_use(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    created = new_key(deps, clock, "leitura", name="painel-de-vendas")
    assert stored(deps, created.info.key_id).last_used_at is None

    response = api.get(ME, headers=bearer(created.key))

    assert response.status_code == 200
    assert response.json()["name"] == "painel-de-vendas"
    assert stored(deps, created.info.key_id).last_used_at == instant(clock)


@pytest.mark.parametrize(
    "header",
    [
        "Bearer",
        "Bearer ",
        "Bearer nao-e-uma-chave",
        "Bearer gp_",
        "Basic {basic}",
        "Token {key}",
        "Bearer {key} sobrando",
        "Bearer {upper}",
        "{key}",
    ],
)
def test_a_header_that_is_not_a_well_formed_bearer_key_is_invalid_credentials(
    api: TestClient, deps: Deps, clock: FakeClock, header: str
) -> None:
    created = new_key(deps, clock)
    sent = header.format(key=created.key, upper=created.key.upper(), basic=secrets.token_hex(8))

    response = api.get(ME, headers={"Authorization": sent})

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_wrong_secret_and_an_unknown_key_get_exactly_the_same_answer(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    created = new_key(deps, clock)

    wrong = api.get(ME, headers=bearer(wrong_secret(created.key)))
    unknown = api.get(ME, headers=bearer(unknown_key()))

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.headers["WWW-Authenticate"] == unknown.headers["WWW-Authenticate"] == "Bearer"
    assert wrong.json()["code"] == "invalid_credentials"


def test_an_expired_key_says_so_to_whoever_has_the_secret(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    created = new_key(deps, clock, expires_at=instant(clock) + timedelta(days=1))
    assert api.get(ME, headers=bearer(created.key)).status_code == 200

    clock.advance(24 * 3600 + 1)
    response = api.get(ME, headers=bearer(created.key))

    assert response.status_code == 401 and response.json()["code"] == "key_expired"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_revoked_key_says_so_to_whoever_has_the_secret_and_nothing_to_anyone_else(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    created = new_key(deps, clock)
    revoke(deps, clock, created)

    right = api.get(ME, headers=bearer(created.key))
    guess = api.get(ME, headers=bearer(wrong_secret(created.key)))

    assert right.status_code == 401 and right.json()["code"] == "key_revoked"
    assert right.headers["WWW-Authenticate"] == "Bearer"
    assert guess.status_code == 401 and guess.json()["code"] == "invalid_credentials"


def test_revoking_takes_effect_on_the_very_next_call(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    created = new_key(deps, clock)
    assert api.get(ME, headers=bearer(created.key)).status_code == 200

    revoke(deps, clock, created)

    assert api.get(ME, headers=bearer(created.key)).json()["code"] == "key_revoked"


def test_a_read_key_cannot_do_what_asks_for_total_and_nothing_is_done(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    reader = new_key(deps, clock, "leitura", name="so-leitura")
    before, audit_before = key_count(deps), len(audited(deps, "api_key.create"))

    response = api.post(KEYS, json=create_body(), headers=bearer(reader.key))

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden_role"
    assert "«total»" in response.json()["detail"] and "«leitura»" in response.json()["detail"]
    assert key_count(deps) == before and len(audited(deps, "api_key.create")) == audit_before


def test_a_total_key_does_both(api: TestClient, deps: Deps, clock: FakeClock) -> None:
    total = new_key(deps, clock, "total")

    assert api.get(ME, headers=bearer(total.key)).status_code == 200
    assert api.post(KEYS, json=create_body(), headers=bearer(total.key)).status_code == 201


def test_the_role_is_checked_before_the_confirmation_and_the_body(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    """Quem não pode a operação recebe `403`, e não uma dica do que faltaria no corpo."""
    reader = new_key(deps, clock, "leitura")

    response = api.post(KEYS, json={"nome": "errado"}, headers=bearer(reader.key))

    assert response.status_code == 403


def test_authentication_comes_before_the_body_and_the_confirmation(api: TestClient) -> None:
    """Sem chave, o corpo vazio, a falta do `confirm` e o papel não aparecem: `401`."""
    empty = api.post(KEYS, content=b"", headers={"Content-Type": "application/json"})

    assert api.post(KEYS, json={}).status_code == 401
    assert empty.status_code == 401
    assert api.post(f"{KEYS}/00000000/revoke", json={"confirm": True}).status_code == 401


# --- só o cabeçalho vale ------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["api_key", "key", "token", "access_token", "authorization"])
def test_a_key_in_the_query_string_is_ignored(
    api: TestClient, deps: Deps, clock: FakeClock, name: str
) -> None:
    created = new_key(deps, clock)

    response = api.get(ME, params={name: created.key})

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"


def test_a_key_in_a_cookie_is_ignored(api: TestClient, deps: Deps, clock: FakeClock) -> None:
    created = new_key(deps, clock)

    for cookie in ("api_key", "mcc_admin", "Authorization"):
        api.cookies.set(cookie, created.key)
    response = api.get(ME)

    assert response.status_code == 401


def test_the_admin_session_does_not_work_in_the_api(api: TestClient, deps: Deps) -> None:
    """A sessão do admin só vale nas páginas; por isso nenhuma escrita da API pede token CSRF."""
    assert login(api).status_code == 303
    token = csrf_token(api)
    assert api.get("/admin/api-keys").status_code == 200  # a sessão está de pé
    before = key_count(deps)

    read = api.get(ME)
    write = api.post(KEYS, json=create_body(), headers={**ORIGIN, "X-CSRF-Token": token})

    assert read.status_code == 401 and read.json()["code"] == "invalid_credentials"
    assert write.status_code == 401 and write.json()["code"] == "invalid_credentials"
    assert key_count(deps) == before


def test_an_api_key_does_not_open_the_admin_pages(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    total = new_key(deps, clock, "total")

    page = api.get("/admin/api-keys", headers=bearer(total.key))

    assert page.status_code == 303 and page.headers["location"].startswith("/admin/login")


# --- o bloqueio por IP da chave errada ----------------------------------------------------------


def test_the_fifth_wrong_key_in_a_row_blocks_the_address_for_a_minute(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    good = new_key(deps, clock)

    first_four = [api.get(ME, headers=bearer(unknown_key())) for _ in range(4)]
    fifth = api.get(ME, headers=bearer(unknown_key()))

    assert [r.status_code for r in first_four] == [401, 401, 401, 401]
    assert fifth.status_code == 429
    assert fifth.json()["code"] == "rate_limited"
    assert fifth.headers["Retry-After"] == "60"
    # Bloqueado, nem a chave certa passa: senão o bloqueio diria qual palpite acertou.
    blocked = api.get(ME, headers=bearer(good.key))
    assert blocked.status_code == 429 and blocked.headers["Retry-After"] == "60"


def test_the_block_ends_with_the_minute_and_a_right_key_clears_the_count(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    good = new_key(deps, clock)
    for _ in range(5):
        api.get(ME, headers=bearer(unknown_key()))

    clock.advance(30)
    assert api.get(ME, headers=bearer(good.key)).headers["Retry-After"] == "30"
    clock.advance(31)
    assert api.get(ME, headers=bearer(good.key)).status_code == 200

    for _ in range(4):
        assert api.get(ME, headers=bearer(unknown_key())).status_code == 401
    assert api.get(ME, headers=bearer(unknown_key())).headers["Retry-After"] == "60"


def test_a_wrong_key_after_a_block_doubles_the_wait(api: TestClient, clock: FakeClock) -> None:
    for _ in range(5):
        api.get(ME, headers=bearer(unknown_key()))
    clock.advance(61)

    again = api.get(ME, headers=bearer(unknown_key()))

    assert again.status_code == 429 and again.headers["Retry-After"] == "120"


def test_a_right_key_in_between_resets_the_count(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    good = new_key(deps, clock)

    for _ in range(3):
        for _ in range(4):
            assert api.get(ME, headers=bearer(unknown_key())).status_code == 401
        assert api.get(ME, headers=bearer(good.key)).status_code == 200


def test_the_block_is_by_address_and_apart_from_the_login_one(api: TestClient) -> None:
    for _ in range(5):
        api.get(ME, headers=bearer(unknown_key()))
    throttle = api.app.state.admin_api.state.throttle  # type: ignore[attr-defined]

    assert throttle.retry_after("testclient") > 0
    assert throttle.retry_after("198.51.100.7") == 0
    # O login do admin tem o contador dele: a operadora entra do mesmo endereço.
    assert login(api).status_code == 303


def test_no_authorization_header_is_not_a_guess_and_does_not_count(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    good = new_key(deps, clock)

    codes = {api.get(ME).status_code for _ in range(12)}

    assert codes == {401}
    assert api.get(ME, headers=bearer(good.key)).status_code == 200


def test_a_revoked_key_is_not_a_guess_and_does_not_count(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    revoked = new_key(deps, clock)
    revoke(deps, clock, revoked)
    good = new_key(deps, clock)

    codes = {api.get(ME, headers=bearer(revoked.key)).status_code for _ in range(12)}

    assert codes == {401}
    assert api.get(ME, headers=bearer(good.key)).status_code == 200


def test_a_refused_key_goes_to_the_log_with_the_address_and_the_id_never_the_secret(
    api: TestClient,
    deps: Deps,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging.getLogger("app.admin.api.security"), "disabled", False)
    caplog.set_level(logging.WARNING)
    created = new_key(deps, clock)
    sent = wrong_secret(created.key)
    secret = sent.split("_", 2)[2]

    api.get(ME, headers=bearer(sent))
    api.get(ME, headers=bearer("lixo-que-nao-e-chave"))

    assert f"ip=testclient key_id={created.info.key_id}" in caplog.text
    assert "motivo=invalid_credentials" in caplog.text and "key_id=-" in caplog.text
    assert sent not in caplog.text and secret not in caplog.text
    assert "lixo-que-nao-e-chave" not in caplog.text


# --- o balde de uso de cada chave ---------------------------------------------------------------


def test_each_key_has_a_bucket_and_the_one_that_runs_out_gets_a_429_with_retry_after(
    deps: Deps, clock: FakeClock
) -> None:
    api = make_api(deps, clock, API_RATE_LIMIT_PER_MINUTE="10")
    busy, quiet = new_key(deps, clock), new_key(deps, clock)

    allowed = [api.get(ME, headers=bearer(busy.key)).status_code for _ in range(10)]
    refused = api.get(ME, headers=bearer(busy.key))

    assert allowed == [200] * 10
    assert refused.status_code == 429 and refused.json()["code"] == "rate_limited"
    assert refused.headers["Retry-After"] == "6"  # 10 por minuto: uma ficha a cada 6 s
    assert api.get(ME, headers=bearer(quiet.key)).status_code == 200  # o balde é da chave
    clock.advance(6)
    assert api.get(ME, headers=bearer(busy.key)).status_code == 200


def test_the_default_is_120_a_minute_with_a_burst_of_30(deps: Deps, clock: FakeClock) -> None:
    api = make_api(deps, clock)
    created = new_key(deps, clock)

    burst = [api.get(ME, headers=bearer(created.key)).status_code for _ in range(30)]
    refused = api.get(ME, headers=bearer(created.key))

    assert burst == [200] * 30 and refused.status_code == 429
    assert refused.headers["Retry-After"] == "1"
    clock.advance(10)  # 120 por minuto são 2 por segundo: 20 fichas
    assert [api.get(ME, headers=bearer(created.key)).status_code for _ in range(20)] == [200] * 20
    assert api.get(ME, headers=bearer(created.key)).status_code == 429


def test_a_call_the_role_refuses_still_spends_a_token(deps: Deps, clock: FakeClock) -> None:
    """A chave foi identificada: sondar operações `total` com uma chave `leitura` também é limitado."""
    api = make_api(deps, clock, API_RATE_LIMIT_PER_MINUTE="20")
    reader = new_key(deps, clock, "leitura")
    for _ in range(19):
        assert api.get(ME, headers=bearer(reader.key)).status_code == 200

    refused = [
        api.post(KEYS, json=create_body(), headers=bearer(reader.key)).status_code for _ in range(3)
    ]

    assert refused == [403, 429, 429]
    clock.advance(3)  # 20 por minuto: uma ficha a cada 3 s
    assert api.get(ME, headers=bearer(reader.key)).status_code == 200


def test_a_failed_authentication_costs_no_key_a_token(deps: Deps, clock: FakeClock) -> None:
    api = make_api(deps, clock, API_RATE_LIMIT_PER_MINUTE="5")
    created = new_key(deps, clock)

    for _ in range(3):  # segredo errado: 401, sem tocar o balde de ninguém
        api.get(ME, headers=bearer(wrong_secret(created.key)))
    codes = [api.get(ME, headers=bearer(created.key)).status_code for _ in range(5)]

    assert codes == [200] * 5


def test_the_token_bucket_in_isolation() -> None:
    now = [100.0]
    limiter = RateLimiter(60, lambda: now[0])  # 1 por segundo; rajada de 30

    assert limiter.capacity == 30
    assert [limiter.take("k") for _ in range(30)] == [0] * 30
    assert limiter.take("k") == 1
    assert limiter.take("outra") == 0
    now[0] += 5
    assert limiter.take("k", HEAVY_COST) == 0  # as 5 fichas da chamada pesada
    assert limiter.take("k", HEAVY_COST) == 5  # espera 5 s por 5 fichas
    now[0] += 1_000_000
    assert limiter.take("k") == 0  # o balde nunca passa da rajada
    with pytest.raises(ValueError):
        RateLimiter(0, lambda: 0.0)


def test_a_limit_below_the_burst_shrinks_the_burst_to_it() -> None:
    assert RateLimiter(10, lambda: 0.0).capacity == 10
    assert RateLimiter(2, lambda: 0.0).capacity == HEAVY_COST  # a pesada sempre cabe


def test_a_heavy_operation_spends_five_tokens(deps: Deps, clock: FakeClock) -> None:
    api = make_api(deps, clock, API_RATE_LIMIT_PER_MINUTE="10")
    router = OperationRouter(tag="Prova", description="Provas.")

    @router.operation(
        "GET",
        "/probe/heavy",
        operation_id="probe_heavy",
        role="leitura",
        heavy=True,
        summary="Pesada",
        description="Uma operação de prova que custa cinco fichas.",
        returns="Nada.",
        response_model=dict[str, bool],
    )
    def heavy() -> dict[str, bool]:
        return {"ok": True}

    add_routes(api, router)
    created = new_key(deps, clock)

    codes = [
        api.get(f"{API}/probe/heavy", headers=bearer(created.key)).status_code for _ in range(3)
    ]

    assert codes == [200, 200, 429]  # 10 fichas: duas pesadas


# --- a transação da autenticação ----------------------------------------------------------------


def test_the_authentication_commits_before_the_operation_runs_and_holds_no_lock(
    deps: Deps, clock: FakeClock
) -> None:
    """O `UPDATE` do `last_used_at` segura o lock da linha da chave até o commit. Se a autenticação
    dividisse a transação com a operação, uma operação demorada (um PDF, o Baserow) travaria a chave:
    aqui, a operação vê o `last_used_at` já confirmado, de outra conexão, e consegue escrever na
    linha da chave sem esperar."""
    api = make_api(deps, clock)
    caller = new_key(deps, clock)
    router = OperationRouter(tag="Prova", description="Provas.")
    seen: dict[str, Any] = {}

    @router.operation(
        "GET",
        "/probe/during",
        operation_id="probe_during",
        role="leitura",
        summary="Durante",
        description="Olha a linha da chave de outra conexão, no meio da operação.",
        returns="O que a operação viu.",
        response_model=dict[str, bool],
    )
    def during() -> dict[str, bool]:
        with deps.sessions.begin() as other:
            other.execute(sa.text("set local lock_timeout = '1s'"))  # travada, falharia em 1 s
            seen["last_used_at"] = other.scalar(
                sa.select(ApiKey.last_used_at).where(ApiKey.key_id == caller.info.key_id)
            )
            other.execute(
                sa.update(ApiKey)
                .where(ApiKey.key_id == caller.info.key_id)
                .values(revoked_by="provado")
            )
        return {"ok": True}

    add_routes(api, router)

    response = api.get(f"{API}/probe/during", headers=bearer(caller.key))

    assert response.status_code == 200
    assert seen["last_used_at"] == instant(clock)  # já confirmado antes da operação


def test_the_database_down_while_authenticating_is_a_503(
    api: TestClient, deps: Deps, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = new_key(deps, clock)

    class Down:
        def begin(self) -> Any:
            raise sa.exc.OperationalError("SELECT 1", {}, Exception("sem rota"))

    monkeypatch.setattr(deps, "sessions", Down())
    response = api.get(ME, headers=bearer(created.key))

    assert response.status_code == 503 and response.json()["code"] == "unavailable"
    assert response.headers["Retry-After"] == "5"


# --- negado por padrão, e o que o OpenAPI diz ---------------------------------------------------


def test_a_route_without_a_declared_role_is_refused_when_it_is_registered() -> None:
    router = OperationRouter(tag="Prova", description="Provas.")

    with pytest.raises(RuntimeError, match="sem papel declarado"):

        @router.get("/sem-papel")
        def open_door() -> dict[str, bool]:
            return {"ok": True}


def plain_route_on(api: TestClient) -> None:
    router = APIRouter()

    @router.get("/probe/aberta")
    def open_door() -> dict[str, bool]:
        return {"ok": True}

    admin_api(api).include_router(router)  # sem o guarda de `add_routes`


def test_the_assembly_refuses_a_route_that_is_not_an_operation_nor_declared_open(
    api: TestClient,
) -> None:
    """Um `APIRouter` comum não vigia o papel: `OperationRoute` nem chega a rodar. O fecho da montagem
    recusa a rota, e `create_admin_api` já a aplica."""
    refuse_unguarded_routes(admin_api(api))  # a sub-aplicação inteira, como montada, passa
    plain_route_on(api)

    with pytest.raises(RuntimeError, match=r"/probe/aberta.*sem papel declarado"):
        refuse_unguarded_routes(admin_api(api))


def test_the_assembly_refuses_a_starlette_route_or_mount_too(api: TestClient) -> None:
    sub = admin_api(api)
    sub.router.routes.append(Route("/aberta", lambda request: PlainTextResponse("oi")))

    with pytest.raises(RuntimeError, match=r"/aberta.*Route"):
        refuse_unguarded_routes(sub)


def test_an_open_route_is_the_declared_exception_and_a_role_route_needs_no_exception(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    sub = admin_api(api)
    open_router = OpenRouter()

    @open_router.get("/probe/sem-chave")
    def without_key() -> dict[str, bool]:
        return {"ok": True}

    add_routes(api, open_router)
    refuse_unguarded_routes(sub)

    assert api.get(f"{API}/probe/sem-chave").json() == {"ok": True}  # aberta, de propósito
    assert api.get(ME).status_code == 401  # as de operação seguem pedindo a chave


def test_an_unknown_role_cannot_be_declared() -> None:
    with pytest.raises(ValueError, match="papel desconhecido"):
        requires("administrador")


def test_reading_the_key_without_the_gate_is_a_programming_error() -> None:
    with pytest.raises(RuntimeError, match="requires"):
        current_key(Request({"type": "http", "headers": []}))


def test_the_openapi_of_the_api_declares_the_role_the_marks_and_the_bearer_scheme(
    api: TestClient,
) -> None:
    document = api.app.state.admin_api.openapi()  # type: ignore[attr-defined]
    assert document["servers"] == [{"url": "/admin/api/v1"}]  # relativo, igual em qualquer máquina
    operations = [
        operation for methods in document["paths"].values() for operation in methods.values()
    ]

    marks = {
        operation["operationId"]: (
            operation["x-required-role"],
            operation.get("x-destructive", False),
            operation.get("x-external-effect", False),
        )
        for operation in operations
    }
    # as quatro das chaves, com as marcas delas; as fases de recurso acrescentam as suas, e cada
    # uma tem o papel e as marcas conferidos pelo teste de contrato e pela matriz
    the_keys = ("get_current_api_key", "list_api_keys", "create_api_key", "revoke_api_key")
    assert {name: marks[name] for name in the_keys} == {
        "get_current_api_key": ("leitura", False, False),
        "list_api_keys": ("leitura", False, False),
        "create_api_key": ("total", False, True),
        "revoke_api_key": ("total", True, False),
    }
    assert all(role in ("leitura", "total") for role, _, _ in marks.values())
    scheme = document["components"]["securitySchemes"]["ApiKeyAuth"]
    assert (scheme["type"], scheme["scheme"]) == ("http", "bearer")
    assert all(operation["security"] == [{"ApiKeyAuth": []}] for operation in operations)
