"""O texto do `openapi.json` da API de admin (ADR 0015, "O arquivo `openapi.json`").

Um formato só, para o arquivo versionado (`docs/api/openapi.json`, que `app.admin.api.openapi`
gera e confere) e para o que a rota `GET /admin/api/v1/openapi.json` serve (`app.admin.api.
reference`): chaves ordenadas, recuo de 2, acentos como estão e uma linha final. Assim o que a
referência mostra é, byte a byte, o arquivo do repositório, sem que a imagem do app (que não leva
`docs/`) precise dele.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def render(document: Mapping[str, Any]) -> str:
    """O texto do arquivo: chaves ordenadas, recuo de 2, acentos como estão e uma linha final."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
