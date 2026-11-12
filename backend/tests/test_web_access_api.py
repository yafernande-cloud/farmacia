"""G1 — acceso web: solicitud, campanita, aprobación con rol `admin`, superadministración.

Reglas que estas pruebas fijan (D-130, precisada por D-158):
  * Google/Supabase identifica, pero no concede acceso.
  * La autorización web es la fila `web_access_grant`, separada de la identidad.
  * La solicitud no otorga nada; solo la aprobación de un `admin` habilita.
  * Aprobar crea la persona con `roles = ['admin']`, `active` y `auth_subject`, sin vencimiento.
  * «Eliminar acceso» borra la autorización: la persona, su `auth_subject`, Telegram y el historial
    se conservan, y sin fila el JWT válido ya no abre la puerta.
  * Una persona aprobada entra al Cotizador; Telegram sigue siendo un plano aparte.
  * La superadministradora es control técnico: tampoco ve ni aprueba solicitudes de otra farmacia.
"""

from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError

from app.api import deps
from app.core.config import get_settings
from app.core.errors import DomainError
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AppUser,
    AuditEvent,
    Organization,
    OutboxEvent,
    WebAccessGrant,
    WebAccessRequest,
)
from app.services import web_access
from app.services.actors import load_actor
from tests.conftest import alembic_config

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
            if "auth_subject" in extra:
                # Estado tras la migración 0031: quien ya tenía identidad enlazada conserva su
                # autorización web vigente (sin vencimiento).
                s.add(
                    WebAccessGrant(
                        app_user_id=u.id,
                        organization_id=o.id,
                        granted_at=datetime.now(UTC),
                        expires_at=None,
                    )
                )
        await s.flush()
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


async def state(api, **kw):
    return await api.get("/api/v1/web-access-requests/status", headers=R(**kw))


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


async def test_estado_nuevo_no_crea_solicitud_ni_revela_datos(api, sessions):
    body = (await state(api)).json()
    assert body == {"status": "none", "requested_at": None, "decided_at": None}
    assert await count(sessions, WebAccessRequest) == 0


async def test_estado_pendiente_sin_duplicar_la_solicitud(api, sessions):
    await ask(api)
    body = (await state(api)).json()
    assert body["status"] == "pending" and body["requested_at"] is not None
    assert body["decided_at"] is None

    await ask(api)
    assert await count(sessions, WebAccessRequest) == 1


async def test_estado_rechazado_y_nueva_solicitud_conserva_el_historial(api, sessions):
    await ask(api)
    rid = await pending_id(api)
    rejected = await api.post(
        f"/api/v1/web-access-requests/{rid}/reject", json={}, headers=H(api)
    )
    assert rejected.status_code == 200
    body = (await state(api)).json()
    assert body["status"] == "rejected" and body["decided_at"] is not None

    # Volver a solicitar es una acción explícita: crea una nueva pendiente y conserva la anterior.
    assert (await ask(api)).status_code == 202
    async with sessions() as s:
        rows = (await s.execute(select(WebAccessRequest))).scalars().all()
    assert len(rows) == 2
    assert sorted(row.status for row in rows) == ["pending", "rejected"]
    assert (await state(api)).json()["status"] == "pending"


async def test_estado_no_revela_la_solicitud_de_otra_identidad(api):
    await ask(api)
    other = await state(api, subject="sub-otro", email="otro@example.test", name="Otra")
    assert other.json() == {"status": "none", "requested_at": None, "decided_at": None}


async def test_estado_de_identidad_ya_vinculada_no_ofrece_solicitud(api, sessions):
    body = (
        await state(api, subject="sub-admin", email="admin@example.test", name="Admin")
    ).json()
    assert body["status"] == "approved"
    assert await count(sessions, WebAccessRequest) == 0


async def test_estado_sin_organizacion_configurada(api, monkeypatch):
    monkeypatch.delenv("WEB_ACCESS_ORGANIZATION_ID", raising=False)
    get_settings.cache_clear()
    r = await state(api)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ACCESS_NOT_CONFIGURED"
    get_settings.cache_clear()


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


