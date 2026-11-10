"""Registro de estrategias por mecanismo.

Agregar un mecanismo nuevo consiste en implementar una estrategia y registrarla aquí. El modelo,
la API y el estado no cambian; tampoco se agrega una rama por proveedor.
"""

from __future__ import annotations

from app.services.connectors import algolia
from app.services.connectors.base import ConnectorIntervention, SupplierStrategy

_STRATEGIES: dict[str, SupplierStrategy] = {
    algolia.STRATEGY.mechanism: algolia.STRATEGY,
}


def get_strategy(mechanism: str) -> SupplierStrategy:
    strategy = _STRATEGIES.get(mechanism)
    if strategy is None:
        raise ConnectorIntervention(
            "mechanism_not_implemented", details={"mechanism": mechanism}
        )
    return strategy


def implemented_mechanisms() -> tuple[str, ...]:
    return tuple(sorted(_STRATEGIES))
