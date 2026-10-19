"""A prévia da página pública do rascunho no admin: a página de verdade, interativa (as perguntas e
as opções condicionais aparecem e somem como para a lead), com todas as etapas, sem enviar nada e
sem guardar nada no aparelho. O que o JavaScript faz com as condições está em
`test_conditions_js.py` (o avaliador) e na conferência manual descrita no ticket; aqui, o que o
servidor entrega ao quadro."""

from __future__ import annotations

import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from test_admin import FakeClock, login
from test_admin_destinations import ADMIN_ENV
from test_admin_drafts import DRAFT, Fields, TestTrigger, save
from test_admin_forms import DEFINITION, SLUG, import_form
from test_form_conditions import DEFINITION_TEXT, compiled
from test_form_conditions import SLUG as CONDITIONAL_SLUG
from test_public import tags, text

from app.api.main import create_app
from app.deps import Deps
from app.settings import Settings

PREVIEW = f"{DRAFT}/preview"
NEW_LABEL = "Quantas pessoas trabalham com você?"


def relabeled(fields: Fields) -> None:
    fields["steps.1.questions.0.label"] = NEW_LABEL


def admin_with(deps: Deps, definition: str) -> TestClient:
    """A operadora no admin, com o formulário do banco já importado (versão 1)."""
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]
    clock = FakeClock()

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    current = TestClient(
        create_app(trigger=fake_trigger, test_trigger=TestTrigger(deps, clock), clock=clock),
        follow_redirects=False,
    )
    assert login(current).status_code == 303
    assert import_form(current, definition).status_code == 303
    return current


@pytest.fixture
def client(deps: Deps) -> TestClient:
    return admin_with(deps, DEFINITION)


@pytest.fixture
def conditional(deps: Deps) -> TestClient:
    return admin_with(deps, DEFINITION_TEXT)


def test_the_preview_page_shows_the_draft_public_page_at_phone_width(client: TestClient) -> None:
    page = client.get(PREVIEW)

    assert page.status_code == 200
    (frame,) = tags(page.text, "iframe")
    assert frame["src"] == f"{PREVIEW}/frame?etapa=1" and frame["width"] == "375"
    assert frame["name"] == "previa" and "celular" in str(frame["title"])
    steps = [link for link in tags(page.text, "a") if link.get("target") == "previa"]
    assert [link["href"] for link in steps] == [f"{PREVIEW}/frame?etapa={n}" for n in (1, 2, 3)]
    shown = text(page.text)
    assert "Nada do que você marcar é enviado" in shown and "neste aparelho" in shown
    assert "perguntas e as opções" in shown  # as condições aparecem e somem, como para a lead


def test_the_preview_frame_has_every_step_of_the_saved_draft(client: TestClient) -> None:
    save(client, relabeled)

    frame = client.get(f"{PREVIEW}/frame?etapa=1")

    assert frame.status_code == 200
    shown = text(frame.text)
    assert "Prévia" in shown and "Nada do que você marcar aqui é enviado" in shown
    assert "Nome completo" in shown and "Seus dados" in shown  # o consentimento, na 1ª etapa
    assert NEW_LABEL in shown and "De 2 a 5 pessoas" in shown  # o rascunho, não a versão 1
    assert len(tags(frame.text, "section", **{"class": "step"})) == 3  # uma seção por etapa
    assert "Gerar meu plano" in shown
    assert NEW_LABEL not in client.get(f"/p/{SLUG}").text  # o que está no ar não muda


def test_the_preview_walks_the_steps_with_the_buttons_of_the_real_page(client: TestClient) -> None:
    html = client.get(f"{PREVIEW}/frame?etapa=1").text

    buttons = [b for b in tags(html, "button") if "data-next" in b or "data-back" in b]
    assert len(buttons) == 4  # Continuar nas duas primeiras etapas, Voltar nas duas últimas
    assert all(b["type"] == "button" and "hidden" in b for b in buttons)  # o script os mostra
    (submit,) = [b for b in tags(html, "button") if "data-submit" in b]
    assert submit["type"] == "button" and "disabled" in submit  # não envia


def test_the_preview_sends_nothing_and_keeps_nothing_on_the_device(client: TestClient) -> None:
    frame = client.get(f"{PREVIEW}/frame?etapa=1")

    assert not tags(frame.text, "form")  # nada vai para lugar nenhum
    (container,) = [d for d in tags(frame.text, "div") if "data-steps" in d]
    assert "data-preview" in container
    assert "data-draft-key" not in container  # nem para o aparelho
    for hidden in tags(frame.text, "input", type="hidden"):
        assert hidden["name"] not in ("_idem", "_consent_version", "_website")
    assert frame.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in frame.headers["content-security-policy"]
    assert client.get(DRAFT).headers["x-frame-options"] == "DENY"  # as outras páginas, não
    anonymous = TestClient(client.app, follow_redirects=False)
    assert anonymous.get(f"{PREVIEW}/frame").status_code == 303


def test_the_preview_loads_the_same_scripts_as_the_page_with_their_versions(
    client: TestClient,
) -> None:
    html = client.get(f"{PREVIEW}/frame?etapa=1").text

    sources = [script["src"] for script in tags(html, "script") if script.get("src")]
    assert [re.sub(r"\?v=[0-9a-f]{12}$", "", str(src)) for src in sources] == [
        "/static/js/conditions.js",
        "/static/js/public.js",
    ]


@pytest.mark.parametrize(("asked", "start"), [("1", "0"), ("2", "1"), ("0", "0"), ("9", "2")])
def test_the_preview_starts_at_the_step_asked_or_the_nearest_one(
    client: TestClient, asked: str, start: str
) -> None:
    frame = client.get(f"{PREVIEW}/frame", params={"etapa": asked})

    (container,) = [d for d in tags(frame.text, "div") if "data-steps" in d]
    assert frame.status_code == 200 and container["data-start-step"] == start


def test_a_step_that_is_not_a_number_starts_at_the_first(client: TestClient) -> None:
    frame = client.get(f"{PREVIEW}/frame", params={"etapa": "dois"})

    (container,) = [d for d in tags(frame.text, "div") if "data-steps" in d]
    assert container["data-start-step"] == "0"


# --- as condições na prévia ---------------------------------------------------------------------


def test_the_preview_of_a_form_with_conditions_carries_them_for_the_script(
    conditional: TestClient,
) -> None:
    path = f"/admin/forms/{CONDITIONAL_SLUG}/draft/preview/frame"

    frame = conditional.get(path)

    assert frame.status_code == 200
    (block,) = re.findall(
        r'<script type="application/json" data-conditions>(.*?)</script>', frame.text, re.DOTALL
    )
    assert json.loads(block) == compiled().conditions.page_data  # type: ignore[union-attr]
    names = re.findall(r'data-question="([^"]+)"', frame.text)
    assert names[:4] == ["nome", "email", "whatsapp", "situacao"]  # todas as etapas, de uma vez
    assert "pausa_7_dias" in names and "resultado" in names
    assert "Quantos clientes? (opcional)" not in text(frame.text)  # obrigatória quando aparece


def test_the_preview_of_a_form_without_conditions_carries_no_block(client: TestClient) -> None:
    frame = client.get(f"{PREVIEW}/frame?etapa=1")

    assert "application/json" not in frame.text and "data-question" in frame.text
