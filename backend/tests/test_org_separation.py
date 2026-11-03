"""Separación SIMULACIÓN / PRUEBA REAL: procedencia, organizaciones y aislamiento (PostgreSQL real).

Contexto: una auditoría halló que datos de una demo se mezclaron con la prueba real (misma organización,
mismos usuarios, mismo ciclo y un catálogo «verificado» creado por la demo). Estas pruebas fijan las
protecciones para que no vuelva a ocurrir.
"""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.errors import DomainError
from app.domain.products import ConfirmPatch
from app.interpretation.vision import SimulatedVisionInterpreter, VisionExtraction, VisionField
from app.models import Attachment, AuditEvent, DemandItem, DemandObservation, OutboxEvent
from app.services.actors import load_actor
from app.services.catalog import create_pharma_identity
from app.services.demand import confirm_item_attributes
from app.services.event_feed import fetch_events
from app.services.provenance import (
    allow_simulated_vision,
    is_simulation_chat,
    origin_for,
    validate_binding,
    vision_guard,
)
from app.telegram.handler import after_handle, handle_update
from tests.conftest import REAL_CHAT, Ctx
from tests.test_h1b_channel import T0, Chat, count, mins
from tests.tgsim import callback_update, fake_jpeg, photo_update, text_update

pytestmark = pytest.mark.db


@pytest.fixture
def real_ctx(sessions, real_seed):
    return Ctx(sessions, real_seed)


async def _exec(engine, sql, **p):
    async with engine.begin() as c:
        return await c.execute(text(sql), p)


# ---------- reglas puras ----------


def test_procedencia_por_tipo_de_organizacion():
    assert (
        origin_for("simulation") == "simulated"
        and origin_for("simulation", ai_derived=True) == "simulated"
    )
    assert (
        origin_for("real_test") == "captured"
        and origin_for("real_test", ai_derived=True) == "ai_derived"
    )
    assert origin_for("production") == "captured"
    assert (
        not allow_simulated_vision("real_test")
        and not allow_simulated_vision("legacy_mixed")
        and allow_simulated_vision("simulation")
    )


def test_chats_ficticios_reservados_a_simulacion():
    assert (
        is_simulation_chat("5550001")
        and is_simulation_chat(5550002)
        and not is_simulation_chat("6358481229")
    )
    validate_binding("5550001", "simulation")
    validate_binding("6358481229", "real_test")
    validate_binding(
        "5550001", "legacy_mixed"
    )  # organización previa a la separación: no se bloquea
    for chat, kind, code in [("5550001", "real_test", "SIMULATION_CHAT_RESERVED"), ("5550001", "production", "SIMULATION_CHAT_RESERVED"),
                             ("6358481229", "simulation", "REAL_CHAT_IN_SIMULATION")]:  # fmt: skip
        with pytest.raises(DomainError) as e:
            validate_binding(chat, kind)
        assert e.value.code == code


def test_vision_simulada_exige_que_todos_los_chats_sean_de_simulacion():
    vision_guard("simulated", ["simulation", "simulation"])
    vision_guard("none", ["real_test"])
    with pytest.raises(DomainError):
        vision_guard("simulated", ["simulation", "real_test"])


# ---------- triggers de la base ----------


async def test_la_base_rechaza_procedencia_incoherente_con_la_organizacion(engine, seed, real_seed):
    ins = """INSERT INTO demand_item (organization_id, cycle_id, ingredient, display_name, data_origin)
             SELECT :o, c.id, 'x', 'x', :d FROM purchase_cycle c WHERE c.organization_id = :o"""
    await _exec(engine, ins, o=seed["org"], d="simulated")  # simulación + simulado: ok
    await _exec(engine, ins, o=real_seed["org"], d="captured")
    await _exec(engine, ins, o=real_seed["org"], d="ai_derived")
    for org, origin, msg in [(seed["org"], "captured", "organización de simulación"), (seed["org"], "unclassified", "organización de simulación"),
                             (real_seed["org"], "simulated", "no admite data_origin=simulated"),
                             (real_seed["org"], "unclassified", "no admite data_origin=unclassified")]:  # fmt: skip
        with pytest.raises(IntegrityError, match=msg):
            await _exec(engine, ins, o=org, d=origin)


