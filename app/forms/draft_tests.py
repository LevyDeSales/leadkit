"""O teste do rascunho de um formulário do banco: uma chamada de verdade ao LLM com o rascunho, as
respostas que a operadora escolheu e, se ela quiser, uma data simulada, sem envio, pedido nem
entrega (ADR 0013, emenda do rascunho).

- `start` valida as respostas e a data (`app.forms.drafts.assemble`), confere a vez do formulário
  (um teste por vez, e `PAUSE` depois do anterior), guarda o teste na fila (`DraftTest`) com a
  cópia da definição, as respostas e a data, e devolve o id. Quem dispara a task `draft_test` do
  worker (`app.workflows`) com o id é a ação do admin (`app.admin.actions.draft_tests`), a mesma da
  página e da API; se o engine não recebe, `not_started`. As respostas chegam como texto JSON (a
  página) ou como objeto (a API).
- `run` é o passo da task: compila a cópia com o relógio no momento do teste (a data simulada, ou
  o clique que o começou), chama o LLM uma vez (`deps.llm`, com a rodada de reparo do adaptador) e
  gera o PDF (`deps.pdf`), sem envio, pedido, plano, PDF no storage nem entrega. Grava o plano, o
  PDF, o tempo, os tokens e os reparos no teste e devolve só o status: a task recebe e devolve só
  ids, e as falhas ficam no teste com o código e a mensagem do adaptador, que não trazem conteúdo.
  Se a task falhar de vez, `fail`.
- Um teste que passa do prazo sem terminar (`QUEUE_DEADLINE`, `RUN_DEADLINE`, um pouco acima dos
  limites da task) conta como interrompido e libera a vez. Ficam os `KEEP_TESTS` mais recentes de
  cada formulário, e a retenção apaga os de mais de `TEST_DAYS` dias (`app.retention`).
- Um teste com sucesso grava no rascunho o que ia ao LLM (`FormDraft.tested_fingerprint`): a prova
  que a publicação pede, que fica mesmo depois de o teste sair.

As respostas do teste são da operadora, fictícias; nada aqui lê dados de lead. Toda partida fica na
auditoria (`form.draft.test`), com o slug, o id e a data simulada, nunca as respostas.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.errors import FailureCode, RetryableError, TerminalError
from app.forms import UnknownForm
from app.forms import assets as form_assets
from app.forms.definition import Clock, DefinitionError, compile_definition, utc_now
from app.forms.definition.changes import llm_fingerprint
from app.forms.definition.images import AssetRef
from app.forms.definition.scoring import ScoringFailure
from app.forms.drafts import Draft, assemble, load, simulated_moment
from app.forms.sandbox import TemplateProblem
from app.models import AuditLog, DraftTest, Form, FormDraft, FormVersion

if TYPE_CHECKING:
    from app.deps import Deps

logger = logging.getLogger(__name__)

KEEP_TESTS = 5  # os testes mais recentes de cada formulário que ficam guardados
TEST_DAYS = 3  # depois disso, a retenção apaga o teste (`app.retention`)
PAUSE = timedelta(seconds=30)  # entre o fim de um teste e o começo do seguinte
# Os limites da task `draft_test` (`app.workflows`): esperar o worker e rodar. Como o `plan` do
# pipeline, o pior caso do adaptador da OpenAI, mais o PDF.
QUEUE_TIMEOUT = timedelta(minutes=5)
RUN_TIMEOUT = timedelta(minutes=9)
# Passou disso sem terminar, o teste conta como interrompido e libera a vez do formulário.
QUEUE_DEADLINE = QUEUE_TIMEOUT + timedelta(minutes=1)
RUN_DEADLINE = RUN_TIMEOUT + timedelta(minutes=1)
BUSY = "Já há um teste deste formulário em andamento. Espere ele terminar para testar de novo."
WAIT = "Espere {seconds} s para testar de novo: cada teste é uma chamada paga ao LLM."
NOT_STARTED = "o worker não recebeu o teste (o Hatchet não respondeu); tente de novo"
INTERRUPTED = "o teste passou do prazo sem terminar (o worker caiu ou reiniciou); tente de novo"
TASK_FAILED = "a task do teste falhou ({code}); tente de novo"
NO_LONGER_COMPILES = "o rascunho testado não compila mais com o código deste deploy"
SCORING_FAILED = "a pontuação não calculou com estas respostas"
# Códigos de falha próprios do teste, além dos do pipeline (`FailureCode`).
NOT_STARTED_CODE = "not_started"
INTERRUPTED_CODE = "interrupted"
IN_PROGRESS = ("queued", "running")


class DraftTestRefused(Exception):
    """O teste não começou: já há um em andamento, ou a pausa depois do último não passou.
    `retry_after` diz em quantos segundos dá para tentar de novo, quando se sabe."""

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after

    @property
    def paused(self) -> bool:
        """A recusa é a pausa depois do último teste (há quanto esperar, em `retry_after`), e não
        um teste em andamento. A página responde 429 com `Retry-After` numa e 409 na outra; a API,
        `429 test_paused` e `409 state_conflict`."""
        return self.retry_after is not None


VERSION_CONFLICT = "O rascunho salvo mudou. Confira a origem e tente novamente com a versão atual."


class DraftVersionConflict(Exception):
    """A origem esperada ou o snapshot montado deixou de ser atual, sem efeitos."""


def check_version(draft: Draft, expected_version: str | None) -> None:
    """Compara o token no próprio snapshot usado pela operação; None mantém o contrato legado."""
    if expected_version is not None and draft.token != expected_version:
        raise DraftVersionConflict(VERSION_CONFLICT)


@dataclass(frozen=True)
class DraftTestResult:
    """Um teste do rascunho e o resultado dele, como as páginas mostram. `status` é o da linha,
    menos quando o teste não terminou no prazo: aí é `interrupted`, antes e depois de outro teste
    tomar a vez dele."""

    id: uuid.UUID
    status: str  # queued | running | succeeded | failed | interrupted
    started_by: str
    started_at: datetime
    finished_at: datetime | None
    simulated_date: date | None
    definition_fingerprint: str
    llm_fingerprint: str
    answers: dict[str, Any]
    instructions: str
    prompt: str
    plan: dict[str, Any] | None
    model: str | None
    usage: dict[str, Any]
    llm_ms: int | None
    pdf_ms: int | None
    pdf_pages: int | None
    has_pdf: bool
    failure_code: str | None
    failure_detail: str | None

    @property
    def finished(self) -> bool:
        return self.status not in IN_PROGRESS


_COLUMNS = (
    DraftTest.id,
    DraftTest.status,
    DraftTest.started_by,
    DraftTest.started_at,
    DraftTest.claimed_at,
    DraftTest.finished_at,
    DraftTest.simulated_date,
    DraftTest.definition_fingerprint,
    DraftTest.llm_fingerprint,
    DraftTest.answers,
    DraftTest.instructions,
    DraftTest.prompt,
    DraftTest.plan,
    DraftTest.model,
    DraftTest.usage,
    DraftTest.llm_ms,
    DraftTest.pdf_ms,
    DraftTest.pdf_pages,
    DraftTest.pdf.is_not(None).label("has_pdf"),
    DraftTest.failure_code,
    DraftTest.failure_detail,
)


def _overdue(status: str, started_at: datetime, claimed_at: datetime | None, now: datetime) -> bool:
    if status == "queued":
        return now > started_at + QUEUE_DEADLINE
    return status == "running" and claimed_at is not None and now > claimed_at + RUN_DEADLINE


def _result(row: sa.Row[Any], now: datetime) -> DraftTestResult:
    values = row._asdict()
    claimed_at = values.pop("claimed_at")
    interrupted = values["failure_code"] == INTERRUPTED_CODE
    if interrupted or _overdue(values["status"], values["started_at"], claimed_at, now):
        values.update(
            status="interrupted", failure_code=INTERRUPTED_CODE, failure_detail=INTERRUPTED
        )
    return DraftTestResult(**values)


GATE_CURRENT = "current"  # o teste testou o rascunho como está agora
GATE_UNLOCKS = "unlocks"  # depois dele, o rascunho só mudou em partes que não vão ao LLM
GATE_STALE = "stale"  # o que vai ao LLM mudou depois dele: não vale para a publicação


def gate_of(draft: Draft, result: DraftTestResult) -> str | None:
    """O que o teste diz à publicação: só um teste com sucesso do que vai ao LLM agora a libera
    (`GATE_CURRENT`, o rascunho como está; `GATE_UNLOCKS`, mudado só em partes que não vão ao LLM) ou
    não (`GATE_STALE`). `None`: o teste não teve sucesso, e não diz nada."""
    if result.status != "succeeded":
        return None
    if result.definition_fingerprint == draft.token:
        return GATE_CURRENT
    unlocks = result.llm_fingerprint == llm_fingerprint(draft.document)
    return GATE_UNLOCKS if unlocks else GATE_STALE


def recent(session: Session, draft: Draft, *, now: datetime) -> list[DraftTestResult]:
    """Os testes guardados do formulário, o mais recente primeiro."""
    rows = session.execute(
        sa.select(*_COLUMNS)
        .where(DraftTest.form_id == draft.form_id)
        .order_by(DraftTest.started_at.desc())
    )
    return [_result(row, now) for row in rows]


def find(
    session: Session, draft: Draft, test_id: uuid.UUID, *, now: datetime
) -> DraftTestResult | None:
    """Um teste do formulário; None se ele não existe (ou já saiu pela retenção)."""
    row = session.execute(
        sa.select(*_COLUMNS).where(DraftTest.form_id == draft.form_id, DraftTest.id == test_id)
    ).one_or_none()
    return _result(row, now) if row is not None else None


def pdf_of(session: Session, draft: Draft, test_id: uuid.UUID) -> bytes | None:
    """O PDF de um teste que chegou até ele; None se não há."""
    pdf: bytes | None = session.scalar(
        sa.select(DraftTest.pdf).where(DraftTest.form_id == draft.form_id, DraftTest.id == test_id)
    )
    return pdf


def start(
    deps: Deps,
    slug: str,
    *,
    actor: str,
    answers: str | Mapping[str, Any] | None = None,
    day: str = "",
    now: datetime,
    expected_version: str | None = None,
) -> uuid.UUID:
    """Põe na fila um teste do rascunho salvo, em nome de `actor`, com as respostas (texto JSON da
    página ou objeto da API) e a data do `assemble`, e devolve o id. Quem dispara a task
    `draft_test` é a página ou a API, com o id (`app.admin.actions.draft_tests`); se o disparo
    falhar, `not_started`. Levanta `UnknownForm`, `DefinitionError` (as respostas ou a data) ou
    `DraftTestRefused` (um teste em andamento, ou a pausa depois do último); nada é gravado nesses
    casos."""
    with deps.sessions() as session:
        draft = load(session, slug)
    if draft is None:
        raise UnknownForm(slug)
    check_version(draft, expected_version)
    assembled = assemble(draft, answers=answers, day=day, now=now)
    test_id = uuid.uuid4()
    simulated = assembled.simulated
    try:
        with deps.sessions.begin() as session:
            from app.forms.continuation_contract import assert_definition_contract

            assert_definition_contract(session, draft.document)
            # A linha do formulário serializa dois cliques: o segundo vê o primeiro na fila.
            session.execute(sa.select(Form.id).where(Form.id == draft.form_id).with_for_update())
            latest = load(session, slug)
            if latest is None or latest.form_id != draft.form_id or latest.token != draft.token:
                raise DraftVersionConflict(VERSION_CONFLICT)
            check_version(latest, expected_version)
            _interrupt_overdue(session, draft.form_id, now)
            _refuse_while_busy(session, draft.form_id, now)
            _keep_latest(session, draft.form_id, KEEP_TESTS - 1)
            session.add(
                DraftTest(
                    id=test_id,
                    form_id=draft.form_id,
                    status="queued",
                    started_by=actor,
                    started_at=now,
                    simulated_date=simulated,
                    definition=draft.document,
                    definition_fingerprint=draft.token,
                    llm_fingerprint=llm_fingerprint(draft.document),
                    answers=assembled.answers.model_dump(mode="json"),
                    instructions=assembled.instructions,
                    prompt=assembled.prompt,
                    usage={},
                )
            )
            session.add(
                AuditLog(
                    actor=actor,
                    action="form.draft.test",
                    target_type="form",
                    target_id=str(draft.form_id),
                    details={
                        "form_slug": slug,
                        "test": str(test_id),
                        "fingerprint": draft.token,
                        **({"simulated_date": simulated.isoformat()} if simulated else {}),
                    },
                )
            )
    except IntegrityError:  # o outro clique passou na frente
        raise DraftTestRefused(BUSY) from None
    return test_id


def _interrupt_overdue(session: Session, form_id: uuid.UUID, now: datetime) -> None:
    """O teste que passou do prazo sem terminar deixa de ocupar a vez do formulário."""
    session.execute(
        sa.update(DraftTest)
        .where(
            DraftTest.form_id == form_id,
            sa.or_(
                sa.and_(DraftTest.status == "queued", DraftTest.started_at < now - QUEUE_DEADLINE),
                sa.and_(DraftTest.status == "running", DraftTest.claimed_at < now - RUN_DEADLINE),
            ),
        )
        .values(status="failed", failure_code=INTERRUPTED_CODE, failure_detail=INTERRUPTED)
    )


def _refuse_while_busy(session: Session, form_id: uuid.UUID, now: datetime) -> None:
    busy = session.scalar(
        sa.select(DraftTest.id).where(
            DraftTest.form_id == form_id, DraftTest.status.in_(IN_PROGRESS)
        )
    )
    if busy is not None:
        raise DraftTestRefused(BUSY)
    last = session.scalar(
        sa.select(sa.func.max(DraftTest.finished_at)).where(DraftTest.form_id == form_id)
    )
    if last is not None and now < last + PAUSE:
        seconds = math.ceil((last + PAUSE - now).total_seconds())
        raise DraftTestRefused(WAIT.format(seconds=seconds), retry_after=seconds)


def _keep_latest(session: Session, form_id: uuid.UUID, keep: int) -> None:
    """Só os `keep` testes mais recentes do formulário ficam (o novo entra em seguida)."""
    latest = (
        sa.select(DraftTest.id)
        .where(DraftTest.form_id == form_id)
        .order_by(DraftTest.started_at.desc())
        .limit(keep)
    )
    session.execute(
        sa.delete(DraftTest).where(DraftTest.form_id == form_id, DraftTest.id.not_in(latest))
    )


def not_started(deps: Deps, test_id: uuid.UUID) -> None:
    """O disparo da task falhou: o teste termina como não começado, sem pausa para o seguinte. Se
    a task chegar a rodar depois, ela não acha o teste na fila e não faz nada."""
    _end(deps, test_id, NOT_STARTED_CODE, NOT_STARTED)


def fail(deps: Deps, test_id: uuid.UUID, code: str) -> None:
    """A task do teste falhou de vez (o `on_failure` dela): o teste termina com o código, em vez
    de esperar o prazo para contar como interrompido."""
    _end(deps, test_id, code, TASK_FAILED.format(code=code))


def _end(deps: Deps, test_id: uuid.UUID, code: str, detail: str) -> None:
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(DraftTest)
            .where(DraftTest.id == test_id, DraftTest.status.in_(IN_PROGRESS))
            .values(status="failed", failure_code=code, failure_detail=detail)
        )


@dataclass
class _Outcome:
    status: str = "failed"
    plan: dict[str, Any] | None = None
    model: str | None = None
    usage: dict[str, Any] | None = None
    llm_ms: int | None = None
    pdf_ms: int | None = None
    pdf: bytes | None = None
    pdf_pages: int | None = None
    failure_code: str | None = None
    failure_detail: str | None = None

    def fail(self, code: str, detail: str) -> _Outcome:
        self.failure_code, self.failure_detail = code, detail
        return self


def _ms(since: float) -> int:
    return round((time.monotonic() - since) * 1000)


def run(deps: Deps, test_id: uuid.UUID, *, now: Clock = utc_now) -> str:
    """O teste do rascunho, como a task `draft_test` do worker o roda: compila a cópia guardada da
    definição com o relógio no momento do teste, chama o LLM (`deps.llm`) uma vez, com as rodadas
    de reparo do adaptador, e gera o PDF (`deps.pdf`). Grava o resultado no teste e devolve só o
    status: `succeeded`, `failed` ou `skipped` (o teste não estava na fila: já rodou, foi
    interrompido ou não existe). Não cria envio, pedido, plano, PDF no storage nem entrega.

    Falha do LLM ou do PDF fica no teste, com o código e a mensagem do adaptador, que não trazem
    conteúdo. Só levanta se o banco falhar, e aí sem a mensagem original, que pode trazer o
    plano."""
    try:
        with deps.sessions.begin() as session:
            claimed = session.execute(
                sa.update(DraftTest)
                .where(DraftTest.id == test_id, DraftTest.status == "queued")
                .values(status="running", claimed_at=now())
                .returning(
                    DraftTest.form_id,
                    DraftTest.definition,
                    DraftTest.answers,
                    DraftTest.simulated_date,
                    DraftTest.started_at,
                )
            ).one_or_none()
            if claimed is None:
                return "skipped"
            form_id, definition, answers, simulated, started_at = claimed
            number = session.scalar(
                sa.select(sa.func.max(FormVersion.number)).where(FormVersion.form_id == form_id)
            )
            # O PDF do teste usa as imagens de agora, como a prévia do editor.
            images = form_assets.live_refs(session, definition)
    except SQLAlchemyError as exc:
        raise RetryableError(FailureCode.INTERNAL, type(exc).__name__) from None
    # O momento do teste é o do clique, não o da task: o prompt que a página mostra é o que vai.
    moment = simulated_moment(simulated, started_at)
    outcome = _outcome(deps, test_id, definition, answers, moment, (number or 0) + 1, images)
    _finish(deps, test_id, form_id, outcome, now)
    return outcome.status


def _outcome(
    deps: Deps,
    test_id: uuid.UUID,
    definition: dict[str, Any],
    answers: dict[str, Any],
    moment: datetime,
    number: int,
    images: Mapping[str, AssetRef],
) -> _Outcome:
    outcome = _Outcome()
    try:
        spec = compile_definition(definition, version_number=number, clock=lambda: moment)
        validated = spec.answers_model.model_validate(answers)
    except (DefinitionError, ValidationError):
        return outcome.fail(FailureCode.INTERNAL.value, NO_LONGER_COMPILES)
    spec = spec.with_assets(images)
    scores: dict[str, Any] = {}
    if spec.scoring is not None:
        try:
            scores = spec.scoring.run(validated).result.scores
        except ScoringFailure as failure:  # antes do LLM: a mensagem não traz nenhuma resposta
            detail = f"{SCORING_FAILED}: {failure.path}: {failure.detail}"
            return outcome.fail(FailureCode.INTERNAL.value, detail)
    started = time.monotonic()
    try:
        result = deps.llm.generate(spec, validated)
    except (RetryableError, TerminalError) as exc:
        outcome.llm_ms = _ms(started)
        return outcome.fail(exc.code.value, exc.detail)
    except TemplateProblem as exc:
        return outcome.fail(FailureCode.INTERNAL.value, f"o prompt não montou ({exc})")
    except Exception as exc:  # noqa: BLE001 - qualquer falha vira o resultado do teste
        logger.error("teste do rascunho %s: o LLM falhou (%s)", test_id, type(exc).__name__)
        return outcome.fail(FailureCode.INTERNAL.value, f"erro inesperado ({type(exc).__name__})")
    outcome.llm_ms = _ms(started)
    outcome.plan, outcome.model, outcome.usage = result.document, result.model, result.usage
    context = {
        "lead": {"name": spec.identity(validated).name},
        "form": {"slug": spec.slug, "title": spec.title},
        "scores": scores,
        "generated_at": moment,
    }
    started = time.monotonic()
    try:
        pdf = deps.pdf.render(spec, result.document, context)
    except TerminalError as exc:
        outcome.pdf_ms = _ms(started)
        return outcome.fail(exc.code.value, exc.detail)
    except Exception as exc:  # noqa: BLE001 - qualquer falha vira o resultado do teste
        logger.error("teste do rascunho %s: o PDF falhou (%s)", test_id, type(exc).__name__)
        unexpected = f"erro inesperado ({type(exc).__name__})"
        return outcome.fail(FailureCode.RENDER_FAILED.value, unexpected)
    outcome.pdf_ms = _ms(started)
    outcome.pdf, outcome.pdf_pages, outcome.status = pdf.content, pdf.page_count, "succeeded"
    return outcome


def _finish(
    deps: Deps, test_id: uuid.UUID, form_id: uuid.UUID, outcome: _Outcome, now: Clock
) -> None:
    """Grava o resultado, se o teste ainda está rodando (não foi interrompido no meio). Um teste
    com sucesso vira a prova que a publicação pede, no rascunho, se ele ainda existe."""
    finished = now()
    try:
        with deps.sessions.begin() as session:
            tested = session.scalar(
                sa.update(DraftTest)
                .where(DraftTest.id == test_id, DraftTest.status == "running")
                .values(
                    status=outcome.status,
                    finished_at=finished,
                    plan=outcome.plan,
                    model=outcome.model,
                    usage=outcome.usage or {},
                    llm_ms=outcome.llm_ms,
                    pdf_ms=outcome.pdf_ms,
                    pdf=outcome.pdf,
                    pdf_pages=outcome.pdf_pages,
                    failure_code=outcome.failure_code,
                    failure_detail=outcome.failure_detail,
                )
                .returning(DraftTest.llm_fingerprint)
            )
            if tested is not None and outcome.status == "succeeded":
                session.execute(
                    sa.update(FormDraft)
                    .where(FormDraft.form_id == form_id)
                    .values(tested_fingerprint=tested, tested_at=finished)
                )
    except SQLAlchemyError as exc:
        raise RetryableError(FailureCode.INTERNAL, type(exc).__name__) from None
