"""E15 — consulta a Salcobrand sin red: credenciales, límites y errores de la fuente."""

from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest

from app.services import salcobrand

URL = "https://app-dsn.algolia.net/1/indexes/*/queries"
PREFERENCES_URL = "https://salcobrand.cl/api/v2/preferences"


class _Respuesta:
    def __init__(self, *, status: int = 200, payload=None, content: bytes | None = None):
        self.status_code = status
        self._payload = payload if payload is not None else {"results": []}
        self.content = content if content is not None else b"{}"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://app-dsn.algolia.net/1/indexes/*/queries")
            raise httpx.HTTPStatusError(
                "error de la fuente", request=request, response=httpx.Response(self.status_code)
            )

    def json(self):
        if self._payload is ValueError:
            raise ValueError("no es JSON")
        return self._payload


class _ClienteFalso:
    """Sustituye a httpx.AsyncClient y registra exactamente lo que se envió."""

    capturado: dict = {}
    respuesta: _Respuesta = _Respuesta()

    def __init__(self, **kwargs):
        self.capturado["cliente"] = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.capturado["url"] = url
        self.capturado["post"] = kwargs
        return self.respuesta

    async def get(self, url, **kwargs):
        self.capturado["url"] = url
        self.capturado["get"] = kwargs
        return self.respuesta


@pytest.fixture
def fuente(monkeypatch):
    _ClienteFalso.capturado = {}
    _ClienteFalso.respuesta = _Respuesta()
    monkeypatch.setattr(salcobrand.httpx, "AsyncClient", _ClienteFalso)
    return _ClienteFalso


async def _fetch(**overrides):
    parametros = dict(
        url=URL,
        app_id="APPID",
        api_key="clave-de-prueba",
        index="sb_variant_production",
        referer="https://salcobrand.cl/",
    )
    parametros.update(overrides)
    return await salcobrand.fetch_search_json("paracetamol", **parametros)


async def test_la_clave_viaja_en_cabeceras_y_nunca_en_la_url(fuente) -> None:
    await _fetch()

    enviado = fuente.capturado
    assert "clave-de-prueba" not in enviado["url"]
    assert "params" not in enviado["post"]
    assert enviado["post"]["headers"]["X-Algolia-API-Key"] == "clave-de-prueba"
    assert enviado["post"]["headers"]["X-Algolia-Application-Id"] == "APPID"
    assert enviado["post"]["headers"]["Referer"] == "https://salcobrand.cl/"
    assert enviado["cliente"]["timeout"] == salcobrand.DEFAULT_TIMEOUT_SECONDS
    assert enviado["cliente"]["follow_redirects"] is False

    peticion = enviado["post"]["json"]["requests"][0]
    assert peticion["indexName"] == "sb_variant_production"
    parametros = parse_qs(peticion["params"])
    assert parametros["query"] == ["paracetamol"]
    assert parametros["hitsPerPage"] == ["24"]
    assert parametros["page"] == ["0"]


async def test_la_busqueda_filtra_con_el_momento_recibido(fuente) -> None:
    momento = datetime(2026, 11, 5, 1, 30, tzinfo=UTC)
    await _fetch(moment=momento)

    peticion = fuente.capturado["post"]["json"]["requests"][0]
    parametros = parse_qs(peticion["params"])
    assert parametros["filters"] == ["(timestamp_available_on < 1793842200)"]


async def test_la_configuracion_entrega_el_sello_de_tiempo_del_sitio(fuente) -> None:
    fuente.respuesta = _Respuesta(payload={"current_timestamp": 1793843573})

    timestamp = await salcobrand.fetch_current_timestamp(
        url=PREFERENCES_URL, referer="https://salcobrand.cl/"
    )

    assert timestamp == 1793843573
    enviado = fuente.capturado
    assert enviado["url"] == PREFERENCES_URL
    assert enviado["get"]["headers"]["Referer"] == "https://salcobrand.cl/"
    # La configuración pública no requiere ni recibe credenciales de Algolia.
    assert not any("algolia" in clave.lower() for clave in enviado["get"]["headers"])


@pytest.mark.parametrize(
    "payload",
    [{}, {"current_timestamp": None}, {"current_timestamp": "1793843573"}, {"current_timestamp": True}],
)
async def test_un_sello_de_tiempo_invalido_es_fallo_de_fuente(fuente, payload) -> None:
    fuente.respuesta = _Respuesta(payload=payload)

    with pytest.raises(salcobrand.SalcobrandFetchError):
        await salcobrand.fetch_current_timestamp(
            url=PREFERENCES_URL, referer="https://salcobrand.cl/"
        )


async def test_un_error_http_de_la_configuracion_es_fallo_de_fuente(fuente) -> None:
    fuente.respuesta = _Respuesta(status=500)

    with pytest.raises(salcobrand.SalcobrandFetchError):
        await salcobrand.fetch_current_timestamp(
            url=PREFERENCES_URL, referer="https://salcobrand.cl/"
        )


async def test_un_error_no_expone_la_clave(fuente) -> None:
    fuente.respuesta = _Respuesta(status=403)

    with pytest.raises(salcobrand.SalcobrandFetchError) as excinfo:
        await _fetch()

    assert "clave-de-prueba" not in str(excinfo.value)
    assert "clave-de-prueba" not in repr(excinfo.value.__cause__)


async def test_una_url_no_http_se_rechaza_sin_consultar(fuente) -> None:
    with pytest.raises(salcobrand.SalcobrandFetchError):
        await _fetch(url="ftp://app-dsn.algolia.net/1/indexes/*/queries")
    assert fuente.capturado == {}


async def test_una_consulta_vacia_se_rechaza_sin_consultar(fuente) -> None:
    with pytest.raises(salcobrand.SalcobrandFetchError):
        await salcobrand.fetch_search_json(
            "   ",
            url=URL,
            app_id="APPID",
            api_key="clave-de-prueba",
            index="sb_variant_production",
            referer="https://salcobrand.cl/",
        )
    assert fuente.capturado == {}


async def test_un_error_http_es_fallo_de_fuente(fuente) -> None:
    fuente.respuesta = _Respuesta(status=403)

    with pytest.raises(salcobrand.SalcobrandFetchError):
        await _fetch()


async def test_un_json_invalido_es_fallo_de_fuente(fuente) -> None:
    fuente.respuesta = _Respuesta(payload=ValueError)

    with pytest.raises(salcobrand.SalcobrandFetchError):
        await _fetch()


async def test_una_respuesta_demasiado_grande_es_fallo_de_fuente(fuente) -> None:
    fuente.respuesta = _Respuesta(content=b"x" * (salcobrand.MAX_RESPONSE_BYTES + 1))

    with pytest.raises(salcobrand.SalcobrandFetchError):
        await _fetch()


async def test_una_respuesta_que_no_es_objeto_es_fallo_de_fuente(fuente) -> None:
    fuente.respuesta = _Respuesta(payload=[1, 2, 3])

    with pytest.raises(salcobrand.SalcobrandFetchError):
        await _fetch()
