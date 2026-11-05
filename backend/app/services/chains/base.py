"""Contrato mínimo de un conector E15: consultar SU cadena o fallar de forma explícita.

Cada conector decide con su propia configuración si está habilitado y cómo consulta su fuente.
El coordinador solo distingue tres resultados: fichas publicadas, cadena no configurada o fuente
caída. Nunca mezcla productos entre cadenas ni completa datos de una con otra.
"""

from collections.abc import Awaitable, Callable
from typing import Any


class ChainNotConfigured(Exception):
    """La cadena no está habilitada o le falta configuración. Motivo apto para el contrato."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ChainUnavailable(Exception):
    """La fuente respondió con un error o con datos inutilizables. Se informa `failed`."""

    def __init__(self, reason: str = "fetch_failed"):
        super().__init__(reason)
        self.reason = reason


# Un conector recibe la consulta y devuelve fichas normalizadas; no persiste nada.
ConnectorFn = Callable[[str], Awaitable[list[dict[str, Any]]]]
