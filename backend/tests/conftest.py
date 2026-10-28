import os
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import ENV_FILE, Settings, get_settings
from app.main import create_app
from app.models import DemandItem
from app.services.actors import load_actor
from app.services.demand import record_observation
from app.services.quantities import apply_quantity_command

BACKEND = Path(__file__).resolve().parents[1]


def _test_url() -> str | None:
    """TEST_DATABASE_URL desde el entorno o desde el .env local (nunca DATABASE_URL)."""
    if os.getenv("TEST_DATABASE_URL"):
        return os.environ["TEST_DATABASE_URL"]
    s = Settings(_env_file=ENV_FILE)
    return s.test_database_url.get_secret_value() if s.test_database_url else None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # Las pruebas nunca leen el .env real: configuración explícita por prueba.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for var in ("DATABASE_URL", "AUTH_MODE", "CORS_ORIGINS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_ENV", "local")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        yield c


# ---------- PostgreSQL real ----------


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def db_url() -> str:
    """Base de pruebas migrada desde cero con Alembic. Falla/omite si no hay PostgreSQL."""
    url = _test_url()

    def unavailable(msg: str):
        (pytest.fail if os.getenv("REQUIRE_DB") else pytest.skip)(msg)

    if not url:
        unavailable("TEST_DATABASE_URL no definida")
    u = make_url(url)
    assert u.database and u.database.endswith("_test"), "La base de pruebas debe terminar en _test"
    dsn = u.set(drivername="postgresql").render_as_string(hide_password=False)
    try:
        admin = psycopg.connect(
            u.set(drivername="postgresql", database="postgres").render_as_string(
                hide_password=False
            ),
            autocommit=True,
        )
    except psycopg.OperationalError as e:
        unavailable(f"PostgreSQL no disponible: {e}")
    with admin:
        if not admin.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (u.database,)
        ).fetchone():
            admin.execute(f'CREATE DATABASE "{u.database}"')
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture
async def engine(db_url):
    eng = create_async_engine(db_url, poolclass=NullPool)
    async with eng.begin() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT string_agg(quote_ident(tablename), ',') FROM pg_tables "
                    "WHERE schemaname='public' AND tablename <> 'alembic_version'"
                )
            )
        ).scalar()
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest.fixture
def sessions(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def seed(sessions):
    """Organización demo con dos auxiliares, la encargada y un ciclo abierto."""
    from app.models import AppUser, Organization, PurchaseCycle

    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia de prueba", kind="simulation")
        other = Organization(name="Otra farmacia", kind="simulation")
        s.add_all([org, other])
        await s.flush()
        ids["org"], ids["other_org"] = org.id, other.id
        for key, name, roles, o in [
            ("ana", "Ana", ["auxiliary"], org),
            ("luis", "Luis", ["auxiliary"], org),
            ("pao", "Pao", ["purchasing"], org),
            ("intruso", "Intruso", ["purchasing"], other),
        ]:
            u = AppUser(organization_id=o.id, display_name=name, roles=roles)
            s.add(u)
            await s.flush()
            ids[key] = u.id
        s.add(PurchaseCycle(organization_id=org.id, name="Ciclo de prueba"))
        s.add(PurchaseCycle(organization_id=other.id, name="Ciclo otra"))
    return ids


def new_key() -> str:
    return str(uuid.uuid4())


class Ctx:
    """Ejecuta servicios como lo haría la API: sesión y transacción propias por llamada."""

    def __init__(self, sessions, seed):
        self.sessions, self.seed = sessions, seed

    async def run(self, who, fn, *a, **kw):
        """Ejecuta un servicio en su propia sesión/transacción, como lo haría la API."""
        async with self.sessions() as s:
            actor = await load_actor(s, self.seed[who])
            try:
                r = await fn(s, actor, *a, **kw)
                await s.commit()
                return r
            except BaseException:
                await s.rollback()
                raise

    def report(self, who, text, key=None, **kw):
        return self.run(who, record_observation, text=text, idempotency_key=key or new_key(), **kw)

    def qty(self, item_id, op, value=None, unit=None, version=1, key=None, who="pao"):
        return self.run(who, apply_quantity_command, item_id, operation=op, value=value, unit=unit,
                        reason="test", idempotency_key=key or new_key(), expected_version=version)  # fmt: skip

    async def items(self):
        async with self.sessions() as s:
            return (await s.execute(select(DemandItem))).scalars().all()

    async def count(self, model):
        async with self.sessions() as s:
            return (await s.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.fixture
def ctx(sessions, seed):
    return Ctx(sessions, seed)


# ---------- Telegram (SIMULADO) ----------
CHAT = "5550001"


@pytest.fixture
async def chan(sessions, seed):
    """Chat autorizado de la farmacia de prueba (organización `seed['org']`)."""
    from app.models import ChannelBinding

    async with sessions() as s, s.begin():
        b = ChannelBinding(organization_id=seed["org"], external_chat_id=CHAT)
        s.add(b)
        await s.flush()
        bid = b.id
    return {"binding_id": bid, "chat": CHAT}


@pytest.fixture
def tg_api():
    from tests.tgsim import FakeTelegramAPI

    return FakeTelegramAPI()


@pytest.fixture
def make_rt(sessions, tg_api, tmp_path):
    """Fábrica de runtime de Telegram con cliente SIMULADO. `vision`: None o un SimulatedVisionInterpreter."""
    from datetime import timedelta

    from app.services.channel import ChannelConfig
    from app.services.photo_processing import PhotoRuntime
    from app.services.storage import LocalPrivateStorage
    from app.telegram.client import TelegramClient
    from app.telegram.handler import TelegramRuntime
    from tests.tgsim import TOKEN

    def build(vision=None, idle=10, pending_age=15, ai=None):
        client = TelegramClient(TOKEN, "http://tg.test", transport=tg_api.transport)
        cfg = ChannelConfig(idle=timedelta(minutes=idle), pending_max_age=timedelta(minutes=pending_age),
                            page_size=3, vision_enabled=vision is not None, ai=ai)  # fmt: skip
        return TelegramRuntime(
            sessions,
            client,
            cfg,
            PhotoRuntime(client, LocalPrivateStorage(tmp_path / "storage"), vision),
        )

    return build


# ---------- organización de PRUEBA REAL (para probar aislamiento frente a la simulación) ----------
REAL_CHAT = "6358481229"


@pytest.fixture
async def real_seed(sessions):
    """Organización `real_test` con su personal, su ciclo y su chat real. Distinta de `seed` (simulación)."""
    from app.models import AppUser, ChannelBinding, Organization, PurchaseCycle

    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Prueba real (tests)", kind="real_test")
        s.add(org)
        await s.flush()
        ids["org"] = org.id
        for key, name, roles in [
            ("pao", "Pao", ["purchasing"]),
            ("ana", "Aux Uno", ["auxiliary"]),
            ("luis", "Aux Dos", ["auxiliary"]),
        ]:
            u = AppUser(organization_id=org.id, display_name=name, roles=roles)
            s.add(u)
            await s.flush()
            ids[key] = u.id
        s.add(PurchaseCycle(organization_id=org.id, name="Ciclo real"))
        b = ChannelBinding(organization_id=org.id, external_chat_id=REAL_CHAT)
        s.add(b)
        await s.flush()
        ids["binding"] = b.id
    return ids
