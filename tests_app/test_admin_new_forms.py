"""Criar um formulário do zero e duplicar um existente no admin, pela costura HTTP (TestClient), com
o LLM falso: o formulário novo nasce sem versão publicada, com o rascunho a partir do modelo inicial
ou da cópia de outro, e só vai ao ar na primeira publicação."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from admin_api_support import admin_routes, built_openapi
from conftest import Receiver, legacy_definition
from fastapi.testclient import TestClient
from test_admin import ORIGIN, FakeClock, csrf_token, finish, login
from test_admin_destinations import ADMIN_ENV
from test_admin_destinations import create as create_destination
from test_admin_drafts import EDITOR_FORM, TestTrigger, editor, hidden, post
from test_admin_form_pages import rows_of
from test_admin_forms import DEFINITION, import_form, problems_of, section_of
from test_admin_forms import SLUG as IMPORTED
from test_public import form_fields, job_id_of, tags, text
from test_weasyprint_pdf import fonts_of, pages_of, squash, text_of

from app import destinations, pipeline
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.admin.actions.destinations import DEFAULT_SCOPE
from app.api.main import create_app
from app.deps import Deps
from app.forms import drafts
from app.forms.stored import RESERVED_SLUGS
from app.models import AuditLog
from app.settings import Settings

NEW = "/admin/forms/new"
TITLE = "Diagnóstico de gestão do escritório"
SLUG = "gestao-do-escritorio"
DRAFT = f"/admin/forms/{SLUG}/draft"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def worker(deps: Deps, clock: FakeClock) -> TestTrigger:
    return TestTrigger(deps, clock)


@pytest.fixture
def client(deps: Deps, clock: FakeClock, worker: TestTrigger) -> TestClient:
    """A operadora no admin, sem formulário do banco nenhum."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, test_trigger=worker, clock=clock),
        follow_redirects=False,
    )
    assert login(current).status_code == 303
    return current


def create(
    client: TestClient, title: str = TITLE, slug: str = SLUG, source: str = ""
) -> httpx.Response:
    """Cria o formulário pela página do admin, como o navegador."""
    return post(client, NEW, title=title, slug=slug, source=source)


def audited(deps: Deps, action: str) -> list[AuditLog]:
    with deps.sessions() as session:
        return list(session.scalars(sa.select(AuditLog).where(AuditLog.action == action)))


def test_a_form_created_from_scratch_starts_from_the_starter_and_is_not_public_yet(
    client: TestClient, deps: Deps
) -> None:
    assert client.get(NEW).status_code == 200

    response = create(client)

    assert response.status_code == 303 and response.headers["location"] == DRAFT
    shown = text(client.get(DRAFT).text)
    assert f"Rascunho de {TITLE}" in shown and "ainda não publicado" in shown
    for question in (
        "Quantas pessoas participam do projeto com você?",
        "Qual parte da organização mais precisa de atenção?",
    ):
        assert question in shown, question
    row = text(rows_of(client.get("/admin/forms"))[SLUG])
    assert TITLE in row and "Nenhuma versão publicada" in row and "Rascunho pendente" in row
    assert client.get(f"/p/{SLUG}").status_code == 404  # o rascunho nunca vai ao ar sozinho
    (entry,) = audited(deps, "form.create")
    assert entry.actor == "operadora" and entry.details["form_slug"] == SLUG
    assert TITLE not in json.dumps(entry.details)  # o slug e a impressão digital, sem conteúdo


def test_the_page_of_a_form_not_published_yet_reads_its_draft(client: TestClient) -> None:
    create(client)

    page = client.get(f"/admin/forms/{SLUG}")

    assert page.status_code == 200
    version = text(section_of(page, "versao"))
    assert "Nenhuma versão publicada" in version and "Exportar" not in version
    draft = text(section_of(page, "rascunho"))
    assert "ainda não foi publicado" in draft and "versão None" not in draft
    assert "Publicar" in draft
    assert "a leitura abaixo é a do rascunho" in text(section_of(page, "nao-publicado"))
    assert "Qual parte da organização mais precisa de atenção?" in text(section_of(page, "pagina"))
    assert 'id="historico"' not in page.text
    compare = client.get(f"/admin/forms/{SLUG}/versions/compare")  # não há versão para comparar
    assert compare.status_code == 404 and "ainda não tem versão publicada" in text(compare.text)


