"""Construcción del runtime de Telegram desde la configuración (sin credenciales → canal apagado)."""

from datetime import timedelta

from app.core.config import Settings
from app.db.session import get_sessionmaker
from app.interpretation.vision import VisionInterpreter, load_simulation
from app.services.channel import ChannelConfig
from app.services.photo_processing import PhotoRuntime
from app.services.storage import LocalPrivateStorage
from app.telegram.client import TelegramClient
from app.telegram.handler import TelegramRuntime


def vision_from_settings(s: Settings) -> VisionInterpreter | None:
    if s.vision_provider == "simulated":
        if not s.vision_simulation_file:
            raise RuntimeError("VISION_PROVIDER=simulated requiere VISION_SIMULATION_FILE")
        return load_simulation(s.vision_simulation_file)
    return None  # no existe un proveedor de visión real conectado


def build_runtime(s: Settings, *, client: TelegramClient | None = None, vision: VisionInterpreter | None = None,
                  sessionmaker=None) -> TelegramRuntime | None:  # fmt: skip
    if client is None:
        if s.telegram_bot_token is None:
            return None
        client = TelegramClient(s.telegram_bot_token.get_secret_value(), s.telegram_api_base)
    vision = vision if vision is not None else vision_from_settings(s)
    cfg = ChannelConfig(
        idle=timedelta(minutes=s.session_idle_minutes),
        pending_max_age=timedelta(minutes=s.pending_attribution_max_age_minutes),
        page_size=s.list_page_size, vision_enabled=vision is not None,
        access_org_id=s.telegram_access_organization_id,
    )  # fmt: skip
    photos = PhotoRuntime(client, LocalPrivateStorage(s.storage_dir), vision, s.max_image_bytes)
    return TelegramRuntime(sessionmaker or get_sessionmaker(), client, cfg, photos)
