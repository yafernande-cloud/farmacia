# Despliegue del backend en Heroku — preparación

Última revisión: 2026-11-03. Estado: **preparado para el primer despliegue manual; no se ha
desplegado nada**. La app `cotizador-farmacia-demo` ya existe, tiene el repositorio GitHub
`yafernande-cloud/farmacia` conectado, la rama `master` seleccionada, *Automatic Deploys*
desactivado y un addon **Heroku Postgres Essential 0**. No se ejecutaron migraciones en Heroku ni
se cargaron datos. Cada paso de este documento requiere autorización explícita.

## 1. Cómo se instala y arranca

El buildpack de Python de Heroku lee la **raíz del repositorio**, no `backend/`. Por eso la
instalación y el arranque se configuran en la raíz, apuntando al código de `backend/`:

- `requirements.txt` (raíz): dependencias fijadas. El buildpack lo detecta y ejecuta `pip install`.
- `.python-version` (raíz): `3.14`. Heroku solo mantiene el último parche de cada versión mayor;
  se indica major.minor para que resuelva el parche disponible. Se eliminó `runtime.txt`, que
  fijaba `python-3.13.4` (un parche que Heroku puede no ofrecer).
- `Procfile` (raíz):
  - `web: cd backend && uvicorn app.main:app --host 0.0.0.0 --port $PORT`
  - `release: cd backend && alembic upgrade head`
- El proceso escucha en `0.0.0.0` y en el puerto dinámico `$PORT`, como exige Heroku.
- `backend/` no se instala como paquete: uvicorn se ejecuta desde ese directorio (`app.main:app`).

Verificado localmente con `APP_ENV=production` y `AUTH_MODE=supabase_jwt`: la app arranca, registra
69 rutas y `GET /api/v1/health` responde `200`.

Compatibilidad de Python 3.14: Heroku lo soporta (buildpack actual) y todas las dependencias
fijadas lo admiten. Los paquetes con extensión nativa publican wheel para Linux x86_64 compatible
con 3.14: `psycopg-binary` (cp314), `greenlet` (cp314), `pydantic-core` (cp314),
`sqlalchemy` (cp314), `uvloop` (cp314), `httptools` (cp314) y `websockets` (cp314);
`watchfiles` y `cryptography` usan wheels `abi3` (compatibles hacia adelante) y el resto son
paquetes puros. No se requiere compilador en el build.

## 2. Base de datos

`DATABASE_URL` la inyecta el addon. La app acepta el formato `postgres://` de Heroku y lo normaliza
a `postgresql+psycopg://` al arrancar (`backend/app/core/config.py`); conserva host, credenciales,
base y parámetros de consulta. Alembic usa la misma URL.

- TLS: Heroku Postgres exige SSL. `psycopg` negocia TLS con `sslmode=prefer` (intenta SSL primero),
  así que la conexión funciona sin tocar la URL. Para **exigir** TLS y evitar una caída a texto
  plano, configurar `PGSSLMODE=require` como variable de entorno (psycopg lee las variables de
  libpq). Si se prefiere, añadir `?sslmode=require` a la URL.
- Essential 0 admite **20 conexiones**. El motor usa `pool_size=5` + `max_overflow=0`: **máximo 5
  por proceso**, más el release y como mucho un dyno de consola, queda margen amplio. **No**
  escalar a varios dynos sin volver a calcularlo.
- El disco del dyno es efímero: `STORAGE_DIR` (fotos) no persiste entre reinicios. No bloquea el
  primer despliegue, pero las fotografías se perderían.

## 3. Archivos preparados

| Archivo | Para qué |
|---|---|
| `Procfile` | Arranque web y fase `release` con Alembic |
| `requirements.txt` | Dependencias de runtime fijadas; se añadió `httpx==0.28.1`, que el adaptador de OpenAI importa al arrancar |
| `.python-version` | `3.14` para el buildpack |
| `backend/pyproject.toml` | Dependencias de desarrollo y pruebas (no lo usa Heroku) |

`requirements.txt` se mantiene a mano: al cambiar `backend/pyproject.toml` hay que actualizarlo en
el mismo cambio.

## 4. Variables de entorno

Nunca se escriben valores secretos en el repositorio, en el chat ni en logs. Los valores se cargan
desde el gestor de secretos de la propietaria.

### Indispensables para el primer despliegue

