"""Perfil de importación por fuente, vista previa de la planilla y ofertas ya importadas (A3).

La pantalla de Proveedores necesita tres cosas que el motor de E8 no exponía:

* **Vista previa** — leer un archivo real y mostrar cabeceras, primeras filas y una propuesta de
  mapeo. La propuesta es solo una coincidencia de nombres de columna: no completa datos que el
  archivo no trae y no crea nada.
* **Perfil** — leer y guardar el mapeo columna→papel por fuente, con versión. La importación
  guarda su propia instantánea, así que editar el perfil nunca reescribe el pasado.
* **Ofertas** — la última observación de cada SKU de cada fuente, con su precio y su fecha.

Nada de esto crea artículos ni vínculos; eso solo ocurre en una importación real y solo si el
perfil lo pide explícitamente (`options.create_missing_items`).
"""

import re
import uuid
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import xlrd
from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.core.permissions import require_admin
from app.models import (
    ImportProfile,
    OfferSnapshot,
    Source,
    Supplier,
    TradeItem,
)
from app.services.actors import Actor
from app.services.events import advisory_lock, emit
from app.services.imports import (
    FILE_FORMATS,
    MAX_BYTES,
    MAX_ROWS,
    ROLES,
    UNITS,
    _json_value,
    _load_profile,
    _normalize_header,
    _text,
    detect_file_format,
    read_raw_rows,
)
from app.services.provenance import org_kind, origin_for

OPTION_KEYS = {
    "currency",
    "price_basis",
    "decimal_separator",
    "thousands_separator",
    "date_format",
    "tax_included",
    "discount_included",
    "pack_base_unit",
    "quantity_unit",
    "default_sale_unit",
    "create_missing_items",
}

# Nombres de cabecera que se reconocen para proponer un mapeo. Son coincidencias exactas después
# de normalizar; no se buscan parecidos ni se completa lo que el archivo no declara.
HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "sku": (
        "sku",
        "codigo",
        "código",
        "cod",
        "code",
        "codigo proveedor",
        "código proveedor",
        "cod proveedor",
        "codigo producto",
        "código producto",
        "id producto",
        "codigo articulo",
        "código artículo",
    ),
    "name": (
        "nombre",
        "descripcion",
        "descripción",
        "producto",
        "nombre producto",
        "descripcion producto",
        "descripción producto",
        "detalle",
        "glosa",
    ),
    "price": (
        "precio",
        "precio neto",
        "precio unitario",
        "precio compra",
        "precio de compra",
        "precio lista",
        "precio de lista",
        "valor",
        "price",
    ),
    "sale_unit": (
        "unidad",
        "unidad venta",
        "unidad de venta",
        "formato",
        "presentacion",
        "presentación",
        "um",
        "u/m",
        "unidad medida",
        "unidad de medida",
    ),
    "currency": ("moneda", "currency"),
    "basis": ("base", "base precio", "base de precio", "price basis"),
    "pack": (
        "contenido",
        "contenido por unidad",
        "cantidad por caja",
        "unidades por caja",
        "unidades por empaque",
        "pack",
    ),
    "minimum": ("minimo", "mínimo", "compra minima", "compra mínima", "min"),
    "multiple": ("multiplo", "múltiplo", "multiple", "multiplicador"),
    "stock": ("stock", "disponible", "inventario", "existencia"),
    "observed_at": (
        "fecha",
        "fecha precio",
        "fecha de precio",
        "fecha actualizacion",
        "fecha actualización",
        "vigencia",
    ),
    "conditions": (
        "condiciones",
        "condiciones comerciales",
        "observaciones",
        "observacion",
        "observación",
    ),
}
_HEADER_SCAN = 15
_SAMPLE_ROWS = 5


def _synonym_index() -> dict[str, str]:
    return {
        _normalize_header(alias): role
        for role, aliases in HEADER_SYNONYMS.items()
        for alias in aliases
    }


