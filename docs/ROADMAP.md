# Roadmap (alcance completo conservado; ver HLD §23 y criterios AC01–AC40)

El orden no reduce el alcance: la presentación final a Pao integra todos los módulos.

| Fase | Contenido | Criterios | Estado |
|---|---|---|---|
| **F0** | Base: repo, FastAPI, salud, config, Alembic, pruebas | — | **Hecha** (2026-10-28) |
| **H1-A** | Persistencia y reglas de demanda (sin Telegram): modelos, migraciones, deduplicación determinista, SET/ADD/CLEAR, auditoría/outbox, API de faltantes | AC03–AC12 (núcleo) | **Hecha** (2026-10-28) |
| **H1-B** | Telegram + sesiones de auxiliar + **fotografías** (sin texto, con texto, con cantidad) + interpretación contextual + identidad farmacéutica/comercial. Detalle y límites en `docs/H1B_TELEGRAM.md` | AC01, AC02, AC12 (e2e simulado), AC13 (API), AC14 | **Implementada con simuladores** (2026-10-28). Falta verificar con el bot real (token) y con un proveedor de visión real |
| **H1-B.0** | Separación simulación / prueba real (organizaciones por tipo, procedencia, guardias, aislamiento) y personal (mantenedor, disponibilidad, botones dinámicos, «Finalizar reportes»). Plan de datos: `docs/decisions/0002-…` | AC33 (aislamiento), AC01/AC02 reforzados | **Aplicada en `farmacia`**: migraciones 0004 y 0005 aplicadas (Alembic en `0005`) y script de datos 0006 ejecutado |
| **H1-B.4** | Acceso desde **múltiples cuentas** de Telegram: solicitud desde el bot, código temporal, aprobación/bloqueo/rehabilitación desde el frontend (API), identidad por cuenta, historial conservado. Detalle en `docs/H1B_TELEGRAM.md` | AC01/AC02 reforzados (autorización y trazabilidad) | **Implementada y probada con simulador de canal + PostgreSQL real; migración 0007 pendiente de tu autorización** |
| **H1-B.5** | Verificación con Telegram real: crear bot, `telegram-check`, `/solicitar`, aprobar desde la API con JWT, `telegram-poll`; vincular el Supabase de Pao; conectar proveedor de visión (requiere autorización y costo) | AC12, AC13 reales | Siguiente |
| **H1-C** | SSE con cursor sobre `outbox_event`, reconexión; dashboard Lovable (Faltantes) | AC15 | Tras H1-B |
| **H1b** | Dashboard en Lovable (Faltantes) conectado a la API | AC03–AC04, AC15 | Tras H1 |
| **H2** | Fotos, extracción con visión, resolución de catálogo | AC13–AC14 | |
| **H3** | Conectores reales (lectura autorizada), referencias de cadenas, comparación estricta | AC16–AC20 | Requiere validar acceso por proveedor |
| **H4** | Propuestas por excepciones, carritos persistentes, reglas conversacionales | AC21–AC24, AC29–AC31, AC37, AC39 | |
| **H5** | Aprobaciones por snapshot, ejecución bloqueada/sandbox, conciliación, recepción | AC25–AC28, AC34, AC40 | Compra real fuera de alcance del demo |
| **H6** | Piloto: evaluación semántica, seguridad, retención, observabilidad | AC36 y restantes | |

## H1-B.0 — separación de datos y personal (2026-10-28)

**Hecho:** migraciones `0004` (tipo de organización, procedencia, triggers, chats ficticios, vínculo único solo entre
activos) y `0005` (disponibilidad y motivo, personal que no se elimina, sesión `revoked`) **aplicadas en `farmacia`**
(Alembic en `0005`) y el script de datos `0006_separar_organizaciones.sql` **ejecutado**; protecciones de código; demos que
ya no pueden tocar lo real ni vaciar tablas; mantenedor `/api/v1/staff`; botones dinámicos; plan de migración de datos
ensayado en una copia (huellas idénticas). **Pendiente:** cargar el personal real definitivo si cambia el archivo de datos.
**Pendiente:** definir el vocabulario de ausencias con Pao (¿fechas de retorno automáticas?), pantalla de personal en Lovable, PIN si Pao lo pide.

