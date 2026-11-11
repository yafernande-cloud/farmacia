"""E1–E3 — búsqueda de productos y ofertas sobre el catálogo local (C4, D-075).

La búsqueda es **síncrona** y consulta el catálogo ya cargado (`trade_item` + `offer_snapshot`).
No hay conectores externos en esta fase: no se inventan fuentes, precios ni stock, y una fuente sin
observaciones se informa `not_configured`, no «sin resultados».

Lo determinista se resuelve con reglas, sin IA (HLD §14.2): la consulta se interpreta con el mismo
analizador conservador de los faltantes y la relación comercial se clasifica en `domain/matching.py`.
La corrida se guarda completa para que E2 devuelva el mismo snapshot sin reejecutar nada.
"""

import base64
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import DomainError, not_found
from app.core.permissions import require_admin
from app.domain.matching import (
    RELATION_RANK,
    Identity,
    classify,
    identity_from_item,
    identity_from_query,
    match_level,
)
from app.domain.products import (
    FORMS,
    PACKAGING,
    STOPWORDS,
    ParsedProduct,
    normalize_name,
    parse_shortage_text,
)
from app.domain.units import UNITS
from app.models import (
    OfferSnapshot,
    SearchResult,
    SearchRun,
    SearchSourceResult,
    Source,
    Supplier,
    TradeItem,
    TradeItemAlias,
)
from app.services import preferences as preferences_svc
from app.services import sources as sources_svc
from app.services.actors import Actor
from app.services.import_freshness import current_list_filter, latest_valid_run_ids
from app.services.provenance import org_kind, origin_for

FILTER_KEYS = ("ingredient", "brand", "laboratory", "form", "gtin")
MAX_LIMIT = 50
ALIAS_SOURCES = ("search",)


# --------------------------------------------------------------------------- interpretación


def _interpretation(parsed: ParsedProduct | None) -> dict[str, Any]:
    """Lo entendido, lo NO entendido y las advertencias. Nunca completa una unidad ni un rol."""
    if parsed is None:
        return {
            "attributes": [],
            "unresolved_terms": [],
            "warnings": ["consulta no interpretada como producto: se busca por texto"],
        }
    attributes: list[dict[str, Any]] = [
        {"attribute": "ingredient", "value": parsed.ingredient, "basis": "explicit"}
    ]
    for s in parsed.strengths:
        attributes.append(
            {
                "attribute": "strength",
                "value": s.measure.label,
                "basis": s.basis.value,
            }
        )
    if parsed.form is not None:
        attributes.append(
            {"attribute": "form", "value": parsed.form.value, "basis": parsed.form.basis.value}
        )
    if parsed.content is not None:
        attributes.append(
            {
                "attribute": "content",
                "value": parsed.content.measure.label,
                "basis": parsed.content.basis.value,
            }
        )
    warnings: list[str] = []
    for m in parsed.unassigned:
        if m.unit is None:
            warnings.append(f"cifra sin unidad: no se asume mg ({m.label})")
        else:
            warnings.append(f"medida sin rol claro: no se usa como potencia ({m.label})")
    return {
        "attributes": attributes,
        "unresolved_terms": [m.label for m in parsed.unassigned],
        "warnings": warnings,
    }


def _parse(query: str) -> ParsedProduct | None:
    try:
        return parse_shortage_text(query, allow_no_product=True)
    except DomainError:
        # Una consulta libre no es un aviso: se busca por texto y se declara lo que no se interpretó.
        return None


# Palabras que no identifican un producto: unidades, formas, envoltorios y cifras.
_NOT_A_NAME = set(FORMS) | set(PACKAGING) | set(UNITS)


def _tokens(query: str) -> list[str]:
    """Términos de nombre: «400 mg comprimidos» no son términos, son atributos."""
    words = normalize_name(query).split()
    return [
        w
        for w in words
        if len(w) >= 2 and not w.isdigit() and w not in STOPWORDS and w not in _NOT_A_NAME
    ]


# --------------------------------------------------------------------------- catálogo y ofertas


