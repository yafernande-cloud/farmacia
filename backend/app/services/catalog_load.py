"""Catálogo propio: carga inicial desde un archivo de datos (D-101).

`load_catalog` es la ÚNICA vía de alta del catálogo en este momento y es un mecanismo **técnico**
(`catalog-load` en la CLI), no la administración cotidiana del catálogo (D-097). Lee líneas JSONL con
dos tipos de registro, tal como quedó congelado en el contrato de entrada A:

    {"type": "article", "identity_key": "...", "display_name": "...", "product_kind": null,
     "ingredient": null, "laboratory": null, "brand": null, "gtin": null,
     "sanitary_registration": null, "attributes": {}}
    {"type": "mapping", "source_id": "uuid", "external_id": "...", "item_identity_key": "...",
     "external_name": null}

Reglas que el servicio NO relaja:

* La `identity_key` la escribe una persona; nunca se deriva del nombre. Una fila sin clave se rechaza.
* No se infiere ningún atributo ni ningún vínculo: lo que no dice el archivo queda `NULL`.
* La idempotencia es por `trade_item.identity_key` y por `(source_id, external_id)`. Un registro que
  ya existe con **otro** contenido no se sobrescribe: se informa como conflicto.
* No crea ofertas, no lee planillas y no toca `chain_product` ni `reference_price` (eso es E8/D-106).
* Solo escribe `trade_item` y `supplier_product_mapping`; el vínculo nace `origin='explicit'`, con
  `created_by` del operador, y nunca `confirmed` (confirmar es de una persona autorizada, no de la carga).
* La procedencia (`data_origin`) la fija la organización: simulación ⇒ `simulated`; real ⇒ `captured`.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.permissions import Role
from app.models import Source, SupplierProductMapping, TradeItem
from app.services.actors import Actor
from app.services.events import advisory_lock, emit
from app.services.provenance import org_kind, origin_for

SCHEMA_VERSION = 2
RECORD_TYPES = ("article", "mapping")
# El vocabulario cerrado de `trade_item.product_kind`; `None` = «no clasificado» y es válido (D-094).
PRODUCT_KINDS = frozenset(
    {
        "medicine",
        "medical_device",
        "cosmetic",
        "personal_care",
        "veterinary",
        "food_supplement",
        "other",
    }
)
ARTICLE_FIELDS = {
    "identity_key",
    "display_name",
    "product_kind",
    "ingredient",
    "laboratory",
    "brand",
    "gtin",
    "sanitary_registration",
    "attributes",
}
MAPPING_FIELDS = {"source_id", "external_id", "item_identity_key", "external_name"}


@dataclass
class LoadReport:
    """Resumen fiel de la carga: lo creado, lo que ya estaba y lo que se rechazó, con su línea."""

    dry_run: bool = False
    articles_created: int = 0
    articles_unchanged: int = 0
    articles_conflicting: int = 0
    mappings_created: int = 0
    mappings_unchanged: int = 0
    mappings_conflicting: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    def fail(self, line: int, record: str | None, code: str, message: str) -> None:
        self.errors.append({"line": line, "type": record, "code": code, "message": message})


class RecordError(Exception):
    """Un registro del archivo no sirve: se rechaza ESA línea y la carga continúa."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _text(value: Any, name: str, max_len: int, *, required: bool = True) -> str | None:
    """Texto tal como viene (solo se recortan los extremos), sin inventar valores ni recortes mudos."""
    if value is None:
        if required:
            raise RecordError("INVALID_RECORD", f'Falta "{name}".')
        return None
    if not isinstance(value, str):
        raise RecordError("INVALID_RECORD", f'"{name}" debe ser texto.')
    v = value.strip()
    if not v:
        if required:
            raise RecordError("INVALID_RECORD", f'"{name}" está vacío.')
        return None
    if len(v) > max_len:
        raise RecordError("INVALID_RECORD", f'"{name}" supera {max_len} caracteres.')
    if any(ord(c) < 32 for c in v):
        raise RecordError("INVALID_RECORD", f'"{name}" contiene caracteres de control.')
    return v


def _unknown(data: dict, allowed: set[str]) -> None:
    extra = sorted(set(data) - allowed - {"type"})
    if extra:
        raise RecordError(
            "INVALID_RECORD", f"Campos no reconocidos en el registro: {', '.join(extra)}."
        )


