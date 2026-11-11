"""Laboratorio y marca en las listas de proveedores (E8/A3, D-150).

Reglas que se verifican aquí:

* Primero la columna propia del proveedor: lo publicado se guarda **tal cual** y nunca se
  sobrescribe con nada deducido.
* Si la columna falta (o la celda viene vacía), se revisa la **descripción original**: solo se
  reconoce una marca explícita («LAB.», «LABORATORIO», «MARCA») seguida de un nombre que ya existe
  en el vocabulario controlado (columnas del propio archivo, catálogo propio, listas anteriores).
  Sin marca o sin nombre confirmado no se asigna nada: no hay parecidos ni suposiciones.
* La descripción original nunca se modifica, y la procedencia (`column`/`description`) y el estado
  de identificación (`published`/`extracted`/`unconfirmed`) quedan guardados en
  `offer_snapshot.evidence.identity`, sin tocar el esquema.
* Una oferta extraída o sin identidad sigue visible y alimenta el motor de preferencias, pero la
  pantalla puede decir que está pendiente: no se presenta como completa.

Las planillas de este archivo son `fixture`: se generan aquí, no son de ningún proveedor real.
"""

from io import BytesIO

import pytest
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook
from sqlalchemy import select

from app.api.deps import get_session
from app.core.config import get_settings
from app.main import create_app
from app.models import (
    AppUser,
    CommercialObservation,
    CommercialPreference,
    ImportProfile,
    ObservationInterpretation,
    OfferSnapshot,
    Organization,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
)
from app.services.actors import load_actor
from app.services.import_profiles import check_import
from app.services.imports import run_import

pytestmark = pytest.mark.db

HEADER = ["Código", "Descripción", "Precio", "Moneda", "Base", "Unidad", "Fecha"]
COLUMNS = {
    "sku": {"header": "Código"},
    "name": {"header": "Descripción"},
    "price": {"header": "Precio"},
    "currency": {"header": "Moneda"},
    "basis": {"header": "Base"},
    "sale_unit": {"header": "Unidad"},
    "observed_at": {"header": "Fecha"},
}
UNITS = {"CAJA": "box", "UNIDAD": "unit"}
OPTIONS = {
    "decimal_separator": ",",
    "thousands_separator": ".",
    "date_format": "DD/MM/YYYY",
    "tax_included": False,
}
BASE_VALUES = {
    "Código": "SKU-001",
    "Descripción": "LOSARTAN 50 MG X 30",
    "Precio": "12.345,60",
    "Moneda": "CLP",
    "Base": "per_commercial_unit",
    "Unidad": "CAJA",
    "Fecha": "15/10/2026",
    # Columnas opcionales que solo algunas planillas traen: por defecto vacías (no informadas).
    "Laboratorio": "",
    "Marca": "",
}


def filas(header, *overrides):
    """Filas de planilla en el orden de `header`; lo no indicado usa `BASE_VALUES`."""
    out = []
    for over in overrides:
        values = {**BASE_VALUES, **over}
        out.append([values[name] for name in header])
    return out


def xlsx(headers, *overrides) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Lista"
    ws.append(headers)
    for row in filas(headers, *overrides):
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
async def farm(sessions):
    """Organización `simulation` con una administradora, dos proveedores y un artículo común."""
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia identidad de lista", kind="simulation")
        s.add(org)
        await s.flush()
        ids["org"] = org.id
        admin = AppUser(organization_id=org.id, display_name="Admin", roles=["admin"])
        s.add(admin)
        await s.flush()
        ids["admin"] = admin.id
        for key, name in (("uno", "Distribuidora Andes"), ("dos", "Distribuidora Pacífico")):
            sup = Supplier(organization_id=org.id, name=name, data_origin="simulated")
            s.add(sup)
            await s.flush()
            ids[key] = sup.id
        item = TradeItem(
            organization_id=org.id,
            identity_key="fixture-losartan-50-30",
            display_name="Losartán 50 mg x 30 comprimidos",
            data_origin="simulated",
        )
        s.add(item)
        await s.flush()
        ids["item"] = item.id
    return ids


