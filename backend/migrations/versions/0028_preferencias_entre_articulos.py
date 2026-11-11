"""preferencias comerciales entre artículos y aclaraciones (A6 Fase 4)

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-11 12:00:00.000000

Agrega `commercial_article_preference`, separada de `commercial_preference`:

* `deprioritize` global: baja la prioridad de un artículo en las listas donde aparezca.
* `prefer` con alternativa: el artículo gana frente a otro cuando ambos están presentes.
* `retire`: deja sin efecto el evento anterior de la misma relación, sin borrar historia.

No hay equivalencia farmacológica ni fusión de artículos: cada fila referencia artículos
existentes y conserva la etiqueta verificada con la que se interpretó. La resolución del evento
más reciente es del motor de lectura; la historia no se reescribe.

También agrega `observation_interpretation.clarification` (JSONB nullable): cuando una referencia
a otro producto es ambigua, la interpretación queda `needs_review`, guarda una pregunta breve y
NO aplica ninguna preferencia hasta que se resuelva. Las filas existentes quedan en NULL.

`downgrade` elimina la tabla y la columna; no toca datos de otras tablas.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "commercial_article_preference",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("interpretation_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("article_label", sa.String(length=300), nullable=False),
        sa.Column("relative_to_article_id", sa.Uuid(), nullable=True),
        sa.Column("relative_to_article_label", sa.String(length=300), nullable=True),
        sa.Column("effect", sa.String(length=12), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="suggested", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column(
            "data_origin", sa.String(length=12), server_default="ai_derived", nullable=False
        ),
        sa.CheckConstraint(
            "effect IN ('prefer','deprioritize','retire')",
            name=op.f("ck_commercial_article_preference_efecto_valido"),
        ),
        sa.CheckConstraint(
            "status IN ('suggested','confirmed','rejected','needs_review')",
            name=op.f("ck_commercial_article_preference_estado_valido"),
        ),
        sa.CheckConstraint(
            "relative_to_article_id IS NULL OR article_id <> relative_to_article_id",
            name=op.f("ck_commercial_article_preference_articulos_distintos"),
        ),
        sa.CheckConstraint(
            "effect <> 'prefer' OR relative_to_article_id IS NOT NULL",
            name=op.f("ck_commercial_article_preference_preferencia_con_alternativa"),
        ),
        sa.CheckConstraint(
            "effect <> 'deprioritize' OR relative_to_article_id IS NULL",
            name=op.f("ck_commercial_article_preference_baja_sin_alternativa"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name=op.f("ck_commercial_article_preference_confianza_valida"),
        ),
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived')",
            name=op.f("ck_commercial_article_preference_origen_dato_valido"),
        ),
        sa.CheckConstraint(
            "length(btrim(article_label)) > 0 AND length(article_label) <= 300",
            name=op.f("ck_commercial_article_preference_etiqueta_articulo_valida"),
        ),
        sa.CheckConstraint(
            "relative_to_article_label IS NULL OR (length(btrim(relative_to_article_label)) > 0"
            " AND length(relative_to_article_label) <= 300)",
            name=op.f("ck_commercial_article_preference_etiqueta_alternativa_valida"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_commercial_article_preference_organization_id_organization"),
        ),
        sa.ForeignKeyConstraint(
            ["interpretation_id", "organization_id"],
            ["observation_interpretation.id", "observation_interpretation.organization_id"],
            name=op.f(
                "fk_commercial_article_preference_interpretation_id_organization_id_observation_interpretation"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id", "organization_id"],
            ["commercial_observation.id", "commercial_observation.organization_id"],
            name=op.f(
                "fk_commercial_article_preference_observation_id_organization_id_commercial_observation"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["article_id", "organization_id"],
            ["trade_item.id", "trade_item.organization_id"],
            name=op.f(
                "fk_commercial_article_preference_article_id_organization_id_trade_item"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["relative_to_article_id", "organization_id"],
            ["trade_item.id", "trade_item.organization_id"],
            name=op.f(
                "fk_commercial_article_preference_relative_to_article_id_organization_id_trade_item"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commercial_article_preference")),
        sa.UniqueConstraint(
            "id", "organization_id", name=op.f("uq_commercial_article_preference_id")
        ),
    )
    op.create_index(
        "ix_commercial_article_preference_article",
        "commercial_article_preference",
        ["organization_id", "article_id", "status", "created_at"],
    )
    op.create_index(
        "ix_commercial_article_preference_relative",
        "commercial_article_preference",
        ["organization_id", "relative_to_article_id", "status", "created_at"],
    )
    op.create_index(
        "ix_commercial_article_preference_interpretation",
        "commercial_article_preference",
        ["interpretation_id"],
    )

    op.add_column(
        "observation_interpretation",
        sa.Column("clarification", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("observation_interpretation", "clarification")
    op.drop_index(
        "ix_commercial_article_preference_interpretation",
        table_name="commercial_article_preference",
    )
    op.drop_index(
        "ix_commercial_article_preference_relative",
        table_name="commercial_article_preference",
    )
    op.drop_index(
        "ix_commercial_article_preference_article",
        table_name="commercial_article_preference",
    )
    op.drop_table("commercial_article_preference")
