"""Recuperação explícita pelo helper real, com Docker/banco sintéticos nas seams externas."""

import copy
import hashlib
import io
import json
import os
import subprocess
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
                "TaskTemplate": {"ContainerSpec": {"Image": CURRENT, "Env": ["KEY=PRIVATE"]}},
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
            }
            cid = hashlib.sha256(role.encode()).hexdigest()
            self.tasks[role] = {
                "ID": role + "-task",
                "ServiceID": role,
                "NodeID": "local-node",
                "DesiredState": "running",
                "Spec": copy.deepcopy(spec["TaskTemplate"]),
                "Endpoint": {"Spec": spec["EndpointSpec"]},
                "Status": {"State": "running", "ContainerStatus": {"ContainerID": cid}},
            }
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
            }

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
            value = [self.services[args[-1].rsplit("_", 1)[-1]]]
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

        def service(self, name):
            return copy.deepcopy(docker.services[name.rsplit("_", 1)[-1]])

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
            if isinstance(spec, bytes):
                spec = json.loads(spec)
            service = docker.services[service_id]
            if getattr(docker, "before_update", None):
                docker.before_update(service_id)
            if service["Version"]["Index"] != version:
                raise RolloutError("CAS recusada")
            docker.updates.append(service_id)
            service["PreviousSpec"] = copy.deepcopy(service["Spec"])
            service["Spec"] = copy.deepcopy(spec)
            service["Version"]["Index"] += 1
            if getattr(docker, "after_update", None):
                docker.after_update(service_id)

    monkeypatch.setattr(recovery, "EngineUnix", Engine)

    def run():
        rollout = Rollout(tmp_path, CURRENT, "fixture", False)
        return recovery.Recovery(
            rollout, CURRENT, {"api": LOST, "worker": LOST}, tmp_path / "recovery.json"
        ).execute()

    return run


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
        docker.tasks["api"]["Endpoint"] = {"Spec": {"Mode": "dnsrr"}}
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
        "version": {"Version": "29.1.3", "ApiVersion": "1.52", "MinAPIVersion": "1.44"},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
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
            else:
                status, value = state["status"], state["body"]
            body = value.encode() if isinstance(value, str) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            state["posts"].append(
                (self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            )
            self.send_response(409)
            self.send_header("Content-Length", "0")
            self.end_headers()

    with tempfile.TemporaryDirectory(
        prefix="leadkit-recovery80-runtime-", dir=Path(__file__).resolve().parents[2]
    ) as folder:
        endpoint = "unix://" + folder + "/engine.sock"
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
