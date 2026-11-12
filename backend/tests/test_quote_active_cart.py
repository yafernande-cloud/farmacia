"""Carrito activo: un solo borrador vigente por organización (corrección 2026-11-12).

Reconstruye el caso de producción: la cotización más reciente se finaliza y existe un borrador
anterior. Ese borrador **no se borra ni se edita** —sigue disponible por id— pero deja de
recuperarse automáticamente como carrito activo, porque pertenece a una generación anterior del
carrito. La selección del carrito vigente es una sola regla, aplicada por el servidor:

    borrador (`status` = `draft`, sin `finalized_at`) creado después del último cierre

Además se comprueba que recuperar-o-crear el carrito activo no duplica borradores ni siquiera con
peticiones concurrentes, y que finalizar sin un faltante de Telegram no libera nada y deja el
informe disponible tras recargar.
"""

import asyncio

import pytest
from test_quotes_api import H, _add, _finalize, _quote, shop  # noqa: F401
from test_quotes_api import api as _api_fixture  # noqa: F401

pytestmark = pytest.mark.db


@pytest.fixture
async def api(_api_fixture):  # noqa: F811
    """El cliente HTTP de la suite de cotizaciones; su `shop` viene importado arriba por nombre."""
    yield _api_fixture


async def _activo(api) -> list[dict]:
    r = await api.get("/api/v1/quotes?active=true&limit=1", headers=H(api))
    assert r.status_code == 200, r.text
    return r.json()["quotes"]


async def _detalle(api, quote_id):
    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    assert r.status_code == 200, r.text
    return r.json()


async def _lineas_activas(detalle) -> list[dict]:
    return [
        linea
        for carrito in detalle["carts"]
        for linea in carrito["lines"]
        if linea["status"] == "active"
    ]


def _linea(detalle, line_id) -> dict:
    for carrito in detalle["carts"]:
        for linea in carrito["lines"]:
            if linea["line_id"] == line_id:
                return linea
    raise AssertionError("línea ausente")


async def test_un_borrador_anterior_a_la_finalizacion_no_es_carrito_activo(api):
    """El borrador huérfano sigue existiendo tal cual; simplemente no es el carrito vigente."""
    huerfano = await _quote(api, "Borrador anterior")
    assert (await _add(api, huerfano, offer="uno_x", value="1")).status_code == 201

    reciente = await _quote(api, "Cotización que se finaliza")
    assert (await _add(api, reciente, offer="uno_x", value="3")).status_code == 201
    r = await _finalize(api, reciente, key="finalize-carrito-activo-1")
    assert r.status_code == 200, r.text
    assert r.json()["quote"]["status"] == "closed"

    # El borrador anterior ya no es carrito activo: el carrito queda vacío al recargar.
    assert await _activo(api) == []

    # No se borró ni se alteró: sigue listado en preparación y conserva su línea.
    r = await api.get("/api/v1/quotes?finalized=false", headers=H(api))
    assert [q["quote_id"] for q in r.json()["quotes"]] == [huerfano]
    detalle = await _detalle(api, huerfano)
    assert detalle["status"] == "draft" and detalle["finalized_at"] is None
    lineas = await _lineas_activas(detalle)
    assert len(lineas) == 1
    assert lineas[0]["quantity"]["value"] == "1"


async def test_el_borrador_huerfano_sigue_disponible_y_editable_por_id(api):
    """Conservarlo no es solo no borrarlo: puede completarse y finalizarse si alguien lo retoma."""
    huerfano = await _quote(api, "Borrador retomado")
    assert (await _add(api, huerfano, offer="uno_x", value="1")).status_code == 201

    reciente = await _quote(api, "Otra que se finaliza")
    await _add(api, reciente, offer="uno_y", value="2")
    assert (await _finalize(api, reciente, key="finalize-carrito-activo-2")).status_code == 200

    # Agregar al borrador por id sigue funcionando, aunque no se recupere solo.
    assert (await _add(api, huerfano, offer="uno_y", value="2")).status_code == 201
    detalle = await _detalle(api, huerfano)
    assert len(await _lineas_activas(detalle)) == 2
    r = await _finalize(api, huerfano, key="finalize-carrito-activo-3")
    assert r.status_code == 200, r.text


async def test_recuperar_o_crear_el_carrito_activo_es_idempotente(api):
    """Primera alta: se crea un carrito nuevo y vacío; la segunda recibe el mismo."""
    vieja = await _quote(api, "Anterior al cierre")
    await _add(api, vieja, offer="uno_x", value="1")
    assert (await _finalize(api, vieja, key="finalize-carrito-activo-4")).status_code == 200

    primero = await api.post("/api/v1/quotes/active", json={}, headers=H(api))
    assert primero.status_code == 200, primero.text
    nuevo = primero.json()
    assert nuevo["quote_id"] != vieja
    assert nuevo["status"] == "draft" and nuevo["finalized_at"] is None
    assert nuevo["carts"] == []

    segundo = await api.post("/api/v1/quotes/active", json={}, headers=H(api))
    assert segundo.status_code == 200, segundo.text
    assert segundo.json()["quote_id"] == nuevo["quote_id"]

    assert [q["quote_id"] for q in await _activo(api)] == [nuevo["quote_id"]]
    # El borrador anterior no se tocó al crear el vigente.
    assert (await _detalle(api, vieja))["finalized_at"] is not None


