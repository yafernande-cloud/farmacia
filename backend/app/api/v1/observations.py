"""A6 Fase 3B — interpretación y consulta de observaciones comerciales (O2–O4).

Flujo: `POST /commercial-observations` guarda el texto libre (E13/O1, `quotes.py`) → este módulo
lo interpreta con el servicio interno de Fase 3A → el frontend consulta el resultado y las
preferencias vigentes. Ninguna lectura interpreta ni llama al proveedor.

Rutas (el espacio `commercial-observations` ya existe en el contrato §8.3, no se duplica):

  * `POST /commercial-observations/{id}/interpretations` — interpretar o devolver el intento ya
    procesado;
  * `GET  /commercial-observations/{id}` — observación, intentos y preferencias;
  * `GET  /articles/{article_id}/preferences` — preferencias vigentes e historial.

Reglas que se conservan tal cual:

  * solo `admin` + `StrongActor`, y siempre dentro de la organización del actor;
  * el cliente nunca envía preferencias, sujetos validados, estados ni organización;
  * la observación se guarda aunque la interpretación falle o sea ambigua;
  * una interpretación ya procesada no se repite; solo un fallo puede reintentarse;
  * el motor determinista sigue siendo el único que ordena ofertas.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Session, StrongActor
from app.api.v1.demand import ERRORS
from app.core.errors import DomainError
from app.db.session import transaction
from app.interpretation.openai_observations import ObservationModel
from app.models import CommercialObservation
from app.services import observation_ai
from app.services import preferences as preferences_svc
from app.services import quotes as quote_svc
from app.services.observation_views import (
    article_preference_view,
    observation_view,
    preference_view,
)

router = APIRouter(tags=["observaciones comerciales"])

InterpretationStatus = Literal[
    "suggested", "confirmed", "rejected", "failed", "needs_review", "no_preference"
]
ObservationInterpretationStatus = Literal[
    "sin_interpretacion",
    "suggested",
    "confirmed",
    "rejected",
    "failed",
    "needs_review",
    "no_preference",
]


def get_observation_model() -> ObservationModel | None:
    """El servicio arma el proveedor real desde la configuración; las pruebas lo sustituyen."""
    return None


ObservationModelDep = Annotated[ObservationModel | None, Depends(get_observation_model)]


# --------------------------------------------------------------------------- salida


class ToleranceOut(BaseModel):
    kind: Literal["relative_percent", "absolute_amount"]
    percent: str | None
    amount: str | None
    currency: str | None


class PriceReferenceOut(BaseModel):
    kind: Literal["acceptable", "maximum"]
    amount: str
    currency: str
    basis: str
    quantity: str | None
    quantity_unit: str | None


class PreferenceOut(BaseModel):
    preference_id: uuid.UUID
    subject_kind: Literal["laboratory", "brand", "supplier", "availability", "price"]
    subject_label: str
    effect: Literal["prefer", "deprioritize"]
    tolerance: ToleranceOut | None = None
    price_reference: PriceReferenceOut | None = None


class ArticlePreferenceOut(BaseModel):
    """Efecto comercial entre dos artículos distintos; nunca una equivalencia."""

    article_preference_id: uuid.UUID
    article_id: uuid.UUID
    article_label: str
    relative_to_article_id: uuid.UUID | None
    relative_to_article_label: str | None
    effect: Literal["prefer", "deprioritize", "retire"]


class ClarificationCandidateOut(BaseModel):
    article_id: uuid.UUID
    display_name: str


ClarificationKind = Literal[
    "ambiguous_article",
    "unknown_article",
    "unknown_laboratory",
    "unknown_brand",
    "unknown_supplier",
    "unclear_referent",
    "missing_detail",
    "conflicting",
    "not_commercial",
    "low_confidence",
]


class ClarificationOut(BaseModel):
    """Pregunta breve para resolver una duda. No aplica nada por sí sola."""

    kind: ClarificationKind
    question: str
    reference: str | None = None
    candidates: list[ClarificationCandidateOut] = []


class InterpretationOut(BaseModel):
    interpretation_id: uuid.UUID
    status: InterpretationStatus
    # Código interno cerrado (`timeout`, `ambiguous`, …); nunca incluye prompts ni razonamiento.
    error_code: str | None
    retired_subject_kinds: list[str]
    created_at: datetime
    preferences: list[PreferenceOut] = []
    article_preferences: list[ArticlePreferenceOut] = []
    clarification: ClarificationOut | None = None


class ObservationDetailOut(BaseModel):
    observation_id: uuid.UUID
    text: str
    trade_item_id: uuid.UUID
    supplier_id: uuid.UUID | None
    offer_snapshot_id: uuid.UUID | None
    quote_id: uuid.UUID | None
    quote_line_id: uuid.UUID | None
    author_id: uuid.UUID
    created_at: datetime
    context: dict[str, Any]
    data_origin: str
    interpretation_status: ObservationInterpretationStatus
    interpretation: InterpretationOut | None
    interpretations: list[InterpretationOut]
    retired_subject_kinds: list[str]


class InterpretationResultOut(BaseModel):
    replayed: bool = Field(
        description="true = se devolvió un intento ya procesado; no hubo llamada nueva."
    )
    observation: ObservationDetailOut


class PreferenceHistoryOut(PreferenceOut):
    interpretation_id: uuid.UUID
    subject_class: str
    status: str
    is_active: bool
    created_at: datetime


class RetirementOut(BaseModel):
    """Retiro explícito por clase (D-144). No borra el historial de la preferencia."""

    subject_kind: str
    interpretation_id: uuid.UUID
    observation_id: uuid.UUID
    created_at: datetime
    is_active: bool


class ArticlePreferencesOut(BaseModel):
    article_id: uuid.UUID
    preferences: list[PreferenceOut] = Field(
        description="Vigentes, ya resueltas por precedencia temporal."
    )
    # Efectos entre artículos vigentes que tocan este artículo (como principal o alternativa).
    article_preferences: list[ArticlePreferenceOut] = []
    retired_subject_kinds: list[str] = []
    history: list[PreferenceHistoryOut] = []
    retirements: list[RetirementOut] = []


# --------------------------------------------------------------------------- constructores


async def _detail(
    session: AsyncSession, row: CommercialObservation
) -> dict[str, Any]:
    """Observación con todos sus intentos. Solo lectura: no interpreta ni llama al proveedor."""
    interpretations = (
        await observation_ai.load_interpretations(session, row.organization_id, [row.id])
    ).get(row.id, ())
    prefs = await observation_ai.load_preferences(
        session, row.organization_id, [attempt.id for attempt in interpretations]
    )
    article_prefs = await observation_ai.load_article_preferences(
        session, row.organization_id, [attempt.id for attempt in interpretations]
    )
    latest = interpretations[0] if interpretations else None
    payload = observation_view(
        row, latest, prefs, history=interpretations, article_prefs=article_prefs
    )
    # Un retiro solo tiene efecto si la interpretación se aplicó (D-144).
    payload["retired_subject_kinds"] = (
        list(latest.retired_subject_kinds or ())
        if latest is not None and latest.status in ("suggested", "confirmed")
        else []
    )
    return payload


# --------------------------------------------------------------------------- endpoints


@router.get(
    "/commercial-observations/{observation_id}",
    response_model=ObservationDetailOut,
    responses=ERRORS,
    summary="Detalle de una observación, sus interpretaciones y sus preferencias",
)
async def get_commercial_observation(
    observation_id: uuid.UUID,
    session: Session,
    actor: StrongActor,
):
    row = await quote_svc.get_observation(session, actor, observation_id)
    return await _detail(session, row)


@router.post(
    "/commercial-observations/{observation_id}/interpretations",
    response_model=InterpretationResultOut,
    responses=ERRORS,
    summary="Interpretar una observación con el servicio interno de A6 Fase 3A",
)
async def interpret_commercial_observation(
    observation_id: uuid.UUID,
    session: Session,
    actor: StrongActor,
    model: ObservationModelDep,
):
    async with transaction(session):
        row = await quote_svc.get_observation(session, actor, observation_id)
        outcome = await observation_ai.interpret_observation(
            session, actor, row.id, model=model
        )
        if outcome.status == "disabled":
            raise DomainError(
                "OBSERVATION_AI_DISABLED",
                "La interpretación con IA no está habilitada en esta instalación.",
                status_code=503,
            )
        return {"replayed": outcome.replayed, "observation": await _detail(session, row)}


@router.get(
    "/articles/{article_id}/preferences",
    response_model=ArticlePreferencesOut,
    responses=ERRORS,
    summary="Preferencias comerciales vigentes e historial de un artículo",
)
async def get_article_preferences(
    article_id: uuid.UUID,
    session: Session,
    actor: StrongActor,
):
    item = await quote_svc.get_trade_item(session, actor, article_id)
    loaded = (
        await preferences_svc.load_item_preferences(
            session, actor.organization_id, [item.id]
        )
    )[item.id]
    history = await preferences_svc.load_preference_history(
        session, actor.organization_id, item.id
    )
    article_loaded = await preferences_svc.load_article_preferences(
        session, actor.organization_id, [item.id]
    )
    active_ids = {pref.id for pref in loaded.applicable}
    current_retired = set(loaded.retired_subject_kinds)
    retirements = [
        {
            "subject_kind": kind,
            "interpretation_id": interpretation.id,
            "observation_id": interpretation.observation_id,
            "created_at": interpretation.created_at,
            "is_active": kind in current_retired,
        }
        for interpretation in reversed(loaded.interpretations)
        if interpretation.status in ("suggested", "confirmed")
        for kind in (interpretation.retired_subject_kinds or ())
    ]
    return {
        "article_id": item.id,
        "preferences": [preference_view(pref) for pref in loaded.applicable],
        "article_preferences": [
            article_preference_view(event) for event in article_loaded.active_events
        ],
        "retired_subject_kinds": list(loaded.retired_subject_kinds),
        "history": [
            {
                **preference_view(pref),
                "interpretation_id": pref.interpretation_id,
                "subject_class": preferences_svc.preference_class(pref),
                "status": pref.status,
                "is_active": pref.id in active_ids,
                "created_at": pref.created_at,
            }
            for pref in history
        ],
        "retirements": retirements,
    }
