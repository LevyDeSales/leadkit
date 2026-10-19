from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import replace

import httpx
import pytest
import sqlalchemy as sa
from conftest import publish, record_webhook
from test_intake import submit

from app import jobs, pipeline
from app.adapters.storage import LocalStorage
from app.adapters.webhook import WebhookSender
from app.deps import Deps
from app.errors import FailureCode, RetryableError, TerminalError
from app.forms import FormSpec
from app.models import Artifact, Delivery, Job, Plan


class CountingLlm:
    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.calls = 0

    def generate(self, spec, answers):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.inner.generate(spec, answers)  # type: ignore[attr-defined]


def run_until_finalize(deps: Deps) -> tuple[uuid.UUID, list[uuid.UUID]]:
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "run-1")
    plan_id, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact_id, _ = pipeline.render_pdf(deps, job.id, 1, plan_id)
    return job.id, pipeline.finalize(deps, job.id, 1, artifact_id)


def test_plan_step_is_idempotent_and_never_pays_the_llm_twice(deps: Deps) -> None:
    llm = CountingLlm(deps.llm)
    deps.llm = llm  # type: ignore[assignment]
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "run-1")
    first, reused_first = pipeline.generate_plan(deps, job.id, 1)
    second, reused_second = pipeline.generate_plan(deps, job.id, 1)
    assert first == second and (reused_first, reused_second) == (False, True)
    assert llm.calls == 1
    with deps.sessions() as session:
        plan = session.get_one(Plan, first)
        assert session.get_one(Job, job.id).stage == "generating"
    assert plan.document["titulo"] and plan.prompt_version == "demo-v1"


def test_each_plan_follows_the_form_version_its_lead_answered(
    deps: Deps, newer_demo: FormSpec
) -> None:
    before = submit(deps, key="antes").job  # versão 1
    publish(deps, newer_demo)
    after = submit(deps, key="depois").job  # versão 2, a atual

    plans = {}
    for job in (before, after):
        with deps.sessions.begin() as session:
            jobs.claim(session, job.id, 1, "run-1")
        plan_id, _ = pipeline.generate_plan(deps, job.id, 1)
        with deps.sessions() as session:
            plans[job.id] = session.get_one(Plan, plan_id)

    assert plans[before.id].prompt_version == "demo-v1"
    assert plans[before.id].document["titulo"] == "Organizar atividades do projeto"
    assert plans[after.id].prompt_version == "demo-v2"
    assert plans[after.id].document["prazo"] == "até o fim do trimestre"


def test_render_step_stores_pdf_with_a_key_that_ignores_the_content(deps: Deps) -> None:
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "run-1")
    plan_id, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact_id, reused = pipeline.render_pdf(deps, job.id, 1, plan_id)
    again, reused_again = pipeline.render_pdf(deps, job.id, 1, plan_id)
    assert artifact_id == again and (reused, reused_again) == (False, True)
    with deps.sessions() as session:
        artifact = session.get_one(Artifact, artifact_id)
    content = deps.storage.get(artifact.storage_key)
    assert content.startswith(b"%PDF") and content.rstrip().endswith(b"%%EOF")
    assert artifact.storage_key == f"plans/{job.id}/v1.pdf"
    assert hashlib.sha256(content).hexdigest() == artifact.sha256


class VaryingPdf:
    """Um renderizador que, como o WeasyPrint, devolve bytes diferentes a cada chamada do mesmo plano
    (a data de geração e o ID do documento mudam)."""

    def __init__(self, inner: object) -> None:
        self.inner = inner
        self.calls = 0
        self.before_return: Callable[[], None] | None = None

    def render(self, spec, document, context):  # type: ignore[no-untyped-def]
        self.calls += 1
        rendered = self.inner.render(spec, document, context)  # type: ignore[attr-defined]
        if self.before_return is not None:
            self.before_return()
        return replace(rendered, content=rendered.content + b"\n%% render %d" % self.calls)


class DiesAfterPut:
    """O storage de verdade, mas a primeira gravação derruba a task logo depois de gravar (o worker
    morre, ou o banco cai, entre o `put` e o INSERT do artefato)."""

    def __init__(self, inner: LocalStorage) -> None:
        self.inner = inner
        self.dead = False

    def put(self, key: str, content: bytes, content_type: str) -> None:
        self.inner.put(key, content, content_type)
        if not self.dead:
            self.dead = True
            raise RetryableError(FailureCode.STORAGE_FAILED, "task morreu depois do put")

    def get(self, key: str) -> bytes:
        return self.inner.get(key)

    def delete(self, key: str) -> None:
        self.inner.delete(key)

    def presigned_url(self, key: str, *, ttl_seconds: int, filename: str) -> str | None:
        return None


