"""Central de integrações: fatos locais, sem credenciais nem consulta aos provedores.

O escopo e o slug são separados: um formulário chamado ``default`` não é o receptor padrão.
Configurações são carregadas uma vez e o histórico se correlaciona por identidade persistida.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.destinations import SecretCipher, host_of
from app.forms import stored as stored_forms
from app.forms.definition import DefinitionError, compile_definition
from app.integration_readiness import problems_for, profile_for
from app.models import (
    CaptureDelivery,
    Delivery,
    Integration,
    IntegrationSync,
    Job,
    Submission,
    WebhookDestination,
)

Connector = Literal["baserow", "webhook"]
ScopeKind = Literal["default", "form"]
Profile = Literal["pdf", "capture"]


class UnknownFormFilter(ValueError):
    """O filtro não identifica formulário persistido nem configuração órfã conhecida."""


@dataclass(frozen=True)
class HubForm:
    slug: str
    title: str | None
    exists: bool
    active: bool | None
    published_version: int | None
    profile: Profile | None


@dataclass(frozen=True)
class Receiver:
    scope_kind: ScopeKind
    form_slug: str | None
    version: str
    host: str
    table_id: int | None
    purpose: Literal["internal", "participant"]
    purpose_origin: Literal["legacy", "explicit", "default"]
    events: list[Literal["plano.concluido", "submissao.persistida", "plano.liberado"]]
    active: bool | None
    credential_unreadable: bool


@dataclass(frozen=True)
class MappingState:
    pdf_present: bool
    capture_present: bool


@dataclass(frozen=True)
class FormIssue:
    path: str
    message: str


@dataclass
class Counts:
    pending: int = 0
    succeeded: int = 0
    failed: int = 0


@dataclass(frozen=True)
class LastSuccess:
    at: datetime
    stream: Profile
    configuration_match: Literal["current", "previous", "unknown"]


@dataclass
class History:
    pdf: Counts = field(default_factory=Counts)
    capture: Counts = field(default_factory=Counts)
    last_success: LastSuccess | None = None


@dataclass(frozen=True)
class HubRow:
    key: str
    connector: Connector
    scope_kind: ScopeKind
    form_slug: str | None
    form: HubForm | None
    configured: bool
    serving: Literal["own", "default", "none"]
    receiver: Receiver | None
    mapping: MappingState | None
    form_issues: list[FormIssue]
    history: History = field(default_factory=History)
    provider_verification: Literal["not_checked"] = "not_checked"


@dataclass(frozen=True)
class Capability:
    connector: Connector
    configured: int
    active: int | None
    served_forms: int


@dataclass(frozen=True)
class Overview:
    catalog: list[Capability]
    items: list[HubRow]
    forms: list[HubForm]


def _receiver(row, cipher: SecretCipher | None) -> Receiver:
    baserow = isinstance(row, Integration)
    sealed = row.token_sealed if baserow else row.secret_sealed
    return Receiver(
        scope_kind="default" if row.form_slug is None else "form",
        form_slug=row.form_slug,
        version=str(row.version),
        host=host_of(row.base_url if baserow else row.url),
        table_id=row.table_id if baserow else None,
        purpose=row.purpose,
        purpose_origin=row.purpose_origin,
        events=["plano.concluido", "submissao.persistida"] if baserow else row.events,
        active=row.active if baserow else None,
        credential_unreadable=cipher is None or not cipher.opens(sealed),
    )


def overview(
    session: Session,
    *,
    cipher: SecretCipher | None,
    connector: Connector | None = None,
    form_slug: str | None = None,
) -> Overview:
    """Associações globais e filtradas; ausência de publicação não omite um formulário."""
    integrations = {r.form_slug: r for r in session.scalars(sa.select(Integration))}
    destinations = {r.form_slug: r for r in session.scalars(sa.select(WebhookDestination))}
    histories = _histories(session, integrations, destinations)
    stored = {r.slug: r for r in stored_forms.listing(session)}
    slugs = sorted(set(stored) | set(integrations) | {s for s in destinations if s is not None})
    if form_slug is not None and form_slug not in slugs:
        raise UnknownFormFilter
    forms = []
    rows = []
    default = destinations.get(None)
    rows.append(
        HubRow(
            "webhook:default",
            "webhook",
            "default",
            None,
            None,
            default is not None,
            "own" if default else "none",
            _receiver(default, cipher) if default else None,
            None,
            [],
            histories.get(("webhook", default.id, None), History()) if default else History(),
        )
    )
    for slug in slugs:
        record = stored.get(slug)
        profile = None
        spec = None
        issues = []
        if record and record.version_number is not None:
            version = stored_forms.stored_version(session, slug, record.version_number)
            try:
                spec = compile_definition(version.definition, version_number=version.number)
                profile = profile_for(spec)
            except DefinitionError:
                issues.append(
                    FormIssue("form", "A versão publicada precisa de revisão; perfil indisponível.")
                )
        elif record:
            issues.append(FormIssue("form", "Publique uma versão para conectar e mapear."))
        else:
            issues.append(FormIssue("form", "Formulário não disponível; configuração preservada."))
        form = HubForm(
            slug,
            record.title if record else None,
            record is not None,
            record.active if record else None,
            record.version_number if record else None,
            profile,
        )
        forms.append(form)
        integration = integrations.get(slug)
        own = destinations.get(slug)
        effective = own or default
        rows.extend(
            [
                HubRow(
                    f"baserow:form:{slug}",
                    "baserow",
                    "form",
                    slug,
                    form,
                    integration is not None,
                    "own" if integration else "none",
                    _receiver(integration, cipher) if integration else None,
                    MappingState(bool(integration.mapping), bool(integration.capture_mapping))
                    if integration
                    else None,
                    list(issues)
                    + (
                        [
                            FormIssue(p.path, p.message)
                            for p in problems_for(
                                spec,
                                destination=None,
                                integrations=[integration] if integration else [],
                            )
                        ]
                        if spec
                        else []
                    ),
                    histories.get(("baserow", integration.id, slug), History())
                    if integration
                    else History(),
                ),
                HubRow(
                    f"webhook:form:{slug}",
                    "webhook",
                    "form",
                    slug,
                    form,
                    own is not None,
                    "own" if own else "default" if default else "none",
                    _receiver(effective, cipher) if effective else None,
                    None,
                    list(issues)
                    + (
                        [
                            FormIssue(p.path, p.message)
                            for p in problems_for(spec, destination=effective, integrations=[])
                        ]
                        if spec
                        else []
                    ),
                    histories.get(("webhook", effective.id, slug), History())
                    if effective
                    else History(),
                ),
            ]
        )
    catalog = [
        Capability(
            "baserow",
            len(integrations),
            sum(r.active for r in integrations.values()),
            sum(f.slug in integrations for f in forms if f.exists),
        ),
        Capability(
            "webhook",
            len(destinations),
            None,
            sum(f.slug in destinations or default is not None for f in forms if f.exists),
        ),
    ]
    rows.sort(key=lambda row: (row.connector, row.scope_kind != "default", row.form_slug or ""))
    return Overview(
        catalog,
        [
            r
            for r in rows
            if (connector is None or r.connector == connector)
            and (form_slug is None or r.form_slug == form_slug)
        ],
        forms,
    )


def _histories(session, integrations, destinations):
    """Agrega só contagens e instantes; payloads, erros e identificadores externos não são lidos."""
    histories = {}
    versions = {("baserow", r.id): r.version for r in integrations.values()}
    versions.update({("webhook", r.id): r.version for r in destinations.values()})

    def collect(connector, stream, records):
        for receiver_id, slug, status, revision, count, at in records:
            # O resumo do padrão é compartilhado; não é somado às linhas dos formulários.
            scopes = [slug]
            if (
                connector == "webhook"
                and destinations.get(None) is not None
                and receiver_id == destinations[None].id
            ):
                scopes.append(None)
            for scope in scopes:
                history = histories.setdefault((connector, receiver_id, scope), History())
                counts = getattr(history, stream)
                setattr(counts, status, getattr(counts, status) + count)
                if at is not None:
                    match = (
                        "unknown"
                        if revision is None
                        else "current"
                        if revision == versions[(connector, receiver_id)]
                        else "previous"
                    )
                    success = LastSuccess(at.astimezone(UTC), stream, match)
                    previous = history.last_success
                    if previous is None or (
                        success.at,
                        success.stream,
                        success.configuration_match,
                    ) > (previous.at, previous.stream, previous.configuration_match):
                        history.last_success = success

    def statement(model, receiver, revision, instant):
        return sa.select(
            receiver,
            Submission.form_slug,
            model.status,
            revision,
            sa.func.count(),
            sa.func.max(sa.case((model.status == "succeeded", instant))),
        ).group_by(receiver, Submission.form_slug, model.status, revision)

    integration_ids = [r.id for r in integrations.values()]
    destination_ids = [r.id for r in destinations.values()]
    collect(
        "baserow",
        "pdf",
        session.execute(
            statement(
                IntegrationSync,
                IntegrationSync.integration_id,
                sa.literal(None),
                IntegrationSync.synced_at,
            )
            .select_from(IntegrationSync)
            .join(Job, Job.id == IntegrationSync.job_id)
            .join(Submission, Submission.id == Job.submission_id)
            .where(IntegrationSync.integration_id.in_(integration_ids))
        ),
    )
    collect(
        "webhook",
        "pdf",
        session.execute(
            statement(
                Delivery,
                Delivery.webhook_destination_id,
                Delivery.config_version,
                Delivery.delivered_at,
            )
            .select_from(Delivery)
            .join(Job, Job.id == Delivery.job_id)
            .join(Submission, Submission.id == Job.submission_id)
            .where(Delivery.webhook_destination_id.in_(destination_ids))
        ),
    )
    for connector, receiver, ids in (
        ("baserow", CaptureDelivery.integration_id, integration_ids),
        ("webhook", CaptureDelivery.webhook_destination_id, destination_ids),
    ):
        collect(
            connector,
            "capture",
            session.execute(
                statement(
                    CaptureDelivery,
                    receiver,
                    CaptureDelivery.config_version,
                    CaptureDelivery.delivered_at,
                )
                .select_from(CaptureDelivery)
                .join(Submission, Submission.id == CaptureDelivery.submission_id)
                .where(receiver.in_(ids))
            ),
        )
    return histories
