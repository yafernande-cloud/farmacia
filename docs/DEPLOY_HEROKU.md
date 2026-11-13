# Despliegue en Heroku y publicación en Lovable

El backend se despliega exclusivamente mediante **Manual Deploy** en el panel de Heroku,
a cargo de la responsable del proyecto. No se escriben valores secretos en este documento, en el repositorio, en el chat ni en los
logs.

## 1. Estado

- La app de Heroku `cotizador-farmacia-demo` existe, con GitHub `yafernande-cloud/farmacia`
  conectado, rama `master`, *Automatic Deploys* desactivado y addon **Heroku Postgres Essential 0**.
- El backend está desplegado y responde en
  `https://cotizador-farmacia-demo-3ea67af09b39.herokuapp.com`. La URL pública también está en el
  frontend como valor por defecto.
- El frontend `farmacia-frontend` está conectado a Lovable por la rama `main`; la vista previa y el
  acceso del profesor fueron verificados. La publicación al dominio final es manual.
- La cabeza de migraciones del repositorio es **0032**. Para la entrega se informó Heroku en
  `0032 (head)`. El despliegue manual posterior del backend en `f355732` terminó correctamente.
  La responsable confirmó la publicación del frontend `7c27bfb` en Lovable.
- Los resultados de las suites se registran en [Pruebas](PRUEBAS.md).

## 2. Cómo arranca

El buildpack de Python lee la **raíz** del repositorio, no `backend/`. Por eso los archivos de
despliegue viven en la raíz:

| Archivo | Función |
|---|---|
| `requirements.txt` | Dependencias de runtime fijadas; se actualiza a mano junto con `backend/pyproject.toml` |
| `.python-version` | `3.14`; Heroku instala el último parche disponible |
| `Procfile` | Procesos `web`, `release` y, opcionalmente, `worker` |

`Procfile` versionado:

```
web: cd backend && uvicorn app.main:app --host 0.0.0.0 --port $PORT
release: cd backend && alembic upgrade head
```

El árbol de trabajo añade una línea para el polling de Telegram:

```
worker: cd backend && PYTHONUNBUFFERED=1 python -m app.cli telegram-poll
```

Esa línea **no está versionada** al cierre de este documento. Si se quiere el polling como dyno
permanente, primero debe formar parte de la rama desplegada; mientras tanto, el polling se ejecuta en
una sesión de Run Console o en el entorno local autorizado. No se escala un proceso que el `Procfile`
desplegado no define.

## 3. Base de datos y conexiones

- `DATABASE_URL` la inyecta el addon. La app acepta `postgres://` y la normaliza a
  `postgresql+psycopg://`; conserva host, credenciales, base y parámetros.
- Heroku Postgres exige TLS. `psycopg` negocia SSL; para **exigirlo**, configurar
  `PGSSLMODE=require`.
- Essential 0 admite **20 conexiones**. El motor usa `pool_size=5` y `max_overflow=0`: máximo 5 por
  proceso. Con web, release y una consola hay margen; **no escalar a varios dynos sin recalcular**.
- El disco del dyno es efímero, pero las fotos **no** viven ahí: se guardan en PostgreSQL
  (`temp_photos`, `BYTEA`, clave SHA-256; migración `0024`). `STORAGE_DIR` y `LocalPrivateStorage`
  quedan solo para pruebas/demostración.
- No hay TTL de fotos: se liberan cuando ningún reporte u operación viva las necesita; una imagen
  compartida por SHA-256 se conserva mientras otra organización la use.

## 4. Variables de entorno

Los valores se cargan desde el gestor de secretos de la propietaria. Las variables marcadas **Sí**
son secretas y nunca se pegan en el chat ni en git.

### Indispensables

