"""Perfil de importación, vista previa de la planilla y ofertas importadas (A3).

Tres piezas que la pantalla de Proveedores necesita y que el sobre E8 no expone:

* `GET/PUT /sources/{id}/import-profile` — mapeo columna→papel por fuente, con versión.
* `POST /sources/{id}/import-preview` — lectura de un archivo real, sin escribir nada.
* `GET /suppliers/{id}/offers` — última observación de cada SKU, con precio y fecha.

Todo exige `admin` + `StrongActor`. Los archivos se leen con el mismo motor de E8, de modo que lo
que muestra la vista previa es lo que la importación verá después.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Header, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Session, StrongActor
from app.api.headers import parse_if_match
from app.api.v1.demand import ERRORS
from app.core.errors import DomainError
from app.db.session import transaction
from app.services import import_profiles as svc
from app.services.imports import MAX_BYTES

router = APIRouter(tags=["listas de precios de proveedores"])


class ImportProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_format: Literal["xlsx", "xls", "csv"]
    sheet: str | None = Field(default=None, max_length=120)
    header_row: int = Field(default=1, ge=1, le=1000)
    columns: dict[str, Any]
    units: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class ImportProfileOut(BaseModel):
    source_id: uuid.UUID
    file_format: str
    sheet: str | None
    header_row: int
    columns: dict[str, Any]
    units: dict[str, Any]
    options: dict[str, Any]
    status: str
    version: int
    created_at: datetime
    updated_at: datetime


class ImportProfileResponse(BaseModel):
    """`profile` es `null` cuando la fuente todavía no tiene perfil; `version` 0 es el punto de
    partida para el primer `If-Match`."""

    profile: ImportProfileOut | None
    version: int


class PreviewRow(BaseModel):
    row: int
    values: list[Any]


class PreviewOut(BaseModel):
    file_format: str
    sheets: list[str]
    sheet: str | None
    header_row: int
    headers: list[str | None]
    sample_rows: list[PreviewRow]
    row_count: int
    suggested_columns: dict[str, dict[str, Any]]
    suggested_options: dict[str, Any]


class OfferOut(BaseModel):
    offer_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str
    external_id: str
    external_name: str | None
    trade_item_id: uuid.UUID
    trade_item_name: str
    price_amount: str
    currency: str
    price_basis: str
    sale_unit: str
    sale_unit_text: str | None
    pack_units_per_commercial_unit: str | None
    pack_base_unit: str | None
    stock_status: str
    stock_quantity: str | None
    review_status: str
    observed_at: datetime
    imported_at: datetime


class OfferListOut(BaseModel):
    offers: list[OfferOut]


async def _read_limited(upload: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_BYTES:
            raise DomainError(
                "PAYLOAD_TOO_LARGE", "El archivo supera los 10 MB permitidos.", status_code=413
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.get(
    "/sources/{source_id}/import-profile",
    response_model=ImportProfileResponse,
    responses=ERRORS,
    summary="Perfil de importación de una fuente (null si todavía no existe)",
)
async def get_import_profile(source_id: uuid.UUID, session: Session, actor: StrongActor):
    profile = await svc.get_profile(session, actor, source_id)
    return {
        "profile": svc.profile_payload(profile),
        "version": profile.version if profile is not None else 0,
    }


@router.put(
    "/sources/{source_id}/import-profile",
    response_model=ImportProfileOut,
    responses=ERRORS,
    summary="Crear o actualizar el perfil de importación (If-Match; 0 si no existe)",
)
async def put_import_profile(
    source_id: uuid.UUID,
    body: ImportProfileIn,
    session: Session,
    actor: StrongActor,
    response: Response,
    if_match: Annotated[
        str | None,
        Header(alias="If-Match", description='Versión actual; "0" si el perfil no existe'),
    ] = None,
):
    expected = parse_if_match(if_match, what="el perfil de importación")
    async with transaction(session):
        profile = await svc.save_profile(
            session,
            actor,
            source_id,
            file_format=body.file_format,
            sheet=body.sheet,
            header_row=body.header_row,
            columns=body.columns,
            units=body.units,
            options=body.options,
            expected_version=expected,
        )
        payload = svc.profile_payload(profile)
    response.headers["ETag"] = f'"{profile.version}"'
    return payload


@router.post(
    "/sources/{source_id}/import-preview",
    response_model=PreviewOut,
    responses={**ERRORS, 413: {"description": "Archivo demasiado grande"}},
    summary="Leer una planilla y proponer columnas sin guardar nada",
)
async def preview_import(
    source_id: uuid.UUID,
    session: Session,
    actor: StrongActor,
    file: Annotated[UploadFile, File(description="Archivo .xlsx, .xls o .csv del proveedor")],
    sheet: Annotated[
        str | None, Form(description="Hoja a leer; por defecto la primera")
    ] = None,
    header_row: Annotated[
        int | None, Form(ge=1, le=1000, description="Fila de encabezado; por defecto se detecta")
    ] = None,
):
    content = await _read_limited(file)
    return await svc.preview_import(
        session,
        actor,
        source_id,
        file_name=file.filename,
        content=content,
        sheet=sheet,
        header_row=header_row,
    )


@router.get(
    "/suppliers/{supplier_id}/offers",
    response_model=OfferListOut,
    responses=ERRORS,
    summary="Últimos precios importados por SKU de cada fuente del proveedor",
)
async def list_supplier_offers(
    supplier_id: uuid.UUID,
    session: Session,
    actor: StrongActor,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    return {"offers": await svc.latest_offers(session, actor, supplier_id, limit=limit)}
