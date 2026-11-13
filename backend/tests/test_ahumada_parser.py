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


# --- Precio por unidad de medida (regresión QA) ---------------------------------------------
# Las fichas de abajo son SINTÉTICAS: reproducen la estructura publicada, no una captura del
# caso reportado. Comprueban la regla, no el dato que originó el informe.


def test_el_precio_por_litro_no_se_publica_como_precio_normal() -> None:
    html = """
    <div class="product-tile" data-pid="96003">
      <a href="/producto-96003.html"><h2>Alcohol Etílico 96 Grados 1 L</h2></a>
      <div class="product-price">
        <del class="old-price">$218.980 por litro</del>
        <span class="promo-price">$10.949</span>
      </div>
    </div>
    """
    product = ahumada.parse_search_results(html)[0]
    # El precio por litro es una razón publicada: no es «Normal» ni se usa como envase.
    assert product["price"]["normal"] is None
    assert product["price"]["promotional"] == {"amount": "10949", "currency": "CLP"}
    assert product["price"]["fractional"] == {
        "amount": "218980",
        "currency": "CLP",
        "per_unit": "litro",
    }


def test_sin_precio_de_envase_identificable_no_se_inventa_uno() -> None:
    html = """
    <div class="product-tile" data-pid="91096">
      <a href="/producto-91096.html"><h2>Ácido Bórico 100 g</h2></a>
      <div class="product-price">$218.980 por litro</div>
    </div>
    """
    product = ahumada.parse_search_results(html)[0]
    assert product["price"]["normal"] is None
    assert product["price"]["promotional"] is None
    assert product["price"]["fractional"]["per_unit"] == "litro"


def test_la_presentacion_no_es_una_razon_por_unidad_de_medida() -> None:
    html = """
    <div class="product-tile" data-pid="2783">
      <a href="/producto-2783.html"><h2>Paracetamol 100 mg/mL x 15 mL Gotas</h2></a>
      <div class="product-price">$3.990 x 15 mL</div>
    </div>
    """
    product = ahumada.parse_search_results(html)[0]
    assert product["price"]["normal"] == {"amount": "3990", "currency": "CLP"}
    assert product["price"]["fractional"] is None


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("$2.190 por 100 ml", ("2190", "100 ml")),
        ("$12.900 /litro", ("12900", "litro")),
        ("$5.990 por kg", ("5990", "kg")),
        ("$82 x Comp.", ("82", "comprimido")),
        ("$3.490 por caja", ("3490", None)),
        ("$1.309 x 15 mL", ("1309", None)),
    ],
)
def test_unidad_fraccionada_reconoce_medidas_y_descarta_la_presentacion(
    texto: str, esperado: tuple[str, str | None]
) -> None:
    assert ahumada._amounts(texto) == [esperado]


# --- Relación con la consulta (regresión QA) -------------------------------------------------


def test_una_pagina_sin_ninguna_coincidencia_no_es_un_resultado() -> None:
    """Una vitrina de destacados no se presenta como resultado de la búsqueda."""
    html = FIXTURE.read_text(encoding="utf-8")
    assert ahumada.parse_search_results(html, query="acido mefenamico") == []
    assert ahumada.parse_search_results(html, query="Ácido Mefenámico") == []


def test_una_ficha_de_marca_se_conserva_cuando_la_pagina_confirma() -> None:
    """La ficha que no repite la consulta no se descarta si la página sí la confirma."""
    html = FIXTURE.read_text(encoding="utf-8")
    results = ahumada.parse_search_results(html, query="paracetamol")
    assert {item["chain_item_id"] for item in results} == {"84574", "84575", "70000", "99999"}
    assert _by_id(results, "84574")["match"] == {
        "level": "direct",
        "causes": ["all_query_terms_in_name"],
    }
    assert _by_id(results, "84575")["match"] == {
        "level": "related",
        "causes": ["query_not_confirmed_by_name"],
    }


def test_la_coincidencia_se_reconoce_con_y_sin_tildes() -> None:
    # Fixture sintético: comprueba la comparación sin tildes, no el caso reportado.
    html = """
    <div class="product-tile" data-pid="140196">
      <a href="/producto-140196.html"><h2>Ácido Mefenámico 500 mg x 10 Comprimidos</h2></a>
      <div class="product-price">$2.990</div>
    </div>
    """
    for query in ("acido mefenamico", "Ácido Mefenámico"):
        results = ahumada.parse_search_results(html, query=query)
        assert [item["chain_item_id"] for item in results] == ["140196"]
        assert results[0]["match"]["level"] == "direct"


def test_sin_query_el_parser_no_evalua_la_coincidencia(results: list[dict]) -> None:
    assert all(item["match"] == {"level": "not_evaluated", "causes": []} for item in results)


@pytest.mark.asyncio
async def test_la_fuente_en_vivo_rechaza_url_no_http() -> None:
    with pytest.raises(ahumada.AhumadaFetchError):
        await ahumada.fetch_search_html("paracetamol", url="file:///etc/passwd")
