"""Interpretación contextual, identidad farmacéutica/comercial y contrato de visión.

NOTA SOBRE LAS PRUEBAS DE VISIÓN: todo lo que usa `VisionExtraction`/`SimulatedVisionInterpreter` es
una SIMULACIÓN. Valida que, DADO lo que un proveedor devolvería, las reglas deterministas lo traten
bien. NO prueba que ningún modelo de IA lea imágenes de verdad: no hay proveedor de visión conectado.
"""

import uuid
from decimal import Decimal as D

import pytest

from app.core.errors import DomainError
from app.domain.products import Basis, Relation, compare, valid_gtin
from app.domain.products import parse_shortage_text as p
from app.domain.units import build_measure
from app.interpretation.ai import AIProposal, sanitize_ai
from app.interpretation.engine import interpret_with, suggest_for
from app.interpretation.segments import Intent, classify_intent, split_segments
from app.interpretation.types import AliasRec, CatalogRec, CommercialRec, ItemRec, Source
from app.interpretation.vision import (
    SimulatedVisionInterpreter,
    VisionExtraction,
    VisionField,
    VisionStatus,
    merge_text_and_image,
    read_extraction,
)

VIT_D = CatalogRec(
    uuid.uuid4(), "Vitamina D 50000 ui", "vitamina d", (build_measure(D(50000), "UI"),), "capsula"
)


def ctx(**kw):
    base = dict(aliases=[], catalog=[], items=[], commercial=[])
    base.update(kw)
    return base


# ---------- lenguaje cotidiano (los ejemplos pedidos) ----------


@pytest.mark.parametrize(
    "txt,ingredient,value",
    [
        ("Falta vitamina D 50000", "vitamina d", "50000"),
        ("No queda la D de cincuenta mil", "d", "50000"),
        ("Se acabó el ibuprofeno de 400", "ibuprofeno", "400"),
        ("Nos faltan dos cajas de paracetamol de 500", "paracetamol", "500"),
    ],
)
def test_lenguaje_cotidiano_valor_informado_sin_unidad(txt, ingredient, value):
    r = p(txt)
    assert r.ingredient == ingredient
    (m,) = r.unassigned
    assert m.unit is None and m.value == D(value)  # la unidad NO se escribió
    assert r.strengths == ()  # y no se inventó


def test_cantidad_en_palabras_y_cifra_citan_lo_escrito():
    assert p("No queda la D de cincuenta mil").unassigned[0].original_text == "cincuenta mil"
    r = p("Nos faltan dos cajas de paracetamol de 500")
    assert (r.requested_quantity, r.requested_unit) == (D(2), "box")


def test_con_unidad_escrita_es_explicito():
    assert p("Falta vitamina D 50000 UI").strengths[0].basis is Basis.EXPLICIT
    r = p("No queda el jarabe de ibuprofeno 100 mg/5 ml")
    assert r.form.value == "jarabe" and r.strengths[0].measure.label == "100 mg/5 ml"


@pytest.mark.parametrize("txt,intent", [("hola buenos días", Intent.CHITCHAT), ("¿hay ibuprofeno de 400?", Intent.QUESTION),
                                        ("se acabó el ibuprofeno de 400", Intent.SHORTAGE), ("gracias", Intent.CHITCHAT)])  # fmt: skip
def test_saludos_y_preguntas_no_crean_faltantes(txt, intent):
    assert classify_intent(txt) is intent


def test_varios_productos_en_un_mensaje_se_separan_sin_romper_un_producto():
    assert split_segments("falta ibuprofeno 400 mg, paracetamol 500 mg")[0] == [
        "falta ibuprofeno 400 mg",
        "paracetamol 500 mg",
    ]
    assert split_segments("ibuprofeno 400 mg, 20 comprimidos\nvitamina d 50000 ui")[0] == [
        "ibuprofeno 400 mg 20 comprimidos",
        "vitamina d 50000 ui",
    ]
    assert split_segments("paracetamol y codeína")[0] == [
        "paracetamol y codeína"
    ]  # no se separa por «y»


# ---------- candidatos: el catálogo propone, nunca confirma ----------


