"""A página da integração de um formulário com o Baserow, já no formato da tela.

Decora a consulta (`app.admin.queries.integrations`, que a API de admin também usa) com o que é só da
tela: o nome do tipo em português, as fontes por grupo, o selo da situação e as datas de Brasília. Monta
a conexão, o mapeamento (uma linha por campo da tabela, com as fontes que servem ao tipo dele), o
relatório de antes de ativar e o estado das sincronizações. Nada daqui fala com o Baserow nem com o
banco. O token não passa por aqui; a tela só diz se o guardado abre.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import quote

from app.admin.queries.integrations import FieldChoice, IntegrationDetail
from app.admin.views import FORMS_URL, Badge, Moment, SyncView, moment, sync_view
from app.capture_deliveries import Counts
from app.integrations import mapping, push, syncs
from app.integrations.config import StoredIntegration
from app.integrations.mapping import Report, Source

STATE_BADGES = {
    "active": Badge("Ativa", "check", "ok"),
    "inactive": Badge("Desativada", "x", "neutral"),
}


def integration_url(slug: str) -> str:
    """A página da integração; as ações dela são esta URL + /connect, /mapping, /test, /activate,
    /deactivate, /remove e /resend-failed."""
    return f"{FORMS_URL}/{quote(slug, safe='')}/baserow"


@dataclass(frozen=True)
class SourceOption:
    key: str
    label: str


@dataclass(frozen=True)
class SourceGroup:
    label: str
    options: list[SourceOption]


@dataclass(frozen=True)
class FieldRow:
    """Um campo da tabela no mapeamento: o que o preenche agora e as fontes que servem ao tipo
    dele. `disabled` é o motivo de a integração não poder preenchê-lo."""

    id: int
    name: str
    type_label: str
    primary: bool
    disabled: str | None
    groups: list[SourceGroup]
    selected: str  # a chave da fonte escolhida; "" = nada
    suggested: bool  # a escolha vem do nome do campo, ainda não foi salva


def _type_label(kind: str) -> str:
    return mapping.TYPE_LABELS.get(kind, kind)


def field_rows(choices: Sequence[FieldChoice], sources: Mapping[str, Source]) -> list[FieldRow]:
    """Uma linha de tela por campo da tabela: o rótulo do tipo e as fontes que servem a ele, por grupo
    (`sources` é o catálogo da versão publicada, pela chave)."""
    return [
        FieldRow(
            id=choice.id,
            name=choice.name,
            type_label=_type_label(choice.type),
            primary=choice.primary,
            disabled=mapping.reason_text(choice.unsupported, choice.type),
            groups=[
                SourceGroup(group, [SourceOption(s.key, s.label) for s in found])
                for group, found in mapping.grouped({key: sources[key] for key in choice.sources})
            ],
            selected=choice.selected,
            suggested=choice.suggested,
        )
        for choice in choices
    ]


@dataclass(frozen=True)
class PairView:
    label: str  # a opção do formulário
    baserow: str | None  # a opção do Baserow que ela alcança; None: sem par


@dataclass(frozen=True)
class FieldReportView:
    name: str
    type_label: str
    source: str
    pairs: list[PairView]

    @property
    def unmatched(self) -> list[PairView]:
        return [pair for pair in self.pairs if pair.baserow is None]


@dataclass(frozen=True)
class ReportView:
    ok: bool
    problems: list[str]
    fields: list[FieldReportView]
    unmatched: int


def report_view(report: Report) -> ReportView:
    return ReportView(
        ok=report.ok,
        problems=list(report.problems),
        fields=[
            FieldReportView(
                name=item.entry.name,
                type_label=_type_label(item.entry.type),
                source=item.source,
                pairs=[
                    PairView(pair.label, pair.baserow.value if pair.baserow else None)
                    for pair in item.pairs
                ],
            )
            for item in report.fields
        ],
        unmatched=report.unmatched,
    )


@dataclass(frozen=True)
class ConnectionView:
    """A conexão guardada como a tela a mostra: nunca o token, só se ele abre e desde quando."""

    base_url: str
    host: str
    table_id: int
    version: int
    created_by: str
    created: Moment | None
    updated_by: str
    updated: Moment | None
    token_changed: Moment | None
    token_unreadable: bool


@dataclass(frozen=True)
class SampleView:
    """O resultado do envio de teste."""

    ok: bool
    headline: str
    note: str


@dataclass(frozen=True)
class IntegrationPage:
    form_slug: str
    form_title: str
    url: str  # a página, prefixo das ações
    form_url: str
    published: bool  # o formulário tem versão publicada: sem ela não há perguntas para mapear
    stored: ConnectionView | None
    active: bool
    state: Badge | None
    fields: list[FieldRow] | None  # None: sem conexão, ou a tabela não foi lida
    read_error: str | None  # por que a tabela não foi lida
    report: ReportView | None
    mapped: int  # quantos campos o mapeamento guardado preenche
    counts: syncs.SyncCounts
    failures: list[SyncView]
    profile: str = "pdf"
    capture_mapped: int = 0
    pdf_mapped: int = 0
    capture_deliveries: tuple[dict, ...] = ()
    capture_counts: Counts = field(default_factory=Counts)


def connection_view(stored: StoredIntegration, *, token_unreadable: bool) -> ConnectionView:
    return ConnectionView(
        base_url=stored.base_url,
        host=stored.host,
        table_id=stored.table_id,
        version=stored.version,
        created_by=stored.created_by,
        created=moment(stored.created_at),
        updated_by=stored.updated_by,
        updated=moment(stored.updated_at),
        token_changed=moment(stored.token_changed_at),
        token_unreadable=token_unreadable,
    )


def integration_page(detail: IntegrationDetail) -> IntegrationPage:
    """A página com o que a consulta leu: a conexão, os campos da tabela de agora (ou `read_error`), o
    relatório e o estado das sincronizações."""
    stored = detail.stored
    return IntegrationPage(
        form_slug=detail.slug,
        form_title=detail.title,
        url=integration_url(detail.slug),
        form_url=f"{FORMS_URL}/{quote(detail.slug, safe='')}",
        published=detail.published,
        stored=connection_view(stored, token_unreadable=detail.token_unreadable)
        if stored
        else None,
        active=bool(stored and stored.active),
        state=(STATE_BADGES["active" if stored.active else "inactive"] if stored else None),
        fields=field_rows(detail.fields, detail.sources) if detail.fields is not None else None,
        read_error=detail.read_error,
        report=report_view(detail.report) if detail.report else None,
        mapped=len(stored.capture_entries if detail.profile == "capture" else stored.entries)
        if stored
        else 0,
        profile=detail.profile,
        capture_deliveries=detail.capture_deliveries,
        capture_counts=detail.capture_counts,
        capture_mapped=len(stored.capture_entries) if stored else 0,
        pdf_mapped=len(stored.entries) if stored else 0,
        counts=detail.counts,
        failures=[sync_view(row) for row in detail.failures],
    )


SAMPLE_OUTCOMES = {
    "created": "A linha de teste foi criada",
    "updated": "A linha de teste foi atualizada",
}


def sample_view(result: push.SampleResult) -> SampleView:
    """O resultado do envio de teste, como a tela o conta."""
    if result.ok:
        return SampleView(
            ok=True,
            headline=f"{SAMPLE_OUTCOMES[result.outcome or 'created']} no Baserow (linha {result.row_id}).",
            note=(
                f"O código do pedido dela é «{result.public_id}». Confira a linha na tabela: "
                "testar de novo atualiza esta mesma linha, e o app não a apaga."
            ),
        )
    return SampleView(
        ok=False,
        headline="O envio de teste não passou.",
        note=result.error or "Motivo não registrado.",
    )