async def test_organizacion_legacy_admite_todo_y_las_demas_no_cambian_de_tipo(engine, seed):
    await _exec(engine, "INSERT INTO organization (name, kind) VALUES ('legacy', 'legacy_mixed')")
    oid = (await _exec(engine, "SELECT id FROM organization WHERE name='legacy'")).scalar()
    await _exec(
        engine, "INSERT INTO purchase_cycle (organization_id, name) VALUES (:o, 'c')", o=oid
    )
    for d in ("simulated", "captured", "unclassified"):
        await _exec(engine, "INSERT INTO demand_item (organization_id, cycle_id, ingredient, display_name, data_origin) "
                            "SELECT :o, id, 'x', 'x', :d FROM purchase_cycle WHERE organization_id=:o", o=oid, d=d)  # fmt: skip
    with pytest.raises(DBAPIError, match="inmutable"):  # una simulación no puede «volverse» real
        await _exec(engine, "UPDATE organization SET kind='real_test' WHERE id=:o", o=seed["org"])
    with pytest.raises(IntegrityError):  # toda organización nueva debe declarar su tipo
        await _exec(engine, "INSERT INTO organization (name) VALUES ('sin tipo')")
    with pytest.raises(IntegrityError):
        await _exec(engine, "INSERT INTO organization (name, kind) VALUES ('tipo raro', 'demo')")


async def test_el_chat_real_no_puede_caer_en_simulacion_ni_el_ficticio_en_real(
    engine, seed, real_seed
):
    ins = "INSERT INTO channel_binding (organization_id, external_chat_id) VALUES (:o, :c)"
    with pytest.raises(IntegrityError, match="ficticio"):
        await _exec(engine, ins, o=real_seed["org"], c="5550009")
    with pytest.raises(IntegrityError, match="solo admite chats ficticios"):
        await _exec(engine, ins, o=seed["org"], c="777")
    await _exec(engine, ins, o=seed["org"], c="5550009")  # ficticio en simulación: ok
    with pytest.raises(IntegrityError):  # un chat no puede estar activo en dos organizaciones
        await _exec(
            engine,
            "UPDATE channel_binding SET external_chat_id=:c WHERE organization_id=:o",
            c=REAL_CHAT,
            o=seed["org"],
        )


async def test_un_chat_se_puede_reasignar_tras_archivar_su_binding_anterior(engine, real_seed):
    await _exec(
        engine,
        "UPDATE channel_binding SET status='inactive' WHERE external_chat_id=:c",
        c=REAL_CHAT,
    )
    await _exec(
        engine,
        "INSERT INTO channel_binding (organization_id, external_chat_id) VALUES (:o, :c)",
        o=real_seed["org"],
        c=REAL_CHAT,
    )
    with pytest.raises(IntegrityError):  # pero no dos activos a la vez
        await _exec(
            engine,
            "INSERT INTO channel_binding (organization_id, external_chat_id) VALUES (:o, :c)",
            o=real_seed["org"],
            c=REAL_CHAT,
        )


# ---------- servicios: procedencia y aislamiento ----------


async def test_la_procedencia_la_decide_la_organizacion(ctx, real_ctx, sessions):
    sim = await ctx.report("ana", "falta ibuprofeno 400 mg")
    real = await real_ctx.report("ana", "falta ibuprofeno 400 mg")
    assert (sim.observation.data_origin, sim.item.data_origin) == ("simulated", "simulated")
    assert (real.observation.data_origin, real.item.data_origin) == ("captured", "captured")