def test_catalogo_verificado_propone_la_unidad_sin_aplicarla():
    i = interpret_with("Falta vitamina D 50000", **ctx(catalog=[VIT_D]))
    (c,) = [c for c in i.candidates if c.kind == "measure_assignment"]
    assert c.source is Source.CATALOG and c.verified and c.assignment["unit_expr"] == "ui"
    assert "unit_not_written_by_user" in c.reasons
    assert i.parsed.strengths == () and i.parsed.unassigned[0].unit is None  # NADA confirmado


def test_sin_catalogo_no_se_inventa_candidato():
    i = interpret_with("Falta vitamina D 50000", **ctx())
    assert i.candidates == []


def test_dos_candidatos_incompatibles_dejan_la_identificacion_pendiente():
    otra = CatalogRec(
        uuid.uuid4(), "Vitamina D 50000 mcg", "vitamina d", (build_measure(D(50000), "mcg"),)
    )
    i = interpret_with("falta vitamina d 50000", **ctx(catalog=[VIT_D, otra]))
    assert {c.assignment["unit_expr"] for c in i.candidates} == {"ui", "mcg"}
    assert "multiple_measure_candidates" in i.uncertainties
    assert i.parsed.strengths == ()


def test_candidato_de_la_lista_no_es_verificado():
    item = ItemRec(uuid.uuid4(), p("vitamina d 50000 ui"))
    i = interpret_with("falta vitamina d 50000", **ctx(items=[item]))
    (c,) = i.candidates
    assert c.source is Source.CONTEXT and c.verified is False


def test_expresion_habitual_de_los_auxiliares_requiere_alias_verificado():
    # sin alias: «la D» solo se propone como candidato de nombre (no es equivalencia clínica)
    sin = interpret_with("no queda la D de cincuenta mil", **ctx(catalog=[VIT_D]))
    assert sin.parsed.ingredient == "d" and sin.unrecognized_name_with_candidates
    assert [c.ingredient for c in sin.candidates if c.kind == "ingredient"] == ["vitamina d"]
    assert "not_a_clinical_equivalence" in sin.candidates[0].reasons
    # con alias verificado y activo: se aplica dentro de su alcance
    al = AliasRec(
        uuid.uuid4(), "expression", "d", "", {"ingredient": "vitamina d"}, ("ingredient_name",)
    )
    con = interpret_with("no queda la D de cincuenta mil", **ctx(catalog=[VIT_D], aliases=[al]))
    assert con.parsed.ingredient == "vitamina d" and con.applied[0]["kind"] == "expression"
    assert not con.unrecognized_name_with_candidates
    # el alias NO resuelve la unidad ni ninguna presentación: eso sigue pendiente
    assert con.parsed.strengths == () and con.parsed.content is None


def test_alias_de_medida_confirma_solo_lo_que_se_valido():
    al = AliasRec(
        uuid.uuid4(),
        "measure_unit",
        "vitamina d",
        "50000",
        {"role": "strength", "unit_expr": "UI"},
        ("strength.unit",),
    )
    i = interpret_with("falta vitamina d 50000", **ctx(aliases=[al]))
    s = i.parsed.strengths[0]
    assert s.basis is Basis.CONFIRMED and s.role_rule == "verified_alias"
    assert (
        i.parsed.form is None and i.parsed.content is None
    )  # forma/contenido/presentación NO se validaron
    assert i.parsed.identity_summary()["commercial"]["complete"] is False
    # otro producto o otra cifra: sin efecto
    assert interpret_with("falta ibuprofeno 50000", **ctx(aliases=[al])).parsed.strengths == ()
    assert interpret_with("falta vitamina d 25000", **ctx(aliases=[al])).parsed.strengths == ()


# ---------- IA: puede sugerir, no decidir ----------


def test_ia_degrada_a_candidato_no_verificado_y_no_toca_lo_interpretado():
    parsed = p("falta vitamina d 50000")
    cands, notes = sanitize_ai(
        [AIProposal("measure_assignment", unit_expr="UI", value="50000", claims_confirmed=True)],
        parsed,
    )
    assert (
        cands[0].source is Source.AI
        and cands[0].verified is False
        and "ai_unverified" in cands[0].reasons
    )
    assert "ai_claimed_confirmation_ignored" in notes
    i = interpret_with(
        "falta vitamina d 50000",
        **ctx(ai_proposals=[AIProposal("measure_assignment", unit_expr="UI", value="50000")]),
    )
    assert i.ai_used and i.parsed.strengths == ()  # jamás confirmado por proponerlo un modelo


