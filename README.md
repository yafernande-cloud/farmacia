# Faltantes Farmacia — Asistente Inteligente de Compras

Demo funcional de validación comercial. Documentación técnica disponible en `docs/`.
Arquitectura: `docs/ARQUITECTURA.md`. Decisiones: `docs/decisions/`. Roadmap: `docs/ROADMAP.md`.

## Ejecutar el backend

```bash
cp .env.example .env              # y ajusta valores locales
scripts/dev.sh                    # crea .venv, instala y arranca en http://localhost:8000
curl localhost:8000/api/v1/health
```

Documentación interactiva: http://localhost:8000/docs

## Base de datos y demo

```bash
open -a Docker                                              # Docker Desktop encendido
cd database && docker compose --env-file ../.env up -d      # PostgreSQL 17 local
cd ../backend && .venv/bin/alembic upgrade head
.venv/bin/python -m app.cli seed-demo                       # Pao, Ana, Luis + ciclo piloto (imprime sus IDs)
scripts/dev.sh                                              # en otra terminal
backend/.venv/bin/python scripts/demo_h1a.py --reset        # demostración H1-A paso a paso
```

Endpoints (`/api/v1`, contrato en `docs/contracts/openapi.json`, interactivo en `/docs`). Identidad por
cabecera `X-Demo-User-Id` mientras `AUTH_MODE=demo_header`. En organizaciones reales (`real_test`/`production`)
las rutas administrativas —`/staff` y los accesos de Telegram— exigen `Authorization: Bearer <JWT de Supabase>`;
la cabecera de demostración solo se acepta en simulación (`docs/AUTH_SUPABASE.md`):

| Método y ruta | Uso |
|---|---|
| `GET /me` | Identidad activa |
| `GET /demand-items` · `GET /demand-items/{id}` | Lista compartida (cursor) · detalle con observaciones e historial |
| `POST /observations` | Aviso de faltante (`Idempotency-Key`); deduplica, nunca suma |
| `GET /observations` · `POST /observations/{id}/resolve` | Avisos dudosos y su resolución (compras) |
| `POST /demand-items/{id}/quantity-commands` | SET/ADD/CLEAR (`Idempotency-Key` + `If-Match`) |
| `POST /demand-items/{id}/resolve` | Confirmar unidad, forma, contenido… (`If-Match`; compras). Lo confirmado queda `confirmed` |
| `GET /product-aliases` · `POST /product-aliases/{id}/deactivate` | Memoria verificada («400 sin unidad = 400 mg» para un producto), revisable |

Cada necesidad devuelve `attributes` (potencias, contenido, forma, envase, medidas sin unidad, con procedencia
`explicit|inferred|confirmed` y la medida *como se escribió* junto a su valor normalizado) y `pending_validation`.

## Organizaciones y datos (importante)

Tres tipos de organización, aislados: **simulación** (demos y pruebas automatizadas; chat ficticio 5550001), **prueba real** (bot real; chat de prueba no publicado) y
un **archivo** con lo anterior a la separación. Las demos usan solo la de simulación. El personal sale de `database/seed/staff.json` (datos) y se administra por
`/api/v1/staff`. Plan de migración de los datos existentes (pendiente de autorización): `docs/decisions/0002-plan-migracion-organizaciones.md`.

## Telegram (H1-B)

Documentación completa, arquitectura, límites y cómo crear el bot: **`docs/H1B_TELEGRAM.md`**.

```bash
backend/.venv/bin/python scripts/demo_h1b.py          # demo SIN bot real (solo org. de simulación): Telegram y visión SIMULADOS, PostgreSQL real
cd backend && .venv/bin/python -m app.cli telegram-check   # valida el token (cuando lo configures)
.venv/bin/python -m app.cli bind-telegram <chat_id> --org real   # bootstrap manual (--org sim|real, sin valor por defecto)
.venv/bin/python -m app.cli list-telegram-accounts --org real --by <admin_id>
.venv/bin/python -m app.cli block-telegram-account <account_id> --org real --by <admin_id> --reason "..."
.venv/bin/python -m app.cli reactivate-telegram-account <account_id> --org real --by <admin_id>
.venv/bin/python -m app.cli telegram-poll                   # escucha sin exponer el backend
```

Varias cuentas pueden usar el mismo bot. Una cuenta nueva pide acceso con `/solicitar` en chat privado y recibe un código
temporal (30 min, un solo uso); se lo comparte a Pao, que lo busca y aprueba desde el frontend (API
`/api/v1/telegram-access-requests`), definiendo modo, persona y tipo de cuenta. Conocer el código no autoriza: aprobar,
bloquear y rehabilitar exigen sesión administrativa fuerte (JWT de Supabase) y conservan el historial. Un teléfono personal
reporta como su persona; el compartido sigue eligiendo auxiliar. Procedimiento completo: `docs/H1B_TELEGRAM.md` §7.

## Base de datos (detalle)

```bash
cd database && docker compose --env-file ../.env up -d     # requiere Docker Desktop encendido
cd ../backend && .venv/bin/alembic upgrade head
```

## Pruebas

```bash
cd backend
.venv/bin/pytest                    # usa TEST_DATABASE_URL del .env (base farmacia_test, se recrea sola)
REQUIRE_DB=1 .venv/bin/pytest       # falla (en vez de omitir) si no hay PostgreSQL: usar para validar
.venv/bin/ruff check .
```

## Estructura

`backend/` API FastAPI · `database/` PostgreSQL local · `docs/` especificación y decisiones ·
`scripts/` utilidades · `frontend/` (se crea al integrar Lovable) · `tests/` de negocio end-to-end (se crea con H1).
