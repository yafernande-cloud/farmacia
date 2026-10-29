"""estado de revisión de la oferta

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-29 20:00:00.000000

Agrega `offer_snapshot.review_status` (D-110), que hasta ahora solo existía como texto libre en
`notes` más un código en `import_run.errors`. El contrato ya habla de ofertas `needs_review`
(cotizador v0, E3), y derivarlas parseando texto sería justamente lo que el proyecto prohíbe.

  * `accepted`     la oferta se leyó completa con el perfil; es la única verificada.
  * `needs_review` la fila se aceptó pero trae una condición comercial no modelada: se conserva el
                   texto en `notes` y la oferta NO puede elegir proveedor.

`offer_snapshot` es de solo agregar (trigger `forbid_mutation()` de la 0008), así que la marca es un
hecho histórico del snapshot y no un flujo de trabajo: una oferta marcada nunca se «desmarca»; la
reemplaza una observación posterior. No se crea tabla de revisión ni se toca `import_run`.

Cambio puramente aditivo: la columna tiene valor por defecto, ninguna fila existente se reescribe y
el sobre de E8 (contrato C0) queda intacto porque el campo no se expone en la API.
"""
import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'offer_snapshot',
        sa.Column(
            'review_status', sa.String(length=20), server_default='accepted', nullable=False
        ),
    )
    op.create_check_constraint(
        op.f('ck_offer_snapshot_estado_revision_valido'),
        'offer_snapshot',
        "review_status IN ('accepted','needs_review')",
    )
    op.create_check_constraint(
        op.f('ck_offer_snapshot_revision_con_motivo'),
        'offer_snapshot',
        "(review_status <> 'needs_review') OR (notes IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f('ck_offer_snapshot_revision_con_motivo'), 'offer_snapshot', type_='check'
    )
    op.drop_constraint(
        op.f('ck_offer_snapshot_estado_revision_valido'), 'offer_snapshot', type_='check'
    )
    op.drop_column('offer_snapshot', 'review_status')
