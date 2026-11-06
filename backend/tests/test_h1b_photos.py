"""H1-B: fotografías por Telegram, sin texto o con texto (PostgreSQL real).

IMPORTANTE — qué se verifica y qué NO:
  * Telegram: SIMULADO (tests/tgsim.py). No se probó contra los servidores reales.
  * Visión: SIMULADA (`SimulatedVisionInterpreter`, resultados escritos a mano, `mode="simulated"`).
    Estas pruebas demuestran que, DADO lo que un proveedor de visión devolvería, el sistema registra,
    separa procedencia y mantiene lo no verificable pendiente. NO demuestran que ninguna IA lea una
    imagen: no hay proveedor de visión conectado. La foto real de Migranol aportada por el usuario
    NO fue procesada por ningún modelo; su contenido se transcribió a mano en `MIGRANOL`.
"""

import stat
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from app.interpretation.vision import (
    SimulatedVisionInterpreter,
    VisionExtraction,
    VisionFailed,
    VisionField,
)
from app.models import Attachment, ChannelMessage, DemandItem
from app.services.photo_processing import process_pending_attachments
from app.services.storage import sha256_hex
from app.telegram.handler import after_handle, handle_update
from tests.conftest import CHAT
from tests.test_h1b_channel import T0, Chat, count, mins, observations  # noqa: F401
from tests.tgsim import callback_update, fake_jpeg, photo_update

pytestmark = pytest.mark.db


def ext(*fields, legibility="clear"):
    return VisionExtraction(
        "simulated",
        "simulator",
        "simulated-v0",
        legibility,
        tuple(VisionField(a, t, c) for a, t, c in fields),
    )


# Lo que se OBSERVA en la caja (transcripción manual de la foto del usuario): marca, laboratorio, forma y
# contenido. Principio activo, dosis y código de barras NO son visibles y por eso NO están aquí.
MIGRANOL = ext(("product_name", "Migranol®", .97), ("laboratory", "Laboratorio Bagó de Chile S.A.", .95),
               ("form", "comprimidos recubiertos", .95), ("content", "10 comprimidos", .95))  # fmt: skip
VIT_D3 = ext(("ingredient", "Vitamina D3", .95), ("strength", "50.000 UI", .93), ("form", "cápsulas", .9),
             ("content", "4 cápsulas", .9), ("visible_quantity", "3 cajas apiladas", .7))  # fmt: skip


@pytest.fixture
def photo_chat(make_rt, seed, tg_api, chan):
    def build(script: dict | None = None, vision=None):
        if vision is None and script is not None:
            vision = SimulatedVisionInterpreter(script)
        return Chat(make_rt(vision=vision), seed, tg_api)

    return build


def img(tg_api, tag=1, fid=None):
    fid = fid or f"fid{tag}"
    tg_api.files[fid] = fake_jpeg(tag)
    return fid


async def attachments(sessions):
    async with sessions() as s:
        return (await s.execute(select(Attachment).order_by(Attachment.created_at))).scalars().all()


async def item_of(sessions):
    async with sessions() as s:
        return (await s.execute(select(DemandItem))).scalar_one()


# ---------- S8: sin proveedor de visión: recibir y registrar, sin inventar ----------


async def test_foto_sin_proveedor_de_vision_se_guarda_y_queda_pendiente(
    photo_chat, sessions, seed, tg_api, tmp_path
):
    c = photo_chat(None)
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 1))
    (a,) = await attachments(sessions)
    assert (
        a.status == "pending"
        and a.author_id == seed["ana"]
        and a.extraction == {}
        and a.extraction_mode is None
    )
    assert (
        a.mime_type == "image/jpeg"
        and a.storage_key
        and a.content_sha256 == sha256_hex(fake_jpeg(1))
    )
    assert (
        "no_vision_provider_connected" in a.uncertainties and a.attempts == 0
    )  # esperar no consume intentos
    assert (
        await observations(sessions) == [] and await count(sessions, DemandItem) == 0
    )  # nada inventado
    assert "Todavía no puedo leerla automáticamente" in tg_api.texts()[-1] and "Ana" in tg_api.texts()[-1]
    stored = tmp_path / "storage" / a.storage_key
    assert (
        stored.read_bytes() == fake_jpeg(1) and stat.S_IMODE(stored.stat().st_mode) == 0o600
    )  # privada
    await process_pending_attachments(c.rt.sessionmaker, c.rt.photos)  # un barrido no la altera
    assert (await attachments(sessions))[0].status == "pending"


