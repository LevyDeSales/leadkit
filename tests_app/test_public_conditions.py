"""As perguntas condicionais na página pública: o que o servidor entrega para o JavaScript (as
condições compiladas, os atributos de cada pergunta e opção), o que a página faz sem JavaScript e a
versão dos arquivos pelo conteúdo. O avaliador do navegador contra o do servidor está em
`test_conditions_js.py`; a gravação e o envio, em `test_form_conditions_flow.py`; a prévia do admin,
em `test_admin_form_preview.py`."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import publish
from fastapi.testclient import TestClient
from test_admin_destinations import ADMIN_ENV
from test_form_conditions import CONSENT, EMPLOYEE, SLUG, compiled, edited, rule_of
from test_public import tags, text

from app.api.main import create_app
from app.deps import Deps
from app.forms import FormSpec
from app.models import Submission
from app.settings import Settings
from app.web import templating

STATIC = Path(templating.__file__).parent / "static"


def digest(name: str) -> str:
    """Os 12 primeiros caracteres do sha256 do arquivo, calculados aqui, sem o código do app."""
    return hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]


@pytest.fixture
def conditional(deps: Deps) -> FormSpec:
    spec = compiled()
    publish(deps, spec)
    return spec


@pytest.fixture
def client(deps: Deps, conditional: FormSpec) -> TestClient:
    async def fake_trigger(*args: object) -> str | None:
        return "run-fake"

    return TestClient(create_app(trigger=fake_trigger))


# --- a versão dos arquivos pelo conteúdo --------------------------------------------------------


def test_the_scripts_are_versioned_by_the_hash_of_their_content(client: TestClient) -> None:
    html = client.get(f"/p/{SLUG}").text

    sources = [script["src"] for script in tags(html, "script") if script.get("src")]
    assert sources == [
        f"/static/js/conditions.js?v={digest('js/conditions.js')}",
        f"/static/js/public.js?v={digest('js/public.js')}",
    ]  # o avaliador vem antes: o public.js o usa ao começar
    assert "conditional-v3" not in html


def test_the_public_stylesheet_is_versioned_by_the_hash_of_its_content(client: TestClient) -> None:
    html = client.get(f"/p/{SLUG}").text

    (sheet,) = [link for link in tags(html, "link") if link.get("rel") == "stylesheet"]
    assert sheet["href"] == f"/static/css/public.css?v={digest('css/public.css')}"


def test_the_admin_stylesheet_and_script_are_versioned_by_the_hash_of_their_content(
    deps: Deps,
) -> None:
    deps.settings = Settings(_env_file=None, **ADMIN_ENV)  # type: ignore[call-arg]
    admin = TestClient(create_app(), follow_redirects=False)

    html = admin.get("/admin/login").text

    (sheet,) = [link for link in tags(html, "link") if link.get("rel") == "stylesheet"]
    assert sheet["href"] == f"/static/css/admin.css?v={digest('css/admin.css')}"
    sources = [script["src"] for script in tags(html, "script") if script.get("src")]
    assert f"/static/js/admin.js?v={digest('js/admin.js')}" in sources


def test_a_changed_file_gets_another_version_and_an_unchanged_one_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    script = tmp_path / "app.js"

    def write(content: str, second: int) -> str:
        script.write_text(content, encoding="utf-8")
        os.utime(script, ns=(second * 10**9, second * 10**9))  # editar muda a data do arquivo
        return templating.static_url("app.js")

    first = write("console.log(1)", 1)
    second = write("console.log(2)", 2)
    again = write("console.log(1)", 3)

    assert first != second
    assert again == first  # o conteúdo voltou: a versão volta
    assert re.fullmatch(r"/static/app\.js\?v=[0-9a-f]{12}", first)


def test_a_path_outside_the_static_folder_has_no_url() -> None:
    with pytest.raises(ValueError, match="fora de static"):
        templating.static_url("../templates/public/form.html")


def test_the_versioned_address_serves_the_file(client: TestClient) -> None:
    response = client.get(f"/static/js/conditions.js?v={digest('js/conditions.js')}")

    assert response.status_code == 200
    assert "FormConditions" in response.text


# --- o que a página entrega ao JavaScript -------------------------------------------------------

DATA_BLOCK = re.compile(
    r'<script type="application/json" data-conditions>(.*?)</script>', re.DOTALL
)


def question_tags(html: str) -> dict[str, dict[str, str | None]]:
    """Os elementos de pergunta da página (`data-question`), pelo nome da pergunta."""
    found = {}
    for tag in ("fieldset", "div"):
        for element in tags(html, tag):
            if "data-question" in element:
                found[str(element["data-question"])] = element
    return found


def test_the_page_carries_the_compiled_conditions_in_a_json_block(
    client: TestClient, conditional: FormSpec
) -> None:
    html = client.get(f"/p/{SLUG}").text

    (block,) = DATA_BLOCK.findall(html)
    assert json.loads(block) == conditional.conditions.page_data  # type: ignore[union-attr]


def test_a_condition_with_markup_in_a_value_cannot_close_the_json_block(deps: Deps) -> None:
    risky = '</script><img src=x onerror="alert(1)">'
    document = edited(
        ("steps.0.questions.5.show_when", rule_of(field="nome", operator="contains", value=risky))
    )
    publish(deps, compiled(document))

    html = TestClient(create_app()).get(f"/p/{SLUG}").text

    (block,) = DATA_BLOCK.findall(html)
    assert json.loads(block)[5]["show_when"]["rules"][0]["value"] == risky  # o valor chega inteiro
    assert "<img" not in html  # e escapado: nada fecha o bloco antes da hora


def test_a_form_without_conditions_sends_no_json_block(client: TestClient) -> None:
    html = client.get("/p/demo").text

    assert not DATA_BLOCK.findall(html)
    assert "data-question" in html  # os atributos de pergunta são os mesmos em todo formulário


def test_each_question_and_option_says_its_name_and_value(client: TestClient) -> None:
    html = client.get(f"/p/{SLUG}").text

    assert re.findall(r'data-question="([^"]+)"', html) == [
        "nome",
        "email",
        "whatsapp",
        "situacao",
        "clientes_proprios",
        "carteira",
        "nome_escritorio",
        "equipe",
        "dor_principal",
        "dores_adicionais",
        "outra_dor",
        "pausa_7_dias",
        "resultado",
        "pergunta",
    ]
    options = [str(label["data-option"]) for label in tags(html, "label") if "data-option" in label]
    assert options[:7] == [
        "clt_escritorio",
        "clt_fora",
        "estudante",
        "autonomo",
        "escritorio_proprio",
        "sim",
        "nao",
    ]
    assert "zero" in options and "um_a_dez" in options


def test_without_javascript_every_question_and_option_is_shown_and_enabled(
    client: TestClient,
) -> None:
    html = client.get(f"/p/{SLUG}").text

    for element in question_tags(html).values():
        assert "hidden" not in element
    for label in tags(html, "label"):
        assert "hidden" not in label
    for field in tags(html, "input") + tags(html, "textarea"):
        assert "disabled" not in field  # só o JavaScript esconde e desliga


def test_a_required_question_with_a_condition_is_not_called_optional(client: TestClient) -> None:
    page = text(client.get(f"/p/{SLUG}").text)

    assert "Quantos clientes? (opcional)" not in page  # obrigatória quando aparece
    assert "Quantas pessoas na equipe? (opcional)" not in page
    assert "Quantos clientes?" in page and "Quantas pessoas na equipe?" in page
    assert "Nome do escritório (opcional)" in page  # opcional com condição segue opcional
    assert "WhatsApp (opcional)" in page


def test_a_required_question_with_a_condition_asks_for_nothing_before_it_shows_up(
    client: TestClient,
) -> None:
    """Sem JavaScript, a página mostra tudo; um `required` aqui travaria o envio de quem não vê a
    pergunta no caminho dela. O JavaScript põe o `required` quando a pergunta aparece."""
    html = client.get(f"/p/{SLUG}").text

    questions = question_tags(html)
    assert "data-required-when-shown" in questions["carteira"]
    assert "data-required-when-shown" in questions["equipe"]
    assert "data-required-when-shown" not in questions["situacao"]
    assert "data-required-when-shown" not in questions["nome_escritorio"]
    assert "data-required-group" not in questions["carteira"]
    for field in tags(html, "input", name="carteira") + tags(html, "input", name="equipe"):
        assert "required" not in field
    assert all("required" in f for f in tags(html, "input", name="situacao"))  # sem condição


def test_a_step_with_only_required_conditional_questions_is_not_called_optional(
    deps: Deps,
) -> None:
    only_conditional = edited(
        ("steps.2.questions.0.show_when", rule_of(field="situacao", value="autonomo")),
        ("steps.2.questions.1.required", False),
    )
    publish(deps, compiled(only_conditional))

    page = text(TestClient(create_app()).get(f"/p/{SLUG}").text)

    assert "O resultado" in page and "O resultado (opcional)" not in page


def test_there_is_a_live_region_for_the_script_to_announce_what_appears(
    client: TestClient,
) -> None:
    html = client.get(f"/p/{SLUG}").text

    (region,) = [p for p in tags(html, "p") if "data-conditions-status" in p]
    assert region["role"] == "status" and region["aria-live"] == "polite"
    assert "sr-only" in str(region["class"])


# --- sem JavaScript -----------------------------------------------------------------------------


def post_without_javascript(client: TestClient, key: str, **answers: str) -> None:
    """O navegador sem JavaScript manda todos os campos de texto, em branco os que a lead não
    preencheu, e só as opções marcadas."""
    response = client.post(
        f"/p/{SLUG}",
        data={
            **{k: v for k, v in EMPLOYEE.items() if k not in ("carteira", "equipe")},
            "whatsapp": "",
            "nome_escritorio": "",
            "outra_dor": "",
            "pergunta": "",
            "_privacy": "1",
            "_consent_version": CONSENT["text_version"],
            "_idem": key,
            **answers,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def stored_answers(deps: Deps) -> dict[str, object]:
    with deps.sessions() as session:
        return dict(session.scalars(sa.select(Submission.answers)).one())


def test_a_browser_without_javascript_sends_the_hidden_branch_blank_and_it_is_accepted(
    deps: Deps, client: TestClient
) -> None:
    """As perguntas que a condição esconde não travam o envio, nem entram nas respostas."""
    post_without_javascript(client, "sem-javascript", clientes_proprios="nao")

    answers = stored_answers(deps)
    assert answers["clientes_proprios"] == "nao"
    assert not {"carteira", "equipe", "nome_escritorio", "outra_dor"} & answers.keys()


def test_a_browser_without_javascript_that_fills_the_hidden_branch_has_it_dropped(
    deps: Deps, client: TestClient
) -> None:
    post_without_javascript(
        client,
        "sem-javascript-2",
        clientes_proprios="nao",
        nome_escritorio="Escritório Digitado",
        carteira="um_a_dez",
    )

    answers = stored_answers(deps)
    assert "nome_escritorio" not in answers and "carteira" not in answers
