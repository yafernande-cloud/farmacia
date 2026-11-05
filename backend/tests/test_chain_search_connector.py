"""E15 — gate de la fuente en vivo de Ahumada y estados de la cadena (sin red)."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.v1.search import ChainSourceOut
from app.services import ahumada, chain_search
from app.services.chains import ahumada as ahumada_connector

FIXTURE = Path(__file__).parent / "fixtures" / "ahumada_search.html"


def _settings(**overrides):
    base = {
        "chain_search_enabled": [],
        "ahumada_live_search_enabled": False,
        "ahumada_live_search_url": None,
        "ahumada_live_search_timeout_seconds": 10,
        # Salcobrand tiene su propio interruptor: apagado, su conector no consulta nada.
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


@pytest.fixture(autouse=True)
def _config_del_conector(monkeypatch):
    """El conector lee su configuración desde su propio módulo, no desde el coordinador."""
    monkeypatch.setattr(ahumada_connector, "get_settings", lambda: _settings())


def _forbid_fetch(*args, **kwargs):
    raise AssertionError("No debe consultarse la fuente sin autorización explícita.")


async def test_por_defecto_no_consulta_la_fuente(monkeypatch) -> None:
    monkeypatch.setattr(ahumada_connector, "get_settings", lambda: _settings())
    monkeypatch.setattr(ahumada, "fetch_search_html", _forbid_fetch)

    source = await chain_search._source_for("ahumada", "paracetamol")

    assert source["state"] == "not_configured"
    assert source["reason"] == "access_not_authorized"
    assert source["results"] == []


async def test_habilitada_sin_url_no_consulta_la_fuente(monkeypatch) -> None:
    monkeypatch.setattr(
        ahumada_connector,
        "get_settings",
        lambda: _settings(chain_search_enabled=["ahumada"], ahumada_live_search_enabled=True),
    )
    monkeypatch.setattr(ahumada, "fetch_search_html", _forbid_fetch)

    source = await chain_search._source_for("ahumada", "paracetamol")

    assert source["state"] == "not_configured"
    assert source["reason"] == "live_source_not_configured"
    assert source["results"] == []


async def test_fuente_autorizada_inyecta_las_fichas(monkeypatch) -> None:
    html = FIXTURE.read_text(encoding="utf-8")

    async def fake_fetch(query: str, *, url: str, timeout_seconds: int) -> str:
        assert query == "paracetamol"
        assert url == "https://fuente-autorizada.example/search"
        return html

    monkeypatch.setattr(
        ahumada_connector,
        "get_settings",
        lambda: _settings(
            chain_search_enabled=["ahumada"],
            ahumada_live_search_enabled=True,
            ahumada_live_search_url="https://fuente-autorizada.example/search",
        ),
    )
    monkeypatch.setattr(ahumada, "fetch_search_html", fake_fetch)

    source = await chain_search._source_for("ahumada", "paracetamol")

    assert source["state"] == "ok"
    assert source["outcome"] == "matches"
    assert source["observed_at"] is not None
    paracetamol = next(
        item for item in source["results"] if item["chain_item_id"] == "84574"
    )
    assert paracetamol["price"]["normal"]["amount"] == "1309"
    assert paracetamol["price"]["promotional"]["amount"] == "618"
    assert paracetamol["price"]["fractional"]["amount"] == "82"


async def test_fuente_sin_coincidencias_es_no_matches(monkeypatch) -> None:
    async def fake_fetch(query: str, *, url: str, timeout_seconds: int) -> str:
        return "<html><body><main class='search-results'></main></body></html>"

    monkeypatch.setattr(
        ahumada_connector,
        "get_settings",
        lambda: _settings(
            chain_search_enabled=["ahumada"],
            ahumada_live_search_enabled=True,
            ahumada_live_search_url="https://fuente-autorizada.example/search",
        ),
    )
    monkeypatch.setattr(ahumada, "fetch_search_html", fake_fetch)

    source = await chain_search._source_for("ahumada", "inexistente")

    assert source["state"] == "ok"
    assert source["outcome"] == "no_matches"
    assert source["results"] == []


async def test_fallo_de_la_fuente_es_failed_y_no_no_matches(monkeypatch) -> None:
    async def fake_fetch(query: str, *, url: str, timeout_seconds: int) -> str:
        raise ahumada.AhumadaFetchError("bloqueo")

    monkeypatch.setattr(
        ahumada_connector,
        "get_settings",
        lambda: _settings(
            chain_search_enabled=["ahumada"],
            ahumada_live_search_enabled=True,
            ahumada_live_search_url="https://fuente-autorizada.example/search",
        ),
    )
    monkeypatch.setattr(ahumada, "fetch_search_html", fake_fetch)

    source = await chain_search._source_for("ahumada", "paracetamol")

    assert source["state"] == "failed"
    assert source["reason"] == "fetch_failed"
    assert source["outcome"] is None
    assert source["results"] == []


async def test_las_otras_cadenas_siguen_sin_autorizacion(monkeypatch) -> None:
    monkeypatch.setattr(ahumada_connector, "get_settings", lambda: _settings())

    cruz_verde = await chain_search._source_for("cruz_verde", "paracetamol")
    salcobrand = await chain_search._source_for("salcobrand", "paracetamol")

    assert cruz_verde["reason"] == "access_not_authorized"
    assert salcobrand["reason"] == "access_not_authorized"
    assert cruz_verde["results"] == [] and salcobrand["results"] == []


def test_el_esquema_de_salida_acepta_la_ficha_parseada() -> None:
    results = ahumada.parse_search_results(FIXTURE.read_text(encoding="utf-8"))
    out = ChainSourceOut.model_validate(
        {
            "chain": "ahumada",
            "state": "ok",
            "reason": None,
            "outcome": "matches",
            "observed_at": datetime.now(UTC),
            "results": results,
        }
    )
    assert out.results[0].chain_item_id == "84574"
    assert out.results[0].price["normal"]["amount"] == "1309"
    assert out.results[0].price["fractional"]["per_unit"] == "comprimido"


@pytest.mark.asyncio
async def test_store_informa_las_tres_cadenas_sin_persistir(monkeypatch) -> None:
    monkeypatch.setattr(ahumada_connector, "get_settings", lambda: _settings())
    store = chain_search.ChainSearchStore()

    started = await store.start("paracetamol", [])

    assert [source["chain"] for source in started["sources"]] == [
        "cruz_verde",
        "salcobrand",
        "ahumada",
    ]
    assert all(source["results"] == [] for source in started["sources"])
