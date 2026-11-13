# Faltantes Farmacia — Cotizador Inteligente

Asistente de compras para Farmacia Parque Lantaño. El personal informa faltantes por Telegram o desde
la web; compras busca el producto en el catálogo de proveedores y en las grandes cadenas, compara
ofertas y prepara una cotización. La compra real permanece **bloqueada por diseño**
(`purchase_execution = blocked`).

El proyecto partió de instrucciones para los agentes de desarrollo. El [Prompt Maestro](PROMPT_MAESTRO.md)
reúne las reglas de negocio, las fases de implementación y los criterios de aceptación. La documentación
de [prompts de la aplicación](prompts/README.md) explica la evolución de Visión y Observaciones
Comerciales, el golden set y el alcance de la evaluación.

Documentación de referencia (solo en este repositorio):

- Arquitectura y stack: [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md)
- Faltantes, búsquedas, cadenas, carrito, cotizaciones, compras e IA: [`docs/COTIZADOR.md`](docs/COTIZADOR.md)
- Motor declarativo de proveedores: perfiles, estrategias y búsqueda en vivo: [`docs/MOTOR_PROVEEDORES.md`](docs/MOTOR_PROVEEDORES.md)
- Autenticación, usuarios y permisos: [`docs/AUTH_SUPABASE.md`](docs/AUTH_SUPABASE.md)
- Telegram, sesiones de auxiliar y fotografías: [`docs/H1B_TELEGRAM.md`](docs/H1B_TELEGRAM.md)
- Despliegue manual en Heroku y publicación en Lovable: [`docs/DEPLOY_HEROKU.md`](docs/DEPLOY_HEROKU.md)
- Pruebas y criterios de evidencia: [`docs/PRUEBAS.md`](docs/PRUEBAS.md)
- Roadmap y pendientes: [`docs/ROADMAP.md`](docs/ROADMAP.md)
- Decisiones y acuerdos: [`docs/decisions/`](docs/decisions/)
- Contrato funcional del cotizador: [`docs/contracts/cotizador-v0.2.md`](docs/contracts/cotizador-v0.2.md)

> **Alcance de esta documentación.** Describe el sistema implementado a 2026-11-09 y distingue
> explícitamente **implementado en código**, **probado localmente**, **verificado en producción** y
> **pendiente**. No contiene contraseñas, tokens, claves ni credenciales. El HLD/LLD original
> (`docs/Farmacia_Parque_Lantano_HLD_LLD.md`) se conserva sin cambios; donde difiere, rigen este
> README, `docs/ARQUITECTURA.md` y las decisiones registradas.

## 1. Problema y propuesta

En una farmacia pequeña el faltante se detecta en el mesón y se pierde entre mensajes, listas y
planillas. El costo de compra depende de comparar presentaciones, precios, despacho y disponibilidad
entre proveedores y cadenas, tarea que suele hacerse a mano y sin trazabilidad.

Este sistema propone un flujo único:

1. **Reportar** el faltante (Telegram, con texto o foto, o la API web).
2. **Buscar** el producto en el catálogo verificado de proveedores y en las cadenas.
3. **Comparar** ofertas con reglas deterministas (precio, despacho, elegibilidad, preferencias).
4. **Cotizar** por proveedor, con asignaciones explícitas a cada reporte.
5. **Comprar** solo cuando exista una decisión humana y la compra esté habilitada; en esta etapa la
   ejecución real está bloqueada.

El principio de diseño es que **el LLM interpreta, pero no decide** dinero, cantidades, permisos ni
identidades. Toda regla de negocio vive en el backend.

## 2. Estado actual (2026-11-09)

