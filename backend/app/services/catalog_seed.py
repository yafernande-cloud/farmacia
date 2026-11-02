"""Carga del catálogo FICTICIO de demostración (`seed-catalog`).

Lee `backend/fixtures/catalog_demo.json` y crea, de forma **idempotente y solo en simulación**:
proveedores ficticios, sus fuentes `fixture`, condiciones de despacho, los artículos canónicos con sus
vínculos SKU (a través de `catalog_load.load_catalog`, la única vía de alta del catálogo, D-101) y las
ofertas fechadas.

Reglas que este servicio NO relaja:

* Solo organización `simulation`: los proveedores, precios y stock son inventados y jamás pueden entrar
  a una organización real (D-037 a D-047).
* Todo lo escrito queda `data_origin='simulated'` y cada oferta lleva `evidence.fixture = true`.
* No se sobrescribe nada: si una fila ya existe con otro contenido se informa como conflicto.
* No inventa atributos fuera del archivo: lo que el fixture no dice queda `NULL`.
* No hace commit: la transacción la abre quien llama (la API usa `transaction()`; la CLI abre la suya).
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.permissions import Role
from app.models import (
    OfferSnapshot,
    ShippingTermsSnapshot,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
)
from app.services import catalog_load
from app.services.actors import Actor
from app.services.events import advisory_lock, emit
from app.services.provenance import OrgKind, org_kind, origin_for

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "catalog_demo.json"
SIMULATION_ONLY = (
    "El catálogo ficticio de demostración solo se puede cargar en la organización de simulación."
)


@dataclass
class SeedReport:
    """Resumen fiel de la carga: creado, ya existente y rechazado."""

    dry_run: bool = False
    suppliers_created: int = 0
    suppliers_unchanged: int = 0
    sources_created: int = 0
    sources_unchanged: int = 0
    shipping_created: int = 0
    shipping_unchanged: int = 0
    offers_created: int = 0
    offers_unchanged: int = 0
    articles_created: int = 0
    articles_unchanged: int = 0
    articles_conflicting: int = 0
    mappings_created: int = 0
    mappings_unchanged: int = 0
    mappings_conflicting: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def fail(self, where: str, code: str, message: str) -> None:
        self.errors.append({"where": where, "code": code, "message": message})


class FixtureError(Exception):
    """El archivo de demostración no cumple la forma esperada."""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise FixtureError(message)


def _text(value: Any, name: str, max_len: int) -> str:
    _require(isinstance(value, str) and value.strip() != "", f'"{name}" debe ser texto no vacío.')
    v = value.strip()
    _require(len(v) <= max_len, f'"{name}" supera {max_len} caracteres.')
    return v


def _decimal(value: Any, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except Exception:  # noqa: BLE001
        raise FixtureError(f'"{name}" debe ser un número decimal.') from None
    _require(d.is_finite() and d >= 0, f'"{name}" debe ser un decimal no negativo.')
    return d


def _observed_at(raw: Any) -> datetime:
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise FixtureError('"observed_at" debe ser una fecha ISO 8601.') from None
    _require(dt.tzinfo is not None, '"observed_at" debe incluir zona horaria (UTC).')
    return dt


def load_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    """Lee y valida la forma del archivo. No toca la base ni escribe nada."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FixtureError(f"No existe el archivo de demostración {path}.") from None
    except json.JSONDecodeError as e:
        raise FixtureError(f"El archivo de demostración no es JSON válido: {e}.") from None
    _require(isinstance(data, dict), "El archivo debe ser un objeto JSON.")
    _require(data.get("fixture") is True, 'El archivo debe declarar "fixture": true.')
    _observed_at(data.get("observed_at"))  # valida la fecha y su zona horaria

    suppliers = data.get("suppliers")
    _require(isinstance(suppliers, list) and suppliers, '"suppliers" debe ser una lista no vacía.')
    supplier_keys = set()
    for s in suppliers:
        key = _text(s.get("key"), "suppliers[].key", 40)
        _require(key not in supplier_keys, f"Proveedor repetido en el archivo: «{key}».")
        supplier_keys.add(key)
        _text(s.get("name"), "suppliers[].name", 200)
        terms = s.get("shipping_terms")
        _require(isinstance(terms, dict), f'El proveedor «{key}» no trae "shipping_terms".')
        _require(
            terms.get("scheme") in {"free", "flat", "free_over_threshold"},
            f'El proveedor «{key}» tiene un esquema de despacho desconocido.',
        )

    sources = data.get("sources")
    _require(isinstance(sources, list) and sources, '"sources" debe ser una lista no vacía.')
    source_keys = set()
    for src in sources:
        key = _text(src.get("key"), "sources[].key", 40)
        _require(key not in source_keys, f"Fuente repetida en el archivo: «{key}».")
        source_keys.add(key)
        _text(src.get("name"), "sources[].name", 200)
        _require(
            src.get("supplier_key") in supplier_keys,
            f'La fuente «{key}» apunta a un proveedor que no está en el archivo.',
        )
        _require(
            src.get("mode") == "fixture",
            f'La fuente «{key}» debe declarar mode = "fixture".',
        )

    articles = data.get("articles")
    _require(isinstance(articles, list) and articles, '"articles" debe ser una lista no vacía.')
    article_keys = set()
    for a in articles:
        key = _text(a.get("identity_key"), "articles[].identity_key", 400)
        _require(key not in article_keys, f"Artículo repetido en el archivo: «{key}».")
        article_keys.add(key)
        _text(a.get("display_name"), "articles[].display_name", 300)
        content = a.get("content")
        _require(isinstance(content, dict), f'El artículo «{key}» no trae "content".')
        _decimal(content.get("value"), f"articles[{key}].content.value")

    offers = data.get("offers")
    _require(isinstance(offers, list) and offers, '"offers" debe ser una lista no vacía.')
    seen_offers = set()
    for o in offers:
        source_key = o.get("source_key")
        _require(source_key in source_keys, "Una oferta apunta a una fuente que no está en el archivo.")
        identity_key = o.get("identity_key")
        _require(
            identity_key in article_keys, "Una oferta apunta a un artículo que no está en el archivo."
        )
        external_id = _text(o.get("external_id"), "offers[].external_id", 120)
        pair = (source_key, external_id)
        _require(pair not in seen_offers, f"Oferta repetida en el archivo: {source_key}/{external_id}.")
        seen_offers.add(pair)
        _decimal(o.get("price"), "offers[].price")
        _require(
            o.get("sale_unit") in {"box", "package", "unit", "bottle"},
            "Una oferta tiene una unidad de venta desconocida.",
        )

    return data


