"""Procesamiento de fotografías recibidas por el canal (HLD §8).

Estados (`attachment.status`): pending → processing → identified | needs_review | error.
 * pending      : recibida y a nombre de alguien; sin proveedor de visión, o aún sin procesar.
 * identified   : se leyó un producto de forma legible. NO implica identidad completa ni confirmada:
                  lo leído de una imagen siempre queda `inferred` hasta que compras lo confirme.
 * needs_review : borrosa, parcial, sin producto legible, o en conflicto con el texto.
 * error        : falló la descarga/lectura; se reintenta.
`extraction_mode` deja explícito si la lectura fue `live` o `simulated`. `live` solo se usa con un
proveedor real configurado (`VISION_PROVIDER=openai`); `simulated` queda para pruebas/demo y siempre
se etiqueta como tal. Sin proveedor (`none`) la foto se guarda y queda pendiente.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select

from app.core.errors import DomainError
from app.domain.products import ParsedProduct, parse_shortage_text
from app.interpretation.vision import (
    ImageReading,
    VisionFailed,
    VisionInterpreter,
    VisionStatus,
    VisionUnavailable,
    merge_text_and_image,
    read_extraction,
)
from app.models import (
    Attachment,
    ChannelBinding,
    ChannelMessage,
    InboundEvent,
    OutboundMessage,
    TelegramAccount,
)
from app.services.actors import load_actor
from app.services.channel import Reply, _ask_confirmation, _fmt_quantity, enqueue
from app.services.demand import Origin, record_interpreted_observation
from app.services.events import advisory_lock, emit
from app.services.provenance import allow_simulated_vision, org_kind
from app.services.storage import PrivateStorage, detect_image_type, sha256_hex

log = logging.getLogger(__name__)

DUPLICATE_WINDOW = timedelta(hours=24)
MAX_ATTEMPTS = 3


class ImageFetcher(Protocol):
    async def fetch(self, file_id: str) -> bytes: ...


@dataclass
class PhotoRuntime:
    fetcher: ImageFetcher
    storage: PrivateStorage
    vision: VisionInterpreter | None = None  # None = no hay proveedor de visión
    max_bytes: int = 10_000_000


@dataclass
class PhotoOutcome:
    attachment_id: uuid.UUID
    status: str
    observation_outcome: str | None = None
    duplicate: bool = False


def _declared_text(res) -> str:
    obs = res.observation
    if obs is not None and obs.declared_quantity is not None:
        return _fmt_quantity(obs.declared_quantity, obs.declared_unit)
    return "1 unidad"


def _not_identified(name: str, *, simulated: bool = False) -> str:
    """Frase única para una foto que no se pudo leer. Nunca se presenta como registrada."""
    sim = " (lectura simulada, no es una IA real)" if simulated else ""
    return (
        "📷 No pude identificar el producto de la fotografía.\n"
        f"La dejé pendiente de revisión.{sim}\n"
        f"👤 Reportado por: {name}"
    )


def _reply_text(
    name, status, res, parsed: ParsedProduct | None, mode, conflicts
) -> str:
    sim = " (lectura simulada, no es una IA real)" if mode == "simulated" else ""
    foot = f"\n👤 Reportado por: {name}"
    if conflicts:
        return (
            "⚠️ La foto y tu texto no coinciden.\n"
            f"Guardé ambos y quedó para revisión de compras.{sim}{foot}"
        )
    if res is None or parsed is None:
        return _not_identified(name, simulated=mode == "simulated")
    shown = res.item.display_name if res.item else parsed.display_name
    if res.outcome == "needs_review":
        return (
            f"📷 Reconocí {shown}, pero no pude confirmarlo.\n"
            f"Quedó pendiente de revisión.{sim}{foot}"
        )
    if res.outcome == "blocked_in_cart":
        return f"⚠️ {shown} está en carrito: no cambié la cantidad solicitada.{sim}{foot}"
    qty = _declared_text(res)
    if res.outcome == "attached_existing":
        lead = f"✅ Anotado: {shown} — {qty} (ya estaba en tu lista)."
    else:
        lead = f"✅ Anotado: {shown} — {qty}."
    missing: list[str] = []
    if parsed.name_anchor:
        missing.append("el principio activo")
    if parsed.form and not parsed.strengths and not parsed.unassigned:
        missing.append("la dosis")
    if missing:
        lead += f" Falta confirmar {' y '.join(missing)}."
    if status == "needs_review":
        lead += " Quedó por confirmar lo leído de la foto."
    return lead + sim + foot


async def process_attachment(
    sm, attachment_id: uuid.UUID, rt: PhotoRuntime, now: datetime | None = None
) -> PhotoOutcome | None:
    now = now or datetime.now(UTC)
    # ---- 1) reclamar (un solo procesador por foto)
    async with sm() as s, s.begin():
        att = (
            await s.execute(
                select(Attachment)
                .where(Attachment.id == attachment_id, Attachment.status.in_(("pending", "error")),
                       Attachment.author_id.is_not(None), Attachment.attempts < MAX_ATTEMPTS)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()  # fmt: skip
        if att is None:
            return None
        att.status, att.attempts = "processing", att.attempts + 1
        msg = await s.get(ChannelMessage, att.channel_message_id)
        inbound = await s.get(InboundEvent, msg.inbound_event_id)
        account = await s.get(TelegramAccount, msg.account_id) if msg.account_id else None
        info = dict(org=att.organization_id, file_id=att.provider_file_id, caption=att.caption,
                    author=att.author_id, key=att.storage_key, binding=msg.channel_binding_id,
                    session=msg.session_id, msg=msg.id, event=inbound.external_event_id, event_id=inbound.id,
                    account=msg.account_id, branch=account.branch_label if account else None,
                    basis=msg.attribution_basis)  # fmt: skip

    async def fail(status: str, error: str, unc: list[str] | None = None) -> PhotoOutcome:
        async with sm() as s2, s2.begin():
            a = await s2.get(Attachment, attachment_id)
            a.status, a.error, a.uncertainties = status, error, unc or []
        return PhotoOutcome(attachment_id, status)

    async def fail_vision(exc: VisionFailed) -> PhotoOutcome:
        """Fallo de lectura: la foto se conserva y queda para revisión; nunca se inventa nada.

        No se reintenta con otra llamada al proveedor (el adaptador ya reintentó solo lo
        transitorio): así una misma foto no consume cuota repetidamente.

        Los metadatos técnicos (estado HTTP, código del proveedor, request ID) son internos y ya
        vienen saneados por el adaptador: nunca incluyen la clave, la imagen ni el cuerpo.
        """
        code = exc.code
        details = [f"vision_failed:{code}"]
        if exc.http_status is not None:
            details.append(f"vision_http_status:{exc.http_status}")
        if exc.provider_code:
            details.append(f"vision_provider_code:{exc.provider_code}")
        if exc.request_id:
            details.append(f"vision_request_id:{exc.request_id}")
        log.warning(
            "vision_failed photo=%s code=%s http_status=%s provider_code=%s request_id=%s",
            attachment_id,
            code,
            exc.http_status,
            exc.provider_code,
            exc.request_id,
        )
        async with sm() as s2, s2.begin():
            a = await s2.get(Attachment, attachment_id)
            binding = await s2.get(ChannelBinding, info["binding"])
            actor = await load_actor(s2, info["author"])
            name = actor.display_name if actor is not None else "auxiliar"
            a.status, a.error = "needs_review", f"vision_failed:{code}"
            a.uncertainties, a.processed_at = details, now
            if binding is not None:
                n = (
                    await s2.execute(
                        select(func.count())
                        .select_from(OutboundMessage)
                        .where(OutboundMessage.inbound_event_id == info["event_id"])
                    )
                ).scalar_one()
                await enqueue(
                    s2,
                    binding,
                    info["event_id"],
                    [Reply(_not_identified(name))],
                    start_seq=n,
                )
            emit(s2, org_id=info["org"], actor_id=info["author"], action="attachment.vision_failed",
                 entity_type="attachment", entity_id=a.id, event_type="attachment.vision_failed",
                 aggregate_version=None,
                 payload={"code": code, "http_status": exc.http_status,
                          "provider_code": exc.provider_code, "request_id": exc.request_id})  # fmt: skip
        return PhotoOutcome(attachment_id, "needs_review")

    # ---- 2) obtener y guardar la imagen original (privada, tipo verificado por firma de bytes)
    try:
        data = (
            rt.storage.get(info["key"]) if info["key"] else await rt.fetcher.fetch(info["file_id"])
        )
        if len(data) > rt.max_bytes:
            return await fail("error", "image_too_large")
        mime = detect_image_type(data)
    except DomainError as e:
        return await fail("error", e.code)
    except Exception as e:  # noqa: BLE001  descarga fallida: se reintenta
        return await fail("error", f"download_failed:{type(e).__name__}")
    sha = sha256_hex(data)
    key = info["key"] or rt.storage.put(data)

    async with sm() as s, s.begin():
        await advisory_lock(s, f"photo:{info['org']}:{sha}")
        att = await s.get(Attachment, attachment_id)
        att.storage_key, att.content_sha256, att.mime_type = key, sha, mime
        prior = (
            await s.execute(
                select(Attachment).where(
                    Attachment.organization_id == info["org"], Attachment.content_sha256 == sha,
                    Attachment.id != attachment_id, Attachment.author_id == info["author"],
                    Attachment.duplicate_of_id.is_(None), Attachment.status != "error",
                    Attachment.created_at >= now - DUPLICATE_WINDOW,
                ).order_by(Attachment.created_at).limit(1)
            )
        ).scalar_one_or_none()  # fmt: skip
        if prior is not None:  # misma foto reenviada: no genera otra observación
            att.duplicate_of_id, att.status = prior.id, prior.status
            att.processed_at = now
            binding = await s.get(ChannelBinding, info["binding"])
            n = (
                await s.execute(
                    select(func.count())
                    .select_from(OutboundMessage)
                    .where(OutboundMessage.inbound_event_id == info["event_id"])
                )
            ).scalar_one()
            u = await load_actor(s, info["author"])
            await enqueue(
                s,
                binding,
                info["event_id"],
                [
                    Reply(
                        "Esa foto ya estaba registrada; no la anoté otra vez.\n"
                        f"👤 Reportado por: {u.display_name}"
                    )
                ],
                start_seq=n,
            )
            emit(s, org_id=info["org"], actor_id=info["author"], action="photo.duplicate", entity_type="attachment",
                 entity_id=att.id, event_type="photo.duplicate", aggregate_version=None,
                 payload={"duplicate_of": str(prior.id)})  # fmt: skip
            return PhotoOutcome(attachment_id, att.status, duplicate=True)

    # ---- 3) lectura visual (fuera de la transacción: puede tardar)
    if rt.vision is None:
        async with sm() as s, s.begin():
            a = await s.get(Attachment, attachment_id)
            a.status, a.uncertainties = "pending", ["no_vision_provider_connected"]
            a.attempts -= 1  # esperar un proveedor no es un intento fallido
        return PhotoOutcome(attachment_id, "pending")
    try:
        ext = await rt.vision.extract(data, mime, info["caption"])
    except VisionUnavailable:
        async with sm() as s, s.begin():
            a = await s.get(Attachment, attachment_id)
            a.status, a.uncertainties = "pending", ["vision_unavailable"]
            a.attempts -= 1
        return PhotoOutcome(attachment_id, "pending")
    except VisionFailed as e:
        return await fail_vision(e)
    except Exception as e:  # noqa: BLE001  error inesperado del doble/proveedor
        return await fail_vision(VisionFailed(type(e).__name__))

    # ---- 4) mismas reglas que un texto: traducir, combinar con el pie de foto, registrar
    reading: ImageReading = read_extraction(ext)
    text_parsed = None
    unc = list(reading.uncertainties)
    if info["caption"]:
        try:
            text_parsed = parse_shortage_text(info["caption"])
        except DomainError as e:
            if e.code == "PRODUCT_NOT_RECOGNIZED":
                text_parsed = parse_shortage_text(
                    info["caption"], allow_no_product=True
                )  # p. ej. «faltan 2 cajas»
            else:
                unc.append(f"caption_ignored:{e.code}")
    parsed, conflicts = None, []
    if reading.parsed is not None:
        parsed, conflicts = merge_text_and_image(text_parsed, reading.parsed)
    elif text_parsed is not None and text_parsed.ingredient:
        parsed = text_parsed  # la imagen no sirvió pero el texto sí identifica el producto
        unc.append("identified_from_caption_only")

    async with sm() as s, s.begin():
        kind = await org_kind(s, info["org"])
        if ext.mode == "simulated" and not allow_simulated_vision(kind):
            # Guardia: una lectura SIMULADA jamás se escribe fuera de la organización de simulación
            # (si no, contaminaría datos reales). Se rechaza sin crear observación ni necesidad.
            a = await s.get(Attachment, attachment_id)
            a.status, a.attempts = (
                "error",
                MAX_ATTEMPTS,
            )  # no se reintenta: es una configuración equivocada
            a.error = "simulated_vision_not_allowed_outside_simulation_org"
            a.uncertainties = ["simulated_extraction_rejected"]
            emit(s, org_id=info["org"], actor_id=info["author"], action="attachment.simulation_rejected",
                 entity_type="attachment", entity_id=a.id, event_type="attachment.simulation_rejected",
                 aggregate_version=None, payload={"org_kind": kind})  # fmt: skip
            return PhotoOutcome(attachment_id, "error")
        att = await s.get(Attachment, attachment_id)
        binding = await s.get(ChannelBinding, info["binding"])
        actor = await load_actor(s, info["author"])
        res = None
        key = f"tg:{info['binding']}:{info['event']}:photo"
        vision_snapshot = {
            "vision": {
                "mode": ext.mode,
                "provider": ext.provider,
                "model": ext.model,
                "legibility": ext.legibility,
                "uncertainties": reading.uncertainties,
                "ignored": reading.ignored,
            }
        }
        origin = Origin(
            "telegram_photo",
            "account" if info["basis"] == "authorized_account" else "declared",
            info["session"],
            info["msg"],
            attachment_id,
            account_id=info["account"],
            branch_label=info["branch"],
        )
        if parsed is not None and actor is not None:
            res = await record_interpreted_observation(
                s, actor, parsed=parsed, raw_text=info["caption"] or "",
                idempotency_key=key, origin=origin,
                force_review=tuple(f"image_text_conflict:{c}" for c in conflicts),
                ai_derived=ext.mode == "live",  # una lectura real de un modelo es `ai_derived`
                apply_default_quantity=True,  # misma regla D-147 que un texto de Telegram
                confirmation_mode="telegram",  # mismo mecanismo de duplicados que un texto
                extra_interpretation=vision_snapshot,
            )  # fmt: skip
        identified = (
            reading.status is VisionStatus.IDENTIFIED and not conflicts and parsed is not None
        )
        att.status = "identified" if identified else "needs_review"
        att.extraction, att.extraction_mode = ext.to_dict(), ext.mode
        att.extraction_provenance = "ai_derived" if ext.mode == "live" else "simulated"
        att.vision_provider, att.vision_model = ext.provider, ext.model
        att.uncertainties, att.processed_at, att.error = (
            [*unc, *[f"image_text_conflict:{c}" for c in conflicts]],
            now,
            None,
        )
        n = (
            await s.execute(
                select(func.count())
                .select_from(OutboundMessage)
                .where(OutboundMessage.inbound_event_id == info["event_id"])
            )
        ).scalar_one()
        if res is not None and res.outcome == "needs_confirmation":
            msg = await s.get(ChannelMessage, info["msg"])
            pending = {
                "segment": info["caption"] or parsed.display_name,
                "key": key,
                "result": res,
                "origin": origin,
                "basis_level": origin.identity_level,
                "account_id": info["account"],
                "branch_label": info["branch"],
                "extra": {
                    "source": "telegram_photo",
                    "attachment_id": str(attachment_id),
                    "raw_text": info["caption"] or "",
                    "parsed": parsed.to_dict(),
                    "ai_derived": ext.mode == "live",
                    "vision_interpretation": vision_snapshot,
                },
            }
            reply = await _ask_confirmation(
                s, binding, actor, msg, pending,
                info["basis"] == "authorized_account",
            )  # fmt: skip
        else:
            reply = Reply(
                _reply_text(actor.display_name, att.status, res, parsed, ext.mode, conflicts)
            )
        await enqueue(s, binding, info["event_id"], [reply], start_seq=n)  # fmt: skip
        emit(s, org_id=info["org"], actor_id=info["author"], action="attachment.processed", entity_type="attachment",
             entity_id=att.id, event_type="attachment.processed", aggregate_version=None,
             payload={"status": att.status, "mode": ext.mode, "observation": res.outcome if res else None},
             after={"extraction_mode": ext.mode, "simulated": ext.mode == "simulated"})  # fmt: skip
        return PhotoOutcome(attachment_id, att.status, res.outcome if res else None)


async def process_pending_attachments(
    sm, rt: PhotoRuntime, limit: int = 10, now: datetime | None = None
) -> int:
    async with sm() as s:
        ids = (
            await s.execute(
                select(Attachment.id).where(Attachment.status.in_(("pending", "error")),
                                            Attachment.author_id.is_not(None), Attachment.attempts < MAX_ATTEMPTS,
                                            Attachment.duplicate_of_id.is_(None))
                .order_by(Attachment.created_at).limit(limit)
            )
        ).scalars().all()  # fmt: skip
    done = 0
    for i in ids:
        if await process_attachment(sm, i, rt, now):
            done += 1
    return done
