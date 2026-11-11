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
        # Identidad: columna publicada o descripción reconocida; en una fila rechazada viajan
        # como None y sin estado (D-150).
        "laboratory",
        "brand",
        "identity_status",
        "laboratory_source",
        "brand_source",
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


# ---------- modo automático (un solo botón) ----------


async def test_auto_detecta_el_mapeo_sin_perfil_y_no_escribe(api, sessions):
    r = await revisar(api, xlsx([fila()]), source=api.farm["source_2"], profile=None, auto="true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile_source"] == "detected"
    assert body["profile_version"] == 0
    assert body["missing_columns"] == []
    assert body["rows_total"] == 1
    columnas = body["profile"]["columns"]
    assert columnas["sku"] == {"header": "Código", "position": 1}
    assert columnas["price"] == {"header": "Precio", "position": 3}
    # Moneda y base no se inventan: la revisión las reporta como problema de cada fila.
    assert "currency" in columnas and "basis" in columnas
    assert body["profile"]["options"] == {"decimal_separator": "."}
    assert await count(sessions, ImportRun) == 0
    assert await count(sessions, ImportProfile) == 1  # solo el perfil de la fixture


async def test_auto_reutiliza_el_perfil_guardado_compatible(api):
    r = await revisar(api, xlsx([fila()]), profile=None, auto="true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile_source"] == "saved"
    assert body["profile_version"] == 1
    assert body["profile"]["columns"] == COLUMNS
    assert body["rows_accepted"] == 1


async def test_auto_no_reutiliza_un_perfil_por_posicion(api, sessions):
    posiciones = {
        rol: {"position": indice}
        for indice, rol in enumerate(
            ["sku", "name", "price", "currency", "basis", "sale_unit"], start=1
        )
    }
    guardado = await api.put(
        f"/api/v1/sources/{api.farm['source_2']}/import-profile",
        json={
            "file_format": "xlsx",
            "sheet": None,
            "header_row": 1,
            "columns": posiciones,
            "units": {},
            "options": {},
        },
        headers={**H(api), "If-Match": '"0"'},
    )
    assert guardado.status_code == 200, guardado.text

    r = await revisar(api, xlsx([fila()]), source=api.farm["source_2"], profile=None, auto="true")
    assert r.status_code == 200, r.text
    body = r.json()
    # Un mapeo solo-posición no se da por compatible: se detecta por nombre.
    assert body["profile_source"] == "detected"
    assert all("header" in spec for spec in body["profile"]["columns"].values())

    async with sessions() as s:
        version = (
            await s.execute(
                select(ImportProfile.version).where(ImportProfile.source_id == api.farm["source_2"])
            )
        ).scalar_one()
    assert version == 1  # la revisión no tocó el perfil guardado


async def test_auto_cae_a_deteccion_si_el_archivo_cambio(api, sessions):
    header = ["Código", "Descripción", "Precio final", "Moneda", "Base", "Unidad"]
    content = xlsx([["SKU-001", "LOSARTAN", "12345.60", "CLP", "x", "CAJA"]], header=header)
    r = await revisar(api, content, profile=None, auto="true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile_source"] == "detected"
    assert body["profile"]["columns"]["price"]["header"] == "Precio final"
    # Sin `auto` el mismo archivo sigue fallando: el cambio es aditivo.
    sin_auto = await revisar(api, content, profile=None)
    assert sin_auto.status_code == 422
    assert await count(sessions, ImportRun) == 0


async def test_auto_informa_columnas_faltantes_en_vez_de_fallar(api, sessions):
    sin_codigo = await revisar(
        api,
        xlsx(
            [["LOSARTAN", "12345.60", "CLP", "per_commercial_unit", "CAJA"]],
            header=["Descripción", "Precio", "Moneda", "Base", "Unidad"],
        ),
        source=api.farm["source_2"],
        profile=None,
        auto="true",
    )
    assert sin_codigo.status_code == 200, sin_codigo.text
    assert sin_codigo.json()["missing_columns"] == ["sku"]
    assert sin_codigo.json()["rows_total"] == 0

    sin_unidad = await revisar(
        api,
        xlsx(
            [["SKU-001", "LOSARTAN", "12345.60", "CLP", "per_commercial_unit"]],
            header=["Código", "Descripción", "Precio", "Moneda", "Base"],
        ),
        source=api.farm["source_2"],
        profile=None,
        auto="true",
    )
    assert sin_unidad.status_code == 200, sin_unidad.text
    cuerpo = sin_unidad.json()
    assert cuerpo["missing_columns"] == ["sale_unit"]

    # La pantalla pregunta la unidad por defecto y reintenta con el mapeo devuelto.
    perfil = cuerpo["profile"]
    perfil["options"] = {**perfil["options"], "default_sale_unit": "box"}
    con_unidad = await revisar(
        api, xlsx([fila()]), source=api.farm["source_2"], profile=perfil, auto="true"
    )
    assert con_unidad.status_code == 200, con_unidad.text
    assert con_unidad.json()["missing_columns"] == []
    assert con_unidad.json()["rows_total"] == 1
    assert await count(sessions, ImportRun) == 0


