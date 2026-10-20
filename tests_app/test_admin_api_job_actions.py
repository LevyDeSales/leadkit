"""As ações sobre os pedidos (`/admin/api/v1`): `reprocess_job`, `resend_job_delivery` e
`resend_job_sync`, pela costura HTTP.

Elas agem pelas mesmas ações que as páginas (`app.admin.actions.jobs`): a regra do motivo, da
confirmação, da tentativa esperada e o disparo no Hatchet são uma só, e a paridade com a página (o mesmo
estado do banco, a mesma auditoria e o mesmo disparo, menos o autor) está no fim de cada operação. O
Hatchet de verdade fica fora: o disparador da tentativa é o falso do `api_client` (`triggered`) e o
`launch_delivery` e o `launch_sync` do `app.workflows` são trocados por registros. **O disparo que não
chega ao engine não é erro:** a resposta é `200` com `launched: false`, e o reconciliador dispara. As
chaves destes testes são sorteadas na execução.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_matrix import OPENAPI_FILE, ROOT, operations_of
from admin_api_matrix import read as read_matrix
from admin_api_support import (
    ORIGIN,
    ApiClient,
    admin_api,
    assert_same_effect,
    make_api,
    url_of,
)
from jobs_world import PEOPLE, JobsWorld, build_jobs_world
from test_admin import FakeClock, csrf_token, login
from test_admin_actions_reprocess import (
    GOOD_REASON,
    attempt_of,
    audits,
    failed_ids,
    launches,
    reprocess_audits_of,
)

from app import jobs, workflows
from app.deps import Deps
from app.integrations import syncs
from app.models import Delivery, IntegrationSync

PAGE = "/admin"
SENTINEL = "ZZ_SENT_" + secrets.token_hex(8).upper()
# O que a chamada da API muda e a página não (o último uso da chave que chama) e o que a ação carimba
# com o relógio da máquina, que difere de uma execução para a outra.
IGNORE = {"api_keys": {"last_used_at"}, "*": {"created_at", "updated_at"}}
ACTIONS = {"reprocess_job", "resend_job_delivery", "resend_job_sync"}

Client = Callable[..., ApiClient]
Triggered = list[tuple[uuid.UUID, int]]


@pytest.fixture
def world(deps: Deps) -> JobsWorld:
    world = build_jobs_world(deps)
    # O reenvio deste cenário é da tentativa atual; o histórico não autoriza outro PDF.
    from app.models import Artifact, utcnow

    with deps.sessions.begin() as session:
        rows = session.execute(
            sa.select(Delivery, Artifact.version)
            .join(Artifact, Artifact.id == Delivery.artifact_id)
            .where(Delivery.job_id == world.beatriz.id)
        ).all()
        for row, attempt in rows:
            row.status = "failed" if attempt == 2 else "succeeded"
            if row.status == "succeeded":
                row.delivered_at, row.last_error = utcnow(), None
    return world


@pytest.fixture
def triggered() -> Triggered:
    return []


def fields_of(response: httpx.Response) -> set[str]:
    """Os campos que um `422 validation_failed` aponta."""
    return {error["field"] for error in response.json()["errors"]}


# --- reprocess_job ------------------------------------------------------------------------------


def reprocess_body(**changes: Any) -> dict[str, Any]:
    return {"reason": GOOD_REASON, "expected_attempt": 1, "confirm": True, **changes}


def reprocess(client: ApiClient, job: Any, **changes: Any) -> httpx.Response:
    return client.post(
        url_of("reprocess_job", public_id=job.public_id), json=reprocess_body(**changes)
    )


def test_reprocess_job_needs_a_key_and_a_read_key_is_refused_and_nothing_happens(
    deps: Deps, api_client: Client, world: JobsWorld, triggered: Triggered
) -> None:
    url = url_of("reprocess_job", public_id=world.carla.public_id)
    anonymous = make_api(deps, FakeClock(), triggered=triggered).post(url, json=reprocess_body())
    reader = api_client("leitura", triggered=triggered).post(url, json=reprocess_body())

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert anonymous.headers["www-authenticate"] == "Bearer"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert triggered == [] and attempt_of(deps, world.carla.id) == 1
    assert reprocess_audits_of(deps, world.carla.id) == []


def test_reprocess_job_creates_the_next_attempt_and_answers_with_the_new_state(
    deps: Deps, api_client: Client, world: JobsWorld, triggered: Triggered
) -> None:
    client = api_client("total", triggered=triggered)

    response = client.post(
        url_of("reprocess_job", public_id=world.carla.public_id), json=reprocess_body()
    )

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["public_id"], body["attempt"], body["launched"]) == (
        world.carla.public_id,
        2,
        True,
    )
    assert (body["status"], body["stage"], body["failure_code"]) == ("queued", "queued", None)
    assert body["form_slug"] == "demo" and body["created_at"].endswith("Z")
    assert "lead_data" not in body  # a ação devolve o estado, nunca o dado da lead
    asked = [event for attempt in body["timeline"] for event in attempt["events"]]
    assert {"type": "reprocess_requested", "actor": client.actor}.items() <= next(
        event for event in asked if event["type"] == "reprocess_requested"
    ).items()
    assert triggered == [(world.carla.id, 2)]  # o Hatchet recebeu o disparo, uma vez
    assert attempt_of(deps, world.carla.id) == 2
    (entry,) = reprocess_audits_of(deps, world.carla.id)
    assert entry.actor == client.actor and entry.actor.startswith("chave:")
    assert entry.details == {
        "reason": GOOD_REASON,
        "attempt": 2,
        "previous_status": "failed",
        "failure_code": "llm_unavailable",
    }
    assert GOOD_REASON not in response.text  # o motivo é texto livre: nunca volta na resposta


def test_reprocess_job_the_new_attempt_shows_in_get_job_with_the_same_state(
    api_client: Client, world: JobsWorld
) -> None:
    client = api_client("total")

    reprocessed = reprocess(client, world.carla).json()
    read = client.get(url_of("get_job", public_id=world.carla.public_id)).json()

    reprocessed.pop("launched")
    assert read.pop("lead_data") is None
    assert reprocessed == read


def test_reprocess_job_an_engine_that_did_not_receive_it_is_a_200_with_launched_false(
    deps: Deps, api_client: Client, world: JobsWorld, triggered: Triggered
) -> None:
    client = api_client("total", triggered=triggered)

    async def engine_down(job_id: uuid.UUID, attempt: int) -> str | None:
        triggered.append((job_id, attempt))
        return None  # o disparador do app engole a falha e devolve None

    admin_api(client).state.trigger = engine_down

    response = reprocess(client, world.carla)

    assert response.status_code == 200 and response.json()["launched"] is False
    assert response.json()["attempt"] == 2 and response.json()["status"] == "queued"
    assert attempt_of(deps, world.carla.id) == 2  # a tentativa existe
    with deps.sessions.begin() as session:  # e o reconciliador a encontra sem run id
        unlaunched = jobs.unlaunched_jobs(session, older_than=timedelta(0), limit=10)
    assert world.carla.id in [job.id for job in unlaunched]
    assert len(reprocess_audits_of(deps, world.carla.id)) == 1


@pytest.mark.parametrize("sent", [{}, {"confirm": False}], ids=["absent", "false"])
def test_reprocess_job_without_the_confirmation_is_a_422_and_nothing_happens(
    deps: Deps, api_client: Client, world: JobsWorld, triggered: Triggered, sent: dict[str, Any]
) -> None:
    client = api_client("total", triggered=triggered)
    body = {"reason": GOOD_REASON, "expected_attempt": 1, **sent}

    response = client.post(url_of("reprocess_job", public_id=world.carla.public_id), json=body)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "errors" not in response.json()
    assert triggered == [] and attempt_of(deps, world.carla.id) == 1
    assert reprocess_audits_of(deps, world.carla.id) == []  # nem auditoria, nem tentativa nova


@pytest.mark.parametrize("size", [10, 300])
def test_reprocess_job_accepts_a_reason_at_the_limits_after_trimming(
    deps: Deps, api_client: Client, world: JobsWorld, size: int
) -> None:
    response = reprocess(api_client("total"), world.carla, reason=f"  {'m' * size}\n")

    assert response.status_code == 200
    (entry,) = reprocess_audits_of(deps, world.carla.id)
    assert entry.details["reason"] == "m" * size


@pytest.mark.parametrize(
    "reason", ["", "         ", "m" * 9, f"  {'m' * 9}  ", "m" * 301], ids=lambda r: f"len{len(r)}"
)
def test_reprocess_job_a_reason_out_of_the_limits_is_a_422_on_the_field_and_nothing_happens(
    deps: Deps, api_client: Client, world: JobsWorld, triggered: Triggered, reason: str
) -> None:
    client = api_client("total", triggered=triggered)

    response = reprocess(client, world.carla, reason=reason)

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    (error,) = response.json()["errors"]
    assert error["field"] == "reason" and error["message"]
    assert reason.strip() not in response.text or reason.strip() == ""
    assert triggered == [] and attempt_of(deps, world.carla.id) == 1


def test_reprocess_job_the_reason_and_the_attempt_are_required_and_nothing_else_is_accepted(
    deps: Deps, api_client: Client, world: JobsWorld
) -> None:
    client = api_client("total")
    url = url_of("reprocess_job", public_id=world.carla.public_id)

    no_reason = client.post(url, json={"expected_attempt": 1, "confirm": True})
    no_attempt = client.post(url, json={"reason": GOOD_REASON, "confirm": True})
    zero = reprocess(client, world.carla, expected_attempt=0)
    text = reprocess(client, world.carla, expected_attempt="uma")
    extra = reprocess(client, world.carla, motivo="outro nome de campo")

    assert all(r.status_code == 422 for r in (no_reason, no_attempt, zero, text, extra))
    assert fields_of(no_reason) == {"reason"} and fields_of(no_attempt) == {"expected_attempt"}
    assert fields_of(zero) == fields_of(text) == {"expected_attempt"}
    assert fields_of(extra) == {"motivo"}
    assert attempt_of(deps, world.carla.id) == 1


def test_reprocess_job_a_wrong_attempt_is_a_409_state_conflict_and_creates_no_attempt(
    deps: Deps, api_client: Client, world: JobsWorld, triggered: Triggered
) -> None:
    client = api_client("total", triggered=triggered)

    response = reprocess(client, world.carla, expected_attempt=5)

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "current_version" not in response.json()  # o conflito é de estado, não de versão
    assert triggered == [] and attempt_of(deps, world.carla.id) == 1
    assert reprocess_audits_of(deps, world.carla.id) == []


def test_reprocess_job_repeating_the_call_after_the_response_was_lost_is_a_409_without_a_second(
    deps: Deps, api_client: Client, world: JobsWorld, triggered: Triggered
) -> None:
    client = api_client("total", triggered=triggered)
    url = url_of("reprocess_job", public_id=world.carla.public_id)

    first = client.post(url, json=reprocess_body())
    again = client.post(url, json=reprocess_body())  # o cliente não viu a resposta e repete

    assert (first.status_code, again.status_code) == (200, 409)
    assert again.json()["code"] == "state_conflict"
    assert triggered == [(world.carla.id, 2)]  # uma tentativa, um disparo
    assert attempt_of(deps, world.carla.id) == 2
    assert len(reprocess_audits_of(deps, world.carla.id)) == 1


@pytest.mark.parametrize("name", ["debora", "elisa"])
def test_reprocess_job_an_order_in_the_queue_or_being_generated_is_a_409(
    deps: Deps, api_client: Client, world: JobsWorld, triggered: Triggered, name: str
) -> None:
    job = getattr(world, name)

    response = reprocess(api_client("total", triggered=triggered), job)

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert triggered == [] and attempt_of(deps, job.id) == 1


def test_reprocess_job_an_order_that_does_not_exist_is_a_404(
    api_client: Client, world: JobsWorld, triggered: Triggered
) -> None:
    client = api_client("total", triggered=triggered)

    response = client.post(url_of("reprocess_job", public_id="nao-existe"), json=reprocess_body())

    assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert triggered == []


def test_reprocess_job_echoes_none_of_a_free_text_that_is_otherwise_valid(
    deps: Deps, api_client: Client, world: JobsWorld
) -> None:
    """`reason` é texto livre. O teste de eco geral para no `422` do Pydantic antes de o serviço rodar
    (o `expected_attempt` do sentinela não é um número); aqui os outros campos são válidos e só o
    motivo leva o sentinela, para ele chegar à ação, à auditoria e à resposta."""
    client = api_client("total")
    url = url_of("reprocess_job", public_id=world.carla.public_id)

    accepted = client.post(url, json=reprocess_body(reason=SENTINEL))
    stale = client.post(url, json=reprocess_body(reason=SENTINEL))  # a tentativa 1 já passou
    too_long = client.post(
        url_of("reprocess_job", public_id=world.beatriz.public_id),
        json=reprocess_body(reason=SENTINEL + "m" * 300, expected_attempt=2),
    )
    unknown = client.post(
        url_of("reprocess_job", public_id="nao-existe"), json=reprocess_body(reason=SENTINEL)
    )

    assert [r.status_code for r in (accepted, stale, too_long, unknown)] == [200, 409, 422, 404]
    for response in (accepted, stale, too_long, unknown):
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)
    (entry,) = reprocess_audits_of(deps, world.carla.id)
    assert entry.details["reason"] == SENTINEL  # o texto livre fica na auditoria, e só nela


def test_reprocess_job_the_launch_carries_only_ids_and_no_log_has_the_reason(
    api_client: Client,
    world: JobsWorld,
    triggered: Triggered,
    caplog: pytest.LogCaptureFixture,
) -> None:
    name, email = PEOPLE["carla"]
    reason = f"{name} ({email}) pediu o plano de novo"
    client = api_client("total", triggered=triggered)

    with caplog.at_level(logging.DEBUG):
        response = reprocess(client, world.carla, reason=reason)

    assert response.status_code == 200
    # o que vai ao Hatchet é o id do pedido e o número da tentativa, e nada mais
    assert [(type(job_id), type(attempt)) for job_id, attempt in triggered] == [(uuid.UUID, int)]
    for text in (name, email, reason, GOOD_REASON):
        assert text not in caplog.text
        assert text not in response.text


def page_reprocess(
    client: ApiClient, job: Any, triggered: Triggered, seen: list[Triggered], **form: str
) -> Callable[[], httpx.Response]:
    def call() -> httpx.Response:
        assert login(client).status_code == 303
        response = client.post(
            f"{PAGE}/jobs/{job.public_id}/reprocess",
            data={
                "csrf_token": csrf_token(client),
                "attempt": "1",
                "reason": GOOD_REASON,
                "confirm": "on",
                **form,
            },
            headers=ORIGIN,
        )
        seen.append(list(triggered))
        triggered.clear()
        return response

    return call


def api_reprocess(
    client: ApiClient, job: Any, triggered: Triggered, seen: list[Triggered], **changes: Any
) -> Callable[[], httpx.Response]:
    def call() -> httpx.Response:
        response = client.post(
            url_of("reprocess_job", public_id=job.public_id), json=reprocess_body(**changes)
        )
        seen.append(list(triggered))
        triggered.clear()
        return response

    return call


def test_reprocess_job_leaves_the_same_state_audit_and_launch_as_the_page(
    api_client: Client, world: JobsWorld, triggered: Triggered
) -> None:
    client = api_client("total", triggered=triggered)
    seen: list[Triggered] = []

    page, api = assert_same_effect(
        page_reprocess(client, world.carla, triggered, seen),
        api_reprocess(client, world.carla, triggered, seen),
        api_actor=client.actor,
        ignore=IGNORE,
    )

    assert (page.status_code, api.status_code) == (303, 200)
    assert seen == [[(world.carla.id, 2)], [(world.carla.id, 2)]]  # o mesmo disparo, nos dois


@pytest.mark.parametrize(
    ("page_form", "api_changes", "statuses"),
    [
        ({"attempt": "7"}, {"expected_attempt": 7}, (303, 409)),
        ({"reason": "curto"}, {"reason": "curto"}, (422, 422)),
        ({"confirm": ""}, {"confirm": False}, (422, 422)),
    ],
    ids=["wrong_attempt", "short_reason", "no_confirmation"],
)
def test_reprocess_job_refuses_what_the_page_refuses_and_changes_nothing_on_either(
    api_client: Client,
    world: JobsWorld,
    triggered: Triggered,
    page_form: dict[str, str],
    api_changes: dict[str, Any],
    statuses: tuple[int, int],
) -> None:
    client = api_client("total", triggered=triggered)
    seen: list[Triggered] = []

    page, api = assert_same_effect(
        page_reprocess(client, world.carla, triggered, seen, **page_form),
        api_reprocess(client, world.carla, triggered, seen, **api_changes),
        api_actor=client.actor,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert (page.status_code, api.status_code) == statuses
    assert seen == [[], []]  # nenhum dos dois disparou nada


# --- resend_job_delivery e resend_job_sync ------------------------------------------------------


class Resend:
    """As duas operações de reenvio, que só diferem no que reenviam."""

    def __init__(
        self, operation_id: str, kind: str, model: type[Delivery | IntegrationSync]
    ) -> None:
        self.operation_id, self.kind, self.model = operation_id, kind, model
        self.id_name = "delivery_id" if kind == "delivery" else "sync_id"
        self.audit = "delivery.resend" if kind == "delivery" else "sync.resend"
        self.collection = "deliveries" if kind == "delivery" else "syncs"
        self.page_part = "deliveries" if kind == "delivery" else "syncs"

    def url(self, public_id: str, target: Any) -> str:
        return url_of(self.operation_id, public_id=public_id, **{self.id_name: str(target)})

    def post(self, client: ApiClient, public_id: str, target: Any, **body: Any) -> httpx.Response:
        versions = (
            {"expected_config_version": 1, "expected_attempt": 2} if self.kind == "delivery" else {}
        )
        return client.post(self.url(public_id, target), json={"confirm": True, **versions, **body})

    def page_url(self, public_id: str, target: Any) -> str:
        return f"{PAGE}/jobs/{public_id}/{self.page_part}/{target}/resend"


DELIVERY = Resend("resend_job_delivery", "delivery", Delivery)
SYNC = Resend("resend_job_sync", "sync", IntegrationSync)
RESENDS = pytest.mark.parametrize("resend", [DELIVERY, SYNC], ids=lambda r: r.kind)


def failed_of(deps: Deps, resend: Resend) -> uuid.UUID:
    (target,) = failed_ids(deps, resend.model)  # é do pedido da beatriz
    return target


def succeeded_of(deps: Deps, resend: Resend, world: JobsWorld) -> uuid.UUID:
    """A entrega ou a sincronização da ana, que saiu bem: não pode ser reenviada."""
    with deps.sessions() as session:
        found = session.scalar(
            sa.select(resend.model.id).where(
                resend.model.job_id == world.ana.id, resend.model.status == "succeeded"
            )
        )
    assert found is not None
    return found


@RESENDS
def test_resend_needs_a_key_and_a_read_key_is_refused_and_nothing_happens(
    deps: Deps,
    api_client: Client,
    world: JobsWorld,
    monkeypatch: pytest.MonkeyPatch,
    resend: Resend,
) -> None:
    calls = launches(monkeypatch, resend.kind)
    target = failed_of(deps, resend)
    url = resend.url(world.beatriz.public_id, target)

    anonymous = make_api(deps, FakeClock()).post(url, json={"confirm": True})
    reader = api_client("leitura").post(url, json={"confirm": True})

    assert anonymous.status_code == 401 and anonymous.json()["code"] == "invalid_credentials"
    assert reader.status_code == 403 and reader.json()["code"] == "forbidden_role"
    assert calls == [] and failed_ids(deps, resend.model) == [target]
    assert audits(deps, resend.audit) == []


def test_resend_job_delivery_returns_the_failed_delivery_to_pending_and_launches_it(
    deps: Deps, api_client: Client, world: JobsWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = launches(monkeypatch, "delivery")
    delivery_id = failed_of(deps, DELIVERY)
    client = api_client("total")

    response = client.post(
        url_of(
            "resend_job_delivery",
            public_id=world.beatriz.public_id,
            delivery_id=str(delivery_id),
        ),
        json={"confirm": True, "expected_config_version": 1, "expected_attempt": 2},
    )

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["id"], body["status"], body["launched"]) == (str(delivery_id), "pending", True)
    assert body["destination"] == "webhook" and body["attempt"] == 2
    assert calls == [delivery_id]  # o Hatchet recebeu o disparo, uma vez
    assert failed_ids(deps, Delivery) == []
    (entry,) = audits(deps, "delivery.resend")
    assert (entry.actor, entry.target_id) == (client.actor, str(delivery_id))
    assert entry.details == {
        "job_id": str(world.beatriz.id),
        "audience": "internal",
        "config_version": 1,
        "dispatch_no": 2,
    }
    state = client.get(url_of("get_job", public_id=world.beatriz.public_id)).json()
    shown = next(item for item in state["deliveries"] if item["id"] == str(delivery_id))
    assert shown["status"] == "pending"


def test_resend_job_sync_returns_the_failed_sync_to_pending_and_launches_it(
    deps: Deps, api_client: Client, world: JobsWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = launches(monkeypatch, "sync")
    sync_id = failed_of(deps, SYNC)
    client = api_client("total")

    response = client.post(
        url_of("resend_job_sync", public_id=world.beatriz.public_id, sync_id=str(sync_id)),
        json={"confirm": True},
    )

    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["id"], body["status"], body["launched"]) == (str(sync_id), "pending", True)
    assert body["integration"] == "baserow" and body["table_id"] == 7
    assert calls == [sync_id]
    assert failed_ids(deps, IntegrationSync) == []
    (entry,) = audits(deps, "sync.resend")
    assert (entry.actor, entry.target_id) == (client.actor, str(sync_id))
    state = client.get(url_of("get_job", public_id=world.beatriz.public_id)).json()
    shown = next(item for item in state["syncs"] if item["id"] == str(sync_id))
    assert shown["status"] == "pending"


@RESENDS
def test_resend_an_engine_that_did_not_receive_it_is_a_200_with_launched_false(
    deps: Deps,
    api_client: Client,
    world: JobsWorld,
    monkeypatch: pytest.MonkeyPatch,
    resend: Resend,
) -> None:
    launches(monkeypatch, resend.kind, run_id=None)  # o SDK devolve None sem engine
    target = failed_of(deps, resend)

    response = resend.post(api_client("total"), world.beatriz.public_id, target)

    assert response.status_code == 200
    assert (response.json()["status"], response.json()["launched"]) == ("pending", False)
    assert failed_ids(deps, resend.model) == []  # voltou para pendente: o reconciliador a acha
    with deps.sessions.begin() as session:
        find = jobs.unlaunched_deliveries if resend.kind == "delivery" else syncs.unlaunched
        found = find(session, older_than=timedelta(0), limit=10)
        assert target in [row.id for row in found]
    assert len(audits(deps, resend.audit)) == 1


@RESENDS
@pytest.mark.parametrize("sent", [{}, {"confirm": False}], ids=["absent", "false"])
def test_resend_without_the_confirmation_is_a_422_and_nothing_happens(
    deps: Deps,
    api_client: Client,
    world: JobsWorld,
    monkeypatch: pytest.MonkeyPatch,
    resend: Resend,
    sent: dict[str, Any],
) -> None:
    calls = launches(monkeypatch, resend.kind)
    target = failed_of(deps, resend)

    response = api_client("total").post(resend.url(world.beatriz.public_id, target), json=sent)

    assert response.status_code == 422 and response.json()["code"] == "confirmation_required"
    assert "errors" not in response.json()
    assert calls == [] and failed_ids(deps, resend.model) == [target]
    assert audits(deps, resend.audit) == []  # nem estado novo, nem auditoria, nem disparo


@RESENDS
def test_resend_repeating_the_call_after_the_response_was_lost_is_a_409_without_a_second_send(
    deps: Deps,
    api_client: Client,
    world: JobsWorld,
    monkeypatch: pytest.MonkeyPatch,
    resend: Resend,
) -> None:
    calls = launches(monkeypatch, resend.kind)
    target = failed_of(deps, resend)
    client = api_client("total")

    first = resend.post(client, world.beatriz.public_id, target)
    again = resend.post(client, world.beatriz.public_id, target)  # o cliente repete

    assert (first.status_code, again.status_code) == (200, 409)
    assert again.json()["code"] == "state_conflict"
    assert calls == [target] and len(audits(deps, resend.audit)) == 1


@RESENDS
def test_resend_of_something_that_did_not_fail_is_a_409_state_conflict(
    deps: Deps,
    api_client: Client,
    world: JobsWorld,
    monkeypatch: pytest.MonkeyPatch,
    resend: Resend,
) -> None:
    calls = launches(monkeypatch, resend.kind)
    done = succeeded_of(deps, resend, world)

    response = resend.post(api_client("total"), world.ana.public_id, done)

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert calls == [] and audits(deps, resend.audit) == []


@RESENDS
def test_resend_of_a_missing_order_or_a_row_that_is_not_of_that_order_is_a_404(
    deps: Deps,
    api_client: Client,
    world: JobsWorld,
    monkeypatch: pytest.MonkeyPatch,
    resend: Resend,
) -> None:
    calls = launches(monkeypatch, resend.kind)
    target = failed_of(deps, resend)
    client = api_client("total")

    no_order = resend.post(client, "nao-existe", target)
    of_another = resend.post(client, world.carla.public_id, target)  # é da beatriz
    unknown = resend.post(client, world.beatriz.public_id, uuid.uuid4())

    for response in (no_order, of_another, unknown):
        assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert calls == [] and failed_ids(deps, resend.model) == [target]


@RESENDS
def test_resend_an_id_that_is_not_a_uuid_is_a_422_on_the_path_field(
    api_client: Client, world: JobsWorld, resend: Resend
) -> None:
    response = resend.post(api_client("total"), world.beatriz.public_id, "nao-e-um-uuid")

    assert response.status_code == 422 and response.json()["code"] == "validation_failed"
    assert [e["field"] for e in response.json()["errors"]] == [resend.id_name]


@RESENDS
def test_resend_the_body_takes_only_the_confirmation(
    deps: Deps,
    api_client: Client,
    world: JobsWorld,
    monkeypatch: pytest.MonkeyPatch,
    resend: Resend,
) -> None:
    calls = launches(monkeypatch, resend.kind)
    target = failed_of(deps, resend)

    response = resend.post(api_client("total"), world.beatriz.public_id, target, motivo="x")

    assert response.status_code == 422
    assert [e["field"] for e in response.json()["errors"]] == ["motivo"]
    assert calls == [] and failed_ids(deps, resend.model) == [target]


@RESENDS
def test_resend_echoes_none_of_a_value_that_is_refused(
    deps: Deps,
    api_client: Client,
    world: JobsWorld,
    monkeypatch: pytest.MonkeyPatch,
    resend: Resend,
) -> None:
    """O código do pedido é texto livre no caminho: com um UUID válido ele chega à busca, e com o
    sentinela no lugar do UUID a validação o recusa. Nenhuma das duas respostas o devolve."""
    launches(monkeypatch, resend.kind)
    client = api_client("total")

    to_the_lookup = resend.post(client, SENTINEL, uuid.uuid4())
    to_the_validation = resend.post(client, world.beatriz.public_id, SENTINEL)

    assert (to_the_lookup.status_code, to_the_validation.status_code) == (404, 422)
    for response in (to_the_lookup, to_the_validation):
        assert SENTINEL not in response.text and SENTINEL not in str(response.headers)


@pytest.mark.parametrize(
    ("resend", "stuck_launch"), [(DELIVERY, "delivery"), (SYNC, "sync")], ids=["delivery", "sync"]
)
def test_resend_waits_no_longer_than_the_trigger_timeout_and_the_launch_goes_on_alone(
    deps: Deps,
    api_client: Client,
    world: JobsWorld,
    monkeypatch: pytest.MonkeyPatch,
    resend: Resend,
    stuck_launch: str,
) -> None:
    released, finished = threading.Event(), threading.Event()

    def stuck(target: uuid.UUID) -> str | None:
        released.wait(timeout=10)  # o SDK insiste por dezenas de segundos sem engine
        finished.set()
        return None

    monkeypatch.setattr(workflows, f"launch_{stuck_launch}", stuck)
    target = failed_of(deps, resend)
    client = api_client("total", HATCHET_TRIGGER_TIMEOUT_SECONDS="0.2")

    started = time.monotonic()
    response = resend.post(client, world.beatriz.public_id, target)
    elapsed = time.monotonic() - started
    assert not finished.is_set()
    released.set()

    assert response.status_code == 200 and elapsed < 5
    assert (response.json()["status"], response.json()["launched"]) == ("pending", False)
    assert finished.wait(timeout=5)  # o disparo que já tinha começado seguiu em segundo plano


def test_resend_nothing_that_goes_to_hatchet_or_to_the_log_is_more_than_an_id(
    deps: Deps,
    api_client: Client,
    world: JobsWorld,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent: list[Any] = []
    monkeypatch.setattr(workflows, "launch_delivery", lambda target: sent.append(target) or "run")
    monkeypatch.setattr(workflows, "launch_sync", lambda target: sent.append(target) or "run")
    client = api_client("total")

    with caplog.at_level(logging.DEBUG):
        for resend in (DELIVERY, SYNC):
            sent_ok = resend.post(client, world.beatriz.public_id, failed_of(deps, resend))
            assert sent_ok.status_code == 200

    assert [type(item) for item in sent] == [uuid.UUID, uuid.UUID]
    for name, email in PEOPLE.values():
        assert name not in caplog.text and email not in caplog.text


def page_resend(
    client: ApiClient, resend: Resend, public_id: str, target: Any
) -> Callable[[], httpx.Response]:
    def call() -> httpx.Response:
        assert login(client).status_code == 303
        return client.post(
            resend.page_url(public_id, target),
            data={
                "csrf_token": csrf_token(client),
                **(
                    {"expected_config_version": 1, "expected_attempt": 2}
                    if resend.kind == "delivery"
                    else {}
                ),
            },
            headers=ORIGIN,
        )

    return call


@RESENDS
def test_resend_job_leaves_the_same_state_audit_and_launch_as_the_page(
    deps: Deps,
    api_client: Client,
    world: JobsWorld,
    monkeypatch: pytest.MonkeyPatch,
    resend: Resend,
) -> None:
    calls = launches(monkeypatch, resend.kind)
    target = failed_of(deps, resend)
    client = api_client("total")
    seen: list[list[uuid.UUID]] = []

    def by_page() -> httpx.Response:
        response = page_resend(client, resend, world.beatriz.public_id, target)()
        seen.append(list(calls))
        calls.clear()
        return response

    def by_api() -> httpx.Response:
        response = resend.post(client, world.beatriz.public_id, target)
        seen.append(list(calls))
        calls.clear()
        return response

    page, api = assert_same_effect(by_page, by_api, api_actor=client.actor, ignore=IGNORE)

    assert (page.status_code, api.status_code) == (303, 200)
    assert seen == [[target], [target]]  # o mesmo disparo, nos dois
    assert api.json()["launched"] is True


@RESENDS
def test_resend_job_refuses_what_the_page_refuses_and_changes_nothing_on_either(
    deps: Deps,
    api_client: Client,
    world: JobsWorld,
    monkeypatch: pytest.MonkeyPatch,
    resend: Resend,
) -> None:
    calls = launches(monkeypatch, resend.kind)
    done = succeeded_of(deps, resend, world)
    client = api_client("total")

    page, api = assert_same_effect(
        page_resend(client, resend, world.ana.public_id, done),
        lambda: resend.post(client, world.ana.public_id, done),
        api_actor=client.actor,
        ignore=IGNORE,
        expect_no_change=True,
    )

    assert (page.status_code, api.status_code) == (303, 409)  # a página avisa na tela; a API, 409
    assert calls == []


# --- o contrato ---------------------------------------------------------------------------------


def test_the_three_order_actions_are_total_external_effect_without_lead_data_in_the_file() -> None:
    document = json.loads((ROOT / OPENAPI_FILE).read_text(encoding="utf-8"))
    in_the_file = {
        operation["operationId"]: (method, path, operation)
        for method, path, operation in operations_of(document)
        if operation["operationId"] in ACTIONS
    }
    in_the_matrix = {
        row.operation_id: row for row in read_matrix().rows if row.operation_id in ACTIONS
    }

    assert set(in_the_file) == set(in_the_matrix) == set(ACTIONS)
    for operation_id, (method, _, operation) in in_the_file.items():
        assert method == "POST"
        assert operation["x-required-role"] == "total"
        assert operation["x-external-effect"] is True
        assert "x-destructive" not in operation and "x-lead-data" not in operation
        assert in_the_matrix[operation_id].marks == {"x-external-effect"}
        assert in_the_matrix[operation_id].role == "total"
        assert in_the_matrix[operation_id].status == "feita"
        assert {"401", "403", "404", "409", "422", "429"} <= set(operation["responses"])
        assert "502" not in operation["responses"]  # o disparo que não chega não é erro
