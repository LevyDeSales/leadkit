"""A comparação de versões de um formulário: a página `/admin/forms/{slug}/versions/compare` e a API
(`compare_form_versions`) escolhem, leem e comparam do mesmo jeito.

Uma comparação tem dois lados, cada um uma versão publicada (o número) ou o rascunho (`DRAFT_REF`).
Sem escolha, a versão atual contra a anterior ou, se há um rascunho pendente, contra o rascunho. A
diferença é por seção, e lê a forma normal das duas definições (`app.forms.definition.changes`): o
padrão do formato escrito por extenso não conta como mudança.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.forms import UnknownForm, drafts
from app.forms import stored as stored_forms
from app.forms.definition import changes
from app.forms.definition.changes import SectionChange

# Na comparação, o rascunho no lugar do número de uma versão.
DRAFT_REF = "rascunho"
MAX_NUMBER_DIGITS = 9  # o banco guarda o número da versão num integer


class NothingToCompare(Exception):
    """O formulário ainda não tem versão publicada: a primeira publicação cria a versão 1."""


class UnknownVersion(Exception):
    """Um dos lados não é o rascunho nem uma versão publicada do formulário."""


@dataclass(frozen=True)
class Comparison:
    """As duas definições comparadas: `old` e `new` são o que foi comparado (o número de uma versão
    em texto, ou `DRAFT_REF`), com a escolha padrão onde a chamada não escolheu."""

    draft: drafts.Draft
    old: str
    new: str
    numbers: tuple[int, ...]  # as versões publicadas, a mais recente primeiro
    sections: tuple[SectionChange, ...]


def version_number(text: str) -> int | None:
    """O número de uma versão num texto (um caminho, uma query); None se não for um."""
    if text.isascii() and text.isdigit() and len(text) <= MAX_NUMBER_DIGITS:
        return int(text)
    return None


def document_of(session: Session, draft: drafts.Draft, ref: str) -> dict[str, Any] | None:
    """A definição de uma versão pelo número, ou a do rascunho; None se não existe."""
    if ref == DRAFT_REF:
        return draft.document
    number = version_number(ref)
    found = stored_forms.stored_version(session, draft.slug, number) if number is not None else None
    return found.definition if found is not None else None


def compare(session: Session, slug: str, *, old: str = "", new: str = "") -> Comparison:
    """Compara `old` com `new`, cada um o número de uma versão ou `DRAFT_REF`. Um lado em branco vai
    para a escolha padrão: `old` é a versão anterior à atual (ou a atual, se há um rascunho
    pendente) e `new` é o rascunho, se há um pendente (`Draft.pending`), ou a versão atual.

    Levanta `UnknownForm`, `NothingToCompare` (sem versão publicada) e `UnknownVersion`."""
    draft = drafts.load(session, slug)
    if draft is None:
        raise UnknownForm(slug)
    if draft.current is None:
        raise NothingToCompare
    current = draft.current.number
    old = old or str(max(1, current - 1) if not draft.pending else current)
    new = new or (DRAFT_REF if draft.pending else str(current))
    before, after = document_of(session, draft, old), document_of(session, draft, new)
    if before is None or after is None:
        raise UnknownVersion
    numbers = tuple(entry.number for entry in stored_forms.history(session, slug))
    return Comparison(draft, old, new, numbers, changes.compare(before, after))
