"""unidad comercial confirmada por la administración en una línea

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-13 12:00:00.000000

FarmaLoop no publica la unidad de venta de sus ofertas. Hasta ahora una línea de ese conector no
podía asociarse a un reporte medido en cajas, envases o botellas sin inventar la equivalencia, y
una línea no podía finalizar si la administración confirmaba la unidad después de agregarla.

Esta migración agrega a `quote_line` una marca booleana:

  * `quantity_unit_confirmed = false` (por defecto): la unidad de la línea es la que declaró la
    oferta, o desconocida si no declaró ninguna.
  * `quantity_unit_confirmed = true`: una persona confirmó la unidad comercial de la oferta. Solo
    se admite junto a una unidad no nula; el servicio exige además que la observación elegida
    tenga `sale_unit = NULL` (no se reemplaza un dato publicado por la fuente).

La observación de la oferta **no se modifica**: sigue conservando `sale_unit = NULL`. Quién
confirmó es `quote_line.added_by` y la decisión completa queda en la auditoría de
`quote.line.add` (`after.sale_unit_confirmation`).

`downgrade` elimina la columna; no transforma líneas ni inventa unidades.
"""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quote_line",
        sa.Column(
            "quantity_unit_confirmed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint(
        "confirmacion_con_unidad",
        "quote_line",
        "NOT quantity_unit_confirmed OR quantity_unit IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("confirmacion_con_unidad", "quote_line", type_="check")
    op.drop_column("quote_line", "quantity_unit_confirmed")