| Área | Código | Pruebas locales | Producción |
|---|---|---|---|
| Faltantes, observaciones, cantidades y cancelación | Implementado | Suite backend con PostgreSQL | Dashboard real en Lovable; acceso del profesor verificado |
| Telegram: bot, cuentas, sesiones, solicitudes y aprobación | Implementado | PostgreSQL real + simulador del canal | Bot real configurado con polling; la suite no llama a Telegram |
| Fotos: almacenamiento en PostgreSQL y miniatura autenticada | Implementado | Pruebas dedicadas | Persistencia comprobada tras un redespliegue, incluida la foto de Predual |
| Visión de fotos con OpenAI | Implementado como adaptador | Dobles de red; sin llamadas facturables | Depende de `VISION_PROVIDER`/`OPENAI_VISION_MODEL`; la validación general del reconocimiento sigue pendiente |
| Búsqueda de catálogo (E1–E3) | Implementado | Suite backend y frontend | En uso desde Lovable |
| Búsqueda de cadenas (E15) | Implementado | Suite backend y frontend | Según configuración por cadena; límite de Cruz Verde verificado manualmente y caso Predual verificado tras el despliegue con resultados de Cruz Verde, Salcobrand y Ahumada |
| Motor de proveedores: perfiles y búsqueda en vivo | Implementado en backend; estrategia Algolia y preset Farmaloop, con clasificación directo/relacionado y bloqueo comercial | 47 pruebas nuevas con transporte simulado y PostgreSQL | Apagado por defecto (`SUPPLIER_CONNECTOR_ENABLED=false`); `0027` aplicada solo en una base desechable; una búsqueda real «predual» validada el 2026-11-10 (5 ofertas válidas, sin persistencia); sin conectar a Lovable |
| Comparación, cotizaciones y carrito (E5/E10) | Implementado en backend | Suite backend | Carrito, finalización e historial de informes confirmados en el sitio publicado por la responsable del proyecto |
| Compras registradas y cierre de faltantes | **No implementado** | — | — |
| Observaciones comerciales e IA (A6/O2) | Implementado en backend | Suite con proveedor simulado | «Enseñar a la IA» confirmado en el sitio publicado por la responsable del proyecto |
| Acceso web con Google y correo+contraseña | Implementado | Suite backend y frontend | Inicio de sesión con ambos métodos verificado en Lovable Preview |
| Solicitudes de acceso web y campanita | Backend implementado; frontend solo demostración | Suite backend | **No verificado de punta a punta**: la campanita de Lovable no llama a la API |

La última suite backend informada con `REQUIRE_DB=1` queda en **1044 pruebas aprobadas y 8 omitidas**;
la última suite frontend informada, en **238 pruebas aprobadas**. Los números y comandos vigentes están en
[`docs/PRUEBAS.md`](docs/PRUEBAS.md).

## 3. Funcionalidades implementadas

### Faltantes y reportes

- Grilla compartida de faltantes con cursor, detalle, observaciones e historial.
- Un aviso repetido **no suma** cantidad; la cantidad desconocida es `NULL`, nunca 0 ni 1.
- Cantidades solo por comandos explícitos `SET`/`ADD`/`CLEAR`, con `Idempotency-Key` e `If-Match`.
- Reportes independientes por autor, cancelación lógica y asignaciones explícitas
  (`quote_line_allocation`) sin reparto automático.
- Memoria verificada de productos (alias) revisable y desactivable.
- Cada reporte nuevo guarda una instantánea del nombre del autor; los históricos no se reescriben.

### Telegram

- Un bot, varias cuentas autorizadas; teléfono compartido o personal.
- Solicitud con `/solicitar`, código temporal de un solo uso y aprobación desde la pantalla real de
  Telegram en Lovable (`/telegram`).
- Selección de auxiliar por cuenta, sucursal en la cuenta y una sola sesión activa por chat.
- Baja terminal de un auxiliar: conserva autoría e historial, revoca sus sesiones de Telegram y
  **no toca** su acceso web ni sus roles.

### Fotografías y visión

- En Telegram se envía una foto por producto. La lectura queda disponible para revisión.
- El binario vive en PostgreSQL (`temp_photos`, `BYTEA`, clave SHA-256), no en el disco efímero del
  dyno. Un redespliegue no borra las fotos.
