"""acceso web: solicitudes, vencimiento y superadministración

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-02 12:00:00.000000

G1 (D-130). Google/Supabase identifica a la persona, pero no concede acceso: la habilitación es una
decisión de un `admin` de la farmacia. La solicitud no otorga nada; la aprobación crea el `app_user`
con `roles = ['admin']`, `active` y `auth_subject` en el mismo paso, sin enlazar ni sobrescribir una
fila existente. Telegram sigue autorizándose por cuenta y chat, nunca por esta vía.

  * `web_access_request`: una solicitud pendiente por (organización, `auth_subject`). Los rechazos se
    conservan y permiten una solicitud posterior. El correo es informativo; la identidad es el `sub`.
  * `app_user.email`: correo del proveedor de identidad (informativo, no identidad).
  * `app_user.is_superadmin`: control técnico de la propietaria. NO es un rol; a lo más UNA activa.
  * `app_user.web_access_expires_at`: NULL = sin vencimiento, evaluado en cada petición.
  * `app_user.web_access_granted_by/_at`: trazabilidad del último otorgamiento.

Aditiva: no borra, renombra ni reescribe datos existentes.
"""
import sqlalchemy as sa
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('app_user', sa.Column('email', sa.String(length=320), nullable=True))
    op.add_column(
        'app_user',
        sa.Column('is_superadmin', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )
    op.add_column(
        'app_user', sa.Column('web_access_expires_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column('app_user', sa.Column('web_access_granted_by', sa.Uuid(), nullable=True))
    op.add_column(
        'app_user', sa.Column('web_access_granted_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        op.f('fk_app_user_web_access_granted_by_app_user'),
        'app_user',
        'app_user',
        ['web_access_granted_by', 'organization_id'],
        ['id', 'organization_id'],
    )
    # Exactamente una superadministradora ACTIVA en todo el sistema (D-119/D-130).
    op.create_index(
        'uq_app_user_one_superadmin',
        'app_user',
        ['is_superadmin'],
        unique=True,
        postgresql_where=sa.text('is_superadmin AND active'),
    )

    op.create_table(
        'web_access_request',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('auth_subject', sa.String(length=100), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('display_name', sa.String(length=100), nullable=False),
        sa.Column('status', sa.String(length=10), server_default='pending', nullable=False),
        sa.Column(
            'requested_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_by', sa.Uuid(), nullable=True),
        sa.Column('decision_note', sa.String(length=500), nullable=True),
        sa.Column(
            'data_origin', sa.String(length=12), server_default='unclassified', nullable=False
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected')",
            name=op.f('ck_web_access_request_estado_valido'),
        ),
        sa.CheckConstraint(
            "length(btrim(auth_subject)) > 0",
            name=op.f('ck_web_access_request_sujeto_valido'),
        ),
        sa.CheckConstraint(
            "length(btrim(email)) > 0", name=op.f('ck_web_access_request_email_valido')
        ),
        sa.CheckConstraint(
            'decision_note IS NULL OR length(decision_note) <= 500',
            name=op.f('ck_web_access_request_nota_valida'),
        ),
        sa.CheckConstraint(
            "data_origin IN ('simulated','captured','ai_derived','unclassified')",
            name=op.f('ck_web_access_request_origen_dato_valido'),
        ),
        sa.CheckConstraint(
            '(decided_at IS NULL) = (decided_by IS NULL)',
            name=op.f('ck_web_access_request_decision_coherente'),
        ),
        sa.CheckConstraint(
            "(status = 'pending') = (decided_at IS NULL)",
            name=op.f('ck_web_access_request_estado_coherente'),
        ),
        sa.ForeignKeyConstraint(
            ['organization_id'],
            ['organization.id'],
            name=op.f('fk_web_access_request_organization_id_organization'),
        ),
        sa.ForeignKeyConstraint(
            ['decided_by', 'organization_id'],
            ['app_user.id', 'app_user.organization_id'],
            name=op.f('fk_web_access_request_decided_by_app_user'),
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_web_access_request')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_web_access_request_id')),
    )
    # Una sola solicitud PENDIENTE por persona y organización; las rechazadas se conservan.
    op.create_index(
        'uq_web_access_request_pending_subject',
        'web_access_request',
        ['organization_id', 'auth_subject'],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        'ix_web_access_request_org_status',
        'web_access_request',
        ['organization_id', 'status', 'requested_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_web_access_request_org_status', table_name='web_access_request')
    op.drop_index('uq_web_access_request_pending_subject', table_name='web_access_request')
    op.drop_table('web_access_request')
    op.drop_index('uq_app_user_one_superadmin', table_name='app_user')
    op.drop_constraint(
        op.f('fk_app_user_web_access_granted_by_app_user'), 'app_user', type_='foreignkey'
    )
    op.drop_column('app_user', 'web_access_granted_at')
    op.drop_column('app_user', 'web_access_granted_by')
    op.drop_column('app_user', 'web_access_expires_at')
    op.drop_column('app_user', 'is_superadmin')
    op.drop_column('app_user', 'email')
