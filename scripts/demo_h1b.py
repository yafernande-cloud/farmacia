#!/usr/bin/env python
"""Demostración H1-B SIN bot real: Telegram SIMULADO + backend real + PostgreSQL local real.

Levanta un Telegram falso en 127.0.0.1:8081 (registra lo que el backend "envía") y el backend en :8000
apuntando a él. Luego envía actualizaciones con la forma de la Bot API al webhook (con el secreto de
origen) y muestra la conversación. La visión es SIMULADA (VISION_PROVIDER=simulated) y se etiqueta así.

Uso:  backend/.venv/bin/python scripts/demo_h1b.py
Requiere: PostgreSQL en marcha, `alembic upgrade head` y `python -m app.cli seed-demo`.
"""

import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
from app.core.config import get_settings  # noqa: E402

TOKEN, SECRET, CHAT = "999999:SIMULADO-demo-no-real", "demo_secreto_origen_0123456789", 5550001
cfg = get_settings()
dsn = cfg.database_url.get_secret_value().replace("+psycopg", "")


def tiny_png(tag: int) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + bytes([tag % 256, 0, 0]))) + chunk(b"IEND", b""))  # fmt: skip


IMG_MIGRANOL, IMG_D3 = tiny_png(1), tiny_png(2)
sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731

# Lo que un proveedor de visión DEVOLVERÍA. Transcripción manual: no lo produjo ninguna IA.
SIM = {
    sha(IMG_MIGRANOL): {"mode": "simulated", "provider": "simulator", "model": "simulated-v0", "legibility": "clear", "warnings": [],
        "fields": [{"attribute": "product_name", "text": "Migranol®", "confidence": .97},
                   {"attribute": "laboratory", "text": "Laboratorio Bagó de Chile S.A.", "confidence": .95},
                   {"attribute": "form", "text": "comprimidos recubiertos", "confidence": .95},
                   {"attribute": "content", "text": "10 comprimidos", "confidence": .95}]},
    sha(IMG_D3): {"mode": "simulated", "provider": "simulator", "model": "simulated-v0", "legibility": "clear", "warnings": [],
        "fields": [{"attribute": "ingredient", "text": "Vitamina D3", "confidence": .95},
                   {"attribute": "strength", "text": "50.000 UI", "confidence": .93},
                   {"attribute": "form", "text": "cápsulas", "confidence": .9},
                   {"attribute": "content", "text": "4 cápsulas", "confidence": .9},
                   {"attribute": "visible_quantity", "text": "3 cajas apiladas", "confidence": .7}]},
}  # fmt: skip

FILES = {"foto_migranol": IMG_MIGRANOL, "foto_d3": IMG_D3, "foto_d3_otra": IMG_D3}
BOT_LOG: list[dict] = []