@pytest.mark.parametrize("prop,note", [
    (AIProposal("measure_assignment", unit_expr="UI", value="99999"), "ai_proposal_not_grounded_in_text"),  # inventa una cifra
    (AIProposal("measure_assignment", unit_expr="xyz", value="50000"), "ai_proposal_invalid_unit"),
    (AIProposal("ingredient", ingredient=""), "ai_proposal_invalid_ingredient"),
    (AIProposal("ejecutar_compra"), "ai_proposal_unknown_kind"),
])  # fmt: skip
def test_ia_propuestas_invalidas_se_descartan_con_motivo(prop, note):
    cands, notes = sanitize_ai([prop], p("falta vitamina d 50000"))
    assert cands == [] and note in notes


# ---------- identidad farmacéutica vs comercial; laboratorio ----------


def test_identidad_farmaceutica_y_comercial_son_distintas():
    r = p("ibuprofeno 400 mg comprimidos x 20 lab saval")
    s = r.identity_summary()
    assert (
        s["pharmaceutical"]["complete"]
        and s["pharmaceutical"]["key"] == "ibuprofeno|mass:400mg|comprimido|-"
    )
    assert s["commercial"]["entities"]["laboratory"]["value"] == "saval"
    assert s["commercial"]["unknown"] == [
        "brand",
        "manufacturer",
        "registration_holder",
        "marketer",
        "gtin",
        "sanitary_registration",
    ]


def test_distinto_laboratorio_no_se_fusiona_como_mismo_sku():
    a = p("ibuprofeno 400 mg comprimidos x 20 lab saval")
    b = p("ibuprofeno 400 mg comprimidos x 20 lab chile")
    assert a.pharma_identity_key == b.pharma_identity_key  # misma identidad farmacéutica…
    c = compare(a, b)
    assert (
        c.relation is Relation.PARTIAL and "laboratory_differs" in c.reasons
    )  # …pero no mismo artículo
    assert (
        a.resolved_identity_key != b.resolved_identity_key
    )  # claves comerciales distintas: pueden coexistir


def test_laboratorio_desconocido_permanece_desconocido():
    r = p("ibuprofeno 400 mg comprimidos x 20")
    assert r.laboratory is None and r.manufacturer is None and r.registration_holder is None


def test_laboratorio_fabricante_titular_y_comercializador_no_se_asumen_iguales():
    ext = VisionExtraction("simulated", "simulator", "m", "clear", (
        VisionField("ingredient", "ibuprofeno", .9), VisionField("strength", "400 mg", .9),
        VisionField("laboratory", "Laboratorio Alfa", .9), VisionField("manufacturer", "Fabricado por Beta S.A.", .9),
        VisionField("marketer", "Distribuye Gamma", .8)))  # fmt: skip
    x = read_extraction(ext).parsed
    assert (x.laboratory.value, x.manufacturer.value, x.marketer.value) == (
        "Laboratorio Alfa",
        "Fabricado por Beta S.A.",
        "Distribuye Gamma",
    )
    assert (
        x.registration_holder is None
    )  # nadie lo dijo: no se deduce del laboratorio ni del fabricante


@pytest.mark.parametrize(
    "code,ok", [("7802800123452", True), ("7802800123456", False), ("123", False)]
)
def test_gtin_solo_con_digito_verificador_valido(code, ok):
    assert valid_gtin(code) is ok


def test_gtin_invalido_leido_de_imagen_no_se_acepta():
    ext = VisionExtraction(
        "simulated",
        "s",
        "m",
        "clear",
        (VisionField("ingredient", "ibuprofeno", 0.9), VisionField("gtin", "7802800123456", 0.9)),
    )
    r = read_extraction(ext)
    assert r.parsed.gtin is None and "gtin_checksum_invalid" in r.uncertainties


# ---------- contrato de visión (SIMULADO) ----------

MIGRANOL = VisionExtraction("simulated", "simulator", "simulated-v0", "clear", (
    VisionField("product_name", "Migranol®", .97), VisionField("laboratory", "Laboratorio Bagó de Chile S.A.", .95),
    VisionField("form", "comprimidos recubiertos", .95), VisionField("content", "10 comprimidos", .95)))  # fmt: skip


