"""E15 — conector de Cruz Verde sin red: sesión, búsqueda, precios y enlaces.

La fuente real se reemplaza por un cliente HTTP falso. Las pruebas comprueban que se envían los
parámetros publicados, que los patrocinados no desplazan resultados orgánicos y que ningún campo
se completa por inferencia.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.services import cruzverde
from app.services.chains import cruzverde as cruzverde_connector
from app.services.chains.base import ChainNotConfigured, ChainUnavailable

MOMENTO = datetime(2026, 11, 5, 3, 30, tzinfo=UTC)
BASE = "https://api.cruzverde.cl/product-service"
LOGIN = "https://api.cruzverde.cl/customer-service/login"


class _Respuesta:
    def __init__(
        self,
        *,
        status: int = 200,
        payload=None,
        headers: list[tuple[str, str]] | None = None,
    ):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = httpx.Headers(headers or [])
        self.content = b"{}"

    def json(self):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", BASE)
            raise httpx.HTTPStatusError(
                "error de la fuente",
                request=request,
                response=httpx.Response(self.status_code),
            )


class _ClienteFalso:
    """Cliente guionado: cada llamada la decide la prueba y queda registrada."""

    rutas: list = []
    llamadas: list[dict] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.llamadas.append({"metodo": "GET", "url": url, **kwargs})
        return _ClienteFalso.rutas.pop(0)

    async def post(self, url, **kwargs):
        self.llamadas.append({"metodo": "POST", "url": url, **kwargs})
        return _ClienteFalso.rutas.pop(0)


class _SesionFalsa:
    def __init__(self):
        self.cookies = ["connect.sid=uno", "connect.sid=dos"]
        self.refrescos = 0

    async def cookie(self, **_kwargs):
        return self.cookies[0]

    async def refresh(self, _stale, **_kwargs):
        self.refrescos += 1
        return self.cookies[min(self.refrescos, len(self.cookies) - 1)]


class _SitemapFalso:
    def __init__(self, enlaces: dict[str, str] | None = None):
        self.enlaces = enlaces or {}
        self.llamadas = 0

    def lookup(self, product_id: str) -> str | None:
        return self.enlaces.get(product_id)

    async def ensure_loaded(self, **_kwargs):
        self.llamadas += 1


@pytest.fixture(autouse=True)
def _estado_limpio():
    cruzverde.session.reset()
    cruzverde.sitemap.clear()
    cruzverde.laboratory_cache.clear()
    _ClienteFalso.rutas = []
    _ClienteFalso.llamadas = []
    yield
    cruzverde.session.reset()
    cruzverde.sitemap.clear()
    cruzverde.laboratory_cache.clear()


def _hit(**overrides):
    base = {
        "hitType": "product",
        "productId": "263909",
        "productName": "Pyriped Ibuprofeno 400 mg 20 Cápsulas Blandas",
        "brand": "MILAB",
        "prices": {"price-list-cl": 6340, "price-sale-cl": 4890},
        "appliedPromotions": {},
        "promotions": [{"promotionId": "FPW001", "calloutMsg": "20% Descuento"}],
        "discountPercentage": 22,
        "pum": "Precio por unidad fraccionada: $245 por Capsula",
        "image": {
            "disBaseLink": "https://beta.cruzverde.cl/imagen/263909.jpg",
            "link": "https://beta.cruzverde.cl/imagen/263909.jpg",
        },
        "link": "https://beta.cruzverde.cl/s/Chile/dw/shop/v19_1/products/263909",
        "stock": 520,
    }
    base.update(overrides)
    return base


def _producto(**overrides):
    hit = _hit(**overrides)
    return cruzverde.parse_search_results(
        {"hits": [hit]}, observed_at=MOMENTO
    )[0]


def test_los_parametros_son_los_publicados_por_el_sitio() -> None:
    assert cruzverde.search_params("ibuprofeno") == {
        "q": "ibuprofeno",
        "limit": "24",
        "offset": "0",
        "isAndes": "true",
        "requestPage": "SearchPLP",
    }


@pytest.mark.parametrize("consulta", ["", "   "])
def test_una_consulta_vacia_no_consulta_la_fuente(consulta: str) -> None:
    with pytest.raises(cruzverde.CruzVerdeFetchError):
        cruzverde.search_params(consulta)


async def test_inicia_sesion_anonima_y_envia_la_cookie(monkeypatch) -> None:
    _ClienteFalso.rutas = [
        _Respuesta(
            status=201,
            payload={"authType": "guest"},
            headers=[
                ("set-cookie", "connect.sid=s%3Acruzverde-123; Path=/; HttpOnly; Secure")
            ],
        ),
        _Respuesta(payload={"hits": []}),
    ]
    monkeypatch.setattr(cruzverde.httpx, "AsyncClient", _ClienteFalso)

    payload = await cruzverde.fetch_search_json(
        "ibuprofeno", session=cruzverde.session, timeout_seconds=5
    )

    assert payload == {"hits": []}
    assert _ClienteFalso.llamadas[0]["metodo"] == "POST"
    assert _ClienteFalso.llamadas[0]["url"] == LOGIN
    assert _ClienteFalso.llamadas[0]["json"] == {}
    assert _ClienteFalso.llamadas[1]["headers"]["Cookie"] == (
        "connect.sid=s%3Acruzverde-123"
    )
    assert _ClienteFalso.llamadas[1]["url"].startswith(BASE + "/products/search?")
    assert "q=ibuprofeno" in _ClienteFalso.llamadas[1]["url"]


async def test_un_401_renueva_la_sesion_una_sola_vez(monkeypatch) -> None:
    _ClienteFalso.rutas = [
        _Respuesta(
            status=201,
            headers=[("set-cookie", "connect.sid=vieja; Path=/")],
            payload={"authType": "guest"},
        ),
        _Respuesta(status=401, payload={"error": "INVALID_SESSION"}),
        _Respuesta(
            status=201,
            headers=[("set-cookie", "connect.sid=nueva; Path=/")],
            payload={"authType": "guest"},
        ),
        _Respuesta(payload={"hits": []}),
    ]
    monkeypatch.setattr(cruzverde.httpx, "AsyncClient", _ClienteFalso)

    await cruzverde.fetch_search_json(
        "ibuprofeno", session=cruzverde.session, timeout_seconds=5
    )

    envios = [c for c in _ClienteFalso.llamadas if c["metodo"] == "GET"]
    assert envios[0]["headers"]["Cookie"] == "connect.sid=vieja"
    assert envios[1]["headers"]["Cookie"] == "connect.sid=nueva"
    assert len([c for c in _ClienteFalso.llamadas if c["metodo"] == "POST"]) == 2


async def test_si_el_401_persiste_la_fuente_falla(monkeypatch) -> None:
    _ClienteFalso.rutas = [
        _Respuesta(
            status=201,
            headers=[("set-cookie", "connect.sid=vieja; Path=/")],
            payload={},
        ),
        _Respuesta(status=401),
        _Respuesta(
            status=201,
            headers=[("set-cookie", "connect.sid=nueva; Path=/")],
            payload={},
        ),
        _Respuesta(status=401),
    ]
    monkeypatch.setattr(cruzverde.httpx, "AsyncClient", _ClienteFalso)

    with pytest.raises(cruzverde.CruzVerdeFetchError):
        await cruzverde.fetch_search_json(
            "ibuprofeno", session=cruzverde.session, timeout_seconds=5
        )


async def test_una_sesion_sin_cookie_es_fallo(monkeypatch) -> None:
    _ClienteFalso.rutas = [_Respuesta(status=201, payload={"authType": "guest"})]
    monkeypatch.setattr(cruzverde.httpx, "AsyncClient", _ClienteFalso)

    with pytest.raises(cruzverde.CruzVerdeFetchError):
        await cruzverde.fetch_search_json(
            "ibuprofeno", session=cruzverde.session, timeout_seconds=5
        )


async def test_la_sesion_se_reutiliza_entre_consultas(monkeypatch) -> None:
    _ClienteFalso.rutas = [
        _Respuesta(
            status=201,
            headers=[("set-cookie", "connect.sid=uno; Path=/")],
            payload={},
        ),
        _Respuesta(payload={"hits": []}),
        _Respuesta(payload={"hits": []}),
    ]
    monkeypatch.setattr(cruzverde.httpx, "AsyncClient", _ClienteFalso)

    await cruzverde.fetch_search_json(
        "ibuprofeno", session=cruzverde.session, timeout_seconds=5
    )
    await cruzverde.fetch_search_json(
        "paracetamol", session=cruzverde.session, timeout_seconds=5
    )

    assert len([c for c in _ClienteFalso.llamadas if c["metodo"] == "POST"]) == 1


def test_excluye_patrocinados_antes_de_recortar_y_conserva_el_orden() -> None:
    sponsored = {
        "isSponsored": True,
        "adId": "anuncio",
        "productName": "Producto patrocinado sin hitType",
    }
    hits = [sponsored] + [
        _hit(productId=str(i), productName=f"Ibuprofeno {i} 20 Comprimidos")
        for i in range(30)
    ]

    productos = cruzverde.parse_search_results(
        {"hits": hits}, limit=24, observed_at=MOMENTO
    )

    assert len(productos) == 24
    assert [p["chain_item_id"] for p in productos] == [str(i) for i in range(24)]
    assert all("patrocinado" not in p["published_name"] for p in productos)


def test_tolera_campos_ausentes_y_conserva_lo_publicado() -> None:
    hit = _hit(
        brand=None,
        image={},
        pum=None,
        prices={},
        appliedPromotions=None,
        productName="Producto sin precio",
    )
    [producto] = cruzverde.parse_search_results({"hits": [hit]}, observed_at=MOMENTO)

    assert producto["brand_as_published"] is None
    assert producto["image_url"] is None
    assert producto["price"] == {"normal": None, "promotional": None, "fractional": None}
    assert producto["has_stock"] is None
    assert producto["conditions"] is None


def test_sin_identificador_o_nombre_la_ficha_se_omite() -> None:
    hits = [
        _hit(productId=None),
        _hit(productName=None),
        {"hitType": "product", "productId": "1", "productName": "Válido"},
    ]
    productos = cruzverde.parse_search_results({"hits": hits}, observed_at=MOMENTO)
    assert [p["chain_item_id"] for p in productos] == ["1"]


def test_precio_normal_y_oferta_solo_cuando_la_oferta_es_menor() -> None:
    [producto] = cruzverde.parse_search_results(
        {"hits": [_hit(prices={"price-list-cl": 2000, "price-sale-cl": 1500})]},
        observed_at=MOMENTO,
    )
    assert producto["price"]["normal"] == {"amount": "2000", "currency": "CLP"}
    assert producto["price"]["promotional"] == {"amount": "1500", "currency": "CLP"}

    [igual] = cruzverde.parse_search_results(
        {"hits": [_hit(prices={"price-list-cl": 2000, "price-sale-cl": 2000})]},
        observed_at=MOMENTO,
    )
    assert igual["price"]["promotional"] is None


def test_una_oferta_sin_promocion_aplicada_no_inventa_condiciones() -> None:
    producto = _producto(
        prices={"price-list-cl": 6340, "price-sale-cl": 4890},
        appliedPromotions={},
        promotions=[{"promotionId": "FPW001", "calloutMsg": "20% Descuento"}],
    )
    assert producto["price"]["promotional"] is not None
    assert producto["conditions"] is None


def test_la_promocion_aplicada_es_la_unica_fuente_de_condiciones() -> None:
    producto = _producto(
        appliedPromotions={
            "price-sale-cl": {
                "calloutMsg": "20% Descuento",
                "promotionalPrice": 1264,
                "promotionId": "FPW001",
            }
        }
    )
    assert producto["conditions"] == "20% Descuento"


def test_el_precio_de_club_no_reemplaza_la_oferta_general() -> None:
    producto = _producto(
        prices={
            "price-list-cl": 6340,
            "price-sale-cl": 4890,
            "price-club-cl": 1000,
        },
        appliedPromotions={"price-club-cl": {"calloutMsg": "Solo con tarjeta"}},
    )
    assert producto["price"]["normal"]["amount"] == "6340"
    assert producto["price"]["promotional"]["amount"] == "4890"
    assert producto["conditions"] is None


@pytest.mark.parametrize(
    ("pum", "esperado"),
    [
        (
            "Precio por unidad fraccionada: $245 por Capsula",
            {"amount": "245", "currency": "CLP", "per_unit": "Capsula"},
        ),
        (
            "Precio por unidad fraccionada: $3.120 por 100 mL",
            {"amount": "3120", "currency": "CLP", "per_unit": "100 mL"},
        ),
        (
            "Precio por Unidad Fraccionada: $ 27450 por 100 mL",
            {"amount": "27450", "currency": "CLP", "per_unit": "100 mL"},
        ),
        (
            "Precio por unidad fraccionada: $1.160 por Sobre",
            {"amount": "1160", "currency": "CLP", "per_unit": "Sobre"},
        ),
        (
            "Precio por unidad fraccionada: $125 por Gramos",
            {"amount": "125", "currency": "CLP", "per_unit": "Gramos"},
        ),
        ("Precio por unidad fraccionada: $3227 por Aplicaciones", None),
        ("$245 por Capsula", None),
        ("Precio por unidad fraccionada: $3,120 por Capsula", None),
        (None, None),
    ],
)
def test_precio_fraccionado_solo_con_importe_y_unidad_identificables(
    pum, esperado
) -> None:
    assert cruzverde.parse_fractional(pum) == esperado


@pytest.mark.parametrize(
    ("nombre", "esperado"),
    [
        ("Pyriped Ibuprofeno 400 mg 20 Cápsulas Blandas", "20 Cápsulas Blandas"),
        ("Paracetamol 500 mg 16 Comprimidos", "16 Comprimidos"),
        ("Paracetamol 100 mg/5mL Suspensión 100 mL", "100 mL"),
        ("Tramadol Clorhidrato 100 mg / mL Solución 20 mL", "20 mL"),
        ("Dolorub Ibuprofeno 5% Crema Tópica 60 gr", "60 gr"),
        ("Tapsin Duo Paracetamol Ibuprofeno 12 Comprimidos Recubiertos", "12 Comprimidos Recubiertos"),
        ("Ibuprofeno 400 mg 20 comprimidos + 2 x 10 comprimidos", "20 comprimidos"),
        ("Ibuprofeno 400 mg", None),
        ("Solución 100 mg/5mL", None),
        ("", None),
    ],
)
def test_presentacion_conservadora(nombre, esperado) -> None:
    assert cruzverde.parse_presentation(nombre) == esperado


def test_el_enlace_canonico_del_sitemap_tiene_prioridad() -> None:
    canonical = "https://www.cruzverde.cl/pyriped-ibuprofeno-400-mg/263909.html"
    producto = _producto()
    index = _SitemapFalso({"263909": canonical})

    cruzverde._apply_links([producto], index)

    assert producto["source_url"] == canonical
    assert producto["provenance"]["link_source"] == "sitemap"


def test_sin_sitemap_usa_el_enlace_publico_de_respaldo_y_lo_marca() -> None:
    producto = _producto()

    assert producto["source_url"] == "https://www.cruzverde.cl/product/263909.html"
    assert producto["provenance"]["link_source"] == "fallback"
    assert "dw/shop" not in producto["source_url"]


def test_una_ficha_sin_enlace_publico_se_conserva_sin_url() -> None:
    producto = _producto(productId="no-numerico")

    assert producto["source_url"] is None
    assert producto["provenance"]["link_source"] == "unavailable"


async def test_el_sitemap_se_lee_del_indice_y_se_cachea(monkeypatch) -> None:
    documentos = {
        "https://www.cruzverde.cl/sitemap_index.xml": (
            "<sitemapindex><sitemap><loc>https://www.cruzverde.cl/sitemap_0-product.xml"
            "</loc></sitemap><sitemap><loc>https://www.cruzverde.cl/sitemap_4-image.xml"
            "</loc></sitemap></sitemapindex>"
        ),
        "https://www.cruzverde.cl/sitemap_0-product.xml": (
            "<urlset><url><loc>https://www.cruzverde.cl/producto-x/263909.html</loc></url>"
            "<url><loc>https://www.cruzverde.cl/otro/580696.html</loc></url></urlset>"
        ),
    }

    async def fake_fetch(url, *, timeout_seconds):
        return documentos[url]

    monkeypatch.setattr(cruzverde, "_fetch_text", fake_fetch)
    index = cruzverde.ProductSitemap()

    await index.ensure_loaded(timeout_seconds=5)

    assert index.lookup("263909") == "https://www.cruzverde.cl/producto-x/263909.html"
    assert index.lookup("580696") == "https://www.cruzverde.cl/otro/580696.html"
    assert len(index) == 2


async def test_busca_normaliza_y_usa_el_sitemap_en_cache(monkeypatch) -> None:
    _ClienteFalso.rutas = [
        _Respuesta(payload={"hits": [_hit(), _hit(productId="580696")]}),
    ]
    monkeypatch.setattr(cruzverde.httpx, "AsyncClient", _ClienteFalso)

    async def sin_fichas(*args, **kwargs):
        return None

    monkeypatch.setattr(cruzverde, "enrich_laboratory", sin_fichas)
    index = _SitemapFalso(
        {
            "263909": "https://www.cruzverde.cl/pyriped-ibuprofeno-400-mg/263909.html",
        }
    )

    productos = await cruzverde.search_products(
        "ibuprofeno",
        session_manager=_SesionFalsa(),
        sitemap_index=index,
        budget_seconds=2,
        observed_at=MOMENTO,
    )

    assert [p["chain_item_id"] for p in productos] == ["263909", "580696"]
    assert productos[0]["source_url"].endswith("/263909.html")
    assert productos[0]["provenance"]["link_source"] == "sitemap"
    assert productos[1]["source_url"] == "https://www.cruzverde.cl/product/580696.html"
    assert productos[1]["provenance"]["link_source"] == "fallback"
    assert all(p["has_stock"] is None for p in productos)


@pytest.mark.asyncio
async def test_la_busqueda_respeta_el_presupuesto_de_la_fuente(monkeypatch) -> None:
    capturado: dict = {}

    async def busqueda_lenta(query, **kwargs):
        capturado.update(kwargs)
        return {"hits": []}

    monkeypatch.setattr(cruzverde, "fetch_search_json", busqueda_lenta)

    productos = await cruzverde.search_products(
        "ibuprofeno",
        timeout_seconds=10,
        budget_seconds=2,
        sitemap_index=_SitemapFalso(),
    )

    assert productos == []
    assert capturado["timeout_seconds"] == 2


def _settings(**overrides):
    base = {
        "chain_search_enabled": [],
        "cruzverde_live_search_enabled": False,
        "cruzverde_live_search_base_url": None,
        "cruzverde_live_search_login_url": None,
        "cruzverde_live_search_sitemap_url": None,
        "cruzverde_live_search_timeout_seconds": 10,
        "cruzverde_live_search_budget_seconds": 4.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_el_conector_apagado_no_consulta_la_fuente(monkeypatch) -> None:
    async def no_consultar(*args, **kwargs):
        raise AssertionError("Una cadena apagada no debe consultar su fuente.")

    monkeypatch.setattr(cruzverde_connector, "get_settings", lambda: _settings())
    monkeypatch.setattr(cruzverde, "search_products", no_consultar)

    with pytest.raises(ChainNotConfigured) as exc:
        await cruzverde_connector.search("ibuprofeno")

    assert exc.value.reason == "access_not_authorized"


async def test_el_conector_habilitado_entrega_las_fichas(monkeypatch) -> None:
    capturado = {}

    async def fake_search(query, **kwargs):
        capturado["query"] = query
        capturado.update(kwargs)
        return [{"chain_item_id": "1", "published_name": "Producto"}]

    monkeypatch.setattr(
        cruzverde_connector,
        "get_settings",
        lambda: _settings(
            chain_search_enabled=["cruz_verde"],
            cruzverde_live_search_enabled=True,
        ),
    )
    monkeypatch.setattr(cruzverde, "search_products", fake_search)

    fichas = await cruzverde_connector.search("ibuprofeno")

    assert fichas == [{"chain_item_id": "1", "published_name": "Producto"}]
    assert capturado["query"] == "ibuprofeno"
    assert capturado["base_url"] == cruzverde.DEFAULT_BASE_URL
    assert capturado["login_url"] == cruzverde.DEFAULT_LOGIN_URL
    assert capturado["sitemap_url"] == cruzverde.DEFAULT_SITEMAP_URL
    assert capturado["budget_seconds"] == 4.0


async def test_un_fallo_de_la_fuente_es_chain_unavailable(monkeypatch) -> None:
    async def rota(*args, **kwargs):
        raise cruzverde.CruzVerdeFetchError("caída")

    monkeypatch.setattr(
        cruzverde_connector,
        "get_settings",
        lambda: _settings(
            chain_search_enabled=["cruz_verde"],
            cruzverde_live_search_enabled=True,
        ),
    )
    monkeypatch.setattr(cruzverde, "search_products", rota)

    with pytest.raises(ChainUnavailable) as exc:
        await cruzverde_connector.search("ibuprofeno")

    assert exc.value.reason == "fetch_failed"


async def test_un_fallo_de_cruz_verde_queda_contenido_en_su_cadena(monkeypatch) -> None:
    from app.services import chain_search

    async def rota(*args, **kwargs):
        raise cruzverde.CruzVerdeFetchError("caída")

    monkeypatch.setattr(
        cruzverde_connector,
        "get_settings",
        lambda: _settings(
            chain_search_enabled=["cruz_verde"],
            cruzverde_live_search_enabled=True,
        ),
    )
    monkeypatch.setattr(cruzverde, "search_products", rota)

    source = await chain_search._source_for("cruz_verde", "ibuprofeno")

    assert source["chain"] == "cruz_verde"
    assert source["state"] == "failed"
    assert source["reason"] == "fetch_failed"
    assert source["outcome"] is None
    assert source["results"] == []


def test_el_esquema_acepta_una_ficha_sin_enlace_publico() -> None:
    from app.api.v1.search import ChainSourceOut

    producto = _producto(productId="no-numerico")
    out = ChainSourceOut.model_validate(
        {
            "chain": "cruz_verde",
            "state": "ok",
            "reason": None,
            "outcome": "matches",
            "observed_at": MOMENTO,
            "results": [producto],
        }
    )

    assert out.results[0].source_url is None
    assert out.results[0].has_stock is None
