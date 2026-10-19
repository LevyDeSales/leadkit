"""Cenários declarativos fictícios com snapshots próprios de prompt, schema e PDF.

Os identificadores históricos são mantidos para compatibilidade técnica dos testes. Os aceites
cobrem calendário diário, regras de validação, versões imutáveis e processamento até webhook,
sem carregar conteúdo de uma instalação privada."""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from conftest import DEMO_ANSWERS, legacy_definition, legacy_spec, record_webhook
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_public import text as page_text
from test_weasyprint_pdf import fonts_of, pages_of, text_of

from app import jobs, pipeline
from app.adapters.openai_llm import strict_schema
from app.adapters.weasyprint_pdf import WeasyPrintRenderer
from app.api.main import create_app
from app.deps import Deps
from app.forms import FormSpec
from app.forms.definition import compile_definition
from app.forms.definition.changes import llm_changes
from app.forms.stored import as_stored, contact_in_prompt, publication_problems
from app.models import Job, Lead, Plan, Submission, utcnow
from app.tokens import new_public_id

GOLDEN = Path(__file__).parent / "fixtures" / "legacy_golden"
FIXTURES = GOLDEN.parent
FORMS = ("demo", "mcc", "reforma-2027")
MOMENT = datetime(2026, 10, 8, 12, tzinfo=UTC)
CLOCK = [MOMENT]
SPECS: dict[str, FormSpec] = {slug: legacy_spec(slug, lambda: CLOCK[0]) for slug in FORMS}
MCC_CASES = json.loads((FIXTURES / "mcc_scoring_cases.json").read_text(encoding="utf-8"))
REFORMA_PROFILES = json.loads((FIXTURES / "reforma_2027_perfis.json").read_text(encoding="utf-8"))
CALENDAR = json.loads((GOLDEN / "calendar-reforma-2027.json").read_text(encoding="utf-8"))
P1_MCC = next(case for case in MCC_CASES if case["caso"] == "p1_reforma")["respostas"]
OPTIONAL_MCC = (
    "whatsapp",
    "nome_escritorio",
    "faixa_custo_mensal",
    "faixa_inadimplencia",
    "criterio_de_sucesso",
    "observacoes",
    "instagram",
)


def valid(slug: str, answers: dict[str, Any]) -> Any:
    return SPECS[slug].answers_model.model_validate(answers)


# --- prompt montado -----------------------------------------------------------------------------

# (arquivo, formulário, respostas): o texto que o código montava em 8/10/2026.
PROMPTS = [
    ("prompt-demo-completo.txt", "demo", DEMO_ANSWERS),
    (
        "prompt-demo-sem-escritorio.txt",
        "demo",
        {key: value for key, value in DEMO_ANSWERS.items() if key != "escritorio"},
    ),
    ("prompt-mcc-p1-reforma.txt", "mcc", P1_MCC),
    (
        "prompt-mcc-so-obrigatorias.txt",
        "mcc",
        {key: value for key, value in P1_MCC.items() if key not in OPTIONAL_MCC},
    ),
    ("prompt-reforma-2027-perfil-0.txt", "reforma-2027", REFORMA_PROFILES[0]["respostas"]),
    ("prompt-reforma-2027-perfil-3.txt", "reforma-2027", REFORMA_PROFILES[3]["respostas"]),
]


def without_situation(prompt: str) -> str:
    """O prompt sem a frase de situação de cada fase do reforma-2027: o motor a escreve (uma
    frase por janela), e o código tinha uma frase escrita à mão. O resto do calendário fica: a
    fase, o período e os tipos de ação que cabem."""
    return re.sub(
        r"^(- Fase \d: [^\n]*?\d{4}\.) .*? (Tipos de ação que cabem: [^\n]*)$",
        r"\1 <situação> \2",
        prompt,
        flags=re.MULTILINE,
    )


@pytest.mark.parametrize(("name", "slug", "answers"), PROMPTS, ids=[p[0] for p in PROMPTS])
def test_the_prompt_is_the_text_the_form_in_code_assembled(
    name: str, slug: str, answers: dict[str, Any]
) -> None:
    CLOCK[0] = MOMENT
    expected = (GOLDEN / name).read_text(encoding="utf-8")

    prompt = SPECS[slug].render_prompt(valid(slug, answers))

    assert (without_situation(prompt) if slug == "reforma-2027" else prompt) == expected


