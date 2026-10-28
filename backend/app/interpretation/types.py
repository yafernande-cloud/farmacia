import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.products import ParsedProduct
from app.domain.units import Measure


class Source(StrEnum):
    RULES = "rules"  # reglas deterministas sobre el texto
    ALIAS = "alias"  # memoria verificada por una persona (puede asociar sola)
    CATALOG = "catalog"  # identidad farmacéutica verificada (solo sugiere)
    CONTEXT = "context"  # lo que ya hay en la lista compartida (solo sugiere)
    AI = "ai"  # modelo de IA (solo sugiere; jamás verificado)


@dataclass(frozen=True)
class AliasRec:
    id: uuid.UUID
    kind: str  # measure_unit | expression
    phrase: str  # ingrediente tal como lo escribe el auxiliar
    source_value: str  # cifra (measure_unit); '' en expression
    assignment: dict
    confirmed_attributes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogRec:
    id: uuid.UUID
    label: str
    ingredient: str
    strengths: tuple[Measure, ...]
    form: str | None = None
    release: str | None = None


@dataclass(frozen=True)
class CommercialRec:
    """Referencia comercial verificada (embrión del catálogo comercial)."""

    id: uuid.UUID
    label: str
    brand_key: str
    laboratory: str | None = None
    gtin: str | None = None
    sanitary_registration: str | None = None
    identity: CatalogRec | None = None  # identidad farmacéutica verificada, si se conoce


@dataclass(frozen=True)
class ItemRec:
    id: uuid.UUID
    parsed: ParsedProduct


@dataclass(frozen=True)
class Candidate:
    """Una interpretación posible. NUNCA es una confirmación."""

    kind: str  # measure_assignment | ingredient
    source: Source
    label: str
    verified: bool  # respaldada por catálogo/alias de una persona; jamás True si source=AI
    ingredient: str | None = None
    assignment: dict | None = None  # {"role": "strength", "unit_expr": "ui", "value": "50000"}
    ref_id: uuid.UUID | None = None  # catálogo o alias de origen
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "source": self.source.value, "label": self.label,
             "verified": self.verified, "ingredient": self.ingredient, "assignment": self.assignment,
             "ref_id": str(self.ref_id) if self.ref_id else None, "reasons": list(self.reasons)}  # fmt: skip
        return {k: v for k, v in d.items() if v is not None and v != []} | {"kind": self.kind}


@dataclass
class Interpretation:
    parsed: ParsedProduct
    candidates: list[Candidate] = field(default_factory=list)
    applied: list[dict] = field(
        default_factory=list
    )  # qué alias verificados modificaron el resultado
    uncertainties: list[str] = field(default_factory=list)
    ai_used: bool = False

    @property
    def unrecognized_name_with_candidates(self) -> bool:
        return "ingredient_unrecognized" in self.uncertainties and any(
            c.kind == "ingredient" for c in self.candidates
        )

    def snapshot(self) -> dict:
        return {
            "version": "interp-1", "applied": self.applied, "ai_used": self.ai_used,
            "uncertainties": self.uncertainties, "candidates": [c.to_dict() for c in self.candidates],
        }  # fmt: skip