def _article_lines(data: dict[str, Any]) -> list[str]:
    """Registros `article` para `load_catalog`, con los atributos explícitos del fixture."""
    lines = []
    for a in data["articles"]:
        attrs: dict[str, Any] = {
            "schema_version": catalog_load.SCHEMA_VERSION,
            "fixture": True,
            "ingredient": a["ingredient"],
            "unassigned": [],
            "name_anchor": False,
        }
        if a.get("strength"):
            attrs["strengths"] = [
                {
                    "measure": {
                        "value": str(a["strength"]["value"]),
                        "unit": a["strength"]["unit"],
                        "original_text": f'{a["strength"]["value"]} {a["strength"]["unit"]}',
                    },
                    "kind": "amount_per_unit",
                    "basis": "explicit",
                    "role_rule": "unit",
                    "origin": "text",
                }
            ]
        if a.get("form"):
            attrs["form"] = {"value": a["form"], "basis": "explicit", "origin": "text"}
        content = a["content"]
        attrs["content"] = {
            "measure": {
                "value": str(content["value"]),
                "unit": content["unit"],
                "original_text": content.get("text") or f'{content["value"]} {content["unit"]}',
            },
            "basis": "explicit",
            "origin": "text",
        }
        rec = {
            "type": "article",
            "identity_key": a["identity_key"],
            "display_name": a["display_name"],
            "product_kind": a.get("product_kind"),
            "ingredient": a["ingredient"],
            "laboratory": None,
            "brand": None,
            "gtin": None,
            "sanitary_registration": None,
            "attributes": attrs,
        }
        lines.append(json.dumps(rec, ensure_ascii=False))
    return lines


def _mapping_lines(data: dict[str, Any], source_ids: dict[str, uuid.UUID]) -> list[str]:
    lines = []
    for o in data["offers"]:
        rec = {
            "type": "mapping",
            "source_id": str(source_ids[o["source_key"]]),
            "external_id": o["external_id"],
            "item_identity_key": o["identity_key"],
            "external_name": o.get("external_name"),
        }
        lines.append(json.dumps(rec, ensure_ascii=False))
    return lines


