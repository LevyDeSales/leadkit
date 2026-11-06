"""Recuperação explícita pelo helper real, com Docker/banco sintéticos nas seams externas."""

import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import UnixStreamServer

import pytest

from deploy.swarm.rollout import Rollout, RolloutError

CURRENT = "sha256:" + "a" * 64
LOST = "sha256:" + "b" * 64
OTHER = "sha256:" + "c" * 64


class DockerFixture:
    """Modela update de Spec idêntica sem recriar tasks; não é prova do Engine."""

    def __init__(self):
        self.calls = []
        self.updates = []
        self.services = {}
        self.tasks = {}
        self.containers = {}
        self.state = {
            "schema": "0020_draft_test_commands",
            "minimum_contract": 3,
            "phase": "verified",
            "epoch": str(uuid.uuid4()),
            "attested_at": "2026-01-01T00:00:00Z",
            "evidence_valid": True,
            "evidence_hash": "d" * 64,
            "evidence_scope": {
                "stack": "fixture",
                "node": "local-node",
                "images": [CURRENT, CURRENT],
            },
        }
        for role in ("api", "worker"):
            spec = {
                "Name": "fixture_" + role,
                "TaskTemplate": {
                    "ContainerSpec": {"Image": CURRENT, "Env": ["KEY=PRIVATE"]},
                    "Runtime": "container",
                    "Networks": [{"Target": "fixture-network"}],
                },
                "Mode": {"Replicated": {"Replicas": 1}},
                "EndpointSpec": {"Mode": "vip"},
            }
            previous = copy.deepcopy(spec)
            previous["TaskTemplate"]["ContainerSpec"]["Image"] = LOST
            self.services[role] = {
                "ID": role,
                "Version": {"Index": 10},
                "Spec": spec,
                "PreviousSpec": previous,
                "Endpoint": {
                    "Spec": {"Mode": "vip"},
                    "VirtualIPs": [
                        {
                            "NetworkID": "fixture-network",
                            "Addr": "198.51.100." + ("1" if role == "api" else "2") + "/24",
                        }
                    ],
                },
            }
            cid = hashlib.sha256(role.encode()).hexdigest()
            self.tasks[role] = {
                "ID": role + "-task",
                "ServiceID": role,
                "NodeID": "local-node",
                "DesiredState": "running",
                "Spec": copy.deepcopy(spec["TaskTemplate"]),
                "NetworksAttachments": [
                    {
                        "Network": {"ID": "fixture-network", "Spec": {"Name": "fixture-internal"}},
                        "Addresses": ["198.51.100." + ("10" if role == "api" else "11") + "/24"],
                    }
                ],
                "Status": {"State": "running", "ContainerStatus": {"ContainerID": cid}},
            }
            # TaskFromGRPC omite Runtime para container; ServiceSpec o explicita.
            self.tasks[role]["Spec"].pop("Runtime")
            self.containers[cid] = {
                "Id": cid,
                "Image": CURRENT,
                "RestartCount": 0,
                "State": {
                    "Running": True,
                    "StartedAt": "2026-01-01T00:00:00Z",
                    "Health": {"Status": "healthy"},
                },
                "Config": {"Labels": {"com.docker.swarm.task.id": role + "-task"}},
                "NetworkSettings": {
                    "Networks": {
                        "fixture-internal": {
                            "NetworkID": "fixture-network",
                            "EndpointID": hashlib.sha256((role + "-endpoint").encode()).hexdigest(),
                            "IPAddress": "198.51.100." + ("10" if role == "api" else "11"),
                            "IPPrefixLen": 24,
                            "GlobalIPv6Address": "",
                            "GlobalIPv6PrefixLen": 0,
                        }
                    }
                },
            }

    def service_projection(self, role, *, view="engine"):
        value = copy.deepcopy(self.services[role])
        if getattr(self, "insert_defaults", False):
            # SwarmKit docker-v29.4.2 interpola somente Spec, não PreviousSpec.
            # Resources ausente vira ResourceRequirements vazio na projeção API.
            value["Spec"]["TaskTemplate"].setdefault("Resources", {})
        if getattr(self, "projection_fault", None):
            self.projection_fault(view, value)
        return value

    def run(self, command, *, input=None, **kwargs):
        args = command[1:]
        self.calls.append(args)
        head = args[:2]
        value = None
        code = 0
        if head == ["image", "inspect"]:
            if args[-1] == LOST:
                return subprocess.CompletedProcess(command, 1, "", "No such image")
            value = OTHER if args[-1] == OTHER else CURRENT
        elif head == ["image", "tag"]:
            pass
        elif args[0] == "info":
            value = "local-node"
        elif head == ["node", "inspect"]:
            value = [{"Status": {"State": "ready"}, "ManagerStatus": {"Reachability": "reachable"}}]
        elif head == ["network", "inspect"]:
            value = []
        elif head == ["service", "inspect"]:
            value = [self.service_projection(args[-1].rsplit("_", 1)[-1], view="cli")]
        elif head == ["service", "ps"]:
            role = args[-1].rsplit("_", 1)[-1]
            value = "\n".join(
                task["ID"] for task in self.tasks.values() if task["ServiceID"] == role
            )
        elif args[0] == "inspect":
            kind = args[args.index("--type") + 1]
            names = args[args.index("--type") + 2 :]
            value = (
                [next(task for task in self.tasks.values() if task["ID"] == name) for name in names]
                if kind == "task"
                else [self.containers[name] for name in names]
            )
        elif args[0] == "ps":
            role = args[-1].rsplit("_", 1)[-1]
            value = "\n".join(
                task["Status"]["ContainerStatus"]["ContainerID"]
                for task in self.tasks.values()
                if task["ServiceID"] == role
            )
        elif args[0] == "run":
            if args[-1] == "capability":
                value = {"contract": getattr(self, "capability", 3)}
            elif args[-1] == "status":
                value = {key: self.state[key] for key in ("minimum_contract", "phase", "epoch")}
            elif args[-1] == "begin":
                if getattr(self, "begin_failure", False):
                    raise RolloutError("begin recusado")
                self.state.update(
                    phase="blocked",
                    epoch=json.loads(input)["epoch"],
                    attested_at=None,
                    evidence_valid=False,
                    evidence_hash=None,
                    evidence_scope=None,
                )
                value = {"ok": True}
            elif args[-1] == "attest":
                from app.deployment_contract import _validate_evidence

                if getattr(self, "attest_before_failure", False):
                    raise RolloutError("Interrompido antes de attest")
                if getattr(self, "attest_exit", False):
                    raise SystemExit(87)
                doc = json.loads(input)
                assert doc["epoch"] == self.state["epoch"]
                _validate_evidence(doc["evidence"], required=3)
                self.state.update(
                    phase="verified",
                    attested_at="2026-11-06T00:00:00Z",
                    evidence_valid=True,
                    evidence_hash=hashlib.sha256(
                        json.dumps(doc["evidence"], sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                    evidence_scope={
                        "stack": "fixture",
                        "node": "local-node",
                        "images": [CURRENT, CURRENT],
                    },
                )
                if getattr(self, "attest_response_lost", False):
                    raise RolloutError("Resposta de attest perdida")
                value = {"ok": True}
            elif args[-2:] == ["python", "-"]:
                value = self.state
            else:
                raise AssertionError(args)
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(
            command, code, (value if isinstance(value, str) else json.dumps(value)) + "\n", ""
        )


@pytest.fixture
def docker(monkeypatch):
    fixture = DockerFixture()
    monkeypatch.setattr("deploy.swarm.rollout.subprocess.run", fixture.run)
    return fixture


def test_normal_deploy_still_refuses_lost_previous_before_begin(tmp_path, docker):
    with pytest.raises(RolloutError):
        Rollout(tmp_path, CURRENT, "fixture", False).execute()
    assert docker.state["phase"] == "verified"
    assert not docker.updates
    assert not any(
        "begin" in call or "migrate" in call or call[:2] == ["stack", "deploy"]
        for call in docker.calls
    )


@pytest.fixture
def recovery_client(tmp_path, docker, monkeypatch):
    from deploy.swarm import recovery

    class Engine:
        def __init__(self, rollout):
            self.node = "local-node"
            self.identity = {
                "daemon": "fixture-daemon",
                "node": self.node,
                "endpoint": "unix:///fixture.sock",
            }

        def confirm(self):
            pass

        def service(self, name, *, insert_defaults=False):
            role = name.rsplit("_", 1)[-1]
            return (
                docker.service_projection(role)
                if insert_defaults
                else copy.deepcopy(docker.services[role])
            )

        def image(self, ref):
            if getattr(docker, "alias_collision", False) and ref.startswith("leadkit-local:"):
                return OTHER
            if getattr(docker, "image_failure", False) and ref == LOST:
                raise RolloutError("Falha de daemon")
            if getattr(docker, "history_available", False) and ref == LOST:
                return LOST
            if ref == LOST or (
                getattr(docker, "missing_alias", False) and ref.startswith("leadkit-local:")
            ):
                raise recovery.ImageMissing
            return OTHER if ref == OTHER else CURRENT

        def container(self, cid):
            return copy.deepcopy(docker.containers[cid])

        def update(self, service_id, version, spec):
            docker.update_requests = getattr(docker, "update_requests", [])
            docker.update_requests.append((service_id, version, spec))
            if isinstance(spec, bytes):
                spec = json.loads(spec)
            service = docker.services[service_id]
            if getattr(docker, "before_update", None):
                try:
                    docker.before_update(service_id)
                except (recovery.UpdateNotSent, recovery.UpdateRejected, recovery.UpdateUnknown):
                    raise
                except RolloutError:
                    raise recovery.UpdateNotSent("Fixture interrompida antes do POST") from None
            if getattr(docker, "unclassified_update", False):
                raise RolloutError("Erro de adapter não classificado")
            if service["Version"]["Index"] != version:
                raise recovery.UpdateRejected("CAS recusada")
            docker.updates.append(service_id)
            service["PreviousSpec"] = copy.deepcopy(service["Spec"])
            service["Spec"] = copy.deepcopy(spec)
            service["Version"]["Index"] += 1
            if getattr(docker, "after_update", None):
                try:
                    docker.after_update(service_id)
                except RolloutError:
                    raise recovery.UpdateUnknown("Fixture perdeu a resposta após commit") from None

    monkeypatch.setattr(recovery, "EngineUnix", Engine)

    def run():
        rollout = Rollout(tmp_path, CURRENT, "fixture", False)
        return recovery.Recovery(
            rollout, CURRENT, {"api": LOST, "worker": LOST}, tmp_path / "recovery.json"
        ).execute()

    return run


def test_recovery_confirms_defaulted_projection_but_posts_original_raw_spec(
    tmp_path, docker, recovery_client
):
    from deploy.swarm.recovery import canonical, fingerprint

    docker.insert_defaults = True
    original = copy.deepcopy(docker.services)
    tasks = copy.deepcopy(docker.tasks)
    assert docker.service_projection("api")["Spec"] != original["api"]["Spec"]
    recovery_client()
    assert docker.update_requests == [
        (role, 10, canonical(original[role]["Spec"])) for role in ("api", "worker")
    ]
    receipt = json.loads((tmp_path / "recovery.json").read_text())
    for role in ("api", "worker"):
        assert receipt["baseline"][role]["spec"] == fingerprint(original[role]["Spec"])
        assert docker.services[role]["PreviousSpec"] == original[role]["Spec"]
        assert "Resources" not in docker.services[role]["Spec"]["TaskTemplate"]
    assert docker.tasks == tasks
    assert docker.state["phase"] == "verified"


@pytest.mark.parametrize("view", ["engine", "cli"])
@pytest.mark.parametrize("field", ["ID", "Version"])
def test_recovery_refuses_projection_identity_drift_before_begin(
    tmp_path, docker, recovery_client, view, field
):
    docker.insert_defaults = True

    def drift(source, value):
        if source == view:
            value[field] = "foreign-service" if field == "ID" else {"Index": 11}

    docker.projection_fault = drift
    with pytest.raises(RolloutError, match="identidade ou versão"):
        recovery_client()
    assert docker.state["phase"] == "verified"
    assert not (tmp_path / "recovery.json").exists()
    assert not docker.updates
    assert not any("begin" in call for call in docker.calls)


@pytest.mark.parametrize("field", ["Spec", "PreviousSpec"])
@pytest.mark.parametrize(
    "damage",
    [
        "absent",
        "null",
        "empty",
        "list",
        "name",
        "task_template",
        "container_spec",
        "image",
        "mode",
        "replicated",
        "replicas",
        "boolean_replicas",
    ],
)
def test_incomplete_projections_are_refused_before_begin(
    tmp_path, docker, recovery_client, field, damage
):
    docker.insert_defaults = True

    def incomplete(source, value):
        if damage == "absent":
            del value[field]
        elif damage == "null":
            value[field] = None
        elif damage == "empty":
            value[field] = {}
        elif damage == "list":
            value[field] = []
        elif damage == "boolean_replicas":
            value[field]["Mode"]["Replicated"]["Replicas"] = True
        else:
            paths = {
                "name": ["Name"],
                "task_template": ["TaskTemplate"],
                "container_spec": ["TaskTemplate", "ContainerSpec"],
                "image": ["TaskTemplate", "ContainerSpec", "Image"],
                "mode": ["Mode"],
                "replicated": ["Mode", "Replicated"],
                "replicas": ["Mode", "Replicated", "Replicas"],
            }
            target = value[field]
            for key in paths[damage][:-1]:
                target = target[key]
            del target[paths[damage][-1]]

    docker.projection_fault = incomplete
    with pytest.raises(RolloutError):
        recovery_client()
    assert docker.state["phase"] == "verified"
    assert not (tmp_path / "recovery.json").exists()
    assert not any("begin" in call for call in docker.calls)
    assert not docker.updates


def test_defaulted_projections_cannot_mask_a_different_raw_current(
    tmp_path, docker, recovery_client
):
    docker.insert_defaults = True
    docker.services["worker"]["Spec"]["TaskTemplate"]["ContainerSpec"]["Image"] = OTHER

    def mask(source, value):
        value["Spec"]["TaskTemplate"]["ContainerSpec"]["Image"] = CURRENT

    docker.projection_fault = mask
    with pytest.raises(RolloutError, match="Current dos dois papéis"):
        recovery_client()
    assert not docker.updates
    assert not (tmp_path / "recovery.json").exists()
    assert not any("begin" in call for call in docker.calls)


@pytest.mark.parametrize("field", ["Spec", "PreviousSpec"])
@pytest.mark.parametrize("fault", ["env", "service", "image", "default"])
def test_recovery_refuses_full_cli_projection_difference_before_begin(
    tmp_path, docker, recovery_client, field, fault
):
    docker.insert_defaults = True

    def mismatch(source, value):
        if source != "cli":
            return
        spec = value[field]
        if fault == "env":
            spec["TaskTemplate"]["ContainerSpec"]["Env"] = ["KEY=OTHER_PRIVATE"]
        elif fault == "service":
            spec["Name"] = "fixture_other"
        elif fault == "image":
            spec["TaskTemplate"]["ContainerSpec"]["Image"] = OTHER
        else:
            spec["TaskTemplate"]["Resources"] = {"Limits": {"MemoryBytes": 1024}}

    docker.projection_fault = mismatch
    with pytest.raises(RolloutError, match="projeção com defaults") as error:
        recovery_client()
    assert "PRIVATE" not in str(error.value)
    assert docker.state["phase"] == "verified"
    assert not (tmp_path / "recovery.json").exists()
    assert not docker.updates
    assert not any("begin" in call for call in docker.calls)


@pytest.mark.parametrize("field", ["ID", "Version"])
def test_recovery_does_not_adopt_new_baseline_shared_by_both_projections(
    tmp_path, docker, recovery_client, field
):
    docker.insert_defaults = True

    def writer(source, value):
        value[field] = "new-service" if field == "ID" else {"Index": 11}

    docker.projection_fault = writer
    with pytest.raises(RolloutError, match="identidade ou versão"):
        recovery_client()
    assert not docker.updates
    assert not (tmp_path / "recovery.json").exists()
    assert not any("begin" in call for call in docker.calls)


def test_public_task_shape_recovers_with_service_endpoint_and_raw_cas(
    tmp_path, docker, recovery_client
):
    from deploy.swarm.recovery import canonical

    original = copy.deepcopy(docker.services)
    tasks, containers = copy.deepcopy(docker.tasks), copy.deepcopy(docker.containers)
    assert all("Endpoint" not in task and "SpecVersion" not in task for task in tasks.values())
    assert all("Runtime" not in task["Spec"] for task in tasks.values())
    recovery_client()
    assert docker.update_requests == [
        (role, 10, canonical(original[role]["Spec"])) for role in ("api", "worker")
    ]
    assert (docker.tasks, docker.containers) == (tasks, containers)
    assert docker.state["phase"] == "verified"
    assert (tmp_path / "recovery.json").exists()
    assert "198.51.100" not in (tmp_path / "recovery.json").read_text()


def test_public_vip_networks_support_multiple_networks_and_dual_stack(docker, recovery_client):
    for role in ("api", "worker"):
        service = docker.services[role]
        task = docker.tasks[role]
        service["Spec"]["TaskTemplate"]["Networks"].append({"Target": "fixture-two"})
        task["Spec"]["Networks"] = copy.deepcopy(service["Spec"]["TaskTemplate"]["Networks"])
        suffix = "10" if role == "api" else "11"
        vip = "1" if role == "api" else "2"
        service["Endpoint"]["VirtualIPs"].extend(
            [
                {"NetworkID": "fixture-two", "Addr": "203.0.113." + vip + "/24"},
                {"NetworkID": "fixture-two", "Addr": "2001:db8:1::" + vip + "/64"},
            ]
        )
        task["NetworksAttachments"].append(
            {
                "Network": {"ID": "fixture-two", "Spec": {"Name": "fixture-two"}},
                "Addresses": ["203.0.113." + suffix + "/24", "2001:db8:1::" + suffix + "/64"],
            }
        )
        cid = task["Status"]["ContainerStatus"]["ContainerID"]
        docker.containers[cid]["NetworkSettings"]["Networks"]["fixture-two"] = {
            "NetworkID": "fixture-two",
            "EndpointID": hashlib.sha256((role + "-two").encode()).hexdigest(),
            "IPAddress": "203.0.113." + suffix,
            "IPPrefixLen": 24,
            "GlobalIPv6Address": "2001:db8:1::" + suffix,
            "GlobalIPv6PrefixLen": 64,
        }
    before = copy.deepcopy(docker.tasks), copy.deepcopy(docker.containers)
    recovery_client()
    assert docker.state["phase"] == "verified"
    assert (docker.tasks, docker.containers) == before


def test_public_vip_cidr_prefix_does_not_define_the_task_subnet(docker, recovery_client):
    # O contrato EndpointVirtualIP usa o IP; o prefixo é compatibilidade de formato.
    docker.services["api"]["Endpoint"]["VirtualIPs"][0]["Addr"] = "198.51.100.1/32"
    recovery_client()
    assert docker.state["phase"] == "verified"


@pytest.mark.parametrize("runtime", ["", "container"])
def test_explicit_container_runtime_representation_is_supported(docker, recovery_client, runtime):
    docker.tasks["api"]["Spec"]["Runtime"] = runtime
    recovery_client()
    assert docker.state["phase"] == "verified"


@pytest.mark.parametrize(
    "fault",
    [
        "runtime_unknown",
        "runtime_null",
        "runtime_missing_raw",
        "runtime_plugin",
        "runtime_attachment",
        "container_spec_missing",
        "container_spec_null",
        "endpoint_missing",
        "endpoint_null",
        "endpoint_empty",
        "endpoint_spec_missing",
        "endpoint_spec_mismatch",
        "endpoint_dnsrr",
        "desired_ports",
        "allocated_ports",
        "task_ports",
        "vips_missing",
        "vips_empty",
        "vip_extra_network",
        "vip_duplicate",
        "vip_invalid_address",
        "vip_duplicate_family",
        "desired_networks_empty",
        "desired_network_duplicate",
        "attachments_missing",
        "attachments_empty",
        "attachment_extra",
        "attachment_duplicate",
        "attachment_addresses_missing",
        "attachment_ingress",
        "container_networks_missing",
        "container_networks_empty",
        "container_extra_network",
        "container_duplicate_network",
        "container_endpoint_missing",
        "container_endpoint_invalid",
        "container_wrong_address",
        "container_wrong_prefix",
        "container_extra_address",
        "task_container_extra_address",
        "container_ipam_extra",
        "container_ipv6_null",
        "container_empty_family_prefix",
        "attachment_ingress_invalid",
    ],
)
def test_public_endpoint_network_runtime_fail_closed_before_begin(
    tmp_path, docker, recovery_client, fault
):
    service = docker.services["api"]
    task = docker.tasks["api"]
    container = docker.containers[task["Status"]["ContainerStatus"]["ContainerID"]]
    network = container["NetworkSettings"]["Networks"]["fixture-internal"]
    endpoint = service["Endpoint"]
    if fault == "runtime_unknown":
        task["Spec"]["Runtime"] = "PRIVATE"
    elif fault == "runtime_null":
        task["Spec"]["Runtime"] = None
    elif fault == "runtime_missing_raw":
        del service["Spec"]["TaskTemplate"]["Runtime"]
    elif fault == "runtime_plugin":
        task["Spec"]["PluginSpec"] = {}
    elif fault == "runtime_attachment":
        task["Spec"]["NetworkAttachmentSpec"] = {}
    elif fault == "container_spec_missing":
        del task["Spec"]["ContainerSpec"]
    elif fault == "container_spec_null":
        task["Spec"]["ContainerSpec"] = None
    elif fault == "endpoint_missing":
        del service["Endpoint"]
    elif fault == "endpoint_null":
        service["Endpoint"] = None
    elif fault == "endpoint_empty":
        service["Endpoint"] = {}
    elif fault == "endpoint_spec_missing":
        del endpoint["Spec"]
    elif fault == "endpoint_spec_mismatch":
        endpoint["Spec"]["Mode"] = "dnsrr"
    elif fault == "endpoint_dnsrr":
        service["Spec"]["EndpointSpec"]["Mode"] = endpoint["Spec"]["Mode"] = "dnsrr"
    elif fault == "desired_ports":
        service["Spec"]["EndpointSpec"]["Ports"] = [{"TargetPort": 8080}]
        endpoint["Spec"] = copy.deepcopy(service["Spec"]["EndpointSpec"])
    elif fault == "allocated_ports":
        endpoint["Ports"] = [{"TargetPort": 8080}]
    elif fault == "task_ports":
        task["Status"]["PortStatus"] = {"Ports": [{"TargetPort": 8080}]}
    elif fault == "vips_missing":
        del endpoint["VirtualIPs"]
    elif fault == "vips_empty":
        endpoint["VirtualIPs"] = []
    elif fault == "vip_extra_network":
        endpoint["VirtualIPs"].append({"NetworkID": "other", "Addr": "203.0.113.1/24"})
    elif fault == "vip_duplicate":
        endpoint["VirtualIPs"] *= 2
    elif fault == "vip_invalid_address":
        endpoint["VirtualIPs"][0]["Addr"] = "PRIVATE"
    elif fault == "vip_duplicate_family":
        endpoint["VirtualIPs"].append({"NetworkID": "fixture-network", "Addr": "198.51.100.3/24"})
    elif fault in {"desired_networks_empty", "desired_network_duplicate"}:
        values = [] if fault == "desired_networks_empty" else [{"Target": "fixture-network"}] * 2
        service["Spec"]["TaskTemplate"]["Networks"] = values
        task["Spec"]["Networks"] = copy.deepcopy(values)
    elif fault == "attachments_missing":
        del task["NetworksAttachments"]
    elif fault == "attachments_empty":
        task["NetworksAttachments"] = []
    elif fault == "attachment_extra":
        extra = copy.deepcopy(task["NetworksAttachments"][0])
        extra["Network"]["ID"] = "other"
        task["NetworksAttachments"].append(extra)
    elif fault == "attachment_duplicate":
        task["NetworksAttachments"] *= 2
    elif fault == "attachment_addresses_missing":
        del task["NetworksAttachments"][0]["Addresses"]
    elif fault == "attachment_ingress":
        task["NetworksAttachments"][0]["Network"]["Spec"]["Ingress"] = True
    elif fault == "container_networks_missing":
        del container["NetworkSettings"]
    elif fault == "container_networks_empty":
        container["NetworkSettings"]["Networks"] = {}
    elif fault in {"container_extra_network", "container_duplicate_network"}:
        extra = copy.deepcopy(network)
        if fault == "container_extra_network":
            extra["NetworkID"] = "other"
        container["NetworkSettings"]["Networks"]["other"] = extra
    elif fault == "container_endpoint_missing":
        del network["EndpointID"]
    elif fault == "container_endpoint_invalid":
        network["EndpointID"] = "PRIVATE"
    elif fault == "container_wrong_address":
        network["IPAddress"] = "198.51.100.12"
    elif fault == "container_wrong_prefix":
        network["IPPrefixLen"] = 25
    elif fault in {"container_extra_address", "task_container_extra_address"}:
        network.update(GlobalIPv6Address="2001:db8::10", GlobalIPv6PrefixLen=64)
        if fault == "task_container_extra_address":
            task["NetworksAttachments"][0]["Addresses"].append("2001:db8::10/64")
    elif fault == "container_ipam_extra":
        network["IPAMConfig"] = {"LinkLocalIPs": ["169.254.1.1"]}
    elif fault == "container_ipv6_null":
        network["GlobalIPv6Address"] = None
    elif fault == "container_empty_family_prefix":
        network["GlobalIPv6PrefixLen"] = 64
    elif fault == "attachment_ingress_invalid":
        task["NetworksAttachments"][0]["Network"]["Spec"]["Ingress"] = 0
    with pytest.raises(RolloutError) as error:
        recovery_client()
    assert "PRIVATE" not in str(error.value)
    assert "198.51." not in str(error.value)
    assert docker.state["phase"] == "verified"
    assert not (tmp_path / "recovery.json").exists()
    assert not any("begin" in call for call in docker.calls)
    assert not docker.updates


@pytest.mark.parametrize("view", ["engine", "cli"])
def test_endpoint_drift_between_same_version_views_is_refused(
    tmp_path, docker, recovery_client, view
):
    def drift(source, value):
        if source == view:
            value["Endpoint"]["VirtualIPs"][0]["Addr"] = "198.51.100.3/24"

    docker.projection_fault = drift
    with pytest.raises(RolloutError, match="endpoint público"):
        recovery_client()
    assert docker.state["phase"] == "verified"
    assert not docker.updates
    assert not (tmp_path / "recovery.json").exists()


@pytest.mark.parametrize("change", ["vip", "endpoint_id", "task_and_container_address"])
def test_public_network_facts_are_frozen_across_updates(docker, recovery_client, change):
    def changed(role):
        if role != "api":
            return
        service = docker.services["api"]
        task = docker.tasks["api"]
        network = docker.containers[task["Status"]["ContainerStatus"]["ContainerID"]][
            "NetworkSettings"
        ]["Networks"]["fixture-internal"]
        if change == "vip":
            service["Endpoint"]["VirtualIPs"][0]["Addr"] = "198.51.100.3/24"
        elif change == "endpoint_id":
            network["EndpointID"] = "f" * 64
        else:
            task["NetworksAttachments"][0]["Addresses"] = ["198.51.100.12/24"]
            network["IPAddress"] = "198.51.100.12"

    docker.after_update = changed
    with pytest.raises(RolloutError):
        recovery_client()
    assert docker.state["phase"] == "blocked"
    assert not any("attest" in call for call in docker.calls)


def test_recovery_replaces_lost_history_without_restarting_tasks(tmp_path, docker, recovery_client):
    before = copy.deepcopy(docker.tasks), copy.deepcopy(docker.containers)
    receipt = tmp_path / "recovery.json"
    recovery_client()
    assert docker.updates == ["api", "worker"]
    assert (docker.tasks, docker.containers) == before
    assert docker.state["phase"] == "verified"
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE" not in receipt.read_text()
    assert json.loads(receipt.read_text())["result"] == "current_replaces_lost_history"
    assert not any(
        "migrate" in call or "--force" in call or call[:2] == ["stack", "deploy"]
        for call in docker.calls
    )


@pytest.mark.parametrize(
    "interruption", ["before_api", "after_api", "before_worker", "after_worker"]
)
def test_reentry_reconciles_partial_or_lost_commit_without_duplicate_update(
    tmp_path, docker, recovery_client, interruption
):
    def fail(role):
        if interruption.endswith(role):
            raise RolloutError("Resposta perdida; commit desconhecido")

    setattr(docker, "before_update" if interruption.startswith("before") else "after_update", fail)
    with pytest.raises(RolloutError):
        recovery_client()
    assert docker.state["phase"] == "blocked"
    docker.before_update = docker.after_update = None
    recovery_client()
    assert docker.updates == ["api", "worker"]
    assert docker.state["phase"] == "verified"
    assert json.loads((tmp_path / "recovery.json").read_text())["stage"] == "done"
    recovery_client()
    assert docker.updates == ["api", "worker"]


def test_reentry_refuses_foreign_epoch(docker, recovery_client):
    docker.before_update = lambda role: (_ for _ in ()).throw(RolloutError("Interrompido"))
    with pytest.raises(RolloutError):
        recovery_client()
    docker.state["epoch"] = str(uuid.uuid4())
    docker.before_update = None
    with pytest.raises(RolloutError):
        recovery_client()
    assert docker.state["phase"] == "blocked"
    assert not docker.updates


@pytest.mark.parametrize("damage", ["version", "spec", "tasks", "previous"])
def test_reentry_refuses_unrecognized_state(docker, recovery_client, damage):
    docker.before_update = lambda role: (_ for _ in ()).throw(RolloutError("Interrompido"))
    with pytest.raises(RolloutError):
        recovery_client()
    docker.before_update = None
    service = docker.services["api"]
    if damage == "version":
        service["Version"]["Index"] += 1
    elif damage == "spec":
        service["Spec"]["Labels"] = {"unexpected": "writer"}
    elif damage == "tasks":
        next(iter(docker.containers.values()))["RestartCount"] += 1
    else:
        service["PreviousSpec"]["TaskTemplate"]["ContainerSpec"]["Image"] = OTHER
    with pytest.raises(RolloutError):
        recovery_client()
    assert docker.state["phase"] == "blocked"
    assert not docker.updates


@pytest.mark.parametrize(
    "fault",
    [
        "restart",
        "unhealthy",
        "dirty",
        "endpoint",
        "other_image",
        "schema",
        "floor",
        "blocked",
        "evidence",
        "capability",
        "previous",
        "alias",
        "image_error",
        "history_exists",
    ],
)
def test_preconditions_refuse_before_begin(docker, recovery_client, fault):
    if fault == "restart":
        next(iter(docker.containers.values()))["RestartCount"] = 1
    elif fault == "unhealthy":
        next(iter(docker.containers.values()))["State"]["Health"]["Status"] = "unhealthy"
    elif fault == "dirty":
        docker.tasks["api"]["Spec"]["ContainerSpec"]["Env"] = ["OTHER=PRIVATE"]
    elif fault == "endpoint":
        docker.services["api"]["Endpoint"]["Spec"] = {"Mode": "dnsrr"}
    elif fault == "other_image":
        docker.services["worker"]["Spec"]["TaskTemplate"]["ContainerSpec"]["Image"] = OTHER
    elif fault == "schema":
        docker.state["schema"] = "0019"
    elif fault == "floor":
        docker.state["minimum_contract"] = 2
    elif fault == "blocked":
        docker.state["phase"] = "blocked"
    elif fault == "evidence":
        docker.state["evidence_valid"] = False
    elif fault == "capability":
        docker.capability = 2
    elif fault == "previous":
        docker.services["api"]["PreviousSpec"]["TaskTemplate"]["ContainerSpec"]["Image"] = OTHER
    elif fault == "alias":
        docker.alias_collision = True
    elif fault == "image_error":
        docker.image_failure = True
    else:
        docker.history_available = True
    with pytest.raises(RolloutError):
        recovery_client()
    assert not docker.updates
    assert not any("begin" in call for call in docker.calls)


def test_begin_failure_leaves_receipt_and_never_updates(docker, recovery_client, tmp_path):
    docker.begin_failure = True
    with pytest.raises(RolloutError):
        recovery_client()
    assert not docker.updates
    assert (tmp_path / "recovery.json").is_file()
    docker.begin_failure = False
    recovery_client()
    assert docker.state["phase"] == "verified"


def test_recovery_requires_preserved_current_alias(docker, recovery_client):
    docker.missing_alias = True
    with pytest.raises(RolloutError):
        recovery_client()
    assert not any("begin" in call for call in docker.calls)


def test_oci_fixture_proof_accepts_runtime_config_in_complete_graph(tmp_path):
    from swarm_recovery_scenario import oci_proof

    blobs = {}

    def blob(value, media):
        data = json.dumps(value).encode()
        digest = hashlib.sha256(data).hexdigest()
        blobs["blobs/sha256/" + digest] = data
        return {"digest": "sha256:" + digest, "size": len(data), "mediaType": media}

    config = blob(
        {"config": {"Env": ["KEY=PRIVATE"]}, "rootfs": {"type": "layers", "diff_ids": []}},
        "application/vnd.oci.image.config.v1+json",
    )
    manifest = blob(
        {"mediaType": "application/vnd.oci.image.manifest.v1+json", "config": config, "layers": []},
        "application/vnd.oci.image.manifest.v1+json",
    )
    platform = {**manifest, "platform": {"os": "linux", "architecture": "amd64"}}
    attestation = {**manifest, "annotations": {"vnd.docker.reference.type": "attestation-manifest"}}
    root = blob(
        {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [platform, attestation],
        },
        "application/vnd.oci.image.index.v1+json",
    )
    root["annotations"] = {
        "io.containerd.image.name": "docker.io/library/leadkit-recovery80:current"
    }
    blobs["index.json"] = json.dumps(
        {"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [root]}
    ).encode()
    archive = tmp_path / "fixture.tar"
    with tarfile.open(archive, "w") as stream:
        for name, data in blobs.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            stream.addfile(entry, io.BytesIO(data))
    oci_proof(archive)


@pytest.mark.parametrize(
    "returncode,phase", [(1, "verified"), (86, "verified"), (0, "blocked"), (87, "blocked")]
)
def test_cut_checkpoint_explains_failure_without_accepting_wrong_exit_or_gate(
    tmp_path, capsys, returncode, phase
):
    import swarm_recovery_scenario as scenario

    message = "Uma task está suja em relação à Current ou endpoint."
    child = subprocess.CompletedProcess(
        ["SYNTHETIC_PRIVATE_ARG"],
        returncode,
        "SYNTHETIC_PRIVATE_STDOUT",
        "deploy.sh: " + message + "\nSYNTHETIC_PRIVATE_STDERR\n",
    )
    with pytest.raises(AssertionError):
        scenario.assert_cut(
            "after_first",
            child,
            lambda: {"phase": phase, "minimum_contract": 3},
            tmp_path / "missing.json",
        )
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["cutpoint"] == "after_first" and data["returncode"] == returncode
    assert data["expected_returncode"] == 86 and data["phase"] == phase
    assert data["helper_error"]["message"] == message
    assert data["receipt"]["status"] == "absent"
    assert "SYNTHETIC_PRIVATE" not in output


@pytest.mark.parametrize("cutpoint,returncode", [("after_first", 86), ("before_attest", 87)])
def test_cut_checkpoint_selects_only_receipt_metadata_and_preserves_success(
    tmp_path, capsys, cutpoint, returncode
):
    import swarm_recovery_scenario as scenario

    receipt = tmp_path / "recovery.json"
    receipt.write_text(
        json.dumps(
            {
                "format": 2,
                "stage": "begun",
                "roles": {
                    "api": {
                        "phase": "inflight",
                        "request_result": "unknown",
                        "Env": "SYNTHETIC_PRIVATE",
                    },
                    "worker": {"phase": "pending", "request_result": "not_attempted"},
                },
                "Spec": "SYNTHETIC_PRIVATE",
                "epoch": "SYNTHETIC_PRIVATE",
            }
        )
    )
    receipt.chmod(0o600)
    child = subprocess.CompletedProcess([], returncode, "", "")
    scenario.assert_cut(
        cutpoint, child, lambda: {"phase": "blocked", "minimum_contract": 3}, receipt
    )
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["receipt"]["stage"] == "begun"
    assert data["receipt"]["roles"] == {
        "api": {"phase": "inflight", "request_result": "unknown"},
        "worker": {"phase": "pending", "request_result": "not_attempted"},
    }
    assert data["helper_error"] is None
    assert "SYNTHETIC_PRIVATE" not in output and "Spec" not in output and "Env" not in output


@pytest.mark.parametrize("unsafe", ["oversized", "symlink", "invalid_values", "invalid_json"])
def test_cut_checkpoint_bounds_and_sanitizes_untrusted_evidence(tmp_path, capsys, unsafe):
    import swarm_recovery_scenario as scenario

    receipt = tmp_path / "recovery.json"
    if unsafe == "symlink":
        target = tmp_path / "private.json"
        target.write_text("SYNTHETIC_PRIVATE")
        receipt.symlink_to(target)
    elif unsafe == "invalid_values":
        receipt.write_text(
            json.dumps(
                {
                    "stage": "SYNTHETIC_PRIVATE",
                    "roles": {
                        "api": {"phase": "SYNTHETIC_PRIVATE", "request_result": "SYNTHETIC_PRIVATE"}
                    },
                }
            )
        )
    else:
        receipt.write_text("SYNTHETIC_PRIVATE" * (4000 if unsafe == "oversized" else 1))
    if not receipt.is_symlink():
        receipt.chmod(0o600)
    child = subprocess.CompletedProcess(
        [], 1, "SYNTHETIC_PRIVATE" * 1000, "SYNTHETIC_PRIVATE" * 1000
    )
    with pytest.raises(AssertionError):
        scenario.assert_cut("after_first", child, lambda: {"phase": "SYNTHETIC_PRIVATE"}, receipt)
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["phase"] == "invalid" and data["helper_error"] is None
    assert data["stderr"] == data["stdout"] == "oversized"
    assert (
        data["receipt"]["status"]
        == {
            "oversized": "oversized",
            "symlink": "unsafe",
            "invalid_values": "selected",
            "invalid_json": "invalid",
        }[unsafe]
    )
    assert "SYNTHETIC_PRIVATE" not in output and len(output) < 4096


@pytest.mark.parametrize(
    "stderr",
    [
        "deploy.sh: Uma task está suja em relação à Current ou endpoint. SYNTHETIC_PRIVATE",
        "Traceback: SYNTHETIC_PRIVATE",
        '{"Env":"SYNTHETIC_PRIVATE"}',
    ],
)
def test_cut_checkpoint_never_echoes_unknown_short_stderr(tmp_path, capsys, stderr):
    import swarm_recovery_scenario as scenario

    with pytest.raises(AssertionError):
        scenario.assert_cut(
            "after_first",
            subprocess.CompletedProcess([], 1, "", stderr),
            lambda: {"phase": "blocked"},
            tmp_path / "missing.json",
        )
    output = capsys.readouterr().out
    assert json.loads(output)["helper_error"] is None
    assert "SYNTHETIC_PRIVATE" not in output


def test_cut_checkpoint_emits_evidence_before_state_read_failure(tmp_path, capsys):
    import swarm_recovery_scenario as scenario

    def unavailable():
        raise RolloutError("SYNTHETIC_PRIVATE_ERROR")

    with pytest.raises(RolloutError):
        scenario.assert_cut(
            "before_attest",
            subprocess.CompletedProcess([], 87, "", ""),
            unavailable,
            tmp_path / "missing.json",
        )
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["returncode"] == 87 and data["phase"] == "unavailable"
    assert data["state_error"] == "RolloutError"
    assert "SYNTHETIC_PRIVATE" not in output


def test_cut_checkpoint_selects_known_traceback_type_and_location_without_paths(tmp_path, capsys):
    import swarm_recovery_scenario as scenario

    stderr = (
        "Traceback (most recent call last):\n"
        '  File "/SYNTHETIC_PRIVATE/rollout.py", line 870, in main\n'
        '  File "<string>", line 15, in <module>\n'
        "NameError: SYNTHETIC_PRIVATE_MESSAGE\n"
    )
    with pytest.raises(AssertionError):
        scenario.assert_cut(
            "after_first",
            subprocess.CompletedProcess([], 1, "", stderr),
            lambda: {"phase": "verified"},
            tmp_path / "missing.json",
        )
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["traceback"] is True and data["child_exception"] == "NameError"
    assert data["child_location"] == {"file": "harness", "line": 15}
    assert "SYNTHETIC_PRIVATE" not in output


def test_lost_attestation_response_reentry_does_not_reopen_or_rewrite(docker, recovery_client):
    docker.attest_response_lost = True
    with pytest.raises(RolloutError):
        recovery_client()
    assert docker.state["phase"] == "verified"
    docker.attest_response_lost = False
    recovery_client()
    assert docker.updates == ["api", "worker"]
    assert sum("begin" in call for call in docker.calls) == 1


def test_existing_attestation_must_belong_to_this_stack(docker, recovery_client):
    docker.state["evidence_scope"]["stack"] = "other-stack"
    with pytest.raises(RolloutError):
        recovery_client()
    assert not any("begin" in call for call in docker.calls)


def test_full_operation_does_not_refresh_stale_cas(docker, recovery_client):
    def writer(role):
        docker.services[role]["Version"]["Index"] += 7

    docker.before_update = writer
    with pytest.raises(RolloutError):
        recovery_client()
    assert not docker.updates
    assert docker.state["phase"] == "blocked"
    docker.before_update = None
    with pytest.raises(RolloutError):
        recovery_client()
    assert not docker.updates


def test_conclusive_cas_refusal_never_claims_external_same_spec_commit(
    docker, recovery_client, tmp_path
):
    def writer(role):
        service = docker.services[role]
        service["PreviousSpec"] = copy.deepcopy(service["Spec"])
        service["Version"]["Index"] += 7

    docker.before_update = writer
    with pytest.raises(RolloutError):
        recovery_client()
    docker.before_update = None
    with pytest.raises(RolloutError):
        recovery_client()
    assert docker.state["phase"] == "blocked"
    assert not docker.updates
    assert not any("attest" in call for call in docker.calls)
    receipt = json.loads((tmp_path / "recovery.json").read_text())
    assert receipt["roles"]["api"]["phase"] == "rejected"


def test_done_receipt_never_reattests_a_later_block_with_same_epoch(docker, recovery_client):
    recovery_client()
    docker.state.update(
        phase="blocked",
        attested_at=None,
        evidence_valid=False,
        evidence_hash=None,
        evidence_scope=None,
    )
    with pytest.raises(RolloutError):
        recovery_client()
    assert docker.state["phase"] == "blocked"
    assert sum("attest" in call for call in docker.calls) == 1


def test_unknown_response_keeps_uncertainty_after_compatible_state_reconciliation(
    docker, recovery_client, tmp_path
):
    docker.after_update = lambda role: (_ for _ in ()).throw(RolloutError("Resposta perdida"))
    with pytest.raises(RolloutError):
        recovery_client()
    receipt = json.loads((tmp_path / "recovery.json").read_text())
    assert receipt["roles"]["api"]["request_result"] == "unknown"
    docker.after_update = None
    recovery_client()
    receipt = json.loads((tmp_path / "recovery.json").read_text())
    assert receipt["roles"]["api"]["phase"] == "complete"
    assert receipt["roles"]["api"]["request_result"] == "unknown"
    assert receipt["roles"]["worker"]["request_result"] == "acknowledged"
    assert docker.updates == ["api", "worker"]


@pytest.mark.parametrize("failure", ["not_sent", "unclassified"])
def test_known_no_send_or_unclassified_error_cannot_adopt_external_commit(
    docker, recovery_client, tmp_path, failure
):
    from deploy.swarm.recovery import UpdateNotSent

    def writer(role):
        service = docker.services[role]
        service["PreviousSpec"] = copy.deepcopy(service["Spec"])
        service["Version"]["Index"] += 7
        if failure == "not_sent":
            raise UpdateNotSent("POST não iniciado")

    docker.before_update = writer
    docker.unclassified_update = failure == "unclassified"
    with pytest.raises(RolloutError):
        recovery_client()
    docker.before_update = None
    docker.unclassified_update = False
    with pytest.raises(RolloutError):
        recovery_client()
    assert docker.state["phase"] == "blocked" and not docker.updates
    assert not any("attest" in call for call in docker.calls)


def test_interruption_before_attest_can_resume_its_unfinished_receipt(docker, recovery_client):
    docker.attest_before_failure = True
    with pytest.raises(RolloutError):
        recovery_client()
    assert docker.state["phase"] == "blocked"
    docker.attest_before_failure = False
    recovery_client()
    assert docker.state["phase"] == "verified"
    assert docker.updates == ["api", "worker"]
    assert sum("begin" in call for call in docker.calls) == 1


@pytest.mark.parametrize("cutpoint", ["after_first", "before_attest"])
def test_process_exit_retains_reconcilable_intent_or_attesting_stage(
    docker, recovery_client, tmp_path, cutpoint
):
    if cutpoint == "after_first":
        docker.after_update = lambda role: (_ for _ in ()).throw(SystemExit(86))
    else:
        docker.attest_exit = True
    with pytest.raises(SystemExit):
        recovery_client()
    receipt = json.loads((tmp_path / "recovery.json").read_text())
    if cutpoint == "after_first":
        assert receipt["roles"]["api"]["request_result"] == "intent_recorded"
    else:
        assert receipt["stage"] == "attesting"
    assert docker.state["phase"] == "blocked"
    docker.after_update = None
    docker.attest_exit = False
    recovery_client()
    assert docker.updates == ["api", "worker"] and docker.state["phase"] == "verified"


@pytest.mark.parametrize("damage", ["mode", "symlink", "oversize", "extra", "hardlink"])
def test_receipt_security_refuses_reentry(tmp_path, docker, recovery_client, damage):
    docker.before_update = lambda role: (_ for _ in ()).throw(RolloutError("Interrompido"))
    with pytest.raises(RolloutError):
        recovery_client()
    receipt = tmp_path / "recovery.json"
    if damage == "mode":
        receipt.chmod(0o644)
    elif damage == "symlink":
        target = tmp_path / "elsewhere.json"
        receipt.rename(target)
        receipt.symlink_to(target)
    elif damage == "oversize":
        receipt.write_text(" " * 65537)
    elif damage == "extra":
        value = json.loads(receipt.read_text())
        value["Env"] = "PRIVATE"
        receipt.write_text(json.dumps(value))
    else:
        os.link(receipt, tmp_path / "second.json")
    docker.before_update = None
    with pytest.raises(RolloutError) as error:
        recovery_client()
    assert "PRIVATE" not in str(error.value)
    assert not docker.updates


@pytest.mark.parametrize(
    "field,value",
    [
        ("Command", ["unexpected"]),
        ("Args", ["unexpected"]),
        ("Mounts", [{"Target": "/other"}]),
        ("Configs", [{"ConfigID": "other"}]),
        ("Secrets", [{"SecretID": "other"}]),
        ("Resources", {"Limits": {"NanoCPUs": 1}}),
        ("RestartPolicy", {"Condition": "none"}),
        ("Placement", {"Constraints": ["node.role==worker"]}),
        ("ForceUpdate", 7),
    ],
)
def test_same_image_dirty_task_fields_are_refused(docker, recovery_client, field, value):
    task = docker.tasks["api"]["Spec"]
    target = (
        task["ContainerSpec"]
        if field in {"Command", "Args", "Mounts", "Configs", "Secrets"}
        else task
    )
    target[field] = value
    with pytest.raises(RolloutError):
        recovery_client()
    assert not any("begin" in call for call in docker.calls)


def test_missing_role_is_refused_before_begin(docker, recovery_client):
    del docker.services["worker"]
    with pytest.raises(RolloutError):
        recovery_client()
    assert not any("begin" in call for call in docker.calls)


def test_recovery_refuses_new_retired_task_during_update(docker, recovery_client):
    def retired(role):
        if role != "api":
            return
        task = copy.deepcopy(docker.tasks["api"])
        task.update(ID="unexpected-retired", DesiredState="shutdown")
        cid = "f" * 64
        task["Status"] = {"State": "shutdown", "ContainerStatus": {"ContainerID": cid}}
        docker.tasks["unexpected"] = task
        container = copy.deepcopy(next(iter(docker.containers.values())))
        container.update(Id=cid)
        container["State"].update(Running=False, FinishedAt="2026-01-02T00:00:00Z", ExitCode=0)
        docker.containers[cid] = container

    docker.after_update = retired
    with pytest.raises(RolloutError):
        recovery_client()
    assert docker.state["phase"] == "blocked"
    assert docker.updates == ["api"]


@pytest.mark.parametrize("wrong", ["target", "expected", "rollback", "incomplete"])
def test_recovery_input_is_exclusive_and_exact(tmp_path, docker, recovery_client, wrong):
    from deploy.swarm.recovery import Recovery

    rollout = Rollout(
        tmp_path, OTHER if wrong == "target" else CURRENT, "fixture", wrong == "rollback"
    )
    with pytest.raises(RolloutError):
        Recovery(
            rollout,
            OTHER if wrong == "expected" else CURRENT,
            {"api": LOST} if wrong == "incomplete" else {"api": LOST, "worker": LOST},
            tmp_path / "recovery.json",
        ).execute()
    assert not any("begin" in call for call in docker.calls)


@pytest.fixture
def unix_engine(monkeypatch):
    info = {
        "ID": "daemon-id",
        "Swarm": {"NodeID": "local-node", "ControlAvailable": True},
        "DriverStatus": [["driver-type", "io.containerd.snapshotter.v1"]],
    }
    state = {
        "status": 404,
        "body": {"message": "No such image: " + LOST},
        "posts": [],
        "post_bytes": [],
        "gets": [],
        "version": {"Version": "29.1.3", "ApiVersion": "1.52", "MinAPIVersion": "1.44"},
        "real_run": subprocess.run,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["gets"].append(self.path)
            status = 200
            if self.path == "/version":
                value = state["version"]
            elif self.path == "/v1.52/info":
                value = info
            elif self.path == "/v1.52/nodes/local-node":
                value = {
                    "ID": "local-node",
                    "Status": {"State": "ready"},
                    "ManagerStatus": {"Reachability": "reachable"},
                }
            elif self.path in state.get("services", {}):
                value = state["services"][self.path]
            else:
                status, value = state["status"], state["body"]
            body = value.encode() if isinstance(value, str) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            state["post_bytes"].append(body)
            state["posts"].append((self.path, json.loads(body)))
            if state.get("post_disconnect"):
                self.close_connection = True
                return
            content = state.get("post_body", "")
            body = content.encode() if isinstance(content, str) else json.dumps(content).encode()
            self.send_response(state.get("post_status", 409))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with tempfile.TemporaryDirectory(
        prefix="leadkit-recovery80-runtime-", dir=Path(__file__).resolve().parents[2]
    ) as folder:
        endpoint = "unix://" + folder + "/engine.sock"
        state["endpoint"] = endpoint
        server = UnixStreamServer(endpoint.removeprefix("unix://"), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cli(command, **kwargs):
            args = command[1:]
            if args[:2] == ["context", "show"]:
                value = "fixture"
            elif args[:2] == ["context", "inspect"]:
                value = [{"Endpoints": {"docker": {"Host": endpoint, "SkipTLSVerify": False}}}]
            elif args[0] == "info":
                value = {**info, "ID": state.get("cli_id", info["ID"])}
            else:
                raise AssertionError(args)
            return subprocess.CompletedProcess(
                command, 0, value if isinstance(value, str) else json.dumps(value), ""
            )

        for name in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "DOCKER_API_VERSION",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr("deploy.swarm.rollout.subprocess.run", cli)
        yield state, info
        server.shutdown()
        server.server_close()
        thread.join()


def test_engine_classifies_only_exact_imageinspect_missing(unix_engine, tmp_path):
    from deploy.swarm.recovery import EngineUnix, ImageMissing

    engine = EngineUnix(Rollout(tmp_path, CURRENT, "fixture", False))
    with pytest.raises(ImageMissing):
        engine.image(LOST)


def test_real_wrapper_catches_the_same_recovery_exception_class(unix_engine, tmp_path):
    state, _ = unix_engine
    state["version"].update(Version="28.5.1", ApiVersion="1.51")
    source = Path(__file__).resolve().parents[1] / "deploy/swarm"
    for name in ("deploy.sh", "rollout.py", "recovery.py"):
        shutil.copy2(source / name, tmp_path / name)
    (tmp_path / "app.env").write_text(
        "STACK_NAME=fixture\nPUBLIC_BASE_URL=https://example.test\nFORWARDED_ALLOW_IPS=127.0.0.1\n"
    )
    (tmp_path / "db.env").touch()
    (tmp_path / "stack.yaml").write_text("services: {}\n")
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "python3").symlink_to(sys.executable)
    context = [{"Endpoints": {"docker": {"Host": state["endpoint"], "SkipTLSVerify": False}}}]
    cli = binary / "docker"
    cli.write_text(
        "#!/usr/bin/env python3\nimport sys,json\na=sys.argv[1:]\n"
        'if a==["context","show"]: print("fixture")\n'
        'elif a==["context","inspect","fixture"]: print(json.dumps(' + repr(context) + "))\n"
        'elif a[:2] in [["image","inspect"],["service","inspect"]]: pass\n'
        "else: raise SystemExit(90)\n"
    )
    cli.chmod(0o700)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DOCKER_") and key not in {"STACK", "DOMAIN", "FORWARDED_ALLOW_IPS"}
    }
    environment.update(
        PATH=str(binary) + os.pathsep + os.environ["PATH"],
        IMAGE=CURRENT,
        RECOVER_CURRENT="1",
        RECOVERY_CURRENT_ID=CURRENT,
        EXPECTED_PREVIOUS_API=LOST,
        EXPECTED_PREVIOUS_WORKER=LOST,
        RECOVERY_RECEIPT="recovery.json",
        ROLLBACK="0",
    )
    result = state["real_run"](
        [str(tmp_path / "deploy.sh")],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert result.stderr.startswith("deploy.sh: Recuperação requer Engine29")
    assert not (tmp_path / "recovery.json").exists()
    assert not state["posts"]


@pytest.mark.parametrize(
    "status,body",
    [
        (404, {"message": "page not found"}),
        (404, {"message": "No such image: other"}),
        (500, {"message": "No such image: " + LOST}),
        (403, {"message": "PRIVATE"}),
        (404, "PRIVATE"),
        (404, {"message": "No such image: " + LOST, "extra": True}),
    ],
)
def test_engine_other_failures_are_not_image_absence(unix_engine, tmp_path, status, body):
    from deploy.swarm.recovery import EngineUnix, ImageMissing

    state, _ = unix_engine
    state.update(status=status, body=body)
    engine = EngineUnix(Rollout(tmp_path, CURRENT, "fixture", False))
    with pytest.raises(RolloutError) as error:
        engine.image(LOST)
    assert not isinstance(error.value, ImageMissing)
    assert "PRIVATE" not in str(error.value)


def test_engine_keeps_raw_and_defaulted_reads_separate_from_original_cas_post(
    unix_engine, tmp_path
):
    from deploy.swarm.recovery import EngineUnix, canonical

    state, _ = unix_engine
    raw = DockerFixture().services["api"]
    projected = copy.deepcopy(raw)
    projected["Spec"]["TaskTemplate"]["Resources"] = {}
    state["services"] = {
        "/v1.52/services/fixture_api?insertDefaults=false": raw,
        "/v1.52/services/api?insertDefaults=true": projected,
    }
    state.update(post_status=200, post_body={})
    engine = EngineUnix(Rollout(tmp_path, CURRENT, "fixture", False))
    baseline = engine.service("fixture_api")
    view = engine.service(baseline["ID"], insert_defaults=True)
    assert baseline == raw
    assert view == projected
    engine.update(baseline["ID"], baseline["Version"]["Index"], canonical(baseline["Spec"]))
    assert state["posts"] == [
        (
            "/v1.52/services/api/update?version=10&registryAuthFrom=spec&rollback=none",
            raw["Spec"],
        )
    ]
    assert state["post_bytes"] == [canonical(raw["Spec"])]
    assert [path for path in state["gets"] if "/services/" in path] == list(state["services"])


def test_engine_sends_original_cas_version_once(unix_engine, tmp_path):
    from deploy.swarm.recovery import EngineUnix

    state, _ = unix_engine
    engine = EngineUnix(Rollout(tmp_path, CURRENT, "fixture", False))
    with pytest.raises(RolloutError):
        engine.update("api", 10, {"Name": "fixture_api"})
    assert state["posts"] == [
        (
            "/v1.52/services/api/update?version=10&registryAuthFrom=spec&rollback=none",
            {"Name": "fixture_api"},
        )
    ]


@pytest.mark.parametrize(
    "status,body,kind",
    [
        (409, "", "UpdateRejected"),
        (403, {"message": "PRIVATE"}, "UpdateRejected"),
        (500, {"message": "update out of sequence"}, "UpdateRejected"),
        (
            500,
            {"message": "rpc error: code = Unknown desc = update out of sequence"},
            "UpdateRejected",
        ),
        (500, {"message": "PRIVATE"}, "UpdateUnknown"),
        (200, "", "UpdateUnknown"),
        (200, "invalid", "UpdateUnknown"),
        (200, {"message": "PRIVATE"}, "UpdateUnknown"),
    ],
)
def test_update_transport_distinguishes_refusal_from_unknown(
    unix_engine, tmp_path, status, body, kind
):
    from deploy.swarm.recovery import EngineUnix

    state, _ = unix_engine
    state.update(post_status=status, post_body=body)
    engine = EngineUnix(Rollout(tmp_path, CURRENT, "fixture", False))
    with pytest.raises(RolloutError) as error:
        engine.update("api", 10, {"Name": "fixture_api"})
    assert type(error.value).__name__ == kind
    assert "PRIVATE" not in str(error.value)
    assert len(state["posts"]) == 1


def test_update_transport_marks_known_no_send(unix_engine, tmp_path):
    from deploy.swarm.recovery import EngineUnix

    state, _ = unix_engine
    engine = EngineUnix(Rollout(tmp_path, CURRENT, "fixture", False))
    Path(state["endpoint"].removeprefix("unix://")).unlink()
    with pytest.raises(RolloutError) as error:
        engine.update("api", 10, {"Name": "fixture_api"})
    assert type(error.value).__name__ == "UpdateNotSent"
    assert not state["posts"]


def test_update_transport_marks_lost_response_unknown(unix_engine, tmp_path):
    from deploy.swarm.recovery import EngineUnix

    state, _ = unix_engine
    state["post_disconnect"] = True
    engine = EngineUnix(Rollout(tmp_path, CURRENT, "fixture", False))
    with pytest.raises(RolloutError) as error:
        engine.update("api", 10, {"Name": "fixture_api"})
    assert type(error.value).__name__ == "UpdateUnknown"
    assert len(state["posts"]) == 1


@pytest.mark.parametrize(
    "name,value",
    [
        ("DOCKER_HOST", "tcp://remote:2375"),
        ("DOCKER_TLS_VERIFY", "1"),
        ("DOCKER_API_VERSION", "1.51"),
    ],
)
def test_engine_refuses_unvalidated_transports(unix_engine, tmp_path, monkeypatch, name, value):
    from deploy.swarm.recovery import EngineUnix

    monkeypatch.setenv(name, value)
    with pytest.raises(RolloutError):
        EngineUnix(Rollout(tmp_path, CURRENT, "fixture", False))


@pytest.mark.parametrize("version", ["29.1.3", "29.4.2"])
def test_engine_records_exact_patch_with_supported_api(unix_engine, tmp_path, version):
    from deploy.swarm.recovery import EngineUnix

    state, _ = unix_engine
    state["version"].update(Version=version, ApiVersion="1.54")
    assert (
        EngineUnix(Rollout(tmp_path, CURRENT, "fixture", False)).identity["engine_version"]
        == version
    )


@pytest.mark.parametrize("fault", ["daemon", "backend", "major", "minimum_api", "missing_api"])
def test_engine_identity_and_runtime_guards(unix_engine, tmp_path, fault):
    from deploy.swarm.recovery import EngineUnix

    state, info = unix_engine
    if fault == "daemon":
        state["cli_id"] = "different-daemon"
    elif fault == "backend":
        info["DriverStatus"] = [["driver-type", "overlay2"]]
    elif fault == "major":
        state["version"]["Version"] = "30.0.0"
    elif fault == "missing_api":
        del state["version"]["MinAPIVersion"]
    else:
        state["version"]["MinAPIVersion"] = "1.53"
    with pytest.raises(RolloutError):
        EngineUnix(Rollout(tmp_path, CURRENT, "fixture", False))