# ---------- Caso real: Migranol, fotografía SIN texto (visión SIMULADA) ----------


async def test_migranol_foto_sin_texto_registra_el_faltante_sin_inventar(
    photo_chat, sessions, seed, tg_api
):
    c = photo_chat({"*": MIGRANOL})
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 7))  # sin caption
    (a,) = await attachments(sessions)
    assert (
        a.status == "identified"
        and a.extraction_mode == "simulated"
        and a.vision_provider == "simulator"
    )
    assert a.extraction["mode"] == "simulated"
    (o,) = await observations(sessions)
    assert (
        o.source == "telegram_photo"
        and o.author_id == seed["ana"]
        and o.attachment_id == a.id
        and o.raw_text == ""
    )
    item = await item_of(sessions)
    at = item.attributes
    assert item.display_name == "Migranol comprimido recubierto x 10" and at["name_anchor"] is True
    assert at["brand"] == {"value": "Migranol®", "basis": "inferred", "origin": "image"}
    assert (
        at["laboratory"]["value"] == "Laboratorio Bagó de Chile S.A."
        and at["laboratory"]["origin"] == "image"
    )
    assert (
        at["form"]["origin"] == "image"
        and at["content"]["origin"] == "image"
        and at["content"]["basis"] == "inferred"
    )
    for k in ("strengths", "unassigned"):
        assert at[k] == []  # dosis y principio activo: desconocidos
    for k in ("gtin", "manufacturer", "registration_holder", "marketer", "sanitary_registration"):
        assert k not in at  # nada de lo no visible
    assert (
        item.pharma_identity_key is None
        and item.resolved_identity_key is None
        and item.target_quantity is None
    )
    pend = {(p["attribute"], p["reason"]) for p in o.parsed["pending_validation"]}
    assert ("active_ingredient", "not_stated") in pend and ("strength", "not_stated") in pend
    assert o.parsed["identity"]["pharmaceutical"]["complete"] is False
    reply = tg_api.texts()[-1]  # breve, natural, sin asegurar identificación farmacéutica
    assert reply.startswith(
        "✅ Anotado: Migranol comprimido recubierto x 10 — 1 unidad."
    )
    assert "Falta confirmar el principio activo y la dosis." in reply
    assert "lectura simulada" in reply and "?" not in reply and len(reply) < 260
    assert "👤 Reportado por: Ana" in reply


async def test_migranol_sin_catalogo_no_hay_candidatos_y_con_referencia_se_propone_sin_aplicar(
    photo_chat, sessions, seed
):
    """La referencia es un FIXTURE con un principio activo FICTICIO: no afirma nada sobre Migranol real."""
    from app.services.actors import load_actor
    from app.services.catalog import create_commercial_reference, create_pharma_identity

    c = photo_chat({"*": MIGRANOL})
    await c.pick("ana")
    await c.send(photo_update, img(c.api, 1))
    assert (await observations(sessions))[0].interpretation[
        "candidates"
    ] == []  # caso real: nada que proponer

    async with sessions() as s, s.begin():
        pao = await load_actor(s, seed["pao"])
        ident = await create_pharma_identity(
            s, pao, "activo de prueba 100 mg comprimido recubierto"
        )
        await create_commercial_reference(
            s, pao, brand="Migranol", laboratory="Bagó", pharma_identity_id=ident.id
        )
    await c.send(photo_update, img(c.api, 2))  # otra foto de la misma caja
    o2 = (await observations(sessions))[1]
    (cand,) = o2.interpretation["candidates"]
    assert (
        cand["kind"] == "commercial_reference"
        and cand["verified"] is True
        and "not_applied_until_confirmed" in cand["reasons"]
    )
    item = await item_of(sessions)
    assert (
        item.attributes["strengths"] == [] and item.attributes["name_anchor"] is True
    )  # NO se aplicó solo


