"""Modelo de producto: separación de atributos, unidades, procedencia y compatibilidad."""

from decimal import Decimal as D

import pytest

from app.core.errors import DomainError
from app.domain.products import (
    Basis,
    ConfirmPatch,
    ParsedProduct,
    Relation,
    apply_alias,
    apply_confirmation,
    compare,
)
from app.domain.products import (
    parse_shortage_text as p,
)
from app.domain.units import Measure, build_measure


def rel(a, b):
    return compare(p(a), p(b)).relation


# ---------- un número sin unidad NUNCA es mg ----------


@pytest.mark.parametrize(
    "txt", ["ibuprofeno 400", "ibuprofeno de 400", "vitamina d 1000", "falta paracetamol 500"]
)
def test_numero_sin_unidad_queda_pendiente_sin_unidad_ni_rol(txt):
    r = p(txt)
    assert r.strengths == () and r.content is None  # no se asignó ningún rol
    (m,) = r.unassigned
    assert m.unit is None  # ni mg, ni UI, ni nada
    assert {
        "attribute": "measure",
        "reason": "unit_not_stated",
        "value": m.label,
    } in r.pending_validation
    assert not r.identity_complete and r.resolved_identity_key is None


def test_el_mismo_numero_con_unidades_distintas_son_cosas_distintas():
    assert rel("vitamina d 1000 UI", "vitamina d 1000 mg") is Relation.INCOMPATIBLE
    assert (
        rel("ibuprofeno 400 mg", "ibuprofeno 400 ml") is not Relation.IDENTICAL
    )  # potencia vs volumen


# ---------- cinco conceptos separados ----------


def test_potencia_contenido_presentacion_y_cantidad_solicitada_se_separan():
    r = p("necesitamos 5 cajas de ibuprofeno 400 mg comprimidos caja de 20")
    assert [s.measure.label for s in r.strengths] == ["400 mg"]  # potencia
    assert r.content.measure.label == "20 un" and r.content.basis is Basis.EXPLICIT  # contenido
    assert (r.form.value, r.packaging.value) == ("comprimido", "caja")  # presentación
    assert (r.requested_quantity, r.requested_unit) == (D(5), "box")  # cantidad a comprar
    assert r.ingredient == "ibuprofeno"  # nada se coló al nombre


def test_x_20_es_contenido_no_cantidad_ni_potencia():
    r = p("falta ibuprofeno 400 mg x 20")
    assert r.content.measure.value == 20 and r.requested_quantity is None
    assert [s.measure.label for s in r.strengths] == ["400 mg"]


def test_5_cajas_de_20_no_inventa_una_potencia():
    r = p("ibuprofeno 5 cajas de 20")  # regresión: antes producía "20 mg"
    assert r.strengths == () and r.unassigned == ()
    assert r.requested_quantity == 5 and r.content.measure.value == 20


def test_volumen_es_contenido_no_potencia():
    r = p("suero fisiologico 500 ml")  # regresión: antes era "potencia 500 ml"
    assert r.strengths == () and r.content.measure.label == "500 ml"


def test_jarabe_con_concentracion_y_volumen():
    r = p("ibuprofeno 100mg/5ml jarabe 120 ml")  # regresión: antes ingrediente "ibuprofeno ml"
    assert r.ingredient == "ibuprofeno"
    (s,) = r.strengths
    assert s.kind == "concentration" and s.measure.label == "100 mg/5 ml"
    assert r.content.measure.label == "120 ml" and r.form.value == "jarabe"


def test_combinacion_conserva_ambas_potencias():
    r = p("paracetamol 500 mg codeina 30 mg")  # regresión: antes ingrediente "… mg"
    assert r.ingredient == "paracetamol codeina"
    assert [s.measure.label for s in r.strengths] == ["500 mg", "30 mg"]


# ---------- unidades simples y compuestas, con representación original ----------


@pytest.mark.parametrize(
    "txt,label,norm",
    [
        ("ibuprofeno 400 mg", "400 mg", "mass:400mg"),
        ("levotiroxina 50 µg", "50 mcg", "mass:0.05mg"),  # regresión: antes 50 g
        ("levotiroxina 50 mcg", "50 mcg", "mass:0.05mg"),
        ("vitamina d 1000 UI", "1000 ui", "iu:1000ui"),
        ("clotrimazol 2,5 %", "2.5 %", "percent:2.5%"),
        ("amoxicilina 250 mg/5 ml", "250 mg/5 ml", "mass/volume:50mg/ml"),
        ("dexametasona 4 mg/ml", "4 mg/ml", "mass/volume:4mg/ml"),
        ("insulina 100 UI/ml", "100 ui/ml", "iu/volume:100ui/ml"),
        ("budesonida 200 mcg/dosis", "200 mcg/dosis", "mass/dose:0.2mg/dosis"),
        ("ibuprofeno 1.000 mg", "1000 mg", "mass:1000mg"),  # regresión: antes 1 mg (miles chilenos)
    ],
)
def test_unidades_simples_y_compuestas(txt, label, norm):
    (s,) = p(txt).strengths
    assert s.measure.label == label and s.measure.normalized().key == norm


