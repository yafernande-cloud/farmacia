"""Adaptador de visión con OpenAI para leer fotografías de productos (HLD §8, §17.3).

El adaptador solo DEVUELVE lo que se ve en la imagen, atributo por atributo y con confianza. No
decide si el producto existe, no asocia reportes y no inventa datos: `read_extraction` sigue
aplicando las mismas reglas deterministas que a un texto escrito.

Decisiones operativas:
* Responses API con salida estructurada estricta (`json_schema`, `strict=true`), sin SDK.
* La imagen viaja como `data:` URI en base64; la clave se queda en el backend y nunca se registra.
* `store=false`: la fotografía de la farmacia no queda almacenada en el proveedor.
* Una sola llamada por foto (más un reintento únicamente ante fallos transitorios). El reintento de
  descarga/procesamiento vive en `photo_processing`, no aquí.
* Cualquier fallo se traduce a `VisionFailed` con un código interno; el auxiliar recibe un mensaje
  claro y la foto queda pendiente de revisión, nunca se presenta como registrada.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from collections import deque
from typing import Any

import httpx

from app.core.config import Settings
from app.interpretation.vision import (
    VISION_ATTRIBUTES,
    VisionExtraction,
    VisionFailed,
    VisionField,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
WINDOW_SECONDS = 60.0
SCHEMA_NAME = "vision_extraction"
MAX_FIELDS = 40
MAX_TEXT = 200
# Solo se reintenta lo transitorio: timeout, límite de tasa y 5xx. Credenciales, modelo
# inexistente, solicitud inválida y cuota agotada no mejoran con un segundo intento.
RETRYABLE = {"timeout", "http_429", "http_5xx", "unavailable"}
MAX_PROVIDER_CODE = 64
MAX_REQUEST_ID = 128
_SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_.:-]")


def _safe_token(value: object, limit: int) -> str | None:
    """Deja solo caracteres de identificador: nunca se guarda texto libre del proveedor."""
    if not isinstance(value, str):
        return None
    return _SAFE_TOKEN.sub("", value.strip())[:limit] or None


def _request_id(response: httpx.Response) -> str | None:
    return _safe_token(response.headers.get("x-request-id"), MAX_REQUEST_ID)


def _provider_error(response: httpx.Response) -> tuple[str | None, str | None]:
    """(request_id, código del proveedor) sin conservar el mensaje ni el cuerpo de la respuesta."""
    request_id = _request_id(response)
    try:
        body = response.json()
    except ValueError:
        return request_id, None
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return request_id, None
    error = body["error"]
    return request_id, _safe_token(error.get("code") or error.get("type"), MAX_PROVIDER_CODE)


def _failure(response: httpx.Response) -> VisionFailed:
    """Traduce el estado HTTP a un código **distinto por familia** y adjunta metadatos saneados."""
    status = response.status_code
    request_id, provider_code = _provider_error(response)
    if status == 429:
        # Cuota agotada no es lo mismo que un límite de tasa temporal.
        code = "quota_exceeded" if provider_code == "insufficient_quota" else "http_429"
    elif status >= 500:
        code = "http_5xx"
    else:
        code = f"http_{status}"
    return VisionFailed(
        code,
        http_status=status,
        provider_code=provider_code,
        request_id=request_id,
    )


def _log_failure(exc: VisionFailed) -> None:
    """Registro interno saneado: nunca incluye clave, imagen ni cuerpo del proveedor."""
    log.warning(
        "openai_vision_error code=%s http_status=%s provider_code=%s request_id=%s",
        exc.code,
        exc.http_status,
        exc.provider_code,
        exc.request_id,
    )

SYSTEM_PROMPT = """Eres un lector de envases de farmacia. Mira la fotografía y devuelve SOLO el \
JSON del esquema pedido. Reglas:
- No inventes. Si un dato no se ve con claridad, no lo incluyas.
- Cada dato va en su propio atributo: no mezcles principio activo con marca, ni dosis con contenido.
- `ingredient`: principio activo (paracetamol, ibuprofeno), solo si está escrito.
- `product_name`: nombre comercial tal como aparece en el envase.
- `brand`: marca comercial. `laboratory`: laboratorio fabricante. `manufacturer`,
  `registration_holder`, `marketer`: solo si aparecen escritos y son entidades distintas.
- `variant`: variante visible (aroma, sabor, color, edición), tal como se lee.
- `strength`: potencia o dosis (500 mg, 100 mg/5 ml, 2,5 %). `concentration`: concentración.
- `content`: contenido del envase (50 ml, 20 comprimidos, 400 ml).
- `form`: forma farmacéutica (comprimido, jarabe, crema).
- `packaging`: envase (caja, frasco, tubo).
- `gtin`: código de barras. `sanitary_registration`: registro sanitario.
- `visible_quantity`: cuántos envases se ven en la foto. NO es la cantidad que la persona pide.
- `confidence`: número entre 0 y 1. Si dudas, baja la confianza o no incluyas el dato.
- `legibility`: clear si el envase se lee bien; partial si se lee solo una parte; blurry si está
  borroso; unreadable si no se puede leer nada.
