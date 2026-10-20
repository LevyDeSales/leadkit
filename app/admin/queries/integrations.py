"""A consulta da integração de um formulário com o Baserow, a que a página (`app.admin.integration_views`)
e a API de admin dividem: a conexão, uma linha por campo da tabela com o que o preenche e as fontes que
servem a ele, o relatório do mapeamento e o estado das sincronizações.

Devolve dados, não telas: códigos (`read_only`, `single_select`, `answer:tamanho_equipe`) e nenhum
rótulo. A página os decora (o nome do tipo em português, as fontes por grupo) e a API os serializa em
JSON. Nada daqui fala com o Baserow: quem lê a tabela de agora (`app.admin.actions.integrations`)
entrega os campos, e esta consulta os junta ao que o banco guarda. O token não passa por aqui; a
consulta só diz se o guardado abre.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.errors import FailureCode
from app.integrations import mapping, syncs
from app.integrations.baserow import BaserowField
from app.integrations.config import StoredIntegration
from app.integrations.mapping import Report, Source

FAILURES_SHOWN = 10  # quantas sincronizações que falharam a consulta lista


@dataclass(frozen=True)
class FieldChoice:
    """Um campo da tabela no mapeamento: o que o preenche agora e as fontes que servem ao tipo dele.

    - `unsupported`: por que a integração não o preenche (`read_only` ou `unsupported_type`), ou None.
      Um campo assim não oferece fonte e não tem escolha.
    - `sources`: as chaves das fontes que servem ao tipo do campo, na ordem do catálogo.
    - `selected`: a chave da fonte escolhida; "" é nada.
    - `suggested`: a escolha vem do nome do campo e ainda não foi salva."""

    id: int
    name: str
    type: str
    primary: bool
    unsupported: str | None
    sources: tuple[str, ...]
    selected: str
    suggested: bool


def field_choices(
    live: Sequence[BaserowField],
    sources: Mapping[str, Source],
    entries: Sequence[mapping.Entry],
    typed: Mapping[int, str] | None = None,
) -> list[FieldChoice]:
    """Uma linha por campo da tabela. A escolha é, em ordem: a que a pessoa acabou de mandar
    (`typed`, numa recusa), a do mapeamento guardado, ou, enquanto nada foi mapeado, a que o nome
    do campo sugere."""
    saved = {entry.field_id: entry.source for entry in entries}
    rows = []
    for field in live:
        kind = mapping.unsupported_kind(field)
        suggested = ""
        if typed is not None:  # o que acabou de mandar vale inteiro: o que não veio é "nada"
            selected = typed.get(field.id, "")
        elif entries:
            selected = saved.get(field.id, "")
        else:
            selected = suggested = mapping.suggest(field, sources)
        rows.append(
            FieldChoice(
                id=field.id,
                name=field.name,
                type=field.type,
                primary=field.primary,
                unsupported=kind,
                sources=tuple(
                    key for key, source in sources.items() if mapping.compatible(source, field.type)
                )
                if kind is None
                else (),
                selected=selected if kind is None else "",
                suggested=bool(suggested),
            )
        )
    return rows


@dataclass(frozen=True)
class IntegrationDetail:
    """A integração de um formulário com tudo o que a leitura dela junta.

    - `published`: o formulário tem versão publicada; sem ela não há perguntas para mapear.
    - `stored`: a conexão (nunca o token); None: o formulário não tem integração.
    - `token_unreadable`: o token guardado não abre com a chave atual.
    - `fields` e `report`: lidos da tabela de agora; None quando não há conexão, a tabela não foi lida
      (`read_error` diz por quê) ou, no relatório, quando nada está mapeado.
    - `sources`: o catálogo de fontes da versão publicada, por chave.
    - `counts` e `failures`: o estado das sincronizações, e as mais recentes que falharam."""

    slug: str
    title: str
    published: bool
    stored: StoredIntegration | None
    token_unreadable: bool
    fields: list[FieldChoice] | None
    read_error: str | None
    report: Report | None
    sources: Mapping[str, Source]
    counts: syncs.SyncCounts
    failures: list[syncs.SyncRow]
    profile: str = "pdf"
    capture_deliveries: tuple[dict, ...] = ()


_CODE = re.compile(r"^\[([a-z_]+)\]")


def failure_code_of(last_error: str | None) -> str | None:
    """O código da falha de uma sincronização (`FailureCode`), tirado do último erro que ela guardou
    (`[codigo] etapa: HTTP 503`, ou só o código quando o erro não ficou). Nulo se não é um código
    conhecido: o resto do texto é livre e fica de fora."""
    if not last_error:
        return None
    found = _CODE.match(last_error)
    code = found.group(1) if found else last_error.strip()
    return code if code in {item.value for item in FailureCode} else None


def sync_overview(
    session: Session, stored: StoredIntegration | None
) -> tuple[syncs.SyncCounts, list[syncs.SyncRow]]:
    """Quantas sincronizações da integração estão em cada estado, e as `FAILURES_SHOWN` mais recentes
    que falharam. Sem integração, nada."""
    if stored is None:
        return syncs.SyncCounts(), []
    return (
        syncs.counts(session, stored.id),
        syncs.recent(session, stored.id, status="failed", limit=FAILURES_SHOWN),
    )
