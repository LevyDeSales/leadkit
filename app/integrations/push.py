"""A sincronização de um pedido concluído com a planilha (a task `sync_integration`) e o envio de
teste.

`push` é um passo do pipeline independente do Hatchet, idempotente por sincronização: o Hatchet
entrega pelo menos uma vez, então ele pode rodar de novo depois de já ter gravado o resultado. Lê
tudo do banco (a task recebe só o id da sincronização), monta a linha com o mapeamento
(`app.integrations.mapping`) e faz o **upsert** pelo código do pedido: acha a linha pelo campo-chave
e a atualiza, ou a cria se não existe. Reenviar, repetir depois de uma falha e reprocessar o pedido
nunca duplicam a linha. Uma trava no Postgres (por integração e código do pedido) serializa o
achar-e-escrever de duas sincronizações do mesmo pedido, em processos diferentes.

Os campos da tabela são lidos de novo a cada sincronização: as opções de seleção e os tipos valem
como estão no Baserow agora, e uma opção que a operadora acrescentou lá salva a sincronização na
próxima tentativa.

O que repete e o que desiste (`RetryableError` e `TerminalError`, com `FailureCode`):
- a planilha fora do ar, lenta ou com limite de uso, a integração desativada e um token que a chave
  atual não abre: repete com espera, e corrigir ou reativar salva a sincronização que ainda está
  repetindo;
- um token recusado, uma tabela que não existe e um mapeamento que não serve (campo que saiu da
  tabela, tipo que mudou, opção sem par): a sincronização falha na hora, e a operadora a reenvia
  pelo admin depois de corrigir.

O motivo da falha fica na sincronização (`last_error`), sem dado da lead: só a etapa, o status, os
códigos do Baserow e os nomes de campos e perguntas, que são configuração.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import pipeline
from app.deps import Deps
from app.destinations import UnreadableSecret
from app.errors import FailureCode, RetryableError, TerminalError
from app.integrations import config, mapping
from app.integrations.baserow import Connection
from app.integrations.mapping import CaptureRowData, Entry, MappingProfile, RowData
from app.models import Integration, IntegrationSync, Job, Submission, utcnow
from app.submission_identity import identity_from_answers, submission_form

SAMPLE_PREFIX = (
    "teste-"  # o código do pedido da linha do envio de teste: teste-<slug do formulário>
)


@dataclass(frozen=True)
class Written:
    """O que o upsert fez: `created` ou `updated`, e o id da linha na planilha."""

    outcome: str
    row_id: int


@dataclass(frozen=True)
class _Loaded:
    connection: Connection
    integration_id: uuid.UUID
    key_field_id: int
    entries: list[Entry]
    data: RowData


def push(deps: Deps, sync_id: uuid.UUID) -> str:
    """Sincroniza um pedido com a planilha. Devolve "succeeded" ou "already_succeeded" (a
    sincronização já estava feita, ou saiu com o pedido pela retenção). Levanta o erro traduzido,
    depois de registrá-lo na sincronização."""
    try:
        loaded = _load(deps, sync_id)
        if loaded is None:
            return "already_succeeded"
        written = upsert(
            deps,
            loaded.connection,
            integration_id=loaded.integration_id,
            key_field_id=loaded.key_field_id,
            entries=loaded.entries,
            data=loaded.data,
        )
    except Exception as exc:
        _record_error(deps, sync_id, exc)
        raise
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(IntegrationSync)
            .where(IntegrationSync.id == sync_id)
            .values(
                status="succeeded",
                outcome=written.outcome,
                row_id=written.row_id,
                synced_at=utcnow(),
                last_error=None,
            )
        )
    return "succeeded"


def _load(deps: Deps, sync_id: uuid.UUID) -> _Loaded | None:
    """O que a sincronização precisa, lido do banco; None se não há o que sincronizar. Conta a tentativa antes de
    qualquer recusa, para o admin mostrar quantas vezes foi tentado."""
    with deps.sessions.begin() as session:
        sync = session.get(IntegrationSync, sync_id)
        if sync is None or sync.status == "succeeded":
            return None
        sync.attempts += 1
        integration = session.get_one(Integration, sync.integration_id)
        data = _row_data(deps, session, sync)
    if not integration.active:
        raise RetryableError(
            FailureCode.INTEGRATION_UNCONFIGURED,
            "a integração está desativada; ative-a para a sincronização seguir",
        )
    entries = mapping.entries_of(integration.mapping)
    if integration.key_field_id is None or mapping.key_entry(entries) is None:
        raise TerminalError(
            FailureCode.INTEGRATION_MAPPING, "o mapeamento está vazio ou sem o código do pedido"
        )
    try:
        connection = config.open_connection(deps.cipher, integration)
    except UnreadableSecret:
        raise RetryableError(
            FailureCode.INTEGRATION_UNCONFIGURED,
            "o token da integração não abre com WEBHOOK_ENCRYPTION_KEY; cole o token de novo",
        ) from None
    return _Loaded(connection, integration.id, integration.key_field_id, entries, data)


def _row_data(deps: Deps, session: Session, sync: IntegrationSync) -> RowData:
    """Os dados do pedido, lidos do banco e prontos para o mapeamento. As perguntas são as da versão
    do formulário que a lead respondeu: os rótulos das opções são os que ela viu."""
    job = session.get_one(Job, sync.job_id)
    submission = session.get_one(Submission, job.submission_id)
    spec = submission_form(deps.forms, submission)
    lead = identity_from_answers(submission, spec)
    # O mapeamento é do pedido/submissão, sem documento ou hash de uma tentativa antiga.
    # O link fixa a tentativa observada; se ficar obsoleto, o endpoint recusa outro PDF.
    links = pipeline.lead_links(deps, job, pdf_attempt=job.attempt)
    return RowData(
        public_id=job.public_id,
        form_version=submission.form_version,
        lead_name=lead.name,
        lead_email=lead.email,
        lead_phone=lead.phone,
        pdf_url=links["pdf_url"],
        page_url=links["page_url"],
        priority=submission.priority,
        persona=submission.persona,
        answers=submission.answers,
        consent=submission.consent,
        tracking=submission.tracking,
        scores=submission.scores,
        questions={question.name: question for question in mapping.questions_of(spec)},
    )


def upsert(
    deps: Deps,
    connection: Connection,
    *,
    integration_id: uuid.UUID,
    key_field_id: int,
    entries: list[Entry],
    data: RowData | CaptureRowData,
    record_key: str | None = None,
    profile: MappingProfile = "pdf",
) -> Written:
    """Monta a linha com os campos da tabela como estão agora e a grava: atualiza a que tem o
    registro, ou cria. Captura exige `record_key` igual ao id da submissão; PDF conserva
    a chave e a trava antigas quando a chave explícita é omitida.
    Levanta `RetryableError` ou `TerminalError`."""
    live = deps.baserow.fields(connection)
    try:
        row = mapping.build_row(entries, live, data, profile=profile)
        if record_key is None:
            if not isinstance(data, RowData) or profile != "pdf":
                raise mapping.MappingError("A captura exige a chave explícita da submissão.")
            record_key = data.public_id
        key_source = mapping.CAPTURE_KEY_SOURCE if profile == "capture" else mapping.KEY_SOURCE
        if not record_key or record_key != mapping.raw_value(key_source, data, profile=profile):
            raise mapping.MappingError("A chave explícita não corresponde ao registro enviado.")
        if profile == "capture":
            mapping.check_entries(
                entries,
                live,
                {
                    **{source.key: source for source in mapping.CAPTURE_SOURCES},
                    **{
                        f"answer:{name}": mapping.answer_source(question)
                        for name, question in data.questions.items()
                    },
                },
                profile=profile,
            )
            key = mapping.key_entry(entries, profile=profile)
            if (
                key is None
                or key.field_id != key_field_id
                or row.get(f"field_{key_field_id}") != record_key
            ):
                raise mapping.MappingError(
                    "O campo-chave não corresponde ao mapeamento da captura."
                )
    except (mapping.MappingError, mapping.InvalidMapping) as exc:
        raise TerminalError(FailureCode.INTEGRATION_MAPPING, str(exc)) from None
    lock = f"integration:{integration_id}:{record_key}"
    # A transação segura a trava até o fim do achar-e-escrever; sem tabela mexida, ela só espera.
    with deps.sessions.begin() as session:
        session.execute(
            sa.text("select pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": lock}
        )
        found = deps.baserow.find_rows(connection, key_field_id, record_key)
        if found:
            return Written("updated", deps.baserow.update_row(connection, found[0], row))
        return Written("created", deps.baserow.create_row(connection, row))


def _record_error(deps: Deps, sync_id: uuid.UUID, exc: Exception) -> None:
    """O motivo da falha fica na sincronização (o admin mostra), sem dados da lead: só códigos."""
    known = isinstance(exc, RetryableError | TerminalError)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(IntegrationSync)
            .where(IntegrationSync.id == sync_id)
            .values(last_error=(str(exc) if known else type(exc).__name__)[:300])
        )


# --- o envio de teste ------------------------------------------------------------------------


@dataclass(frozen=True)
class SampleResult:
    """O que o envio de teste fez: `ok` com o que aconteceu com a linha, ou o motivo da falha."""

    ok: bool
    public_id: str
    outcome: str | None = None
    row_id: int | None = None
    error: str | None = None


def sample_id(form_slug: str) -> str:
    """O código do pedido da linha de teste de um formulário: fixo, então testar de novo atualiza
    a mesma linha em vez de criar outra."""
    return f"{SAMPLE_PREFIX}{form_slug}"


def send_sample(deps: Deps, form_slug: str, *, actor: str) -> SampleResult | None:
    """Manda para a tabela uma linha sintética do formulário (a lead de exemplo, no domínio
    `exemplo`, com respostas inventadas), pelo mesmo caminho de um pedido de verdade, e devolve o
    que aconteceu; None se o formulário não tem integração. A linha fica na tabela, para a operadora
    conferi-la lá. Não passa pelo Hatchet nem toca dado de lead. A auditoria guarda o resultado.
    Levanta `UnreadableSecret`, sem mandar nada, se o token não abre."""
    with deps.sessions() as session:
        stored = config.load(session, form_slug)
        connection = config.stored_connection(session, deps.cipher, form_slug)
    if stored is None or connection is None:
        return None
    public_id = sample_id(form_slug)
    spec = config.form_spec(deps, form_slug)
    written: Written | None = None
    error: str | None = None
    if spec is None:
        error = "o formulário não tem versão publicada para montar a linha de teste"
    elif stored.key_field_id is None or mapping.key_entry(list(stored.entries)) is None:
        error = "o mapeamento está vazio ou sem o código do pedido"
    else:
        try:
            written = upsert(
                deps,
                connection,
                integration_id=stored.id,
                key_field_id=stored.key_field_id,
                entries=list(stored.entries),
                data=mapping.sample_data(spec, public_id),
            )
        except (RetryableError, TerminalError) as exc:
            error = str(exc)[:300]
    result = SampleResult(
        ok=written is not None,
        public_id=public_id,
        outcome=written.outcome if written else None,
        row_id=written.row_id if written else None,
        error=error,
    )
    details: dict[str, Any] = {"ok": result.ok, "public_id": public_id}
    details["outcome" if result.ok else "error"] = result.outcome if result.ok else error
    with deps.sessions.begin() as session:
        config.record_test(session, stored, actor=actor, **details)
    return result
