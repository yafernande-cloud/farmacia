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
| D-039 | Tomada | **Guardias.** Una lectura de imagen `simulated` se rechaza fuera de la organización de simulación (adjunto `error`, sin observación ni necesidad, con auditoría); `telegram-poll` con `VISION_PROVIDER=simulated` exige que TODOS los chats activos sean de simulación; la CLI no tiene organización por defecto (`--org sim\|real`); las demos usan solo la organización de simulación, abren un ciclo nuevo en cada corrida y **ya no vacían tablas** |
| D-040 | Tomada | **Aislamiento**: toda consulta, deduplicación, catálogo, alias, auditoría y feed de eventos (`services/event_feed.py`, base del SSE de H1-C) se acota por `organization_id`; una prueba de esquema exige `organization_id` NOT NULL + FK en toda tabla de negocio. Una simulación jamás se fusiona con una necesidad real |
| D-041 | Pendiente (plan entregado) | Migración de datos existentes: **opción A «archivo»** (0 filas cambian de organización, 0 triggers desactivados, 0 autorías cambiadas). Ensayada en una copia |
| D-042 | Tomada | **Personal: tres conceptos separados.** *Pertenecer* (`active`), *disponibilidad para reportar* (`available` + motivo `vacation/sick_leave/day_off/other` + nota) y *sesión de Telegram activa* (deriva de `actor_session`). Nunca se elimina (trigger): se desactiva. Nombres únicos entre activos (sin distinguir mayúsculas). Un nombre cambiado no reasigna la autoría (va por id) |
| D-043 | Tomada | **Mantenedor** `/api/v1/staff` (listar completo, crear, renombrar, activar/desactivar, disponibilidad con motivo) exige el permiso `manage_staff` = rol `admin` (compras sin `admin` no administra; el HLD separa ambos). **Pao tiene `purchasing` + `admin` en el demo** (decisión de datos). No se puede desactivar a la última administradora. Cada cambio queda auditado con antes/después y emite evento |
| D-044 | Tomada | **Botones dinámicos**: `/start` y «Cambiar auxiliar» leen de PostgreSQL solo personas activas, disponibles y con rol que permita reportar. **Validación en el backend** en cada selección y en cada uso de una sesión: un botón antiguo no elige a quien ya no está disponible; si una persona deja de estar disponible o activa se **revocan sus sesiones** (`end_reason=revoked`, auditado) y el siguiente mensaje exige elegir otra identidad. «Finalizar reportes» cierra la sesión; cambio de auxiliar y expiración por inactividad se mantienen |
| D-045 | Tomada | **Identificación obligatoria**: sin identidad activa y autorizada ningún texto ni foto se vuelve observación (quedan pendientes y requieren atribución explícita); por API directa la persona debe estar disponible |
| D-046 | Tomada | **PIN: no se implementa ni exige en el demo.** Solo queda la arquitectura extensible (`identity_level`, `verify_pin_attempt` con bloqueo persistente, `SESSION_REQUIRE_PIN` rechazado) sin pantallas, campos ni pasos |
| D-047 | Tomada | El personal inicial (Pao, Danny, Julio, Iván, Pato) es un **archivo de datos** (`database/seed/staff.json`) cargado a PostgreSQL por la CLI; ningún nombre está en el código (hay una prueba que lo verifica). Ana y Luis permanecen como referencias históricas del archivo |

## Acceso desde múltiples cuentas de Telegram (2026-10-28)

Motivo: el mismo bot debe usarlo el teléfono compartido de la farmacia (varias auxiliares, identidad declarada) y los
teléfonos personales de Pao y Danny (identidad de la cuenta), además de personas futuras sin tocar código. Antes la
autorización era **por chat** (`channel_binding`): no existía el concepto de cuenta ni forma de revocar sin borrar.

| ID | Estado | Decisión |
|---|---|---|
| D-048 | Tomada | **La autorización es por cuenta de Telegram, no por chat.** `telegram_account` (único por proveedor + id externo) es la unidad autorizable; `channel_binding.account_id` dice qué cuenta usa ese chat. Dos modos: `personal` (cuenta de una persona, `app_user_id` obligatorio) y `shared_device` (teléfono compartido, sin persona fija). `channel_binding.mode` acompaña. La cuenta **no** es la persona: los chats de un teléfono compartido siguen eligiendo auxiliar como hasta ahora |
| D-049 | Reemplazada por D-057 | **Invitaciones de un solo uso.** Código de 20 caracteres en alfabeto sin ambigüedad (0/1/I/O excluidos; ~100 bits), agrupado con guiones solo para mostrarlo; en la base se guarda **solo SHA-256** y el texto se muestra una vez. Vigencia 5 min–7 días (24 h por defecto), un solo uso, revocable antes de usarse. Se generan **solo** con permiso `manage_accounts` (rol `admin`) por CLI o `POST /api/v1/telegram-accounts/invitations`; no hay comando público en el bot que las genere. Se canjean en **chat privado** con `/autorizar CÓDIGO` o `/start CÓDIGO`; código inválido, vencido, usado o revocado responde lo mismo, sin revelar cuál. El código nunca se registra en logs, auditoría, eventos ni mensajes |
| D-050 | Tomada | **La autorización se valida en el servidor, dos veces**: antes de persistir el evento entrante y otra vez dentro de la transacción que lo procesa (evita TOCTOU si se revoca en el intervalo). Los botones y el chat no autorizan nada por sí mismos. Un chat sin cuenta activa no registra ni consulta: recibe el aviso de no autorizado (con su ID para pedir acceso) |
| D-051 | Tomada | **Identidad por cuenta en teléfonos personales, declarada en el compartido.** En una cuenta `personal` la identidad es la persona ligada, sin selector: la sesión se crea/reutiliza con `identity_level='account'` y los registros quedan con `attribution_basis='authorized_account'`. Si la cuenta cambia de persona o se revoca, la sesión se invalida. En `shared_device` se mantiene exactamente el flujo actual (`declared`, selector, pendiente de atribución). `account_id` se guarda en sesiones, mensajes, observaciones y fotos; la autoría sigue siendo de la persona. La cuenta que envía puede no ser la persona que reporta: nunca se deduce una de la otra |
| D-052 | Reemplazada por D-059 | **Revocación sin borrado.** Revocar una cuenta la marca `revoked`, desactiva sus vínculos y termina sus sesiones activas (`end_reason='revoked'`); el historial, las observaciones y la auditoría quedan intactos. Volver a autorizar el mismo id externo **reutiliza** la fila (restricción única) y crea un vínculo nuevo. Una invitación ya usada no se puede revocar (409); sí se puede revocar una pendiente. En el bot, la cuenta revocada pasa a «no autorizado» |
| D-053 | Tomada | **`access_role` (`admin`/`operator`) es descriptivo y prepara la distinción administrativa/operativa; NO es un permiso de negocio.** Los permisos reales siguen en `app_user.roles` (`manage_accounts` solo para `admin`). No se inventan restricciones para Pao, Danny ni las auxiliares: qué puede hacer cada rol operativo se define cuando el negocio lo diga (pendiente explícito) |
| D-054 | Reemplazada por D-062 | **Migración `0007`, encadenada a `0005`.** El script `0006_separar_organizaciones.sql` es de datos y no es una revisión Alembic; se documenta en la propia migración. **Backfill conservador**: solo vínculos privados activos de `real_test`/`production`/`legacy_mixed` se ligan a una cuenta `shared_device` (chat privado ⇒ `from.id = chat.id`), preservando el flujo que ya operaba. **Simulación y grupos no se tocan**: el simulador envía desde otra cuenta ficticia y una cuenta real jamás entra a una organización simulada. Filas históricas conservan `account_id = NULL`. No borra ni reinicializa tablas. **No aplicada a `farmacia`** (sigue rigiendo la restricción sobre 0004/0005/0006) |
| D-055 | Reemplazada por D-063 | **Límites del canal.** El canje exige chat privado (en grupos no se puede saber con certeza quién escribe); una cuenta pertenece a una sola organización y un chat activo a una sola; una cuenta `personal` no puede elegir otra identidad (`/cambiar` se rechaza). Los grupos siguen el camino legado y no redimen invitaciones en esta fase |

## Solicitud, aprobación y bloqueo de accesos de Telegram (2026-10-28)

Motivo: las altas, autorizaciones y bloqueos deben gestionarse desde el futuro frontend de Pao (Configuración →
Accesos Telegram), no con invitaciones creadas por CLI ni canjeadas con `/autorizar`. El bot solo **recibe** la
solicitud y entrega un código temporal; conocer el código **no** autoriza nada. Reemplaza el flujo de invitaciones
(D-049, D-052, D-054 y D-055) sin borrar esas filas del registro.

| ID | Estado | Decisión |
|---|---|---|
| D-056 | Tomada | **Solicitud desde el bot, sin acceso operativo.** Un chat privado desconocido puede pedir acceso con `/solicitar` (y `/start`, que solo explica). La solicitud (`telegram_access_request`, estado `pending`) **no crea cuenta ni vínculo** y no habilita ninguna función: sin identidad vigente no se registra ni se consulta nada. Si el texto enviado tiene forma de código de acceso, el bot **no lo persiste** (ni en `inbound_event`, `channel_message`, outbox ni auditoría) y pide compartirlo con la administradora. Los grupos no solicitan |
| D-057 | Tomada | **Código temporal.** 20 caracteres de un alfabeto de 32 símbolos sin ambigüedad (0/1/I/O excluidos; ~100 bits), mostrado en grupos de 4. Se guarda **solo el SHA-256** normalizado. **Vence a los 30 minutos**, es de un solo uso y tiene límites: 5 intentos por código y 10 por administrador cada 15 minutos. **Una sola solicitud pendiente por cuenta de Telegram** (índice único parcial en la base); un `update_id` nuevo rota el código y el anterior deja de servir; el mismo `update_id` es idempotente y no genera otro. El código se entrega **solo** en la respuesta directa del bot y nunca se persiste, audita, registra en logs, expone en URLs ni devuelve por la API. Los intentos fallidos se registran como hash en `telegram_access_attempt` para que el límite sea real (no se pierde con el rollback) |
| D-058 | Tomada | **La aprobación se hace en el frontend/API, con autenticación fuerte.** Pao busca el código (`POST /telegram-access-requests/lookup`) y ve la solicitud; antes de aprobar define **etiqueta**, **modo** (`personal`/`shared_device`), **persona asociada** cuando corresponda y **rol de cuenta** (`admin`/`operator`). Aprobar exige `manage_accounts` (rol `admin`) y `StrongActor` (ver D-060), valida que la solicitud siga pendiente y vigente, que en modo `personal` la persona exista y esté activa (la disponibilidad se valida al reportar, no al autorizar), que en `shared_device` no se asocie persona, y que la cuenta no esté bloqueada; recién entonces crea/reutiliza la cuenta, crea el vínculo y notifica por Telegram **sin código**. Conocer el código no basta: sin sesión administrativa válida no se aprueba nada |
| D-059 | Tomada | **Bloqueo y rehabilitación sin pérdida de historial (reemplaza D-052).** Bloquear (`POST /telegram-accounts/{id}/block`) exige `StrongActor` + `manage_accounts`, marca `status='blocked'` con `blocked_at/by/reason`, desactiva los vínculos y cierra las sesiones activas; **no borra** cuentas, solicitudes, observaciones ni auditoría. Rehabilitar (`/reactivate`) reutiliza la **misma fila** de cuenta, recrea sus vínculos privados y deja `reactivated_at/by`; el historial completo se consulta en `/telegram-accounts/{id}/history`. Una cuenta bloqueada puede volver a solicitar acceso y la aprobación reutiliza su fila |
| D-060 | Tomada | **Autenticación fuerte para administrar accesos.** En organizaciones reales (`real_test`/`production`/`legacy_mixed`) las rutas de accesos exigen **JWT de Supabase Auth** validado en el servidor (JWKS, `iss`, `aud`, `exp`, `role=authenticated`) y resuelven el usuario por `app_user.auth_subject`; la cabecera de demostración `X-Demo-User-Id` se **rechaza** fuera de la organización de simulación (`STRONG_AUTH_REQUIRED`). No se acepta un UUID de administrador enviado por el cliente. Los permisos siguen saliendo de `app_user.roles` en PostgreSQL; `access_role` sigue siendo descriptivo (D-053) |
| D-061 | Tomada | **Organización de acceso explícita por entorno.** El acceso de Telegram se resuelve contra `TELEGRAM_ACCESS_ORGANIZATION_ID`, **sin valor por defecto**: si falta, el bot responde honestamente que no hay acceso configurado y no crea solicitudes. Durante las pruebas se usa `real_test`; `production` se configurará después. Nunca se usa la organización real por defecto ni se mezclan simulaciones: las pruebas automatizadas siguen en `simulation` |
| D-062 | Tomada | **Migración `0007` reescrita (reemplaza D-054).** No estaba aplicada, así que se rehace en el mismo número (no se crea `0008`): `telegram_access_request`, `telegram_access_attempt` y la nueva `telegram_account` (`origin` = `backfill`/`access_request`/`manual`, `created_from_request_id`, `blocked_*`, `reactivated_*`), más `account_id` en vínculos, sesiones, mensajes y observaciones. La FK circular solicitud↔cuenta se resuelve creando tablas y agregando la FK después (`use_alter=True`). **Backfill conservador**: solo vínculos privados activos de organizaciones reales se ligan a una cuenta `shared_device`; `simulation` y grupos no se tocan; las filas históricas conservan `account_id = NULL`. Downgrade elimina lo nuevo y restaura las restricciones. **Aplicada a `farmacia` el 2026-10-28** con respaldo previo verificado (`pg_dump` fuera del repositorio) y conteos sin cambios; `alembic current` = `0007` |
| D-063 | Tomada | **Se retiran las invitaciones y el canje (reemplaza D-049 y D-055).** No hay `/autorizar`, no hay comando de bot que cree códigos y la CLI no emite códigos (solo bootstrap de desarrollo, sin organización por defecto). El bot solo solicita; la aprobación, el rechazo, el bloqueo y la rehabilitación viven en la API administrativa. Se mantienen los límites del canal: solicitud solo en chat privado, una cuenta por organización, un chat activo por organización y una cuenta `personal` sin selector de auxiliar. El teléfono compartido conserva el selector, las sesiones aisladas y la trazabilidad de auxiliares |

### Autenticación web con Supabase Auth (S0; proyecto creado por el usuario el 2026-10-29)

Estas decisiones no activan Supabase en la aplicación: fijan cómo se enlazará la identidad. El validador JWT y el
enlace por `auth_subject` ya están implementados y probados. El proyecto `faltantes-farmacia` existe (creado por el
usuario, con Google OAuth y el JWKS público verificado compatible); **una identidad está enlazada** —la administradora
de pruebas «Yarella Fernández» en `real_test`, creada con `create-admin` y enlazada con `link-auth-subject` el
2026-10-29, verificada con Google y con correo/contraseña—, `AUTH_MODE` sigue en `demo_header` y Pao permanece sin
enlace (D-067).

| ID | Estado | Decisión |
|---|---|---|
| D-064 | Tomada | **Una sola identidad interna por persona; Google y correo/contraseña son identidades del MISMO usuario de Supabase.** El backend resuelve la persona únicamente por el `sub` del JWT contra `app_user.auth_subject` (único, opcional). Google y contraseña deben producir **el mismo `sub`**; la verificación es **obligatoria antes de habilitar permisos** (`GET /api/v1/me` con cada método debe devolver el mismo `id`, `roles` y organización). Si Supabase crea dos usuarios, **no se enlaza el segundo**: ese `sub` recibe 403 `USER_NOT_PROVISIONED`. No se duplican permisos, filas `app_user` ni cuentas internas. La vinculación de identidades ocurre en Supabase (correo verificado), no en nuestro código |
| D-065 | Tomada | **Enlace administrativo, explícito y auditado; nunca automático.** Vincular o desvincular `auth_subject` exige el permiso `manage_identities` (rol `admin`) y se hace **solo por CLI** en esta etapa (`link-auth-subject`, `unlink-auth-subject`, `list-auth-subjects`, con organización explícita y `--by` de un administrador). No hay endpoint HTTP que lo haga, no se crea personal ni usuarios de Supabase, no se envía correo y ningún JWT provoca un alta. Solo se enlaza a personas **activas** de la organización del administrador (las de otra organización responden `NOT_FOUND`, sin filtrar su existencia); no se reemplaza un enlace existente sin desvincular primero; desvincular **no borra** a la persona ni su historial. Cada cambio deja auditoría (`auth_subject.linked`/`auth_subject.unlinked`) y evento de outbox en la misma transacción. Un usuario válido de Supabase sin enlace recibe 403: registrarse no otorga ningún permiso |
| D-066 | Tomada | **Sin recuperación de contraseña ni registro público (requisito del usuario).** El producto no implementa "olvidé mi contraseña", enlaces de recuperación, formularios equivalentes ni auto-registro; el frontend solo ofrecerá Google y correo/contraseña. El alta de usuarios es manual y administrativa, y los permisos nunca salen de Supabase. **Limitación declarada:** Supabase no ofrece un interruptor que elimine el endpoint de recuperación cuando el proveedor Email está activo; el control compensatorio es que solo existen los usuarios creados a mano y que un correo recuperado, por sí solo, no concede roles. Si Pao pierde ambos accesos, la salida es operativa (restablecimiento desde el panel de Supabase por un administrador), no una función del producto |
| D-067 | Tomada (enlace pendiente) | **Pao en `real_test`; `simulation` y el archivo no se enlazan.** En `farmacia` existen hoy **tres** filas de Pao, todas activas y con `auth_subject = NULL`: `real_test` (`purchasing` + `admin`), `simulation` (`purchasing` + `admin`) y `legacy_mixed` (`purchasing`, organización ARCHIVO del 2026-10-28). Cuando el usuario lo autorice se enlazará **solo** la de `real_test`, con el `sub` verificado según D-064. `simulation` se mantiene sin enlazar porque las simulaciones siguen con `AUTH_MODE=demo_header`; el archivo no se enlaza nunca. No se modifica ningún registro de `farmacia` en esta etapa |
| D-068 | Tomada | **Firma asimétrica obligatoria.** El validador acepta RS256/ES256 contra el JWKS público (`/auth/v1/.well-known/jwks.json`) y exige `iss`, `aud`, `exp`, `iat`, `sub` y `role=authenticated`; los claims de rol/permiso se ignoran. Un proyecto Supabase que solo firme con el secreto heredado HS256 **no sirve**: hay que migrar a JWT Signing Keys asimétricas antes de activar `AUTH_MODE=supabase_jwt` |
| D-069 | Tomada | **El mantenedor de personal exige autenticación fuerte (extiende D-060).** Todas las rutas `/api/v1/staff` usan `StrongActor`: en `real_test`/`production`/`legacy_mixed` rechazan `X-Demo-User-Id` con 403 `STRONG_AUTH_REQUIRED` y exigen JWT de Supabase; en `simulation` el comportamiento de demo se conserva. Administrar personal es administrar quién puede reportar y quién no, con el mismo rigor que los accesos de Telegram |
| D-070 | Pendiente | **Identidad en varias organizaciones.** `auth_subject` es único en toda la base, así que una persona con fila en dos organizaciones no puede reutilizar el mismo `sub`. Hoy no bloquea (solo se enlaza `real_test`), pero al habilitar `production` habrá que decidir entre una tabla de identidades (`sub` → persona, con la organización resuelta por membresía/contexto) o mover la fila en el corte. Requiere autorización y, probablemente, migración |
| D-071 | Tomada (aplicada 2026-10-29) | **La administradora de pruebas es una identidad aparte, no Pao.** La cuenta de Google del usuario se prueba como **primera administradora** del sistema, con nombre visible «Yarella Fernández» y **solo el rol `admin`** (sin `purchasing`): no reporta faltantes, no aparece en el selector del bot y no hereda ni altera nada de Pao. Pao conserva sus tres filas intactas y tendrá su **propia** identidad más adelante. Creada con autorización explícita del usuario (2026-10-29) y su `sub` enlazado; Google y correo/contraseña resuelven la misma identidad (D-064) |
| D-072 | Tomada | **`create-admin` es bootstrap, no un mantenedor de administradoras.** La CLI `create-admin "<nombre>" --org sim\|real --confirm` (servicio `staff.bootstrap_admin`) crea la **primera** administradora cuando ninguna puede iniciar sesión todavía, con estas restricciones: roles fijos `["admin"]` (no se aceptan otros), permitido **solo en `simulation` y `real_test`** (`BOOTSTRAP_NOT_ALLOWED` en `production` y en el archivo `legacy_mixed`), se niega si ya existe una persona **activa con `auth_subject` enlazado** y rol `admin` (`ADMIN_ACCESS_EXISTS`), admite **una sola** administradora de bootstrap **activa** por organización detectada por auditoría (`BOOTSTRAP_ALREADY_DONE`) —si se desactiva, se permite recuperar el acceso—, respeta la unicidad de nombre y **no enlaza `auth_subject`**, no crea usuarios de Supabase ni envía correo. Se audita como acción del **operador** (`actor_id = NULL`, acción `staff.bootstrap_created`, evento de outbox `staff.updated`), nunca como una persona de la farmacia |

## Pendientes V del HLD §25 que bloquearán fases futuras (no la actual)

- H1: bot de Telegram (token vía @BotFather, lo crea el usuario), lista de auxiliares, ¿PIN o selección?
- H3: qué proveedores reales y qué acceso comercial existe; cuáles cadenas. No se asume ninguna API pública.

## Reordenamiento del roadmap: cotizador primero (2026-10-29)

Motivo: el usuario confirmó que el **cotizador de medicamentos** es el corazón original de Faltantes Farmacia y que el
registro de faltantes por Telegram se incorporó después. El HLD §28 D01 («Empezar por Telegram») convertía Telegram en
requisito bloqueante, contra la prioridad del negocio. Rige el usuario y la diferencia queda registrada aquí.

| ID | Estado | Decisión |
|---|---|---|
| D-073 | Tomada (2026-10-29) | **El cotizador es la prioridad principal y Telegram no bloquea su desarrollo.** El vertical del cotizador (catálogo comercial, fuentes, ofertas, comparador y búsqueda) se construye sin depender de H1-B.5 (bot real) ni de H1-C (SSE). Telegram permanece implementado y se verificará cuando el usuario cree el bot, en paralelo y sin condicionar las fases del cotizador. El cotizador **no reutiliza lógica de `services/demand.py`**: comparte organización, catálogo, auditoría y outbox, y nada más. Cubrir faltantes con ofertas sigue siendo una fase posterior (HLD H4). |
| D-074 | Tomada (2026-10-29) | **La pantalla principal de Lovable es el buscador y cotizador.** El dashboard de faltantes deja de ser la primera pantalla y pasa a sección secundaria. Difiere del HLD §3.3, que pone «Faltantes» como vista inicial; rige el usuario y se registra aquí. La pantalla muestra cada resultado con su origen, fecha y condiciones, y **no ejecuta compras** (`purchase_execution` sigue `blocked`). |
| D-075 | Tomada (2026-10-29) | **Búsqueda inicial síncrona sobre catálogo local**, con resultados progresivos y estado por fuente (HLD §19). El patrón `202` + `job_id` del HLD §14.3 se adopta **solo cuando existan conectores externos que lo justifiquen**; mientras el catálogo sea local, una búsqueda que exceda el tiempo objetivo responde con error tipado. Una fuente caída nunca se presenta como «sin ofertas» (AC17). |

Restricciones vigentes para estas fases: el trabajo del cotizador es **aditivo** (tablas, rutas y servicios nuevos); **no
modifica el comportamiento existente de Telegram ni de Supabase Auth**; la compra real sigue bloqueada; toda tabla nueva
lleva `organization_id`, `Decimal` para dinero y procedencia (`data_origin` / modo de fuente).

## Contrato del cotizador C0 (2026-10-29)

Contexto: el usuario aprobó C0 como **contrato integral** (búsqueda, ofertas, proveedores, comparación y fuentes) y
aclaró que el cotizador es una **herramienta interna usada solo por los jefes de la farmacia**. El contrato vive en
`docs/contracts/cotizador-v0.md`. **No se implementó código, modelo, migración ni cambio de base de datos.**

