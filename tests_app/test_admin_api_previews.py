"""As prévias do rascunho na API de admin (`/admin/api/v1`): `render_form_draft_pdf_preview`,
`render_form_draft_result_preview` e `render_form_draft_public_page`, pela costura HTTP.

Agem pelas mesmas ações das páginas do editor do PDF e da prévia (`app.admin.actions.previews`): o
contrato do rascunho com o PDF e a prévia que a chamada manda, a escolha do plano, o PDF do adaptador do
pedido e o HTML de cada página são uma regra só, e a paridade com a página (o mesmo PDF e o mesmo HTML
para os mesmos dados) está nos testes de paridade. **As três só leem**: não gravam, não auditam, não
chamam o LLM e não tocam dado de lead (usam o plano de exemplo ou o do último teste, e as respostas de
exemplo). São de `leitura` e custam 5 fichas do limite de uso. A resposta não é JSON: o PDF é
`application/pdf` e as páginas são `text/html`, com política de conteúdo própria (sem origem, para o
HTML não rodar no contexto do admin). As chaves são sorteadas na execução.
"""

from __future__ import annotations

import io
import json
import re
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_support import ApiClient, audited, built_openapi, make_api, snapshot, url_of
from jobs_world import PEOPLE, build_jobs_world
from PIL import Image
from pypdf import PdfReader
from test_admin import FakeClock, login
from test_admin_actions_previews import TEST_PLAN_TITLE, PlanLlm
from test_admin_drafts import post
from test_admin_forms import DEFINITION, SLUG
from test_admin_scoring import DEFINITION as SCORED_DEFINITION
from test_admin_scoring import SLUG as SCORED
from test_public import text
from test_weasyprint_pdf import squash, text_of

from app.adapters import weasyprint_pdf
from app.adapters.assets import AssetStore
from app.adapters.pdf import RenderedPdf, minimal_pdf
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.admin.actions import previews as preview_actions
from app.deps import Deps
from app.forms import assets, draft_tests, drafts, sandbox, stored

DRAFT_PAGE = f"/admin/forms/{SLUG}/draft"
SENTINEL = "ZZ_SENT_" + secrets.token_hex(8).upper()
Client = Callable[..., ApiClient]
PUBLISHED = json.loads(DEFINITION)
EXAMPLE_TITLE = PUBLISHED["examples"]["plan"]["titulo"]
# Em `http`: o cookie de sessão do TestClient (`http://testserver`) é sem `Secure`, e os testes de
# paridade entram na página do admin com o mesmo cliente.
ORIGIN = "http://planos.exemplo.test"
BASE = f'\n<base href="{ORIGIN}/">'
META = re.compile(r'\n<meta http-equiv="Content-Security-Policy" content="[^"]*">')
UNTOUCHED = {"api_keys"}  # o que a chamada da API muda e a página não: o último uso da chave

PDF_PREVIEW = "render_form_draft_pdf_preview"
RESULT_PREVIEW = "render_form_draft_result_preview"
PUBLIC_PAGE = "render_form_draft_public_page"
TEMPLATE_PREVIEWS = (PDF_PREVIEW, RESULT_PREVIEW)  # as que levam o corpo opcional
PAGES = (RESULT_PREVIEW, PUBLIC_PAGE)  # as que devolvem HTML
ALL = (PDF_PREVIEW, RESULT_PREVIEW, PUBLIC_PAGE)
METHOD = {PDF_PREVIEW: "POST", RESULT_PREVIEW: "POST", PUBLIC_PAGE: "GET"}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def world(deps: Deps) -> None:
    """O formulário demo-definicao e o de pontuação, importados (versão 1)."""
    for definition in (DEFINITION, SCORED_DEFINITION):
        stored.import_definition(deps, definition, actor="testes")


@pytest.fixture
def writer(api_client: Client, clock: FakeClock, world: None) -> ApiClient:
    return api_client("total", clock=clock, PUBLIC_BASE_URL=ORIGIN)


@pytest.fixture
def reader(api_client: Client, clock: FakeClock, world: None) -> ApiClient:
    return api_client("leitura", clock=clock, PUBLIC_BASE_URL=ORIGIN)


def call(
    client: Any, operation_id: str, slug: str = SLUG, body: Any = None, **options: Any
) -> httpx.Response:
    """Chama a operação pelo nome: o corpo JSON é opcional nas duas que o aceitam."""
    if body is not None:
        options["json"] = body
    return client.request(METHOD[operation_id], url_of(operation_id, slug=slug), **options)


def pdf(client: Any, body: Any = None, slug: str = SLUG) -> httpx.Response:
    return call(client, PDF_PREVIEW, slug, body)


def result(client: Any, body: Any = None, slug: str = SLUG) -> httpx.Response:
    return call(client, RESULT_PREVIEW, slug, body)


def page(client: Any, slug: str = SLUG, **params: Any) -> httpx.Response:
    return call(client, PUBLIC_PAGE, slug, params=params)


def draft_of(deps: Deps, slug: str = SLUG) -> drafts.Draft:
    with deps.sessions() as session:
        found = drafts.load(session, slug)
    assert found is not None
    return found


def save_draft(deps: Deps, slug: str = SLUG, **changes: Any) -> None:
    document = {**draft_of(deps, slug).document, **changes}
    drafts.replace_document(
        deps, slug, document, actor="chave:testes", expected=draft_of(deps, slug).token
    )


def fields_of(response: httpx.Response) -> dict[str, str]:
    """Os `errors` de um `422 validation_failed`, por campo."""
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"
    return {item["field"]: item["message"] for item in response.json()["errors"]}


def without_keys() -> dict[str, Any]:
    return {name: rows for name, rows in snapshot().items() if name not in UNTOUCHED}


