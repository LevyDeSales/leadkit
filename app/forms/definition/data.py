"""Os dados do formulário: blocos com nome e tipo declarado (`DataBlockDoc`), conferidos na
compilação e entregues aos templates.

- `value`: um valor JSON livre, que o template lê como está.
- `list`: uma lista de nomes, sem nomes repetidos (iguais, a menos de maiúsculas, acentos e
  espaços). O template a lê como uma lista de textos.
- `windows`: janelas de datas com nome único. Cada uma termina no dia em que começa ou depois, e,
  em sequência (o padrão), começa depois do fim da anterior. O template as lê pelo nome
  (`data.janelas.opcao.start`), com as datas como datas: os filtros `short_date` e `long_date`
  valem nelas. Sem rótulo, a janela mostra o nome.
- `edition`: o nome da edição e, se houver, o início e o fim, que não vêm fora de ordem. O template
  lê `name`, `starts` e `ends` (`None` onde falta).

As datas vão ao banco como texto AAAA-MM-DD; o template as recebe como `date`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.forms.definition.document import (
    DataBlockDoc,
    EditionBlock,
    ListBlock,
    Problem,
    ValueBlock,
    WindowsBlock,
)
from app.forms.definition.plan import comparable

OUT_OF_ORDER = "termina antes de começar"


def data_problems(data: Mapping[str, DataBlockDoc]) -> list[Problem]:
    """O que a estrutura não confere sozinha nos blocos: nomes repetidos e datas fora de ordem."""
    problems: list[Problem] = []
    for name, block in data.items():
        at = f"data.{name}"
        if isinstance(block, ListBlock):
            problems += _list_problems(block, at)
        elif isinstance(block, WindowsBlock):
            problems += _window_problems(block, at)
        elif isinstance(block, EditionBlock) and (
            block.starts and block.ends and block.ends < block.starts
        ):
            problems.append(Problem(f"{at}.ends", OUT_OF_ORDER))
    return problems


def _list_problems(block: ListBlock, at: str) -> list[Problem]:
    seen: set[str] = set()
    problems = []
    for k, item in enumerate(block.items):
        key = comparable(item)
        if key in seen:
            problems.append(Problem(f"{at}.items.{k}", "outro item da lista tem o mesmo nome"))
        seen.add(key)
    return problems


def _window_problems(block: WindowsBlock, at: str) -> list[Problem]:
    problems = []
    names: set[str] = set()
    for k, window in enumerate(block.windows):
        where = f"{at}.windows.{k}"
        if window.name in names:
            problems.append(Problem(f"{where}.name", "outra janela do bloco já usa este nome"))
        names.add(window.name)
        if window.end < window.start:
            problems.append(Problem(f"{where}.end", OUT_OF_ORDER))
        before = block.windows[k - 1] if k else None
        if block.sequential and before is not None and window.start <= before.end:
            problems.append(
                Problem(
                    f"{where}.start",
                    f"precisa começar depois do fim da janela anterior ({before.name})",
                )
            )
    return problems


def template_data(data: Mapping[str, DataBlockDoc]) -> dict[str, Any]:
    """Os blocos como os templates os leem (`data.<nome>`)."""
    return {name: _view(block) for name, block in data.items()}


def _view(block: DataBlockDoc) -> Any:
    if isinstance(block, ValueBlock):
        return block.value
    if isinstance(block, ListBlock):
        return list(block.items)
    if isinstance(block, WindowsBlock):
        return {
            window.name: {
                "name": window.name,
                "label": window.label or window.name,
                "start": window.start,
                "end": window.end,
            }
            for window in block.windows
        }
    return {"name": block.name, "starts": block.starts, "ends": block.ends}
