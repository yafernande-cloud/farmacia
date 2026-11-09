"""E6 — proveedores del cotizador (C0). Administración por rol `admin`, sin permisos nuevos.

Un proveedor es a quién se le compra; sus condiciones comerciales viven en ofertas y en
`shipping_terms_snapshot`, no en esta tabla. Las condiciones en texto libre (`known_conditions`)
nunca alimentan un cálculo. Desactivar no borra nada: el historial y las ofertas se conservan.
"""

import unicodedata
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.credentials import encrypt_portal_password
from app.core.errors import DomainError, not_found
from app.core.permissions import require_admin
from app.models import Source, Supplier, SupplierCredential
from app.services.actors import Actor
from app.services.events import advisory_lock, emit
from app.services.provenance import org_kind, origin_for

# Estado público del portal. `connected` no existe a propósito: guardar una credencial no abre
# ninguna sesión ni prueba el acceso; no hay conector de portal todavía (A3).
PORTAL_NOT_CONFIGURED = "not_configured"
PORTAL_INCOMPLETE = "incomplete"
PORTAL_CREDENTIALS_SAVED = "credentials_saved"


def normalize_name(text: str) -> str:
    """Clave de comparación: minúsculas, sin acentos, espacios colapsados. No se guarda."""
    t = unicodedata.normalize("NFKD", (text or "").strip())
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    return " ".join(t.split())


def _clean_name(name: str) -> str:
    n = " ".join((name or "").split())
    if not (1 <= len(n) <= 200) or any(ord(c) < 32 for c in n):
        raise DomainError(
            "INVALID_NAME",
            "El nombre del proveedor debe tener entre 1 y 200 caracteres.",
            status_code=400,
        )
    return n


def _clean_optional(text: str | None, *, label: str, max_len: int) -> str | None:
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None
    if len(t) > max_len or any(ord(c) < 32 and c not in "\n\t" for c in t):
        raise DomainError(
            "INVALID_TEXT",
            f"{label} no puede superar {max_len} caracteres ni contener caracteres de control.",
            status_code=400,
        )
    return t


def _clean_portal_url(value: str | None) -> str | None:
    """URL http(s) del portal. Vacío = sin portal; no se inventa un esquema que no venga escrito."""
    if value is None:
        return None
    t = value.strip()
    if not t:
        return None
    parts = urlsplit(t)
    if (
        len(t) > 500
        or any(ord(c) < 32 for c in t)
        or parts.scheme not in ("http", "https")
        or not parts.netloc
    ):
        raise DomainError(
            "INVALID_PORTAL_URL",
            "La URL del portal debe empezar por http:// o https:// y tener un dominio válido.",
            status_code=400,
        )
    return t


def _clean_username(value: str | None) -> str | None:
    if value is None:
        return None
    t = value.strip()
    if not t:
        return None
    if len(t) > 200 or any(ord(c) < 32 for c in t):
        raise DomainError(
            "INVALID_CREDENTIALS",
            "El usuario del portal debe tener entre 1 y 200 caracteres sin caracteres de control.",
            status_code=400,
        )
    return t


def _clean_password(value: str | None) -> str | None:
    """No se recorta: los espacios pueden ser parte de la contraseña. Nunca se registra."""
    if value is None:
        return None
    if not value.strip() or len(value) > 500:
        raise DomainError(
            "INVALID_CREDENTIALS",
            "La contraseña del portal no puede quedar vacía ni superar 500 caracteres.",
            status_code=400,
        )
    return value


async def _credential(
    session: AsyncSession, org: uuid.UUID, supplier_id: uuid.UUID
) -> SupplierCredential | None:
    return (
        await session.execute(
            select(SupplierCredential).where(
                SupplierCredential.organization_id == org,
                SupplierCredential.supplier_id == supplier_id,
            )
        )
    ).scalar_one_or_none()


def _portal_status(supplier: Supplier, has_credentials: bool) -> str:
    if supplier.portal_url is None and not has_credentials:
        return PORTAL_NOT_CONFIGURED
    if supplier.portal_url is not None and has_credentials:
        return PORTAL_CREDENTIALS_SAVED
    return PORTAL_INCOMPLETE


