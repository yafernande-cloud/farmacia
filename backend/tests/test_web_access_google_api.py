"""Solicitudes de acceso con JWT real de Supabase, firmado igual que en producción (D-158).

Corrige el defecto que impedía solicitar acceso con una cuenta Google nueva: Supabase no incluye
`email_verified` de nivel superior en el access token de OAuth, así que el backend rechazaba
`GET /status` y el POST con 403 `EMAIL_NOT_VERIFIED` aunque GoTrue ya hubiera confirmado la cuenta.

Estas pruebas usan tokens firmados de verdad (clave RSA de prueba, JWKS sustituido) contra la API
completa y PostgreSQL real:
  * una cuenta Google nueva consulta `none` y registra su solicitud, sin recibir acceso;
  * repetir la solicitud no duplica filas;
  * una cuenta con correo + contraseña sin evidencia de verificación sigue rechazada, incluso si
    `user_metadata` dice `email_verified: true`;
  * una cuenta ya autorizada conserva su acceso.
"""

import base64
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from jwt import PyJWK
from sqlalchemy import func, select

from app.core import supabase_auth
from app.core.config import get_settings
from app.db.session import get_session
from app.main import create_app
from app.models import AppUser, Organization, WebAccessGrant, WebAccessRequest

pytestmark = pytest.mark.db

ISSUER = "https://example.supabase.co/auth/v1"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
KID = "test-kid"

GOOGLE = {"provider": "google", "providers": ["google"]}
EMAIL = {"provider": "email", "providers": ["email"]}


@pytest.fixture(scope="module")
def rsa_key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()

    def b64(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    jwk = PyJWK(
        {
            "kty": "RSA",
            "kid": KID,
            "use": "sig",
            "alg": "RS256",
            "n": b64(numbers.n),
            "e": b64(numbers.e),
        }
    )
    return private_key, jwk


def bearer(rsa_key, **overrides: Any) -> dict[str, str]:
    private_key, _ = rsa_key
    now = int(datetime.now(UTC).timestamp())
    payload: dict[str, Any] = {
        "aud": "authenticated",
        "iss": ISSUER,
        "sub": "sub-google-nueva",
        "email": "nueva@example.test",
        "role": "authenticated",
        "iat": now,
        "exp": now + 300,
        "app_metadata": GOOGLE,
        "user_metadata": {"full_name": "Nueva Persona"},
    }
    payload.update(overrides)
    raw = jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": KID})
    return {"Authorization": f"Bearer {raw}"}


@pytest.fixture
async def farm(sessions):
    """Organización real con una persona ya autorizada (identidad enlazada + autorización)."""
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia Google", kind="real_test")
        s.add(org)
        await s.flush()
        user = AppUser(
            organization_id=org.id,
            display_name="Autorizada",
            roles=["admin"],
            auth_subject="sub-autorizada",
        )
        s.add(user)
        await s.flush()
        s.add(
            WebAccessGrant(
                app_user_id=user.id,
                organization_id=org.id,
                granted_at=datetime.now(UTC),
                expires_at=None,
            )
        )
    return {"org": org.id, "user": user.id}


@pytest.fixture(autouse=True)
def _rate_limits():
    from app.api.v1 import web_access as api_module

    api_module._per_subject.reset()
    api_module._per_ip.reset()
    yield
    api_module._per_subject.reset()
    api_module._per_ip.reset()


@pytest.fixture
async def api(sessions, farm, rsa_key, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "supabase_jwt")
    monkeypatch.setenv("SUPABASE_JWKS_URL", JWKS_URL)
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("WEB_ACCESS_ORGANIZATION_ID", str(farm["org"]))
    get_settings.cache_clear()

    _, jwk = rsa_key

    async def _keys(_url, *, force=False):
        return {KID: jwk}

    monkeypatch.setattr(supabase_auth, "_keys", _keys)
    app = create_app()

    async def _s():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    get_settings.cache_clear()


async def count(sessions, model, *where) -> int:
    async with sessions() as s:
        return (
            await s.execute(select(func.count()).select_from(model).where(*where))
        ).scalar_one()


STATUS_URL = "/api/v1/web-access-requests/status"
REQUESTS_URL = "/api/v1/web-access-requests"


async def test_google_nueva_consulta_su_estado_sin_email_verified(api, rsa_key):
    r = await api.get(STATUS_URL, headers=bearer(rsa_key))
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "none", "requested_at": None, "decided_at": None}


