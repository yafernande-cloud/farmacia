"""Pipeline de entrada y salida de Telegram con idempotencia, reintentos y entrega diferida.

recibir → (chat no autorizado: se atiende SIN persistir el mensaje) → (cuenta y chat autorizados,
validado en el servidor) → persistir evento (único por update_id) → procesar en UNA transacción (se
vuelve a validar el acceso, sesión, dominio, auditoría, respuestas en la cola) → entregar la cola con
reintentos. La respuesta HTTP del webhook solo confirma recepción técnica; el reporte sigue guardado
aunque el envío de la respuesta falle (HLD §15).

Acceso:
  * Un chat desconocido NO tiene ninguna función operativa. `/solicitar` registra una solicitud y
    devuelve un código temporal por respuesta directa (nunca se guarda en evento/outbox/auditoría).
  * La cuenta solo queda habilitada cuando un administrador aprueba la solicitud en la API.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import DomainError
from app.core.invitations import looks_like_code
from app.models import ChannelBinding, InboundEvent, OutboundMessage
from app.services import access as access_svc
from app.services.access import AccountContext
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
    status: str  # ignored | unauthorized | replied | duplicate | processed | failed
    binding_id: str | None = None
    chat_id: str | None = None
    unauthorized_text: str | None = None
    reply_text: str | None = None
    error: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _command(ev) -> str:
    parts = (ev.text or "").strip().split()
    return parts[0].split("@")[0].lower() if parts else ""


def _unidentified_text(chat: str) -> str:
    return (
        f"Este chat no está autorizado para la farmacia. ID del chat: {chat}.\n"
        "Si necesitas acceso, escribe /solicitar. La persona encargada debe aprobar la cuenta "
        "desde el sistema; este chat no puede reportar faltantes por ahora."
    )


async def _request_access(
    rt: TelegramRuntime, ev, chat: str, now: datetime
) -> HandleResult:
    """Crea/reutiliza la solicitud y devuelve el código por respuesta DIRECTA (no se persiste)."""
    if ev.chat_kind != "private":
        return HandleResult(
            "replied",
            chat_id=chat,
            reply_text="El acceso se solicita solo en un chat privado con el bot.",
        )
    if ev.kind not in ("text", "start") or _command(ev) not in ("/solicitar", "/start"):
        return HandleResult("replied", chat_id=chat, reply_text=_unidentified_text(chat))
    if _command(ev) == "/start":
        return HandleResult(
            "replied",
            chat_id=chat,
            reply_text=(
                "Hola. Este bot es de Faltantes Farmacia.\n"
                "Para pedir acceso escribe /solicitar. Recibirás un código temporal: compártelo "
                "con la persona administradora, que debe aprobar la cuenta desde el sistema.\n"
                f"ID del chat: {chat}"
            ),
        )
    if rt.cfg.access_org_id is None:
        return HandleResult(
            "replied",
            chat_id=chat,
            reply_text=(
                "Las solicitudes de acceso no están configuradas en este entorno. "
                "Avisa a la persona administradora."
            ),
        )
    try:
        async with rt.sessionmaker() as s, s.begin():
            issued = await access_svc.request_access(
                s,
                organization_id=rt.cfg.access_org_id,
                provider=PROVIDER,
                external_user_id=ev.sender_id,
                external_chat_id=chat,
                chat_kind=ev.chat_kind,
                update_id=ev.event_id,
                now=now,
            )
    except DomainError as e:
        return HandleResult("replied", chat_id=chat, reply_text=e.message)
    except Exception as e:  # noqa: BLE001
        log.error("fallo al registrar una solicitud de acceso (%s)", type(e).__name__)
        return HandleResult(
            "replied",
            chat_id=chat,
            reply_text="No pude registrar la solicitud. Inténtalo más tarde.",
        )

    minutes = int(access_svc.CODE_TTL.total_seconds() // 60)
    if issued.code is None:
        return HandleResult(
            "replied",
            chat_id=chat,
            reply_text=(
                "Ya hay una solicitud pendiente para esta cuenta. El código que recibiste sigue "
                f"vigente por hasta {minutes} minutos. Compártelo con la persona administradora.\n"
                "Si no lo tienes, espera a que expire o pide que revisen la solicitud."
            ),
        )
    return HandleResult(
        "replied",
        chat_id=chat,
        reply_text=(
            "Solicitud registrada. Este es tu código temporal:\n\n"
            f"{issued.code}\n\n"
            f"Vence en {minutes} minutos y sirve una sola vez. Compártelo SOLO con la persona "
            "administradora del sistema. Mientras no se apruebe, este chat no puede reportar "
            "faltantes."
        ),
    )


async def _on_unidentified(
    rt: TelegramRuntime, ev, chat: str, now: datetime, err: DomainError
) -> HandleResult:
    """Chat o cuenta no autorizados: no se persiste nada ni se habilita ninguna función."""
    text = (ev.text or "").strip()
    if ev.kind == "text" and looks_like_code(text):
        return HandleResult(
            "replied",
            chat_id=chat,
            reply_text=(
                "Ese formato parece un código de acceso. No lo envíes al bot: compártelo con la "
                "persona administradora, que lo ingresará en el sistema."
            ),
        )
    if ev.kind in ("text", "start") and _command(ev) in ("/solicitar", "/start"):
        return await _request_access(rt, ev, chat, now)
    # Cualquier otro mensaje de un chat no autorizado se descarta: no hay evento ni mensaje guardado.
    return HandleResult(
        "unauthorized",
        chat_id=chat,
        unauthorized_text=f"{err.message}\n\nPara pedir acceso, escribe /solicitar.",
    )


def _context(access: access_svc.Access) -> AccountContext | None:
    """La cuenta solo fija identidad y teclado cuando el vínculo es SUYO (personal o compartido)."""
    a = access.account
    if a is None or access.binding.account_id != a.id:
        return None
    return AccountContext(a.id, a.mode, a.app_user_id, a.access_role, a.display_label)


async def handle_update(
    rt: TelegramRuntime, update_json: dict, now: datetime | None = None
) -> HandleResult:
    now = now or _now()
    ev = parse_update(update_json)
    chat = chat_id_of(update_json)
    if ev is None or chat is None:
        return HandleResult("ignored")
    # Un código escrito en el chat jamás se persiste, ni siquiera como mensaje pendiente.
    if ev.kind == "text" and looks_like_code(ev.text):
        return HandleResult(
            "replied",
            chat_id=chat,
            reply_text=(
                "Ese formato parece un código de acceso. No lo envíes al bot: compártelo con la "
                "persona administradora, que lo ingresará en el sistema."
            ),
        )
    # 1) ¿Chat y cuenta autorizados? Se valida en el servidor: ocultar botones no autoriza a nadie.
    async with rt.sessionmaker() as s:
        try:
            access = await access_svc.resolve_access(s, PROVIDER, chat, ev.sender_id)
        except DomainError as e:
            return await _on_unidentified(rt, ev, chat, now, e)
        binding_id, org_id = access.binding.id, access.binding.organization_id
    # 2) persistir el evento una sola vez (AC12). Un duplicado no produce ningún efecto.
    payload = redact(update_json)
    async with rt.sessionmaker() as s, s.begin():
        row = (
            await s.execute(
                pg_insert(InboundEvent).values(
                    organization_id=org_id, provider=PROVIDER, channel_binding_id=binding_id,
                    external_event_id=ev.event_id, event_type=ev.kind, payload=payload,
                    payload_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                ).on_conflict_do_nothing(constraint="uq_inbound_event_external").returning(InboundEvent.id)
            )
        ).first()  # fmt: skip
    if row is None:
        return HandleResult("duplicate", str(binding_id), chat)
    return await _process(rt, row[0], binding_id, chat, now)


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
            ev = parse_update(inbound.payload)
            if ev is None:
                inbound.status, inbound.processed_at, inbound.error = "ignored", now, "EVENT_INVALID"
                return HandleResult("ignored", str(binding_id), chat)
            # Se vuelve a validar el acceso DENTRO de la transacción: pudo revocarse entre el paso 1 y ahora.
            try:
                access = await access_svc.resolve_access(s, PROVIDER, ev.chat_id, ev.sender_id)
            except DomainError as e:
                inbound.status, inbound.processed_at, inbound.error = "ignored", now, e.code
                return HandleResult(
                    "unauthorized", str(binding_id), chat, unauthorized_text=e.message
                )
            if access.binding.id != binding_id:
                inbound.status, inbound.processed_at, inbound.error = (
                    "ignored",
                    now,
                    "BINDING_CHANGED",
                )
                return HandleResult(
                    "unauthorized",
                    str(binding_id),
                    chat,
                    unauthorized_text="La autorización de este chat cambió. Escribe de nuevo para continuar.",
                )
            binding = access.binding
            replies = await handle_event(
                s, binding, ev, inbound, now, rt.cfg, account=_context(access)
            )
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
    if res.status == "replied" and rt.client and res.chat_id and res.reply_text:
        # Respuesta directa (solicitud de acceso): no entra a la cola ni se guarda el mensaje recibido.
        # El código temporal solo existe en esta llamada.
        try:
            await rt.client.call(
                "sendMessage", {"chat_id": int(res.chat_id), "text": res.reply_text}
            )
        except TelegramError:
            log.warning("no se pudo responder a una solicitud de acceso")
        return
    await deliver_outbound(rt)
    if rt.photos is not None and res.status in ("processed", "duplicate"):
        if await process_pending_attachments(rt.sessionmaker, rt.photos):
            await deliver_outbound(rt)
