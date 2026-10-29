# H1-B — Telegram, sesiones de auxiliar, fotografías e interpretación contextual

Estado (2026-10-28): **implementado y verificado con simuladores**. Telegram real y visión real **NO** se han
probado (falta el token del bot y no hay proveedor de visión conectado). Ver «Qué se verificó y qué no».

## 1. Flujo

```
Teléfono compartido ──► Telegram ──► webhook (secreto de origen)  o  getUpdates (poller local)
                                           │
                         app/telegram/  (adaptador: parseo, cliente, entrega)
                                           │ ChannelEvent normalizado
                         app/services/channel.py  (sin Telegram)
                          ├─ sessions.py  (auxiliar activo, expiración, PIN preparado)
                          ├─ ¿hay identidad vigente?  no → mensaje PENDIENTE de atribución
                          │                           sí → segmentos → interpretación → observación
                          └─ fotos → photo_processing.py → visión (interfaz) → mismas reglas
                                           │
              interpretation/ (propone)  ──►  domain/ + services/demand.py (valida y decide)
                                           │
                       PostgreSQL: observación + auditoría + outbox + respuestas en cola (una transacción)
                                           │
                         entrega de la cola con reintentos ──► Telegram
```

* **Idempotencia**: `inbound_event` único por (proveedor, chat, `update_id`); el webhook siempre responde 200 tras
  persistir. Un reintento de Telegram o dos entregas simultáneas producen **un solo** efecto. Cada observación usa
  la clave `tg:{chat}:{update_id}:{n}`.
* **Una transacción por evento**: sesión, dominio, auditoría y respuestas. Si falla, rollback completo, el evento
  queda `failed` y `retry_failed_events` lo reprocesa (idempotente). El reporte nunca depende de que el envío funcione.
* **Concurrencia**: bloqueo por chat (un evento a la vez), por clave de idempotencia y por familia de producto.
* **Acceso**: antes de guardar el evento se resuelve la cuenta autorizada del chat; dentro de la transacción se vuelve a
  validar. Sin cuenta activa no hay registro ni consulta: solo el aviso de «no autorizado» y la invitación a `/solicitar`
  (que no concede nada hasta que una administradora aprueba desde el frontend). Ver §2, multicuenta.

## 2. Sesiones de auxiliar (identidad DECLARADA)

* Chat autorizado = fila `channel_binding`. Un chat desconocido se ignora y el bot le informa su ID.
* «Reportando como Ana» aparece en cada confirmación; `¿Quién soy?` y `Cambiar auxiliar` siempre disponibles.
* A lo más **una** sesión activa por chat (índice único parcial). Expira por **inactividad** (deslizante,
  `SESSION_IDLE_MINUTES`, 10 por defecto). Una sesión expirada no identifica a nadie.
* Auditoría de `session.selected | changed | replaced | expired | manual` (los tres últimos con su motivo).
* **Sin identidad**: el mensaje queda `pending_attribution`. Identificarse **no** lo atribuye: el bot pregunta si se
  anota a nombre de esa persona; solo con confirmación explícita pasa a `attributed` (`confirmed_after_identification`).
  Si tiene más de `PENDING_ATTRIBUTION_MAX_AGE_MINUTES` (15) ya no se puede atribuir: se descarta y se pide reenviar.
* Cada observación guarda autor, fecha, sesión, mensaje, origen e `identity_level` (`declared`/`pin`); son inmutables.
* **PIN individual (piloto)**: `verify_pin_attempt` ya implementa hash scrypt, 5 intentos y bloqueo de 10 min, con el
  contador **persistido** aunque falle. El adaptador aún no lo pide (`SESSION_REQUIRE_PIN=true` se rechaza al arrancar).
  Ojo para el piloto: un PIN escrito en el chat queda en el historial de Telegram; evaluar teclado numérico inline.