- `warnings`: notas breves de lo que no pudiste leer (nunca datos inventados)."""


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["legibility", "fields", "warnings"],
        "properties": {
            "legibility": {
                "type": "string",
                "enum": ["clear", "partial", "blurry", "unreadable"],
            },
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["attribute", "text", "confidence"],
                    "properties": {
                        "attribute": {"type": "string", "enum": list(VISION_ATTRIBUTES)},
                        "text": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


class OpenAIVisionInterpreter:
    """Interpretación visual real. No se usa en pruebas: la suite inyecta un doble controlado."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 20.0,
        max_output_tokens: int = 800,
        max_calls_per_minute: int = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self._max_calls = max_calls_per_minute
        self._calls: deque[float] = deque()
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> OpenAIVisionInterpreter:
        if settings.openai_api_key is None or not settings.openai_vision_model:
            raise VisionFailed("unavailable")
        return cls(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.openai_vision_model,
            base_url=settings.openai_base_url,
            timeout_seconds=settings.openai_vision_timeout_seconds,
            max_output_tokens=settings.openai_vision_max_output_tokens,
            max_calls_per_minute=settings.openai_vision_max_calls_per_minute,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OpenAIVisionInterpreter:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _check_rate(self) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] >= WINDOW_SECONDS:
            self._calls.popleft()
        if len(self._calls) >= self._max_calls:
            raise VisionFailed("rate_limit")
        self._calls.append(now)

    async def extract(
        self, image: bytes, mime: str, caption: str | None
    ) -> VisionExtraction:
        self._check_rate()
        last: VisionFailed | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return await self._post(image, mime, caption)
            except VisionFailed as exc:
                last = exc
                if exc.code not in RETRYABLE or attempt + 1 >= MAX_ATTEMPTS:
                    break
        if last is None:  # defensivo: el bucle siempre deja un fallo
            last = VisionFailed("unavailable")
        # Se registra una sola vez, ya con el resultado final (con o sin reintento).
        _log_failure(last)
        raise last

    async def _post(
        self, image: bytes, mime: str, caption: str | None
    ) -> VisionExtraction:
        b64 = base64.b64encode(image).decode("ascii")
        context = (
            f' El auxiliar escribió como pie de foto: "{caption}". Úsalo solo como contexto; '
            "no lo presentes como texto leído de la imagen."
            if caption
            else ""
        )
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Lee la fotografía y extrae los datos visibles del producto."
                            + context,
                        },
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime};base64,{b64}",
                            "detail": "auto",
                        },
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": SCHEMA_NAME,
                    "schema": _schema(),
                    "strict": True,
                }
            },
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }
        try:
            response = await self._client.post("/responses", json=payload)
        except httpx.TimeoutException as exc:
            raise VisionFailed("timeout") from exc
        except httpx.HTTPError as exc:
            raise VisionFailed("unavailable") from exc
        if response.status_code >= 400:
            raise _failure(response)
        request_id = _request_id(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise VisionFailed(
                "invalid_response", http_status=response.status_code, request_id=request_id
            ) from exc
        if not isinstance(data, dict):
            raise VisionFailed(
                "invalid_response", http_status=response.status_code, request_id=request_id
            )
        status = data.get("status")
        if status == "incomplete":
            details = data.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            raise VisionFailed(
                "output_truncated" if reason == "max_output_tokens" else "invalid_response",
                http_status=response.status_code,
                request_id=request_id,
            )
        if status == "failed":
            # El proveedor respondió 200 pero marcó la generación como fallida.
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            raise VisionFailed(
                "provider_failed",
                http_status=response.status_code,
                provider_code=_safe_token(
                    error.get("code") or error.get("type"), MAX_PROVIDER_CODE
                ),
                request_id=request_id,
            )
        if status not in (None, "completed"):
            raise VisionFailed(
                "invalid_response", http_status=response.status_code, request_id=request_id
            )
        text = _output_text(data)
        if not text:
            raise VisionFailed(
                "invalid_response", http_status=response.status_code, request_id=request_id
            )
        try:
            body = json.loads(text)
        except ValueError as exc:
            raise VisionFailed(
                "invalid_response", http_status=response.status_code, request_id=request_id
            ) from exc
        try:
            return _extraction(body, data, self.model)
        except VisionFailed as exc:
            # La validación local también conserva la traza del proveedor.
            raise VisionFailed(
                exc.code, http_status=response.status_code, request_id=request_id
            ) from exc


def _output_text(data: dict) -> str:
    texts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                if isinstance(content.get("text"), str):
                    texts.append(content["text"])
    return "\n".join(texts).strip()


def _extraction(body: object, response: dict, fallback_model: str) -> VisionExtraction:
    if not isinstance(body, dict):
        raise VisionFailed("invalid_response")
    legibility = body.get("legibility")
    if legibility not in ("clear", "partial", "blurry", "unreadable"):
        raise VisionFailed("invalid_response")
    raw_fields = body.get("fields")
    raw_warnings = body.get("warnings")
    if not isinstance(raw_fields, list) or not isinstance(raw_warnings, list):
        raise VisionFailed("invalid_response")
    if len(raw_fields) > MAX_FIELDS:
        raise VisionFailed("invalid_response")
    fields: list[VisionField] = []
    seen: set[str] = set()
    for item in raw_fields:
        if not isinstance(item, dict):
            raise VisionFailed("invalid_response")
        attribute = item.get("attribute")
        text = item.get("text")
        confidence = item.get("confidence")
        if (
            attribute not in VISION_ATTRIBUTES
            or not isinstance(text, str)
            or not text.strip()
            or len(text.strip()) > MAX_TEXT
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
        ):
            raise VisionFailed("invalid_response")
        # Un atributo repetido no se descarta en silencio: es una salida ambigua.
        if attribute in seen:
            raise VisionFailed("invalid_response")
        seen.add(attribute)
        fields.append(VisionField(attribute, text.strip(), float(confidence)))
    warnings = tuple(
        w.strip()[:MAX_TEXT] for w in raw_warnings if isinstance(w, str) and w.strip()
    )
    model = response.get("model") if isinstance(response.get("model"), str) else fallback_model
    return VisionExtraction(
        mode="live",
        provider="openai",
        model=(model or fallback_model)[:80],
        legibility=legibility,
        fields=tuple(fields),
        warnings=warnings,
    )
