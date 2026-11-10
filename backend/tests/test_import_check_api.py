"""Revisión previa `POST /api/v1/sources/{id}/import-check` — A3.

Comprueba lo que el flujo nuevo necesita: que la revisión muestre lo que la importación haría y
que **no escriba nada** (ni ofertas, ni artículos, ni perfil, ni `import_run`). El motor se prueba
en `test_imports_manual.py`; aquí se verifica que reutilizarlo en seco no deja rastro.

Las planillas y la organización son `fixture`: se generan en esta prueba.
"""

# ruff: noqa: F811 — las fixtures reexportadas se usan como parámetros de cada prueba.

import json
from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import select

# Se reutilizan la organización, la app y los generadores de planillas de las pruebas de E8.
from test_imports_api import H, api, count, farm, fila, xlsx  # noqa: F401

from app.models import (
    ImportProfile,
    ImportRequest,
    ImportRun,
    OfferSnapshot,
    SupplierProductMapping,
    TradeItem,
)

pytestmark = pytest.mark.db

HEADER = ["Código", "Descripción", "Precio", "Moneda", "Base", "Unidad"]
COLUMNS = {
    "sku": {"header": "Código"},
    "name": {"header": "Descripción"},
    "price": {"header": "Precio"},
    "currency": {"header": "Moneda"},
    "basis": {"header": "Base"},
    "sale_unit": {"header": "Unidad"},
}
UNITS = {"CAJA": "box", "UNIDAD": "unit"}
PROFILE = {
    "file_format": "xlsx",
    "sheet": None,
    "header_row": 1,
    "columns": COLUMNS,
    "units": UNITS,
    "options": {},
}


