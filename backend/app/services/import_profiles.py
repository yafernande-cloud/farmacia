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

import hashlib
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any

import xlrd
from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
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
    INVALID_BASIS,
    MAX_BYTES,
    MAX_ROWS,
    REQUIRED_ROLES,
    ROLES,
    UNITS,
    UNKNOWN_UNIT,
    _header_variants,
    _json_value,
    _load_profile,
    _normalize_header,
    _text,
    columns_compatible,
    detect_file_format,
    read_raw_rows,
    run_import,
)
from app.services.provenance import org_kind, origin_for

OPTION_KEYS = {
    "currency",
    "price_basis",
    "basis_values",
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
        "product sku",
        "supplier sku",
        "product id",
        "item id",
        "external id",
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
        "cod articulo",
        "codigo interno",
        "código interno",
        "cod interno",
        "codigo de producto",
        "sku proveedor",
        "codigo sku",
        "nro codigo",
        "n° codigo",
    ),
    "name": (
        "name",
        "nombre",
        "descripcion",
        "descripción",
        "producto",
        "nombre producto",
        "descripcion producto",
        "descripción producto",
        "product name",
        "item name",
        "article name",
        "description",
        "product description",
        "detalle",
        "glosa",
        "descripcion articulo",
        "descripción artículo",
        "nombre articulo",
        "nombre del producto",
        "descripcion del producto",
        "detalle producto",
        "nombre comercial",
    ),
    "price": (
        "price",
        "precio",
        "net price",
        "precio neto",
        "unit price",
        "precio unitario",
        "list price",
        "precio lista",
        "precio de lista",
        "purchase price",
        "precio compra",
        "precio de compra",
        "precio final",
        "valor",
        "precio costo",
        "costo",
        "valor unitario",
        "precio unitario neto",
        "precio neto unitario",
        "precio por unidad",
        "valor neto",
    ),
    "sale_unit": (
        "sale unit",
        "sales unit",
        "unit of sale",
        "selling unit",
        "commercial unit",
        "unidad de venta",
        "unidad venta",
        "unidad comercial",
        "unidad de compra",
        "venta unidad",
        "tipo unidad",
        "tipo de unidad",
        "unidad",
    ),
    "currency": (
        "currency",
        "currency code",
        "price currency",
        "moneda",
        "tipo moneda",
        "tipo de moneda",
        "moneda lista",
    ),
    "basis": (
        "basis",
        "price basis",
        "base de precio",
        "base del precio",
        "base precio",
        "tipo de base",
        "tipo base",
        "base",
    ),
    "pack": (
        "pack",
        "pack size",
        "unit pack",
        "units per pack",
        "units per box",
        "contenido",
        "contenido por unidad",
        "cantidad por caja",
        "unidades por caja",
        "unidades por empaque",
        "unidades por envase",
        "cantidad por envase",
        "contenido neto",
    ),
    "minimum": (
        "minimum",
        "minimum order",
        "minimum quantity",
        "min order",
        "min order qty",
        "min quantity",
        "minimo",
        "mínimo",
        "compra minima",
        "compra mínima",
        "cantidad minima",
        "cantidad mínima",
        "min",
    ),
    "multiple": (
        "multiple",
        "order multiple",
        "selling multiple",
        "multiple of",
        "multiplo",
        "múltiplo",
        "multiplicador",
        "multiplo compra",
        "múltiplo compra",
    ),
    "stock": (
        "stock",
        "available stock",
        "stock available",
        "on hand",
        "disponible",
        "disponibilidad",
        "inventario",
        "existencia",
        "stock disponible",
        "unidades disponibles",
    ),
    "observed_at": (
        "observed at",
        "date",
        "price date",
        "last update",
        "last updated",
        "updated at",
        "valid from",
        "validity date",
        "effective date",
        "fecha",
        "fecha precio",
        "fecha de precio",
        "fecha actualizacion",
        "fecha actualización",
        "vigencia",
        "fecha lista",
        "fecha de lista",
        "fecha vigencia",
        "ultima actualizacion",
        "última actualización",
    ),
    "conditions": (
        "conditions",
        "commercial conditions",
        "remarks",
        "comments",
        "notes",
        "condiciones",
        "condiciones comerciales",
        "observaciones",
        "observacion",
        "observación",
        "condicion",
        "condición",
        "nota",
        "notas",
        "comentario",
        "comentarios",
    ),
}

