"""Ambiente Jinja isolado dos templates que vêm de uma definição de formulário: instruções, prompt,
PDF e prévia.

Quem escreve esses templates é a operadora, no admin, e eles rodam no servidor com os dados da
lead. Por isso o ambiente:
- é o `ImmutableSandboxedEnvironment` do Jinja: recusa atributos internos (`__class__`, `_x`,
  atributos de função e de código) e os métodos que mudam listas, dicionários e conjuntos;
- proíbe variáveis indefinidas (`StrictUndefined`);
- tem um conjunto fechado de filtros e funções (`FILTERS` e `GLOBALS`): o que não está na lista
  não existe, e o template que usa outro filtro nem compila;
- não carrega outros templates (`include`, `import`, `extends`);
- põe teto na repetição de texto e de listas (`*`), no expoente das potências (`**`), na largura
  dos campos do filtro `format` (`"%50000000d"|format(1)`) e no tamanho do texto renderizado.

`SandboxTemplate` compila o texto sem executar nada dele e recusa, já na compilação, a sintaxe
quebrada, os filtros fora da lista, o acesso a atributos que começam com `_` e a carga de outros
templates. Na renderização, o erro sai como `TemplateProblem`, com a linha do template e sem a
mensagem original, que pode trazer dados da lead.

O isolamento protege arquivos, sistema e atributos internos. O que pesa sem virar texto grande
(laços aninhados sem saída, a recursão de uma macro) o isolamento não vê: quem barra é o prazo da
renderização (`RENDER_SECONDS`, abaixo), que corta a thread (`app.forms.render_guard`) e devolve o
`TemplateProblem`. Não há teto de memória para o template: a leitura de memória é a do processo todo, e
outra thread (o otimizador de imagens, por exemplo) cortaria uma prévia inocente. A memória fica com os
tetos de cima (repetição, expoente, largura dos campos, preenchimento de `ljust` e afins e o tamanho
do texto), que só barram o que vira texto grande, e com o prazo: um template que dobra um texto a cada
volta aloca o que couber nele. Quem edita é a operadora, com a sessão do admin.
"""

from __future__ import annotations

import re
import traceback
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from jinja2 import StrictUndefined, TemplateSyntaxError, UndefinedError, meta, nodes
from jinja2.compiler import CodeGenerator
from jinja2.defaults import DEFAULT_FILTERS
from jinja2.filters import do_format
from jinja2.sandbox import ImmutableSandboxedEnvironment, SecurityError, safe_range
from jinja2.utils import Namespace
from markupsafe import soft_str

from app.forms.dates import long_date, month_year, short_date
from app.forms.render_guard import RenderLimit, run_within_limits

# Filtros do Jinja que sobram no ambiente isolado: texto, listas e números. Ficam de fora, entre
# outros, `attr` (atributo por nome calculado), `safe` (desliga o escape do HTML), `pprint`,
# `random`, `tojson` e os que multiplicam o tamanho do texto (`indent`, `center`, `batch`). O
# `format` não está aqui: entra na `FILTERS` em versão com teto na largura dos campos.
_JINJA_FILTERS = (
    "abs",
    "capitalize",
    "count",
    "d",
    "default",
    "dictsort",
    "e",
    "escape",
    "first",
    "float",
    "groupby",
    "int",
    "items",
    "join",
    "last",
    "length",
    "list",
    "lower",
    "map",
    "max",
    "min",
    "reject",
    "rejectattr",
    "replace",
    "reverse",
    "round",
    "select",
    "selectattr",
    "sort",
    "string",
    "sum",
    "title",
    "trim",
    "truncate",
    "unique",
    "upper",
    "wordcount",
)


# Teto para repetir texto ou lista (`"x" * n`), para o expoente de uma potência, para a largura (ou
# a precisão) de um campo do `format` e para o texto que o template rende: um `"x" * 10**9` ou um
# `"%999999999d"|format(1)` num template travaria o processo.
MAX_REPEAT = 100_000
MAX_EXPONENT = 100
MAX_FIELD_WIDTH = 1_000
MAX_OUTPUT = 1_000_000
# O prazo que uma renderização de template pode levar (ver `app.forms.render_guard`). Um template
# normal leva milissegundos.
RENDER_SECONDS = 5.0


class LimitError(SecurityError):
    """Uma operação que passaria do tamanho que o ambiente isolado aceita."""


# Um campo do `%` do Python: `%%` é o `%` literal; no campo vêm o nome (`(n)`), as marcas, a
# largura e a precisão (um número ou `*`, que tira o número dos argumentos do filtro) e a letra da
# conversão. O `%(` que não fecha como campo é um nome que o Python lê melhor que este padrão
# (`%(a(b))9d`): na dúvida, recusa.
_FORMAT_FIELD = re.compile(
    r"%%|%(?:\([^()]*\))?[#0\- +]*(\*|[0-9]+)?(?:\.(\*|[0-9]*))?[hlL]?[diouxXeEfFgGcrsa]|%\("
)


