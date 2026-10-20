"""Entrega de capturas nas costuras de aceite e transporte, sem geração."""

import json

import sqlalchemy as sa
from test_capture import capture_document

from app.forms.stored import import_definition
from app.intake import SubmissionIn, accept_submission
from app.models import Integration, WebhookDestination


def capture(deps, key="capture-delivery"):
    spec = deps.forms.current("capture-test")
    with deps.sessions.begin() as session:
        accepted = accept_submission(
            session,
            spec,
            SubmissionIn(
                answers=capture_document()["examples"]["answers"],
                consent={"privacy": True, "text_version": "capture-test-v1"},
            ),
            idempotency_key=key,
            signer=deps.signer,
            link_ttl=deps.link_ttl,
            limits=deps.submission_limits,
            forms=deps.forms,
        )
        return accepted.submission.id


def configured(deps):
    import_definition(deps, json.dumps(capture_document()), actor="operadora")
    with deps.sessions.begin() as session:
        destination = WebhookDestination(
            form_slug="capture-test",
            url="https://example.test/capture",
            secret_sealed=deps.cipher.seal("synthetic"),
            events=["submissao.persistida"],
            created_by="operadora",
            updated_by="operadora",
        )
        integration = Integration(
            form_slug="capture-test",
            kind="baserow",
            base_url="https://example.test",
            table_id=1,
            token_sealed=deps.cipher.seal("synthetic"),
            active=True,
            created_by="operadora",
            updated_by="operadora",
        )
        session.add_all([destination, integration])
    return destination.id, integration.id


def test_capture_creates_intents_atomically_and_replay_keeps_original_destinations(deps):
    from app.models import CaptureDelivery

    destination_id, integration_id = configured(deps)
    submission_id = capture(deps)
    assert capture(deps) == submission_id
    with deps.sessions() as session:
        deliveries = list(session.scalars(sa.select(CaptureDelivery)))
        assert len(deliveries) == 2
        assert {d.submission_id for d in deliveries} == {submission_id}
        assert {d.event for d in deliveries} == {"submissao.persistida"}
        assert {d.webhook_destination_id for d in deliveries} == {destination_id, None}
        assert {d.integration_id for d in deliveries} == {integration_id, None}
        assert {d.config_version for d in deliveries} == {1}


def test_dispatch_uses_historical_identity_and_fixed_event_without_pdf(deps):
    from app import capture_deliveries
    from app.models import CaptureDelivery, Lead

    destination_id, _ = configured(deps)
    submission_id = capture(deps)
    calls = []

    class Transport:
        def send(self, url, secret, event, event_id, payload):
            calls.append((url, event_id, payload))
            return 204

    deps.webhook = Transport()
    with deps.sessions.begin() as session:
        session.execute(sa.update(Lead).values(name="Outro cadastro"))
        delivery = session.scalar(
            sa.select(CaptureDelivery).where(
                CaptureDelivery.webhook_destination_id == destination_id
            )
        )
    assert capture_deliveries.dispatch(deps, delivery.id, 1) == "succeeded"
    assert capture_deliveries.dispatch(deps, delivery.id, 1) == "already_succeeded"
    assert len(calls) == 1
    _, event_id, payload = calls[0]
    assert event_id == str(delivery.id)
    assert payload["event"] == "submissao.persistida"
    assert payload["submission"] == {"id": str(submission_id)}
    assert payload["lead"]["name"] != "Outro cadastro"
    assert not set(payload).intersection({"job", "plan", "pdf", "receipt", "completion"})


def test_configuration_changed_never_redirects_delivery(deps):
    from app import capture_deliveries
    from app.models import CaptureDelivery

    destination_id, _ = configured(deps)
    capture(deps)

    class Transport:
        def send(self, *args):
            raise AssertionError("configuração alterada não autoriza envio")

    deps.webhook = Transport()
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(WebhookDestination).values(version=2, url="https://example.test/changed")
        )
        delivery = session.scalar(
            sa.select(CaptureDelivery).where(
                CaptureDelivery.webhook_destination_id == destination_id
            )
        )
    assert capture_deliveries.dispatch(deps, delivery.id, 1) == "failed"
    with deps.sessions() as session:
        row = session.get(CaptureDelivery, delivery.id)
        assert row.last_error == "configuration_changed"
        assert row.attempts == 1


