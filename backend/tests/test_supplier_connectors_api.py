"""API del motor de proveedores con una estrategia simulada.

Ninguna prueba hace una solicitud externa: se reemplaza la estrategia registrada por un doble.
Se verifica configuración, estados, caché, versiones y la regla de no persistir ofertas.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.api.v1 import connectors as connectors_api
from app.core.config import get_settings
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AppUser,
    OfferSnapshot,
    Organization,
    Source,
    Supplier,
    SupplierConnectorProfile,
)
from app.services.connectors import registry
from app.services.connectors.base import (
    ConnectorError,
    ConnectorIntervention,
    ConnectorNotConfigured,
    ConnectorResult,
    PublishedProduct,
    SupplierOffer,
)
from app.services.connectors.store import store

pytestmark = pytest.mark.db

SECRET = "valor-secreto-123"


def offer(**over):
    data = dict(
        external_id="PRE-001",
        name="Predual 30 mg comprimidos",
        price=Decimal("12990"),
        currency="CLP",
        laboratory="Laboratorio Ficticio",
        presentation="Caja 30 comprimidos",
        image_url="https://img.test/predual.jpg",
        ean="7801234567890",
    )
    data.update(over)
    return SupplierOffer(**data)


def published_product(**over):
    data = dict(
        external_id="140640",
        name="Predual Pamabron 25 mg 12 Comprimidos",
        currency="CLP",
        laboratory="Laboratorio Ficticio",
        presentation="Comprimido",
        stock_status="out_of_stock",
        reference_price=Decimal("2290"),
    )
    data.update(over)
    return PublishedProduct(**data)


class FakeStrategy:
    mechanism = "algolia"

    def __init__(self):
        self.calls = 0
        self.error: Exception | None = None
        self.fingerprint = "huella-1"
        self.offers = [offer()]
        self.published = []

    async def search(self, query, *, config, field_map, limit, transport=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ConnectorResult(
            offers=list(self.offers),
            observed_at=datetime.now(UTC),
            fingerprint=self.fingerprint,
            published=list(self.published),
            warnings=[],
        )


@pytest.fixture
async def farm(sessions):
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia motor", kind="simulation")
        other = Organization(name="Otra farmacia motor", kind="simulation")
        s.add_all([org, other])
        await s.flush()
        ids["org"], ids["other_org"] = org.id, other.id
        for key, name, roles, o in [
            ("admin", "Pao", ["admin"], org),
            ("outsider", "Externo", ["admin"], other),
        ]:
            u = AppUser(organization_id=o.id, display_name=name, roles=roles)
            s.add(u)
            await s.flush()
            ids[key] = u.id
        supplier = Supplier(
            organization_id=org.id, name="Proveedor Farmaloop", data_origin="simulated"
        )
        s.add(supplier)
        await s.flush()
        ids["supplier"] = supplier.id
        source = Source(
            organization_id=org.id,
            kind="supplier",
            supplier_id=supplier.id,
            name="Farmaloop API",
            mode="api",
            data_origin="simulated",
        )
        manual = Source(
            organization_id=org.id,
            kind="supplier",
            supplier_id=supplier.id,
            name="Lista manual",
            mode="manual_import",
            data_origin="simulated",
        )
        s.add_all([source, manual])
        await s.flush()
        ids["source"], ids["manual"] = source.id, manual.id
    return ids


@pytest.fixture
async def api(sessions, farm, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    monkeypatch.setenv("SUPPLIER_CONNECTOR_ENABLED", "true")
    monkeypatch.setenv("FARMALOOP_ALGOLIA_APP_ID", "APPFICTICIA")
    monkeypatch.setenv("FARMALOOP_ALGOLIA_API_KEY", SECRET)
    get_settings.cache_clear()
    app = create_app()

    async def _s():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    store.reset()
    connectors_api._live_limits = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.farm = farm
        yield c
    store.reset()
    connectors_api._live_limits = None
    get_settings.cache_clear()


@pytest.fixture
def fake(monkeypatch):
    strategy = FakeStrategy()
    monkeypatch.setitem(registry._STRATEGIES, "algolia", strategy)
    return strategy


def H(api, who="admin", **extra):
    return {"X-Demo-User-Id": str(api.farm[who]), **extra}


def url(api, source="source"):
    return f"/api/v1/sources/{api.farm[source]}/connector-profile"


async def put(api, *, body=None, version=0, source="source", headers=True):
    body = body or {"preset": "farmaloop"}
    extra = {"If-Match": f'"{version}"'} if headers else {}
    return await api.put(url(api, source), json=body, headers=H(api, **extra))


async def search(api, query="predual", source="source", limit=10):
    return await api.post(
        "/api/v1/supplier-searches",
        json={"source_id": str(api.farm[source]), "query": query, "limit": limit},
        headers=H(api),
    )


async def count(sessions, model):
    async with sessions() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def profile_row(sessions):
    async with sessions() as s:
        return (await s.execute(select(SupplierConnectorProfile))).scalar_one_or_none()


async def test_sin_perfil_el_estado_es_no_configurado_y_no_consulta(api, fake):
    r = await api.get(url(api), headers=H(api))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 0
    assert body["profile"]["configured"] is False
    assert body["profile"]["state"] == "not_configured"
    assert body["profile"]["reason"] == "profile_missing"

    found = await search(api)
    assert found.status_code == 200, found.text
    assert found.json()["state"] == "not_configured"
    assert found.json()["reason"] == "profile_missing"
    assert found.json()["offers"] == []
    assert fake.calls == 0


async def test_guardar_preset_no_guarda_ni_devuelve_la_clave(api, sessions):
    r = await put(api)
    assert r.status_code == 200, r.text
    assert r.headers["ETag"] == '"1"'
    body = r.json()
    assert body["configured"] is True
    assert body["mechanism"] == "algolia"
    assert body["config"]["index"] == "f_products_prod"
    assert body["config"]["api_key_secret_ref"] == "FARMALOOP_ALGOLIA_API_KEY"
    assert body["enabled"] is True
    assert body["state"] == "not_configured"
    assert body["reason"] == "not_tested"
    assert SECRET not in r.text
    assert "unitCost" not in r.text

    row = await profile_row(sessions)
    assert row is not None
    assert row.config["index"] == "f_products_prod"
    assert row.config["api_key_secret_ref"] == "FARMALOOP_ALGOLIA_API_KEY"
    assert "unitCost" not in repr(row.field_map)


async def test_actualizar_exige_if_match_y_version_correcta(api):
    assert (await put(api)).status_code == 200
    missing = await put(api, headers=False)
    assert missing.status_code == 428
    assert missing.json()["error"]["code"] == "VERSION_REQUIRED"

    stale = await put(api, version=99)
    assert stale.status_code == 409
    assert stale.json()["error"]["details"]["current_version"] == 1

    updated = await put(api, version=1)
    assert updated.status_code == 200
    assert updated.json()["version"] == 2


async def test_una_fuente_manual_no_acepta_perfil(api):
    r = await put(api, source="manual")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SOURCE_MODE_NOT_API"


async def test_otra_organizacion_no_puede_ver_ni_configurar(api):
    assert (await put(api)).status_code == 200
    assert (await api.get(url(api), headers=H(api, "outsider"))).status_code == 404
    other = await api.put(
        url(api),
        json={"preset": "farmaloop"},
        headers=H(api, "outsider", **{"If-Match": '"0"'}),
    )
    assert other.status_code == 404


async def test_busqueda_exitosa_no_persiste_ofertas(api, sessions, fake):
    assert (await put(api)).status_code == 200
    r = await search(api)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "connected"
    assert body["reason"] is None
    assert body["outcome"] == "matches"
    assert body["cached"] is False
    assert len(body["offers"]) == 1
    found = body["offers"][0]
    assert found["external_id"] == "PRE-001"
    assert found["price"] == {"amount": "12990", "currency": "CLP"}
    assert found["sale_unit"] is None
    assert found["price_basis"] is None
    assert found["stock_status"] == "unknown"
    assert found["stock_quantity"] is None
    assert found["match_relation"] == "direct"
    assert found["match_reasons"] == ["all_query_terms_in_name"]
    # La presentación no confirma unidad de venta ni base del precio: sigue bloqueada.
    assert found["cart_eligible"] is False
    assert found["cart_blockers"] == ["sale_unit_unconfirmed", "price_basis_unconfirmed"]
    assert body["published"] == []
    assert fake.calls == 1

    row = await profile_row(sessions)
    assert row.last_success_at is not None
    assert row.last_error is None
    assert row.last_error_kind is None
    assert row.schema_fingerprint == "huella-1"
    assert await count(sessions, OfferSnapshot) == 0


async def test_una_publicacion_sin_precio_se_muestra_aparte_y_nunca_es_elegible(
    api, sessions, fake
):
    """Predual Pamabron existe en el catálogo sin precio vigente: se muestra, no se cotiza."""
    assert (await put(api)).status_code == 200
    fake.offers = [offer(external_id="140429", name="Predual 125 comprimidos")]
    fake.published = [
        published_product(),
        published_product(
            external_id="8127",
            name="Pedialyte 60 Manzana x 500 ml Solución Oral",
            stock_status="unknown",
            reference_price=None,
        ),
    ]

    r = await search(api, query="predual")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["state"] == "connected"
    assert body["outcome"] == "matches"
    assert [o["external_id"] for o in body["offers"]] == ["140429"]

    by_id = {item["external_id"]: item for item in body["published"]}
    assert set(by_id) == {"140640", "8127"}

    directa = by_id["140640"]
    assert directa["match_relation"] == "direct"
    assert directa["stock_status"] == "out_of_stock"
    # El precio de lista es solo referencial: no se copia al precio de venta ni existe aquí.
    assert directa["reference_price"] == {"amount": "2290", "currency": "CLP"}
    assert "price" not in directa
    assert directa["cart_eligible"] is False
    assert directa["cart_blockers"] == [
        "not_available_for_sale",
        "no_current_price",
        "sale_unit_unconfirmed",
        "price_basis_unconfirmed",
    ]

    sin_stock_informado = by_id["8127"]
    assert sin_stock_informado["stock_status"] == "unknown"
    assert sin_stock_informado["reference_price"] is None
    assert sin_stock_informado["match_relation"] == "related"
    assert "not_available_for_sale" not in sin_stock_informado["cart_blockers"]
    assert "no_current_price" in sin_stock_informado["cart_blockers"]

    assert all(item["cart_eligible"] is False for item in body["offers"])
    assert await count(sessions, OfferSnapshot) == 0


async def test_solo_publicaciones_sin_precio_sigue_siendo_una_busqueda_con_resultados(
    api, fake
):
    assert (await put(api)).status_code == 200
    fake.offers = []
    fake.published = [published_product()]

    body = (await search(api, query="pamabron")).json()

    assert body["offers"] == []
    assert body["outcome"] == "matches"
    assert [item["external_id"] for item in body["published"]] == ["140640"]


async def test_los_resultados_relacionados_se_muestran_pero_no_se_presentan_como_coincidencia(
    api, sessions, fake
):
    """Pregabalina no es Predual: se devuelve aparte, no se descarta en silencio."""
    fake.offers = [
        offer(external_id="140429"),
        offer(external_id="105469", name="Predual x 10 Comprimidos"),
        offer(
            external_id="PREG-1",
            name="Pregalex Pregabalina 150 mg 30 comprimidos",
            laboratory="Laboratorio Ficticio",
        ),
        offer(external_id="PREG-2", name="Pregalin 75 mg x 30 Comprimidos"),
    ]
    assert (await put(api)).status_code == 200
    r = await search(api, query="predual")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "connected"
    assert len(body["offers"]) == 4

    by_id = {item["external_id"]: item for item in body["offers"]}
    assert by_id["140429"]["match_relation"] == "direct"
    assert by_id["105469"]["match_relation"] == "direct"
    for related_id in ("PREG-1", "PREG-2"):
        related = by_id[related_id]
        assert related["match_relation"] == "related"
        assert related["match_reasons"] == ["query_not_confirmed_by_name"]
        assert related["cart_eligible"] is False
        assert related["cart_blockers"] == [
            "match_not_direct",
            "sale_unit_unconfirmed",
            "price_basis_unconfirmed",
        ]

    # Ninguna oferta está habilitada mientras falten unidad de venta y base del precio.
    assert all(item["cart_eligible"] is False for item in body["offers"])
    assert await count(sessions, OfferSnapshot) == 0


async def test_la_segunda_busqueda_igual_usa_cache(api, fake):
    assert (await put(api)).status_code == 200
    first = await search(api)
    second = await search(api)
    assert first.status_code == second.status_code == 200
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert fake.calls == 1


async def test_un_error_transitorio_se_informa_y_no_borra_la_configuracion(api, sessions, fake):
    assert (await put(api)).status_code == 200
    fake.error = ConnectorError("timeout")
    r = await search(api)
    assert r.status_code == 200
    assert r.json()["state"] == "error"
    assert r.json()["reason"] == "timeout"
    row = await profile_row(sessions)
    assert row.last_error_kind == "transient"
    assert row.last_error == "timeout"
    assert row.config["index"] == "f_products_prod"


async def test_una_intervencion_se_informa_como_tal(api, sessions, fake):
    assert (await put(api)).status_code == 200
    fake.error = ConnectorIntervention("credentials_rejected")
    r = await search(api)
    assert r.json()["state"] == "requires_intervention"
    assert r.json()["reason"] == "credentials_rejected"
    row = await profile_row(sessions)
    assert row.last_error_kind == "intervention"


async def test_un_secreto_faltante_es_no_configurado_no_un_error(api, sessions, fake):
    assert (await put(api)).status_code == 200
    fake.error = ConnectorNotConfigured("secret_missing:FARMALOOP_ALGOLIA_API_KEY")
    r = await search(api)
    assert r.json()["state"] == "not_configured"
    assert r.json()["reason"].startswith("secret_missing:")
    row = await profile_row(sessions)
    assert row.last_error_kind == "configuration"


async def test_una_fuente_apagada_no_se_consulta(api, sessions, fake):
    assert (await put(api)).status_code == 200
    async with sessions() as s, s.begin():
        await s.execute(
            update(Source).where(Source.id == api.farm["source"]).values(enabled=False)
        )
    r = await search(api)
    assert r.json()["state"] == "not_configured"
    assert r.json()["reason"] == "source_disabled"
    assert fake.calls == 0


async def test_get_search_respeta_la_organizacion(api):
    assert (await put(api)).status_code == 200
    created = (await search(api)).json()
    found = await api.get(f"/api/v1/supplier-searches/{created['search_id']}", headers=H(api))
    assert found.status_code == 200
    assert found.json()["search_id"] == created["search_id"]
    outsider = await api.get(
        f"/api/v1/supplier-searches/{created['search_id']}", headers=H(api, "outsider")
    )
    assert outsider.status_code == 404
    missing = await api.get(f"/api/v1/supplier-searches/{uuid.uuid4()}", headers=H(api))
    assert missing.status_code == 404


async def test_limite_de_busquedas_por_minuto(api, monkeypatch, fake):
    monkeypatch.setenv("SUPPLIER_CONNECTOR_RATE_LIMIT_PER_MINUTE", "1")
    get_settings.cache_clear()
    connectors_api._live_limits = None
    assert (await put(api)).status_code == 200
    assert (await search(api, query="predual")).status_code == 200
    assert (await search(api, query="otra consulta")).status_code == 429


async def test_un_cambio_de_estructura_pide_revision_pero_devuelve_lo_que_valido(api, fake):
    assert (await put(api)).status_code == 200
    assert (await search(api, query="predual")).json()["state"] == "connected"

    fake.fingerprint = "huella-2"
    drift = await search(api, query="predual 30")
    body = drift.json()
    assert body["state"] == "requires_intervention"
    assert body["reason"] == "schema_changed"
    assert body["offers"]
    assert any("estructura" in w.lower() for w in body["warnings"])

    # La huella nueva ya quedó registrada: la siguiente consulta no vuelve a alertar.
    again = await search(api, query="predual 30")
    assert again.json()["state"] == "connected"
