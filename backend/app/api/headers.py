"""Cabeceras HTTP compartidas por las operaciones con control de versión optimista."""

import re

from app.core.errors import DomainError


def parse_if_match(value: str | None, *, what: str = "el recurso") -> int:
    """Lee `If-Match` con la versión (`"3"`, `3` o `W/"3"`). Sin cabecera válida → 428."""
    m = re.fullmatch(r'\s*(?:W/)?"?(\d+)"?\s*', value or "")
    if not m:
        raise DomainError(
            "VERSION_REQUIRED",
            f"Falta If-Match con la versión de {what}.",
            status_code=428,
        )
    return int(m.group(1))
