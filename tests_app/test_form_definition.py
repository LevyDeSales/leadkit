"""Compilador da definição de formulário: um documento JSON vira o mesmo contrato (`FormSpec`) que
o motor consome. Os testes passam pela costura do compilador e pelos adaptadores que consomem o
contrato: respostas validadas, identidade, prompt montado, schema estrito da OpenAI e PDF do
WeasyPrint."""

from __future__ import annotations

import copy
import json
import logging
import socket
import traceback
import uuid
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import DEMO_ANSWERS, publish
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pypdf import PdfReader
from test_openai_llm import (
    NOT_FOR_STRICT_MODE,
    FakeOpenAI,
    _without_openai_env,  # noqa: F401 - fixture autouse: OPENAI_* fora
    generator,
    reply,
    schema_nodes,
)
from test_public import complete, tags
from test_public import text as page_text
from test_weasyprint_pdf import fonts_of, squash, text_of

from app.adapters.openai_llm import strict_schema
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.api.main import create_app
from app.deps import Deps
from app.errors import FailureCode, TerminalError
from app.forms import FormSpec, LeadIdentity
from app.forms.definition import DefinitionError, compile_definition
from app.forms.sandbox import TemplateProblem
from app.models import Artifact, Plan

FIXTURES = Path(__file__).parent / "fixtures"
# Definição equivalente ao demo em código, sem a pontuação. O ticket 05 importa este arquivo.
DEFINITION: dict[str, Any] = json.loads(
    (FIXTURES / "demo_definition.json").read_text(encoding="utf-8")
)
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
# 02:30 UTC de 8 de outubro ainda é 7 de outubro em São Paulo.
LATE_NIGHT = datetime(2026, 10, 8, 2, 30, tzinfo=UTC)


def compiled(
    document: dict[str, Any] | None = None, *, now: datetime = NOW, **options: Any
) -> FormSpec:
    return compile_definition(
        DEFINITION if document is None else document, clock=lambda: now, **options
    )