def test_capture_lost_run_recovers_with_new_epoch_and_old_callback_is_ignored(deps):
    from datetime import timedelta

    from app import capture_deliveries
    from app.crons import RunState, reconcile_once
    from app.models import CaptureDelivery, utcnow

    destination_id, _ = configured(deps)
    capture(deps)
    with deps.sessions.begin() as session:
        delivery = session.scalar(
            sa.select(CaptureDelivery).where(
                CaptureDelivery.webhook_destination_id == destination_id
            )
        )
        delivery.hatchet_run_id = "lost-run"
        delivery.updated_at = utcnow() - timedelta(minutes=20)
    calls = []
    report = reconcile_once(
        deps,
        run_state=lambda _: RunState.MISSING,
        launch_capture_fn=lambda delivery_id, epoch: (
            calls.append((delivery_id, epoch)) or "recovered"
        ),
    )
    assert (delivery.id, 2) in calls
    assert report.captures_relaunched == 1
    with deps.sessions.begin() as session:
        capture_deliveries.set_run_id(session, delivery.id, 1, "old-run")
        row = session.get(CaptureDelivery, delivery.id)
        assert row.dispatch_no == 2
        assert row.hatchet_run_id != "old-run"


def test_webhook_events_default_pdf_and_omitted_update_preserves_choice(deps):
    from app import destinations

    with deps.sessions.begin() as session:
        saved = destinations.save(
            session,
            deps.cipher,
            form_slug=None,
            url="https://example.test/events",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
        )
        assert saved.destination.events == ("plano.concluido",)
    with deps.sessions.begin() as session:
        saved = destinations.save(
            session,
            deps.cipher,
            form_slug=None,
            url="https://example.test/events",
            actor="operadora",
            expected_version=1,
            production=False,
            http_confirmed=False,
            events=["submissao.persistida"],
        )
        assert saved.destination.events == ("submissao.persistida",)
    with deps.sessions.begin() as session:
        saved = destinations.save(
            session,
            deps.cipher,
            form_slug=None,
            url="https://example.test/changed",
            actor="operadora",
            expected_version=2,
            production=False,
            http_confirmed=False,
        )
        assert saved.destination.events == ("submissao.persistida",)


def test_retry_is_explicit_versioned_and_audited_and_old_epoch_cannot_settle(deps):
    import pytest

    from app import capture_deliveries
    from app.admin.actions import captures
    from app.models import AuditLog, CaptureDelivery

    destination_id, _ = configured(deps)
    submission_id = capture(deps)
    with deps.sessions.begin() as session:
        delivery = session.scalar(
            sa.select(CaptureDelivery).where(
                CaptureDelivery.webhook_destination_id == destination_id
            )
        )
        delivery.status = "failed"
        session.get(WebhookDestination, destination_id).version = 2
    with pytest.raises(captures.Conflict):
        captures.retry(
            deps,
            submission_id,
            delivery.id,
            expected_dispatch=1,
            expected_config_version=1,
            actor="operadora",
        )
    result = captures.retry(
        deps,
        submission_id,
        delivery.id,
        expected_dispatch=1,
        expected_config_version=2,
        actor="operadora",
    )
    assert result["dispatch_no"] == 2
    capture_deliveries.mark_failed(deps, delivery.id, 1)
    assert capture_deliveries.dispatch(deps, delivery.id, 1) == "obsolete"
    with deps.sessions() as session:
        row = session.get(CaptureDelivery, delivery.id)
        assert row.status == "pending"
        assert row.config_version == 2
        assert (
            session.scalar(sa.select(AuditLog).where(AuditLog.action == "capture_delivery.retry"))
            is not None
        )


def test_rollback_creates_no_intent_and_replay_does_not_adopt_new_destination(deps):
    import pytest

    from app.models import CaptureDelivery, Submission

    import_definition(deps, json.dumps(capture_document()), actor="operadora")
    spec = deps.forms.current("capture-test")
    with pytest.raises(RuntimeError), deps.sessions.begin() as session:
        accept_submission(
            session,
            spec,
            SubmissionIn(
                answers=capture_document()["examples"]["answers"],
                consent={"privacy": True, "text_version": "capture-test-v1"},
            ),
            idempotency_key="rollback",
            signer=deps.signer,
            link_ttl=deps.link_ttl,
            limits=deps.submission_limits,
        )
        raise RuntimeError("rollback sintético")
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Submission)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(CaptureDelivery)) == 0
    submission_id = capture(deps)
    with deps.sessions.begin() as session:
        session.add(
            WebhookDestination(
                form_slug="capture-test",
                url="https://example.test/new",
                secret_sealed=deps.cipher.seal("synthetic"),
                events=["submissao.persistida"],
                created_by="operadora",
                updated_by="operadora",
            )
        )
    assert capture(deps) == submission_id
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(CaptureDelivery)) == 0


