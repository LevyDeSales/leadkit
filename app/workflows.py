"""Workflows do Hatchet.

`generate_plan`: claim -> plan -> render -> finalize (+ mark_failed se algo falhar de vez).
`deliver`: envia uma entrega ao webhook, com backoff exponencial.
`sync_integration`: envia um pedido concluído à planilha da integração do formulário (Baserow), com
backoff exponencial; a task recebe só o id da sincronização e lê o resto do banco.
`draft_test`: o teste do rascunho de um formulário (o LLM e o PDF, sem pedido), pelo admin.

Regras que vêm do Hatchet (verificadas no SDK 1.41.1):
- entrega pelo menos uma vez: toda task é idempotente por (job, tentativa);
- os padrões `execution_timeout=60s` e `schedule_timeout=5min` são curtos demais aqui: um deploy
  pode deixar a fila sem worker e o engine cancelaria a task; por isso os valores explícitos;
- inputs e outputs ficam gravados no Hatchet: só trafegam IDs, nunca dados do lead;
- tasks síncronas rodam num pool de threads do worker (não bloqueiam o event loop).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from hatchet_sdk import (
    Context,
    DedupeViolationError,
    IdempotencyCollisionError,
    StatusBasedIdempotencyConfig,
    TTLBasedIdempotencyConfig,
)
from pydantic import BaseModel, ConfigDict

from app import jobs, pipeline
from app.deps import get_deps
from app.errors import FailureCode, as_hatchet_error, cause_summary, failure_code_from_errors
from app.forms import draft_tests
from app.hatchet_client import hatchet
from app.integrations import push as integration_push
from app.integrations import syncs
from app.plan_timeout import PLAN_EXECUTION_TIMEOUT

logger = logging.getLogger(__name__)

SCHEDULE_TIMEOUT = timedelta(hours=2)


def _task_failure(task: str, exc: Exception) -> Exception:
    """O erro que a task relança para o Hatchet, sem a mensagem original. O chamador relança com
    `from None`: o SDK grava o traceback encadeado, e a causa (um KeyError do plano, um erro do banco
    com `DETAIL: Key (...)=(...)`) pode trazer dados da lead. O diagnóstico fica no log local, só
    com o tipo e os quadros (`cause_summary`). Sem `exc_info`: ele traria a mensagem junto."""
    logger.error("task %s falhou: %s", task, cause_summary(exc))
    return as_hatchet_error(exc)


class GeneratePlanInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: uuid.UUID
    attempt: int
    run_key: str

    @classmethod
    def for_attempt(cls, job_id: uuid.UUID, attempt: int) -> GeneratePlanInput:
        return cls(job_id=job_id, attempt=attempt, run_key=f"mcc:generate_plan:{job_id}:{attempt}")


class ClaimOutput(BaseModel):
    result: jobs.ClaimResult

    @property
    def proceed(self) -> bool:
        return self.result is jobs.ClaimResult.PROCEED


class PlanOutput(BaseModel):
    plan_id: uuid.UUID | None = None
    reused: bool = False


class RenderOutput(BaseModel):
    artifact_id: uuid.UUID | None = None
    reused: bool = False


class FinalizeOutput(BaseModel):
    completed: bool
    deliveries: int = 0
    deliveries_triggered: int = 0
    syncs: int = 0
    syncs_triggered: int = 0


class FailureOutput(BaseModel):
    code: FailureCode
    failed_task: str | None = None


class DeliverInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    delivery_id: uuid.UUID
    run_key: str

    @classmethod
    def for_delivery(cls, delivery_id: uuid.UUID) -> DeliverInput:
        return cls(delivery_id=delivery_id, run_key=f"mcc:deliver:{delivery_id}")


class DeliverOutput(BaseModel):
    status: str


# --- generate_plan ----------------------------------------------------------------------------

generate_plan = hatchet.workflow(
    name="generate_plan",
    description="Respostas validadas -> plano do LLM -> PDF -> job concluído -> entregas",
    input_validator=GeneratePlanInput,
    # Deduplica disparos repetidos da mesma tentativa (recurso beta do Hatchet). A garantia real
    # é o compare-and-set do `claim` e a idempotência de cada passo.
    idempotency=TTLBasedIdempotencyConfig(key_expression="input.run_key", ttl=timedelta(days=7)),
)


@generate_plan.task(
    execution_timeout=timedelta(seconds=30),
    schedule_timeout=SCHEDULE_TIMEOUT,
    retries=3,
    backoff_factor=2.0,
    backoff_max_seconds=10,
)
def claim(input: GeneratePlanInput, ctx: Context) -> ClaimOutput:
    with get_deps().sessions.begin() as session:
        result = jobs.claim(session, input.job_id, input.attempt, ctx.workflow_run_id)
    return ClaimOutput(result=result)


@generate_plan.task(
    parents=[claim],
    # O pior caso do adaptador OpenAI (modelo principal e reserva, cada um com o pedido e um reparo)
    # são 4 chamadas de até OPENAI_TIMEOUT_SECONDS: 8 min com os 120 s do padrão, mais a folga. O
    # número e a conta ficam em app/plan_timeout.py, e `Settings.check()` recusa um timeout que
    # não cabe nele.
    execution_timeout=PLAN_EXECUTION_TIMEOUT,
    schedule_timeout=SCHEDULE_TIMEOUT,
    retries=3,
    backoff_factor=3.0,
    backoff_max_seconds=120,
)
def plan(input: GeneratePlanInput, ctx: Context) -> PlanOutput:
    if not ctx.task_output(claim).proceed:
        return PlanOutput()
    try:
        plan_id, reused = pipeline.generate_plan(get_deps(), input.job_id, input.attempt)
    except Exception as exc:  # noqa: BLE001 - relançada sem a causa, logo abaixo
        raise _task_failure("plan", exc) from None
    return PlanOutput(plan_id=plan_id, reused=reused)


@generate_plan.task(
    parents=[claim, plan],
    execution_timeout=timedelta(minutes=2),
    schedule_timeout=SCHEDULE_TIMEOUT,
    retries=2,
    backoff_factor=2.0,
    backoff_max_seconds=30,
)
def render(input: GeneratePlanInput, ctx: Context) -> RenderOutput:
    planned = ctx.task_output(plan)
    if not ctx.task_output(claim).proceed or planned.plan_id is None:
        return RenderOutput()
    try:
        artifact_id, reused = pipeline.render_pdf(
            get_deps(), input.job_id, input.attempt, planned.plan_id
        )
    except Exception as exc:  # noqa: BLE001 - relançada sem a causa, logo abaixo
        raise _task_failure("render", exc) from None
    return RenderOutput(artifact_id=artifact_id, reused=reused)


@generate_plan.task(
    parents=[claim, render],
    execution_timeout=timedelta(seconds=60),
    schedule_timeout=SCHEDULE_TIMEOUT,
    retries=3,
    backoff_factor=2.0,
    backoff_max_seconds=15,
)
def finalize(input: GeneratePlanInput, ctx: Context) -> FinalizeOutput:
    rendered = ctx.task_output(render)
    if not ctx.task_output(claim).proceed or rendered.artifact_id is None:
        return FinalizeOutput(completed=False)
    deps = get_deps()
    delivery_ids = pipeline.finalize(deps, input.job_id, input.attempt, rendered.artifact_id)
    triggered = sum(1 for delivery_id in delivery_ids if launch_delivery(delivery_id) is not None)
    sync_ids = pipeline.pending_syncs(deps, rendered.artifact_id)
    synced = sum(1 for sync_id in sync_ids if launch_sync(sync_id) is not None)
    return FinalizeOutput(
        completed=True,
        deliveries=len(delivery_ids),
        deliveries_triggered=triggered,
        syncs=len(sync_ids),
        syncs_triggered=synced,
    )


@generate_plan.on_failure_task(
    execution_timeout=timedelta(seconds=30),
    schedule_timeout=SCHEDULE_TIMEOUT,
    retries=3,
    backoff_factor=2.0,
    backoff_max_seconds=15,
)
def mark_failed(input: GeneratePlanInput, ctx: Context) -> FailureOutput:
    """Roda quando uma task esgota as tentativas. Não roda em cancelamento puro: o reconciliador
    (app.crons) cobre esse caso."""
    code, failed_task = failure_code_from_errors(ctx.task_run_errors)
    with get_deps().sessions.begin() as session:
        jobs.fail(
            session, input.job_id, input.attempt, code.value, f"{failed_task or 'task'} falhou"
        )
    return FailureOutput(code=code, failed_task=failed_task)


# --- deliver ----------------------------------------------------------------------------------

deliver = hatchet.workflow(
    name="deliver",
    description="Envia o resultado de um job para o webhook configurado",
    input_validator=DeliverInput,
    idempotency=StatusBasedIdempotencyConfig(
        key_expression="input.run_key", fallback_ttl=timedelta(hours=12)
    ),
)


@deliver.task(
    execution_timeout=timedelta(minutes=2),
    schedule_timeout=timedelta(hours=6),
    # Atrasos de 4 s, 16 s, 64 s, ~4 min, ~17 min e depois 1 h x3: cerca de 3,5 h de tolerância.
    retries=8,
    backoff_factor=4.0,
    backoff_max_seconds=3600,
)
def send(input: DeliverInput, ctx: Context) -> DeliverOutput:
    try:
        status = pipeline.deliver(get_deps(), input.delivery_id)
    except Exception as exc:  # noqa: BLE001 - relançada sem a causa, logo abaixo
        raise _task_failure("send", exc) from None
    return DeliverOutput(status=status)


@deliver.on_failure_task(retries=3, backoff_factor=2.0, backoff_max_seconds=15)
def delivery_failed(input: DeliverInput, ctx: Context) -> FailureOutput:
    code, failed_task = failure_code_from_errors(ctx.task_run_errors)
    if code is FailureCode.INTERNAL:
        code = FailureCode.DELIVERY_UNAVAILABLE
    pipeline.mark_delivery_failed(get_deps(), input.delivery_id, code.value)
    return FailureOutput(code=code, failed_task=failed_task)


# --- sync_integration -------------------------------------------------------------------------
# A sincronização de um pedido concluído com a planilha da integração do formulário
# (`app.integrations`). A linha
# de `integration_syncs` faz o papel de outbox, como `deliveries`: a task recebe só o id dela.


class SyncInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    sync_id: uuid.UUID
    run_key: str

    @classmethod
    def for_sync(cls, sync_id: uuid.UUID) -> SyncInput:
        return cls(sync_id=sync_id, run_key=f"mcc:sync_integration:{sync_id}")


class SyncOutput(BaseModel):
    status: str  # succeeded | already_succeeded


sync_integration = hatchet.workflow(
    name="sync_integration",
    description="Envia um pedido concluído à planilha da integração do formulário",
    input_validator=SyncInput,
    idempotency=StatusBasedIdempotencyConfig(
        key_expression="input.run_key", fallback_ttl=timedelta(hours=12)
    ),
)


@sync_integration.task(
    # Três chamadas ao Baserow (campos, achar a linha, escrevê-la), cada uma com até 15 s.
    execution_timeout=timedelta(minutes=2),
    schedule_timeout=timedelta(hours=6),
    # Atrasos de 4 s, 16 s, 64 s, ~4 min, ~17 min e depois 1 h x3: cerca de 3,5 h de tolerância.
    retries=8,
    backoff_factor=4.0,
    backoff_max_seconds=3600,
)
def upsert_row(input: SyncInput, ctx: Context) -> SyncOutput:
    try:
        status = integration_push.push(get_deps(), input.sync_id)
    except Exception as exc:  # noqa: BLE001 - relançada sem a causa, logo abaixo
        raise _task_failure("upsert_row", exc) from None
    return SyncOutput(status=status)


@sync_integration.on_failure_task(retries=3, backoff_factor=2.0, backoff_max_seconds=15)
def sync_failed(input: SyncInput, ctx: Context) -> FailureOutput:
    code, failed_task = failure_code_from_errors(ctx.task_run_errors)
    if code is FailureCode.INTERNAL:
        code = FailureCode.INTEGRATION_UNAVAILABLE
    with get_deps().sessions.begin() as session:
        syncs.mark_failed(session, input.sync_id, code.value)
    return FailureOutput(code=code, failed_task=failed_task)


# --- teste do rascunho ------------------------------------------------------------------------
# O teste do rascunho de um formulário (`app.forms.drafts`): o LLM e o PDF de verdade, sem envio,
# pedido nem entrega. A linha do teste no banco guarda a cópia da definição, as respostas e a
# data; a task recebe só o id e devolve só o status. Sem retries: quem tenta de novo é a operadora.


class DraftTestInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    test_id: uuid.UUID
    run_key: str

    @classmethod
    def for_test(cls, test_id: uuid.UUID) -> DraftTestInput:
        return cls(test_id=test_id, run_key=f"mcc:draft_test:{test_id}")


class DraftTestOutput(BaseModel):
    status: str  # succeeded | failed | skipped


draft_test = hatchet.workflow(
    name="draft_test",
    description="Teste do rascunho de um formulário: o LLM e o PDF, sem pedido nem entrega",
    input_validator=DraftTestInput,
    idempotency=TTLBasedIdempotencyConfig(key_expression="input.run_key", ttl=timedelta(days=1)),
)


@draft_test.task(
    # Os limites moram em `app.forms.draft_tests`, que conta o teste como interrompido logo depois.
    execution_timeout=draft_tests.RUN_TIMEOUT,
    schedule_timeout=draft_tests.QUEUE_TIMEOUT,
    retries=0,
)
def run_draft_test(input: DraftTestInput, ctx: Context) -> DraftTestOutput:
    try:
        status = draft_tests.run(get_deps(), input.test_id)
    except Exception as exc:  # noqa: BLE001 - relançada sem a causa, logo abaixo
        # `from None`: a causa (um erro do banco) pode trazer o plano ou as respostas.
        raise as_hatchet_error(exc) from None
    return DraftTestOutput(status=status)


@draft_test.on_failure_task(
    execution_timeout=timedelta(seconds=30),
    schedule_timeout=SCHEDULE_TIMEOUT,
    retries=3,
    backoff_factor=2.0,
    backoff_max_seconds=15,
)
def draft_test_failed(input: DraftTestInput, ctx: Context) -> FailureOutput:
    """A task do teste falhou de vez: o teste termina com o código já, em vez de esperar o prazo
    para contar como interrompido."""
    code, failed_task = failure_code_from_errors(ctx.task_run_errors)
    draft_tests.fail(get_deps(), input.test_id, code.value)
    return FailureOutput(code=code, failed_task=failed_task)


# --- disparo ----------------------------------------------------------------------------------


def launch_generate_plan(job_id: uuid.UUID, attempt: int) -> str | None:
    """Dispara (ou redispara) a tentativa e grava o run id. Nunca levanta; None = engine
    inacessível, e o reconciliador tenta de novo."""
    try:
        ref = generate_plan.run(
            GeneratePlanInput.for_attempt(job_id, attempt),
            wait_for_result=False,
            additional_metadata={"job_id": str(job_id), "attempt": str(attempt)},
        )
        run_id = ref.workflow_run_id
    except IdempotencyCollisionError as exc:
        run_id = exc.existing_run_external_id
    except Exception:
        logger.exception("não foi possível disparar generate_plan job=%s", job_id)
        return None
    with get_deps().sessions.begin() as session:
        jobs.set_run_id(session, job_id, attempt, run_id)
    return run_id


async def alaunch_generate_plan(job_id: uuid.UUID, attempt: int, *, timeout: float) -> str | None:
    """Versão para a API: nunca espera mais que `timeout`. Com o engine fora do ar o SDK só
    desiste depois de ~16 s (medido); o lead não pode esperar isso, o reconciliador termina o
    trabalho."""
    try:
        ref = await asyncio.wait_for(
            generate_plan.aio_run(
                GeneratePlanInput.for_attempt(job_id, attempt),
                wait_for_result=False,
                additional_metadata={"job_id": str(job_id), "attempt": str(attempt)},
            ),
            timeout=timeout,
        )
        run_id = ref.workflow_run_id
    except IdempotencyCollisionError as exc:
        run_id = exc.existing_run_external_id
    except Exception:
        logger.warning("disparo adiado para o reconciliador job=%s", job_id, exc_info=True)
        return None

    def _store() -> None:
        with get_deps().sessions.begin() as session:
            jobs.set_run_id(session, job_id, attempt, run_id)

    await asyncio.to_thread(_store)
    return run_id


async def alaunch_draft_test(test_id: uuid.UUID, *, timeout: float) -> str | None:
    """Dispara o teste do rascunho pela API, sem esperar mais que `timeout`. None: o engine não
    recebeu, e quem chamou marca o teste como não começado (`app.forms.draft_tests.not_started`)."""
    try:
        ref = await asyncio.wait_for(
            draft_test.aio_run(
                DraftTestInput.for_test(test_id),
                wait_for_result=False,
                additional_metadata={"draft_test_id": str(test_id)},
            ),
            timeout=timeout,
        )
        return ref.workflow_run_id
    except IdempotencyCollisionError as exc:
        return exc.existing_run_external_id
    except Exception:
        logger.warning("disparo do teste do rascunho falhou test=%s", test_id, exc_info=True)
        return None


def launch_delivery(delivery_id: uuid.UUID) -> str | None:
    try:
        ref = deliver.run(
            DeliverInput.for_delivery(delivery_id),
            wait_for_result=False,
            additional_metadata={"delivery_id": str(delivery_id)},
        )
        run_id = ref.workflow_run_id
    except IdempotencyCollisionError as exc:
        run_id = exc.existing_run_external_id
    except DedupeViolationError:
        return "existing"
    except Exception:
        logger.exception("não foi possível disparar deliver delivery=%s", delivery_id)
        return None
    pipeline.set_delivery_run_id(get_deps(), delivery_id, run_id)
    return run_id


def launch_sync(sync_id: uuid.UUID) -> str | None:
    """Dispara a sincronização de um pedido com a planilha e grava o run id. Nunca levanta; None = engine
    inacessível, e o reconciliador tenta de novo."""
    try:
        ref = sync_integration.run(
            SyncInput.for_sync(sync_id),
            wait_for_result=False,
            additional_metadata={"sync_id": str(sync_id)},
        )
        run_id = ref.workflow_run_id
    except IdempotencyCollisionError as exc:
        run_id = exc.existing_run_external_id
    except DedupeViolationError:
        return "existing"
    except Exception:
        logger.exception("não foi possível disparar sync_integration sync=%s", sync_id)
        return None
    with get_deps().sessions.begin() as session:
        syncs.set_run_id(session, sync_id, run_id)
    return run_id