### Personal, disponibilidad y botones dinámicos
Los botones de `/start` y «Cambiar auxiliar» se leen de PostgreSQL en cada consulta: solo personas **activas y disponibles** con rol que permite
reportar. Estar de vacaciones, con licencia, de día libre o ausente por otro motivo las oculta; desactivarlas también. El backend lo valida
**además**: un botón antiguo es rechazado («no está disponible para reportar (vacaciones)») y se muestran los botones vigentes; si la persona deja
de estar disponible con una sesión abierta, esta se revoca y el siguiente mensaje queda pendiente de atribución. «Finalizar reportes» cierra la sesión.
Se administra por `/api/v1/staff` (rol administrador). Pertenecer, estar disponible y tener sesión activa son tres estados distintos y se listan por separado.

### Cuentas autorizadas y teléfonos personales (multicuenta)

Un mismo bot, varias cuentas de Telegram. Se distinguen **tres cosas**: la **cuenta** (`telegram_account`, lo que se autoriza),
el **chat/vínculo** (`channel_binding`, dónde llegan los mensajes) y la **persona** (`app_user`, quién reporta). Ninguna se
deduce de otra.

| Modo de la cuenta | Quién la usa | Identidad al reportar |
|---|---|---|
| `shared_device` | Teléfono compartido de la farmacia | Se **elige auxiliar** como siempre (sesión `declared`, botones dinámicos); sin elegir, el mensaje queda pendiente y no se atribuye |
| `personal` | Teléfono personal de Pao, Danny o alguien futuro | **Fija** a la persona ligada a la cuenta (`identity_level='account'`); no hay selector y `/cambiar` se rechaza. El registro queda con `attribution_basis='authorized_account'` |

* **Autorización en el servidor, dos veces**: al recibir el evento (antes de guardarlo) y otra vez dentro de la transacción
  que lo procesa. Si la cuenta se revoca entre ambos momentos, no se registra nada. Los botones no autorizan.
* **Solicitud y aprobación** (reemplaza las invitaciones): un chat privado desconocido pide acceso con `/solicitar`
  (o abre con `/start`, que solo explica). La solicitud queda `pending` y **no crea cuenta ni vínculo**: no hay acceso
  operativo. El bot entrega un código temporal (20 caracteres, ~100 bits) que **vence a los 30 minutos**, es de un solo
  uso y tiene límites de intentos (5 por código y 10 por administrador cada 15 minutos). En la base queda solo su
  SHA-256 y el código **no** se persiste en eventos, mensajes, outbox, auditoría, logs ni URLs. Un `update_id` nuevo
  rota el código; hay **una sola solicitud pendiente por cuenta**. Si alguien escribe el código en el chat, el bot **no
  lo guarda** y le pide compartirlo con la administradora.
* **Aprobación desde el frontend** (`POST /api/v1/telegram-access-requests/lookup` y `…/{id}/approve`): Pao busca el
  código, revisa la cuenta y define etiqueta, modo, persona (si es personal) y `access_role`. Conocer el código **no**
  autoriza: aprobar, rechazar, bloquear y rehabilitar exigen sesión administrativa fuerte y el permiso `manage_accounts`
  resuelto en PostgreSQL. El bot confirma la aprobación **sin** reenviar el código.
* **Sesiones independientes**: cada chat tiene su propia sesión y su propio bloqueo; dos teléfonos pueden reportar a la
  vez sin pisarse. Un chat pertenece a una sola cuenta y a una sola organización.
* **Bloqueo y rehabilitación sin borrado**: bloquear deja la cuenta `blocked`, desactiva sus vínculos y cierra sus
  sesiones (`end_reason='revoked'`); no se borra ningún registro. Rehabilitar reutiliza **la misma fila**, recrea sus
  vínculos privados y conserva el historial (`/telegram-accounts/{id}/history`). Una cuenta bloqueada puede volver a
  solicitar acceso.
* `access_role` (`admin`/`operator`) es **descriptivo**: prepara la separación administrativa/operativa, pero las
  restricciones de negocio no están definidas y no se inventaron. Los permisos reales siguen en `app_user.roles`.
