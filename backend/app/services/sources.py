"""E7 — administración de fuentes y E4 — estado de fuentes (C0).

`kind` separa dos mundos que no se mezclan: una fuente `supplier` pertenece a un proveedor y puede
producir ofertas; una `chain_reference` solo produce fichas y precios públicos, y nunca se convierte
en oferta comprable. El estado (E4) se deriva; no se guarda. Los secretos no existen en esta tabla:
`has_secret` es hoy siempre `false` y el valor del secreto jamás se devuelve.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import DomainError, not_found
from app.core.permissions import require_admin
from app.models import ImportRun, Source, Supplier
from app.services.actors import Actor
from app.services.events import advisory_lock, emit
from app.services.provenance import OrgKind, org_kind, origin_for
from app.services.suppliers import normalize_name

KINDS = ("supplier", "chain_reference")
MODES = ("api", "manual_import", "fixture", "none")
CAPABILITIES = ("search", "product_detail", "price", "stock", "terms")
TRANSACTIONAL = ("remote_cart", "submit_order", "order_status", "cancel_order")


def _clean_name(name: str) -> str:
    n = " ".join((name or "").split())
    if not (1 <= len(n) <= 200) or any(ord(c) < 32 for c in n):
        raise DomainError(
            "INVALID_NAME",
            "El nombre de la fuente debe tener entre 1 y 200 caracteres.",
            status_code=400,
        )
    return n


def _clean_capabilities(caps: list[str], *, kind: str) -> list[str]:
    seen: list[str] = []
    for raw in caps:
        if not isinstance(raw, str) or not raw.strip():
            raise DomainError(
                "SOURCE_CAPABILITY_NOT_ALLOWED",
                "Cada capacidad debe ser un texto no vacío.",
                status_code=422,
            )
        cap = raw.strip()
        if cap in TRANSACTIONAL or cap not in CAPABILITIES:
            raise DomainError(
                "SOURCE_CAPABILITY_NOT_ALLOWED",
                f"La capacidad «{cap}» no está permitida en v0. Solo: {', '.join(CAPABILITIES)}.",
                status_code=422,
            )
        if cap not in seen:
            seen.append(cap)
    if kind == "chain_reference" and "terms" in seen:
        raise DomainError(
            "SOURCE_CAPABILITY_NOT_ALLOWED",
            "Una fuente de cadena no puede declarar condiciones de compra (`terms`).",
            status_code=422,
        )
    return seen


async def _assert_fixture_allowed(session: AsyncSession, actor: Actor, mode: str) -> None:
    if mode == "fixture" and await org_kind(session, actor.organization_id) != OrgKind.SIMULATION:
        raise DomainError(
            "FIXTURE_NOT_ALLOWED",
            "El modo fixture solo se admite en la organización de simulación.",
            status_code=422,
        )


async def _assert_unique_name(
    session: AsyncSession,
    org: uuid.UUID,
    name: str,
    *,
    exclude: uuid.UUID | None = None,
) -> None:
    q = select(Source.id, Source.name).where(Source.organization_id == org)
    if exclude is not None:
        q = q.where(Source.id != exclude)
    wanted = normalize_name(name)
    for other_id, other_name in (await session.execute(q)).all():
        if normalize_name(other_name) == wanted:
            raise DomainError(
                "DUPLICATE_NAME",
                f"Ya existe una fuente llamada «{name}».",
                status_code=409,
                details={"source_id": str(other_id)},
            )


async def _validate_supplier(
    session: AsyncSession, actor: Actor, kind: str, supplier_id: uuid.UUID | None
) -> None:
    if kind == "supplier":
        if supplier_id is None:
            raise DomainError(
                "SUPPLIER_REQUIRED",
                "Una fuente de proveedor exige supplier_id.",
                status_code=422,
            )
        found = (
            await session.execute(
                select(Supplier.id).where(
                    Supplier.id == supplier_id,
                    Supplier.organization_id == actor.organization_id,
                )
            )
        ).first()
        if found is None:
            raise not_found("El proveedor")
    elif supplier_id is not None:
        raise DomainError(
            "SUPPLIER_NOT_ALLOWED",
            "Una fuente de cadena no puede declarar supplier_id.",
            status_code=422,
        )


async def _load(session: AsyncSession, actor: Actor, source_id: uuid.UUID) -> Source:
    row = (
        await session.execute(
            select(Source)
            .where(Source.id == source_id, Source.organization_id == actor.organization_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("La fuente")
    return row


def _snapshot(s: Source) -> dict:
    return {
        "name": s.name,
        "kind": s.kind,
        "supplier_id": str(s.supplier_id) if s.supplier_id else None,
        "mode": s.mode,
        "capabilities": list(s.capabilities or []),
        "enabled": s.enabled,
        "version": s.version,
    }


async def create_source(
    session: AsyncSession,
    actor: Actor,
    *,
    name: str,
    kind: str,
    supplier_id: uuid.UUID | None = None,
    mode: str = "none",
    capabilities: list[str] | None = None,
    enabled: bool = True,
) -> Source:
    require_admin(actor.roles)
    org = actor.organization_id
    clean_name = _clean_name(name)
    if kind not in KINDS:
        raise DomainError(
            "INVALID_KIND", "El tipo debe ser supplier o chain_reference.", status_code=422
        )
    if mode not in MODES:
        raise DomainError(
            "INVALID_MODE",
            f"El modo debe ser uno de: {', '.join(MODES)}.",
            status_code=422,
        )
    await _validate_supplier(session, actor, kind, supplier_id)
    await _assert_fixture_allowed(session, actor, mode)
    caps = _clean_capabilities(capabilities or [], kind=kind)
    await advisory_lock(session, f"source:name:{org}")
    await _assert_unique_name(session, org, clean_name)
    row = Source(
        organization_id=org,
        kind=kind,
        supplier_id=supplier_id,
        name=clean_name,
        mode=mode,
        capabilities=caps,
        enabled=enabled,
        data_origin=origin_for(await org_kind(session, org)),
        created_by=actor.id,
    )
    session.add(row)
    await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="source.created",
        entity_type="source",
        entity_id=row.id,
        event_type="catalog.source_created",
        aggregate_version=1,
        payload={"source_id": str(row.id), "kind": kind},
        after=_snapshot(row),
    )
    return row


async def update_source(
    session: AsyncSession,
    actor: Actor,
    source_id: uuid.UUID,
    *,
    expected_version: int,
    changes: dict,
) -> Source:
    require_admin(actor.roles)
    row = await _load(session, actor, source_id)
    if row.version != expected_version:
        raise DomainError(
            "VERSION_CONFLICT",
            "La fuente cambió. Revisa la versión actual antes de guardar.",
            status_code=409,
            details={"current_version": row.version},
        )
    before = _snapshot(row)
    if "name" in changes:
        if changes["name"] is None:
            raise DomainError(
                "INVALID_NAME", "El nombre de la fuente no puede quedar vacío.", status_code=400
            )
        row.name = _clean_name(changes["name"])
        await advisory_lock(session, f"source:name:{actor.organization_id}")
        await _assert_unique_name(session, actor.organization_id, row.name, exclude=row.id)
    if "mode" in changes:
        if changes["mode"] not in MODES:
            raise DomainError(
                "INVALID_MODE",
                f"El modo debe ser uno de: {', '.join(MODES)}.",
                status_code=422,
            )
        await _assert_fixture_allowed(session, actor, changes["mode"])
        row.mode = changes["mode"]
    if "capabilities" in changes:
        row.capabilities = _clean_capabilities(changes["capabilities"] or [], kind=row.kind)
    if "enabled" in changes:
        row.enabled = bool(changes["enabled"])
    row.version += 1
    row.updated_at = datetime.now(UTC)
    await session.flush()
    emit(
        session,
        org_id=actor.organization_id,
        actor_id=actor.id,
        action="source.updated",
        entity_type="source",
        entity_id=row.id,
        event_type="catalog.source_updated",
        aggregate_version=row.version,
        payload={"source_id": str(row.id), "enabled": row.enabled},
        before=before,
        after=_snapshot(row),
    )
    return row


async def get_source(session: AsyncSession, actor: Actor, source_id: uuid.UUID) -> Source:
    require_admin(actor.roles)
    row = (
        await session.execute(
            select(Source).where(
                Source.id == source_id, Source.organization_id == actor.organization_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("La fuente")
    return row


async def list_sources(
    session: AsyncSession,
    actor: Actor,
    *,
    kind: str | None = None,
    enabled: bool | None = None,
) -> list[Source]:
    require_admin(actor.roles)
    q = select(Source).where(Source.organization_id == actor.organization_id)
    if kind is not None:
        if kind not in KINDS:
            raise DomainError(
                "INVALID_KIND", "El tipo debe ser supplier o chain_reference.", status_code=422
            )
        q = q.where(Source.kind == kind)
    if enabled is not None:
        q = q.where(Source.enabled.is_(enabled))
    return list((await session.execute(q.order_by(Source.name))).scalars().all())


def _effective_last_success(
    source: Source, imported_success_at: datetime | None
) -> datetime | None:
    """Marca de última lectura: la de la fuente o, si faltaba, la de su última importación.

    La importación manual guarda `last_success_at`; esta lectura cubre además las importaciones
    realizadas antes de que existiera esa escritura, sin volver a importar ni duplicar ofertas.
    """
    if imported_success_at is None:
        return source.last_success_at
    if source.last_success_at is None:
        return imported_success_at
    return max(source.last_success_at, imported_success_at)


async def import_success_by_source(
    session: AsyncSession, actor: Actor, source_ids: list[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    """Última importación con filas aceptadas por fuente (solo las fuentes pedidas)."""
    if not source_ids:
        return {}
    rows = (
        await session.execute(
            select(ImportRun.source_id, func.max(ImportRun.imported_at))
            .where(
                ImportRun.organization_id == actor.organization_id,
                ImportRun.source_id.in_(source_ids),
                ImportRun.rows_accepted > 0,
            )
            .group_by(ImportRun.source_id)
        )
    ).all()
    return {source_id: imported_at for source_id, imported_at in rows if imported_at is not None}


def source_status(
    source: Source,
    *,
    now: datetime,
    stale_after_hours: int,
    imported_success_at: datetime | None = None,
) -> str:
    """Estado derivado de E4; nunca se guarda. Un error es `failed` hasta una nueva observación."""
    if source.last_error:
        return "failed"
    last_success_at = _effective_last_success(source, imported_success_at)
    if last_success_at is None:
        return "not_configured"
    if now - last_success_at > timedelta(hours=stale_after_hours):
        return "stale"
    return "ok"


def source_payload(
    source: Source, *, now: datetime | None = None, imported_success_at: datetime | None = None
) -> dict:
    now = now or datetime.now(UTC)
    last_success_at = _effective_last_success(source, imported_success_at)
    return {
        "source_id": source.id,
        "name": source.name,
        "kind": source.kind,
        "supplier_id": source.supplier_id,
        "mode": source.mode,
        "capabilities": list(source.capabilities or []),
        "enabled": source.enabled,
        "status": source_status(
            source,
            now=now,
            stale_after_hours=get_settings().source_stale_after_hours,
            imported_success_at=imported_success_at,
        ),
        # No existe almacén de credenciales todavía: se informa la verdad, no un adorno.
        "has_secret": False,
        "last_success_at": last_success_at,
        "last_error": source.last_error,
        "version": source.version,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }
