"""proveedores: portal con credenciales cifradas y listas .xls/.csv

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-08 13:00:00.000000

Tres cambios, todos del módulo Proveedores (A3):

1. `supplier.portal_url` — la dirección del portal web de compras. Es opcional y no prueba que el
   portal funcione.
2. `supplier_credential` — usuario y contraseña del portal. La contraseña se guarda **cifrada**
   (`password_ciphertext`, Fernet con `SUPPLIER_CREDENTIALS_KEY`) y nunca se devuelve por la API.
   Una fila por proveedor y organización. No existe estado `connected`: guardar una credencial no
   abre ninguna sesión porque todavía no hay conector de portal.
3. `import_profile.file_format` admite `xlsx`, `xls` y `csv`. El motor de E8 lee los tres formatos;
   el perfil decide cuál corresponde a la fuente.

No hay backfill: ningún proveedor existente gana un portal ni una credencial. Ningún perfil
existente cambia de formato.

La revisión anterior de la cadena es `0024` (fotografías en PostgreSQL). `0021`–`0023` siguen
reservadas por el roadmap (C5-B/C5-C/C5-D); cuando se implementen, deben encadenarse desde esta
revisión o desde la última de la cadena para no abrir dos cabezas.
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("supplier", sa.Column("portal_url", sa.String(length=500), nullable=True))
    op.create_check_constraint(
        op.f("ck_supplier_url_portal_valida"),
        "supplier",
        "portal_url IS NULL OR portal_url ~* '^https?://'",
    )
    op.create_table(
        "supplier_credential",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("supplier_id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=200), nullable=False),
        sa.Column("password_ciphertext", sa.Text(), nullable=False),
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
            "length(username) BETWEEN 1 AND 200",
            name=op.f("ck_supplier_credential_usuario_valido"),
        ),
        sa.CheckConstraint(
            "length(password_ciphertext) >= 1",
            name=op.f("ck_supplier_credential_clave_presente"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_supplier_credential_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id", "organization_id"],
            ["supplier.id", "supplier.organization_id"],
            name=op.f("fk_supplier_credential_supplier_id_supplier"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
            name=op.f("fk_supplier_credential_created_by_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_credential")),
        sa.UniqueConstraint("id", "organization_id", name=op.f("uq_supplier_credential_id")),
        sa.UniqueConstraint(
            "organization_id", "supplier_id",
            name=op.f("uq_supplier_credential_organization_id"),
        ),
    )
    op.drop_constraint(
        op.f("ck_import_profile_formato_valido"), "import_profile", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_import_profile_formato_valido"),
        "import_profile",
        "file_format IN ('xlsx','xls','csv')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_import_profile_formato_valido"), "import_profile", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_import_profile_formato_valido"), "import_profile", "file_format IN ('xlsx')"
    )
    op.drop_table("supplier_credential")
    op.drop_constraint(op.f("ck_supplier_url_portal_valida"), "supplier", type_="check")
    op.drop_column("supplier", "portal_url")
