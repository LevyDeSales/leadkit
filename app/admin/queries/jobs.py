"""A consulta dos pedidos, a que a página (`app.admin.views`) e a API de admin dividem.

Devolve dados, não telas: os códigos do banco (`queued`, `failed`...), instantes com fuso e nenhum
rótulo. A página os decora (rótulo em português, data de Brasília) e a API os serializa em JSON.

**O estado do pedido fica separado do dado da lead.** Cada resultado tem duas partes: o *estado*
(código, situação, etapa, tentativa, formulário e versão, datas, código da falha, entregas,
sincronizações, e o que mais não diz quem é a lead) e o *dado da lead* (nome, e-mail, telefone,
prioridade, persona, pontuação, respostas, consentimento e plano). É a barreira da ADR 0015 entre o
papel `leitura` e a lead: quem lê sem o dado da lead pede `with_lead=False` e a segunda parte nem é
lida do banco. Um campo pessoal que aparecesse no estado furaria a barreira, e o teste desta consulta
e o de contrato da API a cobram.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from dataclasses import InitVar, dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.adapters.storage import ArtifactStorage
from app.admin.localtime import local_midnight
from app.errors import TerminalError
from app.forms import FormRegistry, LeadIdentity
from app.forms.definition.document import SLUG_PATTERN
from app.integrations import syncs as integration_syncs
from app.models import (
    JOB_STATUSES,
    Artifact,
    AuditLog,
    Delivery,
    Form,
    FormVersion,
    Integration,
    IntegrationSync,
    Job,
    Plan,
    Submission,
    WebhookDestination,
)
from app.pdf_availability import PdfAvailability, current_pdf
from app.plan_release import release_snapshot
from app.submission_identity import resolve_identity


def _identity_field(role: str) -> sa.ColumnElement[str]:
    """A busca lê a resposta designada pela versão exata, sem consultar o cadastro compartilhado."""
    key = (
        sa.select(FormVersion.definition["identity"][role].astext)
        .join(Form, Form.id == FormVersion.form_id)
        .where(
            Form.slug == Submission.form_slug, FormVersion.number == Submission.form_version_number
        )
        .correlate(Submission)
        .scalar_subquery()
    )
    return Submission.answers.op("->>", return_type=sa.Text)(key)


# --- o filtro -----------------------------------------------------------------------------------

Q_MAX_LENGTH = 200  # o que cabe na busca; a página corta o que passa, a API recusa
FORM_MAX_LENGTH = 64  # o tamanho da coluna `submissions.form_slug`
FORM_PATTERN = SLUG_PATTERN  # a forma do endereço de um formulário
FORM_VERSION_MAX_LENGTH = 64  # o tamanho da coluna `submissions.form_version`
# O `period` da página (a URL) e quantos dias de calendário de Brasília ele recua de hoje: o primeiro
# dia conta hoje inteiro, então "7d" é hoje mais os seis dias anteriores, "30d", hoje mais 29.
PERIOD_DAYS = {"today": 0, "7d": 6, "30d": 29}
# O `delivery` da página (a URL) e da API: a situação de entrega que a lista filtra. A que deu certo
# (`succeeded`) não é filtro: a operadora procura o que não chegou.
DELIVERY_FILTERS = ("failed", "pending")
# O `sync` da página (a URL) e da API: a situação da sincronização com a planilha que a lista filtra.
# As mesmas duas do `delivery`: a que deu certo (`succeeded`) não é filtro, a operadora procura as
# linhas que faltam na planilha.
SYNC_FILTERS = ("failed", "pending")


@dataclass(frozen=True)
class JobFilters:
    """O que a operadora escolheu na lista de pedidos: a busca (`q`), o `status`, o formulário
    (`form`, o endereço dele) e a versão dele (`form_version`), quando o pedido chegou (`period` na
    página, `created_from` e `created_to` na API), a situação da entrega (`delivery`) e a da
    sincronização com a planilha (`sync`). **É o único lugar dos filtros:** a página e a API leem este
    objeto, então um filtro novo entra aqui, com a condição, o "há filtro?" e o endereço da página.

    Um valor que não tem a forma do parâmetro (um `status` que não é de um pedido, um `form` que não
    é um endereço, um `period`, um `delivery` ou um `sync` desconhecido) vira "sem filtro" já na
    construção, e `active` e `url` não o contam. Um `form` bem formado que nenhum formulário tem é
    filtro: a lista sai vazia, sem erro. A API recusa o que a página ignora (`422`) antes de chegar
    aqui.

    **A versão é o texto de `Submission.form_version`**, o que a coluna "Formulário" mostra e o webhook
    leva (`<endereço>-v<número>`; nos pedidos feitos antes da migration `0008`, o que o código gravava,
    como `mcc-2026.10`), e não o número. Vale dentro de um formulário: sem `form` (ou com mais de 64
    caracteres, a coluna) ela é ignorada, porque a página só oferece a versão de um formulário
    escolhido e um filtro escondido, de outro formulário, deixaria a lista vazia sem explicação. Uma
    versão bem formada que o formulário não tem, ou que é de outro formulário, é filtro: lista vazia,
    sem erro.

    **A chegada é o intervalo `[created_from, created_to)` sobre `Job.created_at`** (instantes com
    fuso; qualquer ponta pode faltar). A API passa os limites; a página passa o `period`, e o
    `now` (o instante da leitura, que o teste fixa) o converte em `created_from`: a meia-noite de
    Brasília do primeiro dia do período, até agora (sem `created_to`: nenhum pedido chega depois de
    agora). Um `period` conhecido sem `now`, ou junto com os limites, é erro de quem chama
    (`ValueError`). `url` escreve só o `period`: os limites da API não têm endereço de página.

    **A entrega é a da tentativa atual do pedido**, a mesma que a coluna "Entregas e planilha"
    mostra: o pedido casa com `failed` (ou `pending`) se o PDF da tentativa atual tem uma entrega
    nesse estado. Uma entrega de uma tentativa antiga, já reprocessada, não conta; o pedido sem
    entrega (na fila, ou de um formulário sem destino) não casa com nenhuma das duas.

    **A sincronização é a da tentativa atual**, pela mesma definição (`_current_attempt`): o pedido
    casa com `failed` (ou `pending`) se o PDF da tentativa atual tem uma sincronização com a
    planilha nesse estado. Uma falha de uma tentativa antiga, já refeita, não conta; o pedido sem
    integração (ou que ainda não concluiu) não casa com nenhuma das duas.
    """

    q: str = ""
    status: str = ""
    form: str = ""
    form_version: str = ""
    period: str = ""
    delivery: str = ""
    sync: str = ""
    created_from: datetime | None = None
    created_to: datetime | None = None
    now: InitVar[datetime | None] = None

    def __post_init__(self, now: datetime | None) -> None:
        # Congelada: a normalização grava por `object.__setattr__`.
        object.__setattr__(self, "q", self.q.strip()[:Q_MAX_LENGTH])
        if self.status not in JOB_STATUSES:
            object.__setattr__(self, "status", "")
        if len(self.form) > FORM_MAX_LENGTH or not re.fullmatch(FORM_PATTERN, self.form):
            object.__setattr__(self, "form", "")
        if not self.form or len(self.form_version) > FORM_VERSION_MAX_LENGTH:
            object.__setattr__(self, "form_version", "")
        if self.period not in PERIOD_DAYS:
            object.__setattr__(self, "period", "")
        if self.delivery not in DELIVERY_FILTERS:
            object.__setattr__(self, "delivery", "")
        if self.sync not in SYNC_FILTERS:
            object.__setattr__(self, "sync", "")
        if self.period:
            if now is None:
                raise ValueError("period needs `now`, the instant it is counted from")
            if self.created_from is not None or self.created_to is not None:
                raise ValueError("period and created_from/created_to are two ways to say one thing")
            object.__setattr__(
                self, "created_from", local_midnight(now, days_back=PERIOD_DAYS[self.period])
            )

    @property
    def active(self) -> bool:
        """Se algum filtro vale: a página diz "com estes filtros" e oferece "Limpar filtros"."""
        return bool(
            self.q
            or self.status
            or self.form
            or self.form_version
            or self.period
            or self.delivery
            or self.sync
            or self.created_from is not None
            or self.created_to is not None
        )

    def conditions(self, *, search_lead: bool = True) -> list[sa.ColumnElement[bool]]:
        """As condições da lista, as mesmas na página e na API, todas juntas (E). `q` acha o código do
        pedido (exato) e, com `search_lead`, também parte do nome ou do e-mail da lead (sem diferenciar
        maiúsculas; os curingas do LIKE contam como texto). Buscar a lead pelo nome ou pelo e-mail
        serviria para sondar quem é lead: quem não pode ver o dado da lead busca só pelo código.
        `form` é o formulário do pedido: igualdade com `Submission.form_slug`; `form_version`, a versão
        dele: igualdade com `Submission.form_version`. A chegada é
        `created_from` (inclusive) e `created_to` (exclusive) sobre `Job.created_at`: com o fim antes
        do começo, nada passa. `delivery` pede uma entrega, e `sync` uma sincronização com a planilha,
        nesse estado no PDF da tentativa atual (`_current_attempt`, a mesma definição da coluna
        "Entregas e planilha")."""
        found: list[sa.ColumnElement[bool]] = []
        if self.q:
            by_code = Job.public_id == self.q
            found.append(
                sa.or_(
                    _identity_field("name").icontains(self.q, autoescape=True),
                    _identity_field("email").icontains(self.q, autoescape=True),
                    by_code,
                )
                if search_lead
                else by_code
            )
        if self.status:
            found.append(Job.status == self.status)
        if self.form:
            found.append(Submission.form_slug == self.form)
        if self.form_version:
            found.append(Submission.form_version == self.form_version)
        if self.created_from is not None:
            found.append(Job.created_at >= self.created_from)
        if self.created_to is not None:
            found.append(Job.created_at < self.created_to)
        if self.delivery:
            found.append(
                sa.exists(
                    sa.select(Delivery.id)
                    .join(Artifact, Artifact.id == Delivery.artifact_id)
                    .where(
                        Delivery.job_id == Job.id,
                        Delivery.status == self.delivery,
                        _current_attempt(),
                    )
                )
            )
        if self.sync:
            found.append(
                sa.exists(
                    sa.select(IntegrationSync.id)
                    .join(Artifact, Artifact.id == IntegrationSync.artifact_id)
                    .where(
                        IntegrationSync.job_id == Job.id,
                        IntegrationSync.status == self.sync,
                        _current_attempt(),
                    )
                )
            )
        return found

    def url(self, page: int = 1) -> str:
        """O endereço da página da lista com estes filtros, sem parâmetro vazio nem `page=1`: sem
        filtro é `/admin`."""
        params = {
            "q": self.q,
            "form": self.form,
            "form_version": self.form_version,
            "period": self.period,
            "status": self.status,
            "delivery": self.delivery,
            "sync": self.sync,
            "page": str(page) if page > 1 else "",
        }
        query = urlencode({name: value for name, value in params.items() if value})
        return f"/admin?{query}" if query else "/admin"


@dataclass(frozen=True)
class FormChoice:
    """Um formulário que o filtro da lista oferece: o endereço, o título e se recebe envios."""

    slug: str
    title: str
    active: bool


def form_choices(session: Session) -> list[FormChoice]:
    """Todo formulário do registro, ativo ou não, por título (e endereço, para desempatar). Não
    compila nenhum: lê só a tabela `forms`."""
    statement = sa.select(Form.slug, Form.title, Form.active).order_by(Form.title, Form.slug)
    return [FormChoice(slug, title, active) for slug, title, active in session.execute(statement)]


def form_versions(session: Session, form: str) -> list[str]:
    """As versões (o texto de `form_version`) com que os pedidos do formulário `form` foram feitos, da
    mais nova para a mais antiga: a de maior número no registro primeiro e, no mesmo número, a que
    chegou por último (o texto legado e `<endereço>-v1` são os dois da versão 1; o legado é o mais
    velho). Um formulário sem pedidos, ou que não existe, dá a lista vazia. Lê só os envios dele: o
    `DISTINCT` roda sobre os pedidos de um formulário, que a restrição de unicidade já estreita."""
    statement = (
        sa.select(Submission.form_version)
        .where(Submission.form_slug == form)
        .group_by(Submission.form_version)
        .order_by(
            sa.func.max(Submission.form_version_number).desc(),
            sa.func.max(Submission.created_at).desc(),
            Submission.form_version.desc(),
        )
    )
    return list(session.scalars(statement))


# --- a lista ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryBrief:
    """A entrega do PDF da tentativa atual: para onde (`webhook`) e em que pé."""

    destination: str
    status: str  # pending | succeeded | failed


@dataclass(frozen=True)
class SyncBrief:
    """A sincronização do PDF da tentativa atual com a planilha: com que integração (o tipo dela,
    hoje `baserow`) e em que pé. Sem o texto do último erro, que é livre e fica nas `Note`."""

    integration: str
    status: str  # pending | succeeded | failed


@dataclass(frozen=True)
class JobSummary:
    """O estado de um pedido na lista. Nada que diga quem é a lead."""

    id: uuid.UUID
    public_id: str
    status: str  # queued | running | completed | failed
    stage: str  # queued | generating | rendering | completed | failed
    attempt: int
    form_slug: str
    form_version: str
    failure_code: str | None
    created_at: datetime
    deliveries: tuple[DeliveryBrief, ...]
    syncs: tuple[SyncBrief, ...]


@dataclass(frozen=True)
class SummaryLead:
    """O dado da lead na lista: dado pessoal, que só sai para quem pode vê-lo."""

    name: str | None
    email: str | None
    priority: str | None


@dataclass(frozen=True)
class JobRow:
    """Uma linha da lista: o estado e, só se pedido, o dado da lead."""

    state: JobSummary
    lead: SummaryLead | None


@dataclass(frozen=True)
class JobCursor:
    """Onde a leitura por chave parou: o `created_at` e o `id` do último pedido entregue."""

    created_at: datetime
    id: uuid.UUID


def _select(
    conditions: list[sa.ColumnElement[bool]], with_lead: bool
) -> sa.Select[tuple[object, ...]]:
    columns = [
        Job.id,
        Job.public_id,
        Job.status,
        Job.stage,
        Job.attempt,
        Job.failure_code,
        Job.created_at,
        Submission.form_slug,
        Submission.form_version,
    ]
    if with_lead:
        columns += [Submission]
    return (
        sa.select(*columns).join(Submission, Submission.id == Job.submission_id).where(*conditions)
    )


def count_jobs(session: Session, conditions: list[sa.ColumnElement[bool]]) -> int:
    statement = (
        sa.select(sa.func.count())
        .select_from(Job)
        .join(Submission, Submission.id == Job.submission_id)
        .where(*conditions)
    )
    return session.scalar(statement) or 0


def _rows(
    session: Session, found: list[sa.Row[tuple[object, ...]]], with_lead: bool, forms: FormRegistry
) -> list[JobRow]:
    job_ids = [row.id for row in found]
    deliveries = _current_deliveries(session, job_ids)
    syncs = _current_syncs(session, job_ids)
    return [
        JobRow(
            state=JobSummary(
                id=row.id,
                public_id=row.public_id,
                status=row.status,
                stage=row.stage,
                attempt=row.attempt,
                form_slug=row.form_slug,
                form_version=row.form_version,
                failure_code=row.failure_code,
                created_at=row.created_at,
                deliveries=tuple(deliveries[row.id]),
                syncs=tuple(syncs[row.id]),
            ),
            lead=_summary_lead(forms, row.Submission) if with_lead else None,
        )
        for row in found
    ]


def _display_identity(forms: FormRegistry, submission: Submission) -> LeadIdentity | None:
    """A apresentação preserva o pedido mesmo sem contato histórico confiável; nunca presume outro."""
    try:
        return resolve_identity(forms, submission)
    except TerminalError:
        return None


def _summary_lead(forms: FormRegistry, submission: Submission) -> SummaryLead:
    identity = _display_identity(forms, submission)
    return SummaryLead(
        name=identity.name if identity else None,
        email=identity.email if identity else None,
        priority=submission.priority,
    )


def jobs_by_page(
    session: Session,
    conditions: list[sa.ColumnElement[bool]],
    *,
    forms: FormRegistry,
    limit: int,
    offset: int,
    with_lead: bool,
) -> list[JobRow]:
    """Os mais recentes primeiro, `limit` de cada vez a partir de `offset`: a paginação por número
    da página do admin. `with_lead` (sem valor padrão: quem chama decide) lê também o dado da lead."""
    found = session.execute(
        _select(conditions, with_lead)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return _rows(session, list(found), with_lead, forms)


def jobs_after(
    session: Session,
    conditions: list[sa.ColumnElement[bool]],
    *,
    forms: FormRegistry,
    limit: int,
    after: JobCursor | None = None,
    with_lead: bool,
) -> tuple[list[JobRow], bool]:
    """Os mais recentes primeiro, `limit` pedidos depois de `after`, e se há mais: a paginação por
    chave (`created_at`, `id`) da API. Um pedido que entra durante a leitura é mais novo que o
    cursor e fica fora das páginas seguintes: nenhuma linha é pulada nem repetida, o que a
    paginação por número não garante."""
    statement = _select(conditions, with_lead)
    if after is not None:
        statement = statement.where(
            sa.tuple_(Job.created_at, Job.id) < (after.created_at, after.id)
        )
    found = session.execute(
        statement.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit + 1)
    ).all()
    return _rows(session, list(found[:limit]), with_lead, forms), len(found) > limit


def _current_attempt() -> sa.ColumnElement[bool]:
    """O PDF de que a entrega, ou a sincronização, é pertence à tentativa atual do pedido. **A
    definição de "atual"**, uma só: a coluna "Entregas e planilha" da lista (`_current_deliveries` e
    `_current_syncs`) e os filtros `delivery` e `sync` (`JobFilters.conditions`) a usam, para a
    linha que o filtro acha mostrar a entrega, ou a sincronização, que casou."""
    return Artifact.version == Job.attempt


def _current_deliveries(
    session: Session, job_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[DeliveryBrief]]:
    found: dict[uuid.UUID, list[DeliveryBrief]] = defaultdict(list)
    if not job_ids:
        return found
    statement = (
        sa.select(Delivery.job_id, Delivery.destination, Delivery.status)
        .join(Artifact, Artifact.id == Delivery.artifact_id)
        .join(Job, Job.id == Delivery.job_id)
        .where(Delivery.job_id.in_(job_ids), _current_attempt())
        .order_by(Delivery.created_at)
    )
    for job_id, destination, status in session.execute(statement):
        found[job_id].append(DeliveryBrief(destination=destination, status=status))
    return found


def _current_syncs(session: Session, job_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[SyncBrief]]:
    """As sincronizações do PDF da tentativa atual de cada pedido, lidas todas em uma consulta (o
    par de `_current_deliveries`). Só o tipo da integração e o estado: nada da linha da planilha, do
    endereço dela nem do texto do erro."""
    found: dict[uuid.UUID, list[SyncBrief]] = defaultdict(list)
    if not job_ids:
        return found
    statement = (
        sa.select(IntegrationSync.job_id, Integration.kind, IntegrationSync.status)
        .join(Artifact, Artifact.id == IntegrationSync.artifact_id)
        .join(Job, Job.id == IntegrationSync.job_id)
        .join(Integration, Integration.id == IntegrationSync.integration_id)
        .where(IntegrationSync.job_id.in_(job_ids), _current_attempt())
        .order_by(IntegrationSync.created_at)
    )
    for job_id, kind, status in session.execute(statement):
        found[job_id].append(SyncBrief(integration=kind, status=status))
    return found


# --- o detalhe ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfState:
    """O PDF de uma tentativa. `purged_at`: a retenção apagou o arquivo (a linha fica)."""

    version: int  # a tentativa
    byte_size: int
    created_at: datetime
    purged_at: datetime | None


@dataclass(frozen=True)
class DeliveryState:
    """A entrega do PDF de uma tentativa ao destino externo (hoje, o webhook). O texto do último erro
    é livre e vai nas `Note` do dado da lead, não aqui."""

    id: uuid.UUID
    destination: str
    status: str  # pending | succeeded | failed
    attempts: int
    job_attempt: int  # a tentativa do pedido a que o PDF pertence
    delivered_at: datetime | None
    hatchet_run_id: str | None
    scope: str | None  # form | default: qual destino do webhook o último envio usou
    host: str | None  # o host dele (nunca o caminho da URL, que vale como senha)

    audience: str = "internal"
    authorization_origin: str = "legacy"
    config_version: int | None = None
    current_config_version: int | None = None
    dispatch_no: int = 1


@dataclass(frozen=True)
class SyncState:
    """A sincronização do PDF de uma tentativa com a tabela de uma integração (o Baserow). O texto do
    último erro é livre e vai nas `Note`."""

    id: uuid.UUID
    integration: str  # o tipo da integração: hoje, `baserow`
    host: str
    table_id: int
    status: str  # pending | succeeded | failed
    attempts: int
    job_attempt: int
    outcome: str | None  # created | updated: o que ela fez com a linha
    row_id: int | None
    synced_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class TimelineEvent:
    """Um acontecimento de uma tentativa: o que foi (`type`), quando e quem pediu, sem texto livre.
    Os campos além de `type` e `at` só valem para alguns tipos:

    - `received`, `completed`, `queued`: nada além de `at`;
    - `reprocess_requested`, `delivery_resend_requested`: `actor`, quem pediu;
    - `plan_generated`: `model` e `prompt_version`;
    - `pdf_generated`: `byte_size`;
    - `failed`: `failure_code`; `processing`: `stage`;
    - `delivery_succeeded`, `delivery_failed`, `delivery_pending`: `delivery_id`.
    """

    type: str
    at: datetime | None = None
    actor: str | None = None
    failure_code: str | None = None
    stage: str | None = None
    delivery_id: uuid.UUID | None = None
    model: str | None = None
    prompt_version: str | None = None
    byte_size: int | None = None


@dataclass(frozen=True)
class AttemptState:
    """Uma tentativa do pedido e como terminou (`outcome`: o estado dela; nulo se a tentativa antiga
    não deixou registro do fim)."""

    number: int
    outcome: str | None
    events: tuple[TimelineEvent, ...]


@dataclass(frozen=True)
class JobState:
    """O estado de um pedido: tudo o que se pode ler dele sem saber quem é a lead."""

    id: uuid.UUID
    public_id: str
    status: str  # queued | running | completed | failed
    stage: str  # queued | generating | rendering | completed | failed
    attempt: int
    failure_code: str | None
    form_slug: str
    form_version: str  # o texto da versão, o que o webhook leva
    form_version_number: int  # o número dela no registro dos formulários
    created_at: datetime
    completed_at: datetime | None
    link_expires_at: datetime
    link_expired: bool  # o link da lead já passou do prazo, no instante pedido
    hatchet_run_id: str | None
    current_pdf: PdfAvailability
    release: dict[str, Any]
    pdfs: tuple[PdfState, ...]  # o mais novo primeiro
    deliveries: tuple[DeliveryState, ...]  # do mais antigo ao mais novo
    syncs: tuple[SyncState, ...]  # o mais novo primeiro
    timeline: tuple[AttemptState, ...]  # da tentativa 1 à atual


@dataclass(frozen=True)
class Consent:
    privacy_at: datetime | None
    text_version: str | None
    marketing_email: bool
    marketing_whatsapp: bool


@dataclass(frozen=True)
class PlanData:
    """O plano da tentativa mais recente."""

    version: int
    document: dict[str, Any]
    model: str
    prompt_version: str
    created_at: datetime


# De onde vem o texto livre de uma `Note`.
NOTE_FAILURE_DETAIL = "failure_detail"  # o detalhe da falha do pedido (tentativa atual)
NOTE_REPROCESS_REASON = "reprocess_reason"  # o motivo que a operadora deu ao reprocessar
NOTE_DELIVERY_ERROR = "delivery_error"  # o último erro de uma entrega
NOTE_SYNC_ERROR = "sync_error"  # o último erro de uma sincronização


@dataclass(frozen=True)
class Note:
    """O texto livre de um motivo ou de um erro. Os adaptadores só escrevem códigos e status nos
    erros, mas o motivo de um reprocessamento é o que a operadora digitou, e pode citar a lead: por
    isso o texto vai com o dado da lead, e o estado só diz o que aconteceu. `attempt` é a tentativa
    do pedido; `ref` é a entrega ou a sincronização de que o texto é (nulo nos outros)."""

    kind: str
    attempt: int
    ref: uuid.UUID | None
    text: str


@dataclass(frozen=True)
class LeadData:
    """O dado da lead de um pedido: contato, prioridade e pontuação, respostas, consentimento, o plano
    e os textos livres dos motivos."""

    name: str | None
    email: str | None
    phone: str | None
    persona: str | None
    priority: str | None
    scores: dict[str, Any]
    answers: dict[str, Any]
    consent: Consent
    plan: PlanData | None
    notes: tuple[Note, ...]


@dataclass(frozen=True)
class JobDetail:
    """Um pedido: o estado e, só se pedido (`with_lead`), o dado da lead."""

    state: JobState
    lead: LeadData | None


def run_url(template: str | None, run_id: str | None) -> str | None:
    """Link para a execução no painel do Hatchet; None se não há template ou run."""
    if not template or not run_id:
        return None
    return template.replace("{run_id}", quote(run_id, safe=""))


def read_job(
    session: Session,
    public_id: str,
    *,
    forms: FormRegistry,
    now: datetime,
    with_lead: bool,
    storage: ArtifactStorage | None = None,
) -> JobDetail | None:
    """O pedido de código `public_id`, ou nada. `now` é o instante contra o qual se diz que o link da
    lead venceu. Com `with_lead=False` o dado da lead nem é lido do banco; o parâmetro não tem valor
    padrão: quem chama decide se a leitura leva a lead."""
    job = session.scalar(sa.select(Job).where(Job.public_id == public_id))
    if job is None:
        return None
    form = session.execute(
        sa.select(
            Submission.form_slug, Submission.form_version, Submission.form_version_number
        ).where(Submission.id == job.submission_id)
    ).one()
    # Do plano, só o que o estado conta (a linha do tempo): o documento é da lead.
    plans = {
        plan.version: plan
        for plan in session.execute(
            sa.select(Plan.version, Plan.created_at, Plan.model, Plan.prompt_version).where(
                Plan.job_id == job.id
            )
        )
    }
    artifacts = {
        artifact.version: artifact
        for artifact in session.scalars(
            sa.select(Artifact).where(Artifact.job_id == job.id, Artifact.kind == "pdf")
        )
    }
    delivery_rows = list(
        session.scalars(
            sa.select(Delivery).where(Delivery.job_id == job.id).order_by(Delivery.created_at)
        )
    )
    audits = list(
        session.scalars(
            sa.select(AuditLog)
            .where(AuditLog.target_id.in_([str(job.id), *(str(d.id) for d in delivery_rows)]))
            .order_by(AuditLog.at)
        )
    )
    sync_rows = integration_syncs.for_job(session, job.id)
    version_of = {artifact.id: artifact.version for artifact in artifacts.values()}
    deliveries = [
        DeliveryState(
            id=d.id,
            destination=d.destination,
            status=d.status,
            attempts=d.attempts,
            job_attempt=version_of.get(d.artifact_id, 0),
            delivered_at=d.delivered_at,
            hatchet_run_id=d.hatchet_run_id,
            audience=d.audience,
            authorization_origin=d.authorization_origin,
            config_version=d.config_version,
            current_config_version=session.scalar(
                sa.select(WebhookDestination.version).where(
                    WebhookDestination.id == d.webhook_destination_id
                )
            )
            if d.webhook_destination_id
            else None,
            dispatch_no=d.dispatch_no,
            scope=d.destination_scope,
            host=d.destination_host,
        )
        for d in delivery_rows
    ]
    state = JobState(
        id=job.id,
        public_id=job.public_id,
        status=job.status,
        stage=job.stage,
        attempt=job.attempt,
        failure_code=job.failure_code,
        form_slug=form.form_slug,
        form_version=form.form_version,
        form_version_number=form.form_version_number,
        created_at=job.created_at,
        completed_at=job.completed_at,
        link_expires_at=job.expires_at,
        link_expired=now > job.expires_at,
        hatchet_run_id=job.hatchet_run_id,
        current_pdf=current_pdf(session, storage, job),
        release=release_snapshot(session, job),
        pdfs=tuple(
            PdfState(a.version, a.byte_size, a.created_at, a.purged_at)
            for a in sorted(artifacts.values(), key=lambda a: a.version, reverse=True)
        ),
        deliveries=tuple(deliveries),
        syncs=tuple(
            SyncState(
                id=row.id,
                integration=row.kind,
                host=row.host,
                table_id=row.table_id,
                status=row.status,
                attempts=row.attempts,
                job_attempt=row.job_attempt,
                outcome=row.outcome,
                row_id=row.row_id,
                synced_at=row.synced_at,
                created_at=row.created_at,
            )
            for row in sync_rows
        ),
        timeline=tuple(_timeline(job, plans, artifacts, deliveries, audits)),
    )
    if not with_lead:
        return JobDetail(state, None)
    notes = _notes(job, audits, list(zip(delivery_rows, deliveries, strict=True)), sync_rows)
    return JobDetail(state, _lead_data(session, job, notes, forms))


def _lead_data(session: Session, job: Job, notes: list[Note], forms: FormRegistry) -> LeadData:
    submission = session.get_one(Submission, job.submission_id)
    lead = _display_identity(forms, submission)
    plan = session.scalar(
        sa.select(Plan).where(Plan.job_id == job.id).order_by(Plan.version.desc()).limit(1)
    )
    return LeadData(
        name=lead.name if lead else None,
        email=lead.email if lead else None,
        phone=lead.phone if lead else None,
        persona=submission.persona,
        priority=submission.priority,
        scores=submission.scores,
        answers=submission.answers,
        consent=_consent(submission.consent),
        plan=PlanData(plan.version, plan.document, plan.model, plan.prompt_version, plan.created_at)
        if plan is not None
        else None,
        notes=tuple(notes),
    )


def _consent(raw: dict[str, Any]) -> Consent:
    accepted = raw.get("privacy_at")
    return Consent(
        privacy_at=datetime.fromisoformat(accepted) if isinstance(accepted, str) else None,
        text_version=raw.get("text_version"),
        marketing_email=bool(raw.get("marketing_email")),
        marketing_whatsapp=bool(raw.get("marketing_whatsapp")),
    )


def _notes(
    job: Job,
    audits: list[AuditLog],
    deliveries: list[tuple[Delivery, DeliveryState]],
    syncs: list[integration_syncs.SyncRow],
) -> list[Note]:
    """Os textos livres do pedido, cada um com a tentativa e, se for de uma entrega ou de uma
    sincronização, o id dela."""
    notes = []
    for audit in audits:
        attempt, reason = audit.details.get("attempt"), audit.details.get("reason")
        if audit.action == "job.reprocess" and isinstance(attempt, int) and reason:
            notes.append(Note(NOTE_REPROCESS_REASON, attempt, None, reason))
    if job.status == "failed" and job.failure_detail:
        notes.append(Note(NOTE_FAILURE_DETAIL, job.attempt, None, job.failure_detail))
    for row, state in deliveries:
        if row.last_error:
            notes.append(Note(NOTE_DELIVERY_ERROR, state.job_attempt, row.id, row.last_error))
    for sync in syncs:
        if sync.last_error:
            notes.append(Note(NOTE_SYNC_ERROR, sync.job_attempt, sync.id, sync.last_error))
    return notes


def _timeline(
    job: Job,
    plans: dict[int, Any],
    artifacts: dict[int, Artifact],
    deliveries: list[DeliveryState],
    audits: list[AuditLog],
) -> list[AttemptState]:
    """Os acontecimentos de cada tentativa, montados do que ficou gravado: o job só guarda o estado
    da tentativa atual; o fim das anteriores vem da auditoria do reprocessamento."""
    reprocessed = {a.details.get("attempt"): a for a in audits if a.action == "job.reprocess"}
    resent: dict[str, list[AuditLog]] = defaultdict(list)
    for audit in audits:
        if audit.action == "delivery.resend" and audit.target_id:
            resent[audit.target_id].append(audit)

    attempts: list[AttemptState] = []
    for number in range(1, job.attempt + 1):
        events: list[TimelineEvent] = []
        start = reprocessed.get(number)
        if number == 1:
            events.append(TimelineEvent("received", at=job.created_at))
        elif start is not None:
            events.append(TimelineEvent("reprocess_requested", at=start.at, actor=start.actor))
        if (plan := plans.get(number)) is not None:
            events.append(
                TimelineEvent(
                    "plan_generated",
                    at=plan.created_at,
                    model=plan.model,
                    prompt_version=plan.prompt_version,
                )
            )
        if (artifact := artifacts.get(number)) is not None:
            events.append(
                TimelineEvent("pdf_generated", at=artifact.created_at, byte_size=artifact.byte_size)
            )

        if number == job.attempt:
            status: str | None = job.status
            at = job.completed_at if status == "completed" else job.updated_at
            end = _end_event(status, job.failure_code, at, job.stage)
        else:  # a auditoria do reprocessamento seguinte guardou como esta terminou
            details = reprocessed[number + 1].details if number + 1 in reprocessed else {}
            status = details.get("previous_status")
            end = _end_event(status, details.get("failure_code"))
        if end is not None:
            events.append(end)

        for delivery in (d for d in deliveries if d.job_attempt == number):
            for audit in resent[str(delivery.id)]:
                events.append(
                    TimelineEvent(
                        "delivery_resend_requested",
                        at=audit.at,
                        actor=audit.actor,
                        delivery_id=delivery.id,
                    )
                )
            events.append(_delivery_event(delivery))
        outcome = status if status in JOB_STATUSES else None
        attempts.append(AttemptState(number=number, outcome=outcome, events=tuple(events)))
    return attempts


def _end_event(
    status: str | None,
    failure_code: str | None,
    at: datetime | None = None,
    stage: str | None = None,
) -> TimelineEvent | None:
    """Como a tentativa terminou (ou em que pé está); nada se não ficou registrado."""
    if status == "completed":
        return TimelineEvent("completed", at=at)
    if status == "failed":
        return TimelineEvent("failed", at=at, failure_code=failure_code)
    if status == "running":
        return TimelineEvent("processing", stage=stage)
    if status == "queued":
        return TimelineEvent("queued")
    return None


def _delivery_event(delivery: DeliveryState) -> TimelineEvent:
    kind = {"succeeded": "delivery_succeeded", "failed": "delivery_failed"}.get(
        delivery.status, "delivery_pending"
    )
    return TimelineEvent(kind, at=delivery.delivered_at, delivery_id=delivery.id)


class ProjectionUnavailable(ValueError):
    """A versão histórica não permite obter uma projeção verificável."""


@dataclass(frozen=True)
class PlanProjection:
    public_id: str
    form_slug: str
    form_version: str
    form_version_number: int
    attempt: int
    audience: str
    document: dict[str, Any]


def get_job_plan_projection(
    session: Session, public_id: str, *, forms: FormRegistry, audience: str = "participant"
) -> PlanProjection | None:
    """Consulta somente a tentativa atual e a versão histórica, sem contato nem respostas."""
    from app.forms import PlanProjectionError
    from app.submission_identity import submission_form

    job = session.scalar(sa.select(Job).where(Job.public_id == public_id))
    if job is None:
        return None
    plan = session.scalar(sa.select(Plan).where(Plan.job_id == job.id, Plan.version == job.attempt))
    if plan is None:
        raise ProjectionUnavailable("O plano da tentativa atual ainda não foi gerado.")
    submission = session.get_one(Submission, job.submission_id)
    try:
        spec = submission_form(forms, submission)
        document = spec.project_plan(plan.document, audience=audience)
    except (PlanProjectionError, TerminalError):
        raise ProjectionUnavailable(
            "A projeção não está disponível para a versão histórica deste plano. Consulte a configuração do formulário."
        ) from None
    return PlanProjection(
        job.public_id,
        submission.form_slug,
        submission.form_version,
        submission.form_version_number,
        plan.version,
        audience,
        document,
    )
