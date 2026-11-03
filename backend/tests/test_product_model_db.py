"""Procedencia de atributos, alias verificados y confirmación humana, contra PostgreSQL real."""

import asyncio
from decimal import Decimal as D

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.errors import DomainError
from app.domain.products import ConfirmPatch, ParsedProduct
from app.domain.units import build_measure
from app.models import AuditEvent, DemandItem, OutboxEvent
from app.services.demand import (
    confirm_item_attributes,
    deactivate_alias,
    list_aliases,
    resolve_observation,
)

pytestmark = pytest.mark.db


def parsed(item: DemandItem) -> ParsedProduct:
    return ParsedProduct.from_dict(item.attributes)


async def confirm(ctx, item_id, version, who="pao", remember=False, **patch):
    return await ctx.run(who, confirm_item_attributes, item_id, expected_version=version,
                         patch=ConfirmPatch(**patch), remember_alias=remember)  # fmt: skip


# ---------- un número sin unidad queda pendiente; nada se asume ----------


async def test_cifra_sin_unidad_se_guarda_como_pendiente_no_como_mg(ctx):
    r = await ctx.report("ana", "falta ibuprofeno de 400")
    assert r.outcome == "created_item"
    p = parsed(r.item)
    assert p.strengths == () and [m.label for m in p.unassigned] == ["400"]
    assert r.item.resolved_identity_key is None
    assert "mg" not in r.item.display_name and "sin unidad" in r.item.display_name
    assert "mg" not in str(r.item.attributes)  # ni un solo "mg" inventado en el dato guardado


async def test_texto_original_se_conserva_y_no_se_puede_reescribir(ctx, engine):
    r = await ctx.report("ana", "  Falta IBUPROFENO de 400!!  ")
    assert r.observation.raw_text == "  Falta IBUPROFENO de 400!!  "  # tal cual, sin limpiar
    assert r.item.attributes["original_text"] == "Falta IBUPROFENO de 400!!"
    for col in ("raw_text", "parsed", "author_id"):
        value = {"raw_text": "'x'", "parsed": "'{}'::jsonb", "author_id": f"'{ctx.seed['luis']}'"}[
            col
        ]
        with pytest.raises(DBAPIError, match="inmutables"):
            async with engine.begin() as c:
                await c.execute(text(f"UPDATE demand_observation SET {col} = {value}"))


async def test_cifra_sin_unidad_contra_unidad_explicita_va_a_revision_con_motivo(ctx):
    await ctx.report("ana", "falta ibuprofeno de 400")
    r = await ctx.report("ana", "no queda ibuprofeno 400 mg")
    assert r.outcome == "needs_review" and r.item is None
    assert "unit_unconfirmed" in r.observation.candidates[0]["reasons"]
    assert len(await ctx.items()) == 1  # ni se fusionó ni se creó otra a escondidas


async def test_misma_incognita_repetida_se_asocia_y_sigue_pendiente(ctx):
    a = await ctx.report("ana", "falta ibuprofeno de 400")
    b = await ctx.report("ana", "no queda ibuprofeno 400")
    assert b.outcome == "attached_existing" and b.item.id == a.item.id
    assert parsed(b.item).unassigned and not parsed(b.item).identity_complete


async def test_cifras_distintas_sin_unidad_no_se_mezclan(ctx):
    await ctx.report("ana", "falta ibuprofeno 400")
    r = await ctx.report("luis", "falta ibuprofeno 600")
    assert r.outcome == "created_item" and len(await ctx.items()) == 2


async def test_cantidad_solicitada_contenido_y_potencia_no_se_mezclan(ctx):
    r = await ctx.report("ana", "necesitamos 5 cajas de ibuprofeno 400 mg comprimidos caja de 20")
    assert r.observation.declared_quantity == D(5) and r.observation.declared_unit == "box"
    assert r.item.target_quantity is None  # la cantidad pedida es solo sugerencia
    p = parsed(r.item)
    assert [s.measure.label for s in p.strengths] == ["400 mg"] and p.content.measure.value == 20
    assert r.item.resolved_identity_key  # identidad completa y explícita
    r2 = await ctx.report("ana", "falta ibuprofeno 400 mg comprimidos x 20")
    assert r2.outcome == "attached_existing" and r2.item.id == r.item.id


# ---------- inferencias: nunca se vuelven datos confirmados solas ----------


async def test_inferencia_no_se_fusiona_con_lo_explicito_hasta_confirmarse(ctx):
    a = await ctx.report("ana", "falta amoxicilina 1 g comprimidos x 20")
    assert a.item.resolved_identity_key is None  # potencia deducida del contexto
    b = await ctx.report("ana", "falta amoxicilina 1000 mg comprimidos x 20")
    assert (
        b.outcome == "needs_review"
    )  # misma cifra normalizada, pero hay una inferencia de por medio
    item = await confirm(ctx, a.item.id, 1, confirm_inferred={"strength"})
    assert parsed(item).strengths[0].basis.value == "confirmed" and item.resolved_identity_key
    c = await ctx.report("ana", "falta amoxicilina 1000 mg comprimidos x 20")
    assert c.outcome == "attached_existing"  # ahora sí: ambos sólidos y equivalentes


# ---------- confirmación humana y memoria verificada ----------


