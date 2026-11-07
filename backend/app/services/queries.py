"""Lecturas de la lista compartida. Consultas agregadas; sin N+1."""

import base64
import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.models import AppUser, DemandItem, DemandObservation, QuantityEvent
from app.services.actors import Actor
from app.services.coverage import quoted_totals_subquery


def encode_cursor(created_at: datetime, id_: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{id_}".encode()).decode()


def decode_cursor(c: str) -> tuple[datetime, uuid.UUID]:
    try:
        ts, id_ = base64.urlsafe_b64decode(c.encode()).decode().split("|")
        return datetime.fromisoformat(ts), uuid.UUID(id_)
    except Exception:
        raise DomainError("INVALID_CURSOR", "Cursor inválido.", status_code=400) from None


async def aggregates(session: AsyncSession, org, item_ids: list[uuid.UUID]) -> dict:
    if not item_ids:
        return {}
    rows = await session.execute(
        select(
            DemandObservation.demand_item_id,
            func.count(DemandObservation.id),
            func.max(DemandObservation.created_at),
            func.array_agg(func.distinct(AppUser.display_name)),
        )
        .join(AppUser, and_(AppUser.id == DemandObservation.author_id,
                            AppUser.organization_id == DemandObservation.organization_id))
        .where(DemandObservation.organization_id == org, DemandObservation.demand_item_id.in_(item_ids))
        .group_by(DemandObservation.demand_item_id)
    )  # fmt: skip
    return {r[0]: (r[1], r[2], sorted(r[3])) for r in rows}


async def report_meta(
    session: AsyncSession, org, items: list[DemandItem]
) -> dict[uuid.UUID, dict]:
    """Autor, nombre y cantidad declarada de cada reporte independiente (D-135)."""
    if not items:
        return {}
    ids = [i.id for i in items]
    user_ids = {i.reported_by for i in items if i.reported_by}
    user_ids |= {i.cancelled_by for i in items if i.cancelled_by}
    names: dict = {}
    if user_ids:
        names = dict(
            (
                await session.execute(
                    select(AppUser.id, AppUser.display_name).where(
                        AppUser.organization_id == org, AppUser.id.in_(user_ids)
                    )
                )
            ).all()
        )
    declared = {
        r[0]: (r[1], r[2], (r[3] or {}).get("quantity_origin"))
        for r in (
            await session.execute(
                select(
                    DemandObservation.demand_item_id,
                    DemandObservation.declared_quantity,
                    DemandObservation.declared_unit,
                    DemandObservation.interpretation,
                )
                .where(
                    DemandObservation.organization_id == org,
                    DemandObservation.demand_item_id.in_(ids),
                    DemandObservation.declared_quantity.is_not(None),
                )
                .ext(distinct_on(DemandObservation.demand_item_id))
                .order_by(
                    DemandObservation.demand_item_id,
                    DemandObservation.created_at.desc(),
                    DemandObservation.id.desc(),
                )
            )
        ).all()
    }
    branches = dict(
        (
            await session.execute(
                select(
                    DemandObservation.demand_item_id, DemandObservation.branch_label
                )
                .where(
                    DemandObservation.organization_id == org,
                    DemandObservation.demand_item_id.in_(ids),
                    DemandObservation.branch_label.is_not(None),
                )
                .ext(distinct_on(DemandObservation.demand_item_id))
                .order_by(
                    DemandObservation.demand_item_id,
                    DemandObservation.created_at.desc(),
                    DemandObservation.id.desc(),
                )
            )
        ).all()
    )
    photos = dict(
        (
            await session.execute(
                select(
                    DemandObservation.demand_item_id, DemandObservation.attachment_id
                )
                .where(
                    DemandObservation.organization_id == org,
                    DemandObservation.demand_item_id.in_(ids),
                    DemandObservation.attachment_id.is_not(None),
                )
                .ext(distinct_on(DemandObservation.demand_item_id))
                .order_by(
                    DemandObservation.demand_item_id,
                    DemandObservation.created_at.desc(),
                    DemandObservation.id.desc(),
                )
            )
        ).all()
    )
    first = dict(
        (
            await session.execute(
                select(
                    DemandObservation.demand_item_id, func.min(DemandObservation.created_at)
                )
                .where(
                    DemandObservation.organization_id == org,
                    DemandObservation.demand_item_id.in_(ids),
                )
                .group_by(DemandObservation.demand_item_id)
            )
        ).all()
    )
    return {
        i.id: {
            "reported_by": i.reported_by,
            "reported_by_name": names.get(i.reported_by),
            "branch_label": branches.get(i.id),
            # Fotografía original del reporte, si algún aviso vino con una.
            "photo_attachment_id": photos.get(i.id),
            "cancelled_by_name": names.get(i.cancelled_by),
            "declared_quantity": declared.get(i.id, (None, None, None))[0],
            "declared_unit": declared.get(i.id, (None, None, None))[1],
            # «explicit» = lo escribió la persona; «default» = 1 unidad genérica (D-147).
            "requested_quantity_origin": declared.get(i.id, (None, None, None))[2],
            "reported_at": first.get(i.id),
        }
        for i in items
    }


async def latest_declared_quantities(
    session: AsyncSession, org, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple]:
    """Cantidad DECLARADA más reciente por faltante. No es la cantidad objetivo de compra."""
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            select(
                DemandObservation.demand_item_id,
                DemandObservation.declared_quantity,
                DemandObservation.declared_unit,
            )
            .where(
                DemandObservation.organization_id == org,
                DemandObservation.demand_item_id.in_(item_ids),
                DemandObservation.declared_quantity.is_not(None),
            )
            .ext(distinct_on(DemandObservation.demand_item_id))
            .order_by(
                DemandObservation.demand_item_id,
                DemandObservation.created_at.desc(),
                DemandObservation.id.desc(),
            )
        )
    ).all()
    return {r[0]: (r[1], r[2]) for r in rows}


