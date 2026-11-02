# Roadmap (alcance completo conservado; ver HLD §23 y criterios AC01–AC40)

El orden no reduce el alcance: la presentación final a Pao integra todos los módulos.

**Estado vigente al 2026-11-02 (G1 implementado en código).** El **cotizador** es la prioridad principal y **Telegram
no bloquea** su desarrollo. Las fases `C*` son el camino crítico; las `H*` siguen vigentes pero dejan de ser requisito
previo. **D0** consolidó el contrato v0.2 **revisión 5 → 6**; la **revisión 7 (2026-11-02, D-130)** establece que el
**único perfil funcional web es `admin`** y que **aprobar una solicitud de acceso otorga `admin`** (reemplaza D-128;
`purchasing` y `auxiliary` quedan como roles de Telegram). La revisión 5 cierra el ciclo de reposición **sin recepción
física** (cobertura con compras confirmadas, estado por Telegram y administradores que informan faltantes); la recepción
y las diferencias de entrega quedan fuera del MVP (D-126). **D-127** fija la regla definitiva de cierre: cotizar cubre
de forma provisional y **no cierra**; la necesidad queda gestionada solo cuando las compras confirmadas cubren toda la
cantidad pedida. **Enmienda del 2026-11-02: la compra es todo o nada** —se registra completa o no se registra, y al
registrarse libera el faltante—; **no hay compras parciales por productos ni cantidades en la demo**. D-131
(reporte repetido por Telegram) y D-132 (observaciones abiertas a cualquier `admin`, sin pasos extra) son documentales.
**G1 está implementado en código y la migración 0012 se aplicó únicamente a `farmacia_demo` el 2026-11-02** (con
respaldo `pg_dump` verificado); `farmacia` sigue en 0008 y no hay despliegue. **C4 está implementado en código**
(migración **0013** aplicada **solo a `farmacia_demo`**): E1/E2/E3 sobre el catálogo local y E15 de
cadenas sin persistencia; la suite con `REQUIRE_DB=1` queda en **486 pruebas**.

## Estado real de migraciones

| Ámbito | Versión | Notas |
|---|---|---|
| Base `farmacia` | **0008** | 32 tablas; la 0008 se aplicó el 2026-10-29 con respaldo `pg_dump` verificado, ensayo de subida y reversión y verificación por catálogo (D-098) |
| Base `farmacia_demo` | **0013** | 38 tablas; 0012 y 0013 se aplicaron el 2026-11-02 solo aquí, cada una con respaldo `pg_dump` verificado. El catálogo ficticio (30 artículos, 3 proveedores, 3 fuentes, 90 ofertas) se conservó intacto |
| Código (`backend/migrations/versions`) | **0013** | 0009–0011 de C2, **0012** de G1 y **0013** de C4 (tres tablas de búsqueda). **No aplicadas a `farmacia`** |
| Head propuesto | **0019** | G1 0012 y C4 0013 (**aplicadas solo a `farmacia_demo`**), C3 0014, C5-A 0015, O 0016, C5-B 0017 (3 tablas), C5-C 0018, C5-D 0019. Desde 0014, no creadas |

**No se aplica ninguna migración nueva a `farmacia` sin autorización explícita.** Las 0012 y 0013 se aplicaron solo a
`farmacia_demo` con autorización expresa; ninguna otra base se tocó.


## Fases

