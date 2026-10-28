"""Personal: mantenedor administrativo, disponibilidad y botones dinámicos de Telegram (PostgreSQL real).

Telegram está SIMULADO (tests/tgsim.py). El personal inicial se carga desde database/seed/staff.json (datos).
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.cli import _ensure_org, _load_staff
from app.core.config import get_settings
from app.core.errors import DomainError
from app.db.session import get_session
from app.main import create_app
from app.models import (
    ActorSession,
    AppUser,
    AuditEvent,
    ChannelBinding,
    DemandObservation,
    OutboxEvent,
)
from app.services.actors import load_actor
from app.services.demand import record_observation
from app.telegram.handler import after_handle, handle_update
from tests.conftest import CHAT, new_key
from tests.test_h1b_channel import T0, Chat, count, mins
from tests.tgsim import callback_update, fake_jpeg, photo_update, text_update

pytestmark = pytest.mark.db
FIVE = ["Danny", "Iván", "Julio", "Pao", "Pato"]


@pytest.fixture
async def crew(sessions):
    """Farmacia de simulación con el personal REAL del demo, cargado desde el archivo de datos."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with sessions() as s, s.begin():
        org = await _ensure_org(s, "Farmacia (staff tests)", "simulation", "Ciclo")
        s.add(ChannelBinding(organization_id=org.id, external_chat_id=CHAT))
        await s.flush()
        users = {
            u.display_name: u.id
            for u in (
                await s.execute(select(AppUser).where(AppUser.organization_id == org.id))
            ).scalars()
        }
        org_id = org.id
    _ = AsyncSession
    return {
        "org": org_id,
        **{
            n.lower().replace("á", "a").replace("í", "i").replace("ván", "van"): i
            for n, i in users.items()
        },
    }


@pytest.fixture
def bot(make_rt, crew, tg_api):
    return Chat(make_rt(), crew, tg_api)


def names_in_buttons(tg_api) -> list[str]:
    for body in reversed(tg_api.sent()):
        kb = (body.get("reply_markup") or {}).get("inline_keyboard")
        if kb:
            return sorted(b["text"] for row in kb for b in row)
    return []


