"""Formulários do banco pela costura HTTP (TestClient): a definição importada no admin vira a versão
1 publicada, a página pública e a API atendem o formulário, e o pedido vai até o PDF e a entrega."""

from __future__ import annotations

import html
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from conftest import DEMO_ANSWERS, LEGACY_SLUGS, Receiver
from fastapi.testclient import TestClient
from test_admin import ORIGIN, FakeClock, csrf_token, finish, login, section
from test_admin_destinations import ADMIN_ENV, create
from test_public import job_id_of, tags, text
from test_weasyprint_pdf import fonts_of, pages_of, squash, text_of

from app import pipeline
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.api.main import create_app
from app.deps import Deps
from app.forms import UnknownForm
from app.forms.definition import fingerprint
from app.forms.stored import StoredRegistry
from app.models import AuditLog, Form, FormVersion
from app.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
# A definição equivalente ao demo em código, sem a pontuação (ticket 04), com o slug próprio.
DEFINITION = (FIXTURES / "demo_definition.json").read_text(encoding="utf-8")
SLUG = "demo-definicao"
CONSENT_VERSION = "demo-definicao-v1"
EXAMPLE_TITLE = "Organizar atividades do projeto"  # o plano de exemplo da definição


@pytest.fixture
def triggered() -> list[tuple[uuid.UUID, int]]:
    return []