# Nombre canónico de cada papel: una columna que se llama exactamente así manda sobre cualquier
# sinónimo. Resuelve, por ejemplo, que «price» gane a «net_price» o que «sale_unit» gane a
# «unidad» sin mirar la posición de las columnas.
_CANONICAL_HEADERS: dict[str, str] = {}
for _role, _names in {
    "sku": ("sku", "codigo", "código"),
    "name": ("name", "nombre", "descripcion", "descripción"),
    "price": ("price", "precio"),
    "sale_unit": ("sale unit", "unidad de venta", "unidad comercial"),
    "currency": ("currency", "moneda"),
    "basis": ("basis", "base de precio", "base del precio"),
    "pack": ("pack",),
    "minimum": ("minimum", "minimo", "mínimo"),
    "multiple": ("multiple", "multiplo", "múltiplo"),
    "stock": ("stock",),
    "observed_at": ("observed at", "fecha"),
    "conditions": ("conditions", "condiciones"),
}.items():
    for _name in _names:
        _CANONICAL_HEADERS[_normalize_header(_name)] = _role

# Alias válidos pero poco específicos: se aceptan si no hay una coincidencia mejor. Nunca se usa
# «presentación» como unidad comercial: describe el producto, no cómo se vende.
_GENERIC_HEADERS = frozenset(
    {
        "unidad",
        "base",
        "contenido",
        "fecha",
        "valor",
        "detalle",
        "glosa",
        "cod",
        "code",
        "min",
        "nota",
        "notas",
        "comentario",
        "comentarios",
        "observaciones",
        "observacion",
        "observación",
    }
)

# Equivalencias de unidad comercial que no dejan ambigüedad. Solo se aplican al texto de la
# columna de unidad: nunca se deducen de mg/ml, del nombre ni de la presentación del producto.
_UNIT_ALIASES: dict[str, str] = {
    "box": "box",
    "boxes": "box",
    "caja": "box",
    "cajas": "box",
    "package": "package",
    "packages": "package",
    "paquete": "package",
    "paquetes": "package",
    "pack": "package",
    "packs": "package",
    "pkg": "package",
    "unit": "unit",
    "units": "unit",
    "unidad": "unit",
    "unidades": "unit",
    "bottle": "bottle",
    "bottles": "bottle",
    "frasco": "bottle",
    "frascos": "bottle",
    "botella": "bottle",
    "botellas": "bottle",
}
_HEADER_SCAN = 15
_SAMPLE_ROWS = 5
# Filas interpretadas que devuelve la revisión previa; los agregados cubren todas.
_CHECK_ROWS = 200


def _synonym_index() -> dict[str, str]:
    return {
        _normalize_header(alias): role
        for role, aliases in HEADER_SYNONYMS.items()
        for alias in aliases
    }


def _synonym_role(value: Any, synonyms: dict[str, str]) -> str | None:
    """Papel de una cabecera, probando también su forma sin paréntesis («Precio (CLP)»)."""
    for key in _header_variants(value):
        role = synonyms.get(key)
        if role is not None:
            return role
    return None


def _header_match(value: Any) -> tuple[str, int] | None:
    """Papel y especificidad de una cabecera: exacta y canónica vale más que un alias genérico.

    La posición nunca decide el papel; solo desempata cuando dos columnas son igual de
    específicas para el mismo papel (por ejemplo, dos columnas llamadas «Precio»).
    """
    synonyms = _synonym_index()
    best: tuple[str, int] | None = None
    for depth, key in enumerate(_header_variants(value)):
        role = synonyms.get(key)
        if role is None:
            continue
        if _CANONICAL_HEADERS.get(key) == role:
            score = 3
        elif key in _GENERIC_HEADERS:
            score = 1
        else:
            score = 2
        # La forma entre paréntesis («Precio (CLP)») es una coincidencia un poco más débil.
        score = max(score - depth, 1)
        if best is None or score > best[1]:
            best = (role, score)
    return best


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


def _clean_columns(
    columns: dict[str, Any],
    *,
    default_sale_unit: str | None,
    allow_missing: bool = False,
) -> dict[str, Any]:
    if not isinstance(columns, dict) or (not columns and not allow_missing):
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
    if missing and not allow_missing:
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


def _best_header(rows: list[tuple[int, tuple[Any, ...]]]) -> tuple[int, int]:
    """(fila, puntaje) con más cabeceras reconocidas entre las primeras filas."""
    synonyms = _synonym_index()
    best_row, best_score = (rows[0][0] if rows else 1), 0
    for number, values in rows[:_HEADER_SCAN]:
        score = sum(1 for value in values if _synonym_role(value, synonyms) is not None)
        if score > best_score:
            best_row, best_score = number, score
    return best_row, best_score


