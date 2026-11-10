# Arquitectura

Actualizado: 2026-11-09. Complementa el HLD/LLD original
(`docs/Farmacia_Parque_Lantano_HLD_LLD.md`, que **no se modifica**). Donde difieren, rigen este
documento, el [`README.md`](../README.md), el contrato del cotizador y las decisiones registradas en
`docs/decisions/`.

## 1. Vista general

```
Lovable / navegador
   │  HTTPS + JSON (solo /api/v1)
   ▼
FastAPI  ──SQL──►  PostgreSQL 17
   │                 ├─ negocio, auditoría y outbox en la misma transacción
   │                 └─ fotografías en temp_photos (BYTEA, SHA-256)
   ├── Supabase Auth: JWT validado contra JWKS (sin llamada por petición)
   ├── OpenAI: visión de fotos y observaciones comerciales (opcional)
   ├── Cadenas: HTTP público (Ahumada, Salcobrand, Cruz Verde)
   └── Telegram: polling (o webhook) → mismo dominio y servicios

GitHub master ──► Heroku (build, release Alembic, web, worker opcional)
GitHub main   ──► Lovable (editor, preview y publicación)
```

## 2. Stack

| Capa | Elección | Motivo |
|---|---|---|
| Backend | Python 3.14 (compatible 3.12+), FastAPI, SQLAlchemy 2 async, psycopg 3, Alembic, Pydantic v2 | OpenAPI, tipado, ecosistema IA/OCR |
| Base | PostgreSQL 17 como único almacén transaccional | Unicidad parcial, bloqueos, auditoría y outbox |
| Frontend | React 19, TanStack Start/Router, Vite, TypeScript, Tailwind, shadcn/ui | Lovable; sin lógica de negocio en el cliente |
| Autenticación | Supabase Auth + validación local de JWT (JWKS, ES256/RS256) | Estándar OIDC, sin SDK propietario en el backend |
| IA | OpenAI por HTTP (Responses API), sin SDK | Proveedor reemplazable; pruebas con dobles |
| Canal | Telegram Bot API por long polling | No exige exponer el backend a internet |
| Pruebas | pytest + PostgreSQL real; Vitest + Testing Library | Reglas verificadas contra la base real |

No hay Redis, colas externas, SSE ni WebSockets en producción. El estado en memoria se usa solo para
las búsquedas E15 y para el limitador de tasa.

## 3. Monolito modular

`backend/app/` se divide por responsabilidad:

| Paquete | Responsabilidad |
|---|---|
| `api/v1` | Rutas, esquemas de entrada/salida, transacción por petición |
| `services` | Reglas de negocio, permisos, auditoría + outbox, sin commit propio |
| `domain` | Productos, unidades, cantidades, identidad farmacéutica/comercial |
| `interpretation` | Texto, visión, interpretación de observaciones y adaptadores |
| `telegram` | Parseo, cliente de Bot API, runtime de polling |
| `models` | Modelos SQLAlchemy |
| `core` | Configuración, errores, permisos, autenticación, cifrado |
| `migrations` | Alembic; la cabeza actual es `0026` |

Patrón de servicios: reciben un `Actor` y una `AsyncSession`, verifican permiso, aplican reglas y
emiten auditoría/outbox con `services.events.emit`. La API abre `transaction()` y hace commit; si algo
falla, rollback completo. Los servicios no hacen commit para poder componer operaciones.

## 4. Aislamiento y datos

- Tres tipos de organización: `simulation`, `real_test` y `production` (además del archivo histórico
  `legacy_mixed`). Los datos ficticios viven solo en `simulation`.
- Toda consulta de negocio filtra por la organización del `Actor`; un identificador de otra
  organización responde `404`, igual que uno inexistente.
- La organización **nunca** viaja en el cuerpo de una petición administrativa.
- Dinero con `Decimal`/`NUMERIC`; fechas en UTC y presentación en `America/Santiago`.
- Las fotografías se guardan en `temp_photos` (`BYTEA`, PK SHA-256). El disco del dyno no se usa para
  fotos. La baja se decide por referencias vivas, no por antigüedad; una imagen compartida por SHA-256
  se conserva mientras otra organización la necesite.
- Las credenciales de portales de proveedor se cifran con Fernet (`SUPPLIER_CREDENTIALS_KEY`) y
  nunca se devuelven por la API ni se registran en logs. Guardar una credencial no prueba conexión.

## 5. Flujo de una petición autenticada

1. El navegador envía `Authorization: Bearer <JWT de Supabase>`.
2. `app/core/supabase_auth.py` valida firma contra JWKS (caché de 300 s), `iss`, `aud`, `exp`, `iat`,
   `sub` y `role=authenticated`.
3. `app/api/deps.py` busca `app_user.auth_subject == sub` **activo**; de allí salen organización y
   roles. Si no existe: `403 USER_NOT_PROVISIONED`; si el acceso venció:
   `403 WEB_ACCESS_EXPIRED`.
