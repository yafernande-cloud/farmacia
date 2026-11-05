"""E15 — hora del sitio de Salcobrand: desfase medido una vez y reutilizado (sin red)."""

import time
from datetime import UTC, datetime

from app.services import salcobrand

URL = "https://salcobrand.cl/api/v2/preferences"
REFERER = "https://salcobrand.cl/"


def _segundos(delta) -> float:
    return delta.total_seconds()


async def test_mide_el_desfase_una_vez_y_lo_reutiliza(monkeypatch) -> None:
    llamadas = []

    async def fake_timestamp(*, url, referer, timeout_seconds):
        llamadas.append((url, referer))
        return int(time.time()) + 120

    monkeypatch.setattr(salcobrand, "fetch_current_timestamp", fake_timestamp)
    reloj = salcobrand.SiteClock(ttl_seconds=600)

    primero = await reloj.now(url=URL, referer=REFERER, timeout_seconds=5)
    segundo = await reloj.now(url=URL, referer=REFERER, timeout_seconds=5)

    assert len(llamadas) == 1
    assert llamadas[0] == (URL, REFERER)
    assert 115 <= _segundos(primero - datetime.now(UTC)) <= 125
    assert 115 <= _segundos(segundo - datetime.now(UTC)) <= 125


async def test_renueva_el_desfase_cuando_vence(monkeypatch) -> None:
    llamadas = []

    async def fake_timestamp(**kwargs):
        llamadas.append(1)
        return int(time.time())

    monkeypatch.setattr(salcobrand, "fetch_current_timestamp", fake_timestamp)
    reloj = salcobrand.SiteClock(ttl_seconds=0)

    await reloj.now(url=URL, referer=REFERER, timeout_seconds=5)
    await reloj.now(url=URL, referer=REFERER, timeout_seconds=5)

    assert len(llamadas) == 2


async def test_si_la_hora_del_sitio_falla_usa_el_reloj_local(monkeypatch) -> None:
    llamadas = []

    async def fake_timestamp(**kwargs):
        llamadas.append(1)
        raise salcobrand.SalcobrandFetchError("sin configuración")

    monkeypatch.setattr(salcobrand, "fetch_current_timestamp", fake_timestamp)
    reloj = salcobrand.SiteClock(ttl_seconds=600, failed_ttl_seconds=30)

    primera = await reloj.now(url=URL, referer=REFERER, timeout_seconds=5)
    segunda = await reloj.now(url=URL, referer=REFERER, timeout_seconds=5)

    assert abs(_segundos(primera - datetime.now(UTC))) < 2
    assert abs(_segundos(segunda - datetime.now(UTC))) < 2
    # El fallo se recuerda brevemente para no golpear la configuración en cada búsqueda.
    assert len(llamadas) == 1


async def test_un_sello_inverosimil_no_desplaza_el_reloj(monkeypatch) -> None:
    async def fake_timestamp(**kwargs):
        return int(time.time()) + 10 * 86_400

    monkeypatch.setattr(salcobrand, "fetch_current_timestamp", fake_timestamp)
    reloj = salcobrand.SiteClock(ttl_seconds=600)

    ahora = await reloj.now(url=URL, referer=REFERER, timeout_seconds=5)

    assert abs(_segundos(ahora - datetime.now(UTC))) < 2


async def test_reset_olvida_el_desfase(monkeypatch) -> None:
    llamadas = []

    async def fake_timestamp(**kwargs):
        llamadas.append(1)
        return int(time.time())

    monkeypatch.setattr(salcobrand, "fetch_current_timestamp", fake_timestamp)
    reloj = salcobrand.SiteClock(ttl_seconds=600)

    await reloj.now(url=URL, referer=REFERER, timeout_seconds=5)
    reloj.reset()
    await reloj.now(url=URL, referer=REFERER, timeout_seconds=5)

    assert len(llamadas) == 2
