"""Servicio del motor declarativo de proveedores.

Separa dos planos:

* **configuración** (`upsert_profile`): una persona autorizada guarda un perfil validado y versionado;
* **ejecución** (`search_live`): una consulta usa la estrategia que corresponde al mecanismo y
  devuelve ofertas normalizadas. La búsqueda **no persiste ofertas**; solo actualiza el estado
  operativo mínimo del perfil (última verificación, último error y huella de estructura).

El estado visible se deriva; nunca se escribe como una columna. Un error transitorio no borra la
configuración aprobada y una fuente sin perfil no se presenta como «sin resultados».
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import DomainError, not_found
from app.core.permissions import require_admin
from app.models import (
    Source,
    Supplier,
    SupplierConnectorProfile,
    SupplierProductMapping,
    TradeItem,
)
from app.services.actors import Actor
from app.services.connectors import presets, registry
from app.services.connectors.base import (
    ConnectorError,
    ConnectorIntervention,
    ConnectorNotConfigured,
    ConnectorResult,
    PublishedProduct,
    SupplierOffer,
)
from app.services.connectors.commercial import cart_blockers, published_blockers
from app.services.connectors.config import validate_profile
from app.services.connectors.matching import classify_match
from app.services.connectors.store import store
from app.services.events import advisory_lock, emit
from app.services.offer_equivalence import resolve_equivalent_item_ids
from app.services.offer_identities import offer_identity_key
from app.services.preferences import (
    OfferView,
    load_article_preferences,
    load_item_preferences,
    rank_provider_offers,
)

logger = logging.getLogger(__name__)

MAX_QUERY = 120
MIN_QUERY = 2

# Identificador efímero y estable por resultado vivo (`fuente:SKU`). No identifica un artículo ni
# se guarda: solo permite asociar la clave de orden a la oferta durante esta lectura.
_LIVE_OFFER_NAMESPACE = uuid.UUID("6f1d3f9a-57a2-4b0c-9f0e-2c9f3a1b7d55")


# ------------------------------------------------------------------ estado derivado


def _now() -> datetime:
    return datetime.now(UTC)


def _short(value: str | None, limit: int = 300) -> str | None:
    if value is None:
        return None
    text = "".join(ch for ch in value if ord(ch) >= 32).strip()
    if not text:
        return None
    return text[:limit]


def derive_state(
    profile: SupplierConnectorProfile | None,
    source: Source,
    supplier: Supplier,
    *,
    engine_enabled: bool,
) -> tuple[str, str | None]:
    """Devuelve `(estado, motivo)`. No hay estado «conectado» sin una respuesta válida previa."""
    if not engine_enabled:
        return "not_configured", "engine_disabled"
    if not source.enabled:
        return "not_configured", "source_disabled"
    if source.kind != "supplier":
        return "not_configured", "source_not_supplier"
    if source.mode != "api":
        return "not_configured", "source_mode_not_api"
    if supplier.status != "active":
        return "not_configured", "supplier_inactive"
    if profile is None:
        return "not_configured", "profile_missing"
    if not profile.enabled:
        return "not_configured", "profile_disabled"
    if profile.last_error_kind == "configuration":
        return "not_configured", profile.last_error
    if profile.last_error_kind == "intervention":
        return "requires_intervention", profile.last_error
    if profile.last_error_kind == "transient":
        return "error", profile.last_error
    if profile.last_success_at is not None:
        return "connected", None
    return "not_configured", "not_tested"


def _precondition(
    profile: SupplierConnectorProfile | None,
    source: Source,
    supplier: Supplier,
    *,
    engine_enabled: bool,
) -> str | None:
    """Motivo que impide consultar. `not_tested` no impide: el perfil recién creado debe probarse."""
    if not engine_enabled:
        return "engine_disabled"
    if not source.enabled:
        return "source_disabled"
    if source.kind != "supplier":
        return "source_not_supplier"
    if source.mode != "api":
        return "source_mode_not_api"
    if supplier.status != "active":
        return "supplier_inactive"
    if profile is None:
        return "profile_missing"
    if not profile.enabled:
        return "profile_disabled"
    return None


# ------------------------------------------------------------------ lectura


async def _load_source_supplier(
    session: AsyncSession,
    actor: Actor,
    source_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> tuple[Source, Supplier]:
    q = (
        select(Source, Supplier)
        .join(
            Supplier,
            (Supplier.id == Source.supplier_id)
            & (Supplier.organization_id == Source.organization_id),
        )
        .where(Source.id == source_id, Source.organization_id == actor.organization_id)
    )
    if for_update:
        q = q.with_for_update(of=Source)
    row = (await session.execute(q)).first()
    if row is None:
        raise not_found("La fuente")
    return row[0], row[1]


async def _load_profile(
    session: AsyncSession,
    org: uuid.UUID,
    source_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> SupplierConnectorProfile | None:
    q = select(SupplierConnectorProfile).where(
        SupplierConnectorProfile.organization_id == org,
        SupplierConnectorProfile.source_id == source_id,
    )
    if for_update:
        q = q.with_for_update()
    return (await session.execute(q)).scalar_one_or_none()


def profile_payload(
    profile: SupplierConnectorProfile | None,
    source: Source,
    supplier: Supplier,
    *,
    engine_enabled: bool,
) -> dict[str, Any]:
    state, reason = derive_state(
        profile, source, supplier, engine_enabled=engine_enabled
    )
    base: dict[str, Any] = {
        "source_id": source.id,
        "supplier_id": source.supplier_id,
        "source_name": source.name,
        "supplier_name": supplier.name,
        "configured": profile is not None,
        "state": state,
        "reason": reason,
        "version": profile.version if profile is not None else 0,
        "mechanism": profile.mechanism if profile is not None else None,
        "enabled": profile.enabled if profile is not None else False,
        "config": profile.config if profile is not None else None,
        "field_map": profile.field_map if profile is not None else None,
        "schema_fingerprint": profile.schema_fingerprint if profile is not None else None,
        "last_success_at": profile.last_success_at if profile is not None else None,
        "last_checked_at": profile.last_checked_at if profile is not None else None,
        "last_error": profile.last_error if profile is not None else None,
        "last_error_kind": profile.last_error_kind if profile is not None else None,
        "created_at": profile.created_at if profile is not None else None,
        "updated_at": profile.updated_at if profile is not None else None,
    }
    return base


async def get_profile(
    session: AsyncSession, actor: Actor, source_id: uuid.UUID
) -> dict[str, Any]:
    require_admin(actor.roles)
    source, supplier = await _load_source_supplier(session, actor, source_id)
    profile = await _load_profile(session, actor.organization_id, source_id)
    return profile_payload(
        profile,
        source,
        supplier,
        engine_enabled=get_settings().supplier_connector_enabled,
    )


# ------------------------------------------------------------------ configuración


def _audit_snapshot(profile: SupplierConnectorProfile) -> dict[str, Any]:
    return {
        "mechanism": profile.mechanism,
        "enabled": profile.enabled,
        "version": profile.version,
        "config": profile.config,
        "field_map": profile.field_map,
    }


async def upsert_profile(
    session: AsyncSession,
    actor: Actor,
    source_id: uuid.UUID,
    *,
    preset: str | None,
    mechanism: str | None,
    config: dict[str, Any] | None,
    field_map: dict[str, Any] | None,
    enabled: bool,
    expected_version: int | None,
) -> SupplierConnectorProfile:
    require_admin(actor.roles)
    source, _supplier = await _load_source_supplier(session, actor, source_id, for_update=True)
    if source.kind != "supplier":
        raise DomainError(
            "SOURCE_NOT_SUPPLIER",
            "Solo una fuente de proveedor puede tener un perfil de conexión.",
            status_code=422,
        )
    if source.mode != "api":
        raise DomainError(
            "SOURCE_MODE_NOT_API",
            "La fuente debe estar en modo «api» antes de configurar un conector. "
            "El modo no se cambia aquí.",
            status_code=422,
        )

    if preset is not None:
        try:
            base = presets.preset(preset)
        except KeyError:
            raise DomainError(
                "CONNECTOR_PRESET_UNKNOWN",
                f"El preset «{preset}» no existe. Disponibles: {', '.join(presets.PRESETS)}.",
                status_code=422,
            ) from None
        if config or field_map or mechanism is not None:
            raise DomainError(
                "PRESET_WITH_OVERRIDES",
                "Un preset no se mezcla con mechanism, config ni field_map. "
                "Guarda el preset y luego edítalo.",
                status_code=422,
            )
        mechanism, config, field_map = (
            base["mechanism"],
            base["config"],
            base["field_map"],
        )

    if mechanism is None or config is None or field_map is None:
        raise DomainError(
            "CONNECTOR_PROFILE_INCOMPLETE",
            "Indica un preset o entrega mechanism, config y field_map.",
            status_code=422,
        )
    mechanism, clean_config, clean_map = validate_profile(mechanism, config, field_map)

    await advisory_lock(session, f"connector-profile:{actor.organization_id}:{source.id}")
    existing = await _load_profile(
        session, actor.organization_id, source.id, for_update=True
    )
    current_version = existing.version if existing is not None else 0
    if existing is not None and expected_version is None:
        raise DomainError(
            "VERSION_REQUIRED",
            "Falta If-Match con la versión actual del perfil de conexión.",
            status_code=428,
        )
    if expected_version is not None and expected_version != current_version:
        raise DomainError(
            "VERSION_CONFLICT",
            "El perfil de conexión cambió. Revisa la versión actual antes de guardar.",
            status_code=409,
            details={"current_version": current_version},
        )

    now = _now()
    before: dict[str, Any] | None = None
    if existing is None:
        profile = SupplierConnectorProfile(
            organization_id=actor.organization_id,
            supplier_id=source.supplier_id,
            source_id=source.id,
            mechanism=mechanism,
            config=clean_config,
            field_map=clean_map,
            enabled=enabled,
            version=1,
            data_origin="unclassified",
            created_by=actor.id,
        )
        session.add(profile)
        await session.flush()
        action = "supplier_connector_profile.created"
    else:
        profile = existing
        before = _audit_snapshot(profile)
        config_changed = (
            profile.mechanism != mechanism
            or profile.config != clean_config
            or profile.field_map != clean_map
        )
        profile.mechanism = mechanism
        profile.config = clean_config
        profile.field_map = clean_map
        profile.enabled = enabled
        profile.version += 1
        profile.updated_at = now
        if config_changed:
            # La configuración anterior dejó de ser la vigente: no se conserva como si hubiera
            # sido probada con esta forma.
            profile.last_success_at = None
            profile.last_checked_at = None
            profile.last_error = None
            profile.last_error_kind = None
            profile.schema_fingerprint = None
        await session.flush()
        action = "supplier_connector_profile.updated"

    emit(
        session,
        org_id=actor.organization_id,
        actor_id=actor.id,
        action=action,
        entity_type="supplier_connector_profile",
        entity_id=profile.id,
        event_type="catalog.supplier_connector_profile_saved",
        aggregate_version=profile.version,
        payload={
            "source_id": str(source.id),
            "supplier_id": str(source.supplier_id),
            "mechanism": profile.mechanism,
            "enabled": profile.enabled,
        },
        before=before,
        after=_audit_snapshot(profile),
    )
    return profile


# ------------------------------------------------------------------ búsqueda en vivo


def _clean_query(query: str) -> str:
    text = " ".join((query or "").split())
    text = "".join(ch for ch in text if ord(ch) >= 32)
    if not (MIN_QUERY <= len(text) <= MAX_QUERY):
        raise DomainError(
            "INVALID_QUERY",
            f"La consulta debe tener entre {MIN_QUERY} y {MAX_QUERY} caracteres.",
            status_code=422,
        )
    return text


def _published_payload(product: PublishedProduct, *, query: str) -> dict[str, Any]:
    """Publicación sin precio comercial: se muestra, nunca es una oferta ni entra a un carrito."""
    relation, reasons = classify_match(query, product.name)
    return {
        "external_id": product.external_id,
        # Se completa al leer con un vínculo exacto; el motor no conserva ofertas.
        "trade_item_id": None,
        "name": product.name,
        "laboratory": product.laboratory,
        "presentation": product.presentation,
        "stock_status": product.stock_status,
        # Precio de lista del catálogo, solo referencial; `None` si no se informó.
        "reference_price": (
            {"amount": str(product.reference_price), "currency": product.currency}
            if product.reference_price is not None
            else None
        ),
        "image_url": product.image_url,
        "product_url": product.product_url,
        "ean": product.ean,
        "match_relation": relation,
        "match_reasons": reasons,
        # Sin precio de venta no hay nada que cotizar: la elegibilidad es siempre falsa.
        "cart_eligible": False,
        "cart_blockers": published_blockers(product, match_relation=relation),
    }


def _offer_payload(offer: SupplierOffer, *, query: str) -> dict[str, Any]:
    relation, reasons = classify_match(query, offer.name)
    blockers = cart_blockers(offer, match_relation=relation)
    return {
        "external_id": offer.external_id,
        # Se completa al leer con un vínculo exacto; el motor no conserva ofertas.
        "trade_item_id": None,
        "name": offer.name,
        "laboratory": offer.laboratory,
        "presentation": offer.presentation,
        "price": {"amount": str(offer.price), "currency": offer.currency},
        "price_basis": offer.price_basis,
        "sale_unit": offer.sale_unit,
        "stock_status": offer.stock_status,
        # Sin unidad confirmada no se publica una cantidad que no se puede interpretar.
        "stock_quantity": (
            str(offer.stock_quantity) if offer.stock_quantity is not None else None
        ),
        "stock_unit": offer.stock_unit,
        "image_url": offer.image_url,
        "product_url": offer.product_url,
        "ean": offer.ean,
        # Clasificación textual: no es equivalencia farmacéutica ni un puntaje.
        "match_relation": relation,
        "match_reasons": reasons,
        # La búsqueda no incorpora al carrito: solo informa si los datos comerciales alcanzan.
        "cart_eligible": not blockers,
        "cart_blockers": blockers,
        # Se completa al leer; nunca se persiste con la oferta.
        "order_key": [],
    }


async def _resolve_trade_item_ids(
    session: AsyncSession, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Marca en cada resultado el artículo interno vinculado, o `None`.

    Solo hay fuentes exactas: el vínculo explícito de la fuente con ese SKU, un EAN idéntico que
    apunte a un único artículo activo de la organización y, por último, la identidad técnica
    `offer:<fuente>:<SKU>` cuando esa oferta ya fue enseñada sin vínculo. Un EAN repetido es
    ambiguo y no se resuelve; nunca se comparan nombres ni se inventa un identificador.
    """
    entries = [*snapshot.get("offers", []), *snapshot.get("published", [])]
    if not entries:
        return snapshot
    org = snapshot["organization_id"]
    source_id = snapshot["source_id"]
    external_ids = {entry["external_id"] for entry in entries}

    mapped = dict(
        (
            await session.execute(
                select(
                    SupplierProductMapping.external_id,
                    SupplierProductMapping.trade_item_id,
                ).where(
                    SupplierProductMapping.organization_id == org,
                    SupplierProductMapping.source_id == source_id,
                    SupplierProductMapping.external_id.in_(external_ids),
                )
            )
        ).all()
    )

    eans = {
        entry["ean"]
        for entry in entries
        if entry.get("ean") and entry["external_id"] not in mapped
    }
    by_ean: dict[str, uuid.UUID | None] = {}
    if eans:
        rows = (
            await session.execute(
                select(TradeItem.gtin, TradeItem.id).where(
                    TradeItem.organization_id == org,
                    TradeItem.status == "active",
                    TradeItem.gtin.in_(eans),
                )
            )
        ).all()
        grouped: dict[str, set[uuid.UUID]] = {}
        for gtin, item_id in rows:
            grouped.setdefault(gtin, set()).add(item_id)
        # Solo un artículo distinto por EAN; dos o más quedan sin resolver.
        by_ean = {
            ean: (next(iter(ids)) if len(ids) == 1 else None) for ean, ids in grouped.items()
        }

    # Tercera fuente exacta: la identidad técnica de un SKU ya enseñado. Es `inactive` y nunca
    # aparece como artículo del catálogo; solo permite aplicar a esa oferta su preferencia.
    pending = [
        entry
        for entry in entries
        if entry["external_id"] not in mapped
        and not (entry.get("ean") and by_ean.get(entry["ean"]))
    ]
    by_identity_key: dict[str, uuid.UUID] = {}
    if pending:
        by_identity_key = dict(
            (
                await session.execute(
                    select(TradeItem.identity_key, TradeItem.id).where(
                        TradeItem.organization_id == org,
                        TradeItem.identity_key.in_(
                            [
                                offer_identity_key(source_id, entry["external_id"])
                                for entry in pending
                            ]
                        ),
                    )
                )
            ).all()
        )

    for entry in entries:
        resolved = mapped.get(entry["external_id"])
        if resolved is None and entry.get("ean"):
            resolved = by_ean.get(entry["ean"])
        if resolved is None:
            resolved = by_identity_key.get(offer_identity_key(source_id, entry["external_id"]))
        entry["trade_item_id"] = resolved
    return snapshot


