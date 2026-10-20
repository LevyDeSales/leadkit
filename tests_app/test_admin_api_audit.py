"""A leitura da auditoria (`/admin/api/v1`): `list_audit_log`, pela costura HTTP.

A operação é nova (o admin só mostra a auditoria de um pedido, na linha do tempo) e só lê: nada é gravado
nem auditado (decisão 2 da revisão da ADR 0015). Ela lê pela consulta `app.admin.queries.audit`, que
devolve cada entrada com os `details` das chaves que se sabe próprias para a chave `leitura`. O que estes
testes prendem:

- os filtros (alvo, autor e ação, exatos) e a paginação por cursor `(at, id)`, que não pula nem repete
  linha, nem quando várias entradas têm o mesmo instante;
- a auditoria, como os serviços a gravam hoje, não traz o segredo, o hash de uma chave, o caminho da URL
  de um destino, o motivo livre de um reprocessamento (`reason`, que pode citar a lead) nem o texto de erro
  de fora do teste de uma integração (`error`); e o filtro é pelo **nome** da chave, e não pelo conteúdo
  (o último teste da seção mostra o limite);
- uma ação feita pela API aparece com o autor `chave:<nome>`;
- a recusa de uma chave que falta e de um filtro, limite ou cursor que não vale (`422` no campo).

As chaves e os segredos destes testes são sorteados na execução.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_matrix import OPENAPI_FILE, ROOT, operations_of
from admin_api_matrix import read as read_matrix
from admin_api_support import ApiClient, make_api, url_of
from conftest import FakeBaserow
from jobs_world import PEOPLE, JobsWorld, build_jobs_world
from test_admin import FakeClock
from test_integrations import SLUG, connect_form

from app.admin.api.common import encode_cursor
from app.admin.queries.audit import PUBLIC_DETAIL_KEYS
from app.deps import Deps
from app.models import ApiKey, AuditLog

Client = Callable[..., ApiClient]
SENTINEL = "ZZ_SENT_" + secrets.token_hex(8).upper()
PATH_SECRET = "caminho-" + secrets.token_hex(8)
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
ENTRY_FIELDS = {"id", "at", "actor", "action", "target_type", "target_id", "details"}


def entry(
    *,
    at: datetime = T0,
    actor: str = "operadora",
    action: str = "form.publish",
    target_type: str | None = "form",
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
    id: uuid.UUID | None = None,
) -> AuditLog:
    return AuditLog(
        id=id or uuid.uuid4(),
        at=at,
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
    )


def seed(deps: Deps, *rows: AuditLog) -> None:
    with deps.sessions.begin() as session:
        session.add_all(rows)


def read_log(client: httpx.Client, **params: Any) -> httpx.Response:
    """Lê a lista. Cada leitura enche o balde de uso da chave (o relógio do app é o do teste): uma
    paginação inteira passa da rajada de 30 chamadas, e o limite de uso tem os testes dele."""
    if isinstance(client, ApiClient):
        client.clock.advance(60)
    return client.get(url_of("list_audit_log"), params=params)


MAX_PAGES = 100


def pages(client: httpx.Client, **params: Any) -> list[httpx.Response]:
    """Todas as páginas, seguindo o `next_cursor` até o fim. Um cursor que não avança (a mesma entrada
    de novo, para sempre) reprova o teste em vez de travá-lo."""
    found = [read_log(client, **params)]
    while found[-1].status_code == 200 and found[-1].json()["next_cursor"]:
        assert len(found) < MAX_PAGES, "o cursor não avança: a lista não acaba"
        found.append(read_log(client, **{**params, "cursor": found[-1].json()["next_cursor"]}))
    return found


def ids_of(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def walk(client: httpx.Client, **params: Any) -> list[str]:
    """Os `id` de todas as páginas, na ordem em que vieram."""
    return [found for page in pages(client, **params) for found in ids_of(page)]


def newest_first(rows: list[AuditLog]) -> list[str]:
    """A ordem que a API promete (do instante mais novo para o mais antigo; o `id` maior primeiro entre
    os do mesmo instante), calculada aqui, e não pela consulta."""
    return [str(row.id) for row in sorted(rows, key=lambda row: (row.at, row.id), reverse=True)]


def audit_count(deps: Deps) -> int:
    with deps.sessions() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(AuditLog)) or 0


# --- a chave e o papel --------------------------------------------------------------------------


def test_list_audit_log_needs_a_key(deps: Deps) -> None:
    response = make_api(deps, FakeClock()).get(url_of("list_audit_log"))

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"
    assert response.headers["www-authenticate"] == "Bearer"


def test_list_audit_log_is_a_read_so_a_read_key_is_enough_and_it_writes_nothing(
    deps: Deps, api_client: Client
) -> None:
    seed(deps, entry())
    for role in ("leitura", "total"):
        client = api_client(role)
        before = audit_count(deps)  # a chave nova, que o teste cria, já está contada

        response = read_log(client)

        assert response.status_code == 200, role
        assert audit_count(deps) == before  # ler a auditoria não grava nada nela


# --- a lista ------------------------------------------------------------------------------------


def test_list_audit_log_answers_an_envelope_of_the_newest_entries_first(
    deps: Deps, api_client: Client
) -> None:
    older = entry(at=T0, actor="operadora", action="form.activate", target_id="f-1")
    newer = entry(
        at=T0 + HOUR,
        action="form.publish",
        target_id="f-2",
        details={
            "form_slug": "demo",
            "version": 3,
            "fingerprint": "ab12cd34",
            "contact_in_prompt": ["name", "email"],
            "images": {"logo": "e3b0c44298fc"},
        },
    )
    seed(deps, older, newer)

    response = read_log(api_client("leitura"), actor="operadora")

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"items", "next_cursor"} and body["next_cursor"] is None
    assert [item["id"] for item in body["items"]] == [str(newer.id), str(older.id)]
    first = body["items"][0]
    assert set(first) == ENTRY_FIELDS
    assert (first["actor"], first["action"]) == ("operadora", "form.publish")
    assert (first["target_type"], first["target_id"]) == ("form", "f-2")
    assert first["at"] == "2026-10-01T13:00:00Z"
    assert datetime.fromisoformat(first["at"]).utcoffset() == timedelta(0)
    assert first["details"] == newer.details


def test_list_audit_log_says_null_for_an_entry_without_a_target(
    deps: Deps, api_client: Client
) -> None:
    seed(deps, entry(actor="retention", action="retention.purge", target_type=None))

    (item,) = read_log(api_client("leitura"), actor="retention").json()["items"]

    assert item["target_type"] is None and item["target_id"] is None and item["details"] == {}


def test_list_audit_log_with_nothing_that_matches_is_one_empty_page(
    deps: Deps, api_client: Client
) -> None:
    body = read_log(api_client("leitura"), actor="ninguem").json()

    assert body == {"items": [], "next_cursor": None}


# --- os filtros ---------------------------------------------------------------------------------


def world_of_entries(deps: Deps) -> dict[str, AuditLog]:
    rows = {
        "publish": entry(
            at=T0, actor="operadora", action="form.publish", target_type="form", target_id="f-1"
        ),
        "other_form": entry(
            at=T0 + 1 * HOUR,
            actor="chave:painel",
            action="form.publish",
            target_type="form",
            target_id="f-2",
        ),
        "reprocess_by_key": entry(
            at=T0 + 2 * HOUR,
            actor="chave:painel",
            action="job.reprocess",
            target_type="job",
            target_id="j-1",
        ),
        "reprocess_by_page": entry(
            at=T0 + 3 * HOUR,
            actor="operadora",
            action="job.reprocess",
            target_type="job",
            target_id="j-1",
        ),
        "purge": entry(
            at=T0 + 4 * HOUR,
            actor="retention",
            action="retention.purge",
            target_type=None,
            target_id=None,
        ),
    }
    seed(deps, *rows.values())
    return rows


def test_list_audit_log_filters_by_target_actor_and_action_and_the_filters_add_up(
    deps: Deps, api_client: Client
) -> None:
    rows = world_of_entries(deps)
    client = api_client("leitura")

    def ids(**params: str) -> list[str]:
        # Só as entradas deste teste: a chave do cliente também deixou a dela na auditoria.
        mine = {str(row.id): name for name, row in rows.items()}
        return [mine[found] for found in walk(client, **params) if found in mine]

    assert ids(target_type="job") == ["reprocess_by_page", "reprocess_by_key"]
    assert ids(target_id="j-1") == ["reprocess_by_page", "reprocess_by_key"]
    assert ids(target_type="form", target_id="f-2") == ["other_form"]
    assert ids(target_type="form", target_id="j-1") == []  # os filtros se somam (E)
    assert ids(actor="chave:painel") == ["reprocess_by_key", "other_form"]
    assert ids(action="form.publish") == ["other_form", "publish"]
    assert ids(actor="chave:painel", action="form.publish") == ["other_form"]
    assert ids(actor="operadora", target_type="job") == ["reprocess_by_page"]
    assert ids(action="retention.purge") == ["purge"]
    assert ids(actor="ninguem") == []
    assert sorted(ids()) == sorted(rows)  # sem filtro, todas (e mais a da chave do cliente)


def test_list_audit_log_filters_are_exact_and_no_text_is_a_pattern(
    deps: Deps, api_client: Client
) -> None:
    world_of_entries(deps)
    client = api_client("leitura")

    # Maiúsculas, prefixos e os curingas do LIKE (`%`, `_`) não casam: o filtro é igualdade.
    for params in (
        {"actor": "OPERADORA"},
        {"actor": "opera"},
        {"actor": "%"},
        {"actor": "chave:%"},
        {"action": "form.%"},
        {"action": "form"},
        {"action": "form_publish"},
        {"target_type": "%"},
        {"target_type": "FORM"},
        {"target_id": "j-%"},
        {"target_id": "j_1"},
    ):
        response = read_log(client, **params)
        assert response.status_code == 200 and response.json()["items"] == [], params


def test_list_audit_log_an_entry_without_a_target_matches_no_target_filter(
    deps: Deps, api_client: Client
) -> None:
    seed(deps, entry(actor="retention", action="retention.purge", target_type=None))

    for params in ({"target_type": "job"}, {"target_id": "x"}, {"target_type": "null"}):
        assert read_log(api_client("leitura"), actor="retention", **params).json()["items"] == []


# --- a paginação por cursor ---------------------------------------------------------------------


def equal_instants(deps: Deps) -> list[AuditLog]:
    """Nove entradas em três instantes: quatro no primeiro, uma no segundo e quatro no terceiro. Os
    de um mesmo instante só se distinguem pelo `id` (de propósito, inseridos fora de ordem)."""
    rows = [
        *(entry(at=T0, actor="semente", id=uuid.UUID(int=n)) for n in (3, 1, 4, 2)),
        entry(at=T0 + HOUR, actor="semente", id=uuid.UUID(int=9)),
        *(entry(at=T0 + 2 * HOUR, actor="semente", id=uuid.UUID(int=n)) for n in (7, 5, 8, 6)),
    ]
    seed(deps, *rows)
    return rows


def test_list_audit_log_paginates_by_cursor_without_skipping_or_repeating_equal_instants(
    deps: Deps, api_client: Client
) -> None:
    rows = equal_instants(deps)
    client = api_client("leitura")

    for limit in (1, 2, 3, 4, 5, 8, 9, 10, 200):
        walked = walk(client, actor="semente", limit=limit)
        assert walked == newest_first(rows), limit  # nenhuma pulada, nenhuma repetida, na ordem
        found = pages(client, actor="semente", limit=limit)
        # A última página é a única sem cursor, e nenhuma é vazia (nem quando o total é múltiplo).
        assert [bool(page.json()["next_cursor"]) for page in found] == [True] * (len(found) - 1) + [
            False
        ], limit
        assert all(page.json()["items"] for page in found), limit


def test_list_audit_log_the_cursor_alone_decides_the_next_page_across_equal_instants(
    deps: Deps, api_client: Client
) -> None:
    rows = equal_instants(deps)
    client = api_client("leitura")
    order = newest_first(rows)

    # A página termina no meio do grupo de quatro do instante mais novo: a seguinte começa no
    # próximo `id` do mesmo instante, e não no instante seguinte.
    first = read_log(client, actor="semente", limit=2)
    second = read_log(client, actor="semente", limit=2, cursor=first.json()["next_cursor"])

    assert ids_of(first) == order[:2] and ids_of(second) == order[2:4]
    assert {item["at"] for item in first.json()["items"] + second.json()["items"]} == {
        "2026-10-01T14:00:00Z"
    }


def test_list_audit_log_an_entry_that_arrives_during_the_read_neither_skips_nor_repeats(
    deps: Deps, api_client: Client
) -> None:
    rows = equal_instants(deps)
    client = api_client("leitura")

    first = read_log(client, actor="semente", limit=3)
    seed(deps, entry(at=T0 + 5 * HOUR, actor="semente"))  # chega no meio da leitura, é a mais nova
    rest = walk(client, actor="semente", limit=3, cursor=first.json()["next_cursor"])

    assert [*ids_of(first), *rest] == newest_first(rows)  # a nova ficou de fora das seguintes
    assert ids_of(read_log(client, actor="semente", limit=1)) != [
        newest_first(rows)[0]
    ]  # e a lista a traz


def test_list_audit_log_paginates_the_filtered_list_with_the_same_cursor(
    deps: Deps, api_client: Client
) -> None:
    rows = world_of_entries(deps)
    client = api_client("leitura")

    walked = walk(client, action="job.reprocess", limit=1)

    assert walked == [str(rows["reprocess_by_page"].id), str(rows["reprocess_by_key"].id)]


def test_list_audit_log_limit_goes_from_1_to_200_and_a_bad_one_names_the_field(
    deps: Deps, api_client: Client
) -> None:
    equal_instants(deps)
    client = api_client("leitura")

    assert len(ids_of(read_log(client, actor="semente", limit=1))) == 1
    assert len(ids_of(read_log(client, actor="semente", limit=200))) == 9
    for limit in ("0", "201", "-1", "muitos"):
        refused = read_log(client, limit=limit)
        assert refused.status_code == 422 and refused.json()["code"] == "validation_failed"
        assert [e["field"] for e in refused.json()["errors"]] == ["limit"]


def test_list_audit_log_a_malformed_cursor_is_a_422_on_the_cursor(
    deps: Deps, api_client: Client
) -> None:
    equal_instants(deps)
    client = api_client("leitura")
    not_a_position = "eyJhZnRlciI6ICJ4In0"  # um envelope válido, mas a posição não é `instante|id`
    shapes = (
        "lixo",
        not_a_position,
        "A" * 400,
        encode_cursor("sem-separador"),
        encode_cursor(f"nao-e-uma-data|{uuid.uuid4()}"),
        encode_cursor("2026-10-01T12:00:00+00:00|nao-e-um-uuid"),
        encode_cursor(f"2026-10-01T12:00:00|{uuid.uuid4()}"),  # o instante sem fuso
        encode_cursor(f"2026-10-01|{uuid.uuid4()}"),
    )

    for cursor in shapes:
        refused = read_log(client, cursor=cursor)
        assert refused.status_code == 422, cursor
        assert [e["field"] for e in refused.json()["errors"]] == ["cursor"]


def test_list_audit_log_a_well_formed_cursor_nobody_gave_is_only_a_position(
    deps: Deps, api_client: Client
) -> None:
    """O cursor não é assinado: uma posição bem formada, ainda que a API nunca a tenha entregue, só começa
    a leitura dali (as entradas mais antigas que ela)."""
    rows = equal_instants(deps)
    client = api_client("leitura")
    middle = T0 + HOUR
    cursor = encode_cursor(
        f"{middle.isoformat()}|{uuid.UUID(int=9)}"
    )  # a posição da entrada do meio

    found = walk(client, actor="semente", cursor=cursor)

    assert found == newest_first([row for row in rows if row.at < middle])


# --- os filtros que não valem -------------------------------------------------------------------


@pytest.mark.parametrize("name", ["target_type", "target_id", "actor", "action"])
def test_list_audit_log_a_bad_filter_is_a_422_on_that_field_and_echoes_nothing(
    api_client: Client, name: str
) -> None:
    client = api_client("leitura")

    for bad in ("", SENTINEL + "x" * 300, f"a\x00{SENTINEL}", f"a\n{SENTINEL}"):
        refused = read_log(client, **{name: bad})

        assert refused.status_code == 422, repr(bad)
        assert refused.json()["code"] == "validation_failed"
        assert [e["field"] for e in refused.json()["errors"]] == [name]
        assert SENTINEL not in refused.text and SENTINEL not in str(refused.headers)


def test_list_audit_log_names_every_bad_filter_at_once(api_client: Client) -> None:
    refused = read_log(api_client("leitura"), actor="", action="", limit="0")

    assert refused.status_code == 422
    assert sorted(e["field"] for e in refused.json()["errors"]) == ["action", "actor", "limit"]


def test_list_audit_log_echoes_none_of_a_free_text_that_is_otherwise_valid(
    deps: Deps, api_client: Client
) -> None:
    """Os filtros são texto livre. O eco geral do contrato para no `422` do Pydantic antes de a consulta
    rodar; aqui os outros campos são válidos e só o filtro leva o sentinela, para ele chegar à busca."""
    seed(deps, entry())
    for role in ("leitura", "total"):
        for name in ("target_type", "target_id", "actor", "action"):
            response = read_log(api_client(role), **{name: SENTINEL}, limit=5)

            assert response.status_code == 200 and response.json()["items"] == []
            assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


# --- o que uma ação pela API deixa --------------------------------------------------------------


def test_an_action_done_by_the_api_shows_up_with_the_key_as_the_actor(
    deps: Deps, api_client: Client
) -> None:
    writer = api_client("total")
    created = writer.post(
        url_of("create_api_key"),
        json={"name": "painel-" + secrets.token_hex(3), "role": "leitura", "confirm": True},
    )
    assert created.status_code == 201, created.text
    reader = api_client("leitura")

    found = read_log(reader, actor=writer.actor, action="api_key.create").json()["items"]

    assert writer.actor.startswith("chave:") and writer.actor == f"chave:{writer.key.info.name}"
    (item,) = found
    assert item["actor"] == writer.actor
    assert (item["target_type"], item["target_id"]) == ("api_key", created.json()["key_id"])
    # O nome, o papel e a validade da chave nova, e nada mais: nem o segredo nem o hash.
    assert item["details"] == {
        "name": created.json()["name"],
        "role": "leitura",
        "expires_at": None,
    }


def test_an_action_done_by_the_page_shows_up_with_the_user_and_the_target_filter_finds_it(
    deps: Deps, api_client: Client
) -> None:
    seed(deps, entry(actor="operadora", action="form.deactivate", target_id="f-9"))

    found = read_log(api_client("leitura"), target_type="form", target_id="f-9").json()["items"]

    assert [(i["actor"], i["action"]) for i in found] == [("operadora", "form.deactivate")]


# --- nunca o segredo, o hash nem a lead ---------------------------------------------------------


def acts(deps: Deps, world: JobsWorld, writer: ApiClient) -> dict[str, Any]:
    """O que uma chave `total` faz e que deixa na auditoria algo que poderia vazar: cria uma chave
    (com segredo e hash), cria o destino do formulário `demo` (com segredo e um caminho que vale como senha) e reprocessa
    um pedido com um motivo livre que cita a lead."""
    made = writer.post(
        url_of("create_api_key"),
        json={"name": "outra-" + secrets.token_hex(3), "role": "leitura", "confirm": True},
    ).json()
    destination = writer.put(
        url_of("save_destination", scope="demo"),
        json={
            "url": f"https://hooks.example.test/{PATH_SECRET}",
            "expected_version": "0",
            "confirm": True,
        },
    ).json()
    reason = f"Refazer o plano da Carla Dias, carla@example.test ({SENTINEL})"
    reprocessed = writer.post(
        url_of("reprocess_job", public_id=world.carla.public_id),
        json={"reason": reason, "expected_attempt": 1, "confirm": True},
    )
    assert reprocessed.status_code == 200, reprocessed.text
    return {
        "key": made["key"],
        "key_secret": made["key"].split("_", 2)[2],
        "destination_secret": destination["secret"],
        "reason": reason,
    }


def test_the_audit_never_carries_a_secret_a_key_hash_or_lead_data(
    deps: Deps, api_client: Client
) -> None:
    world = build_jobs_world(deps)
    writer = api_client("total")
    done = acts(deps, world, writer)
    reader = api_client("leitura")  # outra chave, que a própria auditoria também registra
    with deps.sessions() as session:
        hashes = list(session.scalars(sa.select(ApiKey.secret_hash)))

    text = "\n".join(page.text for page in pages(reader, limit=200))

    forbidden = [
        done["key"],
        done["key_secret"],
        *hashes,
        done["destination_secret"],
        PATH_SECRET,
        done["reason"],
        SENTINEL,
        "Carla Dias",
        *(text for person in PEOPLE.values() for text in person),
    ]
    assert len(hashes) == 3  # as chaves dos dois clientes e a que a ação criou
    for secret in forbidden:
        assert secret not in text, secret
    # O que a auditoria diz de cada ação é o que a consulta sabe que é seguro.
    items = [item for page in pages(reader, limit=200) for item in page.json()["items"]]
    assert {item["action"] for item in items} >= {"api_key.create", "job.reprocess"}
    for item in items:
        assert set(item["details"]) <= PUBLIC_DETAIL_KEYS, item["action"]
        assert "reason" not in item["details"], item["action"]


def test_the_audit_of_a_reprocess_keeps_the_attempt_and_the_failure_but_not_the_reason(
    deps: Deps, api_client: Client
) -> None:
    world = build_jobs_world(deps)
    writer = api_client("total")
    acts(deps, world, writer)

    (item,) = read_log(
        api_client("leitura"), action="job.reprocess", target_id=str(world.carla.id)
    ).json()["items"]

    assert item["actor"] == writer.actor and item["target_type"] == "job"
    assert set(item["details"]) == {"attempt", "previous_status", "failure_code"}
    assert item["details"]["attempt"] == 2 and item["details"]["previous_status"] == "failed"
    with deps.sessions() as session:  # o banco guarda o motivo (a página o mostra a quem pode)
        stored = session.scalars(
            sa.select(AuditLog.details).where(
                AuditLog.action == "job.reprocess", AuditLog.target_id == str(world.carla.id)
            )
        ).one()
    assert SENTINEL in stored["reason"]


def test_the_audit_of_a_destination_keeps_the_host_and_not_the_path_of_the_url(
    deps: Deps, api_client: Client
) -> None:
    world = build_jobs_world(deps)
    writer = api_client("total")
    acts(deps, world, writer)

    (item,) = read_log(
        api_client("leitura"), action="webhook_destination.create", actor=writer.actor
    ).json()["items"]

    assert item["target_type"] == "webhook_destination"
    assert item["details"] == {
        "form_slug": "demo",
        "host": "hooks.example.test",
        "events": ["plano.concluido"],
    }


@pytest.fixture
def baserow(deps: Deps) -> FakeBaserow:
    return FakeBaserow().install(deps)


def test_the_upstream_error_of_a_test_action_stays_in_the_database_and_never_reaches_the_read(
    deps: Deps, api_client: Client, baserow: FakeBaserow
) -> None:
    """O texto de um erro de fora é só do `total` (o projeto o trata assim: o último erro de uma
    sincronização só sai no `lead_data.notes`). O teste da integração o grava em `details.error`, e a
    leitura da auditoria, que é de `leitura`, não o devolve a ninguém: nem à chave `total`, que o tem na
    resposta da própria ação. O resultado (`ok`) e o resto saem."""
    connect_form(deps, active=False)
    baserow.fail("GET", 503, where="fields")
    writer = api_client("total")

    sent = writer.post(url_of("send_form_integration_test", slug=SLUG), json={"confirm": True})

    assert sent.status_code == 200 and "HTTP 503" in sent.json()["error"]
    with deps.sessions() as session:
        stored = session.scalars(
            sa.select(AuditLog.details).where(AuditLog.action == "integration.test")
        ).one()
    assert "HTTP 503" in stored["error"]  # o banco guarda o texto
    for role in ("leitura", "total"):
        found = read_log(api_client(role), action="integration.test")
        (item,) = found.json()["items"]
        assert item["actor"] == writer.actor
        assert "error" not in item["details"] and "HTTP 503" not in found.text, role
        assert item["details"]["ok"] is False and item["details"]["form_slug"] == SLUG


def test_the_error_of_the_destination_test_is_withheld_but_the_status_and_the_time_come_out(
    deps: Deps, api_client: Client
) -> None:
    seed(
        deps,
        entry(
            actor="operadora",
            action="webhook_destination.test",
            target_type="webhook_destination",
            details={
                "form_slug": None,
                "host": "hooks.example.test",
                "status": None,
                "elapsed_ms": 31,
                "error": "ConnectError",
            },
        ),
    )

    (item,) = read_log(api_client("leitura"), action="webhook_destination.test").json()["items"]

    assert item["details"] == {
        "form_slug": None,
        "host": "hooks.example.test",
        "status": None,
        "elapsed_ms": 31,
    }


def test_what_the_database_holds_beyond_what_is_known_safe_never_leaves(
    deps: Deps, api_client: Client
) -> None:
    """A lista não confia em quem vai gravar: um `details` com chaves que ela não conhece (um segredo, um
    hash, um texto livre, o e-mail de uma lead) sai sem elas, ainda que alguém as tenha posto no banco, e o
    que está sob uma chave permitida vem inteiro. Uma chave nova com nome de segredo não passa só por ter o
    nome parecido."""
    secret, hashed, email = (secrets.token_hex(8) for _ in range(3))
    hostile = entry(
        actor="hostil",
        details={
            "form_slug": "demo",
            "host": "hooks.example.test",
            "secret": secret,
            "secret_hash": hashed,
            "token": secret,
            "api_key": secret,
            "password": secret,
            "authorization": f"Bearer {secret}",
            "reason": f"cite {email}",
            "email": f"{email}@example.test",
            "answers": {"email": email},
            "unknown_key": secret,
            "images": {"logo": {"fundo": secret}},  # chave segura, valor fundo demais
            "contact_in_prompt": [{"email": email}],  # chave segura, itens que não são simples
        },
    )
    seed(deps, hostile)

    response = read_log(api_client("leitura"), actor="hostil")

    (item,) = response.json()["items"]
    assert item["details"] == {
        "form_slug": "demo",
        "host": "hooks.example.test",
    }
    for leaked in (secret, hashed, email):
        assert leaked not in response.text


def test_the_allow_list_filters_by_the_name_of_the_key_and_not_by_the_content(
    deps: Deps, api_client: Client
) -> None:
    """O limite, dito em teste: o valor sob uma chave permitida sai como foi escrito, seja qual for.
    Hoje nenhum serviço grava um segredo sob `host` ou `name`; se um gravasse, a lista não o pegaria (é a
    revisão de quem grava e de quem inclui a chave que pega)."""
    value = "valor-" + secrets.token_hex(8)
    seed(deps, entry(actor="descuidado", details={"host": value, "name": value, "reason": value}))

    response = read_log(api_client("leitura"), actor="descuidado")

    (item,) = response.json()["items"]
    assert item["details"] == {"host": value, "name": value}  # sob a chave permitida, passa
    assert response.text.count(value) == 2  # e o `reason`, que não é permitido, não passa


def test_the_known_safe_details_come_out_with_their_values_as_they_were_written(
    deps: Deps, api_client: Client
) -> None:
    written = {
        "form_slug": "demo",
        "version": 4,
        "fingerprint": "ab12",
        "ok": True,
        "previous_host": None,
        "elapsed_ms": 12,
        "contact_in_prompt": ["name", "email"],
        "images": {"logo": "e3b0c442"},
        "expires_at": "2027-01-01T00:00:00+00:00",
    }
    seed(deps, entry(actor="honesto", details=written))

    (item,) = read_log(api_client("leitura"), actor="honesto").json()["items"]

    assert item["details"] == written


# --- o contrato, no arquivo e na matriz ---------------------------------------------------------


def the_operation() -> dict[str, Any]:
    document = json.loads((ROOT / OPENAPI_FILE).read_text(encoding="utf-8"))
    (found,) = [
        operation
        for _, _, operation in operations_of(document)
        if operation["operationId"] == "list_audit_log"
    ]
    return found


def test_list_audit_log_is_a_plain_read_in_the_file_and_in_the_matrix() -> None:
    operation = the_operation()
    (row,) = [row for row in read_matrix().rows if row.operation_id == "list_audit_log"]

    assert operation["x-required-role"] == row.role == "leitura"
    assert not {"x-destructive", "x-external-effect", "x-lead-data"} & set(operation)
    assert row.marks == frozenset() and row.status == "feita"
    assert row.operation == "GET /audit-log" and row.test is not None
    assert row.test.startswith("tests_app/test_admin_api_audit.py::test_")
    assert {p["name"] for p in operation["parameters"]} == {
        "limit",
        "cursor",
        "target_type",
        "target_id",
        "actor",
        "action",
    }
    assert {"401", "422", "429", "200"} <= set(operation["responses"])
    assert not {"403", "404", "409"} & set(
        operation["responses"]
    )  # nem papel, nem recurso, nem versão
