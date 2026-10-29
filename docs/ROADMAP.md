# Roadmap (alcance completo conservado; ver HLD §23 y criterios AC01–AC40)

El orden no reduce el alcance: la presentación final a Pao integra todos los módulos.

**Orden revisado el 2026-10-29 (D-073 a D-075):** el **cotizador** es la prioridad principal y **Telegram no bloquea** su
desarrollo. Las fases `C*` son el camino crítico; las `H*` siguen vigentes pero dejan de ser requisito previo.

| Fase | Contenido | Criterios | Estado |
|---|---|---|---|
| **C0** | Contrato integral del cotizador (E1–E9): búsqueda, ofertas, comparación, proveedores, fuentes y **criterios comerciales con anotación (sin aplicación)**. Documento: `docs/contracts/cotizador-v0.md` | C0-01 a C0-19 | **Completada — contrato aprobado y congelado** (2026-10-29, D-078 a D-093) |
| **C1** | Modelo de datos del cotizador, **diseño cerrado** (D-076/D-094, con adenda `chain_product`): artículo neutral al tipo de producto (`trade_item`), ficha publicada por cadena, proveedor, fuente, mapping, ofertas, despacho, referencias, importación, comparaciones, criterios y búsquedas. Se reparte en tres migraciones (**0008** catálogo y ofertas, 10 tablas; **0009** comparaciones y criterios; **0010** búsquedas) y suma `catalog-load` (carga inicial técnica, no administración cotidiana; D-097) | AC16–AC20 (base), AC29–AC31 (base) | **Diseño definido** (2026-10-29); migración **0008 por autorizar** |
| **C2** | Contrato de conector + `manual_import` (Excel) + fuente `fixture` etiquetada | AC16, AC17 | Pendiente; contrato de Excel abierto (D-077) |
| **C3** | Comparador determinista: elegibilidad, cantidades con mínimo/múltiplo, impuestos/descuentos/despacho, total parcial, cota inferior (LB-1…LB-8), explicabilidad y **anotación de criterios (sin alterar números)**; incluye la migración **0009**, con la que recién pasan a tener persistencia real las notas y criterios | AC18–AC20, AC31 (base) | Pendiente |
| **C4** | API de búsqueda y cotización (síncrona, D-075) y E9 `/commercial-criteria` (alta, edición, confirmación y desactivación auditadas); incluye la migración **0010** | AC16–AC19, AC29–AC31 | Pendiente |
| **C5** | Lovable: buscador y cotizador | AC20, AC31 (parcial) | Diseño en paralelo desde C0; **criterios y notas solo como demostración no operativa hasta C3 (0009)**; integración tras C4 |
| **C6** | Referencias de cadenas (precio público separado de la oferta) | AC20 | **Mecanismo abierto** (§9.3) |
| **F0** | Base: repo, FastAPI, salud, config, Alembic, pruebas | — | **Hecha** (2026-10-28) |
| **H1-A** | Persistencia y reglas de demanda (sin Telegram): modelos, migraciones, deduplicación determinista, SET/ADD/CLEAR, auditoría/outbox, API de faltantes | AC03–AC12 (núcleo) | **Hecha** (2026-10-28) |
| **H1-B** | Telegram + sesiones de auxiliar + **fotografías** (sin texto, con texto, con cantidad) + interpretación contextual + identidad farmacéutica/comercial. Detalle y límites en `docs/H1B_TELEGRAM.md` | AC01, AC02, AC12 (e2e simulado), AC13 (API), AC14 | **Implementada con simuladores** (2026-10-28). Falta verificar con el bot real (token) y con un proveedor de visión real |
| **H1-B.0** | Separación simulación / prueba real (organizaciones por tipo, procedencia, guardias, aislamiento) y personal (mantenedor, disponibilidad, botones dinámicos, «Finalizar reportes»). Plan de datos: `docs/decisions/0002-…` | AC33 (aislamiento), AC01/AC02 reforzados | **Aplicada en `farmacia`**: migraciones 0004 y 0005 aplicadas (Alembic en `0005`) y script de datos 0006 ejecutado |
| **H1-B.4** | Acceso desde **múltiples cuentas** de Telegram: solicitud desde el bot, código temporal, aprobación/bloqueo/rehabilitación desde el frontend (API), identidad por cuenta, historial conservado. Detalle en `docs/H1B_TELEGRAM.md` | AC01/AC02 reforzados (autorización y trazabilidad) | **Implementada y probada con simulador de canal + PostgreSQL real; migración 0007 aplicada el 2026-10-28 (D-062)** |
| **H1-B.5** | Verificación con Telegram real: crear bot, `telegram-check`, `/solicitar`, aprobar desde la API con JWT, `telegram-poll`; después el alta de la identidad de Pao (D-067); conectar proveedor de visión (requiere autorización y costo). La **primera administradora web** (`create-admin`, identidad separada de Pao) ya está creada y enlazada en `real_test` (2026-10-29, D-071/D-072) | AC12, AC13 reales | Pendiente, **no bloqueante** (D-073) |
| **H1-C** | SSE con cursor sobre `outbox_event`, reconexión; dashboard Lovable (Faltantes) | AC15 | Paralela, no bloqueante (sirve a Faltantes) |
| **H1b** | Dashboard en Lovable (Faltantes) conectado a la API | AC03–AC04, AC15 | Sección secundaria de Lovable (D-074) |
| **H2** | Fotos, extracción con visión, resolución de catálogo | AC13–AC14 | |
| **H3** | Conectores reales (lectura autorizada), referencias de cadenas, comparación estricta | AC16–AC20 | Se subdivide en C1–C4; referencias de cadenas en C6 (mecanismo abierto). Requiere validar acceso por proveedor |
| **H4** | Propuestas por excepciones, carritos persistentes, **aplicación de criterios comerciales**: recomendación explicada, reglas duras y memoria/aprendizaje desde evaluaciones; propuestas masivas | AC21–AC24, AC29–AC31, AC37, AC39 | |
| **H5** | Aprobaciones por snapshot, ejecución bloqueada/sandbox, conciliación, recepción | AC25–AC28, AC34, AC40 | Compra real fuera de alcance del demo |
| **H6** | Piloto: evaluación semántica, seguridad, retención, observabilidad | AC36 y restantes | |

