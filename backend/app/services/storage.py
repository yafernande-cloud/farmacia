"""Almacenamiento privado de las imágenes de Telegram.

Desde el 2026-11-08 el binario vive en PostgreSQL (`temp_photos`, columna `BYTEA`), no en el disco
efímero del dyno. La interfaz es asíncrona porque la implementación real comparte la sesión
SQLAlchemy de quien registra la asociación: así el contenido y el vínculo con el reporte se
confirman o se revierten juntos.

`LocalPrivateStorage` se conserva para pruebas aisladas y utilidades locales; **no** es el
almacenamiento de producción. El tipo real de la imagen se valida por firma de bytes, no por lo que
diga el cliente, y la clave es el SHA-256 del contenido: una sola copia física por conjunto de
bytes idénticos.
"""

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Protocol

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.models import TempPhoto

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class PrivateStorage(Protocol):
    async def put(
        self, data: bytes, *, mime_type: str | None = None, session: AsyncSession | None = None
    ) -> str: ...

    async def get(self, key: str, *, session: AsyncSession | None = None) -> bytes: ...


def detect_image_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise DomainError(
        "UNSUPPORTED_IMAGE", "El archivo no es una imagen JPEG, PNG o WebP.", status_code=415
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def storage_key(sha: str) -> str:
    """Misma forma histórica `ab/<sha>`: lo que se guarda en `attachment.storage_key`."""
    return f"{sha[:2]}/{sha}"


def sha_from_key(key: str) -> str:
    sha = key.rsplit("/", 1)[-1].strip().lower()
    if not _SHA_RE.fullmatch(sha):
        raise DomainError(
            "INVALID_STORAGE_KEY", "Clave de almacenamiento inválida.", status_code=400
        )
    return sha


class LocalPrivateStorage:
    """Solo para pruebas/utilidades locales. No sobrevive a un reinicio de Heroku."""

    def __init__(self, root: Path):
        self.root = Path(root)

    async def put(
        self, data: bytes, *, mime_type: str | None = None, session: AsyncSession | None = None
    ) -> str:
        sha = sha256_hex(data)
        key = storage_key(sha)
        path = self.root / key
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            # Nombre temporal único: dos procesos que guardan el mismo SHA no comparten `.tmp`.
            tmp = path.with_name(f".{sha}.{uuid.uuid4().hex}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        return key

    async def get(self, key: str, *, session: AsyncSession | None = None) -> bytes:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:  # sin salirse del directorio
            raise DomainError(
                "INVALID_STORAGE_KEY", "Clave de almacenamiento inválida.", status_code=400
            )
        try:
            return path.read_bytes()
        except OSError as e:
            raise DomainError(
                "PHOTO_NOT_AVAILABLE",
                "La fotografía no está disponible.",
                status_code=404,
            ) from e


class PostgresPrivateStorage:
    """Contenido en `temp_photos` (PostgreSQL). Una fila por SHA-256, compartida si los bytes son iguales.

    `put` acepta la sesión de la transacción que asocia la foto al reporte. Si no se pasa, abre una
    propia con el `sessionmaker` configurado. `get` funciona igual, pero la API puede pasar la sesión
    de la petición y evitar abrir otra conexión.
    """

    def __init__(self, sessionmaker=None):
        self._sessionmaker = sessionmaker

    async def _with_session(self, session: AsyncSession | None):
        if session is not None:
            return session, False
        if self._sessionmaker is None:
            raise RuntimeError("PostgresPrivateStorage necesita una sesión o un sessionmaker.")
        s = self._sessionmaker()
        await s.__aenter__()
        return s, True

    async def put(
        self, data: bytes, *, mime_type: str | None = None, session: AsyncSession | None = None
    ) -> str:
        if mime_type is None:
            raise ValueError("mime_type es obligatorio para guardar una fotografía.")
        sha = sha256_hex(data)
        s, own = await self._with_session(session)
        try:
            await s.execute(
                pg_insert(TempPhoto)
                .values(
                    sha256=sha,
                    content=data,
                    mime_type=mime_type,
                    size_bytes=len(data),
                )
                .on_conflict_do_nothing(index_elements=["sha256"])
            )
            if own:
                await s.commit()
        except BaseException:
            if own:
                await s.rollback()
            raise
        finally:
            if own:
                await s.__aexit__(None, None, None)
        return storage_key(sha)

    async def get(self, key: str, *, session: AsyncSession | None = None) -> bytes:
        sha = sha_from_key(key)
        s, own = await self._with_session(session)
        try:
            row = await s.get(TempPhoto, sha)
            if row is None:
                raise DomainError(
                    "PHOTO_NOT_AVAILABLE",
                    "La fotografía no está disponible.",
                    status_code=404,
                )
            return bytes(row.content)
        finally:
            if own:
                await s.__aexit__(None, None, None)
