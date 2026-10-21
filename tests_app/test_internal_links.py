"""Capacidade interna separada do token público e revogável pela revisão do receptor."""

from datetime import timedelta
from uuid import uuid4

import pytest

from app.internal_links import InternalLinkClaims, InternalLinkSigner
from app.models import utcnow
from app.tokens import TokenError, TokenSigner


def test_internal_signature_is_domain_separated_and_rotation_preserves_validation():
    now = utcnow()
    claims = InternalLinkClaims(
        job_public_id="synthetic-job",
        attempt=1,
        artifact_id=uuid4(),
        token_version=1,
        receiver_kind="webhook",
        receiver_id=uuid4(),
        receiver_version=1,
        expires_at=int((now + timedelta(minutes=5)).timestamp()),
    )
    signer = InternalLinkSigner(("synthetic-signing-key-for-internal-links-0001",))
    token = signer.issue(claims)
    assert signer.verify(token, now=now) == claims
    rotated = InternalLinkSigner(
        ("synthetic-signing-key-for-internal-links-0002", *signer.secrets_)
    )
    assert rotated.verify(token, now=now) == claims
    with pytest.raises(TokenError):
        signer.verify(TokenSigner(signer.secrets_).issue("synthetic-job", 1), now=now)
    with pytest.raises(TokenError):
        signer.verify(token, now=now + timedelta(minutes=6))


@pytest.fixture
def manual_pdf(request):
    # Reutiliza a jornada publicada e a atestação reais, mantendo a limpeza do contrato.
    from test_plan_release import manual_pdf as fixture

    yield from fixture.__wrapped__(request.getfixturevalue("deps"), request)


def test_internal_pdf_is_accessible_while_held_and_revocation_is_checked_in_service(
    deps, manual_pdf
):
    from fastapi.testclient import TestClient

    from app import destinations
    from app.api.main import create_app
    from app.internal_links import internal_links
    from app.models import Artifact, Job

    job_id, artifact_id = manual_pdf
    with deps.sessions.begin() as session:
        saved = destinations.save(
            session,
            deps.cipher,
            form_slug="manual",
            purpose="internal",
            url="https://example.test/internal",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
        )
        job = session.get_one(Job, job_id)
        artifact = session.get_one(Artifact, artifact_id)
        links = internal_links(
            session,
            deps,
            job,
            artifact,
            receiver_kind="webhook",
            receiver_id=saved.destination.id,
            receiver_version=saved.destination.version,
        )
        token = deps.signer.issue(job.public_id, job.token_version)
    client = TestClient(create_app())
    assert client.get(links["pdf_url"]).status_code == 200
    public = client.get(f"/v1/jobs/{job.public_id}/pdf", params={"t": token})
    assert public.status_code == 403
    assert client.get(f"/i/jobs/{job.public_id}/pdf", params={"k": token}).status_code == 404
    with deps.sessions.begin() as session:
        destinations.rotate(
            session, deps.cipher, form_slug="manual", actor="operadora", expected_version=1
        )
    assert client.get(links["pdf_url"]).status_code == 404


def test_internal_pdf_holds_receiver_authorization_until_bytes_are_read(
    deps, manual_pdf, monkeypatch
):
    import threading
    from urllib.parse import parse_qs, urlsplit

    from app import destinations
    from app.admin.actions.jobs import pdf_file
    from app.internal_links import InternalPdfAccess, internal_links
    from app.models import Artifact, Job

    job_id, artifact_id = manual_pdf
    with deps.sessions.begin() as session:
        saved = destinations.save(
            session,
            deps.cipher,
            form_slug="manual",
            purpose="internal",
            url="https://example.test/internal",
            actor="operadora",
            expected_version=0,
            production=False,
            http_confirmed=False,
        )
        job = session.get_one(Job, job_id)
        links = internal_links(
            session,
            deps,
            job,
            session.get_one(Artifact, artifact_id),
            receiver_kind="webhook",
            receiver_id=saved.destination.id,
            receiver_version=1,
        )
    token = parse_qs(urlsplit(links["pdf_url"]).query)["k"][0]
    claims = InternalLinkSigner(deps.signer.secrets_).verify(token, now=utcnow())
    reading, resume, changing, changed = (threading.Event() for _ in range(4))
    original = deps.storage.get

    def held_read(key):
        reading.set()
        assert resume.wait(5)
        return original(key)

    monkeypatch.setattr(deps.storage, "get", held_read)
    errors, results = [], []

    def read():
        try:
            results.append(
                pdf_file(
                    deps, job.public_id, None, signed_url=False, access=InternalPdfAccess(claims)
                )
            )
        except Exception as error:  # noqa: BLE001 — captura falha da thread para a asserção principal
            errors.append(type(error).__name__)

    def revoke():
        changing.set()
        with deps.sessions.begin() as session:
            destinations.rotate(
                session, deps.cipher, form_slug="manual", actor="operadora", expected_version=1
            )
        changed.set()

    reader = threading.Thread(target=read)
    writer = threading.Thread(target=revoke)
    reader.start()
    try:
        assert reading.wait(3)
        writer.start()
        assert changing.wait(2)
        assert not changed.wait(0.15)
    finally:
        resume.set()
        reader.join(5)
        if writer.ident:
            writer.join(5)
    assert changed.is_set() and not errors
    assert results[0].content.startswith(b"%PDF")
    with pytest.raises(TokenError):
        pdf_file(deps, job.public_id, None, signed_url=False, access=InternalPdfAccess(claims))
