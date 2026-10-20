from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.errors import FailureCode, RetryableError


@dataclass(frozen=True)
class StoredObject:
    """Identidade opaca para detectar alterações; não é prova criptográfica dos bytes."""

    byte_size: int
    revision: str


class ArtifactStorage(Protocol):
    def head(self, key: str) -> StoredObject:
        """Consulta leve; ausência levanta FileNotFoundError, acesso negado não é ausência."""
        ...

    def put(self, key: str, content: bytes, content_type: str) -> None: ...

    def get(self, key: str) -> bytes: ...

    def presigned_url(self, key: str, *, ttl_seconds: int, filename: str) -> str | None:
        """URL direta e temporária do arquivo, ou None se o storage não oferece (local)."""
        ...

    def delete(self, key: str) -> None:
        """Apaga o arquivo; chave que não existe não é erro (a retenção pode repetir)."""
        ...


class LocalStorage:
    """Arquivos no disco. Só para desenvolvimento e testes (API e worker precisam ver a mesma pasta)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("chave fora do diretório de storage")
        return path

    def put(self, key: str, content: bytes, content_type: str) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        except OSError as exc:
            raise RetryableError(FailureCode.STORAGE_FAILED, type(exc).__name__) from exc

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def head(self, key: str) -> StoredObject:
        with self._path(key).open("rb") as stream:
            import os

            stat = os.fstat(stream.fileno())
        revision = f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"
        return StoredObject(stat.st_size, revision)

    def presigned_url(self, key: str, *, ttl_seconds: int, filename: str) -> str | None:
        return None

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            raise RetryableError(FailureCode.STORAGE_FAILED, type(exc).__name__) from exc
