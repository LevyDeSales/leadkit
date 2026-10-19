"""O editor da pontuação no admin: as notas com fórmula, as faixas de cada nota e as regras de
prioridade e de persona, em campos, no lugar do JSON (ADR 0010: Jinja2 e HTMX). É a parte do editor
do rascunho que cuida de `scoring`; o resto está em `app.admin.form_editor`, que o chama. Os pontos
de cada opção ficam nas perguntas (`form_editor`), onde a operadora já escreve as opções.

O estado mora todo no formulário da página, como no resto do editor. Cada campo tem por nome o
caminho dele na definição (`scoring.scores.0.bands.levels.1.min`), e é por esse nome que um problema
da compilação volta para o campo, com a coluna da fórmula na mensagem. Incluir, remover, reordenar
e trocar o resultado de uma regra de persona não gravam nada: o botão manda o formulário inteiro
com a operação (`op`), e a página volta com a mudança aplicada. Só "Salvar o rascunho" compila e
grava.

- Uma nota (`ScoreEdit`) tem nome e fórmula e, se a operadora quiser, faixas (`BandsEdit`): o nome do
  nível e o do rótulo, que a nota passa a ter ao lado dela, e os níveis (`LevelEdit`) do maior mínimo
  para o menor. O último nível vale para o resto e fica sem mínimo.
- As regras (`RuleEdit`) valem em ordem, a primeira que bater. A de prioridade escolhe entre P1, P2
  e P3; a de persona, entre um texto e a resposta de uma pergunta de escolha única (`result`). A
  última pode ficar sem condição, o "senão".
- Uma regra ou um nível novo entra antes do último ("senão" e "resto"): ele continua por último, e a
  definição segue válida enquanto a operadora preenche o novo.
- As operações começam por `scoring-` (`scoring-add-score`, `scoring-up-level:0:2`,
  `scoring-remove-rule:priority:1`...). Uma operação desconhecida, ou de uma página velha que aponta
  para o que não existe mais, não muda nada.
- Sem nota e sem regra, o editor não escreve a chave `scoring`: um formulário sem pontuação continua
  sem ela.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from app.forms.definition.document import NAME_PATTERN
from app.forms.definition.formulas import FUNCTIONS

PRIORITIES = (("", "Escolha o resultado"), ("P1", "P1"), ("P2", "P2"), ("P3", "P3"))
RESULTS = (
    ("value", "Um texto"),
    ("answer", "A resposta de uma pergunta de escolha única"),
)
RULE_KINDS = ("priority", "persona")
RULE_NAMES = {"priority": "prioridade", "persona": "persona"}
RULE_TITLES = {"priority": "regra de prioridade", "persona": "regra de persona"}
# Cada tipo de regra: o título da caixa e o que a operadora precisa saber para escrevê-la.
RULE_SECTIONS = (
    (
        "priority",
        "Prioridade",
        (
            "Vale a primeira regra que bater, de cima para baixo. A prioridade (P1, P2 ou P3) fica "
            "só no admin e no webhook: nunca chega à lead. A última regra pode ficar sem condição: "
            "é o «senão», e as respostas de exemplo precisam chegar a uma prioridade."
        ),
    ),
    (
        "persona",
        "Persona",
        (
            "Vale a primeira regra que bater, de cima para baixo. O resultado é um texto ou a "
            "resposta de uma pergunta de escolha única, como a «maior preocupação». Fica só no "
            "admin e no webhook. A última regra pode ficar sem condição: é o «senão»."
        ),
    ),
)
FIELD_LABELS = {
    "name": "nome",
    "formula": "fórmula",
    "bands": "faixas",
    "bands.key": "nome do nível",
    "bands.label": "nome do rótulo",
    "bands.levels": "níveis",
    "min": "mínimo",
    "key": "chave",
    "label": "rótulo",
    "when": "condição",
    "value": "resultado",
    "result": "tipo do resultado",
    "answer": "pergunta",
}

_INTEGER = re.compile(r"-?\d{1,9}")
_DECIMAL = re.compile(r"-?\d{1,9}[.,]\d{1,6}")
_SCORE_KEY = re.compile(r"scoring\.scores\.(\d{1,3})\.(name|formula)")
_BANDS_KEY = re.compile(r"scoring\.scores\.(\d{1,3})\.bands\.(key|label)")
_LEVEL_KEY = re.compile(r"scoring\.scores\.(\d{1,3})\.bands\.levels\.(\d{1,3})\.(min|key|label)")
_RULE_KEY = re.compile(r"scoring\.(priority|persona)\.(\d{1,3})\.(when|value|result|answer)")
_OP = re.compile(
    r"scoring-(add|remove|up|down|refresh)-(score|bands|level|rule)((?::[a-z0-9]+){0,2})"
)
_SCORE_AT = re.compile(r"scoring\.scores\.(\d+)(?:\.(.+))?")
_LEVEL_AT = re.compile(r"bands\.levels\.(\d+)(?:\.(.+))?")
_RULE_AT = re.compile(r"scoring\.(priority|persona)(?:\.(\d+)(?:\.(.+))?)?")


class FormFields(Protocol):
    """O formulário que voltou do navegador (o `FormData` do Starlette)."""

    def __iter__(self) -> Iterator[str]: ...

    def getlist(self, key: str) -> list[Any]: ...


def field_id(name: str) -> str:
    """O id do campo na página, pelo nome dele (`scoring.scores.0.name` -> `f-scoring-scores-0-name`)."""
    return "f-" + name.replace(".", "-")


def _lines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _formula(text: str) -> str:
    """A fórmula como foi escrita, sem o fim de linha do navegador nem os espaços do fim: o que vem
    antes não muda, e a coluna que a compilação aponta é a do texto da operadora."""
    return _lines(text).rstrip()


def points_of(text: str) -> int | float | str | None:
    """Um número de pontos do formulário: inteiro, ou decimal (a vírgula vale como ponto); None em
    branco, ou o texto como veio, para a compilação apontar o problema no campo."""
    text = text.strip()
    if not text:
        return None
    if _INTEGER.fullmatch(text):
        return int(text)
    return float(text.replace(",", ".")) if _DECIMAL.fullmatch(text) else text


def add_score_id() -> str:
    return "op-scoring-add-score"


def add_bands_id(i: int) -> str:
    return f"op-scoring-{i}-add-bands"


def add_level_id(i: int) -> str:
    return f"op-scoring-{i}-add-level"


def add_rule_id(kind: str) -> str:
    return f"op-scoring-add-rule-{kind}"


# --- o editor ---------------------------------------------------------------------------------


@dataclass(eq=False)
class LevelEdit:
    """Um nível da faixa: vale a partir do mínimo. O último fica sem mínimo e vale para o resto."""

    min: str = ""
    key: str = ""
    label: str = ""

    def document(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        minimum = points_of(self.min)
        if minimum is not None:
            found["min"] = minimum
        return found | {"key": self.key.strip(), "label": self.label.strip()}


@dataclass(eq=False)
class BandsEdit:
    """As faixas de uma nota: os nomes que o nível e o rótulo ganham ao lado da nota, e os níveis."""

    key: str = ""
    label: str = ""
    levels: list[LevelEdit] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        return {
            "key": self.key.strip(),
            "label": self.label.strip(),
            "levels": [level.document() for level in self.levels],
        }


@dataclass(eq=False)
class ScoreEdit:
    """Uma nota como a operadora a escreve: o nome, a fórmula e, se tiver, as faixas."""

    name: str = ""
    formula: str = ""
    bands: BandsEdit | None = None

    def document(self) -> dict[str, Any]:
        found: dict[str, Any] = {"name": self.name.strip(), "formula": _formula(self.formula)}
        if self.bands is not None:
            found["bands"] = self.bands.document()
        return found


@dataclass(eq=False)
class RuleEdit:
    """Uma regra de prioridade ou de persona. O resultado é `value` (P1, P2 ou P3 na prioridade; um
    texto na persona) ou, só na persona, `answer`: a resposta de uma pergunta de escolha única.
    `result` diz qual dos dois a página mostra; o outro fica guardado até a página recarregar."""

    when: str = ""
    value: str = ""
    result: str = "value"
    answer: str = ""

    def document(self, kind: str) -> dict[str, Any]:
        found: dict[str, Any] = {}
        if self.when.strip():
            found["when"] = _formula(self.when)
        if kind == "persona" and self.result == "answer":
            if self.answer.strip():
                found["answer"] = self.answer.strip()
        elif self.value.strip():
            found["value"] = self.value.strip()
        return found


@dataclass(eq=False)
class ScoringEdit:
    """A pontuação como a operadora a edita."""

    # As operações do editor, no nome do campo `op` sem os números (`scoring-add-score`...): saem da
    # tabela `_OPERATIONS`, no fim do módulo, e o teste de cobertura da API de admin cobra uma linha da
    # matriz para cada uma.
    OPERATIONS: ClassVar[tuple[str, ...]] = ()

    scores: list[ScoreEdit] = field(default_factory=list)
    priority: list[RuleEdit] = field(default_factory=list)
    persona: list[RuleEdit] = field(default_factory=list)

    # --- leitura ------------------------------------------------------------------------------

    @classmethod
    def of_document(cls, scoring: Any) -> ScoringEdit:
        """O editor de uma definição guardada (que compila: a de um rascunho ou de uma versão)."""
        if not isinstance(scoring, Mapping):
            return cls()
        return cls(
            scores=[
                _score_of(score)
                for score in scoring.get("scores") or ()
                if isinstance(score, Mapping)
            ],
            priority=[
                _rule_of(rule)
                for rule in scoring.get("priority") or ()
                if isinstance(rule, Mapping)
            ],
            persona=[
                _rule_of(rule) for rule in scoring.get("persona") or () if isinstance(rule, Mapping)
            ],
        )

    @classmethod
    def of_form(cls, form: FormFields) -> ScoringEdit:
        """O editor como o formulário da página voltou. Os índices dizem a ordem; um campo que não
        é do editor fica de fora."""

        def first(key: str) -> str:
            values = form.getlist(key)
            return _lines(str(values[0])) if values else ""

        scores: dict[int, ScoreEdit] = {}
        bands: dict[int, BandsEdit] = {}
        levels: dict[tuple[int, int], LevelEdit] = {}
        rules: dict[str, dict[int, RuleEdit]] = {kind: {} for kind in RULE_KINDS}
        for key in form:
            if found := _SCORE_KEY.fullmatch(key):
                setattr(scores.setdefault(int(found[1]), ScoreEdit()), found[2], first(key))
            elif found := _BANDS_KEY.fullmatch(key):
                setattr(bands.setdefault(int(found[1]), BandsEdit()), found[2], first(key))
            elif found := _LEVEL_KEY.fullmatch(key):
                level = levels.setdefault((int(found[1]), int(found[2])), LevelEdit())
                setattr(level, found[3], first(key))
                bands.setdefault(int(found[1]), BandsEdit())
            elif found := _RULE_KEY.fullmatch(key):
                rule = rules[found[1]].setdefault(int(found[2]), RuleEdit())
                value = first(key)
                if found[3] == "result":
                    value = value if value in dict(RESULTS) else "value"
                setattr(rule, found[3], value)
        for (i, _), level in sorted(levels.items()):
            bands[i].levels.append(level)
        for i, band in bands.items():
            scores.setdefault(i, ScoreEdit()).bands = band
        return cls(
            scores=[scores[i] for i in sorted(scores)],
            priority=[rules["priority"][i] for i in sorted(rules["priority"])],
            persona=[rules["persona"][i] for i in sorted(rules["persona"])],
        )

    # --- o que vai para a definição -----------------------------------------------------------

    @property
    def empty(self) -> bool:
        return not (self.scores or self.priority or self.persona)

    def document(self) -> dict[str, Any] | None:
        """O bloco `scoring` no formato da definição: só o que difere do padrão. None sem nota e
        sem regra: o formulário não tem pontuação."""
        if self.empty:
            return None
        found: dict[str, Any] = {"scores": [score.document() for score in self.scores]}
        if self.priority:
            found["priority"] = [rule.document("priority") for rule in self.priority]
        if self.persona:
            found["persona"] = [rule.document("persona") for rule in self.persona]
        return found

    # --- consulta -----------------------------------------------------------------------------

    def rules(self, kind: str) -> list[RuleEdit]:
        """As regras de `priority` ou de `persona`. Levanta `KeyError` para outro nome."""
        return {"priority": self.priority, "persona": self.persona}[kind]

    def places(self) -> frozenset[str]:
        """Os nomes que a página desenha, campos e caixas: onde um problema pode aparecer."""
        found = {"scoring", "scoring.scores", "scoring.priority", "scoring.persona"}
        for i, score in enumerate(self.scores):
            at = f"scoring.scores.{i}"
            found |= {at, f"{at}.name", f"{at}.formula"}
            if score.bands is not None:
                found |= {f"{at}.bands", f"{at}.bands.key", f"{at}.bands.label"}
                found.add(f"{at}.bands.levels")
                for k in range(len(score.bands.levels)):
                    level = f"{at}.bands.levels.{k}"
                    found |= {level, f"{level}.min", f"{level}.key", f"{level}.label"}
        for kind in RULE_KINDS:
            for i, rule in enumerate(self.rules(kind)):
                at = f"scoring.{kind}.{i}"
                found |= {at, f"{at}.when"}
                if kind == "priority":
                    found.add(f"{at}.value")
                else:
                    found |= {
                        f"{at}.result",
                        f"{at}.{'answer' if rule.result == 'answer' else 'value'}",
                    }
        return frozenset(found)

    def place_of(self, path: str) -> str:
        """O campo ou a caixa onde um problema do caminho da definição aparece: o mais fundo que a
        página desenha (`scoring.scores.0.bands.levels.1.min`, o campo do mínimo)."""
        places = self.places()
        candidate = path
        while candidate not in places and "." in candidate:
            candidate = candidate.rsplit(".", 1)[0]
        return candidate if candidate in places else "scoring"

    def describe(self, place: str) -> str:
        """Onde está o problema, como a operadora lê: "Pontuação · nota porte · fórmula". `place` é
        o nome de um campo ou de uma caixa do editor."""
        title = "Pontuação"
        if place == "scoring.scores":
            return f"{title} · notas"
        if found := _SCORE_AT.fullmatch(place):
            i = int(found[1])
            name = self.scores[i].name.strip() if i < len(self.scores) else ""
            where = [title, f"nota {name or i + 1}"]
            rest = found[2] or ""
            if level := _LEVEL_AT.fullmatch(rest):
                where.append(f"nível {int(level[1]) + 1}")
                rest = level[2] or ""
            if rest:
                where.append(FIELD_LABELS.get(rest, rest))
            return " · ".join(where)
        if found := _RULE_AT.fullmatch(place):
            kind, number, rest = found[1], found[2], found[3]
            if number is None:
                return f"{title} · {RULE_TITLES[kind]}s"
            where = [title, f"{RULE_TITLES[kind]} {int(number) + 1}"]
            if rest:
                where.append(FIELD_LABELS.get(rest, rest))
            return " · ".join(where)
        return title

    # --- as operações -------------------------------------------------------------------------

    def apply(self, op: str) -> str | None:
        """Aplica uma operação e devolve o id do campo que recebe o foco. Uma operação
        desconhecida, ou de uma página velha que aponta para o que não existe mais, não muda
        nada."""
        found = _OP.fullmatch(op)
        operation = _OPERATIONS.get((found[1], found[2])) if found else None
        if found is None or operation is None:
            return None
        arguments = found[3].split(":")[1:] if found[3] else []
        try:
            return operation(self, *arguments)
        except (IndexError, KeyError, TypeError, ValueError):
            return None

    def _add_score(self) -> str:
        self.scores.append(ScoreEdit())
        return field_id(f"scoring.scores.{len(self.scores) - 1}.name")

    def _remove_score(self, i: str) -> str:
        self.scores.pop(int(i))
        return add_score_id()

    def _move_score(self, i: str, delta: int) -> str:
        return field_id(f"scoring.scores.{_move(self.scores, int(i), delta)}.name")

    def _add_bands(self, i: str) -> str:
        score = self.scores[int(i)]
        if score.bands is not None:
            raise TypeError(i)
        name = score.name.strip()
        named = re.fullmatch(NAME_PATTERN, name) is not None
        score.bands = BandsEdit(
            key=f"nivel_{name}" if named else "",
            label=f"rotulo_{name}" if named else "",
            levels=[LevelEdit()],
        )
        return field_id(f"scoring.scores.{i}.bands.key")

    def _remove_bands(self, i: str) -> str:
        score = self.scores[int(i)]
        if score.bands is None:
            raise TypeError(i)
        score.bands = None
        return add_bands_id(int(i))

    def _bands(self, i: str) -> BandsEdit:
        bands = self.scores[int(i)].bands
        if bands is None:
            raise TypeError(i)
        return bands

    def _add_level(self, i: str) -> str:
        levels = self._bands(i).levels
        at = max(len(levels) - 1, 0)  # antes do último, que vale para o resto
        levels.insert(at, LevelEdit())
        return field_id(f"scoring.scores.{i}.bands.levels.{at}.min")

    def _remove_level(self, i: str, k: str) -> str:
        self._bands(i).levels.pop(int(k))
        return add_level_id(int(i))

    def _move_level(self, i: str, k: str, delta: int) -> str:
        moved = _move(self._bands(i).levels, int(k), delta)
        return field_id(f"scoring.scores.{i}.bands.levels.{moved}.key")

    def _add_rule(self, kind: str) -> str:
        rules = self.rules(kind)
        at = len(rules) - 1 if rules and not rules[-1].when.strip() else len(rules)
        rules.insert(at, RuleEdit())  # antes do "senão", que fica por último
        return field_id(f"scoring.{kind}.{at}.when")

    def _remove_rule(self, kind: str, i: str) -> str:
        self.rules(kind).pop(int(i))
        return add_rule_id(kind)

    def _move_rule(self, kind: str, i: str, delta: int) -> str:
        return field_id(f"scoring.{kind}.{_move(self.rules(kind), int(i), delta)}.when")

    def _refresh_rule(self, kind: str, i: str) -> str:
        if kind != "persona":  # o resultado da prioridade é sempre P1, P2 ou P3
            raise TypeError(kind)
        if not 0 <= int(i) < len(self.persona):
            raise IndexError(i)
        return field_id(f"scoring.persona.{i}.result")


def _move(items: list[Any], index: int, delta: int) -> int:
    """Troca o item de lugar com o vizinho e devolve a posição nova; na ponta, nada muda. Levanta
    `IndexError` se o item não existe."""
    if not 0 <= index < len(items):
        raise IndexError(index)
    target = index + delta
    if 0 <= target < len(items):
        items[index], items[target] = items[target], items[index]
        return target
    return index


_OPERATIONS: dict[tuple[str, str], Callable[..., str | None]] = {
    ("add", "score"): ScoringEdit._add_score,
    ("remove", "score"): ScoringEdit._remove_score,
    ("up", "score"): lambda edit, i: edit._move_score(i, -1),
    ("down", "score"): lambda edit, i: edit._move_score(i, 1),
    ("add", "bands"): ScoringEdit._add_bands,
    ("remove", "bands"): ScoringEdit._remove_bands,
    ("add", "level"): ScoringEdit._add_level,
    ("remove", "level"): ScoringEdit._remove_level,
    ("up", "level"): lambda edit, i, k: edit._move_level(i, k, -1),
    ("down", "level"): lambda edit, i, k: edit._move_level(i, k, 1),
    ("add", "rule"): ScoringEdit._add_rule,
    ("remove", "rule"): ScoringEdit._remove_rule,
    ("up", "rule"): lambda edit, kind, i: edit._move_rule(kind, i, -1),
    ("down", "rule"): lambda edit, kind, i: edit._move_rule(kind, i, 1),
    ("refresh", "rule"): ScoringEdit._refresh_rule,
}
ScoringEdit.OPERATIONS = tuple(f"scoring-{verb}-{noun}" for verb, noun in _OPERATIONS)


def _score_of(document: Mapping[str, Any]) -> ScoreEdit:
    bands = document.get("bands")
    return ScoreEdit(
        name=_text(document.get("name")),
        formula=_text(document.get("formula")),
        bands=_bands_of(bands) if isinstance(bands, Mapping) else None,
    )


def _bands_of(document: Mapping[str, Any]) -> BandsEdit:
    return BandsEdit(
        key=_text(document.get("key")),
        label=_text(document.get("label")),
        levels=[
            LevelEdit(
                min=_text(level.get("min")),
                key=_text(level.get("key")),
                label=_text(level.get("label")),
            )
            for level in document.get("levels") or ()
            if isinstance(level, Mapping)
        ],
    )


def _rule_of(document: Mapping[str, Any]) -> RuleEdit:
    answer = document.get("answer")
    return RuleEdit(
        when=_text(document.get("when")),
        value=_text(document.get("value")),
        result="value" if answer is None else "answer",
        answer=_text(answer),
    )


# --- a página ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChoiceName:
    """Uma pergunta de escolha como a fórmula a vê: o nome, o rótulo, se é de escolha múltipla e as
    opções com os pontos (em branco: sem pontos)."""

    name: str
    label: str
    multiple: bool
    options: tuple[tuple[str, str], ...]  # (valor, pontos)

    @property
    def missing(self) -> tuple[str, ...]:
        """Os valores das opções que ainda não têm pontos: `pontos(nome)` só vale sem eles."""
        return tuple(value for value, points in self.options if not points.strip())


@dataclass(frozen=True)
class ScoringView:
    """O editor da pontuação como a página o desenha: o editor, as caixas abertas (as com problema
    e a do foco) e os nomes que uma fórmula pode usar, tirados do que está na página."""

    edit: ScoringEdit
    opened: frozenset[str]
    troubled: frozenset[str]  # as notas com algum problema do salvar
    choices: tuple[ChoiceName, ...]
    names: tuple[tuple[str, str], ...]  # (nome, o que é): as notas e os níveis das faixas
    functions: tuple[tuple[str, str], ...]  # (a chamada, o que faz)
    priorities: tuple[tuple[str, str], ...] = PRIORITIES
    results: tuple[tuple[str, str], ...] = RESULTS
    rule_sections: tuple[tuple[str, str, str], ...] = RULE_SECTIONS
    rule_names: Mapping[str, str] = field(default_factory=lambda: dict(RULE_NAMES))

    @property
    def singles(self) -> tuple[ChoiceName, ...]:
        """As perguntas de escolha única: o que uma regra de persona pode ler como resposta."""
        return tuple(choice for choice in self.choices if not choice.multiple)

    def answers(self, current: str) -> list[tuple[str, str]]:
        """As perguntas que uma regra de persona pode escolher, com uma opção em branco na frente.
        A que a regra já escolheu e a página não tem mais continua na lista, marcada, para a
        operadora ver o que mudou."""
        found = [("", "Escolha a pergunta")]
        found += [(c.name, f"{c.name} · {c.label}" if c.label else c.name) for c in self.singles]
        if current and all(current != value for value, _ in found):
            found.append((current, f"{current} (não é uma pergunta de escolha única)"))
        return found

    def field_id(self, name: str) -> str:
        return field_id(name)


def _names(edit: ScoringEdit) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    for score in edit.scores:
        name = score.name.strip()
        if name:
            found.append((name, "nota, um número"))
        if score.bands is not None:
            origin = f"da faixa de {name}" if name else "de uma faixa"
            if score.bands.key.strip():
                found.append((score.bands.key.strip(), f"chave do nível {origin}, um texto"))
            if score.bands.label.strip():
                found.append((score.bands.label.strip(), f"rótulo do nível {origin}, um texto"))
    return tuple(found)


def _functions() -> tuple[tuple[str, str], ...]:
    """As funções da linguagem (`FUNCTIONS`): a chamada e o que ela faz."""
    found = []
    for description in FUNCTIONS.values():
        call, _, explanation = description.partition(": ")
        found.append((call, explanation))
    return tuple(found)


def scoring_view(
    edit: ScoringEdit,
    choices: tuple[ChoiceName, ...],
    errors: Mapping[str, str],
    focus: str | None,
) -> ScoringView:
    opened: set[str] = set()
    troubled: set[str] = set()
    for i in range(len(edit.scores)):
        at = f"scoring.scores.{i}"
        if any(key == at or key.startswith(f"{at}.") for key in errors):
            troubled.add(at)
        if focus is not None and (focus == field_id(at) or focus.startswith(f"{field_id(at)}-")):
            opened.add(at)
    return ScoringView(
        edit=edit,
        opened=frozenset(opened | troubled),
        troubled=frozenset(troubled),
        choices=choices,
        names=_names(edit),
        functions=_functions(),
    )
