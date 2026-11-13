# Ejecutar y probar en local

El ejemplo de Docker levanta la API y una base de datos desechable. Para trabajar con el frontend,
clona el [repositorio de Lovable](https://github.com/yafernande-cloud/screenshot-exact-match) junto
a este repositorio. En la carpeta del frontend ejecuta `npm install` y `npm run dev`. Configura
`VITE_API_URL`, `VITE_SUPABASE_URL` y `VITE_SUPABASE_ANON_KEY` para tu entorno.

## Backend en contenedor (Docker)

La imagen se construye con [`backend/Dockerfile`](../backend/Dockerfile), usa la versión de
`.python-version`, instala `requirements.txt` y arranca con el mismo comando que el proceso `web` del
`Procfile`. El contexto de compilación es la **raíz del repositorio** (allí viven `.python-version` y
`requirements.txt`); `.dockerignore` deja fuera credenciales, `.env`, entornos virtuales, respaldos,
pruebas y archivos locales. La configuración y los secretos se pasan **al ejecutar** el contenedor.

```bash
# 1. Construir (desde la raíz del repositorio)
docker build -f backend/Dockerfile -t cotizador-farmacia-backend .

# 2. Base desechable solo para la comprobación (nunca la base real ni la de Heroku)
docker network create cotizador-check
docker run -d --name cotizador-check-db --network cotizador-check \
  -e POSTGRES_USER=farmacia -e POSTGRES_PASSWORD=check_local \
  -e POSTGRES_DB=farmacia_check postgres:17
# Espera a que PostgreSQL acepte conexiones por TCP antes de migrar.
docker exec cotizador-check-db pg_isready -h 127.0.0.1 -U farmacia -d farmacia_check
# Si aún no acepta conexiones, vuelve a ejecutar la línea anterior.
# La imagen no migra sola: el `release` sigue siendo un paso explícito, como en Heroku.
docker run --rm --network cotizador-check \
  -e DATABASE_URL='postgresql+psycopg://farmacia:check_local@cotizador-check-db:5432/farmacia_check' \
  cotizador-farmacia-backend alembic upgrade head

# 3. Arrancar (el puerto del host es un ejemplo)
docker run -d --name cotizador-check-app --network cotizador-check \
  -p 127.0.0.1:18000:8000 \
  -e DATABASE_URL='postgresql+psycopg://farmacia:check_local@cotizador-check-db:5432/farmacia_check' \
  -e AUTH_MODE=demo_header \
  cotizador-farmacia-backend

# 4. Verificar
curl -sS http://127.0.0.1:18000/api/v1/health
# {"status":"ok","env":"local","purchase_execution":"blocked"}
curl -sS http://127.0.0.1:18000/api/v1/health/ready
# {"status":"ok","database":{"status":"ok"}}

# 5. Limpiar
docker rm -f cotizador-check-app cotizador-check-db
docker network rm cotizador-check
```

`/api/v1/health` es liveness (no toca la base) y `/api/v1/health/ready` es readiness (informa
`503` si PostgreSQL no responde). La imagen corre con un usuario sin privilegios (`uid 10001`) y no
contiene `.env` ni pruebas. En producción, con variables de entorno y secretos, se usa
`docker run --env-file` o el equivalente del servicio; **la publicación al dominio sigue siendo un
Manual Deploy en Heroku por la responsable del proyecto**, no se publica esta imagen en ningún
registro.


## Pruebas

```bash
# Backend
cd backend
.venv/bin/pytest                         # usa TEST_DATABASE_URL (farmacia_test)
REQUIRE_DB=1 .venv/bin/pytest            # falla si no hay PostgreSQL; usar para validar
.venv/bin/ruff check .

# Frontend
cd ../farmacia-frontend
npm test
npm run lint
npx tsc --noEmit
```

`TEST_DATABASE_URL` debe apuntar **solo** a una base de pruebas (`farmacia_test` o una base temporal
aislada). Nunca a `farmacia`, `farmacia_demo` ni a la base de Heroku. Detalle y evidencia:
[registro de pruebas](PRUEBAS.md).