async def test_superadmin_no_ve_ni_aprueba_solicitudes_de_otra_organizacion(api, sessions):
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

    # La campanita está siempre acotada a la organización de quien administra, también para la
    # superadministración técnica (D-158): no se mezclan universos separados.
    body = (
        await api.get("/api/v1/web-access-requests", headers=H(api, "superadmin"))
    ).json()
    assert body == {"pending_count": 0, "items": []}

    # Una solicitud propia de su organización sí la ve.
    await ask(api)
    own = (await api.get("/api/v1/web-access-requests", headers=H(api, "superadmin"))).json()
    assert own["pending_count"] == 1
    assert own["items"][0]["organization"] is None

    r = await api.post(
        f"/api/v1/web-access-requests/{other_id}/approve", headers=H(api, "superadmin")
    )
    assert r.status_code == 404  # no aprueba en nombre de una farmacia ajena

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
        grant = (
            await s.execute(
                select(WebAccessGrant).where(WebAccessGrant.app_user_id == api.farm["admin"])
            )
        ).scalar_one()
        grant.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        grant.granted_at = datetime.now(UTC) - timedelta(days=1)

    async def fake_subject(token, settings):
        return "sub-admin"

    monkeypatch.setattr(deps, "verify_supabase_jwt", fake_subject)
    async with sessions() as s:
        with pytest.raises(DomainError) as e:
            await deps._actor_from_jwt(s, "Bearer token")
    assert (e.value.code, e.value.status_code) == ("WEB_ACCESS_EXPIRED", 403)


async def test_jwt_valido_sin_autorizacion_web_es_denegado(api, sessions, monkeypatch):
    """Eliminar la autorización bloquea la siguiente petición aunque el JWT siga vigente."""
    async with sessions() as s, s.begin():
        grant = (
            await s.execute(
                select(WebAccessGrant).where(WebAccessGrant.app_user_id == api.farm["admin"])
            )
        ).scalar_one()
        await s.delete(grant)

    async def fake_subject(token, settings):
        return "sub-admin"

    monkeypatch.setattr(deps, "verify_supabase_jwt", fake_subject)
    async with sessions() as s:
        with pytest.raises(DomainError) as e:
            await deps._actor_from_jwt(s, "Bearer token")
    assert (e.value.code, e.value.status_code) == ("WEB_ACCESS_REQUIRED", 403)


async def test_admin_con_autorizacion_entra_con_jwt(api, sessions, monkeypatch):
    """Compatibilidad con el login Google existente: identidad + autorización vigente = acceso."""

    async def fake_subject(token, settings):
        return "sub-admin"

    monkeypatch.setattr(deps, "verify_supabase_jwt", fake_subject)
    async with sessions() as s:
        actor = await deps._actor_from_jwt(s, "Bearer token")
    assert actor.id == api.farm["admin"]
    assert actor.organization_id == api.farm["org"]
    assert actor.roles == ["admin"]


async def _linked_person(api, sessions, *, subject="sub-linked", name="Persona vinculada",
                         roles=("admin",), expires_at=None):  # fmt: skip
    async with sessions() as s, s.begin():
        person = AppUser(
            organization_id=api.farm["org"],
            display_name=name,
            roles=list(roles),
            auth_subject=subject,
            active=True,
        )
        s.add(person)
        await s.flush()
        s.add(
            WebAccessGrant(
                app_user_id=person.id,
                organization_id=api.farm["org"],
                granted_at=datetime.now(UTC),
                expires_at=expires_at,
            )
        )
        await s.flush()
        person_id = person.id
    return person_id


async def test_eliminar_acceso_borra_la_autorizacion_y_conserva_la_identidad(api, sessions):
    uid = await _linked_person(api, sessions)

    r = await api.delete(f"/api/v1/admin/users/{uid}/web-access", headers=H(api))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "user_id": str(uid),
        "display_name": "Persona vinculada",
        "removed": True,
    }

    async with sessions() as s:
        # La autorización se eliminó de verdad: no queda fila, ni vencida.
        assert (
            await s.execute(select(WebAccessGrant).where(WebAccessGrant.app_user_id == uid))
        ).first() is None
        person = (await s.execute(select(AppUser).where(AppUser.id == uid))).scalar_one()
        # La identidad y sus permisos se conservan: reconocerá su cuenta si vuelve con Google.
        assert person.auth_subject == "sub-linked"
        assert person.active and person.roles == ["admin"]
        # Respaldo de rollback: columnas antiguas con un vencimiento pasado, nunca vigente.
        assert person.web_access_expires_at is not None
        assert person.web_access_expires_at < datetime.now(UTC)
        assert (
            await s.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "web_access.removed")
            )
        ).scalar_one() == 1

    # Idempotente: sin autorización no se audita un borrado inexistente.
    again = await api.delete(f"/api/v1/admin/users/{uid}/web-access", headers=H(api))
    assert again.status_code == 200
    assert again.json()["removed"] is False


async def test_estado_removed_y_nueva_solicitud_tras_eliminar(api, sessions):
    uid = await _linked_person(api, sessions)
    assert (
        await api.delete(f"/api/v1/admin/users/{uid}/web-access", headers=H(api))
    ).status_code == 200

    body = (
        await state(
            api, subject="sub-linked", email="linked@example.test", name="Persona vinculada"
        )
    ).json()
    assert body["status"] == "removed"

    # Volver a solicitar es una acción explícita, no una restauración automática.
    assert (
        await ask(api, subject="sub-linked", email="linked@example.test", name="Persona vinculada")
    ).status_code == 202
    assert (
        await state(
            api, subject="sub-linked", email="linked@example.test", name="Persona vinculada"
        )
    ).json()["status"] == "pending"


