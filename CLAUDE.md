# CLAUDE.md — Farmacia Parque Lantaño

Asistente de compras con IA (demo → producción). Idioma de trabajo y UI: español de Chile.

## Fuentes de verdad
- `docs/Farmacia_Parque_Lantano_HLD_LLD.md`: requisitos, invariantes (§2), AC01–AC40. **No editar**; registrar
  cambios en `docs/decisions/`. Si el prompt del usuario contradice el HLD, rige el usuario y se registra.
- `docs/ARQUITECTURA.md`, `docs/ROADMAP.md`, `docs/decisions/0001-registro-de-decisiones.md`: estado vigente.
- `docs/contracts/cotizador-v0.2.md`: contrato del cotizador **revisión 10-A6**, aprobada el 2026-11-03 (D0
  2026-10-30 y **parcialmente implementado**: C5-A, D-135 y D-137). La revisión 10-A6 incorpora **solo A6** (O3, O4,
  §8.4, §8.6 y AC31): preferencias por producto que ordenan ofertas, sin explicaciones de IA. **C9/D-139 y C11/D-140
  siguen pendientes de incorporar**; el texto de trabajo está en `docs/contracts/cotizador-v0.2-rev10-propuesta.md`.
  Define E10–E15, la cobertura de faltantes, la regla de cierre y la aprobación web que otorga `admin` (D-130). Nada de
  lo pendiente se implementa sin orden explícita.
- `docs/decisions/ACUERDOS_FUNCIONALES.md`: acuerdos A1–A11 y flujo funcional completo con su estado real
  (implementado en código / aprobado sin código / pendiente), contradicciones documentales y recomendación de
  migración. **Es una propuesta en revisión, no aprobada todavía como documento de referencia**; no acredita despliegue.
  C7, C9, C11 y AC31 ya están decididos (D-138 a D-141); A6 se aprobó e incorporó el 2026-11-03 (D-142); lo pendiente es
  su implementación, no su definición.

## Reglas permanentes
- **Git:** rama `master`. NO ejecutar commit, push, init, remote add ni renombrar ramas sin orden explícita.
  Nada de GitHub ni despliegues sin autorización.
- Consultar antes de: servicios externos, costos, credenciales, publicar. Nunca inventar secretos, proveedores,
  APIs, precios, stock ni enlaces. Mocks/fixtures solo en pruebas o etiquetados `fixture`.
- Invariantes críticos: un aviso repetido no suma cantidad; cantidad desconocida = NULL (no 0/1); sin fusión
  automática de dosis/formatos distintos; solo `SET/ADD/CLEAR` explícitos; aprobaciones ligadas a snapshot;
  referencia de cadena ≠ oferta comprable; compra real **bloqueada**; el LLM no decide dinero, cantidades ni permisos.
