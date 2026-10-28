#!/usr/bin/env bash
# Arranca el backend en modo desarrollo. Uso: scripts/dev.sh
set -euo pipefail
cd "$(dirname "$0")/../backend"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -e ".[dev]"; }
exec .venv/bin/uvicorn app.main:app --reload --port 8000