| Fase | Contenido | Criterios | Estado |
|---|---|---|---|
| **F0** | Base: repo, FastAPI, salud, config, Alembic, pruebas | — | **Hecha** (2026-10-28) |
| **H1-A** | Persistencia y reglas de demanda (sin Telegram): modelos, migraciones, deduplicación determinista, SET/ADD/CLEAR, auditoría/outbox, API de faltantes | AC03–AC12 (núcleo) | **Hecha** (2026-10-28) y corregida (D-015 a D-024) |
| **H1-B** | Telegram + sesiones de auxiliar + fotografías + interpretación contextual + identidad farmacéutica/comercial | AC01, AC02, AC12 (e2e simulado), AC13 (API), AC14 | **Implementada con simuladores** (2026-10-28) |
| **H1-B.0** | Separación simulación / prueba real y personal (mantenedor, disponibilidad, botones dinámicos) | AC33, AC01/AC02 reforzados | **Aplicada en `farmacia`**: 0004 y 0005, script de datos 0006 (D-037 a D-047) |
| **H1-B.4** | Acceso desde múltiples cuentas de Telegram: solicitud, código temporal, aprobación/bloqueo/rehabilitación | AC01/AC02 reforzados | **Implementada y probada con simulador + PostgreSQL real**; 0007 aplicada (D-062) |
| **H1-B.5** | Verificación con Telegram real: bot, `telegram-check`, `/solicitar`, aprobar con JWT, `telegram-poll`; alta de Pao; visión real | AC12, AC13 reales | **Pendiente, no bloqueante** (D-073) |
| **H1-C** | SSE con cursor sobre `outbox_event`, reconexión; dashboard Lovable (Faltantes) | AC15 | **Pendiente**, paralela y no bloqueante |
| **H1b** | Dashboard Lovable (Faltantes) conectado a la API | AC03–AC04, AC15 | Sección secundaria (D-074) |
| **C0** | Contrato integral del cotizador (E1–E9): búsqueda, ofertas, comparación, proveedores, fuentes y criterios comerciales con anotación | C0-01 a C0-19 | **Completada — aprobada y congelada** (2026-10-29, D-078 a D-093) |
| **C1** | Modelo de datos del cotizador: `trade_item`, ficha por cadena, proveedor, fuente, mapping, ofertas, despacho, referencias, importación, comparaciones, criterios y búsquedas | AC16–AC20 (base), AC29–AC31 (base) | **Hecha** (2026-10-29): **0008 aplicada** a `farmacia` (D-098) |
| **C2** | Conector + `manual_import` (Excel) + fuente `fixture` etiquetada + `catalog-load` + revisión de ofertas + E8 | AC16, AC17 | **Implementada en código** (0009–0011 y endpoint E8), **no aplicada a `farmacia`**. D-077/D-100 a D-108 |
| **D0** | Consolidación documental: contrato v0.2 rev6, D-113 y D-115 a D-128, este roadmap | — | **Cerrada** (2026-10-30); sin código ni migraciones. Revisión 7 del 2026-11-02 (D-130 a D-132) la supera en el modelo de acceso |
| **G1** | Acceso web con Google: solicitudes, campanita con aprobación que **otorga `admin`** (D-130) y sin vencimiento automático, superadministradora como control técnico, revocación y vencimiento explícito; seguridad JWT + `StrongActor` vigente (sin MFA); Telegram sin cambios | AC09 (web), D-117/D-119/D-130 | **Implementada en código** (2026-11-02); migración **0012** creada y probada, **no aplicada a `farmacia`** |
| **C4** | E1 con `relation` y `attributes.ingredients[]` (E3 conserva su forma); búsqueda de cadenas E15 (sin persistencia) | AC16–AC20, AC29–AC31 | **Implementada en código** (2026-11-02); migración **0013** aplicada **solo a `farmacia_demo`**. 14 pruebas nuevas |
| **C3** | Comparador determinista E5: elegibilidad, cantidades, impuestos/descuentos/despacho, total parcial, cota inferior (LB-1…LB-8), explicabilidad y anotación de criterios | AC18–AC20, AC31 | Pendiente; migración **0014** (5 tablas) |
| **C5-A + F1** | Cotizaciones, carritos por proveedor, líneas, vínculo a faltante, grilla «Productos faltantes» y «Buscar»; observaciones O1 | AC20, AC31, AC37 | Pendiente; migración **0015** |
| **O1–O4** | Interpretación de observaciones con OpenAI: O2 interpretación, O3 precedencia, O4 consejo explicado | AC37, AC39 | Pendiente; migración **0016** |
| **C5-B** | Compras registradas **todo o nada** (enmienda a D-127, 2026-11-02), inmutabilidad, correcciones, anulación y `purchase_index`; **cierre y liberación del faltante por compra confirmada**. **La recepción física queda fuera del MVP (D-126)**; las compras parciales también quedan fuera de la demo | AC21–AC24, AC39 | Pendiente; migración **0017** (**3 tablas**) |
| **C5-C** | Historial operativo: búsqueda por índice con `pg_trgm`/`unaccent` | AC21, AC31 | Pendiente; migración **0018** (extensión e índices) |
| **C5-D** | Archivo externo y purga controlada: diseño cerrado, **ejecución bloqueada** | AC36 (retención) | Pendiente; migración **0019** solo con proveedor aprobado |
| **C5-E** | Enganche de navegación: Mis cotizaciones, Historial, Proveedores (E6) | AC20 | Pendiente; sin migración |
| **C6a** | Cadenas reales: solo con autorización A1/A2 por cadena | AC20 | Pendiente; sin migración |
| **T1** | Pantalla «Configuración de Telegram»: control individual por cuenta | AC01/AC02 | Pendiente; sin migración (reusa API de H1-B.4) |
| **C6** | Referencias de cadenas (precio público separado de la oferta) | AC20 | **Mecanismo decidido en D-113/D-115; ninguna cadena habilitada** |
| **H2** | Fotos, extracción con visión, resolución de catálogo | AC13–AC14 | Pendiente |
| **H3** | Conectores reales (lectura autorizada), referencias de cadenas, comparación estricta | AC16–AC20 | Se subdivide en C1–C4; requiere validar acceso por proveedor |
| **H4** | Propuestas por excepciones, carritos persistentes, aplicación de criterios comerciales, recomendación explicada y memoria | AC21–AC24, AC29–AC31, AC37, AC39 | Pendiente |
| **H5** | Aprobaciones por snapshot, ejecución bloqueada/sandbox, conciliación; **recepción física** como funcionalidad futura | AC25–AC28, AC34, AC40 | Compra real fuera de alcance del demo; la recepción no está en el MVP (D-126) |
| **H6** | Piloto: evaluación semántica, seguridad, retención, observabilidad | AC36 y restantes | Pendiente |

