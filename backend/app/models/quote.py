"""Cotizaciones, carritos por proveedor, líneas y observaciones comerciales (C5-A, E10 y O1).

Cuatro tablas del cotizador:

  * `quote`                    la cotización: `draft` o `closed`, sin archivo ni TTL.
  * `quote_cart`               un carrito por proveedor; nace con la primera línea.
  * `quote_line`               la oferta elegida y la cantidad; **no guarda precio**.
  * `commercial_observation`   texto libre, inmutable y acumulativo (D-116/D-132).

Reglas que la base impone y no se pueden eludir desde el servicio:

  * una línea no puede pertenecer a un carrito de otro proveedor (FK compuesta);
  * una línea puede tener `demand_item_id` + `coverage_relation` o ninguno de los dos, nunca uno;
  * la cantidad es positiva y **entera** (E5 no compara fracciones de caja);
  * el precio **no** se copia: se lee del `offer_snapshot` referenciado (§3, V-03);
  * dos selecciones activas de la misma oferta en el mismo carrito son una sola línea;
  * una observación comercial no se edita ni se borra (gatillo `forbid_mutation()`).

La cobertura de faltantes es **derivada** y no vive aquí: se calcula leyendo estas tablas y las
compras vigentes (§12.3, V-21).
"""

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
from app.models.catalog import ORIGINS, UNITS

UUID_PK = dict(primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid())
_NOW = dict(server_default=func.now())

RELATIONS = "'exact','other_presentation','combined','related','needs_review'"


class Quote(Base):
    """Cotización general. Persistente, sin TTL ni archivo por política (§5.7, §7.2)."""

    __tablename__ = "quote"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        ForeignKeyConstraint(
            ["closed_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        CheckConstraint("status IN ('draft','closed')", name="estado_valido"),
        # Cerrada exige autor y fecha; abierta no los conserva (reabrir los limpia).
        CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL AND closed_by IS NOT NULL)",
            name="cierre_coherente",
        ),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint("version >= 1", name="version_valida"),
        CheckConstraint("title IS NULL OR length(btrim(title)) > 0", name="titulo_no_vacio"),
        CheckConstraint("title IS NULL OR length(title) <= 120", name="titulo_largo_valido"),
        Index("ix_quote_org_updated", "organization_id", "updated_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    # Texto libre («Reposición semanal»); nunca obligatorio para trabajar.
    title: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(10), server_default="draft")
    created_by: Mapped[uuid.UUID] = mapped_column()
    closed_by: Mapped[uuid.UUID | None] = mapped_column()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    version: Mapped[int] = mapped_column(Integer, server_default="1")


