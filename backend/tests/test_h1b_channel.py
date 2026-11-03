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
    assert "otra persona" in tg_api.texts()[-1] or "Ana" in tg_api.texts()[-1]
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


async def test_pao_fija_el_total_y_suma_solo_con_botones(chat, sessions, seed, tg_api, ctx):
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
    assert "¿Son 8 en total o 3 más?" in tg_api.texts()[-1]

    await chat.click(f"rp:add:{pending.id}", now=mins(3))
    (item,) = await ctx.items()
    assert item.target_quantity == Decimal(8)  # ADD explícito: 5 + 3, nunca 5 + 8
    assert "Cantidad actualizada: 8" in tg_api.texts()[-1]

    # Si la cantidad declarada es menor, solo se ofrece fijar el total (SET).
    await chat.text("necesitamos 6 cajas de ibuprofeno 400 mg", now=mins(4))
    async with sessions() as s:
        (pending,) = (
            await s.execute(
                select(ChannelMessage)
                .where(ChannelMessage.confirmation_status == "pending_same_author_quantity")
                .order_by(ChannelMessage.received_at.desc())
            )
        ).scalars().all()
    assert "¿Fijo el total en 6?" in tg_api.texts()[-1]
    assert all("Sumar" not in b["text"] for b in tg_api.last_buttons())
    await chat.click(f"rp:set:{pending.id}", now=mins(5))
    (item,) = await ctx.items()
    assert item.target_quantity == Decimal(6)


async def test_el_auxiliar_solo_deja_una_propuesta_y_no_aplica_la_cantidad(
    chat, sessions, seed, tg_api, ctx
):
    from app.domain.quantities import QuantityOp, QuantityUnit

    await chat.pick("ana")
    await chat.text("falta ibuprofeno 400 mg", now=mins(1))
    (item,) = await ctx.items()
    await ctx.qty(item.id, QuantityOp.SET, Decimal(5), QuantityUnit.BOX, version=item.version)

    await chat.text("necesitamos 8 cajas de ibuprofeno 400 mg", now=mins(2))
    async with sessions() as s:
        (pending,) = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.confirmation_status == "pending_same_author_quantity"
                )
            )
        ).scalars().all()
    await chat.click(f"rp:add:{pending.id}", now=mins(3))

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
    assert "no cambio cantidades" in tg_api.texts()[-1]


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


async def test_pendiente_se_atribuye_solo_con_confirmacion_explicita(chat, sessions, seed, tg_api):
    await chat.text("falta ibuprofeno 400 mg", now=T0)
    await chat.pick("luis", now=mins(2))
    assert await observations(sessions) == []  # identificarse NO atribuye lo pendiente
    assert "¿Los anoto a nombre de Luis?" in tg_api.texts()[-1]
    await chat.click("att:ok", now=mins(3))
    (o,) = await observations(sessions)
    assert o.author_id == seed["luis"]
    async with sessions() as s:
        m = (await s.execute(select(ChannelMessage))).scalar_one()
    assert m.attribution_basis == "confirmed_after_identification" and m.attributed_at is not None
    await chat.click("att:ok", now=mins(4))  # doble clic: sin efecto
    assert len(await observations(sessions)) == 1


async def test_pendiente_descartado_o_viejo_no_se_atribuye(chat, sessions, tg_api):
    await chat.text("falta ibuprofeno 400 mg", now=T0)
    await chat.pick("ana", now=mins(1))
    await chat.click("att:no", now=mins(1))
    assert await observations(sessions) == []
    await chat.text("falta paracetamol 500 mg", now=mins(20))  # sesión expirada: queda pendiente
    await chat.pick("luis", now=mins(60))  # recién una hora después
    await chat.click("att:ok", now=mins(61))
    assert await observations(sessions) == []  # demasiado viejo: no se atribuye a Luis
    assert "no se pueden atribuir" in tg_api.texts()[-1]
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
    assert sorted(st) == ["discarded_by_user", "stale_unattributed"]


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
    assert "Faltantes activos: 4" in lista["text"] and lista["text"].count("\n") == 3  # página de 3
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
