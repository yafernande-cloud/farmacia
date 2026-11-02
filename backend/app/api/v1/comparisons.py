"""E5 — comparación de ofertas; E9 — criterios comerciales y notas (C3).

Todas las operaciones son de `admin` + `StrongActor` (D-130): el cotizador es una herramienta
interna. La comparación se calcula con las fórmulas del contrato (C0 §2.4, §3.4 y §E5): aquí no hay
IA y ningún criterio comercial altera un número (D-091).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Session, StrongActor
from app.api.headers import parse_if_match
from app.api.v1.demand import ERRORS
from app.api.v1.search import MoneyOut
from app.api.v1.sources import SourceOut
from app.core.errors import DomainError
from app.db.session import transaction
from app.services import comparison as comparison_svc
from app.services import criteria as criteria_svc

router = APIRouter(tags=["comparación"])

Unit = Literal["box", "package", "unit", "bottle"]


# --------------------------------------------------------------------------- E5: entrada


class LineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_id: uuid.UUID
    quantity: dict[str, Any] = Field(
        examples=[{"value": "20", "unit": "box"}],
        description="`value` entero > 0 (texto o número) y `unit` ∈ box|package|unit|bottle.",
    )


class ComparisonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[LineIn] = Field(min_length=1, max_length=comparison_svc.MAX_LINES)
    # Pista opcional a nivel de la comparación: no omite validaciones ni verificación de frescura.
    source_search_id: uuid.UUID | None = None


def _line_inputs(body: ComparisonIn) -> list[comparison_svc.LineInput]:
    lines: list[comparison_svc.LineInput] = []
    for raw in body.lines:
        quantity = raw.quantity or {}
        value = _decimal(quantity.get("value"))
        unit = quantity.get("unit")
        if value is None or unit not in ("box", "package", "unit", "bottle"):
            raise DomainError(
                "INVALID_QUANTITY",
                "Cada línea exige `quantity.value` y `quantity.unit` válidos.",
                status_code=422,
            )
        lines.append(comparison_svc.LineInput(article_id=raw.article_id, value=value, unit=unit))
    return lines


def _decimal(raw: Any) -> Decimal | None:
    try:
        return Decimal(str(raw)) if raw is not None else None
    except (ArithmeticError, ValueError):
        return None


# --------------------------------------------------------------------------- E5: salida


class ComparisonScopeOut(BaseModel):
    suppliers_evaluated: int
    suppliers_complete: int
    suppliers_incomplete: int
    sources_consulted: int
    sources_failed: list[uuid.UUID]
    criteria_evaluated_at: datetime


class CandidateOut(BaseModel):
    candidate_id: uuid.UUID
    line_id: uuid.UUID
    offer_id: uuid.UUID
    article_id: uuid.UUID
    supplier: dict[str, Any]
    offer_price: dict[str, Any]
    quantity: dict[str, Any]
    line_gross: MoneyOut | None
    line_discounts: list[dict[str, Any]]
    line_tax_added: MoneyOut | None
    line_payable: MoneyOut | None
    line_net: MoneyOut | None
    eligibility: Literal["eligible", "needs_review", "ineligible"]
    causes: list[str]
    stock_status: str
    stock_sufficient: bool | None
    shipping_terms_id: uuid.UUID | None
    discounted_is_possible: bool
    annotations: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    provenance: dict[str, Any]
    freshness: dict[str, Any]
    computed_at: datetime


class ComparisonLineOut(BaseModel):
    line_id: uuid.UUID
    article_id: uuid.UUID
    quantity: dict[str, Any]
    candidates: list[CandidateOut]
    completeness: Literal["complete", "partial", "unavailable"]
    blockers: list[str]


class ShippingOut(BaseModel):
    scheme: str | None
    known: bool
    amount: MoneyOut | None
    terms_id: uuid.UUID | None


class SupplierTotalOut(BaseModel):
    """Un proveedor **solo** tiene `total` con `completeness=complete`; parcial nunca (AC19)."""

    supplier_id: uuid.UUID
    supplier_name: str
    supplier_status: str
    currency: str | None
    completeness: Literal["complete", "partial"]
    lines_covered: list[uuid.UUID]
    lines_missing: list[uuid.UUID]
    subtotal: MoneyOut | None
    taxes_included: bool | None
    taxes_added: MoneyOut | None
    discounts_total: MoneyOut | None
    shipping: ShippingOut
    other_charges: list[dict[str, Any]]
    total: MoneyOut | None
    lower_bound: MoneyOut | None
    bound_status: Literal["valid", "not_boundable"]
    bound_invalid_reasons: list[str]
    blockers: list[str]
    causes: list[str]
    annotations: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    freshness: dict[str, Any]
    computed_at: datetime


class DiscardedSupplierOut(BaseModel):
    supplier_id: uuid.UUID
    supplier_name: str
    causes: list[str]


class LowestVerifiedOut(BaseModel):
    supplier_id: uuid.UUID
    total: MoneyOut
    is_global_claim: bool
    claim: str


class BeatenByOut(BaseModel):
    supplier_id: uuid.UUID
    lower_bound: MoneyOut
    blockers: list[str]


class UnknownRelationOut(BaseModel):
    supplier_id: uuid.UUID
    blockers: list[str]


class ComparisonOut(BaseModel):
    comparison_id: uuid.UUID
    status: Literal["complete", "partial_coverage", "no_complete_total", "failed"]
    scope: ComparisonScopeOut
    lines: list[ComparisonLineOut]
    supplier_totals: list[SupplierTotalOut]
    discarded_suppliers: list[DiscardedSupplierOut]
    lowest_verified_total: LowestVerifiedOut | None
    could_be_beaten_by: list[BeatenByOut]
    unknown_relation: list[UnknownRelationOut]
    recommendation: dict[str, Any] | None
    recommendation_status: Literal["not_evaluated"]
    annotations: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    sources: list[SourceOut]
    generated_at: datetime


@router.post(
    "/comparisons",
    response_model=ComparisonOut,
    responses=ERRORS,
    summary="Comparar ofertas por línea y calcular totales por proveedor (E5)",
)
async def create_comparison(body: ComparisonIn, session: Session, actor: StrongActor):
    lines = _line_inputs(body)
    async with transaction(session):
        _, payload = await comparison_svc.run_comparison(
            session, actor, lines=lines, source_search_id=body.source_search_id
        )
        return payload


# --------------------------------------------------------------------------- E9: entrada


class CriterionTargetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["organization", "ingredient", "article", "supplier", "supplier_article"]
    ingredient: str | None = Field(default=None, max_length=200)
    article_id: uuid.UUID | None = None
    supplier_id: uuid.UUID | None = None


class MaxPriceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: str | Decimal
    currency: str = Field(min_length=3, max_length=3)
    basis: Literal["per_commercial_unit", "per_base_unit"]


class ConditionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: Literal["always", "min_quantity", "max_price", "date_window"] = "always"
    min_quantity: str | Decimal | None = None
    max_price: MaxPriceIn | None = None
    date_from: datetime | None = Field(default=None, alias="from")
    date_to: datetime | None = Field(default=None, alias="to")

    def to_service(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "min_quantity": None if self.min_quantity is None else str(self.min_quantity),
            "max_price": None if self.max_price is None else self.max_price.model_dump(),
            "from": self.date_from,
            "to": self.date_to,
        }


class ValidityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid_from: datetime | None = None
    valid_until: datetime | None = None


class CriterionSourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comparison_id: uuid.UUID | None = None
    offer_id: uuid.UUID | None = None


class CriterionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["note", "criterion"]
    confirm: bool = False
    target: CriterionTargetIn
    reason_text: str = Field(min_length=1, max_length=2000)
    effect: (
        Literal["note_only", "prefer", "deprioritize", "do_not_recommend", "requires_review"] | None
    ) = None
    condition: ConditionIn | None = None
    validity: ValidityIn | None = None
    source: CriterionSourceIn | None = None


class CriterionPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: CriterionTargetIn | None = None
    reason_text: str | None = Field(default=None, min_length=1, max_length=2000)
    condition: ConditionIn | None = None
    validity: ValidityIn | None = None


class NoteConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect: Literal["prefer", "deprioritize", "do_not_recommend", "requires_review"]
    target: CriterionTargetIn | None = None
    reason_text: str | None = Field(default=None, min_length=1, max_length=2000)
    condition: ConditionIn | None = None
    validity: ValidityIn | None = None


class DeactivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


# --------------------------------------------------------------------------- E9: salida


class CriterionTargetOut(BaseModel):
    level: str
    supplier_id: uuid.UUID | None
    article_id: uuid.UUID | None
    ingredient: str | None


class CriterionConditionOut(BaseModel):
    kind: str
    min_quantity: str | None
    max_price: dict[str, Any] | None
    date_window: dict[str, Any] | None


class CriterionSourceOut(BaseModel):
    kind: str
    comparison_id: uuid.UUID | None
    offer_id: uuid.UUID | None
    note_criterion_id: uuid.UUID | None
    note_version: int | None


class CriterionValidityOut(BaseModel):
    valid_from: datetime | None
    valid_until: datetime | None


class CommercialCriterionOut(BaseModel):
    """`CommercialCriterionOut` de C0 §4.5. `note_only` es el único efecto de una nota."""

    criterion_id: uuid.UUID
    kind: Literal["note", "criterion"]
    status: Literal["active", "inactive"]
    origin: Literal["explicit", "inferred"]
    effect: str
    target: CriterionTargetOut
    condition: CriterionConditionOut
    reason_text: str
    source: CriterionSourceOut
    validity: CriterionValidityOut
    version: int
    created_by: uuid.UUID
    created_at: datetime
    confirmed_by: uuid.UUID | None
    confirmed_at: datetime | None
    deactivated_by: uuid.UUID | None
    deactivated_at: datetime | None
    deactivation_reason: str | None
    priority: int | None


class CriterionListOut(BaseModel):
    criteria: list[CommercialCriterionOut]


IfMatch = Annotated[
    str | None,
    Header(alias="If-Match", description='Versión actual, p. ej. "3"'),
]


@router.get(
    "/commercial-criteria",
    response_model=CriterionListOut,
    responses=ERRORS,
    summary="Listar notas y criterios comerciales (E9)",
)
async def list_criteria(
    session: Session,
    actor: StrongActor,
    kind: Annotated[str | None, Query(pattern="^(note|criterion)$")] = None,
    status: Annotated[str | None, Query(pattern="^(active|inactive)$")] = None,
    target_level: Annotated[
        str | None,
        Query(pattern="^(organization|ingredient|article|supplier|supplier_article)$"),
    ] = None,
    supplier_id: uuid.UUID | None = None,
    article_id: uuid.UUID | None = None,
    include_inactive: bool = False,
):
    rows = await criteria_svc.list_criteria(
        session,
        actor,
        kind=kind,
        status=status,
        target_level=target_level,
        supplier_id=supplier_id,
        article_id=article_id,
        include_inactive=include_inactive,
    )
    return {"criteria": [criteria_svc.criterion_payload(c) for c in rows]}


@router.get(
    "/commercial-criteria/{criterion_id}",
    response_model=CommercialCriterionOut,
    responses=ERRORS,
    summary="Obtener una nota o criterio (E9)",
)
async def get_criterion(criterion_id: uuid.UUID, session: Session, actor: StrongActor):
    row = await criteria_svc.get_criterion(session, actor, criterion_id)
    return criteria_svc.criterion_payload(row)


@router.post(
    "/commercial-criteria",
    status_code=201,
    response_model=CommercialCriterionOut,
    responses=ERRORS,
    summary="Crear una nota o —con confirm:true— un criterio confirmado (E9)",
)
async def create_criterion(body: CriterionIn, session: Session, actor: StrongActor):
    async with transaction(session):
        row = await criteria_svc.create_criterion(
            session,
            actor,
            kind=body.kind,
            effect=body.effect,
            target=body.target.model_dump(),
            reason_text=body.reason_text,
            condition=body.condition.to_service() if body.condition else None,
            validity=body.validity.model_dump() if body.validity else None,
            source=body.source.model_dump() if body.source else None,
            confirm=body.confirm,
        )
        return criteria_svc.criterion_payload(row)


@router.patch(
    "/commercial-criteria/{criterion_id}",
    response_model=CommercialCriterionOut,
    responses=ERRORS,
    summary="Editar una nota o criterio; sube la versión y se audita (If-Match)",
)
async def patch_criterion(
    criterion_id: uuid.UUID,
    body: CriterionPatchIn,
    session: Session,
    actor: StrongActor,
    if_match: IfMatch = None,
):
    version = parse_if_match(if_match, what="el criterio")
    changes = body.model_dump(exclude_unset=True)
    if "condition" in changes and body.condition is not None:
        changes["condition"] = body.condition.to_service()
    async with transaction(session):
        row = await criteria_svc.update_criterion(
            session, actor, criterion_id, expected_version=version, changes=changes
        )
        return criteria_svc.criterion_payload(row)


@router.post(
    "/commercial-criteria/{criterion_id}/confirm",
    status_code=201,
    response_model=CommercialCriterionOut,
    responses=ERRORS,
    summary="Convertir una nota en criterio; crea un registro nuevo (If-Match)",
)
async def confirm_note(
    criterion_id: uuid.UUID,
    body: NoteConfirmIn,
    session: Session,
    actor: StrongActor,
    if_match: IfMatch = None,
):
    version = parse_if_match(if_match, what="la nota")
    async with transaction(session):
        row = await criteria_svc.confirm_note(
            session,
            actor,
            criterion_id,
            expected_version=version,
            effect=body.effect,
            target=body.target.model_dump() if body.target else None,
            reason_text=body.reason_text,
            condition=body.condition.to_service() if body.condition else None,
            validity=body.validity.model_dump() if body.validity else None,
        )
        return criteria_svc.criterion_payload(row)


@router.post(
    "/commercial-criteria/{criterion_id}/deactivate",
    response_model=CommercialCriterionOut,
    responses=ERRORS,
    summary="Desactivar una nota o criterio con motivo (If-Match)",
)
async def deactivate_criterion(
    criterion_id: uuid.UUID,
    body: DeactivateIn,
    session: Session,
    actor: StrongActor,
    if_match: IfMatch = None,
):
    version = parse_if_match(if_match, what="el criterio")
    async with transaction(session):
        row = await criteria_svc.deactivate_criterion(
            session, actor, criterion_id, expected_version=version, reason=body.reason
        )
        return criteria_svc.criterion_payload(row)


@router.post(
    "/commercial-criteria/{criterion_id}/reactivate",
    response_model=CommercialCriterionOut,
    responses=ERRORS,
    summary="Reactivar una nota o criterio inactivo (If-Match)",
)
async def reactivate_criterion(
    criterion_id: uuid.UUID,
    session: Session,
    actor: StrongActor,
    if_match: IfMatch = None,
):
    version = parse_if_match(if_match, what="el criterio")
    async with transaction(session):
        row = await criteria_svc.reactivate_criterion(
            session, actor, criterion_id, expected_version=version
        )
        return criteria_svc.criterion_payload(row)
