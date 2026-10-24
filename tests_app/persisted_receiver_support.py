"""Receptor sintético de arquivo: recibo e efeito são fatos da mesma transação.

Suporte exclusivo de testes; não implementa um serviço nem confirma efeitos externos.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx


class DurableReceiver:
    """Callable HTTP local; ``mode`` permanece ativo até ser alterado ou recriado.

    ``normal`` confirma após commit; ``timeout_before_commit`` desfaz a transação;
    ``timeout_after_commit`` perde a resposta de um recibo já confirmado.
    """

    def __init__(self, database_path: Path, secret: str) -> None:
        self.database_path = database_path
        self.secret = secret
        self.mode: Literal["normal", "timeout_before_commit", "timeout_after_commit"] = "normal"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id TEXT PRIMARY KEY,
                    issuer TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    submission_id TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    UNIQUE (issuer, event_id)
                );
                CREATE TABLE IF NOT EXISTS effects (
                    effect_id TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL UNIQUE REFERENCES receipts(receipt_id),
                    issuer TEXT NOT NULL,
                    submission_id TEXT NOT NULL
                );
                """
            )

    def receipts(self) -> list[dict[str, Any]]:
        """Consulta recibos e efeitos já confirmados por uma conexão nova."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT receipts.*, effects.effect_id FROM receipts
                       JOIN effects USING (receipt_id) ORDER BY receipts.rowid"""
                )
            ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        timestamp = request.headers.get("X-MCC-Timestamp", "")
        try:
            fresh = abs(int(time.time()) - int(timestamp)) <= 300
        except ValueError:
            fresh = False
        signature = (
            "v1="
            + hmac.new(
                self.secret.encode(), timestamp.encode() + b"." + request.content, hashlib.sha256
            ).hexdigest()
        )
        if not fresh or not hmac.compare_digest(
            signature, request.headers.get("X-MCC-Signature", "")
        ):
            return httpx.Response(401)
        try:
            payload = json.loads(request.content)
            if not isinstance(payload, dict):
                raise TypeError
            issuer = payload["issuer"]
            event_id = payload["event_id"]
            submission_id = payload["submission"]["id"]
            if (
                type(payload["schema_version"]) is not int
                or payload["schema_version"] != 2
                or payload["event"] != "submissao.persistida"
                or request.headers.get("X-MCC-Event") != payload["event"]
                or request.headers.get("Idempotency-Key") != event_id
                or not isinstance(issuer, str)
                or not issuer.startswith("leadkit:")
                or str(UUID(issuer.removeprefix("leadkit:"))) != issuer.removeprefix("leadkit:")
                or str(UUID(event_id)) != event_id
                or str(UUID(submission_id)) != submission_id
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            return httpx.Response(400)
        body_sha256 = hashlib.sha256(request.content).hexdigest()
        try:
            with closing(sqlite3.connect(self.database_path, timeout=1)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    original = connection.execute(
                        "SELECT receipt_id, body_sha256 FROM receipts "
                        "WHERE issuer = ? AND event_id = ?",
                        (issuer, event_id),
                    ).fetchone()
                    if original:
                        receipt_id, original_hash = original
                        if original_hash != body_sha256:
                            return httpx.Response(409)
                    else:
                        receipt_id = str(uuid4())
                        connection.execute(
                            "INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                            (receipt_id, issuer, event_id, submission_id, body_sha256),
                        )
                        connection.execute(
                            "INSERT INTO effects VALUES (?, ?, ?, ?)",
                            (str(uuid4()), receipt_id, issuer, submission_id),
                        )
                    if self.mode == "timeout_before_commit":
                        raise httpx.ReadTimeout(
                            "Timeout sintético antes do commit", request=request
                        )
        except sqlite3.Error:
            return httpx.Response(503)
        if self.mode == "timeout_after_commit":
            raise httpx.ReadTimeout("Timeout sintético depois do commit", request=request)
        return httpx.Response(200, json={"receipt_id": receipt_id})