async def test_el_mismo_texto_en_ambas_organizaciones_crea_necesidades_separadas(
    ctx, real_ctx, sessions, seed, real_seed
):
    a = await ctx.report("ana", "falta vitamina d 50000 ui")
    b = await real_ctx.report("ana", "falta vitamina d 50000 ui")
    assert (
        a.outcome == b.outcome == "created_item" and a.item.id != b.item.id
    )  # una simulación NUNCA se fusiona con lo real
    async with sessions() as s:
        orgs = dict((await s.execute(select(DemandItem.id, DemandItem.organization_id))).all())
    assert orgs[a.item.id] == seed["org"] and orgs[b.item.id] == real_seed["org"]
    # Dentro de la misma organización, cada autor conserva su reporte (Modelo A). La
    # deduplicación es por autor: nunca se adjunta al reporte de otra persona.
    own = await real_ctx.report("ana", "no queda vitamina d 50000 ui")
    assert own.outcome == "attached_existing" and own.item.id == b.item.id
    other = await real_ctx.report("luis", "no queda vitamina d 50000 ui")
    assert other.outcome == "created_item" and other.item.id != b.item.id
    assert other.item.organization_id == real_seed["org"]


async def test_un_catalogo_de_simulacion_no_influye_en_los_avisos_reales(
    ctx, real_ctx, sessions, seed
):
    """Reproduce la contaminación hallada: el catálogo «verificado» de la demo aparecía como candidato real."""
    async with sessions() as s, s.begin():
        row = await create_pharma_identity(
            s, await load_actor(s, seed["pao"]), "vitamina d 50000 UI cápsulas"
        )
    assert row.data_origin == "simulated"
    sim = await ctx.report("ana", "falta vitamina d 50000")
    real = await real_ctx.report("ana", "falta vitamina d 50000")
    assert [c["source"] for c in sim.observation.interpretation["candidates"]] == ["catalog"]
    assert (
        real.observation.interpretation["candidates"] == []
    )  # la organización real no ve el catálogo simulado


async def test_lista_eventos_y_auditoria_se_acotan_a_la_organizacion(
    ctx, real_ctx, sessions, seed, real_seed
):
    await ctx.report("ana", "falta ibuprofeno 400 mg")
    await ctx.report("luis", "falta paracetamol 500 mg")
    await real_ctx.report("ana", "falta omeprazol 20 mg")
    async with sessions() as s:
        sim_actor, real_actor = (
            await load_actor(s, seed["pao"]),
            await load_actor(s, real_seed["pao"]),
        )
        sim_ev = await fetch_events(s, sim_actor)
        real_ev = await fetch_events(s, real_actor)
        assert {e["organization_id"] for e in sim_ev} == {str(seed["org"])}
        assert {e["organization_id"] for e in real_ev} == {str(real_seed["org"])}
        assert (
            len(sim_ev) == 4 and len(real_ev) == 2
        )  # 2 avisos de simulación (+2 ítems) y 1 real (+1 ítem)
        assert not {e["event_id"] for e in sim_ev} & {e["event_id"] for e in real_ev}
        after = sim_ev[1]["seq"]
        assert [e["seq"] for e in await fetch_events(s, sim_actor, after_seq=after)] == [
            e["seq"] for e in sim_ev[2:]
        ]  # cursor
        # cada fila de auditoría pertenece a la organización de la entidad que describe
        bad = (
            await s.execute(
                text("""
            SELECT count(*) FROM audit_event a JOIN demand_observation o ON o.id = a.entity_id AND a.entity_type='demand_observation'
            WHERE a.organization_id <> o.organization_id""")
            )
        ).scalar_one()
        assert bad == 0
        outbox_orgs = (
            await s.execute(
                select(OutboxEvent.organization_id, func.count()).group_by(
                    OutboxEvent.organization_id
                )
            )
        ).all()
        assert {o for o, _ in outbox_orgs} == {seed["org"], real_seed["org"]}


async def test_un_auxiliar_de_la_otra_organizacion_no_puede_reportar_ni_leer(
    ctx, real_ctx, seed, real_seed
):
    item = (await ctx.report("ana", "falta ibuprofeno 400 mg")).item
    from app.domain.quantities import QuantityOp, QuantityUnit

    with pytest.raises(
        DomainError
    ) as e:  # el Pao real intenta fijar la cantidad de un ítem de la simulación
        await real_ctx.run("pao", __import__("app.services.quantities", fromlist=["x"]).apply_quantity_command, item.id,
                           operation=QuantityOp.SET, value=5, unit=QuantityUnit.BOX, reason="x", idempotency_key="k-cross-1", expected_version=1)  # fmt: skip
    assert e.value.status_code == 404


