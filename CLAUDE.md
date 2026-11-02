# CLAUDE.md — Farmacia Parque Lantaño

Asistente de compras con IA (demo → producción). Idioma de trabajo y UI: español de Chile.

## Fuentes de verdad
- `docs/Farmacia_Parque_Lantano_HLD_LLD.md`: requisitos, invariantes (§2), AC01–AC40. **No editar**; registrar
  cambios en `docs/decisions/`. Si el prompt del usuario contradice el HLD, rige el usuario y se registra.
- `docs/ARQUITECTURA.md`, `docs/ROADMAP.md`, `docs/decisions/0001-registro-de-decisiones.md`: estado vigente.
- `docs/contracts/cotizador-v0.2.md`: contrato del cotizador **revisión 7**, aprobado conceptualmente en D0
  (2026-10-30) y **no implementado**. Define E10–E15, la cobertura de faltantes, la regla de cierre y la aprobación
  web que otorga `admin` (D-130). Nada de él se implementa sin orden explícita.

## Reglas permanentes
- **Git:** rama `master`. NO ejecutar commit, push, init, remote add ni renombrar ramas sin orden explícita.
  Nada de GitHub ni despliegues sin autorización.
- Consultar antes de: servicios externos, costos, credenciales, publicar. Nunca inventar secretos, proveedores,
  APIs, precios, stock ni enlaces. Mocks/fixtures solo en pruebas o etiquetados `fixture`.
- Invariantes críticos: un aviso repetido no suma cantidad; cantidad desconocida = NULL (no 0/1); sin fusión
  automática de dosis/formatos distintos; solo `SET/ADD/CLEAR` explícitos; aprobaciones ligadas a snapshot;
  referencia de cadena ≠ oferta comprable; compra real **bloqueada**; el LLM no decide dinero, cantidades ni permisos.
- **Cotizador (D-113 a D-132; contrato v0.2 rev7):** el MVP demuestra *faltante → búsqueda → comparación →
  cotización → compra*; **no** es inventario ni recepción. La recepción física y las diferencias de entrega están
  **fuera del MVP** (D-126). El ciclo se cierra **solo** con compras confirmadas que cubren toda la cantidad pedida
  conocida; cotizar cubre de forma **provisional** y nunca cierra. **Compra todo o nada (enmienda a D-127, 2026-11-02):**
  se registra completa —todos los productos y cantidades seleccionados— o no se registra; al registrarse, los faltantes
  cubiertos se cierran y se liberan para nuevos reportes por Telegram. **No hay compras parciales por productos ni
  cantidades en la demo** y no se implementa lógica para ellas. Dos saldos distintos: `pending_quantity` = falta
  **cotizar**, `pending_purchase_quantity` = falta **comprar**. `fulfilled` **no** significa recibido. Telegram solo
  lee: no registra compras, no aplica cantidades ni confirma llegadas. Cotizar y comprar son `admin` + `StrongActor`
  (D-130). Observaciones comerciales: `/api/v1/commercial-observations`, separadas de los avisos de faltante
  (`/api/v1/observations`, D-129).
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
  migración `0013`).
- **Aplicado a `farmacia`:** solo hasta **0008** (32 tablas). Las `0009`–`0013` existen en el código y **no** están
  aplicadas. `farmacia_demo` tiene aplicada **solo la `0012`** (2026-11-02, con respaldo `pg_dump` verificado); la
  `0013` no está aplicada a ninguna base. Ninguna migración nueva se aplica sin autorización explícita.
- **Solo documental (aprobado, sin código):** contrato v0.2 **revisión 7** completo —E10–E15, `quote`/`quote_cart`/
  `quote_line`, compras registradas y su cierre, historial, observaciones (D-132), reporte repetido por Telegram
  (D-131), búsqueda de cadenas E15, `relation`— y las decisiones **D-113 a D-132**. No hay endpoints de cotizador,
  compras ni recepción; la recepción está fuera del MVP (D-126). G1 (D-130) y C4 **sí** están implementados.
- **Sin empezar:** C3 (`0014`), C5-A (`0015`), O1–O4 (`0016`), C5-B (`0017`), C5-C (`0018`), C5-D (`0019`,
  bloqueado), T1, C6/C6a y H2–H6.