## Reordenamiento D0 — bloques aprobados (2026-10-30)

**Hecho:** contrato `docs/contracts/cotizador-v0.2.md` **revisión 5** (→ **revisión 6** en la fase documental del
2026-10-30, D-128; → **revisión 7** el 2026-11-02, D-130); decisiones **D-113** y **D-115 a D-127** (→ **D-128** en esa
misma fase, luego superada); este roadmap. **No se modificó código, modelo, base de datos, migraciones, OpenAPI ni el
mockup aprobado** en D0.

**Orden de ejecución acordado:**

| # | Bloque | Contenido | Depende de | Migración |
|---|---|---|---|---|
| 1 | **D0** | Contrato v0.2 rev5, decisiones y roadmap | C0/C1/C2 | — |
| 2 | **G1** | Google, solicitudes, campanita, superadministradora, vencimiento y revocación web | D0 | **0012** (creada) |
| 3 | **C4** | E1 con `relation`, `attributes.ingredients[]`, cualquier producto (E3 conserva su forma); E15 de cadenas | G1 | **0013** |
| 4 | **C3** | E5: comparación, elegibilidad, totales, cotas y criterios | C4 | **0014** |
| 5 | **C5-A + F1** | Cotizaciones, carritos, líneas, vínculo a faltante, grilla, «Buscar», observaciones O1 | C3 | **0015** |
| 6 | **O1–O4** | Interpretación, precedencia y consejo explicado con OpenAI | C5-A | **0016** |
| 7 | **C5-B** | Compras registradas, correcciones, anulación e índice de historial | C5-A | **0017** |
| 8 | **C5-C** | Búsqueda del historial con `pg_trgm`/`unaccent` | C5-B | **0018** |
| 9 | **C5-D** | Archivo externo y purga: diseño cerrado, ejecución bloqueada | C5-B, proveedor aprobado | **0019** |
| 10 | **C6a** | Cadenas reales | A1/A2 por cadena | — |
| 11 | **T1** | Configuración de Telegram por cuenta | H1-B.4 | — |

**C5-E** (enganche de navegación) puede ir en paralelo con C5-A/C5-B; **C6a** y **T1** no crean migraciones.

**Enmienda D-128 (revisión 6, 2026-10-30) — superada por D-130 el 2026-11-02.** Aprobar una solicitud en G1 crea el
`app_user` con `roles = ['admin']` y `auth_subject` en el mismo paso (D-130); el Cotizador (E10–E15) admite `admin` +
`StrongActor`, salvo la revisión de interpretaciones, que sigue siendo de `admin`. G1 conserva sus **5 columnas** y su
única tabla: no se agrega `telegram_reporter` ni ninguna otra columna. Aprobar no crea cuenta ni vínculo de Telegram;
el selector del teléfono compartido sigue siendo identificación declarada dentro de un chat autorizado. Excluir a
alguien de ese selector es una decisión de **T1** (D-118).

**Numeración ratificada (D-125).** D-112/D-114 proponían C3 = 0012 y C4 = 0013. Con G1 por delante y el código ya en
0011, la numeración lineal del orden acordado es **C4 = 0013** y **C3 = 0014**, confirmada el 2026-10-30. Conservar
C3 = 0013 exigiría **ejecutar C3 antes que C4**, lo que contradice el orden acordado; queda descartado. No se
renumera en silencio: la numeración definitiva es la de esta tabla.

**Tablas de búsqueda (D-125).** C0 describe **dos** tablas (`search_run`, `search_source_result`) y el diseño de C4
propone **tres** (`search_result` como ampliación **aditiva** de C0, no un reemplazo). Las tres pertenecen a la
migración 0013; el detalle está en el contrato §16.

**Dependencias duras.**

- G1 antes de C4: las pantallas nuevas necesitan acceso web real.
- C5-A no puede empezar sin C3: la evaluación por carrito es E5; no se implementa una segunda lógica de dinero.
- O1–O4 después de C5-A: sin observaciones no hay nada que interpretar.
- C5-B después de C5-A: las compras referencian líneas cotizadas.
- C5-C y C5-D al final: solo tienen sentido con compras y archivo.
- C6a depende de autorización por cadena; T1 de la API de Telegram existente.

## Reordenamiento 2026-10-29 — cotizador primero

**Hecho:** registro de D-073 (cotizador prioritario; Telegram no bloquea), D-074 (buscador/cotizador como pantalla
principal) y D-075 (búsqueda síncrona sobre catálogo local). **No se modificó código, base de datos, migraciones ni
configuración.**

**Qué cambia:** el camino crítico pasa a ser C0 → C1 → C2 → D0 → G1 → C4 → C3 → C5-A → O1–O4 → C5-B → C5-C → C5-D.
**Qué no cambia:** alcance total, invariantes del HLD §2, compra real bloqueada y el comportamiento existente de
Telegram y Supabase Auth (el cotizador es aditivo).

