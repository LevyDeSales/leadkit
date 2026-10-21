"""Persistência e projeção de declarações: nenhum efeito sobre liberação ou transporte."""

import hmac
import json
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.orm import undefer

from app import event_sources
from app.admin.api.common import decode_keyset, encode_keyset
from app.deployment_contract import assert_contract
from app.handoff_types import HandoffError, confirmed, version_matches
from app.integration_credentials import digest
from app.integration_readiness import configuration_lock
from app.journey_types import (
    BASE_TYPES,
    EVIDENCE,
    JourneyEventInfo,
    JourneyEventReceipt,
    LocalFact,
    LocalJob,
    SubmissionJourney,
)
from app.models import (
    Delivery,
    EventCredential,
    HandoffReceiver,
    HandoffReference,
    Job,
    JourneyEvent,
    PlanRelease,
    Submission,
)


def authorize_policy(source, body):
    policy = source.policy
    if body.source_revision != source.revision:
        raise HandoffError("source_revision_conflict")
    if (
        body.type not in policy["types"]
        or body.evidence.channel not in policy["channels"]
        or body.evidence.kind not in policy["evidence_kinds"]
        or body.evidence.kind not in EVIDENCE[body.type]
    ):
        raise HandoffError("source_not_authorized", status=403)
    if body.type == "entity.withdrawn" and BASE_TYPES[body.entity.kind] not in policy["types"]:
        raise HandoffError("source_not_authorized", status=403)
    participant = body.type == "participant.receipt_acknowledged"
    if (body.evidence.author.kind == "participant") != participant:
        raise HandoffError("source_not_authorized", status=403)
    if (
        body.type == "notification.delivery_reported"
        and body.data.status == "confirmed"
        and body.evidence.kind != "provider_delivery_receipt"
    ):
        raise HandoffError("source_not_authorized", status=403)


def ingest_event(session, principal, body, *, now):
    source = event_sources.source_row(session, principal.source_id)
    authorize_policy(source, body)
    if body.occurred_at > now + timedelta(minutes=5):
        raise HandoffError("validation_failed", status=422, field="occurred_at")
    context = session.execute(
        sa.select(HandoffReference.submission_id, HandoffReference.receiver_id).where(
            HandoffReference.id == body.reference_id
        )
    ).first()
    configuration_lock(session, shared=True)
    assert_contract(session, required=2)
    if context is None:
        raise HandoffError("correlation_unavailable", status=404)
    submission = session.get(Submission, context.submission_id, with_for_update=True)
    if submission is None:
        raise HandoffError("correlation_unavailable", status=404)
    session.get(HandoffReceiver, context.receiver_id, with_for_update=True)
    source = event_sources.source_row(session, principal.source_id, lock=True)
    credential = session.get(EventCredential, principal.credential_id, with_for_update=True)
    if (
        credential is None
        or credential.source_id != source.id
        or not hmac.compare_digest(credential.secret_hash, principal.secret_hash)
    ):
        raise HandoffError("invalid_credentials", status=401)
    event_sources.validate_authority(source, credential, now)
    authorize_policy(source, body)
    reference = session.get(HandoffReference, body.reference_id, with_for_update=True)
    if (
        reference is None
        or reference.submission_id != submission.id
        or reference.revoked_at
        or reference.issuance_state != "ready"
        or reference.purpose != "support"
        or reference.receiver_id != source.receiver_id
        or reference.receiver_revision != source.receiver_revision
        or submission.form_slug != source.form_slug
    ):
        raise HandoffError("correlation_unavailable", status=404)
    document = body.model_dump(mode="json")
    fingerprint = digest(json.dumps(document, sort_keys=True, separators=(",", ":")))
    event_digest, entity_digest = digest(body.event_id), digest(body.entity.id)
    prior = session.scalar(
        sa.select(JourneyEvent).where(
            JourneyEvent.source_id == source.id, JourneyEvent.event_id_digest == event_digest
        )
    )
    if prior:
        if prior.fingerprint != fingerprint:
            raise HandoffError("event_conflict")
        return JourneyEventReceipt(
            id=prior.id,
            received_at=prior.received_at,
            replay=True,
            projection_effect=prior.safe_data["_effect"],
        )
    entity_events = sa.select(JourneyEvent).where(
        JourneyEvent.source_id == source.id,
        JourneyEvent.reference_id == reference.id,
        JourneyEvent.entity_kind == body.entity.kind,
        JourneyEvent.entity_id_digest == entity_digest,
    )
    if session.scalar(entity_events.where(JourneyEvent.entity_revision == body.entity.revision)):
        raise HandoffError("entity_revision_conflict")
    latest = session.scalar(entity_events.order_by(JourneyEvent.entity_revision.desc()).limit(1))
    effect = (
        "historical"
        if latest and latest.entity_revision > body.entity.revision
        else "withdrawn"
        if body.type == "entity.withdrawn"
        else "current"
    )
    safe = {
        key: value
        for key, value in document["data"].items()
        if key
        in (
            "status",
            "starts_at",
            "ends_at",
            "timezone",
            "change",
            "reason_code",
            "meeting_revision",
        )
    }
    if body.type == "meeting.attendance_recorded":
        safe["_meeting_digest"] = digest(body.data.meeting_id)
    safe["_effect"] = effect
    row = JourneyEvent(
        source_id=source.id,
        credential_id=credential.id,
        source_revision=source.revision,
        reference_id=reference.id,
        event_id_digest=event_digest,
        entity_id_digest=entity_digest,
        fingerprint=fingerprint,
        type=body.type,
        entity_kind=body.entity.kind,
        entity_revision=body.entity.revision,
        occurred_at=body.occurred_at,
        received_at=now,
        safe_data=safe,
        protected_data={
            "event_id": body.event_id,
            "entity_id": body.entity.id,
            "evidence": document["evidence"],
            "data": document["data"],
        },
    )
    session.add(row)
    credential.last_used_at = now
    session.flush()
    return JourneyEventReceipt(
        id=row.id, received_at=row.received_at, replay=False, projection_effect=effect
    )


