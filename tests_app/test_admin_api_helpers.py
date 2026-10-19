"""Os auxiliares dos testes da API de admin: `api_client(role)` e `assert_same_effect`.

Eles são o que cada fase de recurso usa: `api_client` entrega o cliente de uma chave sorteada, e
`assert_same_effect` é a prova de paridade da ADR 0015 (a mesma ação pela página e pela API, a partir
do mesmo estado do banco, com o mesmo estado final e a mesma auditoria, menos o autor). A prova aqui
é feita com as chaves de API, que já têm a página e a API.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from admin_api_support import (
    API,
    ORIGIN,
    PAGE_ACTOR,
    ApiClient,
    assert_same_effect,
    new_key,
    restore,
    snapshot,
    url_of,
)
from fastapi.testclient import TestClient
from test_admin import FakeClock, csrf_token, login

from app.admin.api import keys
from app.admin.auth import instant
from app.deps import Deps, get_deps
from app.models import ApiKey, AuditLog

ME = f"{API}/me"
KEYS = f"{API}/api-keys"
# A chamada da API grava o último uso da chave que chama; a página, não: é o que a comparação tira.
CALLER_USE = {"api_keys": {"last_used_at"}}
# O que uma chave nova sorteia: o id público, o hash do segredo e o alvo da auditoria, que é o id.
RANDOM_KEY = {"api_keys": {"key_id", "secret_hash", "last_used_at"}, "audit_log": {"target_id"}}

ApiClientFactory = Callable[..., ApiClient]


# --- api_client ---------------------------------------------------------------------------------


def test_api_client_sends_the_key_of_the_role_without_being_told(
    api_client: ApiClientFactory,
) -> None:
    reader = api_client("leitura")

    me = reader.get(ME)

    assert me.status_code == 200
    assert me.json()["role"] == "leitura" and me.json()["name"] == reader.key.info.name
    assert reader.role == "leitura" and reader.actor == f"chave:{reader.key.info.name}"


def test_url_of_gives_the_address_of_an_operation_by_its_id_and_fills_the_path() -> None:
    assert url_of("get_current_api_key") == f"{API}/me"
    assert url_of("list_api_keys") == url_of("create_api_key") == f"{API}/api-keys"
    assert url_of("revoke_api_key", key_id="a1b2c3d4") == f"{API}/api-keys/a1b2c3d4/revoke"
    assert url_of("get_job", public_id="abc") == f"{API}/jobs/abc"


def test_url_of_an_id_that_is_not_in_the_api_is_an_error_not_a_404() -> None:
    with pytest.raises(KeyError, match="nao_existe"):
        url_of("nao_existe")


def test_url_of_without_the_value_of_a_path_parameter_is_an_error() -> None:
    with pytest.raises(KeyError, match="key_id"):
        url_of("revoke_api_key")


def test_api_client_without_a_role_is_a_total_key(api_client: ApiClientFactory) -> None:
    client = api_client()

    created = client.post(KEYS, json={"name": "outra-chave", "role": "leitura", "confirm": True})

    assert client.role == "total" and created.status_code == 201


def test_a_read_client_is_refused_where_the_role_is_total(api_client: ApiClientFactory) -> None:
    refused = api_client("leitura").post(
        KEYS, json={"name": "outra-chave", "role": "leitura", "confirm": True}
    )

    assert refused.status_code == 403 and refused.json()["code"] == "forbidden_role"


def test_every_client_gets_a_new_random_name_and_secret(api_client: ApiClientFactory) -> None:
    first, second = api_client(), api_client()

    assert first.key.key != second.key.key
    assert first.key.info.name != second.key.info.name
    assert first.get(ME).status_code == second.get(ME).status_code == 200


def test_the_name_the_clock_and_the_environment_can_be_chosen(
    api_client: ApiClientFactory,
) -> None:
    clock = FakeClock()
    client = api_client("total", name="a-minha-chave", clock=clock, API_RATE_LIMIT_PER_MINUTE="5")

    assert client.get(ME).json()["name"] == "a-minha-chave"
    assert client.clock is clock
    codes = [client.get(ME).status_code for _ in range(8)]
    assert codes[-1] == 429  # o balde de 5 chamadas por minuto, do ambiente do teste


def test_the_client_is_the_whole_app_so_the_page_is_there_too(
    api_client: ApiClientFactory,
) -> None:
    client = api_client()

    assert client.get("/admin/login").status_code == 200
    assert login(client).status_code == 303
    assert client.get("/admin/api-keys").status_code == 200


# --- assert_same_effect -------------------------------------------------------------------------


def row(name: str) -> ApiKey:
    with get_deps().sessions() as session:
        return session.scalars(sa.select(ApiKey).where(ApiKey.name == name)).one()


def audits(action: str) -> list[AuditLog]:
    with get_deps().sessions() as session:
        return list(session.scalars(sa.select(AuditLog).where(AuditLog.action == action)))


def page_revoke(client: TestClient, key_id: str) -> Callable[[], Any]:
    def call() -> Any:
        assert login(client).status_code == 303
        return client.post(
            f"/admin/api-keys/{key_id}/revoke",
            data={"csrf_token": csrf_token(client), "confirm": "on"},
            headers=ORIGIN,
        )

    return call


def api_revoke(client: TestClient, key_id: str) -> Callable[[], Any]:
    return lambda: client.post(f"{KEYS}/{key_id}/revoke", json={"confirm": True})


@pytest.fixture
def target(api_client: ApiClientFactory) -> tuple[ApiClient, keys.CreatedKey]:
    """A chave que chama e a que será revogada."""
    client = api_client("total", name="quem-chama")
    return client, new_key(get_deps(), client.clock, "leitura", name="alvo")


def test_the_same_action_by_the_page_and_by_the_api_has_the_same_effect_but_the_author(
    target: tuple[ApiClient, keys.CreatedKey],
) -> None:
    client, victim = target

    page, api = assert_same_effect(
        page_revoke(client, victim.info.key_id),
        api_revoke(client, victim.info.key_id),
        api_actor=client.actor,
        ignore=CALLER_USE,
    )

    assert (page.status_code, api.status_code) == (303, 200)  # o que cada chamada devolveu
    assert row("alvo").revoked_by == "chave:quem-chama"  # o banco ficou como a API o deixou
    assert [entry.actor for entry in audits("api_key.revoke")] == ["chave:quem-chama"]


def test_an_old_row_written_by_the_page_author_is_not_a_false_difference(
    target: tuple[ApiClient, keys.CreatedKey],
) -> None:
    """O autor só é normalizado no que a ação cria ou muda: uma linha que o mundo do teste gravou com
    o nome do autor da página (`operadora`) e que a ação não toca é a mesma antes e depois, e não
    pode virar `<autor>` de um lado só."""
    client, victim = target
    with get_deps().sessions.begin() as session:
        keys.create(
            session,
            name="antiga",
            role="leitura",
            actor=PAGE_ACTOR,  # o nome do autor da página, num dado que a ação não toca
            expires_at=None,
            now=instant(client.clock),
        )

    assert_same_effect(
        page_revoke(client, victim.info.key_id),
        api_revoke(client, victim.info.key_id),
        api_actor=client.actor,
        ignore=CALLER_USE,
    )

    assert row("antiga").created_by == PAGE_ACTOR  # e segue como estava
    assert row("alvo").revoked_by == client.actor


def test_an_old_cell_of_a_changed_row_keeps_the_page_author_it_was_written_with(
    api_client: ApiClientFactory,
) -> None:
    """O autor é normalizado por célula, e não por linha: a chave que o mundo do teste criou como
    `operadora` e que a ação revoga muda de linha (`revoked_at`, `revoked_by`), mas o `created_by`
    dela é o mesmo de antes, e não pode virar `<autor>` só no lado da página."""
    client = api_client("total", name="quem-chama")
    with get_deps().sessions.begin() as session:
        old = keys.create(
            session,
            name="criada-pela-operadora",
            role="leitura",
            actor=PAGE_ACTOR,
            expires_at=None,
            now=instant(client.clock),
        )

    assert_same_effect(
        page_revoke(client, old.info.key_id),
        api_revoke(client, old.info.key_id),
        api_actor=client.actor,
        ignore=CALLER_USE,
    )

    revoked = row("criada-pela-operadora")
    assert (revoked.created_by, revoked.revoked_by) == (PAGE_ACTOR, client.actor)


def test_a_row_the_action_writes_with_the_wrong_author_is_still_a_difference(
    target: tuple[ApiClient, keys.CreatedKey],
) -> None:
    """Normalizar só o que é novo não afrouxa a prova: a linha que a ação grava com o nome do autor
    da página, onde a API devia gravar o da chave, continua aparecendo."""
    client, victim = target

    def revoked_as_the_page_author() -> Any:
        response = api_revoke(client, victim.info.key_id)()
        with get_deps().sessions.begin() as session:
            session.execute(
                sa.update(ApiKey).where(ApiKey.name == "alvo").values(revoked_by=PAGE_ACTOR)
            )
        return response

    with pytest.raises(AssertionError, match="revoked_by"):
        assert_same_effect(
            page_revoke(client, victim.info.key_id),
            revoked_as_the_page_author,
            api_actor=client.actor,
            ignore=CALLER_USE,
        )


def test_a_creation_with_a_random_secret_is_compared_without_what_is_random(
    api_client: ApiClientFactory,
) -> None:
    client = api_client("total", name="quem-chama")

    def by_page() -> Any:
        assert login(client).status_code == 303
        return client.post(
            "/admin/api-keys",
            data={
                "csrf_token": csrf_token(client),
                "name": "paridade",
                "role": "leitura",
                "validity_days": "90",
            },
            headers=ORIGIN,
        )

    def by_api() -> Any:
        expires = datetime.fromtimestamp(client.clock() + 90 * 86_400, UTC)
        return client.post(
            KEYS,
            json={
                "name": "paridade",
                "role": "leitura",
                "confirm": True,
                "expires_at": expires.isoformat(),
            },
        )

    page, api = assert_same_effect(by_page, by_api, api_actor=client.actor, ignore=RANDOM_KEY)

    assert page.status_code == 200 and api.status_code == 201
    assert row("paridade").created_by == "chave:quem-chama"


def test_the_api_call_starts_from_the_state_the_page_call_started_from(
    target: tuple[ApiClient, keys.CreatedKey],
) -> None:
    """A página já revogou a chave quando a API começa, se o banco não voltasse: o segundo
    `revoke` seria um `409 state_conflict`, e a prova não provaria nada."""
    client, victim = target
    seen: dict[str, Any] = {}

    def by_api() -> Any:
        seen["revoked_at_the_start"] = row("alvo").revoked_at
        return api_revoke(client, victim.info.key_id)()

    _, api = assert_same_effect(
        page_revoke(client, victim.info.key_id),
        by_api,
        api_actor=client.actor,
        ignore=CALLER_USE,
    )

    assert seen["revoked_at_the_start"] is None
    assert api.status_code == 200


def test_it_fails_when_the_api_does_not_leave_the_state_the_page_leaves(
    target: tuple[ApiClient, keys.CreatedKey],
) -> None:
    client, victim = target

    with pytest.raises(AssertionError, match=r"tabela api_keys") as failure:
        assert_same_effect(
            page_revoke(client, victim.info.key_id),
            lambda: client.get(ME),  # a "API" não revoga nada
            api_actor=client.actor,
            ignore=CALLER_USE,
        )

    assert "tabela audit_log" in str(failure.value)  # nem a auditoria
    assert "pela página" in str(failure.value) and "pela API" in str(failure.value)


def test_it_fails_when_the_audit_differs_even_if_the_rows_look_alike(
    api_client: ApiClientFactory,
) -> None:
    client = api_client("total", name="quem-chama")

    def by_page() -> Any:
        assert login(client).status_code == 303
        return client.post(
            "/admin/api-keys",
            data={"csrf_token": csrf_token(client), "name": "diferente", "role": "total"},
            headers=ORIGIN,
        )

    def by_api() -> Any:  # outro papel: a linha e a auditoria dizem outra coisa
        return client.post(KEYS, json={"name": "diferente", "role": "leitura", "confirm": True})

    with pytest.raises(AssertionError) as failure:
        assert_same_effect(by_page, by_api, api_actor=client.actor, ignore=RANDOM_KEY)

    assert "tabela audit_log" in str(failure.value) and "tabela api_keys" in str(failure.value)


def test_the_last_use_of_the_calling_key_is_a_difference_unless_it_is_ignored(
    target: tuple[ApiClient, keys.CreatedKey],
) -> None:
    client, victim = target

    with pytest.raises(AssertionError, match="last_used_at"):
        assert_same_effect(
            page_revoke(client, victim.info.key_id),
            api_revoke(client, victim.info.key_id),
            api_actor=client.actor,
        )


def test_a_wildcard_ignores_a_column_in_every_table(
    target: tuple[ApiClient, keys.CreatedKey],
) -> None:
    client, victim = target

    assert_same_effect(
        page_revoke(client, victim.info.key_id),
        api_revoke(client, victim.info.key_id),
        api_actor=client.actor,
        ignore={"*": {"last_used_at"}},
    )


def test_the_author_is_the_only_thing_that_may_differ(
    target: tuple[ApiClient, keys.CreatedKey],
) -> None:
    """Se a página e a API disserem o mesmo nome de autor onde não deviam, ou o autor da API não for
    o declarado, a normalização não esconde: o autor errado aparece como diferença."""
    client, victim = target

    with pytest.raises(AssertionError, match="revoked_by"):
        assert_same_effect(
            page_revoke(client, victim.info.key_id),
            api_revoke(client, victim.info.key_id),
            api_actor="chave:outra",  # não é quem chama
            ignore=CALLER_USE,
        )


def test_two_calls_that_change_nothing_fail_by_default(
    target: tuple[ApiClient, keys.CreatedKey],
) -> None:
    """Sem mudança nenhuma, páginas e APIs são "iguais" por não terem feito nada: a prova falha."""
    client, _ = target

    with pytest.raises(AssertionError, match="nenhuma das duas chamadas mudou o estado"):
        assert_same_effect(
            lambda: client.get(ME),
            lambda: client.get(ME),
            api_actor=client.actor,
            ignore=CALLER_USE,
        )


def test_a_refusal_that_changes_nothing_passes_when_that_is_what_the_test_expects(
    target: tuple[ApiClient, keys.CreatedKey],
) -> None:
    client, victim = target

    def without_confirm() -> Any:
        return client.post(f"{KEYS}/{victim.info.key_id}/revoke", json={})

    _, api = assert_same_effect(
        lambda: client.get("/admin/login"),
        without_confirm,
        api_actor=client.actor,
        ignore=CALLER_USE,
        expect_no_change=True,
    )

    assert api.status_code == 422 and row("alvo").revoked_at is None


def test_expect_no_change_fails_when_a_call_changed_the_state(
    target: tuple[ApiClient, keys.CreatedKey],
) -> None:
    client, victim = target

    with pytest.raises(AssertionError, match="expect_no_change=True"):
        assert_same_effect(
            page_revoke(client, victim.info.key_id),
            api_revoke(client, victim.info.key_id),
            api_actor=client.actor,
            ignore=CALLER_USE,
            expect_no_change=True,
        )


# --- a fotografia e a volta do banco ------------------------------------------------------------


def test_restore_puts_every_row_back_where_it_was(deps: Deps) -> None:
    new_key(deps, FakeClock(), "total", name="guardada")
    before = snapshot()
    with deps.sessions.begin() as session:
        session.execute(sa.delete(ApiKey))
        session.execute(sa.delete(AuditLog))

    assert snapshot()["api_keys"] == []
    restore(before)

    assert snapshot() == before
    assert {name for name, rows in before.items() if rows} >= {"api_keys", "forms", "form_versions"}