def test_a_draft_that_no_longer_compiles_keeps_the_form_page_and_explains_the_preview(
    client: TestClient, deps: Deps
) -> None:
    """Um rascunho guardado compila quando é salvo; um deploy com o compilador mais rígido pode
    deixá-lo sem compilar, como uma versão publicada."""
    create(client)
    with deps.sessions.begin() as session:
        session.execute(
            sa.text(
                "update form_drafts set definition = "
                "jsonb_set(definition, '{prompt}', '\"{{ answers.nao_existe }}\"')"
            )
        )

    page = client.get(f"/admin/forms/{SLUG}")
    preview = client.get(f"{DRAFT}/preview")

    assert page.status_code == 200
    assert "O rascunho não compila com o código deste deploy" in text(page.text)
    assert preview.status_code == 404
    assert "O rascunho não compila com o código deste deploy" in text(preview.text)
    assert client.get(f"{DRAFT}/preview/frame").status_code == 404
    assert client.get(DRAFT).status_code == 200  # o editor abre, para corrigir


# --- o endereço ---------------------------------------------------------------------------------


def field_error(response: httpx.Response, name: str) -> str:
    """O problema que a página mostra embaixo de um campo."""
    found = tags(response.text, "p", id=f"f-{name}-erro")
    assert found, f"a página não mostrou problema em {name}"
    match = re.search(rf'<p class="error" id="f-{name}-erro">(.*?)</p>', response.text, re.DOTALL)
    assert match is not None
    return text(match.group(1))


@pytest.mark.parametrize(
    ("slug", "problem"),
    [
        ("Gestão do Escritório", "use letras minúsculas sem acento, números e hífens"),
        ("gestao--escritorio", "use letras minúsculas sem acento, números e hífens"),
        ("-gestao", "use letras minúsculas sem acento, números e hífens"),
        ("g" * 49, "use no máximo 48 caracteres"),
        ("new", "este endereço é reservado"),
        ("import", "este endereço é reservado"),
        ("default", "este endereço é reservado"),
        ("demo", "já existe um formulário com este endereço"),
        ("reforma-2027", "já existe um formulário com este endereço"),
    ],
)
def test_the_address_is_checked_when_the_form_is_created(
    client: TestClient, deps: Deps, slug: str, problem: str
) -> None:
    response = create(client, slug=slug)

    assert response.status_code == 422
    assert problem in field_error(response, "slug")
    assert tags(response.text, "input", name="slug", value=slug)  # o que ela escreveu volta
    assert tags(response.text, "input", name="title", value=TITLE)
    assert audited(deps, "form.create") == []


def test_an_address_taken_by_another_database_form_is_refused(client: TestClient) -> None:
    create(client)

    response = create(client, title="Outro diagnóstico")

    assert response.status_code == 422
    assert "já existe um formulário com este endereço" in field_error(response, "slug")


def test_a_blank_address_comes_from_the_title(client: TestClient) -> None:
    response = create(client, title="Diagnóstico: Gestão & Equipe do Escritório", slug=" ")

    assert response.status_code == 303
    slug = "diagnostico-gestao-equipe-do-escritorio"
    assert response.headers["location"] == f"/admin/forms/{slug}/draft"


def test_the_form_needs_a_title(client: TestClient) -> None:
    response = create(client, title="   ")

    assert response.status_code == 422
    assert "dê um título ao formulário" in field_error(response, "title")