| ID | Estado | Decisión |
|---|---|---|
| D-078 | Tomada (2026-10-29) | **C0 es integral y se congela completo; la implementación se reparte en C1–C5.** El contrato define E1 búsqueda, E2 consulta de búsqueda, E3 detalle de artículo, E4 fuentes, E5 comparación, E6 proveedores, E7 administración de fuentes y E8 importación de listas de precios. C1 implementa el modelo, C2 conectores/importación, C3 el comparador, C4 la API de búsqueda y C5 Lovable. Las **cadenas** quedan definidas en el contrato pero su ingesta real sigue en C6. Ninguna operación queda sin fase asignada. |
| D-079 | Tomada (2026-10-29) | **El cotizador tiene un único rol autorizado: `admin`.** Los jefes de farmacia buscan, consultan ofertas y referencias, comparan, importan Excel y administran proveedores y fuentes con ese rol. **No se crea el rol `purchasing` ni permisos nuevos o diferenciados** para el cotizador, y **no se modifican los roles de otros módulos**. Se mantienen los mecanismos existentes: `Actor` resuelto en el servidor, organización desde `app_user`, `StrongActor` para organizaciones reales y `X-Demo-User-Id` solo en `simulation`. **Vigente con la enmienda de D-128 (2026-10-30): el cotizador admite `admin` o `purchasing` + `StrongActor`; `purchasing` ya existe en el código y la aprobación de una solicitud web lo asigna.** |
| D-080 | Tomada (2026-10-29) | **Granularidad y centro del resultado (K1 y K4).** Un artículo es una **presentación comercial** (`trade_item`): caja de 10 y de 20 son artículos distintos. El resultado de búsqueda se centra en el artículo y cuelga sus ofertas y referencias. El esquema definitivo de tablas quedó definido en D-076 (cerrada el 2026-10-29). |
| D-081 | Tomada (2026-10-29) | **Dinero e impuestos (K2 y K10).** Todo importe viaja como string decimal con moneda obligatoria y nunca como número flotante; el precio declara `basis` (por unidad comercial o por unidad base), `tax_included` y `discount_included` en tri-estado (`true`/`false`/`unknown`). No se asume CLP por configuración. |
| D-082 | Tomada (2026-10-29) | **Búsqueda síncrona (K3).** E1 responde `200` con el resultado; `202 + job_id` queda reservado para cuando existan conectores externos que lo justifiquen (D-075). Una búsqueda que excede el tiempo objetivo responde `503 SEARCH_TIMEOUT`, error tipado; no se devuelve un resultado parcial silencioso. |
| D-083 | Tomada (2026-10-29) | **Estados de fuente y fallo (K5 y K7).** Cada fuente informa `ok`, `stale`, `failed` o `not_configured`. Si todas las fuentes externas fallan pero la API funciona, la respuesta es `200` con `status=failed` y errores por fuente (AC17); `503` se reserva a fallo del subsistema o de la base. Una fuente no configurada no es una fuente vacía ni una fuente caída. |
| D-084 | Tomada (2026-10-29) | **Referencias de cadena separadas (K6).** `offers[]` y `reference_prices[]` son estructuras distintas. Una referencia de cadena nunca es comprable, nunca participa en totales ni en `best_eligible` y siempre muestra sus condiciones. El precio de venta de la cadena no es el costo de adquisición (HLD §9.3). |
| D-085 | Tomada (2026-10-29) | **Nombres y alcance (K9 y K11).** La API usa `/articles` para la presentación comercial y `/sources` para las fuentes; `trade_item` sigue siendo el nombre del modelo. Crédito, cupos y condiciones de pago **quedan fuera de v0**; solo se admiten condiciones conocidas como texto con fecha y fuente. |

K12 queda resuelta por D-078. K8 queda resuelta por D-079.

> D-084 nombra `best_eligible` al ganador de la comparación. Ese nombre se reemplaza por `lowest_verified_total`
> en D-087; la regla de D-084 (la cadena nunca participa) no cambia.

## Revisión del contrato C0: correcciones y criterios comerciales (2026-10-29)

Contexto: revisión del contrato en seis puntos (regla del ganador, suficiencia de estructuras para calcular,
esquemas, verificación real de `StrongActor`, alcance congelable de E8 y consistencia interna), seguida de la
capacidad de **criterios comerciales** que pidió el usuario (Pao evalúa resultados y registra criterios con autor y
fecha). Se corrigieron B1–B11 en `docs/contracts/cotizador-v0.md`. **No se implementó código, modelo, migración ni
cambio de base de datos; el contrato no se congeló en esa revisión** (la aprobación y el congelamiento llegaron
después: D-093). Los ajustes de cierre pedidos antes de aprobar C0 —niveles de
resultado, cantidad explicada, trazabilidad nota → criterio, concurrencia, alcance del ámbito y creación directa
consciente— se registran en **D-092**.

| ID | Estado | Decisión |
|---|---|---|
| D-086 | Tomada (2026-10-29) | **Autorización ADMIN verificada y semántica real de `StrongActor` (B1).** Todas las operaciones E1–E9 exigen **dos** comprobaciones independientes: (a) rol `admin` verificado en el servicio mediante un chequeo de rol, **sin crear `Permission` nuevos ni tocar `_GRANTS`** —la matriz actual no tiene un permiso exclusivo de `admin` semánticamente apto: `VIEW_LIST` lo tienen los cuatro roles y `MANAGE_*` son de personal, cuentas e identidades—; y (b) `StrongActor`. Se documenta lo que `StrongActor` **realmente** hace hoy (`backend/app/api/deps.py:83-98`): rechaza `X-Demo-User-Id` fuera de la organización `simulation` y es **no-op** con `AUTH_MODE=supabase_jwt`; **no es MFA ni re-autenticación**. La autenticación fuerte para aprobar o comprar sigue en H5. No se agregan roles ni permisos. |
| D-087 | Tomada (2026-10-29) | **Menor total verificado acotado y cota inferior (B2 y B3).** `best_eligible` se reemplaza por `lowest_verified_total`, con `is_global_claim=false`, `claim=lowest_verified_total_among_evaluated` y alcance explícito (proveedores y fuentes evaluados, fecha). Un proveedor con cobertura parcial **nunca** tiene `total`; el ganador se elige solo entre proveedores completos y elegibles. Los proveedores incompletos exponen `lower_bound` **nullable** con `bound_status` (`valid`/`not_boundable`) y `bound_invalid_reasons[]`, bajo las condiciones LB-1…LB-8 del contrato: una sola moneda; línea resoluble; cantidad determinada; impuesto desconocido tratado como incluido; ningún descuento de aplicación desconocida; **si el mínimo o el múltiplo son desconocidos, ninguna cota es válida cuando existe descuento por umbral** (podría activarse y bajar el total); descuentos restados solo si están cumplidos y sin doble cómputo; la cota es sobre el snapshot observado. Los proveedores con cota válida menor al ganador van a `could_be_beaten_by`; los que no admiten cota van a `unknown_relation`, **nunca** a `could_be_beaten_by`. AC19 se cumple porque no se afirma un mínimo global y no se aprueba nada. |
| D-088 | Tomada (2026-10-29) | **Aritmética tipada y determinista (B4).** El contrato fija las fórmulas: `base = min ? max(requested, min) : requested`, `purchased = ceil(base/mult)*mult`, `surplus = purchased − requested`, con `minimum`/`multiple` como `{known, quantity}` —**su ausencia es desconocido, no 0 ni 1**—; el **stock se compara contra `purchased`**, no contra lo pedido. `subtotal = Σ line_after_discounts`; `taxes_added` se suma solo si el precio no incluye impuesto y la tasa es conocida; `total = subtotal + taxes_added + cargos + despacho − descuentos de pedido`. Los descuentos solo se aplican con `applicability=always` o `min_quantity` cumplido y `discount_included ≠ true` (evita doble resta); `unknown` con descuentos aplicables ⇒ parcial. El despacho usa un `scheme` tipado (`free_over_threshold` evalúa sobre `shipping_base` = subtotal + impuestos + cargos − descuentos de pedido). Moneda única obligatoria. Cantidades con `ROUND_CEILING`, montos internos con 4 decimales y `ROUND_HALF_UP` solo al presentar. Un **mínimo por monto** queda fuera de v0 y produce `needs_review` con `order_minimum_unmodelled`; el texto (`condition_text`, `notes`, `known_conditions`) **nunca** se interpreta para calcular. |
| D-089 | Tomada (2026-10-29) | **Vocabulario de cantidades (B5).** Las cantidades del cotizador usan el enum existente `QuantityUnit` (`box`/`package`/`unit`/`bottle`, `backend/app/domain/quantities.py`), con `value` entero > 0 y `original_text` conservado; se normaliza solo para comparar. El contrato anterior usaba `"unit":"caja"`, un cuarto vocabulario inexistente. Una unidad nueva exige fila en `domain/units.py`/`domain/products.py` y su prueba (D-015 a D-024). |
| D-090 | Tomada (2026-10-29) | **Alcance congelable de E8 (B10).** Se congela el **sobre**: multipart, `supplier_id` y **`source_id` obligatorios** (no se crea ninguna fuente de forma implícita), `Idempotency-Key` 8–128 con replay por hash, límites de 10 MB y 20.000 filas, respuesta **200** con `status=completed\|completed_with_errors\|failed`, 422 `IMPORT_FORMAT_INVALID` solo si el archivo es ilegible, y códigos de fila cerrados (`MISSING_REQUIRED_FIELD`, `INVALID_PRICE`, `INVALID_CURRENCY`, `INVALID_BASIS`, `INVALID_QUANTITY`, `UNKNOWN_UNIT`, `DUPLICATE_ROW`, `UNMAPPED_PRODUCT`, `UNMODELLED_CONDITION`). Las **columnas, unidades y mapeo siguen en D-077**; `UNMAPPED_PRODUCT` es rechazo en v0 (no se inventa identidad de artículo). |
| D-091 | Tomada (2026-10-29) | **Criterios comerciales de Pao: registro y anotación en v0; aplicación y recomendación en H4.** Extiende D-078 de E1–E8 a **E1–E9** sin reemplazarla. Tres planos separados: **cálculo** (determinista), **criterios** (registrados por ADMIN) y **recomendación** (futura, H4). Regla de no interferencia: un criterio **nunca** modifica precios, cantidades, totales, `lower_bound`, elegibilidad ni visibilidad, ni oculta ofertas; solo agrega anotaciones visibles junto a la oferta, el artículo o el proveedor. Un solo recurso `/api/v1/commercial-criteria` con `kind`: **`note`** (observación guardada, con autor, fecha, ámbito, edición y desactivación; efecto `note_only`; **no se convierte en regla jamás por sí sola**) o **`criterion`** (regla confirmada explícitamente, con `effect` ∈ `prefer`/`deprioritize`/`do_not_recommend`/`requires_review`, `origin=explicit`, `reason_text`, `confirmed_by/at` y vigencia). **No existe el estado `proposed`**: lo no confirmado es una nota, y `status` solo es `active`/`inactive`; «vigente» es una propiedad derivada (`active` + dentro de `validity`), no un estado. Confirmar una nota **crea un criterio nuevo** (`source.note_criterion_id`) y desactiva la nota con motivo; editar un criterio confirmado sube `version` con `If-Match`. Ámbito: `organization`/`ingredient`/`article`/`supplier`/`supplier_article` (`category` reservado); condición `always`/`min_quantity`/`max_price`/`date_window`, y `max_price` exige moneda y base (HLD §13.4). Toda alta, edición, confirmación y desactivación se audita con actor, antes/después y evento de outbox en la misma transacción. «Siempre recomendar» es preferencia comercial, **nunca** excepción a validaciones técnicas ni autorización de compra (compra bloqueada). Origen en HLD §13.1–§13.3, §6.2 (`memory_fact`, `comparison_run` con versión de reglas), §14.1 (`/rules`, `/rule-proposals/{id}/confirm`) y AC29–AC31; los nombres del HLD se mapean a `commercial-criteria` con estado explícito. Solo rol `admin` + `StrongActor`. Sin IA: `origin=inferred` queda como nota y no se aplica en v0. |
| D-092 | Tomada (2026-10-29) | **Ajustes de cierre de C0 (precisa D-087 y D-091; no los reemplaza).** (1) **Tres niveles de resultado sin contradicción con C0-06/AC19:** la elegibilidad (`eligible`/`needs_review`/`ineligible` + causas) es **por candidato** (oferta × línea); un **proveedor completo verificable** exige todas sus líneas cubiertas por candidatos `eligible` y todos los costos determinados, y es el único que tiene `total` y puede ganar; un **proveedor parcial** aparece en `supplier_totals` con `completeness=partial`, nunca tiene `total`, nunca gana y solo puede figurar en `could_be_beaten_by`/`unknown_relation`. `SupplierTotalOut` **no** lleva `eligibility`; estar en `supplier_totals` no significa ser elegible. Los `discarded_suppliers` son los que no tienen ninguna línea cubierta por causa dura. (2) **Cantidad explícita:** `ComparisonCandidateOut` expone `minimum`, `multiple`, `base`, `purchased`, `surplus` y `adjustment` (`none`/`minimum`/`multiple`/`minimum_and_multiple`), de modo que todo cambio de cantidad respecto de lo pedido queda explicado por una condición del proveedor y coincide con la fórmula de §2.4; el ejemplo del contrato usa 20 pedidas, mínimo 20, múltiplo 7 → 21 compradas. (3) **Trazabilidad nota → criterio:** confirmar **no borra ni muta** la nota; el criterio guarda `source.note_criterion_id` + `source.note_version`; la nota conserva en `deactivation_reason` la referencia; **un solo criterio derivado por nota** (restricción única) y un segundo `confirm` responde 409 `CRITERION_NOT_ACTIVE`; **nunca hay dos anotaciones activas del mismo contenido**: una nota y su criterio derivado no pueden estar ambos `active` (409 `CRITERION_NOTE_SUPERSEDED`), y desactivar el criterio no reactiva la nota automáticamente. (4) **Concurrencia:** `confirm`, `deactivate`, `reactivate` y `PATCH` exigen `If-Match` (428 si falta, 409 `VERSION_CONFLICT` si no coincide), suben `version` en 1 y son condicionales al estado y la versión esperados dentro de la transacción; dos solicitudes simultáneas no duplican transición ni auditoría. (5) **Alcance exacto:** `organization` (todo), `ingredient` (solo artículos con principio coincidente y procedencia `explicit`/`confirmed`), `article` (= presentación comercial, D-080; no hay nivel «presentación» aparte), `supplier` y `supplier_article`; **sin herencia** entre ámbitos, `condition` se evalúa además (AND) y nunca amplía, `category` se rechaza en v0. (6) **Creación directa consciente:** `POST` con `kind=criterion` exige `"confirm": true` en el cuerpo además de efecto, ámbito, motivo y condición; sin él, 422 `CRITERION_INCOMPLETE`; el servidor fija `origin=explicit`, `confirmed_by/at` y `source.kind` (`manual`, o `evaluation` si el cuerpo indica comparación u oferta), y el cliente no puede enviar estado ni confirmación. |

**Consecuencias en el contrato:** roles sin cambios (ADMIN único, D-079); tabla de errores cerrada con
`INVALID_QUANTITY`, `CURRENCY_MISMATCH`, `CRITERION_INCOMPLETE`, `CRITERION_EFFECT_NOT_ALLOWED`,
`CRITERION_NOT_NOTE`, `CRITERION_NOT_ACTIVE` y `CRITERION_NOTE_SUPERSEDED`; criterios de aceptación C0-01 a **C0-19**
(con C0-06, C0-07 y C0-17 precisados por D-092); E9 con fase C4 (modelo en C1, anotación en C3) y
`recommendation: null` + `recommendation_status="not_evaluated"` reservados para H4.

**Quedaban abiertas en esa revisión:** D-076 (esquema definitivo del catálogo comercial y migración 0008, **no
autorizada**), cerrada el 2026-10-29 en el apartado «Diseño del modelo de datos del cotizador (C1)», y D-077
(contrato y formato de la planilla Excel; E8 define solo el transporte).

## Aprobación final del contrato y cierre de C0 (2026-10-29)

| ID | Estado | Decisión |
|---|---|---|
| D-093 | Tomada (2026-10-29) | **El contrato del cotizador v0 queda aprobado y congelado y la fase C0 se cierra.** El usuario autorizó formalmente el cierre de C0 el 2026-10-29. `docs/contracts/cotizador-v0.md` pasa a **APROBADO Y CONGELADO** y los criterios **C0-01 a C0-19** se dan por cumplidos por revisión del documento; **C0-13 queda cumplido** con esta aprobación. El congelamiento alcanza al contrato completo (E1–E9, tres planos, esquemas, errores, invariantes y AC), con las precisiones de D-086 a D-092 incorporadas y verificadas entre contrato, registro de decisiones y roadmap. **No se modificó código, modelo, endpoint, base de datos, migración ni OpenAPI; la migración 0008 no se ejecutó y C1 no se inició.** Consecuencias: (a) Lovable y el backend implementan el contrato tal cual; cualquier cambio de alcance, esquema o semántica exige **decisión nueva** (D-094 en adelante) y versión nueva del documento; (b) las decisiones comerciales y de cálculo siguen sin poder alterarse por criterios registrados (§1.1 y D-091); (c) D-076 y D-077 quedaban abiertas sin bloquear C1–C4 (D-076 se cierra el mismo 2026-10-29 en el apartado siguiente; |
D-077 sigue abierta); (d) el siguiente paso es **C1** (modelo de datos), que requiere **autorización explícita de migración**; (e) la compra real sigue `blocked` y el LLM no decide dinero, cantidades ni permisos. |

**Estado documental resultante:** contrato congelado (`docs/contracts/cotizador-v0.md`), C0 cerrada con C0-01 a C0-19
verificados (`docs/ROADMAP.md`) y decisiones D-078 a D-093 registradas en este archivo. Los tres documentos declaran
la misma fecha (2026-10-29) y el mismo alcance.

## Diseño del modelo de datos del cotizador (C1) y cierre de D-076 (2026-10-29)

Contexto: tras la propuesta de C1, el usuario aprobó la **alternativa A** de D-094 (catálogo neutral al tipo de
producto), la **separación de migraciones** 0008/0009/0010 y **`catalog-load`** como carga inicial técnica.
**No se implementó código, modelo, endpoint, migración ni cambio de base de datos; la migración 0008 no se escribió ni
se ejecutó y el contrato C0 no se modificó.** Este apartado cierra D-076 y deja el diseño listo para autorizar la 0008
por separado. **Adenda del mismo día:** el usuario aprobó incorporar `chain_product` —la ficha publicada por cada
cadena— con lo que la 0008 pasa a **10 tablas** (§ «Adenda a D-076 — ficha publicada por cadena»); el contrato C0 y el
alcance de C1 no cambian.

| ID | Estado | Decisión |
|---|---|---|
| D-076 | Tomada (2026-10-29) | **Esquema del catálogo comercial y del cotizador: definido y cerrado.** El artículo del cotizador es `trade_item` (presentación comercial, D-080) y es la tabla canónica del catálogo; `pharma_identity` se enlaza **opcionalmente** cuando el producto es medicamento. El esquema completo se reparte en tres migraciones por fase (§ «Diseño aprobado»). Se respetan las convenciones del repo: `organization_id` en todas las tablas, FK compuestas `(x_id, organization_id)`, `version` para `If-Match`, dinero `NUMERIC(18,4)` con moneda `CHAR(3)`, fechas `TIMESTAMPTZ` y auditoría/outbox existentes (`services/events.emit`). **No se tocan** `demand_item`, `demand_observation`, `pharma_identity`, `commercial_reference` ni `product_alias`. Con la **adenda del 2026-10-29** (§ «Adenda a D-076 — ficha publicada por cadena») la migración 0008 crea **10** tablas: se añade `chain_product`, la ficha que publica cada cadena, separada del artículo canónico. |
| D-094 | Tomada (2026-10-29) | **Catálogo neutral al tipo de producto (alternativa A).** `trade_item.product_kind` admite `medicine`, `medical_device`, `cosmetic`, `personal_care`, `veterinary`, `food_supplement`, `other`; es **nullable** y `NULL` significa «no clasificado» (nunca se infiere). Nace `NULL` y solo lo fija una persona. Ningún atributo farmacéutico es obligatorio: `ingredient`, `strengths`, `form`, `release`, `route`, `sanitary_registration` y `pharma_identity_id` pueden faltar, de modo que un cepillo, un cosmético o un producto veterinario se catalogan sin atributos que no les corresponden. `product_kind` **no es** la `category` rechazada por D-091: no segmenta criterios, precios ni ámbitos; solo indica qué atributos aplican. En esta variante **no se expone** en E1 ni E3 y **el contrato C0 no se modifica**; exponerlo (filtro en E1, campo en E3) exigiría una decisión nueva y una versión nueva del contrato. Los criterios con `target.level=ingredient` solo alcanzan artículos con principio de procedencia `explicit` o `confirmed`; para productos sin principio, el ámbito útil es `article`, `supplier` o `organization`. |
| D-095 | Diferida (2026-10-29) | **Productos no farmacéuticos en el módulo de faltantes.** Hoy `demand_item.ingredient` es `NOT NULL` (y lo mismo en `pharma_identity` y `product_alias`), así que un cepillo no puede reportarse como faltante. Sigue incompatibilidad conocida y **explícitamente diferida**: no se amplía ese módulo ni se modifica su esquema. Requiere decisión propia antes de tocarlo. |
| D-096 | Diferida (2026-10-29) | **Unificación de catálogos.** `commercial_reference` (puente histórico del lado de faltantes) y `trade_item` (canónico del cotizador) coexisten sin fusión automática. `commercial_reference` **no se toca** y el cotizador no la lee. Fusionarlas o retirar una queda para una decisión posterior. |
| D-097 | Pendiente (2026-10-29) | **Administración cotidiana del catálogo.** `catalog-load` es un mecanismo **técnico de carga inicial**, no la interfaz habitual: Pao necesitará dar de alta y editar artículos, mappings y alias desde la interfaz, y hoy no existe operación para eso en E1–E9. Se registra como necesidad pendiente y **no se agrega endpoint ni se modifica C0** por ahora; resolverlo exige decidir entre un comando administrativo, una operación nueva (p. ej. E10 en una versión v0.1 del contrato) o ambas. |

### Diseño aprobado (resumen)

**Migraciones y dependencias** (cada una se crea cuando su fase la consume; 0009 y 0010 heredan de 0008):

| Migración | Fase | Tablas |
|---|---|---|
| **0008** catálogo y ofertas | C1/C2 | `trade_item`, `trade_item_alias`, `supplier`, `source`, `supplier_product_mapping`, `shipping_terms_snapshot`, `offer_snapshot`, `chain_product`, `reference_price`, `import_run` (**10**) |
| **0009** comparaciones y criterios | C3 | `comparison_run`, `comparison_line`, `comparison_candidate`, `comparison_supplier_total`, `commercial_criterion` (5) |
| **0010** búsquedas | C4 | `search_run`, `search_source_result`, `search_result` (3) |

**Reutilización:** `organization` (aislamiento y moneda), `app_user` (actor y autores), `audit_event`/`outbox_event`
(toda alta, edición, confirmación y desactivación, en la misma transacción), `pharma_identity` (enlace opcional) y el
patrón de cursor de `services/queries.py`.

**Reglas de integridad acordadas:**

- **Entre organizaciones:** `UniqueConstraint(id, organization_id)` en cada padre y FK compuestas. Claves auxiliares que
  evitan incoherencias que hoy solo cazaría el servicio: `source(id, supplier_id, organization_id)` para que una oferta
  no declare un proveedor distinto del de su fuente, y `comparison_line(id, comparison_run_id, organization_id)` para
  que un candidato no pertenezca a otra corrida.
- **Inmutabilidad:** triggers que bloquean `UPDATE`/`DELETE` en `offer_snapshot`, `shipping_terms_snapshot`,
  `chain_product`, `reference_price`, `comparison_run`, `comparison_line`, `comparison_candidate` y
  `comparison_supplier_total`; `import_run` se inserta una sola vez con su estado final. Una corrección se hace con un
  snapshot nuevo, no editando. **Única excepción:** en `chain_product` el trigger permite modificar solo las columnas
  del vínculo con el catálogo (`trade_item_id`, `match_status`, `version` y la confirmación), y solo por acción
  explícita auditada; el contenido publicado y la foto nunca se reescriben.
- **Procedencia:** trigger `enforce_data_origin` en `trade_item`, `supplier_product_mapping`, `offer_snapshot`,
  `shipping_terms_snapshot`, `chain_product` y `reference_price` (en `simulation` solo `simulated`).
- **Criterios:** `source_note_criterion_id` con índice único parcial (un criterio derivado por nota), columna interna
  `superseded_by_criterion_id` además del texto de `deactivation_reason`, y trigger que impide **nota y criterio
  derivado activos a la vez** (`CRITERION_NOTE_SUPERSEDED`). Checks: `note ⇒ effect=note_only y sin confirmación`;
  `criterion ⇒ effect válido, origin=explicit, reason_text no vacío y confirmed_by/at`; `inactive ⇒ motivo y fechas`;
  XOR de `target_level`; `version >= 1`.
