"""O editor estruturado do rascunho, pela costura HTTP (TestClient), como o navegador o usa: as
etapas, as perguntas, as opções, a identidade da lead e o consentimento em campos, com incluir,
remover e reordenar sem JavaScript (a página volta inteira) e com o HTMX (volta só o trecho)."""

from __future__ import annotations

import html
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_admin import ORIGIN, FakeClock, csrf_token, login
from test_admin_destinations import ADMIN_ENV
from test_admin_drafts import (
    DRAFT,
    EDITOR_FORM,
    Fields,
    TestTrigger,
    editor,
    post,
    save,
)
from test_admin_form_pages import rows_of
from test_admin_forms import DEFINITION, SLUG, import_form, section_of
from test_public import form_fields, tags, text

from app.api.main import create_app
from app.deps import Deps
from app.forms import drafts
from app.forms.definition import fingerprint
from app.forms.definition.changes import compare, llm_fingerprint
from app.models import Form, FormDraft
from app.settings import Settings


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


def stored(deps: Deps, slug: str = SLUG) -> dict[str, Any]:
    """A definição do rascunho, como o banco a guarda."""
    with deps.sessions() as session:
        found = drafts.load(session, slug)
    assert found is not None
    return found.document


def pending(client: TestClient, slug: str = SLUG) -> bool:
    return "Rascunho pendente" in text(rows_of(client.get("/admin/forms"))[slug])


def test_the_editor_shows_steps_questions_options_identity_and_consent_as_fields(
    client: TestClient,
) -> None:
    fields = editor(client)

    assert (fields["title"], fields["minutes"]) == ("Projeto de demonstração", "3")
    assert fields["steps.0.title"] == "Vamos começar por você"
    assert fields["steps.0.questions.0.label"] == "Nome completo"
    assert fields["steps.0.questions.0.kind"] == "text"
    assert fields["steps.0.questions.0.max_length"] == "120"
    assert fields["steps.0.questions.0.required"] == "on"
    assert "steps.0.questions.2.required" not in fields  # o nome do escritório é opcional
    assert fields["steps.0.questions.2.contact"] == "on"
    assert fields["steps.1.questions.0.options.1.value"] == "2_a_5"
    assert fields["steps.1.questions.0.options.1.label"] == "De 2 a 5 pessoas"
    assert fields["steps.1.questions.0.example_option"] == "1"  # a opção do exemplo
    assert fields["steps.2.questions.0.kind"] == "textarea"
    assert (fields["identity.email"], fields["identity.name"]) == ("0.1", "0.0")
    assert fields["identity.phone"] == ""  # o formulário não pede telefone
    assert fields["consent.processing"].startswith("Concordo com o uso das minhas respostas")
    assert fields["consent.marketing.email.offered"] == "on"
    assert "consent.marketing.whatsapp.offered" not in fields


def test_every_field_of_the_editor_has_a_label(client: TestClient) -> None:
    page = client.get(DRAFT).text
    labelled = {str(label["for"]) for label in tags(page, "label") if label.get("for")}
    controls = [*tags(page, "input"), *tags(page, "select"), *tags(page, "textarea")]
    named = [
        control
        for control in controls
        if control.get("name") and control.get("type") not in ("hidden", "radio", "checkbox")
    ]
    assert named
    for control in named:
        assert control.get("id") in labelled or control.get("aria-label"), control["name"]


def test_saving_the_editor_unchanged_leaves_the_published_version_as_it_is(
    client: TestClient, deps: Deps
) -> None:
    response = save(client)

    assert response.status_code == 303
    assert not pending(client)
    assert stored(deps) == json.loads(DEFINITION)


VERBOSE = "demo-por-extenso"
VERBOSE_DRAFT = f"/admin/forms/{VERBOSE}/draft"


def verbose_definition() -> dict[str, Any]:
    """O demo com o padrão do formato por extenso, como uma importação pode trazer."""
    verbose = json.loads(DEFINITION)
    verbose["slug"] = VERBOSE
    verbose["consent"]["version"] = f"{VERBOSE}-v1"
    for step in verbose["steps"]:
        for question in step["questions"]:
            question.setdefault("required", True)
            question.setdefault("contact", False)
    verbose["consent"]["marketing"]["whatsapp"] = None
    return verbose