def _guess_header_row(rows: list[tuple[int, tuple[Any, ...]]]) -> int:
    """Fila con más cabeceras reconocidas entre las primeras; si no hay, la primera."""
    best_row, best_score = _best_header(rows)
    return best_row if best_score >= 2 else (rows[0][0] if rows else 1)


def _peek_sheet(content: bytes, file_format: str, sheet: str) -> list[tuple[int, tuple[Any, ...]]]:
    """Primeras filas de una hoja, sin leerla entera: basta para reconocer su cabecera."""
    if file_format == "xlsx":
        wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
        try:
            ws = wb[sheet]
            return [
                (index, tuple(values))
                for index, values in enumerate(
                    ws.iter_rows(max_row=_HEADER_SCAN, values_only=True), start=1
                )
            ]
        finally:
            wb.close()
    wb = xlrd.open_workbook(file_contents=content, on_demand=True)
    try:
        hoja = wb.sheet_by_name(sheet)
        return [
            (index + 1, tuple(hoja.row_values(index)))
            for index in range(min(hoja.nrows, _HEADER_SCAN))
        ]
    finally:
        wb.release_resources()


def _suggest_sheet(content: bytes, file_format: str, sheets: list[str]) -> str | None:
    """Hoja que más se parece a una lista de productos; la primera si ninguna destaca.

    Puntúa cada hoja por cantidad de cabeceras reconocidas en sus primeras filas. Solo cambia la
    hoja por defecto cuando encuentra una cabecera clara (al menos dos nombres reconocidos): si
    ninguna destaca, se mantiene el comportamiento anterior de leer la primera.
    """
    if file_format == "csv" or not sheets:
        return None
    best_name, best_score = sheets[0], 0
    for name in sheets:
        try:
            _row, score = _best_header(_peek_sheet(content, file_format, name))
        except Exception:  # noqa: BLE001 — una hoja ilegible no debe impedir revisar las demás
            continue
        if score > best_score:
            best_name, best_score = name, score
    return best_name if best_score >= 2 else sheets[0]


