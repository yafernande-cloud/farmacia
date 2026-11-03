"""SIMULADOR de Telegram para pruebas. NO es el servicio real de Telegram.

Genera actualizaciones con la forma de la Bot API y emula sus endpoints con httpx.MockTransport.
Lo verificado aquí es nuestra lógica y el formato de las llamadas; NO se ha probado contra los
servidores reales de Telegram (falta el token del bot).
"""

import json

import httpx

TOKEN = "123456:SIMULADO-no-es-un-token-real"


def text_update(uid: int, chat: str | int, text: str, ts: int = 1_790_000_000, *,
                from_id: int = 42, chat_kind: str = "private") -> dict:  # fmt: skip
    return {"update_id": uid, "message": {"message_id": uid * 10, "date": ts,
                                          "chat": {"id": int(chat), "type": chat_kind},
                                          "from": {"id": from_id, "is_bot": False, "first_name": "Telefono"},
                                          "text": text}}  # fmt: skip


def callback_update(uid: int, chat: str | int, data: str, cbid: str | None = None, *,
                    from_id: int = 42, chat_kind: str = "private") -> dict:  # fmt: skip
    return {"update_id": uid, "callback_query": {"id": cbid or f"cb{uid}",
                                                  "from": {"id": from_id, "first_name": "Telefono"},
                                                  "data": data,
                                                  "message": {"message_id": 1,
                                                              "chat": {"id": int(chat), "type": chat_kind}}}}  # fmt: skip


def photo_update(
    uid: int, chat: str | int, file_id: str, caption: str | None = None, ts: int = 1_790_000_000,
    *, from_id: int = 42, chat_kind: str = "private",
) -> dict:  # fmt: skip
    m = {"message_id": uid * 10, "date": ts, "chat": {"id": int(chat), "type": chat_kind},
         "from": {"id": from_id, "is_bot": False},
         "photo": [{"file_id": file_id + "_s", "file_unique_id": "u_" + file_id, "width": 90, "height": 90, "file_size": 900},
                   {"file_id": file_id, "file_unique_id": "u_" + file_id, "width": 1280, "height": 960, "file_size": 150000}]}  # fmt: skip
    if caption:
        m["caption"] = caption
    return {"update_id": uid, "message": m}


def edited_update(uid: int, chat: str | int, text: str, *, from_id: int = 42,
                  chat_kind: str = "private") -> dict:  # fmt: skip
    u = text_update(uid, chat, text, from_id=from_id, chat_kind=chat_kind)
    return {"update_id": uid, "edited_message": u["message"]}


def fake_jpeg(tag: int = 0) -> bytes:
    """Bytes con firma JPEG válida (no es una foto real; sirve para validar tipo, hash y almacenamiento)."""
    return b"\xff\xd8\xff\xe0\x00\x10JFIF" + f"SIMULADA-{tag}".encode() * 20 + b"\xff\xd9"


class FakeTelegramAPI:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []  # (método, cuerpo) de lo que NOSOTROS enviamos
        self.files: dict[str, bytes] = {}  # file_id → bytes
        self.failures: list[tuple[int, str, int | None]] = []
        self.method_failures: dict[
            str, list[tuple[int, str, int | None]]
        ] = {}  # fallos solo para un método  # errores a inyectar (código, descripción, retry_after)
        self.updates: list[dict] = []
        self._mid = 1000
        self.transport = httpx.MockTransport(self._handle)

    def sent(self, method: str = "sendMessage") -> list[dict]:
        return [b for m, b in self.calls if m == method]

    def texts(self) -> list[str]:
        return [b["text"] for b in self.sent()]

    def last_buttons(self) -> list[dict]:
        rows = self.sent()[-1].get("reply_markup", {}).get("inline_keyboard", [])
        return [b for row in rows for b in row]

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        assert f"/bot{TOKEN}/" in path or f"/file/bot{TOKEN}/" in path, (
            "token mal formado en la URL"
        )
        if path.startswith(f"/file/bot{TOKEN}/"):
            fid = path.rsplit("/", 1)[-1].removesuffix(".jpg")
            data = self.files.get(fid)
            return httpx.Response(200, content=data) if data is not None else httpx.Response(404)
        method = path.rsplit("/", 1)[-1]
        body = json.loads(req.content or b"{}")
        if method != "getUpdates":
            self.calls.append((method, body))
        queue = self.method_failures.get(method) or self.failures
        if queue:
            code, desc, retry = queue.pop(0)
            err = {"ok": False, "error_code": code, "description": desc}
            if retry:
                err["parameters"] = {"retry_after": retry}
            return httpx.Response(code, json=err)
        if method == "getFile":
            if body["file_id"] not in self.files:
                return httpx.Response(
                    400, json={"ok": False, "error_code": 400, "description": "file not found"}
                )
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "file_id": body["file_id"],
                        "file_path": f"photos/{body['file_id']}.jpg",
                    },
                },
            )
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": ups})
        if method == "getMe":
            return httpx.Response(
                200, json={"ok": True, "result": {"id": 123456, "username": "bot_simulado"}}
            )
        self._mid += 1
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": self._mid} if method == "sendMessage" else True,
            },
        )
