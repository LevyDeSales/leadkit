"""Liberação histórica, idempotência e retenção pelas costuras públicas do domínio."""

import json
from uuid import uuid4

import pytest
import sqlalchemy as sa
from conftest import legacy_definition, record_webhook
from test_deployment_contract import evidence
from test_intake import submit

from app import deployment_contract, jobs, pipeline
from app.forms.stored import import_definition
from app.models import Artifact, Job, PlanRelease
from app.plan_release import ReleasePdfCommand, ReleaseRefused, participant_access, release_pdf


@pytest.fixture
def manual_pdf(deps, request):
    with deps.sessions.begin() as session:
        epoch = uuid4()
        deployment_contract.begin_rollout(session, epoch=epoch)
        manifest = evidence()
        for service in manifest["services"].values():
            service["contract"] = service["previous_contract"] = 2
        deployment_contract.attest_rollout(session, epoch=epoch, evidence=manifest)
        document = {**legacy_definition("demo"), "slug": "manual", "release": {"mode": "manual"}}
    import_definition(deps, json.dumps(document), actor="operadora", contact_confirmed=True)
    if getattr(request, "param", None):
        from app import destinations

        record_webhook(deps)
        with deps.sessions.begin() as session:
            destinations.save(
                session,
                deps.cipher,
                form_slug="manual",
                purpose="participant",
                url="https://example.test/release",
                actor="operadora",
                expected_version=0,
                production=False,
                http_confirmed=False,
                events=request.param,
            )
    form = deps.forms.current("manual")
    job = submit(deps, form=form).job
    with deps.sessions.begin() as session:
        jobs.claim(session, job.id, 1, "synthetic-run")
    plan_id, _ = pipeline.generate_plan(deps, job.id, 1)
    artifact_id, _ = pipeline.render_pdf(deps, job.id, 1, plan_id)
    pipeline.finalize(deps, job.id, 1, artifact_id)
    yield job.id, artifact_id
    with deps.sessions.begin() as session:
        deployment_contract.begin_rollout(session, epoch=uuid4())


def command_for(artifact_id):
    return ReleasePdfCommand(
        expected_attempt=1,
        expected_artifact_id=artifact_id,
        expected_form_version_number=1,
        expected_release_version=0,
        idempotency_key=uuid4(),
        mode="alternative",
        reason_code="alternative_channel",
        confirm=True,
    )


def test_release_without_channel_is_a_fact_without_touching_retention(deps, manual_pdf):
    job_id, artifact_id = manual_pdf
    with deps.sessions.begin() as session:
        job = session.get_one(Job, job_id)
        before = (job.updated_at, job.expires_at)
        assert (
            participant_access(session, job, session.get_one(Artifact, artifact_id)).state == "held"
        )
        result = release_pdf(
            session, deps, job.public_id, actor="operadora", command=command_for(artifact_id)
        )
        assert result.notification_decision == "no_participant_channel"
        assert result.delivery_ids == ()
        assert (
            participant_access(session, job, session.get_one(Artifact, artifact_id)).state
            == "released"
        )
        session.refresh(job)
        assert (job.updated_at, job.expires_at) == before
        assert session.scalar(sa.select(sa.func.count()).select_from(PlanRelease)) == 1


def test_replay_of_released_attempt_precedes_current_state_and_storage(deps, manual_pdf):
    job_id, artifact_id = manual_pdf
    command = command_for(artifact_id)
    with deps.sessions.begin() as session:
        job = session.get_one(Job, job_id)
        public_id = job.public_id
        first = release_pdf(session, deps, public_id, actor="operadora", command=command)
    with deps.sessions.begin() as session:
        assert (
            jobs.reprocess(
                session,
                job_id,
                actor="operadora",
                reason="Nova tentativa sintética",
                expected_attempt=1,
            )
            == 2
        )
    with deps.sessions() as session:
        artifact = session.get_one(Artifact, artifact_id)
        deps.storage.delete(artifact.storage_key)
    with deps.sessions.begin() as session:
        deployment_contract.begin_rollout(session, epoch=uuid4())
        replay = release_pdf(session, deps, public_id, actor="operadora", command=command)
        assert replay.release_id == first.release_id
        assert replay.replay and not replay.current
        job = session.get_one(Job, job_id)
        assert participant_access(session, job, None).state == "held"
        with pytest.raises(ReleaseRefused):
            release_pdf(session, deps, public_id, actor="outra-operadora", command=command)