- **Comparaciones históricas:** los números se persisten como `NUMERIC(18,4)` y **no se recalculan al leer**; la oferta
  referenciada es inmutable, de modo que el historial no cambia de significado; se guardan `computed_at`,
  `criteria_evaluated_at` y las anotaciones como snapshot. Invariantes en la base: `complete ⇔ total NOT NULL`
  (AC19), `is_global_claim = false`, y `lower_bound` presente si y solo si `bound_status=valid`.
- **Dinero y cantidades:** `NUMERIC`, nunca float; moneda `^[A-Z]{3}$`; `requested > 0`, `minimum >= 0`, `multiple >= 1`;
  `tax_included` y `discount_included` como booleano nullable, donde `NULL` = `unknown`.

**Carga inicial (`catalog-load`):** comando de `app/cli.py` con el patrón `_admin(--org, --by)` ya existente;
lee un archivo de **datos** JSONL con registros `article` y `mapping`; idempotente por `trade_item.identity_key` y
`(source_id, external_id)`; informa un resumen y admite `--dry-run`; escribe auditoría y outbox; respeta la procedencia
por tipo de organización. No crea artículos por inferencia, no sustituye E8 (que mantiene `UNMAPPED_PRODUCT`) y **no es
la administración cotidiana del catálogo** (D-097).

**Consideraciones registradas:**

1. **Criterios y notas sin persistencia real hasta la 0009.** Hasta que exista esa migración, E9 no tiene dónde guardar
   nada. El prototipo de Lovable **puede mostrarlos como demostración**, pero no debe presentarse como funcionalidad
   operativa ni como algo que Pao pueda usar; conforme a §2.5 y HLD §22, todo dato mostrado así se etiqueta como
   `fixture`/`simulated` y nunca se presenta como real.
2. **`catalog-load` no reemplaza la administración del catálogo.** Es carga inicial y recuperación; la necesidad de
   alta y edición diaria queda registrada como D-097, sin endpoint nuevo y sin cambiar C0.

### Adenda a D-076 — ficha publicada por cadena (`chain_product`, 2026-10-29)

Motivo: las tarjetas de cadenas externas del prototipo de Lovable deben mostrar, además del logo y el precio, la
**fotografía y la ficha descriptiva que publica cada cadena** («Losartan (B) 50 mg, 30 comprimidos recubiertos, de
Laboratorio Chile», con foto del envase, principio activo, forma farmacéutica y dosis). El usuario aprobó incorporar
`chain_product` al diseño de C1 **sin modificar el contrato C0 congelado ni ampliar el alcance de C1**.

**Dos planos separados:**

| Plano | Tabla | Qué guarda | Quién lo fija |
|---|---|---|---|
| Canónico | `trade_item` | La presentación comercial del catálogo (D-080) | Nosotros, con procedencia |
| Publicado | `chain_product` | La ficha tal como la publica una cadena | La cadena |

`reference_price` deja de colgar del artículo canónico: **`reference_price.chain_product_id` es obligatorio**. El vínculo
con `trade_item` vive solo en `chain_product.trade_item_id` (nullable), con `match_status`
(`exact`/`needs_review`/`unmapped`) y causas; **nunca** se crea por coincidencia de principio activo y dosis, y exige
procedencia `explicit` o `confirmed`.

**Campos de `chain_product`** (snapshot por contenido): `source_id` (cadena), `external_id`, `source_url`,
`published_name` (verbatim, «(B)» incluido), `description`, `laboratory_as_published`, `presentation_as_published`,
`published_attributes JSONB` (principio, dosis, forma y contenido **como se escribieron**), `image_url`, `image_hash`,
`image_object_key` (nullable, reservado), `content_hash`, `observed_at`, `data_origin` y `evidence`/`raw_payload`.
`reference_price` conserva precio normal y de promoción, moneda, condiciones, despacho informado, stock informado,
`source_url` del momento, `observed_at`, `evidence` y `data_origin`.

**Trazabilidad fotografía ↔ ficha ↔ precio:**

1. `reference_price` apunta a **una fila exacta** de `chain_product`, no a un artículo: foto y precio no pueden
   desincronizarse, aunque entre medio cambie la ficha publicada.
2. La ficha es **inmutable en su contenido**: si la cadena cambia foto, presentación o descripción, se inserta otra fila
   con distinto `content_hash`, y las observaciones de precio anteriores siguen apuntando a la ficha con la que se
   tomaron. Solo son mutables las columnas del vínculo con el catálogo, por acción explícita auditada (ver «Reglas de
   integridad»).
3. Clave auxiliar `chain_product(id, source_id, organization_id)` y FK compuesta
   `reference_price(chain_product_id, source_id, organization_id)`: el precio de una cadena no puede apuntar a la ficha
   de otra.
4. **Vigencia sin mutar el snapshot:** `first_seen_at` y `last_seen_at` **no se guardan como columnas mutables**, porque
   un `UPDATE` contradiría la inmutabilidad y reescribiría la historia. Se derivan por consulta como
   `MIN(observed_at)` y `MAX(observed_at)` agrupando por `(source_id, external_id)`; la vigencia de una ficha concreta se
   obtiene con el mismo agregado agrupando por `(source_id, external_id, content_hash)`. Si algún día hace falta un
   caché, será una tabla derivada decidida entonces, nunca una escritura sobre la ficha.

**Límite de la fotografía:** `image_url` e `image_hash` **conservan la referencia y permiten detectar cambios**, pero
**no garantizan la disponibilidad futura** de la imagen: la cadena puede retirarla, reemplazarla o bloquear el enlace, y
sin guardar los bytes el hash no se puede reverificar. La tarjeta debe tratarla como referencia de terceros, con un
reemplazo visible cuando no cargue, y **no** deducir de la imagen identidad, presentación ni precio. El **almacenamiento
propio** queda pendiente para **C6** (junto con las condiciones de uso de las imágenes) y exige decisión propia.

**Qué no cambia:** `offers[]` y `reference_prices[]` siguen separados (D-084); una referencia de cadena nunca es
comparable, nunca entra en totales ni en `lowest_verified_total`; E1/E3 no exponen `image_url`, `description` ni
`product_kind`; el contrato C0 congelado no se toca.

**Fuera de este diseño:** cambios en el módulo de faltantes (D-095), unificación de catálogos (D-096), endpoints nuevos,
roles o permisos, `pg_trgm`, `category`, conversión de moneda, migración o carga de datos, cualquier cambio al contrato
C0, la exposición de la foto y la ficha publicada en E3/E4, el almacenamiento propio de imágenes y la ingesta real de
cadenas (C6).

**Estado documental resultante:** D-076 cerrada con el esquema definido y con la adenda `chain_product` del 2026-10-29
(la 0008 crea **10** tablas); D-094 tomada; D-095 y D-096 diferidas; D-097 pendiente; D-077 sigue abierta. La migración
0008 quedaba **por autorizar** y no se escribió en ese momento; se aplicó el mismo día con autorización expresa
(ver «Aplicación de la migración 0008 a `farmacia`», D-098).

## Aplicación de la migración 0008 a `farmacia` (2026-10-29)

| ID | Estado | Decisión |
|---|---|---|
| D-098 | Tomada (2026-10-29) | **La migración 0008 se aplicó a `farmacia` y la fase C1 queda cerrada.** El usuario autorizó, por pasos y por separado, el respaldo (A1), el ensayo de subida (A2), el ensayo de reversión (A2-bis) y la aplicación (A3). Resultado: `alembic current` = **0008**, **32 tablas** en `public`, las **10** tablas de C1 vacías, **219** registros previos intactos y **ningún objeto de 0007 alterado**. No se creó ningún endpoint, conector, `catalog-load` ni dato de catálogo: C2 sigue sin empezar y el contrato C0 no se modificó. El respaldo y las bases de ensayo **no se eliminan** sin autorización nueva. |

**Procedimiento y evidencia (cada paso con autorización explícita del usuario):**

1. **A1 — respaldo y verificación de recuperabilidad.** `pg_dump -Fc` del estado 0007, guardado en `.backups/` y
   copiado **fuera del repositorio** (`~/respaldos-farmacia/`), con SHA-256 `b176e73d…52bdc0` verificado en ambas
   copias. `pg_restore --list` confirmó formato `CUSTOM` y las 22 tablas; la restauración en una base desechable
   devolvió la misma versión, las mismas tablas, los mismos conteos y la misma huella de contenido por tabla.
2. **A2 — ensayo de la subida.** Se clonó la copia anterior (`CREATE DATABASE … TEMPLATE`) en
   `farmacia_rehearsal_0008` y se aplicó `upgrade 0008`: **0008** con **32** tablas. Las 22 tablas originales
   conservaron sus 219 registros y su comportamiento (`audit_event` de solo agregar, `organization.kind` inmutable),
   las 14 comprobaciones de invariantes de la migración pasaron y el inventario de **948** objetos coincidió con el
   de una instalación desde cero.
3. **A2-bis — ensayo de la reversión.** `downgrade 0007` dejó **22** tablas, **559** objetos idénticos al 0007
   prístino, **219** registros intactos y **sin residuos** (ni tablas, ni tipos, ni funciones); `upgrade 0008`
   volvió a **948** objetos idénticos y a los mismos invariantes. La reversión queda validada.
4. **A3 — aplicación.** `alembic upgrade 0008` sobre `farmacia`, con guarda que abortaba si el destino no era
   exactamente `farmacia`, y con la API y el bot detenidos. Verificación **por consultas reales**, no por la salida
   de Alembic: `0008 (head)`, 32 tablas, 10 tablas nuevas presentes y vacías, conteos y huellas MD5 idénticos a la
   línea base (única diferencia: `alembic_version`), 9 funciones, 30 triggers, 306 restricciones, 120 índices y
   **559/559 objetos previos con nombre, tipo y definición sin cambios**.
5. **Comprobación funcional de la API.** Con autorización expresa, `TestClient` de Starlette **en proceso** (sin
   puerto, sin servidor y sin red externa) y **solo peticiones GET** contra `farmacia`: **27/27 casos** con el
   estado esperado —401 sin identidad, 403 por permiso faltante, 404/400 con el contrato de error, 200 en salud,
   catálogo, listas, fotos, canal y personal para `simulation`, y **403 `STRONG_AUTH_REQUIRED`** al usar
   `demo_header` sobre `real_test` (D-071/D-072)—. La instantánea de las 32 tablas (filas + MD5 por tabla) resultó
   **idéntica** antes y después: la comprobación no escribió nada. El bot **no** se probó en vivo: `telegram-check`
   y `telegram-poll` hacen llamadas externas o escriben en la base, así que su comportamiento sigue cubierto por
   los simuladores de la suite.

**Pruebas:** 365 pruebas en verde, entre ellas las 13 de `tests/test_migration_0008.py` y
`test_modelos_y_migracion_estan_sincronizados`, ejecutadas sobre una base de pruebas dedicada en 0008 (no sobre
`farmacia`). La compatibilidad del módulo de faltantes se comprobó con esa misma suite y con la ausencia de
cualquier FK o trigger nuevo sobre sus tablas (D-095/D-096 siguen diferidas y su esquema no se tocó).

**Límites explícitos:** la 0008 no crea endpoints, no carga datos, no toca `commercial_reference` ni el módulo de
faltantes, y no habilita la compra (sigue `blocked`). El primer uso del catálogo (C2) deberá declarar `data_origin`
coherente: `real_test` solo admite `captured` y `ai_derived`. `alembic` no imprime nada en `upgrade`/`downgrade`,
de modo que el éxito se verifica **siempre** por catálogo (`alembic current` + conteos), nunca por la ausencia de
salida.

## Pendiente de seguridad antes del despliegue (2026-10-29)

| ID | Estado | Decisión |
|---|---|---|
| D-099 | Pendiente (2026-10-29) | **`AUTH_MODE=demo_header` no debe permitir el acceso a datos reales en un entorno público.** La comprobación funcional de C1 confirmó el alcance real: con `demo_header` y una persona de `real_test`, la API **sí** responde **lecturas** de datos reales (`GET /api/v1/me` y `GET /api/v1/demand-items` devuelven 200 con datos de `real_test`); `StrongActor` solo bloquea las operaciones **administrativas** (`/staff`, cuentas y solicitudes de Telegram), que responden 403 `STRONG_AUTH_REQUIRED`. Es el comportamiento diseñado (D-069; D-086 ya documenta que `StrongActor` **no** es MFA ni re-autenticación), y la validación de arranque impide `demo_header` con `APP_ENV=staging\|production` (`app/core/config.py`), de modo que el riesgo se materializa solo si se publica un entorno con `APP_ENV=local` o `demo`. **Antes de cualquier despliegue accesible públicamente** hay que decidir e implementar una de estas dos salidas: que `demo_header` resuelva **solo** personas de la organización `simulation` y rechace cualquier otra con 403, o que el modo demo no exista fuera de `local`/`demo`. **No se implementa ahora**: no se modifica `app/api/deps.py`, la matriz de permisos ni la configuración; requiere autorización y decisión propia. |

## Diseño de C2 y cierre de D-077 (2026-10-29)

| ID | Estado | Decisión |
|---|---|---|
| D-100 | Tomada (2026-10-29) | **D-077 queda cerrada: contrato de entrada del catálogo propio y de las planillas de proveedor.** El usuario aprobó el diseño con cuatro precisiones (fila sin SKU no se inventa ni se vincula por nombre; importación parcial cuando la estructura es válida; contrato C0 congelado; criterios de perfiles por fuente, historial sin sobrescritura, moneda y base declaradas, IVA desconocido, sin mappings automáticos, productos no farmacéuticos sin atributos obligatorios y sin funcionalidades nuevas en Lovable). Se aceptan los Excel **originales** de cada proveedor con estructuras distintas: **no hay plantilla única** y **no hay interpretación con IA**. Los dos contratos de entrada (abajo) son la especificación de D-077; **no se modificó el contrato C0** para cerrarla. |
| D-101 | Tomada (2026-10-29) | **Catálogo propio por archivo de datos (`catalog-load`), no por planilla de proveedor.** El archivo JSONL trae registros `article` y `mapping`. `article`: **`identity_key`** (clave canónica, la escribe una persona, nunca se deriva del nombre) y **`display_name`** obligatorios; `product_kind`, `ingredient`, `laboratory`, `brand`, `gtin`, `sanitary_registration` y `attributes` opcionales. `product_kind` se admite `NULL` («no clasificado») y **ningún atributo farmacológico es obligatorio**: el catálogo propio incluye productos no farmacéuticos (reafirma D-094). `mapping`: `source_id` + `external_id` + artículo destino (+ `external_name`), insertado en `supplier_product_mapping` con `origin='explicit'` y `created_by` del operador. Idempotente por `identity_key` y por `(source_id, external_id)`; diferencias de contenido no se sobrescriben en silencio, se informan como conflicto. `--dry-run`, auditoría y outbox. **No crea ofertas, no lee planillas y no infiere atributos ni vínculos.** |
| D-102 | Tomada (2026-10-29) | **Perfil de importación reutilizable por fuente.** Un perfil por **fuente** (no por proveedor: un proveedor puede mandar varios formatos y cada uno es una fuente). Contiene `file_format` (`.xlsx` en v0), `sheet`, `header_row`, `columns` —papel → columna resuelta **por nombre de cabecera con lista de alias**, posición solo como respaldo—, `units` (vocabulario del archivo → `box`/`package`/`unit`/`bottle`, enum cerrado) y `options` (separadores decimal/miles, formato de fecha, y moneda o base de precio **declaradas explícitamente**). Se edita con `version` + `If-Match` como el resto del contrato y se reutiliza en cada importación de esa fuente. |
| D-103 | Tomada (2026-10-29) | **Validación en dos fases y procesamiento parcial con conteos fieles.** Fase 1, estructural: archivo legible, cabecera presente, todas las columnas del perfil resueltas y límites respetados (10 MB, 20.000 filas); si falla **no se escribe ninguna fila**. Fase 2, por fila: se clasifican todas las filas y solo después se escriben las aceptadas, con auditoría y outbox en la misma transacción. `status` = `completed` (todas aceptadas), `completed_with_errors` (estructura válida con rechazos) o `failed` (nada aceptado). `rows_total` cuenta **filas de datos no vacías**; `rows_accepted + rows_rejected = rows_total` y ambas se derivan de lo realmente escrito. Códigos de fila: los **ya cerrados** en D-090. Ninguna fila rechazada se descarta en silencio. |
| D-104 | Tomada (2026-10-29; **enmendada por D-110**) | **Una fila sin SKU no se inventa ni se vincula por nombre.** Sin código de proveedor → rechazo `MISSING_REQUIRED_FIELD`; con código pero sin mapping → `UNMAPPED_PRODUCT`; **nunca** se usa el nombre como identificador ni se busca un parecido. Para poder identificarlas después, la fila rechazada se conserva en `import_run.rejected_rows` con **lo mínimo para localizarla y entenderla**: número de fila, código, fuente, hash del archivo, SKU y nombre tal como vienen, y **solo** las celdas que causaron el rechazo (declaradas por el propio rechazo, nunca adivinadas después). No se copia la fila entera ni el archivo. La respuesta HTTP mantiene **exactamente** la forma congelada en C0 (`row`, `code`, `message`), con el texto identificador dentro de `message`; exponer el detalle estructurado en la API exigiría una versión nueva del contrato (D-108). |
| D-105 | Tomada (2026-10-29) | **Dinero declarado, nunca supuesto.** La moneda y la base del precio (`per_commercial_unit`/`per_base_unit`) vienen del archivo o de una **declaración explícita del perfil**; no hay valor por defecto silencioso, y sin ellas la fila se rechaza (`INVALID_CURRENCY`/`INVALID_BASIS`). `tax_included` y `discount_included` quedan `NULL` = desconocido cuando la planilla no lo dice, y ninguna importación los completa por convención (D-081). Importes con `Decimal`/`NUMERIC`, jamás float. |
| D-106 | Tomada (2026-10-29) | **E8 solo escribe ofertas de proveedor.** La importación crea únicamente `import_run` y `offer_snapshot`; **nunca** `trade_item`, `supplier_product_mapping`, `chain_product` ni `reference_price`, y no confirma identidad farmacéutica ni habilita compra. La fuente debe ser `kind='supplier'`: una `chain_reference` se rechaza antes de leer filas (la base también lo impide con `enforce_source_kind`). Los precios públicos de cadena y los costos de proveedor no se mezclan en ningún punto. |
| D-107 | Propuesta (2026-10-29) | **Esquema necesario para C2 — no aplicado.** (1) Tabla nueva **`import_profile`** (perfil por fuente, con `version`, `status`, `data_origin` y FK compuestas por organización): hoy no existe dónde guardar el mapeo y `source.capabilities` no sirve (es una lista de capacidades del conector, sin validación por columna). (2) En **`import_run`**: `profile_id` (FK compuesta, nullable), `profile_snapshot` (jsonb, el mapeo exacto usado, porque la importación es de solo agregar y debe poder reproducirse) y `rejected_rows` (jsonb, D-104). Serían **un solo cambio de esquema**, con migración y modelo sincronizados y el test de sincronía pasando. Si se aprueba, C2 toma el número **0009** y las migraciones previstas de C3 y C4 pasan a **0010** y **0011** (ninguna existe todavía; **corregido por D-110**: C2 tomó 0009 y 0010 quedó para el estado de revisión de la oferta, así que **C3 = 0011 y C4 = 0012**; luego **D-111** tomó 0011 para la idempotencia de E8, así que el estado vigente es **C3 = 0012 y C4 = 0013**). **Requiere autorización explícita antes de crearse.** |
| D-108 | Pendiente (2026-10-29) | **Puntos del diseño que tocarían el contrato C0 — identificados, no aplicados.** (a) **`dry_run` en E8**: se evita, la vista previa va por CLI `import-check`; si más adelante se quiere en la API, exige contrato v0.1. (b) **Códigos de error de archivo**: no se agregan; una cabecera que no calza con el perfil se informa como el ya existente `422 IMPORT_FORMAT_INVALID`, y el mismo archivo (misma fuente + mismo `file_hash`) devuelve **200 con el resultado original**, sin campo nuevo. (c) **Fuente que no sea `supplier`**: hoy solo puede informarse con un código genérico; un código preciso (`SOURCE_KIND_INVALID`) exige contrato v0.1. (d) **Detalle estructurado de filas rechazadas en la API** (D-104): también exige contrato v0.1. Ninguno bloquea la implementación. |
| D-109 | Tomada (2026-10-29) | **Forma concreta del perfil de importación y superficie del conector `manual_import` (C2.4).**`columns` = papel → `{"header", "aliases"?, "position"?}`; se resuelve por nombre de cabecera normalizado contra `header` y luego `aliases`, y solo como respaldo por `position` (1-based). Papeles: `sku`, `price`, `sale_unit` **obligatorios**; `name`, `currency`, `basis`, `pack`, `minimum`, `multiple`, `stock`, `observed_at`, `conditions` opcionales. Un papel desconocido o una columna obligatoria ausente es `IMPORT_FORMAT_INVALID` y **no escribe nada**. `units` traduce el vocabulario del archivo a `box`/`package`/`unit`/`bottle` (sinónimo no listado = `UNKNOWN_UNIT` por fila). `options`: `currency` y `price_basis` si no vienen por columna (sin ellas la fila es `INVALID_CURRENCY`/`INVALID_BASIS`, nunca hay valor por defecto); `decimal_separator` («.» por defecto) y `thousands_separator` (null por defecto, **no se adivina**: «1.234» sin declaración es 1,234); `date_format` (`YYYY-MM-DD`, `DD-MM-YYYY`, `DD/MM/YYYY`, opcionalmente con « HH:MM[:SS]»); `tax_included`/`discount_included` booleanos o null = desconocido; `pack_base_unit` obligatorio si existe el papel `pack`; `quantity_unit` para mínimo/múltiplo/stock (**si no se declara, se usa la unidad de venta de la fila**). Superficie: servicio `app/services/imports.py` y comandos `import-check` (vista previa, no escribe) e `import-run`; **sin endpoint HTTP** por ahora (el sobre `POST /api/v1/imports` sigue congelado en C0 y se implementará en una etapa autorizada). Fuente `kind='supplier'` obligatoria (una de cadena se rechaza con el código genérico `INVALID_SOURCE`, D-108c). `UNMODELLED_CONDITION` acepta la oferta y guarda el texto en `offer_snapshot.notes`. Auditoría y outbox: **un evento por importación** (`import_run.created` / `catalog.import_completed`) con conteos y hash, no uno por oferta. Dependencias nuevas: `openpyxl` (lectura de `.xlsx`) y `defusedxml` (impide expansión de entidades en XML no confiable), declaradas en `pyproject.toml`. |
| D-110 | Tomada (2026-10-29) | **Estado de revisión de la oferta y fila rechazada mínima (migración 0010, no aplicada a `farmacia`).** (1) `offer_snapshot.review_status` `text NOT NULL DEFAULT 'accepted'`, `CHECK IN ('accepted','needs_review')`, y coherencia `review_status <> 'needs_review' OR notes IS NOT NULL`. La 0009 **no se modifica** (ya publicada): el cambio va en la migración **0010**; por eso **C3 pasó a 0011 y C4 a 0012** (hoy **0012/0013**, porque D-111 tomó 0011). (2) `needs_review` es un **hecho histórico del snapshot**, no un flujo de trabajo: `offer_snapshot` es de solo agregar, así que una oferta marcada nunca se «desmarca»; la reemplaza una observación posterior. **No se crea tabla de revisión ni aprobación manual en v0**; si más adelante se necesita «alguien revisó y aceptó», será una tabla nueva con actor y snapshot, en C3. (3) **Una oferta `needs_review` no es una oferta verificada**: no puede considerarse elegible para seleccionar proveedor ni entrar en la comparación de conveniencia (E3). (4) **Contrato C0 intacto**: el campo no se expone en la API; el sobre de E8 conserva su forma congelada. (5) **D-104 enmendada**: `rejected_rows` guarda número de fila, códigos, fuente, hash, SKU, nombre y **solo** las celdas imputadas por el rechazo, declaradas por el propio `RowProblem` (nunca deducidas después); no la fila entera ni el archivo. |
| D-111 | Tomada (2026-10-30; **solución definitiva**) | **Idempotencia de E8 y endpoint `POST /api/v1/imports` (C2.5, migración 0011).** (1) Tabla **`import_request`** (solo agregar, trigger `forbid_mutation()`): una fila por solicitud HTTP **exitosa** con `Idempotency-Key`, guardando el contexto exacto — organización, `source_id`, `supplier_id`, `file_hash` — y la corrida que devolvió (`import_run_id`). Único `(organization_id, idempotency_key)`: una clave no puede quedar atada a dos contextos. **No** se agrega columna a `import_run`: una misma corrida puede ser devuelta a varias solicitudes (reintentos con otra clave, CLI + API) y la corrida no debe saber nada de cabeceras HTTP. (2) Semántica: clave registrada + mismo archivo/fuente/proveedor → 200 con el resultado original; clave registrada + cualquier diferencia → 409 `IDEMPOTENCY_KEY_REUSED`; clave nueva sobre un archivo ya importado por otra clave → 200 con la corrida original **y la clave nueva queda registrada** (así el 409 posterior funciona); solicitud fallida (403/404/409/413/422) **no** deja fila y no consume la clave. (3) La fila se escribe en la misma transacción que la corrida. (4) Una corrida solo se reutiliza si coincide organización **y** fuente **y** hash, y el proveedor corresponde a esa fuente; nunca por hash solo. (5) La clave **no** viaja en la respuesta: contrato C0 intacto. Números de migración vigentes: **C3 = 0012, C4 = 0013**. (6) El endpoint es una capa fina sobre `run_import`: exige rol `admin` y `StrongActor`, valida `supplier_id`/`source_id`, corta en 10 MB y delega. No expone `dry_run` (D-108a) ni campos nuevos. |

