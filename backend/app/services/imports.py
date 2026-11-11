"""Conector `manual_import`: planillas `.xlsx` de proveedores → ofertas (E8, D-103 a D-106).

Qué hace: lee la planilla ORIGINAL de un proveedor, la interpreta con el **perfil** de su fuente
(D-102), valida en dos fases y escribe ofertas fechadas. Nada más.

Qué NO hace, por diseño:

* **No toca el catálogo propio.** Nunca crea `trade_item`, `trade_item_alias` ni
  `supplier_product_mapping` (D-106): una fila cuyo SKU no está mapeado se rechaza con
  `UNMAPPED_PRODUCT`, y jamás se busca el producto por nombre (D-104).
* **No mezcla planos.** Solo acepta fuentes `kind='supplier'`; los precios públicos de cadena viven
  en `chain_product`/`reference_price` y se cargan por otra vía. Una fuente de cadena se rechaza
  antes de leer una sola fila.
* **No sobrescribe precios.** `offer_snapshot` es de solo agregar: cada importación añade
  observaciones nuevas y las anteriores conservan su fecha y su significado.
* **No inventa dinero ni cantidades.** Moneda y base de precio se declaran (D-105); un dato que la
  planilla no trae queda `NULL` (= desconocido), nunca `0` ni `1` (D-081).
* **No llama a la IA.** Todo es determinista.

Validación en dos fases (D-103):

1. **Estructural** — archivo legible, cabecera, todas las columnas del perfil resueltas y límites
   (10 MB, 20.000 filas). Si falla, se lanza `DomainError` y **no se escribe nada**, ni siquiera el
   registro de la importación.
2. **Por fila** — se clasifican todas las filas y solo después se escriben las aceptadas. Cada fila
   rechazada queda en `import_run.rejected_rows` con **lo mínimo para localizarla** (número de fila,
   código, fuente, hash, SKU, nombre y las celdas que causaron el rechazo; nunca la fila entera,
   D-104) y su motivo en `import_run.errors` con el código cerrado de D-090 y el número de fila de la
   planilla. Una fila aceptada con una condición comercial en texto se guarda como oferta
   `needs_review` (D-110): existe, pero no es una oferta verificada.

Idempotencia: mismo `(organización, fuente, hash del archivo)` devuelve el resultado original sin
escribir nada (el índice único de `import_run` lo garantiza también en la base).

El servicio no hace commit: la transacción la abre quien llama.
"""

import csv
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import xlrd
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.core.permissions import Role
from app.models import (
    ImportProfile,
    ImportRequest,
    ImportRun,
    OfferSnapshot,
    Organization,
    Source,
    SupplierProductMapping,
    TradeItem,
)
from app.services.actors import Actor
from app.services.events import advisory_lock, emit
from app.services.provenance import origin_for

MAX_BYTES = 10 * 1024 * 1024  # 10 MB (contrato E8)
MAX_ROWS = 20_000  # filas de datos no vacías (contrato E8)

# Vocabulario cerrado de unidades de venta (D-102). Un texto fuera de aquí es `UNKNOWN_UNIT`.
UNITS = ("box", "package", "unit", "bottle")
PRICE_BASES = ("per_commercial_unit", "per_base_unit")

# Papeles que el perfil puede mapear. Los tres primeros son obligatorios para poder crear una oferta.
REQUIRED_ROLES = ("sku", "price", "sale_unit")
OPTIONAL_ROLES = (
    "name",
    "currency",
    "basis",
    "pack",
    "minimum",
    "multiple",
    "stock",
    "observed_at",
    "conditions",
)
ROLES = REQUIRED_ROLES + OPTIONAL_ROLES

# Códigos de fila cerrados en D-090. No se agregan códigos nuevos.
MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
INVALID_PRICE = "INVALID_PRICE"
INVALID_CURRENCY = "INVALID_CURRENCY"
INVALID_BASIS = "INVALID_BASIS"
INVALID_QUANTITY = "INVALID_QUANTITY"
UNKNOWN_UNIT = "UNKNOWN_UNIT"
DUPLICATE_ROW = "DUPLICATE_ROW"
UNMAPPED_PRODUCT = "UNMAPPED_PRODUCT"
UNMODELLED_CONDITION = "UNMODELLED_CONDITION"
ROW_CODES = (
    MISSING_REQUIRED_FIELD,
    INVALID_PRICE,
    INVALID_CURRENCY,
    INVALID_BASIS,
    INVALID_QUANTITY,
    UNKNOWN_UNIT,
    DUPLICATE_ROW,
    UNMAPPED_PRODUCT,
    UNMODELLED_CONDITION,
)

# Estado de revisión de la oferta (D-110). `accepted` es la única oferta verificada.
REVIEW_ACCEPTED = "accepted"
REVIEW_NEEDS_REVIEW = "needs_review"

DATE_FORMATS = {
    "YYYY-MM-DD": "%Y-%m-%d",
    "DD-MM-YYYY": "%d-%m-%Y",
    "DD/MM/YYYY": "%d/%m/%Y",
}
TIME_SUFFIXES = {"HH:MM": "%H:%M", "HH:MM:SS": "%H:%M:%S"}


