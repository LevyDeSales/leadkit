"""As leituras dos pedidos (`/admin/api/v1`): `list_jobs`, `get_job` e `download_job_pdf`, pela costura
HTTP.

Elas leem pela mesma consulta e pela mesma ação que as páginas (`app.admin.queries.jobs` e
`app.admin.actions.jobs`), e a paridade com a página (os mesmos pedidos, os mesmos estados e o mesmo
PDF para os mesmos dados) está no fim de cada operação. O que separa o papel `leitura` da lead é o
bloco `lead_data`: com `leitura`, nenhuma resposta traz nome, e-mail, telefone, respostas, pontuação,
consentimento nem plano, nem o texto livre de um motivo. As chaves destes testes são sorteadas na
execução.
"""

from __future__ import annotations

import json
import re
import secrets
import typing
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_matrix import OPENAPI_FILE, ROOT, operations_of
from admin_api_matrix import read as read_matrix
from admin_api_support import API, ApiClient, make_api, url_of
from jobs_world import (
    FAILURE_DETAIL,
    FERNANDA,
    PEOPLE,
    REASON,
    SYNC_CASES,
    SYNC_ERROR,
    SYNC_EXPECTED,
    SYNC_SHOWN,
    VERSION_EXPECTED,
    VERSION_OF,
    JobsWorld,
    add_mcc_order,
    build_jobs_world,
    jobs_by_sync,
    jobs_by_version,
)
from test_admin import (
    ARRIVALS,
    DELIVERY_CASES,
    DELIVERY_EXPECTED,
    PERIOD_EXPECTED,
    PERIOD_NOW,
    FakeClock,
    jobs_by_arrival,
    jobs_by_delivery,
    login,
    new_lead_job,
)

from app.admin.api.common import encode_cursor
from app.admin.api.schemas.jobs import DeliveryFilter, SyncFilter
from app.admin.queries.jobs import DELIVERY_FILTERS, SYNC_FILTERS
from app.deps import Deps
from app.errors import FailureCode, RetryableError
from app.forms import FormSpec
from app.models import Artifact, Delivery, utcnow

JOBS = f"{API}/jobs"
PAGE = "/admin"
SENTINEL = "ZZ_SENT_" + secrets.token_hex(8).upper()

# Tudo o que identifica uma lead nestes pedidos, ou foi escrito por quem a conhece: nada disso pode
# aparecer numa resposta de chave `leitura`.
PERSONAL_TEXT = [
    *(text for person in PEOPLE.values() for text in person),
    *FERNANDA,
    "Escritório Exemplo",
    "Organizar a captação de clientes",
    "Organizar atividades do projeto",
    REASON,
    FAILURE_DETAIL,
    SYNC_ERROR,
    "HTTP 503",
]
# Os campos que só o `lead_data` pode ter (o mesmo conjunto do teste de contrato).
PERSONAL_FIELDS = {
    "name",
    "email",
    "phone",
    "persona",
    "priority",
    "scores",
    "answers",
    "consent",
    "plan",
    "notes",
}

Client = Callable[..., ApiClient]
# As três leituras e o papel de cada uma.
ORDER_READS = {"list_jobs": "leitura", "get_job": "leitura", "download_job_pdf": "total"}


@pytest.fixture
def world(deps: Deps) -> JobsWorld:
    return build_jobs_world(deps)


def ids(response: httpx.Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["public_id"] for item in response.json()["items"]]


def newest_first(world: JobsWorld) -> list[str]:
    return [job.public_id for job in reversed(world.all)]


def key_names(node: Any) -> set[str]:
    """Todo nome de campo de um JSON, em qualquer profundidade, fora do bloco `lead_data`."""
    found: set[str] = set()
    if isinstance(node, dict):
        for name, value in node.items():
            found.add(name)
            if name != "lead_data":
                found |= key_names(value)
    elif isinstance(node, list):
        for item in node:
            found |= key_names(item)
    return found


def assert_no_lead_data(response: httpx.Response) -> None:
    """Nenhum dado pessoal da lead: nem o texto, nem um campo que só o `lead_data` teria."""
    for text in PERSONAL_TEXT:
        assert text not in response.text, text
    assert PERSONAL_FIELDS.isdisjoint(key_names(response.json()))
    assert all(
        node is None
        for node in _lead_blocks(response.json())  # o bloco, quando existe, vem vazio
    )


def _lead_blocks(node: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(node, dict):
        for name, value in node.items():
            if name == "lead_data":
                found.append(value)
            else:
                found += _lead_blocks(value)
    elif isinstance(node, list):
        for item in node:
            found += _lead_blocks(item)
    return found


# --- list_jobs ----------------------------------------------------------------------------------


def test_list_jobs_needs_a_key(deps: Deps, world: JobsWorld) -> None:
    response = make_api(deps, FakeClock()).get(JOBS)  # sem chave

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"
    assert response.headers["www-authenticate"] == "Bearer"
    assert_no_lead_data(response)


def test_list_jobs_is_an_envelope_of_the_newest_first_with_the_state_and_no_lead_data(
    api_client: Client, world: JobsWorld
) -> None:
    response = api_client("leitura").get(url_of("list_jobs"))

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"items", "next_cursor", "total"}
    assert body["next_cursor"] is None and body["total"] is None  # o total só com include_total
    assert ids(response) == newest_first(world)
    by_id = {item["public_id"]: item for item in body["items"]}
    ana, beatriz = by_id[world.ana.public_id], by_id[world.beatriz.public_id]
    assert set(ana) == {
        "public_id",
        "status",
        "stage",
        "attempt",
        "form_slug",
        "form_version",
        "failure_code",
        "created_at",
        "deliveries",
        "syncs",
        "lead_data",
    }
    assert (ana["status"], ana["stage"], ana["attempt"], ana["form_slug"]) == (
        "completed",
        "completed",
        1,
        "demo",
    )
    assert ana["deliveries"] == [{"destination": "webhook", "status": "succeeded"}]
    # a lista traz as entregas da tentativa atual: a 2ª, que ainda está pendente
    assert (beatriz["attempt"], beatriz["deliveries"]) == (
        2,
        [{"destination": "webhook", "status": "pending"}],
    )
    # e as sincronizações com a planilha, da tentativa atual, só com o tipo e o estado: a da 1ª
    # tentativa da Beatriz (pendente, nunca disparada) não conta, e o erro da 2ª não sai.
    assert ana["syncs"] == [{"integration": "baserow", "status": "succeeded"}]
    assert beatriz["syncs"] == [{"integration": "baserow", "status": "failed"}]
    assert all(by_id[job.public_id]["syncs"] == [] for job in (world.carla, world.debora))
    assert SYNC_ERROR not in response.text and "integration_unavailable" not in response.text
    assert by_id[world.carla.public_id]["failure_code"] == "llm_unavailable"
    assert by_id[world.elisa.public_id]["status"] == "running"
    assert by_id[world.elisa.public_id]["stage"] == "rendering"
    assert by_id[world.debora.public_id]["status"] == "queued"
    assert all(item["lead_data"] is None for item in body["items"])
    assert_no_lead_data(response)


def test_list_jobs_says_dates_in_utc_and_the_states_in_english(
    api_client: Client, world: JobsWorld
) -> None:
    items = api_client("leitura").get(JOBS).json()["items"]

    for item in items:
        assert item["created_at"].endswith("Z")
        assert datetime.fromisoformat(item["created_at"]).utcoffset() == timedelta(0)
        assert item["status"] in {"queued", "running", "completed", "failed"}
        assert item["stage"] in {"queued", "generating", "rendering", "completed", "failed"}


def test_list_jobs_paginates_by_cursor_and_neither_skips_nor_repeats_when_an_order_arrives(
    api_client: Client, deps: Deps, world: JobsWorld
) -> None:
    client = api_client("leitura")

    first = client.get(JOBS, params={"limit": 2})
    new_lead_job(deps, "Lead novo", "novo@example.test")  # entra no meio da leitura
    second = client.get(JOBS, params={"limit": 2, "cursor": first.json()["next_cursor"]})
    third = client.get(JOBS, params={"limit": 2, "cursor": second.json()["next_cursor"]})

    seen = [*ids(first), *ids(second), *ids(third)]
    assert seen == newest_first(world)  # o pedido novo ficou de fora; nenhum se repetiu ou sumiu
    assert first.json()["next_cursor"] and second.json()["next_cursor"]
    assert third.json()["next_cursor"] is None
    assert ids(client.get(JOBS))[0] != newest_first(world)[0]  # e a lista de agora já o traz


