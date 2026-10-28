"""Preparación COMÚN de las demos: solo la organización de SIMULACIÓN, jamás la real, y sin borrar nada.

Cada corrida cierra el ciclo abierto de la simulación y abre uno nuevo, de modo que las necesidades de una
demo anterior no se mezclen con la actual (la deduplicación es por ciclo). No hay TRUNCATE ni DELETE.
"""

import sys
from datetime import datetime

import psycopg

SIM_CHAT = "5550001"


def prepare_simulation(dsn: str, app_env: str, *, bind_chat: bool = False) -> tuple[str, dict[str, str]]:
    if app_env != "local":
        sys.exit("Las demos solo corren con APP_ENV=local")
    with psycopg.connect(dsn, autocommit=True) as db:
        try:
            row = db.execute("SELECT id FROM organization WHERE kind = 'simulation'").fetchall()
        except psycopg.errors.UndefinedColumn:
            sys.exit("La base aún no tiene la separación de organizaciones (migración 0004 pendiente de aprobación).")
        if len(row) != 1:
            sys.exit("Falta la organización de simulación. Ejecuta: python -m app.cli seed-demo")
        org = str(row[0][0])
        db.execute("UPDATE purchase_cycle SET status='closed', closed_at=now() WHERE organization_id=%s AND status='open'", (org,))
        db.execute("INSERT INTO purchase_cycle (organization_id, name) VALUES (%s, %s)", (org, f"Simulación {datetime.now():%Y-%m-%d %H:%M:%S}"))
        if bind_chat:
            db.execute("INSERT INTO channel_binding (organization_id, external_chat_id) SELECT %s, %s "
                       "WHERE NOT EXISTS (SELECT 1 FROM channel_binding WHERE provider='telegram' AND external_chat_id=%s AND status='active')",
                       (org, SIM_CHAT, SIM_CHAT))  # fmt: skip
        users = dict(db.execute("SELECT display_name, id::text FROM app_user WHERE organization_id=%s AND active", (org,)).fetchall())
    return org, users
