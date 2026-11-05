"""E15 — conector de Salcobrand sobre el índice público de Algolia.

Este módulo no conoce el coordinador, las otras cadenas ni la configuración global: recibe los
parámetros de conexión, consulta la fuente y devuelve fichas normalizadas con **solo lo publicado**.
Nada se persiste y ningún dato se completa por inferencia.

Reglas de datos (verificadas contra el sitio el 2026-11-05):

- `brand` es la marca comercial; **no** es el laboratorio fabricante y no se publica como tal.
- `prices_by_um` es un texto ya formateado por 100 g/ml; no es un precio por unidad de venta, así
  que no se usa como precio fraccionado ni se reinterpreta.
- `direct_discount_sbpay` es un precio exclusivo de un medio de pago: no se usa, ni siquiera como
  reemplazo del precio de oferta general.
- `has_stock` es un booleano publicado; no se traduce a cantidades ni a disponibilidad por sucursal.
- El filtro temporal usa `current_timestamp` de `/api/v2/preferences`, igual que el buscador del
  sitio. El desfase con ese sello se mide una vez y se reutiliza unos minutos: no se consulta la
  configuración en cada búsqueda.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 24
DEFAULT_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 2_000_000
CURRENCY = "CLP"
CONNECTOR_VERSION = "salcobrand-algolia-1"
PREFERENCES_PATH = "/api/v2/preferences"
DEFAULT_CLOCK_TTL_SECONDS = 600
FAILED_CLOCK_TTL_SECONDS = 30
MAX_CLOCK_DEVIATION_SECONDS = 86_400


class SalcobrandFetchError(Exception):
    """La fuente no respondió, excedió el tamaño permitido o devolvió un JSON inesperado."""


def search_params(
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    page: int = 0,
    moment: datetime | None = None,
) -> dict[str, str]:
    """Parámetros de Algolia para una consulta. El filtro temporal usa la hora del servidor.

    El sitio usa `current_timestamp` de su configuración y el filtro
    `(timestamp_available_on < T)`; `T` es la hora del servidor en ese instante, no una fecha fija.
    """
    moment = moment or datetime.now(UTC)
    timestamp = int(moment.timestamp())
    return {
        "query": query.strip(),
        "hitsPerPage": str(limit),
        "page": str(page),
        "filters": f"(timestamp_available_on < {timestamp})",
    }


async def fetch_current_timestamp(
    *,
    url: str,
    referer: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> int:
    """Lee el sello de tiempo vigente de la configuración pública del sitio.

    Es el mismo `current_timestamp` que usa el buscador de Salcobrand para filtrar disponibilidad.
    No lleva credenciales: solo el Referer del sitio, como cualquier visita.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SalcobrandFetchError("La configuración del sitio no es una URL HTTP(S) válida.")

    headers = {
        "User-Agent": "CotizadorFarmacia/0.1",
        "Accept": "application/json",
        "Referer": referer,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise SalcobrandFetchError("La configuración de Salcobrand no respondió.") from exc

    if len(response.content) > MAX_RESPONSE_BYTES:
        raise SalcobrandFetchError("La configuración de Salcobrand excede el tamaño permitido.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SalcobrandFetchError("Salcobrand no devolvió JSON válido.") from exc

    timestamp = payload.get("current_timestamp") if isinstance(payload, dict) else None
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
        raise SalcobrandFetchError("Salcobrand no publicó un sello de tiempo válido.")
    return timestamp


class SiteClock:
    """Hora del sitio medida una vez y extrapolada con el reloj local.

    Medir el desfase evita una consulta a la configuración por cada búsqueda y corrige un reloj
    local desviado. Ante un fallo o un sello inverosímil se usa el reloj local y no se falla la
    búsqueda: el filtro temporal es una condición de disponibilidad, no una credencial.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_CLOCK_TTL_SECONDS,
        failed_ttl_seconds: int = FAILED_CLOCK_TTL_SECONDS,
    ):
        self.ttl_seconds = ttl_seconds
        self.failed_ttl_seconds = failed_ttl_seconds
        self._offset_seconds: float | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def now(self, *, url: str, referer: str, timeout_seconds: int) -> datetime:
        if time.monotonic() >= self._expires_at:
            async with self._lock:
                # Otra búsqueda pudo refrescar mientras se esperaba el turno.
                if time.monotonic() >= self._expires_at:
                    await self._refresh(url, referer, timeout_seconds)
        if self._offset_seconds is None:
            return datetime.now(UTC)
        return datetime.now(UTC) + timedelta(seconds=self._offset_seconds)

    async def _refresh(self, url: str, referer: str, timeout_seconds: int) -> None:
        try:
            timestamp = await fetch_current_timestamp(
                url=url, referer=referer, timeout_seconds=timeout_seconds
            )
        except SalcobrandFetchError:
            logger.warning("E15: no se pudo leer la hora del sitio de Salcobrand.")
            self._offset_seconds = None
            self._expires_at = time.monotonic() + self.failed_ttl_seconds
            return

        offset = timestamp - time.time()
        if abs(offset) > MAX_CLOCK_DEVIATION_SECONDS:
            logger.warning("E15: el sello de tiempo de Salcobrand no es compatible con el reloj.")
            self._offset_seconds = None
            self._expires_at = time.monotonic() + self.failed_ttl_seconds
            return
        self._offset_seconds = offset
        self._expires_at = time.monotonic() + self.ttl_seconds

    def reset(self) -> None:
        self._offset_seconds = None
        self._expires_at = 0.0


clock = SiteClock()


async def fetch_search_json(
    query: str,
    *,
    url: str,
    app_id: str,
    api_key: str,
    index: str,
    referer: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    limit: int = DEFAULT_LIMIT,
    moment: datetime | None = None,
) -> dict[str, Any]:
    """Consulta el índice de Algolia y devuelve el JSON crudo (validado en su forma mínima)."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SalcobrandFetchError("La fuente configurada no es una URL HTTP(S) válida.")
    if not query.strip():
        raise SalcobrandFetchError("La consulta está vacía.")

    # La clave va en cabeceras, no en la URL: así nunca aparece en un mensaje de error, en un
    # registro ni en una traza. Tampoco se devuelve al frontend. El sitio además exige el Referer
    # de su propio dominio; sin él la clave pública responde 403.
    headers = {
        "User-Agent": "CotizadorFarmacia/0.1",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": referer,
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-API-Key": api_key,
    }
    body = {
        "requests": [
            {
                "indexName": index,
                "params": urlencode(search_params(query, limit=limit, moment=moment)),
            }
        ]
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise SalcobrandFetchError("La fuente de Salcobrand no respondió correctamente.") from exc

    if len(response.content) > MAX_RESPONSE_BYTES:
        raise SalcobrandFetchError("La respuesta de Salcobrand excede el tamaño permitido.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SalcobrandFetchError("Salcobrand no devolvió JSON válido.") from exc
    if not isinstance(payload, dict):
        raise SalcobrandFetchError("Salcobrand devolvió una respuesta inesperada.")
    return payload


def parse_search_results(
    payload: dict[str, Any],
    *,
    limit: int = DEFAULT_LIMIT,
    observed_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Normaliza los productos publicados. Una variante sin nombre, SKU o enlace se omite."""
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise SalcobrandFetchError("Salcobrand no devolvió resultados de búsqueda.")
    first = results[0]
    hits = first.get("hits") if isinstance(first, dict) else None
    if not isinstance(hits, list):
        raise SalcobrandFetchError("Salcobrand devolvió una respuesta sin productos.")

    moment = observed_at or datetime.now(UTC)
    productos: list[dict[str, Any]] = []
    for hit in hits:
        product = _product(hit, moment)
        if product is None:
            continue
        productos.append(product)
        if len(productos) >= limit:
            break
    return productos


def _product(hit: Any, observed_at: datetime) -> dict[str, Any] | None:
    if not isinstance(hit, dict):
        return None
    name = _text(hit.get("name"))
    sku = _text(hit.get("sku")) or _text(hit.get("objectID"))
    slug = _text(hit.get("slug"))
    if not name or not sku or not slug:
        return None

    normal = _amount(hit.get("normal_price"))
    promo = _amount(hit.get("direct_discount"))
    ribbon = hit.get("ribbon_info") if isinstance(hit.get("ribbon_info"), dict) else {}
    if promo is not None:
        conditions = _text(ribbon.get("promotion_label"))
    else:
        # Sin oferta general no hay condición de oferta que mostrar.
        conditions = None

    has_stock = hit.get("has_stock") if isinstance(hit.get("has_stock"), bool) else None

    return {
        "chain_item_id": sku,
        "published_name": name,
        "brand_as_published": _text(hit.get("brand")),
        "laboratory_as_published": None,
        "presentation_as_published": _text(hit.get("options_text")),
        "description": None,
        "image_url": _url(hit.get("catalog_image_url")),
        "price": {
            "normal": _money(normal),
            "promotional": _money(promo),
            # `prices_by_um` describe un precio por 100 g/ml ya formateado: no es un precio
            # fraccionado por unidad de venta y no se representa como tal.
            "fractional": None,
        },
        "conditions": conditions,
        "has_stock": has_stock,
        "source_url": (
            f"https://salcobrand.cl/products/{quote(slug, safe='')}"
            f"?default_sku={quote(sku, safe='')}"
        ),
        "observed_at": observed_at.isoformat(),
        "provenance": {
            "source": "salcobrand",
            "data_origin": "published",
            "parser_version": CONNECTOR_VERSION,
        },
        "match": {"level": "not_evaluated", "causes": []},
    }


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def _url(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    parsed = urlparse(text)
    return text if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _amount(value: Any) -> Decimal | None:
    """Importe publicado. Se rechaza float para no introducir redondeos silenciosos."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, str)):
        return None
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def _money(amount: Decimal | None) -> dict[str, str] | None:
    if amount is None:
        return None
    if amount == amount.to_integral_value():
        text = str(int(amount))
    else:
        text = format(amount.normalize(), "f")
    return {"amount": text, "currency": CURRENCY}
