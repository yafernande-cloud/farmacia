"""A6 Fase 4 — preferencias **entre artículos** (capa transversal).

Pruebas puras: no tocan PostgreSQL ni OpenAI. Comprueban que una baja de prioridad o una
preferencia entre dos artículos reordena solo las ofertas comparables del mismo proveedor, que
todas las ofertas siguen presentes y que ninguna asociación se inventa.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.services.preferences import (
    ArticlePreferences,
    ItemPreferences,
    OfferView,
    _resolve_article_events,
    rank_provider_offers,
)

BASE = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
B = uuid.UUID("00000000-0000-4000-8000-00000000000b")
C = uuid.UUID("00000000-0000-4000-8000-00000000000c")


def _event(article, effect, relative=None, *, created=BASE, ident=None):
    return SimpleNamespace(
        id=ident or uuid.uuid4(),
        article_id=article,
        relative_to_article_id=relative,
        effect=effect,
        created_at=created,
        observation_id=uuid.uuid4(),
    )


def _offer(offer_id: str, article, price: str = "1000") -> OfferView:
    return OfferView(
        offer_id=uuid.uuid5(uuid.NAMESPACE_DNS, offer_id),
        supplier_id=None,
        laboratory=None,
        brand=None,
        currency="CLP",
        price_amount=Decimal(price),
        price_basis="per_commercial_unit",
        pack_units_per_commercial_unit=None,
        stock_status="known",
        stock_quantity=None,
        stock_unit=None,
        sale_unit="box",
        trade_item_id=article,
    )


def _rank(prefs: ArticlePreferences, views) -> dict[str, tuple[int, ...]]:
    keys = rank_provider_offers(prefs, {A: ItemPreferences(A), B: ItemPreferences(B)}, views)
    return {view.offer_id.hex: keys[view.offer_id] for view in views}


# ------------------------------------------------------------------- resolución temporal


def test_una_baja_de_prioridad_vigente_marca_el_articulo():
    prefs = _resolve_article_events([_event(A, "deprioritize")])

    assert prefs.demoted == frozenset({A})
    assert prefs.has_effects is True


def test_el_retiro_posterior_deja_sin_efecto_la_baja():
    prefs = _resolve_article_events(
        [
            _event(A, "deprioritize", created=BASE),
            _event(A, "retire", created=BASE.replace(hour=13)),
        ]
    )

    assert prefs.demoted == frozenset()
    assert prefs.has_effects is False


def test_preferir_despues_de_retirar_vuelve_a_establecer_el_par():
    prefs = _resolve_article_events(
        [
            _event(A, "retire", created=BASE),
            _event(A, "prefer", B, created=BASE.replace(hour=13)),
        ]
    )

    assert prefs.preferred_pairs == ((A, B),)


def test_retirar_la_baja_no_borra_otras_relaciones_del_mismo_articulo():
    prefs = _resolve_article_events(
        [
            _event(A, "deprioritize", created=BASE),
            _event(A, "prefer", B, created=BASE.replace(hour=13)),
            _event(A, "retire", created=BASE.replace(hour=14)),
        ]
    )

    assert prefs.demoted == frozenset()
    assert prefs.preferred_pairs == ()


# ----------------------------------------------------------------------------- orden


def test_la_baja_global_pone_el_articulo_despues_sin_ocultarlo():
    prefs = _resolve_article_events([_event(A, "deprioritize")])
    vistas = [_offer("oferta-a", A), _offer("oferta-b", B)]

    claves = _rank(prefs, vistas)

    assert claves[vistas[1].offer_id.hex][:2] == (0, 0)
    assert claves[vistas[0].offer_id.hex][:2] == (1, 0)
    # Las dos siguen presentes: nada se filtra ni se descarta.
    assert len(claves) == 2


def test_preferir_un_articulo_solo_demora_al_perdedor_cuando_ambos_estan_presentes():
    prefs = _resolve_article_events([_event(A, "prefer", B)])
    ambos = [_offer("oferta-a", A), _offer("oferta-b", B)]

    claves = _rank(prefs, ambos)
    assert claves[ambos[0].offer_id.hex][:2] == (0, 0)
    # El perdedor (la alternativa) queda después por la segunda componente; su prioridad global
    # no cambia.
    assert claves[ambos[1].offer_id.hex][:2] == (0, 1)

    # Con solo el ganador presente no hay comparación posible y nadie se demora.
    solo_ganador = _rank(prefs, [_offer("oferta-a", A), _offer("oferta-x", C)])
    assert solo_ganador[_offer("oferta-x", C).offer_id.hex][:2] == (0, 0)
    assert solo_ganador[_offer("oferta-a", A).offer_id.hex][:2] == (0, 0)


def test_una_oferta_sin_identidad_conserva_la_clave_neutra_y_no_participa():
    prefs = _resolve_article_events([_event(A, "prefer", B)])
    vistas = [_offer("oferta-a", A), _offer("oferta-sin-identidad", None)]

    claves = _rank(prefs, vistas)

    # `A` está presente pero su alternativa no: nadie pierde ni gana. La oferta sin
    # `trade_item_id` no recibe una asociación inventada.
    assert claves[vistas[0].offer_id.hex][:2] == (0, 0)
    assert claves[vistas[1].offer_id.hex][:2] == (0, 0)


def test_la_clave_es_estable_y_no_depende_del_orden_de_entrada():
    prefs = _resolve_article_events([_event(B, "deprioritize")])
    vistas = [_offer("oferta-a", A), _offer("oferta-b", B), _offer("oferta-c", C)]

    directa = _rank(prefs, vistas)
    inversa = _rank(prefs, list(reversed(vistas)))

    assert directa == inversa
    assert directa[vistas[1].offer_id.hex][0] == 1
    assert directa[vistas[0].offer_id.hex][0] == 0
    assert directa[vistas[2].offer_id.hex][0] == 0


def test_sin_relaciones_vigentes_todas_las_claves_son_neutras():
    claves = _rank(_resolve_article_events([]), [_offer("oferta-a", A), _offer("oferta-b", B)])

    assert all(clave[:2] == (0, 0) for clave in claves.values())
