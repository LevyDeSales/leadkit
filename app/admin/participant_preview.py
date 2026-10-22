"""Prévia administrativa da tentativa atual, com projeção antes da fronteira de apresentação."""

from dataclasses import dataclass

import sqlalchemy as sa

from app.admin.actions import previews
from app.admin.queries.jobs import PlanProjection
from app.deps import Deps
from app.errors import TerminalError
from app.forms import PlanProjectionError
from app.forms.dates import long_date
from app.forms.definition import DefinitionError
from app.forms.sandbox import TemplateProblem
from app.models import Artifact, Job, Plan, Submission
from app.pdf_availability import PdfAvailability, artifact_availability
from app.plan_release import ParticipantAccess, ReleaseRefused, participant_access
from app.submission_identity import identity_from_answers, submission_form


class PreviewUnavailable(ValueError):
    """A tentativa atual não oferece uma prévia histórica verificável."""


@dataclass(frozen=True)
class ParticipantResultPreview:
    """Somente representações participantes e estados; nenhum documento integral sai da ação."""

    html: str
    projection: PlanProjection
    access_state: ParticipantAccess
    pdf_state: PdfAvailability


def render_job_result(deps: Deps, public_id: str) -> ParticipantResultPreview | None:
    """Serializa a observação com reprocessamento e retenção; não libera, grava ou gera nada."""
    from app.admin.api.previews import content_policy
    from app.web.origin import origin_of

    with deps.sessions() as session:
        # O reprocessamento e a exclusão do pedido bloqueiam Job antes de alterar os filhos.
        job = session.scalar(sa.select(Job).where(Job.public_id == public_id).with_for_update())
        if job is None:
            return None
        observed = session.execute(
            sa.select(Submission, Plan)
            .join(Plan, sa.and_(Plan.job_id == job.id, Plan.version == job.attempt))
            .where(Submission.id == job.submission_id)
        ).one_or_none()
        if observed is None:
            raise PreviewUnavailable("A prévia da tentativa atual não está disponível.")
        submission, plan = observed
        try:
            spec = submission_form(deps.forms, submission)
            identity = identity_from_answers(submission, spec)
            if spec.templates is None:
                raise PreviewUnavailable("A prévia histórica não está disponível.")
            artifact = session.scalar(
                sa.select(Artifact)
                .where(
                    Artifact.job_id == job.id,
                    Artifact.version == job.attempt,
                    Artifact.kind == "pdf",
                )
                .with_for_update()  # A retenção isolada de PDFs bloqueia somente Artifact.
            )
            matching = artifact is not None and artifact.plan_id == plan.id
            access = participant_access(session, job, artifact if matching else None)
            if artifact is None:
                availability = PdfAvailability(job.attempt, "missing", False, None, False)
            elif not matching:
                availability = PdfAvailability(job.attempt, "invalid", None, False, False)
            else:
                availability = artifact_availability(deps.storage, artifact)
                if availability.available and job.status != "completed":
                    availability = PdfAvailability(
                        job.attempt, "unavailable", True, True, True, availability.verified_at
                    )
            date = long_date(artifact.created_at if matching else plan.created_at)
            # O renderer valida o documento completo e só então projeta para o Jinja.
            fragment = spec.templates.render_preview(
                plan.document,
                lead={"name": identity.name},
                form={"slug": spec.slug, "title": spec.title},
                scores=submission.scores or {},
                generated_date=date,
            )
            projection = PlanProjection(
                job.public_id,
                submission.form_slug,
                submission.form_version,
                submission.form_version_number,
                job.attempt,
                "participant",
                spec.project_plan(plan.document, audience="participant"),
            )
            origin = origin_of(deps.settings.public_base_url)
            html = previews.render_participant_result(
                fragment,
                title=spec.title,
                generated_date=date,
                held=access.state == "held",
                pdf_state=availability.state,
                base_href=deps.settings.public_base_url.rstrip("/") + "/",
                content_policy=content_policy(scripts=False, origin=origin, header=False),
            )
        except (
            TerminalError,
            PlanProjectionError,
            TemplateProblem,
            DefinitionError,
            ReleaseRefused,
        ):
            raise PreviewUnavailable(
                "A prévia da tentativa atual não está disponível. Confira a versão histórica do formulário."
            ) from None
        return ParticipantResultPreview(html, projection, access, availability)
