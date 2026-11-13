"""E15 — conector de Farmacias Ahumada: parser HTML y fuente en vivo gateada.

El parser es puro: recibe HTML ya obtenido y devuelve fichas publicadas, sin red ni base de datos.
La fuente en vivo está **apagada por defecto** y solo se consulta cuando la operadora configura
`AHUMADA_LIVE_SEARCH_ENABLED=true` y `AHUMADA_LIVE_SEARCH_URL` apuntando a una fuente cuyo uso
esté autorizado por escrito (convenio, espejo o feed licenciado). Sin esa declaración explícita,
E15 informa `not_configured` y **no se hace ninguna solicitud**.

Reglas del parser:

* Cada ficha se asocia a su propio `data-pid`; nunca se mezclan precios de tarjetas anidadas.
* El precio por comprimido/unidad se publica aparte (`fractional`) y **nunca** se usa como precio
  del envase.
* Un importe ausente queda en `None`: no se inventa ni se completa con el de un producto vecino.
* Los importes se leen del texto publicado y se conservan como texto decimal (CLP), nunca float.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.services.connectors.matching import classify_match

AHUMADA_BASE_URL = "https://www.farmaciasahumada.cl"
CURRENCY = "CLP"
PARSER_VERSION = "ahumada-html-2"
MAX_RESULTS = 24
MAX_HTML_BYTES = 2_000_000
DEFAULT_TIMEOUT_SECONDS = 10

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

# Importes CLP: el punto separa miles ("$1.309"). También se acepta sin separador ("$618").
_PRICE_RE = re.compile(r"\$\s*(\d{1,3}(?:[.,]\d{3})+|\d+)")
# Solo estas unidades convierten un importe en precio fraccionado. "por caja" sigue siendo envase.
# El marcador debe seguir de inmediato al importe; si se busca en una ventana amplia, el
# "$82 por comprimido" de una línea posterior contaminaría el "$1.309" del envase.
# Ahumada publica el fraccionado como "$82 x Comp." (con "x" y abreviaturas).
_PER_UNIT_RE = re.compile(
    r"^\s*[(]?\s*(?:por|/|x)\s*"
    r"(comprimidos?|comps?\.?|cápsulas?|capsulas?|caps?\.?|tabletas?|tabs?\.?|"
    r"unidades?|un\.?|dosis|ampollas?|sobres?|sachets?)",
    re.IGNORECASE,
)
# Precio por unidad de medida (volumen o peso): «$12.900 por litro», «$2.190 por 100 ml».
# Es un precio de referencia, nunca el precio del envase. A diferencia de las unidades contables,
# aquí el marcador debe ser «por» o «/»: «x 15 ml» describe la presentación, no una razón.
_PER_MEASURE_RE = re.compile(
    r"^\s*[(]?\s*(?:por|/)\s*(?P<cantidad>\d+(?:[.,]\d+)?)?\s*"
    r"(?P<unidad>litros?|lts?\.?|l|mililitros?|mls?\.?|cc|kilos?|kgs?\.?|kg|gramos?|grs?\.?|g)"
    r"(?![a-záéíóúñ])",
    re.IGNORECASE,
)
_UNIT_ALIASES = {
    "comp": "comprimido",
    "comp.": "comprimido",
    "caps": "cápsula",
    "caps.": "cápsula",
    "cap": "cápsula",
    "cap.": "cápsula",
    "tab": "tableta",
    "tab.": "tableta",
    "un": "unidad",
    "un.": "unidad",
    # Unidades de medida: se normalizan para que la ficha muestre «por litro», no «por L».
    "litro": "litro",
    "litros": "litro",
    "lt": "litro",
    "lts": "litro",
    "lt.": "litro",
    "lts.": "litro",
    "l": "litro",
    "mililitro": "ml",
    "mililitros": "ml",
    "ml": "ml",
    "mls": "ml",
    "ml.": "ml",
    "mls.": "ml",
    "cc": "ml",
    "kilo": "kg",
    "kilos": "kg",
    "kg": "kg",
    "kgs": "kg",
    "kg.": "kg",
    "kgs.": "kg",
    "gramo": "g",
    "gramos": "g",
    "g": "g",
    "gr": "g",
    "grs": "g",
    "gr.": "g",
    "grs.": "g",
}
_SKIP_PRICE_RE = re.compile(r"\b(cuotas?|meses|paga en)\b", re.IGNORECASE)

_NAME_CLASSES = ("product-name", "tile-name", "product-title", "pdp-link")
# En las fichas de Ahumada el laboratorio se publica en el bloque de marca de la tarjeta
# ("product-tile-brand": Andromaco S.A., Eurofarma Chile, Valma, Pasteur…).
_LAB_CLASSES = ("laboratory", "manufacturer", "fabricante", "product-tile-brand")
_PRESENTATION_CLASSES = ("presentation", "presentacion")
_DESCRIPTION_CLASSES = ("product-description", "short-description")
_CONDITION_CLASSES = ("promo-condition", "promotion", "promocion", "promo-text", "promo-label")
_OLD_HINTS = {"old", "before", "regular", "normal", "list", "strike", "strikethrough", "was",
              "original", "tachado", "precio-tachado"}
_PROMO_HINTS = {"promo", "promotional", "promocion", "offer", "oferta", "sale", "now", "final",
                "discount", "descuento", "internet", "cmr", "club"}

# La presentación no tiene bloque propio en la ficha: se toma del nombre publicado, que la
# escribe como "x 16 Comprimidos", "18 Comprimidos" o "16 comp.".
_PRESENTATION_IN_NAME = re.compile(
    # Volumen o peso precedido de "x": "x 15 mL" es presentación; "160mg/5ml" es concentración.
    r"\bx\s*\d+(?:[.,]\d+)?\s*(?:ml|g|mg)\b"
    # En jarabes y gotas el volumen va después de la forma farmacéutica, sin "x".
    r"|\b(?:jarabe|gotas|solución|suspensión)\s+\d+(?:[.,]\d+)?\s*(?:ml|g|mg)\b"
    # "Tubo x 10" es la presentación publicada cuando no se indica la unidad contable.
    r"|\btubo\s*x\s*\d+\b"
    r"|(?:\bx\s*)?\d+(?:[.,]\d+)?\s*"
    r"(?:comprimidos?|comps?\.?|cápsulas?|capsulas?|caps?\.?|tabletas?|tabs?\.?|"
    r"unidades?|sobres?|sachets?|ampollas?|supositorios?|grageas?|parches?|óvulos?)",
    re.IGNORECASE,
)


class AhumadaFetchError(RuntimeError):
    """La fuente en vivo no entregó un HTML utilizable. Nunca se convierte en «sin resultados»."""


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None
    children: list[_Node] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)


class _TreeBuilder(HTMLParser):
    """Árbol mínimo suficiente para asociar cada precio a su tarjeta.

    No se usa un parser externo: la extracción es acotada y el backend no incorpora dependencias
    nuevas. Las etiquetas mal cerradas se resuelven cerrando hasta la coincidencia más cercana.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", {}, None)
        self._stack: list[_Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k.lower(): (v or "") for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._stack[-1].text_parts.append(data)


def _parse_html(html_text: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(html_text)
    builder.close()
    return builder.root


def _collapse(value: str) -> str:
    return " ".join(value.split())


def _classes(node: _Node) -> set[str]:
    return {token for token in node.attrs.get("class", "").lower().split() if token}


def _has_class_hint(node: _Node, hints: tuple[str, ...]) -> bool:
    tokens = _classes(node)
    return any(hint in token for token in tokens for hint in hints)


def _walk(node: _Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _owned_walk(node: _Node, pid: str):
    """Recorre la tarjeta sin entrar en otra tarjeta anidada con un `data-pid` distinto."""
    for child in node.children:
        child_pid = child.attrs.get("data-pid", "").strip()
        if child_pid and child_pid != pid:
            continue
        yield child
        yield from _owned_walk(child, pid)


def _owned_text(node: _Node, pid: str) -> str:
    """Texto de la tarjeta sin el de un producto relacionado anidado."""
    parts = list(node.text_parts)
    for child in node.children:
        child_pid = child.attrs.get("data-pid", "").strip()
        if child_pid and child_pid != pid:
            continue
        parts.append(_owned_text(child, pid))
    return _collapse(" ".join(parts))


def _belongs_to_another_product(node: _Node, pid: str) -> bool:
    """True si algún ancestro de `node` declara un `data-pid` distinto."""
    parent = node.parent
    while parent is not None:
        other = parent.attrs.get("data-pid", "").strip()
        if other and other != pid:
            return True
        parent = parent.parent
    return False


def _tile_root(node: _Node, pid: str) -> _Node:
    """Asciende al contenedor `product-tile` del producto, sin cruzar a otro `data-pid`.

    Una tarjeta relacionada puede estar anidada dentro de la tarjeta principal; en ese caso no
    se asciende, porque el contenedor pertenece al otro producto.
    """
    current = node
    while current.parent is not None:
        parent = current.parent
        own_pid = parent.attrs.get("data-pid", "").strip()
        if own_pid and own_pid != pid:
            break
        if not any("product-tile" in token for token in _classes(parent)):
            break
        if _belongs_to_another_product(parent, pid):
            break
        current = parent
    return current


def _tiles(root: _Node):
    seen: set[str] = set()
    for node in _walk(root):
        pid = node.attrs.get("data-pid", "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        yield _tile_root(node, pid), pid


def _first_text(node: _Node, pid: str, hints: tuple[str, ...]) -> str | None:
    for candidate in _owned_walk(node, pid):
        if _has_class_hint(candidate, hints):
            value = _owned_text(candidate, pid)
            if value:
                return value
    return None


def _name(node: _Node, pid: str) -> str | None:
    value = _first_text(node, pid, _NAME_CLASSES)
    if value:
        return value
    for candidate in _owned_walk(node, pid):
        if candidate.tag in {"h2", "h3", "h4"}:
            value = _owned_text(candidate, pid)
            if value:
                return value
    # El enlace de la ficha es el nombre publicado. Los badges (por ejemplo «Schedule Badge»)
    # no son el producto y suelen aparecer antes en el árbol.
    for candidate in _owned_walk(node, pid):
        if candidate.tag == "a":
            value = _owned_text(candidate, pid)
            if value and not _PRICE_RE.search(value):
                return value
    for candidate in _owned_walk(node, pid):
        if candidate.tag == "a":
            for attr in ("title", "aria-label"):
                value = _collapse(candidate.attrs.get(attr, ""))
                if value:
                    return value
    tile_images = [
        candidate
        for candidate in _owned_walk(node, pid)
        if candidate.tag == "img" and "tile-image" in _classes(candidate)
    ]
    for image in tile_images:
        for attr in ("alt", "title"):
            value = _collapse(image.attrs.get(attr, ""))
            if value:
                return value
    for candidate in _owned_walk(node, pid):
        if candidate.tag == "img" and _has_class_hint(candidate, ("badge",)):
            continue
        if candidate.tag == "img":
            value = _collapse(candidate.attrs.get("alt", ""))
            if value:
                return value
    return None


def _source_url(node: _Node, pid: str) -> str | None:
    candidates: list[tuple[int, str]] = []
    for candidate in _owned_walk(node, pid):
        if candidate.tag != "a":
            continue
        href = candidate.attrs.get("href", "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(AHUMADA_BASE_URL, href)
        if urlparse(absolute).scheme not in {"http", "https"}:
            continue
        score = 0
        if pid and pid in absolute:
            score += 2
        if re.search(r"-\d+\.html(?:$|[?#])", absolute):
            score += 1
        candidates.append((score, absolute))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    # Solo se acepta un enlace atribuible al producto (contiene su `data-pid` o el patrón de
    # ficha). Un enlace de categoría no es la URL oficial de esta tarjeta.
    return candidates[0][1] if candidates[0][0] > 0 else None


def _image_url(node: _Node, pid: str) -> str | None:
    images = [candidate for candidate in _owned_walk(node, pid) if candidate.tag == "img"]
    ordered = [image for image in images if "tile-image" in _classes(image)] + images
    for image in ordered:
        raw = (
            image.attrs.get("src")
            or image.attrs.get("data-src")
            or image.attrs.get("data-lazy-src")
            or ""
        ).strip()
        if not raw:
            srcset = image.attrs.get("srcset", "").strip()
            raw = srcset.split(",")[0].split(" ")[0] if srcset else ""
        if not raw:
            continue
        absolute = urljoin(AHUMADA_BASE_URL, raw)
        if urlparse(absolute).scheme in {"http", "https"}:
            return absolute
    return None


def _labelled_text(node: _Node, pid: str, label: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(label)}\s*:\s*(.+)$", re.IGNORECASE)
    for candidate in _owned_walk(node, pid):
        if candidate.children:
            continue
        value = _owned_text(candidate, pid)
        match = pattern.match(value)
        if match:
            return _collapse(match.group(1))
    return None


def _definition_value(node: _Node, pid: str, label: str) -> str | None:
    for element in _owned_walk(node, pid):
        if element.tag != "dl":
            continue
        terms = [child for child in element.children if child.tag == "dt"]
        values = [child for child in element.children if child.tag == "dd"]
        for term, value in zip(terms, values, strict=False):
            if _owned_text(term, pid).lower().startswith(label.lower()):
                text = _owned_text(value, pid)
                if text:
                    return text
    return None


def _laboratory(node: _Node, pid: str) -> str | None:
    return (
        _first_text(node, pid, _LAB_CLASSES)
        or _labelled_text(node, pid, "Laboratorio")
        or _definition_value(node, pid, "Laboratorio")
    )


def _presentation(node: _Node, pid: str, name: str | None) -> str | None:
    value = (
        _first_text(node, pid, _PRESENTATION_CLASSES)
        or _labelled_text(node, pid, "Presentación")
        or _labelled_text(node, pid, "Presentacion")
        or _definition_value(node, pid, "Presentación")
        or _definition_value(node, pid, "Presentacion")
    )
    if value:
        return value
    if name:
        match = _PRESENTATION_IN_NAME.search(name)
        if match:
            return _collapse(match.group(0))
    return None


def _meta_price(node: _Node, pid: str) -> str | None:
    for candidate in _owned_walk(node, pid):
        if candidate.tag != "meta" or candidate.attrs.get("itemprop", "").lower() != "price":
            continue
        digits = re.sub(r"\D", "", candidate.attrs.get("content", ""))
        if digits and int(digits) > 0:
            return str(int(digits))
    return None


def _description(node: _Node, pid: str, name: str | None) -> str | None:
    value = _first_text(node, pid, _DESCRIPTION_CLASSES)
    if value and value != name:
        return value
    return None


def _fractional_unit(after: str) -> str | None:
    """Unidad de una razón publicada justo después de un importe («$82 x Comp.», «$12.900 por litro»).

    Devuelve `None` cuando el importe es del envase. El marcador debe seguir de inmediato al
    importe: «x 15 ml» de la presentación no convierte el precio en una razón.
    """
    count = _PER_UNIT_RE.search(after)
    if count:
        unit = _collapse(count.group(1)).lower()
        return _UNIT_ALIASES.get(unit, unit)
    measure = _PER_MEASURE_RE.search(after)
    if measure:
        unit = _collapse(measure.group("unidad")).lower()
        unit = _UNIT_ALIASES.get(unit, unit)
        quantity = (measure.group("cantidad") or "").strip()
        return f"{quantity} {unit}".strip() if quantity else unit
    return None


def _amounts(text: str) -> list[tuple[str, str | None]]:
    """Devuelve `(importe, unidad_fraccionada)` por cada importe publicado en el texto."""
    found: list[tuple[str, str | None]] = []
    for match in _PRICE_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(1))
        if not digits or int(digits) == 0:
            continue
        amount = str(int(digits))
        after = text[match.end() : match.end() + 50]
        found.append((amount, _fractional_unit(after)))
    return found


def _price_units(node: _Node, pid: str) -> list[tuple[_Node, list[tuple[str, str | None]]]]:
    """Agrupa cada importe con el ancestro más alto que no mezcla varios precios.

    Así se conserva la etiqueta («precio anterior», «precio promocional») sin que un contenedor
    con dos precios distintos contamine la clasificación.
    """
    units: list[tuple[_Node, list[tuple[str, str | None]]]] = []
    seen: set[int] = set()
    for candidate in _owned_walk(node, pid):
        values = _amounts(_owned_text(candidate, pid))
        if not values:
            continue
        top = candidate
        while top.parent is not None and top.parent is not node:
            parent_values = _amounts(_owned_text(top.parent, pid))
            if len(parent_values) != len(values):
                break
            top = top.parent
        if id(top) in seen:
            continue
        seen.add(id(top))
        units.append((top, _amounts(_owned_text(top, pid))))
    return units


def _price_kind(node: _Node) -> str | None:
    hint = node.attrs.get("data-price-type", "").strip().lower()
    label = f"{node.attrs.get('aria-label', '')} {node.attrs.get('title', '')}".lower()
    # La comparación por subcadena reconoce clases reales como «sales» o
    # «promotion-badge-container», que no coinciden con la palabra exacta.
    if (
        node.tag in {"del", "s"}
        or _has_class_hint(node, tuple(_OLD_HINTS))
        or hint in _OLD_HINTS
    ):
        return "normal"
    if _has_class_hint(node, tuple(_PROMO_HINTS)) or hint in _PROMO_HINTS:
        return "promotional"
    if any(word in label for word in _OLD_HINTS):
        return "normal"
    if any(word in label for word in _PROMO_HINTS):
        return "promotional"
    return None


def _money(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": CURRENCY}


def _conditions(node: _Node, pid: str) -> str | None:
    for candidate in _owned_walk(node, pid):
        for attr in ("data-promotion", "data-promo", "data-condition"):
            value = _collapse(candidate.attrs.get(attr, ""))
            if value:
                return value
        if _has_class_hint(candidate, _CONDITION_CLASSES):
            value = _owned_text(candidate, pid)
            if value and not _PRICE_RE.search(value):
                return value
    return None


def _product(node: _Node, pid: str, observed_at: datetime) -> dict[str, Any] | None:
    name = _name(node, pid)
    source_url = _source_url(node, pid)
    if not name or not source_url:
        return None

    normal: list[str] = []
    promotional: list[str] = []
    neutral: list[str] = []
    fractional: tuple[str, str | None] | None = None

    for unit, values in _price_units(node, pid):
        if _SKIP_PRICE_RE.search(_owned_text(unit, pid)):
            continue
        kind = _price_kind(unit)
        package_amounts: list[str] = []
        for amount, per_unit in values:
            if per_unit:
                if fractional is None:
                    fractional = (amount, per_unit)
                continue
            package_amounts.append(amount)
        if kind == "normal":
            normal.extend(package_amounts)
        elif kind == "promotional":
            promotional.extend(package_amounts)
        else:
            neutral.extend(package_amounts)

    normal_price: str | None = normal[0] if normal else None
    promo_price: str | None = promotional[0] if promotional else None

    # Sin etiquetas explícitas: un único importe es el precio publicado del envase. Si hay dos,
    # el mayor es el precio normal y el menor el promocional; ambos salen del mismo texto.
    if normal_price is None and promo_price is None and neutral:
        unique = list(dict.fromkeys(neutral))
        normal_price = max(unique, key=int)
        if len(unique) > 1:
            promo_price = min(unique, key=int)
    elif normal_price is None and neutral:
        larger = [amount for amount in neutral if promo_price is None or int(amount) > int(promo_price)]
        if larger:
            normal_price = max(larger, key=int)
    elif promo_price is None and neutral and normal_price is not None:
        smaller = [amount for amount in neutral if int(amount) < int(normal_price)]
        if smaller:
            promo_price = min(smaller, key=int)
    if normal_price is None and promo_price is None:
        # Algunas fichas publican el precio solo en metadatos estructurados.
        normal_price = _meta_price(node, pid)

    return {
        "chain_item_id": pid,
        "published_name": name,
        "laboratory_as_published": _laboratory(node, pid),
        "presentation_as_published": _presentation(node, pid, name),
        "description": _description(node, pid, name),
        "image_url": _image_url(node, pid),
        "price": {
            "normal": _money(normal_price) if normal_price else None,
            "promotional": _money(promo_price) if promo_price else None,
            "fractional": (
                {**_money(fractional[0]), "per_unit": fractional[1]} if fractional else None
            ),
        },
        "conditions": _conditions(node, pid),
        "source_url": source_url,
        "observed_at": observed_at.isoformat(),
        "provenance": {
            "source": "farmacias_ahumada",
            "data_origin": "published",
            "parser_version": PARSER_VERSION,
        },
        "match": {"level": "not_evaluated", "causes": []},
    }


def parse_search_results(
    html_text: str,
    *,
    observed_at: datetime | None = None,
    limit: int = MAX_RESULTS,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """Extrae las fichas publicadas de una página de resultados de Ahumada.

    No hace red ni persiste nada. Una tarjeta sin nombre o sin URL oficial se omite en lugar de
    completarse con datos de otro producto.

    Con `query` cada ficha declara su relación con lo pedido (`direct` o `related` + causa). La
    página de resultados de Ahumada puede ser una vitrina de destacados cuando la consulta no
    existe: si **ninguna** ficha confirma la consulta, la página no es un resultado y se devuelve
    vacío. Si al menos una confirma, se conservan las que confirman y las que solo coinciden en
    parte (se presentan como `related` y la pantalla las rotula «Coincidencia no confirmada»).
    Las fichas que no confirman nada de la consulta se descartan: son relleno de la vitrina, no
    resultados. Una coincidencia parcial por sí sola tampoco convierte la página en resultado.
    """
    moment = observed_at or datetime.now(UTC)
    root = _parse_html(html_text)
    results: list[dict[str, Any]] = []
    for node, pid in _tiles(root):
        product = _product(node, pid, moment)
        if product is None:
            continue
        if query:
            relation, causes = classify_match(query, product["published_name"])
            product["match"] = {"level": relation, "causes": causes}
        results.append(product)
        if len(results) >= limit:
            break
    if query:
        # Sin ninguna coincidencia directa la página entera es una vitrina. Con al menos una,
        # se descarta el relleno (ni siquiera comparte un término) y se conservan las parciales.
        if not any(item["match"]["level"] == "direct" for item in results):
            return []
        results = [
            item
            for item in results
            if item["match"]["level"] == "direct" or "partial_query_match" in item["match"]["causes"]
        ]
    return results


async def fetch_search_html(
    query: str,
    *,
    url: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Obtiene el HTML de una fuente **autorizada**. Sin autorización no se llama a esta función.

    No simula un navegador, no reutiliza cookies y no sigue redirecciones: es una consulta directa
    a la URL configurada por la operadora. Un fallo se propaga como `AhumadaFetchError` para que
    E15 lo informe como `failed`, nunca como «sin resultados».
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AhumadaFetchError("La fuente configurada no es una URL HTTP(S) válida.")

    # Identificador neutro: el conector no debe declarar una autorización que no acredita por sí
    # mismo. La autorización de uso es una decisión operativa registrada aparte.
    headers = {
        "User-Agent": "CotizadorFarmacia/0.1",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
            response = await client.get(url, params={"q": query}, headers=headers)
            response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise AhumadaFetchError("La fuente autorizada no respondió correctamente.") from exc

    if len(response.content) > MAX_HTML_BYTES:
        raise AhumadaFetchError("La respuesta de la fuente autorizada excede el tamaño permitido.")
    return response.content.decode(response.encoding or "utf-8", errors="replace")
