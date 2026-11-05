"""E15 — fabricante publicado en la ficha de Salcobrand (sin red).

El fabricante se lee solo de `manufacturer.name` en el JSON-LD. La marca nunca se usa como
respaldo, una ficha rota no invalida la búsqueda y la caché evita repetir consultas por SKU.
"""

import asyncio
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import salcobrand
from app.services.chains import salcobrand as salcobrand_connector

MOMENTO = datetime(2026, 11, 5, 1, 30, tzinfo=UTC)
URL = "https://salcobrand.cl/products/kitadol?default_sku=430924"


def _html(manufacturer: str | None, *, brand: str = "Kitadol") -> str:
    node = {
        "@type": ["Product", "Drug"],
        "name": "Kitadol (B) Paracetamol 500mg 24 Comprimidos",
        "brand": {"@type": "Brand", "name": brand},
    }
    if manufacturer is not None:
        node["manufacturer"] = {"@type": "Organization", "name": manufacturer}
    return (
        '<html><head><script type="application/ld+json">'
        + json.dumps({"@context": "https://schema.org", "@graph": [node]})
        + "</script></head></html>"
    )


def _product(sku: str = "430924", *, url: str = URL) -> dict:
    return {
        "chain_item_id": sku,
        "published_name": "Kitadol (B) Paracetamol 500mg 24 Comprimidos",
        "brand_as_published": "Kitadol",
        "laboratory_as_published": None,
        "presentation_as_published": "24 Comprimidos",
        "price": {
            "normal": {"amount": "1499", "currency": "CLP"},
            "promotional": {"amount": "998", "currency": "CLP"},
            "fractional": None,
        },
        "conditions": "Directo",
        "has_stock": True,
        "source_url": url,
    }


@pytest.fixture(autouse=True)
def _cache_limpia():
    salcobrand.laboratory_cache.clear()
    yield
    salcobrand.laboratory_cache.clear()


def test_extrae_el_fabricante_del_jsonld_y_no_la_marca() -> None:
    assert salcobrand.parse_product_manufacturer(_html("LABORATORIO CHILE")) == (
        "LABORATORIO CHILE"
    )


def test_sin_manufacturer_devuelve_none_aunque_haya_marca() -> None:
    assert salcobrand.parse_product_manufacturer(_html(None, brand="Kitadol")) is None


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<html><body>sin datos estructurados</body></html>",
        '<script type="application/ld+json">no es json</script>',
        '<script type="application/ld+json">{"@graph":[{"@type":"Brand",'
        '"name":"Kitadol","manufacturer":{"name":"NO ES PRODUCTO"}}]}</script>',
    ],
)
def test_html_sin_fabricante_publicado_devuelve_none(html: str) -> None:
    assert salcobrand.parse_product_manufacturer(html) is None


