"""Registro de faltantes y observaciones con deduplicación determinista (HLD §7).

Un aviso se registra SIEMPRE (nunca se pierde ni se bloquea esperando respuesta del auxiliar).
Lo que depende de la incertidumbre es el vínculo con una necesidad existente:
  * idéntico, todo explícito/confirmado  → se asocia solo;
  * parcial / no confirmado / ambiguo    → queda pendiente con candidatos y motivos, para que
                                            la encargada confirme en un paso (y recuerde la decisión);
  * incompatible                         → jamás es candidato.
"""

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.core.permissions import Permission, require
from app.domain.products import (
    POLICY_VERSION,
    Basis,
    ConfirmPatch,
    ParsedProduct,
    Relation,
    apply_confirmation,
    compare,
    normalize_name,
)
from app.domain.units import fmt
from app.interpretation.ai import AIInterpreter
from app.interpretation.types import Interpretation
from app.models import (
    DemandItem,
    DemandObservation,
    ProductAlias,
    PurchaseCycle,
    Quote,
    QuoteCart,
    QuoteLine,
    QuoteLineAllocation,
)
from app.services import coverage as coverage_svc
from app.services.actors import Actor
from app.services.events import advisory_lock, emit, request_hash
from app.services.interpretation import interpret_product, interpret_text
from app.services.provenance import org_kind, origin_for

# Cantidad predeterminada de un reporte de Telegram sin cantidad (D-147). Es una unidad genérica:
# no se asume caja, frasco ni sobre. La API la expone con `quantity_origin="default"`.
DEFAULT_QUANTITY = Decimal(1)
DEFAULT_UNIT = "unit"


@dataclass
class ObservationResult:
    observation: DemandObservation | None
    item: DemandItem | None
    # created_item | attached_existing | needs_review | resolved_manual | needs_confirmation |
    # blocked_in_cart
    outcome: str
    parsed: ParsedProduct
    replayed: bool = False
    alias_created: bool = False
    interpretation: Interpretation | None = None
    # Reportes de OTRAS personas sobre el mismo producto. Solo informan: nunca se adjunta ni se
    # fusiona automáticamente un reporte a la necesidad de otra persona (Modelo A, D-135).
    other_reports: list[dict] = field(default_factory=list)
    # Reporte del MISMO autor con cantidad conocida, cuando un nuevo aviso trae otra cantidad.
    same_author_item: dict | None = None


@dataclass(frozen=True)
class Origin:
    """De dónde vino el aviso y bajo qué identidad declarada (trazabilidad, HLD §3.1)."""

    source: str = "api"  # api | telegram_text | telegram_photo | photo_manual
    identity_level: str = "api_user"  # api_user | declared | pin | account
    actor_session_id: uuid.UUID | None = None
    channel_message_id: uuid.UUID | None = None
    attachment_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None  # cuenta de Telegram que envió (puede no ser la persona)
    # Sucursal de esa cuenta al momento del aviso: se copia, no se consulta después (histórico).
    branch_label: str | None = None


def parsed_of_item(i: DemandItem) -> ParsedProduct:
    return ParsedProduct.from_dict(i.attributes)


def _parsed_json(p: ParsedProduct) -> dict[str, Any]:
    d = p.to_dict()
    d.update(display_name=p.display_name, identity_complete=p.identity_complete,
             pending_validation=p.pending_validation, identity=p.identity_summary())  # fmt: skip
    return d


def _apply_to_item(item: DemandItem, p: ParsedProduct) -> None:
    item.attributes = p.to_dict()
    item.display_name = p.display_name
    item.resolved_identity_key = p.resolved_identity_key
    item.pharma_identity_key = p.pharma_identity_key
    item.ingredient = p.ingredient


def _new_item(
    org_id: uuid.UUID,
    cycle_id: uuid.UUID,
    p: ParsedProduct,
    data_origin: str,
    reported_by: uuid.UUID,
) -> DemandItem:
    item = DemandItem(organization_id=org_id, cycle_id=cycle_id, ingredient=p.ingredient,
                      display_name="", attributes={}, data_origin=data_origin,
                      reported_by=reported_by)  # fmt: skip
    _apply_to_item(item, p)
    return item


def _item_snapshot(i: DemandItem) -> dict:
    return {
        "display_name": i.display_name, "target_quantity": i.target_quantity and fmt(i.target_quantity),
        "quantity_unit": i.quantity_unit, "version": i.version, "status": i.status,
        "identity_complete": parsed_of_item(i).identity_complete,
        "reported_by": str(i.reported_by) if i.reported_by else None,
        "cancelled_at": i.cancelled_at.isoformat() if i.cancelled_at else None,
        "cancellation_reason": i.cancellation_reason,
    }  # fmt: skip


async def _open_cycle(session: AsyncSession, org_id: uuid.UUID) -> PurchaseCycle:
    c = (
        await session.execute(
            select(PurchaseCycle).where(
                PurchaseCycle.organization_id == org_id, PurchaseCycle.status == "open"
            )
        )
    ).scalar_one_or_none()
    if c is None:
        raise DomainError("NO_OPEN_CYCLE", "No hay un ciclo de compra abierto.")
    return c


