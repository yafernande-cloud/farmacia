# Registro de decisiones y diferencias con el HLD/LLD

Fecha de inicio: 2026-10-28. Estados: **Tomada**, **Pendiente** (requiere al usuario).

## Diferencias entre el HLD/LLD y el prompt maestro (rige el prompt)

| ID | HLD/LLD dice | Se aplica | Impacto |
|---|---|---|---|
| D-001 | API y web en TypeScript (§4.2, P) | Backend Python/FastAPI; web en Lovable | Ninguno funcional. Las reglas de negocio del HLD se conservan íntegras |
| D-001b | Estructura `apps/`, `packages/`, `db/` (§27, P) | `backend/`, `frontend/`, `database/`, `tests/`, `docs/` | Solo organización |
| D-001c | Archivos privados compatibles con S3 (P) | Se decide en H2 (Supabase Storage u otro) | Sin efecto hasta fotos |

## Decisiones

| ID | Estado | Decisión |
|---|---|---|
| D-002 | Tomada (2026-10-28) | Supabase probablemente para el demo, **sin crear el proyecto aún**. Trabajo con PostgreSQL 17 local (Docker) verificado con migraciones y pruebas |
| D-003 | Tomada | Alembic + SQLAlchemy 2 async/psycopg3. Esquema gobernado por migraciones del repo |
| D-004 | Tomada | La compra real es imposible por configuración en esta etapa; `/health` lo declara |
| D-005 | Tomada (diseño) | Supabase Auth con JWT validado por FastAPI; permisos en nuestra base. Diseño en `docs/AUTH_SUPABASE.md`; **no activada**. Mientras tanto `AUTH_MODE=demo_header` (prohibido en staging/production) |
| D-006 | Tomada | Sin hooks de IA en operaciones deterministas (cantidades, dinero, reglas, deduplicación exacta) |
| D-007 | Tomada | Python mínimo 3.12; entorno local verificado con 3.14.6 |

| D-008 | Tomada | Telegram se crea tras probar el backend de faltantes (H1-B); el token no bloquea H1-A |
| D-009 | Tomada | Auxiliares: selección por nombre (identidad *declarada*, visible, cambiable). `auxiliary_profile.pin_hash` y `core/security.py` (scrypt) preparados para el PIN del piloto; sin sesiones de canal aún |
| D-010 | Tomada | Roles en `app_user.roles`. Auxiliar: reportar y ver; propone cantidad vía `declared_quantity` (sugerencia, nunca aplicada). Compras: aplica cantidades y resuelve dudas. Admin/aprobador: sin permisos de demanda por defecto. Verificado en servicios, no solo en API |
| D-011 | Tomada | Deduplicación por `ingredient` (texto normalizado) + atributos. Incompatible (dosis/forma/contenido/principio distintos) → nunca candidato. Idéntico único → asocia. Parcial (atributo conocido de un solo lado) o varios idénticos → `needs_review`, sin elegir por el usuario. Política `h1a-conservative-1`, reemplazada por `h1a-conservative-2` (D-018). Un nombre comercial se trata como otro principio: no se fusiona con el genérico |
| D-012 | Tomada | Bloqueo `pg_advisory_xact_lock` por (organización, ciclo, principio) serializa avisos concurrentes; índice único parcial como respaldo para identidades completas; lock por Idempotency-Key. Observaciones no suben `version` de la necesidad (solo cambios de cantidad), para no provocar conflictos falsos |
| D-013 | Tomada | Idempotencia por organización + clave (`Idempotency-Key`), con hash del contenido: mismo contenido → resultado original (200, `replayed`); otro contenido → 409. Cantidades exigen además `If-Match` (428 si falta, 409 si desactualizado). ADD sobre cantidad desconocida o unidad distinta → 422 |
| D-014 | Tomada | `audit_event` y `quantity_event` son de solo agregar (trigger en PostgreSQL). Cantidades se serializan sin ceros de relleno ("5", "2.5"). ~~Unidad de potencia supuesta (`DEFAULT_STRENGTH_UNIT`)~~ **revocada por D-015** |

## Corrección del modelo de productos (2026-10-28, tras H1-A)

