#!/usr/bin/env bash
# ENSAYO de la migración sobre una COPIA (farmacia_rehearsal). NUNCA toca la base `farmacia`.
# Uso: scripts/migration/rehearse.sh .backups/farmacia_pre_separacion_XXXX.dump
set -euo pipefail
DUMP="${1:?indica el archivo .dump}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PSQL="docker exec -i database-postgres-1 psql -U farmacia -X -q -v ON_ERROR_STOP=1"
REH=farmacia_rehearsal
echo "== 1) copia de ensayo ($REH) desde $DUMP"
docker exec database-postgres-1 psql -U farmacia -X -q -d postgres -c "DROP DATABASE IF EXISTS $REH" -c "CREATE DATABASE $REH"
docker exec -i database-postgres-1 pg_restore -U farmacia -d $REH --no-owner < "$DUMP"
URL="$(cd "$ROOT/backend" && .venv/bin/python -c "from app.core.config import get_settings;print(get_settings().database_url.get_secret_value().rsplit('/',1)[0]+'/$REH')")"
echo "== 2) esquema 0004 y 0005 sobre la copia (aditivo)"
(cd "$ROOT/backend" && DATABASE_URL="$URL" .venv/bin/alembic upgrade head 2>&1 | tail -3)
echo "== 3) huella ANTES de los datos"
$PSQL -d $REH -f - < "$ROOT/scripts/migration/fingerprint.sql" > /tmp/fp_before.txt; cat /tmp/fp_before.txt
echo "== 4) migración de datos (una transacción)"
$PSQL -d $REH -f - < "$ROOT/scripts/migration/0006_separar_organizaciones.sql"
echo "== 5) personal desde database/seed/staff.json (CLI) en las organizaciones nuevas"
(cd "$ROOT/backend" && DATABASE_URL="$URL" .venv/bin/python -m app.cli seed-demo && DATABASE_URL="$URL" .venv/bin/python -m app.cli seed-real-test)
echo "== 6) huella DESPUÉS y comparación"
$PSQL -d $REH -f - < "$ROOT/scripts/migration/fingerprint.sql" > /tmp/fp_after.txt
if diff /tmp/fp_before.txt /tmp/fp_after.txt; then echo "HUELLAS IDÉNTICAS: autoría, fechas, textos, cantidades y referencias no cambiaron"; else echo "DIFERENCIAS (ver arriba)"; exit 1; fi
echo "== 7) verificaciones"
$PSQL -d $REH -f - < "$ROOT/scripts/migration/verify_0006.sql"
