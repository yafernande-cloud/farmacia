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
| D-072 | Tomada | **`create-admin` es bootstrap, no un mantenedor de administradoras.** La CLI `create-admin "<nombre>" --org sim|real --confirm` (servicio `staff.bootstrap_admin`) crea la **primera** administradora cuando ninguna puede iniciar sesión todavía, con estas restricciones: roles fijos `["admin"]` (no se aceptan otros), permitido **solo en `simulation` y `real_test`** (`BOOTSTRAP_NOT_ALLOWED` en `production` y en el archivo `legacy_mixed`), se niega si ya existe una persona **activa con `auth_subject` enlazado** y rol `admin` (`ADMIN_ACCESS_EXISTS`), admite **una sola** administradora de bootstrap **activa** por organización detectada por auditoría (`BOOTSTRAP_ALREADY_DONE`) —si se desactiva, se permite recuperar el acceso—, respeta la unicidad de nombre y **no enlaza `auth_subject`**, no crea usuarios de Supabase ni envía correo. Se audita como acción del **operador** (`actor_id = NULL`, acción `staff.bootstrap_created`, evento de outbox `staff.updated`), nunca como una persona de la farmacia |

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
| D-079 | Tomada (2026-10-29) | **El cotizador tiene un único rol autorizado: `admin`.** Los jefes de farmacia buscan, consultan ofertas y referencias, comparan, importan Excel y administran proveedores y fuentes con ese rol. **No se crea el rol `purchasing` ni permisos nuevos o diferenciados** para el cotizador, y **no se modifican los roles de otros módulos**. Se mantienen los mecanismos existentes: `Actor` resuelto en el servidor, organización desde `app_user`, `StrongActor` para organizaciones reales y `X-Demo-User-Id` solo en `simulation`. |
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
| D-093 | Tomada (2026-10-29) | **El contrato del cotizador v0 queda aprobado y congelado y la fase C0 se cierra.** El usuario autorizó formalmente el cierre de C0 el 2026-10-29. `docs/contracts/cotizador-v0.md` pasa a **APROBADO Y CONGELADO** y los criterios **C0-01 a C0-19** se dan por cumplidos por revisión del documento; **C0-13 queda cumplido** con esta aprobación. El congelamiento alcanza al contrato completo (E1–E9, tres planos, esquemas, errores, invariantes y AC), con las precisiones de D-086 a D-092 incorporadas y verificadas entre contrato, registro de decisiones y roadmap. **No se modificó código, modelo, endpoint, base de datos, migración ni OpenAPI; la migración 0008 no se ejecutó y C1 no se inició.** Consecuencias: (a) Lovable y el backend implementan el contrato tal cual; cualquier cambio de alcance, esquema o semántica exige **decisión nueva** (D-094 en adelante) y versión nueva del documento; (b) las decisiones comerciales y de cálculo siguen sin poder alterarse por criterios registrados (§1.1 y D-091); (c) D-076 y D-077 quedaban abiertas sin bloquear C1–C4 (D-076 se cierra el mismo 2026-10-29 en el apartado siguiente;
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
| D-099 | Pendiente (2026-10-29) | **`AUTH_MODE=demo_header` no debe permitir el acceso a datos reales en un entorno público.** La comprobación funcional de C1 confirmó el alcance real: con `demo_header` y una persona de `real_test`, la API **sí** responde **lecturas** de datos reales (`GET /api/v1/me` y `GET /api/v1/demand-items` devuelven 200 con datos de `real_test`); `StrongActor` solo bloquea las operaciones **administrativas** (`/staff`, cuentas y solicitudes de Telegram), que responden 403 `STRONG_AUTH_REQUIRED`. Es el comportamiento diseñado (D-069; D-086 ya documenta que `StrongActor` **no** es MFA ni re-autenticación), y la validación de arranque impide `demo_header` con `APP_ENV=staging|production` (`app/core/config.py`), de modo que el riesgo se materializa solo si se publica un entorno con `APP_ENV=local` o `demo`. **Antes de cualquier despliegue accesible públicamente** hay que decidir e implementar una de estas dos salidas: que `demo_header` resuelva **solo** personas de la organización `simulation` y rechace cualquier otra con 403, o que el modo demo no exista fuera de `local`/`demo`. **No se implementa ahora**: no se modifica `app/api/deps.py`, la matriz de permisos ni la configuración; requiere autorización y decisión propia. |

## Diseño de C2 y cierre de D-077 (2026-10-29)

| ID | Estado | Decisión |
|---|---|---|
| D-100 | Tomada (2026-10-29) | **D-077 queda cerrada: contrato de entrada del catálogo propio y de las planillas de proveedor.** El usuario aprobó el diseño con cuatro precisiones (fila sin SKU no se inventa ni se vincula por nombre; importación parcial cuando la estructura es válida; contrato C0 congelado; criterios de perfiles por fuente, historial sin sobrescritura, moneda y base declaradas, IVA desconocido, sin mappings automáticos, productos no farmacéuticos sin atributos obligatorios y sin funcionalidades nuevas en Lovable). Se aceptan los Excel **originales** de cada proveedor con estructuras distintas: **no hay plantilla única** y **no hay interpretación con IA**. Los dos contratos de entrada (abajo) son la especificación de D-077; **no se modificó el contrato C0** para cerrarla. |
| D-101 | Tomada (2026-10-29) | **Catálogo propio por archivo de datos (`catalog-load`), no por planilla de proveedor.** El archivo JSONL trae registros `article` y `mapping`. `article`: **`identity_key`** (clave canónica, la escribe una persona, nunca se deriva del nombre) y **`display_name`** obligatorios; `product_kind`, `ingredient`, `laboratory`, `brand`, `gtin`, `sanitary_registration` y `attributes` opcionales. `product_kind` se admite `NULL` («no clasificado») y **ningún atributo farmacológico es obligatorio**: el catálogo propio incluye productos no farmacéuticos (reafirma D-094). `mapping`: `source_id` + `external_id` + artículo destino (+ `external_name`), insertado en `supplier_product_mapping` con `origin='explicit'` y `created_by` del operador. Idempotente por `identity_key` y por `(source_id, external_id)`; diferencias de contenido no se sobrescriben en silencio, se informan como conflicto. `--dry-run`, auditoría y outbox. **No crea ofertas, no lee planillas y no infiere atributos ni vínculos.** |
| D-102 | Tomada (2026-10-29) | **Perfil de importación reutilizable por fuente.** Un perfil por **fuente** (no por proveedor: un proveedor puede mandar varios formatos y cada uno es una fuente). Contiene `file_format` (`.xlsx` en v0), `sheet`, `header_row`, `columns` —papel → columna resuelta **por nombre de cabecera con lista de alias**, posición solo como respaldo—, `units` (vocabulario del archivo → `box`/`package`/`unit`/`bottle`, enum cerrado) y `options` (separadores decimal/miles, formato de fecha, y moneda o base de precio **declaradas explícitamente**). Se edita con `version` + `If-Match` como el resto del contrato y se reutiliza en cada importación de esa fuente. |
| D-103 | Tomada (2026-10-29) | **Validación en dos fases y procesamiento parcial con conteos fieles.** Fase 1, estructural: archivo legible, cabecera presente, todas las columnas del perfil resueltas y límites respetados (10 MB, 20.000 filas); si falla **no se escribe ninguna fila**. Fase 2, por fila: se clasifican todas las filas y solo después se escriben las aceptadas, con auditoría y outbox en la misma transacción. `status` = `completed` (todas aceptadas), `completed_with_errors` (estructura válida con rechazos) o `failed` (nada aceptado). `rows_total` cuenta **filas de datos no vacías**; `rows_accepted + rows_rejected = rows_total` y ambas se derivan de lo realmente escrito. Códigos de fila: los **ya cerrados** en D-090. Ninguna fila rechazada se descarta en silencio. |
| D-104 | Tomada (2026-10-29) | **Una fila sin SKU no se inventa ni se vincula por nombre.** Sin código de proveedor → rechazo `MISSING_REQUIRED_FIELD`; con código pero sin mapping → `UNMAPPED_PRODUCT`; **nunca** se usa el nombre como identificador ni se busca un parecido. Para poder identificarlas después, la fila rechazada se conserva **íntegra** en `import_run.rejected_rows` (número de fila, valores originales tal como vienen, fuente y hash del archivo). La respuesta HTTP mantiene **exactamente** la forma congelada en C0 (`row`, `code`, `message`), con el texto identificador dentro de `message`; exponer el detalle estructurado en la API exigiría una versión nueva del contrato (D-108). |
| D-105 | Tomada (2026-10-29) | **Dinero declarado, nunca supuesto.** La moneda y la base del precio (`per_commercial_unit`/`per_base_unit`) vienen del archivo o de una **declaración explícita del perfil**; no hay valor por defecto silencioso, y sin ellas la fila se rechaza (`INVALID_CURRENCY`/`INVALID_BASIS`). `tax_included` y `discount_included` quedan `NULL` = desconocido cuando la planilla no lo dice, y ninguna importación los completa por convención (D-081). Importes con `Decimal`/`NUMERIC`, jamás float. |
| D-106 | Tomada (2026-10-29) | **E8 solo escribe ofertas de proveedor.** La importación crea únicamente `import_run` y `offer_snapshot`; **nunca** `trade_item`, `supplier_product_mapping`, `chain_product` ni `reference_price`, y no confirma identidad farmacéutica ni habilita compra. La fuente debe ser `kind='supplier'`: una `chain_reference` se rechaza antes de leer filas (la base también lo impide con `enforce_source_kind`). Los precios públicos de cadena y los costos de proveedor no se mezclan en ningún punto. |
| D-107 | Propuesta (2026-10-29) | **Esquema necesario para C2 — no aplicado.** (1) Tabla nueva **`import_profile`** (perfil por fuente, con `version`, `status`, `data_origin` y FK compuestas por organización): hoy no existe dónde guardar el mapeo y `source.capabilities` no sirve (es una lista de capacidades del conector, sin validación por columna). (2) En **`import_run`**: `profile_id` (FK compuesta, nullable), `profile_snapshot` (jsonb, el mapeo exacto usado, porque la importación es de solo agregar y debe poder reproducirse) y `rejected_rows` (jsonb, D-104). Serían **un solo cambio de esquema**, con migración y modelo sincronizados y el test de sincronía pasando. Si se aprueba, C2 toma el número **0009** y las migraciones previstas de C3 y C4 pasan a **0010** y **0011** (ninguna existe todavía). **Requiere autorización explícita antes de crearse.** |
| D-108 | Pendiente (2026-10-29) | **Puntos del diseño que tocarían el contrato C0 — identificados, no aplicados.** (a) **`dry_run` en E8**: se evita, la vista previa va por CLI `import-check`; si más adelante se quiere en la API, exige contrato v0.1. (b) **Códigos de error de archivo**: no se agregan; una cabecera que no calza con el perfil se informa como el ya existente `422 IMPORT_FORMAT_INVALID`, y el mismo archivo (misma fuente + mismo `file_hash`) devuelve **200 con el resultado original**, sin campo nuevo. (c) **Fuente que no sea `supplier`**: hoy solo puede informarse con un código genérico; un código preciso (`SOURCE_KIND_INVALID`) exige contrato v0.1. (d) **Detalle estructurado de filas rechazadas en la API** (D-104): también exige contrato v0.1. Ninguno bloquea la implementación. |

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
