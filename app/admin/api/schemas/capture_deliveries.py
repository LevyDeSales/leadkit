"""Contrato de estado da entrega, separado dos dados pessoais e do recibo."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

from app.admin.api.common import ConfirmedInput


class CaptureDeliveryState(BaseModel):
    delivery_id: str
    submission_id: str
    destination_id: str
    kind: Literal["baserow", "webhook"]
    event: Literal["submissao.persistida"]
    config_version: int
    current_config_version: int | None
    status: Literal["pending", "succeeded", "failed"]
    attempts: int
    dispatch_no: int
    error_code: str | None
    response_status: int | None
    row_id: int | None
    outcome: str | None
    delivered_at: AwareDatetime | None


class RetryCaptureDeliveryRequest(ConfirmedInput):
    expected_dispatch: int = Field(ge=1, description="Epoch observado na entrega.")
    expected_config_version: int = Field(
        ge=1, description="Revisão atual do mesmo destino que a operadora autoriza explicitamente."
    )