- Validación por firma de bytes y tamaño; deduplicación por contenido entre organizaciones.
- Lectura con OpenAI opcional (`VISION_PROVIDER=openai`); la cantidad visible **no** es la cantidad
  solicitada.
- El nombre comercial visible manda sobre la marca genérica y sobre descripciones largas. La consulta
  a cadenas se acorta (`search_query`) sin reescribir registros históricos.

### Búsqueda, comparación y cotización

- Búsqueda de cualquier producto comercializado, no solo medicamentos.
- Catálogo verificado de proveedores y ofertas; importación de planillas `.xlsx`, `.xls` y `.csv` con
  vista previa y estado de revisión.
- Cadenas con conector propio: Farmacias Ahumada, Salcobrand y Cruz Verde. Cada una falla por
  separado; una fuente caída nunca se presenta como «sin resultados».
- Cruz Verde: consultas de más de 50 caracteres se resuelven con hasta dos consultas internas más
  cortas, conservando la consulta original; un rechazo (`query_rejected`) se distingue de una caída.
- Comparación determinista E5 con elegibilidad, totales completos, cota inferior y criterios.
- La interfaz de cotizaciones consulta el carrito, actualiza sus líneas y finaliza la cotización
  mediante la API del backend. El historial recupera el informe guardado.
- Proveedores con URL de portal y credenciales cifradas en reposo con Fernet. Guardar una credencial
  **no** conecta ni prueba el portal.

### IA generativa

- Interpretación interna de observaciones comerciales con OpenAI Responses API, salida estructurada
  estricta y validación todo o nada.
- Preferencias **del producto específico**: ordenan sus ofertas, no se propagan por laboratorio,
  marca o proveedor y no construyen perfiles globales.
- Sin explicaciones, justificaciones ni puntuaciones visibles. La traza (modelo, prompt, estado) es
  auditoría interna.
- «Enseñar a la IA» guarda una observación comercial y solicita su interpretación mediante la API.

### Compras (no implementado)

No existen tablas, endpoints ni pantalla de compras registradas. `purchase_execution` es `blocked` en
todos los entornos. `fulfilled` no significa «recibido» y la recepción física está fuera del MVP.

## 4. Arquitectura y tecnologías

```
Lovable / navegador ──HTTPS/JSON──► FastAPI ──SQL──► PostgreSQL
        │                              │
        └── Supabase Auth (JWT)        ├── OpenAI (visión / observaciones)
                                       └── cadenas (HTTP público, en memoria)
Telegram ──polling──► FastAPI ──► PostgreSQL
```

| Capa | Tecnología |
|---|---|
| Backend | Python 3.14, FastAPI, SQLAlchemy 2 async, psycopg 3, Alembic, Pydantic v2 |
| Base de datos | PostgreSQL 17; único almacén transaccional; sin Redis ni colas externas |
| Frontend | React 19, TanStack Start/Router, Vite, TypeScript, Tailwind, shadcn/ui, Vitest |
| Autenticación | Supabase Auth (Google y correo+contraseña) + validación local del JWT contra JWKS |
| IA | OpenAI por HTTP (Responses API); sin SDK; proveedor simulado en pruebas |
| Canal | Telegram Bot API por polling; webhook disponible pero no expuesto |
| Despliegue | Heroku (backend) + Lovable (frontend conectado a GitHub `main`) |

El backend es un monolito modular: `backend/app/api/v1` (rutas), `services` (reglas y transacciones),
`domain` (productos, unidades, cantidades), `interpretation` (texto, visión y observaciones),
`telegram` (adaptador del canal) y `models` (SQLAlchemy). Los servicios reciben un `Actor` y una
sesión, verifican permisos y **no hacen commit**; la API abre la transacción. Auditoría y outbox se
emiten en la misma transacción que el cambio.

El frontend solo consume `/api/v1`; no accede a tablas ni a lógica de negocio. La organización y los
permisos salen del `Actor`, nunca de un campo del cuerpo.

Detalle completo: [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md).

## 5. Flujo general

