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

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import DomainError, not_found
from app.core.permissions import Permission, Role, require
from app.core.supabase_auth import JwtClaims
from app.models import AppUser, WebAccessGrant, WebAccessRequest
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
class IdentityStatus:
    """Estado de una identidad que todavía no tiene acceso. Solo consulta, nunca crea."""

    status: str
    requested_at: datetime | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True)
class Decision:
    request: WebAccessRequest
    user: AppUser | None


@dataclass(frozen=True)
class WebUserRow:
    """Persona con autorización web vigente. La autorización es la fila, no un rol."""

    user: AppUser
    grant: WebAccessGrant


@dataclass(frozen=True)
class RemovedAccess:
    user: AppUser
    removed: bool


def _now() -> datetime:
    return datetime.now(UTC)


def _vigente(grant: WebAccessGrant | None, now: datetime) -> bool:
    return grant is not None and (grant.expires_at is None or grant.expires_at > now)


async def _grant(
    session: AsyncSession, user_id: uuid.UUID, *, lock: bool = False
) -> WebAccessGrant | None:
    query = select(WebAccessGrant).where(WebAccessGrant.app_user_id == user_id)
    if lock:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


def _mirror_legacy_columns(
    user: AppUser, *, expires_at: datetime | None, granted_by: uuid.UUID | None, now: datetime
) -> None:
    """Compatibilidad de transición: mantiene las columnas antiguas como respaldo de rollback.

    **Nunca conceden acceso**: `_actor_from_jwt` consulta solo `web_access_grant`. Al eliminar se
    escribe un vencimiento pasado para que, si se revierte el código, la persona siga sin acceso.
    """
    user.web_access_expires_at = expires_at
    user.web_access_granted_by = granted_by
    user.web_access_granted_at = now if granted_by is not None else None
    user.updated_at = now


async def _upsert_grant(
    session: AsyncSession,
    *,
    user: AppUser,
    actor_id: uuid.UUID | None,
    expires_at: datetime | None,
    request_id: uuid.UUID | None = None,
) -> WebAccessGrant:
    """Crea o actualiza la autorización vigente de una persona, con la identidad intacta."""
    now = _now()
    grant = await _grant(session, user.id, lock=True)
    if grant is None:
        grant = WebAccessGrant(
            app_user_id=user.id,
            organization_id=user.organization_id,
            request_id=request_id,
            granted_by=actor_id,
            granted_at=now,
            expires_at=expires_at,
        )
        session.add(grant)
    else:
        grant.request_id = request_id or grant.request_id
        grant.granted_by = actor_id
        grant.granted_at = now
        grant.expires_at = expires_at
    _mirror_legacy_columns(user, expires_at=expires_at, granted_by=actor_id, now=now)
    await session.flush()
    return grant


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
        await session.execute(
            select(AppUser).where(AppUser.auth_subject == subject)
        )
    ).scalar_one_or_none()
    if existing_user is not None:
        # Una identidad de otra organización no crea solicitudes aquí (no se mezclan universos).
        if existing_user.organization_id != organization_id:
            return None
        # Ya tiene autorización vigente: no hay nada que solicitar.
        if _vigente(await _grant(session, existing_user.id), _now()):
            return None
        # Fue eliminada o su autorización venció: puede volver a solicitar explícitamente.

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


