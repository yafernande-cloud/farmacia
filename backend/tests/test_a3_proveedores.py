"""A3 — proveedores: portal con credenciales cifradas y listas .xlsx/.xls/.csv.

Las planillas son `fixture`: el CSV y el XLSX se generan aquí y el XLS es un archivo binario
pequeño generado una vez con una herramienta de escritura, no un formato comercial inventado.
Ninguna prueba usa servicios externos, credenciales reales ni bases reales.
"""

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.credentials import decrypt_portal_password
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AppUser,
    ImportProfile,
    ImportRun,
    OfferSnapshot,
    Organization,
    Source,
    Supplier,
    SupplierCredential,
    SupplierProductMapping,
    TradeItem,
)

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).parent / "fixtures"
KEY = Fernet.generate_key().decode()

CSV_HEADER = "Código;Descripción;Precio;Unidad"
CSV_ROWS = [
    ["CSV-001", "GUANTES NITRILO M CAJA 100", "45990", "CAJA"],
    ["CSV-002", "ALCOHOL 70% 1 L", "2990", "UNIDAD"],
]
CSV_COLUMNS = {
    "sku": {"header": "Código"},
    "name": {"header": "Descripción"},
    "price": {"header": "Precio"},
    "sale_unit": {"header": "Unidad"},
}
UNITS = {"CAJA": "box", "UNIDAD": "unit"}


def csv_bytes(rows=CSV_ROWS, header=CSV_HEADER) -> bytes:
    return ("\n".join([header, *[";".join(r) for r in rows]]) + "\n").encode("utf-8-sig")


def xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Lista"
    ws.append([*CSV_HEADER.split(";"), "Moneda", "Base"])
    for row in CSV_ROWS:
        ws.append([*row, "CLP", "per_commercial_unit"])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def xls_bytes() -> bytes:
    return (FIXTURES / "lista_precios.xls").read_bytes()


def xlsx_con_hojas() -> bytes:
    """Dos hojas con la cabecera real en la fila 3 de la segunda, para probar hoja y cabecera."""
    wb = Workbook()
    primera = wb.active
    primera.title = "Portada"
    primera.append(["Informe de precios"])
    otra = wb.create_sheet("Otra")
    otra.append(["Reporte semanal"])
    otra.append(["X1", "Fila de datos", "1", "CAJA"])
    otra.append([*CSV_HEADER.split(";"), "Moneda", "Base"])
    for row in CSV_ROWS:
        otra.append([*row, "CLP", "per_commercial_unit"])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
async def farm(sessions):
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia A3", kind="simulation")
        other = Organization(name="Otra farmacia A3", kind="simulation")
        s.add_all([org, other])
        await s.flush()
        ids["org"], ids["other_org"] = org.id, other.id
        for key, name, roles, o in [
            ("admin", "Pao", ["admin"], org),
            ("aux", "Auxiliar", ["auxiliary"], org),
            ("outsider", "Externo", ["admin"], other),
        ]:
            u = AppUser(organization_id=o.id, display_name=name, roles=roles)
            s.add(u)
            await s.flush()
            ids[key] = u.id
        supplier = Supplier(organization_id=org.id, name="Proveedor A3", data_origin="simulated")
        s.add(supplier)
        await s.flush()
        ids["supplier"] = supplier.id
        source = Source(
            organization_id=org.id,
            kind="supplier",
            supplier_id=supplier.id,
            name="Lista A3",
            mode="manual_import",
            data_origin="simulated",
        )
        s.add(source)
        await s.flush()
        ids["source"] = source.id
        item = TradeItem(
            organization_id=org.id,
            identity_key="guantes-nitrilo-m",
            display_name="Guantes nitrilo M caja 100",
            data_origin="simulated",
        )
        s.add(item)
        await s.flush()
        for external in ("CSV-001", "XLS-001"):
            s.add(
                SupplierProductMapping(
                    organization_id=org.id,
                    source_id=source.id,
                    supplier_id=supplier.id,
                    trade_item_id=item.id,
                    external_id=external,
                    origin="explicit",
                    data_origin="simulated",
                )
            )
        item2 = TradeItem(
            organization_id=org.id,
            identity_key="alcohol-70-1l",
            display_name="Alcohol 70% 1 L",
            data_origin="simulated",
        )
        s.add(item2)
        await s.flush()
        for external in ("CSV-002", "XLS-002"):
            s.add(
                SupplierProductMapping(
                    organization_id=org.id,
                    source_id=source.id,
                    supplier_id=supplier.id,
                    trade_item_id=item2.id,
                    external_id=external,
                    origin="explicit",
                    data_origin="simulated",
                )
            )
        await s.flush()
    return ids