**Contrato de entrada A — catálogo propio (`catalog-load`, CLI):**

```json
{"type": "article", "identity_key": "...", "display_name": "...", "product_kind": null, "ingredient": null,
 "laboratory": null, "brand": null, "gtin": null, "sanitary_registration": null, "attributes": {}}
{"type": "mapping", "source_id": "uuid", "external_id": "...", "item_identity_key": "...", "external_name": null}
```

**Contrato de entrada B — planilla del proveedor (E8).** Cuatro datos por fila son el mínimo para crear una oferta:
**SKU, precio, moneda y unidad de venta**. Opcionales: nombre externo, contenido por caja, mínimo, múltiplo, stock,
fecha de observación, IVA, descuentos y texto de condiciones. Todo lo demás lo resuelve el perfil de la fuente.

**Qué tabla recibe qué:**

| Tabla | La escribe | Garantía existente |
|---|---|---|
| `trade_item` | `catalog-load` | `UNIQUE (organization_id, identity_key)` |
| `supplier_product_mapping` | `catalog-load` | `UNIQUE (organization_id, source_id, external_id)` |
| `import_profile` (propuesta) | perfil de la fuente | una por fuente, versionada |
| `import_run` | E8 | `UNIQUE (organization_id, source_id, file_hash)` y solo agregar |
| `offer_snapshot` | E8 | solo agregar: un precio nuevo agrega, nunca sobrescribe |

**Reparto:** `catalog-load` crea **artículos y vínculos** desde un archivo técnico; `manual_import` (E8) crea
**ofertas e historial** desde la planilla del proveedor y rechaza lo no mapeado. `catalog-load` no sustituye E8 y E8 no
administra el catálogo (D-097 sigue pendiente y no se toca).

**No cambia:** contrato C0 congelado, tablas y restricciones de 0008, compra real `blocked`, sin IA sobre archivos,
`reference_price`/`chain_product` fuera de C2 y Lovable en pausa.

### D-109 — Conector `manual_import` implementado (C2.4, 2026-10-29)

**Qué es:** `app/services/imports.py` (`run_import`) más los comandos `import-check` (vista previa) e `import-run` de
`app/cli.py`. Lee la planilla **original** del proveedor con `openpyxl` (XML mediante `defusedxml`), la interpreta con
el perfil de su fuente y escribe ofertas fechadas.

**Dos fases, sin escrituras a medias (D-103):**

1. Estructural: archivo legible (10 MB máx.), hoja y fila de cabecera del perfil, todas las columnas obligatorias
   resueltas, 20.000 filas máx. Si algo falla → `IMPORT_FORMAT_INVALID` o `PAYLOAD_TOO_LARGE` y **nada se escribe**, ni
   siquiera `import_run`.
2. Por fila: se clasifican todas y después se escriben las aceptadas. Cada rechazo lleva `{row, code, message}` con el
   número de fila de la planilla; la fila rechazada se conserva en `import_run.rejected_rows` con lo mínimo para
   localizarla (número de fila, código, fuente, hash, SKU, nombre y las celdas que causaron el rechazo — D-104, según
   D-110). `rows_total = rows_accepted + rows_rejected`.

**Escribe solo** `import_run` y `offer_snapshot` (D-106). Nunca `trade_item`, `trade_item_alias` ni
`supplier_product_mapping`: un SKU sin vínculo previo es `UNMAPPED_PRODUCT`, jamás una búsqueda por nombre. Las filas
aceptadas se agregan como observaciones nuevas; ningún precio anterior se actualiza ni se borra.

**Separación de planos:** la fuente debe ser `kind='supplier'`; una fuente de cadena se rechaza antes de leer la
primera fila y `chain_product`/`reference_price` no se tocan.

**Idempotencia:** mismo `(organización, fuente, sha256 del archivo)` → se devuelve el resultado original sin escribir
nada (`repeated`), apoyado en el índice único de `import_run`.

**Procedencia:** `data_origin` según la organización (`simulated` en simulación, `captured` en prueba real), igual que
en `catalog-load`.

**Pendiente y fuera de esta etapa:** el endpoint `POST /api/v1/imports` con el sobre congelado de C0 (multipart,
`Idempotency-Key`, 409 de clave reutilizada, 413), la administración diaria del catálogo (D-097) y la lectura de CSV.

### D-110 — Estado de revisión de la oferta y fila rechazada mínima (2026-10-29)

**Por qué:** el contrato ya habla de ofertas `needs_review` (E3, `cotizador-v0.md` líneas 438, 717 y 939), pero en la
base solo existía como texto libre en `offer_snapshot.notes` más un código en `import_run.errors`. Derivar la
elegibilidad parseando texto sería justamente lo que el proyecto prohíbe. Tampoco había registro de qué se conserva de
una fila rechazada más allá de «íntegra», que en la práctica copiaba columnas ajenas a la compra.

**Migración 0010 — `offer_snapshot.review_status`:**

- `text NOT NULL DEFAULT 'accepted'`; `CHECK review_status IN ('accepted','needs_review')`; coherencia
  `review_status <> 'needs_review' OR notes IS NOT NULL` (no hay marca sin motivo). Cambio aditivo: ninguna fila
  existente se reescribe.
- **No** se modifica la 0009, ya publicada. La 0010 es una migración nueva, con modelo sincronizado y el test
  `test_modelos_y_migracion_estan_sincronizados` cubriéndola. Consecuencia de numeración: **C3 = 0011 y C4 = 0012**
  (hoy **0012/0013**, porque D-111 tomó 0011; ver esa sección).
- **Semántica (a), no flujo de trabajo:** `offer_snapshot` es de solo agregar (trigger `forbid_mutation()` de la 0008),
  así que la marca es un hecho histórico: nunca se «desmarca». La oferta sigue marcada mientras esa condición aplique y
  la reemplaza una observación posterior. **No hay tabla de revisión ni aprobación manual en v0**; si la farmacia
  necesita «alguien revisó y aceptó», será una tabla aparte (`offer_review`, con actor y snapshot) en C3.
- **Regla de negocio:** una oferta `needs_review` **no es una oferta verificada** y no puede considerarse elegible para
  seleccionar proveedor más conveniente ni entrar en la comparación de E3. Es visible, con sus causas, pero no compite.
- **Contrato C0 intacto:** el campo no se expone en la API; la respuesta de E8 conserva su forma congelada. Exponerlo
  exigiría contrato v0.1 (mismo criterio que D-108).

**D-104 enmendada — fila rechazada mínima:** `_rejected_row()` guarda `{row, codes, source_id, file_hash, sku, name,
fields}`. `fields` contiene **solo** las celdas que causaron el rechazo, y las declara el propio `RowProblem` en el punto
donde se detecta (`price` para `INVALID_PRICE`, `sale_unit` para `UNKNOWN_UNIT`, `sku` para `UNMAPPED_PRODUCT` y
`DUPLICATE_ROW`, `currency`/`basis` para las suyas, `minimum`/`multiple`/`stock`/`pack` para `INVALID_QUANTITY`,
`observed_at` para la fecha ilegible). Nada se deduce después ni se completa con convenciones. Motivos: `import_run`
también es de solo agregar (la forma elegida por corrida es permanente), una planilla de 20.000 filas podía dejar varios
MB de JSONB con columnas ajenas a la compra, y el número de fila + fuente + hash + SKU bastan para encontrarla en la
planilla original. Se conserva SKU, nombre y las celdas imputadas para no perder el diagnóstico sin el archivo.

### D-111 — Endpoint E8 implementado e idempotencia por clave (C2.5, 2026-10-30)

**Qué se agregó**

- `POST /api/v1/imports` (`app/api/v1/imports.py`) con el sobre **congelado** de C0: `multipart/form-data` con
  `file`, `supplier_id` y `source_id`; cabecera `Idempotency-Key` obligatoria (8–128); respuesta 200 con
  `import_id`, `status`, `rows_total`, `rows_accepted`, `rows_rejected`, `errors[]`, `observed_at` y **ningún campo
  más** (`schemas.ImportResult`).
- Capa fina: `services.imports.run_import_request` valida el par fuente/proveedor, exige rol `admin` y `StrongActor`,
  aplica la clave y delega todo el trabajo en `run_import`. No hay lógica de importación duplicada, ni endpoints E6/E7,
  ni `dry_run` en la API (D-108a).
- Dependencia nueva: `python-multipart` (parseo del formulario), declarada en `pyproject.toml`.

**Migración 0011 — tabla `import_request`**

- Una fila por solicitud HTTP **exitosa** con `Idempotency-Key`: `organization_id`, `idempotency_key`, `source_id`,
  `supplier_id`, `file_hash`, `import_run_id`, `created_by`, `created_at`, `data_origin`.
- `UNIQUE (organization_id, idempotency_key)`; FKs compuestas por organización (fuente, proveedor, corrida, autor);
  `CHECK` de longitud 8–128, hash hexadecimal de 64 y vocabulario de `data_origin`. De solo agregar
  (`forbid_mutation()`), como los demás snapshots.
- **No** se agrega columna a `import_run`: la corrida es la importación, la solicitud es la solicitud. Una misma
  corrida puede ser devuelta a varias solicitudes (reintento con otra clave, CLI + API).
- La clave no viaja en la respuesta: el contrato C0 no cambia. Números vigentes: **C3 = 0012, C4 = 0013**.

**Reglas de idempotencia (contrato §2.6)**

| Caso | Respuesta |
|---|---|
| Clave registrada, mismo archivo + fuente + proveedor | 200 con el resultado original (sin escribir) |
| Clave registrada, otro archivo, fuente o proveedor | 409 `IDEMPOTENCY_KEY_REUSED` |
| Clave nueva sobre un archivo ya importado por otra clave | 200 con la corrida original **y la clave queda registrada** |
| Clave nueva, archivo nuevo | 200 y se registra |
| Solicitud fallida (403/404/409/413/422) | no deja fila: la clave sigue disponible |

La fila se escribe en la misma transacción que la corrida (o que la reutilización), después de validar que la corrida
devuelta pertenece a esa organización **y** esa fuente **y** ese hash, y que el proveedor corresponde a la fuente:
nunca se reutiliza una importación por coincidencia de hash sola.

**Transporte:** lectura por bloques con corte en 10 MB → 413 `PAYLOAD_TOO_LARGE`; archivo ilegible o sin cabecera →
422 `IMPORT_FORMAT_INVALID` sin escribir nada; falta de `Idempotency-Key` o campos del formulario → 400
`VALIDATION_ERROR` (mapeo ya existente de la API, no un código nuevo).

**Pruebas:** `tests/test_imports_api.py` (21: las de transporte y contrato más las seis de la clave: registro sobre
archivo ya importado, 409 posterior con otro archivo, 409 con otra fuente, no duplicación del registro, solicitud
fallida que no consume la clave, y unicidad + solo agregar), la sincronía modelo↔migración y la regresión del motor
`manual_import`, sobre una base dedicada migrada a 0011 y con el ciclo de reversión comprobado.

## Investigación de catálogos de cadenas (2026-10-30)

| ID | Estado | Decisión |
|---|---|---|
| D-112 | Tomada (2026-10-30) | **Se autoriza investigar (no construir) cómo publican su catálogo Salcobrand, Cruz Verde y Farmacias Ahumada, antes de implementar C6.** Solo investigación técnica y documental, con acceso permitido: páginas públicas, sin autenticación, sin eludir CAPTCHA/MFA, respetando `robots` y términos de uso, con muestra exploratoria pequeña de medicamentos conocidos y sin extracción masiva. **Descubrir no es comparar:** una ficha de cadena puede mostrarse como *resultado de la cadena* aunque no esté vinculada a un artículo del catálogo propio, identificada como tal, sin asumir equivalencia y sin entrar en ofertas ni totales; vincularla o compararla exige correspondencia **confirmada**. Se identificó que la forma congelada de E1/E3 **no admite** fichas de cadena sin artículo: la ampliación queda como **propuesta pendiente (contrato v0.1)**, no aplicada. C0 no se modifica; el orden C3/C4 y la numeración (C3 = 0012, C4 = 0013) tampoco. La investigación no crea conectores, no escribe datos operativos, no aplica migraciones y no toca el frontend. |

### D-112 — Investigación de catálogos de cadenas (2026-10-30)

**Motivo.** Las tres cadenas son parte central del buscador ya diseñado en Lovable. C6 estaba abierto y sin mecanismo.
Se separa **investigar** de **construir**: primero se averigua qué es posible y bajo qué condiciones; construir exige
una decisión nueva con el mecanismo elegido.

**Descubrir ≠ comparar (ajuste del usuario, 2026-10-30).** Se investiga si el buscador podrá mostrar **fichas reales**
de las tres cadenas aunque no estén vinculadas a un artículo del catálogo propio. Esas fichas:

- se presentan como **resultado de la cadena**, con su fuente, modo, procedencia y fecha;
- **no** afirman equivalencia con un artículo, ni dosis, ni presentación equivalentes;
- **no** se vinculan automáticamente a ofertas comerciales, ni entran en `offers[]`, totales o `lowest_verified_total`;
- solo se pueden **comparar** o **vincular** al catálogo propio con correspondencia **confirmada** por una persona.

**Qué se investiga, por cadena:** cómo se carga el catálogo (HTML del servidor o solicitudes JavaScript/JSON de la
propia página); forma de esas respuestas y campos disponibles; si son alcanzables sin sesión ni desafío anti-bot; si
hay bloques legítimos (login, CAPTCHA, `robots`, términos) —en cuyo caso **se detiene esa vía y se informa**—;
estabilidad de identificadores y URL canónica por ficha; cobertura sobre la muestra; granularidad y condiciones del
precio público; y URL de imagen.

**Datos requeridos y destino (el modelo ya existe en 0008; no se crea nada).** Nombre → `chain_product.published_name`
(tal como se publica, nunca el nombre del artículo); laboratorio → `laboratory_as_published`; presentación →
`presentation_as_published`; descripción → `description`; imagen → `image_url` + `image_hash` (referencia, sin copia
propia); precio público → `reference_price.price_normal`/`price_promo` + `currency`; URL → `source_url`; fecha →
`observed_at` (UTC); procedencia → `data_origin='captured'`, `evidence` (url, hash, nota) y `source_id` de la cadena.
Una lectura simulada o de muestra **nunca** entra en una organización real (D-037).

**Separación de planos (C0 intacto).** Una fuente de cadena es `kind='chain_reference'`: no produce ofertas, no
declara `terms` de compra (trigger de la base y D-106). `chain_product` es snapshot por `content_hash`;
`reference_price` exige `chain_product_id` de la misma cadena. El precio público no participa de comparaciones ni de
totales (C0-05, AC20). Las condiciones de la cadena viajan como condiciones de la **referencia**, no como capacidad
de compra (E7).

**Vinculación al catálogo propio.** Solo por acción explícita: `chain_product.trade_item_id` con
`mapping_origin='explicit'|'confirmed'` y `mapped_by`/`mapped_at`. Sin vínculo, `match_status='unmapped'`: la ficha se
conserva para revisión y se muestra como ficha de cadena, **no** como artículo. Los parecidos se guardan en
`match_candidates` con motivos y quedan `needs_review`; nunca se asocia por nombre, dosis ni imagen.

**Muestra exploratoria (sin pedir productos a Pao).** Un puñado de medicamentos conocidos, con presentaciones
distintas (por ejemplo comprimidos y jarabe), buscados a mano en cada cadena. Sus límites se registran en el informe:
no mide cobertura del surtido real de la farmacia, no prueba estabilidad en el tiempo y no representa precios de
sucursal distintas de la consultada.

**Propuesta pendiente, no aplicada — ampliación del contrato.** La forma congelada de C0 no admite presentar fichas
de cadena sin artículo: `SearchResultOut` (E1) exige `article_id` y E3 solo expone `reference_prices[]` de un artículo
existente. Para que Lovable muestre fichas descubiertas sin vínculo se necesita una **versión v0.1 del contrato** con
una estructura propia de resultado de cadena (fuente, procedencia, fecha y precio público, sin campos de oferta), sin
cambiar E1 ni E3. **No se redacta ni se implementa sin autorización.**

**No autoriza:** construir conectores, extraer en volumen, escribir en `chain_product`/`reference_price` operativos,
crear fuentes de cadena reales, modificar C0 o el frontend.

## Búsqueda de precios públicos de cadenas (2026-10-30)

| ID | Estado | Decisión |
|---|---|---|
| D-113 | Tomada (2026-10-30) | **Se aprueba funcionalmente la búsqueda de precios públicos de Cruz Verde, Salcobrand y Farmacias Ahumada, en vivo y bajo demanda, para cualquier producto de farmacia, sin datos ficticios, históricos ni precargados como sustituto.** Opera **independiente de E1**: `POST /api/v1/chain-searches` responde **202** y `GET /api/v1/chain-searches/{search_id}` se sondea; **sin SSE, sin persistencia y sin migración**. Cada cadena informa su estado real; una cadena sin conector o sin autorización aparece `not_configured` con motivo, **nunca con precios inventados**. Los precios públicos siguen fuera de ofertas, carritos, totales y elegibilidad (C0-05, AC20). **La captura por cadena no queda autorizada por esta decisión**: rige D-115. Esta decisión ocupa el número que estaba reservado y no se duplica. |

### D-113 — Búsqueda federada bajo demanda de precios públicos (2026-10-30)

**Motivo.** El buscador ya diseñado en Lovable muestra, junto a las ofertas de proveedores, los precios públicos de
las tres cadenas. C0 los mantiene **fuera** de las ofertas y del cálculo (C0-05, AC20), pero no tenía un mecanismo
para consultarlos. D-112 autorizó **investigar**; D-113 decide **cómo se consulta**, sin construir todavía ningún
conector real.

**Independencia de E1.** La consulta de cadenas no exige un artículo del catálogo propio ni cambia E1/E3:

| Método y ruta | Comportamiento |
|---|---|
| `POST /api/v1/chain-searches` | Inicia la consulta y responde **202** con `search_id`, `status: pending` y `expires_at` |
| `GET /api/v1/chain-searches/{search_id}` | Sondeo; **200** con el resultado, **404 `SEARCH_NOT_FOUND`** si no existe o expiró |

No hay SSE, no hay streaming y no se guarda nada. El estado vive **en memoria del proceso**: se documenta **un solo
worker de API**; un reinicio pierde las búsquedas en vuelo y el sondeo devuelve 404.

**Estados, sin ambigüedad.**

| `state` | `outcome` | Significado |
|---|---|---|
| `ok` | `matches` | Respondió y hay al menos una ficha |
| `ok` | `no_matches` | Respondió válidamente y no hay fichas |
| `stale` | — | Observación anterior conservada solo como referencia; **no** es el precio de esta consulta |
| `failed` | — | Error real: `blocked`, `timeout`, `network_error`, `rate_limited` |
| `not_configured` | — | Sin conector autorizado: `access_not_authorized`, `search_not_available`, `requires_javascript` |

Reglas: **un CAPTCHA o desafío es `failed`, nunca `no_matches`**; una respuesta vacía válida es **200**, nunca 503;
si todas las cadenas están `not_configured`, la búsqueda termina `completed` con 200 y el estado real de cada una;
**no se usan 502 ni 504 para cadenas**; el único 503 es el existente `DATABASE_NOT_CONFIGURED`.

**Límites:** consulta bajo demanda, sin cron ni espejo; una consulta en paralelo por cadena; **4 s por fuente, 8 s
global**; TTL **10 min**; máximo **50 búsquedas vivas** por proceso; **un worker**. Códigos nuevos:
`SEARCH_NOT_FOUND` (404) y `RATE_LIMITED` (429 con `Retry-After`).

**Estado inicial real (ninguna cadena habilitada).**

| Cadena | Estado | Motivo | Fundamento |
|---|---|---|---|
| Cruz Verde | `not_configured` | `requires_javascript` | `robots` permite `/search`, pero los resultados se cargan por JavaScript; no hay conector autorizado |
| Salcobrand | `not_configured` | `search_not_available` | La búsqueda requiere JavaScript y `robots` no permite `/api`; no hay conector autorizado |
| Farmacias Ahumada | `not_configured` | `access_not_authorized` | Hay reCAPTCHA; los términos (versión 13.10.23) **no autorizan ni prohíben** el acceso automatizado; la captura automatizada **no está autorizada** |

La habilitación es por `CHAIN_SEARCH_ENABLED` (lista de cadenas), **no** por código. `sources[]` siempre lista las
tres cadenas con su estado real.

**Sin persistencia.** `chain_product` y `reference_price` (0008) solo se usan como **evidencia** cuando una persona
confirma una correspondencia (D-112); **no** son un espejo del catálogo de la cadena. No hay migración para E15.

**Vigencia.** `observed_at` es de **esta** consulta. Al agregar o revisar una oferta en una cotización se repite la
consulta con `purpose: "quote_verification"`; **solo** el precio de esa verificación puede mostrarse como vigente. Si
la fuente falla, se muestra el fallo; nunca se reutiliza un precio anterior como actual. Los precios de cadena no
entran en `offer_snapshot`, carritos, totales ni elegibilidad.

**No autoriza:** construir conectores, capturar datos de ninguna cadena, usar datos ficticios o históricos como
sustituto, modificar C0/E1/E3, crear tablas o migraciones, ni tocar el frontend. La política de captura es D-115.

## Cotizador: cotizaciones, carritos por proveedor, compras registradas e historial (2026-10-30)

| ID | Estado | Decisión |
|---|---|---|
| D-114 | Tomada (2026-10-30) | **Se amplía C0 con dos excepciones nombradas:** (X1) crear cotizaciones y carritos **en preparación** y (X2) **registrar una compra ya ocurrida** fuera del sistema como hecho documentado, nunca como ejecución. La cotización general agrupa carritos independientes, uno por proveedor, con evaluación E5 por carrito (nunca una segunda lógica de dinero). Compras inmutables y corregibles solo por registro nuevo que sustituye; historial comercial propio, independiente de `audit_event`. Conservación operativa de 6 meses **por compra** y archivo permanente en almacenamiento externo verificado, sin archivar ni eliminar cotizaciones por antigüedad. Contrato `docs/contracts/cotizador-v0.2.md`. C0 no se edita; E1–E9 no cambian; `purchase_execution` sigue `blocked`. E10/E11/E12 exigen `admin` + `StrongActor`. La purga del detalle está **diseñada y deshabilitada**. No autoriza código, migraciones, extensiones, proveedor de archivo ni ejecución de la purga. **Vigente con la enmienda de D-128 (2026-10-30): E10/E11/E12 exigen `admin` o `purchasing` + `StrongActor`; el resto de D-114 no cambia.** |

> Nota de numeración (actualizada en D0, 2026-10-30): **D-113** ya está registrada arriba, tras su aprobación
> funcional; **D-114** conserva su número y su contenido. **D-115 a D-127** se registran al final de esta sección.
> No hay duplicados ni huecos. El texto original de D-114 se conserva; la **enmienda de D0** se registra aparte y
> prevalece solo en los puntos que enumera.

### D-114 — Cotizaciones con carritos por proveedor, compras registradas e historial (2026-10-30)

**Motivo.** El cotizador ya definido (C0) **excluye** explícitamente carritos, propuestas, aprobaciones y ejecución
(§9 «fuera de v0», C0-10). El funcionamiento real de la farmacia exige preparar cotizaciones separadas por proveedor,
retomarlas, y dejar constancia de lo que efectivamente se compró —sin que el sistema envíe órdenes ni mueva dinero—.
Como el prompt del usuario prevalece sobre el HLD, la ampliación se **declara** en esta decisión y en
`cotizador-v0.2.md`, y no se presenta como si C0 ya la permitiera.