def test_list_jobs_limit_goes_from_1_to_200_and_a_bad_one_names_the_field(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")

    assert len(ids(client.get(JOBS, params={"limit": 1}))) == 1
    assert len(ids(client.get(JOBS, params={"limit": 200}))) == 5
    for limit in ("0", "201", "-1", "muitos"):
        refused = client.get(JOBS, params={"limit": limit})
        assert refused.status_code == 422 and refused.json()["code"] == "validation_failed"
        assert [e["field"] for e in refused.json()["errors"]] == ["limit"]


def test_list_jobs_a_cursor_we_did_not_give_is_a_422_on_the_cursor(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")
    foreign = "eyJhZnRlciI6ICJ4In0"  # um JSON válido que não é de uma lista de pedidos

    for cursor in ("lixo", foreign, "A" * 400):
        refused = client.get(JOBS, params={"cursor": cursor})
        assert refused.status_code == 422, cursor
        assert [e["field"] for e in refused.json()["errors"]] == ["cursor"]


def test_list_jobs_a_cursor_with_a_timestamp_without_timezone_is_a_422_on_the_cursor(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")
    job_id = uuid.uuid4()

    for position in (f"2026-10-01T12:00:00|{job_id}", f"2026-10-01|{job_id}"):
        refused = client.get(JOBS, params={"cursor": encode_cursor(position)})

        assert refused.status_code == 422, position
        assert [e["field"] for e in refused.json()["errors"]] == ["cursor"]


def test_list_jobs_total_comes_only_when_asked_and_counts_the_filter_not_the_page(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")

    everything = client.get(JOBS, params={"include_total": "true", "limit": 2}).json()
    failed = client.get(JOBS, params={"include_total": "true", "status": "failed"}).json()

    assert everything["total"] == 5 and len(everything["items"]) == 2
    assert failed["total"] == 1
    assert client.get(JOBS, params={"include_total": "false"}).json()["total"] is None


def test_list_jobs_filters_by_status_and_an_unknown_status_is_a_422_on_the_field(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")

    assert ids(client.get(JOBS, params={"status": "failed"})) == [world.carla.public_id]
    assert ids(client.get(JOBS, params={"status": "completed"})) == [
        world.beatriz.public_id,
        world.ana.public_id,
    ]
    refused = client.get(JOBS, params={"status": "feito"})
    assert refused.status_code == 422
    assert [e["field"] for e in refused.json()["errors"]] == ["status"]


def test_list_jobs_filters_by_form_and_a_form_nobody_has_is_an_empty_list(
    api_client: Client, deps: Deps, world: JobsWorld
) -> None:
    mcc = add_mcc_order(deps)

    for role in ("leitura", "total"):  # o formulário não é dado da lead
        client = api_client(role)
        assert ids(client.get(JOBS, params={"form": "mcc"})) == [mcc.public_id], role
        assert ids(client.get(JOBS, params={"form": "demo"})) == newest_first(world), role
        assert ids(client.get(JOBS, params={"form": "demo", "status": "failed"})) == [
            world.carla.public_id
        ]
        assert ids(client.get(JOBS, params={"form": "mcc", "status": "failed"})) == []

    client = api_client("leitura")
    unknown = client.get(JOBS, params={"form": "nao-existe", "include_total": "true"})
    assert unknown.status_code == 200
    assert unknown.json() == {"items": [], "next_cursor": None, "total": 0}
    totals = {
        form: client.get(JOBS, params={"form": form, "include_total": "true"}).json()["total"]
        for form in ("mcc", "demo")
    }
    assert totals == {"mcc": 1, "demo": 5}  # o total conta o filtro
    assert client.get(JOBS, params={"include_total": "true"}).json()["total"] == 6


def test_list_jobs_the_next_pages_stay_filtered_by_the_form(
    api_client: Client, deps: Deps, world: JobsWorld
) -> None:
    add_mcc_order(deps)  # o mais novo de todos: sem o filtro seria o primeiro da primeira página
    client = api_client("leitura")

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        params = {"form": "demo", "limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get(JOBS, params=params)
        seen += ids(page)
        cursor = page.json()["next_cursor"]
        if cursor is None:
            break

    assert seen == newest_first(world) and cursor is None


def test_list_jobs_a_form_that_is_not_an_address_is_a_422_on_the_field(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")

    for form in ("Mcc", "com espaço", "-mcc", "mcc-", "a--b", "x" * 65, ""):
        refused = client.get(JOBS, params={"form": form})

        assert refused.status_code == 422 and refused.json()["code"] == "validation_failed", form
        assert [e["field"] for e in refused.json()["errors"]] == ["form"], form


def test_list_jobs_filters_by_arrival_from_inclusive_to_exclusive_with_any_zone(
    api_client: Client, deps: Deps
) -> None:
    made = jobs_by_arrival(deps)
    names = {job.public_id: name for name, job in made.items()}

    def arrived(client: ApiClient, **params: str) -> list[str]:
        """Os instantes dos pedidos que a API devolve, do mais novo ao mais antigo."""
        return [names[code] for code in ids(client.get(JOBS, params=params))]

    for role in ("leitura", "total"):  # a data de chegada não é dado da lead
        client = api_client(role)
        today = ["tonight", "start_of_today"]
        assert arrived(client, created_from="2027-01-15T00:00:00-03:00") == today, role
        assert arrived(client, created_from="2027-01-15T03:00:00Z") == today  # o mesmo instante
        assert arrived(client, created_from="2027-01-15T03:00:00.000001Z") == ["tonight"]
        older = list(ARRIVALS)[2:]  # tudo o que chegou antes de hoje
        assert arrived(client, created_to="2027-01-15T00:00:00-03:00") == older, role
        assert arrived(client, created_to="2027-01-15T02:59:59.999999Z") == older  # o fim é aberto
        assert arrived(
            client, created_from="2027-01-09T00:00:00-03:00", created_to="2027-01-15T00:00:00-03:00"
        ) == ["yesterday_late", "start_of_7_days"]
        # Os extremos do calendário não estouram a consulta.
        everything = list(ARRIVALS)
        assert arrived(client, created_from="0001-01-01T00:00:00Z") == everything
        assert arrived(client, created_to="9999-12-31T23:59:59Z") == everything

    client = api_client("leitura")
    backwards = client.get(
        JOBS,
        params={
            "created_from": "2027-01-15T00:00:00-03:00",
            "created_to": "2027-01-09T00:00:00-03:00",
            "include_total": "true",
        },
    )
    assert backwards.status_code == 200  # de depois de até: lista vazia, não erro
    assert backwards.json() == {"items": [], "next_cursor": None, "total": 0}
    week = client.get(
        JOBS, params={"created_from": "2027-01-09T00:00:00-03:00", "include_total": "true"}
    )
    assert week.json()["total"] == 4  # o total conta o mesmo filtro
    # Combina por E com os outros filtros.
    assert arrived(client, created_from="2027-01-09T00:00:00-03:00", status="failed") == []
    assert arrived(client, created_from="2027-01-09T00:00:00-03:00", form="demo") == [
        "tonight",
        "start_of_today",
        "yesterday_late",
        "start_of_7_days",
    ]


def test_list_jobs_the_plus_of_the_zone_goes_as_percent_2b_in_the_url(
    api_client: Client, deps: Deps
) -> None:
    made = jobs_by_arrival(deps)
    client = api_client("leitura")

    escaped = client.get(f"{JOBS}?created_from=2027-01-15T00:00:00%2B00:00")
    bare = client.get(f"{JOBS}?created_from=2027-01-15T00:00:00+00:00")  # o `+` vira espaço

    assert ids(escaped) == [
        made[name].public_id for name in ("tonight", "start_of_today", "yesterday_late")
    ]
    assert bare.status_code == 422 and [e["field"] for e in bare.json()["errors"]] == [
        "created_from"
    ]


def test_list_jobs_the_next_pages_stay_inside_the_arrival_limits(
    api_client: Client, deps: Deps
) -> None:
    made = jobs_by_arrival(deps)
    client = api_client("leitura")

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(8):
        params = {"created_from": "2027-01-09T00:00:00-03:00", "limit": 1}
        page = client.get(JOBS, params={**params, **({"cursor": cursor} if cursor else {})})
        seen += ids(page)
        cursor = page.json()["next_cursor"]
        if cursor is None:
            break

    newest_first_of_the_week = [name for name in ARRIVALS if name in PERIOD_EXPECTED["7d"]]
    assert seen == [made[name].public_id for name in newest_first_of_the_week]
    assert cursor is None


def test_list_jobs_a_date_without_a_zone_or_malformed_is_a_422_on_the_field(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")

    for name in ("created_from", "created_to"):
        for value in (
            "2027-01-15T00:00:00",  # sem fuso
            "2027-01-15",  # só a data
            "ontem",
            "2027-13-45T00:00:00Z",
            "",
        ):
            refused = client.get(JOBS, params={name: value})

            assert refused.status_code == 422, (name, value)
            assert refused.json()["code"] == "validation_failed", (name, value)
            assert [e["field"] for e in refused.json()["errors"]] == [name], (name, value)


def test_list_jobs_filters_by_the_delivery_of_the_current_attempt(
    api_client: Client, deps: Deps
) -> None:
    made = jobs_by_delivery(deps)
    names = {job.public_id: name for name, job in made.items()}

    for role in ("leitura", "total"):  # a entrega é estado do pedido, não dado da lead
        client = api_client(role)
        for delivery, expected in DELIVERY_EXPECTED.items():
            response = client.get(
                JOBS,
                params={"include_total": "true", **({"delivery": delivery} if delivery else {})},
            )
            body = response.json()
            assert [names[code] for code in ids(response)] == [
                name for name in reversed(DELIVERY_CASES) if name in expected
            ], (role, delivery)
            assert body["total"] == len(expected), (role, delivery)  # o total conta o mesmo filtro
            if delivery:  # o item mostra a entrega que casou
                for item in body["items"]:
                    assert delivery in {d["status"] for d in item["deliveries"]}, (role, delivery)

    client = api_client("leitura")
    # Falhou na tentativa 1 e foi recebida na 2: a lista mostra "succeeded", e "failed" não o acha.
    failed = client.get(JOBS, params={"delivery": "failed"})
    assert made["retried_ok"].public_id not in ids(failed)
    everything = client.get(JOBS, params={"limit": 200}).json()["items"]
    (retried,) = (i for i in everything if i["public_id"] == made["retried_ok"].public_id)
    assert retried["attempt"] == 2 and [d["status"] for d in retried["deliveries"]] == ["succeeded"]
    # Combina por E com os outros filtros, e o pedido sem entrega não casa com nenhum.
    assert [
        names[c]
        for c in ids(client.get(JOBS, params={"delivery": "failed", "status": "completed"}))
    ] == [
        "regressed",
        "failed_now",
    ]
    assert ids(client.get(JOBS, params={"delivery": "failed", "status": "queued"})) == []
    assert ids(client.get(JOBS, params={"delivery": "failed", "form": "mcc"})) == []
    assert (
        ids(client.get(JOBS, params={"delivery": "pending", "q": made["failed_now"].public_id}))
        == []
    )
    for delivery in ("failed", "pending"):
        assert made["no_delivery"].public_id not in ids(
            client.get(JOBS, params={"delivery": delivery})
        )


def test_list_jobs_the_next_pages_stay_on_the_deliveries_that_match(
    api_client: Client, deps: Deps
) -> None:
    made = jobs_by_delivery(deps)
    client = api_client("leitura")

    first = client.get(JOBS, params={"delivery": "failed", "limit": 1})
    assert ids(first) == [made["regressed"].public_id]
    second = client.get(
        JOBS,
        params={"delivery": "failed", "limit": 1, "cursor": first.json()["next_cursor"]},
    )

    assert ids(second) == [made["failed_now"].public_id]
    assert (
        second.json()["next_cursor"] is None
    )  # os outros pedidos não entram nas páginas seguintes


def test_list_jobs_a_delivery_that_is_not_failed_or_pending_is_a_422_on_the_field(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")

    assert (
        typing.get_args(DeliveryFilter) == DELIVERY_FILTERS
    )  # o contrato e a página, os mesmos dois
    for value in ("succeeded", "Failed", "all", "", " failed", "x" * 300):
        refused = client.get(JOBS, params={"delivery": value})

        assert refused.status_code == 422, repr(value)
        assert refused.json()["code"] == "validation_failed", repr(value)
        assert [e["field"] for e in refused.json()["errors"]] == ["delivery"], repr(value)


def test_list_jobs_filters_by_the_sync_of_the_current_attempt(
    api_client: Client, deps: Deps
) -> None:
    made = jobs_by_sync(deps)
    names = {job.public_id: name for name, job in made.items()}

    for role in ("leitura", "total"):  # a sincronização é estado do pedido, não dado da lead
        client = api_client(role)
        for sync, expected in SYNC_EXPECTED.items():
            response = client.get(
                JOBS, params={"include_total": "true", **({"sync": sync} if sync else {})}
            )
            body = response.json()
            assert [names[code] for code in ids(response)] == [
                name for name in reversed(SYNC_CASES) if name in expected
            ], (role, sync)
            assert body["total"] == len(expected), (role, sync)  # o total conta o mesmo filtro
            for item in body["items"]:  # o item mostra a sincronização da tentativa atual
                assert [s["status"] for s in item["syncs"]] == SYNC_SHOWN[names[item["public_id"]]]
                if sync:
                    assert sync in {s["status"] for s in item["syncs"]}, (role, sync)

    client = api_client("leitura")
    # Falhou na tentativa 1 e deu certo na 2: a lista mostra "succeeded", e "failed" não o acha.
    assert made["retried_ok"].public_id not in ids(client.get(JOBS, params={"sync": "failed"}))
    # Combina por E com os outros filtros, e o pedido sem integração não casa com nenhum.
    assert [
        names[c] for c in ids(client.get(JOBS, params={"sync": "failed", "status": "completed"}))
    ] == ["regressed", "failed_now"]
    assert ids(client.get(JOBS, params={"sync": "failed", "status": "queued"})) == []
    assert ids(client.get(JOBS, params={"sync": "failed", "form": "mcc"})) == []
    assert ids(client.get(JOBS, params={"sync": "pending", "delivery": "failed"})) == []
    for sync in ("failed", "pending"):
        listed = ids(client.get(JOBS, params={"sync": sync}))
        assert made["queued"].public_id not in listed
        assert made["no_integration"].public_id not in listed


def test_list_jobs_the_next_pages_stay_on_the_syncs_that_match(
    api_client: Client, deps: Deps
) -> None:
    made = jobs_by_sync(deps)
    client = api_client("leitura")

    first = client.get(JOBS, params={"sync": "failed", "limit": 1})
    assert ids(first) == [made["regressed"].public_id]
    second = client.get(
        JOBS, params={"sync": "failed", "limit": 1, "cursor": first.json()["next_cursor"]}
    )

    assert ids(second) == [made["failed_now"].public_id]
    assert (
        second.json()["next_cursor"] is None
    )  # os outros pedidos não entram nas páginas seguintes


def test_list_jobs_a_sync_that_is_not_failed_or_pending_is_a_422_on_the_field(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")

    assert typing.get_args(SyncFilter) == SYNC_FILTERS  # o contrato e a página, os mesmos dois
    for value in ("succeeded", "Failed", "all", "", " failed", "x" * 300):
        refused = client.get(JOBS, params={"sync": value})

        assert refused.status_code == 422, repr(value)
        assert refused.json()["code"] == "validation_failed", repr(value)
        assert [e["field"] for e in refused.json()["errors"]] == ["sync"], repr(value)


def test_list_jobs_a_read_key_searches_by_the_code_and_finds_nobody_by_name_or_email(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")
    name, email = PEOPLE["ana"]

    by_code = client.get(JOBS, params={"q": world.ana.public_id})
    by_name = client.get(JOBS, params={"q": name.split()[0]})
    by_email = client.get(JOBS, params={"q": email})

    assert ids(by_code) == [world.ana.public_id]
    assert ids(by_name) == [] and ids(by_email) == []  # 200 e vazia: não sonda quem é lead
    assert by_email.json()["items"] == [] and email not in by_email.text
    assert (
        ids(client.get(JOBS, params={"q": world.ana.public_id.lower()})) == []
    )  # o código é exato


def test_list_jobs_a_total_key_searches_by_name_email_and_code(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("total")

    assert ids(client.get(JOBS, params={"q": "souza"})) == [world.ana.public_id]  # parte do nome
    assert ids(client.get(JOBS, params={"q": "BEATRIZ@"})) == [world.beatriz.public_id]
    assert ids(client.get(JOBS, params={"q": world.carla.public_id})) == [world.carla.public_id]
    assert ids(client.get(JOBS, params={"q": "100%_"})) == []  # os curingas do LIKE são texto
    assert ids(client.get(JOBS, params={"q": "e", "status": "failed"})) == [world.carla.public_id]
    # a busca por pessoa acha o pedido, mas o dado da lead só vem se pedido
    assert_no_lead_data(client.get(JOBS, params={"q": "souza"}))


def test_list_jobs_the_lead_data_comes_only_with_a_total_key_and_when_asked(
    api_client: Client, world: JobsWorld
) -> None:
    total = api_client("total")

    plain = total.get(JOBS)
    asked = total.get(JOBS, params={"include_lead_data": "true"})

    assert all(item["lead_data"] is None for item in plain.json()["items"])
    first = asked.json()["items"][-1]  # o mais antigo: a Ana
    assert first["public_id"] == world.ana.public_id
    assert first["lead_data"] == {
        "name": PEOPLE["ana"][0],
        "email": PEOPLE["ana"][1],
        "priority": "P2",
    }
    assert [i["lead_data"]["email"] for i in asked.json()["items"]][::-1] == [
        PEOPLE[name][1] for name in ("ana", "beatriz", "carla", "debora", "elisa")
    ]


def test_list_jobs_asking_for_the_lead_data_with_a_read_key_is_the_same_403(
    api_client: Client, world: JobsWorld
) -> None:
    refused = api_client("leitura").get(JOBS, params={"include_lead_data": "true"})

    assert refused.status_code == 403 and refused.json()["code"] == "forbidden_role"
    assert set(refused.json()) == {"code", "detail"}
    assert "include_lead_data" in refused.json()["detail"] and "«total»" in refused.json()["detail"]
    assert_no_lead_data(refused)


def test_list_jobs_with_nothing_in_the_database_is_one_empty_page(api_client: Client) -> None:
    body = api_client("leitura").get(JOBS, params={"include_total": "true"}).json()

    assert body == {"items": [], "next_cursor": None, "total": 0}


def test_list_jobs_echoes_none_of_a_free_text_that_is_otherwise_valid(
    api_client: Client, world: JobsWorld
) -> None:
    """`q` é texto livre. O teste de eco geral para no 422 do Pydantic antes de a consulta rodar; aqui
    os outros campos são válidos e só o texto livre leva o sentinela, para ele chegar à busca."""
    for role in ("leitura", "total"):
        response = api_client(role).get(
            JOBS, params={"q": SENTINEL, "status": "failed", "limit": 5, "include_total": "true"}
        )

        assert response.status_code == 200 and response.json()["items"] == []
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


def test_list_jobs_echoes_none_of_a_value_that_is_refused(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")

    for name in (
        "status",
        "form",
        "delivery",
        "sync",
        "created_from",
        "created_to",
        "limit",
        "cursor",
        "include_total",
        "include_lead_data",
    ):
        response = client.get(JOBS, params={name: SENTINEL})

        assert response.status_code in (403, 422), name
        assert SENTINEL not in response.text, name


def test_list_jobs_shows_the_same_orders_as_the_page_for_the_same_filters(
    api_client: Client, deps: Deps, world: JobsWorld
) -> None:
    """Paridade de leitura (`assert_same_effect` compara o banco, e aqui nada grava): para os
    mesmos dados e os mesmos filtros, a página e a API devolvem os mesmos pedidos, na mesma ordem."""
    mcc = add_mcc_order(deps)  # o mundo é do demo; este é de outro formulário
    client = api_client("total")
    assert login(client).status_code == 303

    codes = {job.public_id for job in [*world.all, mcc]}

    def on_the_page(**params: str) -> tuple[list[str], int]:
        html = client.get(PAGE, params=params).text
        listed = [
            code for code in re.findall(r'<span class="mono">([^<]+)</span>', html) if code in codes
        ]
        total = re.search(r'role="status">(\d+) pedido', html)
        return listed, int(total.group(1)) if total else 0

    def on_the_api(**params: str) -> tuple[list[str], int]:
        body = client.get(JOBS, params={**params, "include_total": "true"}).json()
        return [item["public_id"] for item in body["items"]], body["total"]

    assert on_the_page()[0] == [mcc.public_id, *newest_first(world)]  # o teste olha o que diz olhar
    assert on_the_page(form="mcc")[0] == [mcc.public_id]
    for filters in (
        {},
        {"status": "failed"},
        {"status": "completed"},
        {"q": "souza"},
        {"q": "BEATRIZ@"},
        {"q": world.carla.public_id},
        {"q": "e", "status": "queued"},
        {"q": "nao-existe"},
        {"form": "mcc"},
        {"form": "demo"},
        {"form": "demo", "status": "failed"},
        {"form": "mcc", "status": "queued"},
        {"form": "mcc", "status": "failed"},
        {"form": "mcc", "q": "rocha"},
        {"form": "demo", "q": "rocha"},
        {"form": "nao-existe"},
    ):
        assert on_the_api(**filters) == on_the_page(**filters), filters


def test_list_jobs_the_period_of_the_page_is_the_same_limits_on_the_api(
    api_client: Client, deps: Deps
) -> None:
    """Paridade do "Recebido": `period` na página lista os mesmos pedidos que a API com os limites
    que a página calcula (a meia-noite de Brasília do primeiro dia), a mesma ordem e o mesmo total."""
    made = jobs_by_arrival(deps)
    client = api_client("total")
    client.clock.now = PERIOD_NOW.timestamp()  # o "agora" da página
    assert login(client).status_code == 303
    codes = {job.public_id for job in made.values()}

    def on_the_page(**params: str) -> tuple[list[str], int]:
        html = client.get(PAGE, params=params).text
        listed = [
            code for code in re.findall(r'<span class="mono">([^<]+)</span>', html) if code in codes
        ]
        total = re.search(r'role="status">(\d+) pedido', html)
        return listed, int(total.group(1)) if total else 0

    def on_the_api(**params: str) -> tuple[list[str], int]:
        body = client.get(JOBS, params={**params, "include_total": "true"}).json()
        return [item["public_id"] for item in body["items"]], body["total"]

    # Os limites escritos à mão: hoje, e hoje com os seis ou os 29 dias anteriores.
    limits = {
        "today": {"created_from": "2027-01-15T00:00:00-03:00"},
        "7d": {"created_from": "2027-01-09T00:00:00-03:00"},
        "30d": {"created_from": "2026-12-17T00:00:00-03:00"},
        "": {},
    }
    for period, api_params in limits.items():
        page_filter = {"period": period} if period else {}
        listed = on_the_page(**page_filter)
        assert listed[1] == len(PERIOD_EXPECTED[period]), period  # o teste olha o que diz olhar
        assert on_the_api(**api_params) == listed, period
        assert on_the_api(**api_params, status="queued") == on_the_page(
            **page_filter, status="queued"
        )
        assert on_the_api(**api_params, form="demo") == on_the_page(**page_filter, form="demo")


def test_list_jobs_the_delivery_filter_lists_the_same_orders_as_the_page(
    api_client: Client, deps: Deps
) -> None:
    """Paridade da "Entrega": a página e a API devolvem os mesmos pedidos, na mesma ordem e com o
    mesmo total, para os mesmos dados e os mesmos filtros."""
    made = jobs_by_delivery(deps)
    client = api_client("total")
    assert login(client).status_code == 303
    codes = {job.public_id for job in made.values()}

    def on_the_page(**params: str) -> tuple[list[str], int]:
        html = client.get(PAGE, params=params).text
        listed = [
            code for code in re.findall(r'<span class="mono">([^<]+)</span>', html) if code in codes
        ]
        total = re.search(r'role="status">(\d+) pedido', html)
        return listed, int(total.group(1)) if total else 0

    def on_the_api(**params: str) -> tuple[list[str], int]:
        body = client.get(JOBS, params={**params, "include_total": "true"}).json()
        return [item["public_id"] for item in body["items"]], body["total"]

    for delivery, expected in DELIVERY_EXPECTED.items():
        page_filter = {"delivery": delivery} if delivery else {}
        listed = on_the_page(**page_filter)
        assert listed[1] == len(expected), delivery  # o teste olha o que diz olhar
        assert on_the_api(**page_filter) == listed, delivery
    for filters in (
        {"delivery": "failed", "status": "completed"},
        {"delivery": "failed", "status": "queued"},
        {"delivery": "failed", "form": "demo"},
        {"delivery": "failed", "form": "mcc"},
        {"delivery": "failed", "q": "regressed"},
        {"delivery": "pending", "q": "regressed"},
        {"delivery": "pending", "status": "completed"},
    ):
        assert on_the_api(**filters) == on_the_page(**filters), filters


def test_list_jobs_the_sync_filter_lists_the_same_orders_as_the_page(
    api_client: Client, deps: Deps
) -> None:
    """Paridade da "Planilha": a página e a API devolvem os mesmos pedidos, na mesma ordem e com o
    mesmo total, para os mesmos dados e os mesmos filtros."""
    made = jobs_by_sync(deps)
    client = api_client("total")
    assert login(client).status_code == 303
    codes = {job.public_id for job in made.values()}

    def on_the_page(**params: str) -> tuple[list[str], int]:
        html = client.get(PAGE, params=params).text
        listed = [
            code for code in re.findall(r'<span class="mono">([^<]+)</span>', html) if code in codes
        ]
        total = re.search(r'role="status">(\d+) pedido', html)
        return listed, int(total.group(1)) if total else 0

    def on_the_api(**params: str) -> tuple[list[str], int]:
        body = client.get(JOBS, params={**params, "include_total": "true"}).json()
        return [item["public_id"] for item in body["items"]], body["total"]

    for sync, expected in SYNC_EXPECTED.items():
        page_filter = {"sync": sync} if sync else {}
        listed = on_the_page(**page_filter)
        assert listed[1] == len(expected), sync  # o teste olha o que diz olhar
        assert on_the_api(**page_filter) == listed, sync
    for filters in (
        {"sync": "failed", "status": "completed"},
        {"sync": "failed", "status": "queued"},
        {"sync": "failed", "form": "demo"},
        {"sync": "failed", "form": "mcc"},
        {"sync": "failed", "q": "regressed"},
        {"sync": "pending", "q": "regressed"},
        {"sync": "failed", "delivery": "pending"},
        {"sync": "pending", "delivery": "failed"},
    ):
        assert on_the_api(**filters) == on_the_page(**filters), filters


def test_list_jobs_filters_by_the_form_version_inside_the_form(
    api_client: Client, deps: Deps, newer_demo: FormSpec
) -> None:
    made = jobs_by_version(deps, newer_demo)
    names = {job.public_id: name for name, job in made.items()}

    for role in ("leitura", "total"):  # a versão do formulário não é dado da lead
        client = api_client(role)
        for (form, version), expected in VERSION_EXPECTED.items():
            params = {"form": form, "include_total": "true"}
            response = client.get(
                JOBS, params={**params, **({"form_version": version} if version else {})}
            )
            assert [names[code] for code in ids(response)] == [
                name for name in reversed(list(VERSION_OF)) if name in expected
            ], (role, form, version)
            assert response.json()["total"] == len(expected), (role, form, version)

    client = api_client("leitura")
    # Uma versão que ninguém tem, ou que é de outro formulário, dá a lista vazia, não erro.
    for form, version in (("demo", "demo-v9"), ("demo", "mcc-v1"), ("mcc", "demo-v2")):
        unknown = client.get(
            JOBS, params={"form": form, "form_version": version, "include_total": "true"}
        )
        assert unknown.status_code == 200, (form, version)
        assert unknown.json() == {"items": [], "next_cursor": None, "total": 0}, (form, version)
    # O texto legado e o `<endereço>-v<número>` são duas versões do mesmo formulário.
    legacy = client.get(JOBS, params={"form": "demo", "form_version": "demo-2026.10"})
    assert [names[code] for code in ids(legacy)] == ["legacy"]
    # Combina por E com os outros filtros.
    assert (
        ids(
            client.get(JOBS, params={"form": "demo", "form_version": "demo-v2", "status": "failed"})
        )
        == []
    )
    assert ids(
        client.get(
            JOBS,
            params={"form": "demo", "form_version": "demo-v2", "q": made["v2"].public_id},
        )
    ) == [made["v2"].public_id]


def test_list_jobs_the_next_pages_stay_filtered_by_the_form_version(
    api_client: Client, deps: Deps, newer_demo: FormSpec
) -> None:
    made = jobs_by_version(deps, newer_demo)
    client = api_client("leitura")

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(4):
        params = {"form": "demo", "form_version": "demo-v2", "limit": 1}
        page = client.get(JOBS, params={**params, **({"cursor": cursor} if cursor else {})})
        seen += ids(page)
        cursor = page.json()["next_cursor"]
        if cursor is None:
            break

    assert seen == [made["v2_again"].public_id, made["v2"].public_id] and cursor is None


def test_list_jobs_a_form_version_that_is_too_long_or_empty_or_without_a_form_is_a_422_on_the_field(
    api_client: Client, deps: Deps, newer_demo: FormSpec
) -> None:
    jobs_by_version(deps, newer_demo)
    client = api_client("leitura")

    refused = [
        {"form": "demo", "form_version": SENTINEL + "x" * 64},  # passa de 64 caracteres
        {"form": "demo", "form_version": ""},
        {"form_version": "demo-v2"},  # a versão é de um formulário: sozinha não filtra nada
        {"form": "demo", "form_version": f"a\x00{SENTINEL}"},  # caractere de controle
        {"form": "demo", "form_version": f"a\n{SENTINEL}"},
    ]
    for params in refused:
        response = client.get(JOBS, params=params)

        assert response.status_code == 422 and response.json()["code"] == "validation_failed", (
            params
        )
        assert [e["field"] for e in response.json()["errors"]] == ["form_version"], params
        assert SENTINEL not in response.text, params  # a recusa não repete o valor
    assert ids(client.get(JOBS, params={"form": "demo", "form_version": "v" * 64})) == []


def test_list_jobs_the_form_version_filter_lists_the_same_orders_as_the_page(
    api_client: Client, deps: Deps, newer_demo: FormSpec
) -> None:
    """Paridade da "Versão": a página e a API devolvem os mesmos pedidos, na mesma ordem e com o
    mesmo total, para os mesmos dados e os mesmos filtros."""
    made = jobs_by_version(deps, newer_demo)
    client = api_client("total")
    assert login(client).status_code == 303
    codes = {job.public_id for job in made.values()}

    def on_the_page(**params: str) -> tuple[list[str], int]:
        html = client.get(PAGE, params=params).text
        listed = [
            code for code in re.findall(r'<span class="mono">([^<]+)</span>', html) if code in codes
        ]
        total = re.search(r'role="status">(\d+) pedido', html)
        return listed, int(total.group(1)) if total else 0

    def on_the_api(**params: str) -> tuple[list[str], int]:
        body = client.get(JOBS, params={**params, "include_total": "true"}).json()
        return [item["public_id"] for item in body["items"]], body["total"]

    for (form, version), expected in VERSION_EXPECTED.items():
        filters = {"form": form, **({"form_version": version} if version else {})}
        listed = on_the_page(**filters)
        assert listed[1] == len(expected), (form, version)  # o teste olha o que diz olhar
        assert on_the_api(**filters) == listed, (form, version)
    for filters in (
        {"form": "demo", "form_version": "demo-v2", "status": "completed"},
        {"form": "demo", "form_version": "demo-v2", "q": "v2_again"},
        {"form": "demo", "form_version": "demo-v1", "q": "v2_again"},
        {"form": "demo", "form_version": "demo-v2", "delivery": "pending"},
        {"form": "demo", "form_version": "demo-v2", "sync": "failed"},
    ):
        assert on_the_api(**filters) == on_the_page(**filters), filters


# --- get_job ------------------------------------------------------------------------------------

STATE_FIELDS = {
    "public_id",
    "status",
    "stage",
    "attempt",
    "form_slug",
    "form_version",
    "failure_code",
    "created_at",
    "completed_at",
    "link_expires_at",
    "link_expired",
    "run_url",
    "pdfs",
    "deliveries",
    "syncs",
    "timeline",
    "lead_data",
    "current_pdf",
}
RUN_URL = "https://hatchet.example.test/runs/{run_id}"


def get_job(client: ApiClient, job: Any, **params: str) -> httpx.Response:
    return client.get(url_of("get_job", public_id=job.public_id), params=params)


def event_types(attempt: dict[str, Any]) -> list[str]:
    return [event["type"] for event in attempt["events"]]


def all_instants(node: Any) -> list[str]:
    """Todo valor de um campo de data (`*_at`) de um JSON, em qualquer profundidade."""
    found: list[str] = []
    if isinstance(node, dict):
        for name, value in node.items():
            if (name.endswith("_at") or name == "at") and isinstance(value, str):
                found.append(value)
            found += all_instants(value)
    elif isinstance(node, list):
        for item in node:
            found += all_instants(item)
    return found


def test_get_job_needs_a_key_and_an_order_that_does_not_exist_is_a_404(
    deps: Deps, api_client: Client, world: JobsWorld
) -> None:
    anonymous = make_api(deps, FakeClock()).get(f"{JOBS}/{world.ana.public_id}")
    missing = api_client("leitura").get(f"{JOBS}/nao-existe")

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert_no_lead_data(anonymous)
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    assert set(missing.json()) == {"code", "detail"}


def test_get_job_a_read_key_reads_the_state_of_every_order_and_no_lead_data(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")

    for job in world.all:
        response = get_job(client, job)

        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        body = response.json()
        assert set(body) == STATE_FIELDS and body["public_id"] == job.public_id
        assert body["lead_data"] is None
        assert_no_lead_data(response)


def test_get_job_the_state_says_the_attempt_the_form_and_the_failure(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")

    carla = get_job(client, world.carla).json()
    elisa = get_job(client, world.elisa).json()
    beatriz = get_job(client, world.beatriz).json()

    assert (carla["status"], carla["stage"], carla["failure_code"]) == (
        "failed",
        "failed",
        "llm_unavailable",
    )
    assert carla["completed_at"] is None and carla["pdfs"] == []
    assert (elisa["status"], elisa["stage"]) == ("running", "rendering")
    assert (beatriz["status"], beatriz["attempt"], beatriz["failure_code"]) == (
        "completed",
        2,
        None,
    )
    assert beatriz["form_slug"] == "demo" and beatriz["form_version"]
    assert beatriz["completed_at"] is not None


def test_get_job_dates_are_iso_8601_utc_and_the_states_are_in_english(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")

    for job in world.all:
        body = get_job(client, job).json()

        instants = all_instants(body)
        assert instants, job.public_id
        for instant in instants:
            assert instant.endswith("Z"), instant
            assert datetime.fromisoformat(instant).utcoffset() == timedelta(0)
        assert body["status"] in {"queued", "running", "completed", "failed"}


def test_get_job_lists_the_pdfs_the_deliveries_and_the_syncs_of_each_attempt(
    api_client: Client, world: JobsWorld
) -> None:
    body = get_job(api_client("leitura"), world.beatriz).json()

    assert [(p["version"], p["purged_at"]) for p in body["pdfs"]] == [(2, None), (1, None)]
    assert all(p["byte_size"] > 0 and p["created_at"].endswith("Z") for p in body["pdfs"])
    assert [(d["attempt"], d["status"]) for d in body["deliveries"]] == [
        (1, "failed"),
        (2, "pending"),
    ]
    first = body["deliveries"][0]
    assert first["destination"] == "webhook" and first["attempts"] == 1
    assert set(first) == {
        "id",
        "destination",
        "status",
        "attempts",
        "attempt",
        "delivered_at",
        "scope",
        "host",
        "run_url",
    }
    assert [(s["attempt"], s["status"]) for s in body["syncs"]] == [(2, "failed"), (1, "pending")]
    sync = body["syncs"][0]
    assert (sync["integration"], sync["host"], sync["table_id"], sync["attempts"]) == (
        "baserow",
        "baserow.example.test",
        7,
        4,
    )
    synced = get_job(api_client("leitura"), world.ana).json()["syncs"][0]
    assert (synced["status"], synced["outcome"], synced["row_id"]) == ("succeeded", "created", 7)
    assert synced["synced_at"].endswith("Z")


def test_get_job_a_pdf_the_retention_removed_says_so_in_the_list(
    api_client: Client, deps: Deps, world: JobsWorld
) -> None:
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Artifact).where(Artifact.job_id == world.ana.id).values(purged_at=utcnow())
        )

    (pdf,) = get_job(api_client("leitura"), world.ana).json()["pdfs"]

    assert pdf["purged_at"] and pdf["purged_at"].endswith("Z")


def test_get_job_the_timeline_has_each_attempt_and_no_free_text_for_a_read_key(
    api_client: Client, world: JobsWorld
) -> None:
    response = get_job(api_client("leitura"), world.beatriz)

    first, second = response.json()["timeline"]
    assert (first["attempt"], first["outcome"]) == (1, "completed")
    assert event_types(first) == [
        "received",
        "plan_generated",
        "pdf_generated",
        "completed",
        "delivery_failed",
    ]
    assert (second["attempt"], second["outcome"]) == (2, "completed")
    assert event_types(second) == [
        "reprocess_requested",
        "plan_generated",
        "pdf_generated",
        "completed",
        "delivery_pending",
    ]
    reprocess = second["events"][0]
    assert reprocess["actor"] == "operadora" and reprocess["at"].endswith("Z")
    assert second["events"][1]["model"] and second["events"][1]["prompt_version"]
    assert set(reprocess) == {
        "type",
        "at",
        "actor",
        "failure_code",
        "stage",
        "delivery_id",
        "model",
        "prompt_version",
    }
    # o motivo que a operadora escreveu, o último erro da entrega e o da sincronização: nada disso
    for text in (REASON, SYNC_ERROR, "HTTP 503", "delivery_unavailable]"):
        assert text not in response.text, text
    ids_of_deliveries = {d["id"] for d in response.json()["deliveries"]}
    assert first["events"][-1]["delivery_id"] in ids_of_deliveries


def test_get_job_the_timeline_of_a_failed_order_says_the_code_and_not_the_detail(
    api_client: Client, world: JobsWorld
) -> None:
    response = get_job(api_client("leitura"), world.carla)

    (attempt,) = response.json()["timeline"]
    assert event_types(attempt) == ["received", "failed"]
    assert attempt["events"][1]["failure_code"] == "llm_unavailable"
    assert attempt["outcome"] == "failed" and FAILURE_DETAIL not in response.text


def test_get_job_asking_for_the_lead_data_with_a_read_key_is_a_403_before_the_lookup(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("leitura")

    known = get_job(client, world.ana, include_lead_data="true")
    unknown = client.get(f"{JOBS}/nao-existe", params={"include_lead_data": "true"})

    for refused in (known, unknown):  # nem a existência do pedido se sonda sem o papel
        assert refused.status_code == 403 and refused.json()["code"] == "forbidden_role"
        assert set(refused.json()) == {"code", "detail"}
        assert_no_lead_data(refused)
    assert "include_lead_data" in known.json()["detail"]


def test_get_job_a_total_key_gets_the_lead_data_only_when_it_asks(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("total")

    plain = get_job(client, world.beatriz)
    asked = get_job(client, world.beatriz, include_lead_data="true")
    refused = get_job(client, world.beatriz, include_lead_data="false")

    assert plain.json()["lead_data"] is None and refused.json()["lead_data"] is None
    assert_no_lead_data(plain)
    assert asked.status_code == 200
    assert {k: v for k, v in asked.json().items() if k != "lead_data"} == {
        k: v for k, v in plain.json().items() if k != "lead_data"
    }  # o estado é o mesmo, com ou sem o bloco


def test_get_job_the_lead_data_block_has_the_contact_the_scores_the_answers_and_the_plan(
    api_client: Client, world: JobsWorld
) -> None:
    lead = get_job(api_client("total"), world.beatriz, include_lead_data="true").json()["lead_data"]

    assert set(lead) == {
        "name",
        "email",
        "phone",
        "persona",
        "priority",
        "scores",
        "answers",
        "consent",
        "plan",
        "notes",
    }
    assert (lead["name"], lead["email"], lead["phone"]) == (*PEOPLE["beatriz"], None)
    assert lead["priority"] == "P2" and lead["scores"]
    assert lead["answers"]["nome"] == "Beatriz Lima"
    assert lead["answers"]["escritorio"] == "Escritório Exemplo"
    assert set(lead["consent"]) == {
        "privacy_accepted_at",
        "text_version",
        "marketing_email",
        "marketing_whatsapp",
    }
    assert lead["consent"]["text_version"] == "demo-v1"
    assert lead["consent"]["privacy_accepted_at"].endswith("Z")
    assert (lead["consent"]["marketing_email"], lead["consent"]["marketing_whatsapp"]) == (
        False,
        False,
    )
    assert lead["plan"]["version"] == 2  # o plano da tentativa mais recente
    assert set(lead["plan"]) == {"version", "model", "prompt_version", "created_at", "document"}
    assert lead["plan"]["document"]["titulo"]


def test_get_job_the_free_text_of_the_reasons_comes_only_inside_the_lead_data(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("total")

    beatriz = get_job(client, world.beatriz, include_lead_data="true").json()
    carla = get_job(client, world.carla, include_lead_data="true").json()

    notes = {(n["kind"], n["attempt"]): n for n in beatriz["lead_data"]["notes"]}
    assert notes[("reprocess_reason", 2)]["text"] == REASON
    assert notes[("reprocess_reason", 2)]["reference"] is None
    first_delivery = beatriz["deliveries"][0]["id"]
    assert notes[("delivery_error", 1)]["reference"] == first_delivery
    assert notes[("delivery_error", 1)]["text"].startswith("[delivery_unavailable]")
    sync_id = beatriz["syncs"][0]["id"]
    assert (notes[("sync_error", 2)]["reference"], notes[("sync_error", 2)]["text"]) == (
        sync_id,
        SYNC_ERROR,
    )
    assert [(n["kind"], n["text"]) for n in carla["lead_data"]["notes"]] == [
        ("failure_detail", FAILURE_DETAIL)
    ]
    # e não está em lugar nenhum fora do bloco
    outside = {k: v for k, v in beatriz.items() if k != "lead_data"}
    assert REASON not in json.dumps(outside) and SYNC_ERROR not in json.dumps(outside)


def test_get_job_says_the_link_of_the_lead_expired_by_the_clock_of_the_app(
    api_client: Client, world: JobsWorld
) -> None:
    clock = FakeClock()
    client = api_client("leitura", clock=clock)
    expires = datetime.fromisoformat(get_job(client, world.ana).json()["link_expires_at"])

    clock.now = (expires - timedelta(seconds=30)).timestamp()
    before = get_job(client, world.ana).json()
    clock.now = (expires + timedelta(seconds=30)).timestamp()
    after = get_job(client, world.ana).json()

    assert (before["link_expired"], after["link_expired"]) == (False, True)


def test_get_job_gives_the_link_to_the_run_only_when_the_app_has_a_template(
    api_client: Client, deps: Deps, world: JobsWorld
) -> None:
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Delivery)
            .where(Delivery.job_id == world.ana.id)
            .values(hatchet_run_id="run-d1")
        )

    without = get_job(api_client("leitura"), world.ana).json()
    templated = api_client("leitura", HATCHET_RUN_URL_TEMPLATE=RUN_URL)
    with_template = get_job(templated, world.ana).json()

    assert without["run_url"] is None and without["deliveries"][0]["run_url"] is None
    assert with_template["run_url"] == "https://hatchet.example.test/runs/run-1"
    assert with_template["deliveries"][0]["run_url"] == "https://hatchet.example.test/runs/run-d1"


def test_get_job_echoes_none_of_a_code_or_a_flag_that_is_not_valid(
    api_client: Client, world: JobsWorld
) -> None:
    """O código do pedido vai no caminho (texto livre para quem chama) e o resto da query é válido, ou
    só ele leva o sentinela: nenhuma recusa devolve o valor."""
    client = api_client("total")

    unknown = client.get(f"{JOBS}/{SENTINEL}", params={"include_lead_data": "true"})
    flag = client.get(f"{JOBS}/{world.ana.public_id}", params={"include_lead_data": SENTINEL})

    assert unknown.status_code == 404 and flag.status_code == 422
    assert [e["field"] for e in flag.json()["errors"]] == ["include_lead_data"]
    for response in (unknown, flag):
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


def page_labels(html: str) -> list[list[str]]:
    """Os rótulos dos eventos da linha do tempo da página, uma lista por tentativa."""
    block = re.search(r'<section[^>]*id="linha-do-tempo".*?</section>', html, re.DOTALL)
    assert block is not None
    lists = re.findall(r'<ol class="timeline">(.*?)</ol>', block.group(0), re.DOTALL)
    return [re.findall(r"<li>.*?</svg></span>\s*<span>([^<]*)", ol, re.DOTALL) for ol in lists]


EVENT_LABELS = {
    "received": "Pedido recebido",
    "reprocess_requested": "Reprocessamento pedido por",
    "plan_generated": "Plano gerado pela IA",
    "pdf_generated": "PDF gerado",
    "completed": "Concluído",
    "failed": "Falhou:",
    "processing": "Em processamento",
    "queued": "Na fila",
    "delivery_resend_requested": "Reenvio pedido por",
    "delivery_succeeded": "Entregue ao",
    "delivery_failed": "Entrega ao webhook falhou",
    "delivery_pending": "Entrega ao webhook pendente",
}
BADGES = {"succeeded": "Entregue", "failed": "Falhou", "pending": "Pendente"}
SYNC_BADGES = {"succeeded": "Sincronizado", "failed": "Falhou", "pending": "Pendente"}


def badges_of(html: str, section_id: str) -> list[str]:
    block = re.search(rf'<section[^>]*id="{section_id}".*?</section>', html, re.DOTALL)
    assert block is not None, section_id
    return re.findall(r'class="badge badge--[a-z]+"><svg.*?</svg>([^<]+)</span>', block.group(0))


def test_get_job_shows_the_same_state_as_the_page_for_the_same_order(
    api_client: Client, world: JobsWorld
) -> None:
    """Paridade de leitura: a página e a API contam o mesmo pedido (a linha do tempo, os PDFs, as
    entregas, as sincronizações) e, com `total` e `include_lead_data=true`, a mesma lead."""
    client = api_client("total")
    assert login(client).status_code == 303

    for job in world.all:
        html = client.get(f"{PAGE}/jobs/{job.public_id}").text
        body = get_job(client, job, include_lead_data="true").json()

        timeline = page_labels(html)
        assert len(timeline) == len(body["timeline"]), job.public_id
        for shown, attempt in zip(timeline, body["timeline"], strict=True):
            assert len(shown) == len(attempt["events"])
            for label, event in zip(shown, attempt["events"], strict=True):
                assert label.startswith(EVENT_LABELS[event["type"]]), (label, event["type"])
        assert re.findall(r"PDF da tentativa (\d+)", html) == [
            str(p["version"]) for p in body["pdfs"]
        ]
        assert badges_of(html, "entregas") == [BADGES[d["status"]] for d in body["deliveries"]]
        if body["syncs"]:
            assert badges_of(html, "sincronizacoes") == [
                SYNC_BADGES[s["status"]] for s in body["syncs"]
            ]
        else:
            assert 'id="sincronizacoes"' not in html
        lead = body["lead_data"]
        assert lead["name"] in html and lead["email"] in html
        assert f"Tentativa {body['attempt']}" in html
        for note in lead["notes"]:  # o texto livre que a página mostra, a API o põe nas notas
            assert note["text"] in html, note["kind"]


# --- download_job_pdf ---------------------------------------------------------------------------


def pdf_of(client: ApiClient, job: Any, **params: str) -> httpx.Response:
    return client.get(f"{JOBS}/{job.public_id}/pdf", params=params)


def test_download_job_pdf_is_for_the_total_role_and_needs_a_key(
    deps: Deps, api_client: Client, world: JobsWorld
) -> None:
    anonymous = make_api(deps, FakeClock()).get(f"{JOBS}/{world.ana.public_id}/pdf")
    reader = pdf_of(api_client("leitura"), world.ana)

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert set(reader.json()) == {"code", "detail"} and b"%PDF" not in reader.content
    for refused in (anonymous, reader):
        assert refused.headers["content-type"].startswith("application/json")


def test_download_job_pdf_gives_the_bytes_of_the_latest_attempt_as_an_attachment(
    api_client: Client, world: JobsWorld
) -> None:
    response = pdf_of(api_client("total"), world.beatriz)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="plano-{world.beatriz.public_id}-v2.pdf"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "location" not in response.headers


def test_download_job_pdf_version_asks_for_an_attempt_and_a_bad_one_is_refused(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("total")

    first = pdf_of(client, world.beatriz, version="1")
    second = pdf_of(client, world.beatriz, version="2")
    absent = pdf_of(client, world.beatriz, version="9")

    assert first.headers["content-disposition"].endswith('-v1.pdf"')
    assert second.headers["content-disposition"].endswith('-v2.pdf"')
    assert absent.status_code == 404 and absent.json()["code"] == "not_found"
    for bad in ("0", "-1", "dois", "1.5"):
        # um cliente (e um balde de fichas: cada chamada custa 5) para cada versão recusada
        refused = pdf_of(api_client("total"), world.beatriz, version=bad)
        assert refused.status_code == 422, bad
        assert [e["field"] for e in refused.json()["errors"]] == ["version"]


def test_download_job_pdf_never_gives_a_signed_url_even_when_the_storage_signs(
    api_client: Client, deps: Deps, world: JobsWorld
) -> None:
    asked: list[str] = []

    def presigned_url(key: str, *, ttl_seconds: int, filename: str) -> str | None:
        asked.append(key)
        return "https://armazenamento.exemplo.invalid/plano.pdf?assinatura=1"

    deps.storage.presigned_url = presigned_url  # type: ignore[method-assign]

    response = pdf_of(api_client("total"), world.ana)

    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert "location" not in response.headers and b"assinatura" not in response.content
    assert asked == []  # a assinatura nem foi pedida


def test_download_job_pdf_is_a_404_for_an_order_that_does_not_exist_or_has_no_pdf(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("total")

    unknown = client.get(f"{JOBS}/nao-existe/pdf")
    queued = pdf_of(client, world.debora)  # na fila, ainda sem PDF
    failed = pdf_of(client, world.carla)

    for missing in (unknown, queued, failed):
        assert missing.status_code == 404 and missing.json()["code"] == "not_found"
        assert missing.headers["content-type"].startswith("application/json")


def test_download_job_pdf_is_a_410_after_the_retention_removed_the_file(
    api_client: Client, deps: Deps, world: JobsWorld
) -> None:
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Artifact).where(Artifact.job_id == world.ana.id).values(purged_at=utcnow())
        )

    gone = pdf_of(api_client("total"), world.ana)

    assert gone.status_code == 410 and gone.json()["code"] == "gone"
    assert "retenção" in gone.json()["detail"]


def test_download_job_pdf_is_a_502_when_the_storage_does_not_answer_and_says_nothing_else(
    api_client: Client, deps: Deps, world: JobsWorld
) -> None:
    def broken(key: str) -> bytes:
        raise RetryableError(FailureCode.STORAGE_FAILED, f"S3 recusou {SENTINEL}")

    deps.storage.get = broken  # type: ignore[method-assign]

    response = pdf_of(api_client("total"), world.ana)

    assert response.status_code == 502 and response.json()["code"] == "upstream_failed"
    assert SENTINEL not in response.text  # o texto do provedor não sai


def test_download_job_pdf_is_a_502_too_when_the_file_is_missing_from_the_storage(
    api_client: Client, deps: Deps, world: JobsWorld
) -> None:
    def vanished(key: str) -> bytes:
        raise FileNotFoundError(key)

    deps.storage.get = vanished  # type: ignore[method-assign]

    response = pdf_of(api_client("total"), world.ana)

    assert response.status_code == 502 and response.json()["code"] == "upstream_failed"


def test_download_job_pdf_costs_five_tokens_of_the_usage_limit(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("total", API_RATE_LIMIT_PER_MINUTE="5")  # o balde tem 5 fichas

    first = pdf_of(client, world.ana)
    second = pdf_of(client, world.ana)

    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["code"] == "rate_limited"
    assert int(second.headers["retry-after"]) >= 1


def test_download_job_pdf_echoes_none_of_a_code_or_a_version_that_is_not_valid(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("total")

    unknown = client.get(f"{JOBS}/{SENTINEL}/pdf", params={"version": "1"})
    bad_version = pdf_of(client, world.ana, version=SENTINEL)

    assert unknown.status_code == 404 and bad_version.status_code == 422
    for response in (unknown, bad_version):
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


def test_download_job_pdf_gives_the_same_file_and_the_same_refusals_as_the_page(
    api_client: Client, deps: Deps, world: JobsWorld
) -> None:
    """Paridade de leitura: a página (que, no storage local dos testes, também entrega os bytes) e a
    API devolvem o mesmo arquivo, o mesmo nome e as mesmas recusas para os mesmos dados."""
    client = api_client("total")
    assert login(client).status_code == 303
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Artifact).where(Artifact.job_id == world.ana.id).values(purged_at=utcnow())
        )

    for job, params in (
        (world.beatriz, {}),
        (world.beatriz, {"version": "1"}),
        (world.beatriz, {"version": "9"}),
        (world.ana, {}),  # a retenção apagou
        (world.debora, {}),  # sem PDF
    ):
        on_the_page = client.get(f"{PAGE}/jobs/{job.public_id}/pdf", params=params)
        on_the_api = pdf_of(client, job, **params)

        assert on_the_api.status_code == on_the_page.status_code, (job.public_id, params)
        if on_the_api.status_code == 200:
            assert on_the_api.content == on_the_page.content
            assert (
                on_the_api.headers["content-disposition"]
                == (on_the_page.headers["content-disposition"])
            )


def test_the_three_order_reads_carry_x_lead_data_in_the_file_and_in_the_matrix() -> None:
    document = json.loads((ROOT / OPENAPI_FILE).read_text(encoding="utf-8"))
    in_the_file = {
        operation["operationId"]: operation
        for _, _, operation in operations_of(document)
        if operation["operationId"] in ORDER_READS
    }
    in_the_matrix = {
        row.operation_id: row for row in read_matrix().rows if row.operation_id in ORDER_READS
    }

    assert set(in_the_file) == set(in_the_matrix) == set(ORDER_READS)
    for operation_id, role in ORDER_READS.items():
        assert in_the_file[operation_id]["x-lead-data"] is True, operation_id
        assert in_the_file[operation_id]["x-required-role"] == role
        assert "x-lead-data" in in_the_matrix[operation_id].marks, operation_id
        assert in_the_matrix[operation_id].role == role
