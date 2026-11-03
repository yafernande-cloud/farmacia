"""Catálogo ficticio de demostración: conteos, idempotencia y aislamiento (PostgreSQL real).

Todo lo que crea el fixture debe quedar como `simulated`; ninguna fila puede nacer en una organización
real. Las pruebas usan la organización de simulación y una `real_test` separada.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.models import (
    AppUser,
    OfferSnapshot,
    Organization,
    ShippingTermsSnapshot,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
)
from app.services.actors import load_actor
from app.services.catalog_seed import FIXTURE_PATH, load_fixture, seed_catalog

pytestmark = pytest.mark.db


@pytest.fixture
async def farm(sessions):
    ids = {}
    async with sessions() as s, s.begin():
        sim = Organization(name="Farmacia seed", kind="simulation")
        real = Organization(name="Farmacia seed real", kind="real_test")
        s.add_all([sim, real])
        await s.flush()
        ids["org"], ids["real_org"] = sim.id, real.id
        for key, name, roles, org in [
            ("admin", "Admin", ["admin"], sim),
            ("real", "Admin real", ["admin"], real),
            ("aux", "Auxiliar", ["auxiliary"], sim),
        ]:
            u = AppUser(organization_id=org.id, display_name=name, roles=roles)
            s.add(u)
            await s.flush()
            ids[key] = u.id
    return ids


async def run_seed(sessions, who, **kw):
    async with sessions() as s:
        actor = await load_actor(s, who)
        try:
            report = await seed_catalog(s, actor, **kw)
            await s.commit()
            return report
        except BaseException:
            await s.rollback()
            raise


async def count(sessions, model) -> int:
    async with sessions() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_el_archivo_tiene_lo_que_promete():
    data = load_fixture(FIXTURE_PATH)
    assert len(data["suppliers"]) == 3
    assert len(data["sources"]) == 3
    assert len(data["articles"]) == 30
    assert len(data["offers"]) == 90
    assert data["fixture"] is True
    assert {o["source_key"] for o in data["offers"]} == {s["key"] for s in data["sources"]}


async def test_carga_completa_en_simulacion(sessions, farm):
    report = await run_seed(sessions, farm["admin"])
    assert report.errors == [], report.errors
    assert (report.suppliers_created, report.sources_created) == (3, 3)
    assert (report.articles_created, report.mappings_created) == (30, 90)
    assert report.shipping_created == 3
    assert report.offers_created == 90

    assert await count(sessions, Supplier) == 3
    assert await count(sessions, Source) == 3
    assert await count(sessions, TradeItem) == 30
    assert await count(sessions, SupplierProductMapping) == 90
    assert await count(sessions, ShippingTermsSnapshot) == 3
    assert await count(sessions, OfferSnapshot) == 90

    async with sessions() as s:
        offers = (await s.execute(select(OfferSnapshot))).scalars().all()
        assert all(o.data_origin == "simulated" for o in offers)
        assert all(o.evidence.get("fixture") is True for o in offers)
        assert all(o.review_status == "accepted" for o in offers)
        assert all(o.currency == "CLP" for o in offers)
        # Lo desconocido no se rellena: la fuente Centro no informa stock ni impuestos.
        centro = [o for o in offers if o.external_id.startswith("FC-")]
        assert all(o.stock_status == "unknown" and o.stock_quantity is None for o in centro)
        assert all(o.tax_included is None for o in centro)
        # La fuente Sur exige mínimo de 6 cajas; Norte declara explícitamente 1 y múltiplo 1.
        sur = [o for o in offers if o.external_id.startswith("FS-")]
        assert all(o.minimum_known and o.minimum_quantity == 6 for o in sur)
        assert all(o.multiple_known and o.multiple_quantity == 1 for o in sur)
        norte = [o for o in offers if o.external_id.startswith("FN-")]
        assert all(o.minimum_known and o.minimum_quantity == 1 for o in norte)
        assert all(o.multiple_known and o.multiple_quantity == 1 for o in norte)
        # Centro no informa mínimo ni múltiplo: lo desconocido no se rellena.
        assert all(not o.minimum_known and o.minimum_quantity is None for o in centro)
        assert all(not o.multiple_known and o.multiple_quantity is None for o in centro)

        sources = (await s.execute(select(Source))).scalars().all()
        assert all(x.mode == "fixture" and x.kind == "supplier" for x in sources)
        assert all(x.data_origin == "simulated" for x in sources)
        # Una fuente con ofertas observadas tiene una observación exitosa: E4 la ve `ok`, no
        # `not_configured`.
        assert all(x.last_success_at is not None for x in sources)

        items = (await s.execute(select(TradeItem))).scalars().all()
        assert all(x.data_origin == "simulated" for x in items)
        assert all(x.attributes.get("fixture") is True for x in items)
        assert all(x.attributes.get("schema_version") == 2 for x in items)


async def test_segunda_carga_no_duplica_nada(sessions, farm):
    first = await run_seed(sessions, farm["admin"])
    assert first.offers_created == 90
    second = await run_seed(sessions, farm["admin"])
    assert second.errors == [], second.errors
    assert (second.suppliers_created, second.articles_created) == (0, 0)
    assert (second.mappings_created, second.shipping_created, second.offers_created) == (0, 0, 0)
    assert (second.articles_unchanged, second.mappings_unchanged) == (30, 90)
    assert (second.shipping_unchanged, second.offers_unchanged) == (3, 90)
    assert await count(sessions, OfferSnapshot) == 90


async def test_nunca_entra_a_una_organizacion_real(sessions, farm):
    async with sessions() as s:
        actor = await load_actor(s, farm["real"])
        with pytest.raises(DomainError) as e:
            await seed_catalog(s, actor)
    assert (e.value.code, e.value.status_code) == ("FIXTURE_NOT_ALLOWED", 422)
    assert await count(sessions, OfferSnapshot) == 0
    assert await count(sessions, Supplier) == 0


async def test_completa_la_marca_de_observacion_si_faltaba(sessions, farm):
    await run_seed(sessions, farm["admin"])
    async with sessions() as s, s.begin():
        for row in (await s.execute(select(Source))).scalars():
            row.last_success_at = None
    report = await run_seed(sessions, farm["admin"])
    assert report.sources_created == 0
    async with sessions() as s:
        assert all(
            x is not None for x in (await s.execute(select(Source.last_success_at))).scalars()
        )


async def test_solo_admin(sessions, farm):
    async with sessions() as s:
        actor = await load_actor(s, farm["aux"])
        with pytest.raises(DomainError) as e:
            await seed_catalog(s, actor)
    assert (e.value.code, e.value.status_code) == ("FORBIDDEN", 403)


async def test_vista_previa_no_escribe_nada(sessions, farm):
    report = await run_seed(sessions, farm["admin"], dry_run=True)
    assert (report.suppliers_created, report.sources_created) == (3, 3)
    assert (report.articles_created, report.mappings_created) == (30, 90)
    assert (report.shipping_created, report.offers_created) == (3, 90)
    assert await count(sessions, Supplier) == 0
    assert await count(sessions, Source) == 0
    assert await count(sessions, OfferSnapshot) == 0


async def test_fecha_de_observacion_explicita_para_la_demo(sessions, farm):
    moment = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    report = await run_seed(sessions, farm["admin"], observed_at=moment)
    assert report.offers_created == 90
    async with sessions() as s:
        observed = set(
            (await s.execute(select(OfferSnapshot.observed_at).distinct())).scalars().all()
        )
    assert observed == {moment}


async def test_fecha_futura_o_sin_zona_se_rechaza(sessions, farm):
    for moment in (
        datetime(2099, 1, 1, tzinfo=UTC),
        datetime(2026, 11, 2, 12, 0),  # sin zona horaria
    ):
        async with sessions() as s:
            actor = await load_actor(s, farm["admin"])
            with pytest.raises(DomainError) as e:
                await seed_catalog(s, actor, observed_at=moment)
        assert e.value.code == "INVALID_OBSERVED_AT"
    assert await count(sessions, OfferSnapshot) == 0
