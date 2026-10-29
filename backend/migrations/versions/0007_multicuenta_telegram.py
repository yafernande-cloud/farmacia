"""multicuenta de Telegram: solicitudes de acceso, cuentas autorizadas y trazabilidad

Revision ID: 0007
Revises: 0005
Create Date: 2026-10-28 18:00:00.000000

Nota de numeración: `scripts/migration/0006_separar_organizaciones.sql` es un script de datos
manual, no una revisión Alembic. Por eso esta revisión es 0007 y encadena con 0005 (cabeza real).

Flujo que soporta:
  * `telegram_access_request`: una cuenta de Telegram PIDE acceso. Mientras está `pending` no existe
    cuenta ni chat habilitado: no concede ninguna función operativa. Solo se guarda el SHA-256 del
    código; vence a los 30 minutos, es de un solo uso y tiene límites de intentos.
  * `telegram_account`: la cuenta autorizada, creada al aprobar la solicitud (o por el backfill de
    chats ya autorizados). Puede bloquearse y rehabilitarse sin borrar su historial.
  * `telegram_access_attempt`: intentos de ingresar un código en la pantalla administrativa. Guarda
    solo el hash, nunca el código.
  * `account_id` en vínculos, sesiones, mensajes y observaciones para saber por qué cuenta entró
    cada registro, sin confundir la cuenta con la persona.

Backfill: los vínculos privados ya existentes en organizaciones reales/prueba real/seguimiento legado
se reasocian a una cuenta `shared_device` con el mismo id de usuario del chat. Así el flujo que ya
funciona sigue funcionando (el chat privado tiene from.id = chat.id) y queda la trazabilidad. Los
chats de SIMULACIÓN no se tocan: el simulador envía desde otra cuenta ficticia y se mantiene el
camino legado, sin cuentas reales en una organización simulada.

No borra ni reinicializa ninguna tabla. El historial previo conserva `account_id = NULL`.
"""
import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # La FK de la solicitud hacia la cuenta es circular: se agrega después de crear ambas tablas.
    op.create_table(
        'telegram_access_request',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('provider', sa.String(length=20), server_default='telegram', nullable=False),
        sa.Column('external_user_id', sa.String(length=64), nullable=False),
        sa.Column('external_chat_id', sa.String(length=64), nullable=False),
        sa.Column('request_update_id', sa.String(length=64), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=10), server_default='pending', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_by', sa.Uuid(), nullable=True),
        sa.Column('approved_account_id', sa.Uuid(), nullable=True),
        sa.Column('rejected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rejected_by', sa.Uuid(), nullable=True),
        sa.Column('reject_reason', sa.String(length=200), nullable=True),
        sa.CheckConstraint("provider IN ('telegram')", name=op.f('ck_telegram_access_request_proveedor_valido')),
        sa.CheckConstraint("status IN ('pending','approved','rejected','expired')", name=op.f('ck_telegram_access_request_estado_valido')),
        sa.CheckConstraint('expires_at > created_at', name=op.f('ck_telegram_access_request_vigencia_valida')),
        sa.CheckConstraint('(approved_at IS NULL) = (approved_by IS NULL)', name=op.f('ck_telegram_access_request_aprobacion_autor_valida')),
        sa.CheckConstraint('(approved_at IS NULL) = (approved_account_id IS NULL)', name=op.f('ck_telegram_access_request_aprobacion_cuenta_valida')),
        sa.CheckConstraint('(rejected_at IS NULL) = (rejected_by IS NULL)', name=op.f('ck_telegram_access_request_rechazo_autor_valido')),
        sa.CheckConstraint("(status = 'approved') = (approved_at IS NOT NULL)", name=op.f('ck_telegram_access_request_estado_aprobado_valido')),
        sa.CheckConstraint("(status = 'rejected') = (rejected_at IS NOT NULL)", name=op.f('ck_telegram_access_request_estado_rechazado_valido')),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_telegram_access_request_organization_id_organization')),
        sa.ForeignKeyConstraint(['rejected_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_telegram_access_request_rejected_by_app_user')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_telegram_access_request')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_telegram_access_request_id')),
        sa.UniqueConstraint('code_hash', name='uq_telegram_access_request_code'),
        sa.UniqueConstraint('provider', 'request_update_id', name='uq_telegram_access_request_update'),
    )
    op.create_index('ix_telegram_access_request_org', 'telegram_access_request',
                    ['organization_id', 'created_at'], unique=False)
    op.create_index('ix_telegram_access_request_pending', 'telegram_access_request',
                    ['organization_id', 'expires_at'], unique=False,
                    postgresql_where=sa.text("status = 'pending'"))
    # A lo más UNA solicitud pendiente por cuenta de Telegram (en cualquier organización).
    op.create_index('uq_telegram_access_request_pending_account', 'telegram_access_request',
                    ['provider', 'external_user_id'], unique=True,
                    postgresql_where=sa.text("status = 'pending'"))

    op.create_table(
        'telegram_account',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('provider', sa.String(length=20), server_default='telegram', nullable=False),
        sa.Column('external_user_id', sa.String(length=64), nullable=False),
        sa.Column('display_label', sa.String(length=100), nullable=False),
        sa.Column('mode', sa.String(length=20), nullable=False),
        sa.Column('access_role', sa.String(length=10), server_default='operator', nullable=False),
        sa.Column('app_user_id', sa.Uuid(), nullable=True),
        sa.Column('status', sa.String(length=10), server_default='active', nullable=False),
        sa.Column('origin', sa.String(length=16), server_default='access_request', nullable=False),
        sa.Column('created_from_request_id', sa.Uuid(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.Uuid(), nullable=True),
        sa.Column('blocked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('blocked_by', sa.Uuid(), nullable=True),
        sa.Column('block_reason', sa.String(length=200), nullable=True),
        sa.Column('reactivated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reactivated_by', sa.Uuid(), nullable=True),
        sa.CheckConstraint("provider IN ('telegram')", name=op.f('ck_telegram_account_proveedor_valido')),
        sa.CheckConstraint("mode IN ('personal','shared_device')", name=op.f('ck_telegram_account_modo_valido')),
        sa.CheckConstraint("access_role IN ('admin','operator')", name=op.f('ck_telegram_account_rol_valido')),
        sa.CheckConstraint("status IN ('active','blocked')", name=op.f('ck_telegram_account_estado_valido')),
        sa.CheckConstraint("origin IN ('backfill','access_request','manual')", name=op.f('ck_telegram_account_origen_valido')),
        sa.CheckConstraint("(mode = 'personal') = (app_user_id IS NOT NULL)", name=op.f('ck_telegram_account_persona_coherente')),
        sa.CheckConstraint("(status = 'blocked') = (blocked_at IS NOT NULL)", name=op.f('ck_telegram_account_bloqueo_coherente')),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_telegram_account_organization_id_organization')),
        sa.ForeignKeyConstraint(['created_from_request_id', 'organization_id'],
                                ['telegram_access_request.id', 'telegram_access_request.organization_id'],
                                name=op.f('fk_telegram_account_created_from_request_id_telegram_access_request')),
        sa.ForeignKeyConstraint(['app_user_id', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_telegram_account_app_user_id_app_user')),
        sa.ForeignKeyConstraint(['created_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_telegram_account_created_by_app_user')),
        sa.ForeignKeyConstraint(['blocked_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_telegram_account_blocked_by_app_user')),
        sa.ForeignKeyConstraint(['reactivated_by', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_telegram_account_reactivated_by_app_user')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_telegram_account')),
        sa.UniqueConstraint('id', 'organization_id', name=op.f('uq_telegram_account_id')),
        sa.UniqueConstraint('provider', 'external_user_id', name='uq_telegram_account_external'),
        sa.UniqueConstraint('created_from_request_id', name=op.f('uq_telegram_account_created_from_request_id')),
    )
    op.create_index('ix_telegram_account_org_status', 'telegram_account', ['organization_id', 'status'], unique=False)
    op.create_index('ix_telegram_account_person', 'telegram_account', ['organization_id', 'app_user_id'], unique=False)

    # Cierra el ciclo solicitud → cuenta aprobada.
    op.create_foreign_key(
        op.f('fk_telegram_access_request_approved_account_id_telegram_account'),
        'telegram_access_request', 'telegram_account',
        ['approved_account_id', 'organization_id'], ['id', 'organization_id'],
    )

    op.create_table(
        'telegram_access_attempt',
        sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('organization_id', sa.Uuid(), nullable=False),
        sa.Column('app_user_id', sa.Uuid(), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('success', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('attempted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id'], name=op.f('fk_telegram_access_attempt_organization_id_organization')),
        sa.ForeignKeyConstraint(['app_user_id', 'organization_id'], ['app_user.id', 'app_user.organization_id'], name=op.f('fk_telegram_access_attempt_app_user_id_app_user')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_telegram_access_attempt')),
    )
    op.create_index('ix_telegram_access_attempt_admin', 'telegram_access_attempt',
                    ['organization_id', 'app_user_id', 'attempted_at'], unique=False)
    op.create_index('ix_telegram_access_attempt_code', 'telegram_access_attempt',
                    ['organization_id', 'code_hash', 'attempted_at'], unique=False)

    # --- vínculos: modo + cuenta autorizada
    op.add_column('channel_binding', sa.Column('account_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(op.f('fk_channel_binding_account_id_telegram_account'), 'channel_binding', 'telegram_account',
                          ['account_id', 'organization_id'], ['id', 'organization_id'])
    op.create_check_constraint(op.f('ck_channel_binding_modo_valido'), 'channel_binding',
                               "mode IN ('shared_device','personal')")
    op.create_check_constraint(op.f('ck_channel_binding_cuenta_personal_obligatoria'), 'channel_binding',
                               "mode <> 'personal' OR account_id IS NOT NULL")

    # --- sesiones: la cuenta que envió, separada de la persona
    op.add_column('actor_session', sa.Column('account_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(op.f('fk_actor_session_account_id_telegram_account'), 'actor_session', 'telegram_account',
                          ['account_id', 'organization_id'], ['id', 'organization_id'])
    op.drop_constraint(op.f('ck_actor_session_nivel_valido'), 'actor_session', type_='check')
    op.create_check_constraint(op.f('ck_actor_session_nivel_valido'), 'actor_session',
                               "identity_level IN ('declared','pin','account')")
    op.create_check_constraint(op.f('ck_actor_session_cuenta_coherente'), 'actor_session',
                               "identity_level <> 'account' OR account_id IS NOT NULL")

    # --- mensajes: base de atribución `authorized_account`
    op.add_column('channel_message', sa.Column('account_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(op.f('fk_channel_message_account_id_telegram_account'), 'channel_message', 'telegram_account',
                          ['account_id', 'organization_id'], ['id', 'organization_id'])
    op.drop_constraint(op.f('ck_channel_message_base_valida'), 'channel_message', type_='check')
    op.create_check_constraint(op.f('ck_channel_message_base_valida'), 'channel_message',
                               "attribution_basis IS NULL OR attribution_basis IN "
                               "('session_at_receipt','confirmed_after_identification','authorized_account')")

    # --- observaciones: identidad de cuenta
    op.add_column('demand_observation', sa.Column('account_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(op.f('fk_demand_observation_account_id_telegram_account'), 'demand_observation', 'telegram_account',
                          ['account_id', 'organization_id'], ['id', 'organization_id'])
    op.drop_constraint(op.f('ck_demand_observation_nivel_valido'), 'demand_observation', type_='check')
    op.create_check_constraint(op.f('ck_demand_observation_nivel_valido'), 'demand_observation',
                               "identity_level IN ('api_user','declared','pin','account')")
    op.create_check_constraint(op.f('ck_demand_observation_cuenta_coherente'), 'demand_observation',
                               "identity_level <> 'account' OR account_id IS NOT NULL")

    # --- backfill compatible (sin tocar simulación ni grupos)
    # Un chat privado real tiene external_chat_id = id de usuario; se autoriza esa cuenta como
    # teléfono compartido, que es exactamente el flujo que ya operaba.
    op.execute("""
        INSERT INTO telegram_account
            (organization_id, provider, external_user_id, display_label, mode, access_role, status,
             origin, created_at)
        SELECT DISTINCT b.organization_id, b.provider, b.external_chat_id, 'Teléfono compartido',
               'shared_device', 'operator', 'active', 'backfill', b.created_at
        FROM channel_binding b
        JOIN organization o ON o.id = b.organization_id
        WHERE b.status = 'active' AND b.chat_kind = 'private'
          AND o.kind IN ('real_test','production','legacy_mixed')
          AND NOT EXISTS (
              SELECT 1 FROM telegram_account a
              WHERE a.provider = b.provider AND a.external_user_id = b.external_chat_id
          )
    """)
    op.execute("""
        UPDATE channel_binding b
        SET account_id = a.id, mode = 'shared_device'
        FROM telegram_account a, organization o
        WHERE a.provider = b.provider AND a.external_user_id = b.external_chat_id
          AND a.organization_id = b.organization_id
          AND o.id = b.organization_id
          AND b.status = 'active' AND b.chat_kind = 'private'
          AND o.kind IN ('real_test','production','legacy_mixed')
          AND b.account_id IS NULL
    """)

    # La trazabilidad de la cuenta también es inmutable en una observación ya escrita.
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_raw_text_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.raw_text IS DISTINCT FROM OLD.raw_text OR NEW.parsed IS DISTINCT FROM OLD.parsed
               OR NEW.author_id IS DISTINCT FROM OLD.author_id
               OR NEW.interpretation IS DISTINCT FROM OLD.interpretation
               OR NEW.source IS DISTINCT FROM OLD.source
               OR NEW.identity_level IS DISTINCT FROM OLD.identity_level
               OR NEW.actor_session_id IS DISTINCT FROM OLD.actor_session_id
               OR NEW.channel_message_id IS DISTINCT FROM OLD.channel_message_id
               OR NEW.attachment_id IS DISTINCT FROM OLD.attachment_id
               OR NEW.account_id IS DISTINCT FROM OLD.account_id THEN
                RAISE EXCEPTION 'texto, interpretación, autor, sesión y origen de una observación son inmutables'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$;
    """)
    # Un mensaje ya atribuido tampoco cambia la cuenta que lo envió.
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_message_reattribution() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.text IS DISTINCT FROM OLD.text OR NEW.inbound_event_id IS DISTINCT FROM OLD.inbound_event_id THEN
                RAISE EXCEPTION 'el contenido de un mensaje recibido es inmutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.author_id IS NOT NULL
               AND (NEW.author_id IS DISTINCT FROM OLD.author_id
                    OR NEW.account_id IS DISTINCT FROM OLD.account_id) THEN
                RAISE EXCEPTION 'un mensaje atribuido no puede reatribuirse'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$;
    """)


def downgrade() -> None:
    # Primero se reetiquetan los valores nuevos para poder restaurar las restricciones antiguas.
    op.execute("UPDATE channel_message SET attribution_basis = 'session_at_receipt' "
               "WHERE attribution_basis = 'authorized_account'")
    op.execute("UPDATE actor_session SET identity_level = 'declared' WHERE identity_level = 'account'")
    op.execute("UPDATE demand_observation SET identity_level = 'declared' WHERE identity_level = 'account'")

    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_raw_text_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.raw_text IS DISTINCT FROM OLD.raw_text OR NEW.parsed IS DISTINCT FROM OLD.parsed
               OR NEW.author_id IS DISTINCT FROM OLD.author_id
               OR NEW.interpretation IS DISTINCT FROM OLD.interpretation
               OR NEW.source IS DISTINCT FROM OLD.source
               OR NEW.identity_level IS DISTINCT FROM OLD.identity_level
               OR NEW.actor_session_id IS DISTINCT FROM OLD.actor_session_id
               OR NEW.channel_message_id IS DISTINCT FROM OLD.channel_message_id
               OR NEW.attachment_id IS DISTINCT FROM OLD.attachment_id THEN
                RAISE EXCEPTION 'texto, interpretación, autor, sesión y origen de una observación son inmutables'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION forbid_message_reattribution() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.text IS DISTINCT FROM OLD.text OR NEW.inbound_event_id IS DISTINCT FROM OLD.inbound_event_id THEN
                RAISE EXCEPTION 'el contenido de un mensaje recibido es inmutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.author_id IS NOT NULL AND NEW.author_id IS DISTINCT FROM OLD.author_id THEN
                RAISE EXCEPTION 'un mensaje atribuido no puede reatribuirse'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END $$;
    """)

    op.drop_constraint(op.f('ck_demand_observation_cuenta_coherente'), 'demand_observation', type_='check')
    op.drop_constraint(op.f('ck_demand_observation_nivel_valido'), 'demand_observation', type_='check')
    op.create_check_constraint(op.f('ck_demand_observation_nivel_valido'), 'demand_observation',
                               "identity_level IN ('api_user','declared','pin')")
    op.drop_constraint(op.f('fk_demand_observation_account_id_telegram_account'), 'demand_observation', type_='foreignkey')
    op.drop_column('demand_observation', 'account_id')

    op.drop_constraint(op.f('ck_channel_message_base_valida'), 'channel_message', type_='check')
    op.create_check_constraint(op.f('ck_channel_message_base_valida'), 'channel_message',
                               "attribution_basis IS NULL OR attribution_basis IN "
                               "('session_at_receipt','confirmed_after_identification')")
    op.drop_constraint(op.f('fk_channel_message_account_id_telegram_account'), 'channel_message', type_='foreignkey')
    op.drop_column('channel_message', 'account_id')

    op.drop_constraint(op.f('ck_actor_session_cuenta_coherente'), 'actor_session', type_='check')
    op.drop_constraint(op.f('ck_actor_session_nivel_valido'), 'actor_session', type_='check')
    op.create_check_constraint(op.f('ck_actor_session_nivel_valido'), 'actor_session',
                               "identity_level IN ('declared','pin')")
    op.drop_constraint(op.f('fk_actor_session_account_id_telegram_account'), 'actor_session', type_='foreignkey')
    op.drop_column('actor_session', 'account_id')

    op.drop_constraint(op.f('ck_channel_binding_cuenta_personal_obligatoria'), 'channel_binding', type_='check')
    op.drop_constraint(op.f('ck_channel_binding_modo_valido'), 'channel_binding', type_='check')
    op.drop_constraint(op.f('fk_channel_binding_account_id_telegram_account'), 'channel_binding', type_='foreignkey')
    op.drop_column('channel_binding', 'account_id')

    op.drop_index('ix_telegram_access_attempt_code', table_name='telegram_access_attempt')
    op.drop_index('ix_telegram_access_attempt_admin', table_name='telegram_access_attempt')
    op.drop_table('telegram_access_attempt')

    op.drop_constraint(op.f('fk_telegram_access_request_approved_account_id_telegram_account'),
                       'telegram_access_request', type_='foreignkey')
    op.drop_index('uq_telegram_access_request_pending_account', table_name='telegram_access_request',
                  postgresql_where=sa.text("status = 'pending'"))
    op.drop_index('ix_telegram_access_request_pending', table_name='telegram_access_request',
                  postgresql_where=sa.text("status = 'pending'"))
    op.drop_index('ix_telegram_access_request_org', table_name='telegram_access_request')
    op.drop_index('ix_telegram_account_person', table_name='telegram_account')
    op.drop_index('ix_telegram_account_org_status', table_name='telegram_account')
    op.drop_table('telegram_account')
    op.drop_table('telegram_access_request')