async def test_compras_confirma_el_principio_activo_de_un_producto_anclado_por_marca(
    photo_chat, sessions, seed
):
    from decimal import Decimal as D

    from app.domain.products import ConfirmPatch
    from app.domain.units import build_measure
    from app.services.actors import load_actor
    from app.services.demand import confirm_item_attributes

    c = photo_chat({"*": MIGRANOL})
    await c.pick("ana")
    await c.send(photo_update, img(c.api, 1))
    item = await item_of(sessions)
    async with sessions() as s, s.begin():
        pao = await load_actor(s, seed["pao"])
        new = await confirm_item_attributes(s, pao, item.id, expected_version=1,
                                            patch=ConfirmPatch(ingredient="activo de prueba", strengths=[build_measure(D(100), "mg")],
                                                               confirm_inferred={"form", "content"}))  # fmt: skip
    at = new.attributes
    assert new.ingredient == "activo de prueba" and at["name_anchor"] is False and new.version == 2
    assert (
        at["strengths"][0]["basis"] == "confirmed"
        and at["strengths"][0]["origin"] == "confirmation"
    )
    assert (
        at["brand"]["origin"] == "image" and at["laboratory"]["origin"] == "image"
    )  # la procedencia visual se conserva
    assert new.pharma_identity_key == "activo de prueba|mass:100mg|comprimido recubierto|-"
    assert (
        new.resolved_identity_key.endswith(
            "|brand:migranol®|laboratory:laboratorio bagó de chile s.a."
        )
        is False
    )  # marca inferida: no discrimina aún
    assert new.target_quantity is None


# ---------- S8 con texto: foto + cantidad ----------


async def test_foto_vitamina_d3_con_texto_faltan_2_cajas(photo_chat, sessions, tg_api):
    c = photo_chat({"*": VIT_D3})
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 3), caption="faltan 2 cajas")
    (o,) = await observations(sessions)
    assert (o.declared_quantity, o.declared_unit) == (2, "box") and o.raw_text == "faltan 2 cajas"
    item = await item_of(sessions)
    assert item.target_quantity is None  # la cantidad solicitada es solo sugerencia
    at = item.attributes
    assert (
        item.ingredient == "vitamina d3"
        and at["strengths"][0]["measure"]["unit"] == "ui"
        and at["strengths"][0]["origin"] == "image"
    )
    assert (
        at["strengths"][0]["basis"] == "inferred" and at["content"]["measure"]["value"] == "4"
    )  # contenido, NO cantidad
    assert (
        at["form"]["value"] == "capsula"
        and "— 2 cajas." in tg_api.texts()[-1]
    )


async def test_la_cantidad_visible_en_el_envase_no_es_cantidad_solicitada(photo_chat, sessions):
    c = photo_chat({"*": VIT_D3})  # incluye visible_quantity «3 cajas apiladas»
    await c.pick("ana")
    await c.send(photo_update, img(c.api, 3))  # sin texto
    (o,) = await observations(sessions)
    # la cantidad visible en el envase no se usa; sin pedido explícito rige la unidad predeterminada
    assert (o.declared_quantity, o.declared_unit) == (1, "unit")
    assert o.interpretation["quantity_origin"] == "default"
    assert (await item_of(sessions)).target_quantity is None


async def test_foto_con_texto_que_la_contradice_queda_en_conflicto(photo_chat, sessions, tg_api):
    c = photo_chat({"*": VIT_D3})
    await c.pick("ana")
    await c.send(photo_update, img(c.api, 3), caption="falta vitamina d3 25000 ui")
    (o,) = await observations(sessions)
    assert (
        o.match_status == "needs_review" and o.demand_item_id is None
    )  # AC14: jamás se resuelve sola
    assert "image_text_conflict" in str(o.interpretation["uncertainties"])
    (a,) = await attachments(sessions)
    assert a.status == "needs_review" and "image_text_conflict:strength" in a.uncertainties
    assert "no coinciden" in tg_api.texts()[-1] and await count(sessions, DemandItem) == 0


async def test_foto_borrosa_se_guarda_pendiente_de_identificacion_sin_inventar(
    photo_chat, sessions, tg_api
):
    c = photo_chat(
        {"*": ext(("ingredient", "algo que parece ibuprofeno", 0.99), legibility="blurry")}
    )
    await c.pick("ana")
    await c.send(photo_update, img(c.api, 4))
    (a,) = await attachments(sessions)
    assert (
        a.status == "needs_review"
        and "image_blurry" in a.uncertainties
        and a.extraction_mode == "simulated"
    )
    assert await observations(sessions) == [] and await count(sessions, DemandItem) == 0
    assert "No pude identificar el producto de la fotografía." in tg_api.texts()[-1]
    assert "La dejé pendiente de revisión." in tg_api.texts()[-1]
    assert "👤 Reportado por: Ana" in tg_api.texts()[-1]


