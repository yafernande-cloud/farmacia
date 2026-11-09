"""H1-B: fotografías por Telegram, sin texto o con texto (PostgreSQL real).

IMPORTANTE — qué se verifica y qué NO:
  * Telegram: SIMULADO (tests/tgsim.py). No se probó contra los servidores reales.
  * Visión: dobles controlados. La mayoría usa `SimulatedVisionInterpreter` (resultados escritos a
    mano, `mode="simulated"`); el flujo `live` usa `LiveVision`, que devuelve una extracción como la
    de OpenAI sin llamar al proveedor. El adaptador HTTP real se prueba aparte con MockTransport.
    Ninguna prueba consume cuota ni demuestra que una IA lea una imagen real. La foto real de
    Migranol aportada por el usuario NO fue procesada por ningún modelo; su contenido se transcribió
    a mano en `MIGRANOL`.
"""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.db.session import get_session
from app.interpretation.vision import (
    SimulatedVisionInterpreter,
    VisionExtraction,
    VisionFailed,
    VisionField,
)
from app.main import create_app
from app.models import Attachment, ChannelMessage, DemandItem, OutboxEvent, TempPhoto
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
    photo_chat, sessions, seed, tg_api
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
    async with sessions() as s:
        stored = await s.get(TempPhoto, a.content_sha256)
    assert stored is not None and stored.content == fake_jpeg(1) and stored.mime_type == "image/jpeg"
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
    assert a.storage_key and await c.rt.photos.storage.get(a.storage_key) == fake_jpeg(24)
    assert a.author_id is not None and await observations(sessions) == []
    assert await count(sessions, DemandItem) == 0  # nada inventado
    assert vision.calls == 1  # un fallo no dispara otra lectura de la misma foto
    reply = tg_api.texts()[-1]
    assert "No pude identificar el producto de la fotografía." in reply
    assert "La dejé pendiente de revisión." in reply


async def test_fallo_http_de_openai_guarda_metadatos_internos_y_mantiene_el_mensaje(
    photo_chat, sessions, tg_api
):
    """El auxiliar ve el mismo mensaje simple; el diagnóstico queda solo en registros internos."""
    failure = VisionFailed(
        "http_404",
        http_status=404,
        provider_code="model_not_found",
        request_id="req_abc123",
    )
    vision = LiveVision(error=failure)
    c = photo_chat(vision=vision)
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 31))

    (a,) = await attachments(sessions)
    assert a.status == "needs_review"
    assert a.error == "vision_failed:http_404"
    assert a.uncertainties == [
        "vision_failed:http_404",
        "vision_http_status:404",
        "vision_provider_code:model_not_found",
        "vision_request_id:req_abc123",
    ]
    async with sessions() as s:
        payload = (
            await s.execute(
                select(OutboxEvent.payload).where(OutboxEvent.type == "attachment.vision_failed")
            )
        ).scalar_one()
    assert payload == {
        "code": "http_404",
        "http_status": 404,
        "provider_code": "model_not_found",
        "request_id": "req_abc123",
        "detail": None,
    }
    assert await observations(sessions) == []
    reply = tg_api.texts()[-1]
    assert "No pude identificar el producto de la fotografía." in reply
    assert "404" not in reply and "model_not_found" not in reply and "req_abc123" not in reply


