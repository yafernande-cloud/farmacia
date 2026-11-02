"""E15 — búsqueda de precios públicos de cadenas (contrato v0.2 §11). Sin persistencia.

Tres reglas mandan aquí:

1. **Ninguna cadena está habilitada hoy.** No hay conector autorizado para Cruz Verde, Salcobrand ni
   Farmacias Ahumada; por eso se informan `not_configured` con su motivo real y **no** se inventan
   fichas ni precios (D-115). `CHAIN_SEARCH_ENABLED` existe como interruptor, pero sin conector no
   cambia el estado: habilitar una cadena exige construir y autorizar su acceso (§11.7).
2. **El estado vive en la memoria del proceso** (TTL 10 min, 50 búsquedas vivas, un solo worker de
   API). Reiniciar el backend pierde las búsquedas en vuelo y el sondeo devuelve 404.
3. **No persiste nada** (§11.9): ni tabla, ni migración, ni caché en disco.

Un CAPTCHA o un bloqueo serán `failed`, nunca `no_matches`, cuando exista un conector real.
"""

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

CHAINS = ("cruz_verde", "salcobrand", "ahumada")
# Estado real de cada cadena (§11.6). No es un fixture: es la situación declarada del conector.
INITIAL_STATE: dict[str, str] = {
    "cruz_verde": "requires_javascript",
    "salcobrand": "search_not_available",
    "ahumada": "access_not_authorized",
}
TTL_SECONDS = 600
MAX_LIVE = 50
PURPOSES = ("search", "quote_verification")


def _source(chain: str) -> dict[str, Any]:
    """Una cadena sin conector autorizado: `not_configured` con motivo, resultados vacíos."""
    return {
        "chain": chain,
        "state": "not_configured",
        "reason": INITIAL_STATE[chain],
        "outcome": None,
        "observed_at": None,
        "results": [],
    }


class ChainSearchStore:
    """Estado en memoria, acotado y con vencimiento. No sobrevive a un reinicio."""

    def __init__(self, *, ttl_seconds: int = TTL_SECONDS, max_live: int = MAX_LIVE):
        self.ttl_seconds = ttl_seconds
        self.max_live = max_live
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    def _purge(self, now: float) -> None:
        expired = [key for key, (deadline, _) in self._items.items() if deadline <= now]
        for key in expired:
            del self._items[key]

    async def start(
        self, query: str, chains: list[str], *, purpose: str = "search"
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        requested = [c for c in CHAINS if not chains or c in chains]
        snapshot = {
            "search_id": str(uuid.uuid4()),
            "status": "completed",
            "query": query,
            "purpose": purpose,
            "created_at": now,
            "expires_at": now + timedelta(seconds=self.ttl_seconds),
            "sources": [_source(c) for c in requested],
        }
        async with self._lock:
            mono = time.monotonic()
            self._purge(mono)
            if len(self._items) >= self.max_live:
                # Cota dura: se descarta la búsqueda más antigua, no se bloquea al usuario.
                oldest = min(self._items, key=lambda k: self._items[k][0])
                del self._items[oldest]
            self._items[snapshot["search_id"]] = (mono + self.ttl_seconds, snapshot)
        return snapshot

    async def get(self, search_id: str) -> dict[str, Any] | None:
        async with self._lock:
            mono = time.monotonic()
            self._purge(mono)
            item = self._items.get(search_id)
            return item[1] if item else None

    def reset(self) -> None:
        self._items.clear()


store = ChainSearchStore()