async def active_aliases(session: AsyncSession, org: uuid.UUID, ingredient: str) -> list[dict]:
    rows = (
        await session.execute(
            select(ProductAlias).where(
                ProductAlias.organization_id == org,
                ProductAlias.ingredient == ingredient,
                ProductAlias.status == "active",
            )
        )
    ).scalars()
    return [{"source_value": a.source_value, "assignment": a.assignment} for a in rows]


async def _family_items(
    session,
    org,
    cycle_id,
    ingredient,
    *,
    reported_by: uuid.UUID | None = None,
    all_authors: bool = False,
) -> list[DemandItem]:
    """Faltantes activos de la misma familia, filtrados por autor salvo `all_authors`.

    El filtro por autor es la regla del Modelo A: la deduplicación solo mira las necesidades de la
    MISMA persona. Las de otras personas se consultan aparte, solo para informar (D-135).
    """
    q = select(DemandItem).where(
        DemandItem.organization_id == org, DemandItem.cycle_id == cycle_id,
        DemandItem.status == "active", DemandItem.ingredient == ingredient,
    )  # fmt: skip
    if not all_authors:
        q = q.where(
            DemandItem.reported_by.is_(None)
            if reported_by is None
            else DemandItem.reported_by == reported_by
        )
    return list(
        (await session.execute(q.order_by(DemandItem.created_at, DemandItem.id))).scalars()
    )


def _require_available(actor: Actor, origin: "Origin | None") -> None:
    """Ningún reporte es válido sin una identidad activa y autorizada. Por canal, la sesión se valida al
    recibir/atribuir; por API directa, la persona debe estar disponible para reportar."""
    if (origin is None or origin.actor_session_id is None) and not actor.available:
        raise DomainError(
            "PERSON_NOT_AVAILABLE",
            f"{actor.display_name} no está disponible para reportar.",
            status_code=403,
        )


async def record_observation(
    session: AsyncSession,
    actor: Actor,
    *,
    text: str,
    idempotency_key: str,
    declared_quantity: Decimal | None = None,
    declared_unit: str | None = None,
    ai: AIInterpreter | None = None,
    origin: Origin | None = None,
    confirmation_mode: str | None = None,
    quantity_proposal: dict | None = None,
) -> ObservationResult:
    """Registra un aviso escrito. Nunca modifica cantidades objetivo (inv. 1).

    `confirmation_mode="telegram"` no registra el aviso cuando hay una coincidencia que requiere
    confirmación: devuelve `needs_confirmation` y el canal pregunta antes de crear nada (D-135).
    """
    require(actor.roles, Permission.REPORT_SHORTAGE)
    _require_available(actor, origin)
    org = actor.organization_id
    req = request_hash(
        {"actor": actor.id, "text": text, "q": declared_quantity, "u": declared_unit}
    )
    await advisory_lock(session, f"idem:{org}:{idempotency_key}")  # idempotencia técnica (AC12)
    prior = await _observation_by_key(session, org, idempotency_key)
    if prior:
        return await _replay(session, prior, req)
    cycle = await _open_cycle(session, org)
    interp = await interpret_text(session, org, cycle.id, text, ai)  # puede lanzar DomainError
    return await _store(session, actor, cycle, interp, raw_text=text, idempotency_key=idempotency_key,
                        req=req, declared_quantity=declared_quantity, declared_unit=declared_unit,
                        origin=origin or Origin(), confirmation_mode=confirmation_mode,
                        quantity_proposal=quantity_proposal)  # fmt: skip


async def record_interpreted_observation(
    session: AsyncSession,
    actor: Actor,
    *,
    parsed: ParsedProduct,
    raw_text: str,
    idempotency_key: str,
    origin: Origin,
    force_review: tuple[str, ...] = (),
    extra_interpretation: dict | None = None,
    ai: AIInterpreter | None = None,
    ai_derived: bool = False,
    confirmation_mode: str | None = None,
    quantity_proposal: dict | None = None,
    apply_default_quantity: bool = False,
) -> ObservationResult:
    """Registra un aviso cuya interpretación ya viene hecha (p. ej. foto + pie de foto).

    Pasa por el MISMO motor determinista que un texto: alias, candidatos, compatibilidad y
    deduplicación. `force_review` (p. ej. conflicto foto/texto) impide toda asociación automática.
    """
    require(actor.roles, Permission.REPORT_SHORTAGE)
    _require_available(actor, origin)
    org = actor.organization_id
    req = request_hash(
        {"actor": actor.id, "raw": raw_text, "parsed": parsed.to_dict(), "force": force_review}
    )
    await advisory_lock(session, f"idem:{org}:{idempotency_key}")
    prior = await _observation_by_key(session, org, idempotency_key)
    if prior:
        return await _replay(session, prior, req)
    cycle = await _open_cycle(session, org)
    interp = await interpret_product(session, org, cycle.id, parsed, ai)
    return await _store(session, actor, cycle, interp, raw_text=raw_text, idempotency_key=idempotency_key,
                        req=req, declared_quantity=parsed.requested_quantity,
                        declared_unit=parsed.requested_unit, origin=origin, force_review=force_review,
                        extra_interpretation=extra_interpretation, ai_derived=ai_derived,
                        confirmation_mode=confirmation_mode,
                        quantity_proposal=quantity_proposal,
                        apply_default_quantity=apply_default_quantity)  # fmt: skip


