"""C4 — E1/E2/E3 (búsqueda sobre el catálogo local) y E15 (cadenas, sin persistencia).

Todo se prueba contra PostgreSQL real con datos de `simulation`; no se usan precios reales ni
conectores externos. Las ofertas sembradas son `fixture` del propio test.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.deps import get_session
from app.api.v1 import search as search_api
from app.core.config import get_settings
from app.main import create_app
from app.models import (
    AppUser,
    OfferSnapshot,
    Organization,
    SearchResult,
    SearchRun,
    Source,
    Supplier,
    TradeItem,
)
from app.services import chain_search as chain_svc
from app.services.actors import load_actor

pytestmark = pytest.mark.db

# Relativo al reloj de la corrida: la frescura de la fuente (24 h) se mide contra «ahora».
NOW = datetime.now(UTC) - timedelta(minutes=5)


def _attrs(
    ingredient: str | None,
    *,
    strength: tuple[str, str] | None = None,
    form: str | None = None,
    content: tuple[str, str] | None = None,
    ingredients: list[str] | None = None,
) -> dict:
    attributes: dict = {"schema_version": 2, "unassigned": []}
    if ingredient:
        attributes["ingredient"] = ingredient
    if ingredients:
        attributes["ingredients"] = ingredients
    if strength:
        attributes["strengths"] = [
            {
                "measure": {"value": strength[0], "unit": strength[1]},
                "kind": "amount_per_unit",
                "basis": "explicit",
                "role_rule": "unit",
            }
        ]
    if form:
        attributes["form"] = {"value": form, "basis": "explicit", "origin": "text"}
    if content:
        attributes["content"] = {
            "basis": "explicit",
            "origin": "text",
            "measure": {"value": content[0], "unit": content[1]},
        }
    return attributes


@pytest.fixture
async def farm(sessions):
    ids: dict = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia búsqueda", kind="simulation")
        other = Organization(name="Otra búsqueda", kind="simulation")
        s.add_all([org, other])
        await s.flush()
        ids["org"], ids["other_org"] = org.id, other.id
        admin = AppUser(organization_id=org.id, display_name="Admin", roles=["admin"])
        aux = AppUser(organization_id=org.id, display_name="Auxiliar", roles=["auxiliary"])
        outsider = AppUser(organization_id=other.id, display_name="Externo", roles=["admin"])
        s.add_all([admin, aux, outsider])
        await s.flush()
        ids["admin"], ids["aux"], ids["outsider"] = admin.id, aux.id, outsider.id

        suppliers = [
            Supplier(organization_id=org.id, name=f"Proveedor {n}", data_origin="simulated")
            for n in ("Norte", "Sur")
        ]
        s.add_all(suppliers)
        await s.flush()
        sources = [
            Source(
                organization_id=org.id,
                kind="supplier",
                supplier_id=sup.id,
                name=f"Lista {sup.name}",
                mode="manual_import",
                capabilities=["search", "price", "stock"],
                last_success_at=NOW,
                data_origin="simulated",
            )
            for sup in suppliers
        ]
        # Fuente habilitada sin ninguna observación: se informa `not_configured`, no «sin resultados».
        idle_supplier = Supplier(organization_id=org.id, name="Proveedor Sin Datos")
        s.add(idle_supplier)
        await s.flush()
        idle = Source(
            organization_id=org.id,
            kind="supplier",
            supplier_id=idle_supplier.id,
            name="Lista Sin Datos",
            mode="none",
            capabilities=["search"],
            data_origin="simulated",
        )
        chain = Source(
            organization_id=org.id,
            kind="chain_reference",
            name="Cadena de referencia",
            mode="none",
            capabilities=[],
            data_origin="simulated",
        )
        s.add_all([*sources, idle, chain])
        await s.flush()
        ids["sources"] = [src.id for src in sources]
        ids["idle_source"], ids["chain_source"] = idle.id, chain.id

        items = {
            "a": TradeItem(
                organization_id=org.id,
                display_name="Ibuprofeno 400 mg x 20 comprimidos",
                identity_key="fixture-ibuprofeno-400-20",
                ingredient="ibuprofeno",
                attributes=_attrs(
                    "ibuprofeno", strength=("400", "mg"), form="comprimido", content=("20", "un")
                ),
                data_origin="simulated",
            ),
            "b": TradeItem(
                organization_id=org.id,
                display_name="Ibuprofeno 600 mg x 20 comprimidos",
                identity_key="fixture-ibuprofeno-600-20",
                ingredient="ibuprofeno",
                attributes=_attrs(
                    "ibuprofeno", strength=("600", "mg"), form="comprimido", content=("20", "un")
                ),
                data_origin="simulated",
            ),
            "c": TradeItem(
                organization_id=org.id,
                display_name="Ibuprofeno 400 mg x 10 comprimidos",
                identity_key="fixture-ibuprofeno-400-10",
                ingredient="ibuprofeno",
                attributes=_attrs(
                    "ibuprofeno", strength=("400", "mg"), form="comprimido", content=("10", "un")
                ),
                data_origin="simulated",
            ),
            "combined": TradeItem(
                organization_id=org.id,
                display_name="Ibuprofeno 200 mg + paracetamol 500 mg x 10 comprimidos",
                identity_key="fixture-combinado",
                ingredient="ibuprofeno",
                attributes=_attrs(
                    "ibuprofeno",
                    strength=("200", "mg"),
                    form="comprimido",
                    content=("10", "un"),
                    ingredients=["ibuprofeno", "paracetamol"],
                ),
                data_origin="simulated",
            ),
            "other": TradeItem(
                organization_id=org.id,
                display_name="Paracetamol 500 mg x 20 comprimidos",
                identity_key="fixture-paracetamol-500-20",
                ingredient="paracetamol",
                attributes=_attrs(
                    "paracetamol", strength=("500", "mg"), form="comprimido", content=("20", "un")
                ),
                data_origin="simulated",
            ),
        }
        s.add_all(items.values())
        await s.flush()
        ids["items"] = {key: item.id for key, item in items.items()}

        offers = [
            OfferSnapshot(
                organization_id=org.id,
                supplier_id=suppliers[0].id,
                source_id=sources[0].id,
                trade_item_id=items["a"].id,
                external_id="N-1",
                price_amount="2348.00",
                currency="CLP",
                sale_unit="box",
                sale_unit_text="caja",
                stock_status="known",
                stock_quantity="90",
                stock_unit="box",
                tax_included=True,
                observed_at=NOW,
                data_origin="simulated",
            ),
            OfferSnapshot(
                organization_id=org.id,
                supplier_id=suppliers[0].id,
                source_id=sources[0].id,
                trade_item_id=items["a"].id,
                external_id="N-1-old",
                price_amount="2990.00",
                currency="CLP",
                sale_unit="box",
                sale_unit_text="caja",
                stock_status="unknown",
                observed_at=datetime(2026, 10, 1, tzinfo=UTC),
                data_origin="simulated",
            ),
            OfferSnapshot(
                organization_id=org.id,
                supplier_id=suppliers[1].id,
                source_id=sources[1].id,
                trade_item_id=items["a"].id,
                external_id="S-1",
                price_amount="1990.00",
                currency="CLP",
                sale_unit="box",
                sale_unit_text="caja",
                stock_status="unknown",
                tax_included=None,
                observed_at=NOW,
                data_origin="simulated",
            ),
            OfferSnapshot(
                organization_id=org.id,
                supplier_id=suppliers[0].id,
                source_id=sources[0].id,
                trade_item_id=items["b"].id,
                external_id="N-2",
                price_amount="3100.00",
                currency="CLP",
                sale_unit="box",
                stock_status="unknown",
                observed_at=NOW,
                data_origin="simulated",
            ),
            # Oferta marcada para revisión: no cuenta como verificada.
            OfferSnapshot(
                organization_id=org.id,
                supplier_id=suppliers[0].id,
                source_id=sources[0].id,
                trade_item_id=items["c"].id,
                external_id="N-3",
                price_amount="1500.00",
                currency="CLP",
                sale_unit="box",
                stock_status="unknown",
                review_status="needs_review",
                notes="presentación dudosa",
                observed_at=NOW,
                data_origin="simulated",
            ),
        ]
        s.add_all(offers)
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
    chain_svc.store.reset()
    search_api._chain_limits.reset()
    get_settings.cache_clear()


def H(api, who="admin"):
    return {"X-Demo-User-Id": str(api.farm[who])}


async def _search(api, query="ibuprofeno 400 mg x 20 comprimidos", **body):
    return await api.post("/api/v1/searches", json={"query": query, **body}, headers=H(api))


async def test_e1_clasifica_relacion_y_guarda_la_corrida(api, sessions):
    r = await _search(api)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["query"]["original_text"] == "ibuprofeno 400 mg x 20 comprimidos"
    assert body["errors"] == []
    by_name = {x["display_name"]: x for x in body["results"]}
    exact = by_name["Ibuprofeno 400 mg x 20 comprimidos"]
    assert exact["relation"] == "exact"
    assert exact["match"]["level"] == "exact"
    assert by_name["Ibuprofeno 400 mg x 10 comprimidos"]["relation"] == "other_presentation"
    assert by_name["Ibuprofeno 600 mg x 20 comprimidos"]["relation"] == "related"
    combined = by_name["Ibuprofeno 200 mg + paracetamol 500 mg x 10 comprimidos"]
    assert combined["relation"] == "combined"
    assert combined["match"]["level"] == "needs_review"  # un combinado nunca es equivalente
    assert "Paracetamol 500 mg x 20 comprimidos" not in by_name

    async with sessions() as s:
        run = (await s.execute(select(SearchRun))).scalar_one()
        assert run.organization_id == api.farm["org"]
        assert run.data_origin == "simulated"
        assert run.result_count == 4
        stored = (
            await s.execute(select(func.count()).select_from(SearchResult))
        ).scalar_one()
        assert stored == 4


async def test_e1_agrega_ofertas_vigentes_y_verificadas(api):
    body = (await _search(api)).json()
    exact = next(
        x for x in body["results"] if x["display_name"] == "Ibuprofeno 400 mg x 20 comprimidos"
    )
    # 2 fuentes × última observación; la oferta antigua de la misma fuente no cuenta.
    assert exact["offer_count"] == 2
    assert exact["supplier_count"] == 2
    assert exact["lowest_verified_price"] == {"amount": "1990.0000", "currency": "CLP"}
    assert isinstance(exact["lowest_verified_price"]["amount"], str)
    assert len(exact["sources"]) == 2

    reviewed = next(
        x for x in body["results"] if x["display_name"] == "Ibuprofeno 400 mg x 10 comprimidos"
    )
    assert reviewed["offer_count"] == 0  # `needs_review` no es una oferta verificada
    assert reviewed["lowest_verified_price"] is None


async def test_e1_informa_fuentes_sin_datos_como_no_configuradas(api):
    body = (await _search(api)).json()
    states = {s["source_id"]: s["status"] for s in body["sources"]}
    assert states[str(api.farm["idle_source"])] == "not_configured"
    assert states[str(api.farm["sources"][0])] == "ok"


async def test_e1_no_asume_unidad_ni_inventa_potencia(api):
    body = (await _search(api, "ibuprofeno 400")).json()
    assert body["query"]["interpretation"]["unresolved_terms"] == ["400"]
    assert body["query"]["interpretation"]["warnings"] == [
        "cifra sin unidad: no se asume mg (400)"
    ]
    # «400» sin unidad no se convierte en mg: ningún resultado es equivalente. El combinado
    # aparece porque contiene ibuprofeno, pero también se declara no equivalente (§12.1).
    assert {x["relation"] for x in body["results"]} == {"needs_review", "combined"}
    assert all(x["match"]["level"] == "needs_review" for x in body["results"])


async def test_e1_filtros_cierran_el_conjunto(api):
    # El filtro solo resta: deja el combinado (que sí contiene paracetamol y coincide con la
    # consulta) y nunca incorpora el paracetamol solo, que no coincide con «ibuprofeno».
    r = await _search(api, "ibuprofeno 400 mg x 20 comprimidos", filters={"ingredient": "paracetamol"})
    assert r.status_code == 200
    names = [x["display_name"] for x in r.json()["results"]]
    assert names == ["Ibuprofeno 200 mg + paracetamol 500 mg x 10 comprimidos"]

    bad = await _search(api, filters={"precio_max": "1000"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "VALIDATION_ERROR"

    short = await api.post("/api/v1/searches", json={"query": "ab"}, headers=H(api))
    assert short.status_code == 400


async def test_e2_devuelve_el_mismo_snapshot_y_aisla_organizaciones(api):
    created = (await _search(api)).json()
    search_id = created["search_id"]
    again = await api.get(f"/api/v1/searches/{search_id}", headers=H(api))
    assert again.status_code == 200
    assert again.json()["results"] == created["results"]
    assert again.json()["query"] == created["query"]

    outsider = await api.get(f"/api/v1/searches/{search_id}", headers=H(api, "outsider"))
    assert outsider.status_code == 404

    aux = await api.get(f"/api/v1/searches/{search_id}", headers=H(api, "aux"))
    assert aux.status_code == 403

    missing = await api.get(f"/api/v1/searches/{uuid.uuid4()}", headers=H(api))
    assert missing.status_code == 404


async def test_e2_pagina_con_cursor(api):
    created = (await _search(api, limit=2)).json()
    assert len(created["results"]) == 2
    assert created["next_cursor"]
    page2 = await api.get(
        f"/api/v1/searches/{created['search_id']}",
        params={"cursor": created["next_cursor"], "limit": 2},
        headers=H(api),
    )
    assert page2.status_code == 200
    assert len(page2.json()["results"]) == 2
    assert page2.json()["next_cursor"] is None
    bad = await api.get(
        f"/api/v1/searches/{created['search_id']}", params={"cursor": "no-es"}, headers=H(api)
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_CURSOR"


async def test_e3_detalle_con_ofertas_y_cadenas_no_configuradas(api):
    article_id = api.farm["items"]["a"]
    r = await api.get(f"/api/v1/articles/{article_id}", headers=H(api))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["article"]["identity"]["ingredient"] == "ibuprofeno"
    assert body["article"]["match"]["level"] == "exact"
    assert body["offers_total"] == 2  # última por fuente
    assert len(body["offers"]) == 2
    first = body["offers"][0]
    # El modelo de respuesta no filtra campos de `OfferOut` (C0 §4.2).
    assert set(first) == {
        "offer_id",
        "article_id",
        "supplier",
        "external_id",
        "external_id_kind",
        "price",
        "price_per_base_unit",
        "sale_unit",
        "pack",
        "minimum",
        "multiple",
        "stock",
        "taxes",
        "discounts",
        "shipping_terms_id",
        "validity",
        "notes",
        "provenance",
        "freshness",
    }
    assert first["price"]["money"]["currency"] == "CLP"
    assert isinstance(first["price"]["money"]["amount"], str)
    assert first["minimum"] == {"known": False, "quantity": None, "unit": None}
    assert first["stock"]["status"] in ("known", "unknown")
    assert body["reference_prices"] == []
    assert body["annotations"] == [] and body["notes"] == []
    chain = [s for s in body["sources"] if s["kind"] == "chain_reference"]
    assert chain and chain[0]["status"] == "not_configured"

    missing = await api.get(f"/api/v1/articles/{uuid.uuid4()}", headers=H(api))
    assert missing.status_code == 404


async def test_e3_articulo_de_otra_organizacion_no_se_expone(api):
    other = await api.get(f"/api/v1/articles/{api.farm['items']['a']}", headers=H(api, "outsider"))
    assert other.status_code == 404


async def test_e15_cadenas_sin_conector_se_informan_no_configuradas(api):
    r = await api.post(
        "/api/v1/chain-searches", json={"query": "losartán 50 mg"}, headers=H(api)
    )
    assert r.status_code == 202, r.text
    started = r.json()
    assert started["status"] == "pending"
    assert started["expires_at"]

    got = await api.get(f"/api/v1/chain-searches/{started['search_id']}", headers=H(api))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["status"] == "completed"
    assert body["query"] == "losartán 50 mg"
    assert [s["chain"] for s in body["sources"]] == ["cruz_verde", "salcobrand", "ahumada"]
    assert all(s["state"] == "not_configured" and s["results"] == [] for s in body["sources"])
    assert [s["reason"] for s in body["sources"]] == [
        "access_not_authorized",
        "access_not_authorized",
        "access_not_authorized",
    ]


async def test_e15_solo_las_cadenas_pedidas_y_con_limite_de_tasa(api):
    one = await api.post(
        "/api/v1/chain-searches", json={"query": "ibuprofeno", "chains": ["ahumada"]}, headers=H(api)
    )
    got = await api.get(f"/api/v1/chain-searches/{one.json()['search_id']}", headers=H(api))
    assert [s["chain"] for s in got.json()["sources"]] == ["ahumada"]

    invalid = await api.post(
        "/api/v1/chain-searches", json={"query": "ibuprofeno", "chains": ["otra"]}, headers=H(api)
    )
    assert invalid.status_code == 400

    statuses = []
    for _ in range(10):
        r = await api.post(
            "/api/v1/chain-searches", json={"query": "ibuprofeno"}, headers=H(api)
        )
        statuses.append(r.status_code)
        if r.status_code == 429:
            assert r.headers.get("Retry-After") == "60"
            assert r.json()["error"]["code"] == "RATE_LIMITED"
            break
    assert 429 in statuses

    unknown = await api.get(f"/api/v1/chain-searches/{uuid.uuid4()}", headers=H(api))
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "SEARCH_NOT_FOUND"


async def test_e15_el_estado_vive_en_memoria_y_no_persiste(api, sessions):
    created = (
        await api.post("/api/v1/chain-searches", json={"query": "losartán"}, headers=H(api))
    ).json()
    chain_svc.store.reset()  # equivale a reiniciar el proceso
    lost = await api.get(f"/api/v1/chain-searches/{created['search_id']}", headers=H(api))
    assert lost.status_code == 404


async def test_e1_requiere_admin(api):
    r = await api.post(
        "/api/v1/searches", json={"query": "ibuprofeno 400"}, headers=H(api, "aux")
    )
    assert r.status_code == 403


async def test_admin_aprobado_por_g1_busca_sin_superadmin(api, sessions):
    async with sessions() as s:
        actor = await load_actor(s, api.farm["admin"])
    assert actor is not None and not actor.is_superadmin
    assert (await _search(api)).status_code == 200
