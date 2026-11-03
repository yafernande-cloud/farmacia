"""reportes independientes por autor, cancelación lógica y confirmación de coincidencias (D-135)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-02 21:00:00.000000

Modelo A: cada aviso crea —o reutiliza— una necesidad **de su propio autor**. Dos personas nunca
comparten una fila de `demand_item`: cada una conserva su cantidad declarada, sus observaciones y
su historial. La cantidad pedida sigue siendo una sola por reporte y solo cambia con SET/ADD/CLEAR
explícitos; los avisos repetidos no suman.

Cambios:

  * `demand_item.reported_by` (FK a `app_user` de la misma organización), nullable solo para filas
    legacy. Se rellena desde la observación cuando el faltante tiene **un único** autor; si hubo
    varios autores históricos se deja NULL y no se inventa una atribución.
  * El índice único de identidad activa pasa a incluir `reported_by`: la misma persona no crea dos
    veces el mismo producto; dos personas sí tienen un reporte cada una.
  * Cancelación **lógica**: `status='cancelled'`, `cancelled_at`, `cancelled_by`,
    `cancellation_reason`. Nunca se borra la fila ni sus observaciones.
  * `demand_observation.quantity_proposal`: propuesta de cantidad que el canal deja a la
    administración; el bot nunca la aplica por sí solo.
  * `channel_message.confirmation_status` / `confirmation_data` y el estado
    `pending_confirmation`: el mensaje original se conserva y el botón resuelve la confirmación de
    un reporte coincidente. El bot no aplica cantidades.

Reversión: el esquema anterior exige un único `demand_item` activo por identidad. Si durante el uso
de la 0016 se crearon reportes activos del mismo producto por personas distintas, el `downgrade`
**no puede** recrear el índice antiguo. En ese caso se detiene con un mensaje explícito para que se
cancelen, archiven o fusionen esos reportes antes de revertir; no borra datos por su cuenta.

No se crean tablas de compras: C5-B usa la 0019.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
EMPTY_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    # ---------------------------------------------------------------- demand_item: autor y cancelación
    op.add_column("demand_item", sa.Column("reported_by", sa.Uuid(), nullable=True))
    op.add_column(
        "demand_item", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("demand_item", sa.Column("cancelled_by", sa.Uuid(), nullable=True))
    op.add_column("demand_item", sa.Column("cancellation_reason", sa.Text(), nullable=True))

    # Atribución histórica honesta: solo cuando TODAS las observaciones del faltante son de la
    # misma persona. Con varios autores la fila queda NULL y se trata como legado consolidado.
    op.execute(
        """
        UPDATE demand_item d
        SET reported_by = x.author_id
        FROM (
            SELECT demand_item_id, (array_agg(DISTINCT author_id))[1] AS author_id
            FROM demand_observation
            WHERE demand_item_id IS NOT NULL
            GROUP BY demand_item_id
            HAVING count(DISTINCT author_id) = 1
        ) x
        WHERE d.id = x.demand_item_id AND d.reported_by IS NULL
        """
    )
    # Las cancelaciones anteriores a la 0016 ya tenían fecha de actualización: se conserva como
    # fecha de cancelación en vez de inventar un actor.
    op.execute(
        "UPDATE demand_item SET cancelled_at = updated_at "
        "WHERE status = 'cancelled' AND cancelled_at IS NULL"
    )

    op.create_foreign_key(
        op.f("fk_demand_item_reported_by_app_user"),
        "demand_item",
        "app_user",
        ["reported_by", "organization_id"],
        ["id", "organization_id"],
    )
    op.create_foreign_key(
        op.f("fk_demand_item_cancelled_by_app_user"),
        "demand_item",
        "app_user",
        ["cancelled_by", "organization_id"],
        ["id", "organization_id"],
    )
    op.create_check_constraint(
        op.f("ck_demand_item_cancelacion_coherente"),
        "demand_item",
        "(status = 'cancelled') = (cancelled_at IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_demand_item_cancelada_por_coherente"),
        "demand_item",
        "cancelled_by IS NULL OR status = 'cancelled'",
    )
    op.create_check_constraint(
        op.f("ck_demand_item_motivo_cancelacion_coherente"),
        "demand_item",
        "cancellation_reason IS NULL OR status = 'cancelled'",
    )

    op.drop_index("uq_demand_item_identity_active", table_name="demand_item")
    op.create_index(
        "uq_demand_item_identity_active",
        "demand_item",
        ["organization_id", "cycle_id", "resolved_identity_key", "reported_by"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'active' AND resolved_identity_key IS NOT NULL "
            "AND reported_by IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_demand_item_reporter",
        "demand_item",
        ["organization_id", "cycle_id", "reported_by", "status"],
        unique=False,
    )

    # ---------------------------------------------------------------- propuesta de cantidad
    op.add_column(
        "demand_observation",
        sa.Column("quantity_proposal", JSONB, server_default=EMPTY_OBJECT, nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_demand_observation_propuesta_valida"),
        "demand_observation",
        "jsonb_typeof(quantity_proposal) = 'object'",
    )

    # ---------------------------------------------------------------- confirmación en el canal
    op.add_column(
        "channel_message", sa.Column("confirmation_status", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "channel_message",
        sa.Column("confirmation_data", JSONB, server_default=EMPTY_OBJECT, nullable=False),
    )
    op.drop_constraint(
        op.f("ck_channel_message_atribucion_valida"), "channel_message", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_channel_message_atribucion_valida"),
        "channel_message",
        "attribution_status IN "
        "('attributed','pending_attribution','pending_confirmation','discarded')",
    )
    op.drop_constraint(
        op.f("ck_channel_message_autor_coherente"), "channel_message", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_channel_message_autor_coherente"),
        "channel_message",
        "(attribution_status IN ('attributed','pending_confirmation')) = "
        "(author_id IS NOT NULL) OR attribution_status = 'discarded'",
    )
    op.create_check_constraint(
        op.f("ck_channel_message_confirmacion_valida"),
        "channel_message",
        "confirmation_status IS NULL OR confirmation_status IN "
        "('pending_other_report','pending_same_author_quantity','awaiting_quantity',"
        "'resolved','cancelled')",
    )
    op.create_check_constraint(
        op.f("ck_channel_message_confirmacion_datos_validos"),
        "channel_message",
        "jsonb_typeof(confirmation_data) = 'object'",
    )
    op.create_index(
        "ix_channel_message_confirmation",
        "channel_message",
        ["channel_binding_id", "received_at"],
        unique=False,
        postgresql_where=sa.text("attribution_status = 'pending_confirmation'"),
    )


def downgrade() -> None:
    op.drop_index("ix_channel_message_confirmation", table_name="channel_message")
    op.drop_constraint(
        op.f("ck_channel_message_confirmacion_datos_validos"), "channel_message", type_="check"
    )
    op.drop_constraint(
        op.f("ck_channel_message_confirmacion_valida"), "channel_message", type_="check"
    )
    op.drop_constraint(
        op.f("ck_channel_message_autor_coherente"), "channel_message", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_channel_message_autor_coherente"),
        "channel_message",
        "(attribution_status = 'attributed') = (author_id IS NOT NULL)",
    )
    op.drop_constraint(
        op.f("ck_channel_message_atribucion_valida"), "channel_message", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_channel_message_atribucion_valida"),
        "channel_message",
        "attribution_status IN ('attributed','pending_attribution','discarded')",
    )
    op.drop_column("channel_message", "confirmation_data")
    op.drop_column("channel_message", "confirmation_status")

    op.drop_constraint(
        op.f("ck_demand_observation_propuesta_valida"), "demand_observation", type_="check"
    )
    op.drop_column("demand_observation", "quantity_proposal")

    # Reversión segura: el índice antiguo no admite dos reportes activos de la misma identidad.
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT organization_id, cycle_id, resolved_identity_key, count(*) AS reports
                FROM demand_item
                WHERE status = 'active' AND resolved_identity_key IS NOT NULL
                GROUP BY organization_id, cycle_id, resolved_identity_key
                HAVING count(*) > 1
                LIMIT 1
                """
            )
        )
        .first()
    )
    if duplicate:
        raise RuntimeError(
            "No se puede revertir la 0016: hay más de un reporte activo con la misma identidad "
            f"(organización {duplicate.organization_id}, ciclo {duplicate.cycle_id}, "
            f"identidad {duplicate.resolved_identity_key!r}, {duplicate.reports} reportes). "
            "Cancela, archiva o fusiona esos reportes antes de revertir; el esquema anterior "
            "exige uno solo y esta migración no borra datos por su cuenta."
        )
    op.drop_index("ix_demand_item_reporter", table_name="demand_item")
    op.drop_index("uq_demand_item_identity_active", table_name="demand_item")
    op.create_index(
        "uq_demand_item_identity_active",
        "demand_item",
        ["organization_id", "cycle_id", "resolved_identity_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND resolved_identity_key IS NOT NULL"),
    )
    op.drop_constraint(
        op.f("ck_demand_item_motivo_cancelacion_coherente"), "demand_item", type_="check"
    )
    op.drop_constraint(
        op.f("ck_demand_item_cancelada_por_coherente"), "demand_item", type_="check"
    )
    op.drop_constraint(
        op.f("ck_demand_item_cancelacion_coherente"), "demand_item", type_="check"
    )
    op.drop_constraint(
        op.f("fk_demand_item_cancelled_by_app_user"), "demand_item", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_demand_item_reported_by_app_user"), "demand_item", type_="foreignkey"
    )
    op.drop_column("demand_item", "cancellation_reason")
    op.drop_column("demand_item", "cancelled_by")
    op.drop_column("demand_item", "cancelled_at")
    op.drop_column("demand_item", "reported_by")