Motivo: el usuario indicó que un número sin unidad no puede asumirse como mg. La revisión del modelo H1-A
encontró además errores más graves (ver «Hallazgos»). Estas decisiones **reemplazan** lo que decía D-011/D-014 sobre unidades.

| ID | Estado | Decisión |
|---|---|---|
| D-015 | Tomada | **Un número sin unidad no tiene unidad ni rol.** «ibuprofeno 400» guarda la cifra 400 como *medida pendiente* (`unassigned`); no hay mg, UI ni nada supuesto. Se elimina `DEFAULT_STRENGTH_UNIT`. No entra en la identidad ni en la clave de unicidad hasta que una persona autorizada la confirme |
| D-016 | Tomada | **Cinco conceptos separados**: potencia/concentración (`strengths`, lista: soporta combinaciones), contenido del envase (`content`), presentación comercial (`form` + `content` + `packaging`), cantidad solicitada (`requested_*` en la observación; sugerencia, nunca parte de la identidad ni de `target_quantity`) y medidas sin rol (`unassigned`). Se guardan en `demand_item.attributes` (JSONB, esquema v2); `ingredient` y `resolved_identity_key` siguen como columnas por indexación |
| D-017 | Tomada | **Procedencia por atributo**: `explicit` (lo escribió la persona), `inferred` (lo dedujo una regla por contexto), `confirmed` (lo validó una persona autorizada o un alias verificado). *Desconocido* = ausente/`unassigned`, nunca un valor. Una inferencia nunca pasa a confirmada sola; solo `POST /demand-items/{id}/resolve` o `resolve` de una observación (con permiso `confirm_attributes`, solo compras) lo hace. La identidad completa y la clave única exigen todo explícito/confirmado |
| D-018 | Tomada | **Política `h1a-conservative-2`.** Un aviso se registra SIEMPRE; nunca se bloquea ni se le pregunta al auxiliar. Lo que cambia es el vínculo: *idéntico y sólido* → se asocia solo; *parcial, no confirmado o ambiguo* → `needs_review` con candidatos y **motivos** (`unit_unconfirmed`, `strength_unknown_one_side`, …) para que compras confirme en un paso; *incompatible* (dosis, forma, contenido, principio, o cifra sin unidad que no calza con nada) → jamás candidato. La respuesta al auxiliar nunca es una pregunta: informa lo ambiguo («la unidad de 500 (no la supuse)») |
| D-019 | Tomada | **Memoria verificada (`product_alias`)**: cuando compras resuelve un aviso con `remember_alias`, se guarda «para este principio activo, la cifra X sin unidad = potencia Y» (solo ese producto y esa cifra; por organización; revisable y desactivable). Aplicada, el atributo queda `confirmed` con `role_rule=verified_alias`. Así «ibuprofeno 400» deja de ir a revisión sin que el sistema haya adivinado nada |
| D-020 | Tomada | **Roles por unidad escrita**: mg, mcg, UI, mmol, mEq, %, y toda compuesta (mg/mL, mg/5 mL, UI/mL, mcg/dosis…) → potencia (explícita, porque la unidad está escrita). mL/L, dosis y conteo → contenido. **g es ambiguo** (1 g comprimido vs 30 g crema): con forma sólida → potencia *inferida*; con forma semisólida/líquida → contenido *inferido*; sin forma → medida sin rol. «N comprimidos» sin marcador (`x`, `caja de`) → contenido *inferido* (podría ser una cantidad pedida). Se ignora cualquier unidad como contenido si es de potencia |
| D-021 | Tomada | **Unidades extensibles** (`domain/units.py`): registro `UnitDef` + alias (µg/ug/mcg, cc/ml, lt/l, gr/g, IU/UI…). Compuestas válidas = numerador (masa, UI, mmol, mEq) / denominador (volumen, masa, dosis). Cada `Measure` conserva valor, unidad, denominador y **el texto exacto escrito** (`50 µg`, `1.000 MG`); la normalización (g→mg, mg/5 mL→mg/mL) se **calcula al leer** y solo sirve para comparar y para la clave: añadir unidades no exige migrar datos |
| D-022 | Tomada | Separador de miles chileno: `1.000 mg` = 1000 mg; `2,5` y `1.5` = 2,5 y 1,5. Ambiguo por naturaleza (`1.500` se lee como 1500); si Pao usa otra convención hay que cambiarlo |
| D-023 | Tomada | El texto original es evidencia: `demand_observation.raw_text`, `parsed` y `author_id` son **inmutables** (trigger en PostgreSQL). `attributes.original_text` conserva el texto del primer aviso |
| D-024 | Tomada | Migración 0002 con backfill: lo explícito se conserva; un «mg» que H1-A había supuesto, o un volumen que H1-A llamaba potencia, pasa a medida pendiente (no se conserva como hecho). Downgrade soportado con pérdida controlada (solo primera potencia y contenido). La API cambia (campos `strength`, `dosage_form`, `content_amount`, `release`, `needs` → `attributes`, `pending_validation`, `needs_quantity`); sin consumidores aún |

