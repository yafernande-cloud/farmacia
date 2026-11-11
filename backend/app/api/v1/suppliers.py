"""E6 — API de proveedores. Rol `admin` + `StrongActor` (C0 §6)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.api.deps import Session, StrongActor
from app.api.headers import parse_if_match
from app.api.v1.demand import ERRORS
from app.db.session import transaction
from app.services import suppliers as svc

router = APIRouter(tags=["proveedores"])


class SupplierIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, examples=["Proveedor Ficticio Norte"])
    supplier_code: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)
    # Portal web (A3). Usuario y contraseña son opcionales y se guardan cifrados; nunca se
    # devuelven. Enviar uno sin el otro exige que el otro ya esté guardado.
    portal_url: str | None = Field(default=None, max_length=500)
    portal_username: SecretStr | None = None
    portal_password: SecretStr | None = None


class SupplierPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    supplier_code: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, pattern="^(active|inactive)$")
    portal_url: str | None = Field(default=None, max_length=500)
    # `null` en cualquiera de los dos borra la credencial completa; ver `services.suppliers`.
    portal_username: SecretStr | None = None
    portal_password: SecretStr | None = None


class SupplierOut(BaseModel):
    supplier_id: uuid.UUID
    name: str
    supplier_code: str | None
    status: str
    version: int
    sources: list[uuid.UUID]
    # Ofertas vigentes de la última lista cargada. Aditivo: no son artículos únicos.
    offer_count: int = 0
    known_conditions: list[dict]
    annotations: list[dict]
    notes: list[dict]
    internal_notes: str | None
    # Portal sin secretos: la contraseña y el usuario nunca salen de la API.
    portal_url: str | None
    has_portal_credentials: bool
    portal_status: str
    has_price_list: bool
    created_at: datetime
    updated_at: datetime


class SupplierListOut(BaseModel):
    suppliers: list[SupplierOut]


@router.get(
    "/suppliers",
    response_model=SupplierListOut,
    responses=ERRORS,
    summary="Proveedores de la organización",
)
async def list_suppliers(
    session: Session,
    actor: StrongActor,
    status: Annotated[str | None, Query(pattern="^(active|inactive)$")] = None,
):
    rows = await svc.list_suppliers(session, actor, status=status)
    return {"suppliers": [await svc.supplier_payload(session, r) for r in rows]}


@router.get(
    "/suppliers/{supplier_id}",
    response_model=SupplierOut,
    responses=ERRORS,
    summary="Detalle de un proveedor",
)
async def get_supplier(supplier_id: uuid.UUID, session: Session, actor: StrongActor):
    row = await svc.get_supplier(session, actor, supplier_id)
    return await svc.supplier_payload(session, row)


@router.post(
    "/suppliers",
    status_code=201,
    response_model=SupplierOut,
    responses=ERRORS,
    summary="Crear un proveedor",
)
async def create_supplier(
    body: SupplierIn, session: Session, actor: StrongActor, response: Response
):
    async with transaction(session):
        row = await svc.create_supplier(
            session,
            actor,
            name=body.name,
            supplier_code=body.supplier_code,
            notes=body.notes,
            portal_url=body.portal_url,
            portal_username=(
                body.portal_username.get_secret_value() if body.portal_username else None
            ),
            portal_password=(
                body.portal_password.get_secret_value() if body.portal_password else None
            ),
        )
        payload = await svc.supplier_payload(session, row)
    response.headers["ETag"] = f'"{row.version}"'
    return payload


@router.patch(
    "/suppliers/{supplier_id}",
    response_model=SupplierOut,
    responses=ERRORS,
    summary="Renombrar o activar/desactivar un proveedor (If-Match)",
)
async def update_supplier(
    supplier_id: uuid.UUID,
    body: SupplierPatchIn,
    session: Session,
    actor: StrongActor,
    response: Response,
    if_match: Annotated[
        str | None, Header(alias="If-Match", description='Versión actual, p. ej. "1"')
    ] = None,
):
    version = parse_if_match(if_match, what="el proveedor")
    changes = body.model_dump(exclude_unset=True, exclude={"portal_username", "portal_password"})
    for field_name in ("portal_username", "portal_password"):
        if field_name in body.model_fields_set:
            secret = getattr(body, field_name)
            changes[field_name] = secret.get_secret_value() if secret is not None else None
    async with transaction(session):
        row = await svc.update_supplier(
            session,
            actor,
            supplier_id,
            expected_version=version,
            changes=changes,
        )
        payload = await svc.supplier_payload(session, row)
    response.headers["ETag"] = f'"{row.version}"'
    return payload
