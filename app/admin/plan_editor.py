"""O editor do modelo do plano no admin: a árvore de campos (texto, lista, grupo e opções fixas), os
limites, os campos internos e as validações declarativas, em campos, no lugar do JSON (ADR 0010:
Jinja2 e HTMX). É a parte do editor do rascunho que cuida de `plan`; o resto está em
`app.admin.form_editor`, que o chama.

O estado mora todo no formulário da página, como no resto do editor. Cada campo tem por nome o
caminho dele na definição (`plan.fields.2.items.fields.0.max_length`), e é por esse nome que um
problema da compilação volta para o campo. Incluir, remover, reordenar e trocar o tipo não gravam
nada: o botão manda o formulário inteiro com a operação (`op`), e a página volta com a mudança
aplicada. Só "Salvar o rascunho" compila e grava.

- Um campo de opções fixas escolhe os valores entre os fixos e uma ou mais listas dos dados
  (`values_from`, caixas de marcar): a definição leva o nome da lista, se for uma só, ou a lista de
  nomes. A ordem delas é a dos números ao lado das caixas (`values_order.<lista>`); uma marcada sem
  número vem depois das numeradas, na ordem da página.
- Um campo da árvore é um `NodeEdit`. Os itens de uma lista são um nó sem nome (`items`), do tipo
  texto, grupo ou opções fixas.
- Trocar o tipo guarda o que o campo já tinha, mas a definição só leva o que vale no tipo novo.
- O endereço de um nó nas operações é o nome dele no formulário (`plan.fields.2.items`): o
  destino de `plan-add-field` é `plan` (a raiz) ou um grupo, e o de `plan-remove`, `plan-up`,
  `plan-down` e `plan-refresh`, um campo ou os itens de uma lista.
- As regras de expressões proibidas são `RuleEdit`; cada uma escolhe os campos de texto da árvore
  pelo caminho dos nomes (`fases.foco`).
- As fases com datas (`PhasesEdit`, a chave `phases` da definição) moram ao lado do plano: os dias de
  cada fase, a lista do plano com um item por fase e o bloco de janelas dos dados. A ligação de cada
  valor de um campo de opções fixas a uma janela fica no próprio campo (`in_window`): uma linha
  `valor = janela` por valor ligado.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from app.forms.definition.document import Problem

NODE_KINDS = (
    ("text", "Texto"),
    ("list", "Lista"),
    ("group", "Grupo de campos"),
    ("options", "Opções fixas"),
)
ITEM_KINDS = tuple(kind for kind in NODE_KINDS if kind[0] != "list")
SCOPES = (
    ("all", "Todos os textos do plano"),
    ("only", "Só nos campos marcados"),
    ("except", "Em todos menos nos campos marcados"),
)
# O que cada tipo de campo mostra além do tipo e da descrição, pelo nome no formulário.
KIND_FIELDS = {
    "text": (
        "min_length",
        "max_length",
        "min_paragraphs",
        "forbidden_pattern.pattern",
        "forbidden_pattern.message",
    ),
    "list": ("min_items", "max_items"),
    "group": ("model",),
    "options": ("values", "values_from", "in_window"),
}
NUMBER_FIELDS = ("min_length", "max_length", "min_paragraphs", "min_items", "max_items")
FIELD_LABELS = {
    "name": "nome",
    "kind": "tipo",
    "label": "rótulo",
    "description": "descrição",
    "internal": "campo interno",
    "min_length": "tamanho mínimo",
    "max_length": "tamanho máximo",
    "min_paragraphs": "mínimo de parágrafos",
    "forbidden_pattern": "padrão proibido",
    "forbidden_pattern.pattern": "padrão proibido",
    "forbidden_pattern.message": "mensagem do padrão proibido",
    "min_items": "mínimo de itens",
    "max_items": "máximo de itens",
    "model": "nome do grupo",
    "values": "valores",
    "values_from": "lista dos dados",
    "in_window": "janela de cada valor",
    "items": "itens",
    "fields": "campos",
    "expressions": "expressões",
    "scope": "onde vale",
}
RULE_FIELDS = ("expressions", "scope", "fields")
# Os campos das fases com datas e como a página os chama.
PHASES_FIELDS = ("days", "plan_field", "windows")
PHASES_LABELS = {
    "days": "dias de cada fase",
    "plan_field": "lista de fases do plano",
    "windows": "bloco de janelas",
}
BAD_LINK_LINE = "use uma linha por valor, no formato valor = janela"

_UNIT = r"(?:fields\.\d{1,3}|items)"
# O endereço de um nó: `plan` (a raiz) seguido de campos e itens, com um teto de profundidade.
_ADDRESS = rf"plan(?:\.{_UNIT}){{0,20}}"
_NODE_KEY = re.compile(
    rf"plan((?:\.{_UNIT}){{1,20}})\.("
    r"name|kind|label|description|internal|min_length|max_length|min_paragraphs|min_items|max_items"
    r"|model|values|values_from|values_order\.[a-z][a-z0-9_]{0,63}|in_window"
    r"|forbidden_pattern\.pattern|forbidden_pattern\.message)"
)
_RULE_KEY = re.compile(r"plan\.forbidden\.(\d{1,3})\.(expressions|scope|fields)")
_DAYS_SEPARATOR = re.compile(r"[\s,;]+")
# Os verbos das operações, `plan-<verbo>`: as expressões regulares de `apply` saem destas tuplas, e
# `PlanEdit.OPERATIONS` também. Um verbo novo só entra no `apply` por aqui, e então ele aparece em
# `OPERATIONS`, onde o teste de cobertura da API de admin cobra uma linha da matriz para ele.
_NODE_VERBS = ("add-field", "remove", "up", "down", "refresh")  # sobre o campo de um endereço
_RULE_VERBS = ("add-rule", "remove-rule")  # sobre as regras de texto proibido
_NODE_OP = re.compile("plan-(" + "|".join(_NODE_VERBS) + rf"):({_ADDRESS})")
_RULE_OP = re.compile("plan-(" + "|".join(_RULE_VERBS) + r")(?::(\d{1,3}))?")
_INTEGER = re.compile(r"-?\d{1,9}")
_FIELD_UNIT = re.compile(r"fields\.(\d{1,3})")


class FormFields(Protocol):
    """O formulário que voltou do navegador (o `FormData` do Starlette)."""

    def __iter__(self) -> Iterator[str]: ...

    def getlist(self, key: str) -> list[Any]: ...


def field_id(name: str) -> str:
    """O id do campo na página, pelo nome dele (`plan.fields.0.name` -> `f-plan-fields-0-name`)."""
    return "f-" + name.replace(".", "-")


def _lines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _number(text: str) -> int | str | None:
    """Um número do formulário: inteiro, None em branco, ou o texto como veio, para a compilação
    apontar o problema no campo."""
    text = text.strip()
    if not text:
        return None
    return int(text) if _INTEGER.fullmatch(text) else text


def _names(source: Any) -> list[str]:
    """Os nomes de um `values_from` da definição: um nome, ou a lista de nomes."""
    if isinstance(source, str):
        return [source]
    return [str(name) for name in source] if isinstance(source, list) else []


def _ranked(names: list[str], positions: Mapping[str, str]) -> list[str]:
    """As listas marcadas na ordem dos números que a operadora pôs ao lado delas. As sem número
    (ou com algo que não é número) vêm depois das numeradas; números iguais e sem número seguem a
    ordem da página."""

    def rank(item: tuple[int, str]) -> tuple[int, int, int]:
        index, name = item
        position = positions.get(name, "").strip()
        return (0, int(position), index) if _INTEGER.fullmatch(position) else (1, 0, index)

    return [name for _, name in sorted(enumerate(names), key=rank)]


def _each_line(text: str) -> list[str]:
    """As linhas com texto, sem os espaços das pontas."""
    return [line.strip() for line in _lines(text).split("\n") if line.strip()]


def _links_text(links: Any) -> str:
    """As ligações de valor a janela de um campo, uma linha `valor = janela` por valor."""
    if not isinstance(links, Mapping):
        return ""
    return "\n".join(f"{value} = {window}" for value, window in links.items())


def _units(address: str) -> list[str]:
    """As partes de um endereço depois de `plan`: `plan.fields.2.items` -> `fields.2`, `items`."""
    return re.findall(_UNIT, address)


def _address(units: list[str]) -> str:
    return ".".join(["plan", *units])


# --- o editor ---------------------------------------------------------------------------------


@dataclass(eq=False)
class NodeEdit:
    """Um campo do modelo do plano, ou os itens de uma lista, com os textos como a operadora os
    escreveu."""

    name: str = ""
    kind: str = "text"
    label: str = ""  # como o admin mostra o campo na tela do pedido; em branco, o nome
    description: str = ""
    internal: bool = False
    min_length: str = ""
    max_length: str = ""
    min_paragraphs: str = ""
    pattern: str = ""
    pattern_message: str = ""
    min_items: str = ""
    max_items: str = ""
    model: str = ""
    values: str = ""  # os valores fixos, um por linha
    values_from: list[str] = field(default_factory=list)  # as listas dos dados; nenhuma: os fixos
    in_window: str = ""  # a janela de cada valor ligado, uma linha `valor = janela`
    items: NodeEdit | None = None
    fields: list[NodeEdit] = field(default_factory=list)

    @classmethod
    def of_document(cls, node: Mapping[str, Any]) -> NodeEdit:
        pattern = node.get("forbidden_pattern")
        pattern = pattern if isinstance(pattern, Mapping) else {}
        items, fields = node.get("items"), node.get("fields")
        return cls(
            name=_text(node.get("name")),
            kind=str(node.get("kind", "text")),
            label=_text(node.get("label")),
            description=_text(node.get("description")),
            internal=bool(node.get("internal", False)),
            min_length=_text(node.get("min_length")),
            max_length=_text(node.get("max_length")),
            min_paragraphs=_text(node.get("min_paragraphs")),
            pattern=_text(pattern.get("pattern")),
            pattern_message=_text(pattern.get("message")),
            min_items=_text(node.get("min_items")),
            max_items=_text(node.get("max_items")),
            model=_text(node.get("model")),
            values="\n".join(str(value) for value in node.get("values") or ()),
            values_from=_names(node.get("values_from")),
            in_window=_links_text(node.get("in_window")),
            items=cls.of_document(items) if isinstance(items, Mapping) else None,
            fields=[cls.of_document(child) for child in fields or () if isinstance(child, Mapping)],
        )

    def fill(self) -> None:
        """Dá ao tipo o que ele pede para existir: uma lista nasce com itens de texto, e um grupo,
        com um campo de texto."""
        if self.kind == "list" and self.items is None:
            self.items = NodeEdit()
        if self.kind == "group" and not self.fields:
            self.fields = [NodeEdit()]

    @property
    def shown(self) -> tuple[str, ...]:
        """Os campos do tipo, além do tipo e da descrição."""
        return KIND_FIELDS.get(self.kind, ())

    def document(self, *, item: bool = False) -> dict[str, Any]:
        """O nó no formato da definição: só o que vale no tipo dele e o que difere do padrão."""
        found: dict[str, Any] = {}
        if not item and self.name.strip():
            found["name"] = self.name.strip()
        found["kind"] = self.kind
        if self.label.strip() and not item:
            found["label"] = self.label.strip()
        if self.description.strip():
            found["description"] = _lines(self.description).strip()
        if self.internal and not item:
            found["internal"] = True
        if self.kind == "text":
            for key in ("min_length", "max_length", "min_paragraphs"):
                value = _number(getattr(self, key))
                if value is not None:
                    found[key] = value
            if self.pattern.strip():
                pattern: dict[str, Any] = {"pattern": self.pattern}
                if self.pattern_message.strip():
                    pattern["message"] = _lines(self.pattern_message).strip()
                found["forbidden_pattern"] = pattern
        elif self.kind == "list":
            for key in ("min_items", "max_items"):
                value = _number(getattr(self, key))
                if value is not None:
                    found[key] = value
            if self.items is not None:
                found["items"] = self.items.document(item=True)
        elif self.kind == "group":
            if self.model.strip():
                found["model"] = self.model.strip()
            found["fields"] = [child.document() for child in self.fields]
        else:
            if names := self.sources():
                found["values_from"] = names[0] if len(names) == 1 else names
            else:
                found["values"] = _each_line(self.values)
            links, _ = self.links()
            if links:
                found["in_window"] = links
        return found

    def sources(self) -> list[str]:
        """Os nomes das listas dos dados escolhidas, na ordem, sem os espaços das pontas."""
        return [name.strip() for name in self.values_from if name.strip()]

    def links(self) -> tuple[dict[str, str], list[str]]:
        """As ligações de valor a janela escritas no campo, e os problemas de escrita (em
        português): uma linha sem `=`, com o valor ou a janela em branco, ou um valor em duas
        linhas. A definição leva as linhas que se leem; o editor para o salvar nas outras."""
        links: dict[str, str] = {}
        problems: list[str] = []
        for line in _each_line(self.in_window):
            value, equals, window = (part.strip() for part in line.partition("="))
            if not equals or not value or not window:
                problems.append(BAD_LINK_LINE)
            elif value in links:
                problems.append(f"o valor {value} aparece em mais de uma linha")
            else:
                links[value] = window
        return links, problems


@dataclass(eq=False)
class RuleEdit:
    """Uma regra de expressões proibidas: as expressões (uma por linha), onde ela vale e, se vale
    só em alguns campos ou em todos menos alguns, os campos."""

    expressions: str = ""
    scope: str = "all"
    fields: list[str] = field(default_factory=list)

    def document(self) -> dict[str, Any]:
        found: dict[str, Any] = {"expressions": _each_line(self.expressions)}
        if self.scope != "all":
            found["scope"] = self.scope
            found["fields"] = list(self.fields)
        return found


@dataclass(eq=False)
class PhasesEdit:
    """As fases com datas como a operadora as edita: os dias de cada fase (números separados por
    vírgula), a lista do plano com um item por fase e o bloco de janelas dos dados. Tudo em branco:
    o plano não tem fases com datas."""

    days: str = ""
    plan_field: str = ""
    windows: str = ""

    @classmethod
    def of_document(cls, phases: Any) -> PhasesEdit:
        if not isinstance(phases, Mapping):
            return cls()
        return cls(
            days=", ".join(str(day) for day in phases.get("days") or ()),
            plan_field=_text(phases.get("plan_field")),
            windows=_text(phases.get("windows")),
        )

    def document(self) -> dict[str, Any] | None:
        """O bloco `phases` da definição; None com os três campos em branco."""
        if not (self.days.strip() or self.plan_field.strip() or self.windows.strip()):
            return None
        found: dict[str, Any] = {
            "days": [_number(part) for part in _DAYS_SEPARATOR.split(self.days.strip()) if part]
        }
        if self.plan_field.strip():
            found["plan_field"] = self.plan_field.strip()
        if self.windows.strip():
            found["windows"] = self.windows.strip()
        return found


@dataclass(eq=False)
class PlanEdit:
    """O modelo do plano como a operadora o edita."""

    # As operações do editor, no nome do campo `op` sem o endereço nem o número. Saem das mesmas
    # tuplas de verbos de que o `apply` monta as expressões regulares (`_NODE_VERBS` e `_RULE_VERBS`),
    # então uma operação nova tem de passar por aqui, e o teste de cobertura da API de admin cobra uma
    # linha da matriz para cada uma.
    OPERATIONS: ClassVar[tuple[str, ...]] = tuple(
        f"plan-{verb}" for verb in (*_NODE_VERBS, *_RULE_VERBS)
    )

    description: str = ""
    fields: list[NodeEdit] = field(default_factory=list)
    rules: list[RuleEdit] = field(default_factory=list)
    phases: PhasesEdit = field(default_factory=PhasesEdit)

    # --- leitura ------------------------------------------------------------------------------

    @classmethod
    def of_document(cls, plan: Any, phases: Any = None) -> PlanEdit:
        """O editor de uma definição guardada (que compila: a de um rascunho ou de uma versão):
        o modelo do plano (`plan`) e as fases com datas (`phases`)."""
        if not isinstance(plan, Mapping):
            return cls(phases=PhasesEdit.of_document(phases))
        rules = []
        for rule in plan.get("forbidden") or ():
            if isinstance(rule, Mapping):
                rules.append(
                    RuleEdit(
                        expressions="\n".join(str(item) for item in rule.get("expressions") or ()),
                        scope=str(rule.get("scope", "all")),
                        fields=[str(item) for item in rule.get("fields") or ()],
                    )
                )
        return cls(
            description=_text(plan.get("description")),
            fields=[
                NodeEdit.of_document(node)
                for node in plan.get("fields") or ()
                if isinstance(node, Mapping)
            ],
            rules=rules,
            phases=PhasesEdit.of_document(phases),
        )

    @classmethod
    def of_form(cls, form: FormFields) -> PlanEdit:
        """O editor como o formulário da página voltou. Os índices dizem a ordem; um campo que não
        é do editor fica de fora."""

        def first(key: str) -> str:
            values = form.getlist(key)
            return _lines(str(values[0])) if values else ""

        attrs: dict[tuple[str, ...], dict[str, str | list[str]]] = {}
        known: set[tuple[str, ...]] = set()
        rules: dict[int, RuleEdit] = {}
        orders: dict[tuple[str, ...], dict[str, str]] = {}
        for key in form:
            if found := _NODE_KEY.fullmatch(key):
                units = tuple(_units(found[1]))
                known.update(units[:size] for size in range(1, len(units) + 1))
                if found[2].startswith("values_order."):
                    orders.setdefault(units, {})[found[2].split(".", 1)[1]] = first(key)
                    continue
                attrs.setdefault(units, {})[found[2]] = (
                    "on"
                    if found[2] == "internal"
                    else [str(value) for value in form.getlist(key)]
                    if found[2] == "values_from"
                    else first(key)
                )
            elif found := _RULE_KEY.fullmatch(key):
                rule = rules.setdefault(int(found[1]), RuleEdit())
                if found[2] == "fields":
                    rule.fields = [str(value) for value in form.getlist(key)]
                else:
                    setattr(rule, found[2], first(key))

        def build(at: tuple[str, ...]) -> NodeEdit:
            node = NodeEdit()
            for key, value in attrs.get(at, {}).items():
                if key == "internal":
                    node.internal = True
                elif key == "forbidden_pattern.pattern":
                    node.pattern = value
                elif key == "forbidden_pattern.message":
                    node.pattern_message = str(value)
                else:
                    setattr(node, key, value)
            node.values_from = _ranked(node.sources(), orders.get(at, {}))
            positions = sorted(
                int(found[1])
                for unit in known
                if len(unit) == len(at) + 1
                and unit[: len(at)] == at
                and (found := _FIELD_UNIT.fullmatch(unit[-1]))
            )
            node.fields = [build((*at, f"fields.{position}")) for position in positions]
            if (*at, "items") in known:
                node.items = build((*at, "items"))
            node.fill()
            return node

        positions = sorted(
            int(found[1])
            for unit in known
            if len(unit) == 1 and (found := _FIELD_UNIT.fullmatch(unit[0]))
        )
        return cls(
            description=first("plan.description"),
            fields=[build((f"fields.{position}",)) for position in positions],
            rules=[rules[index] for index in sorted(rules)],
            phases=PhasesEdit(**{key: first(f"phases.{key}") for key in PHASES_FIELDS}),
        )

    # --- o que vai para a definição -----------------------------------------------------------

    def document(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        if self.description.strip():
            found["description"] = _lines(self.description).strip()
        found["fields"] = [node.document() for node in self.fields]
        if self.rules:
            found["forbidden"] = [rule.document() for rule in self.rules]
        return found

    # --- consulta -----------------------------------------------------------------------------

    def text_paths(self) -> list[str]:
        """Os caminhos dos campos de texto da árvore, pelos nomes (`titulo`, `fases.foco`): os
        itens de uma lista não entram. Um campo sem nome ainda não tem caminho."""
        found: list[str] = []

        def visit(node: NodeEdit, trail: tuple[str, ...]) -> None:
            if node.kind == "text":
                found.append(".".join(trail))
            elif node.kind == "list" and node.items is not None:
                visit(node.items, trail)
            elif node.kind == "group":
                for child in node.fields:
                    if child.name.strip():
                        visit(child, (*trail, child.name.strip()))

        for node in self.fields:
            if node.name.strip():
                visit(node, (node.name.strip(),))
        return found

    def nodes(self) -> Iterator[tuple[str, NodeEdit, bool]]:
        """Cada nó da árvore com o endereço dele e se é o item de uma lista, em ordem."""

        def visit(node: NodeEdit, at: str, item: bool) -> Iterator[tuple[str, NodeEdit, bool]]:
            yield at, node, item
            if node.kind == "list" and node.items is not None:
                yield from visit(node.items, f"{at}.items", True)
            for j, child in enumerate(node.fields if node.kind == "group" else ()):
                yield from visit(child, f"{at}.fields.{j}", False)

        for i, node in enumerate(self.fields):
            yield from visit(node, f"plan.fields.{i}", False)

    def list_paths(self) -> list[str]:
        """Os caminhos dos campos lista do plano que cabem como lista de fases: os que se chega
        pelos nomes, só por grupos (`fases`, `roteiro.fases`)."""
        found: list[str] = []

        def visit(node: NodeEdit, trail: tuple[str, ...]) -> None:
            if node.kind == "list":
                found.append(".".join(trail))
            elif node.kind == "group":
                for child in node.fields:
                    if child.name.strip():
                        visit(child, (*trail, child.name.strip()))

        for node in self.fields:
            if node.name.strip():
                visit(node, (node.name.strip(),))
        return found

    def problems(self) -> list[Problem]:
        """O que o editor não consegue escrever na definição: uma ligação de valor a janela mal
        escrita. O caminho é o do campo na definição (`plan.fields.2.in_window`)."""
        return [
            Problem(f"{at}.in_window", message)
            for at, node, _ in self.nodes()
            if node.kind == "options"
            for message in node.links()[1][:1]
        ]

    def places(self) -> frozenset[str]:
        """Os nomes que a página desenha, campos e caixas: onde um problema pode aparecer."""
        found = {"plan", "plan.description", "plan.fields", "plan.forbidden", "phases"}
        found |= {f"phases.{key}" for key in PHASES_FIELDS}
        for at, node, item in self.nodes():
            found.add(at)
            found |= {f"{at}.kind", f"{at}.description"}
            if not item:
                found |= {f"{at}.name", f"{at}.label", f"{at}.internal"}
            found |= {f"{at}.{name}" for name in node.shown}
            if node.kind == "group":
                found.add(f"{at}.fields")
        for r in range(len(self.rules)):
            found |= {f"plan.forbidden.{r}", *(f"plan.forbidden.{r}.{key}" for key in RULE_FIELDS)}
        return frozenset(found)

    def place_of(self, path: str) -> str:
        """O campo ou a caixa onde um problema do caminho da definição aparece: o mais fundo que a
        página desenha (`plan.fields.0.values.1` -> `plan.fields.0.values`)."""
        places = self.places()
        if path == "phases" or path.startswith("phases."):
            key = path.split(".")[1] if "." in path else ""
            return f"phases.{key}" if key in PHASES_FIELDS else "phases"
        candidate = path
        while candidate not in places and "." in candidate:
            candidate = candidate.rsplit(".", 1)[0]
        return candidate if candidate in places else "plan"

    def describe(self, place: str) -> str:
        """Onde está o problema, como a operadora lê: "Modelo do plano · fases › nome · tamanho
        máximo". `place` é o nome de um campo ou de uma caixa do editor."""
        title = "Modelo do plano"
        if place == "phases" or place.startswith("phases."):
            key = place.split(".")[1] if "." in place else ""
            return " · ".join(
                ["Fases com datas", *([PHASES_LABELS[key]] if key in PHASES_LABELS else [])]
            )
        if rule := re.fullmatch(r"plan\.forbidden\.(\d+)(?:\.(.+))?", place):
            where = [title, f"regra de expressões {int(rule[1]) + 1}"]
            return " · ".join(where + ([FIELD_LABELS[rule[2]]] if rule[2] in FIELD_LABELS else []))
        if place == "plan.description":
            return f"{title} · descrição"
        nodes = list(self.nodes())
        deepest = max(
            (at for at, _, _ in nodes if place == at or place.startswith(f"{at}.")),
            key=len,
            default="",
        )
        if not deepest:
            return title
        trail = []
        for at, node, item in nodes:
            if deepest == at or deepest.startswith(f"{at}."):
                if not item:
                    trail.append(node.name.strip() or f"campo {int(at.rsplit('.', 1)[-1]) + 1}")
                elif at == deepest:
                    trail.append("itens")
        where = [title, " › ".join(trail)]
        rest = place[len(deepest) :].lstrip(".")
        if rest:
            where.append(FIELD_LABELS.get(rest, rest))
        return " · ".join(where)

    # --- as operações -------------------------------------------------------------------------

    def apply(self, op: str) -> str | None:
        """Aplica uma operação (`plan-add-field:plan.fields.4`, `plan-up:plan.fields.1`...) e
        devolve o id do campo que recebe o foco. Uma operação desconhecida, ou de uma página velha
        que aponta para o que não existe mais, não muda nada."""
        if found := _NODE_OP.fullmatch(op):
            try:
                return self._on_node(found[1], found[2])
            except (IndexError, TypeError):
                return None
        if found := _RULE_OP.fullmatch(op):
            if found[1] == "add-rule" and found[2] is None:
                self.rules.append(RuleEdit())
                return field_id(f"plan.forbidden.{len(self.rules) - 1}.expressions")
            if found[1] == "remove-rule" and found[2] is not None:
                index = int(found[2])
                if 0 <= index < len(self.rules):
                    self.rules.pop(index)
                    return ADD_RULE_ID
        return None

    def _locate(self, address: str) -> tuple[NodeEdit | None, list[NodeEdit] | None, int]:
        """O nó do endereço (None: a raiz), a lista de irmãos dele e a posição nela (-1: os itens
        de uma lista, que não têm irmãos). Levanta `IndexError` ou `TypeError` para o que não
        existe."""
        node: NodeEdit | None = None
        siblings: list[NodeEdit] | None = None
        index = -1
        for unit in _units(address):
            container = node.fields if node is not None else self.fields
            if unit == "items":
                if node is None or node.kind != "list" or node.items is None:
                    raise TypeError(unit)
                node, siblings, index = node.items, None, -1
            else:
                position = int(unit.split(".")[1])
                if node is not None and node.kind != "group":
                    raise TypeError(unit)
                node, siblings, index = container[position], container, position
        return node, siblings, index

    def _on_node(self, operation: str, address: str) -> str | None:
        node, siblings, index = self._locate(address)
        if operation == "add-field":
            container = (
                self.fields if node is None else node.fields if node.kind == "group" else None
            )
            if container is None:
                return None
            container.append(NodeEdit())
            return field_id(f"{address}.fields.{len(container) - 1}.name")
        if node is None:
            return None
        if operation == "refresh":
            node.fill()
            return field_id(f"{address}.kind")
        if siblings is None:  # os itens de uma lista não mudam de lugar nem saem
            return None
        if operation == "remove":
            siblings.pop(index)
            parent = address.rsplit(".fields.", 1)[0]
            return add_field_id(parent)
        target = index + (-1 if operation == "up" else 1)
        if 0 <= target < len(siblings):
            siblings[index], siblings[target] = siblings[target], siblings[index]
            index = target
        parent = address.rsplit(".fields.", 1)[0]
        return field_id(f"{parent}.fields.{index}.name")


