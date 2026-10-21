"""Recibo local de conclusão compartilhado pela jornada pública e pela prévia administrativa."""

from app.forms import FormSpec
from app.web.templating import templates


def render_completion(
    spec: FormSpec, *, preview: bool = False, completion=None, **frame: object
) -> str:
    """Texto sempre escapado; a prévia não oferece navegação externa nem executa redirect."""
    return templates.get_template("public/completion.html").render(
        spec=spec, completion=completion or spec.completion, preview=preview, **frame
    )


def accepted_completion(deps, accepted, *, now):
    """Uma leitura independente só após a transação de aceite confirmar."""
    from app.destinations import SecretCipher
    from app.handoffs import CompletionView, completion_view
    from app.models import Submission

    if accepted.form.completion.handoff is None:
        completion = accepted.form.completion
        return CompletionView(
            completion.mode, completion.message, completion.button_label, completion.url
        )
    with deps.sessions.begin() as session:
        submission_id = (
            accepted.submission.id
            if hasattr(accepted, "submission")
            else accepted.job.submission_id
        )
        submission = session.get_one(Submission, submission_id)
        return completion_view(
            session,
            submission,
            accepted.form,
            cipher=SecretCipher.from_settings(deps.settings),
            now=now,
        )
