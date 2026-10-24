"""Adoção do contrato de respostas conserva dados e bloqueia retornos antigos."""

import pytest
import sqlalchemy as sa
from alembic import command
from conftest import TEST_DATABASE_URL, reset_test_schema
from test_migrations import alembic


def test_v2_upgrade_installs_stable_issuer_and_blocked_floor(database_url):
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


def test_failed_v2_upgrade_rolls_back_schema_issuer_and_floor(database_url):
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


def test_original_runtime_two_guard_refuses_persisted_floor_three(deps, tmp_path):
    import importlib.util
    import subprocess

    source = subprocess.check_output(
        ["git", "show", "ecc009f501d157e3ee246d916e8a13b0cb69e629:app/deployment_contract.py"]
    )
    path = tmp_path / "deployment_contract_original.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("deployment_contract_original55", path)
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    assert original.BINARY_CONTRACT == 2
    with deps.sessions.begin() as session:
        # Isola o cenário do piso novo das fixtures históricas que exercitam pisos1/2.
        previous = session.scalar(sa.text("SELECT minimum_contract FROM deployment_contract"))
        session.execute(sa.text("UPDATE deployment_contract SET minimum_contract=3"))
        with pytest.raises(original.DeploymentContractError):
            original.assert_runtime_contract(session)
        session.execute(
            sa.text("UPDATE deployment_contract SET minimum_contract=:floor"), {"floor": previous}
        )