**Rol del cotizador:** es una herramienta **interna** usada solo por los jefes de la farmacia. Todas sus operaciones
exigen rol **`admin`** + `StrongActor` (D-130, que sustituye la enmienda D-128 y la cláusula «único rol autorizado:
`admin`» de D-079); no se crea ningún rol ni permiso nuevo. `purchasing` y `auxiliary` siguen existiendo para Telegram
y no habilitan el Cotizador.

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
- El siguiente paso es **C1** (modelo de datos), que requiere **autorización explícita de migración**.

## Diseño de C1 — 2026-10-29 (D-076, D-094, adenda `chain_product`)

**Hecho:** cerrado el diseño del modelo de datos del cotizador y registradas D-076 (esquema, con la adenda del mismo
día que incorpora la ficha publicada por cadena), D-094 (catálogo neutral al tipo de producto, alternativa A), D-095 y
D-096 (diferidas) y D-097 (administración cotidiana del catálogo, pendiente). **No se escribió código, modelo,
endpoint, migración ni cambio de base de datos; la 0008 no se ejecutó y el contrato C0 no se modificó.**

Qué queda definido:

- **Artículo neutro:** `trade_item` admite medicamentos y productos no farmacéuticos; `product_kind` es interno,
  nullable y nunca inferido; ningún atributo farmacéutico es obligatorio. No se expone en E1/E3 (D-094).
- **Ficha publicada por cadena:** `chain_product` guarda lo que publica cada cadena (nombre tal como se escribe,
  descripción, laboratorio, presentación, `image_url`/`image_hash`, URL y fecha de consulta) como snapshot inmutable
  por `content_hash`; `reference_price.chain_product_id` es obligatorio y el vínculo con `trade_item` es explícito o
  confirmado, nunca inferido por principio activo y dosis. E1/E3 no exponen esos campos.
- **Tres migraciones por fase:** 0008 catálogo y ofertas (**10** tablas) → C1; 0009 comparaciones y criterios (5) →
  C3; 0010 búsquedas (3) → C4. Cada tabla nace cuando su fase la consume. *(Numeración histórica de este diseño: el
  código real usa 0009–0011 para C2 y D0 renumera C4 = 0013 y C3 = 0014, §«Reordenamiento D0».)*
- **Integridad:** FK compuestas por organización con claves auxiliares (`source`, `chain_product`, `comparison_line`),
  snapshots inmutables por trigger, `enforce_data_origin` en las tablas con datos externos, exclusividad nota ↔
  criterio derivado y `complete ⇔ total` en la base (AC19).
- **Carga inicial:** `catalog-load` (CLI, idempotente, auditado, `--dry-run`), no un endpoint.

Tres límites explícitos:

1. **Notas y criterios no tienen persistencia real hasta la migración de C3.** El prototipo de Lovable puede mostrarlos
   como demostración, pero **no** deben presentarse como funcionalidad operativa; todo dato así se etiqueta `fixture`
   o `simulated` (HLD §22).
2. **`catalog-load` no es la administración del catálogo.** El alta y edición diaria queda como **D-097 pendiente**,
   sin endpoint nuevo y sin cambiar C0.
3. **La fotografía de una cadena es una referencia, no un archivo propio.** `image_url` e `image_hash` permiten
   señalar la imagen y detectar cambios, pero no garantizan que siga disponible; el almacenamiento propio queda
   pendiente para C6, con decisión aparte.

## C2 — implementación en código (2026-10-29/30)

**Hecho:** C2 está implementada en código: perfiles de importación (0009), estado de revisión de oferta (0010),
solicitudes de importación (0011), `catalog-load`, conector `manual_import` y endpoint E8. **Las migraciones
0009–0011 existen, pero no se aplicaron a `farmacia`.** La base real sigue en 0008.

**Qué no cambió:** C0, el esquema aplicado a `farmacia`, los roles y el frontend. **Pendiente:** autorizar la
aplicación de 0009–0011 a `farmacia`; hasta entonces C2 no se declara operativa en la base real.

## C1 — aplicación de la migración 0008 (2026-10-29, D-098)

**Hecho:** la migración **0008** (catálogo comercial, fichas de cadena, precios públicos y ofertas de proveedores,
**10 tablas**) está **aplicada a `farmacia`**. La fase C1 queda cerrada: el esquema del cotizador existe, sin
endpoints, sin `catalog-load` y sin conectores (eso es C2).

Procedimiento ejecutado, cada paso con autorización explícita del usuario:

1. **Respaldo (A1).** `pg_dump -Fc` de `farmacia` en 0007, guardado en `.backups/` y en `~/respaldos-farmacia/`
   (fuera del repo), con SHA-256 `b176e73d…52bdc0`. Restaurado en una base desechable y comparado: misma versión,
   22 tablas, 219 filas y huella de contenido idéntica por tabla.
2. **Ensayo de subida (A2).** Copia de la base anterior → `farmacia_rehearsal_0008`; `upgrade 0008` → 32 tablas;
   las 22 tablas originales conservaron sus 219 registros y su comportamiento; 14 comprobaciones de invariantes
   aprobadas; inventario de 948 objetos idéntico al de una instalación desde cero.
