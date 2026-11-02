"""E10 — cotizaciones, carritos y líneas; E13/O1 — observaciones comerciales (C5-A).

Todas las operaciones son de `admin` + `StrongActor` (D-130). La organización sale del `Actor` y
nunca del cuerpo. El precio no se guarda en la línea: se lee del `offer_snapshot` elegido (§3).

Esta fase **no** registra compras (C5-B), no implementa compras parciales y no interpreta
observaciones con IA (O2–O4): `advice` es `null` e `interpretation_status` es
`sin_interpretacion`, sin simular un proveedor.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Session, StrongActor
from app.api.v1.demand import ERRORS
from app.core.errors import DomainError
from app.db.session import transaction
from app.services import events
from app.services import quotes as quote_svc

router = APIRouter(tags=["cotizador"])

IdemKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        description="Repetir la misma clave y contenido devuelve el resultado original.",
    ),
]


# --------------------------------------------------------------------------- entrada


class QuoteCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=120, description="Nunca obligatorio.")


class QuotePatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = Field(default=None, description="Versión leída. Obligatoria.")
    title: str | None = Field(default=None, max_length=120)
    status: Literal["draft", "closed"] | None = None


class LineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: uuid.UUID
    offer_snapshot_id: uuid.UUID
    quantity: dict[str, Any] = Field(
        examples=[{"value": "3", "unit": "box"}],
        description="`value` entero > 0 (texto o número) y `unit` ∈ box|package|unit|bottle.",
    )
    demand_item_id: uuid.UUID | None = Field(
        default=None, description="Faltante que esta línea ayuda a cubrir. Opcional."
    )


class LinePatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = None
    quantity: dict[str, Any]
    quantity_unit: str | None = None


class ClosePendingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=2000)


class MoveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_offer_snapshot_id: uuid.UUID
    quantity: dict[str, Any]
    quantity_unit: str | None = None
    demand_item_id: uuid.UUID | None = Field(
        default=None,
        description="Vínculo nuevo y explícito; el original no se arrastra (§5.2).",
    )


class ObservationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    trade_item_id: uuid.UUID
    supplier_id: uuid.UUID | None = None
    offer_snapshot_id: uuid.UUID | None = None
    quote_id: uuid.UUID | None = None


class LineObservationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


# --------------------------------------------------------------------------- salida


class QuoteOut(BaseModel):
    quote_id: uuid.UUID
    title: str | None
    status: str
    version: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    closed_by: uuid.UUID | None
    closed_at: datetime | None
    data_origin: str
    derived_state: str
    carts: list[dict[str, Any]]
    coverage: list[dict[str, Any]]
    estimated_total: dict[str, Any]
    advice: None = None


class QuoteListItemOut(BaseModel):
    quote_id: uuid.UUID
    title: str | None
    status: str
    version: int
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    closed_by: uuid.UUID | None
    closed_at: datetime | None
    data_origin: str
    supplier_ids: list[uuid.UUID] = []
    supplier_count: int = 0
    line_count: int = 0


class QuoteListOut(BaseModel):
    quotes: list[QuoteListItemOut]
    next_cursor: str | None


class QuoteSummaryOut(BaseModel):
    quote_id: uuid.UUID
    title: str | None
    status: str
    version: int
    updated_at: datetime
    derived_state: str
    suppliers: list[dict[str, Any]]
    estimated_total: dict[str, Any]


class LineOut(BaseModel):
    line_id: uuid.UUID
    quote_id: uuid.UUID
    quote_cart_id: uuid.UUID
    supplier_id: uuid.UUID
    offer_snapshot_id: uuid.UUID
    trade_item_id: uuid.UUID
    quantity: dict[str, Any]
    status: str
    removed_at: datetime | None
    removed_by: uuid.UUID | None
    demand_item_id: uuid.UUID | None
    coverage_relation: str | None
    pending_closed_at: datetime | None
    pending_closed_by: uuid.UUID | None
    pending_closed_note: str | None
    purchase_state: str
    remainder: str
    selected: dict[str, Any]
    current: dict[str, Any] | None
    price_state: str
    causes: list[str]
    warnings: list[str]
    advice: None = None
    version: int
    added_by: uuid.UUID
    added_at: datetime
    updated_at: datetime


class LineAddedOut(BaseModel):
    line: LineOut
    cart: dict[str, Any]
    deduplicated: bool = False
    warnings: list[str] = []


class CartOut(BaseModel):
    cart_id: uuid.UUID
    supplier_id: uuid.UUID
    supplier_name: str | None
    status: str
    line_count: int
    version: int
    created_at: datetime
    updated_at: datetime
    derived_state: str
    quote_version: int


class ObservationOut(BaseModel):
    observation_id: uuid.UUID
    text: str
    trade_item_id: uuid.UUID
    supplier_id: uuid.UUID | None
    offer_snapshot_id: uuid.UUID | None
    quote_id: uuid.UUID | None
    quote_line_id: uuid.UUID | None
    author_id: uuid.UUID
    created_at: datetime
    context: dict[str, Any]
    data_origin: str
    interpretations: list[dict[str, Any]] = []
    interpretation_status: Literal["sin_interpretacion"] = "sin_interpretacion"


class ObservationListOut(BaseModel):
    observations: list[ObservationOut]


# --------------------------------------------------------------------------- E10: cotizaciones


@router.post(
    "/quotes",
    status_code=201,
    response_model=QuoteOut,
    responses=ERRORS,
    summary="Crear una cotización en borrador (E10)",
)
async def create_quote(body: QuoteCreateIn, session: Session, actor: StrongActor):
    async with transaction(session):
        quote = await quote_svc.create_quote(session, actor, title=body.title)
        return await quote_svc.quote_detail(session, actor, quote.id)


@router.get(
    "/quotes",
    response_model=QuoteListOut,
    responses=ERRORS,
    summary="Listar cotizaciones por `updated_at` (E10)",
)
async def list_quotes(
    session: Session,
    actor: StrongActor,
    status: Annotated[str | None, Query(pattern="^(draft|closed)$")] = None,
    updated_from: datetime | None = None,
    updated_to: datetime | None = None,
    q: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
):
    items, nxt = await quote_svc.list_quotes(
        session,
        actor,
        status=status,
        updated_from=updated_from,
        updated_to=updated_to,
        q=q,
        limit=limit,
        cursor=cursor,
    )
    return {"quotes": items, "next_cursor": nxt}


@router.get(
    "/quotes/{quote_id}",
    response_model=QuoteOut,
    responses=ERRORS,
    summary="Detalle de la cotización con carritos, evaluación E5 y cobertura (E10)",
)
async def get_quote(quote_id: uuid.UUID, session: Session, actor: StrongActor):
    return await quote_svc.quote_detail(session, actor, quote_id)


@router.get(
    "/quotes/{quote_id}/summary",
    response_model=QuoteSummaryOut,
    responses=ERRORS,
    summary="Resumen por proveedor sin detalle de líneas (E10)",
)
async def get_quote_summary(quote_id: uuid.UUID, session: Session, actor: StrongActor):
    return await quote_svc.quote_summary(session, actor, quote_id)


@router.patch(
    "/quotes/{quote_id}",
    response_model=QuoteOut,
    responses=ERRORS,
    summary="Cambiar título o cerrar/reabrir la cotización (requiere `version`)",
)
async def patch_quote(
    quote_id: uuid.UUID, body: QuotePatchIn, session: Session, actor: StrongActor
):
    changes = body.model_dump(exclude_unset=True)
    if "title" not in changes and "status" not in changes:
        raise DomainError(
            "VALIDATION_ERROR", "No hay cambios que aplicar.", status_code=400
        )
    async with transaction(session):
        await quote_svc.update_quote(
            session,
            actor,
            quote_id,
            expected_version=body.version,
            title=body.title,
            title_set="title" in changes,
            status=body.status,
        )
        return await quote_svc.quote_detail(session, actor, quote_id)


# --------------------------------------------------------------------------- E10: líneas


def _line_value(body: dict[str, Any]) -> tuple[Any, Any]:
    quantity = body or {}
    return quantity.get("value"), quantity.get("unit")


@router.post(
    "/quotes/{quote_id}/lines",
    status_code=201,
    response_model=LineAddedOut,
    responses=ERRORS,
    summary="Agregar una línea; crea el carrito del proveedor si no existe (nunca suma)",
)
async def add_line(
    quote_id: uuid.UUID,
    body: LineIn,
    session: Session,
    actor: StrongActor,
    response: Response,
    idempotency_key: IdemKey = None,
):
    value, unit = _line_value(body.quantity)
    key = idempotency_key
    digest = (
        events.request_hash(
            {
                "quote_id": str(quote_id),
                "supplier_id": str(body.supplier_id),
                "offer_snapshot_id": str(body.offer_snapshot_id),
                "value": str(value),
                "unit": unit,
                "demand_item_id": str(body.demand_item_id) if body.demand_item_id else None,
            }
        )
        if key
        else None
    )
    async with transaction(session):
        line, cart, quote, deduplicated, warnings = await quote_svc.add_line(
            session,
            actor,
            quote_id,
            supplier_id=body.supplier_id,
            offer_snapshot_id=body.offer_snapshot_id,
            quantity=value,
            quantity_unit=unit,
            demand_item_id=body.demand_item_id,
            idempotency_key=key,
            request_hash=digest,
        )
        out = {
            "line": await quote_svc.line_view(session, actor, quote, line),
            "cart": await quote_svc.cart_view(session, actor, quote, cart),
            "deduplicated": deduplicated,
            "warnings": warnings,
        }
    if deduplicated:
        response.status_code = 200
    return out


@router.patch(
    "/quotes/{quote_id}/lines/{line_id}",
    response_model=LineAddedOut,
    responses=ERRORS,
    summary="Fijar la cantidad de una línea (SET explícito, nunca suma; requiere `version`)",
)
async def patch_line(
    quote_id: uuid.UUID,
    line_id: uuid.UUID,
    body: LinePatchIn,
    session: Session,
    actor: StrongActor,
):
    value, unit = _line_value(body.quantity)
    async with transaction(session):
        line, cart, quote = await quote_svc.patch_line(
            session,
            actor,
            quote_id,
            line_id,
            expected_version=body.version,
            quantity=value,
            quantity_unit=body.quantity_unit or unit,
        )
        return {
            "line": await quote_svc.line_view(session, actor, quote, line),
            "cart": await quote_svc.cart_view(session, actor, quote, cart),
            "deduplicated": False,
            "warnings": [],
        }


@router.delete(
    "/quotes/{quote_id}/lines/{line_id}",
    response_model=LineOut,
    responses=ERRORS,
    summary="Quitar una línea de forma lógica (`removed`); nunca se borra",
)
async def remove_line(
    quote_id: uuid.UUID, line_id: uuid.UUID, session: Session, actor: StrongActor
):
    async with transaction(session):
        line, _, quote = await quote_svc.remove_line(session, actor, quote_id, line_id)
        return await quote_svc.line_view(session, actor, quote, line)


@router.post(
    "/quotes/{quote_id}/lines/{line_id}/close-pending",
    response_model=LineOut,
    responses=ERRORS,
    summary="Cerrar lo pendiente de una línea sin cambiarla ni borrarla",
)
async def close_pending(
    quote_id: uuid.UUID,
    line_id: uuid.UUID,
    session: Session,
    actor: StrongActor,
    body: ClosePendingIn | None = None,
):
    async with transaction(session):
        line, _, quote = await quote_svc.close_pending(
            session, actor, quote_id, line_id, note=body.note if body else None
        )
        return await quote_svc.line_view(session, actor, quote, line)


@router.post(
    "/quotes/{quote_id}/lines/{line_id}/reopen-pending",
    response_model=LineOut,
    responses=ERRORS,
    summary="Reabrir lo pendiente de una línea mientras la cotización esté en borrador",
)
async def reopen_pending(
    quote_id: uuid.UUID, line_id: uuid.UUID, session: Session, actor: StrongActor
):
    async with transaction(session):
        line, _, quote = await quote_svc.reopen_pending(session, actor, quote_id, line_id)
        return await quote_svc.line_view(session, actor, quote, line)


@router.post(
    "/quotes/{quote_id}/lines/{line_id}/move",
    response_model=LineAddedOut,
    responses=ERRORS,
    summary="Mover una línea a la oferta de otro proveedor (atómico, auditado)",
)
async def move_line(
    quote_id: uuid.UUID,
    line_id: uuid.UUID,
    body: MoveIn,
    session: Session,
    actor: StrongActor,
):
    value, unit = _line_value(body.quantity)
    async with transaction(session):
        line, cart, quote = await quote_svc.move_line(
            session,
            actor,
            quote_id,
            line_id,
            target_offer_snapshot_id=body.target_offer_snapshot_id,
            quantity=value,
            quantity_unit=body.quantity_unit or unit,
            demand_item_id=body.demand_item_id,
        )
        return {
            "line": await quote_svc.line_view(session, actor, quote, line),
            "cart": await quote_svc.cart_view(session, actor, quote, cart),
            "deduplicated": False,
            "warnings": [],
        }


@router.post(
    "/quotes/{quote_id}/lines/{line_id}/refresh",
    response_model=LineAddedOut,
    responses=ERRORS,
    summary="Aceptar el precio actual y reapuntar la línea (explícito y auditado)",
)
async def refresh_line(
    quote_id: uuid.UUID, line_id: uuid.UUID, session: Session, actor: StrongActor
):
    async with transaction(session):
        line, cart, quote, refreshed = await quote_svc.refresh_line(
            session, actor, quote_id, line_id
        )
        return {
            "line": await quote_svc.line_view(session, actor, quote, line),
            "cart": await quote_svc.cart_view(session, actor, quote, cart),
            "deduplicated": not refreshed,
            "warnings": [],
        }


# --------------------------------------------------------------------------- E10: carritos


@router.post(
    "/quotes/{quote_id}/carts/{supplier_id}/dismiss",
    response_model=CartOut,
    responses=ERRORS,
    summary="Descartar el carrito de un proveedor (no evalúa, no suma, no cubre)",
)
async def dismiss_cart(
    quote_id: uuid.UUID, supplier_id: uuid.UUID, session: Session, actor: StrongActor
):
    async with transaction(session):
        cart, quote = await quote_svc.set_cart_state(
            session, actor, quote_id, supplier_id, dismissed=True
        )
        return await quote_svc.cart_view(session, actor, quote, cart)


@router.post(
    "/quotes/{quote_id}/carts/{supplier_id}/reinstate",
    response_model=CartOut,
    responses=ERRORS,
    summary="Reincorporar un carrito descartado sin recrear sus líneas",
)
async def reinstate_cart(
    quote_id: uuid.UUID, supplier_id: uuid.UUID, session: Session, actor: StrongActor
):
    async with transaction(session):
        cart, quote = await quote_svc.set_cart_state(
            session, actor, quote_id, supplier_id, dismissed=False
        )
        return await quote_svc.cart_view(session, actor, quote, cart)


# --------------------------------------------------------------------------- E13 / O1


@router.post(
    "/commercial-observations",
    status_code=201,
    response_model=ObservationOut,
    responses=ERRORS,
    summary="Registrar una observación comercial sobre un artículo (O1)",
)
async def create_commercial_observation(
    body: ObservationIn, session: Session, actor: StrongActor
):
    async with transaction(session):
        row = await quote_svc.create_observation(
            session,
            actor,
            text=body.text,
            trade_item_id=body.trade_item_id,
            supplier_id=body.supplier_id,
            offer_snapshot_id=body.offer_snapshot_id,
            quote_id=body.quote_id,
        )
        return quote_svc.observation_payload(row)


@router.post(
    "/quotes/{quote_id}/lines/{line_id}/observations",
    status_code=201,
    response_model=ObservationOut,
    responses=ERRORS,
    summary="Observación asociada a una línea de la cotización (O1)",
)
async def create_line_observation(
    quote_id: uuid.UUID,
    line_id: uuid.UUID,
    body: LineObservationIn,
    session: Session,
    actor: StrongActor,
):
    async with transaction(session):
        row = await quote_svc.create_line_observation(
            session, actor, quote_id, line_id, text=body.text
        )
        return quote_svc.observation_payload(row)


@router.get(
    "/quotes/{quote_id}/lines/{line_id}/observations",
    response_model=ObservationListOut,
    responses=ERRORS,
    summary="Observaciones de una línea, más recientes primero (O1)",
)
async def list_line_observations(
    quote_id: uuid.UUID, line_id: uuid.UUID, session: Session, actor: StrongActor
):
    rows = await quote_svc.list_line_observations(session, actor, quote_id, line_id)
    return {"observations": [quote_svc.observation_payload(r) for r in rows]}


@router.get(
    "/commercial-observations",
    response_model=ObservationListOut,
    responses=ERRORS,
    summary="Historial de observaciones del artículo exacto (O1)",
)
async def list_commercial_observations(
    session: Session,
    actor: StrongActor,
    trade_item_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    rows = await quote_svc.list_observations_for_item(
        session, actor, trade_item_id=trade_item_id, limit=limit
    )
    return {"observations": [quote_svc.observation_payload(r) for r in rows]}