class QuoteCart(Base):
    """Un carrito por proveedor y cotización. Se crea al agregar la primera línea."""

    __tablename__ = "quote_cart"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_quote_cart_id"),
        # Clave compuesta para que `quote_line` no pueda quedar en un carrito de otro proveedor.
        UniqueConstraint(
            "id", "supplier_id", "organization_id", name="uq_quote_cart_supplier"
        ),
        # Un carrito por proveedor y cotización.
        UniqueConstraint("quote_id", "supplier_id", name="uq_quote_cart_quote_supplier"),
        ForeignKeyConstraint(
            ["quote_id", "organization_id"], ["quote.id", "quote.organization_id"]
        ),
        ForeignKeyConstraint(
            ["supplier_id", "organization_id"], ["supplier.id", "supplier.organization_id"]
        ),
        ForeignKeyConstraint(
            ["dismissed_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        CheckConstraint("status IN ('active','dismissed')", name="estado_valido"),
        # Descartado exige autor y fecha; activo no los conserva (reincorporar los limpia).
        CheckConstraint(
            "(status = 'dismissed') = "
            "(dismissed_at IS NOT NULL AND dismissed_by IS NOT NULL)",
            name="descarte_coherente",
        ),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint("version >= 1", name="version_valida"),
        Index("ix_quote_cart_quote", "organization_id", "quote_id"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    quote_id: Mapped[uuid.UUID] = mapped_column()
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_by: Mapped[uuid.UUID | None] = mapped_column()
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    version: Mapped[int] = mapped_column(Integer, server_default="1")


class QuoteLine(Base):
    """Producto seleccionado dentro de un carrito. Ancla de precio, nunca copia de precio."""

    __tablename__ = "quote_line"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(
            ["quote_cart_id", "supplier_id", "organization_id"],
            ["quote_cart.id", "quote_cart.supplier_id", "quote_cart.organization_id"],
        ),
        ForeignKeyConstraint(
            ["offer_snapshot_id", "organization_id"],
            ["offer_snapshot.id", "offer_snapshot.organization_id"],
        ),
        ForeignKeyConstraint(
            ["trade_item_id", "organization_id"], ["trade_item.id", "trade_item.organization_id"]
        ),
        # La línea y el faltante que ayuda a cubrir viven en la misma organización.
        ForeignKeyConstraint(
            ["demand_item_id", "organization_id"],
            ["demand_item.id", "demand_item.organization_id"],
        ),
        ForeignKeyConstraint(
            ["added_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        ForeignKeyConstraint(
            ["removed_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        ForeignKeyConstraint(
            ["pending_closed_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
        ),
        CheckConstraint("quantity > 0", name="cantidad_positiva"),
        # E5 exige entero positivo: una línea es una selección explícita, no una estimación.
        CheckConstraint("quantity = trunc(quantity)", name="cantidad_entera"),
        CheckConstraint(f"quantity_unit IN ({UNITS})", name="unidad_valida"),
        CheckConstraint("status IN ('active','removed')", name="estado_valido"),
        CheckConstraint(
            "(status = 'removed') = (removed_at IS NOT NULL AND removed_by IS NOT NULL)",
            name="retiro_coherente",
        ),
        # Vínculo con faltante y clasificación congelada: ambos o ninguno.
        CheckConstraint(
            "(demand_item_id IS NULL) = (coverage_relation IS NULL)", name="vinculo_coherente"
        ),
        CheckConstraint(
            f"coverage_relation IS NULL OR coverage_relation IN ({RELATIONS})",
            name="relacion_valida",
        ),
        # Cerrar el pendiente exige autor y fecha; la nota es opcional y solo con cierre.
        CheckConstraint(
            "(pending_closed_at IS NULL) = (pending_closed_by IS NULL)",
            name="pendiente_cierre_coherente",
        ),
        CheckConstraint(
            "pending_closed_note IS NULL OR pending_closed_at IS NOT NULL",
            name="nota_pendiente_con_cierre",
        ),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint("version >= 1", name="version_valida"),
        # Dos selecciones activas de la misma oferta en el mismo carrito son la misma línea.
        Index(
            "uq_quote_line_active_offer",
            "quote_cart_id",
            "offer_snapshot_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_quote_line_demand", "organization_id", "demand_item_id"),
        Index("ix_quote_line_cart", "quote_cart_id", "status"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    quote_cart_id: Mapped[uuid.UUID] = mapped_column()
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    # Ancla de precio y condiciones: el snapshot elegido, no «la oferta más nueva».
    offer_snapshot_id: Mapped[uuid.UUID] = mapped_column()
    # Copia denormalizada del artículo de la oferta; el servicio valida que coincida.
    trade_item_id: Mapped[uuid.UUID] = mapped_column()
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    quantity_unit: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_by: Mapped[uuid.UUID | None] = mapped_column()
    demand_item_id: Mapped[uuid.UUID | None] = mapped_column()
    # Clasificación congelada al agregar desde un faltante; no se recalcula ni se «mejora».
    coverage_relation: Mapped[str | None] = mapped_column(String(20))
    pending_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_closed_by: Mapped[uuid.UUID | None] = mapped_column()
    pending_closed_note: Mapped[str | None] = mapped_column(Text)
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    added_by: Mapped[uuid.UUID] = mapped_column()
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    version: Mapped[int] = mapped_column(Integer, server_default="1")


class CommercialObservation(Base):
    """Observación libre de una persona sobre un artículo. Inmutable y acumulativa (O1)."""

    __tablename__ = "commercial_observation"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["author_id", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        ForeignKeyConstraint(
            ["quote_id", "organization_id"], ["quote.id", "quote.organization_id"]
        ),
        ForeignKeyConstraint(
            ["quote_line_id", "organization_id"],
            ["quote_line.id", "quote_line.organization_id"],
        ),
        ForeignKeyConstraint(
            ["trade_item_id", "organization_id"], ["trade_item.id", "trade_item.organization_id"]
        ),
        ForeignKeyConstraint(
            ["offer_snapshot_id", "organization_id"],
            ["offer_snapshot.id", "offer_snapshot.organization_id"],
        ),
        ForeignKeyConstraint(
            ["supplier_id", "organization_id"], ["supplier.id", "supplier.organization_id"]
        ),
        # Texto libre, 1–2000 caracteres. Se guarda tal como se escribió (sin normalizar).
        CheckConstraint(
            "length(btrim(text_original)) > 0 AND length(text_original) <= 2000",
            name="texto_valido",
        ),
        CheckConstraint("jsonb_typeof(context) = 'object'", name="contexto_valido"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        Index(
            "ix_commercial_observation_item",
            "organization_id",
            "trade_item_id",
            "created_at",
        ),
        Index("ix_commercial_observation_line", "quote_line_id", "created_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    # El texto se guarda tal como lo escribió la persona: no se normaliza para mostrar.
    text_original: Mapped[str] = mapped_column(Text)
    author_id: Mapped[uuid.UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    quote_id: Mapped[uuid.UUID | None] = mapped_column()
    quote_line_id: Mapped[uuid.UUID | None] = mapped_column()
    trade_item_id: Mapped[uuid.UUID] = mapped_column()
    offer_snapshot_id: Mapped[uuid.UUID | None] = mapped_column()
    supplier_id: Mapped[uuid.UUID | None] = mapped_column()
    # Etiquetas tal como se vieron (producto, presentación, marca, proveedor, oferta, cotización).
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    data_origin: Mapped[str] = mapped_column(String(12), server_default="captured")
