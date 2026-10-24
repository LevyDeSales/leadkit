"""Declaração e evidência mínima do consentimento, sem certificar gesto humano."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError


class ConsentIn(BaseModel):
    """Payload legado, admitido somente pela versão que pede caixa."""

    model_config = ConfigDict(extra="forbid")
    privacy: Literal[True]
    text_version: str = Field(min_length=1, max_length=64)
    marketing_email: bool = False
    marketing_whatsapp: bool = False


class CheckboxConsentIn(ConsentIn):
    method: Literal["checkbox"]
    action: Literal["confirm"]


class SubmitConsentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["submit"]
    action: Literal["submit"]
    text_version: str = Field(min_length=1, max_length=64)
    marketing_email: bool = False
    marketing_whatsapp: bool = False


ConsentDeclaration = (
    Annotated[CheckboxConsentIn | SubmitConsentIn, Field(discriminator="method")] | ConsentIn
)
ConsentSource = Literal["public_html", "public_api_declaration"]


class Evidence(BaseModel):
    """Somente fatos discriminados válidos podem conceder autorização na leitura."""

    model_config = ConfigDict(extra="forbid", strict=True)
    evidence_schema: Literal[2]
    method: Literal["checkbox", "submit"]
    action: Literal["confirm", "submit"]
    source: ConsentSource
    privacy_at: AwareDatetime
    text_version: str = Field(min_length=1, max_length=64)
    marketing_email: bool
    marketing_whatsapp: bool

    @model_validator(mode="after")
    def matching_action(self):
        if self.action != {"checkbox": "confirm", "submit": "submit"}[self.method]:
            raise ValueError("A ação não corresponde ao método de consentimento.")
        return self


def facts(raw: dict[str, Any], *, mode: Literal["python", "json"] = "python") -> dict[str, Any]:
    """Projeta evidência nova estrita e os shapes legados conhecidos, sem criar método histórico."""
    found = {
        "privacy": False,
        "privacy_at": None,
        "text_version": raw.get("text_version"),
        "marketing_email": False,
        "marketing_whatsapp": False,
        "evidence_schema": None,
        "method": None,
        "action": None,
        "source": None,
    }
    if "evidence_schema" in raw:
        try:
            # O JSON persistido representa a data como string; strict preserva flags booleanas.
            import json

            evidence = Evidence.model_validate_json(json.dumps(raw))
        except (ValidationError, ValueError, TypeError):
            return found
        return {**evidence.model_dump(mode=mode), "privacy": True}
    accepted = raw.get("privacy_at")
    try:
        instant = datetime.fromisoformat(accepted) if isinstance(accepted, str) else None
    except ValueError:
        instant = None
    return {
        **found,
        "privacy_at": instant.isoformat() if instant is not None and mode == "json" else instant,
        "privacy": instant is not None or raw.get("processing") is True,
        "marketing_email": raw.get("marketing_email") is True,
        "marketing_whatsapp": raw.get("marketing_whatsapp") is True,
    }


def check_method(expected: str, consent: ConsentDeclaration) -> None:
    """O corpo declara a ação; quem decide o método é a versão do servidor."""
    if getattr(consent, "method", "checkbox") != expected:
        raise ValidationError.from_exception_data(
            "ConsentDeclaration",
            [
                {
                    "type": PydanticCustomError(
                        "consent_method",
                        "O método de consentimento difere desta versão do formulário.",
                    ),
                    "loc": ("consent", "method"),
                    "input": None,
                }
            ],
            hide_input=True,
        )


def record(consent: ConsentDeclaration, now: datetime, source: ConsentSource) -> dict[str, Any]:
    """Horário de aceite no servidor; origem é atribuída pelo adaptador."""
    result = {
        "privacy_at": now.astimezone(UTC).isoformat(),
        "text_version": consent.text_version,
        "marketing_email": consent.marketing_email,
        "marketing_whatsapp": consent.marketing_whatsapp,
    }
    result.update(
        evidence_schema=2,
        method=getattr(consent, "method", "checkbox"),
        action=getattr(consent, "action", "confirm"),
        source=source,
    )
    return result


def same_declaration(raw: dict[str, Any], consent: ConsentDeclaration) -> bool:
    """O replay novo compara intenção; origem e horário continuam sendo os primeiros."""
    if raw.get("evidence_schema") != 2:
        return True
    given = record(consent, datetime.now(UTC), "public_api_declaration")
    return all(
        raw.get(key) == given.get(key)
        for key in ("method", "action", "text_version", "marketing_email", "marketing_whatsapp")
    )
