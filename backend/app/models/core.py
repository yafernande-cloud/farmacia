"""Organización, usuarios y auxiliares (HLD §6.1).

Todas las entidades de negocio llevan organization_id y las claves foráneas son compuestas
(id, organization_id): la base impide enlazar datos de organizaciones distintas.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

ROLES = "ARRAY['auxiliary','purchasing','approver','admin']::text[]"


class Organization(Base):
    """Cada organización es un universo aislado de datos. `kind` separa SIMULACIÓN, prueba real y
    producción: es inmutable y gobierna qué procedencia (`data_origin`) puede tener cada registro."""

    __tablename__ = "organization"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('simulation','real_test','production','legacy_mixed')", name="tipo_valido"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String(200), unique=True)
    # simulation: solo datos simulados y pruebas automatizadas (chats ficticios 555000x)
    # real_test : bot real de pruebas, datos capturados de verdad
    # production: operación real
    # legacy_mixed: transitorio para datos previos a la separación; no recibe simulaciones nuevas
    kind: Mapped[str] = mapped_column(String(14))
    timezone: Mapped[str] = mapped_column(String(64), server_default="America/Santiago")
    currency: Mapped[str] = mapped_column(String(3), server_default="CLP")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppUser(Base):
    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        CheckConstraint(f"roles <@ {ROLES}", name="roles_validos"),
        CheckConstraint(
            "absence_reason IS NULL OR absence_reason IN ('vacation','sick_leave','day_off','other')",
            name="motivo_ausencia_valido",
        ),
        # No disponible exige un motivo; disponible no puede tener motivo de ausencia.
        CheckConstraint("available = (absence_reason IS NULL)", name="disponibilidad_coherente"),
        # Dos personas activas de la misma farmacia no pueden llamarse igual (los botones serían ambiguos).
        Index(
            "uq_app_user_active_name",
            "organization_id",
            func.lower(text("display_name")),
            unique=True,
            postgresql_where=text("active"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    organization_id: Mapped[uuid.UUID] = mapped_column()
    display_name: Mapped[str] = mapped_column(String(100))
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("ARRAY[]::text[]"))
    # Sujeto (`sub`) del JWT de Supabase Auth; se enlaza cuando se active la autenticación web.
    auth_subject: Mapped[str | None] = mapped_column(String(100), unique=True)
    # PERTENENCIA a la farmacia (sigue en el historial aunque se desactive; nunca se elimina).
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # DISPONIBILIDAD para reportar (vacaciones, licencia, día libre…). Distinta de pertenecer y de tener
    # una sesión de Telegram activa (esta última se deriva de `actor_session`).
    available: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    absence_reason: Mapped[str | None] = mapped_column(String(20))
    absence_note: Mapped[str | None] = mapped_column(String(200))
    unavailable_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuxiliaryProfile(Base):
    """Datos específicos de auxiliar. pin_hash nulo = identidad declarada (demo)."""

    __tablename__ = "auxiliary_profile"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    pin_hash: Mapped[str | None] = mapped_column(String(300))
    pin_failed_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    pin_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