def test_migranol_fotografia_sin_texto_representa_lo_visible_con_procedencia_visual():
    """Caja real: marca, laboratorio, forma y contenido visibles; principio activo, dosis y código NO."""
    r = read_extraction(MIGRANOL)
    x = r.parsed
    assert r.status is VisionStatus.IDENTIFIED
    assert (
        x.brand.value == "Migranol®"
        and x.brand.origin == "image"
        and x.brand.basis is Basis.INFERRED
    )
    assert x.laboratory.value == "Laboratorio Bagó de Chile S.A." and x.laboratory.origin == "image"
    assert x.form.value == "comprimido recubierto" and x.form.origin == "image"
    assert x.content.measure.value == 10 and x.content.origin == "image"
    # lo NO visible permanece desconocido: jamás inventado
    assert x.strengths == () and x.unassigned == () and x.gtin is None
    assert x.manufacturer is None and x.registration_holder is None and x.marketer is None
    assert (
        x.name_anchor is True and x.ingredient == "migranol"
    )  # ancla de nombre, NO un principio activo
    pend = {(q["attribute"], q["reason"]) for q in x.pending_validation}
    assert ("active_ingredient", "not_stated") in pend and ("strength", "not_stated") in pend
    assert x.identity_summary()["pharmaceutical"]["complete"] is False
    assert (
        "active_ingredient_not_visible" in r.uncertainties
        and "strength_not_visible" in r.uncertainties
    )


def test_migranol_nada_de_lo_leido_de_la_imagen_queda_confirmado():
    x = read_extraction(MIGRANOL).parsed
    atributos = [x.form, x.content, x.brand, x.laboratory]
    assert all(a.basis is Basis.INFERRED and a.origin == "image" for a in atributos)


def test_migranol_sin_catalogo_no_hay_candidatos_inventados():
    x = read_extraction(MIGRANOL).parsed
    assert interpret_with_parsed(x, ctx_=ctx()).candidates == []


def interpret_with_parsed(parsed, ctx_):
    from app.interpretation.engine import interpret_parsed

    return interpret_parsed(parsed, **ctx_)


def test_migranol_con_referencia_verificada_de_PRUEBA_propone_sin_aplicar():
    """FIXTURE de prueba con un principio activo FICTICIO: no es una afirmación sobre Migranol real."""
    ident = CatalogRec(
        uuid.uuid4(),
        "FIXTURE activo-de-prueba 100 mg",
        "activo de prueba",
        (build_measure(D(100), "mg"),),
        "comprimido recubierto",
    )
    ref = CommercialRec(
        uuid.uuid4(), "FIXTURE Migranol · Bagó", "migranol", "Bagó", None, None, ident
    )
    i = interpret_with_parsed(
        read_extraction(MIGRANOL).parsed, ctx(catalog=[ident], commercial=[ref]) | {}
    )
    (c,) = i.candidates
    assert c.kind == "commercial_reference" and c.verified and "laboratory_matches" in c.reasons
    assert "not_applied_until_confirmed" in c.reasons
    assert i.parsed.name_anchor and i.parsed.strengths == ()  # el candidato NO se aplicó


def test_misma_marca_otro_laboratorio_no_es_la_misma_referencia():
    ident = CatalogRec(uuid.uuid4(), "x", "activo de prueba", (build_measure(D(100), "mg"),))
    ref = CommercialRec(uuid.uuid4(), "otra", "migranol", "Otro Laboratorio", None, None, ident)
    assert (
        interpret_with_parsed(read_extraction(MIGRANOL).parsed, ctx(commercial=[ref])).candidates
        == []
    )


VIT_D3_BOX = VisionExtraction("simulated", "simulator", "simulated-v0", "clear", (
    VisionField("ingredient", "Vitamina D3", .95), VisionField("strength", "50.000 UI", .93),
    VisionField("form", "cápsulas", .9), VisionField("content", "4 cápsulas", .9), VisionField("visible_quantity", "3 cajas apiladas", .7)))  # fmt: skip


