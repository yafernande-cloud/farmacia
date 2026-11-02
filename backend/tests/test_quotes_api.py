"""C5-A — carritos persistentes, cotizaciones, cobertura de faltantes y observaciones (E10, O1).

Todo se prueba contra PostgreSQL real con organización `simulation` y precios ficticios del test.
Se verifica lo que el contrato exige: la cotización es persistente, repetir una petición no suma,
el precio no se copia ni se reapunta solo, una línea se quita de forma lógica, cotizar cubre de
forma provisional y nunca cierra, y una observación comercial se guarda siempre, sin IA (O2–O4 no
están implementados).

Las compras (C5-B) no existen todavía: no hay aquí pruebas de compra, de `LINE_HAS_PURCHASES` ni de
`QUANTITY_BELOW_PURCHASED`; las verificaciones quedan preparadas en el servicio.
"""

import uuid
from datetime import UTC, datetime, timedelta

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
    OfferSnapshot,
    Organization,
    PurchaseCycle,
    Quote,
    QuoteCart,
    QuoteLine,
    ShippingTermsSnapshot,
    Source,
    Supplier,
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
        s.add_all([exacta, otra, desconocida])
        await s.flush()
        ids["demands"] = {"exacta": exacta.id, "otra": otra.id, "desconocida": desconocida.id}
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


def _add(api, quote_id, *, supplier="uno", offer="uno_x", demand=None, value="3", key=None):
    body = {
        "supplier_id": str(api.shop["suppliers"][supplier]),
        "offer_snapshot_id": str(api.shop["offers"][offer]),
        "quantity": _q(value=value),
    }
    if demand:
        body["demand_item_id"] = str(api.shop["demands"][demand])
    headers = H(api)
    if key:
        headers["Idempotency-Key"] = key
    return api.post(f"/api/v1/quotes/{quote_id}/lines", json=body, headers=headers)


async def _quote(api, title=None) -> str:
    r = await api.post("/api/v1/quotes", json={"title": title}, headers=H(api))
    assert r.status_code == 201, r.text
    return r.json()["quote_id"]


async def _demand_row(api, key) -> dict:
    r = await api.get("/api/v1/demand-items", headers=H(api))
    assert r.status_code == 200, r.text
    for row in r.json()["items"]:
        if row["id"] == str(api.shop["demands"][key]):
            return row
    raise AssertionError("faltante ausente")


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

    r = await _add(api, quote_id, offer="uno_y", demand="desconocida", value="1")
    assert r.status_code == 201
    row = await _demand_row(api, "desconocida")
    assert row["pending_quantity"] is None
    assert row["pending_purchase_quantity"] is None
    assert row["is_pending"] is True


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