def test_missing_pdf_refuses_new_release_without_fact(deps, manual_pdf):
    from app.plan_release import ReleaseRefused

    job_id, artifact_id = manual_pdf
    with deps.sessions() as session:
        deps.storage.delete(session.get_one(Artifact, artifact_id).storage_key)
    with deps.sessions.begin() as session:
        job = session.get_one(Job, job_id)
        with pytest.raises(ReleaseRefused, match="pdf_unavailable"):
            release_pdf(
                session, deps, job.public_id, actor="operadora", command=command_for(artifact_id)
            )
        assert (
            participant_access(session, job, session.get_one(Artifact, artifact_id)).state == "held"
        )


def test_second_command_cannot_reauthorize_the_same_attempt(deps, manual_pdf):
    from app.plan_release import ReleaseRefused

    job_id, artifact_id = manual_pdf
    with deps.sessions.begin() as session:
        job = session.get_one(Job, job_id)
        release_pdf(
            session, deps, job.public_id, actor="operadora", command=command_for(artifact_id)
        )
        with pytest.raises(ReleaseRefused, match="state_conflict"):
            release_pdf(
                session, deps, job.public_id, actor="operadora", command=command_for(artifact_id)
            )


def test_held_plan_is_absent_from_json_html_and_direct_download(deps, manual_pdf):
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    client = TestClient(create_app())
    job_id, _ = manual_pdf
    with deps.sessions() as session:
        job = session.get_one(Job, job_id)
        token = deps.signer.issue(job.public_id, job.token_version)
    status = client.get(f"/v1/jobs/{job.public_id}", params={"t": token})
    assert status.status_code == 200
    assert status.json()["release"]["state"] == "held"
    assert status.json().get("pdf_url") is None
    assert client.get(f"/v1/jobs/{job.public_id}/pdf", params={"t": token}).status_code == 403
    page = client.get(f"/d/{job.public_id}", params={"t": token}, follow_redirects=True)
    assert "Seu plano aguarda liberação" in page.text
    assert "Organizar atividades do projeto" not in page.text
    assert f"/d/{job.public_id}/pdf" not in page.text


@pytest.mark.parametrize("manual_pdf", [["plano.concluido", "plano.liberado"]], indirect=True)
def test_release_outbox_uses_distinct_fact_and_unblocks_completion(deps, manual_pdf):
    from app.models import Delivery
    from app.plan_release import delivery_eligible

    job_id, artifact_id = manual_pdf
    with deps.sessions() as session:
        completed = session.scalars(sa.select(Delivery).where(Delivery.job_id == job_id)).one()
        assert completed.event == "plano.concluido"
        assert not delivery_eligible(session, completed)
        completed_id = completed.id
    assert pipeline.deliver(deps, completed_id) == "blocked"
    assert deps.webhook.calls == []
    command = command_for(artifact_id)
    with deps.sessions.begin() as session:
        job = session.get_one(Job, job_id)
        first = release_pdf(session, deps, job.public_id, actor="operadora", command=command)
        assert first.notification_decision == "outbox"
        assert len(first.delivery_ids) == 1
    pipeline.deliver(deps, first.delivery_ids[0])
    pipeline.deliver(deps, completed_id)
    assert [call[0] for call in deps.webhook.calls] == ["plano.liberado", "plano.concluido"]
    payload = deps.webhook.calls[0][2]
    assert payload["release"]["id"] == str(first.release_id)
    assert payload["occurred_at"] == first.occurred_at.isoformat()
    assert "actor" not in payload["release"]


