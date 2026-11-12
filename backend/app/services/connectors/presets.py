"""Perfiles conocidos.

Un preset es comodidad de configuración, no un conector: produce exactamente el mismo perfil
declarativo que una persona podría escribir a mano. Farmaloop no tiene módulo propio; su perfil usa
la estrategia Algolia genérica.

Hallazgos verificados que sostienen este preset (2026-11-09):
* índice público `f_products_prod` y búsqueda sin autenticación de cuenta;
* campos `sku`, `fullName`, `laboratoryName`, `presentation`, `bestPrice`, `photoURL`, `ean`,
  `batchs[].stock`;
* los precios observados ($12.990 y $1.990) son de la consulta pública, no de una cuenta.

Lo que este preset deliberadamente **no** afirma:
* unidad de venta ni base de precio: siguen sin confirmar;
* unidad de stock: `batchs[].stock` se lee pero no se expone mientras no haya unidad confirmada;
* URL de ficha: no se inventa una plantilla sin verificarla;
* `unitCost` no se mapea ni se registra: es un dato de cuenta, no público.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FARMALOOP = "farmaloop"
PRESETS = (FARMALOOP,)

# Identidad del conector FarmaLoop en el perfil guardado: la referencia de la credencial de su
# Algolia público. No es un nombre que envíe el cliente.
FARMALOOP_APP_ID_SECRET_REF = "FARMALOOP_ALGOLIA_APP_ID"

_PRESET: dict[str, Any] = {
    "mechanism": "algolia",
    "config": {
        "endpoint": "https://{app_id}-dsn.algolia.net/1/indexes/{index}/query",
        "app_id_secret_ref": FARMALOOP_APP_ID_SECRET_REF,
        "api_key_secret_ref": "FARMALOOP_ALGOLIA_API_KEY",
        "index": "f_products_prod",
        "query_param": "query",
        "hits_per_page": 15,
        "page_param": "page",
        "page": 0,
        "timeout_seconds": 10,
        "max_response_bytes": 2_000_000,
        "item_path": "hits",
        "currency": "CLP",
    },
    "field_map": {
        "external_id": {"path": "sku", "type": "string"},
        "name": {"path": "fullName", "type": "string"},
        "laboratory": {"path": "laboratoryName", "type": "string"},
        "presentation": {"path": "presentation", "type": "string"},
        "price": {"path": "bestPrice", "type": "decimal"},
        "currency": {"const": "CLP", "type": "string"},
        "image_url": {"path": "photoURL", "type": "url"},
        "ean": {"path": "ean", "type": "string"},
        # Se conserva la ruta para cuando una persona confirme la unidad; hoy no se expone stock.
        "stock": {"path": "batchs[].stock", "transform": "sum", "type": "decimal"},
        # El portal informa si la publicación está disponible para la venta (0/1).
        "available_for_sale": {"path": "availableForSale", "type": "bool"},
        # Precio de lista: solo referencial cuando no hay precio comercial vigente.
        "reference_price": {"path": "basePrice", "type": "decimal"},
    },
}


def preset(name: str) -> dict[str, Any]:
    """Copia profunda del preset. Un preset desconocido no se sustituye por otro."""
    if name != FARMALOOP:
        raise KeyError(name)
    return {
        "mechanism": _PRESET["mechanism"],
        "config": {k: (dict(v) if isinstance(v, dict) else v) for k, v in _PRESET["config"].items()},
        "field_map": {k: dict(v) for k, v in _PRESET["field_map"].items()},
    }


def allows_unknown_commercial_terms(
    *, mechanism: str | None, config: Mapping[str, Any] | None
) -> bool:
    """¿Este conector puede cotizar sin unidad de venta ni base del precio?

    Solo FarmaLoop, y por su **identidad guardada**: el mecanismo Algolia junto con la referencia
    de credencial que lo identifica. La pregunta se responde con el perfil del servidor, nunca con
    un nombre, proveedor ni campo enviado por el cliente, así que la excepción no se puede pedir
    desde la API ni reutilizar para otro proveedor.
    """
    return (
        mechanism == "algolia"
        and isinstance(config, Mapping)
        and config.get("app_id_secret_ref") == FARMALOOP_APP_ID_SECRET_REF
    )
