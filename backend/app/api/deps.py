import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.supabase_auth import JwtClaims, verify_supabase_jwt, verify_supabase_jwt_claims
from app.db.session import get_session
from app.models import AppUser
from app.services.actors import Actor, load_actor
from app.services.provenance import OrgKind, org_kind

Session = Annotated[AsyncSession, Depends(get_session)]


def auth_enabled() -> None:
    if get_settings().auth_mode == "disabled":
        raise DomainError(
            "AUTH_NOT_CONFIGURED", "La autenticación no está configurada.", status_code=503
        )


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def _actor_from_jwt(session: AsyncSession, authorization: str | None) -> Actor:
    token = _bearer(authorization)
    if token is None:
        raise DomainError("UNAUTHENTICATED", "Falta la sesión del usuario.", status_code=401)
    subject = await verify_supabase_jwt(token, get_settings())
    user = (
        await session.execute(
            select(AppUser).where(AppUser.auth_subject == subject, AppUser.active)
        )
    ).scalar_one_or_none()
    if user is None:
        raise DomainError(
            "USER_NOT_PROVISIONED",
            "La cuenta autenticada no está habilitada en esta farmacia.",
            status_code=403,
        )
    if user.web_access_expires_at is not None and user.web_access_expires_at <= datetime.now(UTC):
        raise DomainError(
            "WEB_ACCESS_EXPIRED",
            "Tu acceso web venció. Pide a una administradora que lo renueve.",
            status_code=403,
        )
    actor = await load_actor(session, user.id)
    if actor is None:  # cambió entre las dos consultas
        raise DomainError("UNAUTHENTICATED", "Usuario desconocido o inactivo.", status_code=401)
    return actor


async def get_actor(
    _auth: Annotated[None, Depends(auth_enabled)],
    session: Session,
    authorization: Annotated[
        str | None, Header(description="Bearer <JWT de Supabase> (AUTH_MODE=supabase_jwt)")
    ] = None,
    x_demo_user_id: Annotated[str | None, Header(description="Solo AUTH_MODE=demo_header")] = None,
) -> Actor:
    """Identidad del que actúa, resuelta en el servidor. La organización sale de su registro."""
    settings = get_settings()
    if settings.auth_mode == "supabase_jwt":
        return await _actor_from_jwt(session, authorization)

    # Modo de demostración: solo local/demo (validado al arrancar). Nunca se usa si el modo es JWT.
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


async def get_identity_claims(
    _auth: Annotated[None, Depends(auth_enabled)],
    authorization: Annotated[
        str | None, Header(description="Bearer <JWT de Supabase> (AUTH_MODE=supabase_jwt)")
    ] = None,
    x_demo_subject: Annotated[
        str | None, Header(description="Solo AUTH_MODE=demo_header: `sub` ficticio")
    ] = None,
    x_demo_email: Annotated[
        str | None, Header(description="Solo AUTH_MODE=demo_header: correo ficticio")
    ] = None,
    x_demo_name: Annotated[
        str | None, Header(description="Solo AUTH_MODE=demo_header: nombre visible ficticio")
    ] = None,
) -> JwtClaims:
    """Identidad verificada SIN exigir `app_user`: la usa quien pide acceso por primera vez.

    Con JWT real se exige correo verificado; el cuerpo de la petición nunca aporta identidad ni
    organización. En modo de demostración se aceptan cabeceras `X-Demo-*` (solo local/demo).
    """
    settings = get_settings()
    if settings.auth_mode == "supabase_jwt":
        token = _bearer(authorization)
        if token is None:
            raise DomainError("UNAUTHENTICATED", "Falta la sesión del usuario.", status_code=401)
        claims = await verify_supabase_jwt_claims(token, settings)
        if claims.email is None:
            raise DomainError(
                "EMAIL_REQUIRED",
                "La cuenta no informa un correo. Inicia sesión con Google.",
                status_code=403,
            )
        if not claims.email_verified:
            raise DomainError(
                "EMAIL_NOT_VERIFIED", "El correo de la cuenta no está verificado.", status_code=403
            )
        return claims

    subject = (x_demo_subject or "").strip()
    email = (x_demo_email or "").strip()
    if not subject or "@" not in email:
        raise DomainError(
            "UNAUTHENTICATED",
            "Falta identificar a la cuenta que solicita acceso.",
            status_code=401,
        )
    name = (x_demo_name or "").strip() or email.split("@", 1)[0]
    return JwtClaims(subject=subject, email=email, email_verified=True, display_name=name)


IdentityClaims = Annotated[JwtClaims, Depends(get_identity_claims)]


async def require_strong_admin_authentication(session: AsyncSession, actor: Actor) -> None:
    """Las operaciones administrativas sobre datos reales no aceptan la cabecera de demostración.

    `AUTH_MODE=demo_header` sigue sirviendo para desarrollo y para la organización de simulación,
    pero Pao (real_test/producción) debe entrar con un JWT de Supabase validado en el servidor.
    """
    if get_settings().auth_mode != "demo_header":
        return
    if await org_kind(session, actor.organization_id) == OrgKind.SIMULATION:
        return
    raise DomainError(
        "STRONG_AUTH_REQUIRED",
        "Administrar accesos de una organización real exige iniciar sesión con Supabase Auth "
        "(AUTH_MODE=supabase_jwt). La cabecera de demostración no está permitida.",
        status_code=403,
    )


CurrentActor = Annotated[Actor, Depends(get_actor)]


async def get_strong_actor(actor: CurrentActor, session: Session) -> Actor:
    await require_strong_admin_authentication(session, actor)
    return actor


StrongActor = Annotated[Actor, Depends(get_strong_actor)]
