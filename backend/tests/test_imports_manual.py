"""Conector `manual_import` (E8): planillas .xlsx originales → ofertas fechadas — PostgreSQL real.

Comprueba las dos fases de D-103 (estructural y por fila), los códigos cerrados de D-090, las filas
rechazadas conservadas con lo mínimo para localizarlas (D-104), el dinero declarado (D-105), el estado
de revisión de la oferta (D-110), la idempotencia por hash de archivo y —lo más importante— que la
importación NO toca el catálogo propio ni los precios públicos de cadena (D-106). Las planillas son
`fixture`: se generan aquí, no son de ningún proveedor.
"""

import uuid
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import DomainError
from app.models import (
    AppUser,
    AuditEvent,
    ChainProduct,
    ImportProfile,
    ImportRun,
    OfferSnapshot,
    Organization,
    OutboxEvent,
    ReferencePrice,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
)
from app.services.actors import load_actor
from app.services.imports import run_import

pytestmark = pytest.mark.db

HEADER = [
    "Código",
    "Descripción",
    "Precio",
    "Moneda",
    "Base",
    "Unidad",
    "Mínimo",
    "Múltiplo",
    "Stock",
    "Fecha",
    "Condiciones",
]
COLUMNS = {
    "sku": {"header": "Código"},
    "name": {"header": "Descripción"},
    "price": {"header": "Precio", "aliases": ["Precio neto"]},
    "currency": {"header": "Moneda"},
    "basis": {"header": "Base"},
    "sale_unit": {"header": "Unidad"},
    "minimum": {"header": "Mínimo"},
    "multiple": {"header": "Múltiplo"},
    "stock": {"header": "Stock"},
    "observed_at": {"header": "Fecha"},
    "conditions": {"header": "Condiciones"},
}
UNITS = {"CAJA": "box", "UNIDAD": "unit", "PACK": "package", "BOTELLA": "bottle"}
OPTIONS = {
    "decimal_separator": ",",
    "thousands_separator": ".",
    "date_format": "DD/MM/YYYY",
    "tax_included": False,
}


def fila(sku="SKU-001", precio="12.345,60", moneda="CLP", base="per_commercial_unit", **over):
    """Una fila de planilla con los valores en el orden de HEADER."""
    values = {
        "Descripción": "LOSARTAN 50 X 30",
        "Mínimo": 5,
        "Múltiplo": 1,
        "Stock": 40,
        "Fecha": "15/10/2026",
        "Condiciones": "",
    }
    values.update(over)
    return [
        sku,
        values["Descripción"],
        precio,
        moneda,
        base,
        values.get("Unidad", "CAJA"),
        values["Mínimo"],
        values["Múltiplo"],
        values["Stock"],
        values["Fecha"],
        values["Condiciones"],
    ]


def xlsx(rows, header=HEADER, sheet="Lista") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
async def orgs(sessions):
    """Fuente de proveedor con catálogo mapeado, fuente de cadena y una organización de prueba real."""
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia importación", kind="simulation")
        real = Organization(name="Farmacia real importación", kind="real_test")
        s.add_all([org, real])
        await s.flush()
        ids["org"], ids["real"] = org.id, real.id
        for key, name, roles, o in [
            ("admin", "Yarella", ["admin"], org),
            ("aux", "Danny", ["auxiliary"], org),
            ("real_admin", "Yarella real", ["admin"], real),
        ]:
            u = AppUser(organization_id=o.id, display_name=name, roles=roles)
            s.add(u)
            await s.flush()
            ids[key] = u.id
        sup = Supplier(organization_id=org.id, name="Proveedor Uno", data_origin="simulated")
        s.add(sup)
        await s.flush()
        ids["supplier"] = sup.id
        proveedor = Source(
            organization_id=org.id, kind="supplier", supplier_id=sup.id, name="Lista Uno",
            mode="manual_import", data_origin="simulated",
        )  # fmt: skip
        cadena = Source(
            organization_id=org.id, kind="chain_reference", name="Catálogo cadena",
            mode="manual_import", data_origin="simulated",
        )  # fmt: skip
        s.add_all([proveedor, cadena])
        await s.flush()
        ids["source"], ids["chain"] = proveedor.id, cadena.id
        for key, name in (("item_a", "Losartán 50 mg x 30"), ("item_b", "Cepillo dental")):
            item = TradeItem(
                organization_id=org.id, identity_key=key, display_name=name, data_origin="simulated"
            )
            s.add(item)
            await s.flush()
            ids[key] = item.id
        for sku in ("SKU-001", "SKU-002"):
            s.add(
                SupplierProductMapping(
                    organization_id=org.id,
                    source_id=proveedor.id,
                    supplier_id=sup.id,
                    trade_item_id=ids["item_a" if sku == "SKU-001" else "item_b"],
                    external_id=sku,
                    origin="explicit",
                    data_origin="simulated",
                )
            )
        # SKU-003..SKU-010 también están vinculados: así cada fila inválida puede tener un código
        # distinto y probar el motivo que corresponde, no un duplicado.
        for extra in range(3, 11):
            s.add(
                SupplierProductMapping(
                    organization_id=org.id,
                    source_id=proveedor.id,
                    supplier_id=sup.id,
                    trade_item_id=ids["item_a"],
                    external_id=f"SKU-{extra:03d}",
                    origin="explicit",
                    data_origin="simulated",
                )
            )
        s.add(
            ImportProfile(
                organization_id=org.id,
                source_id=proveedor.id,
                sheet="Lista",
                header_row=1,
                columns=COLUMNS,
                units=UNITS,
                options=OPTIONS,
                data_origin="simulated",
            )
        )
        await s.flush()
    return ids


