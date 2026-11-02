import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.domain.quantities import QuantityOp, QuantityUnit, fmt_qty

Qty = Annotated[Decimal, PlainSerializer(fmt_qty, return_type=str, when_used="json")]


class ErrorBody(BaseModel):
    code: str
    message: str
    correlation_id: str
    details: dict = {}


class ErrorResponse(BaseModel):
    error: ErrorBody


class ObservationIn(BaseModel):
    text: str = Field(min_length=2, max_length=500, examples=["falta ibuprofeno de 400"])
    declared_quantity: Decimal | None = Field(
        default=None, gt=0, description="Sugerencia del declarante. NUNCA se aplica al objetivo."
    )
    declared_unit: QuantityUnit | None = None


class QuantityCommandIn(BaseModel):
    operation: QuantityOp
    value: Decimal | None = Field(default=None, examples=["5"])
    unit: QuantityUnit | None = None
    reason: str | None = Field(default=None, max_length=300)


class ResolveIn(BaseModel):
    demand_item_id: uuid.UUID | None = None
    create_new: bool = False
    apply_observed_attributes: bool = Field(
        default=False,
        description="Confirma en la necesidad los datos explícitos del aviso que ella no tenía "
        "(p. ej. la unidad). Solo toma lo que el aviso escribió; no completa nada más.",
    )
    remember_alias: bool = Field(
        default=False,
        description="Recuerda «cifra sin unidad = lo confirmado» para ese principio activo.",
    )


class MeasureIn(BaseModel):
    value: Decimal = Field(gt=0, examples=["400"])
    unit: str = Field(min_length=1, max_length=20, examples=["mg", "mg/5 ml", "%", "UI"])


class ConfirmAttributesIn(BaseModel):
    """Lo que la persona autorizada confirma. Cada campo entregado queda como CONFIRMADO."""

    ingredient: str | None = Field(
        default=None,
        max_length=200,
        description="Principio activo confirmado (reemplaza un ancla por marca).",
    )
    strengths: list[MeasureIn] | None = Field(default=None, description="Reemplaza las potencias.")
    form: str | None = Field(default=None, max_length=30, examples=["comprimido"])
    content: MeasureIn | None = Field(default=None, description="Contenido: unit 'un' = unidades.")
    release: str | None = Field(default=None, max_length=30)
    packaging: str | None = Field(default=None, max_length=30)
    confirm_inferred: list[Literal["strength", "form", "content", "packaging", "release"]] = Field(
        default=[], description="Pasa a confirmado lo que el sistema había deducido, sin cambiarlo."
    )
    remember_alias: bool = False


class Candidate(BaseModel):
    demand_item_id: uuid.UUID
    display_name: str
    relation: str
    reasons: list[str] = Field(
        default=[], description="Por qué no se asoció sola (p. ej. unit_unconfirmed)"
    )


Basis = Literal["explicit", "inferred", "confirmed"]


class NormalizedOut(BaseModel):
    dimension: str
    value: Qty
    unit: str


class MeasureOut(BaseModel):
    label: str = Field(description="Como se escribió, con unidad canónica")
    as_written: dict = Field(
        description="value/unit/per_value/per_unit/original_text tal como se dijo"
    )
    normalized: NormalizedOut | None = Field(
        description="Solo para comparar; null si no hay unidad"
    )


class StrengthOut(MeasureOut):
    kind: Literal["amount_per_unit", "concentration"]
    basis: Basis
    role_rule: str


class ContentOut(MeasureOut):
    basis: Basis


class TextAttrOut(BaseModel):
    value: str
    basis: Basis


class PendingOut(BaseModel):
    attribute: str
    reason: Literal[
        "not_stated", "unit_not_stated", "role_not_stated", "inferred_needs_confirmation"
    ]
    value: str | None = None


