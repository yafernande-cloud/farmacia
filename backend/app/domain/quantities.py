"""Semántica de cantidades (HLD §7.3). Funciones puras: sin I/O.

Cantidad desconocida es None (no 0 ni 1). ADD solo suma sobre una cantidad conocida y explícita.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.core.errors import DomainError


class QuantityOp(StrEnum):
    SET = "SET"
    ADD = "ADD"
    CLEAR = "CLEAR"


class QuantityUnit(StrEnum):
    BOX = "box"
    PACKAGE = "package"
    UNIT = "unit"
    BOTTLE = "bottle"


@dataclass(frozen=True)
class Quantity:
    value: Decimal
    unit: QuantityUnit


def apply_command(
    current: Quantity | None,
    op: QuantityOp,
    value: Decimal | None,
    unit: QuantityUnit | None,
) -> Quantity | None:
    if op is QuantityOp.CLEAR:
        if value is not None or unit is not None:
            raise DomainError("INVALID_COMMAND", "CLEAR no lleva valor ni unidad.", status_code=400)
        return None
    if value is None or unit is None:
        raise DomainError("INVALID_COMMAND", f"{op} requiere valor y unidad.", status_code=400)
    if op is QuantityOp.SET:
        if value < 0:
            raise DomainError(
                "INVALID_COMMAND", "La cantidad no puede ser negativa.", status_code=400
            )
        return Quantity(value, unit)
    # ADD
    if value <= 0:
        raise DomainError(
            "INVALID_COMMAND", "ADD requiere un valor mayor que cero.", status_code=400
        )
    if current is None:
        raise DomainError(
            "QUANTITY_UNKNOWN",
            "No hay una cantidad definida a la que sumar. Fija el total con SET.",
        )
    if current.unit != unit:
        raise DomainError(
            "UNIT_MISMATCH",
            f"La cantidad actual está en '{current.unit}', no en '{unit}'.",
            details={"current_unit": current.unit},
        )
    return Quantity(current.value + value, unit)


def fmt_qty(d: Decimal | None) -> str | None:
    """Representación estable de una cantidad: '5', '2.5' (sin ceros de relleno)."""
    return None if d is None else format(d.normalize(), "f")