async def test_google_nueva_solicita_acceso_sin_recibirlo(api, rsa_key, sessions, farm):
    r = await api.post(REQUESTS_URL, json={}, headers=bearer(rsa_key))
    assert r.status_code == 202, r.text
    assert r.json() == {"received": True}

    async with sessions() as s:
        row = (await s.execute(select(WebAccessRequest))).scalar_one()
    assert row.organization_id == farm["org"]
    assert row.auth_subject == "sub-google-nueva"
    assert row.email == "nueva@example.test"
    assert row.display_name == "Nueva Persona"
    assert row.status == "pending"

    # La solicitud es solo una solicitud: sin persona nueva y sin autorización web.
    assert (
        await count(sessions, AppUser, AppUser.auth_subject == "sub-google-nueva")
    ) == 0
    assert await count(sessions, WebAccessGrant) == 1  # solo la autorizada preexistente


async def test_solicitud_repetida_no_duplica_y_queda_pendiente(api, rsa_key, sessions):
    for _ in range(2):
        r = await api.post(REQUESTS_URL, json={}, headers=bearer(rsa_key))
        assert r.status_code == 202, r.text
        assert r.json() == {"received": True}

    assert (
        await count(sessions, WebAccessRequest, WebAccessRequest.auth_subject == "sub-google-nueva")
    ) == 1

    r = await api.get(STATUS_URL, headers=bearer(rsa_key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["requested_at"] is not None


async def test_correo_sin_verificar_sigue_rechazado_aunque_metadata_lo_diga(api, rsa_key, sessions):
    headers = bearer(
        rsa_key,
        sub="sub-sin-verificar",
        app_metadata=EMAIL,
        user_metadata={"email_verified": True, "full_name": "Sin Verificar"},
    )

    r = await api.post(REQUESTS_URL, json={}, headers=headers)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"

    r = await api.get(STATUS_URL, headers=headers)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"

    assert (
        await count(sessions, WebAccessRequest, WebAccessRequest.auth_subject == "sub-sin-verificar")
    ) == 0


async def test_sin_correo_sigue_exigiendo_email(api, rsa_key, sessions):
    headers = bearer(rsa_key, sub="sub-sin-correo", email=None)

    r = await api.post(REQUESTS_URL, json={}, headers=headers)
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "EMAIL_REQUIRED"

    assert (
        await count(sessions, WebAccessRequest, WebAccessRequest.auth_subject == "sub-sin-correo")
    ) == 0


async def test_cuenta_autorizada_conserva_su_acceso(api, rsa_key, farm):
    headers = bearer(
        rsa_key, sub="sub-autorizada", email="autorizada@example.test",
        app_metadata=GOOGLE,
    )  # fmt: skip

    r = await api.get("/api/v1/me", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == str(farm["user"])
    assert body["roles"] == ["admin"]

    r = await api.get(STATUS_URL, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"


async def test_cuenta_autorizada_por_correo_y_contrasena_sigue_entrando(api, rsa_key, farm):
    """El acceso ya aprobado no depende del proveedor ni de la verificación en cada petición."""
    headers = bearer(
        rsa_key, sub="sub-autorizada", email="autorizada@example.test", app_metadata=EMAIL,
        user_metadata={},
    )  # fmt: skip

    r = await api.get("/api/v1/me", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == str(farm["user"])