def test_creating_or_duplicating_needs_the_session_and_the_csrf_token(
    client: TestClient, deps: Deps
) -> None:
    anonymous = TestClient(client.app, follow_redirects=False)
    form = {"title": TITLE, "slug": SLUG, "source": "demo"}

    refused = [
        anonymous.post(NEW, data={"csrf_token": "x", **form}, headers=ORIGIN),
        client.post(NEW, data={"csrf_token": "errado", **form}, headers=ORIGIN),
        client.post(NEW, data={"csrf_token": csrf_token(client), **form}),  # sem a origem
    ]

    assert [response.status_code for response in refused] == [303, 403, 403]
    assert refused[0].headers["location"].startswith("/admin/login")
    assert anonymous.get(NEW).status_code == 303  # a página também pede a sessão
    assert client.get(f"/admin/forms/{SLUG}").status_code == 404
    assert audited(deps, "form.duplicate") == [] and audited(deps, "form.create") == []


def test_every_word_of_the_admin_and_of_the_api_where_a_slug_goes_is_a_reserved_address(
    client: TestClient,
) -> None:
    """Um formulário com o endereço de uma página do admin (`/admin/forms/<palavra>`) ficaria sem a
    página dele, e um com o de uma palavra da API (`/forms/<palavra>`, ou o escopo `default` dos
    destinos, que ocupa o lugar do endereço) se confundiria com ela: toda palavra nova nesse nível
    precisa entrar na lista dos reservados. As páginas vêm das rotas do app e as palavras da API do
    esquema da sub-aplicação, e nenhuma depende do `/openapi.json` do app, que produção desliga."""
    pages = {
        path.split("/")[3]
        for _, path in admin_routes(client.app)
        if path.startswith("/admin/forms/")
    }
    api = {path.split("/")[2] for path in built_openapi()["paths"] if path.startswith("/forms/")}
    # O escopo do destino padrão ocupa o lugar do endereço de um formulário: `/admin/destinations/default`
    # na página, e `{scope}` na API (ADR 0015, "Destinos"). A palavra vem da rota da página, e não
    # de uma lista escrita aqui, e tem de ser a que a API aceita (`DEFAULT_SCOPE`); as palavras fixas
    # sob `/destinations/` na API (hoje nenhuma: só `{scope}`) também reservam o endereço.
    scopes = {
        path.split("/")[3]
        for _, path in admin_routes(client.app)
        if path.startswith("/admin/destinations/") and path.split("/")[3] != "forms"
    }
    api |= {
        path.split("/")[2] for path in built_openapi()["paths"] if path.startswith("/destinations/")
    }
    assert scopes == {DEFAULT_SCOPE}
    words = sorted(word for word in pages | api | scopes if not word.startswith("{"))
    assert {"import", "new", "default"} <= set(words)
    assert set(words) <= RESERVED_SLUGS

    for word in words:
        response = create(client, slug=word)
        assert "este endereço é reservado" in field_error(response, "slug"), word


NEW_SLUG = "gestao-2027"


def test_the_address_changes_until_the_first_publication_and_then_stays(
    client: TestClient, deps: Deps, worker: TestTrigger, clock: FakeClock, receiver: Receiver
) -> None:
    create(client)
    # Um destino próprio, criado antes da mudança, acompanha o formulário.
    create_destination(client, f"/admin/destinations/forms/{SLUG}", receiver.url("/gestao"))
    fields = editor(client, DRAFT)
    assert fields["slug"] == SLUG
    fields["slug"] = NEW_SLUG

    response = post(client, DRAFT, **fields)

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/forms/{NEW_SLUG}/draft"
    assert (
        client.get(DRAFT).status_code == 404
        and client.get(f"/admin/forms/{SLUG}").status_code == 404
    )
    assert NEW_SLUG in rows_of(client.get("/admin/forms")) and SLUG not in rows_of(
        client.get("/admin/forms")
    )
    destination = client.get(f"/admin/destinations/forms/{NEW_SLUG}").text
    assert tags(destination, "input", name="url", value=receiver.url("/gestao"))
    (entry,) = audited(deps, "form.rename")
    assert entry.details == {"form_slug": NEW_SLUG, "previous_slug": SLUG}
    run_test(client, worker, clock, slug=NEW_SLUG)
    assert publish(client, slug=NEW_SLUG).status_code == 303
    assert client.get(f"/p/{NEW_SLUG}").status_code == 200
    published = editor(client, f"/admin/forms/{NEW_SLUG}/draft")
    assert "slug" not in published  # depois da primeira publicação, o endereço não tem campo
    crafted = post(client, f"/admin/forms/{NEW_SLUG}/draft", **published, slug="outro-endereco")
    assert crafted.status_code == 422
    assert "o endereço do formulário não muda depois da primeira publicação" in text(crafted.text)
    assert client.get(f"/p/{NEW_SLUG}").status_code == 200