3. **Ensayo de reversión (A2-bis).** `downgrade 0007` → 22 tablas, 219 registros y 559 objetos idénticos al 0007
   prístino, sin residuos; `upgrade 0008` de nuevo → 948 objetos idénticos y los mismos invariantes operativos.
4. **Aplicación (A3).** `alembic upgrade 0008` sobre `farmacia` con guarda de destino, API y bot detenidos.
   Verificación por catálogo: **0008**, **32 tablas**, 10 tablas nuevas vacías, **219 registros intactos**, 9
   funciones, 30 triggers, 306 restricciones, 120 índices, y **ningún objeto previo alterado** (559/559 idénticos).
5. **Comprobación funcional de la API (en proceso, solo GET).** `TestClient` de Starlette dentro del mismo
   proceso —sin puerto, sin servidor y sin llamadas externas— sobre `farmacia`: **27/27 casos** con el estado
   esperado. Salud y `ready` en 200; identidad por cabecera con 401 sin actor y con actor inexistente; listas,
   catálogo, fotos, canal, personal y cuentas en 200 para la organización `simulation`; 403 `FORBIDDEN` cuando
   falta el permiso (auxiliar en catálogo y observaciones); 404 y 400 con el contrato de error; y **403
   `STRONG_AUTH_REQUIRED`** para `demo_header` sobre la organización `real_test` (D-071/D-072). Las 10 tablas
   nuevas no se exponen: ninguna ruta del cotizador en el contrato. Instantánea de las 32 tablas (filas + MD5 por
   tabla) **idéntica** antes y después: la comprobación no escribió nada.

**Pruebas:** 365 pruebas automatizadas en verde sobre una base de pruebas dedicada en 0008, incluidas las 13 de
`test_migration_0008.py` y `test_modelos_y_migracion_estan_sincronizados`.

**Riesgos:** los invariantes nuevos exigen `data_origin` coherente con la organización en la primera carga
(`real_test` solo admite `captured`/`ai_derived`); la 0008 no es idempotente; `alembic` no imprime nada en
`upgrade`/`downgrade`, así que el éxito se verifica siempre por catálogo. El respaldo de A1 y las bases de ensayo
se conservan hasta que el usuario autorice su eliminación. **Pendiente de seguridad antes del despliegue (D-099):**
`AUTH_MODE=demo_header` permite hoy **leer** datos reales con una persona de `real_test` (solo bloquea la
administración con `STRONG_AUTH_REQUIRED`); la publicación de un entorno con `APP_ENV=local`/`demo` exige antes
restringir el modo demo a `simulation` o retirarlo. No se implementa en C1.

## Trabajo paralelo

| Pista | Contenido | Depende de |
|---|---|---|
| **A · Backend cotizador** | D0 → G1 → C4 → C3 → C5-A → O1–O4 → C5-B → C5-C → C5-D; después C5-E | C1 **cerrado** (0008 aplicada); C2, **G1 y C4 implementados en código**; G1 con `0012` aplicada **solo a `farmacia_demo`**; `0013` espera autorización |
| **B · Diseño Lovable** | Mockup aprobado, sin modificar; buscador y cotizador contra el contrato v0.2 rev7, con datos `fixture` etiquetados | C0 **cerrada**; D0 **cerrado**; no consume endpoints que aún no existen |
| **C · Telegram** | H1-B.5 con el bot real (lo crea el usuario); T1 (pantalla de control individual, con D-131) | Token del bot; API de H1-B.4 ya existente |
| **D · Operación** | Contrato generado desde FastAPI, `supabase_jwt` en demo, URL HTTPS + CORS, CI | Autorización de configuración y despliegue |
| **E · Documentación** | **Hecho el 2026-11-02**: contrato **rev7**, **D-130 a D-132**, `CLAUDE.md`, `docs/AUTH_SUPABASE.md` y este roadmap alineados con G1 implementado | Cerrada con la orden del 2026-11-02 |

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
| C0-09 | Seguridad declarada: Bearer, organización desde el `sub`, 404 entre organizaciones, rol `admin` + `StrongActor` (D-130; D-128 superada) |
| C0-10 | Ninguna operación crea carrito, propuesta, aprobación ni ejecución (X1/X2 de D-114 son las únicas excepciones) |
| C0-11 | Sección «fuera de v0» explícita |
| C0-12 | Decisiones D-078 a D-093 registradas; D-076 y D-077 diferidas |
| C0-13 | Revisión y aprobación final del usuario; solo entonces se congela para Lovable — **cumplido el 2026-10-29** (D-093) |
| C0-14 | Unidades y cantidades: `QuantityUnit` único, `requested` entero, `minimum`/`multiple` con `known`, stock contra `purchased`, sobrante y `adjustment` explícito |
| C0-15 | Aritmética determinista: impuestos, descuentos sin doble resta, despacho por esquema, otros cargos, moneda y redondeo |
| C0-16 | E8: sobre congelado (transporte, idempotencia, códigos de fila, parciales); columnas diferidas a D-077 |
| C0-17 | Contrato de criterios completo: esquema, ámbitos, efectos, ciclo de vida, **trazabilidad nota → criterio**, **concurrencia por `If-Match`** y creación directa con `confirm: true` |
| C0-18 | No interferencia comprobable: un criterio no altera precios, totales, elegibilidad ni visibilidad |
| C0-19 | Nota vs criterio confirmado sin estados ambiguos: no existe `proposed`; nada se auto-promueve y **una nota y su criterio nunca están activos a la vez** |