### Hallazgos de la revisión (defectos reales del modelo H1-A, ya corregidos y con prueba de regresión)

- `ibuprofeno 400` → 400 mg «inferido» (lo que el usuario señaló).
- `levotiroxina 50 µg` → **50 g** (se perdía el símbolo µ: error ×1000 en el producto).
- `ibuprofeno 1.000 mg` → 1 mg (miles chilenos).
- `ibuprofeno 5 cajas de 20` → potencia «20 mg» inventada.
- `ibuprofeno 100mg/5ml jarabe 120 ml` → principio activo «ibuprofeno ml» y contenido perdido; `suero 500 ml` → «potencia 500 ml».
- `paracetamol 500 mg codeína 30 mg` → principio «paracetamol codeina mg» (segunda potencia perdida); `caja de 20 comprimidos` contaminaba el nombre.
- `amoxicilina 250 mg/5 ml` guardaba solo «50 mg/ml» (se perdía lo escrito).
- La coincidencia por sistema de «400» con «400 mg» fusionaba sobre una suposición.

### Pendientes de validación con Pao (V)

- ¿Qué significa para ella una cifra suelta de cada producto que compra? (alimenta los alias).
- Vocabulario de formas y envases real de la farmacia (hoy es una lista corta; las formas confirmadas por una persona aceptan texto libre).
- ¿`caja` vs `blister` vs `frasco` distinguen productos para ella? Hoy el envase es informativo: nunca fusiona ni separa por sí solo.

## H1-B: interpretación contextual, Telegram, sesiones y fotografías (2026-10-28)