def changed(changes: dict[str, Any] | None = None, *, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    """Uma cópia da definição do demo com mudanças por caminho (`steps.1.questions.0.kind`)."""
    document = copy.deepcopy(DEFINITION)

    def locate(path: str) -> tuple[Any, str | int]:
        *parents, last = path.split(".")
        target: Any = document
        for key in parents:
            target = target[int(key)] if isinstance(target, list) else target[key]
        return target, int(last) if isinstance(target, list) else last

    for path, value in (changes or {}).items():
        parent, key = locate(path)
        parent[key] = copy.deepcopy(value)
    for path in drop:
        parent, key = locate(path)
        del parent[key]
    return document


def answers_of(spec: FormSpec, answers: dict[str, Any]) -> Any:
    return spec.answers_model.model_validate(answers)


# --- equivalência com o demo em código ----------------------------------------------------------


# O schema estrito do plano do demo, escrito à mão, com os limites nas descrições.
DEMO_STRICT_SCHEMA = {
    "properties": {
        "titulo": {"description": "Entre 3 e 120 caracteres.", "type": "string"},
        "resumo": {"description": "Entre 20 e 1200 caracteres.", "type": "string"},
        "acoes": {
            "description": "Entre 3 e 5 itens.",
            "items": {"type": "string"},
            "type": "array",
        },
        "proximo_passo": {"description": "Entre 10 e 400 caracteres.", "type": "string"},
    },
    "type": "object",
    "additionalProperties": False,
    "required": ["titulo", "resumo", "acoes", "proximo_passo"],
}


def test_openai_adapter_sends_the_instructions_prompt_and_schema_of_the_definition() -> None:
    spec = compiled()
    server = FakeOpenAI(reply(spec.example_plan))

    result = generator(server).generate(spec, answers_of(spec, DEMO_ANSWERS))

    (body,) = server.bodies
    assert body["instructions"] == spec.instructions
    assert body["input"] == [
        {"role": "user", "content": spec.render_prompt(answers_of(spec, DEMO_ANSWERS))}
    ]
    assert body["text"]["format"] == {
        "type": "json_schema",
        "name": "plano_demo-definicao",
        "strict": True,
        "schema": DEMO_STRICT_SCHEMA,
    }
    assert DEMO_ANSWERS["email"].strip().lower() not in json.dumps(body, ensure_ascii=False)
    assert (result.document, result.usage["repairs"]) == (spec.example_plan, 0)


def test_versions_of_a_compiled_form_follow_its_version_number() -> None:
    spec = compiled(version_number=3)

    assert (spec.version_number, spec.version, spec.prompt_version) == (
        3,
        "demo-definicao-v3",
        "demo-definicao-v3",
    )
    assert spec.consent_text_version == "demo-definicao-v1"  # o texto do consentimento tem a sua


def test_demo_definition_writes_each_limit_in_the_description_of_its_field() -> None:
    properties = strict_schema(compiled().plan_model)["properties"]

    assert {name: node.get("description") for name, node in properties.items()} == {
        "titulo": "Entre 3 e 120 caracteres.",
        "resumo": "Entre 20 e 1200 caracteres.",
        "acoes": "Entre 3 e 5 itens.",
        "proximo_passo": "Entre 10 e 400 caracteres.",
    }


# --- modelo do plano em árvore ------------------------------------------------------------------

PHASED_PLAN: dict[str, Any] = {
    "description": "Plano de 90 dias em fases.",
    "fields": [
        {
            "name": "titulo",
            "kind": "text",
            "description": "Título próprio do escritório",
            "max_length": 90,
        },
        {
            "name": "uso_da_janela",
            "kind": "text",
            "internal": True,
            "description": "Planejamento interno, escrito antes das fases.",
            "min_length": 10,
            "max_length": 1000,
        },
        {
            "name": "fases",
            "kind": "list",
            "description": "Fases na ordem de execução.",
            "min_items": 3,
            "max_items": 3,
            "items": {
                "kind": "group",
                "model": "Fase",
                "description": "Um bloco de 30 dias.",
                "fields": [
                    {"name": "nome", "kind": "text", "min_length": 3, "max_length": 60},
                    {
                        "name": "acoes",
                        "kind": "list",
                        "min_items": 1,
                        "max_items": 2,
                        "items": {
                            "kind": "group",
                            "model": "Acao",
                            "fields": [
                                {
                                    "name": "tipo",
                                    "kind": "options",
                                    "internal": True,
                                    "description": "O que a ação faz com a opção.",
                                    "values": ["decisao", "outra"],
                                },
                                {"name": "titulo", "kind": "text", "min_length": 5},
                            ],
                        },
                    },
                ],
            },
        },
        {
            "name": "pontos",
            "kind": "list",
            "max_items": 4,
            "items": {"kind": "text", "min_length": 20, "max_length": 260},
        },
        {
            "name": "leitura",
            "kind": "group",
            "description": "Leitura do momento.",
            "fields": [{"name": "texto", "kind": "text", "min_length": 1, "max_length": 1}],
        },
    ],
}
SECRET_PLANNING = "Planejamento interno que a lead nunca vê."
PHASED_EXAMPLE: dict[str, Any] = {
    "titulo": "Carteira mapeada e clientes orientados",
    "uso_da_janela": SECRET_PLANNING,
    "fases": [
        {
            "nome": f"Tema da fase {name}",
            "acoes": [{"tipo": "outra", "titulo": f"Ação visível {name}"}],
        }
        for name in ("um", "dois", "três")
    ],
    "pontos": ["Revise as conclusões antes de cada decisão importante."],
    "leitura": {"texto": "A"},
}
PHASED_PDF = (
    "<h1>{{ plan.titulo }}</h1>\n"
    "{% for fase in plan.fases %}<h2>{{ fase.nome }}</h2>"
    "{% for acao in fase.acoes %}<p>{{ acao.titulo }}</p>{% endfor %}{% endfor %}\n"
    "<p>{{ plan }}</p>\n"
)
PHASED_PREVIEW = "<h2>{{ plan.titulo }}</h2><p>{{ plan.fases[0] }}</p>"


def phased(changes: dict[str, Any] | None = None) -> dict[str, Any]:
    """A definição do demo com o plano em fases, com campos internos e os templates dele."""
    return changed(
        {
            "plan": PHASED_PLAN,
            "examples.plan": PHASED_EXAMPLE,
            "pdf.html": PHASED_PDF,
            "preview": PHASED_PREVIEW,
            **(changes or {}),
        }
    )


def test_plan_tree_becomes_a_strict_schema_in_tree_order_with_limits_in_the_descriptions() -> None:
    schema = strict_schema(compiled(phased()).plan_model)

    defs = schema["$defs"]
    assert schema["description"] == "Plano de 90 dias em fases."
    assert list(schema["properties"]) == ["titulo", "uso_da_janela", "fases", "pontos", "leitura"]
    assert schema["required"] == list(schema["properties"])
    assert schema["properties"]["titulo"] == {
        "description": "Título próprio do escritório. Até 90 caracteres.",
        "type": "string",
    }
    assert schema["properties"]["fases"] == {
        "description": "Fases na ordem de execução. Exatamente 3 itens.",
        "items": {"$ref": "#/$defs/Fase"},
        "type": "array",
    }
    assert defs["Fase"]["description"] == "Um bloco de 30 dias."
    assert list(defs["Fase"]["properties"]) == ["nome", "acoes"]
    assert defs["Fase"]["properties"]["acoes"]["description"] == "Entre 1 e 2 itens."
    assert defs["Acao"]["properties"]["tipo"] == {
        "description": "O que a ação faz com a opção.",
        "enum": ["decisao", "outra"],
        "type": "string",
    }
    assert defs["Acao"]["properties"]["titulo"]["description"] == "Pelo menos 5 caracteres."
    assert schema["properties"]["pontos"] == {
        "description": "Até 4 itens.",
        "items": {"description": "Entre 20 e 260 caracteres.", "type": "string"},
        "type": "array",
    }
    assert schema["properties"]["leitura"] == {"$ref": "#/$defs/Leitura"}
    assert defs["Leitura"]["description"] == "Leitura do momento."
    assert defs["Leitura"]["properties"]["texto"]["description"] == "Exatamente 1 caractere."


def test_internal_fields_go_to_the_llm_and_stay_in_the_stored_plan() -> None:
    spec = compiled(phased())

    plan = spec.validate_plan(json.dumps(PHASED_EXAMPLE), answers_of(spec, DEMO_ANSWERS))

    assert plan.model_dump(mode="json") == PHASED_EXAMPLE  # vai assim para o banco e o webhook
    assert "uso_da_janela" in strict_schema(spec.plan_model)["properties"]


def test_plan_tree_schema_is_accepted_in_strict_mode() -> None:
    nodes = list(schema_nodes(strict_schema(compiled(phased()).plan_model)))

    for node in nodes:
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])
    assert not [node for node in nodes if NOT_FOR_STRICT_MODE & node.keys()]


# --- contexto do prompt e das instruções --------------------------------------------------------

EDITION = {"nome": "Ciclo demonstrativo 2027", "fim": "31/05/2027"}


def block(value: Any) -> dict[str, Any]:
    """Um bloco livre dos dados do formulário."""
    return {"kind": "value", "value": value}


def test_prompt_gets_option_labels_today_in_sao_paulo_and_the_form_data() -> None:
    prompt = (
        "Equipe: {{ labels.tamanho_equipe }} ({{ answers.tamanho_equipe }})\n"
        "Hoje: {{ today|long_date }} ({{ today|short_date }}), início em {{ today|month_year }}\n"
        "Edição: {{ data.edicao.nome }}, até {{ data.edicao.fim }}\n"
    )
    spec = compiled(changed({"prompt": prompt, "data": {"edicao": block(EDITION)}}), now=LATE_NIGHT)

    rendered = spec.render_prompt(answers_of(spec, DEMO_ANSWERS))

    assert rendered == (
        "Equipe: De 2 a 5 pessoas (2_a_5)\n"
        "Hoje: 7 de outubro de 2026 (07/10/2026), início em outubro de 2026\n"
        "Edição: Ciclo demonstrativo 2027, até 31/05/2027\n"
    )


