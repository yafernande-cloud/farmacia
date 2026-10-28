"""Canal (Telegram), sesiones de auxiliar, eventos entrantes, mensajes, fotos y salida (HLD §6.1, §3.1, §8, §15).

Los vínculos entre entidades son claves compuestas (id, organization_id): la base impide mezclar
organizaciones. La identidad de una sesión es DECLARADA (selección por nombre); `identity_level`
queda listo para `pin` en el piloto.
"""

import uuid
from datetime import datetime
from decimal import Decimal  # noqa: F401
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

UUID_PK = dict(primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid())
_NOW = dict(server_default=func.now())
EMPTY_OBJ = text("'{}'::jsonb")
EMPTY_ARR = text("'[]'::jsonb")


class ChannelBinding(Base):
    """Chat autorizado de la farmacia. Un mensaje de otro chat se ignora."""

    __tablename__ = "channel_binding"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        Index(
            "uq_channel_binding_chat_active",
            "provider",
            "external_chat_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        CheckConstraint("provider IN ('telegram')", name="proveedor_valido"),
        CheckConstraint("chat_kind IN ('private','group')", name="tipo_valido"),
        CheckConstraint("status IN ('active','inactive')", name="estado_valido"),
    )
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    provider: Mapped[str] = mapped_column(String(20), server_default="telegram")
    external_chat_id: Mapped[str] = mapped_column(String(64))
    chat_kind: Mapped[str] = mapped_column(String(10), server_default="private")
    mode: Mapped[str] = mapped_column(String(20), server_default="shared_device")
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class ActorSession(Base):
    """Quién está reportando en el dispositivo compartido ahora. A lo más UNA activa por chat."""

    __tablename__ = "actor_session"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["channel_binding_id", "organization_id"], ["channel_binding.id", "channel_binding.organization_id"]),
        ForeignKeyConstraint(["auxiliary_id", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        CheckConstraint("identity_level IN ('declared','pin')", name="nivel_valido"),
        CheckConstraint("end_reason IS NULL OR end_reason IN ('replaced','expired','manual','revoked')", name="motivo_valido"),
        CheckConstraint("(ended_at IS NULL) = (end_reason IS NULL)", name="fin_coherente"),
        Index("uq_actor_session_active", "channel_binding_id", unique=True, postgresql_where=text("ended_at IS NULL")),
        Index("ix_actor_session_binding", "channel_binding_id", "started_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    channel_binding_id: Mapped[uuid.UUID] = mapped_column()
    auxiliary_id: Mapped[uuid.UUID] = mapped_column()
    identity_level: Mapped[str] = mapped_column(String(10), server_default="declared")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(10))
    selected_by_event_id: Mapped[uuid.UUID | None] = mapped_column()


class InboundEvent(Base):
    """Cada actualización recibida, una sola vez (idempotencia técnica, AC12)."""

    __tablename__ = "inbound_event"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("provider", "channel_binding_id", "external_event_id", name="uq_inbound_event_external"),
        ForeignKeyConstraint(["channel_binding_id", "organization_id"], ["channel_binding.id", "channel_binding.organization_id"]),
        CheckConstraint("status IN ('received','processing','processed','failed','ignored')", name="estado_valido"),
        Index("ix_inbound_event_status", "status", "received_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    provider: Mapped[str] = mapped_column(String(20))
    channel_binding_id: Mapped[uuid.UUID] = mapped_column()
    external_event_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12), server_default="received")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChannelMessage(Base):
    """Mensaje o foto recibidos. Sin identidad vigente queda `pending_attribution`: JAMÁS se
    atribuye solo a alguien (ni a la sesión anterior ni a la siguiente)."""

    __tablename__ = "channel_message"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("inbound_event_id", name="uq_channel_message_event"),
        ForeignKeyConstraint(["channel_binding_id", "organization_id"], ["channel_binding.id", "channel_binding.organization_id"]),
        ForeignKeyConstraint(["inbound_event_id", "organization_id"], ["inbound_event.id", "inbound_event.organization_id"]),
        ForeignKeyConstraint(["author_id", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        ForeignKeyConstraint(["session_id", "organization_id"], ["actor_session.id", "actor_session.organization_id"]),
        CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name="origen_dato_valido"),
        CheckConstraint("kind IN ('text','photo')", name="tipo_valido"),
        CheckConstraint("attribution_status IN ('attributed','pending_attribution','discarded')", name="atribucion_valida"),
        CheckConstraint("(attribution_status = 'attributed') = (author_id IS NOT NULL)", name="autor_coherente"),
        CheckConstraint(
            "attribution_basis IS NULL OR attribution_basis IN ('session_at_receipt','confirmed_after_identification')",
            name="base_valida",
        ),
        Index("ix_channel_message_pending", "channel_binding_id", "received_at", postgresql_where=text("attribution_status = 'pending_attribution'")),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    channel_binding_id: Mapped[uuid.UUID] = mapped_column()
    inbound_event_id: Mapped[uuid.UUID] = mapped_column()
    external_message_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(10))
    text: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # hora de envío según el canal
    # Procedencia del dato (NO es confianza ni confirmación farmacéutica):
    #   simulated = simulador/fixture/prueba · captured = recibido de una persona o dispositivo real ·
    #   ai_derived = resultado derivado por un modelo de IA · unclassified = solo organizaciones legacy.
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), **_NOW
    )  # hora del servidor
    attribution_status: Mapped[str] = mapped_column(String(20))
    author_id: Mapped[uuid.UUID | None] = mapped_column()
    session_id: Mapped[uuid.UUID | None] = mapped_column()
    attribution_basis: Mapped[str | None] = mapped_column(String(40))
    attributed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discard_reason: Mapped[str | None] = mapped_column(String(40))


