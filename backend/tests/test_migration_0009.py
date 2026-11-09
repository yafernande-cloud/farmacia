"""Migración 0009: perfil de importación y las columnas nuevas de `import_run`.

Diseño en D-102, D-104 y D-107. Verifica contra PostgreSQL real que el perfil es uno por fuente,
que nada cruza organizaciones, que los jsonb tienen la forma esperada y que `import_run` sigue
siendo de solo agregar con su instantánea del perfil.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.db

HASH = "a" * 64


async def _exec(engine, sql, **params):
    async with engine.begin() as c:
        return await c.execute(text(sql), params)


async def _scalar(engine, sql, **params):
    return (await _exec(engine, sql, **params)).scalar()


async def _supplier(engine, org, name="Proveedor X"):
    return await _scalar(
        engine,
        "INSERT INTO supplier (organization_id, name, data_origin) VALUES (:o, :n, 'simulated') RETURNING id",
        o=org,
        n=name,
    )


async def _source(engine, org, name, kind="supplier", supplier_id=None):
    return await _scalar(
        engine,
        """INSERT INTO source (organization_id, kind, supplier_id, name, mode, data_origin)
           VALUES (:o, :k, :s, :n, 'manual_import', 'simulated') RETURNING id""",
        o=org,
        k=kind,
        s=supplier_id,
        n=name,
    )


async def _profile(engine, org, source_id, **over):
    cols = dict(
        file_format="xlsx",
        header_row=1,
        columns='{"sku": {"header": "SKU"}, "price": {"header": "Precio"}}',
        units='{"CAJA": "box"}',
        options='{"currency": "CLP", "price_basis": "per_commercial_unit"}',
        data_origin="simulated",
    )
    cols.update(over)
    names = ", ".join(cols)
    vals = ", ".join(f"CAST(:{k} AS jsonb)" if k in ("columns", "units", "options") else f":{k}" for k in cols)
    return await _scalar(
        engine,
        f"INSERT INTO import_profile (organization_id, source_id, {names}) "
        f"VALUES (:org, :src, {vals}) RETURNING id",
        org=org,
        src=source_id,
        **cols,
    )


async def _run(engine, org, source_id, **over):
    cols = dict(file_name="lista.xlsx", file_hash=HASH, status="completed", data_origin="simulated")
    cols.update(over)
    names = ", ".join(cols)
    vals = ", ".join(f":{k}" for k in cols)
    return await _scalar(
        engine,
        f"INSERT INTO import_run (organization_id, source_id, {names}) "
        f"VALUES (:org, :src, {vals}) RETURNING id",
        org=org,
        src=source_id,
        **cols,
    )


# ---------- estructura ----------


async def test_el_perfil_existe_y_import_run_gana_sus_columnas(engine):
    perfil = await _scalar(
        engine, "SELECT to_regclass('public.import_profile') IS NOT NULL"
    )
    assert perfil is True
    rows = await _exec(
        engine,
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'import_run' AND column_name IN "
        "('profile_id','profile_snapshot','rejected_rows')",
    )
    cols = dict(rows.all())
    assert cols == {"profile_id": "YES", "profile_snapshot": "NO", "rejected_rows": "NO"}


async def test_ninguna_fk_nueva_entra_al_modulo_de_faltantes(engine):
    toca = await _scalar(
        engine,
        """
        SELECT count(*) FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_class r ON r.oid = c.confrelid
        WHERE c.contype = 'f'
          AND ((t.relname = 'import_profile' AND r.relname IN
                ('demand_item','demand_observation','pharma_identity','commercial_reference','product_alias'))
            OR (r.relname = 'import_profile' AND t.relname IN
                ('demand_item','demand_observation','pharma_identity','commercial_reference','product_alias')))
        """,
    )
    assert toca == 0


# ---------- un perfil por fuente, aislado por organización ----------


async def test_un_solo_perfil_por_fuente(engine, seed):
    org = seed["org"]
    sup = await _supplier(engine, org)
    src = await _source(engine, org, "Lista X", supplier_id=sup)
    await _profile(engine, org, src)
    with pytest.raises(DBAPIError):
        await _profile(engine, org, src, columns='{"sku": {"header": "Codigo"}}')


async def test_la_misma_fuente_no_sirve_para_otra_organizacion(engine, seed):
    org, other = seed["org"], seed["other_org"]
    sup = await _supplier(engine, org)
    src = await _source(engine, org, "Lista X", supplier_id=sup)
    with pytest.raises(DBAPIError):
        await _profile(engine, other, src)


# ---------- validaciones del perfil ----------


async def test_perfil_rechaza_formato_cabecera_y_jsonb_invalidos(engine, seed):
    org = seed["org"]
    sup = await _supplier(engine, org)
    for i, bad in enumerate(
        (
            dict(file_format="pdf"),  # desde 0025 solo xlsx, xls y csv
            dict(header_row=0),
            dict(columns="[]"),  # debe ser objeto
            dict(units="[]"),
            dict(options="[]"),
            dict(status="draft"),
            dict(version=0),
        )
    ):
        # Una fuente nueva por caso: así el rechazo solo puede venir de la restricción que se prueba.
        src = await _source(engine, org, f"Lista {i}", supplier_id=sup)
        with pytest.raises(DBAPIError):
            await _profile(engine, org, src, **bad)


async def test_el_perfil_acepta_los_tres_formatos_que_lee_el_motor(engine, seed):
    """0025 amplía `file_format` a xls y csv: el motor de E8 lee los tres."""
    org = seed["org"]
    sup = await _supplier(engine, org)
    for fmt in ("xlsx", "xls", "csv"):
        src = await _source(engine, org, f"Lista {fmt}", supplier_id=sup)
        assert await _profile(engine, org, src, file_format=fmt)


async def test_el_perfil_se_edita_con_version(engine, seed):
    org = seed["org"]
    sup = await _supplier(engine, org)
    src = await _source(engine, org, "Lista X", supplier_id=sup)
    pid = await _profile(engine, org, src)
    await _exec(
        engine,
        "UPDATE import_profile SET columns = '{\"sku\": {\"header\": \"Codigo\"}}'::jsonb, "
        "version = 2, updated_at = now() WHERE id = :i",
        i=pid,
    )
    row = (
        await _exec(engine, "SELECT columns->'sku'->>'header', version FROM import_profile WHERE id = :i", i=pid)
    ).one()
    assert row == ("Codigo", 2)


# ---------- import_run: vínculo, snapshot y solo agregar ----------


async def test_import_run_solo_apunta_al_perfil_de_su_organizacion(engine, seed):
    org, other = seed["org"], seed["other_org"]
    sup_o = await _supplier(engine, org)
    src_o = await _source(engine, org, "Lista X", supplier_id=sup_o)
    sup_x = await _supplier(engine, other, "Proveedor ajeno")
    src_x = await _source(engine, other, "Lista ajena", supplier_id=sup_x)
    perfil_ajeno = await _profile(engine, other, src_x)
    # La FK compuesta (profile_id, organization_id) no encuentra la fila: no se cruza de organización.
    with pytest.raises(DBAPIError):
        await _run(engine, org, src_o, profile_id=perfil_ajeno)


async def test_import_run_guarda_snapshot_del_perfil_y_rechazos_minimos(engine, seed):
    org = seed["org"]
    sup = await _supplier(engine, org)
    src = await _source(engine, org, "Lista X", supplier_id=sup)
    pid = await _profile(engine, org, src)
    rechazos = (
        '[{"row": 14, "code": "MISSING_REQUIRED_FIELD", "description": "LOSARTAN 50 MG X 30"}]'
    )
    run = await _run(
        engine,
        org,
        src,
        profile_id=pid,
        profile_snapshot='{"columns": {"sku": {"header": "SKU"}}}',
        rejected_rows=rechazos,
        rows_total=20,
        rows_accepted=19,
        rows_rejected=1,
        status="completed_with_errors",
    )
    row = (
        await _exec(
            engine,
            "SELECT profile_id, profile_snapshot->'columns'->'sku'->>'header', "
            "rejected_rows->0->>'description', rows_total, rows_accepted, rows_rejected "
            "FROM import_run WHERE id = :i",
            i=run,
        )
    ).one()
    assert row == (pid, "SKU", "LOSARTAN 50 MG X 30", 20, 19, 1)


async def test_import_run_sin_perfil_usa_valores_por_defecto(engine, seed):
    org = seed["org"]
    sup = await _supplier(engine, org)
    src = await _source(engine, org, "Lista X", supplier_id=sup)
    run = await _run(engine, org, src)
    row = (
        await _exec(
            engine,
            "SELECT profile_id, profile_snapshot, rejected_rows FROM import_run WHERE id = :i",
            i=run,
        )
    ).one()
    assert row == (None, {}, [])


async def test_import_run_rechaza_snapshot_y_rechazos_con_forma_incorrecta(engine, seed):
    org = seed["org"]
    sup = await _supplier(engine, org)
    src = await _source(engine, org, "Lista X", supplier_id=sup)
    with pytest.raises(DBAPIError):
        await _run(engine, org, src, profile_snapshot="[]", file_hash="b" * 64)
    with pytest.raises(DBAPIError):
        await _run(engine, org, src, rejected_rows="{}", file_hash="c" * 64)


async def test_import_run_sigue_siendo_de_solo_agregar(engine, seed):
    org = seed["org"]
    sup = await _supplier(engine, org)
    src = await _source(engine, org, "Lista X", supplier_id=sup)
    run = await _run(engine, org, src)
    with pytest.raises(DBAPIError, match="solo agregar"):
        await _exec(engine, "UPDATE import_run SET rejected_rows = '[]'::jsonb WHERE id = :i", i=run)
    with pytest.raises(DBAPIError, match="solo agregar"):
        await _exec(engine, "DELETE FROM import_run WHERE id = :i", i=run)