def test_today_follows_the_injected_clock_at_each_rendering() -> None:
    now = [datetime(2026, 12, 31, 12, tzinfo=UTC)]
    spec = compile_definition(changed({"prompt": "{{ today|long_date }}"}), clock=lambda: now[0])
    answers = answers_of(spec, DEMO_ANSWERS)

    before = spec.render_prompt(answers)
    now[0] = datetime(2027, 1, 1, 12, tzinfo=UTC)

    assert (before, spec.render_prompt(answers)) == (
        "31 de dezembro de 2026",
        "1º de janeiro de 2027",
    )


def test_instructions_are_a_template_rendered_with_the_form_data() -> None:
    instructions = "Cite a edição {{ data.edicao.nome }}, que termina em {{ data.edicao.fim }}.\n"

    spec = compiled(changed({"instructions": instructions, "data": {"edicao": block(EDITION)}}))

    assert (
        spec.instructions == "Cite a edição Ciclo demonstrativo 2027, que termina em 31/05/2027.\n"
    )


# --- PDF pelo WeasyPrint ------------------------------------------------------------------------


def pdf_context(spec: FormSpec, name: str | None = "Contadora Exemplo") -> dict[str, Any]:
    """O contexto que o passo `render` do pipeline monta para o adaptador."""
    return {
        "lead": {"name": name},
        "form": {"slug": spec.slug, "title": spec.title},
        "scores": {},
        "generated_at": LATE_NIGHT,
    }


def test_pdf_template_never_receives_the_internal_fields_of_the_plan() -> None:
    spec = compiled(phased())

    rendered = WeasyPrintRenderer().render(spec, PHASED_EXAMPLE, pdf_context(spec))

    text = squash(text_of(rendered.content))  # o template imprime o plano inteiro que recebe
    assert squash("Ação visível três") in text and squash("Tema da fase dois") in text
    assert squash("leitura=namespace(texto='A')") in text
    assert squash(SECRET_PLANNING) not in text
    assert "uso_da_janela" not in text and "tipo" not in text


@pytest.mark.parametrize(
    ("html", "css"),
    [
        ('<p>{{ plan.titulo }}</p><img src="https://evil.example/logo.png">', ""),
        ('<p>{{ plan.titulo }}</p><img src="file:///etc/hosts">', ""),
        ("<p>{{ plan.titulo }}</p>", '@import url("https://evil.example/x.css");'),
        ("<p>{{ plan.titulo }}</p>", 'body { background: url("../forms/demo/__init__.py"); }'),
    ],
)
def test_pdf_of_a_definition_loads_nothing_outside_app_assets(
    monkeypatch: pytest.MonkeyPatch, html: str, css: str
) -> None:
    attempts: list[tuple[object, ...]] = []
    monkeypatch.setattr(socket.socket, "connect", lambda *args: attempts.append(args))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kw: attempts.append(args))
    spec = compiled(changed({"pdf.html": html, "pdf.css": css}))

    with pytest.raises(TerminalError) as caught:
        WeasyPrintRenderer().render(spec, spec.example_plan, pdf_context(spec))

    assert caught.value.code is FailureCode.RENDER_FAILED
    assert attempts == []


PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def test_pdf_of_a_definition_uses_the_brand_kit_of_the_app_and_data_uris() -> None:
    html = (
        "<h1>{{ plan.titulo }}</h1><p>{{ plan.resumo }}</p>"
        f'<img src="data:image/png;base64,{PIXEL}">'
    )
    css = '@import url("brands/leadkit/print.css");'
    spec = compiled(changed({"pdf.html": html, "pdf.css": css}))

    rendered = WeasyPrintRenderer().render(spec, spec.example_plan, pdf_context(spec))

    assert len(PdfReader(BytesIO(rendered.content)).pages[0].images) == 1
    fonts = fonts_of(rendered.content)
    assert {name[:10] for name in fonts} == {"SourceSans", "SourceSeri"}, fonts
    assert all(fonts.values()), fonts  # embutidas


SECRET_LEAD = "Lead Secreta da Silva"


def test_pdf_template_failure_is_terminal_with_the_line_and_without_lead_data() -> None:
    html = (
        "<h1>{{ plan.titulo }}</h1>\n"
        f"{{% if lead.name == '{SECRET_LEAD}' %}}{{{{ lead.name + 1 }}}}{{% endif %}}"
    )
    spec = compiled(changed({"pdf.html": html}))

    with pytest.raises(TerminalError) as caught:
        WeasyPrintRenderer().render(spec, spec.example_plan, pdf_context(spec, SECRET_LEAD))

    assert caught.value.code is FailureCode.RENDER_FAILED
    assert "pdf.html, linha 2" in str(caught.value)
    assert SECRET_LEAD not in "".join(traceback.format_exception(caught.value))


# --- página pública e prévia do resultado -------------------------------------------------------


@pytest.fixture
def client(deps: Deps) -> TestClient:
    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    return TestClient(create_app(trigger=fake_trigger))


def send_form(client: TestClient, spec: FormSpec, answers: dict[str, Any]) -> str:
    """Envia o formulário pela página pública e devolve o endereço do pedido."""
    controls = {"_privacy": "1", "_consent_version": spec.consent_text_version, "_idem": "chave"}
    response = client.post(f"/p/{spec.slug}", data={**answers, **controls})
    assert response.status_code == 200 and response.url.path.startswith("/d/")
    return response.url.path


