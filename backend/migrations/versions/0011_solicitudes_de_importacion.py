"""solicitudes de importación (idempotencia de E8)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-30 10:00:00.000000

El contrato E8 (§2.6) exige que la misma `Idempotency-Key` con **otro** archivo o contexto devuelva
409 `IDEMPOTENCY_KEY_REUSED`. Esa regla no cabe en `import_run`: es de solo agregar y una misma
corrida puede ser devuelta a varias solicitudes (reintentos con otra clave, CLI + API). Por eso la
solicitud tiene su propio registro.

  * `import_request`: una fila por solicitud HTTP **exitosa** que llevó `Idempotency-Key`, con el
    contexto exacto con que se usó (organización, fuente, proveedor, hash del archivo) y la corrida
    que devolvió. Único por (organización, clave): una clave no puede quedar atada a dos contextos.
  * De solo agregar, como `import_run`: lo que una clave significó no se reescribe.

Una solicitud fallida (403/404/409/413/422) no deja fila: la clave sigue disponible para un
reintento corregido. Cambio puramente aditivo; el sobre de E8 (contrato C0) no cambia. Con esta
migración, C3 pasa a 0012 y C4 a 0013 (D-111).
"""
import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'import_request',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('idempotency_key', sa.String(length=128), nullable=False),
        sa.Column('source_id', sa.Uuid(), nullable=False),
        sa.Column('supplier_id', sa.Uuid(), nullable=False),
        sa.Column('file_hash', sa.String(length=64), nullable=False),
        sa.Column('import_run_id', sa.Uuid(), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'data_origin', sa.String(length=12), server_default='unclassified', nullable=False
        ),
        sa.CheckConstraint(
            'length(idempotency_key) BETWEEN 8 AND 128',
            name=op.f('ck_import_request_clave_valida'),
        ),
        sa.CheckConstraint(
            "file_hash ~ '^[0-9a-f]{64}$'", name=op.f('ck_import_request_hash_valido')
        ),
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name=op.f('ck_import_request_origen_dato_valido'),
        ),
        sa.ForeignKeyConstraint(
            ['organization_id'],
            ['organization.id'],
            name=op.f('fk_import_request_organization_id_organization'),
        ),
        sa.ForeignKeyConstraint(
            ['source_id', 'organization_id'],
            ['source.id', 'source.organization_id'],
            name=op.f('fk_import_request_source_id_source'),
        ),
        sa.ForeignKeyConstraint(
            ['supplier_id', 'organization_id'],
            ['supplier.id', 'supplier.organization_id'],
            name=op.f('fk_import_request_supplier_id_supplier'),
        ),
        sa.ForeignKeyConstraint(
            ['import_run_id', 'organization_id'],
            ['import_run.id', 'import_run.organization_id'],
            name=op.f('fk_import_request_import_run_id_import_run'),
        ),
        sa.ForeignKeyConstraint(
            ['created_by', 'organization_id'],
            ['app_user.id', 'app_user.organization_id'],
            name=op.f('fk_import_request_created_by_app_user'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_import_request')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_import_request_id')),
        sa.UniqueConstraint(
            'organization_id',
            'idempotency_key',
            name=op.f('uq_import_request_organization_id'),
        ),
    )
    op.create_index(
        'ix_import_request_run',
        'import_request',
        ['organization_id', 'import_run_id'],
        unique=False,
    )
    op.execute(
        "CREATE TRIGGER import_request_append_only BEFORE UPDATE OR DELETE ON import_request "
        "FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
    )


def downgrade() -> None:
    op.execute('DROP TRIGGER import_request_append_only ON import_request')
    op.drop_index('ix_import_request_run', table_name='import_request')
    op.drop_table('import_request')
