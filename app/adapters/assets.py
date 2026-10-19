"""As imagens dos formulários no storage (`app.adapters.storage`), pela chave do conteúdo.

Cada imagem é guardada em `assets/<sha256>.<extensão>` (`AssetRef.key`): a chave diz o que o arquivo
tem dentro, então ele nunca muda, o mesmo conteúdo nunca se repete e um PDF antigo acha a imagem
que usou. Quem lê confere o sha256 do que veio, e um arquivo que não bate com a chave é recusado.

O cache fica no processo (a API serve a rota das imagens, e o worker renderiza o PDF) e é seguro por
ser pelo conteúdo: o mesmo sha256 são sempre os mesmos bytes, em qualquer storage. Tem um teto de
memória; a imagem que sai dele é lida de novo no pedido seguinte.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

from app.adapters.storage import ArtifactStorage
from app.errors import RetryableError, TerminalError
from app.forms.definition.images import AssetRef

CACHE_BYTES = 32 * 1024 * 1024


class AssetUnavailable(Exception):
    """A imagem não pôde ser lida do storage, ou o que veio não é o que a chave diz. A mensagem
    não traz nada além do que aconteceu."""


class _Cache:
    """Os bytes das imagens lidas, por sha256, dentro de um teto de memória: sai a menos usada.
    Seguro entre threads."""

    def __init__(self, max_bytes: int = CACHE_BYTES) -> None:
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, sha256: str) -> bytes | None:
        with self._lock:
            content = self._items.get(sha256)
            if content is not None:
                self._items.move_to_end(sha256)
            return content

    def add(self, sha256: str, content: bytes) -> None:
        if len(content) > self.max_bytes:
            return
        with self._lock:
            if sha256 in self._items:
                return
            self._items[sha256] = content
            self._size += len(content)
            while self._size > self.max_bytes:
                _, dropped = self._items.popitem(last=False)
                self._size -= len(dropped)

    def drop(self, sha256: str) -> None:
        with self._lock:
            dropped = self._items.pop(sha256, None)
            if dropped is not None:
                self._size -= len(dropped)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0


SHARED_CACHE = _Cache()


class AssetStore:
    """Guarda e lê as imagens dos formulários num `ArtifactStorage`. Quem constrói é barato: o
    cache é o do processo (`SHARED_CACHE`), a menos que se passe outro."""

    def __init__(self, storage: ArtifactStorage, cache: _Cache | None = None) -> None:
        self._storage = storage
        self._cache = cache if cache is not None else SHARED_CACHE

    def put(self, ref: AssetRef, content: bytes) -> None:
        """Guarda os bytes sob a chave do conteúdo. Repetir é inofensivo: a chave é a mesma e o
        conteúdo também. Levanta `ValueError` se os bytes não são os da referência."""
        if hashlib.sha256(content).hexdigest() != ref.sha256:
            raise ValueError("os bytes não são os da referência")
        self._storage.put(ref.key, content, ref.content_type)
        self._cache.add(ref.sha256, content)

    def read(self, ref: AssetRef) -> bytes:
        """Os bytes da imagem. Levanta `AssetUnavailable` se o storage não a tem ou se o arquivo
        não bate com o sha256 da chave, e deixa passar a falha do provedor que o adaptador do
        storage já traduziu (`RetryableError`, `TerminalError`), com a mensagem segura dele."""
        cached = self._cache.get(ref.sha256)
        if cached is not None:
            return cached
        try:
            content = self._storage.get(ref.key)
        except (RetryableError, TerminalError):
            raise
        except Exception:  # noqa: BLE001 - o armazenamento local levanta o erro do sistema
            raise AssetUnavailable("a imagem não está no storage") from None
        if hashlib.sha256(content).hexdigest() != ref.sha256:
            raise AssetUnavailable("o arquivo no storage não é o da imagem")
        self._cache.add(ref.sha256, content)
        return content

    def discard(self, ref: AssetRef) -> None:
        """Apaga o arquivo da imagem. Quem chama garante que nenhuma imagem nem versão a usa."""
        self._cache.drop(ref.sha256)
        self._storage.delete(ref.key)
