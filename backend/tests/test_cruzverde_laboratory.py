"""E15 — laboratorio publicado en la ficha de Cruz Verde (sin red).

La pestaña `LABORATORIO` tiene prioridad sobre el campo `laboratory`; una marca repetida nunca se
publica como laboratorio. Las fichas se piden con concurrencia limitada y presupuesto, y una ficha
caída no invalida la búsqueda.
"""

import asyncio
import time
from datetime import UTC, datetime

import pytest

from app.services import cruzverde

MOMENTO = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)


def _detail(
    *,
    brand: str | None = "MILAB",
    laboratory: str | None = None,
    tab: str | None = None,
) -> dict:
    product = {}
    if brand is not None:
        product["brand"] = brand
    if laboratory is not None:
        product["laboratory"] = laboratory
    if tab is not None:
        product["tabs"] = [{"content": f"<b>LABORATORIO :</b> {tab}<br/>Drogas: ibuprofeno"}]
    return {"productData": product}


def _product(product_id: str = "263909") -> dict:
    return {
        "chain_item_id": product_id,
        "published_name": "Ibuprofeno 400 mg 20 Comprimidos",
        "brand_as_published": "MILAB",
        "laboratory_as_published": None,
        "presentation_as_published": "20 Comprimidos",
        "price": {
            "normal": {"amount": "6340", "currency": "CLP"},
            "promotional": {"amount": "4890", "currency": "CLP"},
            "fractional": None,
        },
        "conditions": None,
        "has_stock": None,
        "source_url": f"https://www.cruzverde.cl/product/{product_id}.html",
    }


@pytest.fixture(autouse=True)
def _cache_limpia():
    cruzverde.laboratory_cache.clear()
    yield
    cruzverde.laboratory_cache.clear()


def test_la_pestana_laboratorio_tiene_prioridad() -> None:
    payload = _detail(laboratory="MILAB", tab="MINTLAB")
    assert cruzverde.parse_detail_laboratory(payload) == "MINTLAB"


def test_si_la_pestana_repite_la_marca_no_se_publica_como_laboratorio() -> None:
    payload = _detail(brand="MILAB", laboratory="MILAB", tab="MILAB")
    assert cruzverde.parse_detail_laboratory(payload) is None


def test_el_campo_laboratory_sirve_solo_si_difiere_de_la_marca() -> None:
    assert (
        cruzverde.parse_detail_laboratory(
            _detail(brand="HEAD & SHOULDERS", laboratory="PROCTER-GILLETTE")
        )
        == "PROCTER-GILLETTE"
    )
    assert (
        cruzverde.parse_detail_laboratory(
            _detail(brand="D&M PHARMA", laboratory="D & M PHARMA")
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"productData": None},
        {"productData": {}},
        {"productData": {"brand": "MILAB"}},
        {"productData": {"tabs": "no es lista"}},
    ],
)
def test_una_ficha_sin_laboratorio_devuelve_none(payload: dict) -> None:
    assert cruzverde.parse_detail_laboratory(payload) is None


async def test_una_ficha_caida_no_afecta_a_las_demas(monkeypatch) -> None:
    async def fake_detail(product_id, **_kwargs):
        if product_id == "2":
            raise cruzverde.CruzVerdeFetchError("ficha caída")
        return _detail(tab="MINTLAB")

    monkeypatch.setattr(cruzverde, "fetch_product_detail", fake_detail)
    productos = [_product("1"), _product("2")]

    await cruzverde.enrich_laboratory(productos, budget_seconds=2)

    assert productos[0]["laboratory_as_published"] == "MINTLAB"
    assert productos[1]["laboratory_as_published"] is None
    # Un error transitorio no se cachea: una búsqueda posterior puede recuperarlo.
    assert cruzverde.laboratory_cache.get("2") == (False, None)


async def test_la_cache_evita_repetir_la_misma_ficha(monkeypatch) -> None:
    llamadas = []

    async def fake_detail(product_id, **_kwargs):
        llamadas.append(product_id)
        return _detail(tab="MINTLAB")

    monkeypatch.setattr(cruzverde, "fetch_product_detail", fake_detail)

    await cruzverde.enrich_laboratory([_product()], budget_seconds=2)
    await cruzverde.enrich_laboratory([_product()], budget_seconds=2)

    assert llamadas == ["263909"]


