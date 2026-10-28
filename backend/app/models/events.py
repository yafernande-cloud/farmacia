"""Auditoría y outbox (HLD §15). Se escriben en la misma transacción que el cambio de dominio."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditEvent(Base):
    """Solo se agrega: un trigger de la migración bloquea UPDATE y DELETE."""

    __tablename__ = "audit_event"
    __table_args__ = (
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["actor_id", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        Index("ix_audit_event_entity", "organization_id", "entity_type", "entity_id", "created_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column()
    actor_id: Mapped[uuid.UUID | None] = mapped_column()
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[uuid.UUID] = mapped_column()
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxEvent(Base):
    """Eventos de dominio durables. `seq` ordena la entrega y servirá de cursor SSE (H1-B).

    Entrega al menos una vez: los consumidores deduplican por event_id.
    """

    __tablename__ = "outbox_event"
    __table_args__ = (
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        Index("ix_outbox_event_unpublished", "seq", postgresql_where=text("published_at IS NULL")),
        Index("ix_outbox_event_org_seq", "organization_id", "seq"),
    )
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        unique=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column()
    type: Mapped[str] = mapped_column(String(80))
    schema_version: Mapped[int] = mapped_column(Integer, server_default="1")
    aggregate_id: Mapped[uuid.UUID] = mapped_column()
    aggregate_version: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    correlation_id: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
