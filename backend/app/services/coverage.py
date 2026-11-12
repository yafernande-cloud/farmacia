"""Cobertura derivada de un faltante (§12.3, D-120/D-121). Nunca se almacena.

Suma dos fuentes sin contar dos veces la misma línea:

  1. lo **cotizado** en líneas activas de carritos activos de cotizaciones `draft`;
  2. lo **comprado** en compras `current` no anuladas.

C5-A implementa la primera fuente. Las compras llegan con C5-B (migración 0019): hasta entonces
`purchased` es 0 porque la tabla no existe, no porque se asuma que no se compró. La consulta de
compras se añade en `_purchased_totals` sin cambiar la forma de este módulo ni la de la respuesta.

Desde 0017 una línea comercial puede cubrir **varios reportes independientes**. La fuente cotizada
se lee de `quote_line_allocation`, no de `quote_line.demand_item_id`: cada asignación dice cuánto
de esa línea cubre a ese reporte. Cuando C5-B exista, una línea con compra vigente usará
`purchase_line_allocation` y no su asignación de cotización: nunca se suman ambas.

Reglas que se respetan aquí:
  * `pending_quantity` es lo que falta **cotizar**; `pending_purchase_quantity`, lo que falta
    **comprar** (D-127). No son el mismo saldo.
  * Cantidad pedida desconocida ⇒ ambos saldos `NULL`, nunca 0 ni 1 (V-06).
  * Cotizar cubre de forma provisional y **no cierra**: `is_pending` sigue `true` mientras falte
    compra confirmada. Solo una compra confirmada que cubra lo pedido cierra (V-39/V-40).
  * Un carrito `dismissed` o una cotización `closed` no cubren (F-3, §5.4).
"""

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DemandItem, Quote, QuoteCart, QuoteLine, QuoteLineAllocation

ZERO = Decimal(0)


def quoted_totals_subquery(org: uuid.UUID):
    """Suma cotizada por reporte: asignaciones de líneas activas, carrito activo y cotización `draft`.

    Solo cuentan asignaciones a reportes `active`: uno cancelado conserva su historial pero ya no
    se cubre. La cantidad asignada es explícita y nunca supera la cantidad de la línea (servicio).
    """
    return (
        select(
            QuoteLineAllocation.demand_item_id.label("demand_item_id"),
            func.sum(QuoteLineAllocation.quantity).label("quantity"),
        )
        .join(QuoteLine, QuoteLine.id == QuoteLineAllocation.quote_line_id)
        .join(QuoteCart, QuoteCart.id == QuoteLine.quote_cart_id)
        .join(Quote, Quote.id == QuoteCart.quote_id)
        .join(DemandItem, DemandItem.id == QuoteLineAllocation.demand_item_id)
        .where(
            QuoteLineAllocation.organization_id == org,
            QuoteLine.status == "active",
            QuoteCart.status == "active",
            Quote.status == "draft",
            DemandItem.status == "active",
        )
        .group_by(QuoteLineAllocation.demand_item_id)
        .subquery()
    )


async def _quoted_totals(
    session: AsyncSession, org: uuid.UUID, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            select(QuoteLineAllocation.demand_item_id, func.sum(QuoteLineAllocation.quantity))
            .join(QuoteLine, QuoteLine.id == QuoteLineAllocation.quote_line_id)
            .join(QuoteCart, QuoteCart.id == QuoteLine.quote_cart_id)
            .join(Quote, Quote.id == QuoteCart.quote_id)
            .join(DemandItem, DemandItem.id == QuoteLineAllocation.demand_item_id)
            .where(
                QuoteLineAllocation.organization_id == org,
                QuoteLineAllocation.demand_item_id.in_(item_ids),
                QuoteLine.status == "active",
                QuoteCart.status == "active",
                Quote.status == "draft",
                DemandItem.status == "active",
            )
            .group_by(QuoteLineAllocation.demand_item_id)
        )
    ).all()
    return {item_id: total for item_id, total in rows if item_id is not None}


async def _purchased_totals(
    session: AsyncSession, org: uuid.UUID, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Compras `current` no anuladas vinculadas al faltante. C5-B (0019) completa esta lectura."""
    return {}


def _state(item: DemandItem, purchased: Decimal, quoted: Decimal) -> str:
    """Precedencia determinista de §12.6; solo se evalúa sobre faltantes `active`."""
    if item.status == "cancelled":
        return "cancelled"
    if item.status == "fulfilled":
        return "fulfilled"
    if item.status == "released":
        return "released"
    if purchased > 0:
        return "purchased"
    if quoted > 0:
        return "in_quote"
    return "pending"


async def coverage_for_items(
    session: AsyncSession, org: uuid.UUID, items: Sequence[DemandItem]
) -> dict[uuid.UUID, dict]:
    item_ids = [i.id for i in items]
    quoted = await _quoted_totals(session, org, item_ids)
    purchased = await _purchased_totals(session, org, item_ids)
    out: dict[uuid.UUID, dict] = {}
    for item in items:
        q = quoted.get(item.id, ZERO)
        p = purchased.get(item.id, ZERO)
        covered = q + p
        if item.target_quantity is None:
            pending = None
            pending_purchase = None
            is_pending = item.status == "active"
        else:
            pending = max(item.target_quantity - covered, ZERO)
            pending_purchase = max(item.target_quantity - p, ZERO)
            is_pending = item.status == "active" and pending_purchase > 0
        out[item.id] = {
            "coverage_quantity": str(covered),
            "quoted_quantity": str(q),
            "purchased_quantity": str(p),
            "pending_quantity": None if pending is None else str(pending),
            "pending_purchase_quantity": (
                None if pending_purchase is None else str(pending_purchase)
            ),
            "is_pending": is_pending,
            "replenishment_state": _state(item, p, q),
        }
    return out


async def coverage_for_ids(
    session: AsyncSession, org: uuid.UUID, item_ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, dict], dict[uuid.UUID, DemandItem]]:
    """Cobertura de una lista de ids. Devuelve también los faltantes para no repetir la consulta."""
    if not item_ids:
        return {}, {}
    items = (
        (
            await session.execute(
                select(DemandItem).where(
                    DemandItem.organization_id == org, DemandItem.id.in_(item_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    return await coverage_for_items(session, org, items), {i.id: i for i in items}
