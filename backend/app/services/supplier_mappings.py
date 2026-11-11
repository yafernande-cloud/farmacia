"""Vínculo explícito entre un producto de una fuente de proveedor y un artículo del catálogo.

Es la única vía admitida para que una oferta en vivo tenga un `trade_item_id`: una persona con
permiso elige un artículo existente y confirma el vínculo. Nace `explicit` y se audita; nunca se
crea un artículo nuevo, no se compara por nombres y no se sobrescribe un vínculo anterior.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.core.permissions import require_admin
from app.models import Source, SupplierProductMapping, TradeItem
from app.services import offer_identities as offer_svc
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
) -> tuple[SupplierProductMapping, bool, offer_svc.AdoptionResult]:
    """Crea el vínculo o devuelve el existente. `created=False` cuando ya apuntaba al mismo artículo.

    Al vincular, las preferencias vigentes de la identidad técnica de esa oferta se reubican en el
    artículo canónico conservando su cronología. Es idempotente y nunca toca otro artículo.
    """
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
            # Reintento idempotente: el vínculo es el mismo y no se duplica la auditoría. La
            # reubicación se reintenta por si un vínculo anterior es previo a esta función.
            adoption = await offer_svc.adopt_offer_identity(
                session,
                actor,
                source_id=source.id,
                external_id=clean_external,
                canonical_item_id=item.id,
            )
            return existing, False, adoption
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
    adoption = await offer_svc.adopt_offer_identity(
        session,
        actor,
        source_id=source.id,
        external_id=clean_external,
        canonical_item_id=item.id,
    )
    return mapping, True, adoption


async def relink_mapping(
    session: AsyncSession,
    actor: Actor,
    *,
    mapping_id: uuid.UUID,
    trade_item_id: uuid.UUID,
    expected_version: int | None = None,
) -> tuple[SupplierProductMapping, bool, offer_svc.AdoptionResult | None]:
    """Corrige el artículo destino de un vínculo existente, con versión y auditoría.

    Solo reubica las preferencias cuando el vínculo anterior apuntaba a la **identidad técnica de
    ese mismo SKU**: la de una oferta viva (`offer:<fuente>:<SKU>`) o la que creó la importación de
    la lista (`import:<fuente>:<SKU>`). Ambas nombran exactamente la misma fuente y el mismo código.
    Si el vínculo ya apuntaba a otro artículo canónico, no se transfiere nada: cambiar el destino de
    un SKU no convierte las enseñanzas de un producto en las de otro. Un artículo `import:` que
    queda sin vínculos ni dependencias se desactiva, para no aparecer como artículo duplicado.

    Devuelve `(mapping, changed, adoption)`. `changed=False` cuando ya apuntaba al mismo artículo;
    en ese caso la reubicación se reintenta de forma idempotente.
    """
    require_admin(actor.roles)
    org = actor.organization_id
    mapping = (
        await session.execute(
            select(SupplierProductMapping).where(
                SupplierProductMapping.id == mapping_id,
                SupplierProductMapping.organization_id == org,
            )
        )
    ).scalar_one_or_none()
    if mapping is None:
        raise not_found("El vínculo")

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

    await advisory_lock(
        session, f"mapping:{org}:{mapping.source_id}:{mapping.external_id}"
    )
    mapping = (
        await session.execute(
            select(SupplierProductMapping).where(
                SupplierProductMapping.id == mapping_id,
                SupplierProductMapping.organization_id == org,
            )
        )
    ).scalar_one()
    if expected_version is not None and mapping.version != expected_version:
        raise DomainError(
            "SUPPLIER_MAPPING_VERSION_CONFLICT",
            "El vínculo cambió desde que lo leíste; vuelve a cargarlo.",
            status_code=409,
            details={"version": mapping.version},
        )

    previous_item = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.id == mapping.trade_item_id,
                TradeItem.organization_id == org,
            )
        )
    ).scalar_one()

    if mapping.trade_item_id == item.id:
        adoption = await offer_svc.adopt_offer_identity(
            session,
            actor,
            source_id=mapping.source_id,
            external_id=mapping.external_id,
            canonical_item_id=item.id,
        )
        return mapping, False, adoption

    previous_id = mapping.trade_item_id
    now = datetime.now(UTC)
    mapping.trade_item_id = item.id
    mapping.origin = "confirmed"
    mapping.confirmed_by = actor.id
    mapping.confirmed_at = now
    mapping.updated_at = now
    mapping.version += 1
    session.add(mapping)
    await session.flush()

    adoption: offer_svc.AdoptionResult | None = None
    if offer_svc.is_identity_of(previous_item, mapping.source_id, mapping.external_id):
        # El destino anterior era la identidad técnica de este mismo SKU (oferta viva o artículo
        # de importación): reubicar es seguro y la cronología decide quién gana.
        adoption = await offer_svc.adopt_offer_identity(
            session,
            actor,
            source_id=mapping.source_id,
            external_id=mapping.external_id,
            canonical_item_id=item.id,
            identity_item=previous_item,
        )
        # El artículo de importación que quedó sin vínculo deja de listarse como vigente. Conserva
        # su historial y no se toca si todavía tiene vínculos u otras dependencias.
        if offer_svc.is_import_identity(previous_item):
            await offer_svc.retire_orphan_import_identity(session, actor, previous_item)
    else:
        # Artículo canónico → artículo canónico: no se transfieren preferencias.
        emit(
            session,
            org_id=org,
            actor_id=actor.id,
            action="preference.adoption_skipped",
            entity_type="supplier_product_mapping",
            entity_id=mapping.id,
            event_type="commercial_preference.adoption_skipped",
            aggregate_version=None,
            payload={
                "mapping_id": str(mapping.id),
                "source_id": str(mapping.source_id),
                "external_id": mapping.external_id,
                "from_trade_item_id": str(previous_id),
                "to_trade_item_id": str(item.id),
                "reason": "previous_target_not_technical_identity",
            },
        )

    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="supplier_product_mapping.relinked",
        entity_type="supplier_product_mapping",
        entity_id=mapping.id,
        event_type="catalog.mapping_relinked",
        aggregate_version=mapping.version,
        payload={
            "mapping_id": str(mapping.id),
            "source_id": str(mapping.source_id),
            "external_id": mapping.external_id,
            "from_trade_item_id": str(previous_id),
            "to_trade_item_id": str(item.id),
            "adopted_preferences": (
                len(adoption.preference_ids) if adoption is not None else 0
            ),
            "adoption_conflicts": (
                len(adoption.conflicts) if adoption is not None else 0
            ),
            "adoption_skipped": adoption.skipped if adoption is not None else "canonical_to_canonical",
        },
        before={"trade_item_id": str(previous_id)},
        after={"trade_item_id": str(item.id), "version": mapping.version},
    )
    return mapping, True, adoption
