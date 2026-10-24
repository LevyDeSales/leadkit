"""Consulta de capturas: estado separado das respostas e da identidade histórica."""

import uuid
from dataclasses import asdict

import sqlalchemy as sa

from app.admin.capture_reading import answer_rows
from app.consent import facts as consent_facts
from app.errors import TerminalError
from app.models import CaptureReceipt, Submission
from app.submission_identity import identity_from_answers, submission_form


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
        spec = None
        identity = None
        try:
            spec = submission_form(forms, submission)
        except TerminalError:
            # Sem versão histórica, não presumimos rótulos nem contato do cadastro compartilhado.
            pass
        if spec is not None:
            try:
                identity = identity_from_answers(submission, spec)
            except TerminalError:
                # Respostas inválidas não impedem a leitura interna dos valores originais.
                pass
        found["lead_data"] = {
            "identity": asdict(identity) if identity is not None else None,
            "identity_unavailable": identity is None,
            "answers": submission.answers,
            "answer_rows": answer_rows(spec, submission.answers),
            "consent": consent_facts(submission.consent, mode="json")
            if "evidence_schema" in submission.consent
            else submission.consent,
            "tracking": submission.tracking,
            "scores": submission.scores,
            "priority": submission.priority,
            "persona": submission.persona,
        }
    return found
