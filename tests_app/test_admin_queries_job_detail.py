"""O detalhe de um pedido (`app.admin.queries.jobs.read_job`): o estado do pedido (entregas,
sincronizações, PDFs e linha do tempo) e, à parte, o dado da lead, onde ficam também os textos
livres dos motivos.

A página o decora e a API o serializa. A lista do mesmo módulo tem os testes em
`test_admin_queries_jobs.py`.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from jobs_world import (
    FAILURE_DETAIL,
    PEOPLE,
    PERSONAL,
    REASON,
    SYNC_ERROR,
    build_jobs_world,
    field_names,
)

from app.admin.queries import jobs as queries
from app.deps import Deps
from app.models import Artifact, Job

NOW = datetime(2027, 1, 2, 3, 4, 5, tzinfo=UTC)


def read(deps: Deps, job: Job, *, with_lead: bool = True, now: datetime = NOW) -> queries.JobDetail:
    with deps.sessions() as session:
        found = queries.read_job(
            session, job.public_id, forms=deps.forms, now=now, with_lead=with_lead
        )
    assert found is not None
    return found


def types(attempt: queries.AttemptState) -> list[str]:
    return [event.type for event in attempt.events]


def test_a_job_that_does_not_exist_is_none(deps: Deps) -> None:
    with deps.sessions() as session:
        assert (
            queries.read_job(session, "nao-existe", forms=deps.forms, now=NOW, with_lead=True)
            is None
        )


def test_the_state_says_the_form_the_dates_and_the_failure(deps: Deps) -> None:
    world = build_jobs_world(deps)

    failed = read(deps, world.carla).state
    done = read(deps, world.ana).state

    assert (failed.status, failed.stage, failed.attempt) == ("failed", "failed", 1)
    assert failed.failure_code == "llm_unavailable" and failed.completed_at is None
    assert (failed.form_slug, failed.form_version_number) == ("demo", 1)
    assert failed.form_version  # o texto da versão, o que o webhook leva
    assert done.status == "completed" and done.failure_code is None
    assert done.completed_at is not None and done.created_at < done.completed_at
    assert done.hatchet_run_id == "run-1"


def test_the_link_of_the_lead_expires_at_the_date_of_the_job_and_the_caller_says_now(
    deps: Deps,
) -> None:
    world = build_jobs_world(deps)
    expires = read(deps, world.ana).state.link_expires_at

    assert read(deps, world.ana, now=expires - timedelta(seconds=1)).state.link_expired is False
    assert read(deps, world.ana, now=expires + timedelta(seconds=1)).state.link_expired is True


def test_the_timeline_has_one_entry_per_attempt_built_from_what_was_recorded(deps: Deps) -> None:
    world = build_jobs_world(deps)

    first, second = read(deps, world.beatriz).state.timeline

    assert (first.number, first.outcome) == (1, "completed")  # o fim vem da auditoria do reprocesso
    assert types(first) == [
        "received",
        "plan_generated",
        "pdf_generated",
        "completed",
        "delivery_failed",
    ]
    assert (second.number, second.outcome) == (2, "completed")
    assert types(second) == [
        "reprocess_requested",
        "plan_generated",
        "pdf_generated",
        "completed",
        "delivery_pending",
    ]
    reprocess = second.events[0]
    assert reprocess.actor == "operadora" and reprocess.at is not None
    assert second.events[1].model and second.events[1].prompt_version
    assert second.events[2].byte_size and second.events[2].byte_size > 0


def test_the_timeline_of_a_failed_job_a_running_one_and_a_queued_one(deps: Deps) -> None:
    world = build_jobs_world(deps)

    (failed,) = read(deps, world.carla).state.timeline
    (running,) = read(deps, world.elisa).state.timeline
    (queued,) = read(deps, world.debora).state.timeline

    assert types(failed) == ["received", "failed"]
    assert failed.events[1].failure_code == "llm_unavailable" and failed.outcome == "failed"
    assert types(running) == ["received", "processing"]
    assert running.events[1].stage == "rendering" and running.outcome == "running"
    assert types(queued) == ["received", "queued"] and queued.outcome == "queued"


def test_a_delivery_event_points_to_its_delivery(deps: Deps) -> None:
    world = build_jobs_world(deps)
    state = read(deps, world.ana).state
    (delivery,) = state.deliveries

    event = state.timeline[0].events[-1]

    assert (event.type, event.delivery_id, event.at) == (
        "delivery_succeeded",
        delivery.id,
        delivery.delivered_at,
    )
    assert (delivery.destination, delivery.status, delivery.attempts) == ("webhook", "succeeded", 1)
    assert delivery.job_attempt == 1


def test_the_deliveries_the_syncs_and_the_pdfs_say_their_state_without_the_error_text(
    deps: Deps,
) -> None:
    world = build_jobs_world(deps)

    state = read(deps, world.beatriz).state

    assert [(d.job_attempt, d.status) for d in state.deliveries] == [(1, "failed"), (2, "pending")]
    assert [(p.version, p.purged_at) for p in state.pdfs] == [(2, None), (1, None)]  # a nova antes
    assert all(p.byte_size > 0 for p in state.pdfs)
    # a sincronização mais recente primeiro (como a página mostra)
    assert [(s.job_attempt, s.status) for s in state.syncs] == [(2, "failed"), (1, "pending")]
    sync = state.syncs[0]
    assert (sync.integration, sync.host, sync.table_id, sync.attempts) == (
        "baserow",
        "baserow.example.test",
        7,
        4,
    )
    synced = read(deps, world.ana).state.syncs[0]
    assert (synced.outcome, synced.row_id) == ("created", 7) and synced.synced_at is not None


def test_a_removed_pdf_says_when_the_retention_removed_it(deps: Deps) -> None:
    world = build_jobs_world(deps)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Artifact).where(Artifact.job_id == world.ana.id).values(purged_at=NOW)
        )

    (pdf,) = read(deps, world.ana).state.pdfs

    assert pdf.purged_at == NOW


# --- a barreira: o estado não tem dado pessoal, o texto livre vai para o dado da lead -----------


def test_the_state_and_everything_it_holds_have_no_personal_field() -> None:
    assert field_names(queries.JobState).isdisjoint(PERSONAL)


def test_no_event_of_the_state_carries_free_text() -> None:
    """Quem escreve um motivo escreve o que quer, inclusive o nome de uma lead: o texto livre fica no
    dado da lead (`Note`), e o evento só diz o que aconteceu."""
    assert {f.name for f in dataclasses.fields(queries.TimelineEvent)} == {
        "type",
        "at",
        "actor",
        "failure_code",
        "stage",
        "delivery_id",
        "model",
        "prompt_version",
        "byte_size",
    }
    assert {"last_error", "failure_detail", "reason", "note"}.isdisjoint(
        field_names(queries.JobState)
    )


def test_without_the_lead_the_detail_has_no_lead_data(deps: Deps) -> None:
    world = build_jobs_world(deps)

    detail = read(deps, world.beatriz, with_lead=False)

    assert detail.lead is None
    assert detail.state == read(deps, world.beatriz).state  # o estado é o mesmo com ou sem a lead


def test_the_lead_data_has_the_contact_the_scores_the_answers_the_consent_and_the_latest_plan(
    deps: Deps,
) -> None:
    world = build_jobs_world(deps)

    lead = read(deps, world.beatriz).lead

    assert lead is not None
    assert (lead.name, lead.email) == PEOPLE["beatriz"]
    assert lead.priority == "P2"
    assert lead.answers["nome"] == "Beatriz Lima" and lead.scores
    assert lead.consent.text_version == "demo-v1" and lead.consent.privacy_at is not None
    assert (lead.consent.marketing_email, lead.consent.marketing_whatsapp) == (False, False)
    assert lead.plan is not None and lead.plan.version == 2  # o plano da tentativa mais recente
    assert lead.plan.document["titulo"] and lead.plan.model


def test_a_job_without_a_plan_has_no_plan_in_the_lead_data(deps: Deps) -> None:
    world = build_jobs_world(deps)

    lead = read(deps, world.debora).lead

    assert lead is not None and lead.plan is None


def test_the_free_text_of_the_reasons_goes_to_the_notes_of_the_lead_data(deps: Deps) -> None:
    world = build_jobs_world(deps)

    beatriz = read(deps, world.beatriz)
    carla = read(deps, world.carla)

    assert beatriz.lead is not None and carla.lead is not None
    delivery_1, delivery_2 = beatriz.state.deliveries
    notes = {(note.kind, note.attempt, note.ref): note.text for note in beatriz.lead.notes}
    assert notes[("reprocess_reason", 2, None)] == REASON
    assert notes[("delivery_error", 1, delivery_1.id)].startswith("[delivery_unavailable]")
    assert ("delivery_error", 2, delivery_2.id) not in notes  # a entrega 2 não tem erro
    (sync_note,) = [n for n in beatriz.lead.notes if n.kind == "sync_error"]
    assert (sync_note.attempt, sync_note.text) == (2, SYNC_ERROR)
    assert {(n.kind, n.attempt, n.text) for n in carla.lead.notes} == {
        ("failure_detail", 1, FAILURE_DETAIL)
    }