## Reordenamiento 2026-10-29 — cotizador primero

**Hecho:** registro de D-073 (cotizador prioritario; Telegram no bloquea), D-074 (buscador/cotizador como pantalla
principal) y D-075 (búsqueda síncrona sobre catálogo local). **No se modificó código, base de datos, migraciones ni
configuración.**

**Qué cambia:** el camino crítico pasa a ser C0 → C1 → C2 → C3 → C4 → C5. **Qué no cambia:** alcance total, invariantes
del HLD §2, compra real bloqueada y el comportamiento existente de Telegram y Supabase Auth (el cotizador es aditivo).

**Rol del cotizador:** es una herramienta **interna** usada solo por los jefes de la farmacia. Todas sus operaciones
exigen rol **`admin`** y `StrongActor`; no se crea un rol `purchasing` ni permisos nuevos, y los roles de los demás
módulos no cambian (D-079).

## Revisión del contrato C0 — 2026-10-29 (D-086 a D-092)

**Hecho:** corregido `docs/contracts/cotizador-v0.md` con B1–B11 y registradas D-086 a D-091. **No se modificó código,
modelo, base de datos, migraciones ni OpenAPI.** En ese momento el contrato **no estaba congelado**: la aprobación
final llegó el 2026-10-29 (D-093, sección siguiente).

- **B1 — autorización.** ADMIN por chequeo de rol (sin permisos nuevos) **más** `StrongActor`; se documenta que
  `StrongActor` hoy solo rechaza la cabecera demo fuera de `simulation` y **no** es MFA (D-086).
- **B2 — ganador.** `best_eligible` → `lowest_verified_total`, con alcance explícito e `is_global_claim=false`; un
  parcial nunca tiene total (D-087).
- **B3 — cota inferior.** `lower_bound` nullable con `bound_status` y LB-1…LB-8; sin cota válida el proveedor va a
  `unknown_relation`, no a `could_be_beaten_by` (D-087).
