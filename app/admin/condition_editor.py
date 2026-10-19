"""O editor das condições (`show_when`) de uma pergunta e de uma opção no admin, em campos, no lugar
do JSON (ADR 0010: Jinja2 e HTMX). É a parte do editor do rascunho que cuida das condições; o resto
está em `app.admin.form_editor`, que a chama. O formato, o que cada operador faz e a conferência
estão em `app.forms.definition.conditions`.

O estado mora todo no formulário da página, como no resto do editor. Cada campo tem por nome o
caminho dele na definição (`steps.0.questions.5.show_when.rules.0.field`), e é por esse nome que um
problema da compilação volta para a regra e o campo certos. A condição de uma opção vem depois da
opção (`steps.0.questions.5.options.1.show_when...`).

- Uma condição (`ConditionEdit`) tem o modo (`all`: todas as regras; `any`: qualquer uma) e de 1 a 20
  regras (`RuleEdit`): a pergunta que a regra olha (`field`), o operador e o valor.
- A pergunta de uma regra é uma das anteriores, escolhida pelo rótulo. O operador e o valor seguem o
  tipo dela: numa escolha, o valor são as opções da pergunta (uma lista de caixas no `in`, uma lista
  de escolha nos outros); no texto, é um texto (no `in`, um valor por linha). Só aparecem os
  operadores que a conferência aceita para o tipo (`OPERATORS`).
- Incluir e remover uma condição ou uma regra, e desenhar a regra de novo depois de trocar a
  pergunta ou o operador, não gravam nada: o botão manda o formulário inteiro com a operação (`op`),
  e a página volta com a mudança aplicada. As operações dependem de quantos números levam:
  - `add-condition:i:j` e `remove-condition:i:j`: a condição da pergunta; com um terceiro número,
    `add-condition:i:j:k`, a da opção `k`;
  - `add-rule:i:j` (ou `add-rule:i:j:k`): uma regra a mais;
  - `remove-rule:i:j:r` (ou `remove-rule:i:j:k:r`): tira a regra `r`; a última leva a condição junto;
  - `refresh-rule:i:j:r` (ou com `k`): a pergunta da regra mudou, e a regra é ajustada a ela;
  - `refresh-operator:i:j:r` (ou com `k`): o operador mudou, e o valor ganha a forma dele.
- Uma pergunta renomeada, removida ou movida para depois da regra, e uma opção renomeada ou
  removida, deixam a regra apontando para o que não existe mais (ou não vem antes). O editor
  mantém o que a regra tinha, em vez de trocar por outra coisa, e avisa na regra e no alto das
  perguntas; a conferência recusa o salvar e aponta o campo.
- Os valores da regra voltam sem os espaços das pontas, menos no `contains`, que compara o trecho
  como está. Uma lista `in` sobre as opções de uma pergunta volta na ordem das opções. Um valor com
  quebra de linha não cabe no editor.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from app.forms.definition.condition_text import (
    Target,
    condition_sentence,
    operator_label,
    rule_of,
    sentence,
)
from app.forms.definition.document import MAX_CONDITION_RULES, Problem

if TYPE_CHECKING:
    from app.admin.form_editor import FormEdit, QuestionEdit
    from app.forms.definition.document import StepDoc

MODES = (("all", "todas as regras"), ("any", "qualquer regra"))
# Os operadores que a conferência (`condition_problems`) aceita para cada tipo de pergunta; o
# primeiro é o que a regra ganha ao mudar de pergunta. Um teste confere esta tabela com a conferência.
OPERATORS = {
    "choice": ("equals", "in"),
    "choices": ("contains", "in"),
}
TEXT_OPERATORS = ("equals", "in", "contains")
RULE_PARTS = {"field": "pergunta", "operator": "operador", "value": "valor"}

Place = tuple[int, int, int | None]  # (etapa, pergunta, opção): a opção é None na da pergunta

_KEY = re.compile(
    r"steps\.(\d{1,3})\.questions\.(\d{1,3})\.(?:options\.(\d{1,3})\.)?show_when"
    r"\.(?:(mode)|rules\.(\d{1,3})\.(field|operator|value))"
)


class FormFields(Protocol):
    """O formulário que voltou do navegador (o `FormData` do Starlette)."""

    def __iter__(self) -> Iterator[str]: ...

    def getlist(self, key: str) -> list[Any]: ...


def operators_for(kind: str | None) -> tuple[str, ...]:
    """Os operadores que valem numa regra sobre uma pergunta deste tipo; sem pergunta escolhida
    (None), todos."""
    return OPERATORS.get(kind or "", TEXT_OPERATORS)


def base_name(place: Place) -> str:
    """O nome da condição de uma pergunta ou de uma opção, no formulário e na definição."""
    i, j, k = place
    prefix = f"steps.{i}.questions.{j}"
    return f"{prefix}.show_when" if k is None else f"{prefix}.options.{k}.show_when"


def _lines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


# --- o editor ---------------------------------------------------------------------------------


def target_of(question: QuestionEdit) -> Target:
    """A pergunta do editor como uma regra a vê."""
    options: tuple[tuple[str, str], ...] = ()
    if question.choice:
        options = tuple(
            (value, option.label.strip() or value)
            for option in question.options
            if (value := option.value.strip())
        )
    return Target(question.name.strip(), question.label.strip(), question.kind, options)


@dataclass(eq=False)
class RuleEdit:
    """Uma regra como o editor a mostra: a pergunta que ela olha (pelo nome), o operador e os
    valores (um só, fora do `in`)."""

    question: str = ""
    operator: str = "equals"
    values: list[str] = field(default_factory=list)

    @classmethod
    def of_document(cls, rule: Any) -> RuleEdit:
        question, operator, values = rule_of(rule)
        return cls(question=question, operator=operator, values=list(values))

    @classmethod
    def of_form(cls, parts: Mapping[str, list[str]]) -> RuleEdit:
        """A regra como a página a mandou, com os campos `field`, `operator` e `value`."""

        def first(key: str) -> str:
            return parts[key][0].strip() if parts.get(key) else ""

        operator = first("operator")
        posted = [_lines(str(item)) for item in parts.get("value", ())]
        if operator == "contains":
            values = posted  # o trecho vale como está, com os espaços
        else:
            values = [line.strip() for item in posted for line in item.split("\n") if line.strip()]
        return cls(question=first("field"), operator=operator, values=values)

    def document(self) -> dict[str, Any]:
        """A regra no formato da definição. O que não confere (um valor em branco, uma lista onde
        vai um texto) fica para a compilação apontar no campo."""
        listed = self.operator == "in" or len(self.values) > 1
        value: str | list[str] = (
            list(self.values) if listed else (self.values[0] if self.values else "")
        )
        return {"field": self.question, "operator": self.operator, "value": value}

    def reshape(self) -> None:
        """O valor ganha a forma do operador: um só fora do `in`."""
        if self.operator != "in":
            self.values = self.values[:1]

    def retarget(self, target: Target | None) -> None:
        """A pergunta da regra mudou: o operador passa a um que vale nela, e os valores que não são
        opções dela saem."""
        valid = operators_for(target.kind if target else None)
        if self.operator not in valid:
            self.operator = valid[0]
        if target is not None and target.choice:
            known = {value for value, _ in target.options}
            self.values = [value for value in self.values if value in known]
        self.reshape()


@dataclass(eq=False)
class ConditionEdit:
    """A condição de uma pergunta ou de uma opção como o editor a mostra."""

    mode: str = "all"
    rules: list[RuleEdit] = field(default_factory=list)

    @classmethod
    def of_document(cls, condition: Any) -> ConditionEdit | None:
        """A condição de uma definição guardada; None sem condição."""
        if not isinstance(condition, Mapping):
            return None
        rules = condition.get("rules")
        return cls(
            mode=_text(condition.get("mode")) or "all",
            rules=[RuleEdit.of_document(rule) for rule in rules] if isinstance(rules, list) else [],
        )

    def document(self) -> dict[str, Any]:
        return {"mode": self.mode, "rules": [rule.document() for rule in self.rules]}

    def problems(self, base: str) -> list[Problem]:
        """O que o editor não escreve na definição: uma regra sem pergunta, que a compilação só
        apontaria como um nome fora do formato."""
        return [
            Problem(f"{base}.rules.{index}.field", "escolha a pergunta que a regra olha")
            for index, rule in enumerate(self.rules)
            if not rule.question
        ]


def conditions_of_form(form: FormFields) -> dict[Place, ConditionEdit]:
    """As condições como a página as mandou, pela posição da pergunta ou da opção. Os índices
    dizem a ordem das regras; um campo que não é de condição fica de fora."""
    modes: dict[Place, str] = {}
    parts: dict[Place, dict[int, dict[str, list[str]]]] = {}
    for key in form:
        found = _KEY.fullmatch(key)
        if found is None:
            continue
        place = (int(found[1]), int(found[2]), None if found[3] is None else int(found[3]))
        values = [str(item) for item in form.getlist(key)]
        if found[4] == "mode":
            modes[place] = values[0].strip() if values else ""
        else:
            parts.setdefault(place, {}).setdefault(int(found[5]), {})[found[6]] = values
    return {
        place: ConditionEdit(
            mode=modes.get(place, "all"),
            rules=[RuleEdit.of_form(rule) for _, rule in sorted(parts.get(place, {}).items())],
        )
        for place in modes.keys() | parts.keys()
    }


def owners_of(question: QuestionEdit, i: int, j: int) -> list[tuple[Place, ConditionEdit | None]]:
    """Quem pode ter condição na pergunta: ela e, nas de escolha, cada opção (a primeira, a da
    pergunta)."""
    owners: list[tuple[Place, ConditionEdit | None]] = [((i, j, None), question.show_when)]
    if question.choice:
        owners += [((i, j, k), option.show_when) for k, option in enumerate(question.options)]
    return owners


def targets_before(edit: FormEdit, i: int, j: int) -> dict[str, Target]:
    """As perguntas com nome que vêm antes da pergunta `j` da etapa `i`, pelo nome."""
    found: dict[str, Target] = {}
    for si, step in enumerate(edit.steps):
        for sj, question in enumerate(step.questions):
            if (si, sj) >= (i, j):
                return found
            target = target_of(question)
            if target.name:
                found.setdefault(target.name, target)
    return found


# --- a frase ----------------------------------------------------------------------------------


def appears_when(condition: ConditionEdit | None, targets: Mapping[str, Target]) -> str | None:
    """A condição em linguagem natural, como a leitura e o resumo da pergunta a dizem; None sem
    condição e enquanto uma regra ainda não diz tudo (`app.forms.definition.condition_text`)."""
    if condition is None:
        return None
    rules = [(rule.question, rule.operator, rule.values) for rule in condition.rules]
    return sentence(condition.mode, rules, targets)


def sentences_of(steps: Sequence[StepDoc]) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """A condição em linguagem natural de cada pergunta com condição (pelo nome) e de cada opção com
    condição (pelo nome da pergunta e o valor da opção), de uma definição."""
    targets: dict[str, Target] = {}
    for step in steps:
        for question in step.questions:
            options = tuple((o.value, o.label) for o in question.options or ())
            targets.setdefault(
                question.name, Target(question.name, question.label, question.kind, options)
            )
    asked: dict[str, str] = {}
    offered: dict[tuple[str, str], str] = {}
    for step in steps:
        for question in step.questions:
            if sentence := _sentence_of_doc(question.show_when, targets):
                asked[question.name] = sentence
            for option in question.options or ():
                if sentence := _sentence_of_doc(option.show_when, targets):
                    offered[question.name, option.value] = sentence
    return asked, offered


def _sentence_of_doc(condition: Any, targets: Mapping[str, Target]) -> str | None:
    if condition is None:
        return None
    return condition_sentence(condition.model_dump(), targets)


# --- a página ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ValueChoice:
    """Uma opção da regra, na lista ou nas caixas, e se está marcada."""

    value: str
    label: str
    checked: bool


@dataclass(frozen=True)
class RuleView:
    """Uma regra como a página a desenha."""

    index: int
    name: str  # o nome dela no formulário (`...show_when.rules.0`)
    at: str  # os números da operação (`0:5:0` ou `0:5:1:0`)
    question: str
    questions: tuple[tuple[str, str], ...]  # (o nome, o texto) das perguntas da lista
    operator: str
    operators: tuple[tuple[str, str], ...]
    widget: str  # `select`, `checks`, `text` ou `lines`
    choices: tuple[ValueChoice, ...]
    text: str
    notes: tuple[str, ...]  # o que a regra aponta e não existe mais


@dataclass(frozen=True)
class ConditionView:
    """A condição de uma pergunta ou de uma opção como a página a desenha."""

    name: str
    at: str  # os números das operações de condição (`0:5` ou `0:5:1`)
    title: str
    empty: str
    adding: str  # o nome do botão de adicionar, para o leitor de tela
    exists: bool
    mode: str
    rules: tuple[RuleView, ...]
    sentence: str | None
    can_add_rule: bool
    add_condition_id: str
    add_rule_id: str
    compact: bool = False  # sem condição, só o botão (as opções, que são muitas)
    conditional_options: int = 0  # na pergunta: quantas opções têm condição

    @property
    def summary(self) -> str | None:
        """O que o resumo da pergunta diz: quando ela aparece, e quantas opções têm condição."""
        options = self.conditional_options
        counted = (
            f"{options} {'opção' if options == 1 else 'opções'} com condição" if options else ""
        )
        return " · ".join(part for part in (self.sentence, counted) if part) or None


@dataclass(frozen=True)
class ReviewNote:
    """Uma regra que aponta para o que não existe mais, na lista do alto das perguntas."""

    where: str
    message: str
    target: str  # o id do campo da regra


@dataclass(frozen=True)
class ConditionsView:
    """As condições do editor, por posição (`of`), e o que a operadora precisa revisar."""

    blocks: Mapping[Place, ConditionView]
    notes: tuple[ReviewNote, ...]
    reviewing: frozenset[str]  # as perguntas com regra a revisar (`etapa.pergunta`)
    modes: tuple[tuple[str, str], ...] = MODES

    def of(self, i: int, j: int, k: int | None = None) -> ConditionView:
        return self.blocks[i, j, k]


def field_id(name: str) -> str:
    """O id do campo na página, pelo nome dele (`form_editor.field_id`)."""
    return "f-" + name.replace(".", "-")


def conditions_view(edit: FormEdit) -> ConditionsView:
    """Tudo que a página precisa para desenhar as condições do editor, uma por pergunta e por opção
    de escolha, com as perguntas que cada regra pode olhar (as anteriores)."""
    everything: dict[str, Target] = {}
    for step in edit.steps:
        for question in step.questions:
            target = target_of(question)
            if target.name:
                everything.setdefault(target.name, target)
    blocks: dict[Place, ConditionView] = {}
    notes: list[ReviewNote] = []
    reviewing: set[str] = set()
    earlier: dict[str, Target] = {}
    for i, step in enumerate(edit.steps):
        for j, question in enumerate(step.questions):
            owners = owners_of(question, i, j)
            before = len(notes)
            for place, condition in owners:
                blocks[place] = _block(place, condition, earlier, everything, notes)
            blocks[i, j, None] = replace(
                blocks[i, j, None],
                conditional_options=sum(1 for _, condition in owners[1:] if condition is not None),
            )
            if len(notes) > before:
                reviewing.add(f"{i}.{j}")
            target = target_of(question)
            if target.name:
                earlier.setdefault(target.name, target)
    return ConditionsView(blocks, tuple(notes), frozenset(reviewing))


def _block(
    place: Place,
    condition: ConditionEdit | None,
    earlier: Mapping[str, Target],
    everything: Mapping[str, Target],
    notes: list[ReviewNote],
) -> ConditionView:
    i, j, k = place
    base = base_name(place)
    at = f"{i}:{j}" if k is None else f"{i}:{j}:{k}"
    if k is None:
        where = f"Etapa {i + 1} · pergunta {j + 1}"
        title, empty = "Mostrar esta pergunta quando…", "Hoje esta pergunta aparece sempre."
        adding = f"Adicionar uma condição à pergunta {j + 1} da etapa {i + 1}"
    else:
        where = f"Etapa {i + 1} · pergunta {j + 1} · opção {k + 1}"
        title, empty = "Mostrar esta opção quando…", "Hoje esta opção aparece sempre."
        adding = f"Adicionar uma condição à opção {k + 1} da pergunta {j + 1} da etapa {i + 1}"
    rules = tuple(
        _rule_view(index, rule, base, at, where, earlier, everything, notes)
        for index, rule in enumerate(condition.rules if condition else ())
    )
    return ConditionView(
        name=base,
        at=at,
        title=title,
        empty=empty,
        adding=adding,
        exists=condition is not None,
        mode=condition.mode if condition else "all",
        rules=rules,
        sentence=appears_when(condition, everything),
        can_add_rule=condition is not None and len(condition.rules) < MAX_CONDITION_RULES,
        add_condition_id=field_id(f"{base}.add_condition"),
        add_rule_id=field_id(f"{base}.add_rule"),
        compact=k is not None,
    )


def _rule_view(
    index: int,
    rule: RuleEdit,
    base: str,
    at: str,
    where: str,
    earlier: Mapping[str, Target],
    everything: Mapping[str, Target],
    notes: list[ReviewNote],
) -> RuleView:
    name = f"{base}.rules.{index}"
    target = earlier.get(rule.question)
    kind = target.kind if target else None
    messages: list[str] = []

    shown_texts = [item.shown for item in earlier.values()]
    questions = [
        (
            item.name,
            item.shown if shown_texts.count(item.shown) == 1 else f"{item.shown} ({item.name})",
        )
        for item in earlier.values()
    ]
    if rule.question and target is None:
        later = everything.get(rule.question)
        if later is None:
            questions.append((rule.question, f"«{rule.question}» (não existe mais)"))
            messages.append(
                f"Não há mais uma pergunta com o nome «{rule.question}»: ela foi renomeada ou "
                "removida. Escolha a pergunta da regra de novo."
            )
        else:
            questions.append((rule.question, f"{later.shown} (vem depois)"))
            messages.append(
                f"A pergunta «{later.shown}» agora é esta ou vem depois dela, e uma regra só olha "
                "perguntas anteriores. Escolha outra pergunta ou mova esta para depois dela."
            )

    valid = operators_for(kind)
    operators = [(item, operator_label(item, kind)) for item in valid]
    if rule.operator not in valid:
        operators.append(
            (rule.operator, f"{operator_label(rule.operator, kind)} (não serve nesta pergunta)")
        )

    chosen = rule.values if rule.operator == "in" else rule.values[:1]
    choices: tuple[ValueChoice, ...] = ()
    text = ""
    if target is not None and target.choice:
        widget = "checks" if rule.operator == "in" else "select"
        known = {value for value, _ in target.options}
        listed = [ValueChoice(value, label, value in chosen) for value, label in target.options]
        gone = [value for value in chosen if value not in known]
        listed += [ValueChoice(value, f"«{value}» (não existe mais)", True) for value in gone]
        choices = tuple(listed)
        if gone:
            messages.append(
                f"{_enumerated(gone)} não "
                f"{'é' if len(gone) == 1 else 'são'} mais "
                f"{'uma opção' if len(gone) == 1 else 'opções'} de «{target.shown}»: "
                "o valor foi renomeado ou a opção removida. Marque o valor da regra de novo."
            )
    elif rule.operator == "in":
        widget, text = "lines", "\n".join(rule.values)
    else:
        widget, text = "text", rule.values[0] if rule.values else ""

    for message in messages:
        notes.append(ReviewNote(f"{where} · regra {index + 1}", message, field_id(f"{name}.field")))
    return RuleView(
        index=index,
        name=name,
        at=f"{at}:{index}",
        question=rule.question,
        questions=tuple(questions),
        operator=rule.operator,
        operators=tuple(operators),
        widget=widget,
        choices=choices,
        text=text,
        notes=tuple(messages),
    )


def _enumerated(values: Sequence[str]) -> str:
    return ", ".join(f"«{value}»" for value in values)


# --- onde está o problema ---------------------------------------------------------------------


def place_of(parts: Sequence[str], at: int) -> str:
    """O campo do editor onde aparece o problema de um caminho da definição que tem `show_when` na
    posição `at` (`steps.0.questions.5.show_when.rules.0.value.1`): a regra, o campo dela ou, para o
    resto, a caixa da condição. Os problemas de um item da lista de valores vão para o campo."""
    base = ".".join(parts[: at + 1])
    rest = list(parts[at + 1 :])
    if rest[:1] == ["mode"]:
        return f"{base}.mode"
    if rest[:1] == ["rules"] and len(rest) > 1 and rest[1].isdigit():
        rule = f"{base}.rules.{rest[1]}"
        if len(rest) > 2 and rest[2] in RULE_PARTS:
            return f"{rule}.{rest[2]}"
        return rule
    return base


def where_words(rest: Sequence[str]) -> list[str]:
    """Como a operadora lê o que vem depois de `show_when` num caminho: "regra 1", "pergunta"."""
    words: list[str] = []
    if rest[:1] == ["mode"]:
        return ["regras que precisam valer"]
    if rest[:1] == ["rules"] and len(rest) > 1 and rest[1].isdigit():
        words.append(f"regra {int(rest[1]) + 1}")
        if len(rest) > 2 and rest[2] in RULE_PARTS:
            words.append(RULE_PARTS[rest[2]])
    return words