def test_the_reforma_prompt_tells_the_situation_of_each_window_in_every_phase() -> None:
    """A frase de situação vem do motor (ticket 11): uma por janela, com o rótulo dela."""
    CLOCK[0] = MOMENT

    prompt = SPECS["reforma-2027"].render_prompt(
        valid("reforma-2027", REFORMA_PROFILES[0]["respostas"])
    )

    assert (
        "- Fase 1: 8 de outubro a 6 de novembro de 2026. Inscrição no projeto: está aberta e "
        "fecha nesta fase. Revisão da inscrição: abre nesta fase. Tipos de ação que cabem: "
        "decisao_da_opcao, desistencia, proxima_janela, outra."
    ) in prompt
    assert "Inscrição no projeto: já fechou. Revisão da inscrição: está aberta durante" in prompt


# --- schema estrito -----------------------------------------------------------------------------


def without_descriptions(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: without_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [without_descriptions(item) for item in node]
    return node


def descriptions(node: Any, path: str = "") -> Iterator[tuple[str, str | None]]:
    """A descrição de cada nó do schema, com o caminho dele, na ordem do schema."""
    if isinstance(node, dict):
        yield path, node.get("description")
        for key, value in node.items():
            if key != "description":
                yield from descriptions(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from descriptions(item, f"{path}[{index}]")


def golden_schema(slug: str) -> Any:
    return json.loads((GOLDEN / f"schema-{slug}.json").read_text(encoding="utf-8"))


def expected_strict_schema(slug: str) -> Any:
    """Schema público congelado, com ordem e enumeração dos recursos fictícios."""
    return without_descriptions(golden_schema(slug))


@pytest.mark.parametrize("slug", FORMS)
def test_the_strict_schema_has_the_structure_and_order_of_the_form_in_code(slug: str) -> None:
    """As descrições mudam (o compilador escreve os limites nelas); o resto é igual, inclusive a
    ordem das chaves, que é a ordem em que o LLM escreve."""
    schema = strict_schema(SPECS[slug].plan_model)

    assert json.dumps(without_descriptions(schema)) == json.dumps(expected_strict_schema(slug))


LIMIT = (
    r"(?:Entre \d+ e \d+ (?:caracteres|itens)|Até \d+ (?:caractere|caracteres|item|itens)"
    r"|Exatamente \d+ (?:item|itens|caractere|caracteres)"
    r"|Pelo menos \d+ (?:caractere|caracteres|item|itens|parágrafo|parágrafos)"
    r"(?:, separados por linha em branco)?)\."
)


@pytest.mark.parametrize("slug", FORMS)
def test_each_description_keeps_the_text_of_the_code_and_only_adds_the_limits(slug: str) -> None:
    expected = dict(descriptions(golden_schema(slug)))
    actual = dict(descriptions(strict_schema(SPECS[slug].plan_model)))
    assert actual == expected
    assert actual and any(value for value in actual.values())


# --- PDF ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("slug", FORMS)
def test_the_pdf_of_the_example_plan_has_the_pages_fonts_and_text_of_the_form_in_code(
    slug: str,
) -> None:
    spec = SPECS[slug]
    expected = json.loads((GOLDEN / f"pdf-{slug}.json").read_text(encoding="utf-8"))
    answers = valid(slug, legacy_definition(slug)["examples"]["answers"])
    scores = spec.score(answers).scores if spec.score else {}
    context = {
        "lead": {"name": "Contadora Exemplo"},
        "form": {"slug": slug, "title": spec.title},
        "scores": scores,
        "generated_at": datetime(2026, 10, 8, 2, 30, tzinfo=UTC),
    }

    pdf = WeasyPrintRenderer().render(spec, spec.example_plan, context)

    assert pdf.page_count == pages_of(pdf.content) == expected["pages"]
    assert fonts_of(pdf.content) == expected["fonts"]
    assert all(fonts_of(pdf.content).values())  # embutidas
    assert text_of(pdf.content) == expected["text"]


# --- calendário do reforma-2027 -----------------------------------------------------------------

KINDS = ("decisao_da_opcao", "desistencia", "proxima_janela", "outra")


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)


def neutral(*kinds: tuple[int, int, str]) -> dict[str, Any]:
    """O plano de exemplo com títulos e textos neutros e os tipos dados: `(fase, ação, tipo)`."""
    plan = copy.deepcopy(SPECS["reforma-2027"].example_plan)
    for fase in plan["fases"]:
        for acao in fase["acoes"]:
            acao["titulo"] = "Ajustar a rotina do escritório"
            acao["como_fazer"] = "Registre o resultado numa planilha simples do escritório."
    for phase, action, kind in kinds:
        plan["fases"][phase]["acoes"][action]["tipo"] = kind
    return plan


def uniform(kind: str) -> dict[str, Any]:
    """O plano neutro com todas as ações do mesmo tipo."""
    return neutral(
        *[
            (phase, action, kind)
            for phase, fase in enumerate(SPECS["reforma-2027"].example_plan["fases"])
            for action in range(len(fase["acoes"]))
        ]
    )


def refusals(plan: dict[str, Any], day: date) -> list[str]:
    spec, CLOCK[0] = SPECS["reforma-2027"], at(day)
    try:
        spec.validate_plan(
            json.dumps(plan), valid("reforma-2027", REFORMA_PROFILES[0]["respostas"])
        )
    except ValidationError as exc:
        return [item["msg"] for item in exc.errors(include_input=False)]
    return []


def spots(messages: list[str]) -> list[list[int]]:
    """Onde cada mensagem recusa: a fase e a ação."""
    found = set()
    for message in messages:
        head = message.split(" (fase ")[0]
        found |= {(int(p), int(a)) for p, a in re.findall(r"fases\.(\d)\.acoes\.(\d)", head)}
    return sorted([phase, action] for phase, action in found)


REFERENCE_DAYS = list(CALENDAR["reference_days"])


@pytest.mark.parametrize("day", REFERENCE_DAYS)
def test_the_reference_days_give_the_periods_and_allowed_types_of_the_form_in_code(
    day: str,
) -> None:
    CLOCK[0] = at(date.fromisoformat(day))
    assert SPECS["reforma-2027"].templates is not None

    phases = SPECS["reforma-2027"].templates.phases()

    expected = CALENDAR["reference_days"][day]["phases"]
    assert [phase.period for phase in phases] == [phase["period"] for phase in expected]
    assert [list(phase.allowed["tipo"]) for phase in phases] == [p["allowed"] for p in expected]
    assert [phase.windows["opcao"].open for phase in phases] == [p["option_open"] for p in expected]
    assert [phase.windows["desistencia"].open for phase in phases] == [
        p["withdrawal_open"] for p in expected
    ]


def test_every_day_of_the_calendar_gives_the_allowed_types_of_the_form_in_code() -> None:
    """De 27/08/2026 a 31/01/2027. Antes de 27/08 a diferença é de modelagem: o código tratava a
    opção como aberta desde sempre, e a janela da definição começa em 25/09/2026, o dia do DOU."""
    assert SPECS["reforma-2027"].templates is not None
    for day, expected in CALENDAR["allowed_by_day"].items():
        CLOCK[0] = at(date.fromisoformat(day))
        phases = SPECS["reforma-2027"].templates.phases()
        assert [list(phase.allowed["tipo"]) for phase in phases] == expected, day


@pytest.mark.parametrize("day", REFERENCE_DAYS)
def test_the_reference_days_refuse_the_actions_the_form_in_code_refused(day: str) -> None:
    for kind in KINDS:
        found = spots(refusals(uniform(kind), date.fromisoformat(day)))

        assert found == CALENDAR["reference_days"][day]["refused"][kind], (day, kind)


def test_the_refusals_come_grouped_by_value_and_phase() -> None:
    messages = refusals(uniform("decisao_da_opcao"), date(2026, 11, 1))  # a opção já fechou

    assert [message.split(": ")[0] for message in messages] == [
        "fases.0.acoes.0.tipo, fases.0.acoes.1.tipo, fases.0.acoes.2.tipo (fase 1)",
        "fases.1.acoes.0.tipo, fases.1.acoes.1.tipo (fase 2)",
        "fases.2.acoes.0.tipo, fases.2.acoes.1.tipo (fase 3)",
    ]


def test_the_simulation_rule_is_gone() -> None:
    """O código barrava uma simulação de clientes marcada como `outra` depois do fim da opção. A
    regra sai (Further Notes da spec): a definição só confere o valor pela fase."""
    plan = neutral()
    plan["fases"][1]["acoes"][1] = {
        "tipo": "outra",
        "titulo": "Simular cenários prioritários",
        "como_fazer": "Para os clientes mais relevantes, simule os cenários com dados reais.",
    }

    assert refusals(plan, date(2026, 10, 8)) == []


def test_the_repair_messages_never_carry_the_text_of_the_plan() -> None:
    secret = "Ajustar a rotina do escritório"

    messages = refusals(uniform("decisao_da_opcao"), date(2026, 11, 1))

    assert messages and all(secret not in message for message in messages)


def test_the_example_plan_passes_on_the_day_it_was_written() -> None:
    assert refusals(SPECS["reforma-2027"].example_plan, date(2026, 10, 8)) == []


def test_the_windows_are_the_dates_of_the_data_file_of_the_form_in_code() -> None:
    """`janela.toml` (Regulamento fictício do projeto): a opção fecha em 30/10/2026 e a desistência vai de
    03/11 a 20/12; a opção abriu no DOU, em 25/09/2026."""
    data = SPECS["reforma-2027"].templates.data  # type: ignore[union-attr]

    assert data["janelas"]["opcao"]["start"] == date(2026, 9, 25)
    assert data["janelas"]["opcao"]["end"] == date(2026, 10, 30)
    assert data["janelas"]["desistencia"]["start"] == date(2026, 11, 3)
    assert data["janelas"]["desistencia"]["end"] == date(2026, 12, 20)
    assert data["opcao"] == {
        "referencia": "o 1º semestre de 2027",
        "fonte": "Regulamento fictício do projeto",
        "revisado_em": "2026-10-08",
    }


# --- dados do formulário ------------------------------------------------------------------------


def test_the_mcc_catalog_has_the_names_the_form_in_code_listed() -> None:
    data = SPECS["mcc"].templates.data  # type: ignore[union-attr]

    assert {kind: len(data[kind]) for kind in ("aulas", "combo", "ecossistema", "materiais")} == {
        "aulas": 44,
        "combo": 11,
        "ecossistema": 25,
        "materiais": 8,
    }
    assert len(data["estruturantes"]) == 6
    # O campo dos entregáveis aceita os três grupos juntos, numa lista só dos dados.
    assert data["entregaveis_irpf"] == [*data["combo"], *data["ecossistema"], *data["materiais"]]
    assert data["edicao"]["name"] == "Ciclo demonstrativo 2027"
    assert data["edicao"]["ends"] == date(2027, 5, 31)


# --- a versão 2 do MCC, sem a lista que repete os entregáveis (issue 49) ---------------------------

UNIONED = ["combo", "ecossistema", "materiais"]


def mcc_v2() -> dict[str, Any]:
    """A definição da versão 2 do MCC como a operadora a monta no admin: o `entregaveis_irpf` do
    plano tira as opções das três listas que o prompt mostra, e o bloco que as repetia sai dos
    dados. A versão 1 (a que a migration `0008` leva ao banco) não muda."""
    document = legacy_definition("mcc")
    field = document["plan"]["fields"][3]["items"]["fields"][5]["items"]
    assert field == {"kind": "options", "values_from": "entregaveis_irpf"}
    field["values_from"] = list(UNIONED)
    del document["data"]["entregaveis_irpf"]
    return document


def mcc_v2_spec() -> FormSpec:
    return compile_definition(mcc_v2(), clock=lambda: CLOCK[0], version_number=2)


def test_the_mcc_v2_has_no_block_that_repeats_the_deliverables_and_the_v1_keeps_it() -> None:
    assert "entregaveis_irpf" not in mcc_v2()["data"]
    assert "entregaveis_irpf" in legacy_definition("mcc")["data"]  # a versão publicada não muda


def test_the_mcc_v2_gives_the_same_strict_schema_as_the_v1() -> None:
    """A união das três listas, na ordem, é a lista que a v1 repetia: a lista de entregáveis (que
    vai na descrição, por causa das aspas duplas de um deles), as outras descrições e a ordem das
    chaves não mudam."""
    v1, v2 = strict_schema(SPECS["mcc"].plan_model), strict_schema(mcc_v2_spec().plan_model)

    assert json.dumps(v2) == json.dumps(v1)
    assert json.dumps(without_descriptions(v2)) == json.dumps(expected_strict_schema("mcc"))
    items = v2["$defs"]["Phase"]["properties"]["entregaveis_irpf"]["items"]
    data = legacy_definition("mcc")["data"]
    union = [item for name in UNIONED for item in data[name]["items"]]
    assert "enum" not in items
    assert items["description"] == f"Valores permitidos: {'; '.join(union)}."


@pytest.mark.parametrize("name", ["prompt-mcc-p1-reforma.txt", "prompt-mcc-so-obrigatorias.txt"])
def test_the_mcc_v2_assembles_the_prompts_of_the_golden_files(name: str) -> None:
    CLOCK[0] = MOMENT
    answers = next(answers for file, slug, answers in PROMPTS if file == name and slug == "mcc")
    spec = mcc_v2_spec()

    prompt = spec.render_prompt(spec.answers_model.model_validate(answers))

    assert prompt == (GOLDEN / name).read_text(encoding="utf-8")


def test_the_mcc_v2_validates_the_same_plans_as_the_v1() -> None:
    v1, v2 = SPECS["mcc"], mcc_v2_spec()
    example = legacy_definition("mcc")["examples"]["plan"]
    elsewhere = edited("mcc", lambda plan: mcc_entregavel(plan, "Um entregável que não existe"))
    swapped = edited(
        "mcc",
        lambda plan: mcc_entregavel(
            plan, legacy_definition("mcc")["data"]["materiais"]["items"][0]
        ),
    )

    assert v2.example_plan == v1.example_plan == example
    for plan in (example, elsewhere, swapped):
        assert accepts(v2, plan) == accepts(v1, plan)
    assert accepts(v2, swapped) and not accepts(v2, elsewhere)


def test_publishing_the_mcc_v2_asks_for_a_test_with_the_llm() -> None:
    """O `enum` do schema é o mesmo, mas o modelo do plano e os dados que ele usa mudaram na
    definição: a publicação pede o teste com o LLM do rascunho, como para qualquer mudança dessas."""
    changes = llm_changes(legacy_definition("mcc"), mcc_v2())

    assert changes == (
        "o modelo do plano",
        "os dados do formulário que o prompt lê ou que as fases e o modelo do plano usam",
    )


# --- validação do plano -------------------------------------------------------------------------


def accepts(spec: FormSpec, plan: dict[str, Any]) -> bool:
    try:
        spec.plan_model.model_validate(plan)
    except ValidationError:
        return False
    return True


def edited(slug: str, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    plan = copy.deepcopy(legacy_definition(slug)["examples"]["plan"])
    change(plan)
    return plan


def mcc_ganho(plan: dict[str, Any], text: str) -> None:
    plan["diagnostico_estrategico"]["ganhos_esperados"][0] = text


def mcc_aula(plan: dict[str, Any], name: str) -> None:
    plan["fases"][0]["aulas_recomendadas"][0] = name


def mcc_entregavel(plan: dict[str, Any], name: str) -> None:
    plan["fases"][0]["entregaveis_irpf"][0] = name


def mcc_final(plan: dict[str, Any], paragraphs: int) -> None:
    plan["nota_final_maturidade"]["mensagem_final"] = "\n\n".join(["Um parágrafo."] * paragraphs)


def mcc_short_ganhos(plan: dict[str, Any]) -> None:
    del plan["diagnostico_estrategico"]["ganhos_esperados"][3:]


def reforma_text(plan: dict[str, Any], text: str) -> None:
    plan["resumo"] = f"{plan['resumo']} {text}"


def reforma_phase_name(plan: dict[str, Any], name: str) -> None:
    plan["fases"][1]["nome"] = name


def reforma_how(plan: dict[str, Any], text: str) -> None:
    plan["fases"][0]["acoes"][0]["como_fazer"] = f"Organize a carteira: {text} e registre."


def reforma_planning(plan: dict[str, Any], text: str) -> None:
    plan["uso_da_janela"] = f"{plan['uso_da_janela']} {text}"


def reforma_fifth_action(plan: dict[str, Any]) -> None:
    actions = plan["fases"][0]["acoes"]
    actions += [copy.deepcopy(actions[0]) for _ in range(5 - len(actions))]


# (formulário, nome, como alterar o plano de exemplo, o código aceitava?, a definição aceita?). A
# coluna do código foi conferida no PR da paridade, contra o código; hoje vale a da definição.
PLAN_CHECKS: list[tuple[str, str, Callable[[dict[str, Any]], None], bool, bool]] = [
    ("mcc", "o exemplo", lambda plan: None, True, True),
    ("mcc", "ganho em reais", lambda p: mcc_ganho(p, "Faturar R$ 10 mil a mais"), False, False),
    ("mcc", "ganho em percentual", lambda p: mcc_ganho(p, "Subir a margem 20%"), False, False),
    ("mcc", "ganho garantido", lambda p: mcc_ganho(p, "Resultado garantido"), False, False),
    ("mcc", "nota final com 2 parágrafos", lambda p: mcc_final(p, 2), False, False),
    ("mcc", "nota final com 3 parágrafos", lambda p: mcc_final(p, 3), True, True),
    ("mcc", "3 ganhos (mínimo 4)", mcc_short_ganhos, False, False),
    ("mcc", "2 fases", lambda p: p["fases"].pop(), False, False),
    (
        "mcc",
        "ação com 221 caracteres",
        lambda p: p["fases"][0]["acoes"].__setitem__(0, "x" * 221),
        False,
        False,
    ),
    ("mcc", "campo a mais", lambda p: p.update(extra="x"), False, False),
    (
        "mcc",
        "aula que não existe",
        lambda p: mcc_aula(p, "MÓDULO 9 — A1: Não existe"),
        False,
        False,
    ),
    # Aceito pelo dono: o nome do catálogo deixa de ser corrigido por aproximação; o LLM escolhe
    # de uma lista fechada, e o nome fora dela vai para a rodada de reparo.
    (
        "mcc",
        "aula com outra caixa",
        lambda p: mcc_aula(p, p["fases"][0]["aulas_recomendadas"][0].upper()),
        True,
        False,
    ),
    ("reforma-2027", "o exemplo", lambda plan: None, True, True),
    (
        "reforma-2027",
        "economia garantida",
        lambda p: reforma_text(p, "Há economia garantida."),
        False,
        False,
    ),
    (
        "reforma-2027",
        "garantia",
        lambda p: reforma_text(p, "Com a garantia do método."),
        False,
        False,
    ),
    (
        "reforma-2027",
        "garantido",
        lambda p: reforma_text(p, "O resultado é garantido."),
        False,
        False,
    ),
    ("reforma-2027", "sem juros", lambda p: reforma_text(p, "Pague sem  JUROS."), False, False),
    (
        "reforma-2027",
        "única IA do mercado",
        lambda p: reforma_text(p, "A única IA do mercado."),
        False,
        False,
    ),
    (
        "reforma-2027",
        "calculadora definitiva",
        lambda p: reforma_text(p, "Calculadora Definitiva."),
        False,
        False,
    ),
    # A expressão proibida é um texto procurado em qualquer parte: o plural também cai (a regra
    # antiga era uma expressão regular com limite de palavra, que deixava passar "garantias").
    (
        "reforma-2027",
        "garantias",
        lambda p: reforma_text(p, "Peça as garantias legais."),
        True,
        False,
    ),
    (
        "reforma-2027",
        "nome interno no texto",
        lambda p: reforma_how(p, "decisao_da_opcao"),
        False,
        False,
    ),
    (
        "reforma-2027",
        "nome interno no planejamento",
        lambda p: reforma_planning(p, "proxima_janela"),
        True,
        True,
    ),
    ("reforma-2027", "fase com número", lambda p: reforma_phase_name(p, "Fase 2"), False, False),
    (
        "reforma-2027",
        "fase com dias",
        lambda p: reforma_phase_name(p, "Dias 31 a 60"),
        False,
        False,
    ),
    ("reforma-2027", "fase com etapa", lambda p: reforma_phase_name(p, "Etapa 3"), False, False),
    ("reforma-2027", "fase com ano", lambda p: reforma_phase_name(p, "Virada de 2027"), True, True),
    ("reforma-2027", "título curto", lambda p: p.update(titulo="Curto"), False, False),
    ("reforma-2027", "5 ações numa fase", reforma_fifth_action, False, False),
    (
        "reforma-2027",
        "tipo que não existe",
        lambda p: p["fases"][0]["acoes"][0].update(tipo="x"),
        False,
        False,
    ),
    ("demo", "o exemplo", lambda plan: None, True, True),
    ("demo", "2 ações (mínimo 3)", lambda p: p["acoes"].pop(), False, False),
]


@pytest.mark.parametrize(
    ("slug", "name", "change", "in_code", "migrated"),
    PLAN_CHECKS,
    ids=[f"{slug}-{name}" for slug, name, *_ in PLAN_CHECKS],
)
def test_the_plan_is_accepted_or_refused_as_the_form_in_code_did(
    slug: str, name: str, change: Callable[[dict[str, Any]], None], in_code: bool, migrated: bool
) -> None:
    assert accepts(SPECS[slug], edited(slug, change)) is migrated


def test_the_only_plans_the_definition_judges_differently_are_the_ones_the_owner_accepted() -> None:
    differing = {
        (slug, name) for slug, name, _, in_code, migrated in PLAN_CHECKS if in_code != migrated
    }

    assert differing == {
        # Further Notes da spec: o nome do catálogo não se corrige mais por aproximação.
        ("mcc", "aula com outra caixa"),
        # A expressão proibida é um texto em qualquer parte: o plural também cai.
        ("reforma-2027", "garantias"),
    }


def test_the_refusals_name_the_field_and_the_rule_and_never_the_text_of_the_plan() -> None:
    secret = "Pague sem juros no escritório"
    plan = edited("reforma-2027", lambda p: reforma_text(p, secret))

    with pytest.raises(ValidationError) as caught:
        SPECS["reforma-2027"].plan_model.model_validate(plan)

    messages = [error["msg"] for error in caught.value.errors(include_input=False)]
    assert messages and all(secret not in message for message in messages)
    assert all("sem juros" in message for message in messages)


# --- respostas ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("(19) 99876-5432", "19998765432"),
        ("(19) 3456-7890", "1934567890"),
        # O telefone passa a ser gravado sem o 55, em todo formulário (Further Notes da spec).
        ("+55 (19) 99876-5432", "19998765432"),
        ("551934567890", "1934567890"),
        # Doze dígitos sem o 55 não são um telefone brasileiro; o código os aceitava.
        ("119998765432", None),
        ("liga pra mim", None),
    ],
)
def test_the_mcc_phone_is_stored_without_the_55(typed: str, stored: str | None) -> None:
    answers = legacy_definition("mcc")["examples"]["answers"]

    try:
        found = valid("mcc", {**answers, "whatsapp": typed}).whatsapp
    except ValidationError:
        found = None

    assert found == stored


