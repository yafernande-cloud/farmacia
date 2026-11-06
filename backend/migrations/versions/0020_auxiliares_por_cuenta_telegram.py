"""auxiliares habilitados por cuenta de Telegram y sucursal histórica (T1, D-146)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-05 12:00:00.000000

Cada cuenta de teléfono compartido tiene su propia lista de personas habilitadas para reportar.
La relación es explícita y con baja lógica: no se concede ningún rol ni permiso, y las cuentas
personales siguen usando `telegram_account.app_user_id` sin filas aquí.

`telegram_account.branch_label` es la sucursal de la cuenta. `demand_observation.branch_label`
conserva una instantánea al registrar el aviso, de modo que cambiar la sucursal después no altera
reportes históricos.

No hay backfill: las cuentas compartidas existentes quedan con cero auxiliares hasta que un
administrador las configure. `downgrade` elimina la tabla y las dos columnas, sin tocar otras tablas.
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.add_column(
        "telegram_account", sa.Column("branch_label", sa.String(length=120), nullable=True)
    )
    op.add_column(
        "demand_observation", sa.Column("branch_label", sa.String(length=120), nullable=True)
    )

    op.create_table(
        "telegram_account_auxiliary",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("app_user_id", sa.Uuid(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deactivated_by", sa.Uuid(), nullable=True),
        sa.Column("deactivation_reason", sa.String(length=200), nullable=True),
        sa.CheckConstraint(
            "(active AND deactivated_at IS NULL AND deactivated_by IS NULL) OR "
            "(NOT active AND deactivated_at IS NOT NULL AND deactivated_by IS NOT NULL)",
            name=op.f("ck_telegram_account_auxiliary_desactivacion_coherente"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_telegram_account_auxiliary_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "organization_id"],
            ["telegram_account.id", "telegram_account.organization_id"],
            name=op.f("fk_telegram_account_auxiliary_account_id_telegram_account"),
        ),
        sa.ForeignKeyConstraint(
            ["app_user_id", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
            name=op.f("fk_telegram_account_auxiliary_app_user_id_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
            name=op.f("fk_telegram_account_auxiliary_created_by_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["deactivated_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
            name=op.f("fk_telegram_account_auxiliary_deactivated_by_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_account_auxiliary")),
        sa.UniqueConstraint(
            "id", "organization_id", name=op.f("uq_telegram_account_auxiliary_id")
        ),
        sa.UniqueConstraint(
            "account_id",
            "app_user_id",
            name="uq_telegram_account_auxiliary_pair",
        ),
    )
    op.create_index(
        "ix_telegram_account_auxiliary_active",
        "telegram_account_auxiliary",
        ["account_id", "active"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_account_auxiliary_org",
        "telegram_account_auxiliary",
        ["organization_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telegram_account_auxiliary_org", table_name="telegram_account_auxiliary"
    )
    op.drop_index(
        "ix_telegram_account_auxiliary_active", table_name="telegram_account_auxiliary"
    )
    op.drop_table("telegram_account_auxiliary")
    op.drop_column("demand_observation", "branch_label")
    op.drop_column("telegram_account", "branch_label")