def reference_for_submission(session, submission_id, *, lock=False):
    submission = session.get(Submission, submission_id, with_for_update=lock)
    if submission is None:
        raise HandoffError("not_found", status=404)
    query = sa.select(HandoffReference).where(HandoffReference.submission_id == submission_id)
    reference = session.scalar(query.with_for_update() if lock else query)
    return submission, reference


def journey_version(session, reference):
    if reference is None:
        return digest("empty")
    rows = session.execute(
        sa.select(JourneyEvent.id, JourneyEvent.erased_at)
        .where(JourneyEvent.reference_id == reference.id)
        .order_by(JourneyEvent.id)
    ).all()
    return digest(json.dumps([[str(row.id), str(row.erased_at)] for row in rows]))


def event_info(session, row, *, with_lead=False):
    safe = row.safe_data
    meeting = None
    if not row.erased_at and row.entity_kind == "attendance" and "_meeting_digest" in safe:
        meeting = session.scalar(
            sa.select(JourneyEvent.id).where(
                JourneyEvent.reference_id == row.reference_id,
                JourneyEvent.source_id == row.source_id,
                JourneyEvent.entity_kind == "meeting",
                JourneyEvent.entity_id_digest == safe["_meeting_digest"],
                JourneyEvent.entity_revision == safe["meeting_revision"],
                JourneyEvent.erased_at.is_(None),
                JourneyEvent.type != "entity.withdrawn",
            )
        )
    return JourneyEventInfo(
        id=row.id,
        source_id=row.source_id,
        source_revision=row.source_revision,
        type=row.type,
        entity_kind=row.entity_kind,
        entity_revision=row.entity_revision,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
        state="erased"
        if row.erased_at
        else "withdrawn"
        if row.type == "entity.withdrawn"
        else "declared",
        status=safe.get("status"),
        starts_at=safe.get("starts_at"),
        ends_at=safe.get("ends_at"),
        timezone=safe.get("timezone"),
        meeting_revision=safe.get("meeting_revision"),
        meeting_event_id=meeting,
        lead_data=row.protected_data if with_lead and not row.erased_at else None,
    )


