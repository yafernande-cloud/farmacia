import pytest

from app.db import session as dbsession


async def test_liveness(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["purchase_execution"] == "blocked"


async def test_readiness_sin_base_configurada_es_503_honesto(client):
    r = await client.get("/api/v1/health/ready")
    assert r.status_code == 503
    assert r.json()["database"]["status"] == "not_configured"


async def test_readiness_con_base_inalcanzable_no_filtra_credenciales(client, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:secreto@127.0.0.1:1/x")
    from app.core.config import get_settings

    get_settings.cache_clear()
    await dbsession.dispose_engine()
    r = await client.get("/api/v1/health/ready")
    await dbsession.dispose_engine()
    assert r.status_code == 503
    assert r.json()["database"]["status"] == "unavailable"
    assert "secreto" not in r.text


@pytest.mark.db
async def test_readiness_con_postgres_real(client, monkeypatch, db_url):
    monkeypatch.setenv("DATABASE_URL", db_url)
    from app.core.config import get_settings

    get_settings.cache_clear()
    await dbsession.dispose_engine()
    r = await client.get("/api/v1/health/ready")
    await dbsession.dispose_engine()
    assert r.status_code == 200
    assert r.json()["database"]["status"] == "ok"
