"""Tasks do Hatchet rodadas com `Task.mock_run` (sem engine), contra o Postgres de teste."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from conftest import record_webhook
from hatchet_sdk import NonRetryableException
from hatchet_sdk.exceptions import TaskRunError
from test_intake import submit

import app.workflows as wf
from app import jobs, pipeline
from app.deps import Deps
from app.errors import (
    FailureCode,
    RetryableError,
    TerminalError,
    as_hatchet_error,
    failure_code_from_errors,
)
from app.models import Job


def out(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")


def test_generate_plan_tasks_run_in_order_and_complete_the_job(
    deps: Deps, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_webhook(deps)
    launched: list[object] = []
    monkeypatch.setattr(
        wf, "launch_delivery", lambda delivery_id: launched.append(delivery_id) or "run-d"
    )
    job = submit(deps).job
    payload = wf.GeneratePlanInput.for_attempt(job.id, 1)

    claimed = wf.claim.mock_run(input=payload)
    assert claimed.proceed
    planned = wf.plan.mock_run(input=payload, parent_outputs={"claim": out(claimed)})
    assert planned.plan_id is not None and not planned.reused
    rendered = wf.render.mock_run(
        input=payload, parent_outputs={"claim": out(claimed), "plan": out(planned)}
    )
    assert rendered.artifact_id is not None
    finalized = wf.finalize.mock_run(
        input=payload, parent_outputs={"claim": out(claimed), "render": out(rendered)}
    )
    assert finalized.completed and finalized.deliveries == 1 and len(launched) == 1

    with deps.sessions() as session:
        assert session.get_one(Job, job.id).status == "completed"


def test_tasks_after_a_non_proceeding_claim_do_nothing(deps: Deps) -> None:
    job = submit(deps).job
    payload = wf.GeneratePlanInput.for_attempt(job.id, 2)  # tentativa que não existe
    claimed = wf.claim.mock_run(input=payload)
    assert not claimed.proceed
    assert wf.plan.mock_run(input=payload, parent_outputs={"claim": out(claimed)}).plan_id is None


def test_run_key_is_stable_per_attempt() -> None:
    a = wf.GeneratePlanInput.for_attempt(__import__("uuid").UUID(int=1), 1)
    b = wf.GeneratePlanInput.for_attempt(__import__("uuid").UUID(int=1), 1)
    c = wf.GeneratePlanInput.for_attempt(__import__("uuid").UUID(int=1), 2)
    assert a.run_key == b.run_key != c.run_key


def test_error_mapping_and_failure_code_extraction() -> None:
    terminal = as_hatchet_error(TerminalError(FailureCode.RENDER_FAILED, "template"))
    assert isinstance(terminal, NonRetryableException)
    retryable = RetryableError(FailureCode.LLM_UNAVAILABLE, "timeout")
    assert as_hatchet_error(retryable) is retryable
    unknown = as_hatchet_error(ValueError("dado do lead: fulano@example.test"))
    assert isinstance(unknown, RetryableError) and "fulano" not in str(unknown)

    errors = {"plan": "RetryableError: [plan_invalid] saída inválida\nTraceback..."}
    assert failure_code_from_errors(errors) == (FailureCode.PLAN_INVALID, "plan")
    assert failure_code_from_errors({"render": "Boom: sem código"}) == (
        FailureCode.INTERNAL,
        "render",
    )


# --- a causa do erro não vai para o Hatchet --------------------------------------------------
# O SDK grava o traceback encadeado da falha (`TaskRunError.from_exception`): com `from exc` a causa,
# e a mensagem dela, vão para o Hatchet Cloud junto.

LEAD_EMAIL = "fulano@example.test"


def leaky_step(*args: object, **kwargs: object) -> None:
    """Um passo do pipeline que falha do jeito que a regra quer fechar: a mensagem traz dado da
    lead (um KeyError do plano, um DETAIL do Postgres), e a causa vem encadeada."""
    try:
        raise KeyError(LEAD_EMAIL)
    except KeyError as cause:
        raise ValueError(f"resposta inválida: {LEAD_EMAIL}") from cause


def run_plan(job_id: uuid.UUID) -> object:
    payload = wf.GeneratePlanInput.for_attempt(job_id, 1)
    claim = wf.ClaimOutput(result=jobs.ClaimResult.PROCEED)
    return wf.plan.mock_run(input=payload, parent_outputs={"claim": out(claim)})


def run_render(job_id: uuid.UUID) -> object:
    payload = wf.GeneratePlanInput.for_attempt(job_id, 1)
    claim = wf.ClaimOutput(result=jobs.ClaimResult.PROCEED)
    planned = wf.PlanOutput(plan_id=uuid.uuid4())
    return wf.render.mock_run(
        input=payload, parent_outputs={"claim": out(claim), "plan": out(planned)}
    )


def run_send(delivery_id: uuid.UUID) -> object:
    return wf.send.mock_run(input=wf.DeliverInput.for_delivery(delivery_id))


# (nome da task, função do pipeline que ela chama, como rodar a task)
LEAKY_TASKS: list[Any] = [
    pytest.param("plan", "generate_plan", run_plan, id="plan"),
    pytest.param("render", "render_pdf", run_render, id="render"),
    pytest.param("send", "deliver", run_send, id="send"),
]


@pytest.mark.parametrize(("task", "step", "run"), LEAKY_TASKS)
def test_task_failure_serialized_by_the_sdk_has_no_lead_data(
    task: str,
    step: str,
    run: Callable[[uuid.UUID], object],
    monkeypatch: pytest.MonkeyPatch,
    deps: Deps,
) -> None:
    monkeypatch.setattr(pipeline, step, leaky_step)

    with pytest.raises(RetryableError) as raised:
        run(uuid.uuid4())

    exc = raised.value
    assert exc.code is FailureCode.INTERNAL and str(exc) == "[internal] ValueError"
    trace = TaskRunError.from_exception(exc, None).trace
    assert "RetryableError" in trace  # o traceback existe; só a causa foi cortada
    assert "fulano" not in trace and "fulano" not in str(exc)


@pytest.mark.parametrize(("task", "step", "run"), LEAKY_TASKS)
def test_task_failure_logs_only_the_type_and_frames_of_the_cause(
    task: str,
    step: str,
    run: Callable[[uuid.UUID], object],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    deps: Deps,
) -> None:
    # O `fileConfig` do alembic, que a suíte roda na subida, desliga os loggers que já existiam.
    monkeypatch.setattr(logging.getLogger("app.workflows"), "disabled", False)
    monkeypatch.setattr(pipeline, step, leaky_step)

    with caplog.at_level(logging.INFO, logger="app.workflows"), pytest.raises(RetryableError):
        run(uuid.uuid4())

    records = [r for r in caplog.records if r.name == "app.workflows"]
    assert len(records) == 1
    log = records[0].getMessage()
    assert task in log
    # a causa localizável: os tipos da cadeia e o ponto da falha (arquivo, linha e função)
    assert "ValueError" in log and "KeyError" in log
    assert "test_workflows.py" in log and "leaky_step" in log
    assert "fulano" not in caplog.text
    assert records[0].exc_info is None  # `logger.exception` traria a mensagem junto


def test_translated_terminal_error_does_not_carry_its_cause_to_the_sdk_trace(
    monkeypatch: pytest.MonkeyPatch, deps: Deps
) -> None:
    def translated(*args: object, **kwargs: object) -> None:
        try:
            raise KeyError(LEAD_EMAIL)
        except KeyError as cause:
            raise TerminalError(FailureCode.RENDER_FAILED, "template") from cause

    monkeypatch.setattr(pipeline, "generate_plan", translated)

    with pytest.raises(NonRetryableException) as raised:
        run_plan(uuid.uuid4())

    trace = TaskRunError.from_exception(raised.value, None).trace
    assert "[render_failed] template" in str(raised.value)
    assert "fulano" not in trace