def stored_keys(storage: LocalStorage) -> set[str]:
    return {p.relative_to(storage.root).as_posix() for p in storage.root.rglob("*") if p.is_file()}


def artifact_keys(deps: Deps) -> set[str]:
    with deps.sessions() as session:
        return set(session.scalars(sa.select(Artifact.storage_key)))


def test_a_render_retried_after_a_crash_leaves_no_file_without_an_artifact(deps: Deps) -> None:
    deps.pdf = VaryingPdf(deps.pdf)  # type: ignore[assignment]
    storage = deps.storage
    assert isinstance(storage, LocalStorage)
    deps.storage = DiesAfterPut(storage)  # type: ignore[assignment]
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "run-1")
    plan_id, _ = pipeline.generate_plan(deps, job.id, 1)

    with pytest.raises(RetryableError):
        pipeline.render_pdf(deps, job.id, 1, plan_id)
    assert artifact_keys(deps) == set()  # o arquivo já está no storage, ainda sem linha
    artifact_id, reused = pipeline.render_pdf(deps, job.id, 1, plan_id)

    assert reused is False and deps.pdf.calls == 2  # type: ignore[attr-defined]
    assert stored_keys(storage) == artifact_keys(deps) == {f"plans/{job.id}/v1.pdf"}
    with deps.sessions() as session:
        artifact = session.get_one(Artifact, artifact_id)
    # O arquivo que ficou é o da segunda renderização, e a linha guarda o hash dele.
    assert hashlib.sha256(storage.get(artifact.storage_key)).hexdigest() == artifact.sha256


@pytest.mark.parametrize(
    "winner_key",
    [
        pytest.param("plans/{job}/v1-0123456789ab.pdf", id="winner-has-an-old-key"),
        pytest.param("plans/{job}/v1.pdf", id="winner-has-the-same-key"),
    ],
)
def test_the_render_that_loses_a_race_leaves_only_the_winners_file(
    deps: Deps, winner_key: str
) -> None:
    pdf = VaryingPdf(deps.pdf)
    deps.pdf = pdf  # type: ignore[assignment]
    storage = deps.storage
    assert isinstance(storage, LocalStorage)
    job = submit(deps).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "run-1")
    plan_id, _ = pipeline.generate_plan(deps, job.id, 1)
    key = winner_key.format(job=job.id)

    def other_execution_wins() -> None:
        """Outra execução da mesma tentativa grava o arquivo e o artefato enquanto esta renderiza."""
        content = b"%PDF-1.4 do vencedor"
        storage.put(key, content, "application/pdf")
        with deps.sessions.begin() as session:
            session.add(
                Artifact(
                    job_id=job.id,
                    plan_id=plan_id,
                    kind="pdf",
                    version=1,
                    storage_key=key,
                    sha256=hashlib.sha256(content).hexdigest(),
                    byte_size=len(content),
                    renderer="fake",
                )
            )

    pdf.before_return = other_execution_wins
    artifact_id, reused = pipeline.render_pdf(deps, job.id, 1, plan_id)

    assert reused is True
    with deps.sessions() as session:
        assert session.get_one(Artifact, artifact_id).storage_key == key
    assert artifact_keys(deps) == {key}
    # Com a chave antiga do vencedor, o arquivo do perdedor (chave nova) saiu; com a mesma chave,
    # apagá-lo levaria o do vencedor, então nada é apagado.
    assert stored_keys(storage) == {key}


def test_finalize_completes_job_without_deliveries_when_no_webhook(deps: Deps) -> None:
    job_id, deliveries = run_until_finalize(deps)
    assert deliveries == []
    with deps.sessions() as session:
        job = session.get_one(Job, job_id)
    assert (job.status, job.stage) == ("completed", "completed") and job.completed_at