async def _assert_unique_name(
    session: AsyncSession,
    org: uuid.UUID,
    name: str,
    *,
    exclude: uuid.UUID | None = None,
) -> None:
    q = select(Supplier.id, Supplier.name).where(Supplier.organization_id == org)
    if exclude is not None:
        q = q.where(Supplier.id != exclude)
    wanted = normalize_name(name)
    for other_id, other_name in (await session.execute(q)).all():
        if normalize_name(other_name) == wanted:
            raise DomainError(
                "DUPLICATE_NAME",
                f"Ya existe un proveedor llamado «{name}».",
                status_code=409,
                details={"supplier_id": str(other_id)},
            )


async def _load(session: AsyncSession, actor: Actor, supplier_id: uuid.UUID) -> Supplier:
    row = (
        await session.execute(
            select(Supplier)
            .where(Supplier.id == supplier_id, Supplier.organization_id == actor.organization_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("El proveedor")
    return row


def _snapshot(s: Supplier) -> dict:
    """Auditoría del proveedor. Nunca incluye usuario ni contraseña del portal."""
    return {
        "name": s.name,
        "supplier_code": s.supplier_code,
        "status": s.status,
        "version": s.version,
        "notes": s.notes,
        "portal_url": s.portal_url,
    }


async def create_supplier(
    session: AsyncSession,
    actor: Actor,
    *,
    name: str,
    supplier_code: str | None = None,
    notes: str | None = None,
    portal_url: str | None = None,
    portal_username: str | None = None,
    portal_password: str | None = None,
) -> Supplier:
    require_admin(actor.roles)
    org = actor.organization_id
    clean_name = _clean_name(name)
    code = _clean_optional(supplier_code, label="El código", max_len=60)
    clean_notes = _clean_optional(notes, label="Las notas", max_len=2000)
    url = _clean_portal_url(portal_url)
    username = _clean_username(portal_username)
    password = _clean_password(portal_password)
    if (username is None) != (password is None):
        raise DomainError(
            "INCOMPLETE_CREDENTIALS",
            "Para guardar las credenciales del portal se necesitan el usuario y la contraseña.",
            status_code=400,
        )
    await advisory_lock(session, f"supplier:name:{org}")
    await _assert_unique_name(session, org, clean_name)
    row = Supplier(
        organization_id=org,
        name=clean_name,
        supplier_code=code,
        notes=clean_notes,
        portal_url=url,
        data_origin=origin_for(await org_kind(session, org)),
        created_by=actor.id,
    )
    session.add(row)
    await session.flush()
    if username is not None and password is not None:
        session.add(
            SupplierCredential(
                organization_id=org,
                supplier_id=row.id,
                username=username,
                password_ciphertext=encrypt_portal_password(password),
                created_by=actor.id,
            )
        )
        await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="supplier.created",
        entity_type="supplier",
        entity_id=row.id,
        event_type="catalog.supplier_created",
        aggregate_version=1,
        payload={"supplier_id": str(row.id), "name": row.name},
        after=_snapshot(row),
    )
    return row


async def update_supplier(
    session: AsyncSession,
    actor: Actor,
    supplier_id: uuid.UUID,
    *,
    expected_version: int,
    changes: dict,
) -> Supplier:
    """Aplica solo los campos presentes. `status` activa/desactiva sin borrar historial.

    Semántica de las credenciales del portal (solo los campos presentes cuentan):

    * `portal_url: null` borra la URL; el resto queda igual.
    * `portal_username` y `portal_password` juntos guardan/actualizan la credencial.
    * uno solo de los dos actualiza esa parte conservando la otra.
    * `null` en cualquiera de los dos borra la credencial completa.
    """
    require_admin(actor.roles)
    org = actor.organization_id
    row = await _load(session, actor, supplier_id)
    if row.version != expected_version:
        raise DomainError(
            "VERSION_CONFLICT",
            "El proveedor cambió. Revisa la versión actual antes de guardar.",
            status_code=409,
            details={"current_version": row.version},
        )
    before = _snapshot(row)
    if "name" in changes:
        if changes["name"] is None:
            raise DomainError(
                "INVALID_NAME",
                "El nombre del proveedor no puede quedar vacío.",
                status_code=400,
            )
        row.name = _clean_name(changes["name"])
        await advisory_lock(session, f"supplier:name:{org}")
        await _assert_unique_name(session, org, row.name, exclude=row.id)
    if "supplier_code" in changes:
        row.supplier_code = _clean_optional(changes["supplier_code"], label="El código", max_len=60)
    if "notes" in changes:
        row.notes = _clean_optional(changes["notes"], label="Las notas", max_len=2000)
    if "status" in changes:
        if changes["status"] not in ("active", "inactive"):
            raise DomainError(
                "INVALID_STATUS", "El estado debe ser active o inactive.", status_code=400
            )
        row.status = changes["status"]
    if "portal_url" in changes:
        row.portal_url = _clean_portal_url(changes["portal_url"])
    if "portal_username" in changes or "portal_password" in changes:
        cred = await _credential(session, org, row.id)
        username_given = "portal_username" in changes
        password_given = "portal_password" in changes
        clear = (username_given and changes["portal_username"] is None) or (
            password_given and changes["portal_password"] is None
        )
        if clear:
            if cred is not None:
                await session.delete(cred)
                await session.flush()
        else:
            username = (
                _clean_username(changes["portal_username"])
                if username_given
                else (cred.username if cred is not None else None)
            )
            password = _clean_password(changes["portal_password"]) if password_given else None
            if username is None:
                raise DomainError(
                    "INCOMPLETE_CREDENTIALS",
                    "Falta el usuario del portal para guardar la contraseña.",
                    status_code=400,
                )
            if cred is None:
                if password is None:
                    raise DomainError(
                        "INCOMPLETE_CREDENTIALS",
                        "Para guardar las credenciales del portal se necesitan el usuario y la "
                        "contraseña.",
                        status_code=400,
                    )
                session.add(
                    SupplierCredential(
                        organization_id=org,
                        supplier_id=row.id,
                        username=username,
                        password_ciphertext=encrypt_portal_password(password),
                        created_by=actor.id,
                    )
                )
                await session.flush()
            else:
                cred.username = username
                if password is not None:
                    cred.password_ciphertext = encrypt_portal_password(password)
                cred.updated_at = datetime.now(UTC)
                await session.flush()
    row.version += 1
    row.updated_at = datetime.now(UTC)
    await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="supplier.updated",
        entity_type="supplier",
        entity_id=row.id,
        event_type="catalog.supplier_updated",
        aggregate_version=row.version,
        payload={"supplier_id": str(row.id), "status": row.status},
        before=before,
        after=_snapshot(row),
    )
    return row


async def get_supplier(session: AsyncSession, actor: Actor, supplier_id: uuid.UUID) -> Supplier:
    require_admin(actor.roles)
    row = (
        await session.execute(
            select(Supplier).where(
                Supplier.id == supplier_id, Supplier.organization_id == actor.organization_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("El proveedor")
    return row


async def list_suppliers(
    session: AsyncSession, actor: Actor, *, status: str | None = None
) -> list[Supplier]:
    require_admin(actor.roles)
    q = select(Supplier).where(Supplier.organization_id == actor.organization_id)
    if status is not None:
        if status not in ("active", "inactive"):
            raise DomainError(
                "INVALID_STATUS", "El estado debe ser active o inactive.", status_code=400
            )
        q = q.where(Supplier.status == status)
    return list((await session.execute(q.order_by(Supplier.name))).scalars().all())


async def supplier_payload(session: AsyncSession, supplier: Supplier) -> dict:
    """Forma pública E6. `known_conditions`/`annotations`/`notes` son listas vacías: esas fuentes
    todavía no existen (las condiciones tipadas se leen aparte y E9 llega en C3)."""
    sources = (
        await session.execute(
            select(Source.id, Source.mode)
            .where(
                Source.organization_id == supplier.organization_id,
                Source.supplier_id == supplier.id,
            )
            .order_by(Source.name)
        )
    ).all()
    has_credentials = (
        await _credential(session, supplier.organization_id, supplier.id) is not None
    )
    return {
        "supplier_id": supplier.id,
        "name": supplier.name,
        "supplier_code": supplier.supplier_code,
        "status": supplier.status,
        "version": supplier.version,
        "sources": [sid for sid, _ in sources],
        "known_conditions": [],
        "annotations": [],
        "notes": [],
        "internal_notes": supplier.notes,
        # El portal se informa sin exponer usuario ni contraseña.
        "portal_url": supplier.portal_url,
        "has_portal_credentials": has_credentials,
        "portal_status": _portal_status(supplier, has_credentials),
        "has_price_list": any(mode == "manual_import" for _, mode in sources),
        "created_at": supplier.created_at,
        "updated_at": supplier.updated_at,
    }
