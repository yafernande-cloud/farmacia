"""Utilidades de línea de comandos.

  seed-demo                       organización de SIMULACIÓN (kind=simulation), con sus usuarios y ciclo
  seed-real-test                  organización de PRUEBA REAL (kind=real_test)
  bind-telegram <chat> --org sim|real [--kind private|group]
                                  autoriza un chat (el tipo de chat y de organización deben coincidir)
  list-telegram-accounts --org sim|real --by <app_user_id>
  list-telegram-requests --org sim|real --by <app_user_id> [--status pending|approved|rejected|expired]
  block-telegram-account <account_id> --org sim|real --by <app_user_id> [--reason ...]
  reactivate-telegram-account <account_id> --org sim|real --by <app_user_id> [--reason ...]
  list-auth-subjects --org sim|real --by <app_user_id>
  link-auth-subject <app_user_id> --org sim|real --by <app_user_id> [--subject <sub>]
  unlink-auth-subject <app_user_id> --org sim|real --by <app_user_id> --confirm
  telegram-check | telegram-poll

Nada aquí usa la organización real por defecto: cada comando nombra la suya y no hay valor implícito.
El flujo normal es: la persona escribe /solicitar al bot, recibe un código temporal, y la
administradora aprueba desde el frontend/API con su sesión de Supabase. La CLI queda solo para
tareas de desarrollo y bootstrap; no es obligatoria para administrar accesos. Ningún comando
imprime códigos ni tokens; el `sub` de Supabase es un identificador, no una credencial.
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import dispose_engine, get_engine
from app.models import AppUser, ChannelBinding, Organization, PurchaseCycle, TelegramAccount
from app.services import access, identity
from app.services.actors import Actor, load_actor
from app.services.provenance import OrgKind, validate_account, validate_binding, vision_guard

SIM_ORG = "Farmacia Parque Lantaño — SIMULACIÓN"
REAL_ORG = "Farmacia Parque Lantaño — Prueba real"
SIM_CHAT = "5550001"
STAFF_FILE = Path(__file__).resolve().parents[2] / "database" / "seed" / "staff.json"
ORGS = {"sim": (SIM_ORG, OrgKind.SIMULATION), "real": (REAL_ORG, OrgKind.REAL_TEST)}


def _load_staff() -> list[dict]:
    """El personal inicial viene de un archivo de DATOS (database/seed/staff.json), no del código."""
    return json.loads(STAFF_FILE.read_text(encoding="utf-8"))["staff"]


async def _ensure_org(s: AsyncSession, name: str, kind: str, cycle_name: str) -> Organization:
    org = (
        await s.execute(select(Organization).where(Organization.name == name))
    ).scalar_one_or_none()
    if org is None:
        org = Organization(name=name, kind=str(kind))
        s.add(org)
        await s.flush()
    elif org.kind != kind:  # el tipo es inmutable: un nombre ya existente con otro tipo es un error
        raise SystemExit(f"La organización «{name}» existe con tipo {org.kind}, no {kind}.")
    for person in _load_staff():
        u = (
            await s.execute(
                select(AppUser).where(
                    AppUser.organization_id == org.id,
                    AppUser.display_name == person["display_name"],
                )
            )
        ).scalar_one_or_none()
        if u is None:
            s.add(
                AppUser(
                    organization_id=org.id,
                    display_name=person["display_name"],
                    roles=person["roles"],
                )
            )
    has = (
        await s.execute(
            select(PurchaseCycle).where(
                PurchaseCycle.organization_id == org.id, PurchaseCycle.status == "open"
            )
        )
    ).scalar_one_or_none()
    if has is None:
        s.add(PurchaseCycle(organization_id=org.id, name=cycle_name))
    await s.flush()
    return org


async def _seed(which: str) -> None:
    name, kind = ORGS[which]
    async with AsyncSession(get_engine(), expire_on_commit=False) as s, s.begin():
        org = await _ensure_org(
            s, name, kind, "Ciclo de simulación" if which == "sim" else "Ciclo de prueba real"
        )
        users = (
            (await s.execute(select(AppUser).where(AppUser.organization_id == org.id)))
            .scalars()
            .all()
        )
    print(f"Organización: {name}  (tipo: {kind})")
    for u in users:
        print(f"  {u.display_name:6} {','.join(u.roles):11} X-Demo-User-Id: {u.id}")
    await dispose_engine()


async def bind_telegram(chat_id: str, which: str, kind: str = "private") -> None:
    name, okind = ORGS[which]
    async with AsyncSession(get_engine(), expire_on_commit=False) as s, s.begin():
        org = (
            await s.execute(select(Organization).where(Organization.name == name))
        ).scalar_one_or_none()
        if org is None:
            raise SystemExit(
                f"No existe «{name}». Ejecuta primero seed-{'demo' if which == 'sim' else 'real-test'}."
            )
        validate_binding(chat_id, org.kind)  # chat ficticio ⇄ organización de simulación
        b = (await s.execute(select(ChannelBinding).where(ChannelBinding.provider == "telegram",
                                                          ChannelBinding.external_chat_id == chat_id,
                                                          ChannelBinding.status == "active"))).scalar_one_or_none()  # fmt: skip
        if b is not None and b.organization_id != org.id:
            raise SystemExit(f"El chat {chat_id} ya está autorizado para otra organización.")
        if b is None:
            account_id = await _shared_account_for_chat(s, org, chat_id, kind)
            s.add(
                ChannelBinding(
                    organization_id=org.id,
                    external_chat_id=chat_id,
                    chat_kind=kind,
                    account_id=account_id,
                )
            )
    extra = "" if kind != "private" or org.kind == OrgKind.SIMULATION else " (cuenta de teléfono compartido)"
    print(f"Chat {chat_id} autorizado para «{name}»{extra}.")
    await dispose_engine()


async def _shared_account_for_chat(
    s: AsyncSession, org: Organization, chat_id: str, kind: str
) -> uuid.UUID | None:
    """En un chat privado real, la cuenta que escribe es la del teléfono. En simulación o en grupos
    se mantiene el vínculo legado sin cuenta (el simulador usa otra cuenta ficticia)."""
    if kind != "private" or org.kind == OrgKind.SIMULATION:
        return None
    validate_account(chat_id, org.kind)
    acc = (
        await s.execute(
            select(TelegramAccount).where(
                TelegramAccount.provider == "telegram",
                TelegramAccount.external_user_id == str(chat_id),
            )
        )
    ).scalar_one_or_none()
    if acc is not None:
        if acc.organization_id != org.id:
            raise SystemExit("Esa cuenta de Telegram ya está autorizada en otra organización.")
        return acc.id
    acc = TelegramAccount(
        organization_id=org.id,
        external_user_id=str(chat_id),
        display_label="Teléfono compartido",
        mode="shared_device",
        access_role="operator",
        origin="manual",
    )
    s.add(acc)
    await s.flush()
    return acc.id


async def _admin(which: str, actor_id: str) -> tuple[Actor, str]:
    """Carga el actor administrador desde la base y su organización; el permiso lo valida el servicio."""
    name, _ = ORGS[which]
    async with AsyncSession(get_engine()) as s:
        org = (
            await s.execute(select(Organization).where(Organization.name == name))
        ).scalar_one_or_none()
        if org is None:
            raise SystemExit(f"No existe «{name}». Ejecuta primero el seed correspondiente.")
        actor = await load_actor(s, _uuid(actor_id, "app_user_id"))
        if actor is None or actor.organization_id != org.id:
            raise SystemExit("El administrador indicado no existe o no pertenece a esa organización.")
    return actor, name


def _uuid(raw: str, label: str):
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise SystemExit(f"{label} no es un UUID válido.") from None


def _arg(a: list[str], flag: str, default: str | None = None) -> str | None:
    if flag in a and a.index(flag) + 1 < len(a):
        return a[a.index(flag) + 1]
    return default


def _subject_arg(a: list[str]) -> str:
    """El `sub` se pide por consola si no viene en --subject, para no dejarlo en el historial del shell."""
    value = _arg(a, "--subject")
    if value:
        return value
    if "--subject" in a:
        raise SystemExit("--subject necesita un valor.")
    if not sys.stdin.isatty():
        raise SystemExit("Indica --subject <sub> o ejecuta el comando en una terminal.")
    return input("sub del usuario de Supabase (identificador, no una contraseña): ")


async def list_telegram_accounts(which: str, a: list[str]) -> None:
    by = _arg(a, "--by")
    if not by:
        raise SystemExit("Indica el administrador con --by <app_user_id>.")
    actor, name = await _admin(which, by)
    try:
        async with AsyncSession(get_engine()) as s:
            rows = await access.list_accounts(s, actor)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(getattr(e, "message", str(e))) from None
    print(f"Cuentas de Telegram de «{name}»: {len(rows)}")
    for r in rows:
        chats = ", ".join(f"{b['external_chat_id']}({b['status']})" for b in r["bindings"]) or "sin chat"
        person = f" · persona: {r['app_user_name']}" if r["app_user_name"] else ""
        print(f"  {r['id']}  {r['status']:7} {r['mode']:13} {r['access_role']:8} {r['external_user_id']}  "
              f"{r['display_label']}{person} · chats: {chats}")  # fmt: skip
    await dispose_engine()


async def list_telegram_requests(which: str, a: list[str]) -> None:
    by = _arg(a, "--by")
    if not by:
        raise SystemExit("Indica el administrador con --by <app_user_id>.")
    actor, name = await _admin(which, by)
    try:
        async with AsyncSession(get_engine()) as s:
            rows = await access.list_requests(s, actor, status=_arg(a, "--status"))
    except Exception as e:  # noqa: BLE001
        raise SystemExit(getattr(e, "message", str(e))) from None
    print(f"Solicitudes de acceso de «{name}»: {len(rows)}")
    for r in rows:
        reviewer = r["approved_by_name"] or r["rejected_by_name"] or ""
        print(f"  {r['id']}  {r['status']:8} usuario {r['external_user_id']}  "
              f"vence {r['expires_at'].isoformat()}  {reviewer}")  # fmt: skip
    await dispose_engine()


async def block_telegram_account(account_id: str, which: str, a: list[str]) -> None:
    by = _arg(a, "--by")
    if not by:
        raise SystemExit("Indica el administrador con --by <app_user_id>.")
    actor, name = await _admin(which, by)
    try:
        async with AsyncSession(get_engine(), expire_on_commit=False) as s, s.begin():
            r = await access.block_account(
                s, actor, _uuid(account_id, "account_id"), reason=_arg(a, "--reason")
            )
    except Exception as e:  # noqa: BLE001
        raise SystemExit(getattr(e, "message", str(e))) from None
    print(f"Cuenta {r.account.id} bloqueada en «{name}»: {r.bindings_deactivated} chat(s) desactivado(s), "
          f"{r.sessions_revoked} sesión(es) cerrada(s). El historial se conserva.")  # fmt: skip
    await dispose_engine()


async def reactivate_telegram_account(account_id: str, which: str, a: list[str]) -> None:
    by = _arg(a, "--by")
    if not by:
        raise SystemExit("Indica el administrador con --by <app_user_id>.")
    actor, name = await _admin(which, by)
    try:
        async with AsyncSession(get_engine(), expire_on_commit=False) as s, s.begin():
            r = await access.reactivate_account(
                s, actor, _uuid(account_id, "account_id"), reason=_arg(a, "--reason")
            )
    except Exception as e:  # noqa: BLE001
        raise SystemExit(getattr(e, "message", str(e))) from None
    print(f"Cuenta {r.account.id} rehabilitada en «{name}»: "
          f"{r.bindings_reactivated} chat(s) reactivado(s). El historial se conserva.")  # fmt: skip
    await dispose_engine()


async def list_auth_subjects(which: str, a: list[str]) -> None:
    by = _arg(a, "--by")
    if not by:
        raise SystemExit("Indica el administrador con --by <app_user_id>.")
    actor, name = await _admin(which, by)
    try:
        async with AsyncSession(get_engine()) as s:
            rows = await identity.list_auth_subjects(s, actor)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(getattr(e, "message", str(e))) from None
    linked = sum(1 for r in rows if r["linked"])
    print(f"Identidades web de «{name}»: {linked} enlazada(s) de {len(rows)} persona(s).")
    for r in rows:
        estado = r["auth_subject"] if r["linked"] else "sin enlazar"
        activo = "" if r["active"] else " (inactiva)"
        print(f"  {r['user_id']}  {r['display_name']}{activo}  sub: {estado}")
    await dispose_engine()


async def link_auth_subject(user_id: str, which: str, a: list[str]) -> None:
    by = _arg(a, "--by")
    if not by:
        raise SystemExit("Indica el administrador con --by <app_user_id>.")
    subject = _subject_arg(a)
    actor, name = await _admin(which, by)
    try:
        async with AsyncSession(get_engine(), expire_on_commit=False) as s, s.begin():
            r = await identity.link_auth_subject(
                s, actor, _uuid(user_id, "app_user_id"), subject=subject
            )
    except Exception as e:  # noqa: BLE001
        raise SystemExit(getattr(e, "message", str(e))) from None
    if r["changed"]:
        print(f"{r['display_name']} ({name}): identidad enlazada. Auditoría y evento registrados.")
    else:
        print(f"{r['display_name']} ({name}): esa identidad ya estaba enlazada; sin cambios.")
    print("Antes de habilitar permisos, verifica que Google y correo/contraseña devuelvan ESTE mismo "
          "sub en GET /api/v1/me (docs/AUTH_SUPABASE.md).")
    await dispose_engine()


async def unlink_auth_subject(user_id: str, which: str, a: list[str]) -> None:
    by = _arg(a, "--by")
    if not by:
        raise SystemExit("Indica el administrador con --by <app_user_id>.")
    if "--confirm" not in a:
        raise SystemExit("Desvincular deja a la persona sin acceso web. Repite con --confirm.")
    actor, name = await _admin(which, by)
    try:
        async with AsyncSession(get_engine(), expire_on_commit=False) as s, s.begin():
            r = await identity.unlink_auth_subject(s, actor, _uuid(user_id, "app_user_id"))
    except Exception as e:  # noqa: BLE001
        raise SystemExit(getattr(e, "message", str(e))) from None
    print(f"{r['display_name']} ({name}): identidad desvinculada. "
          "La persona y su historial se conservan.")  # fmt: skip
    await dispose_engine()


async def telegram_check() -> None:
    """getMe: comprueba que el token funciona SIN mostrarlo."""
    from app.core.config import get_settings
    from app.telegram.client import TelegramClient

    st = get_settings()
    if st.telegram_bot_token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN no está configurado en .env")
    c = TelegramClient(st.telegram_bot_token.get_secret_value(), st.telegram_api_base)
    me = await c.call("getMe")
    print(f"Token válido. Bot: @{me['username']} (id {me['id']}).")
    await c.aclose()


async def telegram_poll() -> None:
    from app.core.config import get_settings
    from app.telegram.poller import run_poller
    from app.telegram.runtime import build_runtime

    st = get_settings()
    if st.vision_provider == "simulated":
        # Una visión simulada nunca debe alimentar chats reales: exige que TODOS los chats activos sean de simulación.
        async with AsyncSession(get_engine()) as s:
            kinds = (await s.execute(select(Organization.kind).join(ChannelBinding, ChannelBinding.organization_id == Organization.id)
                                     .where(ChannelBinding.status == "active"))).scalars().all()  # fmt: skip
        try:
            vision_guard(st.vision_provider, list(kinds))
        except Exception as e:  # noqa: BLE001
            raise SystemExit(
                f"{getattr(e, 'message', e)} Pon VISION_PROVIDER=none (o conecta un proveedor real)."
            ) from None
    rt = build_runtime(st)
    if rt is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN no está configurado en .env")
    print("Escuchando Telegram (Ctrl+C para salir). Visión:", st.vision_provider)
    await run_poller(rt)


def _org_arg(a: list[str]) -> str:
    if "--org" not in a or a.index("--org") + 1 >= len(a) or a[a.index("--org") + 1] not in ORGS:
        raise SystemExit(
            "Indica la organización explícitamente: --org sim | --org real (no hay valor por defecto)"
        )
    return a[a.index("--org") + 1]


def main() -> None:
    a = sys.argv[1:]
    if a == ["seed-demo"]:
        asyncio.run(_seed("sim"))
    elif a == ["seed-real-test"]:
        asyncio.run(_seed("real"))
    elif len(a) >= 2 and a[0] == "bind-telegram":
        asyncio.run(bind_telegram(a[1], _org_arg(a), _arg(a, "--kind", "private") or "private"))
    elif len(a) >= 2 and a[0] == "list-telegram-accounts":
        asyncio.run(list_telegram_accounts(_org_arg(a), a))
    elif len(a) >= 2 and a[0] == "list-telegram-requests":
        asyncio.run(list_telegram_requests(_org_arg(a), a))
    elif len(a) >= 3 and a[0] == "block-telegram-account":
        asyncio.run(block_telegram_account(a[1], _org_arg(a), a))
    elif len(a) >= 3 and a[0] == "reactivate-telegram-account":
        asyncio.run(reactivate_telegram_account(a[1], _org_arg(a), a))
    elif len(a) >= 2 and a[0] == "list-auth-subjects":
        asyncio.run(list_auth_subjects(_org_arg(a), a))
    elif len(a) >= 2 and a[0] == "link-auth-subject":
        asyncio.run(link_auth_subject(a[1], _org_arg(a), a))
    elif len(a) >= 2 and a[0] == "unlink-auth-subject":
        asyncio.run(unlink_auth_subject(a[1], _org_arg(a), a))
    elif a == ["telegram-check"]:
        asyncio.run(telegram_check())
    elif a == ["telegram-poll"]:
        asyncio.run(telegram_poll())
    else:
        raise SystemExit(
            "Uso: python -m app.cli seed-demo | seed-real-test | bind-telegram <chat_id> --org sim|real "
            "[--kind private|group] | list-telegram-accounts --org sim|real --by <id> | "
            "list-telegram-requests --org sim|real --by <id> [--status ...] | "
            "block-telegram-account <id> --org sim|real --by <id> [--reason ...] | "
            "reactivate-telegram-account <id> --org sim|real --by <id> [--reason ...] | "
            "list-auth-subjects --org sim|real --by <id> | "
            "link-auth-subject <id> --org sim|real --by <id> [--subject <sub>] | "
            "unlink-auth-subject <id> --org sim|real --by <id> --confirm | "
            "telegram-check | telegram-poll"
        )


if __name__ == "__main__":
    main()
