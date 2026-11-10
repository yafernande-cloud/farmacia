"""Estrategia Algolia genérica.

No conoce Farmaloop ni ningún proveedor concreto: recibe un perfil declarativo (endpoint, índice,
referencias de secretos y mapeo de campos) y devuelve ofertas normalizadas. Cualquier proveedor con
un índice Algolia se conecta guardando un perfil; no se agrega código por proveedor.

Seguridad y límites:
* La clave nunca se guarda en el perfil ni se devuelve; solo se lee del entorno por su nombre.
* La consulta es la única variable; no se aceptan filtros ni rutas armadas por el usuario.
* La respuesta se corta por bytes y por tiempo. Un fallo transitorio no se disfraza de éxito.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

import httpx

from app.services.connectors.base import (
    ConnectorError,
    ConnectorIntervention,
    ConnectorNotConfigured,
    ConnectorResult,
    PublishedProduct,
    SupplierOffer,
    schema_fingerprint,
)
from app.services.connectors.config import CURRENCY, AlgoliaConfig, FieldSpec
from app.services.connectors.paths import (
    apply_transform,
    as_bool,
    as_decimal,
    as_text,
    as_url,
    render_template,
    resolve_all,
)

USER_AGENT = "Cotizador-IA/0.1 (+motor-de-proveedores)"


def _secret(ref: str) -> str:
    value = os.environ.get(ref, "").strip()
    if not value:
        # El nombre de la variable es útil para la persona; su valor no se registra jamás.
        raise ConnectorNotConfigured(f"secret_missing:{ref}")
    return value


def _clean_text(value: Any, limit: int) -> str | None:
    text = as_text(value, allow_number=True)
    if text is None:
        return None
    text = "".join(ch for ch in text if ord(ch) >= 32).strip()
    return text[:limit] or None


def _read(item: Any, spec: FieldSpec) -> Any:
    if spec.path is not None:
        return apply_transform(resolve_all(item, spec.path), spec.transform)
    if spec.template is not None:
        return render_template(spec.template, item)
    return spec.const


def _read_values(item: Any, specs: dict[str, FieldSpec]) -> dict[str, Any]:
    """Lee y convierte cada campo canónico del ítem según su spec."""
    values: dict[str, Any] = {}
    for name, spec in specs.items():
        raw = _read(item, spec)
        if spec.type == "decimal":
            values[name] = as_decimal(raw)
        elif spec.type == "int":
            amount = as_decimal(raw)
            values[name] = int(amount) if amount is not None and amount == amount.to_integral() else None
        elif spec.type == "url":
            values[name] = as_url(raw)
        elif spec.type == "bool":
            values[name] = as_bool(raw)
        else:
            values[name] = raw
    return values


def _identity(
    values: dict[str, Any], specs: dict[str, FieldSpec], cfg: AlgoliaConfig
) -> tuple[str, str, str] | None:
    """Identidad mínima para nombrar una publicación: código, nombre y moneda válidos.

    Devuelve `None` si el ítem no se puede publicar con identidad propia; el llamador lo descarta.
    """
    external_id = _clean_text(values.get("external_id"), 120)
    name = _clean_text(values.get("name"), 300)
    if external_id is None or name is None:
        return None
    currency = _clean_text(values.get("currency"), 3) or cfg.currency
    if currency is None:
        return None
    currency = currency.upper()
    if CURRENCY.fullmatch(currency) is None:
        return None

    # Un campo marcado `required` también descarta el ítem si no se pudo leer.
    for field_name, spec in specs.items():
        if spec.required and values.get(field_name) is None:
            return None
    return external_id, name, currency


def _stock(
    values: dict[str, Any], specs: dict[str, FieldSpec], cfg: AlgoliaConfig
) -> tuple[str, Decimal | None, str | None]:
    """Estado de stock desde datos del proveedor, sin inventar cantidades.

    `available_for_sale` es la señal explícita del portal sobre si la publicación se puede vender
    (0/1). Si el perfil confirma además la unidad de stock, la cantidad de los lotes la precisa.
    """
    stock_status = "unknown"
    stock_quantity: Decimal | None = None
    stock_unit: str | None = None

    available = values.get("available_for_sale") if "available_for_sale" in specs else None
    if isinstance(available, bool):
        stock_status = "in_stock" if available else "out_of_stock"

    if cfg.stock_unit is not None and "stock" in specs:
        amount = values.get("stock")
        if isinstance(amount, Decimal):
            stock_quantity = amount
            stock_unit = cfg.stock_unit
            stock_status = "in_stock" if amount > 0 else "out_of_stock"
    return stock_status, stock_quantity, stock_unit


def _normalise(item: Any, specs: dict[str, FieldSpec], cfg: AlgoliaConfig) -> SupplierOffer | None:
    """Convierte un ítem crudo en oferta. Si falta un dato obligatorio, devuelve `None`.

    Nunca completa un dato ausente con 0, 1 ni un texto inventado. Un ítem sin precio de venta no
    es una oferta: `_published` decide si todavía puede mostrarse como publicación del catálogo.
    """
    values = _read_values(item, specs)
    price = values.get("price")
    if not isinstance(price, Decimal) or price <= 0:
        return None
    identity = _identity(values, specs, cfg)
    if identity is None:
        return None
    external_id, name, currency = identity
    stock_status, stock_quantity, stock_unit = _stock(values, specs, cfg)

    return SupplierOffer(
        external_id=external_id,
        name=name,
        price=price,
        currency=currency,
        laboratory=_clean_text(values.get("laboratory"), 200),
        presentation=_clean_text(values.get("presentation"), 300),
        price_basis=cfg.price_basis,
        sale_unit=cfg.sale_unit,
        stock_status=stock_status,
        stock_quantity=stock_quantity,
        stock_unit=stock_unit,
        image_url=values.get("image_url") if isinstance(values.get("image_url"), str) else None,
        product_url=(
            values.get("product_url") if isinstance(values.get("product_url"), str) else None
        ),
        ean=_clean_text(values.get("ean"), 32),
    )


def _published(
    item: Any, specs: dict[str, FieldSpec], cfg: AlgoliaConfig
) -> PublishedProduct | None:
    """Publicación del catálogo sin precio comercial vigente.

    Solo llega aquí un ítem que `_normalise` ya rechazó. Se publica cuando la **única** falla es el
    precio: identidad, moneda y campos requeridos siguen siendo obligatorios. El precio de lista
    (`reference_price`) se conserva aparte y nunca se presenta como precio de venta.
    """
    values = _read_values(item, specs)
    price = values.get("price")
    if isinstance(price, Decimal) and price > 0:
        # Tenía precio: si no salió como oferta, falló por identidad o moneda; no se publica.
        return None
    identity = _identity(values, specs, cfg)
    if identity is None:
        return None
    external_id, name, currency = identity
    stock_status, _, _ = _stock(values, specs, cfg)
    reference = values.get("reference_price")

    return PublishedProduct(
        external_id=external_id,
        name=name,
        currency=currency,
        laboratory=_clean_text(values.get("laboratory"), 200),
        presentation=_clean_text(values.get("presentation"), 300),
        stock_status=stock_status,
        reference_price=(
            reference if isinstance(reference, Decimal) and reference > 0 else None
        ),
        image_url=values.get("image_url") if isinstance(values.get("image_url"), str) else None,
        product_url=(
            values.get("product_url") if isinstance(values.get("product_url"), str) else None
        ),
        ean=_clean_text(values.get("ean"), 32),
    )


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise ConnectorIntervention(
                "response_too_large", details={"max_bytes": limit}
            )
        chunks.append(chunk)
    return b"".join(chunks)


class AlgoliaStrategy:
    mechanism = "algolia"

    async def search(
        self,
        query: str,
        *,
        config: dict[str, Any],
        field_map: dict[str, Any],
        limit: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> ConnectorResult:
        try:
            cfg = AlgoliaConfig.model_validate(config)
            specs = {
                name: FieldSpec.model_validate(spec) for name, spec in field_map.items()
            }
        except Exception as exc:
            raise ConnectorIntervention(
                "invalid_profile", details={"field": "config"}
            ) from exc

        app_id = cfg.app_id or _secret(cfg.app_id_secret_ref or "")
        api_key = _secret(cfg.api_key_secret_ref)
        try:
            endpoint = cfg.endpoint.format_map({"app_id": app_id, "index": cfg.index})
        except (KeyError, ValueError) as exc:
            raise ConnectorIntervention("invalid_profile", details={"field": "endpoint"}) from exc
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConnectorIntervention("invalid_profile", details={"field": "endpoint"})

        wanted = max(1, min(limit, cfg.hits_per_page))
        payload: dict[str, Any] = {
            cfg.query_param: query,
            cfg.page_param: cfg.page,
            "hitsPerPage": wanted,
        }
        if cfg.filters:
            payload["filters"] = cfg.filters
        headers = {
            "X-Algolia-API-Key": api_key,
            "X-Algolia-Application-Id": app_id,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if cfg.referer:
            headers["Referer"] = cfg.referer
        if cfg.origin:
            headers["Origin"] = cfg.origin

        try:
            async with httpx.AsyncClient(
                timeout=cfg.timeout_seconds,
                transport=transport,
                follow_redirects=False,
            ) as client:
                async with client.stream("POST", endpoint, json=payload, headers=headers) as response:
                    if response.status_code in (401, 403):
                        raise ConnectorIntervention("credentials_rejected")
                    if 300 <= response.status_code < 400:
                        # No se siguen redirecciones: cambiaría el destino del secreto.
                        raise ConnectorIntervention(
                            "unexpected_redirect", details={"status": response.status_code}
                        )
                    if response.status_code == 429:
                        raise ConnectorError("rate_limited")
                    if response.status_code >= 500:
                        raise ConnectorError("provider_error")
                    if response.status_code >= 400:
                        raise ConnectorIntervention(
                            "request_rejected", details={"status": response.status_code}
                        )
                    body = await _read_bounded(response, cfg.max_response_bytes)
        except httpx.TimeoutException as exc:
            raise ConnectorError("timeout") from exc
        except httpx.HTTPError as exc:
            raise ConnectorError("network_error") from exc

        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ConnectorIntervention("invalid_response") from exc
        if not isinstance(data, dict):
            raise ConnectorIntervention("invalid_response")

        values = resolve_all(data, cfg.item_path)
        if cfg.item_path.strip() in {"", "$"}:
            raise ConnectorIntervention("schema_changed", details={"field": "item_path"})
        raw_items = values[0] if len(values) == 1 and isinstance(values[0], list) else None
        if raw_items is None:
            raise ConnectorIntervention("schema_changed", details={"field": cfg.item_path})

        offers: list[SupplierOffer] = []
        published: list[PublishedProduct] = []
        seen_offers: set[str] = set()
        seen_published: set[str] = set()
        discarded = 0
        for item in raw_items[:wanted]:
            offer = _normalise(item, specs, cfg)
            if offer is not None:
                if offer.external_id in seen_offers:
                    discarded += 1
                    continue
                seen_offers.add(offer.external_id)
                offers.append(offer)
                continue

            product = _published(item, specs, cfg)
            if product is None:
                discarded += 1
                continue
            if product.external_id in seen_offers or product.external_id in seen_published:
                discarded += 1
                continue
            seen_published.add(product.external_id)
            published.append(product)

        # Una oferta comercial manda sobre una publicación sin precio del mismo SKU.
        if seen_offers:
            published = [p for p in published if p.external_id not in seen_offers]

        # Solo se pide intervención cuando no se pudo leer ni una publicación con identidad: un
        # catálogo sin precios vigentes es un estado legítimo del proveedor, no un mapeo roto.
        if raw_items and not offers and not published:
            raise ConnectorIntervention(
                "field_mapping_failed",
                details={"items": len(raw_items), "discarded": discarded},
            )

        warnings: list[str] = []
        if not raw_items:
            warnings.append("La consulta no devolvió resultados.")

        return ConnectorResult(
            offers=offers,
            observed_at=datetime.now(UTC),
            fingerprint=schema_fingerprint(raw_items),
            discarded=discarded,
            published=published,
            warnings=warnings,
        )


STRATEGY = AlgoliaStrategy()
