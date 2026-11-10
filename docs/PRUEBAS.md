# Pruebas y criterios de evidencia

Actualizado: 2026-11-09. Este documento reúne cómo se prueba el sistema, qué cubre cada suite y qué
significa cada nivel de evidencia. Los resultados que se citan son ejecuciones registradas; la
corrida de **1044 aprobadas y 8 omitidas** se ejecutó localmente al cerrar la etapa del motor de
proveedores con clasificación de coincidencias y bloqueo comercial (2026-11-09). Las demás cifras
provienen de las corridas informadas por la propietaria. Ninguna cifra es una garantía permanente ni
se extrapola a producción.

## 1. Niveles de evidencia

| Nivel | Significado | Ejemplo |
|---|---|---|
| **Implementado en código** | La ruta, el servicio o la migración existen y la suite los ejercita | Endpoints de cotizaciones |
| **Probado localmente** | Se ejecutó la suite o una prueba dirigida contra PostgreSQL local | 1044 pruebas backend |
| **Verificado en producción** | Se comprobó manualmente en Heroku/Lovable, con evidencia anotada | Inicio de sesión del profesor; foto tras redespliegue |
| **Pendiente** | Existe una decisión o un diseño, pero no código, o código sin conexión visual | Compras; carrito real en Lovable |

Reglas:

- Una prueba automatizada **no** demuestra que el proveedor externo respondió. Las suites usan dobles
  para OpenAI, Telegram y las cadenas.
- Una corrida con PostgreSQL omitido no cuenta como prueba de base de datos. Para validarla se usa
  `REQUIRE_DB=1`, que convierte la falta de base en fallo y no en omisión.
- No se declara «verificado en producción» nada que no se haya probado en el entorno desplegado.

## 2. Backend

### Preparación

Requisitos: Python 3.12+ (probado en 3.14) y PostgreSQL local. El repositorio trae un
`database/docker-compose.yml` con PostgreSQL 17 para desarrollo.

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

El entorno de pruebas se toma de `TEST_DATABASE_URL` (variable de entorno o `.env` local). **Nunca**
se usa `DATABASE_URL` para las pruebas.

```bash
export TEST_DATABASE_URL='postgresql+psycopg://farmacia:<CLAVE_LOCAL>@127.0.0.1:5432/farmacia_test'
export REQUIRE_DB=1
```

La clave es local y no se escribe en este documento ni en el repositorio.

### Seguridad de la base de pruebas

El fixture de sesión **borra y recrea el esquema `public`** de la base indicada y aplica todas las
migraciones desde cero antes de correr la suite. Por eso:

- La base debe terminar en `_test`; el propio `conftest.py` lo exige y aborta si no.
- Nunca apuntar `TEST_DATABASE_URL` a `farmacia`, `farmacia_demo`, `real_test` ni a la base de
  Heroku.
- Preferir una base desechable, por ejemplo `farmacia_test` en el contenedor local.
- No ejecutar la suite contra producción ni desde una consola de Heroku.

### Comandos

```bash
cd backend

# Suite completa; falla si PostgreSQL no está disponible.
REQUIRE_DB=1 .venv/bin/pytest

# Sin REQUIRE_DB, las pruebas de base se omiten: sirve para un chequeo rápido, no para validar.
.venv/bin/pytest

# Un archivo o una prueba concreta.
.venv/bin/pytest tests/test_cruzverde.py
.venv/bin/pytest tests/test_web_access_api.py -q

# Lint y formato.
.venv/bin/ruff check .
```

La primera corrida migra la base desde cero y tarda bastante más que las siguientes.

### Última corrida informada

| Fecha | Comando | Resultado informado |
|---|---|---|
| 2026-11-09 | `REQUIRE_DB=1 .venv/bin/pytest` | **1044 aprobadas, 8 omitidas** |

Las 8 omitidas no son fallos, pero tampoco evidencia de lo que omiten; el motivo de cada omisión
debe leerse en la salida de pytest y no se interpreta aquí. La corrida al cerrar D-148 fue de 963
aprobadas y 8 omitidas; la anterior a esta etapa era de 997. La corrida de 1034 se ejecutó localmente
el 2026-11-09 al incorporar el motor de proveedores (35 pruebas nuevas); al agregar la clasificación
de coincidencias y el bloqueo comercial se sumaron **10 pruebas** (9 puras de clasificación y 1 de
API con Predual frente a Pregalex/Pregalin), para un total de 1044.

Pruebas dirigidas relevantes:

| Archivo | Qué cubre | Última cifra registrada |
|---|---|---|
| `tests/test_cruzverde.py` | Límite de 50 caracteres, variantes, rechazo tipado y clasificación | 68 aprobadas |
| `tests/test_web_access_api.py` | Solicitud, aprobación, rechazo, campanita, límites | 16 aprobadas |
| `tests/test_telegram_accounts.py` | Auxiliares por cuenta, identificación y sucursal | 44 focalizadas al cerrar T1 |
| `tests/test_supplier_connectors.py` | Estrategia Algolia pura: normalización, errores HTTP, secretos, stock y huella | 23 aprobadas |
| `tests/test_supplier_matching.py` | Clasificación textual directo/relacionado y bloqueo comercial del carrito | 9 aprobadas |
| `tests/test_supplier_connectors_api.py` | Perfil, estados, caché, límite de tasa, clasificación Predual/Pregalex y no persistencia de ofertas | 15 aprobadas |
| `tests/test_auth_identity.py` | JWT, `iss`/`aud`, expiración, enlace de identidad | Suite completa |
| `tests/test_bootstrap_admin.py` | Restricciones de `create-admin` | Suite completa |

