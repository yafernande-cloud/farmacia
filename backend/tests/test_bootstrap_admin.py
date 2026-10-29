"""Bootstrap de la primera administradora web (`create-admin`) — PostgreSQL real.

No hay usuarios de Supabase: lo que se prueba es la fila de nuestra base (solo rol `admin`, sin enlace),
su auditoría como acción del operador y las restricciones que impiden usarla como fábrica de admins.
"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.cli import _org_arg
from app.cli import create_admin as cli_create_admin
from app.core.errors import DomainError
from app.core.permissions import can_report
from app.models import AppUser, AuditEvent, Organization, OutboxEvent
from app.services import staff

pytestmark = pytest.mark.db


@pytest.fixture
async def orgs(sessions):
    """Una organización de prueba real, otra en producción y una de simulación con Pao ya administradora."""
    ids = {}
    async with sessions() as s, s.begin():
        real = Organization(name="Real bootstrap", kind="real_test")
        prod = Organization(name="Produccion bootstrap", kind="production")
        sim = Organization(name="Sim bootstrap", kind="simulation")
        legacy = Organization(name="Archivo bootstrap", kind="legacy_mixed")
        s.add_all([real, prod, sim, legacy])
        await s.flush()
        ids["org"], ids["prod"], ids["sim"], ids["legacy"] = (
            real.id,
            prod.id,
            sim.id,
            legacy.id,
        )
        pao = AppUser(
            organization_id=real.id, display_name="Pao", roles=["admin", "purchasing"]
        )  # fmt: skip
        s.add(pao)
        await s.flush()
        ids["pao"] = pao.id
    return ids


async def run(sessions, org, name="Yarella Fernández"):
    async with sessions() as s:
        try:
            u = await staff.bootstrap_admin(
                s, organization_id=org, display_name=name
            )  # fmt: skip
            await s.commit()
            return u
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


async def test_crea_solo_admin_sin_enlace_y_no_toca_a_pao(sessions, orgs):
    pao_before = await one(sessions, AppUser, AppUser.id == orgs["pao"])
    before = (pao_before.display_name, list(pao_before.roles), pao_before.auth_subject)

    u = await run(sessions, orgs["org"])

    assert u.display_name == "Yarella Fernández"
    assert u.roles == ["admin"]
    assert u.auth_subject is None
    assert u.active and u.available
    assert can_report(u.roles) is False  # no reporta faltantes ni aparece en el selector del bot
    assert u.organization_id == orgs["org"]

    pao_after = await one(sessions, AppUser, AppUser.id == orgs["pao"])
    assert (pao_after.display_name, list(pao_after.roles), pao_after.auth_subject) == before
    assert pao_after.updated_at == pao_before.updated_at


async def test_audita_como_accion_del_operador_y_emite_evento(sessions, orgs):
    u = await run(sessions, orgs["org"])

    audit = await one(sessions, AuditEvent, AuditEvent.entity_id == u.id)
    assert audit.action == "staff.bootstrap_created"
    assert audit.actor_id is None  # la ejecuta el operador, no una persona de la farmacia
    assert audit.organization_id == orgs["org"]
    assert audit.after["roles"] == ["admin"]
    assert audit.after["display_name"] == "Yarella Fernández"

    out = await one(sessions, OutboxEvent, OutboxEvent.aggregate_id == u.id)
    assert out.type == "staff.updated"
    assert out.payload == {"user_id": str(u.id), "origin": "cli_bootstrap"}


async def test_no_es_una_fabrica_de_administradores(sessions, orgs):
    await run(sessions, orgs["org"])
    with pytest.raises(DomainError) as exc:
        await run(sessions, orgs["org"], name="Otra Admin")
    assert exc.value.code == "BOOTSTRAP_ALREADY_DONE"
    assert await n(sessions, AppUser, AppUser.display_name == "Otra Admin") == 0


async def test_se_niega_si_ya_hay_una_administradora_con_acceso_web(sessions, orgs):
    """Pao enlazada es una vía de entrada: el bootstrap ya no corresponde."""
    async with sessions() as s, s.begin():
        pao = await s.get(AppUser, orgs["pao"])
        pao.auth_subject = "sub-pao"
    with pytest.raises(DomainError) as exc:
        await run(sessions, orgs["org"])
    assert exc.value.code == "ADMIN_ACCESS_EXISTS"
    assert await n(sessions, AppUser, AppUser.display_name == "Yarella Fernández") == 0


async def test_permite_recuperar_el_acceso_si_el_admin_de_bootstrap_esta_inactivo(sessions, orgs):
    u = await run(sessions, orgs["org"])
    async with sessions() as s, s.begin():
        (await s.get(AppUser, u.id)).active = False
    again = await run(sessions, orgs["org"])
    assert again.id != u.id
    assert again.roles == ["admin"]


@pytest.mark.parametrize("where", ["prod", "legacy"])
async def test_no_se_permite_en_produccion_ni_en_el_archivo(sessions, orgs, where):
    with pytest.raises(DomainError) as exc:
        await run(sessions, orgs[where])
    assert exc.value.code == "BOOTSTRAP_NOT_ALLOWED"
    assert await n(sessions, AppUser, AppUser.organization_id == orgs[where]) == 0


async def test_rechaza_nombre_duplicado_de_una_persona_activa(sessions, orgs):
    with pytest.raises(DomainError) as exc:
        await run(sessions, orgs["org"], name="pao")
    assert exc.value.code == "DUPLICATE_NAME"


@pytest.mark.parametrize("name", ["", "   ", "x" * 101, "\x01abc"])
async def test_rechaza_nombres_invalidos(sessions, orgs, name):
    with pytest.raises(DomainError) as exc:
        await run(sessions, orgs["org"], name=name)
    assert exc.value.code == "INVALID_NAME"


async def test_simulacion_tambien_puede_arrancar_sin_admin(sessions, orgs):
    """La simulación es un entorno aislado de prueba: ahí también se necesita una primera administradora."""
    u = await run(sessions, orgs["sim"], name="Sim Admin")
    assert u.organization_id == orgs["sim"]
    assert u.roles == ["admin"]


# ---------- CLI (guardas sin base de datos) ----------


def test_cli_exige_confirm():
    with pytest.raises(SystemExit) as exc:
        asyncio.run(cli_create_admin("Yarella Fernández", "real", ["create-admin"]))
    assert "--confirm" in str(exc.value)


def test_cli_exige_organizacion_explicita():
    with pytest.raises(SystemExit) as exc:
        _org_arg(["create-admin", "Yarella Fernández", "--confirm"])
    assert "--org sim | --org real" in str(exc.value)


async def test_el_admin_de_bootstrap_puede_gestionar_el_personal(sessions, orgs):
    """Puede actuar por CLI (`--by`) para el enlace de su propio sub: tiene `manage_staff`/`manage_identities`."""
    u = await run(sessions, orgs["org"])
    from app.services.actors import load_actor

    async with sessions() as s:
        actor = await load_actor(s, u.id)
    assert actor is not None
    assert actor.organization_id == orgs["org"]
    assert "admin" in actor.roles