- **Cotizador (D-113 a D-142; contrato v0.2 rev10-A6, aprobada solo para A6):** el MVP demuestra *faltante → búsqueda → comparación →
  cotización → compra*; **no** es inventario ni recepción. La recepción física y las diferencias de entrega están
  **fuera del MVP** (D-126). El ciclo se cierra **solo** con compras confirmadas que cubren toda la cantidad pedida
  conocida; cotizar cubre de forma **provisional** y nunca cierra. **Cotización cerrada (C9, D-139, aprobado y no
  implementado):** cerrar significa que la cotización **terminó de prepararse y puede convertirse en compra**; no
  significa comprada, descartada ni anulada. `closed` conserva líneas, cantidades y asignaciones, **sigue aportando
  cobertura provisional** y **se compra sin reabrir**. El código actual todavía cuenta solo `draft` y bloquea comprar
  con `409 QUOTE_CLOSED`; esa contradicción se corrige cuando se autorice C5-B. **Compra todo o nada (enmienda a
  D-127, 2026-11-02):** se registra completa —todos los productos y cantidades seleccionados— o no se registra; al
  registrarse, los faltantes cubiertos se cierran y se liberan para nuevos reportes por Telegram. **La compra puede
  incluir cantidades sin asignación para inventario (C11, D-140, aprobado y no implementado):** las asignaciones son
  explícitas, su suma ≤ cantidad comprada, y el excedente no se asigna automáticamente ni cierra reportes. **No hay
  compras parciales por productos ni cantidades de la selección en la demo** y no se implementa lógica para ellas. Dos saldos distintos: `pending_quantity` = falta
  **cotizar**, `pending_purchase_quantity` = falta **comprar**. `fulfilled` **no** significa recibido. Telegram solo
  lee: no registra compras, no aplica cantidades ni confirma llegadas. Cotizar y comprar son `admin` + `StrongActor`
  (D-130). **Reportes independientes (D-135):** cada persona tiene su propio reporte; un aviso de otra persona nunca se
  adjunta ni se suma, el bot pregunta (Confirmar/Modificar/Cancelar) y, si se confirma, crea un reporte propio. La
  línea de cotización cubre uno o varios reportes solo mediante `quote_line_allocation` con cantidades **explícitas**
  (suma ≤ línea, sin reparto automático). Cancelar un reporte es lógico (`admin` + `StrongActor`, versión e
  idempotencia), no borra historial e informa las líneas afectadas. **Integridad de asignaciones (D-137):** una
  cotización **no se cierra** si tiene asignaciones a reportes cancelados o cantidades inválidas
  (`409 QUOTE_HAS_INCONSISTENT_ALLOCATIONS`), y la cantidad pedida **no baja** de lo asignado
  (`409 QUANTITY_BELOW_ALLOCATED`) ni se puede borrar con asignaciones vigentes (`409 REPORT_HAS_ALLOCATIONS`); nada se
  reasigna ni se corrige solo: la administración ajusta las asignaciones (PATCH con lista vacía quita todas) y vuelve a
  cerrar. Telegram: mismo autor con cantidad conocida elige SET/ADD/registrar/cancelar; el auxiliar sin
  `APPLY_QUANTITY` deja propuesta. **Preferencias comerciales (A6, D-136 y D-138; D-142):** «Enseñar a la IA» interpreta
  internamente las preferencias **de un producto específico** y **ordena sus ofertas**; todas las demás siguen visibles;
  las preferencias **no se propagan** a otros productos del mismo laboratorio, marca o proveedor y **no se construyen
  perfiles globales de reputación**. La IA **no** muestra explicaciones, justificaciones, recomendaciones textuales ni
  puntuaciones, no inventa precios/stock/productos y no modifica cantidades. Su trazabilidad se conserva **internamente**
  (AC31/D-141), no en la interfaz. Las **fases 1, 2 y 3A están implementadas**: esquema (modelos, migración `0018`,
  `observation_interpretation`, `commercial_preference`, `preference_order_trace`, columnas de laboratorio/marca por
  oferta), **motor determinista** (`app/services/preferences.py`) que ordena E3/E5 y registra la traza interna y
  **servicio de interpretación interna con OpenAI** (`app/services/observation_ai.py` + adaptador sin SDK
  `app/interpretation/openai_observations.py` + validación estricta `app/interpretation/commercial.py`, **D-145**);
  **corregido por D-143** (elegibilidad primero, estados no aplicados sin efecto, auditoría *best effort* en el detalle,
  traza con modelo/prompt/estado) y **D-144** (retiro explícito por clase con `retired_subject_kinds`, migración `0019`,
  solo para el artículo; una preferencia posterior válida vuelve a establecerla);
  **los endpoints REST de interpretación están implementados (A6 Fase 3B, 2026-11-03; ver
  `docs/A6_FASE_3B_API.md`) y falta conectar Lovable**, y la suite usa un doble: ninguna prueba llama a
  OpenAI ni consume cuota. Observaciones comerciales:
  `/api/v1/commercial-observations`, separadas de los avisos de faltante (`/api/v1/observations`, D-129).
- **Acceso web (D-130, implementado en código; `0012` no aplicada a `farmacia`):** **único perfil funcional web:
  `admin`**; `purchasing` y `auxiliary` siguen siendo roles de Telegram, no perfiles web. Google/Supabase **identifica
  pero no concede**: sin aprobación solo se puede *solicitar*, y la solicitud no otorga nada. Todas las `admin` ven las
  pendientes en la campanita y **cualquier `admin`** aprueba o rechaza. Aprobar crea el `app_user` con
  `roles = ['admin']`, `active` y `auth_subject` en un solo paso; **nunca** enlaza ni sobrescribe una fila existente
  (409 `USER_ALREADY_EXISTS` / `DUPLICATE_NAME`). `superadmin` (`is_superadmin`) es **control técnico** de la
  propietaria, no un perfil funcional: ve todas las organizaciones y otorga/revoca acceso, pero **no aprueba en nombre
  de una farmacia**. Aprobar **no** crea cuenta ni vínculo de Telegram: en teléfono personal manda la cuenta y el
  selector del compartido es identificación declarada, no una lista de autorizados; no se agrega bandera por persona
  ni se toca el bot (eso sería T1). El acceso aprobado queda **sin vencimiento automático**
  (`web_access_expires_at = NULL`) hasta revocación, desactivación o vencimiento fijado explícitamente. La seguridad es
  **JWT de Supabase validado + persona activa + rol `admin` + organización del `Actor` + `StrongActor` con su semántica
  vigente** (D-086/D-099), que **no es MFA ni una segunda autenticación**; MFA real queda para H5. La revisión de
  interpretaciones de IA sigue siendo de `admin` (P-7 resuelto).
