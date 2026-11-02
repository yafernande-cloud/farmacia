"""E9 — criterios comerciales y notas (C3, contrato C0 §3.5 y §E9; D-091).

Dos registros, un solo recurso con `kind`:

  * `note`      observación escrita por la administración. Efecto **ninguno**; se muestra.
  * `criterion` regla confirmada. Efecto declarado (`prefer`, `deprioritize`, `do_not_recommend`,
                `requires_review`), pero **solo anota**: nunca altera precios, totales, cotas,
                elegibilidad, causas ni el orden de los candidatos (AC18/AC30/C0-18).

No hay estado `proposed`: lo no confirmado es, por definición, una nota. Una nota se convierte en
criterio creando un registro nuevo y desactivando la nota; el registro original no se muta.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.core.permissions import require_admin
from app.models import CommercialCriterion, ComparisonRun, OfferSnapshot, Supplier, TradeItem
from app.services.actors import Actor
from app.services.events import emit
from app.services.provenance import org_kind, origin_for
from app.services.suppliers import normalize_name

KINDS = ("note", "criterion")
CRITERION_EFFECTS = ("prefer", "deprioritize", "do_not_recommend", "requires_review")
TARGET_LEVELS = ("organization", "ingredient", "article", "supplier", "supplier_article")
CONDITION_KINDS = ("always", "min_quantity", "max_price", "date_window")


# --------------------------------------------------------------------------- formas públicas


def _money(amount: Decimal | None, currency: str | None) -> dict[str, str] | None:
    if amount is None or currency is None:
        return None
    return {"amount": f"{amount.quantize(Decimal('0.01'))}", "currency": currency}


def criterion_payload(c: CommercialCriterion) -> dict[str, Any]:
    """`CommercialCriterionOut` (C0 §4.5). Nunca se inventa un dato: lo desconocido es `null`."""
    max_price = None
    if c.condition_max_price_amount is not None:
        max_price = {
            "amount": f"{c.condition_max_price_amount.quantize(Decimal('0.01'))}",
            "currency": c.condition_max_price_currency,
            "basis": c.condition_max_price_basis,
        }
    date_window = None
    if c.condition_date_from is not None or c.condition_date_to is not None:
        date_window = {"from": c.condition_date_from, "to": c.condition_date_to}
    return {
        "criterion_id": c.id,
        "kind": c.kind,
        "status": c.status,
        "origin": c.origin,
        "effect": c.effect,
        "target": {
            "level": c.target_level,
            "supplier_id": c.target_supplier_id,
            "article_id": c.target_article_id,
            "ingredient": c.target_ingredient,
        },
        "condition": {
            "kind": c.condition_kind,
            "min_quantity": (
                str(c.condition_min_quantity) if c.condition_min_quantity is not None else None
            ),
            "max_price": max_price,
            "date_window": date_window,
        },
        "reason_text": c.reason_text,
        "source": {
            "kind": c.source_kind,
            "comparison_id": c.source_comparison_id,
            "offer_id": c.source_offer_id,
            "note_criterion_id": c.source_note_criterion_id,
            "note_version": c.source_note_version,
        },
        "validity": {"valid_from": c.validity_from, "valid_until": c.validity_until},
        "version": c.version,
        "created_by": c.created_by,
        "created_at": c.created_at,
        "confirmed_by": c.confirmed_by,
        "confirmed_at": c.confirmed_at,
        "deactivated_by": c.deactivated_by,
        "deactivated_at": c.deactivated_at,
        "deactivation_reason": c.deactivation_reason,
        "priority": c.priority,
    }


def annotation_payload(
    c: CommercialCriterion, *, condition_met: bool | None, condition_status: str
) -> dict[str, Any]:
    """Anotación de E3/E5/E6. Se adjunta; jamás modifica un número."""
    return {
        "criterion_id": c.id,
        "effect": c.effect,
        "reason_text": c.reason_text,
        "target_level": c.target_level,
        "condition": {
            "kind": c.condition_kind,
            "min_quantity": (
                str(c.condition_min_quantity) if c.condition_min_quantity is not None else None
            ),
            "max_price": _money(
                c.condition_max_price_amount, c.condition_max_price_currency
            ),
            "date_window": (
                {"from": c.condition_date_from, "to": c.condition_date_to}
                if c.condition_date_from or c.condition_date_to
                else None
            ),
        },
        "condition_met": condition_met,
        "condition_status": condition_status,
        "confirmed_by": c.confirmed_by,
        "confirmed_at": c.confirmed_at,
        "version": c.version,
    }


def note_payload(c: CommercialCriterion) -> dict[str, Any]:
    return {
        "criterion_id": c.id,
        "origin": c.origin,
        "reason_text": c.reason_text,
        "target_level": c.target_level,
        "created_by": c.created_by,
        "created_at": c.created_at,
        "version": c.version,
    }


# --------------------------------------------------------------------------- vigencia y alcance


def is_active_now(c: CommercialCriterion, now: datetime) -> bool:
    """Vigente = `active` y dentro de `validity`. «Expirado» es derivado, no un estado guardado."""
    if c.status != "active":
        return False
    if c.validity_from is not None and c.validity_from > now:
        return False
    return not (c.validity_until is not None and c.validity_until < now)


@dataclass(frozen=True)
class Scope:
    """Contexto de aplicación: a qué se anota. Un campo `None` no coincide con nada."""

    supplier_id: uuid.UUID | None = None
    article_id: uuid.UUID | None = None
    ingredient: str | None = None
    ingredient_basis: str = "unknown"
    # Cantidad comprada del candidato (solo a nivel de candidato; el proveedor no tiene una).
    quantity: Decimal | None = None
    price_amount: Decimal | None = None
    price_currency: str | None = None
    price_basis: str | None = None


def _target_matches(c: CommercialCriterion, scope: Scope) -> tuple[bool, bool]:
    """(coincide, desconocido). Nunca se amplía el ámbito confirmado."""
    level = c.target_level
    if level == "organization":
        return True, False
    if level == "supplier":
        return (
            (scope.supplier_id is not None and scope.supplier_id == c.target_supplier_id),
            False,
        )
    if level == "article":
        return (
            (scope.article_id is not None and scope.article_id == c.target_article_id),
            False,
        )
    if level == "supplier_article":
        matched = (
            scope.supplier_id is not None
            and scope.supplier_id == c.target_supplier_id
            and scope.article_id is not None
            and scope.article_id == c.target_article_id
        )
        return matched, False
    # ingredient: solo procedencia explícita o confirmada; lo inferido/desconocido no se aplica.
    if scope.ingredient_basis not in ("explicit", "confirmed"):
        return False, True
    if scope.ingredient is None or c.target_ingredient is None:
        return False, True
    return (
        normalize_name(scope.ingredient) == normalize_name(c.target_ingredient),
        False,
    )


def _condition(c: CommercialCriterion, scope: Scope, now: datetime) -> tuple[bool | None, str]:
    """(cumplida, estado) de la condición, que se evalúa **además** del ámbito (AND)."""
    if c.condition_kind == "always":
        return True, "met"
    if c.condition_kind == "min_quantity":
        if scope.quantity is None or c.condition_min_quantity is None:
            return None, "unknown"
        met = scope.quantity >= c.condition_min_quantity
        return met, ("met" if met else "not_met")
    if c.condition_kind == "max_price":
        if (
            scope.price_amount is None
            or scope.price_currency is None
            or scope.price_basis is None
            or c.condition_max_price_amount is None
        ):
            return None, "unknown"
        if scope.price_currency != c.condition_max_price_currency:
            return None, "unknown"
        if scope.price_basis != c.condition_max_price_basis:
            return None, "unknown"
        met = scope.price_amount <= c.condition_max_price_amount
        return met, ("met" if met else "not_met")
    # date_window
    if c.condition_date_from is None and c.condition_date_to is None:
        return None, "unknown"
    if c.condition_date_from is not None and now < c.condition_date_from:
        return False, "not_met"
    if c.condition_date_to is not None and now > c.condition_date_to:
        return False, "not_met"
    return True, "met"


def annotations_for(
    criteria: list[CommercialCriterion], scope: Scope, *, now: datetime
) -> tuple[list[dict], list[dict]]:
    """Anotaciones y notas vigentes que aplican a este ámbito. Solo lectura."""
    annotations: list[dict] = []
    notes: list[dict] = []
    for c in criteria:
        if not is_active_now(c, now):
            continue
        matched, target_unknown = _target_matches(c, scope)
        if not matched:
            continue
        met, status = _condition(c, scope, now)
        if target_unknown:
            met, status = None, "unknown"
        if c.kind == "note":
            # Una nota se muestra como contexto; su condición no la convierte en regla.
            notes.append(note_payload(c))
            continue
        if met is not True:
            continue
        annotations.append(annotation_payload(c, condition_met=met, condition_status=status))
    return annotations, notes


async def active_criteria(
    session: AsyncSession, org: uuid.UUID, *, now: datetime
) -> list[CommercialCriterion]:
    rows = (
        (
            await session.execute(
                select(CommercialCriterion)
                .where(
                    CommercialCriterion.organization_id == org,
                    CommercialCriterion.status == "active",
                )
                .order_by(CommercialCriterion.created_at, CommercialCriterion.id)
            )
        )
        .scalars()
        .all()
    )
    return [c for c in rows if is_active_now(c, now)]


# --------------------------------------------------------------------------- validación


def _fail(code: str, message: str, **details) -> DomainError:
    return DomainError(code, message, status_code=422, details=details or None)


def _condition_fields(condition: dict | None) -> dict[str, Any]:
    condition = condition or {}
    kind = condition.get("kind") or "always"
    if kind not in CONDITION_KINDS:
        raise _fail("INVALID_CONDITION", "El tipo de condición no es válido.")
    fields: dict[str, Any] = {
        "condition_kind": kind,
        "condition_min_quantity": None,
        "condition_max_price_amount": None,
        "condition_max_price_currency": None,
        "condition_max_price_basis": None,
        "condition_date_from": None,
        "condition_date_to": None,
    }
    if kind == "min_quantity":
        raw = condition.get("min_quantity")
        if raw is None:
            raise _fail("INVALID_CONDITION", "La condición por cantidad exige `min_quantity`.")
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            raise _fail("INVALID_CONDITION", "`min_quantity` debe ser un número.") from None
        if value <= 0:
            raise _fail("INVALID_CONDITION", "`min_quantity` debe ser mayor que cero.")
        fields["condition_min_quantity"] = value
    if kind == "max_price":
        price = condition.get("max_price") or {}
        try:
            amount = Decimal(str(price.get("amount")))
        except (InvalidOperation, ValueError, TypeError):
            raise _fail("INVALID_CONDITION", "La condición por precio exige un monto.") from None
        currency = price.get("currency")
        basis = price.get("basis")
        if not currency or not basis:
            # Sin moneda y base comparables la regla no se puede confirmar (C0 §4.5).
            raise _fail(
                "CRITERION_INCOMPLETE",
                "La condición por precio exige moneda y base del precio.",
            )
        if basis not in ("per_commercial_unit", "per_base_unit"):
            raise _fail("INVALID_CONDITION", "La base del precio no es válida.")
        fields["condition_max_price_amount"] = amount
        fields["condition_max_price_currency"] = str(currency).upper()
        fields["condition_max_price_basis"] = basis
    if kind == "date_window":
        start = condition.get("from")
        end = condition.get("to")
        if start is None and end is None:
            raise _fail("INVALID_CONDITION", "La ventana de fechas exige un inicio o un fin.")
        if start is not None and end is not None and end <= start:
            raise _fail("INVALID_CONDITION", "La ventana de fechas termina antes de empezar.")
        fields["condition_date_from"] = start
        fields["condition_date_to"] = end
    return fields


def _validity_fields(validity: dict | None) -> dict[str, Any]:
    validity = validity or {}
    start, end = validity.get("valid_from"), validity.get("valid_until")
    if start is not None and end is not None and end <= start:
        raise _fail("INVALID_VALIDITY", "La vigencia termina antes de empezar.")
    return {"validity_from": start, "validity_until": end}


async def _target_fields(
    session: AsyncSession, org: uuid.UUID, target: dict | None
) -> dict[str, Any]:
    target = target or {}
    level = target.get("level")
    if level not in TARGET_LEVELS:
        raise _fail("CRITERION_INCOMPLETE", "El criterio exige un ámbito válido.")
    fields: dict[str, Any] = {
        "target_level": level,
        "target_ingredient": None,
        "target_article_id": None,
        "target_supplier_id": None,
    }
    if level == "ingredient":
        ingredient = (target.get("ingredient") or "").strip()
        if not ingredient:
            raise _fail("CRITERION_INCOMPLETE", "El ámbito por principio exige el principio activo.")
        fields["target_ingredient"] = ingredient
        return fields
    if level in ("article", "supplier_article"):
        article_id = target.get("article_id")
        if article_id is None:
            raise _fail("CRITERION_INCOMPLETE", "El ámbito por artículo exige `article_id`.")
        exists = (
            await session.execute(
                select(TradeItem.id).where(
                    TradeItem.id == article_id, TradeItem.organization_id == org
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            raise not_found("El artículo")
        fields["target_article_id"] = article_id
    if level in ("supplier", "supplier_article"):
        supplier_id = target.get("supplier_id")
        if supplier_id is None:
            raise _fail("CRITERION_INCOMPLETE", "El ámbito por proveedor exige `supplier_id`.")
        exists = (
            await session.execute(
                select(Supplier.id).where(
                    Supplier.id == supplier_id, Supplier.organization_id == org
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            raise not_found("El proveedor")
        fields["target_supplier_id"] = supplier_id
    return fields


async def _source_fields(session: AsyncSession, org: uuid.UUID, source: dict | None) -> dict:
    """Solo `source.comparison_id`/`offer_id`: el cliente no dicta `kind` ni autores."""
    source = source or {}
    comparison_id, offer_id = source.get("comparison_id"), source.get("offer_id")
    if comparison_id is not None:
        exists = (
            await session.execute(
                select(ComparisonRun.id).where(
                    ComparisonRun.id == comparison_id,
                    ComparisonRun.organization_id == org,
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            raise not_found("La comparación")
    if offer_id is not None:
        exists = (
            await session.execute(
                select(OfferSnapshot.id).where(
                    OfferSnapshot.id == offer_id, OfferSnapshot.organization_id == org
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            raise not_found("La oferta")
    return {
        "source_kind": "evaluation" if (comparison_id or offer_id) else "manual",
        "source_comparison_id": comparison_id,
        "source_offer_id": offer_id,
    }


def _clean_reason(text: str | None) -> str:
    reason = (text or "").strip()
    if not reason:
        raise _fail("CRITERION_INCOMPLETE", "El criterio exige un motivo escrito.")
    if len(reason) > 2000:
        raise _fail("INVALID_REASON", "El motivo no puede superar 2000 caracteres.")
    return reason


def _snapshot(c: CommercialCriterion) -> dict:
    return {
        "kind": c.kind,
        "status": c.status,
        "effect": c.effect,
        "target_level": c.target_level,
        "reason_text": c.reason_text,
        "version": c.version,
    }


# --------------------------------------------------------------------------- operaciones


async def create_criterion(
    session: AsyncSession,
    actor: Actor,
    *,
    kind: str,
    effect: str | None,
    target: dict | None,
    reason_text: str,
    condition: dict | None = None,
    validity: dict | None = None,
    source: dict | None = None,
    confirm: bool = False,
) -> CommercialCriterion:
    require_admin(actor.roles)
    org = actor.organization_id
    if kind not in KINDS:
        raise _fail("VALIDATION_ERROR", "El tipo debe ser `note` o `criterion`.")
    if kind == "note":
        if confirm:
            # Una nota no se convierte en criterio al crearla: se confirma explícitamente después.
            raise _fail("CRITERION_INCOMPLETE", "Una nota no se crea confirmada.")
        if effect not in (None, "note_only"):
            raise _fail(
                "CRITERION_EFFECT_NOT_ALLOWED", "Una nota solo admite el efecto `note_only`."
            )
        effect = "note_only"
        confirmed_by = confirmed_at = None
    else:
        if not confirm:
            # Crear un criterio es consciente y explícito (C0 §E9).
            raise _fail(
                "CRITERION_INCOMPLETE",
                "Crear un criterio exige `confirm: true`.",
            )
        if effect not in CRITERION_EFFECTS:
            raise _fail(
                "CRITERION_INCOMPLETE",
                "Un criterio exige un efecto: prefer, deprioritize, do_not_recommend "
                "o requires_review.",
            )
        confirmed_by, confirmed_at = actor.id, datetime.now(UTC)
    reason = _clean_reason(reason_text)
    row = CommercialCriterion(
        organization_id=org,
        kind=kind,
        status="active",
        origin="explicit",
        effect=effect,
        reason_text=reason,
        created_by=actor.id,
        confirmed_by=confirmed_by,
        confirmed_at=confirmed_at,
        data_origin=origin_for(await org_kind(session, org)),
        **await _target_fields(session, org, target),
        **_condition_fields(condition),
        **_validity_fields(validity),
        **await _source_fields(session, org, source),
    )
    session.add(row)
    await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="criterion.created",
        entity_type="commercial_criterion",
        entity_id=row.id,
        event_type="criterion.created",
        aggregate_version=1,
        payload={"criterion_id": str(row.id), "kind": row.kind, "effect": row.effect},
        after=_snapshot(row),
    )
    return row


async def _load_for_update(
    session: AsyncSession, actor: Actor, criterion_id: uuid.UUID
) -> CommercialCriterion:
    row = (
        await session.execute(
            select(CommercialCriterion)
            .where(
                CommercialCriterion.id == criterion_id,
                CommercialCriterion.organization_id == actor.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("El criterio")
    return row


def _check_version(row: CommercialCriterion, expected: int | None) -> None:
    """`If-Match` obligatorio; dos transiciones no pueden pisarse (C0 §2.6 y §E9)."""
    if expected is None:
        raise DomainError(
            "VERSION_REQUIRED",
            "Esta operación exige `If-Match` con la versión actual.",
            status_code=428,
        )
    if row.version != expected:
        raise DomainError(
            "VERSION_CONFLICT",
            "El criterio cambió. Revisa la versión actual antes de guardar.",
            status_code=409,
            details={"current_version": row.version},
        )


async def get_criterion(
    session: AsyncSession, actor: Actor, criterion_id: uuid.UUID
) -> CommercialCriterion:
    require_admin(actor.roles)
    row = (
        await session.execute(
            select(CommercialCriterion).where(
                CommercialCriterion.id == criterion_id,
                CommercialCriterion.organization_id == actor.organization_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("El criterio")
    return row


async def list_criteria(
    session: AsyncSession,
    actor: Actor,
    *,
    kind: str | None = None,
    status: str | None = None,
    target_level: str | None = None,
    supplier_id: uuid.UUID | None = None,
    article_id: uuid.UUID | None = None,
    include_inactive: bool = False,
) -> list[CommercialCriterion]:
    require_admin(actor.roles)
    q = select(CommercialCriterion).where(
        CommercialCriterion.organization_id == actor.organization_id
    )
    if kind is not None:
        if kind not in KINDS:
            raise _fail("INVALID_KIND", "El tipo debe ser `note` o `criterion`.")
        q = q.where(CommercialCriterion.kind == kind)
    if status is not None:
        if status not in ("active", "inactive"):
            raise _fail("INVALID_STATUS", "El estado debe ser `active` o `inactive`.")
        q = q.where(CommercialCriterion.status == status)
    elif not include_inactive:
        q = q.where(CommercialCriterion.status == "active")
    if target_level is not None:
        if target_level not in TARGET_LEVELS:
            raise _fail("INVALID_TARGET", "El ámbito no es válido.")
        q = q.where(CommercialCriterion.target_level == target_level)
    if supplier_id is not None:
        q = q.where(CommercialCriterion.target_supplier_id == supplier_id)
    if article_id is not None:
        q = q.where(CommercialCriterion.target_article_id == article_id)
    q = q.order_by(CommercialCriterion.created_at, CommercialCriterion.id)
    return list((await session.execute(q)).scalars().all())


async def update_criterion(
    session: AsyncSession,
    actor: Actor,
    criterion_id: uuid.UUID,
    *,
    expected_version: int | None,
    changes: dict,
) -> CommercialCriterion:
    """Editar un criterio **sube la versión** y se audita: afecta anotaciones futuras."""
    require_admin(actor.roles)
    row = await _load_for_update(session, actor, criterion_id)
    _check_version(row, expected_version)
    before = _snapshot(row)
    if "reason_text" in changes:
        row.reason_text = _clean_reason(changes["reason_text"])
    if "target" in changes:
        for field, value in (
            await _target_fields(session, actor.organization_id, changes["target"])
        ).items():
            setattr(row, field, value)
    if "condition" in changes:
        for field, value in _condition_fields(changes["condition"]).items():
            setattr(row, field, value)
    if "validity" in changes:
        for field, value in _validity_fields(changes["validity"]).items():
            setattr(row, field, value)
    row.version += 1
    row.updated_at = datetime.now(UTC)
    await session.flush()
    emit(
        session,
        org_id=actor.organization_id,
        actor_id=actor.id,
        action="criterion.updated",
        entity_type="commercial_criterion",
        entity_id=row.id,
        event_type="criterion.updated",
        aggregate_version=row.version,
        payload={"criterion_id": str(row.id), "kind": row.kind},
        before=before,
        after=_snapshot(row),
    )
    return row


async def confirm_note(
    session: AsyncSession,
    actor: Actor,
    note_id: uuid.UUID,
    *,
    expected_version: int | None,
    effect: str,
    target: dict | None = None,
    reason_text: str | None = None,
    condition: dict | None = None,
    validity: dict | None = None,
) -> CommercialCriterion:
    """Convierte una nota en criterio **sin mutar la nota**: crea otro registro y la desactiva."""
    require_admin(actor.roles)
    org = actor.organization_id
    note = await _load_for_update(session, actor, note_id)
    if note.kind != "note":
        raise DomainError(
            "CRITERION_NOT_NOTE", "Solo una nota se puede convertir en criterio.", status_code=409
        )
    _check_version(note, expected_version)
    if note.status != "active":
        raise DomainError("CRITERION_NOT_ACTIVE", "La nota ya no está activa.", status_code=409)
    already = (
        await session.execute(
            select(CommercialCriterion.id).where(
                CommercialCriterion.source_note_criterion_id == note.id
            )
        )
    ).scalar_one_or_none()
    if already is not None:
        raise DomainError(
            "CRITERION_NOT_ACTIVE",
            "La nota ya fue convertida en criterio.",
            status_code=409,
            details={"criterion_id": str(already)},
        )
    if effect not in CRITERION_EFFECTS:
        raise _fail("CRITERION_INCOMPLETE", "Un criterio exige un efecto válido.")
    now = datetime.now(UTC)
    row = CommercialCriterion(
        organization_id=org,
        kind="criterion",
        status="active",
        origin="explicit",
        effect=effect,
        reason_text=_clean_reason(reason_text if reason_text is not None else note.reason_text),
        source_kind="note",
        source_note_criterion_id=note.id,
        source_note_version=note.version,
        created_by=actor.id,
        confirmed_by=actor.id,
        confirmed_at=now,
        data_origin=origin_for(await org_kind(session, org)),
        **await _target_fields(
            session,
            org,
            target
            if target is not None
            else {
                "level": note.target_level,
                "ingredient": note.target_ingredient,
                "article_id": note.target_article_id,
                "supplier_id": note.target_supplier_id,
            },
        ),
        **_condition_fields(
            condition
            if condition is not None
            else {
                "kind": note.condition_kind,
                "min_quantity": note.condition_min_quantity,
                "max_price": (
                    {
                        "amount": note.condition_max_price_amount,
                        "currency": note.condition_max_price_currency,
                        "basis": note.condition_max_price_basis,
                    }
                    if note.condition_max_price_amount is not None
                    else None
                ),
                "from": note.condition_date_from,
                "to": note.condition_date_to,
            }
        ),
        **_validity_fields(
            validity
            if validity is not None
            else {"valid_from": note.validity_from, "valid_until": note.validity_until}
        ),
    )
    session.add(row)
    await session.flush()
    before = _snapshot(note)
    note.status = "inactive"
    note.deactivated_by = actor.id
    note.deactivated_at = now
    note.deactivation_reason = f"convertida en criterio {row.id}"
    note.version += 1
    note.updated_at = now
    await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="criterion.confirmed",
        entity_type="commercial_criterion",
        entity_id=row.id,
        event_type="criterion.confirmed",
        aggregate_version=row.version,
        payload={
            "criterion_id": str(row.id),
            "note_criterion_id": str(note.id),
            "note_version": row.source_note_version,
        },
        before=before,
        after=_snapshot(row),
    )
    return row


async def deactivate_criterion(
    session: AsyncSession,
    actor: Actor,
    criterion_id: uuid.UUID,
    *,
    expected_version: int | None,
    reason: str,
) -> CommercialCriterion:
    require_admin(actor.roles)
    row = await _load_for_update(session, actor, criterion_id)
    _check_version(row, expected_version)
    if row.status != "active":
        raise DomainError("CRITERION_NOT_ACTIVE", "El registro ya está inactivo.", status_code=409)
    now = datetime.now(UTC)
    before = _snapshot(row)
    row.status = "inactive"
    row.deactivated_by = actor.id
    row.deactivated_at = now
    row.deactivation_reason = _clean_reason(reason)
    row.version += 1
    row.updated_at = now
    await session.flush()
    emit(
        session,
        org_id=actor.organization_id,
        actor_id=actor.id,
        action="criterion.deactivated",
        entity_type="commercial_criterion",
        entity_id=row.id,
        event_type="criterion.deactivated",
        aggregate_version=row.version,
        payload={"criterion_id": str(row.id), "reason": row.deactivation_reason},
        before=before,
        after=_snapshot(row),
    )
    return row


async def reactivate_criterion(
    session: AsyncSession, actor: Actor, criterion_id: uuid.UUID, *, expected_version: int | None
) -> CommercialCriterion:
    require_admin(actor.roles)
    row = await _load_for_update(session, actor, criterion_id)
    _check_version(row, expected_version)
    if row.status != "inactive":
        raise DomainError("CRITERION_NOT_ACTIVE", "El registro ya está activo.", status_code=409)
    # Nota y criterio derivado nunca están activos a la vez (C0 §E9).
    if row.kind == "note":
        derived = (
            await session.execute(
                select(CommercialCriterion.id).where(
                    CommercialCriterion.source_note_criterion_id == row.id,
                    CommercialCriterion.status == "active",
                )
            )
        ).scalar_one_or_none()
        if derived is not None:
            raise DomainError(
                "CRITERION_NOTE_SUPERSEDED",
                "La nota fue convertida en un criterio vigente; desactívalo primero.",
                status_code=409,
                details={"criterion_id": str(derived)},
            )
    else:
        note_id = row.source_note_criterion_id
        if note_id is not None:
            note = (
                await session.execute(
                    select(CommercialCriterion).where(CommercialCriterion.id == note_id)
                )
            ).scalar_one_or_none()
            if note is not None and note.status == "active":
                raise DomainError(
                    "CRITERION_NOTE_SUPERSEDED",
                    "La nota de origen está activa; desactívala primero.",
                    status_code=409,
                    details={"criterion_id": str(note.id)},
                )
    before = _snapshot(row)
    row.status = "active"
    row.deactivated_by = None
    row.deactivated_at = None
    row.deactivation_reason = None
    row.version += 1
    row.updated_at = datetime.now(UTC)
    await session.flush()
    emit(
        session,
        org_id=actor.organization_id,
        actor_id=actor.id,
        action="criterion.reactivated",
        entity_type="commercial_criterion",
        entity_id=row.id,
        event_type="criterion.reactivated",
        aggregate_version=row.version,
        payload={"criterion_id": str(row.id)},
        before=before,
        after=_snapshot(row),
    )
    return row
