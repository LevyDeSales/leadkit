"""Calendário fictício: perfis de pontuação, schema estrito, regras temporais,
PDF neutro, página pública e webhook. Os testes dia a dia vivem em test_legacy_forms_golden.py."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from conftest import legacy_spec, record_webhook
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_openai_llm import (
    NOT_FOR_STRICT_MODE,
    FakeOpenAI,
    _without_openai_env,  # noqa: F401 - fixture autouse: OPENAI_* fora
    generator,
    recorded_by_hatchet,
    reply,
    schema_nodes,
)
from test_public import tags
from test_public import text as page_text
from test_weasyprint_pdf import fonts_of, pages_of, squash, text_of

from app import jobs, pipeline
from app.api.main import create_app
from app.deps import Deps, build_pdf
from app.errors import FailureCode, RetryableError
from app.models import Job
from app.settings import Settings

SPEC = legacy_spec("reforma-2027")
PROFILES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "reforma_2027_perfis.json").read_text(encoding="utf-8")
)
B2B = next(profile for profile in PROFILES if profile["perfil"] == "b2b_em_construcao")
B2B_ANSWERS = SPEC.answers_model.model_validate(B2B["respostas"])
CONSENT: dict[str, Any] = {"privacy": True, "text_version": "reforma-2027-v1"}
# Dia em que o plano de exemplo foi escrito: a opção fecha na fase 1 e a desistência vai até a fase 3.
EXAMPLE_DAY = datetime(2026, 10, 8, 12, tzinfo=UTC)


# O formulário com o relógio parado no dia do plano de exemplo: o prompt e a conferência do plano
# veem a mesma data, e os testes não mudam de resultado quando a janela de opção fecha de verdade.
ON_EXAMPLE_DAY = legacy_spec("reforma-2027", clock=lambda: EXAMPLE_DAY)


@pytest.fixture
def client(deps: Deps) -> TestClient:
    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    return TestClient(create_app(trigger=fake_trigger))


@pytest.fixture
def real_pdf(deps: Deps) -> Deps:
    deps.pdf = build_pdf(Settings(_env_file=None, PDF_MODE="weasyprint"))  # type: ignore[call-arg]
    return deps


def send(client: TestClient, answers: dict[str, Any]) -> httpx.Response:
    return client.post(
        "/v1/forms/reforma-2027/submissions",
        json={"answers": answers, "consent": CONSENT},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


def run_pipeline(deps: Deps, public_id: str) -> list[uuid.UUID]:
    """O que o worker faria: plano (LLM falso), PDF e conclusão. Devolve as entregas criadas."""
    with deps.sessions.begin() as session:
        job_id = session.scalar(sa.select(Job.id).where(Job.public_id == public_id))
        jobs.claim(session, job_id, 1, "run-fake")
    plan_id, _ = pipeline.generate_plan(deps, job_id, 1)
    artifact_id, _ = pipeline.render_pdf(deps, job_id, 1, plan_id)
    return pipeline.finalize(deps, job_id, 1, artifact_id)


# --- pontuação --------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES, ids=[profile["perfil"] for profile in PROFILES])
def test_reference_profiles_get_their_readiness_exposure_priority_and_persona(
    profile: dict[str, Any],
) -> None:
    result = SPEC.score(SPEC.answers_model.model_validate(profile["respostas"]))  # type: ignore[misc]
    expected = profile["esperado"]

    assert {key: result.scores[key] for key in expected if key in result.scores} == {
        key: value for key, value in expected.items() if key not in ("prioridade", "persona")
    }
    assert (result.priority, result.persona) == (expected["prioridade"], expected["persona"])


def test_reference_profiles_cover_every_level_priority_and_persona() -> None:
    """Os cinco perfis são a base da simulação ponta a ponta: precisam gerar planos diferentes."""
    expected = [profile["esperado"] for profile in PROFILES]

    assert {item["nivel_prontidao"] for item in expected} == {
        "inicial",
        "em_construcao",
        "avancada",
        "pronta",
    }
    assert {item["prioridade"] for item in expected} == {"P1", "P2", "P3"}
    assert len({item["persona"] for item in expected}) == 5


# --- respostas --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("(11) 91234-5678", "11912345678"),
        ("+55 11 91234-5678", "11912345678"),
        ("5511912345678", "11912345678"),
        ("(11) 3456-7890", "1134567890"),
        ("", None),
    ],
)
def test_whatsapp_is_stored_as_ddd_and_number(typed: str, stored: str | None) -> None:
    answers = SPEC.answers_model.model_validate({**B2B["respostas"], "whatsapp": typed})

    assert answers.whatsapp == stored  # type: ignore[attr-defined]


@pytest.mark.parametrize("typed", ["1234", "(01) 91234-5678", "liga pra mim", "551191234567890"])
def test_whatsapp_without_ddd_and_number_is_refused(typed: str) -> None:
    with pytest.raises(ValidationError):
        SPEC.answers_model.model_validate({**B2B["respostas"], "whatsapp": typed})


def test_repeated_sectors_count_once_and_unknown_fields_are_refused() -> None:
    answers = SPEC.answers_model.model_validate(
        {**B2B["respostas"], "setores": ["comercio", "comercio", "servicos"]}
    )

    assert answers.setores == ["comercio", "servicos"]  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        SPEC.answers_model.model_validate({**B2B["respostas"], "cpf": "000.000.000-00"})


# --- o que vai para a OpenAI (SDK real sobre transporte simulado) -------------------------------


def sent_prompt(answers: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Prompt e corpo que o adaptador da OpenAI manda ao provedor com estas respostas."""
    server = FakeOpenAI(reply(SPEC.example_plan))
    generator(server).generate(ON_EXAMPLE_DAY, SPEC.answers_model.model_validate(answers))
    (message,) = server.bodies[0]["input"]
    return message["content"], server.bodies[0]


