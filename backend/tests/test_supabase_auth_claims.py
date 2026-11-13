"""Evidencia de correo verificado en el JWT de Supabase (D-158).

El access token de una cuenta Google no trae `email_verified` ni `email_confirmed_at` de nivel
superior: trae `app_metadata.provider = "google"`, firmado por GoTrue. Estas pruebas fijan la
interpretación:

  * los claims de nivel superior, si existen, se respetan (hooks de access token);
  * el proveedor Google firmado en `app_metadata` cuenta como evidencia de correo verificado;
  * `user_metadata` es editable por la persona y **nunca** cuenta como prueba, aunque diga
    `email_verified: true`;
  * los proveedores que no avalan el correo (correo + contraseña) siguen exigiendo evidencia.

La inferencia del proveedor no es la confirmación en sí: depende de la configuración por defecto de
Supabase, que rechaza el inicio cuando el proveedor informa el correo sin verificar. Esa es la
razón por la que solo se listan proveedores OAuth que avala el correo, nunca el proveedor `email`.
"""

import base64
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK

from app.core import supabase_auth
from app.core.config import Settings
from app.core.supabase_auth import (
    JwtClaims,
    email_verified_from_claims,
    verify_supabase_jwt_claims,
)

ISSUER = "https://example.supabase.co/auth/v1"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
KID = "test-kid"


# ---------------------------------------------------------------- interpretación de claims


def test_google_firmado_es_evidencia_de_correo_verificado():
    claims = {
        "email": "nueva@example.test",
        "app_metadata": {"provider": "google", "providers": ["google"]},
        "user_metadata": {"full_name": "Nueva Persona"},
    }
    assert email_verified_from_claims(claims) is True


def test_google_no_depende_de_que_exista_user_metadata():
    assert (
        email_verified_from_claims({"app_metadata": {"provider": "google"}, "user_metadata": {}})
        is True
    )


def test_correo_y_contrasena_sin_claims_no_tiene_evidencia():
    claims = {
        "email": "persona@example.test",
        "app_metadata": {"provider": "email", "providers": ["email"]},
        "user_metadata": {},
    }
    assert email_verified_from_claims(claims) is False


def test_user_metadata_editable_nunca_cuenta_como_prueba():
    # Una persona con sesión puede editar `user_metadata`; no sirve para demostrar verificación.
    claims = {
        "app_metadata": {"provider": "email"},
        "user_metadata": {"email_verified": True, "email_confirmed_at": "2026-01-01T00:00:00Z"},
    }
    assert email_verified_from_claims(claims) is False


def test_un_proveedor_en_user_metadata_no_suplanta_app_metadata():
    claims = {
        "user_metadata": {"provider": "google", "email_verified": True},
        "app_metadata": {"provider": "email"},
    }
    assert email_verified_from_claims(claims) is False


@pytest.mark.parametrize(
    "claims",
    [
        {"email_verified": True},
        {"email_confirmed_at": "2026-11-01T12:00:00Z"},
        {"email_verified": True, "app_metadata": {"provider": "email"}},
    ],
)
def test_claims_de_nivel_superior_se_respetan(claims):
    assert email_verified_from_claims(claims) is True


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"email_verified": False, "email_confirmed_at": None},
        {"email_verified": "true"},  # solo el booleano exacto
        {"app_metadata": None},
        {"app_metadata": "google"},
        {"app_metadata": {"provider": None}},
        {"app_metadata": {"provider": 123}},
        {"app_metadata": {"provider": "github"}},
        {"app_metadata": {"provider": ""}},
    ],
)
def test_sin_evidencia_fiable_no_se_marca_verificado(claims):
    assert email_verified_from_claims(claims) is False


def test_normaliza_el_nombre_del_proveedor():
    assert email_verified_from_claims({"app_metadata": {"provider": " Google "}}) is True


# ---------------------------------------------------------------- JWT firmado completo


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _settings() -> Settings:
    return Settings(
        auth_mode="supabase_jwt",
        supabase_jwks_url=JWKS_URL,
        supabase_jwt_issuer=ISSUER,
    )


def _payload(**overrides) -> dict:
    now = int(time.time())
    base = {
        "aud": "authenticated",
        "iss": ISSUER,
        "sub": "sub-google-nueva",
        "email": "nueva@example.test",
        "role": "authenticated",
        "iat": now,
        "exp": now + 300,
        "app_metadata": {"provider": "google", "providers": ["google"]},
        "user_metadata": {"full_name": "Nueva Persona"},
    }
    base.update(overrides)
    return base


@pytest.fixture
def signed_jwt(monkeypatch):
    """Firma con una clave RSA de prueba y sustituye el JWKS público; no hay red."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    jwk = PyJWK(
        {
            "kty": "RSA",
            "kid": KID,
            "use": "sig",
            "alg": "RS256",
            "n": _b64(numbers.n),
            "e": _b64(numbers.e),
        }
    )

    async def _keys(_url, *, force=False):
        return {KID: jwk}

    monkeypatch.setattr(supabase_auth, "_keys", _keys)

    def sign(claims: dict) -> str:
        return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": KID})

    return sign


async def test_jwt_google_produce_correo_verificado(signed_jwt):
    claims = await verify_supabase_jwt_claims(signed_jwt(_payload()), _settings())
    assert claims == JwtClaims(
        subject="sub-google-nueva",
        email="nueva@example.test",
        email_verified=True,
        display_name="Nueva Persona",
    )


async def test_jwt_correo_y_contrasena_sin_evidencia_no_se_marca_verificado(signed_jwt):
    token = signed_jwt(
        _payload(
            app_metadata={"provider": "email", "providers": ["email"]},
            user_metadata={"email_verified": True, "full_name": "Nueva Persona"},
        )
    )
    claims = await verify_supabase_jwt_claims(token, _settings())
    assert claims.email_verified is False
    # El nombre sí puede venir de `user_metadata` (solo es un dato informativo, no un permiso).
    assert claims.display_name == "Nueva Persona"


async def test_jwt_google_sin_correo_lo_informa_ausente(signed_jwt):
    claims = await verify_supabase_jwt_claims(signed_jwt(_payload(email=None)), _settings())
    assert claims.email is None
    assert claims.email_verified is True  # el proveedor sigue avalando la identidad
