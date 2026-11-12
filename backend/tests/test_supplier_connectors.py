"""Motor declarativo de proveedores: estrategia Algolia pura, sin red.

El transporte HTTP es `httpx.MockTransport`: ninguna prueba sale a Internet, ninguna usa una clave
real y ninguna escribe ofertas. Los perfiles se validan antes de ejecutarse.
"""

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.core.errors import DomainError
from app.services.connectors.algolia import AlgoliaStrategy
from app.services.connectors.base import (
    FINGERPRINT_VERSION,
    ConnectorError,
    ConnectorIntervention,
    ConnectorNotConfigured,
    fingerprints_comparable,
)
from app.services.connectors.config import validate_profile
from app.services.connectors.presets import preset


def farmaloop_hit(**over):
    hit = {
        "sku": "PRE-001",
        "fullName": "Predual 30 mg comprimidos recubiertos",
        "laboratoryName": "Laboratorio Ficticio",
        "presentation": "Caja 30 comprimidos",
        "bestPrice": 12990,
        "basePrice": 15990,
        "photoURL": "https://img.farmaloop.test/predual.jpg",
        "slug": "predual-30-mg",
        "ean": "7801234567890",
        "batchs": [{"stock": 5}, {"stock": 3}],
        # Dato de cuenta: no está en el mapeo y no debe salir.
        "unitCost": 9999,
    }
    hit.update(over)
    return hit


def response(items, status=200):
    return httpx.Response(status, json={"hits": items, "nbHits": len(items)})


def profile(*, with_stock_unit=False):
    base = preset("farmaloop")
    if with_stock_unit:
        base["config"]["stock_unit"] = "unit"
    return base