def test_a_definition_that_writes_the_defaults_out_saves_unchanged_too(
    client: TestClient, deps: Deps
) -> None:
    """Uma definição importada pode trazer o padrão do formato por extenso: o editor não o
    escreve, mas salvar sem mudança não pode criar um rascunho pendente por isso."""
    assert import_form(client, json.dumps(verbose_definition())).status_code == 303

    response = save(client, draft=VERBOSE_DRAFT)

    assert response.status_code == 303
    assert not pending(client, VERBOSE)


def changed_lines(section: str) -> list[str]:
    """As linhas que entraram e as que saíram numa seção da comparação, com o + e o -."""
    found = re.findall(r'<span class="diff__(?:add|del)">(.*?)</span>', section, re.DOTALL)
    return [html.unescape(line) for line in found]


def test_the_comparison_shows_only_what_changed_not_the_defaults_the_editor_leaves_out(
    client: TestClient,
) -> None:
    """A versão importada guarda `"required": true`; o editor não escreve o padrão. Mudar uma
    etapa e salvar não pode fazer a comparação mostrar as outras perguntas como mudadas."""
    assert import_form(client, json.dumps(verbose_definition())).status_code == 303
    assert save(client, relabel_first_step, draft=VERBOSE_DRAFT).status_code == 303

    page = client.get(
        f"/admin/forms/{VERBOSE}/versions/compare", params={"from": "1", "to": "rascunho"}
    )

    lines = changed_lines(section_of(page, "etapas"))
    assert [line[0] for line in lines] == ["-", "+"]
    assert "Vamos começar por você" in lines[0] and "Comece por você" in lines[1]
    assert not [line for line in lines if "required" in line or "contact" in line]


def test_undoing_a_change_leaves_no_pending_draft_even_if_the_import_wrote_the_defaults_out(
    client: TestClient, deps: Deps
) -> None:
    """O rascunho que volta a dizer o mesmo que a versão publicada sai, mesmo escrito de outro
    jeito (sem os padrões por extenso que a importação trouxe): não há o que publicar."""
    assert import_form(client, json.dumps(verbose_definition())).status_code == 303
    assert save(client, relabel_first_step, draft=VERBOSE_DRAFT).status_code == 303
    assert pending(client, VERBOSE)

    def undo(fields: Fields) -> None:
        fields["steps.0.title"] = "Vamos começar por você"

    assert save(client, undo, draft=VERBOSE_DRAFT).status_code == 303

    assert not pending(client, VERBOSE)
    assert stored(deps, VERBOSE) == verbose_definition()  # o rascunho é a versão de novo
    page = text(client.get(VERBOSE_DRAFT).text)
    assert "igual à versão 1, a publicada" in page and "Rascunho pendente" not in page
    assert "Nada para publicar" in text(client.get(f"{VERBOSE_DRAFT}/publish").text)


def test_a_saved_draft_that_only_drops_the_defaults_is_not_pending(
    client: TestClient, deps: Deps
) -> None:
    """Um rascunho gravado antes, igual à versão a menos dos padrões por extenso, não é pendente
    nem na lista, nem na página, nem na publicação."""
    assert import_form(client, json.dumps(verbose_definition())).status_code == 303
    terse = verbose_definition()
    for step in terse["steps"]:
        for question in step["questions"]:
            if question.get("required") is True:
                del question["required"]
            if question.get("contact") is False:
                del question["contact"]
    del terse["consent"]["marketing"]["whatsapp"]
    with deps.sessions.begin() as session:
        form_id = session.scalar(sa.select(Form.id).where(Form.slug == VERBOSE))
        session.add(
            FormDraft(
                form_id=form_id,
                definition=terse,
                fingerprint=fingerprint(terse),
                base_version=1,
                updated_by="operadora",
                updated_at=datetime.now(UTC),
            )
        )

    assert not pending(client, VERBOSE)
    assert "Rascunho pendente" not in text(client.get(VERBOSE_DRAFT).text)
    assert "Nada para publicar" in text(client.get(f"{VERBOSE_DRAFT}/publish").text)


def test_a_real_change_next_to_the_defaults_still_makes_the_draft_pending(
    client: TestClient,
) -> None:
    assert import_form(client, json.dumps(verbose_definition())).status_code == 303

    def require(fields: Fields) -> None:
        fields["steps.0.questions.2.required"] = "on"  # o nome do escritório era opcional

    assert save(client, require, draft=VERBOSE_DRAFT).status_code == 303

    assert pending(client, VERBOSE)