async def _source(session: AsyncSession, actor: Actor, source_id: uuid.UUID) -> Source:
    require_admin(actor.roles)
    row = (
        await session.execute(
            select(Source).where(
                Source.organization_id == actor.organization_id, Source.id == source_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise not_found("La fuente")
    if row.kind != "supplier":
        raise DomainError(
            "INVALID_SOURCE",
            f"«{row.name}» no es una fuente de proveedor: una lista de precios de proveedor no "
            "se importa en una fuente de cadena.",
            status_code=422,
        )
    return row


def _clean_columns(columns: dict[str, Any], *, default_sale_unit: str | None) -> dict[str, Any]:
    if not isinstance(columns, dict) or not columns:
        raise DomainError(
            "IMPORT_FORMAT_INVALID", "El perfil no declara ninguna columna.", status_code=422
        )
    unknown = sorted(set(columns) - set(ROLES))
    if unknown:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"El perfil declara papeles desconocidos: {', '.join(unknown)}.",
            status_code=422,
        )
    required = ("sku", "price") if default_sale_unit else ("sku", "price", "sale_unit")
    missing = [role for role in required if role not in columns]
    if missing:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"El perfil no mapea columnas obligatorias: {', '.join(missing)}.",
            status_code=422,
        )
    clean: dict[str, Any] = {}
    for role, spec in columns.items():
        if not isinstance(spec, dict):
            raise DomainError(
                "IMPORT_FORMAT_INVALID",
                f"La columna de «{role}» debe ser un objeto con «header» o «position».",
                status_code=422,
            )
        header = spec.get("header")
        aliases = spec.get("aliases") or []
        position = spec.get("position")
        if header is not None and (not isinstance(header, str) or not header.strip()):
            raise DomainError(
                "IMPORT_FORMAT_INVALID",
                f"La cabecera de «{role}» debe ser texto no vacío.",
                status_code=422,
            )
        if not isinstance(aliases, list) or any(
            not isinstance(alias, str) or not alias.strip() for alias in aliases
        ):
            raise DomainError(
                "IMPORT_FORMAT_INVALID",
                f"Los alias de «{role}» deben ser una lista de textos.",
                status_code=422,
            )
        if position is not None and (
            not isinstance(position, int) or isinstance(position, bool) or position < 1
        ):
            raise DomainError(
                "IMPORT_FORMAT_INVALID",
                f"La posición de «{role}» debe ser un entero mayor o igual a 1.",
                status_code=422,
            )
        if header is None and position is None:
            raise DomainError(
                "IMPORT_FORMAT_INVALID",
                f"La columna de «{role}» necesita «header» o «position».",
                status_code=422,
            )
        clean[role] = {
            **({"header": header.strip()} if header else {}),
            **({"aliases": [a.strip() for a in aliases]} if aliases else {}),
            **({"position": position} if position is not None else {}),
        }
    return clean


def _clean_units(units: dict[str, str]) -> dict[str, str]:
    if not isinstance(units, dict):
        raise DomainError(
            "IMPORT_FORMAT_INVALID", "«units» del perfil debe ser un objeto.", status_code=422
        )
    bad = sorted({str(v) for v in units.values()} - set(UNITS))
    if bad:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"El perfil traduce unidades a valores no permitidos: {', '.join(bad)}. "
            f"Válidas: {', '.join(UNITS)}.",
            status_code=422,
        )
    return {str(k).strip(): str(v) for k, v in units.items()}


def _clean_options(options: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(options, dict):
        raise DomainError(
            "IMPORT_FORMAT_INVALID", "«options» del perfil debe ser un objeto.", status_code=422
        )
    unknown = sorted(set(options) - OPTION_KEYS)
    if unknown:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"El perfil declara opciones desconocidas: {', '.join(unknown)}.",
            status_code=422,
        )
    return options


