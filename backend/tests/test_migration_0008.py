"""Migración 0008: catálogo, fichas de cadena, precios públicos y ofertas.

Verifica contra PostgreSQL real la separación de planos, la integridad por organización y fuente,
el vínculo explícito/confirmado y la inmutabilidad con su excepción controlada.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.db

HASH_A = "a" * 64
HASH_B = "b" * 64

NUEVAS = (
    "trade_item",
    "trade_item_alias",
    "supplier",
    "source",
    "supplier_product_mapping",
    "shipping_terms_snapshot",
    "offer_snapshot",
    "chain_product",
    "reference_price",
    "import_run",
)


async def _exec(engine, sql, **params):
    async with engine.begin() as c:
        return await c.execute(text(sql), params)


async def _scalar(engine, sql, **params):
    return (await _exec(engine, sql, **params)).scalar()


async def _item(engine, org, key="loratadina-10mg-x30", **over):
    cols = dict(display_name="Loratadina 10 mg x 30", identity_key=key, data_origin="simulated")
    cols.update(over)
    names, vals = ", ".join(cols), ", ".join(f":{k}" for k in cols)
    return await _scalar(
        engine,
        f"INSERT INTO trade_item (organization_id, {names}) VALUES (:org, {vals}) RETURNING id",
        org=org,
        **cols,
    )


async def _supplier(engine, org, name="Proveedor X"):
    return await _scalar(
        engine,
        "INSERT INTO supplier (organization_id, name, data_origin) VALUES (:o, :n, 'simulated') RETURNING id",
        o=org,
        n=name,
    )


async def _source(engine, org, name, kind, supplier_id=None):
    return await _scalar(
        engine,
        """INSERT INTO source (organization_id, kind, supplier_id, name, mode, data_origin)
           VALUES (:o, :k, :s, :n, 'manual_import', 'simulated') RETURNING id""",
        o=org,
        k=kind,
        s=supplier_id,
        n=name,
    )


async def _chain_product(engine, org, source_id, external_id="SKU-1", **over):
    cols = dict(
        external_id=external_id,
        source_url="https://cadena.cl/p/1",
        published_name="Losartan (B) 50 mg, 30 comprimidos recubiertos",
        content_hash=HASH_A,
        observed_at=text("now()"),
        data_origin="simulated",
    )
    cols.update(over)
    names = list(cols)
    vals = ["now()" if k == "observed_at" else f":{k}" for k in cols]
    return await _scalar(
        engine,
        f"INSERT INTO chain_product (organization_id, source_id, {', '.join(names)}) "
        f"VALUES (:org, :src, {', '.join(vals)}) RETURNING id",
        org=org,
        src=source_id,
        **{k: v for k, v in cols.items() if k != "observed_at"},
    )


async def _reference_price(engine, org, source_id, chain_product_id, **over):
    cols = dict(
        price_normal=3490,
        currency="CLP",
        source_url="https://cadena.cl/p/1",
        observed_at=text("now()"),
        data_origin="simulated",
    )
    cols.update(over)
    names = [k for k in cols]
    vals = ["now()" if k == "observed_at" else f":{k}" for k in cols]
    return await _scalar(
        engine,
        f"INSERT INTO reference_price (organization_id, source_id, chain_product_id, "
        f"{', '.join(names)}) VALUES (:org, :src, :cp, {', '.join(vals)}) RETURNING id",
        org=org,
        src=source_id,
        cp=chain_product_id,
        **{k: v for k, v in cols.items() if k != "observed_at"},
    )


async def _offer(engine, org, supplier_id, source_id, trade_item_id, **over):
    cols = dict(
        external_id="SKU-9",
        price_amount=3490,
        currency="CLP",
        sale_unit="box",
        observed_at=text("now()"),
        data_origin="simulated",
    )
    cols.update(over)
    names = [k for k in cols]
    vals = ["now()" if k == "observed_at" else f":{k}" for k in cols]
    return await _scalar(
        engine,
        f"INSERT INTO offer_snapshot (organization_id, supplier_id, source_id, trade_item_id, "
        f"{', '.join(names)}) VALUES (:org, :sup, :src, :item, {', '.join(vals)}) RETURNING id",
        org=org,
        sup=supplier_id,
        src=source_id,
        item=trade_item_id,
        **{k: v for k, v in cols.items() if k != "observed_at"},
    )


# ---------- estructura y separación de planos ----------


async def test_las_diez_tablas_existen_y_las_de_faltantes_no_se_tocan(engine, seed):
    rows = await _exec(
        engine,
        "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename = ANY(:t)",
        t=list(NUEVAS),
    )
    assert sorted(r[0] for r in rows) == sorted(NUEVAS)
    # Ninguna FK nueva entra al módulo de faltantes (D-095/D-096: no se modifica).
    toca = await _scalar(
        engine,
        """
        SELECT count(*) FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE c.contype = 'f' AND t.relname IN
              ('demand_item','demand_observation','pharma_identity','commercial_reference','product_alias')
          AND confrelid IN (SELECT oid FROM pg_class WHERE relname = ANY(:t))
        """,
        t=list(NUEVAS),
    )
    assert toca == 0


async def test_reference_price_exige_la_ficha_y_no_admite_articulo(engine):
    rows = await _exec(
        engine,
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name='reference_price'",
    )
    cols = {name: nullable for name, nullable in rows}
    assert cols["chain_product_id"] == "NO"
    assert "trade_item_id" not in cols  # el vínculo vive solo en la ficha


async def test_precio_publico_y_oferta_de_proveedor_no_se_mezclan(engine, seed):
    org = seed["org"]
    sup = await _supplier(engine, org)
    src_sup = await _source(engine, org, "Lista X", "supplier", sup)
    src_chain = await _source(engine, org, "Cadena Y", "chain_reference")
    item = await _item(engine, org)
    cp = await _chain_product(engine, org, src_chain)
    await _reference_price(engine, org, src_chain, cp)
    # Una referencia de cadena nunca aparece como oferta: la oferta solo cuelga de proveedor.
    with pytest.raises(DBAPIError, match="fuentes de proveedor"):
        await _offer(engine, org, sup, src_chain, item)
    # Y una fuente de proveedor no produce fichas ni precios públicos.
    with pytest.raises(DBAPIError, match="chain_reference"):
        await _chain_product(engine, org, src_sup, external_id="SKU-2")
    with pytest.raises(DBAPIError, match="chain_reference"):
        await _reference_price(engine, org, src_sup, cp)


# ---------- vínculo ficha ↔ artículo: explícito o confirmado ----------


async def test_ficha_no_declara_equivalencia_sin_procedencia(engine, seed):
    org = seed["org"]
    src = await _source(engine, org, "Cadena Y", "chain_reference")
    with pytest.raises(DBAPIError, match="match_coherente"):
        await _chain_product(engine, org, src, match_status="exact")  # sin artículo
    with pytest.raises(DBAPIError):
        await _chain_product(
            engine, org, src, external_id="SKU-2", trade_item_id=None, match_status="unmapped", mapping_origin="inferred"
        )  # fmt: skip


async def test_vinculo_coherente_exige_articulo_autor_y_fecha(engine, seed):
    org = seed["org"]
    src = await _source(engine, org, "Cadena Y", "chain_reference")
    item = await _item(engine, org)
    with pytest.raises(DBAPIError, match="vinculo_"):
        await _chain_product(engine, org, src, match_status="exact", trade_item_id=item, mapping_origin="explicit")
    with pytest.raises(DBAPIError):
        await _chain_product(engine, org, src, external_id="SKU-2", match_status="unmapped", trade_item_id=item)


# ---------- excepción controlada de mutabilidad ----------


async def test_contenido_inmutable_pero_el_vinculo_cambia_con_version(engine, seed):
    org, pao = seed["org"], seed["pao"]
    src = await _source(engine, org, "Cadena Y", "chain_reference")
    item = await _item(engine, org)
    cp = await _chain_product(engine, org, src)

    with pytest.raises(DBAPIError, match="inmutable"):
        await _exec(engine, "UPDATE chain_product SET published_name='otro' WHERE id=:i", i=cp)
    with pytest.raises(DBAPIError, match="no se elimina"):
        await _exec(engine, "DELETE FROM chain_product WHERE id=:i", i=cp)

    # Vincular sin subir la versión se rechaza.
    with pytest.raises(DBAPIError, match="versión en 1"):
        await _exec(
            engine,
            """UPDATE chain_product SET trade_item_id=:t, match_status='exact',
               mapping_origin='explicit', mapped_by=:a, mapped_at=now()
               WHERE id=:i""",
            t=item,
            a=pao,
            i=cp,
        )
    await _exec(
        engine,
        """UPDATE chain_product SET trade_item_id=:t, match_status='exact',
           mapping_origin='explicit', mapped_by=:a, mapped_at=now(), version=2
           WHERE id=:i""",
        t=item,
        a=pao,
        i=cp,
    )
    row = (
        await _exec(engine, "SELECT trade_item_id, match_status, version FROM chain_product WHERE id=:i", i=cp)
    ).one()
    assert row == (item, "exact", 2)


# ---------- integridad por organización y por cadena ----------


async def test_precio_no_puede_apuntar_a_la_ficha_de_otra_cadena(engine, seed):
    org = seed["org"]
    src_a = await _source(engine, org, "Cadena A", "chain_reference")
    src_b = await _source(engine, org, "Cadena B", "chain_reference")
    cp = await _chain_product(engine, org, src_a)
    with pytest.raises(DBAPIError):
        await _reference_price(engine, org, src_b, cp)


async def test_organizaciones_aisladas(engine, seed):
    org, other = seed["org"], seed["other_org"]
    src = await _source(engine, other, "Cadena Y", "chain_reference")
    cp = await _chain_product(engine, other, src)
    # Misma fuente y ficha, pero organización distinta ⇒ la FK compuesta no encuentra la fila.
    with pytest.raises(DBAPIError):
        await _reference_price(engine, org, src, cp)


async def test_mapping_no_puede_declarar_otro_proveedor_que_su_fuente(engine, seed):
    org = seed["org"]
    sup_a = await _supplier(engine, org, "Proveedor A")
    sup_b = await _supplier(engine, org, "Proveedor B")
    src_a = await _source(engine, org, "Lista A", "supplier", sup_a)
    item = await _item(engine, org)
    with pytest.raises(DBAPIError):
        await _exec(
            engine,
            """INSERT INTO supplier_product_mapping
               (organization_id, source_id, supplier_id, trade_item_id, external_id, data_origin)
               VALUES (:o, :s, :b, :t, 'SKU-1', 'simulated')""",
            o=org,
            s=src_a,
            b=sup_b,
            t=item,
        )


# ---------- ofertas: nada desconocido se rellena ----------


async def test_oferta_con_minimo_y_stock_desconocidos_no_inventa_valores(engine, seed):
    org = seed["org"]
    sup = await _supplier(engine, org)
    src = await _source(engine, org, "Lista X", "supplier", sup)
    item = await _item(engine, org)
    await _offer(engine, org, sup, src, item)  # minimum known=false, stock unknown, impuestos NULL
    for bad in (
        dict(minimum_known=True),  # conocido sin cantidad
        dict(stock_status="known"),  # conocido sin cantidad
        dict(minimum_known=False, minimum_quantity=5),  # desconocido con cantidad
        dict(multiple_known=True, multiple_quantity=0),  # múltiplo 0 no existe
        dict(currency="pesos"),
    ):
        with pytest.raises(DBAPIError):
            await _offer(engine, org, sup, src, item, external_id=f"SKU-{len(bad)}", **bad)


# ---------- inmutabilidad y procedencia ----------


async def test_snapshots_son_de_solo_agregar(engine, seed):
    org = seed["org"]
    sup = await _supplier(engine, org)
    src = await _source(engine, org, "Lista X", "supplier", sup)
    item = await _item(engine, org)
    offer = await _offer(engine, org, sup, src, item)
    cp_source = await _source(engine, org, "Cadena Y", "chain_reference")
    cp = await _chain_product(engine, org, cp_source)
    price = await _reference_price(engine, org, cp_source, cp)
    for table, row in (("offer_snapshot", offer), ("reference_price", price)):
        with pytest.raises(DBAPIError, match="solo agregar"):
            await _exec(engine, f"UPDATE {table} SET created_at=now() WHERE id=:i", i=row)
        with pytest.raises(DBAPIError, match="solo agregar"):
            await _exec(engine, f"DELETE FROM {table} WHERE id=:i", i=row)


async def test_import_run_se_inserta_una_vez_por_archivo(engine, seed):
    org = seed["org"]
    sup = await _supplier(engine, org)
    src = await _source(engine, org, "Lista X", "supplier", sup)
    ins = """INSERT INTO import_run (organization_id, source_id, file_name, file_hash, status,
             rows_total, rows_accepted, data_origin)
             VALUES (:o, :s, 'lista.xlsx', :h, 'completed', 10, 10, 'simulated')"""
    run = await _scalar(engine, ins + " RETURNING id", o=org, s=src, h=HASH_B)
    with pytest.raises(DBAPIError):
        await _exec(engine, ins, o=org, s=src, h=HASH_B)
    with pytest.raises(DBAPIError, match="solo agregar"):
        await _exec(engine, "UPDATE import_run SET status='failed' WHERE id=:i", i=run)


async def test_procedencia_debe_coincidir_con_la_organizacion(engine, seed, real_seed):
    org = seed["org"]  # simulación: solo simulated
    with pytest.raises(DBAPIError, match="simulación"):
        await _item(engine, org, data_origin="captured", key="k-capturada")
    # organización de prueba real: los datos simulados no entran
    with pytest.raises(DBAPIError, match="no admite data_origin=simulated"):
        await _item(engine, real_seed["org"], data_origin="simulated", key="k-simulada")
    ok = await _item(engine, real_seed["org"], data_origin="captured", key="k-real")
    assert ok is not None