async def reporter_names(
    session: AsyncSession, org, items: list[DemandItem]
) -> dict[uuid.UUID, str | None]:
    """Nombre de quien reportó cada faltante (D-135), desde `demand_item.reported_by`.

    La lista muestra al autor real de cada fila y no depende de las observaciones: dos personas
    que reportan el mismo producto tienen filas propias con su nombre y su cantidad.
    """
    if not items:
        return {}
    user_ids = {i.reported_by for i in items if i.reported_by}
    names: dict = {}
    if user_ids:
        names = dict(
            (
                await session.execute(
                    select(AppUser.id, AppUser.display_name).where(
                        AppUser.organization_id == org, AppUser.id.in_(user_ids)
                    )
                )
            ).all()
        )
    return {i.id: names.get(i.reported_by) for i in items}


async def last_texts(
    session: AsyncSession, org, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Último texto recibido por faltante, tal como se escribió (`last_raw_text`)."""
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            select(DemandObservation.demand_item_id, DemandObservation.raw_text)
            .where(
                DemandObservation.organization_id == org,
                DemandObservation.demand_item_id.in_(item_ids),
            )
            .ext(distinct_on(DemandObservation.demand_item_id))
            .order_by(
                DemandObservation.demand_item_id,
                DemandObservation.created_at.desc(),
                DemandObservation.id.desc(),
            )
        )
    ).all()
    return {item_id: raw for item_id, raw in rows if item_id is not None}


async def list_items(
    session: AsyncSession,
    actor: Actor,
    *,
    status: str,
    limit: int,
    cursor: str | None,
    pending_only: bool = False,
):
    org = actor.organization_id
    q = select(DemandItem).where(DemandItem.organization_id == org, DemandItem.status == status)
    if pending_only:
        # `pending_only` = activo con saldo sin cubrir por cotización ni compra. C5-A suma lo
        # cotizado; C5-B incorpora las compras vigentes a esta misma resta (§12.3).
        covered = quoted_totals_subquery(org)
        q = (
            q.outerjoin(covered, covered.c.demand_item_id == DemandItem.id)
            .where(
                DemandItem.status == "active",
                DemandItem.target_quantity.is_not(None),
                DemandItem.target_quantity - func.coalesce(covered.c.quantity, 0) > 0,
            )
        )
    if cursor:
        ts, id_ = decode_cursor(cursor)
        q = q.where(
            or_(DemandItem.created_at < ts, and_(DemandItem.created_at == ts, DemandItem.id < id_))
        )
    q = q.order_by(DemandItem.created_at.desc(), DemandItem.id.desc()).limit(limit + 1)
    items = list((await session.execute(q)).scalars())
    nxt = None
    if len(items) > limit:
        items = items[:limit]
        nxt = encode_cursor(items[-1].created_at, items[-1].id)
    agg = await aggregates(session, org, [i.id for i in items])
    pending = (
        await session.execute(
            select(func.count())
            .select_from(DemandObservation)
            .where(
                DemandObservation.organization_id == org,
                DemandObservation.match_status == "needs_review",
            )
        )
    ).scalar_one()
    return items, agg, nxt, pending


async def get_item_detail(session: AsyncSession, actor: Actor, item_id: uuid.UUID):
    org = actor.organization_id
    item = (
        await session.execute(
            select(DemandItem).where(DemandItem.id == item_id, DemandItem.organization_id == org)
        )
    ).scalar_one_or_none()
    if item is None:
        raise not_found("La necesidad")
    obs = (
        await session.execute(
            select(DemandObservation, AppUser.display_name)
            .join(AppUser, and_(AppUser.id == DemandObservation.author_id,
                                AppUser.organization_id == DemandObservation.organization_id))
            .where(DemandObservation.organization_id == org, DemandObservation.demand_item_id == item_id)
            .order_by(DemandObservation.created_at)
        )
    ).all()  # fmt: skip
    evs = (
        await session.execute(
            select(QuantityEvent, AppUser.display_name)
            .join(AppUser, and_(AppUser.id == QuantityEvent.actor_id,
                                AppUser.organization_id == QuantityEvent.organization_id))
            .where(QuantityEvent.organization_id == org, QuantityEvent.demand_item_id == item_id)
            .order_by(QuantityEvent.created_at)
        )
    ).all()  # fmt: skip
    agg = await aggregates(session, org, [item_id])
    return item, agg, obs, evs


async def list_pending_observations(session: AsyncSession, actor: Actor, limit: int):
    return (
        await session.execute(
            select(DemandObservation, AppUser.display_name)
            .join(AppUser, and_(AppUser.id == DemandObservation.author_id,
                                AppUser.organization_id == DemandObservation.organization_id))
            .where(DemandObservation.organization_id == actor.organization_id,
                   DemandObservation.match_status == "needs_review")
            .order_by(DemandObservation.created_at).limit(limit)
        )
    ).all()  # fmt: skip


async def list_items_page(session: AsyncSession, org, *, page: int, size: int):
    """Página de la lista compartida para chats (offset; la lista activa es pequeña)."""
    q = (
        select(DemandItem)
        .where(DemandItem.organization_id == org, DemandItem.status == "active")
        .order_by(DemandItem.created_at.desc(), DemandItem.id.desc())
        .offset(page * size).limit(size + 1)
    )  # fmt: skip
    items = list((await session.execute(q)).scalars())
    more = len(items) > size
    items = items[:size]
    agg = await aggregates(session, org, [i.id for i in items])
    total = (
        await session.execute(
            select(func.count())
            .select_from(DemandItem)
            .where(DemandItem.organization_id == org, DemandItem.status == "active")
        )
    ).scalar_one()
    pending = (
        await session.execute(
            select(func.count())
            .select_from(DemandObservation)
            .where(
                DemandObservation.organization_id == org,
                DemandObservation.match_status == "needs_review",
            )
        )
    ).scalar_one()
    return items, agg, more, total, pending
