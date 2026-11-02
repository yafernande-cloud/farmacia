"""búsqueda de productos y ofertas (C4, E1–E3)

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-02 16:00:00.000000

C0 describe la búsqueda como `search_run` + `search_source_result`; el diseño de C4 añade
`search_result` como ampliación **aditiva** (D-125), no un reemplazo. Las tres nacen aquí porque la
misma fase las consume.

  * `search_run`: la consulta tal como se escribió, sus filtros, la interpretación determinista y el
    desenlace global (`completed`/`partial`/`failed`). E2 la devuelve sin reejecutar nada.
  * `search_source_result`: estado por fuente en ESA corrida y cuántos resultados aportó. Un fallo
    lleva código, mensaje y si es reintentable; lo demás no se disfraza de fallo.
  * `search_result`: una fila por artículo con su clasificación `relation` (contrato §12.1), el
    nivel de `match` de C0 y los agregados de ofertas observados al buscar. Guarda una copia de lo
    que se mostró: si el artículo cambia después, el snapshot no cambia.

Solo agregar: una búsqueda no se edita. `offer_snapshot` sigue siendo la única fuente de precios y
estas tablas no crean ofertas. La búsqueda de cadenas (E15) no se persiste (contrato §11.9).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'search_run',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('query_text', sa.String(length=200), nullable=False),
        sa.Column('filters', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),  # fmt: skip
        sa.Column('interpretation', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),  # fmt: skip
        sa.Column('status', sa.String(length=10), server_default='completed', nullable=False),
        sa.Column('limit_value', sa.Integer(), server_default='20', nullable=False),
        sa.Column('result_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('error_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=False),
        sa.Column('data_origin', sa.String(length=12), server_default='unclassified',
                  nullable=False),  # fmt: skip
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),  # fmt: skip
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name=op.f('ck_search_run_origen_dato_valido'),
        ),
        sa.CheckConstraint("error_count >= 0", name=op.f('ck_search_run_errores_no_negativos')),
        sa.CheckConstraint("result_count >= 0", name=op.f('ck_search_run_resultados_no_negativos')),
        sa.CheckConstraint(
            "status IN ('completed','partial','failed')", name=op.f('ck_search_run_estado_valido')
        ),
        sa.CheckConstraint(
            'limit_value BETWEEN 1 AND 50', name=op.f('ck_search_run_limite_valido')
        ),
        sa.ForeignKeyConstraint(
            ['created_by', 'organization_id'],
            ['app_user.id', 'app_user.organization_id'],
            name=op.f('fk_search_run_created_by_app_user'),
        ),
        sa.ForeignKeyConstraint(
            ['organization_id'],
            ['organization.id'],
            name=op.f('fk_search_run_organization_id_organization'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_search_run')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_search_run_id')),
    )
    op.create_index(
        'ix_search_run_org_created',
        'search_run',
        ['organization_id', 'created_at'],
        unique=False,
    )

    op.create_table(
        'search_source_result',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('search_run_id', sa.Uuid(), nullable=False),
        sa.Column('source_id', sa.Uuid(), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('outcome', sa.String(length=12), nullable=True),
        sa.Column('error_code', sa.String(length=40), nullable=True),
        sa.Column('error_message', sa.String(length=300), nullable=True),
        sa.Column('retryable', sa.Boolean(), nullable=True),
        sa.Column('matches_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),  # fmt: skip
        sa.CheckConstraint(
            "(state = 'failed') = (error_code IS NOT NULL)",
            name=op.f('ck_search_source_result_fallo_con_codigo'),
        ),
        sa.CheckConstraint(
            "(state = 'failed') = (error_message IS NOT NULL)",
            name=op.f('ck_search_source_result_fallo_con_mensaje'),
        ),
        sa.CheckConstraint(
            "(state = 'failed') = (retryable IS NOT NULL)",
            name=op.f('ck_search_source_result_fallo_con_reintento'),
        ),
        sa.CheckConstraint(
            'matches_count >= 0',
            name=op.f('ck_search_source_result_coincidencias_no_negativas'),
        ),
        sa.CheckConstraint(
            "(state IN ('ok','stale')) = (outcome IS NOT NULL)",
            name=op.f('ck_search_source_result_resultado_coherente'),
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('matches','no_matches')",
            name=op.f('ck_search_source_result_resultado_valido'),
        ),
        sa.CheckConstraint(
            "state IN ('ok','stale','failed','not_configured')",
            name=op.f('ck_search_source_result_estado_valido'),
        ),
        sa.ForeignKeyConstraint(
            ['organization_id'],
            ['organization.id'],
            name=op.f('fk_search_source_result_organization_id_organization'),
        ),
        sa.ForeignKeyConstraint(
            ['search_run_id', 'organization_id'],
            ['search_run.id', 'search_run.organization_id'],
            name=op.f('fk_search_source_result_search_run_id_search_run'),
        ),
        sa.ForeignKeyConstraint(
            ['source_id', 'organization_id'],
            ['source.id', 'source.organization_id'],
            name=op.f('fk_search_source_result_source_id_source'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_search_source_result')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_search_source_result_id')),
        sa.UniqueConstraint(
            'search_run_id', 'source_id', name=op.f('uq_search_source_result_search_run_id')
        ),
    )
    op.create_index(
        'ix_search_source_result_run',
        'search_source_result',
        ['search_run_id'],
        unique=False,
    )

    op.create_table(
        'search_result',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('search_run_id', sa.Uuid(), nullable=False),
        sa.Column('trade_item_id', sa.Uuid(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('relation', sa.String(length=20), nullable=False),
        sa.Column('match_level', sa.String(length=20), nullable=False),
        sa.Column('match_causes', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),  # fmt: skip
        sa.Column('display_name', sa.String(length=300), nullable=False),
        sa.Column('identity_summary', sa.String(length=300), nullable=False),
        sa.Column('presentation', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),  # fmt: skip
        sa.Column('offer_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('supplier_count', sa.Integer(), server_default='0', nullable=False),
        sa.Column('lowest_price_amount', sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column('lowest_price_currency', sa.String(length=3), nullable=True),
        sa.Column('source_ids', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),  # fmt: skip
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),  # fmt: skip
        sa.CheckConstraint(
            "jsonb_typeof(match_causes) = 'array'",
            name=op.f('ck_search_result_causas_validas'),
        ),
        sa.CheckConstraint(
            "(lowest_price_amount IS NULL) = (lowest_price_currency IS NULL)",
            name=op.f('ck_search_result_precio_con_moneda'),
        ),
        sa.CheckConstraint(
            "lowest_price_currency IS NULL OR lowest_price_currency ~ '^[A-Z]{3}$'",
            name=op.f('ck_search_result_moneda_valida'),
        ),
        sa.CheckConstraint(
            'lowest_price_amount IS NULL OR lowest_price_amount >= 0',
            name=op.f('ck_search_result_precio_no_negativo'),
        ),
        sa.CheckConstraint(
            "match_level IN ('exact','equivalent_authorized','needs_review')",
            name=op.f('ck_search_result_nivel_valido'),
        ),
        sa.CheckConstraint('offer_count >= 0', name=op.f('ck_search_result_ofertas_no_negativas')),
        sa.CheckConstraint('rank >= 0', name=op.f('ck_search_result_orden_valido')),
        sa.CheckConstraint(
            "relation IN ('exact','other_presentation','combined','related','needs_review')",
            name=op.f('ck_search_result_relacion_valida'),
        ),
        sa.CheckConstraint(
            'supplier_count <= offer_count',
            name=op.f('ck_search_result_proveedores_caben_en_ofertas'),
        ),
        sa.CheckConstraint(
            'supplier_count >= 0', name=op.f('ck_search_result_proveedores_no_negativos')
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_ids) = 'array'", name=op.f('ck_search_result_fuentes_validas')
        ),
        sa.ForeignKeyConstraint(
            ['organization_id'],
            ['organization.id'],
            name=op.f('fk_search_result_organization_id_organization'),
        ),
        sa.ForeignKeyConstraint(
            ['search_run_id', 'organization_id'],
            ['search_run.id', 'search_run.organization_id'],
            name=op.f('fk_search_result_search_run_id_search_run'),
        ),
        sa.ForeignKeyConstraint(
            ['trade_item_id', 'organization_id'],
            ['trade_item.id', 'trade_item.organization_id'],
            name=op.f('fk_search_result_trade_item_id_trade_item'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_search_result')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_search_result_id')),
        sa.UniqueConstraint(
            'search_run_id', 'trade_item_id', name=op.f('uq_search_result_search_run_id')
        ),
    )
    op.create_index(
        'ix_search_result_run_rank',
        'search_result',
        ['search_run_id', 'rank'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_search_result_run_rank', table_name='search_result')
    op.drop_table('search_result')
    op.drop_index('ix_search_source_result_run', table_name='search_source_result')
    op.drop_table('search_source_result')
    op.drop_index('ix_search_run_org_created', table_name='search_run')
    op.drop_table('search_run')
