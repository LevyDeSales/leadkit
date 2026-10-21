"""Contratos mínimos de encaminhamento, sem respostas ou identidade da participante."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
)


class HandoffError(Exception):
    """Recusa segura compartilhada pelas interfaces de gestão e serviço."""

    def __init__(
        self,
        code: str,
        *,
        status: int = 409,
        field: str = "",
        credential_id: UUID | None = None,
        current_version: str | None = None,
    ):
        self.code, self.status, self.field, self.credential_id = code, status, field, credential_id
        self.current_version = current_version
        super().__init__(code)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SaveReceiver(Input):
    expected_version: str
    name: str = Field(min_length=1, max_length=100)
    entry_url: str = Field(max_length=2000)
    reference_ttl_seconds: int = Field(default=604800, ge=60, le=604800)
    confirm: bool = False


class SetReceiverEnabled(Input):
    expected_version: str
    enabled: bool
    confirm: bool = False


class CreateCredential(Input):
    name: str = Field(min_length=1, max_length=100)
    receiver_revision: int = Field(ge=1)
    expected_receiver_version: str
    expires_at: AwareDatetime | None
    idempotency_key: UUID
    confirm: bool = False


class RotateCredential(Input):
    expected_version: str
    expires_at: AwareDatetime | None
    idempotency_key: UUID
    confirm: bool = False


class Revoke(Input):
    expected_version: str
    confirm: bool = False


class HandoffReceiverInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    form_slug: str
    name: str
    entry_url: str
    purpose: Literal["support"]
    projection: Literal["correlation.v1"]
    reference_ttl_seconds: int
    revision: int
    version: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class IntegrationCredentialInfo(BaseModel):
    id: UUID
    key_id: str
    name: str
    receiver_id: UUID
    receiver_revision: int
    form_slug: str
    purpose: Literal["support"] = "support"
    scope: Literal["handoffs:resolve"] = "handoffs:resolve"
    projection: Literal["correlation.v1"] = "correlation.v1"
    version: str
    state: Literal["active", "expired", "revoked"]
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class CreatedIntegrationCredential(BaseModel):
    credential: IntegrationCredentialInfo
    secret: SecretStr

    @field_serializer("secret", when_used="json")
    def expose_once(self, value: SecretStr) -> str:
        """Somente este DTO de emissão expõe a credencial utilizável."""
        return value.get_secret_value()


class HandoffInfo(BaseModel):
    id: UUID
    submission_id: UUID
    receiver_id: UUID
    receiver_revision: int
    purpose: Literal["support"]
    projection: Literal["correlation.v1"]
    issuance_state: str
    unavailable_code: str | None
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    version: str


class ResolvedHandoff(BaseModel):
    schema_version: Literal[1] = 1
    reference_id: UUID
    submission_id: UUID
    form_slug: str
    form_version_number: int
    generation: Literal["none", "pdf"]
    purpose: Literal["support"] = "support"
    receiver_id: UUID
    receiver_revision: int
    projection: Literal["correlation.v1"] = "correlation.v1"
    submitted_at: datetime
    expires_at: datetime


class ResolveHandoffRequest(Input):
    reference: SecretStr = Field(
        min_length=47, max_length=47, json_schema_extra={"pattern": r"^lkh_[A-Za-z0-9_-]{43}$"}
    )

    @field_validator("reference")
    @classmethod
    def valid_reference(cls, value: SecretStr):
        import re

        if not re.fullmatch(r"lkh_[A-Za-z0-9_-]{43}", value.get_secret_value()):
            raise ValueError("referência inválida")
        return value


def confirmed(command) -> None:
    if not command.confirm:
        raise HandoffError("confirmation_required", status=422, field="confirm")


def version_matches(actual: int, expected: str) -> None:
    if str(actual) != expected:
        raise HandoffError("version_conflict", current_version=str(actual))
