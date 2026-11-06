"""Solicitud de acceso desde Telegram y aprobación/bloqueo desde la API (backend).

Telegram está SIMULADO (tests/tgsim.py): no se probó contra los servidores reales de Telegram.
Los JWT se prueban con claves generadas en memoria; no hay proyecto Supabase real.
Todo ocurre en organizaciones `simulation` (chats 555000x) o `real_test` con datos de prueba.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from alembic import command
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from app.api import deps as deps_module
from app.api.deps import get_session
from app.core import supabase_auth
from app.core.config import Settings, get_settings
from app.core.errors import DomainError
from app.core.invitations import code_hash, generate_code, looks_like_code, normalize_code
from app.main import create_app
from app.models import (
    ActorSession,
    AppUser,
    Attachment,
    AuditEvent,
    ChannelBinding,
    ChannelMessage,
    DemandObservation,
    InboundEvent,
    Organization,
    OutboundMessage,
    OutboxEvent,
    PurchaseCycle,
    TelegramAccessRequest,
    TelegramAccount,
    TelegramAccountAuxiliary,
)
from app.services import access
from app.services.actors import Actor
from app.telegram.handler import after_handle, handle_update
from tests.conftest import alembic_config
from tests.tgsim import callback_update, photo_update, text_update

pytestmark = pytest.mark.db
T0 = datetime.now(UTC).replace(second=0, microsecond=0)
mins = lambda n: T0 + timedelta(minutes=n)  # noqa: E731


@pytest.fixture
async def farm(sessions):
    """Organización de simulación con una administradora, dos auxiliares y su ciclo."""
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia accesos", kind="simulation")
        s.add(org)
        await s.flush()
        ids["org"] = org.id
        for key, name, roles in [
            ("admin", "Administradora", ["admin", "purchasing"]),
            ("ana", "Ana", ["auxiliary"]),
            ("luis", "Luis", ["auxiliary"]),
        ]:
            u = AppUser(organization_id=org.id, display_name=name, roles=roles)
            s.add(u)
            await s.flush()
            ids[key] = u.id
        s.add(PurchaseCycle(organization_id=org.id, name="Ciclo accesos"))
    return ids


@pytest.fixture
async def real_farm(sessions):
    """Organización `real_test` (nunca la de producción) con Pao como administradora."""
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia real de prueba", kind="real_test")
        s.add(org)
        await s.flush()
        ids["org"] = org.id
        for key, name, roles, subject in [
            ("pao", "Pao", ["admin", "purchasing"], "sub-pao"),
            ("ana", "Auxiliar real", ["auxiliary"], None),
        ]:
            u = AppUser(
                organization_id=org.id, display_name=name, roles=roles, auth_subject=subject
            )
            s.add(u)
            await s.flush()
            ids[key] = u.id
        s.add(PurchaseCycle(organization_id=org.id, name="Ciclo real de prueba"))
    return ids


def actor(farm, who="admin", roles=("admin",)) -> Actor:
    return Actor(farm[who], farm["org"], "Administradora", list(roles))


async def count(sessions, model) -> int:
    async with sessions() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def accounts(sessions):
    async with sessions() as s:
        return (await s.execute(select(TelegramAccount))).scalars().all()


async def requests(sessions):
    async with sessions() as s:
        return (await s.execute(select(TelegramAccessRequest))).scalars().all()


async def observations(sessions):
    async with sessions() as s:
        return (
            (await s.execute(select(DemandObservation).order_by(DemandObservation.created_at)))
            .scalars()
            .all()
        )


async def issue(
    sessions,
    farm,
    *,
    user_id="55500030",
    chat_id=None,
    update_id="1",
    now=T0,
    chat_kind="private",
):
    async with sessions() as s, s.begin():
        return await access.request_access(
            s,
            organization_id=farm["org"],
            provider="telegram",
            external_user_id=user_id,
            external_chat_id=chat_id or user_id,
            chat_kind=chat_kind,
            update_id=update_id,
            now=now,
        )


async def lookup(sessions, farm, code, *, who="admin", now=T0):
    async with sessions() as s, s.begin():
        return await access.lookup_request(s, actor(farm, who), code=code, now=now)


async def approve(
    sessions,
    farm,
    request_id,
    *,
    mode="personal",
    person="ana",
    label="Teléfono de Ana",
    role="operator",
    who="admin",
    now=T0,
    branch_label=None,
):
    async with sessions() as s, s.begin():
        return await access.approve_request(
            s, actor(farm, who), request_id, display_label=label, mode=mode,
            app_user_id=farm[person] if person else None, access_role=role, now=now,
            branch_label=branch_label,
        )  # fmt: skip


async def block(sessions, farm, account_id, *, who="admin", reason="prueba", now=None):
    async with sessions() as s, s.begin():
        return await access.block_account(
            s, actor(farm, who), account_id, reason=reason, now=now or mins(10)
        )


async def reactivate(sessions, farm, account_id, *, who="admin"):
    async with sessions() as s, s.begin():
        return await access.reactivate_account(
            s, actor(farm, who), account_id, reason="rehabilitación", now=mins(11)
        )


def code_of(text_: str) -> str:
    return next(line for line in text_.splitlines() if looks_like_code(line))


class UserChat:
    """Una cuenta de Telegram con chat privado (chat id = id de usuario por defecto)."""

    _next = 500

    def __init__(self, rt, api, user_id: int, *, chat_id: str | None = None, from_id: int | None = None):
        UserChat._next += 1
        self.rt, self.api, self.user_id, self.uid = rt, api, user_id, UserChat._next * 100
        self.chat_id = chat_id or str(user_id)
        self.from_id = from_id if from_id is not None else user_id

    async def _run(self, update, now=T0, deliver=True):
        res = await handle_update(self.rt, update, now=now)
        if deliver:
            await after_handle(self.rt, res)
        return res

    def text(self, t, now=T0):
        self.uid += 1
        return self._run(
            text_update(self.uid, self.chat_id, t, ts=int(now.timestamp()), from_id=self.from_id),
            now=now,
        )

    def click(self, data, now=T0):
        self.uid += 1
        return self._run(
            callback_update(self.uid, self.chat_id, data, from_id=self.from_id), now=now
        )


@pytest.fixture
def rt(make_rt, farm):
    return make_rt(access_org=farm["org"])


@pytest.fixture
def user(rt, tg_api):
    def build(user_id: int, **kw):
        return UserChat(rt, tg_api, user_id, **kw)

    return build


# ---------------------------------------------------------------- bot: solicitud


async def test_solicitar_registra_pendiente_y_entrega_codigo_sin_persistirlo(
    farm, sessions, user, tg_api
):
    c = user(55500030)
    res = await c.text("/solicitar")
    assert res.status == "replied"
    message = tg_api.texts()[-1]
    assert "Solicitud registrada" in message and "30 minutos" in message
    code = code_of(message)

    (req,) = await requests(sessions)
    assert req.status == "pending" and req.external_user_id == "55500030"
    assert req.code_hash == code_hash(code) and code not in req.code_hash
    assert req.expires_at - req.created_at == timedelta(minutes=30)
    assert await count(sessions, TelegramAccount) == 0
    assert await count(sessions, InboundEvent) == 0
    assert await count(sessions, OutboundMessage) == 0  # el código no entró a la cola

    async with sessions() as s:
        stored = json.dumps(
            [
                *(await s.execute(select(AuditEvent.after))).scalars(),
                *(await s.execute(select(OutboxEvent.payload))).scalars(),
            ],
            default=str,
            ensure_ascii=False,
        )
    assert normalize_code(code) not in normalize_code(stored)


async def test_una_solicitud_pendiente_no_concede_ninguna_funcion_operativa(
    farm, sessions, user, tg_api
):
    c = user(55500031)
    await c.text("/solicitar")
    res = await c.text("falta ibuprofeno 400")
    assert res.status == "unauthorized"
    assert "/solicitar" in tg_api.texts()[-1]
    assert await count(sessions, InboundEvent) == 0
    assert await count(sessions, ChannelMessage) == 0
    assert await count(sessions, DemandObservation) == 0
    assert await count(sessions, TelegramAccount) == 0


async def test_start_solo_indica_como_solicitar(farm, sessions, user, tg_api):
    await user(55500032).text("/start")
    assert "/solicitar" in tg_api.texts()[-1]
    assert await count(sessions, TelegramAccessRequest) == 0


async def test_una_sola_solicitud_pendiente_y_el_codigo_nuevo_rota_el_anterior(
    farm, sessions, user, tg_api
):
    c = user(55500033)
    await c.text("/solicitar")
    first = code_of(tg_api.texts()[-1])
    await c.text("/solicitar")
    second = code_of(tg_api.texts()[-1])
    assert first != second
    (req,) = await requests(sessions)  # la misma fila pendiente, no dos solicitudes
    assert req.code_hash == code_hash(second)
    assert (await lookup(sessions, farm, first)).error is not None
    assert (await lookup(sessions, farm, second)).request is not None


async def test_reintento_del_mismo_update_no_genera_otra_solicitud(farm, sessions):
    first = await issue(sessions, farm, update_id="99")
    second = await issue(sessions, farm, update_id="99")
    assert second.code is None and second.request.id == first.request.id
    assert await count(sessions, TelegramAccessRequest) == 1


async def test_sin_organizacion_configurada_el_bot_lo_dice(make_rt, tg_api):
    rt = make_rt()  # sin access_org_id
    c = UserChat(rt, tg_api, 55500034)
    await c.text("/solicitar")
    assert "no están configuradas" in tg_api.texts()[-1]
    assert await count(rt.sessionmaker, TelegramAccessRequest) == 0


async def test_un_grupo_no_puede_solicitar_acceso(farm, sessions, rt, tg_api):
    c = UserChat(rt, tg_api, 55500035)
    c.uid += 1
    res = await handle_update(
        rt,
        text_update(c.uid, "-10055500035", "/solicitar", chat_kind="group", from_id=55500035),
        now=T0,
    )
    await after_handle(rt, res)
    assert "chat privado" in tg_api.texts()[-1]
    assert await count(sessions, TelegramAccessRequest) == 0


async def test_un_codigo_escrito_en_el_chat_no_se_persiste(farm, sessions, user, tg_api):
    c = user(55500036)
    code = generate_code()
    res = await c.text(code)
    assert res.status == "replied"
    assert "No lo envíes al bot" in tg_api.texts()[-1]
    assert await count(sessions, InboundEvent) == 0
    assert await count(sessions, ChannelMessage) == 0


# ---------------------------------------------------------------- códigos: vigencia e intentos


async def test_el_codigo_vence_a_los_30_minutos(farm, sessions):
    issued = await issue(sessions, farm)
    result = await lookup(sessions, farm, issued.code, now=T0 + timedelta(minutes=30, seconds=1))
    assert result.error is not None and result.error.code == "INVALID_ACCESS_CODE"
    (req,) = await requests(sessions)
    assert req.status == "expired"
    assert await count(sessions, TelegramAccount) == 0


async def test_codigo_invalido_no_revela_si_existio(farm, sessions):
    result = await lookup(sessions, farm, generate_code())
    assert result.error is not None and result.error.code == "INVALID_ACCESS_CODE"
    assert "no es válido" in result.error.message


async def test_intentos_de_codigo_limitados_por_administrador(farm, sessions):
    for i in range(access.MAX_ADMIN_ATTEMPTS):
        result = await lookup(sessions, farm, generate_code(), now=T0 + timedelta(seconds=i))
        assert result.error is not None
    result = await lookup(sessions, farm, generate_code(), now=T0 + timedelta(minutes=1))
    assert result.error is not None and result.error.code == "TOO_MANY_ATTEMPTS"


async def test_lookup_y_aprobacion_exigen_administrador(farm, sessions):
    issued = await issue(sessions, farm)
    async with sessions() as s, s.begin():
        with pytest.raises(DomainError) as e:
            await access.lookup_request(s, actor(farm, "ana", ("auxiliary",)), code=issued.code)
    assert e.value.code == "FORBIDDEN"
    async with sessions() as s, s.begin():
        with pytest.raises(DomainError) as e:
            await access.approve_request(
                s, actor(farm, "ana", ("auxiliary",)), issued.request.id,
                display_label="x", mode="personal", app_user_id=farm["ana"], now=T0,
            )  # fmt: skip
    assert e.value.code == "FORBIDDEN"
    assert await count(sessions, TelegramAccount) == 0


# ---------------------------------------------------------------- aprobación


async def test_aprobar_personal_vincula_y_habilita_la_cuenta(farm, sessions, user, tg_api):
    c = user(55500040)
    await c.text("/solicitar")
    code = code_of(tg_api.texts()[-1])
    result = await lookup(sessions, farm, code)
    assert result.request is not None
    approval = await approve(sessions, farm, result.request.id)

    assert approval.account.mode == "personal"
    assert approval.account.app_user_id == farm["ana"]
    assert approval.account.status == "active" and approval.account.origin == "access_request"
    assert approval.binding.mode == "personal" and approval.binding.account_id == approval.account.id
    (req,) = await requests(sessions)
    assert req.status == "approved" and req.approved_by == farm["admin"]
    assert req.approved_account_id == approval.account.id

    await c.text("falta ibuprofeno 400", now=mins(1))
    (o,) = await observations(sessions)
    assert o.author_id == farm["ana"] and o.identity_level == "account"
    assert o.account_id == approval.account.id

    # La confirmación va por la cola del bot y no contiene el código.
    async with sessions() as s:
        out = (await s.execute(select(OutboundMessage))).scalars().all()
    assert any("autorizado" in m.payload["text"] for m in out)
    assert all(normalize_code(code) not in normalize_code(m.payload["text"]) for m in out)


async def test_aprobar_telefono_compartido_no_liga_persona_y_mantiene_selector(
    farm, sessions, user, tg_api
):
    c = user(55500041)
    await c.text("/solicitar")
    code = code_of(tg_api.texts()[-1])
    req = (await lookup(sessions, farm, code)).request
    approval = await approve(
        sessions, farm, req.id, mode="shared_device", person=None, label="Teléfono farmacia"
    )
    assert approval.account.mode == "shared_device" and approval.account.app_user_id is None
    async with sessions() as s, s.begin():
        await access.add_account_auxiliary(s, actor(farm), approval.account.id, farm["ana"])
        await access.add_account_auxiliary(s, actor(farm), approval.account.id, farm["luis"])

    await c.text("/start", now=mins(1))
    await c.click(f"sel:{farm['luis']}", now=mins(2))
    assert "Reportando como Luis" in tg_api.texts()[-1]
    await c.text("falta paracetamol 500", now=mins(3))
    (o,) = await observations(sessions)
    assert o.author_id == farm["luis"] and o.identity_level == "declared"
    assert o.account_id == approval.account.id


async def test_aprobacion_avisa_automatico_si_hay_un_solo_auxiliar(farm, sessions, user, tg_api):
    """D-146, punto 12: con un auxiliar no se pide «elige quién escribe»."""

    async def last_notice() -> str:
        async with sessions() as s:
            (out,) = (
                await s.execute(
                    select(OutboundMessage).order_by(OutboundMessage.id.desc()).limit(1)
                )
            ).scalars().all()
        return out.payload["text"]

    c = user(55500044)
    await c.text("/solicitar")
    req = (await lookup(sessions, farm, code_of(tg_api.texts()[-1]))).request
    acc = await approve(
        sessions, farm, req.id, mode="shared_device", person=None, label="Teléfono farmacia"
    )
    # Sin auxiliares todavía no se puede prometer identidad automática ni pedir que elija.
    without = await last_notice()
    assert "elige quién escribe" not in without and "auxiliares habilitados" in without

    async with sessions() as s, s.begin():
        await access.add_account_auxiliary(s, actor(farm), acc.account.id, farm["ana"])
    await block(sessions, farm, acc.account.id, now=mins(11))

    await c.text("/solicitar", now=mins(12))
    req2 = (await lookup(sessions, farm, code_of(tg_api.texts()[-1]), now=mins(12))).request
    await approve(
        sessions, farm, req2.id, mode="shared_device", person=None,
        label="Teléfono farmacia", now=mins(12),
    )  # fmt: skip
    one = await last_notice()
    assert "automáticamente a nombre de Ana" in one and "elige quién escribe" not in one

    async with sessions() as s, s.begin():
        await access.add_account_auxiliary(s, actor(farm), acc.account.id, farm["luis"])
    await block(sessions, farm, acc.account.id, now=mins(13))

    await c.text("/solicitar", now=mins(14))
    req3 = (await lookup(sessions, farm, code_of(tg_api.texts()[-1]), now=mins(14))).request
    await approve(
        sessions, farm, req3.id, mode="shared_device", person=None,
        label="Teléfono farmacia", now=mins(14),
    )  # fmt: skip
    assert "elige quién escribe" in await last_notice()


async def test_personal_exige_persona_y_compartido_la_prohibe(farm, sessions):
    one = await issue(sessions, farm, user_id="55500042", update_id="42")
    assert one.request.status == "pending" and one.request.expires_at > T0
    async with sessions() as s, s.begin():
        with pytest.raises(DomainError) as e:
            await access.approve_request(
                s, actor(farm), one.request.id, display_label="Personal", mode="personal",
                app_user_id=None, now=T0,
            )  # fmt: skip
    assert e.value.code == "PERSON_REQUIRED"

    two = await issue(sessions, farm, user_id="55500043", update_id="43")
    async with sessions() as s, s.begin():
        with pytest.raises(DomainError) as e:
            await access.approve_request(
                s, actor(farm), two.request.id, display_label="Compartido",
                mode="shared_device", app_user_id=farm["ana"], now=T0,
            )  # fmt: skip
    assert e.value.code == "SHARED_WITHOUT_PERSON"
    assert await count(sessions, TelegramAccount) == 0


async def test_aprobar_dos_veces_la_misma_solicitud_falla(farm, sessions):
    issued = await issue(sessions, farm)
    await approve(sessions, farm, issued.request.id)
    async with sessions() as s, s.begin():
        with pytest.raises(DomainError) as e:
            await access.approve_request(
                s, actor(farm), issued.request.id, display_label="Otra vez", mode="personal",
                app_user_id=farm["luis"], now=T0,
            )  # fmt: skip
    assert e.value.code == "REQUEST_NOT_PENDING"
    assert await count(sessions, TelegramAccount) == 1


async def test_rechazar_conserva_la_solicitud_y_no_crea_cuenta(farm, sessions):
    issued = await issue(sessions, farm)
    async with sessions() as s, s.begin():
        req = await access.reject_request(
            s, actor(farm), issued.request.id, reason="no corresponde", now=mins(1)
        )
    assert req.status == "rejected" and req.rejected_by == farm["admin"]
    assert (await lookup(sessions, farm, issued.code)).error is not None
    assert await count(sessions, TelegramAccount) == 0
    async with sessions() as s:
        actions = (await s.execute(select(AuditEvent.action))).scalars().all()
    assert "telegram_access_request.rejected" in actions


# ---------------------------------------------------------------- bloqueo y rehabilitación


async def test_bloquear_cierra_sesiones_conserva_historial_y_rehabilitar_reutiliza_la_cuenta(
    farm, sessions, user, tg_api
):
    c = user(55500044)
    await c.text("/solicitar")
    code = code_of(tg_api.texts()[-1])
    req = (await lookup(sessions, farm, code)).request
    approval = await approve(sessions, farm, req.id)
    await c.text("falta ibuprofeno 400", now=mins(1))
    (o,) = await observations(sessions)

    result = await block(sessions, farm, approval.account.id)
    assert result.bindings_deactivated == 1 and result.sessions_revoked == 1

    res = await c.text("falta paracetamol 500", now=mins(12))
    assert res.status == "unauthorized"
    assert await count(sessions, InboundEvent) == 1  # solo el primer mensaje
    assert await count(sessions, DemandObservation) == 1

    async with sessions() as s:
        acc = await s.get(TelegramAccount, approval.account.id)
        saved = await s.get(DemandObservation, o.id)
        sess = (await s.execute(select(ActorSession))).scalar_one()
        binding = await s.get(ChannelBinding, approval.binding.id)
    assert acc.status == "blocked" and acc.blocked_at is not None
    assert acc.blocked_by == farm["admin"]
    assert binding.status == "inactive"
    assert saved.account_id == approval.account.id  # el historial no se borra
    assert sess.ended_at is not None and sess.end_reason == "revoked"

    restored = await reactivate(sessions, farm, approval.account.id)
    assert restored.bindings_reactivated == 1
    await c.text("falta amoxicilina 500", now=mins(13))
    assert await count(sessions, DemandObservation) == 2
    (again,) = await accounts(sessions)
    assert again.id == approval.account.id and again.status == "active"
    async with sessions() as s:
        actions = (await s.execute(select(AuditEvent.action))).scalars().all()
    assert "telegram_account.blocked" in actions and "telegram_account.reactivated" in actions


async def test_una_cuenta_bloqueada_puede_solicitar_de_nuevo_y_se_reutiliza(
    farm, sessions, user, tg_api
):
    c = user(55500045)
    await c.text("/solicitar")
    code = code_of(tg_api.texts()[-1])
    req = (await lookup(sessions, farm, code)).request
    approval = await approve(sessions, farm, req.id)
    await block(sessions, farm, approval.account.id)

    await c.text("/solicitar", now=mins(12))
    new_code = code_of(tg_api.texts()[-1])
    req2 = (await lookup(sessions, farm, new_code, now=mins(12))).request
    assert req2.id != req.id
    restart = await approve(sessions, farm, req2.id, label="Ana de nuevo", now=mins(13))
    assert restart.account.id == approval.account.id
    assert restart.account.status == "active"
    assert await count(sessions, TelegramAccount) == 1


# ---------------------------------------------------------------- sesiones aisladas


async def test_dos_cuentas_personales_reportan_sin_mezclarse(farm, sessions, user, tg_api):
    codes = []
    for uid in (55500046, 55500047):
        c = user(uid)
        await c.text("/solicitar")
        codes.append(code_of(tg_api.texts()[-1]))
    ana, luis = user(55500046), user(55500047)
    r1 = (await lookup(sessions, farm, codes[0])).request
    r2 = (await lookup(sessions, farm, codes[1])).request
    a1 = await approve(sessions, farm, r1.id, label="Cuenta Ana")
    a2 = await approve(sessions, farm, r2.id, label="Cuenta Luis", person="luis")

    await ana.text("falta ibuprofeno 400", now=mins(1))
    await luis.text("falta paracetamol 500", now=mins(2))
    await ana.text("falta amoxicilina 500", now=mins(3))
    obs = await observations(sessions)
    assert [o.author_id for o in obs] == [farm["ana"], farm["luis"], farm["ana"]]
    assert {o.account_id for o in obs} == {a1.account.id, a2.account.id}
    async with sessions() as s:
        active = (
            await s.execute(select(ActorSession).where(ActorSession.ended_at.is_(None)))
        ).scalars().all()
    assert len(active) == 2 and {x.channel_binding_id for x in active} == {
        a1.binding.id,
        a2.binding.id,
    }


async def test_telefono_real_6358481229_conserva_acceso_y_selector(
    real_seed, sessions, tg_api, make_rt
):
    """Regresión: el teléfono de desarrollo/pruebas sigue igual tras el backfill de 0007.

    La cuenta queda `shared_device` (no personal), así que se sigue eligiendo auxiliar; nadie más
    puede usar ese chat aunque conozca su id.
    """
    from tests.conftest import REAL_CHAT

    async with sessions() as s, s.begin():
        acc = TelegramAccount(
            organization_id=real_seed["org"],
            external_user_id=REAL_CHAT,
            display_label="Teléfono compartido",
            mode="shared_device",
            origin="backfill",
        )
        s.add(acc)
        await s.flush()
        binding = await s.get(ChannelBinding, real_seed["binding"])
        binding.account_id = acc.id
        acc_id = acc.id
        admin = Actor(real_seed["pao"], real_seed["org"], "Pao", ["admin", "purchasing"])
        await access.add_account_auxiliary(s, admin, acc.id, real_seed["ana"])
        await access.add_account_auxiliary(s, admin, acc.id, real_seed["luis"])

    rt = make_rt(access_org=real_seed["org"])
    phone = UserChat(rt, tg_api, int(REAL_CHAT))
    await phone.text("/start")
    assert "¿Quién va a reportar ahora?" in tg_api.texts()[-1]
    await phone.click(f"sel:{real_seed['ana']}")
    assert "Reportando como Aux Uno" in tg_api.texts()[-1]
    await phone.text("falta ibuprofeno 400")
    (o,) = await observations(sessions)
    assert o.author_id == real_seed["ana"] and o.identity_level == "declared"
    assert o.account_id == acc_id

    other = UserChat(rt, tg_api, 999, chat_id=REAL_CHAT)  # otra cuenta en el mismo chat
    res = await other.text("falta paracetamol 500")
    assert res.status == "unauthorized"
    assert len(await observations(sessions)) == 1


async def test_telefono_compartido_legado_sigue_eligiendo_auxiliar(farm, sessions, user, tg_api):
    async with sessions() as s, s.begin():
        s.add(ChannelBinding(organization_id=farm["org"], external_chat_id="55500048"))
    shared = user(777, chat_id="55500048", from_id=777)
    await shared.text("/start")
    await shared.click(f"sel:{farm['ana']}")
    await shared.text("falta ibuprofeno 400")
    (o,) = await observations(sessions)
    assert o.author_id == farm["ana"] and o.identity_level == "declared"
    assert o.account_id is None  # vínculo legado sin cuenta


# ---------------------------------------------------------------- API administrativa


@pytest.fixture
async def admin_api(sessions, farm, monkeypatch):
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


def H(api, who="admin"):
    return {"X-Demo-User-Id": str(api.farm[who])}


async def test_api_lookup_aprueba_y_no_expone_el_codigo(admin_api, sessions, user, tg_api):
    c = user(55500049)
    await c.text("/solicitar")
    code = code_of(tg_api.texts()[-1])

    r = await admin_api.post(
        "/api/v1/telegram-access-requests/lookup", json={"code": code}, headers=H(admin_api)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["can_approve"] is True
    assert body["request"]["status"] == "pending"
    assert normalize_code(code) not in normalize_code(json.dumps(body))
    request_id = body["request"]["id"]

    r = await admin_api.post(
        f"/api/v1/telegram-access-requests/{request_id}/approve",
        json={"display_label": "Teléfono Ana", "mode": "personal",
              "app_user_id": str(admin_api.farm["ana"]), "access_role": "operator"},
        headers=H(admin_api),
    )  # fmt: skip
    assert r.status_code == 200, r.text
    account = r.json()
    assert account["status"] == "active" and account["mode"] == "personal"
    assert account["app_user_name"] == "Ana"

    listing = (await admin_api.get("/api/v1/telegram-accounts", headers=H(admin_api))).json()
    assert listing[0]["id"] == account["id"]
    assert normalize_code(code) not in normalize_code(json.dumps(listing))


async def test_api_requiere_administrador_y_sesion(admin_api, sessions):
    assert (await admin_api.get("/api/v1/telegram-accounts")).status_code == 401
    r = await admin_api.get("/api/v1/telegram-accounts", headers=H(admin_api, "ana"))
    assert r.status_code == 403
    r = await admin_api.post(
        "/api/v1/telegram-access-requests/lookup",
        json={"code": generate_code()},
        headers=H(admin_api, "ana"),
    )
    assert r.status_code == 403


async def test_api_bloquea_rehabilita_y_muestra_historial(admin_api, sessions, user, tg_api):
    c = user(55500050)
    await c.text("/solicitar")
    code = code_of(tg_api.texts()[-1])
    req = (
        await admin_api.post(
            "/api/v1/telegram-access-requests/lookup", json={"code": code}, headers=H(admin_api)
        )
    ).json()["request"]
    account = (
        await admin_api.post(
            f"/api/v1/telegram-access-requests/{req['id']}/approve",
            json={"display_label": "Cuenta Ana", "mode": "personal",
                  "app_user_id": str(admin_api.farm["ana"])},
            headers=H(admin_api),
        )
    ).json()  # fmt: skip
    await c.text("falta ibuprofeno 400", now=mins(1))

    r = await admin_api.post(
        f"/api/v1/telegram-accounts/{account['id']}/block",
        json={"reason": "cambio de teléfono"},
        headers=H(admin_api),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "blocked" and r.json()["sessions_revoked"] == 1
    blocked = (await admin_api.get("/api/v1/telegram-accounts", headers=H(admin_api))).json()[0]
    assert blocked["status"] == "blocked" and blocked["block_reason"] == "cambio de teléfono"

    history = (
        await admin_api.get(
            f"/api/v1/telegram-accounts/{account['id']}/history", headers=H(admin_api)
        )
    ).json()
    assert any(x["action"] == "telegram_account.blocked" for x in history)
    assert all("code" not in x for x in history)

    r = await admin_api.post(
        f"/api/v1/telegram-accounts/{account['id']}/reactivate",
        json={"reason": "volvió"}, headers=H(admin_api),
    )
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert await count(sessions, DemandObservation) == 1


async def test_api_rechaza_solicitud_y_refleja_el_estado(admin_api, sessions, user, tg_api):
    c = user(55500051)
    await c.text("/solicitar")
    code = code_of(tg_api.texts()[-1])
    req = (
        await admin_api.post(
            "/api/v1/telegram-access-requests/lookup", json={"code": code}, headers=H(admin_api)
        )
    ).json()["request"]
    r = await admin_api.post(
        f"/api/v1/telegram-access-requests/{req['id']}/reject",
        json={"reason": "no corresponde"}, headers=H(admin_api),
    )
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    rows = (await admin_api.get("/api/v1/telegram-access-requests", headers=H(admin_api))).json()
    assert rows[0]["status"] == "rejected" and rows[0]["reject_reason"] == "no corresponde"
    assert await count(sessions, TelegramAccount) == 0


async def test_api_demo_header_no_sirve_para_organizacion_real(sessions, real_farm, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    app = create_app()

    async def _s():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(
            "/api/v1/telegram-access-requests",
            headers={"X-Demo-User-Id": str(real_farm["pao"])},
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "STRONG_AUTH_REQUIRED"
    get_settings.cache_clear()


@pytest.fixture
async def jwt_api(sessions, real_farm, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "supabase_jwt")
    monkeypatch.setenv("SUPABASE_JWKS_URL", "https://example.supabase.co/auth/v1/.well-known/jwks.json")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", "https://example.supabase.co/auth/v1")
    get_settings.cache_clear()

    async def fake_verify(token, _settings):
        if token == "token-pao":
            return "sub-pao"
        if token == "token-desconocido":
            return "sub-desconocido"
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


async def test_api_jwt_resuelve_permisos_desde_postgresql(jwt_api):
    assert (await jwt_api.get("/api/v1/telegram-accounts")).status_code == 401
    r = await jwt_api.get(
        "/api/v1/telegram-accounts", headers={"Authorization": "Bearer token-desconocido"}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "USER_NOT_PROVISIONED"
    r = await jwt_api.get(
        "/api/v1/telegram-accounts", headers={"Authorization": "Bearer token-pao"}
    )
    assert r.status_code == 200
    # La cabecera de demo se ignora por completo en modo JWT.
    r = await jwt_api.get("/api/v1/telegram-accounts", headers={"X-Demo-User-Id": str(uuid.uuid4())})
    assert r.status_code == 401


# ---------------------------------------------------------------- validador JWT


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        auth_mode="supabase_jwt",
        supabase_jwks_url="https://example.test/jwks",
        supabase_jwt_issuer="https://example.test/auth/v1",
    )


def _token(key, *, kid="k1", **claims):
    base = {
        "sub": "sub-1",
        "aud": "authenticated",
        "iss": "https://example.test/auth/v1",
        "exp": int(datetime.now(UTC).timestamp()) + 300,
        "iat": int(datetime.now(UTC).timestamp()),
        "role": "authenticated",
    }
    base.update(claims)
    return jwt.encode(base, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def jwks(monkeypatch, rsa_key):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key()))
    jwk.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    calls = {"n": 0}

    async def fake_fetch(_url):
        calls["n"] += 1
        return {"keys": [jwk]}

    monkeypatch.setattr(supabase_auth, "_fetch_jwks", fake_fetch)
    supabase_auth.clear_jwks_cache()
    yield calls
    supabase_auth.clear_jwks_cache()


async def test_jwt_valido_devuelve_sub_y_cachea_jwks(rsa_key, jwks):
    token = _token(rsa_key)
    assert await supabase_auth.verify_supabase_jwt(token, _settings()) == "sub-1"
    assert await supabase_auth.verify_supabase_jwt(token, _settings()) == "sub-1"
    assert jwks["n"] == 1  # el JWKS se cachea


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "otra"},
        {"iss": "https://otro.test"},
        {"exp": 1},
        {"role": "service_role"},
        {"sub": ""},
    ],
)
async def test_jwt_con_claims_invalidos_se_rechaza(rsa_key, jwks, claims):
    with pytest.raises(DomainError) as e:
        await supabase_auth.verify_supabase_jwt(_token(rsa_key, **claims), _settings())
    assert e.value.code == "INVALID_TOKEN"


async def test_jwt_firmado_con_otra_clave_se_rechaza(rsa_key, jwks):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(DomainError) as e:
        await supabase_auth.verify_supabase_jwt(_token(other), _settings())
    assert e.value.code == "INVALID_TOKEN"


# ---------------------------------------------------------------- migración


async def test_migracion_0007_reasocia_solo_vinculos_privados_reales(db_url, engine, sessions):
    """Compatibilidad: un chat privado real ya autorizado sigue funcionando sin reautorizar."""
    command.downgrade(alembic_config(db_url), "0005")
    async with sessions() as s, s.begin():
        real = (
            await s.execute(
                text("INSERT INTO organization (name, kind) VALUES ('Legacy real', 'real_test') RETURNING id")
            )
        ).scalar_one()
        sim = (
            await s.execute(
                text("INSERT INTO organization (name, kind) VALUES ('Legacy sim', 'simulation') RETURNING id")
            )
        ).scalar_one()
        ids = {}
        for key, org, chat, kind in [
            ("privado", real, "6358481229", "private"),
            ("grupo", real, "-1006358481229", "group"),
            ("simulado", sim, "5550001", "private"),
        ]:
            ids[key] = (
                await s.execute(
                    text(
                        "INSERT INTO channel_binding (organization_id, external_chat_id, chat_kind, mode) "
                        "VALUES (:org, :chat, :kind, 'shared_device') RETURNING id"
                    ),
                    {"org": org, "chat": chat, "kind": kind},
                )
            ).scalar_one()
    command.upgrade(alembic_config(db_url), "head")

    async with sessions() as s:
        rows = (await s.execute(select(TelegramAccount))).scalars().all()
        bindings = {b.id: b for b in (await s.execute(select(ChannelBinding))).scalars().all()}
    assert len(rows) == 1
    assert rows[0].external_user_id == "6358481229"
    assert rows[0].mode == "shared_device" and rows[0].origin == "backfill"
    assert bindings[ids["privado"]].account_id == rows[0].id
    assert bindings[ids["grupo"]].account_id is None
    assert bindings[ids["simulado"]].account_id is None


# ------------------------------------------------ T1: auxiliares por cuenta y sucursal


async def shared_account(sessions, farm, uid, *, links=(), branch=None, label="Teléfono compartido"):
    issued = await issue(
        sessions, farm, user_id=str(uid), chat_id=str(uid), update_id=str(uid)
    )
    approval = await approve(
        sessions, farm, issued.request.id, mode="shared_device", person=None,
        label=label, branch_label=branch,
    )
    async with sessions() as s, s.begin():
        for key in links:
            await access.add_account_auxiliary(s, actor(farm), approval.account.id, farm[key])
    return approval


async def active_sessions(sessions, binding_id):
    async with sessions() as s:
        return (
            await s.execute(
                select(ActorSession).where(
                    ActorSession.channel_binding_id == binding_id,
                    ActorSession.ended_at.is_(None),
                )
            )
        ).scalars().all()


async def test_teclado_nuevo_y_cambiar_auxiliar_solo_con_varios(
    farm, sessions, user, tg_api
):
    await shared_account(sessions, farm, 55500071, links=("ana",))
    one = user(55500071)
    await one.text("/start", now=mins(1))
    labels = [
        b["text"]
        for row in tg_api.sent()[-1]["reply_markup"]["keyboard"]
        for b in row
    ]
    assert "Ver faltantes" in labels and "Ver auxiliar activo" in labels
    assert "Cambiar auxiliar" not in labels
    assert "Finalizar reportes" not in labels  # /salir sigue como comando oculto

    await shared_account(sessions, farm, 55500072, links=("ana", "luis"))
    two = user(55500072)
    await two.text("/start", now=mins(2))
    await two.click(f"sel:{farm['luis']}", now=mins(2))
    await two.text("hola", now=mins(3))
    labels = [
        b["text"]
        for row in tg_api.sent()[-1]["reply_markup"]["keyboard"]
        for b in row
    ]
    assert "Cambiar auxiliar" in labels and "Ver faltantes" in labels


async def test_un_solo_auxiliar_se_identifica_automaticamente_y_conserva_sucursal(
    farm, sessions, user, tg_api
):
    """Sin selector ni confirmación: único auxiliar, durante días, reinicios y «Finalizar»."""
    a = await shared_account(sessions, farm, 55500060, links=("ana",), branch="Sucursal Centro")
    c = user(55500060)

    await c.text("/start", now=mins(1))
    hello = tg_api.texts()[-1]
    assert "Ana" in hello and "identificación automática" in hello
    assert "¿Quién" not in hello

    await c.text("falta ibuprofeno 400", now=mins(2))
    async with sessions() as s:
        first = (
            await s.execute(
                select(DemandObservation).where(DemandObservation.raw_text == "falta ibuprofeno 400")
            )
        ).scalar_one()
    assert first.author_id == farm["ana"] and first.identity_level == "declared"
    assert first.account_id == a.account.id and first.branch_label == "Sucursal Centro"

    async with sessions() as s, s.begin():
        await access.update_account(
            s, actor(farm), a.account.id, branch_label="Sucursal Norte", update_branch=True
        )

    await c.text("falta paracetamol 500", now=mins(3))
    async with sessions() as s:
        second = (
            await s.execute(
                select(DemandObservation).where(DemandObservation.raw_text == "falta paracetamol 500")
            )
        ).scalar_one()
        first = await s.get(DemandObservation, first.id)
    assert second.branch_label == "Sucursal Norte"
    assert first.branch_label == "Sucursal Centro"  # la instantánea no se reescribe
    assert second.author_id == farm["ana"] and second.account_id == a.account.id

    await c.text("finalizar reportes", now=mins(4))
    assert "próximo mensaje se identifica automáticamente" in tg_api.texts()[-1]
    assert await active_sessions(sessions, a.binding.id) == []

    # Una semana después: ni la sesión ni el reinicio cambian la identificación automática.
    await c.text("falta amoxicilina 500", now=mins(4 + 60 * 24 * 7))
    async with sessions() as s:
        third = (
            await s.execute(
                select(DemandObservation).where(DemandObservation.raw_text == "falta amoxicilina 500")
            )
        ).scalar_one()
    assert third.author_id == farm["ana"] and third.branch_label == "Sucursal Norte"
    assert "¿Quién" not in tg_api.texts()[-1]


async def test_dos_auxiliares_solo_muestran_los_de_su_cuenta_y_rechazan_otra_cuenta(
    farm, sessions, user, tg_api
):
    a = await shared_account(sessions, farm, 55500061, links=("ana", "luis"), branch="A")
    b = await shared_account(sessions, farm, 55500062, links=("admin",), branch="B")
    chat_a, chat_b = user(55500061), user(55500062)

    await chat_a.text("/start", now=mins(1))
    assert "¿Quién va a reportar ahora?" in tg_api.texts()[-1]
    async with sessions() as s:
        out = (
            await s.execute(
                select(OutboundMessage)
                .where(OutboundMessage.channel_binding_id == a.binding.id)
                .order_by(OutboundMessage.seq)
            )
        ).scalars().all()
    with_buttons = [m for m in out if m.payload.get("buttons")]
    labels = {b["text"] for row in with_buttons[-1].payload["buttons"] for b in row}
    assert labels == {"Ana", "Luis"}  # solo los de esta cuenta

    await chat_a.click(f"sel:{farm['admin']}", now=mins(2))
    assert any(
        "no está habilitada para reportar en este teléfono" in t
        for t in tg_api.texts()[-3:]
    )
    assert await active_sessions(sessions, a.binding.id) == []

    await chat_a.click(f"sel:{farm['luis']}", now=mins(3))
    assert "Reportando como Luis" in tg_api.texts()[-1]
    await chat_a.text("falta ibuprofeno 400", now=mins(4))

    # La cuenta B tiene UN auxiliar: se identifica sola y no ve el reporte de A.
    await chat_b.text("falta paracetamol 500", now=mins(5))
    async with sessions() as s:
        rows = (await s.execute(select(DemandObservation))).scalars().all()
    by_text = {o.raw_text: o for o in rows}
    assert by_text["falta ibuprofeno 400"].author_id == farm["luis"]
    assert by_text["falta ibuprofeno 400"].account_id == a.account.id
    assert by_text["falta paracetamol 500"].author_id == farm["admin"]
    assert by_text["falta paracetamol 500"].account_id == b.account.id


async def test_cuenta_sin_auxiliares_no_registra_ni_atribuye(
    farm, sessions, user, tg_api
):
    empty = await shared_account(sessions, farm, 55500063)
    other = await shared_account(sessions, farm, 55500064, links=("ana",))
    c = user(55500063)

    await c.text("falta ibuprofeno 400", now=mins(1))
    assert "no tiene auxiliares disponibles para reportar" in tg_api.texts()[-1]
    await c.text("/start", now=mins(2))
    assert "no tiene auxiliares disponibles para reportar" in tg_api.texts()[-1]
    await c._run(
        photo_update(c.uid, c.chat_id, "foto-t1", from_id=55500063), now=mins(3)
    )
    assert "no tiene auxiliares disponibles para reportar" in tg_api.texts()[-1]

    async with sessions() as s:
        msgs = (
            await s.execute(
                select(ChannelMessage).where(ChannelMessage.channel_binding_id == empty.binding.id)
            )
        ).scalars().all()
        atts = (
            await s.execute(
                select(Attachment).where(Attachment.organization_id == farm["org"])
            )
        ).scalars().all()
        obs = (await s.execute(select(DemandObservation))).scalars().all()
    assert msgs == [] and atts == [] and obs == []  # no queda nada pendiente de atribuir

    # Otra cuenta no se ve afectada.
    await user(55500064).text("falta paracetamol 500", now=mins(4))
    async with sessions() as s:
        obs = (await s.execute(select(DemandObservation))).scalars().all()
    assert len(obs) == 1 and obs[0].account_id == other.account.id


async def test_baja_revoca_solo_sus_sesiones_en_esa_cuenta_y_vuelve_al_unico_auxiliar(
    farm, sessions, user, tg_api
):
    a = await shared_account(sessions, farm, 55500065, links=("ana", "luis"))
    b = await shared_account(sessions, farm, 55500066, links=("luis",))
    chat_a, chat_b = user(55500065), user(55500066)

    await chat_a.click(f"sel:{farm['luis']}", now=mins(1))
    await chat_b.text("falta ibuprofeno 400", now=mins(2))  # en B es automático
    assert len(await active_sessions(sessions, a.binding.id)) == 1
    assert len(await active_sessions(sessions, b.binding.id)) == 1

    async with sessions() as s, s.begin():
        result = await access.deactivate_account_auxiliary(
            s, actor(farm), a.account.id, farm["luis"], reason="cambió de turno"
        )
    assert result["sessions_revoked"] == 1
    assert await active_sessions(sessions, a.binding.id) == []
    assert len(await active_sessions(sessions, b.binding.id)) == 1  # la otra cuenta no se toca

    async with sessions() as s:
        link = (
            await s.execute(
                select(TelegramAccountAuxiliary).where(
                    TelegramAccountAuxiliary.account_id == a.account.id,
                    TelegramAccountAuxiliary.app_user_id == farm["luis"],
                )
            )
        ).scalar_one()
    assert link.active is False and link.deactivated_at is not None
    assert link.deactivation_reason == "cambió de turno"

    # Un botón viejo no puede volver a elegir a la persona dada de baja.
    await chat_a.click(f"sel:{farm['luis']}", now=mins(3))
    assert any(
        "no está habilitada para reportar en este teléfono" in t
        for t in tg_api.texts()[-3:]
    )
    assert await active_sessions(sessions, a.binding.id) == []

    # Queda un solo auxiliar: la siguiente persona se identifica sola y es Ana.
    await chat_a.text("falta paracetamol 500", now=mins(4))
    async with sessions() as s:
        mine = (
            await s.execute(
                select(DemandObservation).where(
                    DemandObservation.raw_text == "falta paracetamol 500"
                )
            )
        ).scalar_one()
    assert mine.author_id == farm["ana"] and mine.account_id == a.account.id
    assert "¿Quién" not in tg_api.texts()[-1]


async def test_pasar_de_uno_a_dos_revoca_la_sesion_y_vuelve_a_pedir_identidad(
    farm, sessions, user, tg_api
):
    a = await shared_account(sessions, farm, 55500067, links=("ana",))
    c = user(55500067)
    await c.text("falta ibuprofeno 400", now=mins(1))
    assert len(await active_sessions(sessions, a.binding.id)) == 1

    async with sessions() as s, s.begin():
        result = await access.add_account_auxiliary(s, actor(farm), a.account.id, farm["luis"])
    assert result["sessions_revoked"] == 1
    assert await active_sessions(sessions, a.binding.id) == []

    await c.text("falta paracetamol 500", now=mins(2))
    assert "¿Quién eres?" in tg_api.texts()[-1]
    async with sessions() as s:
        msgs = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.channel_binding_id == a.binding.id
                )
            )
        ).scalars().all()
    pending = [m for m in msgs if m.attribution_status == "pending_attribution"]
    assert len(pending) == 1 and pending[0].author_id is None


async def test_convertir_a_personal_desactiva_asociaciones_sin_borrar_historial(
    farm, sessions, user, tg_api
):
    a = await shared_account(sessions, farm, 55500068, links=("ana", "luis"))
    await block(sessions, farm, a.account.id)

    issued = await issue(sessions, farm, user_id="55500068", chat_id="55500068", update_id="personal-1")
    personal = await approve(
        sessions, farm, issued.request.id, mode="personal", person="luis", label="Luis"
    )
    assert personal.account.id == a.account.id and personal.account.mode == "personal"

    async with sessions() as s:
        links = (
            await s.execute(
                select(TelegramAccountAuxiliary).where(
                    TelegramAccountAuxiliary.account_id == a.account.id
                )
            )
        ).scalars().all()
        history = await access_scope_aux(s, farm, a.account.id)
    assert len(links) == 2 and all(not link.active for link in links)
    assert all(link.deactivated_by == farm["admin"] for link in links)
    assert all(link.deactivation_reason == "account_mode_personal" for link in links)
    assert len(history) == 2  # siguen visibles como historial

    # La cuenta personal usa su persona fija, no las asociaciones compartidas.
    await user(55500068).text("falta ibuprofeno 400", now=mins(20))
    async with sessions() as s:
        obs = (await s.execute(select(DemandObservation))).scalars().all()
    assert len(obs) == 1 and obs[0].author_id == farm["luis"]
    assert obs[0].identity_level == "account"

    # Volver a compartida no reactiva nada: cero accesos implícitos.
    await block(sessions, farm, a.account.id)
    issued = await issue(sessions, farm, user_id="55500068", chat_id="55500068", update_id="shared-2")
    again = await approve(
        sessions, farm, issued.request.id, mode="shared_device", person=None, label="Otra vez"
    )
    await user(55500068).text("falta paracetamol 500", now=mins(21))
    assert "no tiene auxiliares disponibles para reportar" in tg_api.texts()[-1]
    async with sessions() as s:
        obs = (await s.execute(select(DemandObservation))).scalars().all()
    assert len(obs) == 1 and again.account.id == a.account.id


async def access_scope_aux(session, farm, account_id):
    return (await access.list_account_auxiliaries(session, actor(farm), account_id))["auxiliaries"]


async def test_servicio_de_auxiliares_valida_modo_persona_y_permiso(farm, sessions):
    a = await shared_account(sessions, farm, 55500069)

    async with sessions() as s, s.begin():
        pending = AppUser(
            organization_id=farm["org"], display_name="Solo admin", roles=["admin"]
        )
        s.add(pending)
        await s.flush()
        pending_id = pending.id

    async with sessions() as s, s.begin():
        with pytest.raises(DomainError) as e:
            await access.add_account_auxiliary(s, actor(farm), a.account.id, pending_id)
    assert e.value.code == "PERSON_CANNOT_REPORT"

    async with sessions() as s, s.begin():
        await access.add_account_auxiliary(s, actor(farm), a.account.id, farm["ana"])
    async with sessions() as s, s.begin():
        with pytest.raises(DomainError) as e:
            await access.add_account_auxiliary(s, actor(farm), a.account.id, farm["ana"])
    assert e.value.code == "AUXILIARY_ALREADY_ENABLED"

    async with sessions() as s, s.begin():
        with pytest.raises(DomainError) as e:
            await access.deactivate_account_auxiliary(
                s, actor(farm), a.account.id, farm["luis"]
            )
    assert e.value.code == "AUXILIARY_NOT_ENABLED"

    async with sessions() as s, s.begin():
        with pytest.raises(DomainError) as e:
            await access.add_account_auxiliary(
                s, actor(farm, "ana", ("auxiliary",)), a.account.id, farm["luis"]
            )
    assert e.value.code == "FORBIDDEN"

    personal = await approve(
        sessions, farm,
        (await issue(sessions, farm, user_id="55500070", chat_id="55500070", update_id="p-111")).request.id,
        mode="personal", person="ana", label="Personal",
    )  # fmt: skip
    async with sessions() as s, s.begin():
        with pytest.raises(DomainError) as e:
            await access.add_account_auxiliary(
                s, actor(farm), personal.account.id, farm["luis"]
            )
    assert e.value.code == "ACCOUNT_NOT_SHARED"

    async with sessions() as s, s.begin():
        with pytest.raises(DomainError) as e:
            await access.update_account(s, actor(farm), a.account.id)
    assert e.value.code == "NOTHING_TO_UPDATE"


async def test_api_configura_sucursal_y_auxiliares_de_una_cuenta(admin_api, sessions, user, tg_api):
    c = user(55500071)
    await c.text("/solicitar")
    code = code_of(tg_api.texts()[-1])
    req = (
        await admin_api.post(
            "/api/v1/telegram-access-requests/lookup",
            json={"code": code}, headers=H(admin_api),
        )
    ).json()["request"]
    account = (
        await admin_api.post(
            f"/api/v1/telegram-access-requests/{req['id']}/approve",
            json={
                "display_label": "Teléfono sucursal",
                "mode": "shared_device",
                "branch_label": "Sucursal Norte",
            },
            headers=H(admin_api),
        )
    ).json()
    assert account["branch_label"] == "Sucursal Norte" and account["mode"] == "shared_device"

    r = await admin_api.post(
        f"/api/v1/telegram-accounts/{account['id']}/auxiliaries",
        json={"app_user_id": str(admin_api.farm["ana"])},
        headers=H(admin_api),
    )
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True and r.json()["sessions_revoked"] == 0
    assert r.json()["history_preserved"] is True

    listing = (
        await admin_api.get(
            f"/api/v1/telegram-accounts/{account['id']}/auxiliaries", headers=H(admin_api)
        )
    ).json()
    assert listing["branch_label"] == "Sucursal Norte"
    assert [x["display_name"] for x in listing["auxiliaries"]] == ["Ana"]
    assert listing["auxiliaries"][0]["active"] is True

    r = await admin_api.patch(
        f"/api/v1/telegram-accounts/{account['id']}",
        json={"branch_label": "Sucursal Sur"},
        headers=H(admin_api),
    )
    assert r.status_code == 200 and r.json()["branch_label"] == "Sucursal Sur"

    r = await admin_api.patch(
        f"/api/v1/telegram-accounts/{account['id']}", json={}, headers=H(admin_api)
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "NOTHING_TO_UPDATE"

    r = await admin_api.post(
        f"/api/v1/telegram-accounts/{account['id']}/auxiliaries/"
        f"{admin_api.farm['ana']}/deactivate",
        json={"reason": "prueba"},
        headers=H(admin_api),
    )
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False and r.json()["history_preserved"] is True
    listing = (
        await admin_api.get(
            f"/api/v1/telegram-accounts/{account['id']}/auxiliaries", headers=H(admin_api)
        )
    ).json()
    assert listing["auxiliaries"][0]["active"] is False
    assert listing["auxiliaries"][0]["deactivation_reason"] == "prueba"
