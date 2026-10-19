"""O limite de uso da API de admin: um balde de fichas por chave, na memória do processo (ADR 0015,
"Limite de uso").

- `API_RATE_LIMIT_PER_MINUTE` (padrão 120) é quanto o balde enche por minuto; a rajada, a capacidade
  do balde, é de 30 chamadas (`BURST`). Um limite por minuto menor que 30 encolhe a rajada junto
  (`min(30, limite)`), para o balde não deixar passar de uma vez mais do que o limite de um minuto,
  mas a capacidade nunca é menor que `HEAVY_COST` (5), para a chamada pesada sempre caber.
- Uma chamada comum custa 1 ficha; a pesada (renderizar o PDF ou a prévia, ler a tabela do Baserow,
  baixar um PDF, enviar ou trocar uma imagem, que é decodificada e recodificada) custa `HEAVY_COST`
  (5).
- Sem ficha, a chamada é recusada com `429 rate_limited` e `Retry-After` (os segundos até haver
  fichas para ela). Uma recusa não gasta ficha.
- O balde é por processo e some ao reiniciar; a stack do Swarm tem uma réplica da API, e com mais o
  limite passaria a valer por réplica.
"""

from __future__ import annotations

import math
import threading

from app.admin.auth import Clock

BURST = 30
HEAVY_COST = 5
MAX_KEYS = 10_000  # teto de memória; as chaves são poucas, e o balde de uma chave nova nasce cheio


class RateLimiter:
    def __init__(self, per_minute: int, clock: Clock, *, burst: int = BURST) -> None:
        if per_minute < 1:
            raise ValueError("per_minute precisa ser pelo menos 1")
        self._clock = clock
        self._per_minute = per_minute
        self.capacity = max(HEAVY_COST, min(burst, per_minute))
        self._buckets: dict[str, tuple[float, float]] = {}  # chave -> (fichas, instante)
        self._lock = threading.Lock()

    def take(self, key: str, cost: int = 1) -> int:
        """Gasta `cost` fichas do balde de `key`. Devolve 0 se passou, ou os segundos (inteiros, no
        mínimo 1) até o balde ter as fichas da chamada."""
        cost = min(cost, self.capacity)
        with self._lock:
            now = self._clock()
            tokens, since = self._buckets.pop(key, (float(self.capacity), now))
            elapsed = max(0.0, now - since)
            tokens = min(float(self.capacity), tokens + elapsed * self._per_minute / 60)
            wait = 0
            if tokens >= cost:
                tokens -= cost
            else:
                wait = max(1, math.ceil((cost - tokens) * 60 / self._per_minute))
            self._buckets[key] = (tokens, now)  # reinserir mantém a ordem da chave mais recente
            while len(self._buckets) > MAX_KEYS:
                del self._buckets[next(iter(self._buckets))]
            return wait