def test_the_llm_fingerprint_of_the_definitions_in_use_does_not_change() -> None:
    """As fixtures públicas têm impressão digital fixa; defaults explícitos são neutros."""
    fixtures = Path(__file__).parent / "fixtures" / "forms"
    found = {
        slug: llm_fingerprint(json.loads((fixtures / f"{slug}.json").read_text()))
        for slug in ("demo", "mcc", "reforma-2027")
    }
    assert found == {
        "demo": "476125b045bcb057ab7473e48a0eddc1dbbd635d614581c4b3727fceb9f22149",
        "mcc": "9f0759caf313f0d51b3f3a57ee6fbb9a3b5d54f0d0a06845073272c33991b193",
        "reforma-2027": "9a2100ae4d8e235b292d464a2ccad251f21622846190ed935a93e6f0683eac48",
    }
    assert llm_fingerprint(verbose_definition()) == llm_fingerprint(json.loads(DEFINITION))


def test_comparing_a_stored_version_the_format_no_longer_reads_still_works() -> None:
    """Uma versão guardada que não compila mais (o formato mudou) ainda se compara, como está."""
    old = {"steps": "lixo", "consent": [], "pdf": 3, "examples": {"answers": "x"}}
    new = {"steps": [{"title": "A", "questions": []}], "consent": None}

    sections = compare(old, new)

    assert {section.key for section in sections if section.changed} >= {"etapas", "consentimento"}


def test_an_email_question_marked_as_contact_data_keeps_the_mark(
    client: TestClient, deps: Deps
) -> None:
    """E-mail e telefone ficam fora do rascunho no aparelho de todo jeito, e o editor nem mostra a
    caixa nesses tipos; a marcação que a definição traz continua lá depois de salvar."""
    marked = json.loads(DEFINITION)
    marked["slug"] = "demo-contato"
    marked["consent"]["version"] = "demo-contato-v1"
    marked["steps"][0]["questions"][1]["contact"] = True  # o e-mail
    assert import_form(client, json.dumps(marked)).status_code == 303

    response = save(client, relabel_first_step, draft="/admin/forms/demo-contato/draft")

    assert response.status_code == 303
    assert stored(deps, "demo-contato")["steps"][0]["questions"][1]["contact"] is True


def relabel_first_step(fields: Fields) -> None:
    fields["steps.0.title"] = "Comece por você"


def test_changing_a_label_changes_only_the_draft(client: TestClient, deps: Deps) -> None:
    def relabel(fields: Fields) -> None:
        fields["steps.1.questions.0.label"] = "Quantas pessoas trabalham com você?"

    response = save(client, relabel)

    assert response.status_code == 303
    assert editor(client)["steps.1.questions.0.label"] == "Quantas pessoas trabalham com você?"
    assert pending(client)
    question = stored(deps)["steps"][1]["questions"][0]
    assert question["label"] == "Quantas pessoas trabalham com você?"
    assert question["name"] == "tamanho_equipe"  # o nome e as opções seguem os mesmos
    assert "Quantas pessoas trabalham no escritório" in client.get(f"/p/{SLUG}").text
    assert "não precisa de teste" in text(client.get(f"{DRAFT}/publish").text)


def form_of(response: httpx.Response) -> Fields:
    assert response.status_code == 200, response.text
    return form_fields(response.text, EDITOR_FORM)


# --- incluir, remover e reordenar ---------------------------------------------------------------

EDIT = f"{DRAFT}/edit"
HTMX = {"HX-Request": "true"}


def operate(client: TestClient, fields: Fields, op: str, *, htmx: bool = False) -> httpx.Response:
    """Um botão do editor, como o navegador o manda: o formulário inteiro com a operação, sem
    JavaScript (a página volta inteira) ou pelo HTMX (volta só o trecho das etapas)."""
    headers = {**ORIGIN, **(HTMX if htmx else {})}
    data = {**fields, "csrf_token": csrf_token(client), "op": op}
    return client.post(EDIT, data=data, headers=headers)


def autofocused(response: httpx.Response) -> list[str]:
    found = [*tags(response.text, "input"), *tags(response.text, "button")]
    return [str(item.get("name") or item.get("id")) for item in found if "autofocus" in item]


