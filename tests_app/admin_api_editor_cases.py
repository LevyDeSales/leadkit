"""Os casos de patch das 42 operações dos editores estruturados que mudam o documento (a tabela
"Operações do editor" de `docs/api/cobertura.md`): para cada uma, o `op` que a página manda, o que a
operadora preenche depois no campo novo e o JSON Patch que produz o mesmo documento.

Não é um arquivo de teste (o pytest não o coleta); `test_admin_api_drafts.py` o importa e prova, para
cada caso, que o editor (`FormEdit.apply`, e os `PlanEdit`, `DataEdit` e `ScoringEdit` que ele carrega,
seguido de `screen()`) e o patch deixam o documento igual, na forma normal.

O documento-base é o da pergunta condicional (`fixtures/conditions_definition.json`: condições em
perguntas e opções, pontuação com faixas, regras de prioridade e de persona), mais o que ele não tem e
os casos pedem: um segundo score, blocos de dados (uma lista e janelas de datas), regras de texto
proibido e um grupo no modelo do plano. Os índices dos casos são os dele.

Dois cuidados que a tabela da matriz resume e os casos mostram por inteiro:

- o editor recalcula o que depende do que mudou: a identidade que apontava para uma pergunta que
  sai, e a resposta de exemplo de uma pergunta ou opção que sai. No documento isso é uma operação
  a mais no mesmo patch (`remove /identity/email`, `remove /examples/answers/<nome>`);
- o editor deixa o `examples.plan` ao JSON do resto da definição, e o plano de exemplo tem de
  acompanhar o modelo do plano. Num patch, o exemplo do campo novo entra no mesmo patch
  (`add /examples/plan/<nome>`); essas operações ficam fora da comparação com o editor (`BEYOND`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.admin.form_editor import FormEdit

FIXTURES = Path(__file__).parent / "fixtures"
# O que o editor não gera, e o patch que mantém o documento válido leva junto.
BEYOND = ("/examples/plan",)


def base() -> dict[str, Any]:
    """O documento-base dos casos."""
    document: dict[str, Any] = json.loads(
        (FIXTURES / "conditions_definition.json").read_text(encoding="utf-8")
    )
    document["scoring"]["scores"].append({"name": "urgencia", "formula": "pontos(equipe)"})
    document["data"] = {
        "lista": {"kind": "list", "items": ["Primeiro", "Segundo"]},
        "janelas": {
            "kind": "windows",
            "windows": [
                {"name": "um", "label": "Um", "start": "2026-10-01", "end": "2026-10-10"},
                {"name": "dois", "start": "2026-10-11", "end": "2026-10-20"},
            ],
        },
    }
    plan = document["plan"]
    plan["fields"].append(
        {
            "name": "detalhes",
            "kind": "group",
            "fields": [{"name": "foco", "kind": "text"}, {"name": "risco", "kind": "text"}],
        }
    )
    plan["forbidden"] = [
        {"expressions": ["garantido"]},
        {"expressions": ["lucro certo"], "scope": "only", "fields": ["titulo"]},
    ]
    document["examples"]["plan"]["detalhes"] = {"foco": "A captação", "risco": "A rotina"}
    return document


@dataclass(frozen=True)
class Case:
    """Uma operação do editor e o patch equivalente."""

    editor: str  # FormEdit, PlanEdit, DataEdit ou ScoringEdit, como a matriz agrupa
    operation: str  # o nome na matriz, sem os números: `remove-step`
    op: str  # o campo `op` da página, com os números: `remove-step:2`
    patch: list[dict[str, Any]]
    fill: Callable[[FormEdit], None] | None = None  # o que a operadora escreve no campo novo
    tweak: Callable[[dict[str, Any]], None] | None = None  # muda o documento-base dos dois lados
    # O que mais o documento precisa para compilar depois do patch (o editor deixa a operadora corrigir
    # em seguida): as operações que o completam. O teste de equivalência as ignora, e o de HTTP as manda
    # junto, para o `PATCH` dar `200` nos 53 casos.
    complete: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None

    @property
    def id(self) -> str:
        return self.op


def add(path: str, value: Any) -> dict[str, Any]:
    return {"op": "add", "path": path, "value": value}


def remove(path: str) -> dict[str, Any]:
    return {"op": "remove", "path": path}


def move(source: str, path: str) -> dict[str, Any]:
    return {"op": "move", "from": source, "path": path}


def replace(path: str, value: Any) -> dict[str, Any]:
    return {"op": "replace", "path": path, "value": value}


def prompt_without(text: str) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    """O prompt sem a linha que cita `text`: o que sai (uma pergunta, um campo) não pode ser citado."""

    def complete(document: dict[str, Any]) -> list[dict[str, Any]]:
        lines = document["prompt"].split("\n")
        kept = [line for line in lines if text not in line]
        assert len(kept) < len(lines), f"o prompt não cita {text}"
        return [replace("/prompt", "\n".join(kept))]

    return complete


def swap_the_minimums(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Os níveis de uma faixa vão do maior mínimo para o menor: trocar dois de lugar pede trocar o
    mínimo deles também (a regra do editor, que a operadora cumpre em seguida)."""
    return [
        replace("/scoring/scores/0/bands/levels/0/min", 6),
        replace("/scoring/scores/0/bands/levels/1/min", 2),
    ]