| ID | Estado | Decisión |
|---|---|---|
| D-025 | Tomada | **Dos capas.** `interpretation/` comprende y PROPONE (atributos, candidatos, procedencia, incertidumbres); `domain/`+`services/` VALIDAN y deciden. La IA vive tras `AIInterpreter` (sin implementación, sin servicio externo); sus propuestas se degradan a candidato `ai_unverified` y se descartan si inventan cifras, unidades inválidas o dicen «confirmado» |
| D-026 | Tomada | Lenguaje cotidiano determinista: números en palabras («cincuenta mil», «dos»), muletillas, saludos y preguntas (no crean faltantes), un producto por línea/`;`/coma (no por «y»: puede ser una combinación). Cada medida cita el texto escrito («cincuenta mil») |
| D-027 | Tomada | **Identidad farmacéutica ≠ comercial.** Farmacéutica: principio + potencia/concentración + forma + liberación (`pharma_identity_key`). Comercial: marca, laboratorio, fabricante, titular del registro, comercializador, GTIN, registro sanitario, contenido, envase. Entidades independientes: ninguna se deduce de otra ni del aspecto del envase; desconocido ≠ inventado. Un laboratorio/marca/GTIN distinto conocido en ambos lados **no se fusiona solo** (revisión) y separa la clave estricta |
| D-028 | Tomada | Alias de dos tipos (`measure_unit`, `expression`), por organización, con `confirmed_attributes` y desactivables. Un alias confirma SOLO lo validado (p. ej. `strength.unit`): jamás forma, contenido ni presentación comercial. Un alias de expresión es equivalencia de *nombre*, no clínica. El catálogo verificado (`pharma_identity`) y las referencias (`commercial_reference`, embrión de H3) solo proponen candidatos; no confirman |
| D-029 | Tomada | **Ancla por nombre comercial** (`name_anchor`): si de una foto solo se lee la marca, esta agrupa los avisos sin afirmar principio activo; queda pendiente `active_ingredient`. Compras lo confirma con `ConfirmPatch.ingredient` |
| D-030 | Tomada | **Visión.** Contrato `VisionExtraction`; estados `pending/processing/identified/needs_review/error`; lo leído de imagen es `inferred` con `origin=image`; cantidad visible nunca es cantidad solicitada; foto+texto con contradicción → conflicto (AC14); GTIN con checksum inválido se rechaza; misma imagen (hash) del mismo autor no duplica. **No hay proveedor real**: `none` (guardar y dejar pendiente) o `simulated` (etiquetado `mode="simulated"`, solo demo/pruebas). No se afirma lectura por IA |
| D-031 | Tomada | **Sesiones declaradas**, expiración deslizante por inactividad (`SESSION_IDLE_MINUTES`=10), una activa por chat, auditoría de selección/cambio/expiración/cierre. PIN: verificación con bloqueo ya implementada (contador persistente); el adaptador no lo exige en el demo |
| D-032 | Tomada | **Mensajes sin identidad quedan pendientes y no se atribuyen solos.** Identificarse no atribuye: se pide confirmación explícita; los de más de 15 min se descartan. Reloj único (el del evento) para sesiones y antigüedad |
| D-033 | Tomada | **Entrada/salida.** `inbound_event` único por `update_id`; una transacción por evento; respuestas en cola `outbound_message` con reintentos y espera (429 `retry_after`), errores permanentes marcados; webhook solo con secreto de origen (comparación en tiempo constante), apagado sin configuración; polling local (`getUpdates`) para no exponer el backend; token solo en `SecretStr`, nunca en logs/errores (se silencia el log INFO de httpx, que imprime la URL) |
| D-034 | Tomada | El bot solo registra avisos y consulta: no aplica cantidades, no aprueba, no compra. Una edición de mensaje no reaplica nada |
| D-035 | Tomada | Imágenes originales en almacenamiento privado local (interfaz `PrivateStorage`, listo para S3): tipo por firma de bytes, tamaño máximo, 0600, direccionado por SHA-256, sin salir del directorio |
| D-036 | Tomada | Comparación: dos atributos **inferidos de la misma fuente** y coincidentes se consideran lo mismo aún sin confirmar (dos lecturas de imagen de la misma caja suman aviso); una inferencia frente a un dato explícito/confirmado NO coincide (decide una persona) |

### Defectos reales hallados al implementar H1-B (corregidos, con prueba)

* `to_dict()` omitía marca, laboratorio y origen: se perdían al guardar (solo la persistencia real lo mostró).
* La antigüedad de un mensaje pendiente usaba el reloj de la base y no el del evento: un mensaje de 41 min contaba como reciente.
* El contador de intentos de PIN se perdía por el rollback de la excepción (sin límite real de fuerza bruta).
* `line_for` fallaba cuando el aviso quedaba en revisión (sin necesidad asociada): el evento habría quedado fallido y en reintento perpetuo.
* Un update malformado hacía caer el parseo (`KeyError`); ahora se ignora.

## Separación simulación / prueba real y personal (2026-10-28)

Motivo: la auditoría de la primera prueba con Telegram real mostró datos de una demo mezclados con la prueba real (misma
organización, mismos usuarios, mismo ciclo y un catálogo «verificado» de la demo que alimentó candidatos reales).
Plan de migración de los datos existentes (**pendiente de autorización**): `docs/decisions/0002-plan-migracion-organizaciones.md`.

