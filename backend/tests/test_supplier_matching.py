"""Clasificación de coincidencias y bloqueo comercial del motor de proveedores.

Son pruebas puras: no hay red, base de datos ni estrategia real. Distinguen coincidencia directa de
resultado relacionado y comprueban que una oferta solo queda habilitada para carrito cuando la
unidad de venta y la base del precio están confirmadas.
"""

from decimal import Decimal

from app.services.connectors.base import SupplierOffer
from app.services.connectors.commercial import cart_blockers, cart_eligible
from app.services.connectors.matching import classify_match


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