def _current(offer: OfferSnapshot, now: datetime) -> bool:
    if offer.valid_from is not None and offer.valid_from > now:
        return False
    return not (offer.valid_to is not None and offer.valid_to < now)


async def _latest_offers(
    session: AsyncSession, org: uuid.UUID, item_ids: list[uuid.UUID], *, now: datetime
) -> dict[uuid.UUID, list[OfferSnapshot]]:
    """Última observación por (artículo, fuente) de la lista vigente de cada fuente.

    Para las fuentes de archivo solo compiten las ofertas de la última importación con filas
    aceptadas: una fila ausente o rechazada en la lista nueva no se presenta como precio actual
    (D-151). Las fuentes en vivo, sin `import_run`, conservan la lectura anterior.
    """
    if not item_ids:
        return {}
    statement = (
        select(OfferSnapshot)
        .where(
            OfferSnapshot.organization_id == org,
            OfferSnapshot.trade_item_id.in_(item_ids),
        )
        .ext(distinct_on(OfferSnapshot.trade_item_id, OfferSnapshot.source_id))
        .order_by(
            OfferSnapshot.trade_item_id,
            OfferSnapshot.source_id,
            OfferSnapshot.observed_at.desc(),
            OfferSnapshot.created_at.desc(),
        )
    )
    run_filter = current_list_filter(await latest_valid_run_ids(session, org))
    if run_filter is not None:
        statement = statement.where(run_filter)
    rows = (await session.execute(statement)).scalars().all()
    grouped: dict[uuid.UUID, list[OfferSnapshot]] = {}
    for o in rows:
        grouped.setdefault(o.trade_item_id, []).append(o)
    return grouped


def _aggregate(offers: list[OfferSnapshot], *, now: datetime) -> dict[str, Any]:
    """Agregados de la página: solo ofertas vigentes y aceptadas cuentan como verificadas."""
    current = [o for o in offers if _current(o, now)]
    verified = [o for o in current if o.review_status == "accepted"]
    supplier_ids = {o.supplier_id for o in verified}
    lowest_amount: Decimal | None = None
    lowest_currency: str | None = None
    if verified:
        currencies = {o.currency for o in verified}
        bases = {o.price_basis for o in verified}
        # Comparar monedas o bases distintas produciría un mínimo falso: se declara desconocido.
        if len(currencies) == 1 and len(bases) == 1:
            best = min(verified, key=lambda o: (o.price_amount, str(o.id)))
            lowest_amount, lowest_currency = best.price_amount, best.currency
    return {
        "offer_count": len(verified),
        "supplier_count": len(supplier_ids),
        "lowest_price_amount": lowest_amount,
        "lowest_price_currency": lowest_currency,
        "source_ids": sorted({str(o.source_id) for o in verified}),
    }


PACK_UNITS = {"caja": "box", "frasco": "bottle", "envase": "package", "unidad": "unit"}


def _packaging(attributes: dict[str, Any]) -> dict[str, Any] | None:
    """Envoltorio tal como se declaró. Desconocido = `null`: no se asume caja."""
    attrs = attributes if isinstance(attributes, dict) else {}
    packaging = attrs.get("packaging")
    value = packaging.get("value") if isinstance(packaging, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.split())
    return {"code": PACK_UNITS.get(normalize_name(text)), "original_text": text}


def _content_measure(attributes: dict[str, Any]) -> dict[str, Any] | None:
    attrs = attributes if isinstance(attributes, dict) else {}
    content_attr = attrs.get("content")
    measure = content_attr.get("measure") if isinstance(content_attr, dict) else None
    return measure if isinstance(measure, dict) else None


def _presentation(attributes: dict[str, Any]) -> dict[str, Any]:
    measure = _content_measure(attributes)
    unit = (measure or {}).get("unit")
    return {
        "commercial_unit": _packaging(attributes),
        # Solo cuando el contenido está expresado en unidades del envase.
        "units_per_commercial_unit": (measure or {}).get("value") if unit == "un" else None,
        "content": measure,
    }


