"""autorización web independiente de la identidad

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-12 12:00:00.000000

D-158. Hasta ahora la autorización web vivía en tres columnas de `app_user`
(`web_access_expires_at`, `web_access_granted_by/_at`), mezclada con la identidad. Así, «eliminar
el acceso» solo podía simularse con un vencimiento y la persona quedaba atrapada: su `auth_subject`
seguía existiendo y el backend no le permitía volver a solicitar.

Esta migración separa la autorización en `web_access_grant`:

  * una fila por persona (`app_user_id` es la PK);
  * `app_user` conserva la identidad, el `auth_subject`, los roles, Telegram y todo el historial;
  * eliminar la fila es una eliminación real de la autorización, sin `DELETE` sobre `app_user`;
  * `expires_at` conserva el vencimiento programable (NULL = sin vencimiento).

Relleno: toda persona activa con identidad enlazada y acceso vigente (sin vencimiento o con
vencimiento futuro) recibe su autorización, para que nadie pierda el acceso al desplegar. Las
personas ya vencidas no la reciben y podrán solicitar de nuevo.

Las columnas antiguas **no** se eliminan en esta migración: quedan como respaldo de la transición y
están deprecadas. Ninguna concede acceso por sí sola: el backend consulta solo esta tabla.
"""

import sqlalchemy as sa
from alembic import op

revision = '0031'
down_revision = '0030'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'web_access_grant',
        sa.Column('app_user_id', sa.Uuid(), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('request_id', sa.Uuid(), nullable=True),
        sa.Column('granted_by', sa.Uuid(), nullable=True),
        sa.Column(
            'granted_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            'expires_at IS NULL OR expires_at > granted_at',
            name=op.f('ck_web_access_grant_vigencia_coherente'),
        ),
        sa.ForeignKeyConstraint(
            ['organization_id'],
            ['organization.id'],
            name=op.f('fk_web_access_grant_organization_id_organization'),
        ),
        sa.ForeignKeyConstraint(
            ['app_user_id', 'organization_id'],
            ['app_user.id', 'app_user.organization_id'],
            name='fk_web_access_grant_app_user',
        ),
        sa.ForeignKeyConstraint(
            ['granted_by', 'organization_id'],
            ['app_user.id', 'app_user.organization_id'],
            name='fk_web_access_grant_granted_by',
        ),
        sa.ForeignKeyConstraint(
            ['request_id', 'organization_id'],
            ['web_access_request.id', 'web_access_request.organization_id'],
            name='fk_web_access_grant_request',
        ),
        sa.PrimaryKeyConstraint('app_user_id', name=op.f('pk_web_access_grant')),
    )
    op.create_index(
        'ix_web_access_grant_org',
        'web_access_grant',
        ['organization_id'],
        unique=False,
    )

    # Relleno: conserva el acceso de quienes hoy lo tienen. La condición de vigencia es la misma
    # que aplicaba `_actor_from_jwt` (persona activa y vencimiento nulo o futuro).
    op.execute(
        sa.text(
            """
            INSERT INTO web_access_grant
                (app_user_id, organization_id, granted_by, granted_at, expires_at)
            SELECT u.id,
                   u.organization_id,
                   u.web_access_granted_by,
                   COALESCE(u.web_access_granted_at, u.created_at),
                   u.web_access_expires_at
            FROM app_user u
            WHERE u.active
              AND u.auth_subject IS NOT NULL
              AND (u.web_access_expires_at IS NULL OR u.web_access_expires_at > now())
            """
        )
    )


def downgrade() -> None:
    op.drop_index('ix_web_access_grant_org', table_name='web_access_grant')
    op.drop_table('web_access_grant')
