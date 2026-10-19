"""Lê a matriz de cobertura da API de admin, `docs/api/cobertura.md`, pela gramática dela ("Como o
teste lê este arquivo"): é a fonte da verdade do teste de cobertura e do de contrato.

O arquivo é markdown, e o que o teste lê são as **tabelas**:

- as de "Inventário por recurso": uma por recurso, com a rota do admin na primeira coluna
  (`` `MÉTODO /caminho` `` como o app a registra), a operação da API, o `operation_id`, o papel, as
  marcas e o status;
- as dos editores (`FormEdit`, `PlanEdit`, `DataEdit` e `ScoringEdit`), com a operação do editor na
  primeira coluna e o editor no título da seção (`` (`FormEdit`, 19) ``);
- a das operações novas, as que não vêm de uma rota do admin.

O status é `falta`, `feita · arquivo::teste` ou `fora (letra)`. Não é um arquivo de teste (o pytest
não o coleta); os arquivos `test_admin_api_*.py` o importam.
"""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MATRIX_FILE = "docs/api/cobertura.md"
MATRIX = ROOT / MATRIX_FILE
OPENAPI_FILE = "docs/api/openapi.json"

# As marcas da matriz e o nome delas no OpenAPI.
MARKS = {"destrutiva": "x-destructive", "externo": "x-external-effect", "lead": "x-lead-data"}
ROLES = ("leitura", "total")
STATUSES = ("falta", "feita", "fora")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
EDITORS = ("FormEdit", "PlanEdit", "DataEdit", "ScoringEdit")
NONE = "—"

ROUTES, NEW, EDITOR = "rotas", "novas", "editor"
# A primeira célula do cabeçalho de cada tipo de tabela, e quantas colunas ela tem.
KINDS = {"Rota atual": (ROUTES, 8), "`operation_id`": (NEW, 6), "Operação": (EDITOR, 4)}


class MatrixError(Exception):
    """A matriz não segue a gramática (tabela com colunas a menos, por exemplo)."""


@dataclass(frozen=True)
class Row:
    """Uma linha de uma tabela da matriz. Os campos que a tabela dela não tem ficam vazios."""

    kind: str  # ROUTES, NEW ou EDITOR
    line: int  # a linha do arquivo, para a mensagem apontar
    section: str  # o título da seção em que a tabela está
    status: str  # falta, feita ou fora
    test: str | None = None  # `arquivo::teste`, na linha feita
    letter: str | None = None  # a letra da exceção, na linha fora
    route: str | None = None  # `MÉTODO /caminho` do admin (ROUTES)
    name: str | None = None  # a operação do editor, como na tabela (EDITOR)
    editor: str | None = None  # o editor da seção (EDITOR)
    operation: str | None = None  # `MÉTODO /caminho` da API, sob /admin/api/v1
    operation_id: str | None = None
    role: str | None = None
    marks: frozenset[str] = frozenset()  # os nomes do OpenAPI (`x-destructive`...)

    @property
    def key(self) -> str:
        """Como a linha se chama: a rota (tabela de rotas), o `operation_id` (operações novas) ou o
        editor e a operação (tabelas dos editores)."""
        if self.kind == EDITOR:
            return f"{self.editor} {self.editor_operation}"
        return (self.route if self.kind == ROUTES else self.operation_id) or ""

    @property
    def editor_operation(self) -> str:
        """O nome da operação do editor sem os números (`remove-step:i` é `remove-step`)."""
        assert self.name is not None
        return self.name.partition(":")[0]

    @property
    def route_pair(self) -> tuple[str, str]:
        assert self.route is not None
        method, _, path = self.route.partition(" ")
        return method, path

    @property
    def operation_pair(self) -> tuple[str, str]:
        assert self.operation is not None
        method, _, path = self.operation.partition(" ")
        return method, path

    @property
    def label(self) -> str:
        """Como a mensagem chama a linha."""
        what = self.route or self.operation_id or self.name
        return f"{MATRIX_FILE}:{self.line} ({what})"