def test_public_page_serves_a_compiled_form_from_the_registry(
    client: TestClient, deps: Deps
) -> None:
    spec = compiled()
    publish(deps, spec)

    shown = page_text(client.get(f"/p/{spec.slug}").text)

    for label in (
        "Conte como você organiza seu projeto fictício",
        "Nome do escritório (opcional)",
        "Quantas pessoas trabalham no escritório, contando com você?",
        "Mais de 15 pessoas",
        "O que você quer alcançar nos próximos 90 dias?",
    ):
        assert label in shown, label


def test_result_page_previews_the_plan_with_the_template_of_the_definition(
    client: TestClient, deps: Deps
) -> None:
    spec = compiled(phased())
    publish(deps, spec)
    page = send_form(client, spec, DEMO_ANSWERS)
    complete(deps, page)  # o LLM falso devolve o plano de exemplo da definição

    shown = page_text(client.get(page).text)

    assert "Carteira mapeada e clientes orientados" in shown
    assert "namespace(nome='Tema da fase um', acoes=[namespace(titulo='Ação visível um')])" in shown
    assert SECRET_PLANNING not in shown and "tipo=" not in shown


def test_preview_of_a_definition_gets_the_lead_the_form_and_the_date_like_the_pdf(
    client: TestClient, deps: Deps
) -> None:
    preview = "<p>{{ lead.name }} | {{ form.title }} | {{ form.slug }} | {{ generated_date }}</p>"
    spec = compiled(changed({"preview": preview}))
    publish(deps, spec)
    page = send_form(client, spec, DEMO_ANSWERS)
    complete(deps, page)
    with deps.sessions.begin() as session:  # o PDF ficou pronto às 23h30 de 7/10 em São Paulo
        session.execute(sa.update(Artifact).values(created_at=LATE_NIGHT))

    shown = page_text(client.get(page).text)

    assert (
        "Contadora Exemplo | Projeto de demonstração | demo-definicao | 7 de outubro de 2026"
    ) in shown


BROKEN_TITLE = "Plano que quebra a prévia"


def test_result_page_still_offers_the_pdf_when_the_preview_of_a_definition_fails(
    client: TestClient,
    deps: Deps,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # O `fileConfig` do alembic, que a suíte roda na subida, desliga os loggers que já existiam.
    monkeypatch.setattr(logging.getLogger("app.web.public"), "disabled", False)
    preview = (
        f'{{% if plan.titulo == "{BROKEN_TITLE}" %}}{{{{ plan.titulo + 1 }}}}{{% endif %}}'
        "<h2>{{ plan.titulo }}</h2>"
    )
    spec = compiled(changed({"preview": preview}))
    publish(deps, spec)
    page = send_form(client, spec, DEMO_ANSWERS)
    complete(deps, page)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(Plan).values(document={**spec.example_plan, "titulo": BROKEN_TITLE})
        )

    with caplog.at_level(logging.WARNING, logger="app.web.public"):
        response = client.get(page)

    assert response.status_code == 200
    shown = page_text(response.text)
    assert "Seu plano está pronto, Contadora" in shown and BROKEN_TITLE not in shown
    assert tags(response.text, "a", href=f"{page}/pdf")
    assert "preview, linha 1: erro ao renderizar (TypeError)" in caplog.text
    assert BROKEN_TITLE not in caplog.text and DEMO_ANSWERS["nome"] not in caplog.text


# --- respostas: normalizadores e identidade -----------------------------------------------------

SECTORS = [
    {"value": "comercio", "label": "Comércio"},
    {"value": "servicos", "label": "Serviços"},
    {"value": "industria", "label": "Indústria"},
    {"value": "outros", "label": "Outros"},
]
CONTACT_STEP: dict[str, Any] = {
    "title": "Contato e carteira",
    "questions": [
        {"name": "whatsapp", "kind": "tel", "label": "WhatsApp", "required": False},
        {
            "name": "setores",
            "kind": "choices",
            "label": "Setores",
            "max_choices": 3,
            "options": SECTORS,
        },
        {
            "name": "faixa",
            "kind": "choice",
            "label": "Faixa",
            "required": False,
            "options": SECTORS,
        },
        {
            "name": "canais",
            "kind": "choices",
            "label": "Canais",
            "required": False,
            "options": SECTORS,
        },
    ],
}
RICH_ANSWERS: dict[str, Any] = {**DEMO_ANSWERS, "setores": ["comercio"]}


def rich() -> dict[str, Any]:
    """A definição do demo com mais uma etapa: telefone, escolhas múltiplas e opcionais."""
    document = changed(
        {
            "identity.phone": "whatsapp",
            "consent.marketing.whatsapp": "Quero receber conteúdos no WhatsApp.",
            "examples.answers.setores": ["comercio"],
        }
    )
    document["steps"].append(copy.deepcopy(CONTACT_STEP))
    return document


def errors_of(spec: FormSpec, answers: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    with pytest.raises(ValidationError) as caught:
        answers_of(spec, answers)
    return [(error["type"], error["loc"]) for error in caught.value.errors()]


def test_email_is_lowercased_and_trimmed_and_must_look_like_an_email() -> None:
    spec = compiled()

    answers = answers_of(spec, {**DEMO_ANSWERS, "email": "  Contadora@example.test "})

    assert answers.email == "contadora@example.test"
    assert errors_of(spec, {**DEMO_ANSWERS, "email": "contadora@exemplo"}) == [
        ("value_error", ("email",))
    ]


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("(11) 91234-5678", "11912345678"),
        ("+55 11 91234-5678", "11912345678"),
        ("5511912345678", "11912345678"),
        ("(11) 3456-7890", "1134567890"),
    ],
)
def test_phone_is_stored_as_ddd_and_number_without_the_55(typed: str, stored: str) -> None:
    spec = compiled(rich())

    assert answers_of(spec, {**RICH_ANSWERS, "whatsapp": typed}).whatsapp == stored


@pytest.mark.parametrize("typed", ["1234", "(01) 91234-5678", "liga pra mim", "551191234567890"])
def test_phone_without_ddd_and_number_is_refused(typed: str) -> None:
    spec = compiled(rich())

    assert errors_of(spec, {**RICH_ANSWERS, "whatsapp": typed}) == [("value_error", ("whatsapp",))]


