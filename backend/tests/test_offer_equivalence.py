"""Equivalencia farmacéutica de ofertas vivas (A6/D-152, sin vínculos inventados).

La firma se compara por atributos deterministas: medicamento, concentración, forma, liberación y
contenido. El laboratorio no separa la firma porque es el sujeto de la preferencia; la identidad
comercial (artículo, vínculo y precio) sigue siendo de cada oferta.
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.domain.products import parse_shortage_text
from app.models import (
    OfferSnapshot,
    Organization,
    SupplierProductMapping,
    TradeItem,
)
from app.services.offer_equivalence import (
    item_signature,
    offer_signature,
    resolve_equivalent_item_ids,
)

pytestmark = pytest.mark.db


def sig(name, presentation=None, laboratory=None):
    return offer_signature(name=name, presentation=presentation, laboratory=laboratory)


def test_el_laboratorio_no_separa_la_firma_pero_el_producto_si():
    ascend = sig("Ibuprofeno 400 mg x 20 comprimidos", laboratory="ASCEND")
    otro = sig("Ibuprofeno 400 mg x 20 comprimidos", laboratory="Laboratorio Ficticio")
    assert ascend is not None
    assert ascend == otro  # misma presentación: la preferencia de laboratorio puede ordenar

    assert sig("Ibuprofeno 200 mg x 20 comprimidos", laboratory="ASCEND") != ascend
    assert sig("Ibuprofeno 400 mg x 10 comprimidos", laboratory="ASCEND") != ascend
    assert sig("Ibuprofeno 400 mg jarabe x 120 ml", laboratory="ASCEND") != ascend
    assert sig("Paracetamol 400 mg x 20 comprimidos", laboratory="ASCEND") != ascend


def test_el_laboratorio_pegado_al_nombre_se_retira_como_entidad():
    limpio = sig("Ibuprofeno 400 mg x 20 comprimidos", laboratory="ASCEND")
    pegado = sig("Ibuprofeno Ascend 400 mg x 20 comprimidos", laboratory="ASCEND")
    assert pegado is not None
    assert pegado == limpio


def test_la_presentacion_publicada_completa_el_contenido():
    completo = sig("Ibuprofeno 400 mg x 20 comprimidos", laboratory="ASCEND")
    aparte = sig(
        "Ibuprofeno 400 mg", presentation="Caja de 20 comprimidos", laboratory="ASCEND"
    )
    assert aparte is not None
    assert aparte == completo


def test_un_conteo_sin_x_sigue_siendo_la_misma_presentacion():
    con_x = sig("Ibuprofeno 400 mg x 20 comprimidos", laboratory="ASCEND")
    sin_x = sig("Ibuprofeno 400 mg 20 comprimidos", laboratory="ASCEND")
    assert sin_x is not None
    assert sin_x == con_x


def test_sin_identidad_completa_no_hay_firma():
    # Nombre parcial: no se completa por suposición (D-015 a D-024).
    assert sig("Ibuprofeno", laboratory="ASCEND") is None
    assert sig("Ibuprofeno 400 mg", laboratory="ASCEND") is None
    assert sig("Crema de manos", laboratory="ASCEND") is None
    assert sig(None, laboratory="ASCEND") is None


def test_el_articulo_se_lee_de_sus_atributos_estructurados():
    parsed = parse_shortage_text("Ibuprofeno 400 mg x 20 comprimidos")
    por_atributos = item_signature(
        display_name="Nombre comercial sin datos",
        laboratory="ASCEND",
        attributes=parsed.to_dict(),
    )
    por_nombre = item_signature(
        display_name="Ibuprofeno 400 mg x 20 comprimidos",
        laboratory="ASCEND",
        attributes={"schema_version": 2},
    )
    assert por_atributos is not None
    assert por_atributos == por_nombre


async def _org(sessions, label="Farmacia equivalencia"):
    async with sessions() as s, s.begin():
        org = Organization(name=f"{label} {uuid.uuid4().hex[:8]}", kind="simulation")
        s.add(org)
        await s.flush()
        org_id = org.id
    return org_id


async def _item(sessions, org_id, name, *, identity, laboratory=None, attributes=None):
    async with sessions() as s, s.begin():
        item = TradeItem(
            organization_id=org_id,
            display_name=name,
            identity_key=identity,
            laboratory=laboratory,
            attributes=attributes or {"schema_version": 2},
            data_origin="simulated",
        )
        s.add(item)
        await s.flush()
        item_id = item.id
    return item_id


def entry(external_id, name, presentation=None, laboratory=None, trade_item_id=None):
    return {
        "external_id": external_id,
        "name": name,
        "presentation": presentation,
        "laboratory": laboratory,
        "trade_item_id": trade_item_id,
    }


async def test_resuelve_solo_el_mismo_producto_y_con_firma_unica(sessions):
    org_id = await _org(sessions)
    canonical = await _item(
        sessions, org_id, "Ibuprofeno 400 mg x 20 comprimidos", identity="vivo-ibu-400-20"
    )

    # Solo hay un artículo en el catálogo: la concentración, el contenido, la forma y el nombre
    # parcial no se completan por suposición y no resuelven.
    entries = [
        entry("EQUIV", "Ibuprofeno 400 mg x 20 comprimidos", laboratory="Otro laboratorio"),
        entry("OTRA", "Ibuprofeno 200 mg x 20 comprimidos", laboratory="ASCEND"),
        entry("OTRA2", "Ibuprofeno 400 mg x 10 comprimidos", laboratory="ASCEND"),
        entry("OTRA3", "Ibuprofeno 400 mg jarabe x 120 ml", laboratory="ASCEND"),
        entry("PARCIAL", "Ibuprofeno", laboratory="ASCEND"),
    ]
    async with sessions() as s:
        resolved = await resolve_equivalent_item_ids(s, org_id, entries)
    assert resolved == {"EQUIV": canonical}


async def test_un_vinculo_exacto_no_se_reemplaza_ni_se_duplica(sessions):
    org_id = await _org(sessions)
    canonical = await _item(
        sessions, org_id, "Ibuprofeno 400 mg x 20 comprimidos", identity="vivo-ibu-400-20"
    )
    entries = [
        entry("YA", "Ibuprofeno 400 mg x 20 comprimidos", trade_item_id=canonical),
        entry("NUEVA", "Ibuprofeno 400 mg x 20 comprimidos"),
    ]
    async with sessions() as s:
        resolved = await resolve_equivalent_item_ids(s, org_id, entries)
    assert resolved == {"NUEVA": canonical}


async def test_dos_articulos_con_la_misma_firma_son_ambiguos(sessions):
    org_id = await _org(sessions)
    await _item(sessions, org_id, "Ibuprofeno 400 mg x 20 comprimidos", identity="vivo-ibu-a")
    await _item(sessions, org_id, "Ibuprofeno 400 mg x 20 comprimidos", identity="vivo-ibu-b")
    entries = [entry("EQUIV", "Ibuprofeno 400 mg x 20 comprimidos")]
    async with sessions() as s:
        resolved = await resolve_equivalent_item_ids(s, org_id, entries)
    assert resolved == {}


async def test_un_articulo_inactivo_o_de_otra_organizacion_no_resuelve(sessions):
    org_id = await _org(sessions)
    other_id = await _org(sessions)
    await _item(sessions, other_id, "Ibuprofeno 400 mg x 20 comprimidos", identity="ajeno-ibu")
    async with sessions() as s, s.begin():
        inactive = TradeItem(
            organization_id=org_id,
            display_name="Ibuprofeno 400 mg x 20 comprimidos",
            identity_key="vivo-inactivo",
            attributes={"schema_version": 2},
            status="inactive",
            data_origin="simulated",
        )
        s.add(inactive)
    entries = [entry("EQUIV", "Ibuprofeno 400 mg x 20 comprimidos")]
    async with sessions() as s:
        resolved = await resolve_equivalent_item_ids(s, org_id, entries)
    assert resolved == {}


async def test_la_resolucion_no_escribe_nada(sessions):
    org_id = await _org(sessions)
    await _item(sessions, org_id, "Ibuprofeno 400 mg x 20 comprimidos", identity="vivo-ibu")
    entries = [entry("EQUIV", "Ibuprofeno 400 mg x 20 comprimidos")]
    async with sessions() as s:
        await resolve_equivalent_item_ids(s, org_id, entries)
    async with sessions() as s:
        mappings = (
            await s.execute(select(func.count()).select_from(SupplierProductMapping))
        ).scalar_one()
        snapshots = (
            await s.execute(select(func.count()).select_from(OfferSnapshot))
        ).scalar_one()
    assert mappings == 0
    assert snapshots == 0
