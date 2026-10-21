"""Encaminhamento pela fronteira transacional, com dados sintéticos e PostgreSQL."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from handoff_support import isolated_handoff_contract  # noqa: F401
from pydantic import SecretStr
from test_continuation_models import contract_session as contract_session  # noqa: PLC0414
from test_deployment_contract import evidence

from app import deployment_contract, handoff_receivers, handoffs, integration_credentials
from app.destinations import SecretCipher
from app.forms import starter
from app.forms.definition import compile_definition
from app.handoff_types import CreateCredential, SaveReceiver, SetReceiverEnabled
from app.models import DeploymentContract, Form, FormVersion, Lead, Submission

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


def configured(session, *, generation="none", release_mode=None):
    session.add(Form(slug="synthetic", title="Sintético", created_by="teste"))
    state = session.get(DeploymentContract, 1)
    if state is None:
        session.add(DeploymentContract(id=1, minimum_contract=2))
    else:
        state.minimum_contract = 2
    session.flush()
    epoch = uuid4()
    deployment_contract.begin_rollout(session, epoch=epoch)
    manifest = evidence()
    for service in manifest["services"].values():
        service["contract"] = service["previous_contract"] = 2
    deployment_contract.attest_rollout(session, epoch=epoch, evidence=manifest)
    receiver = handoff_receivers.save_receiver(
        session,
        "synthetic",
        command=SaveReceiver(
            expected_version="0",
            name="Atendimento",
            entry_url="https://example.test/continuar",
            reference_ttl_seconds=3600,
            confirm=True,
        ),
        actor="teste",
        now=NOW,
    )
    created = integration_credentials.create_credential(
        session,
        "synthetic",
        command=CreateCredential(
            name="Serviço",
            receiver_revision=1,
            expected_receiver_version=receiver.version,
            expires_at=None,
            idempotency_key=uuid4(),
            confirm=True,
        ),
        actor="teste",
        now=NOW,
    )
    receiver = handoff_receivers.set_receiver_enabled(
        session,
        "synthetic",
        command=SetReceiverEnabled(expected_version=receiver.version, enabled=True, confirm=True),
        actor="teste",
        cipher=object(),
        now=NOW,
    )
    document = starter.definition(slug="synthetic", title="Sintético", generation=generation)
    if release_mode is not None:
        document["release"] = {"mode": release_mode}
    document["completion"] = {
        "mode": "redirect",
        "url": receiver.entry_url,
        "handoff": {"receiver_id": str(receiver.id), "receiver_revision": receiver.revision},
    }
    form = session.scalar(__import__("sqlalchemy").select(Form).where(Form.slug == "synthetic"))
    session.add(
        FormVersion(
            form_id=form.id,
            number=1,
            definition=document,
            fingerprint="a" * 64,
            note="Sintético",
            published_by="teste",
        )
    )
    session.flush()
    return compile_definition(document), created


def test_confirmed_reference_resolves_only_correlation_and_replays_its_original_url(
    contract_session,
):
    from cryptography.fernet import Fernet

    session = contract_session
    spec, created = configured(session)
    lead = Lead(email="sintetica@example.test")
    session.add(lead)
    session.flush()
    submission = Submission(
        lead_id=lead.id,
        form_slug=spec.slug,
        form_version=spec.version,
        form_version_number=1,
        idempotency_key="synthetic",
        answers={"email": lead.email},
        consent={},
    )
    session.add(submission)
    session.flush()
    cipher = SecretCipher((Fernet.generate_key().decode(),))
    handoffs.prepare_handoff(
        session,
        submission,
        spec,
        receipt_expires_at=NOW + timedelta(days=1),
        cipher=cipher,
        now=NOW,
    )
    view = handoffs.completion_view(session, submission, spec, cipher=cipher, now=NOW)
    assert view.handoff_status == "ready"
    raw = view.url.split("?leadkit_ref=")[1]
    principal = integration_credentials.authenticate_credential(session, created.secret, now=NOW)
    resolved = handoffs.resolve_handoff(session, principal, SecretStr(raw), now=NOW)
    assert resolved.submission_id == submission.id
    assert "email" not in resolved.model_dump_json()
    assert "lead_id" not in resolved.model_dump()
    assert (
        handoffs.completion_view(session, submission, spec, cipher=cipher, now=NOW).url == view.url
    )


def accept(session, spec, deps, *, key="capture", cipher=True):
    from app.intake import SubmissionIn, accept_submission

    document = starter.definition(slug="synthetic", title="Sintético", generation=spec.generation)
    return accept_submission(
        session,
        spec,
        SubmissionIn(
            answers=document["examples"]["answers"],
            consent={"privacy": True, "text_version": spec.consent_text_version},
        ),
        idempotency_key=key,
        signer=deps.signer,
        link_ttl=timedelta(days=1),
        limits=deps.submission_limits,
        cipher=SecretCipher.from_settings(deps.settings) if cipher else None,
        now=NOW,
    )


def test_rollback_does_not_leave_an_intention_and_missing_cipher_never_mints_on_replay(deps):
    import pytest

    from app.handoff_types import HandoffError

    with deps.sessions.begin() as session:
        spec, _ = configured(session)
    with pytest.raises(RuntimeError), deps.sessions.begin() as session:
        rolled_back = accept(session, spec, deps)
        raise RuntimeError("rollback sintético")
    with deps.sessions() as session, pytest.raises(HandoffError):
        handoffs.get_handoff(session, rolled_back.submission.id)
    with deps.sessions.begin() as session:
        accepted = accept(session, spec, deps, cipher=False)
    with deps.sessions.begin() as session:
        replay = accept(session, spec, deps)
        assert replay.replay
        view = handoffs.completion_view(
            session,
            replay.submission,
            spec,
            cipher=SecretCipher.from_settings(deps.settings),
            now=NOW,
        )
        assert view.handoff_status == "unavailable" and view.url is None
        assert (
            handoffs.get_handoff(session, accepted.submission.id).unavailable_code
            == "cipher_unavailable"
        )


def test_cipher_key_rotation_revision_change_and_revocation_never_retarget(deps):
    import pytest
    from cryptography.fernet import Fernet

    from app.handoff_types import HandoffError, Revoke

    with deps.sessions.begin() as session:
        spec, created = configured(session)
        accepted = accept(session, spec, deps)
    old_keys = deps.settings.encryption_keys
    cipher = SecretCipher((Fernet.generate_key().decode(), *old_keys))
    with deps.sessions.begin() as session:
        first = handoffs.completion_view(session, accepted.submission, spec, cipher=cipher, now=NOW)
        same = handoffs.completion_view(
            session, accepted.submission, spec, cipher=SecretCipher(old_keys), now=NOW
        )
        assert first.url == same.url
        principal = integration_credentials.authenticate_credential(
            session, created.secret, now=NOW
        )
        raw = SecretStr(first.url.split("leadkit_ref=")[1])
        info = handoffs.revoke_handoff(
            session,
            accepted.submission.id,
            command=Revoke(expected_version="1", confirm=True),
            actor="teste",
            now=NOW,
        )
        assert info.revoked_at == NOW
        with pytest.raises(HandoffError, match="handoff_unavailable"):
            handoffs.resolve_handoff(session, principal, raw, now=NOW)
        assert (
            handoffs.completion_view(session, accepted.submission, spec, cipher=cipher, now=NOW).url
            is None
        )
        receiver = handoff_receivers.get_receiver(session, spec.slug)
        saved = handoff_receivers.save_receiver(
            session,
            spec.slug,
            command=SaveReceiver(
                expected_version=receiver.version,
                name=receiver.name,
                entry_url="https://example.test/novo",
                reference_ttl_seconds=3600,
                confirm=True,
            ),
            actor="teste",
            now=NOW,
        )
        assert saved.revision == 2 and saved.enabled is False
        assert (
            handoffs.completion_view(session, accepted.submission, spec, cipher=cipher, now=NOW).url
            is None
        )


def test_expiry_removes_ciphertext_and_submission_purge_removes_the_reference(deps):
    import pytest

    from app.handoff_types import HandoffError
    from app.models import HandoffReference
    from app.retention import purge_expired

    with deps.sessions.begin() as session:
        spec, created = configured(session)
        accepted = accept(session, spec, deps)
        view = handoffs.completion_view(
            session,
            accepted.submission,
            spec,
            cipher=SecretCipher.from_settings(deps.settings),
            now=NOW,
        )
        principal = integration_credentials.authenticate_credential(
            session, created.secret, now=NOW
        )
    expiry = NOW + timedelta(hours=1)
    with deps.sessions.begin() as session:
        with pytest.raises(HandoffError, match="handoff_unavailable"):
            handoffs.resolve_handoff(
                session, principal, SecretStr(view.url.split("leadkit_ref=")[1]), now=expiry
            )
        assert handoffs.expire_ciphertexts(session, now=expiry) == 1
        assert session.query(HandoffReference).one().token_ciphertext is None
    report = purge_expired(deps, now=NOW + timedelta(days=400))
    assert report.captures_deleted == 1
    with deps.sessions() as session, pytest.raises(HandoffError, match="not_found"):
        handoffs.get_handoff(session, accepted.submission.id)


def test_concurrent_replay_and_reference_revocation_follow_the_same_lock_order(deps):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    import pytest

    from app.handoff_types import HandoffError, Revoke

    with deps.sessions.begin() as session:
        spec, created = configured(session)
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=5)
        with deps.sessions.begin() as session:
            accepted = accept(session, spec, deps)
            return accepted.submission.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: submit(), range(2)))
    assert ids[0] == ids[1]
    with deps.sessions.begin() as session:
        submission = session.get(Submission, ids[0])
        view = handoffs.completion_view(
            session, submission, spec, cipher=SecretCipher.from_settings(deps.settings), now=NOW
        )
        principal = integration_credentials.authenticate_credential(
            session, created.secret, now=NOW
        )
    barrier = Barrier(2)

    def resolve():
        barrier.wait(timeout=5)
        try:
            with deps.sessions.begin() as session:
                return handoffs.resolve_handoff(
                    session, principal, SecretStr(view.url.split("leadkit_ref=")[1]), now=NOW
                ).submission_id
        except HandoffError as error:
            assert error.code == "handoff_unavailable"

    def revoke():
        barrier.wait(timeout=5)
        with deps.sessions.begin() as session:
            return handoffs.revoke_handoff(
                session,
                ids[0],
                command=Revoke(expected_version="1", confirm=True),
                actor="teste",
                now=NOW,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.submit(resolve), pool.submit(revoke)
        assert right.result(timeout=10).revoked_at == NOW
        assert left.result(timeout=10) in (None, ids[0])
    with deps.sessions.begin() as session, pytest.raises(HandoffError, match="handoff_unavailable"):
        handoffs.resolve_handoff(
            session, principal, SecretStr(view.url.split("leadkit_ref=")[1]), now=NOW
        )


def test_resolver_and_capture_retention_do_not_invert_locks_or_recreate_data(deps):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    import pytest

    from app.handoff_types import HandoffError
    from app.retention import purge_expired

    with deps.sessions.begin() as session:
        spec, created = configured(session)
        accepted = accept(session, spec, deps)
        view = handoffs.completion_view(
            session,
            accepted.submission,
            spec,
            cipher=SecretCipher.from_settings(deps.settings),
            now=NOW,
        )
        principal = integration_credentials.authenticate_credential(
            session, created.secret, now=NOW
        )
    barrier = Barrier(2)

    def resolve():
        barrier.wait(timeout=5)
        try:
            with deps.sessions.begin() as session:
                return handoffs.resolve_handoff(
                    session, principal, SecretStr(view.url.split("leadkit_ref=")[1]), now=NOW
                ).submission_id
        except HandoffError as error:
            assert error.code == "handoff_unavailable"

    def purge():
        barrier.wait(timeout=5)
        return purge_expired(deps, now=NOW + timedelta(days=400))

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.submit(resolve), pool.submit(purge)
        first_purge = right.result(timeout=10)
        assert left.result(timeout=10) in (None, accepted.submission.id)
    # A retenção usa SKIP LOCKED: um registro em leitura pode ficar para a próxima passada.
    second_purge = purge_expired(deps, now=NOW + timedelta(days=400))
    assert first_purge.captures_deleted + second_purge.captures_deleted == 1
    with deps.sessions.begin() as session, pytest.raises(HandoffError, match="handoff_unavailable"):
        handoffs.resolve_handoff(
            session, principal, SecretStr(view.url.split("leadkit_ref=")[1]), now=NOW
        )


def test_new_receiver_revision_cannot_resolve_or_retarget_an_old_reference(deps):
    import pytest

    from app.handoff_types import HandoffError

    with deps.sessions.begin() as session:
        spec, _ = configured(session)
        accepted = accept(session, spec, deps)
        original = handoffs.completion_view(
            session,
            accepted.submission,
            spec,
            cipher=SecretCipher.from_settings(deps.settings),
            now=NOW,
        )
        saved = handoff_receivers.save_receiver(
            session,
            "synthetic",
            command=SaveReceiver(
                expected_version="2",
                name="Novo receptor",
                entry_url="https://example.test/novo",
                reference_ttl_seconds=3600,
                confirm=True,
            ),
            actor="teste",
            now=NOW,
        )
        created = integration_credentials.create_credential(
            session,
            "synthetic",
            command=CreateCredential(
                name="Nova revisão",
                receiver_revision=2,
                expected_receiver_version=saved.version,
                expires_at=None,
                idempotency_key=uuid4(),
                confirm=True,
            ),
            actor="teste",
            now=NOW,
        )
        handoff_receivers.set_receiver_enabled(
            session,
            "synthetic",
            command=SetReceiverEnabled(expected_version=saved.version, enabled=True, confirm=True),
            actor="teste",
            cipher=SecretCipher.from_settings(deps.settings),
            now=NOW,
        )
        principal = integration_credentials.authenticate_credential(
            session, created.secret, now=NOW
        )
        assert (
            handoffs.completion_view(
                session,
                accepted.submission,
                spec,
                cipher=SecretCipher.from_settings(deps.settings),
                now=NOW,
            ).url
            is None
        )
        with pytest.raises(HandoffError, match="handoff_unavailable"):
            handoffs.resolve_handoff(
                session, principal, SecretStr(original.url.split("leadkit_ref=")[1]), now=NOW
            )