def test_gramos_normalizan_a_mg_con_forma_que_da_rol():
    s_ = p("amoxicilina 1 g comprimidos").strengths[0]  # g + forma sólida: rol deducido
    assert s_.measure.label == "1 g" and s_.measure.normalized().key == "mass:1000mg"


def test_normalizar_no_pierde_lo_escrito():
    (s,) = p("amoxicilina 250 mg/5 ml").strengths
    assert s.measure.original_text == "250 mg/5 ml"
    assert (s.measure.value, s.measure.unit, s.measure.per_value, s.measure.per_unit) == (
        D(250),
        "mg",
        D(5),
        "ml",
    )
    assert s.measure.normalized().value == D(50)  # solo sirve para comparar


def test_unidades_equivalentes_se_reconocen_sin_perder_el_original():
    # 1 g sin forma no tiene rol claro: misma cifra normalizada → revisión, no "incompatible" ni "idéntico"
    assert rel("amoxicilina 1 g", "amoxicilina 1000 mg") is Relation.PARTIAL
    assert rel("amoxicilina 250 mg/5 ml", "amoxicilina 50 mg/ml") is Relation.IDENTICAL
    assert (
        rel("ibuprofeno 100 mg/5 ml", "ibuprofeno 100 mg/ml") is Relation.INCOMPATIBLE
    )  # 20 vs 100 mg/mL


def test_unidad_extensible_se_valida():
    m = build_measure(D(5), "mg/ml")
    assert m.normalized().key == "mass/volume:5mg/ml"
    with pytest.raises(ValueError):
        build_measure(D(5), "ui/%")  # compuesta no soportada
    with pytest.raises(ValueError):
        build_measure(D(5), "xyz")


# ---------- procedencia: explícito / inferido / desconocido / confirmado ----------


def test_g_con_forma_solida_es_inferido_y_no_cuenta_como_identidad():
    r = p("amoxicilina 1 g comprimidos")
    assert r.strengths[0].basis is Basis.INFERRED
    assert {
        "attribute": "strength",
        "reason": "inferred_needs_confirmation",
    } in r.pending_validation
    # la misma cifra explícita NO se fusiona con la inferida
    assert rel("amoxicilina 1 g comprimidos", "amoxicilina 1000 mg comprimidos") is Relation.PARTIAL


def test_g_sin_forma_conocida_queda_sin_rol():
    r = p("amoxicilina 1 g")
    assert r.strengths == () and r.unassigned[0].unit == "g"
    assert {
        "attribute": "measure",
        "reason": "role_not_stated",
        "value": "1 g",
    } in r.pending_validation


def test_g_en_crema_es_contenido_inferido():
    r = p("clotrimazol crema 30 g")
    assert r.content.basis is Basis.INFERRED and r.strengths == ()


def test_n_comprimidos_sin_marcador_es_inferido_podria_ser_cantidad():
    r = p("ibuprofeno 400 mg 20 comprimidos")
    assert r.content.basis is Basis.INFERRED and r.requested_quantity is None


def test_confirmar_pasa_a_confirmado_pero_no_antes():
    r = p("ibuprofeno 400")
    assert r.unassigned and not r.strengths
    c = apply_confirmation(r, ConfirmPatch(strengths=[build_measure(D(400), "mg")], form="comprimido",
                                           content=Measure(D(20), "un", original_text="x 20")))  # fmt: skip
    assert c.unassigned == () and c.strengths[0].basis is Basis.CONFIRMED
    assert (
        c.identity_complete
        and c.resolved_identity_key == "ibuprofeno|mass:400mg|comprimido|count:20un|-"
    )
    assert r.strengths == ()  # el original no cambió


def test_confirmar_inferencias_explicitamente():
    r = p("amoxicilina 1 g comprimidos x 20")
    assert r.resolved_identity_key is None
    c = apply_confirmation(r, ConfirmPatch(confirm_inferred={"strength"}))
    assert c.strengths[0].basis is Basis.CONFIRMED and c.resolved_identity_key is not None