def run_a_test(deps: Deps, clock: FakeClock, slug: str = SLUG) -> None:
    """Um teste do rascunho que terminou com sucesso e guardou um plano com outro título."""
    deps.llm = PlanLlm()  # type: ignore[assignment]
    now = datetime.fromtimestamp(clock(), UTC)
    test_id = draft_tests.start(deps, slug, actor="chave:testes", answers=None, day="", now=now)
    draft_tests.run(deps, test_id, now=lambda: now)


def pdf_text(response: httpx.Response) -> str:
    """O texto do PDF da resposta, sem espaços (ele quebra linha em qualquer ponto)."""
    assert response.status_code == 200, response.text
    return squash(text_of(response.content))


def without_additions(html: str) -> str:
    """O HTML da API sem o que ela acrescenta ao da página: o `<base>` e a `<meta>` da política."""
    return META.sub("", html).replace(BASE, "")


def policy_of(response: httpx.Response) -> dict[str, list[str]]:
    """A política de conteúdo da resposta, diretiva a diretiva."""
    found = response.headers["content-security-policy"]
    return {part.split()[0]: part.split()[1:] for part in found.split("; ") if part}


# --- a chave e o papel --------------------------------------------------------------------------


@pytest.mark.parametrize("operation_id", ALL)
def test_every_preview_needs_a_key(
    deps: Deps, clock: FakeClock, world: None, operation_id: str
) -> None:
    response = call(make_api(deps, clock), operation_id)

    assert response.status_code == 401 and response.json()["code"] == "invalid_credentials"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("operation_id", ALL)
def test_a_read_key_previews_everything_because_it_reads_and_writes_nothing(
    reader: ApiClient, operation_id: str
) -> None:
    response = call(reader, operation_id)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("operation_id", ALL)
def test_the_preview_of_a_form_that_does_not_exist_is_a_404(
    reader: ApiClient, operation_id: str
) -> None:
    response = call(reader, operation_id, "nao-existe")

    assert response.status_code == 404 and response.json()["code"] == "not_found"


def test_the_contract_says_these_are_reads_that_answer_with_a_file_or_a_page() -> None:
    document = built_openapi()
    operations = {
        operation["operationId"]: operation
        for methods in document["paths"].values()
        for operation in methods.values()
    }

    for operation_id in ALL:
        operation = operations[operation_id]
        assert operation["x-required-role"] == "leitura"
        assert not {"x-destructive", "x-external-effect", "x-lead-data"} & set(operation)
        assert operation["tags"] == ["Prévias do rascunho"]
    assert operations[PDF_PREVIEW]["responses"]["200"]["content"] == {
        "application/pdf": {"schema": {"type": "string", "format": "binary"}}
    }
    for operation_id in PAGES:
        assert operations[operation_id]["responses"]["200"]["content"] == {
            "text/html": {"schema": {"type": "string"}}
        }
    for operation_id in TEMPLATE_PREVIEWS:  # o corpo é opcional: sem corpo vale o rascunho salvo
        body = operations[operation_id]["requestBody"]
        schema = body["content"]["application/json"]["schema"]
        refs = [part["$ref"] for part in schema.get("anyOf", [schema]) if "$ref" in part]
        assert body.get("required", False) is False
        assert [ref.rsplit("/", 1)[-1] for ref in refs] == ["PreviewDraftRequest"]
    parameters = operations[PUBLIC_PAGE]["parameters"]
    assert [(p["name"], p["in"], p["required"]) for p in parameters if p["name"] == "step"] == [
        ("step", "query", False)
    ]
    assert "requestBody" not in operations[PUBLIC_PAGE]


# --- o custo: 5 fichas --------------------------------------------------------------------------


@pytest.mark.parametrize("operation_id", ALL)
def test_a_preview_costs_five_tokens_so_a_bucket_of_ten_takes_two(
    api_client: Client, world: None, operation_id: str
) -> None:
    """O balde de 10 fichas com o relógio parado: renderizar custa 5 (`heavy`), então a terceira
    chamada já é `429`. Uma leitura comum custaria 1."""
    client = api_client("leitura", API_RATE_LIMIT_PER_MINUTE="10")

    answers = [call(client, operation_id) for _ in range(3)]

    assert [answer.status_code for answer in answers] == [200, 200, 429]
    assert answers[2].json()["code"] == "rate_limited" and "Retry-After" in answers[2].headers


# --- render_form_draft_pdf_preview --------------------------------------------------------------


def test_the_pdf_preview_of_the_saved_draft_opens_as_a_pdf_and_shows_the_example_plan(
    reader: ApiClient, deps: Deps
) -> None:
    deps.pdf = WeasyPrintRenderer()

    response = pdf(reader)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'inline; filename="previa.pdf"'
    assert response.content.startswith(b"%PDF")
    assert len(PdfReader(io.BytesIO(response.content)).pages) >= 1
    assert squash(EXAMPLE_TITLE) in pdf_text(response)


def test_the_pdf_is_the_one_of_the_renderer_of_the_order_with_the_example_data(
    reader: ApiClient, deps: Deps
) -> None:
    """A API não tem um renderizador seu: chama `deps.pdf`, o do pedido, com o contrato do rascunho, o
    plano de exemplo e o contexto de exemplo (a lead de exemplo, nunca uma de verdade)."""
    seen: list[tuple[Any, ...]] = []

    class Spy:
        def render(self, spec: Any, plan: dict[str, Any], context: dict[str, Any]) -> RenderedPdf:
            seen.append((spec.slug, plan, context))
            return RenderedPdf(content=minimal_pdf("do-adaptador"), renderer="spy", page_count=1)

    deps.pdf = Spy()  # type: ignore[assignment]

    response = pdf(reader)

    assert response.content == minimal_pdf("do-adaptador")
    ((slug, plan, context),) = seen
    assert slug == SLUG and plan == PUBLISHED["examples"]["plan"]
    assert context["form"] == {"slug": SLUG, "title": PUBLISHED["title"]}
    assert set(context) == {"lead", "form", "scores", "generated_at"}
    assert context["lead"]["name"] == PUBLISHED["examples"]["answers"]["nome"]


