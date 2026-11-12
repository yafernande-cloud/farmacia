"""Condiciones comerciales mínimas para incorporar una oferta a un carrito.

Mostrar una oferta y poder comprarla son decisiones distintas. Una oferta puede verse en los
resultados aunque falten datos comerciales; **no** puede sumarse a un carrito mientras esos datos
no estén confirmados por una fuente verificable. `presentation` («Comprimido», «Caja 30») describe
la forma o el envase publicado, **no** es la unidad de venta ni la base del precio.

La unidad de venta y la base del precio no son condiciones para cotizar: son datos que la fuente
puede no publicar. Cuando faltan se conservan como desconocidos (`None`), se informa la cantidad en
ofertas seleccionadas y el subtotal se calcula con el precio publicado; lo **nunca** se hace es
rellenarlos por suposición.

Esa excepción es **solo** del conector que la fuente declara en su perfil guardado
(`presets.allows_unknown_commercial_terms`, hoy únicamente FarmaLoop). Para cualquier otro
proveedor —listas cargadas y conectores futuros— ambos atributos siguen siendo obligatorios: sin
ellos la oferta no entra al carrito. Un valor presente pero inválido se rechaza siempre.

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


def cart_blockers(
    offer: SupplierOffer,
    *,
    match_relation: str,
    allow_unknown_terms: bool = False,
) -> list[str]:
    """Motivos por los que la oferta todavía no puede entrar a un carrito, en orden estable.

    `allow_unknown_terms` solo lo habilita el conector identificado en el perfil del servidor
    (FarmaLoop). El valor por defecto es estricto: cualquier otro proveedor exige unidad de venta y
    base del precio confirmadas.
    """
    blockers: list[str] = []
    if match_relation != "direct":
        blockers.append("match_not_direct")
    # El proveedor informó que la publicación no está disponible para la venta.
    if offer.stock_status == "out_of_stock":
        blockers.append("not_available_for_sale")
    if not allow_unknown_terms:
        if offer.sale_unit is None:
            blockers.append("sale_unit_unconfirmed")
        if offer.price_basis is None:
            blockers.append("price_basis_unconfirmed")
    return blockers


def published_blockers(product: PublishedProduct, *, match_relation: str) -> list[str]:
    """Motivos por los que una publicación sin precio vigente no puede entrar a un carrito.

    No es una oferta: nunca es elegible, aunque algún día tenga todos los demás datos. El precio
    de lista es referencial y no reemplaza la falta de un precio comercial vigente.
    """
    blockers: list[str] = []
    if match_relation != "direct":
        blockers.append("match_not_direct")
    if product.stock_status == "out_of_stock":
        blockers.append("not_available_for_sale")
    blockers.append("no_current_price")
    return blockers


def cart_eligible(
    offer: SupplierOffer, *, match_relation: str, allow_unknown_terms: bool = False
) -> bool:
    return not cart_blockers(
        offer, match_relation=match_relation, allow_unknown_terms=allow_unknown_terms
    )