def test_a_new_address_keeps_the_successful_test_since_the_address_never_reaches_the_llm(
    client: TestClient, worker: TestTrigger, clock: FakeClock
) -> None:
    create(client)
    run_test(client, worker, clock)
    fields = editor(client, DRAFT)
    fields["slug"] = NEW_SLUG
    assert post(client, DRAFT, **fields).status_code == 303

    response = publish(client, slug=NEW_SLUG)

    assert response.status_code == 303, text(response.text)


@pytest.mark.parametrize(
    ("slug", "problem"),
    [
        ("Gestão 2027", "use letras minúsculas sem acento, números e hífens"),
        ("demo", "já existe um formulário com este endereço"),
        ("import", "este endereço é reservado"),
        ("default", "este endereço é reservado"),
    ],
)
def test_an_address_change_is_checked_like_a_new_address(
    client: TestClient, slug: str, problem: str
) -> None:
    create(client)
    fields = editor(client, DRAFT)
    fields["slug"] = slug

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    assert problem in field_error(response, "slug")
    assert tags(response.text, "input", name="slug", value=slug)  # o que ela escreveu volta
    assert client.get(DRAFT).status_code == 200  # o formulário segue no endereço de antes


def test_an_address_whose_destination_outlived_its_form_is_refused(
    client: TestClient, deps: Deps, receiver: Receiver
) -> None:
    """O destino guardado de um formulário que saiu do código passaria ao formulário novo com o
    mesmo endereço, e os pedidos dele iriam para o sistema do antigo, com o segredo do antigo."""
    with deps.sessions.begin() as session:  # o formulário saiu do código, o destino ficou
        destinations.save(
            session,
            deps.cipher,
            form_slug=IMPORTED,
            url=receiver.url("/antigo"),
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
        )
    create(client)
    fields = editor(client, DRAFT)
    fields["slug"] = IMPORTED

    created = create(client, title="Outro diagnóstico", slug=IMPORTED)
    renamed = post(client, DRAFT, **fields)
    imported = import_form(client, DEFINITION)

    refusal = "este endereço ainda tem um destino de webhook guardado"
    assert created.status_code == 422 and refusal in field_error(created, "slug")
    assert renamed.status_code == 422 and refusal in field_error(renamed, "slug")
    assert imported.status_code == 422 and f"slug: {refusal}" in problems_of(imported)
    assert client.get(f"/admin/forms/{IMPORTED}").status_code == 404
    assert client.get(DRAFT).status_code == 200  # o formulário segue no endereço de antes


# --- duplicar -----------------------------------------------------------------------------------

COPY_TITLE = "Diagnóstico rápido, segunda turma"
COPY_SLUG = "diagnostico-segunda-turma"


def draft_document(deps: Deps, slug: str) -> dict[str, Any]:
    """A definição do rascunho guardado, como o editor a carrega."""
    with deps.sessions() as session:
        found = drafts.load(session, slug)
    assert found is not None, slug
    return found.document