@dataclass(frozen=True)
class Matrix:
    rows: tuple[Row, ...]
    exceptions: frozenset[str] = frozenset()  # as letras de "O que não entra na API"
    editor_counts: dict[str, int] = field(default_factory=dict)  # a contagem do título da seção

    def of_kind(self, kind: str) -> list[Row]:
        return [row for row in self.rows if row.kind == kind]

    @property
    def missing(self) -> int:
        """Quantas linhas estão `falta`, em todas as tabelas: o número que a catraca vigia."""
        return sum(row.status == "falta" for row in self.rows)

    def replace(self, old: Row, new: Row | None) -> Matrix:
        """Uma cópia sem a linha `old` ou com ela trocada por `new` (o teste do teste muda a matriz)."""
        rows = [new if row is old else row for row in self.rows]
        return Matrix(
            tuple(row for row in rows if row is not None), self.exceptions, self.editor_counts
        )

    def with_row(self, row: Row) -> Matrix:
        return Matrix((*self.rows, row), self.exceptions, self.editor_counts)


def cells_of(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split(" | ")]


def plain(cell: str) -> str:
    """A célula sem os acentos graves: `` `GET /jobs` `` é `GET /jobs`."""
    return cell.strip().strip("`").strip()


def _marks_of(cell: str) -> frozenset[str]:
    if cell == NONE:
        return frozenset()
    names = [name.strip() for name in cell.split(",")]
    unknown = [name for name in names if name not in MARKS]
    if unknown:
        raise MatrixError(f"marca desconhecida {unknown} (as marcas são {sorted(MARKS)})")
    return frozenset(MARKS[name] for name in names)


def _status_of(cell: str) -> dict[str, Any]:
    """O status, o teste de uma `feita` e a letra de uma `fora`."""
    word, _, rest = cell.strip().partition(" ")
    if word == "falta" and not rest:
        return {"status": "falta"}
    if word == "feita":
        _, dot, test = rest.partition("·")
        if not dot or "::" not in plain(test):
            raise MatrixError(f"«{cell}»: `feita` pede `· arquivo::teste`")
        return {"status": "feita", "test": plain(test)}
    if word == "fora":
        letter = rest.strip().removeprefix("(").removesuffix(")")
        if len(letter) != 1 or not letter.isalpha():
            raise MatrixError(f"«{cell}»: `fora` pede a letra, como `fora (A)`")
        return {"status": "fora", "letter": letter}
    raise MatrixError(f"«{cell}»: o status é `falta`, `feita · arquivo::teste` ou `fora (letra)`")


def _optional(cell: str) -> str | None:
    return None if cell == NONE else plain(cell)


def parse(text: str) -> Matrix:
    rows: list[Row] = []
    exceptions: set[str] = set()
    counts: dict[str, int] = {}
    chapter = section = ""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("## "):
            chapter, section = line[3:].strip(), ""
        elif line.startswith("### "):
            section = line[4:].strip()
            editor, count = _editor_of(section)
            if editor is not None:
                counts[editor] = count
        elif chapter.startswith("O que não entra") and line.startswith("- **("):
            exceptions.add(line[len("- **(")])
        elif line.startswith("|"):
            start = index
            while index < len(lines) and lines[index].startswith("|"):
                index += 1
            rows += _table(lines[start:index], start + 1, section)
            continue
        index += 1
    return Matrix(tuple(rows), frozenset(exceptions), counts)


def _editor_of(section: str) -> tuple[str | None, int]:
    """O editor e a contagem de operações do título de uma seção (`` (`FormEdit`, 19) ``)."""
    _, tick, rest = section.partition("(`")
    if not tick:
        return None, 0
    name, _, tail = rest.partition("`, ")
    count = tail.removesuffix(")")
    return (name, int(count)) if name in EDITORS and count.isdigit() else (None, 0)


def _table(lines: list[str], first_line: int, section: str) -> list[Row]:
    header = cells_of(lines[0])
    found = KINDS.get(header[0])
    if found is None:  # a tabela dos totais, por exemplo
        return []
    kind, width = found
    editor, _ = _editor_of(section)
    rows = []
    for number, line in enumerate(lines[2:], start=first_line + 2):  # pula o cabeçalho e o `---`
        cells = cells_of(line)
        if len(cells) != width:
            raise MatrixError(f"{MATRIX_FILE}:{number}: {len(cells)} colunas; a tabela tem {width}")
        try:
            rows.append(_row(kind, number, section, editor, cells))
        except MatrixError as error:
            raise MatrixError(f"{MATRIX_FILE}:{number}: {error}") from None
    return rows