class FakeTelegram(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silencioso
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # descarga de archivo
        name = self.path.rsplit("/", 1)[-1].removesuffix(".jpg")
        data = FILES.get(name)
        self.send_response(200 if data else 404)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        method = self.path.rsplit("/", 1)[-1]
        if method == "getFile":
            return self._json({"ok": True, "result": {"file_path": f"photos/{body['file_id']}.jpg"}})
        if method == "sendMessage":
            BOT_LOG.append(body)
            return self._json({"ok": True, "result": {"message_id": len(BOT_LOG) + 100}})
        self._json({"ok": True, "result": True})


uid = 0


def upd(kind, *a, **kw):
    global uid
    uid += 1
    m = {"message_id": uid * 10, "date": int(time.time()), "chat": {"id": CHAT, "type": "private"}, "from": {"id": 42}}
    if kind == "text":
        return {"update_id": uid, "message": {**m, "text": a[0]}}
    if kind == "photo":
        p = [{"file_id": a[0], "file_unique_id": "u" + a[0], "width": 1200, "height": 900, "file_size": 1000}]
        return {"update_id": uid, "message": {**m, "photo": p, **({"caption": kw["caption"]} if kw.get("caption") else {})}}
    return {"update_id": uid, "callback_query": {"id": f"cb{uid}", "from": {"id": 42}, "data": a[0], "message": {"message_id": 1, "chat": {"id": CHAT, "type": "private"}}}}


def main():
    # --- SOLO la organización de SIMULACIÓN (nunca la real); ciclo nuevo, sin borrar nada
    sys.path.insert(0, str(ROOT / "scripts"))
    from _sim import prepare_simulation

    _org, users = prepare_simulation(dsn, cfg.app_env.value, bind_chat=True)
    humans = sorted(n for n in users if n != "Pao")  # personal leído de PostgreSQL
    A, L = humans[0], humans[1]
    print("Organización de simulación · personal:", ", ".join(users))

    srv = HTTPServer(("127.0.0.1", 8081), FakeTelegram)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp(prefix="farmacia_demo_")
    simfile = Path(tmp) / "sim.json"
    simfile.write_text(json.dumps(SIM))
    env = {**os.environ, "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_WEBHOOK_SECRET": SECRET, "TELEGRAM_API_BASE": "http://127.0.0.1:8081",
           "VISION_PROVIDER": "simulated", "VISION_SIMULATION_FILE": str(simfile), "STORAGE_DIR": str(Path(tmp) / "storage"), "AUTH_MODE": "demo_header"}  # fmt: skip
    api = subprocess.Popen([str(BACKEND / ".venv/bin/uvicorn"), "app.main:app", "--port", "8000"], cwd=BACKEND, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
    try:
        for _ in range(50):
            try:
                if httpx.get("http://127.0.0.1:8000/api/v1/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        hook = lambda u: httpx.post("http://127.0.0.1:8000/api/v1/integrations/telegram/webhook", json=u,  # noqa: E731
                                    headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}, timeout=20)  # fmt: skip
        pao = {"X-Demo-User-Id": users["Pao"]}
        shown = 0

        def say(who, text, u):
            nonlocal shown
            print(f"\n  👤 {who}: {text}")
            r = hook(u)
            assert r.status_code == 200, r.text
            time.sleep(0.8)  # el webhook responde primero; respuestas y fotos se procesan después
            for b in BOT_LOG[shown:]:
                btn = b.get("reply_markup", {}).get("inline_keyboard")
                extra = "   [" + " | ".join(x["text"] for row in btn for x in row) + "]" if btn else ""
                print("  🤖 " + b["text"].replace("\n", "\n     ") + extra)
            shown = len(BOT_LOG)

        def sec(t):
            print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")

        sec("0) Catálogo verificado por Pao: «vitamina d 50000 UI cápsulas» (información que respalda el candidato)")
        r = httpx.post("http://127.0.0.1:8000/api/v1/pharma-identities", json={"text": "vitamina d 50000 UI cápsulas"}, headers=pao)
        print("  ", r.status_code, r.json()["label"])

        sec(f"1) {A} se identifica y reporta «falta vitamina D 50000» (sin unidad)")
        say("(teléfono)", "/start", upd("text", "/start"))
        say(A, f"pulsa [{A}]", upd("cb", f"sel:{users[humans[0]]}"))
        say(A, "falta vitamina D 50000", upd("text", "falta vitamina D 50000"))

        sec(f"2) {L} cambia la identidad activa y reporta lo mismo → una necesidad, dos avisos, sin sumar")
        say("(teléfono)", "Cambiar auxiliar", upd("text", "Cambiar auxiliar"))
        say(L, f"pulsa [{L}]", upd("cb", f"sel:{users[humans[1]]}"))
        say(L, "no queda vitamina D 50000", upd("text", "no queda vitamina D 50000"))
        dup = upd("text", "no queda la D de cincuenta mil")
        say(L, "no queda la D de cincuenta mil   («la D» no es un alias verificado: va a revisión)", dup)
        n = len(BOT_LOG)
        hook(dup)
        time.sleep(0.5)
        print(f"\n  (Telegram reenvía el MISMO evento: respuestas nuevas = {len(BOT_LOG) - n}; no hay segunda observación)")

        sec("3) Fotografía SIN texto — caja de Migranol (lectura SIMULADA: no es una IA real)")
        say(L, "[foto de la caja]", upd("photo", "foto_migranol"))

        sec("4) Fotografía + texto «faltan 2 cajas» — Vitamina D3 (lectura SIMULADA)")
        say(L, "[foto] «faltan 2 cajas»", upd("photo", "foto_d3", caption="faltan 2 cajas"))
        say(L, "[la MISMA foto reenviada]", upd("photo", "foto_d3_otra"))

        sec("5) Sin identidad activa: el mensaje queda pendiente y NO se atribuye")
        say("(teléfono)", "/salir", upd("text", "/salir"))
        say("alguien", "falta paracetamol 500 mg", upd("text", "falta paracetamol 500 mg"))

        sec("6) Lista compartida")
        say("(teléfono)", "Ver lista", upd("text", "Ver lista"))

        sec("Estado en PostgreSQL")
        with psycopg.connect(dsn) as db:
            print("  Observaciones (autor · origen · identidad · resultado):")
            for r in db.execute("""SELECT u.display_name, o.source, o.identity_level, o.match_status, left(o.parsed->>'display_name', 40), o.declared_quantity
                                   FROM demand_observation o JOIN app_user u ON u.id=o.author_id ORDER BY o.created_at"""):
                print(f"    {r[0]:5} {r[1]:14} {r[2]:8} {r[3]:18} {r[4]!s:42} sugerida={r[5]}")
            print("  Necesidades (cantidad objetivo):")
            for r in db.execute("SELECT display_name, target_quantity, quantity_unit, attributes->'name_anchor' FROM demand_item ORDER BY created_at"):
                print(f"    {r[0]:44} cantidad={r[1]}  ancla_por_marca={r[3]}")
            print("  Fotos:", db.execute("SELECT status, extraction_mode, count(*) FROM attachment GROUP BY 1,2 ORDER BY 1").fetchall())
            print("  Mensajes pendientes de atribución:", db.execute("SELECT count(*) FROM channel_message WHERE attribution_status='pending_attribution'").fetchone()[0])
            print("  Auditoría de sesiones:", db.execute("SELECT action, count(*) FROM audit_event WHERE action LIKE 'session.%' GROUP BY 1 ORDER BY 1").fetchall())
        print("\nDemostración completada. Telegram: SIMULADO · Visión: SIMULADA · PostgreSQL: real.")
    finally:
        api.terminate()
        srv.shutdown()


if __name__ == "__main__":
    main()