| Variable | Valor / uso | Secreto |
|---|---|---|
| `APP_ENV` | `production` | No |
| `AUTH_MODE` | `supabase_jwt` (nunca `demo_header` en producción) | No |
| `SUPABASE_JWKS_URL` | JWKS público del proyecto Supabase | No |
| `SUPABASE_JWT_ISSUER` | Emisor esperado del JWT | No |
| `SUPABASE_JWT_AUDIENCE` | `authenticated` (valor por defecto) | No |
| `DATABASE_URL` | La inyecta Heroku Postgres | **Sí** |
| `CORS_ORIGINS` | Dominio del frontend Lovable, separado por comas | No |

Sin `SUPABASE_JWKS_URL` y `SUPABASE_JWT_ISSUER`, la app **no arranca** con
`AUTH_MODE=supabase_jwt` (validación deliberada). Con `AUTH_MODE=disabled` arranca, pero las rutas
de negocio responden `503`; sirve para probar salud, no para usar el producto.

Formatos esperados (sustituir los marcadores; no son secretos, salvo donde se indica):

| Variable | Formato |
|---|---|
| `SUPABASE_JWKS_URL` | `https://<PROJECT_REF>.supabase.co/auth/v1/.well-known/jwks.json` |
| `SUPABASE_JWT_ISSUER` | `https://<PROJECT_REF>.supabase.co/auth/v1` (sin barra final) |
| `SUPABASE_JWT_AUDIENCE` | `authenticated` |
| `CORS_ORIGINS` | `https://<proyecto>.lovable.app` o el dominio propio; varios separados por coma: `https://a.lovable.app,https://dominio.cl`. Sin `*`, sin barra final, con esquema `https://` |

`CORS_ORIGINS` debe contener los orígenes exactos que abrirán el frontend; el backend los separa
por comas y elimina espacios. No admite rutas ni comodines.

### Recomendadas / según alcance

| Variable | Uso | Secreto |
|---|---|---|
| `PGSSLMODE` | `require` para exigir TLS hacia Heroku Postgres | No |
| `WEB_ACCESS_ORGANIZATION_ID` | Organización que recibe solicitudes de acceso web; sin ella el endpoint público responde `503` | No |
| `LOG_LEVEL` | `INFO` por defecto | No |
| `TELEGRAM_BOT_TOKEN` | Solo si se habilita el canal de Telegram | **Sí** |
| `TELEGRAM_WEBHOOK_SECRET` | Solo con Telegram; 16–256 caracteres `[A-Za-z0-9_-]` | **Sí** |
| `TELEGRAM_ACCESS_ORGANIZATION_ID` | Organización que recibe solicitudes del bot | No |
| `OBSERVATION_AI_ENABLED` | `false` en el primer despliegue; se deja apagado | No |
| `OPENAI_API_KEY` | Solo al habilitar la interpretación | **Sí** |
| `OPENAI_MODEL` | Sin valor no hay llamada; no se inventa un modelo por defecto | No |

Con `OBSERVATION_AI_ENABLED=false` no se llama a OpenAI. La observación se guarda y la
interpretación queda pendiente.

## 5. Migraciones

`alembic.ini` y `migrations/` viven en `backend/`; la fase `release` del `Procfile` ejecuta:

```bash
cd backend && alembic upgrade head
```

La URL de migración sale **solo** de `DATABASE_URL`: `alembic.ini` no define `sqlalchemy.url` y
`migrations/env.py` usa `get_settings().database_url`; no hay respaldo a `TEST_DATABASE_URL` ni a
una URL local. En el slug de Heroku no existe `.env`, así que la única fuente es la variable del
addon. No se ejecutó ninguna migración.

Heroku la corre **automáticamente en cada despliegue** (incluido el primero), antes de liberar los
dynos. Por tanto, autorizar el primer despliegue implica autorizar la aplicación de `0001`–`0019`
sobre la base nueva de Heroku. Si se prefiere separar despliegue y migraciones, hay que quitar la
línea `release:` del `Procfile` antes de desplegar y ejecutar después:

```bash
heroku run --app cotizador-farmacia-demo bash -c 'cd backend && alembic upgrade head'
heroku run --app cotizador-farmacia-demo bash -c 'cd backend && alembic current'
```

`farmacia`, `farmacia_demo` y `farmacia_test` no se tocan: la base de Heroku es nueva y separada.
Todavía no se ejecutó ninguna migración en Heroku.

## 6. Datos de demostración

Solo en la organización de **simulación** creada por `seed-demo`; nunca en `real_test` ni en
producción. Los comandos son idempotentes y no imprimen tokens.

