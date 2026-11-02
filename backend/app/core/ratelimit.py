"""Limitador de tasa en proceso (sin dependencias nuevas).

Suficiente para el endpoint público de solicitudes de acceso: ventana deslizante por clave. No se
persiste y no sustituye a un limitador de infraestructura; con varios dynos cada uno limita por su
cuenta. Es una barrera contra reintentos y mensajes en bucle, no una cuota de negocio.
"""

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    hits: deque[float] = field(default_factory=deque)


class SlidingWindow:
    def __init__(self, limit: int, window_seconds: float, *, max_keys: int = 10_000):
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        """Consume un intento de `key`; devuelve False si superó el límite de la ventana."""
        now = time.monotonic()
        async with self._lock:
            if len(self._buckets) > self.max_keys:
                self._buckets.clear()  # cota de memoria: nunca crece sin límite
            bucket = self._buckets.setdefault(key, _Bucket())
            while bucket.hits and now - bucket.hits[0] >= self.window:
                bucket.hits.popleft()
            if len(bucket.hits) >= self.limit:
                return False
            bucket.hits.append(now)
            return True

    def reset(self) -> None:
        self._buckets.clear()
