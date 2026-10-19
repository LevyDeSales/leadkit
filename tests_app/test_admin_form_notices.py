"""Os avisos das páginas de espera e de falha no editor do rascunho, pela costura HTTP (TestClient),
como o navegador o usa: quatro campos por página, o problema ao lado do campo, o que vai para a
definição, a publicação sem teste com o LLM (é texto da página) e o aviso no ar para a lead. O
formato está em `test_form_notices.py`, e as páginas da lead em `test_public_notices.py`."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_admin import FakeClock, login
from test_admin_destinations import ADMIN_ENV
from test_admin_drafts import (
    COMPARE,
    DRAFT,
    Fields,
    TestTrigger,
    editor,
    post,
    publish,
    review,
    save,
)
from test_admin_form_editor import error_of, form_of, operate, pending, stored
from test_admin_form_pages import rows_of
from test_admin_forms import DEFINITION, SLUG, import_form, section_of
from test_form_notices import FAILED, WAITING
from test_public import VALID_FORM, tags, text

from app.api.main import create_app
from app.deps import Deps
from app.settings import Settings

WAITING_FIELDS = {
    "notices.waiting.title": "Seu primeiro passo também pode ser manual",
    "notices.waiting.text": "Baixe o material de uma página.\n\nEle não confirma a sua inscrição.",
    "notices.waiting.link.label": "Baixar o material (PDF)",
    "notices.waiting.link.url": "https://example.test/material.pdf",
}
WAITING_DOCUMENT = {
    "title": "Seu primeiro passo também pode ser manual",
    "text": "Baixe o material de uma página.\n\nEle não confirma a sua inscrição.",
    "link": {"label": "Baixar o material (PDF)", "url": "https://example.test/material.pdf"},
}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def worker(deps: Deps, clock: FakeClock) -> TestTrigger:
    return TestTrigger(deps, clock)


@pytest.fixture
def client(deps: Deps, clock: FakeClock, worker: TestTrigger) -> TestClient:
    """A operadora no admin, com o formulário do banco já importado (versão 1)."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, test_trigger=worker, clock=clock),
        follow_redirects=False,
    )
    assert login(current).status_code == 303
    assert import_form(current, DEFINITION).status_code == 303
    return current


def write_waiting_notice(fields: Fields) -> None:
    fields.update(WAITING_FIELDS)


def with_notices_definition(notices: dict[str, Any]) -> str:
    """A definição do editor (o demo do admin) com os avisos, de outro slug, para importar."""
    document = json.loads(DEFINITION)
    document["slug"] = "demo-com-avisos"
    document["consent"]["version"] = "demo-com-avisos-v1"
    document["notices"] = notices
    return json.dumps(document)


def test_the_editor_has_four_empty_fields_for_the_notice_of_each_page(client: TestClient) -> None:
    fields = editor(client)

    for page in ("waiting", "failed"):
        for name in ("title", "text", "link.label", "link.url"):
            assert fields[f"notices.{page}.{name}"] == ""
    assert "notices" not in json.loads(fields["definition"])  # não vai para o JSON do resto


def test_the_notice_fields_have_labels_and_the_link_ones_say_they_are_optional(
    client: TestClient,
) -> None:
    page = client.get(DRAFT).text
    labelled = {str(label["for"]) for label in tags(page, "label") if label.get("for")}

    for page_key in ("waiting", "failed"):
        for name in ("title", "text", "link-label", "link-url"):
            assert f"f-notices-{page_key}-{name}" in labelled
    shown = text(page)
    assert "Aviso na página de espera (opcional)" in shown
    assert "Aviso na página de falha (opcional)" in shown


def test_a_notice_written_in_the_editor_goes_to_the_draft_definition(
    client: TestClient, deps: Deps
) -> None:
    response = save(client, write_waiting_notice)

    assert response.status_code == 303
    assert stored(deps)["notices"] == {"waiting": WAITING_DOCUMENT}  # a falha não leva a chave
    assert pending(client)
    fields = editor(client)
    assert all(fields[name] == value for name, value in WAITING_FIELDS.items())


def test_a_notice_without_a_link_is_saved_without_the_link_key(
    client: TestClient, deps: Deps
) -> None:
    def text_only(fields: Fields) -> None:
        fields["notices.failed.title"] = "Aviso"
        fields["notices.failed.text"] = "Só o texto."

    assert save(client, text_only).status_code == 303

    assert stored(deps)["notices"] == {"failed": {"title": "Aviso", "text": "Só o texto."}}


def test_saving_a_definition_with_notices_unchanged_keeps_the_version_as_it_is(
    client: TestClient, deps: Deps
) -> None:
    notices = {"waiting": WAITING, "failed": FAILED}
    definition = with_notices_definition(notices)
    assert import_form(client, definition).status_code == 303
    draft = "/admin/forms/demo-com-avisos/draft"

    fields = editor(client, draft)
    assert fields["notices.waiting.title"] == WAITING["title"]
    assert fields["notices.failed.link.url"] == FAILED["link"]["url"]
    assert save(client, draft=draft).status_code == 303

    assert stored(deps, "demo-com-avisos") == json.loads(definition)
    assert "Rascunho pendente" not in text(rows_of(client.get("/admin/forms"))["demo-com-avisos"])


