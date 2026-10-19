"""A pontuação declarativa de uma definição: pontos por opção, notas com fórmulas, faixas e regras
de prioridade e de persona. O formato está em `app.forms.definition`; a linguagem das fórmulas, em
`app.forms.definition.formulas`.

- `scoring_problems` confere o bloco ao salvar e ao publicar, sem avaliar nada: nomes, referências
  que existem, ciclos, tipos, faixas e regras. Cada problema cita o caminho do campo.
- `compile_scoring` monta a `Scoring` de uma definição que passou na conferência.
- `Scoring.run(respostas)` calcula as notas, a prioridade e a persona. As notas saem na ordem em que
  foram declaradas, cada uma seguida do nível e do rótulo da faixa dela; são calculadas na ordem
  das dependências. Vale a primeira regra de prioridade ou de persona que bate.
- `Scoring.score(respostas)` é o `FormSpec.score` do motor. Uma fórmula que falha com respostas
  que o exemplo não cobria (uma divisão por zero) não derruba o envio: o log diz o formulário e a
  nota, nunca as respostas, e o pedido segue sem pontuação.
- `example_problems` roda a pontuação com as respostas de exemplo, que precisam dar uma prioridade
  válida, quando há regras de prioridade.

As notas não têm tipo próprio: dão um número, e o que não é inteiro fica com as casas decimais até
uma fórmula arredondar. Sem resposta, uma pergunta de escolha opcional vale 0 ponto e 0 escolhas.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.forms import ScoreResult
from app.forms.definition.document import (
    FormDocument,
    Problem,
    RuleDoc,
    ScoreDoc,
)
from app.forms.definition.formulas import (
    BOOL,
    NUMBER,
    RESERVED_WORDS,
    TEXT,
    TYPE_NAMES,
    Env,
    Formula,
    FormulaError,
    QuestionInfo,
    Scope,
    parse,
)
from app.forms.definition.questions import CHOICE_KINDS, questions_of

logger = logging.getLogger(__name__)

PRIORITIES = ("P1", "P2", "P3")
PERSONA_MAX_LENGTH = 64  # a coluna `submissions.persona`
# Os nomes de um dicionário não servem de nome de nota: no PDF, `scores.items` leria o método.
_DICT_NAMES = frozenset(name for name in dir(dict) if not name.startswith("_"))
RESERVED_OUTPUTS = RESERVED_WORDS | _DICT_NAMES


# --- conferência --------------------------------------------------------------------------------


def _question_infos(definition: FormDocument) -> dict[str, QuestionInfo]:
    return {
        question.name: QuestionInfo(
            kind=question.kind,
            missing_points=tuple(
                option.value for option in question.options or () if option.points is None
            )
            if question.kind in CHOICE_KINDS
            else (),
        )
        for question in questions_of(definition.steps)
    }


def _outputs(scores: Sequence[ScoreDoc]) -> dict[str, tuple[int, str]]:
    """Os nomes que a pontuação cria: cada nota e, nas faixas, o nível e o rótulo. Cada um diz de
    qual nota vem (o índice) e o tipo."""
    found: dict[str, tuple[int, str]] = {}
    for index, score in enumerate(scores):
        found.setdefault(score.name, (index, NUMBER))
        if score.bands is not None:
            found.setdefault(score.bands.key, (index, TEXT))
            found.setdefault(score.bands.label, (index, TEXT))
    return found


def _scope(definition: FormDocument, scoring_scores: Sequence[ScoreDoc]) -> Scope:
    questions = _question_infos(definition)
    values = {name: TEXT for name, info in questions.items() if info.kind == "choice"}
    values |= {name: kind for name, (_, kind) in _outputs(scoring_scores).items()}
    return Scope(values=values, questions=questions)


@dataclass
class _Checked:
    """O que a conferência leu: as fórmulas válidas de cada nota (por índice) e de cada regra."""

    notes: dict[int, Formula]
    priority: dict[int, Formula]
    persona: dict[int, Formula]


def _formula(
    source: str | None, scope: Scope, expected: str, path: str, problems: list[Problem]
) -> Formula | None:
    """A fórmula lida e conferida; None, com o problema anotado, se não vale."""
    if source is None:
        return None
    try:
        formula = parse(source)
        found = formula.check(scope)
    except FormulaError as exc:
        problems.append(Problem(path, exc.detail))
        return None
    if found != expected:
        wanted = "um número" if expected == NUMBER else "verdadeiro ou falso"
        problems.append(
            Problem(path, f"a fórmula precisa dar {wanted}, mas dá {TYPE_NAMES[found]}")
        )
        return None
    return formula


def scoring_problems(definition: FormDocument) -> list[Problem]:
    """O que a estrutura do bloco `scoring` não confere sozinha: nomes, fórmulas, referências,
    ciclos, faixas e regras. Vazio sem pontuação."""
    scoring = definition.scoring
    if scoring is None:
        return []
    problems: list[Problem] = []
    _names(definition, problems)
    for index, score in enumerate(scoring.scores):
        if score.bands is not None:
            _bands(score, f"scoring.scores.{index}.bands", problems)
    scope = _scope(definition, scoring.scores)
    checked = _Checked(
        notes={
            index: formula
            for index, score in enumerate(scoring.scores)
            if (
                formula := _formula(
                    score.formula, scope, NUMBER, f"scoring.scores.{index}.formula", problems
                )
            )
        },
        priority=_rules(scoring.priority, "priority", scope, problems),
        persona=_rules(scoring.persona, "persona", scope, problems),
    )
    problems += _cycles(scoring.scores, checked.notes)
    return problems


def _names(definition: FormDocument, problems: list[Problem]) -> None:
    """Cada nome que a pontuação cria é novo: não repete outro, não é de uma pergunta nem uma
    palavra da linguagem ou de um dicionário (o PDF lê `scores.nome`)."""
    assert definition.scoring is not None
    questions = {question.name for question in questions_of(definition.steps)}
    taken: set[str] = set()

    def claim(name: str, path: str) -> None:
        if name in RESERVED_OUTPUTS:
            problems.append(Problem(path, f"{name} é uma palavra reservada; escolha outro nome"))
        elif name in questions:
            problems.append(Problem(path, f"{name} já é o nome de uma pergunta; escolha outro"))
        elif name in taken:
            problems.append(Problem(path, f"outra nota ou faixa já usa o nome {name}"))
        taken.add(name)

    for index, score in enumerate(definition.scoring.scores):
        claim(score.name, f"scoring.scores.{index}.name")
        if score.bands is not None:
            claim(score.bands.key, f"scoring.scores.{index}.bands.key")
            claim(score.bands.label, f"scoring.scores.{index}.bands.label")


def _bands(score: ScoreDoc, at: str, problems: list[Problem]) -> None:
    assert score.bands is not None
    levels = score.bands.levels
    keys: set[str] = set()
    previous: int | float | None = None
    for index, level in enumerate(levels):
        where = f"{at}.levels.{index}"
        if level.key in keys:
            problems.append(Problem(f"{where}.key", "outro nível da faixa já usa esta chave"))
        keys.add(level.key)
        if index == len(levels) - 1:
            if level.min is not None:
                problems.append(
                    Problem(f"{where}.min", "o último nível vale para o resto: tire o min")
                )
        elif level.min is None:
            problems.append(
                Problem(f"{where}.min", "só o último nível fica sem min; este precisa do dele")
            )
        elif previous is not None and level.min >= previous:
            problems.append(
                Problem(
                    f"{where}.min",
                    f"os níveis vão do maior min para o menor: use menos de {previous}",
                )
            )
        if level.min is not None:
            previous = level.min


def _rules(
    rules: Sequence[RuleDoc], kind: str, scope: Scope, problems: list[Problem]
) -> dict[int, Formula]:
    """A conferência das regras de prioridade (`priority`) ou de persona (`persona`); devolve as
    condições válidas por índice."""
    conditions: dict[int, Formula] = {}
    for index, rule in enumerate(rules):
        at = f"scoring.{kind}.{index}"
        last = index == len(rules) - 1
        if rule.when is None and not last:
            problems.append(
                Problem(at, "esta regra não tem condição e sempre vale: as seguintes nunca valem")
            )
        formula = _formula(rule.when, scope, BOOL, f"{at}.when", problems)
        if formula is not None:
            conditions[index] = formula
        if kind == "priority":
            _priority_result(rule, at, problems)
        else:
            _persona_result(rule, at, scope, problems)
    return conditions


def _priority_result(rule: RuleDoc, at: str, problems: list[Problem]) -> None:
    if rule.answer is not None:
        problems.append(Problem(f"{at}.answer", "a prioridade é P1, P2 ou P3: use value"))
    elif rule.value is None:
        problems.append(Problem(at, "a regra precisa do resultado: value (P1, P2 ou P3)"))
    if rule.value is not None and rule.value not in PRIORITIES:
        problems.append(Problem(f"{at}.value", "use P1, P2 ou P3"))


def _persona_result(rule: RuleDoc, at: str, scope: Scope, problems: list[Problem]) -> None:
    if rule.value is not None and rule.answer is not None:
        problems.append(Problem(at, "use value ou answer, não os dois"))
    elif rule.value is None and rule.answer is None:
        problems.append(Problem(at, "a regra precisa do resultado: value ou answer"))
    if rule.value is not None and not 0 < len(rule.value) <= PERSONA_MAX_LENGTH:
        problems.append(
            Problem(f"{at}.value", f"use de 1 a {PERSONA_MAX_LENGTH} caracteres na persona")
        )
    if rule.answer is not None:
        info = scope.questions.get(rule.answer)
        if info is None:
            problems.append(Problem(f"{at}.answer", "não existe pergunta com este nome"))
        elif info.kind != "choice":
            problems.append(Problem(f"{at}.answer", "precisa ser uma pergunta de escolha única"))


def _dependencies(
    scores: Sequence[ScoreDoc], formulas: Mapping[int, Formula]
) -> dict[int, list[int]]:
    """De qual nota cada nota depende: as que a fórmula cita, direto ou pelo nível de uma faixa."""
    owner = {name: index for name, (index, _) in _outputs(scores).items()}
    return {
        index: sorted({owner[name] for name in formula.names() if name in owner})
        for index, formula in formulas.items()
    }


def _cycles(scores: Sequence[ScoreDoc], formulas: Mapping[int, Formula]) -> list[Problem]:
    """Uma nota que depende de si mesma, direto ou por outras. Cada ciclo sai uma vez, na primeira
    nota dele."""
    graph = _dependencies(scores, formulas)
    problems: list[Problem] = []
    state: dict[int, int] = {}  # 1: no caminho; 2: terminada
    reported: set[frozenset[int]] = set()

    def visit(index: int, path: list[int]) -> None:
        state[index] = 1
        for dependency in graph.get(index, ()):
            if state.get(dependency) == 1:
                cycle = path[path.index(dependency) :]
                if frozenset(cycle) not in reported:
                    reported.add(frozenset(cycle))
                    names = " → ".join(scores[i].name for i in [*cycle, dependency])
                    first = min(cycle)
                    problems.append(
                        Problem(f"scoring.scores.{first}.formula", f"referência circular: {names}")
                    )
            elif dependency not in state:
                visit(dependency, [*path, dependency])
        state[index] = 2

    for index in sorted(graph):
        if index not in state:
            visit(index, [index])
    return sorted(problems, key=lambda problem: problem.path)


# --- a pontuação compilada ----------------------------------------------------------------------


class ScoringFailure(Exception):
    """Uma fórmula falhou ao calcular (divisão por zero, por exemplo): `path` é o campo dela na
    definição, e `detail`, o motivo, sem nenhuma resposta."""

    def __init__(self, path: str, detail: str, note: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail
        self.note = note


@dataclass(frozen=True)
class _Level:
    min: int | float | None
    key: str
    label: str


@dataclass(frozen=True)
class _Note:
    name: str
    path: str
    formula: Formula
    key: str | None
    label: str | None
    levels: tuple[_Level, ...]


@dataclass(frozen=True)
class _Rule:
    when: Formula | None
    value: str | None
    answer: str | None


@dataclass(frozen=True)
class ScoreRun:
    """O resultado de uma pontuação e qual regra decidiu a prioridade e a persona (o índice na
    lista, a partir de 0; None se nenhuma bateu)."""

    result: ScoreResult
    priority_rule: int | None
    persona_rule: int | None


def _clean(value: Any) -> Any:
    """Um número inteiro por natureza (50.0) sai como inteiro."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


