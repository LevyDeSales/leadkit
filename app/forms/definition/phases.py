"""As fases com datas do plano e a conferência declarativa dos valores por janela.

O plano segue o calendário real sem código por formulário:
- `phases.days` são os dias de cada fase, em ordem; a primeira começa no dia da geração, em São
  Paulo, e cada uma começa no dia seguinte ao fim da anterior;
- `phases.windows` é o bloco de janelas dos dados do formulário (`data`) que as fases cruzam;
- `phases.plan_field` é o campo lista do plano com um item por fase;
- um campo de opções fixas do plano, dentro dessa lista, liga cada valor a uma janela
  (`in_window`: `{valor: janela}`). Um valor ligado só cabe nas fases em que a janela dele está
  aberta; um valor sem ligação cabe em qualquer fase.

O motor calcula, fase a fase, a situação de cada janela e os valores permitidos. O prompt os recebe
em `phases` (`Calendar.on`), e a conferência do plano (`Calendar.check`) recusa o valor fora da
janela, com uma mensagem por valor e fase que cita só a posição e a regra, nunca o texto do plano.

Uma janela está aberta numa fase se as datas se cruzam: a fase termina no início da janela ou
depois, e começa no fim dela ou antes. O estado diz onde a fase cai na janela (`STATES`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from app.forms.dates import date_range, local_date
from app.forms.definition.document import (
    FormDocument,
    PhasesDoc,
    PlanDoc,
    PlanNodeDoc,
    Problem,
    WindowDoc,
    WindowsBlock,
    data_lists,
)

# O estado de uma janela numa fase, e como o prompt o conta ("Opção: está aberta e fecha nesta
# fase"). `open` e `closes` valem quando a janela já está aberta no primeiro dia da fase; `closes` e
# `opens_and_closes`, quando ela termina até o último dia.
STATES: dict[str, str] = {
    "upcoming": "ainda não abriu",
    "opens": "abre nesta fase",
    "open": "está aberta durante toda a fase",
    "closes": "está aberta e fecha nesta fase",
    "opens_and_closes": "abre e fecha nesta fase",
    "closed": "já fechou",
}
NOT_OPEN = frozenset({"upcoming", "closed"})


@dataclass(frozen=True)
class WindowStatus:
    """Uma janela vista de uma fase. `open`: a janela cruza a fase."""

    name: str
    label: str
    start: date
    end: date
    state: str
    text: str
    open: bool


@dataclass(frozen=True)
class PhaseStatus:
    """Uma fase do plano, como o prompt a recebe. `windows` e `allowed` são objetos de leitura:
    `windows` traz cada janela do bloco pelo nome; `allowed`, os valores permitidos de cada campo
    ligado a janelas, pelo nome do campo, na ordem das opções dele."""

    number: int
    first: date
    last: date
    period: str
    situation: str
    windows: Mapping[str, WindowStatus]
    allowed: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class LinkedField:
    """Um campo de opções fixas ligado a janelas. `trail` são os nomes dele a partir de um item da
    lista de fases; `windows`, a janela de cada valor ligado."""

    name: str
    trail: tuple[str, ...]
    values: tuple[str, ...]
    windows: Mapping[str, str]


def window_state(window: WindowDoc, first: date, last: date) -> str:
    """Onde a fase (de `first` a `last`) cai na janela."""
    if window.end < first:
        return "closed"
    if window.start > last:
        return "upcoming"
    already_open = window.start <= first
    closes_here = window.end <= last
    if already_open:
        return "closes" if closes_here else "open"
    return "opens_and_closes" if closes_here else "opens"


def _status(window: WindowDoc, first: date, last: date) -> WindowStatus:
    state = window_state(window, first, last)
    return WindowStatus(
        name=window.name,
        label=window.label or window.name,
        start=window.start,
        end=window.end,
        state=state,
        text=STATES[state],
        open=state not in NOT_OPEN,
    )


def _phases_of(numbers: list[int], *, within: bool = False) -> str:
    """ "a fase 1" e "as fases 1 e 2"; com `within`, "na fase 1" e "nas fases 1 e 2"."""
    names = [str(number) for number in numbers]
    if len(names) == 1:
        phrase = f"a fase {names[0]}"
    else:
        phrase = f"as fases {', '.join(names[:-1])} e {names[-1]}"
    return f"n{phrase}" if within else phrase


def _values_at(value: Any, trail: Sequence[str], at: str) -> Iterator[tuple[str, Any]]:
    """Os valores que o plano tem no campo de `trail` dentro de `value`, com a posição de cada um
    (`fases.1.acoes.0.tipo`). Atravessa as listas, e ignora o que o plano não tem."""
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _values_at(item, trail, f"{at}.{index}")
    elif not trail:
        yield at, value
    elif isinstance(value, Mapping) and trail[0] in value:
        yield from _values_at(value[trail[0]], trail[1:], f"{at}.{trail[0]}")


@dataclass(frozen=True)
class Calendar:
    """A configuração das fases de uma definição compilada."""

    days: tuple[int, ...]
    windows: tuple[WindowDoc, ...]
    plan_path: tuple[str, ...]  # os nomes da lista de fases no plano; vazio: sem conferência
    fields: tuple[LinkedField, ...]

    def on(self, today: date) -> list[PhaseStatus]:
        """As fases de um plano gerado em `today`, cada uma com a situação das janelas e os valores
        permitidos."""
        phases = []
        first = today
        for number, days in enumerate(self.days, start=1):
            last = first + timedelta(days=days - 1)
            windows = {window.name: _status(window, first, last) for window in self.windows}
            allowed = {
                field.name: tuple(
                    value
                    for value in field.values
                    if field.windows.get(value) is None or windows[field.windows[value]].open
                )
                for field in self.fields
            }
            situation = " ".join(f"{window.label}: {window.text}." for window in windows.values())
            phases.append(
                PhaseStatus(
                    number, first, last, date_range(first, last), situation, windows, allowed
                )
            )
            first = last + timedelta(days=1)
        return phases

    def check(self, plan: Mapping[str, Any], today: date) -> list[str]:
        """Os valores do plano (o documento JSON) fora da janela da fase deles, um problema por
        valor e fase, com as posições. Cita só a posição e a regra, nunca o texto do plano."""
        if not self.fields:
            return []
        phases = self.on(today)
        items: Any = plan
        for name in self.plan_path:
            items = items.get(name) if isinstance(items, Mapping) else None
        where = ".".join(self.plan_path)
        broken: dict[tuple[int, str, str], list[str]] = {}
        for index, item in enumerate(items[: len(phases)] if isinstance(items, list) else ()):
            for field in self.fields:
                for position, value in _values_at(item, field.trail, f"{where}.{index}"):
                    if value in field.windows and value not in phases[index].allowed[field.name]:
                        broken.setdefault((index, field.name, value), []).append(position)
        return [
            _message(phases, index, self._field(name), value, positions)
            for (index, name, value), positions in broken.items()
        ]

    def _field(self, name: str) -> LinkedField:
        return next(field for field in self.fields if field.name == name)

    def checker(self, clock: Callable[[], datetime]) -> Callable[[BaseModel, BaseModel], list[str]]:
        """A conferência no formato do contrato (`FormSpec.check_plan`): o plano já validado e as
        respostas entram, e os problemas saem. Lê o relógio a cada plano, como o prompt."""

        def check_plan(plan: BaseModel, answers: BaseModel) -> list[str]:
            return self.check(plan.model_dump(mode="json"), local_date(clock()))

        return check_plan


def _message(
    phases: list[PhaseStatus], index: int, field: LinkedField, value: str, positions: list[str]
) -> str:
    """Uma mensagem por valor e fase: onde, por quê e o que cabe no lugar."""
    phase = phases[index]
    window = phase.windows[field.windows[value]]
    elsewhere = [other.number for other in phases if value in other.allowed[field.name]]
    allowed = ", ".join(phase.allowed[field.name])
    rewrite = f"reescreva o conteúdo para um valor que caiba aqui ({allowed})" if allowed else ""
    head = (
        f"{', '.join(positions)} (fase {phase.number}): o valor {value} não cabe nesta fase "
        f"(janela {window.label}: {window.text})."
    )
    if elsewhere:
        where = _phases_of(elsewhere, within=True)
        fits = f"{value} só cabe {where}: leve esses itens para lá, mantendo as quantidades de cada campo"
        fits += f", ou {rewrite}." if rewrite else "."
    else:
        fits = f"{value} não cabe em nenhuma fase deste plano"
        fits += f": {rewrite}." if rewrite else "."
    return f"{head} {fits} Não troque só o valor: o conteúdo do item precisa combinar com ele."


# --- a definição ------------------------------------------------------------------------------


def linked_options(plan: PlanDoc) -> Iterator[tuple[str, tuple[str, ...], PlanNodeDoc]]:
    """Os campos de opções fixas ligados a janelas, em ordem da árvore: o caminho do campo na
    definição (`plan.fields.1.items.fields.0`), o caminho dos nomes dele no plano (os itens de uma
    lista não entram, e uma lista de opções leva o nome da lista) e o nó."""

    def visit(
        node: PlanNodeDoc, at: str, trail: tuple[str, ...]
    ) -> Iterator[tuple[str, tuple[str, ...], PlanNodeDoc]]:
        if node.kind == "options" and node.in_window is not None:
            yield at, trail, node
        elif node.kind == "list" and node.items is not None:
            yield from visit(node.items, f"{at}.items", trail)
        elif node.kind == "group":
            for k, child in enumerate(node.fields or ()):
                yield from visit(child, f"{at}.fields.{k}", (*trail, child.name or ""))

    for i, node in enumerate(plan.fields):
        yield from visit(node, f"plan.fields.{i}", (node.name or "",))


def _values_of(node: PlanNodeDoc, data: Mapping[str, Any]) -> tuple[str, ...] | None:
    """As opções do campo: os valores fixos, ou a união das listas dos dados. None se alguma lista
    não existe (outra conferência já recusou)."""
    return node.option_values(data_lists(data))


def _locate(plan: PlanDoc, path: tuple[str, ...]) -> tuple[PlanNodeDoc | None, str | None]:
    """A lista de fases do plano pelo caminho dos nomes (só por grupos), ou o motivo de não achar."""
    fields: list[PlanNodeDoc] = plan.fields
    for k, step in enumerate(path):
        node = next((field for field in fields if field.name == step), None)
        if node is None:
            return None, f"não há um campo {step} no plano"
        if k == len(path) - 1:
            if node.kind != "list":
                return None, f"o campo {'.'.join(path)} do plano não é uma lista"
            return node, None
        if node.kind != "group":
            return None, f"o campo {step} do plano não é um grupo"
        fields = node.fields or []
    return None, None


def phases_problems(definition: FormDocument) -> list[Problem]:
    """O que a estrutura não confere sozinha nas fases: o bloco de janelas e a lista de fases
    existem e combinam, e as ligações entre valores e janelas apontam para o que existe."""
    phases = definition.phases
    problems: list[Problem] = []
    block: WindowsBlock | None = None
    path: tuple[str, ...] | None = None
    if phases is not None and phases.windows is not None:
        found = definition.data.get(phases.windows)
        if found is None:
            message = f"não há um bloco chamado {phases.windows} nos dados do formulário"
            problems.append(Problem("phases.windows", message))
        elif not isinstance(found, WindowsBlock):
            message = f"o bloco {phases.windows} dos dados do formulário não é de janelas"
            problems.append(Problem("phases.windows", message))
        else:
            block = found
    if phases is not None and phases.plan_field is not None:
        steps = tuple(phases.plan_field.split("."))
        node, reason = _locate(definition.plan, steps)
        if reason is not None:
            problems.append(Problem("phases.plan_field", reason))
        elif node is not None and not node.min_items == node.max_items == len(phases.days):
            count = len(phases.days)
            problems.append(
                Problem(
                    "phases.plan_field",
                    f"a lista {phases.plan_field} do plano precisa ter exatamente {count} itens, "
                    f"um por fase: use {count} no mínimo e no máximo de itens dela",
                )
            )
        else:
            path = steps
    return problems + _link_problems(definition, phases, block, path)


def _link_problems(
    definition: FormDocument,
    phases: PhasesDoc | None,
    block: WindowsBlock | None,
    path: tuple[str, ...] | None,
) -> list[Problem]:
    problems: list[Problem] = []
    taken: set[str] = set()
    windows = {window.name for window in block.windows} if block is not None else None
    for at, trail, node in linked_options(definition.plan):
        where = f"{at}.in_window"
        if phases is None:
            message = "defina as fases com datas (phases) para ligar valores a janelas"
        elif phases.windows is None:
            message = "defina em phases o bloco de janelas (windows) de onde saem as janelas"
        elif phases.plan_field is None:
            message = "defina em phases a lista do plano com um item por fase (plan_field)"
        elif path is not None and trail[: len(path)] != path:
            message = f"o campo precisa estar dentro de {phases.plan_field}, a lista das fases"
        else:
            message = ""
        if message:
            problems.append(Problem(where, message))
            continue
        if trail[-1] in taken:
            problems.append(
                Problem(
                    where,
                    f"outro campo ligado a janelas já se chama {trail[-1]}; o prompt lê os "
                    "valores permitidos pelo nome do campo",
                )
            )
        taken.add(trail[-1])
        values = _values_of(node, definition.data)
        for value, window in (node.in_window or {}).items():
            if values is not None and value not in values:
                problems.append(
                    Problem(f"{where}.{value}", "este valor não está nas opções do campo")
                )
            if windows is not None and window not in windows and phases is not None:
                message = f"não há uma janela chamada {window} no bloco {phases.windows}"
                problems.append(Problem(f"{where}.{value}", message))
    return problems


def build_calendar(definition: FormDocument) -> Calendar | None:
    """O calendário da definição, que já passou por `phases_problems`; None sem `phases`."""
    phases = definition.phases
    if phases is None:
        return None
    block = definition.data.get(phases.windows) if phases.windows is not None else None
    path = tuple(phases.plan_field.split(".")) if phases.plan_field is not None else ()
    fields = []
    for _, trail, node in linked_options(definition.plan):
        values = _values_of(node, definition.data) or ()
        fields.append(
            LinkedField(
                name=trail[-1],
                trail=trail[len(path) :],
                values=values,
                windows=dict(node.in_window or {}),
            )
        )
    return Calendar(
        days=tuple(phases.days),
        windows=tuple(block.windows) if isinstance(block, WindowsBlock) else (),
        plan_path=path,
        fields=tuple(fields) if path else (),
    )
