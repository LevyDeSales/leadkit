"""Os modelos das chaves de API: o que as operações `get_current_api_key`, `list_api_keys`,
`create_api_key` e `revoke_api_key` recebem e devolvem.

Nenhum traz o hash do segredo (o serviço nem o expõe). A chave inteira (`CreatedApiKey.key`) existe só
na resposta de `create_api_key`, e o exemplo dela não tem a forma de uma chave de verdade.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field

from app.admin.api import keys
from app.admin.api.common import ConfirmedInput, Page

Role = Literal["leitura", "total"]  # os papéis de `app.models.API_KEY_ROLES`


def _utc(moment: datetime | None) -> datetime | None:
    return moment.astimezone(UTC) if moment is not None else None


class ApiKey(BaseModel):
    """Uma chave de API como a API a mostra. Nunca traz o segredo."""

    key_id: str = Field(
        description="O id público da chave (8 caracteres hexadecimais). Serve para achá-la e para "
        "revogá-la; não autentica ninguém.",
        examples=["a1b2c3d4"],
    )
    name: str = Field(
        description="O nome da chave. Não muda e não se reaproveita; a auditoria cita o autor de "
        "uma escrita como `chave:<nome>`.",
        examples=["painel-de-vendas"],
    )
    role: Role = Field(
        description="`leitura` só consulta; `total` faz tudo o que o admin logado faz, inclusive "
        "criar outras chaves.",
    )
    state: keys.State = Field(
        description="`active` vale; `expired` passou da validade; `revoked` foi revogada (a "
        "revogação vale mais que o vencimento)."
    )
    created_by: str = Field(
        description="Quem criou: o usuário do admin, ou `chave:<nome>` se foi outra chave.",
        examples=["operadora"],
    )
    created_at: AwareDatetime = Field(description="Quando foi criada (ISO 8601, UTC).")
    expires_at: AwareDatetime | None = Field(
        default=None, description="Quando vence (ISO 8601, UTC). Nulo: sem validade."
    )
    revoked_at: AwareDatetime | None = Field(
        default=None, description="Quando foi revogada. Nulo: não foi."
    )
    revoked_by: str | None = Field(default=None, description="Quem a revogou.")
    last_used_at: AwareDatetime | None = Field(
        default=None,
        description="A última chamada que a chave fez (gravada no máximo uma vez por minuto). "
        "Nulo: nunca foi usada.",
    )

    @classmethod
    def of(cls, info: keys.KeyInfo, now: datetime) -> ApiKey:
        return cls(
            key_id=info.key_id,
            name=info.name,
            role=info.role,  # type: ignore[arg-type]
            state=info.state(now),
            created_by=info.created_by,
            created_at=_utc(info.created_at),  # type: ignore[arg-type]
            expires_at=_utc(info.expires_at),
            revoked_at=_utc(info.revoked_at),
            revoked_by=info.revoked_by,
            last_used_at=_utc(info.last_used_at),
        )


class CreatedApiKey(ApiKey):
    """A chave recém-criada: tudo de `ApiKey` e a chave inteira, que **não aparece de novo**."""

    key: str = Field(
        description="A chave inteira, para mandar em `Authorization: Bearer`. Só existe nesta "
        "resposta: guarde-a num cofre; se perder, revogue a chave e crie outra.",
        examples=["gp_<id>_<segredo>"],
    )

    @classmethod
    def from_created(cls, created: keys.CreatedKey, now: datetime) -> CreatedApiKey:
        return cls(**ApiKey.of(created.info, now).model_dump(), key=created.key)


class ApiKeyPage(Page[ApiKey]):
    """Uma página de chaves, da mais nova para a mais antiga, as revogadas também."""


class CreateApiKeyRequest(ConfirmedInput):
    """O pedido de uma chave nova. Como a criação é uma ação marcada, exige `confirm: true`."""

    name: str = Field(
        description=f"O nome da chave, {keys.NAME_HINT}. Não muda e não se reaproveita, nem o de "
        "uma chave revogada.",
        examples=["painel-de-vendas"],
    )
    role: Role = Field(
        description="`leitura` só consulta; `total` faz tudo o que o admin logado faz.",
        examples=["leitura"],
    )
    expires_at: AwareDatetime | None = Field(
        default=None,
        description="Quando a chave vence, em ISO 8601 com fuso. Ausente ou nulo: sem validade. "
        "A página do admin sugere 90 dias; aqui quem chama escolhe.",
        examples=["2027-04-15T12:00:00Z"],
    )


class RevokeApiKeyRequest(ConfirmedInput):
    """A revogação de uma chave. Exige `confirm: true`."""
