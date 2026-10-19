"""Cenário extenso fictício: envio pela API e página pública, pontuação, SDK OpenAI
simulado e PDF real WeasyPrint com recursos de demonstração."""

from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import sqlalchemy as sa
from conftest import legacy_spec, record_webhook
from fastapi.testclient import TestClient
from test_openai_llm import (
    NOT_FOR_STRICT_MODE,
    FakeOpenAI,
    _without_openai_env,  # noqa: F401 - fixture autouse: OPENAI_* fora
    generator,
    reply,
    schema_nodes,
)
from test_public import assert_private, tags
from test_public import text as page_text
from test_weasyprint_pdf import fonts_of, pages_of, squash, text_of

from app import jobs, pipeline
from app.api.main import create_app
from app.deps import Deps, build_pdf
from app.models import Job
from app.settings import Settings

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
MCC = legacy_spec("mcc")

# Diagnóstico completo e sintético (identidade e textos inventados), com os campos opcionais.
MCC_ANSWERS: dict[str, Any] = {
    "nome": "Contadora Exemplo da Silva",
    "email": "Contadora.MCC@Example.test",
    "whatsapp": "(19) 99876-5432",
    "nome_escritorio": "Escritório Exemplo Contabilidade",
    "cidade_uf": "Cidade Exemplo, SP",
    "local_atendimento": "escritorio_fisico",
    "estrutura_escritorio": "individual",
    "momento_atual": "lucro_e_tempo",
    "faixa_colaboradores": "3_a_5",
    "ausencia_7_dias": "decisoes_pendentes",
    "uso_do_tempo": "demandas_equipe",
    "delegacao": "delego_com_autonomia",
    "faixa_clientes": "21_a_50",
    "faixa_ticket_medio": "ate_500",
    "fontes_aquisicao": ["indicacao", "instagram"],
    "faixa_propostas_mes": "0_a_5",
    "faixa_taxa_fechamento": "acima_de_60",
    "faixa_faturamento_mensal": "abaixo_de_20_mil",
    "resposta_proposta": "pede_desconto",
    "estagio_escritorio": "cresce_com_algum_controle",
    "realidade_financeira": "margem_baixa",
    "dependencia_faturamento": "mais_de_80",
    "problema_recorrente": "fora_do_escopo",
    "onboarding": "sem_padrao",
    "processos_documentados": "nenhum",
    "diferencial": "solucao_estrategica",
    "cliente_ideal": "sem_clareza",
    "reforma_tributaria": "estudando_sem_plano",
    "queda_faturamento": "pressao_emergencial",
    "custo_por_cliente": "nao_sei",
    "redes_sociais": "conteudo_pontual",
    "principal_gargalo": "reforma_tributaria",
    "prioridade_90_dias": "organizar_operacao",
    "objetivo_principal": "Organizar a rotina do escritório e atender mais clientes sem perder margem.",
    "principal_obstaculo": "Faço quase tudo sozinha e não sobra tempo para vender.",
    "instagram": "@escritorio.exemplo",
    "faixa_custo_mensal": "ate_10_mil",
    "faixa_inadimplencia": "2_a_5",
    "criterio_de_sucesso": "Ter a semana planejada e uma rotina de vendas funcionando.",
    "observacoes": "Atendo principalmente prestadores de serviço.",
}
MCC_CONSENT: dict[str, Any] = {"privacy": True, "text_version": "mcc-v1"}


@pytest.fixture
def client(deps: Deps) -> TestClient:
    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    return TestClient(create_app(trigger=fake_trigger))


@pytest.fixture
def real_pdf(deps: Deps) -> Deps:
    deps.pdf = build_pdf(Settings(_env_file=None, PDF_MODE="weasyprint"))  # type: ignore[call-arg]
    return deps