```bash
# 1) Organización de simulación, personal de database/seed/staff.json y ciclo abierto.
#    Imprime los IDs (Pao es admin). Guardar el ID de Pao.
heroku run --app cotizador-farmacia-demo bash -c 'cd backend && python -m app.cli seed-demo'

# 2) Catálogo FICTICIO (30 artículos, 3 proveedores, 3 fuentes, 90 ofertas).
#    Primero en seco; no escribe nada.
heroku run --app cotizador-farmacia-demo bash -c \
  'cd backend && python -m app.cli seed-catalog --org sim --by <ID_DE_PAO> --dry-run'

# 3) Carga real del fixture, solo si la vista previa es correcta.
heroku run --app cotizador-farmacia-demo bash -c \
  'cd backend && python -m app.cli seed-catalog --org sim --by <ID_DE_PAO>'
```

Para que Pao entre desde Lovable con Supabase, enlazar su `sub` (se obtiene al iniciar sesión; no
es una credencial, pero no se pega en el chat):

```bash
heroku run --app cotizador-farmacia-demo bash -c \
  'cd backend && python -m app.cli link-auth-subject <ID_DE_PAO> --org sim --by <ID_DE_PAO> --subject <SUB_DE_SUPABASE>'
```

`create-admin` es solo para bootstrap de una administradora en `real_test`/`simulation`; no crea
cuentas de Supabase ni enlaza identidad. No ejecutar `seed-real-test` sin una decisión explícita.

## 7. Verificación de salud

| Ruta | Uso | Qué expone |
|---|---|---|
| `GET /api/v1/health` | Liveness; no toca la base | `status`, `env`, `purchase_execution` (`blocked`) |
| `GET /api/v1/health/ready` | Readiness; consulta `SELECT 1` | `status` y estado de la base; `503` si no responde |

Ninguno devuelve credenciales, la URL u host de la base, rutas internas, versión del modelo ni
trazas. `/health` solo confirma que el proceso vive y el entorno lógico (`production`); `/health/ready`
resume `ok`/`unavailable` de PostgreSQL y responde `503` cuando no está disponible. Los detalles de
una falla de conexión se registran solo en el log del servidor. `purchase_execution` siempre es
`blocked`: la compra real no se habilita por configuración.

## 8. Bloqueos y riesgos antes del primer despliegue

1. **Variables de Supabase**: sin JWKS e issuer, `AUTH_MODE=supabase_jwt` impide arrancar.
2. **CORS**: sin el dominio de Lovable en `CORS_ORIGINS`, el navegador bloqueará las llamadas.
3. **Migraciones**: el primer despliegue las aplica por `release`; decidir si se acepta o se pasa a
   ejecución manual (ver §5).
4. **Datos**: la base nueva está vacía; hasta correr los seeds no hay catálogo ni usuarios.
5. **Fotos**: almacenamiento local efímero; persistirlas requiere S3 u otro servicio (decisión
   aparte).
6. **OpenAI**: apagado; no hay costo ni llamadas. Habilitarlo exige clave, modelo y presupuesto.
7. **Conexiones**: Essential 0 permite 20; no escalar dynos sin ajustar el pool.
8. **Python**: Heroku instalará el último parche de 3.14, no exactamente el 3.14.6 local. Las
   dependencias fijadas se probaron en 3.14.6.

## 9. Pasos exactos en el panel de Heroku

1. **Settings → Config Vars**: cargar las variables de §4 (indispensables primero). Los secretos se
   pegan desde el gestor de secretos, no desde el chat.
2. **Settings → Buildpacks**: confirmar que está `heroku/python` (detección automática por
   `requirements.txt`). No se agregan buildpacks adicionales.
3. **Deploy → Deployment method**: GitHub ya conectado a `yafernande-cloud/farmacia`; rama
   `master`; *Automatic Deploys* desactivado.
4. **Deploy → Manual deploy**: pulsar *Deploy Branch* cuando se autorice. El build instala
   `requirements.txt` con Python 3.14 y el release ejecuta Alembic (ver §5).
5. **Activity / View logs**: comprobar que el release termina y el dyno web arranca sin errores.
6. Abrir `https://cotizador-farmacia-demo.herokuapp.com/api/v1/health` y
   `/api/v1/health/ready`.
7. **More → Run console**: ejecutar los seeds de §6 (equivale a `heroku run`).
8. Recién entonces, apuntar Lovable a la URL del backend y actualizar `CORS_ORIGINS` si cambia el
   dominio.

## 10. Reglas que no cambian

- `purchase_execution = blocked` en todos los entornos.
- `AUTH_MODE=demo_header` solo en local/simulación; nunca en producción.
- Ningún dato ficticio entra en una organización real; el catálogo de `fixtures/` es solo de
  simulación.
- No se contrata ni configura ningún servicio externo sin autorización puntual.
