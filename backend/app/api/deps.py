import uuid
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import DomainError
from app.db.session import get_session
from app.services.actors import Actor, load_actor

Session = Annotated[AsyncSession, Depends(get_session)]


def auth_enabled() -> None:
    if get_settings().auth_mode == "disabled":
        raise DomainError(
            "AUTH_NOT_CONFIGURED", "La autenticación no está configurada.", status_code=503
        )


async def get_actor(
    _auth: Annotated[None, Depends(auth_enabled)],
    session: Session,
    x_demo_user_id: Annotated[str | None, Header(description="Solo AUTH_MODE=demo_header")] = None,
) -> Actor:
    """Identidad del que actúa, resuelta en el servidor. La organización sale de su registro.

    Hoy: AUTH_MODE=demo_header (solo local/demo). Diseñado para reemplazarse por la validación
    de JWT de Supabase Auth (ver docs/AUTH_SUPABASE.md) sin tocar los servicios.
    """
    try:
        uid = uuid.UUID(x_demo_user_id or "")
    except ValueError:
        raise DomainError(
            "UNAUTHENTICATED", "Falta identificar al usuario.", status_code=401
        ) from None
    actor = await load_actor(session, uid)
    if actor is None:
        raise DomainError("UNAUTHENTICATED", "Usuario desconocido o inactivo.", status_code=401)
    return actor


CurrentActor = Annotated[Actor, Depends(get_actor)]