def test_adding_a_question_without_javascript_brings_the_page_back_with_it_and_saves_nothing(
    client: TestClient, deps: Deps
) -> None:
    fields = editor(client)
    fields["prompt"] += "Uma linha que a operadora ainda não salvou.\n"

    response = operate(client, fields, "add-question:1")

    after = form_of(response)
    assert "<html" in response.text  # a página inteira
    assert after["steps.1.questions.2.label"] == "" and after["steps.1.questions.2.kind"] == "text"
    assert after["steps.1.questions.2.max_length"] == ""  # em branco, o máximo do tipo
    assert after["steps.1.questions.2.required"] == "on"
    assert autofocused(response) == ["steps.1.questions.2.label"]
    assert after["prompt"] == fields["prompt"]  # o que ela escreveu volta como estava
    assert after["expected"] == fields["expected"]
    assert "ainda não foram salvas" in text(response.text)
    assert not pending(client) and stored(deps) == json.loads(DEFINITION)  # nada foi gravado


def test_with_htmx_only_the_steps_come_back(client: TestClient) -> None:
    response = operate(client, editor(client), "add-step", htmx=True)

    assert response.status_code == 200 and "<html" not in response.text
    assert tags(response.text, "div", id="estrutura")
    assert tags(response.text, "input", name="steps.3.title")
    assert tags(response.text, "input", name="steps.3.questions.0.label")
    assert autofocused(response) == ["steps.3.title"]
    assert not tags(response.text, "textarea", name="prompt")  # o resto da página fica


def test_a_new_choice_question_with_its_options_and_example_is_saved(
    client: TestClient, deps: Deps
) -> None:
    fields = form_of(operate(client, editor(client), "add-question:2"))
    fields["steps.2.questions.1.label"] = "Quantos clientes você atende?"
    fields["steps.2.questions.1.kind"] = "choice"
    fields = form_of(operate(client, fields, "add-option:2:1"))
    fields = form_of(operate(client, fields, "add-option:2:1"))
    fields["steps.2.questions.1.options.0.label"] = "Até 30 clientes"
    fields["steps.2.questions.1.options.1.label"] = "Mais de 30 clientes"
    fields["steps.2.questions.1.example_option"] = "1"

    response = post(client, DRAFT, **fields)

    assert response.status_code == 303, text(response.text)
    document = stored(deps)
    assert document["steps"][2]["questions"][1] == {
        "name": "quantos_clientes_voce_atende",  # sem nome, ele sai do rótulo
        "kind": "choice",
        "label": "Quantos clientes você atende?",
        "options": [
            {"value": "ate_30_clientes", "label": "Até 30 clientes"},
            {"value": "mais_de_30_clientes", "label": "Mais de 30 clientes"},
        ],
    }
    assert document["examples"]["answers"]["quantos_clientes_voce_atende"] == "mais_de_30_clientes"
    page = client.get(DRAFT)
    assert form_of(page)["steps.2.questions.1.options.1.value"] == "mais_de_30_clientes"


def test_removing_an_option_keeps_the_question_open_with_the_focus_on_adding_one(
    client: TestClient,
) -> None:
    """O foco volta ao botão de adicionar opção da mesma pergunta, e a pergunta continua aberta,
    com ou sem JavaScript, em vez de fechar no meio da edição."""
    fields = editor(client)

    for htmx in (True, False):
        response = operate(client, fields, "remove-option:1:0:1", htmx=htmx)

        (box,) = tags(response.text, "details", id="f-steps-1-questions-0")
        assert "open" in box
        (add,) = tags(response.text, "button", id="op-add-option-1-0")
        assert "autofocus" in add
        (other,) = tags(response.text, "details", id="f-steps-0-questions-0")
        assert "open" not in other  # as outras perguntas seguem fechadas