async def test_reaprobacion_sobre_la_misma_identidad_no_duplica_la_persona(api, sessions):
    uid = await _linked_person(api, sessions)
    await api.delete(f"/api/v1/admin/users/{uid}/web-access", headers=H(api))
    await ask(api, subject="sub-linked", email="linked@example.test", name="Persona vinculada")
    rid = await pending_id(api)

    approved = await api.post(f"/api/v1/web-access-requests/{rid}/approve", headers=H(api))
    assert approved.status_code == 200, approved.text
    assert approved.json()["user_id"] == str(uid)  # misma persona, sin duplicar
    async with sessions() as s:
        people = (
            await s.execute(select(AppUser).where(AppUser.auth_subject == "sub-linked"))
        ).scalars().all()
        assert len(people) == 1
        assert people[0].roles == ["admin"]  # aprobar la web no cambia roles
        grant = (
            await s.execute(select(WebAccessGrant).where(WebAccessGrant.app_user_id == uid))
        ).scalar_one()
        assert grant.expires_at is None
        request = (
            await s.execute(select(WebAccessRequest).where(WebAccessRequest.id == rid))
        ).scalar_one()
        assert request.status == "approved" and str(grant.request_id) == rid


async def test_reaprobar_identidad_existente_sin_admin_no_cambia_sus_roles(api, sessions):
    """Los roles se comparten con Telegram: aprobar no debe dar `admin` como efecto colateral."""
    async with sessions() as s, s.begin():
        person = AppUser(
            organization_id=api.farm["org"],
            display_name="Auxiliar vinculada",
            roles=["auxiliary"],
            auth_subject="sub-aux-linked",
            active=True,
        )
        s.add(person)
        await s.flush()
        uid = person.id
    await ask(api, subject="sub-aux-linked", email="aux@example.test", name="Auxiliar vinculada")
    rid = await pending_id(api)

    r = await api.post(f"/api/v1/web-access-requests/{rid}/approve", headers=H(api))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "WEB_ADMIN_ROLE_REQUIRED"
    async with sessions() as s:
        person = (await s.execute(select(AppUser).where(AppUser.id == uid))).scalar_one()
        assert person.roles == ["auxiliary"]  # sin cambios
        assert (
            await s.execute(select(WebAccessGrant).where(WebAccessGrant.app_user_id == uid))
        ).first() is None
        request = (
            await s.execute(select(WebAccessRequest).where(WebAccessRequest.id == rid))
        ).scalar_one()
        assert request.status == "pending"


async def test_protecciones_de_eliminacion(api, sessions):
    # Nadie elimina a la superadministradora.
    r = await api.delete(
        f"/api/v1/admin/users/{api.farm['superadmin']}/web-access", headers=H(api)
    )
    assert (r.status_code, r.json()["error"]["code"]) == (403, "SUPERADMIN_PROTECTED")

    # Nadie elimina su propio acceso.
    r = await api.delete(f"/api/v1/admin/users/{api.farm['admin']}/web-access", headers=H(api))
    assert (r.status_code, r.json()["error"]["code"]) == (409, "CANNOT_REMOVE_SELF")

    # No se cruza de organización (ni siquiera se revela que la persona existe).
    r = await api.delete(
        f"/api/v1/admin/users/{api.farm['outsider']}/web-access", headers=H(api)
    )
    assert r.status_code == 404


async def test_no_puede_eliminar_a_la_ultima_admin_web(api, sessions):
    async with sessions() as s, s.begin():
        other_admin = AppUser(
            organization_id=api.farm["org"], display_name="Admin Dos", roles=["admin"]
        )
        s.add(other_admin)
        await s.flush()
        actor_id = other_admin.id  # administradora sin autorización web propia

    r = await api.delete(
        f"/api/v1/admin/users/{api.farm['admin']}/web-access",
        headers={"X-Demo-User-Id": str(actor_id)},
    )
    assert (r.status_code, r.json()["error"]["code"]) == (409, "LAST_WEB_ADMIN")
    async with sessions() as s:
        assert (
            await s.execute(
                select(WebAccessGrant).where(WebAccessGrant.app_user_id == api.farm["admin"])
            )
        ).first() is not None


