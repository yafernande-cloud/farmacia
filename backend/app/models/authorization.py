"""Acceso de cuentas de Telegram: solicitudes, aprobación, bloqueo y trazabilidad.

Cuatro conceptos distintos (no confundirlos):
  * `telegram_access_request` = una cuenta de Telegram que PIDE acceso. Mientras está
    `pending` no tiene ningún permiso operativo: no existe cuenta ni chat habilitado.
  * `telegram_account` = la CUENTA de Telegram autorizada (modo personal o teléfono compartido).
  * `channel_binding` = el CHAT vinculado a una organización (puede existir sin cuenta, legado).
  * `app_user` = la PERSONA de la farmacia. En modo personal la cuenta está ligada a una persona;
    en el teléfono compartido la persona se sigue eligiendo en `actor_session`.

La solicitud guarda SOLO el SHA-256 del código; el código en claro se envía una vez por Telegram
y nunca se persiste, audita ni registra. `access_role` (admin/operator) describe el tipo de cuenta
para un uso administrativo futuro: NO otorga permisos de negocio. Los permisos siguen saliendo de
`app_user.roles`.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

UUID_PK = dict(primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid())
_NOW = dict(server_default=func.now())


class TelegramAccessRequest(Base):
    """Solicitud de acceso hecha desde el bot. El código se genera aquí y solo se guarda su hash."""

    __tablename__ = "telegram_access_request"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("code_hash", name="uq_telegram_access_request_code"),
        # Idempotencia de Telegram para un chat que todavía no tiene `inbound_event` (no hay vínculo).
        UniqueConstraint("provider", "request_update_id", name="uq_telegram_access_request_update"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["approved_account_id", "organization_id"],
            ["telegram_account.id", "telegram_account.organization_id"],
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["rejected_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
        ),
        CheckConstraint("provider IN ('telegram')", name="proveedor_valido"),
        CheckConstraint("status IN ('pending','approved','rejected','expired')", name="estado_valido"),
        CheckConstraint("expires_at > created_at", name="vigencia_valida"),
        CheckConstraint("(approved_at IS NULL) = (approved_by IS NULL)", name="aprobacion_autor_valida"),
        CheckConstraint(
            "(approved_at IS NULL) = (approved_account_id IS NULL)", name="aprobacion_cuenta_valida"
        ),
        CheckConstraint("(rejected_at IS NULL) = (rejected_by IS NULL)", name="rechazo_autor_valido"),
        CheckConstraint(
            "(status = 'approved') = (approved_at IS NOT NULL)", name="estado_aprobado_valido"
        ),
        CheckConstraint(
            "(status = 'rejected') = (rejected_at IS NOT NULL)", name="estado_rechazado_valido"
        ),
        # A lo más UNA solicitud pendiente por cuenta de Telegram (en cualquier organización).
        Index(
            "uq_telegram_access_request_pending_account",
            "provider",
            "external_user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_telegram_access_request_org", "organization_id", "created_at"),
        Index(
            "ix_telegram_access_request_pending",
            "organization_id",
            "expires_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    provider: Mapped[str] = mapped_column(String(20), server_default="telegram")
    external_user_id: Mapped[str] = mapped_column(String(64))
    external_chat_id: Mapped[str] = mapped_column(String(64))
    request_update_id: Mapped[str] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column()
    approved_account_id: Mapped[uuid.UUID | None] = mapped_column()
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[uuid.UUID | None] = mapped_column()
    reject_reason: Mapped[str | None] = mapped_column(String(200))


class TelegramAccount(Base):
    """Cuenta de Telegram autorizada. `external_user_id` es el id de usuario de Telegram.

    - personal: ligada a una persona (`app_user_id`); el bot la identifica sin preguntar.
    - shared_device: teléfono compartido; la persona se elige en cada sesión (flujo existente).

    `origin` deja explícito cómo nació la fila: `backfill` (migración de chats ya autorizados),
    `access_request` (aprobación de una solicitud) o `manual` (herramienta de bootstrap).
    """

    __tablename__ = "telegram_account"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("provider", "external_user_id", name="uq_telegram_account_external"),
        UniqueConstraint("created_from_request_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["created_from_request_id", "organization_id"],
            ["telegram_access_request.id", "telegram_access_request.organization_id"],
        ),
        ForeignKeyConstraint(["app_user_id", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        ForeignKeyConstraint(["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        ForeignKeyConstraint(["blocked_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        ForeignKeyConstraint(
            ["reactivated_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        CheckConstraint("provider IN ('telegram')", name="proveedor_valido"),
        CheckConstraint("mode IN ('personal','shared_device')", name="modo_valido"),
        CheckConstraint("access_role IN ('admin','operator')", name="rol_valido"),
        CheckConstraint("status IN ('active','blocked')", name="estado_valido"),
        CheckConstraint("origin IN ('backfill','access_request','manual')", name="origen_valido"),
        # Una cuenta personal SIEMPRE está ligada a una persona; una de teléfono compartido, nunca.
        CheckConstraint("(mode = 'personal') = (app_user_id IS NOT NULL)", name="persona_coherente"),
        CheckConstraint("(status = 'blocked') = (blocked_at IS NOT NULL)", name="bloqueo_coherente"),
        Index("ix_telegram_account_org_status", "organization_id", "status"),
        Index("ix_telegram_account_person", "organization_id", "app_user_id"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    provider: Mapped[str] = mapped_column(String(20), server_default="telegram")
    external_user_id: Mapped[str] = mapped_column(String(64))
    display_label: Mapped[str] = mapped_column(String(100))
    # Sucursal de esta cuenta/teléfono. Se copia a cada observación para conservar el histórico.
    branch_label: Mapped[str | None] = mapped_column(String(120))
    mode: Mapped[str] = mapped_column(String(20))
    access_role: Mapped[str] = mapped_column(String(10), server_default="operator")
    app_user_id: Mapped[uuid.UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    origin: Mapped[str] = mapped_column(String(16), server_default="access_request")
    created_from_request_id: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_by: Mapped[uuid.UUID | None] = mapped_column()
    block_reason: Mapped[str | None] = mapped_column(String(200))
    reactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reactivated_by: Mapped[uuid.UUID | None] = mapped_column()


class TelegramAccountAuxiliary(Base):
    """Persona HABILITADA para reportar desde una cuenta de teléfono compartido.

    No otorga roles ni permisos: la persona igual debe estar activa, disponible y tener un rol que
    permita reportar. Es una asociación explícita por cuenta, con baja lógica e historial auditado.
    Las cuentas personales no usan esta tabla: su identidad ya está fijada en `app_user_id`.
    """

    __tablename__ = "telegram_account_auxiliary"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("account_id", "app_user_id", name="uq_telegram_account_auxiliary_pair"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["account_id", "organization_id"],
            ["telegram_account.id", "telegram_account.organization_id"],
        ),
        ForeignKeyConstraint(
            ["app_user_id", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        ForeignKeyConstraint(
            ["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        ForeignKeyConstraint(
            ["deactivated_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        # Activo no tiene baja; inactivo siempre conserva cuándo y quién lo dio de baja.
        CheckConstraint(
            "(active AND deactivated_at IS NULL AND deactivated_by IS NULL) OR "
            "(NOT active AND deactivated_at IS NOT NULL AND deactivated_by IS NOT NULL)",
            name="desactivacion_coherente",
        ),
        Index("ix_telegram_account_auxiliary_active", "account_id", "active"),
        Index("ix_telegram_account_auxiliary_org", "organization_id"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    account_id: Mapped[uuid.UUID] = mapped_column()
    app_user_id: Mapped[uuid.UUID] = mapped_column()
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivated_by: Mapped[uuid.UUID | None] = mapped_column()
    deactivation_reason: Mapped[str | None] = mapped_column(String(200))


class TelegramAccessAttempt(Base):
    """Intento de usar un código en la pantalla administrativa.

    Solo se guarda el hash del código (igual que en la solicitud), NUNCA el código en claro. Sirve
    para limitar intentos fallidos por administrador y por código. No es un registro de auditoría
    visible: la API no lo expone.
    """

    __tablename__ = "telegram_access_attempt"
    __table_args__ = (
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["app_user_id", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        Index("ix_telegram_access_attempt_admin", "organization_id", "app_user_id", "attempted_at"),
        Index("ix_telegram_access_attempt_code", "organization_id", "code_hash", "attempted_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    app_user_id: Mapped[uuid.UUID] = mapped_column()
    code_hash: Mapped[str] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class WebAccessRequest(Base):
    """Solicitud de acceso web (G1/D-130). Distinta de `telegram_access_request`: aquí la identidad
    ya está verificada por Supabase Auth y lo que falta es la habilitación de la persona.

    Una solicitud NO otorga nada. Solo la aprobación de un `admin` de la organización crea el
    `app_user` con `roles = ['admin']`; nunca enlaza ni sobrescribe una fila existente. El correo
    solo es informativo: la identidad es `auth_subject`.
    """

    __tablename__ = "web_access_request"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["decided_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
        ),
        CheckConstraint("status IN ('pending','approved','rejected')", name="estado_valido"),
        CheckConstraint("length(btrim(auth_subject)) > 0", name="sujeto_valido"),
        CheckConstraint("length(btrim(email)) > 0", name="email_valido"),
        CheckConstraint(
            "decision_note IS NULL OR length(decision_note) <= 500", name="nota_valida"
        ),
        CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name="origen_dato_valido",
        ),
        # La decisión y su autor van juntos; `pending` no tiene decisión.
        CheckConstraint(
            "(decided_at IS NULL) = (decided_by IS NULL)", name="decision_coherente"
        ),
        CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL)", name="estado_coherente"
        ),
        # Una sola solicitud PENDIENTE por persona y organización; las rechazadas se conservan.
        Index(
            "uq_web_access_request_pending_subject",
            "organization_id",
            "auth_subject",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_web_access_request_org_status", "organization_id", "status", "requested_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    auth_subject: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10), server_default="pending")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column()
    decision_note: Mapped[str | None] = mapped_column(String(500))
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class WebAccessGrant(Base):
    """Autorización WEB vigente de una persona (D-158).

    Separada de `app_user` a propósito: la persona (identidad, `auth_subject`, roles e historial)
    se conserva siempre, mientras que esta fila es la autorización que se puede **eliminar**.
    Sin fila no hay acceso web, aunque el JWT de Google siga vigente y la persona conserve el rol
    `admin`. `expires_at` conserva el vencimiento programable; `NULL` = sin vencimiento.

    Una sola autorización por persona (`app_user_id` es la PK). El historial de otorgamientos y
    eliminaciones vive en `web_access_request` y en la auditoría (`events.emit`), no aquí.
    """

    __tablename__ = "web_access_grant"
    __table_args__ = (
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["app_user_id", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
            name="fk_web_access_grant_app_user",
        ),
        ForeignKeyConstraint(
            ["granted_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
            name="fk_web_access_grant_granted_by",
        ),
        ForeignKeyConstraint(
            ["request_id", "organization_id"],
            ["web_access_request.id", "web_access_request.organization_id"],
            name="fk_web_access_grant_request",
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > granted_at", name="vigencia_coherente"
        ),
        Index("ix_web_access_grant_org", "organization_id"),
    )
    app_user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    request_id: Mapped[uuid.UUID | None] = mapped_column()
    granted_by: Mapped[uuid.UUID | None] = mapped_column()
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