def _suggested_columns(header: tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    """Mapeo propuesto: primero las coincidencias exactas y específicas, nunca por posición.

    Cada columna alimenta como mucho un papel y cada papel se queda con la mejor columna que lo
    nombre. Una coincidencia genérica («Valor», «Base») solo se usa si ninguna otra más precisa
    cubre el papel.
    """
    candidates: list[tuple[int, int, str, str]] = []
    for index, value in enumerate(header, start=1):
        match = _header_match(value)
        text = _text(value)
        if match is None or text is None:
            continue
        role, score = match
        candidates.append((score, index, role, text[:120]))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    used: set[int] = set()
    suggested: dict[str, dict[str, Any]] = {}
    for _score, index, role, text in candidates:
        if role in suggested or index in used:
            continue
        suggested[role] = {"header": text, "position": index}
        used.add(index)
    return suggested


def _suggested_units(
    data_rows: list[tuple[int, tuple[Any, ...]]], sale_unit_index: int | None
) -> dict[str, str]:
    """Traduce los textos de unidad inequívocos del propio archivo (`caja`→`box`).

    No se deduce nada de mg/ml, del nombre ni de la presentación: solo se traduce la palabra
    escrita en la columna de unidad. Lo que no está aquí se pregunta por valor o se rechaza en su
    fila, sin bloquear la lista completa.
    """
    if sale_unit_index is None:
        return {}
    units: dict[str, str] = {}
    for _number, values in data_rows:
        if sale_unit_index >= len(values):
            continue
        text = _text(values[sale_unit_index])
        if text is None:
            continue
        unit = _UNIT_ALIASES.get(text.casefold())
        if unit is not None:
            units[text.casefold()] = unit
    return units


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
    automática no coincide con el archivo real; sin ellos se elige la hoja que más se parece a una
    lista de productos (la primera si ninguna destaca) y la cabecera con más nombres reconocidos.
    """
    await _source(session, actor, source_id)
    if len(content) > MAX_BYTES:
        raise DomainError(
            "PAYLOAD_TOO_LARGE", "El archivo supera los 10 MB permitidos.", status_code=413
        )
    file_format = detect_file_format(file_name, content)
    sheets = _sheet_names(content, file_format)
    suggested_sheet = _suggest_sheet(content, file_format, sheets) if sheet is None else None
    if sheet is None:
        sheet = suggested_sheet
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
        # La hoja que realmente se leyó y, cuando no se pidió, la que se propone.
        "sheet": sheet or (sheets[0] if sheets else None),
        "suggested_sheet": suggested_sheet,
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


# ---------- revisión previa (sin escrituras) ----------


def _aggregate_problems(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Problemas agrupados por código y campos: una pregunta cubre muchas filas."""
    groups: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for item in rows:
        problem = item.get("problem")
        if not problem:
            continue
        fields = tuple(problem.get("fields") or ())
        key = (problem["code"], fields)
        group = groups.get(key)
        if group is None:
            group = {
                "code": problem["code"],
                "message": problem["message"],
                "fields": list(fields),
                "count": 0,
                "sample_rows": [],
            }
            groups[key] = group
        group["count"] += 1
        if len(group["sample_rows"]) < 20:
            group["sample_rows"].append(item["row"])
    return sorted(groups.values(), key=lambda g: (-g["count"], g["code"]))


def _unrecognised_texts(
    rows: list[dict[str, Any]], *, code: str, role: str
) -> list[dict[str, Any]]:
    """Valores de una columna que el mapeo todavía no interpreta, con cuántas filas afectan."""
    groups: dict[str, dict[str, Any]] = {}
    for item in rows:
        problem = item.get("problem")
        if not problem or problem["code"] != code:
            continue
        text = str((problem.get("values") or {}).get(role) or "").strip()
        if not text:
            continue
        group = groups.setdefault(text, {"text": text, "count": 0, "sample_rows": []})
        group["count"] += 1
        if len(group["sample_rows"]) < 20:
            group["sample_rows"].append(item["row"])
    return sorted(groups.values(), key=lambda g: (-g["count"], g["text"]))


def _unrecognised_units(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Textos de unidad que el perfil todavía no traduce."""
    return _unrecognised_texts(rows, code=UNKNOWN_UNIT, role="sale_unit")


def _unrecognised_bases(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Textos de base de precio que el perfil todavía no confirma (p. ej. «unit»)."""
    return _unrecognised_texts(rows, code=INVALID_BASIS, role="basis")


def _profile_values(
    session_profile: ImportProfile | None,
    payload: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any], str, int]:
    """Columnas, unidades y opciones a usar, más su procedencia y versión."""
    if payload is not None:
        return (
            payload.get("columns") or {},
            payload.get("units") or {},
            payload.get("options") or {},
            "request",
            0,
        )
    if session_profile is None:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            "Esta fuente todavía no tiene un mapeo de columnas. Revisa las columnas antes de "
            "continuar.",
            status_code=422,
        )
    return (
        dict(session_profile.columns or {}),
        dict(session_profile.units or {}),
        dict(session_profile.options or {}),
        "saved",
        session_profile.version,
    )


def _missing_required_roles(
    columns: dict[str, Any], options: dict[str, Any]
) -> list[str]:
    """Roles que el mapeo no cubre y que el flujo automático debe resolver antes de importar."""
    missing = [role for role in ("sku", "price") if not columns.get(role)]
    if not columns.get("sale_unit") and not options.get("default_sale_unit"):
        missing.append("sale_unit")
    return missing