def read_journey(session, submission_id, *, with_lead=False):
    submission, reference = reference_for_submission(session, submission_id)
    job = session.scalar(sa.select(Job).where(Job.submission_id == submission.id))
    local = [LocalFact(type="submission", state="persisted", occurred_at=submission.created_at)]
    if job:
        from app.plan_release import release_snapshot

        access = release_snapshot(session, job)
        local.append(
            LocalFact(
                type="current_plan_authorization",
                state=access["state"],
                occurred_at=access.get("released_at") or job.created_at,
                attempt=job.attempt,
            )
        )
        local.append(
            LocalFact(
                type="generation", state=job.status, occurred_at=job.created_at, attempt=job.attempt
            )
        )
        for release in session.scalars(
            sa.select(PlanRelease).where(PlanRelease.job_id == job.id).order_by(PlanRelease.attempt)
        ):
            local.append(
                LocalFact(
                    type="plan_authorization",
                    state="released",
                    occurred_at=release.occurred_at,
                    attempt=release.attempt,
                )
            )
        for delivery in session.scalars(
            sa.select(Delivery).where(Delivery.job_id == job.id).order_by(Delivery.created_at)
        ):
            local.append(
                LocalFact(
                    type="transport",
                    state=delivery.status,
                    occurred_at=delivery.created_at,
                    attempt=delivery.authorized_attempt,
                )
            )
    projected = []
    if reference:
        ranked = (
            sa.select(
                JourneyEvent.id,
                sa.func.row_number()
                .over(
                    partition_by=(
                        JourneyEvent.source_id,
                        JourneyEvent.entity_kind,
                        JourneyEvent.entity_id_digest,
                    ),
                    order_by=JourneyEvent.entity_revision.desc(),
                )
                .label("position"),
            )
            .where(JourneyEvent.reference_id == reference.id)
            .subquery()
        )
        query = (
            sa.select(JourneyEvent)
            .join(ranked, JourneyEvent.id == ranked.c.id)
            .where(ranked.c.position == 1)
            .order_by(JourneyEvent.source_id, JourneyEvent.entity_kind, JourneyEvent.id)
        )
        if with_lead:
            query = query.options(undefer(JourneyEvent.protected_data))
        projected = [
            event_info(session, row, with_lead=with_lead) for row in session.scalars(query)
        ]
    return SubmissionJourney(
        submission_id=submission.id,
        reference_id=reference.id if reference else None,
        reference_version=str(reference.version) if reference else None,
        journey_version=journey_version(session, reference),
        job=LocalJob(id=job.id, attempt=job.attempt, status=job.status) if job else None,
        local_facts=local,
        external_declared=projected,
    )


def list_events(session, submission_id, *, page, source_id=None, event_type=None, with_lead=False):
    _, reference = reference_for_submission(session, submission_id)
    if reference is None:
        return [], None
    query = sa.select(JourneyEvent).where(JourneyEvent.reference_id == reference.id)
    if source_id is not None:
        query = query.where(JourneyEvent.source_id == source_id)
    if event_type is not None:
        query = query.where(JourneyEvent.type == event_type)
    if page.cursor:
        moment, row_id = decode_keyset(page.cursor)
        query = query.where(
            sa.tuple_(JourneyEvent.received_at, JourneyEvent.id) < sa.tuple_(moment, row_id)
        )
    if with_lead:
        query = query.options(undefer(JourneyEvent.protected_data))
    rows = list(
        session.scalars(
            query.order_by(JourneyEvent.received_at.desc(), JourneyEvent.id.desc()).limit(
                page.limit + 1
            )
        )
    )
    more, rows = len(rows) > page.limit, rows[: page.limit]
    return [event_info(session, row, with_lead=with_lead) for row in rows], encode_keyset(
        rows[-1].received_at, rows[-1].id
    ) if more else None


def erase_journey(session, submission_id, *, command, actor, now):
    confirmed(command)
    configuration_lock(session, shared=True)
    _, reference = reference_for_submission(session, submission_id, lock=True)
    if reference is None:
        raise HandoffError("not_found", status=404)
    if (
        reference.revoked_at
        and session.scalar(
            sa.select(JourneyEvent.id)
            .where(JourneyEvent.reference_id == reference.id, JourneyEvent.erased_at.is_(None))
            .limit(1)
        )
        is None
    ):
        return read_journey(session, submission_id)
    if journey_version(session, reference) != command.expected_journey_version:
        raise HandoffError("version_conflict", current_version=journey_version(session, reference))
    version_matches(reference.version, command.expected_handoff_version)
    session.execute(
        sa.update(JourneyEvent)
        .where(JourneyEvent.reference_id == reference.id, JourneyEvent.erased_at.is_(None))
        .values(protected_data=sa.null(), safe_data={}, erased_at=now)
    )
    if reference.revoked_at is None:
        reference.revoked_at, reference.revoked_by = now, actor
        reference.token_ciphertext = None
        reference.version += 1
    event_sources.audit(session, actor, "journey.erase", reference)
    session.flush()
    return read_journey(session, submission_id)