def test_admin_release_requires_total_confirmation_and_replays_without_dispatch(
    deps, manual_pdf, api_client
):
    job_id, artifact_id = manual_pdf
    with deps.sessions() as session:
        public_id = session.get_one(Job, job_id).public_id
    path = f"/admin/api/v1/jobs/{public_id}/release"
    body = command_for(artifact_id).model_dump(mode="json")
    assert api_client("leitura").post(path, content="invalid json").status_code == 403
    client = api_client("total")
    assert client.post(path, json={**body, "confirm": False}).status_code == 422
    first = client.post(path, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["notification_decision"] == "no_participant_channel"
    assert not first.json()["launched"]
    replay = client.post(path, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["release_id"] == first.json()["release_id"]
    assert replay.json()["replay"] and not replay.json()["launched"]
    safe = api_client("leitura").get(f"/admin/api/v1/jobs/{public_id}")
    assert safe.status_code == 200
    assert safe.json()["release"]["state"] == "released"
    assert safe.json()["lead_data"] is None


def test_release_operation_is_available_to_mcp_without_lead_data_opt_in():
    from app.admin.api.openapi import build
    from app.admin_mcp import catalog

    loaded = catalog.load(build())
    selected = {
        tool.name: tool
        for tool in catalog.select(loaded.tools.values(), role="total", lead_data=False)
    }
    assert "release_job_pdf" in selected
    assert selected["release_job_pdf"].annotations["openWorldHint"]
    assert "release_job_pdf" not in {
        tool.name for tool in catalog.select(loaded.tools.values(), role="leitura", lead_data=False)
    }


@pytest.mark.parametrize("manual_pdf", [["plano.liberado"]], indirect=True)
def test_release_only_subscription_never_creates_completion_intent(deps, manual_pdf):
    from app.models import Delivery

    job_id, artifact_id = manual_pdf
    with deps.sessions.begin() as session:
        assert list(session.scalars(sa.select(Delivery).where(Delivery.job_id == job_id))) == []
        job = session.get_one(Job, job_id)
        released = release_pdf(
            session, deps, job.public_id, actor="operadora", command=command_for(artifact_id)
        )
        assert released.notification_decision == "outbox"
        assert len(released.delivery_ids) == 1


def test_two_concurrent_release_commands_persist_one_fact(deps, manual_pdf):
    import threading

    job_id, artifact_id = manual_pdf
    with deps.sessions() as session:
        public_id = session.get_one(Job, job_id).public_id
    results = []
    gate = threading.Barrier(2)
    command = command_for(artifact_id)

    def authorize():
        gate.wait(timeout=5)
        with deps.sessions.begin() as session:
            results.append(
                release_pdf(session, deps, public_id, actor="operadora", command=command)
            )

    threads = [threading.Thread(target=authorize) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert len(results) == 2
    assert results[0].release_id == results[1].release_id
    assert sorted(result.replay for result in results) == [False, True]


def test_retention_removes_release_relations_without_prolonging_request(deps, manual_pdf):
    from datetime import timedelta

    from app.models import utcnow
    from app.retention import purge_expired

    job_id, artifact_id = manual_pdf
    with deps.sessions.begin() as session:
        job = session.get_one(Job, job_id)
        release_pdf(
            session, deps, job.public_id, actor="operadora", command=command_for(artifact_id)
        )
    report = purge_expired(deps, now=utcnow() + timedelta(days=100))
    assert report.requests_deleted == 1
    with deps.sessions() as session:
        assert session.get(Job, job_id) is None
        assert session.scalar(sa.select(sa.func.count()).select_from(PlanRelease)) == 0


def test_html_release_uses_observed_fields_and_the_shared_service(deps, manual_pdf, api_client):
    import re

    from test_admin import csrf_token, login

    job_id, _ = manual_pdf
    with deps.sessions() as session:
        public_id = session.get_one(Job, job_id).public_id
    client = api_client("total")
    assert login(client).status_code == 303
    page = client.get(f"/admin/jobs/{public_id}")
    assert page.status_code == 200
    assert "Confirmo a liberação desta tentativa" in page.text
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', page.text))
    body = {
        key: fields[key]
        for key in [
            "expected_attempt",
            "expected_artifact_id",
            "expected_form_version_number",
            "expected_release_version",
            "idempotency_key",
        ]
    }
    body.update(
        csrf_token=csrf_token(client),
        mode="alternative",
        reason_code="alternative_channel",
        confirm="on",
    )
    response = client.post(
        f"/admin/jobs/{public_id}/release", data=body, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 303, response.text
    with deps.sessions() as session:
        release = session.scalars(sa.select(PlanRelease).where(PlanRelease.job_id == job_id)).one()
        assert release.mode == "alternative" and release.actor == "operadora"


def test_mcp_release_calls_the_real_api_after_confirmation(deps, manual_pdf, api_client, tmp_path):
    from admin_mcp_live import Live
    from admin_mcp_world import Asker

    job_id, artifact_id = manual_pdf
    with deps.sessions() as session:
        public_id = session.get_one(Job, job_id).public_id
    live = Live(api_client("total"), tmp_path)
    try:
        command = command_for(artifact_id).model_dump(mode="json")
        command.pop("confirm")
        asker = Asker(True)
        released = live.ok("release_job_pdf", {"public_id": public_id, **command}, asker)
        assert released["notification_decision"] == "no_participant_channel"
        assert asker.messages
        assert "release_job_pdf" in asker.messages[0]
        replay = live.ok("release_job_pdf", {"public_id": public_id, **command}, Asker(True))
        assert replay["release_id"] == released["release_id"] and replay["replay"]
    finally:
        live.close()


@pytest.mark.parametrize("manual_pdf", [["plano.liberado"]], indirect=True)
def test_integrations_hub_accepts_release_event_without_exposing_capabilities(
    deps, manual_pdf, api_client
):
    from test_admin_destinations import admin_client

    response = api_client("leitura").get(
        "/admin/api/v1/integrations", params={"form_slug": "manual"}
    )
    assert response.status_code == 200, response.text
    receiver = next(
        item["receiver"] for item in response.json()["items"] if item["connector"] == "webhook"
    )
    assert receiver["events"] == ["plano.liberado"]
    page = admin_client(deps).get("/admin/integrations?form_slug=manual")
    assert page.status_code == 200
    assert "Liberação do plano" in page.text
    assert "?k=i1." not in page.text


@pytest.mark.parametrize("manual_pdf", [["plano.concluido", "plano.liberado"]], indirect=True)
def test_held_delivery_is_skipped_by_worker_and_failure_callback(deps, manual_pdf):
    from app import workflows
    from app.models import Delivery

    job_id, _ = manual_pdf
    with deps.sessions() as session:
        delivery_id = session.scalars(sa.select(Delivery.id).where(Delivery.job_id == job_id)).one()
    output = workflows.send.mock_run(input=workflows.DeliverInput.for_delivery(delivery_id))
    assert output.skipped_count == 1 and output.delivered_count == 0
    assert pipeline.mark_delivery_failed(deps, delivery_id, "synthetic_failure") == 0
    with deps.sessions() as session:
        assert session.get_one(Delivery, delivery_id).status == "pending"


@pytest.mark.parametrize("manual_pdf", [["plano.concluido", "plano.liberado"]], indirect=True)
def test_database_preserves_legacy_uniqueness_and_bidirectional_release_channel(deps, manual_pdf):
    from app.models import Delivery

    job_id, artifact_id = manual_pdf
    with deps.sessions.begin() as session:
        job = session.get_one(Job, job_id)
        release = release_pdf(
            session, deps, job.public_id, actor="operadora", command=command_for(artifact_id)
        )
        release_delivery = release.delivery_ids[0]
        completed = session.scalar(
            sa.select(Delivery.id).where(
                Delivery.job_id == job_id, Delivery.event == "plano.concluido"
            )
        )
        for delivery_id, invalid in [
            (release_delivery, {"release_id": None}),
            (release_delivery, {"event": "plano.concluido"}),
            (release_delivery, {"config_version": None}),
            (release_delivery, {"webhook_destination_id": None}),
            (completed, {"release_id": release.release_id}),
            (completed, {"event": "plano.liberado"}),
        ]:
            with pytest.raises(sa.exc.IntegrityError), session.begin_nested():
                session.execute(
                    sa.update(Delivery).where(Delivery.id == delivery_id).values(**invalid)
                )
        original = session.get_one(Delivery, completed)
        with pytest.raises(sa.exc.IntegrityError), session.begin_nested():
            session.add(
                Delivery(
                    job_id=job.id,
                    artifact_id=artifact_id,
                    destination="webhook",
                    event="plano.concluido",
                    audience="participant",
                    authorization_origin="explicit",
                    webhook_destination_id=original.webhook_destination_id,
                    config_version=original.config_version,
                    authorized_attempt=1,
                )
            )
            session.flush()