@pytest.mark.parametrize("slug", FORMS)
def test_the_example_answers_and_plan_of_each_definition_are_accepted(slug: str) -> None:
    example = legacy_definition(slug)["examples"]

    assert valid(slug, example["answers"]) is not None
    assert (
        SPECS[slug].plan_model.model_validate(example["plan"]).model_dump(mode="json")
        == example["plan"]
    )


# --- versão 1 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("slug", FORMS)
def test_each_definition_is_publishable_as_version_one(deps: Deps, slug: str) -> None:
    """Como o banco a guarda (JSONB), a definição compila na versão 1, gera o PDF de exemplo no
    adaptador real e não manda dado de contato ao LLM: a migração não pede confirmação."""
    with deps.sessions() as session:
        stored = as_stored(session, legacy_definition(slug))

    spec = compile_definition(stored, version_number=1)

    assert (spec.slug, spec.version, spec.prompt_version) == (slug, f"{slug}-v1", f"{slug}-v1")
    assert publication_problems(spec, stored, WeasyPrintRenderer()) == []
    assert contact_in_prompt(spec, stored["identity"]) == []
    assert spec.consent_text_version == stored["consent"]["version"]


# --- pedidos feitos antes da migração -----------------------------------------------------------

# As respostas como o código as gravava (o telefone do MCC com o 55, o texto opcional em branco do
# demo e do reforma-2027 como "") e o texto da versão que o envio guardava.
OLD_REQUESTS: dict[str, tuple[str, dict[str, Any]]] = {
    "demo": (
        "demo-2026.10",
        {**DEMO_ANSWERS, "email": "contadora@example.test", "escritorio": ""},
    ),
    "mcc": (
        "mcc-2026.10",
        {
            **P1_MCC,
            "email": "contadora@example.test",
            "whatsapp": "5519998765432",
            "nome_escritorio": None,
        },
    ),
    "reforma-2027": (
        "reforma-2027-2026.10",
        {
            **REFORMA_PROFILES[0]["respostas"],
            "email": "contadora@example.test",
            "whatsapp": "11912345678",
            "escritorio": "",
        },
    ),
}


