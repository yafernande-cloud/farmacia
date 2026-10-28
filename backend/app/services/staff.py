"""Mantenedor de personal (API administrativa). Nunca elimina: desactiva, y todo cambio queda auditado.

Tres conceptos separados (ver app/core/staff.py): pertenecer, estar disponible y tener sesión activa.
Todo cambio que deja a una persona sin poder reportar (desactivarla o marcarla no disponible) REVOCA sus
sesiones activas en la misma transacción: el siguiente mensaje del teléfono compartido exigirá elegir otra identidad.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.core.permissions import Permission, Role, can_report, require
from app.core.staff import AbsenceReason, absence_label
from app.models import ActorSession, AppUser, DemandObservation
from app.services import sessions
from app.services.actors import Actor
from app.services.events import advisory_lock, emit

ASSIGNABLE_ROLES = {r.value for r in Role}


def _snap(u: AppUser) -> dict:
    return {"display_name": u.display_name, "roles": list(u.roles or []), "active": u.active, "available": u.available,
            "absence_reason": u.absence_reason, "absence_note": u.absence_note}  # fmt: skip


def _clean_name(name: str) -> str:
    n = " ".join((name or "").split())
    if not (1 <= len(n) <= 100) or any(ord(c) < 32 for c in n):
        raise DomainError(
            "INVALID_NAME", "El nombre debe tener entre 1 y 100 caracteres.", status_code=400
        )
    return n


async def _load(session: AsyncSession, actor: Actor, user_id: uuid.UUID) -> AppUser:
    u = (
        await session.execute(
            select(AppUser)
            .where(AppUser.id == user_id, AppUser.organization_id == actor.organization_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if u is None:
        raise not_found(
            "La persona"
        )  # también para personas de otra organización: sin filtrar su existencia
    return u


async def _assert_unique_name(session, org, name: str, exclude: uuid.UUID | None = None) -> None:
    q = select(AppUser.id).where(
        AppUser.organization_id == org,
        AppUser.active,
        func.lower(AppUser.display_name) == name.lower(),
    )
    if exclude:
        q = q.where(AppUser.id != exclude)
    if (await session.execute(q)).first():
        raise DomainError(
            "DUPLICATE_NAME", f"Ya hay una persona activa llamada «{name}».", status_code=409
        )


async def _assert_not_last_admin(session, org, user: AppUser) -> None:
    """No se puede desactivar a la última persona administradora activa (nadie podría volver a gestionar el personal)."""
    if Role.ADMIN.value not in (user.roles or []) or not user.active:
        return
    others = (
        await session.execute(
            select(AppUser).where(
                AppUser.organization_id == org, AppUser.active, AppUser.id != user.id
            )
        )
    ).scalars()
    if not any(Role.ADMIN.value in (o.roles or []) for o in others):
        raise DomainError(
            "LAST_ADMIN",
            "No se puede dejar la farmacia sin ninguna persona administradora activa.",
            status_code=422,
        )


async def _after_change(
    session, actor: Actor, u: AppUser, action: str, before: dict, now: datetime, *, revoke: bool
) -> int:
    u.updated_at = now
    await session.flush()
    revoked = (
        await sessions.revoke_person_sessions(
            session, actor.organization_id, u.id, now, by=actor.id
        )
        if revoke
        else 0
    )
    emit(session, org_id=actor.organization_id, actor_id=actor.id, action=action, entity_type="app_user", entity_id=u.id,
         event_type="staff.updated", aggregate_version=None,
         payload={"user_id": str(u.id), "sessions_revoked": revoked}, before=before, after=_snap(u))  # fmt: skip
    return revoked


async def list_staff(
    session: AsyncSession, actor: Actor, *, include_inactive: bool = True
) -> list[dict]:
    """Listado COMPLETO (también no disponibles y desactivadas), con los tres estados por separado."""
    require(actor.roles, Permission.MANAGE_STAFF)
    org = actor.organization_id
    q = select(AppUser).where(AppUser.organization_id == org)
    if not include_inactive:
        q = q.where(AppUser.active)
    users = list(
        (await session.execute(q.order_by(AppUser.active.desc(), AppUser.display_name))).scalars()
    )
    live = {
        s.auxiliary_id: s
        for s in (
            await session.execute(
                select(ActorSession).where(
                    ActorSession.organization_id == org, ActorSession.ended_at.is_(None)
                )
            )
        ).scalars()
    }
    hist = dict(
        (
            await session.execute(
                select(DemandObservation.author_id, func.count())
                .where(DemandObservation.organization_id == org)
                .group_by(DemandObservation.author_id)
            )
        ).all()
    )
    now = datetime.now(UTC)
    out = []
    for u in users:
        s = live.get(u.id)
        sess_active = bool(s and s.expires_at > now)
        out.append({
            "id": u.id, "display_name": u.display_name, "roles": list(u.roles or []),
            "member_active": u.active,  # pertenece a la farmacia
            "available": u.available, "absence_reason": u.absence_reason, "absence_label": absence_label(u.absence_reason),
            "absence_note": u.absence_note, "unavailable_since": u.unavailable_since,  # puede reportar o no
            "can_report": u.active and u.available and can_report(list(u.roles or [])),
            "telegram_session": {"active": sess_active, "expires_at": s.expires_at if sess_active else None},  # reporta AHORA
            "observations_authored": hist.get(u.id, 0),  # con historial no se elimina nunca
            "created_at": u.created_at, "updated_at": u.updated_at,
        })  # fmt: skip
    return out


async def create_staff(
    session: AsyncSession, actor: Actor, *, display_name: str, roles: list[str] | None = None
) -> AppUser:
    require(actor.roles, Permission.MANAGE_STAFF)
    org = actor.organization_id
    roles = list(dict.fromkeys(roles or [Role.AUXILIARY.value]))
    if not roles or set(roles) - ASSIGNABLE_ROLES:
        raise DomainError(
            "INVALID_ROLES",
            f"Roles válidos: {', '.join(sorted(ASSIGNABLE_ROLES))}.",
            status_code=400,
        )
    name = _clean_name(display_name)
    await advisory_lock(session, f"staff:{org}")
    await _assert_unique_name(session, org, name)
    u = AppUser(organization_id=org, display_name=name, roles=roles)
    session.add(u)
    await session.flush()
    emit(session, org_id=org, actor_id=actor.id, action="staff.created", entity_type="app_user", entity_id=u.id,
         event_type="staff.updated", aggregate_version=None, payload={"user_id": str(u.id)}, after=_snap(u))  # fmt: skip
    return u


async def rename_staff(
    session: AsyncSession, actor: Actor, user_id: uuid.UUID, display_name: str
) -> AppUser:
    require(actor.roles, Permission.MANAGE_STAFF)
    org = actor.organization_id
    await advisory_lock(session, f"staff:{org}")
    u = await _load(session, actor, user_id)
    name = _clean_name(display_name)
    if name == u.display_name:
        return u
    await _assert_unique_name(session, org, name, exclude=u.id if u.active else None)
    before = _snap(u)
    u.display_name = (
        name  # la autoría histórica se guarda por id: renombrar no la reasigna ni la altera
    )
    await _after_change(session, actor, u, "staff.renamed", before, datetime.now(UTC), revoke=False)
    return u


async def set_active(
    session: AsyncSession, actor: Actor, user_id: uuid.UUID, active: bool
) -> tuple[AppUser, int]:
    require(actor.roles, Permission.MANAGE_STAFF)
    org = actor.organization_id
    await advisory_lock(session, f"staff:{org}")
    u = await _load(session, actor, user_id)
    if u.active == active:
        return u, 0
    if active:
        await _assert_unique_name(session, org, u.display_name, exclude=u.id)
    else:
        await _assert_not_last_admin(session, org, u)
    before = _snap(u)
    u.active = active
    revoked = await _after_change(session, actor, u, "staff.activated" if active else "staff.deactivated", before,
                                  datetime.now(UTC), revoke=not active)  # fmt: skip
    return u, revoked


async def set_availability(
    session: AsyncSession, actor: Actor, user_id: uuid.UUID, *, available: bool,
    reason: str | None = None, note: str | None = None,
) -> tuple[AppUser, int]:  # fmt: skip
    require(actor.roles, Permission.MANAGE_STAFF)
    org = actor.organization_id
    await advisory_lock(session, f"staff:{org}")
    u = await _load(session, actor, user_id)
    now = datetime.now(UTC)
    if available:
        reason = note = None
    else:
        try:
            reason = AbsenceReason(reason).value
        except ValueError:
            raise DomainError(
                "ABSENCE_REASON_REQUIRED",
                "Indica el motivo: vacation, sick_leave, day_off u other.",
                status_code=400,
            ) from None
        note = (note or "").strip()[:200] or None
    before = _snap(u)
    if (u.available, u.absence_reason, u.absence_note) == (available, reason, note):
        return u, 0
    u.available, u.absence_reason, u.absence_note = available, reason, note
    u.unavailable_since = None if available else (u.unavailable_since or now)
    revoked = await _after_change(
        session, actor, u, "staff.availability_changed", before, now, revoke=not available
    )
    return u, revoked
