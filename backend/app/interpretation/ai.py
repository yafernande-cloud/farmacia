"""Punto de integración con IA. NO hay modelo conectado: solo la interfaz y su saneamiento.

Regla de oro (HLD §17.3): la IA puede sugerir, no decidir. Todo lo que devuelva se convierte en
`Candidate(source=AI, verified=False)` tras validarlo con las reglas deterministas; nunca modifica
`parsed`, nunca se marca confirmado y no puede asociar solo.
"""

from dataclasses import dataclass
from typing import Protocol

from app.domain.products import ParsedProduct
from app.domain.units import build_measure, fmt
from app.interpretation.types import Candidate, Source

MAX_AI_CANDIDATES = 3


@dataclass(frozen=True)
class AIProposal:
    kind: str  # measure_assignment | ingredient
    ingredient: str | None = None
    unit_expr: str | None = None
    value: str | None = None
    rationale: str = ""
    claims_confirmed: bool = False  # si un modelo dice "confirmado", se ignora


class AIInterpreter(Protocol):
    async def propose(
        self, text: str, parsed: ParsedProduct, known_ingredients: list[str]
    ) -> list[AIProposal]: ...


class NullAIInterpreter:
    """Valor por defecto: sin IA. Todo el flujo funciona sin ella."""

    async def propose(
        self, text: str, parsed: ParsedProduct, known_ingredients: list[str]
    ) -> list[AIProposal]:
        return []


def sanitize_ai(
    proposals: list[AIProposal], parsed: ParsedProduct
) -> tuple[list[Candidate], list[str]]:
    """Valida propuestas de un modelo. Devuelve candidatos no verificados + motivos de descarte."""
    out: list[Candidate] = []
    notes: list[str] = []
    pending_values = {fmt(m.value) for m in parsed.unassigned if m.unit is None}
    for p in proposals[:MAX_AI_CANDIDATES]:
        if p.claims_confirmed:
            notes.append("ai_claimed_confirmation_ignored")
        if p.kind == "measure_assignment":
            try:
                m = build_measure(__import__("decimal").Decimal(p.value or ""), p.unit_expr or "")
            except Exception:  # unidad/valor inválidos: no se acepta
                notes.append("ai_proposal_invalid_unit")
                continue
            if fmt(m.value) not in pending_values:  # no puede inventar cifras que el texto no trae
                notes.append("ai_proposal_not_grounded_in_text")
                continue
            out.append(Candidate("measure_assignment", Source.AI, f"{m.label}", False, parsed.ingredient,
                                 {"role": "strength", "unit_expr": p.unit_expr, "value": p.value},
                                 reasons=("ai_unverified",)))  # fmt: skip
        elif p.kind == "ingredient":
            name = (p.ingredient or "").strip().lower()
            if not name or len(name) > 200 or name == parsed.ingredient:
                notes.append("ai_proposal_invalid_ingredient")
                continue
            out.append(
                Candidate("ingredient", Source.AI, name, False, name, reasons=("ai_unverified",))
            )
        else:
            notes.append("ai_proposal_unknown_kind")
    return out, notes