async def test_la_consulta_antigua_del_frontend_publicado_no_cambia_de_significado(api):
    """Compatibilidad: el cliente ya publicado usa `finalized=false&status=draft&limit=1`.

    Esa consulta conserva exactamente su comportamiento anterior —el último borrador, sin la
    regla nueva— para que el frontend actual siga funcionando mientras no se actualice. La
    corrección llega a la interfaz recién cuando el cliente pide `active=true`.
    """
    vieja = await _quote(api)
    await _add(api, vieja, offer="uno_x", value="1")
    nueva = await _quote(api)
    await _add(api, nueva, offer="uno_x", value="3")
    assert (await _finalize(api, nueva, key="finalize-compat-1")).status_code == 200

    r = await api.get(
        "/api/v1/quotes?finalized=false&status=draft&limit=1", headers=H(api)
    )
    assert r.status_code == 200, r.text
    assert [q["quote_id"] for q in r.json()["quotes"]] == [vieja]

    # Los campos que el cliente publicado ya consume siguen presentes y con el mismo tipo.
    fila = r.json()["quotes"][0]
    for campo in (
        "quote_id",
        "title",
        "status",
        "version",
        "created_at",
        "updated_at",
        "report_number",
        "report_label",
        "finalized_at",
        "finalized_by",
        "supplier_count",
        "line_count",
    ):
        assert campo in fila
    assert fila["status"] == "draft" and fila["finalized_at"] is None

    # El listado del historial y el alta explícita tampoco cambiaron.
    r = await api.get("/api/v1/quotes?finalized=true", headers=H(api))
    assert [q["quote_id"] for q in r.json()["quotes"]] == [nueva]
    r = await api.post("/api/v1/quotes", json={"title": "Alta explícita"}, headers=H(api))
    assert r.status_code == 201, r.text


async def test_el_carrito_activo_es_por_organizacion_y_solo_para_admin(api):
    """Ni el listado ni el recuperar-o-crear cruzan de organización, y exigen `admin`."""
    propio = await _quote(api, "Carrito de mi farmacia")

    # La administración de otra organización no ve el borrador ajeno y crea el propio.
    ajeno = await api.get("/api/v1/quotes?active=true", headers=H(api, "outsider"))
    assert ajeno.status_code == 200, ajeno.text
    assert ajeno.json()["quotes"] == []
    creado_ajeno = await api.post("/api/v1/quotes/active", json={}, headers=H(api, "outsider"))
    assert creado_ajeno.status_code == 200, creado_ajeno.text
    assert creado_ajeno.json()["quote_id"] != propio
    assert creado_ajeno.json()["carts"] == []

    # El carrito de la primera organización sigue intacto y siendo el suyo.
    assert [q["quote_id"] for q in await _activo(api)] == [propio]
    ajeno_activo = await api.get("/api/v1/quotes?active=true", headers=H(api, "outsider"))
    assert [q["quote_id"] for q in ajeno_activo.json()["quotes"]] == [
        creado_ajeno.json()["quote_id"]
    ]

    # Un rol que no es `admin` no recupera ni crea carrito.
    assert (await api.get("/api/v1/quotes?active=true", headers=H(api, "aux"))).status_code == 403
    r = await api.post("/api/v1/quotes/active", json={}, headers=H(api, "aux"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


async def test_crear_el_carrito_activo_no_duplica_borradores_concurrentes(api):
    """Dos pestañas que agregan a la vez comparten el mismo borrador."""
    vieja = await _quote(api)
    await _add(api, vieja, offer="uno_x", value="1")
    assert (await _finalize(api, vieja, key="finalize-carrito-activo-5")).status_code == 200

    respuestas = await asyncio.gather(
        api.post("/api/v1/quotes/active", json={}, headers=H(api)),
        api.post("/api/v1/quotes/active", json={}, headers=H(api)),
    )
    assert all(r.status_code == 200 for r in respuestas), [r.text for r in respuestas]
    ids = {r.json()["quote_id"] for r in respuestas}
    assert len(ids) == 1

    r = await api.get("/api/v1/quotes?finalized=false", headers=H(api))
    assert [q["quote_id"] for q in r.json()["quotes"]] == list(ids)


async def test_finalizar_sin_faltante_de_telegram_no_libera_nada_y_el_informe_sigue(api):
    """Cerrar una cotización armada sin faltante no toca la bandeja ni pierde el informe."""
    r = await api.get("/api/v1/demand-items", headers=H(api))
    assert r.status_code == 200, r.text
    bandeja_antes = {fila["id"]: fila["status"] for fila in r.json()["items"]}

    quote_id = await _quote(api)
    await _add(api, quote_id, offer="uno_x", value="2")
    cierre = await _finalize(api, quote_id, key="finalize-sin-faltante-1")
    assert cierre.status_code == 200, cierre.text
    data = cierre.json()
    assert data["released"] == [] and data["pending"] == []
    assert data["report"]["items"][0]["quantity"] == {"value": "2", "unit": "box"}

    # La bandeja queda exactamente como estaba: sin cotización no hay demanda que liberar.
    r = await api.get("/api/v1/demand-items", headers=H(api))
    assert {fila["id"]: fila["status"] for fila in r.json()["items"]} == bandeja_antes

    # El carrito activo queda vacío y el informe se recupera igual que antes de recargar.
    assert await _activo(api) == []
    r = await api.get(f"/api/v1/quotes/{quote_id}/report", headers=H(api))
    assert r.status_code == 200, r.text
    assert r.json()["report"] == data["report"]