async def test_pie_de_foto_identifica_el_producto_cuando_la_imagen_no_sirve(photo_chat, sessions):
    c = photo_chat({"*": ext(legibility="unreadable")})
    await c.pick("ana")
    await c.send(photo_update, img(c.api, 4), caption="falta ibuprofeno 400 mg")
    (o,) = await observations(sessions)
    assert (
        o.parsed["strengths"][0]["origin"] == "text"
        and (await attachments(sessions))[0].status == "needs_review"
    )


# ---------- visión real (doble controlado): MISMO flujo que un texto ----------


class LiveVision:
    """Doble del proveedor real: responde lo que un `VisionExtraction` live devolvería.

    No hay ninguna llamada a OpenAI: el adaptador HTTP se prueba aparte con `httpx.MockTransport`.
    """

    def __init__(self, extraction=None, error=None):
        self.extraction, self.error, self.calls = extraction, error, 0

    async def extract(self, image, mime, caption):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.extraction


CREMA = VisionExtraction(
    "live",
    "openai",
    "gpt-vision-test",
    "clear",
    (
        VisionField("ingredient", "crema de manos", 0.95),
        VisionField("variant", "Berry Warm", 0.9),
        VisionField("content", "50 ml", 0.95),
        VisionField("visible_quantity", "3 cajas apiladas", 0.8),
    ),
)


async def test_foto_identificada_por_vision_real_registra_igual_que_un_texto(
    photo_chat, sessions, seed, tg_api
):
    vision = LiveVision(CREMA)
    c = photo_chat(vision=vision)
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 21))

    (a,) = await attachments(sessions)
    (o,) = await observations(sessions)
    item = await item_of(sessions)
    assert a.status == "identified" and a.extraction_mode == "live"
    assert a.vision_provider == "openai" and a.vision_model == "gpt-vision-test"
    assert a.author_id == seed["ana"] and a.content_sha256 == sha256_hex(fake_jpeg(21))
    assert o.source == "telegram_photo" and o.author_id == seed["ana"]
    assert o.attachment_id == a.id and o.demand_item_id == item.id
    # cantidad predeterminada: una unidad; la cantidad visible en la foto NO se usa
    assert (o.declared_quantity, o.declared_unit) == (1, "unit")
    assert o.interpretation["quantity_origin"] == "default"
    assert item.display_name == "Crema de manos, variante Berry Warm x 50 ml"
    assert item.attributes["variant"] == {
        "value": "Berry Warm",
        "basis": "inferred",
        "origin": "image",
    }
    assert item.target_quantity is None  # el faltante existe y es visible para compras
    reply = tg_api.texts()[-1]
    assert reply.startswith(
        "✅ Anotado: Crema de manos, variante Berry Warm x 50 ml — 1 unidad."
    )
    assert "👤 Reportado por: Ana" in reply
    assert vision.calls == 1  # una sola lectura por foto


async def test_foto_parcialmente_legible_usa_lo_seguro_y_no_inventa_la_dosis(
    photo_chat, sessions, tg_api
):
    partial = VisionExtraction(
        "live",
        "openai",
        "gpt-vision-test",
        "partial",
        (
            VisionField("ingredient", "ibuprofeno", 0.95),
            VisionField("form", "comprimidos", 0.85),
        ),
    )
    c = photo_chat(vision=LiveVision(partial))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 22))

    (a,) = await attachments(sessions)
    (o,) = await observations(sessions)
    item = await item_of(sessions)
    assert a.status == "needs_review" and "image_partially_legible" in a.uncertainties
    assert item.attributes["strengths"] == []  # la dosis no se veía: no se inventa
    assert o.declared_quantity == 1
    reply = tg_api.texts()[-1]
    assert reply.startswith("✅ Anotado: Ibuprofeno comprimido — 1 unidad.")
    assert "Quedó por confirmar lo leído de la foto." in reply
    assert "👤 Reportado por: Ana" in reply


async def test_foto_ilegible_no_crea_nada_y_avisa_sin_presentarla_como_registrada(
    photo_chat, sessions, tg_api
):
    blurry = VisionExtraction(
        "live",
        "openai",
        "gpt-vision-test",
        "blurry",
        (VisionField("ingredient", "algo que parece ibuprofeno", 0.9),),
    )
    c = photo_chat(vision=LiveVision(blurry))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 23))

    (a,) = await attachments(sessions)
    assert a.status == "needs_review" and a.author_id is not None
    assert await observations(sessions) == [] and await count(sessions, DemandItem) == 0
    reply = tg_api.texts()[-1]
    assert "📷 No pude identificar el producto de la fotografía." in reply
    assert "La dejé pendiente de revisión." in reply
    assert "Anotado" not in reply and "👤 Reportado por: Ana" in reply


