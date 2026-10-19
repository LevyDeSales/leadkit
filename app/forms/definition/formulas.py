"""A linguagem de fórmulas da pontuação: pequena, conferida ao salvar e avaliada sem executar código.

Quem escreve é a operadora, no admin. O texto é lido por um analisador próprio (`parse`), que só
conhece o que está na lista abaixo; o que não está nela nem chega a virar uma fórmula. Nada passa
pelo `eval`, pelo `ast` ou por atributos do Python, e o avaliador só lê os valores que recebe.

O que existe
------------
- números (`42`, `0.4`, com ponto decimal) e textos entre aspas duplas (`"vendas"`), que só servem
  para comparar com uma resposta;
- as quatro operações `+ - * /`, o sinal de menos e os parênteses, com a precedência de sempre;
- comparações `== != < <= > >=` e as combinações `e`, `ou` e `nao` (o `e` vale antes do `ou`);
- nomes: uma nota, o nível de uma faixa (`nivel_x`, um texto) ou uma pergunta de escolha única (a
  opção marcada, um texto);
- as funções de `FUNCTIONS`: `pontos` e `contagem` (das respostas), `minimo`, `maximo`, `soma`,
  `media`, `se` (condição), `faixa` e os dois arredondamentos.

O arredondamento é sempre explícito, dentro da fórmula: `arredondar_meio_para_cima` (2,5 vira 3; o
`Math.round` do JS, usado no MCC) ou `arredondar_meio_para_o_par` (2,5 vira 2; o `round` do Python,
usado no reforma-2027). Sem ele, a nota guarda as casas decimais.

A conta é a do ponto flutuante do Python, na ordem em que a fórmula está escrita: as notas que hoje
são código dão os mesmos números, inclusive onde uma soma cai no meio de dois inteiros.

Uso
---
    formula = parse("arredondar_meio_para_cima(0.4 * pontos(a) + 0.6 * pontos(b))")
    formula.check(scope)        # o tipo (`NUMBER`, `TEXT` ou `BOOL`); levanta `FormulaError`
    formula.evaluate(env)       # o valor; levanta `FormulaError` (divisão por zero, por exemplo)

`FormulaError` traz a mensagem em português e a coluna (a partir de 1), sem o valor de nenhuma
resposta.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

NUMBER = "number"
TEXT = "text"
BOOL = "bool"
TYPE_NAMES = {NUMBER: "número", TEXT: "texto", BOOL: "verdadeiro ou falso"}

MAX_LENGTH = 2000
# Teto de símbolos: a árvore de uma soma comprida é funda, e o avaliador desce nela. A fórmula mais
# longa de hoje (a urgência do MCC) tem uns 100.
MAX_TOKENS = 400
MAX_DEPTH = 40
MAX_NUMBER_DIGITS = 15
MAX_TEXT_LENGTH = 80

KEYWORDS = frozenset({"e", "ou", "nao"})
# O que cada função faz, como o admin e a documentação mostram.
FUNCTIONS: dict[str, str] = {
    "pontos": "pontos(pergunta): os pontos da opção marcada (a soma, numa escolha múltipla)",
    "contagem": "contagem(pergunta): quantas opções foram marcadas numa escolha múltipla",
    "minimo": "minimo(a, b, ...): o menor valor",
    "maximo": "maximo(a, b, ...): o maior valor",
    "soma": "soma(a, b, ...): a soma dos valores",
    "media": "media(a, b, ...): a média dos valores",
    "se": "se(condição, a, b): a se a condição vale, senão b",
    "faixa": (
        "faixa(valor, limite1, resultado1, limite2, resultado2, ..., padrão): o resultado do "
        "primeiro limite que o valor alcança (valor >= limite); senão o padrão"
    ),
    "arredondar_meio_para_cima": "arredondar_meio_para_cima(x): 2,5 vira 3 e -2,5 vira -2",
    "arredondar_meio_para_o_par": "arredondar_meio_para_o_par(x): 2,5 vira 2 e 3,5 vira 4",
}
RESERVED_WORDS = KEYWORDS | FUNCTIONS.keys()

_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<number>[0-9]+(?:\.[0-9]+)?)
  | (?P<text>"[^"\n]*")
  | (?P<name>[a-z][a-z0-9_]*)
  | (?P<bad>\*\*|//|:=|%)
  | (?P<op>==|!=|<=|>=|[<>+\-*/(),])
  | (?P<lone>=|!)
    """,
    re.VERBOSE,
)
_BAD_OPERATORS = {
    "**": "a potência não existe nas fórmulas; as operações são +, -, * e /",
    "//": "a divisão inteira não existe nas fórmulas; use / e um arredondamento",
    "%": "o resto da divisão não existe nas fórmulas; as operações são +, -, * e /",
    ":=": "não há atribuição nas fórmulas; cada nota tem a sua fórmula",
    "=": "para comparar, use ==",
    "!": "para negar, use nao (ou !=, para diferente)",
}


