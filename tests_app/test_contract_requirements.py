"""Requisitos de produto independentes da capacidade do binário composto."""

from uuid import uuid4

import pytest
from test_deployment_contract import evidence

from app import deployment_contract as contract
from app.models import DeploymentContract


def test_verified_floor_one_does_not_enable_new_operations(deps):
    with deps.sessions() as session:
        epoch = uuid4()
        contract.begin_rollout(session, epoch=epoch)
        contract.attest_rollout(session, epoch=epoch, evidence=evidence())
        contract.assert_participant_contract(session)
        with pytest.raises(contract.DeploymentContractError):
            contract.assert_contract(session, required=2)


def test_floor_two_requires_both_current_and_previous_capabilities(deps):
    with deps.sessions() as session:
        session.get_one(DeploymentContract, 1).minimum_contract = 2
        epoch = uuid4()
        contract.begin_rollout(session, epoch=epoch)
        with pytest.raises(contract.DeploymentContractError):
            contract.attest_rollout(session, epoch=epoch, evidence=evidence())
        manifest = evidence()
        for service in manifest["services"].values():
            service["contract"] = 2
            service["previous_contract"] = 2
        contract.attest_rollout(session, epoch=epoch, evidence=manifest)
        contract.assert_contract(session, required=2)
        contract.assert_participant_contract(session)
        assert session.get_one(DeploymentContract, 1).minimum_contract == 2


@pytest.mark.parametrize("required", [0, 3, True, "2"])
def test_unknown_requirement_fails_closed(deps, required):
    with deps.sessions() as session, pytest.raises(contract.DeploymentContractError):
        contract.assert_contract(session, required=required)


def test_attestation_never_promotes_floor_from_fleet_capabilities(deps):
    with deps.sessions() as session:
        epoch = uuid4()
        contract.begin_rollout(session, epoch=epoch)
        manifest = evidence()
        for service in manifest["services"].values():
            service["contract"] = 2
            service["previous_contract"] = 2
        contract.attest_rollout(session, epoch=epoch, evidence=manifest)
        contract.assert_participant_contract(session)
        with pytest.raises(contract.DeploymentContractError):
            contract.assert_contract(session, required=2)
        assert session.get_one(DeploymentContract, 1).minimum_contract == 1


@pytest.mark.parametrize("field", ["contract", "previous_contract"])
def test_floor_two_rejects_either_old_fleet_configuration(deps, field):
    with deps.sessions() as session:
        session.get_one(DeploymentContract, 1).minimum_contract = 2
        epoch = uuid4()
        contract.begin_rollout(session, epoch=epoch)
        manifest = evidence()
        for service in manifest["services"].values():
            service["contract"] = 2
            service["previous_contract"] = 2
        manifest["services"]["api"][field] = 1
        with pytest.raises(contract.DeploymentContractError):
            contract.attest_rollout(session, epoch=epoch, evidence=manifest)
        with pytest.raises(contract.DeploymentContractError):
            contract.assert_participant_contract(session)
