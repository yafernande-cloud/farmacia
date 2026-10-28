"""Webhook de Telegram. APAGADO si falta el token o el secreto: nada se expone sin configurarlo."""

import hmac

from fastapi import APIRouter, BackgroundTasks, Header, Request

from app.core.config import get_settings
from app.core.errors import DomainError
from app.telegram.handler import TelegramRuntime, after_handle, handle_update
from app.telegram.runtime import build_runtime

router = APIRouter(tags=["telegram"])
MAX_BODY = 1_000_000


def get_runtime(request: Request) -> TelegramRuntime | None:
    app = request.app
    rt = getattr(app.state, "telegram_runtime", None)
    if rt is None:
        rt = build_runtime(get_settings())
        if rt is not None:
            app.state.telegram_runtime = rt
    return rt


@router.post("/integrations/telegram/webhook", include_in_schema=True,
             summary="Webhook de Telegram (solo con TELEGRAM_BOT_TOKEN y TELEGRAM_WEBHOOK_SECRET)")  # fmt: skip
async def telegram_webhook(
    request: Request, background: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):  # fmt: skip
    s = get_settings()
    if s.telegram_webhook_secret is None:
        raise DomainError(
            "TELEGRAM_NOT_CONFIGURED",
            "El webhook de Telegram no está configurado.",
            status_code=503,
        )
    # Origen: Telegram reenvía el secreto que se fijó en setWebhook. Comparación en tiempo constante.
    given = (x_telegram_bot_api_secret_token or "").encode()
    if not hmac.compare_digest(given, s.telegram_webhook_secret.get_secret_value().encode()):
        raise DomainError("FORBIDDEN", "Origen no autorizado.", status_code=403)
    rt = get_runtime(request)
    if rt is None:
        raise DomainError(
            "TELEGRAM_NOT_CONFIGURED", "El bot de Telegram no está configurado.", status_code=503
        )
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        raise DomainError("PAYLOAD_TOO_LARGE", "Solicitud demasiado grande.", status_code=413)
    try:
        update = await request.json()
        if not isinstance(update, dict):
            raise ValueError
    except ValueError:
        raise DomainError("VALIDATION_ERROR", "Cuerpo JSON inválido.", status_code=400) from None
    res = await handle_update(rt, update)
    background.add_task(after_handle, rt, res)  # respuestas y fotos después de confirmar
    return {"ok": True}  # solo confirma recepción técnica