async def _store(
    session, actor, cycle, interp: Interpretation, *, raw_text, idempotency_key, req,
    declared_quantity, declared_unit, origin: Origin, force_review=(), extra_interpretation=None,
    ai_derived=False, confirmation_mode: str | None = None, quantity_proposal: dict | None = None,
    apply_default_quantity: bool = False,
) -> ObservationResult:  # fmt: skip
    org = actor.organization_id
    parsed = interp.parsed
    # Procedencia: la organización manda. En simulación todo es `simulated`; en una real, `ai_derived`
    # solo si el resultado lo derivó un modelo (lectura de imagen real) y `captured` si lo escribió una persona.
    kind = await org_kind(session, org)
    data_origin = origin_for(kind, ai_derived=ai_derived)
    # Serializa por familia (ciclo + principio activo YA resuelto): avisos simultáneos del mismo
    # producto producen una necesidad por autor y N observaciones (AC11, HLD §7.4, D-135).
    await advisory_lock(session, f"fam:{org}:{cycle.id}:{parsed.ingredient}")
    if declared_quantity is None:  # la cantidad escrita en el texto es solo una sugerencia
        declared_quantity, declared_unit = parsed.requested_quantity, parsed.requested_unit
    # Procedencia de la cantidad: "explicit" = la escribió la persona; "default" = 1 unidad
    # genérica aplicada por la regla de cantidad predeterminada (D-147). Se conserva en la
    # observación para no confundirla con una cantidad informada (trazabilidad).
    quantity_origin = "explicit" if declared_quantity is not None else None

    # La deduplicación solo considera las necesidades del MISMO autor. Las de otras personas se
    # leen aparte para informar y, en Telegram, pedir confirmación: nunca se adjunta el aviso.
    own_family = await _family_items(
        session, org, cycle.id, parsed.ingredient, reported_by=actor.id
    )
    all_family = await _family_items(
        session, org, cycle.id, parsed.ingredient, all_authors=True
    )
    comps = [
        (c, compare(parsed, parsed_of_item(c)))
        for c in own_family
    ]
    comps = [(c, k) for c, k in comps if k.relation is not Relation.INCOMPATIBLE]  # AC09
    identical = [c for c, k in comps if k.relation is Relation.IDENTICAL]

    other_reports: list[dict] = []
    declared_by_item = await _latest_declared_by_item(
        session, org, [c.id for c in all_family]
    )
    for c in all_family:
        if c.reported_by == actor.id:
            continue
        k = compare(parsed, parsed_of_item(c))
        if k.relation is Relation.INCOMPATIBLE:
            continue
        other_reports.append(
            _other_report(c, k.relation.value, declared_by_item.get(c.id))
        )
    other_identical = [r for r in other_reports if r["relation"] == Relation.IDENTICAL.value]

    # Confirmación explícita en el canal: no se crea ni se adjunta nada todavía.
    if confirmation_mode == "telegram":
        if len(identical) == 1:
            same = identical[0]
            previous = declared_by_item.get(same.id)
            # Repetición exacta del mismo aviso (misma cantidad y unidad): es un aviso, no un
            # cambio. Nunca se suma ni se pregunta por una cantidad que no cambió (D-147).
            same_as_previous = bool(
                previous is not None
                and previous[0] == declared_quantity
                and previous[1] == declared_unit
            )
            if declared_quantity is not None and not same_as_previous:
                # Con el faltante en un carrito activo no se altera la cantidad solicitada.
                if await item_in_cart(session, org, same):
                    return ObservationResult(
                        None, same, "blocked_in_cart", parsed, interpretation=interp
                    )
                # Cualquier cantidad nueva sobre un faltante propio se pregunta antes de cambiar
                # nada: ¿es un total nuevo o una cantidad adicional? Nunca se suma sola y no se
                # supone una equivalencia entre unidades distintas (D-147).
                return ObservationResult(
                    None, same, "needs_confirmation", parsed, interpretation=interp,
                    same_author_item=_other_report(
                        same, Relation.IDENTICAL.value, previous
                    ),
                )  # fmt: skip
        if other_identical:
            # Otra persona ya reportó lo mismo. No se adjunta ni se suma: se pregunta si es un
            # reporte propio independiente (D-135).
            return ObservationResult(
                None, None, "needs_confirmation", parsed, interpretation=interp,
                other_reports=other_reports,
            )  # fmt: skip

    reasons = list(force_review)
    if interp.unrecognized_name_with_candidates:
        reasons.append("ingredient_unrecognized")
    item: DemandItem | None = None
    if reasons:  # nombre dudoso o conflicto foto/texto: nunca se asocia ni se crea solo
        outcome = "needs_review"
        cands = [
            {"demand_item_id": str(c.id), "display_name": c.display_name,
             "relation": k.relation.value, "reasons": k.reasons + reasons}
            for c, k in comps
        ]  # fmt: skip
    elif len(identical) == 1:
        outcome, item, cands = "attached_existing", identical[0], []
    elif comps:
        outcome = "needs_review"
        cands = [
            {"demand_item_id": str(c.id), "display_name": c.display_name,
             "relation": k.relation.value, "reasons": k.reasons}
            for c, k in comps
        ]  # fmt: skip
    else:
        # Sin coincidencia PROPIA: el aviso crea su propio reporte, aunque otra persona ya haya
        # reportado el mismo producto. No se fusiona ni se suma nada entre autores.
        outcome, cands = "created_item", []
        item = _new_item(org, cycle.id, parsed, data_origin, actor.id)
        session.add(item)
        await session.flush()

    # Cantidad predeterminada (D-147): un aviso por Telegram sin cantidad pide 1 unidad genérica.
    # No se supone caja, frasco ni sobre: la unidad queda sin presentación hasta que alguien la
    # informe. Aplica al reporte nuevo y también al aviso que se adjunta a un faltante que aún no
    # tiene cantidad declarada (histórico sin cantidad), sin tocar observaciones anteriores. No
    # aplica a la API ni reemplaza una cantidad ya conocida.
    previous_for_item = declared_by_item.get(item.id) if item is not None else None
    if (
        declared_quantity is None
        and (confirmation_mode == "telegram" or apply_default_quantity)
        and (
            outcome == "created_item"
            or (outcome == "attached_existing" and previous_for_item is None)
        )
    ):
        declared_quantity, declared_unit = DEFAULT_QUANTITY, DEFAULT_UNIT
        quantity_origin = "default"

    snapshot = interp.snapshot() | (extra_interpretation or {})
    if quantity_origin is not None:
        snapshot["quantity_origin"] = quantity_origin
    if reasons:
        snapshot["uncertainties"] = list(dict.fromkeys([*snapshot["uncertainties"], *reasons]))
    if other_reports:
        snapshot["other_reports"] = other_reports
    obs = DemandObservation(
        organization_id=org, cycle_id=cycle.id, demand_item_id=item.id if item else None,
        author_id=actor.id, raw_text=raw_text, parsed=_parsed_json(parsed), interpretation=snapshot,
        source=origin.source, identity_level=origin.identity_level, actor_session_id=origin.actor_session_id,
        channel_message_id=origin.channel_message_id, attachment_id=origin.attachment_id,
        account_id=origin.account_id,
        branch_label=origin.branch_label,
        declared_quantity=declared_quantity, declared_unit=declared_unit,
        quantity_proposal=quantity_proposal or {},
        match_status=outcome, candidates=cands, policy_version=POLICY_VERSION,
        idempotency_key=idempotency_key, request_hash=req, data_origin=data_origin,
    )  # fmt: skip
    session.add(obs)
    await session.flush()

    if outcome == "created_item":
        assert item
        emit(session, org_id=org, actor_id=actor.id, action="demand_item.created",
             entity_type="demand_item", entity_id=item.id, event_type="demand.item_created",
             aggregate_version=item.version, payload={"demand_item_id": str(item.id)},
             after=_item_snapshot(item))  # fmt: skip
    emit(session, org_id=org, actor_id=actor.id, action=f"observation.{outcome}",
         entity_type="demand_observation", entity_id=obs.id,
         event_type=("demand.observation_needs_review" if outcome == "needs_review"
                     else "demand.observation_added"),
         aggregate_version=item.version if item else None,
         payload={"demand_item_id": str(item.id) if item else None, "observation_id": str(obs.id),
                  "quantity_changed": False, "source": origin.source})  # fmt: skip
    return ObservationResult(
        obs, item, outcome, parsed, interpretation=interp, other_reports=other_reports
    )