def test_prompt_has_option_labels_and_the_computed_reading_but_no_contact_data() -> None:
    prompt, body = sent_prompt(B2B["respostas"])
    for expected in (
        "De 6 a 15 pessoas",
        "Comércio, Indústria, Serviços",
        "Em construção",
        "Nota prontidao: 55",
        "Nota exposicao: 80",
        B2B["respostas"]["objetivo"],
        "- Fase 1:",
        "- Fase 3:",
    ):
        assert expected in prompt
    for contact in ("Beatriz", "beatriz.b2b@example.test", "90000", "Escritório Exemplo B2B"):
        assert contact not in prompt
    assert "economia garantida" in body["instructions"]
    assert body["store"] is False


def test_plan_starts_in_the_lead_month_in_sao_paulo() -> None:
    late_october = legacy_spec(
        "reforma-2027", clock=lambda: datetime(2026, 11, 1, 2, 30, tzinfo=UTC)
    )
    prompt = late_october.render_prompt(B2B_ANSWERS)
    assert "31/10/2026" in prompt
    assert "31 de outubro" in prompt


def changed_plan(**changes: Any) -> dict[str, Any]:
    plan = json.loads(json.dumps(SPEC.example_plan))
    for path, value in changes.items():
        target = plan
        *parents, last = path.split("__")
        for key in parents:
            target = target[int(key)] if key.isdigit() else target[key]
        target[int(last) if last.isdigit() else last] = value
    return plan