def _validate_with_engine(
    *,
    org: uuid.UUID,
    source_id: uuid.UUID,
    file_format: str,
    sheet: str | None,
    header_row: int,
    columns: dict[str, Any],
    units: dict[str, str],
    options: dict[str, Any],
) -> None:
    """Valida con el mismo cargador que usa la importación: una sola definición de las reglas."""
    probe = ImportProfile(
        id=uuid.uuid4(),
        organization_id=org,
        source_id=source_id,
        file_format=file_format,
        sheet=sheet,
        header_row=header_row,
        columns=columns,
        units=units,
        options=options,
        status="active",
        version=1,
    )
    _load_profile(probe)


def profile_payload(profile: ImportProfile | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {
        "source_id": profile.source_id,
        "file_format": profile.file_format,
        "sheet": profile.sheet,
        "header_row": profile.header_row,
        "columns": profile.columns,
        "units": profile.units,
        "options": profile.options,
        "status": profile.status,
        "version": profile.version,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


async def get_profile(
    session: AsyncSession, actor: Actor, source_id: uuid.UUID
) -> ImportProfile | None:
    source = await _source(session, actor, source_id)
    return (
        await session.execute(
            select(ImportProfile).where(
                ImportProfile.organization_id == actor.organization_id,
                ImportProfile.source_id == source.id,
            )
        )
    ).scalar_one_or_none()


async def save_profile(
    session: AsyncSession,
    actor: Actor,
    source_id: uuid.UUID,
    *,
    file_format: str,
    sheet: str | None,
    header_row: int,
    columns: dict[str, Any],
    units: dict[str, str],
    options: dict[str, Any],
    expected_version: int | None = None,
) -> ImportProfile:
    source = await _source(session, actor, source_id)
    org = actor.organization_id
    if file_format not in FILE_FORMATS:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"El formato «{file_format}» no está soportado; usa .xlsx, .xls o .csv.",
            status_code=422,
        )
    if not isinstance(header_row, int) or isinstance(header_row, bool) or header_row < 1:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            "La fila de cabecera debe ser un entero mayor o igual a 1.",
            status_code=422,
        )
    if file_format == "csv" and sheet is not None:
        raise DomainError(
            "IMPORT_FORMAT_INVALID", "Un archivo .csv no tiene hojas: quita la hoja del perfil.",
            status_code=422,
        )
    clean_sheet = sheet.strip() if isinstance(sheet, str) and sheet.strip() else None
    clean_options = _clean_options(options)
    clean_units = _clean_units(units)
    default_unit = clean_options.get("default_sale_unit")
    clean_columns = _clean_columns(columns, default_sale_unit=default_unit)
    _validate_with_engine(
        org=org,
        source_id=source.id,
        file_format=file_format,
        sheet=clean_sheet,
        header_row=header_row,
        columns=clean_columns,
        units=clean_units,
        options=clean_options,
    )

    await advisory_lock(session, f"import-profile:{org}:{source.id}")
    existing = (
        await session.execute(
            select(ImportProfile)
            .where(ImportProfile.organization_id == org, ImportProfile.source_id == source.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if expected_version is not None:
        current = existing.version if existing is not None else 0
        if current != expected_version:
            raise DomainError(
                "VERSION_CONFLICT",
                "El perfil de importación cambió. Revisa la versión actual antes de guardar.",
                status_code=409,
                details={"current_version": current},
            )
    if existing is None:
        profile = ImportProfile(
            organization_id=org,
            source_id=source.id,
            file_format=file_format,
            sheet=clean_sheet,
            header_row=header_row,
            columns=clean_columns,
            units=clean_units,
            options=clean_options,
            data_origin=origin_for(await org_kind(session, org)),
            created_by=actor.id,
        )
        session.add(profile)
        await session.flush()
    else:
        profile = existing
        profile.file_format = file_format
        profile.sheet = clean_sheet
        profile.header_row = header_row
        profile.columns = clean_columns
        profile.units = clean_units
        profile.options = clean_options
        profile.version += 1
        profile.updated_at = datetime.now(UTC)
        await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="import_profile.saved",
        entity_type="import_profile",
        entity_id=profile.id,
        event_type="catalog.import_profile_saved",
        aggregate_version=profile.version,
        payload={
            "source_id": str(source.id),
            "file_format": file_format,
            "header_row": header_row,
            "roles": sorted(clean_columns),
        },
    )
    return profile


# ---------- vista previa ----------


def _sheet_names(content: bytes, file_format: str) -> list[str]:
    if file_format == "csv":
        return []
    if file_format == "xlsx":
        wb = load_workbook(BytesIO(content), read_only=True)
        try:
            return list(wb.sheetnames)
        finally:
            wb.close()
    wb = xlrd.open_workbook(file_contents=content, on_demand=True)
    try:
        return list(wb.sheet_names())
    finally:
        wb.release_resources()


def _guess_header_row(rows: list[tuple[int, tuple[Any, ...]]]) -> int:
    """Fila con más cabeceras reconocidas entre las primeras; si no hay, la primera."""
    synonyms = _synonym_index()
    best_row, best_score = 1, 0
    for number, values in rows[:_HEADER_SCAN]:
        score = sum(1 for value in values if _normalize_header(value) in synonyms)
        if score > best_score:
            best_row, best_score = number, score
    return best_row if best_score >= 2 else (rows[0][0] if rows else 1)


def _suggested_columns(header: tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    synonyms = _synonym_index()
    used: set[int] = set()
    suggested: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(header, start=1):
        role = synonyms.get(_normalize_header(value))
        if role is None or role in suggested or index in used:
            continue
        text = _text(value)
        if text is None:
            continue
        suggested[role] = {"header": text[:120], "position": index}
        used.add(index)
    return suggested


def _suggested_decimal_separator(rows: list[tuple[int, tuple[Any, ...]]], price_index: int | None) -> str:
    if price_index is None:
        return "."
    values = [
        _text(values[price_index])
        for _number, values in rows
        if price_index < len(values) and _text(values[price_index])
    ]
    if any(re.fullmatch(r"\d+,\d{1,4}", v or "") for v in values):
        return ","
    return "."


def _preview_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:200]
    return _json_value(value)


async def preview_import(
    session: AsyncSession,
    actor: Actor,
    source_id: uuid.UUID,
    *,
    file_name: str | None,
    content: bytes,
    sheet: str | None = None,
    header_row: int | None = None,
) -> dict[str, Any]:
    """Lee el archivo tal como viene. No escribe perfil, ofertas ni artículos.

    `sheet` y `header_row` permiten repetir la lectura con otra hoja o cabecera cuando la detección
    automática no coincide con el archivo real; sin ellos se usa la primera hoja y la cabecera con
    más nombres reconocidos.
    """
    await _source(session, actor, source_id)
    if len(content) > MAX_BYTES:
        raise DomainError(
            "PAYLOAD_TOO_LARGE", "El archivo supera los 10 MB permitidos.", status_code=413
        )
    file_format = detect_file_format(file_name, content)
    sheets = _sheet_names(content, file_format)
    rows = read_raw_rows(content, file_format, sheet)
    if len(rows) > MAX_ROWS + _HEADER_SCAN:
        raise DomainError(
            "PAYLOAD_TOO_LARGE",
            f"La planilla supera las {MAX_ROWS} filas de datos permitidas.",
            status_code=413,
        )
    if not rows:
        raise DomainError("IMPORT_FORMAT_INVALID", "El archivo no tiene filas.", status_code=422)
    if header_row is None:
        header_row = _guess_header_row(rows)
    elif header_row not in {number for number, _values in rows}:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"La fila {header_row} no existe en la hoja seleccionada.",
            status_code=422,
        )
    header = next((values for number, values in rows if number == header_row), ())
    suggested = _suggested_columns(header)
    price_index = suggested.get("price", {}).get("position")
    price_index = price_index - 1 if isinstance(price_index, int) else None
    data_rows = [(number, values) for number, values in rows if number > header_row]
    sample = [
        {
            "row": number,
            "values": [_preview_value(value) for value in values[:40]],
        }
        for number, values in data_rows[:_SAMPLE_ROWS]
        if any(not (value is None or (isinstance(value, str) and not value.strip())) for value in values)
    ]
    return {
        "file_format": file_format,
        "sheets": sheets,
        # La hoja que realmente se leyó: la pedida o, si no se pidió, la primera.
        "sheet": sheet or (sheets[0] if sheets else None),
        "header_row": header_row,
        "headers": [_text(value) for value in header],
        "sample_rows": sample,
        "row_count": sum(
            1
            for _number, values in data_rows
            if any(
                not (value is None or (isinstance(value, str) and not value.strip()))
                for value in values
            )
        ),
        "suggested_columns": suggested,
        # Solo se propone lo que se puede ver en el archivo. Moneda, base de precio y unidad por
        # defecto quedan nulas: inventarlas sería falsear la oferta.
        "suggested_options": {
            "decimal_separator": _suggested_decimal_separator(data_rows, price_index),
            "currency": None,
            "price_basis": None,
            "default_sale_unit": None,
        },
    }


# ---------- ofertas ya importadas ----------


async def latest_offers(
    session: AsyncSession, actor: Actor, supplier_id: uuid.UUID, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Última observación de cada SKU de cada fuente del proveedor, con precio y fecha."""
    require_admin(actor.roles)
    org = actor.organization_id
    supplier = (
        await session.execute(
            select(Supplier.id).where(Supplier.organization_id == org, Supplier.id == supplier_id)
        )
    ).scalar_one_or_none()
    if supplier is None:
        raise not_found("El proveedor")
    rank = (
        func.row_number()
        .over(
            partition_by=(OfferSnapshot.source_id, OfferSnapshot.external_id),
            order_by=(OfferSnapshot.observed_at.desc(), OfferSnapshot.created_at.desc()),
        )
        .label("rank")
    )
    latest = (
        select(OfferSnapshot.id.label("offer_id"), rank)
        .where(
            OfferSnapshot.organization_id == org,
            OfferSnapshot.supplier_id == supplier_id,
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(OfferSnapshot, TradeItem.display_name, Source.name)
            .join(latest, latest.c.offer_id == OfferSnapshot.id)
            .join(
                TradeItem,
                (TradeItem.id == OfferSnapshot.trade_item_id)
                & (TradeItem.organization_id == org),
            )
            .join(
                Source,
                (Source.id == OfferSnapshot.source_id) & (Source.organization_id == org),
            )
            .where(latest.c.rank == 1)
            .order_by(OfferSnapshot.observed_at.desc(), OfferSnapshot.external_id)
            .limit(limit)
        )
    ).all()
    return [
        {
            "offer_id": offer.id,
            "source_id": offer.source_id,
            "source_name": source_name,
            "external_id": offer.external_id,
            "external_name": offer.external_name,
            "trade_item_id": offer.trade_item_id,
            "trade_item_name": item_name,
            "price_amount": str(offer.price_amount),
            "currency": offer.currency,
            "price_basis": offer.price_basis,
            "sale_unit": offer.sale_unit,
            "sale_unit_text": offer.sale_unit_text,
            "pack_units_per_commercial_unit": (
                str(offer.pack_units_per_commercial_unit)
                if offer.pack_units_per_commercial_unit is not None
                else None
            ),
            "pack_base_unit": offer.pack_base_unit,
            "stock_status": offer.stock_status,
            "stock_quantity": (
                str(offer.stock_quantity) if offer.stock_quantity is not None else None
            ),
            "review_status": offer.review_status,
            "observed_at": offer.observed_at,
            "imported_at": offer.created_at,
        }
        for offer, item_name, source_name in rows
    ]
