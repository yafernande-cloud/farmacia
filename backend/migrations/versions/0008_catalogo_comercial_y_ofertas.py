"""catalogo comercial, fichas de cadena, precios publicos y ofertas de proveedores

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-29 12:00:00.000000

Crea las 10 tablas del diseño de C1 (D-076, D-094 y la adenda `chain_product`):

  trade_item, trade_item_alias, supplier, source, supplier_product_mapping,
  shipping_terms_snapshot, offer_snapshot, chain_product, reference_price, import_run

Planos que NO se mezclan:
  * `trade_item` es el artículo canónico (presentación comercial) y lo fija la farmacia.
  * `chain_product` es la ficha que publica una cadena (`chain_reference`) y solo refleja lo publicado.
  * `reference_price` es una observación fechada del precio público de UNA ficha concreta
    (`chain_product_id` obligatorio). No tiene `trade_item_id`: el vínculo vive solo en la ficha,
    de modo que precio, foto y descripción no pueden desincronizarse. Nunca es oferta ni entra en
    totales (D-084).
  * `offer_snapshot` es la observación de un SKU de proveedor y es la única base de comparaciones.

Reglas de integridad en la base:
  * FK compuestas `(x_id, organization_id)`; claves auxiliares `source(id, supplier_id, organization_id)`
    y `chain_product(id, source_id, organization_id)` para impedir cruces de proveedor o de cadena.
  * `enforce_source_kind`: una ficha o un precio público solo pueden colgar de una fuente
    `chain_reference`; mappings, despacho y ofertas, solo de fuentes de proveedor.
  * `enforce_data_origin`: la procedencia debe ser coherente con el tipo de organización.
  * Snapshots inmutables con `forbid_mutation()`; en `chain_product` el trigger permite cambiar solo
    las columnas del vínculo y exige subir `version` en 1.
  * `import_run` se inserta una sola vez con su estado final.

No modifica tablas existentes ni las del módulo de faltantes.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        'trade_item',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('display_name', sa.String(length=300), nullable=False),
        sa.Column('identity_key', sa.String(length=400), nullable=False),
        sa.Column('product_kind', sa.String(length=20), nullable=True),
        sa.Column('ingredient', sa.String(length=200), nullable=True),
        sa.Column('attributes', jsonb, server_default=sa.text('\'{"schema_version": 2}\'::jsonb'), nullable=False),
        sa.Column('laboratory', sa.String(length=200), nullable=True),
        sa.Column('brand', sa.String(length=200), nullable=True),
        sa.Column('gtin', sa.String(length=14), nullable=True),
        sa.Column('sanitary_registration', sa.String(length=30), nullable=True),
        sa.Column('pharma_identity_id', sa.Uuid(), nullable=True),
        sa.Column('status', sa.String(length=10), server_default='active', nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified', nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('active','inactive')", name=op.f('ck_trade_item_estado_valido')),
        sa.CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name=op.f('ck_trade_item_origen_dato_valido')),
        sa.CheckConstraint(
            "product_kind IS NULL OR product_kind IN ('medicine','medical_device','cosmetic',"
            "'personal_care','veterinary','food_supplement','other')",
            name=op.f('ck_trade_item_tipo_producto_valido'),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attributes) = 'object' AND attributes ? 'schema_version'",
            name=op.f('ck_trade_item_atributos_validos'),
        ),
        sa.CheckConstraint("gtin IS NULL OR gtin ~ '^[0-9]{8,14}$'", name=op.f('ck_trade_item_gtin_valido')),
        sa.CheckConstraint('version >= 1', name=op.f('ck_trade_item_version_valida')),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_trade_item_organization_id_organization')),
        sa.ForeignKeyConstraint(['pharma_identity_id', 'organization_id'], ['pharma_identity.id', 'pharma_identity.organization_id'], name=op.f('fk_trade_item_pharma_identity_id_pharma_identity')),
        sa.ForeignKeyConstraint(['created_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_trade_item_created_by_app_user')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_trade_item')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_trade_item_id')),
        sa.UniqueConstraint('organization_id', 'identity_key', name=op.f('uq_trade_item_organization_id')),
    )
    op.create_index('ix_trade_item_org_status', 'trade_item', ['organization_id', 'status'], unique=False)
    op.create_index('ix_trade_item_ingredient', 'trade_item', ['organization_id', 'ingredient'], unique=False)

    op.create_table(
        'trade_item_alias',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('trade_item_id', sa.Uuid(), nullable=False),
        sa.Column('alias_text', sa.String(length=300), nullable=False),
        sa.Column('normalized_alias', sa.String(length=300), nullable=False),
        sa.Column('source', sa.String(length=10), server_default='manual', nullable=False),
        sa.Column('status', sa.String(length=10), server_default='active', nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified', nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('active','inactive')", name=op.f('ck_trade_item_alias_estado_valido')),
        sa.CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name=op.f('ck_trade_item_alias_origen_dato_valido')),
        sa.CheckConstraint("source IN ('manual','supplier','import','chain')", name=op.f('ck_trade_item_alias_origen_valido')),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_trade_item_alias_organization_id_organization')),
        sa.ForeignKeyConstraint(['trade_item_id', 'organization_id'], ['trade_item.id', 'trade_item.organization_id'], name=op.f('fk_trade_item_alias_trade_item_id_trade_item')),
        sa.ForeignKeyConstraint(['created_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_trade_item_alias_created_by_app_user')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_trade_item_alias')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_trade_item_alias_id')),
        sa.UniqueConstraint('organization_id', 'trade_item_id', 'normalized_alias', name=op.f('uq_trade_item_alias_organization_id')),
    )
    op.create_index('ix_trade_item_alias_lookup', 'trade_item_alias', ['organization_id', 'normalized_alias'], unique=False)

    op.create_table(
        'supplier',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('supplier_code', sa.String(length=60), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=10), server_default='active', nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified', nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('active','inactive')", name=op.f('ck_supplier_estado_valido')),
        sa.CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name=op.f('ck_supplier_origen_dato_valido')),
        sa.CheckConstraint('version >= 1', name=op.f('ck_supplier_version_valida')),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_supplier_organization_id_organization')),
        sa.ForeignKeyConstraint(['created_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_supplier_created_by_app_user')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_supplier')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_supplier_id')),
        sa.UniqueConstraint('organization_id', 'name', name=op.f('uq_supplier_organization_id')),
    )

    op.create_table(
        'source',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('supplier_id', sa.Uuid(), nullable=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('mode', sa.String(length=16), server_default='none', nullable=False),
        sa.Column('capabilities', jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(length=300), nullable=True),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified', nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("kind IN ('supplier','chain_reference')", name=op.f('ck_source_tipo_valido')),
        sa.CheckConstraint("mode IN ('api','manual_import','fixture','none')", name=op.f('ck_source_modo_valido')),
        sa.CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name=op.f('ck_source_origen_dato_valido')),
        sa.CheckConstraint("(kind = 'supplier') = (supplier_id IS NOT NULL)", name=op.f('ck_source_proveedor_coherente')),
        sa.CheckConstraint("jsonb_typeof(capabilities) = 'array'", name=op.f('ck_source_capacidades_validas')),
        sa.CheckConstraint(
            "capabilities <@ '[\"search\",\"product_detail\",\"price\",\"stock\",\"terms\"]'::jsonb",
            name=op.f('ck_source_capacidades_permitidas'),
        ),
        sa.CheckConstraint('version >= 1', name=op.f('ck_source_version_valida')),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_source_organization_id_organization')),
        sa.ForeignKeyConstraint(['supplier_id', 'organization_id'], ['supplier.id', 'supplier.organization_id'], name=op.f('fk_source_supplier_id_supplier')),
        sa.ForeignKeyConstraint(['created_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_source_created_by_app_user')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_source')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_source_id')),
        # Clave auxiliar: una oferta o un mapping no pueden declarar otro proveedor que su fuente.
        sa.UniqueConstraint('id', 'supplier_id', 'organization_id', name='uq_source_id_supplier'),
        sa.UniqueConstraint('organization_id', 'name', name=op.f('uq_source_organization_id')),
    )
    op.create_index('ix_source_kind', 'source', ['organization_id', 'kind'], unique=False)

    op.create_table(
        'supplier_product_mapping',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('source_id', sa.Uuid(), nullable=False),
        sa.Column('supplier_id', sa.Uuid(), nullable=False),
        sa.Column('trade_item_id', sa.Uuid(), nullable=False),
        sa.Column('external_id', sa.String(length=120), nullable=False),
        sa.Column('external_name', sa.String(length=300), nullable=True),
        sa.Column('origin', sa.String(length=12), server_default='explicit', nullable=False),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified', nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('confirmed_by', sa.Uuid(), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("origin IN ('explicit','confirmed')", name=op.f('ck_supplier_product_mapping_origen_valido')),
        sa.CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name=op.f('ck_supplier_product_mapping_origen_dato_valido')),
        sa.CheckConstraint("(origin = 'confirmed') = (confirmed_by IS NOT NULL)", name=op.f('ck_supplier_product_mapping_confirmacion_autor_valida')),
        sa.CheckConstraint("(confirmed_by IS NULL) = (confirmed_at IS NULL)", name=op.f('ck_supplier_product_mapping_confirmacion_fecha_valida')),
        sa.CheckConstraint('version >= 1', name=op.f('ck_supplier_product_mapping_version_valida')),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_supplier_product_mapping_organization_id_organization')),
        sa.ForeignKeyConstraint(['source_id', 'supplier_id', 'organization_id'], ['source.id', 'source.supplier_id', 'source.organization_id'], name=op.f('fk_supplier_product_mapping_source_id_source')),
        sa.ForeignKeyConstraint(['trade_item_id', 'organization_id'], ['trade_item.id', 'trade_item.organization_id'], name=op.f('fk_supplier_product_mapping_trade_item_id_trade_item')),
        sa.ForeignKeyConstraint(['created_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_supplier_product_mapping_created_by_app_user')),
        sa.ForeignKeyConstraint(['confirmed_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_supplier_product_mapping_confirmed_by_app_user')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_supplier_product_mapping')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_supplier_product_mapping_id')),
        sa.UniqueConstraint('organization_id', 'source_id', 'external_id', name=op.f('uq_supplier_product_mapping_organization_id')),
    )
    op.create_index('ix_supplier_product_mapping_item', 'supplier_product_mapping', ['organization_id', 'trade_item_id'], unique=False)

    op.create_table(
        'shipping_terms_snapshot',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('supplier_id', sa.Uuid(), nullable=False),
        sa.Column('source_id', sa.Uuid(), nullable=True),
        sa.Column('scheme', sa.String(length=20), nullable=False),
        sa.Column('flat_amount', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('threshold_amount', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('evidence', jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("scheme IN ('free','flat','free_over_threshold')", name=op.f('ck_shipping_terms_snapshot_esquema_valido')),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f('ck_shipping_terms_snapshot_moneda_valida')),
        sa.CheckConstraint('flat_amount IS NULL OR flat_amount >= 0', name=op.f('ck_shipping_terms_snapshot_monto_no_negativo')),
        sa.CheckConstraint('threshold_amount IS NULL OR threshold_amount >= 0', name=op.f('ck_shipping_terms_snapshot_umbral_no_negativo')),
        sa.CheckConstraint(
            "scheme <> 'free' OR (flat_amount IS NULL AND threshold_amount IS NULL)",
            name=op.f('ck_shipping_terms_snapshot_gratis_sin_montos'),
        ),
        sa.CheckConstraint("scheme <> 'flat' OR flat_amount IS NOT NULL", name=op.f('ck_shipping_terms_snapshot_fijo_con_monto')),
        sa.CheckConstraint(
            "scheme <> 'free_over_threshold' OR (flat_amount IS NOT NULL AND threshold_amount IS NOT NULL)",
            name=op.f('ck_shipping_terms_snapshot_umbral_con_monto'),
        ),
        sa.CheckConstraint(
            'valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from',
            name=op.f('ck_shipping_terms_snapshot_vigencia_valida'),
        ),
        sa.CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name=op.f('ck_shipping_terms_snapshot_origen_dato_valido')),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_shipping_terms_snapshot_organization_id_organization')),
        sa.ForeignKeyConstraint(['supplier_id', 'organization_id'], ['supplier.id', 'supplier.organization_id'], name=op.f('fk_shipping_terms_snapshot_supplier_id_supplier')),
        sa.ForeignKeyConstraint(['source_id', 'supplier_id', 'organization_id'], ['source.id', 'source.supplier_id', 'source.organization_id'], name=op.f('fk_shipping_terms_snapshot_source_id_source')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_shipping_terms_snapshot')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_shipping_terms_snapshot_id')),
    )
    op.create_index('ix_shipping_terms_supplier', 'shipping_terms_snapshot', ['organization_id', 'supplier_id', 'observed_at'], unique=False)

    op.create_table(
        'offer_snapshot',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('supplier_id', sa.Uuid(), nullable=False),
        sa.Column('source_id', sa.Uuid(), nullable=False),
        sa.Column('trade_item_id', sa.Uuid(), nullable=False),
        sa.Column('external_id', sa.String(length=120), nullable=False),
        sa.Column('external_id_kind', sa.String(length=20), server_default='supplier_sku', nullable=False),
        sa.Column('external_name', sa.String(length=300), nullable=True),
        sa.Column('price_amount', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('price_basis', sa.String(length=24), server_default='per_commercial_unit', nullable=False),
        sa.Column('tax_included', sa.Boolean(), nullable=True),
        sa.Column('discount_included', sa.Boolean(), nullable=True),
        sa.Column('sale_unit', sa.String(length=10), nullable=False),
        sa.Column('sale_unit_text', sa.String(length=40), nullable=True),
        sa.Column('pack_units_per_commercial_unit', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('pack_base_unit', sa.String(length=10), nullable=True),
        sa.Column('pack_text', sa.String(length=60), nullable=True),
        sa.Column('minimum_known', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('minimum_quantity', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('minimum_unit', sa.String(length=10), nullable=True),
        sa.Column('multiple_known', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('multiple_quantity', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('multiple_unit', sa.String(length=10), nullable=True),
        sa.Column('stock_status', sa.String(length=10), server_default='unknown', nullable=False),
        sa.Column('stock_quantity', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('stock_unit', sa.String(length=10), nullable=True),
        sa.Column('taxes', jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('discounts', jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('shipping_terms_id', sa.Uuid(), nullable=True),
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('evidence', jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("external_id_kind IN ('supplier_sku')", name=op.f('ck_offer_snapshot_tipo_externo_valido')),
        sa.CheckConstraint("price_basis IN ('per_commercial_unit','per_base_unit')", name=op.f('ck_offer_snapshot_base_precio_valida')),
        sa.CheckConstraint('price_amount >= 0', name=op.f('ck_offer_snapshot_precio_no_negativo')),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f('ck_offer_snapshot_moneda_valida')),
        sa.CheckConstraint("sale_unit IN ('box','package','unit','bottle')", name=op.f('ck_offer_snapshot_unidad_venta_valida')),
        sa.CheckConstraint("minimum_unit IS NULL OR minimum_unit IN ('box','package','unit','bottle')", name=op.f('ck_offer_snapshot_unidad_minimo_valida')),
        sa.CheckConstraint("multiple_unit IS NULL OR multiple_unit IN ('box','package','unit','bottle')", name=op.f('ck_offer_snapshot_unidad_multiplo_valida')),
        sa.CheckConstraint("stock_unit IS NULL OR stock_unit IN ('box','package','unit','bottle')", name=op.f('ck_offer_snapshot_unidad_stock_valida')),
        sa.CheckConstraint("stock_status IN ('known','unknown')", name=op.f('ck_offer_snapshot_estado_stock_valido')),
        sa.CheckConstraint("(stock_status = 'known') = (stock_quantity IS NOT NULL)", name=op.f('ck_offer_snapshot_stock_coherente')),
        sa.CheckConstraint('(stock_quantity IS NULL) = (stock_unit IS NULL)', name=op.f('ck_offer_snapshot_stock_con_unidad')),
        sa.CheckConstraint('(minimum_known) = (minimum_quantity IS NOT NULL)', name=op.f('ck_offer_snapshot_minimo_coherente')),
        sa.CheckConstraint('(minimum_quantity IS NULL) = (minimum_unit IS NULL)', name=op.f('ck_offer_snapshot_minimo_con_unidad')),
        sa.CheckConstraint('minimum_quantity IS NULL OR minimum_quantity >= 0', name=op.f('ck_offer_snapshot_minimo_no_negativo')),
        sa.CheckConstraint('(multiple_known) = (multiple_quantity IS NOT NULL)', name=op.f('ck_offer_snapshot_multiplo_coherente')),
        sa.CheckConstraint('(multiple_quantity IS NULL) = (multiple_unit IS NULL)', name=op.f('ck_offer_snapshot_multiplo_con_unidad')),
        sa.CheckConstraint('multiple_quantity IS NULL OR multiple_quantity >= 1', name=op.f('ck_offer_snapshot_multiplo_positivo')),
        sa.CheckConstraint(
            '(pack_units_per_commercial_unit IS NULL) = (pack_base_unit IS NULL)',
            name=op.f('ck_offer_snapshot_pack_coherente'),
        ),
        sa.CheckConstraint(
            'pack_units_per_commercial_unit IS NULL OR pack_units_per_commercial_unit > 0',
            name=op.f('ck_offer_snapshot_pack_positivo'),
        ),
        sa.CheckConstraint("jsonb_typeof(taxes) = 'array'", name=op.f('ck_offer_snapshot_impuestos_validos')),
        sa.CheckConstraint("jsonb_typeof(discounts) = 'array'", name=op.f('ck_offer_snapshot_descuentos_validos')),
        sa.CheckConstraint(
            'valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from',
            name=op.f('ck_offer_snapshot_vigencia_valida'),
        ),
        sa.CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name=op.f('ck_offer_snapshot_origen_dato_valido')),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_offer_snapshot_organization_id_organization')),
        sa.ForeignKeyConstraint(['supplier_id', 'organization_id'], ['supplier.id', 'supplier.organization_id'], name=op.f('fk_offer_snapshot_supplier_id_supplier')),
        sa.ForeignKeyConstraint(['source_id', 'supplier_id', 'organization_id'], ['source.id', 'source.supplier_id', 'source.organization_id'], name=op.f('fk_offer_snapshot_source_id_source')),
        sa.ForeignKeyConstraint(['trade_item_id', 'organization_id'], ['trade_item.id', 'trade_item.organization_id'], name=op.f('fk_offer_snapshot_trade_item_id_trade_item')),
        sa.ForeignKeyConstraint(['shipping_terms_id', 'organization_id'], ['shipping_terms_snapshot.id', 'shipping_terms_snapshot.organization_id'], name=op.f('fk_offer_snapshot_shipping_terms_id_shipping_terms_snapshot')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_offer_snapshot')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_offer_snapshot_id')),
    )
    op.create_index('ix_offer_snapshot_item', 'offer_snapshot', ['organization_id', 'trade_item_id', 'observed_at'], unique=False)
    op.create_index('ix_offer_snapshot_supplier', 'offer_snapshot', ['organization_id', 'supplier_id', 'observed_at'], unique=False)

    op.create_table(
        'chain_product',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('source_id', sa.Uuid(), nullable=False),
        sa.Column('external_id', sa.String(length=120), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('published_name', sa.String(length=300), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('laboratory_as_published', sa.String(length=200), nullable=True),
        sa.Column('presentation_as_published', sa.String(length=300), nullable=True),
        sa.Column('published_attributes', jsonb, server_default=sa.text('\'{"schema_version": 2}\'::jsonb'), nullable=False),
        sa.Column('image_url', sa.Text(), nullable=True),
        sa.Column('image_hash', sa.String(length=64), nullable=True),
        sa.Column('image_object_key', sa.String(length=500), nullable=True),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('trade_item_id', sa.Uuid(), nullable=True),
        sa.Column('match_status', sa.String(length=16), server_default='unmapped', nullable=False),
        sa.Column('match_candidates', jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('mapping_origin', sa.String(length=12), nullable=True),
        sa.Column('mapped_by', sa.Uuid(), nullable=True),
        sa.Column('mapped_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('version', sa.Integer(), server_default='1', nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('raw_payload', jsonb, nullable=True),
        sa.Column('evidence', jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("match_status IN ('exact','needs_review','unmapped')", name=op.f('ck_chain_product_estado_match_valido')),
        sa.CheckConstraint(
            "mapping_origin IS NULL OR mapping_origin IN ('explicit','confirmed')",
            name=op.f('ck_chain_product_origen_vinculo_valido'),
        ),
        sa.CheckConstraint('(trade_item_id IS NULL) = (mapping_origin IS NULL)', name=op.f('ck_chain_product_vinculo_coherente')),
        sa.CheckConstraint('(mapping_origin IS NULL) = (mapped_by IS NULL)', name=op.f('ck_chain_product_vinculo_autor_valido')),
        sa.CheckConstraint('(mapping_origin IS NULL) = (mapped_at IS NULL)', name=op.f('ck_chain_product_vinculo_fecha_valida')),
        sa.CheckConstraint(
            "mapping_origin <> 'confirmed' OR (mapped_by IS NOT NULL AND mapped_at IS NOT NULL)",
            name=op.f('ck_chain_product_confirmacion_completa'),
        ),
        sa.CheckConstraint("(match_status = 'exact') = (trade_item_id IS NOT NULL)", name=op.f('ck_chain_product_match_coherente')),
        sa.CheckConstraint("jsonb_typeof(published_attributes) = 'object'", name=op.f('ck_chain_product_atributos_validos')),
        sa.CheckConstraint("jsonb_typeof(match_candidates) = 'array'", name=op.f('ck_chain_product_candidatos_validos')),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name=op.f('ck_chain_product_hash_contenido_valido')),
        sa.CheckConstraint("image_hash IS NULL OR image_hash ~ '^[0-9a-f]{64}$'", name=op.f('ck_chain_product_hash_imagen_valido')),
        sa.CheckConstraint("image_url IS NULL OR image_url ~ '^https?://'", name=op.f('ck_chain_product_url_imagen_valida')),
        sa.CheckConstraint("source_url ~ '^https?://'", name=op.f('ck_chain_product_url_ficha_valida')),
        sa.CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name=op.f('ck_chain_product_origen_dato_valido')),
        sa.CheckConstraint('version >= 1', name=op.f('ck_chain_product_version_valida')),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_chain_product_organization_id_organization')),
        sa.ForeignKeyConstraint(['source_id', 'organization_id'], ['source.id', 'source.organization_id'], name=op.f('fk_chain_product_source_id_source')),
        sa.ForeignKeyConstraint(['trade_item_id', 'organization_id'], ['trade_item.id', 'trade_item.organization_id'], name=op.f('fk_chain_product_trade_item_id_trade_item')),
        sa.ForeignKeyConstraint(['mapped_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_chain_product_mapped_by_app_user')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_chain_product')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_chain_product_id')),
        # Clave auxiliar: un precio de una cadena no puede apuntar a la ficha de otra.
        sa.UniqueConstraint('id', 'source_id', 'organization_id', name='uq_chain_product_id_source'),
        sa.UniqueConstraint('organization_id', 'source_id', 'external_id', 'content_hash', name=op.f('uq_chain_product_organization_id')),
    )
    op.create_index('ix_chain_product_external', 'chain_product', ['organization_id', 'source_id', 'external_id', 'observed_at'], unique=False)
    op.create_index('ix_chain_product_item', 'chain_product', ['organization_id', 'trade_item_id'], unique=False,
                    postgresql_where=sa.text('trade_item_id IS NOT NULL'))

    op.create_table(
        'reference_price',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('source_id', sa.Uuid(), nullable=False),
        sa.Column('chain_product_id', sa.Uuid(), nullable=False),
        sa.Column('price_normal', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('price_promo', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('conditions', jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('shipping_reported', jsonb, nullable=True),
        sa.Column('stock_reported', sa.Boolean(), nullable=True),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('evidence', jsonb, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('price_normal >= 0', name=op.f('ck_reference_price_precio_no_negativo')),
        sa.CheckConstraint('price_promo IS NULL OR price_promo >= 0', name=op.f('ck_reference_price_promocion_no_negativa')),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f('ck_reference_price_moneda_valida')),
        sa.CheckConstraint("jsonb_typeof(conditions) = 'array'", name=op.f('ck_reference_price_condiciones_validas')),
        sa.CheckConstraint("source_url ~ '^https?://'", name=op.f('ck_reference_price_url_origen_valida')),
        sa.CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name=op.f('ck_reference_price_origen_dato_valido')),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_reference_price_organization_id_organization')),
        sa.ForeignKeyConstraint(['source_id', 'organization_id'], ['source.id', 'source.organization_id'], name=op.f('fk_reference_price_source_id_source')),
        sa.ForeignKeyConstraint(
            ['chain_product_id', 'source_id', 'organization_id'],
            ['chain_product.id', 'chain_product.source_id', 'chain_product.organization_id'],
            name=op.f('fk_reference_price_chain_product_id_chain_product'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_reference_price')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_reference_price_id')),
    )
    op.create_index('ix_reference_price_product', 'reference_price', ['organization_id', 'chain_product_id', 'observed_at'], unique=False)

    op.create_table(
        'import_run',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('source_id', sa.Uuid(), nullable=False),
        sa.Column('file_name', sa.String(length=300), nullable=False),
        sa.Column('file_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False),
        sa.Column('rows_total', sa.Integer(), server_default='0', nullable=False),
        sa.Column('rows_accepted', sa.Integer(), server_default='0', nullable=False),
        sa.Column('rows_rejected', sa.Integer(), server_default='0', nullable=False),
        sa.Column('errors', jsonb, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('imported_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified', nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.CheckConstraint("status IN ('completed','completed_with_errors','failed')", name=op.f('ck_import_run_estado_valido')),
        sa.CheckConstraint("file_hash ~ '^[0-9a-f]{64}$'", name=op.f('ck_import_run_hash_archivo_valido')),
        sa.CheckConstraint(
            'rows_total >= 0 AND rows_accepted >= 0 AND rows_rejected >= 0',
            name=op.f('ck_import_run_conteos_no_negativos'),
        ),
        sa.CheckConstraint(
            'rows_accepted + rows_rejected <= rows_total', name=op.f('ck_import_run_conteos_coherentes')
        ),
        sa.CheckConstraint("jsonb_typeof(errors) = 'array'", name=op.f('ck_import_run_errores_validos')),
        sa.CheckConstraint("data_origin IN ('simulated','captured','ai_derived','unclassified')", name=op.f('ck_import_run_origen_dato_valido')),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_import_run_organization_id_organization')),
        sa.ForeignKeyConstraint(['source_id', 'organization_id'], ['source.id', 'source.organization_id'], name=op.f('fk_import_run_source_id_source')),
        sa.ForeignKeyConstraint(['created_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_import_run_created_by_app_user')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_import_run')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_import_run_id')),
        sa.UniqueConstraint('organization_id', 'source_id', 'file_hash', name=op.f('uq_import_run_organization_id')),
    )
    op.create_index('ix_import_run_source', 'import_run', ['organization_id', 'source_id', 'imported_at'], unique=False)

    # --- Snapshots inmutables (la función `forbid_mutation` es de la migración 0001)
    for table in ('offer_snapshot', 'shipping_terms_snapshot', 'reference_price', 'import_run'):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
        )

    # --- Ficha de cadena: el contenido publicado no se reescribe; el vínculo sí, con versión.
    op.execute("""
        CREATE FUNCTION forbid_chain_product_content_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'una ficha publicada no se elimina: se supera con otra versión'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
               OR NEW.source_id IS DISTINCT FROM OLD.source_id
               OR NEW.external_id IS DISTINCT FROM OLD.external_id
               OR NEW.source_url IS DISTINCT FROM OLD.source_url
               OR NEW.published_name IS DISTINCT FROM OLD.published_name
               OR NEW.description IS DISTINCT FROM OLD.description
               OR NEW.laboratory_as_published IS DISTINCT FROM OLD.laboratory_as_published
               OR NEW.presentation_as_published IS DISTINCT FROM OLD.presentation_as_published
               OR NEW.published_attributes IS DISTINCT FROM OLD.published_attributes
               OR NEW.image_url IS DISTINCT FROM OLD.image_url
               OR NEW.image_hash IS DISTINCT FROM OLD.image_hash
               OR NEW.image_object_key IS DISTINCT FROM OLD.image_object_key
               OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
               OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
               OR NEW.raw_payload IS DISTINCT FROM OLD.raw_payload
               OR NEW.evidence IS DISTINCT FROM OLD.evidence
               OR NEW.data_origin IS DISTINCT FROM OLD.data_origin
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'el contenido publicado de una ficha de cadena es inmutable: use una ficha nueva'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF (NEW.trade_item_id, NEW.match_status, NEW.mapping_origin, NEW.mapped_by)
               IS DISTINCT FROM (OLD.trade_item_id, OLD.match_status, OLD.mapping_origin, OLD.mapped_by)
               AND NEW.version <> OLD.version + 1 THEN
                RAISE EXCEPTION 'un cambio de vínculo debe subir la versión en 1 (% -> %)', OLD.version, NEW.version
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute(
        "CREATE TRIGGER chain_product_immutable BEFORE UPDATE OR DELETE ON chain_product "
        "FOR EACH ROW EXECUTE FUNCTION forbid_chain_product_content_change()"
    )

    # --- La fuente manda: ficha/precio público ⇔ cadena; mapping/despacho/oferta ⇔ proveedor.
    op.execute("""
        CREATE FUNCTION enforce_source_kind() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE k text;
        BEGIN
            SELECT kind INTO k FROM source
             WHERE id = NEW.source_id AND organization_id = NEW.organization_id;
            IF k IS NULL THEN
                RAISE EXCEPTION 'la fuente % no existe en la organización de la fila', NEW.source_id
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_TABLE_NAME IN ('chain_product', 'reference_price') THEN
                IF k <> 'chain_reference' THEN
                    RAISE EXCEPTION '% solo admite fuentes chain_reference (recibió %)', TG_TABLE_NAME, k
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSIF k <> 'supplier' THEN
                RAISE EXCEPTION '% solo admite fuentes de proveedor (recibió %)', TG_TABLE_NAME, k
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$;
    """)
    for table, cols in (
        ('chain_product', 'source_id'),
        ('reference_price', 'source_id'),
        ('supplier_product_mapping', 'source_id'),
        ('shipping_terms_snapshot', 'source_id'),
        ('offer_snapshot', 'source_id'),
    ):
        op.execute(
            f"CREATE TRIGGER {table}_source_kind BEFORE INSERT OR UPDATE OF {cols} ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION enforce_source_kind()"
        )

    # --- Procedencia coherente con el tipo de organización (función de la migración 0004).
    for table in (
        'trade_item',
        'supplier_product_mapping',
        'offer_snapshot',
        'shipping_terms_snapshot',
        'chain_product',
        'reference_price',
    ):
        op.execute(
            f"CREATE TRIGGER {table}_data_origin BEFORE INSERT OR UPDATE OF data_origin, organization_id "
            f"ON {table} FOR EACH ROW EXECUTE FUNCTION enforce_data_origin()"
        )


