"""Upgrade0017→0018 com vínculos existentes e assinaturas históricas congeladas."""

import subprocess
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from sqlalchemy.orm import Session
from test_baseline import config
from test_baseline import fresh as fresh  # noqa: PLC0414
from test_handoffs import NOW, configured

from app.migrations.compatibility import validate_database
from app.migrations.contracts import SIGNATURES
from app.models import EventCredential, EventSource, HandoffReceiver, JourneyEvent


def test_previous_schema_signatures_are_byte_identical():
    old = subprocess.run(
        ["git", "show", "37f0755b8f97d5f9ccc0e96a0e8a36185fc9e42e:app/migrations/contracts.py"],
        capture_output=True,
        check=False,
    )
    assert old.returncode == 0
    current = Path("app/migrations/contracts.py").read_bytes()
    assert current.startswith(old.stdout)


def test_additive_upgrade_preserves_authority_and_floor_without_seeded_sources(fresh):
    command.upgrade(config(), "0017_continuation_release")
    with Session(fresh) as session, session.begin():
        _, created = configured(session)
        receiver = session.scalar(sa.select(HandoffReceiver))
        receiver_id = receiver.id
        credential_id = created.credential.id
        before = session.execute(
            sa.text(
                "select minimum_contract, phase, epoch, evidence, attested_at from deployment_contract"
            )
        ).one()
    command.upgrade(config(), "0018_journey_events")
    with Session(fresh) as session:
        from app import integration_credentials

        assert validate_database(session.connection(), SIGNATURES) == "0018_journey_events"
        assert (
            session.execute(
                sa.text(
                    "select minimum_contract, phase, epoch, evidence, attested_at from deployment_contract"
                )
            ).one()
            == before
        )
        assert (
            integration_credentials.authenticate_credential(
                session, created.secret, now=NOW
            ).credential_id
            == credential_id
        )
        assert session.get(HandoffReceiver, receiver_id).enabled is True
        assert session.scalar(sa.select(sa.func.count()).select_from(EventSource)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(EventCredential)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(JourneyEvent)) == 0
    command.upgrade(config(), "head")
