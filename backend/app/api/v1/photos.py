"""Mantenimiento administrativo de las fotografías guardadas en PostgreSQL.

No forma parte del flujo normal de Faltantes: la baja dirigida ocurre sola al cancelar un reporte.
Estos endpoints existen para operar y auditar el ciclo de vida (barrido de huérfanos y recuperación
manual desde Telegram), siempre con `admin` y `StrongActor`.

La recuperación está apagada por defecto (`PHOTO_RECOVERY_ENABLED=false`): el endpoint se niega a
descargar nada real hasta que la propietaria lo habilite explícitamente.
"""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Session, StrongActor
from app.api.v1.demand import ERRORS
from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.permissions import require_admin
from app.db.session import transaction
from app.services.photo_gc import (
    collect_unreferenced,
    public_gc,
    public_recovery,
    recover_missing_for_active_reports,
)
from app.telegram.client import TelegramClient

router = APIRouter(tags=["mantenimiento de fotografías"])


class CollectIn(BaseModel):
    # La organización NO viaja en el cuerpo: sale del actor autenticado. Un campo extra se rechaza
    # en vez de ignorarse, para que nadie crea que puede barrer otra organización.
    model_config = ConfigDict(extra="forbid")

    limit: int | None = Field(default=None, ge=1, le=10_000)
    dry_run: bool = True


class RecoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=5, ge=1, le=100)
    dry_run: bool = True


@router.post(
    "/photos/maintenance/collect",
    responses=ERRORS,
    summary="Liberar binarios sin ningún reporte u operación viva (solo admin, su organización)",
)
async def collect_photos(body: CollectIn, session: Session, actor: StrongActor):
    """Barrido seguro y acotado: solo fotos de la organización del actor.

    Vuelve a comprobar las referencias de forma global antes de borrar (una imagen compartida por
    SHA-256 se conserva si otra organización la necesita) y nunca cierra reportes.
    """
    require_admin(actor.roles)
    async with transaction(session):
        result = await collect_unreferenced(
            session,
            organization_id=actor.organization_id,
            limit=body.limit,
            dry_run=body.dry_run,
        )
    return public_gc(result)


@router.post(
    "/photos/maintenance/recover",
    responses=ERRORS,
    summary="Recuperar desde Telegram fotos perdidas de reportes activos (solo admin)",
)
async def recover_photos(body: RecoverIn, session: Session, actor: StrongActor):
    """Solo fotos de reportes activos, verificadas por SHA-256. No crea reportes ni cambia datos."""
    require_admin(actor.roles)
    settings = get_settings()
    if not settings.photo_recovery_enabled:
        raise DomainError(
            "RECOVERY_DISABLED",
            "La recuperación de fotografías desde Telegram está desactivada en este entorno.",
            status_code=403,
        )
    if settings.telegram_bot_token is None:
        raise DomainError(
            "TELEGRAM_NOT_CONFIGURED",
            "El canal de Telegram no está configurado.",
            status_code=503,
        )
    client = TelegramClient(
        settings.telegram_bot_token.get_secret_value(), settings.telegram_api_base
    )
    try:
        async with transaction(session):
            result = await recover_missing_for_active_reports(
                session,
                client,
                organization_id=actor.organization_id,
                limit=body.limit,
                max_bytes=settings.max_image_bytes,
                dry_run=body.dry_run,
            )
    finally:
        await client.aclose()
    return public_recovery(result)
