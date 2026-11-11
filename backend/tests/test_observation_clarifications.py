"""Aclaraciones en lenguaje natural para «Enseñar a la IA» (corrección UX de A6 Fase 4).

Pruebas puras: no tocan PostgreSQL ni OpenAI. Comprueban que, cuando la IA no entiende una
preferencia, el rechazo trae **una pregunta concreta** y, cuando la persona aclara con palabras
naturales («Andes»), solo se acepta si coincide de forma inequívoca con un valor **ya verificado**.
Nunca se comparan parecidos ni se inventan equivalencias: si hay duda, se pregunta.
"""

import uuid

import pytest

from app.interpretation.commercial import (
    CommercialContext,
    ContextSupplier,
    ObservationProposal,
    ProposalRejected,
    validate_proposal,
)

ITEM = uuid.uuid4()


def _context(**overrides) -> CommercialContext:
    base = dict(
        trade_item_id=ITEM,
        display_name="Losartán 50 mg x 20 comprimidos",
        ingredient="losartán",
        item_laboratory=None,
        item_brand=None,
        suppliers=(ContextSupplier(id=uuid.uuid4(), name="Proveedor Norte"),),
        laboratories=(),
        brands=(),
        currencies=("CLP",),
    )
    base.update(overrides)
    return CommercialContext(**base)


def _tolerance() -> dict:
    return {"kind": "none", "percent": None, "amount": None, "currency": None}


def _op(
    subject_kind: str = "laboratory",
    label: str = "Laboratorio Chile",
    effect: str = "prefer",
    confidence: float = 0.9,
) -> dict:
    return {
        "subject_kind": subject_kind,
        "subject_label": label,
        "effect": effect,
        "tolerance": _tolerance(),
        "price_reference": None,
        "confidence": confidence,
    }


def _proposal(operations=(), article_ops=(), ambiguity=None) -> ObservationProposal:
    return ObservationProposal.model_validate(
        {
            "operations": list(operations),
            "article_operations": list(article_ops),
            "retire_subject_kinds": [],
            "ambiguity": ambiguity,
        }
    )


def _reject(context: CommercialContext, proposal: ObservationProposal, text: str) -> ProposalRejected:
    with pytest.raises(ProposalRejected) as exc:
        validate_proposal(proposal, context, text)
    return exc.value


# ------------------------------------------------------------------- caso real «laboratorio Andes»


def test_andes_sin_el_nombre_literal_se_resuelve_con_el_valor_verificado():
    """«Andes» basta si coincide como palabra completa con un solo nombre publicado."""
    context = _context(laboratories=("Laboratorios Andes Ficticios", "Laboratorio Chile"))

    validated = validate_proposal(
        _proposal([_op(label="Andes")]),
        context,
        "Prefiero el laboratorio Andes.",
    )

    assert len(validated.preferences) == 1
    pref = validated.preferences[0]
    # Se persiste el nombre verificado, no el texto abreviado de la persona.
    assert pref.subject_label == "Laboratorios Andes Ficticios"
    assert pref.subject_key == "laboratorios andes ficticios"


def test_andes_tambien_se_resuelve_si_solo_esta_en_el_catalogo_conocido():
    context = _context(known_laboratories=("Laboratorios Andes Ficticios",))

    validated = validate_proposal(
        _proposal([_op(label="Andes")]),
        context,
        "Prefiero el laboratorio Andes.",
    )

    assert validated.preferences[0].subject_label == "Laboratorios Andes Ficticios"


def test_andes_ambiguo_pregunta_con_los_nombres_verificados_y_no_aplica_nada():
    context = _context(laboratories=("Laboratorio Andes Norte", "Laboratorio Andes Sur"))

    rejected = _reject(
        context,
        _proposal([_op(label="Andes")]),
        "Prefiero el laboratorio Andes.",
    )

    assert rejected.code == "validation_error"
    clarification = rejected.clarification
    assert clarification["kind"] == "unknown_laboratory"
    assert "«Laboratorio Andes Norte»" in clarification["question"]
    assert "«Laboratorio Andes Sur»" in clarification["question"]
    assert "¿A cuál te refieres?" in clarification["question"]
    # La pregunta no inventa un candidato ni elige uno por su cuenta.
    assert clarification["candidates"] == []