class AttributesOut(BaseModel):
    """Potencia, contenido y presentación SEPARADOS, cada uno con su procedencia."""

    strengths: list[StrengthOut]
    content: ContentOut | None
    form: TextAttrOut | None
    packaging: TextAttrOut | None
    release: TextAttrOut | None
    unassigned: list[MeasureOut] = Field(description="Medidas sin unidad o sin rol: pendientes")
    pending_validation: list[PendingOut]
    identity_complete: bool
    identity: dict = Field(
        description="Identidad farmacéutica (principio+potencia+forma) y comercial (marca, laboratorio, fabricante, "
        "titular, comercializador, GTIN, registro) por separado, con procedencia. null = desconocido"
    )


class ObservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    demand_item_id: uuid.UUID | None
    author: str | None = None
    raw_text: str
    declared_quantity: Qty | None
    declared_unit: str | None
    match_status: str
    candidates: list[Candidate] = []
    attributes: AttributesOut | None = Field(
        default=None, description="Lo interpretado de este aviso"
    )
    created_at: datetime


class DemandItemOut(BaseModel):
    id: uuid.UUID
    display_name: str
    ingredient: str
    attributes: AttributesOut
    identity_complete: bool
    pending_validation: list[PendingOut]
    target_quantity: Qty | None = Field(description="null = cantidad desconocida (no 0 ni 1)")
    quantity_unit: str | None
    quantity_status: Literal["unknown", "defined"]
    needs_quantity: bool
    data_origin: str = Field(
        default="unclassified", description="simulated | captured | ai_derived | unclassified"
    )
    suggestions: list[dict] = Field(
        default=[],
        description="Candidatos de catálogo verificado para medidas sin unidad. Solo sugieren: nada está confirmado.",
    )
    status: str
    version: int
    observation_count: int
    reporters: list[str]
    last_observed_at: datetime | None
    created_at: datetime


class DemandItemList(BaseModel):
    items: list[DemandItemOut]
    next_cursor: str | None
    pending_review_count: int


class QuantityEventOut(BaseModel):
    id: uuid.UUID
    operation: str
    value: Qty | None
    unit: str | None
    before_value: Qty | None
    before_unit: str | None
    after_value: Qty | None
    after_unit: str | None
    version_after: int
    actor: str
    reason: str | None
    created_at: datetime


class DemandItemDetail(DemandItemOut):
    observations: list[ObservationOut]
    quantity_events: list[QuantityEventOut]


class AliasOut(BaseModel):
    id: uuid.UUID
    ingredient: str
    source_value: str
    assignment: dict
    status: str
    created_at: datetime


class ObservationResultOut(BaseModel):
    outcome: Literal["created_item", "attached_existing", "needs_review", "resolved_manual"]
    replayed: bool
    quantity_changed: Literal[False] = Field(
        False, description="Un aviso nunca cambia la cantidad (HLD inv. 1)."
    )
    message: str
    pending_validation: list[PendingOut] = Field(
        description="Qué falta o no está confirmado. El aviso ya quedó registrado: no es una pregunta."
    )
    alias_created: bool = False
    observation: ObservationOut
    item: DemandItemOut | None


class QuantityResultOut(BaseModel):
    replayed: bool
    previous_quantity: Qty | None
    previous_unit: str | None
    item: DemandItemOut
    event: QuantityEventOut


class MeOut(BaseModel):
    id: uuid.UUID
    display_name: str
    roles: list[str]
    organization_id: uuid.UUID
    identity_level: Literal["declared"] = "declared"
    organization_kind: str = Field(description="simulation | real_test | production | legacy_mixed")
    is_superadmin: bool = Field(
        default=False, description="Control técnico de la propietaria; no es un rol"
    )
    web_access_expires_at: datetime | None = Field(
        default=None, description="NULL = acceso sin vencimiento"
    )


class ImportRowError(BaseModel):
    """Motivo de rechazo de una fila; el código pertenece al vocabulario cerrado de D-090."""

    row: int = Field(description="Número de fila de la planilla original")
    code: str
    message: str


class ImportResult(BaseModel):
    """Sobre congelado de E8 (contrato C0): estos campos y ninguno más."""

    import_id: uuid.UUID | None
    status: Literal["completed", "completed_with_errors", "failed"]
    rows_total: int
    rows_accepted: int
    rows_rejected: int
    errors: list[ImportRowError]
    observed_at: str | None