- **B4 — aritmética.** Cantidades con `known`, stock contra `purchased`, impuestos, descuentos sin doble resta,
  despacho por esquema, otros cargos, moneda y redondeo (D-088).
- **B5 — unidades.** `QuantityUnit` (`box/package/unit/bottle`) y `original_text`; se elimina `"unit":"caja"` (D-089).
- **B6–B9 — esquemas y operaciones.** `OfferOut`, `ComparisonCandidateOut`, `SupplierTotalOut` y
  `CommercialCriterionOut` completos; E1 con `errors[]`, `sources[]` y `INVALID_CURSOR`; E3 con `reference_prices`
  vacío y anotaciones; E4 con `has_secret` y cuatro estados; E5 con candidatos `ineligible` siempre devueltos;
  `recommendation: null` reservado a H4.
- **B10 — E8.** Sobre congelado (transporte, idempotencia, códigos de fila, parciales); columnas en D-077 (D-090).
- **B11 — criterios comerciales (E9).** Registro y anotación en v0; aplicación y recomendación en H4; `note` vs
  `criterion`, sin `proposed`; no interferencia (D-091).
- **Cierre previo a la aprobación (D-092).** Tres niveles de resultado (candidato / proveedor parcial / proveedor
  completo) sin contradicción con C0-06 ni AC19; cantidad explicada con `adjustment`; trazabilidad nota → criterio sin
  anotaciones activas duplicadas; concurrencia con `If-Match`; alcance exacto por ámbito; creación directa solo con
  `confirm: true`.

## Cierre de C0 — 2026-10-29 (D-093)

**Hecho:** el usuario aprobó formalmente el contrato el 2026-10-29. `docs/contracts/cotizador-v0.md` queda
**APROBADO Y CONGELADO** y **C0 se marca completada**, con C0-01 a C0-19 verificados por revisión del documento
(C0-13 cumplido por la aprobación). **No se modificó código, modelo, base de datos, migraciones ni OpenAPI; la
migración 0008 no se ejecuta y C1 no se inicia sin autorización nueva.**

Qué implica el congelamiento:
- Lovable y el backend implementan el contrato **tal cual**; los tres planos (cálculo determinista, criterios sin
  interferencia y recomendación futura en H4) no se mezclan.
- Un cambio de alcance, esquema o semántica posterior exige una **decisión nueva** (D-094 en adelante) y una versión
  nueva del contrato; no se edita en silencio.
- Quedaron abiertas sin bloquear C1–C4: **D-076** (esquema del catálogo), **cerrada el 2026-10-29** con el diseño de
  C1 (sección siguiente), y **D-077** (contrato y formato de la planilla Excel), que sigue abierta.
- El siguiente paso es **C1** (modelo de datos), que requiere **autorización explícita de migración**.

## Diseño de C1 — 2026-10-29 (D-076, D-094, adenda `chain_product`)

**Hecho:** cerrado el diseño del modelo de datos del cotizador y registradas D-076 (esquema, con la adenda del mismo día
que incorpora la ficha publicada por cadena), D-094 (catálogo neutral al tipo de producto, alternativa A), D-095 y D-096
(diferidas) y D-097 (administración cotidiana del catálogo, pendiente). **No se escribió código, modelo, endpoint,
migración ni cambio de base de datos; la 0008 no se ejecutó y el contrato C0 no se modificó.**

Qué queda definido:
- **Artículo neutro:** `trade_item` admite medicamentos y productos no farmacéuticos; `product_kind` es interno,
  nullable y nunca inferido; ningún atributo farmacéutico es obligatorio. No se expone en E1/E3 (D-094).
- **Ficha publicada por cadena:** `chain_product` guarda lo que publica cada cadena (nombre tal como se escribe,
  descripción, laboratorio, presentación, `image_url`/`image_hash`, URL y fecha de consulta) como snapshot inmutable por
  `content_hash`; `reference_price.chain_product_id` es obligatorio y el vínculo con `trade_item` es explícito o
  confirmado, nunca inferido por principio activo y dosis. E1/E3 no exponen esos campos.