def test_duplicating_a_database_form_copies_its_published_definition_under_a_new_address(
    client: TestClient, deps: Deps
) -> None:
    assert import_form(client, DEFINITION).status_code == 303
    source = client.get(f"/admin/forms/{IMPORTED}")
    (link,) = tags(source.text, "a", href=f"{NEW}?from={IMPORTED}")  # "Duplicar…"
    page = client.get(str(link["href"]))
    (option,) = tags(page.text, "option", value=IMPORTED)
    assert "selected" in option  # a origem já vem escolhida, com o título e o endereço sugeridos
    assert tags(page.text, "input", name="title", value="Projeto de demonstração (cópia)")
    assert tags(page.text, "input", name="slug", value=f"{IMPORTED}-copia")

    response = create(client, title=COPY_TITLE, slug=COPY_SLUG, source=IMPORTED)

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/forms/{COPY_SLUG}/draft"
    copy, original = draft_document(deps, COPY_SLUG), json.loads(DEFINITION)
    assert (copy["slug"], copy["title"]) == (COPY_SLUG, COPY_TITLE)
    assert copy["consent"]["version"] == f"{COPY_SLUG}-v1"
    for part in ("steps", "identity", "instructions", "prompt", "plan", "pdf", "examples"):
        assert copy[part] == original[part], part
    assert client.get(f"/p/{COPY_SLUG}").status_code == 404  # a cópia só vai ao ar publicada
    assert client.get(f"/p/{IMPORTED}").status_code == 200  # e a origem segue como estava
    (entry,) = audited(deps, "form.duplicate")
    assert entry.details["form_slug"] == COPY_SLUG
    assert (entry.details["source"], entry.details["source_version"]) == (IMPORTED, 1)


def test_duplicating_a_form_not_published_yet_copies_its_draft(
    client: TestClient, deps: Deps
) -> None:
    create(client)

    create(client, title=COPY_TITLE, slug=COPY_SLUG, source=SLUG)

    copy = draft_document(deps, COPY_SLUG)
    assert copy["steps"] == draft_document(deps, SLUG)["steps"]
    (entry,) = audited(deps, "form.duplicate")
    assert entry.details["source"] == SLUG and entry.details["source_version"] is None


def test_duplicating_a_migrated_form_copies_its_whole_definition(
    client: TestClient, deps: Deps, worker: TestTrigger, clock: FakeClock
) -> None:
    """O reforma-2027 viveu em código e hoje é a versão 1 de um formulário do banco: a cópia leva
    tudo dela, a pontuação, as fases com datas e o PDF, e não só as perguntas."""
    response = create(client, title=COPY_TITLE, slug=COPY_SLUG, source="reforma-2027")

    assert response.status_code == 303
    copy = draft_document(deps, COPY_SLUG)
    origin = legacy_definition("reforma-2027")
    assert {**copy, "slug": "reforma-2027", "title": origin["title"]} == {
        **origin,
        "consent": {**origin["consent"], "version": f"{COPY_SLUG}-v1"},
    }
    assert copy["consent"]["version"] == f"{COPY_SLUG}-v1"
    assert [field["name"] for field in copy["plan"]["fields"]][:3] == [
        "titulo",
        "resumo",
        "prioridades",
    ]
    (entry,) = audited(deps, "form.duplicate")
    assert (entry.details["source"], entry.details["source_version"]) == ("reforma-2027", 1)
    # A cópia passa no teste e publica, com as respostas de exemplo da origem.
    run_test(client, worker, clock, slug=COPY_SLUG)
    assert publish(client, slug=COPY_SLUG).status_code == 303


@pytest.mark.parametrize("source", ["demo", "mcc", "reforma-2027"])
def test_every_migrated_form_can_be_duplicated(client: TestClient, source: str) -> None:
    response = create(client, title=COPY_TITLE, slug=COPY_SLUG, source=source)

    assert response.status_code == 303, text(response.text)
    assert client.get(response.headers["location"]).status_code == 200


def test_a_source_that_does_not_exist_is_refused(client: TestClient) -> None:
    response = create(client, source="nao-existe")

    assert response.status_code == 422
    assert "formulário de origem não encontrado" in field_error(response, "source")


