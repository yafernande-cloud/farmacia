"""Carga inicial del catálogo propio (`catalog-load`) — PostgreSQL real.

Comprueba el contrato de entrada A (D-101): alta idempotente de artículos y vínculos SKU, conflictos que
no se sobrescriben, `--dry-run` que no escribe, rechazos por línea con su número, y procedencia por tipo
de organización. No hay ofertas, planillas ni inferencias en juego: solo `trade_item` y
`supplier_product_mapping`.
"""

import json

import pytest
from sqlalchemy import func, select

from app.core.errors import DomainError
from app.models import (
    AppUser,
    AuditEvent,
    Organization,
    OutboxEvent,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
)
from app.services.actors import load_actor
from app.services.catalog_load import load_catalog

pytestmark = pytest.mark.db


@pytest.fixture
async def orgs(sessions):
    """Simulación (catálogo en juego), otra simulación (ajena) y una de prueba real; con sus fuentes."""
    ids = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia catálogo", kind="simulation")
        other = Organization(name="Farmacia ajena", kind="simulation")
        real = Organization(name="Farmacia real catálogo", kind="real_test")
        s.add_all([org, other, real])
        await s.flush()
        ids["org"], ids["other"], ids["real"] = org.id, other.id, real.id
        for key, name, roles, o in [
            ("admin", "Yarella", ["admin"], org),
            ("pao", "Pao", ["admin", "purchasing"], org),
            ("aux", "Danny", ["auxiliary"], org),
            ("intruso", "Intruso", ["admin"], other),
            ("real_admin", "Yarella real", ["admin"], real),
        ]:
            u = AppUser(organization_id=o.id, display_name=name, roles=roles)
            s.add(u)
            await s.flush()
            ids[key] = u.id
        sup = Supplier(organization_id=org.id, name="Proveedor Uno", data_origin="simulated")
        ajeno = Supplier(organization_id=other.id, name="Proveedor Ajeno", data_origin="simulated")
        s.add_all([sup, ajeno])
        await s.flush()
        ids["supplier"] = sup.id
        for key, kind, name in [
            ("source", "supplier", "Lista Proveedor Uno"),
            ("chain", "chain_reference", "Catálogo de cadena"),
        ]:
            src = Source(
                organization_id=org.id,
                kind=kind,
                supplier_id=sup.id if kind == "supplier" else None,
                name=name,
                mode="manual_import",
                data_origin="simulated",
            )
            s.add(src)
            await s.flush()
            ids[key] = src.id
        ajena = Source(
            organization_id=other.id,
            kind="supplier",
            supplier_id=ajeno.id,
            name="Lista Ajena",
            mode="manual_import",
            data_origin="simulated",
        )
        s.add(ajena)
        await s.flush()
        ids["source_ajena"] = ajena.id
    return ids


def art(key: str, name: str, **over) -> str:
    rec = {"type": "article", "identity_key": key, "display_name": name}
    rec.update(over)
    return json.dumps(rec, ensure_ascii=False)


def link(source_id, external_id: str, item_key: str, **over) -> str:
    rec = {
        "type": "mapping",
        "source_id": str(source_id),
        "external_id": external_id,
        "item_identity_key": item_key,
    }
    rec.update(over)
    return json.dumps(rec, ensure_ascii=False)


async def load(sessions, who, lines, *, dry_run=False):
    async with sessions() as s:
        actor = await load_actor(s, who)
        try:
            report = await load_catalog(s, actor, lines, dry_run=dry_run)
            await s.commit()
            return report
        except BaseException:
            await s.rollback()
            raise


async def count(sessions, model, *where) -> int:
    async with sessions() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def one(sessions, model, *where):
    async with sessions() as s:
        return (await s.execute(select(model).where(*where))).scalar_one_or_none()


async def codes(report) -> list[str]:
    return [e["code"] for e in report.errors]


# ---------- permiso ----------


async def test_solo_administracion_carga_el_catalogo(sessions, orgs):
    with pytest.raises(DomainError) as e:
        await load(sessions, orgs["aux"], [art("K1", "Losartán 50 mg")])
    assert e.value.code == "FORBIDDEN"
    assert await count(sessions, TradeItem) == 0


