"""Resumen de cotización: referencias, productos distintos y cantidades por unidad comercial.

`line_count` conserva su semántica de siempre (referencias cotizadas, no unidades). Los campos
nuevos son aditivos: `distinct_product_count` no cuenta dos veces el mismo artículo en dos
proveedores y `quantity_totals` nunca suma unidades distintas ni mezcla la unidad desconocida con
las conocidas. Un carrito descartado no aporta a ninguno de los tres.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.deps import get_session
from app.core.config import get_settings
from app.main import create_app
from app.models import (
    AppUser,
    OfferSnapshot,
    Organization,
    PurchaseCycle,
    QuoteLine,
    ShippingTermsSnapshot,
    Source,
    Supplier,
    TradeItem,
)

pytestmark = pytest.mark.db

NOW = datetime.now(UTC) - timedelta(hours=1)


def _attrs(ingredient: str, content: tuple[str, str] = ("20", "un")) -> dict:
    return {
        "schema_version": 2,
        "unassigned": [],
        "ingredient": ingredient,
        "strengths": [
            {
                "measure": {"value": "400", "unit": "mg"},
                "kind": "amount_per_unit",
                "basis": "explicit",
                "origin": "text",
            }
        ],
        "form": {"value": "comprimido", "basis": "explicit", "origin": "text"},
        "content": {
            "basis": "explicit",
            "origin": "text",
            "measure": {"value": content[0], "unit": content[1]},
        },
    }


@pytest.fixture
async def tienda(sessions):
    """Dos proveedores y tres artículos: el mismo artículo ofrecido por ambos proveedores."""
    ids: dict = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia totales", kind="simulation")
        s.add(org)
        await s.flush()
        ids["org"] = org.id

        admin = AppUser(organization_id=org.id, display_name="Admin", roles=["admin"])
        s.add(admin)
        await s.flush()
        ids["admin"] = admin.id

        cycle = PurchaseCycle(organization_id=org.id, name="Ciclo totales")
        s.add(cycle)
        await s.flush()
        ids["cycle"] = cycle.id

        uno = Supplier(organization_id=org.id, name="Proveedor Uno", data_origin="simulated")
        dos = Supplier(organization_id=org.id, name="Proveedor Dos", data_origin="simulated")
        s.add_all([uno, dos])
        await s.flush()
        ids["suppliers"] = {"uno": uno.id, "dos": dos.id}

        def _source(supplier: Supplier) -> Source:
            return Source(
                organization_id=org.id,
                kind="supplier",
                supplier_id=supplier.id,
                name=f"Fuente {supplier.name}",
                mode="manual_import",
                capabilities=["search", "price", "stock"],
                enabled=True,
                last_success_at=NOW,
                data_origin="simulated",
            )

        src_uno, src_dos = _source(uno), _source(dos)
        s.add_all([src_uno, src_dos])
        await s.flush()
        ids["sources"] = {"uno": src_uno.id, "dos": src_dos.id}

        for supplier, source in ((uno, src_uno), (dos, src_dos)):
            s.add(
                ShippingTermsSnapshot(
                    organization_id=org.id,
                    supplier_id=supplier.id,
                    source_id=source.id,
                    scheme="flat",
                    flat_amount="990.00",
                    currency="CLP",
                    observed_at=NOW,
                    data_origin="simulated",
                )
            )

        items = {
            "x": TradeItem(
                organization_id=org.id,
                display_name="Ibuprofeno 400 mg x 20 comprimidos",
                identity_key="fixture-totales-x",
                ingredient="ibuprofeno",
                attributes=_attrs("ibuprofeno"),
                data_origin="simulated",
            ),
            "y": TradeItem(
                organization_id=org.id,
                display_name="Paracetamol 400 mg x 20 comprimidos",
                identity_key="fixture-totales-y",
                ingredient="paracetamol",
                attributes=_attrs("paracetamol"),
                data_origin="simulated",
            ),
            "z": TradeItem(
                organization_id=org.id,
                display_name="Loratadina 400 mg x 20 comprimidos",
                identity_key="fixture-totales-z",
                ingredient="loratadina",
                attributes=_attrs("loratadina"),
                data_origin="simulated",
            ),
        }
        s.add_all(items.values())
        await s.flush()
        ids["items"] = {k: v.id for k, v in items.items()}

        def _offer(supplier, source, item, external_id, sale_unit="box") -> OfferSnapshot:
            return OfferSnapshot(
                organization_id=org.id,
                supplier_id=supplier.id,
                source_id=source.id,
                trade_item_id=item.id,
                external_id=external_id,
                price_amount="2000.00",
                currency="CLP",
                price_basis="per_commercial_unit",
                sale_unit=sale_unit,
                sale_unit_text="caja" if sale_unit == "box" else "unidad",
                stock_status="known",
                stock_quantity="90",
                stock_unit=sale_unit,
                tax_included=True,
                discount_included=True,
                minimum_known=True,
                minimum_quantity="1",
                minimum_unit=sale_unit,
                multiple_known=True,
                multiple_quantity="1",
                multiple_unit=sale_unit,
                observed_at=NOW,
                data_origin="simulated",
            )

        offers = {
            "uno_x": _offer(uno, src_uno, items["x"], "U-X"),
            "dos_x": _offer(dos, src_dos, items["x"], "D-X"),
            "uno_y": _offer(uno, src_uno, items["y"], "U-Y", sale_unit="unit"),
            "dos_z": _offer(dos, src_dos, items["z"], "D-Z", sale_unit="unit"),
        }
        s.add_all(offers.values())
        await s.flush()
        ids["offers"] = {k: v.id for k, v in offers.items()}
    return ids


@pytest.fixture
async def api(sessions, tienda, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    app = create_app()

    async def _s():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.tienda = tienda
        c.sessions = sessions
        yield c
    get_settings.cache_clear()


def H(api):
    return {"X-Demo-User-Id": str(api.tienda["admin"])}


async def _quote(api) -> str:
    r = await api.post("/api/v1/quotes", json={"title": "Totales"}, headers=H(api))
    assert r.status_code == 201, r.text
    return r.json()["quote_id"]


async def _add(api, quote_id, *, supplier, offer, value, unit="box") -> dict:
    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines",
        json={
            "supplier_id": str(api.tienda["suppliers"][supplier]),
            "offer_snapshot_id": str(api.tienda["offers"][offer]),
            "quantity": {"value": value, "unit": unit},
        },
        headers=H(api),
    )
    assert r.status_code == 201, r.text
    return r.json()["line"]


async def _list_item(api, quote_id) -> dict:
    r = await api.get("/api/v1/quotes", headers=H(api))
    assert r.status_code == 200, r.text
    return next(q for q in r.json()["quotes"] if q["quote_id"] == quote_id)


async def test_resumen_distingue_referencias_productos_y_cantidades_por_unidad(api):
    quote_id = await _quote(api)
    # El mismo artículo en dos proveedores: dos referencias, un solo producto distinto.
    await _add(api, quote_id, supplier="uno", offer="uno_x", value="3", unit="box")
    await _add(api, quote_id, supplier="dos", offer="dos_x", value="2", unit="box")
    await _add(api, quote_id, supplier="uno", offer="uno_y", value="4", unit="unit")
    await _add(api, quote_id, supplier="dos", offer="dos_z", value="1", unit="unit")

    item = await _list_item(api, quote_id)
    assert item["line_count"] == 4  # referencias, no unidades
    assert item["distinct_product_count"] == 3
    assert item["quantity_totals"] == [
        {"unit": "box", "quantity": "5"},
        {"unit": "unit", "quantity": "5"},
    ]

    r = await api.get(f"/api/v1/quotes/{quote_id}/summary", headers=H(api))
    assert r.status_code == 200, r.text
    resumen = r.json()
    assert resumen["distinct_product_count"] == 3
    assert resumen["quantity_totals"] == item["quantity_totals"]


async def test_unidad_desconocida_es_su_propio_grupo_y_no_se_suma_con_las_conocidas(api, sessions):
    quote_id = await _quote(api)
    await _add(api, quote_id, supplier="uno", offer="uno_x", value="3", unit="box")
    linea = await _add(api, quote_id, supplier="uno", offer="uno_y", value="2", unit="unit")
    # Una línea histórica puede no tener unidad comercial informada (migración 0030).
    async with sessions() as s, s.begin():
        row = (
            await s.execute(select(QuoteLine).where(QuoteLine.id == linea["line_id"]))
        ).scalar_one()
        row.quantity_unit = None
        row.quantity = Decimal("7")

    item = await _list_item(api, quote_id)
    assert item["line_count"] == 2
    assert item["quantity_totals"] == [
        {"unit": "box", "quantity": "3"},
        {"unit": None, "quantity": "7"},
    ]


async def test_carrito_descartado_no_aporta_al_resumen(api):
    quote_id = await _quote(api)
    await _add(api, quote_id, supplier="uno", offer="uno_x", value="3", unit="box")
    await _add(api, quote_id, supplier="dos", offer="dos_x", value="2", unit="box")

    r = await api.post(
        f"/api/v1/quotes/{quote_id}/carts/{api.tienda['suppliers']['dos']}/dismiss",
        headers=H(api),
    )
    assert r.status_code == 200, r.text

    item = await _list_item(api, quote_id)
    assert item["supplier_count"] == 1
    assert item["line_count"] == 1
    assert item["distinct_product_count"] == 1
    assert item["quantity_totals"] == [{"unit": "box", "quantity": "3"}]