Todas las operaciones del cotizador exigen rol `admin` **y** `StrongActor` (D-130, que supera la enmienda D-128),
verificados por separado:
el rol con un chequeo de rol —sin crear permisos nuevos— y `StrongActor` con su semántica real actual (rechazo de la
cabecera demo fuera de `simulation`; **no** es MFA). Leer precios, importar Excel, administrar proveedores o fuentes y
registrar criterios no crea permisos nuevos: es el mismo rol operativo existente.

## Decisiones abiertas

- ~~**D-076**~~ **cerrada el 2026-10-29** (esquema del catálogo, ver «Diseño de C1»).
- ~~**D-077**~~ **cerrada el 2026-10-29**: contrato de entrada del catálogo propio y de las planillas de proveedor,
  perfiles por fuente y validación (D-100 a D-108).
- **D-097 (pendiente):** **administración cotidiana del catálogo** (alta y edición de artículos, mappings y alias desde
  la interfaz). `catalog-load` es solo carga inicial; no se agrega endpoint ni se modifica C0 hasta decidirlo.
- **D-095 (diferida):** productos no farmacéuticos en el módulo de faltantes (`demand_item.ingredient` es `NOT NULL`).
- **D-096 (diferida):** unificación de `commercial_reference` y `trade_item`.
- **D-107 (registro desactualizado):** la fila del registro dice «Propuesta», pero el código **0009–0011** ya implementa
  el perfil de importación y las solicitudes de importación. Actualizar su estado en la fase documental posterior; no
  se reescribe en D0.
- **D-108 (pendiente):** puntos del diseño que tocarían C0 (`dry_run` en la API, códigos de archivo, fuente que no sea
  `supplier`, detalle estructurado de filas rechazadas). No bloquean la implementación.
- ~~**P-8 — cobertura y compras**~~ **resuelta el 2026-10-30 (D-121):** las compras `current` no anuladas cubren,
  sin doble conteo con las líneas activas de cotizaciones `draft`, y cerrar una cotización no devuelve el faltante a
  pendiente. **D-127** añade que esa cobertura por cotización es provisional y que el ciclo solo se cierra con compra
  confirmada que cubra la cantidad pedida.
- ~~**Numeración C4/C3**~~ **ratificada el 2026-10-30 (D-125):** C4 = **0013** y C3 = **0014**.
- ~~**P-9, P-10 y P-11 — recepción**~~ **no bloquean D0 ni la demo desde el 2026-10-30 (D-126):** la recepción
  física está fuera del MVP. Se conservan como preguntas **futuras**, solo si la recepción vuelve al alcance:
  anular/corregir una compra con recepciones, compra que nunca llega o parcial definitiva, y corrección de una
  recepción errónea. El diseño está en D-122 (retirada) y en el contrato §15.1.
- **Superadministradora:** confirmar que el cruce entre organizaciones es **solo** para metadatos administrativos;
  ampliarlo a datos operativos exige decisión nueva.
- **Telegram `shared_device`:** el control individual es por cuenta; en un teléfono compartido el bloqueo afecta a
  todas las personas. Confirmar que el control individual se limita a cuentas `personal`.
- **Cadenas reales:** ninguna habilitada. Falta A1/A2 para Farmacias Ahumada y un conector autorizado para Cruz Verde y
  Salcobrand. Sin eso, la demo muestra `not_configured` con motivo, no precios.
- **OpenAI:** clave, modelo, presupuesto mensual, tarifa y autorización de costo. Sin ellos, O2–O4 quedan
  deshabilitados; O1 sigue operativo.
- **Archivo externo:** proveedor, costos, credenciales y política de retención. Sin ellos, C5-D sigue bloqueado.
- **`pg_trgm`/`unaccent`:** verificar disponibilidad en el PostgreSQL de destino y autorizar su instalación antes de
  C5-C.
- **D-099 (pendiente):** restringir `AUTH_MODE=demo_header` a `simulation` o retirarlo antes de publicar un entorno
  demo/local.
- **Almacenamiento propio de las imágenes y fichas publicadas por las cadenas (C6):** hoy solo se conservan
  `image_url`, `image_hash` y la ficha, sin garantía de disponibilidad futura ni condiciones de uso resueltas.
- Acceso a portales privados de proveedores.
- Cupos de crédito y condiciones comerciales (sin modelo de datos; requisito exploratorio).
- Despliegue definitivo y escalado.
- Proveedor de IA/visión (HLD §4.2: no hay modelo acordado). OpenAI se usa solo para observaciones, no para visión.

## Revisión 5 del contrato — 2026-10-30 (D-121 a D-127)