async def fuente(
    sessions,
    farm,
    *,
    supplier="uno",
    nombre="Lista Andes",
    skus=("SKU-001",),
    columns=None,
    sheet="Lista",
):
    """Fuente de proveedor con sus vínculos SKU→artículo y, si se indica, perfil guardado."""
    async with sessions() as s, s.begin():
        src = Source(
            organization_id=farm["org"],
            kind="supplier",
            supplier_id=farm[supplier],
            name=nombre,
            mode="manual_import",
            data_origin="simulated",
        )  # fmt: skip
        s.add(src)
        await s.flush()
        for sku in skus:
            s.add(
                SupplierProductMapping(
                    organization_id=farm["org"],
                    source_id=src.id,
                    supplier_id=farm[supplier],
                    trade_item_id=farm["item"],
                    external_id=sku,
                    origin="explicit",
                    data_origin="simulated",
                )
            )
        if columns is not None:
            s.add(
                ImportProfile(
                    organization_id=farm["org"],
                    source_id=src.id,
                    sheet=sheet,
                    header_row=1,
                    columns=columns,
                    units=UNITS,
                    options=OPTIONS,
                    data_origin="simulated",
                )
            )
        await s.flush()
        return src.id


async def run(sessions, who, source_id, content):
    async with sessions() as s:
        actor = await load_actor(s, who)
        try:
            outcome = await run_import(
                s, actor, source_id=source_id, file_name="lista.xlsx", content=content
            )
            await s.commit()
            return outcome
        except BaseException:
            await s.rollback()
            raise


async def offers(sessions) -> dict[str, OfferSnapshot]:
    async with sessions() as s:
        rows = (await s.execute(select(OfferSnapshot))).scalars().all()
        return {row.external_id: row for row in rows}


def identidad(offer: OfferSnapshot) -> dict:
    """Procedencia y estado de identidad guardados en la evidencia de la oferta (D-150)."""
    return (offer.evidence or {}).get("identity") or {}


# --------------------------------------------------------------------------- columnas propias


async def test_laboratorio_en_columna_propia_se_guarda_tal_cual(sessions, farm):
    headers = [*HEADER, "Laboratorio"]
    columnas = {**COLUMNS, "laboratory": {"header": "Laboratorio"}}
    source_id = await fuente(sessions, farm, columns=columnas)

    outcome = await run(
        sessions,
        farm["admin"],
        source_id,
        xlsx(headers, {"Laboratorio": "Laboratorios Andes Ficticios"}),
    )

    assert (outcome.rows_total, outcome.rows_accepted, outcome.rows_rejected) == (1, 1, 0)
    offer = (await offers(sessions))["SKU-001"]
    assert offer.laboratory_as_published == "Laboratorios Andes Ficticios"
    assert offer.brand_as_published is None
    # La procedencia queda registrada: el dato vino de una columna, no de la descripción.
    assert identidad(offer) == {
        "status": "published",
        "laboratory": {"source": "column", "matched_text": None},
        "brand": None,
    }
    # La descripción original del proveedor queda intacta.
    assert offer.external_name == "LOSARTAN 50 MG X 30"
    assert offer.review_status == "accepted"


async def test_marca_en_columna_propia_se_guarda_tal_cual(sessions, farm):
    headers = [*HEADER, "Marca"]
    columnas = {**COLUMNS, "brand": {"header": "Marca"}}
    source_id = await fuente(sessions, farm, columns=columnas)

    outcome = await run(
        sessions,
        farm["admin"],
        source_id,
        xlsx(headers, {"Marca": "Marca Alfa Ficticia"}),
    )

    assert (outcome.rows_accepted, outcome.rows_rejected) == (1, 0)
    offer = (await offers(sessions))["SKU-001"]
    assert offer.brand_as_published == "Marca Alfa Ficticia"
    assert offer.laboratory_as_published is None
    assert offer.external_name == "LOSARTAN 50 MG X 30"


async def test_laboratorio_y_marca_en_columnas_distintas_no_se_mezclan(sessions, farm):
    headers = [*HEADER, "Laboratorio", "Marca"]
    columnas = {
        **COLUMNS,
        "laboratory": {"header": "Laboratorio"},
        "brand": {"header": "Marca"},
    }
    source_id = await fuente(sessions, farm, columns=columnas)

    outcome = await run(
        sessions,
        farm["admin"],
        source_id,
        xlsx(
            headers,
            {"Laboratorio": "Laboratorios Andes Ficticios", "Marca": "Marca Alfa Ficticia"},
        ),
    )

    assert (outcome.rows_accepted, outcome.rows_rejected) == (1, 0)
    offer = (await offers(sessions))["SKU-001"]
    assert offer.laboratory_as_published == "Laboratorios Andes Ficticios"
    assert offer.brand_as_published == "Marca Alfa Ficticia"


