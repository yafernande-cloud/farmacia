"""Motor y sesiones asíncronas de PostgreSQL. La conexión es perezosa."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class DatabaseNotConfigured(RuntimeError):
    pass


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        settings = settings or get_settings()
        if settings.database_url is None:
            raise DatabaseNotConfigured("DATABASE_URL no está configurada")
        _engine = create_async_engine(
            settings.database_url.get_secret_value(), pool_pre_ping=True, pool_size=5
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def get_session() -> AsyncIterator[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        yield session


async def check_database() -> None:
    """Lanza excepción si la base no responde."""
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = _sessionmaker = None


@asynccontextmanager
async def transaction(session: AsyncSession):
    """Commit explícito antes de responder (no depende del cierre de dependencias)."""
    try:
        yield
        await session.commit()
    except BaseException:
        await session.rollback()
        raise


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker
