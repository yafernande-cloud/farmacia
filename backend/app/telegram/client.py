"""Cliente mínimo de la Bot API. El token nunca se imprime, registra ni incluye en errores."""

import logging
from collections.abc import Callable

import httpx

from app.services.photo_processing import ImageFetcher  # noqa: F401  (el cliente implementa fetch)

# httpx registra la URL completa (con el token) a nivel INFO: se silencia siempre.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class TelegramError(Exception):
    def __init__(self, code: int, description: str, retry_after: int | None = None):
        super().__init__(f"telegram {code}: {description}")  # nunca incluye URL ni token
        self.code, self.description, self.retry_after = code, description, retry_after

    @property
    def permanent(self) -> bool:
        return self.code in (400, 401, 403, 404) and self.code != 429


class TelegramClient:
    def __init__(self, token: str, base: str = "https://api.telegram.org", *,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 35.0):  # fmt: skip
        self._token = token
        self._base = base.rstrip("/")
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout)

    def __repr__(self) -> str:  # evita filtrar el token por un log accidental
        return "TelegramClient(token=***)"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, payload: dict | None = None) -> dict | list | bool:
        try:
            r = await self._http.post(f"{self._base}/bot{self._token}/{method}", json=payload or {})
        except httpx.HTTPError as e:
            raise TelegramError(
                0, f"network:{type(e).__name__}"
            ) from None  # sin str(e): podría traer la URL
        try:
            body = r.json()
        except ValueError:
            raise TelegramError(r.status_code, "invalid_json") from None
        if not body.get("ok"):
            params = body.get("parameters") or {}
            raise TelegramError(
                int(body.get("error_code", r.status_code)),
                str(body.get("description", "")),
                params.get("retry_after"),
            )
        return body["result"]

    async def fetch(self, file_id: str) -> bytes:
        """Descarga una foto por su file_id (getFile + descarga). Implementa ImageFetcher."""
        info = await self.call("getFile", {"file_id": file_id})
        path = info["file_path"]
        try:
            r = await self._http.get(f"{self._base}/file/bot{self._token}/{path}")
        except httpx.HTTPError as e:
            raise TelegramError(0, f"network:{type(e).__name__}") from None
        if r.status_code != 200:
            raise TelegramError(r.status_code, "file_download_failed")
        return r.content


def render(method: str, payload: dict, chat_id: str) -> tuple[str, dict]:
    """Respuesta genérica del canal → llamada de la Bot API."""
    if method == "answerCallbackQuery":
        return method, {"callback_query_id": payload["callback_query_id"]}
    body: dict = {
        "chat_id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
        "text": payload["text"],
    }
    if payload.get("parse_mode"):
        # Solo lo usan respuestas con formato propio (p. ej. la lista con nombres en negrita);
        # el texto dinámico se escapa antes de armar el HTML.
        body["parse_mode"] = payload["parse_mode"]
    if payload.get("buttons"):
        body["reply_markup"] = {"inline_keyboard": [[{"text": b["text"], "callback_data": b["data"]} for b in row]
                                                    for row in payload["buttons"]]}  # fmt: skip
    elif payload.get("keyboard"):
        body["reply_markup"] = {"keyboard": [[{"text": t} for t in row] for row in payload["keyboard"]],
                                "resize_keyboard": True, "is_persistent": True}  # fmt: skip
    return method, body


Transport = Callable[..., httpx.Response]
