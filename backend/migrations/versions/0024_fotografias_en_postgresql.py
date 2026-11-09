"""fotografías de Telegram en PostgreSQL (tabla normal, sin TTL)

Revision ID: 0024
Revises: 0020
Create Date: 2026-11-08 12:00:00.000000

El disco del dyno es efímero: las fotografías que un faltante activo debe seguir mostrando se
guardan en una tabla normal de PostgreSQL. `sha256` es la clave primaria, de modo que dos reportes
con bytes idénticos comparten una sola copia física. La baja no depende de la antigüedad: la decide
`services.photo_gc` cuando ningún reporte u operación viva necesita el contenido.

No hay backfill: los binarios que hoy viven en el disco local no son legibles desde una migración.
Si todavía existen en el dyno, se pueden recuperar desde Telegram con el procedimiento manual
(verificado por SHA-256); si el dyno se reinició, la recuperación se informa y no se inventa nada.

`down_revision` es `0020` a propósito: `0021`–`0023` están reservadas por el roadmap (C5-B/C5-C/
C5-D) y no existen en el grafo. Cuando se implemente C5-B, su migración `0021` debe encadenarse
desde esta revisión (o desde la última de la cadena) para no abrir dos cabezas.
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "temp_photos",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("mime_type", sa.String(length=50), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_temp_photos_sha256_valido")
        ),
        sa.CheckConstraint(
            "size_bytes >= 0", name=op.f("ck_temp_photos_tamano_valido")
        ),
        sa.PrimaryKeyConstraint("sha256", name=op.f("pk_temp_photos")),
    )


def downgrade() -> None:
    op.drop_table("temp_photos")
