"""Lecturas de la lista compartida. Consultas agregadas; sin N+1."""

import base64
import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.models import AppUser, DemandItem, DemandObservation, QuantityEvent
from app.services.actors import Actor


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


async def list_items(
    session: AsyncSession, actor: Actor, *, status: str, limit: int, cursor: str | None
):
    org = actor.organization_id
    q = select(DemandItem).where(DemandItem.organization_id == org, DemandItem.status == status)
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
