"""As perguntas e as opções condicionais de uma definição: o formato, a conferência ao salvar e ao
publicar, e o avaliador que diz o que a lead vê. O formato em contexto está em
`app.forms.definition`; os modelos (`ConditionDoc`, `ConditionRuleDoc`) ficam em
`app.forms.definition.document`, com os demais.

O formato
---------

Uma pergunta ou uma opção de escolha pode ter `show_when`, a condição para ela aparecer:

    {"mode": "all", "rules": [{"field": "situacao", "operator": "in", "value": ["a", "b"]}]}

- `mode`: `all` (todas as regras valem) ou `any` (ao menos uma vale);
- `rules`: de 1 a 20 regras, cada uma com o `field` (o nome de uma pergunta anterior), o `operator`
  e o `value`:
  - `equals`: a resposta é igual ao `value`, um texto. Vale em texto e em escolha única;
  - `in`: a resposta é um dos itens do `value`, uma lista de textos; numa escolha múltipla, ao
    menos uma opção marcada está na lista;
  - `contains`: o texto da resposta tem o `value` (um texto) dentro, com maiúsculas e minúsculas
    como estão; numa escolha múltipla, o `value` é uma das opções marcadas. Vale em texto e em
    escolha múltipla.

Sem `show_when`, a pergunta ou a opção sempre aparece.

O que a lead vê
---------------

`Conditions.evaluate(respostas)` percorre as perguntas uma vez, na ordem da definição, e devolve a
`Visibility`: as perguntas visíveis e, para cada pergunta de escolha visível, as opções
disponíveis. É a única implementação da regra no servidor. O modelo das respostas, a leitura do
envio pela página e a gravação das respostas usam o resultado dela. O navegador reproduz o mesmo
algoritmo em `app/web/static/js/conditions.js`, sobre as perguntas que a página recebe
(`Conditions.page_data`); `tests_app/fixtures/conditions_cases.json` guarda os casos que os dois
lados precisam resolver igual, e os testes rodam o JavaScript contra eles e contra respostas
sorteadas. Quem muda a regra aqui muda o arquivo de lá no mesmo commit.

- A resposta é lida sem espaços nas pontas; texto em branco, ou nenhuma opção marcada, é pergunta
  não respondida. Numa escolha múltipla, um texto vale como a lista dessa opção.
- Uma regra olha a resposta da pergunta que `field` nomeia, e só das que apareceram: o que está
  escondido conta como não respondido, e uma regra sobre uma pergunta não respondida nunca vale.
  Por isso uma pergunta A escondida esconde a C que depende de A (num `any` com outra regra, a C
  ainda pode aparecer).
- Uma pergunta escondida perde a resposta: o modelo das respostas a descarta, e ela fica ausente
  das respostas gravadas e do webhook. Uma pergunta obrigatória só é exigida quando aparece.
- Uma opção escondida não pode ser escolhida: o envio com ela é recusado, com a mensagem no campo
  da pergunta. Para as regras seguintes, a opção escondida conta como não marcada.

Na conferência
--------------

`condition_problems` confere a definição ao salvar, importar e publicar, com o caminho do campo em
cada problema (`steps.1.questions.3.show_when.rules.0.field`):
- o `field` é uma pergunta que vem antes (e não a própria pergunta, nem uma que vem depois);
- `in` pede uma lista, e os outros operadores, um texto;
- `equals` não vale em escolha múltipla, e `contains` não vale em escolha única;
- quando a pergunta referida é de escolha, cada valor é uma opção dela; nas de texto, `equals` e
  `in` pedem o valor sem espaços nas pontas, porque a resposta é comparada sem eles;
- o e-mail, o nome e o telefone da identidade não têm condição: identificam a lead e sempre
  aparecem.

Uma pergunta de escolha obrigatória que mostra a lead só opções condicionais pode ficar sem opção
nenhuma para certas respostas: a conferência não vê isso, e quem escreve a definição garante que
sempre sobra uma opção.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from app.forms.definition.document import (
    ConditionDoc,
    ConditionRuleDoc,
    FormDocument,
    Problem,
    QuestionDoc,
    StepDoc,
)
from app.forms.definition.questions import CHOICE_KINDS, questions_of

# O papel de cada pergunta da identidade, como a mensagem o chama.
IDENTITY_ROLES = {"email": "o e-mail", "name": "o nome", "phone": "o telefone"}


# --- conferência ----------------------------------------------------------------------------


def condition_problems(definition: FormDocument) -> list[Problem]:
    """O que a estrutura não confere sozinha nas condições (`show_when`) das perguntas e das opções:
    as regras olham perguntas anteriores, com valores e operadores que combinam com elas, e a
    identidade da lead não tem condição. Vazio sem condições."""
    problems: list[Problem] = []
    names = {question.name for question in questions_of(definition.steps)}
    identity = {
        name: role
        for role in IDENTITY_ROLES
        if (name := getattr(definition.identity, role)) is not None
    }
    earlier: dict[str, QuestionDoc] = {}
    for i, step in enumerate(definition.steps):
        for j, question in enumerate(step.questions):
            at = f"steps.{i}.questions.{j}"
            if question.show_when is not None:
                problems += _condition_problems(
                    question.show_when, f"{at}.show_when", earlier, names
                )
                role = identity.get(question.name)
                if role is not None:
                    problems.append(
                        Problem(
                            f"{at}.show_when",
                            f"{IDENTITY_ROLES[role]} da lead a identifica e sempre aparece: "
                            "tire a condição",
                        )
                    )
            for k, option in enumerate(question.options or ()):
                if option.show_when is not None:
                    problems += _condition_problems(
                        option.show_when, f"{at}.options.{k}.show_when", earlier, names
                    )
            earlier.setdefault(question.name, question)
    return problems


def _condition_problems(
    condition: ConditionDoc, at: str, earlier: Mapping[str, QuestionDoc], names: set[str]
) -> list[Problem]:
    problems: list[Problem] = []
    for index, rule in enumerate(condition.rules):
        path = f"{at}.rules.{index}"
        target = earlier.get(rule.field)
        if target is None:
            message = (
                "a pergunta é esta ou vem depois dela: uma regra só olha respostas anteriores"
                if rule.field in names
                else "não há pergunta com este nome"
            )
            problems.append(Problem(f"{path}.field", message))
        problems += _rule_problems(rule, target, path)
    return problems


def _rule_problems(rule: ConditionRuleDoc, target: QuestionDoc | None, at: str) -> list[Problem]:
    """O operador e o valor da regra, e como combinam com a pergunta que ela olha (`target`)."""
    many = isinstance(rule.value, list)
    if rule.operator == "in" and not many:
        return [Problem(f"{at}.value", "o operador in compara com uma lista de valores")]
    if rule.operator != "in" and many:
        return [
            Problem(
                f"{at}.value", f"o operador {rule.operator} compara com um texto, e não uma lista"
            )
        ]
    if target is None:
        return []
    problems: list[Problem] = []
    if rule.operator == "equals" and target.kind == "choices":
        problems.append(
            Problem(
                f"{at}.operator",
                "equals não serve numa escolha múltipla: use contains (uma opção marcada) ou in "
                "(alguma de várias)",
            )
        )
    if rule.operator == "contains" and target.kind == "choice":
        problems.append(
            Problem(
                f"{at}.operator",
                "contains serve em texto e em escolha múltipla: numa escolha única use equals ou in",
            )
        )
    values = [rule.value] if isinstance(rule.value, str) else list(rule.value)
    options = {option.value for option in target.options or ()}
    for index, value in enumerate(values):
        place = f"{at}.value.{index}" if many else f"{at}.value"
        if target.kind in CHOICE_KINDS:
            if value not in options:
                problems.append(
                    Problem(place, f"«{value}» não é uma opção da pergunta {target.name}")
                )
        elif rule.operator != "contains" and value != value.strip():
            problems.append(
                Problem(
                    place, "tire os espaços do começo e do fim: a resposta é comparada sem eles"
                )
            )
    return problems


# --- o avaliador ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Visibility:
    """O que a lead vê de acordo com as respostas, na ordem da definição.

    - `visible` e `hidden`: os nomes das perguntas que aparecem e dos que ficam escondidos;
    - `options`: para cada pergunta de escolha visível, os valores das opções disponíveis (as que
      não têm condição, ou cuja condição vale)."""

    visible: tuple[str, ...]
    hidden: tuple[str, ...]
    options: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class _Node:
    """Uma pergunta como o avaliador a lê: o nome, o tipo, a condição e as opções com a delas."""

    name: str
    kind: str
    required: bool
    condition: ConditionDoc | None
    options: tuple[tuple[str, ConditionDoc | None], ...]


# Uma resposta como as regras a leem: um texto, as opções marcadas, ou None (não respondida).
Answer = str | tuple[str, ...] | None


def _normalized(kind: str, raw: Any) -> Answer:
    """A resposta sem os espaços das pontas; em branco ou fora do tipo, não respondida. Numa escolha
    múltipla, um texto vale como a lista dessa opção, sem repetir nenhuma."""
    if kind == "choices":
        items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list | tuple) else []
        chosen = dict.fromkeys(
            item.strip() for item in items if isinstance(item, str) and item.strip()
        )
        return tuple(chosen) or None
    if isinstance(raw, str):
        return raw.strip() or None
    return None


def _plain(condition: ConditionDoc) -> dict[str, Any]:
    """A condição como dado simples (JSON), na ordem em que a definição a escreve."""
    return condition.model_dump(mode="json")


def _matches(rule: ConditionRuleDoc, actual: Answer) -> bool:
    if actual is None:
        return False
    wanted = rule.value
    if rule.operator == "in":
        listed = wanted if isinstance(wanted, list) else [wanted]
        if isinstance(actual, tuple):
            return any(item in listed for item in actual)
        return actual in listed
    if not isinstance(wanted, str):
        return False
    if rule.operator == "equals":
        return actual == wanted
    return wanted in actual  # contains: o trecho no texto, ou a opção entre as marcadas


def _holds(condition: ConditionDoc | None, given: Mapping[str, Answer]) -> bool:
    if condition is None:
        return True
    results = (_matches(rule, given.get(rule.field)) for rule in condition.rules)
    return all(results) if condition.mode == "all" else any(results)


def _kept(answer: Answer, available: tuple[str, ...]) -> Answer:
    """A resposta sem as opções que não estão disponíveis."""
    if isinstance(answer, tuple):
        return tuple(item for item in answer if item in available) or None
    return answer if answer in available else None


class Conditions:
    """As condições de um formulário, prontas para avaliar. Veja o módulo."""

    def __init__(self, steps: Sequence[StepDoc]) -> None:
        self._nodes = tuple(
            _Node(
                name=question.name,
                kind=question.kind,
                required=question.required,
                condition=question.show_when,
                options=tuple(
                    (option.value, option.show_when) for option in question.options or ()
                ),
            )
            for question in questions_of(list(steps))
        )
        self.present = any(
            node.condition is not None or any(condition for _, condition in node.options)
            for node in self._nodes
        )
        # As obrigatórias que têm condição: o modelo das respostas só as exige quando aparecem, e
        # a página as mostra como obrigatórias (sem "(opcional)") assim que aparecem.
        self.required_when_shown = frozenset(
            node.name for node in self._nodes if node.required and node.condition is not None
        )

    @cached_property
    def page_data(self) -> list[dict[str, Any]]:
        """As perguntas como a página pública as recebe (`conditions.js`), na ordem da definição:
        o nome, o tipo, a condição e, nas de escolha, as opções com a delas. É o formato de
        `tests_app/fixtures/conditions_cases.json`: só o que o avaliador usa, sem rótulos nem
        respostas. Fica pronto uma vez por formulário; quem usa não o altera."""
        data: list[dict[str, Any]] = []
        for node in self._nodes:
            found: dict[str, Any] = {"name": node.name, "kind": node.kind}
            if node.condition is not None:
                found["show_when"] = _plain(node.condition)
            if node.kind in CHOICE_KINDS:
                found["options"] = [
                    {"value": value, **({"show_when": _plain(condition)} if condition else {})}
                    for value, condition in node.options
                ]
            data.append(found)
        return data

    def evaluate(self, answers: Mapping[str, Any]) -> Visibility:
        """O que aparece com estas respostas, como o envio as traz (texto, ou lista nas escolhas
        múltiplas) ou como o modelo as validou (None, se não respondida)."""
        given: dict[str, Answer] = {}  # as respostas que as regras seguintes enxergam
        visible: list[str] = []
        hidden: list[str] = []
        options: dict[str, tuple[str, ...]] = {}
        for node in self._nodes:
            if not _holds(node.condition, given):
                hidden.append(node.name)
                continue
            visible.append(node.name)
            answer = _normalized(node.kind, answers.get(node.name))
            if node.kind in CHOICE_KINDS:
                options[node.name] = tuple(
                    value for value, condition in node.options if _holds(condition, given)
                )
                answer = _kept(answer, options[node.name])
            if answer is not None:
                given[node.name] = answer
        return Visibility(tuple(visible), tuple(hidden), options)
