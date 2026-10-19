"""Tirar a chave de qualquer texto que sai do servidor: retorno de ferramenta, erro, log.

A chave só deveria estar no cabeçalho `Authorization` da requisição à API, e o resto do código não a
repete. Este módulo é o cinto sobre o suspensório: tudo o que vira texto para o cliente MCP ou para o
log passa por `Redactor`, que troca a chave (e o pedaço secreto dela) por `[chave]`, caso um erro de
biblioteca, uma resposta inesperada ou uma mensagem de exceção a traga.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

MASK = "[chave]"
# Uma chave é `gp_<id>_<segredo>`: o segredo sozinho também é a chave. Pedaços curtos demais trocariam
# texto comum (e não protegem nada), por isso o mínimo.
MIN_PIECE = 12


class Redactor:
    """Troca por `[chave]` cada ocorrência dos segredos dados. Sem segredo, devolve o texto como está."""

    def __init__(self, *secrets: str) -> None:
        pieces: set[str] = set()
        for secret in secrets:
            if not secret:
                continue
            pieces.add(secret)
            # `gp_<id>_<segredo>`: o segredo pode ter `_` (é base64 de URL), então o corte é no segundo.
            parts = secret.split("_", 2)
            if len(parts) == 3 and len(parts[2]) >= MIN_PIECE:
                pieces.add(parts[2])
        # A mesma chave escrita de outro jeito: dentro de um texto JSON (`json.dumps` escapa aspas, barras
        # e acentos) e numa URL (`%xx`). Para o alfabeto de uma chave são iguais ao original, mas uma
        # resposta que a eco assim, ou uma chave com um caractere que o JSON escapa, não escapa do filtro.
        for piece in list(pieces):
            pieces.add(json.dumps(piece, ensure_ascii=True)[1:-1])
            pieces.add(json.dumps(piece, ensure_ascii=False)[1:-1])
            pieces.add(quote(piece, safe=""))
        # Os maiores primeiro: a chave inteira sai antes do pedaço dela.
        self._pieces = sorted(pieces, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for piece in self._pieces:
            text = text.replace(piece, MASK)
        return text

    def tree(self, value: Any) -> Any:
        """O mesmo para um valor JSON (texto, lista, objeto): devolve uma cópia sem a chave."""
        if isinstance(value, str):
            return self(value)
        if isinstance(value, list):
            return [self.tree(item) for item in value]
        if isinstance(value, dict):
            return {self(str(key)): self.tree(item) for key, item in value.items()}
        return value


class RedactingFilter(logging.Filter):
    """Filtro de handler: o texto de cada registro (e o do traceback) sai sem a chave."""

    def __init__(self, redact: Redactor) -> None:
        super().__init__()
        self._redact = redact

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.getMessage())
        record.args = None
        if record.exc_info:
            # O traceback só vira texto no formatador, depois dos filtros: formata agora, limpo, e
            # solta o original.
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self._redact(text)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)
        return True
