from decimal import Decimal as D

import pytest

from app.core.errors import DomainError
from app.domain.quantities import Quantity, apply_command
from app.domain.quantities import QuantityOp as Op
from app.domain.quantities import QuantityUnit as U

BOX5 = Quantity(D(5), U.BOX)


def test_set_reemplaza_nunca_suma():
    assert apply_command(BOX5, Op.SET, D(5), U.BOX) == BOX5  # AC06
    assert apply_command(BOX5, Op.SET, D(8), U.BOX) == Quantity(D(8), U.BOX)


def test_add_suma_sobre_cantidad_conocida():
    assert apply_command(BOX5, Op.ADD, D(5), U.BOX) == Quantity(D(10), U.BOX)


def test_add_sobre_desconocida_se_rechaza():
    with pytest.raises(DomainError) as e:
        apply_command(None, Op.ADD, D(5), U.BOX)
    assert e.value.code == "QUANTITY_UNKNOWN"


def test_add_con_otra_unidad_se_rechaza():
    with pytest.raises(DomainError) as e:
        apply_command(BOX5, Op.ADD, D(5), U.UNIT)
    assert e.value.code == "UNIT_MISMATCH"


def test_clear_vuelve_a_desconocida():
    assert apply_command(BOX5, Op.CLEAR, None, None) is None


@pytest.mark.parametrize(
    "op,v,u",
    [(Op.SET, D(-1), U.BOX), (Op.ADD, D(0), U.BOX), (Op.SET, None, None), (Op.CLEAR, D(1), U.BOX)],
)
def test_comandos_invalidos(op, v, u):
    with pytest.raises(DomainError) as e:
        apply_command(BOX5, op, v, u)
    assert e.value.status_code == 400
