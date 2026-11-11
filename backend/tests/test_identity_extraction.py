"""Reconocimiento determinista de laboratorio y marca en la descripción (D-150).

Son pruebas sin base de datos: fijan las reglas del vocabulario controlado y de las marcas
explícitas. Si algo no cumple las dos condiciones —marca explícita y nombre ya confirmado— el
resultado es `None`; nunca hay parecidos ni suposiciones.
"""

from app.domain.identity import (
    SOURCE_COLUMN,
    SOURCE_DESCRIPTION,
    STATUS_EXTRACTED,
    STATUS_PUBLISHED,
    STATUS_UNCONFIRMED,
    build_vocabulary,
    extract_identity,
    identity_evidence,
    identity_from_offer,
    identity_status,
)

VOCABULARIO = build_vocabulary(
    ["Laboratorios Andes Ficticios", "Lab. Cordillera Ficticio"],
    ["Marca Alfa Ficticia"],
)


def test_reconoce_el_laboratorio_con_abreviatura_y_con_el_nombre_completo():
    abreviatura = extract_identity("VITAMINA C - LAB. CORDILLERA FICTICIO", VOCABULARIO)
    completo = extract_identity("COMPLEJO B - LABORATORIOS ANDES FICTICIOS", VOCABULARIO)

    assert abreviatura.laboratory == "Lab. Cordillera Ficticio"
    assert completo.laboratory == "Laboratorios Andes Ficticios"


def test_conserva_el_texto_exacto_que_respalda_el_dato():
    extraida = extract_identity("SUERO - Lab. Cordillera Ficticio", VOCABULARIO)

    assert extraida.laboratory_text == "Lab. Cordillera Ficticio"


def test_reconoce_la_marca_solo_con_la_palabra_marca():
    con_marca = extract_identity("ALCOHOL - MARCA ALFA FICTICIA", VOCABULARIO)
    sin_marca = extract_identity("ALCOHOL - ALFA FICTICIA", VOCABULARIO)

    assert con_marca.brand == "Marca Alfa Ficticia"
    # El nombre está en el vocabulario, pero sin «MARCA» no consta que sea una marca.
    assert sin_marca.brand is None


def test_un_nombre_fuera_del_vocabulario_no_se_asigna():
    extraida = extract_identity("PRODUCTO - LAB. CORDILLERA REAL", VOCABULARIO)

    assert extraida.laboratory is None


def test_un_texto_ambiguo_no_se_interpreta():
    assert extract_identity("TENSIOMETRO - LAB/MARCA SUR", VOCABULARIO).laboratory is None
    assert extract_identity("JERINGA - LINEA CORDILLERA", VOCABULARIO).brand is None


def test_sin_vocabulario_nada_se_reconoce():
    extraida = extract_identity("PRODUCTO - LAB. CORDILLERA FICTICIO", build_vocabulary([], []))

    assert (extraida.laboratory, extraida.brand) == (None, None)


def test_el_laboratorio_de_la_descripcion_no_se_mezcla_con_una_marca():
    extraida = extract_identity(
        "MASCARILLA - LAB. CORDILLERA FICTICIO / MARCA ALFA FICTICIA", VOCABULARIO
    )

    assert extraida.laboratory == "Lab. Cordillera Ficticio"
    assert extraida.brand == "Marca Alfa Ficticia"


def test_el_estado_distingue_publicado_extraido_y_sin_identificar():
    assert identity_status("X", None, SOURCE_COLUMN, None) == STATUS_PUBLISHED
    assert identity_status("X", None, SOURCE_DESCRIPTION, None) == STATUS_EXTRACTED
    assert identity_status(None, None, None, None) == STATUS_UNCONFIRMED
    # Un dato extraído manda sobre el resto: la oferta está pendiente de confirmación.
    assert identity_status("X", "Y", SOURCE_COLUMN, SOURCE_DESCRIPTION) == STATUS_EXTRACTED


def test_la_evidencia_guarda_procedencia_y_texto_de_respaldo():
    evidencia = identity_evidence(
        "Lab. Cordillera Ficticio",
        None,
        SOURCE_DESCRIPTION,
        None,
        laboratory_text="LAB. CORDILLERA FICTICIO",
    )

    assert evidencia == {
        "status": STATUS_EXTRACTED,
        "laboratory": {
            "source": SOURCE_DESCRIPTION,
            "matched_text": "LAB. CORDILLERA FICTICIO",
        },
        "brand": None,
    }


def test_una_oferta_anterior_sin_evidencia_se_considera_publicada():
    estado, laboratory_source, brand_source = identity_from_offer("Lab. Andes", None, {})

    assert estado == STATUS_PUBLISHED
    assert (laboratory_source, brand_source) == (SOURCE_COLUMN, None)


def test_una_oferta_extraida_conserva_su_procedencia_al_releerla():
    evidencia = {
        "identity": {
            "status": STATUS_EXTRACTED,
            "laboratory": {"source": SOURCE_DESCRIPTION, "matched_text": "LAB. SUR"},
            "brand": None,
        }
    }

    estado, laboratory_source, _brand_source = identity_from_offer(
        "Laboratorio Sur Ficticio", None, evidencia
    )

    assert (estado, laboratory_source) == (STATUS_EXTRACTED, SOURCE_DESCRIPTION)