## Metodología
Por fases con informe (objetivo, alcance, archivos, implementación, pruebas, aceptación, riesgos, continuar).
Hecho: F0, **H1-A**, corrección del modelo de productos, **H1-B** con simuladores y **H1-B.4**
(solicitud/aprobación/bloqueo de accesos de Telegram; ver `docs/H1B_TELEGRAM.md`); **C1** (migración `0008` aplicada a `farmacia`); **C2** implementado **en código** (`catalog-load`, conector `manual_import`, perfiles de importación, estado de revisión de oferta y endpoint E8 con idempotencia; migraciones `0009`–`0011`, **no aplicadas a `farmacia`**; D-109 a D-111); **D0 cerrado el 2026-10-30** (commit `38adb6b`: contrato v0.2, decisiones D-113 y D-115 a D-127, roadmap); la **fase documental del 2026-10-30** (contrato **revisión 6**, **D-128**, alineación de `CLAUDE.md`, `docs/AUTH_SUPABASE.md` y `docs/ROADMAP.md`); la **Fase 0 del 2026-11-02** (commit `fe3518e`: preparación para Heroku, proveedores, fuentes y catálogo ficticio; D-129); **G1 implementado en código el 2026-11-02** (contrato **revisión 7**, **D-130 a D-132**, migración `0012`, 16 pruebas nuevas; `0012` aplicada **solo a `farmacia_demo`**) y **C4 implementado en código el 2026-11-02** (E1/E2/E3 + E15, migración `0013` **sin aplicar**, 14 pruebas nuevas; suite **486** con `REQUIRE_DB=1`).
El siguiente paso es **C3** (`0014`), después **C5-A** (`0015`), O1–O4 (`0016`), **C5-B** (`0017`), C5-C (`0018`) y C5-D (`0019`, ejecución bloqueada). Siguen abiertas **H1-B.5** (verificar con el bot real; el usuario crea el bot y guarda el token en `.env`), H1-C (SSE + dashboard Lovable) y las autorizaciones externas (cadenas, OpenAI, almacenamiento).
No declarar pruebas de BD como exitosas si se omitieron: validar con `REQUIRE_DB=1 .venv/bin/pytest` (PostgreSQL local por Docker; `open -a Docker`).
Supabase: proyecto `faltantes-farmacia` creado por el usuario (2026-10-29) con Google OAuth y JWKS asimétrico verificado; `AUTH_MODE=demo_header` se mantiene. Auth web: validador JWT implementado; enlace `auth_subject` por CLI (solo rol `admin`, auditado) y bootstrap `create-admin` (solo `admin`, sin enlace, solo `real_test`/`simulation`, una activa por organización; D-071/D-072); `/staff` y accesos de Telegram exigen `StrongActor`. **G1 (solicitudes con campanita, vencimiento del acceso web, superadministradora y aprobación que otorga `admin`; D-117/D-119/D-130) está implementado en código; `0012` está aplicada solo a `farmacia_demo` y NO a `farmacia`.** El endpoint público exige `WEB_ACCESS_ORGANIZATION_ID`; para pruebas con `AUTH_MODE=demo_header` la identidad se envía en `X-Demo-Subject`/`X-Demo-Email`/`X-Demo-Name`. La superadministradora se marca una sola vez con `set-superadmin <app_user_id>` (CLI, auditado con actor nulo). **La administradora de pruebas «Yarella Fernández» existe en `real_test` (`83c03aa5-6496-4bef-8f96-b92cbbd59f6e`, solo `admin`, separada de Pao) y su identidad está enlazada**; A2/A3 verificaron que Google y correo+contraseña dan el mismo `sub` y que `/api/v1/me` responde 200 (`docs/AUTH_SUPABASE.md`, D-064 a D-072). Pao sigue sin `auth_subject` en `real_test`, `simulation` y `legacy_mixed`; se enlazará con su propia identidad más adelante (D-067).
Patrón de servicios: `services/*` reciben `Actor` + sesión, verifican permisos, no hacen commit (la API usa `transaction()`); auditoría+outbox en la misma transacción (`services/events.emit`).
Cambios de esquema: migración Alembic + modelo; el test `test_modelos_y_migracion_estan_sincronizados` debe seguir pasando.