async def run(sessions, who, source_id, content, *, file_name="lista.xlsx", dry_run=False):
    async with sessions() as s:
        actor = await load_actor(s, who)
        try:
            outcome = await run_import(
                s, actor, source_id=source_id, file_name=file_name, content=content, dry_run=dry_run
            )
            await s.commit()
            return outcome
        except BaseException:
            await s.rollback()
            raise


async def count(sessions, model) -> int:
    async with sessions() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def all_of(sessions, model):
    async with sessions() as s:
        return list((await s.execute(select(model))).scalars())


async def one(sessions, model, *where):
    async with sessions() as s:
        return (await s.execute(select(model).where(*where))).scalar_one_or_none()


# ---------- importación completa ----------


async def test_importa_ofertas_con_dinero_y_cantidades_declaradas(sessions, orgs):
    content = xlsx([fila(), fila(sku="SKU-002", precio="999,00", **{"Unidad": "UNIDAD"})])
    outcome = await run(sessions, orgs["admin"], orgs["source"], content)

    assert (outcome.status, outcome.rows_total, outcome.rows_accepted, outcome.rows_rejected) == (
        "completed",
        2,
        2,
        0,
    )
    offers = {o.external_id: o for o in await all_of(sessions, OfferSnapshot)}
    a = offers["SKU-001"]
    assert a.price_amount == Decimal("12345.60")
    assert (a.currency, a.price_basis, a.sale_unit, a.sale_unit_text) == (
        "CLP",
        "per_commercial_unit",
        "box",
        "CAJA",
    )
    assert (a.minimum_known, a.minimum_quantity, a.minimum_unit) == (True, Decimal("5"), "box")
    assert (a.stock_status, a.stock_quantity, a.stock_unit) == ("known", Decimal("40"), "box")
    assert (a.tax_included, a.discount_included) == (False, None)  # lo no declarado es NULL
    # Fecha en hora de Chile → UTC, y procedencia de la organización.
    assert a.observed_at.isoformat() == "2026-10-15T03:00:00+00:00"
    assert (a.data_origin, a.trade_item_id) == ("simulated", orgs["item_a"])
    assert offers["SKU-002"].sale_unit == "unit"

    run_row = await one(sessions, ImportRun)
    assert (run_row.status, run_row.rows_total, run_row.profile_id) == ("completed", 2, run_row.profile_id)
    assert run_row.profile_id is not None and run_row.rejected_rows == []
    assert run_row.profile_snapshot["units"] == UNITS
    assert await count(sessions, AuditEvent) == 1
    assert await count(sessions, OutboxEvent) == 1


async def test_un_segundo_archivo_agrega_historial_y_no_sobrescribe(sessions, orgs):
    await run(sessions, orgs["admin"], orgs["source"], xlsx([fila(precio="10.000,00")]))
    await run(
        sessions, orgs["admin"], orgs["source"], xlsx([fila(precio="11.000,00")]), file_name="b.xlsx"
    )
    ofertas = [o for o in await all_of(sessions, OfferSnapshot) if o.external_id == "SKU-001"]
    assert sorted(o.price_amount for o in ofertas) == [Decimal("10000.00"), Decimal("11000.00")]
    assert await count(sessions, TradeItem) == 2  # el catálogo no se toca
    assert await count(sessions, SupplierProductMapping) == 10


async def test_el_mismo_archivo_no_se_importa_dos_veces(sessions, orgs):
    content = xlsx([fila()])
    first = await run(sessions, orgs["admin"], orgs["source"], content)
    again = await run(sessions, orgs["admin"], orgs["source"], content, file_name="copia.xlsx")
    assert again.repeated is True
    assert again.import_id == first.import_id
    assert (again.status, again.rows_accepted) == (first.status, first.rows_accepted)
    assert await count(sessions, ImportRun) == 1
    assert await count(sessions, OfferSnapshot) == 1
    assert await count(sessions, AuditEvent) == 1


async def test_dry_run_valida_y_cuenta_sin_escribir(sessions, orgs):
    outcome = await run(
        sessions, orgs["admin"], orgs["source"], xlsx([fila(), fila(sku="NOPE")]), dry_run=True
    )
    assert (outcome.status, outcome.rows_accepted, outcome.rows_rejected) == (
        "completed_with_errors",
        1,
        1,
    )
    assert await count(sessions, ImportRun) == 0
    assert await count(sessions, OfferSnapshot) == 0
    assert await count(sessions, AuditEvent) == 0


# ---------- permisos y fuentes ----------


async def test_solo_administracion_importa(sessions, orgs):
    with pytest.raises(DomainError) as e:
        await run(sessions, orgs["aux"], orgs["source"], xlsx([fila()]))
    assert e.value.code == "FORBIDDEN"
    assert await count(sessions, ImportRun) == 0


async def test_una_fuente_de_cadena_se_rechaza_antes_de_leer(sessions, orgs):
    with pytest.raises(DomainError) as e:
        await run(sessions, orgs["admin"], orgs["chain"], xlsx([fila()]))
    assert e.value.code == "INVALID_SOURCE"
    assert await count(sessions, ImportRun) == 0
    assert await count(sessions, ChainProduct) == 0
    assert await count(sessions, ReferencePrice) == 0


async def test_no_escribe_nada_fuera_de_import_run_y_offer_snapshot(sessions, orgs):
    await run(sessions, orgs["admin"], orgs["source"], xlsx([fila(), fila(sku="SIN-MAPEO")]))
    assert await count(sessions, TradeItem) == 2
    assert await count(sessions, SupplierProductMapping) == 10
    assert await count(sessions, ChainProduct) == 0
    assert await count(sessions, ReferencePrice) == 0


# ---------- validación estructural: no se escribe nada ----------


async def test_una_cabecera_que_no_calza_no_escribe_nada(sessions, orgs):
    content = xlsx([fila()], header=["Codigo", "Nombre", "Valor", "Moneda", "Base", "Unidad",
                                     "Mínimo", "Múltiplo", "Stock", "Fecha", "Condiciones"])  # fmt: skip

    with pytest.raises(DomainError) as e:
        await run(sessions, orgs["admin"], orgs["source"], content)
    assert e.value.code == "IMPORT_FORMAT_INVALID"
    assert "sku" in e.value.message
    assert await count(sessions, ImportRun) == 0
    assert await count(sessions, OfferSnapshot) == 0


async def test_un_archivo_ilegible_es_error_de_formato(sessions, orgs):
    with pytest.raises(DomainError) as e:
        await run(sessions, orgs["admin"], orgs["source"], b"esto no es una planilla")
    assert e.value.code == "IMPORT_FORMAT_INVALID"
    assert await count(sessions, ImportRun) == 0


async def test_un_archivo_sobre_el_limite_se_rechaza(sessions, orgs):
    with pytest.raises(DomainError) as e:
        await run(sessions, orgs["admin"], orgs["source"], b"x" * (10 * 1024 * 1024 + 1))
    assert e.value.code == "PAYLOAD_TOO_LARGE"
    assert await count(sessions, ImportRun) == 0


async def test_sin_perfil_no_se_puede_leer(sessions, orgs):
    async with sessions() as s, s.begin():
        await s.execute(
            ImportProfile.__table__.delete().where(ImportProfile.source_id == orgs["source"])
        )
    with pytest.raises(DomainError) as e:
        await run(sessions, orgs["admin"], orgs["source"], xlsx([fila()]))
    assert e.value.code == "IMPORT_FORMAT_INVALID"
    assert await count(sessions, OfferSnapshot) == 0


# ---------- validación por fila ----------