def _other_report(
    item: DemandItem, relation: str, declared: tuple | None = None
) -> dict:
    return {
        "demand_item_id": str(item.id),
        "display_name": item.display_name,
        "reported_by": str(item.reported_by) if item.reported_by else None,
        "target_quantity": None if item.target_quantity is None else fmt(item.target_quantity),
        "quantity_unit": item.quantity_unit,
        # Cantidad SOLICITADA vigente (la última declarada en este reporte). No es la objetivo.
        "declared_quantity": None if declared is None else fmt(declared[0]),
        "declared_unit": None if declared is None else declared[1],
        "relation": relation,
    }


async def _latest_declared_by_item(
    session, org, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple]:
    """Última cantidad declarada por faltante. Cada faltante pertenece a un solo autor (D-135)."""
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            select(
                DemandObservation.demand_item_id,
                DemandObservation.declared_quantity,
                DemandObservation.declared_unit,
            )
            .where(
                DemandObservation.organization_id == org,
                DemandObservation.demand_item_id.in_(item_ids),
                DemandObservation.declared_quantity.is_not(None),
            )
            .ext(distinct_on(DemandObservation.demand_item_id))
            .order_by(
                DemandObservation.demand_item_id,
                DemandObservation.created_at.desc(),
                DemandObservation.id.desc(),
            )
        )
    ).all()
    return {r[0]: (r[1], r[2]) for r in rows}


async def item_in_cart(session, org, item: DemandItem) -> bool:
    """¿El faltante está en un carrito activo? Mientras siga ahí no se cambia su cantidad (D-147)."""
    cov = await coverage_svc.coverage_for_items(session, org, [item])
    return cov.get(item.id, {}).get("replenishment_state") == "in_quote"


async def _observation_by_key(session, org, key) -> DemandObservation | None:
    return (
        await session.execute(
            select(DemandObservation).where(
                DemandObservation.organization_id == org, DemandObservation.idempotency_key == key
            )
        )
    ).scalar_one_or_none()


