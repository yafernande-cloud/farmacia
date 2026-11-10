"""Caché y almacén en memoria de búsquedas en vivo del motor de proveedores.

No persiste ofertas: ni tabla, ni disco, ni cola. Reiniciar el backend pierde las búsquedas. El
límite de entradas evita crecer sin control y TTL corto evita presentar un precio viejo como actual.
"""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

TTL_SECONDS = 60
MAX_LIVE = 50


class SupplierSearchStore:
    def __init__(self, *, ttl_seconds: int = TTL_SECONDS, max_live: int = MAX_LIVE):
        self.ttl_seconds = ttl_seconds
        self.max_live = max_live
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}
        self._by_key: dict[str, tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    def _purge(self, now: float) -> None:
        expired = [key for key, (deadline, _) in self._items.items() if deadline <= now]
        for key in expired:
            self._items.pop(key, None)
        stale_keys = [key for key, (deadline, _) in self._by_key.items() if deadline <= now]
        for key in stale_keys:
            self._by_key.pop(key, None)

    async def put(self, snapshot: dict[str, Any], *, cache_key: str | None = None) -> None:
        search_id = str(snapshot["search_id"])
        async with self._lock:
            mono = time.monotonic()
            self._purge(mono)
            if len(self._items) >= self.max_live and search_id not in self._items:
                oldest = min(self._items, key=lambda k: self._items[k][0])
                self._items.pop(oldest, None)
                for key, (_, cached_id) in list(self._by_key.items()):
                    if cached_id == oldest:
                        self._by_key.pop(key, None)
            deadline = mono + self.ttl_seconds
            self._items[search_id] = (deadline, snapshot)
            if cache_key is not None:
                self._by_key[cache_key] = (deadline, search_id)

    async def get(self, search_id: str) -> dict[str, Any] | None:
        async with self._lock:
            self._purge(time.monotonic())
            item = self._items.get(search_id)
            return item[1] if item else None

    async def cached(self, cache_key: str) -> dict[str, Any] | None:
        async with self._lock:
            mono = time.monotonic()
            self._purge(mono)
            entry = self._by_key.get(cache_key)
            if entry is None:
                return None
            item = self._items.get(entry[1])
            if item is None:
                self._by_key.pop(cache_key, None)
                return None
            snapshot = copy.deepcopy(item[1])
            snapshot["cached"] = True
            return snapshot

    async def invalidate(self, cache_key: str) -> None:
        async with self._lock:
            self._by_key.pop(cache_key, None)

    def reset(self) -> None:
        self._items.clear()
        self._by_key.clear()


store = SupplierSearchStore()
