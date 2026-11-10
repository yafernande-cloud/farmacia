"""Validación estricta de perfiles de conexión.

La configuración es declarativa y se valida **al guardar**, no en cada búsqueda. Un perfil inválido
no se guarda; un perfil válido no puede ejecutar código. En esta etapa solo está implementada la
estrategia `algolia`; las demás existen en el enum para poder incorporarlas sin cambiar el modelo.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.errors import DomainError

SECRET_REF = re.compile(r"^[A-Z][A-Z0-9_]{2,80}$")
CURRENCY = re.compile(r"^[A-Z]{3}$")

CANONICAL_FIELDS = {
    "external_id",
    "name",
    "laboratory",
    "presentation",
    "price",
    "currency",
    "stock",
    "available_for_sale",
    "reference_price",
    "image_url",
    "product_url",
    "ean",
}
REQUIRED_FIELDS = ("external_id", "name", "price")
EXPECTED_TYPES = {
    "external_id": "string",
    "name": "string",
    "laboratory": "string",
    "presentation": "string",
    "price": "decimal",
    "currency": "string",
    "stock": "decimal",
    # Señal explícita del proveedor sobre si la publicación está disponible para la venta.
    "available_for_sale": "bool",
    # Precio de lista del catálogo. Nunca se usa como precio de venta ni alimenta el carrito.
    "reference_price": "decimal",
    "image_url": "url",
    "product_url": "url",
    "ean": "string",
}


class FieldSpec(BaseModel):
    """Cómo leer un campo canónico de un ítem. Una sola fuente: ruta, constante o plantilla."""

    model_config = ConfigDict(extra="forbid")

    path: str | None = Field(default=None, max_length=200)
    const: Any | None = None
    template: str | None = Field(default=None, max_length=500)
    type: Literal["string", "decimal", "int", "url", "bool"] = "string"
    transform: Literal["first", "sum", "join"] = "first"
    required: bool = False

    @model_validator(mode="after")
    def _una_sola_fuente(self) -> FieldSpec:
        sources = sum(
            [self.path is not None, self.const is not None, self.template is not None]
        )
        if sources != 1:
            raise ValueError("Cada campo debe declarar exactamente una fuente: path, const o template.")
        if self.path is not None and not self.path.strip():
            raise ValueError("La ruta no puede quedar vacía.")
        if self.template is not None and not self.template.strip():
            raise ValueError("La plantilla no puede quedar vacía.")
        return self


class AlgoliaConfig(BaseModel):
    """Parámetros de una consulta Algolia. La clave se referencia por variable de entorno."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=8, max_length=500)
    app_id: str | None = Field(default=None, min_length=1, max_length=80)
    app_id_secret_ref: str | None = Field(default=None, max_length=80)
    index: str = Field(min_length=1, max_length=120)
    api_key_secret_ref: str = Field(min_length=3, max_length=80)
    query_param: str = Field(default="query", min_length=1, max_length=60)
    hits_per_page: int = Field(default=15, ge=1, le=50)
    page_param: str = Field(default="page", min_length=1, max_length=60)
    page: int = Field(default=0, ge=0, le=100)
    filters: str | None = Field(default=None, max_length=1000)
    referer: str | None = Field(default=None, max_length=500)
    origin: str | None = Field(default=None, max_length=500)
    timeout_seconds: int = Field(default=10, ge=1, le=30)
    max_response_bytes: int = Field(default=2_000_000, ge=10_000, le=10_000_000)
    item_path: str = Field(default="hits", min_length=1, max_length=120)
    currency: str | None = Field(default=None, max_length=3)
    sale_unit: Literal["box", "package", "unit", "bottle"] | None = None
    price_basis: Literal["per_commercial_unit", "per_base_unit"] | None = None
    stock_unit: Literal["box", "package", "unit", "bottle"] | None = None

    @field_validator("endpoint")
    @classmethod
    def _endpoint_http(cls, value: str) -> str:
        text = value.strip()
        if not (text.startswith("https://") or text.startswith("http://")):
            raise ValueError("El endpoint debe empezar por http:// o https://.")
        return text

    @field_validator("app_id_secret_ref", "api_key_secret_ref")
    @classmethod
    def _ref_valida(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if SECRET_REF.fullmatch(text) is None:
            raise ValueError(
                "La referencia de secreto debe ser un nombre de variable de entorno "
                "(mayúsculas, números y guion bajo)."
            )
        return text

    @field_validator("currency")
    @classmethod
    def _currency_valida(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip().upper()
        if CURRENCY.fullmatch(text) is None:
            raise ValueError("La moneda debe ser un código ISO de tres letras.")
        return text

    @field_validator("referer", "origin")
    @classmethod
    def _url_opcional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not (text.startswith("https://") or text.startswith("http://")):
            raise ValueError("La URL debe empezar por http:// o https://.")
        return text

    @model_validator(mode="after")
    def _app_id(self) -> AlgoliaConfig:
        if (self.app_id is None) == (self.app_id_secret_ref is None):
            raise ValueError(
                "Declara `app_id` o `app_id_secret_ref`, pero no ambos ni ninguno."
            )
        return self


def _invalid(message: str) -> DomainError:
    return DomainError("INVALID_CONNECTOR_PROFILE", message, status_code=422)


def validate_profile(
    mechanism: str, config: dict[str, Any], field_map: dict[str, Any]
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Valida y normaliza el perfil. Devuelve `(mechanism, config, field_map)` listos para guardar."""
    if mechanism != "algolia":
        raise DomainError(
            "CONNECTOR_MECHANISM_NOT_IMPLEMENTED",
            f"El mecanismo «{mechanism}» todavía no tiene estrategia implementada. "
            "En esta etapa solo está disponible «algolia».",
            status_code=422,
        )
    if not isinstance(field_map, dict) or not field_map:
        raise _invalid("El mapeo de campos no puede quedar vacío.")
    unknown = sorted(set(field_map) - CANONICAL_FIELDS)
    if unknown:
        raise _invalid(f"Campos no reconocidos en el mapeo: {', '.join(unknown)}.")
    for field in REQUIRED_FIELDS:
        if field not in field_map:
            raise _invalid(f"Falta el campo obligatorio «{field}» en el mapeo.")
    try:
        parsed_config = AlgoliaConfig.model_validate(config)
        parsed_map = {
            name: FieldSpec.model_validate(spec).model_dump(exclude_none=True)
            for name, spec in field_map.items()
        }
    except Exception as exc:  # Pydantic entrega el detalle; no se filtra al cliente.
        raise _invalid(f"El perfil no es válido: {exc}") from exc

    for name, expected in EXPECTED_TYPES.items():
        if name in parsed_map and parsed_map[name].get("type") != expected:
            raise _invalid(
                f"El campo «{name}» debe declarar type=\"{expected}\"; "
                "así se evita interpretar un dato con el formato equivocado."
            )

    if "currency" not in parsed_map and parsed_config.currency is None:
        raise _invalid(
            "El perfil debe declarar la moneda: un campo «currency» o la opción `currency`."
        )
    # Declarar la ruta de stock sin `stock_unit` es válido, pero la estrategia no expone la
    # cantidad hasta que una persona confirme la unidad: mejor desconocida que inventada.
    return mechanism, parsed_config.model_dump(exclude_none=True), parsed_map