async def test_la_ficha_se_pide_sin_credenciales_de_algolia(monkeypatch) -> None:
    capturado = {}

    class _Respuesta:
        status_code = 200
        content = b"<html></html>"
        text = "<html></html>"

        def raise_for_status(self):
            return None

    class _Cliente:
        def __init__(self, **kwargs):
            capturado["cliente"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            capturado["url"] = url
            capturado["get"] = kwargs
            return _Respuesta()

    monkeypatch.setattr(salcobrand.httpx, "AsyncClient", _Cliente)

    html = await salcobrand.fetch_product_page(url=URL, referer="https://salcobrand.cl/")

    assert html == "<html></html>"
    assert capturado["url"] == URL
    assert capturado["get"]["headers"]["Referer"] == "https://salcobrand.cl/"
    assert not any("algolia" in k.lower() for k in capturado["get"]["headers"])
    assert capturado["cliente"]["follow_redirects"] is False


async def test_una_url_fuera_del_sitio_se_rechaza_sin_consultar(monkeypatch) -> None:
    async def no_consultar(*args, **kwargs):
        raise AssertionError("No debe consultarse una URL ajena.")

    monkeypatch.setattr(salcobrand.httpx, "AsyncClient", no_consultar)

    with pytest.raises(salcobrand.SalcobrandFetchError):
        await salcobrand.fetch_product_page(
            url="https://example.com/products/x", referer="https://salcobrand.cl/"
        )


async def test_enriquece_por_sku_y_un_fallo_no_invalida_las_demas(monkeypatch) -> None:
    productos = [
        _product("430924", url="https://salcobrand.cl/products/kitadol?default_sku=430924"),
        _product("432432", url="https://salcobrand.cl/products/paracetamol?default_sku=432432"),
    ]

    async def fake_fetch(*, url, referer, timeout_seconds):
        if "432432" in url:
            raise salcobrand.SalcobrandFetchError("ficha caída")
        return _html("LABORATORIO CHILE")

    monkeypatch.setattr(salcobrand, "fetch_product_page", fake_fetch)

    await salcobrand.enrich_laboratory(productos, referer="https://salcobrand.cl/")

    assert productos[0]["laboratory_as_published"] == "LABORATORIO CHILE"
    assert productos[0]["brand_as_published"] == "Kitadol"
    assert productos[1]["laboratory_as_published"] is None
    # El fallo transitorio no se cachea: una búsqueda posterior puede recuperarlo.
    encontrado, _ = salcobrand.laboratory_cache.get("432432")
    assert encontrado is False


async def test_la_cache_evita_repetir_la_misma_ficha(monkeypatch) -> None:
    llamadas = []

    async def fake_fetch(*, url, referer, timeout_seconds):
        llamadas.append(url)
        return _html("LABORATORIO CHILE")

    monkeypatch.setattr(salcobrand, "fetch_product_page", fake_fetch)

    primero = [_product()]
    segundo = [_product()]
    await salcobrand.enrich_laboratory(primero, referer="https://salcobrand.cl/")
    await salcobrand.enrich_laboratory(segundo, referer="https://salcobrand.cl/")

    assert len(llamadas) == 1
    assert primero[0]["laboratory_as_published"] == "LABORATORIO CHILE"
    assert segundo[0]["laboratory_as_published"] == "LABORATORIO CHILE"


async def test_la_ausencia_confirmada_tambien_se_cachea(monkeypatch) -> None:
    llamadas = []

    async def fake_fetch(*, url, referer, timeout_seconds):
        llamadas.append(url)
        return _html(None)

    monkeypatch.setattr(salcobrand, "fetch_product_page", fake_fetch)

    await salcobrand.enrich_laboratory([_product()], referer="https://salcobrand.cl/")
    await salcobrand.enrich_laboratory([_product()], referer="https://salcobrand.cl/")

    assert len(llamadas) == 1
    encontrado, manufacturer = salcobrand.laboratory_cache.get("430924")
    assert encontrado is True
    assert manufacturer is None


async def test_una_cache_explicita_aunque_este_vacia_se_respeta(monkeypatch) -> None:
    llamadas = []

    async def fake_fetch(*, url, referer, timeout_seconds):
        llamadas.append(url)
        return _html("LABORATORIO CHILE")

    monkeypatch.setattr(salcobrand, "fetch_product_page", fake_fetch)
    propia = salcobrand.LaboratoryCache()

    await salcobrand.enrich_laboratory(
        [_product()], referer="https://salcobrand.cl/", cache=propia
    )

    assert len(llamadas) == 1
    assert propia.get("430924") == (True, "LABORATORIO CHILE")
    assert salcobrand.laboratory_cache.get("430924") == (False, None)


async def test_una_ausencia_en_cache_no_deja_un_fabricante_anterior(monkeypatch) -> None:
    monkeypatch.setattr(
        salcobrand, "fetch_product_page", lambda **kwargs: asyncio.sleep(0)
    )
    propia = salcobrand.LaboratoryCache()
    propia.set("430924", None)
    producto = _product()
    producto["laboratory_as_published"] = "VALOR ANTERIOR"

    await salcobrand.enrich_laboratory(
        [producto], referer="https://salcobrand.cl/", cache=propia
    )

    assert producto["laboratory_as_published"] is None


def test_la_cache_vence_y_respeta_el_tamano_maximo() -> None:
    vencida = salcobrand.LaboratoryCache(ttl_seconds=0, max_entries=10)
    vencida.set("1", "LAB")
    assert vencida.get("1") == (False, None)

    acotada = salcobrand.LaboratoryCache(ttl_seconds=600, max_entries=2)
    acotada.set("1", "A")
    acotada.set("2", "B")
    acotada.set("3", "C")

    assert acotada.get("1") == (False, None)
    assert acotada.get("2") == (True, "B")
    assert acotada.get("3") == (True, "C")
    assert len(acotada) == 2


async def test_la_concurrencia_esta_limitada(monkeypatch) -> None:
    activos = 0
    maximo = 0
    lock = asyncio.Lock()

    async def fake_fetch(*, url, referer, timeout_seconds):
        nonlocal activos, maximo
        async with lock:
            activos += 1
            maximo = max(maximo, activos)
        await asyncio.sleep(0.01)
        async with lock:
            activos -= 1
        return _html("LABORATORIO CHILE")

    monkeypatch.setattr(salcobrand, "fetch_product_page", fake_fetch)
    productos = [
        _product(str(i), url=f"https://salcobrand.cl/products/p{i}?default_sku={i}")
        for i in range(10)
    ]

    await salcobrand.enrich_laboratory(
        productos, referer="https://salcobrand.cl/", concurrency=3
    )

    assert maximo <= 3
    assert all(p["laboratory_as_published"] == "LABORATORIO CHILE" for p in productos)


async def test_el_presupuesto_corta_las_fichas_lentas_sin_fallar(monkeypatch) -> None:
    async def lenta(*, url, referer, timeout_seconds):
        await asyncio.sleep(0.3)
        return _html("LABORATORIO CHILE")

    monkeypatch.setattr(salcobrand, "fetch_product_page", lenta)

    productos = [_product()]

    inicio = time.monotonic()
    await salcobrand.enrich_laboratory(
        productos, referer="https://salcobrand.cl/", budget_seconds=0.05
    )
    duracion = time.monotonic() - inicio

    assert duracion < 0.25
    assert productos[0]["laboratory_as_published"] is None


def _settings(**overrides):
    base = {
        "chain_search_enabled": ["salcobrand"],
        "salcobrand_live_search_enabled": True,
        "salcobrand_live_search_url": "https://algolia.example/1/indexes/*/queries",
        "salcobrand_live_search_app_id": "APPID",
        "salcobrand_live_search_api_key": SimpleNamespace(get_secret_value=lambda: "clave"),
        "salcobrand_live_search_index": "sb_variant_production",
        "salcobrand_live_search_referer": "https://salcobrand.cl/",
        "salcobrand_live_search_timeout_seconds": 10,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _hit(**overrides):
    base = {
        "sku": "430924",
        "objectID": "8193",
        "name": "Kitadol (B) Paracetamol 500mg 24 Comprimidos",
        "brand": "Kitadol",
        "options_text": "24 Comprimidos",
        "slug": "kitadol-b-paracetamol-500mg-24-comprimidos",
        "normal_price": 1499,
        "direct_discount": "998.0",
        "catalog_image_url": "https://static.salcobrand.cl/spree/products/169994/small/430924.jpg",
        "has_stock": True,
        "ribbon_info": {"price": "$998", "promotion_label": "Directo"},
    }
    base.update(overrides)
    return base


async def test_el_conector_entrega_el_fabricante_sin_cambiar_los_demas_campos(
    monkeypatch,
) -> None:
    monkeypatch.setattr(salcobrand_connector, "get_settings", lambda: _settings())

    async def fake_now(**kwargs):
        return MOMENTO

    async def fake_search(query, **kwargs):
        return {"results": [{"hits": [_hit()], "nbHits": 1, "page": 0}]}

    async def fake_fetch(*, url, referer, timeout_seconds):
        assert "kitadol-b-paracetamol-500mg-24-comprimidos" in url
        return _html("LABORATORIO CHILE")

    monkeypatch.setattr(salcobrand.clock, "now", fake_now)
    monkeypatch.setattr(salcobrand, "fetch_search_json", fake_search)
    monkeypatch.setattr(salcobrand, "fetch_product_page", fake_fetch)

    [ficha] = await salcobrand_connector.search("paracetamol")

    assert ficha["laboratory_as_published"] == "LABORATORIO CHILE"
    assert ficha["brand_as_published"] == "Kitadol"
    assert ficha["price"] == {
        "normal": {"amount": "1499", "currency": "CLP"},
        "promotional": {"amount": "998", "currency": "CLP"},
        "fractional": None,
    }
    assert ficha["conditions"] == "Directo"
    assert ficha["has_stock"] is True
    assert ficha["source_url"].endswith("?default_sku=430924")


async def test_un_fallo_del_enriquecimiento_no_invalida_la_busqueda(monkeypatch) -> None:
    monkeypatch.setattr(salcobrand_connector, "get_settings", lambda: _settings())

    async def fake_now(**kwargs):
        return MOMENTO

    async def fake_search(query, **kwargs):
        return {"results": [{"hits": [_hit()], "nbHits": 1, "page": 0}]}

    async def explode(*args, **kwargs):
        raise RuntimeError("error inesperado")

    monkeypatch.setattr(salcobrand.clock, "now", fake_now)
    monkeypatch.setattr(salcobrand, "fetch_search_json", fake_search)
    monkeypatch.setattr(salcobrand, "enrich_laboratory", explode)

    [ficha] = await salcobrand_connector.search("paracetamol")

    assert ficha["laboratory_as_published"] is None
    assert ficha["price"]["normal"]["amount"] == "1499"
