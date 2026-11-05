"""E15 — independencia funcional y técnica entre cadenas (sin red).

Cada conector tiene su interruptor, su configuración y su fuente. Estas pruebas comprueban que un
fallo de una cadena no cambia el resultado de la otra y que una cadena apagada no hace red.
"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.v1.search import ChainSourceOut
from app.services import ahumada, chain_search, salcobrand
from app.services.chains import ahumada as ahumada_connector
from app.services.chains import salcobrand as salcobrand_connector

FIXTURE = Path(__file__).parent / "fixtures" / "ahumada_search.html"
AHUMADA_URL = "https://fuente-autorizada.example/search"
SALCOBRAND_URL = "https://algolia.example/1/indexes/*/queries"
MOMENTO = datetime(2026, 11, 5, 1, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reloj_del_sitio(monkeypatch):
    """La hora del filtro se prueba aparte: aquí solo interesa la independencia entre cadenas."""

    async def fake_now(**kwargs):
        return MOMENTO

    monkeypatch.setattr(salcobrand.clock, "now", fake_now)


class _Clave:
    def get_secret_value(self) -> str:
        return "clave-de-prueba"


def _settings(**overrides):
    base = {
        "chain_search_enabled": [],
        "ahumada_live_search_enabled": False,
        "ahumada_live_search_url": None,
        "ahumada_live_search_timeout_seconds": 10,
        "salcobrand_live_search_enabled": False,
        "salcobrand_live_search_url": None,
        "salcobrand_live_search_app_id": None,
        "salcobrand_live_search_api_key": None,
        "salcobrand_live_search_index": "sb_variant_production",
        "salcobrand_live_search_referer": "https://salcobrand.cl/",
        "salcobrand_live_search_timeout_seconds": 10,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _ambas_habilitadas(**overrides):
    base = dict(
        chain_search_enabled=["ahumada", "salcobrand"],
        ahumada_live_search_enabled=True,
        ahumada_live_search_url=AHUMADA_URL,
        salcobrand_live_search_enabled=True,
        salcobrand_live_search_url=SALCOBRAND_URL,
        salcobrand_live_search_app_id="APPID",
        salcobrand_live_search_api_key=_Clave(),
    )
    base.update(overrides)
    return _settings(**base)


def _payload(*hits):
    return {"results": [{"hits": list(hits), "nbHits": len(hits), "page": 0}]}


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
        "ribbon_info": {"price": "$998", "promotion_label": "Directo"},
    }
    base.update(overrides)
    return base


def _config(monkeypatch, *, ahumada_settings, salcobrand_settings) -> None:
    monkeypatch.setattr(ahumada_connector, "get_settings", lambda: ahumada_settings)
    monkeypatch.setattr(salcobrand_connector, "get_settings", lambda: salcobrand_settings)


def _no_red(*args, **kwargs):
    raise AssertionError("Una cadena apagada no debe consultar su fuente.")


async def test_salcobrand_responde_aunque_ahumada_este_apagada(monkeypatch) -> None:
    _config(
        monkeypatch,
        ahumada_settings=_settings(),
        salcobrand_settings=_settings(
            chain_search_enabled=["salcobrand"],
            salcobrand_live_search_enabled=True,
            salcobrand_live_search_url=SALCOBRAND_URL,
            salcobrand_live_search_app_id="APPID",
            salcobrand_live_search_api_key=_Clave(),
        ),
    )

    async def fake_salcobrand(query, **kwargs):
        assert query == "paracetamol"
        return _payload(_hit())

    monkeypatch.setattr(salcobrand, "fetch_search_json", fake_salcobrand)
    monkeypatch.setattr(ahumada, "fetch_search_html", _no_red)

    source = await chain_search._source_for("salcobrand", "paracetamol")
    ahumada_source = await chain_search._source_for("ahumada", "paracetamol")

    assert source["state"] == "ok"
    assert source["outcome"] == "matches"
    assert [item["chain_item_id"] for item in source["results"]] == ["430924"]
    assert ahumada_source["state"] == "not_configured"
    assert ahumada_source["reason"] == "access_not_authorized"
    assert ahumada_source["results"] == []


async def test_fallo_de_ahumada_no_afecta_a_salcobrand(monkeypatch) -> None:
    _config(
        monkeypatch,
        ahumada_settings=_ambas_habilitadas(),
        salcobrand_settings=_ambas_habilitadas(),
    )

    async def ahumada_rota(query, *, url, timeout_seconds):
        raise ahumada.AhumadaFetchError("bloqueo")

    async def salcobrand_ok(query, **kwargs):
        return _payload(_hit())

    monkeypatch.setattr(ahumada, "fetch_search_html", ahumada_rota)
    monkeypatch.setattr(salcobrand, "fetch_search_json", salcobrand_ok)

    store = chain_search.ChainSearchStore()
    snapshot = await store.start("paracetamol", [])
    cadenas = {source["chain"]: source for source in snapshot["sources"]}

    assert cadenas["ahumada"]["state"] == "failed"
    assert cadenas["ahumada"]["reason"] == "fetch_failed"
    assert cadenas["ahumada"]["results"] == []
    assert cadenas["salcobrand"]["state"] == "ok"
    assert cadenas["salcobrand"]["outcome"] == "matches"
    assert len(cadenas["salcobrand"]["results"]) == 1


async def test_fallo_de_salcobrand_no_afecta_a_ahumada(monkeypatch) -> None:
    _config(
        monkeypatch,
        ahumada_settings=_ambas_habilitadas(),
        salcobrand_settings=_ambas_habilitadas(),
    )

    async def salcobrand_rota(query, **kwargs):
        raise salcobrand.SalcobrandFetchError("respuesta inválida")

    async def ahumada_ok(query, *, url, timeout_seconds):
        return FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(salcobrand, "fetch_search_json", salcobrand_rota)
    monkeypatch.setattr(ahumada, "fetch_search_html", ahumada_ok)

    store = chain_search.ChainSearchStore()
    snapshot = await store.start("paracetamol", [])
    cadenas = {source["chain"]: source for source in snapshot["sources"]}

    assert cadenas["salcobrand"]["state"] == "failed"
    assert cadenas["salcobrand"]["reason"] == "fetch_failed"
    assert cadenas["salcobrand"]["results"] == []
    assert cadenas["ahumada"]["state"] == "ok"
    assert cadenas["ahumada"]["outcome"] == "matches"
    assert cadenas["ahumada"]["results"]
    # Las fichas de una cadena nunca aparecen en la otra.
    assert all(
        item["provenance"]["source"] == "farmacias_ahumada"
        for item in cadenas["ahumada"]["results"]
    )


async def test_una_excepcion_inesperada_de_salcobrand_queda_contenida(monkeypatch) -> None:
    _config(
        monkeypatch,
        ahumada_settings=_ambas_habilitadas(),
        salcobrand_settings=_ambas_habilitadas(),
    )

    async def salcobrand_rota(query, **kwargs):
        raise RuntimeError("error no previsto")

    async def ahumada_ok(query, *, url, timeout_seconds):
        return FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(salcobrand, "fetch_search_json", salcobrand_rota)
    monkeypatch.setattr(ahumada, "fetch_search_html", ahumada_ok)

    salcobrand_source = await chain_search._source_for("salcobrand", "paracetamol")
    ahumada_source = await chain_search._source_for("ahumada", "paracetamol")

    assert salcobrand_source["state"] == "failed"
    assert salcobrand_source["results"] == []
    assert ahumada_source["state"] == "ok"


async def test_sin_interruptor_salcobrand_no_hace_ninguna_solicitud(monkeypatch) -> None:
    _config(
        monkeypatch,
        ahumada_settings=_settings(),
        salcobrand_settings=_settings(
            chain_search_enabled=["salcobrand"],
            salcobrand_live_search_enabled=False,
            salcobrand_live_search_url=SALCOBRAND_URL,
            salcobrand_live_search_app_id="APPID",
            salcobrand_live_search_api_key=_Clave(),
        ),
    )
    monkeypatch.setattr(salcobrand, "fetch_search_json", _no_red)

    source = await chain_search._source_for("salcobrand", "paracetamol")

    assert source["state"] == "not_configured"
    assert source["reason"] == "access_not_authorized"
    assert source["results"] == []


async def test_sin_interruptor_ahumada_no_hace_ninguna_solicitud(monkeypatch) -> None:
    _config(
        monkeypatch,
        ahumada_settings=_settings(
            chain_search_enabled=["ahumada"],
            ahumada_live_search_enabled=False,
            ahumada_live_search_url=AHUMADA_URL,
        ),
        salcobrand_settings=_ambas_habilitadas(),
    )
    monkeypatch.setattr(ahumada, "fetch_search_html", _no_red)
    monkeypatch.setattr(salcobrand, "fetch_search_json", _no_red)

    source = await chain_search._source_for("ahumada", "paracetamol")

    assert source["state"] == "not_configured"
    assert source["reason"] == "access_not_authorized"


async def test_salcobrand_habilitada_sin_credenciales_no_consulta(monkeypatch) -> None:
    _config(
        monkeypatch,
        ahumada_settings=_settings(),
        salcobrand_settings=_settings(
            chain_search_enabled=["salcobrand"],
            salcobrand_live_search_enabled=True,
            salcobrand_live_search_url=SALCOBRAND_URL,
            salcobrand_live_search_app_id="APPID",
            salcobrand_live_search_api_key=None,
        ),
    )
    monkeypatch.setattr(salcobrand, "fetch_search_json", _no_red)

    source = await chain_search._source_for("salcobrand", "paracetamol")

    assert source["state"] == "not_configured"
    assert source["reason"] == "live_source_not_configured"


async def test_cada_cadena_informa_su_propia_fuente(monkeypatch) -> None:
    _config(
        monkeypatch,
        ahumada_settings=_ambas_habilitadas(),
        salcobrand_settings=_ambas_habilitadas(),
    )

    async def salcobrand_ok(query, **kwargs):
        return _payload(_hit())

    async def ahumada_ok(query, *, url, timeout_seconds):
        return FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(salcobrand, "fetch_search_json", salcobrand_ok)
    monkeypatch.setattr(ahumada, "fetch_search_html", ahumada_ok)

    store = chain_search.ChainSearchStore()
    snapshot = await store.start("paracetamol", ["ahumada", "salcobrand"])
    cadenas = {source["chain"]: source for source in snapshot["sources"]}

    assert set(cadenas) == {"ahumada", "salcobrand"}
    assert all(
        item["provenance"]["source"] == "salcobrand"
        for item in cadenas["salcobrand"]["results"]
    )
    assert all(
        item["provenance"]["source"] == "farmacias_ahumada"
        for item in cadenas["ahumada"]["results"]
    )


def test_el_esquema_comun_acepta_fichas_de_salcobrand() -> None:
    results = salcobrand.parse_search_results(_payload(_hit()))

    out = ChainSourceOut.model_validate(
        {
            "chain": "salcobrand",
            "state": "ok",
            "reason": None,
            "outcome": "matches",
            "observed_at": results[0]["observed_at"],
            "results": results,
        }
    )

    ficha = out.results[0]
    assert ficha.brand_as_published == "Kitadol"
    assert ficha.laboratory_as_published is None
    assert ficha.has_stock is True
    assert ficha.source_url.endswith("?default_sku=430924")


@pytest.mark.parametrize("cadena", ["ahumada", "salcobrand"])
async def test_una_cadena_apagada_no_impide_el_resultado_de_la_otra(
    monkeypatch, cadena: str
) -> None:
    apagada = _settings()
    encendida = _ambas_habilitadas()
    _config(
        monkeypatch,
        ahumada_settings=apagada if cadena == "ahumada" else encendida,
        salcobrand_settings=apagada if cadena == "salcobrand" else encendida,
    )

    async def salcobrand_ok(query, **kwargs):
        return _payload(_hit())

    async def ahumada_ok(query, *, url, timeout_seconds):
        return FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(salcobrand, "fetch_search_json", salcobrand_ok)
    monkeypatch.setattr(ahumada, "fetch_search_html", ahumada_ok)

    store = chain_search.ChainSearchStore()
    snapshot = await store.start("paracetamol", [])
    cadenas = {source["chain"]: source for source in snapshot["sources"]}

    otra = "salcobrand" if cadena == "ahumada" else "ahumada"
    assert cadenas[cadena]["state"] == "not_configured"
    assert cadenas[otra]["state"] == "ok"
