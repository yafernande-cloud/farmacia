"""Ciclo de compra, faltantes, observaciones y cantidades (HLD §6.1, §6.4, §7)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
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


class PurchaseCycle(Base):
    __tablename__ = "purchase_cycle"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        CheckConstraint("status IN ('open','closed')", name="estado_valido"),
        # Una sola lista abierta por organización (ciclo piloto explícito, HLD §25).
        Index(
            "uq_purchase_cycle_open",
            "organization_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(10), server_default="open")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DemandItem(Base):
    """Necesidad de compra. Una línea por producto en el ciclo; muchos avisos la respaldan."""

    __tablename__ = "demand_item"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["cycle_id", "organization_id"], ["purchase_cycle.id", "purchase_cycle.organization_id"]),
        CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name="origen_dato_valido"),
        # `released`: el reporte quedó cubierto por completo en una cotización finalizada y se
        # libera para que la misma persona pueda volver a reportarlo (la historia no se borra).
        CheckConstraint(
            "status IN ('active','cancelled','fulfilled','released')", name="estado_valido"
        ),
        CheckConstraint("target_quantity IS NULL OR target_quantity >= 0", name="cantidad_no_negativa"),
        # Cantidad y unidad van juntas: nunca una cantidad sin unidad ni al revés (inv. 4).
        CheckConstraint("(target_quantity IS NULL) = (quantity_unit IS NULL)", name="cantidad_con_unidad"),
        CheckConstraint(
            "jsonb_typeof(attributes) = 'object' AND attributes ? 'schema_version'", name="atributos_validos"
        ),
        CheckConstraint("quantity_unit IS NULL OR quantity_unit IN ('box','package','unit','bottle')", name="unidad_valida"),
        # La cancelación es lógica: una necesidad cancelada conserva su historial y sus avisos.
        CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL)", name="cancelacion_coherente"
        ),
        CheckConstraint(
            "cancelled_by IS NULL OR status = 'cancelled'", name="cancelada_por_coherente"
        ),
        CheckConstraint(
            "cancellation_reason IS NULL OR status = 'cancelled'", name="motivo_cancelacion_coherente"
        ),
        # Una necesidad pertenece a la persona que la reportó. Dos personas NUNCA comparten fila:
        # cada reporte conserva su autor, su cantidad declarada y su historial (D-135).
        ForeignKeyConstraint(
            ["reported_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
            name="fk_demand_item_reported_by_app_user",
        ),
        ForeignKeyConstraint(
            ["cancelled_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
            name="fk_demand_item_cancelled_by_app_user",
        ),
        # La liberación la hace una cotización finalizada y conserva autor y fecha.
        ForeignKeyConstraint(
            ["released_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
            name="fk_demand_item_released_by_app_user",
        ),
        ForeignKeyConstraint(
            ["released_quote_id", "organization_id"],
            ["quote.id", "quote.organization_id"],
            name="fk_demand_item_released_quote",
        ),
        # Liberado exige fecha, autor y cotización; cualquier otro estado no los conserva.
        CheckConstraint(
            "(status = 'released') = (released_at IS NOT NULL AND released_by IS NOT NULL"
            " AND released_quote_id IS NOT NULL)",
            name="liberacion_coherente",
        ),
        # Respaldo de la deduplicación por autor: una misma persona no crea dos necesidades idénticas
        # en el mismo ciclo; dos personas distintas sí tienen una cada una (D-135).
        Index(
            "uq_demand_item_identity_active",
            "organization_id", "cycle_id", "resolved_identity_key", "reported_by",
            unique=True,
            postgresql_where=text(
                "status = 'active' AND resolved_identity_key IS NOT NULL "
                "AND reported_by IS NOT NULL"
            ),
        ),
        Index("ix_demand_item_cycle_listing", "organization_id", "cycle_id", "created_at", "id"),
        Index("ix_demand_item_family", "organization_id", "cycle_id", "ingredient"),
        Index("ix_demand_item_reporter", "organization_id", "cycle_id", "reported_by", "status"),
        Index("ix_demand_item_pharma", "organization_id", "pharma_identity_key"),
        Index("ix_demand_item_released_quote", "organization_id", "released_quote_id"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    cycle_id: Mapped[uuid.UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(12), server_default="active")
    # Autor del reporte (NULL solo en filas legado consolidadas antes de la 0016).
    reported_by: Mapped[uuid.UUID | None] = mapped_column()
    # Nombre del autor al crear el reporte (D3). NULL en filas anteriores a la 0026: la vista usa
    # el nombre actual como respaldo, sin reescribir el historial.
    reported_by_name: Mapped[str | None] = mapped_column(String(100))
    # Cancelación lógica: nunca se borra la fila ni sus observaciones.
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column()
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    # Liberación al finalizar una cotización que cubrió todo lo pedido (no es compra).
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_by: Mapped[uuid.UUID | None] = mapped_column()
    released_quote_id: Mapped[uuid.UUID | None] = mapped_column()
    display_name: Mapped[str] = mapped_column(String(300))
    ingredient: Mapped[str] = mapped_column(String(200))
    # Atributos estructurados (esquema v2): potencias con su unidad original, contenido, forma,
    # envase, medidas sin unidad pendientes y la PROCEDENCIA de cada atributo
    # (explicit | inferred | confirmed). Lo desconocido no se guarda como valor.
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("""'{"schema_version": 2}'::jsonb""")
    )
    resolved_identity_key: Mapped[str | None] = mapped_column(String(400))
    # Identidad farmacéutica (principio+potencia+forma+liberación). Varios artículos comerciales
    # (laboratorio, marca, GTIN, envase) pueden compartirla sin ser el mismo SKU.
    pharma_identity_key: Mapped[str | None] = mapped_column(String(400))
    # Cantidad objetivo: NULL = desconocida (no 0 ni 1).
    target_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    quantity_unit: Mapped[str | None] = mapped_column(String(10))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    # Procedencia del dato (NO es confianza ni confirmación farmacéutica):
    #   simulated = simulador/fixture/prueba · captured = recibido de una persona o dispositivo real ·
    #   ai_derived = resultado derivado por un modelo de IA · unclassified = solo organizaciones legacy.
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class DemandObservation(Base):
    """Un aviso de una persona. No es una cantidad (inv. 1 y 3)."""

    __tablename__ = "demand_observation"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        # Idempotencia técnica: el mismo evento/clave produce una sola observación (AC12).
        UniqueConstraint("organization_id", "idempotency_key", name="uq_demand_observation_idempotency"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["cycle_id", "organization_id"], ["purchase_cycle.id", "purchase_cycle.organization_id"]),
        ForeignKeyConstraint(["demand_item_id", "organization_id"], ["demand_item.id", "demand_item.organization_id"]),
        ForeignKeyConstraint(["author_id", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        ForeignKeyConstraint(["actor_session_id", "organization_id"], ["actor_session.id", "actor_session.organization_id"]),
        ForeignKeyConstraint(["channel_message_id", "organization_id"], ["channel_message.id", "channel_message.organization_id"]),
        ForeignKeyConstraint(["attachment_id", "organization_id"], ["attachment.id", "attachment.organization_id"]),
        ForeignKeyConstraint(["account_id", "organization_id"], ["telegram_account.id", "telegram_account.organization_id"]),
        CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name="origen_dato_valido"),
        CheckConstraint("source IN ('api','telegram_text','telegram_photo','photo_manual')", name="origen_valido"),
        CheckConstraint("identity_level IN ('api_user','declared','pin','account')", name="nivel_valido"),
        CheckConstraint("identity_level <> 'account' OR account_id IS NOT NULL", name="cuenta_coherente"),
        CheckConstraint("match_status IN ('created_item','attached_existing','needs_review','resolved_manual')", name="match_valido"),
        # Una observación pendiente no tiene destino; las demás sí.
        CheckConstraint("(match_status = 'needs_review') = (demand_item_id IS NULL)", name="destino_coherente"),
        CheckConstraint("declared_quantity IS NULL OR declared_quantity > 0", name="declarada_positiva"),
        CheckConstraint("jsonb_typeof(quantity_proposal) = 'object'", name="propuesta_valida"),
        Index("ix_demand_observation_item", "organization_id", "demand_item_id", "created_at"),
        Index(
            "ix_demand_observation_pending", "organization_id", "created_at",
            postgresql_where=text("match_status = 'needs_review'"),
        ),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    cycle_id: Mapped[uuid.UUID] = mapped_column()
    demand_item_id: Mapped[uuid.UUID | None] = mapped_column()
    author_id: Mapped[uuid.UUID] = mapped_column()
    # Nombre del autor en el momento del aviso (D3). NULL en avisos anteriores a la 0026: la vista
    # usa el nombre actual como respaldo. La autoría real sigue siendo `author_id` (UUID).
    author_name_snapshot: Mapped[str | None] = mapped_column(String(100))
    raw_text: Mapped[str] = mapped_column(Text)
    parsed: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # Sugerencia del declarante; JAMÁS se aplica al objetivo por sí sola.
    declared_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    declared_unit: Mapped[str | None] = mapped_column(String(10))
    match_status: Mapped[str] = mapped_column(String(20))
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    policy_version: Mapped[str] = mapped_column(String(40))
    # Qué propuso la capa de interpretación (alias aplicados, candidatos, incertidumbres, visión).
    interpretation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    # Propuesta de cantidad que el canal deja a la administración (nunca se aplica sola):
    # {"operation": "SET"|"ADD", "value": "...", "unit": "...", "target_quantity": "...",
    #  "status": "pending", "source": "telegram"}.
    quantity_proposal: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    # Trazabilidad: por qué canal llegó, bajo qué sesión declarada y desde qué mensaje/foto.
    source: Mapped[str] = mapped_column(String(20), server_default="api")
    identity_level: Mapped[str] = mapped_column(String(10), server_default="api_user")
    actor_session_id: Mapped[uuid.UUID | None] = mapped_column()
    channel_message_id: Mapped[uuid.UUID | None] = mapped_column()
    attachment_id: Mapped[uuid.UUID | None] = mapped_column()
    # Cuenta de Telegram que originó el aviso (NULL por API o vínculos legado).
    account_id: Mapped[uuid.UUID | None] = mapped_column()
    # Sucursal de la cuenta en el momento del registro; no se reescribe si la cuenta cambia.
    branch_label: Mapped[str | None] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    # Procedencia del dato (NO es confianza ni confirmación farmacéutica):
    #   simulated = simulador/fixture/prueba · captured = recibido de una persona o dispositivo real ·
    #   ai_derived = resultado derivado por un modelo de IA · unclassified = solo organizaciones legacy.
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class QuantityEvent(Base):
    """Historial inmutable de cambios de cantidad (SET/ADD/CLEAR)."""

    __tablename__ = "quantity_event"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_quantity_event_idempotency"),
        ForeignKeyConstraint(["demand_item_id", "organization_id"], ["demand_item.id", "demand_item.organization_id"]),
        ForeignKeyConstraint(["actor_id", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        CheckConstraint("operation IN ('SET','ADD','CLEAR')", name="operacion_valida"),
        Index("ix_quantity_event_item", "organization_id", "demand_item_id", "created_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    demand_item_id: Mapped[uuid.UUID] = mapped_column()
    actor_id: Mapped[uuid.UUID] = mapped_column()
    operation: Mapped[str] = mapped_column(String(5))
    value: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    unit: Mapped[str | None] = mapped_column(String(10))
    before_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    before_unit: Mapped[str | None] = mapped_column(String(10))
    after_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    after_unit: Mapped[str | None] = mapped_column(String(10))
    version_before: Mapped[int] = mapped_column(Integer)
    version_after: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class ProductAlias(Base):
    """Memoria verificada: una persona autorizada confirmó que, para este principio activo,
    una cifra sin unidad significa algo concreto (p. ej. «ibuprofeno 400» = 400 mg).

    Solo cubre ese principio activo y esa cifra; es revisable y desactivable. Nunca nace de
    una inferencia del sistema.
    """

    __tablename__ = "product_alias"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["confirmed_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        CheckConstraint("status IN ('active','inactive')", name="estado_valido"),
        CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name="origen_dato_valido"),
        CheckConstraint("kind IN ('measure_unit','expression')", name="tipo_valido"),
        Index(
            "uq_product_alias_active", "organization_id", "kind", "ingredient", "source_value",
            unique=True, postgresql_where=text("status = 'active'"),
        ),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    ingredient: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(12), server_default="measure_unit")
    source_value: Mapped[str] = mapped_column(
        String(40)
    )  # la cifra tal cual ("400"); '' en expression
    # Qué atributos confirmó la persona (p. ej. ["strength.unit"]). Todo lo demás —forma, contenido,
    # laboratorio, presentación comercial— NO queda validado por el alias.
    confirmed_attributes: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    assignment: Mapped[dict[str, Any]] = mapped_column(
        JSONB
    )  # {"role": "strength", "unit_expr": "mg"}
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    confirmed_by: Mapped[uuid.UUID] = mapped_column()
    source_observation_id: Mapped[uuid.UUID | None] = mapped_column()
    # Procedencia del dato (NO es confianza ni confirmación farmacéutica):
    #   simulated = simulador/fixture/prueba · captured = recibido de una persona o dispositivo real ·
    #   ai_derived = resultado derivado por un modelo de IA · unclassified = solo organizaciones legacy.
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PharmaIdentity(Base):
    """Catálogo mínimo de identidades FARMACÉUTICAS verificadas (principio+potencia[+forma]).

    Sirve para proponer candidatos («vitamina d 50000» → 50.000 UI), nunca para confirmar solo.
    El catálogo COMERCIAL (marca, laboratorio, GTIN, envase, SKU por proveedor) llega en H3 y
    colgará de esta identidad: una identidad farmacéutica → muchos artículos comerciales.
    """

    __tablename__ = "pharma_identity"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        CheckConstraint("status IN ('active','inactive')", name="estado_valido"),
        CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name="origen_dato_valido"),
        CheckConstraint("source IN ('manual','supplier','import')", name="origen_valido"),
        Index("ix_pharma_identity_ingredient", "organization_id", "ingredient", postgresql_where=text("status = 'active'")),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    label: Mapped[str] = mapped_column(String(300))
    ingredient: Mapped[str] = mapped_column(String(200))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB
    )  # strengths, form, release (esquema v2)
    source: Mapped[str] = mapped_column(String(10), server_default="manual")
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    created_by: Mapped[uuid.UUID] = mapped_column()
    # Procedencia del dato (NO es confianza ni confirmación farmacéutica):
    #   simulated = simulador/fixture/prueba · captured = recibido de una persona o dispositivo real ·
    #   ai_derived = resultado derivado por un modelo de IA · unclassified = solo organizaciones legacy.
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class CommercialReference(Base):
    """Embrión del catálogo COMERCIAL (completo en H3): una referencia verificada por una persona
    que enlaza marca + laboratorio (+ GTIN / registro) con una identidad farmacéutica.

    Una identidad farmacéutica tiene MUCHAS referencias comerciales: mismo principio, potencia y
    forma con distinto laboratorio/marca/envase son artículos distintos para compras, precios y stock.
    Solo propone candidatos; nunca confirma sola.
    """

    __tablename__ = "commercial_reference"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["pharma_identity_id", "organization_id"], ["pharma_identity.id", "pharma_identity.organization_id"]),
        ForeignKeyConstraint(["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        CheckConstraint("status IN ('active','inactive')", name="estado_valido"),
        CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name="origen_dato_valido"),
        CheckConstraint("source IN ('manual','supplier','import')", name="origen_valido"),
        Index("ix_commercial_reference_brand", "organization_id", "brand_key", postgresql_where=text("status = 'active'")),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    label: Mapped[str] = mapped_column(String(300))
    brand_key: Mapped[str] = mapped_column(String(200))  # nombre comercial normalizado
    laboratory: Mapped[str | None] = mapped_column(
        String(200)
    )  # como figura; no se deduce del fabricante
    gtin: Mapped[str | None] = mapped_column(String(14))
    sanitary_registration: Mapped[str | None] = mapped_column(String(30))
    form: Mapped[str | None] = mapped_column(String(40))
    content: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pharma_identity_id: Mapped[uuid.UUID | None] = (
        mapped_column()
    )  # NULL = principio activo aún desconocido
    source: Mapped[str] = mapped_column(String(10), server_default="manual")
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    created_by: Mapped[uuid.UUID] = mapped_column()
    # Procedencia del dato (NO es confianza ni confirmación farmacéutica):
    #   simulated = simulador/fixture/prueba · captured = recibido de una persona o dispositivo real ·
    #   ai_derived = resultado derivado por un modelo de IA · unclassified = solo organizaciones legacy.
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