async def status_for_identity(
    session: AsyncSession, claims: JwtClaims, *, organization_id: uuid.UUID
) -> IdentityStatus:
    """Estado de la identidad autenticada, sin otorgar acceso ni crear una solicitud.

    La identidad sale del JWT y la organización de la configuración del servidor, nunca del
    cuerpo. `approved` exige una **autorización vigente** en `web_access_grant`, no solo que la
    persona exista: tras eliminar el acceso, la pantalla debe ofrecer una solicitud nueva.
    """
    subject = _clean_subject(claims.subject)
    linked = (
        await session.execute(select(AppUser).where(AppUser.auth_subject == subject))
    ).scalar_one_or_none()
    if linked is not None:
        if linked.organization_id != organization_id:
            return IdentityStatus(status="none")
        grant = await _grant(session, linked.id)
        now = _now()
        if _vigente(grant, now):
            return IdentityStatus(status="approved")
        # Autorización eliminada o vencida: la solicitud anterior aprobada es historial.
        # Se informa `removed`/`expired` para que la pantalla pida una acción explícita.
        estado = "expired" if grant is not None else "removed"
        row = (
            await session.execute(
                select(WebAccessRequest)
                .where(
                    WebAccessRequest.organization_id == organization_id,
                    WebAccessRequest.auth_subject == subject,
                )
                .order_by(
                    WebAccessRequest.requested_at.desc(), WebAccessRequest.created_at.desc()
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None and row.status in ("pending", "rejected"):
            return IdentityStatus(
                status=row.status, requested_at=row.requested_at, decided_at=row.decided_at
            )
        return IdentityStatus(status=estado)

    row = (
        await session.execute(
            select(WebAccessRequest)
            .where(
                WebAccessRequest.organization_id == organization_id,
                WebAccessRequest.auth_subject == subject,
            )
            .order_by(
                WebAccessRequest.requested_at.desc(), WebAccessRequest.created_at.desc()
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return IdentityStatus(status="none")
    return IdentityStatus(
        status=row.status, requested_at=row.requested_at, decided_at=row.decided_at
    )


async def list_pending(session: AsyncSession, actor: Actor) -> PendingList:
    """Bandeja de la campanita, siempre acotada a la organización de quien administra.

    La superadministradora es un control técnico: no ve ni aprueba solicitudes de otra farmacia,
    porque aprobar en su nombre no le corresponde (D-130) y mezclaría universos separados.
    """
    require(actor.roles, Permission.MANAGE_WEB_ACCESS)
    rows = (
        (
            await session.execute(
                select(WebAccessRequest)
                .where(
                    WebAccessRequest.organization_id == actor.organization_id,
                    WebAccessRequest.status == "pending",
                )
                .order_by(WebAccessRequest.requested_at)
            )
        )
        .scalars()
        .all()
    )
    items = [RequestRow(r) for r in rows]
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
    """Aprueba y habilita el acceso web **sin duplicar la identidad**.

    Identidad nueva: crea la persona con el único perfil funcional (`admin`) y su autorización.
    Identidad que ya existe en la organización (por ejemplo, tras eliminar su acceso): conserva la
    persona, su `auth_subject`, sus roles y toda su historia de Telegram; solo crea o renueva la
    autorización web. Nunca agrega `admin` a una persona existente: eso cambiaría sus permisos de
    Telegram y debe decidirse explícitamente, no como efecto colateral de aprobar la web.
    """
    request = await _load_pending(session, actor, request_id)
    existing = (
        await session.execute(
            select(AppUser)
            .where(AppUser.auth_subject == request.auth_subject)
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = _now()

    if existing is not None:
        if existing.organization_id != request.organization_id:
            raise DomainError(
                "USER_ALREADY_EXISTS",
                "Esa identidad ya está vinculada a una persona de otra organización.",
                status_code=409,
            )
        if not existing.active:
            raise DomainError(
                "USER_INACTIVE",
                "La persona está desactivada. Actívala antes de darle acceso web.",
                status_code=409,
            )
        if Role.ADMIN.value not in (existing.roles or []):
            raise DomainError(
                "WEB_ADMIN_ROLE_REQUIRED",
                "La identidad ya existe sin el perfil de administración web. Otorgarlo también "
                "cambiaría sus permisos de Telegram; hazlo de forma explícita antes de aprobar.",
                status_code=409,
            )
        if _vigente(await _grant(session, existing.id), now):
            raise DomainError(
                "USER_ALREADY_HAS_ACCESS",
                "Esa persona ya tiene una autorización web vigente.",
                status_code=409,
            )
        await _upsert_grant(
            session,
            user=existing,
            actor_id=actor.id,
            expires_at=None,
            request_id=request.id,
        )
        user = existing
    else:
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
        user = AppUser(
            organization_id=request.organization_id,
            display_name=request.display_name,
            roles=[Role.ADMIN.value],
            auth_subject=request.auth_subject,
            email=request.email,
            active=True,
            available=True,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()
        await _upsert_grant(
            session,
            user=user,
            actor_id=actor.id,
            expires_at=None,
            request_id=request.id,
        )

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


def _actor_in(actor: Actor, organization_id: uuid.UUID) -> uuid.UUID | None:
    """Quien actúa solo se registra si pertenece a la organización del registro.

    La superadministradora puede operar sobre otra organización, pero `audit_event`, `app_user` y
    `web_access_grant` referencian `(actor, organización)` con FK compuesta: atribuir la acción a
    una persona de otra organización no es válido. En ese caso la trazabilidad queda sin actor.
    """
    return actor.id if actor.organization_id == organization_id else None


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
    if expires_at is not None and expires_at <= now:
        raise DomainError(
            "INVALID_EXPIRY", "El vencimiento debe ser futuro; para quitar el acceso, elimínalo.",
            status_code=422,
        )  # fmt: skip
    await _upsert_grant(
        session,
        user=user,
        actor_id=_actor_in(actor, user.organization_id),
        expires_at=expires_at,
    )
    emit(
        session,
        org_id=user.organization_id,
        actor_id=_actor_in(actor, user.organization_id),
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
    """Elimina la autorización web (solo superadministración). No toca Telegram ni `active`."""
    _require_superadmin(actor)
    user = await _load_user(session, user_id)
    if user.is_superadmin:
        raise DomainError(
            "SUPERADMIN_PROTECTED",
            "La superadministradora no puede revocarse a sí misma el acceso web.",
            status_code=403,
        )
    now = _now()
    grant = await _grant(session, user.id, lock=True)
    if grant is not None:
        await session.delete(grant)
        await session.flush()
    _mirror_legacy_columns(user, expires_at=now, granted_by=None, now=now)
    emit(
        session,
        org_id=user.organization_id,
        actor_id=_actor_in(actor, user.organization_id),
        action="web_access.revoked",
        entity_type="app_user",
        entity_id=user.id,
        event_type="web_access.revoked",
        aggregate_version=None,
        payload={"user_id": str(user.id)},
    )
    return user


async def list_web_users(session: AsyncSession, actor: Actor) -> list[WebUserRow]:
    """Personas con autorización web **vigente** de la organización de quien administra.

    No incluye auxiliares de Telegram sin acceso web, personas de otras organizaciones ni
    autorizaciones vencidas. Es el listado de «Gestión de usuarios».
    """
    require(actor.roles, Permission.MANAGE_WEB_ACCESS)
    now = _now()
    rows = (
        await session.execute(
            select(AppUser, WebAccessGrant)
            .join(WebAccessGrant, WebAccessGrant.app_user_id == AppUser.id)
            .where(
                AppUser.organization_id == actor.organization_id,
                AppUser.active,
                or_(
                    WebAccessGrant.expires_at.is_(None),
                    WebAccessGrant.expires_at > now,
                ),
            )
            .order_by(func.lower(AppUser.display_name))
        )
    ).all()
    return [WebUserRow(user=user, grant=grant) for user, grant in rows]


async def remove_web_access(
    session: AsyncSession, actor: Actor, user_id: uuid.UUID
) -> RemovedAccess:
    """Elimina realmente la autorización web de una persona de la organización del `Actor`.

    Conserva la identidad (`app_user`, `auth_subject`, roles), Telegram y todas las referencias
    históricas. Reglas: nadie elimina a la superadministradora, su propio acceso ni la última
    administradora web autorizada de la organización. Es idempotente: si no hay autorización,
    informa `removed=False` sin auditar un borrado inexistente.
    """
    require(actor.roles, Permission.MANAGE_WEB_ACCESS)
    await advisory_lock(session, f"web-access:{actor.organization_id}")
    user = (
        await session.execute(
            select(AppUser)
            .where(
                AppUser.id == user_id,
                AppUser.organization_id == actor.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if user is None:
        raise not_found("La persona")
    if user.is_superadmin:
        raise DomainError(
            "SUPERADMIN_PROTECTED",
            "La superadministradora no puede ser eliminada por otra cuenta.",
            status_code=403,
        )
    if user.id == actor.id:
        raise DomainError(
            "CANNOT_REMOVE_SELF",
            "No puedes eliminar tu propio acceso web.",
            status_code=409,
        )

    grant = await _grant(session, user.id, lock=True)
    if grant is None:
        return RemovedAccess(user=user, removed=False)

    now = _now()
    if Role.ADMIN.value in (user.roles or []):
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(WebAccessGrant)
                .join(AppUser, AppUser.id == WebAccessGrant.app_user_id)
                .where(
                    WebAccessGrant.organization_id == actor.organization_id,
                    WebAccessGrant.app_user_id != user.id,
                    AppUser.active,
                    func.array_position(AppUser.roles, Role.ADMIN.value).is_not(None),
                    or_(
                        WebAccessGrant.expires_at.is_(None),
                        WebAccessGrant.expires_at > now,
                    ),
                )
            )
        ).scalar_one()
        if remaining == 0:
            raise DomainError(
                "LAST_WEB_ADMIN",
                "No puedes eliminar a la última administradora web autorizada de la farmacia.",
                status_code=409,
            )

    before = {
        "granted_at": grant.granted_at.isoformat(),
        "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
    }
    await session.delete(grant)
    await session.flush()
    # Respaldo de rollback: un vencimiento pasado deja a la persona sin acceso también con el
    # código anterior. Las columnas nunca conceden acceso en el código nuevo.
    _mirror_legacy_columns(user, expires_at=now, granted_by=None, now=now)
    emit(
        session,
        org_id=user.organization_id,
        actor_id=actor.id,
        action="web_access.removed",
        entity_type="app_user",
        entity_id=user.id,
        event_type="web_access.removed",
        aggregate_version=None,
        payload={"user_id": str(user.id)},
        before=before,
    )
    return RemovedAccess(user=user, removed=True)


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
