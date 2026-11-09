"""Seguridad del barrido manual de fotografías: siempre acotado a una organización.

Una administradora no puede limpiar binarios globalmente ni ver hashes de fotografías que su
organización nunca tocó. La protección de imágenes compartidas por SHA-256 se mantiene: la
comprobación final de referencias es global, así que una foto que otra organización todavía
necesita no se borra.

No usa Telegram real ni OpenAI: el canal es el simulador y la visión es el doble `LiveVision`.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AppUser,
    Attachment,
    ChannelBinding,
    ChannelMessage,
    InboundEvent,
    TempPhoto,
)
from app.services.photo_gc import collect_unreferenced
from app.services.storage import PostgresPrivateStorage, sha256_hex, storage_key
from tests.test_h1b_channel import T0, Chat
from tests.test_h1b_photos import CREMA, LiveVision, img
from tests.tgsim import fake_jpeg, photo_update

pytestmark = pytest.mark.db


@pytest.fixture
def photo_chat(make_rt, seed, tg_api, chan):
    """Mismo flujo de Telegram simulado que H1-B: genera una foto con reporte activo."""

    def build(vision=None):
        return Chat(make_rt(vision=vision), seed, tg_api)

    return build


@pytest.fixture
async def api(sessions, monkeypatch):
    """API real con sesión de prueba y autenticación de demostración (organización simulación)."""
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    app = create_app()

    async def _s():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    get_settings.cache_clear()


async def _admin(sessions, org) -> uuid.UUID:
    async with sessions() as s, s.begin():
        user = AppUser(organization_id=org, display_name="Admin de fotos", roles=["admin"])
        s.add(user)
        await s.flush()
        return user.id


async def _temp(sessions, sha: str) -> TempPhoto | None:
    async with sessions() as s:
        return await s.get(TempPhoto, sha)


async def _attachments(sessions) -> list[Attachment]:
    async with sessions() as s:
        return (await s.execute(select(Attachment).order_by(Attachment.created_at))).scalars().all()


async def _inactive_photo(
    sessions, *, organization_id, author_id, data: bytes, file_id: str
) -> str:
    """Crea una foto sin reporte vivo (candidata a liberarse) para una organización concreta."""
    sha = sha256_hex(data)
    suffix = uuid.uuid4().hex
    # La base exige chats ficticios `555000x` en organizaciones de simulación.
    chat_id = "555000" + str(uuid.uuid4().int % 1_000_000).zfill(6)
    async with sessions() as s, s.begin():
        binding = ChannelBinding(
            organization_id=organization_id, external_chat_id=chat_id
        )
        s.add(binding)
        await s.flush()
        event = InboundEvent(
            organization_id=organization_id,
            provider="telegram",
            channel_binding_id=binding.id,
            external_event_id=f"ev-{suffix}",
            event_type="message",
            payload={},
            payload_hash="0" * 64,
        )
        s.add(event)
        await s.flush()
        message = ChannelMessage(
            organization_id=organization_id,
            channel_binding_id=binding.id,
            inbound_event_id=event.id,
            external_message_id=f"msg-{suffix}",
            kind="photo",
            data_origin="simulated",
            attribution_status="attributed",
            author_id=author_id,
        )
        s.add(message)
        await s.flush()
        await PostgresPrivateStorage(sessions).put(
            data, mime_type="image/jpeg", session=s
        )
        s.add(
            Attachment(
                organization_id=organization_id,
                channel_message_id=message.id,
                author_id=author_id,
                provider_file_id=file_id,
                content_sha256=sha,
                storage_key=storage_key(sha),
                mime_type="image/jpeg",
                file_size=len(data),
                status="identified",
                data_origin="simulated",
            )
        )
        await s.flush()
    return sha


# ---------- servicio: alcance por organización ----------


async def test_el_barrido_solo_examina_y_devuelve_fotos_de_su_organizacion(sessions, seed):
    admin_a = await _admin(sessions, seed["org"])
    admin_b = await _admin(sessions, seed["other_org"])
    sha_a = await _inactive_photo(
        sessions,
        organization_id=seed["org"],
        author_id=admin_a,
        data=fake_jpeg(101),
        file_id="a-101",
    )
    sha_b = await _inactive_photo(
        sessions,
        organization_id=seed["other_org"],
        author_id=admin_b,
        data=fake_jpeg(102),
        file_id="b-102",
    )

    async with sessions() as s:
        result = await collect_unreferenced(s, organization_id=seed["org"])
        await s.commit()

    assert result.examined == 1
    assert result.deleted_sha256 == [sha_a]
    assert await _temp(sessions, sha_a) is None
    # La foto de la otra organización no se examinó, no se borró y su hash no se devolvió.
    assert sha_b not in result.deleted_sha256
    assert await _temp(sessions, sha_b) is not None


async def test_una_foto_compartida_por_sha_no_se_borra_si_otra_organizacion_la_necesita(
    sessions, seed, tg_api, photo_chat
):
    c = photo_chat(vision=LiveVision(CREMA))
    file_id = img(tg_api, 103)
    await c.pick("ana")
    await c.send(photo_update, file_id, now=T0)
    (att,) = await _attachments(sessions)
    sha = att.content_sha256
    assert sha is not None

    # La otra organización también tiene una foto con los MISMOS bytes, pero sin reporte activo.
    admin_b = await _admin(sessions, seed["other_org"])
    await _inactive_photo(
        sessions,
        organization_id=seed["other_org"],
        author_id=admin_b,
        data=tg_api.files[file_id],
        file_id="compartida-103",
    )

    # Su barrido examina la fila compartida, pero la comprobación global ve el reporte activo de
    # la primera organización y la conserva.
    async with sessions() as s:
        result = await collect_unreferenced(s, organization_id=seed["other_org"])
        await s.commit()

    assert result.examined == 1
    assert result.deleted == 0 and result.kept == 1
    assert await _temp(sessions, sha) is not None


# ---------- endpoint: permisos y cuerpo ----------


async def test_collect_rechaza_no_admin_y_organizacion_en_el_cuerpo(
    api, sessions, seed
):
    admin_a = await _admin(sessions, seed["org"])
    sha_a = await _inactive_photo(
        sessions,
        organization_id=seed["org"],
        author_id=admin_a,
        data=fake_jpeg(104),
        file_id="a-104",
    )
    admin_b = await _admin(sessions, seed["other_org"])
    sha_b = await _inactive_photo(
        sessions,
        organization_id=seed["other_org"],
        author_id=admin_b,
        data=fake_jpeg(105),
        file_id="b-105",
    )

    # Un rol que no es admin no puede ejecutar el barrido.
    r = await api.post(
        "/api/v1/photos/maintenance/collect",
        json={"dry_run": True},
        headers={"X-Demo-User-Id": str(seed["pao"])},
    )
    assert r.status_code == 403

    # La organización no se puede elegir desde el cuerpo.
    r = await api.post(
        "/api/v1/photos/maintenance/collect",
        json={"dry_run": True, "organization_id": str(seed["other_org"])},
        headers={"X-Demo-User-Id": str(admin_a)},
    )
    assert r.status_code == 400

    # La admin de A solo limpia lo suyo y nunca ve el hash de B.
    r = await api.post(
        "/api/v1/photos/maintenance/collect",
        json={"dry_run": False},
        headers={"X-Demo-User-Id": str(admin_a)},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["examined"] == 1
    assert body["deleted_sha256"] == [sha_a]
    assert await _temp(sessions, sha_b) is not None

    # La admin de B limpia su propia foto sin tocar la de A (que ya no existe).
    r = await api.post(
        "/api/v1/photos/maintenance/collect",
        json={"dry_run": False},
        headers={"X-Demo-User-Id": str(admin_b)},
    )
    assert r.status_code == 200
    assert r.json()["deleted_sha256"] == [sha_b]
    assert await _temp(sessions, sha_b) is None
