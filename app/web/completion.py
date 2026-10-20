"""Recibo local de conclusão compartilhado pela jornada pública e pela prévia administrativa."""

from app.forms import FormSpec
from app.web.templating import templates


def render_completion(spec: FormSpec, *, preview: bool = False, **frame: object) -> str:
    """Texto sempre escapado; a prévia não oferece navegação externa nem executa redirect."""
    return templates.get_template("public/completion.html").render(
        spec=spec, completion=spec.completion, preview=preview, **frame
    )