* En grupos no se solicita acceso (no se puede saber con certeza quién escribe); los grupos siguen el camino anterior.
* Los vínculos que ya existían en organizaciones reales se conservan: la migración los asocia a una cuenta
  `shared_device` con el mismo id del chat privado. La organización de simulación no se toca. El chat personal de
  desarrollo/pruebas mantiene su acceso y su selector de auxiliares.
* La organización de acceso se configura explícitamente por entorno (`TELEGRAM_ACCESS_ORGANIZATION_ID`, sin valor por
  defecto): si falta, el bot no crea solicitudes y lo dice.

## 3. Lo que el bot hace y NO hace

Hace: registrar avisos, consultar la lista (páginas), mostrar/cambiar identidad, recibir fotos.
**No** aplica cantidades (la cantidad escrita es solo *sugerencia* en la observación), no aprueba, no compra.
Saludos y preguntas no crean faltantes. Un mensaje con varios productos (un producto por línea, `;` o coma) crea
una observación por producto. Una edición de mensaje **no** reaplica nada (AC38).
Las respuestas son breves y no aseguran identificación: «no puedo asegurar cuál producto es», «no la supuse».

## 4. Interpretación contextual (dos responsabilidades)

* **`app/interpretation/`** (propone): números en palabras («cincuenta mil»), sinónimos, alias de expresión
  («la D»), candidatos del catálogo verificado / lista / referencias comerciales, y la interfaz `AIInterpreter`
  (sin implementación). Devuelve atributos, candidatos, procedencia y motivos de incertidumbre.
* **`domain/` + `services/`** (decide): compatibilidad, unidades, deduplicación, cantidades, idempotencia, auditoría.
  Solo un alias verificado o una persona autorizada confirman. Lo que proponga una IA se degrada a candidato
  `ai_unverified`; cifras no presentes en el texto, unidades inválidas o «confirmado» dicho por el modelo se descartan.
* **Identidad farmacéutica** (principio activo + potencia/concentración + forma + liberación) **≠ identidad comercial**
  (marca, laboratorio, fabricante, titular del registro, comercializador, GTIN, registro sanitario, contenido, envase).
  Cada entidad comercial es independiente y *desconocida* mientras nadie la diga (jamás se deduce una de otra ni del
  aspecto del envase). Mismo principio/potencia/presentación con **distinto laboratorio** no se fusiona solo y tiene
  clave comercial distinta. El catálogo comercial completo llega en H3 y colgará de `pharma_identity`
  (1 identidad farmacéutica → N referencias comerciales); hoy existe su embrión (`commercial_reference`).

## 5. Fotografías

Modalidades: texto, foto sin texto, foto + texto/cantidad. Flujo: recepción (referencia + autor) → descarga y
almacenamiento privado (tipo por firma de bytes, tamaño máximo, permisos 0600, SHA-256) → visión → traducción con las
**mismas reglas** que un texto → observación.

| Estado | Significado |
|---|---|
| `pending` | recibida; sin proveedor de visión o aún sin procesar (sin identidad no se procesa) |
| `processing` | reclamada por un procesador |
| `identified` | se leyó un producto de forma legible. **No** implica identidad completa ni confirmada |
| `needs_review` | borrosa/parcial/sin producto/en conflicto con el texto |
| `error` | descarga o lectura fallidas; se reintenta (hasta 3) |

Contrato `VisionExtraction`: `mode` (`live`|`simulated`), proveedor, modelo, legibilidad y campos
`{attribute, text, confidence}` para ingrediente, nombre, potencia, concentración, contenido, forma, envase,
liberación, marca, laboratorio, fabricante, titular, comercializador, GTIN, registro y cantidad visible.
Reglas: todo lo leído de imagen queda `inferred` con `origin="image"`; confianza < 0.5 se descarta; la **cantidad
visible NO es cantidad solicitada** (solo el texto del auxiliar la sugiere); GTIN con dígito verificador inválido se
rechaza; si solo se ve la marca, esta sirve de **ancla** (`name_anchor`) sin afirmar principio activo; foto vs texto
en contradicción → conflicto, a revisión; la misma imagen (hash) del mismo autor no genera otra observación.
**Caso Migranol** (foto sin texto): se registra marca «Migranol®», «Laboratorio Bagó de Chile S.A.», forma
«comprimido recubierto» y contenido 10, todo con procedencia de imagen; potencia, principio activo, GTIN, fabricante y
titular quedan desconocidos y pendientes.