# --- a primeira publicação ----------------------------------------------------------------------

STARTER_ANSWERS = {
    "nome": "Contadora Exemplo",
    "email": "contadora@example.test",
    "whatsapp": "(11) 91234-5678",
    "equipe": "2_a_5",
    "carteira": "31_a_80",
    "desafio": "precificacao",
    "focos": ["precos", "rotina"],
    "objetivo": "Revisar os preços e organizar a rotina de prazos com a equipe.",
}
EXAMPLE_TITLE = (
    "Encontros de leitura com preparação compartilhada"  # o plano de exemplo do modelo inicial
)


def run_test(client: TestClient, worker: TestTrigger, clock: FakeClock, slug: str = SLUG) -> str:
    """Um teste do rascunho com sucesso, pela página do teste, como agora. Devolve a página dele."""
    response = post(client, f"/admin/forms/{slug}/draft/tests")
    assert response.status_code == 303, response.text
    worker.run()
    clock.advance(31)
    location: str = response.headers["location"]
    return location


def publish(client: TestClient, note: str = "Primeira versão.", slug: str = SLUG) -> httpx.Response:
    """Publica pela página da publicação, como o navegador, com a nota."""
    page = client.get(f"/admin/forms/{slug}/draft/publish")
    assert page.status_code == 200, page.text
    expected = hidden(page.text, "expected")
    return post(client, f"/admin/forms/{slug}/draft/publish", note=note, expected=expected)


def send(client: TestClient, slug: str = SLUG, **changes: str | list[str]) -> str:
    """Envia o formulário pela página pública, sem JavaScript, e devolve o caminho do pedido."""
    controls = {"_privacy": "1", "_consent_version": f"{slug}-v1", "_idem": "envio-da-lead"}
    response = client.post(f"/p/{slug}", data={**STARTER_ANSWERS, **controls, **changes})
    assert response.status_code == 303, response.text
    location: str = response.headers["location"]
    return location


def test_a_created_form_goes_live_on_its_first_publication_with_the_pdf_on_brand(
    client: TestClient, deps: Deps, worker: TestTrigger, clock: FakeClock
) -> None:
    deps.pdf = WeasyPrintRenderer()  # o PDF de verdade; o plano é o de exemplo, do LLM falso
    create(client)
    untested = publish(client)
    assert untested.status_code == 422
    assert "Teste o rascunho com o LLM antes da primeira publicação" in text(untested.text)
    run_test(client, worker, clock)

    response = publish(client)

    assert response.status_code == 303 and response.headers["location"] == f"/admin/forms/{SLUG}"
    assert "Versão 1" in text(section_of(client.get(f"/admin/forms/{SLUG}"), "versao"))
    page = send(client)
    finish(deps, job_id_of(deps, page))
    shown = text(client.get(page).text)  # a prévia do modelo inicial, na página de resultado
    assert EXAMPLE_TITLE in shown and "Seus 90 dias, fase a fase" in shown
    pdf = client.get(f"{page}/pdf").content
    printed = text_of(pdf)
    assert squash(EXAMPLE_TITLE) in squash(printed)
    for part in ("Preparado para Contadora Exemplo", "Dias 1 a 30", "Dias 61 a 90"):
        assert part in printed, part
    assert pages_of(pdf) >= 3
    fonts = fonts_of(pdf)  # a marca: Playfair nos títulos e Source Sans Pro no texto, embutidas
    assert any("SourceSerifPro" in name for name in fonts) and any(
        "SourceSansPro" in n for n in fonts
    )
    assert all(fonts.values())


# --- de ponta a ponta ---------------------------------------------------------------------------


def operate(client: TestClient, fields: dict[str, Any], op: str) -> dict[str, Any]:
    """Um botão do editor estruturado, sem JavaScript: a página volta com a mudança aplicada."""
    data = {**fields, "csrf_token": csrf_token(client), "op": op}
    response = client.post(f"{DRAFT}/edit", data=data, headers=ORIGIN)
    assert response.status_code == 200, response.text
    return form_fields(response.text, EDITOR_FORM)