async def _replay(session, prior: DemandObservation, req: str) -> ObservationResult:
    if prior.request_hash != req:
        raise DomainError(
            "IDEMPOTENCY_KEY_REUSED",
            "Esa clave de idempotencia ya se usó con otro contenido.",
            status_code=409,
        )
    item = await session.get(DemandItem, prior.demand_item_id) if prior.demand_item_id else None
    return ObservationResult(
        prior, item, prior.match_status, ParsedProduct.from_dict(prior.parsed), replayed=True
    )


# ---------------------------------------------------------------- confirmación humana


async def _assert_no_key_collision(
    session, item: DemandItem, key: str | None, ingredient: str | None = None
) -> None:
    if not key:
        return
    # La colisión solo importa dentro del mismo autor: dos personas pueden tener su propio
    # reporte del mismo producto (D-135).
    family = await _family_items(
        session, item.organization_id, item.cycle_id, ingredient or item.ingredient,
        reported_by=item.reported_by,
    )  # fmt: skip
    other = next((o for o in family if o.id != item.id and o.resolved_identity_key == key), None)
    if other:
        raise DomainError(
            "DUPLICATE_ITEM_EXISTS",
            "Con esa confirmación el producto coincide con otra necesidad. Aún no hay fusión manual: "
            "asocia el aviso a esa necesidad.",
            details={"existing_item_id": str(other.id), "existing": other.display_name},
        )


async def _remember_alias(
    session, actor, ingredient, unassigned, confirmed: ParsedProduct, obs_id
) -> bool:
    """Guarda 'cifra sin unidad = X' SOLO si una persona la confirmó y el atributo es sólido."""
    created = False
    solid = [
        (s.measure, "strength")
        for s in confirmed.strengths
        if s.basis in (Basis.EXPLICIT, Basis.CONFIRMED)
    ]
    if confirmed.content and confirmed.content.basis in (Basis.EXPLICIT, Basis.CONFIRMED):
        solid.append((confirmed.content.measure, "content"))
    for u in unassigned:
        if u.unit is not None:
            continue
        match = next(((m, role) for m, role in solid if m.value == u.value and m.unit_expr), None)
        if not match:
            continue
        sv = fmt(u.value)
        exists = (
            await session.execute(
                select(ProductAlias.id).where(
                    ProductAlias.organization_id == actor.organization_id,
                    ProductAlias.ingredient == ingredient,
                    ProductAlias.source_value == sv,
                    ProductAlias.status == "active",
                )
            )
        ).first()
        if exists:
            continue
        m, role = match
        a = ProductAlias(organization_id=actor.organization_id, ingredient=ingredient, source_value=sv,
                         assignment={"role": role, "unit_expr": m.unit_expr}, confirmed_by=actor.id,
                         confirmed_attributes=[f"{role}.unit"], source_observation_id=obs_id,
                         data_origin=origin_for(await org_kind(session, actor.organization_id)))  # fmt: skip
        session.add(a)
        await session.flush()
        emit(session, org_id=actor.organization_id, actor_id=actor.id, action="product_alias.created",
             entity_type="product_alias", entity_id=a.id, event_type="product_alias.created",
             aggregate_version=None, payload={"alias_id": str(a.id), "ingredient": ingredient},
             after={"source_value": sv, "assignment": a.assignment})  # fmt: skip
        created = True
    return created


