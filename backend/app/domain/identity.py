"""Laboratorio y marca reconocidos en la descripción publicada (D-150, revisión 2026-11-10).

La extracción es **determinista y verificable**: solo reconoce una entidad cuando la descripción
trae una marca explícita («LAB.», «LABORATORIO», «MARCA») seguida de un nombre que ya existe en el
**vocabulario controlado** de la organización (columnas publicadas por el proveedor, el catálogo
propio o listas anteriores). No se usan parecidos, ni posiciones, ni modelos de lenguaje: si el
nombre no está en el vocabulario, no se asigna nada.

La procedencia viaja en `offer_snapshot.evidence` (JSONB), de modo que un dato extraído nunca se
confunde con una columna publicada:

* `source="column"`: el proveedor lo publicó en una columna propia.
* `source="description"`: se reconoció en la descripción con una marca explícita y un nombre del
  vocabulario; el valor es el nombre canónico del vocabulario y `matched_text` conserva el texto
  exacto que lo respalda.

Una oferta con identidad extraída o sin identidad queda visible pero **no se presenta como
completa**: el estado (`extracted`/`unconfirmed`) se expone para que la pantalla lo indique.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.domain.products import normalize_name

LABORATORY = "laboratory"
BRAND = "brand"

SOURCE_COLUMN = "column"
SOURCE_DESCRIPTION = "description"

STATUS_PUBLISHED = "published"
STATUS_EXTRACTED = "extracted"
STATUS_UNCONFIRMED = "unconfirmed"

# Marcas explícitas: una palabra completa «LAB.», «LABORATORIO(S)» o «MARCA», seguida del nombre.
# El nombre termina en un separador o en una raya; un nombre interno con espacios se conserva.
_MARKER_RE = re.compile(
    r"(?<![\w])"
    r"(?P<marker>laboratorios?|lab|marca)"
    r"(?![\w])"
    r"\s*\.?\s*"
    r"(?P<name>.+?)"
    r"(?=\s+-\s+|[/,;()\[\]{}|]|$)",
    re.IGNORECASE,
)
_LEADING_LAB_RE = re.compile(r"^(?:laboratorios?|lab)\b")


@dataclass(frozen=True)
class IdentityVocabulary:
    """Nombres ya confirmados, indexados por su forma normalizada y comparable.

    Las claves incluyen la expansión de «Lab.»/«Laboratorios» a «laboratorio», para que una
    descripción y una columna escritas con abreviaturas distintas coincidan sin heurísticas.
    """

    laboratories: dict[str, str] = field(default_factory=dict)
    brands: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.laboratories and not self.brands


EMPTY_VOCABULARY = IdentityVocabulary()


@dataclass(frozen=True)
class ExtractedIdentity:
    """Lo reconocido en una descripción; `None` significa que no hay evidencia suficiente."""

    laboratory: str | None = None
    brand: str | None = None
    laboratory_text: str | None = None
    brand_text: str | None = None


def _expand_lab(name: str) -> str:
    """«Lab. Cordillera Ficticio» y «Laboratorios Cordillera Ficticio» → misma clave."""
    normalized = normalize_name(name)
    return _LEADING_LAB_RE.sub("laboratorio", normalized, count=1) if normalized else ""


def _index(values: Iterable[str | None], *, laboratory: bool) -> dict[str, str]:
    index: dict[str, str] = {}
    for value in values:
        if value is None:
            continue
        text = " ".join(str(value).split())
        if not text:
            continue
        key = _expand_lab(text) if laboratory else normalize_name(text)
        if key:
            index.setdefault(key, text[:200])
    return index


def build_vocabulary(
    laboratories: Iterable[str | None], brands: Iterable[str | None]
) -> IdentityVocabulary:
    """Vocabulario controlado: solo nombres que ya publicó el proveedor o el catálogo propio."""
    return IdentityVocabulary(
        laboratories=_index(laboratories, laboratory=True),
        brands=_index(brands, laboratory=False),
    )


def _lookup(candidate: str, table: dict[str, str]) -> str | None:
    """Nombre canónico si `candidate` es una entrada del vocabulario o la empieza como palabra."""
    if not candidate or not table:
        return None
    for key in sorted(table, key=len, reverse=True):
        if candidate == key or candidate.startswith(f"{key} "):
            return table[key]
    return None


def extract_identity(description: str | None, vocabulary: IdentityVocabulary) -> ExtractedIdentity:
    """Reconoce laboratorio y marca en la descripción con evidencia explícita y vocabulario.

    Nunca inventa: si falta la marca («LAB.», «MARCA»), si el nombre no está en el vocabulario o
    si el texto es ambiguo (`LAB/MARCA SUR`), el campo queda `None`.
    """
    if not description or vocabulary.is_empty:
        return ExtractedIdentity()
    laboratory: str | None = None
    brand: str | None = None
    laboratory_text: str | None = None
    brand_text: str | None = None
    for match in _MARKER_RE.finditer(description):
        marker = normalize_name(match.group("marker"))
        if marker not in ("lab", "laboratorio", "laboratorios", "marca"):
            continue
        name = normalize_name(match.group("name"))
        if not name:
            continue
        if marker == "marca":
            value = _lookup(normalize_name("marca " + name), vocabulary.brands)
            if value is not None and brand is None:
                brand = value
                brand_text = " ".join(match.group(0).split())
        else:
            value = _lookup(normalize_name("laboratorio " + name), vocabulary.laboratories)
            if value is not None and laboratory is None:
                laboratory = value
                laboratory_text = " ".join(match.group(0).split())
    return ExtractedIdentity(
        laboratory=laboratory,
        brand=brand,
        laboratory_text=laboratory_text,
        brand_text=brand_text,
    )


def identity_status(
    laboratory: str | None,
    brand: str | None,
    laboratory_source: str | None,
    brand_source: str | None,
) -> str:
    """Estado de identificación: publicado, extraído de la descripción o sin identificar."""
    sources = (laboratory_source, brand_source)
    if SOURCE_DESCRIPTION in sources:
        return STATUS_EXTRACTED
    if laboratory or brand:
        return STATUS_PUBLISHED
    return STATUS_UNCONFIRMED


def identity_evidence(
    laboratory: str | None,
    brand: str | None,
    laboratory_source: str | None,
    brand_source: str | None,
    laboratory_text: str | None = None,
    brand_text: str | None = None,
) -> dict:
    """Procedencia estructurada que se guarda en `offer_snapshot.evidence.identity`."""
    return {
        "status": identity_status(laboratory, brand, laboratory_source, brand_source),
        "laboratory": (
            {"source": laboratory_source, "matched_text": laboratory_text} if laboratory else None
        ),
        "brand": ({"source": brand_source, "matched_text": brand_text} if brand else None),
    }


def _source_of(field_value: object) -> str | None:
    if isinstance(field_value, dict):
        source = field_value.get("source")
        return source if source in (SOURCE_COLUMN, SOURCE_DESCRIPTION) else None
    return None


def identity_from_offer(
    laboratory: str | None, brand: str | None, evidence: object
) -> tuple[str, str | None, str | None]:
    """Estado y procedencias de una oferta guardada; tolera ofertas anteriores a D-150.

    Una oferta con valor y sin `evidence.identity` se considera publicada en columna: es lo único
    que el importador sabía escribir antes de este cambio.
    """
    identity = evidence.get("identity") if isinstance(evidence, dict) else None
    laboratory_source = (
        _source_of(identity.get("laboratory")) if isinstance(identity, dict) else None
    )
    brand_source = _source_of(identity.get("brand")) if isinstance(identity, dict) else None
    if laboratory and laboratory_source is None:
        laboratory_source = SOURCE_COLUMN
    if brand and brand_source is None:
        brand_source = SOURCE_COLUMN
    return (
        identity_status(laboratory, brand, laboratory_source, brand_source),
        laboratory_source,
        brand_source,
    )
