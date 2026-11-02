"""Búsqueda de productos y ofertas (C4, E1–E3; C0 §3.4 y §E1–E3).

Una corrida de búsqueda se guarda **completa** para que E2 devuelva el mismo snapshot sin
reejecutar nada: la consulta y su interpretación, el estado por fuente, y las filas de resultado
con su clasificación `relation` y los agregados de ofertas observados en ese momento.

Nada aquí es una oferta: las ofertas siguen viviendo en `offer_snapshot` (inmutables y fechadas).
Estas tablas son de solo agregar: una búsqueda no se edita ni se reescribe; una consulta nueva crea
otra corrida. La búsqueda de cadenas (E15) **no** se persiste (contrato §11.9).
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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.catalog import ORIGINS

UUID_PK = dict(primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid())
_NOW = dict(server_default=func.now())

# Clasificación comercial de la v0.2 (contrato §12.1). `match.level` conserva la forma de C0.
RELATIONS = "'exact','other_presentation','combined','related','needs_review'"
MATCH_LEVELS = "'exact','equivalent_authorized','needs_review'"
SOURCE_STATES = "'ok','stale','failed','not_configured'"


class SearchRun(Base):
    """Una corrida de E1: consulta, interpretación, filtros y desenlace global."""

    __tablename__ = "search_run"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["created_by", "organization_id"], ["app_user.id", "app_user.organization_id"]
        ),
        CheckConstraint("status IN ('completed','partial','failed')", name="estado_valido"),
        CheckConstraint("limit_value BETWEEN 1 AND 50", name="limite_valido"),
        CheckConstraint("result_count >= 0", name="resultados_no_negativos"),
        CheckConstraint("error_count >= 0", name="errores_no_negativos"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        Index("ix_search_run_org_created", "organization_id", "created_at"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    query_text: Mapped[str] = mapped_column(String(200))
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    interpretation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(10), server_default="completed")
    limit_value: Mapped[int] = mapped_column(Integer, server_default="20")
    result_count: Mapped[int] = mapped_column(Integer, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, server_default="0")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID] = mapped_column()
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class SearchSourceResult(Base):
    """Estado y aporte de una fuente en una corrida concreta (E1 `sources[]`, E2)."""

    __tablename__ = "search_source_result"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("search_run_id", "source_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["search_run_id", "organization_id"],
            ["search_run.id", "search_run.organization_id"],
        ),
        ForeignKeyConstraint(
            ["source_id", "organization_id"], ["source.id", "source.organization_id"]
        ),
        CheckConstraint(f"state IN ({SOURCE_STATES})", name="estado_valido"),
        CheckConstraint("outcome IS NULL OR outcome IN ('matches','no_matches')", name="resultado_valido"),
        CheckConstraint("matches_count >= 0", name="coincidencias_no_negativas"),
        # Un fallo siempre dice qué falló y si conviene reintentar; lo demás nunca se disfraza de fallo.
        CheckConstraint(
            "(state = 'failed') = (error_code IS NOT NULL)", name="fallo_con_codigo"
        ),
        CheckConstraint(
            "(state = 'failed') = (error_message IS NOT NULL)", name="fallo_con_mensaje"
        ),
        CheckConstraint(
            "(state = 'failed') = (retryable IS NOT NULL)", name="fallo_con_reintento"
        ),
        CheckConstraint(
            "(state IN ('ok','stale')) = (outcome IS NOT NULL)", name="resultado_coherente"
        ),
        Index("ix_search_source_result_run", "search_run_id"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    search_run_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID] = mapped_column()
    state: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str | None] = mapped_column(String(12))
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(String(300))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    matches_count: Mapped[int] = mapped_column(Integer, server_default="0")
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class SearchResult(Base):
    """Una fila de resultado de E1, congelada con su clasificación y sus agregados.

    `relation` (v0.2 §12.1) describe la relación comercial; `match_level` conserva la forma de C0.
    `display_name`, `identity_summary` y `presentation` son la copia que se mostró: si el artículo
    cambia después, la búsqueda guardada no cambia.
    """

    __tablename__ = "search_result"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("search_run_id", "trade_item_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["search_run_id", "organization_id"],
            ["search_run.id", "search_run.organization_id"],
        ),
        ForeignKeyConstraint(
            ["trade_item_id", "organization_id"],
            ["trade_item.id", "trade_item.organization_id"],
        ),
        CheckConstraint(f"relation IN ({RELATIONS})", name="relacion_valida"),
        CheckConstraint(f"match_level IN ({MATCH_LEVELS})", name="nivel_valido"),
        CheckConstraint("rank >= 0", name="orden_valido"),
        CheckConstraint("offer_count >= 0", name="ofertas_no_negativas"),
        CheckConstraint("supplier_count >= 0", name="proveedores_no_negativos"),
        CheckConstraint("supplier_count <= offer_count", name="proveedores_caben_en_ofertas"),
        CheckConstraint(
            "(lowest_price_amount IS NULL) = (lowest_price_currency IS NULL)",
            name="precio_con_moneda",
        ),
        CheckConstraint("lowest_price_amount IS NULL OR lowest_price_amount >= 0",
                        name="precio_no_negativo"),  # fmt: skip
        CheckConstraint(
            "lowest_price_currency IS NULL OR lowest_price_currency ~ '^[A-Z]{3}$'",
            name="moneda_valida",
        ),
        CheckConstraint("jsonb_typeof(match_causes) = 'array'", name="causas_validas"),
        CheckConstraint("jsonb_typeof(source_ids) = 'array'", name="fuentes_validas"),
        Index("ix_search_result_run_rank", "search_run_id", "rank"),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    search_run_id: Mapped[uuid.UUID] = mapped_column()
    trade_item_id: Mapped[uuid.UUID] = mapped_column()
    rank: Mapped[int] = mapped_column(Integer)
    relation: Mapped[str] = mapped_column(String(20))
    match_level: Mapped[str] = mapped_column(String(20))
    match_causes: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    display_name: Mapped[str] = mapped_column(String(300))
    identity_summary: Mapped[str] = mapped_column(String(300))
    presentation: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    offer_count: Mapped[int] = mapped_column(Integer, server_default="0")
    supplier_count: Mapped[int] = mapped_column(Integer, server_default="0")
    lowest_price_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    lowest_price_currency: Mapped[str | None] = mapped_column(String(3))
    source_ids: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