async def test_resolver_confirma_la_unidad_y_recuerda_sin_volver_a_preguntar(ctx):
    base = await ctx.report("ana", "falta ibuprofeno de 400")
    dudoso = await ctx.report("ana", "no queda ibuprofeno 400 mg")
    assert dudoso.outcome == "needs_review"
    ok = await ctx.run("pao", resolve_observation, dudoso.observation.id, demand_item_id=base.item.id,
                       create_new=False, apply_observed_attributes=True, remember_alias=True)  # fmt: skip
    p = parsed(ok.item)
    assert p.strengths[0].basis.value == "confirmed" and p.unassigned == ()  # Pao lo confirmó
    assert ok.item.version == 2 and ok.alias_created

    # a partir de ahora «ibuprofeno 400» a secas se resuelve solo: sin cola ni pregunta
    nxt = await ctx.report("ana", "falta ibuprofeno 400")
    assert nxt.outcome == "attached_existing" and nxt.item.id == base.item.id
    assert nxt.parsed.strengths[0].role_rule == "verified_alias"
    # la memoria cubre SOLO esa cifra y ese producto
    otro = await ctx.report("ana", "falta ibuprofeno 600")
    assert otro.outcome == "created_item" and parsed(otro.item).unassigned
    assert (await ctx.report("ana", "falta paracetamol 400")).item.attributes["unassigned"]


async def test_alias_es_revisable_y_desactivable_y_por_organizacion(ctx):
    base = await ctx.report("ana", "falta ibuprofeno de 400")
    dudoso = await ctx.report("ana", "falta ibuprofeno 400 mg")
    await ctx.run("pao", resolve_observation, dudoso.observation.id, demand_item_id=base.item.id,
                  create_new=False, apply_observed_attributes=True, remember_alias=True)  # fmt: skip
    (alias,) = await ctx.run("pao", list_aliases)
    assert (
        alias.assignment == {"role": "strength", "unit_expr": "mg"}
        and alias.confirmed_by == ctx.seed["pao"]
    )
    with pytest.raises(DomainError) as e:
        await ctx.run("ana", list_aliases)
    assert e.value.status_code == 403
    # otra organización no hereda la memoria
    ajeno = await ctx.report("intruso", "falta ibuprofeno 400")
    assert ajeno.item.attributes["unassigned"] and not ajeno.item.attributes["strengths"]
    await ctx.run("pao", deactivate_alias, alias.id)
    despues = await ctx.report("ana", "falta ibuprofeno 400")  # sin memoria vuelve a ser ambiguo
    assert (
        despues.outcome == "needs_review"
    )  # el ítem ya está confirmado en 400 mg: coincidencia no segura


async def test_auxiliar_no_confirma_atributos(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    with pytest.raises(DomainError) as e:
        await confirm(ctx, r.item.id, 1, who="ana", strengths=[build_measure(D(400), "mg")])
    assert e.value.status_code == 403


async def test_confirmar_exige_version_y_no_pisa_en_silencio(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    await confirm(ctx, r.item.id, 1, strengths=[build_measure(D(400), "mg")])
    with pytest.raises(DomainError) as e:
        await confirm(ctx, r.item.id, 1, form="comprimido")
    assert e.value.code == "VERSION_CONFLICT" and e.value.details["current_version"] == 2
    with pytest.raises(DomainError) as e:  # nada nuevo que confirmar
        await confirm(ctx, r.item.id, 2, strengths=[build_measure(D(400), "mg")])
    assert e.value.code == "NO_CHANGE"


async def test_confirmar_hasta_coincidir_con_otra_necesidad_se_bloquea(ctx):
    a = await ctx.report("ana", "falta ibuprofeno 400 mg comprimidos x 20")
    b = await ctx.report(
        "ana", "falta ibuprofeno 400 comprimidos x 20"
    )  # cifra sin unidad: revisión
    assert b.outcome == "needs_review"
    nuevo = await ctx.run(
        "pao", resolve_observation, b.observation.id, demand_item_id=None, create_new=True
    )
    assert nuevo.item.id != a.item.id
    with pytest.raises(DomainError) as e:  # confirmarla como 400 mg la haría idéntica a la de Ana
        await confirm(ctx, nuevo.item.id, 1, strengths=[build_measure(D(400), "mg")])
    assert e.value.code == "DUPLICATE_ITEM_EXISTS" and e.value.details["existing_item_id"] == str(
        a.item.id
    )


async def test_confirmacion_deja_auditoria_y_evento_sin_cambiar_cantidad(ctx):
    r = await ctx.report("ana", "falta ibuprofeno 400")
    await confirm(ctx, r.item.id, 1, strengths=[build_measure(D(400), "mg")], form="comprimido")
    async with ctx.sessions() as s:
        acts = (await s.execute(select(AuditEvent.action))).scalars().all()
        ev = (
            await s.execute(
                select(OutboxEvent.payload).where(
                    OutboxEvent.type == "demand.item_attributes_confirmed"
                )
            )
        ).scalar_one()
        item = (await s.execute(select(DemandItem))).scalar_one()
    assert "demand_item.attributes_confirmed" in acts and ev["quantity_changed"] is False
    assert item.target_quantity is None  # confirmar identidad no toca la cantidad


async def test_resolver_la_misma_observacion_en_paralelo_solo_una_vez(ctx):
    await ctx.report("ana", "falta ibuprofeno de 400")
    d = await ctx.report("ana", "falta ibuprofeno 400 mg")
    res = await asyncio.gather(
        *[
            ctx.run(
                "pao", resolve_observation, d.observation.id, demand_item_id=None, create_new=True
            )
            for _ in range(4)
        ],
        return_exceptions=True,
    )
    assert sum(not isinstance(x, Exception) for x in res) == 1
    assert all(
        isinstance(x, DomainError) and x.code == "ALREADY_RESOLVED"
        for x in res
        if isinstance(x, Exception)
    )
    assert len(await ctx.items()) == 2
