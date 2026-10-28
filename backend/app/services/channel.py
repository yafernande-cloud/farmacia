"""Flujo de canal independiente del proveedor: evento normalizado → sesión → interpretación →
observación → respuestas. Telegram es solo un adaptador (app/telegram).

Reglas (HLD §3.1, §7, §8):
* Un mensaje sin identidad vigente queda `pending_attribution`: NUNCA se atribuye solo (ni al
  auxiliar anterior ni al siguiente). Tras identificarse, la persona decide registrarlo a su nombre o
  descartarlo; los mensajes viejos no se pueden atribuir.
* El bot no aplica cantidades ni aprueba nada: solo registra avisos (observaciones) y consulta.
* Una respuesta nunca asegura que un producto ambiguo fue identificado.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.interpretation.ai import AIInterpreter
from app.interpretation.segments import Intent, classify_intent, split_segments
from app.models import (
    ActorSession,
    AppUser,
    Attachment,
    ChannelBinding,
    ChannelMessage,
    InboundEvent,
    OutboundMessage,
)
from app.services import queries, sessions
from app.services.actors import Actor, load_actor
from app.services.demand import ObservationResult, Origin, record_observation
from app.services.events import advisory_lock, emit
from app.services.provenance import org_kind, origin_for

KEYBOARD = [["Ver lista", "Cambiar auxiliar"], ["Finalizar reportes", "¿Quién soy?"]]
CMD_LIST = {"/lista", "ver lista", "lista"}
CMD_CHANGE = {"/cambiar", "cambiar auxiliar", "cambiar"}
CMD_WHO = {"/quien", "¿quién soy?", "quien soy", "¿quien soy?", "/yo"}
CMD_HELP = {"/ayuda", "/help", "ayuda"}
CMD_LOGOUT = {"/salir", "/finalizar", "finalizar reportes", "cerrar sesion", "cerrar sesión"}


@dataclass(frozen=True)
class Button:
    text: str
    data: str


@dataclass
class Reply:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    keyboard: list[list[str]] | None = None
    answer_callback_id: str | None = None


@dataclass(frozen=True)
class PhotoRef:
    file_id: str
    file_unique_id: str | None = None
    width: int | None = None
    height: int | None = None
    file_size: int | None = None


@dataclass(frozen=True)
class ChannelEvent:
    provider: str
    event_id: str
    chat_id: str
    kind: Literal["text", "photo", "callback", "edited", "start", "other"]
    text: str | None = None
    message_id: str | None = None
    sent_at: datetime | None = None
    photo: PhotoRef | None = None
    callback_data: str | None = None
    callback_id: str | None = None


@dataclass(frozen=True)
class ChannelConfig:
    idle: timedelta = timedelta(minutes=10)
    pending_max_age: timedelta = timedelta(minutes=15)
    page_size: int = 8
    vision_enabled: bool = False  # hay un proveedor de visión (real o simulado) conectado
    ai: AIInterpreter | None = None


# ------------------------------------------------------------------ textos


def _footer(name: str) -> str:
    return f"— Reportando como {name}"


def _selector(auxs: list[AppUser], text: str) -> Reply:
    """Botones con las personas activas y disponibles (datos de PostgreSQL). Si no hay ninguna, no hay botones."""
    if not auxs:
        return Reply(
            "No hay personas disponibles para reportar en este momento. Pide a compras que revise el personal.",
            keyboard=KEYBOARD,
        )
    rows, row = [], []
    for u in auxs:
        row.append(Button(u.display_name, f"sel:{u.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return Reply(text, rows, KEYBOARD)


_PENDING = {
    "active_ingredient": "el principio activo",
    "strength": "la dosis",
}


def _pending_note(pending: list[dict]) -> str:
    """Solo lo AMBIGUO o ausente que importa; jamás una pregunta al auxiliar."""
    notes = []
    for p in pending:
        if p["reason"] == "unit_not_stated":
            notes.append(f"la unidad de {p.get('value')}")
        elif p["attribute"] in _PENDING and p["reason"] == "not_stated":
            notes.append(_PENDING[p["attribute"]])
    return f" Falta confirmar {' y '.join(dict.fromkeys(notes))}." if notes else ""


def line_for(r: ObservationResult, raw: str) -> str:
    note = _pending_note(r.parsed.pending_validation if r.item else [])
    if r.outcome == "created_item" and r.item:
        return f"Anotado: {r.item.display_name}. Cantidad pendiente.{note}"
    if r.outcome == "attached_existing" and r.item:
        return f"{r.item.display_name} ya estaba en la lista; sumé tu aviso (la cantidad no cambió).{note}"
    return f"Anoté «{raw}» pero no puedo asegurar cuál producto es: queda para que compras lo confirme."


# ------------------------------------------------------------------ helpers de BD


async def _store_message(session, binding, inbound: InboundEvent, ev: ChannelEvent, *, kind, text, status,
                         author=None, sess=None, basis=None, now: datetime) -> ChannelMessage:  # fmt: skip
    m = ChannelMessage(
        organization_id=binding.organization_id, channel_binding_id=binding.id, inbound_event_id=inbound.id,
        external_message_id=ev.message_id, kind=kind, text=text, sent_at=ev.sent_at,
        received_at=now,  # el MISMO reloj que las sesiones: la antigüedad de un pendiente no depende de la base
        attribution_status=status, author_id=author.id if author else None,
        session_id=sess.id if sess else None, attribution_basis=basis,
        attributed_at=now if status == "attributed" else None,
        data_origin=origin_for(await org_kind(session, binding.organization_id)),  # captura real o simulación
    )  # fmt: skip
    session.add(m)
    await session.flush()
    return m


async def _auxiliary_actor(session, sess: ActorSession) -> Actor | None:
    """La persona de la sesión, solo si SIGUE siendo elegible (activa, disponible y con rol para reportar)."""
    person = await session.get(AppUser, sess.auxiliary_id)
    if sessions.ineligibility(person) is not None:
        return None
    return await load_actor(session, sess.auxiliary_id)


async def _record_segments(session, binding, actor: Actor, sess, msg: ChannelMessage, text: str,
                           key_prefix: str, cfg: ChannelConfig, basis_level: str = "declared") -> list[str]:  # fmt: skip
    segs, truncated = split_segments(text)
    lines: list[str] = []
    for i, seg in enumerate(segs):
        try:
            r = await record_observation(
                session, actor, text=seg, idempotency_key=f"{key_prefix}:{i}", ai=cfg.ai,
                origin=Origin("telegram_text", basis_level, sess.id, msg.id),
            )  # fmt: skip
            lines.append(line_for(r, seg))
        except DomainError as e:
            lines.append(f"No pude anotar «{seg}»: {e.message}")
    if truncated:
        lines.append("Anoté solo los primeros productos del mensaje; envía el resto aparte.")
    return lines


async def _pending_messages(session, binding) -> list[ChannelMessage]:
    return list(
        (
            await session.execute(
                select(ChannelMessage)
                .where(ChannelMessage.channel_binding_id == binding.id,
                       ChannelMessage.attribution_status == "pending_attribution")
                .order_by(ChannelMessage.received_at)
            )
        ).scalars()
    )  # fmt: skip


# ------------------------------------------------------------------ eventos


async def handle_event(session: AsyncSession, binding: ChannelBinding, ev: ChannelEvent,
                       inbound: InboundEvent, now: datetime, cfg: ChannelConfig) -> list[Reply]:  # fmt: skip
    await advisory_lock(session, f"chan:{binding.id}")  # un evento por chat a la vez
    org = binding.organization_id
    ack = [Reply("", answer_callback_id=ev.callback_id)] if ev.callback_id else []

    if ev.kind == "edited":
        return [
            Reply(
                "Vi que editaste un mensaje; no cambié nada. Si era otro producto, envíalo de nuevo."
            )
        ]
    if ev.kind == "other":
        return [Reply("Por ahora solo entiendo texto y fotos.")]

    if ev.kind == "callback":
        return ack + await _on_callback(session, binding, ev, inbound, now, cfg)

    text = (ev.text or "").strip()
    low = text.lower()
    if ev.kind == "start" or low in ("/start",):
        sess = await sessions.active_session(session, binding, now, cfg.idle)
        if sess:
            return [await _whoami(session, sess, cfg, prefix="Hola de nuevo. ")]
        return [
            _selector(
                await sessions.list_reporters(session, org), "Hola. ¿Quién va a reportar ahora?"
            )
        ]
    if ev.kind == "text":
        if low in CMD_LIST:
            return [await _list_reply(session, org, 0, cfg)]
        if low in CMD_CHANGE:
            return [
                _selector(
                    await sessions.list_reporters(session, org), "¿Quién va a reportar ahora?"
                )
            ]
        if low in CMD_WHO:
            sess = await sessions.active_session(session, binding, now, cfg.idle)
            return [await _whoami(session, sess, cfg)]
        if low in CMD_HELP:
            return [Reply(_HELP, keyboard=KEYBOARD)]
        if low in CMD_LOGOUT:
            done = await sessions.end_session(session, binding, now)
            return [
                Reply(
                    "Reportes finalizados: nadie queda identificado. Para volver a reportar, elige tu nombre en «Cambiar auxiliar»."
                    if done
                    else "No había una sesión activa.",
                    keyboard=KEYBOARD,
                )
            ]

    sess = await sessions.active_session(session, binding, now, cfg.idle)  # expira si corresponde
    actor = await _auxiliary_actor(session, sess) if sess else None
    if sess and actor is None:  # el auxiliar dejó de estar habilitado
        await sessions.end_session(session, binding, now)
        sess = None

    if ev.kind == "photo":
        return await _on_photo(session, binding, ev, inbound, sess, actor, now, cfg)

    if sess is None or actor is None:
        await _store_message(
            session,
            binding,
            inbound,
            ev,
            kind="text",
            text=text,
            status="pending_attribution",
            now=now,
        )
        emit(session, org_id=org, actor_id=None, action="message.pending_attribution", entity_type="inbound_event",
             entity_id=inbound.id, event_type="message.pending_attribution", aggregate_version=None,
             payload={"binding_id": str(binding.id)})  # fmt: skip
        return [_selector(await sessions.list_reporters(session, org),
                          "Guardé tu mensaje, pero no lo anoto a nombre de nadie hasta saber quién eres. ¿Quién eres?")]  # fmt: skip

    intent = classify_intent(text)
    await sessions.touch(session, sess, now, cfg.idle)
    msg = await _store_message(session, binding, inbound, ev, kind="text", text=text, status="attributed",
                               author=actor, sess=sess, basis="session_at_receipt", now=now)  # fmt: skip
    if intent is Intent.CHITCHAT or intent is Intent.EMPTY:
        return [
            Reply(
                f"Hola, {actor.display_name}. Escribe lo que falta (ej.: «falta ibuprofeno 400 mg») o envía una foto del envase.",
                keyboard=KEYBOARD,
            )
        ]
    if intent is Intent.QUESTION:
        return [
            Reply(
                "No registro preguntas. Para anotar un faltante escribe el producto; para consultar, usa «Ver lista».",
                keyboard=KEYBOARD,
            )
        ]
    lines = await _record_segments(session, binding, actor, sess, msg, text,
                                   f"tg:{binding.id}:{ev.event_id}", cfg)  # fmt: skip
    return [Reply("\n".join(lines) + f"\n{_footer(actor.display_name)}", keyboard=KEYBOARD)]


_HELP = (
    "Escribe lo que falta («falta ibuprofeno 400 mg») o envía una foto del envase. "
    "«Ver lista» muestra lo que ya está anotado; «Cambiar auxiliar» cambia quién reporta."
)


async def _whoami(
    session, sess: ActorSession | None, cfg: ChannelConfig, prefix: str = ""
) -> Reply:
    if sess is None:
        return Reply(
            "Nadie ha elegido identidad todavía (o la sesión expiró). Usa «Cambiar auxiliar».",
            keyboard=KEYBOARD,
        )
    u = await session.get(AppUser, sess.auxiliary_id)
    mins = int(cfg.idle.total_seconds() // 60)
    return Reply(f"{prefix}Estás reportando como {u.display_name} (identidad declarada; "
                 f"expira tras {mins} min sin uso).", keyboard=KEYBOARD)  # fmt: skip


async def _list_reply(session, org, page: int, cfg: ChannelConfig) -> Reply:
    items, agg, more, total, pending = await queries.list_items_page(
        session, org, page=page, size=cfg.page_size
    )
    if not total:
        return Reply("La lista de faltantes está vacía.", keyboard=KEYBOARD)
    lines = [
        f"Faltantes activos: {total}" + (f" · {pending} aviso(s) por revisar" if pending else "")
    ]
    for n, i in enumerate(items, start=page * cfg.page_size + 1):
        cnt, _, who = agg.get(i.id, (0, None, []))
        qty = (
            "cantidad pendiente"
            if i.target_quantity is None
            else f"{i.target_quantity.normalize():f} {i.quantity_unit}"
        )
        lines.append(
            f"{n}. {i.display_name} · {qty} · {cnt} aviso(s)"
            + (f" ({', '.join(who)})" if who else "")
        )
    buttons = [[Button("Ver más", f"lst:{page + 1}")]] if more else []
    return Reply("\n".join(lines), buttons, KEYBOARD)


async def _on_callback(session, binding, ev, inbound, now, cfg) -> list[Reply]:
    org = binding.organization_id
    data = ev.callback_data or ""
    if data.startswith("lst:"):
        try:
            page = max(0, int(data[4:]))
        except ValueError:
            return []
        return [await _list_reply(session, org, page, cfg)]
    if data.startswith("sel:"):
        try:
            uid = uuid.UUID(data[4:])
            sess = await sessions.select_auxiliary(
                session, binding, uid, now, cfg.idle, event_id=inbound.id
            )
        except (ValueError, DomainError) as e:
            # Botón antiguo o persona que ya no está disponible: se explica y se muestran los botones VIGENTES.
            return [
                Reply(
                    "No pude cambiar la identidad: "
                    + getattr(e, "message", "dato inválido")
                    + " Elige a alguien de la lista actual."
                ),
                _selector(
                    await sessions.list_reporters(session, org), "¿Quién va a reportar ahora?"
                ),
            ]
        u = await session.get(AppUser, sess.auxiliary_id)
        replies = [
            Reply(f"Listo, {u.display_name}. Reportando como {u.display_name}.", keyboard=KEYBOARD)
        ]
        pend = await _pending_messages(session, binding)
        if pend:
            fresh = [m for m in pend if m.received_at >= now - cfg.pending_max_age]
            note = f"Tienes {len(fresh)} mensaje(s) recibido(s) antes de identificarte."
            replies.append(Reply(f"{note} ¿Los anoto a nombre de {u.display_name}?",
                                 [[Button(f"Anotar a nombre de {u.display_name}", "att:ok"), Button("Descartar", "att:no")]]))  # fmt: skip
        return replies
    if data in ("att:ok", "att:no"):
        return await _on_pending_decision(session, binding, inbound, data == "att:ok", now, cfg)
    return []


async def _on_pending_decision(session, binding, inbound, accept: bool, now, cfg) -> list[Reply]:
    sess = await sessions.active_session(session, binding, now, cfg.idle)
    actor = await _auxiliary_actor(session, sess) if sess else None
    if sess is None or actor is None:
        return [_selector(await sessions.list_reporters(session, binding.organization_id),
                          "Primero necesito saber quién eres.")]  # fmt: skip
    org = binding.organization_id
    pend = await _pending_messages(session, binding)
    if not pend:
        return [Reply("No hay mensajes pendientes.", keyboard=KEYBOARD)]
    lines: list[str] = []
    done = stale = 0
    for m in pend:
        fresh = m.received_at >= now - cfg.pending_max_age
        if not accept or not fresh:
            m.attribution_status, m.discarded_at = "discarded", now
            m.discard_reason = "stale_unattributed" if accept else "discarded_by_user"
            stale += 1 if accept else 0
            emit(session, org_id=org, actor_id=actor.id, action="message.discarded", entity_type="channel_message",
                 entity_id=m.id, event_type="message.discarded", aggregate_version=None,
                 payload={"reason": m.discard_reason})  # fmt: skip
            continue
        # La persona confirmó explícitamente: queda atribuida con base `confirmed_after_identification`.
        m.attribution_status, m.author_id, m.session_id = "attributed", actor.id, sess.id
        m.attribution_basis, m.attributed_at = "confirmed_after_identification", now
        emit(session, org_id=org, actor_id=actor.id, action="message.attributed", entity_type="channel_message",
             entity_id=m.id, event_type="message.attributed", aggregate_version=None,
             payload={"basis": m.attribution_basis, "session_id": str(sess.id)})  # fmt: skip
        orig = (await session.get(InboundEvent, m.inbound_event_id)).external_event_id
        if m.kind == "text":
            lines += await _record_segments(session, binding, actor, sess, m, m.text or "",
                                            f"tg:{binding.id}:{orig}", cfg)  # fmt: skip
        else:
            att = (
                await session.execute(
                    select(Attachment).where(Attachment.channel_message_id == m.id)
                )
            ).scalar_one()
            att.author_id = actor.id  # desde ahora la foto puede procesarse
            lines.append(
                "La foto quedó a tu nombre; la proceso en un momento."
                if cfg.vision_enabled
                else "La foto quedó a tu nombre; compras la revisará."
            )
        done += 1
    await sessions.touch(session, sess, now, cfg.idle)
    if not accept:
        return [Reply("Descartados. No se anotó nada.", keyboard=KEYBOARD)]
    if stale:
        lines.append(
            f"{stale} mensaje(s) eran de hace demasiado rato y no se pueden atribuir; envíalos de nuevo."
        )
    return [
        Reply(
            "\n".join(lines or ["Listo."]) + f"\n{_footer(actor.display_name)}", keyboard=KEYBOARD
        )
    ]


async def _on_photo(session, binding, ev, inbound, sess, actor, now, cfg) -> list[Reply]:
    org = binding.organization_id
    ph = ev.photo
    if ph is None:
        return [Reply("No pude leer la foto.")]
    attributed = sess is not None and actor is not None
    msg = await _store_message(session, binding, inbound, ev, kind="photo", text=ev.text,
                               status="attributed" if attributed else "pending_attribution",
                               author=actor if attributed else None, sess=sess if attributed else None,
                               basis="session_at_receipt" if attributed else None, now=now)  # fmt: skip
    session.add(Attachment(
        organization_id=org, channel_message_id=msg.id, author_id=actor.id if attributed else None,
        provider_file_id=ph.file_id, provider_file_unique_id=ph.file_unique_id, width=ph.width,
        height=ph.height, file_size=ph.file_size, caption=ev.text, status="pending",
        data_origin=origin_for(await org_kind(session, org)),  # el archivo es captura (o simulación)
    ))  # fmt: skip
    await session.flush()
    emit(session, org_id=org, actor_id=actor.id if attributed else None, action="photo.received",
         entity_type="channel_message", entity_id=msg.id, event_type="photo.received", aggregate_version=None,
         payload={"attributed": attributed, "has_caption": bool(ev.text)})  # fmt: skip
    if not attributed:
        return [_selector(await sessions.list_reporters(session, org),
                          "Guardé la foto, pero no la anoto a nombre de nadie hasta saber quién eres. ¿Quién eres?")]  # fmt: skip
    await sessions.touch(session, sess, now, cfg.idle)
    base = (f"Recibí la foto, {actor.display_name}. " + ("La estoy revisando y te aviso." if cfg.vision_enabled
            else "La guardé; todavía no la leo automáticamente, queda para revisión de compras."))  # fmt: skip
    return [Reply(base + f"\n{_footer(actor.display_name)}", keyboard=KEYBOARD)]


# ------------------------------------------------------------------ salida


async def enqueue(session: AsyncSession, binding: ChannelBinding, inbound_event_id: uuid.UUID | None,
                  replies: list[Reply], *, start_seq: int = 0) -> int:  # fmt: skip
    seq = start_seq
    for r in replies:
        if r.answer_callback_id:
            session.add(OutboundMessage(organization_id=binding.organization_id, channel_binding_id=binding.id,
                                        inbound_event_id=inbound_event_id, seq=seq, method="answerCallbackQuery",
                                        payload={"callback_query_id": r.answer_callback_id}))  # fmt: skip
            seq += 1
        if r.text:
            session.add(OutboundMessage(
                organization_id=binding.organization_id, channel_binding_id=binding.id,
                inbound_event_id=inbound_event_id, seq=seq, method="sendMessage",
                payload={"text": r.text, "buttons": [[{"text": b.text, "data": b.data} for b in row] for row in r.buttons],
                         "keyboard": r.keyboard},
            ))  # fmt: skip
            seq += 1
    await session.flush()
    return seq
