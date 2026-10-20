"""A consulta da auditoria (`audit_log`), a que a API de admin usa em `list_audit_log`: quem fez o quê, em
que alvo e quando, com os detalhes das chaves permitidas.

Devolve dados, não telas: as entradas como dataclasses, com o instante com fuso e os códigos do banco, e
nenhum rótulo. As leituras não são auditadas (decisão 2 da revisão da ADR 0015): ler a auditoria não
grava nada.

**Os `details` saem filtrados, por lista de permissão, e o filtro é pelo nome da chave.** A tabela é
escrita por vários serviços, cada um com os `details` dele, e o que ela guarda não é todo próprio para
quem tem só a chave `leitura`: o `reason` de um reprocessamento é o texto que a operadora digita, e pode
citar a lead (a retenção o apaga junto com o pedido), e o `error` do teste de uma integração é o texto de
um erro de fora, que o projeto trata como só do `total`. A consulta não confia em quem vai gravar: só
passa a chave que está em `PUBLIC_DETAIL_KEYS`, com um valor simples (um texto, um número, um booleano,
nulo, uma lista disso ou um objeto de um nível disso); o resto sai, sem aviso. **O filtro não olha o
conteúdo:** o valor sob uma chave permitida passa como foi escrito, então a segurança de um valor é de
quem grava e de quem inclui a chave aqui. O que a lista dá é que uma chave nova, de um serviço novo, não
sai até alguém lê-la e decidir (e `test_admin_queries_audit.py` reprova o PR que grava uma chave literal
que ninguém classificou). Hoje nenhum serviço grava segredo, hash nem dado de lead (a auditoria dos
destinos guarda o host e não o caminho da URL; a das chaves, o nome, o papel e a validade); os únicos
textos que não são códigos nem nomes são o `reason` e o `error`, e os dois ficam de fora.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import AuditLog

Scalar = str | int | float | bool | None
DetailValue = Scalar | list[Scalar] | dict[str, Scalar]

# As chaves de `details` que a leitura deixa passar, agrupadas por quem as grava. É uma lista de nomes:
# o valor sob cada um passa como foi escrito. Uma chave nova num serviço só sai pela API depois de entrar
# aqui, e o teste reprova uma chave literal que não esteja aqui nem em `WITHHELD` (no teste). Ao incluí-la,
# confira que o valor nunca é segredo, hash de chave, dado da lead, texto que uma pessoa digita nem texto
# de erro de fora. Ficam de fora de propósito: o `reason` (`app/jobs.py`), que a operadora digita ao
# reprocessar e pode citar a lead, e o `error` (`app/integrations/push.py` e `app/destinations.py`), o
# texto de erro de fora do teste de uma integração e o nome da exceção do teste de um destino.
PUBLIC_DETAIL_KEYS = frozenset(
    {
        # Destinos do webhook (`app/destinations.py`): o host da URL, nunca o caminho, que vale como
        # senha; o `source` é o nome da variável de ambiente de onde o destino padrão veio; no teste,
        # o status e o tempo (o `error` fica de fora).
        # Entregas de captura: referências e finalidade autorizada, sem respostas nem credenciais.
        "submission_id",
        "config_version",
        "purpose",
        "purpose_origin",
        "audience",
        "dispatch_no",
        "events",
        "profile",
        "form_slug",
        "host",
        "previous_host",
        "http_confirmed",
        "source",
        "status",
        "elapsed_ms",
        # Pedidos (`app/jobs.py`, `app/integrations/syncs.py`): a tentativa nova, o estado e o código
        # da falha de antes, e o id do pedido. O motivo livre (`reason`) fica de fora.
        "attempt",
        "previous_status",
        "failure_code",
        "job_id",
        "count",
        # Retenção (`app/retention.py`, `app/forms/retention.py`): contagens e prazos em dias.
        "requests_deleted",
        "leads_deleted",
        "pdfs_deleted",
        "storage_errors",
        "draft_tests_deleted",
        "retention_days",
        "retention_pdf_days",
        "pdf_days",
        "request_days",
        "previous_pdf_days",
        "previous_request_days",
        "in_force_pdf_days",
        "in_force_request_days",
        "confirmed_requests",
        "confirmed_pdfs",
        # Formulários (`app/forms/`): versão, impressão digital do documento (nunca o conteúdo), os
        # papéis de contato confirmados (`name`, `email`... e não os valores), o sha256 das imagens,
        # o endereço de antes e a origem de uma cópia.
        "version",
        "fingerprint",
        "contact_in_prompt",
        "images",
        "tested_at",
        "consent_version",
        "previous_slug",
        "source_version",
        "test",
        "simulated_date",
        "active",
        # Imagens (`app/forms/assets.py`): o nome, o sha256 do conteúdo e os tamanhos.
        "name",
        "sha256",
        "previous_sha256",
        "bytes",
        "source_bytes",
        # Integração com o Baserow (`app/integrations/config.py`, `push.py`): o tipo, a tabela, o que
        # mudou (sem o token) e o resultado do teste, que roda com uma linha de exemplo (o `error` fica
        # de fora).
        "kind",
        "table_id",
        "token_changed",
        "mapping_reset",
        "fields",
        "deactivated",
        "ok",
        "public_id",
        "outcome",
        # Chaves de API (`app/admin/api/keys.py`): o nome, o papel e a validade (o serviço não grava
        # o segredo nem o hash). O `name` está acima, com o das imagens.
        "role",
        "expires_at",
    }
)


def _simple(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _allowed(value: object) -> bool:
    """O valor que a leitura deixa passar: um simples, uma lista de simples ou um objeto de simples."""
    if isinstance(value, list):
        return all(_simple(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _simple(item) for key, item in value.items())
    return _simple(value)


def public_details(details: object) -> dict[str, DetailValue]:
    """Os `details` de uma entrada sem o que a leitura não pode mostrar: só as chaves de
    `PUBLIC_DETAIL_KEYS`, e só com um valor simples (`_allowed`). Uma entrada cujo `details` não é um
    objeto sai com `{}`."""
    if not isinstance(details, Mapping):
        return {}
    return {
        key: value
        for key, value in details.items()
        if key in PUBLIC_DETAIL_KEYS and _allowed(value)
    }


@dataclass(frozen=True)
class AuditEntry:
    """Uma entrada da auditoria.

    - `at`: o instante, com fuso.
    - `actor`: quem fez: o usuário do admin, `chave:<nome>` (uma chave de API), `retention` (a retenção
      automática) ou `ambiente` (o destino padrão que veio do `WEBHOOK_URL`).
    - `action`: o que foi feito, como `job.reprocess` ou `api_key.create`.
    - `target_type` e `target_id`: o alvo (o tipo e o id, como texto); nulos nas ações sem alvo, como
      a retenção.
    - `details`: o que a ação guardou, só as chaves permitidas, com valor simples (`public_details`)."""

    id: uuid.UUID
    at: datetime
    actor: str
    action: str
    target_type: str | None
    target_id: str | None
    details: dict[str, DetailValue]


@dataclass(frozen=True)
class AuditCursor:
    """Onde a leitura por chave parou: o `at` e o `id` da última entrada entregue."""

    at: datetime
    id: uuid.UUID


@dataclass(frozen=True)
class AuditFilter:
    """Os filtros, todos por igualdade e somados (E). Nulo é "qualquer"; um texto, mesmo vazio, é
    exatamente esse valor (quem chama decide se o vazio vale)."""

    target_type: str | None = None
    target_id: str | None = None
    actor: str | None = None
    action: str | None = None


def _conditions(filters: AuditFilter) -> list[sa.ColumnElement[bool]]:
    pairs = (
        (AuditLog.target_type, filters.target_type),
        (AuditLog.target_id, filters.target_id),
        (AuditLog.actor, filters.actor),
        (AuditLog.action, filters.action),
    )
    return [column == value for column, value in pairs if value is not None]


def entries_after(
    session: Session,
    filters: AuditFilter,
    *,
    limit: int,
    after: AuditCursor | None = None,
) -> tuple[list[AuditEntry], bool]:
    """As entradas mais novas primeiro, `limit` delas depois de `after`, e se há mais: a paginação por
    chave (`at`, `id`). O `id` desempata as entradas do mesmo instante, então a ordem é total; uma
    entrada que chega durante a leitura é mais nova que o cursor e fica fora das páginas seguintes, e
    nenhuma é pulada nem repetida (a paginação por número não garante). Pede-se uma a mais que o
    `limit` só para saber se há outra página, e uma lista que acaba certo na página não tem uma vazia
    depois."""
    statement = sa.select(AuditLog).where(*_conditions(filters))
    if after is not None:
        statement = statement.where(sa.tuple_(AuditLog.at, AuditLog.id) < (after.at, after.id))
    found = list(
        session.scalars(statement.order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit + 1))
    )
    entries = [
        AuditEntry(
            id=row.id,
            at=row.at,
            actor=row.actor,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            details=public_details(row.details),
        )
        for row in found[:limit]
    ]
    return entries, len(found) > limit
