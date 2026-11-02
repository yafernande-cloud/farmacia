import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppUser


@dataclass(frozen=True)
class Actor:
    """Quién actúa. Siempre derivado del servidor (nunca de datos del cliente)."""

    id: uuid.UUID
    organization_id: uuid.UUID
    display_name: str
    roles: list[str]
    available: bool = True  # disponible para reportar (no estar de vacaciones, con licencia, etc.)
    # Control técnico de la propietaria; NO es un rol ni un perfil funcional (D-130).
    is_superadmin: bool = False
    # NULL = acceso web sin vencimiento. Se evalúa al autenticar, no aquí.
    web_access_expires_at: datetime | None = None


async def load_actor(session: AsyncSession, user_id: uuid.UUID) -> Actor | None:
    u = (
        await session.execute(select(AppUser).where(AppUser.id == user_id, AppUser.active))
    ).scalar_one_or_none()
    if u is None:
        return None
    return Actor(
        u.id,
        u.organization_id,
        u.display_name,
        list(u.roles),
        u.available,
        u.is_superadmin,
        u.web_access_expires_at,
    )
