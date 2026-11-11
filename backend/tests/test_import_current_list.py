"""Precios ambiguos y lista vigente por fuente (D-151) — PostgreSQL real.

Una importación nueva no borra nada: los snapshots históricos siguen existiendo. La lectura
vigente, en cambio, solo considera las ofertas aceptadas de la última corrida con filas aceptadas
de cada fuente. Así PAR500-20 deja de presentarse como oferta actual cuando la lista siguiente ya
no lo trae, sin perder su historial, y una corrida fallida no reemplaza la última lista válida.
"""

from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.models import (
    AppUser,
    ImportProfile,
    ImportRun,
    OfferSnapshot,
    Organization,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
)
from app.services import import_profiles, search
from app.services.actors import load_actor
from app.services.imports import run_import

pytestmark = pytest.mark.db

HEADER = ["Código", "Descripción", "Precio", "Unidad", "Stock", "Condiciones"]
COLUMNS = {
    "sku": {"header": "Código"},
    "name": {"header": "Descripción"},
    "price": {"header": "Precio"},
    "sale_unit": {"header": "Unidad"},
    "stock": {"header": "Stock"},
    "conditions": {"header": "Condiciones"},
}
OPTIONS = {
    "currency": "CLP",
    "price_basis": "per_commercial_unit",
    "decimal_separator": ".",
    "thousands_separator": None,
}


def fila(sku: str, precio, descripcion: str = "Paracetamol 500 mg x 20") -> list:
    return [sku, descripcion, precio, "CAJA", 120, ""]