@pytest.mark.parametrize(
    "plan",
    [
        changed_plan(fases__0__nome="Dias 1 a 30"),
        changed_plan(fases__1__nome="Fase 2"),
        changed_plan(fases__0__acoes__0__titulo="Classificar"),
    ],
    ids=["fase-com-dias", "fase-com-numero", "acao-so-com-verbo"],
)
def test_phase_named_after_days_and_one_word_actions_are_refused(plan: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SPEC.plan_model.model_validate(plan)


@pytest.mark.parametrize(
    "sentence",
    [
        "Evite falar de economia garantida com os clientes nas reuniões.",
        "O escritório terá resultado garantido ao fim dos 90 dias.",
        "Ofereça a consultoria parcelada sem juros para os clientes.",
        "Apresente a única IA do mercado para simular os cenários.",
        "Use a calculadora definitiva para decidir o regime de cada cliente.",
    ],
)
def test_forbidden_promises_are_refused_even_as_a_warning(sentence: str) -> None:
    with pytest.raises(ValidationError):
        SPEC.plan_model.model_validate(changed_plan(pontos_de_atencao__1=sentence))


def test_to_ensure_something_is_not_a_promise() -> None:
    sentence = "Para garantir dados completos, peça os documentos dos clientes logo no início."

    SPEC.plan_model.model_validate(changed_plan(pontos_de_atencao__1=sentence))


def test_forbidden_expression_sends_the_plan_to_one_repair_round_naming_it() -> None:
    warned = changed_plan(pontos_de_atencao__1="Evite falar de economia garantida com os clientes.")
    server = FakeOpenAI(reply(warned), reply(SPEC.example_plan))

    result = generator(server).generate(ON_EXAMPLE_DAY, B2B_ANSWERS)

    assert result.usage["repairs"] == 1
    assert "economia garantida" in server.bodies[1]["input"][-1]["content"]


def test_example_plan_is_accepted_at_once_under_a_strict_schema() -> None:
    server = FakeOpenAI(reply(SPEC.example_plan))

    result = generator(server).generate(ON_EXAMPLE_DAY, B2B_ANSWERS)

    assert result.usage["repairs"] == 0
    assert [len(fase["acoes"]) for fase in result.document["fases"]] == [3, 2, 2]
    nodes = list(schema_nodes(server.bodies[0]["text"]["format"]["schema"]))
    for node in nodes:
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])
    assert not [node for node in nodes if NOT_FOR_STRICT_MODE & node.keys()]


def test_every_list_limit_the_validation_enforces_is_written_for_the_model() -> None:
    """O modo estrito não leva `maxItems` ao modelo. Sem a contagem na descrição, as 5 primeiras
    respostas reais trouxeram 5 pontos de atenção (limite 4) e caíram na rodada de reparo."""
    schema = SPEC.plan_model.model_json_schema()
    objects = [schema, *schema.get("$defs", {}).values()]
    lists = [
        (name, prop)
        for node in objects
        for name, prop in node.get("properties", {}).items()
        if prop.get("type") == "array" and "maxItems" in prop
    ]

    assert {name for name, _ in lists} == {"prioridades", "fases", "pontos_de_atencao", "acoes"}
    for name, prop in lists:
        assert str(prop["maxItems"]) in prop.get("description", ""), name


def test_plan_with_two_priorities_gets_one_repair_round() -> None:
    short = {**SPEC.example_plan, "prioridades": SPEC.example_plan["prioridades"][:2]}
    server = FakeOpenAI(reply(short), reply(SPEC.example_plan))

    result = generator(server).generate(ON_EXAMPLE_DAY, B2B_ANSWERS)

    assert result.usage["repairs"] == 1
    assert "prioridades" in server.bodies[1]["input"][-1]["content"]


# --- calendário da opção: o tipo de cada ação, conferido no servidor ---------------------------

# O erro da rodada final da v5 (3 de 5 planos): orientar a escolha na fase 2, com a opção fechada.
LATE_DECISION = {
    "tipo": "decisao_da_opcao",
    "titulo": "Orientar a decisão de cada cliente",
    "como_fazer": "Depois da simulação, oriente a escolha entre guia única e regime regular.",
}


def test_example_plan_passes_the_check_on_the_day_it_was_written() -> None:
    ON_EXAMPLE_DAY.validate_plan(json.dumps(SPEC.example_plan), B2B_ANSWERS)
    kinds = {acao["tipo"] for fase in SPEC.example_plan["fases"] for acao in fase["acoes"]}
    assert kinds == {"decisao_da_opcao", "desistencia", "proxima_janela", "outra"}


def test_option_decision_after_the_window_closes_goes_to_one_repair_round() -> None:
    late = changed_plan(fases__1__acoes__1=LATE_DECISION)
    server = FakeOpenAI(reply(late), reply(SPEC.example_plan))

    result = generator(server).generate(ON_EXAMPLE_DAY, B2B_ANSWERS)

    assert result.usage["repairs"] == 1
    assert result.usage["repair_reasons"] == ["plano:plan_check"]  # o motivo fica no usage do plano
    assert result.document == SPEC.example_plan
    feedback = server.bodies[1]["input"][-1]["content"]
    assert (
        "- plano: fases.1.acoes.1.tipo (fase 2): o valor decisao_da_opcao não cabe nesta fase "
        "(janela Inscrição no projeto: já fechou). decisao_da_opcao só cabe na fase 1: leve "
        "esses itens para lá, mantendo as quantidades de cada campo, ou reescreva o conteúdo para "
        "um valor que caiba aqui (desistencia, proxima_janela, outra)." in feedback
    )
    assert "Não troque só o valor" in feedback  # trocar só o rótulo esconderia a decisão tardia
    assert LATE_DECISION["titulo"] not in feedback  # só posição, tipo e fase, nunca o texto
    assert LATE_DECISION["como_fazer"] not in feedback