def _article(data: dict) -> dict:
    _unknown(data, ARTICLE_FIELDS)
    key = _text(data.get("identity_key"), "identity_key", 400)
    name = _text(data.get("display_name"), "display_name", 300)
    kind = data.get("product_kind")
    if kind is not None and kind not in PRODUCT_KINDS:
        raise RecordError(
            "INVALID_RECORD",
            f'"product_kind" debe ser null o uno de: {", ".join(sorted(PRODUCT_KINDS))}.',
        )
    attrs = data.get("attributes")
    if attrs is None:
        attrs = {}
    if not isinstance(attrs, dict):
        raise RecordError("INVALID_RECORD", '"attributes" debe ser un objeto JSON.')
    attrs = dict(attrs)
    if attrs.setdefault("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise RecordError(
            "INVALID_RECORD", f'"attributes.schema_version" debe ser {SCHEMA_VERSION}.'
        )
    gtin = _text(data.get("gtin"), "gtin", 14, required=False)
    if gtin is not None and not (gtin.isdigit() and 8 <= len(gtin) <= 14):
        raise RecordError("INVALID_RECORD", '"gtin" debe tener entre 8 y 14 dígitos.')
    fields = {
        "display_name": name,
        "product_kind": kind,
        "ingredient": _text(data.get("ingredient"), "ingredient", 200, required=False),
        "laboratory": _text(data.get("laboratory"), "laboratory", 200, required=False),
        "brand": _text(data.get("brand"), "brand", 200, required=False),
        "gtin": gtin,
        "sanitary_registration": _text(
            data.get("sanitary_registration"), "sanitary_registration", 30, required=False
        ),
        "attributes": attrs,
    }
    return {"record": "article", "identity_key": key, "fields": fields}


def _mapping(data: dict) -> dict:
    _unknown(data, MAPPING_FIELDS)
    raw_source = _text(data.get("source_id"), "source_id", 36)
    try:
        source_id = uuid.UUID(raw_source)
    except ValueError:
        raise RecordError("INVALID_RECORD", '"source_id" debe ser un UUID.') from None
    return {
        "record": "mapping",
        "source_id": source_id,
        "external_id": _text(data.get("external_id"), "external_id", 120),
        "item_identity_key": _text(data.get("item_identity_key"), "item_identity_key", 400),
        "external_name": _text(data.get("external_name"), "external_name", 300, required=False),
    }


def parse_records(lines: Sequence[str], report: LoadReport) -> list[dict[str, Any]]:
    """Clasifica cada línea; las inválidas se rechazan con su número, nunca en silencio."""
    out: list[dict[str, Any]] = []
    for n, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            report.fail(n, None, "INVALID_JSON", f"No es JSON válido: {e.msg}.")
            continue
        if not isinstance(data, dict):
            report.fail(n, None, "INVALID_RECORD", "Cada línea debe ser un objeto JSON.")
            continue
        kind = data.get("type")
        if kind not in RECORD_TYPES:
            report.fail(n, None, "INVALID_RECORD", 'El campo "type" debe ser "article" o "mapping".')
            continue
        try:
            rec = _article(data) if kind == "article" else _mapping(data)
        except RecordError as e:
            report.fail(n, kind, e.code, e.message)
            continue
        rec["line"] = n
        out.append(rec)
    return out


def _same_item(item: TradeItem, fields: dict) -> bool:
    return (
        item.display_name == fields["display_name"]
        and item.product_kind == fields["product_kind"]
        and item.ingredient == fields["ingredient"]
        and item.laboratory == fields["laboratory"]
        and item.brand == fields["brand"]
        and item.gtin == fields["gtin"]
        and item.sanitary_registration == fields["sanitary_registration"]
        and dict(item.attributes or {}) == fields["attributes"]
    )


def _item_snapshot(item: TradeItem) -> dict:
    return {
        "identity_key": item.identity_key,
        "display_name": item.display_name,
        "product_kind": item.product_kind,
        "ingredient": item.ingredient,
        "laboratory": item.laboratory,
        "brand": item.brand,
        "gtin": item.gtin,
        "sanitary_registration": item.sanitary_registration,
        "attributes": dict(item.attributes or {}),
    }


