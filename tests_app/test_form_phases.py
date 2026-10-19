"""Fases com datas na definição, pela costura do compilador: o calendário que o prompt recebe, fase a
fase, a conferência do plano que recusa um valor fora da janela dele e a conferência da
configuração. O relógio é injetado; o plano começa no dia de hoje em São Paulo."""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import DEMO_ANSWERS
from pydantic import ValidationError
from test_form_definition import answers_of, changed

from app.deps import Deps
from app.forms import FormSpec, drafts
from app.forms.definition import DefinitionError, compile_definition
from app.forms.definition.changes import compare, llm_changes, llm_fingerprint, llm_parts, reads
from app.forms.definition.document import fingerprint
from app.forms.stored import StoredVersion

OPTION = {"name": "opcao", "label": "Opção", "start": "2026-09-25", "end": "2026-10-30"}
WITHDRAWAL = {
    "name": "desistencia",
    "label": "Desistência",
    "start": "2026-11-03",
    "end": "2026-12-20",
}
KINDS = ["decisao", "desistencia", "proxima", "outra"]
EXTRAS = ["revisao", "ajuste"]
LINKS = {"decisao": "opcao", "desistencia": "desistencia"}
PLAN: dict[str, Any] = {
    "fields": [
        {"name": "titulo", "kind": "text", "min_length": 3, "max_length": 120},
        {
            "name": "fases",
            "kind": "list",
            "min_items": 3,
            "max_items": 3,
            "items": {
                "kind": "group",
                "model": "Fase",
                "fields": [
                    {"name": "nome", "kind": "text", "max_length": 60},
                    {
                        "name": "acoes",
                        "kind": "list",
                        "min_items": 1,
                        "max_items": 4,
                        "items": {
                            "kind": "group",
                            "model": "Acao",
                            "fields": [
                                {
                                    "name": "tipo",
                                    "kind": "options",
                                    "values": KINDS,
                                    "in_window": LINKS,
                                },
                                {"name": "titulo", "kind": "text", "max_length": 90},
                            ],
                        },
                    },
                ],
            },
        },
    ]
}
PHASES = {"days": [30, 30, 30], "plan_field": "fases", "windows": "janelas"}
TIPO = "plan.fields.1.items.fields.1.items.fields.0"  # o caminho do campo `tipo` na definição


def windows(option: dict[str, Any] = OPTION, withdrawal: dict[str, Any] = WITHDRAWAL) -> Any:
    return {"janelas": {"kind": "windows", "windows": [option, withdrawal]}}


def action(kind: str) -> dict[str, str]:
    return {"tipo": kind, "titulo": f"Ação do tipo {kind}"}


def phase(*kinds: str) -> dict[str, Any]:
    return {"nome": "Tema", "acoes": [action(kind) for kind in kinds]}


# Em 8/10/2026 as fases vão de 8/10 a 6/11, de 7/11 a 6/12 e de 7/12 a 5/01. A opção vai de 25/09
# a 30/10 e a desistência, de 03/11 a 20/12: o exemplo cabe no calendário desse dia.
EXAMPLE = {
    "titulo": "Plano com fases",
    "fases": [phase("decisao", "outra"), phase("desistencia"), phase("proxima", "outra")],
}
PROMPT = (
    "Início: {{ today|long_date }}.\n"
    "{%- for fase in phases %}\n"
    "Fase {{ fase.number }} ({{ fase.period }}): {{ fase.situation }} "
    "Cabem: {{ fase.allowed.tipo|join(', ') }}.\n"
    "{%- endfor %}\n"
    "Objetivo: {{ answers.objetivo_90_dias }}\n"
)
DAY = datetime(2026, 10, 8, 12, tzinfo=UTC)


def phased(**more: Any) -> dict[str, Any]:
    """A definição do demo com o plano em fases, as janelas, as fases e o prompt do calendário."""
    return changed(
        {
            "plan": PLAN,
            "examples.plan": EXAMPLE,
            "data": windows(),
            "phases": PHASES,
            "prompt": PROMPT,
            "pdf.html": "<h1>{{ plan.titulo }}</h1>",
            "preview": "<h2>{{ plan.titulo }}</h2>",
            **more,
        }
    )


def build(document: dict[str, Any] | None = None, *, now: datetime = DAY) -> FormSpec:
    return compile_definition(phased() if document is None else document, clock=lambda: now)


