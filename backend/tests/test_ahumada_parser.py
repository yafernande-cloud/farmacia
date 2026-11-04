"""E15 — parser HTML de Farmacias Ahumada con una muestra acotada (sin red)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services import ahumada

FIXTURE = Path(__file__).parent / "fixtures" / "ahumada_search.html"
OBSERVED_AT = datetime(2026, 11, 4, 15, 30, tzinfo=UTC)


@pytest.fixture
def results() -> list[dict]:
    return ahumada.parse_search_results(FIXTURE.read_text(encoding="utf-8"), observed_at=OBSERVED_AT)


def _by_id(results: list[dict], pid: str) -> dict:
    return next(item for item in results if item["chain_item_id"] == pid)


def test_extrae_las_fichas_publicadas_y_omite_la_incompleta(results: list[dict]) -> None:
    ids = [item["chain_item_id"] for item in results]
    assert "84574" in ids and "84575" in ids and "70000" in ids
    # La tarjeta sin nombre ni URL oficial no se completa con datos inventados.
    assert "00000" not in ids


def test_precio_normal_promocional_y_condicion_del_mismo_producto(results: list[dict]) -> None:
    product = _by_id(results, "84574")
    assert product["price"]["normal"] == {"amount": "1309", "currency": "CLP"}
    assert product["price"]["promotional"] == {"amount": "618", "currency": "CLP"}
    assert product["conditions"] == "Ahumada Contigo"
    assert product["published_name"] == "Paracetamol 500 mg x 16 comprimidos"
    assert product["laboratory_as_published"] == "Laboratorio Chile"
    assert product["presentation_as_published"] == "16 comprimidos"
    assert product["description"] == "Analgésico y antipirético de uso común."


def test_precio_fraccionado_va_aparte_del_envase(results: list[dict]) -> None:
    paracetamol = _by_id(results, "84574")
    assert paracetamol["price"]["fractional"] == {
        "amount": "82",
        "currency": "CLP",
        "per_unit": "comprimido",
    }
    # El precio por comprimido nunca se usa como precio del envase.
    assert paracetamol["price"]["normal"]["amount"] == "1309"
    assert paracetamol["price"]["promotional"]["amount"] == "618"

    ibuprofeno = _by_id(results, "70000")
    assert ibuprofeno["price"]["normal"] == {"amount": "3990", "currency": "CLP"}
    assert ibuprofeno["price"]["promotional"] is None
    assert ibuprofeno["price"]["fractional"] == {
        "amount": "199",
        "currency": "CLP",
        "per_unit": "comprimido",
    }


def test_un_solo_importe_es_el_precio_publicado_del_envase(results: list[dict]) -> None:
    product = _by_id(results, "84575")
    assert product["price"]["normal"] == {"amount": "4590", "currency": "CLP"}
    assert product["price"]["promotional"] is None
    assert product["price"]["fractional"] is None


def test_producto_relacionado_no_contamina_el_precio_principal(results: list[dict]) -> None:
    principal = _by_id(results, "84574")
    relacionados = [item for item in results if item["chain_item_id"] == "99999"]
    assert relacionados and relacionados[0]["price"]["normal"]["amount"] == "9999"
    # 9.999 pertenece al producto anidado, no al paracetamol.
    precios_principal = {
        price["amount"]
        for price in principal["price"].values()
        if isinstance(price, dict) and "amount" in price
    }
    assert "9999" not in precios_principal


def test_producto_anidado_sin_etiquetas_no_contamina_al_principal() -> None:
    """El texto de una tarjeta relacionada tampoco se usa si los precios no tienen etiqueta."""
    html = """
    <div class="product-tile-wrapper" data-pid="1">
      <div class="product-tile">
        <a href="/principal-1.html"><h2>Principal</h2></a>
        <div class="product-price">$1.000</div>
        <div class="product-tile-wrapper" data-pid="2">
          <div class="product-tile">
            <a href="/relacionado-2.html"><h2>Relacionado</h2></a>
            <div class="product-price">$9.999</div>
          </div>
        </div>
      </div>
    </div>
    """
    results = ahumada.parse_search_results(html)
    principal = _by_id(results, "1")
    relacionado = _by_id(results, "2")
    assert principal["price"]["normal"]["amount"] == "1000"
    assert relacionado["price"]["normal"]["amount"] == "9999"


def test_estructura_publicada_de_la_ficha_de_ahumada() -> None:
    """La ficha real trae un badge antes del producto, el laboratorio en la marca y
    el fraccionado como «$ 82 x Comp.» dentro de `.sales`."""
    html = """
    <div class="product product-tile-wrapper" data-pid="84574">
      <div class="product-tile h-100 d-flex flex-column" data-pid="84574">
        <div class="extra-badges-bien-estar">
          <div class="extra-badge-container">
            <img class="extra-badge-bien-estar" alt="Schedule Badge"
                 src="https://www.farmaciasahumada.cl/badges/precio_fijo.png">
          </div>
        </div>
        <div class="image-container">
          <a href="/paracetamol-500-mg-x-16-comprimidos-84574.html">
            <img class="tile-image"
                 src="https://www.farmaciasahumada.cl/medias/84574.jpg"
                 alt="Paracetamol 500 mg x 16 Comprimidos"
                 title="Paracetamol 500 mg x 16 Comprimidos, ">
          </a>
        </div>
        <div class="tile-body">
          <div class="preccio-fracionado-wrapper">
            <span class="preccio-fracionado">$ 82 x Comp.</span>
          </div>
          <div class="product-tile-brand">
            <span class="link" href="/paracetamol-84574.html">Chile</span>
          </div>
          <div class="pdp-link">
            <a class="link" href="/paracetamol-500-mg-x-16-comprimidos-84574.html">
              Paracetamol 500 mg x 16 Comprimidos
            </a>
          </div>
          <div class="price">
            <span class="d-block default-price">
              <span class="sales d-flex">
                <div class="promotion-badge-container flex-row align-items-center ml-0">
                  $618
                  <img class="promotion-badge" alt="promotion badge"
                       src="https://www.farmaciasahumada.cl/badges/promo.png">
                </div>
              </span>
              <del class="text-decoration-none">
                <span class="strike-through list text-decoration-none">
                  <span class="value" content="1309">
                    <span class="sr-only">Price reduced from</span>
                    $1.309
                    <span class="sr-only">to</span>
                  </span>
                  <span class="ml-1 text-decoration-none price-internet">Precio normal</span>
                </span>
              </del>
            </span>
          </div>
        </div>
      </div>
    </div>
    """
    product = ahumada.parse_search_results(html)[0]
    assert product["chain_item_id"] == "84574"
    assert product["published_name"] == "Paracetamol 500 mg x 16 Comprimidos"
    # El laboratorio se publica en el bloque de marca de la ficha.
    assert product["laboratory_as_published"] == "Chile"
    assert product["presentation_as_published"] == "x 16 Comprimidos"
    assert product["price"]["normal"] == {"amount": "1309", "currency": "CLP"}
    assert product["price"]["promotional"] == {"amount": "618", "currency": "CLP"}
    assert product["price"]["fractional"] == {
        "amount": "82",
        "currency": "CLP",
        "per_unit": "comprimido",
    }
    assert product["conditions"] is None
    assert product["image_url"] == "https://www.farmaciasahumada.cl/medias/84574.jpg"
    assert product["source_url"] == (
        "https://www.farmaciasahumada.cl/paracetamol-500-mg-x-16-comprimidos-84574.html"
    )


def test_el_badge_no_se_usa_como_nombre_del_producto() -> None:
    html = """
    <div class="product-tile" data-pid="1">
      <img class="extra-badge-bien-estar" alt="Schedule Badge" src="/badge.png">
      <a href="/producto-1.html"><img class="tile-image" alt="Producto real" src="/1.jpg"></a>
      <div class="product-tile-brand">Laboratorio Demo</div>
      <div class="product-price">$1.000</div>
    </div>
    """
    product = ahumada.parse_search_results(html)[0]
    assert product["published_name"] == "Producto real"
    assert product["laboratory_as_published"] == "Laboratorio Demo"


def test_las_cuotas_no_se_confunden_con_el_precio_del_envase() -> None:
    html = """
    <div class="product-tile" data-pid="3">
      <a href="/producto-3.html"><h2>Producto</h2></a>
      <div class="product-price">$1.000</div>
      <div class="installments">3 cuotas de $333</div>
    </div>
    """
    product = ahumada.parse_search_results(html)[0]
    assert product["price"]["normal"] == {"amount": "1000", "currency": "CLP"}
    assert product["price"]["promotional"] is None
    assert product["price"]["fractional"] is None


def test_extrae_etiquetas_y_precio_estructurado_cuando_no_hay_clases() -> None:
    html = """
    <div class="product-tile" data-pid="4">
      <a href="/producto-4.html"><h2>Producto cuatro</h2></a>
      <meta itemprop="price" content="2500">
      <dl>
        <dt>Laboratorio</dt><dd>Laboratorio Demo</dd>
        <dt>Presentación</dt><dd>20 comprimidos</dd>
      </dl>
    </div>
    """
    product = ahumada.parse_search_results(html)[0]
    assert product["laboratory_as_published"] == "Laboratorio Demo"
    assert product["presentation_as_published"] == "20 comprimidos"
    assert product["price"]["normal"] == {"amount": "2500", "currency": "CLP"}


def test_enlaces_imagenes_y_urls_son_absolutos_y_oficiales(results: list[dict]) -> None:
    product = _by_id(results, "84574")
    assert product["source_url"] == (
        "https://www.farmaciasahumada.cl/paracetamol-500-mg-x-16-comprimidos-84574.html"
    )
    assert product["image_url"] == (
        "https://www.farmaciasahumada.cl/medias/paracetamol-84574.jpg"
    )
    assert product["observed_at"] == OBSERVED_AT.isoformat()
    assert product["provenance"]["data_origin"] == "published"


def test_html_vacio_o_malformado_no_inventa_fichas() -> None:
    assert ahumada.parse_search_results("") == []
    assert ahumada.parse_search_results("<div class='product-tile' data-pid='1'>") == []


def test_limite_de_resultados(results: list[dict]) -> None:
    assert len(results) == 4
    assert len(ahumada.parse_search_results(
        FIXTURE.read_text(encoding="utf-8"), limit=2
    )) == 2


@pytest.mark.asyncio
async def test_la_fuente_en_vivo_rechaza_url_no_http() -> None:
    with pytest.raises(ahumada.AhumadaFetchError):
        await ahumada.fetch_search_html("paracetamol", url="file:///etc/passwd")
