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


class PriceFormatExampleOut(BaseModel):
    """Un precio cuya lectura cambia según el formato, con las lecturas posibles."""

    row: int
    raw: str
    amounts: list[str]


class PriceFormatAmountOut(BaseModel):
    """Lectura concreta de un ejemplo dentro de una interpretación."""

    row: int
    raw: str
    amount: str


class PriceFormatInterpretationOut(BaseModel):
    """Una lectura posible de la columna de precios: decimal o con separador de miles."""

    key: str
    decimal_separator: str
    thousands_separator: str | None
    label: str
    description: str
    examples: list[PriceFormatAmountOut]


class PriceFormatOut(BaseModel):
    """Diagnóstico de separadores. Si es ambiguo, la importación real exige confirmación."""

    status: Literal["ok", "ambiguous", "none"]
    requires_confirmation: bool
    confirmed: bool
    declared_valid: bool
    decimal_separator: str
    thousands_separator: str | None
    values_checked: int
    unrecognized_values: int
    examples: list[PriceFormatExampleOut]
    interpretations: list[PriceFormatInterpretationOut]
    message: str | None


class PreviewOut(BaseModel):
    file_format: str
    sheets: list[str]
    sheet: str | None
    # Hoja propuesta cuando no se pidió una; `sheet` es la que realmente se leyó.
    suggested_sheet: str | None = None
    header_row: int
    headers: list[str | None]
    sample_rows: list[PreviewRow]
    row_count: int
    suggested_columns: dict[str, dict[str, Any]]
    suggested_options: dict[str, Any]
    price_format: PriceFormatOut | None = None


class ImportCheckProblemOut(BaseModel):
    """Un motivo de rechazo agrupado: cuántas filas y algunas de ellas como ejemplo."""

    code: str
    message: str
    fields: list[str]
    count: int
    sample_rows: list[int]


class ImportCheckUnitOut(BaseModel):
    """Texto de unidad que el mapeo todavía no traduce."""

    text: str
    count: int
    sample_rows: list[int]


class ImportCheckBasisOut(BaseModel):
    """Texto de base de precio que el mapeo todavía no confirma para este proveedor."""

    text: str
    count: int
    sample_rows: list[int]


class ImportIgnoredColumnOut(BaseModel):
    """Columna reconocida que se omitió a propósito, con el motivo en lenguaje claro."""

    header: str | None
    role: str
    reason: str


class ImportCheckRow(BaseModel):
    """Fila interpretada por el motor. `problem` solo existe cuando la fila se rechaza."""

    row: int
    status: Literal["ready", "review", "rejected"]
    sku: str | None
    name: str | None
    # Laboratorio y marca: columna publicada o reconocidos en la descripción (D-150); None = no
    # informado ni reconocible.
    laboratory: str | None = None
    brand: str | None = None
    # `published` (columna), `extracted` (descripción) o `unconfirmed` (sin identidad).
    identity_status: str | None = None
    laboratory_source: str | None = None
    brand_source: str | None = None
    price: str | None
    currency: str | None
    price_basis: str | None
    sale_unit: str | None
    sale_unit_text: str | None
    pack_units: str | None
    stock: str | None
    observed_at: str | None
    warnings: list[str]
    problem: dict[str, Any] | None


class ImportCheckOut(BaseModel):
    """Revisión previa: lo que la importación haría, sin escribir nada.

    `rows` trae las primeras `_CHECK_ROWS` filas; `problems` y `unrecognised_units` agregan
    **todas** las filas, para que una sola corrección cubra muchas filas.
    """

    file_format: str
    sheets: list[str]
    sheet: str | None
    header_row: int
    headers: list[str | None]
    rows_total: int
    rows_accepted: int
    rows_rejected: int
    rows_needs_review: int
    rows: list[ImportCheckRow]
    rows_truncated: bool
    problems: list[ImportCheckProblemOut]
    unrecognised_units: list[ImportCheckUnitOut]
    # Bases de precio ambiguas («unit»): se confirman por proveedor, nunca en general.
    unrecognised_bases: list[ImportCheckBasisOut] = Field(default_factory=list)
    profile_source: Literal["saved", "request", "detected"]
    profile_version: int
    # Mapeo efectivo: es el que hay que guardar antes de importar para leer las mismas celdas.
    profile: dict[str, Any] | None = None
    # Roles obligatorios que el mapeo no cubre (solo en modo automático). Nunca se inventan.
    missing_columns: list[str] = Field(default_factory=list)
    # Columnas opcionales omitidas a propósito (por ejemplo `pack` sin unidad base).
    ignored_columns: list[ImportIgnoredColumnOut] = Field(default_factory=list)
    # Separadores de precio: si son ambiguos, hay que confirmar antes de importar.
    price_format: PriceFormatOut | None = None
    file_hash: str