# ---------- alta ----------


async def test_crea_articulos_y_vinculos_con_su_auditoria(sessions, orgs):
    report = await load(
        sessions,
        orgs["admin"],
        [
            art(
                "LOS-50-30",
                "Losartán 50 mg x 30 comprimidos",
                product_kind="medicine",
                ingredient="losartán",
                laboratory="Laboratorio Chile",
                gtin="00780012345678",
            ),
            art("CEPI-100", "Cepillo dental suave"),
            link(orgs["source"], "SKU-001", "LOS-50-30", external_name="LOSARTAN 50 X 30"),
        ],
    )
    assert (report.articles_created, report.articles_conflicting) == (2, 0)
    assert (report.mappings_created, report.mappings_conflicting) == (1, 0)
    assert report.errors == []

    item = await one(sessions, TradeItem, TradeItem.identity_key == "LOS-50-30")
    assert item.display_name == "Losartán 50 mg x 30 comprimidos"
    assert item.product_kind == "medicine"
    assert item.gtin == "00780012345678"
    assert item.attributes == {"schema_version": 2}
    assert (item.data_origin, item.created_by, item.status) == ("simulated", orgs["admin"], "active")
    # Producto no farmacéutico: sin `product_kind` ni atributos farmacológicos (D-094).
    cepillo = await one(sessions, TradeItem, TradeItem.identity_key == "CEPI-100")
    assert (cepillo.product_kind, cepillo.ingredient) == (None, None)

    m = await one(sessions, SupplierProductMapping)
    assert m.trade_item_id == item.id
    assert (m.origin, m.confirmed_by, m.confirmed_at) == ("explicit", None, None)
    assert m.supplier_id == orgs["supplier"]
    assert m.external_name == "LOSARTAN 50 X 30"

    assert await count(sessions, AuditEvent, AuditEvent.action == "trade_item.created") == 2
    assert await count(sessions, AuditEvent, AuditEvent.action == "supplier_product_mapping.created") == 1
    assert await count(sessions, OutboxEvent, OutboxEvent.type == "catalog.trade_item_created") == 2


async def test_la_procedencia_la_fija_la_organizacion(sessions, orgs):
    """En una organización real lo cargado es `captured`; en simulación, `simulated`."""
    await load(sessions, orgs["real_admin"], [art("REAL-1", "Ibuprofeno 400 mg")])
    item = await one(sessions, TradeItem, TradeItem.identity_key == "REAL-1")
    assert item.data_origin == "captured"


# ---------- idempotencia y conflictos ----------


async def test_volver_a_cargar_lo_mismo_no_duplica_ni_audita(sessions, orgs):
    lines = [
        art("LOS-50-30", "Losartán 50 mg x 30", ingredient="losartán"),
        link(orgs["source"], "SKU-001", "LOS-50-30"),
    ]
    await load(sessions, orgs["admin"], lines)
    report = await load(sessions, orgs["admin"], lines)
    assert (report.articles_created, report.articles_unchanged) == (0, 1)
    assert (report.mappings_created, report.mappings_unchanged) == (0, 1)
    assert await count(sessions, TradeItem) == 1
    assert await count(sessions, SupplierProductMapping) == 1
    assert await count(sessions, AuditEvent, AuditEvent.action == "trade_item.created") == 1


async def test_una_diferencia_no_sobrescribe_se_informa_como_conflicto(sessions, orgs):
    await load(sessions, orgs["admin"], [art("LOS-50-30", "Losartán 50 mg x 30")])
    report = await load(
        sessions, orgs["admin"], [art("LOS-50-30", "Losartán 50 mg x 30 (otro envase)")]
    )
    assert (report.articles_created, report.articles_conflicting) == (0, 1)
    assert await codes(report) == ["CONFLICT"]
    item = await one(sessions, TradeItem, TradeItem.identity_key == "LOS-50-30")
    assert item.display_name == "Losartán 50 mg x 30"