async def test_una_celda_vacia_es_no_informado_y_no_copia_la_descripcion(sessions, farm):
    headers = [*HEADER, "Laboratorio", "Marca"]
    columnas = {
        **COLUMNS,
        "laboratory": {"header": "Laboratorio"},
        "brand": {"header": "Marca"},
    }
    source_id = await fuente(sessions, farm, columns=columnas)

    outcome = await run(
        sessions,
        farm["admin"],
        source_id,
        xlsx(headers, {"Laboratorio": "", "Marca": ""}),
    )

    assert (outcome.rows_accepted, outcome.rows_rejected) == (1, 0)
    offer = (await offers(sessions))["SKU-001"]
    assert offer.laboratory_as_published is None
    assert offer.brand_as_published is None
    assert offer.external_name == "LOSARTAN 50 MG X 30"


# --------------------------------------------------------------------------- listas anteriores


async def test_una_lista_sin_columnas_de_identidad_se_importa_igual(sessions, farm):
    """Compatibilidad: las listas que ya funcionaban no se rechazan por no traer laboratorio ni
    marca; los campos quedan NULL y no se inventa nada."""
    source_id = await fuente(sessions, farm, columns=COLUMNS)

    outcome = await run(sessions, farm["admin"], source_id, xlsx(HEADER, {}))

    assert (outcome.rows_accepted, outcome.rows_rejected) == (1, 0)
    offer = (await offers(sessions))["SKU-001"]
    assert (offer.laboratory_as_published, offer.brand_as_published) == (None, None)
    assert offer.review_status == "accepted"
    assert offer.external_name == "LOSARTAN 50 MG X 30"


async def test_sin_vocabulario_confirmado_la_descripcion_no_se_interpreta(sessions, farm):
    """Una marca explícita en la descripción no basta: si el nombre no está en el vocabulario
    controlado, el dato es desconocido. La descripción se conserva exactamente como la entregó el
    proveedor."""
    source_id = await fuente(sessions, farm, columns=COLUMNS)
    descripcion = "LOSARTAN 50 MG X 30 - LABORATORIO ANDES - MARCA ALFA"

    outcome = await run(
        sessions, farm["admin"], source_id, xlsx(HEADER, {"Descripción": descripcion})
    )

    assert (outcome.rows_accepted, outcome.rows_rejected) == (1, 0)
    offer = (await offers(sessions))["SKU-001"]
    assert offer.external_name == descripcion
    assert offer.laboratory_as_published is None
    assert offer.brand_as_published is None
    assert identidad(offer)["status"] == "unconfirmed"


# --------------------------------------------------------------------------- descripción publicada


async def test_laboratorio_en_la_descripcion_se_reconoce_con_el_vocabulario_del_archivo(
    sessions, farm
):
    """Si la columna falta, la descripción con «LAB.» y un nombre que otra fila ya publicó permite
    reconocer el laboratorio; el texto exacto del reconocimiento queda en la evidencia."""
    headers = [*HEADER, "Laboratorio"]
    columnas = {**COLUMNS, "laboratory": {"header": "Laboratorio"}}
    source_id = await fuente(sessions, farm, skus=("SKU-001", "SKU-002"), columns=columnas)
    descripcion = "LOSARTAN 50 MG X 30 - LAB. CORDILLERA FICTICIO"

    outcome = await run(
        sessions,
        farm["admin"],
        source_id,
        xlsx(
            headers,
            {"Código": "SKU-001", "Laboratorio": "Lab. Cordillera Ficticio"},
            {"Código": "SKU-002", "Descripción": descripcion, "Laboratorio": ""},
        ),
    )

    assert (outcome.rows_total, outcome.rows_accepted, outcome.rows_rejected) == (2, 2, 0)
    extraida = (await offers(sessions))["SKU-002"]
    # Se guarda el nombre canónico del vocabulario, no el texto recortado de la descripción.
    assert extraida.laboratory_as_published == "Lab. Cordillera Ficticio"
    assert extraida.brand_as_published is None
    assert extraida.external_name == descripcion
    assert identidad(extraida) == {
        "status": "extracted",
        "laboratory": {"source": "description", "matched_text": "LAB. CORDILLERA FICTICIO"},
        "brand": None,
    }


