"""Comparaciones deterministas (C3, contrato C0 §3.4 y §E5) y criterios comerciales (E9).

Cinco tablas, todas del cotizador:

  * `comparison_run`             la corrida: alcance, estado global y criterios evaluados.
  * `comparison_line`            una línea pedida (artículo + cantidad en unidades comerciales).
  * `comparison_candidate`       un par (línea, oferta): elegibilidad, causas y costo de la línea.
  * `comparison_supplier_total`  el total por proveedor (o su cota inferior) y sus bloqueos.
  * `commercial_criterion`       notas y criterios confirmados; **solo anotan**, nunca calculan.

Las cuatro tablas de comparación son inmutables (`forbid_mutation()`): una comparación no se edita,
se repite. `commercial_criterion` sí cambia de estado y versión, porque una regla se confirma,
desactiva o edita con auditoría.

Un dato desconocido es `NULL`, nunca 0 ni 1 (D-015, D-081): si la cantidad o el pago no se pueden
determinar, la columna queda NULL y la causa tipada explica por qué.
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
from app.models.catalog import ORIGINS, UNITS

UUID_PK = dict(primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid())
_NOW = dict(server_default=func.now())


class ComparisonRun(Base):
    """Una comparación ejecutada. Determinista y de solo agregar: no se recalcula ni se edita."""

    __tablename__ = "comparison_run"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["source_search_id", "organization_id"],
            ["search_run.id", "search_run.organization_id"],
        ),
        CheckConstraint(
            "status IN ('complete','partial_coverage','no_complete_total','failed')",
            name="estado_valido",
        ),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint(
            "suppliers_evaluated >= 0 AND suppliers_complete >= 0 AND suppliers_incomplete >= 0",
            name="conteos_no_negativos",
        ),
        CheckConstraint(
            "suppliers_evaluated = suppliers_complete + suppliers_incomplete",
            name="conteos_coherentes",
        ),
        CheckConstraint("sources_consulted >= 0", name="fuentes_no_negativas"),
        CheckConstraint("jsonb_typeof(sources_failed) = 'array'", name="fuentes_fallidas_validas"),
        Index("ix_comparison_run_org_created", "organization_id", "created_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    # Pista opcional a una búsqueda reciente; no reutiliza ofertas ni omite validaciones.
    source_search_id: Mapped[uuid.UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(20))
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    criteria_evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    suppliers_evaluated: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    suppliers_complete: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    suppliers_incomplete: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    sources_consulted: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    sources_failed: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_by: Mapped[uuid.UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class ComparisonLine(Base):
    """Una línea pedida: artículo + cantidad entera en unidades comerciales. Sin precio propio."""

    __tablename__ = "comparison_line"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        # Clave para la FK compuesta del candidato: la línea y su corrida nunca se separan.
        UniqueConstraint(
            "id", "comparison_run_id", "organization_id", name="uq_comparison_line_id_run"
        ),
        UniqueConstraint("comparison_run_id", "position"),
        ForeignKeyConstraint(
            ["comparison_run_id", "organization_id"],
            ["comparison_run.id", "comparison_run.organization_id"],
        ),
        ForeignKeyConstraint(
            ["article_id", "organization_id"], ["trade_item.id", "trade_item.organization_id"]
        ),
        CheckConstraint("position >= 0", name="posicion_valida"),
        CheckConstraint("quantity_value > 0", name="cantidad_positiva"),
        CheckConstraint(f"quantity_unit IN ({UNITS})", name="unidad_valida"),
        CheckConstraint(
            "completeness IN ('complete','partial','unavailable')", name="completeness_valido"
        ),
        CheckConstraint("jsonb_typeof(blockers) = 'array'", name="bloqueos_validos"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    comparison_run_id: Mapped[uuid.UUID] = mapped_column()
    position: Mapped[int] = mapped_column()
    article_id: Mapped[uuid.UUID] = mapped_column()
    quantity_value: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    quantity_unit: Mapped[str] = mapped_column(String(10))
    completeness: Mapped[str] = mapped_column(String(12))
    blockers: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))


class ComparisonCandidate(Base):
    """Un par (línea, oferta). La elegibilidad es del candidato; el total es del proveedor.

    `line_payable` y las cantidades son `null` cuando no se pueden determinar (unidad que no
    coincide, base de precio irresoluble, impuestos desconocidos): esa línea no compone un total
    completo, pero el candidato **se devuelve igual** (AC18) con sus causas.
    """

    __tablename__ = "comparison_candidate"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(
            ["comparison_line_id", "comparison_run_id", "organization_id"],
            [
                "comparison_line.id",
                "comparison_line.comparison_run_id",
                "comparison_line.organization_id",
            ],
        ),
        ForeignKeyConstraint(
            ["offer_id", "organization_id"],
            ["offer_snapshot.id", "offer_snapshot.organization_id"],
        ),
        ForeignKeyConstraint(
            ["supplier_id", "organization_id"], ["supplier.id", "supplier.organization_id"]
        ),
        ForeignKeyConstraint(
            ["source_id", "organization_id"], ["source.id", "source.organization_id"]
        ),
        CheckConstraint(
            "eligibility IN ('eligible','needs_review','ineligible')", name="elegibilidad_valida"
        ),
        CheckConstraint("jsonb_typeof(causes) = 'array'", name="causas_validas"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="moneda_valida"),
        CheckConstraint(
            "(minimum_known) = (minimum_quantity IS NOT NULL)", name="minimo_coherente"
        ),
        CheckConstraint(
            "(multiple_known) = (multiple_quantity IS NOT NULL)", name="multiplo_coherente"
        ),
        CheckConstraint("quantity_requested > 0", name="cantidad_positiva"),
        CheckConstraint(
            "purchased IS NULL OR purchased >= quantity_requested", name="compra_no_menor"
        ),
        CheckConstraint("surplus IS NULL OR surplus >= 0", name="excedente_no_negativo"),
        CheckConstraint(
            "adjustment IS NULL OR adjustment IN "
            "('none','minimum','multiple','minimum_and_multiple')",
            name="ajuste_valido",
        ),
        CheckConstraint("line_gross IS NULL OR line_gross >= 0", name="bruto_no_negativo"),
        CheckConstraint("line_payable IS NULL OR line_payable >= 0", name="pago_no_negativo"),
        CheckConstraint("stock_status IN ('known','unknown')", name="estado_stock_valido"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        Index("ix_comparison_candidate_line", "comparison_line_id"),
        Index("ix_comparison_candidate_supplier", "comparison_run_id", "supplier_id"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    comparison_run_id: Mapped[uuid.UUID] = mapped_column()
    comparison_line_id: Mapped[uuid.UUID] = mapped_column()
    offer_id: Mapped[uuid.UUID] = mapped_column()
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID] = mapped_column()
    eligibility: Mapped[str] = mapped_column(String(12))
    causes: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    quantity_requested: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    minimum_known: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    minimum_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    multiple_known: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    multiple_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    base_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    purchased: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    surplus: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    adjustment: Mapped[str | None] = mapped_column(String(20))
    price_amount: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    # Copia la base del precio de la oferta evaluada; NULL = no informada (migración 0030).
    price_basis: Mapped[str | None] = mapped_column(String(24))
    tax_included: Mapped[bool | None] = mapped_column(Boolean)
    discount_included: Mapped[bool | None] = mapped_column(Boolean)
    pack_units_per_commercial_unit: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    line_gross: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    line_discounts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    line_discounts_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    line_tax_added: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    line_payable: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    line_net: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    discounted_is_possible: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    stock_status: Mapped[str] = mapped_column(String(10))
    stock_sufficient: Mapped[bool | None] = mapped_column(Boolean)
    shipping_terms_id: Mapped[uuid.UUID | None] = mapped_column()
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class ComparisonSupplierTotal(Base):
    """Total por proveedor. Sin `eligibility`: solo `completeness`, `blockers` y cota inferior.

    `total` existe **solo** con `completeness='complete'`; un proveedor parcial nunca tiene total.
    """

    __tablename__ = "comparison_supplier_total"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("comparison_run_id", "supplier_id"),
        ForeignKeyConstraint(
            ["comparison_run_id", "organization_id"],
            ["comparison_run.id", "comparison_run.organization_id"],
        ),
        ForeignKeyConstraint(
            ["supplier_id", "organization_id"], ["supplier.id", "supplier.organization_id"]
        ),
        CheckConstraint("completeness IN ('complete','partial')", name="completeness_valido"),
        CheckConstraint("bound_status IN ('valid','not_boundable')", name="cota_estado_valido"),
        CheckConstraint(
            "(completeness = 'complete') = (total IS NOT NULL)", name="total_solo_completo"
        ),
        CheckConstraint(
            "(bound_status = 'valid') = (lower_bound IS NOT NULL)", name="cota_coherente"
        ),
        CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="moneda_valida"),
        CheckConstraint("jsonb_typeof(blockers) = 'array'", name="bloqueos_validos"),
        CheckConstraint("jsonb_typeof(causes) = 'array'", name="causas_validas"),
        CheckConstraint(
            "jsonb_typeof(bound_invalid_reasons) = 'array'", name="motivos_cota_validos"
        ),
        CheckConstraint("jsonb_typeof(other_charges) = 'array'", name="cargos_validos"),
        Index("ix_comparison_supplier_total_run", "comparison_run_id"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    comparison_run_id: Mapped[uuid.UUID] = mapped_column()
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    completeness: Mapped[str] = mapped_column(String(10))
    currency: Mapped[str | None] = mapped_column(String(3))
    lines_covered: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    lines_missing: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    taxes_included: Mapped[bool | None] = mapped_column(Boolean)
    taxes_added: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    discounts_total: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    shipping_scheme: Mapped[str | None] = mapped_column(String(20))
    shipping_known: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    shipping_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    shipping_terms_id: Mapped[uuid.UUID | None] = mapped_column()
    other_charges: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    total: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    lower_bound: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    bound_status: Mapped[str] = mapped_column(String(16), server_default="not_boundable")
    bound_invalid_reasons: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    blockers: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    causes: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    freshness: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class CommercialCriterion(Base):
    """Nota (`note`) o criterio confirmado (`criterion`). Solo anota: nunca altera un número.

    `status` es `active` o `inactive`; «vigente» es la combinación de `active` con la ventana de
    `validity`, calculada al leer. No existe `proposed`: lo no confirmado es, por definición, nota.
    """

    __tablename__ = "commercial_criterion"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["source_comparison_id", "organization_id"],
            ["comparison_run.id", "comparison_run.organization_id"],
        ),
        ForeignKeyConstraint(
            ["target_supplier_id", "organization_id"],
            ["supplier.id", "supplier.organization_id"],
        ),
        ForeignKeyConstraint(
            ["target_article_id", "organization_id"],
            ["trade_item.id", "trade_item.organization_id"],
        ),
        ForeignKeyConstraint(
            ["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        ForeignKeyConstraint(
            ["confirmed_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        ForeignKeyConstraint(
            ["deactivated_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        CheckConstraint("kind IN ('note','criterion')", name="tipo_valido"),
        CheckConstraint("status IN ('active','inactive')", name="estado_valido"),
        CheckConstraint("origin IN ('explicit','inferred')", name="origen_valido"),
        CheckConstraint(
            "(kind = 'note' AND effect = 'note_only') OR "
            "(kind = 'criterion' AND effect IN "
            "('prefer','deprioritize','do_not_recommend','requires_review'))",
            name="efecto_del_tipo",
        ),
        CheckConstraint(
            "target_level IN ('organization','ingredient','article','supplier','supplier_article')",
            name="ambito_valido",
        ),
        CheckConstraint(
            "target_level <> 'ingredient' OR target_ingredient IS NOT NULL", name="ingrediente"
        ),
        CheckConstraint(
            "target_level <> 'article' OR target_article_id IS NOT NULL", name="articulo"
        ),
        CheckConstraint(
            "target_level <> 'supplier' OR target_supplier_id IS NOT NULL", name="proveedor"
        ),
        CheckConstraint(
            "target_level <> 'supplier_article' OR "
            "(target_supplier_id IS NOT NULL AND target_article_id IS NOT NULL)",
            name="proveedor_articulo",
        ),
        CheckConstraint(
            "condition_kind IN ('always','min_quantity','max_price','date_window')",
            name="condicion_valida",
        ),
        CheckConstraint(
            "condition_kind <> 'min_quantity' OR condition_min_quantity > 0",
            name="condicion_cantidad",
        ),
        CheckConstraint(
            "condition_kind <> 'max_price' OR "
            "(condition_max_price_amount IS NOT NULL AND condition_max_price_currency IS NOT NULL "
            "AND condition_max_price_basis IS NOT NULL)",
            name="condicion_precio",
        ),
        CheckConstraint(
            "condition_kind <> 'date_window' OR "
            "(condition_date_from IS NOT NULL OR condition_date_to IS NOT NULL)",
            name="condicion_fechas",
        ),
        # Un criterio exige confirmación explícita; una nota nunca la tiene.
        CheckConstraint(
            "kind <> 'criterion' OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="criterio_confirmado",
        ),
        CheckConstraint(
            "kind <> 'note' OR (confirmed_by IS NULL AND confirmed_at IS NULL)",
            name="nota_sin_confirmar",
        ),
        CheckConstraint(
            "status <> 'inactive' OR "
            "(deactivated_by IS NOT NULL AND deactivated_at IS NOT NULL "
            "AND deactivation_reason IS NOT NULL)",
            name="baja_con_motivo",
        ),
        CheckConstraint("version >= 1", name="version_valida"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint("length(btrim(reason_text)) > 0", name="motivo_no_vacio"),
        # Un criterio derivado por nota: la trazabilidad no se duplica ni se pierde.
        Index(
            "uq_criterion_from_note",
            "source_note_criterion_id",
            unique=True,
            postgresql_where=text("source_note_criterion_id IS NOT NULL"),
        ),
        Index("ix_criterion_org_status", "organization_id", "status", "kind"),
        Index("ix_criterion_target", "organization_id", "target_level", "target_article_id"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    kind: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10), server_default="active")
    origin: Mapped[str] = mapped_column(String(10), server_default="explicit")
    effect: Mapped[str] = mapped_column(String(20))
    target_level: Mapped[str] = mapped_column(String(20))
    target_ingredient: Mapped[str | None] = mapped_column(String(200))
    target_article_id: Mapped[uuid.UUID | None] = mapped_column()
    target_supplier_id: Mapped[uuid.UUID | None] = mapped_column()
    condition_kind: Mapped[str] = mapped_column(String(20), server_default="always")
    condition_min_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    condition_max_price_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    condition_max_price_currency: Mapped[str | None] = mapped_column(String(3))
    condition_max_price_basis: Mapped[str | None] = mapped_column(String(24))
    condition_date_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    condition_date_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_text: Mapped[str] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(12), server_default="manual")
    source_comparison_id: Mapped[uuid.UUID | None] = mapped_column()
    source_offer_id: Mapped[uuid.UUID | None] = mapped_column()
    source_note_criterion_id: Mapped[uuid.UUID | None] = mapped_column()
    source_note_version: Mapped[int | None] = mapped_column(Integer)
    validity_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validity_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_criterion_id: Mapped[uuid.UUID | None] = mapped_column()
    priority: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_by: Mapped[uuid.UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column()
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivated_by: Mapped[uuid.UUID | None] = mapped_column()
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivation_reason: Mapped[str | None] = mapped_column(Text)
