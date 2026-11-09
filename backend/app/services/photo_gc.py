"""Ciclo de vida del binario de las fotografías (PostgreSQL).

Regla intransigente: mientras un faltante siga **activo** en la grilla, su fotografía debe poder
verse, aunque pasen días o semanas. Por eso aquí no hay TTL ni borrado por antigüedad. El binario
solo se libera cuando ya no lo necesita ninguna operación viva:

* un reporte `active` (o una asignación de cotización sobre él),
* una observación todavía pendiente de revisión y sin reporte,
* una confirmación de Telegram pendiente,
* una foto sin autor que aún espera atribución,
* una foto pendiente de procesar o con reintentos disponibles.

Un reporte `cancelled` o `fulfilled` no retiene la foto por sí mismo. Si otro reporte activo usa
exactamente los mismos bytes, la fila física se conserva: la comparten por SHA-256.

La baja se ejecuta **después** del commit que cierra el reporte, en su propia transacción. Tanto la
baja como la escritura toman el mismo advisory lock por SHA, de modo que no se borra un binario que
otra transacción está a punto de asociar, ni se asocia uno que ya fue liberado.

La recuperación desde Telegram repara contenido perdido sin crear reportes ni alterar cantidades,
autores u observaciones: solo vuelve a guardar bytes cuyo SHA-256 coincide con el que ya estaba
registrado. Está desactivada por configuración y no se ejecuta sola.

El barrido manual (`collect_unreferenced`) está **siempre acotado a una organización**: solo
examina binarios que tengan al menos un `attachment` de esa organización. No existe barrido global
por API. La comprobación final de referencias sigue siendo global, de modo que una foto compartida
por SHA-256 con otra organización se conserva si allí sigue activa; lo que no puede ocurrir es que
un administrador vea o libere hashes de fotografías ajenas que nunca tocó su organización.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.models import Attachment, DemandItem, DemandObservation, TempPhoto
from app.services.events import advisory_lock
from app.services.storage import detect_image_type, sha256_hex

MAX_ATTEMPTS = 3

# Una fila `temp_photos` se conserva si CUALQUIER `attachment` con ese SHA está en un estado vivo.
# `duplicate_of_id IS NULL` evita que una copia lógica (reenvío del mismo autor) retenga el binario
# por sí sola: el reporte que la originó es quien decide.
_NEEDED_SQL = text(
    """
    SELECT EXISTS (
      SELECT 1
      FROM attachment a
      WHERE a.content_sha256 = :sha
        AND a.duplicate_of_id IS NULL
        AND (
          EXISTS (
            SELECT 1
            FROM demand_observation o
            JOIN demand_item d ON d.id = o.demand_item_id
            WHERE o.attachment_id = a.id AND d.status = 'active'
          )
          OR EXISTS (
            SELECT 1
            FROM demand_observation o
            WHERE o.attachment_id = a.id
              AND o.demand_item_id IS NULL
              AND o.match_status = 'needs_review'
          )
          OR EXISTS (
            SELECT 1
            FROM channel_message m
            WHERE m.id = a.channel_message_id
              AND m.confirmation_status IN (
                'pending_other_report', 'pending_same_author_quantity', 'awaiting_quantity'
              )
          )
          OR (
            a.author_id IS NULL
            AND NOT EXISTS (
              SELECT 1 FROM demand_observation o WHERE o.attachment_id = a.id
            )
            AND NOT EXISTS (
              SELECT 1
              FROM channel_message m
              WHERE m.id = a.channel_message_id
                AND (
                  m.attribution_status = 'discarded'
                  OR m.confirmation_status IN ('resolved', 'cancelled')
                )
            )
          )
          OR (
            (
              a.status IN ('pending', 'processing', 'needs_review')
              OR (a.status = 'error' AND a.attempts < :max_attempts)
            )
            AND NOT EXISTS (
              SELECT 1 FROM demand_observation o WHERE o.attachment_id = a.id
            )
            AND NOT EXISTS (
              SELECT 1
              FROM channel_message m
              WHERE m.id = a.channel_message_id
                AND (
                  m.attribution_status = 'discarded'
                  OR m.confirmation_status IN ('resolved', 'cancelled')
                )
            )
          )
        )
    )
    """
)


@dataclass
class PhotoGcResult:
    examined: int = 0
    kept: int = 0
    deleted: int = 0
    deleted_sha256: list[str] = field(default_factory=list)


async def photo_is_needed(session: AsyncSession, sha: str) -> bool:
    row = await session.execute(_NEEDED_SQL, {"sha": sha, "max_attempts": MAX_ATTEMPTS})
    return bool(row.scalar())


async def _release_if_unused(session: AsyncSession, sha: str) -> bool:
    """Devuelve True si borró el binario. Debe llamarse dentro de una transacción."""
    await advisory_lock(session, f"photo-content:{sha}")
    if await photo_is_needed(session, sha):
        return False
    result = await session.execute(delete(TempPhoto).where(TempPhoto.sha256 == sha))
    return bool(result.rowcount)


async def collect_unreferenced(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    limit: int | None = None,
    dry_run: bool = False,
) -> PhotoGcResult:
    """Libera binarios sin referencias vivas, **solo entre las fotos de esa organización**.

    El candidato debe estar vinculado a un `attachment` de `organization_id`; las fotos cuya única
    referencia esté en otra organización nunca se examinan ni se devuelven en `deleted_sha256`.
    Antes de borrar se recompueba la necesidad de forma global (`photo_is_needed`), así que una
    imagen compartida por SHA-256 con otra organización se conserva si allí sigue activa.
    """
    org_shas = select(Attachment.content_sha256).where(
        Attachment.organization_id == organization_id,
        Attachment.content_sha256.is_not(None),
    )
    q = (
        select(TempPhoto.sha256)
        .where(TempPhoto.sha256.in_(org_shas))
        .order_by(TempPhoto.created_at, TempPhoto.sha256)
    )
    if limit is not None:
        q = q.limit(limit)
    shas = list((await session.execute(q)).scalars().all())
    result = PhotoGcResult(examined=len(shas))
    for sha in shas:
        if dry_run:
            needed = await photo_is_needed(session, sha)
        else:
            needed = not await _release_if_unused(session, sha)
        if needed:
            result.kept += 1
        else:
            result.deleted += 1
            result.deleted_sha256.append(sha)
    return result


async def release_photos_of_item(
    session: AsyncSession, *, organization_id: uuid.UUID, demand_item_id: uuid.UUID
) -> PhotoGcResult:
    """Baja dirigida tras cerrar un reporte. Se llama después del commit de la cancelación/compra.

    Solo mira las fotos asociadas a observaciones de ese reporte; igual vuelve a comprobar todas
    las referencias, porque otro reporte puede usar los mismos bytes.
    """
    shas = list(
        (
            await session.execute(
                select(Attachment.content_sha256)
                .join(DemandObservation, DemandObservation.attachment_id == Attachment.id)
                .where(
                    DemandObservation.organization_id == organization_id,
                    DemandObservation.demand_item_id == demand_item_id,
                    Attachment.content_sha256.is_not(None),
                )
                .distinct()
            )
        ).scalars()
    )
    result = PhotoGcResult(examined=len(shas))
    for sha in shas:
        if await _release_if_unused(session, sha):
            result.deleted += 1
            result.deleted_sha256.append(sha)
        else:
            result.kept += 1
    return result


@dataclass
class RecoveryFailure:
    attachment_id: uuid.UUID
    code: str
    detail: str


@dataclass
class RecoveryResult:
    candidates: int = 0
    recovered: int = 0
    failed: int = 0
    recovered_sha256: list[str] = field(default_factory=list)
    failures: list[RecoveryFailure] = field(default_factory=list)


async def recover_missing_for_active_reports(
    session: AsyncSession,
    fetcher,
    *,
    organization_id: uuid.UUID | None = None,
    limit: int = 5,
    max_bytes: int = 10_000_000,
    dry_run: bool = False,
) -> RecoveryResult:
    """Vuelve a descargar desde Telegram solo fotos perdidas que un reporte activo aún necesita.

    Verifica el SHA-256 contra `attachment.content_sha256` antes de guardar. No crea reportes,
    observaciones, cantidades ni autores; no toca fotos históricas cerradas.
    """
    active = (
        select(DemandObservation.id)
        .join(DemandItem, DemandItem.id == DemandObservation.demand_item_id)
        .where(
            DemandObservation.attachment_id == Attachment.id,
            DemandItem.status == "active",
        )
        .exists()
    )
    missing = (
        select(Attachment)
        .where(
            Attachment.content_sha256.is_not(None),
            Attachment.provider_file_id.is_not(None),
            Attachment.duplicate_of_id.is_(None),
            active,
            ~select(TempPhoto.sha256)
            .where(TempPhoto.sha256 == Attachment.content_sha256)
            .exists(),
        )
        .order_by(Attachment.created_at)
        .limit(limit)
    )
    if organization_id is not None:
        missing = missing.where(Attachment.organization_id == organization_id)
    rows = list((await session.execute(missing)).scalars().all())
    result = RecoveryResult(candidates=len(rows))
    if dry_run:
        return result

    for att in rows:
        att_id, expected_sha, file_id = att.id, att.content_sha256, att.provider_file_id
        try:
            data = await fetcher.fetch(file_id)
        except Exception as e:  # noqa: BLE001  se informa, no se inventa contenido
            result.failed += 1
            result.failures.append(RecoveryFailure(att_id, "fetch_failed", type(e).__name__))
            continue
        if len(data) > max_bytes:
            result.failed += 1
            result.failures.append(RecoveryFailure(att_id, "too_large", "supera el límite"))
            continue
        sha = sha256_hex(data)
        if sha != expected_sha:
            result.failed += 1
            result.failures.append(
                RecoveryFailure(att_id, "sha_mismatch", "el contenido no coincide con el registrado")
            )
            continue
        try:
            mime = detect_image_type(data)
        except DomainError as e:
            result.failed += 1
            result.failures.append(RecoveryFailure(att_id, e.code, "no es una imagen admitida"))
            continue

        async with session.begin_nested():
            await advisory_lock(session, f"photo-content:{sha}")
            # La fotografía pudo dejar de ser necesaria mientras se descargaba. Se vuelve a
            # comprobar antes de escribir y no se altera ningún reporte.
            if not await photo_is_needed(session, sha):
                continue
            await session.execute(
                pg_insert(TempPhoto)
                .values(
                    sha256=sha,
                    content=data,
                    mime_type=mime,
                    size_bytes=len(data),
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=["sha256"])
            )
        result.recovered += 1
        result.recovered_sha256.append(sha)
    return result


def public_gc(result: PhotoGcResult) -> dict:
    return {
        "examined": result.examined,
        "kept": result.kept,
        "deleted": result.deleted,
        "deleted_sha256": result.deleted_sha256,
    }


def public_recovery(result: RecoveryResult) -> dict:
    return {
        "candidates": result.candidates,
        "recovered": result.recovered,
        "failed": result.failed,
        "recovered_sha256": result.recovered_sha256,
        "failures": [
            {"attachment_id": str(f.attachment_id), "code": f.code, "detail": f.detail}
            for f in result.failures
        ],
    }
