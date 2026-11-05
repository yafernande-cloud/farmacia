"""E15 — normalización de Salcobrand con un JSON de prueba acotado (sin red).

El JSON imita la forma real de Algolia verificada el 2026-11-05, pero sus valores son de prueba:
no representan precios vigentes ni se usan como fuente de datos.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services import salcobrand

OBSERVED_AT = datetime(2026, 11, 5, 1, 30, tzinfo=UTC)


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
        "direct_discount_sbpay": "777.0",
        "catalog_image_url": "https://static.salcobrand.cl/spree/products/169994/small/430924.jpg",
        "has_stock": True,
        "prices_by_um": {"offer_price": "$41.580 x 24 UN"},
        "ribbon_info": {
            "badge": "-33%",
            "price": "$998",
            "promotion_label": "Directo",
        },
    }
    base.update(overrides)
    return base


def _payload(*hits):
    return {"results": [{"hits": list(hits), "nbHits": len(hits), "page": 0}]}


def test_normaliza_los_campos_publicados() -> None:
    [product] = salcobrand.parse_search_results(_payload(_hit()), observed_at=OBSERVED_AT)

    assert product["chain_item_id"] == "430924"
    assert product["published_name"] == "Kitadol (B) Paracetamol 500mg 24 Comprimidos"
    assert product["brand_as_published"] == "Kitadol"
    # La marca no se presenta como laboratorio.
    assert product["laboratory_as_published"] is None
    assert product["presentation_as_published"] == "24 Comprimidos"
    assert product["image_url"] == (
        "https://static.salcobrand.cl/spree/products/169994/small/430924.jpg"
    )
    assert product["price"]["normal"] == {"amount": "1499", "currency": "CLP"}
    assert product["price"]["promotional"] == {"amount": "998", "currency": "CLP"}
    assert product["conditions"] == "Directo"
    assert product["has_stock"] is True
    assert product["source_url"] == (
        "https://salcobrand.cl/products/"
        "kitadol-b-paracetamol-500mg-24-comprimidos?default_sku=430924"
    )
    assert product["observed_at"] == OBSERVED_AT.isoformat()
    assert product["provenance"] == {
        "source": "salcobrand",
        "data_origin": "published",
        "parser_version": salcobrand.CONNECTOR_VERSION,
    }
    assert product["match"] == {"level": "not_evaluated", "causes": []}


def test_sin_oferta_general_solo_hay_precio_normal() -> None:
    [product] = salcobrand.parse_search_results(
        _payload(
            _hit(
                direct_discount=None,
                direct_discount_sbpay="777.0",
                ribbon_info={"badge": None, "price": "$1.499", "promotion_label": None},
            )
        ),
        observed_at=OBSERVED_AT,
    )

    assert product["price"]["normal"] == {"amount": "1499", "currency": "CLP"}
    assert product["price"]["promotional"] is None
    assert product["conditions"] is None


def test_sbpay_no_se_usa_ni_se_expone() -> None:
    [product] = salcobrand.parse_search_results(_payload(_hit()), observed_at=OBSERVED_AT)

    serializado = json.dumps(product, ensure_ascii=False)
    assert "777" not in serializado
    assert "sbpay" not in serializado.lower()
    # La oferta general es la publicada, no el precio con tarjeta.
    assert product["price"]["promotional"]["amount"] == "998"


def test_prices_by_um_no_se_interpreta_como_precio_fraccionado() -> None:
    [product] = salcobrand.parse_search_results(_payload(_hit()), observed_at=OBSERVED_AT)

    assert product["price"]["fractional"] is None
    assert "41.580" not in json.dumps(product)


def test_omite_variantes_sin_nombre_sku_o_enlace() -> None:
    productos = salcobrand.parse_search_results(
        _payload(
            _hit(),
            _hit(sku=None, objectID=None),
            _hit(name=None),
            _hit(slug=None),
        ),
        observed_at=OBSERVED_AT,
    )

    assert [p["chain_item_id"] for p in productos] == ["430924"]


def test_respeta_el_limite_de_24_sin_paginar() -> None:
    hits = [_hit(sku=str(1000 + i), slug=f"producto-{i}") for i in range(30)]

    productos = salcobrand.parse_search_results(_payload(*hits), observed_at=OBSERVED_AT)

    assert len(productos) == salcobrand.DEFAULT_LIMIT == 24


def test_hits_vacios_son_lista_vacia_y_no_un_error() -> None:
    assert salcobrand.parse_search_results(_payload(), observed_at=OBSERVED_AT) == []


def test_respuesta_sin_resultados_es_error_de_fuente() -> None:
    with pytest.raises(salcobrand.SalcobrandFetchError):
        salcobrand.parse_search_results({}, observed_at=OBSERVED_AT)
    with pytest.raises(salcobrand.SalcobrandFetchError):
        salcobrand.parse_search_results({"results": []}, observed_at=OBSERVED_AT)
    with pytest.raises(salcobrand.SalcobrandFetchError):
        salcobrand.parse_search_results({"results": [{"hits": "no"}]}, observed_at=OBSERVED_AT)


def test_importes_invalidos_no_se_convierten_en_precio() -> None:
    [product] = salcobrand.parse_search_results(
        _payload(_hit(normal_price="no es un precio", direct_discount=[1])),
        observed_at=OBSERVED_AT,
    )

    assert product["price"]["normal"] is None
    assert product["price"]["promotional"] is None
    assert product["conditions"] is None


def test_los_parametros_usan_el_momento_de_la_consulta() -> None:
    params = salcobrand.search_params(
        " paracetamol ", moment=datetime(2026, 11, 5, 1, 30, tzinfo=UTC)
    )

    assert params["query"] == "paracetamol"
    assert params["hitsPerPage"] == "24"
    assert params["page"] == "0"
    assert params["filters"] == "(timestamp_available_on < 1793842200)"


def test_importes_decimales_se_normalizan_sin_float() -> None:
    assert salcobrand._money(Decimal("998.0")) == {"amount": "998", "currency": "CLP"}
    assert salcobrand._money(Decimal("998.50")) == {"amount": "998.5", "currency": "CLP"}
