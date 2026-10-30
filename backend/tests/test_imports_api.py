"""Endpoint E8 `POST /api/v1/imports` — sobre congelado del contrato C0 (C2.5).

Comprueba permisos, organización, límites, idempotencia por clave y que la respuesta sea exactamente
la del contrato. No vuelve a probar el motor (`test_imports_manual.py`): aquí solo se verifica el
transporte y las reglas que la API agrega. Las planillas son `fixture`: se generan aquí.
"""

import uuid
from io import BytesIO

import pytest
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.api.v1 import imports as imports_api
from app.core.config import get_settings
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AppUser,
    ChainProduct,
    ImportProfile,
    ImportRequest,
    ImportRun,
    OfferSnapshot,
    Organization,
    ReferencePrice,
    Source,
    Supplier,
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


def xlsx(rows, header=HEADER) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Lista"
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def fila(sku="SKU-001", precio="12345.60", **over):
    values = {"desc": "LOSARTAN 50 X 30", "moneda": "CLP", "base": "per_commercial_unit"}
    values.update(over)
    return [sku, values["desc"], precio, values["moneda"], values["base"], values.get("u", "CAJA")]


@pytest.fixture
async def farm(sessions):
    """Organización de simulación con proveedor y perfil, más una organización real y una ajena."""
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia E8", kind="simulation")
        real = Organization(name="Farmacia real E8", kind="real_test")
        otra = Organization(name="Otra farmacia E8", kind="simulation")
        s.add_all([org, real, otra])
        await s.flush()
        ids["org"], ids["real"], ids["otra"] = org.id, real.id, otra.id
        for key, name, roles, o in [
            ("admin", "Yarella", ["admin"], org),
            ("aux", "Danny", ["auxiliary"], org),
            ("real_admin", "Yarella real", ["admin"], real),
            ("otra_admin", "Ajena", ["admin"], otra),
        ]:
            u = AppUser(organization_id=o.id, display_name=name, roles=roles)
            s.add(u)
            await s.flush()
            ids[key] = u.id
        sup = Supplier(organization_id=org.id, name="Proveedor Uno", data_origin="simulated")
        otro_sup = Supplier(organization_id=org.id, name="Proveedor Dos", data_origin="simulated")
        sup_ajeno = Supplier(organization_id=otra.id, name="Proveedor Ajeno", data_origin="simulated")
        s.add_all([sup, otro_sup, sup_ajeno])
        await s.flush()
        ids["supplier"], ids["otro_supplier"] = sup.id, otro_sup.id
        source = Source(
            organization_id=org.id, kind="supplier", supplier_id=sup.id, name="Lista Uno",
            mode="manual_import", data_origin="simulated",
        )  # fmt: skip
        source_ajena = Source(
            organization_id=otra.id, kind="supplier", supplier_id=sup_ajeno.id, name="Lista Ajena",
            mode="manual_import", data_origin="simulated",
        )  # fmt: skip
        cadena = Source(
            organization_id=org.id, kind="chain_reference", name="Catálogo cadena",
            mode="manual_import", data_origin="simulated",
        )  # fmt: skip
        s.add_all([source, source_ajena, cadena])
        source_2 = Source(
            organization_id=org.id, kind="supplier", supplier_id=sup.id, name="Lista Dos",
            mode="manual_import", data_origin="simulated",
        )  # fmt: skip
        s.add(source_2)
        await s.flush()
        ids["source"], ids["source_ajena"], ids["chain"] = (
            source.id,
            source_ajena.id,
            cadena.id,
        )
        ids["source_2"] = source_2.id
        item = TradeItem(
            organization_id=org.id, identity_key="losartan-50-30", display_name="Losartán",
            data_origin="simulated",
        )  # fmt: skip
        s.add(item)
        await s.flush()
        for extra in range(1, 6):
            s.add(
                SupplierProductMapping(
                    organization_id=org.id,
                    source_id=source.id,
                    supplier_id=sup.id,
                    trade_item_id=item.id,
                    external_id=f"SKU-{extra:03d}",
                    origin="explicit",
                    data_origin="simulated",
                )
            )
        s.add(
            ImportProfile(
                organization_id=org.id,
                source_id=source.id,
                sheet="Lista",
                header_row=1,
                columns=COLUMNS,
                units=UNITS,
                options={"tax_included": False},
                data_origin="simulated",
            )
        )
        await s.flush()
    return ids


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


def H(api, who="admin", key="clave-de-prueba-01"):
    return {"X-Demo-User-Id": str(api.farm[who]), "Idempotency-Key": key}


