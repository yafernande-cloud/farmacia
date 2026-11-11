"""A6 Fase 2 — motor determinista de preferencias comerciales (O3/O4, D-142).

Se prueba contra PostgreSQL real con datos `simulation` del propio test. El motor **solo ordena**
las ofertas de un producto: no llama a OpenAI, no descarta ofertas, no cambia precios, stock,
cantidades, totales ni elegibilidad, y no se propaga a otros productos.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import get_session
from app.core.config import get_settings
from app.main import create_app
from app.models import (
    AppUser,
    CommercialArticlePreference,
    CommercialObservation,
    CommercialPreference,
    ComparisonRun,
    ObservationInterpretation,
    OfferSnapshot,
    Organization,
    PreferenceOrderTrace,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
)
from app.services import preferences as preferences_svc

pytestmark = pytest.mark.db

NOW = datetime.now(UTC) - timedelta(minutes=5)
BASE_TIME = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _attrs(ingredient: str, content: str) -> dict:
    return {
        "schema_version": 2,
        "unassigned": [],
        "ingredient": ingredient,
        "form": {"value": "comprimido", "basis": "explicit", "origin": "text"},
        "content": {
            "basis": "explicit",
            "origin": "text",
            "measure": {"value": content, "unit": "un"},
        },
    }


@pytest.fixture
async def farm(sessions):
    ids: dict = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia A6 motor", kind="simulation")
        other = Organization(name="Otra A6 motor", kind="simulation")
        s.add_all([org, other])
        await s.flush()
        ids["org"], ids["other_org"] = org.id, other.id
        admin = AppUser(organization_id=org.id, display_name="Admin", roles=["admin"])
        outsider = AppUser(organization_id=other.id, display_name="Externo", roles=["admin"])
        s.add_all([admin, outsider])
        await s.flush()
        ids["admin"], ids["outsider"] = admin.id, outsider.id

        suppliers = {
            name: Supplier(
                organization_id=org.id, name=f"Proveedor {name}", data_origin="simulated"
            )
            for name in ("Norte", "Sur", "Centro")
        }
        s.add_all(suppliers.values())
        await s.flush()
        sources = {}
        for key, supplier in suppliers.items():
            source = Source(
                organization_id=org.id,
                kind="supplier",
                supplier_id=supplier.id,
                name=f"Lista {key}",
                mode="manual_import",
                capabilities=["search", "price", "stock"],
                last_success_at=NOW,
                data_origin="simulated",
            )
            s.add(source)
            sources[key] = source
        await s.flush()

        items = {
            "a": TradeItem(
                organization_id=org.id,
                display_name="Losartán 50 mg x 20 comprimidos",
                identity_key="fixture-a6-losartan-50-20",
                ingredient="losartán",
                attributes=_attrs("losartán", "20"),
                data_origin="simulated",
            ),
            "b": TradeItem(
                organization_id=org.id,
                display_name="Losartán 100 mg x 20 comprimidos",
                identity_key="fixture-a6-losartan-100-20",
                ingredient="losartán",
                attributes=_attrs("losartán", "20"),
                data_origin="simulated",
            ),
            "c": TradeItem(
                organization_id=org.id,
                display_name="Enalapril 10 mg x 1 comprimido",
                identity_key="fixture-a6-enalapril-10-1",
                ingredient="enalapril",
                attributes=_attrs("enalapril", "1"),
                data_origin="simulated",
            ),
            "d": TradeItem(
                organization_id=org.id,
                display_name="Amoxicilina 500 mg x 20 comprimidos",
                identity_key="fixture-a6-amoxicilina-500-20",
                ingredient="amoxicilina",
                attributes=_attrs("amoxicilina", "20"),
                data_origin="simulated",
            ),
        }
        s.add_all(items.values())
        await s.flush()

        def _offer(supplier, source, item, external_id, price, *, pack="20", **kw) -> OfferSnapshot:
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
                "pack_units_per_commercial_unit": pack,
                "pack_base_unit": "un",
                "pack_text": f"caja x {pack}",
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
            _offer(
                suppliers["Norte"],
                sources["Norte"],
                items["a"],
                "A-N",
                "2000.00",
                laboratory_as_published="Laboratorio Chile",
                brand_as_published="Marca Alfa",
            ),
            _offer(
                suppliers["Sur"],
                sources["Sur"],
                items["a"],
                "A-S",
                "2250.00",
                laboratory_as_published="Laboratorio Andino",
                brand_as_published="Marca Beta",
            ),
            _offer(
                suppliers["Centro"],
                sources["Centro"],
                items["a"],
                "A-C",
                "2100.00",
                laboratory_as_published="Laboratorio Chile",
                brand_as_published="Marca Beta",
            ),
            _offer(
                suppliers["Norte"],
                sources["Norte"],
                items["b"],
                "B-N",
                "3000.00",
                laboratory_as_published="Laboratorio Chile",
                brand_as_published="Marca Alfa",
            ),
            _offer(
                suppliers["Sur"],
                sources["Sur"],
                items["b"],
                "B-S",
                "2900.00",
                laboratory_as_published="Laboratorio Andino",
                brand_as_published="Marca Beta",
            ),
            # c: el precio declarado y el pagable no coinciden (Sur no incluye impuestos).
            _offer(
                suppliers["Norte"],
                sources["Norte"],
                items["c"],
                "C-N",
                "2000.00",
                pack="1",
                laboratory_as_published="Laboratorio Chile",
            ),
            _offer(
                suppliers["Sur"],
                sources["Sur"],
                items["c"],
                "C-S",
                "1900.00",
                pack="1",
                laboratory_as_published="Laboratorio Andino",
                tax_included=False,
                taxes=[{"kind": "iva", "rate_percent": "19", "included_in_price": False}],
            ),
            _offer(
                suppliers["Centro"],
                sources["Centro"],
                items["c"],
                "C-C",
                "2100.00",
                pack="1",
                laboratory_as_published="Laboratorio Chile",
            ),
            # d: disponibilidad informada en una oferta y desconocida en la más barata.
            _offer(
                suppliers["Norte"],
                sources["Norte"],
                items["d"],
                "D-N",
                "1500.00",
                stock_status="known",
                stock_quantity="30",
            ),
            _offer(
                suppliers["Sur"],
                sources["Sur"],
                items["d"],
                "D-S",
                "1000.00",
                stock_status="unknown",
                stock_quantity=None,
                stock_unit=None,
            ),
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
        await s.flush()

        ids["suppliers"] = {k: v.id for k, v in suppliers.items()}
        ids["items"] = {k: v.id for k, v in items.items()}
        ids["offers"] = {f"{o.external_id}": o.id for o in offers}
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


async def _interpret(
    sessions,
    farm,
    item_key: str,
    *,
    status: str = "suggested",
    created_at: datetime | None = None,
    preferences: Sequence[dict] = (),
    retired: Sequence[str] = (),
) -> uuid.UUID:
    """Crea una interpretación semántica y sus preferencias, como las dejaría O2 (sin OpenAI)."""
    async with sessions() as s, s.begin():
        obs = CommercialObservation(
            organization_id=farm["org"],
            text_original="preferencia de prueba",
            author_id=farm["admin"],
            trade_item_id=farm["items"][item_key],
            data_origin="simulated",
        )
        s.add(obs)
        await s.flush()
        kwargs: dict = {"data_origin": "simulated"}
        if status == "failed":
            kwargs["error_code"] = "timeout"
        if status in ("confirmed", "rejected"):
            kwargs.update(reviewed_by=farm["admin"], reviewed_at=created_at or BASE_TIME)
        interp = ObservationInterpretation(
            organization_id=farm["org"],
            observation_id=obs.id,
            trade_item_id=farm["items"][item_key],
            status=status,
            model="test-model",
            prompt_version="test-v1",
            created_at=created_at or BASE_TIME,
            retired_subject_kinds=list(retired),
            **kwargs,
        )
        s.add(interp)
        await s.flush()
        pref_status = {
            "suggested": "suggested",
            "confirmed": "confirmed",
            "rejected": "rejected",
            "needs_review": "needs_review",
        }.get(status, "needs_review")
        for spec in preferences:
            s.add(
                CommercialPreference(
                    organization_id=farm["org"],
                    interpretation_id=interp.id,
                    observation_id=obs.id,
                    trade_item_id=farm["items"][item_key],
                    confidence="1.00",
                    status=pref_status,
                    data_origin="simulated",
                    **spec,
                )
            )
        await s.flush()
        return interp.id


def _lab(key: str, *, effect: str = "prefer", tolerance: str = "none", **extra) -> dict:
    spec = {
        "subject_kind": "laboratory",
        "subject_key": key,
        "subject_label": key,
        "effect": effect,
        "tolerance_kind": tolerance,
        "tolerance_origin": "not_applicable" if tolerance == "none" else "explicit",
    }
    spec.update(extra)
    return spec


def _price(kind: str, amount: str, basis: str, **extra) -> dict:
    spec = {
        "subject_kind": "price",
        "subject_key": "price",
        "subject_label": "precio",
        "effect": "prefer",
        "price_reference_kind": kind,
        "price_reference_amount": amount,
        "price_reference_currency": "CLP",
        "price_reference_basis": basis,
    }
    spec.update(extra)
    return spec


async def _article(api, key: str) -> list[str]:
    r = await api.get(f"/api/v1/articles/{api.farm['items'][key]}", headers=H(api))
    assert r.status_code == 200, r.text
    return [o["offer_id"] for o in r.json()["offers"]]


async def _compare(api, keys: Sequence[str] = ("a",), value: str = "10"):
    lines = [
        {"article_id": str(api.farm["items"][k]), "quantity": {"value": value, "unit": "box"}}
        for k in keys
    ]
    r = await api.post("/api/v1/comparisons", json={"lines": lines}, headers=H(api))
    assert r.status_code == 200, r.text
    return r.json()


def _line_candidates(payload: dict, api, key: str) -> list[dict]:
    line = next(ln for ln in payload["lines"] if ln["article_id"] == str(api.farm["items"][key]))
    return line["candidates"]




# --------------------------------------------------------------------------- E3: orden


async def test_e3_sin_preferencias_conserva_el_orden_actual(api):
    order = await _article(api, "a")
    assert order == sorted(order, reverse=True) or len(order) == 2  # ids aleatorios
    # El conjunto y los precios no dependen del orden.
    r = await api.get(f"/api/v1/articles/{api.farm['items']['a']}", headers=H(api))
    body = r.json()
    assert {o["offer_id"] for o in body["offers"]} == set(order)
    assert {o["price"]["money"]["amount"] for o in body["offers"]} == {
        "2000.00",
        "2250.00",
        "2100.00",
    }


async def test_e3_prefiere_laboratorio_del_producto_y_no_se_propaga(sessions, api):
    await _interpret(sessions, api.farm, "a", preferences=[_lab("laboratorio chile")])
    order_a = await _article(api, "a")
    norte, sur, centro = (
        str(api.farm["offers"]["A-N"]),
        str(api.farm["offers"]["A-S"]),
        str(api.farm["offers"]["A-C"]),
    )
    # Laboratorio Chile: Norte y Centro cumplen; Sur (Andino) queda después.
    assert order_a[-1] == sur
    assert set(order_a[:2]) == {norte, centro}

    # Producto b comparte laboratorio/marca/proveedor, pero NO hereda la preferencia.
    r = await api.get(f"/api/v1/articles/{api.farm['items']['b']}", headers=H(api))
    order_b = [o["offer_id"] for o in r.json()["offers"]]
    assert order_b == sorted(
        [str(api.farm["offers"]["B-N"]), str(api.farm["offers"]["B-S"])], reverse=True
    )

    # c tampoco cambia.
    r = await api.get(f"/api/v1/articles/{api.farm['items']['c']}", headers=H(api))
    order_c = [o["offer_id"] for o in r.json()["offers"]]
    assert order_c == sorted(order_c, reverse=True)


async def test_e3_precedencia_precio_antes_que_proveedor(sessions, api):
    await _interpret(
        sessions,
        api.farm,
        "c",
        preferences=[
            _price("maximum", "1950", "per_commercial_unit"),
            {
                "subject_kind": "supplier",
                "subject_key": "proveedor sur",
                "subject_label": "Proveedor Sur",
                "subject_supplier_id": api.farm["suppliers"]["Sur"],
                "effect": "prefer",
                "tolerance_kind": "none",
                "tolerance_origin": "not_applicable",
            },
        ],
    )
    order = await _article(api, "c")
    # Sur es el único dentro del máximo (precio declarado 1900) aunque el proveedor preferido
    # también sea Sur: el precio va primero y Sur ya estaba primero por precio pagable.
    assert order[0] == str(api.farm["offers"]["C-S"])

    # Si se prefiere otro proveedor, el máximo igual lo deja detrás: precio antes que proveedor.
    await _interpret(
        sessions,
        api.farm,
        "c",
        created_at=BASE_TIME + timedelta(hours=1),
        preferences=[
            _price("maximum", "1950", "per_commercial_unit"),
            {
                "subject_kind": "supplier",
                "subject_key": "proveedor norte",
                "subject_label": "Proveedor Norte",
                "subject_supplier_id": api.farm["suppliers"]["Norte"],
                "effect": "prefer",
                "tolerance_kind": "none",
                "tolerance_origin": "not_applicable",
            },
        ],
    )
    order = await _article(api, "c")
    assert order[0] == str(api.farm["offers"]["C-S"])  # cumple el máximo
    # Norte es el proveedor preferido, pero está fuera del máximo: el precio manda.
    assert order[1] == str(api.farm["offers"]["C-N"])
    assert order[2] == str(api.farm["offers"]["C-C"])


async def test_e3_tolerancia_implicita_y_explicita(sessions, api):
    baseline = await _article(api, "a")
    sur = str(api.farm["offers"]["A-S"])

    # 10 % implícito: 2250 vs 2000 = 12,5 % → no alcanza, Sur queda al final.
    await _interpret(
        sessions,
        api.farm,
        "a",
        preferences=[
            _lab(
                "laboratorio chile",
                tolerance="relative_percent",
                tolerance_percent="10",
                tolerance_origin="implicit_default",
            )
        ],
    )
    assert (await _article(api, "a"))[-1] == sur

    # 15 % explícito reemplaza al anterior: 2250 sí entra y se conserva el orden previo.
    await _interpret(
        sessions,
        api.farm,
        "a",
        created_at=BASE_TIME + timedelta(hours=1),
        preferences=[
            _lab(
                "laboratorio chile",
                tolerance="relative_percent",
                tolerance_percent="15",
                tolerance_origin="explicit",
            )
        ],
    )
    assert await _article(api, "a") == baseline

    # Monto absoluto en CLP: 250 de diferencia; 300 alcanza y 200 no.
    await _interpret(
        sessions,
        api.farm,
        "a",
        created_at=BASE_TIME + timedelta(hours=2),
        preferences=[
            _lab(
                "laboratorio chile",
                tolerance="absolute_amount",
                tolerance_amount="200",
                tolerance_currency="CLP",
            )
        ],
    )
    assert (await _article(api, "a"))[-1] == sur
    await _interpret(
        sessions,
        api.farm,
        "a",
        created_at=BASE_TIME + timedelta(hours=3),
        preferences=[
            _lab(
                "laboratorio chile",
                tolerance="absolute_amount",
                tolerance_amount="300",
                tolerance_currency="CLP",
            )
        ],
    )
    assert await _article(api, "a") == baseline


async def test_e3_interpretacion_rechazada_no_reactiva_la_anterior(sessions, api):
    await _interpret(
        sessions,
        api.farm,
        "a",
        status="confirmed",
        preferences=[_lab("laboratorio chile")],
    )
    sur = str(api.farm["offers"]["A-S"])
    assert (await _article(api, "a"))[-1] == sur

    # La más reciente de la clase es rechazada (preferir Andino): no se aplica ni se reactiva
    # «Laboratorio Chile», pero tampoco la desactiva: la preferencia confirmada sigue vigente.
    await _interpret(
        sessions,
        api.farm,
        "a",
        status="rejected",
        created_at=BASE_TIME + timedelta(hours=1),
        preferences=[_lab("laboratorio andino")],
    )
    assert (await _article(api, "a"))[-1] == sur

    # La lectura anterior dejó su traza; la rechazada no cambia las preferencias aplicadas, así
    # que el estado auditado es el mismo y no se duplica la fila.
    async with sessions() as s:
        traces = (await s.execute(select(PreferenceOrderTrace))).scalars().all()
    assert len(traces) == 1
    assert traces[0].preference_ids


async def test_e3_needs_review_no_se_aplica_y_failed_no_desplaza(sessions, api):
    await _interpret(
        sessions,
        api.farm,
        "a",
        status="confirmed",
        preferences=[_lab("laboratorio chile")],
    )
    sur = str(api.farm["offers"]["A-S"])
    assert (await _article(api, "a"))[-1] == sur

    # Un intento fallido no es semántico: no desplaza la preferencia válida.
    await _interpret(
        sessions, api.farm, "a", status="failed", created_at=BASE_TIME + timedelta(hours=1)
    )
    assert (await _article(api, "a"))[-1] == sur

    # `needs_review` es una interpretación no aplicada: no se usa ni desactiva la preferencia
    # vigente. La ambigüedad no puede tener efectos comerciales.
    await _interpret(
        sessions,
        api.farm,
        "a",
        status="needs_review",
        created_at=BASE_TIME + timedelta(hours=2),
        preferences=[_lab("laboratorio andino")],
    )
    assert (await _article(api, "a"))[-1] == sur


async def test_e3_no_preference_no_desactiva_la_vigente(sessions, api):
    await _interpret(
        sessions,
        api.farm,
        "a",
        status="confirmed",
        preferences=[_lab("laboratorio chile")],
    )
    sur = str(api.farm["offers"]["A-S"])
    assert (await _article(api, "a"))[-1] == sur

    # «Sin preferencia» sin filas no puede adscribirse a una clase: no retira la vigente.
    await _interpret(
        sessions, api.farm, "a", status="no_preference", created_at=BASE_TIME + timedelta(hours=1)
    )
    assert (await _article(api, "a"))[-1] == sur

    # Tampoco la retira si, por un error de interpretación, dejara filas: no está aplicada.
    await _interpret(
        sessions,
        api.farm,
        "a",
        status="no_preference",
        created_at=BASE_TIME + timedelta(hours=2),
        preferences=[_lab("laboratorio andino")],
    )
    assert (await _article(api, "a"))[-1] == sur


async def test_e3_disponibilidad_preferida_sin_inventar_stock(sessions, api):
    await _interpret(
        sessions,
        api.farm,
        "d",
        preferences=[
            {
                "subject_kind": "availability",
                "subject_key": "in_stock",
                "subject_label": "en stock",
                "effect": "prefer",
                "tolerance_kind": "none",
                "tolerance_origin": "not_applicable",
            }
        ],
    )
    order = await _article(api, "d")
    # D-S es más barata pero su stock es desconocido: no se trata como cero ni como stock seguro.
    assert order == [
        str(api.farm["offers"]["D-N"]),
        str(api.farm["offers"]["D-S"]),
    ]


# --------------------------------------------------------------------------- E5: orden


async def test_e5_sin_preferencias_el_resultado_es_identico(api):
    payload = await _compare(api, ("a", "c"))
    # Orden determinista anterior: elegibilidad, pagable y offer_id.
    for key in ("a", "c"):
        cands = _line_candidates(payload, api, key)
        ordered = sorted(
            cands,
            key=lambda p: (
                {"eligible": 0, "needs_review": 1, "ineligible": 2}[p["eligibility"]],
                p["line_payable"] is None,
                Decimal(p["line_payable"]["amount"]) if p["line_payable"] else Decimal(0),
                uuid.UUID(p["offer_id"]).hex,
            ),
        )
        assert [c["offer_id"] for c in cands] == [c["offer_id"] for c in ordered]


async def test_e5_preferencia_ordena_sin_cambiar_numeros_ni_conjunto(sessions, api):
    before = await _compare(api, ("a",))
    await _interpret(sessions, api.farm, "a", preferences=[_lab("laboratorio chile")])
    after = await _compare(api, ("a",))

    cands_before = _line_candidates(before, api, "a")
    cands_after = _line_candidates(after, api, "a")
    assert {c["offer_id"] for c in cands_before} == {c["offer_id"] for c in cands_after}
    assert cands_after[-1]["supplier"]["supplier_id"] == str(api.farm["suppliers"]["Sur"])

    by_id_before = {c["offer_id"]: c for c in cands_before}
    for cand in cands_after:
        old = by_id_before[cand["offer_id"]]
        assert cand["eligibility"] == old["eligibility"]
        assert cand["line_payable"] == old["line_payable"]
        assert cand["line_gross"] == old["line_gross"]
        assert cand["quantity"] == old["quantity"]
        assert cand["causes"] == old["causes"]
    # Los totales por proveedor y el conjunto de ofertas no dependen del orden. Cambian solo el
    # instante de cálculo y los ids de línea de cada corrida.
    fields = (
        "supplier_id",
        "currency",
        "completeness",
        "subtotal",
        "taxes_added",
        "discounts_total",
        "shipping",
        "total",
        "lower_bound",
        "bound_status",
        "blockers",
        "causes",
    )
    assert [
        {k: row[k] for k in fields} for row in before["supplier_totals"]
    ] == [{k: row[k] for k in fields} for row in after["supplier_totals"]]


async def test_e5_elegibilidad_prevalece_sobre_la_preferencia(sessions, api):
    # Nueva observación de la fuente Sur para el artículo `a`: cumple la preferencia de
    # laboratorio (Andino) y es la más barata, pero su presentación no corresponde al artículo.
    async with sessions() as s, s.begin():
        offer = OfferSnapshot(
            organization_id=api.farm["org"],
            supplier_id=api.farm["suppliers"]["Sur"],
            source_id=next(
                o.source_id
                for o in (
                    await s.execute(
                        select(OfferSnapshot).where(
                            OfferSnapshot.id == api.farm["offers"]["A-S"]
                        )
                    )
                ).scalars()
            ),
            trade_item_id=api.farm["items"]["a"],
            external_id="A-S",
            price_amount="100.00",
            currency="CLP",
            price_basis="per_commercial_unit",
            sale_unit="box",
            sale_unit_text="caja",
            pack_units_per_commercial_unit="10",
            pack_base_unit="un",
            pack_text="caja x 10",
            stock_status="known",
            stock_quantity="90",
            stock_unit="box",
            tax_included=True,
            discount_included=True,
            minimum_known=True,
            minimum_quantity="1",
            minimum_unit="box",
            multiple_known=True,
            multiple_quantity="1",
            multiple_unit="box",
            observed_at=NOW + timedelta(minutes=1),
            data_origin="simulated",
            laboratory_as_published="Laboratorio Andino",
            brand_as_published="Marca Beta",
        )
        s.add(offer)
        await s.flush()
        ineligible_id = offer.id

    await _interpret(sessions, api.farm, "a", preferences=[_lab("laboratorio andino")])
    cands = _line_candidates(await _compare(api, ("a",)), api, "a")
    by_id = {c["offer_id"]: c for c in cands}
    assert by_id[str(ineligible_id)]["eligibility"] == "ineligible"
    assert by_id[str(ineligible_id)]["causes"] == ["presentation_mismatch"]
    # La preferencia la cumpliría, pero la elegibilidad manda: las elegibles van primero y la
    # no elegible no adelanta a ninguna, aunque sea más barata y coincida con el laboratorio.
    assert cands[-1]["offer_id"] == str(ineligible_id)
    assert all(c["eligibility"] == "eligible" for c in cands[:-1])
    assert by_id[str(ineligible_id)]["line_payable"] is None
    assert len(cands) == 3


async def test_e5_tolerancia_no_cruza_grupos_de_elegibilidad(sessions, api):
    """Una oferta no elegible tampoco fija la tolerancia de las elegibles."""

    async def _copy(
        base_key: str, *, price: str, pack: str, laboratory: str, observed_at: datetime
    ) -> OfferSnapshot:
        original = (
            await s.execute(
                select(OfferSnapshot).where(OfferSnapshot.id == api.farm["offers"][base_key])
            )
        ).scalar_one()
        return OfferSnapshot(
            organization_id=original.organization_id,
            supplier_id=original.supplier_id,
            source_id=original.source_id,
            trade_item_id=original.trade_item_id,
            external_id=original.external_id,
            external_id_kind=original.external_id_kind,
            price_amount=price,
            currency=original.currency,
            price_basis=original.price_basis,
            sale_unit=original.sale_unit,
            sale_unit_text=original.sale_unit_text,
            pack_units_per_commercial_unit=pack,
            pack_base_unit=original.pack_base_unit,
            pack_text=original.pack_text,
            stock_status=original.stock_status,
            stock_quantity=original.stock_quantity,
            stock_unit=original.stock_unit,
            tax_included=original.tax_included,
            discount_included=original.discount_included,
            minimum_known=original.minimum_known,
            minimum_quantity=original.minimum_quantity,
            minimum_unit=original.minimum_unit,
            multiple_known=original.multiple_known,
            multiple_quantity=original.multiple_quantity,
            multiple_unit=original.multiple_unit,
            observed_at=observed_at,
            data_origin=original.data_origin,
            laboratory_as_published=laboratory,
            brand_as_published=original.brand_as_published,
        )

    later = NOW + timedelta(minutes=1)
    async with sessions() as s, s.begin():
        # A-N elegible: ahora es Andino a 3000 (cumple la preferencia).
        norte = await _copy(
            "A-N", price="3000.00", pack="20", laboratory="Laboratorio Andino", observed_at=later
        )
        # A-S no elegible (presentación distinta): Andino a 100. Es la más barata y cumple la
        # preferencia, pero no debe fijar la tolerancia de las ofertas elegibles.
        sur = await _copy(
            "A-S", price="100.00", pack="10", laboratory="Laboratorio Andino", observed_at=later
        )
        s.add_all([norte, sur])
        await s.flush()
        norte_id, sur_id = norte.id, sur.id

    await _interpret(
        sessions,
        api.farm,
        "a",
        preferences=[
            _lab(
                "laboratorio andino",
                tolerance="absolute_amount",
                tolerance_amount="100",
                tolerance_currency="CLP",
            )
        ],
    )
    cands = _line_candidates(await _compare(api, ("a",)), api, "a")
    by_id = {c["offer_id"]: c for c in cands}
    assert by_id[str(sur_id)]["eligibility"] == "ineligible"
    # La tolerancia se calcula solo con elegibles: A-C (Chile, 2100) queda dentro de los 100 CLP
    # sobre la base elegible de 3000 y, al empatar en la clase, el precio la pone primero. Si la
    # base no elegible de 100 se colara, A-C quedaría después de A-N.
    assert [c["offer_id"] for c in cands] == [
        str(api.farm["offers"]["A-C"]),
        str(norte_id),
        str(sur_id),
    ]


async def test_e5_precio_maximo_usa_la_base_declarada_y_no_oculta(api):
    payload = await _compare(api, ("c",))
    cands = _line_candidates(payload, api, "c")
    # Pagable: Norte 20000, Centro 21000 y Sur 22610 (sin impuestos incluidos).
    assert [c["supplier"]["supplier_id"] for c in cands] == [
        str(api.farm["suppliers"]["Norte"]),
        str(api.farm["suppliers"]["Centro"]),
        str(api.farm["suppliers"]["Sur"]),
    ]


async def test_e5_precio_maximo_ordena_por_precio_declarado(sessions, api):
    await _interpret(
        sessions,
        api.farm,
        "c",
        preferences=[_price("maximum", "1950", "per_commercial_unit")],
    )
    payload = await _compare(api, ("c",))
    cands = _line_candidates(payload, api, "c")
    assert len(cands) == 3  # ninguna oferta se oculta
    # Sur declara 1900 (aunque su pagable sea mayor por impuestos): cumple y va primero.
    assert cands[0]["supplier"]["supplier_id"] == str(api.farm["suppliers"]["Sur"])
    # Las demás siguen visibles con sus números intactos.
    assert cands[0]["line_payable"]["amount"] == "22610.00"


async def test_e5_precio_en_base_unitaria_y_total_de_linea(sessions, api):
    await _interpret(
        sessions,
        api.farm,
        "c",
        preferences=[_price("acceptable", "1950", "per_base_unit")],
    )
    cands = _line_candidates(await _compare(api, ("c",)), api, "c")
    assert cands[0]["supplier"]["supplier_id"] == str(api.farm["suppliers"]["Sur"])
    assert len(cands) == 3  # aceptable no prohíbe: las ofertas más caras siguen visibles


async def test_e5_total_de_linea_exige_la_cantidad_declarada(sessions, api):
    # La preferencia de precio (clase `maximum`) compite con la preferencia de proveedor Sur.
    supplier_sur = {
        "subject_kind": "supplier",
        "subject_key": "proveedor sur",
        "subject_label": "Proveedor Sur",
        "subject_supplier_id": api.farm["suppliers"]["Sur"],
        "effect": "prefer",
        "tolerance_kind": "none",
        "tolerance_origin": "not_applicable",
    }
    # Cantidad de referencia 5 ≠ cantidad de la línea 10: no se compara, el proveedor manda.
    await _interpret(
        sessions,
        api.farm,
        "c",
        preferences=[
            _price(
                "maximum",
                "21000",
                "per_line_total",
                price_reference_quantity="5",
                price_reference_quantity_unit="box",
            ),
            supplier_sur,
        ],
    )
    cands = _line_candidates(await _compare(api, ("c",), value="10"), api, "c")
    assert cands[0]["supplier"]["supplier_id"] == str(api.farm["suppliers"]["Sur"])

    # Con la cantidad correcta el total de línea se aplica y supera a la preferencia de proveedor:
    # Sur (22610) excede el máximo 21000 y queda después de Norte (20000) y Centro (21000).
    await _interpret(
        sessions,
        api.farm,
        "c",
        created_at=BASE_TIME + timedelta(hours=1),
        preferences=[
            _price(
                "maximum",
                "21000",
                "per_line_total",
                price_reference_quantity="10",
                price_reference_quantity_unit="box",
            ),
            supplier_sur,
        ],
    )
    cands = _line_candidates(await _compare(api, ("c",), value="10"), api, "c")
    assert [c["supplier"]["supplier_id"] for c in cands] == [
        str(api.farm["suppliers"]["Norte"]),
        str(api.farm["suppliers"]["Centro"]),
        str(api.farm["suppliers"]["Sur"]),
    ]


async def test_e5_preferencia_negativa_desprioriza_sin_excluir(sessions, api):
    await _interpret(
        sessions,
        api.farm,
        "a",
        preferences=[_lab("laboratorio chile", effect="deprioritize")],
    )
    payload = await _compare(api, ("a",))
    cands = _line_candidates(payload, api, "a")
    assert len(cands) == 3
    assert cands[0]["supplier"]["supplier_id"] == str(api.farm["suppliers"]["Sur"])
    assert {c["eligibility"] for c in cands} == {"eligible"}


# --------------------------------------------------------------------------- traza interna


async def test_traza_interna_se_guarda_y_no_se_expone(sessions, api):
    applied_id = await _interpret(
        sessions, api.farm, "a", preferences=[_lab("laboratorio chile")]
    )
    # Una interpretación rechazada se consultó, pero no se aplicó: la traza debe distinguirlas.
    rejected_id = await _interpret(
        sessions,
        api.farm,
        "a",
        status="rejected",
        created_at=BASE_TIME + timedelta(hours=1),
        preferences=[_price("maximum", "1950", "per_commercial_unit")],
    )
    r = await api.get(f"/api/v1/articles/{api.farm['items']['a']}", headers=H(api))
    body = r.json()
    assert "trace" not in r.text.lower()
    assert set(body) == {
        "article",
        "offers",
        "offers_total",
        "next_cursor",
        "reference_prices",
        "annotations",
        "notes",
        "sources",
        "generated_at",
    }
    assert all("preference" not in o for o in body["offers"])

    async with sessions() as s:
        rows = (await s.execute(select(PreferenceOrderTrace))).scalars().all()
    assert len(rows) == 1
    trace = rows[0]
    assert trace.context == "article_detail"
    assert trace.trade_item_id == api.farm["items"]["a"]
    assert trace.applied_preferences and trace.preference_ids
    assert {uuid.UUID(x) for x in trace.interpretation_ids} == {applied_id, rejected_id}
    # Solo la interpretación aplicada aparece en `applied_preferences`, con el estado, el modelo y
    # la versión del prompt congelados en el momento de aplicar (D-143).
    assert len(trace.applied_preferences) == 1
    descriptor = trace.applied_preferences[0]
    assert descriptor["interpretation_id"] == str(applied_id)
    assert descriptor["interpretation_status"] == "suggested"
    assert descriptor["preference_status"] == "suggested"
    assert descriptor["model"] == "test-model"
    assert descriptor["prompt_version"] == "test-v1"
    assert descriptor["observation_id"]
    assert trace.preference_ids == [descriptor["preference_id"]]
    assert set(trace.offer_ids_before) == set(trace.offer_ids_after)
    assert len(trace.offer_set_hash) == 64 and len(trace.preference_fingerprint) == 64

    # Repetir la lectura con el mismo estado no duplica la traza.
    await api.get(f"/api/v1/articles/{api.farm['items']['a']}", headers=H(api))
    async with sessions() as s:
        again = (await s.execute(select(PreferenceOrderTrace))).scalars().all()
    assert [row.id for row in again] == [trace.id]


async def test_traza_de_comparacion_no_cambia_la_respuesta(sessions, api):
    await _interpret(sessions, api.farm, "a", preferences=[_lab("laboratorio chile")])
    payload = await _compare(api, ("a",))
    assert "trace" not in str(payload).lower()

    async with sessions() as s:
        rows = (await s.execute(select(PreferenceOrderTrace))).scalars().all()
    assert len(rows) == 1 and rows[0].context == "comparison"


async def test_fallo_de_traza_no_interrumpe_el_detalle(
    sessions, api, monkeypatch, caplog
):
    await _interpret(sessions, api.farm, "a", preferences=[_lab("laboratorio chile")])

    async def _boom(*args, **kwargs):
        raise SQLAlchemyError("fallo de auditoría simulado")

    monkeypatch.setattr(preferences_svc, "record_trace", _boom)
    with caplog.at_level(logging.WARNING, logger="app.services.preferences"):
        # La lectura responde igual y conserva todas las ofertas, también al repetirse.
        first = await _article(api, "a")
        second = await _article(api, "a")
    assert set(first) == set(second) and len(first) == 3
    assert "Traza de preferencias no registrada" in caplog.text

    async with sessions() as s:
        rows = (await s.execute(select(PreferenceOrderTrace))).scalars().all()
    assert rows == []  # no se informa falsamente que la auditoría se guardó


async def test_fallo_de_traza_en_comparacion_revierte_la_corrida(
    sessions, api, monkeypatch
):
    await _interpret(sessions, api.farm, "a", preferences=[_lab("laboratorio chile")])

    async def _boom(*args, **kwargs):
        raise SQLAlchemyError("fallo de auditoría simulado")

    monkeypatch.setattr(preferences_svc, "record_trace", _boom)
    with pytest.raises(SQLAlchemyError):
        await _compare(api, ("a",))

    # La traza es obligatoria en la comparación: si falla, no queda una corrida a medias.
    async with sessions() as s:
        runs = (await s.execute(select(ComparisonRun))).scalars().all()
    assert runs == []


# --------------------------------------------------------------------------- N+1


async def test_preferencias_se_cargan_en_dos_consultas_para_varias_lineas(sessions, api, engine):
    await _interpret(sessions, api.farm, "a", preferences=[_lab("laboratorio chile")])
    await _interpret(sessions, api.farm, "b", preferences=[_lab("laboratorio andino")])

    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        await _compare(api, ("a", "b"))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)

    assert sum("FROM commercial_preference" in st for st in statements) == 1
    assert sum("FROM observation_interpretation" in st for st in statements) == 1


# --------------------------------------------------------------------------- retiro explícito (D-144)


def _classes(prefs: preferences_svc.ItemPreferences) -> set[str]:
    return {preferences_svc._class_of(p) for p in prefs.applicable}


async def _item_prefs(sessions, farm, item_id: uuid.UUID, *, org: str = "org"):
    async with sessions() as s:
        return (await preferences_svc.load_item_preferences(s, farm[org], [item_id]))[item_id]


async def test_retiro_1_laboratorio_posterior_reemplaza_al_anterior(sessions, api):
    """Chile y después Andino: la preferencia vigente es la última, no un retiro implícito."""
    norte, sur = str(api.farm["offers"]["B-N"]), str(api.farm["offers"]["B-S"])
    await _interpret(sessions, api.farm, "b", preferences=[_lab("laboratorio chile")])
    assert (await _article(api, "b"))[0] == norte
    await _interpret(
        sessions,
        api.farm,
        "b",
        created_at=BASE_TIME + timedelta(hours=1),
        preferences=[_lab("laboratorio andino")],
    )
    assert (await _article(api, "b"))[0] == sur

    prefs = await _item_prefs(sessions, api.farm, api.farm["items"]["b"])
    assert _classes(prefs) == {"laboratory"}
    assert prefs.retired_subject_kinds == ()
    assert [p.subject_key for p in prefs.applicable] == ["laboratorio andino"]


async def test_retiro_2_retirar_el_laboratorio_deja_sin_preferido(sessions, api):
    baseline = await _article(api, "c")
    await _interpret(sessions, api.farm, "c", preferences=[_lab("laboratorio andino")])
    assert (await _article(api, "c"))[0] == str(api.farm["offers"]["C-S"])
    await _interpret(
        sessions,
        api.farm,
        "c",
        created_at=BASE_TIME + timedelta(hours=1),
        retired=["laboratory"],
    )
    assert await _article(api, "c") == baseline

    prefs = await _item_prefs(sessions, api.farm, api.farm["items"]["c"])
    assert prefs.applicable == ()
    assert prefs.retired_subject_kinds == ("laboratory",)
    assert prefs.has_effects and not prefs.has_preferences


async def test_retiro_3_needs_review_posterior_no_lo_reactiva(sessions, api):
    baseline = await _article(api, "c")
    await _interpret(sessions, api.farm, "c", preferences=[_lab("laboratorio andino")])
    await _interpret(
        sessions, api.farm, "c", created_at=BASE_TIME + timedelta(hours=1), retired=["laboratory"]
    )
    reviewed = await _interpret(
        sessions,
        api.farm,
        "c",
        status="needs_review",
        created_at=BASE_TIME + timedelta(hours=2),
        preferences=[_lab("laboratorio andino")],
    )
    assert await _article(api, "c") == baseline

    prefs = await _item_prefs(sessions, api.farm, api.farm["items"]["c"])
    assert prefs.applicable == () and prefs.retired_subject_kinds == ("laboratory",)
    # La ambigua se consultó (queda en la traza) pero no modificó nada.
    assert reviewed in prefs.interpretation_ids


async def test_retiro_4_failed_posterior_no_lo_reactiva(sessions, api):
    baseline = await _article(api, "c")
    await _interpret(sessions, api.farm, "c", preferences=[_lab("laboratorio andino")])
    await _interpret(
        sessions, api.farm, "c", created_at=BASE_TIME + timedelta(hours=1), retired=["laboratory"]
    )
    await _interpret(
        sessions,
        api.farm,
        "c",
        status="failed",
        created_at=BASE_TIME + timedelta(hours=2),
        preferences=[_lab("laboratorio andino")],
    )
    assert await _article(api, "c") == baseline

    prefs = await _item_prefs(sessions, api.farm, api.farm["items"]["c"])
    assert prefs.applicable == () and prefs.retired_subject_kinds == ("laboratory",)


async def test_retiro_5_preferencia_posterior_valida_vuelve_a_establecer(sessions, api):
    await _interpret(sessions, api.farm, "b", preferences=[_lab("laboratorio andino")])
    await _interpret(
        sessions, api.farm, "b", created_at=BASE_TIME + timedelta(hours=1), retired=["laboratory"]
    )
    await _interpret(
        sessions,
        api.farm,
        "b",
        created_at=BASE_TIME + timedelta(hours=2),
        preferences=[_lab("laboratorio chile")],
    )
    assert (await _article(api, "b"))[0] == str(api.farm["offers"]["B-N"])

    prefs = await _item_prefs(sessions, api.farm, api.farm["items"]["b"])
    assert prefs.retired_subject_kinds == ()
    assert _classes(prefs) == {"laboratory"}
    assert [p.subject_key for p in prefs.applicable] == ["laboratorio chile"]


async def test_retiro_6_solo_retira_su_clase(sessions, api):
    specs = [
        _price("maximum", "1950", "per_commercial_unit"),
        {
            "subject_kind": "supplier",
            "subject_key": "proveedor sur",
            "subject_label": "Proveedor Sur",
            "subject_supplier_id": api.farm["suppliers"]["Sur"],
            "effect": "prefer",
            "tolerance_kind": "none",
            "tolerance_origin": "not_applicable",
        },
        _lab("laboratorio chile"),
        {
            "subject_kind": "brand",
            "subject_key": "marca beta",
            "subject_label": "Marca Beta",
            "effect": "prefer",
            "tolerance_kind": "none",
            "tolerance_origin": "not_applicable",
        },
        {
            "subject_kind": "availability",
            "subject_key": "in_stock",
            "subject_label": "Disponible",
            "effect": "prefer",
            "tolerance_kind": "none",
            "tolerance_origin": "not_applicable",
        },
    ]
    await _interpret(sessions, api.farm, "c", preferences=specs)
    order_before = await _article(api, "c")
    compare_before = await _compare(api, ("c",))
    before = await _item_prefs(sessions, api.farm, api.farm["items"]["c"])
    assert _classes(before) == {
        "price:maximum",
        "supplier",
        "laboratory",
        "brand",
        "availability",
    }

    await _interpret(
        sessions, api.farm, "c", created_at=BASE_TIME + timedelta(hours=1), retired=["laboratory"]
    )
    order_after = await _article(api, "c")
    compare_after = await _compare(api, ("c",))
    after = await _item_prefs(sessions, api.farm, api.farm["items"]["c"])
    assert _classes(after) == {"price:maximum", "supplier", "brand", "availability"}
    assert after.retired_subject_kinds == ("laboratory",)
    # Proveedor, marca, precio y disponibilidad siguen vigentes; el orden no cambia.
    assert order_after == order_before

    cands_before = _line_candidates(compare_before, api, "c")
    cands_after = _line_candidates(compare_after, api, "c")
    by_id = {c["offer_id"]: c for c in cands_before}
    assert {c["offer_id"] for c in cands_after} == set(by_id)
    for cand in cands_after:
        old = by_id[cand["offer_id"]]
        for field in ("eligibility", "line_payable", "line_gross", "quantity", "causes"):
            assert cand[field] == old[field]


async def test_retiro_7_acceptable_y_maximum_se_retiran_por_separado(sessions, api):
    ambos = [
        _price("acceptable", "2050", "per_commercial_unit"),
        _price("maximum", "1950", "per_commercial_unit"),
    ]
    await _interpret(sessions, api.farm, "c", preferences=ambos)
    assert _classes(await _item_prefs(sessions, api.farm, api.farm["items"]["c"])) == {
        "price:acceptable",
        "price:maximum",
    }

    # Retirar el aceptable no retira el máximo: Sur (1900) sigue primero por el máximo.
    await _interpret(
        sessions,
        api.farm,
        "c",
        created_at=BASE_TIME + timedelta(hours=1),
        retired=["price:acceptable"],
    )
    prefs = await _item_prefs(sessions, api.farm, api.farm["items"]["c"])
    assert _classes(prefs) == {"price:maximum"}
    assert prefs.retired_subject_kinds == ("price:acceptable",)
    assert (await _article(api, "c"))[0] == str(api.farm["offers"]["C-S"])

    # Y al revés: retirar el máximo no retira el aceptable (Centro, 2100, queda al final).
    await _interpret(
        sessions,
        api.farm,
        "c",
        created_at=BASE_TIME + timedelta(hours=2),
        preferences=ambos,
    )
    await _interpret(
        sessions,
        api.farm,
        "c",
        created_at=BASE_TIME + timedelta(hours=3),
        retired=["price:maximum"],
    )
    prefs = await _item_prefs(sessions, api.farm, api.farm["items"]["c"])
    assert _classes(prefs) == {"price:acceptable"}
    assert prefs.retired_subject_kinds == ("price:maximum",)
    assert (await _article(api, "c"))[-1] == str(api.farm["offers"]["C-C"])


async def test_retiro_8_una_observacion_retira_y_establece(sessions, api):
    await _interpret(sessions, api.farm, "c", preferences=[_lab("laboratorio andino")])
    await _interpret(
        sessions,
        api.farm,
        "c",
        created_at=BASE_TIME + timedelta(hours=1),
        retired=["laboratory"],
        preferences=[
            {
                "subject_kind": "supplier",
                "subject_key": "proveedor norte",
                "subject_label": "Proveedor Norte",
                "subject_supplier_id": api.farm["suppliers"]["Norte"],
                "effect": "prefer",
                "tolerance_kind": "none",
                "tolerance_origin": "not_applicable",
            }
        ],
    )
    prefs = await _item_prefs(sessions, api.farm, api.farm["items"]["c"])
    assert _classes(prefs) == {"supplier"}
    assert prefs.retired_subject_kinds == ("laboratory",)
    assert (await _article(api, "c"))[0] == str(api.farm["offers"]["C-N"])


async def test_retiro_9_sin_preferencia_previa_no_cambia_nada(sessions, api):
    baseline = await _article(api, "a")
    compare_before = await _compare(api, ("a",))
    await _interpret(sessions, api.farm, "a", retired=["laboratory"])

    r = await api.get(f"/api/v1/articles/{api.farm['items']['a']}", headers=H(api))
    assert [o["offer_id"] for o in r.json()["offers"]] == baseline
    # El retiro se audita internamente; la API pública no lo expone.
    assert "retired" not in r.text.lower() and "trace" not in r.text.lower()
    compare_after = await _compare(api, ("a",))
    cands_before = _line_candidates(compare_before, api, "a")
    cands_after = _line_candidates(compare_after, api, "a")
    assert [c["offer_id"] for c in cands_after] == [c["offer_id"] for c in cands_before]
    by_id = {c["offer_id"]: c for c in cands_before}
    for cand in cands_after:
        old = by_id[cand["offer_id"]]
        for field in ("eligibility", "line_payable", "line_gross", "quantity", "causes"):
            assert cand[field] == old[field]

    async with sessions() as s:
        trace = (
            await s.execute(
                select(PreferenceOrderTrace).where(
                    PreferenceOrderTrace.context == "article_detail",
                    PreferenceOrderTrace.trade_item_id == api.farm["items"]["a"],
                )
            )
        ).scalar_one()
    assert trace.retired_subject_kinds == ["laboratory"]
    assert trace.preference_ids == [] and trace.applied_preferences == []


async def test_retiro_10_aislado_por_articulo_y_organizacion(sessions, api):
    await _interpret(sessions, api.farm, "a", preferences=[_lab("laboratorio andino")])
    await _interpret(
        sessions, api.farm, "b", created_at=BASE_TIME + timedelta(hours=1), retired=["laboratory"]
    )
    await _interpret(sessions, api.farm, "c", preferences=[_lab("laboratorio andino")])

    prefs_a = await _item_prefs(sessions, api.farm, api.farm["items"]["a"])
    prefs_b = await _item_prefs(sessions, api.farm, api.farm["items"]["b"])
    prefs_c = await _item_prefs(sessions, api.farm, api.farm["items"]["c"])
    assert _classes(prefs_a) == {"laboratory"} and prefs_a.retired_subject_kinds == ()
    assert prefs_b.applicable == () and prefs_b.retired_subject_kinds == ("laboratory",)
    assert _classes(prefs_c) == {"laboratory"} and prefs_c.retired_subject_kinds == ()

    # Un retiro de otra organización no se ve ni se aplica desde esta.
    async with sessions() as s, s.begin():
        other_item = TradeItem(
            organization_id=api.farm["other_org"],
            display_name="Enalapril 10 mg x 1 comprimido (otra)",
            identity_key="fixture-a6-otra-org-enalapril",
            ingredient="enalapril",
            attributes=_attrs("enalapril", "1"),
            data_origin="simulated",
        )
        s.add(other_item)
        await s.flush()
        obs = CommercialObservation(
            organization_id=api.farm["other_org"],
            text_original="retiro de prueba",
            author_id=api.farm["outsider"],
            trade_item_id=other_item.id,
            data_origin="simulated",
        )
        s.add(obs)
        await s.flush()
        s.add(
            ObservationInterpretation(
                organization_id=api.farm["other_org"],
                observation_id=obs.id,
                trade_item_id=other_item.id,
                status="suggested",
                model="test-model",
                prompt_version="test-v1",
                retired_subject_kinds=["laboratory"],
                data_origin="simulated",
            )
        )
        await s.flush()
        other_item_id = other_item.id

    foreign = await _item_prefs(sessions, api.farm, other_item_id, org="org")
    assert foreign.applicable == () and foreign.retired_subject_kinds == ()
    own = await _item_prefs(sessions, api.farm, other_item_id, org="other_org")
    assert own.retired_subject_kinds == ("laboratory",)


async def test_retiro_11_conserva_historial_y_trazabilidad(sessions, api):
    item = api.farm["items"]["c"]
    chile = await _interpret(sessions, api.farm, "c", preferences=[_lab("laboratorio chile")])
    retiro = await _interpret(
        sessions, api.farm, "c", created_at=BASE_TIME + timedelta(hours=1), retired=["laboratory"]
    )
    andino = await _interpret(
        sessions,
        api.farm,
        "c",
        created_at=BASE_TIME + timedelta(hours=2),
        preferences=[_lab("laboratorio andino")],
    )
    await _article(api, "c")

    async with sessions() as s:
        interps = (
            (await s.execute(select(ObservationInterpretation))).scalars().all()
        )
        prefs_rows = (await s.execute(select(CommercialPreference))).scalars().all()
    assert {i.id for i in interps} == {chile, retiro, andino}
    assert {p.interpretation_id for p in prefs_rows} == {chile, andino}
    assert next(i for i in interps if i.id == retiro).retired_subject_kinds == ["laboratory"]

    async with sessions() as s:
        traces = (
            (
                await s.execute(
                    select(PreferenceOrderTrace).where(
                        PreferenceOrderTrace.trade_item_id == item,
                        PreferenceOrderTrace.context == "article_detail",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(traces) == 1
    first = traces[0]
    assert first.retired_subject_kinds == []
    assert [d["interpretation_id"] for d in first.applied_preferences] == [str(andino)]
    assert {uuid.UUID(x) for x in first.interpretation_ids} == {chile, retiro, andino}

    # Un retiro posterior es una fila nueva: la anterior no se borra ni se reescribe.
    later = await _interpret(
        sessions, api.farm, "c", created_at=BASE_TIME + timedelta(hours=3), retired=["laboratory"]
    )
    await _article(api, "c")
    async with sessions() as s:
        again = (
            (
                await s.execute(
                    select(PreferenceOrderTrace).where(
                        PreferenceOrderTrace.trade_item_id == item,
                        PreferenceOrderTrace.context == "article_detail",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(again) == 2
    assert first.id in {row.id for row in again}
    latest = next(row for row in again if row.id != first.id)
    assert latest.retired_subject_kinds == ["laboratory"]
    assert latest.preference_ids == [] and latest.applied_preferences == []
    assert {uuid.UUID(x) for x in latest.interpretation_ids} == {chile, retiro, andino, later}


async def test_retiro_12_e3_e5_no_cambian_elegibilidad_precios_ni_totales(sessions, api):
    detail_before = await _article(api, "a")
    compare_before = await _compare(api, ("a", "c"))

    await _interpret(sessions, api.farm, "a", preferences=[_lab("laboratorio andino")])
    await _interpret(sessions, api.farm, "c", preferences=[_lab("laboratorio andino")])
    for key in ("a", "c"):
        await _interpret(
            sessions,
            api.farm,
            key,
            created_at=BASE_TIME + timedelta(hours=1),
            retired=["laboratory"],
        )

    assert await _article(api, "a") == detail_before
    compare_after = await _compare(api, ("a", "c"))
    for key in ("a", "c"):
        cands_before = _line_candidates(compare_before, api, key)
        cands_after = _line_candidates(compare_after, api, key)
        assert [c["offer_id"] for c in cands_after] == [c["offer_id"] for c in cands_before]
        by_id = {c["offer_id"]: c for c in cands_before}
        for cand in cands_after:
            old = by_id[cand["offer_id"]]
            for field in ("eligibility", "line_payable", "line_gross", "quantity", "causes"):
                assert cand[field] == old[field]
    fields = (
        "supplier_id",
        "currency",
        "completeness",
        "subtotal",
        "taxes_added",
        "discounts_total",
        "shipping",
        "total",
        "lower_bound",
        "bound_status",
        "blockers",
        "causes",
    )
    assert [
        {k: row[k] for k in fields} for row in compare_before["supplier_totals"]
    ] == [{k: row[k] for k in fields} for row in compare_after["supplier_totals"]]


# ------------------------------------------------ A6 Fase 4: orden entre artículos (E3/E6)


async def _article_event(
    sessions,
    farm,
    article_key: str,
    effect: str,
    relative_key: str | None = None,
    *,
    status: str = "suggested",
    interpretation_status: str = "suggested",
    label: str | None = None,
) -> None:
    """Deja un evento entre artículos como lo haría O2, sin llamar a OpenAI."""
    interp_id = await _interpret(
        sessions,
        farm,
        article_key,
        status=interpretation_status,
    )
    async with sessions() as s, s.begin():
        interp = await s.get(ObservationInterpretation, interp_id)
        s.add(
            CommercialArticlePreference(
                organization_id=farm["org"],
                interpretation_id=interp_id,
                observation_id=interp.observation_id,
                article_id=farm["items"][article_key],
                article_label=label or farm["items"][article_key].hex,
                relative_to_article_id=(
                    farm["items"][relative_key] if relative_key is not None else None
                ),
                relative_to_article_label=(
                    farm["items"][relative_key].hex if relative_key is not None else None
                ),
                effect=effect,
                confidence="0.90",
                status=status,
                data_origin="simulated",
            )
        )


async def test_una_baja_de_prioridad_reordena_los_precios_de_la_lista(api, sessions):
    await _article_event(sessions, api.farm, "a", "deprioritize")

    r = await api.get(
        f"/api/v1/suppliers/{api.farm['suppliers']['Norte']}/offers", headers=H(api)
    )
    assert r.status_code == 200, r.text
    claves = {o["external_id"]: o["order_key"] for o in r.json()["offers"]}

    # Norte ofrece los dos artículos: el rebajado queda después. Sigue visible.
    assert claves["A-N"][0] == 1
    assert claves["B-N"][0] == 0
    # Todas las ofertas del proveedor siguen presentes.
    assert {"A-N", "B-N", "C-N", "D-N"} <= set(claves)


async def test_una_preferencia_entre_articulos_no_se_aplica_si_falta_el_otro(api, sessions):
    await _article_event(sessions, api.farm, "a", "prefer", "b")

    r = await api.get(
        f"/api/v1/suppliers/{api.farm['suppliers']['Centro']}/offers", headers=H(api)
    )
    assert r.status_code == 200, r.text
    claves = {o["external_id"]: o["order_key"] for o in r.json()["offers"]}

    # Centro no ofrece `b`: sin alternativa comparable nadie se demora ni desaparece.
    assert claves["A-C"][:2] == [0, 0]
    assert claves["C-C"][:2] == [0, 0]


async def test_un_evento_no_aplicado_no_ordena_y_no_inventa_asociaciones(api, sessions):
    await _article_event(
        sessions, api.farm, "a", "deprioritize", interpretation_status="needs_review"
    )

    r = await api.get(
        f"/api/v1/suppliers/{api.farm['suppliers']['Norte']}/offers", headers=H(api)
    )
    assert r.status_code == 200, r.text
    assert {o["order_key"][0] for o in r.json()["offers"]} == {0}


async def test_la_tarjeta_del_proveedor_informa_sus_ofertas_vigentes(api, sessions):
    """`offer_count` es aditivo: cuenta la última observación de cada SKU, no artículos únicos."""
    # Una observación posterior del mismo SKU reemplaza su precio; no suma una oferta nueva.
    async with sessions() as s, s.begin():
        original = await s.get(OfferSnapshot, api.farm["offers"]["A-N"])
        s.add(
            OfferSnapshot(
                organization_id=original.organization_id,
                supplier_id=original.supplier_id,
                source_id=original.source_id,
                trade_item_id=original.trade_item_id,
                external_id=original.external_id,
                price_amount="2050.00",
                currency="CLP",
                price_basis="per_commercial_unit",
                sale_unit="box",
                sale_unit_text="caja",
                stock_status="known",
                stock_quantity="90",
                stock_unit="box",
                tax_included=True,
                discount_included=True,
                minimum_known=True,
                minimum_quantity="1",
                minimum_unit="box",
                multiple_known=True,
                multiple_quantity="1",
                multiple_unit="box",
                observed_at=NOW + timedelta(minutes=1),
                data_origin="simulated",
            )
        )

    listado = await api.get("/api/v1/suppliers", headers=H(api))
    assert listado.status_code == 200, listado.text
    conteos = {s["name"]: s["offer_count"] for s in listado.json()["suppliers"]}
    assert conteos == {"Proveedor Centro": 2, "Proveedor Norte": 4, "Proveedor Sur": 4}

    detalle = await api.get(
        f"/api/v1/suppliers/{api.farm['suppliers']['Norte']}", headers=H(api)
    )
    assert detalle.status_code == 200, detalle.text
    assert detalle.json()["offer_count"] == 4