1. **Ingreso**: Supabase Auth entrega un JWT; FastAPI valida firma, `iss`, `aud`, `exp` y `sub`, y
   busca el `sub` en `app_user.auth_subject`. Sin fila enlazada: `403 USER_NOT_PROVISIONED`.
2. **Reporte**: Telegram o `POST /observations` crean el faltante. La foto se guarda en PostgreSQL.
3. **Revisión**: compras ve la grilla, confirma atributos y corrige cantidades con comandos
   explícitos.
4. **Búsqueda**: E1–E3 sobre catálogo; E15 consulta cadenas en paralelo, cada una con su estado.
5. **Comparación**: E5 ordena ofertas elegibles y calcula totales solo cuando están completos.
6. **Cotización**: E10 arma carritos por proveedor y líneas en el backend; las asignaciones a
   reportes son explícitas. Al finalizar, el backend guarda el informe y libera los faltantes
   activos con cantidad conocida y cobertura suficiente. Los demás quedan activos.
7. **Compra**: **no implementada** en esta etapa. La compra real está bloqueada.

## 6. Integraciones

| Integración | Uso | Estado |
|---|---|---|
| **Telegram** | Reportes, sesiones, solicitudes y aprobación de cuentas | Implementado; bot real con polling; pruebas con simulador |
| **OpenAI** | Visión de fotos y observaciones comerciales | Adaptadores implementados; apagados por defecto; suite sin llamadas reales |
| **Supabase** | Google OAuth y correo+contraseña; JWT validado por JWKS | Implementado y verificado en Lovable Preview |
| **Heroku** | Backend, PostgreSQL y ejecución de migraciones | Desplegado; procedimiento manual documentado |
| **Lovable** | Frontend conectado a GitHub `main` | Desplegado; publicación manual desde el editor |
| **Cadenas** | Precios públicos de Ahumada, Salcobrand y Cruz Verde | Conectores implementados; habilitación por variables y autorización |
| **Proveedores** | Catálogo, ofertas, importación de planillas y credenciales de portal | Implementado; sin conector de portal |

Ninguna credencial se guarda en el repositorio. Las variables y su formato están en
[`docs/DEPLOY_HEROKU.md`](docs/DEPLOY_HEROKU.md) §4.

## 7. Autenticación y usuarios

- **Dos métodos, una identidad**: Google y correo+contraseña deben entregar el mismo `sub` de
  Supabase. Ese `sub` se enlaza a **una sola** fila `app_user`.
- No hay registro público ni recuperación de contraseña. Las cuentas se crean en Supabase y se
  habilitan en el backend.
- **Único perfil funcional web: `admin`**. `purchasing`, `auxiliary` y `approver` son roles de
  negocio/Telegram; no habilitan el Cotizador por sí solos.
- La matriz de permisos vive en PostgreSQL. `admin` puede ver la lista, administrar personal,
  cuentas de Telegram, identidades y accesos web; **no** reporta faltantes (`REPORT_SHORTAGE`).
- Una persona inactiva, sin fila enlazada o con acceso vencido recibe `403` y no ve datos.
- **Solicitud y aprobación web**: el backend implementa solicitud pública, campanita, aprobación que
  crea `admin`, rechazo, otorgamiento/revocación por superadministración y vencimiento. La campanita
  de Lovable es **solo demostración** (estado local, sin llamar a la API), por lo que el flujo no
  está verificado de punta a punta desde la interfaz.
- **Alta operativa usada para Ahmad Armoush**: cuenta creada en Supabase Auth, persona creada en el
  backend con rol `admin` mediante el servicio auditado `create_staff`, y `sub` enlazado con
  `link-auth-subject`. Ingresó con correo y contraseña desde Lovable Preview y accedió al dashboard.
  No se registran correo, UID ni contraseña en esta documentación.

Procedimientos, restricciones y estados: [`docs/AUTH_SUPABASE.md`](docs/AUTH_SUPABASE.md).

## 8. Despliegue

- **Backend en Heroku**: app `cotizador-farmacia-demo`, buildpack Python 3.14, `release` ejecuta
  `alembic upgrade head`, y el proceso `web` sirve FastAPI en `$PORT`.
