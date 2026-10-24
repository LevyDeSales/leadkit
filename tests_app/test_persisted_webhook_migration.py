"""Adoção do contrato de respostas conserva dados e bloqueia retornos antigos."""

import pytest
import sqlalchemy as sa
from alembic import command
from conftest import TEST_DATABASE_URL, reset_test_schema
from test_migrations import alembic


def test_v2_upgrade_installs_stable_issuer_and_blocked_floor():
    reset_test_schema(TEST_DATABASE_URL)
    command.upgrade(alembic(), "0018_journey_events")
    engine = sa.create_engine(TEST_DATABASE_URL)
    try:
        command.upgrade(alembic(), "head")
        with engine.connect() as connection:
            assert connection.execute(
                sa.text("SELECT minimum_contract, phase, epoch FROM deployment_contract")
            ).one() == (3, "blocked", None)
            issuer = connection.scalar(sa.text("SELECT namespace FROM webhook_issuer"))
            assert issuer is not None
        command.upgrade(alembic(), "head")
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT namespace FROM webhook_issuer")) == issuer
    finally:
        engine.dispose()


def test_failed_v2_upgrade_rolls_back_schema_issuer_and_floor():
    from app.migrations.compatibility import schema_signature
    from app.migrations.contracts import SIGNATURES

    reset_test_schema(TEST_DATABASE_URL)
    command.upgrade(alembic(), "0018_journey_events")

    def fail_revision(conn, cursor, statement, parameters, context, many):
        if statement.startswith("UPDATE alembic_version"):
            raise RuntimeError("falha sintética de migration")

    sa.event.listen(sa.engine.Engine, "before_cursor_execute", fail_revision)
    try:
        with pytest.raises(RuntimeError, match="falha sintética"):
            command.upgrade(alembic(), "head")
    finally:
        sa.event.remove(sa.engine.Engine, "before_cursor_execute", fail_revision)
    engine = sa.create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            assert schema_signature(connection) == SIGNATURES["0018_journey_events"]
            assert (
                connection.scalar(sa.text("SELECT minimum_contract FROM deployment_contract")) == 2
            )
        command.upgrade(alembic(), "head")
    finally:
        engine.dispose()
