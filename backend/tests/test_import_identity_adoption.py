"""Corrección de identidades entre ofertas comparables (Ibuprofeno 400 mg x 20).

Caso real que motivó estos cambios: la preferencia «ASCEND LABORATORIES SPA con hasta 10 % más de
precio» se enseñó desde Andes AND-031 y quedó guardada en el artículo técnico
`import:<fuente>:AND-031`. Al vincular ese SKU a un artículo canónico que comparten AND-002
(Cordillera) y AND-031 (ASCEND):

* la adopción reubica la preferencia en el canónico **sin volver a enseñarla**, con la cronología,
  auditoría, idempotencia y reglas de conflicto existentes;
* el artículo `import:` que queda sin vínculos deja de listarse como vigente, sin borrar historial
  ni tocar artículos que todavía tienen vínculos;
* el motor ordena ASCEND primero dentro del proveedor, y lo mismo puede hacer con FarmaLoop cuando
  sus SKU estén **correctamente asociados** (aquí se vinculan explícitamente en el fixture);
* ninguna operación mezcla organizaciones ni toca SIMULACIÓN.

PostgreSQL real y sin llamadas a proveedores externos.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import DomainError
from app.domain.products import normalize_name
from app.models import (
    AppUser,
    AuditEvent,
    CommercialObservation,
    CommercialPreference,
    ObservationInterpretation,
    OfferSnapshot,
    Organization,
    OutboxEvent,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
)
from app.services import offer_identities as offer_svc
from app.services import preferences as pref_svc
from app.services.actors import load_actor
from app.services.supplier_mappings import relink_mapping

pytestmark = pytest.mark.db

CORDILLERA = "Laboratorio Cordillera"
ASCEND = "ASCEND LABORATORIES SPA"
IBUPROFENO = "Ibuprofeno 400 mg x 20 comprimidos"


def _attrs() -> dict:
    return {"schema_version": 2, "unassigned": [], "ingredient": "ibuprofeno"}


async def _run(sessions, user_id, fn, *args, **kwargs):
    async with sessions() as s:
        actor = await load_actor(s, user_id)
        try:
            result = await fn(s, actor, *args, **kwargs)
            await s.commit()
            return result
        except BaseException:
            await s.rollback()
            raise


@pytest.fixture
async def caso(sessions):
    """Organización de prueba real con los dos SKU de Andes y la organización SIMULACIÓN aparte."""
    ids: dict = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Prueba real", kind="real_test")
        sim = Organization(name="Simulación", kind="simulation")
        s.add_all([org, sim])
        await s.flush()
        ids["org"], ids["sim_org"] = org.id, sim.id

        pao = AppUser(organization_id=org.id, display_name="Pao", roles=["admin"])
        sim_admin = AppUser(organization_id=sim.id, display_name="Sim", roles=["admin"])
        s.add_all([pao, sim_admin])
        await s.flush()
        ids["pao"], ids["sim_admin"] = pao.id, sim_admin.id

        andes = Supplier(organization_id=org.id, name="Distribuidora Andes")
        farmaloop = Supplier(organization_id=org.id, name="FarmaLoop")
        s.add_all([andes, farmaloop])
        await s.flush()

        lista_andes = Source(
            organization_id=org.id,
            kind="supplier",
            supplier_id=andes.id,
            name="Distribuidora Andes",
            mode="manual_import",
            capabilities=["search", "price", "stock"],
        )
        vivo_farmaloop = Source(
            organization_id=org.id,
            kind="supplier",
            supplier_id=farmaloop.id,
            name="FarmaLoop",
            mode="api",
            capabilities=["search", "price", "stock"],
        )
        s.add_all([lista_andes, vivo_farmaloop])
        await s.flush()
        ids.update(
            source_andes=lista_andes.id,
            source_farmaloop=vivo_farmaloop.id,
            supplier_andes=andes.id,
            supplier_farmaloop=farmaloop.id,
        )

        canonical = TradeItem(
            organization_id=org.id,
            display_name=IBUPROFENO,
            identity_key="canonical-ibuprofeno-400-20",
            ingredient="ibuprofeno",
            attributes=_attrs(),
            data_origin="captured",
        )
        import_002 = TradeItem(
            organization_id=org.id,
            display_name=IBUPROFENO,
            identity_key=f"import:{lista_andes.id}:AND-002",
            ingredient="ibuprofeno",
            attributes=_attrs(),
            data_origin="captured",
        )
        import_031 = TradeItem(
            organization_id=org.id,
            display_name=IBUPROFENO,
            identity_key=f"import:{lista_andes.id}:AND-031",
            ingredient="ibuprofeno",
            attributes=_attrs(),
            data_origin="captured",
        )
        sim_item = TradeItem(
            organization_id=sim.id,
            display_name=IBUPROFENO,
            identity_key="sim-ibuprofeno-400-20",
            ingredient="ibuprofeno",
            attributes=_attrs(),
            data_origin="simulated",
        )
        s.add_all([canonical, import_002, import_031, sim_item])
        await s.flush()
        ids.update(
            canonical=canonical.id,
            import_002=import_002.id,
            import_031=import_031.id,
            sim_item=sim_item.id,
        )

        mappings: dict[str, SupplierProductMapping] = {}
        for external_id, item in (("AND-002", import_002), ("AND-031", import_031)):
            mapping = SupplierProductMapping(
                organization_id=org.id,
                source_id=lista_andes.id,
                supplier_id=andes.id,
                trade_item_id=item.id,
                external_id=external_id,
                external_name=IBUPROFENO,
                origin="explicit",
                data_origin="captured",
                created_by=pao.id,
            )
            s.add(mapping)
            mappings[external_id] = mapping
        await s.flush()
        ids["mappings"] = {
            external_id: mapping.id for external_id, mapping in mappings.items()
        }
    return ids


async def _teach(
    sessions,
    caso,
    item_id: uuid.UUID,
    *,
    when: datetime,
    label: str = ASCEND,
    tolerance: Decimal = Decimal("10"),
) -> uuid.UUID:
    """Enseñanza histórica con su fecha real (las interpretaciones son inmutables por trigger)."""
    async with sessions() as s, s.begin():
        observation = CommercialObservation(
            organization_id=caso["org"],
            text_original=(
                "Prefiero ASCEND LABORATORIES SPA por sobre otros laboratorios para el ibuprofeno "
                "400 mg, aunque su precio sea un poco más alto"
            ),
            author_id=caso["pao"],
            created_at=when,
            trade_item_id=item_id,
            context={"origin": "test"},
            data_origin="captured",
        )
        s.add(observation)
        await s.flush()
        interpretation = ObservationInterpretation(
            organization_id=caso["org"],
            observation_id=observation.id,
            trade_item_id=item_id,
            status="suggested",
            model="fake-observations",
            prompt_version="v1",
            created_at=when,
            data_origin="captured",
        )
        s.add(interpretation)
        await s.flush()
        s.add(
            CommercialPreference(
                organization_id=caso["org"],
                interpretation_id=interpretation.id,
                observation_id=observation.id,
                trade_item_id=item_id,
                subject_kind="laboratory",
                subject_key=normalize_name(label),
                subject_label=label,
                effect="prefer",
                tolerance_kind="relative_percent",
                tolerance_percent=tolerance,
                tolerance_origin="implicit_default",
                confidence=Decimal("0.9"),
                status="suggested",
                created_at=when,
                data_origin="captured",
            )
        )
        return interpretation.id


async def _prefs(sessions, org, item_id):
    async with sessions() as s:
        return (await pref_svc.load_item_preferences(s, org, [item_id]))[item_id]


async def _relink(sessions, caso, external_id: str, item_id: uuid.UUID):
    return await _run(
        sessions,
        caso["pao"],
        relink_mapping,
        mapping_id=caso["mappings"][external_id],
        trade_item_id=item_id,
    )


async def _count(sessions, model, **filters) -> int:
    async with sessions() as s:
        stmt = select(model.id)
        for column, value in filters.items():
            stmt = stmt.where(getattr(model, column) == value)
        return len((await s.execute(stmt)).scalars().all())


async def _snapshots(sessions, caso) -> dict:
    async with sessions() as s:
        rows = (
            await s.execute(
                select(OfferSnapshot).where(OfferSnapshot.organization_id == caso["org"])
            )
        ).scalars().all()
    return {
        row.id: {
            "trade_item_id": row.trade_item_id,
            "external_id": row.external_id,
            "laboratory": row.laboratory_as_published,
            "price": row.price_amount,
            "stock": row.stock_quantity,
            "review": row.review_status,
        }
        for row in rows
    }


# ------------------------------------------------------------------- adopción desde `import:`


async def test_la_preferencia_aprendida_en_and_031_llega_al_canonico_sin_reensena(sessions, caso):
    when = datetime.now(UTC) - timedelta(days=2)
    source_interp = await _teach(sessions, caso, caso["import_031"], when=when)

    mapping, changed, adoption = await _relink(sessions, caso, "AND-031", caso["canonical"])

    assert changed is True
    assert mapping.trade_item_id == caso["canonical"]
    assert adoption is not None and adoption.adopted is True

    canonical = await _prefs(sessions, caso["org"], caso["canonical"])
    assert canonical.has_effects
    assert [p.subject_label for p in canonical.applicable] == [ASCEND]
    adopted_pref = canonical.applicable[0]
    # La cronología es la original: la corrección no es una enseñanza nueva de la persona.
    assert adopted_pref.tolerance_percent == Decimal("10")
    assert adopted_pref.created_at == when

    # La observación y la interpretación originales permanecen intactas en el historial.
    async with sessions() as s:
        derived = (
            await s.execute(
                select(CommercialObservation).where(
                    CommercialObservation.trade_item_id == caso["canonical"]
                )
            )
        ).scalars().all()
        source_obs = (
            await s.execute(
                select(CommercialObservation).where(
                    CommercialObservation.trade_item_id == caso["import_031"]
                )
            )
        ).scalars().all()
    assert len(source_obs) == 1
    assert len(derived) == 1
    assert derived[0].context["adopted_from_interpretation_id"] == str(source_interp)
    assert derived[0].context["adoption_reason"] == "import_identity_linked"
    assert derived[0].context["adopted_from_identity_key"] == (
        f"import:{caso['source_andes']}:AND-031"
    )
    assert derived[0].text_original == source_obs[0].text_original
    assert derived[0].created_at == source_obs[0].created_at
    assert await _count(sessions, CommercialObservation, id=source_obs[0].id) == 1


async def test_repetir_la_adopcion_no_duplica_preferencias_ni_observaciones(sessions, caso):
    await _teach(sessions, caso, caso["import_031"], when=datetime.now(UTC) - timedelta(hours=3))
    await _relink(sessions, caso, "AND-031", caso["canonical"])

    _, changed, adoption = await _relink(sessions, caso, "AND-031", caso["canonical"])

    assert changed is False
    assert adoption is not None and adoption.adopted is False
    assert await _count(sessions, CommercialObservation, trade_item_id=caso["canonical"]) == 1
    assert await _count(sessions, CommercialPreference, trade_item_id=caso["canonical"]) == 1
    # Tampoco se duplica la desactivación del artículo técnico.
    async with sessions() as s:
        retired = (
            await s.execute(
                select(OutboxEvent).where(
                    OutboxEvent.type == "catalog.technical_identity_retired"
                )
            )
        ).scalars().all()
    assert len(retired) == 1


async def test_un_empate_exacto_se_audita_y_no_sobrescribe_la_preferencia_del_canonico(
    sessions, caso
):
    when = datetime.now(UTC) - timedelta(days=1)
    await _teach(sessions, caso, caso["canonical"], when=when, label="Laboratorio Andino")
    await _teach(sessions, caso, caso["import_031"], when=when)

    _, _, adoption = await _relink(sessions, caso, "AND-031", caso["canonical"])

    assert adoption is not None
    assert adoption.adopted is False
    assert adoption.skipped == "only_conflicts"
    canonical = await _prefs(sessions, caso["org"], caso["canonical"])
    assert [p.subject_label for p in canonical.applicable] == ["Laboratorio Andino"]


async def test_una_preferencia_mas_nueva_del_canonico_gana_a_la_importada(sessions, caso):
    now = datetime.now(UTC)
    await _teach(sessions, caso, caso["import_031"], when=now - timedelta(days=3))
    await _teach(sessions, caso, caso["canonical"], when=now - timedelta(hours=1))

    await _relink(sessions, caso, "AND-031", caso["canonical"])

    canonical = await _prefs(sessions, caso["org"], caso["canonical"])
    # La vigente es la del canónico (más reciente); la importada queda como historial.
    assert [p.subject_label for p in canonical.applicable] == [ASCEND]
    assert canonical.applicable[0].created_at == now - timedelta(hours=1)


async def test_solo_se_adopta_la_identidad_del_mismo_sku_y_la_misma_fuente(sessions, caso):
    await _teach(sessions, caso, caso["import_031"], when=datetime.now(UTC) - timedelta(hours=2))

    # Vincular AND-002 (otro SKU, sin enseñanzas) no traslada nada.
    _, changed, adoption = await _relink(sessions, caso, "AND-002", caso["canonical"])

    assert changed is True
    assert adoption is not None and adoption.adopted is False
    assert adoption.skipped == "no_effects"
    canonical = await _prefs(sessions, caso["org"], caso["canonical"])
    assert canonical.applicable == ()


async def test_otra_identidad_o_organizacion_no_puede_adoptarse(sessions, caso):
    """La adopción exige identidad exacta de fuente y SKU, y la misma organización."""
    async with sessions() as s:
        actor = await load_actor(s, caso["pao"])
        foreign = await s.get(TradeItem, caso["sim_item"])
        assert foreign is not None
        with pytest.raises(DomainError) as exc:
            await offer_svc.adopt_offer_identity(
                s,
                actor,
                source_id=caso["source_andes"],
                external_id="AND-031",
                canonical_item_id=caso["canonical"],
                identity_item=foreign,
            )
        assert exc.value.code == "IDENTITY_NOT_FOR_SKU"
        await s.rollback()

    # El artículo de SIMULACIÓN sigue intacto y sin preferencias.
    async with sessions() as s:
        sim_item = await s.get(TradeItem, caso["sim_item"])
        assert sim_item is not None
        assert sim_item.status == "active"
        assert sim_item.identity_key == "sim-ibuprofeno-400-20"
    assert await _count(sessions, CommercialPreference, trade_item_id=caso["sim_item"]) == 0


# ------------------------------------------------------------------- artículos técnicos


async def test_el_articulo_import_sin_vinculos_deja_de_listarse_y_conserva_historial(
    sessions, caso
):
    await _teach(sessions, caso, caso["import_031"], when=datetime.now(UTC) - timedelta(hours=4))

    await _relink(sessions, caso, "AND-031", caso["canonical"])

    async with sessions() as s:
        old = await s.get(TradeItem, caso["import_031"])
        still_linked = await s.get(TradeItem, caso["import_002"])
        assert old is not None and old.status == "inactive"
        assert old.version == 2
        assert old.identity_key == f"import:{caso['source_andes']}:AND-031"
        # AND-002 conserva su vínculo: sigue vigente.
        assert still_linked is not None and still_linked.status == "active"

        event = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.action == "trade_item.technical_identity_retired"
                )
            )
        ).scalar_one()
        assert event.entity_id == caso["import_031"]

    # El historial de la enseñanza original no se pierde.
    assert await _count(sessions, CommercialObservation, trade_item_id=caso["import_031"]) == 1
    assert await _count(sessions, CommercialPreference, trade_item_id=caso["import_031"]) == 1


async def test_no_se_desactiva_un_articulo_import_con_otra_dependencia(sessions, caso):
    """Un segundo vínculo (otra fuente) todavía apunta al artículo: se conserva vigente."""
    async with sessions() as s, s.begin():
        otra = Source(
            organization_id=caso["org"],
            kind="supplier",
            supplier_id=caso["supplier_andes"],
            name="Andes respaldo",
            mode="manual_import",
            capabilities=["price"],
        )
        s.add(otra)
        await s.flush()
        s.add(
            SupplierProductMapping(
                organization_id=caso["org"],
                source_id=otra.id,
                supplier_id=caso["supplier_andes"],
                trade_item_id=caso["import_031"],
                external_id="AND-031-R",
                external_name=IBUPROFENO,
                origin="explicit",
                data_origin="captured",
                created_by=caso["pao"],
            )
        )

    await _relink(sessions, caso, "AND-031", caso["canonical"])

    async with sessions() as s:
        old = await s.get(TradeItem, caso["import_031"])
        assert old is not None and old.status == "active"


async def test_una_identidad_de_oferta_viva_no_se_retira(sessions, caso):
    async with sessions() as s:
        actor = await load_actor(s, caso["pao"])
        offer_item, _ = await offer_svc.ensure_offer_identity(
            s,
            actor,
            offer_svc.OfferViewContext(
                source_id=caso["source_farmaloop"],
                external_id="15391",
                name=IBUPROFENO,
                laboratory=ASCEND,
                price_amount=Decimal("990"),
                currency="CLP",
                price_basis="per_commercial_unit",
                stock_status="known",
                stock_quantity=Decimal("10"),
                sale_unit="box",
            ),
        )
        await s.commit()
        offer_id = offer_item.id

        retired = await offer_svc.retire_orphan_import_identity(s, actor, offer_item)
        await s.rollback()

    assert retired is False
    async with sessions() as s:
        item = await s.get(TradeItem, offer_id)
        assert item is not None and item.status == "inactive"  # como nace, nunca visible
        assert item.identity_key.startswith("offer:")


# ------------------------------------------------------------------- orden de las ofertas


def _view(offer_id, *, article_id, laboratory, price, supplier_id=None, stock="10"):
    return pref_svc.OfferView(
        offer_id=offer_id,
        supplier_id=supplier_id,
        laboratory=laboratory,
        brand=None,
        currency="CLP",
        price_amount=Decimal(price),
        price_basis="per_commercial_unit",
        pack_units_per_commercial_unit=None,
        stock_status="known",
        stock_quantity=Decimal(stock),
        stock_unit="box",
        sale_unit="box",
        trade_item_id=article_id,
    )


async def test_ascend_queda_primero_entre_las_ofertas_de_andes(sessions, caso):
    await _teach(sessions, caso, caso["import_031"], when=datetime.now(UTC) - timedelta(hours=5))
    await _relink(sessions, caso, "AND-031", caso["canonical"])
    prefs = await _prefs(sessions, caso["org"], caso["canonical"])

    cordillera = uuid.uuid4()
    ascend = uuid.uuid4()
    keys = pref_svc.rank_provider_offers(
        pref_svc.EMPTY_ARTICLES,
        {caso["canonical"]: prefs},
        [
            _view(cordillera, article_id=caso["canonical"], laboratory=CORDILLERA, price="3190"),
            _view(ascend, article_id=caso["canonical"], laboratory=ASCEND, price="2890"),
        ],
    )
    assert keys[ascend] < keys[cordillera]


async def test_la_preferencia_ordena_farmaloop_cuando_sus_sku_estan_asociados(sessions, caso):
    """91280 y 15391 no se asumen asociados: aquí se vinculan explícitamente para probar E3."""
    await _teach(sessions, caso, caso["import_031"], when=datetime.now(UTC) - timedelta(hours=6))
    await _relink(sessions, caso, "AND-031", caso["canonical"])

    async with sessions() as s, s.begin():
        for external_id, name in (("91280", "Ibuprofeno 400 mg x 20"), ("15391", IBUPROFENO)):
            s.add(
                SupplierProductMapping(
                    organization_id=caso["org"],
                    source_id=caso["source_farmaloop"],
                    supplier_id=caso["supplier_farmaloop"],
                    trade_item_id=caso["canonical"],
                    external_id=external_id,
                    external_name=name,
                    origin="explicit",
                    data_origin="captured",
                    created_by=caso["pao"],
                )
            )

    prefs = await _prefs(sessions, caso["org"], caso["canonical"])
    chileno = uuid.uuid4()
    ascend = uuid.uuid4()
    keys = pref_svc.rank_provider_offers(
        pref_svc.EMPTY_ARTICLES,
        {caso["canonical"]: prefs},
        [
            _view(
                chileno,
                article_id=caso["canonical"],
                laboratory="Laboratorio Chile",
                price="1250",
            ),
            _view(ascend, article_id=caso["canonical"], laboratory=ASCEND, price="990"),
        ],
    )
    assert keys[ascend] < keys[chileno]


# ------------------------------------------------------------------- datos originales intactos


async def test_precios_laboratorios_stock_y_condiciones_no_cambian_al_corregir_identidades(
    sessions, caso
):
    async with sessions() as s, s.begin():
        for external_id, item_id, laboratory, price in (
            ("AND-002", caso["import_002"], CORDILLERA, Decimal("3190")),
            ("AND-031", caso["import_031"], ASCEND, Decimal("2890")),
        ):
            s.add(
                OfferSnapshot(
                    organization_id=caso["org"],
                    supplier_id=caso["supplier_andes"],
                    source_id=caso["source_andes"],
                    trade_item_id=item_id,
                    external_id=external_id,
                    external_name=IBUPROFENO,
                    laboratory_as_published=laboratory,
                    price_amount=price,
                    currency="CLP",
                    price_basis="per_commercial_unit",
                    stock_status="known",
                    stock_quantity=Decimal("12"),
                    stock_unit="box",
                    sale_unit="box",
                    review_status="accepted",
                    observed_at=datetime.now(UTC) - timedelta(hours=1),
                    data_origin="captured",
                )
            )

    before = await _snapshots(sessions, caso)
    await _relink(sessions, caso, "AND-031", caso["canonical"])
    after = await _snapshots(sessions, caso)

    # La corrección mueve el vínculo, no la observación: precios, laboratorios y stock intactos.
    assert before == after
    assert {row["laboratory"] for row in after.values()} == {CORDILLERA, ASCEND}
    assert {row["price"] for row in after.values()} == {Decimal("3190"), Decimal("2890")}
    assert {row["stock"] for row in after.values()} == {Decimal("12")}