ADD_RULE_ID = "op-plan-add-rule"


def add_field_id(address: str) -> str:
    """O id do botão de incluir campo no grupo (ou na raiz) deste endereço."""
    return "op-" + address.replace(".", "-") + "-add-field"


# --- a página ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceChoice:
    """Uma lista dos dados na escolha das opções de um campo: se está marcada, se ainda existe nos
    dados e a posição dela entre as marcadas (em branco, a das outras)."""

    name: str
    checked: bool
    exists: bool
    position: str


@dataclass(frozen=True)
class PlanView:
    """O editor do plano como a página o desenha: o editor, as caixas abertas (as com problema e a
    do foco), as listas dos dados que as opções podem usar e os campos de texto que uma regra
    pode escolher."""

    edit: PlanEdit
    opened: frozenset[str]
    troubled: frozenset[str]  # as caixas com algum problema do salvar
    lists: list[str]
    rule_paths: list[str]
    window_blocks: Mapping[str, list[str]]  # os blocos de janelas dos dados e o nome das janelas
    kinds: tuple[tuple[str, str], ...] = NODE_KINDS
    item_kinds: tuple[tuple[str, str], ...] = ITEM_KINDS
    scopes: tuple[tuple[str, str], ...] = SCOPES

    def field_id(self, name: str) -> str:
        return field_id(name)

    def kind_label(self, kind: str) -> str:
        return dict(NODE_KINDS).get(kind, kind)

    def list_choices(self) -> list[tuple[str, str]]:
        """As listas do plano que podem ser a lista de fases, para o campo escolher. A que o campo
        já tem e o plano não tem mais continua na lista, avisada, para a operadora vê-la."""
        shown = [("", "Nenhuma: sem fases com datas")]
        paths = self.edit.list_paths()
        shown += [(path, path) for path in paths]
        current = self.edit.phases.plan_field.strip()
        if current and current not in paths:
            shown.append((current, f"{current} (não existe mais no plano)"))
        return shown

    def source_choices(self, node: NodeEdit) -> list[SourceChoice]:
        """As listas dos dados que as opções do campo podem usar, para as caixas de marcar. As
        marcadas vêm primeiro, na ordem do campo e numeradas a partir de 1 (uma que os dados não têm
        mais continua na lista, avisada, para a operadora vê-la); depois, as outras, na ordem dos
        dados."""
        chosen = list(dict.fromkeys(node.sources()))
        shown = [
            SourceChoice(name, True, name in self.lists, str(position))
            for position, name in enumerate(chosen, start=1)
        ]
        return shown + [
            SourceChoice(name, False, True, "") for name in self.lists if name not in chosen
        ]

    def block_choices(self) -> list[tuple[str, str]]:
        """Os blocos de janelas dos dados, para o campo escolher; o que o campo já tem e os dados
        não têm mais continua na lista, avisado."""
        shown = [("", "Nenhum: sem janelas")]
        shown += [(name, name) for name in self.window_blocks]
        current = self.edit.phases.windows.strip()
        if current and current not in self.window_blocks:
            shown.append((current, f"{current} (não existe mais nos dados)"))
        return shown

    def windows_hint(self) -> str:
        """As janelas que um valor pode usar: as do bloco escolhido nas fases com datas."""
        block = self.edit.phases.windows.strip()
        names = self.window_blocks.get(block)
        if not names:
            return (
                "Escolha o bloco de janelas em «Fases com datas» para ligar valores a janelas. "
                "Um valor sem linha vale em qualquer fase."
            )
        return (
            f"Uma linha por valor, no formato valor = janela. Janelas do bloco {block}: "
            f"{', '.join(names)}. Um valor sem linha vale em qualquer fase."
        )

    def choices(self, rule: RuleEdit) -> list[tuple[str, bool]]:
        """Os campos de texto que a regra pode escolher, e se ela já escolheu cada um. Um campo que
        a regra escolheu e a árvore não tem mais continua na lista, marcado, para ela poder tirá-lo."""
        chosen = set(rule.fields)
        shown = [(path, path in chosen) for path in self.rule_paths]
        return shown + [(path, True) for path in rule.fields if path not in self.rule_paths]


def plan_view(
    edit: PlanEdit,
    lists: list[str],
    errors: Mapping[str, str],
    focus: str | None,
    window_blocks: Mapping[str, list[str]] | None = None,
) -> PlanView:
    opened: set[str] = set()
    troubled: set[str] = set()
    for at, _, _ in edit.nodes():
        if any(key == at or key.startswith(f"{at}.") for key in errors):
            troubled.add(at)
        if focus is not None and (focus == field_id(at) or focus.startswith(f"{field_id(at)}-")):
            opened.add(at)
    return PlanView(
        edit,
        frozenset(opened | troubled),
        frozenset(troubled),
        lists,
        edit.text_paths(),
        window_blocks or {},
    )
