"""A condição (`show_when`) de uma pergunta ou de uma opção em português: "Aparece quando
Situação profissional é uma destas: Autônomo, Escritório próprio".

É a mesma frase no resumo da pergunta no editor, na leitura do formulário e na comparação entre
versões (`app.forms.definition.changes`), por isso mora aqui e não no editor do admin
(`app.admin.condition_editor`), que a usa.

Uma regra só vira frase quando está completa: com a pergunta e ao menos um valor. Sem isso (o
editor, enquanto a operadora escolhe), a condição inteira fica sem frase.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.forms.definition.questions import CHOICE_KINDS

# O operador na frase. Na escolha múltipla, `in` quer dizer que ao menos uma das opções marcadas
# está na lista, e "é uma destas" leria como "são exatamente estas".
OPERATOR_LABELS = {"equals": "é", "in": "é uma destas", "contains": "contém"}
MULTIPLE_OPERATOR_LABELS = {**OPERATOR_LABELS, "in": "inclui alguma destas"}

# Uma regra para a frase: (a pergunta que ela olha, o operador, os valores).
Rule = tuple[str, str, Sequence[str]]


def operator_label(operator: str, kind: str | None) -> str:
    labels = MULTIPLE_OPERATOR_LABELS if kind == "choices" else OPERATOR_LABELS
    return labels.get(operator, operator)


@dataclass(frozen=True)
class Target:
    """Uma pergunta que uma regra pode olhar: o nome, o rótulo, o tipo e as opções (valor, rótulo)."""

    name: str
    label: str
    kind: str
    options: tuple[tuple[str, str], ...] = ()

    @property
    def choice(self) -> bool:
        return self.kind in CHOICE_KINDS

    @property
    def shown(self) -> str:
        return self.label or self.name

    def option_label(self, value: str) -> str | None:
        return next((label for item, label in self.options if item == value), None)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _quoted(value: str) -> str:
    return f"“{value}”"


def rule_of(rule: Any) -> Rule:
    """(pergunta, operador, valores) de uma regra como a definição a guarda. O valor é um texto ou
    uma lista de textos; os em branco saem."""
    found = rule if isinstance(rule, Mapping) else {}
    value = found.get("value")
    values = [_text(item) for item in value] if isinstance(value, list) else [_text(value)]
    return (
        _text(found.get("field")),
        _text(found.get("operator")) or "equals",
        [item for item in values if item] if value is not None else [],
    )


def _rule_sentence(rule: Rule, targets: Mapping[str, Target]) -> str:
    """A regra em português: "Situação profissional é uma destas: Autônomo, Escritório próprio".
    Só de uma regra completa."""
    question, operator, values = rule
    target = targets.get(question)
    subject = target.shown if target else _quoted(question)
    verb = operator_label(operator, target.kind if target else None)
    shown = [(target.option_label(value) if target else None) or _quoted(value) for value in values]
    if operator == "in":
        return f"{subject} {verb}: {', '.join(shown)}"
    return f"{subject} {verb} {shown[0]}"


def sentence(mode: str, rules: Sequence[Rule], targets: Mapping[str, Target]) -> str | None:
    """A condição em linguagem natural; None sem regra e enquanto uma regra ainda não diz tudo (sem
    pergunta ou sem valor). Escolher a pergunta e o valor de uma regra nova não manda nada ao
    servidor até a próxima operação, então uma frase pela metade ("é …") ficaria desatualizada até
    a operadora salvar: sem a regra completa, o resumo fica em branco."""
    if not rules or any(not question or not values for question, _, values in rules):
        return None
    sentences = [_rule_sentence(rule, targets) for rule in rules]
    if len(sentences) == 1:
        return f"Aparece quando {sentences[0]}"
    lead = "todas estas regras valem" if mode == "all" else "qualquer uma destas regras vale"
    return f"Aparece quando {lead}: {'; '.join(sentences)}"


def condition_sentence(condition: Any, targets: Mapping[str, Target]) -> str | None:
    """A frase de uma condição como a definição a guarda (`{"mode": ..., "rules": [...]}`); None
    sem condição ou se ela não diz tudo."""
    if not isinstance(condition, Mapping):
        return None
    rules = condition.get("rules")
    return sentence(
        _text(condition.get("mode")) or "all",
        [rule_of(rule) for rule in rules] if isinstance(rules, list) else [],
        targets,
    )


def targets_of(steps: Any) -> dict[str, Target]:
    """As perguntas das etapas de uma definição como as regras as veem, pelo nome. Lê o documento
    como está, sem conferir a estrutura (uma versão antiga pode não compilar mais)."""
    found: dict[str, Target] = {}
    for step in steps if isinstance(steps, list) else ():
        questions = step.get("questions") if isinstance(step, Mapping) else None
        for question in questions if isinstance(questions, list) else ():
            if not isinstance(question, Mapping) or not isinstance(question.get("name"), str):
                continue
            options = tuple(_options(question.get("options")))
            found.setdefault(
                question["name"],
                Target(
                    question["name"],
                    _text(question.get("label")),
                    _text(question.get("kind")),
                    options,
                ),
            )
    return found


def _options(options: Any) -> Iterable[tuple[str, str]]:
    for option in options if isinstance(options, list) else ():
        if isinstance(option, Mapping):
            yield _text(option.get("value")), _text(option.get("label"))