async def test_fallo_de_openai_conserva_la_foto_y_avisa_sin_reintentar_la_llamada(
    photo_chat, sessions, tg_api
):
    vision = LiveVision(error=VisionFailed("timeout"))
    c = photo_chat(vision=vision)
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 24))

    (a,) = await attachments(sessions)
    assert a.status == "needs_review" and a.error == "vision_failed:timeout"
    assert a.uncertainties == ["vision_failed:timeout"] and a.processed_at is not None
    assert a.storage_key and c.rt.photos.storage.get(a.storage_key) == fake_jpeg(24)
    assert a.author_id is not None and await observations(sessions) == []
    assert await count(sessions, DemandItem) == 0  # nada inventado
    assert vision.calls == 1  # un fallo no dispara otra lectura de la misma foto
    reply = tg_api.texts()[-1]
    assert "No pude identificar el producto de la fotografía." in reply
    assert "La dejé pendiente de revisión." in reply


async def test_foto_del_mismo_producto_ya_anotado_no_duplica_el_faltante(
    photo_chat, sessions, tg_api
):
    c = photo_chat(vision=LiveVision(CREMA))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 25))
    await c.send(photo_update, img(tg_api, 26))  # otra foto del mismo producto

    obs = await observations(sessions)
    assert len(obs) == 2 and obs[1].match_status == "attached_existing"
    assert obs[0].demand_item_id == obs[1].demand_item_id
    assert await count(sessions, DemandItem) == 1
    assert "(ya estaba en tu lista)" in tg_api.texts()[-1]


async def test_foto_de_otro_auxiliar_avisa_quien_lo_reporto_y_no_fusiona(
    photo_chat, sessions, seed, tg_api
):
    c = photo_chat(vision=LiveVision(CREMA))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 27))
    await c.pick("luis")
    await c.send(photo_update, img(tg_api, 28))

    obs = await observations(sessions)
    assert [o.author_id for o in obs] == [seed["ana"], seed["luis"]]
    assert obs[0].demand_item_id != obs[1].demand_item_id  # reportes independientes
    assert await count(sessions, DemandItem) == 2
    reply = tg_api.texts()[-1]
    assert "Ana ya informó este producto." in reply
    assert "Quedó como un reporte tuyo independiente." in reply
    assert "👤 Reportado por: Luis" in reply


# ---------- duplicados, errores y seguridad ----------


async def test_misma_fotografia_reenviada_no_genera_otra_observacion(photo_chat, sessions, tg_api):
    c = photo_chat({"*": MIGRANOL})
    await c.pick("ana")
    await c.send(photo_update, img(c.api, 9, "a1"))
    c.api.files["a2"] = fake_jpeg(9)  # mismos bytes, otro file_id (la reenvió)
    await c.send(photo_update, "a2")
    first, second = await attachments(sessions)
    assert second.duplicate_of_id == first.id and len(await observations(sessions)) == 1
    assert "ya estaba registrada" in tg_api.texts()[-1]
    # el MISMO evento entregado dos veces tampoco duplica
    upd = photo_update(777, CHAT, img(c.api, 10))
    await handle_update(c.rt, upd, now=T0)
    await handle_update(c.rt, upd, now=T0)
    assert await count(sessions, Attachment) == 3


async def test_otro_auxiliar_con_la_misma_foto_suma_aviso_sin_sumar_cantidad(
    photo_chat, sessions, seed
):
    c = photo_chat({"*": MIGRANOL})
    await c.pick("ana")
    await c.send(photo_update, img(c.api, 9, "x1"))
    await c.pick("luis")
    c.api.files["x2"] = fake_jpeg(9)
    await c.send(photo_update, "x2")
    obs = await observations(sessions)
    assert [o.author_id for o in obs] == [seed["ana"], seed["luis"]]
    # Modelo A: la foto de Luis no se adjunta al reporte de Ana; cada persona conserva el suyo.
    assert obs[0].demand_item_id != obs[1].demand_item_id
    async with sessions() as s:
        items = (await s.execute(select(DemandItem))).scalars().all()
    assert len(items) == 2 and all(i.target_quantity is None for i in items)


