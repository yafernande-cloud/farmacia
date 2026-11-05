"""E15 — conector de Cruz Verde sobre su API pública de productos.

Este módulo no conoce el coordinador ni las otras cadenas: recibe la consulta, obtiene datos
publicados por Cruz Verde y devuelve fichas normalizadas. Nada se persiste y ningún campo se
completa por inferencia.

Reglas de datos (verificadas contra el sitio el 2026-11-05):

- La búsqueda exige una sesión anónima (`connect.sid`) que se obtiene con `POST /customer-service/
  login`; la sesión vive en memoria, se renueva ante un 401 y no se comparte con otras cadenas.
- `price-list-cl` es el precio normal; `price-sale-cl` es la oferta publicada y solo se muestra si
  es menor. `appliedPromotions["price-sale-cl"]` es la única prueba de una condición aplicada: el
  campo `promotions` puede enumerar campañas vigentes sin que se hayan aplicado a este producto.
- `price-club-cl`, `discountPercentage` y `promotionalPrice` no se usan para inventar condiciones
  ni para reemplazar el precio general publicado.
- `pum` trae un precio fraccionado ya expresado por unidad («$245 por Capsula», «$3.120 por
  100 mL»). Solo se acepta cuando el importe y la unidad son identificables; nunca se confunde
  con el precio del envase completo.
- `brand` es la marca comercial y no se publica como laboratorio. El laboratorio solo se informa
  cuando la ficha lo declara de forma explícita y distinta de la marca: primero la pestaña
  `LABORATORIO` y, si no existe, el campo `laboratory` (que en medicamentos repite la marca).
- `stock` queda fuera de alcance: `has_stock` siempre es `None`; no se consultan inventarios,
  sucursales ni disponibilidad nacional.
- Los enlaces públicos se toman del sitemap oficial; si el producto aún no está en el sitemap se
  usa `https://www.cruzverde.cl/product/{id}.html` y se marca `link_source: "fallback"`. El enlace
  técnico de Salesforce (`beta.cruzverde.cl/.../dw/shop/...`) nunca se publica.
"""

import asyncio
import html as html_lib
import logging
import re
import time
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.cruzverde.cl/product-service"
DEFAULT_LOGIN_URL = "https://api.cruzverde.cl/customer-service/login"
DEFAULT_SITEMAP_URL = "https://www.cruzverde.cl/sitemap_index.xml"
DEFAULT_LIMIT = 24
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_ENRICHMENT_BUDGET_SECONDS = 4.0
DEFAULT_LABORATORY_CONCURRENCY = 6
# Espera máxima, después de las fichas, para que termine el sitemap ya iniciado.
SITEMAP_GRACE_SECONDS = 0.75
CURRENCY = "CLP"
CONNECTOR_VERSION = "cruzverde-api-1"
USER_AGENT = "CotizadorFarmacia/0.1"
MAX_RESPONSE_BYTES = 2_000_000
MAX_SITEMAP_BYTES = 4_000_000
SESSION_TTL_SECONDS = 1_800
SITEMAP_TTL_SECONDS = 86_400
SITEMAP_FAILURE_TTL_SECONDS = 600
LABORATORY_CACHE_TTL_SECONDS = 86_400
LABORATORY_CACHE_MAX_ENTRIES = 2_000
SITE_ORIGIN = "https://www.cruzverde.cl"
FALLBACK_PRODUCT_URL = SITE_ORIGIN + "/product/{product_id}.html"
API_HEADERS = {
    "Accept": "application/json",
    "Origin": SITE_ORIGIN,
    "Referer": SITE_ORIGIN + "/",
    "User-Agent": USER_AGENT,
}


