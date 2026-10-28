"""Actualizaciones de Telegram → eventos de canal normalizados. Sin red ni base de datos."""

from datetime import UTC, datetime

from app.services.channel import ChannelEvent, PhotoRef

PROVIDER = "telegram"


def _dt(ts) -> datetime | None:
    return datetime.fromtimestamp(ts, UTC) if isinstance(ts, int | float) else None


def chat_id_of(update: dict) -> str | None:
    """ID del chat o None si la actualización es de otra forma o está malformada."""
    for k in ("message", "edited_message"):
        chat = (update.get(k) or {}).get("chat") if isinstance(update.get(k), dict) else None
        if isinstance(chat, dict) and "id" in chat:
            return str(chat["id"])
    cb = update.get("callback_query")
    if isinstance(cb, dict) and isinstance(cb.get("message"), dict):
        chat = cb["message"].get("chat")
        if isinstance(chat, dict) and "id" in chat:
            return str(chat["id"])
    return None


def parse_update(update: dict) -> ChannelEvent | None:
    """None = actualización que no nos interesa (se ignora sin ruido)."""
    uid = update.get("update_id")
    chat = chat_id_of(update)
    if uid is None or chat is None:
        return None
    eid = str(uid)
    if "edited_message" in update:
        return ChannelEvent(PROVIDER, eid, chat, "edited")
    if "callback_query" in update:
        cb = update["callback_query"]
        return ChannelEvent(
            PROVIDER, eid, chat, "callback", callback_data=cb.get("data"), callback_id=str(cb["id"])
        )
    m = update["message"]
    mid, sent = str(m.get("message_id")), _dt(m.get("date"))
    if m.get("photo"):
        best = max(m["photo"], key=lambda p: (p.get("width") or 0) * (p.get("height") or 0))
        ref = PhotoRef(
            best["file_id"],
            best.get("file_unique_id"),
            best.get("width"),
            best.get("height"),
            best.get("file_size"),
        )
        return ChannelEvent(
            PROVIDER,
            eid,
            chat,
            "photo",
            text=m.get("caption"),
            message_id=mid,
            sent_at=sent,
            photo=ref,
        )
    text = m.get("text")
    if text is None:
        return ChannelEvent(PROVIDER, eid, chat, "other", message_id=mid, sent_at=sent)
    kind = "start" if text.split("@")[0].split()[0].lower() == "/start" else "text"
    return ChannelEvent(PROVIDER, eid, chat, kind, text=text, message_id=mid, sent_at=sent)


def redact(update: dict) -> dict:
    """Lo que se guarda del evento: misma forma re-procesable, sin nombres ni datos de perfil."""
    out: dict = {"update_id": update.get("update_id")}
    for k in ("message", "edited_message"):
        if k in update:
            m = update[k]
            slim = {"message_id": m.get("message_id"), "date": m.get("date"),
                    "chat": {"id": m["chat"]["id"], "type": m["chat"].get("type")},
                    "from": {"id": (m.get("from") or {}).get("id")}}  # fmt: skip
            for f in ("text", "caption"):
                if f in m:
                    slim[f] = m[f]
            if m.get("photo"):
                slim["photo"] = [{k2: p.get(k2) for k2 in ("file_id", "file_unique_id", "width", "height", "file_size")}
                                 for p in m["photo"]]  # fmt: skip
            out[k] = slim
    if "callback_query" in update:
        cb = update["callback_query"]
        out["callback_query"] = {"id": cb.get("id"), "data": cb.get("data"), "from": {"id": (cb.get("from") or {}).get("id")},
                                 "message": {"chat": {"id": cb["message"]["chat"]["id"]}} if cb.get("message") else None}  # fmt: skip
    return out