def xlsx_hojas(hojas: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for nombre, filas in hojas.items():
        ws = wb.create_sheet(nombre)
        for values in filas:
            ws.append(values)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def revisar(api, content=None, *, who="admin", source=None, profile=PROFILE, **over):
    data = {}
    if profile is not None:
        data["profile"] = json.dumps(profile)
    data.update(over)
    return await api.post(
        f"/api/v1/sources/{source or api.farm['source']}/import-check",
        files={
            "file": (
                "lista.xlsx",
                content if content is not None else xlsx([fila()]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data=data,
        headers=H(api, who),
    )


# ---------- la revisión no escribe ----------


async def test_revision_interpreta_filas_y_no_escribe_nada(api, sessions):
    antes = {
        m.__name__: await count(sessions, m)
        for m in (ImportRun, ImportRequest, OfferSnapshot, TradeItem, SupplierProductMapping)
    }
    async with sessions() as s:
        perfil_antes = (
            await s.execute(
                select(ImportProfile.version, ImportProfile.updated_at).where(
                    ImportProfile.source_id == api.farm["source"]
                )
            )
        ).one()

    r = await revisar(api, xlsx([fila(), fila(sku="SKU-002", precio="999.00")]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["rows_total"], body["rows_accepted"], body["rows_rejected"]) == (2, 2, 0)
    assert body["rows_needs_review"] == 0
    assert body["profile_source"] == "request"
    assert body["rows_truncated"] is False
    fila_2 = next(row for row in body["rows"] if row["row"] == 2)
    assert fila_2["status"] == "ready"
    assert fila_2["sku"] == "SKU-001"
    assert fila_2["price"] == "12345.60"
    assert (fila_2["currency"], fila_2["price_basis"], fila_2["sale_unit"]) == (
        "CLP",
        "per_commercial_unit",
        "box",
    )
    assert fila_2["problem"] is None

    despues = {
        m.__name__: await count(sessions, m)
        for m in (ImportRun, ImportRequest, OfferSnapshot, TradeItem, SupplierProductMapping)
    }
    async with sessions() as s:
        perfil_despues = (
            await s.execute(
                select(ImportProfile.version, ImportProfile.updated_at).where(
                    ImportProfile.source_id == api.farm["source"]
                )
            )
        ).one()
    assert antes == despues
    assert perfil_antes == perfil_despues


async def test_revision_no_consume_la_importacion_posterior(api, sessions):
    r = await revisar(api, xlsx([fila()]))
    assert r.status_code == 200
    assert await count(sessions, ImportRun) == 0

    from test_imports_api import post

    importacion = await post(api, xlsx([fila()]))
    assert importacion.status_code == 200, importacion.text
    assert importacion.json()["status"] == "completed"
    assert await count(sessions, OfferSnapshot) == 1


# ---------- problemas explicados y agrupados ----------


async def test_revision_agrupa_problemas_y_unidades_desconocidas(api, sessions):
    content = xlsx(
        [
            fila(),
            fila(sku="SIN-MAPEO"),
            fila(sku="SKU-002", precio="n/a"),
            fila(sku="SKU-003", u="FCO"),
            fila(sku="SKU-004", u="FCO"),
        ]
    )
    r = await revisar(api, content)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["rows_total"], body["rows_accepted"], body["rows_rejected"]) == (5, 1, 4)
    grupos = {p["code"]: p for p in body["problems"]}
    assert grupos["UNKNOWN_UNIT"]["count"] == 2
    assert grupos["UNKNOWN_UNIT"]["sample_rows"] == [5, 6]
    assert grupos["UNMAPPED_PRODUCT"]["count"] == 1
    assert grupos["INVALID_PRICE"]["count"] == 1
    unidades = {u["text"]: u["count"] for u in body["unrecognised_units"]}
    assert unidades == {"FCO": 2}
    rechazada = next(row for row in body["rows"] if row["sku"] == "SIN-MAPEO")
    assert rechazada["status"] == "rejected"
    assert rechazada["problem"]["fields"] == ["sku"]

    # Ninguna de esas filas dejó rastro.
    assert await count(sessions, OfferSnapshot) == 0


async def test_revision_marca_condiciones_como_pendiente_de_revision(api):
    header = ["Código", "Precio", "Unidad", "Condiciones"]
    profile = {
        **PROFILE,
        "columns": {
            "sku": {"header": "Código"},
            "price": {"header": "Precio"},
            "sale_unit": {"header": "Unidad"},
            "conditions": {"header": "Condiciones"},
        },
        "units": {"CAJA": "box"},
        "options": {"currency": "CLP", "price_basis": "per_commercial_unit"},
    }
    content = xlsx([["SKU-001", "1990", "CAJA", "Pago a 30 días"]], header=header)
    r = await revisar(api, content, profile=profile)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows_accepted"] == 1
    assert body["rows_needs_review"] == 1
    assert body["rows"][0]["status"] == "review"


# ---------- perfil guardado, perfil enviado y compatibilidad ----------


async def test_revision_usa_el_perfil_guardado_si_no_se_envia(api):
    r = await revisar(api, xlsx([fila()]), profile=None)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile_source"] == "saved"
    assert body["profile_version"] == 1


async def test_revision_falla_si_no_hay_mapeo(api):
    r = await revisar(api, xlsx([fila()]), source=api.farm["source_2"], profile=None)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "IMPORT_FORMAT_INVALID"
    assert "mapeo de columnas" in r.json()["error"]["message"]


async def test_revision_detecta_que_el_archivo_cambio_de_columnas(api, sessions):
    """El perfil guardado busca por nombre: si el archivo cambió, no se aplica por posición."""
    header = ["Código", "Descripción", "Precio final", "Moneda", "Base", "Unidad"]
    content = xlsx(
        [["SKU-001", "LOSARTAN", "12345.60", "CLP", "per_commercial_unit", "CAJA"]], header=header
    )
    r = await revisar(api, content, profile=None)
    assert r.status_code == 422
    assert "cambió" in r.json()["error"]["message"]
    assert await count(sessions, ImportRun) == 0


async def test_revision_acepta_mapeo_por_posicion(api):
    """Compatibilidad hacia atrás: los perfiles antiguos sin nombre siguen funcionando."""
    profile = {
        **PROFILE,
        "columns": {
            "sku": {"position": 1},
            "name": {"position": 2},
            "price": {"position": 3},
            "currency": {"position": 4},
            "basis": {"position": 5},
            "sale_unit": {"position": 6},
        },
    }
    r = await revisar(api, xlsx([fila()]), profile=profile)
    assert r.status_code == 200, r.text
    assert r.json()["rows_accepted"] == 1


async def test_revision_mapeo_invalido_no_escribe(api, sessions):
    profile = {**PROFILE, "columns": {"sku": {"header": "Código"}}}
    r = await revisar(api, xlsx([fila()]), profile=profile)
    assert r.status_code == 422
    assert await count(sessions, ImportRun) == 0


# ---------- hoja de productos ----------


async def test_detecta_la_hoja_de_productos_entre_varias(api):
    content = xlsx_hojas(
        {
            "Portada": [["LISTA DE PRECIOS"], ["Proveedor ejemplo"]],
            "Precios": [HEADER, fila()],
        }
    )
    r = await revisar(api, content, profile={**PROFILE, "sheet": None})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sheet"] == "Precios"
    assert body["rows_accepted"] == 1


async def test_vista_previa_propone_la_hoja_de_productos(api):
    content = xlsx_hojas(
        {
            "Portada": [["LISTA DE PRECIOS"]],
            "Precios": [HEADER, fila()],
        }
    )
    r = await api.post(
        f"/api/v1/sources/{api.farm['source']}/import-preview",
        files={
            "file": (
                "lista.xlsx",
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={},
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["suggested_sheet"] == "Precios"
    assert body["sheet"] == "Precios"


# ---------- permisos y aislamiento ----------


async def test_revision_solo_administracion(api, sessions):
    r = await revisar(api, xlsx([fila()]), who="aux")
    assert r.status_code == 403
    assert await count(sessions, ImportRun) == 0


async def test_revision_no_alcanza_una_fuente_ajena(api):
    r = await revisar(api, xlsx([fila()]), source=api.farm["source_ajena"])
    assert r.status_code == 404


async def test_revision_rechaza_una_fuente_de_cadena(api):
    r = await revisar(api, xlsx([fila()]), source=api.farm["chain"])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_SOURCE"


async def test_revision_valida_el_mapeo_enviado(api):
    r = await api.post(
        f"/api/v1/sources/{api.farm['source']}/import-check",
        files={"file": ("lista.xlsx", xlsx([fila()]), "application/vnd.ms-excel")},
        data={"profile": "{no es json}"},
        headers=H(api),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "IMPORT_FORMAT_INVALID"


async def test_revision_respeta_el_limite_de_tamano(api):
    r = await api.post(
        f"/api/v1/sources/{api.farm['source']}/import-check",
        files={"file": ("lista.xlsx", b"x" * (10 * 1024 * 1024 + 1), "application/vnd.ms-excel")},
        data={"profile": json.dumps(PROFILE)},
        headers=H(api),
    )
    assert r.status_code == 413


async def test_revision_no_expone_la_fila_completa(api):
    """De una fila rechazada solo se devuelven SKU, nombre y las celdas culpables (D-104)."""
    r = await revisar(api, xlsx([fila(sku="SIN-MAPEO", desc="NOMBRE LARGO")]))
    assert r.status_code == 200, r.text
    rechazada = r.json()["rows"][0]
    assert set(rechazada) == {
        "row",
        "status",
        "sku",
        "name",
        "price",
        "currency",
        "price_basis",
        "sale_unit",
        "sale_unit_text",
        "pack_units",
        "stock",
        "observed_at",
        "warnings",
        "problem",
    }
    assert rechazada["problem"]["values"] == {"sku": "SIN-MAPEO"}
    assert len(r.json()["file_hash"]) == 64
