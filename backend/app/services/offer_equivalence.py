"""Equivalencia farmacéutica entre una oferta viva y los artículos del catálogo (A6, D-152).

Una preferencia enseñada pertenece a **un artículo** concreto (D-138). Para que ordene también las
ofertas vivas de otros proveedores, la oferta se agrupa con el artículo cuando su identidad
farmacéutica es exactamente la misma: principio activo, potencia/concentración, forma farmacéutica,
liberación y contenido del envase. Nada se resuelve por parecido de texto: se comparan atributos ya
estructurados y, si faltan, el nombre publicado se vuelve a leer con el mismo parser determinista de
los faltantes (D-015 a D-024).

Reglas que se respetan aquí:
  * solo artículos `active` y una firma **única**: dos artículos con la misma firma son ambiguos y no
    resuelven;
  * la equivalencia **no** crea vínculos (`supplier_product_mapping`) ni cambia `trade_item_id`:
    solo aporta el artículo cuya preferencia ordena la oferta. Cada oferta conserva su precio, su
    proveedor y su historial;
  * el laboratorio no forma parte de la firma porque es justamente el sujeto de la preferencia:
    «prefiero este laboratorio» solo puede ordenar si las alternativas del **mismo producto** se
    comparan juntas. La identidad comercial sigue separada (D-080): no se fusionan artículos;
  * sin principio activo, potencia, forma y contenido completos y sólidos no hay equivalencia; una
    inferencia sin sustento no alcanza para agrupar ofertas.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from decimal import InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.domain.products import (
    Attr,
    Basis,
    ContentAttr,
    ParsedProduct,
    normalize_name,
    parse_shortage_text,
)
from app.domain.units import Measure
from app.models import TradeItem

__all__ = [
    "item_signature",
    "offer_signature",
    "resolve_equivalent_item_ids",
]


def _measure_key(measure: Measure) -> str | None:
    """Clave normalizada de una medida (`mass:400mg`, `count:20un`); `None` si no se puede."""
    normalized = measure.normalized()
    return normalized.key if normalized is not None else None


def _ingredient_without_laboratory(ingredient: str | None, laboratory: str | None) -> str:
    """Retira del texto del ingrediente el laboratorio ya declarado («Ibuprofeno Ascend»).

    El laboratorio es una entidad comercial distinta del principio activo (D-021): si la fuente lo
    publicó aparte y además quedó pegado al nombre, se retira para no ensuciar la identidad. Solo se
    quita como sufijo exacto del nombre normalizado; nunca se busca dentro de otra palabra.
    """
    name = normalize_name(ingredient or "")
    lab = normalize_name(laboratory or "")
    if not lab:
        return name
    if name == lab:
        return ""
    suffix = f" {lab}"
    if name.endswith(suffix):
        return name[: -len(suffix)].strip()
    return name


def _attr_value(attr: Attr | None) -> str | None:
    return attr.value if attr is not None else None


def _pharma_signature(parsed: ParsedProduct, laboratory: str | None) -> str | None:
    """Firma farmacéutica comparable o `None` si la identidad no está completa y sólida."""
    if parsed.name_anchor or parsed.pharma_pending:
        return None
    if not parsed.strengths or parsed.form is None or parsed.content is None:
        return None
    strengths = sorted(k for k in (_measure_key(s.measure) for s in parsed.strengths) if k)
    content = _measure_key(parsed.content.measure)
    form = normalize_name(parsed.form.value)
    ingredient = _ingredient_without_laboratory(
        parsed.ingredient, laboratory or _attr_value(parsed.laboratory)
    )
    if not strengths or content is None or not form or not ingredient:
        return None
    release = normalize_name(parsed.release.value) if parsed.release else "-"
    return "|".join([ingredient, "+".join(strengths), form, release or "-", content])


def _with_presentation(parsed: ParsedProduct, presentation: str) -> ParsedProduct:
    """Completa forma y contenido desde el campo de presentación publicado por la fuente.

    El campo `presentation` **es** la presentación del envase, no una cantidad pedida: por eso un
    conteo leído de ahí es explícito aunque el parser lo marque como inferido en un texto suelto.
    Solo rellena lo que falta; nunca corrige ni reemplaza un dato ya leído del nombre.
    """
    try:
        extra = parse_shortage_text(presentation, allow_no_product=True)
    except DomainError:
        return parsed
    updates: dict[str, Any] = {}
    if parsed.content is None and extra.content is not None:
        updates["content"] = ContentAttr(extra.content.measure, Basis.EXPLICIT)
    if parsed.form is None and extra.form is not None:
        updates["form"] = Attr(extra.form.value, Basis.EXPLICIT)
    return replace(parsed, **updates) if updates else parsed


def offer_signature(
    *, name: str | None, presentation: str | None, laboratory: str | None
) -> str | None:
    """Firma de una oferta viva a partir de lo que publica la fuente. `None` si no alcanza."""
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        parsed = parse_shortage_text(name)
    except DomainError:
        return None
    if parsed.content is None and isinstance(presentation, str) and presentation.strip():
        parsed = _with_presentation(parsed, presentation)
    return _pharma_signature(parsed, laboratory)


def _parsed_from_attributes(attributes: Any) -> ParsedProduct | None:
    """Atributos v2 del artículo → producto parseado. Si no están completos, no hay producto."""
    attrs = attributes if isinstance(attributes, dict) else {}
    if not isinstance(attrs.get("ingredient"), str):
        return None
    try:
        return ParsedProduct.from_dict(attrs)
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        return None


def item_signature(
    *,
    display_name: str | None,
    laboratory: str | None,
    attributes: Any,
) -> str | None:
    """Firma de un artículo canónico: sus atributos estructurados o, si faltan, su nombre."""
    parsed = _parsed_from_attributes(attributes)
    if parsed is not None:
        signature = _pharma_signature(parsed, laboratory or _attr_value(parsed.laboratory))
        if signature is not None:
            return signature
    if not isinstance(display_name, str) or not display_name.strip():
        return None
    try:
        parsed = parse_shortage_text(display_name)
    except DomainError:
        return None
    return _pharma_signature(parsed, laboratory or _attr_value(parsed.laboratory))


def _mentions(attribute_value: str | None, hints: set[str]) -> bool:
    text = normalize_name(attribute_value or "")
    return bool(text) and any(hint in text for hint in hints)


async def resolve_equivalent_item_ids(
    session: AsyncSession,
    organization_id: uuid.UUID,
    entries: list[dict[str, Any]],
) -> dict[str, uuid.UUID]:
    """`external_id` → artículo cuya preferencia ordena esa oferta, solo con firma única.

    Se llama en cada lectura después de resolver los vínculos exactos; una oferta ya vinculada
    (mapping, EAN único o identidad técnica) no se toca. Los artículos con la misma firma son
    ambiguos: se dejan sin equivalencia en vez de elegir uno al azar.
    """
    wanted: dict[str, str] = {}
    for entry in entries:
        if entry.get("trade_item_id"):
            continue
        signature = offer_signature(
            name=entry.get("name"),
            presentation=entry.get("presentation"),
            laboratory=entry.get("laboratory"),
        )
        if signature is not None:
            wanted[entry["external_id"]] = signature
    if not wanted:
        return {}

    wanted_signatures = set(wanted.values())
    hints = {signature.split("|", 1)[0] for signature in wanted_signatures}
    rows = (
        await session.execute(
            select(
                TradeItem.id,
                TradeItem.display_name,
                TradeItem.ingredient,
                TradeItem.laboratory,
                TradeItem.attributes,
            ).where(
                TradeItem.organization_id == organization_id,
                TradeItem.status == "active",
            )
        )
    ).all()

    by_signature: dict[str, set[uuid.UUID]] = {}
    for row in rows:
        attrs = row.attributes if isinstance(row.attributes, dict) else {}
        if not (
            _mentions(row.display_name, hints)
            or _mentions(row.ingredient, hints)
            or _mentions(attrs.get("ingredient"), hints)
        ):
            continue  # prefiltro barato: no puede ser el mismo principio activo
        signature = item_signature(
            display_name=row.display_name,
            laboratory=row.laboratory,
            attributes=row.attributes,
        )
        if signature in wanted_signatures:
            by_signature.setdefault(signature, set()).add(row.id)

    resolved: dict[str, uuid.UUID] = {}
    for external_id, signature in wanted.items():
        candidates = by_signature.get(signature) or set()
        if len(candidates) == 1:
            resolved[external_id] = next(iter(candidates))
    return resolved