async def test_marca_en_la_descripcion_se_reconoce_sin_suponer_el_laboratorio(sessions, farm):
    headers = [*HEADER, "Marca"]
    columnas = {**COLUMNS, "brand": {"header": "Marca"}}
    source_id = await fuente(sessions, farm, skus=("SKU-001", "SKU-002"), columns=columnas)
    descripcion = "SALBUTAMOL AEROSOL 100 MCG X 200 DOSIS - MARCA ALFA FICTICIA"

    outcome = await run(
        sessions,
        farm["admin"],
        source_id,
        xlsx(
            headers,
            {"Código": "SKU-001", "Marca": "Marca Alfa Ficticia"},
            {"Código": "SKU-002", "Descripción": descripcion, "Marca": ""},
        ),
    )

    assert (outcome.rows_accepted, outcome.rows_rejected) == (2, 0)
    extraida = (await offers(sessions))["SKU-002"]
    assert extraida.brand_as_published == "Marca Alfa Ficticia"
    # La descripción del producto no menciona laboratorio: queda desconocido, no se completa.
    assert extraida.laboratory_as_published is None
    assert identidad(extraida)["brand"] == {
        "source": "description",
        "matched_text": "MARCA ALFA FICTICIA",
    }
    assert identidad(extraida)["laboratory"] is None


async def test_una_columna_publicada_nunca_se_sobrescribe(sessions, farm):
    """La columna manda aunque la descripción mencione otro nombre confirmado del vocabulario."""
    headers = [*HEADER, "Laboratorio"]
    columnas = {**COLUMNS, "laboratory": {"header": "Laboratorio"}}
    source_id = await fuente(sessions, farm, skus=("SKU-001", "SKU-002"), columns=columnas)

    outcome = await run(
        sessions,
        farm["admin"],
        source_id,
        xlsx(
            headers,
            {"Código": "SKU-001", "Laboratorio": "Laboratorio Sur Ficticio"},
            {
                "Código": "SKU-002",
                "Descripción": "LOSARTAN 50 MG X 30 - LAB. SUR FICTICIO",
                "Laboratorio": "Laboratorios Andes Ficticios",
            },
        ),
    )

    assert (outcome.rows_accepted, outcome.rows_rejected) == (2, 0)
    oferta = (await offers(sessions))["SKU-002"]
    assert oferta.laboratory_as_published == "Laboratorios Andes Ficticios"
    assert identidad(oferta)["laboratory"] == {"source": "column", "matched_text": None}


async def test_un_nombre_ambiguo_sin_marca_explicita_no_se_interpreta(sessions, farm):
    """«LINEA CORDILLERA», «ANDES» o «LAB/MARCA SUR» no identifican nada por sí solos: sin marca
    explícita y sin coincidencia exacta con el vocabulario no hay dato."""
    headers = [*HEADER, "Laboratorio", "Marca"]
    columnas = {
        **COLUMNS,
        "laboratory": {"header": "Laboratorio"},
        "brand": {"header": "Marca"},
    }
    source_id = await fuente(
        sessions, farm, skus=("SKU-001", "SKU-002", "SKU-003", "SKU-004"), columns=columnas
    )

    outcome = await run(
        sessions,
        farm["admin"],
        source_id,
        xlsx(
            headers,
            {
                "Código": "SKU-001",
                "Laboratorio": "Laboratorio Sur Ficticio",
                "Marca": "Marca Sur Ficticia",
            },
            {"Código": "SKU-002", "Descripción": "JERINGA 5 ML - ANDES"},
            {"Código": "SKU-003", "Descripción": "GASAS ESTERILES - LINEA CORDILLERA"},
            {"Código": "SKU-004", "Descripción": "TENSIOMETRO DIGITAL - LAB/MARCA SUR"},
        ),
    )

    assert (outcome.rows_accepted, outcome.rows_rejected) == (4, 0)
    guardadas = await offers(sessions)
    for sku in ("SKU-002", "SKU-003", "SKU-004"):
        oferta = guardadas[sku]
        assert (oferta.laboratory_as_published, oferta.brand_as_published) == (None, None)
        assert identidad(oferta)["status"] == "unconfirmed"


async def test_el_catalogo_propio_aporta_nombres_al_vocabulario(sessions, farm):
    """El vocabulario no se limita al archivo: un laboratorio y una marca ya confirmados en el
    catálogo propio permiten reconocerlos en la descripción."""
    async with sessions() as s, s.begin():
        item = await s.get(TradeItem, farm["item"])
        item.laboratory = "Laboratorio Cordillera Ficticio"
        item.brand = "Marca Cordillera Ficticia"

    source_id = await fuente(sessions, farm, columns=COLUMNS)
    descripcion = "LOSARTAN 50 MG X 30 - LAB. CORDILLERA FICTICIO / MARCA CORDILLERA FICTICIA"

    outcome = await run(
        sessions, farm["admin"], source_id, xlsx(HEADER, {"Descripción": descripcion})
    )

    assert (outcome.rows_accepted, outcome.rows_rejected) == (1, 0)
    oferta = (await offers(sessions))["SKU-001"]
    assert oferta.laboratory_as_published == "Laboratorio Cordillera Ficticio"
    assert oferta.brand_as_published == "Marca Cordillera Ficticia"
    assert oferta.external_name == descripcion
    assert identidad(oferta)["status"] == "extracted"