- **Tres migraciones por fase:** 0008 catálogo y ofertas (**10** tablas) → C2; 0009 comparaciones y criterios (5) → C3;
  0010 búsquedas (3) → C4. Cada tabla nace cuando su fase la consume.
- **Integridad:** FK compuestas por organización con claves auxiliares (`source`, `chain_product`, `comparison_line`),
  snapshots inmutables por trigger, `enforce_data_origin` en las tablas con datos externos, exclusividad nota ↔ criterio
  derivado y `complete ⇔ total` en la base (AC19).
- **Carga inicial:** `catalog-load` (CLI, idempotente, auditado, `--dry-run`), no un endpoint.

Tres límites explícitos:
1. **Notas y criterios no tienen persistencia real hasta la 0009.** El prototipo de Lovable puede mostrarlos como
   demostración, pero **no** deben presentarse como funcionalidad operativa; todo dato así se etiqueta `fixture` o
   `simulated` (HLD §22).
2. **`catalog-load` no es la administración del catálogo.** El alta y edición diaria queda como **D-097 pendiente**, sin
   endpoint nuevo y sin cambiar C0.
3. **La fotografía de una cadena es una referencia, no un archivo propio.** `image_url` e `image_hash` permiten señalar la
   imagen y detectar cambios, pero no garantizan que siga disponible; el almacenamiento propio queda pendiente para C6,
   con decisión aparte.

## Trabajo paralelo

| Pista | Contenido | Depende de |
|---|---|---|
| **A · Backend cotizador** | C0 → C1 → C2 → C3 → C4 | Autorización de la **migración 0008** (C1) y del contrato de Excel (C2) |
| **B · Diseño Lovable** | Buscador y cotizador contra el contrato v0, con datos `fixture` etiquetados | C0 **cerrada** (contrato congelado el 2026-10-29): puede comenzar |
| **C · Telegram** | H1-B.5 con el bot real (lo crea el usuario) | Token del bot |
| **D · Operación** | Contrato generado desde FastAPI, `supabase_jwt` en demo, URL HTTPS + CORS, CI | Autorización de configuración y despliegue |

### Regla de integración real

Lovable se conecta **solo a endpoints funcionales**, nunca a maquetas. Cada pantalla muestra el **origen** del dato
(`data_origin` y modo de fuente) y su **fecha**; `fixture` y `simulated` se ven como tales y jamás se presentan como
datos reales (HLD §5.2, D-038). Sin compra: `purchase_execution` sigue `blocked`.

## C0 — criterios de aceptación del contrato (2026-10-29) — cerrada

Se cerraron por **revisión del documento**, no por pruebas. El contrato es `docs/contracts/cotizador-v0.md` y quedó
**aprobado y congelado** el 2026-10-29 (C0-13, D-093). Los 19 criterios, verificados:

| # | Criterio |
|---|---|
| C0-01 | E1–E9 tienen método, ruta, request, response, errores y fase asignada |
| C0-02 | Cada operación y campo declara su origen en el HLD o se marca como derivado |
| C0-03 | Todo dato comercial lleva `source`, `mode`, `data_origin`, `observed_at`, evidencia y frescura por campo |
| C0-04 | Dinero como string decimal con moneda, base, impuestos y descuentos; nunca float |
| C0-05 | `offers[]` y `reference_prices[]` separados, sin ranking cruzado |
| C0-06 | Elegibilidad tri-estado **por candidato** con causas; solo `eligible` compone un total `complete`; estar en `supplier_totals` no implica elegibilidad |
| C0-07 | Parcial sin `total` ni opción de ganar; `lowest_verified_total` acotado (`is_global_claim=false`) con cota inferior nullable (LB-1…LB-8) |
| C0-08 | Estados `ok/stale/failed/not_configured`; una caída nunca es «sin ofertas» |
| C0-09 | Seguridad declarada: Bearer, organización desde el `sub`, 404 entre organizaciones, rol `admin` + `StrongActor` |
| C0-10 | Ninguna operación crea carrito, propuesta, aprobación ni ejecución |
| C0-11 | Sección «fuera de v0» explícita |
| C0-12 | Decisiones D-078 a D-093 registradas; D-076 y D-077 diferidas |
| C0-13 | Revisión y aprobación final del usuario; solo entonces se congela para Lovable — **cumplido el 2026-10-29** (D-093) |
| C0-14 | Unidades y cantidades: `QuantityUnit` único, `requested` entero, `minimum`/`multiple` con `known`, stock contra `purchased`, sobrante y `adjustment` explícito |
| C0-15 | Aritmética determinista: impuestos, descuentos sin doble resta, despacho por esquema, otros cargos, moneda y redondeo |
| C0-16 | E8: sobre congelado (transporte, idempotencia, códigos de fila, parciales); columnas diferidas a D-077 |
| C0-17 | Contrato de criterios completo: esquema, ámbitos, efectos, ciclo de vida, **trazabilidad nota → criterio**, **concurrencia por `If-Match`** y creación directa con `confirm: true` |
| C0-18 | No interferencia comprobable: un criterio no altera precios, totales, elegibilidad ni visibilidad |
| C0-19 | Nota vs criterio confirmado sin estados ambiguos: no existe `proposed`; nada se auto-promueve y **una nota y su criterio nunca están activos a la vez** |