async def check_import(
    session: AsyncSession,
    actor: Actor,
    source_id: uuid.UUID,
    *,
    file_name: str | None,
    content: bytes,
    sheet: str | None = None,
    header_row: int | None = None,
    payload: dict[str, Any] | None = None,
    auto: bool = False,
) -> dict[str, Any]:
    """Revisa la planilla con el mapeo indicado y devuelve el resultado por fila.

    **No escribe nada**: ni ofertas, ni artículos, ni perfil, ni `import_run`. Reutiliza el motor de
    E8 con `dry_run`, así que lo que muestra es exactamente lo que la importación haría. El mapeo
    puede venir en la petición (todavía sin guardar) o tomarse del perfil guardado de la fuente.

    `auto=True` es el flujo de un botón: si el perfil guardado calza **por nombre** con el archivo
    se reutiliza; si no, se propone un mapeo a partir de los encabezados reconocidos. Nunca se
    completan moneda, base ni unidad por defecto. La respuesta incluye `profile`, el mapeo efectivo
    que el cliente debe guardar tal cual antes de importar, y `missing_columns` cuando faltan roles
    obligatorios que no se pueden inventar.
    """
    source = await _source(session, actor, source_id)
    if len(content) > MAX_BYTES:
        raise DomainError(
            "PAYLOAD_TOO_LARGE", "El archivo supera los 10 MB permitidos.", status_code=413
        )
    file_format = detect_file_format(file_name, content)
    sheets = _sheet_names(content, file_format)
    saved = (
        await session.execute(
            select(ImportProfile).where(
                ImportProfile.organization_id == actor.organization_id,
                ImportProfile.source_id == source.id,
            )
        )
    ).scalar_one_or_none()
    # En modo automático se prefiere la hoja y la cabecera del perfil guardado: la revisión debe
    # leer exactamente las mismas celdas que leerá la importación.
    reusable = (
        auto
        and payload is None
        and saved is not None
        and saved.file_format == file_format
    )
    if file_format == "csv":
        sheet = None
    elif sheet is None:
        if reusable and saved is not None and saved.sheet in sheets:
            sheet = saved.sheet
        else:
            sheet = _suggest_sheet(content, file_format, sheets)
    rows = read_raw_rows(content, file_format, sheet)
    if len(rows) > MAX_ROWS + _HEADER_SCAN:
        raise DomainError(
            "PAYLOAD_TOO_LARGE",
            f"La planilla supera las {MAX_ROWS} filas de datos permitidas.",
            status_code=413,
        )
    if not rows:
        raise DomainError("IMPORT_FORMAT_INVALID", "El archivo no tiene filas.", status_code=422)
    numbers = {number for number, _values in rows}
    if header_row is None:
        if reusable and saved is not None and saved.header_row in numbers:
            header_row = saved.header_row
        else:
            header_row = _guess_header_row(rows)
    elif header_row not in numbers:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"La fila {header_row} no existe en la hoja seleccionada.",
            status_code=422,
        )
    header = next((values for number, values in rows if number == header_row), ())
    ignored_columns: list[dict[str, Any]] = []
    if payload is not None:
        columns = payload.get("columns") or {}
        units = payload.get("units") or {}
        options = payload.get("options") or {}
        profile_source, profile_version = "request", 0
    elif reusable and columns_compatible(
        saved.columns if saved is not None else None,
        header,
        required_roles=("sku", "price")
        if saved is not None and (saved.options or {}).get("default_sale_unit")
        else REQUIRED_ROLES,
    ):
        assert saved is not None
        columns = dict(saved.columns or {})
        units = dict(saved.units or {})
        options = dict(saved.options or {})
        profile_source, profile_version = "saved", saved.version
    elif auto:
        # Solo se propone lo que está en el archivo: columnas reconocidas, traducción de unidades
        # inequívocas y separador decimal. Moneda, base de precio y unidad por defecto no se
        # infieren nunca.
        detected = _suggested_columns(header)
        price_position = detected.get("price", {}).get("position")
        sale_position = detected.get("sale_unit", {}).get("position")
        data_rows = [(number, values) for number, values in rows if number > header_row]
        # El contenido por envase es opcional y su unidad base no viene en el archivo. Sin esa
        # unidad no se puede guardar: se omite y queda explicado en los detalles técnicos.
        pack = detected.pop("pack", None)
        if pack is not None:
            ignored_columns.append(
                {
                    "header": pack.get("header"),
                    "role": "pack",
                    "reason": (
                        "El archivo trae el contenido por envase, pero no indica en qué unidad se "
                        "mide ese contenido. Se omitió para no inventar esa unidad."
                    ),
                }
            )
        columns = detected
        units = _suggested_units(
            data_rows, sale_position - 1 if isinstance(sale_position, int) else None
        )
        options = {
            "decimal_separator": _suggested_decimal_separator(
                data_rows,
                price_position - 1 if isinstance(price_position, int) else None,
            )
        }
        profile_source, profile_version = "detected", 0
    else:
        columns, units, options, profile_source, profile_version = _profile_values(saved, None)
    clean_columns = _clean_columns(
        columns,
        default_sale_unit=options.get("default_sale_unit"),
        # En modo automático, lo que el archivo no trae se informa en `missing_columns`; no se
        # rechaza con un error técnico antes de que la pantalla pueda preguntar.
        allow_missing=auto,
    )
    clean_units = _clean_units(units)
    clean_options = _clean_options(options)
    # El mapeo que se devuelve es exactamente el que hay que guardar antes de importar: así la
    # revisión y la importación leen la misma hoja, cabecera y configuración.
    profile_payload = {
        "file_format": file_format,
        "sheet": sheet,
        "header_row": header_row,
        "columns": clean_columns,
        "units": clean_units,
        "options": clean_options,
    }
    base = {
        "file_format": file_format,
        "sheets": sheets,
        "sheet": sheet,
        "header_row": header_row,
        "headers": [_text(value) for value in header],
        "profile_source": profile_source,
        "profile_version": profile_version,
        "profile": profile_payload,
        # Columnas que se reconocieron pero se omitieron a propósito, con el motivo. Es
        # diagnóstico: la pantalla no pregunta por ellas ni inventa su unidad.
        "ignored_columns": ignored_columns,
        "file_hash": hashlib.sha256(content).hexdigest(),
    }
    missing_columns = _missing_required_roles(clean_columns, clean_options) if auto else []
    if missing_columns:
        # Se informa qué falta en vez de fallar con un mensaje técnico: la pantalla pregunta por
        # esos datos (o abre los detalles) y vuelve a revisar.
        return {
            **base,
            "rows_total": 0,
            "rows_accepted": 0,
            "rows_rejected": 0,
            "rows_needs_review": 0,
            "rows": [],
            "rows_truncated": False,
            "problems": [],
            "unrecognised_units": [],
            "unrecognised_bases": [],
            "missing_columns": missing_columns,
        }
    probe = ImportProfile(
        id=uuid.uuid4(),
        organization_id=actor.organization_id,
        source_id=source.id,
        file_format=file_format,
        sheet=sheet,
        header_row=header_row,
        columns=clean_columns,
        units=clean_units,
        options=clean_options,
        status="active",
        version=profile_version,
    )
    # Misma validación que usará la importación real; un perfil inválido falla aquí y no al final.
    _load_profile(probe)
    outcome = await run_import(
        session,
        actor,
        source_id=source.id,
        file_name=file_name or "planilla",
        content=content,
        dry_run=True,
        profile_override=probe,
        reuse_previous=False,
    )
    return {
        **base,
        "rows_total": outcome.rows_total,
        "rows_accepted": outcome.rows_accepted,
        "rows_rejected": outcome.rows_rejected,
        "rows_needs_review": sum(1 for row in outcome.rows if row["status"] == "review"),
        "rows": outcome.rows[:_CHECK_ROWS],
        "rows_truncated": len(outcome.rows) > _CHECK_ROWS,
        "problems": _aggregate_problems(outcome.rows),
        "unrecognised_units": _unrecognised_units(outcome.rows),
        "unrecognised_bases": _unrecognised_bases(outcome.rows),
        "missing_columns": [],
    }


