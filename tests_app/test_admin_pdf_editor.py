"""O editor do PDF e da prévia no admin, pela costura HTTP (TestClient): a operadora edita o HTML e
o CSS do PDF, a prévia da página de resultado e as notas que o PDF pode mostrar; vê a prévia com o
plano de exemplo ou o do último teste; e um erro de template volta com a linha. O PDF é o do
WeasyPrint de verdade."""

from __future__ import annotations

import copy
import json
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_admin import FakeClock, login
from test_admin_destinations import ADMIN_ENV
from test_admin_drafts import Fields, ScriptedLlm, TestTrigger, editor, hidden, post
from test_admin_forms import import_form, section_of
from test_admin_scoring import DEFINITION, SCORING, SLUG, scoring_fields
from test_public import form_fields, tags, text
from test_weasyprint_pdf import squash, text_of

from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.admin.views import DRAFT_REF
from app.api.main import create_app
from app.deps import Deps
from app.forms import drafts
from app.settings import Settings

DRAFT = f"/admin/forms/{SLUG}/draft"
PAGE = f"{DRAFT}/pdf"
FORM = "pdf-form"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def worker(deps: Deps, clock: FakeClock) -> TestTrigger:
    return TestTrigger(deps, clock)


@pytest.fixture
def client(deps: Deps, clock: FakeClock, worker: TestTrigger) -> TestClient:
    """A operadora no admin, com o formulário de pontuação importado (versão 1) e o PDF de verdade."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, test_trigger=worker, clock=clock),
        follow_redirects=False,
    )
    assert login(current).status_code == 303
    assert import_form(current, DEFINITION).status_code == 303
    # O PDF de verdade só depois da importação: ela renderiza o PDF de exemplo, o que o editor
    # não precisa provar a cada teste (`test_the_scoring_form_imports_with_the_real_pdf`).
    deps.pdf = WeasyPrintRenderer()
    return current


def fields_of(client: TestClient) -> Fields:
    """O formulário do editor do PDF como o navegador o mandaria sem mudança nenhuma."""
    page = client.get(PAGE)
    assert page.status_code == 200, page.text
    return form_fields(page.text, FORM)


def stored(deps: Deps) -> dict[str, Any]:
    with deps.sessions() as session:
        found = drafts.load(session, SLUG)
    assert found is not None
    return found.document


def save(client: TestClient, change: Callable[[Fields], Any] | None = None) -> httpx.Response:
    fields = fields_of(client)
    if change is not None:
        change(fields)
    return post(client, PAGE, **fields)


def problems_shown(response: httpx.Response) -> str:
    found = re.search(r'<div[^>]*id="problemas".*?</ul>', response.text, re.DOTALL)
    assert found is not None, "a página não mostrou os problemas"
    return text(found.group(0))


def with_html(markup: str) -> Callable[[Fields], None]:
    def change(fields: Fields) -> None:
        fields["pdf_html"] = markup

    return change


PUBLISHED = json.loads(DEFINITION)


# --- a página ---------------------------------------------------------------------------------


def test_the_editor_shows_the_templates_the_notes_the_previews_and_the_components(
    client: TestClient,
) -> None:
    page = client.get(PAGE)

    assert page.status_code == 200
    fields = form_fields(page.text, FORM)
    assert fields["pdf_html"] == PUBLISHED["pdf"]["html"]
    assert fields["pdf_css"] == PUBLISHED["pdf"]["css"]
    assert fields["preview"] == PUBLISHED["preview"]
    assert fields["pdf_scores"] == "porte"  # a nota liberada está marcada, a outra não
    assert fields["plan"] == "example" and fields["expected"] == hidden(page.text, "expected")
    notes = text(section_of(page, "notas"))
    assert "porte" in notes and "alcance" in notes and "A prioridade e a persona nunca" in notes
    reference = text(section_of(page, "componentes"))
    assert "brand.chip(texto)" in reference and "brand.paragraphs(texto)" in reference
    assert "Só no PDF" in reference
    assert tags(page.text, "iframe", name="pdf-preview") and tags(
        page.text, "iframe", name="result-preview"
    )
    assert PAGE in client.get(DRAFT).text  # o editor do rascunho leva até ele


def test_the_previews_open_in_frames_without_javascript(client: TestClient) -> None:
    page = client.get(PAGE)

    (pdf_button,) = tags(page.text, "button", formtarget="pdf-preview")
    (result_button,) = tags(page.text, "button", formtarget="result-preview")
    assert pdf_button["formaction"] == f"{PAGE}/preview"
    assert result_button["formaction"] == f"{PAGE}/result"


def test_the_editor_needs_a_session(client: TestClient, deps: Deps) -> None:
    anonymous = TestClient(create_app(), follow_redirects=False)

    assert anonymous.get(PAGE).status_code == 303
    assert anonymous.post(PAGE, data={}, headers={"origin": "http://testserver"}).status_code in (
        303,
        403,
    )


def test_a_post_without_the_csrf_token_changes_nothing(client: TestClient, deps: Deps) -> None:
    fields = fields_of(client)
    fields["pdf_html"] = "<p>x</p>"
    fields.pop("csrf_token", None)

    response = client.post(PAGE, data=fields, headers={"origin": "http://testserver"})

    assert response.status_code == 403
    assert stored(deps)["pdf"]["html"] == PUBLISHED["pdf"]["html"]


def test_the_json_of_the_main_editor_no_longer_carries_the_pdf_and_the_preview(
    client: TestClient,
) -> None:
    rest = json.loads(editor(client, DRAFT)["definition"])

    assert "pdf" not in rest and "preview" not in rest
    assert "scoring" not in rest and "examples" in rest  # a pontuação tem os campos dela


def test_saving_the_main_editor_keeps_the_pdf_and_the_preview_as_they_were(
    client: TestClient, deps: Deps
) -> None:
    assert save(client, with_html("<p>{{ plan.titulo }}</p>")).status_code == 303
    fields = editor(client, DRAFT)
    fields["prompt"] += "Mais uma linha.\n"

    assert post(client, DRAFT, **fields).status_code == 303

    assert stored(deps)["pdf"]["html"] == "<p>{{ plan.titulo }}</p>"


def test_a_pdf_in_the_json_of_the_main_editor_is_sent_to_the_pdf_editor(
    client: TestClient,
) -> None:
    fields = editor(client, DRAFT)
    rest = json.loads(fields["definition"])
    fields["definition"] = json.dumps({**rest, "pdf": PUBLISHED["pdf"]})

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    assert "PDF: fica no editor do PDF e da prévia; tire do JSON" in text(response.text)


def test_a_problem_in_the_pdf_found_by_the_main_editor_links_to_the_pdf_editor(
    client: TestClient, deps: Deps
) -> None:
    """Tirar do JSON uma nota que o PDF libera quebra o PDF: o problema leva até as notas dele."""
    fields = editor(client, DRAFT)
    without_porte = {**SCORING, "scores": [s for s in SCORING["scores"] if s["name"] != "porte"]}
    for name in [name for name in fields if name.startswith("scoring.")]:
        del fields[name]
    fields.update(scoring_fields(without_porte))

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    assert "Notas do PDF: não é uma nota da pontuação" in problems_shown(response)
    assert f'href="{PAGE}#f-pdf_scores"' in response.text


# --- salvar -----------------------------------------------------------------------------------


def test_saving_changes_the_draft_and_leaves_the_published_version_alone(
    client: TestClient, deps: Deps
) -> None:
    def change(fields: Fields) -> None:
        fields["pdf_html"] = "<h1>{{ plan.titulo }}</h1>"
        fields["pdf_css"] = "h1 { color: #310C0C; }"
        fields["preview"] = "<p>{{ plan.resumo }}</p>"
        fields["pdf_scores"] = ["porte", "alcance"]

    response = save(client, change)

    assert response.status_code == 303 and response.headers["location"] == PAGE
    draft = stored(deps)
    assert draft["pdf"] == {
        "html": "<h1>{{ plan.titulo }}</h1>",
        "css": "h1 { color: #310C0C; }",
        "scores": ["porte", "alcance"],
    }
    assert draft["preview"] == "<p>{{ plan.resumo }}</p>"
    again = client.get(PAGE)
    assert form_fields(again.text, FORM)["pdf_html"] == "<h1>{{ plan.titulo }}</h1>"
    assert "Rascunho pendente" in text(again.text)
    with deps.sessions() as session:
        published = drafts.load(session, SLUG)
    assert published is not None and published.current is not None
    assert published.current.definition["pdf"]["html"] == PUBLISHED["pdf"]["html"]


def test_unchecking_every_note_keeps_the_list_empty_and_the_page_shows_none_marked(
    client: TestClient, deps: Deps
) -> None:
    def change(fields: Fields) -> None:
        del fields["pdf_scores"]
        fields["pdf_html"] = "<p>{{ plan.titulo }}</p>"  # o do exemplo lê as notas liberadas
        fields["preview"] = "<p>{{ plan.resumo }}</p>"

    assert save(client, change).status_code == 303

    assert stored(deps)["pdf"]["scores"] == []
    assert "pdf_scores" not in fields_of(client)


def test_saving_the_page_untouched_leaves_no_pending_draft(client: TestClient) -> None:
    response = save(client)

    assert response.status_code == 303
    assert "Rascunho pendente" not in text(client.get(PAGE).text)


def test_the_line_endings_of_the_browser_do_not_change_the_template(
    client: TestClient, deps: Deps
) -> None:
    response = save(client, with_html("<p>a</p>\r\n<p>{{ plan.titulo }}</p>\r\n"))

    assert response.status_code == 303
    assert stored(deps)["pdf"]["html"] == "<p>a</p>\n<p>{{ plan.titulo }}</p>\n"


BAD_FIELD = {"pdf_html": "PDF (HTML)", "preview": "Prévia da página de resultado"}


@pytest.mark.parametrize(
    ("field", "markup", "problem", "excerpt"),
    [
        (
            "pdf_html",
            "<h1>{{ plan.titulo }}</h1>\n<p>{{ plan.subtitulo }}</p>",
            "PDF (HTML), linha 2: plan.subtitulo não é um campo do plano",
            "<p>{{ plan.subtitulo }}</p>",
        ),
        (
            "pdf_html",
            "<p>ok</p>\n\n<p>{{ plan.titulo ) }}</p>",
            "PDF (HTML), linha 3: sintaxe inválida: símbolo inesperado: )",
            "<p>{{ plan.titulo ) }}</p>",
        ),
        (
            "pdf_html",
            "<p>{{ plan.titulo }}</p>\n{{ brand.cartao('x') }}",
            "PDF (HTML), linha 2: brand.cartao não é um componente da marca",
            "{{ brand.cartao('x') }}",
        ),
        (
            "preview",
            "<p>{{ plan.resumo }}</p>\n<p>{{ scores.alcance }}</p>",
            "Prévia da página de resultado, linha 2: scores.alcance é uma nota que o PDF e a prévia",
            "<p>{{ scores.alcance }}</p>",
        ),
        (
            "preview",
            "<p>{% for a in plan.titulo %}</p>",
            "Prévia da página de resultado, linha 1: sintaxe inválida",
            "<p>{% for a in plan.titulo %}</p>",
        ),
    ],
    ids=["field", "syntax", "component", "unreleased-note", "unclosed-block"],
)
def test_a_template_error_is_shown_on_the_page_with_its_line_and_the_text_comes_back(
    client: TestClient, deps: Deps, field: str, markup: str, problem: str, excerpt: str
) -> None:
    fields = fields_of(client)
    fields[field] = markup

    response = post(client, PAGE, **fields)

    assert response.status_code == 422
    shown = problems_shown(response)
    assert problem in shown and excerpt in shown
    assert tags(response.text, "textarea", name=field, **{"aria-invalid": "true"})
    assert form_fields(response.text, FORM)[field] == markup  # o texto volta para corrigir
    assert stored(deps)["pdf"]["html"] == PUBLISHED["pdf"]["html"]  # nada foi salvo
    assert f'href="#f-{field}"' in response.text  # o problema leva ao campo


def test_a_note_that_does_not_exist_is_refused_on_the_notes(client: TestClient) -> None:
    fields = fields_of(client)
    fields["pdf_scores"] = ["porte", "inventada"]

    response = post(client, PAGE, **fields)

    assert response.status_code == 422
    assert "Notas do PDF: não é uma nota da pontuação" in problems_shown(response)


@pytest.mark.parametrize(
    ("markup", "problem"),
    [
        (
            '<p>ok</p>\n<img src="https://example.test/logo.png">',
            "PDF (HTML), linha 2: carrega um recurso de fora do app",
        ),
        (
            '<img\n  src="//example.test/logo.png">',
            "PDF (HTML), linha 1: carrega um recurso de fora do app",
        ),
        (
            '<link rel="stylesheet" href="https://example.test/estilo.css">',
            "PDF (HTML), linha 1: carrega um recurso de fora do app",
        ),
        (
            '<p style="background: url(http://example.test/fundo.png)">x</p>',
            "PDF (HTML), linha 1: carrega um recurso de fora do app",
        ),
        (
            "<style>\n@import url('https://example.test/a.css');\n</style>",
            "PDF (HTML), linha 2: carrega um recurso de fora do app",
        ),
        (
            '<img src="brands/../../../etc/passwd">',
            "PDF (HTML), linha 1: carrega um recurso de fora do app",
        ),
    ],
)
def test_a_pdf_that_loads_something_from_outside_the_app_is_refused_with_its_line(
    client: TestClient, markup: str, problem: str
) -> None:
    response = save(client, with_html(markup))

    assert response.status_code == 422
    assert problem in problems_shown(response)


def test_the_css_cannot_load_resources_from_outside_the_app_either(client: TestClient) -> None:
    def change(fields: Fields) -> None:
        fields["pdf_css"] = (
            "body { color: red; }\n.capa { background: url('https://example.test/a.png'); }"
        )

    response = save(client, change)

    assert response.status_code == 422
    assert "PDF (CSS), linha 2: carrega um recurso de fora do app" in problems_shown(response)


@pytest.mark.parametrize(
    "markup",
    [
        '<img src="brand:logo-preta.svg">',
        '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">',
        '<img src="{{ plan.titulo }}.png">',
        '<a href="https://example.test/contato">Contato</a>',
        '{# <img src="https://example.test/x.png"> #}<p>ok</p>',
    ],
)
def test_the_files_of_the_app_data_uris_and_plain_links_are_allowed(
    client: TestClient, markup: str
) -> None:
    assert save(client, with_html(markup)).status_code == 303


def test_a_stale_page_changes_nothing_and_keeps_the_text(client: TestClient, deps: Deps) -> None:
    stale = fields_of(client)
    assert save(client, with_html("<p>primeira</p>")).status_code == 303
    stale["pdf_html"] = "<p>de outra aba</p>"

    response = post(client, PAGE, **stale)

    assert response.status_code == 409
    assert "O rascunho mudou desde que você abriu esta página" in text(response.text)
    assert form_fields(response.text, FORM)["pdf_html"] == "<p>de outra aba</p>"
    assert stored(deps)["pdf"]["html"] == "<p>primeira</p>"


def test_the_editor_of_a_form_that_does_not_exist_is_a_404(client: TestClient) -> None:
    assert client.get("/admin/forms/nao-existe/draft/pdf").status_code == 404


# --- a prévia do PDF --------------------------------------------------------------------------


def preview(client: TestClient, change: Callable[[Fields], Any] | None = None) -> httpx.Response:
    fields = fields_of(client)
    if change is not None:
        change(fields)
    return post(client, f"{PAGE}/preview", **fields)


def test_the_pdf_preview_renders_the_text_on_the_page_without_saving_it(
    client: TestClient, deps: Deps
) -> None:
    response = preview(client, with_html("<h1>Rascunho de teste</h1><p>{{ plan.titulo }}</p>"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline")
    assert response.content.startswith(b"%PDF")
    pdf = squash(text_of(response.content))
    assert "Rascunhodeteste" in pdf and squash(PUBLISHED["examples"]["plan"]["titulo"]) in pdf
    assert stored(deps)["pdf"]["html"] == PUBLISHED["pdf"]["html"]  # a prévia não grava


def test_the_pdf_preview_can_be_framed_by_the_admin_only(client: TestClient) -> None:
    response = preview(client)

    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert client.get(PAGE).headers["x-frame-options"] == "DENY"  # a página do editor, não


def test_the_pdf_preview_shows_only_the_released_notes_of_the_example_answers(
    client: TestClient,
) -> None:
    listing = "{% for name in scores %}[{{ name }}={{ scores[name] }}]{% endfor %}"

    def change(fields: Fields) -> None:
        fields["pdf_html"] = f"<p>{listing}</p>"
        fields["pdf_scores"] = ["alcance"]

    response = preview(client, change)

    assert response.status_code == 200
    pdf = squash(text_of(response.content))
    assert "[alcance=25]" in pdf and "porte" not in pdf  # 2 canais nas respostas de exemplo


def test_the_pdf_preview_never_loads_anything_outside_the_app(client: TestClient) -> None:
    response = preview(client, with_html('<p>x</p><img src="http://example.test/a.png">'))

    assert response.status_code == 422
    assert "carrega um recurso de fora do app" in text(response.text)


def test_a_template_error_in_the_preview_shows_the_line_instead_of_a_pdf(
    client: TestClient,
) -> None:
    response = preview(client, with_html("<p>ok</p>\n<p>{{ plan.nada }}</p>"))

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert "PDF (HTML), linha 2: plan.nada não é um campo do plano" in text(response.text)
    assert response.headers["x-frame-options"] == "SAMEORIGIN"


def test_the_pdf_preview_reports_a_render_failure_without_a_stack_trace(
    client: TestClient, deps: Deps
) -> None:
    def change(fields: Fields) -> None:
        fields["pdf_html"] = "<p>x</p>"
        fields["pdf_css"] = "@import url('brands/nao-existe.css');"

    response = preview(client, change)

    assert response.status_code == 422
    assert "O PDF não foi gerado" in text(response.text) and "Traceback" not in response.text


# --- o plano do último teste ------------------------------------------------------------------

TEST_PLAN_TITLE = "Plano que só o último teste escreveu"


class PlanLlm(ScriptedLlm):
    """O LLM do teste: devolve o plano de exemplo com outro título."""

    def generate(self, spec: Any, answers: Any) -> Any:
        result = super().generate(spec, answers)
        result.document["titulo"] = TEST_PLAN_TITLE
        return result


def run_a_test(client: TestClient, deps: Deps, worker: TestTrigger) -> None:
    deps.llm = PlanLlm()
    response = post(client, f"{DRAFT}/tests")
    assert response.status_code == 303, response.text
    worker.run()


def test_the_pdf_preview_can_use_the_plan_of_the_last_test(
    client: TestClient, deps: Deps, worker: TestTrigger
) -> None:
    page = client.get(PAGE)
    assert "Nenhum teste com sucesso guardado" in text(page.text)
    run_a_test(client, deps, worker)

    def change(fields: Fields) -> None:
        fields["plan"] = "test"

    response = preview(client, change)

    assert response.status_code == 200
    pdf = squash(text_of(response.content))
    assert squash(TEST_PLAN_TITLE) in pdf
    assert squash(PUBLISHED["examples"]["plan"]["titulo"]) not in pdf
    assert "Plano do último teste (" in text(client.get(PAGE).text)


def test_the_pdf_preview_of_the_last_test_says_so_when_there_is_none(client: TestClient) -> None:
    def change(fields: Fields) -> None:
        fields["plan"] = "test"

    response = preview(client, change)

    assert response.status_code == 409
    assert "Ainda não há um teste com sucesso" in text(response.text)


def test_the_plan_of_a_test_that_the_model_no_longer_accepts_is_reported(
    client: TestClient, deps: Deps, worker: TestTrigger
) -> None:
    run_a_test(client, deps, worker)
    fields = editor(client, DRAFT)
    fields["plan.fields.0.max_length"] = "5"  # o título do teste não cabe mais
    rest = json.loads(fields["definition"])
    rest["examples"]["plan"]["titulo"] = "Curto"
    fields["definition"] = json.dumps(rest)
    assert post(client, DRAFT, **fields).status_code == 303

    def change(page: Fields) -> None:
        page["plan"] = "test"

    response = preview(client, change)

    assert response.status_code == 409
    assert "não vale mais com o modelo do plano" in text(response.text)


# --- a prévia da página de resultado ----------------------------------------------------------


def result(client: TestClient, change: Callable[[Fields], Any] | None = None) -> httpx.Response:
    fields = fields_of(client)
    if change is not None:
        change(fields)
    return post(client, f"{PAGE}/result", **fields)


def test_the_result_preview_shows_the_template_in_the_page_the_lead_sees(
    client: TestClient, deps: Deps
) -> None:
    def change(fields: Fields) -> None:
        fields["preview"] = (
            '<section class="section"><h2>Sua prévia</h2><p>{{ plan.titulo }}</p></section>'
        )

    response = result(client, change)

    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    page = text(response.text)
    assert (
        "Seu plano está pronto" in page and "Baixar PDF" in page
    )  # a moldura da página de verdade
    assert "Sua prévia" in page and PUBLISHED["examples"]["plan"]["titulo"] in page
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "<script" not in response.text
    assert stored(deps)["preview"] == PUBLISHED["preview"]  # a prévia não grava


def test_the_result_preview_uses_the_plan_of_the_last_test_when_asked(
    client: TestClient, deps: Deps, worker: TestTrigger
) -> None:
    run_a_test(client, deps, worker)

    def change(fields: Fields) -> None:
        fields["plan"] = "test"
        fields["preview"] = "<p>{{ plan.titulo }}</p>"

    assert TEST_PLAN_TITLE in text(result(client, change).text)


def test_the_result_preview_shows_only_the_released_notes(client: TestClient) -> None:
    def change(fields: Fields) -> None:
        fields["pdf_html"] = "<p>{{ plan.titulo }}</p>"
        fields["preview"] = "<p>{% for n in scores %}[{{ n }}]{% endfor %}</p>"
        fields["pdf_scores"] = ["alcance"]

    assert "[alcance]" in text(result(client, change).text)
    assert "[porte]" not in text(result(client, change).text)


def test_a_template_error_in_the_result_preview_shows_the_line(client: TestClient) -> None:
    def change(fields: Fields) -> None:
        fields["preview"] = "<p>ok</p>\n<p>{{ plan.nada }}</p>"

    response = result(client, change)

    assert response.status_code == 422
    assert "Prévia da página de resultado, linha 2: plan.nada não é um campo do plano" in text(
        response.text
    )


# --- a publicação -----------------------------------------------------------------------------


def test_publishing_refuses_a_pdf_that_loads_something_from_outside_the_app(
    client: TestClient, deps: Deps
) -> None:
    document = copy.deepcopy(stored(deps))
    document["pdf"]["html"] += '\n<img src="https://example.test/a.png">'
    document["slug"] = "recurso-de-fora"
    document["consent"]["version"] = "recurso-de-fora-v1"

    response = import_form(client, json.dumps(document))

    assert response.status_code == 422
    assert "carrega um recurso de fora do app" in text(response.text)


def test_the_comparison_lists_the_notes_the_pdf_may_show(client: TestClient) -> None:
    def change(fields: Fields) -> None:
        fields["pdf_scores"] = ["porte", "alcance"]

    assert save(client, change).status_code == 303

    page = client.get(f"/admin/forms/{SLUG}/versions/compare?from=1&to={DRAFT_REF}")
    assert page.status_code == 200
    assert "Notas que o PDF e a prévia podem mostrar" in text(page.text)


# --- o que se manda para ser lido não custa mais que o tamanho dele ---------------------------


def test_saving_a_template_that_weighs_is_refused_in_time_and_the_problem_is_shown(
    client: TestClient, deps: Deps
) -> None:
    """O salvar da página lê o template pelo mesmo leitor da API: comentários que não fecham saíam
    quadráticos, e o `{{ ((((` fundo demais estourava o Jinja (500). Agora são problemas do campo."""
    started = time.perf_counter()
    open_comments = save(client, with_html("{#" * 35_000))
    deep = save(client, with_html("{{ " + "(" * 4000 + "1" + ")" * 4000 + " }}"))

    assert time.perf_counter() - started < 3
    assert open_comments.status_code == deep.status_code == 422
    assert "PDF (HTML)" in problems_shown(open_comments)
    assert "aninha fundo demais" in problems_shown(deep)
    assert stored(deps)["pdf"]["html"] == PUBLISHED["pdf"]["html"]  # nada foi salvo
