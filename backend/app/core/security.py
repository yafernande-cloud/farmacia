"""Hash de PIN de auxiliar (preparado para el piloto; no está expuesto por la API aún).

scrypt de la biblioteca estándar: resistente a fuerza bruta, sin dependencias nuevas.
El PIN nunca se registra en auditoría ni en logs.
"""

import base64
import hashlib
import hmac
import os

_N, _R, _P = 2**14, 8, 1


def hash_pin(pin: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pin.encode(), salt=salt, n=_N, r=_R, p=_P)
    return (
        f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
    )


def verify_pin(pin: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        calc = hashlib.scrypt(
            pin.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p)
        )
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False
