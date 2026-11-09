"""Fotografías en PostgreSQL: persistencia, baja segura y recuperación manual.

No usa Telegram real: el cliente es el simulador de `tgsim` y la recuperación usa un doble que
devuelve bytes conocidos. La visión es el doble `LiveVision`, que no llama a OpenAI.
"""

import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import (
    AppUser,
    Attachment,
    ChannelMessage,
    DemandItem,
    DemandObservation,
    TempPhoto,
)
from app.services.actors import load_actor
from app.services.demand import cancel_item
from app.services.photo_gc import (
    collect_unreferenced,
    recover_missing_for_active_reports,
    release_photos_of_item,
)
from app.services.storage import PostgresPrivateStorage, sha256_hex, storage_key
from tests.test_h1b_channel import T0, Chat, mins, observations
from tests.test_h1b_photos import CREMA, LiveVision, img
from tests.tgsim import fake_jpeg, photo_update

pytestmark = pytest.mark.db


@pytest.fixture
def photo_chat(make_rt, seed, tg_api, chan):
    """Mismo flujo de Telegram simulado que H1-B, sin depender de otro archivo de pruebas."""

    def build(vision=None):
        return Chat(make_rt(vision=vision), seed, tg_api)

    return build


async def _temp(sessions, sha: str) -> TempPhoto | None:
    async with sessions() as s:
        return await s.get(TempPhoto, sha)


async def _attachments(sessions) -> list[Attachment]:
    async with sessions() as s:
        return (
            (await s.execute(select(Attachment).order_by(Attachment.created_at)))
            .scalars()
            .all()
        )


async def _items(sessions) -> list[DemandItem]:
    async with sessions() as s:
        return (await s.execute(select(DemandItem).order_by(DemandItem.created_at))).scalars().all()


async def _admin(sessions, org) -> uuid.UUID:
    async with sessions() as s, s.begin():
        user = AppUser(organization_id=org, display_name="Admin de fotos", roles=["admin"])
        s.add(user)
        await s.flush()
        return user.id


async def _cancel(sessions, admin_id, item: DemandItem) -> None:
    async with sessions() as s, s.begin():
        actor = await load_actor(s, admin_id)
        await cancel_item(
            s, actor, item.id, expected_version=item.version, reason="prueba de fotos"
        )


async def _release(sessions, org, item_id) -> None:
    async with sessions() as s, s.begin():
        await release_photos_of_item(s, organization_id=org, demand_item_id=item_id)


