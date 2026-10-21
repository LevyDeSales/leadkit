"""Contrato fechado de declarações externas e comandos administrativos."""

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from app.handoff_types import Input

OpaqueId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]
EventType = Literal[
    "correlation.linked",
    "invitation.sent",
    "meeting.state_reported",
    "meeting.attendance_recorded",
    "notification.delivery_reported",
    "participant.receipt_acknowledged",
    "entity.withdrawn",
]
EntityKind = Literal["context", "invitation", "meeting", "attendance", "notification", "receipt"]
Channel = Literal["chat", "email", "sms", "voice", "system", "other"]
EvidenceKind = Literal[
    "correlation_record",
    "dispatch_record",
    "calendar_record",
    "attendance_record",
    "provider_delivery_receipt",
    "participant_ack",
    "operator_correction",
]
BASE_TYPES = dict(
    zip(
        ("context", "invitation", "meeting", "attendance", "notification", "receipt"),
        (
            "correlation.linked",
            "invitation.sent",
            "meeting.state_reported",
            "meeting.attendance_recorded",
            "notification.delivery_reported",
            "participant.receipt_acknowledged",
        ),
        strict=True,
    )
)
EVIDENCE = {
    "correlation.linked": {"correlation_record"},
    "invitation.sent": {"dispatch_record"},
    "meeting.state_reported": {"calendar_record"},
    "meeting.attendance_recorded": {"attendance_record"},
    "notification.delivery_reported": {"dispatch_record", "provider_delivery_receipt"},
    "participant.receipt_acknowledged": {"participant_ack"},
    "entity.withdrawn": {"operator_correction"},
}


class Policy(Input):
    types: list[EventType] = Field(default_factory=list)
    channels: list[Channel] = Field(default_factory=list)
    evidence_kinds: list[EvidenceKind] = Field(default_factory=list)

    @model_validator(mode="after")
    def complete(self):
        if any(not EVIDENCE[kind].intersection(self.evidence_kinds) for kind in self.types) or (
            self.types and not self.channels
        ):
            raise ValueError("Escolha canais e evidências correspondentes.")
        return self


class SaveEventSource(Input):
    expected_version: str
    name: str = Field(min_length=1, max_length=100)
    policy: Policy
    confirm: bool = False


class CreateEventSource(SaveEventSource):
    receiver_id: UUID
    receiver_revision: int = Field(ge=1)
    namespace: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    idempotency_key: UUID


class SetEventSourceEnabled(Input):
    expected_version: str
    enabled: bool
    confirm: bool = False


class CreateEventCredential(Input):
    name: str = Field(min_length=1, max_length=100)
    expected_source_version: str
    source_revision: int = Field(ge=1)
    expires_at: AwareDatetime | None
    idempotency_key: UUID
    confirm: bool = False


class RotateEventCredential(Input):
    expected_version: str
    expires_at: AwareDatetime | None
    idempotency_key: UUID
    confirm: bool = False


class EraseJourney(Input):
    expected_journey_version: str
    expected_handoff_version: str
    confirm: bool = False


class Entity(Input):
    kind: EntityKind
    id: OpaqueId
    revision: int = Field(ge=1)


class Author(Input):
    kind: Literal["human", "service", "participant"]
    id: OpaqueId


class Evidence(Input):
    channel: Channel
    kind: EvidenceKind
    id: OpaqueId
    author: Author


class ContextData(Input):
    contact_id: OpaqueId | None
    conversation_id: OpaqueId | None
    assignee_id: OpaqueId | None

    @model_validator(mode="after")
    def nonempty(self):
        if not any((self.contact_id, self.conversation_id, self.assignee_id)):
            raise ValueError("Informe pelo menos um vínculo.")
        return self


class InvitationData(Input):
    conversation_id: OpaqueId | None
    notification_id: OpaqueId


