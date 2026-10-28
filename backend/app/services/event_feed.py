"""Feed de eventos de dominio (base del SSE de H1-C). SIEMPRE acotado a la organización del actor:
nunca se consulta el outbox sin filtrar por `organization_id`, así un panel de una organización jamás
ve eventos de otra (p. ej. la simulación no se mezcla con la prueba real)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission, require
from app.models import OutboxEvent
from app.services.actors import Actor


async def fetch_events(
    session: AsyncSession, actor: Actor, *, after_seq: int = 0, limit: int = 100
) -> list[dict]:
    require(actor.roles, Permission.VIEW_LIST)
    rows = (
        await session.execute(
            select(OutboxEvent)
            .where(
                OutboxEvent.organization_id == actor.organization_id, OutboxEvent.seq > after_seq
            )
            .order_by(OutboxEvent.seq)
            .limit(max(1, min(limit, 500)))
        )
    ).scalars()
    return [
        {"seq": e.seq, "event_id": str(e.event_id), "type": e.type, "schema_version": e.schema_version,
         "organization_id": str(e.organization_id), "aggregate_id": str(e.aggregate_id),
         "aggregate_version": e.aggregate_version, "occurred_at": e.occurred_at.isoformat(),
         "correlation_id": e.correlation_id, "payload": e.payload}
        for e in rows
    ]  # fmt: skip