# --- o que a operadora escreve no campo novo ------------------------------------------------------


def new_question(edit: FormEdit, step: int, name: str = "observacoes") -> None:
    question = edit.steps[step].questions[-1]
    question.name, question.label, question.required = name, "Observações", False


def new_step(edit: FormEdit) -> None:
    edit.steps[-1].title = "Nova etapa"
    new_question(edit, len(edit.steps) - 1)


def new_option(edit: FormEdit) -> None:
    option = edit.steps[1].questions[0].options[-1]
    option.value, option.label = "agenda", "Agenda"


def new_rule(edit: FormEdit, i: int, j: int, k: int | None = None) -> None:
    holder = edit.steps[i].questions[j]
    condition = (holder if k is None else holder.options[k]).show_when
    assert condition is not None
    rule = condition.rules[-1]
    rule.question, rule.operator, rule.values = "situacao", "equals", ["autonomo"]


def new_plan_field(edit: FormEdit) -> None:
    edit.plan.fields[-1].name = "alerta"


def new_forbidden_rule(edit: FormEdit) -> None:
    edit.plan.rules[-1].expressions = "promessa de resultado"


def new_block(edit: FormEdit) -> None:
    block = edit.data.blocks[-1]
    block.name, block.items = "novos", "Alfa\nBeta"


def new_window(edit: FormEdit) -> None:
    window = edit.data.blocks[0].windows[-1]
    window.name, window.label = "tres", "Três"
    window.start, window.end = "2026-10-21", "2026-10-30"


def new_score(edit: FormEdit) -> None:
    score = edit.scoring.scores[-1]
    score.name, score.formula = "folga", "pontos(carteira)"


def new_level(edit: FormEdit) -> None:
    level = edit.scoring.scores[0].bands.levels[2]  # type: ignore[union-attr]
    level.min, level.key, level.label = "1", "baixo_medio", "Baixo a médio"


def new_bands_level(edit: FormEdit) -> None:
    level = edit.scoring.scores[1].bands.levels[-1]  # type: ignore[union-attr]
    level.key, level.label = "unico", "Único"


def new_priority(edit: FormEdit) -> None:
    rule = edit.scoring.priority[2]
    rule.when, rule.value = "porte >= 4", "P2"


def single_forbidden_rule(document: dict[str, Any]) -> None:
    del document["plan"]["forbidden"][1:]