class MeetingData(Input):
    status: Literal["scheduled", "cancelled"]
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None
    timezone: str = Field(max_length=64)
    change: Literal["created", "rescheduled", "timezone_corrected", "corrected"]

    @field_validator("starts_at", "ends_at")
    @classmethod
    def utc(cls, value):
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def valid_interval(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Escolha uma zona IANA válida.") from None
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("O término deve ser posterior ao início.")
        return self


class AttendanceData(Input):
    meeting_id: OpaqueId
    meeting_revision: int = Field(ge=1)
    status: Literal["attended", "no_show", "unknown"]
    change: Literal["attendance", "corrected"]


class NotificationData(Input):
    status: Literal["accepted", "confirmed", "failed"]
    conversation_id: OpaqueId | None


class ReceiptData(InvitationData):
    pass


class WithdrawData(Input):
    reason_code: Literal["incorrect", "duplicate", "erasure_request", "other"]


DATA_MODELS = {
    "correlation.linked": ContextData,
    "invitation.sent": InvitationData,
    "meeting.state_reported": MeetingData,
    "meeting.attendance_recorded": AttendanceData,
    "notification.delivery_reported": NotificationData,
    "participant.receipt_acknowledged": InvitationData,
    "entity.withdrawn": WithdrawData,
}


class JourneyEventInput(Input):
    schema_version: Literal[1]
    source_revision: int = Field(ge=1)
    event_id: OpaqueId
    reference_id: UUID
    type: EventType
    entity: Entity
    occurred_at: AwareDatetime
    evidence: Evidence
    data: (
        ContextData
        | InvitationData
        | MeetingData
        | AttendanceData
        | NotificationData
        | ReceiptData
        | WithdrawData
    )

    @field_validator("occurred_at")
    @classmethod
    def utc(cls, value):
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def matching_snapshot(self):
        if not isinstance(self.data, DATA_MODELS[self.type]) or (
            self.type != "entity.withdrawn" and BASE_TYPES[self.entity.kind] != self.type
        ):
            raise ValueError("Tipo e snapshot incompatíveis.")
        return self


class EventSourceInfo(BaseModel):
    id: UUID
    receiver_id: UUID
    receiver_revision: int
    form_slug: str
    purpose: Literal["support"] = "support"
    namespace: str
    name: str
    policy: Policy
    revision: int
    version: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class EventCredentialInfo(BaseModel):
    id: UUID
    source_id: UUID
    source_revision: int
    scope: Literal["journey:events:write"] = "journey:events:write"
    name: str
    key_id: str
    version: str
    state: Literal["active", "expired", "revoked"]
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class CreatedEventCredential(BaseModel):
    credential: EventCredentialInfo
    secret: SecretStr

    @field_serializer("secret", when_used="json")
    def expose_once(self, value):
        return value.get_secret_value()


class JourneyEventReceipt(BaseModel):
    id: UUID
    received_at: datetime
    replay: bool
    projection_effect: Literal["current", "historical", "withdrawn"]


class EventLeadData(BaseModel):
    event_id: OpaqueId
    entity_id: OpaqueId
    evidence: Evidence
    data: (
        ContextData
        | InvitationData
        | MeetingData
        | AttendanceData
        | NotificationData
        | ReceiptData
        | WithdrawData
    )


class JourneyEventInfo(BaseModel):
    id: UUID
    origin: Literal["external_declared"] = "external_declared"
    source_id: UUID
    source_revision: int
    type: EventType
    entity_kind: EntityKind
    entity_revision: int
    occurred_at: datetime
    received_at: datetime
    state: Literal["declared", "withdrawn", "erased"]
    status: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str | None = None
    meeting_event_id: UUID | None = None
    lead_data: EventLeadData | None = None


class LocalJob(BaseModel):
    id: UUID
    attempt: int
    status: str


class LocalFact(BaseModel):
    origin: Literal["local"] = "local"
    type: str
    state: str
    occurred_at: datetime
    attempt: int | None = None


class SubmissionJourney(BaseModel):
    submission_id: UUID
    reference_id: UUID | None
    reference_version: str | None
    journey_version: str
    job: LocalJob | None
    local_facts: list[LocalFact]
    external_declared: list[JourneyEventInfo]
