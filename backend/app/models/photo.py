"""Contenido binario transitorio de las fotografías de Telegram, en PostgreSQL.

La fotografía de un faltante debe seguir disponible mientras el reporte esté activo, aunque el
proceso se reinicie o pasen semanas (decisión del 2026-11-08). El disco efímero del dyno no sirve
para eso, y no se agrega un servicio de objetos: el binario vive en esta tabla normal.

`sha256` es la clave primaria: dos reportes distintos pueden compartir exactamente los mismos bytes
y, entonces, una sola fila física. La relación lógica con cada reporte sigue en `attachment`
(`content_sha256`), de modo que dar de baja un reporte no afecta a otro que use la misma imagen.
La baja del binario la decide `services.photo_gc`, nunca una antigüedad fija.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TempPhoto(Base):
    __tablename__ = "temp_photos"
    __table_args__ = (
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_valido"),
        CheckConstraint("size_bytes >= 0", name="tamano_valido"),
    )

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(50), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