def test_repeated_choices_count_once_in_the_order_they_came() -> None:
    spec = compiled(rich())

    chosen = ["servicos", "comercio", "servicos", "comercio", "industria"]
    answers = answers_of(spec, {**RICH_ANSWERS, "setores": chosen})

    assert answers.setores == ["servicos", "comercio", "industria"]


def test_a_single_choice_sent_as_text_counts_as_a_list_of_one() -> None:
    """Formulários HTML mandam o texto, e não uma lista, quando só uma caixa está marcada (o MCC já
    aceitava assim). Um texto que não é opção segue recusado, e o texto em branco é resposta
    ausente, como a lista vazia."""
    spec = compiled(rich())

    assert answers_of(spec, {**RICH_ANSWERS, "setores": "servicos"}).setores == ["servicos"]
    assert answers_of(spec, {**RICH_ANSWERS, "canais": " "}).canais is None
    assert [kind for kind, _ in errors_of(spec, {**RICH_ANSWERS, "setores": "cpf"})] == [
        "literal_error"
    ]
    assert [kind for kind, _ in errors_of(spec, {**RICH_ANSWERS, "setores": " "})] == ["missing"]


@pytest.mark.parametrize(
    ("chosen", "error"),
    [
        (["comercio", "servicos", "industria", "outros"], "too_long"),
        (["comercio", "cpf"], "literal_error"),
    ],
)
def test_choices_keep_to_the_options_and_the_maximum(chosen: list[str], error: str) -> None:
    spec = compiled(rich())

    assert [kind for kind, _ in errors_of(spec, {**RICH_ANSWERS, "setores": chosen})] == [error]


@pytest.mark.parametrize(
    ("name", "blank"),
    [("escritorio", "   "), ("whatsapp", ""), ("faixa", " "), ("canais", [])],
)
def test_blank_optional_answer_counts_as_unanswered(name: str, blank: Any) -> None:
    spec = compiled(rich())

    assert getattr(answers_of(spec, {**RICH_ANSWERS, name: blank}), name) is None


@pytest.mark.parametrize(
    ("name", "blank"),
    [("nome", "  "), ("email", ""), ("tamanho_equipe", ""), ("setores", [])],
)
def test_blank_required_answer_is_missing(name: str, blank: Any) -> None:
    spec = compiled(rich())

    assert errors_of(spec, {**RICH_ANSWERS, name: blank}) == [("missing", (name,))]


def test_answers_outside_the_questions_are_refused() -> None:
    assert errors_of(compiled(), {**DEMO_ANSWERS, "cpf": "000.000.000-00"}) == [
        ("extra_forbidden", ("cpf",))
    ]


def test_identity_takes_the_email_name_and_phone_from_the_mapped_answers() -> None:
    spec = compiled(rich())

    answers = answers_of(spec, {**RICH_ANSWERS, "whatsapp": "(11) 91234-5678"})

    assert spec.identity(answers) == LeadIdentity(
        email="contadora@example.test", name="Contadora Exemplo", phone="11912345678"
    )
    demo = compiled()
    assert demo.identity(answers_of(demo, DEMO_ANSWERS)) == LeadIdentity(
        email="contadora@example.test", name="Contadora Exemplo"
    )


# --- problemas da definição, com o caminho do campo ---------------------------------------------


def problems(document: dict[str, Any] | str) -> list[str]:
    with pytest.raises(DefinitionError) as caught:
        compile_definition(document, clock=lambda: NOW)
    return [str(problem) for problem in caught.value.problems]