## 6. Qué se verificó y qué no

| Componente | Verificado con | NO verificado |
|---|---|---|
| Lógica de sesiones, canal, dominio, auditoría | PostgreSQL 17 **real** | — |
| Formato de llamadas a la Bot API, webhook, polling, reintentos | **Simulador** de Telegram (`tests/tgsim.py`, `httpx.MockTransport`) | Servidores reales de Telegram |
| Lectura de imágenes | **Simulación** etiquetada `mode="simulated"` (resultados escritos a mano) | Cualquier IA con visión real |
| Foto de Migranol del usuario | Transcrita a mano en `MIGRANOL`; **ningún modelo la procesó** | Lectura real de esa imagen |
| Entrega real de mensajes, límites de Telegram, mensajes en grupos | — | Pendiente al crear el bot |
| Autorización multicuenta: solicitud, código, aprobación, bloqueo y rehabilitación | PostgreSQL 17 **real** + simulador del canal (solicitud, vencimiento, intentos, concurrencia, bloqueo/rehabilitación, API) | Solicitud y aprobación reales desde teléfonos y frontend |

## 7. Crear el bot y configurar el token de forma segura

1. En Telegram abre **@BotFather** → `/newbot` → nombre y usuario (debe terminar en `bot`). BotFather te entrega un
   **token** `123456789:AA…`. Trátalo como una contraseña: **no lo pegues en este chat ni en git**.
2. (Opcional) `/setprivacy` → *Disable* solo si usarás el bot en un grupo. En chat privado no hace falta.
3. Guarda el token en tu `.env` local **sin que quede en el historial del shell** (zsh):
   ```bash
   cd ~/ia-lab/proyecto-farmacia
   read -s "TOKEN?Pega el token y presiona Enter: "; echo
   printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TOKEN" >> .env; unset TOKEN
   chmod 600 .env
   ```
4. Comprueba el token (no lo imprime): `cd backend && .venv/bin/python -m app.cli telegram-check`
5. Abre una conversación con tu bot y envía `/start`: responderá que el chat no está autorizado y explicará
   `/solicitar`. Con `/solicitar` el bot crea la solicitud y entrega un **código temporal** (30 min).
6. Escucha sin exponer nada a internet (long polling): `.venv/bin/python -m app.cli telegram-poll`
   y vuelve a escribir en el chat.
7. La aprobación **no** se hace por CLI: la administradora busca el código en el frontend (API
   `POST /api/v1/telegram-access-requests/lookup`) y aprueba ahí. Ver «Solicitar y aprobar accesos».
   Para desarrollo/bootstrap sigue existiendo `.venv/bin/python -m app.cli bind-telegram NNN`,
   que crea una cuenta `origin='manual'` y **no** imprime códigos.

### Solicitar y aprobar accesos

El bot **no autoriza**: solo recibe la solicitud y entrega el código. La decisión se toma en el frontend
(Configuración → Accesos Telegram) o, mientras no exista pantalla, por API con una sesión administrativa fuerte.

1. **La persona pide acceso** desde su teléfono, en chat privado: `/solicitar`. El bot responde con el código y le
   pide compartirlo con Pao. La solicitud queda `pending` y **no habilita nada**. Si vuelve a pedir, el código
   anterior deja de servir y recibe uno nuevo; no hay más de una solicitud pendiente por cuenta.
