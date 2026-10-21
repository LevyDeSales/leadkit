"""Emissão única, validade explícita, escopo e revogação pela API de domínio."""

from datetime import timedelta
from uuid import uuid4

import pytest
from handoff_support import isolated_handoff_contract  # noqa: F401
from pydantic import SecretStr, ValidationError
from test_continuation_models import contract_session as contract_session  # noqa: PLC0414
from test_handoffs import NOW, configured

from app import integration_credentials as credentials
from app.handoff_types import CreateCredential, HandoffError, Revoke, RotateCredential


def test_expiry_choice_is_required_but_null_is_valid():
    with pytest.raises(ValidationError):
        CreateCredential(
            name="Teste",
            receiver_revision=1,
            expected_receiver_version="1",
            idempotency_key=uuid4(),
        )


def test_credential_secret_is_usable_once_and_rotation_revokes_the_old_one(contract_session):
    session = contract_session
    _, issued = configured(session)
    assert issued.credential.expires_at is None
    assert issued.model_dump(mode="json")["secret"].startswith("lki_")
    principal = credentials.authenticate_credential(session, issued.secret, now=NOW)
    command = RotateCredential(
        expected_version="1",
        expires_at=NOW + timedelta(hours=1),
        idempotency_key=uuid4(),
        confirm=True,
    )
    successor = credentials.rotate_credential(
        session, principal.credential_id, command=command, actor="teste", now=NOW
    )
    with pytest.raises(HandoffError, match="invalid_credentials"):
        credentials.authenticate_credential(session, issued.secret, now=NOW)
    assert (
        credentials.authenticate_credential(session, successor.secret, now=NOW).receiver_id
        == principal.receiver_id
    )
    with pytest.raises(HandoffError, match="credential_secret_already_issued") as caught:
        credentials.rotate_credential(
            session, principal.credential_id, command=command, actor="teste", now=NOW
        )
    assert caught.value.credential_id == successor.credential.id
    with pytest.raises(HandoffError, match="idempotency_conflict"):
        credentials.rotate_credential(
            session,
            principal.credential_id,
            command=command.model_copy(update={"expires_at": None}),
            actor="teste",
            now=NOW,
        )
    with pytest.raises(HandoffError, match="invalid_credentials"):
        credentials.authenticate_credential(session, successor.secret, now=NOW + timedelta(hours=1))
    result = credentials.revoke_credential(
        session,
        successor.credential.id,
        command=Revoke(expected_version="1", confirm=True),
        actor="teste",
        now=NOW,
    )
    assert result.state == "revoked"
    with pytest.raises(HandoffError, match="invalid_credentials"):
        credentials.authenticate_credential(session, successor.secret, now=NOW)


@pytest.mark.parametrize(
    "value", ["gp_<admin>_<secret>", "lkh_<reference>", "i1.<claims>.<signature>", "receipt", ""]
)
def test_other_capability_namespaces_never_authenticate(contract_session, value):
    with pytest.raises(HandoffError, match="invalid_credentials"):
        credentials.authenticate_credential(contract_session, SecretStr(value), now=NOW)


def test_concurrent_creation_returns_one_secret_and_a_safe_replay_conflict(deps):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with deps.sessions.begin() as session:
        configured(session)
    command = CreateCredential(
        name="Concorrência",
        receiver_revision=1,
        expected_receiver_version="2",
        expires_at=None,
        idempotency_key=uuid4(),
        confirm=True,
    )
    barrier = Barrier(2)

    def issue():
        barrier.wait(timeout=5)
        try:
            with deps.sessions.begin() as session:
                return credentials.create_credential(
                    session, "synthetic", command=command, actor="concorrência", now=NOW
                ).credential.id
        except HandoffError as error:
            assert error.code == "credential_secret_already_issued"
            return error.credential_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: issue(), range(2)))
    assert ids[0] == ids[1]
    with deps.sessions() as session:
        assert len(credentials.list_credentials(session, "synthetic", now=NOW)) == 2
