"""Motor declarativo de proveedores: perfil por fuente y búsqueda en vivo.

La configuración y la búsqueda son de `admin` + `StrongActor` (D-130). No se persisten ofertas: la
búsqueda vive en memoria con TTL corto y el perfil conserva solo configuración y estado operativo.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Session, StrongActor
from app.api.headers import parse_if_match
from app.api.v1.demand import ERRORS
from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.ratelimit import SlidingWindow
from app.db.session import transaction
from app.services.connectors import service as svc

router = APIRouter(tags=["motor de proveedores"])

_live_limits: SlidingWindow | None = None


def _limiter() -> SlidingWindow:
    """Limitador por proceso, con el valor vigente de la configuración."""
    global _live_limits
    limit = get_settings().supplier_connector_rate_limit_per_minute
    if _live_limits is None or _live_limits.limit != limit:
        _live_limits = SlidingWindow(limit=limit, window_seconds=60)
    return _live_limits


class ConnectorProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: str | None = Field(default=None, max_length=60, examples=["farmaloop"])
    mechanism: str | None = Field(
        default=None,
        pattern="^(algolia|rest_json|html_form|html_embedded_json|manual_capture|browser_required|unsupported)$",
    )
    config: dict[str, Any] | None = None
    field_map: dict[str, Any] | None = None
    enabled: bool = True


class ConnectorProfileOut(BaseModel):
    source_id: uuid.UUID
    supplier_id: uuid.UUID
    source_name: str
    supplier_name: str
    configured: bool
    state: Literal["not_configured", "connected", "error", "requires_intervention"]
    reason: str | None
    version: int
    mechanism: str | None
    enabled: bool
    config: dict[str, Any] | None
    field_map: dict[str, Any] | None
    schema_fingerprint: str | None
    last_success_at: datetime | None
    last_checked_at: datetime | None
    last_error: str | None
    last_error_kind: str | None
    created_at: datetime | None
    updated_at: datetime | None


class ConnectorProfileResponse(BaseModel):
    profile: ConnectorProfileOut
    version: int


class SupplierSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: uuid.UUID
    query: str = Field(min_length=2, max_length=120, examples=["predual 30 mg"])
    limit: int = Field(default=10, ge=1, le=50)


class MoneyOut(BaseModel):
    amount: str
    currency: str


class SupplierOfferOut(BaseModel):
    external_id: str
    name: str
    laboratory: str | None
    presentation: str | None
    price: MoneyOut
    price_basis: str | None
    sale_unit: str | None
    stock_status: Literal["unknown", "in_stock", "out_of_stock"]
    stock_quantity: str | None
    stock_unit: str | None
    image_url: str | None
    product_url: str | None
    ean: str | None
    # Clasificación textual de la consulta frente al nombre publicado; no es equivalencia.
    match_relation: Literal["direct", "related", "unverified"]
    match_reasons: list[str]
    # El carrito no se conecta en esta etapa: solo se informa si faltan datos comerciales.
    cart_eligible: bool
    cart_blockers: list[str]


class SupplierSearchOut(BaseModel):
    search_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str
    query: str
    state: Literal["not_configured", "connected", "error", "requires_intervention"]
    reason: str | None
    outcome: Literal["matches", "no_matches"] | None
    observed_at: datetime | None
    created_at: datetime
    expires_at: datetime
    cached: bool
    profile_version: int | None
    discarded: int
    warnings: list[str]
    offers: list[SupplierOfferOut]


@router.get(
    "/sources/{source_id}/connector-profile",
    response_model=ConnectorProfileResponse,
    responses=ERRORS,
    summary="Perfil de conexión de una fuente y su estado derivado",
)
async def get_connector_profile(
    source_id: uuid.UUID, session: Session, actor: StrongActor
):
    payload = await svc.get_profile(session, actor, source_id)
    return {"profile": payload, "version": payload["version"]}


@router.put(
    "/sources/{source_id}/connector-profile",
    response_model=ConnectorProfileOut,
    responses={**ERRORS, 428: {"description": "Falta If-Match al actualizar"}},
    summary="Crear o actualizar el perfil de conexión (If-Match; 0 si no existe)",
)
async def put_connector_profile(
    source_id: uuid.UUID,
    body: ConnectorProfileIn,
    session: Session,
    actor: StrongActor,
    response: Response,
    if_match: Annotated[
        str | None,
        Header(alias="If-Match", description='Versión actual; "0" si el perfil no existe'),
    ] = None,
):
    expected = parse_if_match(if_match) if if_match else None
    async with transaction(session):
        await svc.upsert_profile(
            session,
            actor,
            source_id,
            preset=body.preset,
            mechanism=body.mechanism,
            config=body.config,
            field_map=body.field_map,
            enabled=body.enabled,
            expected_version=expected,
        )
        payload = await svc.get_profile(session, actor, source_id)
    response.headers["ETag"] = f'"{payload["version"]}"'
    return payload


@router.post(
    "/supplier-searches",
    response_model=SupplierSearchOut,
    responses={**ERRORS, 429: {"description": "Demasiadas búsquedas"}, 503: {"description": "Motor deshabilitado"}},
    summary="Buscar en vivo en una fuente de proveedor configurada",
)
async def create_supplier_search(
    body: SupplierSearchIn, session: Session, actor: StrongActor
):
    limiter = _limiter()
    if not await limiter.allow(str(actor.id)):
        raise DomainError(
            "RATE_LIMITED",
            "Demasiadas búsquedas en vivo. Espera un minuto antes de reintentar.",
            status_code=429,
        )
    async with transaction(session):
        snapshot = await svc.search_live(
            session,
            actor,
            body.source_id,
            query=body.query,
            limit=body.limit,
        )
    return snapshot


@router.get(
    "/supplier-searches/{search_id}",
    response_model=SupplierSearchOut,
    responses=ERRORS,
    summary="Resultado de una búsqueda en vivo mientras siga vigente en memoria",
)
async def get_supplier_search(
    search_id: uuid.UUID, session: Session, actor: StrongActor
):
    return await svc.get_search(session, actor, search_id)