async def test_la_ausencia_confirmada_tambien_se_cachea(monkeypatch) -> None:
    llamadas = []

    async def fake_detail(product_id, **_kwargs):
        llamadas.append(product_id)
        return _detail(brand="MILAB", laboratory="MILAB", tab="MILAB")

    monkeypatch.setattr(cruzverde, "fetch_product_detail", fake_detail)

    await cruzverde.enrich_laboratory([_product()], budget_seconds=2)
    await cruzverde.enrich_laboratory([_product()], budget_seconds=2)

    assert llamadas == ["263909"]
    assert cruzverde.laboratory_cache.get("263909") == (True, None)


async def test_una_ausencia_en_cache_no_deja_un_laboratorio_anterior(monkeypatch) -> None:
    async def fake_detail(product_id, **_kwargs):
        return _detail(brand="MILAB", laboratory="MILAB", tab="MILAB")

    monkeypatch.setattr(cruzverde, "fetch_product_detail", fake_detail)
    propia = cruzverde.LaboratoryCache()
    propia.set("263909", None)
    producto = _product()
    producto["laboratory_as_published"] = "VALOR ANTERIOR"

    await cruzverde.enrich_laboratory(
        [producto], budget_seconds=2, cache=propia
    )

    assert producto["laboratory_as_published"] is None


def test_la_cache_vence_y_respeta_el_tamano_maximo() -> None:
    vencida = cruzverde.LaboratoryCache(ttl_seconds=0, max_entries=10)
    vencida.set("1", "LAB")
    assert vencida.get("1") == (False, None)

    acotada = cruzverde.LaboratoryCache(ttl_seconds=600, max_entries=2)
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

    async def fake_detail(product_id, **_kwargs):
        nonlocal activos, maximo
        async with lock:
            activos += 1
            maximo = max(maximo, activos)
        await asyncio.sleep(0.01)
        async with lock:
            activos -= 1
        return _detail(tab="MINTLAB")

    monkeypatch.setattr(cruzverde, "fetch_product_detail", fake_detail)
    productos = [_product(str(i)) for i in range(10)]

    await cruzverde.enrich_laboratory(
        productos, budget_seconds=2, concurrency=3
    )

    assert maximo <= 3
    assert all(p["laboratory_as_published"] == "MINTLAB" for p in productos)


async def test_el_presupuesto_corta_las_fichas_lentas_sin_fallar(monkeypatch) -> None:
    async def lenta(product_id, **_kwargs):
        await asyncio.sleep(0.3)
        return _detail(tab="MINTLAB")

    monkeypatch.setattr(cruzverde, "fetch_product_detail", lenta)
    productos = [_product()]

    inicio = time.monotonic()
    await cruzverde.enrich_laboratory(
        productos, budget_seconds=0.05
    )
    duracion = time.monotonic() - inicio

    assert duracion < 0.25
    assert productos[0]["laboratory_as_published"] is None


class _SitemapFalso:
    def lookup(self, product_id: str) -> str | None:
        return None

    async def ensure_loaded(self, **_kwargs):
        return None


async def test_la_busqueda_conserva_las_fichas_si_el_enriquecimiento_falla(
    monkeypatch,
) -> None:
    async def fake_search(*args, **kwargs):
        return {
            "hits": [
                {
                    "hitType": "product",
                    "productId": "263909",
                    "productName": "Ibuprofeno 400 mg 20 Comprimidos",
                    "brand": "MILAB",
                    "prices": {"price-list-cl": 6340, "price-sale-cl": 4890},
                }
            ]
        }

    async def explode(*args, **kwargs):
        raise RuntimeError("error inesperado")

    monkeypatch.setattr(cruzverde, "fetch_search_json", fake_search)
    monkeypatch.setattr(cruzverde, "enrich_laboratory", explode)

    productos = await cruzverde.search_products(
        "ibuprofeno",
        session_manager=object(),
        sitemap_index=_SitemapFalso(),
        budget_seconds=1,
        observed_at=MOMENTO,
    )

    assert len(productos) == 1
    assert productos[0]["laboratory_as_published"] is None
    assert productos[0]["price"]["normal"]["amount"] == "6340"