- **Permisos (D-124, aprobado y no implementado):** hoy `admin` **no** tiene `REPORT_SHORTAGE` y
  `sessions.list_reporters` lo excluye del selector del bot. La ampliación se implementa en la fase que habilite
  T1/C5-A; no se cambia la matriz "de paso".
- **Productos (D-015 a D-024):** jamás asumir una unidad ni un rol (un número suelto NO es mg). Separar potencia,
  contenido, presentación y cantidad solicitada. Cada atributo lleva procedencia `explicit|inferred|confirmed`;
  solo una persona autorizada o un alias verificado confirma. Guardar siempre lo escrito (`original_text`) y normalizar
  solo para comparar. Registrar el aviso siempre; ante ambigüedad, candidatos+motivos para compras, nunca una pregunta
  obligada al auxiliar. Toda unidad/forma nueva = fila en `domain/units.py`/`domain/products.py` + prueba.
- **Canal/fotos (D-025 a D-036):** sin identidad vigente el mensaje queda pendiente y NUNCA se atribuye solo; el bot no aplica
  cantidades ni compra. Lo leído de una imagen es `inferred` con `origin=image`; la cantidad visible no es cantidad solicitada;
  laboratorio/fabricante/titular/comercializador son entidades distintas y desconocidas hasta que alguien las diga. La IA solo
  sugiere (`AIInterpreter`) y no hay proveedor de visión real: todo lo simulado debe decir `simulated`. No declarar que algo se
  probó con Telegram real o con visión real si solo se probó con simulador. El token de Telegram nunca en chat/logs/git.
- **Separación de datos (D-037 a D-047):** simulaciones y pruebas automatizadas SOLO en la organización `simulation` (chats `555000x`); el bot real
  en `real_test`. Nunca usar la organización real por defecto ni vaciar tablas en demos. `data_origin` (simulated/captured/ai_derived) NO es confianza.
  Una lectura simulada jamás entra en una organización real. El personal no se define en el código (archivo de datos → PostgreSQL), no se elimina, y solo
  personas activas + disponibles pueden ser identidad; PIN no se exige en el demo. **Estado real de `farmacia`: 0004, 0005, 0007 y 0008 aplicadas y script de datos 0006 ejecutado (`alembic current` = 0008, 32 tablas). La 0008 (catálogo comercial y ofertas, 10 tablas) se aplicó el 2026-10-29 con respaldo `pg_dump` verificado (restaurado y comparado fila a fila), ensayo previo de subida y de reversión, y verificación posterior por catálogo: 219 registros intactos y ningún objeto previo alterado (D-098). No aplicar migraciones nuevas a `farmacia` sin autorización explícita.**
- Dinero con `Decimal`/NUMERIC, nunca float. Fechas UTC; presentar en America/Santiago.
- No llamar al LLM para lo determinista. Rendimiento es requisito (dashboard <2 s, resultados progresivos).
- Frontend (Lovable) solo consume `/api/v1`; sin lógica de negocio ni acceso directo a la BD.

## Comandos (desde `backend/`)
- Arrancar: `scripts/dev.sh` · Pruebas: `.venv/bin/pytest` · Lint: `.venv/bin/ruff check .`
- Migraciones: `.venv/bin/alembic revision --autogenerate -m "..."` / `upgrade head` (requiere `DATABASE_URL`)