| Variable | Uso | Secreto |
|---|---|---|
| `APP_ENV` | `production` | No |
| `AUTH_MODE` | `supabase_jwt` (nunca `demo_header` en producción) | No |
| `SUPABASE_JWKS_URL` | JWKS público del proyecto Supabase | No |
| `SUPABASE_JWT_ISSUER` | Emisor esperado, sin barra final | No |
| `SUPABASE_JWT_AUDIENCE` | `authenticated` (por defecto) | No |
| `DATABASE_URL` | La inyecta Heroku Postgres | **Sí** |
| `CORS_ORIGINS` | Orígenes exactos del frontend, separados por coma; sin `*` ni barra final | No |

Sin JWKS e issuer, la app **no arranca** con `AUTH_MODE=supabase_jwt`. Con `AUTH_MODE=disabled`
arranca, pero las rutas de negocio responden `503`.

### Según alcance

| Variable | Uso | Secreto |
|---|---|---|
| `PGSSLMODE` | `require` para exigir TLS | No |
| `WEB_ACCESS_ORGANIZATION_ID` | Organización que recibe solicitudes de acceso web; sin ella, `503` | No |
| `TELEGRAM_ACCESS_ORGANIZATION_ID` | Organización que recibe solicitudes del bot | No |
| `TELEGRAM_BOT_TOKEN` | Canal de Telegram | **Sí** |
| `TELEGRAM_WEBHOOK_SECRET` | Solo si se usa webhook; 16–256 caracteres `[A-Za-z0-9_-]` | **Sí** |
| `VISION_PROVIDER` | `none` (por defecto), `simulated` o `openai` | No |
| `OPENAI_API_KEY` | Visión y/u observaciones | **Sí** |
| `OPENAI_VISION_MODEL` | Modelo de visión; sin valor no hay llamada de visión | No |
| `OPENAI_MODEL` | Modelo de observaciones; sin valor no hay interpretación | No |
| `OBSERVATION_AI_ENABLED` | `false` por defecto | No |
| `OPENAI_REASONING_EFFORT` | `low` por defecto | No |
| `OPENAI_MAX_OUTPUT_TOKENS` | 2500 por defecto | No |
| `SUPPLIER_CREDENTIALS_KEY` | Clave Fernet para cifrar contraseñas de portales | **Sí** |
| `PHOTO_RECOVERY_ENABLED` | `false` por defecto; habilita recuperar fotos desde Telegram | No |
| `CHAIN_SEARCH_ENABLED` | Lista de cadenas, p. ej. `cruz_verde,salcobrand,ahumada` | No |
| `CRUZVERDE_LIVE_SEARCH_ENABLED` | Interruptor propio de Cruz Verde | No |
| `SALCOBRAND_LIVE_SEARCH_ENABLED` | Interruptor propio de Salcobrand | No |
| `AHUMADA_LIVE_SEARCH_ENABLED` | Interruptor propio de Farmacias Ahumada | No |
| `LOG_LEVEL` | `INFO` por defecto | No |

Cada cadena tiene además sus URLs, credenciales de API (Salcobrand) y tiempos de espera. Los valores
reales no se documentan aquí. Una cadena sin interruptor o sin configuración informa
`not_configured` con su motivo; **no se inventan fichas ni precios**.

La interpretación de observaciones y la visión son independientes: se puede tener una apagada y la
otra encendida. Sin clave o modelo, el endpoint de interpretación responde
`503 OBSERVATION_AI_DISABLED` y la foto queda `pending`.

### Claves y gasto de OpenAI

En Heroku, las claves privadas se guardan en **Settings → Config Vars**. Esta guía enumera los
nombres de las variables. Los valores se administran en Heroku.