def _limited_format(value: Any, *args: Any, **kwargs: Any) -> str:
    """O filtro `format` do Jinja, recusando campo com largura ou precisão acima de
    `MAX_FIELD_WIDTH`, e as que vêm dos argumentos (`%*d`), que não dá para conferir antes."""
    for field in _FORMAT_FIELD.finditer(soft_str(value)):
        if field[0] == "%(":
            raise LimitError("campo de formato que não dá para conferir")
        for size in field.groups():
            if size == "*" or (size and int(size) > MAX_FIELD_WIDTH):
                raise LimitError("largura de campo grande demais")
    return do_format(value, *args, **kwargs)


FILTERS: dict[str, Any] = {name: DEFAULT_FILTERS[name] for name in _JINJA_FILTERS} | {
    "format": _limited_format,
    "long_date": long_date,
    "short_date": short_date,
    "month_year": month_year,
}
GLOBALS: dict[str, Any] = {"range": safe_range, "namespace": Namespace}


# Os métodos de texto que preenchem até a largura que se pede: `"x".ljust(400000000)` aloca 400 MB.
_PADDING_METHODS = frozenset({"ljust", "rjust", "center", "zfill"})


class _Environment(ImmutableSandboxedEnvironment):
    intercepted_binops = frozenset({"*", "**"})

    def call(self, context: Any, obj: Any, /, *args: Any, **kwargs: Any) -> Any:
        """A chamada do template, recusando o preenchimento de texto acima de `MAX_REPEAT`."""
        pads = getattr(obj, "__name__", None) in _PADDING_METHODS
        wide = any(isinstance(arg, int) and arg > MAX_REPEAT for arg in args)
        if pads and wide and isinstance(getattr(obj, "__self__", None), str):
            raise LimitError("preenchimento grande demais")
        return super().call(context, obj, *args, **kwargs)

    def call_binop(self, context: Any, operator: str, left: Any, right: Any) -> Any:
        if operator == "**" and isinstance(right, int | float) and abs(right) > MAX_EXPONENT:
            raise LimitError("expoente grande demais")
        if operator == "*":
            for sequence, times in ((left, right), (right, left)):
                repeats = isinstance(sequence, str | list | tuple) and isinstance(times, int)
                if repeats and len(sequence) * times > MAX_REPEAT:
                    raise LimitError("repetição grande demais")
        return super().call_binop(context, operator, left, right)


def _environment(*, html: bool) -> ImmutableSandboxedEnvironment:
    env = _Environment(
        undefined=StrictUndefined,
        autoescape=html,
        # O texto do prompt e das instruções vai ao LLM como está, com a quebra de linha final.
        keep_trailing_newline=not html,
        # Sem calcular expressões constantes na compilação: compilar nunca executa nada do
        # template, e a conferência lê o template como foi escrito.
        optimized=False,
    )
    env.filters = dict(FILTERS)
    env.globals = dict(GLOBALS)
    return env


# O template que aninha fundo demais (parênteses, blocos ou filtros uns dentro dos outros) não compila.
TOO_DEEP = "o template aninha fundo demais (parênteses, blocos ou filtros uns dentro dos outros)"


class _UndeclaredNames(meta.TrackingCodeGenerator):
    """O `TrackingCodeGenerator` do `jinja2.meta`, que acha as variáveis indefinidas, sem calcular
    as expressões constantes: o dele calcula, e com isso executa filtros e muda a árvore."""

    def __init__(self, environment: Any) -> None:
        CodeGenerator.__init__(
            self, environment, "<introspection>", "<introspection>", optimized=False
        )
        self.undeclared_identifiers: set[str] = set()


# As mensagens de sintaxe do Jinja, em português: (padrão na mensagem original, texto).
_SYNTAX: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    (
        re.compile(r"innermost block that needs to be closed is '(\w+)'"),
        lambda found: f"o bloco {found[1]} não foi fechado (falta o end{found[1]})",
    ),
    (
        re.compile(r"^Encountered unknown tag '(\w+)'"),
        lambda found: f"tag desconhecida ou fora do lugar: {found[1]}",
    ),
    (
        re.compile(r"^unexpected end of template"),
        lambda found: "o template termina no meio de uma expressão",
    ),
    (re.compile(r"^Expected an expression"), lambda found: "falta uma expressão"),
    (
        re.compile(r"^expected token"),
        lambda found: "a expressão não fecha onde devia (confira o }} ou o %})",
    ),
    (
        re.compile(r"^unexpected char (.+) at \d+"),
        lambda found: f"caractere inesperado: {found[1]}",
    ),
    (re.compile(r"^unexpected '(.+)'"), lambda found: f"símbolo inesperado: {found[1]}"),
    (
        re.compile(r"^Missing end of comment tag"),
        lambda found: "comentário sem o #} de fechamento",
    ),
)