async def test_auto_revision_y_importacion_leen_la_misma_hoja(api, sessions):
    """El perfil devuelto por la revisión, guardado tal cual, hace que E8 lea la misma hoja."""
    async with sessions() as s, s.begin():
        item_id = (
            await s.execute(
                select(TradeItem.id).where(TradeItem.organization_id == api.farm["org"])
            )
        ).scalar_one()
        s.add(
            SupplierProductMapping(
                organization_id=api.farm["org"],
                source_id=api.farm["source_2"],
                supplier_id=api.farm["supplier"],
                trade_item_id=item_id,
                external_id="SKU-001",
                origin="explicit",
                data_origin="simulated",
            )
        )

    content = xlsx_hojas({"Portada": [["LISTA DE PRECIOS"]], "Precios": [HEADER, fila()]})
    revision = await revisar(api, content, source=api.farm["source_2"], profile=None, auto="true")
    assert revision.status_code == 200, revision.text
    body = revision.json()
    assert body["sheet"] == "Precios"
    assert body["profile"]["sheet"] == body["sheet"]
    assert body["profile"]["header_row"] == body["header_row"]
    # La detección no inventa traducciones de unidad: es la pantalla la que pregunta y guarda
    # «CAJA = caja». Se simula esa respuesta para que la fila sea válida al importar.
    body["profile"]["units"] = {"CAJA": "box"}

    guardado = await api.put(
        f"/api/v1/sources/{api.farm['source_2']}/import-profile",
        json=body["profile"],
        headers={**H(api), "If-Match": '"0"'},
    )
    assert guardado.status_code == 200, guardado.text

    importacion = await api.post(
        "/api/v1/imports",
        files={
            "file": (
                "lista.xlsx",
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={
            "supplier_id": str(api.farm["supplier"]),
            "source_id": str(api.farm["source_2"]),
        },
        headers=H(api, key="auto-misma-hoja-01"),
    )
    assert importacion.status_code == 200, importacion.text
    salida = importacion.json()
    assert (salida["status"], salida["rows_accepted"]) == ("completed", 1)


# ---------- listas reales: cabeceras canónicas, unidades y base por proveedor ----------

FARMA_HEADER = [
    "sku",
    "name",
    "presentation",
    "barcode",
    "category",
    "sale_unit",
    "currency",
    "price",
    "net_price",
    "stock",
    "minimum",
    "multiple",
    "pack",
    "basis",
    "observed_at",
    "conditions",
]


async def mapear(api, sessions, source_key, *external_ids):
    """Vincula los SKU de la prueba a un artículo existente; crear artículos no es el tema aquí."""
    async with sessions() as s, s.begin():
        item_id = (
            await s.execute(
                select(TradeItem.id).where(TradeItem.organization_id == api.farm["org"])
            )
        ).scalar_one()
        for external_id in external_ids:
            s.add(
                SupplierProductMapping(
                    organization_id=api.farm["org"],
                    source_id=api.farm[source_key],
                    supplier_id=api.farm["supplier"],
                    trade_item_id=item_id,
                    external_id=external_id,
                    origin="explicit",
                    data_origin="simulated",
                )
            )


def farma_row(**over):
    values = {
        "sku": "PAR500-20",
        "name": "PARACETAMOL 500 MG X 20",
        "presentation": "Caja x 20 comprimidos",
        "barcode": "780000000001",
        "category": "Analgésico",
        "sale_unit": "caja",
        "currency": "CLP",
        "price": "1012",
        "net_price": "850",
        "stock": "10",
        "minimum": "1",
        "multiple": "1",
        "pack": "20",
        "basis": "per_commercial_unit",
        "observed_at": "2026-11-05",
        "conditions": "Retiro en 24 horas",
    }
    values.update(over)
    return [values[columna] for columna in FARMA_HEADER]


async def test_auto_reconoce_cabeceras_canonicas_y_no_confunde_presentacion_con_unidad(
    api, sessions
):
    await mapear(api, sessions, "source_2", "PAR500-20")
    content = xlsx([farma_row()], header=FARMA_HEADER)
    r = await revisar(api, content, source=api.farm["source_2"], profile=None, auto="true")
    assert r.status_code == 200, r.text
    body = r.json()
    columnas = body["profile"]["columns"]

    assert columnas["sku"]["header"] == "sku"
    assert columnas["name"]["header"] == "name"
    assert columnas["sale_unit"]["header"] == "sale_unit"
    assert columnas["currency"]["header"] == "currency"
    # «price» exacta gana a «net_price»: no se elige por posición ni por orden alfabético.
    assert columnas["price"]["header"] == "price"
    assert columnas["minimum"]["header"] == "minimum"
    assert columnas["observed_at"]["header"] == "observed_at"
    assert columnas["conditions"]["header"] == "conditions"
    # La presentación describe el producto; no es la unidad comercial.
    assert {spec["header"] for spec in columnas.values()} >= {"sku", "sale_unit"}
    assert "presentation" not in {spec["header"] for spec in columnas.values()}
    assert "barcode" not in {spec["header"] for spec in columnas.values()}

    # Las unidades inequívocas se traducen solas: no se pregunta una unidad global.
    assert body["profile"]["units"] == {"caja": "box"}
    assert body["missing_columns"] == []
    assert (body["rows_total"], body["rows_accepted"], body["rows_rejected"]) == (1, 1, 0)
    assert body["unrecognised_units"] == []

    # `pack` se reconoce, pero sin unidad base no se puede guardar: se omite y se explica.
    assert "pack" not in columnas
    assert [c["role"] for c in body["ignored_columns"]] == ["pack"]
    assert body["ignored_columns"][0]["header"] == "pack"
    assert "unidad" in body["ignored_columns"][0]["reason"]


async def test_auto_traduce_unidades_conocidas_y_rechaza_solo_la_fila_desconocida(api, sessions):
    await mapear(api, sessions, "source_2", "SKU-001", "SKU-002", "SKU-003")
    header = ["Código", "Descripción", "Precio", "Moneda", "Base", "Unidad"]
    content = xlsx(
        [
            ["SKU-001", "LOSARTAN", "1000", "CLP", "per_commercial_unit", "caja"],
            ["SKU-002", "IBUPROFENO", "2000", "CLP", "per_commercial_unit", "unidad"],
            ["SKU-003", "PARACETAMOL", "3000", "CLP", "per_commercial_unit", "FCO"],
        ],
        header=header,
    )
    r = await revisar(api, content, source=api.farm["source_2"], profile=None, auto="true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile"]["units"] == {"caja": "box", "unidad": "unit"}
    # La columna de unidad existe: nunca se pregunta una unidad global por unas pocas filas.
    assert body["missing_columns"] == []
    assert (body["rows_total"], body["rows_accepted"], body["rows_rejected"]) == (3, 2, 1)
    assert [u["text"] for u in body["unrecognised_units"]] == ["FCO"]
    rechazada = next(row for row in body["rows"] if row["status"] == "rejected")
    assert rechazada["sku"] == "SKU-003"
    assert rechazada["problem"]["code"] == "UNKNOWN_UNIT"


async def test_auto_base_ambigua_se_confirma_por_proveedor_y_no_se_generaliza(api, sessions):
    await mapear(api, sessions, "source_2", "PAR500-20", "IBU400-20")
    await mapear(api, sessions, "source", "PAR500-20", "IBU400-20")
    header = ["sku", "name", "sale_unit", "currency", "price", "basis"]
    contenido = xlsx(
        [
            ["PAR500-20", "PARACETAMOL 500", "caja", "CLP", "1012", "unit"],
            ["IBU400-20", "IBUPROFENO 400", "unidad", "CLP", "800", "unit"],
        ],
        header=header,
    )

    inicial = await revisar(api, contenido, source=api.farm["source_2"], profile=None, auto="true")
    assert inicial.status_code == 200, inicial.text
    cuerpo = inicial.json()
    # «unit» a secas no se interpreta solo: se pregunta como aclaración comercial.
    assert cuerpo["rows_rejected"] == 2
    assert cuerpo["unrecognised_bases"] == [{"text": "unit", "count": 2, "sample_rows": [2, 3]}]

    perfil = cuerpo["profile"]
    perfil["options"] = {
        **perfil["options"],
        "basis_values": {"unit": "per_commercial_unit"},
    }
    confirmada = await revisar(
        api, contenido, source=api.farm["source_2"], profile=perfil, auto="true"
    )
    assert confirmada.status_code == 200, confirmada.text
    assert confirmada.json()["rows_accepted"] == 2
    assert confirmada.json()["unrecognised_bases"] == []

    guardado = await api.put(
        f"/api/v1/sources/{api.farm['source_2']}/import-profile",
        json=perfil,
        headers={**H(api), "If-Match": '"0"'},
    )
    assert guardado.status_code == 200, guardado.text
    reutilizada = await revisar(
        api, contenido, source=api.farm["source_2"], profile=None, auto="true"
    )
    cuerpo_reutilizado = reutilizada.json()
    assert cuerpo_reutilizado["profile_source"] == "saved"
    assert cuerpo_reutilizado["rows_accepted"] == 2
    # La aclaración quedó guardada en el perfil de ESA fuente: no se vuelve a preguntar.
    assert cuerpo_reutilizado["unrecognised_bases"] == []
    assert cuerpo_reutilizado["profile"]["options"]["basis_values"] == {
        "unit": "per_commercial_unit"
    }

    # La equivalencia vive en el perfil de ESA fuente: la lista de otro proveedor no la hereda.
    otra = await revisar(api, contenido, profile=None, auto="true")
    assert otra.status_code == 200, otra.text
    assert otra.json()["unrecognised_bases"] == [
        {"text": "unit", "count": 2, "sample_rows": [2, 3]}
    ]


async def test_auto_omite_pack_sin_unidad_base_y_lo_deja_en_diagnostico(api, sessions):
    await mapear(api, sessions, "source_2", "PAR500-20")
    header = ["sku", "sale_unit", "currency", "price", "pack", "basis"]
    content = xlsx(
        [["PAR500-20", "caja", "CLP", "1012", "20", "per_commercial_unit"]], header=header
    )
    r = await revisar(api, content, source=api.farm["source_2"], profile=None, auto="true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "pack" not in body["profile"]["columns"]
    assert body["ignored_columns"][0]["role"] == "pack"
    assert body["rows_accepted"] == 1, body["problems"]
    assert body["rows"][0]["pack_units"] is None


async def test_auto_no_depende_del_orden_de_las_columnas(api):
    header = ["Precio", "Código", "Unidad", "Descripción"]
    fila_invertida = ["1500", "SKU-009", "caja", "IBUPROFENO 400"]
    r = await revisar(
        api,
        xlsx([fila_invertida], header=header),
        source=api.farm["source_2"],
        profile=None,
        auto="true",
    )
    assert r.status_code == 200, r.text
    columnas = r.json()["profile"]["columns"]
    assert columnas["sku"] == {"header": "Código", "position": 2}
    assert columnas["price"] == {"header": "Precio", "position": 1}
    assert columnas["sale_unit"] == {"header": "Unidad", "position": 3}
    assert columnas["name"] == {"header": "Descripción", "position": 4}


async def test_auto_unidad_vacia_rechaza_su_fila_sin_preguntar_por_todo_el_archivo(api, sessions):
    await mapear(api, sessions, "source_2", "SKU-001", "SKU-002")
    header = ["Código", "Precio", "Moneda", "Unidad", "Base"]
    content = xlsx(
        [
            ["SKU-001", "1000", "CLP", "caja", "per_commercial_unit"],
            ["SKU-002", "2000", "CLP", "", "per_commercial_unit"],
        ],
        header=header,
    )
    r = await revisar(api, content, source=api.farm["source_2"], profile=None, auto="true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["missing_columns"] == []
    assert (body["rows_accepted"], body["rows_rejected"]) == (1, 1)
    rechazada = next(row for row in body["rows"] if row["status"] == "rejected")
    assert rechazada["problem"]["code"] == "MISSING_REQUIRED_FIELD"
    assert rechazada["problem"]["fields"] == ["sale_unit"]