async def _apply_order_keys(
    session: AsyncSession, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Calcula la clave de orden de las ofertas **vivas** de esta fuente.

    Se hace en cada lectura, después de resolver la identidad y sin guardar nada: una preferencia
    recién enseñada se refleja al refrescar sin crear `OfferSnapshot` ni persistir la oferta. Las
    ofertas sin vínculo quedan con clave neutra y siguen visibles. La marca no viaja en el
    resultado vivo, así que las preferencias de marca no se aplican aquí (no se inventa).

    Una oferta sin vínculo exacto puede **compartir la preferencia** de un artículo cuando su
    identidad farmacéutica es la misma (medicamento, concentración, forma y presentación). Esa
    equivalencia solo alimenta el orden: `trade_item_id` sigue siendo el vínculo exacto y no se
    crea ningún `SupplierProductMapping`.
    """
    entries = snapshot.get("offers", [])
    if not entries:
        return snapshot
    equivalents = await resolve_equivalent_item_ids(
        session, snapshot["organization_id"], entries
    )
    views: list[OfferView] = []
    for entry in entries:
        equivalent_id = equivalents.get(entry["external_id"])
        # El vínculo exacto manda; la equivalencia solo aporta la preferencia que ordena.
        entry["equivalent_trade_item_id"] = equivalent_id
        preference_id = entry.get("trade_item_id") or equivalent_id
        price = entry.get("price") or {}
        try:
            amount = Decimal(str(price.get("amount")))
        except (InvalidOperation, TypeError):
            continue  # sin precio válido no es una oferta comparable; se deja sin clave
        quantity = entry.get("stock_quantity")
        views.append(
            OfferView(
                offer_id=uuid.uuid5(
                    _LIVE_OFFER_NAMESPACE,
                    f"{snapshot['source_id']}:{entry['external_id']}",
                ),
                supplier_id=None,
                laboratory=entry.get("laboratory"),
                brand=None,
                currency=price.get("currency") or "",
                price_amount=amount,
                price_basis=entry.get("price_basis"),
                pack_units_per_commercial_unit=None,
                stock_status=entry.get("stock_status") or "unknown",
                stock_quantity=Decimal(str(quantity)) if quantity is not None else None,
                stock_unit=entry.get("stock_unit"),
                sale_unit=entry.get("sale_unit"),
                trade_item_id=preference_id,
            )
        )
    if not views:
        return snapshot
    item_ids = {view.trade_item_id for view in views if view.trade_item_id is not None}
    item_prefs = await load_item_preferences(
        session, snapshot["organization_id"], item_ids
    )
    article_prefs = await load_article_preferences(
        session, snapshot["organization_id"], item_ids
    )
    ranked = rank_provider_offers(article_prefs, item_prefs, views)
    keyed = {view.offer_id: list(ranked[view.offer_id]) for view in views}
    for entry in entries:
        offer_id = uuid.uuid5(
            _LIVE_OFFER_NAMESPACE, f"{snapshot['source_id']}:{entry['external_id']}"
        )
        if offer_id in keyed:
            entry["order_key"] = keyed[offer_id]
    return snapshot


def _snapshot(
    *,
    source: Source,
    query: str,
    state: str,
    reason: str | None,
    offers: list[SupplierOffer] | None = None,
    observed_at: datetime | None = None,
    discarded: int = 0,
    published: list[PublishedProduct] | None = None,
    warnings: list[str] | None = None,
    profile: SupplierConnectorProfile | None,
) -> dict[str, Any]:
    settings = get_settings()
    now = _now()
    offers = offers or []
    published = published or []
    return {
        "search_id": uuid.uuid4(),
        # Uso interno para comprobar la organización; no forma parte de la respuesta HTTP.
        "organization_id": source.organization_id,
        "source_id": source.id,
        "source_name": source.name,
        "query": query,
        "state": state,
        "reason": reason,
        # Una publicación sin precio también es una coincidencia del catálogo, aunque no se pueda
        # cotizar: el mensaje de «sin resultados» solo corresponde cuando no hay nada que mostrar.
        "outcome": (
            ("matches" if (offers or published) else "no_matches")
            if state in {"connected", "requires_intervention"}
            else None
        ),
        "observed_at": observed_at,
        "created_at": now,
        "expires_at": now + timedelta(seconds=settings.supplier_connector_cache_seconds),
        "cached": False,
        "profile_version": profile.version if profile is not None else None,
        "discarded": discarded,
        "warnings": warnings or [],
        "offers": [_offer_payload(offer, query=query) for offer in offers],
        "published": [_published_payload(product, query=query) for product in published],
    }


def _cache_key(
    source: Source,
    query: str,
    limit: int,
    profile: SupplierConnectorProfile | None,
) -> str:
    return (
        f"{source.organization_id}:{source.id}:{profile.version if profile else 0}:"
        f"{limit}:{query.casefold()}"
    )


async def search_live(
    session: AsyncSession,
    actor: Actor,
    source_id: uuid.UUID,
    *,
    query: str,
    limit: int,
) -> dict[str, Any]:
    require_admin(actor.roles)
    settings = get_settings()
    if not settings.supplier_connector_enabled:
        raise DomainError(
            "CONNECTOR_ENGINE_DISABLED",
            "El motor de proveedores está deshabilitado en este despliegue.",
            status_code=503,
        )
    clean_query = _clean_query(query)
    limit = max(1, min(limit, settings.supplier_connector_max_results))
    # El almacén global es único por proceso; sus cotas siguen la configuración vigente.
    store.ttl_seconds = settings.supplier_connector_cache_seconds
    store.max_live = settings.supplier_connector_max_live

    source, supplier = await _load_source_supplier(session, actor, source_id)
    profile = await _load_profile(session, actor.organization_id, source_id)
    key = _cache_key(source, clean_query, limit, profile)
    block = _precondition(
        profile, source, supplier, engine_enabled=settings.supplier_connector_enabled
    )
    if block is None:
        cached = await store.cached(key)
        if cached is not None and cached.get("organization_id") == actor.organization_id:
            # El vínculo puede haberse creado después de guardar la caché: se resuelve al leer.
            return await _apply_order_keys(
                session, await _resolve_trade_item_ids(session, cached)
            )

    if block is not None:
        snapshot = _snapshot(
            source=source,
            query=clean_query,
            state="not_configured",
            reason=block,
            profile=profile,
        )
        await store.put(snapshot)
        return snapshot

    assert profile is not None  # lo garantiza `_precondition`
    try:
        strategy = registry.get_strategy(profile.mechanism)
        result: ConnectorResult = await strategy.search(
            clean_query,
            config=profile.config,
            field_map=profile.field_map,
            limit=limit,
        )
    except ConnectorNotConfigured as exc:
        _record_configuration(profile, exc.reason)
        snapshot = _snapshot(
            source=source,
            query=clean_query,
            state="not_configured",
            reason=exc.reason,
            profile=profile,
        )
    except ConnectorError as exc:
        _record_error(profile, kind="transient", reason=exc.reason)
        snapshot = _snapshot(
            source=source,
            query=clean_query,
            state="error",
            reason=exc.reason,
            profile=profile,
        )
    except ConnectorIntervention as exc:
        _record_error(profile, kind="intervention", reason=exc.reason)
        snapshot = _snapshot(
            source=source,
            query=clean_query,
            state="requires_intervention",
            reason=exc.reason,
            profile=profile,
        )
    except Exception:
        logger.exception("Motor de proveedores: fallo inesperado en la fuente %s", source.id)
        _record_error(profile, kind="transient", reason="internal_error")
        snapshot = _snapshot(
            source=source,
            query=clean_query,
            state="error",
            reason="internal_error",
            profile=profile,
        )
    else:
        previous = profile.schema_fingerprint
        drift = (
            previous is not None
            and result.fingerprint is not None
            and previous != result.fingerprint
        )
        _record_success(profile, result.fingerprint)
        warnings = list(result.warnings)
        if drift:
            # La estructura cambió. Se devuelven las ofertas que sí validaron, pero el estado pide
            # revisar el mapeo antes de confiar en el portal.
            _record_error(profile, kind="intervention", reason="schema_changed")
            warnings.append(
                "La estructura de la respuesta cambió; revisa el mapeo antes de confiar en estos datos."
            )
            state, reason = "requires_intervention", "schema_changed"
        else:
            state, reason = "connected", None
        snapshot = _snapshot(
            source=source,
            query=clean_query,
            state=state,
            reason=reason,
            offers=result.offers,
            observed_at=result.observed_at,
            discarded=result.discarded,
            published=result.published,
            warnings=warnings,
            profile=profile,
        )

    snapshot = await _resolve_trade_item_ids(session, snapshot)
    snapshot = await _apply_order_keys(session, snapshot)
    await session.flush()
    if snapshot["state"] == "connected":
        await store.put(snapshot, cache_key=key)
    else:
        # Un resultado fallido no puede convivir con un acierto anterior en la caché.
        await store.invalidate(key)
        await store.put(snapshot)
    return snapshot


def _record_configuration(profile: SupplierConnectorProfile, reason: str) -> None:
    now = _now()
    profile.last_checked_at = now
    profile.last_error = _short(reason)
    profile.last_error_kind = "configuration"
    profile.updated_at = now


def _record_error(
    profile: SupplierConnectorProfile,
    *,
    kind: str,
    reason: str,
) -> None:
    now = _now()
    profile.last_checked_at = now
    profile.last_error = _short(reason)
    profile.last_error_kind = kind
    # `last_success_at` se conserva: un fallo de hoy no borra que ayer respondió.
    profile.updated_at = now


def _record_success(profile: SupplierConnectorProfile, fingerprint: str | None) -> None:
    now = _now()
    profile.last_success_at = now
    profile.last_checked_at = now
    profile.last_error = None
    profile.last_error_kind = None
    if fingerprint is not None:
        profile.schema_fingerprint = fingerprint
    profile.updated_at = now


async def get_search(
    session: AsyncSession, actor: Actor, search_id: uuid.UUID
) -> dict[str, Any]:
    require_admin(actor.roles)
    snapshot = await store.get(str(search_id))
    if snapshot is None or snapshot.get("organization_id") != actor.organization_id:
        raise not_found("La búsqueda")
    # Se resuelve también aquí para que un vínculo recién creado aparezca sin repetir la búsqueda.
    return await _apply_order_keys(session, await _resolve_trade_item_ids(session, snapshot))
