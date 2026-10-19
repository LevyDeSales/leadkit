"""O JSON Patch (RFC 6902) sobre o documento de um rascunho: o jeito de a API de admin editar o
rascunho sem uma operação por comando do editor (ADR 0015, "O rascunho é um documento").

`apply` aplica a lista de operações, uma a uma, numa **cópia** do documento, e devolve a cópia. O
documento de quem chama nunca é tocado, e a primeira operação que não se aplica levanta
`DefinitionError`: tudo ou nada, por construção. O resultado ainda não vale nada: quem o guarda o
confere por inteiro, como confere o documento de um salvar (`app.forms.drafts.save_document`).

Uma operação que não se aplica (um caminho que não existe, um índice fora da lista, um `test` que
falha) vira um `Problem` com o campo `patch.<n>`, `n` a posição dela na lista (a partir de 0). A
mensagem é fixa e **nunca traz o caminho nem o valor** da operação: as da biblioteca citam pedaços do
documento (`member 'x' not found in {...}`), e a resposta de erro da API não repete o que recebeu.

O que o aplicador recusa além do que a biblioteca recusa:

- **Índice com zero à esquerda** (`/steps/01`): o RFC 6901 não o aceita, e a `jsonpointer` 3.0.0 o lê
  como o índice 1. Um segmento `0[0-9]+` em `path` ou `from` é `INVALID`.
- **A versão do texto do consentimento** (`/consent/version`): é do sistema (a publicação a sobe
  sozinha quando o texto muda), e o `PUT` a ignora. Uma operação do patch que a escreve é recusada,
  em vez de ser ignorada às escondidas; o `test` dela vale.
- **Mais de `MAX_COPIES` operações `copy`**, e **um documento maior que `MAX_DOCUMENT_BYTES`**: sem
  isso, `copy` do documento nele mesmo o multiplicaria na memória do processo antes de qualquer
  conferência. O `add` e o `replace` não precisam de teto próprio: o que eles põem veio do corpo,
  que a API limita a 256 KiB. Só o `copy` duplica o que já está no documento, e a conta do tamanho
  roda depois de cada um (no máximo `MAX_COPIES` vezes) e uma vez no fim.
- **Aninhamento demais**: o `RecursionError` de uma biblioteca ou da conta do tamanho é o problema
  `patch.<n>`, e nunca um erro 500. O valor de uma operação passa de `MAX_VALUE_DEPTH` níveis já é
  recusado antes, no modelo da API (`depth_of`).

A biblioteca é a `jsonpatch` (decisão 7 da ADR 0015): é um padrão, com implementação mantida, e
escrever o subconjunto em casa seria código de segurança a mais para manter.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import jsonpatch

from app.forms.definition.document import MAX_DEPTH, DefinitionError, Problem

# O teto de operações num patch. O corpo da API já cabe em 256 KiB, e o que importa aqui é não deixar
# um patch de milhares de operações minúsculas prender o processo.
MAX_OPERATIONS = 1000
# Quantos `copy` um patch tem: cada um duplica um pedaço do documento (e, com ele, a conta do tamanho).
MAX_COPIES = 20
# Quantos níveis de listas e objetos o valor de uma operação tem (o mesmo teto do documento).
MAX_VALUE_DEPTH = MAX_DEPTH
# O maior documento que um patch deixa. O documento que um `PATCH` grava tem de caber num `PUT`, que
# devolve o que o `GET` leu: o corpo da API tem 256 KiB (`app.api.main.MAX_BODY_BYTES`, e um teste
# confere), o envelope do `PUT` (`{"expected_version": "<64>", "document": ...}`) pede cerca de 100
# bytes, e o documento vai no corpo **com os acentos escapados** se o cliente escreve o JSON assim (o
# `json.dumps` do Python, por exemplo: `é` vira `\u00e9`, 6 bytes em vez de 2, e há um espaço depois
# de cada vírgula e de cada dois-pontos). A conta é feita desse jeito, o pior caso dos clientes
# comuns, mais a folga de `PUT_ENVELOPE_BYTES`. Um cliente que indenta o JSON gasta mais, e é dele.
BODY_LIMIT_BYTES = 256 * 1024
PUT_ENVELOPE_BYTES = 1024
MAX_DOCUMENT_BYTES = BODY_LIMIT_BYTES - PUT_ENVELOPE_BYTES
FIELD = "patch"

# O que o sistema escreve e um patch não: a versão do texto do consentimento.
SYSTEM_PATHS = frozenset({"/consent/version"})
LEADING_ZERO = re.compile(r"(?:^|/)0[0-9]+(?:/|$)")

NOT_APPLICABLE = (
    "esta operação não se aplica ao documento: o caminho (`path`, ou `from` em `move` e `copy`) não "
    "existe, ou o índice está fora da lista"
)
TEST_FAILED = "a conferência (`test`) falhou: o documento não tem o valor esperado neste caminho"
INVALID = (
    "a operação não vale: confira `op`, `path`, `value` e `from` (um índice de lista não leva zero "
    "à esquerda)"
)
SYSTEM_PATH = (
    "a versão do texto do consentimento (`/consent/version`) é do sistema: a publicação a sobe "
    "sozinha quando o texto muda, e um patch não a escreve"
)
TOO_MANY_COPIES = f"um patch tem no máximo {MAX_COPIES} operações `copy`"
TOO_BIG = (
    f"o documento passaria de {MAX_DOCUMENT_BYTES // 1024} KiB, o que cabe num `PUT` (o corpo da API "
    "tem 256 KiB, e os acentos escapados no JSON ocupam mais)"
)
TOO_DEEP = "o documento passaria do aninhamento que se consegue processar"


def field_of(position: int) -> str:
    """O campo de um problema da operação `position` do patch (`patch.2`)."""
    return f"{FIELD}.{position}"


def depth_of(value: Any, limit: int = MAX_VALUE_DEPTH) -> int:
    """Quantos níveis de listas e objetos um valor tem (um texto ou um número tem zero; `[]`, um). Para
    ao passar de `limit`, e não recorre: um valor aninhado a milhares de níveis não derruba nada."""
    deepest = 0
    pending: list[tuple[Any, int]] = [(value, 1)]
    while pending:
        item, level = pending.pop()
        if isinstance(item, dict):
            children: Any = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        deepest = max(deepest, level)
        if deepest > limit:
            return deepest
        pending.extend((child, level + 1) for child in children)
    return deepest


def _size(document: Any, field: str) -> int:
    """Os bytes do documento num corpo de `PUT`, no pior caso dos clientes comuns: o `json.dumps`
    padrão do Python (e o `requests`), com os acentos escapados e um espaço depois de cada vírgula e
    de cada dois-pontos. Um aninhamento que o `json` não percorre é o problema de `field`."""
    try:
        return len(json.dumps(document))
    except RecursionError:
        raise DefinitionError([Problem(field, TOO_DEEP)]) from None


def _refused(operation: Mapping[str, Any]) -> str | None:
    """Por que o aplicador recusa a operação antes de a biblioteca vê-la; None se ela passa."""
    op, path, source = operation.get("op"), operation.get("path"), operation.get("from")
    for pointer in (path, source):
        if isinstance(pointer, str) and LEADING_ZERO.search(pointer):
            return INVALID
    if op != "test" and isinstance(path, str) and path in SYSTEM_PATHS:
        return SYSTEM_PATH
    if op == "move" and isinstance(source, str) and source in SYSTEM_PATHS:  # tira a versão
        return SYSTEM_PATH
    return None


def apply(document: Any, operations: Sequence[Mapping[str, Any]]) -> Any:
    """O documento com as `operations` aplicadas, numa cópia. Levanta `DefinitionError` com o
    problema `patch.<n>` da primeira operação que não se aplica, e não devolve nada de um patch pela
    metade. Um patch que troca a raiz (`path` vazio) pode devolver o que não é um objeto: quem guarda
    o documento confere."""
    # Um documento que já passava do teto (um `PUT` com acentos sem escape) não é culpa do patch: ele só
    # não cresce além do que tinha.
    limit = max(MAX_DOCUMENT_BYTES, _size(document, FIELD))
    patched: Any = copy.deepcopy(document)
    copies = 0
    for position, operation in enumerate(operations):
        here = field_of(position)
        if (why := _refused(operation)) is not None:
            raise DefinitionError([Problem(here, why)])
        copying = operation.get("op") == "copy"
        copies += copying
        if copies > MAX_COPIES:
            raise DefinitionError([Problem(here, TOO_MANY_COPIES)])
        try:
            patched = jsonpatch.apply_patch(patched, [dict(operation)], in_place=True)
        except jsonpatch.JsonPatchTestFailed:
            raise DefinitionError([Problem(here, TEST_FAILED)]) from None
        except jsonpatch.InvalidJsonPatch:
            raise DefinitionError([Problem(here, INVALID)]) from None
        except RecursionError:
            raise DefinitionError([Problem(here, TOO_DEEP)]) from None
        except (
            jsonpatch.JsonPatchException,
            jsonpatch.JsonPointerException,  # a da `jsonpointer`, que a `jsonpatch` reexporta
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ):
            raise DefinitionError([Problem(here, NOT_APPLICABLE)]) from None
        if copying and _size(patched, here) > limit:
            raise DefinitionError([Problem(here, TOO_BIG)])
    if _size(patched, FIELD) > limit:
        raise DefinitionError([Problem(FIELD, TOO_BIG)])
    return patched
