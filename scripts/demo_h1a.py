#!/usr/bin/env python
"""Demostración H1-A (modelo de producto v2) contra el servidor real (HTTP + PostgreSQL local).

Uso:  backend/.venv/bin/python scripts/demo_h1a.py [--base http://localhost:8000]
Usa EXCLUSIVAMENTE la organización de simulación (kind=simulation) y abre un ciclo nuevo; no borra datos.
Requiere: servidor en marcha, `alembic upgrade head` y `python -m app.cli seed-demo`.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.core.config import get_settings  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:8000")
args = ap.parse_args()
cfg = get_settings()
dsn = cfg.database_url.get_secret_value().replace("+psycopg", "")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sim import prepare_simulation  # noqa: E402

_org, ids = prepare_simulation(dsn, cfg.app_env.value)  # SOLO la organización de simulación; ciclo nuevo, sin borrar nada

c = httpx.Client(base_url=args.base + "/api/v1", timeout=10)


def call(who, method, path, key=True, version=None, **kw):
    h = {"X-Demo-User-Id": ids[who]}
    if key:
        h["Idempotency-Key"] = key if isinstance(key, str) else str(uuid.uuid4())
    if version is not None:
        h["If-Match"] = f'"{version}"'
    return c.request(method, path, headers=h, **kw)


def say(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def show(r, *fields):
    b = r.json()
    print(f"  HTTP {r.status_code}", {k: b.get(k) for k in fields} if fields else "")
    return b


def lista():
    items = call("Pao", "GET", "/demand-items", key=False).json()["items"]
    for i in items:
        pend = [f"{p['attribute']}:{p['reason']}" for p in i["pending_validation"] if p["reason"] != "not_stated"]
        print(f"  · {i['display_name']:<34} cantidad={i['target_quantity']!s:<5} {i['quantity_unit'] or '-':<4} "
              f"avisos={i['observation_count']} {i['reporters']} v{i['version']} por-confirmar={pend}")  # fmt: skip
    return items


def attrs(r):
    a = r.json()["item"]["attributes"]
    return a


say("1) Dos auxiliares reportan el mismo ibuprofeno 400 mg (unidad escrita por ambos)")
r1 = show(call("Ana", "POST", "/observations", json={"text": "falta ibuprofeno de 400 mg"}), "outcome")
r2 = show(call("Luis", "POST", "/observations", json={"text": "no queda ibuprofeno 400 mg"}), "outcome", "quantity_changed")
items = lista()
assert len(items) == 1 and items[0]["observation_count"] == 2 and items[0]["target_quantity"] is None
item_400 = items[0]["id"]

say("2) Una cifra SIN unidad no se convierte en mg: queda pendiente y el aviso no pregunta nada")
r = call("Ana", "POST", "/observations", json={"text": "falta paracetamol de 500"})
b = r.json()
print(f"  HTTP {r.status_code} outcome={b['outcome']}")
print(f"  mensaje al auxiliar: {b['message']}")
print(f"  potencias={b['item']['attributes']['strengths']}  sin_unidad={[m['label'] for m in b['item']['attributes']['unassigned']]}")
assert b["item"]["attributes"]["strengths"] == [] and "?" not in b["message"]
item_500 = b["item"]["id"]

say("3) Otro auxiliar sí escribe la unidad: NO se fusiona solo; queda para que Pao confirme en un paso")
b = call("Luis", "POST", "/observations", json={"text": "no queda paracetamol 500 mg"}).json()
print(f"  outcome={b['outcome']}  motivos={b['observation']['candidates'][0]['reasons']}")
assert b["outcome"] == "needs_review"
obs_id = b["observation"]["id"]
r = call("Pao", "POST", f"/observations/{obs_id}/resolve", key=False,
         json={"demand_item_id": item_500, "apply_observed_attributes": True, "remember_alias": True})  # fmt: skip
a = r.json()["item"]["attributes"]["strengths"][0]
print(f"  Pao confirma: potencia={a['label']} procedencia={a['basis']} · alias recordado={r.json()['alias_created']}")

say("4) Con la memoria verificada, «paracetamol 500» a secas se resuelve solo (sin cola, sin pregunta)")
b = call("Ana", "POST", "/observations", json={"text": "falta paracetamol 500"}).json()
s0 = b["observation"]["attributes"]["strengths"][0]
print(f"  outcome={b['outcome']}  potencia={s0['label']} procedencia={s0['basis']} regla={s0['role_rule']}")
b2 = call("Ana", "POST", "/observations", json={"text": "falta paracetamol 600"}).json()
print(f"  «paracetamol 600» (otra cifra, sin memoria) → {b2['outcome']}, sin unidad: {[m['label'] for m in b2['item']['attributes']['unassigned']]}")
assert b["outcome"] == "attached_existing" and b2["item"]["attributes"]["strengths"] == []

say("5) Unidades simples y compuestas: se conserva lo escrito y se normaliza solo para comparar")
for t in ["falta levotiroxina 50 µg", "falta ibuprofeno 1.000 mg", "falta amoxicilina 250 mg/5 ml jarabe 120 ml", "falta vitamina d 1000 UI"]:
    b = call("Ana", "POST", "/observations", json={"text": t}).json()
    st = b["item"]["attributes"]["strengths"][0]
    ct = b["item"]["attributes"]["content"]
    print(f"  {t!r:50} escrito={st['as_written']['original_text']!r:14} normalizado={st['normalized']['value']} {st['normalized']['unit']}"
          + (f" · contenido={ct['label']}" if ct else ""))  # fmt: skip

say("6) Potencia, contenido, presentación y cantidad solicitada son cosas distintas")
b = call("Ana", "POST", "/observations", json={"text": "necesitamos 5 cajas de ibuprofeno 600 mg comprimidos caja de 20"}).json()
a = b["item"]["attributes"]
print(f"  potencia={[x['label'] for x in a['strengths']]} contenido={a['content']['label']} forma={a['form']['value']} "
      f"cantidad_sugerida={b['observation']['declared_quantity']} {b['observation']['declared_unit']}  → cantidad objetivo={b['item']['target_quantity']}")
assert b["item"]["target_quantity"] is None

say("7) Aviso repetido NO suma; SET y luego ADD idempotente (misma clave) suma una sola vez")
def cmd(v, op, val, key):
    return call("Pao", "POST", f"/demand-items/{item_400}/quantity-commands", key=key, version=v, json={"operation": op, "value": val, "unit": "box"}).json()
cmd(1, "SET", "5", str(uuid.uuid4()))
for who in ("Ana", "Luis", "Ana"):
    call(who, "POST", "/observations", json={"text": "falta ibuprofeno 400 mg"})
k = "demo-add-" + str(uuid.uuid4())
for n in (1, 2, 3):
    b = cmd(2, "ADD", "5", k)
    print(f"  ADD intento {n}: replayed={b['replayed']} cantidad={b['item']['target_quantity']}")
lista()
final = next(i for i in call("Pao", "GET", "/demand-items", key=False).json()["items"] if i["id"] == item_400)
assert final["target_quantity"] == "10" and final["observation_count"] == 5, final

print("\nTodas las comprobaciones de la demostración pasaron.")
