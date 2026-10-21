"""Disponibilidade técnica da tentativa atual; não concede autorização nem dispara geração."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.adapters.storage import ArtifactStorage
from app.errors import FailureCode, RetryableError, TerminalError
from app.models import Artifact, Job, Plan, utcnow
from app.pdf_validation import validate_pdf

PdfState = Literal["available", "missing", "removed", "unavailable", "unknown", "invalid"]


@dataclass(frozen=True)
class PdfAvailability:
    attempt: int
    state: PdfState
    exists: bool | None
    valid: bool | None
    accessible: bool | None
    verified_at: datetime | None = None

    @property
    def available(self) -> bool:
        return self.state == "available"


def artifact_availability(storage: ArtifactStorage | None, artifact: Artifact) -> PdfAvailability:
    """A prova vale só enquanto a identidade e o tamanho do objeto permanecerem iguais."""
    attempt = artifact.version
    verified = artifact.verified_at
    if artifact.purged_at is not None:
        return PdfAvailability(attempt, "removed", False, None, False, verified)
    if storage is None or not hasattr(storage, "head"):
        return PdfAvailability(attempt, "unknown", None, None, None, verified)
    try:
        obj = storage.head(artifact.storage_key)
    except FileNotFoundError:
        return PdfAvailability(attempt, "missing", False, None, False, verified)
    except Exception:  # noqa: BLE001 — fronteira de storage com erro seguro
        # Falhas transitórias e acesso negado não provam inexistência; nenhum erro bruto sai.
        return PdfAvailability(attempt, "unavailable", None, None, False, verified)
    if verified is None or artifact.storage_revision is None:
        return PdfAvailability(attempt, "unknown", True, None, None)
    if obj.byte_size != artifact.byte_size or obj.revision != artifact.storage_revision:
        return PdfAvailability(attempt, "invalid", True, False, None, verified)
    if not hasattr(storage, "probe"):
        return PdfAvailability(attempt, "unknown", True, True, None, verified)
    try:
        readable = storage.probe(artifact.storage_key)
    except NotImplementedError:
        return PdfAvailability(attempt, "unknown", True, True, None, verified)
    except Exception:  # noqa: BLE001 — fronteira de storage com erro seguro
        return PdfAvailability(attempt, "unavailable", True, True, False, verified)
    if readable != obj:
        return PdfAvailability(attempt, "invalid", True, False, None, verified)
    return PdfAvailability(attempt, "available", True, True, True, verified)


def current_pdf(session: Session, storage: ArtifactStorage | None, job: Job) -> PdfAvailability:
    artifact = session.scalar(
        sa.select(Artifact).where(
            Artifact.job_id == job.id, Artifact.kind == "pdf", Artifact.version == job.attempt
        )
    )
    if artifact is None:
        return PdfAvailability(job.attempt, "missing", False, None, False)
    result = artifact_availability(storage, artifact)
    if result.available and job.status != "completed":
        return PdfAvailability(job.attempt, "unavailable", True, True, True, result.verified_at)
    return result


def verified_content(storage: ArtifactStorage, artifact: Artifact) -> tuple[bytes, str]:
    """Lê uma vez, confere bytes/hash/tamanho e identidade antes/depois da leitura."""
    import hashlib

    try:
        before = storage.head(artifact.storage_key)
        content = storage.get(artifact.storage_key)
        after = storage.head(artifact.storage_key)
    except Exception:  # noqa: BLE001 — fronteira de storage com erro seguro
        raise RetryableError(FailureCode.STORAGE_FAILED, "PDF indisponível no storage") from None
    if (
        before != after
        or len(content) != artifact.byte_size
        or after.byte_size != len(content)
        or hashlib.sha256(content).hexdigest() != artifact.sha256
    ):
        raise TerminalError(FailureCode.RENDER_FAILED, "PDF diverge do artefato registrado")
    validate_pdf(content)
    return content, after.revision


def verify_completion(
    session: Session, storage: ArtifactStorage, job: Job, attempt: int, artifact_id: UUID
) -> bytes:
    """Vincula a evidência à tentativa e ao plano corretos antes de marcar completed."""
    artifact = session.get(Artifact, artifact_id)
    if (
        artifact is None
        or artifact.job_id != job.id
        or artifact.version != attempt
        or artifact.kind != "pdf"
        or artifact.purged_at is not None
    ):
        raise TerminalError(FailureCode.RENDER_FAILED, "artefato incompatível com a tentativa")
    plan = session.get(Plan, artifact.plan_id)
    if plan is None or plan.job_id != job.id or plan.version != attempt:
        raise TerminalError(FailureCode.RENDER_FAILED, "plano incompatível com a tentativa")
    content, revision = verified_content(storage, artifact)
    artifact.verified_at = utcnow()
    artifact.storage_revision = revision
    return content


def availability_dict(value: PdfAvailability) -> dict[str, Any]:
    data = asdict(value)
    data["available"] = value.available
    data["verified_at"] = (
        value.verified_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if value.verified_at
        else None
    )
    return data
