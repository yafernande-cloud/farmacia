"""Comandos de cantidad SET/ADD/CLEAR con permisos, versión e idempotencia (HLD §14.2)."""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.core.permissions import Permission, Role, require, require_admin
from app.domain.quantities import Quantity, QuantityOp, QuantityUnit, apply_command, fmt_qty
from app.models import DemandItem, QuantityEvent
from app.services.actors import Actor
from app.services.demand import active_allocations_for_item
from app.services.events import advisory_lock, emit, request_hash


@dataclass
class QuantityResult:
    item: DemandItem
    event: QuantityEvent
    replayed: bool = False


def _q(value, unit) -> Quantity | None:
    return None if value is None else Quantity(Decimal(value), QuantityUnit(unit))


async def apply_quantity_command(
    session: AsyncSession,
    actor: Actor,
    item_id: uuid.UUID,
    *,
    operation: QuantityOp,
    value: Decimal | None,
    unit: QuantityUnit | None,
    reason: str | None,
    idempotency_key: str,
    expected_version: int,
    web_admin: bool = False,
) -> QuantityResult:
    """`web_admin` abre la vía web (D-130/D-135): `admin` + `StrongActor`, sin cambiar la matriz.

    El auxiliar sigue proponiendo y `purchasing` aplicando desde Telegram. La administración web
    puede fijar la cantidad de un reporte porque es quien corrige y cierra; la API exige además
    autenticación fuerte y no se amplía ningún rol funcional.
    """
    if web_admin and Role.ADMIN.value in (actor.roles or []):
        require_admin(actor.roles)
    else:
        web_admin = False
        require(actor.roles, Permission.APPLY_QUANTITY)
    org = actor.organization_id
    req = request_hash(
        {
            "item": item_id,
            "op": operation,
            "v": value,
            "u": unit,
            "r": reason,
            "ver": expected_version,
        }
    )

    # Idempotencia primero: repetir la misma clave y contenido devuelve el resultado original,
    # aunque la versión haya avanzado desde entonces (un ADD repetido no suma dos veces).
    await advisory_lock(session, f"idem:{org}:{idempotency_key}")
    prior = (
        await session.execute(
            select(QuantityEvent).where(
                QuantityEvent.organization_id == org,
                QuantityEvent.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if prior:
        if prior.request_hash != req:
            raise DomainError(
                "IDEMPOTENCY_KEY_REUSED",
                "Esa clave de idempotencia ya se usó con otro contenido.",
                status_code=409,
            )
        item = await session.get(DemandItem, prior.demand_item_id)
        assert item
        return QuantityResult(item, prior, replayed=True)

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
            "VERSION_CONFLICT",
            "La lista cambió. Revisa la versión actual antes de guardar.",
            status_code=409,
            details={
                "current_version": item.version,
                "current_quantity": fmt_qty(item.target_quantity),
                "current_unit": item.quantity_unit,
            },
        )

    before = _q(item.target_quantity, item.quantity_unit)
    after = apply_command(before, operation, value, unit)

    # Integridad con las asignaciones de cotización (D-137). No se reasigna ni se corrige nada
    # solo: la administración debe ajustar o quitar las asignaciones afectadas primero.
    allocations = await active_allocations_for_item(session, org, item.id)
    if allocations:
        allocated = sum((a["quantity"] for a in allocations), Decimal(0))
        details = {
            "allocated": fmt_qty(allocated),
            "unit": item.quantity_unit,
            "allocations": [
                {
                    "quote_id": str(a["quote_id"]),
                    "quote_status": a["quote_status"],
                    "quote_line_id": str(a["quote_line_id"]),
                    "quantity": fmt_qty(a["quantity"]),
                }
                for a in allocations
            ],
        }
        if after is None:
            raise DomainError(
                "REPORT_HAS_ALLOCATIONS",
                "El reporte está asignado a una cotización. Ajusta o quita esas asignaciones "
                "antes de dejar la cantidad sin definir.",
                status_code=409,
                details=details,
            )
        if after.unit.value != item.quantity_unit:
            raise DomainError(
                "UNIT_MISMATCH",
                "El reporte está asignado a una cotización con otra unidad. Ajusta o quita esas "
                "asignaciones antes de cambiar la unidad.",
                status_code=422,
                details={**details, "requested_unit": after.unit.value},
            )
        if after.value < allocated:
            raise DomainError(
                "QUANTITY_BELOW_ALLOCATED",
                "La cantidad pedida no puede quedar por debajo de lo ya asignado a una "
                "cotización. Ajusta o quita esas asignaciones primero.",
                status_code=409,
                details={**details, "requested": fmt_qty(after.value)},
            )

    v_before = item.version
    item.target_quantity = after.value if after else None
    item.quantity_unit = after.unit.value if after else None
    item.version = v_before + 1
    ev = QuantityEvent(
        organization_id=org, demand_item_id=item.id, actor_id=actor.id, operation=operation.value,
        value=value, unit=unit.value if unit else None,
        before_value=before.value if before else None, before_unit=before.unit.value if before else None,
        after_value=after.value if after else None, after_unit=after.unit.value if after else None,
        version_before=v_before, version_after=item.version, reason=reason,
        idempotency_key=idempotency_key, request_hash=req,
    )  # fmt: skip
    session.add(ev)
    await session.flush()
    emit(session, org_id=org, actor_id=actor.id, action=f"quantity.{operation.value.lower()}",
         entity_type="demand_item", entity_id=item.id, event_type="demand.quantity_changed",
         aggregate_version=item.version,
         payload={"demand_item_id": str(item.id), "operation": operation.value,
                  "quantity_changed": before != after,
                  "source": "web" if web_admin else "telegram"},
         before={"target_quantity": before and fmt_qty(before.value), "unit": before and before.unit.value},
         after={"target_quantity": after and fmt_qty(after.value), "unit": after and after.unit.value})  # fmt: skip
    return QuantityResult(item, ev)
