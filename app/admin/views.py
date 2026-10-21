"""Consultas do admin, já no formato das telas: rótulos em pt-BR e datas no horário de Brasília.

Os rótulos dos estados ficam aqui, num lugar só; os templates só dispõem o que recebem.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import destinations
from app.adapters.storage import ArtifactStorage
from app.adapters.webhook import Probe, outcome
from app.admin.actions.destinations import BODY_START_CHARS
from app.admin.actions.versions import DRAFT_REF
from app.admin.localtime import LOCAL_TZ
from app.admin.queries import destinations as destination_queries
from app.admin.queries import forms as form_queries
from app.admin.queries import jobs as job_queries
from app.admin.reading import FormReading, reading_of
from app.capture_deliveries import Counts
from app.errors import FailureCode
from app.forms import (
    FormRegistry,
    FormSpec,
    Question,
    UnknownForm,
    drafts,
)
from app.forms import retention as form_retention
from app.forms import stored as stored_forms
from app.integrations import syncs as integration_syncs
from app.pdf_availability import PdfAvailability
from app.retention import Due
from app.settings import Settings

PAGE_SIZE = 25


@dataclass(frozen=True)
class Badge:
    label: str
    icon: str
    variant: str  # ok | warn | err | prog | neutral | wine


JOB_STATUS_BADGES = {
    "queued": Badge("Na fila", "clock", "neutral"),
    "running": Badge("Gerando", "loader", "prog"),
    "completed": Badge("Concluído", "check", "ok"),
    "failed": Badge("Falhou", "alert-circle", "err"),
}
STATUS_OPTIONS = [(status, badge.label) for status, badge in JOB_STATUS_BADGES.items()]
# As escolhas de "Recebido" (o `period` da URL, de `job_queries.PERIOD_DAYS`); "Tudo" é sem valor.
PERIOD_OPTIONS = [("today", "Hoje"), ("7d", "Últimos 7 dias"), ("30d", "Últimos 30 dias")]
STAGE_LABELS = {
    "queued": "iniciando",
    "generating": "gerando o plano",
    "rendering": "montando o PDF",
}
DELIVERY_BADGES = {
    "pending": Badge("Pendente", "clock", "warn"),
    "succeeded": Badge("Entregue", "check", "ok"),
    "failed": Badge("Falhou", "x", "err"),
}
# As escolhas de "Entrega" (o `delivery` da URL, de `job_queries.DELIVERY_FILTERS`); "Todas" é sem
# valor. O rótulo é o do selo da entrega na coluna "Entregas e planilha".
DELIVERY_OPTIONS = [
    (status, DELIVERY_BADGES[status].label) for status in job_queries.DELIVERY_FILTERS
]
DESTINATION_LABELS = {"webhook": "Webhook"}
PRIORITY_VARIANTS = {"P1": "wine", "P2": "prog", "P3": "neutral"}
FAILURE_LABELS: dict[str, str] = {
    FailureCode.LLM_UNAVAILABLE: "IA indisponível",
    FailureCode.LLM_REJECTED: "IA recusou o pedido",
    FailureCode.PLAN_INVALID: "plano inválido",
    FailureCode.RENDER_FAILED: "falha ao montar o PDF",
    FailureCode.STORAGE_FAILED: "falha ao guardar o PDF",
    FailureCode.DELIVERY_REJECTED: "destino recusou a entrega",
    FailureCode.DELIVERY_UNAVAILABLE: "destino indisponível",
    FailureCode.DELIVERY_UNCONFIGURED: "sem destino do webhook",
    FailureCode.INTEGRATION_UNAVAILABLE: "planilha indisponível",
    FailureCode.INTEGRATION_REJECTED: "planilha recusou o envio",
    FailureCode.INTEGRATION_UNCONFIGURED: "integração desativada ou sem token",
    FailureCode.INTEGRATION_MAPPING: "mapeamento não serve para o envio",
    FailureCode.STUCK: "execução parada",
    FailureCode.INTERNAL: "erro interno",
}


def _job_badge(status: str) -> Badge:
    return JOB_STATUS_BADGES.get(status, Badge(status, "info", "neutral"))


def _priority_badge(priority: str | None) -> Badge | None:
    return Badge(priority, "flag", PRIORITY_VARIANTS.get(priority, "neutral")) if priority else None


def _destination(name: str) -> str:
    return DESTINATION_LABELS.get(name, name)


def _failure(code: str | None) -> str:
    return FAILURE_LABELS.get(code or "", code or "motivo não registrado")


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _ordered_keys(data: dict[str, Any], model: type[BaseModel] | None) -> list[str]:
    """Ordem dos campos do formulário: o JSONB do Postgres não guarda a ordem de gravação."""
    fields = model.model_fields if model else {}
    return [name for name in fields if name in data] + [name for name in data if name not in fields]


def _fields(
    data: dict[str, Any],
    model: type[BaseModel] | None,
    labels: Mapping[str, str] | None = None,
) -> list[tuple[str, Any]]:
    """(rótulo, valor); rótulo = o que o formulário define (`labels`, pelo nome do campo, ou o
    `title` do campo no modelo) ou o nome legível."""
    fields = model.model_fields if model else {}
    titles = {name: field.title for name, field in fields.items() if field.title}
    titles |= labels or {}
    return [
        (titles.get(key) or key.replace("_", " ").capitalize(), data[key])
        for key in _ordered_keys(data, model)
    ]


def _size(byte_size: int) -> str:
    if byte_size < 1024 * 1024:
        return f"{math.ceil(byte_size / 1024)} KB"
    return f"{byte_size / (1024 * 1024):.1f} MB".replace(".", ",")


@dataclass(frozen=True)
class Moment:
    iso: str
    text: str


def moment(value: datetime | str | None) -> Moment | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    local = value.astimezone(LOCAL_TZ)
    return Moment(iso=local.isoformat(timespec="seconds"), text=local.strftime("%d/%m/%Y %H:%M"))


# --- lista ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class JobRow:
    public_id: str
    name: str | None
    email: str | None
    form_title: str  # o título do formulário no admin; o endereço, se o registro não o tem
    form_version: str  # a versão com que a lead respondeu, no texto que o webhook leva
    status: Badge
    stage: str | None  # só enquanto gera
    priority: Badge | None
    received: Moment | None
    deliveries: tuple[Badge, ...]  # do PDF da tentativa atual
    syncs: tuple[Badge, ...]  # as sincronizações com a planilha, do mesmo PDF


@dataclass(frozen=True)
class FilterOption:
    """Uma escolha de select da barra de filtros ("Formulário", "Versão"): o valor (o que vai na URL)
    e o texto."""

    value: str
    label: str


@dataclass(frozen=True)
class JobPage:
    rows: list[JobRow]
    total: int
    page: int
    pages: int
    form_options: tuple[FilterOption, ...]  # as do select "Formulário", sem a "Todos"
    # As do select "Versão", sem a "Todas": as do formulário escolhido, da mais nova à mais antiga. Vazio
    # sem formulário escolhido, e a página nem mostra o select.
    version_options: tuple[FilterOption, ...]


def _form_options(
    choices: list[job_queries.FormChoice], filters: job_queries.JobFilters
) -> tuple[FilterOption, ...]:
    """Um item por formulário do registro, pelo título; o inativo leva "(inativo)". Um endereço de
    formulário que o registro não tem, mas que filtra a lista, ganha o item dele: o select diz o que
    a lista está filtrando, em vez de mostrar "Todos" sobre uma lista vazia."""
    options = [
        FilterOption(choice.slug, choice.title if choice.active else f"{choice.title} (inativo)")
        for choice in choices
    ]
    if filters.form and all(option.value != filters.form for option in options):
        options.append(FilterOption(filters.form, f"{filters.form} (não encontrado)"))
    return tuple(options)


def _version_options(
    versions: list[str], filters: job_queries.JobFilters
) -> tuple[FilterOption, ...]:
    """Um item por versão dos pedidos do formulário escolhido, no texto que a coluna "Formulário"
    mostra (a mais nova primeiro). Sem formulário escolhido, nenhum: o select nem aparece. Uma versão
    que filtra a lista mas o formulário não tem (um endereço escrito à mão, ou a de outro formulário)
    ganha o item dela, como o formulário: o select diz o que a lista está filtrando, em vez de mostrar
    "Todas" sobre uma lista vazia."""
    if not filters.form:
        return ()
    options = [FilterOption(version, version) for version in versions]
    if filters.form_version and filters.form_version not in versions:
        options.append(
            FilterOption(filters.form_version, f"{filters.form_version} (não encontrada)")
        )
    return tuple(options)


def list_jobs(
    session: Session, filters: job_queries.JobFilters, *, forms: FormRegistry, page: int = 1
) -> JobPage:
    """Os pedidos que os filtros deixam passar (busca por parte do nome ou do e-mail, ou pelo código
    exato; status; formulário e versão; chegada; entrega; planilha), mais recentes primeiro. A consulta é
    `app.admin.queries.jobs`, a mesma da API; aqui ela ganha rótulo e data de Brasília."""
    conditions = filters.conditions()
    total = job_queries.count_jobs(session, conditions)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(page, 1), pages)
    found = job_queries.jobs_by_page(
        session,
        conditions,
        forms=forms,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
        with_lead=True,
    )
    choices = job_queries.form_choices(session)
    versions = job_queries.form_versions(session, filters.form) if filters.form else []
    titles = {choice.slug: choice.title for choice in choices}
    rows = []
    for row in found:
        assert row.lead is not None  # a página sempre pede a lead
        rows.append(
            JobRow(
                public_id=row.state.public_id,
                name=row.lead.name,
                email=row.lead.email,
                form_title=titles.get(row.state.form_slug, row.state.form_slug),
                form_version=row.state.form_version,
                status=_job_badge(row.state.status),
                stage=STAGE_LABELS.get(row.state.stage) if row.state.status == "running" else None,
                priority=_priority_badge(row.lead.priority),
                received=moment(row.state.created_at),
                deliveries=tuple(_delivery_badge(d) for d in row.state.deliveries),
                syncs=tuple(_sync_badge(sync) for sync in row.state.syncs),
            )
        )
    return JobPage(
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        form_options=_form_options(choices, filters),
        version_options=_version_options(versions, filters),
    )


# A frase da lista vazia quando só a entrega, ou só a planilha, filtra: sem entrega ou sincronização
# que falhou é boa notícia, e a frase diz isso em vez de "nenhum pedido".
NOTHING_FOUND = {
    ("delivery", "failed"): "Nenhuma entrega falhou",
    ("delivery", "pending"): "Nenhuma entrega pendente",
    ("sync", "failed"): "Nenhuma sincronização falhou",
    ("sync", "pending"): "Nenhuma sincronização pendente",
}


def nothing_found(filters: job_queries.JobFilters) -> str:
    """O que a lista vazia diz (sem o "com estes filtros", que a página acrescenta). Com a entrega e
    a planilha filtrando juntas, a frase não diz qual das duas zerou a lista: é a de sempre."""
    if bool(filters.delivery) != bool(filters.sync):
        key = ("delivery", filters.delivery) if filters.delivery else ("sync", filters.sync)
        return NOTHING_FOUND[key]
    return "Nenhum pedido encontrado"


def _delivery_badge(delivery: job_queries.DeliveryBrief) -> Badge:
    badge = DELIVERY_BADGES.get(delivery.status, Badge(delivery.status, "info", "neutral"))
    label = f"{_destination(delivery.destination)} {badge.label.lower()}"
    return Badge(label, badge.icon, badge.variant)


def _sync_badge(sync: job_queries.SyncBrief) -> Badge:
    """O selo da sincronização na lista ("Planilha sincronizada"): o ícone e a cor são os do selo do
    detalhe (`SYNC_BADGES`); o estado vai no feminino, concordando com "planilha"."""
    badge = SYNC_BADGES.get(sync.status, Badge(sync.status, "info", "neutral"))
    state = SYNC_LIST_STATES.get(sync.status, sync.status)
    return Badge(
        f"{INTEGRATION_LABELS.get(sync.integration, sync.integration)} {state}",
        badge.icon,
        badge.variant,
    )


# --- detalhe ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    kind: str  # ok | bad | wait (ícone e cor)
    label: str
    at: Moment | None = None
    note: str | None = None


@dataclass(frozen=True)
class Attempt:
    number: int
    outcome: Badge | None  # None quando a tentativa antiga não deixou registro do fim
    events: list[Event]


@dataclass(frozen=True)
class DeliveryView:
    id: uuid.UUID
    destination: str
    state: str  # pending | succeeded | failed
    badge: Badge
    attempts: int
    last_error: str | None
    when: Moment | None
    job_attempt: int
    run_url: str | None
    target: str | None  # para qual destino do webhook foi o último envio, e o host

    audience: str = "internal"
    authorization_origin: str = "legacy"
    current_config_version: int | None = None
    event: str = "plano.concluido"
    blocked_reason: str | None = None


SYNC_BADGES = {
    "pending": Badge("Pendente", "clock", "warn"),
    "succeeded": Badge("Sincronizado", "check", "ok"),
    "failed": Badge("Falhou", "x", "err"),
}
# Na lista o selo diz "Planilha …" (`INTEGRATION_LABELS`, o tipo da integração) e o estado concorda
# com ela; o selo do detalhe, acima, fala da sincronização ("Sincronizado").
INTEGRATION_LABELS = {"baserow": "Planilha"}
SYNC_LIST_STATES = {"pending": "pendente", "succeeded": "sincronizada", "failed": "falhou"}
# As escolhas de "Planilha" (o `sync` da URL, de `job_queries.SYNC_FILTERS`); "Todas" é sem valor.
# O rótulo é o do selo da sincronização no detalhe do pedido.
SYNC_OPTIONS = [(status, SYNC_BADGES[status].label) for status in job_queries.SYNC_FILTERS]
SYNC_OUTCOMES = {"created": "linha criada", "updated": "linha atualizada"}


@dataclass(frozen=True)
class SyncView:
    """A sincronização de um pedido com a tabela de uma integração (`app.integrations.syncs`)."""

    id: uuid.UUID
    job_public_id: str
    target: str  # a integração: o Baserow, o host e a tabela
    state: str  # pending | succeeded | failed
    badge: Badge
    attempts: int
    job_attempt: int
    last_error: str | None
    result: str | None  # o que ela fez com a linha, depois de sincronizada
    when: Moment | None
    job_url: str


def sync_view(row: integration_syncs.SyncRow) -> SyncView:
    outcome = SYNC_OUTCOMES.get(row.outcome or "")
    return SyncView(
        id=row.id,
        job_public_id=row.job_public_id,
        target=f"Baserow · {row.host} · tabela {row.table_id}",
        state=row.status,
        badge=SYNC_BADGES.get(row.status, Badge(row.status, "info", "neutral")),
        attempts=row.attempts,
        job_attempt=row.job_attempt,
        last_error=row.last_error,
        result=f"{outcome} (nº {row.row_id})" if outcome and row.row_id else outcome,
        when=moment(row.synced_at or row.created_at),
        job_url=f"/admin/jobs/{quote(row.job_public_id, safe='')}",
    )


@dataclass(frozen=True)
class PdfView:
    version: int
    size: str
    created: Moment | None
    removed: Moment | None = None  # arquivo apagado pela retenção (app.retention)


@dataclass(frozen=True)
class PlanView:
    version: int
    fields: list[tuple[str, Any]]  # (rótulo, valor), na ordem do modelo do plano
    keys: list[str]  # o nome de cada campo de `fields`
    labels: Mapping[str, str]  # os rótulos dos campos do plano, pelo caminho dos nomes
    json: str
    note: str


@dataclass(frozen=True)
class AnswerGroup:
    title: str | None  # etapa do formulário
    items: list[tuple[str, Any]]  # (pergunta, resposta legível)


def _answer_groups(answers: dict[str, Any], spec: FormSpec | None) -> list[AnswerGroup]:
    """Respostas como a lead viu: etapa, pergunta e rótulo da opção (metadados de tela do
    formulário). Sem tela, ou para campos fora dela, ordem e nomes do modelo das respostas."""
    model = spec.answers_model if spec else None
    if spec is None or spec.screen is None:
        return [AnswerGroup(None, _fields(answers, model))]
    groups: list[AnswerGroup] = []
    on_screen: set[str] = set()
    for step in spec.screen.steps:
        on_screen.update(question.name for question in step.questions)
        items = [
            (question.label, _option_labels(question, answers[question.name]))
            for question in step.questions
            if question.name in answers
        ]
        if items:
            groups.append(AnswerGroup(step.title, items))
    rest = {key: value for key, value in answers.items() if key not in on_screen}
    if rest:
        groups.append(AnswerGroup("Outras respostas", _fields(rest, model)))
    return groups


def _option_labels(question: Question, value: Any) -> Any:
    """Valor da opção -> texto que a lead viu (só em perguntas de escolha)."""
    labels = {option.value: option.label for option in question.options}
    if not labels:
        return value
    if isinstance(value, list):
        return [labels.get(item, item) if isinstance(item, str) else item for item in value]
    return labels.get(value, value) if isinstance(value, str) else value


@dataclass(frozen=True)
class ConsentView:
    granted: bool
    label: str
    note: str


@dataclass(frozen=True)
class JobDetail:
    public_id: str
    attempt: int
    status: Badge
    can_reprocess: bool
    failure: str | None
    name: str | None
    email: str | None
    phone: str | None
    form: str
    received: Moment | None
    link_expires: Moment | None
    link_expired: bool
    run_url: str | None
    priority: Badge | None
    persona: str | None
    scores: list[tuple[str, Any]]
    answers: list[AnswerGroup]
    consents: list[ConsentView]
    plan: PlanView | None
    current_pdf: PdfAvailability
    release: dict[str, Any]
    pdfs: list[PdfView]
    deliveries: list[DeliveryView]
    syncs: list[SyncView]
    timeline: list[Attempt]


def job_detail(
    session: Session,
    public_id: str,
    *,
    forms: FormRegistry,
    run_url_template: str | None,
    now: datetime,
    storage: ArtifactStorage | None = None,
) -> JobDetail | None:
    """O pedido como a lead o enviou: rótulos e ordem vêm da versão do formulário que ela
    respondeu (`forms`), não da atual. A consulta é `app.admin.queries.jobs`, a mesma da API; aqui
    ela ganha rótulo, data de Brasília e os textos livres de volta nos eventos."""
    found = job_queries.read_job(
        session, public_id, forms=forms, now=now, with_lead=True, storage=storage
    )
    if found is None:
        return None
    state, lead = found.state, found.lead
    assert lead is not None  # a página sempre pede a lead
    try:
        spec: FormSpec | None = forms.get(state.form_slug, state.form_version_number)
    except UnknownForm:  # formulário que saiu do registro: mostra o que ficou gravado
        spec = None
    notes = {(note.kind, note.attempt, note.ref): note.text for note in lead.notes}
    delivery_views = [
        _delivery_view(
            d, run_url_template, notes.get((job_queries.NOTE_DELIVERY_ERROR, d.job_attempt, d.id))
        )
        for d in state.deliveries
    ]
    syncs = [
        sync_view(
            integration_syncs.SyncRow(
                id=s.id,
                job_public_id=state.public_id,
                form_slug=state.form_slug,
                kind=s.integration,
                host=s.host,
                table_id=s.table_id,
                status=s.status,
                attempts=s.attempts,
                last_error=notes.get((job_queries.NOTE_SYNC_ERROR, s.job_attempt, s.id)),
                outcome=s.outcome,
                row_id=s.row_id,
                synced_at=s.synced_at,
                created_at=s.created_at,
                job_attempt=s.job_attempt,
            )
        )
        for s in state.syncs
    ]
    return JobDetail(
        public_id=state.public_id,
        attempt=state.attempt,
        status=_job_badge(state.status),
        can_reprocess=state.status in ("completed", "failed"),
        failure=_failure(state.failure_code) if state.status == "failed" else None,
        name=lead.name,
        email=lead.email,
        phone=lead.phone,
        form=f"{state.form_slug} · versão {state.form_version}",
        received=moment(state.created_at),
        link_expires=moment(state.link_expires_at),
        link_expired=state.link_expired,
        run_url=job_queries.run_url(run_url_template, state.hatchet_run_id),
        priority=_priority_badge(lead.priority),
        persona=lead.persona,
        scores=_fields(lead.scores, None),
        answers=_answer_groups(lead.answers, spec),
        consents=_consents(lead.consent),
        plan=_plan_view(lead.plan, spec) if lead.plan else None,
        current_pdf=state.current_pdf,
        release=state.release,
        pdfs=[
            PdfView(
                version=pdf.version,
                size=_size(pdf.byte_size),
                created=moment(pdf.created_at),
                removed=moment(pdf.purged_at),
            )
            for pdf in state.pdfs
        ],
        deliveries=delivery_views,
        syncs=syncs,
        timeline=_timeline(state, delivery_views, notes),
    )


def _plan_view(plan: job_queries.PlanData, spec: FormSpec | None) -> PlanView:
    model = spec.plan_model if spec else None
    labels = spec.plan_labels if spec else {}
    keys = _ordered_keys(plan.document, model)
    ordered = {key: plan.document[key] for key in keys}
    return PlanView(
        version=plan.version,
        fields=_fields(plan.document, model, labels),
        keys=keys,
        labels=labels,
        json=json.dumps(ordered, ensure_ascii=False, indent=2),
        note=f"tentativa {plan.version} · modelo {plan.model} · prompt {plan.prompt_version}",
    )


WEBHOOK_SCOPE_LABELS = {"form": "destino do formulário", "default": "destino padrão"}


def _delivery_view(
    delivery: job_queries.DeliveryState, template: str | None, last_error: str | None
) -> DeliveryView:
    scope = WEBHOOK_SCOPE_LABELS.get(delivery.scope or "")
    return DeliveryView(
        id=delivery.id,
        destination=_destination(delivery.destination),
        state=delivery.status,
        badge=DELIVERY_BADGES.get(delivery.status, Badge(delivery.status, "info", "neutral")),
        attempts=delivery.attempts,
        last_error=last_error,
        when=moment(delivery.delivered_at),
        job_attempt=delivery.job_attempt,
        run_url=job_queries.run_url(template, delivery.hatchet_run_id),
        target=f"{scope} · {delivery.host}" if scope else None,
        audience=delivery.audience,
        authorization_origin=delivery.authorization_origin,
        current_config_version=delivery.current_config_version,
        event=delivery.event,
        blocked_reason=delivery.blocked_reason,
    )


def _consents(consent: job_queries.Consent) -> list[ConsentView]:
    version = consent.text_version if consent.text_version is not None else "?"
    accepted_at = moment(consent.privacy_at)
    granted = f"{accepted_at.text if accepted_at else 'data não registrada'} · texto {version}"
    return [
        ConsentView(True, "Tratamento das respostas para gerar o plano", granted),
        *(
            ConsentView(flag, label, f"Autorizado · texto {version}" if flag else "Não autorizado")
            for flag, label in (
                (consent.marketing_email, "Novidades por e-mail"),
                (consent.marketing_whatsapp, "Novidades por WhatsApp"),
            )
        ),
    ]


DELIVERY_EVENTS = ("delivery_succeeded", "delivery_failed", "delivery_pending")


def _timeline(
    state: job_queries.JobState,
    deliveries: list[DeliveryView],
    notes: Mapping[tuple[str, int, uuid.UUID | None], str],
) -> list[Attempt]:
    """Uma lista de eventos por tentativa, com o texto de cada um: a consulta diz o que aconteceu, e
    aqui vêm o rótulo, a data de Brasília e o texto livre do motivo, quando houver."""
    by_id = {delivery.id: delivery for delivery in deliveries}
    return [
        Attempt(
            number=attempt.number,
            outcome=JOB_STATUS_BADGES.get(attempt.outcome) if attempt.outcome else None,
            events=[
                _event_view(event, attempt.number, state, by_id, notes) for event in attempt.events
            ],
        )
        for attempt in state.timeline
    ]


def _event_view(
    event: job_queries.TimelineEvent,
    attempt: int,
    state: job_queries.JobState,
    deliveries: Mapping[uuid.UUID, DeliveryView],
    notes: Mapping[tuple[str, int, uuid.UUID | None], str],
) -> Event:
    at = moment(event.at)
    if event.type in DELIVERY_EVENTS:
        assert event.delivery_id is not None
        return _delivery_event(deliveries[event.delivery_id])
    if event.type == "received":
        note = f"formulário {state.form_slug} · versão {state.form_version}"
        return Event("ok", "Pedido recebido", at, note)
    if event.type == "reprocess_requested":
        reason = notes.get((job_queries.NOTE_REPROCESS_REASON, attempt, None))
        return Event("ok", f"Reprocessamento pedido por {event.actor}", at, reason)
    if event.type == "plan_generated":
        note = f"modelo {event.model} · prompt {event.prompt_version}"
        return Event("ok", "Plano gerado pela IA", at, note)
    if event.type == "pdf_generated":
        return Event("ok", "PDF gerado", at, _size(event.byte_size or 0))
    if event.type == "completed":
        return Event("ok", "Concluído", at)
    if event.type == "failed":
        detail = notes.get((job_queries.NOTE_FAILURE_DETAIL, attempt, None))
        return Event("bad", f"Falhou: {_failure(event.failure_code)}", at, detail)
    if event.type == "processing":
        stage = event.stage or ""
        return Event("wait", f"Em processamento: {STAGE_LABELS.get(stage, stage)}")
    if event.type == "queued":
        return Event("wait", "Na fila")
    if event.type == "delivery_resend_requested":
        return Event("ok", f"Reenvio pedido por {event.actor}", at)
    raise ValueError(f"tipo de evento desconhecido: {event.type}")


def _delivery_event(delivery: DeliveryView) -> Event:
    tries = _plural(delivery.attempts, "tentativa", "tentativas")
    destination = delivery.destination.lower()
    if delivery.state == "succeeded":
        return Event("ok", f"Entregue ao {destination}", delivery.when, tries)
    error = f" · último erro: {delivery.last_error}" if delivery.last_error else ""
    if delivery.state == "failed":
        return Event("bad", f"Entrega ao {destination} falhou", None, tries + error)
    return Event("wait", f"Entrega ao {destination} pendente", None, tries + error)


# --- destinos do webhook ----------------------------------------------------------------------

DESTINATIONS_URL = "/admin/destinations"
SCOPE_BADGES = {
    "form": Badge("Próprio", "check", "ok"),
    "default": Badge("Padrão", "send", "neutral"),
    "none": Badge("Sem destino", "alert-triangle", "warn"),
}


def destination_url(form_slug: str | None) -> str:
    """A página do destino; as ações dela são esta URL + /url, /secret, /test e /remove."""
    if form_slug is None:
        return f"{DESTINATIONS_URL}/default"
    return f"{DESTINATIONS_URL}/forms/{quote(form_slug, safe='')}"


@dataclass(frozen=True)
class DestinationView:
    """Um destino guardado como a tela mostra: a URL e as datas, nunca o segredo."""

    url: str
    host: str
    version: int
    created_by: str
    created: Moment | None
    updated_by: str
    updated: Moment | None
    secret_created: Moment | None
    events: tuple[str, ...] = ("plano.concluido",)
    purpose: str = "participant"
    purpose_origin: str = "default"


def _destination_view(stored: destinations.StoredDestination) -> DestinationView:
    return DestinationView(
        url=stored.url,
        host=stored.host,
        version=stored.version,
        created_by=stored.created_by,
        created=moment(stored.created_at),
        updated_by=stored.updated_by,
        updated=moment(stored.updated_at),
        secret_created=moment(stored.secret_created_at),
        events=stored.events,
        purpose=stored.purpose,
        purpose_origin=stored.purpose_origin,
    )


@dataclass(frozen=True)
class FormDestinationRow:
    title: str
    slug: str
    badge: Badge  # próprio, padrão ou sem destino
    host: str | None
    edit_url: str


@dataclass(frozen=True)
class DestinationsOverview:
    default: DestinationView | None
    forms: list[FormDestinationRow]
    unserved: list[str]  # slugs dos formulários sem destino nenhum: os planos não são entregues
    unreadable: list[str]  # destinos cujo segredo a chave atual não abre ("padrão" ou o slug)


def destinations_overview(
    session: Session, forms: Iterable[FormSpec], cipher: destinations.SecretCipher | None
) -> DestinationsOverview:
    """O destino de cada formulário, na tela (a consulta é `app.admin.queries.destinations`). Um
    destino guardado para um slug que saiu do código também aparece, para poder ser removido."""
    found = destination_queries.overview(session, forms, cipher)
    return DestinationsOverview(
        default=_destination_view(found.default) if found.default else None,
        forms=[
            FormDestinationRow(
                title=row.title or row.slug,
                slug=row.slug,
                badge=SCOPE_BADGES[row.serving],
                host=row.host,
                edit_url=destination_url(row.slug),
            )
            for row in found.forms
        ],
        unserved=found.unserved,
        unreadable=[slug or "padrão" for slug in found.unreadable],
    )


SERVING_PHRASES = {"form": "o destino próprio", "default": "o destino padrão"}


@dataclass(frozen=True)
class ContactInPromptView:
    """Os dados de contato que o prompt cita, por extenso ("o nome e o e-mail")."""

    labels: str
    many: bool  # mais de um: a confirmação vai no plural


def contact_roles(roles: Sequence[str]) -> ContactInPromptView:
    return ContactInPromptView(labels=stored_forms.contact_in_words(roles), many=len(roles) > 1)


# --- formulários ------------------------------------------------------------------------------

FORMS_URL = "/admin/forms"
FORM_STATUS_BADGES = {
    True: Badge("Ativo", "check", "ok"),
    False: Badge("Inativo", "x", "neutral"),
}
NOT_COMPILING = Badge("Não compila", "alert-triangle", "err")
DRAFT_PENDING = Badge("Rascunho pendente", "file", "warn")
DATA_TO_REVIEW = Badge("Dados a revisar", "alert-triangle", "warn")


def form_url(slug: str) -> str:
    """A página do formulário no admin: a leitura da versão publicada e a situação."""
    return f"{FORMS_URL}/{quote(slug, safe='')}"


def draft_url(slug: str) -> str:
    """O editor do rascunho; o teste e a publicação ficam embaixo dele."""
    return f"{form_url(slug)}/draft"


def compare_url(slug: str, old: int | str, new: int | str) -> str:
    """A comparação de duas versões, por seção; `DRAFT_REF` no lugar de um número é o rascunho."""
    return f"{form_url(slug)}/versions/compare?{urlencode({'from': old, 'to': new})}"


def draft_test_url(slug: str, test_id: uuid.UUID) -> str:
    """A página de um teste do rascunho, que acompanha o resultado."""
    return f"{draft_url(slug)}/tests/{test_id}"


@dataclass(frozen=True)
class DraftNav:
    """Um formulário do banco e os endereços das páginas do rascunho dele, montados num lugar só."""

    slug: str
    title: str  # o nome do formulário no admin
    form_url: str
    draft_url: str  # o editor
    preview_url: str  # a página pública do rascunho, na largura de um celular
    pdf_url: str  # o editor do PDF e da prévia da página de resultado
    tests_url: str
    prompt_url: str  # o prompt montado com outras respostas (POST)
    score_url: str  # a pontuação com outras respostas (POST)
    publish_url: str
    compare_url: str | None  # o rascunho contra a versão publicada; None antes da versão 1
    compare_base_url: str  # a comparação, para escolher as versões

    def result_url(self, test_id: uuid.UUID) -> str:
        return draft_test_url(self.slug, test_id)


def draft_nav(draft: drafts.Draft) -> DraftNav:
    base = draft_url(draft.slug)
    return DraftNav(
        slug=draft.slug,
        title=draft.title,
        form_url=form_url(draft.slug),
        draft_url=base,
        preview_url=f"{base}/preview",
        pdf_url=f"{base}/pdf",
        tests_url=f"{base}/tests",
        prompt_url=f"{base}/prompt",
        score_url=f"{base}/score",
        publish_url=f"{base}/publish",
        compare_url=compare_url(draft.slug, draft.current.number, DRAFT_REF)
        if draft.current is not None
        else None,
        compare_base_url=f"{form_url(draft.slug)}/versions/compare",
    )


@dataclass(frozen=True)
class DraftSummary:
    """O rascunho de um formulário do banco, como a página dele e o editor mostram."""

    nav: DraftNav
    badge: Badge | None  # rascunho pendente
    saved_by: str | None
    saved: Moment | None
    base_version: int | None  # de que versão ele foi copiado; None antes da versão 1
    current_version: int | None  # a versão publicada; None antes da primeira publicação
    token: str  # o que uma ação sobre o rascunho devolve (`Draft.token`)
    # As imagens da versão publicada que foram trocadas desde então (`Draft.stale_assets`): há o
    # que publicar mesmo sem o texto ter mudado, e então `saved_by` é None (não há rascunho salvo).
    stale_assets: tuple[str, ...] = ()

    @property
    def published(self) -> bool:
        return self.current_version is not None


def draft_summary(draft: drafts.Draft) -> DraftSummary:
    pending = draft.pending
    current = draft.current.number if draft.current is not None else None
    return DraftSummary(
        nav=draft_nav(draft),
        badge=DRAFT_PENDING if pending else None,
        saved_by=draft.updated_by if pending else None,
        saved=moment(draft.updated_at) if pending else None,
        base_version=draft.base_version if pending else current,
        current_version=current,
        token=draft.token,
        stale_assets=draft.stale_assets,
    )


@dataclass(frozen=True)
class VersionRow:
    """Uma versão no histórico do formulário."""

    number: int
    published_by: str
    published: Moment | None
    note: str
    current: bool
    export_url: str
    compare_url: str | None  # com a versão anterior; None na primeira
    revert_url: str


def version_rows(slug: str, entries: Sequence[stored_forms.VersionEntry]) -> list[VersionRow]:
    latest = max((entry.number for entry in entries), default=0)
    return [
        VersionRow(
            number=entry.number,
            published_by=entry.published_by,
            published=moment(entry.published_at),
            note=entry.note,
            current=entry.number == latest,
            export_url=export_url(slug, entry.number),
            compare_url=compare_url(slug, entry.number - 1, entry.number)
            if entry.number > 1
            else None,
            revert_url=f"{form_url(slug)}/versions/{entry.number}/revert",
        )
        for entry in entries
    ]


@dataclass(frozen=True)
class FormRow:
    """Um formulário na lista do admin."""

    slug: str
    title: str
    url: str  # a página dele no admin
    public_url: str
    status: Badge  # ativo ou inativo
    version: str  # a versão publicada, por extenso
    published: Moment | None  # quando ela foi publicada
    problem: Badge | None  # a versão atual não compila mais
    draft: Badge | None  # o aviso de rascunho não publicado
    review: Badge | None  # o aviso de dados vencidos (`notices`)
    notices: tuple[str, ...]  # o que conferir nos dados do formulário (`app.forms.data_notices`)
    submissions: str  # "3 respostas"
    responses_url: str  # os dois históricos de respostas deste formulário


form_title = form_queries.form_title  # o nome do formulário; a consulta é dividida com a API


def _version_label(version_number: int | None) -> str:
    """A versão publicada, por extenso: o número da atual."""
    if version_number is not None:
        return f"Versão {version_number}"
    return "Nenhuma versão publicada"


def form_list(
    session: Session, forms: FormRegistry, *, public_base_url: str, now: datetime
) -> list[FormRow]:
    """Todo formulário, pelo slug, para a lista do admin. A consulta é `app.admin.queries.forms`, a
    mesma da API; aqui ela ganha rótulo e data de Brasília."""
    return [
        FormRow(
            slug=row.slug,
            title=row.title,
            url=form_url(row.slug),
            public_url=row.public_url,
            status=FORM_STATUS_BADGES[row.active],
            version=_version_label(row.version_number),
            published=moment(row.published_at) if row.published_at else None,
            problem=NOT_COMPILING if row.compiles is False else None,
            draft=DRAFT_PENDING if row.draft_pending else None,
            review=DATA_TO_REVIEW if row.notices else None,
            notices=row.notices,
            submissions=_plural(row.submissions, "resposta", "respostas"),
            responses_url="/admin/responses?" + urlencode({"form": row.slug}),
        )
        for row in form_queries.form_summaries(
            session, forms, public_base_url=public_base_url, now=now
        )
    ]


@dataclass(frozen=True)
class FormSource:
    """Um formulário de onde a operadora pode partir para criar outro."""

    slug: str
    title: str


def form_sources(session: Session) -> list[FormSource]:
    """Todo formulário que dá para duplicar, com o nome dele no admin, publicado ou não."""
    return [FormSource(row.slug, row.title) for row in stored_forms.listing(session)]


@dataclass(frozen=True)
class NewFormPage:
    """O formulário novo, como a página de criar mostra: o título, o endereço e a origem que a
    operadora escolheu e o problema de cada campo (`title`, `slug`, `source`), se a criação foi
    recusada. Sem origem, o rascunho parte do modelo inicial."""

    title: str
    slug: str
    source: str
    sources: list[FormSource]
    errors: Mapping[str, str]
    address_prefix: str  # o endereço público até o slug (`https://.../p/`)
    slug_max_length: int
    generation: str = ""


COPY_SUFFIX = " (cópia)"
COPY_SLUG_SUFFIX = "-copia"


def copy_suggestion(session: Session, source: FormSource) -> tuple[str, str]:
    """O título e o endereço sugeridos para a cópia de um formulário; o endereço fica em branco
    (e sai do título) se a sugestão não serve."""
    title = f"{source.title}{COPY_SUFFIX}"
    slug = f"{source.slug}{COPY_SLUG_SUFFIX}"
    return title, slug if stored_forms.slug_problem(session, slug) is None else ""


@dataclass(frozen=True)
class FormVersionView:
    """A versão publicada de um formulário, como a leitura mostra."""

    label: str  # "Versão 3" ou "Nenhuma versão publicada"
    text: str | None  # o rótulo que vai no envio e no webhook; None se a versão não compila
    published_by: str | None
    published: Moment | None
    note: str | None
    fingerprint: str | None
    export_url: str | None  # a definição da versão; só uma versão publicada tem


@dataclass(frozen=True)
class FormDetail:
    slug: str
    title: str
    url: str  # a página dele no admin, prefixo das ações
    active: bool
    status: Badge
    draft: Badge | None  # o aviso de rascunho não publicado
    # O que conferir nos dados do formulário (`app.forms.data_notices`); vazio: tudo em dia.
    notices: tuple[str, ...]
    draft_summary: DraftSummary
    history: list[VersionRow]  # as versões publicadas, a mais recente primeiro
    public_url: str
    version: FormVersionView
    destination: str | None  # para onde vão os planos dele; None: para lugar nenhum
    destination_url: str  # onde configurar o destino próprio dele
    retention: list[RetentionRow]  # os prazos de retenção que valem para ele
    retention_url: str  # onde a operadora os muda
    submissions: str
    reading: FormReading | None  # None: a versão atual não compila mais
    # Sem versão publicada (criado do zero ou duplicado no admin): a leitura é a do rascunho.
    unpublished: bool = False
    integration: IntegrationBox | None = None  # a integração com a planilha, se ele tem
    integration_issues: tuple[str, ...] = ()
    destination_events: tuple[str, ...] = ()

    @property
    def duplicate_url(self) -> str:
        """A página de criar, com este formulário como origem da cópia."""
        return duplicate_url(self.slug)


def duplicate_url(slug: str) -> str:
    return f"{FORMS_URL}/new?{urlencode({'from': slug})}"


INTEGRATION_BADGES = {
    True: Badge("Ativa", "check", "ok"),
    False: Badge("Desativada", "x", "neutral"),
}


@dataclass(frozen=True)
class IntegrationBox:
    """A integração do formulário com a planilha, como a página dele a resume."""

    url: str  # a página da integração
    configured: bool
    badge: Badge | None
    host: str | None
    table_id: int | None
    pending: int
    failed: int


def integration_box(summary: form_queries.IntegrationSummary, slug: str) -> IntegrationBox:
    url = f"{FORMS_URL}/{quote(slug, safe='')}/baserow"
    if not summary.configured:
        return IntegrationBox(url, False, None, None, None, 0, 0)
    assert summary.active is not None
    return IntegrationBox(
        url=url,
        configured=True,
        badge=INTEGRATION_BADGES[summary.active],
        host=summary.host,
        table_id=summary.table_id,
        pending=summary.pending,
        failed=summary.failed,
    )


def export_url(slug: str, number: int) -> str:
    return f"{form_url(slug)}/versions/{number}/export"


def form_detail(
    session: Session,
    slug: str,
    forms: FormRegistry,
    *,
    public_base_url: str,
    retention_pdf_days: int,
    retention_days: int,
    now: datetime,
) -> FormDetail | None:
    """O formulário com a leitura da versão publicada, a de maior número, que a leitura monta
    também da definição guardada; None se ele não existe. Um formulário ainda sem versão (criado do
    zero ou duplicado) tem a leitura do rascunho. A consulta é `app.admin.queries.forms`, a mesma da
    API; aqui ela ganha rótulo, data de Brasília e a leitura. `now` é o dia contra o qual os dados
    dele são conferidos."""
    found = form_queries.form_detail(
        session,
        slug,
        forms,
        public_base_url=public_base_url,
        retention_pdf_days=retention_pdf_days,
        retention_days=retention_days,
        now=now,
    )
    if found is None:
        return None
    published = found.published
    if published is None:
        version = FormVersionView(
            label="Nenhuma versão publicada",
            text=None,
            published_by=None,
            published=None,
            note=None,
            fingerprint=None,
            export_url=None,
        )
    else:
        version = FormVersionView(
            label=f"Versão {published.number}",
            text=published.text,
            published_by=published.published_by,
            published=moment(published.published_at),
            note=published.note,
            fingerprint=published.fingerprint,
            export_url=export_url(slug, published.number),
        )
    summary = draft_summary(found.draft)
    return FormDetail(
        slug=slug,
        title=found.title,
        url=form_url(slug),
        active=found.active,
        status=FORM_STATUS_BADGES[found.active],
        draft=summary.badge,
        notices=found.notices,
        draft_summary=summary,
        history=version_rows(slug, found.history),
        public_url=found.public_url,
        version=version,
        destination=(
            f"{SERVING_PHRASES[found.destination.scope]} ({found.destination.host})"
            if found.destination
            else None
        ),
        destination_url=destination_url(slug),
        retention=retention_rows(
            found.retention.own,
            default_pdf_days=found.retention.default_pdf_days,
            default_request_days=found.retention.default_request_days,
        ),
        retention_url=retention_url(slug),
        submissions=_plural(found.submissions, "envio", "envios"),
        reading=reading_of(found.spec, found.definition)
        if found.spec and found.definition
        else None,
        unpublished=found.unpublished,
        integration=integration_box(found.integration, slug),
        integration_issues=found.integration_issues,
        destination_events=found.destination.events if found.destination else (),
    )


# --- retenção de um formulário ----------------------------------------------------------------

RETENTION_SOURCES = {True: "prazo deste formulário", False: "padrão do ambiente"}


@dataclass(frozen=True)
class RetentionRow:
    """Um prazo de retenção que vale para o formulário e de onde ele vem."""

    label: str
    value: str
    source: str


def retention_url(slug: str) -> str:
    """A página dos prazos de retenção do formulário; o formulário deles é enviado para ela."""
    return f"{form_url(slug)}/retention"


def retention_rows(
    own: form_retention.Periods, *, default_pdf_days: int, default_request_days: int
) -> list[RetentionRow]:
    """Os dois prazos que valem: o próprio do formulário, ou o padrão do ambiente onde falta."""
    pdf = own.pdf_days if own.pdf_days is not None else default_pdf_days
    request = own.request_days if own.request_days is not None else default_request_days
    return [
        RetentionRow(
            "PDF",
            f"{_plural(pdf, 'dia', 'dias')} depois de gerado",
            RETENTION_SOURCES[own.pdf_days is not None],
        ),
        RetentionRow(
            "Pedido inteiro",
            f"{_plural(request, 'dia', 'dias')} depois da última mudança",
            RETENTION_SOURCES[own.request_days is not None],
        ),
    ]


@dataclass(frozen=True)
class RetentionPage:
    """A página dos prazos de um formulário: o que vale hoje, o padrão e o que a mudança confere."""

    slug: str
    title: str
    url: str  # a leitura do formulário
    action: str  # para onde o formulário dos prazos vai
    rows: list[RetentionRow]
    own: form_retention.Periods
    expected: str  # o que esta página viu, devolvido no envio para recusar uma página velha
    default_pdf_days: int
    default_request_days: int
    link_ttl_days: int
    max_days: int


def retention_page(session: Session, slug: str, settings: Settings) -> RetentionPage | None:
    """A página dos prazos de retenção do formulário; None se ele não existe. A consulta é
    `app.admin.queries.forms`, a mesma da API; aqui ela ganha rótulo e o endereço das ações."""
    found = form_queries.form_retention_of(session, slug, settings)
    if found is None:
        return None
    retention = found.retention
    return RetentionPage(
        slug=slug,
        title=found.title,
        url=form_url(slug),
        action=retention_url(slug),
        rows=retention_rows(
            retention.own,
            default_pdf_days=retention.default_pdf_days,
            default_request_days=retention.default_request_days,
        ),
        own=retention.own,
        expected=retention.version,
        default_pdf_days=retention.default_pdf_days,
        default_request_days=retention.default_request_days,
        link_ttl_days=found.link_ttl_days,
        max_days=found.max_days,
    )


@dataclass(frozen=True)
class ReductionView:
    """O que a próxima retenção apagaria com os prazos digitados, para a operadora confirmar."""

    headline: str
    today: str  # o que apagaria com os prazos de hoje
    seen: str  # a contagem que ela viu, devolvida no envio


def reduction_view(due: Due, today: Due) -> ReductionView:
    reduction = form_queries.reduction_of(due, today)
    return ReductionView(
        headline=form_retention.deletion_sentence(reduction.due),
        today=(
            f"Com os prazos de hoje, seriam {_plural(today.requests, 'pedido', 'pedidos')} e "
            f"{_plural(today.pdfs, 'arquivo de PDF', 'arquivos de PDF')} e {_plural(today.captures, 'captura', 'capturas')}."
        ),
        seen=reduction.seen,
    )


@dataclass(frozen=True)
class ProbeView:
    """O resultado do evento de teste, como a tela mostra."""

    ok: bool  # um plano.concluido com esta resposta conta como entregue
    headline: str
    note: str
    body: str | None  # o começo da resposta do receptor


UNREADABLE_PROBE = ProbeView(
    ok=False,
    headline="O evento não saiu: o segredo deste destino não abre com WEBHOOK_ENCRYPTION_KEY.",
    note="Confira a chave no ambiente do app. Se ela se perdeu, gere um segredo novo aqui e ponha "
    "no receptor.",
    body=None,
)


def probe_view(probe: Probe, *, event: str = "plano.concluido") -> ProbeView:
    """O que o receptor respondeu e o que uma entrega faria com essa resposta (as mesmas regras
    do `plano.concluido`, em `app.adapters.webhook.outcome`)."""
    event_name = (
        "evento de captura (submissao.persistida)"
        if event == "submissao.persistida"
        else "plano.concluido"
    )
    if probe.status is None:
        return ProbeView(
            ok=False,
            headline=(
                f"Não deu para falar com o receptor ({probe.error}) depois de "
                f"{probe.elapsed_ms} ms."
            ),
            note=(
                "Confira a URL, a porta e se o receptor está no ar. Um plano.concluido nessa "
                "situação seria repetido por cerca de 3,5 h."
            ).replace("plano.concluido", event_name),
            body=None,
        )
    verdict = outcome(probe.status)
    if verdict == "delivered":
        note = "Com essa resposta, um plano.concluido conta como entregue."
    elif verdict == "retry":
        note = (
            "Com essa resposta, um plano.concluido seria repetido por cerca de 3,5 h e depois "
            "marcado como falho."
        )
    elif 300 <= probe.status < 400:
        note = (
            "Redirecionamentos não são seguidos: um plano.concluido com essa resposta seria "
            "recusado de vez. Use a URL final do receptor."
        )
    else:
        note = (
            "Com essa resposta, um plano.concluido seria recusado de vez. Confira a URL e o "
            "segredo no receptor."
        )
    return ProbeView(
        ok=verdict == "delivered",
        headline=f"O receptor respondeu HTTP {probe.status} em {probe.elapsed_ms} ms.",
        note=note.replace("plano.concluido", event_name)
        + (
            " A confirmação é do transporte, sem comprovar leitura."
            if event == "submissao.persistida"
            else ""
        ),
        body=probe.body_start[:BODY_START_CHARS] or None,
    )


@dataclass(frozen=True)
class DestinationPage:
    form_slug: str | None
    title: str
    intro: str
    base: str  # a URL da página, prefixo das ações
    stored: DestinationView | None
    fallback: str | None  # sem destino próprio: o que vale para o formulário
    unreadable: bool  # o segredo guardado não abre com a chave atual
    capture_counts: Counts = field(default_factory=Counts)


def destination_page(
    session: Session,
    form_slug: str | None,
    form_title: str | None,
    cipher: destinations.SecretCipher | None,
) -> DestinationPage | None:
    """A página do destino padrão (`form_slug` None) ou do de um formulário; None se o
    formulário não existe e não tem destino guardado."""
    found = destination_queries.detail(session, form_slug, form_title, cipher)
    own = found.stored
    if form_slug is None:
        title = "Destino padrão"
        intro = "Recebe os eventos dos formulários que não têm destino próprio."
        fallback = None
    elif form_title is None and own is None:
        return None
    else:
        title = f"Destino do formulário {form_slug}"
        intro = (
            f"Recebe os eventos de “{form_title}” no lugar do destino padrão."
            if form_title
            else "Este formulário saiu do código; o destino guardado ainda pode ser removido."
        )
        if own:
            fallback = None
        elif found.serving == "default":
            fallback = (
                f"Sem destino próprio, este formulário usa o destino padrão ({found.serving_host})."
            )
        else:
            fallback = (
                "Sem destino próprio nem destino padrão: os planos deste formulário não são "
                "entregues."
            )
    return DestinationPage(
        form_slug=form_slug,
        title=title,
        intro=intro,
        base=destination_url(form_slug),
        stored=_destination_view(own) if own else None,
        fallback=fallback,
        unreadable=found.unreadable,
        capture_counts=found.capture_counts,
    )
