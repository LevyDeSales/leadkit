"""O aviso extra das páginas de espera e de falha (`notices` da definição), pela costura HTTP
(TestClient), como a lead o vê. Nenhum slug mora no código dos templates: o aviso é do formulário, da
versão que a lead respondeu. O formato e a conferência da definição estão em `test_form_notices.py`."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import publish
from fastapi.testclient import TestClient
from test_form_notices import FAILED, WAITING, with_notices
from test_public import VALID_FORM, assert_private, enter, job_id_of, submit, tags, text

from app import jobs
from app.api.main import create_app
from app.deps import Deps
from app.forms.definition import compile_definition
from app.models import Submission

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "public"


# --- as páginas da lead ------------------------------------------------------------------------


@pytest.fixture
def triggered() -> list[tuple[uuid.UUID, int]]:
    return []


@pytest.fixture
def client(deps: Deps, triggered: list[tuple[uuid.UUID, int]]) -> TestClient:
    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        triggered.append((job_id, attempt))
        return "run-fake"

    return TestClient(create_app(trigger=fake_trigger))


def published(deps: Deps, notices: dict[str, Any] | None, **changes: Any) -> None:
    """Publica uma versão 2 do demo com os avisos dados (a 1 segue sem eles)."""
    publish(deps, compile_definition(with_notices(notices, **changes), version_number=2))


def form_for(document: dict[str, Any]) -> dict[str, str]:
    """O envio do demo para o formulário desta definição (o texto do consentimento é o dela)."""
    return {**VALID_FORM, "_consent_version": document["consent"]["version"]}


def fail(deps: Deps, page: str) -> None:
    with deps.sessions.begin() as session:
        jobs.fail(session, job_id_of(deps, page), 1, "llm_unavailable", "OpenAI HTTP 503")


def notice_of(html: str) -> str | None:
    """O texto do aviso da página, ou None se ela não tem o bloco do aviso."""
    found = tags(html, "section", id="aviso")
    if not found:
        return None
    start = html.index('id="aviso"')
    return text(html[start : html.index("</section>", start)])


def test_a_form_without_a_notice_shows_neither_page_any_extra_block(
    client: TestClient, deps: Deps
) -> None:
    page = enter(client)
    waiting = client.get(page)
    fail(deps, page)
    failed = client.get(page)

    assert waiting.status_code == failed.status_code == 200
    assert notice_of(waiting.text) is None and notice_of(failed.text) is None
    assert "Estamos montando seu plano" in text(waiting.text)
    assert "Não conseguimos montar seu plano agora" in text(failed.text)


def test_the_waiting_page_shows_the_waiting_notice_of_the_form(
    client: TestClient, deps: Deps
) -> None:
    published(deps, {"waiting": WAITING, "failed": FAILED})
    page = enter(client)

    response = client.get(page)

    assert response.status_code == 200
    shown = notice_of(response.text)
    assert shown is not None
    assert WAITING["title"] in shown and "Baixar o material (PDF)" in shown
    assert "Enquanto o seu mapa não fica pronto" in shown
    assert "Esta pesquisa é complementar" in shown
    assert FAILED["title"] not in text(response.text)  # o da falha não aparece na espera
    (link,) = tags(response.text, "a", href="https://example.test/material.pdf")
    assert link["target"] == "_blank" and link["rel"] == "noopener noreferrer"
    assert len(tags(response.text, "div", id="progresso")) == 1  # a espera segue como era
    assert_private(response)


def test_the_failed_page_shows_the_failed_notice_of_the_form(
    client: TestClient, deps: Deps
) -> None:
    published(deps, {"waiting": WAITING, "failed": FAILED})
    page = enter(client)
    fail(deps, page)

    response = client.get(page)

    assert response.status_code == 200
    shown = notice_of(response.text)
    assert shown is not None
    assert FAILED["title"] in shown and "começar pelo material de uma página" in shown
    assert WAITING["title"] not in text(response.text)
    assert tags(response.text, "a", href="https://example.test/material.pdf")
    assert page.rsplit("/", 1)[1] in text(response.text)  # o código de atendimento segue lá
    assert "llm_unavailable" not in response.text and "OpenAI" not in response.text


def test_a_notice_for_one_page_leaves_the_other_page_as_it_was(
    client: TestClient, deps: Deps
) -> None:
    published(deps, {"failed": FAILED})
    page = enter(client)

    assert notice_of(client.get(page).text) is None
    fail(deps, page)
    assert notice_of(client.get(page).text) is not None


def test_a_notice_without_a_link_shows_the_text_alone(client: TestClient, deps: Deps) -> None:
    published(deps, {"waiting": {"title": "Aviso", "text": "Só o texto."}})
    page = enter(client)

    response = client.get(page)

    shown = notice_of(response.text)
    assert shown is not None and "Só o texto." in shown
    assert not tags(response.text, "a", target="_blank")


def test_the_text_of_the_notice_is_escaped_and_never_html(client: TestClient, deps: Deps) -> None:
    hostile = {
        "title": "<script>alert(1)</script>",
        "text": '<img src=x onerror=alert(2)> & "aspas"\n\n<b>segundo</b>',
        "link": {
            "label": "<u>Ir</u>",
            "url": 'https://example.test/"onmouseover="alert(3)',
        },
    }
    published(deps, {"waiting": hostile, "failed": hostile})
    page = enter(client)
    pages = [client.get(page).text]
    fail(deps, page)
    pages.append(client.get(page).text)

    for html in pages:
        assert "<script>alert" not in html and "<img src=x" not in html and "<b>segundo" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "&lt;b&gt;segundo&lt;/b&gt;" in html and "&lt;u&gt;Ir&lt;/u&gt;" in html
        (link,) = tags(html, "a", target="_blank")
        assert link["href"] == hostile["link"]["url"]  # o valor inteiro, sem atributo a mais
        assert "onmouseover" not in link


def test_the_paragraphs_of_the_text_are_separated_by_a_blank_line(
    client: TestClient, deps: Deps
) -> None:
    published(deps, {"waiting": {**WAITING, "text": "Primeiro.\r\n\r\nSegundo.\n \nTerceiro."}})
    page = enter(client)

    html = client.get(page).text
    start = html.index('id="aviso"')
    block = html[start : html.index("</section>", start)]

    assert block.count("<p") >= 3
    assert all(word in text(block) for word in ("Primeiro.", "Segundo.", "Terceiro."))


def test_the_notice_belongs_to_the_form_and_never_to_a_slug_in_the_templates(
    client: TestClient, deps: Deps
) -> None:
    """O aviso da Black da Virada, que a produção tinha escrito no template, é só um formulário
    comum aqui: sem o bloco na definição, o formulário de qualquer slug não mostra nada."""
    for name in ("waiting.html", "failed.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "slug ==" not in source and "cenario-ficticio" not in source, name

    document = with_notices(None, slug="cenario-ficticio")
    publish(deps, compile_definition(document))
    page = submit(client).headers["location"]  # o demo
    other = client.post("/p/cenario-ficticio", data=form_for(document), follow_redirects=False)
    assert other.status_code == 303
    blank = other.headers["location"]

    assert notice_of(client.get(blank).text) is None
    assert "Mapa da Virada" not in text(client.get(blank).text)
    assert notice_of(client.get(page).text) is None


def test_two_forms_show_each_the_notice_of_its_own_definition(
    client: TestClient, deps: Deps
) -> None:
    other = with_notices(
        {"waiting": {"title": "Aviso do outro formulário", "text": "Texto do outro."}},
        slug="outro-formulario",
    )
    publish(
        deps,
        compile_definition(with_notices({"waiting": WAITING}), version_number=2),
        compile_definition(other),
    )
    mine = enter(client)
    theirs = client.post("/p/outro-formulario", data=form_for(other), follow_redirects=False)

    assert WAITING["title"] in (notice_of(client.get(mine).text) or "")
    shown = notice_of(client.get(theirs.headers["location"]).text) or ""
    assert "Aviso do outro formulário" in shown and WAITING["title"] not in shown


def test_each_lead_sees_the_notice_of_the_version_she_answered(
    client: TestClient, deps: Deps
) -> None:
    before = enter(client)  # a versão 1, sem aviso
    published(deps, {"waiting": WAITING})
    after = submit(client, _idem="outra-renderizacao").headers["location"]  # a versão 2

    assert notice_of(client.get(before).text) is None
    assert WAITING["title"] in (notice_of(client.get(after).text) or "")


@pytest.mark.parametrize(
    "gone",
    [{"form_slug": "formulario-removido"}, {"form_version_number": 7}],
    ids=["slug", "versao"],
)
def test_the_pages_of_a_form_that_left_the_registry_still_work_without_the_notice(
    client: TestClient, deps: Deps, gone: dict[str, object]
) -> None:
    published(deps, {"waiting": WAITING, "failed": FAILED})
    page = enter(client)
    with deps.sessions.begin() as session:
        session.execute(sa.update(Submission).values(**gone))

    waiting = client.get(page)
    fail(deps, page)
    failed = client.get(page)

    assert waiting.status_code == failed.status_code == 200
    assert notice_of(waiting.text) is None and notice_of(failed.text) is None
    assert page.rsplit("/", 1)[1] in text(failed.text)