async def _load_articles(
    session: AsyncSession,
    actor: Actor,
    records: Sequence[dict],
    data_origin: str,
    report: LoadReport,
) -> dict[str, uuid.UUID]:
    """Alta idempotente de artículos. Devuelve clave de identidad → id, para resolver los vínculos."""
    org = actor.organization_id
    seen: dict[str, dict] = {}
    resolved: dict[str, uuid.UUID] = {}
    for rec in (r for r in records if r["record"] == "article"):
        key = rec["identity_key"]
        if key in seen:
            same = "otro contenido" if seen[key] != rec["fields"] else "el mismo contenido"
            report.fail(
                rec["line"], "article", "DUPLICATE_RECORD", f"«{key}» ya viene antes en el archivo con {same}."
            )
            continue
        seen[key] = rec["fields"]
        existing = (
            await session.execute(
                select(TradeItem).where(
                    TradeItem.organization_id == org, TradeItem.identity_key == key
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            if report.dry_run:
                resolved[key] = uuid.uuid4()  # marcador: no se escribe nada
            else:
                item = TradeItem(
                    organization_id=org,
                    identity_key=key,
                    data_origin=data_origin,
                    created_by=actor.id,
                    **rec["fields"],
                )
                session.add(item)
                await session.flush()
                emit(
                    session,
                    org_id=org,
                    actor_id=actor.id,
                    action="trade_item.created",
                    entity_type="trade_item",
                    entity_id=item.id,
                    event_type="catalog.trade_item_created",
                    aggregate_version=1,
                    payload={"identity_key": key, "origin": "catalog_load"},
                    after=_item_snapshot(item),
                )
                resolved[key] = item.id
            report.articles_created += 1
            continue
        resolved[key] = existing.id
        if _same_item(existing, rec["fields"]):
            report.articles_unchanged += 1
        else:
            report.articles_conflicting += 1
            report.fail(
                rec["line"],
                "article",
                "CONFLICT",
                f"«{key}» ya existe con otro contenido; no se sobrescribe.",
            )
    return resolved


async def _load_mappings(
    session: AsyncSession,
    actor: Actor,
    records: Sequence[dict],
    data_origin: str,
    report: LoadReport,
    resolved: dict[str, uuid.UUID],
) -> None:
    """Vínculos SKU → artículo. Solo fuentes `supplier` (la base también lo impide) y solo explícitos."""
    org = actor.organization_id
    sources: dict[uuid.UUID, Source | None] = {}
    seen: set[tuple[uuid.UUID, str]] = set()
    for rec in (r for r in records if r["record"] == "mapping"):
        if rec["source_id"] not in sources:
            sources[rec["source_id"]] = (
                await session.execute(
                    select(Source).where(
                        Source.organization_id == org, Source.id == rec["source_id"]
                    )
                )
            ).scalar_one_or_none()
        source = sources[rec["source_id"]]
        if source is None:
            report.fail(
                rec["line"],
                "mapping",
                "UNKNOWN_SOURCE",
                f"La fuente {rec['source_id']} no existe en esta organización.",
            )
            continue
        if source.kind != "supplier":
            report.fail(
                rec["line"],
                "mapping",
                "SOURCE_NOT_SUPPLIER",
                f"La fuente «{source.name}» no es de proveedor: un vínculo de SKU solo aplica a "
                "fuentes supplier.",
            )
            continue
        item_id = resolved.get(rec["item_identity_key"])
        if item_id is None:
            item_id = (
                await session.execute(
                    select(TradeItem.id).where(
                        TradeItem.organization_id == org,
                        TradeItem.identity_key == rec["item_identity_key"],
                    )
                )
            ).scalar_one_or_none()
        if item_id is None:
            report.fail(
                rec["line"],
                "mapping",
                "UNKNOWN_ARTICLE",
                f"El artículo «{rec['item_identity_key']}» no está en el archivo ni en el catálogo.",
            )
            continue
        pair = (rec["source_id"], rec["external_id"])
        if pair in seen:
            report.fail(
                rec["line"],
                "mapping",
                "DUPLICATE_RECORD",
                f"El código {rec['external_id']} de esta fuente ya viene antes en el archivo.",
            )
            continue
        seen.add(pair)
        existing = (
            await session.execute(
                select(SupplierProductMapping).where(
                    SupplierProductMapping.organization_id == org,
                    SupplierProductMapping.source_id == rec["source_id"],
                    SupplierProductMapping.external_id == rec["external_id"],
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            if not report.dry_run:
                m = SupplierProductMapping(
                    organization_id=org,
                    source_id=source.id,
                    supplier_id=source.supplier_id,
                    trade_item_id=item_id,
                    external_id=rec["external_id"],
                    external_name=rec["external_name"],
                    origin="explicit",
                    data_origin=data_origin,
                    created_by=actor.id,
                )
                session.add(m)
                await session.flush()
                emit(
                    session,
                    org_id=org,
                    actor_id=actor.id,
                    action="supplier_product_mapping.created",
                    entity_type="supplier_product_mapping",
                    entity_id=m.id,
                    event_type="catalog.mapping_created",
                    aggregate_version=1,
                    payload={
                        "source_id": str(source.id),
                        "external_id": rec["external_id"],
                        "trade_item_id": str(item_id),
                    },
                    after={
                        "trade_item_id": str(item_id),
                        "external_name": rec["external_name"],
                        "origin": "explicit",
                    },
                )
            report.mappings_created += 1
        elif (existing.trade_item_id, existing.external_name) == (item_id, rec["external_name"]):
            report.mappings_unchanged += 1
        else:
            report.mappings_conflicting += 1
            report.fail(
                rec["line"],
                "mapping",
                "CONFLICT",
                f"El código {rec['external_id']} de «{source.name}» ya está vinculado a otro "
                "artículo o con otro nombre; no se sobrescribe.",
            )


async def load_catalog(
    session: AsyncSession,
    actor: Actor,
    lines: Sequence[str],
    *,
    dry_run: bool = False,
) -> LoadReport:
    """Carga inicial del catálogo. No hace commit: la transacción la abre quien llama.

    `dry_run=True` valida y cuenta exactamente igual, pero no escribe ni emite eventos.
    """
    if Role.ADMIN.value not in (actor.roles or []):
        raise DomainError(
            "FORBIDDEN", "Solo la administración puede cargar el catálogo.", status_code=403
        )
    org = actor.organization_id
    data_origin = origin_for(await org_kind(session, org))
    report = LoadReport(dry_run=dry_run)
    records = parse_records(lines, report)
    await advisory_lock(session, f"catalog:{org}")
    resolved = await _load_articles(session, actor, records, data_origin, report)
    await _load_mappings(session, actor, records, data_origin, report, resolved)
    return report