class Scoring:
    """A pontuação de uma versão do formulário, pronta para calcular. Veja o módulo."""

    def __init__(
        self,
        *,
        label: str,
        notes: tuple[_Note, ...],
        order: tuple[int, ...],
        points: Mapping[str, Mapping[str, int | float]],
        singles: frozenset[str],
        priority: tuple[_Rule, ...],
        persona: tuple[_Rule, ...],
    ) -> None:
        self.label = label
        self._notes = notes
        self._order = order
        self._points = points
        self._singles = singles
        self._priority = priority
        self._persona = persona

    @property
    def has_priority(self) -> bool:
        return bool(self._priority)

    @property
    def has_persona(self) -> bool:
        return bool(self._persona)

    @property
    def names(self) -> tuple[str, ...]:
        """Os nomes que a pontuação cria em `scores`, na ordem em que saem."""
        found: list[str] = []
        for note in self._notes:
            found.append(note.name)
            if note.key is not None and note.label is not None:
                found += [note.key, note.label]
        return tuple(found)

    def released(self, notes: Iterable[str]) -> frozenset[str]:
        """Os nomes de `scores` das notas escolhidas: cada nota e, nas faixas, o nível e o rótulo
        dela. É o que o PDF e a prévia podem mostrar (`pdf.scores`)."""
        chosen = set(notes)
        found: set[str] = set()
        for note in self._notes:
            if note.name in chosen:
                found.add(note.name)
                if note.key is not None and note.label is not None:
                    found |= {note.key, note.label}
        return frozenset(found)

    def run(self, answers: Any) -> ScoreRun:
        """Calcula a pontuação das respostas validadas. Levanta `ScoringFailure`."""
        values: dict[str, Any] = {name: getattr(answers, name) for name in self._singles}
        env = Env(
            values=values,
            points=lambda question: self._points_of(answers, question),
            count=lambda question: len(getattr(answers, question) or ()),
        )
        for index in self._order:
            note = self._notes[index]
            try:
                value = _clean(note.formula.evaluate(env))
            except FormulaError as exc:
                raise ScoringFailure(note.path, exc.detail, note.name) from None
            values[note.name] = value
            if note.key is not None and note.label is not None:
                level = next((lv for lv in note.levels if lv.min is None or value >= lv.min), None)
                assert level is not None  # o último nível vale para o resto
                values[note.key], values[note.label] = level.key, level.label
        scores: dict[str, Any] = {}
        for note in self._notes:
            scores[note.name] = values[note.name]
            if note.key is not None and note.label is not None:
                scores[note.key], scores[note.label] = values[note.key], values[note.label]
        priority, priority_rule = self._decide(self._priority, "priority", env, answers)
        persona, persona_rule = self._decide(self._persona, "persona", env, answers)
        return ScoreRun(
            ScoreResult(scores=scores, priority=priority, persona=persona),
            priority_rule,
            persona_rule,
        )

    def score(self, answers: Any) -> ScoreResult:
        """O `FormSpec.score` do motor: a pontuação, ou nenhuma se uma fórmula falhar (o pedido
        não se perde por causa dela)."""
        try:
            return self.run(answers).result
        except ScoringFailure as failure:
            logger.error(
                "pontuação de %s falhou na nota %s (%s)", self.label, failure.note, failure.detail
            )
            return ScoreResult()

    def _points_of(self, answers: Any, question: str) -> int | float:
        chosen = getattr(answers, question)
        table = self._points[question]
        if chosen is None:
            return 0
        if isinstance(chosen, list):
            total: int | float = 0
            for value in chosen:
                total += table[value]
            return total
        return table[chosen]

    def _decide(
        self, rules: tuple[_Rule, ...], kind: str, env: Env, answers: Any
    ) -> tuple[str | None, int | None]:
        for index, rule in enumerate(rules):
            try:
                if rule.when is not None and not rule.when.evaluate(env):
                    continue
            except FormulaError as exc:
                raise ScoringFailure(f"scoring.{kind}.{index}.when", exc.detail, kind) from None
            value = rule.value if rule.answer is None else getattr(answers, rule.answer)
            if value is not None:
                return value, index
        return None, None