def test_ejemplo_vitamina_d3_caja_con_cantidad_visible_no_es_cantidad_solicitada():
    r = read_extraction(VIT_D3_BOX)
    x = r.parsed
    assert x.ingredient == "vitamina d3" and x.strengths[0].measure.label == "50000 ui"
    assert x.strengths[0].origin == "image" and x.strengths[0].basis is Basis.INFERRED
    assert x.form.value == "capsula" and x.content.measure.value == 4
    assert (
        x.requested_quantity is None
    )  # «4 cápsulas» y «3 cajas» visibles NO son cantidad a comprar
    assert "visible_quantity_not_a_requested_quantity" in r.ignored


def test_foto_mas_texto_la_cantidad_solo_sale_del_texto_como_sugerencia():
    x = read_extraction(VIT_D3_BOX).parsed
    m, conflicts = merge_text_and_image(p("faltan 2 cajas", allow_no_product=True), x)
    assert (
        conflicts == []
        and m.ingredient == "vitamina d3"
        and (m.requested_quantity, m.requested_unit) == (D(2), "box")
    )
    assert m.strengths[0].origin == "image"


def test_foto_mas_texto_que_la_contradice_es_conflicto():
    x = read_extraction(VIT_D3_BOX).parsed
    _, conflicts = merge_text_and_image(p("vitamina d3 25000 ui"), x)
    assert conflicts == ["strength"]


def test_texto_con_cifra_suelta_toma_la_unidad_de_la_imagen_solo_como_inferencia():
    x = read_extraction(VIT_D3_BOX).parsed
    m, conflicts = merge_text_and_image(p("falta vitamina d3 50000"), x)
    assert conflicts == [] and m.unassigned == () and m.strengths[0].basis is Basis.INFERRED


@pytest.mark.parametrize("leg", ["blurry", "unreadable"])
def test_imagen_borrosa_no_inventa_nada(leg):
    r = read_extraction(
        VisionExtraction("simulated", "s", "m", leg, (VisionField("ingredient", "algo", 0.99),))
    )
    assert r.parsed is None and r.status is VisionStatus.NEEDS_REVIEW


def test_campos_de_baja_confianza_se_descartan():
    r = read_extraction(
        VisionExtraction(
            "simulated",
            "s",
            "m",
            "clear",
            (VisionField("ingredient", "ibuprofeno", 0.95), VisionField("strength", "400 mg", 0.2)),
        )
    )
    assert r.parsed.strengths == () and "low_confidence:strength" in r.uncertainties


def test_sin_nombre_visible_no_hay_producto():
    r = read_extraction(
        VisionExtraction(
            "simulated", "s", "m", "clear", (VisionField("content", "10 comprimidos", 0.9),)
        )
    )
    assert r.parsed is None and "no_product_name_visible" in r.uncertainties


async def test_el_simulador_siempre_se_declara_simulado():
    sim = SimulatedVisionInterpreter(
        {"*": VisionExtraction("live", "real-ai", "gpt", "clear")}
    )  # intenta pasar por real
    ext = await sim.extract(b"x", "image/jpeg", None)
    assert ext.mode == "simulated" and ext.provider == "simulator"


def test_suggest_for_lectura_de_catalogo():
    assert suggest_for(p("falta vitamina d 50000"), [VIT_D])[0].assignment["unit_expr"] == "ui"
    with pytest.raises(DomainError):
        p("")


def test_serializacion_conserva_origen_y_entidades_comerciales():
    """Regresión: to_dict() omitía marca/laboratorio/origen y se perdían al guardar en la base."""
    x = read_extraction(MIGRANOL).parsed
    back = type(x).from_dict(x.to_dict())
    assert back == x
    assert (
        back.brand.origin == "image" and back.laboratory.value == "Laboratorio Bagó de Chile S.A."
    )
    assert back.form.origin == "image" and back.content.origin == "image" and back.name_anchor
    t = p("ibuprofeno 400 mg comprimidos x 20 lab saval")
    assert type(t).from_dict(t.to_dict()) == t


def test_dos_lecturas_de_imagen_iguales_coinciden_pero_una_inferencia_no_se_une_a_lo_explicito():
    a = read_extraction(MIGRANOL).parsed
    b = read_extraction(MIGRANOL).parsed
    assert (
        compare(a, b).relation is Relation.IDENTICAL
    )  # mismo dato, misma fuente, ambos aún sin confirmar
    texto = p("migranol comprimidos recubiertos x 10")
    assert (
        compare(a, texto).relation is Relation.PARTIAL
    )  # imagen vs texto explícito: decide una persona