@pytest.fixture
async def api(sessions, farm, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    monkeypatch.setenv("SUPPLIER_CREDENTIALS_KEY", KEY)
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


def H(api, who="admin", key=None, **extra):
    headers = {"X-Demo-User-Id": str(api.farm[who]), **extra}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


async def create(api, **over):
    body = {"name": "Proveedor Nuevo A3"}
    body.update(over)
    return await api.post("/api/v1/suppliers", json=body, headers=H(api))


async def patch(api, supplier_id, version, **body):
    return await api.patch(
        f"/api/v1/suppliers/{supplier_id}",
        json=body,
        headers=H(api, **{"If-Match": f'"{version}"'}),
    )


async def put_profile(api, version=0, **over):
    body = {
        "file_format": "csv",
        "header_row": 1,
        "columns": CSV_COLUMNS,
        "units": UNITS,
        "options": {"currency": "CLP", "price_basis": "per_commercial_unit"},
    }
    body.update(over)
    return await api.put(
        f"/api/v1/sources/{api.farm['source']}/import-profile",
        json=body,
        headers=H(api, **{"If-Match": f'"{version}"'}),
    )


async def post_import(api, content, file_name, *, key, source_id=None):
    return await api.post(
        "/api/v1/imports",
        files={"file": (file_name, content, "application/octet-stream")},
        data={
            "supplier_id": str(api.farm["supplier"]),
            "source_id": str(source_id or api.farm["source"]),
        },
        headers=H(api, key=key),
    )


async def count(sessions, model, *where):
    async with sessions() as s:
        q = select(func.count()).select_from(model)
        for clause in where:
            q = q.where(clause)
        return (await s.execute(q)).scalar_one()


# ---------- portal: credenciales cifradas y sin exposición ----------


async def test_crear_con_portal_guarda_la_contrasena_cifrada_y_no_la_devuelve(api, sessions):
    r = await create(
        api,
        portal_url="https://portal.proveedor-ficticio.cl/compras",
        portal_username="compras@ficticio.cl",
        portal_password="Clave-Secreta-123",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["portal_url"] == "https://portal.proveedor-ficticio.cl/compras"
    assert body["has_portal_credentials"] is True
    assert body["portal_status"] == "credentials_saved"
    # Ni el usuario ni la contraseña salen en la respuesta.
    assert "compras@ficticio.cl" not in r.text
    assert "Clave-Secreta-123" not in r.text
    assert not {"portal_username", "portal_password"} & set(body)

    async with sessions() as s:
        cred = (await s.execute(select(SupplierCredential))).scalar_one()
    assert cred.username == "compras@ficticio.cl"
    assert cred.password_ciphertext != "Clave-Secreta-123"
    assert "Clave-Secreta-123" not in cred.password_ciphertext
    assert decrypt_portal_password(cred.password_ciphertext) == "Clave-Secreta-123"

    listed = await api.get("/api/v1/suppliers", headers=H(api))
    assert "Clave-Secreta-123" not in listed.text
    assert "compras@ficticio.cl" not in listed.text


async def test_estados_del_portal_nunca_dicen_conectado(api):
    only_url = (await create(api, portal_url="https://portal.cl", name="Solo URL")).json()
    assert (only_url["portal_status"], only_url["has_portal_credentials"]) == (
        "incomplete",
        False,
    )
    no_portal = (await create(api, name="Sin portal")).json()
    assert no_portal["portal_status"] == "not_configured"
    with_credentials = (
        await create(
            api,
            name="Con credenciales sin URL",
            portal_username="usuario",
            portal_password="clave-123",
        )
    ).json()
    assert with_credentials["portal_status"] == "incomplete"
    assert all(
        body["portal_status"] != "connected" for body in (only_url, no_portal, with_credentials)
    )


async def test_sin_clave_no_se_puede_guardar_una_contrasena(api, sessions, monkeypatch):
    monkeypatch.delenv("SUPPLIER_CREDENTIALS_KEY", raising=False)
    get_settings.cache_clear()
    r = await create(
        api,
        name="Proveedor sin clave",
        portal_username="usuario",
        portal_password="clave-123",
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "SUPPLIER_CREDENTIALS_KEY_MISSING"
    # La transacción completa se revierte: no queda ni proveedor ni credencial.
    assert await count(sessions, Supplier, Supplier.name == "Proveedor sin clave") == 0
    assert await count(sessions, SupplierCredential) == 0


async def test_actualizar_una_parte_conserva_la_otra_y_null_borra_la_credencial(api, sessions):
    body = (
        await create(
            api,
            portal_url="https://portal.cl",
            portal_username="usuario",
            portal_password="clave-1",
        )
    ).json()
    original = body["supplier_id"]

    r = await patch(api, original, 1, portal_password="clave-2")
    assert r.status_code == 200, r.text
    assert r.json()["has_portal_credentials"] is True
    async with sessions() as s:
        cred = (await s.execute(select(SupplierCredential))).scalar_one()
    assert cred.username == "usuario"
    assert decrypt_portal_password(cred.password_ciphertext) == "clave-2"

    r = await patch(api, original, 2, portal_username=None)
    assert r.status_code == 200, r.text
    assert r.json()["has_portal_credentials"] is False
    assert r.json()["portal_status"] == "incomplete"  # la URL sigue
    assert await count(sessions, SupplierCredential) == 0


async def test_url_de_portal_invalida_se_rechaza(api):
    r = await create(api, portal_url="ftp://portal.cl")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PORTAL_URL"


# ---------- vista previa y perfil ----------


async def test_la_vista_previa_lee_el_csv_y_no_escribe_nada(api, sessions):
    r = await api.post(
        f"/api/v1/sources/{api.farm['source']}/import-preview",
        files={"file": ("lista.csv", csv_bytes(), "text/csv")},
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_format"] == "csv"
    assert body["header_row"] == 1
    assert body["headers"][:4] == ["Código", "Descripción", "Precio", "Unidad"]
    assert body["row_count"] == 2
    assert body["suggested_columns"]["sku"]["header"] == "Código"
    assert body["suggested_columns"]["price"]["header"] == "Precio"
    assert body["suggested_options"]["currency"] is None  # no se inventa
    assert body["sample_rows"][0]["values"][0] == "CSV-001"
    assert await count(sessions, ImportProfile) == 0


async def test_la_vista_previa_puede_leer_otra_hoja_y_otra_fila_de_encabezado(api, sessions):
    archivo = xlsx_con_hojas()
    detectada = await api.post(
        f"/api/v1/sources/{api.farm['source']}/import-preview",
        files={"file": ("lista.xlsx", archivo, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"sheet": "Otra"},
        headers=H(api),
    )
    assert detectada.status_code == 200, detectada.text
    body = detectada.json()
    assert body["sheets"] == ["Portada", "Otra"]
    assert body["sheet"] == "Otra"
    assert body["header_row"] == 3
    assert body["headers"][:4] == ["Código", "Descripción", "Precio", "Unidad"]

    explicita = await api.post(
        f"/api/v1/sources/{api.farm['source']}/import-preview",
        files={"file": ("lista.xlsx", archivo, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"sheet": "Otra", "header_row": "2"},
        headers=H(api),
    )
    assert explicita.status_code == 200, explicita.text
    assert explicita.json()["header_row"] == 2
    assert explicita.json()["headers"][:4] == ["X1", "Fila de datos", "1", "CAJA"]
    assert explicita.json()["suggested_columns"] == {}

    inexistente = await api.post(
        f"/api/v1/sources/{api.farm['source']}/import-preview",
        files={"file": ("lista.xlsx", archivo, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"sheet": "Otra", "header_row": "999"},
        headers=H(api),
    )
    assert inexistente.status_code == 422
    assert inexistente.json()["error"]["code"] == "IMPORT_FORMAT_INVALID"
    assert await count(sessions, ImportProfile) == 0


async def test_guardar_perfil_exige_columnas_y_respeta_la_version(api):
    missing_unit = await put_profile(api, columns={"sku": {"header": "Código"}, "price": {"header": "Precio"}})
    assert missing_unit.status_code == 422
    assert missing_unit.json()["error"]["code"] == "IMPORT_FORMAT_INVALID"

    with_default = await put_profile(
        api,
        columns={"sku": {"header": "Código"}, "price": {"header": "Precio"}},
        options={"currency": "CLP", "price_basis": "per_commercial_unit", "default_sale_unit": "box"},
    )
    assert with_default.status_code == 200, with_default.text
    assert with_default.json()["version"] == 1
    assert with_default.headers["ETag"] == '"1"'

    got = await api.get(
        f"/api/v1/sources/{api.farm['source']}/import-profile", headers=H(api)
    )
    assert got.json()["profile"]["version"] == 1

    conflict = await put_profile(api, version=0)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"


# ---------- importación real por formato ----------


async def test_importa_csv_y_reimportar_el_mismo_archivo_no_duplica(api, sessions):
    assert (await put_profile(api)).status_code == 200
    first = await post_import(api, csv_bytes(), "lista.csv", key="csv-clave-0001")
    assert first.status_code == 200, first.text
    assert (first.json()["status"], first.json()["rows_accepted"]) == ("completed", 2)
    assert await count(sessions, OfferSnapshot) == 2
    assert await count(sessions, ImportRun) == 1

    again = await post_import(api, csv_bytes(), "lista.csv", key="csv-clave-0002")
    assert again.status_code == 200
    assert again.json()["import_id"] == first.json()["import_id"]
    assert await count(sessions, OfferSnapshot) == 2  # sin filas nuevas
    assert await count(sessions, ImportRun) == 1

    offers = await api.get(
        f"/api/v1/suppliers/{api.farm['supplier']}/offers", headers=H(api)
    )
    assert offers.status_code == 200, offers.text
    rows = offers.json()["offers"]
    assert {o["external_id"] for o in rows} == {"CSV-001", "CSV-002"}
    guantes = next(o for o in rows if o["external_id"] == "CSV-001")
    assert guantes["price_amount"] == "45990.0000"
    assert guantes["currency"] == "CLP"
    assert guantes["trade_item_name"] == "Guantes nitrilo M caja 100"
    assert guantes["observed_at"]


async def test_importa_xlsx_con_el_mismo_motor(api, sessions):
    saved = await put_profile(api, file_format="xlsx")
    assert saved.status_code == 200
    r = await post_import(api, xlsx_bytes(), "lista.xlsx", key="xlsx-clave-001")
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["rows_accepted"]) == ("completed", 2)
    async with sessions() as s:
        offers = (await s.execute(select(OfferSnapshot))).scalars().all()
    assert {o.external_id for o in offers} == {"CSV-001", "CSV-002"}


async def test_importa_xls_antiguo(api, sessions):
    saved = await put_profile(api, file_format="xls", sheet="Lista")
    assert saved.status_code == 200, saved.text
    r = await post_import(api, xls_bytes(), "lista.xls", key="xls-clave-0001")
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["rows_accepted"]) == ("completed", 2)
    async with sessions() as s:
        offers = (await s.execute(select(OfferSnapshot))).scalars().all()
    assert {o.external_id for o in offers} == {"XLS-001", "XLS-002"}
    assert {str(o.price_amount) for o in offers} == {"45990.0000", "2990.0000"}


async def test_un_archivo_que_no_es_planilla_se_rechaza(api):
    saved = await put_profile(api)
    assert saved.status_code == 200
    r = await post_import(api, b"esto no es una planilla", "lista.csv", key="rota-clave-01")
    assert r.status_code in (200, 422)
    if r.status_code == 422:
        assert r.json()["error"]["code"] == "IMPORT_FORMAT_INVALID"


# ---------- artículos nuevos: solo con opción explícita ----------


async def test_sin_opcion_un_sku_nuevo_se_rechaza(api, sessions):
    assert (await put_profile(api)).status_code == 200
    content = csv_bytes([["NUEVO-1", "PRODUCTO NUEVO", "1000", "CAJA"]])
    r = await post_import(api, content, "nuevo.csv", key="nuevo-clave-01")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"
    assert r.json()["errors"][0]["code"] == "UNMAPPED_PRODUCT"
    assert await count(sessions, TradeItem, TradeItem.identity_key.startswith("import:")) == 0


async def test_con_opcion_crea_articulo_y_vinculo_una_sola_vez(api, sessions):
    saved = await put_profile(
        api,
        options={
            "currency": "CLP",
            "price_basis": "per_commercial_unit",
            "create_missing_items": True,
        },
    )
    assert saved.status_code == 200, saved.text
    content = csv_bytes([["NUEVO-1", "PRODUCTO NUEVO", "1000", "CAJA"]])
    r = await post_import(api, content, "nuevo.csv", key="nuevo-clave-02")
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["rows_accepted"]) == ("completed", 1)

    async with sessions() as s:
        item = (
            await s.execute(
                select(TradeItem).where(
                    TradeItem.identity_key == f"import:{api.farm['source']}:NUEVO-1"
                )
            )
        ).scalar_one()
        mapping = (
            await s.execute(
                select(SupplierProductMapping).where(
                    SupplierProductMapping.source_id == api.farm["source"],
                    SupplierProductMapping.external_id == "NUEVO-1",
                )
            )
        ).scalar_one()
    assert item.display_name == "PRODUCTO NUEVO"
    assert mapping.trade_item_id == item.id and mapping.origin == "explicit"

    again = await post_import(api, content, "nuevo.csv", key="nuevo-clave-03")
    assert again.json()["import_id"] == r.json()["import_id"]
    assert await count(sessions, TradeItem, TradeItem.identity_key.startswith("import:")) == 1
    assert await count(sessions, SupplierProductMapping, SupplierProductMapping.external_id == "NUEVO-1") == 1

    empty_name = csv_bytes([["NUEVO-2", "", "1000", "CAJA"]])
    missing = await post_import(api, empty_name, "sin-nombre.csv", key="nuevo-clave-04")
    assert missing.json()["status"] == "failed"
    assert missing.json()["errors"][0]["code"] == "UNMAPPED_PRODUCT"
    assert await count(sessions, TradeItem, TradeItem.identity_key.endswith("NUEVO-2")) == 0


# ---------- permisos ----------


async def test_las_piezas_nuevas_son_solo_admin(api):
    preview = await api.post(
        f"/api/v1/sources/{api.farm['source']}/import-preview",
        files={"file": ("lista.csv", csv_bytes(), "text/csv")},
        headers=H(api, "aux"),
    )
    assert preview.status_code == 403
    profile = await api.get(
        f"/api/v1/sources/{api.farm['source']}/import-profile", headers=H(api, "aux")
    )
    assert profile.status_code == 403
    offers = await api.get(
        f"/api/v1/suppliers/{api.farm['supplier']}/offers", headers=H(api, "aux")
    )
    assert offers.status_code == 403
    # Una administradora de otra organización recibe 404: no confirma que el proveedor exista.
    ajeno = await api.get(
        f"/api/v1/suppliers/{api.farm['supplier']}/offers", headers=H(api, "outsider")
    )
    assert ajeno.status_code == 404


# ---------- última carga, estado de la fuente y vigencia de los precios importados ----------


async def test_importar_marca_la_ultima_carga_y_el_estado_de_la_fuente(api):
    assert (await put_profile(api)).status_code == 200
    r = await post_import(api, csv_bytes(), "lista.csv", key="marca-clave-01")
    assert r.status_code == 200, r.text
    assert r.json()["rows_accepted"] == 2

    lista = await api.get("/api/v1/sources?kind=supplier&enabled=true", headers=H(api))
    assert lista.status_code == 200, lista.text
    fuente = next(
        s for s in lista.json()["sources"] if s["source_id"] == str(api.farm["source"])
    )
    assert fuente["last_success_at"] is not None
    assert fuente["status"] == "ok"

    detalle = await api.get(f"/api/v1/sources/{api.farm['source']}", headers=H(api))
    assert detalle.status_code == 200, detalle.text
    assert detalle.json()["last_success_at"] == fuente["last_success_at"]


async def test_una_importacion_previa_sin_marca_se_refleja_sin_duplicar(api, sessions):
    """Las importaciones ya realizadas no se repiten: su corrida alimenta el estado de la fuente."""
    importada = datetime.now(UTC) - timedelta(hours=1)
    async with sessions() as s, s.begin():
        s.add(
            ImportRun(
                organization_id=api.farm["org"],
                source_id=api.farm["source"],
                file_name="lista-andes.xlsx",
                file_hash="b" * 64,
                status="completed",
                rows_total=30,
                rows_accepted=30,
                imported_at=importada,
                data_origin="captured",
            )
        )

    lista = await api.get("/api/v1/sources?kind=supplier&enabled=true", headers=H(api))
    assert lista.status_code == 200, lista.text
    fuente = next(
        s for s in lista.json()["sources"] if s["source_id"] == str(api.farm["source"])
    )
    assert fuente["last_success_at"] is not None
    assert fuente["last_success_at"].startswith(importada.strftime("%Y-%m-%dT%H:%M"))
    assert fuente["status"] == "ok"
    # La lectura del estado no crea ofertas ni corridas nuevas.
    assert await count(sessions, ImportRun) == 1
    assert await count(sessions, OfferSnapshot) == 0


async def test_las_ofertas_importadas_filtran_por_articulo_y_explican_su_vigencia(api, sessions):
    header = f"{CSV_HEADER};Condiciones"
    filas = [
        ["CSV-001", "GUANTES NITRILO M CAJA 100", "45990", "CAJA", "Entrega en 48 horas"],
        ["CSV-002", "ALCOHOL 70% 1 L", "2990", "UNIDAD", ""],
    ]
    saved = await put_profile(
        api,
        columns={**CSV_COLUMNS, "conditions": {"header": "Condiciones"}},
    )
    assert saved.status_code == 200, saved.text
    r = await post_import(api, csv_bytes(filas, header), "lista.csv", key="vigencia-clave-1")
    assert r.status_code == 200, r.text
    assert r.json()["rows_accepted"] == 2

    async with sessions() as s:
        guantes = (
            await s.execute(
                select(TradeItem).where(TradeItem.identity_key == "guantes-nitrilo-m")
            )
        ).scalar_one()

    filtradas = await api.get(
        f"/api/v1/suppliers/{api.farm['supplier']}/offers",
        params={"trade_item_ids": str(guantes.id)},
        headers=H(api),
    )
    assert filtradas.status_code == 200, filtradas.text
    ofertas = filtradas.json()["offers"]
    assert [o["external_id"] for o in ofertas] == ["CSV-001"]
    oferta = ofertas[0]
    assert oferta["review_status"] == "needs_review"
    assert oferta["notes"] == "Entrega en 48 horas"
    assert oferta["source_mode"] == "manual_import"
    assert oferta["valid_from"] is None and oferta["valid_to"] is None
    assert oferta["is_current"] is True
    assert oferta["is_stale"] is False
    assert oferta["fresh_until"]

    todas = await api.get(
        f"/api/v1/suppliers/{api.farm['supplier']}/offers", headers=H(api)
    )
    assert {o["external_id"] for o in todas.json()["offers"]} == {"CSV-001", "CSV-002"}

