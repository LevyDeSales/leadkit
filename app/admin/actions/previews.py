"""As ações do admin sobre as prévias do rascunho: o PDF e a página de resultado com o PDF e a prévia
que quem chama manda (sem gravar), e a página pública do rascunho como a lead a veria.

A página (`app.admin.routes`) e a API (`app.admin.api.previews`) chamam as mesmas funções (ADR 0015, "A
página e a API dividem os serviços"): o contrato que a prévia usa, a escolha do plano (o de exemplo
ou o do último teste), o PDF do adaptador do pedido e o HTML de cada página saem daqui, de modo que o
que a API devolve é, byte a byte, o que a página mostra para os mesmos dados. Elas não conhecem
`Request` nem aviso de tela: recebem o rascunho pelo endereço e o instante, devolvem um resultado
tipado ou levantam uma exceção de domínio, e cada lado a traduz (a página, num quadro com o motivo; a
API, em JSON e status).

**Os erros são dados.** O PDF ou a prévia que não compilam levantam `DefinitionError` com os
problemas (o caminho do template e a linha; nunca o valor), o plano do último teste que falta ou que o
modelo não aceita mais é `PreviewUnavailable`, e o PDF que o adaptador não consegue gerar é
`PdfFailed`, com a mensagem fixa do adaptador (ela não traz dado nenhum). Uma página de resultado que
não renderiza com o plano de agora também volta como `DefinitionError`, com o template e a linha.

**Nada aqui grava, audita ou chama o LLM.** O PDF e a página de resultado usam o plano de exemplo do
rascunho ou o do último teste, e as respostas de exemplo: nenhum dado de lead entra. O PDF sai do
adaptador do pedido (o WeasyPrint em produção), que só abre arquivos do app, as imagens do formulário e
`data:`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from markupsafe import Markup

from app.adapters.pdf import RenderedPdf
from app.deps import Deps
from app.errors import TerminalError
from app.forms import FormSpec, Screen, UnknownForm, draft_previews, draft_tests, drafts
from app.forms import assets as form_assets
from app.forms.definition import DefinitionError, Problem, compile_definition
from app.forms.sandbox import TemplateProblem
from app.web.completion import render_completion
from app.web.formview import StepView, conditions_data, steps_view
from app.web.templating import templates

RESULT_TEMPLATE = "admin/form_result_frame.html"
PUBLIC_TEMPLATE = "admin/form_preview_frame.html"


def completion_preview(deps: Deps, slug: str, **frame: object) -> str:
    """A conclusão salva, sem dados de envio, LLM, navegação ou geração de PDF."""
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
    if draft is None:
        raise UnknownForm(slug)
    spec = compile_definition(draft.document, version_number=draft.next_number)
    return render_completion(spec, preview=True, **frame)


class PdfFailed(Exception):
    """O adaptador não conseguiu gerar o PDF. `detail` é a mensagem fixa dele (sem dado do plano, da
    lead nem da exceção original)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class TemplateChanges:
    """O que a chamada manda do PDF e da prévia da página de resultado. `None` é o que o rascunho
    salvo já tem: a prévia vale com o que for mandado, sem gravar, e com o resto do salvo."""

    pdf_html: str | None = None
    pdf_css: str | None = None
    preview: str | None = None
    scores: tuple[str, ...] | None = None  # as notas que o PDF e a prévia podem mostrar

    @classmethod
    def of(cls, texts: drafts.TemplateTexts) -> TemplateChanges:
        """Todos os textos, como o editor do PDF os manda: nenhum fica com o do rascunho salvo."""
        return cls(texts.pdf_html, texts.pdf_css, texts.preview, texts.scores)

    def applied(self, saved: drafts.TemplateTexts) -> drafts.TemplateTexts:
        """Os textos de `saved` com os que esta chamada manda no lugar."""
        return drafts.TemplateTexts(
            pdf_html=saved.pdf_html if self.pdf_html is None else self.pdf_html,
            pdf_css=saved.pdf_css if self.pdf_css is None else self.pdf_css,
            preview=saved.preview if self.preview is None else self.preview,
            scores=saved.scores if self.scores is None else self.scores,
        )


def template_preview(
    deps: Deps,
    slug: str,
    changes: TemplateChanges,
    *,
    source: str,
    now: datetime,
) -> draft_previews.TemplatePreview:
    """O rascunho de `slug` com o PDF e a prévia de `changes` (o resto, do rascunho salvo), sem
    gravar, e o plano de `source` (`draft_previews.EXAMPLE` ou `TEST`, o do último teste com
    sucesso): o que o PDF e a página de resultado recebem de um pedido, com as imagens do formulário
    no conteúdo de agora.

    Levanta `UnknownForm`, `DefinitionError` (o PDF ou a prévia não valem, ou o rascunho salvo não
    compila: o caminho do campo e a linha do template) e `PreviewUnavailable` (o plano do último
    teste não existe ou o modelo do rascunho não o aceita mais)."""
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
        if draft is None:
            raise UnknownForm(slug)
        library = {name: asset.ref for name, asset in form_assets.visible(session, slug).items()}
        tests = draft_tests.recent(session, draft, now=now) if source == draft_previews.TEST else []
    from app.forms.definition import compile_definition

    compile_definition(draft.document).require_generation()
    return draft_previews.preview_of(
        draft,
        changes.applied(drafts.template_texts_of(draft.document)),
        source=source,
        tests=tests,
        now=now,
        assets=library,
    )