def _syntax(message: str) -> str:
    for pattern, text in _SYNTAX:
        found = pattern.search(message)
        if found:
            return f"sintaxe inválida: {text(found)}"
    return "sintaxe inválida"


_TEXT = _environment(html=False)
_HTML = _environment(html=True)
_LOADS_OTHER_TEMPLATES = (nodes.Extends, nodes.Include, nodes.Import, nodes.FromImport)


class TemplateProblem(Exception):
    """Um template que não compila ou não renderiza. `line` é a linha do template, quando se sabe
    qual é; `detail` nunca traz valores das variáveis."""

    def __init__(self, name: str, line: int | None, detail: str) -> None:
        where = f"{name}, linha {line}" if line is not None else name
        super().__init__(f"{where}: {detail}")
        self.name = name
        self.line = line
        self.detail = detail


class SandboxTemplate:
    """Um template compilado no ambiente isolado. `name` identifica o template nos erros (por
    exemplo `prompt` ou `pdf.html`); `html` liga o escape automático do HTML.

    Levanta `TemplateProblem` na construção, se o texto não compila (a sintaxe quebrada, e também o
    template que aninha fundo demais, que o Jinja não aguenta), e em `render`, se a renderização
    falha."""

    def __init__(self, source: str, *, name: str, html: bool) -> None:
        self.name = name
        env = _HTML if html else _TEXT
        try:
            self._ast = env.parse(source, name=name, filename=name)
            self._refuse_unknown_names(env)
            self._refuse_internal_attributes()
            code = env.compile(self._ast, name=name, filename=name)
        except TemplateSyntaxError as exc:
            raise TemplateProblem(name, exc.lineno, _syntax(exc.message or "")) from None
        except (RecursionError, SyntaxError):
            # O Jinja lê e compila por recursão, e o Python limita a indentação (`IndentationError`) e
            # os blocos aninhados (`SyntaxError`) do código que ele gera: um template fundo demais não
            # se compila. Não é um erro nosso nem tem uma linha só (a do código gerado não é a dele).
            raise TemplateProblem(name, None, TOO_DEEP) from None
        self._template = env.template_class.from_code(env, code, env.make_globals(None), None)

    def _refuse_unknown_names(self, env: ImmutableSandboxedEnvironment) -> None:
        """Outro template, filtro ou teste fora da lista nunca funcionariam: saem já na
        compilação, em qualquer ramo do template, mesmo nos que os exemplos não percorrem."""
        for node in self._ast.find_all(_LOADS_OTHER_TEMPLATES):
            raise TemplateProblem(self.name, node.lineno, "não é possível carregar outro template")
        for node in self._ast.find_all((nodes.Filter, nodes.Test)):
            known = env.filters if isinstance(node, nodes.Filter) else env.tests
            if node.name not in known:
                kind = "filtro" if isinstance(node, nodes.Filter) else "teste"
                raise TemplateProblem(self.name, node.lineno, f"{kind} inexistente: {node.name}")

    def _refuse_internal_attributes(self) -> None:
        """Atributo que começa com `_` o ambiente isolado recusaria na renderização: sai já na
        compilação, em qualquer ramo. O nome calculado na renderização (`x[nome]`) quem recusa é
        o próprio ambiente."""
        for attribute, line in self._accessed_names():
            if attribute.startswith("_"):
                raise TemplateProblem(self.name, line, f"atributo interno proibido: {attribute}")

    def _accessed_names(self) -> list[tuple[str, int]]:
        """Atributos e chaves fixas acessados no template (`x.nome`, `x["nome"]`), com a linha."""
        found = [(node.attr, node.lineno) for node in self._ast.find_all(nodes.Getattr)]
        found += [
            (node.arg.value, node.lineno)
            for node in self._ast.find_all(nodes.Getitem)
            if isinstance(node.arg, nodes.Const) and isinstance(node.arg.value, str)
        ]
        return found

    def module(self) -> Any:
        """O módulo do template: as macros e as variáveis do nível de cima, como atributos. A
        biblioteca de componentes da marca (`app.forms.definition.components`) é um módulo, que os
        templates da operadora recebem no contexto."""
        return self._template.make_module()

    def undeclared(self) -> dict[str, int]:
        """Variáveis que o template usa sem definir, com a linha do primeiro uso: precisam vir do
        contexto da renderização ou de `GLOBALS`."""
        tracker = _UndeclaredNames(self._ast.environment)
        tracker.visit(self._ast)
        names = tracker.undeclared_identifiers - GLOBALS.keys()
        lines: dict[str, int] = {}
        for node in self._ast.find_all(nodes.Name):
            if node.name in names and node.ctx == "load":
                lines.setdefault(node.name, node.lineno)
        return dict(sorted(lines.items(), key=lambda item: item[1]))

    def fields_of(self, variable: str) -> list[tuple[str, int]]:
        """Campos lidos direto de uma variável do contexto (`answers.nome`, `plan["titulo"]`),
        com a linha. Vazio se o template redefine a variável (`set`, `for`, macro)."""
        stored = {
            node.name for node in self._ast.find_all(nodes.Name) if node.ctx in ("store", "param")
        }
        if variable in stored:
            return []
        found: list[tuple[str, int]] = []
        for node in self._ast.find_all((nodes.Getattr, nodes.Getitem)):
            target = node.node
            if not (isinstance(target, nodes.Name) and target.name == variable):
                continue
            if isinstance(node, nodes.Getattr):
                found.append((node.attr, node.lineno))
            elif isinstance(node.arg, nodes.Const) and isinstance(node.arg.value, str):
                found.append((node.arg.value, node.lineno))
        return found

    def reads(self, variable: str) -> tuple[str, ...] | None:
        """Os campos que o template lê de uma variável do contexto (`data.edicao`,
        `data["edicao"]`), na ordem em que aparecem, sem repetir. None se ele usa a variável de
        outro jeito: inteira (`{{ data }}`, num laço, num filtro), com a chave calculada, chamando
        um método dela (`data.items()`) ou redefinindo-a. Aí qualquer campo pode ser lido."""
        names = [node for node in self._ast.find_all(nodes.Name) if node.name == variable]
        if any(node.ctx != "load" for node in names):
            return None
        called = {id(node.node) for node in self._ast.find_all(nodes.Call)}
        read_through: set[int] = set()  # os usos da variável que leem um campo dela
        fields: list[str] = []
        for node in self._ast.find_all((nodes.Getattr, nodes.Getitem)):
            target = node.node
            if not (isinstance(target, nodes.Name) and target.name == variable):
                continue
            if id(node) in called:
                return None
            if isinstance(node, nodes.Getattr):
                fields.append(node.attr)
            elif isinstance(node.arg, nodes.Const) and isinstance(node.arg.value, str):
                fields.append(node.arg.value)
            else:
                return None
            read_through.add(id(target))
        if any(id(node) not in read_through for node in names):
            return None
        return tuple(dict.fromkeys(fields))

    def render(self, context: Mapping[str, Any]) -> str:
        try:
            return run_within_limits(
                lambda: _join_within_limit(self._template.generate(context)),
                seconds=RENDER_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - qualquer falha do template vira TemplateProblem
            line = _line_of(exc, self.name)
            if isinstance(exc, RenderLimit) and line is not None:
                # O corte não passou pelo `handle_exception` do Jinja, que troca a linha do código
                # gerado pela do template: a troca é feita aqui.
                line = self._template.get_corresponding_lineno(line)
            # `from None`: a exceção original pode trazer valores do contexto, ou seja, da lead.
            raise TemplateProblem(self.name, line, _kind_of(exc)) from None


def _join_within_limit(chunks: Iterable[str]) -> str:
    """Junta os pedaços do texto renderizado e para ao passar de `MAX_OUTPUT`: um laço que
    acrescenta texto a cada volta não enche a memória até o fim."""
    parts: list[str] = []
    size = 0
    for chunk in chunks:
        size += len(chunk)
        if size > MAX_OUTPUT:
            raise LimitError("texto renderizado grande demais")
        parts.append(chunk)
    return "".join(parts)


def _line_of(error: BaseException, name: str) -> int | None:
    """A linha do template na pilha da exceção (o Jinja reescreve a pilha com as linhas dele)."""
    lines = [
        frame.lineno
        for frame in traceback.extract_tb(error.__traceback__)
        if frame.filename == name
    ]
    return lines[-1] if lines else None


def _kind_of(error: BaseException) -> str:
    """O tipo do erro, sem a mensagem original."""
    if isinstance(error, RenderLimit):  # a mensagem é fixa, de `app.forms.render_guard`
        return f"a renderização {error}"
    if isinstance(error, LimitError):
        return "operação grande demais para o ambiente isolado"
    if isinstance(error, SecurityError):
        return "acesso proibido pelo ambiente isolado"
    if isinstance(error, UndefinedError):
        return "variável ou campo inexistente"
    return f"erro ao renderizar ({type(error).__name__})"
