"""«Enseñar a la IA» en vivo: resolución del artículo y vínculo explícito con el catálogo.

Ninguna prueba sale a la red: la estrategia del motor se reemplaza por un doble. Se fija la regla
exacta de resolución (vínculo de la fuente o EAN único), la persistencia entre búsquedas y
sesiones, la autorización y la idempotencia del alta. No hay condiciones por proveedor.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.v1 import connectors as connectors_api
from app.core.config import get_settings
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AppUser,
    AuditEvent,
    Organization,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
)
from app.services.connectors import registry
from app.services.connectors.base import (
    ConnectorResult,
    PublishedProduct,
    SupplierOffer,
)
from app.services.connectors.store import store

pytestmark = pytest.mark.db

EAN_ITEM = "7801234567890"
EAN_AMBIGUO = "7809999999999"
EAN_DESCONOCIDO = "7800000000000"

PRE = "PRE-001"
PUB = "140640"


def offer(**over):
    data = dict(
        external_id=PRE,
        name="Predual 30 mg comprimidos",
        price=Decimal("12990"),
        currency="CLP",
        laboratory="Laboratorio Ficticio",
        ean=EAN_DESCONOCIDO,
    )
    data.update(over)
    return SupplierOffer(**data)


def published_product(**over):
    data = dict(
        external_id=PUB,
        name="Predual Pamabron 25 mg 12 Comprimidos",
        currency="CLP",
        stock_status="out_of_stock",
        reference_price=Decimal("2290"),
    )
    data.update(over)
    return PublishedProduct(**data)


class FakeStrategy:
    mechanism = "algolia"

    def __init__(self):
        self.offers = [offer()]
        self.published = []

    async def search(self, query, *, config, field_map, limit, transport=None):
        return ConnectorResult(
            offers=list(self.offers),
            observed_at=datetime.now(UTC),
            fingerprint="huella-vinculos",
            published=list(self.published),
            warnings=[],
        )


@pytest.fixture
async def farm(sessions):
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia vínculos", kind="simulation")
        other = Organization(name="Otra farmacia vínculos", kind="simulation")
        s.add_all([org, other])
        await s.flush()
        ids["org"], ids["other_org"] = org.id, other.id
        for key, name, roles, o in [
            ("admin", "Pao", ["admin"], org),
            ("buyer", "Compradora sin permiso web", ["purchasing"], org),
            ("outsider", "Externa", ["admin"], other),
        ]:
            u = AppUser(organization_id=o.id, display_name=name, roles=roles)
            s.add(u)
            await s.flush()
            ids[key] = u.id

        farmer = Supplier(organization_id=org.id, name="Farmaloop", data_origin="simulated")
        generic = Supplier(
            organization_id=org.id, name="Proveedor Genérico Ficticio", data_origin="simulated"
        )
        s.add_all([farmer, generic])
        await s.flush()
        ids["supplier"], ids["supplier_generic"] = farmer.id, generic.id
        source = Source(
            organization_id=org.id,
            kind="supplier",
            supplier_id=farmer.id,
            name="Farmaloop API",
            mode="api",
            data_origin="simulated",
        )
        generic_source = Source(
            organization_id=org.id,
            kind="supplier",
            supplier_id=generic.id,
            name="Proveedor Genérico API",
            mode="api",
            data_origin="simulated",
        )
        chain = Source(
            organization_id=org.id,
            kind="chain_reference",
            name="Cadena de referencia",
            mode="api",
            data_origin="simulated",
        )
        s.add_all([source, generic_source, chain])
        await s.flush()
        ids["source"], ids["generic_source"], ids["chain"] = (
            source.id,
            generic_source.id,
            chain.id,
        )

        item = TradeItem(
            organization_id=org.id,
            identity_key="paracetamol-500-20",
            display_name="Paracetamol 500 mg x 20",
            gtin=EAN_ITEM,
            data_origin="simulated",
        )
        otro = TradeItem(
            organization_id=org.id,
            identity_key="ibuprofeno-400-10",
            display_name="Ibuprofeno 400 mg x 10",
            data_origin="simulated",
        )
        duplicado_a = TradeItem(
            organization_id=org.id,
            identity_key="ambiguo-a",
            display_name="Ambiguo A",
            gtin=EAN_AMBIGUO,
            data_origin="simulated",
        )
        duplicado_b = TradeItem(
            organization_id=org.id,
            identity_key="ambiguo-b",
            display_name="Ambiguo B",
            gtin=EAN_AMBIGUO,
            data_origin="simulated",
        )
        ajeno = TradeItem(
            organization_id=other.id,
            identity_key="ajeno",
            display_name="Artículo de otra organización",
            data_origin="simulated",
        )
        s.add_all([item, otro, duplicado_a, duplicado_b, ajeno])
        await s.flush()
        ids["item"], ids["otro"] = item.id, otro.id
        ids["ajeno"] = ajeno.id
    return ids


@pytest.fixture
async def api(sessions, farm, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    monkeypatch.setenv("SUPPLIER_CONNECTOR_ENABLED", "true")
    monkeypatch.setenv("FARMALOOP_ALGOLIA_APP_ID", "APPFICTICIA")
    monkeypatch.setenv("FARMALOOP_ALGOLIA_API_KEY", "clave-ficticia")
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


def H(api, who="admin"):
    return {"X-Demo-User-Id": str(api.farm[who])}


async def configurar(api, source="source", who="admin"):
    r = await api.put(
        f"/api/v1/sources/{api.farm[source]}/connector-profile",
        json={"preset": "farmaloop"},
        headers={**H(api, who), "If-Match": '"0"'},
    )
    assert r.status_code == 200, r.text


async def buscar(api, *, source="source", query="predual", who="admin"):
    return await api.post(
        "/api/v1/supplier-searches",
        json={"source_id": str(api.farm[source]), "query": query, "limit": 10},
        headers=H(api, who),
    )


async def vincular(
    api,
    *,
    source="source",
    external_id=PRE,
    external_name="Predual 30 mg comprimidos",
    trade_item_id=None,
    who="admin",
):
    return await api.post(
        "/api/v1/supplier-product-mappings",
        json={
            "source_id": str(api.farm[source]),
            "external_id": external_id,
            "external_name": external_name,
            "trade_item_id": str(trade_item_id or api.farm["item"]),
        },
        headers=H(api, who),
    )


async def contar(sessions, model):
    async with sessions() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


# ------------------------------------------------------------- resolución en la búsqueda


async def test_sin_vinculo_ni_ean_no_se_inventa_el_articulo(api, fake):
    await configurar(api)
    r = await buscar(api)
    assert r.status_code == 200, r.text
    assert r.json()["offers"][0]["trade_item_id"] is None


async def test_un_ean_identico_y_unico_resuelve_el_articulo(api, fake):
    fake.offers = [offer(ean=EAN_ITEM)]
    await configurar(api)
    r = await buscar(api)
    assert r.json()["offers"][0]["trade_item_id"] == str(api.farm["item"])


async def test_un_ean_repetido_es_ambiguo_y_no_se_resuelve(api, fake):
    fake.offers = [offer(ean=EAN_AMBIGUO)]
    await configurar(api)
    r = await buscar(api)
    assert r.json()["offers"][0]["trade_item_id"] is None


async def test_las_publicaciones_tambien_resuelven_su_articulo(api, fake):
    fake.published = [published_product(ean=EAN_ITEM)]
    await configurar(api)
    r = await buscar(api)
    assert r.json()["published"][0]["trade_item_id"] == str(api.farm["item"])


async def test_el_vinculo_explicito_manda_sobre_un_ean_ambiguo(api, sessions, fake):
    fake.offers = [offer(ean=EAN_AMBIGUO)]
    async with sessions() as s, s.begin():
        s.add(
            SupplierProductMapping(
                organization_id=api.farm["org"],
                source_id=api.farm["source"],
                supplier_id=api.farm["supplier"],
                trade_item_id=api.farm["item"],
                external_id=PRE,
                origin="explicit",
                data_origin="simulated",
            )
        )
    await configurar(api)
    r = await buscar(api)
    assert r.json()["offers"][0]["trade_item_id"] == str(api.farm["item"])


async def test_un_vinculo_creado_despues_aparece_aunque_la_busqueda_este_en_cache(
    api, fake
):
    await configurar(api)
    primero = await buscar(api)
    assert primero.json()["offers"][0]["trade_item_id"] is None
    assert primero.json()["cached"] is False

    assert (await vincular(api)).status_code == 201

    segundo = await buscar(api)
    assert segundo.json()["cached"] is True
    assert segundo.json()["offers"][0]["trade_item_id"] == str(api.farm["item"])


async def test_el_vinculo_persiste_entre_busquedas_y_sesiones(api, fake):
    await configurar(api)
    assert (await vincular(api)).status_code == 201

    # Un reinicio del proceso pierde la caché; el vínculo vive en la base.
    store.reset()
    r = await buscar(api, query="predual")
    assert r.json()["cached"] is False
    assert r.json()["offers"][0]["trade_item_id"] == str(api.farm["item"])

    detalle = await api.get(
        f"/api/v1/supplier-searches/{r.json()['search_id']}", headers=H(api)
    )
    assert detalle.status_code == 200, detalle.text
    assert detalle.json()["offers"][0]["trade_item_id"] == str(api.farm["item"])


async def test_otro_proveedor_usa_el_mismo_flujo_sin_condiciones_por_nombre(
    api, fake
):
    await configurar(api, source="generic_source")
    await configurar(api, source="source")
    r = await vincular(api, source="generic_source")
    assert r.status_code == 201, r.text
    assert r.json()["supplier_id"] == str(api.farm["supplier_generic"])

    store.reset()
    encontrado = await buscar(api, source="generic_source", query="predual")
    assert encontrado.json()["offers"][0]["trade_item_id"] == str(api.farm["item"])

    # El vínculo de un proveedor no se comparte con otro.
    otro = await buscar(api, source="source", query="predual")
    assert otro.json()["offers"][0]["trade_item_id"] is None


# ------------------------------------------------------------- alta del vínculo


async def test_crear_vinculo_explicito_queda_auditado(api, sessions):
    r = await vincular(api)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True
    assert body["origin"] == "explicit"
    assert body["source_id"] == str(api.farm["source"])
    assert body["supplier_id"] == str(api.farm["supplier"])
    assert body["trade_item_id"] == str(api.farm["item"])

    async with sessions() as s:
        mapping = (await s.execute(select(SupplierProductMapping))).scalar_one()
        assert mapping.origin == "explicit"
        # El alta explícita se identifica por su autor (`created_by`); `confirmed_by` es del paso
        # posterior de confirmación y el contrato lo exige nulo cuando `origin = 'explicit'`.
        assert mapping.confirmed_by is None
        assert mapping.confirmed_at is None
        assert mapping.created_by == api.farm["admin"]
        evento = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "supplier_product_mapping.created"
                )
            )
        ).scalar_one()
        assert evento.actor_id == api.farm["admin"]
        assert evento.after["trade_item_id"] == str(api.farm["item"])


async def test_el_alta_explicita_no_admite_confirmador_y_sigue_siendo_valida(api, sessions, fake):
    """`confirmed_by` NULL no es un olvido: es el contrato del alta explícita.

    El modelo exige `(origin = 'confirmed') = (confirmed_by IS NOT NULL)`. Un vínculo que nace
    explícito declara su autor en `created_by`; la confirmación (`confirmed_by` + `confirmed_at`)
    pertenece a otro paso del ciclo de vida. La prueba demuestra que (1) la base rechaza la mezcla
    y (2) el vínculo explícito, aun sin confirmador, resuelve y persiste entre búsquedas.
    """
    r = await vincular(api)
    assert r.status_code == 201, r.text
    mapping_id = uuid.UUID(r.json()["mapping_id"])

    async with sessions() as s:
        mapping = await s.get(SupplierProductMapping, mapping_id)
        assert mapping is not None
        assert mapping.origin == "explicit"
        assert mapping.created_by == api.farm["admin"]
        assert mapping.confirmed_by is None
        assert mapping.confirmed_at is None

    # Escribir a mano un confirmador sobre un vínculo explícito viola la restricción del contrato.
    async with sessions() as s:
        mapping = await s.get(SupplierProductMapping, mapping_id)
        mapping.confirmed_by = api.farm["admin"]
        mapping.confirmed_at = datetime.now(UTC)
        with pytest.raises(IntegrityError):
            await s.flush()
        await s.rollback()

    # Sigue vigente y persistente: una búsqueda nueva resuelve el mismo artículo desde el vínculo.
    await configurar(api)
    busqueda = await buscar(api)
    assert busqueda.status_code == 200, busqueda.text
    assert busqueda.json()["offers"][0]["trade_item_id"] == str(api.farm["item"])

    async with sessions() as s:
        assert await s.get(SupplierProductMapping, mapping_id) is not None


async def test_repetir_el_mismo_vinculo_es_idempotente(api, sessions):
    primero = await vincular(api)
    assert primero.status_code == 201
    segundo = await vincular(api)
    assert segundo.status_code == 200, segundo.text
    assert segundo.json()["created"] is False
    assert segundo.json()["mapping_id"] == primero.json()["mapping_id"]

    assert await contar(sessions, SupplierProductMapping) == 1
    async with sessions() as s:
        eventos = (
            await s.execute(
                select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.action == "supplier_product_mapping.created"
                )
            )
        ).scalar_one()
    # Un reintento no duplica la auditoría del alta.
    assert eventos == 1


async def test_cambiar_el_destino_de_un_vinculo_existente_da_conflicto(api, sessions):
    assert (await vincular(api)).status_code == 201
    r = await vincular(api, trade_item_id=api.farm["otro"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SUPPLIER_MAPPING_CONFLICT"

    async with sessions() as s:
        mapping = (await s.execute(select(SupplierProductMapping))).scalar_one()
        assert mapping.trade_item_id == api.farm["item"]


async def test_solo_admin_puede_vincular(api, sessions):
    r = await vincular(api, who="buyer")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"
    assert await contar(sessions, SupplierProductMapping) == 0


async def test_no_se_vincula_con_fuente_ni_articulo_de_otra_organizacion(api):
    fuente_ajena = await api.post(
        "/api/v1/supplier-product-mappings",
        json={
            "source_id": str(api.farm["source"]),
            "external_id": PRE,
            "trade_item_id": str(api.farm["item"]),
        },
        headers=H(api, "outsider"),
    )
    assert fuente_ajena.status_code == 404

    articulo_ajeno = await vincular(api, trade_item_id=api.farm["ajeno"])
    assert articulo_ajeno.status_code == 404


async def test_una_fuente_que_no_es_de_proveedor_no_se_vincula(api):
    r = await vincular(api, source="chain")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SOURCE_NOT_SUPPLIER"


async def test_un_articulo_inactivo_no_se_vincula(api, sessions):
    async with sessions() as s, s.begin():
        item = await s.get(TradeItem, api.farm["otro"])
        item.status = "inactive"
    r = await vincular(api, trade_item_id=api.farm["otro"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRADE_ITEM_INACTIVE"


async def test_el_identificador_externo_es_obligatorio(api):
    r = await api.post(
        "/api/v1/supplier-product-mappings",
        json={
            "source_id": str(api.farm["source"]),
            "external_id": "   ",
            "trade_item_id": str(api.farm["item"]),
        },
        headers=H(api),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "EXTERNAL_ID_REQUIRED"