def test_steps_questions_and_options_move_and_the_identity_follows_the_question(
    client: TestClient, deps: Deps
) -> None:
    fields = form_of(operate(client, editor(client), "down-step:0"))
    assert (fields["steps.0.title"], fields["steps.1.title"]) == (
        "Seu escritório hoje",
        "Vamos começar por você",
    )
    assert (fields["identity.email"], fields["identity.name"]) == ("1.1", "1.0")

    fields = form_of(operate(client, fields, "up-question:1:0"))  # passa para o fim da etapa 1
    assert fields["steps.0.questions.2.label"] == "Nome completo"
    assert fields["identity.name"] == "0.2"
    fields = form_of(operate(client, fields, "down-option:0:0:0"))
    assert fields["steps.0.questions.0.options.0.value"] == "2_a_5"
    assert fields["steps.0.questions.0.options.1.value"] == "so_eu"
    assert fields["steps.0.questions.0.example_option"] == "0"  # o exemplo segue a opção
    fields = form_of(operate(client, fields, "remove-option:0:0:3"))
    fields = form_of(operate(client, fields, "remove-question:0:1"))
    # O prompt cita a pergunta que saiu: a mudança dele vai no mesmo salvar.
    fields["prompt"] = fields["prompt"].replace(
        "- Principal desafio: {{ answers.principal_desafio }}\n", ""
    )
    response = post(client, DRAFT, **fields)

    assert response.status_code == 303, text(response.text)
    document = stored(deps)
    assert [step["title"] for step in document["steps"]] == [
        "Seu escritório hoje",
        "Vamos começar por você",
        "Seus próximos 90 dias",
    ]
    first = document["steps"][0]["questions"]
    assert [question["name"] for question in first] == ["tamanho_equipe", "nome"]
    assert [option["value"] for option in first[0]["options"]] == ["2_a_5", "so_eu", "6_a_15"]
    assert document["identity"] == {"email": "email", "name": "nome"}
    assert "principal_desafio" not in document["examples"]["answers"]  # saiu com a pergunta


def test_removing_a_question_the_prompt_cites_needs_the_prompt_in_the_same_save(
    client: TestClient,
) -> None:
    fields = form_of(operate(client, editor(client), "remove-question:2:0"))
    fields = form_of(operate(client, fields, "remove-step:2"))

    refused = post(client, DRAFT, **fields)

    assert refused.status_code == 422
    assert "answers.objetivo_90_dias não é uma pergunta do formulário" in text(refused.text)
    fields = form_of_refused(refused)
    assert "steps.2.title" not in fields  # a etapa removida segue fora, como ela deixou
    fields["prompt"] = fields["prompt"].replace(
        "- Objetivo para os próximos 90 dias: {{ answers.objetivo_90_dias }}\n", ""
    )
    assert post(client, DRAFT, **fields).status_code == 303


def form_of_refused(response: httpx.Response) -> Fields:
    assert response.status_code == 422, response.text
    return form_fields(response.text, EDITOR_FORM)


def test_an_operation_from_a_page_that_points_to_nothing_changes_nothing(
    client: TestClient,
) -> None:
    fields = editor(client)

    response = operate(client, fields, "remove-question:9:9")

    assert form_of(response)["steps.2.questions.0.label"] == fields["steps.2.questions.0.label"]
    assert autofocused(response) == []


# --- o problema no campo dele -------------------------------------------------------------------


def error_of(response: httpx.Response, name: str) -> str:
    """O problema que a página mostra embaixo do campo (ou da caixa) com este nome."""
    found = re.search(
        rf'<p class="error" id="f-{name.replace(".", "-")}-erro">(.*?)</p>',
        response.text,
        re.DOTALL,
    )
    assert found is not None, f"a página não mostrou problema em {name}"
    return text(found.group(1))


def no_options(fields: Fields) -> None:
    for key in [key for key in fields if key.startswith("steps.1.questions.0.options.")]:
        del fields[key]


def no_questions_in_the_last_step(fields: Fields) -> None:
    for key in [key for key in fields if key.startswith("steps.2.questions.")]:
        del fields[key]