def _row(kind: str, line: int, section: str, editor: str | None, cells: list[str]) -> Row:
    common: dict[str, Any] = {"kind": kind, "line": line, "section": section}
    if kind == ROUTES:
        route, _, _, operation, operation_id, role, marks, status = cells
        return Row(
            **common,
            **_status_of(status),
            route=plain(route),
            operation=_optional(operation),
            operation_id=_optional(operation_id),
            role=_optional(role),
            marks=_marks_of(marks),
        )
    if kind == NEW:
        operation_id, operation, _, role, marks, status = cells
        return Row(
            **common,
            **_status_of(status),
            operation=_optional(operation),
            operation_id=_optional(operation_id),
            role=_optional(role),
            marks=_marks_of(marks),
        )
    name, _, _, status = cells
    return Row(**common, **_status_of(status), name=plain(name), editor=editor)


def read(path: Path = MATRIX) -> Matrix:
    return parse(path.read_text(encoding="utf-8"))


# --- o que os dois testes dividem ---------------------------------------------------------------


def snake_case(name: str) -> bool:
    """`verbo_recurso`: minúsculas e números separados por `_`, com pelo menos duas partes."""
    parts = name.split("_")
    return len(parts) >= 2 and all(part and part.isalnum() and part.islower() for part in parts)


def marks_in(operation: dict[str, Any]) -> frozenset[str]:
    """As marcas `x-*` de uma operação do OpenAPI."""
    return frozenset(mark for mark in MARKS.values() if operation.get(mark))


HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def operations_of(document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(método em maiúsculas, caminho, operação) de cada operação do OpenAPI."""
    return [
        (method.upper(), path, found)
        for path, item in document.get("paths", {}).items()
        for method, found in item.items()
        if method in HTTP_METHODS
    ]


def differences(row: Row, method: str, path: str, operation: dict[str, Any]) -> list[str]:
    """Em que a operação do OpenAPI diverge do que a linha da matriz diz dela: método, caminho,
    papel e marcas."""
    found = []
    if row.operation_pair != (method, path):
        found.append(f"a matriz diz {' '.join(row.operation_pair)} e o OpenAPI tem {method} {path}")
    if row.role != operation.get("x-required-role"):
        found.append(
            f"o papel é «{row.role}» na matriz e «{operation.get('x-required-role')}» no OpenAPI"
        )
    if row.marks != marks_in(operation):
        found.append(
            f"as marcas são {sorted(row.marks)} na matriz e {sorted(marks_in(operation))} no OpenAPI"
        )
    return found


@functools.lru_cache(maxsize=256)
def _parsed(source: str) -> ast.Module:
    """A árvore de um arquivo. O teste de cobertura confere a matriz inteira (uns 180 testes citados,
    em uns 20 arquivos de mais de mil linhas) e cada teste do teste a confere de novo: o cache é pelo
    **texto** do arquivo, e não pelo caminho, então um arquivo reescrito nunca devolve a árvore velha.
    Ninguém muda a árvore."""
    return ast.parse(source)


@functools.lru_cache(maxsize=4096)
def _function(source: str, wanted: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """A primeira função (ou método) de nome `wanted` no código `source`, na ordem do `ast.walk`."""
    for node in ast.walk(_parsed(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == wanted:
            return node
    return None


def _code_of(citation: str, root: Path) -> tuple[str, str] | None:
    """O código do arquivo do `arquivo::teste` citado e o nome do teste, ou nada se o arquivo não
    está no repositório ou não é Python."""
    path, _, name = citation.partition("::")
    file = root / path
    if not file.is_file() or file.suffix != ".py":
        return None
    return file.read_text(encoding="utf-8"), name.rpartition("::")[2]


def citation_exists(citation: str, root: Path = ROOT) -> bool:
    """Se o `arquivo::teste` citado existe: o arquivo está no repositório e define a função (ou o
    método de uma classe). Lê o arquivo como código, sem importá-lo."""
    code = _code_of(citation, root)
    return code is not None and _function(*code) is not None


def function_of_citation(
    citation: str, root: Path = ROOT
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """A função do `arquivo::teste` citado, ou nada se o arquivo ou a função não existem."""
    code = _code_of(citation, root)
    return None if code is None else _function(*code)


def _reach(tree: ast.Module, start: ast.AST) -> list[ast.AST]:
    """O `start` e as funções e constantes do mesmo arquivo que ele usa, e as que essas usam: o que o
    teste faz de fato, ainda que passe por um auxiliar (`create(...)`) ou uma constante
    (`KEYS = f"{API}/api-keys"`)."""
    defined: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            defined[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined[node.target.id] = node
    seen: set[int] = set()
    reached: list[ast.AST] = []
    pending: list[ast.AST] = [start]
    while pending:
        node = pending.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        reached.append(node)
        pending += [
            defined[inner.id]
            for inner in ast.walk(node)
            if isinstance(inner, ast.Name) and inner.id in defined
        ]
    return reached


def _code_tokens(reached: Iterable[ast.AST]) -> list[str]:
    """O que o código dos nós alcançados escreve: os textos literais (também os pedaços de um
    f-string), os nomes e os atributos. **Não** entram os docstrings nem os comentários (que a árvore
    nem guarda), nem o nome da própria função: quem os lê não executa nada."""
    tokens: list[str] = []
    for root in reached:
        docstrings = set()
        for node in ast.walk(root):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docstrings.add(id(value))
        for node in ast.walk(root):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    tokens.append(node.value)
            elif isinstance(node, ast.Name):
                tokens.append(node.id)
            elif isinstance(node, ast.Attribute):
                tokens.append(node.attr)
    return tokens


@functools.lru_cache(maxsize=4096)
def _reached_text(source: str, wanted: str) -> str:
    """O código-fonte do que o teste `wanted` exerce (`_reach`), em um texto só."""
    function = _function(source, wanted)
    assert function is not None
    return "\n".join(
        ast.get_source_segment(source, node) or "" for node in _reach(_parsed(source), function)
    )


@functools.lru_cache(maxsize=4096)
def _reached_tokens(source: str, wanted: str) -> tuple[str, ...]:
    """Os textos, nomes e atributos do código do que o teste `wanted` exerce (`_code_tokens`)."""
    function = _function(source, wanted)
    assert function is not None
    return tuple(_code_tokens(_reach(_parsed(source), function)))


def path_is_shared(operation: tuple[str, str], others: Iterable[tuple[str, str]]) -> bool:
    """Se o caminho de `operation` (método e caminho) é o prefixo do de outra operação, ou o mesmo
    caminho com outro método (`/jobs` em `/jobs/{public_id}`; `GET` e `PUT` de `/forms/{slug}/draft`):
    o caminho sozinho, num teste, não diz qual das duas ele exerce."""
    method, path = operation
    return any(
        (other_method, other_path) != (method, path)
        and (other_path == path or other_path.startswith(path.rstrip("/") + "/"))
        for other_method, other_path in others
    )


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text) is not None


def citation_mentions(
    citation: str,
    operation_id: str,
    operation: str,
    root: Path = ROOT,
    *,
    id_in_code: bool = False,
) -> bool:
    """Se o teste citado fala da operação: o `operation_id` aparece no código dele (ou no das funções
    e constantes do mesmo arquivo que ele usa), ou o caminho da operação (`MÉTODO /caminho`), com cada
    trecho fixo dele (`/jobs` e `/pdf`, em `/jobs/{public_id}/pdf`) seguido de um fim de palavra. É
    uma conferência de que a linha não cita um teste qualquer, não a de que o teste prova a operação.

    Com `id_in_code` (para a operação cujo caminho é prefixo do de outra, ver `path_is_shared`), o
    caminho não basta: o `operation_id` tem de estar no **código** do teste, como texto literal, nome
    ou atributo, e não só no docstring ou num comentário."""
    code = _code_of(citation, root)
    if code is None or _function(*code) is None:
        return False
    if id_in_code:
        return any(_has_word(token, operation_id) for token in _reached_tokens(*code))
    text = _reached_text(*code)
    if _has_word(text, operation_id):
        return True
    _, _, path = operation.partition(" ")
    segments = [part for part in path.split("/") if part and not part.startswith("{")]
    return bool(segments) and all(
        re.search(rf"/{re.escape(part)}(?![\w-])", text) for part in segments
    )


def duplicates(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    repeated: list[str] = []
    for item in items:
        if item in seen and item not in repeated:
            repeated.append(item)
        seen.add(item)
    return repeated
