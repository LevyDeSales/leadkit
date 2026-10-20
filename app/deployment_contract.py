"""Piso persistido de compatibilidade; não consulta Docker nem serviços externos."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.integration_readiness import configuration_lock
from app.models import DeploymentContract

PARTICIPANT_CONTRACT = 1


class DeploymentContractError(Exception):
    """Erro estável, sem metadados operacionais ou parâmetros do banco."""

    code = "deployment_contract_blocked"
    message = "A atualização ainda não permite entregas à participante. Aguarde a conclusão."

    def __init__(self) -> None:
        super().__init__(self.message)


def _state(session: Session) -> DeploymentContract | None:
    if not sa.inspect(session.connection()).has_table("deployment_contract"):
        return None
    return session.scalar(
        sa.select(DeploymentContract)
        .where(DeploymentContract.id == 1)
        .execution_options(populate_existing=True)
    )


def assert_runtime_contract(session: Session) -> None:
    """Confere o piso antes de preparar destinos ou registrar workflows."""
    try:
        state = _state(session)
        if state is not None and state.minimum_contract > PARTICIPANT_CONTRACT:
            raise DeploymentContractError
    except SQLAlchemyError:
        raise DeploymentContractError from None


def assert_participant_contract(session: Session) -> None:
    """A transação chamadora mantém a trava compartilhada até o fim do transporte."""
    try:
        configuration_lock(session, shared=True)
        state = _state(session)
        if (
            state is None
            or state.minimum_contract != PARTICIPANT_CONTRACT
            or state.phase != "verified"
        ):
            raise DeploymentContractError
        if state.epoch is None or state.attested_at is None:
            raise DeploymentContractError
        _validate_evidence(state.evidence)
    except SQLAlchemyError:
        raise DeploymentContractError from None


def begin_rollout(session: Session, *, epoch) -> None:
    """Fecha a habilitação antes de alterar a frota; não reduz o piso."""
    import uuid

    try:
        epoch = uuid.UUID(str(epoch))
        configuration_lock(session)
        if not sa.inspect(session.connection()).has_table("deployment_contract"):
            raise DeploymentContractError
        state = _state(session)
        if state is None:
            state = DeploymentContract(id=1, minimum_contract=1)
            session.add(state)
        if state.minimum_contract > PARTICIPANT_CONTRACT:
            raise DeploymentContractError
        state.minimum_contract = max(1, state.minimum_contract)
        state.phase = "blocked"
        state.epoch = epoch
        state.evidence = {}
        state.attested_at = None
        session.flush()
    except (SQLAlchemyError, ValueError, TypeError):
        raise DeploymentContractError from None


def attest_rollout(session: Session, *, epoch, evidence) -> None:
    """Aceita somente o manifesto completo da tentativa operacional corrente."""
    import uuid

    from app.models import utcnow

    try:
        epoch = uuid.UUID(str(epoch))
        manifest = _validate_evidence(evidence)
        configuration_lock(session)
        state = _state(session)
        if (
            state is None
            or state.epoch != epoch
            or state.phase != "blocked"
            or state.minimum_contract > PARTICIPANT_CONTRACT
        ):
            raise DeploymentContractError
        state.minimum_contract = max(1, state.minimum_contract)
        state.evidence = manifest
        state.attested_at = utcnow()
        state.phase = "verified"
        session.flush()
    except (SQLAlchemyError, ValueError, TypeError):
        raise DeploymentContractError from None


def _validate_evidence(evidence):
    """Aceita apenas o contrato fechado de metadados; nenhum campo arbitrário persiste."""
    from typing import Annotated, Literal

    from pydantic import BaseModel, ConfigDict, Field, ValidationError

    # Classes locais evitam que a CLI capability precise montar validação operacional.
    class Metadata(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)

    identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")]
    digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]

    class Task(Metadata):
        id: identifier
        node_id: identifier
        container_id: identifier
        image: digest
        state: Literal["running", "complete", "shutdown", "failed", "rejected", "remove"]
        healthy: bool
        running: bool

    class Service(Metadata):
        id: identifier
        spec_version: Annotated[int, Field(ge=1)]
        observed_spec_version: Annotated[int, Field(ge=1)]
        image: digest
        contract: Literal[1]
        previous_image: digest
        previous_contract: Literal[1]
        replicas: Annotated[int, Field(ge=1)]
        tasks: list[Task]
        retired: list[Task]

    class Services(Metadata):
        api: Service
        worker: Service

    class Evidence(Metadata):
        schema_version: Literal[1]
        stack: identifier
        node_id: identifier
        services: Services

    try:
        parsed = Evidence.model_validate(evidence)
        seen = set()
        for service in (parsed.services.api, parsed.services.worker):
            if (
                service.spec_version != service.observed_spec_version
                or len(service.tasks) != service.replicas
            ):
                raise DeploymentContractError
            for task in service.tasks:
                if (
                    task.state != "running"
                    or not task.running
                    or not task.healthy
                    or task.image != service.image
                ):
                    raise DeploymentContractError
            for task in service.retired:
                if task.running or task.state == "running":
                    raise DeploymentContractError
            for task in service.tasks + service.retired:
                if task.node_id != parsed.node_id or task.id in seen:
                    raise DeploymentContractError
                seen.add(task.id)
        return parsed.model_dump()
    except (ValidationError, ValueError, TypeError):
        raise DeploymentContractError from None


def _status(session: Session) -> dict:
    """Estado resumido sem documento de evidência nem configuração sensível."""
    exists = sa.inspect(session.connection()).has_table("deployment_contract")
    state = _state(session) if exists else None
    return {
        "minimum_contract": max(1, state.minimum_contract) if state is not None else int(exists),
        "phase": state.phase if state is not None else "blocked",
        "epoch": str(state.epoch) if state is not None and state.epoch is not None else None,
    }


def main(argv=None) -> int:
    """CLI da imagem: saída JSON fechada, erros sem traceback ou valores recebidos."""
    import json
    import os
    import sys
    import uuid

    args = sys.argv[1:] if argv is None else argv
    invalid = False
    engine = None
    try:
        if len(args) != 1 or args[0] not in {"capability", "status", "begin", "attest"}:
            invalid = True
            raise DeploymentContractError
        action = args[0]
        if action == "capability":
            print(json.dumps({"contract": PARTICIPANT_CONTRACT}))
            return 0
        document = {}
        if action in {"begin", "attest"}:
            try:
                raw = sys.stdin.read(65537)
                if len(raw) > 65536:
                    raise ValueError
                document = json.loads(raw)
                expected = {"epoch", "evidence"} if action == "attest" else {"epoch"}
                if not isinstance(document, dict) or set(document) != expected:
                    raise ValueError
                document["epoch"] = uuid.UUID(document["epoch"])
                if action == "attest":
                    document["evidence"] = _validate_evidence(document["evidence"])
            except (ValueError, TypeError, DeploymentContractError):
                invalid = True
                raise DeploymentContractError from None
        from app.db import make_engine

        database_url = os.environ.get("DATABASE_URL", "")
        if sa.engine.make_url(database_url).get_backend_name() != "postgresql":
            raise DeploymentContractError
        engine = make_engine(database_url)
        with Session(engine) as session, session.begin():
            if action == "status":
                result = _status(session)
            else:
                if action == "begin":
                    begin_rollout(session, **document)
                else:
                    attest_rollout(session, **document)
                result = {"ok": True}
        print(json.dumps(result))
        return 0
    except (DeploymentContractError, SQLAlchemyError, OSError, ValueError, TypeError):
        print(
            json.dumps(
                {
                    "error": {
                        "code": DeploymentContractError.code,
                        "message": DeploymentContractError.message,
                    }
                },
                ensure_ascii=False,
            )
        )
        return 2 if invalid else 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
