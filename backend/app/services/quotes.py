"""Cotizaciones, carritos y líneas (C5-A, E10) y observaciones comerciales (O1, E13).

Reglas que este módulo hace cumplir:

  * todas las operaciones son de `admin`; la organización sale del `Actor`, nunca del cuerpo;
  * la cotización cerrada no se edita: `409 QUOTE_CLOSED`;
  * el carrito nace con la primera línea y uno `dismissed` no se resucita en silencio;
  * repetir la misma petición **no suma**: deduplicación natural y `Idempotency-Key`;
  * el precio no se copia a la línea: se lee del `offer_snapshot` elegido (§3, V-03);
  * una línea con compras vigentes no se quita, ni se mueve, ni baja de lo comprado
    (`409 LINE_HAS_PURCHASES` / `409 QUANTITY_BELOW_PURCHASED`). C5-B crea las compras; la
    verificación existe aquí y hoy no encuentra ninguna;
  * la cobertura de faltantes es derivada y nunca se guarda (§12.3, V-21);
  * una observación comercial se guarda siempre y no se interpreta: O2–O4 no están implementados
    en C5-A, así que el consejo es `null`, no una IA simulada (D-116/D-132).

La evaluación de cada carrito usa E5 (`comparison.run_comparison`) con la oferta **elegida** fijada
por línea: el precio no se reapunta solo aunque exista una observación más nueva (V-04). El cálculo
se hace sin persistir la corrida: leer una cotización no llena las tablas de comparaciones.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.core.permissions import require_admin
from app.domain.matching import classify, identity_from_item, identity_from_query
from app.models import (
    AuditEvent,
    CommercialObservation,
    DemandItem,
    ObservationInterpretation,
    OfferSnapshot,
    Quote,
    QuoteCart,
    QuoteLine,
    QuoteLineAllocation,
    Supplier,
    TradeItem,
)
from app.services import comparison as comparison_svc
from app.services import coverage as coverage_svc
from app.services import demand as demand_svc
from app.services import events
from app.services.actors import Actor
from app.services.observation_views import observation_view
from app.services.provenance import org_kind, origin_for
from app.services.search import _latest_offers

UNITS = ("box", "package", "unit", "bottle")
MAX_TEXT = 2000
ZERO = Decimal(0)


# --------------------------------------------------------------------------- utilidades


def _now() -> datetime:
    return datetime.now(UTC)


def _dec(raw: Any) -> Decimal | None:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


def _quantity(raw: Any) -> Decimal:
    value = _dec(raw)
    if value is None or value <= 0 or value != value.to_integral_value():
        raise DomainError(
            "INVALID_QUANTITY", "La cantidad debe ser un entero mayor que cero.", status_code=422
        )
    return value


def _unit(raw: Any) -> str:
    if raw not in UNITS:
        raise DomainError("INVALID_UNIT", "La unidad de la cantidad no es válida.", status_code=422)
    return raw


def _qty(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _money(amount: Decimal | None, currency: str | None) -> dict | None:
    if amount is None or currency is None:
        return None
    return {"amount": str(amount.quantize(Decimal("0.01"))), "currency": currency}


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _json_safe(value: Any) -> Any:
    """Auditoría/outbox son JSONB: UUID, fechas y Decimal se guardan como texto."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _emit(session: AsyncSession, **kw: Any) -> None:
    for key in ("payload", "before", "after"):
        if kw.get(key) is not None:
            kw[key] = _json_safe(kw[key])
    events.emit(session, **kw)


def _require_version(expected: int | None, current: int) -> None:
    if expected is None:
        raise DomainError(
            "VERSION_REQUIRED",
            "Indica la versión actual del recurso (`version`).",
            status_code=428,
        )
    if expected != current:
        raise DomainError(
            "VERSION_CONFLICT",
            "El recurso cambió desde que lo leíste. Vuelve a cargarlo.",
            status_code=409,
            details={"current_version": current},
        )


