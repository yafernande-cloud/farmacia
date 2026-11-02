"""Validación de los JWT de Supabase Auth contra el JWKS público del proyecto.

* No se llama a Supabase en cada petición: las claves se cachean unos minutos y se refrescan si
  aparece un `kid` desconocido (rotación de claves).
* Se exigen firma asimétrica, `exp`, `iat`, `aud`, `iss`, `sub` y `role=authenticated`.
* Los permisos y la organización NUNCA salen del token: el `sub` se busca en
  `app_user.auth_subject` y de esa fila salen los roles (ver `api/deps.py`).
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from app.core.config import Settings
from app.core.errors import DomainError

JWKS_TTL_SECONDS = 300
JWKS_TIMEOUT_SECONDS = 5
ALLOWED_ALGORITHMS = frozenset({"RS256", "ES256"})
REQUIRED_ROLE = "authenticated"


@dataclass
class _Cache:
    fetched_at: float
    keys: dict[str, PyJWK]


_cache: dict[str, _Cache] = {}
_lock = asyncio.Lock()


def clear_jwks_cache() -> None:
    """Solo para pruebas o para forzar una recarga operativa."""
    _cache.clear()


async def _fetch_jwks(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=JWKS_TIMEOUT_SECONDS) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise ValueError("JWKS inválido")
    return data


async def _keys(url: str, *, force: bool = False) -> dict[str, PyJWK]:
    now = time.monotonic()
    cached = _cache.get(url)
    if not force and cached is not None and now - cached.fetched_at < JWKS_TTL_SECONDS:
        return cached.keys

    async with _lock:
        # Otra petición pudo refrescar mientras esperábamos el candado.
        cached = _cache.get(url)
        now = time.monotonic()
        if not force and cached is not None and now - cached.fetched_at < JWKS_TTL_SECONDS:
            return cached.keys
        try:
            data = await _fetch_jwks(url)
        except Exception as exc:  # noqa: BLE001
            raise DomainError(
                "JWKS_UNAVAILABLE",
                "No se pudo validar la sesión en este momento. Inténtalo más tarde.",
                status_code=503,
            ) from exc
        keys: dict[str, PyJWK] = {}
        for raw in data["keys"]:
            if not isinstance(raw, dict):
                continue
            kid = raw.get("kid")
            if not kid:
                continue
            try:
                keys[str(kid)] = PyJWK(raw)
            except jwt.PyJWTError:
                continue
        if not keys:
            raise DomainError(
                "JWKS_UNAVAILABLE",
                "No se pudo validar la sesión en este momento. Inténtalo más tarde.",
                status_code=503,
            )
        _cache[url] = _Cache(fetched_at=now, keys=keys)
        return keys


def _invalid_token() -> DomainError:
    return DomainError(
        "INVALID_TOKEN", "La sesión no es válida o expiró. Inicia sesión otra vez.", status_code=401
    )


@dataclass(frozen=True)
class JwtClaims:
    """Claims mínimos para gestionar una solicitud de acceso. No son permisos.

    El `sub` es la identidad; `email` y `display_name` solo se guardan como datos informativos de la
    solicitud y del `app_user`. Ningún rol ni organización sale del token.
    """

    subject: str
    email: str | None
    email_verified: bool
    display_name: str | None


async def verify_supabase_jwt_claims(token: str, settings: Settings) -> JwtClaims:
    """Valida el JWT y devuelve los claims mínimos; lanza `DomainError` 401/503 si no es válido."""
    claims = await _decode(token, settings)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise _invalid_token()
    email = claims.get("email")
    email = email.strip() if isinstance(email, str) and email.strip() else None
    metadata = claims.get("user_metadata")
    name = None
    if isinstance(metadata, dict):
        for key in ("full_name", "name"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
    if name is None:
        raw_name = claims.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
    # Supabase puede entregar `email_verified` o `email_confirmed_at`; ambos son verificaciones.
    verified = claims.get("email_verified") is True or bool(claims.get("email_confirmed_at"))
    return JwtClaims(
        subject=subject.strip(), email=email, email_verified=verified, display_name=name
    )


async def verify_supabase_jwt(token: str, settings: Settings) -> str:
    """Devuelve el `sub` verificado o lanza un `DomainError` 401/503. Nunca devuelve claims crudos."""
    return (await verify_supabase_jwt_claims(token, settings)).subject


async def _decode(token: str, settings: Settings) -> dict[str, Any]:
    """Verifica firma, emisor, audiencia, expiración y rol; devuelve los claims ya validados."""
    if not settings.supabase_jwks_url or not settings.supabase_jwt_issuer:
        raise DomainError(
            "AUTH_NOT_CONFIGURED", "La autenticación no está configurada.", status_code=503
        )
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise _invalid_token() from exc
    algorithm = header.get("alg")
    kid = header.get("kid")
    if algorithm not in ALLOWED_ALGORITHMS or not kid:
        raise _invalid_token()

    keys = await _keys(settings.supabase_jwks_url)
    key = keys.get(str(kid))
    if key is None:  # rotación de claves: refrescar una vez
        keys = await _keys(settings.supabase_jwks_url, force=True)
        key = keys.get(str(kid))
    if key is None:
        raise _invalid_token()

    try:
        claims = jwt.decode(
            token,
            key=key.key,
            algorithms=[algorithm],
            audience=settings.supabase_jwt_audience,
            issuer=settings.supabase_jwt_issuer,
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise _invalid_token() from exc

    if claims.get("role") != REQUIRED_ROLE:
        raise _invalid_token()
    return claims
