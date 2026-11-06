"""Flujo de canal independiente del proveedor: evento normalizado → sesión → interpretación →
observación → respuestas. Telegram es solo un adaptador (app/telegram).

Reglas (HLD §3.1, §7, §8):
* Un mensaje sin identidad vigente queda `pending_attribution`: NUNCA se atribuye solo (ni al
  auxiliar anterior ni al siguiente). Tras identificarse, la persona decide registrarlo a su nombre o
  descartarlo; los mensajes viejos no se pueden atribuir.
* El bot no aplica cantidades ni aprueba nada: solo registra avisos (observaciones) y consulta.
* Una respuesta nunca asegura que un producto ambiguo fue identificado.
"""

import html
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.permissions import can_apply_quantity
from app.domain.quantities import QuantityOp, QuantityUnit
from app.interpretation.ai import AIInterpreter
from app.interpretation.segments import Intent, classify_intent, split_segments
from app.models import (
    ActorSession,
    AppUser,
    Attachment,
    ChannelBinding,
    ChannelMessage,
    DemandItem,
    InboundEvent,
    OutboundMessage,
)
from app.services import coverage as coverage_svc
from app.services import queries, sessions
from app.services.access import AccountContext
from app.services.actors import Actor, load_actor
from app.services.demand import ObservationResult, Origin, item_in_cart, record_observation
from app.services.events import advisory_lock, emit
from app.services.provenance import org_kind, origin_for
from app.services.quantities import apply_quantity_command

# Dos filas: el listado en la primera y las dos acciones de identidad/ayuda en la segunda.
KEYBOARD = [["📋 Ver listado de Faltantes"], ["👤 Cambiar auxiliar", "❓ Ayuda"]]
# Con un solo auxiliar habilitado no hay nada que cambiar: el teclado no ofrece el selector.
KEYBOARD_SINGLE = [["📋 Ver listado de Faltantes"], ["❓ Ayuda"]]
# En un teléfono personal no hay selector de identidad: la cuenta autorizada ya la fija.
KEYBOARD_PERSONAL = [["📋 Ver listado de Faltantes"], ["❓ Ayuda"]]
CMD_LIST = {
    "/lista", "/faltantes", "ver lista", "lista", "ver faltantes", "faltantes",
    "📋 ver listado de faltantes", "ver listado de faltantes", "listado de faltantes",
}  # fmt: skip
CMD_CHANGE = {"/cambiar", "cambiar auxiliar", "cambiar", "👤 cambiar auxiliar"}
# Se conservan las variantes antiguas aunque «Ver auxiliar activo» ya no esté en el teclado.
CMD_WHO = {
    "/quien", "¿quién soy?", "quien soy", "¿quien soy?", "/yo",
    "ver auxiliar activo", "auxiliar activo", "¿quién está activo?", "quien esta activo",
}  # fmt: skip
CMD_HELP = {"/ayuda", "/help", "ayuda", "❓ ayuda"}
# «/salir» sigue disponible como comando de compatibilidad, aunque ya no está en el teclado.
CMD_LOGOUT = {"/salir", "/finalizar", "finalizar reportes", "cerrar sesion", "cerrar sesión"}


def _command_text(text: str) -> str:
    """Texto normalizado para comparar comandos: sin espacios extra, en minúsculas y sin la
    mención `@bot` que Telegram agrega en grupos (`/faltantes@MiBot`)."""
    low = (text or "").strip().lower()
    if low.startswith("/"):
        first, sep, rest = low.partition(" ")
        low = first.split("@", 1)[0] + (sep + rest if sep else "")
    return low


def _is_command_text(text: str) -> bool:
    """Todo texto que empieza con «/» es un comando, nunca el nombre de un producto."""
    return _command_text(text).startswith("/")


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
    # Formato que entiende el canal (Telegram: "HTML"). Solo se usa en respuestas con etiquetas.
    parse_mode: str | None = None


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
    # Quién envía (cuenta de Telegram) y tipo de chat. Sin esto no se puede validar la cuenta.
    sender_id: str | None = None
    chat_kind: str | None = None


@dataclass(frozen=True)
class ChannelConfig:
    idle: timedelta = timedelta(minutes=10)
    pending_max_age: timedelta = timedelta(minutes=15)
    page_size: int = 8
    vision_enabled: bool = False  # hay un proveedor de visión (real o simulado) conectado
    ai: AIInterpreter | None = None
    # Organización que recibe las solicitudes de acceso. Sin ella, el bot lo dice con honestidad.
    access_org_id: uuid.UUID | None = None


# ------------------------------------------------------------------ textos


def _footer(name: str, personal: bool = False) -> str:
    return f"— Reportando como {name}" + (" (cuenta autorizada)" if personal else "")


def _is_personal(account: AccountContext | None) -> bool:
    return account is not None and account.personal


def _keyboard(account: AccountContext | None) -> list[list[str]]:
    if _is_personal(account):
        return KEYBOARD_PERSONAL
    if account is not None and account.auxiliary_count is not None:
        return KEYBOARD_SINGLE if account.auxiliary_count <= 1 else KEYBOARD
    return KEYBOARD


def _account_id(account: AccountContext | None) -> uuid.UUID | None:
    return account.account_id if account else None


def _branch(account: AccountContext | None) -> str | None:
    return account.branch_label if account else None


_EMPTY_ACCOUNT = (
    "Esta cuenta no tiene auxiliares disponibles para reportar en este momento. "
    "Pide a la administración que revise quién está habilitado en este teléfono."
)


async def _selectable(
    session, binding: ChannelBinding, account: AccountContext | None
) -> list[AppUser]:
    """Quiénes se pueden elegir: SOLO los habilitados en esta cuenta compartida.

    Un vínculo legado sin cuenta conserva el selector de la organización (compatibilidad
    histórica); una cuenta compartida nunca ve personas de otra cuenta.
    """
    if account is not None and not account.personal:
        return await sessions.list_account_reporters(
            session, binding.organization_id, account.account_id
        )
    return await sessions.list_reporters(session, binding.organization_id)