@dataclass
class ImportOutcome:
    """Resultado de una importación. `as_response()` conserva exactamente la forma de C0 (E8).

    `rows` solo se llena en `dry_run` y solo lo consume la revisión previa: no forma parte del
    sobre congelado y `as_response()` nunca lo expone.
    """

    status: str
    rows_total: int
    rows_accepted: int
    rows_rejected: int
    import_id: uuid.UUID | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    observed_at: datetime | None = None
    repeated: bool = False
    dry_run: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)

    def as_response(self) -> dict[str, Any]:
        return {
            "import_id": str(self.import_id) if self.import_id else None,
            "status": self.status,
            "rows_total": self.rows_total,
            "rows_accepted": self.rows_accepted,
            "rows_rejected": self.rows_rejected,
            "errors": list(self.errors),
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


@dataclass
class AcceptedRow:
    row: int
    external_id: str
    external_name: str | None
    trade_item_id: uuid.UUID
    price: Decimal
    currency: str
    price_basis: str
    sale_unit: str
    sale_unit_text: str
    pack_units: Decimal | None
    minimum: Decimal | None
    multiple: Decimal | None
    stock: Decimal | None
    observed_at: datetime
    notes: str | None
    needs_item: bool = False


class RowProblem(Exception):
    """Una fila no se puede aceptar. `code` pertenece al vocabulario cerrado de D-090.

    `fields` son los papeles de columna cuyos valores causaron el rechazo: son los únicos que se
    conservan de una fila rechazada (D-104, D-110), y se declaran en el punto exacto donde se
    detecta el problema — nunca se adivinan después.
    """

    def __init__(self, code: str, message: str, fields: tuple[str, ...] = ()):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields


# ---------- lectura del archivo ----------


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value: Any) -> str | None:
    """Texto de una celda tal como viene, sin inventar: números y fechas se pasan a texto legible."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v or None
    if isinstance(value, bool):
        return "SI" if value else "NO"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    return str(value).strip() or None


def _normalize_header(value: Any) -> str:
    """Clave de comparación de una cabecera: sin mayúsculas, sin signos y con espacios únicos.

    No traduce ni interpreta: solo unifica las formas habituales de escribir el mismo nombre
    («Código», «CODIGO», «Código:», «código  proveedor»).
    """
    text = _text(value)
    if not text:
        return ""
    text = text.casefold()
    # El guion bajo también separa palabras: las cabeceras técnicas («sale_unit») deben
    # reconocerse igual que sus formas habituales («sale unit»).
    text = re.sub(r"[^\wáéíóúüñ]+|_", " ", text)
    return " ".join(text.split())


def _header_variants(value: Any) -> list[str]:
    """Claves candidatas de una cabecera, de la más literal a la más permisiva.

    La segunda variante descarta el texto entre paréntesis («Precio (CLP)» → «precio»), que en las
    planillas suele aclarar la unidad o la moneda sin cambiar el papel de la columna.
    """
    text = _text(value)
    if not text:
        return []
    key = _normalize_header(text)
    base = _normalize_header(re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text))
    return [key] if not base or base == key else [key, base]


# Equivalencias de base de precio que no dejan ambigüedad. «unit»/«unidad» a secas **no** están
# aquí: pueden significar la unidad de venta o la unidad base. Esas se confirman por proveedor en
# `options.basis_values` y nunca se generalizan a otros.
_BASIS_ALIASES: dict[str, str] = {
    "per commercial unit": "per_commercial_unit",
    "commercial unit": "per_commercial_unit",
    "unidad comercial": "per_commercial_unit",
    "por unidad comercial": "per_commercial_unit",
    "precio por unidad comercial": "per_commercial_unit",
    "per base unit": "per_base_unit",
    "base unit": "per_base_unit",
    "unidad base": "per_base_unit",
    "por unidad base": "per_base_unit",
    "precio por unidad base": "per_base_unit",
}


def _basis_from_text(value: Any) -> str | None:
    """Base de precio publicada, solo cuando su significado es inequívoco."""
    return _BASIS_ALIASES.get(_normalize_header(value))


FILE_FORMATS = ("xlsx", "xls", "csv")
_FORMAT_LABEL = {
    "xlsx": "planilla .xlsx",
    "xls": "planilla .xls",
    "csv": "archivo .csv",
}


def detect_file_format(file_name: str | None, content: bytes) -> str:
    """Formato por extensión y, si no hay o no coincide, por la firma del contenido.

    Es solo una ayuda para la vista previa: la importación usa el formato del perfil guardado.
    """
    name = (file_name or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        return "xlsx"
    if name.endswith(".xls"):
        return "xls"
    if name.endswith(".csv"):
        return "csv"
    if content.startswith(b"PK\x03\x04"):
        return "xlsx"
    if content.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"
    if content:
        return "csv"
    raise DomainError("IMPORT_FORMAT_INVALID", "El archivo está vacío.", status_code=422)


def _collect(
    header_row: int, source_rows: list[tuple[int, tuple[Any, ...]]]
) -> tuple[tuple[Any, ...], list[tuple[int, tuple[Any, ...]]]]:
    """Aplica la cabecera y los límites comunes a cualquier formato ya leído."""
    header: tuple[Any, ...] | None = None
    rows: list[tuple[int, tuple[Any, ...]]] = []
    for index, values in source_rows:
        if index < header_row:
            continue
        if index == header_row:
            header = tuple(values)
            continue
        if all(_blank(v) for v in values):
            continue
        rows.append((index, tuple(values)))
        if len(rows) > MAX_ROWS:
            raise DomainError(
                "PAYLOAD_TOO_LARGE",
                f"La planilla supera las {MAX_ROWS} filas de datos permitidas.",
                status_code=413,
            )
    if header is None or all(_blank(h) for h in header):
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"La planilla no tiene una cabecera legible en la fila {header_row}.",
            status_code=422,
        )
    return header, rows


def _xlsx_rows(content: bytes, sheet_name: str | None) -> list[tuple[int, tuple[Any, ...]]]:
    try:
        wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 — cualquier fallo del formato es el mismo error de archivo
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            "El archivo no se puede abrir como planilla .xlsx.",
            status_code=422,
        ) from e
    try:
        if sheet_name is not None:
            if sheet_name not in wb.sheetnames:
                raise DomainError(
                    "IMPORT_FORMAT_INVALID",
                    f"La planilla no tiene la hoja «{sheet_name}» que declara el perfil.",
                    status_code=422,
                )
            ws = wb[sheet_name]
        else:
            ws = wb.active
        if ws is None:
            raise DomainError("IMPORT_FORMAT_INVALID", "La planilla no tiene hojas.", status_code=422)
        return [
            (index, tuple(values))
            for index, values in enumerate(ws.iter_rows(values_only=True), start=1)
        ]
    finally:
        wb.close()


def _xls_value(cell, datemode: int) -> Any:
    """Valor de una celda .xls sin inventar: texto, número o fecha real, no su serial."""
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(cell.value, datemode)
        except (ValueError, xlrd.XLDateError):
            return cell.value
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        # xlrd entrega todo como float; un SKU numérico 123 debe quedar «123», no «123.0».
        return int(cell.value) if float(cell.value).is_integer() else cell.value
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    return cell.value


def _xls_rows(content: bytes, sheet_name: str | None) -> list[tuple[int, tuple[Any, ...]]]:
    try:
        wb = xlrd.open_workbook(file_contents=content, on_demand=True)
    except Exception as e:  # noqa: BLE001
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            "El archivo no se puede abrir como planilla .xls.",
            status_code=422,
        ) from e
    try:
        if sheet_name is not None:
            if sheet_name not in wb.sheet_names():
                raise DomainError(
                    "IMPORT_FORMAT_INVALID",
                    f"La planilla no tiene la hoja «{sheet_name}» que declara el perfil.",
                    status_code=422,
                )
            ws = wb.sheet_by_name(sheet_name)
        else:
            if wb.nsheets == 0:
                raise DomainError(
                    "IMPORT_FORMAT_INVALID", "La planilla no tiene hojas.", status_code=422
                )
            ws = wb.sheet_by_index(0)
        return [
            (index, tuple(_xls_value(ws.cell(index - 1, col), wb.datemode) for col in range(ws.ncols)))
            for index in range(1, ws.nrows + 1)
        ]
    finally:
        wb.release_resources()


def _decode_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DomainError(
        "IMPORT_FORMAT_INVALID", "El archivo .csv no se puede decodificar como texto.", status_code=422
    )


def _csv_delimiter(text: str) -> str:
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
    except csv.Error:
        first = next((line for line in sample.splitlines() if line.strip()), "")
        counts = {d: first.count(d) for d in (",", ";", "\t")}
        best = max(counts, key=counts.get)
        return best if counts[best] > 0 else ","


def _csv_rows(content: bytes) -> list[tuple[int, tuple[Any, ...]]]:
    text = _decode_csv(content)
    reader = csv.reader(StringIO(text), delimiter=_csv_delimiter(text))
    try:
        return [(index, tuple(values)) for index, values in enumerate(reader, start=1)]
    except csv.Error as e:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            "El archivo no se puede leer como .csv: revisa que el formato del perfil corresponda "
            "al archivo.",
            status_code=422,
        ) from e


def read_raw_rows(
    content: bytes, file_format: str, sheet_name: str | None = None
) -> list[tuple[int, tuple[Any, ...]]]:
    """Todas las filas del archivo, sin decidir todavía dónde está la cabecera (vista previa)."""
    if not content:
        raise DomainError("IMPORT_FORMAT_INVALID", "El archivo está vacío.", status_code=422)
    if file_format == "xlsx":
        return _xlsx_rows(content, sheet_name)
    if file_format == "xls":
        return _xls_rows(content, sheet_name)
    if file_format == "csv":
        if sheet_name is not None:
            raise DomainError(
                "IMPORT_FORMAT_INVALID",
                "Un archivo .csv no tiene hojas: quita la hoja del perfil.",
                status_code=422,
            )
        return _csv_rows(content)
    raise DomainError(
        "IMPORT_FORMAT_INVALID",
        f"El formato «{file_format}» no está soportado; usa .xlsx, .xls o .csv.",
        status_code=422,
    )


def _read_sheet(
    content: bytes, sheet_name: str | None, header_row: int, file_format: str = "xlsx"
) -> tuple[tuple[Any, ...], list[tuple[int, tuple[Any, ...]]]]:
    """Fase 1 estructural: abre el archivo y devuelve la cabecera y las filas de datos no vacías."""
    if not content:
        raise DomainError("IMPORT_FORMAT_INVALID", "El archivo está vacío.", status_code=422)
    if file_format not in FILE_FORMATS:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"El formato «{file_format}» no está soportado; usa .xlsx, .xls o .csv.",
            status_code=422,
        )
    return _collect(header_row, read_raw_rows(content, file_format, sheet_name))


def _resolve_columns(
    columns: dict, header: tuple[Any, ...], *, required_roles: tuple[str, ...] = REQUIRED_ROLES
) -> dict[str, int]:
    """Cada papel del perfil se busca por nombre de cabecera (con sus alias) y, si no, por posición.

    La posición solo se usa cuando el mapeo **no declara nombre**: si declara `header` y ese nombre
    no está en el archivo, se falla en vez de aplicar la configuración a una columna equivocada
    porque el proveedor cambió el orden.
    """
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
    normalized = [_normalize_header(h) for h in header]
    positions: dict[str, int] = {}
    for role, spec in columns.items():
        if not isinstance(spec, dict):
            raise DomainError(
                "IMPORT_FORMAT_INVALID",
                f"La columna de «{role}» debe ser un objeto con «header» o «position».",
                status_code=422,
            )
        candidates = [spec.get("header"), *(spec.get("aliases") or [])]
        declared_name = bool(spec.get("header"))
        found = None
        for name in candidates:
            for key in _header_variants(name):
                if key in normalized:
                    found = normalized.index(key)
                    break
            if found is not None:
                break
        if found is None and not declared_name and isinstance(spec.get("position"), int):
            pos = spec["position"] - 1
            found = pos if 0 <= pos < len(normalized) else None
        if found is None and declared_name:
            raise DomainError(
                "IMPORT_FORMAT_INVALID",
                f"El archivo no tiene la columna «{spec['header']}» que el mapeo guardado espera "
                f"para «{role}». Revisa las columnas: la planilla cambió.",
                status_code=422,
            )
        if found is None:
            raise DomainError(
                "IMPORT_FORMAT_INVALID",
                f"No se encontró en la cabecera la columna de «{role}» "
                f"(buscada como {', '.join(str(c) for c in candidates if c) or 'sin nombre'}).",
                status_code=422,
            )
        positions[role] = found
    missing = [r for r in required_roles if r not in positions]
    if missing:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"El perfil no mapea columnas obligatorias: {', '.join(missing)}.",
            status_code=422,
        )
    return positions


def columns_compatible(
    columns: Any,
    header: tuple[Any, ...],
    *,
    required_roles: tuple[str, ...] = REQUIRED_ROLES,
) -> bool:
    """¿El mapeo guardado calza con esta cabecera **por nombre**?

    En el flujo automático solo se reutiliza un perfil si **todas** sus columnas declaran nombre y
    ese nombre existe en el archivo. Un perfil solo-posición no se da por compatible: aplicar
    posiciones viejas a un archivo nuevo puede leer la columna equivocada.
    """
    if not isinstance(columns, dict) or not columns:
        return False
    if any(not (isinstance(spec, dict) and spec.get("header")) for spec in columns.values()):
        return False
    try:
        _resolve_columns(columns, header, required_roles=required_roles)
    except DomainError:
        return False
    return True


def _parse_date_format(raw: Any) -> str:
    text = str(raw or "").strip()
    for sep in (" ", "T"):
        base, _, rest = text.partition(sep)
        if base in DATE_FORMATS and rest in TIME_SUFFIXES:
            return DATE_FORMATS[base] + " " + TIME_SUFFIXES[rest]
    if text in DATE_FORMATS:
        return DATE_FORMATS[text]
    raise DomainError(
        "IMPORT_FORMAT_INVALID",
        f'El perfil declara un formato de fecha no soportado: «{text}». '
        f"Usa {', '.join(DATE_FORMATS)} (opcionalmente con \" HH:MM\").",
        status_code=422,
    )


@dataclass
class _Profile:
    id: uuid.UUID
    version: int
    column_specs: dict[str, Any]
    units: dict[str, str]
    currency: str | None
    price_basis: str | None
    basis_values: dict[str, str]
    decimal_sep: str
    thousands_sep: str | None
    date_format: str | None
    tax_included: bool | None
    discount_included: bool | None
    pack_base_unit: str | None
    quantity_unit: str | None
    default_sale_unit: str | None
    create_missing_items: bool
    snapshot: dict[str, Any]
    columns: dict[str, int] = field(default_factory=dict)  # se llena al resolver la cabecera


def _load_profile(profile: ImportProfile) -> _Profile:
    """Resuelve las opciones del perfil UNA vez por importación (nunca por fila)."""
    options = profile.options if isinstance(profile.options, dict) else {}
    units = profile.units if isinstance(profile.units, dict) else {}
    bad_units = sorted({str(v) for v in units.values()} - set(UNITS))
    if bad_units:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"El perfil traduce unidades a valores no permitidos: {', '.join(bad_units)}. "
            f"Válidas: {', '.join(UNITS)}.",
            status_code=422,
        )
    currency = options.get("currency")
    if currency is not None:
        currency = str(currency).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise DomainError(
                "IMPORT_FORMAT_INVALID",
                f"La moneda declarada en el perfil no es un código de 3 letras: «{currency}».",
                status_code=422,
            )
    basis = options.get("price_basis")
    if basis is not None and basis not in PRICE_BASES:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"La base de precio del perfil debe ser {' o '.join(PRICE_BASES)}.",
            status_code=422,
        )
    raw_basis_values = options.get("basis_values", {})
    if not isinstance(raw_basis_values, dict):
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            "«basis_values» del perfil debe ser un objeto texto → base de precio.",
            status_code=422,
        )
    bad_basis_values = sorted(
        {
            str(value)
            for value in raw_basis_values.values()
            if str(value) not in PRICE_BASES
        }
    )
    if bad_basis_values:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            "El perfil traduce bases de precio a valores no permitidos: "
            f"{', '.join(bad_basis_values)}. Válidas: {', '.join(PRICE_BASES)}.",
            status_code=422,
        )
    basis_values = {
        _normalize_header(key): str(value) for key, value in raw_basis_values.items()
    }
    decimal_sep = options.get("decimal_separator", ".")
    thousands_sep = options.get("thousands_separator")
    if decimal_sep not in (".", ",", None):
        raise DomainError(
            "IMPORT_FORMAT_INVALID", "El separador decimal del perfil debe ser «.» o «,».", status_code=422
        )
    if thousands_sep is not None and thousands_sep not in (".", ",", " "):
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            "El separador de miles del perfil debe ser «.», «,», un espacio o null.",
            status_code=422,
        )
    if thousands_sep is not None and thousands_sep == decimal_sep:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            "El separador de miles y el decimal del perfil no pueden ser el mismo carácter.",
            status_code=422,
        )
    pack_base_unit = options.get("pack_base_unit")
    if pack_base_unit is not None and pack_base_unit not in UNITS:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"La unidad base del contenido debe ser una de: {', '.join(UNITS)}.",
            status_code=422,
        )
    quantity_unit = options.get("quantity_unit")
    if quantity_unit is not None and quantity_unit not in UNITS:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"La unidad de mínimo/múltiplo/stock debe ser una de: {', '.join(UNITS)}.",
            status_code=422,
        )
    default_sale_unit = options.get("default_sale_unit")
    if default_sale_unit is not None and default_sale_unit not in UNITS:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            f"La unidad de venta por defecto debe ser una de: {', '.join(UNITS)}.",
            status_code=422,
        )
    create_missing_items = options.get("create_missing_items", False)
    if not isinstance(create_missing_items, bool):
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            "«create_missing_items» del perfil debe ser true o false.",
            status_code=422,
        )
    tax_included = options.get("tax_included")
    discount_included = options.get("discount_included")
    for name, value in (("tax_included", tax_included), ("discount_included", discount_included)):
        if value is not None and not isinstance(value, bool):
            raise DomainError(
                "IMPORT_FORMAT_INVALID",
                f"«{name}» del perfil debe ser true, false o null (desconocido).",
                status_code=422,
            )
    return _Profile(
        id=profile.id,
        version=profile.version,
        column_specs=profile.columns if isinstance(profile.columns, dict) else {},
        units={str(k).strip().casefold(): str(v) for k, v in units.items()},
        currency=currency,
        price_basis=basis,
        basis_values=basis_values,
        decimal_sep=decimal_sep or ".",
        thousands_sep=thousands_sep,
        date_format=_parse_date_format(options["date_format"]) if options.get("date_format") else None,
        tax_included=tax_included,
        discount_included=discount_included,
        pack_base_unit=pack_base_unit,
        quantity_unit=quantity_unit,
        default_sale_unit=default_sale_unit,
        create_missing_items=create_missing_items,
        snapshot={
            "id": str(profile.id),
            "version": profile.version,
            "file_format": profile.file_format,
            "sheet": profile.sheet,
            "header_row": profile.header_row,
            "columns": profile.columns,
            "units": profile.units,
            "options": profile.options,
        },
    )


# ---------- validación por fila ----------


def _decimal(raw: Any, prof: _Profile, *, role: str, code: str, message: str) -> Decimal:
    text = _text(raw)
    if text is None:
        raise RowProblem(code, message, fields=(role,))
    cleaned = text.replace(" ", "")
    if prof.thousands_sep:
        cleaned = cleaned.replace(prof.thousands_sep, "")
    if prof.decimal_sep != ".":
        cleaned = cleaned.replace(prof.decimal_sep, ".")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        raise RowProblem(code, message, fields=(role,))
    try:
        return Decimal(cleaned)
    except InvalidOperation:  # pragma: no cover — la expresión regular ya lo impide
        raise RowProblem(code, message, fields=(role,)) from None


def _quantity(
    raw: Any, prof: _Profile, *, role: str, minimum: Decimal, message: str
) -> Decimal | None:
    """Cantidad de una columna opcional: vacía o desconocida es `NULL`, nunca 0 ni 1."""
    if _blank(raw):
        return None
    value = _decimal(raw, prof, role=role, code=INVALID_QUANTITY, message=message)
    if value != value.to_integral_value():
        raise RowProblem(INVALID_QUANTITY, message + " Debe ser un número entero.", fields=(role,))
    if value < minimum:
        raise RowProblem(
            INVALID_QUANTITY, message + f" Debe ser mayor o igual a {minimum}.", fields=(role,)
        )
    return value


def _observed_at(raw: Any, prof: _Profile, tz: ZoneInfo, fallback: datetime) -> datetime:
    if prof.date_format is None:
        return fallback  # la planilla no declara fecha: se usa la del servidor (contrato E8)
    if _blank(raw):
        return fallback
    if isinstance(raw, datetime):
        value = raw
    else:
        try:
            value = datetime.strptime(str(_text(raw)), prof.date_format)
        except ValueError:
            raise RowProblem(
                MISSING_REQUIRED_FIELD,
                "La fecha de observación no se pudo leer con el formato declarado en el perfil.",
                fields=("observed_at",),
            ) from None
    return (value.replace(tzinfo=tz) if value.tzinfo is None else value).astimezone(UTC)


def _classify_row(
    row_number: int,
    values: tuple[Any, ...],
    prof: _Profile,
    items: dict[str, uuid.UUID],
    tz: ZoneInfo,
    now: datetime,
) -> tuple[AcceptedRow | None, str | None]:
    """Devuelve (fila aceptada, aviso no bloqueante) o lanza `RowProblem`."""

    def cell(role: str) -> Any:
        idx = prof.columns.get(role)
        return values[idx] if idx is not None and idx < len(values) else None

    external_id = _text(cell("sku"))
    if external_id is None:
        raise RowProblem(
            MISSING_REQUIRED_FIELD, "La fila no trae código de proveedor (SKU).", fields=("sku",)
        )
    if len(external_id) > 120:
        raise RowProblem(
            MISSING_REQUIRED_FIELD,
            "El código de proveedor supera los 120 caracteres.",
            fields=("sku",),
        )
    name_text = _text(cell("name")) if "name" in prof.columns else None
    item_id = items.get(external_id)
    needs_item = False
    if item_id is None:
        # Sin vínculo explícito previo no hay artículo: nunca se adivina por nombre (D-104).
        # La única excepción es una opción explícita del perfil: crear el artículo con el nombre
        # que la propia planilla publica. Sin nombre no se crea nada.
        if prof.create_missing_items and name_text:
            item_id = uuid.uuid4()  # marcador; el artículo se crea al escribir, no al validar
            needs_item = True
        else:
            message = (
                f"El código {external_id} no tiene un vínculo con el catálogo y la fila no trae "
                "nombre: no se puede crear un artículo sin un nombre publicado."
                if prof.create_missing_items
                else (
                    f"El código {external_id} no tiene un vínculo con el catálogo; "
                    "regístralo antes de importar."
                )
            )
            raise RowProblem(UNMAPPED_PRODUCT, message, fields=("sku",))

    price = _decimal(
        cell("price"),
        prof,
        role="price",
        code=INVALID_PRICE,
        message="La fila no trae un precio decimal válido.",
    )
    if price < 0:
        raise RowProblem(INVALID_PRICE, "El precio no puede ser negativo.", fields=("price",))

    currency = _text(cell("currency")) if "currency" in prof.columns else None
    currency = (currency or prof.currency or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise RowProblem(
            INVALID_CURRENCY,
            "La fila no declara una moneda válida (código de 3 letras) y el perfil tampoco.",
            fields=("currency",),
        )

    basis_text = _text(cell("basis")) if "basis" in prof.columns else None
    if basis_text is None:
        basis = prof.price_basis or ""
    else:
        # Primero la equivalencia confirmada para ESTE proveedor; después solo los valores
        # inequívocos. Un «unit» genérico sin confirmación sigue siendo inválido.
        basis = (
            prof.basis_values.get(_normalize_header(basis_text))
            or _basis_from_text(basis_text)
            or basis_text.strip().lower()
        )
    if basis not in PRICE_BASES:
        message = (
            f"La lista indica «{basis_text}» como base del precio, pero ese valor no está "
            "confirmado para este proveedor."
            if basis_text is not None
            else (
                f"La base de precio debe ser {PRICE_BASES[0]} o {PRICE_BASES[1]}, y ni la fila "
                "ni el perfil la declaran."
            )
        )
        raise RowProblem(INVALID_BASIS, message, fields=("basis",))

    unit_text = _text(cell("sale_unit"))
    if unit_text is None:
        # El perfil puede declarar la unidad por defecto cuando la planilla no trae la columna.
        sale_unit = prof.default_sale_unit
        if sale_unit is None:
            raise RowProblem(
                MISSING_REQUIRED_FIELD,
                "La fila no trae la unidad de venta y el perfil no declara una por defecto.",
                fields=("sale_unit",),
            )
    else:
        sale_unit = prof.units.get(unit_text.casefold())
        if sale_unit is None:
            raise RowProblem(
                UNKNOWN_UNIT,
                f"La unidad «{unit_text}» no está en el vocabulario del perfil, que traduce a: "
                f"{', '.join(UNITS)}.",
                fields=("sale_unit",),
            )

    pack_units = None
    if "pack" in prof.columns:
        pack_units = _quantity(
            cell("pack"),
            prof,
            role="pack",
            minimum=Decimal(1),
            message="El contenido por unidad no es válido.",
        )

    minimum = _quantity(
        cell("minimum") if "minimum" in prof.columns else None,
        prof,
        role="minimum",
        minimum=Decimal(0),
        message="El mínimo de compra no es válido.",
    )
    multiple = _quantity(
        cell("multiple") if "multiple" in prof.columns else None,
        prof,
        role="multiple",
        minimum=Decimal(1),
        message="El múltiplo de compra no es válido.",
    )
    stock = _quantity(
        cell("stock") if "stock" in prof.columns else None,
        prof,
        role="stock",
        minimum=Decimal(0),
        message="El stock no es válido.",
    )

    conditions = _text(cell("conditions")) if "conditions" in prof.columns else None
    notes = conditions[:2000] if conditions else None
    warning = (
        UNMODELLED_CONDITION
        if conditions
        else None  # se acepta la oferta marcada `needs_review`, nunca como verificada (D-110)
    )
    return (
        AcceptedRow(
            row=row_number,
            external_id=external_id,
            external_name=name_text[:300] if name_text else None,
            trade_item_id=item_id,
            price=price,
            currency=currency,
            price_basis=basis,
            sale_unit=sale_unit,
            sale_unit_text=unit_text[:40] if unit_text else None,
            pack_units=pack_units,
            minimum=minimum,
            multiple=multiple,
            stock=stock,
            observed_at=_observed_at(
                cell("observed_at") if "observed_at" in prof.columns else None, prof, tz, now
            ),
            notes=notes,
            needs_item=needs_item,
        ),
        warning,
    )


# ---------- orquestación ----------


async def _organization(session: AsyncSession, org_id: uuid.UUID) -> tuple[str, ZoneInfo]:
    """Tipo de organización (para la procedencia) y su zona horaria (para fechas ingenuas del archivo)."""
    kind, tz_name = (
        await session.execute(
            select(Organization.kind, Organization.timezone).where(Organization.id == org_id)
        )
    ).one()
    try:
        return kind, ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return kind, ZoneInfo("UTC")


async def _mappings(
    session: AsyncSession, org_id: uuid.UUID, source_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    rows = await session.execute(
        select(SupplierProductMapping.external_id, SupplierProductMapping.trade_item_id).where(
            SupplierProductMapping.organization_id == org_id,
            SupplierProductMapping.source_id == source_id,
        )
    )
    return {external: item for external, item in rows.all()}


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _rejected_row(
    row_number: int, values: tuple[Any, ...], prof: _Profile, source_id, file_hash: str,
    problem: RowProblem,
) -> dict[str, Any]:
    """Fila rechazada con lo mínimo para localizarla y entenderla (D-104, D-110).

    Número de fila, código, fuente y hash bastan para encontrarla en la planilla original; no se
    copia la fila entera. Del contenido solo se conservan, tal como vienen: el SKU y el nombre
    (identifican el producto) y las celdas que causaron el rechazo, declaradas por el propio
    `RowProblem`. Nada se reconstruye ni se normaliza.
    """

    def cell(role: str) -> Any:
        idx = prof.columns.get(role)
        return values[idx] if idx is not None and idx < len(values) else None

    return {
        "row": row_number,
        "codes": [problem.code],
        "source_id": str(source_id),
        "file_hash": file_hash,
        "sku": _text(cell("sku")),
        "name": _text(cell("name")),
        "fields": {role: _json_value(cell(role)) for role in problem.fields},
    }


def _check_accepted_row(row: AcceptedRow, warning: str | None) -> dict[str, Any]:
    """Fila aceptada tal como la interpreta el perfil, para la revisión previa (no se escribe).

    Solo se expone lo que el motor ya calculó: el precio es el decimal validado, no una
    reconstrucción del texto original.
    """
    return {
        "row": row.row,
        "status": "review" if warning is not None else "ready",
        "sku": row.external_id,
        "name": row.external_name,
        "price": str(row.price),
        "currency": row.currency,
        "price_basis": row.price_basis,
        "sale_unit": row.sale_unit,
        "sale_unit_text": row.sale_unit_text,
        "pack_units": str(row.pack_units) if row.pack_units is not None else None,
        "stock": str(row.stock) if row.stock is not None else None,
        "observed_at": row.observed_at.isoformat(),
        "warnings": [warning] if warning is not None else [],
        "problem": None,
    }


def _check_rejected_row(
    rejected: dict[str, Any], problem: RowProblem
) -> dict[str, Any]:
    """Fila rechazada para la revisión previa: motivo, campos culpables y nada más.

    Reutiliza lo que ya guarda `_rejected_row` (SKU, nombre y las celdas del propio `RowProblem`);
    nunca expone la fila completa.
    """
    return {
        "row": rejected["row"],
        "status": "rejected",
        "sku": rejected.get("sku"),
        "name": rejected.get("name"),
        "price": None,
        "currency": None,
        "price_basis": None,
        "sale_unit": None,
        "sale_unit_text": None,
        "pack_units": None,
        "stock": None,
        "observed_at": None,
        "warnings": [],
        "problem": {
            "code": problem.code,
            "message": problem.message,
            "fields": list(problem.fields),
            "values": rejected.get("fields", {}),
        },
    }


async def _create_item_for_row(
    session: AsyncSession,
    actor: Actor,
    org: uuid.UUID,
    source: Source,
    origin: str,
    row: AcceptedRow,
) -> uuid.UUID:
    """Crea artículo y vínculo para un SKU nuevo, SOLO si el perfil lo autoriza y hay nombre.

    Es una excepción explícita a D-106 (que prohíbe crear artículos al importar) y se registra
    como tal: la clave de identidad es técnica (`import:<fuente>:<SKU>`) y nunca se deriva del
    nombre; el nombre guardado es el que publica el proveedor, sin normalizar ni completar.
    La misma fuente y el mismo SKU reutilizan el artículo y el vínculo, así que reimportar no
    duplica filas.
    """
    identity_key = f"import:{source.id}:{row.external_id}"
    existing = (
        await session.execute(
            select(TradeItem).where(
                TradeItem.organization_id == org, TradeItem.identity_key == identity_key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.id
    item = TradeItem(
        organization_id=org,
        identity_key=identity_key,
        display_name=row.external_name,
        attributes={"schema_version": 2, "origin": "supplier_price_list"},
        data_origin=origin,
        created_by=actor.id,
    )
    session.add(item)
    await session.flush()
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="trade_item.created",
        entity_type="trade_item",
        entity_id=item.id,
        event_type="catalog.trade_item_created",
        aggregate_version=1,
        payload={"identity_key": identity_key, "origin": "supplier_price_list"},
        after={"identity_key": identity_key, "display_name": item.display_name},
    )
    mapping = SupplierProductMapping(
        organization_id=org,
        source_id=source.id,
        supplier_id=source.supplier_id,
        trade_item_id=item.id,
        external_id=row.external_id,
        external_name=row.external_name,
        origin="explicit",
        data_origin=origin,
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
            "external_id": row.external_id,
            "trade_item_id": str(item.id),
            "origin": "supplier_price_list",
        },
        after={"trade_item_id": str(item.id), "external_name": row.external_name},
    )
    return item.id


async def run_import(
    session: AsyncSession,
    actor: Actor,
    *,
    source_id: uuid.UUID,
    file_name: str,
    content: bytes,
    dry_run: bool = False,
    profile_override: ImportProfile | None = None,
    reuse_previous: bool = True,
) -> ImportOutcome:
    """Procesa una planilla de una fuente de proveedor. `dry_run` valida y cuenta sin escribir nada.

    `profile_override` permite revisar una interpretación que todavía no se ha guardado, sin
    escribirla. `reuse_previous=False` obliga a clasificar de nuevo aunque el hash ya exista: la
    revisión previa siempre describe el mapeo que la persona está viendo ahora.
    """
    if Role.ADMIN.value not in (actor.roles or []):
        raise DomainError(
            "FORBIDDEN", "Solo la administración puede importar listas de precios.", status_code=403
        )
    org = actor.organization_id
    source = (
        await session.execute(
            select(Source).where(Source.organization_id == org, Source.id == source_id)
        )
    ).scalar_one_or_none()
    if source is None:
        raise not_found("La fuente")
    if source.kind != "supplier":
        # D-108(c): se informa con un código genérico; uno preciso exigiría contrato v0.1.
        raise DomainError(
            "INVALID_SOURCE",
            f"«{source.name}» no es una fuente de proveedor: los precios públicos de cadena no se "
            "importan por esta vía.",
            status_code=422,
        )
    if len(content) > MAX_BYTES:
        raise DomainError(
            "PAYLOAD_TOO_LARGE", "El archivo supera los 10 MB permitidos.", status_code=413
        )
    file_hash = hashlib.sha256(content).hexdigest()
    await advisory_lock(session, f"import:{org}:{source.id}")

    if reuse_previous:
        previous = (
            await session.execute(
                select(ImportRun).where(
                    ImportRun.organization_id == org,
                    ImportRun.source_id == source.id,
                    ImportRun.file_hash == file_hash,
                )
            )
        ).scalar_one_or_none()
        if previous is not None:
            # Mismo archivo (mismo hash) para la misma fuente: resultado original, sin escribir de
            # nuevo. Una repetición también repara la marca de última carga de las importaciones
            # hechas antes de que se guardara, sin crear ofertas ni corridas.
            if previous.rows_accepted > 0 and (
                source.last_success_at is None or source.last_success_at < previous.imported_at
            ):
                source.last_success_at = previous.imported_at
                source.last_error = None
            return _outcome_of(previous, dry_run=dry_run)

    if profile_override is not None:
        profile = profile_override
    else:
        profile = (
            await session.execute(
                select(ImportProfile).where(
                    ImportProfile.organization_id == org, ImportProfile.source_id == source.id
                )
            )
        ).scalar_one_or_none()
    if profile is None:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            "La fuente no tiene perfil de importación: sin perfil no se puede leer la planilla.",
            status_code=422,
        )
    if profile.status != "active":
        raise DomainError(
            "IMPORT_FORMAT_INVALID", "El perfil de importación de la fuente está inactivo.", status_code=422
        )
    prof = _load_profile(profile)

    header, rows = _read_sheet(
        content, profile.sheet, profile.header_row, profile.file_format
    )
    required_roles = ("sku", "price") if prof.default_sale_unit else REQUIRED_ROLES
    prof.columns = _resolve_columns(prof.column_specs, header, required_roles=required_roles)
    if "pack" in prof.columns and prof.pack_base_unit is None:
        raise DomainError(
            "IMPORT_FORMAT_INVALID",
            "El perfil mapea «pack» pero no declara «options.pack_base_unit»: no se puede "
            "guardar el contenido sin su unidad base.",
            status_code=422,
        )

    kind, tz = await _organization(session, org)
    origin = origin_for(kind)
    items = await _mappings(session, org, source.id)
    now = datetime.now(UTC)

    accepted: list[tuple[AcceptedRow, str | None]] = []
    rejected: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    detail: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row_number, values in rows:
        external_id = _text(values[prof.columns["sku"]] if prof.columns["sku"] < len(values) else None)
        if external_id is not None and external_id in seen:
            problem = RowProblem(
                DUPLICATE_ROW,
                f"El código {external_id} ya viene en una fila anterior de este archivo.",
                fields=("sku",),
            )
            rejected.append(_rejected_row(row_number, values, prof, source.id, file_hash, problem))
            errors.append({"row": row_number, "code": problem.code, "message": problem.message})
            if dry_run:
                detail.append(
                    _check_rejected_row(
                        _rejected_row(row_number, values, prof, source.id, file_hash, problem),
                        problem,
                    )
                )
            continue
        if external_id is not None:
            seen.add(external_id)
        try:
            row, warning = _classify_row(row_number, values, prof, items, tz, now)
        except RowProblem as problem:
            rejected.append(_rejected_row(row_number, values, prof, source.id, file_hash, problem))
            errors.append({"row": row_number, "code": problem.code, "message": problem.message})
            if dry_run:
                detail.append(
                    _check_rejected_row(
                        _rejected_row(row_number, values, prof, source.id, file_hash, problem),
                        problem,
                    )
                )
            continue
        accepted.append((row, warning))
        if dry_run:
            detail.append(_check_accepted_row(row, warning))
        if warning is not None:
            errors.append(
                {
                    "row": row_number,
                    "code": warning,
                    "message": "La fila trae condiciones comerciales en texto: la oferta se acepta y "
                    "queda pendiente de revisión.",
                }
            )

    rows_total = len(accepted) + len(rejected)
    if not accepted:
        status = "failed"
    elif rejected:
        status = "completed_with_errors"
    else:
        status = "completed"

    if dry_run:
        return ImportOutcome(
            status=status,
            rows_total=rows_total,
            rows_accepted=len(accepted),
            rows_rejected=len(rejected),
            errors=errors,
            observed_at=now,
            dry_run=True,
            rows=detail,
        )

    # Artículos nuevos: solo para las filas que ya pasaron toda la validación. Una fila rechazada
    # no deja un artículo huérfano, y el artículo nace con el nombre publicado en la planilla.
    for row, _warning in accepted:
        if row.needs_item:
            row.trade_item_id = await _create_item_for_row(session, actor, org, source, origin, row)

    run = ImportRun(
        organization_id=org,
        source_id=source.id,
        profile_id=profile.id,
        file_name=file_name[:300],
        file_hash=file_hash,
        status=status,
        rows_total=rows_total,
        rows_accepted=len(accepted),
        rows_rejected=len(rejected),
        errors=errors,
        rejected_rows=rejected,
        profile_snapshot=prof.snapshot,
        data_origin=origin,
    )
    session.add(run)
    await session.flush()
    for row, warning in accepted:
        # Mínimo, múltiplo y stock se expresan en la unidad declarada por el perfil o, si no la
        # declara, en la unidad de venta de la propia fila. Nunca se supone otra.
        q_unit = prof.quantity_unit or row.sale_unit
        session.add(
            OfferSnapshot(
                organization_id=org,
                supplier_id=source.supplier_id,
                source_id=source.id,
                trade_item_id=row.trade_item_id,
                external_id=row.external_id,
                external_name=row.external_name,
                price_amount=row.price,
                currency=row.currency,
                price_basis=row.price_basis,
                sale_unit=row.sale_unit,
                sale_unit_text=row.sale_unit_text,
                pack_units_per_commercial_unit=row.pack_units,
                pack_base_unit=prof.pack_base_unit if row.pack_units is not None else None,
                minimum_known=row.minimum is not None,
                minimum_quantity=row.minimum,
                minimum_unit=q_unit if row.minimum is not None else None,
                multiple_known=row.multiple is not None,
                multiple_quantity=row.multiple,
                multiple_unit=q_unit if row.multiple is not None else None,
                stock_status="known" if row.stock is not None else "unknown",
                stock_quantity=row.stock,
                stock_unit=q_unit if row.stock is not None else None,
                tax_included=prof.tax_included,
                discount_included=prof.discount_included,
                notes=row.notes,
                # Una condición no modelada deja la oferta marcada: se guarda, pero no es una
                # oferta verificada y no puede elegir proveedor (D-110).
                review_status=REVIEW_NEEDS_REVIEW if warning else REVIEW_ACCEPTED,
                observed_at=row.observed_at,
                evidence={
                    "file_name": file_name[:300],
                    "file_hash": file_hash,
                    "row": row.row,
                    "import_run_id": str(run.id),
                    "profile_version": prof.version,
                },
                data_origin=origin,
            )
        )
    await session.flush()
    # La respuesta y una repetición posterior deben coincidir: se informa la marca guardada.
    await session.refresh(run, ["imported_at"])
    if accepted:
        # Una importación con filas aceptadas es una lectura exitosa de la fuente. E4 deriva el
        # estado de esta marca; sin ella, la pantalla seguiría diciendo «sin archivo cargado».
        source.last_success_at = run.imported_at
        source.last_error = None
    emit(
        session,
        org_id=org,
        actor_id=actor.id,
        action="import_run.created",
        entity_type="import_run",
        entity_id=run.id,
        event_type="catalog.import_completed",
        aggregate_version=None,
        payload={
            "source_id": str(source.id),
            "file_hash": file_hash,
            "status": status,
            "rows_total": rows_total,
            "rows_accepted": len(accepted),
            "rows_rejected": len(rejected),
            "offers_written": len(accepted),
        },
        after={
            "status": status,
            "rows_total": rows_total,
            "rows_accepted": len(accepted),
            "rows_rejected": len(rejected),
        },
    )
    return ImportOutcome(
        import_id=run.id,
        status=status,
        rows_total=rows_total,
        rows_accepted=len(accepted),
        rows_rejected=len(rejected),
        errors=errors,
        observed_at=run.imported_at,
        dry_run=False,
    )


def _outcome_of(run: ImportRun, *, dry_run: bool = False) -> ImportOutcome:
    """Resultado original de una corrida ya guardada, sin volver a leer el archivo."""
    return ImportOutcome(
        import_id=run.id,
        status=run.status,
        rows_total=run.rows_total,
        rows_accepted=run.rows_accepted,
        rows_rejected=run.rows_rejected,
        errors=list(run.errors or []),
        observed_at=run.imported_at,
        repeated=True,
        dry_run=dry_run,
    )


async def run_import_request(
    session: AsyncSession,
    actor: Actor,
    *,
    source_id: uuid.UUID,
    supplier_id: uuid.UUID,
    idempotency_key: str,
    file_name: str,
    content: bytes,
) -> ImportOutcome:
    """Camino de la API para E8: valida el par fuente/proveedor y aplica la `Idempotency-Key`.

    Es una capa fina sobre `run_import` (que hace todo el trabajo): aquí solo viven las reglas del
    sobre congelado en C0 que no existen en la CLI — clave obligatoria, 409 al reutilizarla con otro
    archivo o contexto, y la fuente debe pertenecer al proveedor declarado. Nada de esto lee ni
    escribe ofertas por su cuenta.

    La clave queda registrada en `import_request` **también** cuando la importación ya existía por
    otra clave: así una reutilización posterior con otro archivo se detecta (D-111).
    """
    org = actor.organization_id
    if Role.ADMIN.value not in (actor.roles or []):
        raise DomainError(
            "FORBIDDEN", "Solo la administración puede importar listas de precios.", status_code=403
        )
    source = (
        await session.execute(
            select(Source).where(Source.organization_id == org, Source.id == source_id)
        )
    ).scalar_one_or_none()
    if source is None:
        raise not_found("La fuente")
    if source.supplier_id is not None and source.supplier_id != supplier_id:
        # Ambas partes vienen del cliente; no se acepta una fuente que no sea de ese proveedor.
        # Una fuente de cadena (sin proveedor) sigue su propio camino y se rechaza por su tipo.
        raise DomainError(
            "INVALID_SOURCE",
            "La fuente indicada no pertenece al proveedor indicado.",
            status_code=422,
        )

    file_hash = hashlib.sha256(content).hexdigest()
    await advisory_lock(session, f"idem:{org}:{idempotency_key}")

    prior = (
        await session.execute(
            select(ImportRequest).where(
                ImportRequest.organization_id == org,
                ImportRequest.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        # La clave ya se usó: solo vale para el mismo contexto exacto.
        if (prior.file_hash, prior.source_id, prior.supplier_id) != (
            file_hash,
            source.id,
            supplier_id,
        ):
            raise _key_reused()
        run = await session.get(ImportRun, prior.import_run_id)
        return _outcome_of(_check_run(run, org=org, source_id=source.id, file_hash=file_hash))

    outcome = await run_import(
        session, actor, source_id=source.id, file_name=file_name, content=content
    )
    run = await session.get(ImportRun, outcome.import_id)
    _check_run(run, org=org, source_id=source.id, file_hash=file_hash)

    # La solicitud se registra en la misma transacción: si algo falla, la clave no queda consumida.
    session.add(
        ImportRequest(
            organization_id=org,
            idempotency_key=idempotency_key,
            source_id=source.id,
            supplier_id=supplier_id,
            file_hash=file_hash,
            import_run_id=run.id,
            created_by=actor.id,
            data_origin=run.data_origin,
        )
    )
    await session.flush()
    return outcome


def _key_reused() -> DomainError:
    return DomainError(
        "IDEMPOTENCY_KEY_REUSED",
        "Esa Idempotency-Key ya se usó con otro archivo o con otra fuente o proveedor.",
        status_code=409,
    )


def _check_run(
    run: ImportRun | None, *, org: uuid.UUID, source_id: uuid.UUID, file_hash: str
) -> ImportRun:
    """Una corrida solo se reutiliza si es de esta organización y esta fuente, no por el hash solo."""
    if run is None or (run.organization_id, run.source_id, run.file_hash) != (
        org,
        source_id,
        file_hash,
    ):
        raise RuntimeError("La corrida de importación no corresponde a la solicitud.")
    return run