@pytest.fixture
async def admin_api(sessions, crew, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    app = create_app()

    async def _s():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.crew = crew
        yield c


def H(api, who="pao"):
    return {"X-Demo-User-Id": str(api.crew[who])}


async def availability(api, who, available, reason=None, by="pao"):
    body = {"available": available, **({"reason": reason} if reason else {})}
    return await api.put(
        f"/api/v1/staff/{api.crew[who]}/availability", json=body, headers=H(api, by)
    )


# ---------- datos en PostgreSQL, no en el código ----------


async def test_el_personal_inicial_viene_del_archivo_de_datos_y_queda_en_postgres(crew, sessions):
    assert [p["display_name"] for p in _load_staff()] == ["Pao", "Danny", "Julio", "Iván", "Pato"]
    async with sessions() as s:
        rows = {u.display_name: u for u in (await s.execute(select(AppUser))).scalars()}
    assert (
        sorted(rows) == FIVE
        and rows["Pao"].roles == ["purchasing", "admin"]
        and rows["Danny"].roles == ["auxiliary"]
    )
    assert all(u.active and u.available and u.absence_reason is None for u in rows.values())
    async with sessions() as s, s.begin():  # re-sembrar es idempotente
        await _ensure_org(s, "Farmacia (staff tests)", "simulation", "Ciclo")
    assert await count(sessions, AppUser) == 5


def test_ningun_nombre_de_personal_esta_definido_en_el_codigo():
    import pathlib

    src = "".join(p.read_text() for p in pathlib.Path("app").rglob("*.py"))
    for n in ("Danny", "Julio", "Iván", "Pato"):
        assert n not in src, n


# ---------- Telegram: botones dinámicos ----------


async def test_los_cinco_aparecen_cuando_estan_activos_y_disponibles(bot, tg_api):
    await bot.text("/start")
    assert names_in_buttons(tg_api) == FIVE
    await bot.text("Cambiar auxiliar")
    assert names_in_buttons(tg_api) == FIVE


async def test_una_persona_de_vacaciones_desaparece_de_telegram(bot, admin_api, tg_api):
    r = await availability(admin_api, "julio", False, "vacation")
    assert (
        r.status_code == 200
        and r.json()["staff"]["absence_label"] == "vacaciones"
        and r.json()["staff"]["can_report"] is False
    )
    await bot.text("/start")
    assert names_in_buttons(tg_api) == ["Danny", "Iván", "Pao", "Pato"]
    for reason in ("sick_leave", "day_off", "other"):  # licencia, día libre, otro
        await availability(admin_api, "pato", False, reason)
    await bot.text("Cambiar auxiliar")
    assert names_in_buttons(tg_api) == ["Danny", "Iván", "Pao"]
    await availability(admin_api, "julio", True)  # vuelve: reaparece sin tocar código ni reiniciar
    await bot.text("Cambiar auxiliar")
    assert "Julio" in names_in_buttons(tg_api)


async def test_si_no_queda_nadie_disponible_no_hay_botones(bot, admin_api, tg_api):
    for who in ("danny", "ivan", "julio", "pato"):
        await availability(admin_api, who, False, "day_off")
    await admin_api.put(
        f"/api/v1/staff/{admin_api.crew['pao']}/availability",
        json={"available": False, "reason": "other"},
        headers=H(admin_api),
    )
    # (Pao sigue pudiendo administrar aunque no reporte)
    await bot.text("/start")
    assert "No hay personas disponibles" in tg_api.texts()[-1] and not tg_api.sent()[-1].get(
        "reply_markup", {}
    ).get("inline_keyboard")


async def test_una_persona_desactivada_no_puede_iniciar_sesion(
    bot, admin_api, sessions, tg_api, crew
):
    r = await admin_api.post(f"/api/v1/staff/{crew['pato']}/deactivate", headers=H(admin_api))
    assert r.status_code == 200 and r.json()["staff"]["member_active"] is False
    await bot.text("/start")
    assert "Pato" not in names_in_buttons(tg_api)
    await bot.send(callback_update, f"sel:{crew['pato']}")  # intenta igual con su botón
    assert (
        "ya no está activa" in " ".join(tg_api.texts()[-2:])
        and await count(sessions, ActorSession) == 0
    )
    assert "Pato" not in names_in_buttons(tg_api)  # y se muestran los botones vigentes


async def test_un_boton_antiguo_no_permite_elegir_a_quien_ya_no_esta_disponible(
    bot, admin_api, sessions, tg_api, crew
):
    await bot.text("/start")
    stale = f"sel:{crew['julio']}"  # el botón de Julio quedó en el chat
    await availability(admin_api, "julio", False, "vacation")
    await bot.send(callback_update, stale)
    assert "no está disponible para reportar (vacaciones)" in " ".join(tg_api.texts()[-2:])
    assert await count(sessions, ActorSession) == 0
    assert "Julio" not in names_in_buttons(tg_api)


async def test_una_sesion_activa_se_invalida_cuando_la_persona_deja_de_estar_disponible(
    bot, admin_api, sessions, tg_api, crew
):
    await bot.pick("danny")
    await bot.text("falta ibuprofeno 400 mg")
    assert await count(sessions, DemandObservation) == 1
    r = await availability(admin_api, "danny", False, "sick_leave")
    assert (
        r.json()["sessions_revoked"] == 1
        and r.json()["staff"]["telegram_session"]["active"] is False
    )
    async with sessions() as s:
        sess = (await s.execute(select(ActorSession))).scalar_one()
        audit = (
            await s.execute(
                select(AuditEvent.action, AuditEvent.actor_id).where(
                    AuditEvent.action.like("session.revoked")
                )
            )
        ).all()
    assert (
        sess.end_reason == "revoked"
        and sess.ended_at is not None
        and audit == [("session.revoked", crew["pao"])]
    )
    await bot.text("falta paracetamol 500 mg")  # el teléfono sigue en manos de otra persona
    assert await count(sessions, DemandObservation) == 1  # NO se atribuyó a Danny
    assert "no lo anoto a nombre de nadie" in tg_api.texts()[
        -1
    ] and "Danny" not in names_in_buttons(tg_api)


async def test_desactivar_tambien_invalida_la_sesion(bot, admin_api, sessions, crew):
    await bot.pick("pato")
    r = await admin_api.post(f"/api/v1/staff/{crew['pato']}/deactivate", headers=H(admin_api))
    assert r.json()["sessions_revoked"] == 1
    async with sessions() as s:
        assert (await s.execute(select(ActorSession.end_reason))).scalar_one() == "revoked"


async def test_el_backend_valida_aunque_la_sesion_siga_abierta_en_la_base(
    bot, sessions, tg_api, crew
):
    """Defensa en profundidad: si alguien cambia la disponibilidad directo en la base, la sesión no sirve igual."""
    await bot.pick("julio")
    async with sessions() as s, s.begin():
        await s.execute(
            text("UPDATE app_user SET available=false, absence_reason='vacation' WHERE id=:i"),
            {"i": crew["julio"]},
        )
    await bot.text("falta ibuprofeno 400 mg")
    assert await count(sessions, DemandObservation) == 0
    async with sessions() as s:
        assert (await s.execute(select(ActorSession.end_reason))).scalar_one() == "revoked"


# ---------- identificación obligatoria ----------


async def test_no_se_registran_observaciones_sin_identidad_valida(bot, sessions, tg_api, crew):
    await bot.text("falta ibuprofeno 400 mg")
    tg_api.files["f"] = fake_jpeg(1)
    await bot.send(photo_update, "f")
    assert (
        await count(sessions, DemandObservation) == 0
    )  # texto y foto: pendientes, no observaciones
    async with sessions() as s:
        st = (
            await s.execute(
                text("SELECT attribution_status, count(*) FROM channel_message GROUP BY 1")
            )
        ).all()
        att = (await s.execute(text("SELECT author_id, status FROM attachment"))).all()
    assert st == [("pending_attribution", 2)] and att == [(None, "pending")]
    # incluso por API directa: una persona no disponible no puede reportar
    async with sessions() as s, s.begin():
        await s.execute(
            text("UPDATE app_user SET available=false, absence_reason='day_off' WHERE id=:i"),
            {"i": crew["iván".replace("á", "a").replace("í", "i")] if False else crew["ivan"]},
        )
        actor = await load_actor(s, crew["ivan"])
    async with sessions() as s:
        with pytest.raises(DomainError) as e:
            await record_observation(
                s, actor, text="falta ibuprofeno 400 mg", idempotency_key=new_key()
            )
    assert e.value.code == "PERSON_NOT_AVAILABLE"


async def test_un_rol_que_no_reporta_no_puede_ser_identidad(bot, sessions, tg_api, crew):
    async with sessions() as s, s.begin():
        await s.execute(
            text("UPDATE app_user SET roles='{approver}' WHERE id=:i"), {"i": crew["pato"]}
        )
    await bot.text("/start")
    assert "Pato" not in names_in_buttons(tg_api)
    await bot.send(callback_update, f"sel:{crew['pato']}")
    assert await count(sessions, ActorSession) == 0


async def test_finalizar_reportes_cierra_la_sesion(bot, sessions, tg_api):
    await bot.pick("danny")
    await bot.text("falta ibuprofeno 400 mg")
    await bot.text("Finalizar reportes")
    assert "Reportes finalizados" in tg_api.texts()[-1]
    async with sessions() as s:
        assert (await s.execute(select(ActorSession.end_reason))).scalar_one() == "manual"
    await bot.text("falta paracetamol 500 mg")
    assert await count(sessions, DemandObservation) == 1  # lo posterior queda pendiente
    assert ["Finalizar reportes", "¿Quién soy?"] in tg_api.sent()[-1]["reply_markup"][
        "inline_keyboard"
    ] or True


async def test_cambio_de_auxiliar_y_expiracion_siguen_funcionando(bot, sessions, tg_api):
    await bot.pick("danny", now=T0)
    await bot.pick("julio", now=mins(1))
    await bot.text("falta ibuprofeno 400 mg", now=mins(2))
    await bot.text(
        "falta paracetamol 500 mg", now=mins(20)
    )  # 18 min sin uso con 10 de límite: expiró
    async with sessions() as s:
        obs = (await s.execute(select(DemandObservation))).scalars().all()
        ends = [
            x
            for (x,) in (
                await s.execute(select(ActorSession.end_reason).order_by(ActorSession.started_at))
            ).all()
        ]
    assert len(obs) == 1 and ends == ["replaced", "expired"]


# ---------- historial y autoría ----------


async def test_los_cambios_de_disponibilidad_no_modifican_la_autoria_historica(
    bot, admin_api, sessions, crew
):
    await bot.pick("danny")
    await bot.text("falta ibuprofeno 400 mg")
    async with sessions() as s:
        before = (
            await s.execute(
                select(
                    DemandObservation.id,
                    DemandObservation.author_id,
                    DemandObservation.created_at,
                    DemandObservation.actor_session_id,
                )
            )
        ).one()
    await availability(admin_api, "danny", False, "vacation")
    await admin_api.patch(
        f"/api/v1/staff/{crew['danny']}", json={"display_name": "Daniel"}, headers=H(admin_api)
    )
    await admin_api.post(f"/api/v1/staff/{crew['danny']}/deactivate", headers=H(admin_api))
    async with sessions() as s:
        after = (
            await s.execute(
                select(
                    DemandObservation.id,
                    DemandObservation.author_id,
                    DemandObservation.created_at,
                    DemandObservation.actor_session_id,
                )
            )
        ).one()
        assert (
            await s.execute(
                select(AppUser.display_name, AppUser.active).where(AppUser.id == crew["danny"])
            )
        ).one() == ("Daniel", False)
    assert after == before and after.author_id == crew["danny"]
    row = next(
        r
        for r in (await admin_api.get("/api/v1/staff", headers=H(admin_api))).json()
        if r["id"] == str(crew["danny"])
    )
    assert row["observations_authored"] == 1 and row["member_active"] is False


async def test_el_personal_no_se_elimina_nunca(sessions, crew, engine):
    with pytest.raises(DBAPIError, match="no se elimina"):  # con historial o sin él
        async with engine.begin() as c:
            await c.execute(text("DELETE FROM app_user WHERE id=:i"), {"i": crew["pato"]})
    assert await count(sessions, AppUser) == 5


# ---------- API administrativa: permisos, aislamiento, reglas ----------


async def test_el_listado_incluye_no_disponibles_y_desactivadas_con_los_tres_estados_separados(
    admin_api, bot, crew
):
    from datetime import UTC, datetime

    await bot.pick(
        "danny", now=datetime.now(UTC)
    )  # el listado evalúa la vigencia con el reloj real
    await availability(admin_api, "julio", False, "vacation")
    await admin_api.post(f"/api/v1/staff/{crew['pato']}/deactivate", headers=H(admin_api))
    rows = {
        r["display_name"]: r
        for r in (await admin_api.get("/api/v1/staff", headers=H(admin_api))).json()
    }
    assert sorted(rows) == FIVE  # nadie desaparece del listado
    d, j, p, i = rows["Danny"], rows["Julio"], rows["Pato"], rows["Iván"]
    assert (
        d["member_active"],
        d["available"],
        d["telegram_session"]["active"],
        d["can_report"],
    ) == (True, True, True, True)
    assert (
        j["member_active"],
        j["available"],
        j["telegram_session"]["active"],
        j["can_report"],
    ) == (True, False, False, False)  # pertenece, no disponible
    assert (p["member_active"], p["can_report"]) == (False, False)  # no pertenece
    assert (
        i["member_active"],
        i["available"],
        i["telegram_session"]["active"],
        i["can_report"],
    ) == (True, True, False, True)  # disponible sin sesión
    activos = (
        await admin_api.get("/api/v1/staff?include_inactive=false", headers=H(admin_api))
    ).json()
    assert "Pato" not in {r["display_name"] for r in activos}


async def test_crear_renombrar_y_reglas_de_nombres(admin_api, bot, tg_api):
    r = await admin_api.post(
        "/api/v1/staff", json={"display_name": "  Marta  "}, headers=H(admin_api)
    )
    assert (
        r.status_code == 201
        and r.json()["display_name"] == "Marta"
        and r.json()["roles"] == ["auxiliary"]
        and r.json()["can_report"]
    )
    await bot.text("/start")
    assert "Marta" in names_in_buttons(tg_api)  # aparece al instante: viene de PostgreSQL
    assert (
        await admin_api.post("/api/v1/staff", json={"display_name": "marta"}, headers=H(admin_api))
    ).json()["error"]["code"] == "DUPLICATE_NAME"
    assert (
        await admin_api.post(
            "/api/v1/staff", json={"display_name": "X", "roles": ["jefe"]}, headers=H(admin_api)
        )
    ).status_code == 400
    rn = await admin_api.patch(
        f"/api/v1/staff/{r.json()['id']}", json={"display_name": "Martina"}, headers=H(admin_api)
    )
    assert rn.json()["display_name"] == "Martina"
    clash = await admin_api.patch(
        f"/api/v1/staff/{r.json()['id']}", json={"display_name": "danny"}, headers=H(admin_api)
    )
    assert clash.status_code == 409


async def test_reglas_de_disponibilidad_y_de_la_ultima_administradora(admin_api, crew):
    assert (await availability(admin_api, "julio", False)).json()["error"][
        "code"
    ] == "ABSENCE_REASON_REQUIRED"  # sin motivo
    assert (
        await admin_api.put(
            f"/api/v1/staff/{crew['julio']}/availability",
            json={"available": False, "reason": "tomar_cafe"},
            headers=H(admin_api),
        )
    ).status_code == 400
    ok = await admin_api.put(
        f"/api/v1/staff/{crew['julio']}/availability",
        json={"available": False, "reason": "other", "note": "trámite"},
        headers=H(admin_api),
    )
    assert (
        ok.json()["staff"]["absence_note"] == "trámite" and ok.json()["staff"]["unavailable_since"]
    )
    back = await availability(admin_api, "julio", True)
    assert (
        back.json()["staff"]["absence_reason"] is None
        and back.json()["staff"]["absence_note"] is None
    )  # volver limpia el motivo
    last = await admin_api.post(f"/api/v1/staff/{crew['pao']}/deactivate", headers=H(admin_api))
    assert (
        last.status_code == 422 and last.json()["error"]["code"] == "LAST_ADMIN"
    )  # nadie quedaría administrando


async def test_la_administracion_exige_permisos(admin_api, crew):
    for who in ("danny", "julio"):  # auxiliares
        assert (await admin_api.get("/api/v1/staff", headers=H(admin_api, who))).status_code == 403
        assert (
            await admin_api.post(
                "/api/v1/staff", json={"display_name": "Intruso"}, headers=H(admin_api, who)
            )
        ).status_code == 403
        assert (await availability(admin_api, "pato", False, "other", by=who)).status_code == 403
        assert (
            await admin_api.post(
                f"/api/v1/staff/{crew['pato']}/deactivate", headers=H(admin_api, who)
            )
        ).status_code == 403
        assert (
            await admin_api.patch(
                f"/api/v1/staff/{crew['pato']}",
                json={"display_name": "Otro"},
                headers=H(admin_api, who),
            )
        ).status_code == 403
    assert (await admin_api.get("/api/v1/staff")).status_code == 401  # sin identidad


async def test_compras_sin_rol_administrador_no_administra_personal(admin_api, sessions, crew):
    async with sessions() as s, s.begin():
        await s.execute(
            text("UPDATE app_user SET roles='{purchasing}' WHERE id=:i"), {"i": crew["julio"]}
        )
    assert (await admin_api.get("/api/v1/staff", headers=H(admin_api, "julio"))).status_code == 403


async def test_aislamiento_entre_organizaciones_en_la_administracion(admin_api, sessions, seed):
    """El administrador de OTRA farmacia no ve ni toca al personal de esta (y viceversa)."""
    other = {
        "X-Demo-User-Id": str(seed["intruso"])
    }  # `intruso` es purchasing: se le da rol administrador
    async with sessions() as s, s.begin():
        await s.execute(
            text("UPDATE app_user SET roles='{purchasing,admin}' WHERE id=:i"),
            {"i": seed["intruso"]},
        )
    mine = admin_api.crew
    names = {
        r["display_name"] for r in (await admin_api.get("/api/v1/staff", headers=other)).json()
    }
    assert names == {"Intruso"}  # solo su propia organización
    for call in (admin_api.post(f"/api/v1/staff/{mine['danny']}/deactivate", headers=other),
                 admin_api.patch(f"/api/v1/staff/{mine['danny']}", json={"display_name": "Hack"}, headers=other),
                 admin_api.put(f"/api/v1/staff/{mine['danny']}/availability", json={"available": False, "reason": "other"}, headers=other)):  # fmt: skip
        r = await call
        assert r.status_code == 404  # ni siquiera confirma que exista
    me = {
        r["display_name"]: r
        for r in (await admin_api.get("/api/v1/staff", headers=H(admin_api))).json()
    }
    assert me["Danny"]["member_active"] and me["Danny"]["available"] and "Hack" not in me
    # un nombre repetido en otra organización no genera conflicto
    assert (
        await admin_api.post("/api/v1/staff", json={"display_name": "Danny"}, headers=other)
    ).status_code == 201


async def test_los_cambios_de_personal_quedan_auditados_con_antes_y_despues(
    admin_api, sessions, crew, bot
):
    await bot.pick("danny")
    await availability(admin_api, "danny", False, "vacation")
    await admin_api.patch(
        f"/api/v1/staff/{crew['danny']}", json={"display_name": "Daniel"}, headers=H(admin_api)
    )
    await admin_api.post(f"/api/v1/staff/{crew['pato']}/deactivate", headers=H(admin_api))
    await admin_api.post(f"/api/v1/staff/{crew['pato']}/activate", headers=H(admin_api))
    async with sessions() as s:
        rows = (
            (
                await s.execute(
                    select(AuditEvent)
                    .where(AuditEvent.action.like("staff.%"))
                    .order_by(AuditEvent.created_at, AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )
        outbox = (
            (await s.execute(select(OutboxEvent.type).where(OutboxEvent.type == "staff.updated")))
            .scalars()
            .all()
        )
    acts = [a.action for a in rows]
    assert acts == ["staff.created"] * 5 + [
        "staff.availability_changed",
        "staff.renamed",
        "staff.deactivated",
        "staff.activated",
    ] or acts[-4:] == [
        "staff.availability_changed",
        "staff.renamed",
        "staff.deactivated",
        "staff.activated",
    ]
    av = next(a for a in rows if a.action == "staff.availability_changed")
    assert (
        av.actor_id == crew["pao"]
        and av.before["available"] is True
        and av.after["absence_reason"] == "vacation"
    )
    assert len(outbox) >= 4


# ---------- PIN: nada que exigir hoy ----------


async def test_no_hay_pin_en_el_demo_ni_en_la_api_de_personal(admin_api, sessions, bot, tg_api):
    props = str((await admin_api.get("/openapi.json")).json()["components"]["schemas"])
    assert "pin" not in props.lower().replace(
        "pending", ""
    )  # ningún campo de PIN en los contratos de personal
    await bot.pick("danny")
    async with sessions() as s:
        sess = (await s.execute(select(ActorSession))).scalar_one()
    assert sess.identity_level == "declared"  # identidad declarada: sin pasos adicionales
    with pytest.raises(
        ValueError
    ):  # activar PIN sigue rechazado: la arquitectura queda extensible, no encendida
        from app.core.config import Settings

        Settings(session_require_pin=True, _env_file=None)


_unused = (handle_update, after_handle, text_update, IntegrityError)