**Hecho:** `docs/contracts/cotizador-v0.2.md` pasa a **revisión 5** y registra D-121 a D-127. Se documenta el ciclo
completo de reposición —aviso del faltante, cobertura, compra y cierre— **sin recepción física**. **No se modificó
código, modelo, base de datos, migraciones ni el mockup.**

- **D-121 — cobertura con compras.** La cobertura de un faltante considera las compras `current` no anuladas además
  de las líneas activas de cotizaciones `draft`, sin doble conteo. Anular o corregir una compra recalcula; cerrar
  una cotización comprada **no** devuelve el faltante a pendiente. Resuelve P-8 sin migración.
- **D-122 — recepción física (retirada del MVP por D-126).** El diseño (tablas `receipt`/`receipt_line`, parciales,
  diferencias, cierre condicionado) se conserva como **funcionalidad futura**, separado del MVP; no crea tablas,
  endpoints, permisos ni pantallas.
- **D-123 — estado por Telegram.** Un usuario autorizado consulta el estado; informar de nuevo un faltante ya
  comprado no crea una segunda necesidad: el bot responde que **hay una compra registrada**, sin afirmar que el
  producto llegó. Cantidad adicional solo por `SET/ADD/CLEAR` explícitos (AC35). Sin IA: determinista y de solo
  lectura.
- **D-124 — administradores informan faltantes.** `admin` obtiene `REPORT_SHORTAGE` en la matriz de permisos y puede
  informar por Telegram si su cuenta está `active`; no se crea un rol nuevo ni se excluye a nadie. Permisos web y
  de Telegram son independientes. **El cambio de matriz no se implementa en D0.**
- **D-125 — numeración y búsqueda.** C4 = **0013** y C3 = **0014** ratificados; las **tres** tablas de búsqueda de
  C4 (`search_run`, `search_source_result`, `search_result`) quedan explicadas como ampliación aditiva de C0.
- **D-126 — recepción fuera del MVP.** Se retira del alcance implementable todo el control de recepción física y
  diferencias de entrega: sin `receipt`/`receipt_line`, cantidades entregadas, parciales, saldos pendientes de
  recepción, correcciones/anulaciones de recepción, endpoints, permisos, validaciones ni pantallas, y sin cierre
  condicionado a recepción. C5-B vuelve a **3 tablas** y v0.2 a **9 tablas**. Una compra confirmada marca el término
  del proceso que administra el MVP; **no certifica** la llegada física.
- **D-127 — cierre solo por compra confirmada (enmienda del 2026-11-02: compra todo o nada).** La necesidad se
  considera gestionada cuando la cantidad solicitada queda **completamente cubierta por compras confirmadas**;
  `fulfilled` no significa recibido. Cotizar cubre de forma **provisional** (mientras la línea esté en un carrito
  activo) y **nunca** cierra. La compra se registra **completa o no se registra**; al registrarse cierra y libera el
  faltante para un nuevo reporte. ~~Compra parcial: 10 pedidas, 6 compradas → 6 cubiertas y 4 abiertas en el mismo
  faltante~~ **Fuera de la demo (2026-11-02):** no hay compras parciales por productos ni cantidades.
  `is_pending` significa «el ciclo sigue abierto» y se distinguen dos saldos: `pending_quantity` (falta **cotizar**) y
  el derivado `pending_purchase_quantity` (falta **comprar**); un faltante cubierto por un carrito se ve `in_quote`, no
  «sin gestionar». La distinción
  aviso repetido / ciclo nuevo depende del **ciclo**, no de un plazo ni de la recepción. Cantidad desconocida: no se
  cierra automáticamente; solo sale del ciclo por `ADD`/`SET` explícito o anulación con motivo.

**Cierre del ciclo (D-127).** Un `demand_item` `active` con cantidad pedida **conocida** pasa a `fulfilled` solo cuando
las **compras confirmadas** alcanzan el objetivo. Con cantidad desconocida no se cierra automáticamente: la cantidad no
se inventa. Cerrar no borra nada; si el faltante vuelve, se crea una solicitud nueva y se conserva el historial.

**Pendientes reales antes de aprobar D0:** P-9/P-10/P-11 (recepción) **ya no bloquean D0 ni la demo**; quedan como
preguntas futuras solo si la recepción vuelve al alcance. Los bloqueos externos siguen siendo los mismos (cadenas,
OpenAI, archivo, `pg_trgm`, `farmacia`/0011 y documentos desactualizados).

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

## Revisión 6 del contrato — fase documental (2026-10-30, D-128) — **superada por la revisión 7**

> Se conserva como historial. **D-130 (2026-11-02) reemplaza D-128:** aprobar otorga `admin` y `purchasing` no es
> perfil web. Lo demás —aprobación transaccional, Telegram intacto, seguridad JWT + `StrongActor` sin MFA, acceso sin
> vencimiento automático, P-7— sigue vigente.

**Hecho:** `docs/contracts/cotizador-v0.2.md` pasa a **revisión 6**; se registra **D-128**; y se alinean `CLAUDE.md`,
`docs/AUTH_SUPABASE.md` y este roadmap. **No se modificó código, modelo, pruebas, migración ni base de datos.**

