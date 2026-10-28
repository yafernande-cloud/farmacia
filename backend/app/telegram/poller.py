"""Long polling (getUpdates): permite usar el bot real SIN exponer el backend públicamente."""

import asyncio
import logging
from datetime import UTC, datetime

from app.services import sessions
from app.telegram.client import TelegramError
from app.telegram.handler import (
    TelegramRuntime,
    after_handle,
    deliver_outbound,
    handle_update,
    retry_failed_events,
)

log = logging.getLogger(__name__)
ALLOWED = ["message", "edited_message", "callback_query"]


async def poll_once(rt: TelegramRuntime, offset: int | None, timeout: int = 25) -> int | None:
    assert rt.client is not None
    payload = {"timeout": timeout, "allowed_updates": ALLOWED}
    if offset is not None:
        payload["offset"] = offset
    updates = await rt.client.call("getUpdates", payload)
    for u in updates:
        res = await handle_update(rt, u)
        await after_handle(rt, res)
        offset = u["update_id"] + 1  # confirma a Telegram; un reinicio no pierde nada (idempotente)
    return offset


async def sweep(rt: TelegramRuntime) -> None:
    """Mantenimiento: cerrar sesiones vencidas, reintentar eventos fallidos y entregar pendientes."""
    async with rt.sessionmaker() as s, s.begin():
        await sessions.expire_stale(s, datetime.now(UTC))
    await retry_failed_events(rt)
    await deliver_outbound(rt)


async def run_poller(rt: TelegramRuntime) -> None:  # pragma: no cover - bucle de larga duración
    assert rt.client is not None
    await rt.client.call(
        "deleteWebhook", {"drop_pending_updates": False}
    )  # getUpdates excluye al webhook
    offset, last_sweep = None, 0.0
    while True:
        try:
            offset = await poll_once(rt, offset)
            if asyncio.get_event_loop().time() - last_sweep > 30:
                await sweep(rt)
                last_sweep = asyncio.get_event_loop().time()
        except TelegramError as e:
            log.warning("error de Telegram (%s); reintento", e.code)
            await asyncio.sleep(min(30, e.retry_after or 5))
        except Exception:  # noqa: BLE001
            log.exception("error en el poller; reintento")
            await asyncio.sleep(5)