# --------------------------------------------------------------------------- detección de cabeceras


async def test_las_cabeceras_de_laboratorio_y_marca_se_detectan_solas(sessions, farm):
    headers = [*HEADER, "Lab.", "Marca comercial"]
    source_id = await fuente(sessions, farm)

    async with sessions() as s:
        actor = await load_actor(s, farm["admin"])
        body = await check_import(
            s,
            actor,
            source_id,
            file_name="lista.xlsx",
            content=xlsx(
                headers,
                {
                    # Precio con punto decimal: el flujo automático no adivina separadores.
                    "Precio": "12345.60",
                    "Lab.": "Laboratorios Andes Ficticios",
                    "Marca comercial": "Marca Alfa",
                },
            ),
            auto=True,
        )

    assert body["profile_source"] == "detected"
    columnas = body["profile"]["columns"]
    assert columnas["laboratory"] == {"header": "Lab.", "position": 8}
    assert columnas["brand"] == {"header": "Marca comercial", "position": 9}
    # La revisión lee laboratorio y marca con ese mapeo. La fila se rechaza (si acaso) por lo que
    # el flujo de un botón nunca inventa —moneda, base—, jamás por la identidad del producto.
    row = body["rows"][0]
    assert (row["laboratory"], row["brand"]) == ("Laboratorios Andes Ficticios", "Marca Alfa")
    assert row["identity_status"] == "published"
    assert (row["laboratory_source"], row["brand_source"]) == ("column", "column")
    if row["problem"] is not None:
        assert {"laboratory", "brand"} & set(row["problem"]["fields"]) == set()


# --------------------------------------------------------------------------- API