2. **Pao busca el código** (`POST /api/v1/telegram-access-requests/lookup` con `{"code": "..."}`). Ve la solicitud y,
   si la cuenta ya existía (por ejemplo, bloqueada), el historial que se reutilizaría. Intentos fallidos quedan
   limitados (5 por código, 10 por administrador cada 15 min) y la respuesta es siempre la misma.
3. **Pao aprueba** (`POST /api/v1/telegram-access-requests/{id}/approve`) definiendo:
   `display_label`, `mode` (`personal`/`shared_device`), `app_user_id` (obligatorio en personal, prohibido en
   compartido) y `access_role` (`admin`/`operator`, descriptivo). El backend valida permisos, vigencia y persona
   activa (la disponibilidad se exige al reportar); recién entonces crea/reutiliza la cuenta, crea el vínculo y
   notifica por Telegram **sin código**.
4. **Verificar**: `GET /api/v1/telegram-accounts` (cuenta, modo, persona, chats, estado) y
   `GET /api/v1/telegram-access-requests[?status=pending]`. También la CLI de desarrollo:
   `.venv/bin/python -m app.cli list-telegram-accounts --org real --by <admin>`.
5. **Bloquear / rehabilitar** desde el frontend:
   `POST /api/v1/telegram-accounts/{id}/block` y `POST /api/v1/telegram-accounts/{id}/reactivate`.
   Bloquear desactiva vínculos y cierra sesiones; rehabilitar reutiliza la misma cuenta. Ninguno borra historial:
   `GET /api/v1/telegram-accounts/{id}/history`. Por CLI: `block-telegram-account` y `reactivate-telegram-account`.
6. **Sesión administrativa**: en `real_test`/`production` estas rutas exigen `Authorization: Bearer <JWT de Supabase>`
   (`AUTH_MODE=supabase_jwt`) y `manage_accounts`; la cabecera de demostración solo se acepta en la organización de
   simulación. La organización se configura con `TELEGRAM_ACCESS_ORGANIZATION_ID`, sin valor por defecto.

**Webhook (más adelante, requiere tu autorización explícita):** exige una URL HTTPS pública y
`TELEGRAM_WEBHOOK_SECRET` (16–256 caracteres `[A-Za-z0-9_-]`), que se registra con `setWebhook?secret_token=…`.
No se activó ni se expuso el backend. Mientras no haya secreto, `/api/v1/integrations/telegram/webhook` responde 503.
`VISION_PROVIDER=simulated` + `VISION_SIMULATION_FILE` solo sirve para demo; sin proveedor de visión las fotos se
guardan y quedan `pending`.

## 8. Limitaciones y pendientes

* Sin verificar contra Telegram real ni con visión real (ver §6). La solicitud, el código, la aprobación, el bloqueo y la
  rehabilitación están probados contra PostgreSQL real con el simulador del canal, **no** desde teléfonos reales.
* Multicuenta: en grupos no se solicita acceso; una cuenta `personal` no puede elegir otra identidad; falta la pantalla
  de accesos en Lovable (hoy solo API y CLI de desarrollo) y el proyecto Supabase no existe todavía, así que en
  organizaciones reales la API administrativa no puede ejercerse hasta configurarlo. `access_role` es descriptivo: las
  restricciones entre cuentas administrativas y operativas **no están definidas** (D-053).
* No hay catálogo comercial completo (H3): hoy solo candidatos desde un catálogo mínimo verificado por compras.
* Un mensaje «ibuprofeno y paracetamol» (con «y») se interpreta como un solo producto; se separa por línea, `;` o coma.
* El PIN por Telegram y grupos con varios chats por farmacia: no implementados.
* Dashboard/SSE (H1-C) y AC35 («en pedido»), AC15 (SSE): pendientes. AC13 (tarjeta de confirmación editable de la
  extracción) existe como API (`POST /demand-items/{id}/resolve`) pero no como pantalla.
* Retención/eliminación de fotos y datos personales visibles en imágenes: sin política definida (HLD §17.4).
* Una foto con varios productos produce una sola lectura (un producto por foto).