def _identity_summary(item: TradeItem, identity: Identity) -> str:
    parts: list[str] = []
    if identity.ingredient:
        parts.append(identity.ingredient)
    strengths = sorted(identity.strengths)
    parts.extend(strengths)
    if identity.form:
        parts.append(identity.form)
    return " · ".join(parts) if parts else item.display_name


# --------------------------------------------------------------------------- filtros y candidatos


def _matches_filters(item: TradeItem, identity: Identity, filters: dict[str, Any]) -> bool:
    if filters.get("ingredient"):
        wanted = normalize_name(filters["ingredient"])
        if wanted not in identity.all_ingredients:
            return False
    if filters.get("brand"):
        wanted = normalize_name(filters["brand"])
        if not identity.brand or wanted not in identity.brand:
            return False
    if filters.get("laboratory"):
        wanted = normalize_name(filters["laboratory"])
        if not identity.laboratory or wanted not in identity.laboratory:
            return False
    if filters.get("form"):
        wanted = normalize_name(filters["form"])
        if identity.form != wanted:
            return False
    if filters.get("gtin"):
        if item.gtin != filters["gtin"]:
            return False
    return True


def _text_match(tokens: list[str], haystack: str) -> bool:
    return any(t in haystack for t in tokens)


# --------------------------------------------------------------------------- E1