**Aprobado conceptualmente por el usuario (2026-10-30):** cotizaciones con múltiples carritos persistentes; compras
registradas de forma independiente; historial comercial inmutable; archivo permanente después de seis meses; y que
registrar una compra **solo documenta** una operación hecha fuera del sistema.

**Las dos excepciones (nombradas, con límite).**

| # | Excepción | Límite |
|---|---|---|
| X1 | Crear cotizaciones y carritos en preparación | Documentos de trabajo; no son propuestas aprobadas ni comprometen compra |
| X2 | Registrar una compra ya ocurrida | Hecho documentado; el sistema no envía órdenes, no paga y no integra proveedores |

Todo lo demás de C0-10 sigue en pie: aprobaciones, propuestas formales, envío de órdenes, pagos y recepción quedan
fuera. `purchase_execution` sigue `blocked`.

**Modelo (6 tablas; contrato v0.2 §2).** `quote` (cotización general: `draft|closed`), `quote_cart` (uno por
proveedor, `active|dismissed`, único por cotización y proveedor), `quote_line` (oferta seleccionada + cantidad entera
explícita + `active|removed` + cierre del pendiente, sin columna de precio), `purchase` y `purchase_line`
(inmutables, solo agregar) y `purchase_index` (índice de búsqueda del historial). **El encabezado `purchase` no se
elimina nunca**: sus columnas de archivo (`archive_status`, `archive_sha256`, `archive_restore_ok`,
`detail_purged_at`) sustituyen a la tabla `archive_record` de la propuesta anterior (P-1/P-2, 2026-10-30). **No** se
crea tabla de eventos: los cambios van a `audit_event`. **No** se crea tabla de idempotencia: `purchase` lleva
`idempotency_key` + `request_hash` con unicidad por organización, como ya hace `import_request` (D-111).

**Sin identificadores técnicos para Pao.** El carrito **nace con la primera línea** de ese proveedor y se identifica
por proveedor, no por `cart_id`. No existe endpoint de «crear carrito».

**Evaluación comercial.** Cada carrito se evalúa con **E5**, invocado internamente **una vez por carrito** y en
paralelo en proceso. Motivo: mínimo, múltiplo, impuestos, descuentos y despacho son **por proveedor**, y el reparto de
un mismo artículo entre proveedores exige cantidades distintas. Consecuencia declarada: la cotización general **no**
tiene una única llamada a E5. El carrito **no** implementa aritmética propia (C0-15 intacto).

**Precio seleccionado ≠ precio vigente.** La línea se ancla a un `offer_snapshot` inmutable y nunca se reapunta sola.
La lectura distingue `selected` (lo que Pao eligió), `current` (observación más reciente, si existe) y un
`price_state` (`current|requires_review|stale|no_longer_offered`). Aceptar el precio nuevo es una operación explícita y
auditada. Nunca se presenta un precio histórico como vigente. En el carrito **no** se copia el precio; en la compra
**sí** se copia todo, por ser evidencia histórica que debe sobrevivir al archivo y a cualquier cambio posterior.

**Reparto, mover, quitar, descartar y desistir.** Repartir cantidades no es una operación nueva: es la misma
referencia en el carrito de otro proveedor. Mover exige **oferta destino explícita** y **el mismo artículo**, es
atómico y auditado. Quitar es lógico (`removed`); descartar un carrito es `dismissed` y **reincorporar** es su
operación inversa —ambas existen en la API, de modo que ninguna transición queda alcanzable sin operación
definida—. Las ofertas distintas de un mismo artículo conviven y **no se fusionan** (se advierte
`duplicate_article`).

**Compras ya registradas y desistimiento del pendiente (precisión P-5, 2026-10-30).** Ninguna operación sobre una
línea altera la evidencia histórica: la compra copió sus valores al registrarse. Reglas: la cantidad nueva no puede
ser menor que lo comprado vigente (`QUANTITY_BELOW_PURCHASED`); quitar o mover una línea con compras vigentes está
bloqueado (`LINE_HAS_PURCHASES`); aceptar un precio nuevo sí se permite porque no toca compras. Para que Pao pueda
**desistir de comprar lo que falta** —cotizó 20, compró 15, no comprará las 5 restantes— existe una operación
explícita de **cierre del pendiente** (`pending_closed_at`, con reapertura): **no cambia la cantidad cotizada**, no
modifica la compra histórica y no borra la línea. El estado de una línea se deriva en **dos dimensiones
independientes** —`purchase_state` (`pending|partially_purchased|purchased|exceeded`) y `remainder`
(`open|closed_by_user|closed_by_quote`)—, nunca desde un contador guardado: así una línea comprada a medias dice
cuánto se compró y, a la vez, por qué el resto ya no se espera.

**Total general estimado (ajuste del usuario, 2026-10-30).** Objeto nuevo, **fuera de E5**, que solo se muestra cuando
los importes son **suficientemente completos, compatibles y calculables**: todos los carritos activos `complete` en
E5, una sola moneda, ninguna causa desconocida pendiente. Si falta algo → `available: false` con las causas visibles.
**Nunca** se presenta una cota inferior como costo total general, y las cotas por proveedor no se suman entre sí.
C0-07 no se contradice: el estimado no es un `total` de E5, no compara proveedores y no decide nada.

**Compras registradas.** Una compra = un proveedor y un momento; **varias compras por cotización**, en fechas
distintas, sin cerrar prematuramente la cotización ni perder los carritos pendientes. Se registran cantidades
**mayores** que las cotizadas: la línea queda `exceeded`, se advierte y se conservan ambas cantidades; nunca se recorta.
Se puede **cerrar con productos no comprados**: esas líneas conservan su cantidad cotizada y su cobertura, y su
`remainder` queda `closed_by_quote` (cotizada no comprada); no se borran. Reabrir la cotización devuelve a cada
línea su estado anterior, respetando `pending_closed_at` si Pao ya había desistido de esa línea.

**Correcciones sin doble conteo (ajuste del usuario, 2026-10-30; precisión P-3).** Como la compra es inmutable,
corregir **no edita**: se registra una compra nueva con `supersedes_purchase_id` y motivo obligatorio. El original se
conserva. Un registro es **vigente** (`current`) si ninguno lo sustituye; **el historial y los reportes suman solo
registros vigentes y no anulados**, y muestran el sustituido marcado, con enlace al vigente. **`UNIQUE
(supersedes_purchase_id)`** impide dos sustituciones vigentes a la vez —la segunda recibe `409
PURCHASE_ALREADY_SUPERSEDED`—; la garantía es de la base, no del código. Las compras **archivadas también se pueden
corregir**: el bundle verificado precarga el formulario y la cadena se conserva íntegra aunque el detalle ya no esté
en PostgreSQL. **Anulación (P-4):** una compra registrada por error se anula con un **registro nuevo** marcado
`is_annulment`, sin líneas, con motivo obligatorio y auditoría; queda excluida de los totales, pero **la evidencia
original nunca se borra** ni se oculta.

**Historial comercial ≠ auditoría.** `audit_event` **no** reemplaza el historial: la compra tiene su registro
inmutable completo. Historial es página independiente del menú (Lovable), con búsqueda por producto, proveedor, fecha
o período e identificador de cotización, y distinción entre cotizaciones finalizadas y compras registradas. Un dato
desconocido se conserva desconocido: no se rellena para completar el historial.

**Conservación y archivo (ajuste del usuario, 2026-10-30; precisiones P-1, P-2, P-6 y bloqueo).** Las compras
permanecen **completas** en la base operativa durante **6 meses** desde `registered_at`. Después, **solo el detalle de
líneas** (`purchase_line`) pasa a **almacenamiento externo** de objetos, versionado y con bloqueo de retención,
separado de PostgreSQL. **La unidad de archivo es la compra, no la cotización**: la propuesta anterior archivaba por
cotización y **no** permitía lo que el usuario exige —se corrige—. Archivar una compra antigua **no** archiva ni
elimina su cotización, sus carritos ni sus líneas pendientes. **El encabezado `purchase` no se elimina nunca**:
sostiene la cadena de correcciones, los totales, las FK y los reportes, y es pequeño. La búsqueda del historial no
depende del almacén externo: `purchase_index` guarda por línea el producto, la presentación, el laboratorio, el
proveedor, la fecha, la cantidad y el precio unitario —**sin** impuestos, descuentos, despacho, condiciones ni
evidencia— y se consulta con `pg_trgm`/`unaccent`; el bundle se abre solo al abrir un registro. **La extensión no se
instala sin verificar antes su disponibilidad en el PostgreSQL de destino y sin autorización.**

**Purga controlada (diseñada, no ejecutada).** El detalle solo puede salir por una función `SECURITY DEFINER` de un
rol dedicado, con `DELETE` negado al rol de la aplicación, trigger que rechaza `UPDATE` siempre y `DELETE` salvo con
el indicador de purga y la compra en `archive_status='verified'` con `archive_restore_ok=true`. `DELETE` sobre
`purchase` no existe. **No se ejecuta nada** mientras no haya almacenamiento externo aprobado y no se hayan probado
integridad, restauración y consulta de los registros archivados; hasta entonces las compras siguen completas aunque
superen los 6 meses. **Sin copia verificada y restaurable no se elimina nada**; no hay borrado por temporizador. El
archivo **no reemplaza** los respaldos de PostgreSQL.

**Contrato y API.** `docs/contracts/cotizador-v0.2.md` (borrador para revisión, revisión 2) con **E10** cotizaciones
y carritos, **E11** compras registradas y **E12** historial. Códigos nuevos propuestos: `QUOTE_NOT_FOUND`,
`QUOTE_CLOSED`, `QUOTE_VERSION_CONFLICT`, `CART_DISMISSED`, `OFFER_NOT_IN_SUPPLIER`, `ARTICLE_MISMATCH`,
`INVALID_QUANTITY`, `PURCHASE_INVALID`, `PURCHASE_ALREADY_SUPERSEDED`, `LINE_HAS_PURCHASES`,
`QUANTITY_BELOW_PURCHASED`, `INVALID_ANNULMENT`, `ARCHIVE_UNAVAILABLE`. El menú **Proveedores** es **E6 de C0** (ya
definido, no implementado) y Telegram sigue siendo un canal, no un proveedor ni una fuente de precios.

**Permisos (P-7, contrato v0.2 §5).** E10, E11 y E12 exigen **rol `admin` + `StrongActor`**, conforme a C0 §6,
D-079 y D-086; cualquier otro rol recibe `403 FORBIDDEN`. **No se amplían permisos a auxiliares en esta etapa**, y
la organización sale del `Actor`, nunca del cuerpo. Consecuencia práctica declarada: Pao necesita rol `admin` para
usar el cotizador. Un rol diferenciado para auxiliares sería una decisión aparte, contraria a D-079, y no se propone.

**Estados.** Almacenados: `quote` `draft|closed` (con reapertura); `quote_cart` `active|dismissed` (con
reincorporación); `quote_line` `active|removed` más el cierre del pendiente (`pending_closed_at`, con reapertura);
`purchase` sin estado (existe porque ocurrió), con `archive_status` `none|pending|verified|failed`. Derivados, nunca
guardados: carrito (`empty|pending|partially_purchased|purchased`), línea en dos dimensiones
(`purchase_state` `pending|partially_purchased|purchased|exceeded` · `remainder`
`open|closed_by_user|closed_by_quote`), cotización (`in_preparation|partially_purchased|fully_purchased|
closed_without_purchase`) y compra (`current|superseded`, más `is_annulment`).

**Roadmap.** C3 = 0012 y C4 = 0013 **no cambian**. Después: C5-A cotizaciones y carritos (**0014**), C5-B compras e
`purchase_index` (**0015**), C5-C historial con búsqueda por índice (sin migración; exige verificar
`pg_trgm`/`unaccent`), C5-D archivo externo y purga (**0016**, diseño cerrado y ejecución bloqueada), C5-E enganche de
navegación. Dependencia real: C5 no puede ir antes de C3 porque la evaluación es E5. `farmacia` sigue en 0008.

**Capacidad futura identificada (no diseñada, no autorizada):** observaciones libres de Pao sobre productos («este
medicamento no me gusta por el laboratorio») con memoria de preferencias interpretada por IA. Será preferencia
**propia**, no afirmación objetiva; no se generaliza sola a toda una marca o laboratorio; Pao puede corregirla o
eliminarla. **No** forma parte del cálculo determinista de E5 ni altera precios o condiciones.

**No autoriza:** implementar código, crear tablas, aplicar migraciones (0014–0016) ni tocar `farmacia`, elegir
proveedor de almacenamiento o autorizar costos, **instalar extensiones de PostgreSQL**, **ejecutar la purga de
detalle**, modificar C0, E1–E9 ni el frontend Lovable, adelantar C3/C4/C5, ni hacer commit o push. El contrato v0.2
queda como **borrador para revisión**.

**Aprobado conceptualmente el 2026-10-30 (P-1 a P-7).** Encabezado `purchase` permanente; `archive_record`
sustituido por metadatos de archivo + `purchase_index`; `UNIQUE (supersedes_purchase_id)`; corrección de compras
archivadas; anulación con motivo y exclusión de totales; desistimiento del pendiente sin alterar lo cotizado ni lo
comprado; `purchase_index` con `pg_trgm`/`unaccent` sujeto a verificación de disponibilidad; `admin` + `StrongActor`
en E10/E11/E12; purga diseñada y **deshabilitada** hasta que exista almacenamiento aprobado y pruebas superadas.

## Mecanismo de cadenas, observaciones, accesos y faltantes en el cotizador (D0, 2026-10-30)

| ID | Estado | Decisión |
|---|---|---|
| D-115 | Tomada (2026-10-30) | **Mecanismo de consulta por cadena, política de captura y vigencia.** Solo conectores viables y permitidos; bajo demanda, sin cron ni espejo; consultas en paralelo por cadena; **4 s por fuente y 8 s global**; estados `ok/stale/failed/not_configured` y, solo en `ok`, `outcome matches/no_matches`; TTL 10 min; 50 búsquedas vivas; **un worker**; `SEARCH_NOT_FOUND`/`RATE_LIMITED`; **sin persistencia**. Estado inicial: Cruz Verde `requires_javascript`, Salcobrand `search_not_available`, Ahumada `access_not_authorized`; **ninguna habilitada**. La captura automatizada de Ahumada **no está autorizada**; requiere A1 (autorización escrita/API) o A2 (decisión de riesgo registrada). Verificación al cotizar: solo el precio de esa consulta es actual; un precio anterior nunca se presenta como vigente y el fallo se muestra. |
| D-116 | Tomada (2026-10-30) | **Observaciones comerciales independientes, inmutables y acumulativas; aprendizaje O1–O4 con OpenAI.** Se rechaza un `note_text` único en la línea. `commercial_observation` guarda cada observación con texto original, autor, fecha y contexto. `observation_interpretation` guarda la sugerencia de la IA con modelo, prompt, tokens, costo estimado y latencia; el contenido es inmutable y **solo** los campos de revisión cambian por transición auditada `suggested→confirmed\|rejected`, sin retorno. O1 persistencia (C5-A), O2 interpretación, O3 recuperación por artículo exacto y precedencia de la más reciente, O4 consejo explicado `advice` con `based_on` y `supersedes`. **OpenAI por Responses API, solo desde el backend**; salida estructurada; timeout, reintento acotado, presupuesto mensual y tope por lote; si falla o está deshabilitado, la observación se guarda y el consejo dice «sin interpretación», **nunca se finge IA con reglas**. La IA no decide dinero, cantidades, permisos ni visibilidad. |
| D-117 | Tomada (2026-10-30) | **Acceso web con Google: autenticación ≠ autorización; sin código de vinculación para usuarios nuevos.** Tres niveles: **superadministradora** (Yarella Fernández; administración global, organizaciones, administradoras y permisos globales), **administradora de farmacia** (Pao; gestiona usuarios de su farmacia, aprueba/rechaza/revoca, sin privilegios globales) y **usuarios autorizados** (Google; solo lo permitido, sin admin automático). Flujo: Google → JWT → validación → `app_user.auth_subject` activo y vigente; si no, `403 USER_NOT_PROVISIONED` y «Solicitar acceso» → `POST /web-access-requests` (organización desde configuración del servidor). Campanita para `admin`: `GET /web-access-requests` y aprobar/rechazar; **aprobar crea/enlaza con `roles = []`, nunca `admin`** (**vigente según D-128: aprobar asigna `roles = ['purchasing']`, nunca `admin`**). Pao, Danny y Julio se habilitan previamente con `create-admin`/`link-auth-subject`. `app_user` gana `email`, `is_superadmin`, `web_access_expires_at`, `web_access_granted_by/at`; vencimiento por petición (`WEB_ACCESS_EXPIRED`, sin job). La superadministradora no puede ser desactivada ni degradada por una administradora de farmacia. Sin contraseña ni registro público. |
| D-118 | Tomada (2026-10-30) | **Control individual de Telegram, no un interruptor global.** Pao habilita o deshabilita **por cuenta** quién puede enviar faltantes por Telegram. Se reutiliza `telegram_account.status active\|blocked`, `blocked_at/by/reason`, los endpoints `block`/`reactivate`/`history` y `GET /telegram-accounts` (con `MANAGE_ACCOUNTS` + `StrongActor`). Bloquear revoca sesiones (`end_reason='revoked'`), desactiva vínculos y **conserva todo el historial**; rehabilitar reutiliza la misma fila y recrea el vínculo privado sin nueva solicitud ni código. El backend valida el acceso en **cada** mensaje. **No hay tabla ni migración nuevas.** Límite: es por cuenta, no por chat; una cuenta `shared_device` se deshabilita para todas las personas del teléfono. Telegram y web son permisos independientes; `app_user.active=false` es una suspensión más fuerte que corta ambos. |
| D-119 | Tomada (2026-10-30) | **Superadministradora global.** `app_user.is_superadmin` booleano, **no** un rol; `roles` conserva su CHECK cerrado. **Exactamente una activa**; no puede ser desactivada, bloqueada ni degradada por una administradora de farmacia, solo por ella misma o por el CLI `set-superadmin` (uso único, auditado). **Excepción nombrada a D-037:** cruza organizaciones **solo** para metadatos administrativos (organizaciones, administradoras, roles, solicitudes de acceso); los datos operativos siguen acotados a la organización. Ampliar el cruce a datos operativos exige decisión nueva. |
| D-120 | Tomada (2026-10-30) | **Faltantes de Telegram en el Cotizador, con cobertura reversible.** Grilla «Productos faltantes (Telegram)» en la pantalla principal (orden: búsqueda, precios públicos, faltantes, ofertas, Mis cotizaciones). Reutiliza `GET /demand-items` con campos aditivos `last_raw_text`, `coverage_quantity`, `pending_quantity`, `is_pending` y filtro `pending_only=true`; **sin tabla ni endpoint nuevos**. «Buscar» usa la búsqueda principal y **no cambia** el estado del faltante. «Agregar a cotización» desde una fila **vincula la oferta al faltante sin confirmación adicional**, aunque sea otra marca o presentación; la clasificación y advertencias quedan visibles y **no se afirma equivalencia**. Vínculo solo si la petición lleva `demand_item_id`; una búsqueda libre no vincula nada. Cobertura **derivada, nunca almacenada**, de líneas activas de carritos activos de cotizaciones `draft`, **ampliada por D-121 a las compras `current` no anuladas, sin doble conteo**; quitar líneas, descartar carritos, anular o corregir compras recalcula y puede devolver el faltante a pendiente; varios proveedores suman; la cantidad desconocida no se inventa; el historial original de Telegram nunca se borra. `quote_line` gana `coverage_relation` (clasificación congelada). **P-8 resuelto por D-121.** |
| D-121 | Tomada (2026-10-30) | **Cobertura de faltantes con compras vigentes, sin doble conteo (resuelve P-8).** La cobertura suma (a) líneas activas de carritos activos de cotizaciones `draft` sin compra vigente y (b) compras `current` no anuladas. Una línea con compra vigente cuenta **solo** lo comprado; compra y cotización nunca se suman dos veces. Cerrar la cotización no quita cobertura. Anular o corregir una compra recalcula. Las recepciones **no** se suman aparte (los campos de recepción se retiran con D-126). **D-127 precisa el alcance:** la cotización cubre de forma **provisional** y **nunca** cierra; solo la compra confirmada cierra. Sin tabla, columna ni migración nuevas: todo es derivado. |
| D-122 | Tomada (2026-10-30); **retirada del MVP por D-126** | **Recepción física desde el Cotizador web, con parciales y diferencias.** Se agregan `receipt` y `receipt_line` (inmutables, de solo agregar) a la migración **0017**. Solo rol `admin` + `StrongActor`, **solo web**; Telegram nunca confirma recepciones. Se registran cantidades aceptadas y rechazadas, diferencias (`missing`, `wrong_product`, `damaged`, `other`) y responsable/fecha. Un producto equivocado **no** se confirma como recibido. Recepción parcial deja saldo pendiente y **no** declara completo. El faltante pasa a `fulfilled` solo si toda compra vigente está `complete` y la cobertura alcanza el objetivo (o la cantidad es desconocida); cerrar conserva todo el historial. Un faltante cerrado no se reabre: un nuevo aviso crea un nuevo ciclo. P-9 (anulación con recepciones), P-10 (compra que nunca llega) y P-11 (corrección de recepción) quedan **abiertos**. |
| D-123 | Tomada (2026-10-30) | **Estado por Telegram sin duplicar necesidades.** Un aviso repetido se registra como observación del mismo faltante `active`; no crea un segundo faltante ni suma cantidad. La respuesta informa el estado: pendiente, en cotización, comprado (con cantidades y, si es parcial, el saldo por comprar) o cerrado. Si ya hay compra, el bot dice «ya hay una compra registrada» y **no** afirma que llegó; ajustado por D-126 (sin estados de recepción). Cantidad adicional solo por `ADD`/`SET`/`CLEAR` explícito. Consulta `estado <texto>` determinista, sin IA, de solo lectura, con candidatos ante ambigüedad. **D-127:** la distinción aviso repetido / necesidad nueva depende del **ciclo** (mientras el faltante esté `active`), no de un plazo ni de la recepción; con cantidad desconocida la necesidad no se cierra por un aviso. Después de `fulfilled`, un nuevo aviso crea un nuevo faltante. |
| D-124 | Tomada (2026-10-30) | **Las personas con rol `admin` también informan faltantes por Telegram.** Se agrega `REPORT_SHORTAGE` al rol `admin` en la matriz de permisos; no se crea un rol nuevo ni se fusionan planos. Para usar Telegram, una administradora necesita su propia cuenta `telegram_account` `active` e identidad vigente, igual que un auxiliar. Los permisos web y la autorización de Telegram siguen siendo independientes: tener `admin` no habilita el bot, y tener Telegram no otorga roles web. Sin exclusión alguna que impida a `admin` reportar. |
| D-126 | Tomada (2026-10-30) | **La recepción física y las diferencias de entrega quedan fuera del MVP/demo.** El objetivo es demostrar solicitud de faltantes, búsqueda, comparación, cotización y compra; no un sistema de inventario ni de recepción. Se retiran del alcance implementable: confirmación de recepción, recepciones completas o parciales, cantidades entregadas, diferencias y productos equivocados, saldos pendientes de recepción, correcciones/anulaciones de recepción, tablas `receipt`/`receipt_line`, endpoints, permisos, validaciones y pantallas asociados, y cierre condicionado a recepción. Subsiste D-121: la cobertura suma líneas activas de cotización y compras confirmadas, sin duplicar cantidades; una compra confirmada **no certifica llegada física**, solo marca el término del proceso que administra el MVP. **D-127** precisa que lo cotizado cubre de forma provisional y nunca cierra: la necesidad pasa a `fulfilled` solo cuando las compras confirmadas cubren toda la cantidad pedida conocida. C5-B vuelve a **3 tablas** (`purchase`, `purchase_line`, `purchase_index`) y v0.2 a **9 tablas**. P-9/P-10/P-11 dejan de bloquear D0 o la demo y se conservan como funcionalidad futura. D-122 y A10 quedan retiradas; D-123 y D-124 se mantienen con la redacción ajustada. |
| D-127 | Tomada (2026-10-30); **compras parciales fuera de la demo por enmienda del 2026-11-02** | **Regla definitiva de cierre del ciclo de reposición y compras parciales.** Una necesidad se considera gestionada cuando la cantidad solicitada queda **completamente cubierta por compras confirmadas**; `fulfilled` no significa recibido físicamente. Cubrir con **cotización** es provisional —mientras la línea siga en un carrito activo— y **nunca** cierra el ciclo. Compra parcial (10 pedidas, 6 compradas): las 6 quedan cubiertas, las 4 restantes siguen como necesidad abierta **en el mismo faltante** (`purchased`, `pending_purchase_quantity = 4`), sin crear una solicitud duplicada, y se cotizan y compran sobre ese faltante hasta cerrar. `is_pending` significa «el ciclo sigue abierto» y no se apaga por cotizar; se distinguen `pending_quantity` (falta
**cotizar**) y el derivado `pending_purchase_quantity` (falta **comprar**), sin tabla ni endpoint nuevos. La distinción aviso repetido / ciclo nuevo depende del **ciclo** (`active` vs. `fulfilled`), no de un plazo ni de la recepción. Cantidad desconocida: no se cierra automáticamente; sale del ciclo solo por `ADD`/`SET` explícito que declare la cantidad o por anulación con motivo, nunca inventando una cantidad ni una confirmación de recepción. Sin migración ni código. |
| D-125 | Tomada (2026-10-30) | **Tres tablas de búsqueda en C4 y numeración definitiva.** C0 nombra dos tablas (`search_run`, `search_source_result`); el diseño aprobado usa tres: se agrega `search_result` (filas de resultado por producto) como **ampliación aditiva**, sin editar C0. Las tres se crean en la migración **0013**. Se ratifica **C4 = 0013** y **C3 = 0014**; no se invierte el orden. |