async def search(handler, *, cfg=None, limit=10, query="predual"):
    base = cfg or profile()
    return await AlgoliaStrategy().search(
        query,
        config=base["config"],
        field_map=base["field_map"],
        limit=limit,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv("FARMALOOP_ALGOLIA_APP_ID", "APPFICTICIA")
    monkeypatch.setenv("FARMALOOP_ALGOLIA_API_KEY", "clave-publica-de-prueba")
    yield


async def test_algolia_lee_las_ofertas_y_no_expone_la_clave():
    seen = {}

    async def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read().decode()
        return response([farmaloop_hit()])

    result = await search(handler)

    assert len(result.offers) == 1
    offer = result.offers[0]
    assert offer.external_id == "PRE-001"
    assert offer.name.startswith("Predual")
    assert offer.laboratory == "Laboratorio Ficticio"
    assert offer.presentation == "Caja 30 comprimidos"
    assert offer.price == Decimal("12990")
    assert offer.currency == "CLP"
    # El portal no informó unidad de venta ni base: quedan desconocidas, no se inventan.
    assert offer.sale_unit is None
    assert offer.price_basis is None
    assert offer.stock_status == "unknown"
    assert offer.stock_quantity is None
    assert offer.stock_unit is None
    assert offer.image_url == "https://img.farmaloop.test/predual.jpg"
    assert offer.ean == "7801234567890"
    assert not hasattr(offer, "unit_cost")
    assert "9999" not in repr(offer)

    assert "appficticia-dsn.algolia.net" in seen["url"].lower()
    assert "/f_products_prod/query" in seen["url"]
    assert seen["headers"]["x-algolia-api-key"] == "clave-publica-de-prueba"
    assert seen["headers"]["x-algolia-application-id"] == "APPFICTICIA"
    assert '"hitsPerPage":10' in seen["body"].replace(" ", "")
    assert result.fingerprint


async def test_algolia_publica_sin_precio_en_vez_de_descartar():
    """Un producto con identidad válida y sin precio se muestra aparte; no es una oferta."""
    async def handler(_: httpx.Request):
        return response(
            [
                farmaloop_hit(),
                farmaloop_hit(sku="PRE-002", bestPrice=None, availableForSale=0),
            ]
        )

    result = await search(handler)

    assert [o.external_id for o in result.offers] == ["PRE-001"]
    assert [p.external_id for p in result.published] == ["PRE-002"]
    product = result.published[0]
    assert product.name.startswith("Predual")
    # El proveedor informó que no está disponible: «sin stock», no un dato inventado.
    assert product.stock_status == "out_of_stock"
    # El precio de lista se conserva aparte, solo como referencia.
    assert product.reference_price == Decimal("15990")
    assert result.discarded == 0
    # Los descartes ya no viajan como advertencia visible.
    assert result.warnings == []


async def test_algolia_publicado_sin_disponibilidad_informada_no_inventa_stock():
    async def handler(_: httpx.Request):
        return response([farmaloop_hit(bestPrice=0, availableForSale=None, basePrice=0)])

    result = await search(handler)

    product = result.published[0]
    assert product.stock_status == "unknown"
    assert product.reference_price is None


async def test_algolia_oferta_no_disponible_se_muestra_con_su_estado_real():
    async def handler(_: httpx.Request):
        return response(
            [farmaloop_hit(availableForSale=0), farmaloop_hit(sku="PRE-003", availableForSale=1)]
        )

    result = await search(handler)

    assert [o.stock_status for o in result.offers] == ["out_of_stock", "in_stock"]
    assert [o.external_id for o in result.published] == []


async def test_algolia_descarta_items_sin_identidad_aunque_no_tengan_precio():
    async def handler(_: httpx.Request):
        return response(
            [
                farmaloop_hit(),
                farmaloop_hit(sku="", fullName="", bestPrice=0),
                farmaloop_hit(sku="PRE-002", bestPrice="no es un precio"),
            ]
        )

    result = await search(handler)

    assert [o.external_id for o in result.offers] == ["PRE-001"]
    # `PRE-002` tiene identidad válida y precio ilegible: se publica sin precio.
    assert [p.external_id for p in result.published] == ["PRE-002"]
    assert result.discarded == 1


async def test_algolia_la_oferta_gana_sobre_la_publicacion_del_mismo_sku():
    async def handler(_: httpx.Request):
        return response(
            [farmaloop_hit(sku="PRE-001", bestPrice=0), farmaloop_hit(sku="PRE-001")]
        )

    result = await search(handler)

    assert [o.external_id for o in result.offers] == ["PRE-001"]
    assert result.published == []


async def test_algolia_sin_nada_legible_pide_intervencion():
    async def handler(_: httpx.Request):
        return response([farmaloop_hit(sku="", fullName="")])

    with pytest.raises(ConnectorIntervention) as exc:
        await search(handler)
    assert exc.value.reason == "field_mapping_failed"


async def test_algolia_respuesta_sin_hits_pide_intervencion():
    async def handler(_: httpx.Request):
        return httpx.Response(200, json={"nbHits": 0})

    with pytest.raises(ConnectorIntervention) as exc:
        await search(handler)
    assert exc.value.reason == "schema_changed"


async def test_algolia_json_invalido_pide_intervencion():
    async def handler(_: httpx.Request):
        return httpx.Response(200, content=b"<html>bloqueo</html>")

    with pytest.raises(ConnectorIntervention) as exc:
        await search(handler)
    assert exc.value.reason == "invalid_response"


@pytest.mark.parametrize(
    ("status", "error", "reason"),
    [
        (401, ConnectorIntervention, "credentials_rejected"),
        (403, ConnectorIntervention, "credentials_rejected"),
        (429, ConnectorError, "rate_limited"),
        (500, ConnectorError, "provider_error"),
        (400, ConnectorIntervention, "request_rejected"),
    ],
)
async def test_algolia_traduce_estados_http(status, error, reason):
    async def handler(_: httpx.Request):
        return httpx.Response(status, json={"message": "detalle del proveedor"})

    with pytest.raises(error) as exc:
        await search(handler)
    assert exc.value.reason == reason


async def test_algolia_sin_secreto_no_consulta_ni_filtra_el_valor(monkeypatch):
    monkeypatch.delenv("FARMALOOP_ALGOLIA_API_KEY")

    async def handler(_: httpx.Request):  # nunca debe ejecutarse
        raise AssertionError("no debe hacerse la solicitud")

    with pytest.raises(ConnectorNotConfigured) as exc:
        await search(handler)
    assert exc.value.reason == "secret_missing:FARMALOOP_ALGOLIA_API_KEY"


async def test_algolia_corta_respuestas_demasiado_grandes():
    async def handler(_: httpx.Request):
        return httpx.Response(200, content=b"{" + b"0" * 20_000 + b"}")

    base = profile()
    base["config"]["max_response_bytes"] = 10_000
    with pytest.raises(ConnectorIntervention) as exc:
        await search(handler, cfg=base)
    assert exc.value.reason == "response_too_large"


async def test_algolia_expone_stock_solo_si_la_unidad_esta_confirmada():
    async def handler(_: httpx.Request):
        return response([farmaloop_hit()])

    result = await search(handler, cfg=profile(with_stock_unit=True))
    offer = result.offers[0]
    assert offer.stock_status == "in_stock"
    assert offer.stock_quantity == Decimal("8")
    assert offer.stock_unit == "unit"


async def test_algolia_permite_plantilla_para_url_de_ficha():
    base = profile()
    base["field_map"]["product_url"] = {
        "template": "https://tienda.test/producto/{slug}",
        "type": "url",
    }

    async def handler(_: httpx.Request):
        return response([farmaloop_hit()])

    result = await search(handler, cfg=base)
    assert result.offers[0].product_url == "https://tienda.test/producto/predual-30-mg"


async def test_algolia_url_de_ficha_invalida_no_se_inventa():
    base = profile()
    base["field_map"]["product_url"] = {
        "template": "javascript:alert('{slug}')",
        "type": "url",
    }

    async def handler(_: httpx.Request):
        return response([farmaloop_hit()])

    result = await search(handler, cfg=base)
    assert result.offers[0].product_url is None


async def test_la_huella_no_cambia_entre_consultas_con_productos_distintos():
    """El contenido de cada producto no es estructura: no puede marcar «requiere revisión».

    Productos de otra consulta pueden traer campos opcionales ausentes, lotes vacíos o metadatos
    de coincidencia distintos. El sobre de la respuesta sigue siendo el mismo.
    """

    async def completa(_: httpx.Request):
        return response([farmaloop_hit()])

    async def dispersa(_: httpx.Request):
        item = farmaloop_hit(presentation=None, ean=None, batchs=[])
        item["_highlightResult"] = {
            "fullName": {
                "value": "<em>Predual</em> 30 mg",
                "matchLevel": "full",
                "matchedWords": ["predual"],
            }
        }
        return response([item])

    primera = await search(completa, query="predual")
    segunda = await search(dispersa, query="amoxicilina 500 mg")

    assert primera.fingerprint == segunda.fingerprint
    assert primera.fingerprint is not None
    assert primera.fingerprint.startswith(FINGERPRINT_VERSION)
    assert len(primera.fingerprint) == 64  # cabe en la columna sin migración
    # El precio sí cambia entre consultas; la huella no.

    async def otro_precio(_: httpx.Request):
        return response([farmaloop_hit(bestPrice=9990)])

    tercera = await search(otro_precio, query="ibuprofeno 400 mg")
    assert tercera.fingerprint == primera.fingerprint


async def test_la_huella_cambia_si_cambia_el_sobre_de_la_respuesta():
    async def normal(_: httpx.Request):
        return response([farmaloop_hit()])

    async def con_sobre_nuevo(_: httpx.Request):
        return httpx.Response(
            200,
            json={
                "hits": [farmaloop_hit()],
                "renderingContent": {"facetOrdering": {}},
            },
        )

    primera = await search(normal)
    segunda = await search(con_sobre_nuevo, query="otra consulta")
    assert primera.fingerprint != segunda.fingerprint


async def test_una_lista_vacia_es_sin_resultados_con_sobre_valido():
    async def vacia(_: httpx.Request):
        return response([])

    result = await search(vacia, query="producto sin coincidencias")
    assert result.offers == [] and result.published == []
    assert result.fingerprint is not None
    assert any("no devolvió resultados" in w.lower() for w in result.warnings)


def test_una_huella_de_contenido_anterior_no_se_compara_con_la_del_sobre():
    """Al desplegar la corrección, la huella vieja se reemplaza sin una falsa alarma."""

    assert fingerprints_comparable("v2" + "a" * 62, "v2" + "b" * 62)
    assert not fingerprints_comparable("a" * 64, "v2" + "b" * 62)
    assert not fingerprints_comparable(None, "v2" + "b" * 62)


def test_el_preset_de_farmaloop_declara_sus_limites():
    mechanism, config, field_map = validate_profile(**preset("farmaloop"))
    assert mechanism == "algolia"
    assert config["index"] == "f_products_prod"
    assert config["currency"] == "CLP"
    # No hay unidad de venta, base de precio ni unidad de stock confirmadas.
    assert "sale_unit" not in config
    assert "price_basis" not in config
    assert "stock_unit" not in config
    assert "sale_unit" not in field_map
    assert "unitCost" not in repr(field_map)
    assert field_map["price"]["type"] == "decimal"
    # Disponibilidad explícita y precio de lista: nunca se usan como precio de venta.
    assert field_map["available_for_sale"]["path"] == "availableForSale"
    assert field_map["available_for_sale"]["type"] == "bool"
    assert field_map["reference_price"]["path"] == "basePrice"
    assert field_map["reference_price"]["type"] == "decimal"
    assert field_map["price"]["path"] == "bestPrice"


def test_un_perfil_sin_moneda_se_rechaza():
    base = preset("farmaloop")
    base["field_map"].pop("currency")
    base["config"].pop("currency")
    with pytest.raises(DomainError) as exc:
        validate_profile(**base)
    assert "moneda" in str(exc.value).lower()


def test_un_campo_con_dos_fuentes_se_rechaza():
    base = preset("farmaloop")
    base["field_map"]["price"] = {"path": "bestPrice", "const": 1}
    with pytest.raises(DomainError):
        validate_profile(**base)


def test_un_campo_con_tipo_incorrecto_se_rechaza():
    base = preset("farmaloop")
    base["field_map"]["price"]["type"] = "string"
    with pytest.raises(DomainError) as exc:
        validate_profile(**base)
    assert "price" in str(exc.value)


async def test_algolia_no_sigue_redirecciones():
    async def handler(_: httpx.Request):
        return httpx.Response(302, headers={"Location": "https://otro.test/"})

    with pytest.raises(ConnectorIntervention) as exc:
        await search(handler)
    assert exc.value.reason == "unexpected_redirect"


def test_un_mecanismo_sin_estrategia_no_se_guarda():
    with pytest.raises(DomainError) as exc:
        validate_profile("rest_json", {}, {})
    assert "rest_json" in str(exc.value)


async def test_algolia_observa_en_utc():
    async def handler(_: httpx.Request):
        return response([farmaloop_hit()])

    before = datetime.now(UTC)
    result = await search(handler)
    assert result.observed_at >= before
    assert result.observed_at.tzinfo is not None
