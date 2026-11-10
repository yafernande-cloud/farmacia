"""motor declarativo de proveedores: perfiles de conexión versionados

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-09 18:00:00.000000

Agrega `supplier_connector_profile`, una configuración por fuente de proveedor. El perfil dice
cómo consultar (mecanismo, endpoint, mapeo de campos) y no contiene secretos: solo referencias a
variables de entorno. La búsqueda en vivo no persiste ofertas; esta tabla conserva únicamente la
configuración y el estado operativo mínimo.

No hay backfill: ninguna fuente existente gana un perfil. Las instalaciones actuales siguen
funcionando igual hasta que una persona configure un conector.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supplier_connector_profile",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("supplier_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("mechanism", sa.String(length=32), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "field_map",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("last_error_kind", sa.String(length=16), nullable=True),
        sa.Column("schema_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "data_origin",
            sa.String(length=12),
            server_default="unclassified",
            nullable=False,
        ),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mechanism IN ('algolia','rest_json','html_form','html_embedded_json',"
            "'manual_capture','browser_required','unsupported')",
            name=op.f("ck_supplier_connector_profile_mecanismo_valido"),
        ),
        sa.CheckConstraint(
            "last_error_kind IS NULL OR last_error_kind IN ('transient','intervention','configuration')",
            name=op.f("ck_supplier_connector_profile_tipo_error_valido"),
        ),
        sa.CheckConstraint(
            "(last_error IS NULL) = (last_error_kind IS NULL)",
            name=op.f("ck_supplier_connector_profile_error_coherente"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(config) = 'object'",
            name=op.f("ck_supplier_connector_profile_config_objeto"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(field_map) = 'object'",
            name=op.f("ck_supplier_connector_profile_mapeo_objeto"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_supplier_connector_profile_version_valida"),
        ),
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name=op.f("ck_supplier_connector_profile_origen_dato_valido"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
            name=op.f(
                "fk_supplier_connector_profile_created_by_app_user"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_supplier_connector_profile_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "supplier_id", "organization_id"],
            ["source.id", "source.supplier_id", "source.organization_id"],
            name=op.f("fk_supplier_connector_profile_source_id_source"),
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id", "organization_id"],
            ["supplier.id", "supplier.organization_id"],
            name=op.f("fk_supplier_connector_profile_supplier_id_supplier"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_connector_profile")),
        sa.UniqueConstraint(
            "id",
            "organization_id",
            name=op.f("uq_supplier_connector_profile_id"),
        ),
        sa.UniqueConstraint(
            "organization_id",
            "source_id",
            name=op.f("uq_supplier_connector_profile_organization_id"),
        ),
    )
    op.create_index(
        "ix_supplier_connector_profile_supplier",
        "supplier_connector_profile",
        ["organization_id", "supplier_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_supplier_connector_profile_supplier",
        table_name="supplier_connector_profile",
    )
    op.drop_table("supplier_connector_profile")