def calendar(spec: FormSpec) -> list[str]:
    """As linhas de fase do prompt montado, como o LLM as lê."""
    prompt = spec.render_prompt(answers_of(spec, DEMO_ANSWERS))
    return [line for line in prompt.splitlines() if line.startswith("Fase ")]


def refusals(spec: FormSpec, plan: dict[str, Any]) -> list[str]:
    """As mensagens da conferência do plano, como a rodada de reparo as recebe."""
    try:
        spec.validate_plan(json.dumps(plan), answers_of(spec, DEMO_ANSWERS))
    except ValidationError as exc:
        assert {item["type"] for item in exc.errors(include_input=False)} == {"plan_check"}
        return [item["msg"] for item in exc.errors(include_input=False)]
    return []


def problems_of(document: dict[str, Any]) -> list[str]:
    with pytest.raises(DefinitionError) as raised:
        compile_definition(document)
    return [f"{problem.path}: {problem.message}" for problem in raised.value.problems]


def plan_of(*phases: dict[str, Any]) -> dict[str, Any]:
    return {"titulo": "Plano com fases", "fases": list(phases)}


# --- o calendário que o prompt recebe -----------------------------------------------------------


def test_the_prompt_gets_each_phase_with_its_dates_and_the_state_of_the_windows() -> None:
    assert calendar(build()) == [
        (
            "Fase 1 (8 de outubro a 6 de novembro de 2026): Opção: está aberta e fecha nesta fase. "
            "Desistência: abre nesta fase. Cabem: decisao, desistencia, proxima, outra."
        ),
        (
            "Fase 2 (7 de novembro a 6 de dezembro de 2026): Opção: já fechou. "
            "Desistência: está aberta durante toda a fase. Cabem: desistencia, proxima, outra."
        ),
        (
            "Fase 3 (7 de dezembro de 2026 a 5 de janeiro de 2027): Opção: já fechou. "
            "Desistência: está aberta e fecha nesta fase. Cabem: desistencia, proxima, outra."
        ),
    ]


def test_the_plan_starts_on_the_day_of_the_clock_in_sao_paulo() -> None:
    # 02:30 UTC de 8 de outubro ainda é 7 de outubro em São Paulo.
    spec = build(now=datetime(2026, 10, 8, 2, 30, tzinfo=UTC))

    assert calendar(spec)[0].startswith("Fase 1 (7 de outubro a 5 de novembro de 2026)")


def test_each_phase_has_its_own_number_of_days() -> None:
    spec = build(phased(phases={**PHASES, "days": [10, 45, 20]}))

    assert [line.split(": ")[0] for line in calendar(spec)] == [
        "Fase 1 (8 de outubro a 17 de outubro de 2026)",
        "Fase 2 (18 de outubro a 1º de dezembro de 2026)",
        "Fase 3 (2 de dezembro a 21 de dezembro de 2026)",
    ]


def test_the_period_shows_both_years_when_a_phase_crosses_the_new_year() -> None:
    spec = build(now=datetime(2026, 12, 20, 12, tzinfo=UTC))

    assert calendar(spec)[0].startswith("Fase 1 (20 de dezembro de 2026 a 18 de janeiro de 2027)")


# A fase 1 de 8/10/2026 vai de 8/10 a 6/11. A desistência fica sempre depois da opção.
LATER = {"name": "desistencia", "label": "Desistência", "start": "2026-12-10", "end": "2026-12-20"}


@pytest.mark.parametrize(
    ("start", "end", "state"),
    [
        ("2026-11-20", "2026-12-01", "ainda não abriu"),
        ("2026-10-20", "2026-12-01", "abre nesta fase"),
        ("2026-10-08", "2026-12-01", "está aberta durante toda a fase"),
        ("2026-09-01", "2026-12-01", "está aberta durante toda a fase"),
        ("2026-09-01", "2026-10-30", "está aberta e fecha nesta fase"),
        ("2026-09-01", "2026-11-06", "está aberta e fecha nesta fase"),
        ("2026-10-15", "2026-10-20", "abre e fecha nesta fase"),
        ("2026-08-01", "2026-09-30", "já fechou"),
    ],
    ids=[
        "antes",
        "abre-na-fase",
        "abre-no-primeiro-dia",
        "aberta-a-fase-toda",
        "fecha-na-fase",
        "fecha-no-ultimo-dia",
        "abre-e-fecha",
        "depois",
    ],
)
def test_a_window_is_described_by_where_the_phase_falls_in_it(
    start: str, end: str, state: str
) -> None:
    option = {**OPTION, "start": start, "end": end}
    spec = build(phased(data=windows(option, LATER)))

    assert f"Opção: {state}." in calendar(spec)[0]


