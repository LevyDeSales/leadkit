"""O editor dos dados do formulário no admin: os blocos com tipo declarado (lista, janelas, edição
e valor livre), em campos, no lugar do JSON (ADR 0010: Jinja2 e HTMX). É a parte do editor do
rascunho que cuida de `data`; o resto está em `app.admin.form_editor`, que o chama.

O estado mora todo no formulário da página, como no resto do editor. Cada campo tem por nome a
posição do bloco (`data.1.windows.0.start`), e não o nome dele, que a operadora pode estar
mudando. Um problema da compilação, que fala do bloco pelo nome (`data.janelas.windows.0.start`),
volta para o campo pela posição.

- O banco não guarda a ordem das chaves de um objeto: ao abrir o rascunho, os blocos vêm em ordem
  de nome, e por isso não há botões de subir e descer blocos. Só as janelas de um bloco têm ordem.
- Dois blocos com o mesmo nome, um bloco sem nome e um valor livre que não é JSON são problemas do
  editor (`DataEdit.problems`): a definição não tem como guardar o que o editor não consegue
  escrever, e salvar para antes de compilar.
- Cada tipo mostra os campos dele; trocar o tipo guarda o que o bloco já tinha, mas a definição só
  leva o que vale no tipo novo.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from app.forms.definition.document import JSON_MESSAGES, Problem

BLOCK_KINDS = (
    ("list", "Lista de nomes"),
    ("windows", "Janelas de datas"),
    ("edition", "Edição"),
    ("value", "Valor livre (JSON)"),
)
# A ordem das janelas de um bloco: em sequência (o padrão) ou podendo se sobrepor.
ORDERS = (
    ("yes", "Em sequência: cada janela começa depois do fim da anterior"),
    ("no", "Podem se sobrepor"),
)
# O que cada tipo mostra além do nome e do tipo, pelo nome no formulário depois de `data.<n>.`.
KIND_FIELDS = {
    "list": ("items",),
    "windows": ("windows", "sequential"),
    "edition": ("edition.name", "edition.starts", "edition.ends"),
    "value": ("value",),
}
WINDOW_FIELDS = ("name", "label", "start", "end")
NO_NAME = "dê um nome ao bloco"
REPEATED = "outro bloco dos dados já usa este nome"
FIELD_LABELS = {
    "name": "nome do bloco",
    "kind": "tipo",
    "items": "itens",
    "windows": "janelas",
    "sequential": "ordem das janelas",
    "value": "valor",
    "label": "rótulo",
    "start": "início",
    "end": "fim",
    "edition.name": "nome da edição",
    "edition.starts": "início",
    "edition.ends": "fim",
}

_KEY = re.compile(
    r"data\.(\d{1,3})\.("
    r"name|kind|items|value|sequential|edition\.name|edition\.starts|edition\.ends"
    r"|windows\.(\d{1,3})\.(?:name|label|start|end))"
)
# Os verbos das operações: as expressões regulares de `apply` saem destas tuplas, e
# `DataEdit.OPERATIONS` também. Um verbo novo só entra no `apply` por aqui, e então ele aparece em
# `OPERATIONS`, onde o teste de cobertura da API de admin cobra uma linha da matriz para ele.
_BLOCK_VERBS = ("add", "remove", "refresh")  # `data-<verbo>`: sobre um bloco
_WINDOW_VERBS = ("add", "remove", "up", "down")  # `data-<verbo>-window`: sobre uma janela
_OP = re.compile("data-(" + "|".join(_BLOCK_VERBS) + r")(?::(\d{1,3}))?")
_WINDOW_OP = re.compile("data-(" + "|".join(_WINDOW_VERBS) + r")-window:(\d{1,3})(?::(\d{1,3}))?")
ADD_BLOCK_ID = "op-data-add"


class FormFields(Protocol):
    """O formulário que voltou do navegador (o `FormData` do Starlette)."""

    def __iter__(self) -> Iterator[str]: ...

    def getlist(self, key: str) -> list[Any]: ...


def field_id(name: str) -> str:
    """O id do campo na página, pelo nome dele (`data.0.name` -> `f-data-0-name`)."""
    return "f-" + name.replace(".", "-")


def add_window_id(i: int) -> str:
    return f"op-data-{i}-add-window"


def _lines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _text(value: Any) -> str:
    return "" if value is None else str(value)


# --- o editor ---------------------------------------------------------------------------------


@dataclass(eq=False)
class WindowEdit:
    name: str = ""
    label: str = ""
    start: str = ""
    end: str = ""

    def document(self) -> dict[str, Any]:
        found: dict[str, Any] = {"name": self.name.strip()}
        if self.label.strip():
            found["label"] = self.label.strip()
        return found | {"start": self.start.strip(), "end": self.end.strip()}


@dataclass(eq=False)
class BlockEdit:
    """Um bloco dos dados como o editor o mostra: os textos como a operadora escreveu."""

    name: str = ""
    kind: str = "list"
    items: str = ""  # lista: um nome por linha
    windows: list[WindowEdit] = field(default_factory=list)
    sequential: bool = True
    edition_name: str = ""
    starts: str = ""
    ends: str = ""
    value: str = ""  # valor livre: o JSON como texto

    @classmethod
    def of_document(cls, name: str, block: Mapping[str, Any]) -> BlockEdit:
        kind = str(block.get("kind", "value"))
        value = block.get("value")
        return cls(
            name=name,
            kind=kind,
            items="\n".join(str(item) for item in block.get("items") or ()),
            windows=[
                WindowEdit(
                    name=_text(window.get("name")),
                    label=_text(window.get("label")),
                    start=_text(window.get("start")),
                    end=_text(window.get("end")),
                )
                for window in block.get("windows") or ()
                if isinstance(window, Mapping)
            ],
            sequential=bool(block.get("sequential", True)),
            edition_name=_text(block.get("name")) if kind == "edition" else "",
            starts=_text(block.get("starts")),
            ends=_text(block.get("ends")),
            value=json.dumps(value, ensure_ascii=False, indent=2) if kind == "value" else "",
        )

    def fill(self) -> None:
        """Um bloco de janelas nasce com uma janela."""
        if self.kind == "windows" and not self.windows:
            self.windows = [WindowEdit()]

    @property
    def shown(self) -> tuple[str, ...]:
        return KIND_FIELDS.get(self.kind, ())

    def parsed_value(self) -> tuple[Any, str | None]:
        """O valor livre lido do texto, e o problema (em português) se ele não é JSON."""
        try:
            return json.loads(_lines(self.value)), None
        except json.JSONDecodeError as exc:
            detail = JSON_MESSAGES.get(exc.msg, "formato inválido")
            return None, f"JSON inválido: {detail} (linha {exc.lineno}, coluna {exc.colno})"
        except RecursionError:
            return None, "JSON inválido: há listas ou objetos demais, um dentro do outro"

    def document(self) -> dict[str, Any]:
        """O bloco no formato da definição: só o que vale no tipo e o que difere do padrão."""
        if self.kind == "list":
            lines = [line.strip() for line in _lines(self.items).split("\n") if line.strip()]
            return {"kind": "list", "items": lines}
        if self.kind == "windows":
            found: dict[str, Any] = {
                "kind": "windows",
                "windows": [window.document() for window in self.windows],
            }
            return found if self.sequential else found | {"sequential": False}
        if self.kind == "edition":
            found = {"kind": "edition", "name": self.edition_name.strip()}
            for key, text in (("starts", self.starts), ("ends", self.ends)):
                if text.strip():
                    found[key] = text.strip()
            return found
        value, problem = self.parsed_value()
        return {"kind": self.kind, "value": _lines(self.value) if problem else value}


@dataclass(eq=False)
class DataEdit:
    """Os dados do formulário como a operadora os edita."""

    # As operações do editor, no nome do campo `op` sem os números. Saem das mesmas tuplas de verbos de
    # que o `apply` monta as expressões regulares (`_BLOCK_VERBS` e `_WINDOW_VERBS`), então uma
    # operação nova tem de passar por aqui, e o teste de cobertura da API de admin cobra uma linha da
    # matriz para cada uma.
    OPERATIONS: ClassVar[tuple[str, ...]] = (
        *(f"data-{verb}" for verb in _BLOCK_VERBS),
        *(f"data-{verb}-window" for verb in _WINDOW_VERBS),
    )

    blocks: list[BlockEdit] = field(default_factory=list)

    # --- leitura ------------------------------------------------------------------------------

    @classmethod
    def of_document(cls, data: Any) -> DataEdit:
        """O editor de uma definição guardada, com os blocos em ordem de nome."""
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            [
                BlockEdit.of_document(name, data[name])
                for name in sorted(data)
                if isinstance(data[name], Mapping)
            ]
        )

    @classmethod
    def of_form(cls, form: FormFields) -> DataEdit:
        """O editor como o formulário da página voltou. Os índices dizem a ordem; um campo que não
        é do editor fica de fora."""
        blocks: dict[int, BlockEdit] = {}
        windows: dict[tuple[int, int], WindowEdit] = {}
        for key in form:
            found = _KEY.fullmatch(key)
            if found is None:
                continue
            block = blocks.setdefault(int(found[1]), BlockEdit())
            value = _lines(str(form.getlist(key)[0])) if form.getlist(key) else ""
            name = found[2]
            if found[3] is not None:
                window = windows.setdefault((int(found[1]), int(found[3])), WindowEdit())
                setattr(window, name.rsplit(".", 1)[-1], value)
            elif name == "sequential":
                block.sequential = value != "no"
            elif name.startswith("edition."):
                setattr(block, {"name": "edition_name"}.get(name[8:], name[8:]), value)
            else:
                setattr(block, name, value)
        for (i, _), window in sorted(windows.items()):
            blocks[i].windows.append(window)
        for block in blocks.values():
            block.fill()
        return cls([blocks[i] for i in sorted(blocks)])

    # --- o que vai para a definição -----------------------------------------------------------

    def document(self) -> dict[str, Any]:
        """Os blocos pelo nome. Dois blocos com o mesmo nome ficam com o primeiro, e o segundo
        é um problema do editor (`problems`); o bloco sem nome fica de fora, pelo mesmo motivo."""
        found: dict[str, Any] = {}
        for block in self.blocks:
            name = block.name.strip()
            if name and name not in found:
                found[name] = block.document()
        return found

    def problems(self) -> list[Problem]:
        """O que o editor não consegue escrever na definição: bloco sem nome, nome repetido e valor
        livre que não é JSON. O caminho é a posição do bloco no formulário (`data.1.name`)."""
        problems: list[Problem] = []
        seen: set[str] = set()
        for i, block in enumerate(self.blocks):
            name = block.name.strip()
            if not name:
                problems.append(Problem(f"data.{i}.name", NO_NAME))
            elif name in seen:
                problems.append(Problem(f"data.{i}.name", REPEATED))
            seen.add(name)
            if block.kind == "value":
                _, problem = block.parsed_value()
                if problem:
                    problems.append(Problem(f"data.{i}.value", problem))
        return problems

    # --- consulta -----------------------------------------------------------------------------

    def list_names(self) -> list[str]:
        """Os nomes dos blocos de lista, de onde um campo de opções do plano pode tirar os valores."""
        return [
            block.name.strip()
            for block in self.blocks
            if block.kind == "list" and block.name.strip()
        ]

    def window_blocks(self) -> dict[str, list[str]]:
        """Os blocos de janelas, com o nome das janelas de cada um: de onde as fases com datas
        tiram as janelas."""
        return {
            block.name.strip(): [w.name.strip() for w in block.windows if w.name.strip()]
            for block in self.blocks
            if block.kind == "windows" and block.name.strip()
        }

    def places(self) -> frozenset[str]:
        """Os nomes que a página desenha, campos e caixas: onde um problema pode aparecer."""
        found = {"data"}
        for i, block in enumerate(self.blocks):
            found |= {f"data.{i}", f"data.{i}.name", f"data.{i}.kind"}
            found |= {f"data.{i}.{name}" for name in block.shown}
            for k in range(len(block.windows) if block.kind == "windows" else 0):
                found.add(f"data.{i}.windows.{k}")
                found |= {f"data.{i}.windows.{k}.{name}" for name in WINDOW_FIELDS}
        return frozenset(found)

    def place_of(self, path: str) -> str:
        """O campo ou a caixa onde um problema do caminho da definição aparece. O caminho fala do
        bloco pelo nome (`data.janelas.windows.0.end`) ou, nos problemas do editor, pela posição
        (`data.1.name`)."""
        parts = path.split(".")
        if len(parts) > 1 and not parts[1].isdigit():
            names = [block.name.strip() for block in self.blocks]
            if parts[1] not in names:
                return "data"
            i = names.index(parts[1])
            rest = parts[2:] or ["name"]  # o problema no nome do bloco, e não num campo dele
            if self.blocks[i].kind == "edition" and rest[0] in ("name", "starts", "ends"):
                rest = ["edition", rest[0]]
            path = ".".join(["data", str(i), *rest])
        places = self.places()
        candidate = path
        while candidate not in places and "." in candidate:
            candidate = candidate.rsplit(".", 1)[0]
        return candidate if candidate in places else "data"

    def describe(self, place: str) -> str:
        """Onde está o problema, como a operadora lê: "Dados · janelas · janela 2 · fim"."""
        title = "Dados do formulário"
        match = re.fullmatch(r"data\.(\d+)(?:\.(.+))?", place)
        if match is None or int(match[1]) >= len(self.blocks):
            return title
        block = self.blocks[int(match[1])]
        where = [title, block.name.strip() or f"bloco {int(match[1]) + 1}"]
        rest = match[2] or ""
        if window := re.fullmatch(r"windows\.(\d+)(?:\.(.+))?", rest):
            where.append(f"janela {int(window[1]) + 1}")
            rest = window[2] or ""
        if rest:
            where.append(FIELD_LABELS.get(rest, rest))
        return " · ".join(where)

    # --- as operações -------------------------------------------------------------------------

    def apply(self, op: str) -> str | None:
        """Aplica uma operação (`data-add`, `data-remove:1`, `data-add-window:2`...) e devolve o
        id do campo que recebe o foco. Uma operação desconhecida, ou de uma página velha que aponta
        para o que não existe mais, não muda nada."""
        if found := _OP.fullmatch(op):
            return self._on_block(found[1], found[2])
        if found := _WINDOW_OP.fullmatch(op):
            return self._on_window(found[1], int(found[2]), found[3])
        return None

    def _on_block(self, operation: str, number: str | None) -> str | None:
        if operation == "add":
            if number is not None:
                return None
            self.blocks.append(BlockEdit())
            return field_id(f"data.{len(self.blocks) - 1}.name")
        if number is None or int(number) >= len(self.blocks):
            return None
        i = int(number)
        if operation == "remove":
            self.blocks.pop(i)
            return ADD_BLOCK_ID
        self.blocks[i].fill()
        return field_id(f"data.{i}.kind")

    def _on_window(self, operation: str, i: int, number: str | None) -> str | None:
        if i >= len(self.blocks) or self.blocks[i].kind != "windows":
            return None
        windows = self.blocks[i].windows
        if operation == "add":
            if number is not None:
                return None
            windows.append(WindowEdit())
            return field_id(f"data.{i}.windows.{len(windows) - 1}.name")
        if number is None or int(number) >= len(windows):
            return None
        k = int(number)
        if operation == "remove":
            windows.pop(k)
            return add_window_id(i)
        target = k + (-1 if operation == "up" else 1)
        if 0 <= target < len(windows):
            windows[k], windows[target] = windows[target], windows[k]
            k = target
        return field_id(f"data.{i}.windows.{k}.name")


# --- a página ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DataView:
    """O editor dos dados como a página o desenha: o editor e os tipos de bloco."""

    edit: DataEdit
    kinds: tuple[tuple[str, str], ...] = BLOCK_KINDS
    orders: tuple[tuple[str, str], ...] = ORDERS

    def field_id(self, name: str) -> str:
        return field_id(name)