## H1-B.4 — acceso desde múltiples cuentas de Telegram (2026-10-28)

**Objetivo:** que el mismo bot lo usen el teléfono compartido y los teléfonos personales de Pao y Danny (y personas futuras)
sin mezclar conversaciones ni perder la identificación individual de las auxiliares.
**Implementado:** solicitud desde el bot con `/solicitar` (`telegram_access_request`, `pending` sin acceso operativo);
código temporal de 20 caracteres que vence a los 30 min, de un solo uso, solo SHA-256 en la base, una pendiente por cuenta
y límites de intentos (`telegram_access_attempt`); aprobación/rechazo desde la API con `StrongActor` + `manage_accounts`
(JWT de Supabase en organizaciones reales); `telegram_account` (`personal`/`shared_device`) reutilizable; bloqueo y
rehabilitación con historial conservado; validación de acceso en el servidor **dos veces** (antes de persistir y dentro de
la transacción); identidad fija por cuenta en teléfonos personales y selector intacto en el compartido; `account_id` en
vínculos, sesiones, mensajes y observaciones.
**Migración:** `0007` reescrita (encadenada a `0005`; `0006` es un script de datos, no Alembic), con backfill solo de chats
privados reales y downgrade. **NO aplicada a `farmacia`**: requiere tu autorización explícita (0004/0005/0006 ya están
aplicadas).
**Pruebas:** 35 en `tests/test_telegram_accounts.py`; suite completa **313** con PostgreSQL 17 real. Cubren solicitud sin
persistir el código, pendiente sin acceso, rotación, idempotencia, vencimiento, límites de intentos, permisos y sesión
fuerte, aprobación personal/compartida, rechazo, bloqueo/rehabilitación con historial, aislamiento entre cuentas, teléfono
compartido sin regresión, guardias de simulación, JWT y backfill de la migración. **Con Telegram real no se probó**: el
canal se ejerció con el simulador.
**Decisión pendiente:** qué restricciones de negocio tendrán las cuentas administrativas frente a las operativas. Hoy
`access_role` es descriptivo y no se inventó ninguna restricción (D-053). También falta crear el proyecto Supabase
(no autorizado) para que Pao pueda ejercer la API administrativa en `real_test`.

## H1-B — informe de fase

**Objetivo:** que los auxiliares reporten faltantes por Telegram (texto, foto, o foto+texto) en un teléfono compartido,
con identidad declarada visible, sin inflar cantidades y sin inventar datos farmacéuticos.
**Implementado:** sesiones (selección, cambio, expiración, PIN preparado, auditoría); adaptador Telegram (webhook con secreto,
polling, cola de salida con reintentos, idempotencia por `update_id`); pendientes de atribución; lista paginada; interpretación
contextual en dos capas con IA tras una interfaz; identidad farmacéutica vs comercial (marca, laboratorio, fabricante, titular,
comercializador, GTIN, registro); catálogo verificado y referencias comerciales mínimos; alias de medida y de expresión;
fotografías con almacenamiento privado, estados de procesamiento, contrato de visión, simulador etiquetado, fusión foto+texto y
detección de duplicados; caso Migranol (foto sin texto, ancla por marca).
**Migración:** 0003 (canal, sesiones, eventos, mensajes, adjuntos, cola de salida, catálogo, alias ampliados, columnas de trazabilidad,
triggers de inmutabilidad), con backfill y downgrade.
**Pruebas:** **236** — 126 sin base de datos y 110 que exigen **PostgreSQL 17 real** (sin base salen omitidas; con `REQUIRE_DB=1` fallan).
*Simuladas:* todo Telegram (`tests/tgsim.py`) y toda lectura de imágenes (`mode="simulated"`). *Reales:* PostgreSQL, migraciones,
restricciones, concurrencia, transacciones. **Ninguna prueba demuestra comunicación con Telegram real ni lectura por una IA real.**
**Criterios:** AC01, AC02, AC04, AC11, AC12 (simulado), AC14, AC36 parcial (fixtures etiquetados); AC13 solo vía API; AC38 (edición); AC35 y AC15 pendientes.
**Pendiente / riesgos:** verificación con el bot real; proveedor de visión real (costo/privacidad: las fotos pueden mostrar datos personales);
retención de imágenes; PIN por chat; un producto por foto; el vocabulario real de Pao (formas, laboratorios, expresiones).

