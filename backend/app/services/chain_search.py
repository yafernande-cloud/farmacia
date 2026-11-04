"""E15 — búsqueda de precios públicos de cadenas (contrato v0.2 §11). Sin persistencia.

Tres reglas mandan aquí:

1. **Ninguna cadena está autorizada hoy.** Cruz Verde y Salcobrand no tienen conector. Ahumada
   tiene parser HTML y una fuente en vivo preparada, pero permanece apagada por defecto: solo se
   consulta cuando la operadora configura una fuente autorizada (`AHUMADA_LIVE_SEARCH_ENABLED` +
   `AHUMADA_LIVE_SEARCH_URL`). Sin esa declaración se informa `not_configured` con su motivo real
   y **no** se inventan fichas ni precios (D-115, contrato §11.7).
2. **El estado vive en la memoria del proceso** (TTL 10 min, 50 búsquedas vivas, un solo worker de
   API). Reiniciar el backend pierde las búsquedas en vuelo y el sondeo devuelve 404.
3. **No persiste nada** (§11.9): ni tabla, ni migración, ni caché en disco.

Un CAPTCHA o un bloqueo serán `failed`, nunca `no_matches`, cuando exista un conector real.
"""

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import get_settings
from app.services import ahumada

logger = logging.getLogger(__name__)

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
    """Estado real de una cadena para ESTA consulta.

    Ahumada usa el parser HTML solo si la operadora habilitó explícitamente la fuente en vivo.
    La fuente apunta a un acceso autorizado; sin esa configuración no se hace ninguna solicitud y
    se conserva `access_not_authorized` (D-115, contrato §11.6/§11.7).
    """
    if chain != "ahumada":
        return _source(chain)

    settings = get_settings()
    if "ahumada" not in settings.chain_search_enabled or not settings.ahumada_live_search_enabled:
        return _source(chain)
    if not settings.ahumada_live_search_url:
        return _source(chain, "live_source_not_configured")

    try:
        html_text = await ahumada.fetch_search_html(
            query,
            url=settings.ahumada_live_search_url,
            timeout_seconds=settings.ahumada_live_search_timeout_seconds,
        )
        results = ahumada.parse_search_results(html_text)
    except Exception:
        # Un bloqueo, un CAPTCHA o una fuente caída son `failed`, nunca «sin resultados».
        logger.warning("E15: la fuente de Ahumada no respondió", exc_info=True)
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
