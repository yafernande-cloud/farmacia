import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_database_url_exige_driver_psycopg():
    with pytest.raises(ValidationError):
        Settings(database_url="postgres://u:p@h/db", _env_file=None)


def test_cors_origins_desde_texto():
    s = Settings(cors_origins="https://a.lovable.app, http://localhost:5173", _env_file=None)
    assert s.cors_origins == ["https://a.lovable.app", "http://localhost:5173"]


def test_ejecucion_de_compras_siempre_bloqueada():
    assert Settings(_env_file=None).purchase_execution == "blocked"


def test_cors_origins_desde_variable_de_entorno_con_comas(monkeypatch):
    # Regresión: pydantic-settings intentaba parsear la lista como JSON.
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:8080")
    assert Settings(_env_file=None).cors_origins == [
        "http://localhost:5173",
        "http://localhost:8080",
    ]


def test_auth_demo_prohibida_en_produccion():
    with pytest.raises(ValidationError):
        Settings(auth_mode="demo_header", app_env="production", _env_file=None)