async def post(api, content=None, *, who="admin", key="clave-de-prueba-01", **over):
    data = {
        "supplier_id": str(api.farm["supplier"]),
        "source_id": str(api.farm["source"]),
    }
    data.update(over)
    return await api.post(
        "/api/v1/imports",
        files={
            "file": (
                "lista.xlsx",
                content if content is not None else xlsx([fila()]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data=data,
        headers=H(api, who, key),
    )


async def count(sessions, model) -> int:
    async with sessions() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


# ---------- camino correcto y sobre del contrato ----------


async def test_importa_y_respeta_el_sobre_congelado(api, sessions):
    r = await post(api, xlsx([fila(), fila(sku="SKU-002", precio="999.00")]))
    assert r.status_code == 200, r.text
    body = r.json()
    # Exactamente los campos del contrato C0: ni uno más.
    assert set(body) == {
        "import_id",
        "status",
        "rows_total",
        "rows_accepted",
        "rows_rejected",
        "errors",
        "observed_at",
    }
    assert (body["status"], body["rows_total"], body["rows_accepted"]) == ("completed", 2, 2)
    assert body["rows_rejected"] == 0 and body["errors"] == []
    assert uuid.UUID(body["import_id"])

    async with sessions() as s:
        run = (await s.execute(select(ImportRun))).scalar_one()
        offers = (await s.execute(select(OfferSnapshot))).scalars().all()
        pedido = (await s.execute(select(ImportRequest))).scalar_one()
    assert body["import_id"] == str(run.id)
    assert (pedido.idempotency_key, pedido.import_run_id) == ("clave-de-prueba-01", run.id)
    assert {o.external_id for o in offers} == {"SKU-001", "SKU-002"}
    assert {o.data_origin for o in offers} == {"simulated"}  # la organización manda (D-037)


async def test_importacion_parcial_informa_cada_rechazo(api, sessions):
    content = xlsx(
        [
            fila(),  # 2 válida
            fila(sku="SIN-MAPEO"),  # 3 sin vínculo
            fila(sku="SKU-002", precio="n/a"),  # 4 precio inválido
        ]
    )
    r = await post(api, content)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed_with_errors"
    assert (body["rows_total"], body["rows_accepted"], body["rows_rejected"]) == (3, 1, 2)
    assert [(e["row"], e["code"]) for e in body["errors"]] == [
        (3, "UNMAPPED_PRODUCT"),
        (4, "INVALID_PRICE"),
    ]
    assert all(set(e) == {"row", "code", "message"} for e in body["errors"])
    assert await count(sessions, OfferSnapshot) == 1


async def test_no_crea_articulos_vinculos_ni_toca_la_cadena(api, sessions):
    async with sessions() as s:
        antes = {
            m.__name__: await s.scalar(select(func.count()).select_from(m))
            for m in (TradeItem, SupplierProductMapping, ChainProduct, ReferencePrice)
        }
    await post(api, xlsx([fila(), fila(sku="SIN-MAPEO")]))
    async with sessions() as s:
        despues = {
            m.__name__: await s.scalar(select(func.count()).select_from(m))
            for m in (TradeItem, SupplierProductMapping, ChainProduct, ReferencePrice)
        }
    assert antes == despues


# ---------- permisos, organización y sesión fuerte ----------


async def test_solo_administracion_importa(api, sessions):
    r = await post(api, who="aux")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"
    assert await count(sessions, ImportRun) == 0


async def test_una_fuente_de_otra_organizacion_no_es_accesible(api, sessions):
    r = await post(api, source_id=str(api.farm["source_ajena"]))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"
    assert await count(sessions, ImportRun) == 0


async def test_la_fuente_debe_pertenecer_al_proveedor_indicado(api, sessions):
    r = await post(api, supplier_id=str(api.farm["otro_supplier"]))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_SOURCE"
    assert await count(sessions, ImportRun) == 0


async def test_una_fuente_de_cadena_no_se_importa_por_aqui(api, sessions):
    r = await post(api, source_id=str(api.farm["chain"]))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_SOURCE"
    assert await count(sessions, ImportRun) == 0


async def test_real_test_exige_sesion_fuerte(api, sessions):
    r = await post(api, who="real_admin")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "STRONG_AUTH_REQUIRED"
    assert await count(sessions, ImportRun) == 0


# ---------- transporte: clave, límites y archivo inválido ----------


async def test_sin_clave_de_idempotencia_es_invalido(api, sessions):
    r = await api.post(
        "/api/v1/imports",
        files={"file": ("lista.xlsx", xlsx([fila()]), "application/octet-stream")},
        data={"supplier_id": str(api.farm["supplier"]), "source_id": str(api.farm["source"])},
        headers={"X-Demo-User-Id": str(api.farm["admin"])},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await count(sessions, ImportRun) == 0


async def test_un_archivo_ilegible_no_escribe_nada(api, sessions):
    r = await post(api, b"esto no es una planilla")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "IMPORT_FORMAT_INVALID"
    assert await count(sessions, ImportRun) == 0


async def test_un_archivo_sobre_el_limite_es_413(api, sessions, monkeypatch):
    monkeypatch.setattr(imports_api, "MAX_BYTES", 512)
    r = await post(api, xlsx([fila()]))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert await count(sessions, ImportRun) == 0


# ---------- idempotencia ----------


async def test_la_misma_clave_y_el_mismo_archivo_devuelven_el_original(api, sessions):
    content = xlsx([fila()])
    primero = await post(api, content)
    segundo = await post(api, content)
    assert (primero.status_code, segundo.status_code) == (200, 200)
    assert segundo.json() == primero.json()
    assert await count(sessions, ImportRun) == 1
    assert await count(sessions, OfferSnapshot) == 1


async def test_la_misma_clave_con_otro_archivo_es_409(api, sessions):
    await post(api, xlsx([fila()]))
    r = await post(api, xlsx([fila(precio="999.00")]))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert await count(sessions, ImportRun) == 1
    assert await count(sessions, OfferSnapshot) == 1


async def test_otra_clave_con_el_mismo_archivo_no_duplica(api, sessions):
    content = xlsx([fila()])
    primero = await post(api, content)
    segundo = await post(api, content, key="otra-clave-0002")
    assert segundo.status_code == 200
    assert segundo.json()["import_id"] == primero.json()["import_id"]
    assert await count(sessions, ImportRun) == 1
    assert await count(sessions, OfferSnapshot) == 1


async def test_dry_run_no_existe_en_la_api(api, sessions):
    r = await post(api, xlsx([fila()]), dry_run="true")
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
    assert await count(sessions, OfferSnapshot) == 1  # el campo extra se ignora, no es contrato


# ---------- la clave queda atada aunque la importación ya existiera (D-111) ----------


async def test_una_clave_nueva_sobre_un_archivo_ya_importado_queda_registrada(api, sessions):
    content = xlsx([fila()])
    primera = await post(api, content, key="clave-archivo-01")
    segunda = await post(api, content, key="clave-archivo-02")
    assert (primera.status_code, segunda.status_code) == (200, 200)
    assert segunda.json() == primera.json()  # misma corrida, sin duplicar
    assert await count(sessions, ImportRun) == 1
    async with sessions() as s:
        claves = {r.idempotency_key for r in (await s.execute(select(ImportRequest))).scalars()}
    assert claves == {"clave-archivo-01", "clave-archivo-02"}


async def test_la_clave_registrada_por_un_archivo_ya_importado_luego_da_409(api, sessions):
    content = xlsx([fila()])
    await post(api, content, key="clave-archivo-01")
    await post(api, content, key="clave-archivo-02")  # archivo ya importado, clave nueva
    r = await post(api, xlsx([fila(precio="777.00")]), key="clave-archivo-02")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert await count(sessions, ImportRun) == 1


async def test_la_misma_clave_con_otra_fuente_es_409(api, sessions):
    content = xlsx([fila()])
    await post(api, content, key="clave-fuente-01")
    r = await post(api, content, key="clave-fuente-01", source_id=str(api.farm["source_2"]))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert await count(sessions, ImportRun) == 1


async def test_la_misma_clave_y_el_mismo_archivo_no_duplica_el_registro(api, sessions):
    content = xlsx([fila()])
    primera = await post(api, content, key="clave-repetida-1")
    segunda = await post(api, content, key="clave-repetida-1")
    assert segunda.json() == primera.json()
    assert await count(sessions, ImportRequest) == 1
    assert await count(sessions, ImportRun) == 1


async def test_una_solicitud_fallida_no_consume_la_clave(api, sessions):
    r = await post(api, b"esto no es una planilla", key="clave-fallida-1")
    assert r.status_code == 422
    assert await count(sessions, ImportRequest) == 0
    # La misma clave sigue disponible para el reintento corregido.
    ok = await post(api, xlsx([fila()]), key="clave-fallida-1")
    assert ok.status_code == 200
    assert await count(sessions, ImportRequest) == 1


async def test_import_request_es_unica_por_organizacion_y_solo_agrega(api, sessions):
    await post(api, xlsx([fila()]))
    async with sessions() as s:
        req = (await s.execute(select(ImportRequest))).scalar_one()
        datos = {
            c.name: getattr(req, c.name)
            for c in ImportRequest.__table__.columns
            if c.name not in ("id", "created_at")
        }
    # Misma organización y misma clave: la base no admite dos contextos.
    async with sessions() as s:
        s.add(ImportRequest(**datos))
        with pytest.raises(IntegrityError):
            await s.flush()
        await s.rollback()
    # Y lo escrito no se reescribe.
    async with sessions() as s:
        with pytest.raises(DBAPIError, match="solo agregar"):
            await s.execute(
                text("UPDATE import_request SET file_hash = :h"), {"h": "0" * 64}
            )
        await s.rollback()
