"""Cliente único do Hatchet no processo.

Configurado só por ambiente (o SDK lê `HATCHET_CLIENT_TOKEN`, `HATCHET_CLIENT_TLS_STRATEGY` etc.).
Construir o cliente não abre conexão: apenas valida o formato do token.
"""

from __future__ import annotations

from hatchet_sdk import Hatchet

hatchet = Hatchet()