def options(*values: str) -> list[dict[str, str]]:
    return [{"value": value, "label": value.capitalize()} for value in values]


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        # estrutura
        (changed(drop=("title",)), "title: campo obrigatório"),
        (changed({"format": 2}), "format: use um destes valores: 1"),
        (changed({"cor": "vinho"}), "cor: campo desconhecido"),
        (changed({"scoring": {}}), "scoring.scores: campo obrigatório"),
        (changed({"minutes": 0}), "minutes: precisa ser pelo menos 1"),
        (
            changed({"slug": "Demo Definição"}),
            "slug: use letras minúsculas sem acento, números e hífens entre as palavras",
        ),
        (
            changed({"steps.0.questions.1.kind": "data"}),
            (
                "steps.0.questions.1.kind: use um destes valores: 'text', 'email', 'tel', "
                "'textarea', 'choice' ou 'choices'"
            ),
        ),
        (changed({"steps.1.questions": []}), "steps.1.questions: precisa de pelo menos 1 item"),
        (
            changed({"steps.0.questions.0.name": "Nome"}),
            (
                "steps.0.questions.0.name: use letras minúsculas sem acento, números e _, "
                "começando por uma letra"
            ),
        ),
        # perguntas, identidade e consentimento
        (
            changed({"steps.0.questions.2.name": "nome"}),
            "steps.0.questions.2.name: outra pergunta já usa este nome",
        ),
        (
            changed({"steps.0.questions.2.name": "model_dump"}),
            "steps.0.questions.2.name: nome reservado; escolha outro",
        ),
        (
            changed({"steps.1.questions.0.options": None}),
            "steps.1.questions.0.options: a pergunta de escolha precisa das opções",
        ),
        (
            changed({"steps.0.questions.0.options": options("a", "b")}),
            "steps.0.questions.0.options: só perguntas de escolha têm opções",
        ),
        (
            changed({"steps.1.questions.0.options.1.value": "so_eu"}),
            "steps.1.questions.0.options.1.value: outra opção da pergunta já usa este valor",
        ),
        (
            changed(drop=("steps.0.questions.0.max_length",)),
            "steps.0.questions.0.max_length: a resposta de texto precisa de um tamanho máximo",
        ),
        (
            changed({"steps.0.questions.0.min_length": 200}),
            "steps.0.questions.0.min_length: o mínimo passa do máximo (120)",
        ),
        (
            changed({"steps.1.questions.0.max_length": 10}),
            "steps.1.questions.0.max_length: só respostas de texto têm limite de tamanho",
        ),
        (
            changed({"steps.0.questions.1.min_length": 5}),
            "steps.0.questions.1.min_length: e-mail e telefone não têm tamanho mínimo",
        ),
        (
            changed({"steps.1.questions.0.max_choices": 2}),
            "steps.1.questions.0.max_choices: só a escolha múltipla tem mínimo e máximo de escolhas",
        ),
        (
            changed({"steps.1.questions.1.placeholder": "Escolha"}),
            "steps.1.questions.1.placeholder: só perguntas de texto têm placeholder",
        ),
        (
            changed({"identity.email": "escritorio"}),
            "identity.email: precisa ser uma pergunta de e-mail obrigatória",
        ),
        (
            changed({"steps.0.questions.1.required": False}),
            "identity.email: precisa ser uma pergunta de e-mail obrigatória",
        ),
        (
            changed({"identity.email": "contato"}),
            "identity.email: não há pergunta com este nome",
        ),
        (changed({"identity.name": "email"}), "identity.name: precisa ser uma pergunta de texto"),
        (
            changed({"identity.phone": "nome"}),
            "identity.phone: precisa ser uma pergunta de telefone",
        ),
        (
            changed({"consent.marketing.whatsapp": "Quero receber no WhatsApp."}),
            (
                "consent.marketing.whatsapp: o canal WhatsApp precisa do telefone da lead "
                "(identity.phone)"
            ),
        ),
        # modelo do plano
        (
            changed({"plan.fields.1.name": "titulo"}),
            "plan.fields.1.name: outro campo do grupo já usa este nome",
        ),
        (changed(drop=("plan.fields.0.name",)), "plan.fields.0.name: campo obrigatório"),
        (
            changed({"plan.fields.0.name": "json"}),
            "plan.fields.0.name: nome reservado; escolha outro",
        ),
        (
            changed(drop=("plan.fields.2.items",)),
            "plan.fields.2.items: a lista precisa dos itens",
        ),
        (
            changed({"plan.fields.2.items.name": "acao"}),
            "plan.fields.2.items.name: os itens de uma lista não têm nome",
        ),
        (
            changed({"plan.fields.2.items.internal": True}),
            "plan.fields.2.items.internal: marque como interno o campo da lista, não os itens",
        ),
        (
            changed({"plan.fields.2.items": {"kind": "list", "items": {"kind": "text"}}}),
            "plan.fields.2.items.kind: lista de listas não é suportada",
        ),
        (
            changed({"plan.fields.0.min_length": 500}),
            "plan.fields.0.min_length: o mínimo passa do máximo (120)",
        ),
        (
            changed({"plan.fields.2.min_items": 9}),
            "plan.fields.2.min_items: o mínimo passa do máximo (5)",
        ),
        (
            changed({"plan.fields.0.min_items": 1}),
            "plan.fields.0.min_items: não vale para campos do tipo text",
        ),
        (
            changed({"plan.fields.0": {"name": "tipo", "kind": "options"}}),
            "plan.fields.0.values: as opções fixas precisam dos valores ou de uma lista dos dados do formulário",
        ),
        (
            changed({"plan.fields.0": {"name": "tipo", "kind": "options", "values": ["a", "a"]}}),
            "plan.fields.0.values.1: valor repetido",
        ),
        (
            changed({"plan.fields.0": {"name": "grupo", "kind": "group"}}),
            "plan.fields.0.fields: o grupo precisa dos campos",
        ),
        (
            phased({"plan.fields.2.items.fields.1.items.model": "Fase"}),
            "plan.fields.2.items.fields.1.items.model: outro grupo já usa o nome Fase",
        ),
        (
            changed({"data": {"edicao": {"value": 1}}}),
            "data.edicao.kind: campo obrigatório",
        ),
        (
            changed({"data": {"edicao": {"kind": "janelas", "value": 1}}}),
            "data.edicao.kind: use um destes tipos: value, list, windows, edition",
        ),
        (
            changed({"data": {"Edição": block(1)}}),
            (
                "data.Edição: nome inválido; use letras minúsculas sem acento, números e _, "
                "começando por uma letra"
            ),
        ),
        # exemplos
        (
            changed({"examples.answers.tamanho_equipe": "dez"}),
            (
                "examples.answers.tamanho_equipe: use um destes valores: 'so_eu', '2_a_5', "
                "'6_a_15' ou 'mais_de_15'"
            ),
        ),
        (changed(drop=("examples.answers.nome",)), "examples.answers.nome: campo obrigatório"),
        (
            changed({"examples.plan.acoes": ["Só uma ação."]}),
            "examples.plan.acoes: precisa de pelo menos 3 itens",
        ),
        (changed({"examples.plan.extra": "x"}), "examples.plan.extra: campo desconhecido"),
    ],
)
def test_definition_problems_say_where_they_are(document: dict[str, Any], expected: str) -> None:
    assert expected in problems(document)