async def test_respuesta_200_invalida_guarda_la_rama_del_analisis(photo_chat, sessions, tg_api):
    """HTTP 200 que no se puede interpretar: se guarda qué rama falló, nunca el texto leído."""
    failure = VisionFailed(
        "invalid_response",
        http_status=200,
        request_id="req_babb87",
        detail="output_not_json",
    )
    c = photo_chat(vision=LiveVision(error=failure))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 32))

    (a,) = await attachments(sessions)
    assert a.status == "needs_review"
    assert a.error == "vision_failed:invalid_response"
    assert a.uncertainties == [
        "vision_failed:invalid_response",
        "vision_http_status:200",
        "vision_request_id:req_babb87",
        "vision_detail:output_not_json",
    ]
    async with sessions() as s:
        payload = (
            await s.execute(
                select(OutboxEvent.payload).where(OutboxEvent.type == "attachment.vision_failed")
            )
        ).scalar_one()
    assert payload["detail"] == "output_not_json" and payload["http_status"] == 200
    assert await observations(sessions) == []
    reply = tg_api.texts()[-1]
    assert "No pude identificar el producto de la fotografía." in reply
    assert "output_not_json" not in reply and "req_babb87" not in reply


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
    reply = tg_api.texts()[-1]
    assert "ya está en tu lista" in reply and "no lo agregué de nuevo" in reply
    assert "Anotado" not in reply


async def test_foto_de_otro_auxiliar_pide_confirmacion_y_crea_reporte_al_confirmar(
    photo_chat, sessions, seed, tg_api
):
    c = photo_chat(vision=LiveVision(CREMA))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 27), now=T0)
    await c.pick("luis", now=mins(1))
    await c.send(photo_update, img(tg_api, 28), now=mins(2))

    # Antes de confirmar NO se crea nada para Luis: se reutiliza el mecanismo de texto.
    assert len(await observations(sessions)) == 1
    assert await count(sessions, DemandItem) == 1
    async with sessions() as s:
        (pending,) = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.confirmation_status == "pending_other_report"
                )
            )
        ).scalars().all()
    reply = tg_api.texts()[-1]
    assert "Ana ya informó" in reply
    assert "¿También quieres reportarlo?" in reply
    assert "reporte tuyo independiente" in reply
    assert "Anotado" not in reply
    assert [b["text"] for b in tg_api.last_buttons()] == ["Sí, agregar mi reporte", "No, cancelar"]

    await c.click(f"rp:ok:{pending.id}", now=mins(3))

    obs = await observations(sessions)
    assert [o.author_id for o in obs] == [seed["ana"], seed["luis"]]
    assert obs[0].demand_item_id != obs[1].demand_item_id  # reportes independientes
    assert await count(sessions, DemandItem) == 2
    luis_obs = obs[1]
    (luis_att,) = [a for a in await attachments(sessions) if a.id == luis_obs.attachment_id]
    assert luis_obs.source == "telegram_photo" and luis_obs.raw_text == ""
    assert luis_obs.match_status == "created_item"
    assert (luis_obs.declared_quantity, luis_obs.declared_unit) == (1, "unit")
    assert luis_att.status == "identified" and luis_att.author_id == seed["luis"]
    assert luis_att.storage_key and luis_att.content_sha256 == sha256_hex(fake_jpeg(28))
    final = tg_api.texts()[-1]
    assert "✅ Anotado: Crema de manos, variante Berry Warm x 50 ml — 1 unidad." in final
    assert "reporte independiente" in final


