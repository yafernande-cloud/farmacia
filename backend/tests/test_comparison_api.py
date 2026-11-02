"""C3 — E5 (comparación de ofertas) y E9 (criterios comerciales y notas).

Todo se prueba contra PostgreSQL real con datos de `simulation`: precios y stock ficticios del
propio test. Se verifica lo que el contrato exige: elegibilidad por candidato, totales solo cuando
la cobertura es completa, cotas inferiores separadas de los totales, y que una anotación comercial
nunca cambie un número.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.api.deps import get_session
from app.core.config import get_settings
from app.main import create_app
from app.models import (
    AppUser,
    ComparisonCandidate,
    ComparisonLine,
    ComparisonRun,
    ComparisonSupplierTotal,
    OfferSnapshot,
    Organization,
    ShippingTermsSnapshot,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
)

pytestmark = pytest.mark.db

NOW = datetime.now(UTC) - timedelta(hours=1)


def _attrs(ingredient: str, content: tuple[str, str]) -> dict:
    return {
        "schema_version": 2,
        "unassigned": [],
        "ingredient": ingredient,
        "form": {"value": "comprimido", "basis": "explicit", "origin": "text"},
        "content": {
            "basis": "explicit",
            "origin": "text",
            "measure": {"value": content[0], "unit": content[1]},
        },
    }


@pytest.fixture
async def farm(sessions):
    """Tres proveedores activos con perfiles distintos, uno inactivo y uno sin observaciones."""
    ids: dict = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia C3", kind="simulation")
        other = Organization(name="Otra C3", kind="simulation")
        s.add_all([org, other])
        await s.flush()
        ids["org"], ids["other_org"] = org.id, other.id

        admin = AppUser(organization_id=org.id, display_name="Admin", roles=["admin"])
        aux = AppUser(organization_id=org.id, display_name="Auxiliar", roles=["auxiliary"])
        outsider = AppUser(organization_id=other.id, display_name="Externo", roles=["admin"])
        s.add_all([admin, aux, outsider])
        await s.flush()
        ids.update(admin=admin.id, aux=aux.id, outsider=outsider.id)

        norte = Supplier(organization_id=org.id, name="Lista Ficticia Norte", data_origin="simulated")
        sur = Supplier(organization_id=org.id, name="Lista Ficticia Sur", data_origin="simulated")
        centro = Supplier(organization_id=org.id, name="Lista Ficticia Centro", data_origin="simulated")
        inactivo = Supplier(
            organization_id=org.id,
            name="Lista Ficticia Inactiva",
            status="inactive",
            data_origin="simulated",
        )
        sin_datos = Supplier(organization_id=org.id, name="Proveedor Sin Datos", data_origin="simulated")
        s.add_all([norte, sur, centro, inactivo, sin_datos])
        await s.flush()
        ids["suppliers"] = {
            "norte": norte.id,
            "sur": sur.id,
            "centro": centro.id,
            "inactivo": inactivo.id,
            "sin_datos": sin_datos.id,
        }

        def _source(supplier: Supplier, *, enabled: bool = True, suffix: str = "") -> Source:
            return Source(
                organization_id=org.id,
                kind="supplier",
                supplier_id=supplier.id,
                name=f"Fuente {supplier.name}{suffix}",
                mode="manual_import",
                capabilities=["search", "price", "stock"],
                enabled=enabled,
                last_success_at=NOW,
                data_origin="simulated",
            )

        src_norte, src_sur, src_centro, src_inactivo, src_vacio = (
            _source(norte),
            _source(sur),
            _source(centro),
            _source(inactivo),
            _source(sin_datos),
        )
        disabled = _source(sur, enabled=False, suffix=" (deshabilitada)")
        chain = Source(
            organization_id=org.id,
            kind="chain_reference",
            name="Cadena de referencia",
            mode="none",
            capabilities=[],
            data_origin="simulated",
        )
        s.add_all([src_norte, src_sur, src_centro, src_inactivo, src_vacio, disabled, chain])
        await s.flush()
        ids["sources"] = {
            "norte": src_norte.id,
            "sur": src_sur.id,
            "centro": src_centro.id,
            "inactivo": src_inactivo.id,
            "vacio": src_vacio.id,
            "chain": chain.id,
        }

        # Despacho tipado: umbral en Norte, fijo en Sur y desconocido en Centro.
        s.add_all(
            [
                ShippingTermsSnapshot(
                    organization_id=org.id,
                    supplier_id=norte.id,
                    source_id=src_norte.id,
                    scheme="free_over_threshold",
                    flat_amount="2990.00",
                    threshold_amount="50000.00",
                    currency="CLP",
                    observed_at=NOW,
                    data_origin="simulated",
                ),
                ShippingTermsSnapshot(
                    organization_id=org.id,
                    supplier_id=sur.id,
                    source_id=src_sur.id,
                    scheme="flat",
                    flat_amount="3990.00",
                    currency="CLP",
                    observed_at=NOW,
                    data_origin="simulated",
                ),
            ]
        )

        a = TradeItem(
            organization_id=org.id,
            display_name="Ibuprofeno 400 mg x 20 comprimidos",
            identity_key="fixture-c3-ibuprofeno-400-20",
            ingredient="ibuprofeno",
            attributes=_attrs("ibuprofeno", ("20", "un")),
            data_origin="simulated",
        )
        b = TradeItem(
            organization_id=org.id,
            display_name="Paracetamol 500 mg x 20 comprimidos",
            identity_key="fixture-c3-paracetamol-500-20",
            ingredient="paracetamol",
            attributes=_attrs("paracetamol", ("20", "un")),
            data_origin="simulated",
        )
        c = TradeItem(
            organization_id=org.id,
            display_name="Losartán 50 mg x 30 comprimidos",
            identity_key="fixture-c3-losartan-50-30",
            ingredient="losartán",
            attributes=_attrs("losartán", ("30", "un")),
            data_origin="simulated",
        )
        s.add_all([a, b, c])
        await s.flush()
        ids["items"] = {"a": a.id, "b": b.id, "c": c.id}

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

        offers = [
            # Norte: mínimo 1 y múltiplo 7 → 10 pedidas se compran en 14.
            _offer(
                norte,
                src_norte,
                a,
                "N-1",
                "2000.00",
                minimum_quantity="10",
                multiple_quantity="7",
            ),
            _offer(norte, src_norte, b, "N-2", "1000.00"),
            # Sur: solo cubre la primera línea; precio sin impuestos incluidos.
            _offer(
                sur,
                src_sur,
                a,
                "S-1",
                "2200.00",
                tax_included=False,
                taxes=[{"kind": "iva", "rate_percent": "19", "included_in_price": False}],
            ),
            # Centro: cubre ambas líneas con precios más bajos, sin despacho observado.
            _offer(centro, src_centro, a, "C-1", "1900.00"),
            _offer(centro, src_centro, b, "C-2", "900.00"),
            # Inactivo: causa dura → descartado de totales y cotas.
            _offer(inactivo, src_inactivo, a, "I-1", "999.00"),
        ]
        s.add_all(offers)
        await s.flush()

        mappings = [
            SupplierProductMapping(
                organization_id=org.id,
                source_id=o.source_id,
                supplier_id=o.supplier_id,
                trade_item_id=o.trade_item_id,
                external_id=o.external_id,
                data_origin="simulated",
            )
            for o in offers
        ]
        s.add_all(mappings)
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


def H(api, who="admin"):
    return {"X-Demo-User-Id": str(api.farm[who])}


def line(api, key="a", value="10", unit="box") -> dict:
    return {"article_id": str(api.farm["items"][key]), "quantity": {"value": value, "unit": unit}}


async def _compare(api, lines, **extra):
    return await api.post("/api/v1/comparisons", json={"lines": lines, **extra}, headers=H(api))


def _supplier(payload: dict, supplier_id) -> dict:
    for row in payload["supplier_totals"]:
        if str(row["supplier_id"]) == str(supplier_id):
            return row
    raise AssertionError(f"proveedor {supplier_id} ausente de supplier_totals")


# --------------------------------------------------------------------------- E5: cálculo


async def test_e5_total_completo_y_cota_inferior_no_se_confunden(api):
    r = await _compare(api, [line(api, "a"), line(api, "b", "5")])
    assert r.status_code == 200, r.text
    data = r.json()

    # Hay un total completo (Norte) y otros dos proveedores parciales.
    assert data["status"] == "partial_coverage"
    assert data["scope"]["suppliers_complete"] == 1
    assert data["scope"]["suppliers_evaluated"] == 4  # 3 en totales + 1 descartado
    assert data["scope"]["suppliers_incomplete"] == 3
    # Fuentes: 5 de proveedor habilitadas con `search` (la deshabilitada y la de cadena no cuentan).
    assert data["scope"]["sources_consulted"] == 5

    norte = _supplier(data, api.farm["suppliers"]["norte"])
    assert norte["completeness"] == "complete"
    assert norte["blockers"] == []
    # 10 pedidas → mínimo 10 no eleva, múltiplo 7 → 14 compradas; 14 × 2000 = 28000.
    primera = next(ln for ln in data["lines"] if ln["article_id"] == str(api.farm["items"]["a"]))
    candidato = next(
        c for c in primera["candidates"]
        if c["supplier"]["supplier_id"] == str(api.farm["suppliers"]["norte"])
    )
    assert candidato["quantity"]["base"] == "10"
    assert candidato["quantity"]["purchased"] == "14"
    assert candidato["quantity"]["surplus"] == "4"
    assert candidato["quantity"]["adjustment"] == "multiple"
    assert candidato["line_gross"]["amount"] == "28000.00"
    # 28000 + (5 × 1000) = 33000, por debajo del umbral 50000 → despacho 2990.
    assert norte["subtotal"]["amount"] == "33000.00"
    assert norte["shipping"]["amount"]["amount"] == "2990.00"
    assert norte["total"]["amount"] == "35990.00"
    assert norte["lower_bound"]["amount"] == "35990.00"

    sur = _supplier(data, api.farm["suppliers"]["sur"])
    assert sur["completeness"] == "partial"
    assert sur["total"] is None
    assert len(sur["lines_missing"]) == 1
    # Sin cobertura completa no hay cota: no se puede afirmar que sea más barato.
    assert sur["lower_bound"] is None
    assert sur["bound_status"] == "not_boundable"
    assert "source_failed" in sur["bound_invalid_reasons"]

    centro = _supplier(data, api.farm["suppliers"]["centro"])
    assert centro["completeness"] == "partial"
    assert centro["total"] is None
    assert "shipping_unknown" in centro["blockers"]
    # Impuestos y cargos desconocidos suman 0: la cota (23500) sí es válida y queda bajo el ganador.
    assert centro["lower_bound"]["amount"] == "23500.00"
    assert centro["bound_status"] == "valid"

    # El ganador es solo el menor total COMPLETO verificado, nunca una cota.
    assert data["lowest_verified_total"]["total"]["amount"] == "35990.00"
    assert data["lowest_verified_total"]["is_global_claim"] is False
    assert data["lowest_verified_total"]["claim"] == "lowest_verified_total_among_evaluated"
    assert [str(x["supplier_id"]) for x in data["could_be_beaten_by"]] == [
        str(api.farm["suppliers"]["centro"])
    ]
    assert [str(x["supplier_id"]) for x in data["unknown_relation"]] == [
        str(api.farm["suppliers"]["sur"])
    ]
    # v0 no recomienda: el campo está reservado.
    assert data["recommendation"] is None
    assert data["recommendation_status"] == "not_evaluated"


async def test_e5_impuestos_no_incluidos_suman_al_pago_de_la_linea(api):
    r = await _compare(api, [line(api, "a")])
    assert r.status_code == 200, r.text
    data = r.json()
    primera = data["lines"][0]
    sur = next(
        c for c in primera["candidates"] if c["supplier"]["supplier_id"] == str(api.farm["suppliers"]["sur"])
    )
    assert sur["offer_price"]["tax_included"] is False
    assert sur["line_net"]["amount"] == "22000.00"
    assert sur["line_tax_added"]["amount"] == "4180.00"
    assert sur["line_payable"]["amount"] == "26180.00"


async def test_e5_descartado_por_causa_dura_y_candidatos_visibles(api):
    r = await _compare(api, [line(api, "a")])
    assert r.status_code == 200, r.text
    data = r.json()
    descartado = next(
        d for d in data["discarded_suppliers"]
        if d["supplier_id"] == str(api.farm["suppliers"]["inactivo"])
    )
    assert descartado["causes"] == ["supplier_inactive"]
    # Descartado no participa de totales ni de cotas (AC18).
    assert all(
        row["supplier_id"] != str(api.farm["suppliers"]["inactivo"])
        for row in data["supplier_totals"]
    )
    candidatos = data["lines"][0]["candidates"]
    inactivo = next(
        c for c in candidatos if c["supplier"]["supplier_id"] == str(api.farm["suppliers"]["inactivo"])
    )
    # La oferta ineligible se devuelve con sus causas, nunca se filtra.
    assert inactivo["eligibility"] == "ineligible"
    assert inactivo["line_payable"] is None


async def test_e5_sin_cobertura_no_hay_total(api):
    r = await _compare(api, [line(api, "c")])
    assert r.status_code == 200, r.text
    data = r.json()
    # Ninguna fuente entregó una oferta para ese artículo: no hay datos que comparar.
    assert data["status"] == "failed"
    assert data["lowest_verified_total"] is None
    assert data["lines"][0]["completeness"] == "unavailable"
    assert data["supplier_totals"] == []
    assert data["discarded_suppliers"] == []


async def test_e5_valida_cantidad_y_unidad(api):
    r = await _compare(api, [line(api, "a", "2.5")])
    assert r.status_code == 422
    assert r.json()["error"]["code"] in ("INVALID_QUANTITY", "QUANTITY_UNKNOWN")
    r = await _compare(api, [line(api, "a", "3", "litro")])
    assert r.status_code == 422
    r = await _compare(api, [])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = await api.post("/api/v1/comparisons", json={"lines": [line(api, "a")]}, headers=H(api, "aux"))
    assert r.status_code == 403


async def test_e5_guarda_la_corrida_completa(api, sessions):
    r = await _compare(api, [line(api, "a"), line(api, "b", "5")])
    assert r.status_code == 200, r.text
    comparison_id = r.json()["comparison_id"]
    async with sessions() as s:
        run = await s.get(ComparisonRun, uuid.UUID(comparison_id))
        assert run.status == "partial_coverage"
        assert run.data_origin == "simulated"
        assert await s.scalar(
            select(func.count()).select_from(ComparisonLine).where(
                ComparisonLine.comparison_run_id == run.id
            )
        ) == 2
        assert await s.scalar(
            select(func.count()).select_from(ComparisonCandidate).where(
                ComparisonCandidate.comparison_run_id == run.id
            )
        ) == 6  # línea 1: Norte, Sur, Centro e Inactivo; línea 2: Norte y Centro
        assert await s.scalar(
            select(func.count()).select_from(ComparisonSupplierTotal).where(
                ComparisonSupplierTotal.comparison_run_id == run.id
            )
        ) == 3


async def test_e5_un_dato_desconocido_no_se_convierte_en_cero(api, sessions):
    """Mínimo y múltiplo desconocidos: la cantidad no se rellena con 1 y el proveedor no es completo."""
    async with sessions() as s, s.begin():
        org = api.farm["org"]
        item = await s.get(TradeItem, api.farm["items"]["a"])
        supplier = await s.get(Supplier, api.farm["suppliers"]["sur"])
        offer = OfferSnapshot(
            organization_id=org,
            supplier_id=supplier.id,
            source_id=api.farm["sources"]["sur"],
            trade_item_id=item.id,
            external_id="S-2",
            price_amount="2200.00",
            currency="CLP",
            sale_unit="box",
            stock_status="unknown",
            tax_included=True,
            discount_included=True,
            minimum_known=False,
            multiple_known=False,
            observed_at=NOW,
            data_origin="simulated",
        )
        s.add(offer)
        await s.flush()
        s.add(
            SupplierProductMapping(
                organization_id=org,
                source_id=offer.source_id,
                supplier_id=supplier.id,
                trade_item_id=item.id,
                external_id=offer.external_id,
                data_origin="simulated",
            )
        )
    r = await _compare(api, [line(api, "a")])
    assert r.status_code == 200, r.text
    data = r.json()
    sur = _supplier(data, api.farm["suppliers"]["sur"])
    assert sur["completeness"] == "partial"
    assert "minimum_unknown" in sur["blockers"]
    assert "multiple_unknown" in sur["blockers"]
    assert sur["total"] is None


async def test_e5_sin_vinculo_explicito_no_se_compara(api, sessions):
    """Una oferta sin `supplier_product_mapping` no se puede atribuir a este artículo (D-076)."""
    async with sessions() as s, s.begin():
        org = api.farm["org"]
        supplier = await s.get(Supplier, api.farm["suppliers"]["sur"])
        offer = OfferSnapshot(
            organization_id=org,
            supplier_id=supplier.id,
            source_id=api.farm["sources"]["sur"],
            trade_item_id=api.farm["items"]["a"],
            external_id="S-3",
            price_amount="1500.00",
            currency="CLP",
            sale_unit="box",
            stock_status="known",
            stock_quantity="50",
            stock_unit="box",
            tax_included=True,
            discount_included=True,
            minimum_known=True,
            minimum_quantity="1",
            minimum_unit="box",
            multiple_known=True,
            multiple_quantity="1",
            multiple_unit="box",
            observed_at=NOW,
            data_origin="simulated",
        )
        s.add(offer)
        await s.flush()
        offer_id = offer.id
    r = await _compare(api, [line(api, "a")])
    assert r.status_code == 200, r.text
    candidato = next(
        c for c in r.json()["lines"][0]["candidates"] if c["offer_id"] == str(offer_id)
    )
    assert candidato["eligibility"] == "ineligible"
    assert candidato["causes"] == ["identity_mismatch"]
    assert candidato["line_payable"] is None


async def test_e5_una_comparacion_no_se_edita(api, sessions):
    r = await _compare(api, [line(api, "a")])
    assert r.status_code == 200, r.text
    comparison_id = r.json()["comparison_id"]
    async with sessions() as s:
        with pytest.raises(DBAPIError):
            await s.execute(
                text("UPDATE comparison_run SET status='failed' WHERE id=:i"),
                {"i": comparison_id},
            )
            await s.commit()
        await s.rollback()


# --------------------------------------------------------------------------- E9: criterios


def _criterion(**body) -> dict:
    base = {
        "kind": "note",
        "target": {"level": "supplier"},
        "reason_text": "Entrega incompleta en la última compra.",
    }
    base.update(body)
    return base


async def test_e9_nota_no_confirma_nada(api):
    r = await api.post(
        "/api/v1/commercial-criteria",
        json=_criterion(target={"level": "supplier", "supplier_id": str(api.farm["suppliers"]["sur"])}),
        headers=H(api),
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["kind"] == "note"
    assert data["effect"] == "note_only"
    assert data["confirmed_by"] is None and data["confirmed_at"] is None
    assert data["version"] == 1


async def test_e9_criterio_exige_confirmacion_explicita(api):
    body = _criterion(
        kind="criterion",
        effect="do_not_recommend",
        target={"level": "supplier", "supplier_id": str(api.farm["suppliers"]["centro"])},
    )
    r = await api.post("/api/v1/commercial-criteria", json=body, headers=H(api))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "CRITERION_INCOMPLETE"

    r = await api.post("/api/v1/commercial-criteria", json={**body, "confirm": True}, headers=H(api))
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["origin"] == "explicit"
    assert data["confirmed_by"] == str(api.farm["admin"])

    r = await api.post(
        "/api/v1/commercial-criteria", json=_criterion(), headers=H(api, "aux")
    )
    assert r.status_code == 403


async def test_e9_la_anotacion_no_altera_ningun_numero(api):
    antes = (await _compare(api, [line(api, "a"), line(api, "b", "5")])).json()
    r = await api.post(
        "/api/v1/commercial-criteria",
        json=_criterion(
            kind="criterion",
            confirm=True,
            effect="do_not_recommend",
            target={"level": "supplier", "supplier_id": str(api.farm["suppliers"]["centro"])},
        ),
        headers=H(api),
    )
    assert r.status_code == 201, r.text
    despues = (await _compare(api, [line(api, "a"), line(api, "b", "5")])).json()

    centro_antes = _supplier(antes, api.farm["suppliers"]["centro"])
    centro_despues = _supplier(despues, api.farm["suppliers"]["centro"])
    assert centro_antes["annotations"] == []
    assert [a["effect"] for a in centro_despues["annotations"]] == ["do_not_recommend"]
    for field in ("subtotal", "lower_bound", "total", "completeness", "blockers", "bound_status"):
        assert centro_antes[field] == centro_despues[field]
    # El ganador y las cotas tampoco cambian.
    assert antes["lowest_verified_total"] == despues["lowest_verified_total"]
    assert len(antes["could_be_beaten_by"]) == len(despues["could_be_beaten_by"])


async def test_e9_confirmar_nota_crea_otro_registro_y_desactiva_la_nota(api):
    r = await api.post(
        "/api/v1/commercial-criteria",
        json=_criterion(target={"level": "organization"}),
        headers=H(api),
    )
    assert r.status_code == 201, r.text
    nota = r.json()

    r = await api.post(
        f"/api/v1/commercial-criteria/{nota['criterion_id']}/confirm",
        json={"effect": "requires_review"},
        headers={**H(api), "If-Match": f'"{nota["version"]}"'},
    )
    assert r.status_code == 201, r.text
    criterio = r.json()
    assert criterio["kind"] == "criterion"
    assert criterio["source"]["kind"] == "note"
    assert criterio["source"]["note_criterion_id"] == nota["criterion_id"]
    assert criterio["source"]["note_version"] == nota["version"]

    nota_guardada = (
        await api.get(f"/api/v1/commercial-criteria/{nota['criterion_id']}", headers=H(api))
    ).json()
    assert nota_guardada["status"] == "inactive"
    assert nota_guardada["reason_text"] == nota["reason_text"]  # la nota no se muta
    assert nota_guardada["deactivation_reason"] == f"convertida en criterio {criterio['criterion_id']}"

    # Un segundo `confirm` sobre la misma nota choca con la restricción única.
    r = await api.post(
        f"/api/v1/commercial-criteria/{nota['criterion_id']}/confirm",
        json={"effect": "prefer"},
        headers={**H(api), "If-Match": f'"{nota_guardada["version"]}"'},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CRITERION_NOT_ACTIVE"


async def test_e9_version_y_baja_con_motivo(api):
    r = await api.post(
        "/api/v1/commercial-criteria",
        json=_criterion(target={"level": "organization"}),
        headers=H(api),
    )
    criterion_id = r.json()["criterion_id"]

    r = await api.patch(
        f"/api/v1/commercial-criteria/{criterion_id}",
        json={"reason_text": "Actualizado"},
        headers=H(api),
    )
    assert r.status_code == 428
    assert r.json()["error"]["code"] == "VERSION_REQUIRED"

    r = await api.patch(
        f"/api/v1/commercial-criteria/{criterion_id}",
        json={"reason_text": "Actualizado"},
        headers={**H(api), "If-Match": '"2"'},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "VERSION_CONFLICT"

    r = await api.patch(
        f"/api/v1/commercial-criteria/{criterion_id}",
        json={"reason_text": "Actualizado"},
        headers={**H(api), "If-Match": '"1"'},
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2

    r = await api.post(
        f"/api/v1/commercial-criteria/{criterion_id}/reactivate",
        headers={**H(api), "If-Match": '"2"'},
    )
    assert r.status_code == 409  # ya está activa

    r = await api.post(
        f"/api/v1/commercial-criteria/{criterion_id}/deactivate",
        json={"reason": "Ya no aplica"},
        headers={**H(api), "If-Match": '"2"'},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "inactive"
    assert r.json()["deactivation_reason"] == "Ya no aplica"
    assert r.json()["version"] == 3

    r = await api.post(
        f"/api/v1/commercial-criteria/{criterion_id}/reactivate",
        headers={**H(api), "If-Match": '"3"'},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    assert r.json()["version"] == 4


async def test_e9_lista_solo_activas_por_defecto(api):
    r = await api.post(
        "/api/v1/commercial-criteria",
        json=_criterion(target={"level": "organization"}),
        headers=H(api),
    )
    nota = r.json()
    await api.post(
        f"/api/v1/commercial-criteria/{nota['criterion_id']}/deactivate",
        json={"reason": "Vencida"},
        headers={**H(api), "If-Match": f'"{nota["version"]}"'},
    )
    activas = (await api.get("/api/v1/commercial-criteria", headers=H(api))).json()
    assert activas["criteria"] == []
    todas = (
        await api.get("/api/v1/commercial-criteria?include_inactive=true", headers=H(api))
    ).json()
    assert [c["criterion_id"] for c in todas["criteria"]] == [nota["criterion_id"]]