async def test_gestion_de_usuarios_lista_solo_autorizaciones_vigentes(api, sessions):
    async with sessions() as s, s.begin():
        # Autorización vencida: no se lista aunque exista la fila.
        expired = AppUser(
            organization_id=api.farm["org"], display_name="Vencida", roles=["admin"]
        )
        s.add(expired)
        await s.flush()
        s.add(
            WebAccessGrant(
                app_user_id=expired.id,
                organization_id=api.farm["org"],
                granted_at=datetime.now(UTC) - timedelta(days=10),
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await s.flush()

    body = (await api.get("/api/v1/admin/users", headers=H(api))).json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["display_name"] == "Admin"
    assert row["user_id"] == str(api.farm["admin"])
    assert row["roles"] == ["admin"]
    assert row["is_self"] is True and row["is_superadmin"] is False
    assert row["web_access_expires_at"] is None and row["granted_at"] is not None

    # Los auxiliares de Telegram sin autorización web no se mezclan en este listado.
    assert (await api.get("/api/v1/admin/users", headers=H(api, "aux"))).status_code == 403
    # Otra organización ve solo lo suyo.
    other = (await api.get("/api/v1/admin/users", headers=H(api, "outsider"))).json()
    assert [item["display_name"] for item in other["items"]] == ["Externo"]


def test_migracion_0031_rellena_autorizaciones_vigentes(db_url):
    """El relleno conserva el acceso de quienes ya lo tenían y no inventa autorizaciones."""
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "0030")
    eng = create_engine(db_url)
    try:
        with eng.begin() as c:
            org_id = c.execute(text("SELECT gen_random_uuid()")).scalar_one()
            c.execute(
                text("INSERT INTO organization (id, name, kind) VALUES (:o, 'Migración 0031', 'simulation')"),
                {"o": org_id},
            )
            rows = {
                "vigente": (True, "sub-vigente", None),
                "futuro": (True, "sub-futuro", "now() + interval '30 days'"),
                "vencido": (True, "sub-vencido", "now() - interval '1 day'"),
                "inactivo": (False, "sub-inactivo", None),
                "sin_identidad": (True, None, None),
            }
            for name, (active, subject, expiry) in rows.items():
                expiry_sql = expiry or "NULL"
                c.execute(
                    text(
                        "INSERT INTO app_user (organization_id, display_name, roles, active,"
                        " auth_subject, web_access_expires_at) VALUES (:o, :n, ARRAY['admin'],"
                        f" :a, :s, {expiry_sql})"
                    ),
                    {"o": org_id, "n": name, "a": active, "s": subject},
                )

        command.upgrade(cfg, "head")

        with eng.connect() as c:
            granted = c.execute(
                text(
                    "SELECT u.display_name FROM web_access_grant g"
                    " JOIN app_user u ON u.id = g.app_user_id"
                    " WHERE u.organization_id = :o ORDER BY u.display_name"
                ),
                {"o": org_id},
            ).scalars().all()
            expiry = c.execute(
                text(
                    "SELECT g.expires_at FROM web_access_grant g JOIN app_user u"
                    " ON u.id = g.app_user_id WHERE u.display_name = 'futuro'"
                )
            ).scalar_one()
            legacy = c.execute(
                text(
                    "SELECT count(*) FROM web_access_grant g JOIN app_user u"
                    " ON u.id = g.app_user_id WHERE u.display_name = 'vigente'"
                )
            ).scalar_one()
        # Solo las personas activas, con identidad y acceso vigente reciben autorización.
        assert granted == ["futuro", "vigente"]
        assert legacy == 1
        assert expiry is not None  # el vencimiento futuro se conserva
    finally:
        eng.dispose()


async def test_superadmin_otorga_en_otra_organizacion_sin_romper_la_trazabilidad(api, sessions):
    async with sessions() as s, s.begin():
        persona = AppUser(
            organization_id=api.farm["other_org"],
            display_name="Otra persona",
            roles=["admin"],
        )
        s.add(persona)
        await s.flush()
        uid = persona.id

    r = await api.post(
        f"/api/v1/admin/users/{uid}/web-access", json={}, headers=H(api, "superadmin")
    )
    assert r.status_code == 200, r.text
    async with sessions() as s:
        grant = (
            await s.execute(select(WebAccessGrant).where(WebAccessGrant.app_user_id == uid))
        ).scalar_one()
        # `granted_by` referencia a una persona de la misma organización; si no la hay, queda nula.
        assert grant.granted_by is None
        persona = (await s.execute(select(AppUser).where(AppUser.id == uid))).scalar_one()
        assert persona.web_access_granted_by is None
        audit = (
            await s.execute(
                select(AuditEvent)
                .where(AuditEvent.action == "web_access.granted")
                .order_by(AuditEvent.created_at.desc())
                .limit(1)
            )
        ).scalar_one()
        assert audit.actor_id is None  # trazabilidad sin atribuir a otra organización

    revocado = await api.post(
        f"/api/v1/admin/users/{uid}/web-access/revoke", headers=H(api, "superadmin")
    )
    assert revocado.status_code == 200, revocado.text
    async with sessions() as s:
        assert (
            await s.execute(select(WebAccessGrant).where(WebAccessGrant.app_user_id == uid))
        ).first() is None


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
