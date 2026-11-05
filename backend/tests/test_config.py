import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.mark.parametrize("esquema", ["postgres://", "postgresql://"])
def test_database_url_de_proveedor_se_normaliza_a_psycopg(esquema):
    # Heroku entrega `postgres://` y otros entornos `postgresql://`: ambos se aceptan y solo se
    # reescribe el esquema; host, credenciales, base y parámetros se conservan.
    s = Settings(database_url=f"{esquema}u:p@h:5432/db?sslmode=require", _env_file=None)
    assert s.database_url.get_secret_value() == (
        "postgresql+psycopg://u:p@h:5432/db?sslmode=require"
    )


def test_database_url_rechaza_otro_driver():
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+asyncpg://u:p@h/db", _env_file=None)


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


def test_fuente_de_cadena_vacia_significa_sin_configurar():
    # Una variable vacía no es una fuente: el conector queda sin configuración y no consulta.
    s = Settings(
        salcobrand_live_search_url="",
        salcobrand_live_search_app_id="   ",
        salcobrand_live_search_api_key="",
        _env_file=None,
    )
    assert s.salcobrand_live_search_enabled is False
    assert s.salcobrand_live_search_url is None
    assert s.salcobrand_live_search_app_id is None
    assert s.salcobrand_live_search_api_key is None
    assert s.salcobrand_live_search_index == "sb_variant_production"
    assert s.salcobrand_live_search_referer == "https://salcobrand.cl/"

