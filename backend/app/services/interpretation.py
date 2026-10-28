"""Carga el contexto verificado desde PostgreSQL y ejecuta el motor de interpretación."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.products import ParsedProduct
from app.domain.units import Measure
from app.interpretation.ai import AIInterpreter
from app.interpretation.engine import interpret_parsed, interpret_with, suggest_for
from app.interpretation.types import AliasRec, CatalogRec, CommercialRec, Interpretation, ItemRec
from app.models import CommercialReference, DemandItem, PharmaIdentity, ProductAlias


async def load_aliases(session: AsyncSession, org: uuid.UUID) -> list[AliasRec]:
    rows = (
        await session.execute(
            select(ProductAlias).where(
                ProductAlias.organization_id == org, ProductAlias.status == "active"
            )
        )
    ).scalars()
    return [
        AliasRec(
            a.id,
            a.kind,
            a.ingredient,
            a.source_value,
            a.assignment,
            tuple(a.confirmed_attributes or []),
        )
        for a in rows
    ]


async def load_catalog(
    session: AsyncSession, org: uuid.UUID, ingredients: set[str] | None = None
) -> list[CatalogRec]:
    q = select(PharmaIdentity).where(
        PharmaIdentity.organization_id == org, PharmaIdentity.status == "active"
    )
    if ingredients is not None:
        q = q.where(PharmaIdentity.ingredient.in_(ingredients))
    out = []
    for c in (await session.execute(q)).scalars():
        a = c.attributes
        out.append(CatalogRec(c.id, c.label, c.ingredient,
                              tuple(Measure.from_dict(s["measure"]) for s in a.get("strengths", [])),
                              (a.get("form") or {}).get("value"), (a.get("release") or {}).get("value")))  # fmt: skip
    return out


async def load_items(session: AsyncSession, org: uuid.UUID, cycle_id: uuid.UUID) -> list[ItemRec]:
    rows = (
        await session.execute(
            select(DemandItem.id, DemandItem.attributes).where(
                DemandItem.organization_id == org,
                DemandItem.cycle_id == cycle_id,
                DemandItem.status == "active",
            )
        )
    ).all()
    return [ItemRec(i, ParsedProduct.from_dict(a)) for i, a in rows]


async def load_commercial(session: AsyncSession, org: uuid.UUID) -> list[CommercialRec]:
    rows = (
        await session.execute(
            select(CommercialReference).where(
                CommercialReference.organization_id == org, CommercialReference.status == "active"
            )
        )
    ).scalars().all()  # fmt: skip
    cat = {c.id: c for c in await load_catalog(session, org)}
    return [CommercialRec(r.id, r.label, r.brand_key, r.laboratory, r.gtin, r.sanitary_registration,
                          cat.get(r.pharma_identity_id)) for r in rows]  # fmt: skip


async def _context(session, org, cycle_id):
    return dict(aliases=await load_aliases(session, org), catalog=await load_catalog(session, org),
                items=await load_items(session, org, cycle_id),
                commercial=await load_commercial(session, org))  # fmt: skip


async def interpret_text(session: AsyncSession, org: uuid.UUID, cycle_id: uuid.UUID, text: str,
                         ai: AIInterpreter | None = None) -> Interpretation:  # fmt: skip
    ctx = await _context(session, org, cycle_id)
    interp = interpret_with(text, **ctx)
    return await _with_ai(interp, ai, text, ctx)


async def interpret_product(session: AsyncSession, org: uuid.UUID, cycle_id: uuid.UUID,
                            parsed: ParsedProduct, ai: AIInterpreter | None = None) -> Interpretation:  # fmt: skip
    ctx = await _context(session, org, cycle_id)
    interp = interpret_parsed(parsed, **ctx)
    return await _with_ai(interp, ai, parsed.original_text, ctx)


async def _with_ai(
    interp: Interpretation, ai: AIInterpreter | None, text: str, ctx: dict
) -> Interpretation:
    """La IA (si existe) solo agrega candidatos NO verificados; `interp.parsed` no se toca."""
    if ai is None:
        return interp
    known = sorted(
        {i.parsed.ingredient for i in ctx["items"]} | {c.ingredient for c in ctx["catalog"]}
    )
    proposals = await ai.propose(text, interp.parsed, known)
    if proposals:
        extra = interpret_parsed(interp.parsed, **{**ctx, "ai_proposals": proposals})
        interp.candidates = extra.candidates
        interp.uncertainties = extra.uncertainties
        interp.ai_used = True
    return interp


async def suggestions_for_items(
    session: AsyncSession, org: uuid.UUID, items: list[DemandItem]
) -> dict:
    """Sugerencias del catálogo para necesidades con cifras sin unidad. Solo lectura; nunca confirma."""
    wanted = {
        i.ingredient
        for i in items
        if any(m.get("unit") is None for m in (i.attributes or {}).get("unassigned", []))
    }
    if not wanted:
        return {}
    catalog = await load_catalog(session, org, wanted)
    out = {}
    for i in items:
        if i.ingredient in wanted:
            cands = suggest_for(ParsedProduct.from_dict(i.attributes), catalog)
            if cands:
                out[i.id] = [c.to_dict() for c in cands]
    return out