def test_alias_verificado_solo_aplica_donde_una_persona_lo_confirmo():
    alias = [{"source_value": "400", "assignment": {"role": "strength", "unit_expr": "mg"}}]
    r = apply_alias(p("ibuprofeno 400"), alias)
    assert r.strengths[0].basis is Basis.CONFIRMED and r.strengths[0].role_rule == "verified_alias"
    # un número distinto, o con unidad escrita, no se toca
    assert apply_alias(p("ibuprofeno 600"), alias).strengths == ()
    assert apply_alias(p("ibuprofeno 400 mg"), alias).strengths[0].basis is Basis.EXPLICIT


# ---------- compatibilidad: no fusionar lo incompatible ni lo ambiguo ----------


@pytest.mark.parametrize(
    "x,y",
    [
        ("ibuprofeno 400 mg", "ibuprofeno 600 mg"),  # AC09 dosis
        ("ibuprofeno 400 mg comprimidos", "ibuprofeno 400 mg suspension"),  # AC09 forma
        ("ibuprofeno 400 mg x 10", "ibuprofeno 400 mg x 20"),  # contenido
        ("ibuprofeno 400", "ibuprofeno 600"),  # cifras sin unidad distintas
        ("ibuprofeno 600", "ibuprofeno 400 mg"),  # la cifra no calza con la potencia conocida
        ("ibuprofeno 400 mg", "paracetamol 400 mg"),
        ("paracetamol 500 mg codeina 30 mg", "paracetamol 500 mg codeina 60 mg"),
    ],
)
def test_incompatibles_nunca_se_fusionan(x, y):
    assert rel(x, y) is Relation.INCOMPATIBLE


def test_misma_cifra_sin_unidad_contra_unidad_explicita_va_a_revision_con_motivo():
    c = compare(p("ibuprofeno 400"), p("ibuprofeno 400 mg"))
    assert c.relation is Relation.PARTIAL and "unit_unconfirmed" in c.reasons
    assert (
        compare(p("ibuprofeno 400 mg"), p("ibuprofeno 400")).relation is Relation.PARTIAL
    )  # simétrico


def test_identicos_exactos_si_se_asocian():
    assert rel("falta ibuprofeno de 400 mg", "no queda ibuprofeno 400 mg") is Relation.IDENTICAL
    assert rel("ibuprofeno 400", "falta ibuprofeno de 400") is Relation.IDENTICAL  # misma incógnita


def test_atributo_conocido_solo_de_un_lado_pide_revision():
    assert rel("ibuprofeno", "ibuprofeno 400") is Relation.PARTIAL
    assert rel("ibuprofeno 400 mg", "ibuprofeno 400 mg comprimidos") is Relation.PARTIAL
    assert rel("ibuprofeno 400 mg", "ibuprofeno 400 mg retard") is Relation.PARTIAL


def test_serializacion_ida_y_vuelta_conserva_todo():
    r = p("ibuprofeno 100mg/5ml jarabe 120 ml 5 cajas")
    back = ParsedProduct.from_dict(r.to_dict())
    assert back.to_dict() == r.to_dict() and back.strengths[0].measure.original_text == "100mg/5ml"


# ---------- texto original y casos no interpretables ----------


def test_el_texto_original_se_conserva_tal_cual():
    assert p("  Falta IBUPROFENO de 400 mg!!  ").original_text == "Falta IBUPROFENO de 400 mg!!"


@pytest.mark.parametrize(
    "txt", ["ya llegó el ibuprofeno", "falta", "400 mg", "ibuprofeno jarabe comprimidos"]
)
def test_textos_no_interpretables_se_rechazan(txt):
    with pytest.raises(DomainError):
        p(txt)


@pytest.mark.parametrize(
    "txt,written",
    [
        ("Falta levotiroxina 50 µg", "50 µg"),  # el símbolo escrito, no el normalizado
        ("ibuprofeno 1.000 MG", "1.000 MG"),  # mayúsculas y separador de miles tal cual
        ("amoxicilina  250  mg / 5 ml", "250 mg / 5 ml"),  # solo se colapsan espacios
        ("Vitamina D 1000 UI", "1000 UI"),
    ],
)
def test_cada_medida_cita_lo_que_la_persona_escribio(txt, written):
    (s,) = p(txt).strengths
    assert s.measure.original_text == written  # y la etiqueta canónica va aparte
    assert s.measure.label != "" and p(txt).original_text == txt.strip()


def test_contenido_y_envase_tambien_citan_el_texto_escrito():
    r = p("ibuprofeno Caja de 20 Comprimidos")
    assert r.content.measure.original_text == "Caja de 20 Comprimidos"
