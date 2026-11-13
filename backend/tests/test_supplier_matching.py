"""Clasificación de coincidencias y bloqueo comercial del motor de proveedores.

Son pruebas puras: no hay red, base de datos ni estrategia real. Distinguen coincidencia directa de
resultado relacionado y comprueban que una oferta solo queda habilitada para carrito cuando la
unidad de venta y la base del precio están confirmadas.
"""

from decimal import Decimal

from app.services.connectors.base import SupplierOffer
from app.services.connectors.commercial import cart_blockers, cart_eligible
from app.services.connectors.matching import classify_match, reduce_query


def test_predual_publicado_confirma_la_consulta():
    relation, reasons = classify_match("predual", "Predual 125 comprimidos")
    assert relation == "direct"
    assert reasons == ["all_query_terms_in_name"]


def test_predual_es_directo_sin_importar_mayusculas_ni_tildes():
    relation, _ = classify_match("PRÉDUAL", "predual x 10 comprimidos")
    assert relation == "direct"


def test_pregalex_y_pregalin_quedan_relacionados_no_directos():
    for name in (
        "Pregalex Pregabalina 150 mg 30 comprimidos",
        "Pregalin 75 mg x 30 Comprimidos",
        "Pregalex Pregabalina 75mg 30 Comprimidos",
    ):
        relation, reasons = classify_match("predual", name)
        assert relation == "related", name
        assert reasons == ["query_not_confirmed_by_name"]


def test_una_parte_de_la_consulta_no_alcanza_para_coincidencia_directa():
    relation, reasons = classify_match("predual 30 mg", "Predual 125 comprimidos")
    assert relation == "related"
    assert reasons == ["partial_query_match"]


def test_multiflora_confirma_con_plural_abreviatura_y_cantidad_junta():
    """El caso real: «Multiflora plus capsula 30» debe confirmar «Multiflora Plus x 30 Capsulas»."""
    nombre = "Multiflora Plus x 30 Capsulas"
    for consulta in (
        "Multiflora plus capsula 30",
        "Multiflora plus capsulas 30",
        "Multiflora plus caps 30",
        "Multiflora x30",
        "Multiflora 30",
        "Multiflora plus capsula 30 caja",
    ):
        relation, reasons = classify_match(consulta, nombre)
        assert relation == "direct", consulta
        assert reasons == ["all_query_terms_in_name"], consulta


def test_otra_concentracion_no_confirma_aunque_comparta_el_nombre():
    relation, reasons = classify_match("Multiflora plus capsula 30", "Multiflora Plus x 60 Capsulas")
    assert relation == "related"
    assert reasons == ["partial_query_match"]


def test_otra_forma_farmaceutica_sigue_siendo_relacionada():
    for nombre in ("Multiflora Plus x 30 Comprimidos", "Multiflora Plus Jarabe 30 ml"):
        relation, _ = classify_match("Multiflora plus capsula 30", nombre)
        assert relation == "related", nombre


def test_un_envase_distinto_no_confirma():
    relation, _ = classify_match("Multiflora frasco", "Multiflora Plus caja x 30")
    assert relation == "related"


def test_la_consulta_reducida_conserva_solo_los_terminos_de_identidad():
    assert reduce_query("Multiflora plus capsula 30 caja") == "multiflora plus"
    assert reduce_query("Amoxicilina 500 mg comprimidos") == "amoxicilina"
    assert reduce_query("Paracetamol 500") == "paracetamol"
    # Sin algo que quitar, no hay respaldo: no se reenvía la misma consulta.
    assert reduce_query("Multiflora") is None
    assert reduce_query("x 30") is None


def test_sin_nombre_no_se_afirma_coincidencia():
    relation, reasons = classify_match("predual", None)
    assert relation == "unverified"
    assert reasons == ["name_missing"]


def test_sin_consulta_no_se_afirma_coincidencia():
    relation, reasons = classify_match("   ", "Predual 125 comprimidos")
    assert relation == "unverified"
    assert reasons == ["empty_query"]


def _offer(**over) -> SupplierOffer:
    data = dict(
        external_id="140429",
        name="Predual 125 comprimidos",
        price=Decimal("12990"),
        currency="CLP",
        laboratory="LABORATORIO MAVER S.A",
        presentation="Comprimido",
    )
    data.update(over)
    return SupplierOffer(**data)


def test_el_carrito_sigue_bloqueado_sin_unidad_de_venta_ni_base_de_precio():
    offer = _offer()
    assert cart_eligible(offer, match_relation="direct") is False
    assert cart_blockers(offer, match_relation="direct") == [
        "sale_unit_unconfirmed",
        "price_basis_unconfirmed",
    ]


def test_solo_el_conector_identificado_puede_omitir_esos_atributos():
    """La excepción es explícita y nunca el comportamiento por defecto."""
    offer = _offer()
    assert cart_eligible(offer, match_relation="direct", allow_unknown_terms=True) is True
    assert cart_blockers(offer, match_relation="direct", allow_unknown_terms=True) == []
    # Aun con la excepción, un impedimento real sigue bloqueando.
    sin_stock = _offer(stock_status="out_of_stock")
    assert cart_eligible(sin_stock, match_relation="direct", allow_unknown_terms=True) is False
    relacionada = _offer()
    assert (
        cart_eligible(relacionada, match_relation="related", allow_unknown_terms=True) is False
    )


def test_con_datos_confirmados_la_elegibilidad_no_cambia():
    """Con datos confirmados la elegibilidad es la misma, con o sin la excepción."""
    offer = _offer(sale_unit="box", price_basis="per_commercial_unit")
    assert cart_eligible(offer, match_relation="direct", allow_unknown_terms=True) is True


def test_un_resultado_relacionado_tampoco_entra_al_carrito():
    offer = _offer(name="Pregalex Pregabalina 150 mg 30 comprimidos")
    relation, _ = classify_match("predual", offer.name)
    assert relation == "related"
    assert "match_not_direct" in cart_blockers(offer, match_relation=relation)
    assert cart_eligible(offer, match_relation=relation) is False


def test_solo_una_oferta_directa_con_datos_confirmados_queda_habilitada():
    offer = _offer(sale_unit="box", price_basis="per_commercial_unit")
    assert cart_eligible(offer, match_relation="direct") is True
    assert cart_blockers(offer, match_relation="direct") == []
