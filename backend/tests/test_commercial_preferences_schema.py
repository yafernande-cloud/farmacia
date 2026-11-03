"""Esquema de interpretaciones y preferencias comerciales (A6, O2–O4, D-136/D-138/D-141).

Pruebas de base, sin llamadas a OpenAI. Verifican que una preferencia no puede cruzar artículo u
organización, que la historia no se reescribe, que las tolerancias y referencias de precio son
coherentes y que el orden de ofertas deja traza interna. La aplicación determinista del orden se
prueba en la fase siguiente.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models import (
    CommercialObservation,
    CommercialPreference,
    ObservationInterpretation,
    OfferSnapshot,
    Source,
    Supplier,
    TradeItem,
)
from tests.conftest import alembic_config

pytestmark = pytest.mark.db

NOW = datetime.now(UTC)
LAB = "Laboratorio Chile"


@pytest.fixture
async def world(sessions, seed):
    """Dos artículos del mismo laboratorio, un proveedor y una observación del primero."""
    ids: dict = {}
    async with sessions() as s, s.begin():
        org = seed["org"]
        uno = TradeItem(
            organization_id=org,
            display_name="Losartán 50 mg 30 comprimidos",
            identity_key="losartan-50-30",
            laboratory=LAB,
            data_origin="simulated",
        )
        dos = TradeItem(
            organization_id=org,
            display_name="Losartán 100 mg 30 comprimidos",
            identity_key="losartan-100-30",
            laboratory=LAB,
            data_origin="simulated",
        )
        supplier = Supplier(organization_id=org, name="Proveedor Uno", data_origin="simulated")
        s.add_all([uno, dos, supplier])
        await s.flush()
        source = Source(
            organization_id=org,
            kind="supplier",
            supplier_id=supplier.id,
            name="Fuente Proveedor Uno",
            mode="manual_import",
            capabilities=["search", "price", "stock"],
            enabled=True,
            last_success_at=NOW,
            data_origin="simulated",
        )
        s.add(source)
        await s.flush()
        obs = CommercialObservation(
            organization_id=org,
            text_original="Prefiero Laboratorio Chile aunque sea un poco más caro",
            author_id=seed["pao"],
            trade_item_id=uno.id,
            data_origin="captured",
        )
        s.add(obs)
        await s.flush()
        ids.update(
            org=org,
            item=uno.id,
            other_item=dos.id,
            supplier=supplier.id,
            source=source.id,
            observation=obs.id,
            admin=seed["pao"],
        )
    return ids


async def _interpretation(sessions, world, **over):
    values = dict(
        organization_id=world["org"],
        observation_id=world["observation"],
        trade_item_id=world["item"],
        status="suggested",
        model="modelo-de-prueba",
        prompt_version="prompt-v1",
        raw_output={"preferences": []},
        data_origin="ai_derived",
    )
    values.update(over)
    async with sessions() as s, s.begin():
        row = ObservationInterpretation(**values)
        s.add(row)
        await s.flush()
        return row.id


def _preference(world, interpretation_id, **over) -> CommercialPreference:
    values = dict(
        organization_id=world["org"],
        interpretation_id=interpretation_id,
        observation_id=world["observation"],
        trade_item_id=world["item"],
        subject_kind="laboratory",
        subject_key="laboratorio chile",
        subject_label=LAB,
        effect="prefer",
        tolerance_kind="none",
        tolerance_origin="not_applicable",
        price_reference_kind="none",
        confidence=Decimal("0.90"),
        status="suggested",
        data_origin="ai_derived",
    )
    values.update(over)
    return CommercialPreference(**values)


async def _insert_preference(sessions, world, interpretation_id, **over):
    async with sessions() as s, s.begin():
        row = _preference(world, interpretation_id, **over)
        s.add(row)
        await s.flush()
        return row.id


async def _exec(engine, sql, **params):
    async with engine.begin() as c:
        return await c.execute(text(sql), params)


async def test_tablas_y_columnas_nuevas_existen(engine):
    async with engine.connect() as c:
        tables = set(
            (
                await c.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                        "AND tablename IN ('observation_interpretation','commercial_preference',"
                        "'preference_order_trace')"
                    )
                )
            ).scalars()
        )
        offer_columns = set(
            (
                await c.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='offer_snapshot' "
                        "AND column_name IN ('laboratory_as_published','brand_as_published')"
                    )
                )
            ).scalars()
        )
    assert tables == {
        "observation_interpretation",
        "commercial_preference",
        "preference_order_trace",
    }
    assert offer_columns == {"laboratory_as_published", "brand_as_published"}


async def test_interpretacion_es_historica_y_solo_se_revisa(engine, sessions, world):
    interp = await _interpretation(sessions, world)

    with pytest.raises(DBAPIError, match="no se reescribe"):
        await _exec(
            engine,
            "UPDATE observation_interpretation SET model='otro' WHERE id=:i",
            i=interp,
        )

    # Revisar sí se permite, con autor y fecha.
    await _exec(
        engine,
        "UPDATE observation_interpretation SET status='confirmed', reviewed_by=:a, "
        "reviewed_at=now() WHERE id=:i",
        a=world["admin"],
        i=interp,
    )
    # Confirmada es terminal.
    with pytest.raises(DBAPIError, match="estado terminal"):
        await _exec(
            engine,
            "UPDATE observation_interpretation SET status='rejected', reviewed_by=:a, "
            "reviewed_at=now() WHERE id=:i",
            a=world["admin"],
            i=interp,
        )
    with pytest.raises(DBAPIError, match="no se permite borrar"):
        await _exec(engine, "DELETE FROM observation_interpretation WHERE id=:i", i=interp)


async def test_interpretacion_fallida_exige_codigo(sessions, world):
    with pytest.raises(IntegrityError):
        await _interpretation(sessions, world, status="failed", error_code=None)

    interp = await _interpretation(
        sessions, world, status="failed", error_code="quota_exceeded", raw_output=None
    )
    assert interp is not None


async def test_interpretacion_confirmada_exige_revision(sessions, world):
    with pytest.raises(IntegrityError):
        await _interpretation(sessions, world, status="confirmed", reviewed_at=None)

    interp = await _interpretation(
        sessions, world, status="confirmed", reviewed_by=world["admin"], reviewed_at=NOW
    )
    assert interp is not None


async def test_preferencia_no_cruza_articulo_ni_observacion(sessions, world):
    interp = await _interpretation(sessions, world)

    with pytest.raises(IntegrityError):
        await _insert_preference(sessions, world, interp, trade_item_id=world["other_item"])

    with pytest.raises(IntegrityError):
        await _insert_preference(
            sessions,
            world,
            interp,
            observation_id=await _other_observation(sessions, world),
        )


async def _other_observation(sessions, world) -> object:
    async with sessions() as s, s.begin():
        row = CommercialObservation(
            organization_id=world["org"],
            text_original="Otra observación",
            author_id=world["admin"],
            trade_item_id=world["other_item"],
            data_origin="captured",
        )
        s.add(row)
        await s.flush()
        return row.id


async def test_preferencia_es_historica_y_solo_cambia_estado(engine, sessions, world):
    interp = await _interpretation(sessions, world)
    pref = await _insert_preference(sessions, world, interp)

    with pytest.raises(DBAPIError, match="no se reescribe"):
        await _exec(
            engine,
            "UPDATE commercial_preference SET subject_label='Otro' WHERE id=:p",
            p=pref,
        )

    await _exec(
        engine,
        "UPDATE commercial_preference SET status='confirmed' WHERE id=:p",
        p=pref,
    )
    with pytest.raises(DBAPIError, match="estado terminal"):
        await _exec(
            engine,
            "UPDATE commercial_preference SET status='rejected' WHERE id=:p",
            p=pref,
        )
    with pytest.raises(DBAPIError, match="no se permite borrar"):
        await _exec(engine, "DELETE FROM commercial_preference WHERE id=:p", p=pref)


@pytest.mark.parametrize(
    "bad",
    [
        # Porcentaje sin origen explícito/implícito.
        dict(tolerance_kind="relative_percent", tolerance_percent=10),
        # Porcentaje con monto absoluto a la vez.
        dict(
            tolerance_kind="relative_percent",
            tolerance_percent=10,
            tolerance_amount=500,
            tolerance_currency="CLP",
            tolerance_origin="explicit",
        ),
        # Monto absoluto sin moneda.
        dict(
            tolerance_kind="absolute_amount",
            tolerance_amount=500,
            tolerance_origin="explicit",
        ),
        # Una preferencia negativa no lleva tolerancia.
        dict(
            effect="deprioritize",
            tolerance_kind="relative_percent",
            tolerance_percent=10,
            tolerance_origin="explicit",
        ),
        # El precio no lleva tolerancia; usa referencia.
        dict(
            subject_kind="price",
            subject_key="9990",
            subject_label="$9.990",
            tolerance_kind="relative_percent",
            tolerance_percent=10,
            tolerance_origin="explicit",
            price_reference_kind="maximum",
            price_reference_amount=9990,
            price_reference_currency="CLP",
            price_reference_basis="per_commercial_unit",
        ),
    ],
)
async def test_tolerancias_incoherentes_rechazadas(sessions, world, bad):
    interp = await _interpretation(sessions, world)
    with pytest.raises(IntegrityError):
        await _insert_preference(sessions, world, interp, **bad)


async def test_tolerancias_validas_se_guardan(sessions, world):
    interp = await _interpretation(sessions, world)
    implicit = await _insert_preference(
        sessions,
        world,
        interp,
        tolerance_kind="relative_percent",
        tolerance_percent=10,
        tolerance_origin="implicit_default",
    )
    explicit = await _insert_preference(
        sessions,
        world,
        interp,
        tolerance_kind="relative_percent",
        tolerance_percent=15,
        tolerance_origin="explicit",
    )
    absolute = await _insert_preference(
        sessions,
        world,
        interp,
        tolerance_kind="absolute_amount",
        tolerance_amount=500,
        tolerance_currency="CLP",
        tolerance_origin="explicit",
    )
    assert len({implicit, explicit, absolute}) == 3


@pytest.mark.parametrize(
    "bad",
    [
        # Un precio sin referencia no es una preferencia de precio.
        dict(subject_kind="price", subject_key="9990", subject_label="$9.990"),
        # Referencia de precio en un sujeto que no es precio.
        dict(
            price_reference_kind="acceptable",
            price_reference_amount=9990,
            price_reference_currency="CLP",
            price_reference_basis="per_commercial_unit",
        ),
        # Referencia sin base.
        dict(
            subject_kind="price",
            subject_key="9990",
            subject_label="$9.990",
            price_reference_kind="maximum",
            price_reference_amount=9990,
            price_reference_currency="CLP",
        ),
        # Precio total sin cantidad/unidad.
        dict(
            subject_kind="price",
            subject_key="9990",
            subject_label="$9.990",
            price_reference_kind="maximum",
            price_reference_amount=9990,
            price_reference_currency="CLP",
            price_reference_basis="per_line_total",
        ),
        # El precio no se usa para despriorizar.
        dict(
            subject_kind="price",
            subject_key="9990",
            subject_label="$9.990",
            effect="deprioritize",
            price_reference_kind="maximum",
            price_reference_amount=9990,
            price_reference_currency="CLP",
            price_reference_basis="per_commercial_unit",
        ),
    ],
)
async def test_referencias_de_precio_incoherentes_rechazadas(sessions, world, bad):
    interp = await _interpretation(sessions, world)
    with pytest.raises(IntegrityError):
        await _insert_preference(sessions, world, interp, **bad)


async def test_referencias_de_precio_validas_se_guardan(sessions, world):
    interp = await _interpretation(sessions, world)
    acceptable = await _insert_preference(
        sessions,
        world,
        interp,
        subject_kind="price",
        subject_key="9990",
        subject_label="$9.990",
        price_reference_kind="acceptable",
        price_reference_amount=9990,
        price_reference_currency="CLP",
        price_reference_basis="per_commercial_unit",
    )
    maximum = await _insert_preference(
        sessions,
        world,
        interp,
        subject_kind="price",
        subject_key="9990",
        subject_label="$9.990",
        price_reference_kind="maximum",
        price_reference_amount=9990,
        price_reference_currency="CLP",
        price_reference_basis="per_line_total",
        price_reference_quantity=3,
        price_reference_quantity_unit="box",
    )
    assert acceptable != maximum


async def test_disponibilidad_solo_prefiere_stock(sessions, world):
    interp = await _interpretation(sessions, world)
    pref = await _insert_preference(
        sessions,
        world,
        interp,
        subject_kind="availability",
        subject_key="in_stock",
        subject_label="con stock",
    )
    assert pref is not None

    with pytest.raises(IntegrityError):
        await _insert_preference(
            sessions,
            world,
            interp,
            subject_kind="availability",
            subject_key="in_stock",
            subject_label="con stock",
            effect="deprioritize",
        )
    with pytest.raises(IntegrityError):
        await _insert_preference(
            sessions,
            world,
            interp,
            subject_kind="availability",
            subject_key="otro",
            subject_label="otro",
        )


async def test_proveedor_exige_identificador_y_los_demas_lo_prohiben(sessions, world):
    interp = await _interpretation(sessions, world)
    with pytest.raises(IntegrityError):
        await _insert_preference(
            sessions,
            world,
            interp,
            subject_kind="supplier",
            subject_key="proveedor uno",
            subject_label="Proveedor Uno",
        )
    with pytest.raises(IntegrityError):
        await _insert_preference(
            sessions, world, interp, subject_supplier_id=world["supplier"]
        )
    pref = await _insert_preference(
        sessions,
        world,
        interp,
        subject_kind="supplier",
        subject_key="proveedor uno",
        subject_label="Proveedor Uno",
        subject_supplier_id=world["supplier"],
        effect="deprioritize",
    )
    assert pref is not None


async def test_oferta_guarda_laboratorio_y_marca_sin_inferir(sessions, world):
    async with sessions() as s, s.begin():
        s.add_all(
            [
                OfferSnapshot(
                    organization_id=world["org"],
                    supplier_id=world["supplier"],
                    source_id=world["source"],
                    trade_item_id=world["item"],
                    external_id="SKU-1",
                    price_amount=Decimal("1000"),
                    currency="CLP",
                    sale_unit="box",
                    laboratory_as_published=LAB,
                    brand_as_published=None,
                    observed_at=NOW,
                    data_origin="simulated",
                ),
                OfferSnapshot(
                    organization_id=world["org"],
                    supplier_id=world["supplier"],
                    source_id=world["source"],
                    trade_item_id=world["other_item"],
                    external_id="SKU-2",
                    price_amount=Decimal("1200"),
                    currency="CLP",
                    sale_unit="box",
                    observed_at=NOW,
                    data_origin="simulated",
                ),
            ]
        )
    async with sessions() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT laboratory_as_published, brand_as_published FROM offer_snapshot "
                    "ORDER BY external_id"
                )
            )
        ).all()
    assert rows == [(LAB, None), (None, None)]


async def test_traza_de_orden_es_solo_agregar(engine, world):
    await _exec(
        engine,
        """
        INSERT INTO preference_order_trace
            (organization_id, trade_item_id, context, offer_set_hash, preference_fingerprint)
        VALUES (:org, :item, 'article_detail', repeat('a', 64), repeat('b', 64))
        """,
        org=world["org"],
        item=world["item"],
    )
    with pytest.raises(DBAPIError, match="solo agregar"):
        await _exec(engine, "UPDATE preference_order_trace SET context='comparison'")
    with pytest.raises(DBAPIError, match="solo agregar"):
        await _exec(engine, "DELETE FROM preference_order_trace")
    with pytest.raises(IntegrityError):
        await _exec(
            engine,
            """
            INSERT INTO preference_order_trace
                (organization_id, trade_item_id, context, offer_set_hash, preference_fingerprint)
            VALUES (:org, :item, 'article_detail', repeat('a', 64), repeat('b', 64))
            """,
            org=world["org"],
            item=world["item"],
        )


def test_migracion_0018_baja_y_sube(db_url):
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "0017")
    eng = create_engine(db_url)
    with eng.connect() as c:
        tables = set(
            c.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "AND tablename IN ('observation_interpretation','commercial_preference',"
                    "'preference_order_trace')"
                )
            ).scalars()
        )
        columns = set(
            c.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='offer_snapshot' "
                    "AND column_name IN ('laboratory_as_published','brand_as_published')"
                )
            ).scalars()
        )
    assert tables == set()
    assert columns == set()
    eng.dispose()
    command.upgrade(cfg, "head")