async def test_distinto_laboratorio_no_se_fusiona_automaticamente(photo_chat, sessions):
    base = [
        ("ingredient", "Ibuprofeno", 0.95),
        ("strength", "400 mg", 0.95),
        ("form", "comprimidos", 0.9),
        ("content", "20 comprimidos", 0.9),
    ]
    a = ext(*base, ("laboratory", "Laboratorio Alfa", 0.9))
    b = ext(*base, ("laboratory", "Laboratorio Beta", 0.9))
    c = photo_chat({sha256_hex(fake_jpeg(1)): a, sha256_hex(fake_jpeg(2)): b})
    await c.pick("ana")
    await c.send(photo_update, img(c.api, 1))
    await c.send(photo_update, img(c.api, 2))
    o1, o2 = await observations(sessions)
    assert (
        o1.match_status == "created_item" and o2.match_status == "needs_review"
    )  # misma identidad farmacéutica, otro laboratorio
    assert (
        "laboratory_differs" in o2.candidates[0]["reasons"]
        and await count(sessions, DemandItem) == 1
    )


async def test_error_de_descarga_se_reintenta_y_luego_se_procesa(photo_chat, sessions, tg_api):
    c = photo_chat({"*": MIGRANOL})
    await c.pick("ana")
    fid = img(tg_api, 5)
    tg_api.method_failures["getFile"] = [
        (500, "Internal Server Error", None)
    ]  # falla getFile una vez
    await c.send(photo_update, fid)
    (a,) = await attachments(sessions)
    assert a.status == "error" and a.attempts == 1 and a.error.startswith("download_failed")
    assert await observations(sessions) == []
    assert await process_pending_attachments(c.rt.sessionmaker, c.rt.photos) == 1
    await after_handle(c.rt, type("R", (), {"status": "processed", "chat_id": CHAT})())
    (a,) = await attachments(sessions)
    assert (
        a.status == "identified"
        and a.attempts == 2
        and a.error is None
        and len(await observations(sessions)) == 1
    )


async def test_archivo_que_no_es_imagen_se_rechaza_y_no_se_guarda(
    photo_chat, sessions, tg_api, tmp_path
):
    c = photo_chat({"*": MIGRANOL})
    await c.pick("ana")
    tg_api.files["mal"] = (
        b"#!/bin/sh\nrm -rf /\n" + b"x" * 100
    )  # firma de bytes no es JPEG/PNG/WebP
    await c.send(photo_update, "mal")
    (a,) = await attachments(sessions)
    assert a.status == "error" and a.error == "UNSUPPORTED_IMAGE" and a.storage_key is None
    assert (
        not (tmp_path / "storage").exists()
        or not list((tmp_path / "storage").rglob("*.*"))
        and await observations(sessions) == []
    )


# ---------- foto sin identidad: pendiente de atribución ----------


async def test_foto_sin_sesion_no_se_procesa_ni_se_atribuye_hasta_confirmar(
    photo_chat, sessions, seed, tg_api
):
    c = photo_chat({"*": MIGRANOL})
    await c.send(photo_update, img(tg_api, 6), now=T0)
    (a,) = await attachments(sessions)
    assert a.author_id is None and a.status == "pending" and a.storage_key is None  # ni descargada
    async with sessions() as s:
        m = (await s.execute(select(ChannelMessage))).scalar_one()
    assert m.attribution_status == "pending_attribution" and await observations(sessions) == []
    await c.pick("luis", now=mins(1))
    assert (await attachments(sessions))[0].author_id is None  # identificarse no la atribuye
    await c.click("att:ok", now=mins(2))
    (a,) = await attachments(sessions)
    assert a.author_id == seed["luis"] and a.status == "identified"
    (o,) = await observations(sessions)
    assert o.author_id == seed["luis"]


async def test_trazabilidad_de_una_observacion_no_se_puede_reescribir(photo_chat, sessions, engine):
    from sqlalchemy.exc import DBAPIError

    c = photo_chat({"*": MIGRANOL})
    await c.pick("ana")
    await c.send(photo_update, img(c.api, 1))
    for col, val in (("attachment_id", "NULL"), ("source", "'api'"), ("actor_session_id", "NULL")):
        with pytest.raises(DBAPIError, match="inmutables"):
            async with engine.begin() as conn:
                await conn.execute(text(f"UPDATE demand_observation SET {col} = {val}"))
    with pytest.raises(DBAPIError, match="no puede reatribuirse"):
        async with engine.begin() as conn:
            await conn.execute(text(f"UPDATE channel_message SET author_id = '{c.seed['luis']}'"))


_unused = (datetime, UTC, callback_update)