def old_request(deps: Deps, slug: str) -> tuple[uuid.UUID, str, str]:
    """Um pedido gravado pelo código anterior à migração: a versão legada (`form_version_number`
    1, o padrão da migration 0003) e o texto da versão do código. Devolve o id do pedido, o
    endereço público e o texto da versão."""
    version, answers = OLD_REQUESTS[slug]
    public_id = new_public_id()
    with deps.sessions.begin() as session:
        lead_id = session.execute(
            sa.insert(Lead.__table__)
            .values(email=answers["email"], name=answers["nome"])
            .returning(Lead.__table__.c.id)
        ).scalar_one()
        submission_id = session.execute(
            sa.insert(Submission.__table__)
            .values(
                lead_id=lead_id,
                form_slug=slug,
                form_version=version,
                idempotency_key="pedido-antigo",
                answers=answers,
                consent={"privacy_at": utcnow().isoformat(), "text_version": f"{slug}-v1"},
            )
            .returning(Submission.__table__.c.id)
        ).scalar_one()
        job_id = session.execute(
            sa.insert(Job.__table__)
            .values(
                submission_id=submission_id,
                public_id=public_id,
                expires_at=utcnow() + timedelta(days=30),
            )
            .returning(Job.__table__.c.id)
        ).scalar_one()
    return job_id, public_id, version


