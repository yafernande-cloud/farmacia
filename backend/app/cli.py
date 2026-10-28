"""Utilidades de línea de comandos.

  seed-demo                       organización de SIMULACIÓN (kind=simulation), con sus usuarios y ciclo
  seed-real-test                  organización de PRUEBA REAL (kind=real_test)
  bind-telegram <chat> --org sim|real   autoriza un chat (el tipo de chat y de organización deben coincidir)
  telegram-check | telegram-poll

Nada aquí usa la organización real por defecto: cada comando nombra la suya y no hay valor implícito.
"""

import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import dispose_engine, get_engine
from app.models import AppUser, ChannelBinding, Organization, PurchaseCycle
from app.services.provenance import OrgKind, validate_binding, vision_guard

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
            s.add(ChannelBinding(organization_id=org.id, external_chat_id=chat_id, chat_kind=kind))
    print(f"Chat {chat_id} autorizado para «{name}».")
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
        asyncio.run(bind_telegram(a[1], _org_arg(a)))
    elif a == ["telegram-check"]:
        asyncio.run(telegram_check())
    elif a == ["telegram-poll"]:
        asyncio.run(telegram_poll())
    else:
        raise SystemExit(
            "Uso: python -m app.cli seed-demo | seed-real-test | bind-telegram <chat_id> --org sim|real | telegram-check | telegram-poll"
        )


if __name__ == "__main__":
    main()
