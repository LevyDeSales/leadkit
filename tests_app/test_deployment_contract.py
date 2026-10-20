"""Contrato persistido de rollout: banco real, CLI da imagem e concorrência."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from conftest import TEST_DATABASE_URL, reset_test_schema
from sqlalchemy.orm import Session

from app.db import make_engine


@pytest.fixture(scope="module")
def rollout_engine():
    reset_test_schema(TEST_DATABASE_URL)
    command.upgrade(Config(str(Path(__file__).parents[1] / "app/alembic.ini")), "head")
    engine = make_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def contract_session(rollout_engine):
    with Session(rollout_engine) as session, session.begin():
        yield session
        session.rollback()


def test_migrated_database_blocks_participant_but_allows_compatible_runtime(contract_session):
    from app.deployment_contract import (
        DeploymentContractError,
        assert_participant_contract,
        assert_runtime_contract,
    )

    assert_runtime_contract(contract_session)
    with pytest.raises(DeploymentContractError, match="atualização"):
        assert_participant_contract(contract_session)


def evidence():
    """Manifesto sintético calculado a partir de duas réplicas conhecidas."""

    def service(role, digit):
        image = "sha256:" + digit * 64
        return {
            "id": role,
            "spec_version": 4,
            "observed_spec_version": 4,
            "image": image,
            "contract": 1,
            "previous_image": image,
            "previous_contract": 1,
            "replicas": 1,
            "tasks": [
                {
                    "id": role + "-task",
                    "node_id": "local-node",
                    "container_id": role + "-container",
                    "image": image,
                    "state": "running",
                    "healthy": True,
                    "running": True,
                }
            ],
            "retired": [],
        }

    return {
        "schema_version": 1,
        "stack": "leadkit-test",
        "node_id": "local-node",
        "services": {"api": service("api", "a"), "worker": service("worker", "b")},
    }


def test_attestation_requires_current_epoch_and_complete_fleet(contract_session):
    from uuid import uuid4

    from app.deployment_contract import (
        DeploymentContractError,
        assert_participant_contract,
        attest_rollout,
        begin_rollout,
    )

    epoch = uuid4()
    begin_rollout(contract_session, epoch=epoch)
    with pytest.raises(DeploymentContractError):
        attest_rollout(contract_session, epoch=uuid4(), evidence=evidence())
    with pytest.raises(DeploymentContractError):
        assert_participant_contract(contract_session)
    attest_rollout(contract_session, epoch=epoch, evidence=evidence())
    assert_participant_contract(contract_session)
    begin_rollout(contract_session, epoch=uuid4())
    with pytest.raises(DeploymentContractError):
        assert_participant_contract(contract_session)


@pytest.mark.parametrize(
    "change",
    [
        "old_current",
        "old_previous",
        "running_old",
        "missing_replica",
        "remote_node",
        "spec_drift",
        "extra",
    ],
)
def test_incomplete_or_mixed_evidence_never_enables_participant(contract_session, change):
    from uuid import uuid4

    from app.deployment_contract import (
        DeploymentContractError,
        assert_participant_contract,
        attest_rollout,
        begin_rollout,
    )

    manifest = evidence()
    service = manifest["services"]["worker"]
    if change == "old_current":
        service["contract"] = 0
    if change == "old_previous":
        service["previous_contract"] = 0
    if change == "running_old":
        service["retired"] = [{**service["tasks"][0], "id": "old-task", "state": "shutdown"}]
    if change == "missing_replica":
        service["replicas"] = 2
    if change == "remote_node":
        service["tasks"][0]["node_id"] = "other-node"
    if change == "spec_drift":
        service["observed_spec_version"] = 5
    if change == "extra":
        manifest["Env"] = ["private-value"]
    epoch = uuid4()
    begin_rollout(contract_session, epoch=epoch)
    with pytest.raises(DeploymentContractError) as error:
        attest_rollout(contract_session, epoch=epoch, evidence=manifest)
    assert "private-value" not in str(error.value)
    with pytest.raises(DeploymentContractError):
        assert_participant_contract(contract_session)


def test_capability_needs_neither_configuration_nor_database():
    import json
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "app.deployment_contract", "capability"],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"contract": 1}


@pytest.mark.parametrize(
    "document", ['{"private":"private-input"}', '{"epoch":{}}', '{"epoch":42}']
)
def test_cli_does_not_echo_invalid_input_or_configuration(document):
    import json
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "app.deployment_contract", "begin"],
        env={"PATH": os.environ["PATH"], "DATABASE_URL": "private-invalid-value"},
        input=document,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "deployment_contract_blocked"
    assert "private-" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_missing_table_and_missing_singleton_never_enable_participant(contract_session):
    import sqlalchemy as sa

    from app.deployment_contract import (
        DeploymentContractError,
        assert_participant_contract,
        assert_runtime_contract,
    )
    from app.models import DeploymentContract

    contract_session.execute(sa.delete(DeploymentContract))
    assert_runtime_contract(contract_session)
    with pytest.raises(DeploymentContractError):
        assert_participant_contract(contract_session)
    contract_session.execute(sa.text("DROP TABLE deployment_contract"))
    assert_runtime_contract(contract_session)
    with pytest.raises(DeploymentContractError):
        assert_participant_contract(contract_session)


def test_future_floor_blocks_runtime_without_lowering_it(contract_session):
    from uuid import uuid4

    import sqlalchemy as sa

    from app.deployment_contract import (
        DeploymentContractError,
        assert_runtime_contract,
        begin_rollout,
    )
    from app.models import DeploymentContract

    contract_session.execute(sa.update(DeploymentContract).values(minimum_contract=2))
    with pytest.raises(DeploymentContractError):
        assert_runtime_contract(contract_session)
    with pytest.raises(DeploymentContractError):
        begin_rollout(contract_session, epoch=uuid4())


def test_begin_waits_until_authorized_transport_transaction_finishes(rollout_engine):
    import threading
    from uuid import uuid4

    from app.deployment_contract import (
        DeploymentContractError,
        assert_participant_contract,
        attest_rollout,
        begin_rollout,
    )
    from app.models import DeploymentContract

    with Session(rollout_engine) as session, session.begin():
        epoch = uuid4()
        begin_rollout(session, epoch=epoch)
        attest_rollout(session, epoch=epoch, evidence=evidence())
    started = threading.Event()
    finished = threading.Event()
    errors = []

    def close_gate():
        try:
            with Session(rollout_engine) as session, session.begin():
                started.set()
                begin_rollout(session, epoch=uuid4())
        except DeploymentContractError as error:
            errors.append(type(error).__name__)
        finally:
            finished.set()

    try:
        with Session(rollout_engine) as participant, participant.begin():
            assert_participant_contract(participant)
            worker = threading.Thread(target=close_gate)
            worker.start()
            assert started.wait(2)
            assert not finished.wait(0.15)
        assert finished.wait(3)
        worker.join()
        assert errors == []
    finally:
        with Session(rollout_engine) as session, session.begin():
            session.query(DeploymentContract).delete()
            session.add(DeploymentContract(id=1, minimum_contract=1, phase="blocked"))
