"""Interpretación de observaciones comerciales con un modelo (O2 / A6 Fase 3).

Flujo (contrato §8.4 y §8.5):

1. La observación **ya está guardada**; este servicio nunca la crea, edita ni borra.
2. Se arma el contexto verificado del artículo (solo sus ofertas reales) y se llama al modelo
   detrás de `ObservationModel`. En pruebas se inyecta un doble: no hay llamadas reales.
3. La salida estructurada se valida con `app.interpretation.commercial`. Si algo no cuadra, no se
   guarda **ninguna** preferencia: la interpretación queda `needs_review` o `failed` con un código
   interno.
4. Solo una propuesta válida crea `observation_interpretation` (estado `suggested`) y sus
   `commercial_preference`, en la misma transacción. Con retiros no hay preferencias, solo
   `retired_subject_kinds`.
5. No se ordena ninguna oferta aquí: el motor determinista (`services.preferences`) aplica el
   resultado al leer E3/E5.

Reintentos: si la observación ya tiene una interpretación aplicable (`suggested`/`confirmed`) no
se crea otra. Un intento fallido **sí** se puede reintentar y crea una fila nueva, sin duplicar la
observación ni sus preferencias (contrato §8.3 y §8.5.7). Un resultado ya procesado que no aplica
—`needs_review`, `no_preference` o `rejected`— se devuelve tal cual: repetir la interpretación no
duplica filas ni vuelve a consumir el proveedor. `retry_failed=False` también devuelve el fallo
anterior sin llamar al modelo.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import not_found
from app.core.permissions import require_admin
from app.domain.products import normalize_name
from app.interpretation.commercial import (
    ArticleResolution,
    CommercialContext,
    ContextArticle,
    ContextSupplier,
    ObservationProposal,
    ProposalRejected,
    ValidatedInterpretation,
    build_prompt,
    default_clarification,
    output_json_schema,
    validate_proposal,
)
from app.interpretation.openai_observations import (
    ModelError,
    ObservationModel,
    OpenAIResponsesModel,
)
from app.models import (
    CommercialArticlePreference,
    CommercialObservation,
    CommercialPreference,
    ObservationInterpretation,
    OfferSnapshot,
    Supplier,
    TradeItem,
    TradeItemAlias,
)
from app.services.actors import Actor
from app.services.events import advisory_lock, emit

logger = logging.getLogger(__name__)

APPLIED_STATUSES = frozenset({"suggested", "confirmed"})
CONTEXT_OFFER_LIMIT = 200
# Cota de sujetos conocidos que se muestran al modelo. Evita prompts sin límite en catálogos
# grandes; el orden es determinista.
CONTEXT_SUBJECT_LIMIT = 200
# Cota del catálogo que se indexa para resolver nombres de otros artículos por igualdad exacta.
CONTEXT_ARTICLE_LIMIT = 5000
CONTEXT_ALIAS_LIMIT = 20000
CLARIFICATION_CANDIDATE_LIMIT = 8
UNCONFIGURED_MODEL = "not_configured"


@dataclass(frozen=True)
class InterpretationOutcome:
    """Resultado de un intento. `status` usa los estados del contrato; `disabled` no es uno."""

    status: str
    interpretation_id: uuid.UUID | None = None
    preference_ids: tuple[uuid.UUID, ...] = ()
    article_preference_ids: tuple[uuid.UUID, ...] = ()
    retired_subject_kinds: tuple[str, ...] = ()
    clarification: dict | None = None
    error_code: str | None = None
    replayed: bool = False


async def interpret_observation(
    session: AsyncSession,
    actor: Actor,
    observation_id: uuid.UUID,
    *,
    model: ObservationModel | None = None,
    settings: Settings | None = None,
    retry_failed: bool = True,
) -> InterpretationOutcome:
    """Interpreta una observación existente. La observación debe estar guardada antes.

    `model` se inyecta en pruebas. Sin él se usa el adaptador de OpenAI solo si
    `OBSERVATION_AI_ENABLED` y la configuración están presentes; si no, no hay llamada ni fila.
    """
    require_admin(actor.roles)
    cfg = settings or get_settings()
    org = actor.organization_id

    observation = (
        await session.execute(
            select(CommercialObservation).where(
                CommercialObservation.id == observation_id,
                CommercialObservation.organization_id == org,
            )
        )
    ).scalar_one_or_none()
    if observation is None:
        raise not_found("La observación")

    # Serializa dos interpretaciones simultáneas de la misma observación en esta transacción.
    await advisory_lock(session, f"obsai:{org}:{observation.id}")

    # Se devuelve el último intento ya procesado sin volver a llamar al proveedor. Solo un fallo
    # puede reintentarse (y solo si el llamador lo permite): así un reintento del cliente no
    # duplica interpretaciones ni vuelve a consumir cuota.
    latest = (
        (
            await session.execute(
                select(ObservationInterpretation)
                .where(
                    ObservationInterpretation.organization_id == org,
                    ObservationInterpretation.observation_id == observation.id,
                )
                .order_by(
                    ObservationInterpretation.created_at.desc(),
                    ObservationInterpretation.id.desc(),
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if latest is not None and (
        latest.status in APPLIED_STATUSES
        or not retry_failed
        or latest.status != "failed"
    ):
        return await _replayed(session, latest)

    provider = model
    owns_provider = False
    if provider is None:
        if not cfg.observation_ai_enabled:
            return InterpretationOutcome(status="disabled")
        if cfg.openai_api_key is None or not cfg.openai_model:
            logger.warning(
                "Interpretación de observaciones habilitada sin configuración de OpenAI; "
                "la observación %s queda pendiente.",
                observation.id,
            )
            return await _persist_attempt(
                session,
                actor,
                observation,
                status="failed",
                model_name=cfg.openai_model or UNCONFIGURED_MODEL,
                prompt_version=cfg.openai_prompt_version,
                raw_output=None,
                input_tokens=None,
                output_tokens=None,
                latency_ms=0,
                error_code="unavailable",
                validated=None,
            )
        provider = OpenAIResponsesModel.from_settings(cfg)
        owns_provider = True

    context = await _build_context(session, cfg, observation)
    resolve_article = await _catalog_article_resolver(
        session, org, context.trade_item_id, context.display_name
    )
    system, user = build_prompt(context, observation.text_original)
    started = time.monotonic()
    try:
        completion = await provider.complete(
            system=system, user=user, json_schema=output_json_schema()
        )
    except ModelError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "Fallo al interpretar la observación %s (código interno=%s); no se aplica nada.",
            observation.id,
            exc.code,
        )
        return await _persist_attempt(
            session,
            actor,
            observation,
            status="failed",
            model_name=cfg.openai_model or UNCONFIGURED_MODEL,
            prompt_version=cfg.openai_prompt_version,
            raw_output=None,
            input_tokens=None,
            output_tokens=None,
            latency_ms=latency_ms,
            error_code=exc.code,
            validated=None,
        )
    finally:
        if owns_provider:
            await provider.aclose()

    proposal, raw_output = _parse_output(completion.text)
    if proposal is None:
        return await _persist_attempt(
            session,
            actor,
            observation,
            status="failed",
            model_name=completion.model,
            prompt_version=cfg.openai_prompt_version,
            raw_output=raw_output,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            latency_ms=completion.latency_ms,
            error_code="invalid_response",
            validated=None,
        )

    try:
        validated = validate_proposal(
            proposal,
            context,
            observation.text_original,
            implicit_tolerance_percent=cfg.observation_implicit_tolerance_percent,
            resolve_article=resolve_article,
        )
    except ProposalRejected as exc:
        # Todo rechazo queda con una pregunta: si el motivo no tiene una específica, se guarda
        # una genérica. Nunca se deja una interpretación pendiente sin explicar qué falta.
        return await _persist_attempt(
            session,
            actor,
            observation,
            status="needs_review",
            model_name=completion.model,
            prompt_version=cfg.openai_prompt_version,
            raw_output=raw_output,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            latency_ms=completion.latency_ms,
            error_code=exc.code,
            validated=None,
            clarification=exc.clarification or default_clarification(),
        )

    status = "suggested" if validated.has_effects else "no_preference"
    return await _persist_attempt(
        session,
        actor,
        observation,
        status=status,
        model_name=completion.model,
        prompt_version=cfg.openai_prompt_version,
        raw_output=raw_output,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        latency_ms=completion.latency_ms,
        error_code=None,
        validated=validated,
    )


def _parse_output(text: str) -> tuple[ObservationProposal | None, dict | None]:
    """JSON del modelo → propuesta estricta. Solo un objeto JSON puede guardarse en `raw_output`."""
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, None
    if not isinstance(raw, dict):
        return None, None
    try:
        return ObservationProposal.model_validate(raw), raw
    except ValidationError:
        return None, raw


async def _replayed(session: AsyncSession, existing: ObservationInterpretation) -> InterpretationOutcome:
    preference_ids = (
        (
            await session.execute(
                select(CommercialPreference.id).where(
                    CommercialPreference.interpretation_id == existing.id
                )
            )
        )
        .scalars()
        .all()
    )
    article_preference_ids = (
        (
            await session.execute(
                select(CommercialArticlePreference.id).where(
                    CommercialArticlePreference.interpretation_id == existing.id
                )
            )
        )
        .scalars()
        .all()
    )
    return InterpretationOutcome(
        status=existing.status,
        interpretation_id=existing.id,
        preference_ids=tuple(preference_ids),
        article_preference_ids=tuple(article_preference_ids),
        retired_subject_kinds=tuple(existing.retired_subject_kinds or ()),
        clarification=existing.clarification,
        error_code=existing.error_code,
        replayed=True,
    )


async def _persist_attempt(
    session: AsyncSession,
    actor: Actor,
    observation: CommercialObservation,
    *,
    status: str,
    model_name: str,
    prompt_version: str,
    raw_output: dict | None,
    input_tokens: int | None,
    output_tokens: int | None,
    latency_ms: int,
    error_code: str | None,
    validated: ValidatedInterpretation | None,
    clarification: dict | None = None,
) -> InterpretationOutcome:
    """Escribe la interpretación y, solo si es aplicable, todas sus preferencias a la vez."""
    now = datetime.now(UTC)
    retired = validated.retired_subject_kinds if validated is not None else ()
    interpretation = ObservationInterpretation(
        id=uuid.uuid4(),
        organization_id=observation.organization_id,
        observation_id=observation.id,
        trade_item_id=observation.trade_item_id,
        status=status,
        model=model_name[:80],
        prompt_version=prompt_version[:40],
        retired_subject_kinds=list(retired),
        clarification=clarification,
        raw_output=raw_output,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        error_code=error_code,
        created_at=now,
        data_origin="ai_derived",
    )
    session.add(interpretation)
    # La FK compuesta de `commercial_preference` apunta a la interpretación; sin relaciones ORM la
    # unidad de trabajo no deduce el orden, así que se asegura dentro de la misma transacción.
    await session.flush()
    preferences: list[CommercialPreference] = []
    if validated is not None and status == "suggested":
        for pref in validated.preferences:
            row = CommercialPreference(
                id=uuid.uuid4(),
                organization_id=observation.organization_id,
                interpretation_id=interpretation.id,
                observation_id=observation.id,
                trade_item_id=observation.trade_item_id,
                subject_kind=pref.subject_kind,
                subject_key=pref.subject_key,
                subject_label=pref.subject_label,
                subject_supplier_id=pref.subject_supplier_id,
                effect=pref.effect,
                tolerance_kind=pref.tolerance_kind,
                tolerance_percent=pref.tolerance_percent,
                tolerance_amount=pref.tolerance_amount,
                tolerance_currency=pref.tolerance_currency,
                tolerance_origin=pref.tolerance_origin,
                price_reference_kind=pref.price_reference_kind,
                price_reference_amount=pref.price_reference_amount,
                price_reference_currency=pref.price_reference_currency,
                price_reference_basis=pref.price_reference_basis,
                price_reference_quantity=pref.price_reference_quantity,
                price_reference_quantity_unit=pref.price_reference_quantity_unit,
                confidence=pref.confidence,
                status="suggested",
                created_at=now,
                data_origin="ai_derived",
            )
            session.add(row)
            preferences.append(row)
    article_preferences: list[CommercialArticlePreference] = []
    if validated is not None and status == "suggested":
        for pref in validated.article_preferences:
            row = CommercialArticlePreference(
                id=uuid.uuid4(),
                organization_id=observation.organization_id,
                interpretation_id=interpretation.id,
                observation_id=observation.id,
                article_id=pref.article_id,
                article_label=pref.article_label,
                relative_to_article_id=pref.relative_to_article_id,
                relative_to_article_label=pref.relative_to_article_label,
                effect=pref.effect,
                confidence=pref.confidence,
                status="suggested",
                created_at=now,
                data_origin="ai_derived",
            )
            session.add(row)
            article_preferences.append(row)
    await session.flush()

    emit(
        session,
        org_id=observation.organization_id,
        actor_id=actor.id,
        action="observation.interpret",
        entity_type="observation_interpretation",
        entity_id=interpretation.id,
        event_type="commercial_observation.interpreted",
        aggregate_version=None,
        payload={
            "observation_id": str(observation.id),
            "trade_item_id": str(observation.trade_item_id),
            "status": status,
            "error_code": error_code,
            "model": interpretation.model,
            "prompt_version": interpretation.prompt_version,
            "preference_count": len(preferences),
            "article_preference_count": len(article_preferences),
            "clarification_kind": clarification.get("kind") if clarification else None,
            "retired_subject_kinds": list(retired),
        },
        after={"status": status, "error_code": error_code},
    )
    return InterpretationOutcome(
        status=status,
        interpretation_id=interpretation.id,
        preference_ids=tuple(row.id for row in preferences),
        article_preference_ids=tuple(row.id for row in article_preferences),
        retired_subject_kinds=tuple(retired),
        clarification=clarification,
        error_code=error_code,
    )


async def _build_context(
    session: AsyncSession,
    settings: Settings,
    observation: CommercialObservation,
) -> CommercialContext:
    """Contexto verificado: el artículo, sus ofertas, el catálogo de sujetos de la organización
    y sus proveedores activos.

    El catálogo aporta únicamente nombres de laboratorio y marca (nunca ofertas, precios ni
    stock de otros artículos). Los proveedores activos se incluyen aunque todavía no tengan una
    oferta para este artículo. Todo queda acotado a la organización del `Actor`.
    """
    org = observation.organization_id
    item = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.id == observation.trade_item_id, TradeItem.organization_id == org
            )
        )
    ).scalar_one()
    offers = (
        (
            await session.execute(
                select(OfferSnapshot)
                .where(
                    OfferSnapshot.organization_id == org,
                    OfferSnapshot.trade_item_id == item.id,
                )
                .order_by(OfferSnapshot.observed_at.desc(), OfferSnapshot.id.desc())
                .limit(CONTEXT_OFFER_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    suppliers = tuple(
        ContextSupplier(id=row.id, name=row.name)
        for row in (
            (
                await session.execute(
                    select(Supplier)
                    .where(Supplier.organization_id == org, Supplier.status == "active")
                    .order_by(Supplier.name, Supplier.id)
                    .limit(CONTEXT_SUBJECT_LIMIT)
                )
            )
            .scalars()
            .all()
        )
    )
    catalog_subjects = (
        await session.execute(
            select(TradeItem.laboratory, TradeItem.brand)
            .where(TradeItem.organization_id == org, TradeItem.status == "active")
            .order_by(TradeItem.display_name, TradeItem.id)
            .limit(CONTEXT_SUBJECT_LIMIT)
        )
    ).all()
    # Oferta en vivo que originó la observación: es su único contexto comercial posible, porque
    # las ofertas vivas no se persisten como `OfferSnapshot`. No se inventa nada: se usa lo que la
    # fuente publicó y la persona está viendo.
    raw_view = (observation.context or {}).get("offer_view")
    offer_view = raw_view if isinstance(raw_view, dict) else None
    offer_laboratory = (offer_view or {}).get("laboratory")
    offer_brand = (offer_view or {}).get("brand")
    offer_currencies: set[str] = set()
    if offer_view is not None and isinstance(offer_view.get("price"), dict):
        currency = offer_view["price"].get("currency")
        if currency:
            offer_currencies.add(str(currency))
    # El propio artículo va primero, aunque estuviera fuera del límite o inactivo.
    known_laboratories = _unique(
        [offer_laboratory, item.laboratory, *(row[0] for row in catalog_subjects)]
    )[:CONTEXT_SUBJECT_LIMIT]
    known_brands = _unique(
        [offer_brand, item.brand, *(row[1] for row in catalog_subjects)]
    )[:CONTEXT_SUBJECT_LIMIT]
    return CommercialContext(
        trade_item_id=item.id,
        display_name=item.display_name,
        ingredient=item.ingredient,
        item_laboratory=item.laboratory,
        item_brand=item.brand,
        suppliers=suppliers,
        laboratories=_unique(
            [offer_laboratory, *(offer.laboratory_as_published for offer in offers)]
        ),
        brands=_unique([offer_brand, *(offer.brand_as_published for offer in offers)]),
        currencies=tuple(
            sorted({settings.currency} | {offer.currency for offer in offers} | offer_currencies)
        ),
        known_laboratories=known_laboratories,
        known_brands=known_brands,
        offer_view=offer_view,
    )


async def _catalog_article_resolver(
    session: AsyncSession,
    organization_id: uuid.UUID,
    current_id: uuid.UUID,
    current_name: str,
):
    """Resolver determinista de nombres de artículos por igualdad exacta normalizada.

    Solo usa el catálogo de la organización: nombre visible y alias activos. No compara parecido,
    marca, laboratorio ni presentación, y no fusiona artículos. Dos coincidencias distintas son
    ambigüedad legítima: se devuelven candidatos y se pide aclaración en lenguaje natural.
    """
    rows = (
        await session.execute(
            select(TradeItem.id, TradeItem.display_name)
            .where(
                TradeItem.organization_id == organization_id,
                TradeItem.status == "active",
            )
            .order_by(TradeItem.display_name, TradeItem.id)
            .limit(CONTEXT_ARTICLE_LIMIT)
        )
    ).all()
    articles: dict[uuid.UUID, ContextArticle] = {
        item_id: ContextArticle(item_id, name) for item_id, name in rows
    }
    # El artículo de la observación siempre se puede nombrar, aunque esté fuera de la cota.
    articles.setdefault(current_id, ContextArticle(current_id, current_name))
    by_name: dict[str, list[ContextArticle]] = {}
    for article in articles.values():
        key = normalize_name(article.display_name)
        if key:
            by_name.setdefault(key, []).append(article)
    aliases = (
        await session.execute(
            select(TradeItemAlias.trade_item_id, TradeItemAlias.alias_text)
            .where(
                TradeItemAlias.organization_id == organization_id,
                TradeItemAlias.status == "active",
            )
            .order_by(TradeItemAlias.normalized_alias, TradeItemAlias.id)
            .limit(CONTEXT_ALIAS_LIMIT)
        )
    ).all()
    for item_id, alias in aliases:
        article = articles.get(item_id)
        key = normalize_name(alias)
        if article is None or not key:
            continue
        group = by_name.setdefault(key, [])
        if all(existing.id != article.id for existing in group):
            group.append(article)

    def resolve(label: str) -> ArticleResolution:
        key = normalize_name(label)
        matches = by_name.get(key, ())
        if not matches:
            return ArticleResolution("not_found")
        ordered = tuple(sorted(matches, key=lambda a: (normalize_name(a.display_name), str(a.id))))
        if len(ordered) == 1:
            return ArticleResolution("resolved", ordered[0])
        return ArticleResolution(
            "ambiguous", None, ordered[:CLARIFICATION_CANDIDATE_LIMIT]
        )

    return resolve


async def load_interpretations(
    session: AsyncSession,
    organization_id: uuid.UUID,
    observation_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, tuple[ObservationInterpretation, ...]]:
    """Todos los intentos de esas observaciones, del más reciente al más antiguo.

    Solo lectura: no interpreta, no llama al proveedor y no crea filas.
    """
    ids = list(dict.fromkeys(observation_ids))
    if not ids:
        return {}
    rows = (
        (
            await session.execute(
                select(ObservationInterpretation)
                .where(
                    ObservationInterpretation.organization_id == organization_id,
                    ObservationInterpretation.observation_id.in_(ids),
                )
                .order_by(
                    ObservationInterpretation.observation_id,
                    ObservationInterpretation.created_at.desc(),
                    ObservationInterpretation.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[ObservationInterpretation]] = {}
    for row in rows:
        grouped.setdefault(row.observation_id, []).append(row)
    return {key: tuple(value) for key, value in grouped.items()}


async def load_preferences(
    session: AsyncSession,
    organization_id: uuid.UUID,
    interpretation_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, tuple[CommercialPreference, ...]]:
    """Preferencias de esas interpretaciones, agrupadas y en orden estable. Solo lectura."""
    ids = list(dict.fromkeys(interpretation_ids))
    if not ids:
        return {}
    rows = (
        (
            await session.execute(
                select(CommercialPreference)
                .where(
                    CommercialPreference.organization_id == organization_id,
                    CommercialPreference.interpretation_id.in_(ids),
                )
                .order_by(
                    CommercialPreference.created_at,
                    CommercialPreference.id,
                )
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[CommercialPreference]] = {}
    for row in rows:
        grouped.setdefault(row.interpretation_id, []).append(row)
    return {key: tuple(value) for key, value in grouped.items()}


async def load_article_preferences(
    session: AsyncSession,
    organization_id: uuid.UUID,
    interpretation_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, tuple[CommercialArticlePreference, ...]]:
    """Eventos entre artículos de esas interpretaciones. Solo lectura."""
    ids = list(dict.fromkeys(interpretation_ids))
    if not ids:
        return {}
    rows = (
        (
            await session.execute(
                select(CommercialArticlePreference)
                .where(
                    CommercialArticlePreference.organization_id == organization_id,
                    CommercialArticlePreference.interpretation_id.in_(ids),
                )
                .order_by(
                    CommercialArticlePreference.created_at,
                    CommercialArticlePreference.id,
                )
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[CommercialArticlePreference]] = {}
    for row in rows:
        grouped.setdefault(row.interpretation_id, []).append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _unique(values: Iterable[str | None]) -> tuple[str, ...]:
    """Valores publicados no vacíos, sin duplicados por nombre normalizado, en orden de aparición."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        clean = value.strip()
        key = normalize_name(clean)
        if clean and key and key not in seen:
            seen.add(key)
            out.append(clean)
    return tuple(out)