def test_the_pdf_preview_uses_what_the_call_sends_without_saving_it(
    writer: ApiClient, deps: Deps
) -> None:
    deps.pdf = WeasyPrintRenderer()
    saved = draft_of(deps).document
    before = without_keys()

    response = pdf(
        writer,
        {"pdf": {"html": "<h1>Só na prévia</h1><p>{{ plan.titulo }}</p>"}, "preview": "<p>x</p>"},
    )

    shown = pdf_text(response)
    assert "Sónaprévia" in shown and squash(EXAMPLE_TITLE) in shown
    assert "Só na prévia" not in saved["pdf"]["html"]
    assert draft_of(deps).document == saved and without_keys() == before  # nada foi gravado


def test_what_the_call_leaves_out_is_what_the_saved_draft_has(
    writer: ApiClient, deps: Deps
) -> None:
    deps.pdf = WeasyPrintRenderer()
    save_draft(
        deps, pdf={**draft_of(deps).document["pdf"], "html": "<p>Salvo {{ plan.titulo }}</p>"}
    )

    answers = [pdf(writer, {"pdf": {"css": "p { color: #0b3d2e; }"}}), pdf(writer, {}), pdf(writer)]

    for answer in answers:
        assert "Salvo" in pdf_text(answer) and squash(EXAMPLE_TITLE) in pdf_text(answer)


def test_the_pdf_preview_releases_only_the_notes_the_call_names(
    writer: ApiClient, deps: Deps
) -> None:
    deps.pdf = WeasyPrintRenderer()
    listing = "<p>{% for name in scores %}[{{ name }}={{ scores[name] }}]{% endfor %}</p>"

    one = pdf(writer, {"pdf": {"html": listing, "scores": ["alcance"]}}, SCORED)

    shown = pdf_text(one)
    assert "[alcance=25]" in shown and "porte" not in shown  # 2 canais nas respostas de exemplo