Todas las operaciones del cotizador exigen rol `admin` (D-079) **y** `StrongActor` (D-086), verificados por separado:
el rol con un chequeo de rol —sin crear permisos nuevos— y `StrongActor` con su semántica real actual (rechazo de la
cabecera demo fuera de `simulation`; **no** es MFA). Leer precios, importar Excel, administrar proveedores o fuentes y
registrar criterios no crea permisos nuevos: es el mismo rol administrativo existente.

## Decisiones abiertas (no bloquean C1–C4)

- ~~**D-076**~~ **cerrada el 2026-10-29** (esquema del catálogo, ver «Diseño de C1»).
- **D-077 (pendiente):** carga de Excel **desde la interfaz administrativa**; contrato técnico y formato de planilla sin
  definir.
- **D-097 (pendiente):** **administración cotidiana del catálogo** (alta y edición de artículos, mappings y alias desde
  la interfaz). `catalog-load` es solo carga inicial; no se agrega endpoint ni se modifica C0 hasta decidirlo.
- **D-095 (diferida):** productos no farmacéuticos en el módulo de faltantes (`demand_item.ingredient` es `NOT NULL`).
- **D-096 (diferida):** unificación de `commercial_reference` y `trade_item`.
- Mecanismo de extracción de precios públicos de cadenas (API, archivo, captura o navegador; sin eludir CAPTCHA/MFA).
- **Almacenamiento propio de las imágenes y fichas publicadas por las cadenas (C6)**: hoy solo se conservan `image_url`,
  `image_hash` y la ficha, sin garantía de disponibilidad futura ni condiciones de uso resueltas.
- Acceso a portales privados de proveedores.
- Cupos de crédito y condiciones comerciales (sin modelo de datos; requisito exploratorio).
- Despliegue definitivo y escalado.
- Proveedor de IA/visión (HLD §4.2: no hay modelo acordado).

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
privados reales y downgrade.
**Pruebas:** 35 en `tests/test_telegram_accounts.py`; suite completa **313** con PostgreSQL 17 real. Cubren solicitud sin
persistir el código, pendiente sin acceso, rotación, idempotencia, vencimiento, límites de intentos, permisos y sesión
fuerte, aprobación personal/compartida, rechazo, bloqueo/rehabilitación con historial, aislamiento entre cuentas, teléfono
compartido sin regresión, guardias de simulación, JWT y backfill de la migración. **Con Telegram real no se probó**: el
canal se ejerció con el simulador.
**Estado:** `0007` **aplicada** a `farmacia` el 2026-10-28, con respaldo `pg_dump` verificado fuera del repositorio y
conteos sin cambios.
**Decisión pendiente:** qué restricciones de negocio tendrán las cuentas administrativas frente a las operativas. Hoy
`access_role` es descriptivo y no se inventó ninguna restricción (D-053). También falta crear el proyecto Supabase
(no autorizado) para que Pao pueda ejercer la API administrativa en `real_test`; el enlace de su identidad ya está
implementado por CLI y auditado (D-064 a D-070, `docs/AUTH_SUPABASE.md`).

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