def test_timeout_preserves_event_and_late_task_after_purge_sends_nothing(deps):
    from datetime import timedelta

    import pytest

    from app import capture_deliveries
    from app.errors import RetryableError
    from app.models import CaptureDelivery, Submission, utcnow
    from app.retention import purge_expired

    destination_id, _ = configured(deps)
    submission_id = capture(deps)
    calls = []

    class Transport:
        def send(self, url, secret, event, event_id, payload):
            calls.append(event_id)
            if len(calls) == 1:
                raise TimeoutError("marcador-pessoal-nao-pode-sair")
            return 204

    deps.webhook = Transport()
    with deps.sessions() as session:
        delivery = session.scalar(
            sa.select(CaptureDelivery).where(
                CaptureDelivery.webhook_destination_id == destination_id
            )
        )
    with pytest.raises(RetryableError, match="entrega de captura pendente") as failure:
        capture_deliveries.dispatch(deps, delivery.id, 1)
    assert "marcador-pessoal" not in str(failure.value)
    assert capture_deliveries.dispatch(deps, delivery.id, 1) == "succeeded"
    assert calls == [str(delivery.id), str(delivery.id)]
    with deps.sessions.begin() as session:
        session.get(Submission, submission_id).created_at = utcnow() - timedelta(days=400)
    assert purge_expired(deps).captures_deleted == 1
    assert capture_deliveries.dispatch(deps, delivery.id, 1) == "already_succeeded"
    assert len(calls) == 2


def test_dispatch_serializes_purge_on_submission(deps):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta
    from threading import Event

    from app import capture_deliveries
    from app.models import CaptureDelivery, utcnow
    from app.retention import purge_expired

    destination_id, _ = configured(deps)
    capture(deps)
    with deps.sessions() as session:
        delivery = session.scalar(
            sa.select(CaptureDelivery).where(
                CaptureDelivery.webhook_destination_id == destination_id
            )
        )
    sending, release = Event(), Event()

    class Transport:
        def send(self, *args):
            sending.set()
            assert release.wait(5)
            return 204

    deps.webhook = Transport()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(capture_deliveries.dispatch, deps, delivery.id, 1)
        assert sending.wait(5)
        try:
            assert purge_expired(deps, now=utcnow() + timedelta(days=400)).captures_deleted == 0
        finally:
            release.set()
        assert future.result(timeout=5) == "succeeded"
    assert purge_expired(deps, now=utcnow() + timedelta(days=400)).captures_deleted == 1


def test_capture_publication_refuses_incompatible_effective_webhook_locally(deps, monkeypatch):
    from app.forms import drafts
    from app.models import WebhookDestination

    slug = drafts.create(
        deps, title="Pesquisa", slug="capture-test", actor="operadora", generation="none"
    )
    with deps.sessions.begin() as session:
        session.add(
            WebhookDestination(
                form_slug=None,
                url="https://example.test/pdf",
                secret_sealed=deps.cipher.seal("synthetic"),
                created_by="operadora",
                updated_by="operadora",
            )
        )
        draft = drafts.load(session, slug)

    def forbidden(*args, **kwargs):
        raise AssertionError("publicação não consulta provedor")

    monkeypatch.setattr(deps.baserow, "fields", forbidden)
    simulation = drafts.publish(
        deps, slug, actor="operadora", note="Captura", expected=draft.token, dry_run=True
    )
    assert not simulation.ready
    assert any(problem.path == "integrations" for problem in simulation.review.problems)


def test_capture_baserow_dispatch_uses_capture_profile_and_original_submission_key(deps):
    from conftest import BASEROW_TABLE, BASEROW_TOKEN, BASEROW_URL, FakeBaserow

    from app import capture_deliveries
    from app.models import CaptureDelivery

    _, integration_id = configured(deps)
    fake = FakeBaserow().install(deps)
    with deps.sessions.begin() as session:
        row = session.get(Integration, integration_id)
        row.base_url, row.table_id, row.token_sealed = (
            BASEROW_URL,
            BASEROW_TABLE,
            deps.cipher.seal(BASEROW_TOKEN),
        )
        row.capture_mapping = [
            {"field_id": 104, "name": "Código", "type": "text", "source": "submission.id"},
            {"field_id": 101, "name": "Nome", "type": "text", "source": "lead.name"},
        ]
        row.capture_key_field_id = 104
    submission_id = capture(deps)
    with deps.sessions() as session:
        delivery = session.scalar(
            sa.select(CaptureDelivery).where(CaptureDelivery.integration_id == integration_id)
        )
    assert capture_deliveries.dispatch(deps, delivery.id, 1) == "succeeded"
    assert (
        fake.row_where(104, str(submission_id))["field_101"]
        == capture_document()["examples"]["answers"]["nome"]
    )
    assert len(fake.rows) == 1


