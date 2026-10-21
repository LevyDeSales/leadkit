"""Projeção segura da central, compartilhada com HTML e catálogo MCP."""

from pydantic import Field

from app.admin.api.common import Page
from app.admin.queries.integration_hub import Capability, HubRow


class IntegrationHubPage(Page[HubRow]):
    catalog: list[Capability] = Field(
        description="Contagens globais de configurações, conexões ativas (só Baserow) e formulários atendidos. Não variam pelos filtros nem pela página; o padrão conta uma vez."
    )