def test_the_template_reads_each_window_by_name_with_its_state_and_dates() -> None:
    prompt = (
        "{% for fase in phases %}"
        "{{ fase.windows.opcao.state }} | {{ fase.windows.opcao.open }} | "
        "{{ fase.windows.opcao.end|short_date }} | {{ fase.first|short_date }} | "
        "{{ fase.last|short_date }} | {{ fase.windows.desistencia.label }}\n"
        "{% endfor %}"
    )
    spec = build(phased(prompt=prompt))

    assert spec.render_prompt(answers_of(spec, DEMO_ANSWERS)).splitlines() == [
        "closes | True | 30/10/2026 | 08/10/2026 | 06/11/2026 | Desistência",
        "closed | False | 30/10/2026 | 07/11/2026 | 06/12/2026 | Desistência",
        "closed | False | 30/10/2026 | 07/12/2026 | 05/01/2027 | Desistência",
    ]


def test_a_window_without_a_label_shows_its_name() -> None:
    bare = {key: value for key, value in WITHDRAWAL.items() if key != "label"}
    spec = build(phased(data=windows(OPTION, bare)))

    assert "desistencia: abre nesta fase." in calendar(spec)[0]


def test_a_form_without_phases_has_no_calendar_and_no_check() -> None:
    spec = compile_definition(changed())

    assert spec.templates is not None and spec.templates.phases() == []
    assert spec.check_plan is None


def test_a_prompt_that_reads_phases_needs_the_phases_to_be_configured() -> None:
    document = phased()
    del document["phases"]
    document["plan"] = copy.deepcopy(PLAN)
    del document["plan"]["fields"][1]["items"]["fields"][1]["items"]["fields"][0]["in_window"]

    assert problems_of(document) == ["prompt: o formulário não tem fases com datas (phases)"]


# --- a conferência do plano ---------------------------------------------------------------------

DECISION_LATE = (
    "o valor decisao não cabe nesta fase (janela Opção: já fechou). decisao só cabe na fase 1: "
    "leve esses itens para lá, mantendo as quantidades de cada campo, ou reescreva o conteúdo "
    "para um valor que caiba aqui (desistencia, proxima, outra). Não troque só o valor: o "
    "conteúdo do item precisa combinar com ele."
)


def test_a_plan_that_fits_the_calendar_is_accepted() -> None:
    assert refusals(build(), EXAMPLE) == []


def test_a_value_outside_its_window_is_refused_with_the_position_and_the_rule() -> None:
    plan = plan_of(phase("decisao", "outra"), phase("desistencia", "decisao"), phase("proxima"))

    assert refusals(build(), plan) == [f"fases.1.acoes.1.tipo (fase 2): {DECISION_LATE}"]


def test_the_messages_group_the_positions_by_value_and_phase() -> None:
    plan = plan_of(phase("outra"), phase("decisao", "outra", "decisao"), phase("decisao"))

    messages = refusals(build(), plan)

    assert [message.split(": ")[0] for message in messages] == [
        "fases.1.acoes.0.tipo, fases.1.acoes.2.tipo (fase 2)",
        "fases.2.acoes.0.tipo (fase 3)",
    ]
    assert all(DECISION_LATE in message for message in messages)


def test_a_value_that_fits_nowhere_in_the_plan_says_so_and_offers_what_fits() -> None:
    spec = build(now=datetime(2026, 11, 1, 12, tzinfo=UTC))  # a opção já fechou na fase 1
    plan = plan_of(phase("decisao"), phase("outra"), phase("outra"))

    assert refusals(spec, plan) == [
        (
            "fases.0.acoes.0.tipo (fase 1): o valor decisao não cabe nesta fase (janela Opção: já "
            "fechou). decisao não cabe em nenhuma fase deste plano: reescreva o conteúdo para um "
            "valor que caiba aqui (desistencia, proxima, outra). Não troque só o valor: o conteúdo "
            "do item precisa combinar com ele."
        )
    ]


def test_the_repair_never_gets_the_text_of_the_plan() -> None:
    secret = "Texto do plano que não pode ir para o reparo"
    bad = phase("outra")
    bad["acoes"][0] = {"tipo": "decisao", "titulo": secret}
    plan = plan_of(phase("outra"), bad, phase("outra"))

    messages = refusals(build(), plan)

    assert messages and all(secret not in message for message in messages)


