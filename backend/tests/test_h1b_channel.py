"""H1-B: sesiones de auxiliar y flujo Telegram → sesión → interpretación → observación (PostgreSQL real).

Telegram está SIMULADO (tests/tgsim.py): se prueba nuestra lógica con actualizaciones con la forma de
la Bot API, no el servicio real.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models import (
    ActorSession,
    AuditEvent,
    ChannelMessage,
    DemandItem,
    DemandObservation,
    InboundEvent,
    OutboundMessage,
)
from app.services.channel import TELEGRAM_TEXT_LIMIT
from app.telegram.handler import after_handle, handle_update
from tests.conftest import CHAT
from tests.tgsim import callback_update, edited_update, text_update

pytestmark = pytest.mark.db
T0 = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
mins = lambda n: T0 + timedelta(minutes=n)  # noqa: E731


class Chat:
    """Conversación con el bot: cada llamada es una actualización con id creciente."""

    def __init__(self, rt, seed, api):
        self.rt, self.seed, self.api, self.uid = rt, seed, api, 100

    async def send(self, update_fn, *a, now=T0, deliver=True, **kw):
        self.uid += 1
        res = await handle_update(self.rt, update_fn(self.uid, CHAT, *a, **kw), now=now)
        if deliver:
            await after_handle(self.rt, res)
        return res

    def text(self, t, now=T0, **kw):
        return self.send(text_update, t, now=now, **kw)

    def pick(self, who, now=T0):  # pulsa el botón con el nombre del auxiliar
        return self.send(callback_update, f"sel:{self.seed[who]}", now=now)

    def click(self, data, now=T0):
        return self.send(callback_update, data, now=now)


@pytest.fixture
def chat(make_rt, seed, tg_api, chan):
    return Chat(make_rt(), seed, tg_api)


async def count(sessions, model, *where):
    async with sessions() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def observations(sessions):
    async with sessions() as s:
        return (
            (await s.execute(select(DemandObservation).order_by(DemandObservation.created_at)))
            .scalars()
            .all()
        )


# ---- Escenarios 1-4: identidad declarada, cambio de auxiliar, avisos sin sumar ----


async def test_ana_se_identifica_y_reporta_vitamina_d(chat, sessions, seed, tg_api):
    await chat.text("/start")
    assert any(
        b["text"] == "Ana"
        for row in tg_api.sent()[-1]["reply_markup"]["inline_keyboard"]
        for b in row
    )
    await chat.pick("ana")
    assert "Reportando como Ana" in tg_api.texts()[-1]  # identidad activa visible
    await chat.text("falta vitamina D 50000")
    (o,) = await observations(sessions)
    assert (
        o.author_id == seed["ana"]
        and o.source == "telegram_text"
        and o.identity_level == "declared"
    )
    assert o.actor_session_id is not None and o.channel_message_id is not None
    assert o.created_at is not None  # cuándo se declaró
    reply = tg_api.texts()[-1]
    assert (
        "Anotado: Vitamina D 50000 (sin unidad)" in reply
        and "la unidad de 50000" in reply
        and "Ana" in reply
    )
    assert "?" not in reply  # no pregunta: informa lo pendiente
    async with sessions() as s:
        item = (await s.execute(select(DemandItem))).scalar_one()
    assert item.target_quantity is None and item.attributes["strengths"] == []  # nada supuesto


async def test_catalogo_verificado_propone_vitamina_d_50000_ui_sin_aplicarla(chat, sessions, seed):
    from app.services.actors import load_actor
    from app.services.catalog import create_pharma_identity

    async with sessions() as s, s.begin():
        await create_pharma_identity(
            s, await load_actor(s, seed["pao"]), "vitamina d 50000 UI cápsulas"
        )
    await chat.pick("ana")
    await chat.text("falta vitamina D 50000")
    (o,) = await observations(sessions)
    (c,) = [c for c in o.interpretation["candidates"] if c["kind"] == "measure_assignment"]
    assert (
        c["source"] == "catalog" and c["verified"] is True and c["assignment"]["unit_expr"] == "ui"
    )
    async with sessions() as s:
        item = (await s.execute(select(DemandItem))).scalar_one()
    assert (
        item.attributes["strengths"] == [] and item.attributes["unassigned"][0]["value"] == "50000"
    )  # NO confirmado


async def test_luis_cambia_identidad_mismo_producto_dos_avisos_sin_sumar(
    chat, sessions, seed, tg_api
):
    await chat.pick("ana")
    await chat.text("falta vitamina D 50000", now=mins(1))
    await chat.text("Cambiar auxiliar", now=mins(2))
    await chat.pick("luis", now=mins(2))
    assert "Reportando como Luis" in tg_api.texts()[-1]
    await chat.text("no queda vitamina D 50000", now=mins(3))
    # Modelo A: el bot no adjunta el aviso al reporte de otra persona; pregunta primero.
    async with sessions() as s:
        (pending,) = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.confirmation_status == "pending_other_report"
                )
            )
        ).scalars().all()
        (item,) = (await s.execute(select(DemandItem))).scalars().all()
    assert item.reported_by == seed["ana"]
    assert "Ana ya informó" in tg_api.texts()[-1]
    assert "¿También quieres reportarlo?" in tg_api.texts()[-1]
    assert "reporte tuyo independiente" in tg_api.texts()[-1]
    # Exactamente dos caminos: un reporte propio o cancelar. Nada de terceras opciones.
    assert [b["text"] for b in tg_api.last_buttons()] == ["Sí, agregar mi reporte", "No, cancelar"]
    assert len(await observations(sessions)) == 1  # todavía no se creó nada para Luis

    await chat.click(f"rp:ok:{pending.id}", now=mins(4))
    obs = await observations(sessions)
    assert [o.author_id for o in obs] == [seed["ana"], seed["luis"]]  # autores correctos
    assert obs[0].actor_session_id != obs[1].actor_session_id
    assert obs[0].demand_item_id != obs[1].demand_item_id  # reportes independientes
    assert obs[1].match_status == "created_item"
    async with sessions() as s:
        items = (await s.execute(select(DemandItem))).scalars().all()
        sess = (
            (await s.execute(select(ActorSession).order_by(ActorSession.started_at)))
            .scalars()
            .all()
        )
    assert len(items) == 2 and all(i.target_quantity is None for i in items)
    assert {i.reported_by for i in items} == {seed["ana"], seed["luis"]}
    assert [(x.end_reason, x.ended_at is None) for x in sess] == [("replaced", False), (None, True)]
    assert "reporte independiente" in tg_api.texts()[-1]


# ---- D-135: confirmación explícita antes de tocar una cantidad ----


async def test_la_correccion_por_texto_nunca_toca_la_cantidad_de_compra(
    chat, sessions, seed, tg_api, ctx
):
    from app.domain.quantities import QuantityOp, QuantityUnit

    await chat.pick("pao")
    await chat.text("falta ibuprofeno 400 mg", now=mins(1))
    (item,) = await ctx.items()
    await ctx.qty(
        item.id, QuantityOp.SET, Decimal(5), QuantityUnit.BOX, version=item.version
    )

    await chat.text("necesitamos 8 cajas de ibuprofeno 400 mg", now=mins(2))
    async with sessions() as s:
        (pending,) = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.confirmation_status == "pending_same_author_quantity"
                )
            )
        ).scalars().all()
        assert (
            await s.execute(select(DemandItem))
        ).scalar_one().target_quantity == Decimal(5)  # el texto no cambió nada
        assert (
            await s.execute(select(func.count()).select_from(DemandObservation))
        ).scalar_one() == 1
    text = tg_api.texts()[-1]
    assert "Ya tienes 5 cajas de Ibuprofeno 400 mg" in text
    assert "Escribe la cantidad correcta" in text
    assert _buttons(tg_api) == []  # la corrección no usa botones

    await chat.text("3 cajas más", now=mins(3))
    (item,) = await ctx.items()
    assert item.target_quantity == Decimal(5)  # el texto nunca cambia la cantidad de compra
    assert "Listo, actualicé tu solicitud de Ibuprofeno 400 mg a 8 cajas." in tg_api.texts()[-1]

    # Otra cifra vuelve a preguntar antes de cambiar nada, también sin botones.
    await chat.text("necesitamos 6 cajas de ibuprofeno 400 mg", now=mins(4))
    async with sessions() as s:
        (pending,) = (
            await s.execute(
                select(ChannelMessage)
                .where(ChannelMessage.confirmation_status == "pending_same_author_quantity")
                .order_by(ChannelMessage.received_at.desc())
            )
        ).scalars().all()
    assert "¿Quieres agregar más o cambiar el total?" in tg_api.texts()[-1]
    assert _buttons(tg_api) == []
    await chat.text("en total 6 cajas", now=mins(5))
    (item,) = await ctx.items()
    assert item.target_quantity == Decimal(5)  # sigue intacta la decisión de compra
    assert "Listo, actualicé tu solicitud de Ibuprofeno 400 mg a 6 cajas." in tg_api.texts()[-1]


async def test_el_auxiliar_solo_deja_una_propuesta_y_no_aplica_la_cantidad(
    chat, sessions, seed, tg_api, ctx
):
    from app.domain.quantities import QuantityOp, QuantityUnit

    await chat.pick("ana")
    await chat.text("falta ibuprofeno 400 mg", now=mins(1))
    (item,) = await ctx.items()
    await ctx.qty(item.id, QuantityOp.SET, Decimal(5), QuantityUnit.BOX, version=item.version)

    await chat.text("necesitamos 8 cajas de ibuprofeno 400 mg", now=mins(2))
    await chat.text("3 cajas más", now=mins(3))

    (item,) = await ctx.items()
    assert item.target_quantity == Decimal(5)  # Ana no aplica cantidades (D-124/D-135)
    async with sessions() as s:
        obs = (
            (
                await s.execute(
                    select(DemandObservation).order_by(DemandObservation.created_at.desc()).limit(1)
                )
            )
            .scalars()
            .one()
        )
        assert obs.author_id == seed["ana"]
        assert obs.quantity_proposal["operation"] == "ADD"
        assert obs.quantity_proposal["value"] == "3"
    assert "Listo, actualicé tu solicitud de Ibuprofeno 400 mg a 8 cajas." in tg_api.texts()[-1]
    assert _buttons(tg_api) == []  # toda la corrección se conversa por texto


async def test_auxiliar_corrige_por_texto_libre_sin_otra_fila_ni_tocar_el_objetivo(
    chat, sessions, seed, tg_api
):
    await chat.pick("ana")
    await chat.text("2 cajas de omeprazol 20 mg", now=mins(1))
    # Un segundo aviso del mismo autor abre la pregunta; todavía no se registra nada más.
    await chat.text("5 cajas de omeprazol 20 mg", now=mins(2))
    assert "¿Quieres agregar más o cambiar el total?" in tg_api.texts()[-1]
    assert _buttons(tg_api) == []
    async with sessions() as s:
        assert (
            await s.execute(select(func.count()).select_from(DemandObservation))
        ).scalar_one() == 1
        assert (await s.execute(select(func.count()).select_from(DemandItem))).scalar_one() == 1

    # Una cantidad sola es ambigua: se pregunta y NO se modifica.
    await chat.text("7", now=mins(3))
    assert "¿Quieres agregar esa cantidad o cambiar el total?" in tg_api.texts()[-1]
    assert _buttons(tg_api) == []
    async with sessions() as s:
        assert (
            await s.execute(select(func.count()).select_from(DemandObservation))
        ).scalar_one() == 1

    # «En total» es explícito: corrige la cantidad solicitada sin tocar la objetivo.
    await chat.text("en total 15 cajas", now=mins(4))
    assert "Listo, actualicé tu solicitud de Omeprazol 20 mg a 15 cajas." in tg_api.texts()[-1]
    async with sessions() as s:
        (item,) = (await s.execute(select(DemandItem))).scalars().all()
        assert item.target_quantity is None  # la auxiliar nunca aplica cantidades
        obs = (
            (await s.execute(select(DemandObservation).order_by(DemandObservation.created_at)))
            .scalars()
            .all()
        )
        assert len(obs) == 2  # misma fila/historial, sin un segundo reporte
        assert obs[-1].demand_item_id == item.id
        assert (obs[-1].declared_quantity, obs[-1].declared_unit) == (Decimal(15), "box")
        assert obs[-1].quantity_proposal == {
            "operation": "SET",
            "value": "15",
            "unit": "box",
            "reason": "correccion_telegram",
            "declared_quantity": "15",
        }
        pending = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.confirmation_status == "pending_same_author_quantity"
                )
            )
        ).scalar_one_or_none()
        assert pending is None  # la confirmación quedó resuelta


async def test_una_cifra_menor_tambien_se_confirma_antes_de_cambiar(
    chat, sessions, seed, tg_api
):
    await chat.pick("ana")
    await chat.text("5 cajas de omeprazol 20 mg", now=mins(1))
    await chat.text("3 cajas de omeprazol 20 mg", now=mins(2))
    assert "¿Quieres agregar más o cambiar el total?" in tg_api.texts()[-1]
    assert _buttons(tg_api) == []
    await chat.text("2", now=mins(3))
    assert "Tienes 5 cajas de Omeprazol 20 mg y escribiste 2 cajas" in tg_api.texts()[-1]
    assert _buttons(tg_api) == []
    async with sessions() as s:
        assert (
            await s.execute(select(func.count()).select_from(DemandObservation))
        ).scalar_one() == 1  # todavía no se modificó nada
    await chat.text("en total 2 cajas", now=mins(4))
    async with sessions() as s:
        obs = (
            (await s.execute(select(DemandObservation).order_by(DemandObservation.created_at)))
            .scalars()
            .all()
        )
        assert (obs[-1].declared_quantity, obs[-1].declared_unit) == (Decimal(2), "box")


async def test_auxiliar_suma_por_texto_libre_sobre_su_cantidad_solicitada(
    chat, sessions, seed, tg_api
):
    await chat.pick("ana")
    await chat.text("2 cajas de omeprazol 20 mg", now=mins(1))
    await chat.text("4 cajas de omeprazol 20 mg", now=mins(2))
    assert "¿Quieres agregar más o cambiar el total?" in tg_api.texts()[-1]
    # «más» es explícito: 2 solicitadas + 4 más = 6 solicitadas. No son 6 en total.
    await chat.text("4 cajas más", now=mins(3))
    assert "Listo, actualicé tu solicitud de Omeprazol 20 mg a 6 cajas." in tg_api.texts()[-1]
    async with sessions() as s:
        (item,) = (await s.execute(select(DemandItem))).scalars().all()
        assert item.target_quantity is None
        obs = (
            (await s.execute(select(DemandObservation).order_by(DemandObservation.created_at)))
            .scalars()
            .all()
        )
        assert (obs[-1].declared_quantity, obs[-1].declared_unit) == (Decimal(6), "box")
        assert obs[-1].quantity_proposal["operation"] == "ADD"
        assert obs[-1].quantity_proposal["value"] == "4"


async def test_auxiliar_corrige_su_solicitud_sin_sobrescribir_la_cantidad_de_pao(
    chat, sessions, seed, tg_api, ctx
):
    from app.domain.quantities import QuantityOp, QuantityUnit

    await chat.pick("ana")
    await chat.text("falta ibuprofeno 400 mg", now=mins(1))
    (item,) = await ctx.items()
    await ctx.qty(item.id, QuantityOp.SET, Decimal(5), QuantityUnit.BOX, version=item.version)

    await chat.text("8 cajas de ibuprofeno 400 mg", now=mins(2))
    assert "¿Quieres agregar más o cambiar el total?" in tg_api.texts()[-1]
    await chat.text("en total 8 cajas", now=mins(3))
    assert "Listo, actualicé tu solicitud de Ibuprofeno 400 mg a 8 cajas." in tg_api.texts()[-1]
    (item,) = await ctx.items()
    assert item.target_quantity == Decimal(5)  # la decisión de compra de Pao no se tocó
    async with sessions() as s:
        obs = (
            (
                await s.execute(
                    select(DemandObservation).order_by(DemandObservation.created_at.desc()).limit(1)
                )
            )
            .scalars()
            .one()
        )
        assert obs.demand_item_id == item.id
        assert (obs.declared_quantity, obs.declared_unit) == (Decimal(8), "box")
        assert obs.quantity_proposal["operation"] == "SET"
        assert obs.quantity_proposal["value"] == "8"


async def test_otro_autor_modifica_su_cantidad_y_crea_su_reporte_independiente(
    chat, sessions, seed, tg_api
):
    await chat.pick("ana")
    await chat.text("falta ibuprofeno 400 mg", now=mins(1))
    await chat.text("Cambiar auxiliar", now=mins(2))
    await chat.pick("luis", now=mins(2))
    await chat.text("falta ibuprofeno 400 mg", now=mins(3))
    async with sessions() as s:
        (pending,) = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.confirmation_status == "pending_other_report"
                )
            )
        ).scalars().all()
    assert "independiente" in tg_api.texts()[-1]

    await chat.click(f"rp:new:{pending.id}", now=mins(4))
    assert "Escribe la cantidad" in tg_api.texts()[-1]
    await chat.text("6 cajas", now=mins(5))

    async with sessions() as s:
        obs = (
            await s.execute(select(DemandObservation).order_by(DemandObservation.created_at))
        ).scalars().all()
        items = (await s.execute(select(DemandItem))).scalars().all()
    assert [o.author_id for o in obs] == [seed["ana"], seed["luis"]]
    assert (obs[1].declared_quantity, obs[1].declared_unit) == (Decimal(6), "box")
    assert obs[1].match_status == "created_item" and obs[1].demand_item_id != obs[0].demand_item_id
    assert len(items) == 2 and all(i.target_quantity is None for i in items)
    assert {i.reported_by for i in items} == {seed["ana"], seed["luis"]}
    assert "reporte independiente" in tg_api.texts()[-1]


async def test_otro_autor_puede_cancelar_su_aviso_sin_crear_nada(chat, sessions, seed, tg_api):
    await chat.pick("ana")
    await chat.text("falta paracetamol 500 mg", now=mins(1))
    await chat.text("Cambiar auxiliar", now=mins(2))
    await chat.pick("luis", now=mins(2))
    await chat.text("falta paracetamol 500 mg", now=mins(3))
    async with sessions() as s:
        (pending,) = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.confirmation_status == "pending_other_report"
                )
            )
        ).scalars().all()
    await chat.click(f"rp:cancel:{pending.id}", now=mins(4))

    assert "no registré ese aviso" in tg_api.texts()[-1]
    async with sessions() as s:
        assert (
            await s.execute(select(func.count()).select_from(DemandObservation))
        ).scalar_one() == 1
        assert (
            await s.execute(select(func.count()).select_from(DemandItem))
        ).scalar_one() == 1
        msg = await s.get(ChannelMessage, pending.id)
        assert msg.attribution_status == "discarded"
        assert msg.confirmation_status == "cancelled"
        assert msg.discard_reason == "confirmation_cancelled"


# ---- Escenario 5: duplicados técnicos ----


async def test_mensaje_repetido_por_telegram_no_genera_otra_observacion(chat, sessions):
    await chat.pick("ana")
    upd = text_update(500, CHAT, "falta ibuprofeno 400 mg")
    r1 = await handle_update(chat.rt, upd, now=T0)
    r2 = await handle_update(chat.rt, upd, now=T0)  # Telegram reintenta
    r3 = await handle_update(chat.rt, upd, now=mins(1))
    assert (r1.status, r2.status, r3.status) == ("processed", "duplicate", "duplicate")
    assert len(await observations(sessions)) == 1
    assert await count(sessions, InboundEvent, InboundEvent.external_event_id == "500") == 1
    assert (
        await count(sessions, OutboundMessage, OutboundMessage.inbound_event_id.is_not(None)) >= 1
    )


# ---- Escenarios 6-7: sin identidad y sesión expirada ----


async def test_mensaje_sin_sesion_queda_pendiente_y_no_se_atribuye(chat, sessions, tg_api):
    await chat.text("falta ibuprofeno 400 mg")
    assert await observations(sessions) == []
    async with sessions() as s:
        m = (await s.execute(select(ChannelMessage))).scalar_one()
    assert (
        m.attribution_status == "pending_attribution"
        and m.author_id is None
        and m.session_id is None
    )
    assert "no lo anoto a nombre de nadie" in tg_api.texts()[-1]


async def test_sesion_expirada_no_atribuye_al_auxiliar_anterior(chat, sessions, seed, tg_api):
    await chat.pick("ana", now=T0)
    await chat.text("falta ibuprofeno 400 mg", now=mins(5))  # actividad: renueva la sesión
    await chat.text(
        "falta paracetamol 500 mg", now=mins(14)
    )  # 9 min desde la última: sigue vigente
    assert len(await observations(sessions)) == 2
    await chat.text("falta omeprazol 20 mg", now=mins(30))  # 16 min sin uso: expiró
    assert len(await observations(sessions)) == 2  # NO se atribuyó a Ana
    async with sessions() as s:
        pend = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.attribution_status == "pending_attribution"
                )
            )
        ).scalar_one()
        sess = (await s.execute(select(ActorSession))).scalar_one()
        acts = (
            (await s.execute(select(AuditEvent.action).where(AuditEvent.action.like("session.%"))))
            .scalars()
            .all()
        )
    assert "omeprazol" in pend.text and sess.end_reason == "expired" and sess.ended_at is not None
    assert (
        "session.selected" in acts and "session.expired" in acts
    )  # auditoría de selección y expiración
    assert "no lo anoto a nombre de nadie" in tg_api.texts()[-1]


async def test_barrido_cierra_sesiones_vencidas_aunque_nadie_escriba(chat, sessions):
    from app.services import sessions as sess_svc

    await chat.pick("ana", now=T0)
    async with sessions() as s, s.begin():
        assert await sess_svc.expire_stale(s, mins(5)) == 0
        assert await sess_svc.expire_stale(s, mins(11)) == 1
        assert await sess_svc.expire_stale(s, mins(12)) == 0  # idempotente


# ---- Atribución de pendientes: jamás automática ----


async def test_pendiente_se_atribuye_al_elegir_el_nombre_sin_segunda_pregunta(
    chat, sessions, seed, tg_api
):
    await chat.text("falta ibuprofeno 400 mg", now=T0)
    await chat.pick("luis", now=mins(2))
    # Elegir el nombre ES la confirmación: el reporte pendiente continúa solo.
    (o,) = await observations(sessions)
    assert o.author_id == seed["luis"]
    reply = "\n".join(tg_api.texts()[-2:])
    assert "Reportando como Luis" in reply
    assert "Anotado: Ibuprofeno" in reply
    assert "¿Los anoto" not in reply
    async with sessions() as s:
        m = (await s.execute(select(ChannelMessage))).scalar_one()
    assert m.attribution_basis == "confirmed_after_identification" and m.attributed_at is not None
    await chat.click("att:ok", now=mins(4))  # botón viejo: no vuelve a registrar nada
    assert len(await observations(sessions)) == 1


async def test_pendiente_viejo_no_se_atribuye_al_elegir_el_nombre(chat, sessions, tg_api):
    await chat.text("falta ibuprofeno 400 mg", now=T0)
    await chat.pick("luis", now=mins(60))  # fuera de la ventana de pendientes
    assert await observations(sessions) == []  # demasiado viejo: no se atribuye a Luis
    assert "Un mensaje era de hace demasiado rato" in tg_api.texts()[-1]
    async with sessions() as s:
        st = (
            (
                await s.execute(
                    select(ChannelMessage.discard_reason).where(
                        ChannelMessage.attribution_status == "discarded"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert st == ["stale_unattributed"]


# ---- Escenario 9: alias ambiguo ----


async def test_alias_ambiguo_no_provoca_fusion_incorrecta(chat, sessions, seed):
    from app.services.actors import load_actor
    from app.services.catalog import create_pharma_identity

    async with sessions() as s, s.begin():
        pao = await load_actor(s, seed["pao"])
        await create_pharma_identity(s, pao, "vitamina d 50000 UI")
        await create_pharma_identity(s, pao, "vitamina d3 50000 UI")
    await chat.pick("ana")
    await chat.text("falta vitamina D 50000")
    await chat.text(
        "no queda la D de cincuenta mil"
    )  # «d» cabe en «vitamina d» y en «vitamina d3»? solo token completo
    obs = await observations(sessions)
    assert (
        obs[1].match_status == "needs_review" and obs[1].demand_item_id is None
    )  # conservada, sin fusionar
    cands = [
        c["ingredient"] for c in obs[1].interpretation["candidates"] if c["kind"] == "ingredient"
    ]
    assert "vitamina d" in cands  # propuesto…
    assert (
        await count(sessions, DemandItem)
    ) == 1  # …pero no creó un faltante basura ni fusionó nada


async def test_resolver_el_nombre_elegido_y_recordarlo_como_alias(chat, sessions, seed):
    from app.services.actors import load_actor
    from app.services.catalog import create_pharma_identity
    from app.services.demand import resolve_observation

    async with sessions() as s, s.begin():
        await create_pharma_identity(s, await load_actor(s, seed["pao"]), "vitamina d 50000 UI")
    await chat.pick("ana")
    await chat.text("falta vitamina D 50000")
    await chat.text("no queda la D de cincuenta mil")
    o = (await observations(sessions))[1]
    async with sessions() as s, s.begin():
        pao = await load_actor(s, seed["pao"])
        item = (await s.execute(select(DemandItem))).scalar_one()
        r = await resolve_observation(
            s,
            pao,
            o.id,
            demand_item_id=item.id,
            create_new=False,
            as_ingredient="vitamina d",
            remember_alias=True,
        )
    assert r.outcome == "resolved_manual" and r.alias_created
    await chat.text(
        "se acabó la D de cincuenta mil"
    )  # ahora el alias verificado resuelve solo el nombre
    last = (await observations(sessions))[-1]
    assert (
        last.match_status == "attached_existing"
        and last.interpretation["applied"][0]["kind"] == "expression"
    )
    async with sessions() as s:
        assert (await s.execute(select(DemandItem))).scalar_one().attributes[
            "strengths"
        ] == []  # la unidad sigue sin confirmar


# ---- Escenario 10: concurrencia ----


async def test_eventos_concurrentes_no_corrompen_sesiones_ni_observaciones(
    make_rt, seed, tg_api, chan, sessions
):
    rt = make_rt()
    uid = iter(range(1000, 2000))
    batch = []
    for i in range(6):
        batch.append(callback_update(next(uid), CHAT, f"sel:{seed['ana' if i % 2 else 'luis']}"))
    msgs = [text_update(next(uid), CHAT, "falta ibuprofeno 400 mg") for _ in range(10)]
    dups = [msgs[0]] * 4 + [batch[0]] * 3  # reentregas simultáneas del mismo evento
    results = await asyncio.gather(
        *[handle_update(rt, u, now=T0) for u in batch + msgs + dups], return_exceptions=True
    )
    assert not [r for r in results if isinstance(r, Exception)]
    assert sum(r.status == "failed" for r in results) == 0
    async with sessions() as s:
        active = (
            await s.execute(
                select(func.count())
                .select_from(ActorSession)
                .where(ActorSession.ended_at.is_(None))
            )
        ).scalar_one()
        obs = (await s.execute(select(DemandObservation))).scalars().all()
        sess = {x.id: x for x in (await s.execute(select(ActorSession))).scalars()}
        items = (await s.execute(select(DemandItem))).scalars().all()
        msg_attr = (
            await s.execute(
                select(ChannelMessage.attribution_status, func.count()).group_by(
                    ChannelMessage.attribution_status
                )
            )
        ).all()
    assert active <= 1  # a lo más una sesión activa por chat
    for o in obs:  # cada observación pertenece a la persona de SU sesión
        assert sess[o.actor_session_id].auxiliary_id == o.author_id
    assert len({o.idempotency_key for o in obs}) == len(obs)  # sin observaciones repetidas
    assert (
        len(obs)
        + dict(msg_attr).get("pending_attribution", 0)
        + dict(msg_attr).get("pending_confirmation", 0)
        == 10
    )  # cada mensaje: observación, pendiente de identidad o pendiente de confirmación
    assert len(items) <= 2 and all(i.target_quantity is None for i in items)
    assert await count(sessions, InboundEvent) == len(batch) + len(msgs)  # duplicados descartados


# ---- Comandos, identidad visible, listas y casos borde ----


async def test_comandos_identidad_lista_y_ayuda(chat, sessions, tg_api):
    await chat.text("¿Quién soy?")
    assert "Nadie ha elegido identidad" in tg_api.texts()[-1]
    await chat.pick("ana")
    await chat.text("¿Quién soy?")
    assert "Estás reportando como Ana" in tg_api.texts()[-1] and "declarada" in tg_api.texts()[-1]
    await chat.text("Ver lista")
    assert "vacía" in tg_api.texts()[-1]
    for t in (
        "falta ibuprofeno 400 mg",
        "falta paracetamol 500 mg",
        "falta omeprazol 20 mg",
        "falta loratadina 10 mg",
    ):
        await chat.text(t)
    await chat.text("Ver lista")
    lista = tg_api.sent()[-1]
    assert "📋 Faltantes activos (4)" in lista["text"]
    assert "1. " in lista["text"] and "3. " in lista["text"] and "4. " not in lista["text"]
    assert lista["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "lst:1"
    await chat.click("lst:1")
    assert "4. " in tg_api.texts()[-1]
    await chat.text("/salir")
    await chat.text("¿Quién soy?")
    assert "Nadie ha elegido identidad" in tg_api.texts()[-1]


async def test_saludos_preguntas_y_ediciones_no_crean_ni_cambian_nada(chat, sessions, tg_api):
    await chat.pick("ana")
    await chat.text("hola buenos días")
    await chat.text("¿hay ibuprofeno de 400?")
    await chat.send(edited_update, "falta ibuprofeno 600 mg")
    assert await observations(sessions) == [] and await count(sessions, DemandItem) == 0
    assert "no cambié nada" in tg_api.texts()[-1]


async def test_mensaje_con_varios_productos_registra_uno_por_producto(chat, sessions):
    await chat.pick("ana")
    await chat.text("falta ibuprofeno 400 mg, paracetamol 500 mg\nvitamina d 50000 ui")
    assert len(await observations(sessions)) == 3 and await count(sessions, DemandItem) == 3


async def test_el_bot_nunca_aplica_cantidades(chat, sessions):
    await chat.pick("ana")
    await chat.text("necesitamos 5 cajas de ibuprofeno 400 mg")
    (o,) = await observations(sessions)
    assert (o.declared_quantity, o.declared_unit) == (5, "box")  # sugerencia registrada…
    async with sessions() as s:
        assert (
            await s.execute(select(DemandItem))
        ).scalar_one().target_quantity is None  # …no aplicada


async def test_respuesta_inmediata_muestra_la_cantidad_declarada_sin_aplicarla(
    chat, sessions, tg_api
):
    await chat.pick("ana")
    await chat.text("2 cajas de omeprazol 20 mg")
    reply = tg_api.texts()[-1]
    assert "✅ Anotado: Omeprazol 20 mg — 2 cajas." in reply
    assert "👤 Reportado por: Ana" in reply
    (o,) = await observations(sessions)
    assert (o.declared_quantity, o.declared_unit) == (Decimal(2), "box")  # declarada…
    async with sessions() as s:
        assert (
            await s.execute(select(DemandItem))
        ).scalar_one().target_quantity is None  # …nunca aplicada


async def test_respuesta_inmediata_sin_cantidad_usa_unidad_predeterminada(
    chat, sessions, tg_api
):
    """D-147: sin cantidad se pide 1 unidad genérica, nunca caja/frasco/sobre."""
    await chat.pick("ana")
    await chat.text("falta ibuprofeno 400 mg")
    assert "✅ Anotado: Ibuprofeno 400 mg — 1 unidad." in tg_api.texts()[-1]
    (o,) = await observations(sessions)
    assert (o.declared_quantity, o.declared_unit) == (Decimal(1), "unit")
    assert (o.interpretation or {}).get("quantity_origin") == "default"
    async with sessions() as s:
        assert (
            await s.execute(select(DemandItem))
        ).scalar_one().target_quantity is None  # la unidad predeterminada no es cantidad de compra


async def test_lista_muestra_la_cantidad_declarada_sin_volverla_objetivo(
    chat, sessions, tg_api
):
    await chat.pick("ana")
    await chat.text("2 cajas de omeprazol 20 mg", now=mins(1))
    await chat.text("falta ibuprofeno 400 mg", now=mins(2))
    await chat.text("/lista", now=mins(3))
    text = tg_api.texts()[-1]
    assert (
        "<b>Omeprazol 20 mg</b>\n"
        "   Cantidad: 2 cajas\n"
        "   Reportado por: Ana\n"
        "   Estado: Pendiente de compra\n"
        "   1 aviso" in text
    )
    assert (
        "<b>Ibuprofeno 400 mg</b>\n"
        "   Cantidad: 1 unidad\n"
        "   Reportado por: Ana\n"
        "   Estado: Pendiente de compra\n"
        "   1 aviso" in text
    )
    assert "aviso(s)" not in text
    async with sessions() as s:
        assert all(
            i.target_quantity is None for i in (await s.execute(select(DemandItem))).scalars()
        )  # la lista no convierte lo declarado en objetivo


async def test_lista_muestra_el_estado_real_de_cobertura(chat, tg_api, monkeypatch):
    """«Ver faltantes» traduce la cobertura vigente; la cobertura real se prueba en C5-A."""
    from app.services import channel as channel_svc

    await chat.pick("ana")
    await chat.text("falta ibuprofeno 400 mg", now=mins(1))

    async def in_cart(session, org, items):
        return {i.id: {"replenishment_state": "in_quote"} for i in items}

    monkeypatch.setattr(channel_svc.coverage_svc, "coverage_for_items", in_cart)
    await chat.text("Ver faltantes", now=mins(2))
    text = tg_api.texts()[-1]
    assert "Estado: 🛒 En carrito" in text and "Pendiente de compra" not in text
    assert "<s><b>Ibuprofeno 400 mg</b></s>" in text  # tachado solo en el nombre
    assert "Cantidad: 1 unidad\n   Reportado por: Ana" in text  # el resto sigue visible

    # Al retirarlo del carrito sin comprar, el mismo faltante vuelve a verse normal.
    async def pending(session, org, items):
        return {i.id: {"replenishment_state": "pending"} for i in items}

    monkeypatch.setattr(channel_svc.coverage_svc, "coverage_for_items", pending)
    await chat.text("Ver faltantes", now=mins(3))
    text = tg_api.texts()[-1]
    assert "Estado: Pendiente de compra" in text
    assert "<s>" not in text and "<b>Ibuprofeno 400 mg</b>" in text
    assert "Cantidad: 1 unidad" in text and "Reportado por: Ana" in text  # conserva todo


async def test_lista_tacha_solo_el_reporte_que_esta_en_carrito(
    chat, sessions, seed, tg_api, monkeypatch
):
    """D-135/D-147: dos autores, dos filas; solo la cubierta por el carrito se tacha."""
    from app.services import channel as channel_svc

    await chat.pick("ana")
    await chat.text("2 cajas de omeprazol 20 mg", now=mins(1))
    await chat.text("Cambiar auxiliar", now=mins(2))
    await chat.pick("luis", now=mins(2))
    await chat.text("6 cajas de omeprazol 20 mg", now=mins(3))
    (pending,) = await _pending(sessions, "pending_other_report")
    await chat.click(f"rp:ok:{pending.id}", now=mins(4))

    async with sessions() as s:
        items = (await s.execute(select(DemandItem))).scalars().all()
    ana_item = next(i for i in items if i.reported_by == seed["ana"])

    async def only_ana_in_cart(session, org, rows):
        return {
            i.id: {"replenishment_state": "in_quote" if i.id == ana_item.id else "pending"}
            for i in rows
        }

    monkeypatch.setattr(channel_svc.coverage_svc, "coverage_for_items", only_ana_in_cart)
    await chat.text("Ver faltantes", now=mins(5))

    text = tg_api.texts()[-1]
    blocks = text.split("\n\n")
    ana_block = next(b for b in blocks if "Reportado por: Ana" in b)
    luis_block = next(b for b in blocks if "Reportado por: Luis" in b)
    assert "<s><b>Omeprazol 20 mg</b></s>" in ana_block
    assert "Cantidad: 2 cajas" in ana_block and "Estado: 🛒 En carrito" in ana_block
    assert "<s>" not in luis_block
    assert "Cantidad: 6 cajas" in luis_block and "Estado: Pendiente de compra" in luis_block


async def test_lista_muestra_cada_reporte_con_su_autor_y_su_cantidad(
    chat, sessions, seed, tg_api
):
    """Dos personas por el mismo producto: dos filas, cada una con su autor y su cantidad."""
    await chat.pick("ana")
    await chat.text("2 cajas de omeprazol 20 mg", now=mins(1))
    await chat.text("Cambiar auxiliar", now=mins(2))
    await chat.pick("luis", now=mins(2))
    await chat.text("6 cajas de omeprazol 20 mg", now=mins(3))
    async with sessions() as s:
        (pending,) = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.confirmation_status == "pending_other_report"
                )
            )
        ).scalars().all()
    await chat.click(f"rp:ok:{pending.id}", now=mins(4))

    await chat.text("Ver faltantes", now=mins(5))
    text = tg_api.texts()[-1]
    assert "Cantidad: 2 cajas\n   Reportado por: Ana" in text
    assert "Cantidad: 6 cajas\n   Reportado por: Luis" in text
    assert "📋 Faltantes activos (2)" in text
    async with sessions() as s:
        items = (await s.execute(select(DemandItem))).scalars().all()
    assert {i.reported_by for i in items} == {seed["ana"], seed["luis"]}


async def test_lista_prioriza_la_cantidad_objetivo(chat, sessions, tg_api, ctx):
    from app.domain.quantities import QuantityOp, QuantityUnit

    await chat.pick("ana")
    await chat.text("2 cajas de omeprazol 20 mg", now=mins(1))
    (item,) = await ctx.items()
    await ctx.qty(
        item.id, QuantityOp.SET, Decimal(5), QuantityUnit.BOX, version=item.version
    )
    await chat.text("/lista", now=mins(2))
    text = tg_api.texts()[-1]
    assert (
        "<b>Omeprazol 20 mg</b>\n"
        "   Cantidad: 5 cajas\n"
        "   Reportado por: Ana\n"
        "   Estado: Pendiente de compra\n"
        "   1 aviso" in text
    )
    assert "solicitada" not in text


async def test_lista_usa_negrita_y_un_bloque_por_producto(chat, tg_api):
    await chat.pick("ana")
    await chat.text("2 cajas de omeprazol 20 mg", now=mins(1))
    await chat.text("5 cajas de omeprazol 20 mg", now=mins(2))
    await chat.text("3 cajas más", now=mins(3))  # 2 + 3 = 5: segunda observación del mismo faltante
    await chat.text("falta ibuprofeno 400 mg", now=mins(4))
    await chat.text("Ver faltantes", now=mins(5))
    msg = tg_api.sent()[-1]
    text = msg["text"]
    assert msg.get("parse_mode") == "HTML"
    assert text.startswith("📋 Faltantes activos (2)\n\n1. <b>")
    blocks = text.split("\n\n")
    assert len(blocks) == 3  # encabezado + un bloque por producto
    omeprazol = next(b for b in blocks if "<b>Omeprazol 20 mg</b>" in b)
    ibuprofeno = next(b for b in blocks if "<b>Ibuprofeno 400 mg</b>" in b)
    assert (
        "   Cantidad: 5 cajas\n"
        "   Reportado por: Ana\n"
        "   Estado: Pendiente de compra\n"
        "   2 avisos" in omeprazol
    )
    assert "   Cantidad: 1 unidad" in ibuprofeno
    assert "   1 aviso" in ibuprofeno and "1 avisos" not in text
    assert "aviso(s)" not in text
    assert " · " not in omeprazol  # datos en líneas separadas, no amontonados
    assert "<s>" not in text  # pendientes de compra: nombre normal, sin tachado
    assert "Estado: Pendiente de compra" in text


async def test_lista_escapa_caracteres_especiales_del_nombre(chat, sessions, tg_api):
    await chat.pick("ana")
    await chat.text("falta vitamina C", now=mins(1))
    # El intérprete normaliza símbolos; se fija el nombre tal cual para probar el escape del canal.
    async with sessions() as s, s.begin():
        item = (await s.execute(select(DemandItem))).scalar_one()
        item.display_name = "Vitamina C <500 mg> & zinc"
    await chat.text("Ver faltantes", now=mins(2))
    msg = tg_api.sent()[-1]
    assert msg.get("parse_mode") == "HTML"
    assert "<b>Vitamina C &lt;500 mg&gt; &amp; zinc</b>" in msg["text"]
    # El nombre no rompe el HTML: no queda ningún «<» sin escapar dentro de la negrita.
    name_line = next(line for line in msg["text"].split("\n") if line.startswith("1. "))
    assert name_line.count("<b>") == 1 and name_line.count("</b>") == 1
    assert "<s>" not in name_line  # pendiente: sin tachado
    assert "<500" not in name_line and "&amp; zinc" in name_line


async def test_lista_pagina_sin_repetir_y_dentro_del_limite(chat, tg_api):
    await chat.pick("ana")
    for n, product in enumerate(
        ("omeprazol 20 mg", "paracetamol 500 mg", "ibuprofeno 400 mg", "loratadina 10 mg"), start=1
    ):
        await chat.text(f"falta {product}", now=mins(n))
    await chat.text("Ver faltantes", now=mins(9))
    first = tg_api.sent()[-1]
    assert "📋 Faltantes activos (4)" in first["text"]
    assert "1. " in first["text"] and "3. " in first["text"] and "4. " not in first["text"]
    assert first["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "lst:1"

    await chat.click("lst:1", now=mins(10))
    second = tg_api.sent()[-1]
    assert "4. " in second["text"] and "1. " not in second["text"]
    assert "inline_keyboard" not in (second.get("reply_markup") or {})  # no hay página siguiente
    assert len(first["text"]) <= TELEGRAM_TEXT_LIMIT
    assert len(second["text"]) <= TELEGRAM_TEXT_LIMIT


def test_los_mensajes_largos_se_parten_por_lineas_sin_perder_texto():
    from app.services.channel import _split_telegram_text

    text = "\n".join(["Dato del producto " * 8] * 60)  # supera el límite de la Bot API
    chunks = _split_telegram_text(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_TEXT_LIMIT for chunk in chunks)
    assert "\n".join(chunks) == text  # solo se parte, no se pierde ni se reordena nada


def test_los_mensajes_largos_conservan_el_html_balanceado():
    from app.services.channel import _split_telegram_text

    lines: list[str] = []
    for n in range(1, 200):
        lines += [
            f"{n}. <s><b>Producto &amp; {n}</b></s>",
            "   Cantidad: 1 caja",
            "   Reportado por: Ana",
            "   Estado: 🛒 En carrito",
            "   1 aviso",
            "",
        ]
    text = "\n".join(lines)
    chunks = _split_telegram_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= TELEGRAM_TEXT_LIMIT
        # Cada trozo sigue siendo HTML válido: ninguna etiqueta queda abierta.
        assert chunk.count("<b>") == chunk.count("</b>")
        assert chunk.count("<s>") == chunk.count("</s>")
    # No se pierde ni se reordena ninguna línea con contenido; solo puede cambiar una línea
    # vacía justo en el corte entre mensajes.
    assert [ln for ln in "\n".join(chunks).split("\n") if ln] == [ln for ln in text.split("\n") if ln]


async def test_chat_no_autorizado_se_ignora_e_informa_su_id(make_rt, sessions, seed, tg_api):
    rt = make_rt()
    res = await handle_update(rt, text_update(900, "777", "falta ibuprofeno"), now=T0)
    await after_handle(rt, res)
    assert res.status == "unauthorized" and "777" in tg_api.texts()[-1]
    assert (
        await count(sessions, InboundEvent) == 0 and await count(sessions, DemandObservation) == 0
    )


async def test_auxiliar_de_otra_organizacion_no_puede_elegirse(chat, sessions, seed, tg_api):
    from app.telegram.handler import handle_update as hu

    res = await hu(chat.rt, callback_update(950, CHAT, f"sel:{seed['intruso']}"), now=T0)
    await after_handle(chat.rt, res)
    assert await count(sessions, ActorSession) == 0 and "No pude cambiar" in " ".join(tg_api.texts()[-2:])


async def test_pin_individual_preparado_con_bloqueo_por_intentos(chat, sessions, seed):
    from app.core.security import hash_pin
    from app.models import AuxiliaryProfile, ChannelBinding
    from app.services import sessions as svc

    async with sessions() as s, s.begin():
        s.add(
            AuxiliaryProfile(
                user_id=seed["ana"], organization_id=seed["org"], pin_hash=hash_pin("4321")
            )
        )

    async def attempt(pin, now=T0):
        """Como lo haría un adaptador con PIN: verifica, CONFIRMA (persiste el intento) y luego decide."""
        async with sessions() as s, s.begin():
            b = (await s.execute(select(ChannelBinding))).scalar_one()
            err = await svc.verify_pin_attempt(s, seed["ana"], seed["org"], pin, now)
            sess = (
                None
                if err
                else await svc.select_auxiliary(
                    s, b, seed["ana"], now, timedelta(minutes=10), pin_verified=True
                )
            )
            return err, sess

    err, ok = await attempt("4321")
    assert err is None and ok.identity_level == "pin"  # nivel distinto de «declarada»
    codes = [(await attempt("0000"))[0].code for _ in range(5)]
    assert codes == ["PIN_INVALID"] * 4 + ["PIN_INVALID"]  # los intentos fallidos SÍ persisten
    err, none = await attempt("4321", now=mins(1))  # bloqueada aunque ahora sea correcto
    assert err.code == "PIN_LOCKED" and none is None
    err, again = await attempt("4321", now=mins(20))  # pasado el bloqueo vuelve a funcionar
    assert err is None and again.identity_level == "pin"
    async with sessions() as s:
        acts = (
            (await s.execute(select(AuditEvent.after).where(AuditEvent.action.like("session.%"))))
            .scalars()
            .all()
        )
    assert "4321" not in str(acts) and "0000" not in str(acts)  # el PIN jamás queda en auditoría


async def test_la_api_muestra_a_compras_el_candidato_de_catalogo_sin_confirmarlo(
    chat, sessions, seed, monkeypatch
):
    """El candidato «vitamina d 50000 UI» llega a la lista de compras como SUGERENCIA, no como dato."""
    from httpx import ASGITransport, AsyncClient

    from app.core.config import get_settings
    from app.db.session import get_session
    from app.main import create_app
    from app.services.actors import load_actor
    from app.services.catalog import create_pharma_identity

    async with sessions() as s, s.begin():
        await create_pharma_identity(
            s, await load_actor(s, seed["pao"]), "vitamina d 50000 UI cápsulas"
        )
    await chat.pick("ana")
    await chat.text("falta vitamina D 50000")
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    app = create_app()

    async def _s():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as api:
        r = await api.get("/api/v1/demand-items", headers={"X-Demo-User-Id": str(seed["pao"])})
    (item,) = r.json()["items"]
    (sug,) = item["suggestions"]
    assert (
        sug["source"] == "catalog"
        and sug["verified"] is True
        and sug["assignment"]["unit_expr"] == "ui"
    )
    assert (
        item["attributes"]["strengths"] == []
        and item["attributes"]["unassigned"][0]["label"] == "50000"
    )  # sin confirmar
    assert item["attributes"]["identity"]["pharmaceutical"]["complete"] is False


# ---- D-147: cantidad predeterminada, correcciones y unidades (2026-11-05) ----


def _buttons(tg_api):
    """Botones inline del último mensaje; vacío cuando la respuesta solo lleva teclado fijo."""
    rows = tg_api.sent()[-1].get("reply_markup", {}).get("inline_keyboard") or []
    return [b["text"] for row in rows for b in row]


async def _pending(sessions, status="pending_same_author_quantity"):
    async with sessions() as s:
        return (
            await s.execute(
                select(ChannelMessage)
                .where(ChannelMessage.confirmation_status == status)
                .order_by(ChannelMessage.received_at.desc())
            )
        ).scalars().all()


async def test_d147_1_reporte_sin_cantidad_registra_una_unidad_generica(
    chat, sessions, tg_api
):
    await chat.pick("ana")
    await chat.text("falta loratadina 10 mg", now=mins(1))
    assert "✅ Anotado: Loratadina 10 mg — 1 unidad." in tg_api.texts()[-1]
    (o,) = await observations(sessions)
    assert (o.declared_quantity, o.declared_unit) == (Decimal(1), "unit")
    assert o.interpretation["quantity_origin"] == "default"
    (item,) = await ctx_items(sessions)
    assert item.target_quantity is None  # la unidad predeterminada no es cantidad de compra


async def ctx_items(sessions):
    async with sessions() as s:
        return (await s.execute(select(DemandItem))).scalars().all()


async def test_d147_2_unidad_generica_mas_cajas_pide_aclaracion_sin_sumar(
    chat, sessions, tg_api
):
    await chat.pick("ana")
    await chat.text("falta loratadina 10 mg", now=mins(1))
    await chat.text("10 cajas de loratadina 10 mg", now=mins(2))
    text = tg_api.texts()[-1]
    assert "Ya tienes 1 unidad de Loratadina 10 mg" in text
    assert "¿Quieres agregar más o cambiar el total?" in text
    assert "En total 5 cajas" in text and "5 cajas más" in text
    assert _buttons(tg_api) == []  # sin botones: la respuesta se escribe
    assert len(await observations(sessions)) == 1  # nada se registró todavía
    (item,) = await ctx_items(sessions)
    assert item.target_quantity is None


async def test_d147_3_total_nuevo_confirmado_reemplaza_la_cantidad(
    chat, sessions, tg_api
):
    await chat.pick("ana")
    await chat.text("falta loratadina 10 mg", now=mins(1))
    await chat.text("10 cajas de loratadina 10 mg", now=mins(2))
    await chat.text("en total 10 cajas", now=mins(3))
    obs = await observations(sessions)
    assert len(obs) == 2 and (obs[-1].declared_quantity, obs[-1].declared_unit) == (
        Decimal(10),
        "box",
    )
    assert obs[-1].quantity_proposal["operation"] == "SET"
    assert "Listo, actualicé tu solicitud de Loratadina 10 mg a 10 cajas." in tg_api.texts()[-1]
    assert _buttons(tg_api) == []
    (item,) = await ctx_items(sessions)
    assert item.target_quantity is None
    assert not await _pending(sessions)


async def test_d147_4_adicional_confirmado_con_unidades_compatibles_suma(
    chat, sessions, tg_api
):
    await chat.pick("ana")
    await chat.text("falta loratadina 10 mg", now=mins(1))
    await chat.text("10 cajas de loratadina 10 mg", now=mins(2))
    await chat.text("en total 10 cajas", now=mins(3))
    await chat.text("15 cajas de loratadina 10 mg", now=mins(4))
    text = tg_api.texts()[-1]
    assert "¿Quieres agregar más o cambiar el total?" in text
    assert _buttons(tg_api) == []
    await chat.text("5 cajas más", now=mins(5))
    o = (await observations(sessions))[-1]  # la revisión más reciente
    assert (o.declared_quantity, o.declared_unit) == (Decimal(15), "box")
    assert o.quantity_proposal["operation"] == "ADD"
    assert o.quantity_proposal["value"] == "5"
    (item,) = await ctx_items(sessions)
    assert item.target_quantity is None  # Telegram no toca la cantidad de compra


async def test_d147_5_unidades_incompatibles_no_se_suman_ni_se_inventan(
    chat, sessions, tg_api
):
    await chat.pick("ana")
    await chat.text("2 cajas de loratadina 10 mg", now=mins(1))
    await chat.text("3 frascos de loratadina 10 mg", now=mins(2))
    assert "¿Quieres agregar más o cambiar el total?" in tg_api.texts()[-1]
    assert _buttons(tg_api) == []
    # «más» con unidades distintas se rechaza por texto: no hay equivalencia conocida.
    await chat.text("3 frascos más", now=mins(3))
    text = tg_api.texts()[-1]
    assert "No puedo sumar cajas con frascos" in text
    assert "En total 3 frascos" in text
    assert _buttons(tg_api) == []
    assert len(await observations(sessions)) == 1  # nada se sumó ni se inventó


async def test_d147_6_correccion_por_texto_libre_antes_de_confirmar(
    chat, sessions, tg_api
):
    await chat.pick("ana")
    await chat.text("falta loratadina 10 mg", now=mins(1))
    await chat.text("10 cajas de loratadina 10 mg", now=mins(2))
    await chat.text("me equivoqué, son 5", now=mins(3))
    assert "Listo, actualicé tu solicitud de Loratadina 10 mg a 5 cajas." in tg_api.texts()[-1]
    assert _buttons(tg_api) == []
    (o,) = (await observations(sessions))[-1:]
    assert (o.declared_quantity, o.declared_unit) == (Decimal(5), "box")
    assert o.quantity_proposal["operation"] == "SET"
    assert not await _pending(sessions)
    (item,) = await ctx_items(sessions)
    assert item.target_quantity is None


async def test_d147_7_repetir_el_mismo_aviso_no_incrementa(chat, sessions, tg_api):
    await chat.pick("ana")
    await chat.text("2 cajas de loratadina 10 mg", now=mins(1))
    await chat.text("2 cajas de loratadina 10 mg", now=mins(2))
    reply = tg_api.texts()[-1]
    # No se creó otro reporte: se dice claramente en vez de «Anotado».
    assert "ya está en tu lista" in reply and "no lo agregué de nuevo" in reply
    assert "Anotado" not in reply
    obs = await observations(sessions)
    assert len(obs) == 2 and obs[-1].declared_quantity == Decimal(2)
    # La cantidad solicitada vigente sigue siendo la última declarada: 2 cajas, no 4.
    async with sessions() as s:
        from app.services import queries

        (item,) = (await s.execute(select(DemandItem))).scalars().all()
        meta = await queries.report_meta(s, item.organization_id, [item])
    assert meta[item.id]["declared_quantity"] == 2


async def test_d147_8_un_solo_faltante_con_historial_preservado(chat, sessions):
    await chat.pick("ana")
    await chat.text("falta loratadina 10 mg", now=mins(1))
    await chat.text("10 cajas de loratadina 10 mg", now=mins(2))
    await chat.text("en total 10 cajas", now=mins(3))
    (item,) = await ctx_items(sessions)
    obs = await observations(sessions)
    assert len(obs) == 2
    assert [o.demand_item_id for o in obs] == [item.id, item.id]
    assert [o.interpretation.get("quantity_origin") for o in obs] == ["default", "explicit"]
    assert item.target_quantity is None


async def test_d147_9_solicitada_y_objetivo_siguen_separadas(chat, sessions, ctx):
    await chat.pick("ana")
    await chat.text("falta loratadina 10 mg", now=mins(1))
    await chat.text("10 cajas de loratadina 10 mg", now=mins(2))
    await chat.text("en total 10 cajas", now=mins(3))
    from app.domain.quantities import QuantityOp, QuantityUnit

    (item,) = await ctx_items(sessions)
    assert item.target_quantity is None  # la auxiliar no cambió la compra
    # Pao sí puede fijar la cantidad objetivo; la solicitada sigue siendo la de la auxiliar.
    await ctx.qty(
        item.id, QuantityOp.SET, Decimal(8), QuantityUnit.BOX, version=item.version
    )
    async with sessions() as s:
        from app.services import queries

        (fresh,) = (await s.execute(select(DemandItem))).scalars().all()
        meta = await queries.report_meta(s, fresh.organization_id, [fresh])
    assert fresh.target_quantity == Decimal(8) and fresh.quantity_unit == "box"
    assert meta[fresh.id]["declared_quantity"] == 10  # solicitada por la auxiliar, intacta


async def test_d147_10_en_carrito_no_se_cambia_la_cantidad(chat, sessions, tg_api, monkeypatch):
    from app.services import demand as demand_svc

    await chat.pick("ana")
    await chat.text("2 cajas de loratadina 10 mg", now=mins(1))
    await chat.text("5 cajas de loratadina 10 mg", now=mins(2))
    (pending,) = await _pending(sessions)

    async def in_cart(session, org, items):
        return {i.id: {"replenishment_state": "in_quote"} for i in items}

    monkeypatch.setattr(demand_svc.coverage_svc, "coverage_for_items", in_cart)
    await chat.text("8 cajas de loratadina 10 mg", now=mins(3))
    assert "está en carrito" in tg_api.texts()[-1]
    assert len(await observations(sessions)) == 1
    await chat.text("en total 12 cajas", now=mins(4))
    assert "está en carrito" in tg_api.texts()[-1]
    assert len(await observations(sessions)) == 1
    assert (await _pending(sessions))[0].id == pending.id  # la pregunta original sigue vigente


async def test_d147_11_reportes_independientes_por_auxiliar(chat, sessions, seed, tg_api):
    await chat.pick("ana")
    await chat.text("2 cajas de loratadina 10 mg", now=mins(1))
    await chat.text("Cambiar auxiliar", now=mins(2))
    await chat.pick("luis", now=mins(2))
    await chat.text("3 cajas de loratadina 10 mg", now=mins(3))
    (pending,) = await _pending(sessions, "pending_other_report")
    await chat.click(f"rp:ok:{pending.id}", now=mins(4))
    items = await ctx_items(sessions)
    assert len(items) == 2 and {i.reported_by for i in items} == {seed["ana"], seed["luis"]}
    by_author = {
        o.author_id: o.declared_quantity
        for o in await observations(sessions)
        if o.demand_item_id is not None
    }
    assert by_author == {seed["ana"]: Decimal(2), seed["luis"]: Decimal(3)}
    assert all(i.target_quantity is None for i in items)


async def test_d147_12_historico_sin_cantidad_no_se_reescribe_y_pide_confirmacion(
    chat, sessions, ctx, tg_api
):
    # Registro histórico: un faltante con cantidad NULL (como el caso real de Loratadina).
    r = await ctx.report("ana", "falta loratadina 10 mg")
    item_id = r.item.id
    await chat.pick("ana")
    await chat.text("10 cajas de loratadina 10 mg", now=mins(1))
    assert "No tenías una cantidad anotada" in tg_api.texts()[-1]
    assert _buttons(tg_api) == []
    await chat.text("en total 10 cajas", now=mins(2))
    obs = await observations(sessions)
    assert len(obs) == 2 and len(await ctx_items(sessions)) == 1
    historical = [o for o in obs if o.declared_quantity is None]
    assert len(historical) == 1  # la observación histórica no se toca
    confirmed = [o for o in obs if o.declared_quantity is not None]
    assert [(o.declared_quantity, o.demand_item_id) for o in confirmed] == [
        (Decimal(10), item_id)
    ]
    assert (await ctx_items(sessions))[0].target_quantity is None


async def test_d147_13_cancelar_por_texto_no_cambia_la_cantidad_solicitada(
    chat, sessions, tg_api
):
    await chat.pick("ana")
    await chat.text("2 cajas de loratadina 10 mg", now=mins(1))
    await chat.text("5 cajas de loratadina 10 mg", now=mins(2))
    assert "¿Quieres agregar más o cambiar el total?" in tg_api.texts()[-1]
    assert _buttons(tg_api) == []

    await chat.text("Cancelar", now=mins(3))
    assert "Cancelado: no cambié la cantidad solicitada." in tg_api.texts()[-1]
    assert _buttons(tg_api) == []
    obs = await observations(sessions)
    assert len(obs) == 1 and (obs[0].declared_quantity, obs[0].declared_unit) == (
        Decimal(2),
        "box",
    )
    assert await _pending(sessions) == []


async def test_d147_14_unidad_inicial_generica_se_aclara_por_texto_antes_de_sumar(
    chat, sessions, tg_api
):
    await chat.pick("ana")
    await chat.text("falta loratadina 10 mg", now=mins(1))  # 1 unidad genérica
    await chat.text("10 cajas de loratadina 10 mg", now=mins(2))
    await chat.text("10 cajas más", now=mins(3))
    text = tg_api.texts()[-1]
    assert "Para sumar necesito saber qué representa esa 1 unidad inicial de Loratadina 10 mg" in text
    assert "¿Es 1 caja?" in text
    assert _buttons(tg_api) == []
    assert len(await observations(sessions)) == 1  # sin inventar equivalencias

    await chat.text("sí, es 1 caja", now=mins(4))
    obs = await observations(sessions)
    assert len(obs) == 2
    assert (obs[-1].declared_quantity, obs[-1].declared_unit) == (Decimal(11), "box")
    assert obs[-1].quantity_proposal["operation"] == "ADD"
    assert "Listo, actualicé tu solicitud de Loratadina 10 mg a 11 cajas." in tg_api.texts()[-1]


# ---- interpretación de mensajes reales (texto) ----


async def test_caso_real_falta_paracetamol_500_mg_2_cajas(chat, sessions, tg_api):
    await chat.pick("ana")
    await chat.text("falta paracetamol 500 mg 2 cajas")
    reply = tg_api.texts()[-1]
    assert "✅ Anotado: Paracetamol 500 mg — 2 cajas." in reply
    assert "no puedo asegurar" not in reply and "revisión de compras" not in reply
    (o,) = await observations(sessions)
    assert (o.declared_quantity, o.declared_unit) == (Decimal(2), "box")
    assert o.match_status == "created_item"
    (item,) = await ctx_items(sessions)
    assert item.display_name == "Paracetamol 500 mg" and item.target_quantity is None


async def test_formas_habituales_se_interpretan_sin_pedir_aclaraciones(chat, sessions, tg_api):
    await chat.pick("ana")
    await chat.text("necesitamos 3 frascos de ibuprofeno suspensión", now=mins(1))
    (o,) = await observations(sessions)
    assert (o.declared_quantity, o.declared_unit) == (Decimal(3), "bottle")
    assert "✅ Anotado: Ibuprofeno suspension — 3 frascos." in tg_api.texts()[-1]
    assert "?" not in tg_api.texts()[-1]

    await chat.text("shampoo 400 ml", now=mins(2))
    shampoo = (await observations(sessions))[-1]
    assert (shampoo.declared_quantity, shampoo.declared_unit) == (Decimal(1), "unit")
    assert shampoo.interpretation["quantity_origin"] == "default"
    assert "✅ Anotado: Shampoo x 400 ml — 1 unidad." in tg_api.texts()[-1]


async def test_la_dosis_no_se_confunde_con_la_cantidad_solicitada(chat, sessions, tg_api):
    await chat.pick("ana")
    await chat.text("paracetamol 500 mg")
    (o,) = await observations(sessions)
    assert (o.declared_quantity, o.declared_unit) == (Decimal(1), "unit")
    assert o.parsed["strengths"][0]["measure"]["value"] == "500"
    assert "✅ Anotado: Paracetamol 500 mg — 1 unidad." in tg_api.texts()[-1]
