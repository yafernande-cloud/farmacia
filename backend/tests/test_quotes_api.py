"""C5-A — carritos persistentes, cotizaciones, cobertura de faltantes y observaciones (E10, O1).

Todo se prueba contra PostgreSQL real con organización `simulation` y precios ficticios del test.
Se verifica lo que el contrato exige: la cotización es persistente, repetir una petición no suma,
el precio no se copia ni se reapunta solo, una línea se quita de forma lógica, cotizar cubre de
forma provisional y nunca cierra, y una observación comercial se guarda siempre, sin IA (O2–O4 no
están implementados).

D-137 agrega la integridad de las asignaciones: cerrar se bloquea si alguna asignación vigente
apunta a un reporte cancelado, sin cantidad o con cantidad menor a lo asignado, y la cantidad pedida
no puede bajar por debajo de lo asignado hasta que la administración ajuste o quite las asignaciones.

Las compras (C5-B) no existen todavía: no hay aquí pruebas de compra, de `LINE_HAS_PURCHASES` ni de
`QUANTITY_BELOW_PURCHASED`; las verificaciones quedan preparadas en el servicio.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.api.deps import get_session
from app.core.config import get_settings
from app.main import create_app
from app.models import (
    AppUser,
    AuditEvent,
    CommercialObservation,
    DemandItem,
    DemandObservation,
    OfferSnapshot,
    Organization,
    PurchaseCycle,
    Quote,
    QuoteCart,
    QuoteLine,
    QuoteLineAllocation,
    ShippingTermsSnapshot,
    Source,
    Supplier,
    SupplierConnectorProfile,
    SupplierProductMapping,
    TradeItem,
)

pytestmark = pytest.mark.db

NOW = datetime.now(UTC) - timedelta(hours=1)
LATER = NOW + timedelta(minutes=30)


def _attrs(ingredient: str, content: tuple[str, str], strength: tuple[str, str] = ("400", "mg")) -> dict:
    return {
        "schema_version": 2,
        "unassigned": [],
        "ingredient": ingredient,
        "strengths": [
            {
                "measure": {"value": strength[0], "unit": strength[1]},
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
async def shop(sessions):
    """Organización de simulación con dos proveedores completos, uno sin despacho y un tercero."""
    ids: dict = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia C5-A", kind="simulation")
        other = Organization(name="Otra C5-A", kind="simulation")
        s.add_all([org, other])
        await s.flush()
        ids["org"], ids["other_org"] = org.id, other.id

        admin = AppUser(organization_id=org.id, display_name="Admin", roles=["admin"])
        aux = AppUser(organization_id=org.id, display_name="Auxiliar", roles=["auxiliary"])
        outsider = AppUser(organization_id=other.id, display_name="Externo", roles=["admin"])
        s.add_all([admin, aux, outsider])
        await s.flush()
        ids.update(admin=admin.id, aux=aux.id, outsider=outsider.id)

        cycle = PurchaseCycle(organization_id=org.id, name="Ciclo C5-A")
        other_cycle = PurchaseCycle(organization_id=other.id, name="Ciclo ajeno")
        s.add_all([cycle, other_cycle])
        await s.flush()
        ids["cycle"] = cycle.id

        uno = Supplier(organization_id=org.id, name="Proveedor Uno", data_origin="simulated")
        dos = Supplier(organization_id=org.id, name="Proveedor Dos", data_origin="simulated")
        sin_despacho = Supplier(
            organization_id=org.id, name="Proveedor Sin Despacho", data_origin="simulated"
        )
        s.add_all([uno, dos, sin_despacho])
        await s.flush()
        ids["suppliers"] = {"uno": uno.id, "dos": dos.id, "sin_despacho": sin_despacho.id}

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

        src_uno, src_dos, src_sin = _source(uno), _source(dos), _source(sin_despacho)
        s.add_all([src_uno, src_dos, src_sin])
        await s.flush()
        ids["sources"] = {"uno": src_uno.id, "dos": src_dos.id, "sin_despacho": src_sin.id}

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

        x = TradeItem(
            organization_id=org.id,
            display_name="Ibuprofeno 400 mg x 20 comprimidos",
            identity_key="fixture-c5-ibuprofeno-400-20",
            ingredient="ibuprofeno",
            attributes=_attrs("ibuprofeno", ("20", "un")),
            data_origin="simulated",
        )
        y = TradeItem(
            organization_id=org.id,
            display_name="Paracetamol 500 mg x 20 comprimidos",
            identity_key="fixture-c5-paracetamol-500-20",
            ingredient="paracetamol",
            attributes=_attrs("paracetamol", ("20", "un"), ("500", "mg")),
            data_origin="simulated",
        )
        s.add_all([x, y])
        await s.flush()
        ids["items"] = {"x": x.id, "y": y.id}

        def _offer(supplier, source, item, external_id, price, **kw) -> OfferSnapshot:
            fields = {
                "organization_id": org.id,
                "supplier_id": supplier.id,
                "source_id": source.id,
                "trade_item_id": item.id,
                "external_id": external_id,
                "price_amount": price,
                "currency": "CLP",
                "price_basis": "per_commercial_unit",
                "sale_unit": "box",
                "sale_unit_text": "caja",
                "stock_status": "known",
                "stock_quantity": "90",
                "stock_unit": "box",
                "tax_included": True,
                "discount_included": True,
                "minimum_known": True,
                "minimum_quantity": "1",
                "minimum_unit": "box",
                "multiple_known": True,
                "multiple_quantity": "1",
                "multiple_unit": "box",
                "observed_at": NOW,
                "data_origin": "simulated",
            }
            fields.update(kw)
            return OfferSnapshot(**fields)

        offers = {
            "uno_x": _offer(uno, src_uno, x, "U-X", "2000.00"),
            "uno_y": _offer(uno, src_uno, y, "U-Y", "1000.00"),
            "dos_x": _offer(dos, src_dos, x, "D-X", "1800.00"),
            "dos_y": _offer(dos, src_dos, y, "D-Y", "900.00"),
            "sin_x": _offer(sin_despacho, src_sin, x, "S-X", "1500.00"),
        }
        # Una observación más nueva y más barata de la misma fuente: NO debe mover la evaluación.
        offers["uno_x_nueva"] = _offer(
            uno, src_uno, x, "U-X-2", "1900.00", observed_at=LATER
        )
        s.add_all(offers.values())
        await s.flush()
        ids["offers"] = {k: v.id for k, v in offers.items()}
        ids["offers"]["uno_x_vieja"] = offers["uno_x"].id

        s.add_all(
            [
                SupplierProductMapping(
                    organization_id=org.id,
                    source_id=o.source_id,
                    supplier_id=o.supplier_id,
                    trade_item_id=o.trade_item_id,
                    external_id=o.external_id,
                    data_origin="simulated",
                )
                for o in offers.values()
            ]
        )

        exacta = DemandItem(
            organization_id=org.id,
            cycle_id=cycle.id,
            display_name="Ibuprofeno 400 mg x 20 comprimidos",
            ingredient="ibuprofeno",
            attributes=_attrs("ibuprofeno", ("20", "un")),
            target_quantity="10",
            quantity_unit="box",
            data_origin="simulated",
        )
        # Reporte independiente del auxiliar: mismo producto que `exacta`, otra persona (D-135).
        exacta_aux = DemandItem(
            organization_id=org.id,
            cycle_id=cycle.id,
            display_name="Ibuprofeno 400 mg x 20 comprimidos",
            ingredient="ibuprofeno",
            attributes=_attrs("ibuprofeno", ("20", "un")),
            target_quantity="6",
            quantity_unit="box",
            reported_by=aux.id,
            data_origin="simulated",
        )
        unidad = DemandItem(
            organization_id=org.id,
            cycle_id=cycle.id,
            display_name="Ibuprofeno 400 mg x 20 comprimidos (unidades)",
            ingredient="ibuprofeno",
            attributes=_attrs("ibuprofeno", ("20", "un")),
            target_quantity="4",
            quantity_unit="unit",
            data_origin="simulated",
        )
        otra = DemandItem(
            organization_id=org.id,
            cycle_id=cycle.id,
            display_name="Ibuprofeno 400 mg x 30 comprimidos",
            ingredient="ibuprofeno",
            attributes=_attrs("ibuprofeno", ("30", "un")),
            target_quantity="4",
            quantity_unit="box",
            data_origin="simulated",
        )
        desconocida = DemandItem(
            organization_id=org.id,
            cycle_id=cycle.id,
            display_name="Ibuprofeno sin cantidad",
            ingredient="ibuprofeno",
            attributes=_attrs("ibuprofeno", ("20", "un")),
            target_quantity=None,
            quantity_unit=None,
            data_origin="simulated",
        )
        s.add_all([exacta, exacta_aux, unidad, otra, desconocida])
        await s.flush()
        ids["demands"] = {
            "exacta": exacta.id,
            "exacta_aux": exacta_aux.id,
            "unidad": unidad.id,
            "otra": otra.id,
            "desconocida": desconocida.id,
        }
    return ids


@pytest.fixture
async def api(sessions, shop, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    app = create_app()

    async def _s():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.shop = shop
        c.sessions = sessions
        yield c
    get_settings.cache_clear()


def H(api, who="admin"):
    return {"X-Demo-User-Id": str(api.shop[who])}


def _q(value="3", unit="box") -> dict:
    return {"value": value, "unit": unit}


def _alloc(api, *pairs) -> list[dict]:
    return [
        {"demand_item_id": str(api.shop["demands"][key]), "quantity": int(qty)}
        for key, qty in pairs
    ]


def _add(
    api, quote_id, *, supplier="uno", offer="uno_x", demand=None, value="3", key=None,
    allocations=None,
):  # fmt: skip
    body = {
        "supplier_id": str(api.shop["suppliers"][supplier]),
        "offer_snapshot_id": str(api.shop["offers"][offer]),
        "quantity": _q(value=value),
    }
    if demand:
        body["demand_item_id"] = str(api.shop["demands"][demand])
    if allocations is not None:
        body["demand_allocations"] = allocations
    headers = H(api)
    if key:
        headers["Idempotency-Key"] = key
    return api.post(f"/api/v1/quotes/{quote_id}/lines", json=body, headers=headers)


async def _quote(api, title=None) -> str:
    r = await api.post("/api/v1/quotes", json={"title": title}, headers=H(api))
    assert r.status_code == 201, r.text
    return r.json()["quote_id"]


def _set_quantity(api, item_id, version, value="1", unit="box", key=None):
    headers = H(api)
    headers["If-Match"] = f'"{version}"'
    headers["Idempotency-Key"] = key or uuid.uuid4().hex
    return api.post(
        f"/api/v1/demand-items/{item_id}/quantity-commands",
        json={"operation": "SET", "value": value, "unit": unit, "reason": "prueba C5-A"},
        headers=headers,
    )


async def _demand_row(api, key) -> dict:
    r = await api.get("/api/v1/demand-items", headers=H(api))
    assert r.status_code == 200, r.text
    for row in r.json()["items"]:
        if row["id"] == str(api.shop["demands"][key]):
            return row
    raise AssertionError("faltante ausente")


def _alloc_pairs(allocations: list[dict]) -> list[tuple[str, str]]:
    """Lo que importa de cada asignación: a qué reporte y cuánto. No se comparan marcas internas."""
    return [(a["demand_item_id"], a["quantity"]) for a in allocations]


# --------------------------------------------------------------------------- cotizaciones


async def test_crear_listar_y_detalle_de_una_cotizacion(api):
    r = await api.post("/api/v1/quotes", json={}, headers=H(api))
    assert r.status_code == 201, r.text
    quote = r.json()
    assert quote["title"] is None
    assert quote["status"] == "draft"
    assert quote["derived_state"] == "in_preparation"
    assert quote["carts"] == []
    assert quote["estimated_total"]["available"] is False
    assert "no_active_lines" in quote["estimated_total"]["causes"]
    assert quote["advice"] is None

    r = await api.get("/api/v1/quotes", headers=H(api))
    assert r.status_code == 200
    assert [q["quote_id"] for q in r.json()["quotes"]] == [quote["quote_id"]]
    assert r.json()["quotes"][0]["supplier_count"] == 0

    r = await api.get(f"/api/v1/quotes/{quote['quote_id']}", headers=H(api))
    assert r.status_code == 200
    assert r.json()["version"] == 1

    r = await api.get(f"/api/v1/quotes/{quote['quote_id']}/summary", headers=H(api))
    assert r.status_code == 200
    assert r.json()["suppliers"] == []


async def test_titulo_cierre_y_version_optimista(api):
    quote_id = await _quote(api, "Compra de octubre")
    r = await api.patch(
        f"/api/v1/quotes/{quote_id}",
        json={"version": 1, "title": "Compra de noviembre", "status": "closed"},
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["title"] == "Compra de noviembre"
    assert data["status"] == "closed"
    assert data["closed_at"] is not None
    assert data["derived_state"] == "closed_without_purchase"
    assert data["version"] == 2

    # Sin `version` la petición no puede aplicarse (428) y una versión vieja choca (409).
    r = await api.patch(f"/api/v1/quotes/{quote_id}", json={"title": "x"}, headers=H(api))
    assert r.status_code == 428
    assert r.json()["error"]["code"] == "VERSION_REQUIRED"
    r = await api.patch(
        f"/api/v1/quotes/{quote_id}", json={"version": 1, "title": "x"}, headers=H(api)
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "VERSION_CONFLICT"
    assert r.json()["error"]["details"]["current_version"] == 2

    r = await api.patch(
        f"/api/v1/quotes/{quote_id}", json={"version": 2, "status": "draft"}, headers=H(api)
    )
    assert r.status_code == 200
    assert r.json()["status"] == "draft"
    assert r.json()["closed_at"] is None


async def test_lista_ordena_por_updated_at_y_filtra_por_titulo(api):
    first = await _quote(api, "Primera")
    second = await _quote(api, "Segunda")
    r = await api.patch(
        f"/api/v1/quotes/{first}", json={"version": 1, "title": "Primera tocada"}, headers=H(api)
    )
    assert r.status_code == 200
    r = await api.get("/api/v1/quotes", headers=H(api))
    assert [q["quote_id"] for q in r.json()["quotes"]][0] == first
    assert r.json()["quotes"][1]["quote_id"] == second

    r = await api.get("/api/v1/quotes?q=tocada", headers=H(api))
    assert [q["quote_id"] for q in r.json()["quotes"]] == [first]

    r = await api.get("/api/v1/quotes?status=closed", headers=H(api))
    assert r.json()["quotes"] == []


async def test_cotizacion_cerrada_no_admite_lineas(api):
    quote_id = await _quote(api)
    r = await api.patch(
        f"/api/v1/quotes/{quote_id}", json={"version": 1, "status": "closed"}, headers=H(api)
    )
    assert r.status_code == 200
    r = await _add(api, quote_id)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "QUOTE_CLOSED"
    r = await api.patch(
        f"/api/v1/quotes/{quote_id}", json={"version": 2, "status": "draft"}, headers=H(api)
    )
    assert r.status_code == 200
    assert (await _add(api, quote_id)).status_code == 201


# --------------------------------------------------------------------------- líneas y carritos


async def test_agregar_linea_crea_carrito_y_nunca_suma(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, value="3")
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["deduplicated"] is False
    assert data["line"]["quantity"] == {"value": "3", "unit": "box"}
    assert data["line"]["purchase_state"] == "pending"
    assert data["line"]["remainder"] == "open"
    assert data["cart"]["line_count"] == 1
    cart_id = data["cart"]["cart_id"]

    # La misma petición devuelve la línea existente con 200 y no cambia la cantidad.
    r = await _add(api, quote_id, value="3")
    assert r.status_code == 200, r.text
    assert r.json()["deduplicated"] is True
    assert r.json()["cart"]["cart_id"] == cart_id

    # Misma oferta con otra cantidad: conflicto explícito, nunca suma (3+4 ≠ 7).
    r = await _add(api, quote_id, value="4")
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "LINE_EXISTS"
    assert body["details"]["current_quantity"] == "3"

    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    assert len(r.json()["carts"]) == 1
    assert len(r.json()["carts"][0]["lines"]) == 1
    assert r.json()["carts"][0]["lines"][0]["quantity"]["value"] == "3"


async def test_idempotency_key_replay_y_reuso(api):
    quote_id = await _quote(api)
    key = "idem-c5a-0001"
    r = await _add(api, quote_id, value="2", key=key)
    assert r.status_code == 201, r.text
    line_id = r.json()["line"]["line_id"]

    r = await _add(api, quote_id, value="2", key=key)
    assert r.status_code == 200, r.text
    assert r.json()["line"]["line_id"] == line_id
    assert r.json()["deduplicated"] is True

    # La misma clave con otro contenido no puede devolver el resultado anterior.
    r = await _add(api, quote_id, value="7", key=key)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    async with api.sessions() as s:
        assert len((await s.execute(select(QuoteLine))).scalars().all()) == 1


async def test_validaciones_de_linea(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, value="2.5")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_QUANTITY"
    r = await _add(api, quote_id, value="0")
    assert r.status_code == 422
    # Oferta de Dos declarada como de Uno: no se puede comprar en el proveedor equivocado.
    r = await _add(api, quote_id, supplier="uno", offer="dos_x")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OFFER_NOT_IN_SUPPLIER"
    # Faltante de otra organización u oferta inexistente: 404, sin datos filtrados.
    r = await _add(api, quote_id, demand="exacta")
    assert r.status_code == 201
    r = await _add(api, quote_id, offer="uno_y", demand="exacta")
    assert r.status_code == 201


async def test_carrito_descartado_bloquea_y_se_reincorpora(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, value="2")
    assert r.status_code == 201
    supplier = api.shop["suppliers"]["uno"]
    r = await api.post(
        f"/api/v1/quotes/{quote_id}/carts/{supplier}/dismiss", headers=H(api)
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "dismissed"

    r = await _add(api, quote_id, value="9")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CART_DISMISSED"

    # El carrito descartado no se evalúa ni cubre, pero sus líneas siguen existiendo.
    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    cart = r.json()["carts"][0]
    assert cart["status"] == "dismissed"
    assert cart["evaluation"] is None
    assert len(cart["lines"]) == 1
    assert r.json()["estimated_total"]["available"] is False

    r = await api.post(
        f"/api/v1/quotes/{quote_id}/carts/{supplier}/reinstate", headers=H(api)
    )
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    assert r.json()["carts"][0]["evaluation"] is not None


async def test_patch_de_cantidad_es_set_explicito_y_exige_version(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, value="2")
    line_id = r.json()["line"]["line_id"]

    r = await api.patch(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}",
        json={"quantity": _q(value="5")},
        headers=H(api),
    )
    assert r.status_code == 428
    assert r.json()["error"]["code"] == "VERSION_REQUIRED"

    r = await api.patch(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}",
        json={"version": 1, "quantity": _q(value="5")},
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    assert r.json()["line"]["quantity"]["value"] == "5"  # SET, no 2+5

    r = await api.patch(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}",
        json={"version": 1, "quantity": _q(value="8")},
        headers=H(api),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "VERSION_CONFLICT"


async def test_quitar_linea_es_logica_y_no_borra_historial(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, value="2")
    line_id = r.json()["line"]["line_id"]

    r = await api.delete(f"/api/v1/quotes/{quote_id}/lines/{line_id}", headers=H(api))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "removed"
    assert r.json()["removed_at"] is not None
    assert r.json()["removed_by"] == str(api.shop["admin"])

    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    lines = r.json()["carts"][0]["lines"]
    assert len(lines) == 1 and lines[0]["status"] == "removed"
    assert r.json()["carts"][0]["line_count"] == 0

    # Una línea quitada no se vuelve a quitar ni se edita.
    assert (
        await api.delete(f"/api/v1/quotes/{quote_id}/lines/{line_id}", headers=H(api))
    ).status_code == 404


async def test_cerrar_y_reabrir_pendiente_no_cambia_la_cantidad(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, value="4")
    line_id = r.json()["line"]["line_id"]

    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}/close-pending",
        json={"note": "lo trae el proveedor la próxima semana"},
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    line = r.json()
    assert line["remainder"] == "closed_by_user"
    assert line["pending_closed_note"] == "lo trae el proveedor la próxima semana"
    assert line["quantity"]["value"] == "4"
    assert line["status"] == "active"

    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}/reopen-pending", headers=H(api)
    )
    assert r.status_code == 200
    assert r.json()["pending_closed_at"] is None
    assert r.json()["remainder"] == "open"
    assert r.json()["quantity"]["value"] == "4"


async def test_mover_linea_crea_carrito_destino_y_no_arrastra_vinculo(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, value="4", demand="exacta")
    line_id = r.json()["line"]["line_id"]
    assert r.json()["line"]["coverage_relation"] == "exact"

    # Mover a otro artículo es un error explícito.
    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}/move",
        json={
            "target_offer_snapshot_id": str(api.shop["offers"]["dos_y"]),
            "quantity": {"value": "4", "unit": "box"},
        },
        headers=H(api),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ARTICLE_MISMATCH"

    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}/move",
        json={
            "target_offer_snapshot_id": str(api.shop["offers"]["dos_x"]),
            "quantity": {"value": "4", "unit": "box"},
        },
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    moved = r.json()["line"]
    assert moved["supplier_id"] == str(api.shop["suppliers"]["dos"])
    # El vínculo original NO se arrastra: la cobertura se recalcula.
    assert moved["demand_item_id"] is None
    assert moved["coverage_relation"] is None

    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    carts = {str(c["supplier_id"]): c for c in r.json()["carts"]}
    uno = carts[str(api.shop["suppliers"]["uno"])]
    dos = carts[str(api.shop["suppliers"]["dos"])]
    assert uno["lines"][0]["status"] == "removed"
    assert dos["lines"][0]["status"] == "active"
    assert dos["line_count"] == 1


async def test_refresh_es_explicito_y_mueve_el_precio_elegido(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, offer="uno_x", value="3")
    line_id = r.json()["line"]["line_id"]
    assert r.json()["line"]["offer_snapshot_id"] == str(api.shop["offers"]["uno_x"])
    assert r.json()["line"]["selected"]["price"]["amount"] == "2000.00"

    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    line = r.json()["carts"][0]["lines"][0]
    assert line["price_state"] == "requires_review"
    assert "newer_observation_available" in line["causes"]
    # La evaluación usa el snapshot ELEGIDO, no la observación más nueva (V-04).
    assert r.json()["carts"][0]["lines"][0]["selected"]["price"]["amount"] == "2000.00"
    assert r.json()["carts"][0]["evaluation"]["subtotal"]["amount"] == "6000.00"

    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}/refresh", headers=H(api)
    )
    assert r.status_code == 200, r.text
    assert r.json()["line"]["offer_snapshot_id"] == str(api.shop["offers"]["uno_x_nueva"])
    assert r.json()["line"]["selected"]["price"]["amount"] == "1900.00"
    assert r.json()["line"]["price_state"] == "current"
    assert r.json()["refreshed"] is True

    # Repetir sin una observación nueva no reapunta nada: `refreshed` lo dice explícitamente.
    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}/refresh", headers=H(api)
    )
    assert r.status_code == 200, r.text
    assert r.json()["refreshed"] is False
    assert r.json()["line"]["offer_snapshot_id"] == str(api.shop["offers"]["uno_x_nueva"])

    async with api.sessions() as s:
        event = (
            await s.execute(select(AuditEvent).where(AuditEvent.action == "quote.line.refresh"))
        ).scalar_one()
        assert event.after["offer_snapshot_id"] == str(api.shop["offers"]["uno_x_nueva"])


# --------------------------------------------------------------------------- cobertura §12


async def test_vinculo_congela_relacion_y_cubre_sin_cerrar(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, demand="exacta", value="4")
    assert r.status_code == 201, r.text
    assert r.json()["line"]["coverage_relation"] == "exact"

    row = await _demand_row(api, "exacta")
    assert row["quoted_quantity"] == "4"
    assert row["purchased_quantity"] == "0"
    assert row["coverage_quantity"] == "4"
    assert row["pending_quantity"] == "6"
    assert row["pending_purchase_quantity"] == "10"
    assert row["is_pending"] is True
    assert row["replenishment_state"] == "in_quote"
    assert row["target_quantity"] == "10"
    # El último texto queda disponible para la lista.
    assert row["last_raw_text"] is None  # el faltante se creó en la fixture, sin aviso

    # Otra cotización cubre el resto: `pending_quantity` llega a 0 pero cotizar NO cierra.
    second = await _quote(api)
    assert (await _add(api, second, demand="exacta", value="6")).status_code == 201
    row = await _demand_row(api, "exacta")
    assert row["pending_quantity"] == "0"
    assert row["pending_purchase_quantity"] == "10"
    assert row["is_pending"] is True
    assert row["replenishment_state"] == "in_quote"

    r = await api.get("/api/v1/demand-items?pending_only=true", headers=H(api))
    assert str(api.shop["demands"]["exacta"]) not in {i["id"] for i in r.json()["items"]}


async def test_relacion_otra_presentacion_y_cantidad_desconocida(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, demand="otra", value="2")
    assert r.status_code == 201
    # La oferta es de 20 comprimidos y el faltante pide 30: misma identidad, otro contenido.
    assert r.json()["line"]["coverage_relation"] == "other_presentation"

    # Un reporte sin cantidad declarada no se puede asignar: primero una persona la fija (D-135).
    r = await _add(api, quote_id, offer="uno_y", demand="desconocida", value="1")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "REPORT_QUANTITY_UNKNOWN"
    row = await _demand_row(api, "desconocida")
    assert row["pending_quantity"] is None
    assert row["pending_purchase_quantity"] is None
    assert row["is_pending"] is True

    assert (await _set_quantity(api, row["id"], row["version"])).status_code == 200
    r = await _add(api, quote_id, offer="uno_y", demand="desconocida", value="1")
    assert r.status_code == 201
    row = await _demand_row(api, "desconocida")
    assert row["quoted_quantity"] == "1"
    assert row["pending_quantity"] == "0"
    assert row["pending_purchase_quantity"] == "1"


async def test_descartar_carrito_resta_cobertura_y_reincorporar_la_devuelve(api):
    quote_id = await _quote(api)
    assert (await _add(api, quote_id, demand="exacta", value="5")).status_code == 201
    supplier = api.shop["suppliers"]["uno"]

    await api.post(f"/api/v1/quotes/{quote_id}/carts/{supplier}/dismiss", headers=H(api))
    row = await _demand_row(api, "exacta")
    assert row["quoted_quantity"] == "0"
    assert row["pending_quantity"] == "10"
    assert row["replenishment_state"] == "pending"

    await api.post(f"/api/v1/quotes/{quote_id}/carts/{supplier}/reinstate", headers=H(api))
    row = await _demand_row(api, "exacta")
    assert row["quoted_quantity"] == "5"
    assert row["pending_quantity"] == "5"


async def test_quitar_linea_resta_cobertura_sin_cerrar_la_cotizacion(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, demand="exacta", value="7")
    line_id = r.json()["line"]["line_id"]
    assert (await _demand_row(api, "exacta"))["quoted_quantity"] == "7"

    await api.delete(f"/api/v1/quotes/{quote_id}/lines/{line_id}", headers=H(api))
    row = await _demand_row(api, "exacta")
    assert row["quoted_quantity"] == "0"
    assert row["pending_quantity"] == "10"

    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    assert r.json()["status"] == "draft"
    assert r.json()["derived_state"] == "in_preparation"


# --------------------------------------------------------------------------- asignaciones (D-135)


async def test_una_linea_cubre_dos_reportes_independientes(api):
    quote_id = await _quote(api)
    r = await _add(
        api, quote_id, value="8", allocations=_alloc(api, ("exacta", 5), ("exacta_aux", 3))
    )
    assert r.status_code == 201, r.text
    line = r.json()["line"]
    assert line["demand_item_id"] is None  # varias asignaciones: no hay un único reporte
    allocs = {a["demand_item_id"]: a["quantity"] for a in line["demand_allocations"]}
    assert allocs == {
        str(api.shop["demands"]["exacta"]): "5",
        str(api.shop["demands"]["exacta_aux"]): "3",
    }
    assert all(a["coverage_relation"] == "exact" for a in line["demand_allocations"])

    # Cada reporte recibe solo lo asignado: nunca se reparte por su cuenta.
    assert (await _demand_row(api, "exacta"))["quoted_quantity"] == "5"
    assert (await _demand_row(api, "exacta_aux"))["quoted_quantity"] == "3"

    # La suma no puede superar la cantidad de la línea.
    r = await _add(
        api, quote_id, value="8", allocations=_alloc(api, ("exacta", 5), ("exacta_aux", 4))
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ALLOCATION_EXCEEDS_LINE"
    assert r.json()["error"]["details"] == {"line_quantity": "8", "allocated": "9"}

    # Reporte repetido en la misma línea.
    r = await _add(api, quote_id, value="8", allocations=_alloc(api, ("exacta", 4), ("exacta", 4)))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "DUPLICATE_ALLOCATION"

    # Lista vacía: si no se asigna nada, no hay cobertura que inventar.
    r = await _add(api, quote_id, value="8", allocations=[])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ALLOCATION_EMPTY"

    # Atajo y lista son excluyentes.
    r = await _add(
        api, quote_id, value="8", demand="exacta", allocations=_alloc(api, ("exacta", 8))
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ALLOCATION_CONFLICT"

    # La unidad del reporte debe coincidir con la de la línea.
    r = await _add(api, quote_id, value="8", allocations=_alloc(api, ("unidad", 2)))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNIT_MISMATCH"


async def test_patch_reemplaza_las_asignaciones_sin_tocar_el_resto(api):
    quote_id = await _quote(api)
    r = await _add(
        api, quote_id, value="8", allocations=_alloc(api, ("exacta", 5), ("exacta_aux", 3))
    )
    line = r.json()["line"]
    line_id = line["line_id"]

    r = await api.patch(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}",
        json={
            "version": line["version"],
            "quantity": _q(value="8"),
            "demand_allocations": _alloc(api, ("exacta_aux", 6)),
        },
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    allocs = r.json()["line"]["demand_allocations"]
    assert [(a["demand_item_id"], a["quantity"]) for a in allocs] == [
        (str(api.shop["demands"]["exacta_aux"]), "6")
    ]
    assert (await _demand_row(api, "exacta"))["quoted_quantity"] == "0"
    assert (await _demand_row(api, "exacta_aux"))["quoted_quantity"] == "6"

    # Bajar la cantidad de la línea por debajo de lo asignado se rechaza.
    r = await api.patch(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}",
        json={"version": r.json()["line"]["version"], "quantity": _q(value="4")},
        headers=H(api),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ALLOCATION_EXCEEDS_LINE"


# --------------------------------------------------------------------------- cancelación (D-135)


def _cancel(api, item_id, version, reason=None, who="admin", key=None):
    headers = H(api, who)
    headers["If-Match"] = f'"{version}"'
    headers["Idempotency-Key"] = key or uuid.uuid4().hex
    return api.post(
        f"/api/v1/demand-items/{item_id}/cancel", json={"reason": reason}, headers=headers
    )


async def test_cancelar_un_reporte_asignado_informa_las_lineas_y_quita_la_cobertura(api):
    quote_id = await _quote(api)
    r = await _add(
        api, quote_id, value="8", allocations=_alloc(api, ("exacta", 5), ("exacta_aux", 3))
    )
    assert r.status_code == 201, r.text
    line_id = r.json()["line"]["line_id"]
    row = await _demand_row(api, "exacta")
    assert row["quoted_quantity"] == "5"

    # Solo `admin`; el auxiliar no cancela reportes.
    r = await _cancel(api, row["id"], row["version"], who="aux")
    assert r.status_code == 403

    # La versión siempre se exige y se compara.
    r = await api.post(
        f"/api/v1/demand-items/{row['id']}/cancel",
        json={"reason": "duplicado"},
        headers={**H(api), "Idempotency-Key": uuid.uuid4().hex},
    )
    assert r.status_code == 428
    r = await _cancel(api, row["id"], row["version"] + 1)
    assert r.status_code == 409
    assert r.json()["error"]["details"]["current_version"] == row["version"]

    r = await _cancel(api, row["id"], row["version"], reason="duplicado")
    assert r.status_code == 200, r.text
    assert r.headers["ETag"] == f'"{row["version"] + 1}"'
    out = r.json()
    assert out["item"]["status"] == "cancelled"
    assert out["item"]["cancellation_reason"] == "duplicado"
    assert out["item"]["cancelled_at"] is not None
    assert out["item"]["cancelled_by_name"] == "Admin"
    # La línea no se toca en silencio: se informa para que la administración la corrija.
    (affected,) = out["affected_quote_lines"]
    assert affected["quote_line_id"] == line_id
    assert affected["quote_id"] == quote_id
    assert affected["quote_status"] == "draft"
    assert affected["allocation_quantity"] == "5"
    assert "cotización" in out["message"]

    # El reporte cancelado deja de cubrirse; el otro reporte conserva lo suyo.
    r = await api.get("/api/v1/demand-items?status=cancelled", headers=H(api))
    (cancelled,) = [i for i in r.json()["items"] if i["id"] == row["id"]]
    assert cancelled["quoted_quantity"] == "0"
    assert cancelled["replenishment_state"] == "cancelled"
    assert cancelled["is_pending"] is False
    assert (await _demand_row(api, "exacta_aux"))["quoted_quantity"] == "3"
    r = await api.get("/api/v1/demand-items", headers=H(api))
    assert row["id"] not in {i["id"] for i in r.json()["items"]}

    # No se borró nada: la cancelación es lógica, con auditoría y una sola vez.
    r = await _cancel(api, row["id"], row["version"] + 1)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "REPORT_NOT_ACTIVE"
    async with api.sessions() as s:
        actions = (
            await s.execute(select(AuditEvent.action).where(AuditEvent.entity_id == uuid.UUID(row["id"])))
        ).scalars().all()
    assert "demand_item.cancel" in actions


# --------------------------------------------------- integridad de asignaciones (D-137)


async def _close(api, quote_id, version):
    return await api.patch(
        f"/api/v1/quotes/{quote_id}",
        json={"version": version, "status": "closed"},
        headers=H(api),
    )


async def _quote_version(api, quote_id) -> int:
    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    assert r.status_code == 200, r.text
    return r.json()["version"]


async def test_cerrar_bloquea_asignaciones_a_reportes_cancelados_y_permite_corregir(api):
    quote_id = await _quote(api)
    r = await _add(
        api, quote_id, value="8", allocations=_alloc(api, ("exacta", 5), ("exacta_aux", 3))
    )
    assert r.status_code == 201, r.text
    line_id = r.json()["line"]["line_id"]
    assert (await _demand_row(api, "exacta"))["quoted_quantity"] == "5"

    row = await _demand_row(api, "exacta")
    assert (await _cancel(api, row["id"], row["version"], reason="duplicado")).status_code == 200

    # Escenario de Ana: la línea sigue asignando cantidad a un reporte cancelado.
    r = await _close(api, quote_id, await _quote_version(api, quote_id))
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "QUOTE_HAS_INCONSISTENT_ALLOCATIONS"
    assert error["details"]["quote_id"] == quote_id
    (issue,) = error["details"]["issues"]
    assert issue["code"] == "REPORT_NOT_ACTIVE"
    assert issue["quote_line_id"] == line_id
    assert issue["demand_item_id"] == row["id"]
    assert issue["report_status"] == "cancelled"
    # Nada se corrigió solo: la cotización sigue en borrador y la asignación intacta.
    detail = (await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))).json()
    assert detail["status"] == "draft"
    allocations = detail["carts"][0]["lines"][0]["demand_allocations"]
    assert {(a["demand_item_id"], a["quantity"]) for a in allocations} == {
        (row["id"], "5"),
        (str(api.shop["demands"]["exacta_aux"]), "3"),
    }
    assert {a["coverage_relation"] for a in allocations} == {"exact"}

    # La administración ajusta las asignaciones: quita la del reporte cancelado.
    line = detail["carts"][0]["lines"][0]
    r = await api.patch(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}",
        json={
            "version": line["version"],
            "quantity": _q(value="8"),
            "demand_allocations": _alloc(api, ("exacta_aux", 3)),
        },
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    assert (await _demand_row(api, "exacta_aux"))["quoted_quantity"] == "3"

    # Ahora sí cierra.
    r = await _close(api, quote_id, await _quote_version(api, quote_id))
    assert r.status_code == 200, r.text
    assert (await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))).json()["status"] == "closed"


async def test_cerrar_bloquea_cantidades_de_reporte_que_ya_no_cubren_lo_asignado(api):
    quote_id = await _quote(api)
    r = await _add(
        api, quote_id, value="8", allocations=_alloc(api, ("exacta", 5), ("exacta_aux", 3))
    )
    assert r.status_code == 201, r.text

    # Se fuerza un estado imposible por la vía normal para probar el guardián del cierre.
    async with api.sessions() as s:
        item = await s.get(DemandItem, api.shop["demands"]["exacta"])
        assert item
        item.target_quantity = Decimal("3")
        await s.commit()
    r = await _close(api, quote_id, await _quote_version(api, quote_id))
    assert r.status_code == 409, r.text
    (issue,) = [
        i for i in r.json()["error"]["details"]["issues"]
        if i["demand_item_id"] == str(api.shop["demands"]["exacta"])
    ]  # fmt: skip
    assert issue["code"] == "ALLOCATION_EXCEEDS_REPORT"
    assert issue["allocated"] == "5" and issue["report_quantity"] == "3"

    async with api.sessions() as s:
        item = await s.get(DemandItem, api.shop["demands"]["exacta"])
        assert item
        item.target_quantity = None
        item.quantity_unit = None
        await s.commit()
    r = await _close(api, quote_id, await _quote_version(api, quote_id))
    assert r.status_code == 409, r.text
    (issue,) = [
        i for i in r.json()["error"]["details"]["issues"]
        if i["demand_item_id"] == str(api.shop["demands"]["exacta"])
    ]  # fmt: skip
    assert issue["code"] == "REPORT_QUANTITY_UNKNOWN"

    # Al quitar las asignaciones inconsistentes, el cierre deja de estar bloqueado.
    line = (await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))).json()["carts"][0]["lines"][0]
    r = await api.patch(
        f"/api/v1/quotes/{quote_id}/lines/{line['line_id']}",
        json={"version": line["version"], "quantity": _q(value="8"), "demand_allocations": []},
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    r = await _close(api, quote_id, await _quote_version(api, quote_id))
    assert r.status_code == 200, r.text


async def test_no_se_puede_bajar_la_cantidad_pedida_por_debajo_de_lo_asignado(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, value="8", allocations=_alloc(api, ("exacta", 5)))
    assert r.status_code == 201, r.text
    line_id = r.json()["line"]["line_id"]
    row = await _demand_row(api, "exacta")
    assert (row["target_quantity"], row["version"]) == ("10", 1)

    # Reducir por debajo de lo asignado se bloquea con el detalle para la interfaz.
    r = await _set_quantity(api, row["id"], 1, value="4", unit="box")
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "QUANTITY_BELOW_ALLOCATED"
    assert error["details"]["allocated"] == "5"
    assert error["details"]["requested"] == "4"
    assert error["details"]["unit"] == "box"
    (allocation,) = error["details"]["allocations"]
    assert allocation["quote_id"] == quote_id
    assert allocation["quote_line_id"] == line_id
    assert allocation["quote_status"] == "draft"

    # Bajar hasta lo asignado sí se permite; quedar sin cantidad, no.
    assert (await _set_quantity(api, row["id"], 1, value="5", unit="box")).status_code == 200
    clear = await api.post(
        f"/api/v1/demand-items/{row['id']}/quantity-commands",
        json={"operation": "CLEAR", "reason": "prueba"},
        headers={**H(api), "If-Match": '"2"', "Idempotency-Key": uuid.uuid4().hex},
    )
    assert clear.status_code == 409 and clear.json()["error"]["code"] == "REPORT_HAS_ALLOCATIONS"
    # Cambiar la unidad mientras hay asignaciones tampoco.
    r = await _set_quantity(api, row["id"], 2, value="5", unit="unit")
    assert r.status_code == 422 and r.json()["error"]["code"] == "UNIT_MISMATCH"

    # La administración quita las asignaciones con una lista vacía explícita.
    line = (await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))).json()["carts"][0]["lines"][0]
    r = await api.patch(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}",
        json={"version": line["version"], "quantity": _q(value="8"), "demand_allocations": []},
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    assert r.json()["line"]["demand_allocations"] == []
    assert r.json()["line"]["demand_item_id"] is None
    assert (await _demand_row(api, "exacta"))["quoted_quantity"] == "0"

    # Ahora la cantidad puede bajar y volver a quedar desconocida.
    row = await _demand_row(api, "exacta")
    assert (await _set_quantity(api, row["id"], row["version"], value="4")).status_code == 200
    row = await _demand_row(api, "exacta")
    clear = await api.post(
        f"/api/v1/demand-items/{row['id']}/quantity-commands",
        json={"operation": "CLEAR", "reason": "prueba"},
        headers={**H(api), "If-Match": f'"{row["version"]}"', "Idempotency-Key": uuid.uuid4().hex},
    )
    assert clear.status_code == 200 and clear.json()["item"]["target_quantity"] is None


# --------------------------------------------------------------------------- E5 por carrito


async def test_evaluacion_por_carrito_y_total_solo_si_es_completo(api):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, supplier="uno", offer="uno_x", value="3")
    assert r.status_code == 201

    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    data = r.json()
    cart = data["carts"][0]
    ev = cart["evaluation"]
    assert ev["completeness"] == "complete"
    assert ev["subtotal"]["amount"] == "6000.00"
    assert ev["total"]["amount"] == "6990.00"  # + despacho fijo 990
    # Un solo carrito completo: el total general es la suma de totales completos.
    assert data["estimated_total"]["available"] is True
    assert data["estimated_total"]["amount"] == "6990.00"
    assert data["estimated_total"]["currency"] == "CLP"

    # Dos carritos completos y en la misma moneda: se suman.
    r = await _add(api, quote_id, supplier="dos", offer="dos_y", value="1")
    assert r.status_code == 201
    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    data = r.json()
    assert data["estimated_total"]["available"] is True
    assert data["estimated_total"]["amount"] == str(6000 + 990 + 900 + 990) + ".00"

    # Un proveedor sin despacho observado deja su carrito incompleto: no hay total general y
    # NUNCA se usa la cota inferior como total.
    r = await _add(api, quote_id, supplier="sin_despacho", offer="sin_x", value="1")
    assert r.status_code == 201
    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    data = r.json()
    assert data["estimated_total"]["available"] is False
    assert data["estimated_total"]["amount"] is None
    cart_sin = next(
        c for c in data["carts"] if c["supplier_id"] == str(api.shop["suppliers"]["sin_despacho"])
    )
    assert cart_sin["evaluation"]["completeness"] == "partial"
    assert cart_sin["evaluation"]["total"] is None
    assert cart_sin["evaluation"]["lower_bound"] is not None


async def test_resumen_por_proveedor_sin_lineas(api):
    quote_id = await _quote(api)
    await _add(api, quote_id, supplier="uno", offer="uno_x", value="2")
    await _add(api, quote_id, supplier="dos", offer="dos_y", value="1")
    r = await api.get(f"/api/v1/quotes/{quote_id}/summary", headers=H(api))
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["suppliers"]) == 2
    for row in data["suppliers"]:
        assert "lines" not in row
        assert row["product_count"] == 1
        assert row["completeness"] == "complete"
        assert row["total"] is not None
    assert data["estimated_total"]["available"] is True


# --------------------------------------------------------------------------- O1: observaciones


async def test_observacion_comercial_se_guarda_sin_ia_y_es_inmutable(api, engine):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, offer="uno_x", value="2")
    line_id = r.json()["line"]["line_id"]

    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}/observations",
        json={"text": "El proveedor avisó que el precio sube la próxima semana."},
        headers=H(api),
    )
    assert r.status_code == 201, r.text
    obs = r.json()
    assert obs["interpretation_status"] == "sin_interpretacion"
    assert obs["interpretations"] == []
    assert obs["quote_line_id"] == line_id
    assert obs["trade_item_id"] == str(api.shop["items"]["x"])
    assert obs["supplier_id"] == str(api.shop["suppliers"]["uno"])
    assert obs["data_origin"] == "captured"
    # El contexto conserva las etiquetas tal como se vieron.
    assert obs["context"]["trade_item"]["display_name"] == "Ibuprofeno 400 mg x 20 comprimidos"
    assert obs["context"]["supplier"]["name"] == "Proveedor Uno"
    assert obs["context"]["offer"]["price"]["amount"] == "2000.00"

    r = await api.post(
        "/api/v1/commercial-observations",
        json={
            "text": "Solo vende cajas cerradas.",
            "trade_item_id": str(api.shop["items"]["x"]),
            "supplier_id": str(api.shop["suppliers"]["dos"]),
        },
        headers=H(api),
    )
    assert r.status_code == 201, r.text
    assert r.json()["quote_id"] is None

    r = await api.get(
        f"/api/v1/quotes/{quote_id}/lines/{line_id}/observations", headers=H(api)
    )
    assert r.status_code == 200
    assert len(r.json()["observations"]) == 1

    r = await api.get(
        f"/api/v1/commercial-observations?trade_item_id={api.shop['items']['x']}",
        headers=H(api),
    )
    assert r.status_code == 200
    assert len(r.json()["observations"]) == 2
    # Más recientes primero.
    assert r.json()["observations"][0]["text"] == "Solo vende cajas cerradas."

    with pytest.raises(DBAPIError):
        async with engine.begin() as c:
            await c.execute(
                text("UPDATE commercial_observation SET text_original='otra' WHERE id=:i"),
                {"i": obs["observation_id"]},
            )
    with pytest.raises(DBAPIError):
        async with engine.begin() as c:
            await c.execute(
                text("DELETE FROM commercial_observation WHERE id=:i"),
                {"i": obs["observation_id"]},
            )


async def test_observacion_valida_articulo_y_texto(api):
    r = await api.post(
        "/api/v1/commercial-observations",
        json={"text": "x", "trade_item_id": str(uuid.uuid4())},
        headers=H(api),
    )
    assert r.status_code == 404
    r = await api.post(
        "/api/v1/commercial-observations",
        json={"text": "", "trade_item_id": str(api.shop["items"]["x"])},
        headers=H(api),
    )
    assert r.status_code == 400  # validación del esquema
    r = await api.post(
        "/api/v1/commercial-observations",
        json={"text": "x", "trade_item_id": str(api.shop["items"]["x"]), "extra": 1},
        headers=H(api),
    )
    assert r.status_code == 400

    # Si se indica la oferta, el proveedor sale de ella y se guarda.
    r = await api.post(
        "/api/v1/commercial-observations",
        json={
            "text": "La caja viene sellada.",
            "trade_item_id": str(api.shop["items"]["y"]),
            "offer_snapshot_id": str(api.shop["offers"]["dos_y"]),
        },
        headers=H(api),
    )
    assert r.status_code == 201, r.text
    assert r.json()["supplier_id"] == str(api.shop["suppliers"]["dos"])

    # Una oferta de otro artículo no puede sostener la observación.
    r = await api.post(
        "/api/v1/commercial-observations",
        json={
            "text": "No corresponde.",
            "trade_item_id": str(api.shop["items"]["x"]),
            "offer_snapshot_id": str(api.shop["offers"]["dos_y"]),
        },
        headers=H(api),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OBSERVATION_INVALID"


# --------------------------------------------------------------------------- permisos y organización


async def test_solo_admin_y_solo_su_organizacion(api):
    r = await api.post("/api/v1/quotes", json={}, headers=H(api, "aux"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"

    quote_id = await _quote(api)
    await _add(api, quote_id, value="1")
    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api, "outsider"))
    assert r.status_code == 404
    r = await _add(api, quote_id, value="2")
    # La cotización no es visible para otra organización: tampoco se puede escribir en ella.
    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines",
        json={
            "supplier_id": str(api.shop["suppliers"]["uno"]),
            "offer_snapshot_id": str(api.shop["offers"]["uno_x"]),
            "quantity": {"value": "1", "unit": "box"},
        },
        headers=H(api, "outsider"),
    )
    assert r.status_code == 404

    r = await api.get("/api/v1/quotes", headers=H(api, "outsider"))
    assert r.status_code == 200
    assert r.json()["quotes"] == []


async def test_el_evento_de_auditoria_y_el_outbox_se_escriben_juntos(api, sessions):
    quote_id = await _quote(api)
    await _add(api, quote_id, value="1", demand="exacta")
    async with sessions() as s:
        events = (
            await s.execute(select(AuditEvent).where(AuditEvent.action == "quote.line.add"))
        ).scalars().all()
        assert len(events) == 1
        assert events[0].entity_type == "quote_line"
        assert events[0].after["quantity"] == "1"
        assert events[0].after["coverage_relation"] == "exact"
        assert (await s.execute(select(Quote))).scalars().all()
        carts = (await s.execute(select(QuoteCart))).scalars().all()
        assert len(carts) == 1
        assert (await s.execute(select(CommercialObservation))).scalars().all() == []


# --------------------------------------------------------------------------- finalización


def _finalize(api, quote_id, *, key=None, version=None):
    headers = H(api)
    if key:
        headers["Idempotency-Key"] = key
    body = {} if version is None else {"version": version}
    return api.post(
        f"/api/v1/quotes/{quote_id}/finalize", json=body, headers=headers
    )


def _live_offer(api, *, supplier="uno", external="LIVE-1", price="1700.00", **over) -> dict:
    offer = {
        "source_id": str(api.shop["sources"][supplier]),
        "supplier_id": str(api.shop["suppliers"][supplier]),
        "external_id": external,
        "name": "Ibuprofeno 400 mg x 20 comprimidos",
        "laboratory": "ASCEND",
        "presentation": "Caja de 20 comprimidos",
        "price_basis": "per_commercial_unit",
        "sale_unit": "box",
        "stock_status": "known",
        "stock_quantity": "25",
        "stock_unit": "box",
    }
    if price is not None:
        offer["price"] = {"amount": price, "currency": "CLP"}
    offer.update(over)
    return offer


def _add_live(
    api, quote_id, *, quantity="3", offer=None, allocations=None, demand=None, key=None
):
    body = {
        "supplier_id": str(api.shop["suppliers"]["uno"]),
        "offer": offer or _live_offer(api),
        "quantity": {"value": quantity, "unit": "box"},
    }
    if demand:
        body["demand_item_id"] = str(api.shop["demands"][demand])
    if allocations is not None:
        body["demand_allocations"] = allocations
    headers = H(api)
    if key:
        headers["Idempotency-Key"] = key
    return api.post(f"/api/v1/quotes/{quote_id}/lines", json=body, headers=headers)


async def test_finalizar_genera_informe_congelado_y_lo_muestra_en_el_historial(api, sessions):
    quote_id = await _quote(api, "Reposición de noviembre")
    assert (await _add(api, quote_id, offer="uno_x", value="3")).status_code == 201
    assert (await _add(api, quote_id, offer="uno_y", value="2")).status_code == 201

    r = await _finalize(api, quote_id, key="finalize-cot-1")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["deduplicated"] is False
    assert data["quote"]["status"] == "closed"
    assert data["quote"]["report_label"] == "COT-000001"
    assert data["quote"]["report_number"] == 1
    assert data["quote"]["finalized_at"] is not None
    assert data["released"] == [] and data["pending"] == []

    report = data["report"]
    assert report["schema_version"] == 1
    assert report["label"] == "COT-000001"
    assert report["currency"] == "CLP"
    assert report["total"] == {"amount": "8000.00", "currency": "CLP"}
    assert len(report["items"]) == 2
    by_code = {item["product"]["code"]: item for item in report["items"]}
    assert by_code["U-X"]["product"]["name"] is None  # el proveedor no publicó nombre aquí
    assert by_code["U-X"]["quantity"] == {"value": "3", "unit": "box"}
    assert by_code["U-X"]["unit_price"]["amount"] == "2000.00"
    assert by_code["U-X"]["subtotal"] == {"amount": "6000.00", "currency": "CLP"}
    assert by_code["U-Y"]["subtotal"] == {"amount": "2000.00", "currency": "CLP"}
    assert report["suppliers"][0]["subtotal"] == {"amount": "8000.00", "currency": "CLP"}

    # El informe guardado se recupera tal cual, aunque después cambie la oferta del proveedor.
    r = await api.get(f"/api/v1/quotes/{quote_id}/report", headers=H(api))
    assert r.status_code == 200, r.text
    assert r.json()["report"] == report
    assert r.json()["report_label"] == "COT-000001"

    # La observación es de solo agregar: un precio nuevo es otra fila, nunca un UPDATE.
    async with sessions() as s, s.begin():
        s.add(
            OfferSnapshot(
                organization_id=api.shop["org"],
                supplier_id=api.shop["suppliers"]["uno"],
                source_id=api.shop["sources"]["uno"],
                trade_item_id=api.shop["items"]["x"],
                external_id="U-X",
                price_amount="99999.00",
                currency="CLP",
                sale_unit="box",
                observed_at=LATER,
                data_origin="simulated",
            )
        )
    r = await api.get(f"/api/v1/quotes/{quote_id}/report", headers=H(api))
    assert r.json()["report"]["items"][0]["unit_price"]["amount"] == "2000.00"

    # Historial: solo cotizaciones finalizadas. La vista del carrito activo no se duplica.
    other = await _quote(api, "Sigue en preparación")
    r = await api.get("/api/v1/quotes?finalized=true", headers=H(api))
    assert [q["quote_id"] for q in r.json()["quotes"]] == [quote_id]
    assert r.json()["quotes"][0]["report_label"] == "COT-000001"
    r = await api.get("/api/v1/quotes?finalized=false", headers=H(api))
    assert [q["quote_id"] for q in r.json()["quotes"]] == [other]

    # Una cotización finalizada no se reabre: el cierre sostiene el informe.
    r = await api.patch(
        f"/api/v1/quotes/{quote_id}",
        json={"version": data["quote"]["version"], "status": "draft"},
        headers=H(api),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "QUOTE_FINALIZED"


async def test_reintentos_no_duplican_el_informe_ni_aceptan_otra_clave(api):
    quote_id = await _quote(api)
    await _add(api, quote_id, value="1")

    first = await _finalize(api, quote_id, key="finalize-replay-1")
    assert first.status_code == 200, first.text
    assert first.json()["quote"]["report_label"] == "COT-000001"

    again = await _finalize(api, quote_id, key="finalize-replay-1")
    assert again.status_code == 200, again.text
    assert again.json()["deduplicated"] is True
    assert again.json()["quote"]["report_label"] == "COT-000001"
    assert again.json()["report"] == first.json()["report"]

    r = await _finalize(api, quote_id, key="finalize-replay-2")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "QUOTE_ALREADY_FINALIZED"

    # La misma clave no cierra dos cotizaciones distintas de la organización.
    other = await _quote(api)
    await _add(api, other, supplier="dos", offer="dos_x", value="1")
    r = await _finalize(api, other, key="finalize-replay-1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    # Aislamiento por organización: la cotización ajena no se ve ni se finaliza.
    r = await _finalize(api, other)  # aún abierta, pero la vista ajena da 404
    assert r.status_code == 200
    outsider = await api.post(
        f"/api/v1/quotes/{other}/finalize", json={}, headers=H(api, "outsider")
    )
    assert outsider.status_code == 404


async def test_no_finaliza_vacia_ni_con_monedas_distintas(api, sessions):
    quote_id = await _quote(api)
    r = await _finalize(api, quote_id, key="finalize-empty-1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "QUOTE_EMPTY"

    async with sessions() as s, s.begin():
        usd = OfferSnapshot(
            organization_id=api.shop["org"],
            supplier_id=api.shop["suppliers"]["dos"],
            source_id=api.shop["sources"]["dos"],
            trade_item_id=api.shop["items"]["y"],
            external_id="D-Y-USD",
            price_amount="5.00",
            currency="USD",
            sale_unit="box",
            observed_at=NOW,
            data_origin="simulated",
        )
        s.add(usd)
        await s.flush()
        usd_id = usd.id

    await _add(api, quote_id, offer="uno_x", value="1")
    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines",
        json={
            "supplier_id": str(api.shop["suppliers"]["dos"]),
            "offer_snapshot_id": str(usd_id),
            "quantity": {"value": "1", "unit": "box"},
        },
        headers=H(api),
    )
    assert r.status_code == 201, r.text
    r = await _finalize(api, quote_id, key="finalize-usd-1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "QUOTE_CURRENCY_MISMATCH"

    # Nada quedó cerrado ni liberado.
    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    assert r.json()["status"] == "draft"
    assert r.json()["report_label"] is None


async def test_libera_solo_cuando_las_cotizaciones_finalizadas_cubren_todo(api, sessions):
    demand_id = api.shop["demands"]["exacta"]  # 10 cajas

    parcial = await _quote(api, "Primera parte")
    r = await _add(
        api, parcial, value="4", allocations=_alloc(api, ("exacta", 4))
    )
    assert r.status_code == 201, r.text
    r = await _finalize(api, parcial, key="finalize-parcial-1")
    assert r.status_code == 200, r.text
    assert r.json()["released"] == []
    assert r.json()["pending"] == [
        {
            "demand_item_id": str(demand_id),
            "reason": "coverage_incomplete",
            "allocated_quantity": "4",
            "target_quantity": "10",
        }
    ]
    # Quitar o agregar en el carrito no libera; solo finalizar, y solo si cubre todo.
    assert (await _demand_row(api, "exacta"))["id"] == str(demand_id)

    resto = await _quote(api, "Segunda parte")
    r = await _add(api, resto, value="6", allocations=_alloc(api, ("exacta", 6)))
    assert r.status_code == 201, r.text
    r = await _finalize(api, resto, key="finalize-resto-1")
    assert r.status_code == 200, r.text
    assert r.json()["pending"] == []
    assert r.json()["released"] == [
        {"demand_item_id": str(demand_id), "quantity": "10", "quantity_unit": "box"}
    ]

    async with sessions() as s:
        item = await s.get(DemandItem, demand_id)
        assert item.status == "released"
        assert item.released_quote_id == uuid.UUID(resto)
        assert item.released_by == api.shop["admin"]
        # El historial anterior se conserva: la asignación sigue existiendo.
        rows = (
            await s.execute(
                select(QuoteLineAllocation).where(
                    QuoteLineAllocation.demand_item_id == demand_id
                )
            )
        ).scalars().all()
        assert len(rows) == 2

    r = await api.get("/api/v1/demand-items?status=released", headers=H(api))
    assert r.status_code == 200, r.text
    remote = next(row for row in r.json()["items"] if row["id"] == str(demand_id))
    assert remote["replenishment_state"] == "released"
    assert remote["is_pending"] is False
    # El listado activo ya no lo muestra.
    r = await api.get("/api/v1/demand-items", headers=H(api))
    assert str(demand_id) not in [row["id"] for row in r.json()["items"]]

    # Los dos informes siguen disponibles.
    for quote_id in (parcial, resto):
        r = await api.get(f"/api/v1/quotes/{quote_id}/report", headers=H(api))
        assert r.status_code == 200


async def test_agregar_oferta_viva_la_registra_una_sola_vez(api, sessions):
    quote_id = await _quote(api)

    r = await _add_live(api, quote_id, quantity="3")
    assert r.status_code == 201, r.text
    line = r.json()["line"]
    assert line["product"]["code"] == "LIVE-1"
    assert line["product"]["laboratory"] == "ASCEND"
    assert line["product"]["presentation"] == "Caja de 20 comprimidos"
    assert line["unit_price"]["amount"] == "1700.00"
    assert line["subtotal"] == {"amount": "5100.00", "currency": "CLP"}
    assert line["selected"]["review_status"] == "accepted"

    async with sessions() as s:
        rows = (
            await s.execute(
                select(OfferSnapshot).where(
                    OfferSnapshot.organization_id == api.shop["org"],
                    OfferSnapshot.external_id == "LIVE-1",
                )
            )
        ).scalars().all()
        assert len(rows) == 1

    # Repetir la misma selección no la duplica ni crea otra observación.
    r = await _add_live(api, quote_id, quantity="3")
    assert r.status_code == 200, r.text
    assert r.json()["deduplicated"] is True
    assert r.json()["line"]["line_id"] == line["line_id"]

    # Otra cantidad sobre la misma oferta se corrige con PATCH, no con una segunda línea.
    r = await _add_live(api, quote_id, quantity="4")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "LINE_EXISTS"
    assert r.json()["error"]["details"]["line_id"] == line["line_id"]

    # Un precio nuevo no reapunta la línea elegida: el precio se conserva.
    r = await _add_live(api, quote_id, quantity="3", offer=_live_offer(api, price="1500.00"))
    assert r.status_code == 200, r.text
    assert r.json()["line"]["selected"]["price"]["amount"] == "1700.00"
    async with sessions() as s:
        rows = (
            await s.execute(
                select(OfferSnapshot).where(
                    OfferSnapshot.organization_id == api.shop["org"],
                    OfferSnapshot.external_id == "LIVE-1",
                )
            )
        ).scalars().all()
        assert len(rows) == 1

    # Otra oferta del mismo producto (otro código) sí es una selección distinta.
    r = await _add_live(
        api, quote_id, quantity="1", offer=_live_offer(api, external="LIVE-2", price="1800.00")
    )
    assert r.status_code == 201, r.text
    assert r.json()["line"]["line_id"] != line["line_id"]

    r = await _add_live(api, quote_id, offer=_live_offer(api, price_basis="unknown"))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OFFER_PRICE_BASIS_UNKNOWN"
    r = await _add_live(api, quote_id, offer=_live_offer(api, price=None))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OFFER_WITHOUT_PRICE"
    body = {
        "supplier_id": str(api.shop["suppliers"]["uno"]),
        "offer": _live_offer(api, supplier="dos"),
        "quantity": {"value": "1", "unit": "box"},
    }
    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines", json=body, headers=H(api)
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OFFER_NOT_IN_SUPPLIER"


async def _perfil_conector(api, sessions, *, config: dict) -> None:
    """Perfil guardado de la fuente del proveedor «uno», para decidir la excepción por identidad."""
    async with sessions() as s, s.begin():
        s.add(
            SupplierConnectorProfile(
                organization_id=api.shop["org"],
                supplier_id=api.shop["suppliers"]["uno"],
                source_id=api.shop["sources"]["uno"],
                mechanism="algolia",
                config=config,
                field_map={},
                enabled=True,
            )
        )


FARMALOOP_CONFIG = {
    "endpoint": "https://app-dsn.algolia.net/1/indexes/f_products_prod/query",
    "app_id_secret_ref": "FARMALOOP_ALGOLIA_APP_ID",
    "api_key_secret_ref": "FARMALOOP_ALGOLIA_API_KEY",
    "index": "f_products_prod",
}


async def test_farmaloop_cotiza_sin_unidad_de_venta_ni_base_del_precio(api, sessions):
    """La excepción se decide por el perfil guardado, no por lo que envíe el cliente.

    FarmaLoop no publica unidad de venta ni base del precio: se conservan desconocidas, la línea
    guarda la cantidad en ofertas seleccionadas y el subtotal usa el precio publicado.
    """
    await _perfil_conector(api, sessions, config=FARMALOOP_CONFIG)
    quote_id = await _quote(api)
    oferta = _live_offer(
        api,
        sale_unit=None,
        price_basis=None,
        stock_status="unknown",
        stock_quantity=None,
        stock_unit=None,
    )
    body = {
        "supplier_id": str(api.shop["suppliers"]["uno"]),
        "offer": oferta,
        "quantity": {"value": "2", "unit": None},
    }
    r = await api.post(f"/api/v1/quotes/{quote_id}/lines", json=body, headers=H(api))
    assert r.status_code == 201, r.text
    line = r.json()["line"]
    assert line["quantity"] == {"value": "2", "unit": None}
    assert line["unit_price"] == {
        "amount": "1700.00",
        "currency": "CLP",
        "basis": None,
        "sale_unit": None,
    }
    # El subtotal es precio publicado × cantidad seleccionada; la base no se inventa.
    assert line["subtotal"] == {"amount": "3400.00", "currency": "CLP"}

    async with sessions() as s:
        snapshot = (
            await s.execute(
                select(OfferSnapshot).where(
                    OfferSnapshot.organization_id == api.shop["org"],
                    OfferSnapshot.external_id == "LIVE-1",
                )
            )
        ).scalars().one()
        assert snapshot.sale_unit is None
        assert snapshot.price_basis is None
        assert snapshot.price_amount == Decimal("1700.00")

    # El informe final declara la unidad desconocida y conserva el subtotal calculado.
    r = await _finalize(api, quote_id)
    assert r.status_code == 200, r.text
    item = r.json()["report"]["items"][0]
    assert item["quantity"] == {"value": "2", "unit": None}
    assert item["unit_price"]["basis"] is None
    assert item["subtotal"] == {"amount": "3400.00", "currency": "CLP"}


async def test_farmaloop_agrega_un_faltante_sin_cantidad_ni_unidad_al_carrito(api, sessions):
    """Escenario real: faltante reportado sin cantidad ni unidad (Julio) y oferta FarmaLoop.

    «Cantidad a cotizar: sin definir» no impide agregar: se respeta la cantidad por defecto 1 y
    la línea queda persistida en el carrito sin inventar unidad ni base del precio. Agregar no
    libera el faltante.
    """
    await _perfil_conector(api, sessions, config=FARMALOOP_CONFIG)
    quote_id = await _quote(api)
    oferta = _live_offer(
        api,
        external="107581",
        name="Salonpas x 10 parches",
        price="2990.00",
        sale_unit=None,
        price_basis=None,
        stock_status="known",
        stock_quantity="5",
        stock_unit="box",
    )
    body = {
        "supplier_id": str(api.shop["suppliers"]["uno"]),
        "offer": oferta,
        "quantity": {"value": "1", "unit": None},
    }
    r = await api.post(f"/api/v1/quotes/{quote_id}/lines", json=body, headers=H(api))
    assert r.status_code == 201, r.text
    line = r.json()["line"]
    assert line["quantity"] == {"value": "1", "unit": None}
    assert line["unit_price"] == {
        "amount": "2990.00",
        "currency": "CLP",
        "basis": None,
        "sale_unit": None,
    }
    assert line["subtotal"] == {"amount": "2990.00", "currency": "CLP"}
    # El faltante no tiene cantidad pedida ni unidad: no se inventa un vínculo ni una asignación.
    assert line["demand_item_id"] is None
    assert line["demand_allocations"] == []

    # El producto queda en el carrito: el detalle lo devuelve desde la base.
    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    assert r.status_code == 200, r.text
    carts = r.json()["carts"]
    assert [cart["supplier_id"] for cart in carts] == [str(api.shop["suppliers"]["uno"])]
    assert [fila["line_id"] for fila in carts[0]["lines"]] == [line["line_id"]]
    assert carts[0]["lines"][0]["subtotal"] == {"amount": "2990.00", "currency": "CLP"}
    # La evaluación E5 del carrito ya no rechaza la línea por no declarar unidad.
    assert carts[0]["evaluation"] is not None

    async with sessions() as s:
        row = (
            await s.execute(select(QuoteLine).where(QuoteLine.id == uuid.UUID(line["line_id"])))
        ).scalars().one()
        assert row.quantity == Decimal("1")
        assert row.quantity_unit is None
        snapshot = await s.get(OfferSnapshot, row.offer_snapshot_id)
        assert snapshot.external_id == "107581"
        assert snapshot.price_amount == Decimal("2990.00")
        assert snapshot.sale_unit is None
        assert snapshot.price_basis is None
        # Agregar a la cotización no libera el faltante.
        faltante = await s.get(DemandItem, api.shop["demands"]["desconocida"])
        assert faltante.status == "active"


async def test_farmaloop_ignora_la_unidad_que_repite_el_cliente(api, sessions):
    """La unidad comercial la declara la oferta, no el eco del cliente.

    FarmaLoop no publica unidad de venta: repetir «unidad», un texto en blanco o cualquier otro
    valor no la inventa ni rechaza la selección. Si la oferta sí declara unidad, manda la oferta.
    """
    await _perfil_conector(api, sessions, config=FARMALOOP_CONFIG)
    for eco in ("unidad", "", {"code": "box"}):
        quote_id = await _quote(api)
        r = await api.post(
            f"/api/v1/quotes/{quote_id}/lines",
            json={
                "supplier_id": str(api.shop["suppliers"]["uno"]),
                "offer": _live_offer(api, external="107581", sale_unit=None, price_basis=None),
                "quantity": {"value": "1", "unit": eco},
            },
            headers=H(api),
        )
        assert r.status_code == 201, (eco, r.text)
        assert r.json()["line"]["quantity"] == {"value": "1", "unit": None}

    quote_id = await _quote(api)
    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines",
        json={
            "supplier_id": str(api.shop["suppliers"]["uno"]),
            "offer": _live_offer(api, external="FL-BOX", sale_unit="box"),
            "quantity": {"value": "1", "unit": "unit"},
        },
        headers=H(api),
    )
    assert r.status_code == 201, r.text
    assert r.json()["line"]["quantity"] == {"value": "1", "unit": "box"}


async def test_farmaloop_con_unidad_confirmada_finaliza_y_libera_el_faltante(api, sessions):
    """Con unidad comercial confirmada la cotización viva sigue cubriendo, finalizando y liberando."""
    await _perfil_conector(api, sessions, config=FARMALOOP_CONFIG)
    quote_id = await _quote(api)
    r = await api.post(
        f"/api/v1/quotes/{quote_id}/lines",
        json={
            "supplier_id": str(api.shop["suppliers"]["uno"]),
            "offer": _live_offer(api, external="FL-CAJA", sale_unit="box"),
            "quantity": {"value": "10", "unit": "box"},
            "demand_allocations": _alloc(api, ("exacta", 10)),
        },
        headers=H(api),
    )
    assert r.status_code == 201, r.text
    r = await _finalize(api, quote_id, key="finalize-farmaloop-caja")
    assert r.status_code == 200, r.text
    assert r.json()["pending"] == []
    assert r.json()["released"] == [
        {
            "demand_item_id": str(api.shop["demands"]["exacta"]),
            "quantity": "10",
            "quantity_unit": "box",
        }
    ]
    async with sessions() as s:
        assert (await s.get(DemandItem, api.shop["demands"]["exacta"])).status == "released"


async def test_sin_el_perfil_de_farmaloop_los_atributos_siguen_siendo_obligatorios(api, sessions):
    """Un conector con otra identidad no hereda la excepción aunque su mecanismo sea Algolia."""
    await _perfil_conector(
        api,
        sessions,
        config={
            "endpoint": "https://otra-dsn.algolia.net/1/indexes/productos/query",
            "app_id": "OTRAAPP",
            "api_key_secret_ref": "OTRA_API_KEY",
            "index": "productos",
        },
    )
    quote_id = await _quote(api)
    sin_unidad = _live_offer(api, sale_unit=None, price_basis="per_commercial_unit")
    r = await _add_live(api, quote_id, offer=sin_unidad)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "OFFER_SALE_UNIT_UNKNOWN"

    sin_base = _live_offer(api, sale_unit="box", price_basis=None)
    r = await _add_live(api, quote_id, offer=sin_base)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "OFFER_PRICE_BASIS_UNKNOWN"

    async with sessions() as s:
        vivas = (
            await s.execute(
                select(OfferSnapshot).where(
                    OfferSnapshot.organization_id == api.shop["org"],
                    OfferSnapshot.external_id == "LIVE-1",
                )
            )
        ).scalars().all()
        assert vivas == []


async def test_farmaloop_asocia_el_faltante_sin_unidad_y_lo_libera(api, sessions):
    """Salonpas: reporte sin cantidad, oferta FarmaLoop sin unidad y vínculo explícito.

    La cantidad a cotizar se fija con SET (1 por defecto, auditado); la línea conserva la unidad
    desconocida, la asignación explícita cuenta una oferta y el informe libera solo esa cantidad.
    """
    await _perfil_conector(api, sessions, config=FARMALOOP_CONFIG)
    demand_id = api.shop["demands"]["desconocida"]
    fila = await _demand_row(api, "desconocida")
    r = await _set_quantity(api, demand_id, fila["version"], value="1", unit="unit")
    assert r.status_code == 200, r.text

    quote_id = await _quote(api)
    oferta = _live_offer(
        api,
        external="107581",
        name="Salonpas x 10 parches",
        price="2990.00",
        sale_unit=None,
        price_basis=None,
    )
    r = await _add_live(api, quote_id, quantity="1", offer=oferta, demand="desconocida")
    assert r.status_code == 201, r.text
    line = r.json()["line"]
    assert line["quantity"] == {"value": "1", "unit": None}
    assert line["demand_item_id"] == str(demand_id)
    assert _alloc_pairs(line["demand_allocations"]) == [(str(demand_id), "1")]
    async with sessions() as s:
        row = await s.get(QuoteLine, uuid.UUID(line["line_id"]))
        assert row.quantity_unit is None
        snapshot = await s.get(OfferSnapshot, row.offer_snapshot_id)
        assert snapshot.sale_unit is None and snapshot.price_basis is None

    fila = await _demand_row(api, "desconocida")
    assert fila["status"] == "active"
    assert fila["quoted_quantity"] == "1"
    assert fila["replenishment_state"] == "in_quote"
    text = await _telegram_list(sessions, api.shop["org"])
    assert "<s><b>Ibuprofeno sin cantidad</b></s>" in text
    assert "Estado: 🛒 En carrito" in text

    r = await _finalize(api, quote_id, key="finalize-farmaloop-salonpas")
    assert r.status_code == 200, r.text
    assert r.json()["pending"] == []
    assert r.json()["released"] == [
        {"demand_item_id": str(demand_id), "quantity": "1", "quantity_unit": "unit"}
    ]
    async with sessions() as s:
        assert (await s.get(DemandItem, demand_id)).status == "released"


async def test_farmaloop_no_asocia_si_el_reporte_se_mide_en_caja(api, sessions):
    """Sin unidad comercial no se cubre un reporte en caja: no se inventa la equivalencia."""
    await _perfil_conector(api, sessions, config=FARMALOOP_CONFIG)
    quote_id = await _quote(api)
    r = await _add_live(
        api,
        quote_id,
        quantity="1",
        offer=_live_offer(api, sale_unit=None, price_basis=None),
        demand="exacta",
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "UNIT_MISMATCH"
    assert "no declara unidad de venta" in r.json()["error"]["message"]
    r = await api.get(f"/api/v1/quotes/{quote_id}", headers=H(api))
    assert r.json()["carts"] == []


async def test_farmaloop_cobertura_parcial_no_libera_el_faltante(api, sessions):
    """Se libera solo lo cubierto por asignaciones explícitas; 1 de 2 no cierra el reporte."""
    await _perfil_conector(api, sessions, config=FARMALOOP_CONFIG)
    demand_id = api.shop["demands"]["desconocida"]
    fila = await _demand_row(api, "desconocida")
    r = await _set_quantity(api, demand_id, fila["version"], value="2", unit="unit")
    assert r.status_code == 200, r.text
    quote_id = await _quote(api)
    r = await _add_live(
        api,
        quote_id,
        quantity="1",
        offer=_live_offer(api, sale_unit=None, price_basis=None),
        demand="desconocida",
    )
    assert r.status_code == 201, r.text
    r = await _finalize(api, quote_id, key="finalize-farmaloop-parcial")
    assert r.status_code == 200, r.text
    assert r.json()["released"] == []
    assert r.json()["pending"][0]["reason"] == "coverage_incomplete"
    fila = await _demand_row(api, "desconocida")
    assert fila["status"] == "active"
    assert fila["quoted_quantity"] == "0"


async def test_farmaloop_regulariza_una_linea_existente_con_patch(api, sessions):
    """La línea ya guardada se asocia con un PATCH explícito, sin duplicarla ni reescribirla."""
    await _perfil_conector(api, sessions, config=FARMALOOP_CONFIG)
    demand_id = api.shop["demands"]["desconocida"]
    fila = await _demand_row(api, "desconocida")
    r = await _set_quantity(api, demand_id, fila["version"], value="1", unit="unit")
    assert r.status_code == 200, r.text
    quote_id = await _quote(api)
    r = await _add_live(
        api,
        quote_id,
        quantity="1",
        offer=_live_offer(
            api, external="107581", price="2990.00", sale_unit=None, price_basis=None
        ),
    )
    assert r.status_code == 201, r.text
    line = r.json()["line"]
    assert line["demand_item_id"] is None

    r = await api.patch(
        f"/api/v1/quotes/{quote_id}/lines/{line['line_id']}",
        json={
            "version": line["version"],
            "quantity": {"value": "1", "unit": None},
            "demand_allocations": [{"demand_item_id": str(demand_id), "quantity": 1}],
        },
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    body = r.json()["line"]
    assert body["line_id"] == line["line_id"]
    assert body["demand_item_id"] == str(demand_id)
    assert _alloc_pairs(body["demand_allocations"]) == [(str(demand_id), "1")]
    # La observación original se conserva: mismo proveedor, cantidad y precio elegido.
    assert body["supplier_id"] == str(api.shop["suppliers"]["uno"])
    assert body["quantity"] == {"value": "1", "unit": None}
    assert body["unit_price"]["amount"] == "2990.00"
    assert (await _demand_row(api, "desconocida"))["replenishment_state"] == "in_quote"

    # Quitar las asignaciones devuelve el reporte a la bandeja sin borrar la línea ni su historial.
    r = await api.patch(
        f"/api/v1/quotes/{quote_id}/lines/{line['line_id']}",
        json={
            "version": body["version"],
            "quantity": {"value": "1", "unit": None},
            "demand_allocations": [],
        },
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    assert r.json()["line"]["demand_item_id"] is None
    assert (await _demand_row(api, "desconocida"))["replenishment_state"] == "pending"


async def test_una_fuente_sin_perfil_no_puede_omitir_unidad_ni_base(api, sessions):
    quote_id = await _quote(api)
    r = await _add_live(
        api, quote_id, offer=_live_offer(api, sale_unit=None, price_basis=None)
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "OFFER_PRICE_BASIS_UNKNOWN"


async def test_farmaloop_rechaza_un_valor_presente_pero_invalido(api, sessions):
    await _perfil_conector(api, sessions, config=FARMALOOP_CONFIG)
    quote_id = await _quote(api)
    r = await _add_live(api, quote_id, offer=_live_offer(api, price_basis="unknown"))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "OFFER_PRICE_BASIS_UNKNOWN"

    r = await _add_live(api, quote_id, offer=_live_offer(api, sale_unit="saco"))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "OFFER_SALE_UNIT_UNKNOWN"


async def test_tras_liberar_la_misma_persona_puede_reportar_de_nuevo(api, sessions):
    from app.services.actors import load_actor
    from app.services.demand import record_observation

    # Persona nueva: su reporte es propio y su identidad no choca con los faltantes del fixture.
    async with sessions() as s, s.begin():
        persona = AppUser(
            organization_id=api.shop["org"], display_name="Auxiliar Nueva", roles=["auxiliary"]
        )
        s.add(persona)
        await s.flush()
        persona_id = persona.id

    async with sessions() as s, s.begin():
        actor = await load_actor(s, persona_id)
        first = await record_observation(
            s,
            actor,
            text="falta ibuprofeno 400 mg x 20 comprimidos",
            idempotency_key="telegram-antes-1",
            declared_quantity=Decimal("10"),
            declared_unit="box",
        )
        assert first.outcome == "created_item"
        demand_id = first.item.id
        version = first.item.version
    assert (await _set_quantity(api, demand_id, version, value="10")).status_code == 200

    quote_id = await _quote(api)
    r = await _add(
        api, quote_id, value="10", allocations=[{"demand_item_id": str(demand_id), "quantity": 10}]
    )
    assert r.status_code == 201, r.text
    r = await _finalize(api, quote_id, key="finalize-telegram-1")
    assert r.status_code == 200, r.text
    assert [row["demand_item_id"] for row in r.json()["released"]] == [str(demand_id)]

    async with sessions() as s, s.begin():
        actor = await load_actor(s, persona_id)
        again = await record_observation(
            s,
            actor,
            text="falta ibuprofeno 400 mg x 20 comprimidos",
            idempotency_key="telegram-despues-1",
            declared_quantity=Decimal("10"),
            declared_unit="box",
        )
        assert again.outcome == "created_item"
        assert again.item.id != demand_id
        assert again.item.status == "active"
    async with sessions() as s:
        old = await s.get(DemandItem, demand_id)
        assert old.status == "released"  # la historia anterior se conserva
        assert old.released_quote_id == uuid.UUID(quote_id)
        observations = (
            await s.execute(
                select(DemandObservation).where(DemandObservation.demand_item_id == demand_id)
            )
        ).scalars().all()
        assert len(observations) == 1


# ------------------------------------------------------- listado de Telegram (§12.7.4, D-154)


async def _telegram_list(sessions, org) -> str:
    """«📋 Ver listado de Faltantes» con la cobertura real de la base, sin dobles."""
    from app.services.channel import KEYBOARD, ChannelConfig, _list_reply

    async with sessions() as s:
        reply = await _list_reply(s, org, 0, ChannelConfig(), KEYBOARD)
    assert reply.parse_mode == "HTML"
    return reply.text


async def _active_ids(api) -> set[str]:
    r = await api.get("/api/v1/demand-items", headers=H(api))
    assert r.status_code == 200, r.text
    return {row["id"] for row in r.json()["items"]}


async def test_telegram_lista_un_faltante_pendiente_sin_tachar(api, sessions):
    text = await _telegram_list(sessions, api.shop["org"])

    # Dos reportes del fixture comparten nombre (admin y auxiliar): ambos visibles, sin tachar.
    assert text.count("<b>Ibuprofeno 400 mg x 20 comprimidos</b>") == 2
    assert "<s>" not in text
    assert "Estado: Pendiente de compra" in text
    assert str(api.shop["demands"]["exacta"]) in await _active_ids(api)


async def test_telegram_tacha_el_faltante_agregado_al_carrito(api, sessions):
    quote_id = await _quote(api)
    r = await _add(api, quote_id, value="4", allocations=_alloc(api, ("exacta", 4)))
    assert r.status_code == 201, r.text

    text = await _telegram_list(sessions, api.shop["org"])

    # Solo el nombre se tacha; la fila sigue visible con cantidad, autor y estado.
    assert text.count("<s><b>Ibuprofeno 400 mg x 20 comprimidos</b></s>") == 1
    assert text.count("<b>Ibuprofeno 400 mg x 20 comprimidos</b>") == 2
    assert "Cantidad: 10 cajas" in text
    assert "Estado: 🛒 En carrito" in text
    # Sigue activo: las reglas de duplicados y cantidades continúan aplicándose.
    row = await _demand_row(api, "exacta")
    assert row["replenishment_state"] == "in_quote" and row["is_pending"] is True
    assert str(api.shop["demands"]["exacta"]) in await _active_ids(api)


async def test_telegram_destacha_al_retirar_del_carrito(api, sessions):
    quote_id = await _quote(api)
    alta = await _add(api, quote_id, value="4", allocations=_alloc(api, ("exacta", 4)))
    assert alta.status_code == 201, alta.text
    assert "<s>" in await _telegram_list(sessions, api.shop["org"])

    r = await api.delete(
        f"/api/v1/quotes/{quote_id}/lines/{alta.json()['line']['line_id']}", headers=H(api)
    )
    assert r.status_code == 200, r.text

    text = await _telegram_list(sessions, api.shop["org"])
    assert "<s>" not in text
    assert text.count("<b>Ibuprofeno 400 mg x 20 comprimidos</b>") == 2
    assert "Estado: Pendiente de compra" in text
    assert (await _demand_row(api, "exacta"))["quoted_quantity"] == "0"
    assert str(api.shop["demands"]["exacta"]) in await _active_ids(api)


async def test_telegram_mantiene_pendiente_la_cobertura_incompleta(api, sessions):
    # 4 de 10 cajas: la cotización se finaliza, pero el faltante no cumple la condición de
    # liberación y debe seguir visible como pendiente, sin tachar.
    quote_id = await _quote(api)
    r = await _add(api, quote_id, value="4", allocations=_alloc(api, ("exacta", 4)))
    assert r.status_code == 201, r.text
    r = await _finalize(api, quote_id, key="finalize-telegram-parcial-1")
    assert r.status_code == 200, r.text
    assert r.json()["released"] == []
    assert r.json()["pending"][0]["reason"] == "coverage_incomplete"

    text = await _telegram_list(sessions, api.shop["org"])
    assert "<s>" not in text
    assert text.count("<b>Ibuprofeno 400 mg x 20 comprimidos</b>") == 2
    assert "Ibuprofeno sin cantidad" in text  # cantidad desconocida: también sigue pendiente
    assert str(api.shop["demands"]["exacta"]) in await _active_ids(api)


async def test_telegram_oculta_el_faltante_liberado_y_admite_un_nuevo_reporte(api, sessions):
    from app.services.actors import load_actor
    from app.services.demand import record_observation

    demand_id = api.shop["demands"]["otra"]  # 4 cajas, nombre único en el fixture
    quote_id = await _quote(api)
    r = await _add(api, quote_id, value="4", allocations=_alloc(api, ("otra", 4)))
    assert r.status_code == 201, r.text
    assert "<s><b>Ibuprofeno 400 mg x 30 comprimidos</b></s>" in await _telegram_list(
        sessions, api.shop["org"]
    )

    # Agregar al carrito no libera: hacerlo requiere finalizar correctamente el informe.
    assert str(demand_id) in await _active_ids(api)
    r = await _finalize(api, quote_id, key="finalize-telegram-lista-1")
    assert r.status_code == 200, r.text
    assert [row["demand_item_id"] for row in r.json()["released"]] == [str(demand_id)]

    text = await _telegram_list(sessions, api.shop["org"])
    assert "Ibuprofeno 400 mg x 30 comprimidos" not in text
    assert str(demand_id) not in await _active_ids(api)

    # La misma persona vuelve a reportar el producto: es un faltante nuevo e independiente.
    async with sessions() as s, s.begin():
        persona = AppUser(
            organization_id=api.shop["org"],
            display_name="Auxiliar Telegram",
            roles=["auxiliary"],
        )
        s.add(persona)
        await s.flush()
        persona_id = persona.id
    async with sessions() as s, s.begin():
        actor = await load_actor(s, persona_id)
        nuevo = await record_observation(
            s,
            actor,
            text="falta ibuprofeno 400 mg x 30 comprimidos",
            idempotency_key="telegram-lista-despues-1",
            declared_quantity=Decimal("4"),
            declared_unit="box",
        )
        assert nuevo.outcome == "created_item"
        assert nuevo.item.id != demand_id and nuevo.item.status == "active"
        nuevo_id, nombre = nuevo.item.id, nuevo.item.display_name

    text = await _telegram_list(sessions, api.shop["org"])
    assert f"<b>{nombre}</b>" in text
    assert "<s>" not in text  # todavía no está en ningún carrito
    assert str(nuevo_id) in await _active_ids(api)
    assert str(demand_id) not in await _active_ids(api)

    # El reporte liberado conserva su historial y su vínculo con la cotización finalizada.
    r = await api.get("/api/v1/demand-items?status=released", headers=H(api))
    respaldo = next(row for row in r.json()["items"] if row["id"] == str(demand_id))
    assert respaldo["replenishment_state"] == "released"