## Estado: implementado vs aprobado (2026-11-02)
- **Implementado en código:** H1-A, H1-B/H1-B.4 con simuladores, personal y `/staff`, auth web (validador JWT,
  enlace `auth_subject` por CLI, `create-admin`, `StrongActor`), **C2** (`catalog-load`, `manual_import`, perfiles de
  importación, `needs_review`, E8 con `Idempotency-Key`; `0009`–`0011`) y **G1** (solicitud pública de acceso,
  campanita, aprobación que otorga `admin`, rechazo con motivo, otorgar/revocar solo por `superadmin`,
  `set-superadmin` por CLI, vencimiento del acceso web y límite de tasa; migración `0012`) y **C4** (búsqueda E1/E2
  con `relation`, detalle E3, cadenas E15 sin persistencia; `search_run`, `search_source_result`, `search_result`;
  migración `0013`) y **C3** (comparación E5 con elegibilidad por candidato, totales solo completos y cota inferior,
  criterios y notas E9 que solo anotan; `comparison_run`, `comparison_line`, `comparison_candidate`,
  `comparison_supplier_total`, `commercial_criterion`; migración `0014`) y **C5-A** (cotizaciones E10 con carritos por
  proveedor, líneas sin copia de precio, SET/retiro lógico/close-pending/reopen-pending/move/refresh, evaluación E5 por
  carrito y total general solo completo; observaciones comerciales O1 inmutables y sin IA; cobertura provisional §12;
  `quote`, `quote_cart`, `quote_line`, `commercial_observation`; migración `0015`; D-134) y **D-135** (reportes
  independientes por autor, cancelación lógica `admin` + `StrongActor`, confirmación en Telegram, propuesta de cantidad
  sin `APPLY_QUANTITY`, asignaciones `quote_line_allocation`; migraciones `0016`–`0017`; contrato rev8) y **D-137**
  (integridad de asignaciones: cierre bloqueado con asignaciones inconsistentes, cantidad pedida protegida por debajo de
  lo asignado, `PATCH []` quita todas; contrato rev9; **sin tablas ni columnas nuevas**, migraciones siguen `0016`–`0017`)
  y **A6 Fase 1** (esquema de preferencias comerciales: `observation_interpretation`, `commercial_preference`,
  `preference_order_trace`, laboratorio/marca por oferta; migración `0018`; contrato **revisión 10-A6**; **D-142**) y
  **A6 Fase 2** (motor determinista `app/services/preferences.py`: precedencia por clase, tolerancias, precios
  aceptables/máximos, orden en E3/E5 y traza interna; **sin migración nueva y sin OpenAI**) y **D-143** (corrección de
  interpretación: elegibilidad primero, estados no aplicados sin efecto comercial, auditoría *best effort* en
  `GET /articles/{id}` y obligatoria en comparación, traza con modelo/prompt/estado; **sin migración**; 4 pruebas
  nuevas) y **D-144** (retiro explícito de preferencias por clase: `retired_subject_kinds` en interpretación y traza,
  migración **`0019`**, resolución temporal de nuevas/reemplazos/retiros y 12 pruebas nuevas).
- **Aplicado a `farmacia`:** solo hasta **0008** (32 tablas). Las `0009`–`0019` existen en el código y **no** están
  aplicadas allí. `farmacia_demo` tiene aplicadas **`0012`–`0015`** (2026-11-02, cada una con respaldo `pg_dump`
  verificado; 47 tablas) y es la base donde se prueban C3, C4 y C5-A con el catálogo ficticio; **`0016`–`0019` no están
  aplicadas** en ninguna base fuera de `farmacia_test` y no se aplican sin autorización explícita.
- **Solo documental (aprobado, sin código):** contrato v0.2 **revisión 10-A6** en lo que falta —compras registradas y su
  cierre, historial, búsqueda de cadenas E15, y la incorporación de C9 y C11— y las decisiones **D-113 a D-145**. E10 y
  O1 **sí** están implementados (C5-A); los reportes independientes y las asignaciones (D-135) también; la integridad de
  asignaciones (D-137) también. **A6 (preferencias que ordenan ofertas vía «Enseñar a la IA») está aprobada (D-142) con
  sus fases 1, 2 y 3A implementadas** y **precisada por D-143**; el **retiro explícito quedó implementado por D-144**:
  esquema (`0018`/`0019`), **motor determinista** que ordena E3/E5 y genera la traza interna y **servicio interno de
  interpretación con OpenAI (D-145)**; la elegibilidad manda,
  los estados no aplicados no retiran preferencias y el retiro explícito solo afecta su clase y su artículo; **los
  endpoints REST de interpretación y preferencias están implementados (A6 Fase 3B, `docs/A6_FASE_3B_API.md`);
  Lovable NO está conectado**, y las pruebas no llaman al proveedor real. Las
  preferencias son
  **del producto
  específico** (D-138), no se propagan por
  laboratorio, marca o proveedor y no construyen perfiles globales; aplican a cualquier producto comercializado (no solo
  medicamentos). **D-139 (C9, cotización cerrada preparada para comprar)** y **D-140 (C11, compra adicional para
  inventario)** están aprobadas y **no implementadas**; el código actual todavía contradice D-139 (cobertura solo de
  `draft`, compra bloqueada con `409 QUOTE_CLOSED`). **D-141 (AC31, auditoría interna de la IA)** tiene su esquema en
  `0018`, y **la traza ya se genera** con el motor de Fase 2 (con preferencias aplicables o retiros vigentes). **A3
  (mantenedor de proveedores con URL, contacto comercial, cuentas,
  credenciales y estado de conexión)** sigue aprobado y **sin código ni migración**: no hay conectores reales, cuentas
  operativas ni credenciales almacenadas. La recepción está fuera del MVP (D-126).
