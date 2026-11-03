"""Vistas de observaciones, interpretaciones y preferencias para la API (O1–O4).

Solo transforman filas ya persistidas. No interpretan, no llaman al proveedor y no exponen
`raw_output`, prompts, tokens, latencia ni la confianza del modelo: el frontend ve el estado y la
preferencia vigente, no una explicación ni una puntuación (AC31/D-141).
"""

from __future__ import annotations

import uuid
from typing import Any

from app.models import CommercialObservation, CommercialPreference, ObservationInterpretation


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def preference_view(pref: CommercialPreference) -> dict[str, Any]:
    tolerance = None
    if pref.tolerance_kind != "none":
        tolerance = {
            "kind": pref.tolerance_kind,
            "percent": _text(pref.tolerance_percent),
            "amount": _text(pref.tolerance_amount),
            "currency": pref.tolerance_currency,
        }
    price_reference = None
    if pref.price_reference_kind != "none":
        price_reference = {
            "kind": pref.price_reference_kind,
            "amount": _text(pref.price_reference_amount),
            "currency": pref.price_reference_currency,
            "basis": pref.price_reference_basis,
            "quantity": _text(pref.price_reference_quantity),
            "quantity_unit": pref.price_reference_quantity_unit,
        }
    return {
        "preference_id": pref.id,
        "subject_kind": pref.subject_kind,
        "subject_label": pref.subject_label,
        "effect": pref.effect,
        "tolerance": tolerance,
        "price_reference": price_reference,
    }


def interpretation_view(
    interpretation: ObservationInterpretation,
    prefs: dict[uuid.UUID, tuple[CommercialPreference, ...]],
) -> dict[str, Any]:
    return {
        "interpretation_id": interpretation.id,
        "status": interpretation.status,
        # Código interno cerrado (`timeout`, `ambiguous`, …); nunca prompts ni razonamiento.
        "error_code": interpretation.error_code,
        "retired_subject_kinds": list(interpretation.retired_subject_kinds or ()),
        "created_at": interpretation.created_at,
        "preferences": [
            preference_view(pref) for pref in prefs.get(interpretation.id, ())
        ],
    }


def observation_view(
    row: CommercialObservation,
    latest: ObservationInterpretation | None,
    prefs: dict[uuid.UUID, tuple[CommercialPreference, ...]],
    *,
    history: tuple[ObservationInterpretation, ...] = (),
) -> dict[str, Any]:
    """Forma O1 + la interpretación más reciente y, si se pide, todos los intentos."""
    attempts = history or ((latest,) if latest is not None else ())
    return {
        "observation_id": row.id,
        "text": row.text_original,
        "trade_item_id": row.trade_item_id,
        "supplier_id": row.supplier_id,
        "offer_snapshot_id": row.offer_snapshot_id,
        "quote_id": row.quote_id,
        "quote_line_id": row.quote_line_id,
        "author_id": row.author_id,
        "created_at": row.created_at,
        "context": row.context or {},
        "data_origin": row.data_origin,
        "interpretation_status": latest.status if latest is not None else "sin_interpretacion",
        "interpretation": (
            interpretation_view(latest, prefs) if latest is not None else None
        ),
        "interpretations": [
            interpretation_view(interpretation, prefs) for interpretation in attempts
        ],
    }
