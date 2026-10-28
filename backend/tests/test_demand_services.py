"""Invariantes de demanda contra PostgreSQL real: deduplicación, cantidades, concurrencia."""

import asyncio
from decimal import Decimal as D

import pytest
from sqlalchemy import select, text

from app.core.errors import DomainError
from app.domain.quantities import QuantityOp as Op
from app.domain.quantities import QuantityUnit as U
from app.models import AuditEvent, DemandObservation, OutboxEvent, QuantityEvent
from app.services.demand import resolve_observation

pytestmark = pytest.mark.db


# ---- Escenarios pedidos ----


async def test_dos_auxiliares_mismo_ibuprofeno_una_necesidad_dos_observaciones(ctx):
    a = await ctx.report("ana", "falta ibuprofeno de 400 mg")
    b = await ctx.report("luis", "no queda ibuprofeno 400 mg")
    assert a.outcome == "created_item" and b.outcome == "attached_existing"
    items = await ctx.items()
    assert len(items) == 1
    assert await ctx.count(DemandObservation) == 2
    assert items[0].target_quantity is None  # y la cantidad no cambió (AC04)


async def test_aviso_repetido_no_aumenta_la_cantidad(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400 mg")
    await ctx.qty(r.item.id, Op.SET, D(5), U.BOX)
    for who in ("ana", "luis", "ana"):
        await ctx.report(who, "falta ibuprofeno 400 mg")
    (item,) = await ctx.items()
    assert (item.target_quantity, item.quantity_unit) == (D(5), "box")  # AC05
    assert await ctx.count(DemandObservation) == 4


async def test_cantidad_declarada_en_el_texto_es_solo_sugerencia(ctx):
    r = await ctx.report("ana", "necesitamos 5 cajas de ibuprofeno 400 mg")
    assert r.observation.declared_quantity == D(5) and r.observation.declared_unit == "box"
    (item,) = await ctx.items()
    assert item.target_quantity is None  # no se aplicó


async def test_400_y_600_permanecen_separados(ctx):
    await ctx.report("ana", "falta ibuprofeno 400 mg")
    await ctx.report("luis", "falta ibuprofeno 600 mg")
    await ctx.report("ana", "falta ibuprofeno 400 mg comprimidos")  # parcial vs 400: revisión
    await ctx.report("luis", "falta ibuprofeno 400 mg suspension")  # forma distinta de la anterior
    names = sorted(i.display_name for i in await ctx.items())
    assert "Ibuprofeno 400 mg" in names and "Ibuprofeno 600 mg" in names
    # nunca hay una necesidad que mezcle 400 y 600
    assert not any("400" in n and "600" in n for n in names)


async def test_cantidad_desconocida_permanece_nula(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    assert r.item.target_quantity is None and r.item.quantity_unit is None
    with pytest.raises(DomainError) as e:  # ADD sobre desconocida no inventa un punto de partida
        await ctx.qty(r.item.id, Op.ADD, D(5), U.BOX)
    assert e.value.code == "QUANTITY_UNKNOWN"
    (item,) = await ctx.items()
    assert item.target_quantity is None


async def test_add_repetido_con_misma_clave_no_suma_dos_veces(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    await ctx.qty(r.item.id, Op.SET, D(10), U.BOX, version=1)
    first = await ctx.qty(r.item.id, Op.ADD, D(5), U.BOX, version=2, key="add-1")
    again = await ctx.qty(r.item.id, Op.ADD, D(5), U.BOX, version=2, key="add-1")  # reenvío
    assert first.item.target_quantity == D(15) and again.replayed
    assert again.event.id == first.event.id
    (item,) = await ctx.items()
    assert item.target_quantity == D(15) and item.version == 3  # AC07
    assert await ctx.count(QuantityEvent) == 2  # SET + un solo ADD


async def test_misma_clave_con_otro_contenido_se_rechaza(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    await ctx.qty(r.item.id, Op.SET, D(10), U.BOX, key="k")
    with pytest.raises(DomainError) as e:
        await ctx.qty(r.item.id, Op.SET, D(99), U.BOX, key="k")
    assert e.value.code == "IDEMPOTENCY_KEY_REUSED" and e.value.status_code == 409


# ---- Reglas de cantidad, versiones y permisos ----


async def test_set_no_suma_y_clear_vuelve_a_desconocida(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    await ctx.qty(r.item.id, Op.SET, D(5), U.BOX, version=1)
    await ctx.qty(r.item.id, Op.SET, D(5), U.BOX, version=2)  # AC06: "necesitamos 5" sobre 5 → 5
    (item,) = await ctx.items()
    assert item.target_quantity == D(5)
    await ctx.qty(r.item.id, Op.CLEAR, version=3)
    (item,) = await ctx.items()
    assert item.target_quantity is None and item.quantity_unit is None


async def test_version_desactualizada_devuelve_conflicto_con_estado_actual(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    await ctx.qty(r.item.id, Op.SET, D(5), U.BOX, version=1)
    with pytest.raises(DomainError) as e:
        await ctx.qty(r.item.id, Op.SET, D(7), U.BOX, version=1)
    assert e.value.code == "VERSION_CONFLICT" and e.value.details["current_version"] == 2
    assert e.value.details["current_quantity"] == "5"


async def test_auxiliar_no_aplica_cantidades_ni_resuelve(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    with pytest.raises(DomainError) as e:
        await ctx.qty(r.item.id, Op.SET, D(5), U.BOX, who="ana")
    assert e.value.status_code == 403
    with pytest.raises(DomainError) as e:
        await ctx.run(
            "ana", resolve_observation, r.observation.id, demand_item_id=None, create_new=True
        )
    assert e.value.status_code == 403
    (item,) = await ctx.items()
    assert item.target_quantity is None


async def test_otra_organizacion_no_accede_a_la_necesidad(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    with pytest.raises(DomainError) as e:
        await ctx.qty(r.item.id, Op.SET, D(5), U.BOX, who="intruso")
    assert e.value.status_code == 404


async def test_sin_ciclo_abierto_se_rechaza(ctx, sessions):
    async with sessions() as s, s.begin():
        await s.execute(text("UPDATE purchase_cycle SET status='closed'"))
    with pytest.raises(DomainError) as e:
        await ctx.report("ana", "falta ibuprofeno 400")
    assert e.value.code == "NO_OPEN_CYCLE"


# ---- Ambigüedad y revisión ----


async def test_coincidencia_parcial_va_a_revision_sin_elegir_por_el_usuario(ctx):
    await ctx.report("ana", "falta ibuprofeno 400 mg")
    r = await ctx.report("luis", "falta ibuprofeno 400 mg comprimidos x 20")
    assert r.outcome == "needs_review" and r.item is None
    assert r.observation.candidates[0]["relation"] == "compatible_partial"
    assert len(await ctx.items()) == 1  # no se creó ni fusionó nada


async def test_resolver_asocia_o_crea_pero_nunca_une_incompatibles(ctx):
    base = await ctx.report("ana", "falta ibuprofeno 400 mg")
    review = await ctx.report("luis", "falta ibuprofeno 400 mg comprimidos x 20")
    ok = await ctx.run(
        "pao",
        resolve_observation,
        review.observation.id,
        demand_item_id=base.item.id,
        create_new=False,
    )
    assert ok.item.id == base.item.id and ok.outcome == "resolved_manual"
    with pytest.raises(DomainError) as e:  # ya resuelta
        await ctx.run(
            "pao",
            resolve_observation,
            review.observation.id,
            demand_item_id=base.item.id,
            create_new=False,
        )
    assert e.value.code == "ALREADY_RESOLVED"

    # una observación de 600 mg nunca puede asociarse a la necesidad de 400 mg
    six = await ctx.report("ana", "falta ibuprofeno 600 mg comprimidos")
    assert six.outcome == "created_item"
    dudosa = await ctx.report("luis", "falta ibuprofeno 600 mg")
    with pytest.raises(DomainError) as e:
        await ctx.run(
            "pao",
            resolve_observation,
            dudosa.observation.id,
            demand_item_id=base.item.id,
            create_new=False,
        )
    assert e.value.code == "INCOMPATIBLE_PRODUCTS"


async def test_varios_candidatos_parciales_exigen_aclaracion(ctx):
    await ctx.report("ana", "falta ibuprofeno 400 mg comprimidos")
    await ctx.report("ana", "falta ibuprofeno 400 mg suspension")
    r = await ctx.report("luis", "falta ibuprofeno 400")
    assert r.outcome == "needs_review" and len(r.observation.candidates) == 2  # AC10


# ---- Concurrencia y repetición de eventos ----


async def test_concurrentes_equivalentes_una_necesidad_n_observaciones(ctx):
    res = await asyncio.gather(
        *[
            ctx.report(
                "ana" if i % 2 else "luis", f"falta ibuprofeno {'de ' if i % 3 else ''}400 mg"
            )
            for i in range(12)
        ]
    )
    assert sorted(r.outcome for r in res).count("created_item") == 1  # AC11
    assert len(await ctx.items()) == 1
    assert await ctx.count(DemandObservation) == 12
    assert (await ctx.items())[0].target_quantity is None


async def test_mismo_evento_entregado_varias_veces_una_sola_observacion(ctx):
    res = await asyncio.gather(
        *[ctx.report("ana", "falta ibuprofeno 400", key="tg-evento-1") for _ in range(8)]
    )
    assert sum(not r.replayed for r in res) == 1  # AC12
    assert await ctx.count(DemandObservation) == 1 and len(await ctx.items()) == 1
    assert await ctx.count(AuditEvent) == 2  # item creado + observación, una sola vez


async def test_misma_clave_distinto_texto_en_paralelo_no_duplica(ctx):
    res = await asyncio.gather(
        ctx.report("ana", "falta ibuprofeno 400", key="k"),
        ctx.report("ana", "falta paracetamol 500", key="k"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, DomainError) and r.code == "IDEMPOTENCY_KEY_REUSED" for r in res) == 1
    assert await ctx.count(DemandObservation) == 1


async def test_add_concurrentes_con_la_misma_version_solo_uno_gana(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    await ctx.qty(r.item.id, Op.SET, D(10), U.BOX, version=1)
    res = await asyncio.gather(
        *[ctx.qty(r.item.id, Op.ADD, D(5), U.BOX, version=2) for _ in range(6)],
        return_exceptions=True,
    )
    assert sum(not isinstance(x, Exception) for x in res) == 1
    assert all(x.code == "VERSION_CONFLICT" for x in res if isinstance(x, DomainError))
    (item,) = await ctx.items()
    assert item.target_quantity == D(15)  # AC24: nunca "último en guardar gana"


async def test_add_concurrente_con_la_misma_clave_suma_una_vez(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    await ctx.qty(r.item.id, Op.SET, D(10), U.BOX, version=1)
    res = await asyncio.gather(
        *[ctx.qty(r.item.id, Op.ADD, D(5), U.BOX, version=2, key="add-x") for _ in range(6)]
    )
    assert sum(not x.replayed for x in res) == 1
    (item,) = await ctx.items()
    assert item.target_quantity == D(15)


# ---- Auditoría y outbox ----


async def test_auditoria_y_outbox_se_escriben_con_el_cambio(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    await ctx.report("luis", "falta ibuprofeno 400")
    await ctx.qty(r.item.id, Op.SET, D(5), U.BOX)
    async with ctx.sessions() as s:
        types = (
            (await s.execute(select(OutboxEvent.type).order_by(OutboxEvent.seq))).scalars().all()
        )
        payloads = (
            (
                await s.execute(
                    select(OutboxEvent.payload).where(
                        OutboxEvent.type == "demand.observation_added"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert types == [
        "demand.item_created",
        "demand.observation_added",
        "demand.observation_added",
        "demand.quantity_changed",
    ]
    assert all(p["quantity_changed"] is False for p in payloads)


async def test_si_falla_la_operacion_no_queda_auditoria_ni_evento(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    before = (await ctx.count(AuditEvent), await ctx.count(OutboxEvent))
    with pytest.raises(DomainError):
        await ctx.qty(r.item.id, Op.ADD, D(5), U.BOX)  # desconocida → rechazo
    assert (await ctx.count(AuditEvent), await ctx.count(OutboxEvent)) == before
