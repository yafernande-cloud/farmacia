"""La migración 0002 convierte datos reales de H1-A sin conservar suposiciones como hechos."""

import json

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from app.domain.products import ParsedProduct, parse_shortage_text
from tests.conftest import alembic_config

pytestmark = pytest.mark.db

# Filas tal como las dejaba H1-A: (nombre, ingrediente, valor normalizado, unidad, texto, inferida,
# forma, contenido, liberación, clave legacy, texto original del auxiliar)
LEGACY = [
    ("Ibuprofeno 400 mg comprimido x 20", "ibuprofeno", "400", "mg", "400 mg", False, "comprimido", 20, None,
     "ibuprofeno|400mg|comprimido|20|-", "falta ibuprofeno 400 mg comprimidos x 20"),
    ("Ibuprofeno 400 mg", "ibuprofeno", "400", "mg", "400 mg", True, None, None, None, None,
     "falta ibuprofeno de 400"),  # el "mg" fue SUPUESTO por H1-A
    ("Suero Fisiologico 500 ml", "suero fisiologico", "500", "ml", "500 ml", False, None, None, None, None,
     "suero fisiologico 500 ml"),  # un volumen tratado como potencia
    ("Ibuprofeno 20 mg/ml jarabe x 120", "ibuprofeno", "20", "mg/ml", "100 mg/5 ml", False, "jarabe", 120, None,
     "ibuprofeno|20mg/ml|jarabe|120|-", "ibuprofeno 100 mg/5 ml jarabe x 120"),
    ("Paracetamol", "paracetamol", None, None, None, False, None, None, None, None, "falta paracetamol"),
]  # fmt: skip


def test_migracion_de_datos_legacy(db_url):
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "0001")
    eng = create_engine(db_url)
    with eng.begin() as c:
        c.execute(text("TRUNCATE organization CASCADE"))
        org = c.execute(
            text("INSERT INTO organization (name) VALUES ('legacy') RETURNING id")
        ).scalar()
        user = c.execute(
            text(
                "INSERT INTO app_user (organization_id, display_name, roles) VALUES (:o,'Ana','{auxiliary}') RETURNING id"
            ),
            {"o": org},
        ).scalar()
        cyc = c.execute(
            text("INSERT INTO purchase_cycle (organization_id, name) VALUES (:o,'c') RETURNING id"),
            {"o": org},
        ).scalar()
        for i, row in enumerate(LEGACY):
            name, ing, sv, su, st, inf, form, cont, rel, key, original = row
            item = c.execute(
                text("""INSERT INTO demand_item (organization_id, cycle_id, display_name, ingredient, strength_value,
                        strength_unit, strength_text, strength_unit_inferred, dosage_form, content_amount, release,
                        resolved_identity_key) VALUES (:o,:c,:n,:i,:sv,:su,:st,:inf,:f,:ct,:r,:k) RETURNING id"""),
                {
                    "o": org,
                    "c": cyc,
                    "n": name,
                    "i": ing,
                    "sv": sv,
                    "su": su,
                    "st": st,
                    "inf": inf,
                    "f": form,
                    "ct": cont,
                    "r": rel,
                    "k": key,
                },
            ).scalar()
            c.execute(
                text("""INSERT INTO demand_observation (organization_id, cycle_id, demand_item_id, author_id, raw_text,
                        parsed, match_status, policy_version, idempotency_key, request_hash)
                        VALUES (:o,:c,:it,:u,:t,'{"old": true}','created_item','h1a-conservative-1',:k,'h')"""),
                {"o": org, "c": cyc, "it": item, "u": user, "t": original, "k": f"k{i}"},
            )

    command.upgrade(cfg, "head")

    with eng.connect() as c:
        rows = {
            r.display_name: r
            for r in c.execute(
                text(
                    "SELECT display_name, ingredient, attributes, resolved_identity_key FROM demand_item"
                )
            ).mappings()
        }
        legacy_parsed = c.execute(text("SELECT parsed FROM demand_observation LIMIT 1")).scalar()
        cols = {
            r[0]
            for r in c.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='demand_item'"
                )
            )
        }
    assert (
        legacy_parsed["schema_version"] == 1 and legacy_parsed["old"] is True
    )  # se marca, no se pierde
    assert not cols & {
        "strength_value",
        "strength_unit",
        "strength_text",
        "dosage_form",
        "content_amount",
        "release",
    }

    # (a) lo explícito se conserva como explícito, con la MISMA clave que genera el dominio hoy
    a = rows["Ibuprofeno 400 mg comprimido x 20"]
    pa = ParsedProduct.from_dict(a["attributes"])
    assert (
        pa.strengths[0].basis.value == "explicit"
        and pa.strengths[0].measure.original_text == "400 mg"
    )
    assert a["attributes"]["original_text"] == "falta ibuprofeno 400 mg comprimidos x 20"
    assert (
        a["resolved_identity_key"]
        == parse_shortage_text("ibuprofeno 400 mg comprimidos x 20").resolved_identity_key
    )

    # (b) el "mg" supuesto NO sobrevive como hecho: queda como cifra sin unidad, pendiente
    b = rows["Ibuprofeno 400 (sin unidad)"]
    attrs = b["attributes"]
    assert attrs["strengths"] == [] and attrs["unassigned"] == [
        {"value": "400", "original_text": "400"}
    ]
    assert b["resolved_identity_key"] is None and "mg" not in json.dumps(attrs)

    # (c) un volumen que H1-A llamaba "potencia" pasa a pendiente (con su unidad real), no a potencia
    c_ = rows["Suero Fisiologico 500 ml (rol por confirmar)"]
    assert c_["attributes"]["strengths"] == [] and c_["attributes"]["unassigned"][0]["unit"] == "ml"

    # (d) concentración normalizada → misma clave que hoy produce el intérprete
    d = rows["Ibuprofeno 20 mg/ml jarabe x 120"]
    assert (
        d["resolved_identity_key"]
        == parse_shortage_text("ibuprofeno 100 mg/5 ml jarabe x 120").resolved_identity_key
    )
    (s,) = ParsedProduct.from_dict(d["attributes"]).strengths
    assert s.kind == "concentration" and s.measure.original_text == "100 mg/5 ml"

    # (e) un producto sin atributos sigue sin atributos (nada inventado)
    e = ParsedProduct.from_dict(rows["Paracetamol"]["attributes"])
    assert e.strengths == () and e.unassigned == () and e.form is None

    # ida y vuelta: baja a 0001 y vuelve a subir sin romperse
    command.downgrade(cfg, "0001")
    with eng.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT dosage_form FROM demand_item WHERE ingredient='ibuprofeno' AND content_amount=20"
                )
            ).scalar()
            == "comprimido"
        )
    command.upgrade(cfg, "head")
    eng.dispose()