class OfferOut(BaseModel):
    offer_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str
    # `api`/`fixture`/`manual_import`: la pantalla separa los precios de archivos de los en vivo.
    source_mode: str
    external_id: str
    external_name: str | None
    # Columna publicada o reconocidos en la descripción; NULL = no informado. La procedencia y el
    # estado viajan aparte para no presentar como completa una oferta sin identidad (D-150).
    laboratory_as_published: str | None
    brand_as_published: str | None
    # `published` (columna), `extracted` (descripción, pendiente de confirmación) o `unconfirmed`.
    identity_status: str
    laboratory_source: str | None
    brand_source: str | None
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
    stock_unit: str | None
    review_status: str
    # Condiciones comerciales tal como las escribió el proveedor; explican una revisión pendiente.
    notes: str | None
    valid_from: datetime | None
    valid_to: datetime | None
    is_current: bool
    fresh_until: datetime
    is_stale: bool
    observed_at: datetime
    imported_at: datetime
    # Clave opaca de orden comercial (preferencias). La pantalla ordena de forma estable por esta
    # clave y nunca por nombre ni fuente; sin preferencias todas las ofertas valen lo mismo.
    order_key: list[int] = Field(default_factory=list)


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
    sheet: Annotated[str | None, Form(description="Hoja a leer; por defecto la primera")] = None,
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


@router.post(
    "/sources/{source_id}/import-check",
    response_model=ImportCheckOut,
    responses={**ERRORS, 413: {"description": "Archivo demasiado grande"}},
    summary="Revisar la planilla con un mapeo sin guardar ni importar nada",
)
async def check_import(
    source_id: uuid.UUID,
    session: Session,
    actor: StrongActor,
    file: Annotated[UploadFile, File(description="Archivo .xlsx, .xls o .csv del proveedor")],
    sheet: Annotated[
        str | None, Form(description="Hoja a revisar; por defecto se detecta la de productos")
    ] = None,
    header_row: Annotated[
        int | None, Form(ge=1, le=1000, description="Fila de encabezado; por defecto se detecta")
    ] = None,
    profile: Annotated[
        str | None,
        Form(
            description="Mapeo a revisar como JSON (file_format, columns, units, options). "
            "Si falta, se usa el perfil guardado de la fuente."
        ),
    ] = None,
    auto: Annotated[
        bool,
        Form(
            description="Flujo de un botón: reutiliza el perfil guardado si calza por nombre o "
            "propone un mapeo desde los encabezados, sin inventar moneda, base ni unidad."
        ),
    ] = False,
):
    body = None
    if profile is not None:
        try:
            body = ImportProfileIn.model_validate_json(profile).model_dump()
        except ValueError as exc:
            raise DomainError(
                "IMPORT_FORMAT_INVALID",
                "El mapeo enviado para revisar no tiene el formato esperado.",
                status_code=422,
            ) from exc
    content = await _read_limited(file)
    async with transaction(session):
        return await svc.check_import(
            session,
            actor,
            source_id,
            file_name=file.filename,
            content=content,
            sheet=sheet,
            header_row=header_row,
            payload=body,
            auto=auto,
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
    # Repetible: `?trade_item_ids=<uuid>&trade_item_ids=<uuid>`. Acota la lectura a los artículos
    # que la búsqueda ya mostró, sin cambiar cuál es la última observación de cada SKU.
    trade_item_ids: Annotated[list[uuid.UUID] | None, Query()] = None,
):
    return {
        "offers": await svc.latest_offers(
            session, actor, supplier_id, limit=limit, trade_item_ids=trade_item_ids
        )
    }
