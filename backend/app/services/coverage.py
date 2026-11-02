"""Cobertura derivada de un faltante (§12.3, D-120/D-121). Nunca se almacena.

Suma dos fuentes sin contar dos veces la misma línea:

  1. lo **cotizado** en líneas activas de carritos activos de cotizaciones `draft`;
  2. lo **comprado** en compras `current` no anuladas.

C5-A implementa la primera fuente. Las compras llegan con C5-B (migración 0017): hasta entonces
`purchased` es 0 porque la tabla no existe, no porque se asuma que no se compró. La consulta de
compras se añade en `_purchased_totals` sin cambiar la forma de este módulo ni la de la respuesta.

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

from app.models import DemandItem, Quote, QuoteCart, QuoteLine

ZERO = Decimal(0)


def quoted_totals_subquery(org: uuid.UUID):
    """Suma cotizada por faltante: líneas activas, carrito activo y cotización `draft`."""
    return (
        select(
            QuoteLine.demand_item_id.label("demand_item_id"),
            func.sum(QuoteLine.quantity).label("quantity"),
        )
        .join(QuoteCart, QuoteCart.id == QuoteLine.quote_cart_id)
        .join(Quote, Quote.id == QuoteCart.quote_id)
        .where(
            QuoteLine.organization_id == org,
            QuoteLine.demand_item_id.is_not(None),
            QuoteLine.status == "active",
            QuoteCart.status == "active",
            Quote.status == "draft",
        )
        .group_by(QuoteLine.demand_item_id)
        .subquery()
    )


async def _quoted_totals(
    session: AsyncSession, org: uuid.UUID, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            select(QuoteLine.demand_item_id, func.sum(QuoteLine.quantity))
            .join(QuoteCart, QuoteCart.id == QuoteLine.quote_cart_id)
            .join(Quote, Quote.id == QuoteCart.quote_id)
            .where(
                QuoteLine.organization_id == org,
                QuoteLine.demand_item_id.in_(item_ids),
                QuoteLine.status == "active",
                QuoteCart.status == "active",
                Quote.status == "draft",
            )
            .group_by(QuoteLine.demand_item_id)
        )
    ).all()
    return {item_id: total for item_id, total in rows if item_id is not None}


async def _purchased_totals(
    session: AsyncSession, org: uuid.UUID, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Compras `current` no anuladas vinculadas al faltante. C5-B (0017) completa esta lectura."""
    return {}


def _state(item: DemandItem, purchased: Decimal, quoted: Decimal) -> str:
    """Precedencia determinista de §12.6; solo se evalúa sobre faltantes `active`."""
    if item.status == "cancelled":
        return "cancelled"
    if item.status == "fulfilled":
        return "fulfilled"
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