def test_option_decision_still_late_after_the_repair_is_plan_invalid() -> None:
    late = changed_plan(fases__1__acoes__1=LATE_DECISION)
    server = FakeOpenAI(reply(late), reply(late))

    with pytest.raises(RetryableError) as caught:
        generator(server).generate(ON_EXAMPLE_DAY, B2B_ANSWERS)

    assert caught.value.code is FailureCode.PLAN_INVALID
    assert len(server.requests) == 2  # a mesma e única rodada de reparo
    recorded = recorded_by_hatchet(caught.value)
    assert "1 erro(s) (plan_check)" in recorded
    assert LATE_DECISION["titulo"] not in recorded and B2B["respostas"]["objetivo"] not in recorded


def test_internal_names_never_reach_the_text_the_lead_reads() -> None:
    leaked = "Marque esta ação como decisao_da_opcao na planilha da carteira."

    with pytest.raises(ValidationError, match="decisao_da_opcao"):
        SPEC.plan_model.model_validate(changed_plan(fases__0__acoes__0__como_fazer=leaked))
    internal = f"{SPEC.example_plan['uso_da_janela']} Só a fase 1 aceita decisao_da_opcao."
    SPEC.plan_model.model_validate(changed_plan(uso_da_janela=internal))


def test_prompt_lists_the_action_types_that_fit_each_phase() -> None:
    """Os tipos que o `check_plan` aceita em cada fase vão escritos no calendário do prompt: sem
    eles, 3 de 10 primeiras respostas reais punham simulação e decisão na fase 2."""
    prompt, _ = sent_prompt(B2B["respostas"])

    calendar = [line for line in prompt.splitlines() if line.startswith("- Fase ")]
    assert [line.rsplit("Tipos de ação que cabem: ", 1)[1] for line in calendar] == [
        "decisao_da_opcao, desistencia, proxima_janela, outra.",
        "desistencia, proxima_janela, outra.",
        "desistencia, proxima_janela, outra.",
    ]


def test_model_plans_the_window_before_the_phases_and_types_each_action_first() -> None:
    """A saída estruturada sai na ordem do schema: o modelo escreve o uso da janela antes das
    fases e escolhe o tipo antes de escrever cada ação."""
    server = FakeOpenAI(reply(SPEC.example_plan))

    generator(server).generate(ON_EXAMPLE_DAY, B2B_ANSWERS)

    schema = server.bodies[0]["text"]["format"]["schema"]
    fields = list(schema["properties"])
    assert fields.index("uso_da_janela") == fields.index("fases") - 1
    action = schema["$defs"]["Acao"]["properties"]
    assert next(iter(action)) == "tipo"
    assert action["tipo"]["enum"] == ["decisao_da_opcao", "desistencia", "proxima_janela", "outra"]


# --- PDF, página pública e webhook ------------------------------------------------------------


def test_pdf_is_on_brand_and_shows_the_readiness_and_the_plan(
    client: TestClient, real_pdf: Deps
) -> None:
    body = send(client, B2B["respostas"]).json()

    run_pipeline(real_pdf, body["public_id"])

    pdf = client.get(f"/v1/jobs/{body['public_id']}/pdf", params={"t": body["token"]})
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pages_of(pdf.content) >= 3
    fonts = fonts_of(pdf.content)  # fontes da marca, embutidas; nenhuma fonte padrão do app
    assert all(fonts.values()), fonts
    assert {name.startswith(("SourceSerifPro", "SourceSansPro")) for name in fonts} == {True}, fonts
    assert any(name.startswith("SourceSerifPro") for name in fonts), fonts
    assert any(name.startswith("SourceSansPro") for name in fonts), fonts
    text = squash(text_of(pdf.content))
    for expected in (
        "BeatrizExemplo",
        squash(SPEC.example_plan["titulo"]),
        "Emconstrução",
        "55de100",
        "Dias1a30",
        "Dias61a90",
        "Comeceestasemana",  # rótulo em caixa alta pelo CSS
        squash(SPEC.example_plan["proximo_passo"]),
    ):
        assert expected in text, expected
    for private in ("beatriz.b2b@example.test", "11900000002", "P2", "escolha_regime"):
        assert private not in text, private
    # Planejamento interno do calendário da opção: o uso da janela cita a fase pelo número.
    for internal in (squash(SPEC.example_plan["uso_da_janela"]), "decisao_da_opcao", "proxima_"):
        assert internal not in text, internal


