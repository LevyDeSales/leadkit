"""API e worker comprovam compatibilidade antes de preparar destinos ou aceitar trabalho."""

import pytest
from fastapi.testclient import TestClient

from app import destinations
from app.api.main import create_app
from app.deployment_contract import DeploymentContractError
from app.models import DeploymentContract
from app.worker import ready_deps


@pytest.mark.parametrize("runtime", ["api", "worker"])
def test_future_floor_stops_runtime_before_configuration_writes(deps, monkeypatch, runtime):
    with deps.sessions.begin() as session:
        previous_floor = session.get_one(DeploymentContract, 1).minimum_contract
        session.get_one(DeploymentContract, 1).minimum_contract = 3
    prepared = []
    monkeypatch.setattr(destinations, "prepare", lambda deps: prepared.append(True))
    try:
        with pytest.raises(DeploymentContractError):
            if runtime == "api":
                with TestClient(create_app()):
                    pass
            else:
                ready_deps()
        assert prepared == []
    finally:
        with deps.sessions.begin() as session:
            session.get_one(DeploymentContract, 1).minimum_contract = previous_floor


@pytest.mark.parametrize("runtime", ["api", "worker"])
def test_contract_one_runtime_stops_at_composed_floor(deps, monkeypatch, runtime):
    from app import deployment_contract

    with deps.sessions.begin() as session:
        previous_floor = session.get_one(DeploymentContract, 1).minimum_contract
        session.get_one(DeploymentContract, 1).minimum_contract = 2
    monkeypatch.setattr(deployment_contract, "BINARY_CONTRACT", 1)
    prepared = []
    monkeypatch.setattr(destinations, "prepare", lambda deps: prepared.append(True))
    try:
        with pytest.raises(DeploymentContractError):
            if runtime == "api":
                with TestClient(create_app()):
                    pass
            else:
                ready_deps()
        assert prepared == []
    finally:
        with deps.sessions.begin() as session:
            session.get_one(DeploymentContract, 1).minimum_contract = previous_floor
