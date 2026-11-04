"""Validación del parser de Ahumada con el HTML real de una búsqueda (archivo local, sin red).

Este archivo **no contiene ninguna captura**: la página real se guarda fuera del control de
versiones y la prueba solo la lee desde disco. Sin archivo, la prueba se omite y explica dónde
dejarlo.

Cómo ejecutarla:

    cd backend
    .venv/bin/pytest tests/test_ahumada_html_real.py -q -s

Por defecto lee `tests/fixtures/ahumada_search_real.html`. Para usar otro archivo:

    AHUMADA_HTML=/ruta/a/resultados-ahumada.html .venv/bin/pytest tests/test_ahumada_html_real.py -q -s

Caso de referencia: `84574` — Paracetamol 500 mg x 16 comprimidos, con precios observados de
$1.309 normal, $618 promocional y $82 por comprimido.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from app.services import ahumada

DEFAULT_HTML = Path(__file__).parent / "fixtures" / "ahumada_search_real.html"
REFERENCE_PID = "84574"


def _html_path() -> Path:
    return Path(os.environ.get("AHUMADA_HTML", DEFAULT_HTML)).expanduser()


def _clp(price: dict[str, Any] | None) -> str:
    if not price:
        return "—"
    text = f"${int(price['amount']):,}".replace(",", ".")
    if price.get("per_unit"):
        text += f" por {price['per_unit']}"
    return text


def _issues(product: dict[str, Any]) -> list[str]:
    """Campos que no se pudieron extraer o combinaciones que requieren revisión manual."""
    price = product["price"]
    issues: list[str] = []
    if not product["laboratory_as_published"]:
        issues.append("sin laboratorio")
    if not product["presentation_as_published"]:
        issues.append("sin presentación")
    if not product["image_url"]:
        issues.append("sin imagen")
    if not product["source_url"]:
        issues.append("sin enlace oficial")
    if price["normal"] is None and price["promotional"] is None and price["fractional"] is None:
        issues.append("sin ningún precio publicado")
    elif price["normal"] is None and price["promotional"] is None:
        issues.append("solo precio fraccionado, sin precio de envase")
    if price["promotional"] and not price["normal"]:
        issues.append("precio promocional sin precio normal")
    if price["promotional"] and not product["conditions"]:
        issues.append("precio promocional sin condición publicada")
    if (
        price["normal"]
        and price["promotional"]
        and int(price["promotional"]["amount"]) >= int(price["normal"]["amount"])
    ):
        issues.append("precio promocional ≥ precio normal")
    if price["fractional"] and not price["fractional"].get("per_unit"):
        issues.append("precio fraccionado sin unidad")
    if (
        price["fractional"]
        and price["normal"]
        and int(price["fractional"]["amount"]) >= int(price["normal"]["amount"])
    ):
        issues.append("precio fraccionado ≥ precio del envase")
    return issues


@pytest.fixture(scope="module")
def reporte() -> dict[str, Any]:
    path = _html_path()
    if not path.exists():
        pytest.skip(
            "No hay HTML real para validar. Guarda la página de resultados en "
            f"{DEFAULT_HTML} o indícala con AHUMADA_HTML=/ruta/al/archivo.html"
        )

    html_text = path.read_text(encoding="utf-8", errors="replace")
    root = ahumada._parse_html(html_text)
    candidate_ids = [pid for _, pid in ahumada._tiles(root)]
    # Para validar la extracción completa se anula el tope de la interfaz (24).
    results = ahumada.parse_search_results(html_text, limit=max(len(candidate_ids), 1))
    return {
        "path": path,
        "bytes": len(html_text.encode("utf-8")),
        "candidate_ids": candidate_ids,
        "results": results,
    }


def test_informe_de_extraccion(reporte: dict[str, Any]) -> None:
    """Imprime el detalle ficha por ficha y señala los errores de extracción detectados."""
    results = reporte["results"]
    candidate_ids = reporte["candidate_ids"]
    detected_ids = [product["chain_item_id"] for product in results]
    omitted = [pid for pid in candidate_ids if pid not in detected_ids]

    print(f"\n=== HTML real: {reporte['path']} ({reporte['bytes']:,} bytes) ===")
    print(f"Tarjetas candidatas (data-pid): {len(candidate_ids)}")
    print(f"Productos detectados: {len(results)}")
    if omitted:
        print(f"Tarjetas omitidas (sin nombre o URL oficial atribuible): {', '.join(omitted)}")

    duplicates = {pid for pid in detected_ids if detected_ids.count(pid) > 1}
    if duplicates:
        print(f"IDs duplicados: {', '.join(sorted(duplicates))}")

    errors: list[str] = []
    for index, product in enumerate(results, start=1):
        issues = _issues(product)
        price = product["price"]
        print(f"\n[{index}] {product['chain_item_id']} — {product['published_name']}")
        print(f"    Laboratorio: {product['laboratory_as_published'] or '—'}")
        print(f"    Presentación: {product['presentation_as_published'] or '—'}")
        print(f"    Precio normal: {_clp(price['normal'])}")
        print(f"    Precio promocional: {_clp(price['promotional'])}")
        print(f"    Precio fraccionado: {_clp(price['fractional'])}")
        print(f"    Condición promocional: {product['conditions'] or '—'}")
        print(f"    Imagen: {product['image_url'] or '—'}")
        print(f"    Enlace oficial: {product['source_url'] or '—'}")
        print(f"    Errores de extracción: {', '.join(issues) if issues else 'ninguno'}")
        errors.extend(f"{product['chain_item_id']}: {issue}" for issue in issues)

    print("\n=== Resumen ===")
    if not candidate_ids:
        print("ERROR: no se encontraron tarjetas con data-pid; la estructura del HTML pudo cambiar.")
    print(f"Productos con errores de extracción: {len(errors)}")
    for error in errors:
        print(f"  - {error}")

    assert candidate_ids, (
        "El HTML real no contiene tarjetas con data-pid. Revisa que sea la página de resultados "
        "completa y que la estructura siga usando product-tile/data-pid."
    )
    assert results, "Se encontraron tarjetas, pero ninguna con nombre y URL oficial atribuible."


def test_caso_de_referencia_84574(reporte: dict[str, Any]) -> None:
    """Contrasta el producto inspeccionado manualmente contra lo que extrae el parser."""
    results = reporte["results"]
    reference = next(
        (product for product in results if product["chain_item_id"] == REFERENCE_PID), None
    )

    assert reference is not None, (
        f"No se detectó el producto {REFERENCE_PID}. IDs detectados: "
        f"{', '.join(product['chain_item_id'] for product in results) or 'ninguno'}"
    )
    assert "paracetamol" in reference["published_name"].lower()
    assert "500" in reference["published_name"]
    assert reference["price"]["normal"] == {"amount": "1309", "currency": "CLP"}
    assert reference["price"]["promotional"] == {"amount": "618", "currency": "CLP"}
    assert reference["price"]["fractional"] is not None
    assert reference["price"]["fractional"]["amount"] == "82"
    assert reference["price"]["fractional"]["per_unit"].startswith("comprimido")
    assert reference["source_url"].startswith("https://www.farmaciasahumada.cl/")
    assert reference["image_url"] and reference["image_url"].startswith("https://")
