"""Auditoría + outbox en la misma transacción que el cambio de dominio (HLD §15)."""

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import correlation_id_var
from app.models import AuditEvent, OutboxEvent


def request_hash(payload: dict[str, Any]) -> str:
    """Hash de una representación canónica del pedido, para detectar claves reutilizadas."""
    canon = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


async def advisory_lock(session: AsyncSession, key: str) -> None:
    """Bloqueo transaccional por clave lógica (se libera al terminar la transacción)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})


def emit(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    event_type: str,
    aggregate_version: int | None,
    payload: dict[str, Any],
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    cid = correlation_id_var.get()
    session.add(
        AuditEvent(
            organization_id=org_id, actor_id=actor_id, action=action, entity_type=entity_type,
            entity_id=entity_id, before=before, after=after, correlation_id=cid,
        )
    )  # fmt: skip
    session.add(
        OutboxEvent(
            organization_id=org_id, type=event_type, aggregate_id=entity_id,
            aggregate_version=aggregate_version, payload=payload, correlation_id=cid,
        )
    )  # fmt: skip
