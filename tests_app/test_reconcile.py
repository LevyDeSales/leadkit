from __future__ import annotations

import uuid
from datetime import timedelta

import sqlalchemy as sa
from conftest import publish, record_webhook
from test_intake import submit
from test_pipeline import run_until_finalize

from app import crons, jobs, pipeline
from app.crons import RunState, reconcile_once
from app.deps import Deps
from app.forms import FormSpec
from app.models import Delivery, Job, Plan, utcnow


def age(deps: Deps, model: type, delta: timedelta, column: str = "updated_at") -> None:
    with deps.sessions.begin() as session:
        session.execute(sa.update(model).values({column: utcnow() - delta}))


def test_launches_jobs_whose_trigger_was_lost(deps: Deps) -> None:
    job = submit(deps).job
    age(deps, Job, timedelta(minutes=5))
    launched: list[tuple[object, int]] = []

    report = reconcile_once(deps, launch_job=lambda j, a: launched.append((j, a)) or "run-1")

    assert launched == [(job.id, 1)] and report.jobs_launched == 1


def test_relaunched_jobs_keep_the_form_version_the_lead_answered(
    deps: Deps, newer_demo: FormSpec
) -> None:
    job = submit(deps).job  # versão 1, com o disparo perdido
    publish(deps, newer_demo)
    age(deps, Job, timedelta(minutes=5))

    def worker(job_id: uuid.UUID, attempt: int) -> str:  # roda o que o reconciliador disparou
        with deps.sessions.begin() as session:
            jobs.claim(session, job_id, attempt, "run-1")
        pipeline.generate_plan(deps, job_id, attempt)
        return "run-1"

    assert reconcile_once(deps, launch_job=worker).jobs_launched == 1  # type: ignore[arg-type]
    with deps.sessions() as session:
        plan = session.scalars(sa.select(Plan).where(Plan.job_id == job.id)).one()
    assert plan.prompt_version == "demo-v1"


def test_stops_when_engine_is_unreachable(deps: Deps) -> None:
    submit(deps, key="a")
    submit(deps, key="b")
    age(deps, Job, timedelta(minutes=5))
    calls: list[object] = []

    report = reconcile_once(deps, launch_job=lambda j, a: calls.append(j))  # devolve None

    assert len(calls) == 1 and report.errors == 1


def test_recent_jobs_are_left_alone(deps: Deps) -> None:
    submit(deps)
    report = reconcile_once(deps, launch_job=lambda j, a: "nao-devia")
    assert report.jobs_launched == 0


def _in_flight(deps: Deps, run_id: str = "run-1") -> Job:
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, run_id)
    return job


def test_run_missing_in_engine_is_relaunched_unless_stuck(deps: Deps) -> None:
    job = _in_flight(deps)
    age(deps, Job, timedelta(minutes=15))
    relaunched: list[object] = []
    report = reconcile_once(
        deps,
        launch_job=lambda j, a: relaunched.append(j) or "run-2",
        run_state=lambda run_id: RunState.MISSING,
    )
    assert relaunched == [job.id] and report.jobs_relaunched == 1

    age(deps, Job, crons.STUCK_AFTER + timedelta(minutes=1))
    report = reconcile_once(
        deps, launch_job=lambda j, a: "x", run_state=lambda run_id: RunState.MISSING
    )
    with deps.sessions() as session:
        stored = session.get_one(Job, job.id)
    assert report.jobs_failed == 1 and (stored.status, stored.failure_code) == ("failed", "stuck")


def test_run_that_ended_without_updating_the_job_fails_it(deps: Deps) -> None:
    job = _in_flight(deps)
    age(deps, Job, timedelta(minutes=15))
    report = reconcile_once(deps, run_state=lambda run_id: RunState.FAILED)
    with deps.sessions() as session:
        assert session.get_one(Job, job.id).status == "failed"
    assert report.jobs_failed == 1


def test_active_runs_are_untouched(deps: Deps) -> None:
    job = _in_flight(deps)
    age(deps, Job, timedelta(minutes=15))
    reconcile_once(deps, run_state=lambda run_id: RunState.ACTIVE)
    with deps.sessions() as session:
        assert session.get_one(Job, job.id).status == "running"


def test_launches_pending_deliveries_without_run(deps: Deps) -> None:
    record_webhook(deps)
    _, deliveries = run_until_finalize(deps)
    age(deps, Delivery, timedelta(minutes=5), column="created_at")
    launched: list[object] = []
    report = reconcile_once(
        deps,
        launch_job=lambda j, a: "x",
        run_state=lambda run_id: RunState.ACTIVE,
        launch_delivery_fn=lambda d: launched.append(d) or "run-d",
    )
    assert launched == deliveries and report.deliveries_launched == 1


def test_cron_is_off_by_default_outside_production(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("RECONCILE_CRON", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    assert crons.reconcile_schedule() == []
    monkeypatch.setenv("APP_ENV", "production")
    assert crons.reconcile_schedule() == ["*/2 * * * *"]
    monkeypatch.setenv("RECONCILE_CRON", "")
    assert crons.reconcile_schedule() == []
