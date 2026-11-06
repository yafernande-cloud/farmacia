"""API administrativa de accesos de Telegram (futuro frontend de Pao).

Flujo: el bot registra una SOLICITUD y entrega un código temporal por Telegram; la persona se lo
comparte a la administradora; ella busca el código, revisa la cuenta y la aprueba aquí. Conocer el
código no autoriza nada: todas estas rutas exigen sesión fuerte (JWT de Supabase en organizaciones
reales) y el permiso `manage_accounts` resuelto en PostgreSQL.

Los códigos y sus hashes NUNCA aparecen en respuestas, URLs ni auditoría. La entrega del código
ocurre solo por Telegram.
"""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import Session, StrongActor
from app.api.v1.demand import ERRORS
from app.db.session import transaction
from app.services import access

router = APIRouter(tags=["accesos de Telegram"])


class BindingOut(BaseModel):
    id: uuid.UUID
    external_chat_id: str
    mode: Literal["personal", "shared_device"]
    status: Literal["active", "inactive"]
    created_at: datetime


class AccountOut(BaseModel):
    id: uuid.UUID
    external_user_id: str = Field(description="ID de usuario de Telegram (no un teléfono)")
    display_label: str
    branch_label: str | None = Field(
        default=None, description="Sucursal de esta cuenta; se copia a cada aviso como histórico."
    )
    mode: Literal["personal", "shared_device"]
    access_role: Literal["admin", "operator"] = Field(
        description="Tipo de cuenta para administración; NO otorga permisos de negocio"
    )
    status: Literal["active", "blocked"]
    origin: Literal["backfill", "access_request", "manual"]
    created_from_request_id: uuid.UUID | None
    app_user_id: uuid.UUID | None
    app_user_name: str | None
    person_active: bool | None
    person_available: bool | None
    bindings: list[BindingOut]
    created_at: datetime
    created_by_name: str | None
    blocked_at: datetime | None
    blocked_by_name: str | None
    block_reason: str | None
    reactivated_at: datetime | None
    reactivated_by_name: str | None


class AccessRequestOut(BaseModel):
    id: uuid.UUID
    external_user_id: str
    external_chat_id: str
    status: Literal["pending", "approved", "rejected", "expired"]
    created_at: datetime
    expires_at: datetime
    approved_at: datetime | None
    approved_account_id: uuid.UUID | None
    approved_by_name: str | None
    rejected_at: datetime | None
    reject_reason: str | None
    rejected_by_name: str | None


class ExistingAccountOut(BaseModel):
    """Cuenta ya existente (normalmente bloqueada) que se reutilizaría al aprobar."""

    id: uuid.UUID
    display_label: str
    branch_label: str | None = Field(
        default=None, description="Sucursal de esta cuenta; se copia a cada aviso como histórico."
    )
    mode: Literal["personal", "shared_device"]
    access_role: Literal["admin", "operator"]
    status: Literal["active", "blocked"]
    app_user_id: uuid.UUID | None


class CodeLookupRequest(BaseModel):
    code: str = Field(min_length=1, max_length=100, description="Código que compartió la persona")


class CodeLookupResponse(BaseModel):
    request: AccessRequestOut
    existing_account: ExistingAccountOut | None = Field(
        default=None,
        description="Si la cuenta ya existía (p. ej. bloqueada), la aprobación reutiliza su historial.",
    )
    can_approve: bool = Field(description="La solicitud está pendiente y vigente.")


class ApproveIn(BaseModel):
    display_label: str = Field(min_length=1, max_length=100)
    branch_label: str | None = Field(default=None, max_length=120)
    mode: Literal["personal", "shared_device"]
    app_user_id: uuid.UUID | None = Field(
        default=None, description="Obligatorio en modo personal; prohibido en compartido"
    )
    access_role: Literal["admin", "operator"] = Field(
        default="operator",
        description="Tipo de cuenta; no otorga permisos de negocio (salen de app_user.roles)",
    )


class UpdateAccountIn(BaseModel):
    display_label: str | None = Field(default=None, min_length=1, max_length=100)
    branch_label: str | None = Field(default=None, max_length=120)


class AccountAuxiliaryOut(BaseModel):
    app_user_id: uuid.UUID
    display_name: str
    roles: list[str]
    person_active: bool
    person_available: bool
    can_report: bool
    active: bool
    created_at: datetime
    deactivated_at: datetime | None
    deactivation_reason: str | None


