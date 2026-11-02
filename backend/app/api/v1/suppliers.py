"""E6 — API de proveedores. Rol `admin` + `StrongActor` (C0 §6)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

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


class SupplierPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    supplier_code: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(default=None, pattern="^(active|inactive)$")


class SupplierOut(BaseModel):
    supplier_id: uuid.UUID
    name: str
    supplier_code: str | None
    status: str
    version: int
    sources: list[uuid.UUID]
    known_conditions: list[dict]
    annotations: list[dict]
    notes: list[dict]
    internal_notes: str | None
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
    async with transaction(session):
        row = await svc.update_supplier(
            session,
            actor,
            supplier_id,
            expected_version=version,
            changes=body.model_dump(exclude_unset=True),
        )
        payload = await svc.supplier_payload(session, row)
    response.headers["ETag"] = f'"{row.version}"'
    return payload
