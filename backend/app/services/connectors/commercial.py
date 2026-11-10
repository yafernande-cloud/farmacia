"""Condiciones comerciales mínimas para incorporar una oferta a un carrito.

Mostrar una oferta y poder comprarla son decisiones distintas. Una oferta puede verse en los
resultados aunque falten datos comerciales; **no** puede sumarse a un carrito mientras esos datos
no estén confirmados por una fuente verificable. `presentation` («Comprimido», «Caja 30») describe
la forma o el envase publicado, **no** es la unidad de venta ni la base del precio.

Este módulo no decide equivalencia farmacéutica: `match_not_direct` solo exige que el nombre
publicado confirme la consulta. La clasificación completa está en `matching.py`.
"""

from __future__ import annotations

from app.services.connectors.base import PublishedProduct, SupplierOffer

CART_BLOCKERS = (
    "match_not_direct",
    "not_available_for_sale",
    "no_current_price",
    "sale_unit_unconfirmed",
    "price_basis_unconfirmed",
)


def cart_blockers(offer: SupplierOffer, *, match_relation: str) -> list[str]:
    """Motivos por los que la oferta todavía no puede entrar a un carrito, en orden estable."""
    blockers: list[str] = []
    if match_relation != "direct":
        blockers.append("match_not_direct")
    # El proveedor informó que la publicación no está disponible para la venta.
    if offer.stock_status == "out_of_stock":
        blockers.append("not_available_for_sale")
    if offer.sale_unit is None:
        blockers.append("sale_unit_unconfirmed")
    if offer.price_basis is None:
        blockers.append("price_basis_unconfirmed")
    return blockers


def published_blockers(product: PublishedProduct, *, match_relation: str) -> list[str]:
    """Motivos por los que una publicación sin precio vigente no puede entrar a un carrito.

    No es una oferta: nunca es elegible, aunque algún día tenga todos los demás datos.
    """
    blockers: list[str] = []
    if match_relation != "direct":
        blockers.append("match_not_direct")
    if product.stock_status == "out_of_stock":
        blockers.append("not_available_for_sale")
    blockers.append("no_current_price")
    blockers.append("sale_unit_unconfirmed")
    blockers.append("price_basis_unconfirmed")
    return blockers


def cart_eligible(offer: SupplierOffer, *, match_relation: str) -> bool:
    return not cart_blockers(offer, match_relation=match_relation)