def test_the_pdf_preview_can_use_the_plan_of_the_last_test(
    reader: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    run_a_test(deps, clock)
    deps.pdf = WeasyPrintRenderer()

    from_test = pdf(reader, {"source": "test"})
    from_example = pdf(reader, {"source": "example"})

    assert squash(TEST_PLAN_TITLE) in pdf_text(from_test)
    assert squash(TEST_PLAN_TITLE) not in pdf_text(from_example)


def test_the_pdf_preview_shows_an_image_of_the_form_by_its_name(
    writer: ApiClient, deps: Deps
) -> None:
    deps.pdf = WeasyPrintRenderer(AssetStore(deps.storage))
    image = io.BytesIO()
    Image.new("RGB", (60, 40), (220, 20, 40)).save(image, "PNG")
    assets.upload(deps, scope=SLUG, name="logo-black", content=image.getvalue(), actor="testes")

    response = pdf(writer, {"pdf": {"html": '<p>Plano</p><img src="asset:logo-black" alt="">'}})

    pages = PdfReader(io.BytesIO(response.content)).pages
    assert response.status_code == 200 and sum(len(one.images) for one in pages) == 1


def test_an_image_the_form_does_not_have_is_a_422_on_the_pdf_with_its_line(
    writer: ApiClient,
) -> None:
    response = pdf(writer, {"pdf": {"html": '<p>x</p>\n<img src="asset:nao-existe" alt="">'}})

    (item,) = response.json()["errors"]
    assert response.status_code == 422 and (item["field"], item["line"]) == ("pdf.html", 2)
    assert "a imagem «nao-existe» não existe" in item["message"]


@pytest.mark.parametrize("operation_id", TEMPLATE_PREVIEWS)
def test_the_plan_of_the_last_test_that_is_missing_is_a_409_that_says_so(
    reader: ApiClient, operation_id: str
) -> None:
    response = call(reader, operation_id, body={"source": "test"})

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "Ainda não há um teste com sucesso" in response.json()["detail"]


@pytest.mark.parametrize("operation_id", TEMPLATE_PREVIEWS)
def test_the_plan_of_a_test_the_model_no_longer_accepts_is_a_409_without_the_plan(
    reader: ApiClient, deps: Deps, clock: FakeClock, operation_id: str
) -> None:
    run_a_test(deps, clock)
    document = draft_of(deps).document
    document["plan"]["fields"][0]["max_length"] = 5  # o título do teste não cabe mais
    document["examples"]["plan"]["titulo"] = "Curto"
    drafts.replace_document(
        deps, SLUG, document, actor="chave:testes", expected=draft_of(deps).token
    )

    response = call(reader, operation_id, body={"source": "test"})

    assert response.status_code == 409 and response.json()["code"] == "state_conflict"
    assert "não vale mais com o modelo do plano" in response.json()["detail"]
    assert TEST_PLAN_TITLE not in response.text


@pytest.mark.parametrize("operation_id", TEMPLATE_PREVIEWS)
def test_a_template_error_comes_with_the_path_and_the_line_and_never_the_value(
    writer: ApiClient, operation_id: str
) -> None:
    broken = {"pdf": {"html": "<p>ok</p>\n<p>{{ plan.nada }}</p>"}}

    response = call(writer, operation_id, body=broken)

    assert fields_of(response) == {"pdf.html": "plan.nada não é um campo do plano"}
    (item,) = response.json()["errors"]
    assert item["line"] == 2
    assert "ok" not in response.json()["detail"]


def test_a_template_error_in_the_preview_of_the_result_page_names_the_preview(
    writer: ApiClient,
) -> None:
    response = result(writer, {"preview": "<p>ok</p>\n<p>{{ plan.nada }}</p>"})

    assert fields_of(response) == {"preview": "plan.nada não é um campo do plano"}
    assert response.json()["errors"][0]["line"] == 2


def test_the_pdf_that_asks_for_something_outside_the_app_is_refused_with_its_line(
    writer: ApiClient,
) -> None:
    outside = {"pdf": {"html": '<p>x</p>\n<img src="http://example.test/a.png">'}}

    response = pdf(writer, outside)

    (item,) = response.json()["errors"]
    assert response.status_code == 422 and item["field"] == "pdf.html" and item["line"] == 2
    assert "recurso de fora do app" in item["message"] and "example.test" not in response.text


def test_the_css_cannot_load_a_file_outside_the_app_either(writer: ApiClient) -> None:
    response = pdf(writer, {"pdf": {"css": "body { background: url(http://example.test/a.png); }"}})

    assert list(fields_of(response)) == ["pdf.css"]


def test_a_pdf_the_renderer_cannot_make_is_a_422_on_the_pdf_with_the_fixed_message(
    writer: ApiClient, deps: Deps
) -> None:
    deps.pdf = WeasyPrintRenderer()

    response = pdf(
        writer, {"pdf": {"html": "<p>x</p>", "css": "@import url('brands/nao-existe.css');"}}
    )

    assert fields_of(response) == {
        "pdf": "O PDF não foi gerado: template do PDF pediu um recurso fora de app/assets."
    }
    assert "Traceback" not in response.text


def test_a_pdf_that_takes_too_long_is_a_422_and_the_next_preview_still_works(
    writer: ApiClient, deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cem mil parágrafos de um template de 50 bytes levavam 43 s e seguravam o lock do PDF."""
    deps.pdf = WeasyPrintRenderer()
    monkeypatch.setattr(weasyprint_pdf, "PDF_SECONDS", 1.0)
    many = "{% for i in range(100000) %}<p>x</p>{% endfor %}"

    slow = pdf(writer, {"pdf": {"html": many}})

    assert fields_of(slow) == {"pdf": "O PDF não foi gerado: o PDF passou do prazo de 1 s."}
    monkeypatch.setattr(weasyprint_pdf, "PDF_SECONDS", 30.0)
    assert squash(EXAMPLE_TITLE) in pdf_text(pdf(writer))


def test_a_template_that_never_ends_is_a_422_on_its_field_with_the_line(
    writer: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "RENDER_SECONDS", 1.0)
    never = "<p>ok</p>\n{% for i in range(100000) %}{% for j in range(100000) %}{% endfor %}{% endfor %}"

    response = pdf(writer, {"pdf": {"html": never}})

    (item,) = response.json()["errors"]
    assert response.status_code == 422 and (item["field"], item["line"]) == ("pdf.html", 2)
    assert "passou do prazo de 1 s" in item["message"]


@pytest.mark.parametrize("operation_id", TEMPLATE_PREVIEWS)
def test_the_previews_use_the_sandbox_of_the_templates_and_its_limits(
    writer: ApiClient, operation_id: str
) -> None:
    """O mesmo ambiente isolado do salvar e da publicação: atributo interno e saída grande demais
    são recusados, com o campo, e nunca viram um PDF nem uma página."""
    internal = call(writer, operation_id, body={"pdf": {"html": "<p>{{ plan.__class__ }}</p>"}})
    huge = call(
        writer,
        operation_id,
        body={"pdf": {"html": '{% for i in range(500) %}{{ "x" * 5000 }}{% endfor %}'}},
    )

    assert list(fields_of(internal)) == ["pdf.html"]
    assert list(fields_of(huge)) == ["pdf.html"]


# --- render_form_draft_result_preview -----------------------------------------------------------


def test_the_result_preview_is_the_page_the_lead_sees_with_the_preview_of_the_call(
    writer: ApiClient, deps: Deps
) -> None:
    before = without_keys()
    markup = '<section class="section"><h2>Sua prévia</h2><p>{{ plan.titulo }}</p></section>'

    response = result(writer, {"preview": markup})

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-disposition"].startswith("inline")
    shown = text(response.text)
    assert "Seu plano está pronto" in shown and "Baixar PDF" in shown  # a moldura de verdade
    assert "Sua prévia" in shown and EXAMPLE_TITLE in shown
    assert "<script" not in response.text
    assert draft_of(deps).document["preview"] == PUBLISHED["preview"]
    assert without_keys() == before  # a prévia não grava


def test_the_result_preview_shows_only_the_released_notes(writer: ApiClient) -> None:
    body = {
        "pdf": {"html": "<p>{{ plan.titulo }}</p>", "scores": ["alcance"]},
        "preview": "<p>{% for n in scores %}[{{ n }}]{% endfor %}</p>",
    }

    shown = text(result(writer, body, SCORED).text)

    assert "[alcance]" in shown and "[porte]" not in shown


def test_the_result_preview_can_use_the_plan_of_the_last_test(
    writer: ApiClient, deps: Deps, clock: FakeClock
) -> None:
    run_a_test(deps, clock)

    shown = text(result(writer, {"source": "test", "preview": "<p>{{ plan.titulo }}</p>"}).text)

    assert TEST_PLAN_TITLE in shown


def test_the_result_preview_swaps_an_image_for_the_route_that_serves_it(
    writer: ApiClient, deps: Deps
) -> None:
    image = io.BytesIO()
    Image.new("RGB", (60, 40), (20, 40, 220)).save(image, "PNG")
    logo = assets.upload(
        deps, scope=SLUG, name="logo-black", content=image.getvalue(), actor="testes"
    ).asset

    html = result(writer, {"preview": '<img src="asset:logo-black" alt="">'}).text

    assert f'src="{logo.ref.url}"' in html and "asset:" not in html


# --- render_form_draft_public_page --------------------------------------------------------------


def test_the_public_page_is_the_page_of_the_form_with_every_step_and_no_form_to_send(
    reader: ApiClient, deps: Deps
) -> None:
    response = page(reader)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-disposition"].startswith("inline")
    html = response.text
    assert "data-preview" in html and 'data-start-step="0"' in html
    assert "<form" not in html  # nada é enviado
    for step in draft_of(deps).document["steps"]:
        assert step["title"] in html
    assert "Conte como você organiza seu projeto fictício" in text(html)


def test_the_public_page_opens_at_the_step_asked_or_the_nearest_one(
    reader: ApiClient, deps: Deps
) -> None:
    last = len(draft_of(deps).document["steps"])

    starts = {asked: page(reader, step=asked).text for asked in (1, 2, last, last + 40)}

    assert 'data-start-step="0"' in starts[1]
    assert 'data-start-step="1"' in starts[2]
    assert f'data-start-step="{last - 1}"' in starts[last]
    assert f'data-start-step="{last - 1}"' in starts[last + 40]  # passou do fim: a última


@pytest.mark.parametrize("asked", ["0", "-1", "abc", "1.5", "10000"])
def test_a_step_that_is_not_a_number_from_one_to_9999_is_a_422_on_the_step(
    reader: ApiClient, asked: str
) -> None:
    response = page(reader, step=asked)

    assert list(fields_of(response)) == ["step"]
    assert asked.isdigit() or asked not in response.text  # o que não é número não volta


def test_the_public_page_reads_the_saved_draft_and_not_the_published_version(
    reader: ApiClient, deps: Deps
) -> None:
    save_draft(deps, intro="Uma introdução só do rascunho.")

    assert "Uma introdução só do rascunho." in text(page(reader).text)
    assert "Uma introdução só do rascunho." not in PUBLISHED["intro"]


def test_the_public_page_of_a_draft_that_no_longer_compiles_is_a_422_with_the_problems(
    reader: ApiClient, deps: Deps
) -> None:
    save_draft(deps, intro="Mudança para existir um rascunho.")
    with deps.sessions.begin() as session:
        session.execute(
            sa.text(
                "update form_drafts set definition = "
                "jsonb_set(definition, '{prompt}', '\"{{ answers.nao_existe }}\"')"
            )
        )

    response = page(reader)

    assert "prompt" in fields_of(response)


# --- o <base> e a política de conteúdo do HTML --------------------------------------------------


@pytest.mark.parametrize("operation_id", PAGES)
def test_the_page_says_where_it_lives_so_its_files_load_away_from_the_api(
    reader: ApiClient, operation_id: str
) -> None:
    html = call(reader, operation_id).text

    assert html.count("<base ") == 1
    assert f'<base href="{ORIGIN}/">' in html
    assert html.index("<base ") < html.index("<link ")  # antes de qualquer arquivo
    assert "/static/css/public.css" in html  # os arquivos do app, pelo caminho de sempre


@pytest.mark.parametrize("operation_id", PAGES)
def test_without_a_usable_public_base_url_the_page_goes_without_a_base(
    api_client: Client, world: None, operation_id: str
) -> None:
    client = api_client("leitura", PUBLIC_BASE_URL="não é uma url")

    assert "<base" not in call(client, operation_id).text


def test_the_public_page_runs_scripts_of_the_app_only_and_in_a_sandbox_without_an_origin(
    reader: ApiClient,
) -> None:
    response = page(reader)

    policy = policy_of(response)
    assert policy["sandbox"] == ["allow-scripts"]  # roda scripts, mas sem a origem do admin
    assert "allow-same-origin" not in str(policy) and "allow-top-navigation" not in str(policy)
    assert policy["script-src"] == ["'self'"] and policy["default-src"] == ["'none'"]
    assert policy["connect-src"] == ["'none'"] and policy["form-action"] == ["'none'"]
    assert "'unsafe-inline'" not in str(policy) and "'unsafe-eval'" not in str(policy)
    assert policy["frame-ancestors"] == ["'none'"] and response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_the_result_page_runs_no_script_at_all_not_even_the_one_the_operator_wrote(
    writer: ApiClient,
) -> None:
    hostile = '<p>oi</p><script>fetch("/admin/api/v1/me")</script><img src=x onerror="alert(1)">'

    response = result(writer, {"preview": hostile})

    assert response.status_code == 200 and "<script>" in response.text  # o HTML é o do operador
    policy = policy_of(response)
    assert policy["sandbox"] == []  # sem `allow-scripts`: nenhum script roda
    assert "allow-same-origin" not in str(policy) and policy["default-src"] == ["'none'"]
    assert "script-src" not in policy  # cai no `default-src 'none'`
    assert "'unsafe-inline'" not in str(policy) and "'unsafe-eval'" not in str(policy)
    assert response.headers["x-frame-options"] == "DENY"


def meta_policy_of(html: str) -> dict[str, list[str]]:
    """A política da `<meta http-equiv>` da página, diretiva a diretiva."""
    found = re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">', html)
    assert found is not None, "a página não tem a <meta> da política"
    text = found.group(1).replace("&#39;", "'")
    return {part.split()[0]: part.split()[1:] for part in text.split("; ") if part}


@pytest.mark.parametrize("operation_id", PAGES)
def test_the_page_carries_the_policy_in_a_meta_for_when_the_header_is_lost(
    reader: ApiClient, operation_id: str
) -> None:
    """Salva em arquivo ou aberta fora da resposta da API, a página perde o cabeçalho: a `<meta>` leva a
    mesma política, menos o que a meta não aceita (`sandbox` e `frame-ancestors`), com a origem do app ao
    lado do `'self'` (que ali seria o arquivo), antes do `<base>` e de qualquer arquivo."""
    response = call(reader, operation_id)
    html = response.text

    meta = meta_policy_of(html)
    header = policy_of(response)
    assert "sandbox" not in meta and "frame-ancestors" not in meta
    assert meta["default-src"] == ["'none'"] and meta["form-action"] == ["'none'"]
    assert meta["base-uri"] == [ORIGIN]  # o <base> da página é o único que a meta deixa
    assert meta["style-src"] == ["'self'", ORIGIN] and meta["img-src"] == [
        "'self'",
        ORIGIN,
        "data:",
    ]
    assert "'unsafe-inline'" not in str(meta) and "'unsafe-eval'" not in str(meta)
    assert html.index("Content-Security-Policy") < html.index("<base ") < html.index("<link ")
    assert set(meta) == set(header) - {"sandbox", "frame-ancestors"}  # as mesmas diretivas


def test_the_meta_of_the_public_page_lets_the_scripts_of_the_app_run_and_the_result_page_none(
    reader: ApiClient,
) -> None:
    public = meta_policy_of(page(reader).text)
    result_page = meta_policy_of(result(reader).text)

    assert public["script-src"] == ["'self'", ORIGIN] and public["connect-src"] == ["'none'"]
    assert "script-src" not in result_page  # cai no `default-src 'none'`: nenhum script roda


@pytest.mark.parametrize("operation_id", PAGES)
def test_without_a_usable_public_base_url_the_meta_trusts_no_base_and_the_page_has_none(
    api_client: Client, world: None, operation_id: str
) -> None:
    client = api_client("leitura", PUBLIC_BASE_URL="não é uma url")

    html = call(client, operation_id).text

    assert "<base" not in html and meta_policy_of(html)["base-uri"] == ["'none'"]
    assert meta_policy_of(html)["style-src"] == ["'self'"]


def test_the_pdf_is_not_framed_by_anyone(reader: ApiClient) -> None:
    response = pdf(reader)

    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["content-disposition"] == 'inline; filename="previa.pdf"'


# --- mandar um template é do papel total --------------------------------------------------------

TEMPLATE_BODIES = {
    "pdf-html": {"pdf": {"html": "<p>x</p>"}},
    "pdf-css": {"pdf": {"css": "p { color: red; }"}},
    "pdf-scores": {"pdf": {"scores": []}},
    "preview": {"preview": "<p>x</p>"},
    "everything": {"pdf": {"html": "<p>x</p>", "css": "", "scores": []}, "preview": "<p>x</p>"},
}
SAVED_ONLY = {
    "no-body": None,
    "empty": {},
    "empty-pdf": {"pdf": {}},
    "example": {"source": "example"},
}


@pytest.mark.parametrize("operation_id", TEMPLATE_PREVIEWS)
@pytest.mark.parametrize("body", list(TEMPLATE_BODIES.values()), ids=list(TEMPLATE_BODIES))
def test_a_read_key_cannot_make_the_process_compile_a_template_it_wrote(
    reader: ApiClient,
    deps: Deps,
    monkeypatch: pytest.MonkeyPatch,
    operation_id: str,
    body: dict[str, Any],
) -> None:
    """O `403` vem antes de qualquer conta: nem o contrato do rascunho é montado, nem o PDF rendido."""

    def never(*_: Any, **__: Any) -> Any:
        raise AssertionError("a chave `leitura` fez o processo compilar o que ela mandou")

    monkeypatch.setattr(preview_actions, "template_preview", never)
    deps.pdf = never  # type: ignore[assignment]

    response = call(reader, operation_id, body=body)

    assert response.status_code == 403 and response.json()["code"] == "forbidden_role"
    assert "«total»" in response.json()["detail"] and "«leitura»" in response.json()["detail"]


def test_a_read_key_that_sends_a_huge_template_is_refused_at_once(reader: ApiClient) -> None:
    started = time.perf_counter()

    answers = [
        pdf(reader, {"pdf": {"html": "{#" * 125_000}}),
        result(reader, {"preview": "{#" * 125_000}),
    ]

    assert [answer.status_code for answer in answers] == [403, 403]
    assert time.perf_counter() - started < 5  # de imediato; a folga é para a suíte em paralelo


@pytest.mark.parametrize("operation_id", TEMPLATE_PREVIEWS)
@pytest.mark.parametrize("body", list(SAVED_ONLY.values()), ids=list(SAVED_ONLY))
def test_a_read_key_previews_the_saved_draft_when_the_body_sends_no_template(
    reader: ApiClient, operation_id: str, body: dict[str, Any] | None
) -> None:
    response = call(reader, operation_id, body=body)

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("operation_id", TEMPLATE_PREVIEWS)
def test_a_read_key_can_ask_for_the_plan_of_the_last_test(
    reader: ApiClient, deps: Deps, clock: FakeClock, operation_id: str
) -> None:
    run_a_test(deps, clock)

    response = call(reader, operation_id, body={"source": "test"})

    assert response.status_code == 200, response.text  # o plano do teste não é um template


@pytest.mark.parametrize("operation_id", TEMPLATE_PREVIEWS)
@pytest.mark.parametrize("body", list(TEMPLATE_BODIES.values()), ids=list(TEMPLATE_BODIES))
def test_a_total_key_sends_the_template_the_read_key_cannot(
    writer: ApiClient, operation_id: str, body: dict[str, Any]
) -> None:
    assert call(writer, operation_id, body=body).status_code == 200


def test_the_contract_declares_the_403_of_the_templates_only_where_the_body_is() -> None:
    document = built_openapi()
    operations = {
        operation["operationId"]: operation
        for methods in document["paths"].values()
        for operation in methods.values()
    }

    for operation_id in TEMPLATE_PREVIEWS:
        operation = operations[operation_id]
        assert (
            operation["x-required-role"] == "leitura"
        )  # o papel da operação segue sendo `leitura`
        assert "total" in operation["responses"]["403"]["description"]
        assert "total" in operation["description"] and "`pdf`" in operation["description"]
    assert "403" not in operations[PUBLIC_PAGE]["responses"]  # sem corpo, sem template


# --- o que se manda para ser lido não custa mais que o tamanho dele -----------------------------

HOSTILE = {
    "open-comments": ({"pdf": {"html": "{#" * 125_000}}, "pdf.html"),
    "open-comments-preview": ({"preview": "{#" * 125_000}, "preview"),
    "url-and-spaces": ({"pdf": {"css": "url(" + " " * 20_000}}, None),
    "open-quoted-urls": ({"pdf": {"css": "url('" * 45_000}}, None),
    "open-imports": ({"pdf": {"css": "@import " * 30_000}}, None),
    "deep-parens": ({"pdf": {"html": "{{ " + "(" * 4000 + "1" + ")" * 4000 + " }}"}}, "pdf.html"),
    "deep-ifs": ({"pdf": {"html": "{% if x %}" * 3000 + "{% endif %}" * 3000}}, "pdf.html"),
    "deep-fors": ({"preview": "{% for i in x %}" * 25 + "{% endfor %}" * 25}, "preview"),
}


@pytest.mark.parametrize("operation_id", TEMPLATE_PREVIEWS)
@pytest.mark.parametrize("name", list(HOSTILE))
def test_a_template_made_to_weigh_is_refused_in_time_and_never_as_a_500(
    writer: ApiClient, operation_id: str, name: str
) -> None:
    body, field = HOSTILE[name]
    started = time.perf_counter()

    response = call(writer, operation_id, body=body)

    assert time.perf_counter() - started < 3
    assert response.status_code in (200, 422), response.text[:200]  # um CSS que só não fecha vale
    if field is not None:
        assert field in fields_of(response)


def test_a_template_too_deep_says_so_in_its_field(writer: ApiClient) -> None:
    response = pdf(writer, {"pdf": {"html": "{{ " + "(" * 4000 + "1" + ")" * 4000 + " }}"}})

    (item,) = response.json()["errors"]
    assert item["field"] == "pdf.html" and item["line"] is None
    assert "fundo demais" in item["message"] and "(((" not in response.text


# --- as prévias só leem -------------------------------------------------------------------------


@pytest.mark.parametrize("operation_id", ALL)
def test_a_preview_writes_nothing_audits_nothing_and_never_calls_the_llm(
    writer: ApiClient, deps: Deps, operation_id: str
) -> None:
    class NoLlm:
        calls = 0

        def generate(self, spec: Any, answers: Any) -> Any:
            NoLlm.calls += 1
            raise AssertionError("o LLM não pode ser chamado por uma prévia")

    deps.llm = NoLlm()  # type: ignore[assignment]
    before = without_keys()

    answers = [call(writer, operation_id)]
    if operation_id in TEMPLATE_PREVIEWS:
        answers.append(call(writer, operation_id, body={"preview": "<p>x</p>"}))

    assert [answer.status_code for answer in answers] == [200] * len(answers)
    assert without_keys() == before and NoLlm.calls == 0
    for action in ("form.draft.save", "form.draft.test", "form.publish"):
        assert audited(deps, action) == []


def test_the_previews_show_the_example_data_and_never_the_data_of_a_lead(
    reader: ApiClient, deps: Deps
) -> None:
    """Há pedidos de leads de verdade no banco, do mesmo formulário (o `demo`): nenhuma prévia os
    mostra, porque usam o plano e as respostas de exemplo."""
    build_jobs_world(deps)
    deps.pdf = WeasyPrintRenderer()

    in_pdf = pdf(reader, slug="demo")
    html = [result(reader, slug="demo").text, page(reader, "demo").text]

    shown = pdf_text(in_pdf)
    for name, email in PEOPLE.values():
        assert squash(name) not in shown and squash(email) not in shown
        assert all(name not in one and email not in one for one in html)


# --- a entrada ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"source": "outro"}, "source"),
        ({"source": 3}, "source"),
        ({"pdf": "html"}, "pdf"),
        ({"pdf": {"html": 1}}, "pdf.html"),
        ({"pdf": {"unknown": "x"}}, "pdf.unknown"),
        ({"pdf": {"scores": "porte"}}, "pdf.scores"),
        ({"pdf": {"scores": ["Nome Inválido"]}}, "pdf.scores.0"),
        ({"preview": 3}, "preview"),
        ({"other": 1}, "other"),
    ],
)
@pytest.mark.parametrize("operation_id", TEMPLATE_PREVIEWS)
def test_a_body_that_does_not_fit_is_a_422_on_its_field(
    writer: ApiClient, operation_id: str, body: dict[str, Any], field: str
) -> None:
    response = call(writer, operation_id, body=body)

    assert list(fields_of(response)) == [field]


@pytest.mark.parametrize("operation_id", TEMPLATE_PREVIEWS)
def test_a_note_that_does_not_exist_is_refused_on_the_notes(
    writer: ApiClient, operation_id: str
) -> None:
    response = call(writer, operation_id, SCORED, {"pdf": {"scores": ["porte", "inventada"]}})

    assert fields_of(response) == {"pdf.scores.1": "não é uma nota da pontuação"}


# --- a paridade com a página --------------------------------------------------------------------


def as_the_page_sends(deps: Deps, body: dict[str, Any]) -> dict[str, Any]:
    """O corpo da API como os campos que o editor do PDF manda: a página manda tudo, e o que a
    chamada deixa de fora é o que o rascunho já tem."""
    saved = draft_of(deps).document
    sent = {**saved["pdf"], **body.get("pdf", {})}
    return {
        "pdf_html": sent["html"],
        "pdf_css": sent.get("css", ""),
        "pdf_scores": sent.get("scores", []),
        "preview": body.get("preview", saved["preview"]),
        "plan": body.get("source", "example"),
    }


def test_the_pdf_of_the_api_is_the_pdf_the_page_shows_for_the_same_data(
    writer: ApiClient, deps: Deps
) -> None:
    deps.pdf = WeasyPrintRenderer()
    assert login(writer).status_code == 303
    body = {"pdf": {"html": "<h1>Mesma prévia</h1><p>{{ plan.titulo }}</p>", "css": "p {margin:0}"}}

    by_page = post(writer, f"{DRAFT_PAGE}/pdf/preview", **as_the_page_sends(deps, body))
    by_api = pdf(writer, body)

    assert by_page.status_code == by_api.status_code == 200
    assert by_page.headers["content-type"] == by_api.headers["content-type"] == "application/pdf"
    assert by_page.headers["content-disposition"] == by_api.headers["content-disposition"]
    assert pdf_text(by_page) == pdf_text(by_api) and "Mesmaprévia" in pdf_text(by_api)


def test_the_result_page_of_the_api_is_the_page_the_admin_shows_for_the_same_data(
    writer: ApiClient, deps: Deps
) -> None:
    assert login(writer).status_code == 303
    body = {"preview": '<section class="section"><h2>Igual</h2><p>{{ plan.titulo }}</p></section>'}

    by_page = post(writer, f"{DRAFT_PAGE}/pdf/result", **as_the_page_sends(deps, body))
    by_api = result(writer, body)

    assert by_page.status_code == by_api.status_code == 200
    assert without_additions(by_api.text) == by_page.text  # só o <base> e a <meta> a mais
    assert "Igual" in by_api.text


def test_the_public_page_of_the_api_is_the_page_the_admin_shows_for_the_same_data(
    writer: ApiClient,
) -> None:
    assert login(writer).status_code == 303

    for step in (1, 2):
        by_page = writer.get(f"{DRAFT_PAGE}/preview/frame", params={"etapa": step})
        by_api = page(writer, step=step)

        assert by_page.status_code == by_api.status_code == 200
        assert without_additions(by_api.text) == by_page.text


def test_the_errors_of_the_api_are_the_problems_the_page_shows_in_the_frame(
    writer: ApiClient, deps: Deps
) -> None:
    assert login(writer).status_code == 303
    body = {"pdf": {"html": "<p>ok</p>\n<p>{{ plan.nada }}</p>"}}

    by_page = post(writer, f"{DRAFT_PAGE}/pdf/preview", **as_the_page_sends(deps, body))
    by_api = pdf(writer, body)

    assert by_page.status_code == by_api.status_code == 422
    assert "PDF (HTML), linha 2: plan.nada não é um campo do plano" in text(by_page.text)
    assert fields_of(by_api) == {"pdf.html": "plan.nada não é um campo do plano"}


# --- o eco: o que se manda não volta num erro ---------------------------------------------------


def test_no_error_of_the_preview_operations_gives_back_the_free_text_that_was_sent(
    writer: ApiClient,
) -> None:
    """O eco geral do contrato para no `422` do Pydantic, antes de o serviço rodar. Aqui o texto livre
    de cada operação vai com o sentinela e o resto válido, e a recusa vem do serviço. (Uma mensagem
    de template cita o nome que o template pede, como `plan.nada` ou a imagem `asset:<nome>`, o mesmo
    da página e do `save_form_draft`: o que não pode voltar é o texto que se mandou.)"""
    broken = f"<p>{SENTINEL}</p>\n{{% if %}}"
    refused = [
        pdf(writer, {"pdf": {"html": broken}}),
        pdf(writer, {"pdf": {"css": f"{SENTINEL} {{"}, "preview": broken}),
        pdf(writer, {"pdf": {"html": f'<img src="http://{SENTINEL}.test/a.png">'}}),
        pdf(writer, {"source": SENTINEL}),
        pdf(writer, {"pdf": {"scores": [SENTINEL]}}),
        pdf(writer, {"source": "test", "preview": f"<p>{SENTINEL}</p>"}),
        result(writer, {"preview": broken}),
        result(writer, {"pdf": {"html": broken}}),
        result(writer, {"source": SENTINEL}),
        result(writer, {"pdf": {"scores": [SENTINEL]}}),
        result(writer, {"source": "test", "preview": f"<p>{SENTINEL}</p>"}),
        page(writer, step=SENTINEL),
        page(writer, SENTINEL),
        pdf(writer, {"pdf": {"html": broken}}, SENTINEL),
        result(writer, {"preview": broken}, SENTINEL),
    ]

    assert all(response.status_code >= 400 for response in refused)
    leaked = [r.request.url for r in refused if SENTINEL.lower() in r.text.lower()]
    assert not leaked, "um erro devolveu o texto"
    assert not [r for r in refused if SENTINEL in "".join(r.headers.values())]
