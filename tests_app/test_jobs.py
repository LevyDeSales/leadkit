from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from test_intake import submit

from app import jobs
from app.deps import Deps
from app.models import AuditLog, Job


def get_job(deps: Deps, job_id: object) -> Job:
    with deps.sessions() as session:
        return session.get_one(Job, job_id)


def test_claim_moves_to_running_and_records_run_id(deps: Deps) -> None:
    job = submit(deps).job
    with deps.sessions.begin() as session:
        assert jobs.claim(session, job.id, 1, "run-1") is jobs.ClaimResult.PROCEED
    stored = get_job(deps, job.id)
    assert stored.status == "running" and stored.hatchet_run_id == "run-1"


def test_claim_is_repeatable_for_the_same_attempt_and_keeps_first_run_id(deps: Deps) -> None:
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "run-1")
    with deps.sessions.begin() as session:
        assert jobs.claim(session, job.id, 1, "run-2") is jobs.ClaimResult.PROCEED
    assert get_job(deps, job.id).hatchet_run_id == "run-1"


def test_claim_after_completion_or_of_old_attempt_does_not_proceed(deps: Deps) -> None:
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "run-1")
        assert jobs.complete(session, job.id, 1)
    with deps.sessions.begin() as session:
        assert jobs.claim(session, job.id, 1, "dup") is jobs.ClaimResult.ALREADY_DONE
        assert jobs.reprocess(session, job.id, actor="admin@teste", reason="teste") == 2
    with deps.sessions.begin() as session:
        assert jobs.claim(session, job.id, 1, "velho") is jobs.ClaimResult.SUPERSEDED


def test_complete_and_fail_only_apply_to_the_current_attempt(deps: Deps) -> None:
    job = submit(deps).job
    with deps.sessions.begin() as session:
        assert not jobs.complete(session, job.id, 1)  # ainda queued
        assert not jobs.fail(session, job.id, 2, "internal", "tentativa errada")
        assert jobs.fail(session, job.id, 1, "plan_invalid", "plan falhou")
        assert not jobs.complete(session, job.id, 1)  # terminal
    stored = get_job(deps, job.id)
    assert (stored.status, stored.stage, stored.failure_code) == (
        "failed",
        "failed",
        "plan_invalid",
    )


def test_reprocess_resets_execution_and_audits(deps: Deps) -> None:
    job = submit(deps).job
    with deps.sessions.begin() as session:
        assert jobs.reprocess(session, job.id, actor="a", reason="x") is None  # queued: recusa
        jobs.fail(session, job.id, 1, "internal", "falhou")
    with deps.sessions.begin() as session:
        assert jobs.reprocess(session, job.id, actor="operadora", reason="cliente pediu") == 2
    stored = get_job(deps, job.id)
    assert (stored.status, stored.attempt, stored.failure_code, stored.hatchet_run_id) == (
        "queued",
        2,
        None,
        None,
    )
    with deps.sessions() as session:
        audit = session.scalars(sa.select(AuditLog)).one()
    assert audit.actor == "operadora" and audit.details["attempt"] == 2


def test_public_status_derives_expired(deps: Deps) -> None:
    job = get_job(deps, submit(deps).job.id)
    assert jobs.public_status(job) == "queued"
    assert jobs.public_status(job, now=datetime.now(UTC) + timedelta(days=31)) == "expired"
    job.status = "completed"
    assert jobs.public_status(job, now=datetime.now(UTC) + timedelta(days=31)) == "completed"
