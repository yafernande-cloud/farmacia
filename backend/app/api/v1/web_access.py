"""Acceso web del Cotizador (G1): solicitud pública, campanita y decisiones.

Flujo (D-130, precisado por D-158):
  1. Google/Supabase identifica a la persona, pero no concede acceso. La autorización web es una
     fila de `web_access_grant`, separada de la identidad: sin fila, los endpoints protegidos
     responden 403 aunque el JWT siga vigente y la persona conserve el rol `admin`.
  2. `POST /api/v1/web-access-requests` registra una solicitud `pending` con la organización
     configurada en el servidor. La solicitud **no otorga nada**.
  3. Cualquier `admin` ve las pendientes de su organización en la campanita y aprueba o rechaza.
  4. Aprobar una identidad nueva crea la persona con `roles = ['admin']` y su autorización; aprobar
     una identidad existente solo crea o renueva la autorización, sin duplicarla ni cambiar roles.
  5. «Eliminar acceso» borra la autorización: la persona, su `auth_subject`, Telegram y el
     historial se conservan, y puede volver a solicitar acceso.

El endpoint público responde siempre `202 {"received": true}`: no revela si la cuenta ya existía ni
a qué organización pertenece. La respuesta es idempotente por `(organización, auth_subject)`.

Telegram es independiente: nada de este módulo crea cuentas, vínculos ni solicitudes del bot.
"""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import IdentityClaims, Session, StrongActor
from app.api.v1.demand import ERRORS
from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.ratelimit import SlidingWindow
from app.db.session import transaction
from app.services import web_access

router = APIRouter(tags=["acceso web"])

# 5 solicitudes por minuto por identidad y 20 por minuto por IP (D-128, conservado en D-130).
_per_subject = SlidingWindow(limit=5, window_seconds=60)
_per_ip = SlidingWindow(limit=20, window_seconds=60)


class ReceivedOut(BaseModel):
    received: Literal[True] = True


class PendingItemOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    requested_at: datetime
    organization: str | None = Field(
        default=None, description="Solo la superadministradora recibe este campo"
    )


class PendingOut(BaseModel):
    pending_count: int
    items: list[PendingItemOut]


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=500, description="Motivo opcional")


class ApprovedOut(BaseModel):
    request_id: uuid.UUID
    status: Literal["approved"] = "approved"
    user_id: uuid.UUID
    display_name: str
    roles: list[str]
    web_access_expires_at: datetime | None = None


class RejectedOut(BaseModel):
    request_id: uuid.UUID
    status: Literal["rejected"] = "rejected"


class StatusOut(BaseModel):
    status: Literal["none", "pending", "rejected", "approved", "removed", "expired"]
    requested_at: datetime | None = None
    decided_at: datetime | None = None


class UserAccessOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    roles: list[str]
    active: bool
    web_access_expires_at: datetime | None


class WebUserOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    email: str | None
    roles: list[str]
    is_superadmin: bool
    is_self: bool = Field(description="La propia cuenta: no se puede eliminar a sí misma")
    web_access_expires_at: datetime | None
    granted_at: datetime


class WebUsersOut(BaseModel):
    total: int
    items: list[WebUserOut]


class RemovedOut(BaseModel):
    user_id: uuid.UUID
    display_name: str
    removed: bool = Field(description="False si ya no tenía autorización (idempotente)")


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[str] | None = Field(
        default=None, description="Solo roles existentes; nunca agrega `superadmin`"
    )
    expires_at: datetime | None = Field(
        default=None, description="NULL = sin vencimiento automático"
    )


def _user_out(user) -> UserAccessOut:
    return UserAccessOut(
        user_id=user.id,
        display_name=user.display_name,
        roles=list(user.roles),
        active=user.active,
        web_access_expires_at=user.web_access_expires_at,
    )


@router.post(
    "/web-access-requests",
    response_model=ReceivedOut,
    status_code=202,
    responses=ERRORS,
    summary="Solicitar acceso al Cotizador (no otorga nada)",
)
async def request_access(
    claims: IdentityClaims, session: Session, request: Request
) -> ReceivedOut:
    organization_id = get_settings().web_access_organization_id
    if organization_id is None:
        raise DomainError(
            "ACCESS_NOT_CONFIGURED",
            "El acceso web no está configurado en este entorno.",
            status_code=503,
        )
    ip = request.client.host if request.client and request.client.host else "desconocida"
    if not await _per_subject.allow(f"sub:{claims.subject}") or not await _per_ip.allow(
        f"ip:{ip}"
    ):
        raise DomainError(
            "RATE_LIMITED",
            "Demasiadas solicitudes seguidas. Espera un minuto y vuelve a intentarlo.",
            status_code=429,
        )
    async with transaction(session):
        await web_access.request_access(session, claims, organization_id=organization_id)
    return ReceivedOut()


