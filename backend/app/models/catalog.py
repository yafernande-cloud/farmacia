"""Catálogo comercial del cotizador (C1, D-076, D-094 y adenda `chain_product`).

Cuatro planos que NO se mezclan:

  * `trade_item`         artículo canónico = presentación comercial (D-080). Lo fija la farmacia.
  * `chain_product`      ficha tal como la publica una cadena (`chain_reference`). La fija la cadena.
  * `reference_price`    observación fechada del precio público de una ficha de cadena. Nunca es oferta.
  * `offer_snapshot`     observación fechada de un SKU de un proveedor para un artículo. Es lo único
                         que puede entrar en una comparación y en un total.

El vínculo ficha de cadena → artículo es explícito o confirmado y nunca se infiere por principio
activo y dosis. No se modifican las tablas del módulo de faltantes (D-095, D-096).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
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

# Procedencia del dato: NO es confianza ni confirmación (D-015 a D-024, D-037 a D-047).
ORIGINS = "'simulated','captured','ai_derived','unclassified'"
# Unidad de venta/comercial admitida hoy (app/domain/quantities.py).
UNITS = "'box','package','unit','bottle'"
# Capacidades aceptadas en v0 (contrato C0 §3.2). Las transaccionales quedan fuera hasta H5.
CAPABILITIES = '["search","product_detail","price","stock","terms"]'


class TradeItem(Base):
    """Artículo canónico del catálogo: una presentación comercial (caja de 10 ≠ caja de 20).

    Neutral al tipo de producto (D-094): ningún atributo farmacéutico es obligatorio y
    `product_kind` es interno, nullable y nunca inferido. Los atributos estructurados viven en
    `attributes` (esquema v2, con la procedencia de cada uno); `ingredient` se duplica como columna
    solo para consultas y criterios futuros y puede quedar NULL.
    """

    __tablename__ = "trade_item"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        # Idempotencia de `catalog-load` (D-097): una sola fila por clave de identidad.
        UniqueConstraint("organization_id", "identity_key"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["pharma_identity_id", "organization_id"], ["pharma_identity.id", "pharma_identity.organization_id"]),
        ForeignKeyConstraint(["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        CheckConstraint("status IN ('active','inactive')", name="estado_valido"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint(
            "product_kind IS NULL OR product_kind IN ('medicine','medical_device','cosmetic',"
            "'personal_care','veterinary','food_supplement','other')",
            name="tipo_producto_valido",
        ),
        CheckConstraint(
            "jsonb_typeof(attributes) = 'object' AND attributes ? 'schema_version'",
            name="atributos_validos",
        ),
        CheckConstraint("gtin IS NULL OR gtin ~ '^[0-9]{8,14}$'", name="gtin_valido"),
        CheckConstraint("version >= 1", name="version_valida"),
        Index("ix_trade_item_org_status", "organization_id", "status"),
        Index("ix_trade_item_ingredient", "organization_id", "ingredient"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    display_name: Mapped[str] = mapped_column(String(300))
    identity_key: Mapped[str] = mapped_column(String(400))
    product_kind: Mapped[str | None] = mapped_column(String(20))
    ingredient: Mapped[str | None] = mapped_column(String(200))
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("""'{"schema_version": 2}'::jsonb""")
    )
    laboratory: Mapped[str | None] = mapped_column(String(200))
    brand: Mapped[str | None] = mapped_column(String(200))
    gtin: Mapped[str | None] = mapped_column(String(14))
    sanitary_registration: Mapped[str | None] = mapped_column(String(30))
    pharma_identity_id: Mapped[uuid.UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class TradeItemAlias(Base):
    """Nombre alternativo de un artículo, con el texto tal como se escribió.

    Un mismo alias puede apuntar a dos artículos distintos (ambigüedad legítima): la unicidad es
    por artículo, no global, de modo que la ambigüedad se resuelve con candidatos y no se oculta.
    """

    __tablename__ = "trade_item_alias"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("organization_id", "trade_item_id", "normalized_alias"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["trade_item_id", "organization_id"], ["trade_item.id", "trade_item.organization_id"]),
        ForeignKeyConstraint(["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        CheckConstraint("status IN ('active','inactive')", name="estado_valido"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint("source IN ('manual','supplier','import','chain')", name="origen_valido"),
        Index("ix_trade_item_alias_lookup", "organization_id", "normalized_alias"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    trade_item_id: Mapped[uuid.UUID] = mapped_column()
    alias_text: Mapped[str] = mapped_column(String(300))
    normalized_alias: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(10), server_default="manual")
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class Supplier(Base):
    """A quién se le compra. Sus condiciones comerciales viven en ofertas y despacho, no aquí."""

    __tablename__ = "supplier"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("organization_id", "name"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        CheckConstraint("status IN ('active','inactive')", name="estado_valido"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint("version >= 1", name="version_valida"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    name: Mapped[str] = mapped_column(String(200))
    supplier_code: Mapped[str | None] = mapped_column(String(60))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class Source(Base):
    """Conector concreto: lista de un proveedor o catálogo público de una cadena.

    `kind` separa los dos mundos: `supplier` pertenece a un proveedor y puede producir ofertas;
    `chain_reference` es una cadena y solo produce fichas y precios públicos. `status` no se guarda:
    se deriva (§E4) de `last_success_at`, la frescura y la configuración.
    """

    __tablename__ = "source"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        # Clave auxiliar: una oferta o un mapping no pueden declarar un proveedor distinto del de su fuente.
        UniqueConstraint("id", "supplier_id", "organization_id", name="uq_source_id_supplier"),
        UniqueConstraint("organization_id", "name"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["supplier_id", "organization_id"], ["supplier.id", "supplier.organization_id"]),
        ForeignKeyConstraint(["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        CheckConstraint("kind IN ('supplier','chain_reference')", name="tipo_valido"),
        CheckConstraint("mode IN ('api','manual_import','fixture','none')", name="modo_valido"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint("(kind = 'supplier') = (supplier_id IS NOT NULL)", name="proveedor_coherente"),
        CheckConstraint("jsonb_typeof(capabilities) = 'array'", name="capacidades_validas"),
        # En v0 solo se aceptan las cinco capacidades de lectura (§3.2).
        CheckConstraint(
            f"capabilities <@ '{CAPABILITIES}'::jsonb", name="capacidades_permitidas"
        ),
        CheckConstraint("version >= 1", name="version_valida"),
        Index("ix_source_kind", "organization_id", "kind"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    kind: Mapped[str] = mapped_column(String(16))
    supplier_id: Mapped[uuid.UUID | None] = mapped_column()
    name: Mapped[str] = mapped_column(String(200))
    mode: Mapped[str] = mapped_column(String(16), server_default="none")
    capabilities: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(300))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class SupplierProductMapping(Base):
    """Vínculo verificado entre un SKU de un proveedor y un artículo canónico.

    Es la ÚNICA vía por la que un producto externo se convierte en el artículo que se compara.
    Nace explícito (carga o alta manual) o se confirma; nunca de una inferencia.
    """

    __tablename__ = "supplier_product_mapping"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("organization_id", "source_id", "external_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["source_id", "supplier_id", "organization_id"], ["source.id", "source.supplier_id", "source.organization_id"]),
        ForeignKeyConstraint(["trade_item_id", "organization_id"], ["trade_item.id", "trade_item.organization_id"]),
        ForeignKeyConstraint(["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        ForeignKeyConstraint(["confirmed_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        CheckConstraint("origin IN ('explicit','confirmed')", name="origen_valido"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint("(origin = 'confirmed') = (confirmed_by IS NOT NULL)", name="confirmacion_autor_valida"),
        CheckConstraint("(confirmed_by IS NULL) = (confirmed_at IS NULL)", name="confirmacion_fecha_valida"),
        CheckConstraint("version >= 1", name="version_valida"),
        Index("ix_supplier_product_mapping_item", "organization_id", "trade_item_id"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID] = mapped_column()
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    trade_item_id: Mapped[uuid.UUID] = mapped_column()
    external_id: Mapped[str] = mapped_column(String(120))
    external_name: Mapped[str | None] = mapped_column(String(300))
    origin: Mapped[str] = mapped_column(String(12), server_default="explicit")
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column()
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class ShippingTermsSnapshot(Base):
    """Condiciones de despacho tipadas de un proveedor, observadas en una fecha.

    Solo la estructura alimenta el cálculo (C3); el texto que informa el proveedor no se interpreta.
    """

    __tablename__ = "shipping_terms_snapshot"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["supplier_id", "organization_id"], ["supplier.id", "supplier.organization_id"]),
        ForeignKeyConstraint(["source_id", "supplier_id", "organization_id"], ["source.id", "source.supplier_id", "source.organization_id"]),
        CheckConstraint("scheme IN ('free','flat','free_over_threshold')", name="esquema_valido"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="moneda_valida"),
        CheckConstraint("flat_amount IS NULL OR flat_amount >= 0", name="monto_no_negativo"),
        CheckConstraint("threshold_amount IS NULL OR threshold_amount >= 0", name="umbral_no_negativo"),
        CheckConstraint(
            "scheme <> 'free' OR (flat_amount IS NULL AND threshold_amount IS NULL)",
            name="gratis_sin_montos",
        ),
        CheckConstraint("scheme <> 'flat' OR flat_amount IS NOT NULL", name="fijo_con_monto"),
        CheckConstraint(
            "scheme <> 'free_over_threshold' OR (flat_amount IS NOT NULL AND threshold_amount IS NOT NULL)",
            name="umbral_con_monto",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from",
            name="vigencia_valida",
        ),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        Index("ix_shipping_terms_supplier", "organization_id", "supplier_id", "observed_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID | None] = mapped_column()
    scheme: Mapped[str] = mapped_column(String(20))
    flat_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    threshold_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class OfferSnapshot(Base):
    """Observación fechada de un SKU de proveedor para un artículo. Inmutable y la única base de
    comparaciones y totales.

    `tax_included` y `discount_included` son tri-estado: NULL = desconocido (no false).
    `minimum`/`multiple` usan `*_known`; si son desconocidos, la cantidad es NULL, nunca 0 ni 1.
    """

    __tablename__ = "offer_snapshot"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["supplier_id", "organization_id"], ["supplier.id", "supplier.organization_id"]),
        ForeignKeyConstraint(["source_id", "supplier_id", "organization_id"], ["source.id", "source.supplier_id", "source.organization_id"]),
        ForeignKeyConstraint(["trade_item_id", "organization_id"], ["trade_item.id", "trade_item.organization_id"]),
        ForeignKeyConstraint(["shipping_terms_id", "organization_id"], ["shipping_terms_snapshot.id", "shipping_terms_snapshot.organization_id"]),
        CheckConstraint("external_id_kind IN ('supplier_sku')", name="tipo_externo_valido"),
        CheckConstraint("price_basis IN ('per_commercial_unit','per_base_unit')", name="base_precio_valida"),
        CheckConstraint("price_amount >= 0", name="precio_no_negativo"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="moneda_valida"),
        CheckConstraint(f"sale_unit IN ({UNITS})", name="unidad_venta_valida"),
        CheckConstraint("minimum_unit IS NULL OR minimum_unit IN ('box','package','unit','bottle')", name="unidad_minimo_valida"),
        CheckConstraint("multiple_unit IS NULL OR multiple_unit IN ('box','package','unit','bottle')", name="unidad_multiplo_valida"),
        CheckConstraint("stock_unit IS NULL OR stock_unit IN ('box','package','unit','bottle')", name="unidad_stock_valida"),
        CheckConstraint("stock_status IN ('known','unknown')", name="estado_stock_valido"),
        # Conocido ⇔ cantidad presente; desconocido nunca se rellena con 0 ni 1.
        CheckConstraint("(stock_status = 'known') = (stock_quantity IS NOT NULL)", name="stock_coherente"),
        CheckConstraint("(stock_quantity IS NULL) = (stock_unit IS NULL)", name="stock_con_unidad"),
        CheckConstraint("(minimum_known) = (minimum_quantity IS NOT NULL)", name="minimo_coherente"),
        CheckConstraint("(minimum_quantity IS NULL) = (minimum_unit IS NULL)", name="minimo_con_unidad"),
        CheckConstraint("minimum_quantity IS NULL OR minimum_quantity >= 0", name="minimo_no_negativo"),
        CheckConstraint("(multiple_known) = (multiple_quantity IS NOT NULL)", name="multiplo_coherente"),
        CheckConstraint("(multiple_quantity IS NULL) = (multiple_unit IS NULL)", name="multiplo_con_unidad"),
        CheckConstraint("multiple_quantity IS NULL OR multiple_quantity >= 1", name="multiplo_positivo"),
        CheckConstraint(
            "(pack_units_per_commercial_unit IS NULL) = (pack_base_unit IS NULL)",
            name="pack_coherente",
        ),
        CheckConstraint(
            "pack_units_per_commercial_unit IS NULL OR pack_units_per_commercial_unit > 0",
            name="pack_positivo",
        ),
        CheckConstraint("jsonb_typeof(taxes) = 'array'", name="impuestos_validos"),
        CheckConstraint("jsonb_typeof(discounts) = 'array'", name="descuentos_validos"),
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from",
            name="vigencia_valida",
        ),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        Index("ix_offer_snapshot_item", "organization_id", "trade_item_id", "observed_at"),
        Index("ix_offer_snapshot_supplier", "organization_id", "supplier_id", "observed_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID] = mapped_column()
    trade_item_id: Mapped[uuid.UUID] = mapped_column()
    external_id: Mapped[str] = mapped_column(String(120))
    external_id_kind: Mapped[str] = mapped_column(String(20), server_default="supplier_sku")
    external_name: Mapped[str | None] = mapped_column(String(300))
    price_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    price_basis: Mapped[str] = mapped_column(String(24), server_default="per_commercial_unit")
    tax_included: Mapped[bool | None] = mapped_column(Boolean)
    discount_included: Mapped[bool | None] = mapped_column(Boolean)
    sale_unit: Mapped[str] = mapped_column(String(10))
    sale_unit_text: Mapped[str | None] = mapped_column(String(40))
    pack_units_per_commercial_unit: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    pack_base_unit: Mapped[str | None] = mapped_column(String(10))
    pack_text: Mapped[str | None] = mapped_column(String(60))
    minimum_known: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    minimum_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    minimum_unit: Mapped[str | None] = mapped_column(String(10))
    multiple_known: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    multiple_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    multiple_unit: Mapped[str | None] = mapped_column(String(10))
    stock_status: Mapped[str] = mapped_column(String(10), server_default="unknown")
    stock_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    stock_unit: Mapped[str | None] = mapped_column(String(10))
    taxes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    discounts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    shipping_terms_id: Mapped[uuid.UUID | None] = mapped_column()
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class ChainProduct(Base):
    """Ficha publicada por una cadena: nombre, descripción, foto y presentación, tal como se publican.

    Es un snapshot por contenido: si la cadena cambia la ficha, nace otra fila con otro `content_hash`
    y los precios anteriores siguen apuntando a la versión observada. Solo las columnas del vínculo
    (`trade_item_id`, `match_status`, `mapping_origin`, `mapped_by`, `mapped_at`, `version`) se pueden
    modificar, y únicamente por acción explícita auditada (trigger de la migración 0008).
    """

    __tablename__ = "chain_product"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        # Clave auxiliar: un precio de una cadena no puede apuntar a la ficha de otra.
        UniqueConstraint("id", "source_id", "organization_id", name="uq_chain_product_id_source"),
        # Una fila por contenido publicado: reingerir la misma ficha no duplica.
        UniqueConstraint("organization_id", "source_id", "external_id", "content_hash"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["source_id", "organization_id"], ["source.id", "source.organization_id"]),
        ForeignKeyConstraint(["trade_item_id", "organization_id"], ["trade_item.id", "trade_item.organization_id"]),
        ForeignKeyConstraint(["mapped_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        CheckConstraint("match_status IN ('exact','needs_review','unmapped')", name="estado_match_valido"),
        CheckConstraint(
            "mapping_origin IS NULL OR mapping_origin IN ('explicit','confirmed')",
            name="origen_vinculo_valido",
        ),
        # Un vínculo existe si y solo si hay artículo; sin vínculo no hay procedencia.
        CheckConstraint("(trade_item_id IS NULL) = (mapping_origin IS NULL)", name="vinculo_coherente"),
        CheckConstraint("(mapping_origin IS NULL) = (mapped_by IS NULL)", name="vinculo_autor_valido"),
        CheckConstraint("(mapping_origin IS NULL) = (mapped_at IS NULL)", name="vinculo_fecha_valida"),
        CheckConstraint(
            "mapping_origin <> 'confirmed' OR (mapped_by IS NOT NULL AND mapped_at IS NOT NULL)",
            name="confirmacion_completa",
        ),
        # Sin vínculo no hay equivalencia: 'exact' exige artículo y 'unmapped' lo prohíbe.
        CheckConstraint("(match_status = 'exact') = (trade_item_id IS NOT NULL)", name="match_coherente"),
        CheckConstraint("jsonb_typeof(published_attributes) = 'object'", name="atributos_validos"),
        CheckConstraint("jsonb_typeof(match_candidates) = 'array'", name="candidatos_validos"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="hash_contenido_valido"),
        CheckConstraint("image_hash IS NULL OR image_hash ~ '^[0-9a-f]{64}$'", name="hash_imagen_valido"),
        CheckConstraint("image_url IS NULL OR image_url ~ '^https?://'", name="url_imagen_valida"),
        CheckConstraint("source_url ~ '^https?://'", name="url_ficha_valida"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint("version >= 1", name="version_valida"),
        Index("ix_chain_product_external", "organization_id", "source_id", "external_id", "observed_at"),
        Index(
            "ix_chain_product_item",
            "organization_id",
            "trade_item_id",
            postgresql_where=text("trade_item_id IS NOT NULL"),
        ),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID] = mapped_column()
    external_id: Mapped[str] = mapped_column(String(120))
    source_url: Mapped[str] = mapped_column(Text)
    published_name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    laboratory_as_published: Mapped[str | None] = mapped_column(String(200))
    presentation_as_published: Mapped[str | None] = mapped_column(String(300))
    published_attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("""'{"schema_version": 2}'::jsonb""")
    )
    image_url: Mapped[str | None] = mapped_column(Text)
    image_hash: Mapped[str | None] = mapped_column(String(64))
    image_object_key: Mapped[str | None] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64))
    trade_item_id: Mapped[uuid.UUID | None] = mapped_column()
    match_status: Mapped[str] = mapped_column(String(16), server_default="unmapped")
    match_candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    mapping_origin: Mapped[str | None] = mapped_column(String(12))
    mapped_by: Mapped[uuid.UUID | None] = mapped_column()
    mapped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class ReferencePrice(Base):
    """Precio público observado de una ficha de cadena. NUNCA es oferta ni entra en totales.

    `chain_product_id` es obligatorio: el precio siempre corresponde a una ficha publicada concreta,
    así la foto, la descripción y el precio no pueden desincronizarse. No existe `trade_item_id` aquí:
    el vínculo con el artículo vive solo en `chain_product`.
    """

    __tablename__ = "reference_price"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["source_id", "organization_id"], ["source.id", "source.organization_id"]),
        # La ficha referenciada debe ser de la MISMA cadena (source_id va en la FK).
        ForeignKeyConstraint(["chain_product_id", "source_id", "organization_id"], ["chain_product.id", "chain_product.source_id", "chain_product.organization_id"]),
        CheckConstraint("price_normal >= 0", name="precio_no_negativo"),
        CheckConstraint("price_promo IS NULL OR price_promo >= 0", name="promocion_no_negativa"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="moneda_valida"),
        CheckConstraint("jsonb_typeof(conditions) = 'array'", name="condiciones_validas"),
        CheckConstraint("source_url ~ '^https?://'", name="url_origen_valida"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        Index("ix_reference_price_product", "organization_id", "chain_product_id", "observed_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID] = mapped_column()
    chain_product_id: Mapped[uuid.UUID] = mapped_column()
    price_normal: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    price_promo: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    conditions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    shipping_reported: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    stock_reported: Mapped[bool | None] = mapped_column(Boolean)
    source_url: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class ImportProfile(Base):
    """Perfil de lectura de las planillas de una fuente (C2, D-102).

    Configura el mapeo columna→papel, el vocabulario de unidades del archivo y las opciones de
    formato. Se reutiliza en cada importación de la misma fuente y se edita con `version`; como la
    importación guarda su propia instantánea (`ImportRun.profile_snapshot`), cambiar el perfil nunca
    reescribe el pasado. Un proveedor con dos formatos se modela como dos fuentes, así que hay un
    perfil por fuente.
    """

    __tablename__ = "import_profile"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("organization_id", "source_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["source_id", "organization_id"], ["source.id", "source.organization_id"]),
        ForeignKeyConstraint(["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        # En v0 solo se leen planillas .xlsx; CSV exige una migración y su propia prueba.
        CheckConstraint("file_format IN ('xlsx')", name="formato_valido"),
        CheckConstraint("header_row >= 1", name="fila_cabecera_valida"),
        CheckConstraint("jsonb_typeof(columns) = 'object'", name="columnas_objeto"),
        CheckConstraint("jsonb_typeof(units) = 'object'", name="unidades_objeto"),
        CheckConstraint("jsonb_typeof(options) = 'object'", name="opciones_objeto"),
        CheckConstraint("status IN ('active','inactive')", name="estado_valido"),
        CheckConstraint("version >= 1", name="version_valida"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID] = mapped_column()
    file_format: Mapped[str] = mapped_column(String(10), server_default="xlsx")
    sheet: Mapped[str | None] = mapped_column(String(120))
    header_row: Mapped[int] = mapped_column(Integer, server_default="1")
    columns: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    units: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class ImportRun(Base):
    """Ejecución de una importación de lista de precios (E8, C2). Se inserta una sola vez con su
    estado final; una reimportación del mismo archivo no crea otra fila.

    `profile_snapshot` conserva el mapeo exacto con que se leyó el archivo y `rejected_rows` las
    filas rechazadas con lo mínimo para identificarlas y revisarlas después (D-104).
    """

    __tablename__ = "import_run"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("organization_id", "source_id", "file_hash"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(["source_id", "organization_id"], ["source.id", "source.organization_id"]),
        ForeignKeyConstraint(["profile_id", "organization_id"], ["import_profile.id", "import_profile.organization_id"]),
        ForeignKeyConstraint(["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]),
        CheckConstraint("status IN ('completed','completed_with_errors','failed')", name="estado_valido"),
        CheckConstraint("file_hash ~ '^[0-9a-f]{64}$'", name="hash_archivo_valido"),
        CheckConstraint(
            "rows_total >= 0 AND rows_accepted >= 0 AND rows_rejected >= 0",
            name="conteos_no_negativos",
        ),
        CheckConstraint(
            "rows_accepted + rows_rejected <= rows_total", name="conteos_coherentes"
        ),
        CheckConstraint("jsonb_typeof(errors) = 'array'", name="errores_validos"),
        CheckConstraint("jsonb_typeof(profile_snapshot) = 'object'", name="perfil_snapshot_valido"),
        CheckConstraint("jsonb_typeof(rejected_rows) = 'array'", name="rechazos_validos"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        Index("ix_import_run_source", "organization_id", "source_id", "imported_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID] = mapped_column()
    profile_id: Mapped[uuid.UUID | None] = mapped_column()
    file_name: Mapped[str] = mapped_column(String(300))
    file_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24))
    rows_total: Mapped[int] = mapped_column(Integer, server_default="0")
    rows_accepted: Mapped[int] = mapped_column(Integer, server_default="0")
    rows_rejected: Mapped[int] = mapped_column(Integer, server_default="0")
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    rejected_rows: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_by: Mapped[uuid.UUID | None] = mapped_column()
