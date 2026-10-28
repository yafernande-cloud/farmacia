"""Adaptador de Telegram: parseo, cliente, entrega con reintentos, webhook y polling.

Telegram está SIMULADO (tests/tgsim.py). Se verifica nuestra lógica y el formato de las llamadas a la
Bot API; NO se ha probado contra el servicio real (falta el token del bot).
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.main import create_app
from app.models import DemandObservation, InboundEvent, OutboundMessage
from app.telegram.client import TelegramClient, TelegramError, render
from app.telegram.handler import deliver_outbound, handle_update, retry_failed_events
from app.telegram.poller import poll_once
from app.telegram.types import parse_update, redact
from tests.conftest import CHAT
from tests.test_h1b_channel import T0, Chat, count, mins
from tests.tgsim import TOKEN, callback_update, edited_update, photo_update, text_update

# ---------- puras (sin base de datos) ----------


def test_parseo_de_actualizaciones_de_telegram():
    t = parse_update(text_update(1, CHAT, "falta ibuprofeno"))
    assert (t.kind, t.text, t.chat_id, t.event_id) == ("text", "falta ibuprofeno", CHAT, "1")
    assert parse_update(text_update(2, CHAT, "/start@mi_bot")).kind == "start"
    p = parse_update(photo_update(3, CHAT, "F", caption="faltan 2"))
    assert (
        p.kind == "photo"
        and p.photo.file_id == "F"
        and p.photo.width == 1280
        and p.text == "faltan 2"
    )  # la más grande
    assert parse_update(callback_update(4, CHAT, "sel:x", "c9")).callback_id == "c9"
    assert parse_update(edited_update(5, CHAT, "x")).kind == "edited"
    sticker = {
        "update_id": 6,
        "message": {
            "message_id": 1,
            "date": 1,
            "chat": {"id": 1, "type": "private"},
            "sticker": {},
        },
    }
    assert parse_update(sticker).kind == "other"
    grupo = text_update(7, -1001234, "hola")
    assert parse_update(grupo).chat_id == "-1001234"
    assert (
        parse_update({"update_id": 8, "poll": {}}) is None and parse_update({"message": {}}) is None
    )


def test_lo_que_se_guarda_del_evento_no_incluye_nombres_de_perfil_y_se_puede_reprocesar():
    u = text_update(1, CHAT, "falta ibuprofeno")
    r = redact(u)
    assert "Telefono" not in str(r) and "first_name" not in str(r)
    assert parse_update(r).text == "falta ibuprofeno"  # misma forma: reintentable desde la base


def test_render_teclados():
    m, b = render(
        "sendMessage",
        {
            "text": "hola",
            "buttons": [[{"text": "Ana", "data": "sel:1"}]],
            "keyboard": [["Ver lista"]],
        },
        CHAT,
    )
    assert b["reply_markup"] == {"inline_keyboard": [[{"text": "Ana", "callback_data": "sel:1"}]]}
    _, b2 = render(
        "sendMessage",
        {"text": "hola", "buttons": [], "keyboard": [["Ver lista", "Cambiar auxiliar"]]},
        CHAT,
    )
    assert (
        b2["reply_markup"]["keyboard"][0][1] == {"text": "Cambiar auxiliar"}
        and b2["reply_markup"]["is_persistent"]
    )
    assert render("answerCallbackQuery", {"callback_query_id": "z"}, CHAT) == (
        "answerCallbackQuery",
        {"callback_query_id": "z"},
    )


async def test_el_token_nunca_aparece_en_repr_logs_ni_errores(tg_api, caplog):
    caplog.set_level(logging.DEBUG)
    c = TelegramClient(TOKEN, "http://tg.test", transport=tg_api.transport)
    assert TOKEN not in repr(c) and TOKEN not in str(c)
    await c.call("sendMessage", {"chat_id": 1, "text": "x"})
    tg_api.failures.append((403, "Forbidden: bot was blocked", None))
    with pytest.raises(TelegramError) as e:
        await c.call("sendMessage", {"chat_id": 1, "text": "x"})
    assert TOKEN not in str(e.value) and e.value.permanent
    assert TOKEN not in caplog.text  # httpx registra la URL a nivel INFO: debe estar silenciado
    assert logging.getLogger("httpx").level >= logging.WARNING


async def test_error_de_red_no_filtra_la_url(tg_api):
    import httpx

    def boom(req):
        raise httpx.ConnectError("fallo conectando a https://api.telegram.org/bot" + TOKEN)

    c = TelegramClient(TOKEN, "http://tg.test", transport=httpx.MockTransport(boom))
    with pytest.raises(TelegramError) as e:
        await c.call("getMe")
    assert TOKEN not in str(e.value) and TOKEN not in repr(e.value.args)


# ---------- con PostgreSQL real ----------

db = pytest.mark.db


@db
async def test_la_respuesta_falla_pero_el_reporte_queda_guardado_y_se_reintenta(
    make_rt, seed, tg_api, chan, sessions
):
    rt = make_rt()
    c = Chat(rt, seed, tg_api)
    await c.pick("ana")
    tg_api.method_failures["sendMessage"] = [(429, "Too Many Requests", 7)]
    res = await c.text("falta ibuprofeno 400 mg", now=T0)  # el envío de la confirmación falla
    assert (
        res.status == "processed" and await count(sessions, DemandObservation) == 1
    )  # el reporte NO se pierde
    async with sessions() as s:
        out = (
            (await s.execute(select(OutboundMessage).where(OutboundMessage.status == "pending")))
            .scalars()
            .all()
        )
    assert (
        len(out) == 1 and out[0].attempts == 1 and out[0].next_attempt_at > T0
    )  # espera lo que pidió Telegram (7 s)
    assert await deliver_outbound(rt, now=T0) == 0  # aún no toca
    assert (
        await deliver_outbound(rt, now=datetime.now(UTC) + timedelta(minutes=1)) == 1
    )  # pasada la espera
    assert (
        "Anotado" in tg_api.texts()[-1] and await count(sessions, DemandObservation) == 1
    )  # sin duplicar


@db
async def test_error_permanente_de_envio_se_marca_fallido_sin_reintentar(
    make_rt, seed, tg_api, chan, sessions
):
    rt = make_rt()
    c = Chat(rt, seed, tg_api)
    tg_api.method_failures["sendMessage"] = [(403, "Forbidden: bot was blocked by the user", None)]
    await c.text("/start", now=T0)
    async with sessions() as s:
        st = (await s.execute(select(OutboundMessage.status, OutboundMessage.last_error))).all()
    assert st[0][0] == "failed" and st[0][1].startswith("403")
    before = len(tg_api.sent())
    await deliver_outbound(rt, now=mins(5))
    assert len(tg_api.sent()) == before


@db
async def test_las_respuestas_se_entregan_en_orden(make_rt, seed, tg_api, chan):
    rt = make_rt()
    c = Chat(rt, seed, tg_api)
    await c.pick("ana")
    await c.text("falta ibuprofeno 400 mg\nfalta paracetamol 500 mg")
    tg_api.calls.clear()
    await c.text("Ver lista")
    await c.text("¿Quién soy?")
    assert tg_api.texts()[0].startswith("Faltantes activos") and tg_api.texts()[1].startswith(
        "Estás reportando"
    )


@db
async def test_evento_fallido_se_reintenta_sin_duplicar_efectos(
    make_rt, seed, tg_api, chan, sessions, monkeypatch
):
    import app.telegram.handler as h

    rt = make_rt()
    c = Chat(rt, seed, tg_api)
    await c.pick("ana")
    real = h.handle_event
    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("caída transitoria")
        return await real(*a, **kw)

    monkeypatch.setattr(h, "handle_event", flaky)
    res = await handle_update(rt, text_update(901, CHAT, "falta ibuprofeno 400 mg"), now=T0)
    assert (
        res.status == "failed" and await count(sessions, DemandObservation) == 0
    )  # rollback completo
    async with sessions() as s:
        ev = (
            await s.execute(select(InboundEvent).where(InboundEvent.external_event_id == "901"))
        ).scalar_one()
    assert ev.status == "failed" and ev.attempts == 1 and "caída transitoria" in ev.error
    assert await retry_failed_events(rt, now=T0) == 1
    assert await retry_failed_events(rt, now=T0) == 0  # ya procesado: no se repite
    assert await count(sessions, DemandObservation) == 1
    # y re-entregar el mismo update por Telegram sigue siendo un duplicado
    assert (
        await handle_update(rt, text_update(901, CHAT, "falta ibuprofeno 400 mg"), now=T0)
    ).status == "duplicate"


@db
async def test_polling_procesa_entrega_y_confirma_el_offset(make_rt, seed, tg_api, chan, sessions):
    rt = make_rt()
    tg_api.updates = [
        callback_update(41, CHAT, f"sel:{seed['ana']}"),
        text_update(42, CHAT, "falta ibuprofeno 400 mg"),
    ]
    offset = await poll_once(rt, None, timeout=0)
    assert offset == 43 and await count(sessions, DemandObservation) == 1
    assert any("Anotado" in t for t in tg_api.texts())
    tg_api.updates = [
        text_update(42, CHAT, "falta ibuprofeno 400 mg")
    ]  # Telegram reentrega tras un reinicio
    assert (
        await poll_once(rt, offset, timeout=0) == 43
        and await count(sessions, DemandObservation) == 1
    )


# ---------- webhook ----------

SECRET = "secreto_de_prueba_0123456789"


@pytest.fixture
async def hook(monkeypatch, make_rt, sessions, seed, chan):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    app = create_app()
    app.state.telegram_runtime = make_rt()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.rt, c.seed = app.state.telegram_runtime, seed
        yield c


H = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
URL = "/api/v1/integrations/telegram/webhook"


async def test_webhook_apagado_si_no_hay_secreto(client):
    r = await client.post(URL, json={}, headers=H)
    assert r.status_code == 503 and r.json()["error"]["code"] == "TELEGRAM_NOT_CONFIGURED"


async def test_webhook_sin_token_del_bot_no_procesa_aunque_haya_secreto(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", SECRET)
    get_settings.cache_clear()
    r = await client.post(URL, json=text_update(1, CHAT, "x"), headers=H)
    assert r.status_code == 503


@db
async def test_webhook_rechaza_origen_sin_secreto_o_con_secreto_incorrecto(hook, sessions):
    assert (await hook.post(URL, json=text_update(1, CHAT, "hola"))).status_code == 403
    assert (
        await hook.post(
            URL,
            json=text_update(1, CHAT, "hola"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "otro"},
        )
    ).status_code == 403
    assert await count(sessions, InboundEvent) == 0  # nada se procesó


@db
async def test_webhook_procesa_responde_y_es_idempotente(hook, sessions, tg_api):
    r = await hook.post(URL, json=callback_update(1, CHAT, f"sel:{hook.seed['ana']}"), headers=H)
    assert r.status_code == 200 and r.json() == {"ok": True}
    up = text_update(2, CHAT, "falta ibuprofeno 400 mg")
    assert (await hook.post(URL, json=up, headers=H)).status_code == 200
    assert (
        await hook.post(URL, json=up, headers=H)
    ).status_code == 200  # Telegram reintenta: 200 y sin efecto
    assert await count(sessions, DemandObservation) == 1
    assert any("Anotado" in t for t in tg_api.texts())  # la respuesta salió tras confirmar


@db
async def test_webhook_cuerpo_invalido_y_chat_ajeno(hook, sessions, tg_api):
    r = await hook.post(
        URL, content=b"no es json", headers={**H, "content-type": "application/json"}
    )
    assert r.status_code == 400
    assert (
        await hook.post(URL, json=text_update(5, "999", "falta ibuprofeno"), headers=H)
    ).status_code == 200
    assert (
        "no está autorizado" in tg_api.texts()[-1] and await count(sessions, DemandObservation) == 0
    )


_u = (mins,)