async def _get_or_create_supplier(
    session: AsyncSession, actor: Actor, data: dict, report: SeedReport, dry_run: bool
) -> Supplier | None:
    org = actor.organization_id
    existing = (
        await session.execute(
            select(Supplier).where(
                Supplier.organization_id == org, Supplier.name == data["name"]
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        report.suppliers_unchanged += 1
        return existing
    report.suppliers_created += 1
    if dry_run:
        return None
    supplier = Supplier(
        organization_id=org,
        name=data["name"],
        supplier_code=data.get("supplier_code"),
        notes=data.get("notes"),
        status="active",
        data_origin=origin_for(OrgKind.SIMULATION),
        created_by=actor.id,
    )
    session.add(supplier)
    await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="supplier.created",
        entity_type="supplier",
        entity_id=supplier.id,
        event_type="catalog.supplier_created",
        aggregate_version=1,
        payload={"name": supplier.name, "origin": "seed_catalog"},
        after={"name": supplier.name, "supplier_code": supplier.supplier_code},
    )
    return supplier


async def _get_or_create_source(
    session: AsyncSession,
    actor: Actor,
    data: dict,
    supplier: Supplier | None,
    observed_at: datetime,
    report: SeedReport,
    dry_run: bool,
) -> Source | None:
    org = actor.organization_id
    existing = (
        await session.execute(
            select(Source).where(Source.organization_id == org, Source.name == data["name"])
        )
    ).scalar_one_or_none()
    if existing is not None:
        report.sources_unchanged += 1
        # Una fuente con ofertas observadas no está «sin configurar»: si una carga anterior no dejó
        # la marca, se completa con el instante de esta observación (sin tocar versión ni contenido).
        if existing.last_success_at is None and not dry_run:
            existing.last_success_at = observed_at
        return existing
    report.sources_created += 1
    if dry_run:
        return None
    source = Source(
        organization_id=org,
        kind="supplier",
        supplier_id=supplier.id if supplier else None,
        name=data["name"],
        mode="fixture",
        capabilities=list(data["capabilities"]),
        enabled=True,
        last_success_at=observed_at,
        data_origin=origin_for(OrgKind.SIMULATION),
        created_by=actor.id,
    )
    session.add(source)
    await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="source.created",
        entity_type="source",
        entity_id=source.id,
        event_type="catalog.source_created",
        aggregate_version=1,
        payload={"name": source.name, "kind": source.kind, "origin": "seed_catalog"},
        after={"name": source.name, "mode": source.mode},
    )
    return source


async def _get_or_create_shipping(
    session: AsyncSession,
    actor: Actor,
    supplier: Supplier,
    source: Source,
    terms: dict,
    observed_at: datetime,
    report: SeedReport,
    dry_run: bool,
) -> ShippingTermsSnapshot | None:
    org = actor.organization_id
    existing = (
        await session.execute(
            select(ShippingTermsSnapshot).where(
                ShippingTermsSnapshot.organization_id == org,
                ShippingTermsSnapshot.source_id == source.id,
                ShippingTermsSnapshot.observed_at == observed_at,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        report.shipping_unchanged += 1
        return existing
    report.shipping_created += 1
    if dry_run:
        return None
    scheme = terms["scheme"]
    snap = ShippingTermsSnapshot(
        organization_id=org,
        supplier_id=supplier.id,
        source_id=source.id,
        scheme=scheme,
        flat_amount=_decimal(terms["flat_amount"], "flat_amount")
        if terms.get("flat_amount") is not None
        else None,
        threshold_amount=_decimal(terms["threshold_amount"], "threshold_amount")
        if terms.get("threshold_amount") is not None
        else None,
        currency=terms.get("currency") or "CLP",
        observed_at=observed_at,
        evidence={"fixture": True, "origin": "seed_catalog"},
        data_origin=origin_for(OrgKind.SIMULATION),
    )
    session.add(snap)
    await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="shipping_terms.observed",
        entity_type="shipping_terms_snapshot",
        entity_id=snap.id,
        event_type="catalog.shipping_terms_observed",
        aggregate_version=None,
        payload={"supplier_id": str(supplier.id), "scheme": scheme, "origin": "seed_catalog"},
        after={"scheme": scheme, "currency": snap.currency},
    )
    return snap


async def _get_or_create_offer(
    session: AsyncSession,
    actor: Actor,
    offer: dict,
    source: Source,
    trade_item: TradeItem,
    shipping: ShippingTermsSnapshot | None,
    observed_at: datetime,
    report: SeedReport,
    dry_run: bool,
) -> None:
    org = actor.organization_id
    existing = (
        await session.execute(
            select(OfferSnapshot.id).where(
                OfferSnapshot.organization_id == org,
                OfferSnapshot.source_id == source.id,
                OfferSnapshot.external_id == offer["external_id"],
                OfferSnapshot.observed_at == observed_at,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        report.offers_unchanged += 1
        return
    report.offers_created += 1
    if dry_run:
        return
    stock = offer.get("stock")
    minimum = offer.get("minimum")
    multiple = offer.get("multiple")
    snap = OfferSnapshot(
        organization_id=org,
        supplier_id=source.supplier_id,
        source_id=source.id,
        trade_item_id=trade_item.id,
        external_id=offer["external_id"],
        external_id_kind="supplier_sku",
        external_name=offer.get("external_name"),
        price_amount=_decimal(offer["price"], "offers[].price"),
        currency="CLP",
        price_basis="per_commercial_unit",
        tax_included=offer.get("tax_included", True),
        discount_included=offer.get("discount_included", True),
        sale_unit=offer["sale_unit"],
        sale_unit_text=offer.get("sale_unit_text"),
        minimum_known=minimum is not None,
        minimum_quantity=_decimal(minimum, "minimum") if minimum is not None else None,
        minimum_unit=offer["sale_unit"] if minimum is not None else None,
        multiple_known=multiple is not None,
        multiple_quantity=_decimal(multiple, "multiple") if multiple is not None else None,
        multiple_unit=offer["sale_unit"] if multiple is not None else None,
        stock_status="known" if stock else "unknown",
        stock_quantity=_decimal(stock["quantity"], "stock.quantity") if stock else None,
        stock_unit=stock["unit"] if stock else None,
        shipping_terms_id=shipping.id if shipping else None,
        review_status="accepted",
        observed_at=observed_at,
        evidence={"fixture": True, "origin": "seed_catalog"},
        data_origin=origin_for(OrgKind.SIMULATION),
    )
    session.add(snap)
    await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="offer_snapshot.observed",
        entity_type="offer_snapshot",
        entity_id=snap.id,
        event_type="catalog.offer_observed",
        aggregate_version=None,
        payload={
            "source_id": str(source.id),
            "external_id": snap.external_id,
            "trade_item_id": str(trade_item.id),
            "origin": "seed_catalog",
        },
        after={"price_amount": str(snap.price_amount), "currency": snap.currency},
    )


async def _trade_items(session: AsyncSession, org: uuid.UUID, keys: set[str]) -> dict[str, TradeItem]:
    rows = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.organization_id == org, TradeItem.identity_key.in_(sorted(keys))
            )
        )
    ).scalars()
    return {row.identity_key: row for row in rows}


async def seed_catalog(
    session: AsyncSession,
    actor: Actor,
    *,
    fixture_path: Path = FIXTURE_PATH,
    observed_at: datetime | None = None,
    dry_run: bool = False,
) -> SeedReport:
    """Carga el catálogo ficticio en la organización de simulación. No hace commit.

    `observed_at` permite fechar la observación en el momento de la demo (debe traer zona horaria y
    no puede estar en el futuro). Sin él se usa el instante del archivo, que mantiene la carga
    reproducible y las pruebas estables. La idempotencia es por ese instante: repetir la carga con la
    misma fecha no duplica nada; una fecha nueva registra una observación nueva, como corresponde.
    """
    if Role.ADMIN.value not in (actor.roles or []):
        raise DomainError(
            "FORBIDDEN", "Solo la administración puede cargar el catálogo.", status_code=403
        )
    kind = await org_kind(session, actor.organization_id)
    if kind != OrgKind.SIMULATION:
        raise DomainError("FIXTURE_NOT_ALLOWED", SIMULATION_ONLY, status_code=422)

    data = load_fixture(fixture_path)
    if observed_at is None:
        observed_at = _observed_at(data["observed_at"])
    elif observed_at.tzinfo is None:
        raise DomainError(
            "INVALID_OBSERVED_AT", "La fecha de observación debe incluir zona horaria.", status_code=422
        )
    elif observed_at > datetime.now(UTC):
        raise DomainError(
            "INVALID_OBSERVED_AT",
            "La fecha de observación no puede estar en el futuro.",
            status_code=422,
        )
    report = SeedReport(dry_run=dry_run)
    await advisory_lock(session, f"seed-catalog:{actor.organization_id}")

    suppliers: dict[str, Supplier] = {}
    for supplier_data in data["suppliers"]:
        supplier = await _get_or_create_supplier(session, actor, supplier_data, report, dry_run)
        if supplier is not None:
            suppliers[supplier_data["key"]] = supplier

    sources: dict[str, Source] = {}
    for source_data in data["sources"]:
        supplier = suppliers.get(source_data["supplier_key"])
        source = await _get_or_create_source(
            session, actor, source_data, supplier, observed_at, report, dry_run
        )
        if source is not None:
            sources[source_data["key"]] = source

    if dry_run:
        org = actor.organization_id
        existing_keys = set(
            (
                await session.execute(
                    select(TradeItem.identity_key).where(TradeItem.organization_id == org)
                )
            ).scalars()
        )
        wanted = {a["identity_key"] for a in data["articles"]}
        report.articles_created = len(wanted - existing_keys)
        report.articles_unchanged = len(wanted & existing_keys)
        existing_pairs = set(
            (
                await session.execute(
                    select(SupplierProductMapping.source_id, SupplierProductMapping.external_id)
                    .join(Source, Source.id == SupplierProductMapping.source_id)
                    .where(Source.name.in_([s["name"] for s in data["sources"]]))
                )
            ).all()
        )
        report.mappings_created = sum(
            1
            for o in data["offers"]
            if (src := sources.get(o["source_key"])) is None
            or (src.id, o["external_id"]) not in existing_pairs
        )
        report.mappings_unchanged = len(data["offers"]) - report.mappings_created
        # Despacho y ofertas: se cuentan por nombre de proveedor/fuente y el instante del fixture,
        # sin depender de ids que en la vista previa todavía no existen.
        existing_shipping = set(
            (
                await session.execute(
                    select(Supplier.name).join(
                        ShippingTermsSnapshot,
                        ShippingTermsSnapshot.supplier_id == Supplier.id,
                    ).where(
                        Supplier.organization_id == org,
                        ShippingTermsSnapshot.observed_at == observed_at,
                    )
                )
            ).scalars()
        )
        wanted_suppliers = {s["name"] for s in data["suppliers"]}
        report.shipping_created = len(wanted_suppliers - existing_shipping)
        report.shipping_unchanged = len(wanted_suppliers & existing_shipping)
        existing_offers = set(
            (
                await session.execute(
                    select(Source.name, OfferSnapshot.external_id)
                    .join(OfferSnapshot, OfferSnapshot.source_id == Source.id)
                    .where(
                        Source.organization_id == org,
                        OfferSnapshot.observed_at == observed_at,
                    )
                )
            ).all()
        )
        wanted_offers = {(o["source_key"], o["external_id"]) for o in data["offers"]}
        key_by_name = {s["name"]: s["key"] for s in data["sources"]}
        present = {
            (key_by_name[name], eid) for name, eid in existing_offers if name in key_by_name
        }
        report.offers_created = len(wanted_offers - present)
        report.offers_unchanged = len(wanted_offers & present)
        return report

    lines = _article_lines(data) + _mapping_lines(data, {k: v.id for k, v in sources.items()})
    load_report = await catalog_load.load_catalog(session, actor, lines)
    report.articles_created = load_report.articles_created
    report.articles_unchanged = load_report.articles_unchanged
    report.articles_conflicting = load_report.articles_conflicting
    report.mappings_created = load_report.mappings_created
    report.mappings_unchanged = load_report.mappings_unchanged
    report.mappings_conflicting = load_report.mappings_conflicting
    for e in load_report.errors:
        report.fail(f"línea {e['line']}", e["code"], e["message"])

    source_supplier = {s["key"]: s["supplier_key"] for s in data["sources"]}
    supplier_source = {supplier_key: sources[key] for key, supplier_key in source_supplier.items()}
    shipping: dict[str, ShippingTermsSnapshot] = {}
    for supplier_data in data["suppliers"]:
        supplier = suppliers.get(supplier_data["key"])
        source = supplier_source.get(supplier_data["key"])
        if supplier is None or source is None:
            continue
        snap = await _get_or_create_shipping(
            session,
            actor,
            supplier,
            source,
            supplier_data["shipping_terms"],
            observed_at,
            report,
            dry_run,
        )
        if snap is not None:
            shipping[supplier_data["key"]] = snap

    items = await _trade_items(
        session, actor.organization_id, {a["identity_key"] for a in data["articles"]}
    )
    for offer in data["offers"]:
        source = sources[offer["source_key"]]
        item = items.get(offer["identity_key"])
        if item is None:
            report.fail(
                f"oferta {offer['source_key']}/{offer['external_id']}",
                "UNKNOWN_ARTICLE",
                "El artículo no quedó registrado; la oferta se omite.",
            )
            continue
        await _get_or_create_offer(
            session,
            actor,
            offer,
            source,
            item,
            shipping.get(source_supplier[offer["source_key"]]),
            observed_at,
            report,
            dry_run,
        )
    return report