async def test_cada_fila_rechazada_queda_con_su_codigo_y_sus_valores(sessions, orgs):
    content = xlsx(
        [
            fila(sku="SIN-MAPEO"),  # 2
            fila(sku=None),  # 3
            fila(sku="SKU-003", precio="n/a"),  # 4
            fila(sku="SKU-004", moneda="PESOS"),  # 5
            fila(sku="SKU-005", base="por caja"),  # 6
            fila(sku="SKU-006", **{"Unidad": "DOCENA"}),  # 7
            fila(sku="SKU-001", precio="1.000,00"),  # 8 válida
            fila(sku="SKU-001", precio="2.000,00"),  # 9 duplicada
            fila(sku="SKU-002", **{"Mínimo": "2,5"}),  # 10 mínimo fraccionario
            fila(sku="SKU-007", precio="3.000,00"),  # 11 válida
        ]
    )
    outcome = await run(sessions, orgs["admin"], orgs["source"], content)

    assert outcome.status == "completed_with_errors"
    assert (outcome.rows_total, outcome.rows_accepted, outcome.rows_rejected) == (10, 2, 8)
    assert outcome.rows_accepted + outcome.rows_rejected == outcome.rows_total
    assert [(e["row"], e["code"]) for e in outcome.errors] == [
        (2, "UNMAPPED_PRODUCT"),
        (3, "MISSING_REQUIRED_FIELD"),
        (4, "INVALID_PRICE"),
        (5, "INVALID_CURRENCY"),
        (6, "INVALID_BASIS"),
        (7, "UNKNOWN_UNIT"),
        (9, "DUPLICATE_ROW"),
        (10, "INVALID_QUANTITY"),
    ]
    run_row = await one(sessions, ImportRun)
    assert [r["row"] for r in run_row.rejected_rows] == [2, 3, 4, 5, 6, 7, 9, 10]
    primero = run_row.rejected_rows[0]
    # La fila rechazada conserva lo mínimo para localizarla y entenderla… (D-104, D-110)
    assert set(primero) == {"row", "codes", "source_id", "file_hash", "sku", "name", "fields"}
    assert (primero["sku"], primero["name"]) == ("SIN-MAPEO", "LOSARTAN 50 X 30")
    assert primero["codes"] == ["UNMAPPED_PRODUCT"]
    assert primero["fields"] == {"sku": "SIN-MAPEO"}
    assert primero["file_hash"] == run_row.file_hash
    # …y solo las celdas que causaron cada rechazo, nunca la fila entera.
    por_fila = {r["row"]: r for r in run_row.rejected_rows}
    assert por_fila[4]["fields"] == {"price": "n/a"}
    assert por_fila[5]["fields"] == {"currency": "PESOS"}
    assert por_fila[7]["fields"] == {"sale_unit": "DOCENA"}
    assert por_fila[10]["fields"] == {"minimum": "2,5"}
    assert "values" not in primero
    assert await count(sessions, OfferSnapshot) == 2


async def test_las_cantidades_desconocidas_quedan_en_null(sessions, orgs):
    content = xlsx([fila(**{"Mínimo": "", "Múltiplo": "", "Stock": ""})])
    await run(sessions, orgs["admin"], orgs["source"], content)
    offer = await one(sessions, OfferSnapshot)
    assert (offer.minimum_known, offer.minimum_quantity, offer.minimum_unit) == (False, None, None)
    assert (offer.multiple_known, offer.multiple_quantity) == (False, None)
    assert (offer.stock_status, offer.stock_quantity, offer.stock_unit) == ("unknown", None, None)


async def test_una_condicion_no_modelada_se_acepta_con_aviso(sessions, orgs):
    content = xlsx([fila(**{"Condiciones": "Pago a 30 días con factura"})])
    outcome = await run(sessions, orgs["admin"], orgs["source"], content)
    assert (outcome.status, outcome.rows_accepted) == ("completed", 1)
    assert [(e["row"], e["code"]) for e in outcome.errors] == [(2, "UNMODELLED_CONDITION")]
    offer = await one(sessions, OfferSnapshot)
    assert offer.notes == "Pago a 30 días con factura"
    # La oferta existe, pero no es una oferta verificada: queda marcada (D-110).
    assert offer.review_status == "needs_review"
    # La respuesta HTTP conserva exactamente la forma congelada de C0 (E8).
    assert set(outcome.as_response()) == {
        "import_id",
        "status",
        "rows_total",
        "rows_accepted",
        "rows_rejected",
        "errors",
        "observed_at",
    }


async def test_una_oferta_sin_condiciones_queda_verificada(sessions, orgs):
    await run(sessions, orgs["admin"], orgs["source"], xlsx([fila()]))
    offer = await one(sessions, OfferSnapshot)
    assert offer.review_status == "accepted"