4. Se construye un `Actor`; `StrongActor` añade la exigencia de sesión fuerte para rutas
   administrativas. En `AUTH_MODE=demo_header` solo se acepta la organización de simulación.
5. El servicio valida permisos y reglas; la transacción escribe negocio + auditoría + outbox.

Los permisos viven en PostgreSQL, no en claims editables del token. Detalle:
[`AUTH_SUPABASE.md`](AUTH_SUPABASE.md).

## 6. Módulos funcionales

| Módulo | Estado | Dónde |
|---|---|---|
| Faltantes, observaciones, cantidades, cancelación | Implementado | `services/demand.py`, `api/v1/demand.py` |
| Personal, disponibilidad y baja de auxiliares | Implementado | `services/staff.py`, `api/v1/staff.py` |
| Telegram: sesiones, cuentas, solicitudes, aprobación | Implementado | `services/channel.py`, `services/sessions.py`, `api/v1/telegram_accounts.py` |
| Fotos y visión | Implementado | `services/photo_processing.py`, `services/storage.py`, `interpretation/openai_vision.py` |
| Catálogo, proveedores, fuentes e importación | Implementado | `services/catalog.py`, `services/suppliers.py`, `services/imports.py` |
| Búsqueda E1–E3 y cadenas E15 | Implementado | `api/v1/search.py`, `services/chain_search.py`, `services/chains/` |
| Comparación E5 y criterios E9 | Implementado | `services/comparison.py`, `api/v1/comparisons.py` |
| Cotizaciones, carritos y asignaciones E10 | Implementado en backend | `services/quotes.py`, `api/v1/quotes.py` |
| Observaciones comerciales e IA A6/O2 | Implementado en backend | `services/observation_ai.py`, `api/v1/observations.py` |
| Acceso web G1 | Implementado | `services/web_access.py`, `api/v1/web_access.py` |
| Compras registradas y cierre | **No implementado** | Reservado `0021` |
| SSE / dashboard en tiempo real | **No implementado** | H1-C pendiente |

## 7. Integraciones

### Telegram

`app/telegram/` traduce la Bot API a `ChannelEvent`; `services/channel.py` aplica idempotencia,
sesiones, autorización de cuenta, interpretación y respuestas. La entrega es por cola con reintentos.
El webhook existe pero no se expone; el modo operativo es polling. Ver
[`H1B_TELEGRAM.md`](H1B_TELEGRAM.md).

### OpenAI

Dos adaptadores independientes, ambos opcionales y apagados por defecto:

- **Visión** (`VISION_PROVIDER=openai`): lee la foto y devuelve una extracción estructurada.
- **Observaciones comerciales** (`OBSERVATION_AI_ENABLED=true` + `OPENAI_MODEL`): interpreta texto
  libre y propone preferencias; el motor determinista decide el orden.

Sin clave o modelo no se llama al proveedor. La suite usa un doble y no consume cuota. Ver
[`COTIZADOR.md`](COTIZADOR.md) §8 y [`A6_FASE_3B_API.md`](A6_FASE_3B_API.md).

### Cadenas

Cada cadena tiene su conector en `services/chains/`. `chain_search` los consulta en paralelo, con
presupuesto propio, y publica un estado por cadena (`ok`, `failed`, `not_configured`). Los resultados
viven en memoria (TTL 10 min, 50 búsquedas) y **no se persisten**. Una cadena caída o que rechaza la
consulta no se convierte en «sin resultados».

### Supabase

Solo autenticación: Google y correo+contraseña. El backend no usa el cliente Supabase ni accede a sus
tablas. La identidad es `auth_subject`; el correo es informativo.

## 8. Despliegue

- **Heroku**: buildpack Python desde la raíz; `requirements.txt`, `.python-version` y `Procfile`.
  `release` ejecuta `alembic upgrade head`; `web` sirve uvicorn en `$PORT`. El polling de Telegram
  corre como `worker` si se habilita (la línea debe estar versionada en la rama desplegada).
- **Lovable**: repositorio `farmacia-frontend` conectado a `main`; el editor sincroniza y la
  publicación es manual.
- **Migraciones**: solo desde `DATABASE_URL`; nunca desde `TEST_DATABASE_URL`. La base de Heroku es
  independiente de `farmacia`, `farmacia_demo` y `farmacia_test`.
- **Fotos**: sobreviven al redespliegue porque viven en PostgreSQL. La recuperación desde Telegram es
  una operación manual apagada por defecto.

Procedimiento: [`DEPLOY_HEROKU.md`](DEPLOY_HEROKU.md).

## 9. Reglas que no cambian

- `purchase_execution = blocked` en todos los entornos; no depende de una variable.
- El LLM no decide precios, cantidades, permisos ni identidades.
- Un aviso repetido no suma; la cantidad desconocida es `NULL`.
- Cotizar cubre de forma provisional y no cierra; solo una compra confirmada cerraría (cuando exista).
- Una referencia de cadena no es una oferta comprable.
- Nada simulado entra en una organización real.
- El frontend no accede a la base ni replica reglas de negocio.
