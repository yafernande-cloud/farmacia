"""Clasificación comercial entre lo buscado y lo encontrado (contrato v0.2 §12.1).

`relation` describe la relación entre la identidad buscada y la del artículo; **no** es un puntaje
ni una equivalencia farmacológica. `match.level` conserva la forma de C0 (v0 no tiene equivalencias
autorizadas: lo que no es exacto queda `needs_review`).

Reglas que se respetan aquí:
  * una cifra sin unidad no se asume mg: si falta la potencia, no hay identidad suficiente;
  * un combinado se reconoce solo por `attributes.ingredients[]`; si falta ese dato, `needs_review`;
  * el nombre comercial de un artículo no se convierte en «principio activo»;
  * lo incompleto se declara incompleto, nunca se completa por suposición.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.products import ParsedProduct, normalize_name
from app.domain.units import Measure

RELATION_RANK = {
    "exact": 0,
    "other_presentation": 1,
    "combined": 2,
    "related": 3,
    "needs_review": 4,
}


@dataclass(frozen=True)
class Identity:
    """Identidad comparable: principio(s), potencia(s), forma y contenido, ya normalizados."""

    ingredient: str | None = None
    ingredients: frozenset[str] = frozenset()
    strengths: frozenset[str] = frozenset()
    form: str | None = None
    content: str | None = None
    brand: str | None = None
    laboratory: str | None = None

    @property
    def all_ingredients(self) -> frozenset[str]:
        if self.ingredients:
            return self.ingredients
        return frozenset({self.ingredient}) if self.ingredient else frozenset()


def _measure_key(measure: dict[str, Any] | None) -> str | None:
    """Clave normalizada (`dimension:valor unidad`); None si la medida no se puede normalizar."""
    if not isinstance(measure, dict):
        return None
    try:
        m = Measure.from_dict(measure)
    except (KeyError, ValueError, InvalidOperation, TypeError):
        return None
    n = m.normalized()
    return n.key if n is not None else None


def _measure_from_attr(attr: Any) -> str | None:
    if attr is None:
        return None
    n = attr.measure.normalized()
    return n.key if n is not None else None


def identity_from_query(parsed: ParsedProduct) -> Identity:
    """Lo que la consulta declare. Lo que no declare queda en None (no se completa)."""
    content = None
    if parsed.content is not None and isinstance(parsed.content.measure.value, Decimal):
        content = _measure_from_attr(parsed.content)
    strengths = frozenset(
        k for k in (_measure_from_attr(s) for s in parsed.strengths) if k is not None
    )
    return Identity(
        ingredient=normalize_name(parsed.ingredient) or None,
        strengths=strengths,
        form=normalize_name(parsed.form.value) or None if parsed.form else None,
        content=content,
        brand=normalize_name(parsed.brand.value) or None if parsed.brand else None,
        laboratory=normalize_name(parsed.laboratory.value) or None if parsed.laboratory else None,
    )


def identity_from_item(
    *, ingredient: str | None, brand: str | None, laboratory: str | None, attributes: dict
) -> Identity:
    """La identidad que el artículo canónico declara en `attributes` (esquema v2)."""
    attrs = attributes if isinstance(attributes, dict) else {}
    raw_ingredient = attrs.get("ingredient") or ingredient
    ingredients = frozenset(
        normalize_name(x)
        for x in (attrs.get("ingredients") or [])
        if isinstance(x, str) and x.strip()
    )
    strengths = frozenset(
        k
        for k in (
            _measure_key(s.get("measure")) for s in (attrs.get("strengths") or []) if isinstance(s, dict)
        )
        if k is not None
    )  # fmt: skip
    content_attr = attrs.get("content")
    content = (
        _measure_key(content_attr.get("measure"))
        if isinstance(content_attr, dict)
        else None
    )
    form_attr = attrs.get("form")
    form = (
        normalize_name(form_attr.get("value"))
        if isinstance(form_attr, dict) and isinstance(form_attr.get("value"), str)
        else None
    )
    return Identity(
        ingredient=normalize_name(raw_ingredient) or None if isinstance(raw_ingredient, str) else None,
        ingredients=ingredients,
        strengths=strengths,
        form=form or None,
        content=content,
        brand=normalize_name(brand) or None if brand else None,
        laboratory=normalize_name(laboratory) or None if laboratory else None,
    )


def classify(query: Identity, item: Identity) -> tuple[str, list[str]]:
    """Devuelve (`relation`, causas). Las causas explican el resultado; nunca lo maquillan."""
    q_ings, item_ings = query.all_ingredients, item.all_ingredients
    if not q_ings:
        return "needs_review", ["query_without_ingredient"]
    if not item_ings:
        return "needs_review", ["item_without_ingredient"]
    if q_ings - item_ings:
        return "related", ["different_ingredient"]
    if item_ings - q_ings:
        # El artículo declara más principios activos que lo buscado: es un combinado, no un equivalente.
        return "combined", ["combined_ingredients"]
    if not query.strengths or not item.strengths:
        return "needs_review", ["strength_unknown"]
    if query.strengths != item.strengths:
        return "related", ["different_strength"]
    if query.form and item.form and query.form != item.form:
        return "related", ["different_form"]
    if not query.form or not item.form:
        return "needs_review", ["form_unknown"]
    if not query.content or not item.content:
        return "needs_review", ["content_unknown"]
    if query.content == item.content:
        return "exact", ["same_identity"]
    return "other_presentation", ["same_identity", "different_content"]


def match_level(relation: str) -> str:
    """C0 conserva su forma: v0 no tiene equivalencias autorizadas configuradas."""
    return "exact" if relation == "exact" else "needs_review"