@pytest.mark.parametrize(
    ("change", "field", "problem"),
    [
        ({"steps.1.questions.0.label": ""}, "steps.1.questions.0.label", "não pode ficar vazio"),
        (
            {"steps.1.questions.0.options.1.value": "so_eu"},
            "steps.1.questions.0.options.1.value",
            "outra opção da pergunta já usa este valor",
        ),
        (no_options, "steps.1.questions.0.options", "precisa de pelo menos 1 item"),
        (no_questions_in_the_last_step, "steps.2", "precisa de pelo menos 1 item"),
        (
            {"steps.0.questions.0.min_length": "200"},
            "steps.0.questions.0.min_length",
            "o mínimo passa do máximo (120)",
        ),
        (
            {"steps.0.questions.0.max_length": "muitos"},
            "steps.0.questions.0.max_length",
            "precisa ser um número inteiro",
        ),
        ({"minutes": "90"}, "minutes", "precisa ser no máximo 60"),
        ({"steps.2.questions.0.example": ""}, "steps.2.questions.0.example", "campo obrigatório"),
        ({"identity.email": ""}, "identity.email", "campo obrigatório"),
        (
            {"identity.name": "0.1"},
            "identity.name",
            "precisa ser uma pergunta de texto",
        ),
        (
            {"consent.marketing.whatsapp.offered": "on"},
            "consent.marketing.whatsapp",
            "o canal WhatsApp precisa do telefone da lead",
        ),
    ],
    ids=[
        "label",
        "repeated-value",
        "no-options",
        "empty-step",
        "min-over-max",
        "not-a-number",
        "minutes",
        "example",
        "no-email",
        "name-from-an-email-question",
        "whatsapp-without-phone",
    ],
)
def test_a_problem_shows_next_to_its_field_and_nothing_is_saved(
    client: TestClient, change: Any, field: str, problem: str
) -> None:
    fields = editor(client)
    if callable(change):
        change(fields)
    else:
        fields.update(change)

    response = post(client, DRAFT, **fields)

    assert response.status_code == 422
    assert problem in error_of(response, field)
    assert problem in text(section_of(response, "problemas"))
    if field.count(".") > 2:  # um campo de uma pergunta: ela vem aberta, com o campo marcado
        controls = [*tags(response.text, "input"), *tags(response.text, "select")]
        for control in (c for c in controls if c.get("name") == field):
            assert control.get("aria-invalid") == "true"
        question = ".".join(field.split(".")[:4]).replace(".", "-")
        (box,) = tags(response.text, "details", id=f"f-{question}")
        assert "open" in box
    assert not pending(client)


# --- a identidade e o consentimento -------------------------------------------------------------


def with_whatsapp(client: TestClient, fields: Fields) -> Fields:
    """Uma pergunta de telefone na primeira etapa, como o telefone da lead, e as novidades por
    WhatsApp oferecidas."""
    fields = form_of(operate(client, fields, "add-question:0"))
    fields["steps.0.questions.3.label"] = "Seu WhatsApp"
    fields["steps.0.questions.3.kind"] = "tel"
    fields["steps.0.questions.3.name"] = "whatsapp"
    fields.pop("steps.0.questions.3.required")
    fields = form_of(operate(client, fields, "refresh"))
    fields["identity.phone"] = "0.3"
    fields["consent.marketing.whatsapp.offered"] = "on"
    fields["consent.marketing.whatsapp"] = "Quero receber novidades pelo WhatsApp."
    return fields


def test_the_lead_phone_and_the_whatsapp_channel_are_set_in_the_editor(
    client: TestClient, deps: Deps
) -> None:
    response = post(client, DRAFT, **with_whatsapp(client, editor(client)))

    assert response.status_code == 303, text(response.text)
    document = stored(deps)
    assert document["identity"] == {"email": "email", "name": "nome", "phone": "whatsapp"}
    question = document["steps"][0]["questions"][3]
    assert question == {
        "name": "whatsapp",
        "kind": "tel",
        "label": "Seu WhatsApp",
        "required": False,
    }
    marketing = document["consent"]["marketing"]
    assert marketing["whatsapp"] == "Quero receber novidades pelo WhatsApp."
    assert document["consent"]["version"] == "demo-definicao-v1"  # sobe na publicação
    fields = editor(client)
    assert fields["identity.phone"] == "0.3"
    assert fields["consent.marketing.whatsapp.offered"] == "on"


def test_the_consent_texts_change_in_the_editor_and_their_version_rises_on_publishing(
    client: TestClient,
) -> None:
    def new_processing(fields: Fields) -> None:
        fields["consent.processing"] = "Autorizo o uso das minhas respostas para gerar o plano."
        fields.pop("consent.marketing.email.offered")  # deixa de oferecer o e-mail

    assert save(client, new_processing).status_code == 303
    page = client.get(f"{DRAFT}/publish")
    assert "a versão dele sobe para demo-definicao-v2" in text(page.text)
    expected = form_fields(page.text, "publicar-form")["expected"]
    published = post(client, f"{DRAFT}/publish", note="Consentimento novo.", expected=expected)

    assert published.status_code == 303
    consent = text(section_of(client.get(f"/admin/forms/{SLUG}"), "consentimento"))
    assert "Autorizo o uso das minhas respostas" in consent and "demo-definicao-v2" in consent
    assert "Novidades por e-mail não oferecido" in consent
    public = client.get(f"/p/{SLUG}").text
    assert 'value="demo-definicao-v2"' in public and "_mkt_email" not in public


