"""Pipeline de entrada y salida de Telegram con idempotencia, reintentos y entrega diferida.

recibir → (chat autorizado?) → persistir evento (único por update_id) → procesar en UNA transacción
(sesión, dominio, auditoría, respuestas en la cola) → entregar la cola con reintentos.
La respuesta HTTP del webhook solo confirma recepción técnica; el reporte sigue guardado aunque el
envío de la respuesta falle (HLD §15).
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ChannelBinding, InboundEvent, OutboundMessage
from app.services import sessions
from app.services.channel import ChannelConfig, enqueue, handle_event
from app.services.photo_processing import PhotoRuntime, process_pending_attachments
from app.telegram.client import TelegramClient, TelegramError, render
from app.telegram.types import PROVIDER, chat_id_of, parse_update, redact

log = logging.getLogger(__name__)
MAX_EVENT_ATTEMPTS = 5
MAX_SEND_ATTEMPTS = 8
STUCK_AFTER = timedelta(seconds=60)


@dataclass
class TelegramRuntime:
    sessionmaker: async_sessionmaker[AsyncSession]
    client: TelegramClient | None
    cfg: ChannelConfig
    photos: PhotoRuntime | None = None


@dataclass
class HandleResult:
    status: str  # ignored | unauthorized | duplicate | processed | failed
    binding_id: str | None = None
    chat_id: str | None = None
    unauthorized_text: str | None = None
    error: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


async def handle_update(
    rt: TelegramRuntime, update_json: dict, now: datetime | None = None
) -> HandleResult:
    now = now or _now()
    ev = parse_update(update_json)
    chat = chat_id_of(update_json)
    if ev is None or chat is None:
        return HandleResult("ignored")
    # 1) ¿chat autorizado? Si no, se ignora (y se informa su ID para que un administrador lo autorice).
    async with rt.sessionmaker() as s:
        binding = await sessions.get_binding(s, PROVIDER, chat)
    if binding is None:
        return HandleResult("unauthorized", chat_id=chat,
                            unauthorized_text=f"Este chat no está autorizado para la farmacia. ID del chat: {chat}. "
                                              "Pide a un administrador que lo autorice.")  # fmt: skip
    # 2) persistir el evento una sola vez (AC12). Un duplicado no produce ningún efecto.
    payload = redact(update_json)
    async with rt.sessionmaker() as s, s.begin():
        row = (
            await s.execute(
                pg_insert(InboundEvent).values(
                    organization_id=binding.organization_id, provider=PROVIDER, channel_binding_id=binding.id,
                    external_event_id=ev.event_id, event_type=ev.kind, payload=payload,
                    payload_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                ).on_conflict_do_nothing(constraint="uq_inbound_event_external").returning(InboundEvent.id)
            )
        ).first()  # fmt: skip
    if row is None:
        return HandleResult("duplicate", str(binding.id), chat)
    return await _process(rt, row[0], binding.id, chat, now)


async def _process(
    rt: TelegramRuntime, inbound_id, binding_id, chat: str, now: datetime
) -> HandleResult:
    try:
        async with rt.sessionmaker() as s, s.begin():
            claimed = (
                await s.execute(
                    update(InboundEvent)
                    .where(
                        InboundEvent.id == inbound_id,
                        InboundEvent.status.in_(("received", "failed")),
                    )
                    .values(status="processing", attempts=InboundEvent.attempts + 1)
                    .returning(InboundEvent.id)
                )
            ).first()
            if claimed is None:  # otro proceso ya lo tomó o terminó
                return HandleResult("duplicate", str(binding_id), chat)
            inbound = await s.get(InboundEvent, inbound_id)
            binding = await s.get(ChannelBinding, binding_id)
            ev = parse_update(inbound.payload)
            assert ev is not None
            replies = await handle_event(s, binding, ev, inbound, now, rt.cfg)
            await enqueue(s, binding, inbound.id, replies)
            inbound.status, inbound.processed_at, inbound.error = "processed", now, None
        return HandleResult("processed", str(binding_id), chat)
    except Exception as e:  # noqa: BLE001
        log.exception("fallo al procesar el evento %s", inbound_id)
        async with rt.sessionmaker() as s, s.begin():
            await s.execute(
                update(InboundEvent).where(InboundEvent.id == inbound_id)
                .values(status="failed", attempts=InboundEvent.attempts + 1, error=f"{type(e).__name__}: {str(e)[:300]}")
            )  # fmt: skip
        return HandleResult("failed", str(binding_id), chat, error=type(e).__name__)


async def retry_failed_events(rt: TelegramRuntime, now: datetime | None = None) -> int:
    """Reprocesa eventos fallidos o atascados. Idempotente: el dominio usa claves por evento."""
    now = now or _now()
    async with rt.sessionmaker() as s:
        rows = (
            await s.execute(
                select(InboundEvent.id, InboundEvent.channel_binding_id, InboundEvent.payload)
                .where(InboundEvent.attempts < MAX_EVENT_ATTEMPTS,
                       (InboundEvent.status == "failed") | ((InboundEvent.status == "received") & (InboundEvent.received_at < now - STUCK_AFTER)))
                .order_by(InboundEvent.received_at).limit(50)
            )
        ).all()  # fmt: skip
    done = 0
    for eid, bid, payload in rows:
        r = await _process(rt, eid, bid, chat_id_of(payload) or "", now)
        done += r.status == "processed"
    return done


async def deliver_outbound(
    rt: TelegramRuntime, limit: int = 50, now: datetime | None = None
) -> int:
    """Entrega la cola de respuestas en orden. Reintenta con espera; los errores permanentes se marcan."""
    if rt.client is None:
        return 0
    now = now or _now()
    async with rt.sessionmaker() as s:
        ids = (
            await s.execute(select(OutboundMessage.id).where(OutboundMessage.status == "pending", OutboundMessage.next_attempt_at <= now)
                            .order_by(OutboundMessage.id).limit(limit))
        ).scalars().all()  # fmt: skip
    sent = 0
    for oid in ids:
        async with rt.sessionmaker() as s, s.begin():
            m = (await s.execute(select(OutboundMessage).where(OutboundMessage.id == oid, OutboundMessage.status == "pending")
                                 .with_for_update(skip_locked=True))).scalar_one_or_none()  # fmt: skip
            if m is None:
                continue
            binding = await s.get(ChannelBinding, m.channel_binding_id)
            method, body = render(m.method, m.payload, binding.external_chat_id)
            try:
                res = await rt.client.call(method, body)
                m.status, m.sent_at = "sent", now
                m.provider_message_id = (
                    str(res.get("message_id")) if isinstance(res, dict) else None
                )
                sent += 1
            except TelegramError as e:
                m.attempts += 1
                m.last_error = f"{e.code}:{e.description[:200]}"
                if e.permanent or m.attempts >= MAX_SEND_ATTEMPTS:
                    m.status = "failed"
                else:
                    wait = e.retry_after or min(300, 5 * 2**m.attempts)
                    m.next_attempt_at = now + timedelta(seconds=wait)
    return sent


async def after_handle(rt: TelegramRuntime, res: HandleResult) -> None:
    """Tras confirmar la transacción: entregar respuestas, procesar fotos y entregar sus respuestas."""
    if res.status == "unauthorized" and rt.client and res.chat_id and res.unauthorized_text:
        try:
            await rt.client.call(
                "sendMessage", {"chat_id": int(res.chat_id), "text": res.unauthorized_text}
            )
        except TelegramError:
            log.warning("no se pudo avisar a un chat no autorizado")
        return
    await deliver_outbound(rt)
    if rt.photos is not None and res.status in ("processed", "duplicate"):
        if await process_pending_attachments(rt.sessionmaker, rt.photos):
            await deliver_outbound(rt)