def test_the_check_follows_the_clock_every_time_it_runs() -> None:
    now = [DAY]
    spec = compile_definition(phased(), clock=lambda: now[0])
    plan = plan_of(phase("decisao"), phase("outra"), phase("outra"))

    assert refusals(spec, plan) == []
    now[0] = datetime(2026, 11, 1, 12, tzinfo=UTC)
    assert len(refusals(spec, plan)) == 1


def test_values_without_a_window_fit_in_every_phase() -> None:
    plan = plan_of(phase("proxima", "outra"), phase("proxima", "outra"), phase("proxima", "outra"))

    assert refusals(build(now=datetime(2027, 3, 1, 12, tzinfo=UTC)), plan) == []


def test_the_example_plan_is_not_checked_against_the_calendar_when_compiling() -> None:
    # O plano de exemplo vale em qualquer data: o LLM falso o devolve sem conferir, e uma versão
    # publicada tem de compilar em todo deploy seguinte.
    late = datetime(2027, 6, 1, 12, tzinfo=UTC)

    assert build(now=late).example_plan["fases"][0]["acoes"][0]["tipo"] == "decisao"


def test_the_check_only_exists_when_a_field_is_linked_to_a_window() -> None:
    document = phased(prompt="{% for fase in phases %}Fase {{ fase.number }}\n{% endfor %}")
    del document["plan"]["fields"][1]["items"]["fields"][1]["items"]["fields"][0]["in_window"]

    spec = build(document)

    assert spec.check_plan is None
    assert spec.templates is not None and len(spec.templates.phases()) == 3


def test_the_allowed_values_follow_each_linked_field_by_its_name() -> None:
    prompt = (
        "{% for fase in phases %}{{ fase.allowed.tipo|join(',') }}\n{% endfor %}"
        "{{ phases[0].allowed.tipo|length }}"
    )

    spec = build(phased(prompt=prompt))

    assert spec.render_prompt(answers_of(spec, DEMO_ANSWERS)).splitlines() == [
        "decisao,desistencia,proxima,outra",
        "desistencia,proxima,outra",
        "desistencia,proxima,outra",
        "4",
    ]


# --- a conferência da configuração --------------------------------------------------------------


def with_phases(**changes: Any) -> dict[str, Any]:
    return phased(phases={**PHASES, **changes})


def without(path: str) -> dict[str, Any]:
    document = phased()
    *parents, last = path.split(".")
    target: Any = document
    for key in parents:
        target = target[int(key)] if isinstance(target, list) else target[key]
    del target[last]
    return document


def linked(links: Any) -> dict[str, Any]:
    document = phased()
    tipo = document["plan"]["fields"][1]["items"]["fields"][1]["items"]["fields"][0]
    tipo["in_window"] = links
    return document


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (with_phases(days=[]), "phases.days: precisa de pelo menos 1 item"),
        (with_phases(days=[30] * 13), "phases.days: aceita no máximo 12 itens"),
        (with_phases(days=[30, 0, 30]), "phases.days.1: precisa ser pelo menos 1"),
        (with_phases(days=[30, 400, 30]), "phases.days.1: precisa ser no máximo 366"),
        (with_phases(days=[30, "trinta", 30]), "phases.days.1: precisa ser um número inteiro"),
        (
            with_phases(days=[30, 30]),
            (
                "phases.plan_field: a lista fases do plano precisa ter exatamente 2 itens, um por "
                "fase: use 2 no mínimo e no máximo de itens dela"
            ),
        ),
        (
            with_phases(plan_field="Fases"),
            (
                "phases.plan_field: use os nomes dos campos separados por ponto, como fases ou "
                "plano.fases"
            ),
        ),
        (
            with_phases(plan_field="outras"),
            "phases.plan_field: não há um campo outras no plano",
        ),
        (
            with_phases(plan_field="titulo"),
            "phases.plan_field: o campo titulo do plano não é uma lista",
        ),
        (
            with_phases(plan_field="titulo.fases"),
            "phases.plan_field: o campo titulo do plano não é um grupo",
        ),
        (
            with_phases(windows="nada"),
            "phases.windows: não há um bloco chamado nada nos dados do formulário",
        ),
        (
            phased(
                data={**windows(), "lista": {"kind": "list", "items": ["a"]}},
                phases={
                    **PHASES,
                    "windows": "lista",
                },
            ),
            "phases.windows: o bloco lista dos dados do formulário não é de janelas",
        ),
        (with_phases(extra=1), "phases.extra: campo desconhecido"),
        (without("phases.days"), "phases.days: campo obrigatório"),
    ],
    ids=[
        "sem-fases",
        "fases-demais",
        "zero-dias",
        "dias-demais",
        "dias-em-texto",
        "lista-com-outra-contagem",
        "caminho-mal-escrito",
        "campo-que-nao-existe",
        "campo-que-nao-e-lista",
        "caminho-por-um-texto",
        "bloco-que-nao-existe",
        "bloco-que-nao-e-de-janelas",
        "campo-desconhecido",
        "sem-dias",
    ],
)
def test_the_phases_configuration_is_checked_where_it_is(document: Any, expected: str) -> None:
    assert problems_of(document) == [expected]


