"""As ações do admin sobre as prévias do rascunho (`app.admin.actions.previews`): o contrato do
rascunho com o PDF e a prévia que a chamada manda (sem gravar), o plano de exemplo ou o do último
teste, o PDF do adaptador do pedido, a página de resultado, a página pública e os erros como dado (a
linha do template, o plano do teste que falta).

A página (`test_admin_pdf_editor.py`, `test_admin_form_preview.py`) e a API
(`test_admin_api_previews.py`) chamam estas funções; os testes delas provam que a página não mudou.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from admin_api_support import snapshot
from test_admin_scoring import DEFINITION as SCORED_DEFINITION
from test_admin_scoring import SLUG

from app.admin.actions import previews
from app.deps import Deps
from app.errors import FailureCode, TerminalError
from app.forms import UnknownForm, draft_tests, drafts, stored
from app.forms.definition import DefinitionError
from app.forms.draft_previews import EXAMPLE, TEST, PreviewUnavailable

NOW = datetime(2027, 1, 15, 12, 0, tzinfo=UTC)
PUBLISHED = json.loads(SCORED_DEFINITION)
TEST_PLAN_TITLE = "Plano que só o último teste escreveu"


@pytest.fixture
def published(deps: Deps) -> None:
    stored.import_definition(deps, SCORED_DEFINITION, actor="testes")


def draft_of(deps: Deps) -> drafts.Draft:
    with deps.sessions() as session:
        found = drafts.load(session, SLUG)
    assert found is not None
    return found


def preview(
    deps: Deps, changes: previews.TemplateChanges | None = None, *, source: str = EXAMPLE
) -> Any:
    return previews.template_preview(
        deps, SLUG, changes or previews.TemplateChanges(), source=source, now=NOW
    )


class PlanLlm:
    """O LLM do teste: devolve o plano de exemplo com outro título."""

    def generate(self, spec: Any, answers: Any) -> Any:
        from app.adapters.llm import LlmResult

        plan = spec.plan_model.model_validate(spec.example_plan).model_dump(mode="json")
        plan["titulo"] = TEST_PLAN_TITLE
        return LlmResult(document=plan, model="gpt-teste", usage={})


def run_a_test(deps: Deps) -> None:
    deps.llm = PlanLlm()  # type: ignore[assignment]
    test_id = draft_tests.start(deps, SLUG, actor="chave:testes", answers=None, day="", now=NOW)
    draft_tests.run(deps, test_id, now=lambda: NOW)


# --- o que a chamada manda: a mudança sobre o rascunho salvo ------------------------------------


def test_the_changes_take_the_place_of_the_saved_texts_and_leave_the_rest() -> None:
    saved = drafts.TemplateTexts(pdf_html="a", pdf_css="b", preview="c", scores=("x",))

    only_html = previews.TemplateChanges(pdf_html="novo").applied(saved)
    everything = previews.TemplateChanges(
        pdf_html="1", pdf_css="2", preview="3", scores=()
    ).applied(saved)

    assert only_html == drafts.TemplateTexts(
        pdf_html="novo", pdf_css="b", preview="c", scores=("x",)
    )
    assert everything == drafts.TemplateTexts(pdf_html="1", pdf_css="2", preview="3", scores=())
    assert previews.TemplateChanges().applied(saved) == saved


def test_changes_of_the_page_are_every_text_the_page_sent() -> None:
    sent = drafts.TemplateTexts(pdf_html="a", pdf_css="", preview="c", scores=())

    changes = previews.TemplateChanges.of(sent)

    assert changes.applied(drafts.TemplateTexts("x", "y", "z", ("q",))) == sent


# --- a prévia dos templates ---------------------------------------------------------------------


def test_a_preview_without_changes_is_the_saved_draft_with_the_example_plan(
    deps: Deps, published: None
) -> None:
    found = preview(deps)

    assert found.source == EXAMPLE
    assert found.plan == found.spec.example_plan
    assert found.spec.title == PUBLISHED["title"]
    assert found.context["generated_at"] == NOW
    assert found.context["form"] == {"slug": SLUG, "title": PUBLISHED["title"]}


def test_the_changes_are_what_the_preview_renders_and_nothing_is_saved(
    deps: Deps, published: None
) -> None:
    before = snapshot()

    found = preview(deps, previews.TemplateChanges(preview="<p>Só na prévia</p>"))

    assert "Só na prévia" in previews.render_result(found)
    assert snapshot() == before
    assert draft_of(deps).document["preview"] == PUBLISHED["preview"]


def test_a_template_error_comes_with_the_path_and_the_line_and_never_the_value(
    deps: Deps, published: None
) -> None:
    broken = previews.TemplateChanges(pdf_html="<p>ok</p>\n<p>{{ plan.nada }}</p>")

    with pytest.raises(DefinitionError) as refused:
        preview(deps, broken)

    (problem,) = refused.value.problems
    assert (problem.path, problem.line) == ("pdf.html", 2)
    assert "plan.nada" in problem.message


def test_a_pdf_that_asks_for_something_outside_the_app_is_refused_with_its_line(
    deps: Deps, published: None
) -> None:
    outside = previews.TemplateChanges(pdf_html='<p>x</p>\n<img src="http://example.test/a.png">')

    with pytest.raises(DefinitionError) as refused:
        preview(deps, outside)

    (problem,) = refused.value.problems
    assert problem.line == 2 and "recurso de fora do app" in problem.message


def test_a_note_that_does_not_exist_is_refused_on_the_notes(deps: Deps, published: None) -> None:
    with pytest.raises(DefinitionError) as refused:
        preview(deps, previews.TemplateChanges(scores=("porte", "inventada")))

    assert [problem.path for problem in refused.value.problems] == ["pdf.scores.1"]


def test_the_released_notes_are_the_ones_the_changes_name(deps: Deps, published: None) -> None:
    listing = "{% for name in scores %}[{{ name }}]{% endfor %}"

    found = preview(
        deps,
        previews.TemplateChanges(pdf_html="<p>x</p>", preview=listing, scores=("alcance",)),
    )

    shown = previews.render_result(found)
    assert "[alcance]" in shown and "[porte]" not in shown


def test_the_plan_of_the_last_test_is_the_one_used_when_asked(deps: Deps, published: None) -> None:
    run_a_test(deps)

    found = preview(deps, source=TEST)

    assert found.source == TEST and found.plan["titulo"] == TEST_PLAN_TITLE
    assert preview(deps).plan["titulo"] != TEST_PLAN_TITLE


def test_asking_for_the_last_test_when_there_is_none_says_so(deps: Deps, published: None) -> None:
    with pytest.raises(PreviewUnavailable, match="Ainda não há um teste com sucesso"):
        preview(deps, source=TEST)


def test_a_plan_the_model_no_longer_accepts_says_so_without_the_plan(
    deps: Deps, published: None
) -> None:
    run_a_test(deps)
    document = draft_of(deps).document
    document["plan"]["fields"][0]["max_length"] = 5  # o título do teste não cabe mais
    document["examples"]["plan"]["titulo"] = "Curto"
    drafts.replace_document(
        deps, SLUG, document, actor="chave:testes", expected=draft_of(deps).token
    )

    with pytest.raises(PreviewUnavailable) as unavailable:
        preview(deps, source=TEST)

    assert "não vale mais com o modelo do plano" in str(unavailable.value)
    assert TEST_PLAN_TITLE not in str(unavailable.value)


def test_a_form_that_does_not_exist_is_refused(deps: Deps) -> None:
    with pytest.raises(UnknownForm):
        previews.template_preview(
            deps, "nao-existe", previews.TemplateChanges(), source=EXAMPLE, now=NOW
        )


# --- o PDF --------------------------------------------------------------------------------------


def test_the_pdf_is_the_one_of_the_renderer_of_the_deps(deps: Deps, published: None) -> None:
    rendered = previews.render_pdf(deps, preview(deps))

    assert rendered.content.startswith(b"%PDF") and rendered.renderer == "fake"


def test_a_pdf_the_renderer_cannot_make_is_a_failure_with_the_fixed_message(
    deps: Deps, published: None
) -> None:
    class Failing:
        def render(self, *_: Any) -> Any:
            raise TerminalError(FailureCode.RENDER_FAILED, "WeasyPrint falhou (ValueError)")

    deps.pdf = Failing()  # type: ignore[assignment]

    with pytest.raises(previews.PdfFailed) as failed:
        previews.render_pdf(deps, preview(deps))

    assert failed.value.detail == "WeasyPrint falhou (ValueError)"


# --- a página de resultado ----------------------------------------------------------------------


def test_the_result_page_is_the_page_of_the_lead_with_the_preview_and_a_fake_file(
    deps: Deps, published: None
) -> None:
    html = previews.render_result(
        preview(
            deps, previews.TemplateChanges(preview="<h2>Sua prévia</h2><p>{{ plan.titulo }}</p>")
        )
    )

    assert html.startswith("<!doctype html>")
    assert "Seu plano está pronto" in html and "plano-exemplo.pdf" in html
    assert "Sua prévia" in html and PUBLISHED["examples"]["plan"]["titulo"] in html
    assert "<base" not in html and "<script" not in html


def test_the_result_page_says_where_the_page_lives_only_when_asked(
    deps: Deps, published: None
) -> None:
    found = preview(deps)

    plain = previews.render_result(found)
    based = previews.render_result(found, base_href="https://planos.exemplo.test/")

    assert '<base href="https://planos.exemplo.test/">' in based
    assert based.replace('\n<base href="https://planos.exemplo.test/">', "") == plain


def test_a_page_that_fails_to_render_is_a_problem_with_the_template_and_no_value(
    deps: Deps, published: None
) -> None:
    """O erro de template que só aparece ao renderizar com o plano de agora (o do último teste, que
    o modelo do rascunho pode já não aceitar) volta como problema do `preview`, sem o plano."""
    found = replace(preview(deps), plan={"titulo": 7})

    with pytest.raises(DefinitionError) as refused:
        previews.render_result(found)

    (problem,) = refused.value.problems
    assert problem.path == "preview" and "7" not in problem.message


# --- a página pública ---------------------------------------------------------------------------


def test_the_public_page_has_every_step_and_opens_at_the_one_asked(
    deps: Deps, published: None
) -> None:
    page = previews.public_page(deps, SLUG, 2)

    assert page.number == 2 and len(page.steps) == len(page.screen.steps) > 1
    assert page.spec.slug == SLUG


def test_the_public_page_opens_at_the_nearest_step_when_it_is_out_of_range(
    deps: Deps, published: None
) -> None:
    last = len(previews.public_page(deps, SLUG, 1).steps)

    assert [previews.public_page(deps, SLUG, asked).number for asked in (0, 1, 999)] == [1, 1, last]


def test_the_public_page_is_the_page_of_the_form_without_form_and_with_preview_marks(
    deps: Deps, published: None
) -> None:
    html = previews.render_public_page(previews.public_page(deps, SLUG, 1))

    assert html.startswith("<!doctype html>")
    assert "data-preview" in html and 'data-start-step="0"' in html
    assert "<form" not in html and "<base" not in html


def test_the_public_page_says_where_the_page_lives_only_when_asked(
    deps: Deps, published: None
) -> None:
    page = previews.public_page(deps, SLUG, 1)

    plain = previews.render_public_page(page)
    based = previews.render_public_page(page, base_href="https://planos.exemplo.test/")

    assert based.count("<base ") == 1 and based.index("<base ") < based.index("<link ")
    assert based.replace('\n<base href="https://planos.exemplo.test/">', "") == plain


def test_the_public_page_of_a_form_that_does_not_exist_is_refused(deps: Deps) -> None:
    with pytest.raises(UnknownForm):
        previews.public_page(deps, "nao-existe", 1)


def test_the_public_page_of_a_draft_that_no_longer_compiles_is_a_definition_error(
    deps: Deps, published: None
) -> None:
    document = draft_of(deps).document
    drafts.replace_document(
        deps,
        SLUG,
        {**document, "intro": "Uma introdução nova."},
        actor="chave:testes",
        expected=draft_of(deps).token,
    )
    with deps.sessions.begin() as session:
        session.execute(
            sa.text(
                "update form_drafts set definition = "
                "jsonb_set(definition, '{prompt}', '\"{{ answers.nao_existe }}\"')"
            )
        )

    with pytest.raises(DefinitionError):
        previews.public_page(deps, SLUG, 1)


# --- a política de conteúdo em `<meta>` -----------------------------------------------------------

POLICY = "default-src 'none'; style-src https://planos.exemplo.test"
ESCAPED = POLICY.replace("'", "&#39;")
META = f'<meta http-equiv="Content-Security-Policy" content="{ESCAPED}">'


def test_the_result_page_carries_the_policy_in_a_meta_only_when_asked_and_before_the_base(
    deps: Deps, published: None
) -> None:
    found = preview(deps)

    plain = previews.render_result(found)
    protected = previews.render_result(
        found, base_href="https://planos.exemplo.test/", content_policy=POLICY
    )

    assert "Content-Security-Policy" not in plain
    assert protected.index("Content-Security-Policy") < protected.index("<base ")
    assert protected.count("Content-Security-Policy") == 1
    assert (
        protected.replace(f"\n{META}", "").replace(
            '\n<base href="https://planos.exemplo.test/">', ""
        )
        == plain
    )


def test_the_public_page_carries_the_policy_in_a_meta_only_when_asked(
    deps: Deps, published: None
) -> None:
    page = previews.public_page(deps, SLUG, 1)

    plain = previews.render_public_page(page)
    protected = previews.render_public_page(page, content_policy=POLICY)

    assert "Content-Security-Policy" not in plain
    assert protected.replace(f"\n{META}", "") == plain