### D-115 — Consulta por cadena, captura y vigencia (2026-10-30)

**Alcance.** D-113 aprueba el contrato de búsqueda; D-115 fija **qué se puede consultar y cómo**, sin construir
conectores. Una cadena solo se habilita si existe un mecanismo **viable y permitido**: API pública, HTML de servidor
sin desafío, o autorización expresa. Si no, se informa `not_configured` con motivo y **no se inventa** nada.

**Consulta.** Bajo demanda, sin cron y sin espejo del catálogo. Una consulta por cadena, en paralelo. Límites: **4 s
por fuente**, **8 s global**, TTL **10 min**, máximo **50 búsquedas vivas** por proceso y **un worker** (el estado
está en memoria; reiniciar pierde las búsquedas en vuelo). Códigos: `SEARCH_NOT_FOUND` (404) y `RATE_LIMITED` (429 con
`Retry-After`). No se usan 502/504 para cadenas.

**Estado inicial y fundamento.**

| Cadena | Estado | Motivo | Fundamento |
|---|---|---|---|
| Cruz Verde | `not_configured` | `requires_javascript` | `robots` permite `/search`, pero los resultados se cargan por JavaScript; no hay conector autorizado |
| Salcobrand | `not_configured` | `search_not_available` | La búsqueda requiere JavaScript y `robots` no permite `/api`; no hay conector autorizado |
| Farmacias Ahumada | `not_configured` | `access_not_authorized` | reCAPTCHA; términos versión 13.10.23 **no autorizan ni prohíben** el acceso automatizado; la captura automatizada **no está autorizada** |

**Política de captura.** No se usan datos ficticios, precargados ni históricos como sustituto. No se extrae en
volumen, no se eluden CAPTCHA/MFA, no se simula un navegador para saltar protecciones. Si se habilita una cadena, el
cliente se identifica honestamente, respeta límites de tasa y consulta **solo bajo demanda**.

**Farmacias Ahumada, dos caminos, ninguno ejecutado:**

- **A1:** autorización escrita o API del titular.
- **A2:** decisión explícita de riesgo de negocio, registrada en D-113 con **fecha, fundamento jurídico, condición de
  revocación, consultas limitadas, sin extracción masiva, sin eludir protecciones, identificación honesta y límite de
  tasa**.

Sin A1 o A2, el conector **no se construye**. Lo mismo para cualquier cadena sin autorización.

**Vigencia.** `observed_at` es de **esta** consulta. Al agregar o revisar una oferta en una cotización se repite la
consulta con `purpose: "quote_verification"`; **solo** el precio de esa verificación puede mostrarse como vigente. Si
la fuente falla, se muestra el fallo; nunca se reutiliza un precio anterior como actual. El precio público no entra
en `offer_snapshot`, carritos, totales ni elegibilidad.

**No autoriza:** construir conectores, capturar datos, crear tablas o migraciones, modificar C0/E1/E3, ni tocar el
frontend.

### D-116 — Observaciones comerciales y aprendizaje O1–O4 (2026-10-30)

**Motivo.** Pao quiere dejar constancia de observaciones libres («este medicamento no me gusta por el laboratorio»,
«este proveedor despacha tarde») y que el sistema las recuerde y las explique al cotizar. Un campo único de nota por
línea se sobrescribe, mezcla fechas y autores, y no permite mostrar que una observación posterior supera a otra sin
borrar la primera. Por eso se separan **observación** (hecho escrito por Pao) e **interpretación** (sugerencia de IA).

**`commercial_observation` (inmutable, solo agregar).** `text_original`, `author_id`, `created_at`, `quote_id`,
`quote_line_id`, `trade_item_id`, `offer_snapshot_id`, `supplier_id`, `context` jsonb con las etiquetas **tal como se
vieron** (producto, presentación, laboratorio/marca, proveedor, oferta, cotización) y `data_origin='captured'`.
Trigger rechaza `UPDATE` y `DELETE`. No hay edición, borrado ni reemplazo. La línea nunca se borra físicamente, así
que no hay huérfanas. El contexto no convierte `laboratory_as_published` en laboratorio confirmado.

**`observation_interpretation` (contenido inmutable, revisión controlada).** `stance` (`favor|caution|neutral`),
`subject_kind` (`article|brand|supplier`), `subject_label`, `temporality`, `confidence`, `model`, `prompt_version`,
tokens de entrada/salida, `estimated_cost_usd`, `latency_ms`, `data_origin='ai_derived'`, `status`
(`suggested|confirmed|rejected`), `reviewed_by/at`, `review_note`. **Contradicción resuelta:** el contenido no se
edita ni se borra; **solo** los campos de revisión cambian, por función restringida y auditada, con transición
`suggested → confirmed | rejected`, **sin retorno**. Varias interpretaciones por observación son válidas; la más
reciente revisada gana y las anteriores se muestran como superadas, nunca se ocultan.

**O1–O4.**

| Etapa | Qué hace | Fase / migración |
|---|---|---|
| O1 | Guardar y mostrar la observación con autor, fecha, texto original y contexto | C5-A / 0015 |
| O2 | Interpretar con IA y guardar la sugerencia estructurada | O1–O4 / 0016 |
| O3 | Recuperar **solo** observaciones del artículo exacto; la más reciente para el mismo sujeto y clase gana; las anteriores se muestran superadas | En vivo, sin tabla |
| O4 | Devolver `advice` con `stance`, `text`, `based_on[]`, `supersedes[]` e `interpretation_status` | En vivo, en la cotización |

**Precedencia (O3).** Solo `trade_item_id` exacto; nunca por nombre, marca o laboratorio parecido. Para el mismo
sujeto y clase de consejo, gana la más reciente (`created_at`, desempate por `id`). Las anteriores no se borran. Una
observación de marca o proveedor **no se aplica** a otro artículo de la misma marca. Sin observaciones, `advice` es
`null`; no se rellena con texto genérico.

**OpenAI, Responses API, solo desde el backend.** Lovable nunca ve la clave ni llama al proveedor.

| Configuración | Tipo | Regla |
|---|---|---|
| `OPENAI_API_KEY` | `SecretStr` | Obligatoria para O2–O4; si falta, O1 sigue operativo y el consejo dice «sin interpretación» |
| `OPENAI_MODEL` | texto | **Sin valor por defecto inventado**; vacío ⇒ O2–O4 deshabilitados |
| `OBSERVATION_AI_ENABLED` | booleano | Interruptor de despliegue; por defecto `false` |
| `OPENAI_MAX_OUTPUT_TOKENS` | entero | Cota por solicitud |
| `OPENAI_TIMEOUT_SECONDS` | entero | Al vencer, la observación se guarda sin interpretación |
| `OPENAI_MONTHLY_BUDGET_USD` | decimal | Tope mensual; al alcanzarlo no se llama y se informa |
| `OPENAI_PROMPT_VERSION` | texto | Versión del prompt, guardada en cada interpretación |
| `OPENAI_INPUT_USD_PER_1M`, `OPENAI_OUTPUT_USD_PER_1M` | decimal | Tarifa configurable para **estimar** el costo; no se hardcodea un precio |

**Reglas de llamada.** Salida estructurada validada contra JSON Schema estricto; si no valida, se descarta y se
registra el fallo. Se guardan modelo, prompt, tokens, costo estimado y latencia. Timeout y **máximo un reintento**.
Presupuesto mensual y tope por lote antes de cada llamada. Si el proveedor falla, la observación se guarda igual y la
respuesta dice `advice_status: "sin_interpretacion"` con motivo visible; **nunca se sustituye por reglas fingidas**.
La clave vive en `backend/.env` (fuera de git), la carga `core/config.py` y **nunca** se escribe en logs, respuestas,
auditoría ni frontend.

**Prohibiciones de la IA.** No modifica precio, stock, condiciones, cantidad, moneda, totales ni elegibilidad; no crea,
quita, oculta, reordena ni destaca ofertas; no otorga permisos; no afirma equivalencia farmacológica; no aplica una
observación a otro artículo por parecido; no borra ni edita observaciones.

**Interfaz sin cambios.** Botón «Observación», campo de texto libre, Guardar/Cancelar. Sin categorías, selectores ni
formularios adicionales.

**Aceptación.** Dos observaciones contradictorias en fechas distintas; la cotización posterior cita la más reciente y
marca la anterior como superada sin borrarla; el texto original queda visible; **las ofertas, precios, stock y totales
son exactamente los mismos** que sin observaciones; una observación no se aplica a otro artículo de la misma marca;
si la IA falla o está deshabilitada, la observación se guarda y el consejo dice «sin interpretación».

**No autoriza:** contratar OpenAI, fijar modelo, tarifa o presupuesto, ni activar O2–O4. Sin clave y autorización de
costo, solo O1 queda operativo.

### D-117 — Acceso web con Google, solicitudes y niveles (2026-10-30)

> **Cláusula `roles = []` superada por D-128 y luego por D-130.** Hoy aprobar crea `roles = ['admin']` (D-130). Las
> tres entidades de esta decisión —Google autentica y la base autoriza, el flujo por solicitud y campanita, y la
> separación entre niveles técnicos y funcionales— siguen vigentes.

**Separación.** Google/Supabase **autentica**; nuestra base **autoriza**. Un JWT válido no basta: debe existir
`app_user` activo, con `auth_subject` enlazado y acceso vigente.

**Tres niveles.**

| Nivel | Quién | Puede | No puede |
|---|---|---|---|
| **Superadministradora** | Yarella Fernández | Administración global, organizaciones, administradoras y permisos globales | Ser revocada o degradada por una administradora de farmacia |
| **Administradora de farmacia** | Pao | Gestionar usuarios de **su** farmacia; aprobar, rechazar y revocar accesos; usar el cotizador | Privilegios globales; otras organizaciones |
| **Usuario autorizado** | Personas con Google | Solo las funciones de sus roles | Administración automática por tener cuenta Google |

Los nombres son personas, no código. **No se crea un rol nuevo**; `roles` sigue con sus cuatro valores.

**Flujo.** «Continuar con Google» → JWT → validación de firma, emisor, audiencia y expiración → búsqueda por
`auth_subject` activo y vigente. Si no existe o no está enlazado → `403 USER_NOT_PROVISIONED` y la pantalla ofrece
«Solicitar acceso» → `POST /api/v1/web-access-requests` crea una solicitud `pending` en la organización **configurada
en el servidor** → campanita de Pao → aprobar o rechazar. **Aprobar no asigna roles:** crea o enlaza `app_user` con
`roles = []` y `active = true`; el acceso queda limitado a lo que un usuario sin roles puede ver. Los roles se
otorgan después, por la superadministradora.

**Pao, Danny y Julio** se habilitan previamente con `create-admin` / `link-auth-subject` (D-071/D-072) y entran con
Google sin pasar por la campanita. La campanita es para solicitudes nuevas o excepcionales.

**Endpoints.**

| Método y ruta | Quién | Para qué |
|---|---|---|
| `POST /api/v1/web-access-requests` | JWT válido, sin `app_user` | Crear solicitud; cuerpo `{}`; organización desde configuración; idempotente por `(organization_id, auth_subject)` pendiente |
| `GET /api/v1/web-access-requests` | `admin` de la organización | `{pending_count, items:[{id,email,requested_at}]}` |
| `POST /api/v1/web-access-requests/{id}/approve` | `admin` de la organización | Aprueba y crea/enlaza con `roles = []` |
| `POST /api/v1/web-access-requests/{id}/reject` | `admin` de la organización | Rechaza con motivo opcional; no crea usuario |
| `POST /api/v1/admin/users/{user_id}/web-access` | Superadministradora | Otorga o cambia roles y `expires_at`; solo roles existentes; auditado |
| `POST /api/v1/admin/users/{user_id}/web-access/revoke` | Superadministradora | Revoca **solo la web** (`expires_at = now`); no toca Telegram ni `active` |
| `GET /api/v1/me` | Cualquier autenticado | Identidad, organización, roles, `is_superadmin`, vencimiento |

**Seguridad del endpoint público.** Mismo validador de JWT; del token solo `sub`, `email` y nombre; se exige
`email_verified`; organización desde configuración, **nunca** del cuerpo; límite de tasa por `sub` y por IP; **misma
respuesta** exista o no cuenta o solicitud; no se revela el correo registrado ni la organización. La solicitud **no
otorga nada**. Solo la aprobación habilita, y solo un `admin` de esa organización puede aprobar. Aprobar es
transaccional y se audita en `audit_event`.

**Campanita.** Check verde (aprobar) y cruz roja (rechazar), sin formularios adicionales. Al decidir, la solicitud
desaparece y el contador baja; sin pendientes: «No hay solicitudes pendientes». La decisión **permanece auditada**.
La superadministradora ve todas las organizaciones, con la organización indicada; una administradora de farmacia ve
solo la suya.

**Columnas nuevas de `app_user`.** `email`, `is_superadmin` (booleano, **no** rol), `web_access_expires_at`,
`web_access_granted_by`, `web_access_granted_at`. El vencimiento se evalúa en **cada** petición → `403
WEB_ACCESS_EXPIRED`; no hay job en segundo plano. Revocar la web no borra la persona ni su historial y no bloquea
Telegram; `active = false` es una suspensión más fuerte que corta ambos.

**Protección de la superadministradora.** Exactamente una activa; no puede ser desactivada, bloqueada ni degradada por
una administradora de farmacia; solo por ella misma o por `set-superadmin` (CLI de uso único, auditado, no ejecutado
en D0). `SUPERADMIN_PROTECTED` (403) si se intenta.

**Sin contraseña ni registro público** (D-066). El alta es por Google + solicitud aprobada, o por
`create-admin`/`link-auth-subject`.

**Nombre.** Se usa `web_access_request` / `/web-access-requests`, **no** `access_request`, para no confundirlo con
`telegram_access_request` (D-118).

**Vigente con la enmienda de D-128 (2026-10-30).** La cláusula «aprobar no asigna roles: `roles = []`», el endpoint
`approve` con `roles = []` y la frase «los roles se otorgan después, por la superadministradora» quedan **sustituidos**:
aprobar crea el `app_user` con `roles = ['purchasing']` y `auth_subject` en el mismo paso. Lo demás de D-117 sigue
intacto: solicitud sin acceso, misma respuesta pública, límite de tasa, organización desde configuración, cinco
columnas de `app_user`, vencimiento evaluado en cada petición, superadministradora como bandera y `set-superadmin` de
uso único. Aprobar **no** otorga `admin`, `is_superadmin`, permisos `MANAGE_*` ni acceso a Telegram.

### D-118 — Control individual de Telegram (2026-10-30)

**Decisión.** Pao habilita o deshabilita **por cuenta** quién puede usar Telegram para enviar faltantes. **No hay un
interruptor global del bot.** Se reutiliza lo existente; no hay tabla ni migración nuevas.

| Método y ruta | Efecto |
|---|---|
| `GET /api/v1/telegram-accounts` | Persona, chat, tipo (`personal`/`shared_device`), estado y última modificación |
| `POST /api/v1/telegram-accounts/{id}/block` | `active → blocked`, motivo obligatorio; revoca sesiones (`end_reason='revoked'`), desactiva vínculos y **conserva el historial** |
| `POST /api/v1/telegram-accounts/{id}/reactivate` | `blocked → active`; reutiliza la **misma fila**; recrea el vínculo privado sin nueva solicitud ni código |
| `GET /api/v1/telegram-accounts/{id}/history` | Historial de la cuenta, incluidos bloqueos y rehabilitaciones |

Requieren `MANAGE_ACCOUNTS` + `StrongActor`; en organización real, JWT de Supabase. El backend valida el acceso en
**cada** mensaje entrante (`resolve_access`): una cuenta `blocked` no opera aunque tenga sesión previa. Bloquear no
borra mensajes, observaciones, faltantes ni vínculos. Se audita `channel.account_blocked` /
`channel.account_reactivated` con actor, motivo y fecha.

**Límites.** El control es **por cuenta**, no por chat. Una cuenta `shared_device` se deshabilita para **todas** las
personas de ese teléfono; el control individual exige cuentas `personal` (configuración actual). **Telegram y web son
independientes:** una persona puede tener Telegram sin web y viceversa; los auxiliares solo usan Telegram y no cotizan
ni entran al Cotizador web. Bloquear Telegram no revoca la web; revocar la web no bloquea Telegram;
`app_user.active = false` es la suspensión fuerte que corta ambos. El token del bot sigue del lado del servidor y
nunca se muestra en esta pantalla.

**Fase.** La pantalla «Configuración de Telegram» es **T1**, sin migración.

### D-119 — Superadministradora global (2026-10-30)

**Decisión.** `app_user.is_superadmin` booleano, **no** un rol. `roles` conserva su CHECK cerrado
(`auxiliary|purchasing|approver|admin`); agregar `superadmin` a `roles` sería ambiguo con el modelo existente.

**Reglas.**

1. **Exactamente una superadministradora activa.**
2. No puede ser desactivada, bloqueada ni degradada por una administradora de farmacia; solo por ella misma o por el
   CLI `set-superadmin`, de uso único y auditado.
3. **Excepción nombrada a D-037:** puede cruzar organizaciones **solo** para metadatos administrativos
   (organizaciones, administradoras, roles, solicitudes de acceso). **Los datos operativos siguen acotados a la
   organización.**
4. Ampliar el cruce a datos operativos exige una **decisión nueva**; no se hace por conveniencia.
5. `SUPERADMIN_PROTECTED` (403) si una administradora de farmacia intenta desactivarla o degradarla.

**No autoriza:** implementar el CLI, aplicar la columna o crear la superadministradora en una organización real. Eso
es G1, con autorización de migración.

### D-120 — Faltantes de Telegram en el Cotizador y cobertura reversible (2026-10-30)

> **Vigencia:** este registro conserva el texto original. La cobertura fue ampliada por **D-121** (compras vigentes)
> y la fórmula, los saldos y el cierre fueron fijados por **D-127** (`pending_quantity` = falta cotizar,
> `pending_purchase_quantity` = falta comprar, cierre solo por compra confirmada); prevalece el contrato §12.

**Decisión.** Los faltantes que llegan por Telegram se muestran y gestionan en el Cotizador, sin duplicar su historia
ni convertirlos en otra entidad.

**Pantalla.** Grilla «Productos faltantes (Telegram)» en la pantalla principal. Orden acordado: 1 búsqueda, 2 precios
públicos de cadenas, 3 faltantes, 4 ofertas de proveedores, 5 Mis cotizaciones. Columnas: nombre/descripción,
cantidad pedida **solo si fue declarada**, quién lo pidió, fecha/hora, **estado de reposición** (§12.6: pendiente, en
cotización o comprado) y botón «Buscar».

**Listado sin tabla nueva.** Se reutiliza `GET /api/v1/demand-items` con campos aditivos `last_raw_text`,
`coverage_quantity`, `pending_quantity`, `is_pending` y filtro `pending_only=true`. No se crea endpoint de listado ni
tabla de cobertura.

**«Buscar».** Usa la búsqueda principal (`POST /api/v1/searches` + búsqueda de cadenas). **No cambia el estado del
faltante.** La búsqueda principal es independiente de Telegram.

**Vínculo.** `quote_line.demand_item_id` (nullable) + `coverage_relation` (clasificación congelada: `exact`,
`other_presentation`, `combined`, `related`, `needs_review`). **Sin botón de confirmación adicional:** «Agregar a
cotización» desde una fila de faltante ya es la acción explícita. Se vincula aunque sea otra marca o presentación; la
clasificación y las advertencias quedan visibles; **no se afirma equivalencia farmacológica**. El vínculo solo se
crea si la petición lleva `demand_item_id`; una búsqueda libre no vincula nada.

**Fórmula de cobertura (derivada, nunca almacenada).**

```
covered(item) = SUM(l.quantity)
  FROM quote_line l
  JOIN quote_cart c ON c.id = l.quote_cart_id
  JOIN quote q      ON q.id = c.quote_id
 WHERE l.demand_item_id = item.id
   AND l.organization_id = item.organization_id
   AND l.status = 'active'
   AND c.status = 'active'
   AND q.status = 'draft'

if target_quantity IS NOT NULL:
    pending_quantity = max(target_quantity - covered, 0)
    is_pending       = covered < target_quantity
if target_quantity IS NULL:
    pending_quantity = NULL
    is_pending       = (covered = 0)
```

**Reglas de reversibilidad.**

1. Buscar no cambia el estado.
2. Agregar cantidad suficiente saca el faltante de pendientes.
3. Una cobertura parcial deja saldo pendiente.
4. Varios proveedores suman.
5. Quitar una línea resta cobertura y puede devolver el faltante a pendiente.
6. Descartar un carrito resta toda su cobertura; reincorporarlo la devuelve.
7. Cambiar la cantidad recalcula; una cantidad menor puede devolverlo a pendiente.
8. Si otro carrito activo cubre el faltante, sigue fuera; si ninguno lo cubre, vuelve solo.
9. El historial original de Telegram **nunca se borra**; no se escribe en `demand_item` ni en `demand_observation`.
10. `target_quantity` y `quantity_event` siguen siendo datos del faltante; la cobertura solo los lee.

**Clasificación `relation` (aditiva a E1).** Se agrega `relation` junto a `match.level`, sin cambiar este último:
`exact` (misma identidad comercial: ingrediente/marca + potencia + forma + contenido), `other_presentation` (misma
identidad farmacéutica, distinto contenido/envase), `combined` (`attributes.ingredients[]` contiene el ingrediente
buscado y al menos otro), `related` (texto/marca/línea, identidad distinta) y `needs_review` (identidad incompleta o
ambigua). No filtra ni oculta: solo ordena y advierte. `combined` nunca se presenta como equivalente. Productos no
farmacéuticos se clasifican por marca + línea + tamaño. `demand_item.ingredient` es una cadena NOT NULL y puede
contener una frase de producto; no se muestra como «principio activo» ni se modifica la columna. Para combinados se
usa `attributes.ingredients[]` (JSONB existente, sin migración); si falta, `needs_review`.

**Cobertura con compras (P-8, resuelto por D-121).** La fórmula de D-120 se amplía: además de las líneas activas de
cotizaciones `draft`, cubren las compras `current` no anuladas, **sin doble conteo**. Una línea con compra vigente
cuenta lo comprado, no lo cotizado. Cerrar una cotización comprada ya no devuelve el faltante a pendiente. La
reversibilidad se mantiene: anular o corregir una compra recalcula. El detalle está en D-121 y en el contrato §12.3.

### D-121 — Cobertura con compras vigentes (2026-10-30)

**Problema.** D-120 contaba solo líneas activas de cotizaciones `draft`. Al cerrar una cotización comprada, sus líneas
dejaban de contar y el faltante podía reaparecer como pendiente aunque la compra existiera (P-8).

**Regla.** La cobertura es derivada y suma dos fuentes, sin doble conteo:

1. líneas activas de carritos activos de cotizaciones `draft` **sin compra vigente**;
2. compras `current` no anuladas, esté la cotización `draft` o `closed`.

Una línea con compra vigente cuenta **solo** lo comprado. **Corrección por D-126:** no existen campos de
recepción; la cobertura mide el proceso administrativo, no la llegada física. **Corrección por D-127:** las dos
fuentes no valen lo mismo al cerrar; lo cotizado cubre de forma **provisional** y nunca cierra el ciclo (§12.3).