class FormulaError(ValueError):
    """Uma fórmula que não vale: `message` explica o problema, e `column` diz onde (a partir de
    1; None quando o problema é da fórmula inteira)."""

    def __init__(self, message: str, column: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.column = column

    @property
    def detail(self) -> str:
        """A mensagem com a coluna: o que o admin mostra."""
        return self.message if self.column is None else f"{self.message} (coluna {self.column})"


# --- a árvore -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Num:
    value: int | float
    pos: int


@dataclass(frozen=True)
class Str:
    value: str
    pos: int


@dataclass(frozen=True)
class Ref:
    name: str
    pos: int


@dataclass(frozen=True)
class Neg:
    operand: Node
    pos: int


@dataclass(frozen=True)
class Not:
    operand: Node
    pos: int


@dataclass(frozen=True)
class Op:
    op: str
    left: Node
    right: Node
    pos: int


@dataclass(frozen=True)
class Call:
    fn: str
    args: tuple[Node, ...]
    pos: int


Node = Num | Str | Ref | Neg | Not | Op | Call
ARITHMETIC = ("+", "-", "*", "/")
EQUALITY = ("==", "!=")
ORDER = ("<", "<=", ">", ">=")


# --- leitura ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: str  # number | text | name | op | end
    text: str
    pos: int

    @property
    def shown(self) -> str:
        return "o fim da fórmula" if self.kind == "end" else f"`{self.text}`"


def _tokens(source: str) -> list[_Token]:
    found: list[_Token] = []
    pos = 0
    while pos < len(source):
        match = _TOKEN.match(source, pos)
        if match is None:
            char = source[pos]
            if char.isalpha() or char == "_":
                hint = "nomes usam letras minúsculas sem acento, números e _, começando por letra"
                raise FormulaError(f"`{char}` não vale aqui: {hint}", pos + 1)
            raise FormulaError(f"o caractere `{char}` não existe nas fórmulas", pos + 1)
        kind = match.lastgroup or "space"
        if kind in ("bad", "lone"):
            raise FormulaError(_BAD_OPERATORS[match[0]], pos + 1)
        if kind != "space":
            found.append(_Token(kind, match[0], pos))
        pos = match.end()
    if len(found) > MAX_TOKENS:
        raise FormulaError(f"a fórmula é grande demais (no máximo {MAX_TOKENS} símbolos)")
    found.append(_Token("end", "", len(source)))
    return found


class _Parser:
    def __init__(self, source: str) -> None:
        self.tokens = _tokens(source)
        self.index = 0
        self.depth = 0

    @property
    def token(self) -> _Token:
        return self.tokens[self.index]

    def advance(self) -> _Token:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def at(self, *texts: str) -> bool:
        return self.token.kind in ("op", "name") and self.token.text in texts

    def fail(self, expected: str) -> FormulaError:
        token = self.token
        return FormulaError(f"esperava {expected}, mas achei {token.shown}", token.pos + 1)

    def expect(self, text: str, expected: str) -> _Token:
        if not (self.token.kind == "op" and self.token.text == text):
            raise self.fail(expected)
        return self.advance()

    def nested(self, parse: Callable[[], Node]) -> Node:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise FormulaError("a fórmula tem parênteses e chamadas demais, um dentro do outro")
        try:
            return parse()
        finally:
            self.depth -= 1

    def formula(self) -> Node:
        node = self.nested(self.disjunction)
        if self.token.kind != "end":
            raise self.fail("um operador ou o fim da fórmula")
        return node

    def disjunction(self) -> Node:
        node = self.conjunction()
        while self.at("ou"):
            pos = self.advance().pos
            node = Op("ou", node, self.conjunction(), pos)
        return node

    def conjunction(self) -> Node:
        node = self.negation()
        while self.at("e"):
            pos = self.advance().pos
            node = Op("e", node, self.negation(), pos)
        return node

    def negation(self) -> Node:
        if self.at("nao"):
            pos = self.advance().pos
            return Not(self.nested(self.negation), pos)
        return self.comparison()

    def comparison(self) -> Node:
        node = self.sum()
        if self.at(*EQUALITY, *ORDER):
            token = self.advance()
            node = Op(token.text, node, self.sum(), token.pos)
            if self.at(*EQUALITY, *ORDER):
                raise FormulaError(
                    "compare dois valores por vez; combine as comparações com e / ou",
                    self.token.pos + 1,
                )
        return node

    def sum(self) -> Node:
        node = self.term()
        while self.at("+", "-"):
            token = self.advance()
            node = Op(token.text, node, self.term(), token.pos)
        return node

    def term(self) -> Node:
        node = self.unary()
        while self.at("*", "/"):
            token = self.advance()
            node = Op(token.text, node, self.unary(), token.pos)
        return node

    def unary(self) -> Node:
        if self.at("-"):
            pos = self.advance().pos
            return Neg(self.nested(self.unary), pos)
        return self.primary()

    def primary(self) -> Node:
        token = self.token
        if token.kind == "number":
            self.advance()
            digits = token.text.replace(".", "")
            if len(digits) > MAX_NUMBER_DIGITS:
                raise FormulaError(
                    f"o número tem dígitos demais (no máximo {MAX_NUMBER_DIGITS})", token.pos + 1
                )
            return Num(float(token.text) if "." in token.text else int(token.text), token.pos)
        if token.kind == "text":
            self.advance()
            text = token.text[1:-1]
            if len(text) > MAX_TEXT_LENGTH:
                raise FormulaError(f"o texto passa de {MAX_TEXT_LENGTH} caracteres", token.pos + 1)
            return Str(text, token.pos)
        if token.kind == "name":
            if token.text in KEYWORDS:
                raise self.fail("um valor")
            self.advance()
            if self.at("("):
                return self.call(token)
            return Ref(token.text, token.pos)
        if self.at("("):
            self.advance()
            node = self.nested(self.disjunction)
            self.expect(")", "`)` para fechar o parêntese")
            return node
        raise self.fail("um número, uma nota, uma pergunta ou uma função")

    def call(self, name: _Token) -> Node:
        if name.text not in FUNCTIONS:
            listed = ", ".join(FUNCTIONS)
            raise FormulaError(
                f"a função {name.text} não existe; as funções são: {listed}", name.pos + 1
            )
        self.advance()  # (
        args: list[Node] = []
        if not self.at(")"):
            args.append(self.nested(self.disjunction))
            while self.at(","):
                self.advance()
                args.append(self.nested(self.disjunction))
        self.expect(")", "`,` ou `)` na chamada de " + name.text)
        return Call(name.text, tuple(args), name.pos)


@dataclass(frozen=True)
class Formula:
    """Uma fórmula lida: conferida na sintaxe, ainda não nos nomes (`check`)."""

    source: str
    root: Node

    def names(self) -> tuple[str, ...]:
        """Os nomes que a fórmula lê como valor (notas, níveis e perguntas de escolha única), na
        ordem em que aparecem. Os das chamadas de `pontos` e `contagem` não entram."""
        found: list[str] = []
        _walk(self.root, lambda node: found.append(node.name) if isinstance(node, Ref) else None)
        return tuple(dict.fromkeys(found))

    def questions(self) -> tuple[str, ...]:
        """As perguntas citadas em `pontos(...)` e `contagem(...)`."""
        found: list[str] = []

        def visit(node: Node) -> None:
            if isinstance(node, Call) and node.fn in ("pontos", "contagem"):
                found.extend(arg.name for arg in node.args if isinstance(arg, Ref))

        _walk(self.root, visit, calls=True)
        return tuple(dict.fromkeys(found))

    def check(self, scope: Scope) -> str:
        """O tipo da fórmula (`NUMBER`, `TEXT` ou `BOOL`). Levanta `FormulaError` no primeiro
        problema: nome inexistente, função com argumentos errados ou tipos que não combinam."""
        return _check(self.root, scope)

    def evaluate(self, env: Env) -> Any:
        """O valor da fórmula com os valores de `env`. Levanta `FormulaError` (divisão por zero,
        resultado que não é um número)."""
        return _evaluate(self.root, env)


def parse(source: str) -> Formula:
    """Lê a fórmula. Levanta `FormulaError` com a coluna do primeiro problema de sintaxe."""
    if not source.strip():
        raise FormulaError("a fórmula está vazia")
    if len(source) > MAX_LENGTH:
        raise FormulaError(f"a fórmula passa de {MAX_LENGTH} caracteres")
    return Formula(source, _Parser(source).formula())


def _walk(node: Node, visit: Callable[[Node], None], *, calls: bool = False) -> None:
    """Percorre a árvore. Os argumentos de `pontos` e `contagem` são nomes de pergunta, e não
    valores: só entram na visita com `calls`."""
    visit(node)
    if isinstance(node, Neg | Not):
        _walk(node.operand, visit, calls=calls)
    elif isinstance(node, Op):
        _walk(node.left, visit, calls=calls)
        _walk(node.right, visit, calls=calls)
    elif isinstance(node, Call):
        if node.fn in ("pontos", "contagem") and not calls:
            return
        for arg in node.args:
            _walk(arg, visit, calls=calls)


# --- conferência dos nomes e dos tipos ----------------------------------------------------------


@dataclass(frozen=True)
class QuestionInfo:
    """O que a fórmula precisa saber de uma pergunta: o tipo e as opções sem pontos."""

    kind: str  # text | email | tel | textarea | choice | choices
    missing_points: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scope:
    """Os nomes que uma fórmula pode usar: `values` (nota, nível de faixa e pergunta de escolha
    única, com o tipo) e `questions` (todas as perguntas, para `pontos` e `contagem`)."""

    values: Mapping[str, str]
    questions: Mapping[str, QuestionInfo]


def _kind_phrase(name: str, scope: Scope) -> str | None:
    info = scope.questions.get(name)
    if info is None:
        return None
    if info.kind == "choices":
        return (
            f"{name} é uma pergunta de escolha múltipla: use contagem({name}) para contar as "
            f"opções ou pontos({name}) para somar os pontos"
        )
    return f"{name} é uma pergunta de texto: só as de escolha entram nas fórmulas"


def _describe(kind: str) -> str:
    return TYPE_NAMES[kind]


def _numbers(args: Iterable[Node], scope: Scope, what: str) -> None:
    for arg in args:
        found = _check(arg, scope)
        if found != NUMBER:
            raise FormulaError(
                f"{what} só vale com números, mas isto é {_describe(found)}", arg.pos + 1
            )


def _question_argument(call: Call, scope: Scope) -> QuestionInfo:
    if len(call.args) != 1 or not isinstance(call.args[0], Ref):
        raise FormulaError(
            f"{call.fn} recebe o nome de uma pergunta, por exemplo {call.fn}(nome_da_pergunta)",
            call.pos + 1,
        )
    ref = call.args[0]
    info = scope.questions.get(ref.name)
    if info is None:
        raise FormulaError(f"{ref.name}: não existe pergunta com este nome", ref.pos + 1)
    return info


def _check(node: Node, scope: Scope) -> str:
    match node:
        case Num():
            return NUMBER
        case Str():
            return TEXT
        case Ref(name=name):
            found = scope.values.get(name)
            if found is not None:
                return found
            message = _kind_phrase(name, scope)
            raise FormulaError(
                message or f"{name}: não é uma nota nem uma pergunta de escolha", node.pos + 1
            )
        case Neg(operand=operand):
            _numbers([operand], scope, "o sinal de menos")
            return NUMBER
        case Not(operand=operand):
            found = _check(operand, scope)
            if found != BOOL:
                raise FormulaError(
                    f"nao só vale diante de uma comparação, mas isto é {_describe(found)}",
                    operand.pos + 1,
                )
            return BOOL
        case Op():
            return _check_operation(node, scope)
        case Call():
            return _check_call(node, scope)
    raise AssertionError(node)  # pragma: no cover - todo nó da árvore tem um caso


def _check_operation(node: Op, scope: Scope) -> str:
    left, right = _check(node.left, scope), _check(node.right, scope)
    if node.op in ARITHMETIC:
        for side, found in ((node.left, left), (node.right, right)):
            if found != NUMBER:
                raise FormulaError(
                    f"{node.op} só vale entre números, mas isto é {_describe(found)}",
                    side.pos + 1,
                )
        return NUMBER
    if node.op in ORDER:
        for side, found in ((node.left, left), (node.right, right)):
            if found != NUMBER:
                raise FormulaError(
                    f"{node.op} só compara números, mas isto é {_describe(found)}",
                    side.pos + 1,
                )
        return BOOL
    if node.op in EQUALITY:
        if left != right or left == BOOL:
            raise FormulaError(
                f"{node.op} compara dois números ou dois textos, mas aqui são "
                f"{_describe(left)} e {_describe(right)}",
                node.pos + 1,
            )
        return BOOL
    for side, found in ((node.left, left), (node.right, right)):  # e, ou
        if found != BOOL:
            raise FormulaError(
                f"{node.op} só junta comparações, mas isto é {_describe(found)}", side.pos + 1
            )
    return BOOL


def _check_call(call: Call, scope: Scope) -> str:
    fn, args = call.fn, call.args
    if fn == "pontos":
        info = _question_argument(call, scope)
        name = args[0].name  # type: ignore[attr-defined]
        if info.kind not in ("choice", "choices"):
            raise FormulaError(
                f"pontos({name}): {name} não é uma pergunta de escolha", args[0].pos + 1
            )
        if info.missing_points:
            missing = ", ".join(info.missing_points)
            raise FormulaError(
                f"pontos({name}): faltam os pontos das opções {missing}", args[0].pos + 1
            )
        return NUMBER
    if fn == "contagem":
        info = _question_argument(call, scope)
        name = args[0].name  # type: ignore[attr-defined]
        if info.kind != "choices":
            raise FormulaError(
                f"contagem({name}): {name} não é uma pergunta de escolha múltipla",
                args[0].pos + 1,
            )
        return NUMBER
    if fn in ("minimo", "maximo", "soma", "media"):
        if not args:
            raise FormulaError(f"{fn} precisa de pelo menos um valor", call.pos + 1)
        _numbers(args, scope, fn)
        return NUMBER
    if fn == "se":
        if len(args) != 3:
            raise FormulaError("se precisa de três partes: se(condição, a, b)", call.pos + 1)
        condition, first, second = (_check(arg, scope) for arg in args)
        if condition != BOOL:
            raise FormulaError(
                f"a condição de se precisa ser verdadeiro ou falso, mas isto é "
                f"{_describe(condition)}",
                args[0].pos + 1,
            )
        if first != second or first == BOOL:
            raise FormulaError(
                f"as duas saídas de se precisam ser números ou textos do mesmo tipo, mas aqui são "
                f"{_describe(first)} e {_describe(second)}",
                args[1].pos + 1,
            )
        return first
    if fn == "faixa":
        if len(args) < 4 or len(args) % 2:
            raise FormulaError(
                "faixa precisa do valor, de pelo menos um par (limite, resultado) e do padrão: "
                "faixa(valor, limite1, resultado1, ..., padrão)",
                call.pos + 1,
            )
        _numbers(args, scope, "faixa")
        return NUMBER
    if len(args) != 1:  # os dois arredondamentos
        raise FormulaError(f"{fn} recebe um valor só: {fn}(valor)", call.pos + 1)
    _numbers(args, scope, fn)
    return NUMBER


# --- avaliação ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Env:
    """Os valores de uma avaliação: `values` (notas, níveis de faixa e respostas de escolha única,
    None quando a pergunta ficou sem resposta) e como ler os pontos e a contagem de uma pergunta."""

    values: Mapping[str, Any]
    points: Callable[[str], int | float]
    count: Callable[[str], int]


def _total(values: list[int | float]) -> int | float:
    """A soma da esquerda para a direita, como a conta escrita à mão: não depende da versão do
    Python (o `sum` de floats mudou na 3.12)."""
    total = values[0]
    for value in values[1:]:
        total += value
    return total


def half_up(value: float) -> int:
    """O arredondamento meio para cima: 2,5 vira 3 e -2,5 vira -2."""
    return value if isinstance(value, int) else math.floor(value + 0.5)


def half_even(value: float) -> int:
    """O arredondamento meio para o par: 2,5 vira 2 e 3,5 vira 4."""
    return value if isinstance(value, int) else round(value)


def _finite(value: Any, column: int) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise FormulaError("o resultado não é um número válido", column)
    return value


def _evaluate(node: Node, env: Env) -> Any:
    match node:
        case Num(value=value) | Str(value=value):
            return value
        case Ref(name=name):
            return env.values[name]
        case Neg(operand=operand):
            return -_evaluate(operand, env)
        case Not(operand=operand):
            return not _evaluate(operand, env)
        case Op():
            return _operate(node, env)
        case Call():
            return _call(node, env)
    raise AssertionError(node)  # pragma: no cover - todo nó da árvore tem um caso


def _operate(node: Op, env: Env) -> Any:
    op = node.op
    if op == "e":
        return bool(_evaluate(node.left, env)) and bool(_evaluate(node.right, env))
    if op == "ou":
        return bool(_evaluate(node.left, env)) or bool(_evaluate(node.right, env))
    left, right = _evaluate(node.left, env), _evaluate(node.right, env)
    column = node.pos + 1
    if op == "+":
        return _finite(left + right, column)
    if op == "-":
        return _finite(left - right, column)
    if op == "*":
        return _finite(left * right, column)
    if op == "/":
        if right == 0:
            raise FormulaError("divisão por zero", column)
        return _finite(left / right, column)
    if op == "==":
        return bool(left == right)
    if op == "!=":
        return bool(left != right)
    if op == "<":
        return bool(left < right)
    if op == "<=":
        return bool(left <= right)
    if op == ">":
        return bool(left > right)
    return bool(left >= right)


def _call(node: Call, env: Env) -> Any:
    fn, args = node.fn, node.args
    if fn == "pontos":
        return env.points(args[0].name)  # type: ignore[attr-defined]
    if fn == "contagem":
        return env.count(args[0].name)  # type: ignore[attr-defined]
    if fn == "se":
        return _evaluate(args[1] if _evaluate(args[0], env) else args[2], env)
    values = [_evaluate(arg, env) for arg in args]
    column = node.pos + 1
    if fn == "minimo":
        return min(values)
    if fn == "maximo":
        return max(values)
    if fn == "soma":
        return _finite(_total(values), column)
    if fn == "media":
        return _finite(_total(values) / len(values), column)
    if fn == "faixa":
        value, *rest = values
        for limit, result in zip(rest[0:-1:2], rest[1:-1:2], strict=True):
            if value >= limit:
                return result
        return rest[-1]
    value = _finite(values[0], column)
    return half_up(value) if fn == "arredondar_meio_para_cima" else half_even(value)
