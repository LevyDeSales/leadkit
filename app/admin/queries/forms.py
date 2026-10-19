"""A consulta dos formulários, a que a página (`app.admin.views`) e a API de admin dividem: a lista, o
detalhe de um formulário e os prazos de retenção dele.

Devolve dados, não telas: os códigos do banco, instantes com fuso e nenhum rótulo. A página os decora
(rótulo em português, data de Brasília, endereço das páginas do admin) e a API os serializa em JSON.

O detalhe leva também o que a página precisa para montar a leitura da versão (`spec` e `definition`):
a leitura é apresentação e fica na página; a API entrega a definição (`get_form_version`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import destinations
from app.forms import FormRegistry, FormSpec, UnknownForm, data_notices, drafts
from app.forms import retention as form_retention
from app.forms import stored as stored_forms
from app.forms.definition import DefinitionError, compile_definition
from app.forms.definition.document import FormDocument, parse
from app.integrations import config as integration_config
from app.integrations import syncs as integration_syncs
from app.models import Submission
from app.retention import Due
from app.settings import Settings


def form_title(session: Session, slug: str) -> str | None:
    """O nome do formulário no admin, ativo ou não: o da linha dele em `forms`, que nasce do título
    da definição e a publicação mantém igual ao da versão atual. None se ele não existe."""
    row = next(iter(stored_forms.listing(session, slug)), None)
    return row.title if row is not None else None


def public_url(public_base_url: str, slug: str) -> str:
    """O endereço público do formulário, o que vai nos anúncios e nas páginas."""
    return f"{public_base_url.rstrip('/')}/p/{quote(slug, safe='')}"


def submission_counts(session: Session, slug: str | None = None) -> dict[str, int]:
    """Quantos envios cada formulário (só o do `slug`, se vier) tem no banco, pelo slug. A
    retenção apaga os antigos."""
    statement = sa.select(Submission.form_slug, sa.func.count()).group_by(Submission.form_slug)
    if slug is not None:
        statement = statement.where(Submission.form_slug == slug)
    return {form_slug: count for form_slug, count in session.execute(statement)}


def _notices(session: Session, now: datetime) -> dict[str, tuple[str, ...]]:
    return {
        slug: tuple(notice.message for notice in found)
        for slug, found in data_notices.current_notices(session, now).items()
    }


# --- a lista ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FormSummary:
    """Um formulário na lista."""

    slug: str
    title: str
    active: bool
    public_url: str
    version_number: int | None  # a versão publicada atual; None se nenhuma foi publicada
    published_at: datetime | None
    # A versão atual compila neste deploy? None: não há versão publicada.
    compiles: bool | None
    draft_pending: bool  # há mudança no rascunho que ainda não foi publicada
    notices: tuple[str, ...]  # o que conferir nos dados do formulário (`app.forms.data_notices`)
    submissions: int


def form_summaries(
    session: Session, forms: FormRegistry, *, public_base_url: str, now: datetime
) -> list[FormSummary]:
    """Todo formulário, pelo slug: os da lista de `forms`, inclusive os que ficaram fora do registro
    (a versão atual não compila mais, ou nenhuma foi publicada). `now` é o dia contra o qual os
    dados de cada formulário são conferidos."""
    compiled = {spec.slug for spec in forms.current_forms()}
    counts = submission_counts(session)
    pending = drafts.pending_slugs(session)
    notices = _notices(session, now)
    return [
        FormSummary(
            slug=row.slug,
            title=row.title,
            active=row.active,
            public_url=public_url(public_base_url, row.slug),
            version_number=row.version_number,
            published_at=row.published_at,
            compiles=(row.slug in compiled) if row.version_number else None,
            draft_pending=row.slug in pending,
            notices=notices.get(row.slug, ()),
            submissions=counts.get(row.slug, 0),
        )
        for row in stored_forms.listing(session)
    ]


# --- o detalhe ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishedVersion:
    """A versão publicada atual de um formulário."""

    number: int
    text: str | None  # o rótulo que vai no envio e no webhook; None se a versão não compila
    published_by: str
    published_at: datetime
    note: str
    fingerprint: str
    compiles: bool


@dataclass(frozen=True)
class Serving:
    """Qual destino do webhook serve o formulário: o próprio (`form`) ou o padrão (`default`), e o
    host dele. Nunca a URL: o caminho costuma valer como senha."""

    scope: str
    host: str


def serving_scope(
    stored: dict[str | None, destinations.StoredDestination], form_slug: str
) -> tuple[str, destinations.StoredDestination | None]:
    """Qual destino serve o formulário (`form`, `default` ou `none`) e qual é ele."""
    serving = destinations.serving(stored, form_slug)
    if serving is None:
        return "none", None
    return ("form" if form_slug in stored else "default"), serving


@dataclass(frozen=True)
class Retention:
    """Os prazos de retenção de um formulário: os próprios (None: o padrão do ambiente) e os
    padrões do ambiente."""

    own: form_retention.Periods
    default_pdf_days: int
    default_request_days: int

    @property
    def in_force(self) -> form_retention.InForce:
        """Os prazos que valem de fato: os próprios, com o padrão do ambiente onde falta um."""
        return form_retention.InForce(
            self.own.pdf_days if self.own.pdf_days is not None else self.default_pdf_days,
            self.own.request_days
            if self.own.request_days is not None
            else self.default_request_days,
        )

    @property
    def version(self) -> str:
        """O que quem lê viu, para recusar uma mudança feita sobre outra (`Periods.token`)."""
        return self.own.token


@dataclass(frozen=True)
class IntegrationSummary:
    """A integração do formulário com a planilha, resumida. Nunca traz o token."""

    configured: bool
    active: bool | None  # None: sem integração
    host: str | None
    table_id: int | None
    pending: int  # sincronizações à espera
    failed: int


def integration_summary(session: Session, slug: str) -> IntegrationSummary:
    stored = integration_config.load(session, slug)
    if stored is None:
        return IntegrationSummary(False, None, None, None, 0, 0)
    counts = integration_syncs.counts(session, stored.id)
    return IntegrationSummary(
        configured=True,
        active=stored.active,
        host=stored.host,
        table_id=stored.table_id,
        pending=counts.pending,
        failed=counts.failed,
    )


@dataclass(frozen=True)
class FormDetail:
    """Um formulário com tudo o que a leitura dele junta."""

    slug: str
    title: str
    active: bool
    public_url: str
    submissions: int
    published: PublishedVersion | None  # None: criado do zero ou duplicado, sem versão publicada
    history: tuple[stored_forms.VersionEntry, ...]  # as versões publicadas, a mais recente primeiro
    draft: drafts.Draft
    notices: tuple[str, ...]
    destination: Serving | None  # None: os planos não são entregues em lugar nenhum
    retention: Retention
    integration: IntegrationSummary
    # A leitura da versão mostrada (a publicada, ou a do rascunho antes da primeira publicação) é
    # montada da página, do contrato e da definição guardada; None: a versão não compila mais.
    spec: FormSpec | None
    definition: FormDocument | None

    @property
    def unpublished(self) -> bool:
        return self.published is None


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
    """O formulário com a versão publicada, a de maior número, que a leitura também monta da
    definição guardada; None se ele não existe. Um formulário ainda sem versão (criado do zero ou
    duplicado) tem a leitura do rascunho. `now` é o dia contra o qual os dados dele são conferidos."""
    row = next(iter(stored_forms.listing(session, slug)), None)
    published = stored_forms.stored_version(session, slug)
    draft = drafts.load(session, slug)
    if row is None or draft is None:
        return None
    spec: FormSpec | None
    if published is None:
        try:  # guardado, ele compilava; um deploy com o compilador mais rígido pode mudar isso
            spec = compile_definition(draft.document, version_number=draft.next_number)
        except DefinitionError:
            spec = None
    else:
        try:
            spec = forms.get(slug, published.number)
        except UnknownForm:  # a versão guardada não compila mais: o log já disse qual
            spec = None
    shown = published.definition if published is not None else draft.document
    # As datas moram na versão publicada; um formulário ainda sem versão não tem o que conferir.
    notices = (
        tuple(
            notice.message
            for notice in data_notices.data_notices(
                slug, published.definition, published_at=published.published_at, now=now
            )
        )
        if published is not None
        else ()
    )
    scope, serving = serving_scope(destinations.listing(session), slug)
    return FormDetail(
        slug=slug,
        title=row.title,
        active=row.active,
        public_url=public_url(public_base_url, slug),
        submissions=submission_counts(session, slug).get(slug, 0),
        published=None
        if published is None
        else PublishedVersion(
            number=published.number,
            text=spec.version if spec else None,
            published_by=published.published_by,
            published_at=published.published_at,
            note=published.note,
            fingerprint=published.fingerprint,
            compiles=spec is not None,
        ),
        history=tuple(stored_forms.history(session, slug)),
        draft=draft,
        notices=notices,
        destination=Serving(scope, serving.host) if serving else None,
        retention=Retention(
            form_retention.periods_of(session, slug) or form_retention.Periods(),
            default_pdf_days=retention_pdf_days,
            default_request_days=retention_days,
        ),
        integration=integration_summary(session, slug),
        spec=spec,
        definition=parse(shown) if spec else None,
    )


# --- os prazos de retenção ----------------------------------------------------------------------


@dataclass(frozen=True)
class FormRetention:
    """Os prazos de retenção de um formulário, com o que a mudança confere."""

    slug: str
    title: str
    retention: Retention
    link_ttl_days: int  # a validade do link da lead, que o prazo do PDF não pode ficar abaixo
    max_days: int


def form_retention_of(session: Session, slug: str, settings: Settings) -> FormRetention | None:
    """Os prazos de retenção do formulário; None se ele não existe."""
    row = next(iter(stored_forms.listing(session, slug)), None)
    own = form_retention.periods_of(session, slug)
    if row is None or own is None:
        return None
    return FormRetention(
        slug=slug,
        title=row.title,
        retention=Retention(
            own,
            default_pdf_days=settings.retention_pdf_days,
            default_request_days=settings.retention_days,
        ),
        link_ttl_days=settings.link_ttl_days,
        max_days=form_retention.MAX_DAYS,
    )


@dataclass(frozen=True)
class Reduction:
    """O que a próxima retenção apagaria com os prazos novos, para quem muda os prazos confirmar."""

    due: Due  # com os prazos novos
    today: Due  # com os de hoje: a diferença é o que a mudança acrescenta
    seen: str  # a contagem de `due` no formato que volta na confirmação (`seen_token`)


def reduction_of(due: Due, today: Due) -> Reduction:
    return Reduction(due=due, today=today, seen=form_retention.seen_token(due))