def downgrade() -> None:
    for table in (
        'trade_item',
        'supplier_product_mapping',
        'offer_snapshot',
        'shipping_terms_snapshot',
        'chain_product',
        'reference_price',
    ):
        op.execute(f"DROP TRIGGER {table}_data_origin ON {table}")
    for table in (
        'chain_product',
        'reference_price',
        'supplier_product_mapping',
        'shipping_terms_snapshot',
        'offer_snapshot',
    ):
        op.execute(f"DROP TRIGGER {table}_source_kind ON {table}")
    op.execute("DROP TRIGGER chain_product_immutable ON chain_product")
    op.execute("DROP FUNCTION forbid_chain_product_content_change()")
    op.execute("DROP FUNCTION enforce_source_kind()")
    for table in ('offer_snapshot', 'shipping_terms_snapshot', 'reference_price', 'import_run'):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")

    op.drop_index('ix_import_run_source', table_name='import_run')
    op.drop_table('import_run')
    op.drop_index('ix_reference_price_product', table_name='reference_price')
    op.drop_table('reference_price')
    op.drop_index('ix_chain_product_item', table_name='chain_product',
                   postgresql_where=sa.text('trade_item_id IS NOT NULL'))
    op.drop_index('ix_chain_product_external', table_name='chain_product')
    op.drop_table('chain_product')
    op.drop_index('ix_offer_snapshot_supplier', table_name='offer_snapshot')
    op.drop_index('ix_offer_snapshot_item', table_name='offer_snapshot')
    op.drop_table('offer_snapshot')
    op.drop_index('ix_shipping_terms_supplier', table_name='shipping_terms_snapshot')
    op.drop_table('shipping_terms_snapshot')
    op.drop_index('ix_supplier_product_mapping_item', table_name='supplier_product_mapping')
    op.drop_table('supplier_product_mapping')
    op.drop_index('ix_source_kind', table_name='source')
    op.drop_table('source')
    op.drop_table('supplier')
    op.drop_index('ix_trade_item_alias_lookup', table_name='trade_item_alias')
    op.drop_table('trade_item_alias')
    op.drop_index('ix_trade_item_ingredient', table_name='trade_item')
    op.drop_index('ix_trade_item_org_status', table_name='trade_item')
    op.drop_table('trade_item')