def xlsx(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Lista"
    ws.append(HEADER)
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
async def farm(sessions):
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia lista vigente", kind="simulation")
        s.add(org)
        await s.flush()
        ids["org"] = org.id
        admin = AppUser(organization_id=org.id, display_name="Pao", roles=["admin"])
        s.add(admin)
        await s.flush()
        ids["admin"] = admin.id
        supplier = Supplier(
            organization_id=org.id, name="Distribuidora Andes", data_origin="simulated"
        )
        s.add(supplier)
        await s.flush()
        ids["supplier"] = supplier.id
        source = Source(
            organization_id=org.id,
            kind="supplier",
            supplier_id=supplier.id,
            name="Lista Andes",
            mode="manual_import",
            data_origin="simulated",
        )
        s.add(source)
        await s.flush()
        ids["source"] = source.id
        old_item = TradeItem(
            organization_id=org.id,
            identity_key="paracetamol-viejo",
            display_name="Paracetamol 500 mg x 20",
            data_origin="simulated",
        )
        new_item = TradeItem(
            organization_id=org.id,
            identity_key="paracetamol-andes",
            display_name="Paracetamol 500 mg 20 comprimidos",
            data_origin="simulated",
        )
        s.add_all([old_item, new_item])
        await s.flush()
        ids["old_item"], ids["new_item"] = old_item.id, new_item.id
        for external, item_id in (("PAR500-20", old_item.id), ("AND-001", new_item.id)):
            s.add(
                SupplierProductMapping(
                    organization_id=org.id,
                    source_id=source.id,
                    supplier_id=supplier.id,
                    trade_item_id=item_id,
                    external_id=external,
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
                units={"CAJA": "box"},
                options=OPTIONS,
                data_origin="simulated",
            )
        )
        await s.flush()
    return ids


async def run(sessions, who, source_id, content, *, dry_run=False):
    async with sessions() as s:
        actor = await load_actor(s, who)
        try:
            outcome = await run_import(
                s,
                actor,
                source_id=source_id,
                file_name="lista.xlsx",
                content=content,
                dry_run=dry_run,
            )
            await s.commit()
            return outcome
        except BaseException:
            await s.rollback()
            raise


async def count(sessions, model) -> int:
    async with sessions() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def snapshots(sessions):
    async with sessions() as s:
        return list((await s.execute(select(OfferSnapshot))).scalars())


# --------------------------------------------------------------- precios ambiguos


async def test_dos_mil_novecientos_cuarenta_no_se_importa_sin_confirmacion(sessions, farm):
    content = xlsx([fila("AND-001", "2.490")])

    revision = await run(sessions, farm["admin"], farm["source"], content, dry_run=True)
    assert revision.price_format is not None
    assert revision.price_format["status"] == "ambiguous"
    assert revision.price_format["requires_confirmation"] is True

    with pytest.raises(DomainError) as error:
        await run(sessions, farm["admin"], farm["source"], content)
    assert error.value.code == "IMPORT_FORMAT_INVALID"
    assert error.value.status_code == 422
    assert "ambiguo" in error.value.message

    # Nada se escribió: ni oferta ni corrida de importación.
    assert await count(sessions, OfferSnapshot) == 0
    assert await count(sessions, ImportRun) == 0


async def test_la_confirmacion_de_miles_guarda_2490_y_no_2_49(sessions, farm):
    async with sessions() as s, s.begin():
        profile = (await s.execute(select(ImportProfile))).scalar_one()
        profile.options = {
            **OPTIONS,
            "decimal_separator": ",",
            "thousands_separator": ".",
            "price_format_confirmed": True,
        }

    outcome = await run(sessions, farm["admin"], farm["source"], xlsx([fila("AND-001", "2.490")]))
    assert outcome.status == "completed"
    offer = (await snapshots(sessions))[0]
    assert offer.price_amount == Decimal("2490")


async def test_una_celda_numerica_de_excel_conserva_su_valor(sessions, farm):
    # Excel ya resolvió el número: 2490 no se convierte en 249000 ni 2.49 en 249.
    outcome = await run(
        sessions,
        farm["admin"],
        farm["source"],
        xlsx([fila("AND-001", 2490), fila("PAR500-20", 12.5)]),
    )
    assert outcome.status == "completed"
    precios = {o.external_id: o.price_amount for o in await snapshots(sessions)}
    assert precios == {"AND-001": Decimal("2490"), "PAR500-20": Decimal("12.5")}


# --------------------------------------------------------------- lista vigente e historial


async def test_la_lista_nueva_reemplaza_la_lectura_actual_sin_borrar_historial(sessions, farm):
    await run(sessions, farm["admin"], farm["source"], xlsx([fila("PAR500-20", "1012")]))
    await run(sessions, farm["admin"], farm["source"], xlsx([fila("AND-001", "2490")]))

    # El historial tiene las dos observaciones: nada se borró ni se sobrescribió.
    historial = await snapshots(sessions)
    assert {(o.external_id, o.price_amount) for o in historial} == {
        ("PAR500-20", Decimal("1012")),
        ("AND-001", Decimal("2490")),
    }

    async with sessions() as s:
        actor = await load_actor(s, farm["admin"])
        vigentes = await search._latest_offers(
            s,
            farm["org"],
            [farm["old_item"], farm["new_item"]],
            now=historial[0].observed_at,
        )
        # PAR500-20 ya no está en la lista vigente; AND-001 sí.
        assert farm["old_item"] not in vigentes
        assert [o.external_id for o in vigentes[farm["new_item"]]] == ["AND-001"]

        detalle_viejo = await search.article_detail(s, actor, farm["old_item"])
        detalle_nuevo = await search.article_detail(s, actor, farm["new_item"])
        assert detalle_viejo["offers"] == []
        assert detalle_viejo["offers_total"] == 0
        assert [o["external_id"] for o in detalle_nuevo["offers"]] == ["AND-001"]

        ofertas_proveedor = await import_profiles.latest_offers(s, actor, farm["supplier"])
        assert [o["external_id"] for o in ofertas_proveedor] == ["AND-001"]


async def test_una_corrida_fallida_no_reemplaza_la_ultima_lista_valida(sessions, farm):
    await run(sessions, farm["admin"], farm["source"], xlsx([fila("AND-001", "2490")]))

    fallida = await run(sessions, farm["admin"], farm["source"], xlsx([fila("AND-001", "n/a")]))
    assert (fallida.status, fallida.rows_accepted) == ("failed", 0)

    async with sessions() as s:
        actor = await load_actor(s, farm["admin"])
        ofertas = await import_profiles.latest_offers(s, actor, farm["supplier"])
        assert [(o["external_id"], o["price_amount"]) for o in ofertas] == [
            ("AND-001", "2490.0000")
        ]
        vigentes = await search._latest_offers(
            s, farm["org"], [farm["new_item"]], now=ofertas[0]["observed_at"]
        )
        assert len(vigentes[farm["new_item"]]) == 1
    # La corrida fallida queda registrada, pero no aporta ofertas ni reemplaza la válida.
    assert await count(sessions, ImportRun) == 2
    assert await count(sessions, OfferSnapshot) == 1


async def test_una_fila_rechazada_de_la_lista_nueva_no_sigue_siendo_precio_actual(sessions, farm):
    await run(
        sessions,
        farm["admin"],
        farm["source"],
        xlsx([fila("PAR500-20", "1012"), fila("AND-001", "2490")]),
    )
    # La lista nueva trae PAR500-20 con un precio ilegible: se rechaza y no debe seguir vigente.
    await run(
        sessions,
        farm["admin"],
        farm["source"],
        xlsx([fila("PAR500-20", "n/a"), fila("AND-001", "2590")]),
    )

    async with sessions() as s:
        actor = await load_actor(s, farm["admin"])
        ofertas = await import_profiles.latest_offers(s, actor, farm["supplier"])
        assert [(o["external_id"], o["price_amount"]) for o in ofertas] == [
            ("AND-001", "2590.0000")
        ]
        vigentes = await search._latest_offers(
            s,
            farm["org"],
            [farm["old_item"], farm["new_item"]],
            now=ofertas[0]["observed_at"],
        )
        assert farm["old_item"] not in vigentes
    # El precio histórico de PAR500-20 sigue guardado.
    assert any(o.external_id == "PAR500-20" for o in await snapshots(sessions))


async def test_el_filtro_de_lista_vigente_no_oculta_las_ofertas_api(sessions, farm):
    """Una fuente en vivo no tiene `import_run`: el filtro no la toca ni la compara por antigüedad.

    La comparación de proveedores (E5) y la cotización consumen este mismo `_latest_offers`, así
    que si la oferta API sigue aquí, sigue disponible como candidata.
    """
    async with sessions() as s, s.begin():
        api_source = Source(
            organization_id=farm["org"],
            kind="supplier",
            supplier_id=farm["supplier"],
            name="API Andes",
            mode="api",
            data_origin="simulated",
        )
        s.add(api_source)
        await s.flush()
        s.add(
            OfferSnapshot(
                organization_id=farm["org"],
                supplier_id=farm["supplier"],
                source_id=api_source.id,
                trade_item_id=farm["old_item"],
                external_id="PAR500-20",
                price_amount="900",
                currency="CLP",
                sale_unit="box",
                stock_status="unknown",
                observed_at=datetime(2026, 11, 9, tzinfo=UTC),
                data_origin="simulated",
            )
        )

    # La lista importada ya no trae PAR500-20; la oferta API no participa de esa corrida.
    await run(sessions, farm["admin"], farm["source"], xlsx([fila("AND-001", "2490")]))

    async with sessions() as s:
        actor = await load_actor(s, farm["admin"])
        vigentes = await search._latest_offers(
            s,
            farm["org"],
            [farm["old_item"], farm["new_item"]],
            now=datetime(2026, 11, 11, tzinfo=UTC),
        )
        assert [o.price_amount for o in vigentes[farm["old_item"]]] == [Decimal("900")]
        assert [o.external_id for o in vigentes[farm["new_item"]]] == ["AND-001"]

        detalle = await search.article_detail(s, actor, farm["old_item"])
        assert [o["external_id"] for o in detalle["offers"]] == ["PAR500-20"]
        assert detalle["offers_total"] == 1