# ---------- canal: la simulación jamás escribe en lo real ----------


class RealChat(Chat):
    """Conversación en el chat REAL (organización real_test)."""

    async def send(self, update_fn, *a, now=T0, deliver=True, **kw):
        self.uid += 1
        res = await handle_update(self.rt, update_fn(self.uid, REAL_CHAT, *a, **kw), now=now)
        if deliver:
            await after_handle(self.rt, res)
        return res

    def pick(self, who, now=T0):
        return self.send(callback_update, f"sel:{self.seed[who]}", now=now)


@pytest.fixture
def real_chat(make_rt, real_seed, tg_api):
    def build(vision=None):
        return RealChat(make_rt(vision=vision), real_seed, tg_api)

    return build


SIM_EXT = VisionExtraction("simulated", "simulator", "simulated-v0", "clear", (
    VisionField("product_name", "Migranol®", .97), VisionField("form", "comprimidos recubiertos", .95), VisionField("content", "10 comprimidos", .95)))  # fmt: skip
LIVE_EXT = VisionExtraction("live", "proveedor-x", "modelo-y", "clear", (
    VisionField("ingredient", "Vitamina D3", .95), VisionField("strength", "50.000 UI", .93), VisionField("form", "cápsulas", .9)))  # fmt: skip


async def test_una_lectura_simulada_se_rechaza_en_la_organizacion_real(real_chat, sessions, tg_api):
    c = real_chat(SimulatedVisionInterpreter({"*": SIM_EXT}))
    await c.pick("ana")
    tg_api.files["f"] = fake_jpeg(1)
    await c.send(photo_update, "f")
    async with sessions() as s:
        a = (await s.execute(select(Attachment))).scalar_one()
        audit = (
            (
                await s.execute(
                    select(AuditEvent.action).where(
                        AuditEvent.action == "attachment.simulation_rejected"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert (
        a.status == "error"
        and a.error == "simulated_vision_not_allowed_outside_simulation_org"
        and a.data_origin == "captured"
    )
    assert (
        await count(sessions, DemandObservation) == 0 and await count(sessions, DemandItem) == 0
    )  # nada contaminó lo real
    assert audit == ["attachment.simulation_rejected"]


async def test_la_misma_lectura_simulada_si_funciona_en_la_organizacion_de_simulacion(
    make_rt, seed, chan, tg_api, sessions
):
    c = Chat(make_rt(SimulatedVisionInterpreter({"*": SIM_EXT})), seed, tg_api)
    await c.pick("ana")
    tg_api.files["f"] = fake_jpeg(1)
    await c.send(photo_update, "f")
    async with sessions() as s:
        a = (await s.execute(select(Attachment))).scalar_one()
        o = (await s.execute(select(DemandObservation))).scalar_one()
    assert a.status == "identified" and (a.data_origin, a.extraction_provenance) == (
        "simulated",
        "simulated",
    )
    assert o.data_origin == "simulated"


class LiveVision:
    """Doble de un proveedor REAL (mode=live) para probar la clasificación `ai_derived`. No es una IA."""

    async def extract(self, image, mime, caption):
        return LIVE_EXT


async def test_lo_derivado_por_un_modelo_real_es_ai_derived_y_lo_escrito_es_captured(
    real_chat, sessions, tg_api
):
    c = real_chat(LiveVision())
    await c.pick("ana")
    await c.send(text_update, "falta paracetamol 500 mg")
    tg_api.files["f"] = fake_jpeg(2)
    await c.send(photo_update, "f")
    async with sessions() as s:
        obs = {o.source: o for o in (await s.execute(select(DemandObservation))).scalars()}
        att = (await s.execute(select(Attachment))).scalar_one()
        items = {i.display_name: i for i in (await s.execute(select(DemandItem))).scalars()}
    assert (
        obs["telegram_text"].data_origin == "captured"
        and obs["telegram_photo"].data_origin == "ai_derived"
    )
    assert (att.data_origin, att.extraction_provenance, att.extraction_mode) == (
        "captured",
        "ai_derived",
        "live",
    )  # el archivo es captura; la lectura, IA
    assert {i.data_origin for i in items.values()} == {"captured", "ai_derived"}


async def test_la_procedencia_no_es_confianza_confirmar_no_cambia_el_origen(
    real_chat, real_ctx, sessions, tg_api, real_seed
):
    c = real_chat(LiveVision())
    await c.pick("ana")
    tg_api.files["f"] = fake_jpeg(2)
    await c.send(photo_update, "f")
    async with sessions() as s:
        item = (await s.execute(select(DemandItem))).scalar_one()
    assert (
        item.data_origin == "ai_derived" and item.attributes["strengths"][0]["basis"] == "inferred"
    )
    new = await real_ctx.run(
        "pao",
        confirm_item_attributes,
        item.id,
        expected_version=1,
        patch=ConfirmPatch(confirm_inferred={"strength", "form"}),
    )
    assert (
        new.attributes["strengths"][0]["basis"] == "confirmed"
    )  # confianza: confirmada por una persona
    assert new.data_origin == "ai_derived"  # procedencia: sigue siendo derivada por IA
    assert new.attributes["strengths"][0]["origin"] == "image"


async def test_el_chat_ficticio_y_el_real_no_comparten_sesiones_ni_mensajes(
    make_rt, seed, real_seed, chan, tg_api, sessions
):
    sim = Chat(make_rt(), seed, tg_api)
    real = RealChat(make_rt(), real_seed, tg_api)
    await sim.pick("ana", now=T0)  # hay una sesión activa en el chat de simulación…
    await real.text(
        "falta ibuprofeno 400 mg", now=mins(1)
    )  # …que NO identifica a nadie en el chat real
    async with sessions() as s:
        pend = (
            await s.execute(
                text(
                    "SELECT count(*) FROM channel_message WHERE attribution_status='pending_attribution'"
                )
            )
        ).scalar_one()
        sess_orgs = (
            (await s.execute(text("SELECT DISTINCT organization_id FROM actor_session")))
            .scalars()
            .all()
        )
        msg_org = (
            (await s.execute(text("SELECT organization_id FROM channel_message"))).scalars().all()
        )
    assert pend == 1 and sess_orgs == [seed["org"]] and msg_org == [real_seed["org"]]
    assert await count(sessions, DemandObservation) == 0 and await count(sessions, DemandItem) == 0


# ---------- esquema: toda tabla de negocio está acotada a su organización ----------


async def test_toda_tabla_con_organization_id_lo_exige_y_tiene_clave_foranea(engine):
    async with engine.connect() as c:
        rows = (
            await c.execute(
                text("""
            SELECT c.table_name, c.is_nullable,
                   EXISTS (SELECT 1 FROM information_schema.table_constraints tc
                           JOIN information_schema.key_column_usage k ON k.constraint_name = tc.constraint_name
                           WHERE tc.table_name = c.table_name AND tc.constraint_type='FOREIGN KEY' AND k.column_name='organization_id') AS has_fk
            FROM information_schema.columns c
            WHERE c.table_schema='public' AND c.column_name='organization_id' AND c.table_name <> 'organization'""")
            )
        ).all()
    names = {r[0] for r in rows}
    assert {
        "demand_item",
        "demand_observation",
        "audit_event",
        "outbox_event",
        "channel_binding",
        "actor_session",
        "attachment",
        "inbound_event",
        "channel_message",
        "outbound_message",
        "product_alias",
        "pharma_identity",
        "commercial_reference",
        "quantity_event",
        "app_user",
        "purchase_cycle",
    } <= names
    assert all(r[1] == "NO" and r[2] for r in rows), [r for r in rows if r[1] != "NO" or not r[2]]