def test_the_list_of_phases_may_sit_inside_a_group() -> None:
    document = phased()
    fases = document["plan"]["fields"].pop(1)
    document["plan"]["fields"].append({"name": "roteiro", "kind": "group", "fields": [fases]})
    document["examples"]["plan"] = {"titulo": "Plano", "roteiro": {"fases": EXAMPLE["fases"]}}
    document["phases"] = {**PHASES, "plan_field": "roteiro.fases"}
    spec = build(document)

    assert refusals(spec, {"titulo": "Plano", "roteiro": {"fases": EXAMPLE["fases"]}}) == []
    late = {"titulo": "Plano", "roteiro": {"fases": [phase("decisao")] * 3}}
    assert [message.split(": ")[0] for message in refusals(spec, late)] == [
        "roteiro.fases.1.acoes.0.tipo (fase 2)",
        "roteiro.fases.2.acoes.0.tipo (fase 3)",
    ]


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (
            linked({"decisao": "nada"}),
            f"{TIPO}.in_window.decisao: não há uma janela chamada nada no bloco janelas",
        ),
        (
            linked({"nada": "opcao"}),
            f"{TIPO}.in_window.nada: este valor não está nas opções do campo",
        ),
        (linked({}), f"{TIPO}.in_window: precisa de pelo menos 1 item"),
        (
            linked({"Decisao": "opcao"}),
            (
                f"{TIPO}.in_window.Decisao: nome inválido; use letras minúsculas sem acento, números "
                "e _"
            ),
        ),
    ],
    ids=["janela-que-nao-existe", "valor-que-nao-existe", "sem-ligacoes", "valor-mal-escrito"],
)
def test_the_links_between_values_and_windows_are_checked_where_they_are(
    document: Any, expected: str
) -> None:
    assert problems_of(document) == [expected]


def test_a_link_needs_the_phases_the_windows_and_the_list_of_phases() -> None:
    assert problems_of(without("phases")) == [
        f"{TIPO}.in_window: defina as fases com datas (phases) para ligar valores a janelas"
    ]
    assert problems_of(without("phases.windows")) == [
        f"{TIPO}.in_window: defina em phases o bloco de janelas (windows) de onde saem as janelas"
    ]
    assert problems_of(without("phases.plan_field")) == [
        f"{TIPO}.in_window: defina em phases a lista do plano com um item por fase (plan_field)"
    ]


def test_a_linked_field_must_sit_inside_the_list_of_phases() -> None:
    document = phased()
    outside = {
        "name": "decisao_final",
        "kind": "options",
        "values": KINDS,
        "in_window": {"decisao": "opcao"},
    }
    document["plan"]["fields"].append(outside)
    document["examples"]["plan"]["decisao_final"] = "outra"

    assert problems_of(document) == [
        "plan.fields.2.in_window: o campo precisa estar dentro de fases, a lista das fases"
    ]


def test_two_linked_fields_cannot_share_a_name() -> None:
    document = phased()
    other = {
        "name": "marcos",
        "kind": "list",
        "items": {
            "kind": "group",
            "fields": [
                {
                    "name": "tipo",
                    "kind": "options",
                    "values": KINDS,
                    "in_window": {"decisao": "opcao"},
                }
            ],
        },
    }
    document["plan"]["fields"][1]["items"]["fields"].append(other)
    document["examples"]["plan"]["fases"] = [
        {**item, "marcos": [{"tipo": "outra"}]} for item in EXAMPLE["fases"]
    ]

    assert problems_of(document) == [
        (
            "plan.fields.1.items.fields.2.items.fields.0.in_window: outro campo ligado a janelas já "
            "se chama tipo; o prompt lê os valores permitidos pelo nome do campo"
        )
    ]


