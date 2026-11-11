"""E7 — administración de fuentes y E4 — estado. Rol `admin` + `StrongActor` (C0 §6)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Session, StrongActor
from app.api.headers import parse_if_match
from app.api.v1.demand import ERRORS
from app.db.session import transaction
from app.services import sources as svc

router = APIRouter(tags=["fuentes"])


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200, examples=["Lista Ficticia Norte"])
    kind: str = Field(pattern="^(supplier|chain_reference)$")
    supplier_id: uuid.UUID | None = None
    mode: str = Field(default="none", pattern="^(api|manual_import|fixture|none)$")
    capabilities: list[str] = Field(default_factory=list)
    enabled: bool = True


class SourcePatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    mode: str | None = Field(default=None, pattern="^(api|manual_import|fixture|none)$")
    capabilities: list[str] | None = None
    enabled: bool | None = None


class SourceOut(BaseModel):
    source_id: uuid.UUID
    name: str
    kind: str
    supplier_id: uuid.UUID | None
    mode: str
    capabilities: list[str]
    enabled: bool
    status: str
    has_secret: bool
    last_success_at: datetime | None
    last_error: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class SourceListOut(BaseModel):
    sources: list[SourceOut]


@router.get(
    "/sources",
    response_model=SourceListOut,
    responses=ERRORS,
    summary="Fuentes de la organización y su estado derivado",
)
async def list_sources(
    session: Session,
    actor: StrongActor,
    kind: Annotated[str | None, Query(pattern="^(supplier|chain_reference)$")] = None,
    enabled: bool | None = None,
):
    rows = await svc.list_sources(session, actor, kind=kind, enabled=enabled)
    importadas = await svc.import_success_by_source(session, actor, [r.id for r in rows])
    return {
        "sources": [
            svc.source_payload(r, imported_success_at=importadas.get(r.id)) for r in rows
        ]
    }


@router.get(
    "/sources/{source_id}",
    response_model=SourceOut,
    responses=ERRORS,
    summary="Detalle de una fuente",
)
async def get_source(source_id: uuid.UUID, session: Session, actor: StrongActor):
    row = await svc.get_source(session, actor, source_id)
    importadas = await svc.import_success_by_source(session, actor, [row.id])
    return svc.source_payload(row, imported_success_at=importadas.get(row.id))


@router.post(
    "/sources",
    status_code=201,
    response_model=SourceOut,
    responses=ERRORS,
    summary="Crear una fuente",
)
async def create_source(
    body: SourceIn, session: Session, actor: StrongActor, response: Response
):
    async with transaction(session):
        row = await svc.create_source(
            session,
            actor,
            name=body.name,
            kind=body.kind,
            supplier_id=body.supplier_id,
            mode=body.mode,
            capabilities=body.capabilities,
            enabled=body.enabled,
        )
        payload = svc.source_payload(row)
    response.headers["ETag"] = f'"{row.version}"'
    return payload


@router.patch(
    "/sources/{source_id}",
    response_model=SourceOut,
    responses=ERRORS,
    summary="Actualizar una fuente (If-Match)",
)
async def update_source(
    source_id: uuid.UUID,
    body: SourcePatchIn,
    session: Session,
    actor: StrongActor,
    response: Response,
    if_match: Annotated[
        str | None, Header(alias="If-Match", description='Versión actual, p. ej. "1"')
    ] = None,
):
    version = parse_if_match(if_match, what="la fuente")
    async with transaction(session):
        row = await svc.update_source(
            session,
            actor,
            source_id,
            expected_version=version,
            changes=body.model_dump(exclude_unset=True),
        )
        payload = svc.source_payload(row)
    response.headers["ETag"] = f'"{row.version}"'
    return payload