def test_capture_delivery_api_requires_total_confirmation_and_versions_without_pii(deps):
    from admin_api_support import api_client, url_of

    from app.models import CaptureDelivery

    destination_id, _ = configured(deps)
    submission_id = capture(deps)
    with deps.sessions.begin() as session:
        delivery = session.scalar(
            sa.select(CaptureDelivery).where(
                CaptureDelivery.webhook_destination_id == destination_id
            )
        )
        delivery.status = "failed"
    url = url_of(
        "retry_capture_delivery", submission_id=str(submission_id), delivery_id=str(delivery.id)
    )
    body = {"expected_dispatch": 1, "expected_config_version": 1}
    with api_client(deps, role="leitura") as client:
        state = client.get(url_of("get_capture", submission_id=str(submission_id))).json()
        assert "answers" not in json.dumps(state)
        assert len(state["deliveries"]) == 2
        assert client.post(url, json={**body, "confirm": True}).status_code == 403
    with api_client(deps) as client:
        assert client.post(url, json=body).status_code == 422
        result = client.post(url, json={**body, "confirm": True})
        assert result.status_code == 200, result.text
        assert result.json()["dispatch_no"] == 2
        assert client.post(url, json={**body, "confirm": True}).status_code == 409


def test_capture_http_completes_while_trigger_is_blocked_and_keeps_intents(deps, monkeypatch):
    from threading import Event

    from fastapi.testclient import TestClient

    from app import workflows
    from app.api.main import create_app
    from app.models import CaptureDelivery

    configured(deps)
    entered, release, finished = Event(), Event(), Event()

    def blocked_trigger(delivery_id, epoch):
        entered.set()
        try:
            assert release.wait(5)
            return
        finally:
            finished.set()

    monkeypatch.setattr(workflows, "launch_capture", blocked_trigger)
    payload = {
        "answers": capture_document()["examples"]["answers"],
        "consent": {"privacy": True, "text_version": "capture-test-v1"},
    }
    try:
        with TestClient(create_app()) as client:
            response = client.post(
                "/v1/forms/capture-test/submissions",
                json=payload,
                headers={"Idempotency-Key": "slow-trigger"},
            )
            assert response.status_code == 202
            assert entered.wait(5)
            assert not release.is_set()
            with deps.sessions() as session:
                assert session.scalar(sa.select(sa.func.count()).select_from(CaptureDelivery)) == 2
    finally:
        release.set()
        assert finished.wait(5)


def test_capture_workflow_returns_only_reference_and_real_counts_and_sanitizes_errors(
    deps, monkeypatch
):
    import uuid

    import pytest
    from hatchet_sdk.exceptions import TaskRunError

    from app import capture_deliveries, workflows
    from app.errors import RetryableError

    input = workflows.CaptureInput.for_delivery(uuid.uuid4(), 1)
    result = workflows.send_capture.mock_run(input=input)
    assert result.model_dump() == {
        "delivery_id": input.delivery_id,
        "delivered_count": 0,
        "skipped_count": 1,
        "failed_count": 0,
    }

    def leaking(*args):
        raise ValueError("sentinela-respostas-pessoais")

    monkeypatch.setattr(capture_deliveries, "dispatch", leaking)
    with pytest.raises(RetryableError) as failure:
        workflows.send_capture.mock_run(input=input)
    assert (
        "sentinela-respostas-pessoais" not in TaskRunError.from_exception(failure.value, None).trace
    )


def test_capture_configuration_api_returns_selected_profile_and_tests_only_synthetic_data(deps):
    from admin_api_support import api_client, url_of
    from conftest import BASEROW_TABLE, BASEROW_TOKEN, BASEROW_URL, FakeBaserow

    from app.models import Submission

    import_definition(deps, json.dumps(capture_document()), actor="operadora")
    fake = FakeBaserow().install(deps)
    with api_client(deps) as client:
        connection = client.put(
            url_of("connect_form_integration", slug="capture-test"),
            json={
                "base_url": BASEROW_URL,
                "table_id": BASEROW_TABLE,
                "token": BASEROW_TOKEN,
                "expected_version": "0",
                "confirm": True,
            },
        )
        assert connection.status_code == 200, connection.text
        saved = client.put(
            url_of("save_form_integration_mapping", slug="capture-test"),
            json={"sources": {"104": "submission.id", "101": "lead.name"}, "expected_version": "1"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["profile"] == "capture"
        assert saved.json()["key_field_id"] == 104
        assert {entry["source"] for entry in saved.json()["entries"]} == {
            "submission.id",
            "lead.name",
        }
        activated = client.post(
            url_of("activate_form_integration", slug="capture-test"),
            json={"expected_version": "2", "confirm": True},
        )
        assert activated.status_code == 200, activated.text
        sample = client.post(
            url_of("send_form_integration_test", slug="capture-test"), json={"confirm": True}
        )
        assert sample.status_code == 200 and sample.json()["ok"], sample.text
        detail = client.get(url_of("get_form_integration", slug="capture-test")).json()
        assert detail["profile"] == "capture"
        assert len(detail["capture_mapping"]) == 2
        assert detail["mapping"] == []
    assert len(fake.rows) == 1
    assert fake.row_where(104, "teste-capture-test") is not None
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Submission)) == 0