def test_a_link_is_only_for_options_fields() -> None:
    document = phased()
    document["plan"]["fields"][0]["in_window"] = {"decisao": "opcao"}

    assert problems_of(document) == ["plan.fields.0.in_window: não vale para campos do tipo text"]


def test_options_taken_from_a_list_can_be_linked_to_windows() -> None:
    document = phased()
    tipo = document["plan"]["fields"][1]["items"]["fields"][1]["items"]["fields"][0]
    del tipo["values"]
    tipo["values_from"] = "tipos"
    document["data"]["tipos"] = {"kind": "list", "items": KINDS}
    tipo["in_window"] = {"decisao": "opcao", "inventado": "desistencia"}

    assert problems_of(document) == [
        f"{TIPO}.in_window.inventado: este valor não está nas opções do campo"
    ]
    tipo["in_window"] = {"decisao": "opcao"}
    assert calendar(build(document))[1].endswith("Cabem: desistencia, proxima, outra.")


def from_two_lists() -> dict[str, Any]:
    """`phased()` com as opções de `tipo` tiradas de duas listas dos dados (a segunda repete
    `decisao`), e `proxima`, que só a segunda tem, ligada à janela da opção."""
    document = phased()
    tipo = document["plan"]["fields"][1]["items"]["fields"][1]["items"]["fields"][0]
    del tipo["values"]
    tipo["values_from"] = ["tipos_base", "tipos_extra"]
    tipo["in_window"] = {"proxima": "opcao"}
    document["data"]["tipos_base"] = {"kind": "list", "items": ["decisao", "desistencia"]}
    document["data"]["tipos_extra"] = {"kind": "list", "items": ["proxima", "outra", "decisao"]}
    document["examples"]["plan"] = {
        "titulo": "Plano com fases",
        "fases": [phase("proxima", "outra"), phase("desistencia"), phase("decisao", "outra")],
    }
    return document


def test_a_value_of_the_second_list_can_be_linked_to_a_window() -> None:
    spec = build(from_two_lists())

    # A união das duas listas, na ordem dos nomes, e a opção só vale na fase 1 (fecha em 30/10).
    assert calendar(spec)[0].endswith("Cabem: decisao, desistencia, proxima, outra.")
    assert calendar(spec)[1].endswith("Cabem: decisao, desistencia, outra.")
    broken = {"titulo": "Plano", "fases": [phase("outra"), phase("proxima"), phase("outra")]}
    [message] = refusals(spec, broken)
    assert message.startswith("fases.1.acoes.0.tipo (fase 2): o valor proxima não cabe nesta fase")


def test_a_link_to_a_value_that_is_in_none_of_the_lists_is_refused() -> None:
    document = from_two_lists()
    link = document["plan"]["fields"][1]["items"]["fields"][1]["items"]["fields"][0]["in_window"]
    link["inventado"] = "opcao"

    assert problems_of(document) == [
        f"{TIPO}.in_window.inventado: este valor não está nas opções do campo"
    ]


# --- o que a publicação pede e a comparação das versões -----------------------------------------


def test_a_change_in_the_phases_goes_to_the_llm_and_asks_for_a_test() -> None:
    before = phased()
    after = phased(phases={**PHASES, "days": [15, 30, 30]})

    assert llm_changes(before, after) == ("as fases com datas",)
    assert llm_fingerprint(before) != llm_fingerprint(after)


def test_the_windows_of_the_phases_count_even_when_the_prompt_does_not_read_them() -> None:
    before = phased()
    moved = {**OPTION, "end": "2026-10-31"}
    after = phased(data=windows(moved))

    assert llm_changes(before, after) == (DATA_PART,)


# --- a lista de onde as opções do plano vêm (`values_from`) -------------------------------------

