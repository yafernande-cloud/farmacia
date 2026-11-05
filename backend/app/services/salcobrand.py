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
import json
import logging
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 24
DEFAULT_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 2_000_000
CURRENCY = "CLP"
CONNECTOR_VERSION = "salcobrand-algolia-2"
PREFERENCES_PATH = "/api/v2/preferences"
DEFAULT_CLOCK_TTL_SECONDS = 600
FAILED_CLOCK_TTL_SECONDS = 30
MAX_CLOCK_DEVIATION_SECONDS = 86_400

# Enriquecimiento de fabricante desde los datos estructurados de la ficha publicada. No comparte
# credenciales con Algolia: la ficha es HTML público y solo se lee `manufacturer.name`.
DEFAULT_LABORATORY_CONCURRENCY = 6
DEFAULT_LABORATORY_BUDGET_SECONDS = 8.0
DEFAULT_PRODUCT_TIMEOUT_SECONDS = 8
MAX_PRODUCT_BYTES = 1_500_000
LABORATORY_CACHE_TTL_SECONDS = 86_400
LABORATORY_CACHE_MAX_ENTRIES = 2_000
ALLOWED_PRODUCT_HOSTS = {"salcobrand.cl", "www.salcobrand.cl"}


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


async def fetch_product_page(
    *,
    url: str,
    referer: str,
    timeout_seconds: int = DEFAULT_PRODUCT_TIMEOUT_SECONDS,
) -> str:
    """Descarga la ficha HTML pública de un producto. No lleva credenciales de Algolia.

    Solo se aceptan URL HTTPS del propio sitio: la URL viene de una ficha normalizada, pero se
    valida igual para no convertir el conector en un cliente HTTP arbitrario.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_PRODUCT_HOSTS:
        raise SalcobrandFetchError("La ficha de Salcobrand no es una URL válida.")

    headers = {
        "User-Agent": "CotizadorFarmacia/0.1",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "es-CL,es;q=0.9",
        "Referer": referer,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise SalcobrandFetchError("La ficha de Salcobrand no respondió correctamente.") from exc

    if len(response.content) > MAX_PRODUCT_BYTES:
        raise SalcobrandFetchError("La ficha de Salcobrand excede el tamaño permitido.")
    return response.text


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


class _JsonLdScripts(HTMLParser):
    """Recolecta el contenido de los bloques `application/ld+json` de una ficha."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._capturando = False
        self._buffer: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "script":
            return
        tipo = dict(attrs).get("type") or ""
        if tipo.strip().lower() == "application/ld+json":
            self._capturando = True
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capturando:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturando:
            self.blocks.append("".join(self._buffer))
            self._capturando = False


def _jsonld_nodes(value: Any) -> list[dict[str, Any]]:
    """Nodos de datos estructurados, incluidos los agrupados en `@graph`."""
    if isinstance(value, list):
        nodos = []
        for item in value:
            nodos.extend(_jsonld_nodes(item))
        return nodos
    if not isinstance(value, dict):
        return []
    nodos = [value]
    graph = value.get("@graph")
    if isinstance(graph, list):
        nodos.extend(item for item in graph if isinstance(item, dict))
    return nodos


def _is_product_node(node: dict[str, Any]) -> bool:
    tipo = node.get("@type")
    if isinstance(tipo, str):
        return tipo == "Product"
    return isinstance(tipo, list) and "Product" in tipo


def parse_product_manufacturer(html: str) -> str | None:
    """Fabricante publicado en el JSON-LD de la ficha. Nunca usa `brand` como respaldo.

    El sitio publica `manufacturer.name` en el nodo `Product` de schema.org. Es un dato distinto
    de la marca comercial; si el bloque falta, no es JSON válido o no trae fabricante, se devuelve
    `None` y la ficha conserva `laboratory_as_published = None`.
    """
    if not isinstance(html, str):
        return None
    parser = _JsonLdScripts()
    try:
        parser.feed(html)
    except Exception:  # HTML malformado: se trata como ficha sin fabricante.
        return None

    for block in parser.blocks:
        try:
            data = json.loads(block)
        except (TypeError, ValueError):
            continue
        for node in _jsonld_nodes(data):
            if not _is_product_node(node):
                continue
            manufacturer = node.get("manufacturer")
            if isinstance(manufacturer, dict):
                name = _text(manufacturer.get("name"))
            else:
                name = _text(manufacturer)
            if name:
                return name
    return None