def send_mcc(client: TestClient, answers: dict[str, Any] | None = None) -> httpx.Response:
    return client.post(
        "/v1/forms/mcc/submissions",
        json={"answers": answers or MCC_ANSWERS, "consent": MCC_CONSENT},
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


def test_mcc_sent_through_the_api_becomes_a_multi_page_pdf_with_accents(
    client: TestClient, real_pdf: Deps
) -> None:
    sent = send_mcc(client)
    assert sent.status_code == 202
    body = sent.json()

    run_pipeline(real_pdf, body["public_id"])

    pdf = client.get(f"/v1/jobs/{body['public_id']}/pdf", params={"t": body["token"]})
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pages_of(pdf.content) >= 6
    text = squash(text_of(pdf.content))
    # Do template e do plano de exemplo (saída real do LLM do legado), com acentos e cedilha.
    for accented in ("Diagnóstico", "Ações", "Orientação", "atenção"):
        assert accented in text, accented


INTERNAL = (
    # pontuação e persona (ficam na submissão e no webhook)
    "lead_score",
    "Fit",
    "Perfil de calendário",
    "Perfil de apoio",
    "Perfil de participação",
    "Perfil de atividades",
    "Perfil de organização",
    "Perfil de ampliação",
    # contato e versões
    "contadora.mcc@example.test",
    "99876",
    "mcc-v4.1",
    "mcc-2026.10",
    "pdf.html",
    "fake",
    "UTC",
    "Não informado",
)


def test_pdf_follows_the_v2_page_map_in_reading_order_without_internal_data(
    client: TestClient, real_pdf: Deps
) -> None:
    body = send_mcc(client).json()
    run_pipeline(real_pdf, body["public_id"])
    pdf = client.get(f"/v1/jobs/{body['public_id']}/pdf", params={"t": body["token"]}).content
    pages = pages_of(pdf)
    shown = squash(text_of(pdf)).casefold()
    assert pages >= 3
    headings = [
        "Diagnóstico do projeto",
        "Orientação do projeto",
        "Fases",
        "Pontos de atenção",
        "Dificuldades na execução",
        "Próximos aprendizados",
    ]
    positions = [shown.find(squash(heading).casefold()) for heading in headings]
    assert -1 not in positions and positions == sorted(positions)
    assert "Página 2 de" in text_of(pdf)
    for internal in INTERNAL:
        assert internal not in text_of(pdf)
    fonts = fonts_of(pdf)
    assert fonts and all(fonts.values())
    assert all(name.startswith(("SourceSansPro", "SourceSerifPro")) for name in fonts)


@pytest.mark.parametrize("field", ["endereco", "data_nascimento", "estado_civil", "filhos"])
def test_personal_data_the_plan_does_not_use_is_refused(client: TestClient, field: str) -> None:
    response = send_mcc(client, {**MCC_ANSWERS, field: "Rua Secreta, 123"})

    assert response.status_code == 422
    assert "Rua Secreta" not in response.text


def test_api_accepts_one_source_as_text_and_blank_optional_answers(client: TestClient) -> None:
    """Formatos que formulários HTML mandam: uma caixa marcada vira texto; opcional vazio, ""."""
    answers = {
        **MCC_ANSWERS,
        "fontes_aquisicao": "indicacao",
        "whatsapp": "",
        "faixa_custo_mensal": "",
        "observacoes": "  ",
    }

    assert send_mcc(client, answers).status_code == 202


# --- página pública ---------------------------------------------------------------------------

STEP_TITLES = (
    "Vamos começar por você",
    "Seu escritório",
    "Sua equipe",
    "Seus clientes e vendas",
    "Números do negócio",
    "Maturidade do escritório",
    "Operação",
    "Comercial e estratégia",
    "Risco e marketing",
    "Suas prioridades",
    "Detalhes (opcional)",
)


def step_headings(html: str) -> list[str]:
    return re.findall(r'<h2 id="etapa-\d+">(.*?)</h2>', html, flags=re.DOTALL)


def test_mcc_form_page_has_ten_steps_plus_optional_details_and_whatsapp_consent(
    client: TestClient,
) -> None:
    response = client.get("/p/mcc")

    assert response.status_code == 200
    html = response.text
    titles = [" ".join(page_text(f"<h2>{title}</h2>").split()) for title in step_headings(html)]
    assert titles == list(STEP_TITLES)
    shown = page_text(html)
    for label in (
        "WhatsApp (opcional)",
        "Quantos colaboradores o escritório tem hoje?",
        "De onde vêm seus clientes hoje?",
        "Qual percentual do faturamento depende de você?",
        "Entre 50% e 80%",
        "Qual é o principal gargalo do escritório hoje?",
        "Prefiro não informar",
    ):
        assert label in shown, label
    for removed in ("Endereço", "nascimento", "Estado civil", "filhos"):
        assert removed not in shown, removed
    (whatsapp,) = tags(html, "input", name="whatsapp")
    assert whatsapp["type"] == "tel" and "required" not in whatsapp
    assert len(tags(html, "input", name="fontes_aquisicao", type="checkbox")) == 6
    (privacy,) = tags(html, "input", name="_privacy")
    assert "required" in privacy and "checked" not in privacy
    for channel in ("_mkt_email", "_mkt_whatsapp"):
        (box,) = tags(html, "input", name=channel)
        assert "required" not in box and "checked" not in box
    assert tags(html, "input", name="_consent_version")[0]["value"] == "mcc-v1"
    drafted = {f["name"] for f in tags(html, "input") + tags(html, "textarea") if "data-draft" in f}
    for contact in ("nome", "email", "whatsapp", "nome_escritorio", "cidade_uf", "instagram"):
        assert contact not in drafted, contact
    assert "objetivo_principal" in drafted
    assert_private(response)


def form_data(**changes: str | list[str] | None) -> dict[str, Any]:
    """O que o navegador envia: respostas pelo nome do campo e os campos de controle."""
    data: dict[str, Any] = {
        **MCC_ANSWERS,
        "_privacy": "1",
        "_mkt_whatsapp": "1",
        "_consent_version": "mcc-v1",
        "_idem": "chave-mcc",
    }
    data.update(changes)
    return {key: value for key, value in data.items() if value is not None}


def test_mcc_sent_from_the_public_page_ends_with_a_preview_and_the_pdf(
    client: TestClient, real_pdf: Deps
) -> None:
    sent = client.post("/p/mcc", data=form_data(), follow_redirects=False)
    assert sent.status_code == 303
    page = sent.headers["location"]

    run_pipeline(real_pdf, page.rsplit("/", 1)[1])

    result = client.get(page)
    assert result.status_code == 200
    shown = page_text(result.text)
    assert "Seu plano está pronto, Contadora" in shown
    for preview in (
        MCC.example_plan["titulo"],
        "Diagnóstico do projeto",
        "Recursos para experimentar",
        "não substitui consultoria",
    ):
        assert preview in shown, preview
    for internal in INTERNAL:
        assert internal not in shown, internal
    pdf = client.get(f"{page}/pdf")
    assert pdf.status_code == 200 and pages_of(pdf.content) >= 6


def test_invalid_whatsapp_comes_back_with_a_field_error_without_echoing_it(
    client: TestClient,
) -> None:
    response = client.post("/p/mcc", data=form_data(whatsapp="liga pra mim"))

    assert response.status_code == 422
    assert "liga pra mim" not in response.text
    assert "Informe o DDD e o número, por exemplo (11) 91234-5678" in page_text(response.text)
    assert tags(response.text, "input", name="whatsapp")[0]["aria-invalid"] == "true"


# Esperados medidos no JS legado (nó "Calcular Scores e Clusters" do n8n, rodado por
# `reports/review/2026-10-07-refatoracao-python-fastapi-hatchet/esbocos/legacy/run_scoring.js`
# sobre o `corpo_legado` de cada caso: opções pelo rótulo e um número dentro de cada faixa).
SCORING_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "mcc_scoring_cases.json").read_text(encoding="utf-8")
)


