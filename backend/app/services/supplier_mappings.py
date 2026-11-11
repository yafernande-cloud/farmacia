"""Vínculo explícito entre un producto de una fuente de proveedor y un artículo del catálogo.

Es la única vía admitida para que una oferta en vivo tenga un `trade_item_id`: una persona con
permiso elige un artículo existente y confirma el vínculo. Nace `explicit` y se audita; nunca se
crea un artículo nuevo, no se compara por nombres y no se sobrescribe un vínculo anterior.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.core.permissions import require_admin
from app.models import Source, SupplierProductMapping, TradeItem
from app.services.actors import Actor
from app.services.events import advisory_lock, emit
from app.services.provenance import org_kind, origin_for


async def create_mapping(
    session: AsyncSession,
    actor: Actor,
    *,
    source_id: uuid.UUID,
    external_id: str,
    external_name: str | None,
    trade_item_id: uuid.UUID,
) -> tuple[SupplierProductMapping, bool]:
    """Crea el vínculo o devuelve el existente. `created=False` cuando ya apuntaba al mismo artículo."""
    require_admin(actor.roles)
    org = actor.organization_id

    source = (
        await session.execute(
            select(Source).where(Source.id == source_id, Source.organization_id == org)
        )
    ).scalar_one_or_none()
    if source is None:
        raise not_found("La fuente")
    if source.kind != "supplier" or source.supplier_id is None:
        raise DomainError(
            "SOURCE_NOT_SUPPLIER",
            "Solo una fuente de proveedor puede vincular sus productos con el catálogo interno.",
            status_code=422,
        )

    item = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.id == trade_item_id, TradeItem.organization_id == org
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise not_found("El artículo")
    if item.status != "active":
        raise DomainError(
            "TRADE_ITEM_INACTIVE",
            "El artículo del catálogo está inactivo; elige uno vigente.",
            status_code=409,
        )

    clean_external = external_id.strip()
    if not clean_external:
        raise DomainError(
            "EXTERNAL_ID_REQUIRED",
            "El producto externo debe traer su identificador de la fuente.",
            status_code=422,
        )
    clean_name = (external_name or "").strip() or None

    # La clave única es (organización, fuente, SKU): el bloqueo serializa dos intentos simultáneos.
    await advisory_lock(session, f"mapping:{org}:{source.id}:{clean_external}")
    existing = (
        await session.execute(
            select(SupplierProductMapping).where(
                SupplierProductMapping.organization_id == org,
                SupplierProductMapping.source_id == source.id,
                SupplierProductMapping.external_id == clean_external,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.trade_item_id == item.id:
            # Reintento idempotente: el vínculo es el mismo y no se duplica la auditoría.
            return existing, False
        raise DomainError(
            "SUPPLIER_MAPPING_CONFLICT",
            "Ese producto de la fuente ya está vinculado a otro artículo del catálogo.",
            status_code=409,
            details={
                "mapping_id": str(existing.id),
                "trade_item_id": str(existing.trade_item_id),
            },
        )

    kind = await org_kind(session, org)
    mapping = SupplierProductMapping(
        organization_id=org,
        source_id=source.id,
        supplier_id=source.supplier_id,
        trade_item_id=item.id,
        external_id=clean_external,
        external_name=clean_name,
        origin="explicit",
        data_origin=origin_for(kind),
        created_by=actor.id,
    )
    session.add(mapping)
    await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="supplier_product_mapping.created",
        entity_type="supplier_product_mapping",
        entity_id=mapping.id,
        event_type="catalog.mapping_created",
        aggregate_version=1,
        payload={
            "source_id": str(source.id),
            "external_id": clean_external,
            "trade_item_id": str(item.id),
            "origin": mapping.origin,
            "linked_via": "live_offer",
        },
        after={"trade_item_id": str(item.id), "external_name": clean_name},
    )
    return mapping, True