- **Frontend en Lovable**: el repositorio `farmacia-frontend` está conectado a la rama `main`; cada
  push se sincroniza con el editor. La publicación al dominio final es manual.
- El despliegue de Heroku es **manual** desde `master`; no hay despliegue automático.
- Las migraciones se aplican en cada release. La cabeza del repositorio es **0032**. El último
  estado de Heroku informado para la entrega es `0032 (head)`, con backend `6b61fa1`. El commit
  documental `d0949a3` se publicó en GitHub.
- El `Procfile` versionado define `web` y `release`. El polling de Telegram puede correr como proceso
  `worker`; esa línea está en el árbol de trabajo pero no en la versión versionada al cierre de este
  documento, así que debe confirmarse antes de escalar el proceso en Heroku.
- La recuperación de fotos desde Telegram está apagada por defecto
  (`PHOTO_RECOVERY_ENABLED=false`).

Pasos exactos, variables, verificación y rollback: [`docs/DEPLOY_HEROKU.md`](docs/DEPLOY_HEROKU.md).

## 9. Ejecutar en local

Requisitos: Python 3.12+ (probado en 3.14), Docker Desktop y Node.js/npm para el frontend.

```bash
# 1. Backend
cp .env.example .env                 # ajustar valores locales; nunca commitear .env
scripts/dev.sh                       # crea .venv, instala dependencias y arranca en :8000
curl http://localhost:8000/api/v1/health
# Documentación interactiva: http://localhost:8000/docs

# 2. PostgreSQL local
open -a Docker
cd database && docker compose --env-file ../.env up -d
cd ../backend && .venv/bin/alembic upgrade head
.venv/bin/python -m app.cli seed-demo          # Pao, Ana, Luis + ciclo piloto; imprime IDs
scripts/dev.sh                                  # en otra terminal

# 3. Frontend (repositorio separado)
cd ../farmacia-frontend
npm install
npm run dev
```

El frontend usa `VITE_API_URL`, `VITE_SUPABASE_URL` y `VITE_SUPABASE_ANON_KEY`. La clave `anon` es
pública, pero se mantiene fuera del repositorio. En la vista previa de Lovable se usa la configuración
pública versionada como respaldo.

### 9.1 Backend en contenedor (Docker)

La imagen se construye con [`backend/Dockerfile`](backend/Dockerfile), usa la versión de
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


## 10. Pruebas

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
[`docs/PRUEBAS.md`](docs/PRUEBAS.md).

## 11. Estructura del repositorio

```
backend/        API FastAPI, dominio, servicios, migraciones, pruebas y Dockerfile
database/       docker-compose de PostgreSQL y datos semilla
docs/           arquitectura, módulos, despliegue, pruebas y decisiones
prompts/        prompts de sistema, golden set y evaluación
scripts/        utilidades de desarrollo y demostraciones
requirements.txt / Procfile / .python-version   preparación de Heroku en la raíz
.dockerignore   contexto de la imagen del backend (excluye secretos y archivos locales)
```

El frontend vive en un repositorio separado: `farmacia-frontend` (Lovable). No se duplica su
documentación aquí; este repositorio es la fuente de verdad del contrato y de las reglas.

## 12. Limitaciones y pendientes

- Compras registradas, cierre por compra e historial con búsqueda: no implementados.
- Recepción física y diferencias de entrega: fuera del MVP.
- No hay registro público ni recuperación de contraseña; el alta es operativa.
- MFA y reautenticación fuerte: pendientes (H5); `StrongActor` no es MFA.
- Cadenas reales: dependen de autorización y configuración por cadena; no hay precios persistidos.
- No hay conector de portales de proveedor: guardar credenciales no prueba acceso.
- La suite no llama a OpenAI ni a Telegram reales; usa dobles y simuladores.
- Retención/anonimización de datos personales en fotos: política pendiente.

Roadmap completo y criterios de aceptación: [`docs/ROADMAP.md`](docs/ROADMAP.md).
