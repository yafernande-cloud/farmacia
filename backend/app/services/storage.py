"""Almacenamiento privado de imágenes (HLD §8). Local hoy; la interfaz permite S3 después.

Se valida el tipo REAL por firma de bytes (no por lo que diga el cliente), se limita el tamaño y el
archivo se guarda con permisos solo del dueño, direccionado por su SHA-256.
"""

import hashlib
import os
from pathlib import Path
from typing import Protocol

from app.core.errors import DomainError


class PrivateStorage(Protocol):
    def put(self, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...


def detect_image_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise DomainError(
        "UNSUPPORTED_IMAGE", "El archivo no es una imagen JPEG, PNG o WebP.", status_code=415
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LocalPrivateStorage:
    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, data: bytes) -> str:
        key = f"{sha256_hex(data)[:2]}/{sha256_hex(data)}"
        path = self.root / key
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return key

    def get(self, key: str) -> bytes:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:  # sin salirse del directorio
            raise DomainError(
                "INVALID_STORAGE_KEY", "Clave de almacenamiento inválida.", status_code=400
            )
        return path.read_bytes()