DATA_PART = "os dados do formulário que o prompt lê ou que as fases e o modelo do plano usam"
NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def with_list_at(where: str, names: tuple[str, ...] = ("tipos",)) -> dict[str, Any]:
    """`phased()` com um campo de opções do plano que tira as opções da lista `tipos` dos dados (ou
    de várias, em `names`: a primeira com os `KINDS`, as outras com `EXTRAS`), que nem o prompt nem
    as instruções leem: na raiz do plano, num grupo, nos itens de uma lista, ou no campo `tipo` das
    ações (uma lista dentro de um grupo dentro de uma lista)."""
    document = phased()
    fields = document["plan"]["fields"]
    if where == "root":
        field: dict[str, Any] = {"name": "foco", "kind": "options"}
        fields.append(field)
    elif where == "group":
        field = {"name": "foco", "kind": "options"}
        fields.append({"name": "resumo", "kind": "group", "model": "Resumo", "fields": [field]})
    elif where == "items":
        field = {"kind": "options"}
        fields.append(
            {"name": "marcas", "kind": "list", "min_items": 1, "max_items": 3, "items": field}
        )
    else:
        field = fields[1]["items"]["fields"][1]["items"]["fields"][0]
        del field["values"]
    field["values_from"] = names[0] if len(names) == 1 else list(names)
    for number, name in enumerate(names):
        document["data"][name] = {"kind": "list", "items": KINDS if number == 0 else EXTRAS}
    return document


def with_items(document: dict[str, Any], items: list[str], name: str = "tipos") -> dict[str, Any]:
    """Uma cópia da definição com outros itens na lista `name`."""
    other = copy.deepcopy(document)
    other["data"][name]["items"] = items
    return other


POSITIONS = ["root", "group", "items", "deep"]


@pytest.mark.parametrize("where", POSITIONS)
def test_the_list_an_options_field_takes_its_values_from_goes_to_the_llm(where: str) -> None:
    document = with_list_at(where)

    assert reads(document).data == ()  # nem as instruções nem o prompt leem os dados
    assert llm_parts(document)["data"] == {
        "janelas": document["data"]["janelas"],
        "tipos": {"kind": "list", "items": KINDS},
    }


@pytest.mark.parametrize("where", POSITIONS)
def test_every_list_of_a_list_of_names_goes_to_the_llm(where: str) -> None:
    document = with_list_at(where, ("tipos", "extras"))

    assert reads(document).data == ()
    assert llm_parts(document)["data"] == {
        "janelas": document["data"]["janelas"],
        "tipos": {"kind": "list", "items": KINDS},
        "extras": {"kind": "list", "items": EXTRAS},
    }


def test_the_list_goes_to_the_llm_even_when_the_definition_does_not_compile() -> None:
    document = with_list_at("deep")
    fields = document["plan"]["fields"]
    fields.insert(0, {"name": "quebrado", "kind": "nao_existe"})
    fields.insert(1, "texto solto")
    fields.insert(2, {"name": "sem_lista", "kind": "options", "values_from": 5})

    with pytest.raises(DefinitionError):
        compile_definition(document)
    assert list(llm_parts(document)["data"]) == ["janelas", "tipos"]


def test_a_list_of_names_with_something_else_in_it_still_sends_the_names_it_has() -> None:
    document = with_list_at("root", ("tipos", "extras"))
    document["plan"]["fields"][-1]["values_from"] = ["tipos", 5, ["extras"], "extras"]

    with pytest.raises(DefinitionError):
        compile_definition(document)
    assert list(llm_parts(document)["data"]) == ["janelas", "tipos", "extras"]


def test_a_prompt_that_reads_all_the_data_still_sends_every_block_once() -> None:
    document = with_list_at("deep")
    document["prompt"] = "{{ data }}"
    document["data"]["avulsa"] = {"kind": "list", "items": ["x"]}

    assert reads(document).data is None
    assert llm_parts(document)["data"] == document["data"]


LIST_EDITS: dict[str, Callable[[list[str]], list[str]]] = {
    "add": lambda items: [*items, "nova_opcao"],
    "remove": lambda items: items[:-1],
    "rename": lambda items: [*items[:-1], "geral"],
}


@pytest.mark.parametrize("edit", LIST_EDITS)
@pytest.mark.parametrize("where", POSITIONS)
def test_changing_the_items_of_the_list_goes_to_the_llm(where: str, edit: str) -> None:
    before = with_list_at(where)
    after = with_items(before, LIST_EDITS[edit](KINDS))

    assert llm_changes(before, after) == (DATA_PART,)
    assert llm_fingerprint(before) != llm_fingerprint(after)


