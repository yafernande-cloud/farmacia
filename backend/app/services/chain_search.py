"""E15 — búsqueda de precios públicos de cadenas (contrato v0.2 §11). Sin persistencia.

Tres reglas mandan aquí:

1. **Cada cadena tiene su propio conector** (`app.services.chains`). Este coordinador solo pregunta
   por la cadena y traduce su respuesta a los estados del contrato. Un conector consulta su fuente
   únicamente cuando su interruptor y su configuración están presentes; sin eso se informa
   `not_configured` con su motivo real y **no** se inventan fichas ni precios (D-115, §11.6/§11.7).
   Las tres cadenas tienen conector propio; una cadena apagada conserva su motivo declarado.
2. **El estado vive en la memoria del proceso** (TTL 10 min, 50 búsquedas vivas, un solo worker de
   API). Reiniciar el backend pierde las búsquedas en vuelo y el sondeo devuelve 404.
3. **No persiste nada** (§11.9): ni tabla, ni migración, ni caché en disco.

Las cadenas se consultan en paralelo y cada una falla por separado: una fuente caída se informa
`failed` y nunca se convierte en «sin resultados» ni afecta a las otras cadenas.
"""

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.chains import CONNECTORS, ChainNotConfigured

logger = logging.getLogger(__name__)

CHAINS = ("cruz_verde", "salcobrand", "ahumada")
# Estado declarado de una cadena si su conector faltara del registro. No es un fixture: es el
# motivo con el que se informa una cadena que no puede consultarse sin autorización explícita.
INITIAL_STATE: dict[str, str] = {
    "cruz_verde": "access_not_authorized",
    "salcobrand": "access_not_authorized",
    "ahumada": "access_not_authorized",
}
TTL_SECONDS = 600
MAX_LIVE = 50
PURPOSES = ("search", "quote_verification")


def _source(chain: str, reason: str | None = None) -> dict[str, Any]:
    """Una cadena sin conector autorizado: `not_configured` con motivo, resultados vacíos."""
    return {
        "chain": chain,
        "state": "not_configured",
        "reason": reason or INITIAL_STATE[chain],
        "outcome": None,
        "observed_at": None,
        "results": [],
    }


async def _source_for(chain: str, query: str) -> dict[str, Any]:
    """Estado real de una cadena para ESTA consulta, preguntando a su propio conector.

    El coordinador no sabe de HTML, Algolia ni credenciales: solo recibe fichas o un motivo. Una
    cadena sin conector conserva su estado declarado y no hace ninguna solicitud de red.
    """
    connector = CONNECTORS.get(chain)
    if connector is None:
        return _source(chain)

    try:
        results = await connector(query)
    except ChainNotConfigured as exc:
        return _source(chain, exc.reason)
    except Exception:
        # Un bloqueo, un CAPTCHA, un tiempo de espera o una respuesta inválida son `failed`,
        # nunca «sin resultados», y solo afectan a esta cadena.
        logger.warning("E15: el conector de %s no respondió", chain, exc_info=True)
        return {
            "chain": chain,
            "state": "failed",
            "reason": "fetch_failed",
            "outcome": None,
            "observed_at": None,
            "results": [],
        }

    observed_at = datetime.now(UTC)
    return {
        "chain": chain,
        "state": "ok",
        "reason": None,
        "outcome": "matches" if results else "no_matches",
        "observed_at": observed_at,
        "results": results,
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
        # La consulta externa se hace fuera del lock: el lock solo protege el diccionario.
        sources = await asyncio.gather(*(_source_for(chain, query) for chain in requested))
        snapshot = {
            "search_id": str(uuid.uuid4()),
            "status": "completed",
            "query": query,
            "purpose": purpose,
            "created_at": now,
            "expires_at": now + timedelta(seconds=self.ttl_seconds),
            "sources": list(sources),
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