# ---------- ofertas ya importadas ----------


async def latest_offers(
    session: AsyncSession,
    actor: Actor,
    supplier_id: uuid.UUID,
    *,
    limit: int = 200,
    trade_item_ids: Sequence[uuid.UUID] | None = None,
) -> list[dict[str, Any]]:
    """Última observación de cada SKU de cada fuente del proveedor, con precio y fecha.

    `trade_item_ids` acota la lectura a los artículos que la pantalla puede mostrar; sin él se
    devuelven las últimas observaciones del proveedor hasta `limit`. La fila sigue siendo la última
    de su SKU: filtrar por artículo no cambia qué precio es el vigente de cada fuente.
    """
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
    latest_query = select(OfferSnapshot.id.label("offer_id"), rank).where(
        OfferSnapshot.organization_id == org,
        OfferSnapshot.supplier_id == supplier_id,
    )
    if trade_item_ids:
        latest_query = latest_query.where(OfferSnapshot.trade_item_id.in_(list(trade_item_ids)))
    latest = latest_query.subquery()
    rows = (
        await session.execute(
            select(OfferSnapshot, TradeItem.display_name, Source.name, Source.mode)
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
    now = datetime.now(UTC)
    fresh_hours = get_settings().source_stale_after_hours
    out: list[dict[str, Any]] = []
    for offer, item_name, source_name, source_mode in rows:
        fresh_until = offer.observed_at + timedelta(hours=fresh_hours)
        is_current = not (
            (offer.valid_from is not None and offer.valid_from > now)
            or (offer.valid_to is not None and offer.valid_to < now)
        )
        out.append(
            {
                "offer_id": offer.id,
                "source_id": offer.source_id,
                "source_name": source_name,
                "source_mode": source_mode,
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
                "stock_unit": offer.stock_unit,
                "review_status": offer.review_status,
                "notes": offer.notes,
                "valid_from": offer.valid_from,
                "valid_to": offer.valid_to,
                "is_current": is_current,
                "fresh_until": fresh_until,
                "is_stale": now > fresh_until,
                "observed_at": offer.observed_at,
                "imported_at": offer.created_at,
            }
        )
    return out