def phone_in_its_own_step(document: dict[str, Any]) -> None:
    """O WhatsApp numa etapa só dele, no fim: uma etapa com uma pergunta da identidade que se pode tirar."""
    questions = document["steps"][0]["questions"]
    phone = next(q for q in questions if q["name"] == "whatsapp")
    questions.remove(phone)
    document["steps"].append({"title": "Contato", "questions": [phone]})


def windows_may_overlap(document: dict[str, Any]) -> None:
    """Janelas fora de sequência: sem isso, trocar duas de lugar as deixaria fora de ordem."""
    document["data"]["janelas"]["sequential"] = False


QUESTION = {
    "name": "observacoes",
    "kind": "text",
    "label": "Observações",
    "required": False,
    "max_length": 300,
}
FORM = "FormEdit"
PLAN = "PlanEdit"
DATA = "DataEdit"
SCORING = "ScoringEdit"
Q_COND = "/steps/0/questions/5/show_when"  # `carteira`, com duas regras
RULE = {"field": "situacao", "operator": "equals", "value": "autonomo"}
CONDITION = {"mode": "all", "rules": [RULE]}

CASES: tuple[Case, ...] = (
    # --- etapas, perguntas e opções (FormEdit) ---
    Case(
        FORM,
        "add-step",
        "add-step",
        [add("/steps/-", {"title": "Nova etapa", "questions": [QUESTION]})],
        new_step,
    ),
    Case(
        FORM,
        "remove-step",
        "remove-step:2",
        [remove("/steps/2"), remove("/examples/answers/resultado")],
        complete=prompt_without("labels.resultado"),
    ),
    Case(
        FORM,
        "remove-step",
        "remove-step:3",  # a etapa de uma pergunta da identidade: o papel sai junto
        [remove("/steps/3"), remove("/identity/phone")],
        tweak=phone_in_its_own_step,
        # O canal de WhatsApp do consentimento pede o telefone da lead na identidade.
        complete=lambda document: [remove("/consent/marketing/whatsapp")],
    ),
    Case(FORM, "up-step", "up-step:2", [move("/steps/2", "/steps/1")]),
    Case(FORM, "down-step", "down-step:1", [move("/steps/1", "/steps/2")]),
    Case(
        FORM,
        "add-question",
        "add-question:1",
        [add("/steps/1/questions/-", QUESTION)],
        lambda edit: new_question(edit, 1),
    ),
    Case(
        FORM,
        "remove-question",
        "remove-question:1:2",
        [remove("/steps/1/questions/2"), remove("/examples/answers/outra_dor")],
        complete=prompt_without("answers.outra_dor"),
    ),
    Case(
        FORM,
        "up-question",
        "up-question:1:1",
        [move("/steps/1/questions/1", "/steps/1/questions/0")],
    ),
    Case(
        FORM,
        "up-question",
        "up-question:1:0",  # no topo, vai para o fim da etapa de cima
        [move("/steps/1/questions/0", "/steps/0/questions/-")],
    ),
    Case(
        FORM,
        "down-question",
        "down-question:1:0",
        [move("/steps/1/questions/0", "/steps/1/questions/1")],
    ),
    Case(
        FORM,
        "down-question",
        "down-question:1:3",  # no fim, vai para o começo da etapa de baixo
        [move("/steps/1/questions/3", "/steps/2/questions/0")],
    ),
    Case(
        FORM,
        "add-option",
        "add-option:1:0",
        [add("/steps/1/questions/0/options/-", {"value": "agenda", "label": "Agenda"})],
        new_option,
    ),
    Case(
        FORM,
        "remove-option",
        "remove-option:1:0:0",
        [remove("/steps/1/questions/0/options/0")],
    ),
    Case(
        FORM,
        "up-option",
        "up-option:0:3:2",
        [move("/steps/0/questions/3/options/2", "/steps/0/questions/3/options/1")],
    ),
    Case(
        FORM,
        "down-option",
        "down-option:0:3:0",
        [move("/steps/0/questions/3/options/0", "/steps/0/questions/3/options/1")],
    ),
    Case(
        FORM,
        "add-condition",
        "add-condition:1:0",
        [add("/steps/1/questions/0/show_when", CONDITION)],
        lambda edit: new_rule(edit, 1, 0),
    ),
    Case(
        FORM,
        "add-condition",
        "add-condition:1:0:1",  # a condição de uma opção
        [add("/steps/1/questions/0/options/1/show_when", CONDITION)],
        lambda edit: new_rule(edit, 1, 0, 1),
    ),
    Case(
        FORM, "remove-condition", "remove-condition:1:3", [remove("/steps/1/questions/3/show_when")]
    ),
    Case(
        FORM,
        "remove-condition",
        "remove-condition:0:5:0",
        [remove("/steps/0/questions/5/options/0/show_when")],
    ),
    Case(
        FORM,
        "add-rule",
        "add-rule:0:5",
        [add(f"{Q_COND}/rules/-", RULE)],
        lambda edit: new_rule(edit, 0, 5),
    ),
    Case(
        FORM,
        "add-rule",
        "add-rule:0:5:0",  # a regra de uma opção
        [add("/steps/0/questions/5/options/0/show_when/rules/-", RULE)],
        lambda edit: new_rule(edit, 0, 5, 0),
    ),
    Case(FORM, "remove-rule", "remove-rule:0:5:1", [remove(f"{Q_COND}/rules/1")]),
    Case(
        FORM,
        "remove-rule",
        "remove-rule:1:3:0",  # a última regra leva a condição junto
        [remove("/steps/1/questions/3/show_when")],
    ),
    Case(
        FORM,
        "remove-rule",
        "remove-rule:0:5:0:0",  # a última regra de uma opção
        [remove("/steps/0/questions/5/options/0/show_when")],
    ),
    # --- o modelo do plano (PlanEdit) ---
    Case(
        PLAN,
        "plan-add-field",
        "plan-add-field:plan",
        [
            add("/plan/fields/-", {"name": "alerta", "kind": "text"}),
            add("/examples/plan/alerta", "Atenção ao prazo da proposta."),
        ],
        new_plan_field,
    ),
    Case(
        PLAN,
        "plan-remove",
        "plan-remove:plan.fields.4",
        [remove("/plan/fields/4"), remove("/examples/plan/detalhes")],
    ),
    Case(
        PLAN,
        "plan-remove",
        "plan-remove:plan.fields.4.fields.1",  # um campo dentro do grupo
        [remove("/plan/fields/4/fields/1"), remove("/examples/plan/detalhes/risco")],
    ),
    Case(PLAN, "plan-up", "plan-up:plan.fields.2", [move("/plan/fields/2", "/plan/fields/1")]),
    Case(
        PLAN,
        "plan-up",
        "plan-up:plan.fields.4.fields.1",
        [move("/plan/fields/4/fields/1", "/plan/fields/4/fields/0")],
    ),
    Case(PLAN, "plan-down", "plan-down:plan.fields.0", [move("/plan/fields/0", "/plan/fields/1")]),
    Case(
        PLAN,
        "plan-add-rule",
        "plan-add-rule",
        [add("/plan/forbidden/-", {"expressions": ["promessa de resultado"]})],
        new_forbidden_rule,
    ),
    Case(PLAN, "plan-remove-rule", "plan-remove-rule:1", [remove("/plan/forbidden/1")]),
    Case(
        PLAN,
        "plan-remove-rule",
        "plan-remove-rule:0",  # a última regra leva a chave `forbidden` junto
        [remove("/plan/forbidden")],
        tweak=single_forbidden_rule,
    ),
    # --- os dados (DataEdit) ---
    Case(
        DATA,
        "data-add",
        "data-add",
        [add("/data/novos", {"kind": "list", "items": ["Alfa", "Beta"]})],
        new_block,
    ),
    Case(DATA, "data-remove", "data-remove:1", [remove("/data/lista")]),
    Case(
        DATA,
        "data-add-window",
        "data-add-window:0",
        [
            add(
                "/data/janelas/windows/-",
                {"name": "tres", "label": "Três", "start": "2026-10-21", "end": "2026-10-30"},
            )
        ],
        new_window,
    ),
    Case(DATA, "data-remove-window", "data-remove-window:0:1", [remove("/data/janelas/windows/1")]),
    Case(
        DATA,
        "data-up-window",
        "data-up-window:0:1",
        [move("/data/janelas/windows/1", "/data/janelas/windows/0")],
        tweak=windows_may_overlap,
    ),
    Case(
        DATA,
        "data-down-window",
        "data-down-window:0:0",
        [move("/data/janelas/windows/0", "/data/janelas/windows/1")],
        tweak=windows_may_overlap,
    ),
    # --- a pontuação (ScoringEdit) ---
    Case(
        SCORING,
        "scoring-add-score",
        "scoring-add-score",
        [add("/scoring/scores/-", {"name": "folga", "formula": "pontos(carteira)"})],
        new_score,
    ),
    Case(SCORING, "scoring-remove-score", "scoring-remove-score:1", [remove("/scoring/scores/1")]),
    Case(
        SCORING,
        "scoring-up-score",
        "scoring-up-score:1",
        [move("/scoring/scores/1", "/scoring/scores/0")],
    ),
    Case(
        SCORING,
        "scoring-down-score",
        "scoring-down-score:0",
        [move("/scoring/scores/0", "/scoring/scores/1")],
    ),
    Case(
        SCORING,
        "scoring-add-bands",
        "scoring-add-bands:1",
        [
            add(
                "/scoring/scores/1/bands",
                {
                    "key": "nivel_urgencia",
                    "label": "rotulo_urgencia",
                    "levels": [{"key": "unico", "label": "Único"}],
                },
            )
        ],
        new_bands_level,
    ),
    Case(
        SCORING,
        "scoring-remove-bands",
        "scoring-remove-bands:0",
        [remove("/scoring/scores/0/bands")],
    ),
    Case(
        SCORING,
        "scoring-add-level",
        "scoring-add-level:0",  # antes do último, que vale para o resto
        [
            add(
                "/scoring/scores/0/bands/levels/2",
                {"min": 1, "key": "baixo_medio", "label": "Baixo a médio"},
            )
        ],
        new_level,
    ),
    Case(
        SCORING,
        "scoring-remove-level",
        "scoring-remove-level:0:1",
        [remove("/scoring/scores/0/bands/levels/1")],
    ),
    Case(
        SCORING,
        "scoring-up-level",
        "scoring-up-level:0:1",
        [move("/scoring/scores/0/bands/levels/1", "/scoring/scores/0/bands/levels/0")],
        complete=swap_the_minimums,
    ),
    Case(
        SCORING,
        "scoring-down-level",
        "scoring-down-level:0:0",
        [move("/scoring/scores/0/bands/levels/0", "/scoring/scores/0/bands/levels/1")],
        complete=swap_the_minimums,
    ),
    Case(
        SCORING,
        "scoring-add-rule",
        "scoring-add-rule:priority",  # antes do "senão", que fica por último
        [add("/scoring/priority/2", {"when": "porte >= 4", "value": "P2"})],
        new_priority,
    ),
    Case(
        SCORING,
        "scoring-remove-rule",
        "scoring-remove-rule:priority:1",
        [remove("/scoring/priority/1")],
    ),
    Case(
        SCORING,
        "scoring-up-rule",
        "scoring-up-rule:priority:1",
        [move("/scoring/priority/1", "/scoring/priority/0")],
    ),
    Case(
        SCORING,
        "scoring-down-rule",
        "scoring-down-rule:priority:0",
        [move("/scoring/priority/0", "/scoring/priority/1")],
    ),
)