class AccountAuxiliariesOut(BaseModel):
    account_id: uuid.UUID
    mode: Literal["personal", "shared_device"]
    branch_label: str | None
    app_user_id: uuid.UUID | None = Field(
        description="Persona fija en una cuenta personal; null en un teléfono compartido."
    )
    auxiliaries: list[AccountAuxiliaryOut]


class AddAuxiliaryIn(BaseModel):
    app_user_id: uuid.UUID


class AuxiliaryChangeOut(BaseModel):
    account_id: uuid.UUID
    app_user_id: uuid.UUID
    active: bool
    sessions_revoked: int
    history_preserved: bool = True
    reactivated: bool | None = None


class DeactivateAuxiliaryIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class RejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class BlockIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class ReactivateIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class BlockedOut(BaseModel):
    account_id: uuid.UUID
    status: Literal["blocked"]
    bindings_deactivated: int
    sessions_revoked: int
    history_preserved: bool = True


class ReactivatedOut(BaseModel):
    account_id: uuid.UUID
    status: Literal["active"]
    bindings_reactivated: int
    history_preserved: bool = True


class HistoryEntryOut(BaseModel):
    action: str
    actor_name: str | None
    created_at: datetime
    before: dict | None
    after: dict | None


async def _account_out(session, actor, account_id: uuid.UUID) -> AccountOut:
    rows = await access.list_accounts(session, actor)
    row = next(r for r in rows if r["id"] == account_id)
    return AccountOut(**row)


@router.get(
    "/telegram-accounts",
    response_model=list[AccountOut],
    responses=ERRORS,
    summary="Cuentas de Telegram autorizadas y bloqueadas (historial conservado)",
)
async def list_accounts(session: Session, actor: StrongActor):
    return [AccountOut(**r) for r in await access.list_accounts(session, actor)]


@router.get(
    "/telegram-access-requests",
    response_model=list[AccessRequestOut],
    responses=ERRORS,
    summary="Solicitudes de acceso (pendientes, aprobadas, rechazadas o vencidas)",
)
async def list_requests(
    session: Session,
    actor: StrongActor,
    status: Literal["pending", "approved", "rejected", "expired"] | None = Query(default=None),
):
    return [
        AccessRequestOut(**r)
        for r in await access.list_requests(session, actor, status=status)
    ]


@router.post(
    "/telegram-access-requests/lookup",
    response_model=CodeLookupResponse,
    responses=ERRORS,
    summary="Buscar la solicitud pendiente por el código que compartió la persona",
)
async def lookup_request(body: CodeLookupRequest, session: Session, actor: StrongActor):
    async with transaction(session):
        result = await access.lookup_request(session, actor, code=body.code)
    if result.error is not None:
        raise result.error
    assert result.request is not None
    request = await access.get_request(session, actor, result.request.id)
    existing = result.existing_account
    return CodeLookupResponse(
        request=AccessRequestOut(**request),
        existing_account=(
            ExistingAccountOut(
                id=existing.id, display_label=existing.display_label, mode=existing.mode,
                access_role=existing.access_role, status=existing.status,
                app_user_id=existing.app_user_id,
            )
            if existing is not None
            else None
        ),
        can_approve=request["status"] == "pending",
    )


@router.get(
    "/telegram-access-requests/{request_id}",
    response_model=AccessRequestOut,
    responses=ERRORS,
    summary="Detalle de una solicitud",
)
async def get_request(request_id: uuid.UUID, session: Session, actor: StrongActor):
    return AccessRequestOut(**await access.get_request(session, actor, request_id))


@router.post(
    "/telegram-access-requests/{request_id}/approve",
    response_model=AccountOut,
    responses=ERRORS,
    summary="Aprobar: vincula y habilita la cuenta; notifica por Telegram (sin código)",
)
async def approve_request(
    request_id: uuid.UUID, body: ApproveIn, session: Session, actor: StrongActor
):
    async with transaction(session):
        approval = await access.approve_request(
            session,
            actor,
            request_id,
            display_label=body.display_label,
            mode=body.mode,
            app_user_id=body.app_user_id,
            access_role=body.access_role,
            branch_label=body.branch_label,
        )
        return await _account_out(session, actor, approval.account.id)


