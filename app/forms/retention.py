"""Os prazos de retenção de cada formulário (ADR 0011, ticket 13): o do arquivo do PDF e o do pedido
inteiro, definidos no admin e guardados no registro do formulário (`forms`), não na versão.

- Moram no registro porque são política de operação, não conteúdo: mudar um prazo não pede
  rascunho, teste nem publicação, vale na próxima retenção (`app.retention`) e vale também para os
  pedidos que já existem.
- Sem prazo próprio (nulo), vale o padrão do ambiente (`RETENTION_PDF_DAYS`, `RETENTION_DAYS`).
- A regra é a do ambiente, e a função é a mesma (`app.settings.retention_problem`): o PDF não dura
  menos que o link da lead, e o pedido não dura menos que o PDF. A conferência olha os prazos que
  valem de fato, os próprios com o padrão onde falta um.
- Reduzir o prazo que vale, o próprio ou o padrão a que o formulário volta, apaga dados na próxima
  execução. `save` só grava depois de a operadora ver quanto vai sair (`ConfirmationNeeded`, com a
  contagem de `app.retention.due_for_form`) e confirmar essa contagem: se ela cresceu entre a tela
  e o clique, a tela volta com o número novo.
- Toda mudança vai para a auditoria (`form.retention`), em nome de quem a fez, com os prazos de
  antes e de depois, os próprios e os que valem, e a contagem confirmada de uma redução. Nunca
  guarda dado de lead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.forms import UnknownForm
from app.models import AuditLog, Form
from app.retention import Due, due_for_form
from app.settings import PeriodNames, Settings, retention_problem

MAX_DAYS = 3650  # o teto de RETENTION_DAYS e de RETENTION_PDF_DAYS (app.settings)
AUDIT_ACTION = "form.retention"
# Como a tela chama cada prazo nas mensagens de `retention_problem`.
PERIOD_NAMES = PeriodNames(
    link="a validade do link da lead",
    pdf="o prazo do PDF",
    request="o prazo do pedido",
    unit=" dias",
)
INVALID_DAYS = (
    "Informe um número inteiro de dias, de 1 a {max}, ou deixe em branco para usar o padrão do "
    "ambiente."
)
REQUEST_IS_THE_DEFAULT = (
    " Esse é o padrão do ambiente: defina também um prazo próprio para o pedido."
)


@dataclass(frozen=True)
class Periods:
    """Os prazos próprios de um formulário, em dias; None: o padrão do ambiente."""

    pdf_days: int | None = None
    request_days: int | None = None

    @property
    def token(self) -> str:
        """O que a página aberta viu, para recusar uma mudança feita sobre outra."""
        return f"{self.pdf_days or ''}:{self.request_days or ''}"


class InForce(NamedTuple):
    """Os prazos que valem de fato: os próprios, com o padrão do ambiente onde falta um."""

    pdf_days: int
    request_days: int


def in_force(periods: Periods, settings: Settings) -> InForce:
    return InForce(
        periods.pdf_days if periods.pdf_days is not None else settings.retention_pdf_days,
        periods.request_days if periods.request_days is not None else settings.retention_days,
    )


def parse_days(text: str) -> int | None:
    """Os dias digitados; None se o campo veio em branco (o padrão do ambiente). Levanta
    `ValueError` com a mensagem para a tela se não for um inteiro de 1 a `MAX_DAYS`."""
    text = text.strip()
    if not text:
        return None
    if not (text.isascii() and text.isdecimal()) or not 1 <= int(text) <= MAX_DAYS:
        raise ValueError(INVALID_DAYS.format(max=MAX_DAYS))
    return int(text)


def periods_of(session: Session, slug: str) -> Periods | None:
    """Os prazos próprios do formulário; None se ele não existe."""
    row = session.execute(
        sa.select(Form.retention_pdf_days, Form.retention_days).where(Form.slug == slug)
    ).one_or_none()
    return Periods(*row) if row is not None else None


def seen_token(due: Due) -> str:
    """A contagem que a tela mostrou, para voltar no envio de confirmação."""
    return f"{due.requests}:{due.pdfs}"


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def deletion_sentence(due: Due) -> str:
    """O que a próxima retenção apagaria com os prazos, por extenso: é o que a tela mostra e o que a
    API diz quando pede a confirmação de uma redução."""
    parts = []
    if due.requests:
        parts.append(_plural(due.requests, "pedido inteiro", "pedidos inteiros"))
    if due.pdfs:
        parts.append(
            f"{_plural(due.pdfs, 'arquivo de PDF', 'arquivos de PDF')} dos pedidos que ficam"
        )
    if parts:
        return f"A próxima retenção vai apagar {' e '.join(parts)}."
    return "A próxima retenção não vai apagar nada deste formulário."


def parse_seen(text: str) -> Due | None:
    """A contagem de `seen_token`; None se o texto não é uma (a confirmação não vale)."""
    requests, _, pdfs = text.partition(":")
    if not (requests.isascii() and requests.isdecimal() and pdfs.isascii() and pdfs.isdecimal()):
        return None
    return Due(int(requests), int(pdfs))


class Refused(Exception):
    """Os prazos não servem: a mensagem de cada campo (`pdf_days`, `request_days`)."""

    def __init__(self, problems: dict[str, str]) -> None:
        super().__init__()
        self.problems = problems


class Changed(Exception):
    """Os prazos mudaram desde que a página foi aberta (outra aba, outra pessoa)."""


class ConfirmationNeeded(Exception):
    """O prazo que vale diminui: `due` é o que a próxima retenção apagaria com os prazos novos, e
    `today` o que apagaria com os de hoje (a diferença é o que a mudança acrescenta)."""

    def __init__(self, due: Due, today: Due) -> None:
        super().__init__()
        self.due = due
        self.today = today


@dataclass(frozen=True)
class Saved:
    changed: bool  # False: os prazos já eram esses, e nada foi gravado
    periods: Periods


def save(
    sessions: sessionmaker[Session],
    settings: Settings,
    slug: str,
    *,
    new: Periods,
    expected: str,
    actor: str,
    now: datetime,
    confirmed: Due | None = None,
) -> Saved:
    """Grava os prazos do formulário em nome de `actor`, com uma entrada na auditoria.

    `expected` é o `token` dos prazos que a página mostrava. `now` é o dia em que a contagem de
    uma redução é feita. `confirmed` é a contagem que a operadora viu e confirmou.

    Levanta `UnknownForm`; `Changed` se os prazos já não são os da página; `Refused` se não passam
    na regra de `app.settings.retention_problem`; `ConfirmationNeeded` se o prazo que vale diminui
    e `confirmed` não cobre o que a próxima retenção apagaria. Em qualquer uma, nada é gravado."""
    with sessions.begin() as session:
        form = session.scalar(sa.select(Form).where(Form.slug == slug).with_for_update())
        if form is None:
            raise UnknownForm(slug)
        current = Periods(form.retention_pdf_days, form.retention_days)
        if current.token != expected:
            raise Changed
        after = in_force(new, settings)
        problem = retention_problem(
            link_ttl_days=settings.link_ttl_days,
            pdf_days=after.pdf_days,
            request_days=after.request_days,
            names=PERIOD_NAMES,
        )
        if problem is not None:
            message = problem.message
            if problem.field == "request" and new.request_days is None:
                message += REQUEST_IS_THE_DEFAULT
            raise Refused({f"{problem.field}_days": message})
        if new == current:
            return Saved(False, current)
        before = in_force(current, settings)
        reduced = after.pdf_days < before.pdf_days or after.request_days < before.request_days
        due = None
        if reduced:
            due = due_for_form(session, slug, **_days_of(after), now=now)
            if confirmed is None or due.requests > confirmed.requests or due.pdfs > confirmed.pdfs:
                today = due_for_form(session, slug, **_days_of(before), now=now)
                raise ConfirmationNeeded(due, today)
        form.retention_pdf_days = new.pdf_days
        form.retention_days = new.request_days
        session.add(
            AuditLog(
                actor=actor,
                action=AUDIT_ACTION,
                target_type="form",
                target_id=str(form.id),
                details={
                    "form_slug": slug,
                    "pdf_days": new.pdf_days,
                    "request_days": new.request_days,
                    "previous_pdf_days": current.pdf_days,
                    "previous_request_days": current.request_days,
                    "in_force_pdf_days": after.pdf_days,
                    "in_force_request_days": after.request_days,
                    **(
                        {"confirmed_requests": confirmed.requests, "confirmed_pdfs": confirmed.pdfs}
                        if due is not None and confirmed is not None
                        else {}
                    ),
                },
            )
        )
    return Saved(True, new)


def _days_of(periods: InForce) -> dict[str, int]:
    return {"pdf_days": periods.pdf_days, "request_days": periods.request_days}
