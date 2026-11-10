"""Motor declarativo de proveedores.

Punto de entrada estable del paquete. La implementación se reparte en:

* `base`     — contrato de estrategia, ofertas normalizadas y estados;
* `config`   — validación estricta de perfiles (sin ejecutar código);
* `paths`    — lectura declarativa de campos;
* `algolia`  — estrategia genérica para índices Algolia;
* `presets`  — perfiles conocidos, sin lógica por proveedor;
* `registry` — mecanismo → estrategia;
* `service`  — configuración y búsqueda en vivo;
* `store`    — caché en memoria, sin persistir ofertas.
"""

from app.services.connectors.base import (
    CONNECTOR_STATES,
    ConnectorError,
    ConnectorIntervention,
    ConnectorNotConfigured,
    ConnectorResult,
    SupplierOffer,
    SupplierStrategy,
)

__all__ = [
    "CONNECTOR_STATES",
    "ConnectorError",
    "ConnectorIntervention",
    "ConnectorNotConfigured",
    "ConnectorResult",
    "SupplierOffer",
    "SupplierStrategy",
]
