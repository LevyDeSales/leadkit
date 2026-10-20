"""Mensagens JSON num socket local herdado; conteúdo privado nunca vai para argv ou logs."""

from __future__ import annotations

import json
import socket
import struct
from typing import Any

# Limita a alocação antes de aceitar um tamanho anunciado pelo outro processo.
MAX_FRAME_BYTES = 512 * 2**20


def encode(message: dict[str, Any]) -> bytes:
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("mensagem de renderização grande demais")
    return struct.pack("!I", len(payload)) + payload


def take(buffer: bytearray) -> dict[str, Any] | None:
    if len(buffer) < 4:
        return None
    size = struct.unpack_from("!I", buffer)[0]
    if size > MAX_FRAME_BYTES:
        raise ValueError("mensagem de renderização grande demais")
    if len(buffer) < size + 4:
        return None
    message = json.loads(buffer[4 : size + 4])
    del buffer[: size + 4]
    if not isinstance(message, dict):
        raise TypeError("mensagem de renderização inválida")
    return message


def receive(channel: socket.socket) -> dict[str, Any]:
    buffer = bytearray()
    while (message := take(buffer)) is None:
        chunk = channel.recv(65536)
        if not chunk:
            raise EOFError("canal de renderização encerrado")
        buffer.extend(chunk)
    return message
