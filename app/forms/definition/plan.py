"""O modelo do plano a partir da árvore de campos da definição.

- A ordem da árvore é a ordem do schema, e por isso a ordem em que o LLM escreve.
- O modo estrito da saída estruturada não leva ao LLM os limites de tamanho e de quantidade
  (`maxLength`, `minItems`...). Por isso a descrição de cada campo ganha os limites dele, escritos
  por extenso depois do texto da operadora: "Entre 3 e 120 caracteres.", "Exatamente 3 itens.".
- A descrição de um grupo vira a descrição do modelo dele no schema (como a docstring de um
  modelo escrito em Python).
- Campo interno (`internal`) vai ao LLM e fica no plano gravado e no webhook, mas não chega ao PDF
  nem à prévia (`lead_view`).
- O rótulo (`label`) é só do admin, que o mostra no lugar do nome do campo (`plan_labels`): não entra
  no schema do LLM, no plano gravado nem no PDF.
- As validações declarativas viram validadores do próprio modelo, e por isso valem em toda parte
  em que o plano é validado: a resposta do LLM, o plano de exemplo e o PDF. Cada recusa diz o
  caminho e a regra, nunca o texto do plano:
  - expressões proibidas (`ForbiddenDoc`): sem diferença de maiúsculas, de acentos nem de
    espaços, em qualquer parte do texto;
  - padrão proibido de um campo (`forbidden_pattern`): uma expressão regular, sem diferença de
    maiúsculas. Roda no módulo `regex`, com um tempo máximo por texto (`PATTERN_TIMEOUT`): um padrão
    com backtracking catastrófico não prende o processo, e o texto que ele não conseguiu conferir
    é recusado, como se tivesse o padrão;
  - mínimo de parágrafos (`min_paragraphs`): os parágrafos se separam por linha em branco.
- Um campo de opções fixas pode tirar os valores de uma lista dos dados do formulário, ou de
  várias (`values_from`: um nome, ou uma lista de nomes): o modelo guarda os valores no momento da
  compilação, a união das listas na ordem dos nomes, sem repetir.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Annotated, Any, Literal

import regex
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, create_model
from pydantic_core import PydanticCustomError

from app.forms.definition.document import (
    ForbiddenDoc,
    ListBlock,
    PlanDoc,
    PlanNodeDoc,
    Problem,
    counted,
    data_lists,
)

ROOT_MODEL = "Plan"
# Segundos que um padrão proibido tem para conferir o texto de um campo. Um padrão normal leva
# microssegundos; o tempo só pesa para um padrão que repete grupos sobrepostos, como `(a|aa)+$`.
PATTERN_TIMEOUT = 0.5
SENTENCE_ENDINGS = (".", "!", "?", "…", ":")
# As chaves próprias de cada tipo de nó; as dos outros tipos não valem nele.
KIND_KEYS = {
    "text": {"min_length", "max_length", "min_paragraphs", "forbidden_pattern"},
    "list": {"min_items", "max_items", "items"},
    "group": {"fields", "model"},
    "options": {"values", "values_from", "in_window"},
}
SPECIFIC_KEYS = sorted(set().union(*KIND_KEYS.values()))


# --- conferência ----------------------------------------------------------------------------


def comparable(text: str) -> str:
    """O texto como as expressões proibidas o comparam: sem acentos, em minúsculas e com os espaços
    e as quebras de linha reduzidos a um espaço."""
    plain = unicodedata.normalize("NFD", text)
    plain = "".join(char for char in plain if unicodedata.category(char) != "Mn")
    return " ".join(plain.casefold().split())


def text_nodes(
    fields: list[PlanNodeDoc], trail: tuple[str, ...] = ()
) -> Iterator[tuple[str, PlanNodeDoc]]:
    """Os campos de texto do plano com o caminho dos nomes deles (`titulo`, `fases.foco`), na ordem
    da árvore. Os itens de uma lista não têm nome e não entram no caminho: `acoes` é o texto de
    cada item da lista `acoes`."""

    def visit(node: PlanNodeDoc, at: tuple[str, ...]) -> Iterator[tuple[str, PlanNodeDoc]]:
        if node.kind == "text":
            yield ".".join(at), node
        elif node.kind == "list" and node.items is not None:
            yield from visit(node.items, at)
        elif node.kind == "group":
            for child in node.fields or ():
                yield from visit(child, (*at, child.name or ""))

    for field_ in fields:
        yield from visit(field_, (*trail, field_.name or ""))


def plan_labels(fields: list[PlanNodeDoc], trail: tuple[str, ...] = ()) -> dict[str, str]:
    """Os rótulos dos campos que têm um, pelo caminho dos nomes (`situacao`, `fases.foco`). Os itens
    de uma lista não têm nome nem rótulo: os campos de um grupo de itens seguem o caminho da lista.
    O admin cai no nome do campo quando o caminho não está aqui."""
    found: dict[str, str] = {}

    def visit(node: PlanNodeDoc, at: tuple[str, ...]) -> None:
        if node.label:
            found[".".join(at)] = node.label
        holder = node.items if node.kind == "list" and node.items is not None else node
        for child in holder.fields or ():
            visit(child, (*at, child.name or ""))

    for field_ in fields:
        visit(field_, (*trail, field_.name or ""))
    return found


def text_paths(fields: list[PlanNodeDoc], trail: tuple[str, ...] = ()) -> list[str]:
    """Só os caminhos de `text_nodes`."""
    return [path for path, _ in text_nodes(fields, trail)]


def plan_problems(plan: PlanDoc, data: Mapping[str, Any] | None = None) -> list[Problem]:
    """O que a estrutura não confere sozinha na árvore do modelo do plano. Os nomes dos modelos
    dos grupos (o da definição ou o derivado do campo) não se repetem: viram as definições do
    schema. `data` são os blocos dos dados do formulário, de onde as opções podem vir."""
    problems: list[Problem] = []
    models = {ROOT_MODEL}
    blocks = data or {}

    def refuse(path: str, message: str) -> None:
        problems.append(Problem(path, message))

    def group(fields: list[PlanNodeDoc], at: str) -> None:
        names: set[str] = set()
        for i, field_ in enumerate(fields):
            where = f"{at}.{i}"
            if field_.name is None:
                refuse(f"{where}.name", "campo obrigatório")
            elif field_.name in names:
                refuse(f"{where}.name", "outro campo do grupo já usa este nome")
            names.add(field_.name or "")
            node(field_, where, pascal(field_.name) if field_.name else "")

    def items(item: PlanNodeDoc, where: str, fallback: str) -> None:
        if item.name is not None:
            refuse(f"{where}.name", "os itens de uma lista não têm nome")
        if item.internal:
            refuse(f"{where}.internal", "marque como interno o campo da lista, não os itens")
        if item.label is not None:
            refuse(f"{where}.label", "dê o rótulo ao campo da lista, não aos itens")
        if item.kind == "list":
            refuse(f"{where}.kind", "lista de listas não é suportada")
            return
        node(item, where, fallback)

    def node(field_: PlanNodeDoc, where: str, fallback: str) -> None:
        for key in SPECIFIC_KEYS:
            if key not in KIND_KEYS[field_.kind] and getattr(field_, key) is not None:
                refuse(f"{where}.{key}", f"não vale para campos do tipo {field_.kind}")
        if field_.kind == "text":
            _check_range(field_.min_length, field_.max_length, f"{where}.min_length", refuse)
            if field_.forbidden_pattern is not None:
                _check_pattern(
                    field_.forbidden_pattern.pattern, f"{where}.forbidden_pattern", refuse
                )
        elif field_.kind == "list":
            _check_range(field_.min_items, field_.max_items, f"{where}.min_items", refuse)
            if field_.items is None:
                refuse(f"{where}.items", "a lista precisa dos itens")
            else:
                items(field_.items, f"{where}.items", f"{fallback}Item")
        elif field_.kind == "options":
            _options(field_, where)
        else:
            name = model_name(field_, fallback)
            if name in models:
                refuse(f"{where}.model", f"outro grupo já usa o nome {name}")
            models.add(name)
            if field_.fields is None:
                refuse(f"{where}.fields", "o grupo precisa dos campos")
            else:
                group(field_.fields, f"{where}.fields")

    def _options(field_: PlanNodeDoc, where: str) -> None:
        if field_.values is None and field_.values_from is None:
            refuse(
                f"{where}.values",
                "as opções fixas precisam dos valores ou de uma lista dos dados do formulário",
            )
        elif field_.values is not None and field_.values_from is not None:
            refuse(f"{where}.values_from", "use os valores fixos ou a lista, não os dois")
        for k, value in enumerate(field_.values or ()):
            if value in (field_.values or ())[:k]:
                refuse(f"{where}.values.{k}", "valor repetido")
        listed = isinstance(
            field_.values_from, list
        )  # um nome só aponta para o campo, não para o item
        seen: set[str] = set()
        for k, source in enumerate(field_.source_names):
            at = f"{where}.values_from.{k}" if listed else f"{where}.values_from"
            block = blocks.get(source)
            if source in seen:
                refuse(at, "lista repetida")
            elif block is None:
                refuse(at, f"não há uma lista chamada {source} nos dados do formulário")
            elif not isinstance(block, ListBlock):
                refuse(at, f"o bloco {source} dos dados do formulário não é uma lista")
            seen.add(source)

    group(plan.fields, "plan.fields")
    texts = set(text_paths(plan.fields))
    for r, rule in enumerate(plan.forbidden):
        _check_rule(rule, f"plan.forbidden.{r}", texts, refuse)
    return problems


def _check_rule(
    rule: ForbiddenDoc, at: str, texts: set[str], refuse: Callable[[str, str], None]
) -> None:
    seen: set[str] = set()
    for k, expression in enumerate(rule.expressions):
        key = comparable(expression)
        if key in seen:
            refuse(f"{at}.expressions.{k}", "expressão repetida na regra")
        seen.add(key)
    if rule.scope == "all" and rule.fields:
        refuse(f"{at}.fields", "a regra vale para o plano inteiro: tire os campos ou mude o escopo")
    if rule.scope != "all" and not rule.fields:
        refuse(f"{at}.fields", "escolha ao menos um campo")
    chosen: set[str] = set()
    for k, path in enumerate(rule.fields):
        if path not in texts:
            refuse(f"{at}.fields.{k}", "não é um campo de texto do plano")
        elif path in chosen:
            refuse(f"{at}.fields.{k}", "campo repetido na regra")
        chosen.add(path)


def _check_pattern(pattern: str, path: str, refuse: Callable[[str, str], None]) -> None:
    try:
        regex.compile(pattern, regex.IGNORECASE)
    except regex.error as exc:
        refuse(f"{path}.pattern", f"expressão regular inválida (posição {exc.pos or 0})")


def _check_range(
    low: int | None, high: int | None, path: str, refuse: Callable[[str, str], None]
) -> None:
    if low is not None and high is not None and low > high:
        refuse(path, f"o mínimo passa do máximo ({high})")


# --- o modelo -------------------------------------------------------------------------------


def range_sentence(low: int | None, high: int | None, singular: str, plural: str) -> str | None:
    """O limite por extenso ("Entre 3 e 120 caracteres."). Mínimo 0 é o mesmo que nenhum mínimo.
    O admin escreve os limites que mostra com a mesma frase."""
    low = low or None
    if low is not None and high is not None:
        if low == high:
            return f"Exatamente {counted(low, singular, plural)}."
        return f"Entre {low} e {counted(high, singular, plural)}."
    if high is not None:
        return f"Até {counted(high, singular, plural)}."
    if low is not None:
        return f"Pelo menos {counted(low, singular, plural)}."
    return None


def limits_of(node: PlanNodeDoc) -> str | None:
    if node.kind == "text":
        return range_sentence(node.min_length, node.max_length, "caractere", "caracteres")
    if node.kind == "list":
        return range_sentence(node.min_items, node.max_items, "item", "itens")
    return None


def paragraphs_sentence(minimum: int | None) -> str | None:
    """O mínimo de parágrafos por extenso ("Pelo menos 3 parágrafos, separados por linha em
    branco."). Mínimo 0 é o mesmo que nenhum mínimo."""
    if not minimum:
        return None
    sentence = f"Pelo menos {counted(minimum, 'parágrafo', 'parágrafos')}"
    return f"{sentence}." if minimum == 1 else f"{sentence}, separados por linha em branco."


def description_of(node: PlanNodeDoc) -> str | None:
    """O texto da operadora, terminado em ponto, seguido dos limites do campo."""
    text = (node.description or "").strip()
    if text and not text.endswith(SENTENCE_ENDINGS):
        text += "."
    paragraphs = paragraphs_sentence(node.min_paragraphs) if node.kind == "text" else None
    described = " ".join(part for part in (text, limits_of(node), paragraphs) if part)
    return described or None


def model_name(node: PlanNodeDoc, fallback: str) -> str:
    """O nome do modelo de um grupo no schema: o da definição ou um derivado do campo."""
    return node.model or fallback


def pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


@dataclass(frozen=True)
class _Build:
    """O que o modelo precisa além da árvore: as regras de expressões proibidas do plano e as
    listas dos dados do formulário de onde as opções podem vir."""

    forbidden: tuple[ForbiddenDoc, ...]
    lists: Mapping[str, tuple[str, ...]]

    def rules_at(self, path: str) -> list[ForbiddenDoc]:
        """As regras de expressões proibidas que valem no campo de texto deste caminho."""
        return [
            rule
            for rule in self.forbidden
            if rule.scope == "all" or (path in rule.fields) == (rule.scope == "only")
        ]


def _expression_check(expressions: list[str]) -> Callable[[str], str]:
    keys = [(expression, comparable(expression)) for expression in expressions]

    def check(value: str) -> str:
        text = comparable(value)
        found = [expression for expression, key in keys if key and key in text]
        if len(found) == 1:
            raise PydanticCustomError(
                "forbidden_expression", "contém a expressão proibida: {found}", {"found": found[0]}
            )
        if found:
            raise PydanticCustomError(
                "forbidden_expression",
                "contém as expressões proibidas: {found}",
                {"found": ", ".join(found)},
            )
        return value

    return check


def _pattern_check(pattern: str, message: str | None) -> Callable[[str], str]:
    compiled = regex.compile(pattern, regex.IGNORECASE)
    shown = message or "contém um trecho que este campo não aceita"

    def check(value: str) -> str:
        try:
            found = compiled.search(value, timeout=PATTERN_TIMEOUT)
        except TimeoutError:
            raise PydanticCustomError(
                "forbidden_pattern_timeout",
                "o padrão proibido deste campo demorou demais para conferir o texto: simplifique-o",
            ) from None
        if found:
            raise PydanticCustomError("forbidden_pattern", "{message}", {"message": shown})
        return value

    return check


def _paragraphs_check(minimum: int) -> Callable[[str], str]:
    ask = f"escreva pelo menos {counted(minimum, 'parágrafo', 'parágrafos')}"
    if minimum > 1:
        ask += ", separados por linha em branco"

    def check(value: str) -> str:
        if len([part for part in re.split(r"\n\s*\n", value) if part.strip()]) < minimum:
            raise PydanticCustomError("too_few_paragraphs", "{ask}", {"ask": ask})
        return value

    return check


def _text_checks(node: PlanNodeDoc, path: str, build: _Build) -> list[AfterValidator]:
    """Os validadores declarativos de um campo de texto: o mínimo de parágrafos, o padrão proibido
    do campo e as expressões proibidas das regras do plano que valem nele."""
    checks: list[Callable[[str], str]] = []
    if node.min_paragraphs:
        checks.append(_paragraphs_check(node.min_paragraphs))
    if node.forbidden_pattern is not None:
        checks.append(
            _pattern_check(node.forbidden_pattern.pattern, node.forbidden_pattern.message)
        )
    for rule in build.rules_at(path):
        checks.append(_expression_check(rule.expressions))
    return [AfterValidator(check) for check in checks]


def _values_of(node: PlanNodeDoc, build: _Build) -> tuple[str, ...]:
    values = node.option_values(build.lists)
    assert values is not None  # a validação da definição garante que as listas existem
    return values


def _type(node: PlanNodeDoc, name: str, trail: tuple[str, ...], build: _Build) -> Any:
    """O tipo de um nó. `name` é o nome do modelo, se o nó for um grupo; `trail`, os nomes dos
    campos até aqui (o caminho que as regras do plano usam)."""
    if node.kind == "text":
        checks = _text_checks(node, ".".join(trail), build)
        return Annotated[(str, *checks)] if checks else str
    if node.kind == "options":
        return Literal[_values_of(node, build)]
    if node.kind == "group":
        return group_model(
            model_name(node, name), node.description, node.fields or [], trail=trail, build=build
        )
    items = node.items
    assert items is not None  # a validação da definição garante os itens de toda lista
    item = _type(items, f"{name}Item", trail, build)
    if items.kind != "group":  # texto e opções levam os limites e a descrição no próprio item
        item = Annotated[
            item,
            Field(
                min_length=items.min_length,
                max_length=items.max_length,
                description=description_of(items),
            ),
        ]
    return list[item]


def _field(node: PlanNodeDoc, trail: tuple[str, ...], build: _Build) -> tuple[Any, Any]:
    assert node.name is not None  # todo campo de um grupo tem nome
    at = (*trail, node.name)
    kind = _type(node, pascal(node.name), at, build)
    if node.kind == "group":
        return kind, Field()  # a descrição do grupo fica no modelo dele
    if node.kind == "list":
        limits = {"min_length": node.min_items, "max_length": node.max_items}
    elif node.kind == "text":
        limits = {"min_length": node.min_length, "max_length": node.max_length}
    else:
        limits = {}
    return kind, Field(description=description_of(node), **limits)


def group_model(
    name: str,
    description: str | None,
    fields: list[PlanNodeDoc],
    *,
    trail: tuple[str, ...],
    build: _Build,
) -> type[BaseModel]:
    definitions = {field.name: _field(field, trail, build) for field in fields}
    return create_model(
        name,
        __config__=ConfigDict(extra="forbid"),
        __doc__=(description or "").strip() or None,
        **definitions,
    )


def plan_model(plan: PlanDoc, data: Mapping[str, Any] | None = None) -> type[BaseModel]:
    """O modelo do plano da árvore. `data` são os blocos dos dados do formulário: as listas deles
    dão as opções dos campos com `values_from`."""
    build = _Build(forbidden=tuple(plan.forbidden), lists=data_lists(data or {}))
    return group_model(ROOT_MODEL, plan.description, plan.fields, trail=(), build=build)


def lead_view(plan: PlanDoc) -> Callable[[Mapping[str, Any]], SimpleNamespace]:
    """O plano como o PDF e a prévia o recebem: sem os campos internos, com acesso por atributo
    (`plan.fases[0].nome`) e nada além dos campos."""

    def view(node: PlanNodeDoc, value: Any) -> Any:
        if node.kind == "group":
            return group(node.fields or [], value)
        if node.kind == "list" and node.items is not None:
            return [view(node.items, item) for item in value]
        return value

    def group(fields: list[PlanNodeDoc], value: Mapping[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            **{
                field.name: view(field, value[field.name])
                for field in fields
                if field.name is not None and not field.internal
            }
        )

    return lambda document: group(plan.fields, document)
