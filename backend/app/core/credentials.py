"""Cifrado en reposo de las credenciales de portales de proveedores (A3).

La contraseña del portal nunca se guarda en claro ni se devuelve por la API. Se cifra con Fernet
(AES-128-CBC + HMAC) usando `SUPPLIER_CREDENTIALS_KEY`, que vive solo en el entorno del backend.
Sin clave configurada, guardar una credencial se rechaza (`SUPPLIER_CREDENTIALS_KEY_MISSING`);
nunca se cae a texto plano para «que funcione igual».

Guardar la credencial no prueba ninguna conexión: no hay conector de portal todavía.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.core.errors import DomainError

KEY_MISSING = "SUPPLIER_CREDENTIALS_KEY_MISSING"
KEY_INVALID = "SUPPLIER_CREDENTIALS_KEY_INVALID"
UNREADABLE = "SUPPLIER_CREDENTIALS_UNREADABLE"


def _cipher() -> Fernet:
    raw = get_settings().supplier_credentials_key
    value = raw.get_secret_value().strip() if raw is not None else ""
    if not value:
        raise DomainError(
            KEY_MISSING,
            "No está configurada la clave de cifrado de credenciales de proveedores. "
            "Sin ella no se puede guardar una contraseña.",
            status_code=503,
        )
    try:
        return Fernet(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as e:
        raise DomainError(
            KEY_INVALID,
            "SUPPLIER_CREDENTIALS_KEY no es una clave Fernet válida (32 bytes en base64 "
            "url-safe).",
            status_code=503,
        ) from e


def encrypt_portal_password(plain: str) -> str:
    """Devuelve el texto cifrado que se guarda en `supplier_credential.password_ciphertext`."""
    return _cipher().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_portal_password(ciphertext: str) -> str:
    """Descifra en el backend. No se expone por la API ni se registra en logs."""
    try:
        return _cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as e:
        raise DomainError(
            UNREADABLE,
            "La contraseña guardada no se puede descifrar con la clave actual.",
            status_code=500,
        ) from e