class LaboratoryCache:
    """Caché en memoria por SKU: valor publicado o ausencia confirmada, con vencimiento y tope."""

    def __init__(
        self,
        *,
        ttl_seconds: int = LABORATORY_CACHE_TTL_SECONDS,
        max_entries: int = LABORATORY_CACHE_MAX_ENTRIES,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max(1, max_entries)
        self._entries: OrderedDict[str, tuple[float, str | None]] = OrderedDict()

    def get(self, sku: str) -> tuple[bool, str | None]:
        """(encontrado, fabricante). Un `None` cacheado también es una respuesta válida."""
        if not isinstance(sku, str) or not sku:
            return False, None
        entry = self._entries.get(sku)
        if entry is None:
            return False, None
        expires_at, manufacturer = entry
        if time.monotonic() >= expires_at:
            self._entries.pop(sku, None)
            return False, None
        self._entries.move_to_end(sku)
        return True, manufacturer

    def set(self, sku: str, manufacturer: str | None) -> None:
        if not isinstance(sku, str) or not sku:
            return
        self._entries[sku] = (time.monotonic() + self.ttl_seconds, manufacturer)
        self._entries.move_to_end(sku)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


laboratory_cache = LaboratoryCache()


async def enrich_laboratory(
    products: list[dict[str, Any]],
    *,
    referer: str,
    timeout_seconds: int = DEFAULT_PRODUCT_TIMEOUT_SECONDS,
    concurrency: int = DEFAULT_LABORATORY_CONCURRENCY,
    budget_seconds: float = DEFAULT_LABORATORY_BUDGET_SECONDS,
    cache: LaboratoryCache | None = None,
) -> None:
    """Completa `laboratory_as_published` con el fabricante publicado en cada ficha.

    Modifica las fichas en sitio. Una ficha que falla, tarda más que el presupuesto o no publica
    fabricante se queda con `None`; nunca falla la búsqueda completa ni usa la marca como respaldo.
    La caché evita repetir consultas de SKU ya vistos, incluida la ausencia confirmada de dato.
    """
    if not products:
        return
    # Un cache vacío es un objeto válido: no se sustituye por el global por ser falsy.
    cache = cache if cache is not None else laboratory_cache

    objetivos: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for product in products:
        sku = _text(product.get("chain_item_id"))
        if not sku:
            continue
        encontrado, manufacturer = cache.get(sku)
        if encontrado:
            product["laboratory_as_published"] = manufacturer
            continue
        url = _url(product.get("source_url"))
        if not url:
            continue
        if sku in objetivos:
            objetivos[sku][1].append(product)
        else:
            objetivos[sku] = (url, [product])

    if not objetivos:
        return

    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def consultar(sku: str, url: str) -> tuple[str, str | None]:
        async with sem:
            try:
                html = await fetch_product_page(
                    url=url,
                    referer=referer,
                    timeout_seconds=timeout_seconds,
                )
            except Exception:
                # Error transitorio: no se cachea y la ficha conserva laboratorio nulo.
                return sku, None
            manufacturer = parse_product_manufacturer(html)
            cache.set(sku, manufacturer)
            return sku, manufacturer

    tareas = {
        asyncio.create_task(consultar(sku, url)): sku
        for sku, (url, _) in objetivos.items()
    }
    hechas, pendientes = await asyncio.wait(tareas, timeout=budget_seconds)
    for tarea in pendientes:
        tarea.cancel()
    if pendientes:
        await asyncio.gather(*pendientes, return_exceptions=True)

    for tarea in hechas:
        try:
            sku, manufacturer = tarea.result()
        except Exception:
            continue
        if not manufacturer:
            continue
        for product in objetivos[sku][1]:
            product["laboratory_as_published"] = manufacturer


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
