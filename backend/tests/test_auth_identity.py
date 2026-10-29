"""Enlace de la identidad web (Supabase Auth) con el personal — PostgreSQL real.

No hay proyecto Supabase real: los `sub` son identificadores ficticios y la verificación del JWT se
sustituye por una función de prueba (igual que en `test_telegram_accounts.py`). Lo que se prueba es
lo que sí es nuestro: el enlace `app_user.auth_subject`, sus validaciones y su auditoría.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api import deps as deps_module
from app.api.deps import get_session
from app.cli import _subject_arg
from app.cli import link_auth_subject as cli_link
from app.cli import unlink_auth_subject as cli_unlink
from app.core.config import get_settings
from app.core.errors import DomainError
from app.main import create_app
from app.models import AppUser, AuditEvent, Organization, OutboxEvent
from app.services import identity
from app.services.actors import Actor

pytestmark = pytest.mark.db


@pytest.fixture
async def people(sessions):
    """Dos organizaciones reales y una de simulación, con una persona inactiva y un `sub` ya usado."""
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Real identidad", kind="real_test")
        other = Organization(name="Otra real", kind="real_test")
        sim = Organization(name="Sim identidad", kind="simulation")
        s.add_all([org, other, sim])
        await s.flush()
        ids["org"], ids["other"], ids["sim"] = org.id, other.id, sim.id
        for key, name, roles, o, subject, active in [
            ("pao", "Pao", ["admin", "purchasing"], org, None, True),
            ("ana", "Ana", ["auxiliary"], org, None, True),
            ("baja", "Baja", ["auxiliary"], org, "sub-baja", False),
            ("otra", "Otra", ["admin"], other, None, True),
            ("externa", "Externa", ["auxiliary"], other, "sub-externo", True),
            ("simpao", "Sim Pao", ["admin"], sim, None, True),
        ]:
            u = AppUser(
                organization_id=o.id, display_name=name, roles=roles, auth_subject=subject,
                active=active,
            )  # fmt: skip
            s.add(u)
            await s.flush()
            ids[key] = u.id
    return ids


async def call(sessions, people, fn, *a, who="pao", roles=("admin",), org="org", **kw):
    """Ejecuta el servicio con su propia transacción, como la CLI."""
    async with sessions() as s:
        actor = Actor(people[who], people[org], "Prueba", list(roles))
        try:
            r = await fn(s, actor, *a, **kw)
            await s.commit()
            return r
        except BaseException:
            await s.rollback()
            raise


async def one(sessions, model, *where):
    async with sessions() as s:
        return (await s.execute(select(model).where(*where))).scalar_one_or_none()


async def n(sessions, model, *where) -> int:
    async with sessions() as s:
        return (
            await s.execute(select(func.count()).select_from(model).where(*where))
        ).scalar_one()


async def test_enlaza_identidad_y_deja_auditoria_y_evento(sessions, people):
    r = await call(
        sessions, people, identity.link_auth_subject, people["ana"], subject="  sub-ana  "
    )
    assert r == {
        "user_id": people["ana"],
        "display_name": "Ana",
        "auth_subject": "sub-ana",
        "changed": True,
    }
    ana = await one(sessions, AppUser, AppUser.id == people["ana"])
    assert ana.auth_subject == "sub-ana"

    audit = await one(sessions, AuditEvent, AuditEvent.action == "auth_subject.linked")
    assert audit.actor_id == people["pao"] and audit.organization_id == people["org"]
    assert audit.entity_type == "app_user" and audit.entity_id == people["ana"]
    assert (audit.before, audit.after) == ({"auth_subject": None}, {"auth_subject": "sub-ana"})

    out = await one(sessions, OutboxEvent, OutboxEvent.type == "identity.linked")
    assert out.aggregate_id == people["ana"]
    assert out.payload == {"user_id": str(people["ana"]), "auth_subject": "sub-ana"}


async def test_enlazar_lo_mismo_dos_veces_no_duplica_auditoria(sessions, people):
    await call(sessions, people, identity.link_auth_subject, people["ana"], subject="sub-ana")
    r = await call(sessions, people, identity.link_auth_subject, people["ana"], subject="sub-ana")
    assert r["changed"] is False
    assert await n(sessions, AuditEvent, AuditEvent.action == "auth_subject.linked") == 1
    assert await n(sessions, OutboxEvent, OutboxEvent.type == "identity.linked") == 1


async def test_rechaza_un_sub_ya_usado_sin_decir_de_quien_es(sessions, people):
    with pytest.raises(DomainError) as exc:
        await call(
            sessions, people, identity.link_auth_subject, people["ana"], subject="sub-externo"
        )
    assert exc.value.code == "AUTH_SUBJECT_IN_USE"
    assert "Externa" not in exc.value.message  # no filtra personas de otra organización
    assert (await one(sessions, AppUser, AppUser.id == people["ana"])).auth_subject is None
    assert await n(sessions, AuditEvent) == 0


async def test_no_enlaza_personas_de_otra_organizacion(sessions, people):
    with pytest.raises(DomainError) as exc:
        await call(
            sessions, people, identity.link_auth_subject, people["otra"], subject="sub-x",
            org="org",
        )  # fmt: skip
    assert exc.value.code == "NOT_FOUND"


async def test_no_enlaza_a_una_persona_inactiva(sessions, people):
    with pytest.raises(DomainError) as exc:
        await call(sessions, people, identity.link_auth_subject, people["baja"], subject="sub-baja")
    assert exc.value.code == "USER_INACTIVE"
    assert await n(sessions, AuditEvent) == 0


async def test_no_enlaza_a_una_persona_inactiva_sin_enlace_previo(sessions, people):
    async with sessions() as s, s.begin():
        u = await s.get(AppUser, people["ana"])
        u.active = False
    with pytest.raises(DomainError) as exc:
        await call(sessions, people, identity.link_auth_subject, people["ana"], subject="sub-ana")
    assert exc.value.code == "USER_INACTIVE"
    assert (await one(sessions, AppUser, AppUser.id == people["ana"])).auth_subject is None


@pytest.mark.parametrize("subject", ["", "   ", "a b", "a\tb", "x" * 101])
async def test_rechaza_subject_invalidos(sessions, people, subject):
    with pytest.raises(DomainError) as exc:
        await call(sessions, people, identity.link_auth_subject, people["ana"], subject=subject)
    assert exc.value.code == "INVALID_SUBJECT"
    assert await n(sessions, AuditEvent) == 0


async def test_exige_permiso_manage_identities(sessions, people):
    """Ni auxiliares ni compras sin `admin` enlazan (ni consultan) identidades web."""
    for who, roles in [("ana", ("auxiliary",)), ("pao", ("purchasing",))]:
        with pytest.raises(DomainError) as exc:
            await call(
                sessions, people, identity.link_auth_subject, people["ana"],
                subject="sub-ana", who=who, roles=roles,
            )  # fmt: skip
        assert exc.value.code == "FORBIDDEN"
        with pytest.raises(DomainError) as exc:
            await call(sessions, people, identity.list_auth_subjects, who=who, roles=roles)
        assert exc.value.code == "FORBIDDEN"
    assert (await one(sessions, AppUser, AppUser.id == people["ana"])).auth_subject is None
    assert await n(sessions, AuditEvent) == 0


async def test_no_reemplaza_un_enlace_existente_sin_desvincular(sessions, people):
    await call(sessions, people, identity.link_auth_subject, people["ana"], subject="sub-uno")
    with pytest.raises(DomainError) as exc:
        await call(sessions, people, identity.link_auth_subject, people["ana"], subject="sub-dos")
    assert exc.value.code == "AUTH_SUBJECT_ALREADY_SET"
    assert (await one(sessions, AppUser, AppUser.id == people["ana"])).auth_subject == "sub-uno"


async def test_desvincula_y_audita_sin_borrar_a_la_persona(sessions, people):
    await call(sessions, people, identity.link_auth_subject, people["ana"], subject="sub-ana")
    r = await call(sessions, people, identity.unlink_auth_subject, people["ana"])
    assert r["changed"] is True and r["auth_subject"] is None
    ana = await one(sessions, AppUser, AppUser.id == people["ana"])
    assert ana is not None and ana.auth_subject is None  # la fila NO se elimina

    audit = await one(sessions, AuditEvent, AuditEvent.action == "auth_subject.unlinked")
    assert audit.actor_id == people["pao"] and audit.entity_id == people["ana"]
    assert (audit.before, audit.after) == ({"auth_subject": "sub-ana"}, {"auth_subject": None})
    assert await n(sessions, OutboxEvent, OutboxEvent.type == "identity.unlinked") == 1

    with pytest.raises(DomainError) as exc:
        await call(sessions, people, identity.unlink_auth_subject, people["ana"])
    assert exc.value.code == "AUTH_SUBJECT_NOT_SET"


async def test_tambien_desvincula_a_una_persona_inactiva(sessions, people):
    """Quitar un acceso no depende de que la persona siga activa (limpieza sin borrar historial)."""
    r = await call(sessions, people, identity.unlink_auth_subject, people["baja"])
    assert r["auth_subject"] is None
    assert (await one(sessions, AppUser, AppUser.id == people["baja"])).auth_subject is None


async def test_lista_solo_su_organizacion_y_sin_correos(sessions, people):
    await call(sessions, people, identity.link_auth_subject, people["ana"], subject="sub-ana")
    rows = await call(sessions, people, identity.list_auth_subjects)
    assert [r["display_name"] for r in rows] == ["Ana", "Baja", "Pao"]
    flags = {r["display_name"]: r["linked"] for r in rows}
    assert flags == {"Ana": True, "Baja": True, "Pao": False}
    assert all("email" not in r for r in rows)
    expected = {"user_id", "display_name", "roles", "active", "auth_subject", "linked"}
    assert all(set(r) == expected for r in rows)


# ---------------------------------------------------------------- integración con la API


@pytest.fixture
async def jwt_api(sessions, people, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "supabase_jwt")
    monkeypatch.setenv(
        "SUPABASE_JWKS_URL", "https://example.supabase.co/auth/v1/.well-known/jwks.json"
    )
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", "https://example.supabase.co/auth/v1")
    get_settings.cache_clear()

    async def fake_verify(token, _settings):
        subs = {"token-pao": "sub-pao-nuevo", "token-otra": "sub-de-otra-org"}
        if token in subs:
            return subs[token]
        raise DomainError("INVALID_TOKEN", "La sesión no es válida o expiró.", status_code=401)

    monkeypatch.setattr(deps_module, "verify_supabase_jwt", fake_verify)
    app = create_app()

    async def _s():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    get_settings.cache_clear()


async def test_jwt_resuelve_la_persona_con_el_enlace_hecho_en_la_cli(jwt_api, sessions, people):
    await call(
        sessions, people, identity.link_auth_subject, people["pao"], subject="sub-pao-nuevo"
    )
    r = await jwt_api.get("/api/v1/me", headers={"Authorization": "Bearer token-pao"})
    assert r.status_code == 200
    body = r.json()
    assert body["display_name"] == "Pao" and body["roles"] == ["admin", "purchasing"]
    assert body["organization_kind"] == "real_test" and body["id"] == str(people["pao"])

    # Un usuario válido de Supabase sin fila enlazada: 403 y ningún permiso.
    r = await jwt_api.get("/api/v1/me", headers={"Authorization": "Bearer token-otra"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "USER_NOT_PROVISIONED"

    # Sin token, 401; la cabecera de demostración no sirve en modo JWT.
    assert (await jwt_api.get("/api/v1/me")).status_code == 401
    r = await jwt_api.get("/api/v1/me", headers={"X-Demo-User-Id": str(people["pao"])})
    assert r.status_code == 401


async def test_staff_exige_sesion_fuerte_en_organizacion_real(sessions, people, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    app = create_app()

    async def _s():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/v1/staff", headers={"X-Demo-User-Id": str(people["pao"])})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "STRONG_AUTH_REQUIRED"

        # La simulación mantiene el comportamiento de demo (no rompe demos ni pruebas).
        r = await c.get("/api/v1/staff", headers={"X-Demo-User-Id": str(people["simpao"])})
        assert r.status_code == 200
        assert [s["display_name"] for s in r.json()] == ["Sim Pao"]
    get_settings.cache_clear()


# ---------------------------------------------------------------- CLI (guardas sin base)


def test_cli_toma_el_subject_de_la_bandera():
    assert _subject_arg(["--subject", "sub-123"]) == "sub-123"


def test_cli_no_inventa_un_subject_sin_terminal():
    with pytest.raises(SystemExit):
        _subject_arg([])
    with pytest.raises(SystemExit):
        _subject_arg(["--subject"])


async def test_cli_unlink_exige_confirmacion():
    with pytest.raises(SystemExit) as exc:
        await cli_unlink(str(uuid.uuid4()), "sim", ["--by", str(uuid.uuid4())])
    assert "--confirm" in str(exc.value)


async def test_cli_link_no_acepta_un_subject_vacio_por_omision():
    with pytest.raises(SystemExit):
        await cli_link(str(uuid.uuid4()), "sim", ["--by", str(uuid.uuid4())])
