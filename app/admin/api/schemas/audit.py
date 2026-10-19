"""Os modelos da auditoria: o que `list_audit_log` devolve.

Dados, não telas: o instante em ISO 8601 UTC, o autor e a ação como a auditoria os guarda e nenhum
rótulo. Os `details` de cada entrada passam por uma lista de **chaves** que a consulta
(`app.admin.queries.audit`) sabe próprias para a chave `leitura`, e o valor sob uma chave permitida sai
como foi escrito: a lista não olha o conteúdo. Os serviços não gravam o segredo, o hash de uma chave nem o
caminho da URL de um destino; o texto livre que a operadora digita ao reprocessar um pedido (`reason`) e o
texto de erro de fora do teste de uma integração (`error`) são gravados e ficam de fora.
"""

from __future__ import annotations

import uuid

from pydantic import AwareDatetime, BaseModel, Field

from app.admin.api.common import Page, utc
from app.admin.queries import audit as queries


class AuditEntry(BaseModel):
    """Uma entrada da auditoria: quem fez o quê, em que alvo e quando."""

    id: uuid.UUID = Field(
        description="O id da entrada. Entradas do mesmo instante se distinguem por ele, e o cursor da "
        "lista o usa.",
        examples=["0b0a8f3e-6f0b-4c2e-9d57-3f1d1c1f2a10"],
    )
    at: AwareDatetime = Field(description="Quando a ação foi feita (ISO 8601, UTC).")
    actor: str = Field(
        description="Quem fez: o usuário do painel (como `operadora`), `chave:<nome>` para uma chave "
        "de API, `retention` para a retenção automática ou `ambiente` para o destino padrão que veio "
        "do `WEBHOOK_URL`. O nome de uma chave não muda nem se reaproveita, então uma entrada antiga "
        "ainda diz quem foi.",
        examples=["chave:painel-de-vendas"],
    )
    action: str = Field(
        description="O que foi feito, no formato `recurso.ação`: `job.reprocess`, `form.publish`, "
        "`api_key.create`, `webhook_destination.update`, `retention.purge`...",
        examples=["job.reprocess"],
    )
    target_type: str | None = Field(
        description="O tipo do alvo (`job`, `delivery`, `integration_sync`, `form`, `asset`, "
        "`webhook_destination`, `integration` ou `api_key`). Nulo nas ações sem alvo, como a retenção.",
        examples=["job"],
    )
    target_id: str | None = Field(
        description="O id do alvo, como texto, no formato em que a auditoria o guarda: o id interno "
        "(um UUID) do pedido, do formulário, da imagem, da entrega, da sincronização, do destino ou da "
        "integração, ou o `key_id` público de uma chave. Nulo nas ações sem alvo. O id interno de um "
        "pedido ou de um formulário não é o código ou o endereço que o resto da API usa; o `slug` do "
        "formulário vem em `details.form_slug`.",
    )
    details: dict[str, queries.DetailValue] = Field(
        description="O que a ação guardou, só nas chaves que se sabe próprias para a chave `leitura` "
        "(a lista é de nomes de chave, e o valor sai como foi escrito): valores simples (texto, "
        "número, booleano ou nulo, uma lista deles ou um objeto de um nível deles), como `form_slug`, "
        "`version`, `host`, `attempt` ou, para uma chave de API, `name`, `role` e `expires_at`. Os "
        "serviços não gravam o segredo, o hash de uma chave nem o caminho da URL de um destino. "
        "**Ficam de fora**, mesmo gravados: o motivo digitado ao reprocessar um pedido (`reason`), "
        "que só a chave `total` lê, em `lead_data.notes` de `get_job`, e o texto de erro de fora do "
        "teste de uma integração (`error`), que é só do `total`. Uma chave que a auditoria guarda e a "
        "API ainda não conhece também não vem.",
        examples=[{"attempt": 2, "previous_status": "failed", "failure_code": "llm_unavailable"}],
    )

    @classmethod
    def of(cls, entry: queries.AuditEntry) -> AuditEntry:
        return cls(
            id=entry.id,
            at=utc(entry.at),
            actor=entry.actor,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            details=entry.details,
        )


class AuditLogPage(Page[AuditEntry]):
    """Uma página da auditoria, das entradas mais novas para as mais antigas."""