Los nombres de archivo pueden cambiar; si una ruta no existe, buscar con
`ls backend/tests | grep -i <tema>` antes de concluir que falta cobertura.

### Qué no prueba la suite

- No llama a Telegram: usa un cliente simulado y eventos de canal.
- No llama a OpenAI: usa un doble de `ObservationModel` y del proveedor de visión.
- No llama a los portales de cadena ni a Algolia/Farmaloop: usa respuestas HTTP simuladas y un doble
  de la estrategia. Las únicas verificaciones contra servicios reales fueron manuales y puntuales: el
  límite de 50 caracteres de Cruz Verde (2026-11-09) y **una** búsqueda real «predual» contra el
  índice público de Farmaloop (registrada el 2026-11-10T01:20:56Z), que devolvió 5 ofertas válidas y
  5 descartadas, sin persistir ofertas. Esa observación no forma parte de la suite, no se repite
  automáticamente y no se generaliza a otros productos, precios ni stock.
- No usa Supabase real: firma JWT con claves de prueba.
- No prueba el despliegue, CORS, el iframe de Lovable ni la publicación. Eso es verificación manual.

## 3. Frontend

Repositorio separado `farmacia-frontend` (Lovable).

```bash
cd farmacia-frontend
npm install
npm test              # vitest run
npm run lint          # eslint
npx tsc --noEmit      # tipos
```

### Última corrida informada

| Fecha | Comando | Resultado informado |
|---|---|---|
| 2026-11-09 | `npm test` | **238 aprobadas** |

Pruebas dirigidas del acceso web:

| Archivo | Qué cubre |
|---|---|
| `src/test/password-login.test.tsx` | Formulario correo+contraseña, orden visual, errores y presencia de Google (7 casos) |
| `src/test/session-password.test.tsx` | `signInWithPassword`, validación de sesión y deduplicación (2 casos) |
| `src/test/auth.test.ts` | Rutas protegidas, redirección y mensajes (+6 casos) |

```bash
npx vitest run src/test/password-login.test.tsx src/test/session-password.test.tsx src/test/auth.test.ts
```

La suite del frontend no inicia sesión contra Supabase real ni abre Lovable. Usa mocks y
Testing Library.

## 4. Verificaciones manuales en producción

Estas no son pruebas automatizadas y no se repiten en cada corrida:

| Fecha | Verificación manual | Resultado informado |
|---|---|---|
| 2026-11-09 | Heroku: `cd backend && alembic current` | **`0026 (head)`** |
| 2026-11-09 | Inicio de sesión con Google y con correo+contraseña en Lovable Preview | Confirmado; mismo `sub` |
| 2026-11-09 | El profesor Ahmad Armoush ingresó con correo y contraseña desde Lovable Preview | Confirmado; accedió al dashboard con rol `admin` |
| 2026-11-09 | Límite de 50 caracteres de Cruz Verde contra el buscador público | Confirmado manualmente |
| 2026-11-09 | Caso **Predual** después del despliegue | El faltante histórico muestra «Predual», el botón Buscar usa «Predual» y aparecen resultados de Cruz Verde, Salcobrand y Ahumada |
| 2026-11-09 | Fotografía de Predual después de un nuevo despliegue | Permaneció visible; el binario vive en PostgreSQL |

Estas verificaciones fueron informadas por la propietaria; no se repitieron para esta actualización
documental.

Pendientes de verificación en producción:

- **Validación general del reconocimiento de fotografías con OpenAI**: no se afirma que todos los
  productos se interpreten correctamente ni se registra una corrida facturable completa.
- Flujo completo Google → solicitud → campanita → aprobación desde Lovable (la campanita es demo).
- Carrito y cotizaciones desde la interfaz (hoy usan datos de demostración).
- «Enseñar a la IA» desde la interfaz (hoy deshabilitado).

## 5. Criterios para aceptar una entrega

1. `REQUIRE_DB=1 .venv/bin/pytest` sin fallos y con el número de omitidas explicado.
2. `npm test`, `npm run lint` y `npx tsc --noEmit` limpios si se tocó el frontend.
3. `ruff check .` limpio si se tocó el backend.
4. Migraciones revisadas: cabeza esperada, `down_revision` correcto, sin cambios destructivos sin
   respaldo.
5. Verificación funcional en local o preview de lo que la suite no cubre.
6. Ninguna credencial, token ni dato personal en el diff.

## 6. Higiene

- No incluir `.env`, tokens, claves, contraseñas ni capturas con datos reales en el repositorio, el
  chat o los logs.
- No usar datos de producción para pruebas automatizadas.
- No dejar contenedores, dynos ni procesos de prueba corriendo sin necesidad.
- Un resultado no registrado se describe como pendiente, no como aprobado.