async def run_search(
    session: AsyncSession,
    actor: Actor,
    *,
    query: str,
    filters: dict[str, Any],
    limit: int,
    now: datetime | None = None,
) -> tuple[SearchRun, list[SearchResult], list[SearchSourceResult], dict[uuid.UUID, Source]]:
    """Ejecuta E1 y guarda la corrida completa. No hace commit (lo hace la API)."""
    require_admin(actor.roles)
    now = now or datetime.now(UTC)
    parsed = _parse(query)
    interpretation = _interpretation(parsed)
    tokens = _tokens(query)

    items = (
        (
            await session.execute(
                select(TradeItem).where(
                    TradeItem.organization_id == actor.organization_id,
                    TradeItem.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    aliases = (
        await session.execute(
            select(TradeItemAlias.trade_item_id, TradeItemAlias.normalized_alias).where(
                TradeItemAlias.organization_id == actor.organization_id,
                TradeItemAlias.status == "active",
            )
        )
    ).all()
    alias_by_item: dict[uuid.UUID, list[str]] = {}
    for item_id, alias in aliases:
        alias_by_item.setdefault(item_id, []).append(normalize_name(alias))

    query_identity = identity_from_query(parsed) if parsed is not None else Identity()

    candidates: list[tuple[TradeItem, Identity, str, list[str]]] = []
    for item in items if tokens else []:
        identity = identity_from_item(
            ingredient=item.ingredient,
            brand=item.brand,
            laboratory=item.laboratory,
            attributes=item.attributes,
        )
        haystack = " ".join(
            [
                normalize_name(item.display_name),
                identity.ingredient or "",
                normalize_name(item.brand or ""),
                normalize_name(item.laboratory or ""),
                " ".join(alias_by_item.get(item.id, [])),
            ]
        )
        if tokens and not _text_match(tokens, haystack):
            continue
        if not _matches_filters(item, identity, filters):
            continue
        relation, causes = classify(query_identity, identity)
        candidates.append((item, identity, relation, causes))

    offered = await _latest_offers(
        session, actor.organization_id, [c[0].id for c in candidates], now=now
    )

    rows: list[dict[str, Any]] = []
    for item, identity, relation, causes in candidates:
        aggregate = _aggregate(offered.get(item.id, []), now=now)
        rows.append(
            {
                "item": item,
                "identity": identity,
                "relation": relation,
                "causes": causes,
                "aggregate": aggregate,
            }
        )
    rows.sort(
        key=lambda r: (
            RELATION_RANK[r["relation"]],
            -r["aggregate"]["offer_count"],
            normalize_name(r["item"].display_name),
            str(r["item"].id),
        )
    )

    origin = origin_for(await org_kind(session, actor.organization_id))
    run = SearchRun(
        organization_id=actor.organization_id,
        query_text=query,
        filters=filters,
        interpretation=interpretation,
        status="completed",
        limit_value=limit,
        result_count=len(rows),
        error_count=0,
        generated_at=now,
        created_by=actor.id,
        data_origin=origin,
    )
    session.add(run)
    await session.flush()

    results: list[SearchResult] = []
    matches_by_source: dict[uuid.UUID, int] = {}
    for rank, row in enumerate(rows):
        item, identity, aggregate = row["item"], row["identity"], row["aggregate"]
        result = SearchResult(
            organization_id=actor.organization_id,
            search_run_id=run.id,
            trade_item_id=item.id,
            rank=rank,
            relation=row["relation"],
            match_level=match_level(row["relation"]),
            match_causes=row["causes"],
            display_name=item.display_name,
            identity_summary=_identity_summary(item, identity),
            presentation=_presentation(item.attributes),
            offer_count=aggregate["offer_count"],
            supplier_count=aggregate["supplier_count"],
            lowest_price_amount=aggregate["lowest_price_amount"],
            lowest_price_currency=aggregate["lowest_price_currency"],
            source_ids=aggregate["source_ids"],
        )
        session.add(result)
        results.append(result)
        for source_id in aggregate["source_ids"]:
            key = uuid.UUID(source_id)
            matches_by_source[key] = matches_by_source.get(key, 0) + 1

    source_rows, sources = await _source_rows(
        session, actor, run=run, matches_by_source=matches_by_source, offers=offered, now=now
    )
    run.error_count = sum(1 for s in source_rows if s.state == "failed")
    if source_rows:
        failed = run.error_count
        usable = sum(1 for s in source_rows if s.state != "failed")
        run.status = "failed" if failed and not usable else ("partial" if failed else "completed")
    await session.flush()
    return run, results, source_rows, sources


async def _source_rows(
    session: AsyncSession,
    actor: Actor,
    *,
    run: SearchRun,
    matches_by_source: dict[uuid.UUID, int],
    offers: dict[uuid.UUID, list[OfferSnapshot]],
    now: datetime,
) -> tuple[list[SearchSourceResult], dict[uuid.UUID, Source]]:
    """Estado por fuente de ESTA corrida.

    La búsqueda lee el catálogo local, así que una fuente «consultada» es una fuente de proveedor
    habilitada con capacidad `search`: se informa el estado derivado de sus observaciones guardadas
    (E4) y cuántos resultados aportó. Una fuente sin ninguna observación queda `not_configured`.
    """
    rows = (
        (
            await session.execute(
                select(Source).where(
                    Source.organization_id == actor.organization_id,
                    Source.kind == "supplier",
                    Source.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    configured = [s for s in rows if "search" in (s.capabilities or [])]
    observed_at_by_source: dict[uuid.UUID, datetime] = {}
    for source_offers in offers.values():
        for o in source_offers:
            prev = observed_at_by_source.get(o.source_id)
            if prev is None or o.observed_at > prev:
                observed_at_by_source[o.source_id] = o.observed_at

    out: list[SearchSourceResult] = []
    for source in configured:
        state = sources_svc.source_status(
            source, now=now, stale_after_hours=get_settings().source_stale_after_hours
        )
        matches = matches_by_source.get(source.id, 0)
        out.append(
            SearchSourceResult(
                organization_id=actor.organization_id,
                search_run_id=run.id,
                source_id=source.id,
                state=state,
                outcome=("matches" if matches else "no_matches")
                if state in ("ok", "stale")
                else None,
                error_code="SOURCE_FAILED" if state == "failed" else None,
                error_message=source.last_error if state == "failed" else None,
                retryable=True if state == "failed" else None,
                matches_count=matches,
                observed_at=observed_at_by_source.get(source.id),
            )
        )
    session.add_all(out)
    await session.flush()
    return out, {s.id: s for s in configured}


def search_payload(
    run: SearchRun,
    results: list[SearchResult],
    source_rows: list[SearchSourceResult],
    sources: dict[uuid.UUID, Source],
    *,
    limit: int,
    cursor: int = 0,
) -> dict[str, Any]:
    """Respuesta de E1/E2. `next_cursor` pagina sobre las filas guardadas, no reejecuta nada."""
    page = results[cursor : cursor + limit]
    next_cursor = None
    if cursor + limit < len(results):
        next_cursor = _encode_result_cursor(cursor + limit)
    source_payloads = []
    now = datetime.now(UTC)
    for row in source_rows:
        row_source = sources.get(row.source_id)
        if row_source is None:
            continue
        payload = sources_svc.source_payload(row_source, now=now)
        payload["status"] = row.state  # el estado guardado en la corrida, no el de hoy
        payload["outcome"] = row.outcome
        payload["matches_count"] = row.matches_count
        source_payloads.append(payload)
    return {
        "search_id": run.id,
        "status": run.status,
        "query": {
            "original_text": run.query_text,
            "filters": run.filters,
            "interpretation": run.interpretation,
        },
        "generated_at": run.generated_at,
        "results": [
            {
                "article_id": r.trade_item_id,
                "display_name": r.display_name,
                "identity_summary": r.identity_summary,
                "presentation": r.presentation,
                "match": {"level": r.match_level, "causes": r.match_causes},
                "relation": r.relation,
                "offer_count": r.offer_count,
                "supplier_count": r.supplier_count,
                "lowest_verified_price": (
                    {"amount": str(r.lowest_price_amount), "currency": r.lowest_price_currency}
                    if r.lowest_price_amount is not None
                    else None
                ),
                "sources": [uuid.UUID(s) for s in r.source_ids],
            }
            for r in page
        ],
        "sources": source_payloads,
        "errors": [
            {
                "source_id": row.source_id,
                "code": row.error_code,
                "message": row.error_message,
                "retryable": bool(row.retryable),
            }
            for row in source_rows
            if row.state == "failed"
        ],
        "next_cursor": next_cursor,
    }


def _encode_result_cursor(rank: int) -> str:
    return base64.urlsafe_b64encode(f"r|{rank}".encode()).decode()


def decode_result_cursor(cursor: str) -> int:
    try:
        kind, raw = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        if kind != "r":
            raise ValueError
        rank = int(raw)
        if rank < 0:
            raise ValueError
        return rank
    except Exception:
        raise DomainError("INVALID_CURSOR", "Cursor inválido.", status_code=400) from None


async def load_search(
    session: AsyncSession, actor: Actor, search_id: uuid.UUID
) -> tuple[SearchRun, list[SearchResult], list[SearchSourceResult], dict[uuid.UUID, Source]]:
    require_admin(actor.roles)
    run = (
        await session.execute(
            select(SearchRun).where(
                SearchRun.id == search_id,
                SearchRun.organization_id == actor.organization_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise not_found("La búsqueda")
    results = (
        (
            await session.execute(
                select(SearchResult)
                .where(SearchResult.search_run_id == run.id)
                .order_by(SearchResult.rank)
            )
        )
        .scalars()
        .all()
    )
    source_rows = (
        (
            await session.execute(
                select(SearchSourceResult)
                .where(SearchSourceResult.search_run_id == run.id)
                .order_by(SearchSourceResult.id)
            )
        )
        .scalars()
        .all()
    )
    source_ids = [r.source_id for r in source_rows]
    sources = {
        s.id: s
        for s in (
            await session.execute(select(Source).where(Source.id.in_(source_ids)))
        )
        .scalars()
        .all()
    }
    return run, list(results), list(source_rows), sources


# --------------------------------------------------------------------------- E3


def _money(amount: Decimal, currency: str) -> dict[str, str]:
    return {"amount": f"{amount.quantize(Decimal('0.01'))}", "currency": currency}


def offer_payload(offer: OfferSnapshot, supplier: Supplier, source: Source, *, now: datetime) -> dict:
    """`OfferOut` de C0 §4.2. Un dato desconocido viaja como `null`, nunca como 0 ni `false`."""
    fresh_until = offer.observed_at + timedelta(hours=get_settings().source_stale_after_hours)
    price_per_base_unit = None
    if (
        offer.price_basis == "per_commercial_unit"
        and offer.pack_units_per_commercial_unit
        and offer.pack_units_per_commercial_unit > 0
    ):
        price_per_base_unit = {
            "amount": str(
                (offer.price_amount / offer.pack_units_per_commercial_unit).quantize(
                    Decimal("0.0001")
                )
            ),
            "currency": offer.currency,
            "base_unit": offer.pack_base_unit,
        }
    is_current = _current(offer, now)
    return {
        "offer_id": offer.id,
        "article_id": offer.trade_item_id,
        "supplier": {
            "supplier_id": supplier.id,
            "name": supplier.name,
            "status": supplier.status,
        },
        "external_id": offer.external_id,
        "external_id_kind": offer.external_id_kind,
        "price": {
            "money": _money(offer.price_amount, offer.currency),
            "basis": offer.price_basis,
            "tax_included": offer.tax_included,
            "discount_included": offer.discount_included,
        },
        "price_per_base_unit": price_per_base_unit,
        "sale_unit": {"code": offer.sale_unit, "original_text": offer.sale_unit_text},
        "pack": (
            {
                "units_per_commercial_unit": str(offer.pack_units_per_commercial_unit),
                "base_unit": offer.pack_base_unit,
                "original_text": offer.pack_text,
            }
            if offer.pack_units_per_commercial_unit is not None
            else None
        ),
        "minimum": {
            "known": offer.minimum_known,
            "quantity": str(offer.minimum_quantity)
            if offer.minimum_quantity is not None
            else None,
            "unit": offer.minimum_unit,
        },
        "multiple": {
            "known": offer.multiple_known,
            "quantity": str(offer.multiple_quantity)
            if offer.multiple_quantity is not None
            else None,
            "unit": offer.multiple_unit,
        },
        "stock": {
            "status": offer.stock_status,
            "quantity": str(offer.stock_quantity) if offer.stock_quantity is not None else None,
            "unit": offer.stock_unit,
        },
        "taxes": offer.taxes or [],
        "discounts": offer.discounts or [],
        "shipping_terms_id": offer.shipping_terms_id,
        "validity": {
            "valid_from": offer.valid_from,
            "valid_to": offer.valid_to,
            "is_current": is_current,
        },
        "notes": offer.notes,
        "provenance": {
            "source": {"source_id": source.id, "name": source.name, "mode": source.mode},
            "data_origin": offer.data_origin,
            "observed_at": offer.observed_at,
            "evidence": offer.evidence,
        },
        "freshness": {
            "observed_at": offer.observed_at,
            "fresh_until": fresh_until,
            "is_stale": now > fresh_until,
            "stale_fields": [],
        },
    }


async def article_detail(
    session: AsyncSession,
    actor: Actor,
    article_id: uuid.UUID,
    *,
    limit: int = 50,
    cursor: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """E3. Las ofertas son las últimas por fuente; el detalle no cotiza ni elige proveedor."""
    require_admin(actor.roles)
    now = now or datetime.now(UTC)
    item = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.id == article_id,
                TradeItem.organization_id == actor.organization_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise not_found("El artículo")

    offers = await _latest_offers(session, actor.organization_id, [item.id], now=now)
    item_offers = sorted(
        offers.get(item.id, []),
        key=lambda o: (o.observed_at, str(o.id)),
        reverse=True,
    )
    # El orden por preferencias comerciales del producto (A6/O4) se aplica sobre el listado
    # completo y conserva el orden anterior como desempate estable. Todas las ofertas siguen
    # visibles: nada se filtra, oculta ni recalcula.
    item_prefs = (
        await preferences_svc.load_item_preferences(session, actor.organization_id, [item.id])
    ).get(item.id, preferences_svc.EMPTY)
    if item_prefs.has_effects and item_offers:
        ranks = preferences_svc.rank_offers(
            item_prefs, [preferences_svc.offer_view(o) for o in item_offers]
        )
        before = [o.id for o in item_offers]
        item_offers = sorted(item_offers, key=lambda o: ranks[o.id])
        await preferences_svc.record_trace_best_effort(
            session,
            organization_id=actor.organization_id,
            trade_item_id=item.id,
            context="article_detail",
            prefs=item_prefs,
            offer_ids_before=before,
            offer_ids_after=[o.id for o in item_offers],
            now=now,
        )
    start = 0
    if cursor:
        start = decode_result_cursor(cursor)
    page = item_offers[start : start + limit]
    next_cursor = _encode_result_cursor(start + limit) if start + limit < len(item_offers) else None

    supplier_ids = {o.supplier_id for o in page}
    source_ids = {o.source_id for o in page}
    suppliers = {
        s.id: s
        for s in (
            await session.execute(select(Supplier).where(Supplier.id.in_(supplier_ids)))
        )
        .scalars()
        .all()
    }
    sources = {
        s.id: s
        for s in (
            await session.execute(select(Source).where(Source.id.in_(source_ids)))
        )
        .scalars()
        .all()
    }

    attributes = item.attributes if isinstance(item.attributes, dict) else {}
    identity = identity_from_item(
        ingredient=item.ingredient,
        brand=item.brand,
        laboratory=item.laboratory,
        attributes=item.attributes,
    )
    form = attributes.get("form") if isinstance(attributes.get("form"), dict) else None
    release = attributes.get("release") if isinstance(attributes.get("release"), dict) else None
    packaging = (
        attributes.get("packaging") if isinstance(attributes.get("packaging"), dict) else None
    )
    content_attr = attributes.get("content") if isinstance(attributes.get("content"), dict) else None
    strengths = [
        {
            "measure": s.get("measure"),
            "kind": s.get("kind"),
            "basis": s.get("basis", "explicit"),
            "role_rule": s.get("role_rule", "unit"),
        }
        for s in (attributes.get("strengths") or [])
        if isinstance(s, dict)
    ]
    identity_complete = bool(identity.ingredient and identity.strengths and identity.form and identity.content)
    causes: list[str] = []
    if not identity.ingredient:
        causes.append("ingredient_unknown")
    if not identity.strengths:
        causes.append("strength_unknown")
    if not identity.form:
        causes.append("form_unknown")
    if not identity.content:
        causes.append("content_unknown")

    sources_out = [
        sources_svc.source_payload(s, now=now)
        for s in (
            await session.execute(
                select(Source).where(
                    Source.organization_id == actor.organization_id,
                    Source.kind == "chain_reference",
                )
            )
        )
        .scalars()
        .all()
    ]
    return {
        "article": {
            "article_id": item.id,
            "display_name": item.display_name,
            "identity": {
                "ingredient": identity.ingredient,
                "strengths": strengths,
                "form": form,
                "route": None,
                "release": release,
                "brand": item.brand,
                "laboratory": item.laboratory,
                "gtin": item.gtin,
                "sanitary_registration": item.sanitary_registration,
            },
            "presentation": {
                "packaging": packaging,
                "content": content_attr,
                "commercial_unit": _packaging(attributes),
                "units_per_commercial_unit": (
                    (content_attr or {}).get("measure", {}).get("value")
                    if content_attr
                    and (content_attr or {}).get("measure", {}).get("unit") == "un"
                    else None
                ),
            },
            "match": {
                "level": "exact" if identity_complete else "needs_review",
                "causes": causes,
            },
            "status": item.status,
            "data_origin": item.data_origin,
        },
        "offers": [
            offer_payload(o, suppliers[o.supplier_id], sources[o.source_id], now=now)
            for o in page
        ],
        "offers_total": len(item_offers),
        "next_cursor": next_cursor,
        # Reservado: en C4 llega vacío y la fuente de cadena se informa `not_configured`.
        "reference_prices": [],
        "annotations": [],
        "notes": [],
        "sources": sources_out,
        "generated_at": now,
    }
