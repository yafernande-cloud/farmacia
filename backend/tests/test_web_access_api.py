"""G1 — acceso web: solicitud, campanita, aprobación con rol `admin`, superadministración.

Reglas que estas pruebas fijan (D-130):
  * Google/Supabase identifica, pero no concede acceso.
  * La solicitud no otorga nada; solo la aprobación de un `admin` habilita.
  * Aprobar crea la persona con `roles = ['admin']`, `active` y `auth_subject`, sin vencimiento.
  * Una persona aprobada entra al Cotizador; Telegram sigue siendo un plano aparte.
  * La superadministradora es control técnico: ve todas las solicitudes, pero no aprueba ajenas.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api import deps
from app.core.config import get_settings
from app.core.errors import DomainError
from app.db.session import get_session
from app.main import create_app
from app.models import AppUser, AuditEvent, Organization, OutboxEvent, WebAccessRequest
from app.services import web_access
from app.services.actors import load_actor

pytestmark = pytest.mark.db


@pytest.fixture
async def farm(sessions):
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia acceso", kind="simulation")
        other = Organization(name="Otra acceso", kind="simulation")
        s.add_all([org, other])
        await s.flush()
        ids["org"], ids["other_org"] = org.id, other.id
        for key, name, roles, o, extra in [
            ("admin", "Admin", ["admin"], org, {"auth_subject": "sub-admin"}),
            ("aux", "Auxiliar", ["auxiliary"], org, {}),
            ("superadmin", "Yarella", ["admin"], org, {"is_superadmin": True}),
            ("outsider", "Externo", ["admin"], other, {"auth_subject": "sub-outside"}),
        ]:
            u = AppUser(organization_id=o.id, display_name=name, roles=roles, **extra)
            s.add(u)
            await s.flush()
            ids[key] = u.id
    return ids


@pytest.fixture(autouse=True)
def _rate_limits():
    from app.api.v1 import web_access as api

    api._per_subject.reset()
    api._per_ip.reset()
    yield
    api._per_subject.reset()
    api._per_ip.reset()


@pytest.fixture
async def api(sessions, farm, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    monkeypatch.setenv("WEB_ACCESS_ORGANIZATION_ID", str(farm["org"]))
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


def R(subject="sub-nuevo", email="nueva@example.test", name="Nueva Persona"):
    return {
        "X-Demo-Subject": subject,
        "X-Demo-Email": email,
        "X-Demo-Name": name,
    }


async def ask(api, **kw):
    return await api.post("/api/v1/web-access-requests", json={}, headers=R(**kw))


async def pending_id(api, who="admin") -> str:
    body = (await api.get("/api/v1/web-access-requests", headers=H(api, who))).json()
    return body["items"][0]["id"]


async def count(sessions, model) -> int:
    async with sessions() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_solicitud_publica_crea_pendiente_sin_otorgar_acceso(api, sessions):
    r = await ask(api)
    assert r.status_code == 202, r.text
    assert r.json() == {"received": True}
    async with sessions() as s:
        row = (await s.execute(select(WebAccessRequest))).scalar_one()
        assert row.status == "pending"
        assert row.organization_id == api.farm["org"]
        assert (row.auth_subject, row.email, row.display_name) == (
            "sub-nuevo",
            "nueva@example.test",
            "Nueva Persona",
        )
        assert row.data_origin == "simulated"  # organización de simulación
        assert row.decided_at is None and row.decided_by is None
        # No existe persona: la solicitud no otorga nada.
        assert (
            await s.execute(select(AppUser).where(AppUser.auth_subject == "sub-nuevo"))
        ).first() is None
        assert (await s.execute(select(func.count()).select_from(AuditEvent))).scalar_one() == 1


async def test_solicitud_repetida_no_duplica_ni_revela_estado(api, sessions):
    first = await ask(api)
    second = await ask(api, name="Otro Nombre")
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json() == {"received": True}
    assert await count(sessions, WebAccessRequest) == 1


async def test_identidad_ya_vinculada_no_crea_solicitud(api, sessions):
    r = await ask(api, subject="sub-admin", email="admin@example.test", name="Admin")
    assert r.status_code == 202
    assert r.json() == {"received": True}
    assert await count(sessions, WebAccessRequest) == 0


async def test_campanita_solo_para_admin_y_acotada_a_su_organizacion(api, sessions):
    await ask(api)
    body = (await api.get("/api/v1/web-access-requests", headers=H(api, "admin"))).json()
    assert body["pending_count"] == 1
    assert body["items"][0]["email"] == "nueva@example.test"
    assert body["items"][0]["organization"] is None  # solo lo ve la superadministradora

    assert (await api.get("/api/v1/web-access-requests", headers=H(api, "aux"))).status_code == 403
    other = (await api.get("/api/v1/web-access-requests", headers=H(api, "outsider"))).json()
    assert other == {"pending_count": 0, "items": []}


async def test_aprobar_crea_admin_activo_sin_vencimiento(api, sessions):
    await ask(api)
    rid = await pending_id(api)
    r = await api.post(f"/api/v1/web-access-requests/{rid}/approve", headers=H(api))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["roles"] == ["admin"]
    assert body["web_access_expires_at"] is None
    assert body["display_name"] == "Nueva Persona"

    async with sessions() as s:
        user = (
            await s.execute(select(AppUser).where(AppUser.id == body["user_id"]))
        ).scalar_one()
        assert user.roles == ["admin"]
        assert user.active and user.available
        assert user.auth_subject == "sub-nuevo"
        assert user.email == "nueva@example.test"
        assert user.web_access_expires_at is None
        assert user.web_access_granted_by == api.farm["admin"]
        request = (
            await s.execute(select(WebAccessRequest).where(WebAccessRequest.id == rid))
        ).scalar_one()
        assert request.status == "approved"
        assert request.decided_by == api.farm["admin"] and request.decided_at is not None
        assert (
            await s.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "web_access.approved")
            )
        ).scalar_one() == 1
        assert (
            await s.execute(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.type == "web_access.decided")
            )
        ).scalar_one() == 1

    again = await api.post(f"/api/v1/web-access-requests/{rid}/approve", headers=H(api))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "REQUEST_ALREADY_DECIDED"


async def test_aprobado_entra_al_cotizador_como_admin(api, sessions):
    await ask(api)
    rid = await pending_id(api)
    body = (await api.post(f"/api/v1/web-access-requests/{rid}/approve", headers=H(api))).json()
    new_user = str(body["user_id"])

    async with sessions() as s:
        actor = await load_actor(s, body["user_id"])
    assert actor is not None and actor.roles == ["admin"] and not actor.is_superadmin

    r = await api.post(
        "/api/v1/suppliers",
        json={"name": "Proveedor de la persona nueva", "supplier_code": "NUEVA-1"},
        headers={"X-Demo-User-Id": new_user},
    )
    assert r.status_code == 201, r.text


async def test_rechazar_no_crea_persona_y_conserva_motivo(api, sessions):
    await ask(api)
    rid = await pending_id(api)
    r = await api.post(
        f"/api/v1/web-access-requests/{rid}/reject",
        json={"note": "No corresponde a esta farmacia"},
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    async with sessions() as s:
        request = (
            await s.execute(select(WebAccessRequest).where(WebAccessRequest.id == rid))
        ).scalar_one()
        assert request.status == "rejected"
        assert request.decision_note == "No corresponde a esta farmacia"
        assert (await s.execute(select(func.count()).select_from(AppUser))).scalar_one() == 4
    again = await api.post(f"/api/v1/web-access-requests/{rid}/approve", headers=H(api))
    assert again.status_code == 409


async def test_no_sobrescribe_una_persona_activa_con_el_mismo_nombre(api, sessions):
    await ask(api, name="Admin")
    rid = await pending_id(api)
    r = await api.post(f"/api/v1/web-access-requests/{rid}/approve", headers=H(api))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DUPLICATE_NAME"
    async with sessions() as s:
        request = (
            await s.execute(select(WebAccessRequest).where(WebAccessRequest.id == rid))
        ).scalar_one()
        assert request.status == "pending"  # la administradora decide qué hacer
    assert await count(sessions, AppUser) == 4


async def test_otra_organizacion_no_puede_aprobar(api, sessions):
    await ask(api)
    rid = await pending_id(api)
    r = await api.post(f"/api/v1/web-access-requests/{rid}/approve", headers=H(api, "outsider"))
    assert r.status_code == 404


async def test_superadmin_ve_todas_las_organizaciones_pero_no_aprueba_ajenas(api, sessions):
    async with sessions() as s, s.begin():
        other_request = WebAccessRequest(
            organization_id=api.farm["other_org"],
            auth_subject="sub-otra-org",
            email="otra@example.test",
            display_name="Persona de otra farmacia",
            status="pending",
            requested_at=datetime.now(UTC),
            data_origin="simulated",
        )
        s.add(other_request)
        await s.flush()
        other_id = other_request.id

    body = (
        await api.get("/api/v1/web-access-requests", headers=H(api, "superadmin"))
    ).json()
    assert body["pending_count"] == 1
    assert body["items"][0]["organization"] == "Otra acceso"

    r = await api.post(
        f"/api/v1/web-access-requests/{other_id}/approve", headers=H(api, "superadmin")
    )
    assert r.status_code == 404  # no aprueba en nombre de una farmacia

    own = await api.post(f"/api/v1/web-access-requests/{other_id}/approve", headers=H(api))
    assert own.status_code == 404  # tampoco la administradora de la otra organización


async def test_revocar_y_ajustar_es_solo_superadministracion(api, sessions):
    target = str(api.farm["aux"])
    assert (
        await api.post(
            f"/api/v1/admin/users/{target}/web-access", json={}, headers=H(api, "admin")
        )
    ).status_code == 403

    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    r = await api.post(
        f"/api/v1/admin/users/{target}/web-access",
        json={"roles": ["admin"], "expires_at": future},
        headers=H(api, "superadmin"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["roles"] == ["admin"]

    invalid = await api.post(
        f"/api/v1/admin/users/{target}/web-access",
        json={"roles": ["superadmin"]},
        headers=H(api, "superadmin"),
    )
    assert invalid.status_code == 422

    revoked = await api.post(
        f"/api/v1/admin/users/{target}/web-access/revoke",
        headers=H(api, "superadmin"),
    )
    assert revoked.status_code == 200
    async with sessions() as s:
        user = (await s.execute(select(AppUser).where(AppUser.id == target))).scalar_one()
        assert user.web_access_expires_at is not None
        assert user.active  # revocar la web no desactiva ni toca Telegram
        assert (
            await s.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action.in_(["web_access.granted", "web_access.revoked"]))
            )
        ).scalar_one() == 2


async def test_acceso_vencido_se_rechaza_al_autenticar(api, sessions, monkeypatch):
    async with sessions() as s, s.begin():
        user = (await s.execute(select(AppUser).where(AppUser.id == api.farm["admin"]))).scalar_one()
        user.web_access_expires_at = datetime.now(UTC) - timedelta(minutes=1)

    async def fake_subject(token, settings):
        return "sub-admin"

    monkeypatch.setattr(deps, "verify_supabase_jwt", fake_subject)
    async with sessions() as s:
        with pytest.raises(DomainError) as e:
            await deps._actor_from_jwt(s, "Bearer token")
    assert (e.value.code, e.value.status_code) == ("WEB_ACCESS_EXPIRED", 403)


async def test_set_superadmin_es_unico_y_auditado(api, sessions):
    async with sessions() as s, s.begin():
        second = AppUser(
            organization_id=api.farm["org"], display_name="Segunda", roles=["admin"]
        )
        s.add(second)
        await s.flush()
        second_id = second.id
    async with sessions() as s, s.begin():
        with pytest.raises(DomainError) as e:
            await web_access.set_superadmin(s, second_id)
    assert e.value.code == "SUPERADMIN_ALREADY_SET"
    async with sessions() as s:
        assert (
            await s.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "app_user.superadmin_set")
            )
        ).scalar_one() == 0


async def test_la_base_impide_dos_superadmins_activas(api, sessions):
    async with sessions() as s, s.begin():
        with pytest.raises(IntegrityError):
            await s.execute(
                AppUser.__table__.update()
                .where(AppUser.id == api.farm["admin"])
                .values(is_superadmin=True)
            )


async def test_sin_organizacion_configurada_no_hay_solicitud(api, sessions, monkeypatch):
    monkeypatch.delenv("WEB_ACCESS_ORGANIZATION_ID", raising=False)
    get_settings.cache_clear()
    r = await ask(api)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ACCESS_NOT_CONFIGURED"
    get_settings.cache_clear()


async def test_limite_de_tasa_por_identidad(api):
    for _ in range(5):
        assert (await ask(api)).status_code == 202
    blocked = await ask(api)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
