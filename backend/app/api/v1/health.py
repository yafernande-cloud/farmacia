import logging

from fastapi import APIRouter, Response, status

from app.core.config import get_settings
from app.db.session import DatabaseNotConfigured, check_database

router = APIRouter(tags=["salud"])
log = logging.getLogger(__name__)


@router.get("/health")
async def liveness() -> dict:
    """Liveness: el proceso responde. No toca la base de datos."""
    s = get_settings()
    return {"status": "ok", "env": s.app_env, "purchase_execution": s.purchase_execution}


@router.get("/health/ready")
async def readiness(response: Response) -> dict:
    """Readiness: dependencias críticas. Informa honestamente si la base no responde."""
    try:
        await check_database()
        db = {"status": "ok"}
    except DatabaseNotConfigured:
        db = {"status": "not_configured"}
    except Exception:  # noqa: BLE001 - no filtrar detalles de conexión al cliente
        log.exception("Fallo de readiness en PostgreSQL")
        db = {"status": "unavailable"}
    ok = db["status"] == "ok"
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "degraded", "database": db}