BILLING = "steps.1.questions.3"  # a pergunta nova, no fim da segunda etapa
BILLING_NAME = "como_voce_cobra_os_servicos_hoje"  # o nome que sai do rótulo


def test_a_form_created_from_scratch_is_edited_tested_published_and_answered_up_to_the_pdf(
    client: TestClient, deps: Deps, worker: TestTrigger, clock: FakeClock, receiver: Receiver
) -> None:
    deps.pdf = WeasyPrintRenderer()  # o PDF de verdade; o plano é o de exemplo, do LLM falso
    # 1. Cria do zero, a partir do modelo inicial.
    assert create(client).status_code == 303
    # 2. Edita no editor estruturado, sem JSON: o título, um rótulo e uma pergunta nova de escolha,
    #    com as opções e a resposta de exemplo; o prompt passa a citá-la.
    fields = editor(client, DRAFT)
    fields["title"] = "Diagnóstico de gestão 2027"
    fields["steps.1.questions.2.label"] = "Qual é o maior desafio do escritório hoje?"
    fields = operate(client, fields, "add-question:1")
    fields[f"{BILLING}.label"] = "Como você cobra os serviços hoje?"
    fields[f"{BILLING}.kind"] = "choice"
    fields = operate(client, operate(client, fields, "add-option:1:3"), "add-option:1:3")
    fields[f"{BILLING}.options.0.label"] = "Por mensalidade fixa"
    fields[f"{BILLING}.options.1.label"] = "Por serviço avulso"
    fields[f"{BILLING}.example_option"] = "0"
    fields["prompt"] += (
        f"\n- Pergunta: Como você cobra os serviços hoje?\n  Resposta: {{{{ labels.{BILLING_NAME} }}}}"
    )
    saved = post(client, DRAFT, **fields)
    assert saved.status_code == 303, text(saved.text)
    # 3. Confere a página na prévia do celular, etapa por etapa.
    second_step = text(client.get(f"{DRAFT}/preview/frame?etapa=2").text)
    assert (
        "Como você cobra os serviços hoje?" in second_step and "Por serviço avulso" in second_step
    )
    # 4. Testa com o LLM (o falso) e o PDF de verdade, sem pedido nem entrega.
    result = run_test(client, worker, clock)
    assert "Concluído" in text(section_of(client.get(result), "resultado"))
    assert "Resposta: Por mensalidade fixa" in text(section_of(client.get(result), "enviado"))
    assert client.get(f"{result}/pdf").content.startswith(b"%PDF")
    # 5. Publica a versão 1, que vai ao ar no endereço público.
    assert publish(client, note="Primeira versão do diagnóstico de gestão.").status_code == 303
    secret = create_destination(
        client, f"/admin/destinations/forms/{SLUG}", receiver.url("/gestao")
    )
    # 6. A lead responde pela página pública, com a pergunta nova.
    page = send(client, **{BILLING_NAME: "por_servico_avulso"})
    (delivery_id,) = finish(deps, job_id_of(deps, page))
    assert pipeline.deliver(deps, delivery_id) == "succeeded"
    (request,) = receiver.requests
    event = request.json()
    assert request.signed_with(secret) and event["form"]["version"] == f"{SLUG}-v1"
    assert event["answers"][BILLING_NAME] == "por_servico_avulso"
    # A identidade do modelo inicial: o telefone sempre sem o 55, com DDD.
    assert event["lead"] == {
        "email": "contadora@example.test",
        "name": "Contadora Exemplo",
        "phone": "11912345678",
    }
    # 7. O PDF na marca, com o plano e a lead.
    pdf = client.get(f"{page}/pdf").content
    printed = text_of(pdf)
    assert squash(EXAMPLE_TITLE) in squash(printed)
    assert "Preparado para Contadora Exemplo" in printed and "Diagnóstico de gestão 2027" in printed
    assert all(fonts_of(pdf).values())
