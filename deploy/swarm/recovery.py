"""Recompõe um retorno com a Current, sem restaurar a imagem histórica perdida."""

from __future__ import annotations

import copy
import hashlib
import http.client
import json
import os
import re
import socket
import stat
import tempfile
import time
from urllib.parse import quote

if __package__:
    from .rollout import DIGEST, RolloutError
else:
    from rollout import DIGEST, RolloutError


class ImageMissing(RolloutError):
    """Somente ImageInspect autenticado no daemon local pode produzir esta classificação."""


class UpdateNotSent(RolloutError):
    """A tentativa falhou antes de iniciar o POST de atualização."""


class UpdateRejected(RolloutError):
    """O Engine devolveu recusa conclusiva; estado equivalente não autoriza reentrada."""


class UpdateUnknown(RolloutError):
    """O envio iniciou, mas a resposta não comprova o desfecho da atualização."""


class EngineUnix:
    """Transporte operacional local; não fornece Docker ao app."""

    def __init__(self, rollout):
        self.r = rollout
        self.endpoint = self.endpoint_for_cli()
        version = self.request("GET", "/version")
        if (
            not re.fullmatch(r"29\.[0-9]+\.[0-9]+", version.get("Version", ""))
            or not all(
                re.fullmatch(r"[0-9]+\.[0-9]+", version.get(key, ""))
                for key in ("ApiVersion", "MinAPIVersion")
            )
            or tuple(map(int, version["ApiVersion"].split("."))) < (1, 52)
            or tuple(map(int, version["MinAPIVersion"].split("."))) > (1, 52)
        ):
            raise RolloutError("Recuperação requer Engine29 e suporte comprovado à API1.52.")
        info = self.request("GET", "/v1.52/info")
        self.node = info["Swarm"]["NodeID"]
        self.identity = {
            "daemon": info["ID"],
            "node": self.node,
            "endpoint": self.endpoint,
            "engine_version": version["Version"],
            "api": "1.52",
        }
        if not info["Swarm"]["ControlAvailable"] or [
            "driver-type",
            "io.containerd.snapshotter.v1",
        ] not in info.get("DriverStatus", []):
            raise RolloutError(
                "O daemon precisa ser manager local com store containerd comprovado."
            )
        self.confirm()

    def endpoint_for_cli(self):
        if (
            any(
                os.environ.get(key)
                for key in ("DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
            )
            or os.environ.get("DOCKER_API_VERSION", "1.52") != "1.52"
        ):
            raise RolloutError("TLS ou versão de transporte não validada para recuperação.")
        host = os.environ.get("DOCKER_HOST")
        if host and os.environ.get("DOCKER_CONTEXT"):
            raise RolloutError("Não combine DOCKER_HOST e DOCKER_CONTEXT na recuperação.")
        context = self.r.docker("context", "show").stdout.strip()
        document = self.r.decoded("context", "inspect", context)[0]["Endpoints"]["docker"]
        endpoint = host or document["Host"]
        if document.get("SkipTLSVerify") or not re.fullmatch(r"unix:///[^\s\x00-\x1f]+", endpoint):
            raise RolloutError("Só socket Unix local explicitamente validado é aceito.")
        return endpoint

    def request(self, method, route, document=None, *, missing=None, update=False):
        connection = http.client.HTTPConnection("localhost", timeout=30)
        send_started = False
        try:
            transport = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            transport.settimeout(30)
            connection.sock = transport
            transport.connect(self.endpoint.removeprefix("unix://"))
            body = (
                document
                if isinstance(document, bytes)
                else canonical(document)
                if document is not None
                else None
            )
            if body is not None and len(body) > 1048576:
                error = UpdateNotSent if update else RolloutError
                raise error("A Spec excede o limite operacional; nada foi enviado.")
            send_started = True
            connection.request(method, route, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            if update and 400 <= response.status < 500:
                raise UpdateRejected(
                    "O Engine recusou o POST; esta tentativa não pode adotar outra escrita."
                )
            raw = response.read(1048577)
            if len(raw) > 1048576:
                raise ValueError
            if update and not raw:
                raise UpdateUnknown(
                    "O POST não devolveu documento de resultado; preserve o recibo."
                )
            value = json.loads(raw) if raw else {}
            if update:
                # O SwarmKit devolve esse conflito antes do commit. Outros500
                # podem esconder falha RPC depois do commit e ficam indeterminados.
                if response.status == 500 and value in (
                    {"message": "update out of sequence"},
                    {"message": "rpc error: code = Unknown desc = update out of sequence"},
                ):
                    raise UpdateRejected(
                        "CAS recusada pelo Engine; esta tentativa permanece fechada."
                    )
                if (
                    response.status != 200
                    or not isinstance(value, dict)
                    or set(value) - {"Warnings"}
                    or (
                        value.get("Warnings") is not None
                        and (
                            not isinstance(value["Warnings"], list)
                            or not all(isinstance(warning, str) for warning in value["Warnings"])
                        )
                    )
                ):
                    raise UpdateUnknown(
                        "A resposta do POST não comprova o resultado; preserve o recibo."
                    )
            if (
                missing is not None
                and response.status == 404
                and value == {"message": "No such image: " + missing}
            ):
                raise ImageMissing("ImageInspect confirmou a referência esperada ausente.")
            if response.status not in {200, 201} or not isinstance(value, dict):
                raise RolloutError(
                    "Engine recusou a operação; preserve blocked e reconcilie o recibo."
                )
            return value
        except (OSError, http.client.HTTPException, ValueError, TypeError):
            error = (UpdateUnknown if send_started else UpdateNotSent) if update else RolloutError
            raise error(
                "Resposta do Engine inválida ou sem conclusão; reconcilie sem retry cego."
            ) from None
        finally:
            connection.close()

    def confirm(self):
        if self.endpoint_for_cli() != self.endpoint:
            raise RolloutError("O endpoint Docker mudou durante a recuperação.")
        cli = self.r.decoded("info", "--format", "{{json .}}")
        info = self.request("GET", "/v1.52/info")
        if any(
            value.get("ID") != self.identity["daemon"]
            or value.get("Swarm", {}).get("NodeID") != self.node
            or value.get("Swarm", {}).get("ControlAvailable") is not True
            for value in (cli, info)
        ):
            raise RolloutError("CLI e Engine não comprovam o mesmo daemon/manager/nó.")
        node = self.request("GET", "/v1.52/nodes/" + quote(self.node, safe=""))
        if (
            node.get("ID") != self.node
            or node["Status"]["State"] != "ready"
            or node.get("ManagerStatus", {}).get("Reachability") != "reachable"
        ):
            raise RolloutError("O nó local não está ready/reachable.")

    def image(self, ref):
        if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_./:@-]{1,512}", ref):
            raise RolloutError("Referência de imagem inválida para recuperação.")
        value = self.request("GET", "/v1.52/images/" + quote(ref, safe="") + "/json", missing=ref)
        if not DIGEST.fullmatch(value.get("Id", "")):
            raise RolloutError("O Engine não confirmou identidade executável da imagem.")
        return value["Id"]

    def service(self, name, *, insert_defaults=False):
        projection = "true" if insert_defaults else "false"
        return self.request(
            "GET", "/v1.52/services/" + quote(name, safe="") + "?insertDefaults=" + projection
        )

    def container(self, cid):
        return self.request("GET", "/v1.52/containers/" + quote(cid, safe="") + "/json")

    def update(self, service_id, version, spec):
        if type(version) is not int or version < 1:
            raise UpdateNotSent("Versão da baseline inválida para CAS; POST não iniciado.")
        try:
            self.confirm()
        except (RolloutError, OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise UpdateNotSent("A confirmação do daemon falhou antes de iniciar o POST.") from None
        # A versão é a baseline validada; não há inspect/refresh/retry no envio.
        self.request(
            "POST",
            "/v1.52/services/"
            + quote(service_id, safe="")
            + f"/update?version={version}&registryAuthFrom=spec&rollback=none",
            spec,
            update=True,
        )


# Código fixo enviado por stdin à Current. A transação só lê metadados; o documento
# existente é validado dentro da imagem e somente seu hash deixa o container.
READ_STATE = r"""
import hashlib, json, os
import sqlalchemy as sa
from app.db import make_engine
from app.deployment_contract import _validate_evidence
engine = None
try:
    engine = make_engine(os.environ["DATABASE_URL"])
    with engine.connect() as connection, connection.begin():
        connection.execute(sa.text("SET TRANSACTION READ ONLY"))
        revisions = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
        row = connection.execute(sa.text("SELECT minimum_contract, phase, epoch, attested_at, evidence FROM deployment_contract WHERE id = 1")).mappings().one()
        valid = False
        evidence_hash = None
        evidence_scope = None
        if row["phase"] == "verified":
            evidence = _validate_evidence(row["evidence"], required=3)
            evidence_hash = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            valid = True
            evidence_scope = {"stack": evidence["stack"], "node": evidence["node_id"], "images": [evidence["services"][role]["image"] for role in ("api", "worker")]}
        elif row["evidence"] != {} or row["attested_at"] is not None:
            raise ValueError()
        result = {key: row[key] for key in ("minimum_contract", "phase")}
        result.update(schema=revisions[0] if len(revisions) == 1 else None,
                      epoch=str(row["epoch"]) if row["epoch"] else None,
                      attested_at=row["attested_at"].isoformat() if row["attested_at"] else None,
                      evidence_valid=valid, evidence_hash=evidence_hash, evidence_scope=evidence_scope)
    print(json.dumps(result))
except Exception:
    print('{"error":"recovery_state_unavailable"}')
    raise SystemExit(1)
finally:
    if engine is not None: engine.dispose()
"""


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class Recovery:
    """Uma tentativa com recibo e CAS, sob o lock canônico de rollout.py."""

    def __init__(self, rollout, current, previous, receipt):
        self.r = rollout
        self.current = current
        self.previous = previous
        self.path = receipt

    def state(self):
        r = self.r
        raw = r.docker(
            "run",
            "--rm",
            "-i",
            "--network",
            f"{r.stack}_internal",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--env-file",
            str(r.folder / "app.env"),
            self.current,
            "python",
            "-",
            stdin=READ_STATE,
        ).stdout
        if len(raw) > 4096:
            raise RolloutError("Metadados do banco excederam o limite seguro.")
        value = json.loads(raw)
        if (
            set(value)
            != {
                "schema",
                "minimum_contract",
                "phase",
                "epoch",
                "attested_at",
                "evidence_valid",
                "evidence_hash",
                "evidence_scope",
            }
            or value["schema"] != "0020_draft_test_commands"
            or type(value["minimum_contract"]) is not int
            or value["minimum_contract"] != 3
        ):
            raise RolloutError("A recuperação exige schema0020 e piso3 comprovados.")
        return value

    def load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_size > 65536
            ):
                raise RolloutError("Recibo inseguro; preserve blocked e reconcilie manualmente.")
            value = json.load(stream)
        if (
            set(value)
            != {
                "format",
                "stack",
                "engine",
                "current",
                "expected_previous",
                "baseline",
                "epoch",
                "initial_state",
                "result",
                "stage",
                "roles",
                "attestation_hash",
            }
            or value["format"] != 2
            or value["stack"] != self.r.stack
            or value["engine"] != self.engine.identity
            or value["current"] != self.current
            or value["expected_previous"] != self.previous
            or value["result"] != "current_replaces_lost_history"
            or value["stage"] not in {"prepared", "begun", "attesting", "done"}
            or set(value["baseline"]) != {"api", "worker"}
            or set(value["roles"]) != {"api", "worker"}
        ):
            raise RolloutError("O recibo não pertence a esta recuperação.")
        import uuid

        if str(uuid.UUID(value["epoch"])) != value["epoch"]:
            raise RolloutError("Epoch inválido no recibo.")
        for role, row in value["baseline"].items():
            progress = value["roles"][role]
            allowed = {
                "pending": {"not_attempted", "not_sent"},
                "inflight": {"intent_recorded", "unknown", "acknowledged"},
                "complete": {"intent_recorded", "unknown", "acknowledged"},
                "rejected": {"rejected"},
                "failed": {"unclassified"},
            }
            if (
                set(row) != {"id", "version", "spec", "previous", "previous_ref", "tasks"}
                or type(row["version"]) is not int
                or row["version"] < 1
                or row["previous_ref"] != self.previous[role]
                or not all(
                    re.fullmatch(r"[a-f0-9]{64}", row[key]) for key in ("spec", "previous", "tasks")
                )
                or set(value["roles"][role]) != {"phase", "version", "request_result"}
                or value["roles"][role]["phase"]
                not in {"pending", "inflight", "complete", "rejected", "failed"}
                or value["roles"][role]["request_result"]
                not in {
                    "not_attempted",
                    "intent_recorded",
                    "acknowledged",
                    "unknown",
                    "rejected",
                    "not_sent",
                    "unclassified",
                }
                or progress["request_result"] not in allowed[progress["phase"]]
                or (
                    progress["phase"] == "complete"
                    and (
                        type(progress["version"]) is not int
                        or progress["version"] <= row["version"]
                    )
                )
                or (progress["phase"] != "complete" and progress["version"] is not None)
            ):
                raise RolloutError("Baseline incompleta no recibo; não repita a escrita.")
        return value

    def save(self, *, create=False):
        if not create and self.load() is None:
            raise RolloutError("Recibo desapareceu; não continue a tentativa.")
        fd, name = tempfile.mkstemp(prefix=".recovery-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.receipt, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            if create:
                os.link(name, self.path)  # Criação exclusiva, inclusive contra symlink pendente.
            else:
                os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def reconcile(self, snapshot):
        if any(
            progress["phase"] in {"rejected", "failed"}
            for progress in self.receipt["roles"].values()
        ):
            raise RolloutError(
                "Há recusa ou erro não classificado no recibo; estado equivalente não autoriza retomada."
            )
        for role, row in snapshot.items():
            baseline = self.receipt["baseline"][role]
            progress = self.receipt["roles"][role]
            if any(row[key] != baseline[key] for key in ("id", "spec", "tasks")):
                raise RolloutError(
                    "Current ou execução mudou; reconciliação desconhecida, gate fechado."
                )
            untouched = (
                row["version"] == baseline["version"] and row["previous"] == baseline["previous"]
            )
            committed = row["version"] > baseline["version"] and row["previous"] == baseline["spec"]
            if progress["phase"] == "complete":
                if not committed or row["version"] != progress["version"]:
                    raise RolloutError("Outro writer alterou o retorno recomposto; não ateste.")
            elif (
                progress["phase"] == "inflight"
                and committed
                and progress["request_result"] in {"intent_recorded", "unknown", "acknowledged"}
            ):
                # Observa compatibilidade do estado, sem deduzir autoria quando
                # não houve resposta. O resultado do envio permanece no recibo.
                progress.update(phase="complete", version=row["version"])
                self.save()
            elif progress["request_result"] == "acknowledged":
                raise RolloutError(
                    "A resposta positiva não corresponde ao estado observado; não repita o POST."
                )
            elif not untouched:
                raise RolloutError("Não foi possível atribuir este estado à tentativa; não repita.")
            else:
                try:
                    self.engine.image(self.previous[role])
                except ImageMissing:
                    pass
                else:
                    raise RolloutError(
                        "A referência histórica mudou de disponibilidade; reconcilie sem escrever."
                    )

    def require_blocked(self):
        value = self.state()
        if (
            value["phase"] != "blocked"
            or value["epoch"] != self.r.epoch
            or value["attested_at"] is not None
            or value["evidence_valid"] is not False
        ):
            raise RolloutError("O gate não confirma piso3/blocked e epoch próprio; não atualize.")

    def snapshot(self):
        r = self.r
        self.engine.confirm()
        result = {}
        self.specs = {}
        for role in ("api", "worker"):
            service = self.engine.service(f"{r.stack}_{role}")
            spec = service["Spec"]
            self.specs[role] = canonical(spec)
            if (
                type(service["Version"]["Index"]) is not int
                or service["Version"]["Index"] < 1
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", service["ID"])
                or spec["Name"] != f"{r.stack}_{role}"
                or type(spec["Mode"]["Replicated"]["Replicas"]) is not int
            ):
                raise RolloutError("Identidade, versão ou réplicas da baseline são inválidas.")
            if service.get("UpdateStatus", {}).get("State", "") not in {"", "completed"}:
                raise RolloutError("Há atualização ou rollback em andamento; recuperação recusada.")
            if (
                self.engine.image(spec["TaskTemplate"]["ContainerSpec"]["Image"]) != self.current
                or r.image_id(spec["TaskTemplate"]["ContainerSpec"]["Image"]) != self.current
                or spec["Mode"]["Replicated"]["Replicas"] < 1
            ):
                raise RolloutError("A Current dos dois papéis deve ser a identidade esperada.")
            # A CLI inspeciona com defaults; compare documentos da mesma projeção.
            # Somente a leitura bruta acima fornece hashes e bytes para o POST CAS.
            projected = self.engine.service(service["ID"], insert_defaults=True)
            cli = r.decoded("service", "inspect", f"{r.stack}_{role}")[0]
            if any(
                view.get("ID") != service["ID"] or view.get("Version") != service["Version"]
                for view in (projected, cli)
            ):
                raise RolloutError("A identidade ou versão mudou entre as leituras da baseline.")
            # Igualdade entre documentos ausentes não comprova inventário. Os
            # campos essenciais da Current não são alterados pela interpolação;
            # PreviousSpec não é interpolada e deve continuar íntegra como a RAW.
            if any(
                not isinstance(view["Spec"], dict)
                or view["Spec"]["Name"] != spec["Name"]
                or view["Spec"]["TaskTemplate"]["ContainerSpec"]["Image"]
                != spec["TaskTemplate"]["ContainerSpec"]["Image"]
                or canonical(view["Spec"]["Mode"]) != canonical(spec["Mode"])
                or not isinstance(view["PreviousSpec"], dict)
                or not view["PreviousSpec"]
                or canonical(view["PreviousSpec"]) != canonical(service["PreviousSpec"])
                for view in (projected, cli)
            ):
                raise RolloutError(
                    "A projeção com defaults está incompleta ou diverge da baseline bruta."
                )
            if any(cli[key] != projected[key] for key in ("Spec", "PreviousSpec")):
                raise RolloutError("CLI e Engine divergem na projeção com defaults da baseline.")
            rows = r.observe(role)
            active = [row for row in rows if row["running"]]
            if (
                len(active) != spec["Mode"]["Replicated"]["Replicas"]
                or any(
                    row["running"]
                    or row["state"] not in {"complete", "shutdown", "failed", "rejected", "remove"}
                    for row in rows
                    if row not in active
                )
                or any(
                    not row["healthy"] or row["state"] != "running" or row["image"] != self.current
                    for row in active
                )
            ):
                raise RolloutError("A frota não está saudável e estável para recuperação.")
            executions = []
            for row in active:
                task = r.tracked[role][row["id"]]
                expected = copy.deepcopy(spec["TaskTemplate"])
                actual = copy.deepcopy(task["Spec"])
                # SwarmKit ignora PullOptions de tasks já running. Qualquer outra
                # diferença é recusada conservadoramente, inclusive placement.
                expected["ContainerSpec"].pop("PullOptions", None)
                actual["ContainerSpec"].pop("PullOptions", None)
                if (
                    task["DesiredState"] != "running"
                    or task["ServiceID"] != service["ID"]
                    or expected != actual
                    or task.get("Endpoint", {}).get("Spec", {}) != spec.get("EndpointSpec", {})
                ):
                    raise RolloutError("Uma task está suja em relação à Current ou endpoint.")
                container = self.engine.container(row["container_id"])
                if (
                    container["Id"] != row["container_id"]
                    or type(container["RestartCount"]) is not int
                    or not container["State"]["Running"]
                    or container["State"]["Health"]["Status"] != "healthy"
                    or container["Image"] != self.current
                    or container["RestartCount"] != 0
                    or not r.valid_execution_times(
                        container["State"]["StartedAt"],
                        container["State"]["StartedAt"],
                        time.time_ns(),
                    )
                ):
                    raise RolloutError("O Engine não confirma a execução saudável da Current.")
                executions.append(
                    {
                        "task": row,
                        "started_at": container["State"]["StartedAt"],
                        "restarts": container["RestartCount"],
                    }
                )
            result[role] = {
                "id": service["ID"],
                "version": service["Version"]["Index"],
                "spec": fingerprint(spec),
                "previous": fingerprint(service.get("PreviousSpec")),
                "previous_ref": service["PreviousSpec"]["TaskTemplate"]["ContainerSpec"]["Image"],
                "tasks": fingerprint(
                    {
                        "active": sorted(executions, key=lambda row: row["task"]["id"]),
                        "retired": sorted(
                            [row for row in rows if row not in active], key=lambda row: row["id"]
                        ),
                    }
                ),
            }
        self.engine.confirm()
        return result

    def execute(self):
        try:
            self._execute()
        except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise RolloutError(
                "Inventário, metadados ou recibo inválidos; reconcilie mantendo o gate fechado."
            ) from None

    def _execute(self):
        r = self.r
        if (
            r.rollback
            or not DIGEST.fullmatch(self.current)
            or r.image != self.current
            or set(self.previous) != {"api", "worker"}
            or not all(self.previous.values())
        ):
            raise RolloutError(
                "Recuperação exclusiva exige CurrentID e Previous esperadas dos dois papéis."
            )
        if (
            self.path.parent.resolve() != r.folder.resolve()
            or self.path.is_symlink()
            or self.path.suffix != ".json"
        ):
            raise RolloutError("O recibo deve ficar na pasta canônica, sem symlink.")
        self.engine = EngineUnix(r)
        r.node = self.engine.node
        r.target = self.current
        r.floor = 3
        if self.engine.image(self.current) != self.current or r.capability(self.current) != 3:
            raise RolloutError("A Current precisa comprovar capacidade3 exata.")
        alias = "leadkit-local:" + self.current.removeprefix("sha256:")
        try:
            if self.engine.image(alias) != self.current:
                raise RolloutError("O alias preservado colide com outra imagem.")
        except ImageMissing:
            raise RolloutError(
                "O alias da Current não está preservado; confira os bytes antes de begin."
            ) from None
        status = self.state()
        baseline = self.snapshot()
        self.receipt = self.load()
        if self.receipt is None:
            if (
                status["phase"] != "verified"
                or not status["epoch"]
                or not status["attested_at"]
                or status["evidence_valid"] is not True
                or status["evidence_scope"]
                != {"stack": r.stack, "node": r.node, "images": [self.current, self.current]}
            ):
                raise RolloutError(
                    "A recuperação inicial exige atestação verified existente e válida."
                )
            for role, row in baseline.items():
                if row["previous_ref"] != self.previous[role]:
                    raise RolloutError("A referência histórica difere da esperada.")
                try:
                    self.engine.image(self.previous[role])
                except ImageMissing:
                    continue
                raise RolloutError(
                    "A imagem histórica existe; este modo só substitui histórico perdido."
                )
            if baseline != self.snapshot() or status != self.state():
                raise RolloutError("A baseline mudou antes de begin; nada foi iniciado.")
            self.receipt = {
                "format": 2,
                "stack": r.stack,
                "engine": self.engine.identity,
                "current": self.current,
                "expected_previous": self.previous,
                "baseline": baseline,
                "epoch": r.epoch,
                "initial_state": status,
                "result": "current_replaces_lost_history",
                "stage": "prepared",
                "roles": {
                    role: {"phase": "pending", "version": None, "request_result": "not_attempted"}
                    for role in baseline
                },
                "attestation_hash": None,
            }
            self.save(create=True)
        r.epoch = self.receipt["epoch"]
        if self.receipt["stage"] == "done" and (
            status["phase"] != "verified"
            or status["epoch"] != r.epoch
            or status["evidence_hash"] != self.receipt["attestation_hash"]
        ):
            raise RolloutError(
                "Recibo concluído só verifica a atestação correspondente; não reabre bloqueio posterior."
            )
        self.reconcile(baseline)
        if status == self.receipt["initial_state"] and self.receipt["stage"] == "prepared":
            if r.contract("begin", {"epoch": r.epoch}) != {"ok": True}:
                raise RolloutError("begin não confirmou o commit; reconcilie antes de continuar.")
            self.require_blocked()
            self.receipt["stage"] = "begun"
            self.save()
        elif status["epoch"] != r.epoch:
            raise RolloutError("Epoch alheio; o recibo não autoriza esta tentativa.")
        elif status["phase"] == "verified":
            if (
                self.receipt["stage"] not in {"attesting", "done"}
                or status["evidence_hash"] != self.receipt["attestation_hash"]
            ):
                raise RolloutError("Atestação não reconhecida no recibo; preserve o gate.")
            self.finish(already_attested=True)
            return
        self.require_blocked()
        for role, row in self.receipt["baseline"].items():
            self.reconcile(self.snapshot())
            progress = self.receipt["roles"][role]
            if progress["phase"] == "complete":
                continue
            body = self.specs[role]
            if hashlib.sha256(body).hexdigest() != row["spec"]:
                raise RolloutError("A Spec mudou antes do CAS; preserve o gate bloqueado.")
            self.require_blocked()
            # Só registra intenção após as verificações locais; morte sem
            # desfecho persistido continua explicitamente indeterminada.
            progress.update(phase="inflight", request_result="intent_recorded")
            self.save()
            try:
                self.engine.update(row["id"], row["version"], body)
            except UpdateRejected:
                progress.update(phase="rejected", request_result="rejected")
                self.save()
                raise
            except UpdateNotSent:
                progress.update(phase="pending", request_result="not_sent")
                self.save()
                raise
            except UpdateUnknown:
                progress["request_result"] = "unknown"
                self.save()
                raise
            except Exception:  # noqa: BLE001 — não classifica erro arbitrário como resultado desconhecido.
                progress.update(phase="failed", request_result="unclassified")
                self.save()
                raise RolloutError(
                    "Erro de envio não classificado; reconciliação automática recusada."
                ) from None
            progress["request_result"] = "acknowledged"
            self.save()
            self.reconcile(self.snapshot())
            if progress["phase"] != "complete":
                raise RolloutError("O Engine não confirmou commit; reconcilie sem retry cego.")
        self.finish()

    def finish(self, *, already_attested=False):
        r = self.r
        final = self.snapshot()
        self.reconcile(final)
        if any(row["phase"] != "complete" for row in self.receipt["roles"].values()):
            raise RolloutError("Faltam papéis recompostos; o gate permanece bloqueado.")
        evidence = r.verify()
        if evidence != r.verify() or final != self.snapshot():
            raise RolloutError("A frota mudou antes da atestação; mantenha blocked.")
        if not already_attested:
            self.require_blocked()
            self.receipt.update(stage="attesting", attestation_hash=fingerprint(evidence))
            self.save()
            if r.contract("attest", {"epoch": r.epoch, "evidence": evidence}) != {"ok": True}:
                raise RolloutError("Atestação sem resposta conclusiva; reconcilie o recibo.")
        final_state = self.state()
        if (
            final_state["phase"] != "verified"
            or final_state["epoch"] != r.epoch
            or final_state["evidence_valid"] is not True
            or final_state["evidence_hash"] != self.receipt["attestation_hash"]
            or fingerprint(evidence) != self.receipt["attestation_hash"]
        ):
            raise RolloutError("A atestação final não foi confirmada.")
        self.receipt["stage"] = "done"
        self.save()
        print("deploy.sh: retorno recomposto com Current; imagem histórica não restaurada")