class Attachment(Base):
    """Fotografía recibida. La imagen original se conserva en almacenamiento privado; el procesamiento
    visual tiene su propio estado y `extraction_mode` deja explícito si fue real o simulado."""

    __tablename__ = "attachment"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["channel_message_id", "organization_id"], ["channel_message.id", "channel_message.organization_id"]),
        ForeignKeyConstraint(["author_id", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        ForeignKeyConstraint(["duplicate_of_id", "organization_id"], ["attachment.id", "attachment.organization_id"]),
        CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name="origen_dato_valido"),
        CheckConstraint("status IN ('pending','processing','identified','needs_review','error')", name="estado_valido"),
        CheckConstraint("extraction_mode IS NULL OR extraction_mode IN ('live','simulated')", name="modo_valido"),
        CheckConstraint("extraction_provenance IS NULL OR extraction_provenance IN ('ai_derived','simulated')", name="procedencia_extraccion_valida"),
        Index("ix_attachment_status", "organization_id", "status", "created_at"),
        Index("ix_attachment_sha", "organization_id", "content_sha256"),
        Index("ix_attachment_unique_file", "organization_id", "provider_file_unique_id"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    channel_message_id: Mapped[uuid.UUID] = mapped_column()
    author_id: Mapped[uuid.UUID | None] = (
        mapped_column()
    )  # autor declarado; NULL si aún sin atribuir
    provider_file_id: Mapped[str] = mapped_column(String(256))
    provider_file_unique_id: Mapped[str | None] = mapped_column(String(128))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    file_size: Mapped[int | None] = mapped_column(Integer)
    caption: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(
        String(40)
    )  # verificada por firma de bytes, no por el cliente
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(String(200))  # None = aún no descargada
    status: Mapped[str] = mapped_column(String(14), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    extraction: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJ)
    extraction_mode: Mapped[str | None] = mapped_column(String(10))
    # procedencia del RESULTADO visual: ai_derived (modelo real) | simulated (simulador); NULL si no hubo lectura
    extraction_provenance: Mapped[str | None] = mapped_column(String(12))
    vision_provider: Mapped[str | None] = mapped_column(String(40))
    vision_model: Mapped[str | None] = mapped_column(String(60))
    uncertainties: Mapped[list[str]] = mapped_column(JSONB, server_default=EMPTY_ARR)
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Procedencia del dato (NO es confianza ni confirmación farmacéutica):
    #   simulated = simulador/fixture/prueba · captured = recibido de una persona o dispositivo real ·
    #   ai_derived = resultado derivado por un modelo de IA · unclassified = solo organizaciones legacy.
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class OutboundMessage(Base):
    """Respuestas al canal, persistidas en la misma transacción que el cambio y entregadas después
    (HLD §15). Si el envío falla, el reporte sigue guardado y se reintenta."""

    __tablename__ = "outbound_message"
    __table_args__ = (
        ForeignKeyConstraint(["channel_binding_id", "organization_id"], ["channel_binding.id", "channel_binding.organization_id"]),
        CheckConstraint("status IN ('pending','sent','failed')", name="estado_valido"),
        Index("uq_outbound_event_seq", "inbound_event_id", "seq", unique=True, postgresql_where=text("inbound_event_id IS NOT NULL")),
        Index("ix_outbound_pending", "next_attempt_at", postgresql_where=text("status = 'pending'")),
    )  # fmt: skip
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    channel_binding_id: Mapped[uuid.UUID] = mapped_column()
    inbound_event_id: Mapped[uuid.UUID | None] = mapped_column()
    seq: Mapped[int] = mapped_column(Integer, server_default="0")
    method: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(8), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