@router.get(
    "/web-access-requests/status",
    response_model=StatusOut,
    responses=ERRORS,
    summary="Estado de la propia solicitud (no otorga acceso ni crea nada)",
)
async def request_status(claims: IdentityClaims, session: Session) -> StatusOut:
    organization_id = get_settings().web_access_organization_id
    if organization_id is None:
        raise DomainError(
            "ACCESS_NOT_CONFIGURED",
            "El acceso web no está configurado en este entorno.",
            status_code=503,
        )
    result = await web_access.status_for_identity(
        session, claims, organization_id=organization_id
    )
    return StatusOut(
        status=result.status,
        requested_at=result.requested_at,
        decided_at=result.decided_at,
    )


@router.get(
    "/web-access-requests",
    response_model=PendingOut,
    responses=ERRORS,
    summary="Campanita: solicitudes pendientes",
)
async def pending_requests(session: Session, actor: StrongActor) -> PendingOut:
    result = await web_access.list_pending(session, actor)
    return PendingOut(
        pending_count=result.pending_count,
        items=[
            PendingItemOut(
                id=row.request.id,
                email=row.request.email,
                display_name=row.request.display_name,
                requested_at=row.request.requested_at,
                organization=row.organization_name,
            )
            for row in result.items
        ],
    )


@router.post(
    "/web-access-requests/{request_id}/approve",
    response_model=ApprovedOut,
    responses=ERRORS,
    summary="Aprobar: crea la persona con rol `admin`",
)
async def approve_request(
    request_id: uuid.UUID, session: Session, actor: StrongActor
) -> ApprovedOut:
    async with transaction(session):
        decision = await web_access.approve(session, actor, request_id)
    assert decision.user is not None
    return ApprovedOut(
        request_id=decision.request.id,
        user_id=decision.user.id,
        display_name=decision.user.display_name,
        roles=list(decision.user.roles),
        web_access_expires_at=decision.user.web_access_expires_at,
    )


@router.post(
    "/web-access-requests/{request_id}/reject",
    response_model=RejectedOut,
    responses=ERRORS,
    summary="Rechazar una solicitud (no crea persona)",
)
async def reject_request(
    request_id: uuid.UUID,
    session: Session,
    actor: StrongActor,
    body: RejectIn | None = None,
) -> RejectedOut:
    async with transaction(session):
        decision = await web_access.reject(
            session, actor, request_id, note=body.note if body else None
        )
    return RejectedOut(request_id=decision.request.id)


@router.post(
    "/admin/users/{user_id}/web-access",
    response_model=UserAccessOut,
    responses=ERRORS,
    summary="Otorgar o ajustar acceso web (solo superadministración técnica)",
)
async def grant_access(
    user_id: uuid.UUID, body: GrantIn, session: Session, actor: StrongActor
) -> UserAccessOut:
    async with transaction(session):
        user = await web_access.grant_web_access(
            session, actor, user_id, roles=body.roles, expires_at=body.expires_at
        )
    return _user_out(user)


@router.post(
    "/admin/users/{user_id}/web-access/revoke",
    response_model=UserAccessOut,
    responses=ERRORS,
    summary="Revocar solo el acceso web (solo superadministración técnica)",
)
async def revoke_access(
    user_id: uuid.UUID, session: Session, actor: StrongActor
) -> UserAccessOut:
    async with transaction(session):
        user = await web_access.revoke_web_access(session, actor, user_id)
    return _user_out(user)


@router.get(
    "/admin/users",
    response_model=WebUsersOut,
    responses=ERRORS,
    summary="Gestión de usuarios: autorizaciones web vigentes de la organización",
)
async def list_users(session: Session, actor: StrongActor) -> WebUsersOut:
    rows = await web_access.list_web_users(session, actor)
    items = [
        WebUserOut(
            user_id=row.user.id,
            display_name=row.user.display_name,
            email=row.user.email,
            roles=list(row.user.roles),
            is_superadmin=row.user.is_superadmin,
            is_self=row.user.id == actor.id,
            web_access_expires_at=row.grant.expires_at,
            granted_at=row.grant.granted_at,
        )
        for row in rows
    ]
    return WebUsersOut(total=len(items), items=items)


@router.delete(
    "/admin/users/{user_id}/web-access",
    response_model=RemovedOut,
    responses=ERRORS,
    summary="Eliminar la autorización web (conserva identidad, Telegram e historial)",
)
async def remove_access(
    user_id: uuid.UUID, session: Session, actor: StrongActor
) -> RemovedOut:
    async with transaction(session):
        result = await web_access.remove_web_access(session, actor, user_id)
    return RemovedOut(
        user_id=result.user.id,
        display_name=result.user.display_name,
        removed=result.removed,
    )
