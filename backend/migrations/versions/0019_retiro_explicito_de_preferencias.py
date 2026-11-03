"""retiro explícito de preferencias comerciales por clase (A6, D-144)

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-03 12:00:00.000000

Una interpretación aplicada puede retirar una clase completa para su `trade_item_id` mediante
`retired_subject_kinds`. El retiro es explícito: no se simula con `status='rejected'`, no crea
preferencias ficticias y no copia sujetos anteriores. Las clases válidas distinguen
`price:acceptable` de `price:maximum`; retirar una no retira la otra.

La traza interna conserva las clases retiradas en el momento de aplicar el orden, para que la
auditoría sea autosuficiente. Los estados `rejected`, `needs_review`, `failed` y `no_preference`
siguen sin efecto comercial.

`downgrade` elimina las dos columnas y sus restricciones y restaura el guardián de inmutabilidad
anterior. No toca datos de otras tablas.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
EMPTY_ARRAY = sa.text("'[]'::jsonb")
RETIRED = '["laboratory","brand","supplier","availability","price:acceptable","price:maximum"]'


def _guard_sql(*, with_retirement: bool) -> str:
    retirement_new = "NEW.retired_subject_kinds, " if with_retirement else ""
    retirement_old = "OLD.retired_subject_kinds, " if with_retirement else ""
    return f"""
        CREATE OR REPLACE FUNCTION observation_interpretation_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'La interpretación es histórica (no se permite borrar)'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF ROW(NEW.organization_id, NEW.observation_id, NEW.trade_item_id, NEW.model,
                   NEW.prompt_version, {retirement_new}NEW.raw_output, NEW.input_tokens,
                   NEW.output_tokens, NEW.latency_ms, NEW.error_code, NEW.data_origin,
                   NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.organization_id, OLD.observation_id, OLD.trade_item_id, OLD.model,
                   OLD.prompt_version, {retirement_old}OLD.raw_output, OLD.input_tokens,
                   OLD.output_tokens, OLD.latency_ms, OLD.error_code, OLD.data_origin,
                   OLD.created_at) THEN
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


def upgrade() -> None:
    op.add_column(
        "observation_interpretation",
        sa.Column("retired_subject_kinds", JSONB, server_default=EMPTY_ARRAY, nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_observation_interpretation_retiros_validos"),
        "observation_interpretation",
        "jsonb_typeof(retired_subject_kinds) = 'array'",
    )
    op.create_check_constraint(
        op.f("ck_observation_interpretation_retiros_permitidos"),
        "observation_interpretation",
        f"retired_subject_kinds <@ '{RETIRED}'::jsonb",
    )
    # El retiro es contenido de la interpretación: tampoco se reescribe.
    op.execute(_guard_sql(with_retirement=True))

    op.add_column(
        "preference_order_trace",
        sa.Column("retired_subject_kinds", JSONB, server_default=EMPTY_ARRAY, nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_preference_order_trace_retiros_validos"),
        "preference_order_trace",
        "jsonb_typeof(retired_subject_kinds) = 'array'",
    )
    op.create_check_constraint(
        op.f("ck_preference_order_trace_retiros_permitidos"),
        "preference_order_trace",
        f"retired_subject_kinds <@ '{RETIRED}'::jsonb",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_preference_order_trace_retiros_permitidos"),
        "preference_order_trace",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_preference_order_trace_retiros_validos"),
        "preference_order_trace",
        type_="check",
    )
    op.drop_column("preference_order_trace", "retired_subject_kinds")

    # Restaurar el guardián antes de eliminar la columna que referencia.
    op.execute(_guard_sql(with_retirement=False))
    op.drop_constraint(
        op.f("ck_observation_interpretation_retiros_permitidos"),
        "observation_interpretation",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_observation_interpretation_retiros_validos"),
        "observation_interpretation",
        type_="check",
    )
    op.drop_column("observation_interpretation", "retired_subject_kinds")