def test_webhook_event_carries_the_whatsapp_the_legacy_score_and_the_plan(
    client: TestClient, deps: Deps
) -> None:
    webhook = record_webhook(deps)
    case = next(case for case in SCORING_CASES if case["caso"] == "p1_reforma")
    body = send_mcc(client, {**case["respostas"], "whatsapp": "(19) 99876-5432"}).json()

    (delivery,) = run_pipeline(deps, body["public_id"])
    pipeline.deliver(deps, delivery)

    (_, _, payload) = webhook.calls[0]
    assert payload["lead"]["phone"] == "19998765432"
    assert payload["priority"] == "P1"  # "P1 - ação imediata", medido no legado (p1_reforma)
    assert payload["persona"] == "Perfil de calendário"
    assert payload["scores"]["lead_score"] == 72 and payload["scores"]["fit_score"] == 79
    assert len(payload["plan"]["fases"]) == 3


# --- o que vai para a OpenAI (SDK real sobre transporte simulado) -------------------------------


def sent_prompt(answers: dict[str, Any]) -> str:
    """Prompt que o adaptador da OpenAI manda ao provedor com estas respostas."""
    server = FakeOpenAI(reply(MCC.example_plan))
    generator(server).generate(MCC, MCC.answers_model.model_validate(answers))
    (message,) = server.bodies[0]["input"]
    return message["content"]


