"""A fronteira Hatchet do PDF trafega somente referências e contagens seguras."""

import json
import logging
from uuid import uuid4

from conftest import record_webhook
from hatchet_sdk.context.context import Context
from test_pipeline import run_until_finalize

from app import workflows

SENTINEL = "SYNTHETIC_PRIVATE_PROVIDER_PAYLOAD"


def test_pdf_task_outputs_distinguish_real_send_from_duplicate_and_old_callback(deps, monkeypatch):
    record_webhook(deps)
    _, deliveries = run_until_finalize(deps)
    input = workflows.DeliverInput.for_delivery(deliveries[0])
    sent = workflows.send.mock_run(input=input)
    assert sent.model_dump() == {
        "delivery_id": input.delivery_id,
        "delivered_count": 1,
        "skipped_count": 0,
        "failed_count": 0,
    }
    duplicate = workflows.send.mock_run(input=input)
    assert duplicate.model_dump() == {
        "delivery_id": input.delivery_id,
        "delivered_count": 0,
        "skipped_count": 1,
        "failed_count": 0,
    }
    monkeypatch.setattr(
        Context,
        "task_run_errors",
        property(lambda self: {SENTINEL: "[delivery_unavailable] " + SENTINEL}),
    )
    callback = workflows.delivery_failed.mock_run(input=input)
    assert callback.model_dump() == {
        "delivery_id": input.delivery_id,
        "delivered_count": 0,
        "skipped_count": 1,
        "failed_count": 0,
    }
    assert SENTINEL not in json.dumps(callback.model_dump(mode="json"))


def test_current_failure_counts_only_the_transition_that_it_persisted(deps, monkeypatch):
    record_webhook(deps)
    _, deliveries = run_until_finalize(deps)
    input = workflows.DeliverInput.for_delivery(deliveries[0])
    monkeypatch.setattr(
        Context,
        "task_run_errors",
        property(lambda self: {"send": "[delivery_unavailable] " + SENTINEL}),
    )
    first = workflows.delivery_failed.mock_run(input=input)
    second = workflows.delivery_failed.mock_run(input=input)
    assert (first.failed_count, first.skipped_count) == (1, 0)
    assert (second.failed_count, second.skipped_count) == (0, 1)


def test_trigger_failure_logs_only_delivery_reference_and_epoch(deps, monkeypatch, caplog):
    monkeypatch.setattr(logging.getLogger("app.workflows"), "disabled", False)
    caplog.set_level(logging.WARNING)
    record_webhook(deps)
    _, deliveries = run_until_finalize(deps)

    def fail(*args, **kwargs):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(workflows.deliver, "run", fail)
    assert workflows.launch_delivery(deliveries[0]) is None
    assert SENTINEL not in caplog.text
    records = [record for record in caplog.records if record.name == "app.workflows"]
    assert records and all(record.exc_info is None for record in records)


def test_provider_error_details_never_reach_hatchet_serialized_error(deps, monkeypatch, caplog):
    import pytest
    from hatchet_sdk.exceptions import TaskRunError

    from app import pipeline
    from app.errors import FailureCode, RetryableError

    def fail(*args, **kwargs):
        raise RetryableError(FailureCode.DELIVERY_UNAVAILABLE, SENTINEL)

    monkeypatch.setattr(pipeline, "deliver", fail)
    with pytest.raises(RetryableError) as raised:
        workflows.send.mock_run(input=workflows.DeliverInput.for_delivery(uuid4()))
    assert SENTINEL not in TaskRunError.from_exception(raised.value, None).trace
    assert SENTINEL not in caplog.text


def test_provider_details_are_not_persisted_or_returned_by_admin_queries(deps):
    from dataclasses import asdict

    import pytest

    from app import pipeline
    from app.admin.queries.jobs import read_job
    from app.errors import FailureCode, RetryableError
    from app.models import Delivery, Job, utcnow

    record_webhook(deps, RetryableError(FailureCode.DELIVERY_UNAVAILABLE, SENTINEL))
    job_id, deliveries = run_until_finalize(deps)
    with pytest.raises(RetryableError):
        pipeline.deliver(deps, deliveries[0])
    with deps.sessions() as session:
        persisted = session.get_one(Delivery, deliveries[0]).last_error
        job = session.get_one(Job, job_id)
        internal = read_job(
            session,
            job.public_id,
            forms=deps.forms,
            now=utcnow(),
            with_lead=True,
            storage=deps.storage,
        )
    assert persisted == "[delivery_unavailable] falha no transporte da entrega PDF"
    assert SENTINEL not in json.dumps(asdict(internal), default=str)
