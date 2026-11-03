"""Migraciones y restricciones verificadas contra PostgreSQL real (no se omiten sin base)."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

import app.models  # noqa: F401
from app.db.base import Base
from tests.conftest import alembic_config

pytestmark = pytest.mark.db


def test_migracion_sube_baja_y_sube(db_url):
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "base")
    eng = create_engine(db_url)
    with eng.connect() as c:
        left = c.execute(
            text(
                "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename<>'alembic_version'"
            )
        ).scalar()
        fn = c.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname='forbid_mutation'")
        ).scalar()
    assert left == 0 and fn == 0
    command.upgrade(cfg, "head")
    eng.dispose()


def test_modelos_y_migracion_estan_sincronizados(db_url):
    eng = create_engine(db_url)
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    eng.dispose()
    assert diff == [], diff


async def _exec(engine, sql, **params):
    async with engine.begin() as c:
        return await c.execute(text(sql), params)


async def test_fk_compuesta_impide_mezclar_organizaciones(engine, seed):
    # Observación en la organización A escrita por un usuario de la organización B.
    with pytest.raises(IntegrityError):
        await _exec(
            engine,
            """
            INSERT INTO demand_observation (organization_id, cycle_id, author_id, raw_text, parsed,
              match_status, policy_version, idempotency_key, request_hash, data_origin, demand_item_id)
            SELECT :org, c.id, :intruso, 'x', '{}', 'needs_review', 'p', 'k', 'h', 'simulated', NULL
            FROM purchase_cycle c WHERE c.organization_id = :org
        """,
            org=seed["org"],
            intruso=seed["intruso"],
        )


async def _item(engine, seed, **over):
    cols = dict(
        ingredient="ibuprofeno", display_name="Ibuprofeno", status="active", data_origin="simulated"
    )
    cols.update(over)
    names, vals = ", ".join(cols), ", ".join(f":{k}" for k in cols)
    return await _exec(
        engine,
        f"""
        INSERT INTO demand_item (organization_id, cycle_id, {names})
        SELECT :org, c.id, {vals} FROM purchase_cycle c WHERE c.organization_id = :org
    """,
        org=seed["org"],
        **cols,
    )


@pytest.mark.parametrize(
    "bad",
    [
        dict(target_quantity=5),  # cantidad sin unidad
        dict(quantity_unit="box"),  # unidad sin cantidad
        dict(target_quantity=-1, quantity_unit="box"),  # negativa
        dict(target_quantity=1, quantity_unit="docena"),  # unidad inválida
    ],
)
async def test_cantidad_invalida_rechazada_por_la_base(engine, seed, bad):
    with pytest.raises(IntegrityError):
        await _item(engine, seed, **bad)


async def test_cantidad_desconocida_queda_nula(engine, seed):
    await _item(engine, seed)
    row = (await _exec(engine, "SELECT target_quantity, quantity_unit FROM demand_item")).one()
    assert row == (None, None)


async def test_identidad_resuelta_activa_es_unica_por_autor(engine, seed):
    await _item(engine, seed, resolved_identity_key="k1", reported_by=seed["ana"])
    with pytest.raises(IntegrityError):  # la misma persona no puede tener dos veces el mismo producto
        await _item(engine, seed, resolved_identity_key="k1", reported_by=seed["ana"])
    # Otra persona con la misma identidad es un reporte independiente, no un duplicado (D-135).
    await _item(engine, seed, resolved_identity_key="k1", reported_by=seed["luis"])
    # mismas claves nulas (identidades parciales) sí pueden coexistir: las protege el bloqueo del servicio
    await _item(engine, seed)
    await _item(engine, seed)
    # un reporte cancelado no bloquea uno nuevo de la misma persona con la misma identidad
    await _exec(
        engine,
        "UPDATE demand_item SET status='cancelled', cancelled_at=now() "
        "WHERE resolved_identity_key='k1' AND reported_by=:a",
        a=seed["ana"],
    )
    await _item(engine, seed, resolved_identity_key="k1", reported_by=seed["ana"])


async def test_un_solo_ciclo_abierto_por_organizacion(engine, seed):
    with pytest.raises(IntegrityError):
        await _exec(
            engine,
            "INSERT INTO purchase_cycle (organization_id, name) VALUES (:o, 'otro')",
            o=seed["org"],
        )


async def test_idempotencia_unica_por_organizacion(engine, seed):
    ins = """INSERT INTO demand_observation (organization_id, cycle_id, author_id, raw_text, parsed,
              match_status, policy_version, idempotency_key, request_hash, data_origin)
             SELECT :org, c.id, :a, 'x', '{}', 'needs_review', 'p', 'misma-clave', 'h', 'simulated'
             FROM purchase_cycle c WHERE c.organization_id = :org"""
    await _exec(engine, ins, org=seed["org"], a=seed["ana"])
    with pytest.raises(IntegrityError):
        await _exec(engine, ins, org=seed["org"], a=seed["luis"])


async def test_observacion_pendiente_no_puede_tener_destino(engine, seed):
    await _item(engine, seed)
    with pytest.raises(IntegrityError):
        await _exec(
            engine,
            """INSERT INTO demand_observation (organization_id, cycle_id, author_id, raw_text,
              parsed, match_status, policy_version, idempotency_key, request_hash, data_origin, demand_item_id)
              SELECT :org, i.cycle_id, :a, 'x', '{}', 'needs_review', 'p', 'k', 'h', 'simulated', i.id
              FROM demand_item i""",
            org=seed["org"],
            a=seed["ana"],
        )


@pytest.mark.parametrize("table", ["audit_event", "quantity_event"])
async def test_tablas_de_historia_son_solo_agregar(engine, seed, table):
    if table == "audit_event":
        await _exec(
            engine,
            "INSERT INTO audit_event (organization_id, action, entity_type, entity_id, correlation_id) "
            "VALUES (:o, 'a', 'x', gen_random_uuid(), 'c')",
            o=seed["org"],
        )
    else:
        await _item(engine, seed)
        await _exec(
            engine,
            """INSERT INTO quantity_event (organization_id, demand_item_id, actor_id, operation, value, unit,
              after_value, after_unit, version_before, version_after, idempotency_key, request_hash)
              SELECT :o, id, :a, 'SET', 5, 'box', 5, 'box', 1, 2, 'k', 'h' FROM demand_item""",
            o=seed["org"],
            a=seed["pao"],
        )
    with pytest.raises(DBAPIError, match="solo agregar"):
        await _exec(engine, f"UPDATE {table} SET created_at = now()")
    with pytest.raises(DBAPIError, match="solo agregar"):
        await _exec(engine, f"DELETE FROM {table}")
