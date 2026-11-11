"""Identidad interna de una oferta en vivo, para enseñar sin depender del catálogo canónico.

Una oferta viva puede no tener vínculo explícito ni EAN único. Para que «Enseñar a la IA» no
quede bloqueado, se crea —de forma idempotente y solo cuando hace falta— un `TradeItem` **técnico**
con `identity_key = offer:<fuente>:<SKU>`. Ese artículo:

* nace `inactive`, así que nunca aparece en la búsqueda ni crea un catálogo paralelo visible;
* no crea `SupplierProductMapping`: el vínculo verificado sigue siendo el único que apunta a un
  artículo canónico;
* conserva en `attributes` la evidencia de la oferta (fuente, SKU, nombre, laboratorio, EAN…);
* permite registrar la preferencia de inmediato y aplicarla a esa oferta en la próxima búsqueda.

Cuando una persona vincula el SKU a un artículo canónico, las preferencias vigentes de la
identidad se **reubican** en el canónico conservando su `created_at` original (ver
`adopt_offer_identity`). No se copian historiales completos ni se mezclan alcances: se reproduce,
por clase, el mismo evento que el motor ya consideraba vigente, y la cronología existente decide
quién gana. Un empate exacto de fecha se audita como conflicto y **no** se sobrescribe nada.

La corrección de identidades admite también la identidad `import:<fuente>:<SKU>` que crea la
importación de una lista, siempre que sea **exactamente** el mismo SKU de la misma fuente: al
vincularlo a un artículo canónico, lo enseñado no se pierde ni se vuelve a enseñar. El artículo
técnico que queda sin vínculos se desactiva, sin borrar historial.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.models import (
    ChainProduct,
    CommercialObservation,
    CommercialPreference,
    ObservationInterpretation,
    QuoteLine,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
    TradeItemAlias,
)
from app.services.actors import Actor
from app.services.events import advisory_lock, emit
from app.services.preferences import effective_events, load_item_preferences
from app.services.provenance import org_kind, origin_for

OFFER_IDENTITY_PREFIX = "offer:"
IMPORT_IDENTITY_PREFIX = "import:"
MAX_NAME = 300
_GTIN = re.compile(r"[0-9]{8,14}")


def offer_identity_key(source_id: uuid.UUID, external_id: str) -> str:
    """Clave determinista de la identidad técnica de una oferta viva."""
    return f"{OFFER_IDENTITY_PREFIX}{source_id}:{external_id.strip()}"


def import_identity_key(source_id: uuid.UUID, external_id: str) -> str:
    """Clave determinista del artículo que crea la importación de una lista (`import:`)."""
    return f"{IMPORT_IDENTITY_PREFIX}{source_id}:{external_id.strip()}"


def is_offer_identity(item: TradeItem) -> bool:
    return item.identity_key.startswith(OFFER_IDENTITY_PREFIX)


def is_import_identity(item: TradeItem) -> bool:
    return item.identity_key.startswith(IMPORT_IDENTITY_PREFIX)


def is_identity_of(item: TradeItem, source_id: uuid.UUID, external_id: str) -> bool:
    """¿La identidad técnica de `item` es exactamente la de este SKU de esta fuente?

    Admite las dos formas técnicas que nombran el mismo SKU —`offer:` de una oferta viva enseñada
    sin vínculo e `import:` de la lista cargada— y exige **identificador de fuente y código
    idénticos**. No compara nombres, laboratorios, marcas ni presentaciones: dos productos
    parecidos nunca comparten identidad por parecido.
    """
    clean = external_id.strip()
    return item.identity_key in {
        offer_identity_key(source_id, clean),
        import_identity_key(source_id, clean),
    }


@dataclass(frozen=True)
class OfferViewContext:
    """Datos comerciales que la persona está viendo, tal como los publicó la fuente.

    No declara ninguna equivalencia: solo permite interpretar el comentario con el contexto real
    de la oferta (laboratorio, presentación, precio y stock incluidos).
    """

    source_id: uuid.UUID
    external_id: str
    name: str
    supplier_id: uuid.UUID | None = None
    laboratory: str | None = None
    brand: str | None = None
    presentation: str | None = None
    price_amount: Decimal | None = None
    currency: str | None = None
    price_basis: str | None = None
    sale_unit: str | None = None
    stock_status: str | None = None
    stock_quantity: Decimal | None = None
    stock_unit: str | None = None
    ean: str | None = None
    product_url: str | None = None
    image_url: str | None = None

    def as_observation_context(self) -> dict[str, Any]:
        """Bloque `offer_view` que se guarda en `commercial_observation.context`."""
        price = None
        if self.price_amount is not None and self.currency:
            price = {"amount": str(self.price_amount), "currency": self.currency}
        return {
            "source_id": str(self.source_id),
            "external_id": self.external_id.strip(),
            "supplier_id": str(self.supplier_id) if self.supplier_id else None,
            "name": self.name,
            "laboratory": self.laboratory,
            "brand": self.brand,
            "presentation": self.presentation,
            "price": price,
            "price_basis": self.price_basis,
            "sale_unit": self.sale_unit,
            "stock_status": self.stock_status,
            "stock_quantity": str(self.stock_quantity) if self.stock_quantity is not None else None,
            "stock_unit": self.stock_unit,
            "ean": self.ean,
            "product_url": self.product_url,
            "image_url": self.image_url,
        }


@dataclass(frozen=True)
class ResolvedOffer:
    item: TradeItem
    source: Source
    supplier: Supplier | None
    resolution: Literal["mapping", "ean", "offer_identity", "offer_identity_created"]


async def _load_source(session: AsyncSession, org: uuid.UUID, source_id: uuid.UUID) -> Source:
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
            "Solo una fuente de proveedor puede enseñar preferencias sobre sus ofertas.",
            status_code=422,
        )
    return source


async def _load_supplier(
    session: AsyncSession, org: uuid.UUID, supplier_id: uuid.UUID | None
) -> Supplier | None:
    if supplier_id is None:
        return None
    supplier = (
        await session.execute(
            select(Supplier).where(Supplier.id == supplier_id, Supplier.organization_id == org)
        )
    ).scalar_one_or_none()
    if supplier is None:
        raise not_found("El proveedor")
    return supplier


async def ensure_offer_identity(
    session: AsyncSession,
    actor: Actor,
    offer: OfferViewContext,
    *,
    source: Source | None = None,
) -> tuple[TradeItem, bool]:
    """Devuelve la identidad técnica del SKU, creándola si no existe. Nunca crea duplicados.

    El bloqueo por `(organización, fuente, SKU)` serializa dos enseñanzas simultáneas: la segunda
    encuentra la fila ya creada y la reutiliza.
    """
    org = actor.organization_id
    clean_external = offer.external_id.strip()
    if not clean_external:
        raise DomainError(
            "EXTERNAL_ID_REQUIRED",
            "La oferta debe traer su código de la fuente.",
            status_code=422,
        )
    source = source or await _load_source(session, org, offer.source_id)
    key = offer_identity_key(source.id, clean_external)

    await advisory_lock(session, f"offer-identity:{org}:{source.id}:{clean_external}")
    existing = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.organization_id == org, TradeItem.identity_key == key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    now = datetime.now(UTC)
    name = (offer.name or "").strip() or clean_external
    gtin = offer.ean.strip() if offer.ean and _GTIN.fullmatch(offer.ean.strip()) else None
    attributes = {
        "schema_version": 2,
        "origin": "offer_identity",
        "offer_identity": {
            "source_id": str(source.id),
            "supplier_id": str(source.supplier_id) if source.supplier_id else None,
            "external_id": clean_external,
            "external_name": name,
            "laboratory": offer.laboratory,
            "brand": offer.brand,
            "presentation": offer.presentation,
            "ean": gtin,
            "product_url": offer.product_url,
            "first_seen_at": now.isoformat(),
        },
    }
    kind = await org_kind(session, org)
    item = TradeItem(
        organization_id=org,
        identity_key=key,
        display_name=name[:MAX_NAME],
        laboratory=(offer.laboratory or None),
        brand=(offer.brand or None),
        gtin=gtin,
        # Técnica y trazable, nunca visible: no compite con el catálogo publicado.
        status="inactive",
        attributes=attributes,
        data_origin=origin_for(kind),
        created_by=actor.id,
    )
    session.add(item)
    await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="trade_item.offer_identity_created",
        entity_type="trade_item",
        entity_id=item.id,
        event_type="catalog.offer_identity_created",
        aggregate_version=1,
        payload={
            "trade_item_id": str(item.id),
            "identity_key": key,
            "source_id": str(source.id),
            "external_id": clean_external,
            "external_name": name,
            "status": item.status,
        },
        after={"identity_key": key, "status": item.status},
    )
    return item, True


async def resolve_offer_item(
    session: AsyncSession, actor: Actor, offer: OfferViewContext
) -> ResolvedOffer:
    """Resuelve la oferta con evidencia verificable, en este orden exacto:

    1. vínculo explícito `(organización, fuente, SKU)`;
    2. EAN idéntico a **un único** artículo activo;
    3. identidad técnica `offer:<fuente>:<SKU>` ya creada;
    4. creación idempotente de esa identidad.

    Nunca compara nombres ni crea equivalencias con otro artículo.
    """
    org = actor.organization_id
    source = await _load_source(session, org, offer.source_id)
    clean_external = offer.external_id.strip()

    mapped = (
        await session.execute(
            select(SupplierProductMapping).where(
                SupplierProductMapping.organization_id == org,
                SupplierProductMapping.source_id == source.id,
                SupplierProductMapping.external_id == clean_external,
            )
        )
    ).scalar_one_or_none()
    supplier_id = offer.supplier_id or source.supplier_id
    supplier = await _load_supplier(session, org, supplier_id)
    if mapped is not None:
        item = (
            await session.execute(
                select(TradeItem).where(
                    TradeItem.id == mapped.trade_item_id, TradeItem.organization_id == org
                )
            )
        ).scalar_one()
        return ResolvedOffer(item, source, supplier, "mapping")

    ean = offer.ean.strip() if offer.ean and _GTIN.fullmatch(offer.ean.strip()) else None
    if ean:
        by_ean = (
            (
                await session.execute(
                    select(TradeItem).where(
                        TradeItem.organization_id == org,
                        TradeItem.status == "active",
                        TradeItem.gtin == ean,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(by_ean) == 1:
            return ResolvedOffer(by_ean[0], source, supplier, "ean")
        # Un EAN repetido es ambiguo y no se resuelve: se usa la identidad propia del SKU.

    key = offer_identity_key(source.id, clean_external)
    existing = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.organization_id == org, TradeItem.identity_key == key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return ResolvedOffer(existing, source, supplier, "offer_identity")

    item, _ = await ensure_offer_identity(session, actor, offer, source=source)
    return ResolvedOffer(item, source, supplier, "offer_identity_created")


# --------------------------------------------------------------------------- reubicación


@dataclass(frozen=True)
class AdoptionConflict:
    """Empate exacto entre una enseñanza de la oferta y el estado vigente del canónico.

    No se resuelve copiando ni sobrescribiendo: se conserva el estado actual del canónico, la
    enseñanza original permanece intacta en la identidad y el conflicto queda auditado.
    """

    subject_kind: str
    source_interpretation_id: uuid.UUID
    canonical_interpretation_id: uuid.UUID
    reason: str = "same_timestamp"


@dataclass(frozen=True)
class AdoptionResult:
    adopted: bool
    derived_observation_ids: tuple[uuid.UUID, ...] = ()
    preference_ids: tuple[uuid.UUID, ...] = ()
    conflicts: tuple[AdoptionConflict, ...] = ()
    skipped: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


async def adopt_offer_identity(
    session: AsyncSession,
    actor: Actor,
    *,
    source_id: uuid.UUID,
    external_id: str,
    canonical_item_id: uuid.UUID,
    identity_item: TradeItem | None = None,
) -> AdoptionResult:
    """Reubica en el artículo canónico el **estado vigente** de la identidad de la oferta.

    Por cada clase (`laboratory`, `supplier`, `price:maximum`, …) se reproduce el mismo evento que
    el motor ya consideraba vigente en la identidad —preferencia establecida o retiro explícito—
    con el `created_at` original. El motor temporal existente decide entonces por cronología: una
    enseñanza más nueva que la del canónico gana, una más antigua queda como historial y un empate
    exacto se audita sin sobrescribir.

    Es idempotente: cada interpretación origen se reubica una sola vez. Las observaciones,
    interpretaciones y preferencias originales no se modifican. Nunca se transfieren preferencias
    entre dos artículos canónicos distintos.

    `identity_item` permite reubicar desde un artículo técnico de **importación** (`import:`), pero
    solo si es exactamente la identidad de esta fuente y este SKU: es el caso en que se corrige el
    vínculo de una lista cargada y la enseñada no debe perderse. Sin ese parámetro se busca, como
    siempre, la identidad `offer:` de la oferta viva.
    """
    org = actor.organization_id
    clean_external = external_id.strip()

    if identity_item is not None:
        if identity_item.organization_id != org or not is_identity_of(
            identity_item, source_id, clean_external
        ):
            raise DomainError(
                "IDENTITY_NOT_FOR_SKU",
                "La identidad técnica no corresponde a este SKU y esta fuente.",
                status_code=422,
            )
        identity: TradeItem | None = identity_item
    else:
        key = offer_identity_key(source_id, clean_external)
        identity = (
            await session.execute(
                select(TradeItem).where(
                    TradeItem.organization_id == org, TradeItem.identity_key == key
                )
            )
        ).scalar_one_or_none()
    if identity is None:
        return AdoptionResult(adopted=False, skipped="no_identity")
    if identity.id == canonical_item_id:
        return AdoptionResult(adopted=False, skipped="same_item")

    canonical = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.id == canonical_item_id, TradeItem.organization_id == org
            )
        )
    ).scalar_one_or_none()
    if canonical is None:
        raise not_found("El artículo")
    if canonical.status != "active":
        raise DomainError(
            "TRADE_ITEM_INACTIVE",
            "El artículo del catálogo está inactivo; elige uno vigente.",
            status_code=409,
        )

    await advisory_lock(session, f"adoption:{org}:{source_id}:{clean_external}")

    identity_prefs = (
        await load_item_preferences(session, org, [identity.id])
    ).get(identity.id)
    if identity_prefs is None or not identity_prefs.has_effects:
        return AdoptionResult(adopted=False, skipped="no_effects")
    canonical_prefs = (
        await load_item_preferences(session, org, [canonical.id])
    ).get(canonical.id)
    canonical_events = effective_events(canonical_prefs) if canonical_prefs else {}

    # Idempotencia real: una interpretación ya reubicada no se vuelve a evaluar. Sin esto, su copia
    # en el canónico lleva la misma fecha y un segundo vínculo la leería como un empate artificial.
    adopted_before = set(
        (
            await session.execute(
                select(
                    CommercialObservation.context["adopted_from_interpretation_id"].astext
                ).where(
                    CommercialObservation.organization_id == org,
                    CommercialObservation.context["adopted_from_trade_item_id"].astext
                    == str(identity.id),
                )
            )
        )
        .scalars()
        .all()
    )

    conflicts: list[AdoptionConflict] = []
    grouped: dict[uuid.UUID, dict[str, Any]] = {}
    for cls, event in effective_events(identity_prefs).items():
        if str(event.interpretation_id) in adopted_before:
            continue
        source_interp = identity_prefs.interpretation(event.interpretation_id)
        if source_interp is None:
            continue
        winner = canonical_events.get(cls)
        if winner is not None:
            canonical_interp = (
                canonical_prefs.interpretation(winner.interpretation_id)
                if canonical_prefs is not None
                else None
            )
            if (
                canonical_interp is not None
                and canonical_interp.created_at == source_interp.created_at
            ):
                conflicts.append(
                    AdoptionConflict(
                        subject_kind=cls,
                        source_interpretation_id=source_interp.id,
                        canonical_interpretation_id=canonical_interp.id,
                    )
                )
                continue
        entry = grouped.setdefault(
            source_interp.id, {"interpretation": source_interp, "retired": set(), "prefs": []}
        )
        if event.action == "retire":
            entry["retired"].add(cls)
        else:
            entry["prefs"].extend(event.preferences)

    if conflicts:
        emit(
            session,
            org_id=org,
            actor_id=actor.id,
            action="preference.adoption_conflict",
            entity_type="trade_item",
            entity_id=canonical.id,
            event_type="commercial_preference.adoption_conflict",
            aggregate_version=None,
            payload={
                "source_trade_item_id": str(identity.id),
                "canonical_trade_item_id": str(canonical.id),
                "source_id": str(source_id),
                "external_id": clean_external,
                "conflicts": [
                    {
                        "subject_kind": c.subject_kind,
                        "source_interpretation_id": str(c.source_interpretation_id),
                        "canonical_interpretation_id": str(c.canonical_interpretation_id),
                        "reason": c.reason,
                    }
                    for c in conflicts
                ],
            },
        )

    if not grouped:
        return AdoptionResult(
            adopted=False,
            conflicts=tuple(conflicts),
            skipped=(
                "only_conflicts"
                if conflicts
                else "already_adopted"
                if adopted_before
                else "no_effective_classes"
            ),
        )

    derived_rows: list[uuid.UUID] = []
    derived_prefs: list[uuid.UUID] = []
    already: list[str] = []
    for source_interp_id in sorted(
        grouped, key=lambda i: (grouped[i]["interpretation"].created_at, str(i))
    ):
        entry = grouped[source_interp_id]
        interp: ObservationInterpretation = entry["interpretation"]
        prior = (
            await session.execute(
                select(CommercialObservation.id)
                .where(
                    CommercialObservation.organization_id == org,
                    CommercialObservation.context["adopted_from_interpretation_id"].astext
                    == str(interp.id),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if prior is not None:
            already.append(str(interp.id))
            continue

        original = (
            await session.execute(
                select(CommercialObservation).where(
                    CommercialObservation.id == interp.observation_id,
                    CommercialObservation.organization_id == org,
                )
            )
        ).scalar_one_or_none()
        if original is None:
            continue

        context = dict(original.context or {})
        context.update(
            {
                # Trazabilidad explícita: no es una enseñanza nueva de la persona.
                "adopted_from_observation_id": str(original.id),
                "adopted_from_interpretation_id": str(interp.id),
                "adopted_from_trade_item_id": str(identity.id),
                "adopted_from_author_id": str(original.author_id),
                "adopted_from_identity_key": identity.identity_key,
                "canonical_trade_item_id": str(canonical.id),
                "adoption_reason": (
                    "import_identity_linked"
                    if is_import_identity(identity)
                    else "offer_identity_linked"
                ),
            }
        )
        observation = CommercialObservation(
            id=uuid.uuid4(),
            organization_id=org,
            text_original=original.text_original,
            # La reubicación la ejecuta quien vincula, no la persona que enseñó.
            author_id=actor.id,
            created_at=original.created_at,
            quote_id=None,
            quote_line_id=None,
            trade_item_id=canonical.id,
            offer_snapshot_id=None,
            supplier_id=original.supplier_id,
            context=context,
            data_origin=original.data_origin,
        )
        session.add(observation)
        await session.flush()

        interpretation = ObservationInterpretation(
            organization_id=org,
            observation_id=observation.id,
            trade_item_id=canonical.id,
            # El motor aplica `suggested` igual que `confirmed`; la reubicación no es una revisión
            # humana nueva, así que no se marca como confirmada ni se inventan tokens.
            status="suggested",
            model=interp.model,
            prompt_version=interp.prompt_version,
            retired_subject_kinds=sorted(entry["retired"]),
            raw_output=None,
            input_tokens=None,
            output_tokens=None,
            latency_ms=None,
            error_code=None,
            reviewed_by=None,
            reviewed_at=None,
            review_note=None,
            clarification=None,
            created_at=interp.created_at,
            data_origin=interp.data_origin,
        )
        session.add(interpretation)
        await session.flush()

        for source_pref in entry["prefs"]:
            pref = CommercialPreference(
                organization_id=org,
                interpretation_id=interpretation.id,
                observation_id=observation.id,
                trade_item_id=canonical.id,
                subject_kind=source_pref.subject_kind,
                subject_key=source_pref.subject_key,
                subject_label=source_pref.subject_label,
                subject_supplier_id=source_pref.subject_supplier_id,
                effect=source_pref.effect,
                tolerance_kind=source_pref.tolerance_kind,
                tolerance_percent=source_pref.tolerance_percent,
                tolerance_amount=source_pref.tolerance_amount,
                tolerance_currency=source_pref.tolerance_currency,
                tolerance_origin=source_pref.tolerance_origin,
                price_reference_kind=source_pref.price_reference_kind,
                price_reference_amount=source_pref.price_reference_amount,
                price_reference_currency=source_pref.price_reference_currency,
                price_reference_basis=source_pref.price_reference_basis,
                price_reference_quantity=source_pref.price_reference_quantity,
                price_reference_quantity_unit=source_pref.price_reference_quantity_unit,
                confidence=source_pref.confidence,
                status="suggested",
                created_at=interp.created_at,
                data_origin=source_pref.data_origin,
            )
            session.add(pref)
            await session.flush()
            derived_prefs.append(pref.id)

        emit(
            session,
            org_id=org,
            actor_id=actor.id,
            action="observation.adopted",
            entity_type="commercial_observation",
            entity_id=observation.id,
            event_type="commercial_observation.adopted",
            aggregate_version=None,
            payload={
                "observation_id": str(observation.id),
                "interpretation_id": str(interpretation.id),
                "trade_item_id": str(canonical.id),
                "adopted_from_observation_id": str(original.id),
                "adopted_from_interpretation_id": str(interp.id),
                "adopted_from_trade_item_id": str(identity.id),
                "subject_kinds": sorted(
                    set(entry["retired"])
                    | {pref.subject_kind for pref in entry["prefs"]}
                ),
            },
        )
        derived_rows.append(observation.id)

    if not derived_rows:
        return AdoptionResult(
            adopted=False,
            conflicts=tuple(conflicts),
            skipped="already_adopted",
            notes=tuple(already),
        )
    return AdoptionResult(
        adopted=True,
        derived_observation_ids=tuple(derived_rows),
        preference_ids=tuple(derived_prefs),
        conflicts=tuple(conflicts),
    )


# ------------------------------------------------------------------- retiro de identidades técnicas


async def _has_dependency(
    session: AsyncSession, model: type[Any], org: uuid.UUID, item_id: uuid.UUID
) -> bool:
    stmt = (
        select(model.id)
        .where(model.organization_id == org, model.trade_item_id == item_id)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def retire_orphan_import_identity(
    session: AsyncSession, actor: Actor, item: TradeItem
) -> bool:
    """Desactiva un artículo técnico `import:` que quedó sin vínculos.

    Solo actúa sobre una identidad de importación **activa** de la propia organización y solo si no
    queda ningún `SupplierProductMapping` ni otra dependencia operativa (línea de cotización,
    precio de cadena o alias). El historial —ofertas observadas, observaciones,
    interpretaciones, preferencias y trazas— se conserva intacto: la fila sigue existiendo, con su
    identidad y su nombre publicados, y solo deja de listarse como artículo vigente. Devuelve
    `True` cuando cambió el estado.
    """
    if not is_import_identity(item) or item.organization_id != actor.organization_id:
        return False
    if item.status != "active":
        return False
    for model in (
        SupplierProductMapping,
        QuoteLine,
        ChainProduct,
        TradeItemAlias,
    ):
        if await _has_dependency(session, model, actor.organization_id, item.id):
            return False

    item.status = "inactive"
    item.version += 1
    item.updated_at = datetime.now(UTC)
    session.add(item)
    await session.flush()
    emit(
        session,
        org_id=actor.organization_id,
        actor_id=actor.id,
        action="trade_item.technical_identity_retired",
        entity_type="trade_item",
        entity_id=item.id,
        event_type="catalog.technical_identity_retired",
        aggregate_version=item.version,
        payload={
            "trade_item_id": str(item.id),
            "identity_key": item.identity_key,
            "identity_kind": "import",
        },
        before={"status": "active"},
        after={"status": item.status, "version": item.version},
    )
    return True