def test_un_nombre_desconocido_lista_los_verificados_sin_inventar_coincidencias():
    context = _context(laboratories=("Laboratorios Andes Ficticios",))

    rejected = _reject(
        context,
        _proposal([_op(label="Laboratorio Austral")]),
        "Prefiero el laboratorio Austral.",
    )

    clarification = rejected.clarification
    assert clarification["kind"] == "unknown_laboratory"
    assert clarification["reference"] == "Laboratorio Austral"
    assert "«Laboratorios Andes Ficticios»" in clarification["question"]


@pytest.mark.parametrize("label", ["lab", "ficticio", "andes ficticio"])
def test_no_se_compara_por_parecido(label: str):
    """Solo palabras completas: «lab» no es «laboratorios» ni «ficticio» es «ficticios»."""
    context = _context(laboratories=("Laboratorios Andes Ficticios",))

    rejected = _reject(
        context,
        _proposal([_op(label=label)]),
        f"Prefiero el {label}.",
    )

    assert rejected.code == "validation_error"
    assert rejected.clarification["kind"] == "unknown_laboratory"
    assert rejected.clarification["reference"] == label


def test_sin_laboratorios_verificados_no_promete_que_existan():
    context = _context()

    rejected = _reject(
        context,
        _proposal([_op(label="Andes")]),
        "Prefiero el laboratorio Andes.",
    )

    clarification = rejected.clarification
    assert clarification["kind"] == "unknown_laboratory"
    assert "no tiene laboratorios informados" in clarification["question"]


# ------------------------------------------------------------------- otras dudas con pregunta


def test_marca_desconocida_pregunta_con_su_clase_y_sus_nombres():
    context = _context(brands=("Marca Alfa", "Marca Beta"))

    rejected = _reject(
        context,
        _proposal([_op("brand", "Marca Gamma")]),
        "Prefiero la Marca Gamma.",
    )

    clarification = rejected.clarification
    assert clarification["kind"] == "unknown_brand"
    assert "marcas verificados" in clarification["question"]
    assert "«Marca Alfa»" in clarification["question"]


def test_proveedor_desconocido_pregunta_con_los_proveedores_activos():
    rejected = _reject(
        _context(),
        _proposal([_op("supplier", "Proveedor Fantasma")]),
        "Prefiero al Proveedor Fantasma.",
    )

    clarification = rejected.clarification
    assert clarification["kind"] == "unknown_supplier"
    assert "«Proveedor Norte»" in clarification["question"]


def test_articulo_desconocido_pide_el_nombre_del_catalogo():
    proposal = _proposal(
        article_ops=[
            {
                "article_label": "Ibuprofeno 800 mg",
                "relative_to_label": "Losartán 50 mg x 20 comprimidos",
                "effect": "prefer",
                "confidence": 0.9,
            }
        ]
    )

    rejected = _reject(_context(), proposal, "Prefiero el ibuprofeno al losartán.")

    assert rejected.clarification["kind"] == "unknown_article"
    assert rejected.clarification["reference"] == "Ibuprofeno 800 mg"
    assert "Cómo se llama el producto" in rejected.clarification["question"]


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("unclear_referent", "unclear_referent"),
        ("insufficient_context", "missing_detail"),
        ("conflicting", "conflicting"),
        ("not_commercial", "not_commercial"),
    ],
)
def test_cada_ambiguedad_del_modelo_tiene_pregunta_propia(code: str, kind: str):
    """La pregunta se escribe aquí; nunca se copia la nota ni el razonamiento del modelo."""
    proposal = _proposal(ambiguity={"code": code, "note": "texto interno del modelo"})

    rejected = _reject(_context(), proposal, "Me gusta el laboratorio ese.")

    assert rejected.code == "ambiguous"
    assert rejected.clarification["kind"] == kind
    assert rejected.clarification["question"]
    assert "texto interno del modelo" not in rejected.clarification["question"]


def test_baja_confianza_pide_aclaracion_sin_aplicar_nada():
    rejected = _reject(
        _context(),
        _proposal([_op(label="Laboratorio Chile", confidence=0.2)]),
        "Prefiero el laboratorio Chile.",
    )

    assert rejected.code == "low_confidence"
    assert rejected.clarification["kind"] == "low_confidence"
    assert rejected.clarification["reference"] == "Laboratorio Chile"