@pytest.fixture
async def api(sessions, farm, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    app = create_app()

    async def _s():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.farm = farm
        yield c
    get_settings.cache_clear()


def H(api, key=None, **extra):
    headers = {"X-Demo-User-Id": str(api.farm["admin"]), **extra}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


async def test_la_api_expone_lo_publicado_y_la_descripcion_original(api, sessions):
    headers = [*HEADER, "Laboratorio", "Marca"]
    columnas = {
        **COLUMNS,
        "laboratory": {"header": "Laboratorio"},
        "brand": {"header": "Marca"},
    }
    source_id = await fuente(sessions, api.farm)
    saved = await api.put(
        f"/api/v1/sources/{source_id}/import-profile",
        json={
            "file_format": "xlsx",
            "sheet": "Lista",
            "header_row": 1,
            "columns": columnas,
            "units": UNITS,
            "options": OPTIONS,
        },
        headers=H(api, **{"If-Match": '"0"'}),
    )
    assert saved.status_code == 200, saved.text

    imported = await api.post(
        "/api/v1/imports",
        files={
            "file": (
                "lista.xlsx",
                xlsx(
                    headers,
                    {
                        "Laboratorio": "Laboratorios Andes Ficticios",
                        "Marca": "Marca Alfa Ficticia",
                    },
                ),
                "application/octet-stream",
            )
        },
        data={
            "supplier_id": str(api.farm["uno"]),
            "source_id": str(source_id),
        },
        headers=H(api, key="identidad-clave-01"),
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["rows_accepted"] == 1

    listed = await api.get(f"/api/v1/suppliers/{api.farm['uno']}/offers", headers=H(api))
    assert listed.status_code == 200, listed.text
    oferta = listed.json()["offers"][0]
    assert oferta["laboratory_as_published"] == "Laboratorios Andes Ficticios"
    assert oferta["brand_as_published"] == "Marca Alfa Ficticia"
    assert oferta["identity_status"] == "published"
    assert (oferta["laboratory_source"], oferta["brand_source"]) == ("column", "column")
    assert oferta["external_name"] == "LOSARTAN 50 MG X 30"
    assert oferta["review_status"] == "accepted"


# --------------------------------------------------------------------------- motor de preferencias


async def _preferir_laboratorio(
    sessions, farm, *, key="laboratorio chile", label="Laboratorio Chile"
):
    """Deja una preferencia vigente como la produciría O2, sin llamar a OpenAI."""
    async with sessions() as s, s.begin():
        obs = CommercialObservation(
            organization_id=farm["org"],
            text_original="Prefiero Laboratorio Chile",
            author_id=farm["admin"],
            trade_item_id=farm["item"],
            data_origin="simulated",
        )
        s.add(obs)
        await s.flush()
        interp = ObservationInterpretation(
            organization_id=farm["org"],
            observation_id=obs.id,
            trade_item_id=farm["item"],
            status="suggested",
            model="test-model",
            prompt_version="test-v1",
            data_origin="simulated",
        )
        s.add(interp)
        await s.flush()
        s.add(
            CommercialPreference(
                organization_id=farm["org"],
                interpretation_id=interp.id,
                observation_id=obs.id,
                trade_item_id=farm["item"],
                subject_kind="laboratory",
                subject_key=key,
                subject_label=label,
                effect="prefer",
                status="suggested",
                confidence="1.00",
                tolerance_kind="none",
                tolerance_origin="not_applicable",
                data_origin="simulated",
            )
        )
        await s.flush()


async def test_una_preferencia_por_laboratorio_ordena_las_ofertas_importadas(api, sessions):
    """El laboratorio publicado por la planilla llega al motor determinista: la oferta preferida
    sube aunque sea más cara y más antigua, y ninguna oferta desaparece."""
    headers = [*HEADER, "Laboratorio"]
    columnas = {**COLUMNS, "laboratory": {"header": "Laboratorio"}}

    chile = await fuente(
        sessions, api.farm, supplier="uno", nombre="Lista Andes", skus=("AND-001",)
    )
    andino = await fuente(
        sessions, api.farm, supplier="dos", nombre="Lista Pacífico", skus=("PAC-001",)
    )

    async def guardar_perfil(source_id):
        r = await api.put(
            f"/api/v1/sources/{source_id}/import-profile",
            json={
                "file_format": "xlsx",
                "sheet": "Lista",
                "header_row": 1,
                "columns": columnas,
                "units": UNITS,
                "options": OPTIONS,
            },
            headers=H(api, **{"If-Match": '"0"'}),
        )
        assert r.status_code == 200, r.text

    for source_id in (chile, andino):
        await guardar_perfil(source_id)

    async def importar(source_id, sku, laboratorio, precio, fecha, clave):
        return await api.post(
            "/api/v1/imports",
            files={
                "file": (
                    "lista.xlsx",
                    xlsx(
                        headers,
                        {
                            "Código": sku,
                            "Laboratorio": laboratorio,
                            "Precio": precio,
                            "Fecha": fecha,
                        },
                    ),
                    "application/octet-stream",
                )
            },
            data={
                "supplier_id": str(api.farm["dos" if source_id == andino else "uno"]),
                "source_id": str(source_id),
            },
            headers=H(api, key=clave),
        )

    r1 = await importar(
        chile, "AND-001", "Laboratorio Chile", "2.500,00", "15/10/2026", "clave-chile"
    )
    r2 = await importar(
        andino, "PAC-001", "Laboratorio Andino", "2.000,00", "16/10/2026", "clave-andino"
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert (r1.json()["rows_accepted"], r2.json()["rows_accepted"]) == (1, 1)

    async with sessions() as s:
        items = (await s.execute(select(OfferSnapshot))).scalars().all()
    ofertas = {o.external_id: o.id for o in items}
    chile_id, andino_id = ofertas["AND-001"], ofertas["PAC-001"]

    # Sin preferencias, la oferta más reciente (Andino) va primero.
    before = await api.get(f"/api/v1/articles/{api.farm['item']}", headers=H(api))
    assert before.status_code == 200, before.text
    assert [o["offer_id"] for o in before.json()["offers"]] == [str(andino_id), str(chile_id)]

    await _preferir_laboratorio(sessions, api.farm)

    after = await api.get(f"/api/v1/articles/{api.farm['item']}", headers=H(api))
    assert after.status_code == 200, after.text
    orden = [o["offer_id"] for o in after.json()["offers"]]
    assert orden == [str(chile_id), str(andino_id)]
    # Ordenar no cambia precios ni oculta ofertas.
    precios = {o["offer_id"]: o["price"]["money"]["amount"] for o in after.json()["offers"]}
    assert precios == {str(chile_id): "2500.00", str(andino_id): "2000.00"}


async def test_una_preferencia_ordena_con_el_laboratorio_extraido_de_la_descripcion(api, sessions):
    """El laboratorio reconocido en la descripción se usa igual que uno publicado: el motor lo
    recibe y ordena las ofertas, sin ocultar ninguna ni cambiar su precio."""
    headers = [*HEADER, "Laboratorio"]
    con_columna = {**COLUMNS, "laboratory": {"header": "Laboratorio"}}

    # Vocabulario confirmado: el catálogo propio ya declara ese laboratorio.
    async with sessions() as s, s.begin():
        item = await s.get(TradeItem, api.farm["item"])
        item.laboratory = "Laboratorio Cordillera Ficticio"

    descripcion = await fuente(
        sessions, api.farm, supplier="uno", nombre="Lista Andes", skus=("AND-001",)
    )
    columna = await fuente(
        sessions, api.farm, supplier="dos", nombre="Lista Pacífico", skus=("PAC-001",)
    )

    async def guardar_perfil(source_id, columns):
        r = await api.put(
            f"/api/v1/sources/{source_id}/import-profile",
            json={
                "file_format": "xlsx",
                "sheet": "Lista",
                "header_row": 1,
                "columns": columns,
                "units": UNITS,
                "options": OPTIONS,
            },
            headers=H(api, **{"If-Match": '"0"'}),
        )
        assert r.status_code == 200, r.text

    # La fuente de «uno» no mapea laboratorio: el dato solo puede venir de la descripción.
    await guardar_perfil(descripcion, COLUMNS)
    await guardar_perfil(columna, con_columna)

    async def importar(source_id, supplier, values, clave):
        return await api.post(
            "/api/v1/imports",
            files={
                "file": (
                    "lista.xlsx",
                    xlsx(headers, values),
                    "application/octet-stream",
                )
            },
            data={"supplier_id": str(api.farm[supplier]), "source_id": str(source_id)},
            headers=H(api, key=clave),
        )

    r1 = await importar(
        descripcion,
        "uno",
        {
            "Código": "AND-001",
            "Descripción": "LOSARTAN 50 MG X 30 - LAB. CORDILLERA FICTICIO",
            "Precio": "2.500,00",
            "Fecha": "15/10/2026",
        },
        "clave-descripcion",
    )
    r2 = await importar(
        columna,
        "dos",
        {
            "Código": "PAC-001",
            "Laboratorio": "Laboratorio Andino",
            "Precio": "2.000,00",
            "Fecha": "16/10/2026",
        },
        "clave-columna",
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    async with sessions() as s:
        guardadas = (await s.execute(select(OfferSnapshot))).scalars().all()
    ofertas = {o.external_id: o for o in guardadas}
    extraida, publicada = ofertas["AND-001"], ofertas["PAC-001"]
    assert identidad(extraida)["laboratory"] == {
        "source": "description",
        "matched_text": "LAB. CORDILLERA FICTICIO",
    }

    before = await api.get(f"/api/v1/articles/{api.farm['item']}", headers=H(api))
    assert before.status_code == 200, before.text
    assert [o["offer_id"] for o in before.json()["offers"]] == [
        str(publicada.id),
        str(extraida.id),
    ]

    await _preferir_laboratorio(
        sessions,
        api.farm,
        key="laboratorio cordillera ficticio",
        label="Laboratorio Cordillera Ficticio",
    )

    after = await api.get(f"/api/v1/articles/{api.farm['item']}", headers=H(api))
    assert after.status_code == 200, after.text
    ofertas_e3 = {o["offer_id"]: o for o in after.json()["offers"]}
    # El orden solo cambia si el motor recibió el laboratorio extraído: es la misma oferta, con el
    # mismo precio, y la preferencia no aplicaría sin ese dato.
    assert list(ofertas_e3) == [str(extraida.id), str(publicada.id)]
    assert ofertas_e3[str(extraida.id)]["price"]["money"]["amount"] == "2500.00"
    assert ofertas_e3[str(publicada.id)]["price"]["money"]["amount"] == "2000.00"


# --------------------------------------------------------------------------- Excel de demostración

DEMO = (
    __import__("pathlib").Path(__file__).parents[2]
    / "docs"
    / "ejemplos"
    / "lista_distribuidora_andes.xlsx"
)


async def test_el_excel_de_demostracion_se_importa_sin_reescribir_la_planilla(sessions, farm):
    """Los 30 productos ficticios de Distribuidora Andes se leen como están en el archivo.

    Filas 1–20: la identidad viene de las columnas propias (ocho laboratorio, seis marca, seis
    ambas). Filas 21–26: solo la descripción la menciona, con «LAB.»/«LABORATORIOS»/«MARCA» y un
    nombre que otra fila del mismo archivo publica; se reconoce y se guarda con procedencia
    `description`. Filas 27–30: el nombre es ambiguo o no trae marca explícita, así que quedan sin
    identificar. En todos los casos la descripción original queda intacta y solo AND-006 y AND-020
    (condiciones comerciales) quedan en revisión.
    """
    columnas = {
        "sku": {"header": "Código"},
        "name": {"header": "Descripción"},
        "laboratory": {"header": "Laboratorio"},
        "brand": {"header": "Marca"},
        "price": {"header": "Precio"},
        "currency": {"header": "Moneda"},
        "basis": {"header": "Base"},
        "sale_unit": {"header": "Unidad"},
        "stock": {"header": "Stock"},
        "observed_at": {"header": "Fecha"},
        "conditions": {"header": "Condiciones"},
    }
    source_id = await fuente(
        sessions,
        farm,
        skus=[f"AND-{i:03d}" for i in range(1, 31)],
        columns=columnas,
        sheet="Lista Andes",
    )

    outcome = await run(sessions, farm["admin"], source_id, DEMO.read_bytes())

    assert (outcome.rows_total, outcome.rows_accepted, outcome.rows_rejected) == (30, 30, 0)
    guardadas = await offers(sessions)
    assert len(guardadas) == 30
    assert sorted(
        o.external_id for o in guardadas.values() if o.review_status == "needs_review"
    ) == ["AND-006", "AND-020"]

    # La revisión comercial no se mezcla con la identidad: ninguna fila se vuelve `needs_review`
    # por haber extraído (o no) laboratorio y marca.
    con_laboratorio = [o for o in guardadas.values() if o.laboratory_as_published]
    con_marca = [o for o in guardadas.values() if o.brand_as_published]
    assert (len(con_laboratorio), len(con_marca)) == (18, 15)
    assert sum(1 for o in guardadas.values() if identidad(o)["status"] == "published") == 20
    assert sum(1 for o in guardadas.values() if identidad(o)["status"] == "extracted") == 6
    assert sum(1 for o in guardadas.values() if identidad(o)["status"] == "unconfirmed") == 4

    # Filas 21–26: identidad reconocida en la descripción, con el nombre que el archivo ya usa.
    esperadas = {
        "AND-021": ("Lab. Cordillera Ficticio", None),
        "AND-022": ("Laboratorios Andes Ficticios", None),
        "AND-023": ("Lab. Omega Ficticio", None),
        "AND-024": (None, "Marca Alfa Ficticia"),
        "AND-025": (None, "Marca Delta Ficticia"),
        "AND-026": ("Laboratorio Sur Ficticio", "Marca Sur Ficticia"),
    }
    for sku, (laboratorio, marca) in esperadas.items():
        oferta = guardadas[sku]
        evidencia = identidad(oferta)
        assert (oferta.laboratory_as_published, oferta.brand_as_published) == (laboratorio, marca)
        assert evidencia["status"] == "extracted"
        if laboratorio:
            assert evidencia["laboratory"]["source"] == "description"
            assert evidencia["laboratory"]["matched_text"] in oferta.external_name.upper()
        else:
            assert evidencia["laboratory"] is None
        if marca:
            assert evidencia["brand"]["source"] == "description"
            assert evidencia["brand"]["matched_text"] in oferta.external_name.upper()
        else:
            assert evidencia["brand"] is None

    # Filas 27–30: sin marca explícita o con un nombre ambiguo no se inventa identidad.
    for sku in ("AND-027", "AND-028", "AND-029", "AND-030"):
        oferta = guardadas[sku]
        assert (oferta.laboratory_as_published, oferta.brand_as_published) == (None, None)
        assert identidad(oferta)["status"] == "unconfirmed"

    # La descripción original del proveedor se conserva tal cual, con o sin reconocimiento.
    assert guardadas["AND-021"].external_name.endswith("- LAB. CORDILLERA FICTICIO")
    assert guardadas["AND-026"].external_name.endswith("- LAB. SUR FICTICIO / MARCA SUR FICTICIA")
    assert guardadas["AND-027"].external_name.endswith("- ANDES")
    assert guardadas["AND-028"].external_name.endswith("- LINEA CORDILLERA")
    assert guardadas["AND-030"].external_name.endswith("- LAB/MARCA SUR")
    assert all(oferta.external_name for oferta in guardadas.values())