**Reversibilidad.** Quitar una línea, descartar un carrito, anular una compra o corregirla recalcula. Si el saldo
vuelve a quedar sin compra confirmada, se muestra como pendiente de compra (`is_pending: true`); la operación no
cierra ni reabre un ciclo por sí sola. Una compra corregida sustituye; nunca se suman original y
corrección. Una compra sin línea cotizada (`quote_line_id = null`) no cubre ningún faltante.

**Sin migración.** Todo es derivado; no se escribe en `demand_item` ni se crea tabla de cobertura. La recepción que
proponía D-122 fue **retirada del MVP por D-126**.

### D-122 — Recepción física, parciales y cierre del ciclo (2026-10-30) — **retirada del MVP por D-126**

> **Estado:** el diseño se conserva como **funcionalidad futura**. Desde D-126 (2026-10-30) la recepción física y las
> diferencias de entrega **no forman parte del MVP ni de D0**: no hay `receipt`, `receipt_line`, endpoints, permisos
> ni cierre condicionado. Las preguntas P-9, P-10 y P-11 dejan de ser bloqueos y quedan como materia de una fase
> futura.

**Alcance.** La recepción de mercadería entra en v0.2. No es una tercera excepción a C0-10: documenta un hecho ya
ocurrido, no ejecuta una orden ni un pago. `purchase_execution` sigue `blocked`.

**Tablas.** `receipt` (entrega confirmada, inmutable) y `receipt_line` (cantidad aceptada, rechazada, diferencia y
nota), ambas en la migración **0017** junto con las compras.

**Permisos.** Rol `admin` + `StrongActor`, **solo desde la web**. Pao, Danny y Julio pueden confirmar recepciones
completas o parciales, registrar cantidades reales y diferencias; los auxiliares y cualquier operación por Telegram
quedan excluidos. La superadministradora no confirma en otra organización.

**Reglas.**
- Recepción parcial: se guarda lo recibido; el saldo queda pendiente; **no** se declara completo.
- Producto equivocado o dañado: `received_quantity = 0`, `rejected_quantity > 0`, `issue` y nota; el producto pedido
  **no** se marca recibido y no se crea una compra nueva.
- `receipt_status` por compra (`none|partial|complete`) y `pending_receipt_quantity` son **derivados**; no se
  almacenan ni contradicen la compra inmutable.
- El faltante pasa a `fulfilled` solo cuando **toda** compra vigente está `complete` **y** la cobertura alcanza el
  objetivo (o la cantidad es desconocida). Una recepción completa de una compra que no cubre el objetivo **no**
  cierra el faltante.
- Cerrar conserva historial, observaciones, mensajes, líneas, compras y recepciones. Un faltante cerrado no se reabre:
  un nuevo aviso crea un nuevo ciclo.
- Anular una compra con recepciones está **bloqueado** (`409 RECEIPTS_EXIST`) mientras P-9 no se resuelva.
- **P-9, P-10 y P-11 quedan abiertos:** anulación/corrección con recepciones, compra que nunca llega o recepción
  incompleta definitiva, y corrección de una recepción errónea. No se inventa su solución.

### D-123 — Estado por Telegram sin duplicar necesidades (2026-10-30)

- Un aviso repetido se registra como observación del **mismo** faltante `active`; no crea otra necesidad ni suma
  cantidad.
- La respuesta informa el estado: pendiente, en cotización, comprado (con cantidades y, si el ciclo sigue abierto, el
  saldo por comprar) o cerrado. Si ya hay compra, dice «ya hay una compra registrada» y **nunca** afirma que llegó.
- **Ajuste por D-126:** con la recepción fuera del MVP, el bot no informa recepción parcial ni saldos de recepción;
  después de la compra solo informa las cantidades compradas.
- **Ajuste por D-127:** la distinción entre aviso repetido y necesidad nueva depende del **ciclo de reposición**
  (mientras el faltante esté `active`), no de un plazo arbitrario ni de la recepción física. Con la **compra todo o
  nada** (enmienda del 2026-11-02), una compra confirmada cierra el ciclo; si el ciclo sigue abierto —cantidad
  desconocida o corrección—, el bot informa cuánto falta por comprar.
- Cantidad adicional solo con `ADD`/`SET`/`CLEAR` explícito y el permiso correspondiente; no hay suma implícita
  (AC35).
- **Cantidad desconocida:** el bot informa que la necesidad sigue abierta; no la cierra, no inventa la cantidad ni
  una confirmación de recepción (V-06).
- Consulta `estado <texto>`: determinista, sin IA, de solo lectura, con candidatos ante ambigüedad. También se puede
  ver desde «Ver lista».
- Después de `fulfilled`, un nuevo aviso crea un nuevo faltante. La consulta no crea, no modifica y no cierra nada.
- **Telegram no registra compras ni confirma llegadas** (D-126); el bot solo lee y sugiere.

### D-124 — `admin` también informa faltantes por Telegram (2026-10-30)

- Se agrega `REPORT_SHORTAGE` al rol `admin` en `app/core/permissions.py`. **No se crea un rol nuevo** ni se fusionan
  planos.
- Para reportar, una administradora necesita su propia cuenta de Telegram `active` e identidad vigente, igual que un
  auxiliar. Tener rol `admin` **no** habilita el bot; tener Telegram **no** otorga roles web.
- Pao, Danny y Julio pueden informar un faltante, y luego entrar a la web a cotizarlo y confirmar la compra
  (la recepción quedó fuera del MVP, D-126).
- No se agrega ninguna exclusión que impida a `admin` participar del flujo de Telegram.
- **Este cambio de matriz no se implementa en D0**; queda registrado para la fase que habilite T1/C5-A.

### D-125 — Tres tablas de búsqueda y numeración definitiva (2026-10-30)

- C0 nombra `search_run` + `search_source_result`; el diseño aprobado de C4 usa **tres** tablas: `search_run`
  (corrida y cobertura), `search_source_result` (estado por fuente) y `search_result` (resultados por producto).
- `search_result` es una **ampliación aditiva** de C0: no se edita C0, no se cambia E1–E9 y no se implementa en D0.
- Las tres se crean en la migración **0013**. **C4 = 0013** y **C3 = 0014** quedan ratificados; no se invierte el
  orden.

### D-126 — Recepción física fuera del alcance del MVP (2026-10-30)

**Decisión.** El MVP/demo demuestra **solicitud de faltantes, búsqueda, comparación de proveedores, cotización y
compra**. No es un sistema de inventario ni de control de recepción de mercadería. Por eso se retira del alcance
implementable todo el control de recepción física y de diferencias de entrega.

**Qué se retira.** Confirmación de recepción; recepciones completas o parciales; control de cantidades efectivamente
entregadas; diferencias de entrega y productos equivocados; saldos pendientes de recepción; correcciones y anulaciones
de recepciones; tablas `receipt` y `receipt_line`; endpoints, permisos, validaciones y pantallas asociados; y cierre
de faltantes condicionado a la recepción.

**Qué se mantiene (D-121).** La cobertura de un faltante considera las **líneas activas de cotización** y las
**compras confirmadas** (`current`, no anuladas), **sin duplicar cantidades**. Una compra confirmada marca el término
del proceso que administra el MVP y **no certifica** que los productos hayan llegado físicamente; el sistema no afirma
ni niega la llegada.

**Ciclo resultante.**

1. Un usuario autorizado informa un faltante por Telegram (p. ej. 10 cajas).
2. Pao, Danny o Julio incorporan la cantidad a una cotización.
3. El faltante queda cubierto mientras la línea permanezca en un carrito activo.
4. Un administrador confirma la compra.
5. Lo comprado cubre la necesidad y la cotización puede cerrarse **sin** que el faltante reaparezca.
6. Si el producto vuelve a faltar más adelante, cualquier usuario autorizado informa una nueva necesidad y comienza
   otro ciclo; el cierre conserva el historial y no se reabre el aviso anterior.

**Consecuencias.**
- `receipt` y `receipt_line` salen de 0017: C5-B vuelve a **3 tablas** (`purchase`, `purchase_line`,
  `purchase_index`) y v0.2 a **9 tablas**.
- No hay estados ni campos de recepción; `replenishment_state` es `pending | in_quote | purchased | fulfilled |
  cancelled`.
- Un faltante con cantidad pedida **conocida** pasa a `fulfilled` solo cuando las **compras confirmadas** cubren el
  objetivo; la cotización **no** cierra nada (D-127). Con cantidad desconocida no se cierra automáticamente (no se
  inventa la cantidad).
- Se retiran los códigos `RECEIPTS_EXIST`, `PURCHASE_NOT_RECEIVABLE`, `PURCHASE_ALREADY_RECEIVED`,
  `RECEIPT_INVALID`, `RECEIPT_LINE_MISMATCH`, `RECEIPT_UNIT_MISMATCH` y la advertencia `OVER_RECEIPT`.
- **P-9, P-10 y P-11 dejan de bloquear D0, C5-B y la demo**; se conservan como preguntas de una fase futura.
- **Telegram (D-123, D-124) sigue vigente** con redacción ajustada: auxiliares autorizados y los tres
  administradores informan faltantes y consultan estados; si ya hay compra, el bot dice «ya hay una compra
  registrada» sin afirmar recepción; nunca crea una segunda necesidad duplicada ni aplica cantidades. Cotizar y
  confirmar compras siguen siendo operaciones web de `admin` + `StrongActor`.
- El diseño de recepción de **D-122** se conserva como **funcionalidad futura**, claramente separado del MVP
  (contrato §15.1); no se promete en la interfaz.

**Sin migración ni código.** D-126 es solo documental: no crea ni elimina migraciones, no toca la base y no modifica el
mockup.

### D-127 — Regla definitiva de cierre y compras parciales (2026-10-30) — **compras parciales fuera de la demo (enmienda 2026-11-02)**

**Problema.** Las revisiones 4 y 5 cerraban el ciclo con la **cobertura total** (cotizado sin compra + compras
vigentes). Eso permitía que un carrito en preparación diera por gestionada una necesidad que nunca se compró, y no
decía qué pasa cuando se compra solo una parte de lo pedido.

**Decisión.** Para este MVP, una necesidad de reposición se considera **gestionada cuando la cantidad solicitada queda
completamente cubierta por compras confirmadas**. Nada más cierra el ciclo.

**Ciclo.** 1) Danny informa por Telegram que faltan 10 cajas. 2) Pao las incorpora a una cotización. 3) Mientras están
en el carrito la necesidad está **cubierta provisionalmente**, pero **no** cerrada. 4) Pao confirma la compra de las
10. 5) La necesidad queda `fulfilled` y el historial conserva la compra. 6) Desde ese momento, cualquier usuario
autorizado puede informar paracetamol otra vez y comienza una necesidad nueva. **No se requiere confirmación de
recepción física.**

**Compras parciales.** Si se pidieron 10 y se compraron 6: las 6 quedan cubiertas; las 4 restantes siguen como
necesidad abierta **en el mismo faltante**; la solicitud original **no** se cierra; el sistema permite cotizar y
comprar las 4 sin crear una solicitud duplicada. Las cantidades en carritos activos pueden cubrir provisionalmente,
pero **no** provocan el cierre definitivo.

**Estados (sin cambios de nombres).** `pending` (pendiente), `in_quote` (cubierto provisionalmente por cotización
activa), `purchased` (hay compra confirmada que aún no cubre toda la cantidad pedida), `fulfilled` (cantidad pedida
completamente cubierta por compras confirmadas; ciclo cerrado), `cancelled`. `fulfilled` **no** significa recibido
físicamente.

**Avisos repetidos.** Mientras exista una necesidad abierta del producto, el bot la reconoce e informa su estado sin
crear otra solicitud idéntica. Después de `fulfilled`, un aviso nuevo inicia otro ciclo. La distinción depende del
**ciclo**, no de un plazo ni de la recepción. Con cantidad desconocida no se cierra automáticamente: la necesidad
sigue abierta y solo sale del ciclo por un `ADD`/`SET` explícito que declare la cantidad o por anulación con motivo;
nunca se inventa una cantidad ni una confirmación de recepción (V-06).

**Administradores y Telegram.** Se mantiene D-123/D-124: Pao, Danny y Julio tienen rol `admin` en la web y pueden
informar faltantes por Telegram si su cuenta está autorizada; los permisos de cada canal siguen siendo independientes.

**Dos saldos distintos.** `pending_quantity` es lo que falta **cotizar** (ninguna fuente lo cubre);
`pending_purchase_quantity` es lo que falta **comprar** (las compras confirmadas no lo cubren). Un faltante de 10
unidades completamente cubierto por un carrito activo queda con `pending_quantity = 0`,
`pending_purchase_quantity = 10` y estado `in_quote`: **cubierto provisionalmente**, no una solicitud sin gestionar y
tampoco cerrado. `pending_only=true` filtra por `pending_quantity > 0` e `is_pending` indica que el ciclo sigue
abierto. Se reutilizan los campos derivados existentes: **sin campo, tabla ni funcionalidad nuevos**.

**Consecuencias.** Se agrega el campo **derivado** `pending_purchase_quantity` (cantidad pedida menos cantidad
comprada) a `GET /api/v1/demand-items`; `is_pending` pasa a significar «el ciclo sigue abierto». Todo se
calcula en vivo: **sin tabla, columna, endpoint ni migración nuevos**; v0.2 sigue con **9 tablas** y C5-B/0017 con
**3**. Precisan este criterio el contrato §12.3–§12.7, V-34, V-39 y V-40, y el roadmap (C5-B). D-121 y D-126 quedan
intactas en lo demás; D-122 sigue fuera del MVP.

#### Enmienda a D-127 — compra **todo o nada** para la demo (2026-11-02)

**Decisión del usuario (prevalece).** Para la demo del 2026-11-13 **no se implementan compras parciales**. La compra
es **todo o nada**:

1. Cuando un administrador **confirma** una compra, se registra **completa**, con **todos los productos y cantidades
   seleccionados**, en una sola transacción.
2. Al **registrar la compra**, se **liberan los faltantes correspondientes**: se cierran (`fulfilled`) y el producto
   puede volver a reportarse por Telegram como un ciclo nuevo.
3. Si la compra **no se confirma**, **no se registra ni se libera nada**.
4. **No se permite** registrar parcialmente una compra **por productos ni por cantidades**: no hay endpoint,
   parámetro, pantalla ni flujo para dividir una compra.

**Reglas resultantes.**

- La cantidad de cada línea comprada es la **cantidad pendiente completa** del faltante al momento de confirmar; no
  se admite una cantidad menor. La compra se guarda íntegra e inmutable, como ya exige D-114.
- El cierre sigue siendo el de D-127: `fulfilled` solo cuando las compras confirmadas cubren **toda** la cantidad
  pedida conocida, en la misma transacción. Como la compra cubre completa, la transición ocurre al registrarla.
- Con cantidad **desconocida** no se cierra automáticamente (V-06): sigue vigente el `ADD`/`SET` explícito.
- `pending_purchase_quantity` se conserva como derivado, pero en la demo no aparece un **saldo nacido de una compra
  parcial**: es 0 o el pendiente completo. Los estados `partially_purchased` siguen nombrados en el modelo y **no se
  producen** por el flujo de compra de la demo; no se renombran ni se eliminan.
- **Correcciones y anulaciones** de una compra ya registrada mantienen sus reglas (§6.2, §12.4): recalculan cobertura
  y no reabren un faltante `fulfilled`. No son una compra parcial.

**Alcance.** Solo documental: no hay código, endpoint, columna ni migración nuevos; C5-B sigue sin implementar. Las
compras parciales quedan **fuera de la demo** y se evaluarán en una versión posterior **solo si Pao prueba y aprueba el
sistema**.

**Ajustes que esta enmienda obliga.** Contrato §4.2 (nota de estados), §6.5, §6.7, §10.5, §12.3, §12.4, V-40 y §15;
D-131; `CLAUDE.md` y `docs/ROADMAP.md`. Todo lo demás de D-127 (cotizar no cierra, cierre solo por compra confirmada,
`fulfilled` ≠ recibido, avisos repetidos, dos saldos) sigue intacto.

### Enmienda D0 a D-114 (2026-10-30)

Esta enmienda **no reemplaza** D-114: conserva X1/X2, las seis tablas originales, la inmutabilidad de compras, el
archivo por compra y la purga diseñada. Corrige y amplía lo siguiente:

| Punto | Corrección |
|---|---|
| **F-1** | `purchase` no puede ser «UPDATE siempre bloqueado» y recibir metadatos de archivo después. Se agrega la función restringida `purchase_mark_archive(...)`: solo escribe columnas `archive_*` y `detail_purged_at` (con `app.purchase_archive_update=on`), exige que las columnas de negocio queden byte a byte idénticas, y transiciona `none→pending→verified\|failed`. **`verified` es terminal**; `failed` puede reintentarse con una ejecución auditada que vuelve a `pending`. `DELETE` sobre `purchase` no existe |
| **F-2** | `pg_trgm`/`unaccent` son DDL: **no** pueden quedar «sin migración». C5-C pasa a tener migración propia (**0018**), con verificación previa de disponibilidad y autorización |
| **F-3** | Un carrito `dismissed` conservaba líneas activas y seguía bloqueando el total. Regla: **no se evalúa, no cuenta en el total y no cubre faltantes**; sus líneas se conservan y se reincorporan |
| **F-4** | `INVALID_QUANTITY`, `QUOTE_NOT_FOUND` y `QUOTE_VERSION_CONFLICT` duplicaban o contradecían C0. Se reutilizan `INVALID_QUANTITY`, `NOT_FOUND`, `VERSION_REQUIRED` y `VERSION_CONFLICT` (con `details.current_version`). Se retiran los códigos duplicados |
| **F-5** | Faltaban las garantías de persistencia: sin TTL, sin borrado por inactividad; sobrevive recarga, cierre del navegador, cierre de sesión, reinicio del backend y días; varias cotizaciones independientes; repetir `POST /lines` no duplica ni suma |
| **F-6** | El roadmap decía C3 = 0012 y C4 = 0013; el código ya está en 0011 y G1 entra primero. Nueva numeración: **G1 0012, C4 0013, C3 0014, C5-A 0015, O 0016, C5-B 0017, C5-C 0018, C5-D 0019** |
| **Tablas y columnas** | v0.2 completa **9 tablas** (6 de D-114 + `commercial_observation`, `observation_interpretation`, `web_access_request`) y **7 columnas** respecto del esquema actual: `quote_line.demand_item_id` (ya prevista en rev2), `quote_line.coverage_relation` (nueva en rev3) y 5 en `app_user`. **6 son nuevas en la revisión 3; las revisiones 4 y 5 no agregan columnas.** La revisión 5 retira `receipt` y `receipt_line` (D-126) |
| **P-8** | Resuelto por D-121: las compras `current` no anuladas cubren, sin doble conteo; cerrar la cotización no quita cobertura |
| **Recepción** | ~~Entra en alcance con D-122~~ **Retirada del MVP por D-126**: fuera del alcance implementable; se conserva como funcionalidad futura |
| **`note_text`** | Se elimina como campo único. Las observaciones viven en `commercial_observation` (D-116). `quote_line` conserva `demand_item_id` (ya previsto) y gana `coverage_relation` |
| **Compra en cotización cerrada** | Registrar una compra exige reabrir la cotización: `409 QUOTE_CLOSED`. Reabrir es explícito y auditado |
| **Nomenclatura** | La solicitud de acceso web es `web_access_request`, distinta de `telegram_access_request` |

**D-114 conserva** su texto y su fecha; esta enmienda prevalece solo en los puntos enumerados. El contrato
`docs/contracts/cotizador-v0.2.md` pasa a **revisión 3**; la **revisión 4** agrega D-121 a D-125; la **revisión 5**
retira la recepción del MVP (D-126) y sustituye a la revisión 4 sin reescribir D-114.

### Enmienda de la revisión 4 a D-114 (2026-10-30) — **parcialmente sustituida por D-126**

> Las filas **Recepción**, **Cierre del ciclo** y **Tablas** quedan sustituidas por la enmienda de la revisión 5. Lo
> demás (P-8, Telegram, búsqueda) sigue vigente.

| Punto | Corrección |
|---|---|
| **Recepción** | D-114 decía «recepción de mercadería» fuera de alcance. D-122 la incorpora: `receipt` + `receipt_line`, solo web, rol `admin` + `StrongActor`; documenta un hecho, no ejecuta nada. `purchase_execution` sigue `blocked` |
| **P-8** | La cobertura ya no es solo de líneas `draft`: suma compras `current` no anuladas, sin doble conteo (D-121). La fórmula de §12.3 del contrato se reemplaza |
| **Cierre del ciclo** | Un faltante pasa a `fulfilled` solo con cobertura suficiente y toda compra vigente recibida; no se cierra por recepción parcial ni por recepción completa de una compra que no cubre el objetivo (D-122) |
| **Telegram** | El estado se consulta y se informa sin duplicar necesidades (D-123); `admin` puede reportar si su cuenta está autorizada (D-124) |
| **Tablas** | 11 en total; 2 nuevas (`receipt`, `receipt_line`) en 0017. Las columnas siguen siendo 7 |
| **Búsqueda** | Tres tablas en 0013, con `search_result` como ampliación aditiva; C4 = 0013 y C3 = 0014 ratificados (D-125) |

### Enmienda de la revisión 5 a D-114 (2026-10-30, D-126 y D-127)

> **Actualizado el 2026-11-02:** la fila «Compras parciales» y la cláusula de compra parcial del «Cierre del ciclo»
> quedan sustituidas por la **enmienda todo o nada a D-127** (2026-11-02). Lo demás sigue vigente.

| Punto | Corrección |
|---|---|
| **Recepción** | D-122 la había incorporado; **D-126 la retira del MVP**. Sin `receipt`, `receipt_line`, endpoints, permisos, validaciones ni pantallas; sin cierre condicionado a recepción. Se conserva como funcionalidad futura (§15.1 del contrato) |
| **Cierre del ciclo** | ~~La cobertura alcanza el objetivo~~ **Precisado por D-127:** un faltante `active` con cantidad pedida **conocida** pasa a `fulfilled` solo cuando las **compras confirmadas** alcanzan el objetivo. Cubrir con cotización es provisional y no cierra; con cantidad desconocida no se cierra automáticamente. **Enmienda 2026-11-02:** la compra se registra todo o nada y la cubre completa, así que no hay saldo por compra parcial en la demo |
| **Compras parciales** | ~~**D-127:** 10 pedidas, 6 compradas → 6 cubiertas y 4 abiertas en el **mismo** faltante, sin solicitud duplicada~~ **Fuera de la demo (enmienda a D-127, 2026-11-02):** no se registran compras parciales por productos ni cantidades; la compra confirmada cubre completa y libera el faltante. Campo derivado `pending_purchase_quantity`, sin tabla ni endpoint nuevos |
| **Compra confirmada** | Marca el término del proceso del MVP; **no certifica** la llegada física. El sistema ni la afirma ni la niega (V-33) |
| **Telegram** | Sigue vigente D-123/D-124: un aviso repetido no duplica la necesidad y el bot informa «ya hay una compra registrada» sin afirmar recepción; `admin` informa si su cuenta está autorizada. **D-127:** la distinción aviso repetido / ciclo nuevo depende del ciclo, no de un plazo |
| **Tablas** | **9** en total: se retiran `receipt` y `receipt_line`; C5-B/0017 vuelve a 3 tablas (`purchase`, `purchase_line`, `purchase_index`). Las columnas siguen siendo 7 |
| **P-9/P-10/P-11** | Dejan de ser bloqueos de D0 y de la demo; quedan como preguntas futuras solo si la recepción vuelve al alcance |

### Contradicciones y pendientes detectados en D0

**Resueltas en esta fase:**

1. **D-113 reservada y sin registrar** → registrada antes de D-114, sin duplicar el número.
2. **Numeración C4/C3** → **ratificada**: C4 = 0013 y C3 = 0014 por orden de ejecución. No se invierte ni se
   renumera.
3. **`observation_interpretation` inmutable con `status` mutable** → contenido inmutable; solo campos de revisión
   cambian por función restringida y transición sin retorno.
4. **`purchase` inmutable con columnas de archivo** → función restringida; negocio byte a byte idéntico; sin `DELETE`
   del encabezado.
5. **Carrito `dismissed` bloqueando el total** → excluido de evaluación, total y cobertura.
6. **`pg_trgm` «sin migración»** → migración propia 0018 y verificación previa.
7. **Códigos duplicados** → se reutilizan los de C0; se retiran los duplicados.
8. **Persistencia no escrita** → garantías explícitas en el contrato §5.7.
9. **Conteo de tablas y columnas** → **9 tablas** totales (3 nuevas en la revisión 3; la revisión 4 proponía 2 más y
   la revisión 5 las retira con D-126); 7 columnas respecto del esquema actual, 6 nuevas en la revisión 3.
