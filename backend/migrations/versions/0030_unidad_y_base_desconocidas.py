"""unidad de venta y base del precio desconocidas en ofertas vivas

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-11 18:00:00.000000

Un conector puede informar precio y stock sin declarar la unidad de venta ni la base del precio
(FarmaLoop no publica ninguno de los dos). Inventarlos —«caja», «por unidad comercial»— afirmaría
una condición comercial que la fuente nunca dijo, así que se conservan como desconocidos.

Esta migración solo relaja la obligatoriedad; no borra ni transforma datos:

  * `offer_snapshot.sale_unit` y `offer_snapshot.price_basis` aceptan NULL (la observación conserva
    el precio y la moneda, que sí están informados).
  * `quote_line.quantity_unit` acepta NULL: la cantidad se cuenta en ofertas seleccionadas y no se
    declara una unidad de venta que nadie confirmó.
  * `comparison_candidate.price_basis` acepta NULL: la evaluación compara ofertas y copia la base
    del precio que la oferta evaluada declare, o NULL cuando no la declara.

Los `CHECK` existentes no se modifican: en PostgreSQL una comparación con NULL no falla, así que un
valor presente sigue validándose igual.

`price_basis` pierde además su `server_default` (`per_commercial_unit`): mantenerlo haría que un
INSERT que declara «no informado» terminara afirmando una base comercial que nadie dijo. Las
importaciones, que sí conocen el dato, lo siguen enviando de forma explícita.

`downgrade` vuelve a exigir esos valores. Si ya existen ofertas vivas sin unidad o base, no se
inventa un valor: se detiene con un mensaje explícito para que la decisión sea de una persona.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def _conteo(tabla: str, columna: str) -> int:
    return op.get_bind().execute(sa.text(f"SELECT count(*) FROM {tabla} WHERE {columna} IS NULL")).scalar_one()


def upgrade() -> None:
    op.alter_column("quote_line", "quantity_unit", existing_type=sa.String(length=10), nullable=True)
    op.alter_column("offer_snapshot", "sale_unit", existing_type=sa.String(length=10), nullable=True)
    op.alter_column(
        "offer_snapshot",
        "price_basis",
        existing_type=sa.String(length=24),
        nullable=True,
        server_default=None,
    )
    op.alter_column(
        "comparison_candidate", "price_basis", existing_type=sa.String(length=24), nullable=True
    )


def downgrade() -> None:
    con_datos = {
        "quote_line.quantity_unit": _conteo("quote_line", "quantity_unit"),
        "offer_snapshot.sale_unit": _conteo("offer_snapshot", "sale_unit"),
        "offer_snapshot.price_basis": _conteo("offer_snapshot", "price_basis"),
        "comparison_candidate.price_basis": _conteo("comparison_candidate", "price_basis"),
    }
    pendientes = {clave: filas for clave, filas in con_datos.items() if filas}
    if pendientes:
        raise RuntimeError(
            "No se revierte 0030: hay ofertas o líneas sin unidad de venta o base del precio "
            f"({pendientes}). Decidir su valor es una decisión humana; no se completa por suposición."
        )
    op.alter_column(
        "comparison_candidate", "price_basis", existing_type=sa.String(length=24), nullable=False
    )
    op.alter_column(
        "offer_snapshot",
        "price_basis",
        existing_type=sa.String(length=24),
        nullable=False,
        server_default=sa.text("'per_commercial_unit'"),
    )
    op.alter_column("offer_snapshot", "sale_unit", existing_type=sa.String(length=10), nullable=False)
    op.alter_column("quote_line", "quantity_unit", existing_type=sa.String(length=10), nullable=False)
