"""Motor determinista de preferencias comerciales (O3/O4, A6, D-136/D-138/D-142/D-144).

Las preferencias son **del producto exacto** (`trade_item_id`). Este módulo solo **ordena** las
ofertas que E3/E5 ya recuperaron: no quita ni agrega ofertas, no cambia precios, stock,
cantidades, descuentos, impuestos, totales ni elegibilidad, y no llama a ningún modelo.

Reglas aplicadas (§8.4 del contrato rev10-A6):

* Precedencia por clase: **solo** las interpretaciones aplicadas (`suggested`/`confirmed`) compiten
  por su clase; la más reciente gana y la anterior queda en el historial. Una interpretación
  `rejected`, `needs_review` o `no_preference` no aplica, no reactiva una anterior **ni desactiva
  la preferencia vigente**; un intento `failed` no es semántico y no desplaza nada.
* Retiro explícito (D-144): una interpretación aplicada puede retirar una clase mediante
  `retired_subject_kinds`. El retiro compite en la misma secuencia temporal que las preferencias de
  la clase; no se infiere de una ausencia ni se simula con preferencias ficticias.
* Orden lexicográfico: precio → proveedor → laboratorio → marca → disponibilidad. Dentro de cada
  clase el grupo que cumple va primero; los empates conservan el orden determinista anterior.
* Tolerancia (solo laboratorio, marca y proveedor con `prefer`): una oferta que no cumple puede
  quedar en el grupo que cumple si su precio está dentro de la tolerancia relativa o absoluta,
  comparado con la oferta más barata que sí cumple, en la misma moneda y base de precio.
* `maximum`/`acceptable` solo ordenan: un precio que excede la referencia queda después, nunca
  se oculta ni se vuelve inelegible.
* La traza (`preference_order_trace`) es interna: no se devuelve en la API pública.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.products import normalize_name
from app.models import (
    CommercialPreference,
    ObservationInterpretation,
    PreferenceOrderTrace,
)

logger = logging.getLogger(__name__)

APPLIED_STATUSES = frozenset({"suggested", "confirmed"})
# Estado que no es una interpretación semántica: no compite por la precedencia de su clase.
NON_SEMANTIC_STATUSES = frozenset({"failed"})

ZERO_RANKS: tuple[int, ...] = (0, 0, 0, 0, 0, 0)
Q4 = Decimal("0.0001")
HUNDRED = Decimal("100")

# Clases en el orden exacto de precedencia. El precio distingue aceptable de máximo (§8.4.3).
CLASS_ORDER: tuple[str, ...] = (
    "price:acceptable",
    "price:maximum",
    "supplier",
    "laboratory",
    "brand",
    "availability",
)


@dataclass(frozen=True)
class OfferView:
    """Lo único que el motor necesita de una oferta. Nada se infiere: NULL sigue siendo NULL."""

    offer_id: uuid.UUID
    supplier_id: uuid.UUID
    laboratory: str | None
    brand: str | None
    currency: str
    price_amount: Decimal
    price_basis: str
    pack_units_per_commercial_unit: Decimal | None
    stock_status: str
    stock_quantity: Decimal | None
    stock_unit: str | None
    sale_unit: str
    line_payable: Decimal | None = None


def offer_view(offer: Any, *, line_payable: Decimal | None = None) -> OfferView:
    """`OfferSnapshot` → vista mínima. `line_payable` solo existe en E5 (total de la línea)."""
    return OfferView(
        offer_id=offer.id,
        supplier_id=offer.supplier_id,
        laboratory=offer.laboratory_as_published,
        brand=offer.brand_as_published,
        currency=offer.currency,
        price_amount=offer.price_amount,
        price_basis=offer.price_basis,
        pack_units_per_commercial_unit=offer.pack_units_per_commercial_unit,
        stock_status=offer.stock_status,
        stock_quantity=offer.stock_quantity,
        stock_unit=offer.stock_unit,
        sale_unit=offer.sale_unit,
        line_payable=line_payable,
    )


@dataclass(frozen=True)
class ItemPreferences:
    """Preferencias vigentes y retiros de UN artículo, ya resueltos por precedencia.

    `interpretations` son las interpretaciones semánticas **consultadas** para el artículo
    (incluidas rechazadas, `needs_review` y `no_preference`, que no aplican). `applicable` son
    únicamente las filas vigentes y `retired_subject_kinds` las clases retiradas explícitamente; la
    traza distingue ambas cosas.
    """

    trade_item_id: uuid.UUID | None
    applicable: tuple[CommercialPreference, ...] = ()
    interpretations: tuple[ObservationInterpretation, ...] = ()
    retired_subject_kinds: tuple[str, ...] = ()

    @property
    def has_preferences(self) -> bool:
        return bool(self.applicable)

    @property
    def has_effects(self) -> bool:
        """Hay algo que aplicar o auditar: preferencias vigentes o retiros explícitos."""
        return bool(self.applicable or self.retired_subject_kinds)

    @property
    def interpretation_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(interp.id for interp in self.interpretations)

    def interpretation(self, interpretation_id: uuid.UUID) -> ObservationInterpretation | None:
        return next((i for i in self.interpretations if i.id == interpretation_id), None)


EMPTY = ItemPreferences(trade_item_id=None)


def _class_of(pref: CommercialPreference) -> str:
    if pref.subject_kind == "price":
        return f"price:{pref.price_reference_kind}"
    return pref.subject_kind


def _winner_rows(
    rows: Sequence[CommercialPreference],
    interpretations: dict[uuid.UUID, ObservationInterpretation],
    item_id: uuid.UUID,
) -> tuple[list[CommercialPreference], list[str]]:
    """Resuelve, por clase, el último evento aplicado: preferencia o retiro explícito.

    Solo participan interpretaciones y filas en `suggested`/`confirmed`. Una interpretación
    rechazada, ambigua (`needs_review`) o sin preferencia (`no_preference`) no se adscribe a
    ninguna clase y, por tanto, **no desactiva** la preferencia válida anterior ni un retiro.
    La clase se conoce por las filas producidas o por `retired_subject_kinds`; si una misma
    interpretación retira y establece la misma clase, prevalece el retiro.
    """
    retired_events: dict[str, list[uuid.UUID]] = {}
    for interp in interpretations.values():
        if interp.trade_item_id != item_id or interp.status not in APPLIED_STATUSES:
            continue
        for cls in interp.retired_subject_kinds or ():
            if cls in CLASS_ORDER:
                retired_events.setdefault(cls, []).append(interp.id)

    set_groups: dict[str, dict[uuid.UUID, list[CommercialPreference]]] = {}
    for pref in rows:
        interp = interpretations.get(pref.interpretation_id)
        if interp is None or interp.status not in APPLIED_STATUSES:
            continue
        if pref.status not in APPLIED_STATUSES:
            continue
        cls = _class_of(pref)
        if cls in (interp.retired_subject_kinds or ()):
            continue  # contradicción en la misma interpretación: el retiro manda
        set_groups.setdefault(cls, {}).setdefault(pref.interpretation_id, []).append(pref)

    winners: list[CommercialPreference] = []
    retired: list[str] = []
    for cls in CLASS_ORDER:
        events: list[tuple[tuple[datetime, str], str, uuid.UUID]] = []
        for interpretation_id in set_groups.get(cls, {}):
            interp = interpretations[interpretation_id]
            events.append(((interp.created_at, str(interpretation_id)), "set", interpretation_id))
        for interpretation_id in retired_events.get(cls, ()):
            interp = interpretations[interpretation_id]
            events.append(
                ((interp.created_at, str(interpretation_id)), "retire", interpretation_id)
            )
        if not events:
            continue
        # `created_at` y `id`: el evento aplicado más reciente de la clase gana; el anterior es
        # historial. Un retiro posterior gana; una preferencia posterior vuelve a establecerla.
        _, action, interpretation_id = max(events, key=lambda event: event[0])
        if action == "retire":
            retired.append(cls)
        else:
            winners.extend(set_groups[cls][interpretation_id])
    winners.sort(key=lambda p: (CLASS_ORDER.index(_class_of(p)), str(p.id)))
    return winners, retired


async def load_item_preferences(
    session: AsyncSession,
    organization_id: uuid.UUID,
    item_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, ItemPreferences]:
    """Carga interpretaciones y preferencias de varios artículos en **dos** consultas.

    El motor nunca busca por nombre, marca ni laboratorio parecido: solo por `trade_item_id`
    exacto de la organización.
    """
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        return {}
    interpretations = {
        i.id: i
        for i in (
            await session.execute(
                select(ObservationInterpretation).where(
                    ObservationInterpretation.organization_id == organization_id,
                    ObservationInterpretation.trade_item_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    }
    prefs = (
        (
            await session.execute(
                select(CommercialPreference).where(
                    CommercialPreference.organization_id == organization_id,
                    CommercialPreference.trade_item_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[CommercialPreference]] = {}
    for pref in prefs:
        grouped.setdefault(pref.trade_item_id, []).append(pref)
    consulted: dict[uuid.UUID, list[ObservationInterpretation]] = {}
    for interp in interpretations.values():
        if interp.status in NON_SEMANTIC_STATUSES:
            continue
        consulted.setdefault(interp.trade_item_id, []).append(interp)
    for rows in consulted.values():
        rows.sort(key=lambda i: (i.created_at, str(i.id)))

    out: dict[uuid.UUID, ItemPreferences] = {}
    for item_id in ids:
        winners, retired = _winner_rows(grouped.get(item_id, []), interpretations, item_id)
        out[item_id] = ItemPreferences(
            trade_item_id=item_id,
            applicable=tuple(winners),
            interpretations=tuple(consulted.get(item_id, [])),
            retired_subject_kinds=tuple(retired),
        )
    return out


# --------------------------------------------------------------------------- orden


def _price_comparable(
    pref: CommercialPreference,
    view: OfferView,
    *,
    quantity: Decimal | None,
    unit: str | None,
) -> Decimal | None:
    """Precio comparable o `None`. Nunca mezcla monedas, bases ni unidad con total de línea."""
    if view.currency != pref.price_reference_currency:
        return None
    basis = pref.price_reference_basis
    if basis == "per_commercial_unit":
        return view.price_amount if view.price_basis == "per_commercial_unit" else None
    if basis == "per_base_unit":
        if view.price_basis == "per_base_unit":
            return view.price_amount
        pack = view.pack_units_per_commercial_unit
        if view.price_basis == "per_commercial_unit" and pack and pack > 0:
            return (view.price_amount / pack).quantize(Q4, rounding=ROUND_HALF_UP)
        return None
    if basis == "per_line_total":
        if quantity is None or unit is None:
            return None
        if quantity != pref.price_reference_quantity or unit != pref.price_reference_quantity_unit:
            return None
        return view.line_payable
    return None


def _price_rank(
    group: Sequence[CommercialPreference],
    view: OfferView,
    *,
    quantity: Decimal | None,
    unit: str | None,
) -> int:
    comparable = [
        (pref, _price_comparable(pref, view, quantity=quantity, unit=unit)) for pref in group
    ]
    known = [(pref, value) for pref, value in comparable if value is not None]
    if not known:
        return 0  # no se puede afirmar nada: no se penaliza
    return 0 if any(value <= pref.price_reference_amount for pref, value in known) else 1


def _published(view: OfferView, kind: str) -> str | None:
    return view.laboratory if kind == "laboratory" else view.brand


def _matches(pref: CommercialPreference, view: OfferView) -> bool:
    if pref.subject_kind == "supplier":
        return pref.subject_supplier_id is not None and view.supplier_id == pref.subject_supplier_id
    published = _published(view, pref.subject_kind)
    if not published:
        return False
    return normalize_name(published) == normalize_name(pref.subject_key)


def _in_stock(view: OfferView) -> bool:
    """Disponibilidad informada: desconocida no es cero ni stock seguro."""
    if view.stock_status != "known" or view.stock_quantity is None:
        return False
    if view.stock_quantity <= 0:
        return False
    return view.stock_unit is None or view.stock_unit == view.sale_unit


def _within_tolerance(
    pref: CommercialPreference,
    view: OfferView,
    baselines: dict[tuple[str, str], Decimal],
) -> bool:
    """¿Una oferta que no cumple está lo bastante cerca del cumplimiento más barato?"""
    if pref.effect != "prefer" or pref.tolerance_kind == "none":
        return False
    baseline = baselines.get((view.currency, view.price_basis))
    if baseline is None:
        return False
    if pref.tolerance_kind == "relative_percent" and pref.tolerance_percent is not None:
        limit = baseline * (Decimal("1") + pref.tolerance_percent / HUNDRED)
        return view.price_amount <= limit
    if (
        pref.tolerance_kind == "absolute_amount"
        and pref.tolerance_amount is not None
        and pref.tolerance_currency == view.currency
    ):
        return view.price_amount <= baseline + pref.tolerance_amount
    return False


def _tolerance_baselines(
    group: Sequence[CommercialPreference], views: Sequence[OfferView]
) -> dict[tuple[str, str], Decimal]:
    """Precio más bajo que cumple, por moneda y base. Solo con tolerancia que lo necesite."""
    needs = any(p.effect == "prefer" and p.tolerance_kind != "none" for p in group)
    if not needs:
        return {}
    baselines: dict[tuple[str, str], Decimal] = {}
    for view in views:
        if not any(_matches(pref, view) for pref in group):
            continue
        key = (view.currency, view.price_basis)
        current = baselines.get(key)
        if current is None or view.price_amount < current:
            baselines[key] = view.price_amount
    return baselines


def _attribute_rank(
    group: Sequence[CommercialPreference], views: Sequence[OfferView]
) -> dict[uuid.UUID, int]:
    prefer = [p for p in group if p.effect == "prefer"]
    baselines = _tolerance_baselines(group, views)
    ranks: dict[uuid.UUID, int] = {}
    for view in views:
        if prefer:
            satisfied = any(
                _matches(pref, view) or _within_tolerance(pref, view, baselines) for pref in prefer
            )
            ranks[view.offer_id] = 0 if satisfied else 1
        else:
            ranks[view.offer_id] = 1 if any(_matches(p, view) for p in group) else 0
    return ranks


def _availability_rank(
    group: Sequence[CommercialPreference], views: Sequence[OfferView]
) -> dict[uuid.UUID, int]:
    return {view.offer_id: 0 if _in_stock(view) else 1 for view in views}


def rank_offers(
    prefs: ItemPreferences,
    views: Sequence[OfferView],
    *,
    quantity: Decimal | None = None,
    unit: str | None = None,
) -> dict[uuid.UUID, tuple[int, ...]]:
    """Clave de orden por oferta. Sin preferencias aplicables, todas las claves son cero."""
    if not prefs.has_preferences or not views:
        return {view.offer_id: ZERO_RANKS for view in views}
    ranks: dict[uuid.UUID, list[int]] = {view.offer_id: [] for view in views}
    for cls in CLASS_ORDER:
        group = [p for p in prefs.applicable if _class_of(p) == cls]
        if not group:
            table = {view.offer_id: 0 for view in views}
        elif cls.startswith("price:"):
            table = {
                view.offer_id: _price_rank(group, view, quantity=quantity, unit=unit)
                for view in views
            }
        elif cls == "availability":
            table = _availability_rank(group, views)
        else:
            table = _attribute_rank(group, views)
        for view in views:
            ranks[view.offer_id].append(table[view.offer_id])
    return {offer_id: tuple(values) for offer_id, values in ranks.items()}


# --------------------------------------------------------------------------- traza interna


def _descriptor(
    pref: CommercialPreference, interp: ObservationInterpretation | None
) -> dict[str, Any]:
    """Representación estable de una preferencia aplicada (sin texto del proveedor).

    Incluye el estado, el modelo y la versión del prompt de la interpretación **en el momento de
    aplicar**, para que la traza sea autosuficiente sin migrar el esquema (D-143).
    """
    out: dict[str, Any] = {
        "preference_id": str(pref.id),
        "interpretation_id": str(pref.interpretation_id),
        "interpretation_status": interp.status if interp is not None else None,
        "observation_id": str(interp.observation_id) if interp is not None else None,
        "model": interp.model if interp is not None else None,
        "prompt_version": interp.prompt_version if interp is not None else None,
        "preference_status": pref.status,
        "subject_kind": pref.subject_kind,
        "effect": pref.effect,
        "subject_key": pref.subject_key,
        "subject_supplier_id": (
            str(pref.subject_supplier_id) if pref.subject_supplier_id is not None else None
        ),
        "tolerance_kind": pref.tolerance_kind,
        "tolerance_origin": pref.tolerance_origin,
        "tolerance_percent": (
            str(pref.tolerance_percent) if pref.tolerance_percent is not None else None
        ),
        "tolerance_amount": (
            str(pref.tolerance_amount) if pref.tolerance_amount is not None else None
        ),
        "tolerance_currency": pref.tolerance_currency,
        "price_reference_kind": pref.price_reference_kind,
        "price_reference_amount": (
            str(pref.price_reference_amount) if pref.price_reference_amount is not None else None
        ),
        "price_reference_currency": pref.price_reference_currency,
        "price_reference_basis": pref.price_reference_basis,
        "price_reference_quantity": (
            str(pref.price_reference_quantity)
            if pref.price_reference_quantity is not None
            else None
        ),
        "price_reference_quantity_unit": pref.price_reference_quantity_unit,
    }
    return out


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def record_trace(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    trade_item_id: uuid.UUID,
    context: str,
    prefs: ItemPreferences,
    offer_ids_before: Sequence[uuid.UUID],
    offer_ids_after: Sequence[uuid.UUID],
    now: datetime,
) -> bool:
    """Registra una fila de auditoría interna. Repetir el mismo estado no duplica (AC31/D-141).

    Se escribe cuando hay preferencias vigentes **o** retiros explícitos: ambos son efectos del
    motor sobre el orden (o sobre la ausencia de él) y deben quedar auditados (D-144). Sin ninguno
    de los dos no ocurrió ninguna aplicación y no hay fila. Devuelve `True` únicamente si insertó
    una fila; un conflicto de idempotencia devuelve `False`.
    """
    if not prefs.has_effects:
        return False
    before = [str(x) for x in offer_ids_before]
    after = [str(x) for x in offer_ids_after]
    offer_set_hash = _digest(sorted(set(before) | set(after)))
    applied = [
        _descriptor(pref, prefs.interpretation(pref.interpretation_id)) for pref in prefs.applicable
    ]
    retired = list(prefs.retired_subject_kinds)
    # El retiro también forma parte del estado: dos lecturas con las mismas preferencias vigentes
    # pero distinto retiro no son el mismo orden.
    fingerprint = _digest({"applied": applied, "retired": retired})
    stmt = (
        pg_insert(PreferenceOrderTrace)
        .values(
            organization_id=organization_id,
            trade_item_id=trade_item_id,
            context=context,
            # Consultadas (incluye las que no aplican) frente a `applied_preferences` (vigentes).
            interpretation_ids=[str(x) for x in prefs.interpretation_ids],
            preference_ids=[str(pref.id) for pref in prefs.applicable],
            applied_preferences=applied,
            retired_subject_kinds=retired,
            offer_ids_before=before,
            offer_ids_after=after,
            offer_set_hash=offer_set_hash,
            preference_fingerprint=fingerprint,
            created_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_preference_order_trace_estado")
        .returning(PreferenceOrderTrace.id)
    )
    inserted = (await session.execute(stmt)).first()
    return inserted is not None


async def record_trace_best_effort(session: AsyncSession, **kwargs: Any) -> bool:
    """Traza para una lectura: un fallo de auditoría no interrumpe la consulta (D-143).

    La inserción corre en un savepoint; si falla, se revierte solo la traza y se registra el
    incidente internamente (sin texto de la observación ni datos de proveedor). No hay colas ni
    reintentos: la siguiente lectura vuelve a intentar la auditoría.
    """
    try:
        async with session.begin_nested():
            return await record_trace(session, **kwargs)
    except SQLAlchemyError as exc:
        logger.warning(
            "Traza de preferencias no registrada (contexto=%s, error=%s); la lectura continúa",
            kwargs.get("context"),
            type(exc).__name__,
        )
        return False
