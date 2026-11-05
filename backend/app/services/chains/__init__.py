"""Registro de conectores E15. El coordinador solo conoce este mapa, nunca los detalles de cada
fuente. Incorporar una cadena nueva es agregar su módulo y una entrada aquí; no se tocan las demás.
"""

from app.services.chains import ahumada, cruzverde, salcobrand
from app.services.chains.base import ChainNotConfigured, ChainUnavailable, ConnectorFn

CONNECTORS: dict[str, ConnectorFn] = {
    ahumada.CHAIN: ahumada.search,
    cruzverde.CHAIN: cruzverde.search,
    salcobrand.CHAIN: salcobrand.search,
}

__all__ = ["CONNECTORS", "ChainNotConfigured", "ChainUnavailable", "ConnectorFn"]
