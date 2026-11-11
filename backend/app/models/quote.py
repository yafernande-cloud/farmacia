"""Cotizaciones, carritos por proveedor, líneas y observaciones comerciales (C5-A, E10 y O1).

Cuatro tablas del cotizador más la asignación de líneas a reportes:

  * `quote`                    la cotización: `draft` o `closed`, sin archivo ni TTL.
  * `quote_cart`               un carrito por proveedor; nace con la primera línea.
  * `quote_line`               la oferta elegida y la cantidad; **no guarda precio**.
  * `quote_line_allocation`    qué parte de la línea cubre cada reporte independiente (D-135).
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

# Clases que una interpretación puede retirar explícitamente (D-144). El precio distingue
# `acceptable` de `maximum`; retirar una no retira la otra.
RETIRED_SUBJECT_KINDS = (
    "laboratory",
    "brand",
    "supplier",
    "availability",
    "price:acceptable",
    "price:maximum",
)
RETIRED_SUBJECT_KINDS_JSON = "[" + ",".join(f'"{kind}"' for kind in RETIRED_SUBJECT_KINDS) + "]"

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


class QuoteLineAllocation(Base):
    """Parte de una línea comercial que cubre un reporte concreto (Modelo A, D-135).

    Una línea de 20 puede cubrir dos reportes independientes de 10 mediante dos asignaciones
    explícitas. La suma de asignaciones **nunca** supera la cantidad de la línea; la unidad es la de
    la línea. La relación E1 se congela por asignación porque puede diferir entre reportes.
    """

    __tablename__ = "quote_line_allocation"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint(
            "quote_line_id", "demand_item_id", name="uq_quote_line_allocation_line_demand"
        ),
        ForeignKeyConstraint(
            ["quote_line_id", "organization_id"],
            ["quote_line.id", "quote_line.organization_id"],
        ),
        ForeignKeyConstraint(
            ["demand_item_id", "organization_id"],
            ["demand_item.id", "demand_item.organization_id"],
        ),
        ForeignKeyConstraint(
            ["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        CheckConstraint("quantity > 0", name="cantidad_positiva"),
        CheckConstraint("quantity = trunc(quantity)", name="cantidad_entera"),
        CheckConstraint(f"coverage_relation IN ({RELATIONS})", name="relacion_valida"),
        Index("ix_quote_line_allocation_demand", "organization_id", "demand_item_id"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    quote_line_id: Mapped[uuid.UUID] = mapped_column()
    demand_item_id: Mapped[uuid.UUID] = mapped_column()
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    # Clasificación E1 congelada para ESTE reporte; no se recalcula ni se comparte con otros.
    coverage_relation: Mapped[str] = mapped_column(String(20))
    created_by: Mapped[uuid.UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class CommercialObservation(Base):
    """Observación libre de una persona sobre un artículo. Inmutable y acumulativa (O1)."""

    __tablename__ = "commercial_observation"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        # Alcance compuesto que las interpretaciones y preferencias heredan por FK.
        UniqueConstraint(
            "id", "trade_item_id", "organization_id", name="uq_commercial_observation_scope"
        ),
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


class ObservationInterpretation(Base):
    """Intento de interpretación de una observación comercial (O2–O4, D-136/D-138/D-141).

    Una fila por llamada al modelo; el texto original nunca se reescribe. `suggested` y `confirmed`
    pueden aplicarse; `rejected`, `failed`, `needs_review` y `no_preference` no. Un fallo se reintenta
    creando otra fila, nunca editando la anterior. El contenido es inmutable: solo se revisan el
    estado y sus datos de revisión. `retired_subject_kinds` expresa un retiro explícito por clase
    (`laboratory`, `brand`, `supplier`, `availability`, `price:acceptable`, `price:maximum`); no se
    simula con filas ficticias ni con `status='rejected'` (D-144).
    """

    __tablename__ = "observation_interpretation"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        # Alcance compuesto: una preferencia no puede cruzar artículo u organización.
        UniqueConstraint(
            "id", "trade_item_id", "organization_id", name="uq_observation_interpretation_scope"
        ),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["observation_id", "trade_item_id", "organization_id"],
            [
                "commercial_observation.id",
                "commercial_observation.trade_item_id",
                "commercial_observation.organization_id",
            ],
        ),
        ForeignKeyConstraint(
            ["trade_item_id", "organization_id"], ["trade_item.id", "trade_item.organization_id"]
        ),
        ForeignKeyConstraint(
            ["reviewed_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        CheckConstraint(
            "status IN ('suggested','confirmed','rejected','failed','needs_review','no_preference')",
            name="estado_valido",
        ),
        CheckConstraint("length(btrim(model)) > 0", name="modelo_no_vacio"),
        CheckConstraint("length(btrim(prompt_version)) > 0", name="prompt_no_vacio"),
        CheckConstraint("input_tokens IS NULL OR input_tokens >= 0", name="tokens_entrada_validos"),
        CheckConstraint("output_tokens IS NULL OR output_tokens >= 0", name="tokens_salida_validos"),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latencia_valida"),
        CheckConstraint("raw_output IS NULL OR jsonb_typeof(raw_output) = 'object'", name="salida_valida"),
        # Un fallo siempre tiene código interno; el código solo acompaña fallos o revisión.
        CheckConstraint("(status <> 'failed') OR (error_code IS NOT NULL)", name="fallo_con_codigo"),
        CheckConstraint(
            "(error_code IS NULL) OR (status IN ('failed','needs_review'))", name="codigo_solo_fallo"
        ),
        CheckConstraint(
            "(reviewed_by IS NULL) = (reviewed_at IS NULL)", name="revision_coherente"
        ),
        CheckConstraint(
            "review_note IS NULL OR reviewed_at IS NOT NULL", name="nota_con_revision"
        ),
        # Confirmar o rechazar exige una revisión humana; lo demás no la conserva.
        CheckConstraint(
            "(status IN ('confirmed','rejected')) = (reviewed_at IS NOT NULL)",
            name="revision_obligatoria",
        ),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint(
            "jsonb_typeof(retired_subject_kinds) = 'array'", name="retiros_validos"
        ),
        CheckConstraint(
            f"retired_subject_kinds <@ '{RETIRED_SUBJECT_KINDS_JSON}'::jsonb",
            name="retiros_permitidos",
        ),
        Index("ix_observation_interpretation_item", "organization_id", "trade_item_id", "created_at"),
        Index("ix_observation_interpretation_observation", "observation_id", "created_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    observation_id: Mapped[uuid.UUID] = mapped_column()
    trade_item_id: Mapped[uuid.UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(16), server_default="suggested")
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40))
    # Retiro explícito por clase. Solo tiene efecto si la interpretación está `suggested` o
    # `confirmed`; el resto de estados no modifica preferencias vigentes.
    retired_subject_kinds: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    # Respuesta estructurada del proveedor, tal como llegó; nunca se ejecuta ni se muestra.
    # `none_as_null` distingue «sin respuesta» (SQL NULL) de un JSON `null`.
    raw_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(40))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column()
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    # Aclaración excepcional para una referencia ambigua (A6 Fase 4). Se guarda el intento tal
    # como quedó (`needs_review`) y NO se aplica ninguna preferencia hasta que se resuelva.
    clarification: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    data_origin: Mapped[str] = mapped_column(String(12), server_default="ai_derived")


class CommercialPreference(Base):
    """Preferencia comercial estructurada de un artículo concreto (O3/O4, D-136/D-138).

    No es texto libre ni una puntuación. El motor determinista la usa para **ordenar** las ofertas
    del producto; nunca cambia precios, stock, elegibilidad ni totales. `laboratory`/`brand`/
    `supplier` aceptan una tolerancia (porcentaje relativo o monto absoluto) y una preferencia
    negativa solo se expresa como `deprioritize`. `price` usa una referencia aceptable o máxima;
    `availability` solo prefiere `in_stock`.
    """

    __tablename__ = "commercial_preference"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        # La preferencia hereda artículo y organización de su interpretación (FK compuesta).
        ForeignKeyConstraint(
            ["interpretation_id", "trade_item_id", "organization_id"],
            [
                "observation_interpretation.id",
                "observation_interpretation.trade_item_id",
                "observation_interpretation.organization_id",
            ],
        ),
        ForeignKeyConstraint(
            ["observation_id", "trade_item_id", "organization_id"],
            [
                "commercial_observation.id",
                "commercial_observation.trade_item_id",
                "commercial_observation.organization_id",
            ],
        ),
        ForeignKeyConstraint(
            ["trade_item_id", "organization_id"], ["trade_item.id", "trade_item.organization_id"]
        ),
        ForeignKeyConstraint(
            ["subject_supplier_id", "organization_id"],
            ["supplier.id", "supplier.organization_id"],
        ),
        CheckConstraint(
            "subject_kind IN ('laboratory','brand','supplier','availability','price')",
            name="sujeto_valido",
        ),
        CheckConstraint("effect IN ('prefer','deprioritize')", name="efecto_valido"),
        CheckConstraint(
            "status IN ('suggested','confirmed','rejected','needs_review')", name="estado_valido"
        ),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint(
            "length(btrim(subject_key)) > 0 AND length(subject_key) <= 300",
            name="clave_sujeto_valida",
        ),
        CheckConstraint(
            "length(btrim(subject_label)) > 0 AND length(subject_label) <= 300",
            name="etiqueta_sujeto_valida",
        ),
        CheckConstraint(
            "(subject_kind = 'supplier') = (subject_supplier_id IS NOT NULL)",
            name="proveedor_coherente",
        ),
        CheckConstraint(
            "subject_kind <> 'availability' OR (effect = 'prefer' AND subject_key = 'in_stock')",
            name="disponibilidad_valida",
        ),
        CheckConstraint("subject_kind <> 'price' OR effect = 'prefer'", name="precio_solo_prefiere"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confianza_valida"),
        # Tolerancias: solo tienen sentido al preferir un laboratorio, marca o proveedor.
        CheckConstraint(
            "tolerance_kind IN ('none','relative_percent','absolute_amount')",
            name="tolerancia_valida",
        ),
        CheckConstraint(
            "tolerance_origin IN ('not_applicable','implicit_default','explicit')",
            name="origen_tolerancia_valido",
        ),
        CheckConstraint(
            "(tolerance_kind = 'none') OR (subject_kind IN ('laboratory','brand','supplier')"
            " AND effect = 'prefer')",
            name="tolerancia_solo_preferencia",
        ),
        CheckConstraint(
            "(tolerance_kind <> 'none') = (tolerance_origin <> 'not_applicable')",
            name="origen_tolerancia_coherente",
        ),
        CheckConstraint(
            "tolerance_kind <> 'relative_percent' OR (tolerance_percent > 0"
            " AND tolerance_percent <= 100 AND tolerance_amount IS NULL"
            " AND tolerance_currency IS NULL)",
            name="tolerancia_relativa_valida",
        ),
        CheckConstraint(
            "tolerance_kind <> 'absolute_amount' OR (tolerance_amount > 0"
            " AND tolerance_currency IS NOT NULL AND tolerance_percent IS NULL"
            " AND tolerance_origin = 'explicit')",
            name="tolerancia_absoluta_valida",
        ),
        CheckConstraint(
            "(tolerance_kind = 'none') = (tolerance_percent IS NULL AND tolerance_amount IS NULL"
            " AND tolerance_currency IS NULL)",
            name="tolerancia_nula_coherente",
        ),
        CheckConstraint(
            "tolerance_currency IS NULL OR tolerance_currency ~ '^[A-Z]{3}$'",
            name="moneda_tolerancia_valida",
        ),
        # Referencias de precio: aceptable o máximo, con su base explícita.
        CheckConstraint(
            "price_reference_kind IN ('none','acceptable','maximum')",
            name="referencia_precio_valida",
        ),
        CheckConstraint(
            "price_reference_basis IS NULL OR price_reference_basis IN"
            " ('per_base_unit','per_commercial_unit','per_line_total')",
            name="base_precio_valida",
        ),
        CheckConstraint(
            "(subject_kind = 'price') = (price_reference_kind <> 'none')",
            name="referencia_solo_precio",
        ),
        CheckConstraint("subject_kind <> 'price' OR tolerance_kind = 'none'", name="precio_sin_tolerancia"),
        CheckConstraint(
            "(price_reference_kind = 'none') = (price_reference_amount IS NULL"
            " AND price_reference_currency IS NULL AND price_reference_basis IS NULL"
            " AND price_reference_quantity IS NULL AND price_reference_quantity_unit IS NULL)",
            name="referencia_nula_coherente",
        ),
        CheckConstraint(
            "price_reference_kind = 'none' OR (price_reference_amount > 0"
            " AND price_reference_currency IS NOT NULL AND price_reference_basis IS NOT NULL)",
            name="referencia_completa",
        ),
        CheckConstraint(
            "price_reference_currency IS NULL OR price_reference_currency ~ '^[A-Z]{3}$'",
            name="moneda_referencia_valida",
        ),
        CheckConstraint(
            "price_reference_quantity IS NULL OR price_reference_quantity > 0",
            name="cantidad_referencia_positiva",
        ),
        CheckConstraint(
            "price_reference_basis IS DISTINCT FROM 'per_line_total'"
            " OR (price_reference_quantity IS NOT NULL"
            " AND price_reference_quantity_unit IS NOT NULL)",
            name="referencia_total_con_cantidad",
        ),
        CheckConstraint(
            "price_reference_basis = 'per_line_total' OR (price_reference_quantity IS NULL"
            " AND price_reference_quantity_unit IS NULL)",
            name="referencia_unitaria_sin_cantidad",
        ),
        CheckConstraint(
            "price_reference_quantity_unit IS NULL OR price_reference_quantity_unit IN"
            " ('box','package','unit','bottle')",
            name="unidad_referencia_valida",
        ),
        Index(
            "ix_commercial_preference_item",
            "organization_id",
            "trade_item_id",
            "subject_kind",
            "created_at",
        ),
        Index("ix_commercial_preference_interpretation", "interpretation_id"),
        Index("ix_commercial_preference_status", "organization_id", "trade_item_id", "status"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    interpretation_id: Mapped[uuid.UUID] = mapped_column()
    observation_id: Mapped[uuid.UUID] = mapped_column()
    trade_item_id: Mapped[uuid.UUID] = mapped_column()
    subject_kind: Mapped[str] = mapped_column(String(16))
    # Clave normalizada para comparar (laboratorio/marca/proveedor/in_stock/price).
    subject_key: Mapped[str] = mapped_column(String(300))
    # Etiqueta legible tal como se interpretó; no se muestra como explicación de la IA.
    subject_label: Mapped[str] = mapped_column(String(300))
    subject_supplier_id: Mapped[uuid.UUID | None] = mapped_column()
    effect: Mapped[str] = mapped_column(String(12))
    tolerance_kind: Mapped[str] = mapped_column(String(24), server_default="none")
    tolerance_percent: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    tolerance_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    tolerance_currency: Mapped[str | None] = mapped_column(String(3))
    tolerance_origin: Mapped[str] = mapped_column(String(24), server_default="not_applicable")
    price_reference_kind: Mapped[str] = mapped_column(String(16), server_default="none")
    price_reference_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    price_reference_currency: Mapped[str | None] = mapped_column(String(3))
    price_reference_basis: Mapped[str | None] = mapped_column(String(24))
    price_reference_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    price_reference_quantity_unit: Mapped[str | None] = mapped_column(String(10))
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2))
    status: Mapped[str] = mapped_column(String(16), server_default="suggested")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    data_origin: Mapped[str] = mapped_column(String(12), server_default="ai_derived")


class CommercialArticlePreference(Base):
    """Preferencia comercial **entre artículos** (A6 Fase 4, D-147).

    Separada de `commercial_preference`, que solo ordena ofertas del mismo artículo. Aquí no se
    declara equivalencia farmacológica: cada fila es un evento explícito sobre uno o dos artículos
    del catálogo.

    * `deprioritize` con `relative_to_article_id = NULL`: baja la prioridad del artículo en
      cualquier lista donde aparezca.
    * `prefer` con `relative_to_article_id`: el artículo gana frente a la alternativa cuando
      ambos están presentes. Se guarda una sola dirección (ganador → perdedor).
    * `retire`: deja sin efecto el evento anterior de la misma relación (global o par). No borra
      historia: el motor resuelve el evento más reciente.

    `article_label` y `relative_to_article_label` conservan el nombre verificado al momento de
    interpretar, para que la API no tenga que inventar etiquetas ni exponer el texto del modelo.
    """

    __tablename__ = "commercial_article_preference"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["interpretation_id", "organization_id"],
            [
                "observation_interpretation.id",
                "observation_interpretation.organization_id",
            ],
        ),
        ForeignKeyConstraint(
            ["observation_id", "organization_id"],
            ["commercial_observation.id", "commercial_observation.organization_id"],
        ),
        ForeignKeyConstraint(
            ["article_id", "organization_id"], ["trade_item.id", "trade_item.organization_id"]
        ),
        ForeignKeyConstraint(
            ["relative_to_article_id", "organization_id"],
            ["trade_item.id", "trade_item.organization_id"],
        ),
        CheckConstraint(
            "effect IN ('prefer','deprioritize','retire')", name="efecto_valido"
        ),
        CheckConstraint(
            "status IN ('suggested','confirmed','rejected','needs_review')",
            name="estado_valido",
        ),
        CheckConstraint(
            "relative_to_article_id IS NULL OR article_id <> relative_to_article_id",
            name="articulos_distintos",
        ),
        # Preferir exige una alternativa; bajar la prioridad global no lleva alternativa.
        CheckConstraint(
            "effect <> 'prefer' OR relative_to_article_id IS NOT NULL",
            name="preferencia_con_alternativa",
        ),
        CheckConstraint(
            "effect <> 'deprioritize' OR relative_to_article_id IS NULL",
            name="baja_sin_alternativa",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confianza_valida"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        CheckConstraint(
            "length(btrim(article_label)) > 0 AND length(article_label) <= 300",
            name="etiqueta_articulo_valida",
        ),
        CheckConstraint(
            "relative_to_article_label IS NULL OR (length(btrim(relative_to_article_label)) > 0"
            " AND length(relative_to_article_label) <= 300)",
            name="etiqueta_alternativa_valida",
        ),
        Index(
            "ix_commercial_article_preference_article",
            "organization_id",
            "article_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_commercial_article_preference_relative",
            "organization_id",
            "relative_to_article_id",
            "status",
            "created_at",
        ),
        Index("ix_commercial_article_preference_interpretation", "interpretation_id"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    interpretation_id: Mapped[uuid.UUID] = mapped_column()
    observation_id: Mapped[uuid.UUID] = mapped_column()
    article_id: Mapped[uuid.UUID] = mapped_column()
    article_label: Mapped[str] = mapped_column(String(300))
    relative_to_article_id: Mapped[uuid.UUID | None] = mapped_column()
    relative_to_article_label: Mapped[str | None] = mapped_column(String(300))
    effect: Mapped[str] = mapped_column(String(12))
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2))
    status: Mapped[str] = mapped_column(String(16), server_default="suggested")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    data_origin: Mapped[str] = mapped_column(String(12), server_default="ai_derived")


class PreferenceOrderTrace(Base):
    """Traza interna del orden aplicado a las ofertas de un artículo (AC31/D-141).

    No se muestra en el cotizador. Guarda qué interpretaciones y preferencias participaron, qué
    clases se retiraron explícitamente (D-144) y el orden anterior y resultante. Es de solo
    agregar: corregir el orden no reescribe la historia.
    """

    __tablename__ = "preference_order_trace"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["trade_item_id", "organization_id"], ["trade_item.id", "trade_item.organization_id"]
        ),
        CheckConstraint("context IN ('article_detail','comparison')", name="contexto_valido"),
        CheckConstraint("jsonb_typeof(interpretation_ids) = 'array'", name="interpretaciones_validas"),
        CheckConstraint("jsonb_typeof(preference_ids) = 'array'", name="preferencias_validas"),
        CheckConstraint("jsonb_typeof(applied_preferences) = 'array'", name="aplicadas_validas"),
        CheckConstraint(
            "jsonb_typeof(retired_subject_kinds) = 'array'", name="retiros_validos"
        ),
        CheckConstraint(
            f"retired_subject_kinds <@ '{RETIRED_SUBJECT_KINDS_JSON}'::jsonb",
            name="retiros_permitidos",
        ),
        CheckConstraint("jsonb_typeof(offer_ids_before) = 'array'", name="orden_previo_valido"),
        CheckConstraint("jsonb_typeof(offer_ids_after) = 'array'", name="orden_nuevo_valido"),
        CheckConstraint("length(offer_set_hash) = 64", name="hash_ofertas_valido"),
        CheckConstraint("length(preference_fingerprint) = 64", name="huella_valida"),
        UniqueConstraint(
            "organization_id",
            "trade_item_id",
            "context",
            "offer_set_hash",
            "preference_fingerprint",
            name="uq_preference_order_trace_estado",
        ),
        Index("ix_preference_order_trace_item", "organization_id", "trade_item_id", "created_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    trade_item_id: Mapped[uuid.UUID] = mapped_column()
    context: Mapped[str] = mapped_column(String(20))
    interpretation_ids: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    preference_ids: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    applied_preferences: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    retired_subject_kinds: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    offer_ids_before: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    offer_ids_after: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    offer_set_hash: Mapped[str] = mapped_column(String(64))
    preference_fingerprint: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[uuid.UUID] = mapped_column(server_default=func.gen_random_uuid())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