def test_recovery_fences_a_run_lost_before_its_id_was_persisted(deps):
    from datetime import timedelta

    from app.capture_deliveries import set_run_id
    from app.crons import reconcile_once
    from app.models import CaptureDelivery, utcnow

    configured(deps)
    capture(deps)
    with deps.sessions.begin() as session:
        session.execute(
            sa.update(CaptureDelivery).values(updated_at=utcnow() - timedelta(minutes=20))
        )
    calls = []
    result = reconcile_once(
        deps, launch_capture_fn=lambda id_, epoch: calls.append((id_, epoch)) or "new-run"
    )
    assert result.captures_launched == 2
    assert {epoch for _, epoch in calls} == {2}
    with deps.sessions.begin() as session:
        for id_, _ in calls:
            set_run_id(session, id_, 1, "late-old-run")
            assert session.get(CaptureDelivery, id_).hatchet_run_id is None


def test_capture_webhook_configuration_test_is_marked_synthetic_without_real_submission(deps):
    from app import destinations
    from app.adapters.webhook import Probe
    from app.models import Submission

    configured(deps)
    payloads = []

    class Transport:
        def probe(self, url, secret, event, event_id, payload):
            payloads.append(payload)
            return Probe(status=204, elapsed_ms=1)

    deps.webhook = Transport()
    assert (
        destinations.send_test(
            deps, "capture-test", actor="operadora", event="submissao.persistida"
        ).status
        == 204
    )
    assert payloads[0]["test"] is True
    assert payloads[0]["event"] == "submissao.persistida"
    assert payloads[0]["lead"]["email"] == "teste@example.test"
    assert not {"job", "pdf", "plan", "receipt"}.intersection(payloads[0])
    with deps.sessions() as session:
        assert session.scalar(sa.select(sa.func.count()).select_from(Submission)) == 0


def test_activation_revalidates_the_new_purpose_when_publication_changes_during_field_read(
    deps, monkeypatch
):
    import pytest
    from conftest import BASEROW_TABLE, BASEROW_TOKEN, BASEROW_URL, FakeBaserow
    from test_admin_forms import store_version

    from app.admin.actions import integrations as actions
    from app.forms.starter import definition
    from app.integrations import config

    document = definition(slug="capture-test", title="Pesquisa")
    import_definition(deps, json.dumps(document), actor="operadora")
    fake = FakeBaserow().install(deps)
    with deps.sessions.begin() as session:
        stored = config.connect(
            session,
            deps.cipher,
            form_slug="capture-test",
            base_url=BASEROW_URL,
            table_id=BASEROW_TABLE,
            token=BASEROW_TOKEN,
            actor="operadora",
            expected_version=0,
            production=False,
        )
        row = session.get(Integration, stored.id)
        row.mapping = [
            {"field_id": 104, "name": "Código", "type": "text", "source": "job.public_id"}
        ]
        row.key_field_id = 104
        # A fonte existe e serve ao tipo; suas opções não têm pares na tabela remota.
        row.capture_mapping = [
            {"field_id": 104, "name": "Código", "type": "text", "source": "submission.id"},
            {"field_id": 108, "name": "Equipe", "type": "single_select", "source": "answer:equipe"},
        ]
        row.capture_key_field_id = 104
    original = actions.read_fields

    def field_read(deps_, slug):
        result = original(deps_, slug)
        store_version(deps_, 2, capture_document())
        return result

    monkeypatch.setattr(actions, "read_fields", field_read)
    with pytest.raises((actions.ReportPending, config.Conflict)):
        actions.activate(deps, "capture-test", expected_version=1, actor="operadora")
    with deps.sessions() as session:
        assert session.get(Integration, stored.id).active is False
    assert len(fake.rows) == 0