# --- os seis tipos ------------------------------------------------------------------------------


def test_every_kind_of_question_is_edited_with_its_limits(client: TestClient, deps: Deps) -> None:
    fields = editor(client)
    for _ in range(3):
        fields = form_of(operate(client, fields, "add-question:2"))
    long_text, email, many = "steps.2.questions.1", "steps.2.questions.2", "steps.2.questions.3"
    fields |= {
        f"{long_text}.label": "Conte mais sobre a sua equipe",
        f"{long_text}.kind": "textarea",
        f"{email}.label": "Um e-mail do financeiro",
        f"{email}.kind": "email",
        f"{email}.name": "email_financeiro",
        f"{many}.label": "Quais serviços o escritório oferece?",
        f"{many}.kind": "choices",
    }
    fields.pop(f"{long_text}.required")
    fields.pop(f"{email}.required")
    for _ in range(3):
        fields = form_of(operate(client, fields, "add-option:2:3"))
    fields |= {
        f"{long_text}.min_length": "20",
        f"{long_text}.max_length": "800",
        f"{long_text}.placeholder": "Quem faz o quê",
        f"{long_text}.example": "Somos três pessoas, cada uma cuida de uma parte da carteira.",
        f"{email}.max_length": "120",
        f"{many}.options.0.label": "Contabilidade",
        f"{many}.options.1.label": "Departamento pessoal",
        f"{many}.options.2.label": "Fiscal",
        f"{many}.min_choices": "1",
        f"{many}.max_choices": "2",
        f"{many}.example_option": ["0", "2"],
    }

    response = post(client, DRAFT, **fields)

    assert response.status_code == 303, text(response.text)
    added = stored(deps)["steps"][2]["questions"][1:]
    assert added == [
        {
            "name": "conte_mais_sobre_a_sua_equipe",
            "kind": "textarea",
            "label": "Conte mais sobre a sua equipe",
            "placeholder": "Quem faz o quê",
            "required": False,
            "min_length": 20,
            "max_length": 800,
        },
        {
            "name": "email_financeiro",
            "kind": "email",
            "label": "Um e-mail do financeiro",
            "required": False,
            "max_length": 120,
        },
        {
            "name": "quais_servicos_o_escritorio_oferece",
            "kind": "choices",
            "label": "Quais serviços o escritório oferece?",
            "min_choices": 1,
            "max_choices": 2,
            "options": [
                {"value": "contabilidade", "label": "Contabilidade"},
                {"value": "departamento_pessoal", "label": "Departamento pessoal"},
                {"value": "fiscal", "label": "Fiscal"},
            ],
        },
    ]
    answers = stored(deps)["examples"]["answers"]
    assert answers["quais_servicos_o_escritorio_oferece"] == ["contabilidade", "fiscal"]
    assert answers["conte_mais_sobre_a_sua_equipe"].startswith("Somos três pessoas")
    assert "email_financeiro" not in answers  # opcional, sem resposta de exemplo


def test_changing_the_kind_brings_the_question_back_open_with_the_focus_on_the_kind(
    client: TestClient,
) -> None:
    """O tipo novo traz outros campos: a pergunta volta aberta, com o foco no tipo, com ou sem
    JavaScript, em vez de fechar no meio da edição."""
    page = client.get(DRAFT).text
    (kind,) = tags(page, "select", name="steps.1.questions.0.kind")
    assert kind["hx-vals"] == '{"op": "refresh-question:1:0"}'  # o que o HTMX manda na troca
    assert tags(page, "button", value="refresh-question:1:0")  # o botão sem JavaScript
    fields = editor(client)
    fields["steps.1.questions.0.kind"] = "choices"

    for htmx in (True, False):
        response = operate(client, fields, "refresh-question:1:0", htmx=htmx)

        (box,) = tags(response.text, "details", id="f-steps-1-questions-0")
        assert "open" in box
        assert tags(response.text, "input", name="steps.1.questions.0.max_choices")
        (kind,) = tags(response.text, "select", name="steps.1.questions.0.kind")
        assert "autofocus" in kind