@pytest.fixture
def client(deps: Deps, triggered: list[tuple[uuid.UUID, int]]) -> TestClient:
    """O navegador da operadora, já no admin, e o da lead: o mesmo app, sem o Hatchet."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        triggered.append((job_id, attempt))
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, clock=FakeClock()), follow_redirects=False
    )
    assert login(current).status_code == 303
    return current


def import_form(
    client: TestClient,
    definition: str = "",
    *,
    upload: bytes | None = None,
    note: str = "",
    confirm_contact: bool = False,
) -> httpx.Response:
    """Importa pelo formulário do admin, como o navegador: colado no campo ou num arquivo."""
    files = {"file": ("definicao.json" if upload else "", upload or b"", "application/json")}
    data = {"csrf_token": csrf_token(client), "definition": definition, "note": note}
    if confirm_contact:
        data["confirm_contact"] = "on"
    return client.post("/admin/forms/import", data=data, files=files, headers=ORIGIN)


def section_of(response: httpx.Response, section_id: str) -> str:
    """Um trecho da página pelo id, seja `section` ou `div`."""
    found = re.search(rf'<(section|div)[^>]*id="{section_id}".*?</\1>', response.text, re.DOTALL)
    assert found is not None, section_id
    return found.group(0)


def test_an_imported_definition_is_published_as_version_1_at_its_public_address(
    client: TestClient,
) -> None:
    response = import_form(client, DEFINITION)

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/forms/{SLUG}"  # a leitura do formulário
    published = text(client.get(response.headers["location"]).text)
    assert "Projeto de demonstração publicado na versão 1" in published
    assert f"http://testserver/p/{SLUG}" in published
    page = client.get(f"/p/{SLUG}")
    assert page.status_code == 200
    shown = text(page.text)
    for label in (
        "Conte como você organiza seu projeto fictício",
        "Nome do escritório (opcional)",
        "Quantas pessoas trabalham no escritório, contando com você?",
        "Mais de 15 pessoas",
        "O que você quer alcançar nos próximos 90 dias?",
    ):
        assert label in shown, label


def send_form(client: TestClient, slug: str = SLUG, **changes: str) -> str:
    """Envia o formulário pela página pública, sem JavaScript, e devolve o caminho do pedido."""
    controls = {"_privacy": "1", "_consent_version": CONSENT_VERSION, "_idem": "chave-do-envio"}
    response = client.post(f"/p/{slug}", data={**DEMO_ANSWERS, **controls, **changes})
    assert response.status_code == 303, response.text
    return response.headers["location"]


def test_a_request_to_an_imported_form_ends_with_the_pdf_delivered_to_its_destination(
    client: TestClient, deps: Deps, receiver: Receiver
) -> None:
    deps.pdf = WeasyPrintRenderer()  # o PDF de verdade; o plano é o de exemplo, do LLM falso
    assert import_form(client, DEFINITION).status_code == 303
    secret = create(client, f"/admin/destinations/forms/{SLUG}", receiver.url("/demo-definicao"))

    page = send_form(client)
    (delivery_id,) = finish(deps, job_id_of(deps, page))
    assert pipeline.deliver(deps, delivery_id) == "succeeded"

    (request,) = receiver.requests  # o destino que o slug resolve
    assert request.path == "/demo-definicao" and request.signed_with(secret)
    event = request.json()
    assert event["event"] == "plano.concluido"
    assert event["form"] == {"slug": SLUG, "version": "demo-definicao-v1"}
    assert event["plan"]["titulo"] == EXAMPLE_TITLE
    shown = text(client.get(page).text)  # a prévia da definição, na página de resultado
    assert "Seu plano está pronto, Contadora" in shown and EXAMPLE_TITLE in shown
    download = client.get(f"{page}/pdf")
    assert download.status_code == 200 and download.content.startswith(b"%PDF")
    pdf = download.content
    assert squash(EXAMPLE_TITLE) in squash(text_of(pdf))
    assert "Preparado para Contadora Exemplo" in text_of(pdf)
    assert pages_of(pdf) >= 1 and all(fonts_of(pdf).values())


def test_the_json_api_takes_submissions_to_an_imported_form(
    client: TestClient, deps: Deps, triggered: list[tuple[uuid.UUID, int]]
) -> None:
    assert import_form(client, DEFINITION).status_code == 303
    body = {"answers": DEMO_ANSWERS, "consent": {"privacy": True, "text_version": CONSENT_VERSION}}

    response = client.post(f"/v1/forms/{SLUG}/submissions", json=body)

    assert response.status_code == 202, response.text
    assert len(triggered) == 1
    accepted = response.json()
    finish(deps, triggered[0][0])
    detail = text(client.get(f"/admin/jobs/{accepted['public_id']}").text)
    assert f"{SLUG} · versão demo-definicao-v1" in detail
    assert "prompt demo-definicao-v1" in detail
    token = {"Authorization": f"Bearer {accepted['token']}"}
    status = client.get(f"/v1/jobs/{accepted['public_id']}", headers=token).json()
    assert status["status"] == "completed"
    pdf = client.get(f"/v1/jobs/{accepted['public_id']}/pdf", headers=token)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    refused = client.post(f"/v1/forms/{SLUG}/submissions", json={**body, "answers": {}})
    assert refused.status_code == 422  # validado pelas perguntas da definição


def test_the_definition_can_come_in_a_file_which_counts_over_the_pasted_text(
    client: TestClient,
) -> None:
    response = import_form(client, "texto colado que não é JSON", upload=DEFINITION.encode())

    assert response.status_code == 303
    assert client.get(f"/p/{SLUG}").status_code == 200


def test_importing_needs_the_session_and_the_csrf_token(client: TestClient, deps: Deps) -> None:
    anonymous = TestClient(client.app, follow_redirects=False)
    form = {"definition": DEFINITION}

    refused = [
        anonymous.get("/admin/forms/import"),
        anonymous.post("/admin/forms/import", data={"csrf_token": "x", **form}, headers=ORIGIN),
        client.post("/admin/forms/import", data={"csrf_token": "errado", **form}, headers=ORIGIN),
        client.post("/admin/forms/import", data={"csrf_token": csrf_token(client), **form}),
    ]

    assert [response.status_code for response in refused] == [303, 303, 403, 403]
    assert all(r.headers["location"].startswith("/admin/login") for r in refused[:2])
    assert stored_slugs(deps) == []


def test_the_import_is_audited_in_the_operator_name_without_the_definition(
    client: TestClient, deps: Deps
) -> None:
    response = import_form(client, DEFINITION, note="Primeira versão do diagnóstico")

    confirmation = text(client.get(response.headers["location"]).text)
    assert "Nota Primeira versão do diagnóstico" in confirmation
    assert "Publicado operadora ·" in confirmation
    with deps.sessions() as session:
        (entry,) = session.scalars(sa.select(AuditLog))
    assert (entry.actor, entry.action, entry.target_type) == ("operadora", "form.import", "form")
    assert set(entry.details) == {"form_slug", "version", "fingerprint"}
    assert entry.details["form_slug"] == SLUG and entry.details["version"] == 1
    assert re.fullmatch(r"[0-9a-f]{64}", entry.details["fingerprint"])


NEWER_INTRO = "Versão 2 do diagnóstico, publicada com o app no ar."


def store_version(deps: Deps, number: int, document: dict[str, Any]) -> None:
    """Uma versão nova do formulário importado, gravada como a publicação vai gravar (ticket 07)."""
    with deps.sessions.begin() as session:
        form_id = session.scalar(sa.select(Form.id).where(Form.slug == document["slug"]))
        session.add(
            FormVersion(
                form_id=form_id,
                number=number,
                definition=document,
                fingerprint=fingerprint(document),
                note=f"Versão {number}",
                published_by="operadora",
            )
        )


def admin_text(client: TestClient, page: str) -> str:
    """O detalhe do pedido no admin, a partir do caminho da página da lead (`/d/<código>`)."""
    return text(client.get(f"/admin/jobs/{page.rsplit('/', 1)[1]}").text)


def test_a_new_version_serves_new_submissions_without_a_restart_and_old_ones_keep_theirs(
    client: TestClient, deps: Deps
) -> None:
    import_form(client, DEFINITION)
    before = send_form(client)  # na versão 1, que fica em cache
    assert NEWER_INTRO not in text(client.get(f"/p/{SLUG}").text)

    store_version(deps, 2, {**json.loads(DEFINITION), "intro": NEWER_INTRO})
    assert NEWER_INTRO in text(client.get(f"/p/{SLUG}").text)  # o app não reiniciou
    after = send_form(client, _idem="envio-da-versao-2")
    for page in (before, after):
        finish(deps, job_id_of(deps, page))

    assert "prompt demo-definicao-v1" in admin_text(client, before)
    assert "prompt demo-definicao-v2" in admin_text(client, after)
    assert f"{SLUG} · versão demo-definicao-v2" in admin_text(client, after)


@pytest.mark.parametrize(
    "change",
    [sa.update(FormVersion).values(note="outra nota"), sa.delete(FormVersion)],
    ids=["update", "delete"],
)
def test_a_published_version_never_changes_nor_goes_away(
    client: TestClient, deps: Deps, change: sa.Executable
) -> None:
    response = import_form(client, DEFINITION, note="A nota publicada")

    refused = pytest.raises(sa.exc.IntegrityError, match="uma versão publicada não muda nem sai")
    with refused, deps.sessions.begin() as session:  # nem por fora do app
        session.execute(change)

    assert "Nota A nota publicada" in text(client.get(response.headers["location"]).text)


def test_the_fingerprint_is_the_sha256_of_the_canonical_json() -> None:
    # sha256 de '{"a":"é","b":[1,2.5,null,true]}', calculado fora do app (shasum -a 256)
    expected = "0c187fb652afca8017b5ef38178eeef09a9dfef844316f80ba4207cdf1995ee5"

    assert fingerprint({"b": [1, 2.5, None, True], "a": "é"}) == expected
    assert fingerprint(json.loads('{ "a" : "\\u00e9",\n "b": [1, 2.5, null, true] }')) == expected


def test_the_imported_form_has_its_place_in_the_webhook_destinations(
    client: TestClient, receiver: Receiver
) -> None:
    published = import_form(client, DEFINITION).headers["location"]

    confirmation = section(client.get(published).text, "destino")
    assert "Sem destino do webhook: os planos deste formulário não são entregues" in text(
        confirmation
    )
    assert tags(confirmation, "a", href=f"/admin/destinations/forms/{SLUG}")
    destinations_page = client.get("/admin/destinations").text
    assert "Projeto de demonstração" in section(destinations_page, "formularios")
    assert SLUG in section(destinations_page, "sem-destino")

    create(client, "/admin/destinations/default", receiver.url("/padrao"))
    shown = text(section(client.get(published).text, "destino"))
    assert f"Os planos vão para o destino padrão ({receiver.host})" in shown


def test_the_registry_answers_every_version_even_with_a_cache_of_one(
    client: TestClient, deps: Deps
) -> None:
    import_form(client, DEFINITION)
    store_version(deps, 2, {**json.loads(DEFINITION), "intro": NEWER_INTRO})
    registry = StoredRegistry(deps.sessions, cache_size=1)
    first_intro = json.loads(DEFINITION)["intro"]

    intros = [registry.get(SLUG, number).screen.intro for number in (1, 2, 1, 2)]  # type: ignore[union-attr]

    assert intros == [first_intro, NEWER_INTRO, first_intro, NEWER_INTRO]
    assert registry.current(SLUG).version == "demo-definicao-v2"
    assert [spec.slug for spec in registry.current_forms()] == ["demo", SLUG, "mcc", "reforma-2027"]
    demo = registry.current("demo")  # um dos três que viveram em código, na versão 1
    assert (demo.version_number, demo.version) == (1, "demo-v1")
    for unknown in (
        lambda: registry.get(SLUG, 3),
        lambda: registry.get("demo", 2),
        lambda: registry.current("nao-existe"),
    ):
        with pytest.raises(UnknownForm):
            unknown()


SECRET_INTRO = "Introdução que não pode ir para o log"


def test_a_stored_version_that_no_longer_compiles_is_left_out_and_the_log_has_no_content(
    client: TestClient,
    deps: Deps,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # O `fileConfig` do alembic, que a suíte roda na subida, desliga os loggers que já existiam.
    monkeypatch.setattr(logging.getLogger("app.forms.stored"), "disabled", False)
    import_form(client, DEFINITION)
    page = send_form(client)  # na versão 1
    # Uma versão 2 que o compilador de um deploy seguinte não aceita mais.
    store_version(deps, 2, {**json.loads(DEFINITION), "intro": SECRET_INTRO, "minutes": 0})

    with caplog.at_level(logging.ERROR, logger="app.forms.stored"):
        public = client.get(f"/p/{SLUG}")
        listed = client.get("/admin/destinations")

    assert public.status_code == 404 and "Não encontramos esta página" in text(public.text)
    assert listed.status_code == 200
    assert "demo-definicao versão 2: a definição guardada não compila" in caplog.text
    assert SECRET_INTRO not in caplog.text and "minutes" not in caplog.text
    finish(deps, job_id_of(deps, page))  # o pedido da versão 1 segue a versão dele
    assert "prompt demo-definicao-v1" in admin_text(client, page)


CONTACT_PROMPT = DEFINITION.replace(
    "Diagnóstico do escritório:\\n",
    "Diagnóstico do escritório de {{ answers.nome }} ({{ answers.email }}):\\n",
    1,
)


def test_a_prompt_that_sends_contact_data_to_the_llm_needs_a_confirmation(
    client: TestClient, deps: Deps
) -> None:
    assert CONTACT_PROMPT != DEFINITION

    asked = import_form(client, CONTACT_PROMPT)

    assert asked.status_code == 422 and stored_slugs(deps) == []
    warning = text(section_of(asked, "contato"))
    assert "O prompt manda ao LLM o nome e o e-mail da lead" in warning
    assert tags(asked.text, "input", name="confirm_contact")
    assert kept_definition(asked) == CONTACT_PROMPT
    confirmed = import_form(client, CONTACT_PROMPT, confirm_contact=True)
    assert confirmed.status_code == 303 and stored_slugs(deps) == [SLUG]
    with deps.sessions() as session:
        (entry,) = session.scalars(sa.select(AuditLog))
    assert entry.details["contact_in_prompt"] == ["name", "email"]


def test_a_prompt_that_uses_the_whole_answers_asks_to_confirm_every_contact_data(
    client: TestClient, deps: Deps
) -> None:
    whole = DEFINITION.replace("Diagnóstico do escritório:\\n", "Respostas: {{ answers }}\\n", 1)
    assert whole != DEFINITION

    asked = import_form(client, whole)

    assert asked.status_code == 422 and stored_slugs(deps) == []
    assert "O prompt manda ao LLM o nome e o e-mail da lead" in text(section_of(asked, "contato"))


def test_the_import_checks_the_definition_as_the_database_keeps_it(
    client: TestClient, deps: Deps
) -> None:
    # As instruções só quebram com a chave "a" na frente, a ordem em que o banco devolve o objeto:
    # o texto colado traz "bb" antes, e a importação precisa conferir o que vai ser servido.
    document = json.loads(DEFINITION)
    document["data"] = {"ordem": {"kind": "value", "value": {"bb": 2, "a": 1}}}
    document["instructions"] = (
        '{% for nome, valor in data.ordem.items() %}{% if loop.first and nome == "a" %}'
        "{{ 1 / 0 }}{% endif %}{% endfor %}" + document["instructions"]
    )

    response = import_form(client, json.dumps(document, ensure_ascii=False))

    assert response.status_code == 422
    assert "instructions, linha 1: erro ao renderizar (ZeroDivisionError)" in problems_of(response)
    assert stored_slugs(deps) == []


def with_slug(slug: str) -> str:
    return DEFINITION.replace(f'"slug": "{SLUG}"', f'"slug": "{slug}"', 1)


def problems_of(response: httpx.Response) -> str:
    """Os problemas que a página mostra, como a operadora os lê."""
    found = re.search(r'<div[^>]*id="problemas".*?</ul>', response.text, re.DOTALL)
    assert found is not None, "a página não mostrou os problemas"
    return text(found.group(0))


def kept_definition(response: httpx.Response) -> str:
    """O texto que volta no campo da definição, para a operadora corrigir."""
    textarea = r'<textarea[^>]*id="definicao"[^>]*>(.*?)</textarea>'
    found = re.search(textarea, response.text, re.DOTALL)
    assert found is not None
    return html.unescape(found.group(1))


def stored_slugs(deps: Deps) -> list[str]:
    """Os formulários do banco, sem os três que viveram em código e que o `deps` já traz."""
    with deps.sessions() as session:
        return [slug for slug in session.scalars(sa.select(Form.slug)) if slug not in LEGACY_SLUGS]


def test_a_definition_cannot_take_the_address_of_an_existing_form(
    client: TestClient, deps: Deps
) -> None:
    response = import_form(client, with_slug("reforma-2027"))

    assert response.status_code == 422
    assert "slug: já existe um formulário com este endereço" in problems_of(response)
    assert stored_slugs(deps) == []
    assert kept_definition(response) == with_slug("reforma-2027")
    assert "Calendário de atividades" in text(client.get("/p/reforma-2027").text)


@pytest.mark.parametrize("slug", ["import", "new", "default"])
def test_a_slug_that_names_a_page_of_the_admin_or_a_scope_of_the_api_is_refused(
    client: TestClient, deps: Deps, slug: str
) -> None:
    response = import_form(client, with_slug(slug))

    assert response.status_code == 422
    assert "slug: este endereço é reservado; escolha outro" in problems_of(response)
    assert stored_slugs(deps) == []


def test_a_slug_already_imported_is_refused_and_the_first_form_stays(
    client: TestClient, deps: Deps
) -> None:
    import_form(client, DEFINITION)
    other_title = DEFINITION.replace(
        '"title": "Projeto de demonstração"', '"title": "Outro diagnóstico"', 1
    )

    response = import_form(client, other_title)

    assert response.status_code == 422
    assert "slug: já existe um formulário com este endereço" in problems_of(response)
    assert stored_slugs(deps) == [SLUG]
    assert "Projeto de demonstração" in text(client.get(f"/p/{SLUG}").text)


def test_a_definition_whose_example_pdf_does_not_render_is_refused(
    client: TestClient, deps: Deps
) -> None:
    deps.pdf = WeasyPrintRenderer()
    # O endereço montado em Jinja passa pela conferência do texto, que só lê o que está escrito:
    # quem o recusa é o adaptador do PDF, na renderização do exemplo.
    outside = DEFINITION.replace(
        "<h1>{{ plan.titulo }}</h1>",
        "<h1>{{ plan.titulo }}</h1><img src=\\\"{{ 'https' }}://cdn.example.test/logo.png\\\">",
        1,
    )
    assert outside != DEFINITION

    response = import_form(client, outside)

    assert response.status_code == 422
    assert (
        "pdf: o PDF de exemplo não foi gerado: template do PDF pediu um recurso fora de app/assets"
    ) in problems_of(response)
    assert stored_slugs(deps) == []


def without_title() -> str:
    document = json.loads(DEFINITION)
    del document["title"]
    return json.dumps(document, ensure_ascii=False, indent=2)


@pytest.mark.parametrize(
    ("definition", "problem"),
    [
        (
            '{\n  "format": 1\n  "slug": "sem-virgula"\n}',
            "definição, linha 3: JSON inválido: falta uma vírgula (coluna 3)",
        ),
        (without_title(), "title: campo obrigatório"),
        (
            DEFINITION.replace("{{ answers.tamanho_equipe }}", "{{ answers.equipe }}", 1),
            "prompt, linha 3: answers.equipe não é uma pergunta do formulário",
        ),
        ("  \n ", "definição: cole a definição em JSON ou escolha o arquivo dela"),
    ],
    ids=["json", "campo", "template", "vazia"],
)
def test_a_definition_that_does_not_compile_shows_each_problem_where_it_is(
    client: TestClient, deps: Deps, definition: str, problem: str
) -> None:
    response = import_form(client, definition)

    assert response.status_code == 422
    assert problem in problems_of(response)
    assert stored_slugs(deps) == []
