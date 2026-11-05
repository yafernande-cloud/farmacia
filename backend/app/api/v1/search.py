"""E1–E3 — búsqueda de productos y ofertas; E15 — búsqueda de cadenas (C4).

Todas las operaciones son de `admin` + `StrongActor` (D-130): el cotizador es una herramienta
interna. E15 no persiste nada y su estado vive en el proceso (contrato §11.2).
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Session, StrongActor
from app.api.v1.demand import ERRORS
from app.api.v1.sources import SourceOut
from app.core.errors import DomainError
from app.core.ratelimit import SlidingWindow
from app.db.session import transaction
from app.services import chain_search as chain_svc
from app.services import search as search_svc

router = APIRouter(tags=["búsqueda"])

_chain_limits = SlidingWindow(limit=10, window_seconds=60)


class SearchFiltersIn(BaseModel):
    """Conjunto cerrado de filtros: solo restringen, nunca completan datos faltantes."""

    model_config = ConfigDict(extra="forbid")

    ingredient: str | None = Field(default=None, max_length=200)
    brand: str | None = Field(default=None, max_length=200)
    laboratory: str | None = Field(default=None, max_length=200)
    form: str | None = Field(default=None, max_length=30)
    gtin: str | None = Field(default=None, pattern="^[0-9]{8,14}$")


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=200, examples=["ibuprofeno 400"])
    filters: SearchFiltersIn = Field(default_factory=SearchFiltersIn)
    limit: int = Field(default=20, ge=1, le=search_svc.MAX_LIMIT)


class ChainSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=200, examples=["losartán 50 mg"])
    chains: list[Literal["cruz_verde", "salcobrand", "ahumada"]] = Field(default_factory=list)
    purpose: Literal["search", "quote_verification"] = "search"


# --------------------------------------------------------------- salidas (§5, §11.3 y §4.2 de C0)


class MoneyOut(BaseModel):
    """Dinero como texto decimal + moneda: nunca float."""

    amount: str
    currency: str


class InterpretationOut(BaseModel):
    attributes: list[dict[str, Any]]
    unresolved_terms: list[str]
    warnings: list[str]


class SearchQueryOut(BaseModel):
    original_text: str
    filters: dict[str, Any]
    interpretation: InterpretationOut


class PresentationOut(BaseModel):
    commercial_unit: dict[str, Any] | None
    units_per_commercial_unit: str | None
    content: dict[str, Any] | None


class MatchOut(BaseModel):
    level: Literal["exact", "needs_review"]
    causes: list[str]


class SearchResultOut(BaseModel):
    article_id: uuid.UUID
    display_name: str
    identity_summary: str
    presentation: PresentationOut
    match: MatchOut
    relation: Literal["exact", "other_presentation", "combined", "related", "needs_review"]
    offer_count: int
    supplier_count: int
    lowest_verified_price: MoneyOut | None
    sources: list[uuid.UUID]


class SearchSourceOut(SourceOut):
    """Estado de la fuente en ESTA corrida; `status` es el guardado, no el de hoy."""

    outcome: Literal["matches", "no_matches"] | None
    matches_count: int


class SearchErrorOut(BaseModel):
    source_id: uuid.UUID
    code: str | None
    message: str | None
    retryable: bool


class SearchOut(BaseModel):
    search_id: uuid.UUID
    status: Literal["completed", "partial", "failed"]
    query: SearchQueryOut
    generated_at: datetime
    results: list[SearchResultOut]
    sources: list[SearchSourceOut]
    errors: list[SearchErrorOut]
    next_cursor: str | None


class OfferSupplierOut(BaseModel):
    supplier_id: uuid.UUID
    name: str
    status: str


class OfferPriceOut(BaseModel):
    money: MoneyOut
    basis: Literal["per_commercial_unit", "per_base_unit"]
    tax_included: bool | None
    discount_included: bool | None


class PricePerBaseUnitOut(MoneyOut):
    base_unit: str | None


class SaleUnitOut(BaseModel):
    code: str
    original_text: str | None


class PackOut(BaseModel):
    units_per_commercial_unit: str
    base_unit: str | None
    original_text: str | None


class ThresholdOut(BaseModel):
    """`known=false` es desconocido: la cantidad va en `null`, nunca en 0 ni 1."""

    known: bool
    quantity: str | None
    unit: str | None


class StockOut(BaseModel):
    status: Literal["known", "unknown"]
    quantity: str | None
    unit: str | None


class ValidityOut(BaseModel):
    valid_from: datetime | None
    valid_to: datetime | None
    is_current: bool


class ProvenanceSourceOut(BaseModel):
    source_id: uuid.UUID
    name: str
    mode: str


class OfferProvenanceOut(BaseModel):
    source: ProvenanceSourceOut
    data_origin: str
    observed_at: datetime
    evidence: dict[str, Any] | None


class FreshnessOut(BaseModel):
    observed_at: datetime
    fresh_until: datetime
    is_stale: bool
    stale_fields: list[str]


class OfferOut(BaseModel):
    """`OfferOut` de C0 §4.2. Un dato desconocido viaja como `null`."""

    offer_id: uuid.UUID
    article_id: uuid.UUID
    supplier: OfferSupplierOut
    external_id: str
    external_id_kind: str
    price: OfferPriceOut
    price_per_base_unit: PricePerBaseUnitOut | None
    sale_unit: SaleUnitOut
    pack: PackOut | None
    minimum: ThresholdOut
    multiple: ThresholdOut
    stock: StockOut
    taxes: list[dict[str, Any]]
    discounts: list[dict[str, Any]]
    shipping_terms_id: uuid.UUID | None
    validity: ValidityOut
    notes: str | None
    provenance: OfferProvenanceOut
    freshness: FreshnessOut


class ArticleIdentityOut(BaseModel):
    ingredient: str | None
    strengths: list[dict[str, Any]]
    form: dict[str, Any] | None
    route: str | None
    release: dict[str, Any] | None
    brand: str | None
    laboratory: str | None
    gtin: str | None
    sanitary_registration: str | None


class ArticlePresentationOut(BaseModel):
    packaging: dict[str, Any] | None
    content: dict[str, Any] | None
    commercial_unit: dict[str, Any] | None
    units_per_commercial_unit: str | None


class ArticleOut(BaseModel):
    article_id: uuid.UUID
    display_name: str
    identity: ArticleIdentityOut
    presentation: ArticlePresentationOut
    match: MatchOut
    status: str
    data_origin: str


class ArticleDetailOut(BaseModel):
    article: ArticleOut
    offers: list[OfferOut]
    offers_total: int
    next_cursor: str | None
    # Reservados en C4: la referencia de cadena no es una oferta comprable (C0-05).
    reference_prices: list[dict[str, Any]]
    annotations: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    sources: list[SourceOut]
    generated_at: datetime


class ChainResultOut(BaseModel):
    """Ficha publicada por una cadena (§11.3). Solo contiene datos publicados por esa cadena.

    `brand_as_published` y `has_stock` son opcionales para no alterar las fichas de conectores que
    no publican esos campos; cada conector los informa únicamente cuando su fuente los entrega.
    """

    chain_item_id: str
    published_name: str
    brand_as_published: str | None = None
    laboratory_as_published: str | None = None
    presentation_as_published: str | None = None
    description: str | None = None
    image_url: str | None = None
    price: dict[str, Any]
    conditions: str | None = None
    has_stock: bool | None = None
    source_url: str
    observed_at: datetime
    provenance: dict[str, Any]
    match: dict[str, Any]


class ChainSourceOut(BaseModel):
    chain: Literal["cruz_verde", "salcobrand", "ahumada"]
    state: Literal["ok", "stale", "failed", "not_configured"]
    reason: str | None = None
    outcome: Literal["matches", "no_matches"] | None = None
    observed_at: datetime | None = None
    results: list[ChainResultOut] = Field(default_factory=list)


class ChainSearchAcceptedOut(BaseModel):
    search_id: str
    status: Literal["pending"]
    expires_at: datetime


class ChainSearchOut(BaseModel):
    search_id: str
    status: Literal["completed", "failed"]
    query: str
    purpose: Literal["search", "quote_verification"]
    created_at: datetime
    expires_at: datetime
    sources: list[ChainSourceOut]


@router.post(
    "/searches",
    response_model=SearchOut,
    responses=ERRORS,
    summary="Buscar productos y ofertas en el catálogo local (E1)",
)
async def create_search(body: SearchIn, session: Session, actor: StrongActor):
    async with transaction(session):
        run, results, source_rows, sources = await search_svc.run_search(
            session,
            actor,
            query=body.query,
            filters=body.filters.model_dump(exclude_none=True),
            limit=body.limit,
        )
        return search_svc.search_payload(run, results, source_rows, sources, limit=body.limit)


@router.get(
    "/searches/{search_id}",
    response_model=SearchOut,
    responses=ERRORS,
    summary="Recuperar una búsqueda guardada, sin reejecutarla (E2)",
)
async def get_search(
    search_id: uuid.UUID,
    session: Session,
    actor: StrongActor,
    cursor: Annotated[str | None, Query(description="Cursor de la página anterior")] = None,
    limit: Annotated[int, Query(ge=1, le=search_svc.MAX_LIMIT)] = 20,
):
    run, results, source_rows, sources = await search_svc.load_search(session, actor, search_id)
    start = search_svc.decode_result_cursor(cursor) if cursor else 0
    return search_svc.search_payload(
        run, results, source_rows, sources, limit=limit, cursor=start
    )


@router.get(
    "/articles/{article_id}",
    response_model=ArticleDetailOut,
    responses=ERRORS,
    summary="Detalle de un artículo y sus últimas ofertas por fuente (E3)",
)
async def get_article(
    article_id: uuid.UUID,
    session: Session,
    actor: StrongActor,
    cursor: Annotated[str | None, Query(description="Cursor de la página anterior")] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
):
    # La lectura aplica preferencias y deja su traza interna (AC31/D-141); el commit la conserva.
    async with transaction(session):
        return await search_svc.article_detail(
            session, actor, article_id, limit=limit, cursor=cursor
        )


@router.post(
    "/chain-searches",
    status_code=202,
    response_model=ChainSearchAcceptedOut,
    responses=ERRORS,
    summary="Iniciar la búsqueda de precios públicos de cadenas (E15; sin persistencia)",
)
async def start_chain_search(body: ChainSearchIn, actor: StrongActor):
    if not await _chain_limits.allow(str(actor.id)):
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": "60"},
            content=DomainError(
                "RATE_LIMITED",
                "Demasiadas búsquedas de cadenas. Intenta de nuevo en un minuto.",
                status_code=429,
            ).to_body(),
        )
    snapshot = await chain_svc.store.start(body.query, list(dict.fromkeys(body.chains)),
                                          purpose=body.purpose)  # fmt: skip
    return {
        "search_id": snapshot["search_id"],
        "status": "pending",
        "expires_at": snapshot["expires_at"],
    }


@router.get(
    "/chain-searches/{search_id}",
    response_model=ChainSearchOut,
    responses=ERRORS,
    summary="Sondear el resultado de una búsqueda de cadenas (E15)",
)
async def get_chain_search(search_id: str, actor: StrongActor):
    snapshot = await chain_svc.store.get(search_id)
    if snapshot is None:
        raise DomainError(
            "SEARCH_NOT_FOUND",
            "La búsqueda no existe, expiró o se perdió al reiniciar el servicio.",
            status_code=404,
        )
    return snapshot
