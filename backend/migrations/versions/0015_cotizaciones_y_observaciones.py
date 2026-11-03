"""cotizaciones, carritos por proveedor, líneas y observaciones comerciales (C5-A, E10 y O1)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-02 20:00:00.000000

Cuatro tablas del cotizador. La cotización es persistente (sin TTL ni archivo), el carrito nace
con la primera línea y la línea **no guarda precio**: solo referencia el `offer_snapshot` elegido,
que es su ancla de precio y condiciones (§3, V-03).

Integridad que impone la base:

  * una línea no puede pertenecer a un carrito de otro proveedor (FK compuesta);
  * `demand_item_id` y `coverage_relation` van juntos o no van;
  * la cantidad es positiva y entera (E5 no compara fracciones de caja);
  * dos selecciones activas de la misma oferta en el mismo carrito son una sola línea;
  * `commercial_observation` es inmutable: el gatillo `forbid_mutation()` (creado en la 0001)
    rechaza UPDATE y DELETE.

No se crean tablas de compras, recepción ni interpretación de IA: A6 (Fase 1) usa la 0018 y C5-B la 0019,
y la recepción física está fuera del MVP (D-126).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
NOW = sa.text('now()')
EMPTY_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    # ------------------------------------------------------------------ cotización
    op.create_table(
        'quote',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('title', sa.String(length=120), nullable=True),
        sa.Column('status', sa.String(length=10), server_default='draft', nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('closed_by', sa.Uuid(), nullable=True),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified',
                  nullable=False),  # fmt: skip
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.CheckConstraint("status IN ('draft','closed')",
                           name=op.f('ck_quote_estado_valido')),  # fmt: skip
        # Cerrada exige autor y fecha; abierta no los conserva (reabrir los limpia).
        sa.CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL AND closed_by IS NOT NULL)",
            name=op.f('ck_quote_cierre_coherente'),
        ),
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name=op.f('ck_quote_origen_dato_valido'),
        ),
        sa.CheckConstraint('version >= 1', name=op.f('ck_quote_version_valida')),
        sa.CheckConstraint("title IS NULL OR length(btrim(title)) > 0",
                           name=op.f('ck_quote_titulo_no_vacio')),  # fmt: skip
        sa.CheckConstraint("title IS NULL OR length(title) <= 120",
                           name=op.f('ck_quote_titulo_largo_valido')),  # fmt: skip
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'],
                                name=op.f('fk_quote_organization_id_organization')),  # fmt: skip
        sa.ForeignKeyConstraint(['created_by', 'organization_id'],
                                ['app_user.id', 'app_user.organization_id'],
                                name=op.f('fk_quote_created_by_app_user')),  # fmt: skip
        sa.ForeignKeyConstraint(['closed_by', 'organization_id'],
                                ['app_user.id', 'app_user.organization_id'],
                                name=op.f('fk_quote_closed_by_app_user')),  # fmt: skip
        sa.PrimaryKeyConstraint('id', name=op.f('pk_quote')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_quote_id')),
    )
    op.create_index('ix_quote_org_updated', 'quote', ['organization_id', 'updated_at'],
                    unique=False)  # fmt: skip

    op.create_table(
        'quote_cart',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('quote_id', sa.Uuid(), nullable=False),
        sa.Column('supplier_id', sa.Uuid(), nullable=False),
        sa.Column('status', sa.String(length=10), server_default='active', nullable=False),
        sa.Column('dismissed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dismissed_by', sa.Uuid(), nullable=True),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified',
                  nullable=False),  # fmt: skip
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.CheckConstraint("status IN ('active','dismissed')",
                           name=op.f('ck_quote_cart_estado_valido')),  # fmt: skip
        # Descartado exige autor y fecha; activo no los conserva (reincorporar los limpia).
        sa.CheckConstraint(
            "(status = 'dismissed') = "
            "(dismissed_at IS NOT NULL AND dismissed_by IS NOT NULL)",
            name=op.f('ck_quote_cart_descarte_coherente'),
        ),
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name=op.f('ck_quote_cart_origen_dato_valido'),
        ),
        sa.CheckConstraint('version >= 1', name=op.f('ck_quote_cart_version_valida')),
        sa.ForeignKeyConstraint(['quote_id', 'organization_id'],
                                ['quote.id', 'quote.organization_id'],
                                name=op.f('fk_quote_cart_quote_id_quote')),  # fmt: skip
        sa.ForeignKeyConstraint(['supplier_id', 'organization_id'],
                                ['supplier.id', 'supplier.organization_id'],
                                name=op.f('fk_quote_cart_supplier_id_supplier')),  # fmt: skip
        sa.ForeignKeyConstraint(['dismissed_by', 'organization_id'],
                                ['app_user.id', 'app_user.organization_id'],
                                name=op.f('fk_quote_cart_dismissed_by_app_user')),  # fmt: skip
        sa.PrimaryKeyConstraint('id', name=op.f('pk_quote_cart')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_quote_cart_id')),
        # Clave compuesta para que `quote_line` no pueda quedar en un carrito de otro proveedor.
        sa.UniqueConstraint('id', 'supplier_id', 'organization_id', name='uq_quote_cart_supplier'),
        # Un carrito por proveedor y cotización.
        sa.UniqueConstraint('quote_id', 'supplier_id', name='uq_quote_cart_quote_supplier'),
    )
    op.create_index('ix_quote_cart_quote', 'quote_cart', ['organization_id', 'quote_id'],
                    unique=False)  # fmt: skip

    op.create_table(
        'quote_line',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('quote_cart_id', sa.Uuid(), nullable=False),
        sa.Column('supplier_id', sa.Uuid(), nullable=False),
        sa.Column('offer_snapshot_id', sa.Uuid(), nullable=False),
        sa.Column('trade_item_id', sa.Uuid(), nullable=False),
        sa.Column('quantity', sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column('quantity_unit', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=10), server_default='active', nullable=False),
        sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('removed_by', sa.Uuid(), nullable=True),
        sa.Column('demand_item_id', sa.Uuid(), nullable=True),
        sa.Column('coverage_relation', sa.String(length=20), nullable=True),
        sa.Column('pending_closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('pending_closed_by', sa.Uuid(), nullable=True),
        sa.Column('pending_closed_note', sa.Text(), nullable=True),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified',
                  nullable=False),  # fmt: skip
        sa.Column('added_by', sa.Uuid(), nullable=False),
        sa.Column('added_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.CheckConstraint('quantity > 0', name=op.f('ck_quote_line_cantidad_positiva')),
        # E5 exige entero positivo: una línea es una selección explícita, no una estimación.
        sa.CheckConstraint('quantity = trunc(quantity)',
                           name=op.f('ck_quote_line_cantidad_entera')),  # fmt: skip
        sa.CheckConstraint("quantity_unit IN ('box','package','unit','bottle')",
                           name=op.f('ck_quote_line_unidad_valida')),  # fmt: skip
        sa.CheckConstraint("status IN ('active','removed')",
                           name=op.f('ck_quote_line_estado_valido')),  # fmt: skip
        sa.CheckConstraint(
            "(status = 'removed') = (removed_at IS NOT NULL AND removed_by IS NOT NULL)",
            name=op.f('ck_quote_line_retiro_coherente'),
        ),
        # Vínculo con faltante y clasificación congelada: ambos o ninguno.
        sa.CheckConstraint(
            "(demand_item_id IS NULL) = (coverage_relation IS NULL)",
            name=op.f('ck_quote_line_vinculo_coherente'),
        ),
        sa.CheckConstraint(
            "coverage_relation IS NULL OR coverage_relation IN "
            "('exact','other_presentation','combined','related','needs_review')",
            name=op.f('ck_quote_line_relacion_valida'),
        ),
        # Cerrar el pendiente exige autor y fecha; la nota es opcional y solo con cierre.
        sa.CheckConstraint(
            "(pending_closed_at IS NULL) = (pending_closed_by IS NULL)",
            name=op.f('ck_quote_line_pendiente_cierre_coherente'),
        ),
        sa.CheckConstraint(
            "pending_closed_note IS NULL OR pending_closed_at IS NOT NULL",
            name=op.f('ck_quote_line_nota_pendiente_con_cierre'),
        ),
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name=op.f('ck_quote_line_origen_dato_valido'),
        ),
        sa.CheckConstraint('version >= 1', name=op.f('ck_quote_line_version_valida')),
        sa.ForeignKeyConstraint(
            ['quote_cart_id', 'supplier_id', 'organization_id'],
            ['quote_cart.id', 'quote_cart.supplier_id', 'quote_cart.organization_id'],
            name=op.f('fk_quote_line_quote_cart_id_quote_cart'),
        ),
        sa.ForeignKeyConstraint(['offer_snapshot_id', 'organization_id'],
                                ['offer_snapshot.id', 'offer_snapshot.organization_id'],
                                name=op.f('fk_quote_line_offer_snapshot_id_offer_snapshot')),  # fmt: skip
        sa.ForeignKeyConstraint(['trade_item_id', 'organization_id'],
                                ['trade_item.id', 'trade_item.organization_id'],
                                name=op.f('fk_quote_line_trade_item_id_trade_item')),  # fmt: skip
        # La línea y el faltante que ayuda a cubrir viven en la misma organización.
        sa.ForeignKeyConstraint(['demand_item_id', 'organization_id'],
                                ['demand_item.id', 'demand_item.organization_id'],
                                name=op.f('fk_quote_line_demand_item_id_demand_item')),  # fmt: skip
        sa.ForeignKeyConstraint(['added_by', 'organization_id'],
                                ['app_user.id', 'app_user.organization_id'],
                                name=op.f('fk_quote_line_added_by_app_user')),  # fmt: skip
        sa.ForeignKeyConstraint(['removed_by', 'organization_id'],
                                ['app_user.id', 'app_user.organization_id'],
                                name=op.f('fk_quote_line_removed_by_app_user')),  # fmt: skip
        sa.ForeignKeyConstraint(['pending_closed_by', 'organization_id'],
                                ['app_user.id', 'app_user.organization_id'],
                                name=op.f('fk_quote_line_pending_closed_by_app_user')),  # fmt: skip
        sa.PrimaryKeyConstraint('id', name=op.f('pk_quote_line')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_quote_line_id')),
    )
    # Dos selecciones activas de la misma oferta en el mismo carrito son la misma línea.
    op.create_index(
        'uq_quote_line_active_offer',
        'quote_line',
        ['quote_cart_id', 'offer_snapshot_id'],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index('ix_quote_line_demand', 'quote_line', ['organization_id', 'demand_item_id'],
                    unique=False)  # fmt: skip
    op.create_index('ix_quote_line_cart', 'quote_line', ['quote_cart_id', 'status'],
                    unique=False)  # fmt: skip

    # ------------------------------------------------------------------ observaciones (O1)
    op.create_table(
        'commercial_observation',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('text_original', sa.Text(), nullable=False),
        sa.Column('author_id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column('quote_id', sa.Uuid(), nullable=True),
        sa.Column('quote_line_id', sa.Uuid(), nullable=True),
        sa.Column('trade_item_id', sa.Uuid(), nullable=False),
        sa.Column('offer_snapshot_id', sa.Uuid(), nullable=True),
        sa.Column('supplier_id', sa.Uuid(), nullable=True),
        sa.Column('context', JSONB, server_default=EMPTY_OBJECT, nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='captured',
                  nullable=False),  # fmt: skip
        # Texto libre, 1–2000 caracteres. Se guarda tal como se escribió (sin normalizar).
        sa.CheckConstraint(
            "length(btrim(text_original)) > 0 AND length(text_original) <= 2000",
            name=op.f('ck_commercial_observation_texto_valido'),
        ),
        sa.CheckConstraint("jsonb_typeof(context) = 'object'",
                           name=op.f('ck_commercial_observation_contexto_valido')),  # fmt: skip
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name=op.f('ck_commercial_observation_origen_dato_valido'),
        ),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'],
                                name=op.f('fk_commercial_observation_organization_id_organization')),  # fmt: skip
        sa.ForeignKeyConstraint(['author_id', 'organization_id'],
                                ['app_user.id', 'app_user.organization_id'],
                                name=op.f('fk_commercial_observation_author_id_app_user')),  # fmt: skip
        sa.ForeignKeyConstraint(['quote_id', 'organization_id'],
                                ['quote.id', 'quote.organization_id'],
                                name=op.f('fk_commercial_observation_quote_id_quote')),  # fmt: skip
        sa.ForeignKeyConstraint(['quote_line_id', 'organization_id'],
                                ['quote_line.id', 'quote_line.organization_id'],
                                name=op.f('fk_commercial_observation_quote_line_id_quote_line')),  # fmt: skip
        sa.ForeignKeyConstraint(['trade_item_id', 'organization_id'],
                                ['trade_item.id', 'trade_item.organization_id'],
                                name=op.f('fk_commercial_observation_trade_item_id_trade_item')),  # fmt: skip
        sa.ForeignKeyConstraint(['offer_snapshot_id', 'organization_id'],
                                ['offer_snapshot.id', 'offer_snapshot.organization_id'],
                                name=op.f('fk_commercial_observation_offer_snapshot_id_offer_snapshot')),  # fmt: skip
        sa.ForeignKeyConstraint(['supplier_id', 'organization_id'],
                                ['supplier.id', 'supplier.organization_id'],
                                name=op.f('fk_commercial_observation_supplier_id_supplier')),  # fmt: skip
        sa.PrimaryKeyConstraint('id', name=op.f('pk_commercial_observation')),
        sa.UniqueConstraint('id', 'organization_id',
                            name=op.f('uq_commercial_observation_id')),  # fmt: skip
    )
    op.create_index('ix_commercial_observation_item', 'commercial_observation',
                    ['organization_id', 'trade_item_id', 'created_at'], unique=False)  # fmt: skip
    op.create_index('ix_commercial_observation_line', 'commercial_observation',
                    ['quote_line_id', 'created_at'], unique=False)  # fmt: skip

    # Una observación comercial no se edita ni se borra (§2.7, V-20).
    op.execute(
        "CREATE TRIGGER commercial_observation_append_only "
        "BEFORE UPDATE OR DELETE ON commercial_observation "
        "FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS commercial_observation_append_only ON commercial_observation"
    )
    op.drop_table('commercial_observation')
    op.drop_table('quote_line')
    op.drop_table('quote_cart')
    op.drop_table('quote')
