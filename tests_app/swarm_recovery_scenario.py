"""Ensaio real da #80, somente na fixture descartável do job Swarm.

Não importa fixtures/fakes de pytest nem oferece flags de falha ao produto.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deploy.swarm import recovery
from deploy.swarm.rollout import Rollout, RolloutError


def run(*args, stdin=None, optional=False, timeout=660, env=None):
    result = subprocess.run(
        args, input=stdin, capture_output=True, text=True, timeout=timeout, env=env, check=False
    )
    if result.returncode and not optional:
        raise RuntimeError("A operação da fixture falhou; saída arbitrária foi omitida.")
    return result


def docker(*args, **kwargs):
    return run("docker", *args, **kwargs).stdout.strip()


def decoded(*args):
    return json.loads(docker(*args))


def runtime(stack):
    rows = []
    for role in ("db", "api", "worker"):
        ids = docker(
            "service",
            "ps",
            "--no-trunc",
            "-q",
            "--filter",
            "desired-state=running",
            f"{stack}_{role}",
        ).split()
        assert ids
        for tid in ids:
            task = decoded("inspect", "--type", "task", tid)[0]
            cid = task["Status"]["ContainerStatus"]["ContainerID"]
            container = decoded("inspect", "--type", "container", cid)[0]
            assert task["Status"]["State"] == "running" and container["State"]["Running"]
            assert container["State"]["Health"]["Status"] == "healthy"
            rows.append(
                {
                    "role": role,
                    "task": tid,
                    "container": cid,
                    "image": container["Image"],
                    "started": container["State"]["StartedAt"],
                    "restarts": container["RestartCount"],
                }
            )
    return sorted(rows, key=lambda row: row["task"])


def database(stack):
    db = next(row for row in runtime(stack) if row["role"] == "db")["container"]
    # A mesma impressão digital sintética do ensaio de retorno existente.
    query = "select md5(jsonb_build_array((select jsonb_agg(to_jsonb(s) order by id) from submissions s), (select jsonb_agg(to_jsonb(f) order by id) from form_versions f), (select jsonb_agg(id order by id) from jobs), (select version_num from alembic_version))::text)"
    value = docker(
        "exec",
        db,
        "sh",
        "-c",
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"',
        "sh",
        query,
    )
    assert re.fullmatch(r"[a-f0-9]{32}", value)
    return value


def backup(stack, path):
    db = next(row for row in runtime(stack) if row["role"] == "db")["container"]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as target:
        result = subprocess.run(
            [
                "docker",
                "exec",
                db,
                "sh",
                "-c",
                'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom',
            ],
            stdout=target,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
    assert result.returncode == 0 and path.stat().st_size > 0
    with path.open("rb") as source:
        result = subprocess.run(
            ["docker", "exec", "-i", db, "pg_restore", "--list"],
            stdin=source,
            capture_output=True,
            check=False,
            timeout=60,
        )
    assert result.returncode == 0 and b"TABLE DATA" in result.stdout


def rollout(folder, image):
    run(
        str(folder / "deploy.sh"),
        env={**os.environ, "IMAGE": image, "RECOVER_CURRENT": "0", "ROLLBACK": "0"},
    )


def oci_proof(archive):
    with tarfile.open(archive) as stream:
        index = json.load(stream.extractfile("index.json"))
        assert index["mediaType"] == "application/vnd.oci.image.index.v1+json"
        blobs = {
            item.name: item
            for item in stream.getmembers()
            if item.name.startswith("blobs/sha256/") and item.isfile()
        }
        for name, item in blobs.items():
            content = stream.extractfile(item).read()
            assert hashlib.sha256(content).hexdigest() == name.rsplit("/", 1)[-1]
        checked = set()

        def graph(descriptor):
            digest = descriptor["digest"]
            content = stream.extractfile("blobs/sha256/" + digest.removeprefix("sha256:")).read()
            assert len(content) == descriptor["size"]
            if digest in checked:
                return
            checked.add(digest)
            if "json" in descriptor["mediaType"]:
                document = json.loads(content)
                for child in document.get("manifests", []) + document.get("layers", []):
                    graph(child)
                config = document.get("config")
                if isinstance(config, dict) and {"digest", "size", "mediaType"} <= set(config):
                    graph(config)

        for descriptor in index["manifests"]:
            graph(descriptor)
        roots = [
            entry
            for entry in index["manifests"]
            if any(
                value.endswith("leadkit-recovery80:current")
                for value in entry.get("annotations", {}).values()
            )
        ]
        assert roots
        root = roots[0]
        content = stream.extractfile(
            "blobs/sha256/" + root["digest"].removeprefix("sha256:")
        ).read()
        assert len(content) == root["size"]
        manifest = json.loads(content)
        assert manifest["mediaType"] == "application/vnd.oci.image.index.v1+json"
        entries = manifest["manifests"]
        assert any(
            entry.get("platform", {}).get("os") == "linux"
            and entry.get("platform", {}).get("architecture") == "amd64"
            for entry in entries
        )
        assert any(
            entry.get("annotations", {}).get("vnd.docker.reference.type") == "attestation-manifest"
            for entry in entries
        )
        return root["digest"]


def prepare(folder, stack, scratch):
    for kind in ("previous", "current"):
        docker(
            "buildx",
            "build",
            "--builder",
            "default",
            "--platform",
            "linux/amd64",
            "--provenance=mode=min",
            "--output",
            f"type=image,oci-mediatypes=true,name=leadkit-recovery80:{kind},push=false",
            "-",
            stdin=f"FROM gerador-planos:ci\nLABEL ci.recovery80.role={kind}\n",
        )
    p, c = [
        docker("image", "inspect", "--format", "{{.Id}}", "leadkit-recovery80:" + kind)
        for kind in ("previous", "current")
    ]
    assert p != c
    rollout(folder, "leadkit-recovery80:previous")
    rollout(folder, "leadkit-recovery80:current")
    archive = scratch / "current.tar"
    docker("image", "save", "-o", str(archive), "leadkit-recovery80:current")
    root_digest = oci_proof(archive)
    descriptor = decoded("image", "inspect", "leadkit-recovery80:current")[0]["Descriptor"]
    assert (
        descriptor["mediaType"] == "application/vnd.oci.image.index.v1+json"
        and descriptor["digest"] == root_digest
    )
    archive.chmod(0o600)
    docker("image", "load", "-i", str(archive))
    assert docker("image", "inspect", "--format", "{{.Id}}", "leadkit-recovery80:current") == c
    backup(stack, scratch / "fixture.dump")
    # Leitura do backup e da Current é uma pré-condição da fixture; nada é enviado
    # como artifact público e somente recursos próprios serão removidos.
    assert archive.stat().st_size > 0
    old = {}
    for role in ("api", "worker"):
        service = decoded("service", "inspect", f"{stack}_{role}")[0]
        old[role] = service["PreviousSpec"]["TaskTemplate"]["ContainerSpec"]["Image"]
        assert docker("image", "inspect", "--format", "{{.Id}}", old[role]) == p
    for cid in docker("ps", "-aq", "--no-trunc").split():
        item = decoded("inspect", "--type", "container", cid)[0]
        if item["Image"] != p:
            continue
        labels = item["Config"].get("Labels", {})
        assert labels.get("com.docker.swarm.service.name") in {f"{stack}_api", f"{stack}_worker"}
        assert item["State"]["Running"] is False
        docker("container", "rm", cid)
    image = decoded("image", "inspect", p)[0]
    tags = image.get("RepoTags") or []
    allowed = {"leadkit-recovery80:previous", "leadkit-local:" + p.removeprefix("sha256:")}
    assert set(tags) <= allowed
    for tag in tags:
        docker("image", "rm", "--no-prune", tag)
    if run("docker", "image", "inspect", p, optional=True).returncode == 0:
        docker("image", "rm", "--no-prune", p)
    return c, old


def exercise(folder, stack, current, previous, scratch, *, cutpoint):
    client = Rollout(folder, current, stack, False)
    client.target = current
    engine = recovery.EngineUnix(client)
    print(
        json.dumps(
            {
                "stage": "daemon",
                "engine": engine.identity["engine_version"],
                "api": "1.52",
                "backend": "io.containerd.snapshotter.v1",
            }
        )
    )
    for ref in previous.values():
        try:
            engine.image(ref)
        except recovery.ImageMissing:
            pass
        else:
            raise AssertionError("A fixture não perdeu Previous.")
    initial = runtime(stack)
    before_db = database(stack)
    reader = recovery.Recovery(client, current, previous, folder / "recovery-probe.json")
    state = reader.state()
    assert state["phase"] == "verified" and state["minimum_contract"] == 3
    normal = run(str(folder / "deploy.sh"), env={**os.environ, "IMAGE": current}, optional=True)
    assert normal.returncode != 0 and reader.state() == state and runtime(stack) == initial
    # Proxy de CLI real, limitado à janela de recuperação: nunca grava argv/env.
    real = shutil.which("docker")
    bindir = scratch / "bin"
    bindir.mkdir()
    proxy = bindir / "docker"
    proxy.write_text(
        "#!/usr/bin/env python3\nimport os, sys\na=sys.argv[1:]\nif a[:2] in [['stack','deploy'],['service','scale'],['service','update'],['image','pull'],['image','tag']] or (a[:1]==['run'] and a[-1:] == ['migrate']):\n raise SystemExit(91)\nos.execv("
        + repr(real)
        + ", ["
        + repr(real)
        + "]+a)\n"
    )
    proxy.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
        "IMAGE": current,
        "RECOVER_CURRENT": "1",
        "RECOVERY_CURRENT_ID": current,
        "EXPECTED_PREVIOUS_API": previous["api"],
        "EXPECTED_PREVIOUS_WORKER": previous["worker"],
        "RECOVERY_RECEIPT": "recovery-ci.json",
    }
    # Processo filho corta a resposta após POST real ou antes de attest, sem
    # modificar flags/comportamento do helper instalado.
    harness = """import os, sys, json, hashlib