- **D-128 — aprobación web con rol operativo.** Aprobar una solicitud crea el `app_user` con `roles = ['purchasing']`,
  `active = true` y `auth_subject` en el mismo paso. El Cotizador (E10–E15) admite `admin` **o** `purchasing` +
  `StrongActor`, salvo la revisión de interpretaciones, que sigue siendo de `admin`. Aprobar no otorga `admin`,
  `is_superadmin`, permisos `MANAGE_*` ni acceso a Telegram.
- **Enmiendas, sin reescribir historia.** D-079 («único rol autorizado `admin`»), D-114 (`admin` + `StrongActor` para
  E10/E11/E12) y D-117 («aprobar no asigna roles») conservan su texto y reciben la nota «vigente según D-128».
- **Telegram intacto.** La web no crea cuenta, vínculo ni chat autorizado; la autorización es por cuenta (D-118) y el
  selector del teléfono compartido es identificación declarada dentro de un chat ya autorizado. **No se agrega
  `telegram_reporter`** ni ninguna columna nueva; excluir a alguien de ese selector es decisión de **T1**.
- **Sin cambios de esquema.** v0.2 sigue con **9 tablas** y **7 columnas**; G1 sigue con **1 tabla** y **5 columnas** en
  `0012`. `farmacia` sigue en **0008** y el código en **0011**.
- **Seguridad y vigencia, confirmadas el 2026-10-30.** La autenticación es el **JWT de Supabase validado** y
  `StrongActor` con su semántica vigente (D-086/D-099): **no es MFA ni una segunda autenticación**, no bloquea a una
  persona aprobada en modo JWT y la autenticación fuerte real queda para **H5**. La aprobación **no fija vencimiento**
  (`web_access_expires_at = NULL`, sin plazo automático): el acceso sigue vigente hasta revocación, desactivación o
  vencimiento explícito.
- **P-7 resuelto:** la revisión de interpretaciones de IA sigue exigiendo `admin` + `StrongActor`; no se amplía a
  `purchasing`.

## Revisión 7 del contrato y G1 implementado en código (2026-11-02, D-130 a D-132)

**Hecho:** `docs/contracts/cotizador-v0.2.md` pasa a **revisión 7**; se registran **D-130** (reemplaza D-128),
**D-131** y **D-132**; se alinean `CLAUDE.md`, `docs/AUTH_SUPABASE.md` y este roadmap; y **G1 queda implementado en
código** con la migración **0012** creada y probada. **No se aplicó ninguna migración a una base operativa**
(`farmacia` sigue en 0008, `farmacia_demo` en 0011), **no se desplegó** y **no se tocó Lovable**.

- **D-130 — un solo perfil funcional web: `admin`.** Google/Supabase identifica pero no concede; se solicita acceso;
  todas las `admin` ven las pendientes; cualquier `admin` aprueba o rechaza; aprobar crea `roles = ['admin']`,
  `active` y `auth_subject` en un paso, sin vencimiento. `superadmin` es control técnico (ve todas las organizaciones,
  otorga/revoca, no aprueba en nombre de una farmacia). Telegram y la web siguen separados.
- **D-131 — reporte repetido por Telegram.** Si el producto ya está en la lista activa, el bot informa quién lo
  reportó antes y, si la persona confirma, registra un reporte **dentro del faltante existente** sin duplicar el
  producto; el producto se libera al registrar la compra; el historial se conserva; **Telegram no usa OpenAI**.
  Se implementa en **T1**.
- **D-132 — observaciones comerciales abiertas.** Cualquier `admin` usa el botón «Observación» con texto libre; las
  observaciones persisten y afectan las propuestas de IA; no se exigen explicaciones ni formularios ni pasos extra.
  Complementa D-116; se implementa en **O1–O4 (C5-A)**.
- **D-129 sigue vigente:** observaciones comerciales en `/api/v1/commercial-observations`; `/api/v1/observations`
  queda solo para faltantes. El contrato rev7 ya nombró la ruta.
- **G1 en código:** tabla `web_access_request`, cinco columnas en `app_user`, índice de una sola superadministradora
  activa, endpoints de solicitud/campanita/aprobación/rechazo y de otorgar/revocar, `set-superadmin` por CLI,
  permiso `MANAGE_WEB_ACCESS` solo para `admin`, `WEB_ACCESS_ORGANIZATION_ID`, cabeceras demo `X-Demo-*` y
  `403 WEB_ACCESS_EXPIRED`. **16 pruebas nuevas; 472 en la suite con `REQUIRE_DB=1`**; la 0012 quedó aplicada **solo a
  `farmacia_demo`**.
- **C4 en código:** `search_run`, `search_source_result` y `search_result` (0013, aplicada **solo a `farmacia_demo`**), E1/E2/E3 con `relation`
  y filtros cerrados, E15 de cadenas en memoria (`not_configured` con el motivo real de cada cadena), modelos de
  respuesta OpenAPI de las cuatro rutas y **14 pruebas nuevas; 486 en la suite con `REQUIRE_DB=1`.**
- **Siguiente paso:** **C3** (`0014`).
