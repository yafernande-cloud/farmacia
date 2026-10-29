"""Enlace de la identidad web (Supabase Auth) con una persona del personal.

* `app_user.auth_subject` guarda el `sub` del JWT: es el **único** puente entre «quién inició sesión en
  Supabase» y «qué puede hacer en esta farmacia». Es opcional y único en toda la base.
* Google y correo/contraseña deben ser DOS identidades del MISMO usuario de Supabase (mismo `sub`).
  Si terminan en dos `sub` distintos, solo uno puede enlazarse y el otro recibe 403
  `USER_NOT_PROVISIONED`: el fallo es seguro, no concede permisos (D-064, docs/AUTH_SUPABASE.md).
* Aquí NO se crea personal, ni organizaciones, ni usuarios de Supabase, ni se envía correo alguno: solo
  se rellena o se vacía un campo de una fila existente, con permiso `manage_identities` (rol admin).
* Vincular o desvincular NO cambia los roles: los permisos siguen saliendo de `app_user.roles`.
* Cada cambio deja auditoría y evento en la misma transacción del llamador.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.core.permissions import Permission, require
from app.models import AppUser
from app.services.actors import Actor
from app.services.events import advisory_lock, emit

SUBJECT_MAX = 100


def normalize_subject(raw: str | None) -> str:
    """El `sub` es un identificador opaco: sin espacios ni caracteres de control, 1..100."""
    subject = (raw or "").strip()
    if not subject or len(subject) > SUBJECT_MAX or not subject.isprintable() or any(
        c.isspace() for c in subject
    ):
        raise DomainError(
            "INVALID_SUBJECT",
            f"El identificador (sub) debe tener entre 1 y {SUBJECT_MAX} caracteres visibles, "
            "sin espacios.",
            status_code=400,
        )
    return subject


async def _person(
    session: AsyncSession, actor: Actor, user_id: uuid.UUID, *, lock: bool = True
) -> AppUser:
    statement = (
        select(AppUser)
        .where(AppUser.id == user_id, AppUser.organization_id == actor.organization_id)
    )
    if lock:
        statement = statement.with_for_update()
    person = (await session.execute(statement)).scalar_one_or_none()
    if person is None:
        # También para personas de otra organización: no se filtra su existencia.
        raise not_found("La persona")
    return person


async def link_auth_subject(
    session: AsyncSession, actor: Actor, user_id: uuid.UUID, *, subject: str
) -> dict:
    """Enlaza el `sub` a una persona ACTIVA de la organización del actor. No reemplaza enlaces."""
    require(actor.roles, Permission.MANAGE_IDENTITIES)
    clean = normalize_subject(subject)
    await advisory_lock(session, f"auth_subject:{clean}")
    person = await _person(session, actor, user_id)
    if not person.active:
        raise DomainError(
            "USER_INACTIVE",
            "Solo se enlaza la identidad de una persona activa. Actívala primero.",
            status_code=409,
        )
    if person.auth_subject == clean:
        return {"user_id": person.id, "display_name": person.display_name,
                "auth_subject": clean, "changed": False}  # fmt: skip
    if person.auth_subject is not None:
        raise DomainError(
            "AUTH_SUBJECT_ALREADY_SET",
            "Esa persona ya tiene una identidad enlazada. Desvincúlala antes de cambiarla.",
            status_code=409,
        )
    if (
        await session.execute(select(AppUser.id).where(AppUser.auth_subject == clean))
    ).scalars().first() is not None:
        # No se indica de quién es: el `sub` puede pertenecer a otra organización.
        raise DomainError(
            "AUTH_SUBJECT_IN_USE",
            "Ese identificador ya está enlazado a otra persona.",
            status_code=409,
        )

    person.auth_subject = clean
    await session.flush()
    emit(session, org_id=actor.organization_id, actor_id=actor.id,
         action="auth_subject.linked", entity_type="app_user", entity_id=person.id,
         event_type="identity.linked", aggregate_version=None,
         payload={"user_id": str(person.id), "auth_subject": clean},
         before={"auth_subject": None}, after={"auth_subject": clean})  # fmt: skip
    return {"user_id": person.id, "display_name": person.display_name, "auth_subject": clean,
            "changed": True}  # fmt: skip


async def unlink_auth_subject(
    session: AsyncSession, actor: Actor, user_id: uuid.UUID
) -> dict:
    """Quita el enlace. La persona y su historial se conservan; pierde el acceso web hasta reenlazar."""
    require(actor.roles, Permission.MANAGE_IDENTITIES)
    # Mismo orden de candados que `link_auth_subject` (primero el `sub`, después la fila).
    current = await _person(session, actor, user_id, lock=False)
    if current.auth_subject is None:
        raise DomainError(
            "AUTH_SUBJECT_NOT_SET", "Esa persona no tiene una identidad enlazada.", status_code=409
        )
    await advisory_lock(session, f"auth_subject:{current.auth_subject}")
    person = await _person(session, actor, user_id)
    if person.auth_subject is None:
        raise DomainError(
            "AUTH_SUBJECT_NOT_SET", "Esa persona no tiene una identidad enlazada.", status_code=409
        )
    previous = person.auth_subject
    person.auth_subject = None
    await session.flush()
    emit(session, org_id=actor.organization_id, actor_id=actor.id,
         action="auth_subject.unlinked", entity_type="app_user", entity_id=person.id,
         event_type="identity.unlinked", aggregate_version=None,
         payload={"user_id": str(person.id), "auth_subject": previous},
         before={"auth_subject": previous}, after={"auth_subject": None})  # fmt: skip
    return {"user_id": person.id, "display_name": person.display_name, "auth_subject": None,
            "changed": True}  # fmt: skip


async def list_auth_subjects(session: AsyncSession, actor: Actor) -> list[dict]:
    """Estado del enlace en la organización del actor. No incluye otras organizaciones ni correos."""
    require(actor.roles, Permission.MANAGE_IDENTITIES)
    rows = (
        (
            await session.execute(
                select(AppUser)
                .where(AppUser.organization_id == actor.organization_id)
                .order_by(AppUser.display_name)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "user_id": u.id,
            "display_name": u.display_name,
            "roles": list(u.roles or []),
            "active": u.active,
            "auth_subject": u.auth_subject,
            "linked": u.auth_subject is not None,
        }
        for u in rows
    ]
