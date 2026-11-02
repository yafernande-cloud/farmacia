"""comparaciones de ofertas y criterios comerciales (C3, E5 y E9)

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-02 18:00:00.000000

Cinco tablas del cotizador. Las cuatro de comparación son de solo agregar: una comparación no se
edita, se repite, y su gatillo `forbid_mutation()` (creado en la 0001) lo impone en la base.

  * `comparison_run`            la corrida: alcance, estado global y criterios evaluados.
  * `comparison_line`           la línea pedida (artículo + cantidad en unidades comerciales).
  * `comparison_candidate`      el par (línea, oferta) con elegibilidad, causas y costo de la línea.
  * `comparison_supplier_total` el total por proveedor (o su cota inferior) y sus bloqueos;
                                `total` existe **solo** cuando la cobertura es completa.
  * `commercial_criterion`      notas y criterios confirmados. Solo anotan: no calculan dinero.

Los desconocidos son NULL y llevan una causa tipada; nunca 0 ni 1 (D-015, D-081). Ninguna tabla de
esta migración crea ofertas ni reemplaza `offer_snapshot` como fuente de precios.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
NOW = sa.text('now()')
EMPTY_LIST = sa.text("'[]'::jsonb")


def upgrade() -> None:
    # ------------------------------------------------------------------ comparación (E5)
    op.create_table(
        'comparison_run',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('source_search_id', sa.Uuid(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified',
                  nullable=False),  # fmt: skip
        sa.Column('criteria_evaluated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('suppliers_evaluated', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('suppliers_complete', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('suppliers_incomplete', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),  # fmt: skip
        sa.Column('sources_consulted', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('sources_failed', JSONB, server_default=EMPTY_LIST, nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column('generated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "status IN ('complete','partial_coverage','no_complete_total','failed')",
            name=op.f('ck_comparison_run_estado_valido'),
        ),
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name=op.f('ck_comparison_run_origen_dato_valido'),
        ),
        sa.CheckConstraint(
            'suppliers_evaluated >= 0 AND suppliers_complete >= 0 AND suppliers_incomplete >= 0',
            name=op.f('ck_comparison_run_conteos_no_negativos'),
        ),
        # Un proveedor no se pierde entre los conteos: evaluado = completo + incompleto.
        sa.CheckConstraint(
            'suppliers_evaluated = suppliers_complete + suppliers_incomplete',
            name=op.f('ck_comparison_run_conteos_coherentes'),
        ),
        sa.CheckConstraint('sources_consulted >= 0',
                           name=op.f('ck_comparison_run_fuentes_no_negativas')),  # fmt: skip
        sa.CheckConstraint("jsonb_typeof(sources_failed) = 'array'",
                           name=op.f('ck_comparison_run_fuentes_fallidas_validas')),  # fmt: skip
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'],
                                name=op.f('fk_comparison_run_organization_id_organization')),  # fmt: skip
        sa.ForeignKeyConstraint(
            ['source_search_id', 'organization_id'],
            ['search_run.id', 'search_run.organization_id'],
            name=op.f('fk_comparison_run_source_search_id_search_run'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_comparison_run')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_comparison_run_id')),
    )
    op.create_index('ix_comparison_run_org_created', 'comparison_run',
                    ['organization_id', 'created_at'], unique=False)  # fmt: skip

    op.create_table(
        'comparison_line',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('comparison_run_id', sa.Uuid(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('article_id', sa.Uuid(), nullable=False),
        sa.Column('quantity_value', sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column('quantity_unit', sa.String(length=10), nullable=False),
        sa.Column('completeness', sa.String(length=12), nullable=False),
        sa.Column('blockers', JSONB, server_default=EMPTY_LIST, nullable=False),
        sa.CheckConstraint('position >= 0', name=op.f('ck_comparison_line_posicion_valida')),
        sa.CheckConstraint('quantity_value > 0', name=op.f('ck_comparison_line_cantidad_positiva')),
        sa.CheckConstraint("quantity_unit IN ('box','package','unit','bottle')",
                           name=op.f('ck_comparison_line_unidad_valida')),  # fmt: skip
        sa.CheckConstraint("completeness IN ('complete','partial','unavailable')",
                           name=op.f('ck_comparison_line_completeness_valido')),  # fmt: skip
        sa.CheckConstraint("jsonb_typeof(blockers) = 'array'",
                           name=op.f('ck_comparison_line_bloqueos_validos')),  # fmt: skip
        sa.ForeignKeyConstraint(
            ['comparison_run_id', 'organization_id'],
            ['comparison_run.id', 'comparison_run.organization_id'],
            name=op.f('fk_comparison_line_comparison_run_id_comparison_run'),
        ),
        sa.ForeignKeyConstraint(['article_id', 'organization_id'],
                                ['trade_item.id', 'trade_item.organization_id'],
                                name=op.f('fk_comparison_line_article_id_trade_item')),  # fmt: skip
        sa.PrimaryKeyConstraint('id', name=op.f('pk_comparison_line')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_comparison_line_id')),
        sa.UniqueConstraint('id', 'comparison_run_id', 'organization_id',
                            name='uq_comparison_line_id_run'),  # fmt: skip
        sa.UniqueConstraint('comparison_run_id', 'position',
                            name=op.f('uq_comparison_line_comparison_run_id')),  # fmt: skip
    )

    op.create_table(
        'comparison_candidate',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('comparison_run_id', sa.Uuid(), nullable=False),
        sa.Column('comparison_line_id', sa.Uuid(), nullable=False),
        sa.Column('offer_id', sa.Uuid(), nullable=False),
        sa.Column('supplier_id', sa.Uuid(), nullable=False),
        sa.Column('source_id', sa.Uuid(), nullable=False),
        sa.Column('eligibility', sa.String(length=12), nullable=False),
        sa.Column('causes', JSONB, server_default=EMPTY_LIST, nullable=False),
        sa.Column('quantity_requested', sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column('minimum_known', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('minimum_quantity', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('multiple_known', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('multiple_quantity', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('base_quantity', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('purchased', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('surplus', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('adjustment', sa.String(length=20), nullable=True),
        sa.Column('price_amount', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('price_basis', sa.String(length=24), nullable=False),
        sa.Column('tax_included', sa.Boolean(), nullable=True),
        sa.Column('discount_included', sa.Boolean(), nullable=True),
        sa.Column('pack_units_per_commercial_unit', sa.Numeric(precision=12, scale=3),
                  nullable=True),  # fmt: skip
        sa.Column('line_gross', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('line_discounts', JSONB, server_default=EMPTY_LIST, nullable=False),
        sa.Column('line_discounts_amount', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('line_tax_added', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('line_payable', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('line_net', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('discounted_is_possible', sa.Boolean(), server_default=sa.text('false'),
                  nullable=False),  # fmt: skip
        sa.Column('stock_status', sa.String(length=10), nullable=False),
        sa.Column('stock_sufficient', sa.Boolean(), nullable=True),
        sa.Column('shipping_terms_id', sa.Uuid(), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('evidence', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified',
                  nullable=False),  # fmt: skip
        sa.Column('computed_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("eligibility IN ('eligible','needs_review','ineligible')",
                           name=op.f('ck_comparison_candidate_elegibilidad_valida')),  # fmt: skip
        sa.CheckConstraint("jsonb_typeof(causes) = 'array'",
                           name=op.f('ck_comparison_candidate_causas_validas')),  # fmt: skip
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'",
                           name=op.f('ck_comparison_candidate_moneda_valida')),  # fmt: skip
        # Conocido ⇔ hay valor: un mínimo desconocido no se guarda como NULL con known=true.
        sa.CheckConstraint('(minimum_known) = (minimum_quantity IS NOT NULL)',
                           name=op.f('ck_comparison_candidate_minimo_coherente')),  # fmt: skip
        sa.CheckConstraint('(multiple_known) = (multiple_quantity IS NOT NULL)',
                           name=op.f('ck_comparison_candidate_multiplo_coherente')),  # fmt: skip
        sa.CheckConstraint('quantity_requested > 0',
                           name=op.f('ck_comparison_candidate_cantidad_positiva')),  # fmt: skip
        sa.CheckConstraint('purchased IS NULL OR purchased >= quantity_requested',
                           name=op.f('ck_comparison_candidate_compra_no_menor')),  # fmt: skip
        sa.CheckConstraint('surplus IS NULL OR surplus >= 0',
                           name=op.f('ck_comparison_candidate_excedente_no_negativo')),  # fmt: skip
        sa.CheckConstraint(
            "adjustment IS NULL OR adjustment IN "
            "('none','minimum','multiple','minimum_and_multiple')",
            name=op.f('ck_comparison_candidate_ajuste_valido'),
        ),
        sa.CheckConstraint('line_gross IS NULL OR line_gross >= 0',
                           name=op.f('ck_comparison_candidate_bruto_no_negativo')),  # fmt: skip
        sa.CheckConstraint('line_payable IS NULL OR line_payable >= 0',
                           name=op.f('ck_comparison_candidate_pago_no_negativo')),  # fmt: skip
        sa.CheckConstraint("stock_status IN ('known','unknown')",
                           name=op.f('ck_comparison_candidate_estado_stock_valido')),  # fmt: skip
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name=op.f('ck_comparison_candidate_origen_dato_valido'),
        ),
        sa.ForeignKeyConstraint(
            ['comparison_line_id', 'comparison_run_id', 'organization_id'],
            [
                'comparison_line.id',
                'comparison_line.comparison_run_id',
                'comparison_line.organization_id',
            ],
            name=op.f('fk_comparison_candidate_comparison_line_id_comparison_line'),
        ),
        sa.ForeignKeyConstraint(['offer_id', 'organization_id'],
                                ['offer_snapshot.id', 'offer_snapshot.organization_id'],
                                name=op.f('fk_comparison_candidate_offer_id_offer_snapshot')),  # fmt: skip
        sa.ForeignKeyConstraint(['supplier_id', 'organization_id'],
                                ['supplier.id', 'supplier.organization_id'],
                                name=op.f('fk_comparison_candidate_supplier_id_supplier')),  # fmt: skip
        sa.ForeignKeyConstraint(['source_id', 'organization_id'],
                                ['source.id', 'source.organization_id'],
                                name=op.f('fk_comparison_candidate_source_id_source')),  # fmt: skip
        sa.PrimaryKeyConstraint('id', name=op.f('pk_comparison_candidate')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_comparison_candidate_id')),
    )
    op.create_index('ix_comparison_candidate_line', 'comparison_candidate',
                    ['comparison_line_id'], unique=False)  # fmt: skip
    op.create_index('ix_comparison_candidate_supplier', 'comparison_candidate',
                    ['comparison_run_id', 'supplier_id'], unique=False)  # fmt: skip

    op.create_table(
        'comparison_supplier_total',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('comparison_run_id', sa.Uuid(), nullable=False),
        sa.Column('supplier_id', sa.Uuid(), nullable=False),
        sa.Column('completeness', sa.String(length=10), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=True),
        sa.Column('lines_covered', JSONB, server_default=EMPTY_LIST, nullable=False),
        sa.Column('lines_missing', JSONB, server_default=EMPTY_LIST, nullable=False),
        sa.Column('subtotal', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('taxes_included', sa.Boolean(), nullable=True),
        sa.Column('taxes_added', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('discounts_total', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('shipping_scheme', sa.String(length=20), nullable=True),
        sa.Column('shipping_known', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('shipping_amount', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('shipping_terms_id', sa.Uuid(), nullable=True),
        sa.Column('other_charges', JSONB, server_default=EMPTY_LIST, nullable=False),
        sa.Column('total', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('lower_bound', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('bound_status', sa.String(length=16), server_default='not_boundable',
                  nullable=False),  # fmt: skip
        sa.Column('bound_invalid_reasons', JSONB, server_default=EMPTY_LIST, nullable=False),
        sa.Column('blockers', JSONB, server_default=EMPTY_LIST, nullable=False),
        sa.Column('causes', JSONB, server_default=EMPTY_LIST, nullable=False),
        sa.Column('freshness', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('computed_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("completeness IN ('complete','partial')",
                           name=op.f('ck_comparison_supplier_total_completeness_valido')),  # fmt: skip
        sa.CheckConstraint("bound_status IN ('valid','not_boundable')",
                           name=op.f('ck_comparison_supplier_total_cota_estado_valido')),  # fmt: skip
        # Un proveedor parcial NUNCA tiene total: la cota inferior no es un total.
        sa.CheckConstraint("(completeness = 'complete') = (total IS NOT NULL)",
                           name=op.f('ck_comparison_supplier_total_total_solo_completo')),  # fmt: skip
        sa.CheckConstraint("(bound_status = 'valid') = (lower_bound IS NOT NULL)",
                           name=op.f('ck_comparison_supplier_total_cota_coherente')),  # fmt: skip
        sa.CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'",
                           name=op.f('ck_comparison_supplier_total_moneda_valida')),  # fmt: skip
        sa.CheckConstraint("jsonb_typeof(blockers) = 'array'",
                           name=op.f('ck_comparison_supplier_total_bloqueos_validos')),  # fmt: skip
        sa.CheckConstraint("jsonb_typeof(causes) = 'array'",
                           name=op.f('ck_comparison_supplier_total_causas_validas')),  # fmt: skip
        sa.CheckConstraint("jsonb_typeof(bound_invalid_reasons) = 'array'",
                           name=op.f('ck_comparison_supplier_total_motivos_cota_validos')),  # fmt: skip
        sa.CheckConstraint("jsonb_typeof(other_charges) = 'array'",
                           name=op.f('ck_comparison_supplier_total_cargos_validos')),  # fmt: skip
        sa.ForeignKeyConstraint(
            ['comparison_run_id', 'organization_id'],
            ['comparison_run.id', 'comparison_run.organization_id'],
            name=op.f('fk_comparison_supplier_total_comparison_run_id_comparison_run'),
        ),
        sa.ForeignKeyConstraint(['supplier_id', 'organization_id'],
                                ['supplier.id', 'supplier.organization_id'],
                                name=op.f('fk_comparison_supplier_total_supplier_id_supplier')),  # fmt: skip
        sa.PrimaryKeyConstraint('id', name=op.f('pk_comparison_supplier_total')),
        sa.UniqueConstraint('id', 'organization_id',
                            name=op.f('uq_comparison_supplier_total_id')),  # fmt: skip
        sa.UniqueConstraint('comparison_run_id', 'supplier_id',
                            name=op.f('uq_comparison_supplier_total_comparison_run_id')),  # fmt: skip
    )
    op.create_index('ix_comparison_supplier_total_run', 'comparison_supplier_total',
                    ['comparison_run_id'], unique=False)  # fmt: skip

    # Una corrida no se edita: sus cuatro tablas son de solo agregar.
    for table in (
        'comparison_run',
        'comparison_line',
        'comparison_candidate',
        'comparison_supplier_total',
    ):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
        )

    # ------------------------------------------------------------------ criterios (E9)
    op.create_table(
        'commercial_criterion',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=10), server_default='active', nullable=False),
        sa.Column('origin', sa.String(length=10), server_default='explicit', nullable=False),
        sa.Column('effect', sa.String(length=20), nullable=False),
        sa.Column('target_level', sa.String(length=20), nullable=False),
        sa.Column('target_ingredient', sa.String(length=200), nullable=True),
        sa.Column('target_article_id', sa.Uuid(), nullable=True),
        sa.Column('target_supplier_id', sa.Uuid(), nullable=True),
        sa.Column('condition_kind', sa.String(length=20), server_default='always',
                  nullable=False),  # fmt: skip
        sa.Column('condition_min_quantity', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('condition_max_price_amount', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('condition_max_price_currency', sa.String(length=3), nullable=True),
        sa.Column('condition_max_price_basis', sa.String(length=24), nullable=True),
        sa.Column('condition_date_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('condition_date_to', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reason_text', sa.Text(), nullable=False),
        sa.Column('source_kind', sa.String(length=12), server_default='manual', nullable=False),
        sa.Column('source_comparison_id', sa.Uuid(), nullable=True),
        sa.Column('source_offer_id', sa.Uuid(), nullable=True),
        sa.Column('source_note_criterion_id', sa.Uuid(), nullable=True),
        sa.Column('source_note_version', sa.Integer(), nullable=True),
        sa.Column('validity_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('validity_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('superseded_by_criterion_id', sa.Uuid(), nullable=True),
        sa.Column('priority', sa.Integer(), nullable=True),
        sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified',
                  nullable=False),  # fmt: skip
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column('confirmed_by', sa.Uuid(), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deactivated_by', sa.Uuid(), nullable=True),
        sa.Column('deactivated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deactivation_reason', sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('note','criterion')",
                           name=op.f('ck_commercial_criterion_tipo_valido')),  # fmt: skip
        sa.CheckConstraint("status IN ('active','inactive')",
                           name=op.f('ck_commercial_criterion_estado_valido')),  # fmt: skip
        sa.CheckConstraint("origin IN ('explicit','inferred')",
                           name=op.f('ck_commercial_criterion_origen_valido')),  # fmt: skip
        # Una nota no recomienda ni ordena; solo un criterio confirmado lo hace.
        sa.CheckConstraint(
            "(kind = 'note' AND effect = 'note_only') OR "
            "(kind = 'criterion' AND effect IN "
            "('prefer','deprioritize','do_not_recommend','requires_review'))",
            name=op.f('ck_commercial_criterion_efecto_del_tipo'),
        ),
        sa.CheckConstraint(
            "target_level IN ('organization','ingredient','article','supplier','supplier_article')",
            name=op.f('ck_commercial_criterion_ambito_valido'),
        ),
        sa.CheckConstraint("target_level <> 'ingredient' OR target_ingredient IS NOT NULL",
                           name=op.f('ck_commercial_criterion_ingrediente')),  # fmt: skip
        sa.CheckConstraint("target_level <> 'article' OR target_article_id IS NOT NULL",
                           name=op.f('ck_commercial_criterion_articulo')),  # fmt: skip
        sa.CheckConstraint("target_level <> 'supplier' OR target_supplier_id IS NOT NULL",
                           name=op.f('ck_commercial_criterion_proveedor')),  # fmt: skip
        sa.CheckConstraint(
            "target_level <> 'supplier_article' OR "
            "(target_supplier_id IS NOT NULL AND target_article_id IS NOT NULL)",
            name=op.f('ck_commercial_criterion_proveedor_articulo'),
        ),
        sa.CheckConstraint(
            "condition_kind IN ('always','min_quantity','max_price','date_window')",
            name=op.f('ck_commercial_criterion_condicion_valida'),
        ),
        sa.CheckConstraint("condition_kind <> 'min_quantity' OR condition_min_quantity > 0",
                           name=op.f('ck_commercial_criterion_condicion_cantidad')),  # fmt: skip
        sa.CheckConstraint(
            "condition_kind <> 'max_price' OR "
            "(condition_max_price_amount IS NOT NULL AND condition_max_price_currency IS NOT NULL "
            "AND condition_max_price_basis IS NOT NULL)",
            name=op.f('ck_commercial_criterion_condicion_precio'),
        ),
        sa.CheckConstraint(
            "condition_kind <> 'date_window' OR "
            "(condition_date_from IS NOT NULL OR condition_date_to IS NOT NULL)",
            name=op.f('ck_commercial_criterion_condicion_fechas'),
        ),
        sa.CheckConstraint(
            "kind <> 'criterion' OR (confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name=op.f('ck_commercial_criterion_criterio_confirmado'),
        ),
        sa.CheckConstraint(
            "kind <> 'note' OR (confirmed_by IS NULL AND confirmed_at IS NULL)",
            name=op.f('ck_commercial_criterion_nota_sin_confirmar'),
        ),
        sa.CheckConstraint(
            "status <> 'inactive' OR "
            "(deactivated_by IS NOT NULL AND deactivated_at IS NOT NULL "
            "AND deactivation_reason IS NOT NULL)",
            name=op.f('ck_commercial_criterion_baja_con_motivo'),
        ),
        sa.CheckConstraint('version >= 1', name=op.f('ck_commercial_criterion_version_valida')),
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name=op.f('ck_commercial_criterion_origen_dato_valido'),
        ),
        sa.CheckConstraint('length(btrim(reason_text)) > 0',
                           name=op.f('ck_commercial_criterion_motivo_no_vacio')),  # fmt: skip
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'],
                                name=op.f('fk_commercial_criterion_organization_id_organization')),  # fmt: skip
        sa.ForeignKeyConstraint(
            ['source_comparison_id', 'organization_id'],
            ['comparison_run.id', 'comparison_run.organization_id'],
            name=op.f('fk_commercial_criterion_source_comparison_id_comparison_run'),
        ),
        sa.ForeignKeyConstraint(['target_supplier_id', 'organization_id'],
                                ['supplier.id', 'supplier.organization_id'],
                                name=op.f('fk_commercial_criterion_target_supplier_id_supplier')),  # fmt: skip
        sa.ForeignKeyConstraint(['target_article_id', 'organization_id'],
                                ['trade_item.id', 'trade_item.organization_id'],
                                name=op.f('fk_commercial_criterion_target_article_id_trade_item')),  # fmt: skip
        sa.ForeignKeyConstraint(['created_by', 'organization_id'],
                                ['app_user.id', 'app_user.organization_id'],
                                name=op.f('fk_commercial_criterion_created_by_app_user')),  # fmt: skip
        sa.ForeignKeyConstraint(['confirmed_by', 'organization_id'],
                                ['app_user.id', 'app_user.organization_id'],
                                name=op.f('fk_commercial_criterion_confirmed_by_app_user')),  # fmt: skip
        sa.ForeignKeyConstraint(['deactivated_by', 'organization_id'],
                                ['app_user.id', 'app_user.organization_id'],
                                name=op.f('fk_commercial_criterion_deactivated_by_app_user')),  # fmt: skip
        sa.PrimaryKeyConstraint('id', name=op.f('pk_commercial_criterion')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_commercial_criterion_id')),
    )
    # Una nota se convierte en criterio una sola vez: la trazabilidad no se duplica.
    op.create_index(
        'uq_criterion_from_note',
        'commercial_criterion',
        ['source_note_criterion_id'],
        unique=True,
        postgresql_where=sa.text('source_note_criterion_id IS NOT NULL'),
    )
    op.create_index('ix_criterion_org_status', 'commercial_criterion',
                    ['organization_id', 'status', 'kind'], unique=False)  # fmt: skip
    op.create_index('ix_criterion_target', 'commercial_criterion',
                    ['organization_id', 'target_level', 'target_article_id'], unique=False)  # fmt: skip


def downgrade() -> None:
    op.drop_table('commercial_criterion')
    for table in (
        'comparison_supplier_total',
        'comparison_candidate',
        'comparison_line',
        'comparison_run',
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
        op.drop_table(table)