def render_pdf(deps: Deps, found: draft_previews.TemplatePreview) -> RenderedPdf:
    """O PDF da prévia, pelo adaptador do pedido (`deps.pdf`): só abre arquivos do app, imagens do
    formulário e `data:`. Levanta `PdfFailed`."""
    try:
        return deps.pdf.render(found.spec, found.plan, found.context)
    except TerminalError as failed:
        raise PdfFailed(failed.detail) from None


def render_result(
    found: draft_previews.TemplatePreview,
    *,
    base_href: str | None = None,
    content_policy: str | None = None,
) -> str:
    """A página de resultado do rascunho como a lead a vê (a mesma moldura de `public/result.html`),
    com a prévia da página no meio e um arquivo para baixar de mentira, sem JavaScript.
    `base_href` é o `<base>` do documento, para quem o mostra fora da origem do app, e `content_policy`,
    a política de conteúdo numa `<meta http-equiv>`, para quando o cabeçalho da resposta se perde.

    Levanta `DefinitionError` se a prévia não renderiza com o plano (o template e a linha)."""
    assert found.spec.templates is not None  # uma definição sempre tem os templates
    try:
        html = found.spec.templates.render_preview(
            found.plan,
            lead=found.context["lead"],
            form=found.context["form"],
            scores=found.context["scores"],
            generated_date=found.generated_date,
        )
    except TemplateProblem as problem:
        raise DefinitionError([Problem(problem.name, problem.detail, problem.line)]) from None
    return _render(
        RESULT_TEMPLATE,
        {"preview": Markup(html), "title": found.spec.title, "date": found.generated_date},
        base_href,
        content_policy,
    )


@dataclass(frozen=True)
class PublicPage:
    """A página pública do rascunho, como a lead a veria, interativa e sem nada para enviar: todas
    as etapas, porque as condições atravessam etapas, e o JavaScript abre a de número `number`."""

    spec: FormSpec
    screen: Screen
    steps: tuple[StepView, ...]
    conditions: list[dict[str, Any]] | None  # as condições para o JavaScript (`conditions_data`)
    number: int  # a etapa em que a prévia abre, a partir de 1


def public_page(deps: Deps, slug: str, step: int) -> PublicPage:
    """A página pública do rascunho guardado de `slug`, aberta na etapa `step` (a partir de 1; fora
    do intervalo, na mais próxima). Levanta `UnknownForm` e `DefinitionError` (o rascunho guardado não
    compila mais: um deploy com o compilador mais rígido)."""
    with deps.sessions() as session:
        draft = drafts.load(session, slug)
    if draft is None:
        raise UnknownForm(slug)
    spec = compile_definition(draft.document, version_number=draft.next_number)
    assert spec.screen is not None  # uma definição sempre tem a página pública
    steps = steps_view(spec, spec.screen)
    number = min(max(step, 1), len(steps))
    return PublicPage(spec, spec.screen, steps, conditions_data(spec), number)


def render_public_page(
    page: PublicPage, *, base_href: str | None = None, content_policy: str | None = None
) -> str:
    """O HTML da página pública do rascunho: a de verdade (`public/_steps.html`, `public.js` e
    `conditions.js`) num `<div>` no lugar do `<form>`, com `data-preview` (o JavaScript não guarda
    nada no aparelho e deixa andar pelas etapas sem conferir as respostas). `base_href` é o `<base>`
    do documento, para quem o mostra fora da origem do app, e `content_policy`, a política de conteúdo
    numa `<meta http-equiv>`, para quando o cabeçalho da resposta se perde."""
    return _render(
        PUBLIC_TEMPLATE,
        {
            "frame": page,
            "spec": page.spec,
            "screen": page.screen,
            "steps": page.steps,
            "conditions_data": page.conditions,
            "preview": True,
            "consent": {"privacy": False, "marketing_email": False, "marketing_whatsapp": False},
            "consent_error": None,
        },
        base_href,
        content_policy,
    )


def _render(
    name: str, context: dict[str, Any], base_href: str | None, content_policy: str | None
) -> str:
    """O template como a página e a API o mostram: o mesmo, e só o `<base>` e a `<meta>` da política a
    mais quando a API os pede. A moldura não usa a requisição, então não precisa de uma."""
    if base_href is not None:
        context = {**context, "base_href": base_href}
    if content_policy is not None:
        context = {**context, "content_policy": content_policy}
    return templates.get_template(name).render(context)