def _selector(
    auxs: list[AppUser],
    text: str,
    *,
    current: str | None = None,
    keyboard: list[list[str]] | None = None,
) -> Reply:
    """Botones con las personas activas y disponibles (datos de PostgreSQL).

    Si `current` viene, se informa quién reporta ahora antes de preguntar, para que elegir al mismo
    auxiliar sea una decisión informada y no un cambio a ciegas. `keyboard` respeta la forma del
    teclado de la cuenta (sin «Cambiar auxiliar» cuando hay cero o un auxiliar).
    """
    kb = KEYBOARD if keyboard is None else keyboard
    if current:
        text = f"Ahora reporta: {current}.\n\n{text}"
    if not auxs:
        return Reply(
            "No hay personas disponibles para reportar en este momento. Pide a compras que revise el personal.",
            keyboard=kb,
        )
    rows, row = [], []
    for u in auxs:
        row.append(Button(u.display_name, f"sel:{u.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return Reply(text, rows, kb)


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


_UNIT_LABELS = {
    "box": ("caja", "cajas"),
    "package": ("paquete", "paquetes"),
    "unit": ("unidad", "unidades"),
    "bottle": ("frasco", "frascos"),
}


def _fmt_quantity(value, unit: str | None) -> str:
    """Cantidad declarada con su unidad, en el vocabulario que usa la persona («2 cajas»)."""
    n = Decimal(str(value)).normalize()
    number = f"{n:f}"
    labels = _UNIT_LABELS.get(unit)
    if labels is None:
        return number
    return f"{number} {labels[0] if n == 1 else labels[1]}"


def _avisos(count) -> str:
    """«1 aviso» / «N avisos», sin la forma «aviso(s)»."""
    return f"{count} aviso" if int(count) == 1 else f"{count} avisos"


def _one_line(value: str) -> str:
    """Colapsa espacios y saltos de línea: una etiqueta HTML nunca debe quedar partida."""
    return " ".join(str(value).split())


def _declared_text(r: ObservationResult) -> str:
    """Lo que la persona declaró. Nunca se presenta como cantidad objetivo ni se aplica."""
    obs = r.observation
    value = obs.declared_quantity if obs is not None else None
    unit = obs.declared_unit if obs is not None else None
    if value is None:
        value, unit = r.parsed.requested_quantity, r.parsed.requested_unit
    return "Cantidad no indicada" if value is None else _fmt_quantity(value, unit)


def _quantity_origin(r: ObservationResult) -> str | None:
    """Cómo se estableció la cantidad: escrita por la persona (`explicit`) o predeterminada."""
    obs = r.observation
    return (obs.interpretation or {}).get("quantity_origin") if obs is not None else None


def line_for(r: ObservationResult, raw: str) -> str:
    note = _pending_note(r.parsed.pending_validation if r.item else [])
    if r.outcome == "created_item" and r.item:
        qty = _declared_text(r)
        if _quantity_origin(r) == "default":
            qty += " solicitada"
        return f"Anotado: {r.item.display_name} — {qty}.{note}"
    if r.outcome == "attached_existing" and r.item:
        if _quantity_origin(r) == "default":
            return (
                f"Anotado: {r.item.display_name} — 1 unidad solicitada "
                f"(no tenías cantidad anotada).{note}"
            )
        return (
            f"{r.item.display_name} ya estaba en tu lista; registré tu aviso "
            f"(la cantidad no cambió).{note}"
        )
    if r.outcome == "blocked_in_cart" and r.item:
        return (
            f"{r.item.display_name} está en carrito: no cambié la cantidad solicitada. "
            "Para corregirla, pide a compras que lo retire del carrito."
        )
    if r.outcome == "needs_confirmation":
        return f"Guardé tu aviso de {r.parsed.display_name}; falta que confirmes cómo seguimos."
    return f"Anoté «{raw}» pero no puedo asegurar cuál producto es: queda para que compras lo confirme."


# ------------------------------------------------------------------ helpers de BD


async def _store_message(session, binding, inbound: InboundEvent, ev: ChannelEvent, *, kind, text, status,
                         author=None, sess=None, basis=None, account_id=None, now: datetime) -> ChannelMessage:  # fmt: skip
    m = ChannelMessage(
        organization_id=binding.organization_id, channel_binding_id=binding.id, inbound_event_id=inbound.id,
        external_message_id=ev.message_id, kind=kind, text=text, sent_at=ev.sent_at,
        received_at=now,  # el MISMO reloj que las sesiones: la antigüedad de un pendiente no depende de la base
        attribution_status=status, author_id=author.id if author else None,
        session_id=sess.id if sess else None, account_id=account_id, attribution_basis=basis,
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


async def _personal_identity(
    session, binding, account: AccountContext, now: datetime, idle: timedelta,
    event_id: uuid.UUID | None = None,
) -> tuple[ActorSession | None, Actor | None, DomainError | None]:  # fmt: skip
    """Identidad FIJA de una cuenta personal: la persona ligada, sin selector ni pasos extra.

    La sesión sigue expirando por inactividad, pero se renueva sola en el siguiente mensaje. Si la
    persona deja de estar activa/disponible, no se identifica (el mensaje queda pendiente).
    """
    person = await session.get(AppUser, account.app_user_id) if account.app_user_id else None
    err = sessions.ineligibility(person)
    if err is not None:
        return None, None, err
    sess = await sessions.active_session(session, binding, now, idle)
    if sess is None or sess.identity_level != "account" or sess.account_id != account.account_id:
        sess = await sessions.select_auxiliary(
            session, binding, account.app_user_id, now, idle, event_id=event_id,
            identity_level="account", account_id=account.account_id,
        )  # fmt: skip
    else:
        await sessions.touch(session, sess, now, idle)
    actor = await load_actor(session, account.app_user_id)
    if actor is None:
        return None, None, DomainError("PERSON_NOT_FOUND", "La persona ya no está activa.", status_code=403)
    return sess, actor, None


async def _single_auxiliary_identity(
    session, binding, account: AccountContext, person: AppUser, now: datetime, idle: timedelta,
    event_id: uuid.UUID | None = None,
) -> tuple[ActorSession | None, Actor | None, DomainError | None]:  # fmt: skip
    """Con UN solo auxiliar habilitado no se pregunta quién es: se identifica automáticamente.

    No depende de la sesión anterior ni de su vencimiento: tras «Finalizar reportes», un reinicio
    o días sin uso, el siguiente mensaje vuelve a identificar a la misma persona. Sigue siendo
    identificación declarada (el teléfono compartido no autentica a nadie).
    """
    err = sessions.ineligibility(person)
    if err is not None:
        return None, None, err
    sess = await sessions.active_session(session, binding, now, idle)
    if (
        sess is None
        or sess.auxiliary_id != person.id
        or sess.account_id != account.account_id
    ):
        sess = await sessions.select_auxiliary(
            session, binding, person.id, now, idle, event_id=event_id,
            identity_level="declared", account_id=account.account_id,
        )  # fmt: skip
    else:
        await sessions.touch(session, sess, now, idle)
    actor = await load_actor(session, person.id)
    if actor is None:
        return None, None, DomainError(
            "PERSON_NOT_FOUND", "La persona ya no está activa.", status_code=403
        )
    return sess, actor, None


async def _resolve_identity(
    session, binding, account: AccountContext | None, now: datetime, idle: timedelta,
    event_id: uuid.UUID | None = None,
) -> tuple[ActorSession | None, Actor | None, DomainError | None]:  # fmt: skip
    if _is_personal(account):
        return await _personal_identity(session, binding, account, now, idle, event_id)
    if account is not None:
        auxs = await sessions.list_account_reporters(
            session, binding.organization_id, account.account_id
        )
        if len(auxs) == 1:
            return await _single_auxiliary_identity(
                session, binding, account, auxs[0], now, idle, event_id
            )
    sess = await sessions.active_session(session, binding, now, idle)  # expira si corresponde
    if sess is not None and account is not None:
        if not await sessions.is_account_auxiliary(
            session, account.account_id, sess.auxiliary_id
        ):
            # Baja en esta cuenta: la sesión deja de identificar aunque no se haya revocado.
            await sessions.end_session(session, binding, now)
            sess = None
    actor = await _auxiliary_actor(session, sess) if sess else None
    if sess and actor is None:  # el auxiliar dejó de estar habilitado
        await sessions.end_session(session, binding, now)
        sess = None
    return sess, actor, None


def _identity_error_text(err: DomainError | None) -> str:
    return (
        err.message
        if err is not None
        else "Nadie ha elegido identidad todavía (o la sesión expiró)."
    )


async def _record_segments(session, binding, actor: Actor, sess, msg: ChannelMessage, text: str,
                           key_prefix: str, cfg: ChannelConfig, basis_level: str = "declared",
                           account_id: uuid.UUID | None = None,
                           branch_label: str | None = None,
                           confirm: bool = True) -> tuple[list[str], list[dict]]:
    """Registra cada segmento. Devuelve (líneas de respuesta, confirmaciones pendientes).

    `confirm=True` (Telegram) no crea nada cuando hay un reporte coincidente: deja el mensaje
    original como `pending_confirmation` y el botón resuelve (D-135). Solo se pregunta por el
    primer segmento; los demás se registran sin interrumpir el resto del mensaje.
    """
    segs, truncated = split_segments(text)
    lines: list[str] = []
    pending: list[dict] = []
    for i, seg in enumerate(segs):
        key = f"{key_prefix}:{i}"
        origin = Origin(
            "telegram_text", basis_level, sess.id, msg.id, account_id=account_id,
            branch_label=branch_label,
        )  # fmt: skip
        try:
            r = await record_observation(
                session, actor, text=seg, idempotency_key=key, ai=cfg.ai,
                origin=origin, confirmation_mode="telegram" if confirm else None,
            )  # fmt: skip
            if r.outcome == "needs_confirmation" and not pending:
                pending.append(
                    {
                        "segment": seg,
                        "key": key,
                        "result": r,
                        "origin": origin,
                        "basis_level": basis_level,
                        "account_id": account_id,
                        "branch_label": branch_label,
                    }
                )
                continue
            if r.outcome == "needs_confirmation":
                # Segundo producto del mismo mensaje: se registra sin otra pregunta (ya hay una).
                r = await record_observation(
                    session, actor, text=seg, idempotency_key=f"{key}:auto",
                    ai=cfg.ai, origin=origin, confirmation_mode=None,
                )  # fmt: skip
            lines.append(line_for(r, seg))
        except DomainError as e:
            lines.append(f"No pude anotar «{seg}»: {e.message}")
    if truncated:
        lines.append("Anoté solo los primeros productos del mensaje; envía el resto aparte.")
    return lines, pending


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


def _discard_pending_command(session, org, m: ChannelMessage, actor_id, now) -> bool:
    """Resuelve un pendiente que era un comando, no un producto. Devuelve si pedía el listado.

    Sirve para filas guardadas por versiones anteriores que aún no reconocían el comando: la
    identidad no las convierte en reportes.
    """
    m.attribution_status, m.discarded_at = "discarded", now
    m.discard_reason = "command_not_report"
    emit(session, org_id=org, actor_id=actor_id, action="message.discarded", entity_type="channel_message",
         entity_id=m.id, event_type="message.discarded", aggregate_version=None,
         payload={"reason": m.discard_reason})  # fmt: skip
    return _command_text(m.text or "") in CMD_LIST


# ------------------------------------------------------------------ confirmación de coincidencias

_QTY_RE = re.compile(r"(\d+(?:[.,]\d+)?)")
_UNIT_WORDS = {
    "caja": "box", "cajas": "box", "cajita": "box", "cajitas": "box",
    "paquete": "package", "paquetes": "package",
    "unidad": "unit", "unidades": "unit",
    "frasco": "bottle", "frascos": "bottle", "botella": "bottle", "botellas": "bottle",
}  # fmt: skip


def _parse_quantity_text(text: str, default_unit: str | None) -> tuple[Decimal, str] | None:
    """Lee una cantidad escrita por la persona. Si no hay número, no se inventa nada."""
    m = _QTY_RE.search(text or "")
    if m is None:
        return None
    try:
        value = Decimal(m.group(1).replace(",", "."))
    except InvalidOperation:
        return None
    if value <= 0 or value != value.to_integral_value():
        return None
    unit = default_unit
    for word in re.findall(r"[a-záéíóúñ]+", (text or "").lower()):
        if word in _UNIT_WORDS:
            unit = _UNIT_WORDS[word]
            break
    return (value, unit) if unit in {u.value for u in QuantityUnit} else None


_ADD_MARKERS = {
    "mas", "más", "adicional", "adicionales", "sumar", "sumale", "súmale", "sumando",
    "agregar", "agrega", "agregale", "agrégale", "anadir", "añadir", "añade", "anade",
}  # fmt: skip
_SET_MARKERS = {
    "total", "en", "corregir", "corrijo", "correccion", "corrección", "fijar", "fijo",
    "dejar", "dejalo", "déjalo", "poner", "pon", "quedar", "queda", "quedan", "seria",
    "sería", "serian", "serían", "mejor",
}  # fmt: skip
# Marcadores que fijan la operación sin ambigüedad («en total 15», «me equivoqué, son 5»).
_SET_OP_MARKERS = {"total", "corregir", "corrijo", "fijar", "fijo", "poner", "pon"}
_CORRECTION_MARKERS = {
    "equivoque", "equivoqué", "equivocado", "equivocada", "error", "confundi", "confundí",
    "rectificar", "rectifico", "rectificacion", "rectificación",
}  # fmt: skip
_CANCEL_WORDS = {
    "cancelar", "cancela", "cancelen", "cancelalo", "cáncelalo", "olvidalo", "olvídalo",
    "dejalo", "déjalo", "nada",
}  # fmt: skip
_AFFIRM_WORDS = {
    "si", "sí", "correcto", "correcta", "exacto", "exacta", "claro", "dale", "eso", "asi",
    "así",
}  # fmt: skip
# Ayuda fija de la corrección por texto: la conversación de cantidades no usa botones (D-147).
_CORRECTION_HELP = (
    'Escribe la cantidad correcta, por ejemplo: "En total 5 cajas" o "5 cajas más".'
)
# Palabras que no convierten un texto en un reporte de producto: solo expresan una cantidad.
_QTY_ONLY_WORDS = (
    _ADD_MARKERS
    | _SET_MARKERS
    | _CORRECTION_MARKERS
    | set(_UNIT_WORDS)
    | {
        "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas", "son", "es",
        "y", "o", "por", "favor", "ahora", "entonces", "creo", "digamos", "tengo", "tiene",
        "tenemos", "hay", "anotado", "anotadas", "anotados", "solicitado", "solicitadas",
        "solicitados", "pedido", "pedidas", "pedidos", "quiero", "queremos", "sean", "sera",
        "será", "van", "va", "hacen", "hace", "falta", "faltan", "mas", "más",
        "me", "mi", "si", "sí", "perdon", "perdón", "disculpa", "disculpas",
    }
)  # fmt: skip


def _parse_quantity_revision(
    text: str, default_unit: str | None
) -> tuple[Decimal, str, str | None] | None:
    """Cantidad + operación explícita si la persona la escribió («5 más», «en total 15»).

    Sin marcador, la operación queda ambigua y se pregunta antes de cambiar nada.
    """
    parsed = _parse_quantity_text(text, default_unit)
    if parsed is None:
        return None
    words = set(re.findall(r"[a-záéíóúñ]+", (text or "").lower()))
    has_add = bool(words & _ADD_MARKERS)
    has_set = bool(words & (_SET_OP_MARKERS | _CORRECTION_MARKERS))
    op = None
    if has_add and not has_set:
        op = "add"
    elif has_set and not has_add:
        op = "set"
    # Con marcadores contradictorios («en total 5 más») la operación sigue siendo ambigua.
    return parsed[0], parsed[1], op


def _is_quantity_revision_text(text: str) -> bool:
    """¿El texto solo expresa una cantidad? Evita confundir un producto nuevo con una corrección."""
    words = re.findall(r"[a-záéíóúñ]+", (text or "").lower())
    return bool(_QTY_RE.search(text or "")) and all(w in _QTY_ONLY_WORDS for w in words)


def _is_cancel_text(text: str) -> bool:
    """¿El texto es solo una negativa a la pregunta pendiente? No cambia ninguna cantidad."""
    words = re.findall(r"[a-záéíóúñ]+", (text or "").lower())
    return bool(words) and all(w in _CANCEL_WORDS for w in words)


def _is_affirmation_text(text: str) -> bool:
    """¿El texto es solo una afirmación? Sirve para confirmar por texto una aclaración de unidad."""
    words = re.findall(r"[a-záéíóúñ]+", (text or "").lower())
    return bool(words) and all(w in _AFFIRM_WORDS for w in words)


def _fmt_qty(value: Decimal | str | None) -> str:
    """Formatea una cantidad ya validada; acepta `Decimal` o el texto serializado del reporte."""
    return "?" if value is None else f"{Decimal(str(value)).normalize():f}"


async def _author_name(session, org, user_id) -> str | None:
    if user_id is None:
        return None
    user = await session.get(AppUser, user_id)
    return user.display_name if user and user.organization_id == org else None


async def _ask_confirmation(
    session, binding, actor: Actor, msg: ChannelMessage, pending: dict, personal: bool,
    keyboard: list[list[str]] | None = None,
) -> Reply:  # fmt: skip
    """Deja el mensaje `pending_confirmation` y devuelve la pregunta.

    La corrección de cantidades del mismo autor se responde solo por texto; el aviso de otro
    autor conserva sus botones (Confirmar/Modificar/Cancelar), porque no es una corrección.
    """
    keyboard = keyboard or (KEYBOARD_PERSONAL if personal else KEYBOARD)
    org = binding.organization_id
    r: ObservationResult = pending["result"]
    data = {
        "segment": pending["segment"],
        "key": pending["key"],
        "basis_level": pending["basis_level"],
        "account_id": str(pending["account_id"]) if pending["account_id"] else None,
        "branch_label": pending.get("branch_label"),
        "session_id": str(pending["origin"].actor_session_id)
        if pending["origin"].actor_session_id
        else None,
        "declared_quantity": None,
        "declared_unit": None,
    }
    # La cantidad escrita, si la hubo, se conserva como dato declarado (nunca como pedido).
    parsed_qty = r.parsed.requested_quantity
    parsed_unit = r.parsed.requested_unit
    if parsed_qty is not None:
        data["declared_quantity"] = _fmt_qty(parsed_qty)
        data["declared_unit"] = parsed_unit
    declared = parsed_qty
    unit = parsed_unit

    if r.same_author_item is not None:
        item = r.same_author_item
        raw_target = item.get("target_quantity")
        target = Decimal(str(raw_target)) if raw_target is not None else None
        raw_previous = item.get("declared_quantity")
        previous = Decimal(str(raw_previous)) if raw_previous is not None else None
        # Referencia para preguntar «¿total o adicional?»: manda la cantidad objetivo; si no hay,
        # la cantidad solicitada anterior del mismo autor. Así la propuesta que ve compras es
        # coherente con la cantidad sobre la que se aplicaría.
        baseline = target if target is not None else previous
        baseline_unit = (
            item.get("quantity_unit") if target is not None else item.get("declared_unit")
        )
        data.update(
            {
                "kind": "same_author",
                "same_item_id": str(item["demand_item_id"]),
                "target_quantity": None if target is None else _fmt_qty(target),
                "target_unit": item.get("quantity_unit"),
                "previous_declared_quantity": None if previous is None else _fmt_qty(previous),
                "previous_declared_unit": item.get("declared_unit"),
                "baseline_quantity": None if baseline is None else _fmt_qty(baseline),
                "baseline_unit": baseline_unit,
            }
        )
        msg.attribution_status = "pending_confirmation"
        msg.confirmation_status = "pending_same_author_quantity"
        msg.confirmation_data = data
        data["unit_mismatch"] = (baseline_unit or "unit") != (unit or "unit")
        name = item.get("display_name")
        # La corrección de cantidades se conversa SOLO por texto: sin botones inline (D-147).
        if declared is None:
            return Reply(
                f"Ya tienes {_fmt_quantity(baseline, baseline_unit)} de {name}. "
                f"{_CORRECTION_HELP}",
                keyboard=keyboard,
            )
        if baseline is None:
            return Reply(
                f"No tenías una cantidad anotada de {name}. Escribe la cantidad que quieres "
                'solicitar, por ejemplo: "5 cajas".',
                keyboard=keyboard,
            )
        return Reply(
            f"Ya tienes {_fmt_quantity(baseline, baseline_unit)} de {name}. "
            f"¿Quieres agregar más o cambiar el total? {_CORRECTION_HELP}",
            keyboard=keyboard,
        )

    other = r.other_reports[0]
    author = await _author_name(session, org, other.get("reported_by"))
    data.update(
        {
            "kind": "other_report",
            "other_item_ids": [str(o["demand_item_id"]) for o in r.other_reports],
        }
    )
    msg.attribution_status = "pending_confirmation"
    msg.confirmation_status = "pending_other_report"
    msg.confirmation_data = data
    qty = other.get("target_quantity")
    declared_qty = other.get("declared_quantity")
    parts = []
    if qty is not None:
        parts.append(
            f"pedida para compra: {_fmt_qty(qty)} {other.get('quantity_unit') or ''}".strip()
        )
    if declared_qty is not None:
        parts.append(
            f"solicitada: {_fmt_qty(declared_qty)} {other.get('declared_unit') or ''}".strip()
        )
    qty_text = ", ".join(parts) if parts else "cantidad no indicada"
    who = author or "otra persona"
    return Reply(
        f"Ya hay un reporte de {who} por {other.get('display_name')} ({qty_text}). "
        "Cada persona conserva su propio reporte: no voy a sumar tu aviso al de esa persona. "
        "¿Lo creo como un reporte tuyo independiente?",
        [
            [Button("Confirmar mi reporte", f"rp:ok:{msg.id}")],
            [Button("Modificar cantidad", f"rp:new:{msg.id}"),
             Button("Cancelar", f"rp:cancel:{msg.id}")],
        ],
        keyboard,
    )


async def _resolve_pending_message(
    session, msg: ChannelMessage, status: str
) -> None:
    msg.confirmation_status = status
    if status == "cancelled":
        msg.attribution_status = "discarded"
    else:
        msg.attribution_status = "attributed"


async def _record_confirmed(
    session, actor: Actor, msg: ChannelMessage, info: dict, *,
    declared_quantity: Decimal | None = None,
    declared_unit: str | None = None,
    quantity_proposal: dict | None = None,
    key_suffix: str = "",
) -> ObservationResult:
    """Registra el aviso que había quedado a la espera de confirmación. No suma cantidades."""
    account_id = uuid.UUID(info["account_id"]) if info.get("account_id") else None
    session_id = uuid.UUID(info["session_id"]) if info.get("session_id") else None
    origin = Origin(
        "telegram_text", info.get("basis_level") or "declared", session_id, msg.id,
        account_id=account_id, branch_label=info.get("branch_label"),
    )  # fmt: skip
    return await record_observation(
        session,
        actor,
        text=info["segment"],
        idempotency_key=f"{info['key']}{key_suffix}",
        origin=origin,
        declared_quantity=declared_quantity
        if declared_quantity is not None
        else (Decimal(info["declared_quantity"]) if info.get("declared_quantity") else None),
        declared_unit=declared_unit or info.get("declared_unit"),
        quantity_proposal=quantity_proposal,
        confirmation_mode=None,
    )


async def _apply_or_propose(
    session, actor: Actor, info: dict, operation: QuantityOp, value: Decimal, unit: str
) -> str:
    """Aplica la cantidad si la persona puede; si no, deja la propuesta a la administración.

    El botón es una decisión explícita de la persona, no una inferencia: SET/ADD vienen del
    botón, no de sumar textos. Aun así, el auxiliar no aplica cantidades (D-124/D-135).
    """
    item_id = uuid.UUID(info["same_item_id"])
    item = await session.get(DemandItem, item_id)
    if item is None or item.organization_id != actor.organization_id or item.status != "active":
        return "No pude aplicar la cantidad: el reporte cambió. Revisa la lista."
    try:
        quantity_unit = QuantityUnit(unit)
    except ValueError:
        quantity_unit = QuantityUnit(item.quantity_unit or "unit")
    if can_apply_quantity(actor.roles):
        result = await apply_quantity_command(
            session,
            actor,
            item.id,
            operation=operation,
            value=value,
            unit=quantity_unit,
            reason="Confirmación explícita en Telegram",
            idempotency_key=f"{info['key']}:{operation.value}",
            expected_version=item.version,
        )
        after = result.event.after_value
        return (
            f"Cantidad actualizada: {_fmt_qty(after)} {result.event.after_unit or ''}.".strip()
            if after is not None
            else "Cantidad actualizada."
        )
    return "Registré tu aviso. Compras revisará la cantidad solicitada."


async def _on_report_confirmation(
    session, binding, ev: ChannelEvent, data: str, now: datetime, cfg: ChannelConfig,
    account: AccountContext | None,
) -> list[Reply]:  # fmt: skip
    try:
        _, action, raw_id = data.split(":", 2)
        msg_id = uuid.UUID(raw_id)
    except (ValueError, AttributeError):
        return []
    sess, actor, err = await _resolve_identity(
        session, binding, account, now, cfg.idle, event_id=None
    )
    if actor is None or sess is None:
        return [Reply(_identity_error_text(err), keyboard=_keyboard(account))]
    msg = await session.get(ChannelMessage, msg_id)
    if (
        msg is None
        or msg.channel_binding_id != binding.id
        or msg.confirmation_status is None
        or msg.confirmation_status in ("resolved", "cancelled")
    ):
        return [Reply("Ese botón ya no está vigente.", keyboard=_keyboard(account))]
    if msg.author_id != actor.id:
        return [Reply("Ese aviso es de otra persona; no puedo responder por ella.", keyboard=_keyboard(account))]
    info = msg.confirmation_data or {}
    org = binding.organization_id

    if action == "cancel":
        await _resolve_pending_message(session, msg, "cancelled")
        msg.discarded_at = now
        msg.discard_reason = "confirmation_cancelled"
        emit(session, org_id=org, actor_id=actor.id, action="message.confirmation_cancelled",
             entity_type="channel_message", entity_id=msg.id,
             event_type="message.confirmation_cancelled", aggregate_version=None,
             payload={"confirmation": info.get("kind")})  # fmt: skip
        return [Reply("Listo, no registré ese aviso.", keyboard=_keyboard(account))]

    if action == "new":
        msg.confirmation_status = "awaiting_quantity"
        return [Reply(
            "Escribe la cantidad que falta (ej.: «5 cajas»). No se suma a la cantidad de otra persona.",
            keyboard=_keyboard(account),
        )]

    if action == "base":
        # La persona aclaró que la unidad genérica previa equivale a una unidad de la nueva
        # presentación («esa 1 unidad es 1 caja»). Ahora sí se puede sumar, sin inventar nada.
        if info.get("kind") != "same_author" or not info.get("needs_baseline_unit"):
            return [Reply("Ese botón no corresponde a este aviso.", keyboard=_keyboard(account))]
        baseline = Decimal(info["baseline_quantity"]) if info.get("baseline_quantity") else None
        declared = Decimal(info["declared_quantity"]) if info.get("declared_quantity") else None
        unit = info.get("declared_unit") or "unit"
        if baseline is None or declared is None:
            return [Reply("No pude aplicar esa cantidad. Revisa la lista.", keyboard=_keyboard(account))]
        new_total = baseline + declared
        proposal = {
            "operation": QuantityOp.ADD.value,
            "value": str(declared),
            "unit": unit,
            "reason": "confirmacion_telegram",
            "declared_quantity": str(new_total),
        }
        info.pop("needs_baseline_unit", None)
        info["declared_quantity"] = _fmt_qty(new_total)
        info["unit_mismatch"] = False
        msg.confirmation_data = info
        await _record_confirmed(
            session, actor, msg, info, declared_quantity=new_total, declared_unit=unit,
            quantity_proposal=proposal,
        )  # fmt: skip
        if can_apply_quantity(actor.roles):
            await _apply_or_propose(session, actor, info, QuantityOp.ADD, declared, unit)
        await _resolve_pending_message(session, msg, "resolved")
        return [Reply(
            f"Listo, actualicé tu solicitud a {_fmt_quantity(new_total, unit)}.",
            keyboard=_keyboard(account),
        )]

    operation = {"set": QuantityOp.SET, "add": QuantityOp.ADD}.get(action)
    if operation is not None:
        if info.get("kind") != "same_author":
            return [Reply("Ese botón no corresponde a este aviso.", keyboard=_keyboard(account))]
        if info.get("needs_baseline_unit"):
            return [Reply(
                "Antes de sumar necesito saber qué representa esa unidad inicial. "
                "Responde «es 1 caja» o escribe el total, por ejemplo: «En total 5 cajas».",
                keyboard=_keyboard(account),
            )]
        target = Decimal(info.get("target_quantity") or "0") if info.get("target_quantity") else None
        previous = (
            Decimal(info.get("previous_declared_quantity") or "0")
            if info.get("previous_declared_quantity")
            else None
        )
        baseline = (
            Decimal(info["baseline_quantity"])
            if info.get("baseline_quantity")
            else (target if target is not None else previous)
        )
        declared = Decimal(info["declared_quantity"]) if info.get("declared_quantity") else None
        unit = (
            info.get("declared_unit")
            or info.get("baseline_unit")
            or info.get("previous_declared_unit")
            or info.get("target_unit")
            or "unit"
        )
        if operation is QuantityOp.ADD and info.get("unit_mismatch"):
            # Unidades distintas: no se suman. Solo se puede aclarar la unidad genérica previa.
            if (info.get("baseline_unit") or "") != "unit" or declared is None:
                return [Reply(
                    "No puedo sumar unidades distintas. Escribe el total correcto, por "
                    "ejemplo: «En total 10 cajas», o «Cancelar».",
                    keyboard=_keyboard(account),
                )]
            info["needs_baseline_unit"] = True
            msg.confirmation_data = info
            return [Reply(
                f"Para sumar necesito saber qué representa esa "
                f"{_fmt_quantity(baseline, info.get('baseline_unit'))} inicial. "
                f"¿Es 1 {_UNIT_LABELS.get(unit, ('unidad', 'unidades'))[0]}? Responde "
                "«sí» o escribe el total, por ejemplo: «En total 10 cajas».",
                keyboard=_keyboard(account),
            )]
        if operation is QuantityOp.ADD:
            if declared is None or baseline is None or declared <= baseline:
                return [Reply("No hay una cantidad adicional que sumar.", keyboard=_keyboard(account))]
            value = declared - baseline
        else:
            if declared is None:
                return [Reply("No hay una cantidad que fijar.", keyboard=_keyboard(account))]
            value = declared
        proposal = None
        if not can_apply_quantity(actor.roles):
            proposal = {
                "operation": operation.value,
                "value": str(value),
                "unit": unit,
                "reason": "confirmacion_telegram",
                "declared_quantity": str(declared) if declared is not None else None,
            }
        await _record_confirmed(session, actor, msg, info, quantity_proposal=proposal)
        note = await _apply_or_propose(session, actor, info, operation, value, unit)
        await _resolve_pending_message(session, msg, "resolved")
        return [Reply(note, keyboard=_keyboard(account))]

    if action in ("ok", "log"):
        result = await _record_confirmed(session, actor, msg, info)
        await _resolve_pending_message(session, msg, "resolved")
        text = line_for(result, info["segment"])
        if info.get("kind") == "other_report":
            text += " Queda como tu reporte independiente: no se sumó a la cantidad de otra persona."
        return [Reply(text, keyboard=_keyboard(account))]

    return []


async def _awaiting_quantity_message(session, binding, actor: Actor) -> ChannelMessage | None:
    return (
        await session.execute(
            select(ChannelMessage)
            .where(
                ChannelMessage.channel_binding_id == binding.id,
                ChannelMessage.author_id == actor.id,
                ChannelMessage.confirmation_status == "awaiting_quantity",
            )
            .order_by(ChannelMessage.received_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _pending_same_author_message(
    session, binding, actor: Actor
) -> ChannelMessage | None:
    return (
        await session.execute(
            select(ChannelMessage)
            .where(
                ChannelMessage.channel_binding_id == binding.id,
                ChannelMessage.author_id == actor.id,
                ChannelMessage.confirmation_status == "pending_same_author_quantity",
            )
            .order_by(ChannelMessage.received_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _revise_pending_quantity(
    session, binding, actor: Actor, msg: ChannelMessage, text: str,
    account: AccountContext | None, event_id: str,
) -> Reply | None:  # fmt: skip
    """Corrige la cantidad solicitada por texto libre, antes o después de confirmar.

    No crea otra fila ni cambia la cantidad objetivo de un auxiliar. Si la operación es ambigua
    («5», sin «total» ni «más»), pregunta y no modifica nada hasta la confirmación.
    """
    info = dict(msg.confirmation_data or {})
    default_unit = (
        info.get("declared_unit") or info.get("baseline_unit")
        or info.get("previous_declared_unit") or info.get("target_unit") or "unit"
    )  # fmt: skip
    target = Decimal(info["target_quantity"]) if info.get("target_quantity") else None
    previous = (
        Decimal(info["previous_declared_quantity"])
        if info.get("previous_declared_quantity")
        else None
    )
    baseline = (
        Decimal(info["baseline_quantity"])
        if info.get("baseline_quantity")
        else (target if target is not None else previous)
    )
    baseline_unit = (
        info.get("baseline_unit") or info.get("previous_declared_unit")
        or info.get("target_unit") or "unit"
    )  # fmt: skip
    keyboard = _keyboard(account)
    item_name = None
    item = await session.get(DemandItem, uuid.UUID(info["same_item_id"]))
    if item is not None:
        item_name = item.display_name
    item_name = item_name or info.get("segment") or "el producto"

    if item is not None and await item_in_cart(session, item.organization_id, item):
        return Reply(
            f"{item_name} está en carrito: no cambié la cantidad solicitada. Si "
            "corresponde, pide a compras que lo retire del carrito.",
            keyboard=keyboard,
        )

    async def finish(
        new_total: Decimal, final_unit: str, operation: QuantityOp, proposal_value: Decimal
    ) -> Reply:
        """Registra la corrección como cantidad solicitada; la compra no se toca desde Telegram."""
        info["declared_quantity"] = _fmt_qty(new_total)
        info["declared_unit"] = final_unit
        info.pop("needs_baseline_unit", None)
        msg.confirmation_data = info
        proposal = {
            "operation": operation.value,
            "value": str(proposal_value),
            "unit": final_unit,
            "reason": "correccion_telegram",
            "declared_quantity": str(new_total),
        }
        await _record_confirmed(
            session, actor, msg, info, declared_quantity=new_total, declared_unit=final_unit,
            quantity_proposal=proposal, key_suffix=f":rev:{event_id}",
        )  # fmt: skip
        await _resolve_pending_message(session, msg, "resolved")
        return Reply(
            f"Listo, actualicé tu solicitud de {item_name} a "
            f"{_fmt_quantity(new_total, final_unit)}.",
            keyboard=keyboard,
        )

    parsed = _parse_quantity_revision(text, default_unit)
    if parsed is None:
        if info.get("needs_baseline_unit") and _is_affirmation_text(text):
            add_value = Decimal(info["declared_quantity"])
            add_unit = info.get("declared_unit") or "unit"
            return await finish(
                (baseline or Decimal(0)) + add_value, add_unit, QuantityOp.ADD, add_value
            )
        return None
    value, unit, op = parsed
    unit_one = _UNIT_LABELS.get(unit, ("unidad", "unidades"))[0]

    if info.get("needs_baseline_unit"):
        add_value = Decimal(info["declared_quantity"])
        add_unit = info.get("declared_unit") or "unit"
        if unit == add_unit and value == 1:
            return await finish(
                (baseline or Decimal(0)) + add_value, add_unit, QuantityOp.ADD, add_value
            )
        if op == "add" and unit != add_unit:
            plural = _UNIT_LABELS.get(add_unit, ("unidad", "unidades"))[1]
            return Reply(
                f"No puedo convertir {_fmt_quantity(value, unit)} a "
                f"{_fmt_quantity(add_value, add_unit)}: no conozco esa equivalencia. "
                f'Escribe el total correcto, por ejemplo: "En total {_fmt_qty(add_value)} '
                f'{plural}", o "Cancelar".',
                keyboard=keyboard,
            )
        # Cualquier otra cifra se toma como el total corregido, nunca como una suma.
        return await finish(value, unit, QuantityOp.SET, value)

    if op is None:
        if baseline is None or (unit == baseline_unit and value == baseline):
            op = "set"
        else:
            # Sin «más» ni «total» no se puede decidir: se pregunta por texto y no se toca nada.
            info["declared_quantity"] = _fmt_qty(value)
            info["declared_unit"] = unit
            info["unit_mismatch"] = unit != baseline_unit
            msg.confirmation_data = info
            return Reply(
                f"Tienes {_fmt_quantity(baseline, baseline_unit)} de {item_name} y escribiste "
                f"{_fmt_quantity(value, unit)}. ¿Quieres agregar esa cantidad o cambiar el total? "
                f"{_CORRECTION_HELP}",
                keyboard=keyboard,
            )

    if op == "add" and baseline is None:
        op = "set"
    if op == "add":
        assert baseline is not None
        if unit != baseline_unit:
            plural = _UNIT_LABELS.get(unit, ("unidad", "unidades"))[1]
            if baseline_unit == "unit":
                # La unidad inicial es genérica: antes de sumar hay que saber qué representa.
                info["declared_quantity"] = _fmt_qty(value)
                info["declared_unit"] = unit
                info["needs_baseline_unit"] = True
                msg.confirmation_data = info
                return Reply(
                    f"Para sumar necesito saber qué representa esa 1 unidad inicial de "
                    f"{item_name}. ¿Es 1 {unit_one}? Responde «sí, es 1 {unit_one}» o escribe "
                    f'el total, por ejemplo: "En total {_fmt_qty(value)} {plural}".',
                    keyboard=keyboard,
                )
            from_label = _UNIT_LABELS.get(
                baseline_unit, ("unidad", "unidades")
            )[1]
            return Reply(
                f"No puedo sumar {from_label} con {plural}. Tienes "
                f"{_fmt_quantity(baseline, baseline_unit)} de {item_name}; escribe el total "
                f'correcto, por ejemplo: "En total {_fmt_qty(value)} {plural}", o "Cancelar".',
                keyboard=keyboard,
            )
        return await finish(baseline + value, unit, QuantityOp.ADD, value)
    return await finish(value, unit, QuantityOp.SET, value)


async def _apply_new_quantity(
    session, binding, actor: Actor, msg: ChannelMessage, text: str, now: datetime,
    cfg: ChannelConfig, account: AccountContext | None,
) -> Reply:  # fmt: skip
    info = msg.confirmation_data or {}
    default_unit = info.get("declared_unit") or "unit"
    parsed = _parse_quantity_text(text, default_unit)
    if parsed is None:
        return Reply(
            "No entendí la cantidad. Escríbela como «5 cajas», «10 unidades» o «3 paquetes».",
            keyboard=_keyboard(account),
        )
    value, unit = parsed
    result = await _record_confirmed(
        session, actor, msg, info, declared_quantity=value, declared_unit=unit,
        key_suffix=":new",
    )  # fmt: skip
    await _resolve_pending_message(session, msg, "resolved")
    return Reply(
        line_for(result, info.get("segment") or text)
        + " Queda como tu reporte independiente; la cantidad declarada no se suma a la de otra persona.",
        keyboard=_keyboard(account),
    )


# ------------------------------------------------------------------ eventos


async def handle_event(session: AsyncSession, binding: ChannelBinding, ev: ChannelEvent,
                       inbound: InboundEvent, now: datetime, cfg: ChannelConfig,
                       account: AccountContext | None = None) -> list[Reply]:  # fmt: skip
    await advisory_lock(session, f"chan:{binding.id}")  # un evento por chat a la vez
    org = binding.organization_id
    personal = _is_personal(account)
    keyboard = _keyboard(account)
    ack = [Reply("", answer_callback_id=ev.callback_id)] if ev.callback_id else []

    if account is not None and not personal:
        # Cero auxiliares habilitados: esta cuenta no registra nada y no guarda mensajes
        # pendientes que después podrían atribuirse por error. Otras cuentas no se ven afectadas.
        if not await sessions.list_account_reporters(session, org, account.account_id):
            return ack + [Reply(_EMPTY_ACCOUNT, keyboard=keyboard)]

    if ev.kind == "edited":
        return [
            Reply(
                "Vi que editaste un mensaje; no cambié nada. Si era otro producto, envíalo de nuevo.",
                keyboard=keyboard,
            )
        ]
    if ev.kind == "other":
        return [Reply("Por ahora solo entiendo texto y fotos.", keyboard=keyboard)]

    if ev.kind == "callback":
        return ack + await _on_callback(session, binding, ev, inbound, now, cfg, account)

    text = (ev.text or "").strip()
    low = _command_text(text)
    if ev.kind == "start" or low in ("/start",):
        sess, _actor, err = await _resolve_identity(
            session, binding, account, now, cfg.idle, event_id=inbound.id
        )
        if sess is None:
            if personal:
                return [Reply(f"Hola. {_identity_error_text(err)}", keyboard=keyboard)]
            auxs = await _selectable(session, binding, account)
            if not auxs:
                return [_selector(auxs, "Hola.", keyboard=keyboard)]
            return [
                Reply(
                    f"Hola. {_identity_error_text(err)} ¿Quién va a reportar ahora?",
                    _selector_buttons(auxs),
                    keyboard,
                )
            ]
        auxs = [] if personal else await _selectable(session, binding, account)
        return [await _whoami(session, sess, cfg, prefix="Hola de nuevo. ", personal=personal,
                              automatic=len(auxs) == 1)]  # fmt: skip
    if ev.kind == "text":
        if low in CMD_LIST:
            return [await _list_reply(session, org, 0, cfg, keyboard)]
        if low in CMD_CHANGE:
            if personal:
                return [
                    Reply(
                        f"Esta cuenta está vinculada a {account.display_label}: la identidad se fija "
                        "automáticamente y no se cambia desde aquí. Para elegir otra persona, usa el "
                        "teléfono compartido.",
                        keyboard=keyboard,
                    )
                ]
            auxs = await _selectable(session, binding, account)
            if len(auxs) == 1:
                return [
                    Reply(
                        f"Este teléfono tiene un solo auxiliar habilitado: {auxs[0].display_name}. "
                        "La identidad se identifica automáticamente; no hay nada que elegir.",
                        keyboard=keyboard,
                    )
                ]
            # Se informa quién reporta ahora (si sigue habilitado en esta cuenta) para poder
            # volver a elegir a la misma persona sin advertencias ni bloqueos.
            active = await sessions.active_session(session, binding, now, cfg.idle)
            current = None
            if active is not None and any(u.id == active.auxiliary_id for u in auxs):
                current_user = await session.get(AppUser, active.auxiliary_id)
                current = current_user.display_name if current_user is not None else None
            return [_selector(auxs, "¿Quién va a reportar ahora?", current=current,
                              keyboard=keyboard)]  # fmt: skip
        if low in CMD_WHO:
            sess, _actor, err = await _resolve_identity(
                session, binding, account, now, cfg.idle, event_id=inbound.id
            )
            if sess is None:
                return [Reply(_identity_error_text(err), keyboard=keyboard)]
            auxs = [] if personal else await _selectable(session, binding, account)
            return [await _whoami(session, sess, cfg, personal=personal,
                                  automatic=len(auxs) == 1)]  # fmt: skip
        if low in CMD_HELP:
            return [Reply(_HELP_PERSONAL if personal else _HELP, keyboard=keyboard)]
        if low in CMD_LOGOUT:
            if personal:
                return [
                    Reply(
                        "Esta cuenta autorizada se identifica automáticamente: no queda sin identidad. "
                        "Si no quieres reportar, simplemente no envíes mensajes.",
                        keyboard=keyboard,
                    )
                ]
            done = await sessions.end_session(session, binding, now)
            auxs = await _selectable(session, binding, account)
            if len(auxs) == 1:
                text = (
                    "Reportes finalizados: nadie queda identificado. Este teléfono tiene un solo "
                    "auxiliar habilitado, así que tu próximo mensaje se identifica automáticamente."
                )
            else:
                text = (
                    "Reportes finalizados: nadie queda identificado. Para volver a reportar, elige "
                    "tu nombre en «Cambiar auxiliar»."
                    if done
                    else "No había una sesión activa."
                )
            return [Reply(text, keyboard=keyboard)]
        if _is_command_text(text):
            # Un comando desconocido NUNCA es el nombre de un producto: no se guarda pendiente
            # ni se convierte en observación. Se orienta a las acciones que sí existen.
            return [Reply(
                "No conozco ese comando. Escribe el producto que falta o usa "
                "«📋 Ver listado de Faltantes».",
                keyboard=keyboard,
            )]

    sess, actor, err = await _resolve_identity(
        session, binding, account, now, cfg.idle, event_id=inbound.id
    )

    if ev.kind == "photo":
        return await _on_photo(session, binding, ev, inbound, sess, actor, now, cfg, account, err)

    if sess is None or actor is None:
        msg = await _store_message(
            session, binding, inbound, ev, kind="text", text=text,
            status="pending_attribution", account_id=_account_id(account), now=now,
        )  # fmt: skip
        emit(session, org_id=org, actor_id=None, action="message.pending_attribution", entity_type="inbound_event",
             entity_id=inbound.id, event_type="message.pending_attribution", aggregate_version=None,
             payload={"binding_id": str(binding.id),
                      "account_id": str(_account_id(account)) if account else None})  # fmt: skip
        if personal:
            return [Reply(
                f"Guardé tu mensaje, pero no lo anoto: {_identity_error_text(err)}",
                buttons=[[Button(f"Anotar a nombre de {account.display_label}", "att:ok"),
                          Button("Descartar", "att:no")]],
                keyboard=keyboard,
            )]  # fmt: skip
        return [_selector(await _selectable(session, binding, account),
                          "Guardé tu mensaje, pero no lo anoto a nombre de nadie hasta saber quién eres. ¿Quién eres?",
                          keyboard=keyboard)]  # fmt: skip

    intent = classify_intent(text)
    await sessions.touch(session, sess, now, cfg.idle)
    basis = "authorized_account" if personal else "session_at_receipt"
    msg = await _store_message(session, binding, inbound, ev, kind="text", text=text, status="attributed",
                               author=actor, sess=sess, basis=basis,
                               account_id=_account_id(account), now=now)  # fmt: skip
    waiting = await _awaiting_quantity_message(session, binding, actor)
    if waiting is not None:
        return [
            await _apply_new_quantity(session, binding, actor, waiting, text, now, cfg, account)
        ]
    # Corrección por texto libre de una cantidad que está esperando confirmación. Solo se toma
    # un texto que exprese SOLO una cantidad; un producto nuevo sigue su flujo normal.
    revising = await _pending_same_author_message(session, binding, actor)
    if revising is not None and _is_cancel_text(text):
        await _resolve_pending_message(session, revising, "cancelled")
        return [Reply("Cancelado: no cambié la cantidad solicitada.", keyboard=keyboard)]
    if revising is not None and _is_quantity_revision_text(text):
        revised = await _revise_pending_quantity(
            session, binding, actor, revising, text, account, ev.event_id
        )
        if revised is not None:
            return [revised]
    if intent is Intent.CHITCHAT or intent is Intent.EMPTY:
        return [
            Reply(
                f"Hola, {actor.display_name}. Escribe lo que falta (ej.: «falta ibuprofeno 400 mg») o envía una foto del envase.",
                keyboard=keyboard,
            )
        ]
    if intent is Intent.QUESTION:
        return [
            Reply(
                "No registro preguntas. Para anotar un faltante escribe el producto; para consultar, usa «📋 Ver listado de Faltantes».",
                keyboard=keyboard,
            )
        ]
    lines, pending = await _record_segments(
        session, binding, actor, sess, msg, text, f"tg:{binding.id}:{ev.event_id}", cfg,
        basis_level="account" if personal else "declared", account_id=_account_id(account),
        branch_label=_branch(account),
    )  # fmt: skip
    replies: list[Reply] = []
    if lines:
        replies.append(
            Reply("\n".join(lines) + f"\n{_footer(actor.display_name, personal)}", keyboard=keyboard)
        )
    if pending:
        replies.append(
            await _ask_confirmation(
                session, binding, actor, msg, pending[0], personal, keyboard
            )
        )
    return replies or [Reply("No pude interpretar el mensaje.", keyboard=keyboard)]


def _selector_buttons(auxs: list[AppUser]) -> list[list[Button]]:
    rows, row = [], []
    for u in auxs:
        row.append(Button(u.display_name, f"sel:{u.id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


_HELP = (
    "Escribe lo que falta («falta ibuprofeno 400 mg») o envía una foto del envase. "
    "«📋 Ver listado de Faltantes» muestra todos los faltantes activos de la farmacia. "
    "«👤 Cambiar auxiliar» dice quién está reportando y permite elegirlo. "
    "«❓ Ayuda» vuelve a mostrar este texto."
)
_HELP_PERSONAL = (
    "Escribe lo que falta («falta ibuprofeno 400 mg») o envía una foto del envase. "
    "«📋 Ver listado de Faltantes» muestra todos los faltantes activos de la farmacia. "
    "Esta cuenta está vinculada a una persona: no necesitas elegir identidad en cada mensaje."
)


async def _whoami(
    session, sess: ActorSession | None, cfg: ChannelConfig, prefix: str = "", personal: bool = False,
    automatic: bool = False,
) -> Reply:  # fmt: skip
    if sess is None:
        return Reply(
            "Nadie ha elegido identidad todavía (o la sesión expiró). Usa «Cambiar auxiliar».",
            keyboard=KEYBOARD_PERSONAL if personal else KEYBOARD,
        )
    u = await session.get(AppUser, sess.auxiliary_id)
    if personal:
        return Reply(
            f"{prefix}Estás reportando como {u.display_name} (cuenta de Telegram autorizada; "
            "la identidad se fija automáticamente).",
            keyboard=KEYBOARD_PERSONAL,
        )
    if automatic:
        return Reply(
            f"{prefix}Estás reportando como {u.display_name} (identificación automática: este "
            "teléfono tiene un solo auxiliar habilitado).",
            keyboard=KEYBOARD_SINGLE,
        )
    mins = int(cfg.idle.total_seconds() // 60)
    return Reply(f"{prefix}Estás reportando como {u.display_name} (identidad declarada; "
                 f"expira tras {mins} min sin uso).", keyboard=KEYBOARD)  # fmt: skip


async def _list_reply(
    session, org, page: int, cfg: ChannelConfig, keyboard: list[list[str]] = KEYBOARD
) -> Reply:
    items, agg, more, total, pending = await queries.list_items_page(
        session, org, page=page, size=cfg.page_size
    )
    if not total:
        return Reply("La lista de faltantes está vacía.", keyboard=keyboard)
    header = f"📋 Faltantes activos ({total})"
    if pending:
        header += f" · {_avisos(pending)} por revisar"
    declared = await queries.latest_declared_quantities(session, org, [i.id for i in items])
    reporters = await queries.reporter_names(session, org, items)
    cov = await coverage_svc.coverage_for_items(session, org, items)
    blocks: list[str] = []
    for n, i in enumerate(items, start=page * cfg.page_size + 1):
        cnt = agg.get(i.id, (0, None, []))[0]
        if i.target_quantity is not None:
            qty = _fmt_quantity(i.target_quantity, i.quantity_unit)
        elif declared.get(i.id):
            value, unit = declared[i.id]
            qty = _fmt_quantity(value, unit)
        else:
            qty = "Cantidad no indicada"
        # Estado real de reposición (contrato §12.7.4): lo cotizado en un carrito activo está
        # «En carrito»; lo demás sigue «Pendiente de compra». No cierra nada ni cambia D-139.
        # Es la MISMA condición que ya define el estado: el tachado es solo su representación.
        in_cart = cov.get(i.id, {}).get("replenishment_state") == "in_quote"
        state = "🛒 En carrito" if in_cart else "Pendiente de compra"
        # Quién reportó esta fila (D-135): cada persona tiene su propio faltante. Se muestra
        # siempre, no solo cuando las observaciones traen varios autores.
        reporter = reporters.get(i.id) or "auxiliar no registrado"
        # Nombre en negrita real de Telegram (parse_mode HTML); todo lo dinámico se escapa.
        # Solo el nombre se tacha cuando el faltante está en un carrito activo.
        name = html.escape(_one_line(i.display_name), quote=False)
        name_html = f"<s><b>{name}</b></s>" if in_cart else f"<b>{name}</b>"
        blocks.append(
            f"{n}. {name_html}\n"
            f"   Cantidad: {html.escape(_one_line(qty), quote=False)}\n"
            f"   Reportado por: {html.escape(_one_line(reporter), quote=False)}\n"
            f"   Estado: {html.escape(_one_line(state), quote=False)}\n"
            f"   {_avisos(cnt)}"
        )
    buttons = [[Button("Ver más", f"lst:{page + 1}")]] if more else []
    return Reply("\n\n".join([header, *blocks]), buttons, keyboard, parse_mode="HTML")


async def _on_callback(session, binding, ev, inbound, now, cfg,
                       account: AccountContext | None = None) -> list[Reply]:  # fmt: skip
    org = binding.organization_id
    personal = _is_personal(account)
    keyboard = _keyboard(account)
    data = ev.callback_data or ""
    if data.startswith("lst:"):
        try:
            page = max(0, int(data[4:]))
        except ValueError:
            return []
        return [await _list_reply(session, org, page, cfg, keyboard)]
    if data.startswith("sel:"):
        if personal:
            return [
                Reply(
                    "Esta cuenta está vinculada a una persona: la identidad no se cambia desde aquí.",
                    keyboard=keyboard,
                )
            ]
        try:
            uid = uuid.UUID(data[4:])
            sess = await sessions.select_auxiliary(
                session, binding, uid, now, cfg.idle, event_id=inbound.id,
                account_id=_account_id(account),
            )  # fmt: skip
        except (ValueError, DomainError) as e:
            # Botón antiguo o persona que ya no está disponible: se explica y se muestran los botones VIGENTES.
            return [
                Reply(
                    "No pude cambiar la identidad: "
                    + getattr(e, "message", "dato inválido")
                    + " Elige a alguien de la lista actual.",
                    keyboard=keyboard,
                ),
                _selector(
                    await _selectable(session, binding, account),
                    "¿Quién va a reportar ahora?",
                    keyboard=keyboard,
                ),
            ]
        u = await session.get(AppUser, sess.auxiliary_id)
        # Confirmación sencilla e idéntica elija al mismo auxiliar o a otro.
        replies = [Reply(f"✅ Reportando como {u.display_name}.", keyboard=keyboard)]
        pend = await _pending_messages(session, binding)
        fresh = [m for m in pend if m.received_at >= now - cfg.pending_max_age]
        # Un comando pendiente (p. ej. `/faltantes` guardado por una versión anterior) no es un
        # reporte: al identificarse se resuelve como consulta, sin preguntar si se anota.
        queries = [m for m in fresh if m.kind == "text" and _is_command_text(m.text or "")]
        show_list = False
        for m in queries:
            show_list = (
                _discard_pending_command(session, org, m, sess.auxiliary_id, now) or show_list
            )
        if queries:
            ids = {m.id for m in queries}
            fresh = [m for m in fresh if m.id not in ids]
            if show_list:
                replies.append(await _list_reply(session, org, 0, cfg, keyboard))
        if fresh:
            note = f"Tienes {len(fresh)} mensaje(s) recibido(s) antes de identificarte."
            replies.append(Reply(f"{note} ¿Los anoto a nombre de {u.display_name}?",
                                 [[Button(f"Anotar a nombre de {u.display_name}", "att:ok"), Button("Descartar", "att:no")]],
                                 keyboard))  # fmt: skip
        return replies
    if data.startswith("rp:"):
        return await _on_report_confirmation(session, binding, ev, data, now, cfg, account)
    if data in ("att:ok", "att:no"):
        return await _on_pending_decision(
            session, binding, inbound, data == "att:ok", now, cfg, account
        )
    return []


async def _on_pending_decision(session, binding, inbound, accept: bool, now, cfg,
                               account: AccountContext | None = None) -> list[Reply]:  # fmt: skip
    personal = _is_personal(account)
    keyboard = _keyboard(account)
    sess, actor, err = await _resolve_identity(
        session, binding, account, now, cfg.idle, event_id=inbound.id
    )
    if sess is None or actor is None:
        if personal:
            return [Reply(_identity_error_text(err), keyboard=keyboard)]
        return [_selector(await _selectable(session, binding, account),
                          "Primero necesito saber quién eres.", keyboard=keyboard)]  # fmt: skip
    org = binding.organization_id
    pend = await _pending_messages(session, binding)
    if not pend:
        return [Reply("No hay mensajes pendientes.", keyboard=keyboard)]
    lines: list[str] = []
    hold: tuple[ChannelMessage, dict] | None = None
    show_list = False
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
        if m.kind == "text" and _is_command_text(m.text or ""):
            # Confirmar la identidad no convierte un comando en reporte: se descarta como comando
            # y, si era el listado, se ejecuta la consulta que la persona pidió.
            show_list = _discard_pending_command(session, org, m, actor.id, now) or show_list
            continue
        # La persona confirmó explícitamente: queda atribuida. En una cuenta personal la base es
        # `authorized_account` (la cuenta ya fijaba la identidad); en el teléfono compartido,
        # `confirmed_after_identification`.
        m.attribution_status, m.author_id, m.session_id = "attributed", actor.id, sess.id
        m.attribution_basis = "authorized_account" if personal else "confirmed_after_identification"
        m.attributed_at = now
        if m.account_id is None:
            m.account_id = _account_id(account)
        emit(session, org_id=org, actor_id=actor.id, action="message.attributed", entity_type="channel_message",
             entity_id=m.id, event_type="message.attributed", aggregate_version=None,
             payload={"basis": m.attribution_basis, "session_id": str(sess.id),
                      "account_id": str(m.account_id) if m.account_id else None})  # fmt: skip
        orig = (await session.get(InboundEvent, m.inbound_event_id)).external_event_id
        if m.kind == "text":
            seg_lines, seg_pending = await _record_segments(
                session, binding, actor, sess, m, m.text or "", f"tg:{binding.id}:{orig}", cfg,
                basis_level="account" if personal else "declared", account_id=m.account_id,
                branch_label=_branch(account),
            )  # fmt: skip
            lines += seg_lines
            if seg_pending:
                hold = (m, seg_pending[0])
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
        return [Reply("Descartados. No se anotó nada.", keyboard=keyboard)]
    if stale:
        lines.append(
            f"{stale} mensaje(s) eran de hace demasiado rato y no se pueden atribuir; envíalos de nuevo."
        )
    replies: list[Reply] = []
    if show_list:
        replies.append(await _list_reply(session, org, 0, cfg, keyboard))
    if lines or hold is not None or not show_list:
        replies.append(
            Reply(
                "\n".join(lines or ["Listo."]) + f"\n{_footer(actor.display_name, personal)}",
                keyboard=keyboard,
            )
        )
    if hold is not None:
        hold_msg, hold_pending = hold
        replies.append(
            await _ask_confirmation(
                session, binding, actor, hold_msg, hold_pending, personal, keyboard
            )
        )
    return replies


async def _on_photo(session, binding, ev, inbound, sess, actor, now, cfg,
                    account: AccountContext | None = None,
                    identity_error: DomainError | None = None) -> list[Reply]:  # fmt: skip
    org = binding.organization_id
    personal = _is_personal(account)
    keyboard = _keyboard(account)
    ph = ev.photo
    if ph is None:
        return [Reply("No pude leer la foto.", keyboard=keyboard)]
    attributed = sess is not None and actor is not None
    basis = ("authorized_account" if personal else "session_at_receipt") if attributed else None
    msg = await _store_message(session, binding, inbound, ev, kind="photo", text=ev.text,
                               status="attributed" if attributed else "pending_attribution",
                               author=actor if attributed else None, sess=sess if attributed else None,
                               basis=basis, account_id=_account_id(account), now=now)  # fmt: skip
    session.add(Attachment(
        organization_id=org, channel_message_id=msg.id, author_id=actor.id if attributed else None,
        provider_file_id=ph.file_id, provider_file_unique_id=ph.file_unique_id, width=ph.width,
        height=ph.height, file_size=ph.file_size, caption=ev.text, status="pending",
        data_origin=origin_for(await org_kind(session, org)),  # el archivo es captura (o simulación)
    ))  # fmt: skip
    await session.flush()
    emit(session, org_id=org, actor_id=actor.id if attributed else None, action="photo.received",
         entity_type="channel_message", entity_id=msg.id, event_type="photo.received", aggregate_version=None,
         payload={"attributed": attributed, "has_caption": bool(ev.text),
                  "account_id": str(_account_id(account)) if account else None})  # fmt: skip
    if not attributed:
        if personal:
            return [Reply(
                f"Guardé la foto, pero no la anoto: {_identity_error_text(identity_error)}",
                buttons=[[Button(f"Anotar a nombre de {account.display_label}", "att:ok"),
                          Button("Descartar", "att:no")]],
                keyboard=keyboard,
            )]  # fmt: skip
        return [_selector(await _selectable(session, binding, account),
                          "Guardé la foto, pero no la anoto a nombre de nadie hasta saber quién eres. ¿Quién eres?",
                          keyboard=keyboard)]  # fmt: skip
    await sessions.touch(session, sess, now, cfg.idle)
    base = (f"Recibí la foto, {actor.display_name}. " + ("La estoy revisando y te aviso." if cfg.vision_enabled
            else "La guardé; todavía no la leo automáticamente, queda para revisión de compras."))  # fmt: skip
    return [Reply(base + f"\n{_footer(actor.display_name, personal)}", keyboard=keyboard)]


# ------------------------------------------------------------------ salida


# La Bot API acepta 4096 caracteres por mensaje; se deja margen para no rozar el límite.
TELEGRAM_TEXT_LIMIT = 4000


def _split_telegram_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Parte un texto largo por líneas para respetar el límite de la Bot API.

    Los trozos conservan el orden y no cortan líneas cortas; una línea que por sí sola supera el
    límite se corta en seco (caso extremo, no ocurre con las listas paginadas).
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    chunks.append(current)
    return chunks


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
            # Telegram limita cada mensaje a 4096 caracteres: se parte por líneas y los botones
            # y el teclado viajan en el último trozo, para no perder la navegación.
            chunks = _split_telegram_text(r.text)
            for idx, chunk in enumerate(chunks):
                last = idx == len(chunks) - 1
                session.add(OutboundMessage(
                    organization_id=binding.organization_id, channel_binding_id=binding.id,
                    inbound_event_id=inbound_event_id, seq=seq, method="sendMessage",
                    payload={
                        "text": chunk,
                        "buttons": [[{"text": b.text, "data": b.data} for b in row] for row in r.buttons]
                        if last else [],
                        "keyboard": r.keyboard if last else None,
                        "parse_mode": r.parse_mode,
                    },
                ))  # fmt: skip
                seq += 1
    await session.flush()
    return seq