El proyecto de la API de OpenAI quedó configurado con un **límite mensual de USD 5,00** y el
corte de gasto activo (**Limit enforced**), confirmado en el panel el 13 de noviembre de 2026.
El límite se aplica a las solicitudes del proyecto al que pertenece `OPENAI_API_KEY`.
Este monto es el límite operativo mensual. La comparación de prompts tiene su propio presupuesto.
[Guía de límites](https://help.openai.com/en/articles/9186755-managing-projects-in-the-api-platform).

## 5. Migraciones

`alembic.ini` y `migrations/` viven en `backend/`. La URL sale **solo** de `DATABASE_URL`; no hay
respaldo a `TEST_DATABASE_URL` ni a una URL local.

```bash
# La fase release ejecuta esto en cada despliegue, antes de liberar los dynos:
cd backend && alembic upgrade head
```

- La cabeza del repositorio es **0032**. `0024` baja de `0020` a propósito porque `0021`–`0023` están
  reservadas para compras e historial; `0025` y `0026` continúan la cadena.
- Autorizar un despliegue implica autorizar las migraciones pendientes sobre la base de Heroku.
- Para separar despliegue y migración, quitar `release:` del `Procfile` (requiere commit) o ejecutar
  primero en Run Console:
  ```bash
  cd backend && alembic current
  cd backend && alembic upgrade head
  ```
- **Nunca** ejecutar migraciones contra `farmacia`, `farmacia_demo` o `farmacia_test` desde Heroku.
- Antes de una migración destructiva: respaldo verificado. Un rollback de código **no revierte** el
  esquema.

## 6. Despliegue manual del backend (panel de Heroku)

1. **Requisitos previos:** rama `master` en el estado deseado, pruebas verdes, migraciones revisadas
   y variables cargadas. No desplegar desde una rama con cambios sin commit.
2. **Settings → Config Vars:** confirmar las indispensables de §4. Los secretos se pegan desde el
   gestor, nunca desde el chat.
3. **Settings → Buildpacks:** `heroku/python` (detección por `requirements.txt`). No agregar otros.
4. **Deploy → Deployment method:** GitHub conectado, rama `master`, *Automatic Deploys* desactivado.
5. **Deploy → Manual deploy:** pulsar **Deploy Branch** solo con autorización.
6. **Activity / View logs:** comprobar que el build instala dependencias, que el release ejecuta
   Alembic sin error y que el dyno web arranca.
7. **Verificar salud:**
   - `GET /api/v1/health` → `200`, `purchase_execution: "blocked"`.
   - `GET /api/v1/health/ready` → `200` y base `ok`; `503` si PostgreSQL no responde.
   - `/docs` carga el OpenAPI.
8. **Run Console** (More → Run console), solo para operaciones autorizadas:
   ```bash
   cd backend && alembic current
   cd backend && python -m app.cli telegram-check
   cd backend && python -m app.cli list-telegram-accounts --org real --by <ADMIN_ID>
   ```
9. **Proceso worker (opcional):** si el `Procfile` desplegado ya incluye `worker`, activarlo en
   **Resources** y comprobar los logs. Si no está versionado, no aparece en la formación de dynos.
10. **Frontend:** confirmar que `CORS_ORIGINS` contiene el origen exacto de Lovable; luego publicar
    desde Lovable (§8).

### Seeds y datos

Solo en la organización de simulación, nunca en `real_test` ni en producción:

```bash
cd backend && python -m app.cli seed-demo
cd backend && python -m app.cli seed-catalog --org sim --by <ID_DE_PAO> --dry-run
cd backend && python -m app.cli seed-catalog --org sim --by <ID_DE_PAO>
```

`create-admin` es solo bootstrap de `simulation`/`real_test`; no crea cuentas de Supabase ni enlaza
identidad. No ejecutar `seed-real-test` sin decisión explícita.

## 7. Verificación de fotografías tras un redespliegue

La persistencia se comprobó en producción con la fotografía del caso Predual: siguió visible después
de un nuevo despliegue. Para repetir la verificación:

1. Confirmar que la base está en `0024` o superior: `cd backend && alembic current`.
2. Enviar una foto por Telegram o abrir un reporte con foto; comprobar la miniatura en Faltantes.
3. Anotar el `attachment_id` (la API lo expone como `photo_url`:
   `/api/v1/attachments/{id}/content`).
4. Redesplegar o reiniciar el dyno web.
5. Volver a abrir Faltantes: la misma foto debe cargar desde PostgreSQL. El binario **no** depende
   del disco del dyno.
6. Si una foto antigua no aparece, verificar `temp_photos` y, solo con `PHOTO_RECOVERY_ENABLED=true`,
   ejecutar `POST /api/v1/photos/maintenance/recover` con `dry_run` primero. La recuperación descarga
   desde Telegram únicamente fotos perdidas de reportes activos y verifica el SHA-256.

No se borran fotos por antigüedad. El barrido (`POST /photos/maintenance/collect`) solo libera
binarios sin referencias vivas y se limita a la organización del actor.

## 8. Publicación en Lovable

1. El repositorio `farmacia-frontend` está conectado a Lovable por `main`. Cada push se sincroniza
   con el editor; no se reescribe historia publicada (nada de force push, rebase o amend de commits ya
   subidos).
2. Antes de publicar: `npm test`, `npm run lint` y `npx tsc --noEmit` en el repositorio del frontend.
3. Abrir la **Preview** de Lovable y probar:
   - ingreso con correo+contraseña y con Google;
   - carga de Faltantes desde el backend;
   - búsqueda de catálogo y de cadenas;
   - pantalla de Telegram y proveedores.
4. Verificar que el backend desplegado tenga en `CORS_ORIGINS` el origen exacto de la preview y del
   dominio publicado. Sin eso, el navegador bloquea las llamadas aunque la API esté sana.
5. Pulsar **Publish** / **Share** en Lovable para publicar el dominio final. Es una acción manual y
   requiere autorización.
6. Volver a probar el dominio publicado; la preview y el dominio final pueden tener orígenes
   distintos.

El frontend solo usa variables públicas (`VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`,
`VITE_API_URL`). La clave `anon` es pública, pero se mantiene fuera del repositorio. Nunca se pone una
clave `service_role`, un token de Telegram ni una clave de OpenAI en el frontend.

## 9. Rollback

- **Código:** Heroku → Activity → Rollback a un release anterior. Requiere que el esquema siga siendo
  compatible con ese código.
- **Esquema:** no se revierte automáticamente. Un `alembic downgrade` exige autorización, respaldo
  verificado y revisión de la migración; puede perder datos.
- **Frontend:** Lovable conserva el historial; volver a un estado anterior implica un cambio de
  código y un nuevo push, no un force push.
- **Fotos:** permanecen en PostgreSQL aunque se revierta el código; no dependen del release.

## 10. Problemas frecuentes

| Síntoma | Causa probable |
|---|---|
| La app no arranca | Faltan `SUPABASE_JWKS_URL` o `SUPABASE_JWT_ISSUER` con `AUTH_MODE=supabase_jwt` |
| El navegador bloquea las llamadas | `CORS_ORIGINS` no contiene el origen exacto (esquema, dominio, sin barra final) |
| `release` falla | `DATABASE_URL` ausente, migración inválida o base inaccesible; el dyno no se libera |
| `503` en `/health/ready` | PostgreSQL no responde o el pool está agotado |
| `403 USER_NOT_PROVISIONED` | JWT válido sin `app_user.auth_subject` enlazado |
| `403 WEB_ACCESS_EXPIRED` | Acceso vencido o revocado |
| Fotos no cargan tras redespliegue | Verificar `0024`, `temp_photos` y la ruta autenticada de la miniatura |
| Una cadena no responde | Revisar su interruptor y configuración; `failed`/`not_configured` no es «sin resultados» |
| El worker no existe | La línea `worker` no está en el `Procfile` desplegado |

## 11. Reglas que no cambian

- `purchase_execution = blocked` en todos los entornos.
- `AUTH_MODE=demo_header` solo en local/simulación; nunca en producción.
- Ningún dato ficticio entra en una organización real.
- Ningún servicio externo se contrata o configura sin autorización puntual.
- Ningún secreto se escribe en el repositorio, en el chat o en un log.
- No se despliega ni se publica sin autorización explícita.
