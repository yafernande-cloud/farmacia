"""Motor puro de interpretación: texto + contexto verificado → Interpretation. Sin base de datos."""

from dataclasses import replace
from decimal import Decimal

from app.domain.products import (
    Basis,
    ParsedProduct,
    apply_alias,
    normalize_name,
    parse_shortage_text,
)
from app.domain.units import fmt
from app.interpretation.ai import AIProposal, sanitize_ai
from app.interpretation.types import (
    AliasRec,
    Candidate,
    CatalogRec,
    CommercialRec,
    Interpretation,
    ItemRec,
    Source,
)


def _contains_tokens(small: str, big: str) -> bool:
    a, b = small.split(), big.split()
    return len(a) < len(b) and any(b[i : i + len(a)] == a for i in range(len(b) - len(a) + 1))


def interpret_with(text: str, **ctx) -> Interpretation:
    """Texto escrito → interpretación (reglas + memoria verificada + candidatos)."""
    return interpret_parsed(parse_shortage_text(text), **ctx)


def interpret_parsed(
    parsed: ParsedProduct,
    *,
    aliases: list[AliasRec],
    catalog: list[CatalogRec],
    items: list[ItemRec],
    commercial: list[CommercialRec] | None = None,
    ai_proposals: list[AIProposal] | None = None,
) -> Interpretation:
    """Contexto verificado → candidatos. Sirve igual para un texto o para lo leído de una foto: las
    mismas reglas deterministas, sin importar el origen."""
    interp = Interpretation(parsed)

    # 1) Alias de expresión verificado: «la D» → «vitamina d». Nombre, no equivalencia clínica.
    for a in aliases:
        if a.kind == "expression" and a.phrase == parsed.ingredient:
            interp.applied.append({"alias_id": str(a.id), "kind": "expression",
                                   "from": parsed.ingredient, "to": a.assignment["ingredient"]})  # fmt: skip
            parsed = replace(parsed, ingredient=a.assignment["ingredient"])
            break

    # 2) Alias de medida verificado: «400» sin unidad = 400 mg SOLO para este producto y cifra.
    mine = [{"source_value": a.source_value, "assignment": a.assignment}
            for a in aliases if a.kind == "measure_unit" and a.phrase == parsed.ingredient]  # fmt: skip
    after = apply_alias(parsed, mine)
    if after is not parsed:
        interp.applied.append({"kind": "measure_unit", "ingredient": parsed.ingredient,
                               "values": [fmt(m.value) for m in parsed.unassigned
                                          if m not in after.unassigned]})  # fmt: skip
    parsed = after
    interp.parsed = parsed

    # 3) Candidatos. Ninguno cambia la procedencia de `parsed`.
    known: dict[str, Source] = {}
    for c in catalog:
        known.setdefault(c.ingredient, Source.CATALOG)
    for it in items:
        known.setdefault(it.parsed.ingredient, Source.CONTEXT)
    for a in aliases:
        if a.kind == "expression":
            known.setdefault(a.assignment["ingredient"], Source.ALIAS)

    pending = [m for m in parsed.unassigned if m.unit is None]
    for m in pending:
        found: dict[str, Candidate] = {}
        for c in catalog:
            if c.ingredient != parsed.ingredient:
                continue
            for s in c.strengths:
                if s.value == m.value and s.unit and s.unit_expr:
                    found[s.unit_expr] = Candidate(
                        "measure_assignment", Source.CATALOG, c.label, True, c.ingredient,
                        {"role": "strength", "unit_expr": s.unit_expr, "value": fmt(m.value)},
                        c.id, ("catalog_identity_verified", "unit_not_written_by_user"),
                    )  # fmt: skip
        for it in items:
            if it.parsed.ingredient != parsed.ingredient:
                continue
            for s in it.parsed.strengths:
                if (
                    s.basis in (Basis.EXPLICIT, Basis.CONFIRMED)
                    and s.measure.value == m.value
                    and s.measure.unit_expr
                ):
                    found.setdefault(s.measure.unit_expr, Candidate(
                        "measure_assignment", Source.CONTEXT, it.parsed.display_name, False, parsed.ingredient,
                        {"role": "strength", "unit_expr": s.measure.unit_expr, "value": fmt(m.value)},
                        it.id, ("already_in_shared_list", "unit_not_written_by_user"),
                    ))  # fmt: skip
        interp.candidates.extend(found.values())
        if len(found) > 1:
            interp.uncertainties.append("multiple_measure_candidates")

    if parsed.ingredient not in known:
        interp.uncertainties.append("ingredient_unrecognized")
        for name, src in sorted(known.items()):
            if _contains_tokens(parsed.ingredient, name):
                interp.candidates.append(Candidate(
                    "ingredient", src, name, src is Source.CATALOG, name,
                    reasons=("name_contained_in_known_product", "not_a_clinical_equivalence"),
                ))  # fmt: skip
        names = [c for c in interp.candidates if c.kind == "ingredient"]
        if len(names) > 1:
            interp.uncertainties.append("multiple_ingredient_candidates")

    # 3b) Referencias comerciales verificadas: marca/laboratorio/GTIN → identidad farmacéutica CANDIDATA.
    interp.candidates.extend(_commercial_candidates(parsed, commercial or []))

    # 4) IA opcional: solo sugiere; se sanea y degrada a candidato no verificado.
    if ai_proposals:
        interp.ai_used = True
        cands, notes = sanitize_ai(ai_proposals, parsed)
        interp.candidates.extend(cands)
        interp.uncertainties.extend(notes)

    for p in parsed.pending_validation:
        if p["reason"] in ("unit_not_stated", "role_not_stated", "inferred_needs_confirmation"):
            interp.uncertainties.append(f"{p['attribute']}:{p['reason']}")
    interp.uncertainties = list(dict.fromkeys(interp.uncertainties))
    return interp


