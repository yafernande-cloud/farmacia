# Despliegue en Heroku — preparación (sin ejecutar)

Estado: **preparación únicamente**. No existe app de Heroku, no se creó ningún servicio, no se
configuró ninguna credencial y no se desplegó nada. Cada paso de este documento requiere
**autorización explícita** antes de ejecutarse.

## Arquitectura objetivo

```
Lovable (frontend)  ──HTTPS──▶  FastAPI en Heroku  ──▶  Heroku Postgres
                                      │
                                      ├──▶ Supabase Auth (JWT + JWKS)
                                      ├──▶ OpenAI Responses API (Fase 2)
                                      └──▶ Telegram Bot API (T1 / H1-B.5)
```

El frontend solo consume `/api/v1`; no accede a PostgreSQL ni ejecuta reglas de negocio.

## Archivos ya preparados

| Archivo | Para qué |
|---|---|
| `Procfile` | `web`: uvicorn desde `backend/`; `release`: `alembic upgrade head` |
| `runtime.txt` | Versión de Python del buildpack (ver «Verificación pendiente») |
| `requirements.txt` | Dependencias directas fijadas a las versiones probadas en local |

`requirements.txt` vive en la raíz porque el buildpack de Python lee la raíz del repositorio, aunque
el código esté en `backend/`. **Al cambiar `backend/pyproject.toml` hay que actualizar
`requirements.txt` en el mismo cambio**; no hay generación automática todavía.

## Verificación pendiente antes del primer despliegue

1. **`runtime.txt`**: `python-3.13.4` es un candidato, no una versión confirmada del buildpack. Al
   crear la app se comprueba cuál acepta Heroku y se ajusta el archivo. El código exige
   `>=3.12`.
2. **Base de datos nueva**: el `release` ejecuta `alembic upgrade head`. Eso aplica `0009`–`0011`,
   que **nunca** se aplicaron a `farmacia`. La base de Heroku sería una base **nueva y separada**;
   `farmacia` no se toca.
3. **Almacenamiento de fotos**: `storage_dir` es local y el disco de Heroku es efímero. Las fotos se
   perderían al reiniciar el dyno. Persistirlas exige almacenamiento externo (S3 u otro), lo que
   requiere decisión y autorización; no se implementa en Fase 0.
4. **Migraciones de datos**: `seed-demo`, `seed-real-test` y `seed-catalog` son comandos de
   desarrollo. En Heroku no se ejecutan sin una decisión explícita.

## Variables de configuración (nombres; los valores no se escriben en el repositorio)

| Variable | Uso | Secreto |
|---|---|---|
| `APP_ENV` | `production` en Heroku | No |
| `AUTH_MODE` | `supabase_jwt` en Heroku (nunca `demo_header`) | No |
| `SUPABASE_JWKS_URL` | JWKS público del proyecto Supabase | No |
| `SUPABASE_JWT_ISSUER` | Emisor esperado del JWT | No |
| `SUPABASE_JWT_AUDIENCE` | Audiencia (por defecto `authenticated`) | No |
| `DATABASE_URL` | La inyecta el addon de Heroku Postgres | **Sí** |
| `CORS_ORIGINS` | Dominio del frontend Lovable, separado por comas | No |
| `TELEGRAM_BOT_TOKEN` | Token del bot; solo cuando se habilite el canal | **Sí** |
| `TELEGRAM_WEBHOOK_SECRET` | Secreto del webhook | **Sí** |
| `TELEGRAM_ACCESS_ORGANIZATION_ID` | Organización que recibe solicitudes del bot | No |
| `LOG_LEVEL` | `INFO` por defecto | No |

`DATABASE_URL` puede venir como `postgres://` (formato de Heroku): la aplicación lo normaliza a
`postgresql+psycopg://` al arrancar. Las credenciales **nunca** se pegan en este documento, en el
chat, en logs ni en Git.

## Pasos previstos (ninguno ejecutado)

1. Crear la app de Heroku y el addon de PostgreSQL — **requiere autorización y costo**.
2. Cargar las variables con `heroku config:set` (los secretos, desde el gestor de secretos del
   usuario, no desde el chat).
3. Desplegar `master` con el buildpack de Python — **requiere autorización**.
4. Verificar `GET /api/v1/health`, que `alembic current` coincida con el head y que una sesión de
   Supabase obtenga `200` en `/api/v1/me`.
5. Recién entonces, apuntar el frontend Lovable a la URL del backend.

## Reglas que no cambian

- `purchase_execution = blocked`: la compra real sigue bloqueada en todos los entornos.
- Sin `AUTH_MODE=demo_header` fuera de local/simulación.
- Ningún dato ficticio se presenta como real; el catálogo de `fixtures/` es solo para simulación.
- No se contrata ni se configura ningún servicio externo sin autorización puntual.
