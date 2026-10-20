"""Consulta de capturas: estado separado das respostas e da identidade histórica."""

import uuid
from dataclasses import asdict

import sqlalchemy as sa

from app.models import CaptureReceipt, Submission
from app.submission_identity import resolve_identity


def listing(session, forms, *, with_lead=False, form=None, version=None, after=None, limit=50):
    """Lê apenas colunas de estado; dados pessoais são carregados somente com opt-in autorizado."""
    statement = sa.select(
        Submission.id,
        Submission.form_slug,
        Submission.form_version,
        Submission.form_version_number,
        Submission.created_at,
    ).join(CaptureReceipt, CaptureReceipt.submission_id == Submission.id)
    if form:
        statement = statement.where(Submission.form_slug == form)
    if version is not None:
        statement = statement.where(Submission.form_version_number == version)
    if after:
        statement = statement.where(sa.tuple_(Submission.created_at, Submission.id) < after)
    rows = session.execute(
        statement.order_by(Submission.created_at.desc(), Submission.id.desc()).limit(limit)
    ).all()
    return [_row(session, forms, row, with_lead) for row in rows]


def detail(session, forms, submission_id, *, with_lead=False):
    """Uma captura por referência estável, nunca por Job ou artefato."""
    try:
        wanted = uuid.UUID(str(submission_id))
    except ValueError:
        return None
    row = session.execute(
        sa.select(
            Submission.id,
            Submission.form_slug,
            Submission.form_version,
            Submission.form_version_number,
            Submission.created_at,
        )
        .join(CaptureReceipt, CaptureReceipt.submission_id == Submission.id)
        .where(Submission.id == wanted)
    ).one_or_none()
    return _row(session, forms, row, with_lead) if row else None


def _row(session, forms, row, with_lead):
    found = dict(row._mapping)
    found["submission_id"] = str(found.pop("id"))
    from app.capture_deliveries import listing as deliveries

    found.update(
        status="captured", lead_data=None, deliveries=deliveries(session, submission_id=row.id)
    )
    if with_lead:
        submission = session.get_one(Submission, row.id)
        identity = resolve_identity(forms, submission)
        found["lead_data"] = {
            "identity": asdict(identity),
            "answers": submission.answers,
            "consent": submission.consent,
            "tracking": submission.tracking,
            "scores": submission.scores,
            "priority": submission.priority,
            "persona": submission.persona,
        }
    return found