def test_changing_the_kind_between_choice_and_text_starts_the_example_over(
    client: TestClient,
) -> None:
    fields = editor(client)
    fields["steps.1.questions.0.kind"] = "text"  # era escolha única, com a opção 2 no exemplo

    as_text = form_of(operate(client, fields, "refresh-question:1:0"))

    assert as_text["steps.1.questions.0.example"] == ""  # e não o número da opção marcada
    as_text["steps.1.questions.0.example"] = "Duas pessoas"
    as_text["steps.1.questions.0.kind"] = "choice"
    back = form_of(operate(client, as_text, "refresh-question:1:0"))
    assert "steps.1.questions.0.example_option" not in back  # nenhuma opção marcada


# --- o aviso dos valores publicados -------------------------------------------------------------


def test_changing_a_published_option_value_warns_that_the_webhook_receiver_may_depend_on_it(
    client: TestClient,
) -> None:
    assert not tags(client.get(DRAFT).text, "div", id="valores-removidos")
    fields = editor(client)
    fields["steps.1.questions.0.options.1.value"] = "de_2_a_5"

    assert post(client, DRAFT, **fields).status_code == 303

    warning = text(section_of(client.get(DRAFT), "valores-removidos"))
    assert "tamanho_equipe" in warning and "2_a_5" in warning and "receptor do webhook" in warning
    publish_page = text(section_of(client.get(f"{DRAFT}/publish"), "valores-removidos"))
    assert "2_a_5" in publish_page and "receptor do webhook" in publish_page


# --- um formulário grande -----------------------------------------------------------------------


def large_definition(questions: int = 100) -> str:
    """O demo com mais uma etapa de perguntas de escolha, de quatro opções cada: com 100, o editor
    passa de mil campos, o teto padrão do Starlette."""
    document = json.loads(DEFINITION)
    document["slug"] = "demo-grande"
    document["consent"]["version"] = "demo-grande-v1"
    options = [{"value": f"opcao_{k}", "label": f"Opção {k}"} for k in range(4)]
    names = [f"pergunta_{n}" for n in range(questions)]
    document["steps"].append(
        {
            "title": "Muitas perguntas",
            "questions": [
                {"name": name, "kind": "choice", "label": name, "options": options}
                for name in names
            ],
        }
    )
    document["examples"]["answers"] |= dict.fromkeys(names, "opcao_0")
    return json.dumps(document, ensure_ascii=False)


def test_a_form_with_more_than_a_thousand_fields_saves(client: TestClient) -> None:
    assert import_form(client, large_definition()).status_code == 303
    draft = "/admin/forms/demo-grande/draft"
    fields = editor(client, draft)
    sent = sum(len(value) if isinstance(value, list) else 1 for value in fields.values())
    assert sent > 1000

    assert post(client, draft, **fields).status_code == 303
    data = {**fields, "csrf_token": csrf_token(client), "op": "add-step"}
    assert client.post(f"{draft}/edit", data=data, headers=ORIGIN).status_code == 200


# --- acesso -------------------------------------------------------------------------------------


def test_the_editor_operations_need_the_session_and_the_csrf_token(client: TestClient) -> None:
    fields = editor(client)
    anonymous = TestClient(client.app, follow_redirects=False)

    refused = [
        anonymous.post(EDIT, data={**fields, "op": "add-step"}, headers=ORIGIN),
        client.post(EDIT, data={**fields, "op": "add-step", "csrf_token": "x"}, headers=ORIGIN),
        client.post(EDIT, data={**fields, "op": "add-step", "csrf_token": csrf_token(client)}),
    ]

    assert [response.status_code for response in refused] == [303, 403, 403]
    assert refused[0].headers["location"].startswith("/admin/login")
    with_header = {**ORIGIN, **HTMX, "X-CSRF-Token": csrf_token(client)}
    allowed = client.post(EDIT, data={**fields, "op": "add-step"}, headers=with_header)
    assert allowed.status_code == 200  # o HTMX manda o token no cabeçalho


def test_consent_method_and_layout_roundtrip_in_editor_without_changing_text_version(
    client: TestClient, deps: Deps
) -> None:
    fields = editor(client)
    assert fields["consent.acquisition_method"] == "checkbox"
    fields["consent.acquisition_method"] = "submit"
    fields["consent.presentation"] = "compact"
    fields["consent.position"] = "before_submit"
    assert save(client, lambda current: current.update(fields)).status_code == 303
    consent = stored(deps)["consent"]
    assert consent["acquisition_method"] == "submit"
    assert consent["presentation"] == "compact"
    assert consent["position"] == "before_submit"
    assert editor(client)["consent.acquisition_method"] == "submit"
    assert consent["version"] == json.loads(DEFINITION)["consent"]["version"]
