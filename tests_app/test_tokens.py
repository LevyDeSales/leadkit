from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.tokens import TokenError, TokenSigner, new_public_id

SECRET_A = "a" * 40
SECRET_B = "b" * 40


@dataclass
class FakeJob:
    public_id: str
    token_version: int = 1
    expires_at: datetime = datetime.now(UTC) + timedelta(days=30)


def test_issued_token_verifies_and_is_stable() -> None:
    signer = TokenSigner((SECRET_A,))
    job = FakeJob(public_id=new_public_id())
    token = signer.issue(job.public_id, job.token_version)
    assert token == signer.issue(job.public_id, job.token_version)
    assert TokenSigner.public_id_of(token) == job.public_id
    signer.verify(token, job)


@pytest.mark.parametrize("token", ["", "sem-ponto", ".so-assinatura", "x." + "a" * 200])
def test_malformed_tokens(token: str) -> None:
    with pytest.raises(TokenError) as error:
        TokenSigner.public_id_of(token)
    assert error.value.reason == "malformed"


def test_wrong_signature_and_non_ascii_are_invalid_not_server_errors() -> None:
    signer = TokenSigner((SECRET_A,))
    job = FakeJob(public_id="abc")
    for token in ("abc.assinaturaerrada", "abc.é-não-ascii"):
        with pytest.raises(TokenError) as error:
            signer.verify(token, job)
        assert error.value.reason == "invalid"


def test_token_of_another_job_is_invalid() -> None:
    signer = TokenSigner((SECRET_A,))
    token = signer.issue("outro", 1)
    with pytest.raises(TokenError):
        signer.verify(token, FakeJob(public_id="abc"))


def test_revocation_by_version_and_expiry() -> None:
    signer = TokenSigner((SECRET_A,))
    job = FakeJob(public_id="abc")
    token = signer.issue("abc", 1)
    job.token_version = 2
    with pytest.raises(TokenError) as revoked:
        signer.verify(token, job)
    assert revoked.value.reason == "invalid"

    job = FakeJob(public_id="abc", expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(TokenError) as expired:
        signer.verify(token, job)
    assert expired.value.reason == "expired"


def test_rotation_accepts_tokens_signed_with_previous_secret() -> None:
    old = TokenSigner((SECRET_A,))
    rotated = TokenSigner((SECRET_B, SECRET_A))
    job = FakeJob(public_id="abc")
    rotated.verify(old.issue("abc", 1), job)
    assert rotated.issue("abc", 1) != old.issue("abc", 1)


def test_short_secrets_are_refused() -> None:
    with pytest.raises(ValueError):
        TokenSigner(("curto",))
