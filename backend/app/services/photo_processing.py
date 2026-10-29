"""Procesamiento de fotografías recibidas por el canal (HLD §8).

Estados (`attachment.status`): pending → processing → identified | needs_review | error.
 * pending      : recibida y a nombre de alguien; sin proveedor de visión, o aún sin procesar.
 * identified   : se leyó un producto de forma legible. NO implica identidad completa ni confirmada:
                  lo leído de una imagen siempre queda `inferred` hasta que compras lo confirme.
 * needs_review : borrosa, parcial, sin producto legible, o en conflicto con el texto.
 * error        : falló la descarga/lectura; se reintenta.
`extraction_mode` deja explícito si la lectura fue `live` o `simulated`. HOY NO existe proveedor
real: solo `none` (se guarda y queda pendiente) o `simulated` (demo/pruebas, etiquetado).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select

from app.core.errors import DomainError
from app.domain.products import ParsedProduct, parse_shortage_text
from app.interpretation.vision import (
    ImageReading,
    VisionInterpreter,
    VisionStatus,
    VisionUnavailable,
    merge_text_and_image,
    read_extraction,
)
from app.models import Attachment, ChannelBinding, ChannelMessage, InboundEvent, OutboundMessage
from app.services.actors import load_actor
from app.services.channel import Reply, enqueue
from app.services.demand import Origin, record_interpreted_observation
from app.services.events import advisory_lock, emit
from app.services.provenance import allow_simulated_vision, org_kind
from app.services.storage import PrivateStorage, detect_image_type, sha256_hex

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


def _reply_text(name, status, res, parsed: ParsedProduct | None, mode, conflicts, qty_note) -> str:
    sim = " (lectura simulada: no es una IA real)" if mode == "simulated" else ""
    foot = f"\n— Reportando como {name}"
    if conflicts:
        return (
            "La foto y tu texto no coinciden; guardé ambos y quedó para revisión de compras."
            + sim
            + foot
        )
    if res is None or parsed is None:
        return (
            "Guardé la foto, pero no pude identificar el producto con seguridad; queda para revisión de compras."
            + sim
            + foot
        )
    if res.outcome == "needs_review":
        return (
            "Anoté el faltante de la foto, pero no puedo asegurar cuál producto es; queda para revisión de compras."
            + qty_note
            + sim
            + foot
        )
    shown = res.item.display_name if res.item else parsed.display_name
    lab = f" ({parsed.laboratory.value})" if parsed.laboratory else ""
    missing = []
    if parsed.name_anchor:
        missing.append("el principio activo")
    if not parsed.strengths and not parsed.unassigned:
        missing.append("la dosis")
    tail = (f" No pude ver {' ni '.join(missing)}, así que queda por confirmar."
            if missing else " Compras confirmará los datos leídos de la foto.")  # fmt: skip
    verb = (
        "Ya estaba en la lista; sumé tu aviso"
        if res.outcome == "attached_existing"
        else "Anoté el faltante"
    )
    return f"{verb}: {shown}{lab}.{tail}{qty_note}{sim}{foot}"


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
        info = dict(org=att.organization_id, file_id=att.provider_file_id, caption=att.caption,
                    author=att.author_id, key=att.storage_key, binding=msg.channel_binding_id,
                    session=msg.session_id, msg=msg.id, event=inbound.external_event_id, event_id=inbound.id,
                    account=msg.account_id, basis=msg.attribution_basis)  # fmt: skip

    async def fail(status: str, error: str, unc: list[str] | None = None) -> PhotoOutcome:
        async with sm() as s2, s2.begin():
            a = await s2.get(Attachment, attachment_id)
            a.status, a.error, a.uncertainties = status, error, unc or []
        return PhotoOutcome(attachment_id, status)

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
                        f"Esa foto ya estaba registrada; no la anoté otra vez.\n— Reportando como {u.display_name}"
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
    except Exception as e:  # noqa: BLE001
        return await fail("error", f"vision_failed:{type(e).__name__}")

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
        qty_note = ""
        if parsed is not None and actor is not None:
            res = await record_interpreted_observation(
                s, actor, parsed=parsed, raw_text=info["caption"] or "",
                idempotency_key=f"tg:{info['binding']}:{info['event']}:photo",
                origin=Origin("telegram_photo",
                              "account" if info["basis"] == "authorized_account" else "declared",
                              info["session"], info["msg"], attachment_id, account_id=info["account"]),
                force_review=tuple(f"image_text_conflict:{c}" for c in conflicts),
                ai_derived=ext.mode == "live",  # una lectura real de un modelo es `ai_derived`
                extra_interpretation={"vision": {"mode": ext.mode, "provider": ext.provider, "model": ext.model,
                                                 "legibility": ext.legibility, "uncertainties": reading.uncertainties,
                                                 "ignored": reading.ignored}},
            )  # fmt: skip
            if parsed.requested_quantity is not None:
                q = parsed.requested_quantity
                qty_note = f" Anoté {q.normalize():f} como pedido sugerido (no cambia la cantidad)."
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
        await enqueue(s, binding, info["event_id"],
                      [Reply(_reply_text(actor.display_name, att.status, res, parsed, ext.mode, conflicts, qty_note))],
                      start_seq=n)  # fmt: skip
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
