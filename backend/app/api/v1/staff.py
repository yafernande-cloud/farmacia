"""API administrativa del personal (preparada para una pantalla en Lovable). Exige el rol administrador.

En organizaciones reales estas rutas exigen autenticación fuerte (`StrongActor`): la cabecera de
demostración solo se acepta en la organización de simulación. Administrar el personal es administrar
quién puede reportar y quién no, igual que administrar accesos de Telegram.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import Session, StrongActor
from app.api.v1.demand import ERRORS
from app.core.errors import DomainError
from app.core.staff import AbsenceReason
from app.db.session import transaction
from app.services import staff as svc

router = APIRouter(tags=["personal"])


class SessionState(BaseModel):
    active: bool = Field(
        description="¿Está reportando AHORA en un chat de Telegram? (deriva de la sesión)"
    )
    expires_at: datetime | None


class StaffOut(BaseModel):
    id: uuid.UUID
    display_name: str
    roles: list[str]
    member_active: bool = Field(
        description="PERTENECE a la farmacia (desactivar no elimina historial)"
    )
    available: bool = Field(
        description="DISPONIBLE para reportar (no está de vacaciones, con licencia, etc.)"
    )
    absence_reason: AbsenceReason | None
    absence_label: str | None
    absence_note: str | None
    unavailable_since: datetime | None
    can_report: bool = Field(
        description="activa Y disponible Y con rol que permite reportar: aparece en los botones"
    )
    telegram_session: SessionState
    observations_authored: int = Field(description="Con historial no se elimina nunca")
    created_at: datetime
    updated_at: datetime


class StaffCreateIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=100, examples=["María"])
    roles: list[Literal["auxiliary", "purchasing", "approver", "admin"]] = Field(
        default=["auxiliary"]
    )


class StaffRenameIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)


class AvailabilityIn(BaseModel):
    available: bool
    reason: AbsenceReason | None = Field(default=None, description="Obligatorio si available=false")
    note: str | None = Field(default=None, max_length=200)


class StaffChangeOut(BaseModel):
    staff: StaffOut
    sessions_revoked: int = Field(
        description="Sesiones de Telegram que se cerraron por este cambio"
    )


async def _one(session, actor, uid: uuid.UUID) -> StaffOut:
    for r in await svc.list_staff(session, actor):
        if r["id"] == uid:
            return StaffOut(**r)
    raise DomainError("NOT_FOUND", "La persona no existe o no es accesible.", status_code=404)


@router.get(
    "/staff",
    response_model=list[StaffOut],
    responses=ERRORS,
    summary="Listado COMPLETO del personal (incluye no disponibles y desactivadas)",
)
async def list_staff(
    session: Session, actor: StrongActor, include_inactive: Annotated[bool, Query()] = True
):
    return [
        StaffOut(**r)
        for r in await svc.list_staff(session, actor, include_inactive=include_inactive)
    ]


@router.post(
    "/staff",
    response_model=StaffOut,
    status_code=201,
    responses=ERRORS,
    summary="Crear una persona",
)
async def create_staff(body: StaffCreateIn, session: Session, actor: StrongActor):
    async with transaction(session):
        u = await svc.create_staff(
            session, actor, display_name=body.display_name, roles=list(body.roles)
        )
        return await _one(session, actor, u.id)


@router.patch(
    "/staff/{user_id}",
    response_model=StaffOut,
    responses=ERRORS,
    summary="Cambiar el nombre visible (la autoría histórica no cambia)",
)
async def rename_staff(
    user_id: uuid.UUID, body: StaffRenameIn, session: Session, actor: StrongActor
):
    async with transaction(session):
        await svc.rename_staff(session, actor, user_id, body.display_name)
        return await _one(session, actor, user_id)


@router.post(
    "/staff/{user_id}/deactivate",
    response_model=StaffChangeOut,
    responses=ERRORS,
    summary="Desactivar (no elimina; cierra sus sesiones)",
)
async def deactivate(user_id: uuid.UUID, session: Session, actor: StrongActor):
    async with transaction(session):
        _, n = await svc.set_active(session, actor, user_id, False)
        return StaffChangeOut(staff=await _one(session, actor, user_id), sessions_revoked=n)


@router.post(
    "/staff/{user_id}/activate",
    response_model=StaffChangeOut,
    responses=ERRORS,
    summary="Reactivar",
)
async def activate(user_id: uuid.UUID, session: Session, actor: StrongActor):
    async with transaction(session):
        _, n = await svc.set_active(session, actor, user_id, True)
        return StaffChangeOut(staff=await _one(session, actor, user_id), sessions_revoked=n)


@router.put("/staff/{user_id}/availability", response_model=StaffChangeOut, responses=ERRORS,
            summary="Marcar disponibilidad para reportar (vacaciones, licencia, día libre u otro)")  # fmt: skip
async def availability(
    user_id: uuid.UUID, body: AvailabilityIn, session: Session, actor: StrongActor
):
    async with transaction(session):
        _, n = await svc.set_availability(session, actor, user_id, available=body.available,
                                          reason=body.reason.value if body.reason else None, note=body.note)  # fmt: skip
        return StaffChangeOut(staff=await _one(session, actor, user_id), sessions_revoked=n)