def compile_scoring(definition: FormDocument, *, label: str) -> Scoring | None:
    """A pontuação de uma definição que passou em `scoring_problems`; None sem pontuação."""
    scoring = definition.scoring
    if scoring is None:
        return None
    graph_formulas = {index: parse(score.formula) for index, score in enumerate(scoring.scores)}
    graph = _dependencies(scoring.scores, graph_formulas)
    order: list[int] = []
    done: set[int] = set()

    def visit(index: int) -> None:
        if index in done:
            return
        done.add(index)
        for dependency in graph[index]:
            visit(dependency)
        order.append(index)

    for index in range(len(scoring.scores)):
        visit(index)
    notes = tuple(
        _Note(
            name=score.name,
            path=f"scoring.scores.{index}.formula",
            formula=graph_formulas[index],
            key=score.bands.key if score.bands else None,
            label=score.bands.label if score.bands else None,
            levels=tuple(_Level(lv.min, lv.key, lv.label) for lv in score.bands.levels)
            if score.bands
            else (),
        )
        for index, score in enumerate(scoring.scores)
    )
    points = {
        question.name: {option.value: option.points for option in question.options or ()}
        for question in questions_of(definition.steps)
        if question.kind in CHOICE_KINDS
    }
    singles = frozenset(
        question.name for question in questions_of(definition.steps) if question.kind == "choice"
    )

    def rules(items: Sequence[RuleDoc]) -> tuple[_Rule, ...]:
        return tuple(
            _Rule(parse(rule.when) if rule.when is not None else None, rule.value, rule.answer)
            for rule in items
        )

    return Scoring(
        label=label,
        notes=notes,
        order=tuple(order),
        points=points,  # type: ignore[arg-type] - as opções sem pontos nunca são lidas: a conferência exige `pontos` completos
        singles=singles,
        priority=rules(scoring.priority),
        persona=rules(scoring.persona),
    )


def example_problems(scoring: Scoring | None, example_answers: Any) -> list[Problem]:
    """O que a pontuação diz das respostas de exemplo: uma fórmula que falha (divisão por zero) e,
    com regras de prioridade, uma prioridade que não é P1, P2 ou P3 por falta de regra que bata."""
    if scoring is None:
        return []
    try:
        run = scoring.run(example_answers)
    except ScoringFailure as failure:
        return [Problem(failure.path, f"com as respostas de exemplo, {failure.detail}")]
    if scoring.has_priority and run.result.priority not in PRIORITIES:
        return [
            Problem(
                "scoring.priority",
                "nenhuma regra de prioridade vale para as respostas de exemplo: acrescente uma "
                "regra final sem condição",
            )
        ]
    return []