async def resolve_observation(
    session: AsyncSession,
    actor: Actor,
    observation_id: uuid.UUID,
    *,
    demand_item_id: uuid.UUID | None,
    create_new: bool,
    apply_observed_attributes: bool = False,
    remember_alias: bool = False,
    as_ingredient: str | None = None,
) -> ObservationResult:
    """Una persona resuelve una coincidencia dudosa. Nunca fusiona productos incompatibles.

    `apply_observed_attributes`: confirma en la necesidad los datos explícitos del aviso que ella
    no tenía (p. ej. la unidad). `remember_alias`: recuerda «cifra sin unidad = X» para el futuro.
    """
    require(actor.roles, Permission.RESOLVE_OBSERVATION)
    if (demand_item_id is None) == (not create_new):
        raise DomainError(
            "INVALID_COMMAND", "Indica demand_item_id o create_new=true, no ambos.", status_code=400
        )
    org = actor.organization_id
    obs = (
        await session.execute(
            select(DemandObservation)
            .where(DemandObservation.id == observation_id, DemandObservation.organization_id == org)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if obs is None:
        raise not_found("La observación")
    if obs.match_status != "needs_review":
        raise DomainError("ALREADY_RESOLVED", "Esta observación ya fue resuelta.", status_code=409)

    parsed = ParsedProduct.from_dict(
        obs.parsed
    )  # lo que se interpretó al recibirlo (incluye alias de entonces)
    original_name = parsed.ingredient
    if as_ingredient:
        # Solo se puede elegir entre los nombres que la interpretación propuso: no es texto libre.
        proposed = {c.get("ingredient") for c in (obs.interpretation or {}).get("candidates", [])
                    if c.get("kind") in ("ingredient", "commercial_reference")}  # fmt: skip
        if as_ingredient not in proposed:
            raise DomainError(
                "INVALID_INGREDIENT_CHOICE", "Ese nombre no está entre los candidatos propuestos."
            )
        parsed = replace(parsed, ingredient=as_ingredient, name_anchor=False)
    await advisory_lock(session, f"fam:{org}:{obs.cycle_id}:{parsed.ingredient}")
    # La resolución tampoco cruza autores: la observación pertenece a su autor y solo puede
    # asociarse a una necesidad de esa persona (D-135).
    family = await _family_items(
        session, org, obs.cycle_id, parsed.ingredient, reported_by=obs.author_id
    )
    alias_created = False

    if create_new:
        if any(compare(parsed, parsed_of_item(c)).relation is Relation.IDENTICAL for c in family):
            raise DomainError(
                "DUPLICATE_ITEM_EXISTS",
                "Ya existe una necesidad idéntica; asocia la observación a ella.",
            )
        item = _new_item(org, obs.cycle_id, parsed, obs.data_origin, obs.author_id)
        await _assert_no_key_collision(session, item, item.resolved_identity_key)
        session.add(item)
        await session.flush()
        emit(session, org_id=org, actor_id=actor.id, action="demand_item.created",
             entity_type="demand_item", entity_id=item.id, event_type="demand.item_created",
             aggregate_version=item.version, payload={"demand_item_id": str(item.id)},
             after=_item_snapshot(item))  # fmt: skip
    else:
        item = next((c for c in family if c.id == demand_item_id), None)
        if item is None:
            other = (
                await session.execute(
                    select(DemandItem).where(
                        DemandItem.id == demand_item_id,
                        DemandItem.organization_id == org,
                        DemandItem.cycle_id == obs.cycle_id,
                        DemandItem.status == "active",
                    )
                )
            ).scalar_one_or_none()
            if other is not None:
                raise DomainError(
                    "CROSS_AUTHOR_ITEM",
                    "Esa necesidad es de otra persona. Cada reporte conserva su autor: crea uno nuevo.",
                    status_code=409,
                )
            raise not_found("La necesidad")
        current = parsed_of_item(item)
        if compare(parsed, current).relation is Relation.INCOMPATIBLE:
            raise DomainError(
                "INCOMPATIBLE_PRODUCTS",
                "Los productos difieren en dosis, forma, contenido o principio activo: no se pueden unir.",
                details={"observation": parsed.display_name, "item": item.display_name},
            )
        merged = current
        if apply_observed_attributes:
            merged = _absorb_observed(current, parsed)
            if merged != current:
                await _assert_no_key_collision(session, item, merged.resolved_identity_key)
                before = _item_snapshot(item)
                _apply_to_item(item, merged)
                item.version += 1
                emit(session, org_id=org, actor_id=actor.id, action="demand_item.attributes_confirmed",
                     entity_type="demand_item", entity_id=item.id,
                     event_type="demand.item_attributes_confirmed", aggregate_version=item.version,
                     payload={"demand_item_id": str(item.id), "quantity_changed": False},
                     before=before, after=_item_snapshot(item))  # fmt: skip
        if remember_alias:
            alias_created = await _remember_alias(
                session,
                actor,
                parsed.ingredient,
                list(parsed.unassigned) + list(current.unassigned),
                merged,
                obs.id,
            )
    if as_ingredient and remember_alias and original_name != as_ingredient:
        alias_created = (
            await create_expression_alias(session, actor, original_name, as_ingredient, quiet=True)
            or alias_created
        )
    obs.demand_item_id, obs.match_status = item.id, "resolved_manual"
    await session.flush()
    emit(session, org_id=org, actor_id=actor.id, action="observation.resolved",
         entity_type="demand_observation", entity_id=obs.id, event_type="demand.observation_resolved",
         aggregate_version=item.version,
         payload={"demand_item_id": str(item.id), "observation_id": str(obs.id),
                  "quantity_changed": False})  # fmt: skip
    return ObservationResult(obs, item, "resolved_manual", parsed, alias_created=alias_created)


def _absorb_observed(current: ParsedProduct, observed: ParsedProduct) -> ParsedProduct:
    """Datos explícitos/confirmados del aviso que cubren medidas sin unidad de la necesidad.

    Solo se toma lo que el aviso dijo con todas sus letras y que calza numéricamente con una
    medida pendiente: nunca se inventa ni se completa nada más.
    """
    pend = {m.value for m in current.unassigned}
    sol = (Basis.EXPLICIT, Basis.CONFIRMED)
    strengths = [
        s.measure for s in observed.strengths if s.basis in sol and s.measure.value in pend
    ]
    content = (
        observed.content.measure
        if observed.content
        and observed.content.basis in sol
        and observed.content.measure.value in pend
        else None
    )
    if not strengths and content is None:
        return current
    keep = [s.measure for s in current.strengths]
    return apply_confirmation(
        current, ConfirmPatch(strengths=keep + strengths if strengths else None, content=content)
    )


async def confirm_item_attributes(
    session: AsyncSession,
    actor: Actor,
    item_id: uuid.UUID,
    *,
    expected_version: int,
    patch: ConfirmPatch,
    remember_alias: bool = False,
) -> DemandItem:
    """Una persona autorizada confirma unidad, forma, contenido, etc. de una necesidad."""
    require(actor.roles, Permission.CONFIRM_ATTRIBUTES)
    org = actor.organization_id
    item = (
        await session.execute(
            select(DemandItem)
            .where(DemandItem.id == item_id, DemandItem.organization_id == org)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise not_found("La necesidad")
    if item.status != "active":
        raise DomainError("ITEM_NOT_ACTIVE", "La necesidad no está activa.")
    if item.version != expected_version:
        raise DomainError(
            "VERSION_CONFLICT", "La lista cambió. Revisa la versión actual antes de guardar.",
            status_code=409, details={"current_version": item.version},
        )  # fmt: skip
    await advisory_lock(session, f"fam:{org}:{item.cycle_id}:{item.ingredient}")
    current = parsed_of_item(item)
    new = apply_confirmation(current, patch)
    if new == current:
        raise DomainError("NO_CHANGE", "No hay nada nuevo que confirmar.")
    if (
        new.ingredient != current.ingredient
    ):  # el ancla por marca pasa a principio activo confirmado
        await advisory_lock(session, f"fam:{org}:{item.cycle_id}:{new.ingredient}")
    await _assert_no_key_collision(session, item, new.resolved_identity_key, new.ingredient)
    before = _item_snapshot(item)
    _apply_to_item(item, new)
    item.version += 1
    await session.flush()
    if remember_alias:
        await _remember_alias(session, actor, item.ingredient, list(current.unassigned), new, None)
    emit(session, org_id=org, actor_id=actor.id, action="demand_item.attributes_confirmed",
         entity_type="demand_item", entity_id=item.id, event_type="demand.item_attributes_confirmed",
         aggregate_version=item.version,
         payload={"demand_item_id": str(item.id), "quantity_changed": False},
         before=before, after=_item_snapshot(item))  # fmt: skip
    return item


# ---------------------------------------------------------------- cancelación de reportes


async def affected_quote_lines(
    session: AsyncSession, org: uuid.UUID, item_id: uuid.UUID
) -> list[dict]:
    """Líneas de cotización que asignan cantidad a este reporte. Solo informan: no se tocan."""
    rows = (
        await session.execute(
            select(
                QuoteLineAllocation.quote_line_id,
                QuoteLineAllocation.quantity,
                QuoteLineAllocation.coverage_relation,
                QuoteLine.quantity.label("line_quantity"),
                QuoteLine.quantity_unit,
                QuoteLine.supplier_id,
                Quote.status.label("quote_status"),
                QuoteCart.quote_id,
            )
            .join(QuoteLine, QuoteLine.id == QuoteLineAllocation.quote_line_id)
            .join(QuoteCart, QuoteCart.id == QuoteLine.quote_cart_id)
            .join(Quote, Quote.id == QuoteCart.quote_id)
            .where(
                QuoteLineAllocation.organization_id == org,
                QuoteLineAllocation.demand_item_id == item_id,
                QuoteLine.status == "active",
            )
            .order_by(QuoteCart.quote_id, QuoteLineAllocation.quote_line_id)
        )
    ).all()
    return [
        {
            "quote_line_id": r.quote_line_id,
            "quote_id": r.quote_id,
            "quote_status": r.quote_status,
            "supplier_id": r.supplier_id,
            "line_quantity": r.line_quantity,
            "quantity_unit": r.quantity_unit,
            "allocation_quantity": r.quantity,
            "coverage_relation": r.coverage_relation,
        }
        for r in rows
    ]


async def active_allocations_for_item(
    session: AsyncSession, org: uuid.UUID, item_id: uuid.UUID
) -> list[dict]:
    """Asignaciones **vigentes** de un reporte: línea activa en carrito activo.

    Es la misma noción de vigencia que usa la cobertura (§12.3): un carrito descartado o una línea
    retirada no cubren y, por tanto, no impiden bajar la cantidad pedida. La consulta no modifica
    nada; los servicios deciden si bloquean la operación.
    """
    rows = (
        await session.execute(
            select(
                QuoteLineAllocation.quantity,
                QuoteLineAllocation.quote_line_id,
                QuoteLine.quantity.label("line_quantity"),
                QuoteLine.quantity_unit,
                QuoteCart.quote_id,
                Quote.status.label("quote_status"),
            )
            .join(QuoteLine, QuoteLine.id == QuoteLineAllocation.quote_line_id)
            .join(QuoteCart, QuoteCart.id == QuoteLine.quote_cart_id)
            .join(Quote, Quote.id == QuoteCart.quote_id)
            .where(
                QuoteLineAllocation.organization_id == org,
                QuoteLineAllocation.demand_item_id == item_id,
                QuoteLine.status == "active",
                QuoteCart.status == "active",
            )
            .order_by(QuoteCart.quote_id, QuoteLineAllocation.quote_line_id)
        )
    ).all()
    return [
        {
            "quote_line_id": r.quote_line_id,
            "quote_id": r.quote_id,
            "quote_status": r.quote_status,
            "quantity": r.quantity,
            "line_quantity": r.line_quantity,
            "quantity_unit": r.quantity_unit,
        }
        for r in rows
    ]


async def _has_registered_purchase(session: AsyncSession, org: uuid.UUID, item_id: uuid.UUID) -> bool:
    """C5-B (0019) conectará aquí `purchase_line_allocation`. Hoy no existe una compra registrada."""
    return False


async def cancel_item(
    session: AsyncSession,
    actor: Actor,
    item_id: uuid.UUID,
    *,
    expected_version: int,
    reason: str | None,
) -> tuple[DemandItem, list[dict]]:
    """Cancelación lógica de un reporte. Nunca borra observaciones, cantidades ni historial.

    Si el reporte está asignado a líneas de cotización, se devuelven las líneas afectadas para que
    la administración corrija las asignaciones antes de cerrar la cotización. No se modifican líneas
    en silencio. Una compra registrada bloqueará la cancelación (`409 REPORT_HAS_PURCHASE`, C5-B).
    """
    from app.core.permissions import require_admin

    require_admin(actor.roles)
    org = actor.organization_id
    item = (
        await session.execute(
            select(DemandItem)
            .where(DemandItem.id == item_id, DemandItem.organization_id == org)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise not_found("El reporte")
    if item.status != "active":
        raise DomainError("REPORT_NOT_ACTIVE", "El reporte no está activo.", status_code=409)
    if item.version != expected_version:
        raise DomainError(
            "VERSION_CONFLICT",
            "El reporte cambió. Revisa la versión actual antes de cancelar.",
            status_code=409,
            details={"current_version": item.version},
        )
    if await _has_registered_purchase(session, org, item.id):
        raise DomainError(
            "REPORT_HAS_PURCHASE",
            "El reporte tiene una compra registrada: anula o corrige la compra antes de cancelarlo.",
            status_code=409,
        )
    affected = await affected_quote_lines(session, org, item.id)
    before = _item_snapshot(item)
    item.status = "cancelled"
    item.cancelled_at = datetime.now(UTC)
    item.cancelled_by = actor.id
    item.cancellation_reason = reason
    item.version += 1
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="demand_item.cancel",
        entity_type="demand_item",
        entity_id=item.id,
        event_type="demand.item_cancelled",
        aggregate_version=item.version,
        payload={
            "demand_item_id": str(item.id),
            "reason": reason,
            "affected_quote_lines": len(affected),
        },
        before=before,
        after=_item_snapshot(item),
    )
    await session.flush()
    return item, affected


# ---------------------------------------------------------------- alias verificados


async def list_aliases(
    session: AsyncSession, actor: Actor, *, active_only: bool = True
) -> list[ProductAlias]:
    require(actor.roles, Permission.CONFIRM_ATTRIBUTES)
    q = select(ProductAlias).where(ProductAlias.organization_id == actor.organization_id)
    if active_only:
        q = q.where(ProductAlias.status == "active")
    return list((await session.execute(q.order_by(ProductAlias.created_at))).scalars())


async def deactivate_alias(
    session: AsyncSession, actor: Actor, alias_id: uuid.UUID
) -> ProductAlias:
    require(actor.roles, Permission.CONFIRM_ATTRIBUTES)
    a = (
        await session.execute(
            select(ProductAlias)
            .where(
                ProductAlias.id == alias_id, ProductAlias.organization_id == actor.organization_id
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if a is None:
        raise not_found("El alias")
    if a.status == "active":
        from sqlalchemy import func

        a.status, a.deactivated_at = "inactive", func.now()
        await session.flush()
        emit(session, org_id=actor.organization_id, actor_id=actor.id, action="product_alias.deactivated",
             entity_type="product_alias", entity_id=a.id, event_type="product_alias.deactivated",
             aggregate_version=None, payload={"alias_id": str(a.id)})  # fmt: skip
    return a


async def create_expression_alias(
    session: AsyncSession, actor: Actor, phrase: str, ingredient: str, *, quiet: bool = False
) -> bool:
    """«la D» = «vitamina d»: el auxiliar nombra así un producto. Equivalencia de NOMBRE entre dos
    textos de la misma farmacia; no afirma ninguna equivalencia clínica. Solo la crea compras."""
    require(actor.roles, Permission.CONFIRM_ATTRIBUTES)
    org = actor.organization_id
    ph, tg = normalize_name(phrase), normalize_name(ingredient)
    if not ph or not tg or ph == tg:
        raise DomainError(
            "INVALID_ALIAS",
            "La expresión y el producto deben ser distintos y no vacíos.",
            status_code=400,
        )
    await advisory_lock(session, f"alias:{org}:{ph}")
    exists = (
        await session.execute(
            select(ProductAlias.id).where(
                ProductAlias.organization_id == org, ProductAlias.kind == "expression",
                ProductAlias.ingredient == ph, ProductAlias.status == "active",
            )
        )
    ).first()  # fmt: skip
    if exists:
        if quiet:
            return False
        raise DomainError(
            "ALIAS_EXISTS", "Ya hay un alias activo para esa expresión.", status_code=409
        )
    a = ProductAlias(organization_id=org, kind="expression", ingredient=ph, source_value="",
                     assignment={"ingredient": tg}, confirmed_by=actor.id,
                     confirmed_attributes=["ingredient_name"],
                     data_origin=origin_for(await org_kind(session, org)))  # fmt: skip
    session.add(a)
    await session.flush()
    emit(session, org_id=org, actor_id=actor.id, action="product_alias.created", entity_type="product_alias",
         entity_id=a.id, event_type="product_alias.created", aggregate_version=None,
         payload={"alias_id": str(a.id), "kind": "expression"}, after={"phrase": ph, "ingredient": tg})  # fmt: skip
    return True
