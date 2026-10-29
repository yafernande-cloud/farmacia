"""Códigos temporales de solicitud de acceso (los comparte la persona con la administradora).

* Alfabeto sin caracteres ambiguos (0/1/I/O): 32 símbolos, 20 caracteres ≈ 100 bits.
* Se muestra agrupado (ABCD-EFGH-…) para leerlo en voz alta o copiarlo.
* En la base SOLO se guarda el SHA-256 del código normalizado. El código en claro se envía una vez
  por Telegram y nunca se registra en logs, auditoría, outbox ni mensajes entrantes.
* La normalización ignora separadores y mayúsculas; no se adivinan caracteres parecidos.
"""

import hashlib
import re
import secrets

# Sin 0, 1, I, O (se confunden al dictarlos). 32 símbolos.
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 20
GROUP_SIZE = 4
CODE_RE = re.compile(r"^(?:[2-9A-HJ-NP-Z]{4}[-\s]?){4}[2-9A-HJ-NP-Z]{4}$", re.IGNORECASE)


def generate_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i : i + GROUP_SIZE] for i in range(0, CODE_LENGTH, GROUP_SIZE))


def normalize_code(code: str | None) -> str:
    """Mayúsculas y sin separadores. No reemplaza caracteres ambiguos."""
    return re.sub(r"[^0-9A-Za-z]", "", code or "").upper()


def code_hash(code: str | None) -> str:
    return hashlib.sha256(normalize_code(code).encode("ascii", "ignore")).hexdigest()


def looks_like_code(text: str | None) -> bool:
    """¿El texto tiene la FORMA de un código? Sirve para no persistirlo si se escribe en el chat."""
    return bool(CODE_RE.fullmatch((text or "").strip()))