| ID | Estado | Decisión |
|---|---|---|
| D-037 | Tomada | **Organizaciones por tipo** (`organization.kind`, inmutable): `simulation` (solo simulaciones y pruebas automatizadas), `real_test` (bot real de pruebas), `production`, `legacy_mixed` (transitorio, para datos previos a la separación; no recibe simulaciones nuevas). Chats ficticios reservados `555000x` (5550001): solo pueden vincularse a `simulation` y una `simulation` solo acepta chats ficticios (trigger en la base + validación en la CLI). Un chat solo puede estar activo en una organización |
| D-038 | Tomada | **Procedencia** (`data_origin`) en observaciones, necesidades, mensajes, fotos, catálogo, referencias y alias: `simulated`, `captured` (recibido de una persona o dispositivo real), `ai_derived` (resultado derivado por un modelo; en fotos, `extraction_provenance`), `unclassified` (solo `legacy_mixed`). **No es confianza ni confirmación**: un atributo `ai_derived` puede quedar `confirmed` por una persona sin dejar de ser `ai_derived`. La organización manda (trigger): simulación ⇒ solo `simulated`; real/producción ⇒ nunca `simulated` ni `unclassified` |
| D-039 | Tomada | **Guardias.** Una lectura de imagen `simulated` se rechaza fuera de la organización de simulación (adjunto `error`, sin observación ni necesidad, con auditoría); `telegram-poll` con `VISION_PROVIDER=simulated` exige que TODOS los chats activos sean de simulación; la CLI no tiene organización por defecto (`--org sim|real`); las demos usan solo la organización de simulación, abren un ciclo nuevo en cada corrida y **ya no vacían tablas** |
| D-040 | Tomada | **Aislamiento**: toda consulta, deduplicación, catálogo, alias, auditoría y feed de eventos (`services/event_feed.py`, base del SSE de H1-C) se acota por `organization_id`; una prueba de esquema exige `organization_id` NOT NULL + FK en toda tabla de negocio. Una simulación jamás se fusiona con una necesidad real |
| D-041 | Pendiente (plan entregado) | Migración de datos existentes: **opción A «archivo»** (0 filas cambian de organización, 0 triggers desactivados, 0 autorías cambiadas). Ensayada en una copia |
| D-042 | Tomada | **Personal: tres conceptos separados.** *Pertenecer* (`active`), *disponibilidad para reportar* (`available` + motivo `vacation/sick_leave/day_off/other` + nota) y *sesión de Telegram activa* (deriva de `actor_session`). Nunca se elimina (trigger): se desactiva. Nombres únicos entre activos (sin distinguir mayúsculas). Un nombre cambiado no reasigna la autoría (va por id) |
| D-043 | Tomada | **Mantenedor** `/api/v1/staff` (listar completo, crear, renombrar, activar/desactivar, disponibilidad con motivo) exige el permiso `manage_staff` = rol `admin` (compras sin `admin` no administra; el HLD separa ambos). **Pao tiene `purchasing` + `admin` en el demo** (decisión de datos). No se puede desactivar a la última administradora. Cada cambio queda auditado con antes/después y emite evento |
| D-044 | Tomada | **Botones dinámicos**: `/start` y «Cambiar auxiliar» leen de PostgreSQL solo personas activas, disponibles y con rol que permita reportar. **Validación en el backend** en cada selección y en cada uso de una sesión: un botón antiguo no elige a quien ya no está disponible; si una persona deja de estar disponible o activa se **revocan sus sesiones** (`end_reason=revoked`, auditado) y el siguiente mensaje exige elegir otra identidad. «Finalizar reportes» cierra la sesión; cambio de auxiliar y expiración por inactividad se mantienen |
| D-045 | Tomada | **Identificación obligatoria**: sin identidad activa y autorizada ningún texto ni foto se vuelve observación (quedan pendientes y requieren atribución explícita); por API directa la persona debe estar disponible |
| D-046 | Tomada | **PIN: no se implementa ni exige en el demo.** Solo queda la arquitectura extensible (`identity_level`, `verify_pin_attempt` con bloqueo persistente, `SESSION_REQUIRE_PIN` rechazado) sin pantallas, campos ni pasos |
| D-047 | Tomada | El personal inicial (Pao, Danny, Julio, Iván, Pato) es un **archivo de datos** (`database/seed/staff.json`) cargado a PostgreSQL por la CLI; ningún nombre está en el código (hay una prueba que lo verifica). Ana y Luis permanecen como referencias históricas del archivo |

## Pendientes V del HLD §25 que bloquearán fases futuras (no la actual)

- H1: bot de Telegram (token vía @BotFather, lo crea el usuario), lista de auxiliares, ¿PIN o selección?
- H3: qué proveedores reales y qué acceso comercial existe; cuáles cadenas. No se asume ninguna API pública.
