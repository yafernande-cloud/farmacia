"""Sesiones de auxiliar en un dispositivo compartido (HLD §3.1). Sin ninguna dependencia de Telegram.

* La selección por nombre es identificación DECLARADA, no autenticación fuerte (`identity_level`).
* A lo más una sesión activa por chat (índice único parcial + bloqueo por chat).
* Expiración por inactividad, deslizante; una sesión expirada NO identifica a nadie.
* Todo cambio deja auditoría y evento (selección, cambio, expiración, cierre).
* PIN individual: la verificación con bloqueo por intentos ya existe aquí; el adaptador la usará en el piloto.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.permissions import can_report
from app.core.security import verify_pin
from app.core.staff import absence_label
from app.models import ActorSession, AppUser, AuxiliaryProfile, ChannelBinding
from app.services.events import advisory_lock, emit

MAX_PIN_ATTEMPTS = 5
PIN_LOCK = timedelta(minutes=10)


def utcnow() -> datetime:
    return datetime.now(UTC)


async def get_binding(session: AsyncSession, provider: str, chat_id: str) -> ChannelBinding | None:
    return (
        await session.execute(
            select(ChannelBinding).where(
                ChannelBinding.provider == provider,
                ChannelBinding.external_chat_id == str(chat_id),
                ChannelBinding.status == "active",
            )
        )
    ).scalar_one_or_none()


def ineligibility(user: AppUser | None) -> DomainError | None:
    """¿Por qué esta persona NO puede ser la identidad activa? None = puede. Se valida en el BACKEND en cada
    selección y en cada uso de una sesión; ocultar botones no basta."""
    if user is None:
        return DomainError(
            "PERSON_NOT_FOUND", "Esa persona no existe en la farmacia.", status_code=404
        )
    if not user.active:
        return DomainError(
            "PERSON_INACTIVE",
            f"{user.display_name} ya no está activa en la farmacia.",
            status_code=403,
        )
    if not user.available:
        why = absence_label(user.absence_reason) or "no disponible"
        return DomainError(
            "PERSON_UNAVAILABLE",
            f"{user.display_name} no está disponible para reportar ({why}).",
            status_code=403,
        )
    if not can_report(list(user.roles or [])):
        return DomainError(
            "NOT_A_REPORTER",
            f"{user.display_name} no tiene un rol que permita reportar faltantes.",
            status_code=403,
        )
    return None


async def list_reporters(session: AsyncSession, org: uuid.UUID) -> list[AppUser]:
    """Quiénes aparecen en los botones: SOLO personas activas, disponibles y con rol para reportar,
    leídas de PostgreSQL en cada consulta (nada definido en el código)."""
    rows = (
        await session.execute(
            select(AppUser).where(AppUser.organization_id == org, AppUser.active, AppUser.available)
            .order_by(AppUser.display_name)
        )
    ).scalars()  # fmt: skip
    return [u for u in rows if can_report(list(u.roles or []))]


async def _lock_chat(session: AsyncSession, binding: ChannelBinding) -> None:
    await advisory_lock(session, f"chan:{binding.id}")


async def _active_row(session: AsyncSession, binding: ChannelBinding) -> ActorSession | None:
    return (
        await session.execute(
            select(ActorSession)
            .where(ActorSession.channel_binding_id == binding.id, ActorSession.ended_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()


def _end(session, binding, sess: ActorSession, when: datetime, reason: str, actor_id=None) -> None:
    sess.ended_at, sess.end_reason = when, reason
    emit(session, org_id=binding.organization_id, actor_id=actor_id, action=f"session.{reason}",
         entity_type="actor_session", entity_id=sess.id, event_type=f"session.{reason}",
         aggregate_version=None,
         payload={"session_id": str(sess.id), "auxiliary_id": str(sess.auxiliary_id),
                  "binding_id": str(binding.id),
                  "account_id": str(sess.account_id) if sess.account_id else None})  # fmt: skip


async def active_session(
    session: AsyncSession, binding: ChannelBinding, now: datetime, idle: timedelta
) -> ActorSession | None:
    """La sesión vigente o None. Si expiró por inactividad, se cierra y NO identifica a nadie."""
    await _lock_chat(session, binding)
    sess = await _active_row(session, binding)
    if sess is None:
        return None
    if now >= sess.expires_at:
        _end(session, binding, sess, sess.expires_at, "expired")
        await session.flush()
        return None
    person = await session.get(AppUser, sess.auxiliary_id)
    if (
        ineligibility(person) is not None
    ):  # dejó de estar disponible/activa: la sesión ya no identifica a nadie
        _end(session, binding, sess, now, "revoked")
        await session.flush()
        return None
    return sess


async def touch(session: AsyncSession, sess: ActorSession, now: datetime, idle: timedelta) -> None:
    sess.last_activity_at, sess.expires_at = now, now + idle
    await session.flush()


async def verify_pin_attempt(
    session: AsyncSession, user_id: uuid.UUID, org: uuid.UUID, pin: str | None, now: datetime
) -> DomainError | None:
    """Verifica un PIN y registra el intento. DEVUELVE el error en vez de lanzarlo: así quien llama
    confirma la transacción y el contador de intentos fallidos persiste (lanzar haría rollback y
    permitiría fuerza bruta sin límite). El PIN jamás se registra."""
    prof = (
        await session.execute(
            select(AuxiliaryProfile).where(AuxiliaryProfile.user_id == user_id, AuxiliaryProfile.organization_id == org)
            .with_for_update()
        )
    ).scalar_one_or_none()  # fmt: skip
    if prof is None or not prof.pin_hash:
        return DomainError(
            "PIN_NOT_SET", "Este auxiliar no tiene PIN configurado.", status_code=403
        )
    if prof.pin_locked_until and now < prof.pin_locked_until:
        return DomainError(
            "PIN_LOCKED", "Demasiados intentos. Espera unos minutos.", status_code=429
        )
    if not pin or not verify_pin(pin, prof.pin_hash):
        prof.pin_failed_attempts += 1
        if prof.pin_failed_attempts >= MAX_PIN_ATTEMPTS:
            prof.pin_locked_until, prof.pin_failed_attempts = now + PIN_LOCK, 0
        await session.flush()
        return DomainError("PIN_INVALID", "PIN incorrecto.", status_code=403)
    prof.pin_failed_attempts, prof.pin_locked_until = 0, None
    await session.flush()
    return None


async def select_auxiliary(
    session: AsyncSession,
    binding: ChannelBinding,
    user_id: uuid.UUID,
    now: datetime,
    idle: timedelta,
    *,
    event_id: uuid.UUID | None = None,
    pin_verified: bool = False,
    identity_level: str | None = None,
    account_id: uuid.UUID | None = None,
) -> ActorSession:
    """Identifica al auxiliar activo (cierra la sesión anterior).

    `declared` salvo que el llamador haya verificado un PIN (`pin`) o la cuenta personal ya esté
    autorizada (`account`, identidad fija sin selección). `account_id` deja trazabilidad de la cuenta
    de Telegram que envía, también en el teléfono compartido.
    """
    await _lock_chat(session, binding)
    org = binding.organization_id
    user = (
        await session.execute(
            select(AppUser).where(AppUser.id == user_id, AppUser.organization_id == org)
        )
    ).scalar_one_or_none()
    err = ineligibility(user)
    if err is not None:  # un botón antiguo no puede elegir a quien ya no está disponible
        raise err
    assert user is not None
    level = identity_level or ("pin" if pin_verified else "declared")
    if level not in ("declared", "pin", "account"):
        raise DomainError("INVALID_IDENTITY_LEVEL", "Nivel de identidad inválido.", status_code=400)
    if level == "account" and account_id is None:
        raise DomainError(
            "ACCOUNT_REQUIRED", "Una identidad de cuenta requiere la cuenta autorizada.", status_code=400
        )
    prev = await _active_row(session, binding)
    changed = False
    if prev is not None:
        changed = prev.auxiliary_id != user_id and now < prev.expires_at
        if now >= prev.expires_at:
            _end(session, binding, prev, prev.expires_at, "expired")
        else:
            _end(session, binding, prev, now, "replaced", actor_id=user_id)
        await session.flush()
    sess = ActorSession(
        organization_id=org, channel_binding_id=binding.id, auxiliary_id=user_id, identity_level=level,
        account_id=account_id, started_at=now, last_activity_at=now, expires_at=now + idle,
        selected_by_event_id=event_id,
    )  # fmt: skip
    session.add(sess)
    await session.flush()
    emit(session, org_id=org, actor_id=user_id, action="session.changed" if changed else "session.selected",
         entity_type="actor_session", entity_id=sess.id, event_type="session.selected",
         aggregate_version=None,
         payload={"session_id": str(sess.id), "auxiliary_id": str(user_id), "identity_level": level,
                  "account_id": str(account_id) if account_id else None,
                  "previous_auxiliary_id": str(prev.auxiliary_id) if prev else None},
         after={"auxiliary": user.display_name, "identity_level": level, "declared_not_authenticated": level == "declared"})  # fmt: skip
    return sess


async def end_session(session: AsyncSession, binding: ChannelBinding, now: datetime) -> bool:
    await _lock_chat(session, binding)
    sess = await _active_row(session, binding)
    if sess is None:
        return False
    _end(session, binding, sess, now, "manual", actor_id=sess.auxiliary_id)
    await session.flush()
    return True


async def expire_stale(session: AsyncSession, now: datetime) -> int:
    """Barrido periódico: cierra sesiones vencidas aunque nadie escriba (auditoría fiel)."""
    rows = (
        await session.execute(
            select(ActorSession, ChannelBinding)
            .join(ChannelBinding, ChannelBinding.id == ActorSession.channel_binding_id)
            .where(ActorSession.ended_at.is_(None), ActorSession.expires_at <= now)
            .with_for_update(of=ActorSession, skip_locked=True)
        )
    ).all()
    for sess, binding in rows:
        _end(session, binding, sess, sess.expires_at, "expired")
    await session.flush()
    return len(rows)


async def revoke_person_sessions(
    session: AsyncSession,
    org: uuid.UUID,
    user_id: uuid.UUID,
    now: datetime,
    *,
    by: uuid.UUID | None = None,
) -> int:
    """Cierra TODA sesión activa de esa persona (en cualquier chat) con motivo `revoked` y la audita."""
    rows = (
        await session.execute(
            select(ActorSession, ChannelBinding)
            .join(ChannelBinding, ChannelBinding.id == ActorSession.channel_binding_id)
            .where(
                ActorSession.organization_id == org,
                ActorSession.auxiliary_id == user_id,
                ActorSession.ended_at.is_(None),
            )
            .with_for_update(of=ActorSession)
        )
    ).all()
    for sess, binding in rows:
        await advisory_lock(session, f"chan:{binding.id}")
        _end(session, binding, sess, now, "revoked", actor_id=by)
    await session.flush()
    return len(rows)


async def revoke_binding_sessions(
    session: AsyncSession,
    binding: ChannelBinding,
    now: datetime,
    *,
    by: uuid.UUID | None = None,
) -> int:
    """Cierra la sesión activa de un chat (al revocar la cuenta que lo autorizó)."""
    await _lock_chat(session, binding)
    rows = list(
        (
            await session.execute(
                select(ActorSession)
                .where(
                    ActorSession.channel_binding_id == binding.id,
                    ActorSession.ended_at.is_(None),
                )
                .with_for_update()
            )
        ).scalars()
    )
    for sess in rows:
        _end(session, binding, sess, now, "revoked", actor_id=by)
    await session.flush()
    return len(rows)