@pytest.mark.parametrize("slug", FORMS)
def test_a_request_made_before_the_migration_is_processed_and_shown_by_version_one(
    deps: Deps, slug: str
) -> None:
    """O pedido antigo aponta para a versão 1: os dados que o código gravou ainda valem, o pedido
    segue o pipeline, a página do link mostra a prévia e o webhook leva o texto da versão antiga."""
    webhook = record_webhook(deps)
    job_id, public_id, version = old_request(deps, slug)

    with deps.sessions.begin() as session:
        jobs.claim(session, job_id, 1, "run-fake")
    plan_id, _ = pipeline.generate_plan(deps, job_id, 1)
    artifact_id, _ = pipeline.render_pdf(deps, job_id, 1, plan_id)
    (delivery,) = pipeline.finalize(deps, job_id, 1, artifact_id)
    pipeline.deliver(deps, delivery)

    ((_, _, payload),) = webhook.calls
    assert payload["form"] == {"slug": slug, "version": version}
    assert payload["plan"] == SPECS[slug].example_plan
    with deps.sessions() as session:
        plan = session.scalars(sa.select(Plan)).one()
        job = session.get_one(Job, job_id)
    assert plan.prompt_version == f"{slug}-v1"

    async def fake_trigger(job_id: uuid.UUID, attempt: int) -> str | None:
        return "run-fake"

    token = deps.signer.issue(public_id, job.token_version)
    page = TestClient(create_app(trigger=fake_trigger)).get(f"/d/{public_id}", params={"t": token})
    assert page.status_code == 200
    shown = page_text(page.text)
    # A prévia do MCC abre com o resumo, e as dos outros dois, com o título do plano.
    assert SPECS[slug].example_plan["titulo"] in shown
