"""O reprocessamento e os reenvios do admin (`app.admin.actions.jobs`): `reprocess`, `resend_delivery` e
`resend_sync`, antes de a página e a API traduzirem o resultado em tela ou em JSON.

A regra morava nas rotas HTML: o motivo de 10 a 300 caracteres, a confirmação, a tentativa que a pessoa
viu e o disparo no Hatchet, com a diferença entre "enfileirada" e "adiada para o reconciliador". Aqui ela
é provada sem HTTP; o Hatchet fica fora: o disparador da tentativa é um falso assíncrono e os
`launch_delivery` e `launch_sync` do `app.workflows` (a fronteira) são trocados por registros.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from jobs_world import build_jobs_world

from app import jobs, workflows
from app.admin.actions import jobs as actions
from app.deps import Deps
from app.models import AuditLog, Delivery, IntegrationSync, Job

ACTOR = "operadora"
GOOD_REASON = "Prompt corrigido pela equipe"


class FakeTrigger:
    """O `app.state.trigger` de mentira: grava o que lhe pedem e devolve o run id (ou `None`, como o
    do app quando o engine não recebe)."""

    def __init__(self, run_id: str | None = "run-fake") -> None:
        self.run_id = run_id
        self.calls: list[tuple[uuid.UUID, int]] = []

    async def __call__(self, job_id: uuid.UUID, attempt: int) -> str | None:
        self.calls.append((job_id, attempt))
        return self.run_id


def reprocess(
    deps: Deps,
    trigger: FakeTrigger,
    public_id: str,
    *,
    reason: str = GOOD_REASON,
    confirm: bool = True,
    expected_attempt: int | None = 1,
) -> actions.Reprocessed:
    return asyncio.run(
        actions.reprocess(
            deps,
            trigger,
            public_id,
            actor=ACTOR,
            reason=reason,
            confirm=confirm,
            expected_attempt=expected_attempt,
        )
    )


def attempt_of(deps: Deps, job_id: uuid.UUID) -> int:
    with deps.sessions() as session:
        return session.scalar(sa.select(Job.attempt).where(Job.id == job_id)) or 0


def audits(deps: Deps, action: str) -> list[AuditLog]:
    with deps.sessions() as session:
        return list(session.scalars(sa.select(AuditLog).where(AuditLog.action == action)))


def reprocess_audits_of(deps: Deps, job_id: uuid.UUID) -> list[AuditLog]:
    return [e for e in audits(deps, "job.reprocess") if e.target_id == str(job_id)]


# --- reprocessar --------------------------------------------------------------------------------


def test_reprocess_creates_the_next_attempt_audits_the_author_and_launches_it_once(
    deps: Deps,
) -> None:
    world = build_jobs_world(deps)
    trigger = FakeTrigger()

    result = reprocess(deps, trigger, world.carla.public_id)

    assert result == actions.Reprocessed(job_id=world.carla.id, attempt=2, launched=True)
    assert trigger.calls == [(world.carla.id, 2)]
    assert attempt_of(deps, world.carla.id) == 2
    (entry,) = reprocess_audits_of(deps, world.carla.id)
    assert entry.actor == ACTOR
    assert entry.details == {
        "reason": GOOD_REASON,
        "attempt": 2,
        "previous_status": "failed",
        "failure_code": "llm_unavailable",
    }


def test_a_reprocess_the_engine_did_not_receive_is_a_created_attempt_left_to_the_reconciler(
    deps: Deps,
) -> None:
    world = build_jobs_world(deps)

    result = reprocess(deps, FakeTrigger(run_id=None), world.carla.public_id)

    assert result == actions.Reprocessed(job_id=world.carla.id, attempt=2, launched=False)
    assert attempt_of(deps, world.carla.id) == 2  # a ação vale; o reconciliador a dispara
    with deps.sessions.begin() as session:
        unlaunched = jobs.unlaunched_jobs(session, older_than=timedelta(0), limit=10)
    assert world.carla.id in [job.id for job in unlaunched]


@pytest.mark.parametrize("size", [10, 300])
def test_the_reason_is_accepted_at_the_limits_and_trimmed_before_counting(
    deps: Deps, size: int
) -> None:
    world = build_jobs_world(deps)
    padded = "   " + "m" * size + "  \n"

    result = reprocess(deps, FakeTrigger(), world.carla.public_id, reason=padded)

    assert result.attempt == 2
    (entry,) = reprocess_audits_of(deps, world.carla.id)
    assert entry.details["reason"] == "m" * size


@pytest.mark.parametrize("size", [0, 9, 301])
def test_a_reason_out_of_the_limits_is_refused_before_anything_happens(
    deps: Deps, size: int
) -> None:
    world = build_jobs_world(deps)
    trigger = FakeTrigger()

    with pytest.raises(actions.InvalidReprocess) as refused:
        reprocess(deps, trigger, world.carla.public_id, reason=" " + "m" * size + " ")

    assert refused.value.errors == {
        "reason": "Descreva o motivo com pelo menos 10 caracteres (no máximo 300)."
    }
    assert trigger.calls == [] and attempt_of(deps, world.carla.id) == 1


def test_without_the_confirmation_nothing_happens_and_both_problems_come_together(
    deps: Deps,
) -> None:
    world = build_jobs_world(deps)
    trigger = FakeTrigger()

    with pytest.raises(actions.InvalidReprocess) as refused:
        reprocess(deps, trigger, world.carla.public_id, reason="curto", confirm=False)

    assert set(refused.value.errors) == {"reason", "confirm"}
    assert refused.value.errors["confirm"] == (
        "Confirme que entende que isso gera uma nova tentativa."
    )
    assert trigger.calls == [] and attempt_of(deps, world.carla.id) == 1
    assert reprocess_audits_of(deps, world.carla.id) == []


def test_the_text_of_the_reason_is_never_in_the_message_of_the_error(deps: Deps) -> None:
    world = build_jobs_world(deps)

    with pytest.raises(actions.InvalidReprocess) as refused:
        reprocess(deps, FakeTrigger(), world.carla.public_id, reason="Maria", confirm=False)

    assert "Maria" not in str(refused.value)


def test_an_attempt_that_is_not_the_expected_one_is_refused_and_creates_no_second_attempt(
    deps: Deps,
) -> None:
    world = build_jobs_world(deps)
    trigger = FakeTrigger()
    reprocess(deps, trigger, world.carla.public_id, expected_attempt=1)

    with pytest.raises(actions.ReprocessRefused):
        reprocess(deps, trigger, world.carla.public_id, expected_attempt=1)  # a resposta se perdeu

    assert trigger.calls == [(world.carla.id, 2)]
    assert attempt_of(deps, world.carla.id) == 2


def test_without_an_expected_attempt_the_only_check_is_the_state_of_the_request(
    deps: Deps,
) -> None:
    world = build_jobs_world(deps)

    result = reprocess(deps, FakeTrigger(), world.beatriz.public_id, expected_attempt=None)

    assert result.attempt == 3  # a beatriz estava na tentativa 2


@pytest.mark.parametrize("name", ["debora", "elisa"])
def test_a_request_in_the_queue_or_being_generated_cannot_be_reprocessed(
    deps: Deps, name: str
) -> None:
    job = getattr(build_jobs_world(deps), name)
    trigger = FakeTrigger()

    with pytest.raises(actions.ReprocessRefused):
        reprocess(deps, trigger, job.public_id)

    assert trigger.calls == [] and attempt_of(deps, job.id) == 1


def test_a_request_that_does_not_exist_is_not_found_even_with_a_good_reason(deps: Deps) -> None:
    build_jobs_world(deps)
    trigger = FakeTrigger()

    with pytest.raises(actions.JobNotFound):
        reprocess(deps, trigger, "nao-existe")

    assert trigger.calls == []


# --- reenviar uma entrega e uma sincronização ---------------------------------------------------


def failed_ids(deps: Deps, model: type[Delivery | IntegrationSync]) -> list[uuid.UUID]:
    with deps.sessions() as session:
        return list(session.scalars(sa.select(model.id).where(model.status == "failed")))


def resend_delivery(deps: Deps, public_id: str, delivery_id: uuid.UUID) -> actions.Resent:
    return asyncio.run(actions.resend_delivery(deps, public_id, delivery_id, actor=ACTOR))


def resend_sync(deps: Deps, public_id: str, sync_id: uuid.UUID) -> actions.Resent:
    return asyncio.run(actions.resend_sync(deps, public_id, sync_id, actor=ACTOR))


def launches(
    monkeypatch: pytest.MonkeyPatch, kind: str, run_id: str | None = "run-x"
) -> list[uuid.UUID]:
    """Troca o disparo do Hatchet (a fronteira) por um registro: `launch_delivery` ou `launch_sync`."""
    calls: list[uuid.UUID] = []

    def fake(target: uuid.UUID) -> str | None:
        calls.append(target)
        return run_id

    monkeypatch.setattr(workflows, f"launch_{kind}", fake)
    return calls


def test_resending_a_failed_delivery_returns_it_to_pending_audited_and_launches_it(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_jobs_world(deps)
    (delivery_id,) = failed_ids(deps, Delivery)
    calls = launches(monkeypatch, "delivery")

    result = resend_delivery(deps, world.beatriz.public_id, delivery_id)

    assert result == actions.Resent(launched=True)
    assert calls == [delivery_id]
    assert failed_ids(deps, Delivery) == []
    (entry,) = audits(deps, "delivery.resend")
    assert (entry.actor, entry.target_id) == (ACTOR, str(delivery_id))
    assert entry.details == {"job_id": str(world.beatriz.id)}


def test_a_resent_delivery_the_engine_did_not_receive_stays_pending_for_the_reconciler(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_jobs_world(deps)
    (delivery_id,) = failed_ids(deps, Delivery)
    launches(monkeypatch, "delivery", run_id=None)

    result = resend_delivery(deps, world.beatriz.public_id, delivery_id)

    assert result == actions.Resent(launched=False)
    assert failed_ids(deps, Delivery) == []  # voltou para pendente, sem run id
    with deps.sessions.begin() as session:
        unlaunched = jobs.unlaunched_deliveries(session, older_than=timedelta(0), limit=10)
    assert delivery_id in [delivery.id for delivery in unlaunched]


def test_resending_a_failed_sync_returns_it_to_pending_audited_and_launches_it(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = build_jobs_world(deps)
    (sync_id,) = failed_ids(deps, IntegrationSync)
    calls = launches(monkeypatch, "sync")

    result = resend_sync(deps, world.beatriz.public_id, sync_id)

    assert result == actions.Resent(launched=True)
    assert calls == [sync_id]
    assert failed_ids(deps, IntegrationSync) == []
    (entry,) = audits(deps, "sync.resend")
    assert (entry.actor, entry.target_id) == (ACTOR, str(sync_id))


@pytest.mark.parametrize("kind", ["delivery", "sync"])
def test_a_resend_of_something_that_is_not_failed_is_refused_without_launching(
    deps: Deps, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    world = build_jobs_world(deps)
    calls = launches(monkeypatch, kind)
    resend = resend_delivery if kind == "delivery" else resend_sync
    model = Delivery if kind == "delivery" else IntegrationSync
    with deps.sessions() as session:  # os da ana saíram bem
        done = session.scalar(
            sa.select(model.id).where(model.job_id == world.ana.id, model.status == "succeeded")
        )
    assert done is not None

    with pytest.raises(actions.ResendRefused) as refused:
        resend(deps, world.ana.public_id, done)

    assert refused.value.missing is False  # existe, mas não falhou
    assert calls == [] and audits(deps, f"{kind}.resend") == []


@pytest.mark.parametrize("kind", ["delivery", "sync"])
def test_a_resend_of_a_row_that_is_not_of_that_request_or_does_not_exist_is_missing(
    deps: Deps, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    world = build_jobs_world(deps)
    calls = launches(monkeypatch, kind)
    resend = resend_delivery if kind == "delivery" else resend_sync
    model = Delivery if kind == "delivery" else IntegrationSync
    (failed,) = failed_ids(deps, model)  # é da beatriz

    with pytest.raises(actions.ResendRefused) as of_another:
        resend(deps, world.carla.public_id, failed)
    with pytest.raises(actions.ResendRefused) as unknown:
        resend(deps, world.beatriz.public_id, uuid.uuid4())

    assert of_another.value.missing is True and unknown.value.missing is True
    assert calls == [] and failed_ids(deps, model) == [failed]  # nada mudou


@pytest.mark.parametrize("kind", ["delivery", "sync"])
def test_a_resend_for_a_request_that_does_not_exist_is_not_found(
    deps: Deps, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    build_jobs_world(deps)
    launches(monkeypatch, kind)
    resend = resend_delivery if kind == "delivery" else resend_sync

    with pytest.raises(actions.JobNotFound):
        resend(deps, "nao-existe", uuid.uuid4())
