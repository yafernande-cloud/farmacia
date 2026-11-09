"""baja lógica de auxiliares de Telegram y nombre del autor al registrar (D1–D5)

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-08 12:00:00.000000

Dos cambios independientes, ambos sin backfill:

1. `app_user` gana una baja lógica como auxiliar de Telegram
   (`telegram_auxiliary_deleted_at/by/reason`). Es terminal, queda auditada y NO toca
   `active`, `roles`, `auth_subject` ni el acceso web: una administradora eliminada del
   listado de auxiliares sigue entrando al Cotizador. El índice de unicidad de nombres
   `uq_app_user_active_name` excluye a las personas dadas de baja, de modo que se puede
   crear otra persona con el mismo nombre (siempre con un UUID nuevo).

2. `demand_item.reported_by_name` y `demand_observation.author_name_snapshot` conservan el
   nombre del autor en el momento del registro. Las filas existentes quedan NULL y las vistas
   usan el nombre actual como respaldo: no se reescribe ningún reporte histórico.

`downgrade` revierte ambos cambios; no hay datos que reconstruir.
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column("telegram_auxiliary_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("app_user", sa.Column("telegram_auxiliary_deleted_by", sa.Uuid(), nullable=True))
    op.add_column(
        "app_user",
        sa.Column("telegram_auxiliary_deletion_reason", sa.String(length=200), nullable=True),
    )
    op.create_foreign_key(
        "fk_app_user_telegram_auxiliary_deleted_by_app_user",
        "app_user",
        "app_user",
        ["telegram_auxiliary_deleted_by", "organization_id"],
        ["id", "organization_id"],
    )
    op.create_check_constraint(
        "baja_auxiliar_coherente",
        "app_user",
        "(telegram_auxiliary_deleted_at IS NULL AND telegram_auxiliary_deleted_by IS NULL) OR "
        "(telegram_auxiliary_deleted_at IS NOT NULL AND telegram_auxiliary_deleted_by IS NOT NULL)",
    )

    # El nombre se libera al dar de baja: la autoría histórica sigue por UUID, no por nombre.
    op.drop_index("uq_app_user_active_name", table_name="app_user")
    op.create_index(
        "uq_app_user_active_name",
        "app_user",
        ["organization_id", sa.literal_column("lower(display_name)")],
        unique=True,
        postgresql_where=sa.text("active AND telegram_auxiliary_deleted_at IS NULL"),
    )

    op.add_column(
        "demand_item", sa.Column("reported_by_name", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "demand_observation",
        sa.Column("author_name_snapshot", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("demand_observation", "author_name_snapshot")
    op.drop_column("demand_item", "reported_by_name")

    op.drop_index("uq_app_user_active_name", table_name="app_user")
    op.create_index(
        "uq_app_user_active_name",
        "app_user",
        ["organization_id", sa.literal_column("lower(display_name)")],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    op.drop_constraint("baja_auxiliar_coherente", "app_user", type_="check")
    op.drop_constraint(
        "fk_app_user_telegram_auxiliary_deleted_by_app_user", "app_user", type_="foreignkey"
    )
    op.drop_column("app_user", "telegram_auxiliary_deletion_reason")
    op.drop_column("app_user", "telegram_auxiliary_deleted_by")
    op.drop_column("app_user", "telegram_auxiliary_deleted_at")
