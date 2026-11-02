"""E7/E4 — fuentes: validación de capacidades, fixture solo en simulación y estado derivado."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_session
from app.main import create_app
from app.models import AppUser, Organization, Source, Supplier

pytestmark = pytest.mark.db


@pytest.fixture
async def farm(sessions):
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia fuentes", kind="simulation")
        real = Organization(name="Farmacia real fuentes", kind="real_test")
        other = Organization(name="Otra fuentes", kind="simulation")
        s.add_all([org, real, other])
        await s.flush()
        ids["org"], ids["real_org"], ids["other_org"] = org.id, real.id, other.id
        for key, name, roles, o in [
            ("admin", "Admin", ["admin"], org),
            ("real", "Real", ["admin"], real),
            ("aux", "Auxiliar", ["auxiliary"], org),
            ("outsider", "Externo", ["admin"], other),
        ]:
            u = AppUser(organization_id=o.id, display_name=name, roles=roles)
            s.add(u)
            await s.flush()
            ids[key] = u.id
        sup = Supplier(
            organization_id=org.id, name="Proveedor Fuentes", data_origin="simulated"
        )
        ajeno = Supplier(
            organization_id=other.id, name="Proveedor Ajeno", data_origin="simulated"
        )
        s.add_all([sup, ajeno])
        await s.flush()
        ids["supplier"], ids["foreign_supplier"] = sup.id, ajeno.id
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
    body = {
        "name": "Lista Ficticia Norte",
        "kind": "supplier",
        "supplier_id": str(api.farm["supplier"]),
        "mode": "fixture",
        "capabilities": ["search", "price"],
    }
    body.update(data)
    return await api.post("/api/v1/sources", json=body, headers=H(api, who))


async def test_crear_fuente_de_proveedor(api):
    r = await create(api)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "supplier"
    assert body["mode"] == "fixture"
    assert body["capabilities"] == ["search", "price"]
    assert body["has_secret"] is False
    assert body["status"] == "not_configured"
    assert body["version"] == 1
    assert r.headers["ETag"] == '"1"'


async def test_fuente_de_cadena_no_declara_proveedor_ni_condiciones(api):
    r = await create(api, kind="chain_reference", supplier_id=None, name="Cadena Ficticia")
    assert r.status_code == 201, r.text
    assert r.json()["supplier_id"] is None

    r = await create(
        api, kind="chain_reference", supplier_id=None, name="Cadena con terms", capabilities=["terms"]
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SOURCE_CAPABILITY_NOT_ALLOWED"

    r = await create(
        api, kind="chain_reference", supplier_id=str(api.farm["supplier"]), name="Cadena con proveedor"
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SUPPLIER_NOT_ALLOWED"


async def test_fuente_de_proveedor_exige_proveedor_de_la_organizacion(api):
    r = await create(api, supplier_id=None)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SUPPLIER_REQUIRED"

    r = await create(api, supplier_id=str(api.farm["foreign_supplier"]))
    assert r.status_code == 404

    r = await create(api, kind="chain_reference")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SUPPLIER_NOT_ALLOWED"


async def test_capacidades_de_compra_no_se_admiten_en_v0(api):
    r = await create(api, capabilities=["search", "remote_cart"])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SOURCE_CAPABILITY_NOT_ALLOWED"
    r = await create(api, capabilities=["search", "inventada"])
    assert r.status_code == 422


async def test_fixture_solo_en_simulacion(api, sessions, farm):
    # AUTH_MODE=demo_header ya bloquea administrar una organización real (StrongActor), así que la
    # regla se comprueba en el servicio, que es donde vive.
    from app.core.errors import DomainError
    from app.services import sources as svc
    from app.services.actors import load_actor

    async with sessions() as s, s.begin():
        actor = await load_actor(s, farm["real"])
        with pytest.raises(DomainError) as e:
            await svc.create_source(
                s,
                actor,
                name="Lista real",
                kind="chain_reference",
                supplier_id=None,
                mode="fixture",
                capabilities=["search"],
                enabled=True,
            )
    assert (e.value.code, e.value.status_code) == ("FIXTURE_NOT_ALLOWED", 422)

    r = await create(api, mode="none", name="Lista en simulación")
    assert r.status_code == 201, r.text


async def test_solo_admin(api):
    r = await create(api, who="aux")
    assert r.status_code == 403
    assert (await api.get("/api/v1/sources", headers=H(api, "aux"))).status_code == 403


async def test_aislamiento_por_organizacion(api):
    created = (await create(api)).json()
    r = await api.get(f"/api/v1/sources/{created['source_id']}", headers=H(api, "outsider"))
    assert r.status_code == 404
    assert (await api.get("/api/v1/sources", headers=H(api, "outsider"))).json() == {"sources": []}


async def test_estado_derivado_de_frescura(api, sessions):
    created = (await create(api)).json()
    sid = created["source_id"]

    async def state(freshness: timedelta):
        async with sessions() as s, s.begin():
            row = (await s.execute(select(Source).where(Source.id == sid))).scalar_one()
            row.last_success_at = datetime.now(UTC) - freshness
        r = await api.get(f"/api/v1/sources/{sid}", headers=H(api))
        assert r.status_code == 200
        return r.json()["status"]

    assert await state(timedelta(minutes=5)) == "ok"
    assert await state(timedelta(hours=25)) == "stale"
    async with sessions() as s, s.begin():
        row = (await s.execute(select(Source).where(Source.id == sid))).scalar_one()
        row.last_error = "timeout"
    assert (await api.get(f"/api/v1/sources/{sid}", headers=H(api))).json()["status"] == "failed"


async def test_actualizar_exige_if_match(api):
    created = (await create(api)).json()
    url = f"/api/v1/sources/{created['source_id']}"
    r = await api.patch(url, json={"enabled": False}, headers=H(api))
    assert r.status_code == 428
    r = await api.patch(url, json={"enabled": False}, headers=H(api, **{"If-Match": '"1"'}))
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["version"] == 2