async def _quote(session: AsyncSession, org: uuid.UUID, quote_id: uuid.UUID) -> Quote:
    row = (
        await session.execute(
            select(Quote).where(Quote.id == quote_id, Quote.organization_id == org)
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("La cotización")
    return row


def _ensure_draft(quote: Quote) -> None:
    if quote.status != "draft":
        raise DomainError(
            "QUOTE_CLOSED", "La cotización está cerrada. Reábrela para editarla.", status_code=409
        )


async def _cart(
    session: AsyncSession, org: uuid.UUID, quote: Quote, supplier_id: uuid.UUID
) -> QuoteCart:
    row = (
        await session.execute(
            select(QuoteCart).where(
                QuoteCart.quote_id == quote.id,
                QuoteCart.organization_id == org,
                QuoteCart.supplier_id == supplier_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("El carrito del proveedor")
    return row


async def _line(
    session: AsyncSession, org: uuid.UUID, quote: Quote, line_id: uuid.UUID, *, active: bool = True
) -> tuple[QuoteLine, QuoteCart]:
    row = (
        await session.execute(
            select(QuoteLine, QuoteCart)
            .join(QuoteCart, QuoteCart.id == QuoteLine.quote_cart_id)
            .where(
                QuoteLine.id == line_id,
                QuoteLine.organization_id == org,
                QuoteCart.quote_id == quote.id,
            )
        )
    ).first()
    if row is None or (active and row[0].status != "active"):
        raise not_found("La línea de la cotización")
    return row[0], row[1]


def _touch_quote(quote: Quote, now: datetime) -> None:
    quote.updated_at = now
    quote.version += 1


def _touch_cart(cart: QuoteCart, now: datetime) -> None:
    cart.updated_at = now
    cart.version += 1


async def _offer(session: AsyncSession, org: uuid.UUID, offer_id: uuid.UUID) -> OfferSnapshot:
    row = (
        await session.execute(
            select(OfferSnapshot).where(
                OfferSnapshot.id == offer_id, OfferSnapshot.organization_id == org
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("La oferta")
    return row


def _selected(offer: OfferSnapshot) -> dict:
    return {
        "offer_snapshot_id": offer.id,
        "price": _money(offer.price_amount, offer.currency),
        "observed_at": offer.observed_at,
        "review_status": offer.review_status,
    }


def _current(offer: OfferSnapshot) -> dict:
    return {
        "offer_snapshot_id": offer.id,
        "price": _money(offer.price_amount, offer.currency),
        "observed_at": offer.observed_at,
    }


def _price_state(
    selected: OfferSnapshot, current: OfferSnapshot | None, now: datetime
) -> tuple[str, list[str]]:
    """Estado del precio elegido. Reapuntar es una operación explícita, nunca automática."""
    causes: list[str] = []
    state = "current"
    if current is None:
        return "no_longer_offered", ["no_current_observation"]
    if selected.valid_to is not None and selected.valid_to < now:
        state = "stale"
        causes.append("valid_to_expired")
    if selected.valid_from is not None and selected.valid_from > now:
        state = "stale"
        causes.append("not_yet_valid")
    if selected.review_status != "accepted":
        if state == "current":
            state = "requires_review"
        causes.append("selected_needs_review")
    if (
        current.id != selected.id
        and current.review_status == "accepted"
        and current.observed_at > selected.observed_at
    ):
        if state == "current":
            state = "requires_review"
        causes.append("newer_observation_available")
    return state, causes


def _remainder(line: QuoteLine, quote: Quote) -> str:
    if line.pending_closed_at is not None:
        return "closed_by_user"
    if quote.status == "closed":
        return "closed_by_quote"
    return "open"


def _line_payload(
    line: QuoteLine,
    quote: Quote,
    *,
    selected: OfferSnapshot,
    current: OfferSnapshot | None,
    now: datetime,
    allocations: list[QuoteLineAllocation] = (),
) -> dict:
    state, causes = _price_state(selected, current, now)
    return {
        "line_id": line.id,
        "quote_id": quote.id,
        "quote_cart_id": line.quote_cart_id,
        "supplier_id": line.supplier_id,
        "offer_snapshot_id": line.offer_snapshot_id,
        "trade_item_id": line.trade_item_id,
        "quantity": {"value": _qty(line.quantity), "unit": line.quantity_unit},
        "status": line.status,
        "removed_at": line.removed_at,
        "removed_by": line.removed_by,
        # Atajo compatible: solo se llena cuando la línea tiene UNA asignación. La verdad está en
        # `demand_allocations`, que permite cubrir varios reportes independientes (D-135).
        "demand_item_id": line.demand_item_id,
        "coverage_relation": line.coverage_relation,
        "demand_allocations": [_allocation_payload(a) for a in allocations],
        "pending_closed_at": line.pending_closed_at,
        "pending_closed_by": line.pending_closed_by,
        "pending_closed_note": line.pending_closed_note,
        # C5-A no tiene compras: una línea está pendiente y su resto abierto o cerrado por la
        # cotización. C5-B completa estos derivados sin cambiar la forma de la respuesta.
        "purchase_state": "pending",
        "remainder": _remainder(line, quote),
        "selected": _selected(selected),
        "current": None if current is None else _current(current),
        "price_state": state,
        "causes": causes,
        "warnings": [],
        # O2–O4 no están implementados: sin interpretación no se inventa un consejo (D-116).
        "advice": None,
        "version": line.version,
        "added_by": line.added_by,
        "added_at": line.added_at,
        "updated_at": line.updated_at,
    }


def _cart_payload(cart: QuoteCart, supplier: Supplier | None, *, line_count: int) -> dict:
    return {
        "cart_id": cart.id,
        "supplier_id": cart.supplier_id,
        "supplier_name": supplier.name if supplier else None,
        "status": cart.status,
        "line_count": line_count,
        "version": cart.version,
        "created_at": cart.created_at,
        "updated_at": cart.updated_at,
    }


def _quote_summary(quote: Quote) -> dict:
    return {
        "quote_id": quote.id,
        "title": quote.title,
        "status": quote.status,
        "version": quote.version,
        "created_by": quote.created_by,
        "created_at": quote.created_at,
        "updated_at": quote.updated_at,
        "closed_by": quote.closed_by,
        "closed_at": quote.closed_at,
        "data_origin": quote.data_origin,
    }


def _derived_quote_state(quote: Quote) -> str:
    """Sin compras en C5-A: en preparación o cerrada sin compra (§4.2)."""
    return "closed_without_purchase" if quote.status == "closed" else "in_preparation"


# --------------------------------------------------------------------------- E10: cotizaciones


async def create_quote(session: AsyncSession, actor: Actor, *, title: str | None) -> Quote:
    require_admin(actor.roles)
    now = _now()
    clean = title.strip() if isinstance(title, str) else None
    quote = Quote(
        id=uuid.uuid4(),
        organization_id=actor.organization_id,
        title=clean or None,
        status="draft",
        created_by=actor.id,
        data_origin=origin_for(await org_kind(session, actor.organization_id)),
        created_at=now,
        updated_at=now,
    )
    session.add(quote)
    await session.flush()
    _emit(
        session,
        org_id=actor.organization_id,
        actor_id=actor.id,
        action="quote.create",
        entity_type="quote",
        entity_id=quote.id,
        event_type="quote.created",
        aggregate_version=quote.version,
        payload={"quote_id": str(quote.id), "title": quote.title},
        after=_quote_summary(quote),
    )
    return quote


def _encode_cursor(updated_at: datetime, quote_id: uuid.UUID) -> str:
    import base64

    return base64.urlsafe_b64encode(f"{updated_at.isoformat()}|{quote_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    import base64

    try:
        ts, raw = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(ts), uuid.UUID(raw)
    except Exception:
        raise DomainError("INVALID_CURSOR", "Cursor inválido.", status_code=400) from None


async def list_quotes(
    session: AsyncSession,
    actor: Actor,
    *,
    status: str | None = None,
    updated_from: datetime | None = None,
    updated_to: datetime | None = None,
    q: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[list[dict], str | None]:
    require_admin(actor.roles)
    org = actor.organization_id
    stmt = select(Quote).where(Quote.organization_id == org)
    if status is not None:
        stmt = stmt.where(Quote.status == status)
    if updated_from is not None:
        stmt = stmt.where(Quote.updated_at >= updated_from)
    if updated_to is not None:
        stmt = stmt.where(Quote.updated_at <= updated_to)
    if q:
        stmt = stmt.where(Quote.title.ilike(f"%{q.strip()}%"))
    if cursor:
        ts, quote_id = _decode_cursor(cursor)
        stmt = stmt.where(
            (Quote.updated_at < ts) | and_(Quote.updated_at == ts, Quote.id < quote_id)
        )
    stmt = stmt.order_by(Quote.updated_at.desc(), Quote.id.desc()).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars())
    nxt = None
    if len(rows) > limit:
        rows = rows[:limit]
        nxt = _encode_cursor(rows[-1].updated_at, rows[-1].id)
    stats = await _list_stats(session, org, [r.id for r in rows])
    items = []
    for quote in rows:
        item = _quote_summary(quote)
        item.update(stats.get(quote.id, {"supplier_ids": [], "supplier_count": 0, "line_count": 0}))
        items.append(item)
    return items, nxt


async def _list_stats(
    session: AsyncSession, org: uuid.UUID, quote_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict]:
    if not quote_ids:
        return {}
    supplier_rows = (
        await session.execute(
            select(QuoteCart.quote_id, QuoteCart.supplier_id)
            .where(
                QuoteCart.organization_id == org,
                QuoteCart.quote_id.in_(quote_ids),
                QuoteCart.status == "active",
            )
            .distinct()
        )
    ).all()
    line_rows = (
        await session.execute(
            select(QuoteCart.quote_id, func.count(QuoteLine.id))
            .join(QuoteLine, QuoteLine.quote_cart_id == QuoteCart.id)
            .where(
                QuoteCart.organization_id == org,
                QuoteCart.quote_id.in_(quote_ids),
                QuoteCart.status == "active",
                QuoteLine.status == "active",
            )
            .group_by(QuoteCart.quote_id)
        )
    ).all()
    suppliers: dict[uuid.UUID, list[uuid.UUID]] = {}
    for quote_id, supplier_id in supplier_rows:
        suppliers.setdefault(quote_id, []).append(supplier_id)
    lines = {quote_id: count for quote_id, count in line_rows}
    return {
        quote_id: {
            "supplier_ids": suppliers.get(quote_id, []),
            "supplier_count": len(suppliers.get(quote_id, [])),
            "line_count": lines.get(quote_id, 0),
        }
        for quote_id in quote_ids
    }


async def update_quote(
    session: AsyncSession,
    actor: Actor,
    quote_id: uuid.UUID,
    *,
    expected_version: int | None,
    title: str | None = None,
    title_set: bool = False,
    status: str | None = None,
) -> Quote:
    require_admin(actor.roles)
    quote = await _quote(session, actor.organization_id, quote_id)
    before = _quote_summary(quote)
    _require_version(expected_version, quote.version)
    now = _now()
    if title_set:
        clean = title.strip() if isinstance(title, str) else None
        if clean is not None and len(clean) > 120:
            raise DomainError(
                "VALIDATION_ERROR", "El título admite hasta 120 caracteres.", status_code=400
            )
        quote.title = clean or None
    if status is not None:
        if status not in ("draft", "closed"):
            raise DomainError(
                "VALIDATION_ERROR", "El estado debe ser `draft` o `closed`.", status_code=400
            )
        if status == "closed" and quote.status != "closed":
            # D-137: una cotización no se cierra con asignaciones rotas. No se corrige nada solo;
            # la administración ajusta o quita las asignaciones y vuelve a intentar el cierre.
            issues = await _quote_allocation_issues(session, actor.organization_id, quote.id)
            if issues:
                raise DomainError(
                    "QUOTE_HAS_INCONSISTENT_ALLOCATIONS",
                    "La cotización tiene asignaciones que no coinciden con sus reportes "
                    "(reportes cancelados o cantidades menores). Corrige las líneas antes de "
                    "cerrarla.",
                    status_code=409,
                    details={"quote_id": str(quote.id), "issues": issues},
                )
            quote.status = "closed"
            quote.closed_by = actor.id
            quote.closed_at = now
        elif status == "draft" and quote.status != "draft":
            quote.status = "draft"
            quote.closed_by = None
            quote.closed_at = None
    _touch_quote(quote, now)
    _emit(
        session,
        org_id=actor.organization_id,
        actor_id=actor.id,
        action="quote.update",
        entity_type="quote",
        entity_id=quote.id,
        event_type="quote.updated",
        aggregate_version=quote.version,
        payload={"quote_id": str(quote.id), "status": quote.status},
        before=before,
        after=_quote_summary(quote),
    )
    return quote


# --------------------------------------------------------------------------- E10: carritos


async def set_cart_state(
    session: AsyncSession,
    actor: Actor,
    quote_id: uuid.UUID,
    supplier_id: uuid.UUID,
    *,
    dismissed: bool,
) -> tuple[QuoteCart, Quote]:
    require_admin(actor.roles)
    quote = await _quote(session, actor.organization_id, quote_id)
    cart = await _cart(session, actor.organization_id, quote, supplier_id)
    before = {"status": cart.status, "version": cart.version}
    now = _now()
    if dismissed and cart.status != "dismissed":
        cart.status = "dismissed"
        cart.dismissed_at = now
        cart.dismissed_by = actor.id
        _touch_cart(cart, now)
        _touch_quote(quote, now)
    elif not dismissed and cart.status != "active":
        cart.status = "active"
        cart.dismissed_at = None
        cart.dismissed_by = None
        _touch_cart(cart, now)
        _touch_quote(quote, now)
    _emit(
        session,
        org_id=actor.organization_id,
        actor_id=actor.id,
        action="quote.cart.dismiss" if dismissed else "quote.cart.reinstate",
        entity_type="quote_cart",
        entity_id=cart.id,
        event_type="quote.cart.dismissed" if dismissed else "quote.cart.reinstated",
        aggregate_version=cart.version,
        payload={
            "quote_id": str(quote.id),
            "supplier_id": str(supplier_id),
            "status": cart.status,
        },
        before=before,
        after={"status": cart.status, "version": cart.version},
    )
    return cart, quote


# --------------------------------------------------------------------------- E10: líneas


def _relation_for(demand: DemandItem, item: TradeItem) -> str:
    """Clasificación E1 congelada al agregar; si no se puede calcular, `needs_review`."""
    try:
        parsed = demand_svc.parsed_of_item(demand)
        query = identity_from_query(parsed)
        target = identity_from_item(
            ingredient=item.ingredient,
            brand=item.brand,
            laboratory=item.laboratory,
            attributes=item.attributes or {},
        )
        relation, _ = classify(query, target)
        return relation
    except (KeyError, ValueError, TypeError):
        return "needs_review"


async def _allocation_demand(
    session: AsyncSession, org: uuid.UUID, demand_item_id: uuid.UUID
) -> DemandItem:
    """Reporte asignable: activo, con cantidad conocida y no cancelado (D-135)."""
    demand = (
        await session.execute(
            select(DemandItem).where(
                DemandItem.id == demand_item_id, DemandItem.organization_id == org
            )
        )
    ).scalar_one_or_none()
    if demand is None:
        raise not_found("El reporte")
    if demand.status != "active":
        raise DomainError(
            "REPORT_NOT_ACTIVE",
            "El reporte no está activo: no se le puede asignar cantidad.",
            status_code=409,
        )
    if demand.target_quantity is None:
        raise DomainError(
            "REPORT_QUANTITY_UNKNOWN",
            "El reporte no tiene cantidad conocida. Fija la cantidad (SET) antes de asignarlo.",
            status_code=422,
            details={"demand_item_id": str(demand.id)},
        )
    return demand


async def _normalize_allocations(
    session: AsyncSession,
    org: uuid.UUID,
    *,
    line_quantity: Decimal,
    unit: str,
    item: TradeItem,
    raw: list[dict[str, Any]] | None,
    demand_item_id: uuid.UUID | None,
) -> list[dict[str, Any]]:
    """Valida asignaciones explícitas. Nunca suma ni reparte cantidades por su cuenta.

    Compatibilidad: una línea con `demand_item_id` y sin lista recibe una asignación por su
    cantidad total, que es exactamente lo que hacía C5-A.
    """
    if raw is not None and demand_item_id is not None:
        raise DomainError(
            "ALLOCATION_CONFLICT",
            "Usa `demand_allocations` o `demand_item_id`, no ambos.",
            status_code=422,
        )
    if raw is None:
        if demand_item_id is None:
            return []
        raw = [{"demand_item_id": demand_item_id, "quantity": line_quantity}]
    if not raw:
        raise DomainError(
            "ALLOCATION_EMPTY", "La lista de asignaciones no puede estar vacía.", status_code=422
        )
    out: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    total = ZERO
    for entry in raw:
        try:
            demand_id = uuid.UUID(str(entry["demand_item_id"]))
            quantity = _quantity(entry["quantity"])
        except (KeyError, TypeError, ValueError):
            raise DomainError(
                "ALLOCATION_INVALID",
                "Cada asignación exige `demand_item_id` y una cantidad entera mayor que cero.",
                status_code=422,
            ) from None
        if demand_id in seen:
            raise DomainError(
                "DUPLICATE_ALLOCATION",
                "Un reporte no puede aparecer dos veces en la misma línea.",
                status_code=422,
                details={"demand_item_id": str(demand_id)},
            )
        seen.add(demand_id)
        demand = await _allocation_demand(session, org, demand_id)
        if demand.quantity_unit != unit:
            raise DomainError(
                "UNIT_MISMATCH",
                "La unidad del reporte no coincide con la unidad de la línea.",
                status_code=422,
                details={
                    "demand_item_id": str(demand.id),
                    "report_unit": demand.quantity_unit,
                    "line_unit": unit,
                },
            )
        total += quantity
        out.append(
            {
                "demand": demand,
                "quantity": quantity,
                "coverage_relation": _relation_for(demand, item),
            }
        )
    if total > line_quantity:
        raise DomainError(
            "ALLOCATION_EXCEEDS_LINE",
            "La suma de asignaciones supera la cantidad de la línea.",
            status_code=409,
            details={"line_quantity": _qty(line_quantity), "allocated": _qty(total)},
        )
    return out


async def _allocations_by_line(
    session: AsyncSession, org: uuid.UUID, line_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[QuoteLineAllocation]]:
    if not line_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(QuoteLineAllocation)
                .where(
                    QuoteLineAllocation.organization_id == org,
                    QuoteLineAllocation.quote_line_id.in_(line_ids),
                )
                .order_by(QuoteLineAllocation.created_at, QuoteLineAllocation.id)
            )
        )
        .scalars()
        .all()
    )
    out: dict[uuid.UUID, list[QuoteLineAllocation]] = {}
    for row in rows:
        out.setdefault(row.quote_line_id, []).append(row)
    return out


def _allocation_payload(row: QuoteLineAllocation) -> dict:
    return {
        "allocation_id": row.id,
        "demand_item_id": row.demand_item_id,
        "quantity": _qty(row.quantity),
        "coverage_relation": row.coverage_relation,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


async def _add_allocations(
    session: AsyncSession, actor: Actor, line: QuoteLine, allocations: list[dict[str, Any]]
) -> None:
    if not allocations:
        return
    now = _now()
    for entry in allocations:
        session.add(
            QuoteLineAllocation(
                id=uuid.uuid4(),
                organization_id=actor.organization_id,
                quote_line_id=line.id,
                demand_item_id=entry["demand"].id,
                quantity=entry["quantity"],
                coverage_relation=entry["coverage_relation"],
                created_by=actor.id,
                created_at=now,
            )
        )
    await session.flush()


async def _allocations_equivalent(
    session: AsyncSession, org: uuid.UUID, line: QuoteLine, allocations: list[dict[str, Any]]
) -> bool:
    rows = (
        await session.execute(
            select(QuoteLineAllocation).where(
                QuoteLineAllocation.organization_id == org,
                QuoteLineAllocation.quote_line_id == line.id,
            )
        )
    ).scalars().all()
    current = {(r.demand_item_id, r.quantity) for r in rows}
    wanted = {(a["demand"].id, a["quantity"]) for a in allocations}
    return current == wanted


async def _assert_allocations_fit(
    session: AsyncSession, org: uuid.UUID, line: QuoteLine, quantity: Decimal
) -> None:
    total = (
        await session.execute(
            select(func.coalesce(func.sum(QuoteLineAllocation.quantity), 0)).where(
                QuoteLineAllocation.organization_id == org,
                QuoteLineAllocation.quote_line_id == line.id,
            )
        )
    ).scalar_one()
    if Decimal(total) > quantity:
        raise DomainError(
            "ALLOCATION_EXCEEDS_LINE",
            "La cantidad de la línea no puede quedar por debajo de lo asignado a sus reportes.",
            status_code=409,
            details={"allocated": _qty(Decimal(total)), "line_quantity": _qty(quantity)},
        )


async def _quote_allocation_issues(
    session: AsyncSession, org: uuid.UUID, quote_id: uuid.UUID
) -> list[dict]:
    """Inconsistencias entre las asignaciones de la cotización y sus reportes (D-137).

    Solo mira asignaciones vigentes (línea activa en carrito activo), igual que la cobertura. No
    corrige nada: devuelve la lista para que la administración ajuste o quite las asignaciones.
    """
    rows = (
        await session.execute(
            select(QuoteLineAllocation, QuoteLine, DemandItem)
            .join(QuoteLine, QuoteLine.id == QuoteLineAllocation.quote_line_id)
            .join(QuoteCart, QuoteCart.id == QuoteLine.quote_cart_id)
            .join(
                DemandItem,
                and_(
                    DemandItem.id == QuoteLineAllocation.demand_item_id,
                    DemandItem.organization_id == org,
                ),
            )
            .where(
                QuoteLineAllocation.organization_id == org,
                QuoteCart.quote_id == quote_id,
                QuoteLine.status == "active",
                QuoteCart.status == "active",
            )
            .order_by(QuoteLineAllocation.quote_line_id, QuoteLineAllocation.demand_item_id)
        )
    ).all()
    issues: list[dict] = []
    per_line: dict[uuid.UUID, tuple[QuoteLine, Decimal]] = {}
    for allocation, line, demand in rows:
        previous = per_line.get(line.id)
        total = (previous[1] if previous else ZERO) + allocation.quantity
        per_line[line.id] = (line, total)
        base = {
            "quote_line_id": str(line.id),
            "demand_item_id": str(demand.id),
            "allocated": _qty(allocation.quantity),
            "line_quantity": _qty(line.quantity),
        }
        if demand.status != "active":
            issues.append(
                {
                    **base,
                    "code": "REPORT_NOT_ACTIVE",
                    "report_status": demand.status,
                    "message": "El reporte asignado ya no está activo.",
                }
            )
        elif demand.target_quantity is None:
            issues.append(
                {
                    **base,
                    "code": "REPORT_QUANTITY_UNKNOWN",
                    "message": "El reporte asignado quedó sin cantidad pedida.",
                }
            )
        elif allocation.quantity > demand.target_quantity:
            issues.append(
                {
                    **base,
                    "code": "ALLOCATION_EXCEEDS_REPORT",
                    "report_quantity": _qty(demand.target_quantity),
                    "message": "La línea asigna más cantidad de la que pide el reporte.",
                }
            )
    for line, total in per_line.values():
        if total > line.quantity:
            issues.append(
                {
                    "code": "ALLOCATION_EXCEEDS_LINE",
                    "quote_line_id": str(line.id),
                    "allocated": _qty(total),
                    "line_quantity": _qty(line.quantity),
                    "message": "Las asignaciones de la línea superan su cantidad.",
                }
            )
    return issues


async def _replace_allocations(
    session: AsyncSession,
    actor: Actor,
    org: uuid.UUID,
    line: QuoteLine,
    item: TradeItem,
    raw: list[dict[str, Any]],
) -> None:
    """Reemplaza las asignaciones de una línea en borrador. Queda auditado el antes y el después."""
    existing = (
        await session.execute(
            select(QuoteLineAllocation).where(
                QuoteLineAllocation.organization_id == org,
                QuoteLineAllocation.quote_line_id == line.id,
            )
        )
    ).scalars().all()
    before = [_allocation_payload(row) for row in existing]
    # `PATCH` con lista vacía es la acción explícita de **quitar** todas las asignaciones (D-137).
    # En el alta (`add_line`) la lista vacía sigue rechazándose: ahí no hay nada que quitar.
    normalized = (
        await _normalize_allocations(
            session, org, line_quantity=line.quantity, unit=line.quantity_unit, item=item,
            raw=raw, demand_item_id=None,
        )
        if raw
        else []
    )  # fmt: skip
    for row in existing:
        await session.delete(row)
    await session.flush()
    await _add_allocations(session, actor, line, normalized)
    single = normalized[0] if len(normalized) == 1 else None
    line.demand_item_id = single["demand"].id if single else None
    line.coverage_relation = single["coverage_relation"] if single else None
    after = [
        {
            "demand_item_id": str(a["demand"].id),
            "quantity": _qty(a["quantity"]),
            "coverage_relation": a["coverage_relation"],
        }
        for a in normalized
    ]
    _emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="quote.line.allocate",
        entity_type="quote_line",
        entity_id=line.id,
        event_type="quote.line.allocations_replaced",
        aggregate_version=line.version,
        payload={"quote_line_id": str(line.id), "allocations": after},
        before={"allocations": before},
        after={"allocations": after},
    )


async def _prior_add(
    session: AsyncSession, org: uuid.UUID, key: str
) -> AuditEvent | None:
    return (
        await session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.organization_id == org,
                AuditEvent.action == "quote.line.add",
                AuditEvent.after["idempotency_key"].astext == key,
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def add_line(
    session: AsyncSession,
    actor: Actor,
    quote_id: uuid.UUID,
    *,
    supplier_id: uuid.UUID,
    offer_snapshot_id: uuid.UUID,
    quantity: Any,
    quantity_unit: Any,
    demand_item_id: uuid.UUID | None = None,
    demand_allocations: list[dict[str, Any]] | None = None,
    idempotency_key: str | None = None,
    request_hash: str | None = None,
) -> tuple[QuoteLine, QuoteCart, Quote, bool, list[str]]:
    """Agrega una línea. Devuelve (línea, carrito, cotización, deduplicada, advertencias)."""
    require_admin(actor.roles)
    org = actor.organization_id
    quote = await _quote(session, org, quote_id)
    if idempotency_key:
        await events.advisory_lock(session, f"idem:{org}:{idempotency_key}")
        prior = await _prior_add(session, org, idempotency_key)
        if prior is not None:
            if prior.after.get("request_hash") != request_hash:
                raise DomainError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Esa clave de idempotencia ya se usó con otro contenido.",
                    status_code=409,
                )
            line, cart = await _line(session, org, quote, prior.entity_id, active=False)
            return line, cart, quote, True, []
    await events.advisory_lock(session, f"quote:{quote.id}")
    quote = await _quote(session, org, quote_id)
    _ensure_draft(quote)
    value = _quantity(quantity)
    unit = _unit(quantity_unit)
    offer = await _offer(session, org, offer_snapshot_id)
    if offer.supplier_id != supplier_id:
        raise DomainError(
            "OFFER_NOT_IN_SUPPLIER",
            "La oferta no pertenece al proveedor indicado.",
            status_code=422,
        )
    supplier = (
        await session.execute(
            select(Supplier).where(Supplier.id == supplier_id, Supplier.organization_id == org)
        )
    ).scalar_one_or_none()
    if supplier is None:
        raise not_found("El proveedor")
    item = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.id == offer.trade_item_id, TradeItem.organization_id == org
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise not_found("El artículo")
    cart = (
        await session.execute(
            select(QuoteCart).where(
                QuoteCart.quote_id == quote.id,
                QuoteCart.organization_id == org,
                QuoteCart.supplier_id == supplier_id,
            )
        )
    ).scalar_one_or_none()
    now = _now()
    if cart is not None and cart.status == "dismissed":
        raise DomainError(
            "CART_DISMISSED",
            "El carrito de ese proveedor está descartado: reincorpóralo antes de agregar líneas.",
            status_code=409,
        )

    warnings: list[str] = []
    allocations = await _normalize_allocations(
        session, org, line_quantity=value, unit=unit, item=item,
        raw=demand_allocations, demand_item_id=demand_item_id,
    )  # fmt: skip
    # Atajo compatible: con una sola asignación la línea conserva `demand_item_id`/relación.
    single = allocations[0] if len(allocations) == 1 else None
    demand: DemandItem | None = single["demand"] if single else None
    relation: str | None = single["coverage_relation"] if single else None
    # C5-B comprobará aquí las compras vigentes del reporte y agregará
    # `demand_already_purchased` a `warnings`; hoy la tabla de compras no existe.

    if cart is None:
        cart = QuoteCart(
            id=uuid.uuid4(),
            organization_id=org,
            quote_id=quote.id,
            supplier_id=supplier_id,
            status="active",
            data_origin=origin_for(await org_kind(session, org)),
            created_at=now,
            updated_at=now,
        )
        session.add(cart)
        await session.flush()

    existing = (
        await session.execute(
            select(QuoteLine).where(
                QuoteLine.quote_cart_id == cart.id,
                QuoteLine.offer_snapshot_id == offer.id,
                QuoteLine.status == "active",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.quantity == value and (
            not demand_allocations
            or await _allocations_equivalent(session, org, existing, allocations)
        ):
            return existing, cart, quote, True, []
        raise DomainError(
            "LINE_EXISTS",
            "Esa oferta ya está en el carrito con otra cantidad. Usa PATCH para cambiarla.",
            status_code=409,
            details={"current_quantity": _qty(existing.quantity), "line_id": str(existing.id)},
        )

    line = QuoteLine(
        id=uuid.uuid4(),
        organization_id=org,
        quote_cart_id=cart.id,
        supplier_id=supplier_id,
        offer_snapshot_id=offer.id,
        trade_item_id=offer.trade_item_id,
        quantity=value,
        quantity_unit=unit,
        status="active",
        demand_item_id=demand.id if demand else None,
        coverage_relation=relation,
        data_origin=origin_for(await org_kind(session, org)),
        added_by=actor.id,
        added_at=now,
        updated_at=now,
    )
    session.add(line)
    _touch_cart(cart, now)
    _touch_quote(quote, now)
    await session.flush()
    await _add_allocations(session, actor, line, allocations)
    after = {
        "quote_id": str(quote.id),
        "line_id": str(line.id),
        "cart_id": str(cart.id),
        "supplier_id": str(supplier_id),
        "offer_snapshot_id": str(offer.id),
        "trade_item_id": str(line.trade_item_id),
        "quantity": _qty(value),
        "quantity_unit": unit,
        "demand_item_id": str(demand.id) if demand else None,
        "coverage_relation": relation,
        "demand_allocations": [
            {
                "demand_item_id": str(a["demand"].id),
                "quantity": _qty(a["quantity"]),
                "coverage_relation": a["coverage_relation"],
            }
            for a in allocations
        ],
    }
    if idempotency_key:
        # La clave se guarda en la auditoría de la misma transacción: no hay tabla nueva y el
        # registro es duradero e inmutable (§5.2, E8).
        after = {**after, "idempotency_key": idempotency_key, "request_hash": request_hash}
    _emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="quote.line.add",
        entity_type="quote_line",
        entity_id=line.id,
        event_type="quote.line.added",
        aggregate_version=line.version,
        payload={
            "quote_id": str(quote.id),
            "cart_id": str(cart.id),
            "supplier_id": str(supplier_id),
            "offer_snapshot_id": str(offer.id),
            "quantity": _qty(value),
            "quantity_unit": unit,
            "demand_item_id": str(demand.id) if demand else None,
        },
        after=after,
    )
    return line, cart, quote, False, warnings


async def patch_line(
    session: AsyncSession,
    actor: Actor,
    quote_id: uuid.UUID,
    line_id: uuid.UUID,
    *,
    expected_version: int | None,
    quantity: Any,
    quantity_unit: Any | None = None,
    demand_allocations: list[dict[str, Any]] | None = None,
) -> tuple[QuoteLine, QuoteCart, Quote]:
    require_admin(actor.roles)
    org = actor.organization_id
    quote = await _quote(session, org, quote_id)
    line, cart = await _line(session, org, quote, line_id)
    _require_version(expected_version, line.version)
    _ensure_draft(quote)
    value = _quantity(quantity)
    unit = _unit(quantity_unit) if quantity_unit is not None else line.quantity_unit
    if unit != line.quantity_unit and await _allocations_by_line(session, org, [line.id]):
        raise DomainError(
            "UNIT_MISMATCH",
            "No se puede cambiar la unidad de una línea con reportes asignados; quita las "
            "asignaciones primero.",
            status_code=422,
        )
    # C5-B: si hay compras vigentes, `value` no puede ser menor que lo comprado
    # (`409 QUANTITY_BELOW_PURCHASED`). Hoy no existe la tabla de compras.
    now = _now()
    before = {"quantity": _qty(line.quantity), "quantity_unit": line.quantity_unit}
    line.quantity = value
    line.quantity_unit = unit
    line.version += 1
    line.updated_at = now
    await session.flush()
    await _assert_allocations_fit(session, org, line, value)
    if demand_allocations is not None:
        item = (
            await session.execute(
                select(TradeItem).where(
                    TradeItem.id == line.trade_item_id, TradeItem.organization_id == org
                )
            )
        ).scalar_one_or_none()
        if item is None:
            raise not_found("El artículo")
        await _replace_allocations(session, actor, org, line, item, demand_allocations)
    _touch_cart(cart, now)
    _touch_quote(quote, now)
    _emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="quote.line.update",
        entity_type="quote_line",
        entity_id=line.id,
        event_type="quote.line.quantity_set",
        aggregate_version=line.version,
        payload={"quote_id": str(quote.id), "quantity": _qty(value), "quantity_unit": unit},
        before=before,
        after={"quantity": _qty(value), "quantity_unit": unit},
    )
    return line, cart, quote


async def remove_line(
    session: AsyncSession, actor: Actor, quote_id: uuid.UUID, line_id: uuid.UUID
) -> tuple[QuoteLine, QuoteCart, Quote]:
    require_admin(actor.roles)
    org = actor.organization_id
    quote = await _quote(session, org, quote_id)
    line, cart = await _line(session, org, quote, line_id)
    _ensure_draft(quote)
    # C5-B: con compras vigentes se responde `409 LINE_HAS_PURCHASES`. Hoy no hay compras.
    now = _now()
    line.status = "removed"
    line.removed_at = now
    line.removed_by = actor.id
    line.version += 1
    line.updated_at = now
    _touch_cart(cart, now)
    _touch_quote(quote, now)
    _emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="quote.line.remove",
        entity_type="quote_line",
        entity_id=line.id,
        event_type="quote.line.removed",
        aggregate_version=line.version,
        payload={"quote_id": str(quote.id), "supplier_id": str(line.supplier_id)},
        before={"status": "active"},
        after={"status": "removed"},
    )
    return line, cart, quote


async def close_pending(
    session: AsyncSession,
    actor: Actor,
    quote_id: uuid.UUID,
    line_id: uuid.UUID,
    *,
    note: str | None = None,
) -> tuple[QuoteLine, QuoteCart, Quote]:
    require_admin(actor.roles)
    org = actor.organization_id
    quote = await _quote(session, org, quote_id)
    line, cart = await _line(session, org, quote, line_id)
    _ensure_draft(quote)
    now = _now()
    if line.pending_closed_at is None:
        line.pending_closed_at = now
        line.pending_closed_by = actor.id
        line.pending_closed_note = note
        line.version += 1
        line.updated_at = now
        _touch_cart(cart, now)
        _touch_quote(quote, now)
        _emit(
            session,
            org_id=org,
            actor_id=actor.id,
            action="quote.line.pending.close",
            entity_type="quote_line",
            entity_id=line.id,
            event_type="quote.line.pending_closed",
            aggregate_version=line.version,
            payload={"quote_id": str(quote.id)},
            after={"pending_closed_at": now.isoformat(), "note": note},
        )
    return line, cart, quote


async def reopen_pending(
    session: AsyncSession, actor: Actor, quote_id: uuid.UUID, line_id: uuid.UUID
) -> tuple[QuoteLine, QuoteCart, Quote]:
    require_admin(actor.roles)
    org = actor.organization_id
    quote = await _quote(session, org, quote_id)
    line, cart = await _line(session, org, quote, line_id)
    _ensure_draft(quote)
    now = _now()
    if line.pending_closed_at is not None:
        line.pending_closed_at = None
        line.pending_closed_by = None
        line.pending_closed_note = None
        line.version += 1
        line.updated_at = now
        _touch_cart(cart, now)
        _touch_quote(quote, now)
        _emit(
            session,
            org_id=org,
            actor_id=actor.id,
            action="quote.line.pending.reopen",
            entity_type="quote_line",
            entity_id=line.id,
            event_type="quote.line.pending_reopened",
            aggregate_version=line.version,
            payload={"quote_id": str(quote.id)},
            before={"pending_closed_at": "closed"},
            after={"pending_closed_at": None},
        )
    return line, cart, quote


async def move_line(
    session: AsyncSession,
    actor: Actor,
    quote_id: uuid.UUID,
    line_id: uuid.UUID,
    *,
    target_offer_snapshot_id: uuid.UUID,
    quantity: Any,
    quantity_unit: Any | None = None,
    demand_item_id: uuid.UUID | None = None,
    demand_allocations: list[dict[str, Any]] | None = None,
) -> tuple[QuoteLine, QuoteCart, Quote]:
    """Quita y agrega en una transacción. La línea nueva no arrastra el vínculo original."""
    require_admin(actor.roles)
    org = actor.organization_id
    quote = await _quote(session, org, quote_id)
    source, source_cart = await _line(session, org, quote, line_id)
    _ensure_draft(quote)
    value = _quantity(quantity)
    unit = _unit(quantity_unit) if quantity_unit is not None else source.quantity_unit
    target = await _offer(session, org, target_offer_snapshot_id)
    if target.trade_item_id != source.trade_item_id:
        raise DomainError(
            "ARTICLE_MISMATCH",
            "La oferta destino corresponde a otro artículo.",
            status_code=422,
        )
    target_cart = (
        await session.execute(
            select(QuoteCart).where(
                QuoteCart.quote_id == quote.id,
                QuoteCart.organization_id == org,
                QuoteCart.supplier_id == target.supplier_id,
            )
        )
    ).scalar_one_or_none()
    if target_cart is not None and target_cart.status == "dismissed":
        raise DomainError(
            "CART_DISMISSED",
            "El carrito de destino está descartado: reincorpóralo antes de mover líneas.",
            status_code=409,
        )
    if target_cart is not None:
        duplicate = (
            await session.execute(
                select(QuoteLine).where(
                    QuoteLine.quote_cart_id == target_cart.id,
                    QuoteLine.offer_snapshot_id == target.id,
                    QuoteLine.status == "active",
                )
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise DomainError(
                "LINE_EXISTS",
                "Esa oferta ya está en el carrito de destino.",
                status_code=409,
                details={"current_quantity": _qty(duplicate.quantity), "line_id": str(duplicate.id)},
            )
    now = _now()
    source.status = "removed"
    source.removed_at = now
    source.removed_by = actor.id
    source.version += 1
    source.updated_at = now
    if target_cart is None:
        target_cart = QuoteCart(
            id=uuid.uuid4(),
            organization_id=org,
            quote_id=quote.id,
            supplier_id=target.supplier_id,
            status="active",
            data_origin=origin_for(await org_kind(session, org)),
            created_at=now,
            updated_at=now,
        )
        session.add(target_cart)
        await session.flush()

    item = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.id == target.trade_item_id, TradeItem.organization_id == org
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise not_found("El artículo")
    allocations = await _normalize_allocations(
        session, org, line_quantity=value, unit=unit, item=item,
        raw=demand_allocations, demand_item_id=demand_item_id,
    )  # fmt: skip
    single = allocations[0] if len(allocations) == 1 else None
    demand: DemandItem | None = single["demand"] if single else None
    relation: str | None = single["coverage_relation"] if single else None

    new_line = QuoteLine(
        id=uuid.uuid4(),
        organization_id=org,
        quote_cart_id=target_cart.id,
        supplier_id=target.supplier_id,
        offer_snapshot_id=target.id,
        trade_item_id=target.trade_item_id,
        quantity=value,
        quantity_unit=unit,
        status="active",
        demand_item_id=demand.id if demand else None,
        coverage_relation=relation,
        data_origin=origin_for(await org_kind(session, org)),
        added_by=actor.id,
        added_at=now,
        updated_at=now,
    )
    session.add(new_line)
    await session.flush()
    await _add_allocations(session, actor, new_line, allocations)
    _touch_cart(source_cart, now)
    if target_cart.id != source_cart.id:  # mismo proveedor: un solo toque de versión
        _touch_cart(target_cart, now)
    _touch_quote(quote, now)
    await session.flush()
    _emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="quote.line.move",
        entity_type="quote_line",
        entity_id=source.id,
        event_type="quote.line.moved",
        aggregate_version=source.version,
        payload={
            "quote_id": str(quote.id),
            "from_line_id": str(source.id),
            "to_line_id": str(new_line.id),
            "to_supplier_id": str(target.supplier_id),
        },
        before={
            "supplier_id": str(source.supplier_id),
            "offer_snapshot_id": str(source.offer_snapshot_id),
            "quantity": _qty(source.quantity),
            "quantity_unit": source.quantity_unit,
            "demand_item_id": str(source.demand_item_id) if source.demand_item_id else None,
        },
        after={
            "supplier_id": str(new_line.supplier_id),
            "offer_snapshot_id": str(new_line.offer_snapshot_id),
            "quantity": _qty(new_line.quantity),
            "quantity_unit": new_line.quantity_unit,
            "demand_item_id": str(new_line.demand_item_id) if new_line.demand_item_id else None,
            "demand_allocations": [
                {
                    "demand_item_id": str(a["demand"].id),
                    "quantity": _qty(a["quantity"]),
                    "coverage_relation": a["coverage_relation"],
                }
                for a in allocations
            ],
        },
    )
    return new_line, target_cart, quote


async def refresh_line(
    session: AsyncSession, actor: Actor, quote_id: uuid.UUID, line_id: uuid.UUID
) -> tuple[QuoteLine, QuoteCart, Quote, bool]:
    """Acepta el precio actual. Reapuntar es explícito y auditado; nunca automático."""
    require_admin(actor.roles)
    org = actor.organization_id
    quote = await _quote(session, org, quote_id)
    line, cart = await _line(session, org, quote, line_id)
    _ensure_draft(quote)
    selected = await _offer(session, org, line.offer_snapshot_id)
    latest = await _latest_offers(session, org, [selected.trade_item_id], now=_now())
    current = next(
        (o for o in latest.get(selected.trade_item_id, []) if o.source_id == selected.source_id),
        None,
    )
    if current is None:
        raise not_found("El precio actual de la oferta")
    now = _now()
    refreshed = current.id != selected.id
    if refreshed:
        line.offer_snapshot_id = current.id
        line.version += 1
        line.updated_at = now
        _touch_cart(cart, now)
        _touch_quote(quote, now)
        _emit(
            session,
            org_id=org,
            actor_id=actor.id,
            action="quote.line.refresh",
            entity_type="quote_line",
            entity_id=line.id,
            event_type="quote.line.price_refreshed",
            aggregate_version=line.version,
            payload={
                "quote_id": str(quote.id),
                "from_offer_snapshot_id": str(selected.id),
                "to_offer_snapshot_id": str(current.id),
            },
            before={"offer_snapshot_id": str(selected.id)},
            after={"offer_snapshot_id": str(current.id)},
        )
    return line, cart, quote, refreshed


async def line_view(session: AsyncSession, actor: Actor, quote: Quote, line: QuoteLine) -> dict:
    """Vista de una línea con su precio elegido y la observación más reciente del mismo origen."""
    now = _now()
    selected = await _offer(session, actor.organization_id, line.offer_snapshot_id)
    latest = await _latest_offers(
        session, actor.organization_id, [line.trade_item_id], now=now
    )
    current = next(
        (o for o in latest.get(line.trade_item_id, []) if o.source_id == selected.source_id), None
    )
    allocations = await _allocations_by_line(session, actor.organization_id, [line.id])
    return _line_payload(
        line, quote, selected=selected, current=current, now=now,
        allocations=allocations.get(line.id, []),
    )  # fmt: skip


async def cart_view(
    session: AsyncSession, actor: Actor, quote: Quote, cart: QuoteCart
) -> dict:
    org = actor.organization_id
    supplier = (
        await session.execute(
            select(Supplier).where(
                Supplier.id == cart.supplier_id, Supplier.organization_id == org
            )
        )
    ).scalar_one_or_none()
    count = (
        await session.execute(
            select(func.count())
            .select_from(QuoteLine)
            .where(QuoteLine.quote_cart_id == cart.id, QuoteLine.status == "active")
        )
    ).scalar_one()
    payload = _cart_payload(cart, supplier, line_count=count)
    payload["derived_state"] = "empty" if count == 0 else "pending"
    payload["quote_version"] = quote.version
    return payload


def observation_payload(
    row: CommercialObservation,
    *,
    latest: ObservationInterpretation | None = None,
    prefs: dict | None = None,
    history: tuple[ObservationInterpretation, ...] = (),
) -> dict:
    return _observation_payload(row, latest=latest, prefs=prefs, history=history)


# --------------------------------------------------------------------------- evaluación por carrito


async def _evaluate_cart(
    session: AsyncSession, actor: Actor, cart: QuoteCart, lines: list[QuoteLine], now: datetime
) -> dict | None:
    active = [line for line in lines if line.status == "active"]
    if cart.status != "active" or not active:
        return None
    inputs = [
        comparison_svc.LineInput(
            article_id=line.trade_item_id, value=line.quantity, unit=line.quantity_unit
        )
        for line in active
    ]
    overrides = {index: line.offer_snapshot_id for index, line in enumerate(active)}
    _, payload = await comparison_svc.run_comparison(
        session, actor, lines=inputs, now=now, offer_overrides=overrides, persist=False
    )
    total = next(
        (t for t in payload["supplier_totals"] if t["supplier_id"] == cart.supplier_id), None
    )
    if total is None:
        discarded = next(
            (d for d in payload["discarded_suppliers"] if d["supplier_id"] == cart.supplier_id),
            None,
        )
        causes = discarded["causes"] if discarded else ["source_failed"]
        return {
            "comparison_id": payload["comparison_id"],
            "comparison_status": payload["status"],
            "completeness": "partial",
            "currency": None,
            "subtotal": None,
            "total": None,
            "lower_bound": None,
            "blockers": causes,
            "causes": causes,
            "freshness": {},
            "computed_at": payload["generated_at"],
            "discarded": discarded is not None,
        }
    return {
        "comparison_id": payload["comparison_id"],
        "comparison_status": payload["status"],
        "completeness": total["completeness"],
        "currency": total["currency"],
        "subtotal": total["subtotal"],
        "total": total["total"],
        "lower_bound": total["lower_bound"],
        "blockers": total["blockers"],
        "causes": total["causes"],
        "freshness": total["freshness"],
        "computed_at": total["computed_at"],
        "discarded": False,
    }


def _estimated_total(
    entries: list[tuple[QuoteCart, dict | None]],
    *,
    empty_supplier_ids: list[uuid.UUID],
    now: datetime,
) -> dict:
    """Solo hay total general si todos los carritos activos están completos y sin causas unknown."""
    causes: list[str] = []
    evaluated = [(cart, ev) for cart, ev in entries if ev is not None]
    for supplier_id in empty_supplier_ids:
        causes.append(f"cart_empty:{supplier_id}")
    if not evaluated:
        causes.append("no_active_lines")
    currencies: set[str] = set()
    for cart, ev in evaluated:
        if ev["completeness"] != "complete":
            causes.append(f"cart_incomplete:{cart.supplier_id}")
        for blocker in ev["blockers"]:
            causes.append(f"cart_blocker:{cart.supplier_id}:{blocker}")
        if ev["currency"] is None:
            causes.append(f"currency_unknown:{cart.supplier_id}")
        else:
            currencies.add(ev["currency"])
        for cause in ev["causes"]:
            if cause.endswith("_unknown"):
                causes.append(f"unknown_condition:{cart.supplier_id}:{cause}")
    if len(currencies) > 1:
        causes.append("currency_mismatch")
    if causes:
        return {
            "available": False,
            "kind": None,
            "amount": None,
            "currency": next(iter(currencies)) if len(currencies) == 1 else None,
            "suppliers_included": [],
            "causes": sorted(set(causes)),
            "computed_at": now,
        }
    amount = sum((Decimal(ev["total"]["amount"]) for _, ev in evaluated), ZERO)
    return {
        "available": True,
        "kind": "complete_estimate",
        "amount": str(amount.quantize(Decimal("0.01"))),
        "currency": next(iter(currencies)),
        "suppliers_included": [cart.supplier_id for cart, _ in evaluated],
        "causes": [],
        "computed_at": now,
    }


# --------------------------------------------------------------------------- E10: lectura


async def quote_detail(session: AsyncSession, actor: Actor, quote_id: uuid.UUID) -> dict:
    require_admin(actor.roles)
    org = actor.organization_id
    quote = await _quote(session, org, quote_id)
    now = _now()
    carts = list(
        (
            await session.execute(
                select(QuoteCart)
                .where(QuoteCart.quote_id == quote.id, QuoteCart.organization_id == org)
                .order_by(QuoteCart.created_at, QuoteCart.id)
            )
        )
        .scalars()
        .all()
    )
    cart_ids = [c.id for c in carts]
    lines = (
        list(
            (
                await session.execute(
                    select(QuoteLine)
                    .where(
                        QuoteLine.organization_id == org,
                        QuoteLine.quote_cart_id.in_(cart_ids),
                    )
                    .order_by(QuoteLine.added_at, QuoteLine.id)
                )
            )
            .scalars()
            .all()
        )
        if cart_ids
        else []
    )
    by_cart: dict[uuid.UUID, list[QuoteLine]] = {}
    for line in lines:
        by_cart.setdefault(line.quote_cart_id, []).append(line)
    allocs = await _allocations_by_line(session, org, [line.id for line in lines])

    supplier_ids = {c.supplier_id for c in carts}
    suppliers = {
        s.id: s
        for s in (
            (
                await session.execute(
                    select(Supplier).where(
                        Supplier.organization_id == org, Supplier.id.in_(supplier_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
    } if supplier_ids else {}

    trade_ids = list({line.trade_item_id for line in lines})
    selected_offers = {
        o.id: o
        for o in (
            (
                await session.execute(
                    select(OfferSnapshot).where(
                        OfferSnapshot.organization_id == org,
                        OfferSnapshot.id.in_({line.offer_snapshot_id for line in lines}),
                    )
                )
            )
            .scalars()
            .all()
        )
    } if lines else {}
    latest = await _latest_offers(session, org, trade_ids, now=now) if trade_ids else {}

    evaluations: dict[uuid.UUID, dict | None] = {}
    for cart in carts:
        evaluations[cart.id] = await _evaluate_cart(
            session, actor, cart, by_cart.get(cart.id, []), now
        )

    cart_payloads = []
    entries: list[tuple[QuoteCart, dict | None]] = []
    empty_supplier_ids: list[uuid.UUID] = []
    for cart in carts:
        cart_lines = by_cart.get(cart.id, [])
        active = [line for line in cart_lines if line.status == "active"]
        evaluation = evaluations[cart.id]
        entries.append((cart, evaluation))
        if cart.status == "active" and not active:
            empty_supplier_ids.append(cart.supplier_id)
        payload = _cart_payload(cart, suppliers.get(cart.supplier_id), line_count=len(active))
        payload["derived_state"] = (
            "empty" if not active else "pending"
        )
        payload["lines"] = [
            _line_payload(
                line,
                quote,
                selected=selected_offers[line.offer_snapshot_id],
                current=next(
                    (
                        o
                        for o in latest.get(line.trade_item_id, [])
                        if o.source_id == selected_offers[line.offer_snapshot_id].source_id
                    ),
                    None,
                ),
                now=now,
                allocations=allocs.get(line.id, []),
            )
            for line in cart_lines
        ]
        payload["evaluation"] = evaluation
        cart_payloads.append(payload)

    coverage = await _quote_coverage(session, org, lines)
    result = _quote_summary(quote)
    result.update(
        {
            "derived_state": _derived_quote_state(quote),
            "carts": cart_payloads,
            "coverage": coverage,
            "estimated_total": _estimated_total(
                entries, empty_supplier_ids=empty_supplier_ids, now=now
            ),
            "advice": None,
        }
    )
    return result


async def _quote_coverage(
    session: AsyncSession, org: uuid.UUID, lines: list[QuoteLine]
) -> list[dict]:
    """Cobertura por reporte desde las asignaciones, incluidos los de líneas ya retiradas.

    Las líneas retiradas se informan con cobertura 0: el reporte sigue visible para que la
    administración decida, y la cobertura la calcula el servicio, no este listado.
    """
    if not lines:
        return []
    allocs = await _allocations_by_line(session, org, [line.id for line in lines])
    demand_ids = sorted({a.demand_item_id for rows in allocs.values() for a in rows}, key=str)
    if not demand_ids:
        return []
    data, items = await coverage_svc.coverage_for_ids(session, org, demand_ids)
    relations: dict[uuid.UUID, list[str]] = {}
    for rows in allocs.values():
        for alloc in rows:
            relations.setdefault(alloc.demand_item_id, [])
            if alloc.coverage_relation not in relations[alloc.demand_item_id]:
                relations[alloc.demand_item_id].append(alloc.coverage_relation)
    out = []
    for demand_id in demand_ids:
        item = items.get(demand_id)
        if item is None:
            continue
        row = {
            "demand_item_id": demand_id,
            "display_name": item.display_name,
            "target_quantity": item.target_quantity,
            "quantity_unit": item.quantity_unit,
            "status": item.status,
            "coverage_relations": relations.get(demand_id, []),
        }
        row.update(data.get(demand_id, {}))
        out.append(row)
    out.sort(key=lambda r: str(r["demand_item_id"]))
    return out


async def quote_summary(session: AsyncSession, actor: Actor, quote_id: uuid.UUID) -> dict:
    require_admin(actor.roles)
    org = actor.organization_id
    quote = await _quote(session, org, quote_id)
    now = _now()
    carts = list(
        (
            await session.execute(
                select(QuoteCart)
                .where(QuoteCart.quote_id == quote.id, QuoteCart.organization_id == org)
                .order_by(QuoteCart.created_at, QuoteCart.id)
            )
        )
        .scalars()
        .all()
    )
    cart_ids = [c.id for c in carts]
    lines = (
        list(
            (
                await session.execute(
                    select(QuoteLine).where(
                        QuoteLine.organization_id == org,
                        QuoteLine.quote_cart_id.in_(cart_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        if cart_ids
        else []
    )
    by_cart: dict[uuid.UUID, list[QuoteLine]] = {}
    for line in lines:
        by_cart.setdefault(line.quote_cart_id, []).append(line)
    supplier_ids = {c.supplier_id for c in carts}
    suppliers = {
        s.id: s
        for s in (
            (
                await session.execute(
                    select(Supplier).where(
                        Supplier.organization_id == org, Supplier.id.in_(supplier_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
    } if supplier_ids else {}
    entries: list[tuple[QuoteCart, dict | None]] = []
    empty_supplier_ids: list[uuid.UUID] = []
    rows = []
    for cart in carts:
        cart_lines = by_cart.get(cart.id, [])
        active = [line for line in cart_lines if line.status == "active"]
        evaluation = await _evaluate_cart(session, actor, cart, cart_lines, now)
        entries.append((cart, evaluation))
        if cart.status == "active" and not active:
            empty_supplier_ids.append(cart.supplier_id)
        causes: list[str] = []
        if evaluation is None:
            causes.append("cart_empty" if not active else "cart_not_evaluated")
        else:
            causes.extend(evaluation["blockers"])
        rows.append(
            {
                "supplier_id": cart.supplier_id,
                "supplier_name": suppliers[cart.supplier_id].name
                if cart.supplier_id in suppliers
                else None,
                "product_count": len(active),
                "status": cart.status,
                "derived_state": "empty" if not active else "pending",
                "completeness": evaluation["completeness"] if evaluation else None,
                "subtotal": evaluation["subtotal"] if evaluation else None,
                "total": evaluation["total"] if evaluation else None,
                "lower_bound": evaluation["lower_bound"] if evaluation else None,
                "causes": sorted(set(causes)),
            }
        )
    result = _quote_summary(quote)
    result.update(
        {
            "derived_state": _derived_quote_state(quote),
            "suppliers": rows,
            "estimated_total": _estimated_total(
                entries, empty_supplier_ids=empty_supplier_ids, now=now
            ),
        }
    )
    return result


# --------------------------------------------------------------------------- E13 / O1: observaciones


def _observation_payload(
    row: CommercialObservation,
    *,
    latest: ObservationInterpretation | None = None,
    prefs: dict | None = None,
    history: tuple[ObservationInterpretation, ...] = (),
) -> dict:
    return observation_view(row, latest, prefs or {}, history=history)


async def _observation_context(
    session: AsyncSession,
    *,
    item: TradeItem,
    supplier: Supplier | None,
    offer: OfferSnapshot | None,
    quote: Quote | None,
    line: QuoteLine | None,
) -> dict:
    context: dict[str, Any] = {
        "trade_item": {
            "id": str(item.id),
            "display_name": item.display_name,
            "brand": item.brand,
            "laboratory": item.laboratory,
        }
    }
    if supplier is not None:
        context["supplier"] = {"id": str(supplier.id), "name": supplier.name}
    if offer is not None:
        context["offer"] = {
            "id": str(offer.id),
            "external_name": offer.external_name,
            "price": _money(offer.price_amount, offer.currency),
            "observed_at": _iso(offer.observed_at),
        }
    if quote is not None:
        context["quote"] = {"id": str(quote.id), "title": quote.title}
    if line is not None:
        context["quote_line"] = {
            "id": str(line.id),
            "quantity": _qty(line.quantity),
            "quantity_unit": line.quantity_unit,
        }
    return context


async def _prior_observation(
    session: AsyncSession, org: uuid.UUID, key: str
) -> AuditEvent | None:
    return (
        await session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.organization_id == org,
                AuditEvent.action == "observation.create",
                AuditEvent.after["idempotency_key"].astext == key,
            )
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_observation(
    session: AsyncSession,
    actor: Actor,
    *,
    text: str,
    trade_item_id: uuid.UUID,
    supplier_id: uuid.UUID | None = None,
    offer_snapshot_id: uuid.UUID | None = None,
    quote_id: uuid.UUID | None = None,
    line: QuoteLine | None = None,
    idempotency_key: str | None = None,
) -> CommercialObservation:
    """Guarda la observación siempre, sin categorías ni pasos extra (D-132).

    `Idempotency-Key` es opcional. Si se repite con el mismo texto y artículo devuelve la
    observación original; con otro contenido responde `409 IDEMPOTENCY_KEY_REUSED`. No hay
    columna nueva: la clave vive en el evento de auditoría de la creación.
    """
    require_admin(actor.roles)
    org = actor.organization_id
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise DomainError(
            "OBSERVATION_INVALID",
            f"La observación debe tener entre 1 y {MAX_TEXT} caracteres.",
            status_code=422,
        )
    request_hash = hashlib.sha256(
        f"{trade_item_id}|{supplier_id}|{offer_snapshot_id}|{quote_id}|{text}".encode()
    ).hexdigest()
    if idempotency_key:
        await events.advisory_lock(session, f"idem:obs:{org}:{idempotency_key}")
        prior = await _prior_observation(session, org, idempotency_key)
        if prior is not None:
            if prior.after.get("request_hash") != request_hash:
                raise DomainError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "Esa clave de idempotencia ya se usó con otro contenido.",
                    status_code=409,
                )
            row = (
                await session.execute(
                    select(CommercialObservation).where(
                        CommercialObservation.id == prior.entity_id,
                        CommercialObservation.organization_id == org,
                    )
                )
            ).scalar_one_or_none()
            if row is not None:
                return row
    item = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.id == trade_item_id, TradeItem.organization_id == org
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise not_found("El artículo")

    quote: Quote | None = None
    if line is not None:
        if line.trade_item_id != item.id:
            raise DomainError(
                "OBSERVATION_INVALID",
                "La observación no coincide con el artículo de la línea.",
                status_code=422,
            )
        cart = (
            await session.execute(
                select(QuoteCart).where(QuoteCart.id == line.quote_cart_id)
            )
        ).scalar_one()
        quote = await _quote(session, org, cart.quote_id)
        if quote_id is not None and quote_id != quote.id:
            raise DomainError(
                "OBSERVATION_INVALID",
                "La línea pertenece a otra cotización.",
                status_code=422,
            )
        quote_id = quote.id
        supplier_id = line.supplier_id
        offer_snapshot_id = line.offer_snapshot_id
    elif quote_id is not None:
        quote = await _quote(session, org, quote_id)

    supplier: Supplier | None = None
    if supplier_id is not None:
        supplier = (
            await session.execute(
                select(Supplier).where(
                    Supplier.id == supplier_id, Supplier.organization_id == org
                )
            )
        ).scalar_one_or_none()
        if supplier is None:
            raise not_found("El proveedor")

    offer: OfferSnapshot | None = None
    if offer_snapshot_id is not None:
        offer = await _offer(session, org, offer_snapshot_id)
        if offer.trade_item_id != item.id:
            raise DomainError(
                "OBSERVATION_INVALID",
                "La oferta corresponde a otro artículo.",
                status_code=422,
            )
        if supplier_id is not None and offer.supplier_id != supplier_id:
            raise DomainError(
                "OBSERVATION_INVALID",
                "La oferta pertenece a otro proveedor.",
                status_code=422,
            )
        if supplier is None:
            supplier = (
                await session.execute(
                    select(Supplier).where(Supplier.id == offer.supplier_id)
                )
            ).scalar_one_or_none()
            # La oferta identifica a su proveedor: se guarda, no solo se usa en el contexto.
            supplier_id = offer.supplier_id

    now = _now()
    row = CommercialObservation(
        id=uuid.uuid4(),
        organization_id=org,
        text_original=text,
        author_id=actor.id,
        created_at=now,
        quote_id=quote_id,
        quote_line_id=line.id if line else None,
        trade_item_id=item.id,
        offer_snapshot_id=offer_snapshot_id,
        supplier_id=supplier_id,
        context=await _observation_context(
            session, item=item, supplier=supplier, offer=offer, quote=quote, line=line
        ),
        data_origin="captured",
    )
    session.add(row)
    await session.flush()
    audit_after = {
        "trade_item_id": str(item.id),
        "quote_id": str(quote_id) if quote_id else None,
    }
    if idempotency_key:
        audit_after.update({"idempotency_key": idempotency_key, "request_hash": request_hash})
    _emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="observation.create",
        entity_type="commercial_observation",
        entity_id=row.id,
        event_type="commercial_observation.created",
        aggregate_version=None,
        payload={
            "observation_id": str(row.id),
            "trade_item_id": str(item.id),
            "quote_id": str(quote_id) if quote_id else None,
            "quote_line_id": str(line.id) if line else None,
        },
        after=audit_after,
    )
    return row


async def create_line_observation(
    session: AsyncSession,
    actor: Actor,
    quote_id: uuid.UUID,
    line_id: uuid.UUID,
    *,
    text: str,
) -> CommercialObservation:
    org = actor.organization_id
    quote = await _quote(session, org, quote_id)
    line, _ = await _line(session, org, quote, line_id)
    return await create_observation(
        session, actor, text=text, trade_item_id=line.trade_item_id, line=line
    )


async def list_line_observations(
    session: AsyncSession, actor: Actor, quote_id: uuid.UUID, line_id: uuid.UUID
) -> list[CommercialObservation]:
    require_admin(actor.roles)
    org = actor.organization_id
    quote = await _quote(session, org, quote_id)
    line, _ = await _line(session, org, quote, line_id, active=False)
    return list(
        (
            await session.execute(
                select(CommercialObservation)
                .where(
                    CommercialObservation.organization_id == org,
                    CommercialObservation.quote_line_id == line.id,
                )
                .order_by(
                    CommercialObservation.created_at.desc(), CommercialObservation.id.desc()
                )
            )
        )
        .scalars()
        .all()
    )




async def get_trade_item(
    session: AsyncSession, actor: Actor, trade_item_id: uuid.UUID
) -> TradeItem:
    """Artículo de la organización del actor; 404 si no existe o es de otra organización."""
    require_admin(actor.roles)
    item = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.id == trade_item_id,
                TradeItem.organization_id == actor.organization_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise not_found("El artículo")
    return item


async def get_observation(
    session: AsyncSession, actor: Actor, observation_id: uuid.UUID
) -> CommercialObservation:
    """Observación de la organización del actor. Un id ajeno responde 404, nunca lo filtra.

    El `join` con el artículo repite la comprobación de organización: ni una observación ni su
    artículo pueden pertenecer a otra farmacia.
    """
    require_admin(actor.roles)
    row = (
        await session.execute(
            select(CommercialObservation)
            .join(TradeItem, TradeItem.id == CommercialObservation.trade_item_id)
            .where(
                CommercialObservation.id == observation_id,
                CommercialObservation.organization_id == actor.organization_id,
                TradeItem.organization_id == actor.organization_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("La observación")
    return row


async def list_observations_page(
    session: AsyncSession,
    actor: Actor,
    *,
    trade_item_id: uuid.UUID,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[CommercialObservation], str | None]:
    """Historial del artículo, más recientes primero, con cursor (mismo patrón que E10)."""
    await get_trade_item(session, actor, trade_item_id)
    stmt = select(CommercialObservation).where(
        CommercialObservation.organization_id == actor.organization_id,
        CommercialObservation.trade_item_id == trade_item_id,
    )
    if cursor:
        ts, observation_id = _decode_cursor(cursor)
        stmt = stmt.where(
            (CommercialObservation.created_at < ts)
            | and_(
                CommercialObservation.created_at == ts,
                CommercialObservation.id < observation_id,
            )
        )
    rows = list(
        (
            await session.execute(
                stmt.order_by(
                    CommercialObservation.created_at.desc(), CommercialObservation.id.desc()
                ).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    nxt = None
    if len(rows) > limit:
        rows = rows[:limit]
        nxt = _encode_cursor(rows[-1].created_at, rows[-1].id)
    return rows, nxt
