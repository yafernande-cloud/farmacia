"""interpretación de observaciones y preferencias comerciales por artículo (A6, O2–O4, D-136/D-138/D-141)

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-03 09:00:00.000000

La IA interpreta una observación **de un artículo concreto** y produce una interpretación estructurada
y una o más preferencias. El texto original no se toca y cada llamada deja una fila histórica:
`suggested`/`confirmed` pueden aplicarse; `rejected`, `failed`, `needs_review` y `no_preference` no.

`commercial_preference` no ejecuta texto ni guarda explicaciones: guarda sujeto, efecto, tolerancia y
referencia de precio con tipos y restricciones. `preference_order_trace` conserva internamente qué
preferencias participaron y cómo cambió el orden (AC31/D-141).

También se agregan a `offer_snapshot` el laboratorio y la marca **tal como los publica la fuente**
(`laboratory_as_published`, `brand_as_published`). Son opcionales: NULL significa no informado y nunca
se deduce del nombre del producto. No se modifican precios, stock, elegibilidad ni totales.

`downgrade` elimina las tres tablas nuevas, sus gatillos y las dos columnas de oferta; no toca ninguna
otra tabla ni datos existentes. Las interpretaciones y preferencias se pierden al revertir: exportarlas
antes si se necesitan para auditoría.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
NOW = sa.text("now()")
EMPTY_ARRAY = sa.text("'[]'::jsonb")
ORIGINS = "'simulated','captured','ai_derived','unclassified'"


def upgrade() -> None:
    # ------------------------------------------------ datos publicados por la fuente
    op.add_column(
        "offer_snapshot", sa.Column("laboratory_as_published", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "offer_snapshot", sa.Column("brand_as_published", sa.String(length=200), nullable=True)
    )

    # Una observación puede ser interpretada muchas veces; el alcance compuesto permite que una
    # preferencia herede artículo y organización sin poder mezclarlos.
    op.create_unique_constraint(
        "uq_commercial_observation_scope",
        "commercial_observation",
        ["id", "trade_item_id", "organization_id"],
    )

    # ------------------------------------------------ interpretación
    op.create_table(
        "observation_interpretation",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("trade_item_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="suggested", nullable=False),
        sa.Column("model", sa.String(length=80), nullable=False),
        sa.Column("prompt_version", sa.String(length=40), nullable=False),
        sa.Column("raw_output", JSONB, nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("data_origin", sa.String(length=12), server_default="ai_derived", nullable=False),
        sa.CheckConstraint(
            "status IN ('suggested','confirmed','rejected','failed','needs_review','no_preference')",
            name=op.f("ck_observation_interpretation_estado_valido"),
        ),
        sa.CheckConstraint(
            "length(btrim(model)) > 0", name=op.f("ck_observation_interpretation_modelo_no_vacio")
        ),
        sa.CheckConstraint(
            "length(btrim(prompt_version)) > 0",
            name=op.f("ck_observation_interpretation_prompt_no_vacio"),
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens >= 0",
            name=op.f("ck_observation_interpretation_tokens_entrada_validos"),
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens >= 0",
            name=op.f("ck_observation_interpretation_tokens_salida_validos"),
        ),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0",
            name=op.f("ck_observation_interpretation_latencia_valida"),
        ),
        sa.CheckConstraint(
            "raw_output IS NULL OR jsonb_typeof(raw_output) = 'object'",
            name=op.f("ck_observation_interpretation_salida_valida"),
        ),
        sa.CheckConstraint(
            "(status <> 'failed') OR (error_code IS NOT NULL)",
            name=op.f("ck_observation_interpretation_fallo_con_codigo"),
        ),
        sa.CheckConstraint(
            "(error_code IS NULL) OR (status IN ('failed','needs_review'))",
            name=op.f("ck_observation_interpretation_codigo_solo_fallo"),
        ),
        sa.CheckConstraint(
            "(reviewed_by IS NULL) = (reviewed_at IS NULL)",
            name=op.f("ck_observation_interpretation_revision_coherente"),
        ),
        sa.CheckConstraint(
            "review_note IS NULL OR reviewed_at IS NOT NULL",
            name=op.f("ck_observation_interpretation_nota_con_revision"),
        ),
        sa.CheckConstraint(
            "(status IN ('confirmed','rejected')) = (reviewed_at IS NOT NULL)",
            name=op.f("ck_observation_interpretation_revision_obligatoria"),
        ),
        sa.CheckConstraint(
            f"data_origin IN ({ORIGINS})",
            name=op.f("ck_observation_interpretation_origen_dato_valido"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_observation_interpretation_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id", "trade_item_id", "organization_id"],
            [
                "commercial_observation.id",
                "commercial_observation.trade_item_id",
                "commercial_observation.organization_id",
            ],
            name=op.f("fk_observation_interpretation_observation_id_commercial_observation"),
        ),
        sa.ForeignKeyConstraint(
            ["trade_item_id", "organization_id"],
            ["trade_item.id", "trade_item.organization_id"],
            name=op.f("fk_observation_interpretation_trade_item_id_trade_item"),
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
            name=op.f("fk_observation_interpretation_reviewed_by_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_observation_interpretation")),
        sa.UniqueConstraint("id", "organization_id", name=op.f("uq_observation_interpretation_id")),
        sa.UniqueConstraint(
            "id",
            "trade_item_id",
            "organization_id",
            name="uq_observation_interpretation_scope",
        ),
    )
    op.create_index(
        "ix_observation_interpretation_item",
        "observation_interpretation",
        ["organization_id", "trade_item_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_observation_interpretation_observation",
        "observation_interpretation",
        ["observation_id", "created_at"],
        unique=False,
    )

    # ------------------------------------------------ preferencia estructurada
    op.create_table(
        "commercial_preference",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("interpretation_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("trade_item_id", sa.Uuid(), nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_key", sa.String(length=300), nullable=False),
        sa.Column("subject_label", sa.String(length=300), nullable=False),
        sa.Column("subject_supplier_id", sa.Uuid(), nullable=True),
        sa.Column("effect", sa.String(length=12), nullable=False),
        sa.Column("tolerance_kind", sa.String(length=24), server_default="none", nullable=False),
        sa.Column("tolerance_percent", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("tolerance_amount", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("tolerance_currency", sa.String(length=3), nullable=True),
        sa.Column(
            "tolerance_origin",
            sa.String(length=24),
            server_default="not_applicable",
            nullable=False,
        ),
        sa.Column("price_reference_kind", sa.String(length=16), server_default="none", nullable=False),
        sa.Column("price_reference_amount", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("price_reference_currency", sa.String(length=3), nullable=True),
        sa.Column("price_reference_basis", sa.String(length=24), nullable=True),
        sa.Column("price_reference_quantity", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("price_reference_quantity_unit", sa.String(length=10), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="suggested", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("data_origin", sa.String(length=12), server_default="ai_derived", nullable=False),
        sa.CheckConstraint(
            "subject_kind IN ('laboratory','brand','supplier','availability','price')",
            name=op.f("ck_commercial_preference_sujeto_valido"),
        ),
        sa.CheckConstraint(
            "effect IN ('prefer','deprioritize')",
            name=op.f("ck_commercial_preference_efecto_valido"),
        ),
        sa.CheckConstraint(
            "status IN ('suggested','confirmed','rejected','needs_review')",
            name=op.f("ck_commercial_preference_estado_valido"),
        ),
        sa.CheckConstraint(
            f"data_origin IN ({ORIGINS})",
            name=op.f("ck_commercial_preference_origen_dato_valido"),
        ),
        sa.CheckConstraint(
            "length(btrim(subject_key)) > 0 AND length(subject_key) <= 300",
            name=op.f("ck_commercial_preference_clave_sujeto_valida"),
        ),
        sa.CheckConstraint(
            "length(btrim(subject_label)) > 0 AND length(subject_label) <= 300",
            name=op.f("ck_commercial_preference_etiqueta_sujeto_valida"),
        ),
        sa.CheckConstraint(
            "(subject_kind = 'supplier') = (subject_supplier_id IS NOT NULL)",
            name=op.f("ck_commercial_preference_proveedor_coherente"),
        ),
        sa.CheckConstraint(
            "subject_kind <> 'availability' OR (effect = 'prefer' AND subject_key = 'in_stock')",
            name=op.f("ck_commercial_preference_disponibilidad_valida"),
        ),
        sa.CheckConstraint(
            "subject_kind <> 'price' OR effect = 'prefer'",
            name=op.f("ck_commercial_preference_precio_solo_prefiere"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_commercial_preference_confianza_valida"),
        ),
        sa.CheckConstraint(
            "tolerance_kind IN ('none','relative_percent','absolute_amount')",
            name=op.f("ck_commercial_preference_tolerancia_valida"),
        ),
        sa.CheckConstraint(
            "tolerance_origin IN ('not_applicable','implicit_default','explicit')",
            name=op.f("ck_commercial_preference_origen_tolerancia_valido"),
        ),
        sa.CheckConstraint(
            "(tolerance_kind = 'none') OR (subject_kind IN ('laboratory','brand','supplier')"
            " AND effect = 'prefer')",
            name=op.f("ck_commercial_preference_tolerancia_solo_preferencia"),
        ),
        sa.CheckConstraint(
            "(tolerance_kind <> 'none') = (tolerance_origin <> 'not_applicable')",
            name=op.f("ck_commercial_preference_origen_tolerancia_coherente"),
        ),
        sa.CheckConstraint(
            "tolerance_kind <> 'relative_percent' OR (tolerance_percent > 0"
            " AND tolerance_percent <= 100 AND tolerance_amount IS NULL"
            " AND tolerance_currency IS NULL)",
            name=op.f("ck_commercial_preference_tolerancia_relativa_valida"),
        ),
        sa.CheckConstraint(
            "tolerance_kind <> 'absolute_amount' OR (tolerance_amount > 0"
            " AND tolerance_currency IS NOT NULL AND tolerance_percent IS NULL"
            " AND tolerance_origin = 'explicit')",
            name=op.f("ck_commercial_preference_tolerancia_absoluta_valida"),
        ),
        sa.CheckConstraint(
            "(tolerance_kind = 'none') = (tolerance_percent IS NULL AND tolerance_amount IS NULL"
            " AND tolerance_currency IS NULL)",
            name=op.f("ck_commercial_preference_tolerancia_nula_coherente"),
        ),
        sa.CheckConstraint(
            "tolerance_currency IS NULL OR tolerance_currency ~ '^[A-Z]{3}$'",
            name=op.f("ck_commercial_preference_moneda_tolerancia_valida"),
        ),
        sa.CheckConstraint(
            "price_reference_kind IN ('none','acceptable','maximum')",
            name=op.f("ck_commercial_preference_referencia_precio_valida"),
        ),
        sa.CheckConstraint(
            "price_reference_basis IS NULL OR price_reference_basis IN"
            " ('per_base_unit','per_commercial_unit','per_line_total')",
            name=op.f("ck_commercial_preference_base_precio_valida"),
        ),
        sa.CheckConstraint(
            "(subject_kind = 'price') = (price_reference_kind <> 'none')",
            name=op.f("ck_commercial_preference_referencia_solo_precio"),
        ),
        sa.CheckConstraint(
            "subject_kind <> 'price' OR tolerance_kind = 'none'",
            name=op.f("ck_commercial_preference_precio_sin_tolerancia"),
        ),
        sa.CheckConstraint(
            "(price_reference_kind = 'none') = (price_reference_amount IS NULL"
            " AND price_reference_currency IS NULL AND price_reference_basis IS NULL"
            " AND price_reference_quantity IS NULL AND price_reference_quantity_unit IS NULL)",
            name=op.f("ck_commercial_preference_referencia_nula_coherente"),
        ),
        sa.CheckConstraint(
            "price_reference_kind = 'none' OR (price_reference_amount > 0"
            " AND price_reference_currency IS NOT NULL AND price_reference_basis IS NOT NULL)",
            name=op.f("ck_commercial_preference_referencia_completa"),
        ),
        sa.CheckConstraint(
            "price_reference_currency IS NULL OR price_reference_currency ~ '^[A-Z]{3}$'",
            name=op.f("ck_commercial_preference_moneda_referencia_valida"),
        ),
        sa.CheckConstraint(
            "price_reference_quantity IS NULL OR price_reference_quantity > 0",
            name=op.f("ck_commercial_preference_cantidad_referencia_positiva"),
        ),
        sa.CheckConstraint(
            "price_reference_basis IS DISTINCT FROM 'per_line_total'"
            " OR (price_reference_quantity IS NOT NULL"
            " AND price_reference_quantity_unit IS NOT NULL)",
            name=op.f("ck_commercial_preference_referencia_total_con_cantidad"),
        ),
        sa.CheckConstraint(
            "price_reference_basis = 'per_line_total' OR (price_reference_quantity IS NULL"
            " AND price_reference_quantity_unit IS NULL)",
            name=op.f("ck_commercial_preference_referencia_unitaria_sin_cantidad"),
        ),
        sa.CheckConstraint(
            "price_reference_quantity_unit IS NULL OR price_reference_quantity_unit IN"
            " ('box','package','unit','bottle')",
            name=op.f("ck_commercial_preference_unidad_referencia_valida"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_commercial_preference_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["interpretation_id", "trade_item_id", "organization_id"],
            [
                "observation_interpretation.id",
                "observation_interpretation.trade_item_id",
                "observation_interpretation.organization_id",
            ],
            name=op.f("fk_commercial_preference_interpretation_id_observation_interpretation"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id", "trade_item_id", "organization_id"],
            [
                "commercial_observation.id",
                "commercial_observation.trade_item_id",
                "commercial_observation.organization_id",
            ],
            name=op.f("fk_commercial_preference_observation_id_commercial_observation"),
        ),
        sa.ForeignKeyConstraint(
            ["trade_item_id", "organization_id"],
            ["trade_item.id", "trade_item.organization_id"],
            name=op.f("fk_commercial_preference_trade_item_id_trade_item"),
        ),
        sa.ForeignKeyConstraint(
            ["subject_supplier_id", "organization_id"],
            ["supplier.id", "supplier.organization_id"],
            name=op.f("fk_commercial_preference_subject_supplier_id_supplier"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commercial_preference")),
        sa.UniqueConstraint("id", "organization_id", name=op.f("uq_commercial_preference_id")),
    )
    op.create_index(
        "ix_commercial_preference_item",
        "commercial_preference",
        ["organization_id", "trade_item_id", "subject_kind", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_commercial_preference_interpretation",
        "commercial_preference",
        ["interpretation_id"],
        unique=False,
    )
    op.create_index(
        "ix_commercial_preference_status",
        "commercial_preference",
        ["organization_id", "trade_item_id", "status"],
        unique=False,
    )

    # ------------------------------------------------ traza interna del orden
    op.create_table(
        "preference_order_trace",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("trade_item_id", sa.Uuid(), nullable=False),
        sa.Column("context", sa.String(length=20), nullable=False),
        sa.Column("interpretation_ids", JSONB, server_default=EMPTY_ARRAY, nullable=False),
        sa.Column("preference_ids", JSONB, server_default=EMPTY_ARRAY, nullable=False),
        sa.Column("applied_preferences", JSONB, server_default=EMPTY_ARRAY, nullable=False),
        sa.Column("offer_ids_before", JSONB, server_default=EMPTY_ARRAY, nullable=False),
        sa.Column("offer_ids_after", JSONB, server_default=EMPTY_ARRAY, nullable=False),
        sa.Column("offer_set_hash", sa.String(length=64), nullable=False),
        sa.Column("preference_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "correlation_id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "context IN ('article_detail','comparison')",
            name=op.f("ck_preference_order_trace_contexto_valido"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(interpretation_ids) = 'array'",
            name=op.f("ck_preference_order_trace_interpretaciones_validas"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(preference_ids) = 'array'",
            name=op.f("ck_preference_order_trace_preferencias_validas"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(applied_preferences) = 'array'",
            name=op.f("ck_preference_order_trace_aplicadas_validas"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(offer_ids_before) = 'array'",
            name=op.f("ck_preference_order_trace_orden_previo_valido"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(offer_ids_after) = 'array'",
            name=op.f("ck_preference_order_trace_orden_nuevo_valido"),
        ),
        sa.CheckConstraint(
            "length(offer_set_hash) = 64",
            name=op.f("ck_preference_order_trace_hash_ofertas_valido"),
        ),
        sa.CheckConstraint(
            "length(preference_fingerprint) = 64",
            name=op.f("ck_preference_order_trace_huella_valida"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_preference_order_trace_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["trade_item_id", "organization_id"],
            ["trade_item.id", "trade_item.organization_id"],
            name=op.f("fk_preference_order_trace_trade_item_id_trade_item"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preference_order_trace")),
        sa.UniqueConstraint("id", "organization_id", name=op.f("uq_preference_order_trace_id")),
        sa.UniqueConstraint(
            "organization_id",
            "trade_item_id",
            "context",
            "offer_set_hash",
            "preference_fingerprint",
            name="uq_preference_order_trace_estado",
        ),
    )
    op.create_index(
        "ix_preference_order_trace_item",
        "preference_order_trace",
        ["organization_id", "trade_item_id", "created_at"],
        unique=False,
    )

    # ------------------------------------------------ historia y revisión
    op.execute(
        """
        CREATE FUNCTION observation_interpretation_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'La interpretación es histórica (no se permite borrar)'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF ROW(NEW.organization_id, NEW.observation_id, NEW.trade_item_id, NEW.model,
                   NEW.prompt_version, NEW.raw_output, NEW.input_tokens, NEW.output_tokens,
                   NEW.latency_ms, NEW.error_code, NEW.data_origin, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.organization_id, OLD.observation_id, OLD.trade_item_id, OLD.model,
                   OLD.prompt_version, OLD.raw_output, OLD.input_tokens, OLD.output_tokens,
                   OLD.latency_ms, OLD.error_code, OLD.data_origin, OLD.created_at) THEN
                RAISE EXCEPTION 'La interpretación no se reescribe; solo se revisa su estado'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status THEN
                IF OLD.status IN ('confirmed','rejected','failed','no_preference') THEN
                    RAISE EXCEPTION 'La interpretación ya está en un estado terminal'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF OLD.status = 'suggested'
                   AND NEW.status NOT IN ('confirmed','rejected','needs_review') THEN
                    RAISE EXCEPTION 'Transición de interpretación no permitida'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF OLD.status = 'needs_review'
                   AND NEW.status NOT IN ('confirmed','rejected') THEN
                    RAISE EXCEPTION 'Transición de interpretación no permitida'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER observation_interpretation_guard "
        "BEFORE UPDATE OR DELETE ON observation_interpretation "
        "FOR EACH ROW EXECUTE FUNCTION observation_interpretation_guard()"
    )

    op.execute(
        """
        CREATE FUNCTION commercial_preference_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'La preferencia es histórica (no se permite borrar)'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF ROW(NEW.organization_id, NEW.interpretation_id, NEW.observation_id,
                   NEW.trade_item_id, NEW.subject_kind, NEW.subject_key, NEW.subject_label,
                   NEW.subject_supplier_id, NEW.effect, NEW.tolerance_kind,
                   NEW.tolerance_percent, NEW.tolerance_amount, NEW.tolerance_currency,
                   NEW.tolerance_origin, NEW.price_reference_kind, NEW.price_reference_amount,
                   NEW.price_reference_currency, NEW.price_reference_basis,
                   NEW.price_reference_quantity, NEW.price_reference_quantity_unit,
                   NEW.confidence, NEW.data_origin, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.organization_id, OLD.interpretation_id, OLD.observation_id,
                   OLD.trade_item_id, OLD.subject_kind, OLD.subject_key, OLD.subject_label,
                   OLD.subject_supplier_id, OLD.effect, OLD.tolerance_kind,
                   OLD.tolerance_percent, OLD.tolerance_amount, OLD.tolerance_currency,
                   OLD.tolerance_origin, OLD.price_reference_kind, OLD.price_reference_amount,
                   OLD.price_reference_currency, OLD.price_reference_basis,
                   OLD.price_reference_quantity, OLD.price_reference_quantity_unit,
                   OLD.confidence, OLD.data_origin, OLD.created_at) THEN
                RAISE EXCEPTION 'La preferencia no se reescribe; solo se revisa su estado'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status THEN
                IF OLD.status IN ('confirmed','rejected') THEN
                    RAISE EXCEPTION 'La preferencia ya está en un estado terminal'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF OLD.status = 'suggested'
                   AND NEW.status NOT IN ('confirmed','rejected','needs_review') THEN
                    RAISE EXCEPTION 'Transición de preferencia no permitida'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF OLD.status = 'needs_review'
                   AND NEW.status NOT IN ('confirmed','rejected') THEN
                    RAISE EXCEPTION 'Transición de preferencia no permitida'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER commercial_preference_guard "
        "BEFORE UPDATE OR DELETE ON commercial_preference "
        "FOR EACH ROW EXECUTE FUNCTION commercial_preference_guard()"
    )
    op.execute(
        "CREATE TRIGGER preference_order_trace_append_only "
        "BEFORE UPDATE OR DELETE ON preference_order_trace "
        "FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS preference_order_trace_append_only ON preference_order_trace")
    op.execute("DROP TRIGGER IF EXISTS commercial_preference_guard ON commercial_preference")
    op.execute("DROP TRIGGER IF EXISTS observation_interpretation_guard ON observation_interpretation")
    op.execute("DROP FUNCTION IF EXISTS commercial_preference_guard()")
    op.execute("DROP FUNCTION IF EXISTS observation_interpretation_guard()")
    op.drop_index("ix_preference_order_trace_item", table_name="preference_order_trace")
    op.drop_table("preference_order_trace")
    op.drop_index("ix_commercial_preference_status", table_name="commercial_preference")
    op.drop_index("ix_commercial_preference_interpretation", table_name="commercial_preference")
    op.drop_index("ix_commercial_preference_item", table_name="commercial_preference")
    op.drop_table("commercial_preference")
    op.drop_index(
        "ix_observation_interpretation_observation", table_name="observation_interpretation"
    )
    op.drop_index("ix_observation_interpretation_item", table_name="observation_interpretation")
    op.drop_table("observation_interpretation")
    op.drop_constraint(
        "uq_commercial_observation_scope", "commercial_observation", type_="unique"
    )
    op.drop_column("offer_snapshot", "brand_as_published")
    op.drop_column("offer_snapshot", "laboratory_as_published")
