"""Acceso web del Cotizador (G1, D-130).

Un único perfil funcional: `admin`.

  * Google/Supabase identifica a la persona, pero NO concede acceso.
  * La persona **solicita** acceso; la solicitud no otorga nada.
  * Cualquier `admin` de la farmacia ve las pendientes en la campanita y aprueba o rechaza.
  * Aprobar crea el `app_user` con `roles = ['admin']`, `active` y `auth_subject` **en el mismo
    paso**, sin vencimiento (`web_access_expires_at = NULL`). Nunca enlaza ni sobrescribe una fila
    existente: si el nombre visible ya está activo, responde 409 y la administradora decide.
  * Telegram es un plano aparte: nada de esto crea cuentas, vínculos ni solicitudes del bot.

`superadmin` (`is_superadmin`) es control técnico de la propietaria, no un perfil funcional: ve las
solicitudes de todas las organizaciones, pero **no aprueba en nombre de una farmacia**. La aprobación
siempre la hace un `admin` de la organización dueña.

La organización de una solicitud sale de la configuración del servidor, nunca del cuerpo ni del
token. Toda decisión se audita en la misma transacción (`events.emit`).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import DomainError, not_found
from app.core.permissions import Permission, Role, require
from app.core.supabase_auth import JwtClaims
from app.models import AppUser, Organization, WebAccessRequest
from app.services.actors import Actor
from app.services.events import advisory_lock, emit
from app.services.provenance import org_kind, origin_for

MAX_SUBJECT = 100
MAX_EMAIL = 320
MAX_NAME = 100
MAX_NOTE = 500
_ROLES = {r.value for r in Role}


@dataclass(frozen=True)
class RequestRow:
    request: WebAccessRequest
    organization_name: str | None = None


@dataclass(frozen=True)
class PendingList:
    items: list[RequestRow]
    pending_count: int


@dataclass(frozen=True)
class Decision:
    request: WebAccessRequest
    user: AppUser | None


def _now() -> datetime:
    return datetime.now(UTC)


def _clean_name(raw: str, email: str) -> str:
    name = " ".join((raw or "").split())[:MAX_NAME].strip()
    if not name:
        name = email.split("@", 1)[0][:MAX_NAME]
    return name


def _clean_subject(raw: str) -> str:
    subject = (raw or "").strip()
    if not subject or len(subject) > MAX_SUBJECT:
        raise DomainError(
            "INVALID_IDENTITY", "La identidad de la cuenta no es válida.", status_code=401
        )
    return subject


def _clean_email(raw: str | None) -> str:
    email = (raw or "").strip().lower()[:MAX_EMAIL]
    if not email or "@" not in email:
        raise DomainError(
            "EMAIL_REQUIRED",
            "La cuenta no informa un correo válido. Inicia sesión con Google.",
            status_code=403,
        )
    return email


def _request_payload(r: WebAccessRequest, *, action: str) -> dict:
    # Sin correo ni nombre en el payload del outbox: el detalle vive en la fila y la auditoría.
    return {"request_id": str(r.id), "action": action, "status": r.status}


async def request_access(
    session: AsyncSession, claims: JwtClaims, *, organization_id: uuid.UUID
) -> WebAccessRequest | None:
    """Registra (o reutiliza) la solicitud pendiente de una persona. Nunca revela su estado.

    Devuelve la solicitud pendiente, o `None` si ya existe una persona con ese `sub` (no se crea
    ninguna fila nueva y la respuesta pública es la misma).
    """
    subject = _clean_subject(claims.subject)
    email = _clean_email(claims.email)
    name = _clean_name(claims.display_name or "", email)
    kind = await org_kind(session, organization_id)
    if get_settings().auth_mode == "demo_header" and kind != "simulation":
        raise DomainError(
            "STRONG_AUTH_REQUIRED",
            "Solicitar acceso a una organización real exige una sesión de Supabase Auth.",
            status_code=403,
        )

    await advisory_lock(session, f"web-access-request:{organization_id}:{subject}")
    existing_user = (
        await session.execute(select(AppUser.id).where(AppUser.auth_subject == subject))
    ).first()
    if existing_user is not None:
        return None

    pending = (
        await session.execute(
            select(WebAccessRequest).where(
                WebAccessRequest.organization_id == organization_id,
                WebAccessRequest.auth_subject == subject,
                WebAccessRequest.status == "pending",
            )
        )
    ).scalar_one_or_none()
    if pending is not None:
        return pending

    now = _now()
    request = WebAccessRequest(
        organization_id=organization_id,
        auth_subject=subject,
        email=email,
        display_name=name,
        status="pending",
        requested_at=now,
        data_origin=origin_for(kind),
        created_at=now,
        updated_at=now,
    )
    session.add(request)
    await session.flush()
    emit(
        session,
        org_id=organization_id,
        actor_id=None,
        action="web_access.requested",
        entity_type="web_access_request",
        entity_id=request.id,
        event_type="web_access.requested",
        aggregate_version=None,
        payload=_request_payload(request, action="requested"),
    )
    return request


async def list_pending(session: AsyncSession, actor: Actor) -> PendingList:
    """Bandeja de la campanita. Un `admin` ve su organización; la superadministradora, todas."""
    require(actor.roles, Permission.MANAGE_WEB_ACCESS)
    query = select(WebAccessRequest, Organization.name).join(
        Organization, Organization.id == WebAccessRequest.organization_id
    )
    if not actor.is_superadmin:
        query = query.where(WebAccessRequest.organization_id == actor.organization_id)
    query = query.where(WebAccessRequest.status == "pending").order_by(
        WebAccessRequest.requested_at
    )
    rows = (await session.execute(query)).all()
    items = [
        RequestRow(r, name if actor.is_superadmin else None)
        for r, name in rows
    ]
    return PendingList(items=items, pending_count=len(items))


async def _load_pending(
    session: AsyncSession, actor: Actor, request_id: uuid.UUID
) -> WebAccessRequest:
    """Solicitud pendiente de la organización del `Actor`, con bloqueo de fila."""
    require(actor.roles, Permission.MANAGE_WEB_ACCESS)
    await advisory_lock(session, f"web-access:{actor.organization_id}")
    request = (
        await session.execute(
            select(WebAccessRequest)
            .where(
                WebAccessRequest.id == request_id,
                WebAccessRequest.organization_id == actor.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if request is None:
        raise not_found("La solicitud de acceso")
    if request.status != "pending":
        raise DomainError(
            "REQUEST_ALREADY_DECIDED",
            "La solicitud ya fue aprobada o rechazada.",
            status_code=409,
        )
    return request


async def approve(
    session: AsyncSession, actor: Actor, request_id: uuid.UUID
) -> Decision:
    """Aprueba y crea la persona con el único perfil funcional: `admin`."""
    request = await _load_pending(session, actor, request_id)
    if (
        await session.execute(select(AppUser.id).where(AppUser.auth_subject == request.auth_subject))
    ).first():
        raise DomainError(
            "USER_ALREADY_EXISTS",
            "Esa identidad ya está vinculada a una persona. Usa el enlace existente.",
            status_code=409,
        )
    duplicate = (
        await session.execute(
            select(AppUser.id).where(
                AppUser.organization_id == request.organization_id,
                AppUser.active,
                func.lower(AppUser.display_name) == request.display_name.lower(),
            )
        )
    ).first()
    if duplicate:
        raise DomainError(
            "DUPLICATE_NAME",
            f"Ya hay una persona activa llamada «{request.display_name}».",
            status_code=409,
        )

    now = _now()
    user = AppUser(
        organization_id=request.organization_id,
        display_name=request.display_name,
        roles=[Role.ADMIN.value],
        auth_subject=request.auth_subject,
        email=request.email,
        active=True,
        available=True,
        web_access_expires_at=None,
        web_access_granted_by=actor.id,
        web_access_granted_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    await session.flush()
    request.status = "approved"
    request.decided_at = now
    request.decided_by = actor.id
    request.updated_at = now
    await session.flush()
    emit(
        session,
        org_id=request.organization_id,
        actor_id=actor.id,
        action="web_access.approved",
        entity_type="web_access_request",
        entity_id=request.id,
        event_type="web_access.decided",
        aggregate_version=None,
        payload={**_request_payload(request, action="approved"), "user_id": str(user.id)},
        after={"roles": list(user.roles), "user_id": str(user.id)},
    )
    return Decision(request=request, user=user)


async def reject(
    session: AsyncSession, actor: Actor, request_id: uuid.UUID, note: str | None = None
) -> Decision:
    request = await _load_pending(session, actor, request_id)
    text = " ".join((note or "").split()).strip()
    if len(text) > MAX_NOTE:
        raise DomainError(
            "NOTE_TOO_LONG", "El motivo no puede superar 500 caracteres.", status_code=422
        )
    now = _now()
    request.status = "rejected"
    request.decided_at = now
    request.decided_by = actor.id
    request.decision_note = text or None
    request.updated_at = now
    await session.flush()
    emit(
        session,
        org_id=request.organization_id,
        actor_id=actor.id,
        action="web_access.rejected",
        entity_type="web_access_request",
        entity_id=request.id,
        event_type="web_access.decided",
        aggregate_version=None,
        payload=_request_payload(request, action="rejected"),
        after={"decision_note": request.decision_note},
    )
    return Decision(request=request, user=None)


def _require_superadmin(actor: Actor) -> None:
    if not actor.is_superadmin:
        raise DomainError(
            "FORBIDDEN", "Esta operación es solo de la superadministración técnica.", status_code=403
        )


async def _load_user(session: AsyncSession, user_id: uuid.UUID) -> AppUser:
    user = (
        await session.execute(select(AppUser).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise not_found("La persona")
    return user


async def grant_web_access(
    session: AsyncSession,
    actor: Actor,
    user_id: uuid.UUID,
    *,
    roles: list[str] | None = None,
    expires_at: datetime | None = None,
) -> AppUser:
    """Otorga o ajusta el acceso web de una persona. Solo la superadministradora (D-119/D-130)."""
    _require_superadmin(actor)
    user = await _load_user(session, user_id)
    if user.is_superadmin and user.id != actor.id:
        raise DomainError(
            "SUPERADMIN_PROTECTED",
            "La superadministradora no puede ser modificada por otra cuenta.",
            status_code=403,
        )
    if roles is not None:
        cleaned = sorted({r for r in roles})
        if not cleaned or any(r not in _ROLES for r in cleaned):
            raise DomainError(
                "INVALID_ROLES", "Los roles deben ser los existentes y no pueden quedar vacíos.", status_code=422
            )
        user.roles = cleaned
    now = _now()
    user.web_access_expires_at = expires_at
    user.web_access_granted_by = actor.id
    user.web_access_granted_at = now
    user.updated_at = now
    await session.flush()
    emit(
        session,
        org_id=user.organization_id,
        actor_id=actor.id,
        action="web_access.granted",
        entity_type="app_user",
        entity_id=user.id,
        event_type="web_access.granted",
        aggregate_version=None,
        payload={"user_id": str(user.id)},
        after={
            "roles": list(user.roles),
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )
    return user


async def revoke_web_access(session: AsyncSession, actor: Actor, user_id: uuid.UUID) -> AppUser:
    """Revoca SOLO la web; no toca Telegram ni `active` ni el historial."""
    _require_superadmin(actor)
    user = await _load_user(session, user_id)
    if user.is_superadmin:
        raise DomainError(
            "SUPERADMIN_PROTECTED",
            "La superadministradora no puede revocarse a sí misma el acceso web.",
            status_code=403,
        )
    now = _now()
    user.web_access_expires_at = now
    user.updated_at = now
    await session.flush()
    emit(
        session,
        org_id=user.organization_id,
        actor_id=actor.id,
        action="web_access.revoked",
        entity_type="app_user",
        entity_id=user.id,
        event_type="web_access.revoked",
        aggregate_version=None,
        payload={"user_id": str(user.id)},
    )
    return user


async def set_superadmin(session: AsyncSession, user_id: uuid.UUID) -> AppUser:
    """Marca la superadministradora técnica (uso único, auditado con actor nulo, D-119)."""
    user = await _load_user(session, user_id)
    if not user.active:
        raise DomainError(
            "INACTIVE_USER", "La superadministradora debe estar activa.", status_code=422
        )
    existing = (
        await session.execute(
            select(AppUser.id).where(
                AppUser.is_superadmin, AppUser.active, AppUser.id != user.id
            )
        )
    ).first()
    if existing:
        raise DomainError(
            "SUPERADMIN_ALREADY_SET",
            "Ya existe una superadministradora activa. Es una operación de uso único.",
            status_code=409,
        )
    if user.is_superadmin:
        return user
    user.is_superadmin = True
    await session.flush()
    emit(
        session,
        org_id=user.organization_id,
        actor_id=None,
        action="app_user.superadmin_set",
        entity_type="app_user",
        entity_id=user.id,
        event_type="app_user.superadmin_set",
        aggregate_version=None,
        payload={"user_id": str(user.id)},
    )
    return user