def test_form_page_has_four_steps_multi_choice_sectors_and_whatsapp_consent(
    client: TestClient,
) -> None:
    response = client.get("/p/reforma-2027")

    assert response.status_code == 200
    html = response.text
    shown = page_text(html)
    for label in (
        "Vamos começar por você",
        "Sua carteira de clientes",
        "Como o escritório está se preparando",
        "Seu foco até a virada do ano",
        "WhatsApp (opcional)",
        "Quem compra de outra empresa passa a olhar o crédito de etapas A e B.",
    ):
        assert label in shown, label
    assert len(tags(html, "input", name="setores", type="checkbox")) == 5
    assert tags(html, "input", name="_consent_version")[0]["value"] == "reforma-2027-v1"
    for channel in ("_mkt_email", "_mkt_whatsapp"):
        (box,) = tags(html, "input", name=channel)
        assert "required" not in box and "checked" not in box


def form_data(answers: dict[str, Any]) -> dict[str, Any]:
    """O que o navegador envia: respostas pelo nome do campo e os campos de controle."""
    return {
        **answers,
        "_privacy": "1",
        "_consent_version": "reforma-2027-v1",
        "_idem": f"chave-{uuid.uuid4()}",
    }


def test_sent_from_the_public_page_ends_with_a_preview_and_the_pdf(
    client: TestClient, real_pdf: Deps
) -> None:
    sent = client.post("/p/reforma-2027", data=form_data(B2B["respostas"]), follow_redirects=False)
    assert sent.status_code == 303
    page = sent.headers["location"]

    run_pipeline(real_pdf, page.rsplit("/", 1)[1])

    result = client.get(page)
    assert result.status_code == 200
    shown = page_text(result.text)
    for preview in (
        SPEC.example_plan["titulo"],
        "Suas três prioridades",
        SPEC.example_plan["prioridades"][0]["titulo"],
        "Comece esta semana",
    ):
        assert preview in shown, preview
    pdf = client.get(f"{page}/pdf")
    assert pdf.status_code == 200 and pages_of(pdf.content) >= 3


def test_webhook_event_carries_the_reading_priority_persona_and_whatsapp(
    client: TestClient, deps: Deps
) -> None:
    webhook = record_webhook(deps)
    body = send(client, B2B["respostas"]).json()

    (delivery,) = run_pipeline(deps, body["public_id"])
    pipeline.deliver(deps, delivery)

    ((event, _, payload),) = webhook.calls
    assert event == "plano.concluido"
    assert payload["form"]["slug"] == "reforma-2027"
    assert payload["lead"]["phone"] == "11900000002"
    assert (payload["priority"], payload["persona"]) == ("P2", "escolha_regime")
    assert payload["scores"]["rotulo_prontidao"] == "Em construção"
    assert payload["scores"]["necessidade"] == 59
    assert len(payload["plan"]["fases"]) == 3


@pytest.mark.parametrize("name", ["Virada de 2027", "Rotinas para etapas A e B em 2027"])
def test_phase_name_may_carry_a_year(name: str) -> None:
    """Na rodada ao vivo do merge, 2 de 5 planos foram ao reparo só por um ano no nome da fase
    (`fases.2.nome:value_error` no usage)."""
    SPEC.plan_model.model_validate(changed_plan(fases__2__nome=name))


@pytest.mark.parametrize("name", ["Etapa 3", "Fase 2: ajustes", "Dias 61 a 90"])
def test_phase_name_with_a_step_number_is_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        SPEC.plan_model.model_validate(changed_plan(fases__2__nome=name))
