"""Configuración validada desde variables de entorno (.env en local).

Ningún valor secreto tiene default: si falta, la funcionalidad que lo necesita
debe quedar deshabilitada, no inventada.
"""

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# .env vive en la raíz del repo, sin importar desde dónde se ejecute el backend.
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class AppEnv(StrEnum):
    LOCAL = "local"
    DEMO = "demo"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    app_env: AppEnv = AppEnv.LOCAL
    log_level: str = "INFO"
    # Orígenes permitidos para el frontend (Lovable, localhost). Lista separada por comas.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # PostgreSQL. Formato: postgresql+psycopg://usuario:clave@host:5432/base
    database_url: SecretStr | None = None
    # Base solo para pruebas (se migra y se vacía entre pruebas). Nunca apuntar a datos reales.
    test_database_url: SecretStr | None = None
    # `disabled`: la API de negocio responde 503. `demo_header`: identidad por cabecera
    # X-Demo-User-Id, SOLO local/demo. La validación de JWT de Supabase queda diseñada, no activa.
    auth_mode: Literal["disabled", "demo_header"] = "disabled"
    # ---- Telegram (opcional: sin token/secreto el canal queda apagado; nada se inventa)
    telegram_bot_token: SecretStr | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_api_base: str = "https://api.telegram.org"
    # ---- Sesiones de auxiliar en el teléfono compartido
    session_idle_minutes: int = Field(default=10, ge=1, le=240)
    pending_attribution_max_age_minutes: int = Field(default=15, ge=1, le=240)
    list_page_size: int = Field(default=8, ge=1, le=20)
    session_require_pin: bool = False  # PIN preparado en el servicio; el adaptador aún no lo pide
    # ---- Fotografías
    vision_provider: Literal["none", "simulated"] = "none"  # no existe proveedor real conectado
    vision_simulation_file: str | None = None
    storage_dir: Path = Path(__file__).resolve().parents[3] / ".storage"
    max_image_bytes: int = Field(default=10_000_000, ge=1_000)
    timezone: str = "America/Santiago"
    currency: str = "CLP"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("database_url")
    @classmethod
    def _check_driver(cls, v: SecretStr | None):
        if v is not None and not v.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL debe usar el esquema postgresql+psycopg://")
        return v

    @field_validator("telegram_webhook_secret")
    @classmethod
    def _check_secret(cls, v: SecretStr | None):
        # Telegram solo admite A-Z a-z 0-9 _ - (1 a 256). Exigimos largo suficiente.
        if v is not None and not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", v.get_secret_value()):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET: 16-256 caracteres de [A-Za-z0-9_-]")
        return v

    @model_validator(mode="after")
    def _pin_no_implementado(self):
        if self.session_require_pin:
            raise ValueError(
                "SESSION_REQUIRE_PIN: el PIN por Telegram aún no está implementado en el adaptador"
            )
        return self

    @model_validator(mode="after")
    def _demo_auth_solo_en_entornos_de_prueba(self):
        if self.auth_mode == "demo_header" and self.app_env in (AppEnv.STAGING, AppEnv.PRODUCTION):
            raise ValueError("AUTH_MODE=demo_header no está permitido en staging/production")
        return self

    @property
    def purchase_execution(self) -> str:
        """La ejecución real de compras está bloqueada por diseño (HLD §22).

        No existe variable de entorno que la habilite: requiere una fase propia
        con conectores verificados y autorización expresa.
        """
        return "blocked"


@lru_cache
def get_settings() -> Settings:
    return Settings()