async def test_el_mismo_codigo_de_fuente_no_se_reapunta(sessions, orgs):
    await load(sessions, orgs["admin"], [art("A", "Artículo A"), art("B", "Artículo B")])
    await load(sessions, orgs["admin"], [link(orgs["source"], "SKU-1", "A")])
    report = await load(sessions, orgs["admin"], [link(orgs["source"], "SKU-1", "B")])
    assert report.mappings_conflicting == 1
    m = await one(sessions, SupplierProductMapping)
    assert m.trade_item_id == (await one(sessions, TradeItem, TradeItem.identity_key == "A")).id


async def test_una_clave_repetida_en_el_archivo_se_rechaza(sessions, orgs):
    report = await load(
        sessions,
        orgs["admin"],
        [art("K", "Primero"), art("K", "Segundo")],
    )
    assert (report.articles_created, report.errors[0]["line"]) == (1, 2)
    assert await codes(report) == ["DUPLICATE_RECORD"]
    assert (await one(sessions, TradeItem)).display_name == "Primero"


# ---------- vista previa ----------


async def test_dry_run_cuenta_igual_pero_no_escribe(sessions, orgs):
    report = await load(
        sessions,
        orgs["admin"],
        [art("K1", "Artículo K1"), link(orgs["source"], "SKU-1", "K1")],
        dry_run=True,
    )
    assert report.dry_run is True
    assert (report.articles_created, report.mappings_created) == (1, 1)
    assert await count(sessions, TradeItem) == 0
    assert await count(sessions, SupplierProductMapping) == 0
    assert await count(sessions, AuditEvent) == 0


# ---------- rechazos por línea ----------

BAD_LINES = [
    "no es json",
    json.dumps(["article"]),
    json.dumps({"type": "oferta", "identity_key": "X"}),
    json.dumps({"type": "article", "display_name": "Sin clave"}),
    json.dumps({"type": "article", "identity_key": "X", "display_name": ""}),
    art("G", "GTIN inválido", gtin="123"),
    art("K", "Tipo inválido", product_kind="crema"),
    art("A", "Atributos inválidos", attributes=[1, 2]),
    art("K", "Campo desconocido", identiy_key="typo"),
    json.dumps({"type": "mapping", "source_id": "no-uuid", "external_id": "1", "item_identity_key": "K"}),
]


async def test_cada_linea_invalida_se_rechaza_con_su_numero_y_la_carga_sigue(sessions, orgs):
    report = await load(sessions, orgs["admin"], [*BAD_LINES, art("BUENO", "Artículo válido")])
    assert report.articles_created == 1
    assert len(report.errors) == len(BAD_LINES)
    assert [e["line"] for e in report.errors] == list(range(1, len(BAD_LINES) + 1))
    assert set(await codes(report)) == {"INVALID_JSON", "INVALID_RECORD"}
    assert await count(sessions, TradeItem) == 1


async def test_un_vinculo_sin_articulo_se_rechaza(sessions, orgs):
    report = await load(sessions, orgs["admin"], [link(orgs["source"], "SKU-9", "NO-EXISTE")])
    assert await codes(report) == ["UNKNOWN_ARTICLE"]
    assert await count(sessions, SupplierProductMapping) == 0


async def test_el_vinculo_puede_venir_antes_que_su_articulo(sessions, orgs):
    report = await load(
        sessions,
        orgs["admin"],
        [link(orgs["source"], "SKU-1", "LUEGO"), art("LUEGO", "Artículo definido al final")],
    )
    assert (report.articles_created, report.mappings_created) == (1, 1)
    assert report.errors == []


async def test_el_vinculo_exige_una_fuente_de_proveedor_de_su_organizacion(sessions, orgs):
    report = await load(
        sessions,
        orgs["admin"],
        [art("K", "Artículo K"), link(orgs["chain"], "SKU-1", "K")],
    )
    assert await codes(report) == ["SOURCE_NOT_SUPPLIER"]

    report = await load(sessions, orgs["admin"], [link(orgs["source_ajena"], "SKU-1", "K")])
    assert await codes(report) == ["UNKNOWN_SOURCE"]