10. **`access_request` ambiguo** → `web_access_request`.
11. **Roadmap desactualizado** (C2 como «solo diseño», C3/C4 con números viejos) → actualizado en `docs/ROADMAP.md`.
12. **P-8 — cobertura y compras** → resuelto por D-121: las compras `current` no anuladas cubren sin doble conteo.
13. **Recepción dentro o fuera de alcance** → resuelto por **D-126**: queda **fuera del MVP**; se conserva el diseño
    de D-122 como funcionalidad futura, sin tablas ni endpoints.
14. **Tablas de búsqueda (2 vs. 3)** → resuelto por D-125: tres tablas, `search_result` aditiva; C0 no se edita.
15. **`admin` sin `REPORT_SHORTAGE`** → resuelto por D-124: se agrega el permiso, sin rol nuevo ni exclusión.
16. **P-9/P-10/P-11 como bloqueos de D0** → resuelto por **D-126**: dejan de bloquear D0 y la demo; quedan como
    preguntas futuras de recepción.
17. **Cierre provocado por cotizar** → resuelto por **D-127**: lo cotizado cubre provisionalmente y nunca cierra; solo
    la compra confirmada que cubre toda la cantidad pedida cierra el ciclo. ~~Las compras parciales mantienen el ciclo
    abierto en el mismo faltante~~ **Fuera de la demo (enmienda a D-127, 2026-11-02):** la compra es todo o nada y
    libera el faltante al registrarse.

**Abiertas, no bloquean D0:**

1. **Alcance de la superadministradora.** Confirmar que el cruce entre organizaciones es **solo** para metadatos
   administrativos; ampliarlo a datos operativos exige decisión nueva.
2. **Telegram en teléfono compartido.** El control individual es por cuenta; en `shared_device` el bloqueo afecta a
   todas las personas del teléfono. Confirmar que el control individual se limita a cuentas `personal`.
3. **Cadenas reales.** Ninguna habilitada; falta A1/A2 para Ahumada y un conector autorizado para Cruz Verde y
   Salcobrand. Sin eso, la demo muestra `not_configured` con motivo, no precios.
4. **OpenAI.** Clave, modelo, presupuesto, tarifa y autorización de costo. Sin ellos, O2–O4 deshabilitados; O1
   operativo.
5. **Archivo externo.** Proveedor, costos, credenciales y retención; C5-D bloqueado.
6. **`pg_trgm`/`unaccent`.** Verificar disponibilidad en el PostgreSQL de destino y autorizar su instalación.
7. **`farmacia`.** Sigue en **0008**; el código llega a **0012** (G1). Ninguna migración nueva se aplica sin
   autorización.
8. **Documentos desactualizados.** `CLAUDE.md` y `docs/AUTH_SUPABASE.md` todavía describen C2 como no implementado y
    no incluyen G1/D-115 a D-127; se actualizarán en una fase documental posterior, fuera de D0. **Resuelto el
    2026-10-30 (D-128):** ambos documentos y `docs/ROADMAP.md` quedaron alineados con C2, G1, D-113 a D-128 y el
    contrato revisión 6.
9. **Registro de D-107.** Su fila dice «Propuesta», pero el código 0009–0011 ya implementa el perfil de importación
    y las solicitudes de importación. Se anota como registro desactualizado, sin reescribir la decisión.

**Futuras, solo si la recepción vuelve al alcance (no bloquean el MVP):**

- **P-9** — anulación o corrección de una compra con recepciones.
- **P-10** — compra que nunca llega o recepción incompleta definitiva.
- **P-11** — corrección de una recepción errónea.

El diseño completo está en D-122 (retirada del MVP por D-126) y en el contrato §15.1.

---

## G1 — aprobación web, rol operativo y alcance documental (2026-10-30)

### D-128 — La aprobación web otorga el rol operativo `purchasing` (2026-10-30) — **superada por D-130**

> **Superada por D-130 (2026-11-02).** Se conserva el texto íntegro como historial. El usuario reemplazó el modelo de
> dos perfiles web (`admin` y `purchasing`) por uno solo: **aprobar otorga `admin`**. Todo lo demás de D-128 que no
> dependa de `purchasing` como perfil web —aprobar no enlaza ni sobrescribe, Telegram es un plano aparte, seguridad =
> JWT validado + persona activa + organización del `Actor` + `StrongActor` (no MFA), `web_access_expires_at = NULL` sin
> plazo por defecto y revisión de interpretaciones solo de `admin`— sigue vigente y D-130 lo confirma.

**Decisión del usuario (prevalece sobre el texto anterior).** Aprobar una solicitud de acceso web crea el `app_user`
con `roles = ['purchasing']`, `active = true` y `auth_subject`, **sin segunda autorización** y sin intervención de la
superadministradora. El Cotizador (E10–E15) admite **`admin` o `purchasing`** + `StrongActor`, con la excepción de la
revisión de interpretaciones de observaciones, que sigue siendo de `admin` (P-7, resuelto el 2026-10-30 en ese
sentido). Aprobar no otorga administración de usuarios, identidades ni configuraciones administrativas.

**Enmienda, no reescribe.** Sustituye la cláusula «aprobar no asigna roles / `roles = []`» de **D-117**, la
exclusividad de rol de **D-079** («único rol autorizado: `admin`») y la exigencia `admin` + `StrongActor` de **D-114**
para E10/E11/E12. Esas entradas conservan su texto y su fecha, y reciben la nota «vigente según D-128». Ninguna otra
cláusula de D-079, D-114 ni D-117 cambia: X1/X2, inmutabilidad de compras, historial, archivo, purga diseñada y
`purchase_execution = blocked` siguen intactos.

**Telegram.** Aprobar **no** crea cuenta de Telegram, ni vínculo, ni solicitud, ni autoriza chat alguno: la autorización
sigue siendo por cuenta y por chat (D-118), y la web nunca la concede. En un teléfono personal la identidad la fija la
cuenta autorizada. En un teléfono compartido el selector de reportantes es **identificación declarada** dentro de un
chat ya autorizado, **no** una lista de autorizados; por eso puede incluir a cualquier persona activa, disponible y con
`REPORT_SHORTAGE`, incluida una aprobada por la web. **No se agrega una bandera de reportante por persona**
(`app_user.telegram_reporter` queda descartada) y **no se modifica el bot**: excluir a alguien de ese selector exige una
señal por persona y es una decisión de **T1** (D-118), no de G1.

**Alcance de `purchasing`.** Conserva tal cual sus permisos actuales de la matriz: `VIEW_LIST`, `REPORT_SHORTAGE`,
`APPLY_QUANTITY`, `RESOLVE_OBSERVATION` y `CONFIRM_ATTRIBUTES`. Ninguno administra usuarios ni la matriz `MANAGE_*`, y
no obtiene `is_superadmin`. G1 **no amplía ni rediseña** la gestión de alias: `CONFIRM_ATTRIBUTES` se documenta con su
alcance de hoy (confirmar unidad, forma, contenido y presentación, y operar los alias conforme a D-015 a D-024) y
cualquier cambio posterior es una decisión propia. La revisión de interpretaciones de observaciones
(`POST /api/v1/observation-interpretations/{id}/review`) **sigue siendo de `admin`**; **P-7 queda resuelto** el
2026-10-30 en ese mismo sentido: no se amplía a `purchasing`.

**Seguridad y vigencia del acceso (confirmado por el usuario el 2026-10-30).** La autenticación de G1 y del Cotizador
es el **JWT de Supabase validado por el servidor**, sin MFA ni segunda autenticación: `StrongActor` conserva
**exactamente** su semántica vigente (D-086/D-099) —rechaza la cabecera de demostración fuera de `simulation` y es
**no-op** con `AUTH_MODE=supabase_jwt`— y **no debe presentarse como MFA**. Al implementar E10–E15 se verifica de forma
expresa: (a) JWT válido y persona `app_user.active`; (b) rol `admin` **o** `purchasing`; (c) la organización del
`Actor` autenticado, nunca el cuerpo; (d) `StrongActor` conforme a su semántica vigente. **La autenticación fuerte real
(MFA o re-autenticación) queda para H5**, como decisión futura: no se implementa ni se modifica ningún control de
seguridad en G1.

**La aprobación no fija vencimiento.** `web_access_expires_at` queda `NULL`, es decir, **acceso sin fecha de
expiración**; sigue vigente hasta una revocación, una desactivación (`active = false`) o un vencimiento fijado
explícitamente. **No hay plazo automático** —ni 90 días ni otro— y se mantienen los mecanismos de vencimiento y
revocación del contrato (§9.8), evaluados en cada petición.

**Enlace e identidad.** Para personas que ya existen, el enlace (`link-auth-subject`) y la asignación de roles
(`POST /api/v1/admin/users/{user_id}/web-access`) siguen siendo **operaciones separadas, explícitas y auditadas**. No se
agregan comandos nuevos (`--roles` en el enlace y un `grant-web-access` quedan descartados): el 409 por nombre visible
duplicado obliga a enlazar la fila existente por la vía que ya existe.

**Criterios de implementación de G1 acordados con esta decisión** (no implementados):

| Punto | Criterio |
|---|---|
| **Aprobar solo crea** (C-2) | Una solicitud aprobada crea el `app_user`; **nunca** enlaza ni sobrescribe una fila existente |
| **Unicidad de superadministradora** (P-3) | Validación en el servicio bajo bloqueo y `SUPERADMIN_PROTECTED`; si el test de sincronía lo tolera, índice único parcial en `0012` |
| **Endpoint público** (P-4, P-6) | `202` con cuerpo constante `{ "received": true }`; limitador de tasa en proceso (5/min por `sub`, 20/min por IP) sin dependencia nueva |
| **`set-superadmin`** (P-5) | Se implementa en G1: uso único, por CLI, auditado con `actor_id = NULL` |
| **Autorización de los endpoints de solicitudes** (P-1) | Permiso `MANAGE_WEB_ACCESS` concedido **solo** a `admin`; `purchasing` no administra accesos |
| **Nombres** (C-1) | Las administradoras operativas son Pao, Danny y Julio (§9.2 del contrato); D-117 nombraba solo a Pao y no se reescribe |

**Consecuencias documentales.** El contrato `docs/contracts/cotizador-v0.2.md` pasa a **revisión 6**: encabezado
«aprobado conceptualmente» (C-0), Cotizador `admin` o `purchasing`, aprobación con `purchasing`, §9.10 sobre Telegram,
V-27 y V-38 ajustadas y «7 columnas / 9 tablas» sin cambios. `CLAUDE.md`, `docs/AUTH_SUPABASE.md` y `docs/ROADMAP.md`
se alinean en la misma fase. **No se modificó código, modelo, pruebas, migración ni base de datos**; la migración
**0012** sigue sin crear y `farmacia` sigue en **0008**.

### D-129 — Observaciones comerciales separadas de los avisos de faltante (2026-11-02)

**Decisión.** Las observaciones **comerciales** del Cotizador (E13: notas sobre una cotización, una línea o una compra
registrada) se exponen en **`/api/v1/commercial-observations`**. Las rutas de faltantes **no cambian**:
`POST /api/v1/observations` y `GET /api/v1/observations` siguen siendo, exclusivamente, el registro y la bandeja de
avisos de faltante de H1-A/H1-B.

**Motivo.** Ambos conceptos comparten nombre pero no dominio, ni autor, ni ciclo de vida: un aviso de faltante nace de
un mensaje de Telegram o de la web y alimenta la lista compartida; una observación comercial es texto libre ligado a un
documento del Cotizador y no toca cantidades ni faltantes. Mantenerlas en la misma ruta haría ambiguo el permiso, el
`Idempotency-Key` y la auditoría, y obligaría a cambiar el contrato ya implementado de faltantes.

**Reglas.**

1. La ruta de faltantes queda **intacta**: mismo contrato, mismos permisos, misma deduplicación y misma auditoría.
2. `commercial-observations` se implementa en **C5-A**, junto con E13, con su propio permiso y su propio contrato.
3. No se crea un alias, redirección ni parámetro de tipo en `/observations`: una ruta, un dominio.
4. El contrato `docs/contracts/cotizador-v0.2.md` (E13) se actualizará **antes** de implementar C5-A para nombrar la
   ruta nueva; hasta entonces E13 sigue siendo solo documental.
5. Nada de esta decisión se implementa en Fase 0: no hay endpoint, modelo, migración ni cambio de código.

**Estado.** Aprobada por el usuario el 2026-11-02. **No implementada.** No modifica D-113 a D-128 ni el alcance del MVP.

**Actualización 2026-11-02 (misma fecha).** La regla 4 quedó cumplida: el contrato `cotizador-v0.2.md` **revisión 7**
nombra la ruta en §8.3 (`POST/GET /api/v1/commercial-observations`) y aclara que `/api/v1/observations` sigue siendo
solo el registro y la bandeja de faltantes. Sigue **sin implementar**: no hay endpoint, modelo ni migración.

## G1 implementado: un solo perfil funcional web (2026-11-02)

### D-130 — El acceso web aprobado otorga `admin`; `admin` es el único perfil funcional (2026-11-02) — **reemplaza D-128**

**Decisión del usuario (prevalece sobre D-128, que queda superada).**

1. **Único perfil funcional web: `admin`.** No hay un segundo perfil web. `purchasing` y `auxiliary` siguen existiendo
   como roles de Telegram (lo que decía D-124 sobre `admin` y `REPORT_SHORTAGE` no cambia); ninguno de los dos es un
   perfil de acceso web.
2. **Google/Supabase identifica, no concede.** Un JWT válido no habilita nada por sí solo: hace falta una persona
   `app_user` activa, con `auth_subject` enlazado y acceso vigente.
3. **El usuario solicita acceso.** Sin aprobación solo puede solicitar; la solicitud nace `pending` y **no otorga
   nada**.
4. **Todos los `admin` ven las pendientes en la campanita** y **cualquier `admin`** aprueba o rechaza.
5. **Aprobar otorga acceso completo como `admin`**: crea el `app_user` con `roles = ['admin']`, `active`, `available`
   y `auth_subject` **en un solo paso**, con `web_access_expires_at = NULL` (sin vencimiento automático).
6. **La autorización de Telegram es independiente** del acceso web: aprobar no crea cuenta, vínculo ni solicitud del
   bot, y no modifica el bot.
7. **`superadmin` es control técnico de la propietaria**, no un perfil funcional diferenciado: ve las solicitudes de
   todas las organizaciones y puede otorgar/revocar acceso y fijar vencimientos, pero **no aprueba en nombre de una
   farmacia**.

**Seguridad (vigente, sin cambios por D-130).** La autenticación es **JWT de Supabase validado + persona activa + rol
`admin` + organización del `Actor` + `StrongActor` con su semántica vigente** (D-086/D-099). **No es MFA ni una segunda
autenticación**; MFA real queda para H5 y no se implementa en G1. El acceso aprobado **no vence automáticamente**: se
mantiene hasta revocación, desactivación o un vencimiento fijado explícitamente. La revisión de interpretaciones de
observaciones sigue siendo de `admin` (P-7 resuelto).

**Aprobar no enlaza ni sobrescribe.** Si la identidad ya está vinculada → `409 USER_ALREADY_EXISTS`; si hay una persona
activa con el mismo nombre visible → `409 DUPLICATE_NAME` y la solicitud **sigue pendiente** para que una `admin`
decida. Enlazar una fila existente sigue siendo `link-auth-subject` (CLI, auditado).

**Consecuencias.** Se retiran del contrato las menciones a `purchasing` como perfil web: E10–E15 y la administración son
`admin` + `StrongActor`. El contrato pasa a **revisión 7**. `CLAUDE.md`, `docs/AUTH_SUPABASE.md` y `docs/ROADMAP.md` se
alinean en la misma fase.

**Implementado en G1 (código, 2026-11-02; migración `0012` creada y probada).**

| Pieza | Estado |
|---|---|
| `0012_acceso_web_solicitudes_y_superadministracion` | Creada: 5 columnas en `app_user` (`email`, `is_superadmin`, `web_access_expires_at`, `web_access_granted_by/at`), FK compuesta, índice único parcial de una sola superadministradora activa y tabla `web_access_request` con sus checks e índices |
| Servicio `services/web_access.py` | `request_access`, `list_pending`, `approve` (`roles=['admin']`), `reject`, `grant_web_access`, `revoke_web_access`, `set_superadmin`; todo auditado con `events.emit` en la misma transacción |
| API `/api/v1/web-access-requests` | `POST` público (202 constante, límite 5/min por `sub` y 20/min por IP → `429 RATE_LIMITED`, `503 ACCESS_NOT_CONFIGURED` sin organización), `GET` campanita, `POST /{id}/approve`, `POST /{id}/reject` |
| API `/api/v1/admin/users/{id}/web-access` | Otorgar/ajustar y revocar; solo `is_superadmin` |
| Permiso | `MANAGE_WEB_ACCESS` concedido **solo** a `admin` |
| CLI | `set-superadmin <app_user_id>`: uso único, auditado con `actor_id = NULL` |
| Autenticación | `verify_supabase_jwt_claims` (sub, correo verificado, nombre) y cabeceras demo `X-Demo-Subject`/`-Email`/`-Name`; `_actor_from_jwt` rechaza con `403 WEB_ACCESS_EXPIRED` el acceso vencido |
| Configuración | `WEB_ACCESS_ORGANIZATION_ID` (obligatoria para el endpoint público) |

**Alcance de la implementación.** La migración **no** se aplicó a `farmacia` (sigue en **0008**) ni a `farmacia_demo`
(sigue en **0011**); no hay despliegue, cambio de Lovable ni integración real de Telegram. Las pruebas de la suite
crean su propia base y la migran a `0012`.

### D-131 — Reporte repetido de un producto que ya está en la lista activa (2026-11-02)

**Decisión del usuario.** Cuando un auxiliar autorizado reporta por Telegram un producto que **ya está en la lista
activa**:

1. El bot identifica **quién reporta**, **qué producto** y **qué cantidad** (la cantidad desconocida sigue siendo
   `NULL`, nunca 0 ni 1).
2. Informa **quién lo reportó anteriormente** y pregunta si esa persona también quiere reportarlo.
3. Si **confirma**, el nuevo aviso se registra **dentro del faltante existente**, como reporte de la nueva persona;
   **no se duplica el producto** ni se crea un segundo faltante.
4. Mientras el faltante siga activo **no se inicia otro ciclo** para el mismo producto.
5. Cuando un administrador **registra la compra**, el producto se libera y puede volver a reportarse. Con la compra
   **todo o nada** de la demo (enmienda a D-127), la compra confirmada cubre toda la cantidad pedida de los faltantes
   incluidos: cada uno se cierra (`fulfilled`) y se libera en la misma transacción. Si la compra no se confirma, no se
   registra ni se libera nada.
6. Se conserva el **historial** de quién reportó, cuándo y con qué cantidad declarada.
7. **No se usa OpenAI** para interpretar mensajes de Telegram: la detección de producto y cantidad es determinista
   (alias, catálogo y reglas de D-015 a D-024) o queda como pregunta explícita, nunca como suposición.

**Relación con lo ya decidido.** No cambia D-123 (Estado por Telegram sin duplicar necesidades) ni la regla de que un
aviso repetido **no suma cantidad**: la confirmación agrega un **reporte**, no una cantidad. Tampoco cambia la regla de
cierre (D-127, con su enmienda del 2026-11-02): `fulfilled` sigue sin significar recibido y no hay compras parciales en
la demo.

**Estado.** Aprobada el 2026-11-02. **Solo documental**: se implementa en **T1**, junto con el bot real. No hay código,
modelo ni migración.

### D-132 — Observaciones comerciales abiertas para cualquier `admin`, sin pasos adicionales (2026-11-02)

**Decisión del usuario.** Cualquier `admin` puede usar el botón «Observación» y escribir **texto libre** («no volver a
sugerir este laboratorio», «nos fue mal en ventas», «prefiero Laboratorio Chile»). Las observaciones **persisten** y
**afectan las propuestas futuras de la IA**. No se exige **explicar cada recomendación**, ni imponer **formularios
técnicos**, ni **pasos adicionales de confirmación** que el usuario no pidió.

**Complementa D-116, no la reescribe.** La observación sigue siendo inmutable, acumulativa y separada de la
interpretación de IA; la IA sigue **solo sugiriendo** (`stance`, `text`, `based_on[]`, `supersedes[]`) y sigue sin
tocar precios, stock, cantidades, permisos ni el orden de las ofertas. Lo que cambia es el **alcance del autor** —no es
una sola persona, es cualquier `admin`— y la **prohibición explícita de fricción**: nada de categorías, selectores,
justificaciones obligatorias ni pantallas extra.

**Estado.** Aprobada el 2026-11-02. **Solo documental**: se implementa en O1–O4 (C5-A, migración `0015`). El contrato
en revisión 7 ya la recoge en §8.1 y §8.3.

### D-133 — Implementación de C3 (comparación E5 y criterios E9) y desviaciones declaradas (2026-11-02)

**Contexto.** El usuario ordenó implementar C3 con los resultados de búsqueda y las ofertas ya registradas, sin inventar
proveedores ni agregar funcionalidad fuera del contrato v0.2 rev7. Se implementó `POST /api/v1/comparisons` (E5) y el
recurso `/api/v1/commercial-criteria` (E9) con las fórmulas de C0 §2.4, §3.4 y §E5, y la migración `0014` (cinco
tablas: `comparison_run`, `comparison_line`, `comparison_candidate`, `comparison_supplier_total`,
`commercial_criterion`). Las cuatro tablas de comparación son de solo agregar (`forbid_mutation()`).

**Reglas que quedan fijadas por la implementación:**

1. **Elegibilidad por candidato, total por proveedor.** Causas duras → `ineligible`; causas blandas → `needs_review`;
   sin causas → `eligible`. Solo un proveedor con todas las líneas cubiertas por candidatos `eligible` y todos los
   costos determinados tiene `completeness=complete` y `total`. Un proveedor parcial **nunca** tiene `total`.
2. **Un desconocido es `NULL` y una causa tipada**, jamás 0 ni 1: mínimo/múltiplo, impuestos, inclusión de descuentos,
   base del precio y stock desconocidos se informan como tales y bloquean el total.
3. **La cota inferior no es un total.** Se calcula solo si ninguna línea falta, hay una sola moneda y ningún descuento
   es desconocido; impuestos y cargos desconocidos suman 0, los descuentos desconocidos **no** se asumen 0.
4. **La comparación se escribe una sola vez.** Nada se calcula "en dos pasos" actualizando filas: el estado, la
   completitud de cada línea y los totales se determinan antes de insertar. Las tablas lo imponen con un gatillo.
5. **Ningún criterio comercial altera un número.** `annotations[]` y `notes[]` se adjuntan y se prueban comparando
   totales antes y después de crear un criterio `do_not_recommend`.

**Desviaciones declaradas (pendientes de aprobación explícita):**

- **Monedas mixtas:** el contrato pide 422 `CURRENCY_MISMATCH` en §E5 y, a la vez, que el proveedor quede parcial. Se
  implementó lo segundo: la comparación responde 200, el proveedor queda `partial` con `currency_mismatch` en
  `blockers`, su `total` es `null` y `lowest_verified_total` es `null` si los totales completos no comparten moneda. Un
  fallo de una sola oferta no invalida la comparación completa.
- **`discarded_suppliers` con `quantity_unit_mismatch`:** el contrato enumera tres causas duras
  (`identity_mismatch`, `presentation_mismatch`, `supplier_inactive`); se agregó `quantity_unit_mismatch` porque un
  candidato en otra unidad no se puede comparar ni convertir sin un pack confirmado.
- **`source_search_id`** se valida como búsqueda de la organización pero **no** reutiliza ofertas: la comparación
  siempre relee la última observación por fuente y verifica frescura.
- **Sin `GET /comparisons/{id}`:** el contrato no lo define; no se implementó.

**Datos de la demo.** Con el catálogo ficticio actual (`multiple_known=false` en las 90 ofertas) ninguna línea puede
llegar a `complete`: el resultado honesto es `no_complete_total` con cotas inferiores. Para una demo con total completo
hacen falta mínimos y múltiplos explícitos en los datos simulados; es un cambio de **datos**, no de código, y no se
aplicó.

**Estado.** Implementada en código el 2026-11-02; migración `0014` **solo en `farmacia_test`** (la suite la recrea).
No se aplicó a `farmacia` ni a `farmacia_demo` sin autorización.