class Fetcher:
    """Doble del descargador de Telegram: devuelve bytes conocidos por file_id."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.calls: list[str] = []

    async def fetch(self, file_id: str) -> bytes:
        self.calls.append(file_id)
        return self.files[file_id]


# ---------- persistencia y deduplicación física ----------


async def test_el_binario_vive_en_postgresql_y_sobrevive_a_otra_conexion(sessions, db_url):
    data = fake_jpeg(70)
    sha = sha256_hex(data)
    key = await PostgresPrivateStorage(sessions).put(data, mime_type="image/jpeg")
    assert key == storage_key(sha)

    # Otra instancia y otro motor: equivale a un proceso nuevo, sin estado en memoria.
    eng = create_async_engine(db_url, poolclass=NullPool)
    other = async_sessionmaker(eng, expire_on_commit=False)
    try:
        assert await PostgresPrivateStorage(other).get(key) == data
        async with other() as s:
            row = await s.get(TempPhoto, sha)
        assert row is not None and row.mime_type == "image/jpeg"
        assert row.size_bytes == len(data) and row.content == data
    finally:
        await eng.dispose()


async def test_dos_auxiliares_con_los_mismos_bytes_comparten_una_sola_copia(
    sessions, seed, tg_api, photo_chat
):
    c = photo_chat(vision=LiveVision(CREMA))
    tg_api.files["same1"] = fake_jpeg(77)
    tg_api.files["same2"] = fake_jpeg(77)  # mismo contenido, otro file_id
    await c.pick("ana")
    await c.send(photo_update, "same1", now=T0)
    await c.pick("luis", now=mins(1))
    await c.send(photo_update, "same2", now=mins(2))

    async with sessions() as s:
        pending = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.confirmation_status == "pending_other_report"
                )
            )
        ).scalar_one()
    await c.click(f"rp:ok:{pending.id}", now=mins(3))

    obs = await observations(sessions)
    assert len(obs) == 2 and obs[0].demand_item_id != obs[1].demand_item_id
    async with sessions() as s:
        count = (
            await s.execute(select(func.count()).select_from(TempPhoto))
        ).scalar_one()
    assert count == 1  # una copia física, dos reportes lógicos


# ---------- la baja depende de las referencias, no de la antigüedad ----------


async def test_cancelar_un_reporte_conserva_la_foto_que_usa_otro_activo(
    sessions, seed, tg_api, photo_chat
):
    c = photo_chat(vision=LiveVision(CREMA))
    tg_api.files["same1"] = fake_jpeg(78)
    tg_api.files["same2"] = fake_jpeg(78)
    await c.pick("ana")
    await c.send(photo_update, "same1", now=T0)
    await c.pick("luis", now=mins(1))
    await c.send(photo_update, "same2", now=mins(2))
    async with sessions() as s:
        pending = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.confirmation_status == "pending_other_report"
                )
            )
        ).scalar_one()
    await c.click(f"rp:ok:{pending.id}", now=mins(3))

    items = await _items(sessions)
    atts = await _attachments(sessions)
    sha = atts[0].content_sha256
    admin_id = await _admin(sessions, seed["org"])

    await _cancel(sessions, admin_id, items[0])
    await _release(sessions, seed["org"], items[0].id)
    assert await _temp(sessions, sha) is not None  # Luis sigue activo

    await _cancel(sessions, admin_id, items[1])
    await _release(sessions, seed["org"], items[1].id)
    assert await _temp(sessions, sha) is None  # ya no lo necesita nadie

    # El historial se conserva íntegro: reportes, observaciones y vínculo con la foto.
    assert len(await observations(sessions)) == 2
    assert {i.status for i in await _items(sessions)} == {"cancelled"}
    assert all(a.storage_key == storage_key(sha) for a in await _attachments(sessions))


async def test_un_reporte_activo_o_una_foto_pendiente_retienen_el_binario(
    sessions, seed, tg_api, photo_chat
):
    c = photo_chat(vision=LiveVision(CREMA))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 79))
    (att,) = await _attachments(sessions)
    sha = att.content_sha256
    async with sessions() as s:
        result = await collect_unreferenced(s, organization_id=seed["org"])
        await s.commit()
    assert result.deleted == 0 and await _temp(sessions, sha) is not None

    # Una foto todavía sin reporte (pendiente de procesar) también se conserva.
    c2 = photo_chat(None)
    c2.uid = 200  # otra conversación: no reutiliza el update_id de la primera
    await c2.pick("luis", now=mins(1))
    await c2.send(photo_update, img(tg_api, 80), now=mins(1))
    async with sessions() as s:
        result = await collect_unreferenced(s, organization_id=seed["org"])
        await s.commit()
    assert result.deleted == 0
    assert len(await _attachments(sessions)) == 2


async def test_fulfilled_y_cancelado_no_retienen_la_foto(
    sessions, seed, tg_api, photo_chat
):
    c = photo_chat(vision=LiveVision(CREMA))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 81))
    (item,) = await _items(sessions)
    (att,) = await _attachments(sessions)
    sha = att.content_sha256

    async with sessions() as s, s.begin():
        row = await s.get(DemandItem, item.id)
        row.status = "fulfilled"  # aún no hay compra real: se simula solo el estado terminal
    async with sessions() as s:
        result = await collect_unreferenced(s, organization_id=seed["org"])
        await s.commit()
    assert result.deleted == 1 and await _temp(sessions, sha) is None
    # La historia del reporte y de la observación sigue disponible.
    assert len(await _items(sessions)) == 1 and len(await observations(sessions)) == 1


# ---------- recuperación desde Telegram (simulada) ----------


async def test_recupera_solo_lo_perdido_de_reportes_activos_y_verifica_el_sha(
    sessions, seed, tg_api, photo_chat
):
    c = photo_chat(vision=LiveVision(CREMA))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 82))
    (att,) = await _attachments(sessions)
    (obs,) = await observations(sessions)
    sha = att.content_sha256
    async with sessions() as s, s.begin():
        await s.execute(delete(TempPhoto).where(TempPhoto.sha256 == sha))

    fetcher = Fetcher({att.provider_file_id: fake_jpeg(82)})
    async with sessions() as s:
        result = await recover_missing_for_active_reports(
            s, fetcher, organization_id=seed["org"], limit=5
        )
        await s.commit()
    assert result.recovered == 1 and result.failed == 0
    assert await _temp(sessions, sha) is not None
    # No crea reportes ni observaciones ni cambia sus datos.
    assert len(await observations(sessions)) == 1
    assert len(await _items(sessions)) == 1
    async with sessions() as s:
        still = await s.get(DemandObservation, obs.id)
    assert still.demand_item_id == obs.demand_item_id and still.author_id == obs.author_id


async def test_un_contenido_que_no_coincide_se_informa_y_no_se_guarda(
    sessions, seed, tg_api, photo_chat
):
    c = photo_chat(vision=LiveVision(CREMA))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 83))
    (att,) = await _attachments(sessions)
    sha = att.content_sha256
    async with sessions() as s, s.begin():
        await s.execute(delete(TempPhoto).where(TempPhoto.sha256 == sha))

    fetcher = Fetcher({att.provider_file_id: fake_jpeg(999)})  # bytes distintos
    async with sessions() as s:
        result = await recover_missing_for_active_reports(
            s, fetcher, organization_id=seed["org"]
        )
        await s.commit()
    assert result.recovered == 0 and result.failed == 1
    assert result.failures[0].code == "sha_mismatch"
    assert await _temp(sessions, sha) is None


async def test_no_recupera_fotos_de_reportes_cerrados(
    sessions, seed, tg_api, photo_chat
):
    c = photo_chat(vision=LiveVision(CREMA))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 84))
    (item,) = await _items(sessions)
    (att,) = await _attachments(sessions)
    sha = att.content_sha256
    admin_id = await _admin(sessions, seed["org"])
    await _cancel(sessions, admin_id, item)
    async with sessions() as s, s.begin():
        await s.execute(delete(TempPhoto).where(TempPhoto.sha256 == sha))

    fetcher = Fetcher({att.provider_file_id: fake_jpeg(84)})
    async with sessions() as s:
        result = await recover_missing_for_active_reports(
            s, fetcher, organization_id=seed["org"]
        )
        await s.commit()
    assert result.candidates == 0 and result.recovered == 0
    assert fetcher.calls == []  # ni siquiera descarga una foto histórica cerrada
