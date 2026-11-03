"""Adaptador del proveedor OpenAI para interpretar observaciones comerciales (O2 / A6 Fase 3).

El resto del backend solo conoce `ObservationModel` y `RawCompletion`: sustituir el proveedor no
cambia la validación, la persistencia ni el motor de orden. El adaptador:

* usa la **Responses API** con salida estructurada estricta (`json_schema`, `strict=true`);
* no guarda la conversación en el proveedor (`store=false`) y nunca registra la clave;
* traduce cada fallo a un código **interno** (`timeout`, `rate_limit`, `quota_exceeded`,
  `unavailable`, `invalid_response`, `output_truncated`) sin exponer cuerpos del proveedor;
* una respuesta `incomplete` por `max_output_tokens` **no se parsea ni se reintenta**: el mismo
  límite volvería a truncarla y una preferencia nunca se aplica desde una salida parcial;
* reintenta **una sola vez** los fallos transitorios; cuota, credenciales y errores de solicitud
  no se reintentan;
* limita las llamadas por minuto en el proceso. No hay colas ni workers (contrato §8.5).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import Settings
from app.interpretation.commercial import OUTPUT_SCHEMA_NAME

MAX_ATTEMPTS = 2
WINDOW_SECONDS = 60.0


class ModelError(RuntimeError):
    """Fallo interno del proveedor. `code` nunca se muestra al usuario."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class RawCompletion:
    text: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


class ObservationModel(Protocol):
    async def complete(
        self, *, system: str, user: str, json_schema: dict
    ) -> RawCompletion: ...


class OpenAIResponsesModel:
    """Adaptador HTTP mínimo (sin SDK) contra `POST /v1/responses`."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 20.0,
        max_output_tokens: int = 2500,
        reasoning_effort: str | None = "low",
        max_calls_per_minute: int = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
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
    ) -> OpenAIResponsesModel:
        if settings.openai_api_key is None or not settings.openai_model:
            raise ModelError("unavailable")
        return cls(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            timeout_seconds=settings.openai_timeout_seconds,
            max_output_tokens=settings.openai_max_output_tokens,
            reasoning_effort=settings.openai_reasoning_effort,
            max_calls_per_minute=settings.openai_max_calls_per_minute,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OpenAIResponsesModel:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _check_rate(self) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] >= WINDOW_SECONDS:
            self._calls.popleft()
        if len(self._calls) >= self._max_calls:
            raise ModelError("rate_limit")
        self._calls.append(now)

    async def complete(
        self, *, system: str, user: str, json_schema: dict
    ) -> RawCompletion:
        self._check_rate()
        last: ModelError | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return await self._post(system, user, json_schema)
            except ModelError as exc:
                last = exc
                if not exc.retryable or attempt + 1 >= MAX_ATTEMPTS:
                    raise
        raise last or ModelError("unavailable")

    async def _post(self, system: str, user: str, json_schema: dict) -> RawCompletion:
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": user}]},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": OUTPUT_SCHEMA_NAME,
                    "schema": json_schema,
                    "strict": True,
                }
            },
            "max_output_tokens": self.max_output_tokens,
            # La observación es del cliente: no se deja almacenada en el proveedor.
            "store": False,
        }
        if self.reasoning_effort is not None:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        started = time.monotonic()
        try:
            response = await self._client.post("/responses", json=payload)
        except httpx.TimeoutException as exc:
            raise ModelError("timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ModelError("unavailable", retryable=True) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            raise _error_for(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ModelError("invalid_response") from exc
        if not isinstance(data, dict):
            raise ModelError("invalid_response")

        status = data.get("status")
        if status == "incomplete":
            details = data.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            # Truncada por el límite: no se reintenta (el mismo límite la volvería a cortar) y
            # nunca se parsea el fragmento. El servicio la registra como intento fallido.
            if reason == "max_output_tokens":
                raise ModelError("output_truncated")
            raise ModelError("invalid_response")
        if status == "failed":
            raise ModelError("unavailable")
        if status not in (None, "completed"):
            raise ModelError("invalid_response")

        texts: list[str] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    texts.append(content["text"])
        text = "\n".join(texts).strip()
        if not text:
            # Una negativa del modelo es una respuesta sin propuesta válida, no una preferencia.
            raise ModelError("invalid_response")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        model = data.get("model") if isinstance(data.get("model"), str) else self.model
        return RawCompletion(
            text=text,
            model=(model or self.model)[:80],
            input_tokens=_token(usage.get("input_tokens")),
            output_tokens=_token(usage.get("output_tokens")),
            latency_ms=latency_ms,
        )


def _token(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _error_for(response: httpx.Response) -> ModelError:
    """Traduce el estado HTTP a un código interno. Nunca propaga el cuerpo al usuario."""
    status = response.status_code
    if status in (401, 403):
        return ModelError("unavailable")
    if status == 429:
        code = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    code = str(error.get("code") or error.get("type") or "")
        except ValueError:
            code = ""
        if "insufficient_quota" in code:
            return ModelError("quota_exceeded")
        return ModelError("rate_limit", retryable=True)
    if status >= 500:
        return ModelError("unavailable", retryable=True)
    return ModelError("unavailable")
