"""Configuración validada desde variables de entorno (.env en local).

Ningún valor secreto tiene default: si falta, la funcionalidad que lo necesita
debe quedar deshabilitada, no inventada.
"""

import re
import uuid
from decimal import Decimal
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
    # X-Demo-User-Id, SOLO local/demo y NUNCA para administrar datos reales. `supabase_jwt`:
    # valida el JWT de Supabase Auth contra su JWKS y resuelve permisos en PostgreSQL.
    auth_mode: Literal["disabled", "demo_header", "supabase_jwt"] = "disabled"
    # JWKS público de Supabase (no es un secreto). Sin estos valores el modo JWT no arranca.
    supabase_jwks_url: str | None = None
    supabase_jwt_issuer: str | None = None
    supabase_jwt_audience: str = "authenticated"
    # Organización que recibe las solicitudes de acceso del bot. Se configura EXPLÍCITAMENTE por
    # entorno (`real_test` ahora, `production` después); no hay valor por defecto.
    telegram_access_organization_id: uuid.UUID | None = None
    # Organización que recibe las solicitudes de acceso WEB. Igual que la de Telegram, se configura
    # EXPLÍCITAMENTE por entorno: nunca sale del cuerpo ni de los claims. Sin valor → 503.
    web_access_organization_id: uuid.UUID | None = None
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
    # `none`: no hay proveedor de visión (la foto se guarda y queda pendiente). `simulated`: solo
    # pruebas/demo, etiquetado. `openai`: lectura real con OpenAI (requiere clave y modelo).
    vision_provider: Literal["none", "simulated", "openai"] = "none"
    vision_simulation_file: str | None = None
    storage_dir: Path = Path(__file__).resolve().parents[3] / ".storage"
    max_image_bytes: int = Field(default=10_000_000, ge=1_000)
    # La recuperación de fotos desde Telegram es una operación manual y explícita. Apagada, el
    # endpoint existe pero se niega: no se descarga nada real sin habilitarla.
    photo_recovery_enabled: bool = False
    # ---- Credenciales de portales de proveedores
    # Clave Fernet (32 bytes en base64 url-safe) para cifrar en reposo la contraseña del portal.
    # Sin clave, guardar una credencial se rechaza: nunca se guarda una contraseña en claro.
    # Generar con: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    supplier_credentials_key: SecretStr | None = None
    timezone: str = "America/Santiago"
    currency: str = "CLP"
    # Una fuente sin observación exitosa en esta ventana se informa `stale` (E4). No inventa frescura.
    source_stale_after_hours: int = Field(default=24, ge=1, le=24 * 365)
    # ---- Búsqueda de precios públicos de cadenas (E15)
    # Habilita cadenas por nombre. Cada conector conserva su propio interruptor y su propia fuente;
    # una cadena listada sin fuente configurada se informa `not_configured` y no inventa resultados
    # (contrato §11.6/§11.7).
    chain_search_enabled: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # ---- Fuente en vivo de Farmacias Ahumada (E15)
    # Apagada por defecto. Solo se activa cuando la operadora declara por escrito que la fuente
    # configurada está autorizada para este uso (convenio, espejo o feed licenciado). Sin ambas
    # variables, E15 informa `not_configured` y NO se hace ninguna solicitud al sitio.
    ahumada_live_search_enabled: bool = False
    # URL de la fuente autorizada que entrega el HTML de resultados (sin barra final necesaria).
    ahumada_live_search_url: str | None = None
    ahumada_live_search_timeout_seconds: int = Field(default=10, ge=1, le=60)
    # ---- Fuente en vivo de Salcobrand (E15)
    # Conector independiente sobre el índice público de Algolia del sitio. Apagado por defecto y
    # sin credenciales en el código: la operadora configura su propia fuente y su propio acceso.
    salcobrand_live_search_enabled: bool = False
    # Endpoint de consultas de Algolia (p. ej. https://<app>-dsn.algolia.net/1/indexes/*/queries).
    salcobrand_live_search_url: str | None = None
    salcobrand_live_search_app_id: str | None = None
    # Clave de búsqueda pública del sitio; se trata como secreto operativo y nunca sale al frontend.
    salcobrand_live_search_api_key: SecretStr | None = None
    salcobrand_live_search_index: str = "sb_variant_production"
    # Origen declarado en la consulta. Es una restricción de la clave del sitio, no una identidad.
    salcobrand_live_search_referer: str = "https://salcobrand.cl/"
    salcobrand_live_search_timeout_seconds: int = Field(default=10, ge=1, le=60)
    # ---- Fuente en vivo de Cruz Verde (E15)
    # Conector independiente sobre la API pública de productos del sitio. Apagado por defecto y sin
    # credenciales en el código: la sesión es anónima y vive solo en memoria. Los endpoints tienen
    # valores oficiales; las variables permiten apuntar a un espejo autorizado sin tocar el código.
    cruzverde_live_search_enabled: bool = False
    cruzverde_live_search_base_url: str | None = None
    cruzverde_live_search_login_url: str | None = None
    cruzverde_live_search_sitemap_url: str | None = None
    cruzverde_live_search_timeout_seconds: int = Field(default=10, ge=1, le=60)
    # Presupuesto para fichas de detalle y enlaces canónicos; al vencer se devuelven los productos
    # ya disponibles sin esperar más (D-115).
    cruzverde_live_search_budget_seconds: float = Field(default=4.0, ge=1, le=30)
    # ---- Interpretación de observaciones comerciales (A6 Fase 3 / O2, contrato §8.5)
    # Interruptor de despliegue: apagado por defecto. Apagado, la observación se guarda y la
    # interpretación queda pendiente; nunca se simula una respuesta real.
    observation_ai_enabled: bool = False
    # Sin `OPENAI_MODEL` la interpretación queda deshabilitada: no hay valor por defecto inventado.
    openai_model: str | None = None
    openai_api_key: SecretStr | None = None
    # Modelo con visión para las fotografías. Se configura aparte porque no todo modelo de texto
    # acepta imágenes; sin `OPENAI_VISION_MODEL` la lectura visual queda deshabilitada.
    openai_vision_model: str | None = None
    # Base de la API (permite un proxy propio). La clave nunca sale del backend.
    openai_base_url: str = "https://api.openai.com/v1"
    # El límite incluye los tokens de razonamiento. La extracción es breve, pero un modelo con
    # razonamiento necesita holgura para no truncar el JSON (ver `openai_reasoning_effort`).
    openai_max_output_tokens: int = Field(default=2500, ge=64, le=8000)
    openai_timeout_seconds: int = Field(default=20, ge=1, le=120)
    # Lectura de una fotografía: salida breve y un tiempo acotado para no retener el webhook.
    openai_vision_timeout_seconds: int = Field(default=20, ge=1, le=60)
    openai_vision_max_output_tokens: int = Field(default=800, ge=64, le=2000)
    openai_vision_max_calls_per_minute: int = Field(default=20, ge=1, le=300)
    # Esfuerzo de razonamiento de los modelos GPT-5.x. `low` basta para extraer una propuesta
    # breve y acotada; `none` lo desactiva. Se envía solo cuando tiene valor.
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] | None = "low"
    openai_prompt_version: str = "a6-observations-v2"
    # Protección básica contra llamadas excesivas; no sustituye un presupuesto (D-142 §8.5).
    openai_max_calls_per_minute: int = Field(default=30, ge=1, le=600)
    # Tolerancia implícita cuando la persona dice «aunque sea un poco más caro», sin porcentaje
    # (contrato §8.7.3–4). Un porcentaje explícito la reemplaza; nunca se suman.
    observation_implicit_tolerance_percent: Decimal = Field(
        default=Decimal("10"), gt=0, le=100
    )

    @field_validator("cors_origins", "chain_search_enabled", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("chain_search_enabled")
    @classmethod
    def _check_chains(cls, v: list[str]):
        permitidas = {"cruz_verde", "salcobrand", "ahumada"}
        desconocidas = sorted(set(v) - permitidas)
        if desconocidas:
            raise ValueError(
                "CHAIN_SEARCH_ENABLED: cadenas desconocidas "
                + ", ".join(desconocidas)
                + " (válidas: cruz_verde, salcobrand, ahumada)"
            )
        return v

    @field_validator("openai_reasoning_effort", mode="before")
    @classmethod
    def _esfuerzo_vacio_es_nulo(cls, v):
        """Permite desactivar el campo con una variable vacía, sin inventar un valor."""
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator(
        "ahumada_live_search_url",
        "salcobrand_live_search_url",
        "salcobrand_live_search_app_id",
        "salcobrand_live_search_api_key",
        "cruzverde_live_search_base_url",
        "cruzverde_live_search_login_url",
        "cruzverde_live_search_sitemap_url",
        mode="before",
    )
    @classmethod
    def _fuente_vacia_es_nula(cls, v):
        """Una variable vacía significa «sin fuente configurada», no un valor inventado."""
        if isinstance(v, str) and not v.strip():
            return None
        return v.strip() if isinstance(v, str) else v

    @field_validator("openai_model", "openai_vision_model", "openai_prompt_version", "openai_base_url")
    @classmethod
    def _texto_openai_no_vacio(cls, v: str | None):
        if v is not None and not v.strip():
            raise ValueError("La configuración de OpenAI no admite valores vacíos.")
        return v.strip() if isinstance(v, str) else v

    @field_validator("database_url", "test_database_url", mode="before")
    @classmethod
    def _normalize_postgres_url(cls, v):
        """Acepta las URL que entregan los proveedores y normaliza el driver.

        Heroku entrega `postgres://`; otros entornos `postgresql://`. SQLAlchemy necesita el
        driver explícito, así que solo se reescribe el esquema: host, credenciales, base y
        parámetros de consulta se conservan tal cual.
        """
        if isinstance(v, SecretStr):
            raw = v.get_secret_value()
        else:
            raw = v
        if not isinstance(raw, str):
            return v
        raw = raw.strip()
        for prefix in ("postgres://", "postgresql://"):
            if raw.startswith(prefix):
                raw = "postgresql+psycopg://" + raw[len(prefix) :]
                break
        return SecretStr(raw) if isinstance(v, SecretStr) else raw

    @field_validator("database_url", "test_database_url")
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
    def _jwt_completo(self):
        if self.auth_mode == "supabase_jwt":
            faltan = [
                nombre
                for nombre, valor in (
                    ("SUPABASE_JWKS_URL", self.supabase_jwks_url),
                    ("SUPABASE_JWT_ISSUER", self.supabase_jwt_issuer),
                )
                if not valor
            ]
            if faltan:
                raise ValueError(
                    "AUTH_MODE=supabase_jwt requiere " + " y ".join(faltan)
                )
        return self

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