async def test_la_base_no_acepta_una_oferta_marcada_sin_motivo(sessions, orgs):
    await run(sessions, orgs["admin"], orgs["source"], xlsx([fila()]))
    offer = await one(sessions, OfferSnapshot)
    async with sessions() as s:
        datos = {
            c.name: getattr(offer, c.name)
            for c in OfferSnapshot.__table__.columns
            if c.name not in ("id", "created_at")
        }
        for over in (
            {"review_status": "needs_review", "notes": None},  # marcada sin motivo
            {"review_status": "revisar"},  # estado fuera del vocabulario
        ):
            copia = OfferSnapshot(**{**datos, **over, "external_id": uuid.uuid4().hex[:12]})
            s.add(copia)
            with pytest.raises(IntegrityError):
                await s.flush()
            await s.rollback()


async def test_una_fila_solo_de_encabezado_termina_en_failed(sessions, orgs):
    outcome = await run(sessions, orgs["admin"], orgs["source"], xlsx([]))
    assert (outcome.status, outcome.rows_total) == ("failed", 0)
    assert await count(sessions, OfferSnapshot) == 0


# ---------- perfiles ----------


async def test_el_perfil_por_opciones_evita_las_columnas_de_moneda_y_base(sessions, orgs):
    async with sessions() as s, s.begin():
        perfil = (
            await s.execute(select(ImportProfile).where(ImportProfile.source_id == orgs["source"]))
        ).scalar_one()
        perfil.columns = {
            "sku": {"header": "Código"},
            "price": {"header": "Precio"},
            "sale_unit": {"header": "Unidad", "aliases": ["Formato"]},
        }
        perfil.options = {
            "currency": "clp",
            "price_basis": "per_base_unit",
            "decimal_separator": ",",
            "thousands_separator": ".",
            "date_format": "DD/MM/YYYY",
        }
        perfil.version = 2
    content = xlsx(
        [["SKU-001", "", "10,00", "", "", "CAJA", "", "", "", "", ""]],
    )
    outcome = await run(sessions, orgs["admin"], orgs["source"], content)
    assert (outcome.status, outcome.rows_accepted) == ("completed", 1)
    offer = await one(sessions, OfferSnapshot)
    assert (offer.currency, offer.price_basis) == ("CLP", "per_base_unit")
    run_row = await one(sessions, ImportRun)
    assert run_row.profile_snapshot["version"] == 2


async def test_una_base_de_precio_no_declarada_no_se_supone(sessions, orgs):
    async with sessions() as s, s.begin():
        perfil = (
            await s.execute(select(ImportProfile).where(ImportProfile.source_id == orgs["source"]))
        ).scalar_one()
        perfil.columns = {
            "sku": {"header": "Código"},
            "price": {"header": "Precio"},
            "sale_unit": {"header": "Unidad"},
        }
        perfil.options = {"currency": "CLP"}
    content = xlsx([["SKU-001", "", "10.00", "", "", "CAJA", "", "", "", "", ""]])
    outcome = await run(sessions, orgs["admin"], orgs["source"], content)
    assert outcome.rows_rejected == 1
    assert [e["code"] for e in outcome.errors] == ["INVALID_BASIS"]


async def test_una_fuente_inexistente_es_not_found(sessions, orgs):
    with pytest.raises(DomainError) as e:
        await run(sessions, orgs["admin"], uuid.uuid4(), xlsx([fila()]))
    assert e.value.code == "NOT_FOUND"


# ---------- procedencia ----------


async def test_en_prueba_real_la_importacion_es_captured(sessions, orgs):
    async with sessions() as s, s.begin():
        sup = Supplier(organization_id=orgs["real"], name="Proveedor real", data_origin="captured")
        s.add(sup)
        await s.flush()
        src = Source(
            organization_id=orgs["real"], kind="supplier", supplier_id=sup.id, name="Lista real",
            mode="manual_import", data_origin="captured",
        )  # fmt: skip
        s.add(src)
        await s.flush()
        item = TradeItem(
            organization_id=orgs["real"], identity_key="real-1", display_name="Losartán",
            data_origin="captured",
        )  # fmt: skip
        s.add(item)
        await s.flush()
        s.add(
            SupplierProductMapping(
                organization_id=orgs["real"], source_id=src.id, supplier_id=sup.id,
                trade_item_id=item.id, external_id="SKU-001", origin="explicit",
                data_origin="captured",
            )
        )  # fmt: skip
        s.add(
            ImportProfile(
                organization_id=orgs["real"], source_id=src.id, sheet="Lista", columns=COLUMNS,
                units=UNITS, options=OPTIONS, data_origin="captured",
            )
        )  # fmt: skip
        real_source = src.id
    outcome = await run(sessions, orgs["real_admin"], real_source, xlsx([fila()]))
    assert outcome.status == "completed"
    offer = await one(sessions, OfferSnapshot)
    assert offer.data_origin == "captured"
    assert (await one(sessions, ImportRun)).data_origin == "captured"
