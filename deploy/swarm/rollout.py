"""Roteiro local do Swarm: pausa inicial, piso persistido e atestação da frota.

Executado apenas pela operadora no host Docker, depois das validações de deploy.sh.
Nenhum dado de configuração ou saída arbitrária do daemon entra em mensagens de erro.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

TERMINAL = {"complete", "shutdown", "failed", "rejected", "remove"}
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class RolloutError(Exception):
    """Mensagem operacional controlada, sem conteúdo devolvido pelo Docker."""


class Rollout:
    def __init__(self, folder, image, stack, rollback):
        self.folder = Path(folder)
        self.image = image
        self.stack = stack
        self.rollback = rollback
        self.target = ""
        self.pinned = ""
        self.node = ""
        self.tracked = {"api": {}, "worker": {}}
        self.observed_images = {}
        self.observed_since = time.time_ns()
        self.exit_proofs = {}
        self.epoch = str(uuid.uuid4())
        self.baseline = {}

    def docker(self, *args, stdin=None, timeout=660, optional=False):
        try:
            result = subprocess.run(
                ["docker", *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RolloutError(
                "Docker indisponível ou operação sem conclusão; mantenha o gate bloqueado."
            ) from None
        if result.returncode and not optional:
            raise RolloutError(
                "Docker recusou uma operação; mantenha a pausa e corrija com imagem compatível."
            )
        return result

    def decoded(self, *args):
        try:
            return json.loads(self.docker(*args).stdout)
        except (ValueError, TypeError):
            raise RolloutError(
                "O inventário Docker está incompleto; nenhuma atestação foi gravada."
            ) from None

    def image_id(self, image):
        result = self.docker("image", "inspect", "--format", "{{.Id}}", image).stdout.strip()
        if not DIGEST.fullmatch(result):
            raise RolloutError("A identidade imutável da imagem não pôde ser comprovada.")
        return result

    def capability(self, image):
        identity = self.image_id(image)
        result = self.docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            identity,
            "python",
            "-m",
            "app.deployment_contract",
            "capability",
            optional=True,
        )
        try:
            value = json.loads(result.stdout)
            if result.returncode == 0 and value == {"contract": 1}:
                return 1
        except ValueError:
            pass
        return 0

    def contract(self, action, document=None):
        args = [
            "run",
            "--rm",
            "-i",
            "--network",
            f"{self.stack}_internal",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--env-file",
            str(self.folder / "app.env"),
            self.target,
            "python",
            "-m",
            "app.deployment_contract",
            action,
        ]
        result = self.docker(*args, stdin=json.dumps(document) if document is not None else None)
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, dict) or "error" in value:
                raise ValueError
            return value
        except ValueError:
            raise RolloutError(
                "O contrato do banco não pôde ser confirmado; não restaure imagem antiga."
            ) from None

    def services(self):
        result = {}
        for role in ("api", "worker"):
            raw = self.docker("service", "inspect", f"{self.stack}_{role}", optional=True)
            if raw.returncode:
                # Ausência só é aceita quando a lista explícita dos serviços também a confirma.
                names = self.docker("service", "ls", "--format", "{{.Name}}").stdout.splitlines()
                if f"{self.stack}_{role}" in names:
                    raise RolloutError("Um serviço existente não pôde ser observado.")
                continue
            try:
                item = json.loads(raw.stdout)[0]
                spec = item["Spec"]
                previous = item.get("PreviousSpec")
                result[role] = {
                    "id": item["ID"],
                    "spec_version": item["Version"]["Index"],
                    "image_ref": spec["TaskTemplate"]["ContainerSpec"]["Image"],
                    "replicas": spec["Mode"]["Replicated"]["Replicas"],
                    "previous_ref": previous["TaskTemplate"]["ContainerSpec"]["Image"]
                    if previous
                    else None,
                    "previous_replicas": previous["Mode"]["Replicated"]["Replicas"]
                    if previous
                    else None,
                    "update": item.get("UpdateStatus", {}).get("State", "completed"),
                }
            except (KeyError, IndexError, TypeError, ValueError):
                raise RolloutError(
                    "Spec ou PreviousSpec incompleta; o gate permanece bloqueado."
                ) from None
        if result and set(result) != {"api", "worker"}:
            raise RolloutError("API e worker precisam ser observáveis em conjunto.")
        return result

    def tasks(self, role):
        ids = self.docker(
            "service", "ps", "-q", "--no-trunc", f"{self.stack}_{role}"
        ).stdout.split()
        tasks = self.decoded("inspect", "--type", "task", *ids) if ids else []
        try:
            observed = [task["ID"] for task in tasks]
            if len(ids) != len(set(ids)) or len(observed) != len(ids) or set(observed) != set(ids):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise RolloutError(
                "O inventário de tasks está incompleto; mantenha o gate bloqueado."
            ) from None
        # O inventário local também acha containers que já saíram do desired-state=running.
        containers = self.docker(
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label=com.docker.swarm.service.name={self.stack}_{role}",
        ).stdout.split()
        by_container = {}
        for task in tasks:
            state = task["Status"]["State"]
            cid = task["Status"].get("ContainerStatus", {}).get("ContainerID")
            if state not in TERMINAL or task["ID"] in self.tracked[role] or cid in containers:
                if task.get("NodeID") != self.node:
                    raise RolloutError(
                        "Há task em outro nó ou nó desconhecido; este roteiro só atesta o host local."
                    )
                if not cid:
                    raise RolloutError(
                        "Uma task não tem container observável; o gate permanece bloqueado."
                    )
                self.tracked[role][task["ID"]] = task
                by_container[cid] = task
        for cid in containers:
            if cid not in by_container:
                # Uma task já removida ainda pode ter container vivo: nunca inferir saída por ausência.
                previous = next(
                    (
                        t
                        for t in self.tracked[role].values()
                        if t["Status"].get("ContainerStatus", {}).get("ContainerID") == cid
                    ),
                    None,
                )
                if previous is None:
                    raise RolloutError(
                        "Há container sem task identificável; a saída não foi comprovada."
                    )
        return tasks

    def removed_container(self, task, present):
        cid = task["Status"]["ContainerStatus"]["ContainerID"]
        image = self.observed_images.get(cid)
        if (
            not isinstance(image, str)
            or not DIGEST.fullmatch(image)
            or not re.fullmatch(r"[a-f0-9]{64}", cid)
            or task["NodeID"] != self.node
            or (task["ID"] in present and task["Status"]["State"] not in TERMINAL)
        ):
            raise RolloutError("Um container ausente não tem saída comprovada; mantenha a pausa.")
        node = self.decoded("node", "inspect", self.node)[0]
        local = self.docker("info", "--format", "{{.Swarm.NodeID}}").stdout.strip()
        if (
            local != self.node
            or node["Status"]["State"] != "ready"
            or node.get("ManagerStatus", {}).get("Reachability") != "reachable"
        ):
            raise RolloutError("O nó local não confirma a saída; mantenha a pausa.")
        # Não basta inspect falhar: o daemon precisa listar e comprovar a destruição
        # do ID completo cuja imagem já foi observada, inclusive após remover a task.
        if self.docker("ps", "-aq", "--no-trunc", "--filter", f"id={cid}").stdout.strip():
            raise RolloutError(
                "Um container existente não pôde ser inspecionado; mantenha a pausa."
            )
        until = time.time_ns()
        if cid in self.exit_proofs:
            proof = self.exit_proofs[cid]
            if (
                isinstance(proof, dict)
                and set(proof) == {"container_id", "image", "node_id", "since", "time_nano"}
                and proof["container_id"] == cid
                and proof["image"] == image
                and proof["node_id"] == self.node
                and type(proof["since"]) is int
                and proof["since"] == self.observed_since
                and type(proof["time_nano"]) is int
                and self.observed_since <= proof["time_nano"] <= until
            ):
                return {"Image": image, "State": {"Running": False}}, "remove"
            raise RolloutError("A prova de saída não corresponde à observação; mantenha a pausa.")
        raw = self.docker(
            "events",
            "--since",
            f"{self.observed_since // 10**9}.{self.observed_since % 10**9:09d}",
            "--until",
            f"{until // 10**9}.{until % 10**9:09d}",
            "--filter",
            "type=container",
            "--filter",
            f"container={cid}",
            "--filter",
            "event=destroy",
            "--format",
            "{{json .}}",
        )
        try:
            events = [json.loads(line) for line in raw.stdout.splitlines()]
            destroyed = next(
                (
                    event["timeNano"]
                    for event in events
                    if event.get("Type") == "container"
                    and event.get("Action") == "destroy"
                    and event.get("Actor", {}).get("ID") == cid
                    and event.get("scope") == "local"
                    and type(event.get("timeNano")) is int
                    and self.observed_since <= event["timeNano"] <= until
                ),
                None,
            )
        except (ValueError, TypeError, AttributeError):
            destroyed = False
        if destroyed is None or destroyed is False:
            raise RolloutError("Falta prova local de destruição do container; mantenha a pausa.")
        self.exit_proofs[cid] = {
            "container_id": cid,
            "image": image,
            "node_id": self.node,
            "since": self.observed_since,
            "time_nano": destroyed,
        }
        return {"Image": image, "State": {"Running": False}}, "remove"

    def container(self, role, task):
        cid = task["Status"]["ContainerStatus"]["ContainerID"]
        raw = self.docker("inspect", "--type", "container", cid, optional=True)
        if raw.returncode:
            # A task pode ter saído durante wait ou entre o inventário e inspect.
            present = {item["ID"] for item in self.tasks(role)}
            task = self.tracked[role][task["ID"]]
            return self.removed_container(task, present)
        try:
            container = json.loads(raw.stdout)[0]
            image = container["Image"]
            if not DIGEST.fullmatch(image):
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise RolloutError("Um container não tem identidade de imagem comprovada.") from None
        if cid in self.observed_images and self.observed_images[cid] != image:
            raise RolloutError(
                "A imagem de um container observado mudou; mantenha o gate bloqueado."
            )
        self.observed_images.setdefault(cid, image)
        return container, task["Status"]["State"]

    def observe(self, role, *, wait=False, wait_ids=None):
        self.tasks(role)
        observations = []
        for task in list(self.tracked[role].values()):
            cid = task["Status"]["ContainerStatus"]["ContainerID"]
            container, state = self.container(role, task)
            running = container["State"]["Running"]
            if wait and running and (wait_ids is None or task["ID"] in wait_ids):
                # O Docker espera o processo efetivamente sair, inclusive durante o grace period.
                self.docker("container", "wait", cid, optional=True)
                self.tasks(role)
                task = self.tracked[role][task["ID"]]
                container, state = self.container(role, task)
                running = container["State"]["Running"]
            image = container["Image"]
            if not DIGEST.fullmatch(image):
                raise RolloutError("Um container não tem identidade de imagem comprovada.")
            observations.append(
                {
                    "id": task["ID"],
                    "node_id": task["NodeID"],
                    "container_id": cid,
                    "image": image,
                    "state": state,
                    "healthy": container["State"].get("Health", {}).get("Status") == "healthy",
                    "running": running,
                }
            )
        if {row["id"] for row in observations} != set(self.tracked[role]):
            raise RolloutError(
                "O inventário de tasks mudou durante a coleta; nenhuma pausa foi comprovada."
            )
        return observations

    def quiesce(self):
        current = self.services()
        if not current:
            for role in ("api", "worker"):
                orphaned = self.docker(
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=com.docker.swarm.service.name={self.stack}_{role}",
                ).stdout.strip()
                if orphaned:
                    raise RolloutError(
                        "Há containers sem serviço observável; não execute a migration."
                    )
            return
        for role in current:
            self.observe(role)
        self.docker(
            "service", "scale", "--detach=false", f"{self.stack}_api=0", f"{self.stack}_worker=0"
        )
        for role in current:
            self.observe(role, wait=True)
            rows = self.observe(role)
            if any(row["running"] or row["state"] not in TERMINAL for row in rows):
                raise RolloutError(
                    "Um processo antigo ainda está em saída; não execute a migration."
                )
        if any(s["replicas"] != 0 for s in self.services().values()):
            raise RolloutError(
                "A pausa dos serviços não ficou persistida; não execute a migration."
            )
        # A escala guarda a Spec anterior ativa. Atualizar ainda em zero fecha
        # também o retorno persistido antes de qualquer DDL.
        for role in current:
            self.docker("service", "update", "--force", "--detach=false", f"{self.stack}_{role}")
        if any(s["replicas"] != 0 or s["previous_replicas"] != 0 for s in self.services().values()):
            raise RolloutError("O retorno dos serviços não ficou pausado; não execute a migration.")

    def stack_deploy(self, *, paused=False):
        args = [
            "stack",
            "deploy",
            "--detach=false",
            "--resolve-image",
            "changed",
            "-c",
            str(self.folder / "stack.yaml"),
        ]
        overlay = None
        try:
            if paused:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".yaml", dir=self.folder, delete=False
                ) as file:
                    file.write(
                        "services:\n  api:\n    deploy:\n      replicas: 0\n  worker:\n    deploy:\n      replicas: 0\n"
                    )
                    overlay = Path(file.name)
                args += ["-c", str(overlay)]
            self.docker(*args, self.stack)
        finally:
            if overlay is not None:
                overlay.unlink(missing_ok=True)

    def migrate(self):
        print(f"deploy.sh: migrations com {self.image}")
        result = self.docker(
            "run",
            "--rm",
            "--name",
            f"{self.stack}-migrate",
            "--network",
            f"{self.stack}_internal",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--env-file",
            str(self.folder / "app.env"),
            self.target,
            "migrate",
            optional=True,
        )
        if result.returncode:
            if "revisão incompatível" in result.stdout + result.stderr:
                raise RolloutError(
                    "revisão incompatível; a stack não foi mexida. Use imagem compatível e o roteiro de retorno."
                )
            raise RolloutError(
                "as migrations falharam; a stack não foi mexida. Preserve o gate bloqueado."
            )

    def update_unchanged(self, role, service):
        old = self.baseline.get(role)
        return old is not None and all(
            old[key] == service[key] for key in ("id", "spec_version", "update")
        )

    def verify(self):
        services = self.services()
        if set(services) != {"api", "worker"}:
            raise RolloutError("Faltam serviços para comprovar a frota.")
        result = {}
        for role, service in services.items():
            if service["update"] not in {"completed", ""} and not self.update_unchanged(
                role, service
            ):
                raise RolloutError(
                    "A atualização não terminou; o Swarm voltou ou interrompeu a troca."
                )
            identity = self.image_id(service["image_ref"])
            if identity != self.target or self.capability(identity) < 1:
                raise RolloutError(
                    "Uma réplica continua com outra imagem; o gate permanece bloqueado."
                )
            if not service["previous_ref"] or self.capability(service["previous_ref"]) < 1:
                raise RolloutError(
                    "PreviousSpec ainda permite imagem antiga; falta a segunda atualização compatível."
                )
            rows = self.observe(role)
            desired = {
                task["ID"]
                for task in self.tracked[role].values()
                if task["DesiredState"] == "running"
            }
            active = [
                row
                for row in rows
                if row["id"] in desired and row["state"] == "running" and row["running"]
            ]
            retired = [row for row in rows if row not in active]
            if (
                service["replicas"] < 1
                or len(active) != service["replicas"]
                or any(row["image"] != identity or not row["healthy"] for row in active)
                or any(row["running"] or row["state"] not in TERMINAL for row in retired)
            ):
                raise RolloutError(
                    "Há réplica ausente, sem saúde ou processo antigo vivo; nenhuma atestação foi gravada."
                )
            result[role] = {
                "id": service["id"],
                "spec_version": service["spec_version"],
                "observed_spec_version": service["spec_version"],
                "image": identity,
                "contract": 1,
                "previous_image": self.image_id(service["previous_ref"]),
                "previous_contract": 1,
                "replicas": service["replicas"],
                "tasks": active,
                "retired": retired,
            }
        if services != self.services():
            raise RolloutError("A Spec mudou durante a coleta; repita o roteiro com novo epoch.")
        return {"schema_version": 1, "stack": self.stack, "node_id": self.node, "services": result}

    def execute(self):
        self.target = self.image_id(self.image)
        if self.capability(self.target) < 1:
            raise RolloutError("A imagem não comprova contrato1; nada foi alterado.")
        self.node = self.docker("info", "--format", "{{.Swarm.NodeID}}").stdout.strip()
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", self.node):
            raise RolloutError("O nó local do Swarm não pôde ser identificado.")
        node = self.decoded("node", "inspect", self.node)[0]
        if (
            node["Status"]["State"] != "ready"
            or not node.get("ManagerStatus", {}).get("Reachability") == "reachable"
        ):
            raise RolloutError("O manager local não está disponível; nenhuma mudança foi iniciada.")
        network = (
            self.docker("network", "inspect", f"{self.stack}_internal", optional=True).returncode
            == 0
        )
        try:
            status = (
                self.contract("status") if network else {"minimum_contract": 0, "phase": "blocked"}
            )
        except RolloutError:
            raise RolloutError(
                "O banco não confirmou o contrato; a stack não foi mexida."
            ) from None
        floor = status.get("minimum_contract")
        if type(floor) is not int or floor not in {0, 1}:
            raise RolloutError("O piso do banco excede a capacidade da imagem; nada foi alterado.")
        if self.rollback and floor == 0:
            raise RolloutError(
                "Não há contrato compatível para rollback; use a primeira adoção com migration."
            )
        # O comando de capacidade usa o ID exato; o serviço usa referência de conteúdo, não tag móvel.
        digests = self.decoded("image", "inspect", "--format", "{{json .RepoDigests}}", self.target)
        self.pinned = next((d for d in (digests or []) if "@sha256:" in d), "")
        if not self.pinned:
            self.pinned = "leadkit-local:" + self.target.removeprefix("sha256:")
            self.docker("image", "tag", self.target, self.pinned)
        os.environ["IMAGE"] = self.pinned
        initial = self.services() if network else {}
        bootstrap = floor == 0
        if bootstrap:
            self.quiesce()
            if not network:
                self.stack_deploy(paused=True)
                # Banco novo: aguarda conexão sem registrar DSN ou saída do driver.
                deadline = time.monotonic() + 120
                while True:
                    try:
                        self.contract("status")
                        break
                    except RolloutError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(1)
            self.migrate()
            if self.contract("status").get("minimum_contract") != 1:
                raise RolloutError("A migration não confirmou piso1; mantenha os serviços parados.")
        else:
            for role, service in initial.items():
                if self.capability(service["image_ref"]) < 1 and service["replicas"]:
                    raise RolloutError(
                        "Há imagem antiga com réplicas ativas após0016; mantenha a pausa para recuperação."
                    )
                self.observe(role)
        self.contract("begin", {"epoch": self.epoch})
        if self.rollback:
            print("deploy.sh: ROLLBACK=1, sem migrations; piso de compatibilidade preservado")
        if not bootstrap and not self.rollback:
            self.migrate()
        self.baseline = self.services()
        self.stack_deploy()
        current = self.services()
        failures = [
            f"{self.stack}_{role} ({service['update']})"
            for role, service in current.items()
            if service["update"]
            in {"paused", "rollback_started", "rollback_paused", "rollback_completed"}
            and not self.update_unchanged(role, service)
        ]
        if failures:
            raise RolloutError("a versão nova não passou no healthcheck: " + ", ".join(failures))
        for role, service in current.items():
            if self.image_id(service["image_ref"]) != self.target or (
                service["update"] not in {"", "completed"}
                and not self.update_unchanged(role, service)
            ):
                raise RolloutError(
                    "O Swarm não confirmou a imagem solicitada; mantenha o gate bloqueado."
                )
            previous_ok = service["previous_ref"] and self.capability(service["previous_ref"]) >= 1
            if bootstrap or not previous_ok or service["previous_replicas"] == 0:
                # Um retorno antigo aqui só é permitido se a Spec guardada realmente estiver pausada.
                if not previous_ok and service["previous_replicas"] != 0:
                    raise RolloutError(
                        "PreviousSpec antiga tem réplicas ativas; não execute force nem habilite participante."
                    )
                self.observe(role)
                self.docker(
                    "service", "update", "--force", "--detach=false", f"{self.stack}_{role}"
                )
        for role in current:
            # Apenas as tasks que deixaram a configuração atual devem sair; as ativas permanecem.
            tasks = self.tasks(role)
            active_ids = {task["ID"] for task in tasks if task["DesiredState"] == "running"}
            self.observe(role, wait=True, wait_ids=set(self.tracked[role]) - active_ids)
        evidence = self.verify()
        # Nova coleta completa antes do commit detecta mudança de Spec, tasks ou saúde.
        if evidence != self.verify():
            raise RolloutError("A frota mudou antes da atestação; o gate permanece bloqueado.")
        self.contract("attest", {"epoch": self.epoch, "evidence": evidence})
        print(f"deploy.sh: {self.stack} no ar com {self.image}; contrato participante verificado")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--stack", required=True)
    parser.add_argument("--rollback", choices=["0", "1"], required=True)
    args = parser.parse_args()
    try:
        lock_path = Path(args.folder) / ".deployment.lock"
        with lock_path.open("a+") as lock:
            lock_path.chmod(0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RolloutError(
                    "Já há um deploy nesta pasta; aguarde a conclusão antes de repetir."
                ) from None
            Rollout(args.folder, args.image, args.stack, args.rollback == "1").execute()
        return 0
    except RolloutError as exc:
        print(f"deploy.sh: {exc}", file=__import__("sys").stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        print(
            "deploy.sh: inventário ou contrato inválido; preserve a pausa e use imagem compatível.",
            file=__import__("sys").stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
