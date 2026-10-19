"""As quatro operações das chaves de API (`/admin/api/v1`): `get_current_api_key`, `list_api_keys`,
`create_api_key` e `revoke_api_key`, pela costura HTTP.

Elas chamam o serviço `app.admin.api.keys`, o mesmo da página `/admin/api-keys`: a paridade com a página
(mesmo estado, mesma auditoria, menos o autor) está no fim. As chaves destes testes são sorteadas na
execução, nunca literais.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa
from admin_api_support import API, ORIGIN, audited, bearer, make_api, new_key, url_of
from fastapi.testclient import TestClient
from test_admin import FakeClock, csrf_token, login

from app.admin.api import keys
from app.admin.auth import instant
from app.deps import Deps
from app.models import API_KEY_ROLES, ApiKey, AuditLog

ME = f"{API}/me"
KEYS = f"{API}/api-keys"
KEY_SHAPE = re.compile(r"gp_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
PAGE = "/admin/api-keys"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def api(deps: Deps, clock: FakeClock) -> TestClient:
    return make_api(deps, clock)


@pytest.fixture
def caller(deps: Deps, clock: FakeClock) -> keys.CreatedKey:
    """A chave `total` que faz as chamadas dos testes."""
    return new_key(deps, clock, "total", name="quem-chama")


def headers_of(created: keys.CreatedKey) -> dict[str, str]:
    return bearer(created.key)


def create(api: TestClient, caller: keys.CreatedKey, name: str | None = None, **body: Any) -> Any:
    sent = {
        "name": name if name is not None else f"nova-{secrets.token_hex(3)}",
        "role": "leitura",
        "confirm": True,
        **body,
    }
    return api.post(url_of("create_api_key"), json=sent, headers=headers_of(caller))


def revoke(api: TestClient, caller: keys.CreatedKey, key_id: str, **body: Any) -> Any:
    return api.post(
        f"{KEYS}/{key_id}/revoke", json={"confirm": True, **body}, headers=headers_of(caller)
    )


def row(deps: Deps, name: str) -> ApiKey:
    with deps.sessions() as session:
        return session.scalars(sa.select(ApiKey).where(ApiKey.name == name)).one()


def key_count(deps: Deps) -> int:
    with deps.sessions() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(ApiKey)) or 0


def audits(deps: Deps, action: str) -> list[AuditLog]:
    return audited(deps, action)


def everything_stored(deps: Deps) -> str:
    with deps.sessions() as session:
        return "\n".join(
            str(row)
            for table in ("api_keys", "audit_log")
            for row in session.execute(sa.text(f"SELECT * FROM {table}"))
        )


# --- get_current_api_key ------------------------------------------------------------------------


def test_me_says_who_is_calling_without_the_secret(
    api: TestClient, deps: Deps, clock: FakeClock
) -> None:
    created = new_key(
        deps,
        clock,
        "leitura",
        name="painel-de-vendas",
        expires_at=instant(clock) + timedelta(days=90),
    )

    response = api.get(ME, headers=headers_of(created))

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "key_id": created.info.key_id,
        "name": "painel-de-vendas",
        "role": "leitura",
        "state": "active",
        "created_by": "testes",
        "created_at": body["created_at"],
        "expires_at": body["expires_at"],
        "revoked_at": None,
        "revoked_by": None,
        "last_used_at": body["last_used_at"],
    }
    assert datetime.fromisoformat(body["created_at"]) == instant(clock)
    assert datetime.fromisoformat(body["expires_at"]) == instant(clock) + timedelta(days=90)
    assert body["created_at"].endswith("Z") and body["expires_at"].endswith("Z")
    assert datetime.fromisoformat(body["last_used_at"]) == instant(clock)  # esta chamada
    assert created.key not in response.text and "secret" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_me_works_for_every_role(api: TestClient, deps: Deps, clock: FakeClock) -> None:
    for role in API_KEY_ROLES:
        created = new_key(deps, clock, role)

        assert api.get(ME, headers=headers_of(created)).json()["role"] == role


# --- list_api_keys ------------------------------------------------------------------------------


def test_the_list_is_an_envelope_with_every_key_the_newest_first_the_revoked_too(
    api: TestClient, deps: Deps, clock: FakeClock, caller: keys.CreatedKey
) -> None:
    clock.advance(60)
    second = new_key(deps, clock, "leitura", name="segunda")
    clock.advance(60)
    third = new_key(deps, clock, "leitura", name="terceira")
    with deps.sessions.begin() as session:
        keys.revoke(session, second.info.key_id, actor="testes", now=instant(clock))

    response = api.get(url_of("list_api_keys"), headers=headers_of(caller))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "next_cursor"} and body["next_cursor"] is None
    assert [(item["name"], item["state"]) for item in body["items"]] == [
        ("terceira", "active"),
        ("segunda", "revoked"),
        ("quem-chama", "active"),
    ]
    revoked = body["items"][1]
    assert revoked["revoked_by"] == "testes" and revoked["revoked_at"].endswith("Z")
    assert third.key not in response.text and second.key not in response.text


def test_the_list_never_carries_a_secret_nor_a_hash(
    api: TestClient, deps: Deps, clock: FakeClock, caller: keys.CreatedKey
) -> None:
    other = new_key(deps, clock)
    response = api.get(KEYS, headers=headers_of(caller))

    for created in (caller, other):
        secret = created.key.split("_", 2)[2]
        assert secret not in response.text and created.key not in response.text
        assert row(deps, created.info.name).secret_hash not in response.text
    assert {"key", "secret_hash"}.isdisjoint(response.json()["items"][0])


def test_a_read_key_may_list(api: TestClient, deps: Deps, clock: FakeClock) -> None:
    reader = new_key(deps, clock, "leitura")

    assert api.get(KEYS, headers=headers_of(reader)).status_code == 200


def test_the_state_follows_the_clock_of_the_app(
    api: TestClient, deps: Deps, clock: FakeClock, caller: keys.CreatedKey
) -> None:
    new_key(deps, clock, name="temporaria", expires_at=instant(clock) + timedelta(days=1))

    def state_of_it() -> str:
        items = api.get(KEYS, headers=headers_of(caller)).json()["items"]
        return next(item["state"] for item in items if item["name"] == "temporaria")

    assert state_of_it() == "active"
    clock.advance(24 * 3600 + 1)
    assert state_of_it() == "expired"


def test_the_pages_of_the_list_follow_the_cursor_without_skipping_a_key(
    api: TestClient, deps: Deps, clock: FakeClock, caller: keys.CreatedKey
) -> None:
    for number in range(1, 5):
        clock.advance(60)
        new_key(deps, clock, name=f"chave-{number}")
    # 5 chaves, da mais nova para a mais antiga: chave-4, chave-3, chave-2, chave-1, quem-chama

    first = api.get(KEYS, params={"limit": 2}, headers=headers_of(caller)).json()
    clock.advance(60)
    new_key(deps, clock, name="chegou-no-meio")  # entra na frente: não muda o que falta
    second = api.get(
        KEYS, params={"limit": 2, "cursor": first["next_cursor"]}, headers=headers_of(caller)
    ).json()
    third = api.get(
        KEYS, params={"limit": 2, "cursor": second["next_cursor"]}, headers=headers_of(caller)
    ).json()

    names = [[item["name"] for item in page["items"]] for page in (first, second, third)]
    assert names == [["chave-4", "chave-3"], ["chave-2", "chave-1"], ["quem-chama"]]
    assert third["next_cursor"] is None


def test_a_cursor_that_is_not_ours_is_a_422_on_the_cursor(
    api: TestClient, caller: keys.CreatedKey
) -> None:
    response = api.get(KEYS, params={"cursor": "lixo"}, headers=headers_of(caller))

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "cursor"


# --- create_api_key -----------------------------------------------------------------------------


def test_creating_a_key_returns_it_whole_once_and_it_works(
    api: TestClient, deps: Deps, clock: FakeClock, caller: keys.CreatedKey
) -> None:
    expires = instant(clock) + timedelta(days=30)

    response = create(api, caller, "painel-de-vendas", role="total", expires_at=expires.isoformat())

    assert response.status_code == 201
    body = response.json()
    assert KEY_SHAPE.fullmatch(body["key"])
    assert body["key"].split("_")[1] == body["key_id"]
    assert (body["name"], body["role"], body["state"]) == ("painel-de-vendas", "total", "active")
    assert body["created_by"] == "chave:quem-chama"
    assert datetime.fromisoformat(body["expires_at"]) == expires
    assert response.headers["Cache-Control"] == "no-store"
    # A chave que acabou de sair vale, com o papel que tem.
    mine = api.get(ME, headers=bearer(body["key"]))
    assert mine.status_code == 200 and mine.json()["name"] == "painel-de-vendas"
    assert row(deps, "painel-de-vendas").created_by == "chave:quem-chama"


def test_a_key_without_validity_has_none(api: TestClient, caller: keys.CreatedKey) -> None:
    for body in ({}, {"expires_at": None}):
        response = create(api, caller, **body)

        assert response.status_code == 201 and response.json()["expires_at"] is None


def test_the_validity_may_come_with_any_utc_offset(
    api: TestClient, deps: Deps, clock: FakeClock, caller: keys.CreatedKey
) -> None:
    moment = datetime.fromtimestamp(clock(), UTC) + timedelta(days=10)
    local = moment.astimezone(timezone(timedelta(hours=-3))).isoformat()

    created = create(api, caller, "no-fuso-do-brasil", expires_at=local)

    assert created.status_code == 201
    assert row(deps, "no-fuso-do-brasil").expires_at == moment
    assert datetime.fromisoformat(created.json()["expires_at"]) == moment


def test_the_secret_is_only_in_the_response_that_creates_it(
    api: TestClient,
    deps: Deps,
    clock: FakeClock,
    caller: keys.CreatedKey,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("app.admin.api.api_keys", "app.admin.api.security", "app.admin.api.keys"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    caplog.set_level(logging.DEBUG)

    created = create(api, caller, "so-uma-vez").json()
    secret = created["key"].split("_", 2)[2]
    later = [
        api.get(ME, headers=bearer(created["key"])),
        api.get(KEYS, headers=headers_of(caller)),
        create(api, caller, "so-uma-vez"),  # o nome repetido é recusado
    ]

    assert "api: chave de API criada nome=so-uma-vez" in caplog.text  # o log existe, sem o segredo
    assert created["key"] not in caplog.text and secret not in caplog.text
    for response in later:
        assert created["key"] not in response.text and secret not in response.text
    stored = everything_stored(deps)
    assert created["key"] not in stored and secret not in stored
    assert row(deps, "so-uma-vez").secret_hash == keys.digest(secret)


def test_creating_is_audited_in_the_name_of_the_key_that_called(
    api: TestClient, deps: Deps, clock: FakeClock, caller: keys.CreatedKey
) -> None:
    expires = instant(clock) + timedelta(days=90)
    created = create(api, caller, "auditada", role="total", expires_at=expires.isoformat()).json()

    (entry,) = [e for e in audits(deps, "api_key.create") if e.details["name"] == "auditada"]
    assert entry.actor == "chave:quem-chama"
    assert (entry.target_type, entry.target_id) == ("api_key", created["key_id"])
    assert entry.details["role"] == "total"
    assert datetime.fromisoformat(entry.details["expires_at"]) == expires
    assert created["key"] not in repr(entry.details)


def test_a_total_key_can_create_another_total_key_and_the_audit_says_who(
    api: TestClient, deps: Deps, caller: keys.CreatedKey
) -> None:
    child = create(api, caller, "filha", role="total").json()
    grandchild = api.post(
        KEYS,
        json={"name": "neta", "role": "leitura", "confirm": True},
        headers=bearer(child["key"]),
    )

    assert grandchild.status_code == 201 and grandchild.json()["created_by"] == "chave:filha"
    actors = {e.details["name"]: e.actor for e in audits(deps, "api_key.create")}
    assert actors["filha"] == "chave:quem-chama" and actors["neta"] == "chave:filha"


@pytest.mark.parametrize(
    "confirm", [{}, {"confirm": False}, {"confirm": "true"}, {"confirm": 1}, {"confirm": None}]
)
def test_creating_without_the_confirmation_creates_nothing(
    api: TestClient, deps: Deps, caller: keys.CreatedKey, confirm: dict[str, Any]
) -> None:
    before, audit_before = key_count(deps), len(audits(deps, "api_key.create"))

    response = api.post(
        KEYS, json={"name": "sem-confirmar", "role": "total", **confirm}, headers=headers_of(caller)
    )

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert key_count(deps) == before and len(audits(deps, "api_key.create")) == audit_before


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({"name": "ab"}, "name", keys.NAME_INVALID),
        ({"name": "Nome Com Espaço"}, "name", keys.NAME_INVALID),
        ({"name": "---"}, "name", keys.NAME_INVALID),
        ({"name": "x" * 61}, "name", keys.NAME_INVALID),
        ({"name": ""}, "name", keys.NAME_INVALID),
        ({"role": "admin"}, "role", "Use um destes valores: 'leitura' ou 'total'."),
        ({"role": ""}, "role", "Use um destes valores: 'leitura' ou 'total'."),
        ({"expires_at": "2020-01-01T00:00:00Z"}, "expires_at", keys.EXPIRES_IN_THE_PAST),
        (
            {"expires_at": "2099-01-01T00:00:00"},
            "expires_at",
            "Informe o fuso horário, por exemplo 2027-01-31T12:00:00Z.",
        ),
        (
            {"expires_at": "amanhã"},
            "expires_at",
            "Informe a data e a hora em ISO 8601, por exemplo 2027-01-31T12:00:00Z.",
        ),
        ({"name": 7}, "name", "Informe um texto."),
        ({"nome": "errado"}, "nome", "Campo desconhecido."),
    ],
)
def test_a_refused_creation_says_which_field_and_creates_nothing(
    api: TestClient,
    deps: Deps,
    caller: keys.CreatedKey,
    body: dict[str, Any],
    field: str,
    message: str,
) -> None:
    before = key_count(deps)

    response = create(api, caller, **{"name": "valida", **body})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert response.json()["errors"] == [{"field": field, "message": message, "line": None}]
    assert key_count(deps) == before


def test_a_name_that_exists_is_refused_even_if_the_key_was_revoked(
    api: TestClient, deps: Deps, caller: keys.CreatedKey
) -> None:
    first = create(api, caller, "reaproveitada").json()
    assert revoke(api, caller, first["key_id"]).status_code == 200

    again = create(api, caller, "reaproveitada")

    assert again.status_code == 422
    assert again.json()["errors"] == [{"field": "name", "message": keys.NAME_TAKEN, "line": None}]
    assert key_count(deps) == 2


def test_a_missing_name_and_role_are_reported_together(
    api: TestClient, caller: keys.CreatedKey
) -> None:
    response = api.post(KEYS, json={"confirm": True}, headers=headers_of(caller))

    assert {e["field"] for e in response.json()["errors"]} == {"name", "role"}


def test_the_error_of_a_refused_creation_never_carries_the_value(
    api: TestClient, caller: keys.CreatedKey
) -> None:
    sentinel = secrets.token_hex(16)

    response = create(api, caller, name=sentinel + "!", role=sentinel)

    assert response.status_code == 422 and sentinel not in response.text


# --- revoke_api_key -----------------------------------------------------------------------------


def test_revoking_a_key_takes_it_out_of_service_at_once_and_shows_the_new_state(
    api: TestClient, deps: Deps, clock: FakeClock, caller: keys.CreatedKey
) -> None:
    victim = create(api, caller, "vitima").json()
    assert api.get(ME, headers=bearer(victim["key"])).status_code == 200

    response = revoke(api, caller, victim["key_id"])

    assert response.status_code == 200
    body = response.json()
    assert (body["name"], body["state"], body["revoked_by"]) == (
        "vitima",
        "revoked",
        "chave:quem-chama",
    )
    assert datetime.fromisoformat(body["revoked_at"]) == instant(clock)
    assert "key" not in body
    after = api.get(ME, headers=bearer(victim["key"]))
    assert after.status_code == 401 and after.json()["code"] == "key_revoked"
    (entry,) = audits(deps, "api_key.revoke")
    assert entry.actor == "chave:quem-chama" and entry.target_id == victim["key_id"]
    assert entry.details["name"] == "vitima"


def test_revoking_without_the_confirmation_revokes_nothing(
    api: TestClient, deps: Deps, caller: keys.CreatedKey
) -> None:
    victim = create(api, caller, "poupada").json()

    for body in ({}, {"confirm": False}, {"confirm": "true"}):
        response = api.post(
            f"{KEYS}/{victim['key_id']}/revoke", json=body, headers=headers_of(caller)
        )
        assert response.status_code == 422 and response.json()["code"] == "confirmation_required"

    assert row(deps, "poupada").revoked_at is None and not audits(deps, "api_key.revoke")
    assert api.get(ME, headers=bearer(victim["key"])).status_code == 200


def test_revoking_a_key_that_does_not_exist_is_a_404_in_the_error_format(
    api: TestClient, deps: Deps, caller: keys.CreatedKey
) -> None:
    for key_id in ("00000000", "nao-e-um-id", "A" * 80):
        response = revoke(api, caller, key_id)

        assert response.status_code == 404
        assert response.json() == {"code": "not_found", "detail": "Chave não encontrada."}
    assert not audits(deps, "api_key.revoke")


def test_revoking_twice_is_a_409_and_the_first_revocation_stands(
    api: TestClient, deps: Deps, clock: FakeClock, caller: keys.CreatedKey
) -> None:
    victim = create(api, caller, "revogada-duas-vezes").json()
    other = new_key(deps, clock, "total", name="outra-que-chama")
    assert revoke(api, caller, victim["key_id"]).status_code == 200
    first = row(deps, "revogada-duas-vezes")
    clock.advance(3600)

    again = revoke(api, other, victim["key_id"])

    assert again.status_code == 409 and again.json()["code"] == "state_conflict"
    assert "revogada-duas-vezes" in again.json()["detail"]
    assert "chave:quem-chama" in again.json()["detail"]
    after = row(deps, "revogada-duas-vezes")
    assert (after.revoked_at, after.revoked_by) == (first.revoked_at, first.revoked_by)
    assert len(audits(deps, "api_key.revoke")) == 1


def test_a_key_may_revoke_itself_and_the_next_call_is_refused(
    api: TestClient, deps: Deps, caller: keys.CreatedKey
) -> None:
    response = revoke(api, caller, caller.info.key_id)

    assert response.status_code == 200 and response.json()["state"] == "revoked"
    assert response.json()["revoked_by"] == "chave:quem-chama"
    assert api.get(ME, headers=headers_of(caller)).json()["code"] == "key_revoked"
    (entry,) = audits(deps, "api_key.revoke")
    assert entry.actor == "chave:quem-chama" and entry.target_id == caller.info.key_id


def test_revoking_a_key_leaves_the_keys_it_created_alone(
    api: TestClient, deps: Deps, caller: keys.CreatedKey
) -> None:
    """Por isso a auditoria (`api_key.create`) diz quais chaves uma chave criou."""
    child = create(api, caller, "filha-da-chamadora", role="total").json()

    assert revoke(api, caller, caller.info.key_id).status_code == 200

    assert api.get(ME, headers=bearer(child["key"])).status_code == 200
    assert row(deps, "filha-da-chamadora").revoked_at is None


def test_a_read_key_cannot_revoke(api: TestClient, deps: Deps, clock: FakeClock) -> None:
    reader = new_key(deps, clock, "leitura", name="so-olha")
    victim = new_key(deps, clock, "leitura", name="seguira")

    response = revoke(api, reader, victim.info.key_id)

    assert response.status_code == 403 and response.json()["code"] == "forbidden_role"
    assert row(deps, "seguira").revoked_at is None


# --- o contrato ---------------------------------------------------------------------------------


def contract(api: TestClient) -> dict[str, Any]:
    found: dict[str, Any] = api.app.state.admin_api.openapi()  # type: ignore[attr-defined]
    return found


def test_the_four_operations_are_declared_as_the_matrix_says(api: TestClient) -> None:
    declared = {
        found["operationId"]: (method.upper(), path, found["x-required-role"], found["tags"])
        for path, methods in contract(api)["paths"].items()
        for method, found in methods.items()
    }

    # as quatro operações das chaves são estas; as fases de recurso acrescentam as delas ao documento
    assert (
        declared.items()
        >= {
            "get_current_api_key": ("GET", "/me", "leitura", ["Chaves de API"]),
            "list_api_keys": ("GET", "/api-keys", "leitura", ["Chaves de API"]),
            "create_api_key": ("POST", "/api-keys", "total", ["Chaves de API"]),
            "revoke_api_key": ("POST", "/api-keys/{key_id}/revoke", "total", ["Chaves de API"]),
        }.items()
    )


def test_the_contract_version_is_1_n_0_with_n_the_number_of_operations(api: TestClient) -> None:
    document = contract(api)
    operations = sum(len(methods) for methods in document["paths"].values())

    assert operations >= 4  # as das chaves; as fases de recurso acrescentam as delas
    assert document["info"]["version"] == f"1.{operations}.0"


def test_the_marked_operations_declare_their_confirm_and_their_marks(api: TestClient) -> None:
    document = contract(api)
    schemas = document["components"]["schemas"]
    create_op = document["paths"]["/api-keys"]["post"]
    revoke_op = document["paths"]["/api-keys/{key_id}/revoke"]["post"]

    assert create_op["x-external-effect"] is True and "x-destructive" not in create_op
    assert revoke_op["x-destructive"] is True and "x-external-effect" not in revoke_op
    for name in ("CreateApiKeyRequest", "RevokeApiKeyRequest"):
        assert schemas[name]["properties"]["confirm"]["type"] == "boolean"
        assert schemas[name]["additionalProperties"] is False
    assert schemas["CreatedApiKey"]["properties"]["key"]["description"]
    assert "key" not in schemas["ApiKey"]["properties"]  # o segredo só na resposta da criação
    assert schemas["ApiKey"]["properties"]["role"]["enum"] == list(API_KEY_ROLES)


def test_every_operation_has_a_portuguese_text_and_named_models(api: TestClient) -> None:
    for methods in contract(api)["paths"].values():
        for found in methods.values():
            if found["tags"] != ["Chaves de API"]:
                continue  # as outras têm o contrato conferido por `test_admin_api_contract.py`
            assert found["summary"] and found["description"]
            success = next(r for code, r in found["responses"].items() if code[0] == "2")
            assert success["description"] != "Successful Response"
            assert success["content"]["application/json"]["schema"]["$ref"].startswith(
                "#/components/schemas/"
            )
            body = found.get("requestBody")
            if body:
                assert body["content"]["application/json"]["schema"]["$ref"]


def test_the_contract_has_no_example_that_looks_like_a_secret(api: TestClient) -> None:
    text = json.dumps(contract(api))

    assert not KEY_SHAPE.search(text)
    assert not re.search(r"[A-Za-z0-9_-]{40,}", text)


def test_the_list_of_roles_in_the_contract_is_the_one_of_the_model() -> None:
    from typing import get_args

    from app.admin.api.schemas.api_keys import Role

    assert get_args(Role) == API_KEY_ROLES


# --- paridade com a página ----------------------------------------------------------------------


def page_form(api: TestClient, path: str, **data: str) -> Any:
    return api.post(path, data={"csrf_token": csrf_token(api), **data}, headers=ORIGIN)


def details_without_the_name(entry: AuditLog) -> dict[str, Any]:
    return {key: value for key, value in entry.details.items() if key != "name"}


def test_creating_and_revoking_by_the_api_leave_the_same_state_and_audit_as_the_page(
    api: TestClient, deps: Deps, clock: FakeClock, caller: keys.CreatedKey
) -> None:
    assert login(api).status_code == 303
    expires = instant(clock) + timedelta(days=90)

    # a página: validade de 90 dias, como o formulário traz preenchida
    assert (
        page_form(api, PAGE, name="pela-pagina", role="total", validity_days="90").status_code
        == 200
    )
    # a API: o mesmo nome (menos o sufixo), papel e validade
    assert (
        create(api, caller, "pela-api", role="total", expires_at=expires.isoformat()).status_code
        == 201
    )
    by_page, by_api = row(deps, "pela-pagina"), row(deps, "pela-api")

    assert (by_page.role, by_page.expires_at, by_page.created_at) == (
        by_api.role,
        by_api.expires_at,
        by_api.created_at,
    )
    assert (by_page.revoked_at, by_api.revoked_at) == (None, None)
    assert (by_page.created_by, by_api.created_by) == ("operadora", "chave:quem-chama")
    created = {e.details["name"]: e for e in audits(deps, "api_key.create")}
    assert details_without_the_name(created["pela-pagina"]) == details_without_the_name(
        created["pela-api"]
    )
    assert (created["pela-pagina"].target_type, created["pela-api"].target_type) == ("api_key",) * 2
    assert (created["pela-pagina"].actor, created["pela-api"].actor) == (
        "operadora",
        "chave:quem-chama",
    )

    clock.advance(600)
    page_form(api, f"{PAGE}/{by_page.key_id}/revoke", confirm="on")
    assert revoke(api, caller, by_api.key_id).status_code == 200
    by_page, by_api = row(deps, "pela-pagina"), row(deps, "pela-api")

    assert by_page.revoked_at == by_api.revoked_at == instant(clock)
    assert (by_page.revoked_by, by_api.revoked_by) == ("operadora", "chave:quem-chama")
    revoked = {e.details["name"]: e for e in audits(deps, "api_key.revoke")}
    assert details_without_the_name(revoked["pela-pagina"]) == details_without_the_name(
        revoked["pela-api"]
    )
    assert (revoked["pela-pagina"].actor, revoked["pela-api"].actor) == (
        "operadora",
        "chave:quem-chama",
    )


def test_the_listing_of_the_api_has_the_same_keys_as_the_service_the_page_uses(
    api: TestClient, deps: Deps, clock: FakeClock, caller: keys.CreatedKey
) -> None:
    for number in range(3):
        clock.advance(1)
        new_key(deps, clock, name=f"chave-{number}")
    with deps.sessions() as session:
        from_service = [info.name for info in keys.listing(session)]

    listed = api.get(KEYS, headers=headers_of(caller)).json()["items"]

    assert [item["name"] for item in listed] == from_service