def test_clearing_every_field_of_a_notice_takes_it_out_of_the_definition(
    client: TestClient, deps: Deps
) -> None:
    assert import_form(client, with_notices_definition({"waiting": WAITING})).status_code == 303
    draft = "/admin/forms/demo-com-avisos/draft"

    def clear(fields: Fields) -> None:
        for name in WAITING_FIELDS:
            fields[name] = ""

    assert save(client, clear, draft=draft).status_code == 303

    assert "notices" not in stored(deps, "demo-com-avisos")  # nem a chave vazia
    assert "Rascunho pendente" in text(rows_of(client.get("/admin/forms"))["demo-com-avisos"])


def test_other_editor_operations_keep_the_notices_the_page_carries(client: TestClient) -> None:
    fields = editor(client)
    write_waiting_notice(fields)

    after = form_of(operate(client, fields, "add-question:0"))

    assert {name: after[name] for name in WAITING_FIELDS} == WAITING_FIELDS


@pytest.mark.parametrize(
    ("change", "field", "problem"),
    [
        (
            {"notices.waiting.title": "", "notices.waiting.text": "Texto."},
            "notices.waiting.title",
            "campo obrigatório",
        ),
        (
            {"notices.failed.title": "Aviso"},
            "notices.failed.text",
            "campo obrigatório",
        ),
        (
            {
                "notices.waiting.title": "Aviso",
                "notices.waiting.text": "Texto.",
                "notices.waiting.link.label": "Baixar",
            },
            "notices.waiting.link.url",
            "campo obrigatório",
        ),
        (
            {
                "notices.waiting.title": "Aviso",
                "notices.waiting.text": "Texto.",
                "notices.waiting.link.url": "https://example.test/material.pdf",
            },
            "notices.waiting.link.label",
            "campo obrigatório",
        ),
        (
            {**WAITING_FIELDS, "notices.waiting.link.url": "http://example.test/material.pdf"},
            "notices.waiting.link.url",
            "use um endereço que comece por https://",
        ),
        (
            {**WAITING_FIELDS, "notices.waiting.link.url": "javascript:alert(1)"},
            "notices.waiting.link.url",
            "use um endereço que comece por https://",
        ),
        (
            {**WAITING_FIELDS, "notices.waiting.text": "x" * 2001},
            "notices.waiting.text",
            "use no máximo 2000 caracteres",
        ),
    ],
    ids=["title", "text", "url", "label", "http", "javascript", "long-text"],
)
def test_a_problem_in_a_notice_shows_next_to_its_field_and_nothing_is_saved(
    client: TestClient, change: dict[str, str], field: str, problem: str
) -> None:
    fields = editor(client)
    fields.update(change)

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    assert problem in error_of(response, field)
    assert problem in text(section_of(response, "problemas"))
    controls = [*tags(response.text, "input"), *tags(response.text, "textarea")]
    assert [c.get("aria-invalid") for c in controls if c.get("name") == field] == ["true"]
    assert not pending(client)


def test_the_problem_names_the_page_and_the_field_in_the_list_of_problems(
    client: TestClient,
) -> None:
    fields = editor(client)
    fields.update(WAITING_FIELDS)
    fields["notices.waiting.link.url"] = "ftp://example.test/material.pdf"

    response = post(client, DRAFT, **fields)

    listed = text(section_of(response, "problemas"))
    assert "Aviso na página de espera · endereço do link:" in listed


def test_a_notice_is_page_text_the_publication_needs_no_test_for(
    client: TestClient, deps: Deps
) -> None:
    assert save(client, write_waiting_notice).status_code == 303

    assert "não precisa de teste" in text(review(client).text)
    assert publish(client, note="Aviso da espera.").status_code == 303

    reading = client.get(f"/admin/forms/{SLUG}")
    shown = text(section_of(reading, "pagina"))
    assert "Aviso da página de espera: Seu primeiro passo também pode ser manual" in shown
    assert "Baixe o material de uma página." in shown
    assert "https://example.test/material.pdf" in shown
    assert "Aviso da página de falha" not in shown


def test_the_comparison_shows_the_notices_in_their_own_section(client: TestClient) -> None:
    assert save(client, write_waiting_notice).status_code == 303
    assert publish(client, note="Aviso da espera.").status_code == 303

    page = client.get(COMPARE, params={"from": "1", "to": "2"})

    assert page.status_code == 200
    assert "https://example.test/material.pdf" in text(section_of(page, "avisos"))
    assert not tags(page.text, "li", id="avisos")  # a seção mudou: não está em "sem mudança"


def test_a_published_notice_reaches_the_waiting_page_of_the_next_lead(
    client: TestClient, deps: Deps
) -> None:
    assert save(client, write_waiting_notice).status_code == 303
    assert publish(client, note="Aviso da espera.").status_code == 303
    lead = TestClient(client.app, follow_redirects=True)
    answers = {**VALID_FORM, "_consent_version": "demo-definicao-v1"}

    page = lead.post(f"/p/{SLUG}", data=answers)

    assert page.status_code == 200 and "Estamos montando seu plano" in text(page.text)
    assert "Seu primeiro passo também pode ser manual" in text(page.text)
    assert tags(page.text, "a", href="https://example.test/material.pdf")


def test_the_definition_json_refuses_the_notices_that_have_their_own_fields(
    client: TestClient,
) -> None:
    fields = editor(client)
    fields["definition"] = json.dumps({**json.loads(fields["definition"]), "notices": {}})

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    assert "fica no editor estruturado, mais acima; tire do JSON" in text(
        section_of(response, "problemas")
    )
