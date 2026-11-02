"""E6 — proveedores: permisos, versión, duplicados y aislamiento por organización (PostgreSQL real)."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import get_session
from app.main import create_app
from app.models import AppUser, AuditEvent, Organization, OutboxEvent, Supplier

pytestmark = pytest.mark.db


@pytest.fixture
async def farm(sessions):
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia proveedores", kind="simulation")
        other = Organization(name="Otra proveedores", kind="simulation")
        s.add_all([org, other])
        await s.flush()
        ids["org"], ids["other_org"] = org.id, other.id
        for key, name, roles, o in [
            ("admin", "Admin", ["admin"], org),
            ("aux", "Auxiliar", ["auxiliary"], org),
            ("outsider", "Externo", ["admin"], other),
        ]:
            u = AppUser(organization_id=o.id, display_name=name, roles=roles)
            s.add(u)
            await s.flush()
            ids[key] = u.id
    return ids


@pytest.fixture
async def api(sessions, farm, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    app = create_app()

    async def _s():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.farm = farm
        yield c
    get_settings.cache_clear()


def H(api, who="admin", **extra):
    return {"X-Demo-User-Id": str(api.farm[who]), **extra}


async def create(api, who="admin", **data):
    body = {"name": "Proveedor Ficticio Norte", "supplier_code": "FIX-NORTE"}
    body.update(data)
    return await api.post("/api/v1/suppliers", json=body, headers=H(api, who))


async def test_crear_proveedor_audita_y_marca_procedencia(api, sessions):
    r = await create(api)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Proveedor Ficticio Norte"
    assert body["status"] == "active"
    assert body["version"] == 1
    assert body["sources"] == []
    assert r.headers["ETag"] == '"1"'
    async with sessions() as s:
        row = (
            await s.execute(select(Supplier).where(Supplier.id == body["supplier_id"]))
        ).scalar_one()
        assert row.data_origin == "simulated"
        assert row.created_by == api.farm["admin"]
        assert (
            await s.execute(select(func.count()).select_from(AuditEvent))
        ).scalar_one() == 1
        assert (
            await s.execute(select(func.count()).select_from(OutboxEvent))
        ).scalar_one() == 1


async def test_nombre_duplicado_ignora_mayusculas_y_tildes(api):
    assert (await create(api, name="Proveedor Ficticio Norte")).status_code == 201
    r = await create(api, name="  PROVEEDOR FICTICIO NORTE  ")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DUPLICATE_NAME"


async def test_solo_admin_puede_administrar_proveedores(api):
    r = await create(api, who="aux")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"
    assert (await api.get("/api/v1/suppliers", headers=H(api, "aux"))).status_code == 403


async def test_una_organizacion_no_ve_ni_toca_proveedores_ajenos(api):
    created = (await create(api)).json()
    r = await api.get(f"/api/v1/suppliers/{created['supplier_id']}", headers=H(api, "outsider"))
    assert r.status_code == 404
    r = await api.patch(
        f"/api/v1/suppliers/{created['supplier_id']}",
        json={"name": "Secuestrado"},
        headers=H(api, "outsider", **{"If-Match": '"1"'}),
    )
    assert r.status_code == 404
    listed = (await api.get("/api/v1/suppliers", headers=H(api, "outsider"))).json()
    assert listed == {"suppliers": []}


async def test_actualizar_exige_if_match_y_version_vigente(api):
    created = (await create(api)).json()
    url = f"/api/v1/suppliers/{created['supplier_id']}"

    r = await api.patch(url, json={"name": "Nuevo nombre"}, headers=H(api))
    assert r.status_code == 428
    assert r.json()["error"]["code"] == "VERSION_REQUIRED"

    r = await api.patch(
        url, json={"name": "Nuevo nombre"}, headers=H(api, **{"If-Match": '"9"'})
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "VERSION_CONFLICT"
    assert r.json()["error"]["details"]["current_version"] == 1

    r = await api.patch(
        url, json={"name": "Nuevo nombre"}, headers=H(api, **{"If-Match": '"1"'})
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Nuevo nombre"
    assert r.json()["version"] == 2
    assert r.headers["ETag"] == '"2"'


async def test_desactivar_y_listar_por_estado(api):
    created = (await create(api)).json()
    r = await api.patch(
        f"/api/v1/suppliers/{created['supplier_id']}",
        json={"status": "inactive"},
        headers=H(api, **{"If-Match": '"1"'}),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "inactive"
    activos = (await api.get("/api/v1/suppliers?status=active", headers=H(api))).json()
    assert activos == {"suppliers": []}
    inactivos = (await api.get("/api/v1/suppliers?status=inactive", headers=H(api))).json()
    assert len(inactivos["suppliers"]) == 1
