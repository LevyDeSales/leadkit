"""Adoção composta confirma schema e piso no mesmo commit PostgreSQL."""

import sqlalchemy as sa
from alembic import command
from conftest import TEST_DATABASE_URL, reset_test_schema
from test_migrations import alembic

from app.migrations.compatibility import schema_signature
from app.migrations.contracts import SIGNATURES


def test_composed_upgrade_blocks_new_contract_and_preserves_previous_objects():
    reset_test_schema(TEST_DATABASE_URL)
    command.upgrade(alembic(), "0016_plan_audiences")
    engine = sa.create_engine(TEST_DATABASE_URL)
    try:
        with engine.begin() as connection:
            before = schema_signature(connection)
            connection.execute(
                sa.text("UPDATE deployment_contract SET phase='blocked', minimum_contract=1")
            )
        command.upgrade(alembic(), "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == "0017_continuation_release"
            )
            assert connection.execute(
                sa.text(
                    "SELECT minimum_contract, phase, epoch, evidence, attested_at FROM deployment_contract"
                )
            ).one() == (2, "blocked", None, {}, None)
            after = schema_signature(connection)
            assert all(after[key] == value for key, value in before.items())
            assert after == SIGNATURES["0017_continuation_release"]
        command.upgrade(alembic(), "head")
    finally:
        engine.dispose()


def test_failed_upgrade_rolls_back_schema_floor_and_revision():
    reset_test_schema(TEST_DATABASE_URL)
    command.upgrade(alembic(), "0016_plan_audiences")
    engine = sa.create_engine(TEST_DATABASE_URL)

    def fail_before_revision(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE alembic_version"):
            raise RuntimeError("falha sintética antes do commit")

    sa.event.listen(sa.engine.Engine, "before_cursor_execute", fail_before_revision)
    try:
        import pytest

        with pytest.raises(RuntimeError, match="falha sintética"):
            command.upgrade(alembic(), "head")
    finally:
        sa.event.remove(sa.engine.Engine, "before_cursor_execute", fail_before_revision)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == "0016_plan_audiences"
            )
            assert (
                connection.scalar(sa.text("SELECT minimum_contract FROM deployment_contract")) == 1
            )
            assert schema_signature(connection) == SIGNATURES["0016_plan_audiences"]
        command.upgrade(alembic(), "head")
    finally:
        engine.dispose()