sys.path.insert(0, sys.argv[1])
import recovery, rollout
cutpoint = sys.argv[3]
real = recovery.EngineUnix.update
def cut(self, service_id, version, spec):
    real(self, service_id, version, spec)
    body = spec if isinstance(spec, bytes) else recovery.canonical(spec)
    print(json.dumps({'stage':'engine_post','service_id':service_id,'baseline_version':version,'body_sha256':hashlib.sha256(body).hexdigest(),'committed':True}), flush=True)
    if cutpoint == 'after_first': os._exit(86)
contract = rollout.Rollout.contract
def before_attest(self, action, document=None):
    if action == 'attest': os._exit(87)
    return contract(self, action, document)
recovery.EngineUnix.update = cut
if cutpoint == 'before_attest': rollout.Rollout.contract = before_attest
sys.argv = ['rollout.py','--folder',sys.argv[1],'--image',os.environ['IMAGE'],'--stack',sys.argv[2],'--rollback','0','--recover-current','--expected-current',os.environ['IMAGE'],'--expected-previous-api',os.environ['EXPECTED_PREVIOUS_API'],'--expected-previous-worker',os.environ['EXPECTED_PREVIOUS_WORKER'],'--recovery-receipt',os.environ['RECOVERY_RECEIPT']]
raise SystemExit(rollout.main())
"""
    cut = run(
        sys.executable, "-c", harness, str(folder), stack, cutpoint, env=environment, optional=True
    )
    assert (
        cut.returncode == (86 if cutpoint == "after_first" else 87)
        and reader.state()["phase"] == "blocked"
    )
    partial_receipt = json.loads((folder / "recovery-ci.json").read_text())
    witness = [json.loads(line) for line in cut.stdout.splitlines()]
    assert len(witness) == (1 if cutpoint == "after_first" else 2)
    for item in witness:
        baseline = next(
            row for row in partial_receipt["baseline"].values() if row["id"] == item["service_id"]
        )
        assert (
            item["baseline_version"] == baseline["version"]
            and item["body_sha256"] == baseline["spec"]
            and item["committed"] is True
        )
        print(json.dumps(item))
    committed = [
        engine.service(f"{stack}_{role}")["PreviousSpec"]
        == engine.service(f"{stack}_{role}")["Spec"]
        for role in ("api", "worker")
    ]
    assert committed == ([True, False] if cutpoint == "after_first" else [True, True])
    assert runtime(stack) == initial
    run(str(folder / "deploy.sh"), env=environment)
    assert runtime(stack) == initial and database(stack) == before_db
    assert reader.state()["phase"] == "verified"
    # Replay comprovado da mesma tentativa não reenvia updates nem begin.
    receipt = json.loads((folder / "recovery-ci.json").read_text())
    run(str(folder / "deploy.sh"), env=environment)
    assert json.loads((folder / "recovery-ci.json").read_text()) == receipt
    assert runtime(stack) == initial
    # CAS stale real num serviço próprio: label só de serviço avança a versão,
    # preservando tasks. O Adapter não pode absorver essa versão mais nova.
    service = engine.service(f"{stack}_api")
    changed = dict(service["Spec"])
    changed["Labels"] = {**changed.get("Labels", {}), "ci.recovery80.cas": "writer"}
    engine.update(service["ID"], service["Version"]["Index"], changed)
    competing = engine.service(f"{stack}_api")
    try:
        engine.update(service["ID"], service["Version"]["Index"], service["Spec"])
    except recovery.UpdateRejected:
        pass
    else:
        raise AssertionError("CAS stale foi aceita.")
    assert engine.service(f"{stack}_api") == competing
    assert runtime(stack) == initial
    print(
        json.dumps(
            {
                "stage": "recovery80",
                "cutpoint": cutpoint,
                "same_tasks": True,
                "stale_cas_refused": True,
                "reentry": True,
                "schema": "0020_draft_test_commands",
                "floor": 3,
            }
        )
    )
    (folder / "recovery-ci.json").unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    args = parser.parse_args()
    assert (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("CI_ADOPTION_BASELINE") == "contract-two"
    )
    folder = args.folder.resolve()
    stack = os.environ["CI_STACK_NAME"]
    assert stack == "gerador-planos" and folder == Path(os.environ["CONF"]).resolve()
    # Guard antes de construir ou alterar a fixture; não instala/troca daemon.
    recovery.EngineUnix(Rollout(folder, "gerador-planos:ci", stack, False))
    with tempfile.TemporaryDirectory(
        prefix="recovery80-", dir=os.environ["RUNNER_TEMP"]
    ) as scratch:
        for cutpoint in ("after_first", "before_attest"):
            attempt = Path(scratch) / cutpoint
            attempt.mkdir()
            current, previous = prepare(folder, stack, attempt)
            exercise(folder, stack, current, previous, attempt, cutpoint=cutpoint)
        # Fora da janela sem restart: retorno C é executável pelo roteiro normal.
        run(str(folder / "deploy.sh"), env={**os.environ, "IMAGE": current, "ROLLBACK": "1"})
        rollout(folder, "gerador-planos:ci")
        rollout(folder, "gerador-planos:ci")
        final = recovery.Recovery(
            Rollout(folder, current, stack, False), current, previous, folder / "recovery-ci.json"
        )
        final.r.target = current
        assert final.state()["phase"] == "verified"
        for role in ("api", "worker"):
            service = decoded("service", "inspect", f"{stack}_{role}")[0]
            assert docker(
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                service["Spec"]["TaskTemplate"]["ContainerSpec"]["Image"],
            ) == docker("image", "inspect", "--format", "{{.Id}}", "gerador-planos:ci")
    print('{"stage":"postcondition","base_restored":true,"return_usable":true}')


if __name__ == "__main__":
    try:
        main()
    except (
        AssertionError,
        RuntimeError,
        RolloutError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        subprocess.TimeoutExpired,
    ) as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        print(
            json.dumps(
                {
                    "stage": "failed",
                    "kind": type(error).__name__,
                    "file": Path(frame.filename).name,
                    "line": frame.lineno,
                }
            ),
            file=sys.stderr,
        )
        print(
            "O ensaio real de recuperação falhou; nenhuma prova verde foi produzida.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