@router.post(
    "/telegram-access-requests/{request_id}/reject",
    response_model=AccessRequestOut,
    responses=ERRORS,
    summary="Rechazar una solicitud pendiente (conserva la solicitud)",
)
async def reject_request(
    request_id: uuid.UUID, body: RejectIn, session: Session, actor: StrongActor
):
    async with transaction(session):
        await access.reject_request(session, actor, request_id, reason=body.reason)
        return AccessRequestOut(
            **await access.get_request(session, actor, request_id)
        )


@router.post(
    "/telegram-accounts/{account_id}/block",
    response_model=BlockedOut,
    responses=ERRORS,
    summary="Bloquear: desactiva chats, cierra sesiones y conserva todo el historial",
)
async def block_account(
    account_id: uuid.UUID, body: BlockIn, session: Session, actor: StrongActor
):
    async with transaction(session):
        result = await access.block_account(session, actor, account_id, reason=body.reason)
    return BlockedOut(
        account_id=result.account.id,
        status="blocked",
        bindings_deactivated=result.bindings_deactivated,
        sessions_revoked=result.sessions_revoked,
    )


@router.post(
    "/telegram-accounts/{account_id}/reactivate",
    response_model=AccountOut,
    responses=ERRORS,
    summary="Rehabilitar una cuenta bloqueada (recrea sus chats privados)",
)
async def reactivate_account(
    account_id: uuid.UUID, body: ReactivateIn, session: Session, actor: StrongActor
):
    async with transaction(session):
        await access.reactivate_account(session, actor, account_id, reason=body.reason)
        return await _account_out(session, actor, account_id)


@router.get(
    "/telegram-accounts/{account_id}/history",
    response_model=list[HistoryEntryOut],
    responses=ERRORS,
    summary="Historial de la cuenta y de la solicitud que la aprobó (sin códigos)",
)
async def account_history(account_id: uuid.UUID, session: Session, actor: StrongActor):
    rows = await access.account_history(session, actor, account_id)
    return [HistoryEntryOut(**r) for r in rows]


@router.patch(
    "/telegram-accounts/{account_id}",
    response_model=AccountOut,
    responses=ERRORS,
    summary="Editar nombre o sucursal de una cuenta (no cambia modo ni auxiliares)",
)
async def update_account(
    account_id: uuid.UUID, body: UpdateAccountIn, session: Session, actor: StrongActor
):
    async with transaction(session):
        await access.update_account(
            session,
            actor,
            account_id,
            display_label=body.display_label,
            branch_label=body.branch_label,
            update_display="display_label" in body.model_fields_set,
            update_branch="branch_label" in body.model_fields_set,
        )
        return await _account_out(session, actor, account_id)


@router.get(
    "/telegram-accounts/{account_id}/auxiliaries",
    response_model=AccountAuxiliariesOut,
    responses=ERRORS,
    summary="Auxiliares habilitados en una cuenta compartida, incluidos los dados de baja",
)
async def list_auxiliaries(account_id: uuid.UUID, session: Session, actor: StrongActor):
    return AccountAuxiliariesOut(
        **await access.list_account_auxiliaries(session, actor, account_id)
    )


@router.post(
    "/telegram-accounts/{account_id}/auxiliaries",
    response_model=AuxiliaryChangeOut,
    responses=ERRORS,
    summary="Habilitar (o reactivar) una persona en esta cuenta; no otorga roles ni permisos",
)
async def add_auxiliary(
    account_id: uuid.UUID, body: AddAuxiliaryIn, session: Session, actor: StrongActor
):
    async with transaction(session):
        result = await access.add_account_auxiliary(
            session, actor, account_id, body.app_user_id
        )
    return AuxiliaryChangeOut(**result)


@router.post(
    "/telegram-accounts/{account_id}/auxiliaries/{app_user_id}/deactivate",
    response_model=AuxiliaryChangeOut,
    responses=ERRORS,
    summary="Dar de baja a una persona SOLO en esta cuenta y revocar sus sesiones de esta cuenta",
)
async def deactivate_auxiliary(
    account_id: uuid.UUID,
    app_user_id: uuid.UUID,
    body: DeactivateAuxiliaryIn,
    session: Session,
    actor: StrongActor,
):
    async with transaction(session):
        result = await access.deactivate_account_auxiliary(
            session, actor, account_id, app_user_id, reason=body.reason
        )
    return AuxiliaryChangeOut(**result)
