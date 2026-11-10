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


def _normalise(item: Any, specs: dict[str, FieldSpec], cfg: AlgoliaConfig) -> SupplierOffer | None:
    """Convierte un ítem crudo en oferta. Si falta un dato obligatorio, devuelve `None`.

    Nunca completa un dato ausente con 0, 1 ni un texto inventado: el llamador cuenta el descarte.
    """
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

    external_id = _clean_text(values.get("external_id"), 120)
    name = _clean_text(values.get("name"), 300)
    price = values.get("price")
    if not isinstance(price, Decimal) or price <= 0:
        return None
    currency = _clean_text(values.get("currency"), 3) or cfg.currency
    if currency is None:
        return None
    currency = currency.upper()
    if CURRENCY.fullmatch(currency) is None:
        return None
    if external_id is None or name is None:
        return None

    # Un campo marcado `required` también descarta el ítem si no se pudo leer.
    for field_name, spec in specs.items():
        if spec.required and values.get(field_name) is None:
            return None

    stock_quantity: Decimal | None = None
    stock_status = "unknown"
    stock_unit: str | None = None
    if cfg.stock_unit is not None and "stock" in specs:
        stock_quantity = values.get("stock")
        if isinstance(stock_quantity, Decimal):
            stock_unit = cfg.stock_unit
            stock_status = "in_stock" if stock_quantity > 0 else "out_of_stock"
        else:
            stock_quantity = None

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
        seen: set[str] = set()
        discarded = 0
        for item in raw_items[:wanted]:
            offer = _normalise(item, specs, cfg)
            if offer is None:
                discarded += 1
                continue
            if offer.external_id in seen:
                discarded += 1
                continue
            seen.add(offer.external_id)
            offers.append(offer)

        if raw_items and not offers:
            raise ConnectorIntervention(
                "field_mapping_failed",
                details={"items": len(raw_items), "discarded": discarded},
            )

        warnings: list[str] = []
        if discarded:
            warnings.append(f"{discarded} ítem(s) descartados por datos incompletos o inválidos.")
        if not raw_items:
            warnings.append("La consulta no devolvió resultados.")

        return ConnectorResult(
            offers=offers,
            observed_at=datetime.now(UTC),
            fingerprint=schema_fingerprint(raw_items),
            discarded=discarded,
            warnings=warnings,
        )


STRATEGY = AlgoliaStrategy()