async def test_foto_de_otro_auxiliar_cancelada_no_crea_reporte_y_conserva_la_foto(
    photo_chat, sessions, seed, tg_api
):
    c = photo_chat(vision=LiveVision(CREMA))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 29), now=T0)
    await c.pick("luis", now=mins(1))
    await c.send(photo_update, img(tg_api, 30), now=mins(2))
    async with sessions() as s:
        (pending,) = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.confirmation_status == "pending_other_report"
                )
            )
        ).scalars().all()

    await c.click(f"rp:cancel:{pending.id}", now=mins(3))

    assert len(await observations(sessions)) == 1  # solo el de Ana
    assert await count(sessions, DemandItem) == 1
    async with sessions() as s:
        m = await s.get(ChannelMessage, pending.id)
    assert m.confirmation_status == "cancelled"
    luis_att = [a for a in await attachments(sessions) if a.author_id == seed["luis"]][0]
    assert luis_att.status == "identified"  # la foto no se pierde ni se registra sola
    assert luis_att.storage_key and luis_att.content_sha256 == sha256_hex(fake_jpeg(30))
    assert "no registré ese aviso" in tg_api.texts()[-1]


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
    await c.send(photo_update, img(c.api, 9, "x1"), now=T0)
    await c.pick("luis", now=mins(1))
    c.api.files["x2"] = fake_jpeg(9)
    await c.send(photo_update, "x2", now=mins(2))
    # La deduplicación de la imagen es por autor: para Luis la foto es nueva, pero el producto
    # ya está reportado por Ana; se pregunta antes de crear su reporte.
    obs = await observations(sessions)
    assert [o.author_id for o in obs] == [seed["ana"]]
    async with sessions() as s:
        (pending,) = (
            await s.execute(
                select(ChannelMessage).where(
                    ChannelMessage.confirmation_status == "pending_other_report"
                )
            )
        ).scalars().all()
    await c.click(f"rp:ok:{pending.id}", now=mins(3))
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
    photo_chat, sessions, tg_api
):
    c = photo_chat({"*": MIGRANOL})
    await c.pick("ana")
    tg_api.files["mal"] = (
        b"#!/bin/sh\nrm -rf /\n" + b"x" * 100
    )  # firma de bytes no es JPEG/PNG/WebP
    await c.send(photo_update, "mal")
    (a,) = await attachments(sessions)
    assert a.status == "error" and a.error == "UNSUPPORTED_IMAGE" and a.storage_key is None
    async with sessions() as s:
        stored = (
            await s.execute(select(func.count()).select_from(TempPhoto))
        ).scalar_one()
    assert stored == 0 and await observations(sessions) == []


# ---------- foto sin identidad: pendiente de atribución ----------


async def test_foto_sin_sesion_se_atribuye_y_procesa_al_elegir_el_nombre(
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
    # Elegir el nombre ES la confirmación: la foto se atribuye y se procesa sola.
    (a,) = await attachments(sessions)
    assert a.author_id == seed["luis"] and a.status == "identified"
    (o,) = await observations(sessions)
    assert o.author_id == seed["luis"]
    reply = "\n".join(tg_api.texts()[-2:])
    assert "La foto quedó a tu nombre" in reply
    assert "¿Los anoto" not in reply


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


# ---------- API: fotografía original del reporte ----------


async def test_api_permite_ver_la_fotografia_original_del_reporte(
    photo_chat, sessions, seed, tg_api, monkeypatch
):
    c = photo_chat(vision=LiveVision(CREMA))
    await c.pick("ana")
    await c.send(photo_update, img(tg_api, 41))
    (att,) = await attachments(sessions)
    assert att.storage_key is not None

    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    app = create_app()

    async def _session():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as api:
            headers = {"X-Demo-User-Id": str(seed["pao"])}
            listing = (await api.get("/api/v1/demand-items", headers=headers)).json()
            (item,) = listing["items"]
            expected = f"/api/v1/attachments/{att.id}/content"
            assert item["photo_url"] == expected  # la tarjeta puede abrir la foto original

            detail = (
                await api.get(f"/api/v1/demand-items/{item['id']}", headers=headers)
            ).json()
            (obs,) = detail["observations"]
            assert obs["attachment_id"] == str(att.id) and obs["photo_url"] == expected

            image = await api.get(expected, headers=headers)
            assert image.status_code == 200 and image.content == fake_jpeg(41)
            assert image.headers["content-type"] == "image/jpeg"
            assert image.headers["cache-control"].startswith("private")

            # La fotografía es privada: otra farmacia no puede leerla.
            other = await api.get(
                expected, headers={"X-Demo-User-Id": str(seed["intruso"])}
            )
            assert other.status_code == 404
            # Y sin identidad no se sirve: nunca es un archivo público.
            assert (await api.get(expected)).status_code == 401
    finally:
        get_settings.cache_clear()


_unused = callback_update