def test_every_problem_of_the_definition_comes_at_once() -> None:
    document = changed(
        {
            "steps.0.questions.2.name": "nome",
            "plan.fields.1.name": "titulo",
            "consent.marketing.whatsapp": "Quero receber no WhatsApp.",
        }
    )

    assert problems(document) == [
        "steps.0.questions.2.name: outra pergunta já usa este nome",
        "consent.marketing.whatsapp: o canal WhatsApp precisa do telefone da lead (identity.phone)",
        "plan.fields.1.name: outro campo do grupo já usa este nome",
    ]


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            changed({"prompt": "Linha 1\nLinha 2 {{ answers.nome "}),
            "prompt, linha 2: sintaxe inválida: o template termina no meio de uma expressão",
        ),
        (
            changed({"prompt": "{% if answers.nome %}\nsem fim"}),
            "prompt, linha 1: sintaxe inválida: o bloco if não foi fechado (falta o endif)",
        ),
        (
            changed({"preview": "{% sefor x in plan.acoes %}"}),
            "preview, linha 1: sintaxe inválida: tag desconhecida ou fora do lugar: sefor",
        ),
        (
            changed({"pdf.html": "{{ plan.titulo ) }}"}),
            "pdf.html, linha 1: sintaxe inválida: símbolo inesperado: )",
        ),
        (
            changed({"prompt": "Olá\n\n{{ respostas.nome }}"}),
            "prompt, linha 3: variável inexistente: respostas",
        ),
        (
            changed({"prompt": "{{ answers.cpf }}"}),
            "prompt, linha 1: answers.cpf não é uma pergunta do formulário",
        ),
        (
            changed({"prompt": "\n{{ labels.objetivo_90_dias }}"}),
            "prompt, linha 2: labels.objetivo_90_dias não é uma pergunta de escolha",
        ),
        (
            changed({"instructions": "{{ lipsum() }}"}),
            "instructions, linha 1: variável inexistente: lipsum",
        ),
        (
            changed({"pdf.html": "<h1>{{ plan.subtitulo }}</h1>"}),
            "pdf.html, linha 1: plan.subtitulo não é um campo do plano",
        ),
        (
            phased({"pdf.html": "<p>\n{{ plan.uso_da_janela }}</p>"}),
            (
                "pdf.html, linha 2: plan.uso_da_janela é um campo interno, que não chega ao PDF "
                "nem à prévia"
            ),
        ),
        (
            changed({"pdf.html": "{{ lead.email }}"}),
            "pdf.html, linha 1: lead.email não existe no contexto do template",
        ),
        (
            changed({"preview": "{{ plan.titulo|pprint }}"}),
            "preview, linha 1: filtro inexistente: pprint",
        ),
        (
            changed({"pdf.html": "<p>{{ plan.resumo|safe }}</p>"}),
            "pdf.html, linha 1: filtro inexistente: safe",
        ),
        (
            changed({"preview": "{% include 'outro.html' %}"}),
            "preview, linha 1: não é possível carregar outro template",
        ),
        (
            changed({"prompt": "{{ answers.objetivo_90_dias + 1 }}"}),
            "prompt, linha 1: erro ao renderizar (TypeError)",
        ),
        (
            changed({"pdf.html": "{% for acao in plan.acoes %}\n{{ acao.texto }}{% endfor %}"}),
            "pdf.html, linha 2: variável ou campo inexistente",
        ),
        (
            changed({"instructions": "{{ data.edicao.nome }}"}),
            "instructions, linha 1: variável ou campo inexistente",
        ),
    ],
)
def test_template_problems_surface_at_compile_time_with_the_line(
    document: dict[str, Any], expected: str
) -> None:
    found = problems(document)

    assert any(problem.startswith(expected) for problem in found), found


def test_problems_of_every_template_come_at_once() -> None:
    document = changed({"prompt": "{{ respostas }}", "pdf.html": "{{ plan.subtitulo }}"})

    assert problems(document) == [
        "prompt, linha 1: variável inexistente: respostas",
        "pdf.html, linha 1: plan.subtitulo não é um campo do plano",
    ]


# --- ambiente isolado ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (changed({"prompt": "{{ answers.__class__ }}"}), "prompt, linha 1"),
        (changed({"instructions": "{{ ''.__class__.__mro__ }}"}), "instructions, linha 1"),
        (changed({"pdf.html": "\n{{ plan.titulo.__class__.__base__ }}"}), "pdf.html, linha 2"),
        (changed({"preview": "{{ plan['__class__'] }}"}), "preview, linha 1"),
        (changed({"prompt": "{{ answers._fields }}"}), "prompt, linha 1"),
    ],
)
def test_templates_cannot_reach_internal_attributes(
    document: dict[str, Any], expected: str
) -> None:
    found = problems(document)

    assert any(problem.startswith(f"{expected}: atributo interno proibido") for problem in found)


def test_internal_attribute_built_from_constants_is_refused_when_compiling() -> None:
    """A conferência lê o template como foi escrito; a renderização com os exemplos, que a
    compilação faz, esbarra no ambiente isolado."""
    assert problems(changed({"prompt": '{{ answers[("_", "_class__")|join] }}'})) == [
        "prompt, linha 1: acesso proibido pelo ambiente isolado"
    ]


def test_sandbox_refuses_internal_attributes_reached_by_a_name_known_only_when_rendering() -> None:
    """O nome vem dos dados, então a conferência da compilação não o vê (e o ramo não roda com as
    respostas de exemplo): quem recusa, na renderização, é o próprio ambiente isolado."""
    prompt = (
        '{% if answers.escritorio == "Escritório Hostil" %}'
        "{{ answers[data.campo] }}{% endif %}{{ answers.nome }}"
    )
    spec = compiled(changed({"prompt": prompt, "data": {"campo": block("__class__")}}))
    hostile = answers_of(spec, {**DEMO_ANSWERS, "escritorio": "Escritório Hostil"})

    with pytest.raises(TemplateProblem) as caught:
        spec.render_prompt(hostile)

    assert str(caught.value) == "prompt, linha 1: acesso proibido pelo ambiente isolado"
    trace = "".join(traceback.format_exception(caught.value))
    assert "Escritório Hostil" not in trace and DEMO_ANSWERS["nome"] not in trace


def test_sandbox_refuses_changing_the_form_data() -> None:
    document = changed(
        {"instructions": "{{ data.lista.append(4) }}", "data": {"lista": block([1, 2, 3])}}
    )

    assert problems(document) == ["instructions, linha 1: acesso proibido pelo ambiente isolado"]


