"""informe de cotización finalizada y liberación de faltantes

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-11 15:00:00.000000

Cierra el flujo del carrito: al finalizar, la cotización congela su informe y libera los faltantes
que quedaron cubiertos por completo.

`quote` agrega:
  * `report_number` / `report_label`: número correlativo por organización y su etiqueta legible
    (`COT-000001`). Solo existe en cotizaciones finalizadas (índice único parcial).
  * `finalized_at` / `finalized_by`: quién finalizó y cuándo.
  * `report` (JSONB): el informe congelado —productos, cantidades, proveedores, precios, subtotales
    y total—. Los cambios posteriores de los proveedores no lo modifican.
  * `finalize_key` / `finalize_hash`: idempotencia del cierre. Repetir la misma clave y contenido
    devuelve el informe original; nunca crea un segundo registro.
  * `finalizacion_coherente`: finalizar implica cerrar; una cotización sin finalizar conserva estos
    campos vacíos.

`demand_item` agrega el estado `released` y sus columnas `released_at`, `released_by` y
`released_quote_id`. Liberar **no** es comprar ni cumplir: solo deja de estar `active` cuando una
cotización finalizada cubrió toda la cantidad conocida, de modo que la misma persona pueda volver a
reportar el producto (el índice único de identidad solo aplica a `active`). El historial de la fila,
sus observaciones y sus asignaciones se conservan.

`downgrade` revierte columnas y constraints. Los faltantes `released` vuelven a `active` porque el
estado deja de existir; si la misma persona ya reportó de nuevo el producto, la fila liberada pasa a
`cancelled` para no violar la unicidad de identidades activas. Los informes ya generados se pierden
(no hay otra copia).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("quote", sa.Column("report_number", sa.Integer(), nullable=True))
    op.add_column("quote", sa.Column("report_label", sa.String(length=30), nullable=True))
    op.add_column(
        "quote", sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("quote", sa.Column("finalized_by", sa.Uuid(), nullable=True))
    op.add_column("quote", sa.Column("report", JSONB, nullable=True))
    op.add_column("quote", sa.Column("finalize_key", sa.String(length=128), nullable=True))
    op.add_column("quote", sa.Column("finalize_hash", sa.String(length=64), nullable=True))
    op.create_foreign_key(
        op.f("fk_quote_finalized_by_app_user"),
        "quote",
        "app_user",
        ["finalized_by", "organization_id"],
        ["id", "organization_id"],
    )
    op.create_check_constraint(
        op.f("ck_quote_finalizacion_coherente"),
        "quote",
        "(finalized_at IS NULL AND report_number IS NULL AND report_label IS NULL"
        " AND finalized_by IS NULL AND report IS NULL)"
        " OR (finalized_at IS NOT NULL AND report_number IS NOT NULL"
        " AND report_label IS NOT NULL AND finalized_by IS NOT NULL"
        " AND report IS NOT NULL AND status = 'closed')",
    )
    op.create_index(
        "uq_quote_report_number",
        "quote",
        ["organization_id", "report_number"],
        unique=True,
        postgresql_where=sa.text("report_number IS NOT NULL"),
    )
    op.create_index(
        "uq_quote_finalize_key",
        "quote",
        ["organization_id", "finalize_key"],
        unique=True,
        postgresql_where=sa.text("finalize_key IS NOT NULL"),
    )

    op.drop_constraint(op.f("ck_demand_item_estado_valido"), "demand_item", type_="check")
    op.create_check_constraint(
        op.f("ck_demand_item_estado_valido"),
        "demand_item",
        "status IN ('active','cancelled','fulfilled','released')",
    )
    op.add_column(
        "demand_item", sa.Column("released_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("demand_item", sa.Column("released_by", sa.Uuid(), nullable=True))
    op.add_column("demand_item", sa.Column("released_quote_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_demand_item_released_by_app_user",
        "demand_item",
        "app_user",
        ["released_by", "organization_id"],
        ["id", "organization_id"],
    )
    op.create_foreign_key(
        "fk_demand_item_released_quote",
        "demand_item",
        "quote",
        ["released_quote_id", "organization_id"],
        ["id", "organization_id"],
    )
    op.create_check_constraint(
        op.f("ck_demand_item_liberacion_coherente"),
        "demand_item",
        "(status = 'released') = (released_at IS NOT NULL AND released_by IS NOT NULL"
        " AND released_quote_id IS NOT NULL)",
    )
    op.create_index(
        "ix_demand_item_released_quote",
        "demand_item",
        ["organization_id", "released_quote_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_demand_item_released_quote", table_name="demand_item")
    op.drop_constraint(
        op.f("ck_demand_item_liberacion_coherente"), "demand_item", type_="check"
    )
    op.drop_constraint("fk_demand_item_released_quote", "demand_item", type_="foreignkey")
    op.drop_constraint(
        "fk_demand_item_released_by_app_user", "demand_item", type_="foreignkey"
    )
    # El estado `released` deja de existir: esos reportes vuelven a estar activos (no se borran).
    # Si la misma persona ya volvió a reportar el producto, no puede haber dos filas activas con
    # la misma identidad: la liberada se marca cancelada para conservar su historial sin chocar.
    op.execute(
        sa.text(
            "UPDATE demand_item d SET status = 'active', released_at = NULL,"
            " released_by = NULL, released_quote_id = NULL"
            " WHERE d.status = 'released' AND NOT EXISTS ("
            "  SELECT 1 FROM demand_item a"
            "  WHERE a.organization_id = d.organization_id AND a.cycle_id = d.cycle_id"
            "   AND a.reported_by = d.reported_by"
            "   AND a.resolved_identity_key = d.resolved_identity_key"
            "   AND a.status = 'active'"
            " )"
        )
    )
    op.execute(
        sa.text(
            "UPDATE demand_item SET status = 'cancelled',"
            " cancelled_at = COALESCE(released_at, now()), released_at = NULL,"
            " released_by = NULL, released_quote_id = NULL WHERE status = 'released'"
        )
    )
    op.drop_column("demand_item", "released_quote_id")
    op.drop_column("demand_item", "released_by")
    op.drop_column("demand_item", "released_at")
    op.drop_constraint(op.f("ck_demand_item_estado_valido"), "demand_item", type_="check")
    op.create_check_constraint(
        op.f("ck_demand_item_estado_valido"),
        "demand_item",
        "status IN ('active','cancelled','fulfilled')",
    )

    op.drop_index("uq_quote_finalize_key", table_name="quote")
    op.drop_index("uq_quote_report_number", table_name="quote")
    op.drop_constraint(
        op.f("ck_quote_finalizacion_coherente"), "quote", type_="check"
    )
    op.drop_constraint(
        op.f("fk_quote_finalized_by_app_user"), "quote", type_="foreignkey"
    )
    op.drop_column("quote", "finalize_hash")
    op.drop_column("quote", "finalize_key")
    op.drop_column("quote", "report")
    op.drop_column("quote", "finalized_by")
    op.drop_column("quote", "finalized_at")
    op.drop_column("quote", "report_label")
    op.drop_column("quote", "report_number")