def _lab_tokens(x: str | None) -> set[str]:
    stop = {"laboratorio", "laboratorios", "lab", "de", "chile", "sa", "s", "a", "ltda", "spa"}
    return {t for t in normalize_name(x or "").split() if t not in stop}


def _commercial_candidates(parsed: ParsedProduct, refs: list[CommercialRec]) -> list[Candidate]:
    out: list[Candidate] = []
    brand = normalize_name(parsed.brand.value) if parsed.brand else None
    for r in refs:
        reasons: list[str] = []
        if parsed.gtin and r.gtin and parsed.gtin.value == r.gtin:
            reasons.append("gtin_matches_verified_reference")
        elif brand and brand == r.brand_key:
            reasons.append("brand_matches_verified_reference")
            if parsed.laboratory and r.laboratory:
                a, b = _lab_tokens(parsed.laboratory.value), _lab_tokens(r.laboratory)
                if a and b and not (a & b):
                    continue  # misma marca pero otro laboratorio: no es la misma referencia
                reasons.append("laboratory_matches" if a & b else "laboratory_unconfirmed")
            else:
                reasons.append("laboratory_unconfirmed")
        else:
            continue
        ident = r.identity
        assignment = None
        if ident:
            assignment = {"ingredient": ident.ingredient,
                          "strengths": [{"value": fmt(s.value), "unit_expr": s.unit_expr} for s in ident.strengths],
                          "form": ident.form}  # fmt: skip
        out.append(Candidate("commercial_reference", Source.CATALOG, r.label, True,
                             ident.ingredient if ident else None, assignment, r.id,
                             tuple(reasons + ["not_applied_until_confirmed"])))  # fmt: skip
    return out


def suggest_for(parsed: ParsedProduct, catalog: list[CatalogRec]) -> list[Candidate]:
    """Sugerencias de catálogo para una necesidad con medidas sin unidad (lectura; nunca confirma)."""
    out: dict[tuple, Candidate] = {}
    for m in (x for x in parsed.unassigned if x.unit is None):
        for c in catalog:
            if c.ingredient != parsed.ingredient:
                continue
            for s in c.strengths:
                if s.value == m.value and s.unit_expr:
                    out[(fmt(m.value), s.unit_expr)] = Candidate(
                        "measure_assignment", Source.CATALOG, c.label, True, c.ingredient,
                        {"role": "strength", "unit_expr": s.unit_expr, "value": fmt(m.value)},
                        c.id, ("catalog_identity_verified", "unit_not_written_by_user"),
                    )  # fmt: skip
    return list(out.values())


__all__ = ["interpret_with", "suggest_for", "Decimal"]