class CruzVerdeFetchError(Exception):
    """La fuente no respondió, excedió el tamaño permitido o devolvió datos inutilizables."""


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def _identifier(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return _text(value)


def _http_url(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def _amount(value: Any) -> Decimal | None:
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


def _clp_amount(text: str) -> Decimal | None:
    """Importe chileno publicado: `27450`, `3.120` o `1.149.500` (punto de miles)."""
    text = text.strip()
    if not re.fullmatch(r"\d{1,3}(?:\.\d{3})+|\d+", text):
        return None
    value = text.replace(".", "") if "." in text else text
    amount = _amount(value)
    if amount is None or amount <= 0:
        return None
    return amount


class AnonymousSession:
    """Sesión anónima en memoria: se renueva sola y nunca se comparte entre procesos."""

    def __init__(self, *, ttl_seconds: int = SESSION_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._cookie: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def cookie(self, *, login_url: str, timeout_seconds: int) -> str:
        async with self._lock:
            if self._cookie and time.monotonic() < self._expires_at:
                return self._cookie
            self._cookie = await self._login(login_url, timeout_seconds)
            self._expires_at = time.monotonic() + self.ttl_seconds
            return self._cookie

    async def refresh(
        self, stale_cookie: str, *, login_url: str, timeout_seconds: int
    ) -> str:
        """Renueva solo si la cookie vencida sigue siendo la vigente.

        Varias fichas pueden recibir 401 a la vez: la primera renueva y las demás reutilizan la
        cookie nueva sin iniciar sesiones adicionales.
        """
        async with self._lock:
            if self._cookie == stale_cookie:
                self._cookie = None
                self._expires_at = 0.0
        return await self.cookie(login_url=login_url, timeout_seconds=timeout_seconds)

    async def _login(self, login_url: str, timeout_seconds: int) -> str:
        parsed = urlparse(login_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CruzVerdeFetchError("La URL de sesión de Cruz Verde no es válida.")
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.post(login_url, json={}, headers=API_HEADERS)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise CruzVerdeFetchError("Cruz Verde no permitió iniciar la sesión anónima.") from exc
        if response.status_code not in {200, 201}:
            raise CruzVerdeFetchError("Cruz Verde no permitió iniciar la sesión anónima.")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise CruzVerdeFetchError("La sesión de Cruz Verde excede el tamaño permitido.")
        cookie = _connect_sid(response)
        if cookie is None:
            raise CruzVerdeFetchError("Cruz Verde no entregó una sesión anónima válida.")
        return cookie

    def reset(self) -> None:
        self._cookie = None
        self._expires_at = 0.0


def _connect_sid(response: httpx.Response) -> str | None:
    headers = response.headers.get_list("set-cookie")
    for header in headers:
        match = re.search(r"(?:^|,\s*)connect\.sid=([^;]+)", header)
        if match:
            return f"connect.sid={match.group(1)}"
    return None


async def _get_json(
    url: str,
    *,
    session: AnonymousSession | None,
    login_url: str,
    timeout_seconds: int,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CruzVerdeFetchError("La fuente configurada no es una URL HTTP(S) válida.")

    cookie = None
    if session is not None:
        cookie = await session.cookie(
            login_url=login_url, timeout_seconds=timeout_seconds
        )

    response: httpx.Response | None = None
    for attempt in range(2):
        headers = dict(API_HEADERS)
        if cookie:
            headers["Cookie"] = cookie
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.get(url, headers=headers)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise CruzVerdeFetchError("Cruz Verde no respondió correctamente.") from exc

        if (
            response.status_code == 401
            and attempt == 0
            and session is not None
            and cookie
        ):
            cookie = await session.refresh(
                cookie, login_url=login_url, timeout_seconds=timeout_seconds
            )
            continue
        break

    assert response is not None
    if response.status_code >= 400:
        raise CruzVerdeFetchError(
            f"Cruz Verde respondió con estado {response.status_code}."
        )
    if len(response.content) > max_bytes:
        raise CruzVerdeFetchError("La respuesta de Cruz Verde excede el tamaño permitido.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise CruzVerdeFetchError("Cruz Verde no devolvió JSON válido.") from exc
    if not isinstance(payload, dict):
        raise CruzVerdeFetchError("Cruz Verde devolvió una respuesta inesperada.")
    return payload


def search_params(
    query: str, *, limit: int = DEFAULT_LIMIT, offset: int = 0
) -> dict[str, str]:
    if not query.strip():
        raise CruzVerdeFetchError("La consulta está vacía.")
    if limit < 1:
        raise CruzVerdeFetchError("El límite de resultados debe ser al menos 1.")
    return {
        "q": query.strip(),
        "limit": str(min(limit, DEFAULT_LIMIT)),
        "offset": str(max(0, offset)),
        "isAndes": "true",
        "requestPage": "SearchPLP",
    }


async def fetch_search_json(
    query: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    login_url: str = DEFAULT_LOGIN_URL,
    session: AnonymousSession,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Consulta el buscador publicado con los mismos parámetros que usa el sitio."""
    url = base_url.rstrip("/") + "/products/search?" + urlencode(
        search_params(query, limit=limit, offset=offset)
    )
    return await _get_json(
        url,
        session=session,
        login_url=login_url,
        timeout_seconds=timeout_seconds,
    )


async def fetch_product_detail(
    product_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    login_url: str = DEFAULT_LOGIN_URL,
    session: AnonymousSession,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    product_id = product_id.strip()
    if not product_id:
        raise CruzVerdeFetchError("La ficha de Cruz Verde no tiene identificador.")
    url = base_url.rstrip("/") + "/products/detail/" + quote(product_id, safe="")
    return await _get_json(
        url,
        session=session,
        login_url=login_url,
        timeout_seconds=timeout_seconds,
    )


def parse_search_results(
    payload: dict[str, Any],
    *,
    limit: int = DEFAULT_LIMIT,
    observed_at: datetime | None = None,
    link_for: Any | None = None,
) -> list[dict[str, Any]]:
    """Normaliza hasta 24 resultados orgánicos, en el orden publicado.

    Los espacios patrocinados se anteponen a la lista y no traen `hitType: "product"`: se excluyen
    antes de recortar para no desplazar publicaciones orgánicas. Una ficha sin identificador o sin
    nombre no se puede presentar y se omite; el resto tolera campos ausentes.
    """
    hits = payload.get("hits")
    if hits is None:
        hits = []
    if not isinstance(hits, list):
        raise CruzVerdeFetchError("Cruz Verde devolvió una respuesta sin productos.")

    moment = observed_at or datetime.now(UTC)
    productos: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict) or hit.get("hitType") != "product":
            continue
        product = _product(hit, moment, link_for)
        if product is None:
            continue
        productos.append(product)
        if len(productos) >= max(1, limit):
            break
    return productos


def _product(
    hit: dict[str, Any], observed_at: datetime, link_for: Any | None
) -> dict[str, Any] | None:
    product_id = _identifier(hit.get("productId"))
    name = _text(hit.get("productName")) or _text(hit.get("name"))
    if not product_id or not name:
        return None

    normal, promotional, conditions = _prices(hit)
    image = hit.get("image") if isinstance(hit.get("image"), dict) else {}
    source_url = link_for(product_id) if callable(link_for) else None
    link_source = "sitemap" if source_url else "fallback"
    if not source_url and product_id.isdigit():
        source_url = FALLBACK_PRODUCT_URL.format(product_id=product_id)
    if not source_url:
        link_source = "unavailable"

    return {
        "chain_item_id": product_id,
        "published_name": name,
        "brand_as_published": _text(hit.get("brand")),
        "laboratory_as_published": None,
        "presentation_as_published": parse_presentation(name),
        "description": _text(hit.get("description")),
        "image_url": _http_url(image.get("disBaseLink")) or _http_url(image.get("link")),
        "price": {
            "normal": normal,
            "promotional": promotional,
            "fractional": parse_fractional(hit.get("pum")),
        },
        "conditions": conditions,
        # El stock está fuera de alcance: no se consulta ni se interpreta.
        "has_stock": None,
        "source_url": source_url,
        "observed_at": observed_at.isoformat(),
        "provenance": {
            "source": "cruz_verde",
            "data_origin": "published",
            "parser_version": CONNECTOR_VERSION,
            "link_source": link_source,
        },
        "match": {"level": "not_evaluated", "causes": []},
    }


def _prices(hit: dict[str, Any]) -> tuple[dict[str, str] | None, dict[str, str] | None, str | None]:
    prices = hit.get("prices") if isinstance(hit.get("prices"), dict) else {}
    normal_amount = _amount(prices.get("price-list-cl"))
    sale_amount = _amount(prices.get("price-sale-cl"))

    if normal_amount is None:
        # Sin precio de lista solo hay un importe publicado. Se presenta como referencia normal
        # para no inventar una oferta que la fuente no declaró.
        normal = _money(sale_amount)
        promotional = None
    else:
        normal = _money(normal_amount)
        promotional = (
            _money(sale_amount)
            if sale_amount is not None and sale_amount < normal_amount
            else None
        )

    conditions = None
    applied = hit.get("appliedPromotions")
    sale_applied = applied.get("price-sale-cl") if isinstance(applied, dict) else None
    if promotional is not None and isinstance(sale_applied, dict):
        conditions = _text(sale_applied.get("calloutMsg"))
    return normal, promotional, conditions


_PRESENTATION_RE = re.compile(
    r"(?P<qty>\d{1,4}(?:[.,]\d+)?)\s*"
    r"(?P<unit>"
    r"comprimidos?(?:\s+recubiertos?)?"
    r"|c[aá]psulas?(?:\s+(?:blandas?|duras?))?"
    r"|tabletas?(?:\s+recubiertas?)?"
    r"|sobres?"
    r"|supositorios?"
    r"|unidades?"
    r"|mililitros?"
    r"|ml"
    r"|gramos?"
    r"|gr"
    r"|g"
    r")\b",
    re.IGNORECASE,
)


def parse_presentation(name: str) -> str | None:
    """Contenido del envase cuando el nombre lo publica de forma inequívoca.

    Solo reconoce cantidades seguidas de una forma farmacéutica o una medida de contenido. No usa
    dosis (`mg`, `%`, `mg/mL`) ni cantidades de una concentración; si hay una multiplicación
    (`2 x 20 comprimidos`) prefiere no presentar nada antes que confundir el total.
    """
    if not isinstance(name, str) or not name.strip():
        return None

    candidatos: list[re.Match[str]] = []
    for match in _PRESENTATION_RE.finditer(name):
        previous = name[: match.start()].rstrip()
        if previous.endswith(("/", "x", "×")):
            continue
        qty = match.group("qty").replace(",", ".")
        try:
            cantidad = Decimal(qty)
        except InvalidOperation:
            continue
        if cantidad <= 0:
            continue
        candidatos.append(match)
    if not candidatos:
        return None

    presentation = " ".join(candidatos[-1].group(0).split())
    return presentation if len(presentation) <= 40 else None


_FRACTIONAL_RE = re.compile(
    r"^\s*precio por unidad fraccionada\s*:\s*\$\s*"
    r"(?P<amount>[\d.]+)\s+por\s+(?P<unit>.+?)\s*$",
    re.IGNORECASE,
)
_FRACTIONAL_UNIT_RE = re.compile(
    r"^(?:\d+\s+)?"
    r"(?:comprimidos?|c[aá]psulas?|supositorios?|sobres?|tabletas?|unidades?"
    r"|mililitros?|ml|gramos?|gr|g)$",
    re.IGNORECASE,
)


def parse_fractional(pum: Any) -> dict[str, str] | None:
    """Precio fraccionado publicado por unidad o por medida (100 mL, 100 g, etc.).

    La fuente ya lo declara como fraccionado. Solo se acepta si el importe usa formato chileno y
    la unidad es una unidad reconocible; el texto de la unidad se conserva («Capsula», «100 mL»)
    para que nunca se confunda con el precio del envase completo.
    """
    if not isinstance(pum, str):
        return None
    match = _FRACTIONAL_RE.match(pum)
    if match is None:
        return None
    amount = _clp_amount(match.group("amount"))
    if amount is None:
        return None
    unit = " ".join(match.group("unit").split()).strip(" .;,-")
    if not unit or len(unit) > 40 or _FRACTIONAL_UNIT_RE.match(unit) is None:
        return None
    return {"amount": _money(amount)["amount"], "currency": CURRENCY, "per_unit": unit}


def _normalized_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value)
    plain = "".join(ch for ch in decomposed if not (0x300 <= ord(ch) <= 0x36F))
    return "".join(ch for ch in plain.casefold() if ch.isalnum())


def _tab_laboratory(tabs: Any) -> str | None:
    if not isinstance(tabs, list):
        return None
    for tab in tabs:
        content = tab.get("content") if isinstance(tab, dict) else None
        if not isinstance(content, str):
            continue
        unescaped = html_lib.unescape(content)
        match = re.search(
            r"LABORATORIO\s*:\s*(?:<[^>]*>\s*)*([^<]+)", unescaped, re.IGNORECASE
        )
        if match is None:
            continue
        value = " ".join(match.group(1).split()).strip(" .;,-")
        if value:
            return value[:80]
    return None


def parse_detail_laboratory(payload: dict[str, Any]) -> str | None:
    """Laboratorio explícito de la ficha, solo si difiere de la marca comercial.

    En medicamentos el campo `laboratory` repite la marca; en cuidado personal suele traer el
    fabricante. La pestaña `LABORATORIO` tiene prioridad. Si el valor explícito es igual a la
    marca, se devuelve `None` para no duplicar la marca como laboratorio.
    """
    product = payload.get("productData")
    if not isinstance(product, dict):
        return None
    brand = _text(product.get("brand"))
    tab = _tab_laboratory(product.get("tabs"))
    if tab is not None:
        if brand and _normalized_name(tab) == _normalized_name(brand):
            return None
        return tab
    laboratory = _text(product.get("laboratory"))
    if laboratory and brand and _normalized_name(laboratory) == _normalized_name(brand):
        return None
    return laboratory


class LaboratoryCache:
    """Caché por producto: valor publicado o ausencia confirmada, con vencimiento y tope."""

    def __init__(
        self,
        *,
        ttl_seconds: int = LABORATORY_CACHE_TTL_SECONDS,
        max_entries: int = LABORATORY_CACHE_MAX_ENTRIES,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max(1, max_entries)
        self._entries: dict[str, tuple[float, str | None]] = {}

    def get(self, product_id: str) -> tuple[bool, str | None]:
        entry = self._entries.get(product_id)
        if entry is None:
            return False, None
        expires_at, laboratory = entry
        if time.monotonic() >= expires_at:
            self._entries.pop(product_id, None)
            return False, None
        return True, laboratory

    def set(self, product_id: str, laboratory: str | None) -> None:
        if not product_id:
            return
        self._entries[product_id] = (
            time.monotonic() + self.ttl_seconds,
            laboratory,
        )
        while len(self._entries) > self.max_entries:
            oldest = next(iter(self._entries))
            del self._entries[oldest]

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


laboratory_cache = LaboratoryCache()
session = AnonymousSession()


async def enrich_laboratory(
    products: list[dict[str, Any]],
    *,
    base_url: str = DEFAULT_BASE_URL,
    login_url: str = DEFAULT_LOGIN_URL,
    session_manager: AnonymousSession | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    concurrency: int = DEFAULT_LABORATORY_CONCURRENCY,
    budget_seconds: float = DEFAULT_ENRICHMENT_BUDGET_SECONDS,
    cache: LaboratoryCache | None = None,
) -> None:
    """Completa el laboratorio publicado sin invalidar la búsqueda si una ficha falla.

    Modifica las fichas en sitio. La etapa tiene su propio presupuesto: si una ficha tarda, se
    cancela y el producto conserva `laboratory_as_published = None`. Los errores transitorios no
    se cachean; una ausencia confirmada sí.
    """
    if not products:
        return
    cache = cache if cache is not None else laboratory_cache
    manager = session_manager if session_manager is not None else session

    objetivos: dict[str, list[dict[str, Any]]] = {}
    for product in products:
        product_id = _identifier(product.get("chain_item_id"))
        if not product_id:
            continue
        found, laboratory = cache.get(product_id)
        if found:
            product["laboratory_as_published"] = laboratory
            continue
        objetivos.setdefault(product_id, []).append(product)

    if not objetivos:
        return

    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def consultar(product_id: str) -> tuple[str, str | None, bool]:
        async with semaphore:
            try:
                payload = await fetch_product_detail(
                    product_id,
                    base_url=base_url,
                    login_url=login_url,
                    session=manager,
                    timeout_seconds=timeout_seconds,
                )
            except Exception:
                return product_id, None, False
            laboratory = parse_detail_laboratory(payload)
            cache.set(product_id, laboratory)
            return product_id, laboratory, True

    tasks = {
        asyncio.create_task(consultar(product_id)): product_id
        for product_id in objetivos
    }
    done, pending = await asyncio.wait(tasks, timeout=max(0.0, budget_seconds))
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    for task in done:
        try:
            product_id, laboratory, ok = task.result()
        except Exception:
            continue
        if not ok:
            continue
        for product in objetivos[product_id]:
            product["laboratory_as_published"] = laboratory


class ProductSitemap:
    """Mapa productId → URL canónica, leído del sitemap público y cacheado en memoria."""

    def __init__(
        self,
        *,
        ttl_seconds: int = SITEMAP_TTL_SECONDS,
        failure_ttl_seconds: int = SITEMAP_FAILURE_TTL_SECONDS,
    ):
        self.ttl_seconds = ttl_seconds
        self.failure_ttl_seconds = failure_ttl_seconds
        self._by_id: dict[str, str] = {}
        self._next_refresh = 0.0
        self._loaded_once = False
        self._lock = asyncio.Lock()

    def lookup(self, product_id: str) -> str | None:
        return self._by_id.get(product_id)

    def _fresh(self) -> bool:
        return self._loaded_once and time.monotonic() < self._next_refresh

    async def ensure_loaded(
        self, *, index_url: str = DEFAULT_SITEMAP_URL, timeout_seconds: int
    ) -> None:
        if self._fresh():
            return
        async with self._lock:
            if self._fresh():
                return
            try:
                index = await _fetch_text(index_url, timeout_seconds=timeout_seconds)
                locations = [
                    location.strip()
                    for location in re.findall(
                        r"<loc>\s*([^<]+?)\s*</loc>", index, re.IGNORECASE
                    )
                ]
                product_locations = [
                    location
                    for location in locations
                    if "-product" in location.lower() and location.lower().endswith(".xml")
                ]
                if not product_locations:
                    product_locations = [index_url]
                documents = await asyncio.gather(
                    *(
                        _fetch_text(location, timeout_seconds=timeout_seconds)
                        for location in product_locations
                    ),
                    return_exceptions=True,
                )
                by_id: dict[str, str] = {}
                for document in documents:
                    if isinstance(document, BaseException):
                        continue
                    for match in re.finditer(
                        r"<loc>\s*(https://(?:www\.)?cruzverde\.cl/[^<\s]*?/(\d+)\.html)"
                        r"\s*</loc>",
                        document,
                        re.IGNORECASE,
                    ):
                        by_id[match.group(2)] = match.group(1)
                if not by_id:
                    raise CruzVerdeFetchError("El sitemap de Cruz Verde no entregó productos.")
                self._by_id = by_id
                self._next_refresh = time.monotonic() + self.ttl_seconds
            except Exception:
                logger.warning("E15: no se pudo leer el sitemap de Cruz Verde.")
                # Se conserva el mapa anterior, si existe, y se reintenta más tarde.
                self._next_refresh = time.monotonic() + self.failure_ttl_seconds
            finally:
                self._loaded_once = True

    def clear(self) -> None:
        self._by_id.clear()
        self._next_refresh = 0.0
        self._loaded_once = False

    def __len__(self) -> int:
        return len(self._by_id)


sitemap = ProductSitemap()


async def _fetch_text(url: str, *, timeout_seconds: int) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CruzVerdeFetchError("La URL del sitemap no es válida.")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise CruzVerdeFetchError("El sitemap de Cruz Verde no respondió.") from exc
    if len(response.content) > MAX_SITEMAP_BYTES:
        raise CruzVerdeFetchError("El sitemap de Cruz Verde excede el tamaño permitido.")
    return response.text


def _apply_links(products: list[dict[str, Any]], index: ProductSitemap) -> None:
    for product in products:
        product_id = _identifier(product.get("chain_item_id"))
        if not product_id:
            continue
        canonical = index.lookup(product_id)
        if canonical:
            product["source_url"] = canonical
            product["provenance"]["link_source"] = "sitemap"


async def search_products(
    query: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    login_url: str = DEFAULT_LOGIN_URL,
    sitemap_url: str = DEFAULT_SITEMAP_URL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    budget_seconds: float = DEFAULT_ENRICHMENT_BUDGET_SECONDS,
    limit: int = DEFAULT_LIMIT,
    session_manager: AnonymousSession | None = None,
    sitemap_index: ProductSitemap | None = None,
    observed_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Busca, normaliza y enriquece las publicaciones con lo disponible dentro del presupuesto."""
    if not query.strip():
        raise CruzVerdeFetchError("La consulta está vacía.")
    manager = session_manager if session_manager is not None else session
    index = sitemap_index if sitemap_index is not None else sitemap
    started = time.monotonic()

    # La búsqueda también consume el presupuesto de la fuente: si la API tarda más que el
    # presupuesto, se corta aquí en vez de esperar el tiempo de red máximo y bloquear E15.
    search_timeout = max(1, min(int(timeout_seconds), int(budget_seconds)))
    payload = await fetch_search_json(
        query,
        base_url=base_url,
        login_url=login_url,
        session=manager,
        timeout_seconds=search_timeout,
        limit=limit,
    )
    products = parse_search_results(
        payload,
        limit=limit,
        observed_at=observed_at,
        link_for=index.lookup,
    )

    remaining = max(0.0, budget_seconds - (time.monotonic() - started))
    if products and remaining > 0:
        deadline = time.monotonic() + remaining
        detail_task = asyncio.create_task(
            enrich_laboratory(
                products,
                base_url=base_url,
                login_url=login_url,
                session_manager=manager,
                timeout_seconds=timeout_seconds,
                budget_seconds=remaining,
            )
        )
        sitemap_task = asyncio.create_task(
            index.ensure_loaded(index_url=sitemap_url, timeout_seconds=timeout_seconds)
        )
        # Las fichas de detalle son el enriquecimiento principal. El sitemap corre en paralelo y,
        # si no alcanza a cargarse, los productos se devuelven con el enlace de respaldo: no se
        # bloquea la respuesta por un enlace canónico.
        done, pending = await asyncio.wait({detail_task}, timeout=remaining)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*done, return_exceptions=True)

        grace = min(SITEMAP_GRACE_SECONDS, max(0.0, deadline - time.monotonic()))
        if not sitemap_task.done():
            await asyncio.wait({sitemap_task}, timeout=grace)
        if not sitemap_task.done():
            sitemap_task.cancel()
        await asyncio.gather(sitemap_task, return_exceptions=True)

    # Si el sitemap ya estaba en caché (o alcanzó a cargarse), se prefiere la URL canónica.
    _apply_links(products, index)
    return products
