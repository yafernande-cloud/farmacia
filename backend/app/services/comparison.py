"""E5 — comparación determinista de ofertas (C3, C0 §2.4, §3.4 y §E5).

Tres niveles que no se confunden:

  * **candidato**   un par (línea, oferta): `eligibility` + causas, y el costo de esa línea.
  * **proveedor completo**  todas las líneas con candidato `eligible` y todos los costos
                    determinados: `completeness=complete`, `blockers=[]` y `total`.
  * **proveedor parcial**   cubre algo, pero falta cobertura o un costo determinable: **nunca**
                    tiene `total`; puede tener cota inferior.

No hay IA aquí: cantidades, descuentos, impuestos y despacho se calculan con las fórmulas del
contrato. Un dato desconocido es `NULL` y una causa tipada, nunca 0 ni 1. Ninguna anotación
comercial altera un número (D-091).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import DomainError, not_found
from app.core.permissions import require_admin
from app.models import (
    ComparisonCandidate,
    ComparisonLine,
    ComparisonRun,
    ComparisonSupplierTotal,
    OfferSnapshot,
    SearchRun,
    ShippingTermsSnapshot,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
)
from app.services import criteria as criteria_svc
from app.services import sources as sources_svc
from app.services.actors import Actor
from app.services.provenance import org_kind, origin_for
from app.services.search import _latest_offers

Q4 = Decimal("0.0001")
ZERO = Decimal(0)
CENT = Decimal("0.01")
MAX_LINES = 50
UNITS_SET = ("box", "package", "unit", "bottle")
HARD_CAUSES = (
    "identity_mismatch",
    "presentation_mismatch",
    "supplier_inactive",
    "quantity_unit_mismatch",
)
ELIGIBILITY_ORDER = {"eligible": 0, "needs_review": 1, "ineligible": 2}


@dataclass(frozen=True)
class LineInput:
    article_id: uuid.UUID
    value: Decimal
    unit: str


# --------------------------------------------------------------------------- formatos


def _money(amount: Decimal | None, currency: str | None) -> dict | None:
    if amount is None or currency is None:
        return None
    return {"amount": str(amount.quantize(CENT, rounding=ROUND_HALF_UP)), "currency": currency}


def _qty(value: Decimal | None) -> str | None:
    """Cantidad como texto sin ceros de relleno: 20.000 → «20»."""
    if value is None:
        return None
    return format(value.normalize(), "f")


def _dec(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- contexto de oferta


def _article_units(item: TradeItem) -> Decimal | None:
    """Unidades por envase declaradas en el artículo (solo cuando el contenido está en «un»)."""
    attrs = item.attributes if isinstance(item.attributes, dict) else {}
    content = attrs.get("content")
    measure = content.get("measure") if isinstance(content, dict) else None
    if not isinstance(measure, dict) or measure.get("unit") != "un":
        return None
    return _dec(measure.get("value"))


def _ingredient_info(item: TradeItem) -> tuple[str | None, str]:
    """(principio activo, procedencia). Una cadena sola se considera explícita: solo se escribe
    cuando alguien la declaró; lo ausente queda desconocido, no inferido."""
    attrs = item.attributes if isinstance(item.attributes, dict) else {}
    raw = attrs.get("ingredient")
    if isinstance(raw, dict):
        value = raw.get("value")
        basis = raw.get("basis") or "explicit"
        return (value if isinstance(value, str) else None), basis
    if isinstance(raw, str) and raw.strip():
        return raw.strip(), "explicit"
    return item.ingredient, ("explicit" if item.ingredient else "unknown")


def _limits(offer: OfferSnapshot) -> tuple[Decimal | None, Decimal | None, list[str]]:
    """(mínimo, múltiplo, causas). Ausente no es 0 ni 1: si no se conoce, no se aplica."""
    causes: list[str] = []
    minimum: Decimal | None = None
    if offer.minimum_known and offer.minimum_quantity is not None:
        if offer.minimum_unit and offer.minimum_unit != offer.sale_unit:
            # Convertir exigiría un pack confirmado; no se interpreta, se declara no modelado.
            causes.append("unmodelled_condition")
        else:
            minimum = offer.minimum_quantity
    else:
        causes.append("minimum_unknown")
    multiple: Decimal | None = None
    if offer.multiple_known and offer.multiple_quantity is not None:
        if offer.multiple_unit and offer.multiple_unit != offer.sale_unit:
            causes.append("unmodelled_condition")
        elif offer.multiple_quantity >= 1:
            multiple = offer.multiple_quantity
    else:
        causes.append("multiple_unknown")
    return minimum, multiple, causes


def _quantity(
    requested: Decimal, minimum: Decimal | None, multiple: Decimal | None
) -> tuple[Decimal, Decimal, Decimal, str]:
    """Fórmula de C0 §2.4: base, comprado, excedente y qué condición elevó la cantidad."""
    base = requested if minimum is None else max(requested, minimum)
    step = multiple or Decimal(1)
    purchased = (base / step).to_integral_value(rounding=ROUND_CEILING) * step
    min_raised = minimum is not None and minimum > requested
    mult_raised = purchased > base
    if min_raised and mult_raised:
        adjustment = "minimum_and_multiple"
    elif min_raised:
        adjustment = "minimum"
    elif mult_raised:
        adjustment = "multiple"
    else:
        adjustment = "none"
    return base, purchased, purchased - requested, adjustment


def _tax_rate(offer: OfferSnapshot) -> tuple[Decimal, bool]:
    """(tasa total en %, ¿todas conocidas?). Sin impuestos declarados la tasa es 0 conocida."""
    total = ZERO
    for tax in offer.taxes or []:
        if not isinstance(tax, dict):
            return ZERO, False
        rate = _dec(tax.get("rate_percent"))
        if rate is None:
            return ZERO, False
        total += rate
    return total, True


def _discount_rules(offer: OfferSnapshot) -> list[dict[str, Any]]:
    """Estructura tipada de C0 §4.2; `condition_text` nunca se interpreta."""
    rules: list[dict[str, Any]] = []
    for raw in offer.discounts or []:
        if not isinstance(raw, dict):
            rules.append({"invalid": True})
            continue
        threshold = raw.get("threshold")
        min_quantity = (
            threshold.get("quantity") if isinstance(threshold, dict) else raw.get("min_quantity")
        )
        rules.append(
            {
                "kind": raw.get("kind"),
                "value": raw.get("value"),
                "applicability": raw.get("applicability"),
                "min_quantity": min_quantity,
                "scope": raw.get("scope", "line"),
            }
        )
    return rules


# --------------------------------------------------------------------------- candidato


def _candidate_values(
    *,
    line: LineInput,
    item: TradeItem,
    offer: OfferSnapshot,
    supplier: Supplier,
    mapped_item_id: uuid.UUID | None,
    now: datetime,
    stale_after_hours: int,
) -> dict[str, Any]:
    """Todo el cálculo de un par (línea, oferta). No toca la base."""
    hard: list[str] = []
    soft: list[str] = []

    # --- causas duras: identidad, presentación, proveedor y unidad
    if mapped_item_id != item.id:
        # Sin vínculo explícito no se puede afirmar que el SKU sea este artículo (D-076).
        hard.append("identity_mismatch")
    units = _article_units(item)
    pack = offer.pack_units_per_commercial_unit
    if units is not None and pack is not None and pack != units:
        hard.append("presentation_mismatch")
    if supplier.status != "active":
        hard.append("supplier_inactive")
    if line.unit != offer.sale_unit:
        hard.append("quantity_unit_mismatch")

    values: dict[str, Any] = {
        "eligibility": "ineligible",
        "causes": sorted(set(hard)),
        "quantity_requested": line.value,
        "minimum_known": offer.minimum_known,
        "minimum_quantity": offer.minimum_quantity,
        "multiple_known": offer.multiple_known,
        "multiple_quantity": offer.multiple_quantity,
        "base_quantity": None,
        "purchased": None,
        "surplus": None,
        "adjustment": None,
        "price_amount": offer.price_amount,
        "currency": offer.currency,
        "price_basis": offer.price_basis,
        "tax_included": offer.tax_included,
        "discount_included": offer.discount_included,
        "pack_units_per_commercial_unit": offer.pack_units_per_commercial_unit,
        "line_gross": None,
        "line_discounts": [],
        "line_discounts_amount": None,
        "line_tax_added": None,
        "line_payable": None,
        "line_net": None,
        "discounted_is_possible": False,
        "stock_status": offer.stock_status,
        "stock_sufficient": None,
        "shipping_terms_id": offer.shipping_terms_id,
        "observed_at": offer.observed_at,
        "evidence": offer.evidence or {},
        "data_origin": offer.data_origin,
    }
    if hard:
        return values

    # --- frescura y estado de revisión
    fresh_until = offer.observed_at + timedelta(hours=stale_after_hours)
    if now > fresh_until:
        soft.append("price_stale")
    if offer.valid_to is not None and offer.valid_to < now:
        soft.append("price_stale")
    if offer.valid_from is not None and offer.valid_from > now:
        soft.append("price_stale")
    if offer.review_status != "accepted":
        # Una oferta marcada no es una oferta verificada (D-110) y su motivo no es una causa tipada.
        soft.append("unmodelled_condition")

    # --- cantidad comprada
    minimum, multiple, limit_causes = _limits(offer)
    soft.extend(limit_causes)
    base, purchased, surplus, adjustment = _quantity(line.value, minimum, multiple)

    # --- precio bruto
    gross: Decimal | None = None
    if offer.price_basis == "per_commercial_unit":
        gross = purchased * offer.price_amount
    elif offer.price_basis == "per_base_unit" and pack:
        gross = purchased * pack * offer.price_amount
    else:
        soft.append("price_basis_unknown")

    # --- descuentos
    discounts_out: list[dict[str, Any]] = []
    discount_total = ZERO
    possible = False
    for rule in _discount_rules(offer):
        if rule.get("invalid") or rule.get("scope") != "line":
            # Un cargo o descuento de pedido no modelado impide afirmar un total.
            soft.append("unmodelled_condition")
            continue
        applicability = rule["applicability"]
        if applicability == "always":
            applies = True
        elif applicability == "min_quantity":
            threshold = _dec(rule["min_quantity"])
            if threshold is None:
                soft.append("discount_conditions_unknown")
                possible = True
                continue
            applies = purchased >= threshold
            if not applies and not (offer.minimum_known and offer.multiple_known):
                # La cantidad puede subir: el descuento podría activarse (LB-6).
                possible = True
        else:
            soft.append("discount_conditions_unknown")
            possible = True
            continue
        if not applies or offer.discount_included is True:
            continue  # no aplica, o ya está incorporado en el precio (evita doble resta)
        if offer.discount_included is None:
            soft.append("discount_inclusion_unknown")
            continue
        value = _dec(rule["value"])
        amount: Decimal | None = None
        if rule["kind"] == "percent" and value is not None and gross is not None:
            amount = (gross * value / 100).quantize(Q4, rounding=ROUND_HALF_UP)
        elif rule["kind"] == "amount" and value is not None:
            amount = value.quantize(Q4, rounding=ROUND_HALF_UP)
        if amount is None or amount < 0:
            soft.append("unmodelled_condition")
            continue
        discount_total += amount
        discounts_out.append(
            {
                "kind": rule["kind"],
                "value": str(value),
                "amount": _money(amount, offer.currency),
                "applicability": applicability,
            }
        )

    after: Decimal | None = None if gross is None else gross - discount_total
    if after is not None and after < 0:
        soft.append("unmodelled_condition")
        after = None

    # --- impuestos
    rate, rate_known = _tax_rate(offer)
    tax_added: Decimal | None
    net: Decimal | None
    payable: Decimal | None
    if offer.tax_included is None:
        tax_added, net, payable = None, None, None
        soft.append("taxes_unknown")
    elif offer.tax_included:
        tax_added = ZERO
        net = (
            (after / (1 + rate / 100)).quantize(Q4, rounding=ROUND_HALF_UP)
            if after is not None and rate_known
            else None
        )
        payable = after
    elif after is not None and rate_known:
        tax_added = (after * rate / 100).quantize(Q4, rounding=ROUND_HALF_UP)
        net = after
        payable = after + tax_added
    else:
        tax_added, net, payable = None, None, None
        soft.append("taxes_unknown")

    # --- stock (se evalúa contra lo COMPRADO, no contra lo pedido)
    stock_sufficient: bool | None = None
    if (
        offer.stock_status == "known"
        and offer.stock_quantity is not None
        and offer.stock_unit == offer.sale_unit
    ):
        stock_sufficient = offer.stock_quantity >= purchased
        if not stock_sufficient:
            soft.append("stock_insufficient")
    else:
        soft.append("stock_unknown")

    soft = sorted(set(soft))
    values.update(
        {
            "eligibility": "needs_review" if soft else "eligible",
            "causes": soft,
            "base_quantity": base,
            "purchased": purchased,
            "surplus": surplus,
            "adjustment": adjustment,
            "line_gross": gross,
            "line_discounts": discounts_out,
            "line_discounts_amount": discount_total if gross is not None else None,
            "line_tax_added": tax_added,
            "line_payable": payable,
            "line_net": net,
            "discounted_is_possible": possible,
            "stock_sufficient": stock_sufficient,
        }
    )
    return values



# --------------------------------------------------------------------------- totales


@dataclass
class CandidateCalc:
    """Resultado en memoria de un par (línea, oferta) antes de persistir nada."""

    line_index: int
    offer: OfferSnapshot
    supplier: Supplier
    values: dict[str, Any]

    @property
    def eligibility(self) -> str:
        return self.values["eligibility"]

    @property
    def causes(self) -> list[str]:
        return self.values["causes"]

    @property
    def line_payable(self) -> Decimal | None:
        return self.values["line_payable"]


def _candidate_order(c: CandidateCalc) -> tuple:
    return (
        ELIGIBILITY_ORDER[c.values["eligibility"]],
        c.values["line_payable"] is None,
        c.values["line_payable"] or ZERO,
        str(c.offer.id),
    )


def _shipping(
    terms: ShippingTermsSnapshot | None, *, currency: str | None, base: Decimal | None
) -> tuple[str | None, bool, Decimal | None, uuid.UUID | None, str | None]:
    """(scheme, known, amount, terms_id, causa). El texto del proveedor nunca se interpreta."""
    if terms is None:
        return None, False, None, None, "shipping_unknown"
    if currency is None or terms.currency != currency:
        return terms.scheme, False, None, terms.id, "currency_mismatch"
    if terms.scheme == "free":
        return terms.scheme, True, ZERO, terms.id, None
    if terms.scheme == "flat":
        if terms.flat_amount is None:
            return terms.scheme, False, None, terms.id, "shipping_unknown"
        return terms.scheme, True, terms.flat_amount, terms.id, None
    # free_over_threshold: el umbral se evalúa sobre la base del pedido, no sobre el precio suelto.
    if base is None or terms.threshold_amount is None or terms.flat_amount is None:
        return terms.scheme, False, None, terms.id, "shipping_unknown"
    amount = ZERO if base >= terms.threshold_amount else terms.flat_amount
    return terms.scheme, True, amount, terms.id, None


def _supplier_total(
    supplier: Supplier,
    cands: list[CandidateCalc],
    line_indexes: list[int],
    terms: ShippingTermsSnapshot | None,
) -> dict[str, Any]:
    """Mejor candidato por línea y agregados del proveedor. Nunca calcula un total parcial."""
    best: dict[int, CandidateCalc] = {}
    for c in cands:
        current = best.get(c.line_index)
        if current is None or _candidate_order(c) < _candidate_order(current):
            best[c.line_index] = c

    covered = [i for i in line_indexes if i in best and best[i].eligibility != "ineligible"]
    missing = [i for i in line_indexes if i not in best or best[i].eligibility == "ineligible"]
    chosen = [best[i] for i in covered]

    currencies = {c.values["currency"] for c in chosen}
    currency = next(iter(currencies)) if len(currencies) == 1 else None

    blockers: set[str] = set()
    if len(currencies) > 1:
        # Monedas distintas entre líneas: no hay un total sumable ni un mínimo comparable.
        blockers.add("currency_mismatch")
    for i in line_indexes:
        cand = best.get(i)
        if cand is None:
            blockers.add("source_failed")
        elif cand.eligibility != "eligible":
            blockers.update(cand.causes)

    subtotal: Decimal | None = ZERO
    taxes_added: Decimal | None = ZERO
    discounts_total: Decimal | None = ZERO
    for c in chosen:
        gross = c.values["line_gross"]
        disc = c.values["line_discounts_amount"]
        if gross is None or disc is None:
            subtotal = None
            discounts_total = None
        else:
            if subtotal is not None:
                subtotal += gross - disc
            if discounts_total is not None:
                discounts_total += disc
        tax = c.values["line_tax_added"]
        if tax is None:
            taxes_added = None
        elif taxes_added is not None:
            taxes_added += tax

    tax_flags = {c.values["tax_included"] for c in chosen}
    taxes_included = next(iter(tax_flags)) if len(tax_flags) == 1 and None not in tax_flags else None

    shipping_base = (
        subtotal + taxes_added if subtotal is not None and taxes_added is not None else None
    )
    scheme, shipping_known, shipping_amount, terms_id, shipping_cause = _shipping(
        terms, currency=currency, base=shipping_base
    )
    if shipping_cause:
        blockers.add(shipping_cause)

    complete = (
        not missing
        and bool(chosen)
        and all(c.eligibility == "eligible" for c in chosen)
        and subtotal is not None
        and taxes_added is not None
        and shipping_known
        and currency is not None
        and not blockers
    )
    total: Decimal | None = None
    if complete:
        total = (subtotal + taxes_added + (shipping_amount or ZERO)).quantize(Q4)

    # --- cota inferior (LB-1 … LB-8)
    bound_reasons: set[str] = set()
    if len(currencies) > 1:
        bound_reasons.add("currency_mismatch")
    if missing:
        bound_reasons.add("source_failed")
    bound_sum = ZERO
    for c in chosen:
        if c.values["line_gross"] is None or c.values["line_discounts_amount"] is None:
            bound_reasons.add("price_basis_unknown")
            continue
        if "discount_inclusion_unknown" in c.causes:
            bound_reasons.add("discount_inclusion_unknown")
        if "discount_conditions_unknown" in c.causes:
            bound_reasons.add("discount_conditions_unknown")
        if c.values["discounted_is_possible"] and (
            "minimum_unknown" in c.causes or "multiple_unknown" in c.causes
        ):
            # LB-6: un descuento por umbral podría activarse y bajar el total.
            bound_reasons.add("discount_conditions_unknown")
        bound_sum += c.values["line_gross"] - c.values["line_discounts_amount"]
    lower_bound: Decimal | None = None
    if chosen and not bound_reasons:
        lower_bound = (bound_sum + (shipping_amount if shipping_known else ZERO)).quantize(Q4)

    observed = [c.offer.observed_at for c in chosen] or [c.offer.observed_at for c in cands]
    freshness = {
        "oldest_observed_at": min(observed).isoformat(),
        "newest_observed_at": max(observed).isoformat(),
        "is_stale": any("price_stale" in c.causes for c in chosen),
        "stale_lines": [str(i) for i, c in zip(covered, chosen, strict=True) if "price_stale" in c.causes],
    }

    return {
        "supplier": supplier,
        "completeness": "complete" if complete else "partial",
        "currency": currency,
        "lines_covered": covered,
        "lines_missing": missing,
        "subtotal": subtotal,
        "taxes_included": taxes_included,
        "taxes_added": taxes_added,
        "discounts_total": discounts_total,
        "shipping_scheme": scheme,
        "shipping_known": shipping_known,
        "shipping_amount": shipping_amount if shipping_known else None,
        "shipping_terms_id": terms_id,
        "other_charges": [],
        "total": total,
        "lower_bound": lower_bound,
        "bound_status": "valid" if lower_bound is not None else "not_boundable",
        "bound_invalid_reasons": sorted(bound_reasons),
        "blockers": sorted(blockers),
        "causes": sorted(blockers),
        "freshness": freshness,
    }


# --------------------------------------------------------------------------- E5


async def _shipping_terms(
    session: AsyncSession, org: uuid.UUID, supplier_ids: set[uuid.UUID], *, now: datetime
) -> dict[uuid.UUID, ShippingTermsSnapshot]:
    """Última observación de despacho por proveedor, solo si su vigencia cubre `now`."""
    if not supplier_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(ShippingTermsSnapshot)
                .where(
                    ShippingTermsSnapshot.organization_id == org,
                    ShippingTermsSnapshot.supplier_id.in_(supplier_ids),
                    ShippingTermsSnapshot.observed_at <= now,
                )
                .ext(distinct_on(ShippingTermsSnapshot.supplier_id))
                .order_by(
                    ShippingTermsSnapshot.supplier_id,
                    ShippingTermsSnapshot.observed_at.desc(),
                    ShippingTermsSnapshot.created_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    out: dict[uuid.UUID, ShippingTermsSnapshot] = {}
    for t in rows:
        if t.valid_from is not None and t.valid_from > now:
            continue
        if t.valid_to is not None and t.valid_to < now:
            continue
        out[t.supplier_id] = t
    return out


async def _mappings(
    session: AsyncSession, org: uuid.UUID, offers: list[OfferSnapshot]
) -> dict[tuple[uuid.UUID, str], uuid.UUID]:
    if not offers:
        return {}
    pairs = list({(o.source_id, o.external_id) for o in offers})
    rows = (
        await session.execute(
            select(
                SupplierProductMapping.source_id,
                SupplierProductMapping.external_id,
                SupplierProductMapping.trade_item_id,
            ).where(
                SupplierProductMapping.organization_id == org,
                tuple_(
                    SupplierProductMapping.source_id, SupplierProductMapping.external_id
                ).in_(pairs),
            )
        )
    ).all()
    return {(src, ext): item for src, ext, item in rows}


def _candidate_payload(
    row: ComparisonCandidate,
    supplier: Supplier,
    line: ComparisonLine,
    *,
    annotations: list,
    notes: list,
) -> dict[str, Any]:
    return {
        "candidate_id": row.id,
        "line_id": row.comparison_line_id,
        "offer_id": row.offer_id,
        "article_id": line.article_id,
        "supplier": {
            "supplier_id": supplier.id,
            "name": supplier.name,
            "status": supplier.status,
        },
        "offer_price": {
            "money": _money(row.price_amount, row.currency),
            "basis": row.price_basis,
            "tax_included": row.tax_included,
            "discount_included": row.discount_included,
        },
        "quantity": {
            "requested": {"value": _qty(row.quantity_requested), "unit": line.quantity_unit},
            "minimum": {
                "known": row.minimum_known,
                "quantity": _qty(row.minimum_quantity),
                "unit": line.quantity_unit if row.minimum_known else None,
            },
            "multiple": {
                "known": row.multiple_known,
                "quantity": _qty(row.multiple_quantity),
                "unit": line.quantity_unit if row.multiple_known else None,
            },
            "base": _qty(row.base_quantity),
            "purchased": _qty(row.purchased),
            "surplus": _qty(row.surplus),
            "unit": line.quantity_unit,
            "adjustment": row.adjustment,
        },
        "line_gross": _money(row.line_gross, row.currency),
        "line_discounts": row.line_discounts or [],
        "line_tax_added": _money(row.line_tax_added, row.currency),
        "line_payable": _money(row.line_payable, row.currency),
        "line_net": _money(row.line_net, row.currency),
        "eligibility": row.eligibility,
        "causes": row.causes or [],
        "stock_status": row.stock_status,
        "stock_sufficient": row.stock_sufficient,
        "shipping_terms_id": row.shipping_terms_id,
        "discounted_is_possible": row.discounted_is_possible,
        "annotations": annotations,
        "notes": notes,
        "provenance": {
            "source_id": row.source_id,
            "data_origin": row.data_origin,
            "observed_at": row.observed_at,
            "evidence": row.evidence or {},
        },
        "freshness": {
            "observed_at": row.observed_at,
            "is_stale": "price_stale" in (row.causes or []),
        },
        "computed_at": row.computed_at,
    }


def _total_payload(
    row: ComparisonSupplierTotal,
    supplier: Supplier,
    *,
    annotations: list,
    notes: list,
) -> dict[str, Any]:
    return {
        "supplier_id": row.supplier_id,
        "supplier_name": supplier.name,
        "supplier_status": supplier.status,
        "currency": row.currency,
        "completeness": row.completeness,
        "lines_covered": [uuid.UUID(x) for x in row.lines_covered],
        "lines_missing": [uuid.UUID(x) for x in row.lines_missing],
        "subtotal": _money(row.subtotal, row.currency),
        "taxes_included": row.taxes_included,
        "taxes_added": _money(row.taxes_added, row.currency),
        "discounts_total": _money(row.discounts_total, row.currency),
        "shipping": {
            "scheme": row.shipping_scheme,
            "known": row.shipping_known,
            "amount": _money(row.shipping_amount, row.currency),
            "terms_id": row.shipping_terms_id,
        },
        "other_charges": row.other_charges,
        "total": _money(row.total, row.currency),
        "lower_bound": _money(row.lower_bound, row.currency),
        "bound_status": row.bound_status,
        "bound_invalid_reasons": row.bound_invalid_reasons,
        "blockers": row.blockers,
        "causes": row.causes,
        "annotations": annotations,
        "notes": notes,
        "freshness": row.freshness,
        "computed_at": row.computed_at,
    }


async def run_comparison(
    session: AsyncSession,
    actor: Actor,
    *,
    lines: list[LineInput],
    source_search_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> tuple[ComparisonRun, dict[str, Any]]:
    """Ejecuta E5 y guarda la corrida completa en una sola escritura por fila."""
    require_admin(actor.roles)
    now = now or datetime.now(UTC)
    settings = get_settings()
    org = actor.organization_id
    stale_after = settings.source_stale_after_hours

    if not 1 <= len(lines) <= MAX_LINES:
        raise DomainError(
            "INVALID_QUANTITY",
            f"Una comparación exige entre 1 y {MAX_LINES} líneas.",
            status_code=422,
        )
    for line in lines:
        if line.value is None or line.value <= 0 or line.value != line.value.to_integral_value():
            raise DomainError(
                "INVALID_QUANTITY",
                "La cantidad debe ser un entero mayor que cero.",
                status_code=422,
            )
        if line.unit not in UNITS_SET:
            raise DomainError(
                "INVALID_QUANTITY", "La unidad de la cantidad no es válida.", status_code=422
            )
    article_ids = list(dict.fromkeys(line.article_id for line in lines))
    items = {
        i.id: i
        for i in (
            await session.execute(
                select(TradeItem).where(
                    TradeItem.organization_id == org, TradeItem.id.in_(article_ids)
                )
            )
        )
        .scalars()
        .all()
    }
    for article_id in article_ids:
        if article_id not in items:
            raise not_found("El artículo")

    if source_search_id is not None:
        exists = (
            await session.execute(
                select(SearchRun.id).where(
                    SearchRun.id == source_search_id, SearchRun.organization_id == org
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            raise not_found("La búsqueda")

    offers_by_item = await _latest_offers(session, org, article_ids, now=now)
    all_offers = [o for group in offers_by_item.values() for o in group]
    supplier_ids = {o.supplier_id for o in all_offers}
    suppliers = {
        s.id: s
        for s in (
            await session.execute(
                select(Supplier).where(
                    Supplier.organization_id == org, Supplier.id.in_(supplier_ids)
                )
            )
        )
        .scalars()
        .all()
    }
    mappings = await _mappings(session, org, all_offers)
    terms = await _shipping_terms(session, org, supplier_ids, now=now)
    # Fuentes que la comparación consulta: las habilitadas con capacidad de búsqueda.
    source_rows = [
        s
        for s in (
            await session.execute(
                select(Source).where(
                    Source.organization_id == org,
                    Source.kind == "supplier",
                    Source.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
        if "search" in (s.capabilities or [])
    ]
    # El estado se informa tal como lo ve E4; no se usa para descartar proveedores.
    source_states = {
        s.id: sources_svc.source_status(s, now=now, stale_after_hours=stale_after)
        for s in source_rows
    }

    criteria_rows = await criteria_svc.active_criteria(session, org, now=now)

    calcs: list[CandidateCalc] = []
    for index, line in enumerate(lines):
        item = items[line.article_id]
        for offer in offers_by_item.get(line.article_id, []):
            supplier = suppliers.get(offer.supplier_id)
            if supplier is None:
                continue
            calcs.append(
                CandidateCalc(
                    line_index=index,
                    offer=offer,
                    supplier=supplier,
                    values=_candidate_values(
                        line=line,
                        item=item,
                        offer=offer,
                        supplier=supplier,
                        mapped_item_id=mappings.get((offer.source_id, offer.external_id)),
                        now=now,
                        stale_after_hours=stale_after,
                    ),
                )
            )

    # --- totales por proveedor (los descartados no participan de nada)
    by_supplier: dict[uuid.UUID, list[CandidateCalc]] = {}
    for c in calcs:
        by_supplier.setdefault(c.supplier.id, []).append(c)
    line_indexes = list(range(len(lines)))
    totals: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    for supplier_id, group in by_supplier.items():
        if all(c.eligibility == "ineligible" for c in group):
            causes = sorted({cause for c in group for cause in c.causes})
            discarded.append(
                {
                    "supplier_id": supplier_id,
                    "supplier_name": suppliers[supplier_id].name,
                    "causes": causes,
                }
            )
            continue
        totals.append(
            _supplier_total(suppliers[supplier_id], group, line_indexes, terms.get(supplier_id))
        )

    # --- estado global
    failed = [str(sid) for sid, state in source_states.items() if state == "failed"]
    complete = sum(1 for t in totals if t["completeness"] == "complete")
    if not calcs:
        status = "failed"
    elif complete == 0:
        status = "no_complete_total"
    elif complete == len(totals) and not discarded and not failed:
        status = "complete"
    else:
        status = "partial_coverage"

    line_states: list[tuple[str, list[str]]] = []
    for index in line_indexes:
        group = [c for c in calcs if c.line_index == index]
        elig = [c.eligibility for c in group]
        blockers = sorted({cause for c in group for cause in c.causes})
        if "eligible" in elig:
            line_states.append(("complete", []))
        elif "needs_review" in elig:
            line_states.append(("partial", blockers))
        else:
            line_states.append(("unavailable", blockers))

    # --- persistencia: cada fila se escribe UNA vez (las tablas son de solo agregar)
    run = ComparisonRun(
        organization_id=org,
        source_search_id=source_search_id,
        status=status,
        data_origin=origin_for(await org_kind(session, org)),
        criteria_evaluated_at=now,
        suppliers_evaluated=len(totals) + len(discarded),
        suppliers_complete=complete,
        suppliers_incomplete=len(totals) - complete + len(discarded),
        sources_consulted=len(source_rows),
        sources_failed=failed,
        created_by=actor.id,
        generated_at=now,
    )
    session.add(run)
    await session.flush()

    line_rows = [
        ComparisonLine(
            organization_id=org,
            comparison_run_id=run.id,
            position=index,
            article_id=line.article_id,
            quantity_value=line.value,
            quantity_unit=line.unit,
            completeness=line_states[index][0],
            blockers=line_states[index][1],
        )
        for index, line in enumerate(lines)
    ]
    session.add_all(line_rows)
    await session.flush()

    cand_rows = [
        ComparisonCandidate(
            organization_id=org,
            comparison_run_id=run.id,
            comparison_line_id=line_rows[c.line_index].id,
            offer_id=c.offer.id,
            supplier_id=c.supplier.id,
            source_id=c.offer.source_id,
            **c.values,
        )
        for c in calcs
    ]
    session.add_all(cand_rows)
    await session.flush()

    total_rows: list[ComparisonSupplierTotal] = []
    for t in totals:
        total_rows.append(
            ComparisonSupplierTotal(
                organization_id=org,
                comparison_run_id=run.id,
                supplier_id=t["supplier"].id,
                completeness=t["completeness"],
                currency=t["currency"],
                lines_covered=[str(line_rows[i].id) for i in t["lines_covered"]],
                lines_missing=[str(line_rows[i].id) for i in t["lines_missing"]],
                subtotal=t["subtotal"],
                taxes_included=t["taxes_included"],
                taxes_added=t["taxes_added"],
                discounts_total=t["discounts_total"],
                shipping_scheme=t["shipping_scheme"],
                shipping_known=t["shipping_known"],
                shipping_amount=t["shipping_amount"],
                shipping_terms_id=t["shipping_terms_id"],
                other_charges=t["other_charges"],
                total=t["total"],
                lower_bound=t["lower_bound"],
                bound_status=t["bound_status"],
                bound_invalid_reasons=t["bound_invalid_reasons"],
                blockers=t["blockers"],
                causes=t["causes"],
                freshness=t["freshness"],
            )
        )
    session.add_all(total_rows)
    await session.flush()

    # --- armado de la respuesta: las anotaciones solo leen, nunca cambian un número
    payload_lines = []
    for index, line_row in enumerate(line_rows):
        item = items[line_row.article_id]
        ingredient, basis = _ingredient_info(item)
        entries: list[dict[str, Any]] = []
        for calc, row in zip(calcs, cand_rows, strict=True):
            if calc.line_index != index:
                continue
            scope = criteria_svc.Scope(
                supplier_id=calc.supplier.id,
                article_id=item.id,
                ingredient=ingredient,
                ingredient_basis=basis,
                quantity=calc.values["purchased"],
                price_amount=calc.values["price_amount"],
                price_currency=calc.values["currency"],
                price_basis=calc.values["price_basis"],
            )
            annotations, notes = criteria_svc.annotations_for(criteria_rows, scope, now=now)
            entries.append(
                _candidate_payload(
                    row, calc.supplier, line_row, annotations=annotations, notes=notes
                )
            )
        entries.sort(
            key=lambda p: (
                {"eligible": 0, "needs_review": 1, "ineligible": 2}[p["eligibility"]],
                p["line_payable"] is None,
                Decimal(p["line_payable"]["amount"]) if p["line_payable"] else ZERO,
                p["offer_id"].hex,
            )
        )
        payload_lines.append(
            {
                "line_id": line_row.id,
                "article_id": item.id,
                "quantity": {"value": _qty(line_row.quantity_value), "unit": line_row.quantity_unit},
                "candidates": entries,
                "completeness": line_row.completeness,
                "blockers": line_row.blockers,
            }
        )

    supplier_payloads = []
    for row in sorted(
        total_rows,
        key=lambda r: (r.total is None, r.total if r.total is not None else Decimal("1e18")),
    ):
        annotations, notes = criteria_svc.annotations_for(
            criteria_rows, criteria_svc.Scope(supplier_id=row.supplier_id), now=now
        )
        supplier_payloads.append(
            _total_payload(
                row, suppliers[row.supplier_id], annotations=annotations, notes=notes
            )
        )

    complete_totals = [t for t in total_rows if t.total is not None]
    winner = None
    if complete_totals and len({t.currency for t in complete_totals}) == 1:
        best = min(complete_totals, key=lambda t: (t.total, str(t.supplier_id)))
        winner = {
            "supplier_id": best.supplier_id,
            "total": _money(best.total, best.currency),
            "is_global_claim": False,
            "claim": "lowest_verified_total_among_evaluated",
        }

    could_be_beaten_by = []
    if winner is not None:
        winner_total = Decimal(winner["total"]["amount"])
        for t in total_rows:
            if (
                t.bound_status == "valid"
                and t.lower_bound is not None
                and t.completeness != "complete"
                and t.currency == winner["total"]["currency"]
                and t.lower_bound < winner_total
            ):
                could_be_beaten_by.append(
                    {
                        "supplier_id": t.supplier_id,
                        "lower_bound": _money(t.lower_bound, t.currency),
                        "blockers": t.blockers,
                    }
                )
    unknown_relation = [
        {"supplier_id": t.supplier_id, "blockers": t.blockers}
        for t in total_rows
        if t.completeness != "complete" and t.bound_status != "valid"
    ]

    annotations, notes = criteria_svc.annotations_for(criteria_rows, criteria_svc.Scope(), now=now)

    payload = {
        "comparison_id": run.id,
        "status": run.status,
        "scope": {
            "suppliers_evaluated": run.suppliers_evaluated,
            "suppliers_complete": run.suppliers_complete,
            "suppliers_incomplete": run.suppliers_incomplete,
            "sources_consulted": run.sources_consulted,
            "sources_failed": [uuid.UUID(x) for x in run.sources_failed],
            "criteria_evaluated_at": run.criteria_evaluated_at,
        },
        "lines": payload_lines,
        "supplier_totals": supplier_payloads,
        "discarded_suppliers": discarded,
        "lowest_verified_total": winner,
        "could_be_beaten_by": could_be_beaten_by,
        "unknown_relation": unknown_relation,
        "recommendation": None,
        "recommendation_status": "not_evaluated",
        "annotations": annotations,
        "notes": notes,
        "sources": [
            sources_svc.source_payload(s, now=now) for s in sorted(source_rows, key=lambda s: s.name)
        ],
        "generated_at": run.generated_at,
    }
    return run, payload
