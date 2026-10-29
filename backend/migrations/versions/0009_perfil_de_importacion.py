"""perfil de importación de planillas de proveedor

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-29 18:00:00.000000

Cierra el esquema que D-107 dejó propuesto para C2 (D-102, D-104):

  * `import_profile`: el mapeo columna→papel de UNA fuente, reutilizable en cada importación.
    Un proveedor con dos formatos se modela como dos fuentes, así que hay un perfil por fuente.
  * `import_run`: tres columnas nuevas, todas con valor por defecto para no tocar filas existentes:
      - `profile_id`    qué perfil se usó (FK compuesta por organización).
      - `profile_snapshot` el mapeo exacto aplicado, porque `import_run` es de solo agregar:
                          cambiar el perfil nunca reescribe una importación pasada.
      - `rejected_rows` las filas rechazadas, conservadas para poder identificarlas y revisarlas
                        después (D-104). Guarda solo lo necesario para eso, nunca el archivo entero.

Ningún dato del catálogo, de las ofertas ni de los precios públicos cambia: esta migración no crea
`trade_item`, no escribe `offer_snapshot`, no toca `chain_product` ni `reference_price`, y no
modifica el módulo de faltantes.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

jsonb = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        'import_profile',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('source_id', sa.Uuid(), nullable=False),
        sa.Column('file_format', sa.String(length=10), server_default='xlsx', nullable=False),
        sa.Column('sheet', sa.String(length=120), nullable=True),
        sa.Column('header_row', sa.Integer(), server_default='1', nullable=False),
        sa.Column('columns', jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('units', jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('options', jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('status', sa.String(length=10), server_default='active', nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified', nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("file_format IN ('xlsx')", name=op.f('ck_import_profile_formato_valido')),
        sa.CheckConstraint('header_row >= 1', name=op.f('ck_import_profile_fila_cabecera_valida')),
        sa.CheckConstraint("jsonb_typeof(columns) = 'object'", name=op.f('ck_import_profile_columnas_objeto')),
        sa.CheckConstraint("jsonb_typeof(units) = 'object'", name=op.f('ck_import_profile_unidades_objeto')),
        sa.CheckConstraint("jsonb_typeof(options) = 'object'", name=op.f('ck_import_profile_opciones_objeto')),
        sa.CheckConstraint("status IN ('active','inactive')", name=op.f('ck_import_profile_estado_valido')),
        sa.CheckConstraint('version >= 1', name=op.f('ck_import_profile_version_valida')),
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name=op.f('ck_import_profile_origen_dato_valido'),
        ),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_import_profile_organization_id_organization')),
        sa.ForeignKeyConstraint(
            ['source_id', 'organization_id'], ['source.id', 'source.organization_id'],
            name=op.f('fk_import_profile_source_id_source'),
        ),
        sa.ForeignKeyConstraint(
            ['created_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'],
            name=op.f('fk_import_profile_created_by_app_user'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_import_profile')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_import_profile_id')),
        sa.UniqueConstraint('organization_id', 'source_id', name=op.f('uq_import_profile_organization_id')),
    )  # fmt: skip

    op.add_column('import_run', sa.Column('profile_id', sa.Uuid(), nullable=True))
    op.add_column(
        'import_run',
        sa.Column('profile_snapshot', jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
    )
    op.add_column(
        'import_run',
        sa.Column('rejected_rows', jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.create_foreign_key(
        op.f('fk_import_run_profile_id_import_profile'),
        'import_run',
        'import_profile',
        ['profile_id', 'organization_id'],
        ['id', 'organization_id'],
    )
    op.create_check_constraint(
        op.f('ck_import_run_perfil_snapshot_valido'), 'import_run', "jsonb_typeof(profile_snapshot) = 'object'"
    )
    op.create_check_constraint(
        op.f('ck_import_run_rechazos_validos'), 'import_run', "jsonb_typeof(rejected_rows) = 'array'"
    )


def downgrade() -> None:
    op.drop_constraint(op.f('ck_import_run_rechazos_validos'), 'import_run', type_='check')
    op.drop_constraint(op.f('ck_import_run_perfil_snapshot_valido'), 'import_run', type_='check')
    op.drop_constraint(op.f('fk_import_run_profile_id_import_profile'), 'import_run', type_='foreignkey')
    op.drop_column('import_run', 'rejected_rows')
    op.drop_column('import_run', 'profile_snapshot')
    op.drop_column('import_run', 'profile_id')
    op.drop_table('import_profile')