- **Sin empezar:** C5-B (`0020`), **la conexión de O2 con Lovable** (el backend de O2 está implementado,
  A6 Fase 3B), C5-C (`0021`), C5-D (`0022`, bloqueado), T1, C6/C6a y H2–H6.

## Metodología
Por fases con informe (objetivo, alcance, archivos, implementación, pruebas, aceptación, riesgos, continuar).
Hecho: F0, **H1-A**, corrección del modelo de productos, **H1-B** con simuladores y **H1-B.4**
(solicitud/aprobación/bloqueo de accesos de Telegram; ver `docs/H1B_TELEGRAM.md`); **C1** (migración `0008` aplicada a `farmacia`); **C2** implementado **en código** (`catalog-load`, conector `manual_import`, perfiles de importación, estado de revisión de oferta y endpoint E8 con idempotencia; migraciones `0009`–`0011`, **no aplicadas a `farmacia`**; D-109 a D-111); **D0 cerrado el 2026-10-30** (commit `38adb6b`: contrato v0.2, decisiones D-113 y D-115 a D-127, roadmap); la **fase documental del 2026-10-30** (contrato **revisión 6**, **D-128**, alineación de `CLAUDE.md`, `docs/AUTH_SUPABASE.md` y `docs/ROADMAP.md`); la **Fase 0 del 2026-11-02** (commit `fe3518e`: preparación para Heroku, proveedores, fuentes y catálogo ficticio; D-129); **G1 implementado en código el 2026-11-02** (contrato **revisión 7**, **D-130 a D-132**, migración `0012`, 16 pruebas nuevas; `0012` aplicada **solo a `farmacia_demo`**) **C4 implementado en código el 2026-11-02** (E1/E2/E3 + E15, migración `0013` aplicada **solo a `farmacia_demo`**, 14 pruebas nuevas; suite **486** con `REQUIRE_DB=1`) y **C3 implementado en código el 2026-11-02** (E5/E9, migración `0014` solo en `farmacia_test`, 15 pruebas nuevas; **D-133**) y **C5-A implementado en código el 2026-11-02** (E10/O1, migración `0015` aplicada **solo a `farmacia_demo`** junto con `0014`, 23 pruebas; **D-134**) y **D-135 implementada en código el 2026-11-02** (reportes independientes, cancelación lógica, confirmación en Telegram y asignaciones; migraciones `0016`–`0017` **no aplicadas**, contrato rev8, 7 pruebas nuevas; **D-135**) y **D-137 implementada en código el 2026-11-02** (integridad de asignaciones: cierre bloqueado, cantidad protegida, `PATCH []`; contrato **revisión 9**, 3 pruebas nuevas; **D-137**) y **A6 Fase 1 implementada en código el 2026-11-03** (esquema de preferencias comerciales, migración `0018`, 23 pruebas de esquema nuevas; contrato **revisión 10-A6**; **D-142**) y **A6 Fase 2 implementada en código el 2026-11-03** (motor determinista de preferencias: precedencia, tolerancias, precios aceptables/máximos, orden en E3/E5 y traza interna; 17 pruebas nuevas; **sin migración nueva y sin OpenAI**) y **D-143 implementada en código el 2026-11-03** (corrección de interpretación de A6: elegibilidad primero, estados no aplicados sin efecto ni retiro implícito, auditoría *best effort* en el detalle y obligatoria en comparación, traza con modelo/prompt/estado; 5 pruebas nuevas; **sin migración**) y **D-144 implementada en código el 2026-11-03** (retiro explícito por clase: `retired_subject_kinds`, migración **`0019`**, resolución temporal de nuevas/reemplazos/retiros, traza interna; 12 pruebas nuevas) y **D-145 implementada en código el 2026-11-03** (A6 Fase 3A: interpretación interna de observaciones con OpenAI —servicio, adaptador de la Responses API, validación estricta de la salida, estados, retiros y errores internos—; **sin migración, sin endpoints y con proveedor simulado en la suite**; 68 pruebas focalizadas nuevas). El 2026-11-02 se registraron además **D-138 (C7, preferencias por producto)**, **D-139 (C9, cotización cerrada preparada para comprar)**, **D-140 (C11, compra adicional para inventario)** y **D-141 (AC31, auditoría interna)**, y el 2026-11-03 **D-142 (A6 aprobada)**, **D-143 (corrección)**, **D-144 (retiro)** y **D-145 (Fase 3A)**; el **2026-11-03 se implementó en código la A6 Fase 3B** (endpoints REST de O2–O4:
guardar y listar en `/commercial-observations`, interpretar en `/commercial-observations/{id}/interpretations`,
detalle, preferencias vigentes e historial en `/articles/{id}/preferences`; idempotencia sin migración; 13 pruebas
nuevas; ver `docs/A6_FASE_3B_API.md`) y **los endpoints REST de O2 ya están implementados**; siguen **sin
implementar** D-139 y D-140. La suite completa no se ejecutó en esta fase; quedan **691 casos recopilados** (incluidos
los live, que no se ejecutan en la suite normal).
El siguiente paso es **C5-B** (`0020`, que debe incorporar D-139 y D-140) o **conectar O2 con Lovable**
(backend ya implementado en A6 Fase 3B), según autorización; después C5-C (`0021`) y C5-D (`0022`, ejecución bloqueada). Las migraciones `0016`–`0019` solo se aplican a `farmacia_demo` con autorización explícita (ver `docs/decisions/ACUERDOS_FUNCIONALES.md` §7), tras el ensayo en `farmacia_test`. Siguen abiertas **H1-B.5** (verificar con el bot real; el usuario crea el bot y guarda el token en `.env`), H1-C (SSE + dashboard Lovable) y las autorizaciones externas (cadenas, modelo de OpenAI —`OPENAI_MODEL` no tiene valor por defecto—, almacenamiento).
No declarar pruebas de BD como exitosas si se omitieron: validar con `REQUIRE_DB=1 .venv/bin/pytest` (PostgreSQL local por Docker; `open -a Docker`).
Supabase: proyecto `faltantes-farmacia` creado por el usuario (2026-10-29) con Google OAuth y JWKS asimétrico verificado; `AUTH_MODE=demo_header` se mantiene. Auth web: validador JWT implementado; enlace `auth_subject` por CLI (solo rol `admin`, auditado) y bootstrap `create-admin` (solo `admin`, sin enlace, solo `real_test`/`simulation`, una activa por organización; D-071/D-072); `/staff` y accesos de Telegram exigen `StrongActor`. **G1 (solicitudes con campanita, vencimiento del acceso web, superadministradora y aprobación que otorga `admin`; D-117/D-119/D-130) está implementado en código; `0012` está aplicada solo a `farmacia_demo` y NO a `farmacia`.** El endpoint público exige `WEB_ACCESS_ORGANIZATION_ID`; para pruebas con `AUTH_MODE=demo_header` la identidad se envía en `X-Demo-Subject`/`X-Demo-Email`/`X-Demo-Name`. La superadministradora se marca una sola vez con `set-superadmin <app_user_id>` (CLI, auditado con actor nulo). **La administradora de pruebas «Yarella Fernández» existe en `real_test` (`83c03aa5-6496-4bef-8f96-b92cbbd59f6e`, solo `admin`, separada de Pao) y su identidad está enlazada**; A2/A3 verificaron que Google y correo+contraseña dan el mismo `sub` y que `/api/v1/me` responde 200 (`docs/AUTH_SUPABASE.md`, D-064 a D-072). Pao sigue sin `auth_subject` en `real_test`, `simulation` y `legacy_mixed`; se enlazará con su propia identidad más adelante (D-067).
Patrón de servicios: `services/*` reciben `Actor` + sesión, verifican permisos, no hacen commit (la API usa `transaction()`); auditoría+outbox en la misma transacción (`services/events.emit`).
Cambios de esquema: migración Alembic + modelo; el test `test_modelos_y_migracion_estan_sincronizados` debe seguir pasando.
