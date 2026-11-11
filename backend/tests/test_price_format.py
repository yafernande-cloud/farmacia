"""Interpretación de precios y detección de formatos ambiguos (D-151).

Son pruebas sin base de datos: solo el analizador determinista. El caso central es «2.490»:
con separador de miles es 2490; como decimal es 2,49; sin evidencia no se elige ninguna lectura y
la importación real debe pedir confirmación.
"""

from decimal import Decimal

import pytest

from app.services.imports import assess_price_format, parse_decimal_text


@pytest.mark.parametrize(
    ("texto", "decimal", "miles", "esperado"),
    [
        # Formato chileno: punto de miles, coma decimal.
        ("12.345,60", ",", ".", Decimal("12345.60")),
        ("1.234.567", ",", ".", Decimal("1234567")),
        ("999,00", ",", ".", Decimal("999.00")),
        # Formato con coma de miles y punto decimal.
        ("12,345.60", ".", ",", Decimal("12345.60")),
        ("1,234,567", ".", ",", Decimal("1234567")),
        # Solo decimales.
        ("2.49", ".", None, Decimal("2.49")),
        ("12,50", ",", None, Decimal("12.50")),
        # Número entero sin separadores.
        ("2490", ".", None, Decimal("2490")),
        # Espacio como separador de miles: nunca puede ser decimal.
        ("2 490", ",", " ", Decimal("2490")),
        ("12 345,60", ",", " ", Decimal("12345.60")),
        # Signo.
        ("-2.490", ",", ".", Decimal("-2490")),
    ],
)
def test_interpreta_formatos_declarados(texto, decimal, miles, esperado):
    assert parse_decimal_text(texto, decimal, miles) == esperado


@pytest.mark.parametrize(
    ("texto", "decimal", "miles"),
    [
        # El separador de miles debe agrupar de tres en tres: «2.49» no es 24900.
        ("2.49", ",", "."),
        ("12.34", ",", "."),
        (".490", ",", "."),
        # Un separador que el formato no declara no se elimina en silencio.
        ("2,490", ".", None),
        ("2 490", ".", None),
        # Símbolos y valores incompletos.
        ("$2.490", ",", "."),
        ("2.", ".", None),
        ("abc", ".", None),
        ("--2", ".", None),
        ("1e3", ".", None),
    ],
)
def test_rechaza_lo_que_el_formato_no_explica(texto, decimal, miles):
    assert parse_decimal_text(texto, decimal, miles) is None


def _assess(values, **kwargs):
    return assess_price_format(
        [(index + 2, (value,)) for index, value in enumerate(values)], 0, **kwargs
    )


def test_dos_mil_novecientos_cuarenta_es_ambiguo_y_pide_confirmacion():
    resultado = _assess(["2.490", "3.490", "1.500"])

    assert resultado["status"] == "ambiguous"
    assert resultado["requires_confirmation"] is True
    assert {i["key"] for i in resultado["interpretations"]} == {"decimal", "thousands"}
    lecturas = {i["key"]: i["examples"][0]["amount"] for i in resultado["interpretations"]}
    assert lecturas == {"decimal": "2.49", "thousands": "2490"}
    assert resultado["examples"][0]["amounts"] == ["2.49", "2490"]
    assert "ambiguo" in (resultado["message"] or "")


def test_una_confirmacion_explicita_desbloquea_la_lectura_de_miles():
    resultado = _assess(["2.490"], decimal_sep=",", thousands_sep=".", confirmed=True)

    assert resultado["status"] == "ambiguous"
    assert resultado["declared_valid"] is True
    assert resultado["requires_confirmation"] is False


def test_una_confirmacion_explicita_tambien_puede_elegir_decimales():
    resultado = _assess(["2.490"], decimal_sep=".", thousands_sep=None, confirmed=True)

    assert resultado["declared_valid"] is True
    assert resultado["requires_confirmation"] is False


def test_un_formato_inequivoco_no_pide_confirmacion():
    assert _assess(["2.49", "3.10"])["status"] == "ok"
    assert _assess(["12.345,60", "999,00"])["status"] == "ok"
    assert _assess(["1.234.567"])["status"] == "ok"


def test_los_numeros_de_excel_no_se_reinterpretan_por_sus_separadores():
    # 2.49 y 2490 son números ya resueltos por Excel: no se vuelven a leer como texto.
    resultado = _assess([Decimal("2.490"), 2490, 3.5])

    assert resultado["status"] == "ok"
    assert resultado["interpretations"] == []
    assert resultado["requires_confirmation"] is False


def test_una_celda_ilegible_no_oculta_la_ambiguedad_de_las_demas():
    resultado = _assess(["n/a", "2.490"])

    assert resultado["status"] == "ambiguous"
    assert resultado["unrecognized_values"] == 1
    assert resultado["requires_confirmation"] is True


def test_el_espacio_solo_agrupa_miles_y_no_genera_ambiguedad():
    resultado = _assess(["2 490", "3 000"])

    assert resultado["status"] == "ok"
    assert resultado["thousands_separator"] == " "
    assert resultado["requires_confirmation"] is False