@pytest.mark.parametrize("name", ["tipos", "extras"])
@pytest.mark.parametrize("edit", LIST_EDITS)
@pytest.mark.parametrize("where", POSITIONS)
def test_changing_any_list_of_a_list_of_names_goes_to_the_llm(
    where: str, edit: str, name: str
) -> None:
    before = with_list_at(where, ("tipos", "extras"))
    items = KINDS if name == "tipos" else EXTRAS
    after = with_items(before, LIST_EDITS[edit](items), name)

    assert llm_changes(before, after) == (DATA_PART,)
    assert llm_fingerprint(before) != llm_fingerprint(after)


def drafted(
    document: dict[str, Any], *, published: dict[str, Any] | None, tested: dict[str, Any] | None
) -> drafts.Draft:
    """O rascunho `document` de um formulário com a versão 1 `published` (None: ainda sem versão
    publicada), com o teste com sucesso de `tested` (None: sem teste), como o banco o devolveria."""
    current = None
    if published is not None:
        current = StoredVersion(
            slug=document["slug"],
            number=1,
            definition=published,
            fingerprint=fingerprint(published),
            note="Primeira versão.",
            published_by="operadora",
            published_at=NOW,
        )
    return drafts.Draft(
        form_id=uuid.uuid4(),
        slug=document["slug"],
        title=document["title"],
        current=current,
        document=document,
        base_version=None if current is None else 1,
        updated_by="operadora",
        updated_at=NOW,
        tested_fingerprint=None if tested is None else llm_fingerprint(tested),
        tested_at=None if tested is None else NOW,
    )


def reviewed(deps: Deps, draft: drafts.Draft) -> drafts.Review:
    with deps.sessions() as session:
        return drafts.review(session, draft)


@pytest.mark.parametrize("published", [False, True], ids=["first publication", "next publication"])
def test_the_publication_asks_for_a_new_test_when_the_list_changes(
    deps: Deps, published: bool
) -> None:
    before = with_list_at("deep")
    after = with_items(before, [*KINDS, "nova_opcao"])
    note = {"note": "A lista mudou.", "contact_confirmed": True}

    stale = reviewed(deps, drafted(after, published=before if published else None, tested=before))

    assert stale.problems == () and stale.needs_test
    asked = (
        drafts.TEST_REQUIRED.format(changes=drafts.changed((DATA_PART,)))
        if published
        else drafts.FIRST_TEST_REQUIRED
    )
    assert stale.reasons(**note) == [asked]
    current = reviewed(deps, drafted(after, published=before if published else None, tested=after))
    assert not current.needs_test and current.reasons(**note) == []


def test_a_data_block_that_nothing_uses_still_needs_no_test(deps: Deps) -> None:
    before = with_list_at("deep")
    before["data"]["avulsa"] = {"kind": "list", "items": ["x", "y"]}
    after = copy.deepcopy(before)
    after["data"]["avulsa"]["items"].append("z")

    assert "avulsa" not in llm_parts(after)["data"]
    assert llm_changes(before, after) == ()
    assert llm_fingerprint(before) == llm_fingerprint(after)
    found = reviewed(deps, drafted(after, published=before, tested=None))
    assert not found.needs_test and found.reasons(note="Lista nova.", contact_confirmed=True) == []


def test_a_definition_without_values_from_keeps_its_llm_parts_and_fingerprint() -> None:
    document = {
        "slug": "sem-lista",
        "title": "Sem lista",
        "instructions": "Escreva em tom direto.",
        "prompt": "Objetivo: {{ answers.objetivo }}",
        "plan": {"fields": [{"name": "tipo", "kind": "options", "values": ["a", "b"]}]},
        "data": {"tipos": {"kind": "list", "items": ["a", "b"]}, "avulsa": {"kind": "list"}},
        "steps": [{"questions": [{"name": "objetivo", "kind": "text"}]}],
    }
    expected = {  # calculado à mão: a definição, menos o que não vai ao LLM e os dados que ninguém lê
        "instructions": "Escreva em tom direto.",
        "prompt": "Objetivo: {{ answers.objetivo }}",
        "plan": document["plan"],
        "data": {},
        "answers": {"objetivo": {"kind": "text", "options": []}},
        "labels": {},
    }

    assert llm_parts(document) == expected
    assert llm_fingerprint(document) == fingerprint(expected)


def test_the_comparison_shows_the_phases_in_their_own_section() -> None:
    after = phased(phases={**PHASES, "days": [15, 30, 30]})

    sections = {section.key: section for section in compare(phased(), after)}

    assert sections["fases"].title == "Fases com datas" and sections["fases"].changed
    assert not sections["outras"].changed