@pytest.mark.parametrize(
    "expression",
    [
        "attr('upper')",
        "safe",
        "pprint",
        "tojson",
        "random",
        "indent(4)",
        "center(80)",
        "batch(2)",
        "slice(2)",
        "wordwrap(10)",
        "urlize",
        "xmlattr",
    ],
)
def test_filters_outside_the_closed_set_do_not_compile(expression: str) -> None:
    document = changed({"preview": f"<p>{{{{ plan.titulo|{expression} }}}}</p>"})

    assert problems(document) == [
        f"preview, linha 1: filtro inexistente: {expression.split('(')[0]}"
    ]


def test_filters_the_current_templates_use_stay_in_the_closed_set() -> None:
    prompt = (
        '{{ "%02d"|format(3) }} {{ ["b", "a", "b"]|unique|join(", ") }} {{ answers.nome|lower }} '
        '{{ answers.objetivo_90_dias|truncate(20, end="…") }} '
        '{{ "MÓDULO 1"|replace("MÓDULO", "Módulo") }} {{ [1, 2]|length }} {{ ["x", "y"]|first }}'
    )
    spec = compiled(changed({"prompt": prompt}))

    rendered = spec.render_prompt(answers_of(spec, DEMO_ANSWERS))

    assert rendered == "03 b, a contadora exemplo Organizar a… Módulo 1 2 x"


@pytest.mark.parametrize(
    "expression",
    [
        '"x" * 10**9',
        "[1] * 1000000",
        "2 ** 100000",
        '"%50000000d"|format(1)',
        '"%-999999999s"|format("x")',
        '"%.50000000f"|format(1.5)',
        '"%*d"|format(50000000, 1)',
        '"%(n)50000000d"|format(n=1)',
        '"%(a(b))50000000d"|format(**{"a(b)": 1})',
    ],
)
def test_sandbox_refuses_results_too_big_to_compute(expression: str) -> None:
    document = changed({"prompt": f"{{{{ {expression} }}}}"})

    assert problems(document) == ["prompt, linha 1: operação grande demais para o ambiente isolado"]


@pytest.mark.parametrize(
    "template",
    [
        '{% for n in range(50) %}{{ "x" * 100000 }}{% endfor %}',
        '{{ (["x" * 100000] * 20)|join }}',
    ],
)
def test_sandbox_refuses_output_over_the_size_limit(template: str) -> None:
    document = changed({"prompt": template})

    assert problems(document) == ["prompt: operação grande demais para o ambiente isolado"]


def test_format_filter_keeps_working_with_ordinary_field_widths() -> None:
    prompt = (
        '{{ "%02d"|format(3) }}|{{ "%-6s"|format("ab") }}|{{ "%8.2f"|format(3.14159) }}|'
        '{{ "%(n)05d"|format(n=42) }}|{{ "100%% de %s"|format("meta") }}|'
        '{{ "%%50000000d"|format() }}'
    )
    spec = compiled(changed({"prompt": prompt}))

    rendered = spec.render_prompt(answers_of(spec, DEMO_ANSWERS))

    assert rendered == "03|ab    |    3.14|00042|100% de meta|%50000000d"


def test_definition_given_as_json_text_reports_the_line_of_a_syntax_error() -> None:
    assert problems('{\n  "format": 1,\n  "slug": \n}') == [
        "definição, linha 4: JSON inválido: falta um valor (coluna 1)"
    ]
    assert problems('{"format" 1}') == [
        (
            "definição, linha 1: JSON inválido: faltam os dois-pontos depois do nome do campo "
            "(coluna 11)"
        )
    ]
    assert compile_definition(json.dumps(DEFINITION), clock=lambda: NOW).slug == "demo-definicao"


def test_definition_given_as_bytes_must_be_utf8() -> None:
    assert problems('{"title": "Diagnóstico"}'.encode("latin-1")) == [
        "definição: o texto JSON precisa estar em UTF-8"
    ]
    encoded = json.dumps(DEFINITION, ensure_ascii=False).encode("utf-8")
    assert compile_definition(encoded, clock=lambda: NOW).title == DEFINITION["title"]


def nested(levels: int) -> list[Any]:
    value: list[Any] = []
    for _ in range(levels):
        value = [value]
    return value


NOT_A_NUMBER = "NaN e Infinity não são números em JSON"
BAD_CHARACTER = "o texto tem um caractere que não vale (o nulo, \\u0000, ou a metade de um emoji)"


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            json.dumps(DEFINITION).replace(
                '"data": {}', '"data": {"meta": {"kind": "value", "value": NaN}}'
            ),
            f"data.meta.value: {NOT_A_NUMBER}",
        ),
        (
            changed({"data": {"meta": block([1, float("inf")])}}),
            f"data.meta.value.1: {NOT_A_NUMBER}",
        ),
        (
            changed({"steps.0.questions.0.label": "Nome\x00completo"}),
            f"steps.0.questions.0.label: {BAD_CHARACTER}",
        ),
        (changed({"prompt": "Olá \ud83d"}), f"prompt: {BAD_CHARACTER}"),
        (
            changed({"data": {"meta": block({"chave\x00": 1})}}),
            f"data.meta.value: nome inválido; {BAD_CHARACTER}",
        ),
        (
            "[" * 50_000 + "]" * 50_000,
            "definição: JSON inválido: há listas ou objetos demais, um dentro do outro",
        ),
    ],
    ids=["nan", "infinito", "nulo", "meio-emoji", "nome-com-nulo", "aninhado-no-texto"],
)
def test_json_that_the_database_cannot_keep_is_refused_where_it_is(
    document: dict[str, Any] | str, expected: str
) -> None:
    """O JSON do Python aceita o que o banco (JSONB) não guarda: a definição recusa antes."""
    assert problems(document) == [expected]


def test_nesting_has_a_ceiling_far_above_any_real_definition() -> None:
    (problem,) = problems(changed({"data": {"meta": block(nested(70))}}))

    assert problem.startswith("data.meta.value.0.0.")
    assert problem.endswith(": há listas ou objetos demais, um dentro do outro")
    assert compile_definition(changed({"data": {"meta": block(nested(40))}}), clock=lambda: NOW)