## H1-A — informe de fase

**Objetivo:** persistencia y reglas de demanda verificadas contra PostgreSQL real, antes de integrar servicios.
**Alcance:** organizaciones, usuarios/auxiliares (PIN preparado, no expuesto), ciclo de compra, faltantes,
observaciones, cantidades, auditoría inmutable, outbox, API. Sin IA, sin Telegram, sin conectores.
**Implementación:** `domain/` (parser y compatibilidad deterministas, cantidades puras), `services/`
(demanda, cantidades, eventos), `api/v1/demand.py`, migración `0001`. Detalle de decisiones en
`docs/decisions/0001-registro-de-decisiones.md` (D-008 a D-014).
**Pruebas (primera entrega):** 80 (34 sin base + 46 contra PostgreSQL 17 real: migración sube/baja/sube, modelos≡migración,
restricciones, escenarios, concurrencia, API HTTP). Sin base, las 46 se reportan *omitidas* (y fallan con `REQUIRE_DB=1`).
**Criterios cubiertos:** AC03, AC04, AC05, AC06 (SET), AC07, AC08, AC09, AC10, AC11, AC12 (a nivel de servicio/API con
clave de idempotencia; el evento Telegram real se demuestra en H1-B), AC24 (versión), AC26 parcial (permisos de
cantidad/resolución), AC33 parcial (aislamiento por organización en lecturas/escrituras de demanda).
**No cubierto aún:** AC01/AC02 (sesiones Telegram), AC15 (SSE), AC35 (estado "en pedido"), fusión manual de
líneas (`/demand-merges`) y `/demand-items/{id}/resolve` de identidad (H1-C/H2), CLEAR con asignaciones (H4).
**Riesgos:** la deduplicación es conservadora a propósito: variaciones ortográficas ("ibuprofen") crean necesidad
separada (falso negativo, seguro); la calibración con casos reales es H6. `DEFAULT_STRENGTH_UNIT` es una
convención a validar con Pao *(revocada: ver corrección del modelo de productos)*.

### H1-A — corrección del modelo de productos (2026-10-28)

Se revisó el modelo contra el requisito «no asumir unidades ni significados» y se rehízo (D-015 a D-024):
potencia/concentración, contenido, presentación y cantidad solicitada separados; unidades simples y compuestas
extensibles con el texto original conservado; procedencia explícito/inferido/confirmado por atributo; medidas
sin unidad pendientes; alias verificados; confirmación humana (`POST /demand-items/{id}/resolve`).
**Pruebas:** 138 (72 sin base + 66 que exigen PostgreSQL 17 real; sin base esas 66 salen omitidas y con REQUIRE_DB=1 fallan), incluida la migración de datos 0002 con filas legacy
(bajar a 0001, insertar filas H1-A —con «mg» supuesto—, subir y verificar, ida y vuelta).
**Criterios adicionales cubiertos:** AC08 (x 20 ≠ cantidad), AC09/AC10 ampliados (dosis, forma, contenido,
concentración, cifra sin unidad), AC30 (una inferencia no se vuelve regla/dato confirmado sin validación humana).
**Pendiente relacionado:** calibrar con casos reales de la farmacia (H6); vocabulario de formas/envases con Pao.

## F0 — informe de fase

1. **Objetivo:** base sólida y verificable. 2. **Alcance:** sin dominio, sin servicios externos.
3. **Archivos:** ver README. 4. **Implementación:** FastAPI con `create_app`, `Settings` validada, sesión async
perezosa, `/api/v1/health` (liveness) y `/health/ready` (503 honesto si falta/cae la BD), Alembic sin revisiones.
5. **Pruebas:** `backend/tests` (6 pasan; 1 contra Postgres real se omite sin `TEST_DATABASE_URL`).
6. **Aceptación:** app arranca, salud responde, pruebas pasan. 7. **Riesgos:** PostgreSQL no verificado en vivo
(Docker no estaba corriendo); Alembic no probado contra una BD. 8. **Continuar:** ver README y D-002/D-005.
