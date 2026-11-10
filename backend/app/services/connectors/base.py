"""Contrato mínimo del motor de proveedores.

Una estrategia recibe una consulta y un perfil y devuelve ofertas normalizadas o falla con un
motivo cerrado. No persiste nada, no conoce E1–E3 y no llama a la IA.

Los cuatro estados visibles del motor se derivan de estas excepciones y del resultado:

* `not_configured`      — falta el perfil, el interruptor o un secreto referenciado.
* `connected`           — la consulta respondió y los campos obligatorios se pudieron leer.
* `error`               — fallo transitorio de red, tasa o proveedor; se puede reintentar.
* `requires_intervention` — configuración, credenciales o estructura que una persona debe corregir.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

CONNECTOR_STATES = (
    "not_configured",
    "connected",
    "error",
    "requires_intervention",
)

MECHANISMS = (
    "algolia",
    "rest_json",
    "html_form",
    "html_embedded_json",
    "manual_capture",
    "browser_required",
    "unsupported",
)


class ConnectorNotConfigured(Exception):
    """Falta configuración o un secreto referenciado. No es un fallo del proveedor."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ConnectorError(Exception):
    """Fallo transitorio: red, tiempo de espera, tasa o respuesta 5xx."""

    def __init__(self, reason: str = "fetch_failed"):
        super().__init__(reason)
        self.reason = reason


class ConnectorIntervention(Exception):
    """La estructura, las credenciales o la configuración necesitan a una persona."""

    def __init__(self, reason: str = "requires_intervention", *, details: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


@dataclass(frozen=True)
class SupplierOffer:
    """Oferta normalizada de una fuente. Un dato desconocido queda en `None`, nunca en 0/1."""

    external_id: str
    name: str
    price: Decimal
    currency: str
    laboratory: str | None = None
    presentation: str | None = None
    price_basis: str | None = None
    sale_unit: str | None = None
    stock_status: str = "unknown"
    stock_quantity: Decimal | None = None
    stock_unit: str | None = None
    image_url: str | None = None
    product_url: str | None = None
    ean: str | None = None


@dataclass(frozen=True)
class PublishedProduct:
    """Publicación del catálogo sin precio comercial vigente.

    No es una oferta: no tiene precio de venta, no puede entrar a un carrito y no participa de
    comparaciones ni totales. `reference_price` es el precio de lista del proveedor y se muestra
    **solo** como referencial. Un dato ausente queda en `None`, nunca en 0/1.
    """

    external_id: str
    name: str
    currency: str
    laboratory: str | None = None
    presentation: str | None = None
    stock_status: str = "unknown"
    reference_price: Decimal | None = None
    image_url: str | None = None
    product_url: str | None = None
    ean: str | None = None


@dataclass(frozen=True)
class ConnectorResult:
    offers: list[SupplierOffer]
    observed_at: datetime
    fingerprint: str | None = None
    discarded: int = 0
    # Publicaciones con identidad válida pero sin precio de venta: se muestran, no se compran.
    published: list[PublishedProduct] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SupplierStrategy(Protocol):
    mechanism: str

    async def search(
        self,
        query: str,
        *,
        config: dict[str, Any],
        field_map: dict[str, Any],
        limit: int,
        transport: Any | None = None,
    ) -> ConnectorResult: ...


def schema_fingerprint(items: list[Any], *, sample: int = 3) -> str | None:
    """Huella de la forma de la respuesta (rutas y tipos), no de sus valores.

    Es deliberadamente barata: recorre solo los primeros `sample` ítems y acota la profundidad.
    Un cambio de tipos o de campos cambia la huella; un cambio de precio no.
    """
    if not items:
        return None
    paths: set[str] = set()

    def walk(value: Any, prefix: str, depth: int) -> None:
        if depth > 12:
            return
        if isinstance(value, dict):
            for key in sorted(value):
                walk(value[key], f"{prefix}.{key}" if prefix else str(key), depth + 1)
        elif isinstance(value, list):
            paths.add(f"{prefix}[]:list")
            for item in value[:2]:
                walk(item, f"{prefix}[]", depth + 1)
        elif value is None:
            # Un campo opcional nulo no cambia la forma: evita marcar el portal como cambiado
            # solo porque un producto no trae fotografía o presentación.
            return
        elif isinstance(value, bool):
            paths.add(f"{prefix}:bool")
        elif isinstance(value, (int, float)):
            paths.add(f"{prefix}:number")
        else:
            paths.add(f"{prefix}:{type(value).__name__}")

    for item in items[:sample]:
        walk(item, "", 0)
    if not paths:
        return None
    payload = "\n".join(sorted(paths))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:64]