def test_prompt_sent_to_openai_has_option_labels_catalog_and_edition_but_no_contact_data() -> None:
    today = [datetime.now(SAO_PAULO).strftime("%d/%m/%Y")]
    prompt = sent_prompt(MCC_ANSWERS)
    today.append(datetime.now(SAO_PAULO).strftime("%d/%m/%Y"))  # virada do dia no meio

    for contact in (
        "Contadora Exemplo",
        "contadora.mcc@example.test",
        "Contadora.MCC@Example.test",
        "99876",
        "@escritorio.exemplo",
        "Escritório Exemplo Contabilidade",
        "Cidade Exemplo",
    ):
        assert contact not in prompt, contact
    # Rótulos das opções escolhidas, nunca os valores estáveis.
    for label in (
        "Mais de 80%",
        "Tenho faturamento, mas quero aumentar lucro e ganhar mais tempo",
        "Indicação, Instagram",
        "Abaixo de R$ 20 mil",
        "Adaptação à Calendário do projeto",
        "Até R$ 10 mil",
    ):
        assert label in prompt, label
    for value in ("mais_de_80", "lucro_e_tempo", "abaixo_de_20_mil", "ate_10_mil", "indicacao"):
        assert value not in prompt, value
    assert f"«{MCC_ANSWERS['objetivo_principal']}»" in prompt
    assert f"«{MCC_ANSWERS['criterio_de_sucesso']}»" in prompt
    # O catálogo que o plano é obrigado a usar: cada aula e cada entregável numa linha.
    catalog = MCC.templates.data  # type: ignore[union-attr]
    for name in [
        *catalog["aulas"],
        *catalog["combo"],
        *catalog["ecossistema"],
        *catalog["materiais"],
    ]:
        assert f"\n- {name}\n" in prompt, name
    # Edição de teste configurada no formulário e a data de hoje em São Paulo.
    assert "Ciclo demonstrativo 2027" in prompt and "31/05/2027" in prompt
    assert "29/05/2026" not in prompt and "29 de maio" not in prompt
    assert any(f"Data atual de referência: {day}" in prompt for day in today)
    assert "{{" not in prompt and "{%" not in prompt


def test_unanswered_optional_details_are_left_out_of_the_prompt() -> None:
    answers = {
        key: value
        for key, value in MCC_ANSWERS.items()
        if key not in ("criterio_de_sucesso", "observacoes", "faixa_inadimplencia")
    }
    answers["faixa_custo_mensal"] = "prefiro_nao_informar"
    prompt = sent_prompt(answers)
    assert MCC_ANSWERS["criterio_de_sucesso"] not in prompt
    assert MCC_ANSWERS["observacoes"] not in prompt
    assert "None" not in prompt and "Prefiro não informar" not in prompt


def test_real_legacy_llm_output_is_accepted_at_once_under_a_strict_schema() -> None:
    """O plano fictício respeita o schema e as opções fechadas, sem envelope de identidade."""
    server = FakeOpenAI(reply(MCC.example_plan))

    result = generator(server).generate(MCC, MCC.answers_model.model_validate(MCC_ANSWERS))

    assert result.usage["repairs"] == 0
    assert [len(fase["acoes"]) for fase in result.document["fases"]] == [5, 5, 5]
    # A aula citada com ponto final no legado entra no plano de exemplo com o nome exato do catálogo.
    assert result.document["fases"][2]["aulas_recomendadas"][4] == ("Recurso aulas 41")
    nodes = list(schema_nodes(server.bodies[0]["text"]["format"]["schema"]))
    for node in nodes:
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])
    assert not [node for node in nodes if NOT_FOR_STRICT_MODE & node.keys()]
    assert not [node for node in nodes if "$ref" in node and len(node) > 1]


def test_lessons_and_materials_outside_the_catalog_get_one_repair_round() -> None:
    invented = copy.deepcopy(MCC.example_plan)
    invented["fases"][1]["aulas_recomendadas"][0] = "MÓDULO 9 — A1: Aula Que Não Existe"
    invented["fases"][2]["entregaveis_irpf"][0] = "Kit Inventado de IRPF"
    server = FakeOpenAI(reply(invented), reply(MCC.example_plan))

    result = generator(server).generate(MCC, MCC.answers_model.model_validate(MCC_ANSWERS))

    feedback = server.bodies[1]["input"][-1]["content"]
    assert "fases.1.aulas_recomendadas.0" in feedback
    assert "fases.2.entregaveis_irpf.0" in feedback
    assert "Aula Que Não Existe" not in feedback and "Kit Inventado" not in feedback
    assert result.usage["repairs"] == 1


def test_catalog_names_are_not_corrected_by_approximation_but_sent_back_for_repair() -> None:
    """O catálogo é uma lista fechada: o nome com outro traço, outra caixa ou sem acento, que o
    código corrigia por aproximação, volta para a rodada de reparo (Further Notes da spec)."""
    variant = copy.deepcopy(MCC.example_plan)
    variant["fases"][0]["aulas_recomendadas"][0] = (
        "modulo 2 - a1: como desenhar a proposta unica de valor do seu escritorio."
    )
    server = FakeOpenAI(reply(variant), reply(MCC.example_plan))

    result = generator(server).generate(MCC, MCC.answers_model.model_validate(MCC_ANSWERS))

    feedback = server.bodies[1]["input"][-1]["content"]
    assert "fases.0.aulas_recomendadas.0" in feedback and "modulo 2" not in feedback
    assert result.usage["repairs"] == 1
    assert result.document["fases"][0]["aulas_recomendadas"][0] == ("Recurso aulas 5")
