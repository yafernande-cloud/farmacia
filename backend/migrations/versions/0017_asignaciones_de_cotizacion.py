"""asignaciones de una línea de cotización a varios reportes (D-135)

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-02 21:10:00.000000

Una línea comercial de 20 unidades puede cubrir dos reportes independientes de 10 con dos
asignaciones explícitas. La suma de asignaciones **nunca** supera la cantidad de la línea; esa
invariante se valida en el servicio, porque la base no puede expresar una suma entre filas sin un
gatillo. La relación E1 se congela por asignación: el mismo producto puede ser `exact` para un
reporte y `other_presentation` para otro.

Backfill: cada línea existente con `demand_item_id` recibe una asignación por su cantidad total, con
la relación congelada y el mismo autor y fecha del alta. No se inventan asignaciones para líneas sin
vínculo. `quote_line.demand_item_id`/`coverage_relation` se conservan como atajo compatible para una
sola asignación; con varias, quedan NULL.

Reversión: el `downgrade` elimina `quote_line_allocation`. Las líneas vuelven a conservar solo el
atajo `quote_line.demand_item_id` y **se pierden** las asignaciones múltiples (una línea que cubría
a dos reportes no puede representarse en el esquema anterior). Antes de revertir hay que respaldar o
exportar las asignaciones; la migración no intenta reconstruirlas ni repartirlas.

No se crean tablas de compras: C5-B usa la 0018.
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

NOW = sa.text("now()")
RELATIONS = "'exact','other_presentation','combined','related','needs_review'"


def upgrade() -> None:
    op.create_table(
        "quote_line_allocation",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("quote_line_id", sa.Uuid(), nullable=False),
        sa.Column("demand_item_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("coverage_relation", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("quantity > 0",
                           name=op.f("ck_quote_line_allocation_cantidad_positiva")),  # fmt: skip
        sa.CheckConstraint("quantity = trunc(quantity)",
                           name=op.f("ck_quote_line_allocation_cantidad_entera")),  # fmt: skip
        sa.CheckConstraint(f"coverage_relation IN ({RELATIONS})",
                           name=op.f("ck_quote_line_allocation_relacion_valida")),  # fmt: skip
        sa.ForeignKeyConstraint(
            ["quote_line_id", "organization_id"],
            ["quote_line.id", "quote_line.organization_id"],
            name=op.f("fk_quote_line_allocation_quote_line_id_quote_line"),
        ),
        sa.ForeignKeyConstraint(
            ["demand_item_id", "organization_id"],
            ["demand_item.id", "demand_item.organization_id"],
            name=op.f("fk_quote_line_allocation_demand_item_id_demand_item"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
            name=op.f("fk_quote_line_allocation_created_by_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quote_line_allocation")),
        sa.UniqueConstraint("id", "organization_id", name=op.f("uq_quote_line_allocation_id")),
        sa.UniqueConstraint(
            "quote_line_id",
            "demand_item_id",
            name="uq_quote_line_allocation_line_demand",
        ),
    )
    op.create_index(
        "ix_quote_line_allocation_demand",
        "quote_line_allocation",
        ["organization_id", "demand_item_id"],
        unique=False,
    )
    # Backfill compatible: una línea con vínculo pasa a tener exactamente una asignación.
    op.execute(
        """
        INSERT INTO quote_line_allocation
            (organization_id, quote_line_id, demand_item_id, quantity, coverage_relation,
             created_by, created_at)
        SELECT organization_id, id, demand_item_id, quantity, coverage_relation,
               added_by, added_at
        FROM quote_line
        WHERE demand_item_id IS NOT NULL AND coverage_relation IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_quote_line_allocation_demand", table_name="quote_line_allocation")
    op.drop_table("quote_line_allocation")