def test_finalize_creates_one_delivery_per_artifact_and_deliver_sends_payload(deps: Deps) -> None:
    webhook = record_webhook(deps)
    job_id, deliveries = run_until_finalize(deps)
    assert len(deliveries) == 1
    with deps.sessions() as session:
        artifact = session.scalars(sa.select(Artifact)).one()
        artifact_id, sha256 = artifact.id, artifact.sha256
    assert pipeline.finalize(deps, job_id, 1, artifact_id) == deliveries  # idempotente

    assert pipeline.deliver(deps, deliveries[0]) == "succeeded"
    assert pipeline.deliver(deps, deliveries[0]) == "already_succeeded"
    assert len(webhook.calls) == 1
    event, event_id, payload = webhook.calls[0]
    assert event == "plano.concluido" and event_id == str(deliveries[0])
    assert payload["lead"]["email"] == "contadora@example.test"
    assert payload["priority"] == "P2" and payload["plan"]["acoes"]
    assert payload["pdf"]["url"].startswith("http://testserver/v1/jobs/")
    assert "?t=" in payload["pdf"]["url"] and payload["pdf"]["direct_url"] is None
    assert payload["pdf"]["sha256"] == sha256  # o hash está na linha, não mais na chave
    with deps.sessions() as session:
        delivery = session.get_one(Delivery, deliveries[0])
    assert (delivery.status, delivery.attempts, delivery.response_status) == ("succeeded", 1, 200)


def test_deliver_failure_records_error_and_mark_failed(deps: Deps) -> None:
    record_webhook(deps, error=RetryableError(FailureCode.DELIVERY_UNAVAILABLE, "HTTP 503"))
    _, deliveries = run_until_finalize(deps)
    with pytest.raises(RetryableError):
        pipeline.deliver(deps, deliveries[0])
    with deps.sessions() as session:
        pending = session.get_one(Delivery, deliveries[0])
    assert (pending.status, pending.last_error) == ("pending", "[delivery_unavailable] HTTP 503")

    pipeline.mark_delivery_failed(deps, deliveries[0], "delivery_unavailable")
    with deps.sessions() as session:
        failed = session.get_one(Delivery, deliveries[0])
    # O motivo do último envio fica, com o código entre colchetes.
    assert (failed.status, failed.last_error) == ("failed", "[delivery_unavailable] HTTP 503")


def _sender(handler: httpx.MockTransport) -> WebhookSender:
    return WebhookSender(
        "https://hooks.example/mcc", "s" * 40, client=httpx.Client(transport=handler)
    )


def test_webhook_sender_signs_body_and_classifies_status() -> None:
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    assert _sender(httpx.MockTransport(ok)).send("plano.concluido", "evt-1", {"a": "ç"}) == 204
    request = seen[0]
    timestamp = request.headers["X-MCC-Timestamp"]
    expected = hmac.new(
        b"s" * 40, timestamp.encode() + b"." + request.content, hashlib.sha256
    ).hexdigest()
    assert request.headers["X-MCC-Signature"] == f"v1={expected}"
    assert request.headers["Idempotency-Key"] == "evt-1"
    assert json.loads(request.content) == {"a": "ç"}

    for status, error in ((503, RetryableError), (429, RetryableError), (400, TerminalError)):
        with pytest.raises(error):
            _sender(httpx.MockTransport(lambda r, s=status: httpx.Response(s))).send("e", "id", {})

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede")

    with pytest.raises(RetryableError):
        _sender(httpx.MockTransport(down)).send("e", "id", {})


def test_webhook_probe_signs_like_send_and_reports_the_answer_as_it_came() -> None:
    """O evento de teste: assinado como o `send`, sem seguir o 302 e lendo só o começo do corpo."""
    seen: list[httpx.Request] = []

    def moved(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        headers = {"Location": "https://outro.example/mcc"}
        return httpx.Response(302, headers=headers, content=b"x" * 5000)

    probe = _sender(httpx.MockTransport(moved)).probe("webhook.teste", "evt-2", {"test": True})

    assert (probe.status, probe.error) == (302, None) and probe.body_start == "x" * 2048
    (request,) = seen
    timestamp = request.headers["X-MCC-Timestamp"]
    expected = hmac.new(
        b"s" * 40, timestamp.encode() + b"." + request.content, hashlib.sha256
    ).hexdigest()
    assert request.headers["X-MCC-Signature"] == f"v1={expected}"
    assert (request.headers["X-MCC-Event"], request.headers["Idempotency-Key"]) == (
        "webhook.teste",
        "evt-2",
    )

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede")

    failed = _sender(httpx.MockTransport(down)).probe("webhook.teste", "evt-3", {})
    assert (failed.status, failed.error, failed.body_start) == (None, "ConnectError", "")
