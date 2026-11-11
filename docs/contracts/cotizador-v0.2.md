# Contrato del cotizador — v0.2 (cotizaciones, compras, historial, observaciones, accesos y precios públicos)

**Estado: revisión 10-A6 aprobada el 2026-11-03.** Esta revisión aprueba **solo A6** («Enseñar a la IA», D-136
precisada por D-138, D-141, D-143 y D-144): interpretación estructurada **por producto**, preferencias comerciales y **orden
determinista de sus ofertas**, sin ocultar ninguna, sin explicaciones ni puntuaciones visibles y con traza interna.
La revisión 10 **completa sigue pendiente**: **D-139** (C9, cotización cerrada preparada para comprar) y **D-140**
(C11, compra adicional para inventario) **no están aprobadas aquí** y conservan el texto histórico de la revisión 9.
El texto propuesto para esa parte sigue en `docs/contracts/cotizador-v0.2-rev10-propuesta.md`.

**Implementación real (2026-11-03).** E10 y O1 (C5-A), D-135 y D-137 están implementados en código. De A6 se
implementaron el **esquema y los modelos** (`observation_interpretation`, `commercial_preference`,
`preference_order_trace` y las columnas opcionales `offer_snapshot.laboratory_as_published` y
`brand_as_published`, migración `0018`), el **motor determinista de orden y traza** (D-143) y el **retiro explícito**
(`retired_subject_kinds`, migración `0019`, D-144). La **Fase 3A (D-145)** implementó además la **interpretación
interna con OpenAI**: el servicio `app/services/observation_ai.py`, el adaptador sin SDK
`app/interpretation/openai_observations.py` y la validación estricta de la salida en
`app/interpretation/commercial.py`. La suite usa un doble del proveedor: **ninguna prueba hace llamadas reales**.
Los **endpoints de interpretación y la conexión con Lovable todavía no existen** y se implementan en fases posteriores.
Nada de esto está aplicado a `farmacia` (sigue en `0008`) ni a `farmacia_demo` (sigue en `0015`); `0018` y `0019` solo
se aplicaron en `farmacia_test`, la base aislada que las pruebas recrean. Las migraciones
`0016`–`0017` tampoco están aplicadas a `farmacia` ni `farmacia_demo`. Las compras registradas y su cierre, el historial y la búsqueda de cadenas E15
siguen sin implementar. La revisión 5 se aprobó en el cierre de D0; este encabezado corrige la contradicción que la
llamaba «borrador no aprobado» (**C-0**, resuelta en D-128). Este documento **no modifica C0**
(`docs/contracts/cotizador-v0.md`, congelado por D-093): lo **amplía** y declara cada ampliación de forma explícita.

**Revisión 3 (fase D0, 2026-10-30):** consolidó **D-113** (búsqueda de precios públicos de cadenas) y **D-115 a
D-120** (mecanismo y captura por cadena; observaciones y aprendizaje con OpenAI; acceso web con Google; control
individual de Telegram; superadministradora; faltantes de Telegram en el cotizador con cobertura reversible).
Incorporó la clasificación **`relation`** junto a `match.level`, las correcciones **F-1 a F-6**, tres tablas nuevas
(`commercial_observation`, `observation_interpretation`, `web_access_request`) y seis columnas nuevas en la revisión 3
(siete respecto del esquema actual).

**Revisión 4 (fase D0, 2026-10-30; superada en parte):** incorporó el **ciclo de reposición** (D-121 a D-125):
cobertura con compras vigentes sin doble conteo (resuelve **P-8**), estado por Telegram sin duplicar necesidades,
`admin` que también informa faltantes y numeración ratificada. Incluía además la **recepción física**
(`receipt`/`receipt_line`), **retirada del MVP en la revisión 5 (D-126)**.

**Revisión 5 (fase D0, 2026-10-30):** retira del alcance implementable del MVP **todo el control de recepción física
y diferencias de entrega** (**D-126**): sin `receipt` ni `receipt_line`, sin cantidades entregadas, sin parciales, sin
saldos pendientes de recepción, sin endpoints ni pantallas asociados y sin cierre condicionado a la recepción. Subsisten
**D-121** (cobertura con compras confirmadas), **D-123** (estado por Telegram sin duplicar necesidades) y **D-124**
(`admin` informa por Telegram). Total: **9 tablas** (6 de la revisión 2 + 3 de la revisión 3) y las mismas **7
columnas** respecto del esquema actual. Ratifica **C4 = 0013** y **C3 = 0014**, y aclara las tres tablas de búsqueda
frente a las dos que nombra C0. **Sustituye la revisión 4**; no cambia C0.

**Revisión 6 (fase documental, 2026-10-30; D-128):** la aprobación de una solicitud de acceso web **otorga el rol
operativo `purchasing`** y habilita el Cotizador de inmediato, sin segunda autorización; el Cotizador (E10–E15) admite
**`admin` o `purchasing`** + `StrongActor`, salvo la revisión de interpretaciones, que sigue siendo de `admin` (§8.3).
**No agrega tablas ni columnas** (siguen 9 y 7), **no crea roles** y **no cambia Telegram**: la web nunca concede
acceso al bot (§9.10). `purchasing` conserva sus permisos actuales y **no se amplía ni rediseña la gestión de alias**
(§9.3, paso 8). Sustituye la cláusula «aprobar no asigna roles» de las
revisiones 3 a 5 y enmienda **D-079**, **D-114** y **D-117** sin reescribir su historial.

**Revisión 7 (corrección funcional, 2026-11-02; D-130):** el Cotizador tiene **un único perfil funcional web:
`admin`**. Se elimina `purchasing` como perfil del Cotizador: la aprobación de una solicitud de acceso crea el
`app_user` con **`roles = ['admin']`**, `active` y `auth_subject` en el mismo paso, sin vencimiento automático, y la
persona obtiene acceso completo. Cualquier `admin` aprueba o rechaza; no hay un aprobador separado. `superadmin` sigue
siendo **control técnico** de la propietaria, no un perfil funcional. `purchasing` y `auxiliary` **siguen existiendo**
para Telegram y los flujos de faltantes, pero **no son perfiles del Cotizador web**. **No agrega tablas ni columnas**
(siguen 9 y 7). Sustituye a D-128; mantiene la independencia con Telegram y la seguridad JWT + `StrongActor`.

**Revisión 8 (corrección funcional, 2026-11-02; D-135):** cada persona tiene su **reporte independiente**. Un aviso de
otra persona sobre el mismo producto **nunca** se registra dentro del faltante ajeno ni se suma: el bot pregunta y, si
se confirma, crea un reporte propio. La línea de cotización puede cubrir **varios reportes** mediante
`quote_line_allocation`, con cantidades **explícitas** cuya suma no supera la cantidad de la línea; no hay reparto
automático. Un reporte se **cancela lógicamente** (`admin` + `StrongActor`), nunca se borra, y las líneas que lo
cubrían se informan para corregirlas antes de cerrar. En Telegram, el mismo autor con cantidad conocida elige **Total
(SET)**, **Sumar (ADD)**, **solo registrar** o **cancelar**; el auxiliar sin `APPLY_QUANTITY` deja una **propuesta**.
Agrega la tabla `quote_line_allocation` (migración `0017`) y columnas de autor, cancelación y confirmación en
`demand_item`, `demand_observation` y `channel_message` (migración `0016`). Enmienda **D-131** sin reescribir su
historial, según se registra en D-135.

**Revisión 9 (corrección funcional, 2026-11-02; D-137):** las asignaciones dejan de ser solo una
verificación al crearlas. Una cotización **no se cierra** si alguna asignación vigente apunta a un
reporte cancelado, sin cantidad pedida o con cantidad menor a lo asignado, ni si la suma asignada
supera la cantidad de la línea: `409 QUOTE_HAS_INCONSISTENT_ALLOCATIONS` con `details.issues[]` para
que la interfaz muestre cada caso. La cantidad pedida de un reporte **no puede bajar** por debajo de
lo asignado (`409 QUANTITY_BELOW_ALLOCATED`), ni quedar sin definir (`409 REPORT_HAS_ALLOCATIONS`),
ni cambiar de unidad (`422 UNIT_MISMATCH`) mientras tenga asignaciones vigentes. Para corregir, la
administración ajusta o quita las asignaciones: en `PATCH` una lista vacía **quita todas**; en el
alta la lista vacía sigue rechazándose. Nada se reasigna ni se corrige solo. No agrega tablas ni
columnas; las migraciones siguen siendo `0016` y `0017`.

**Revisión 10-A6 (aprobada, 2026-11-03; D-136, D-138 y D-141):** sustituye en §1.3, §2.8, §4.2, §5.1, §8.3, §8.4,
§8.5, §8.6, §8.7, §16 y §17 el consejo `advice` visible y la prohibición absoluta de reordenar. Las observaciones
comerciales se interpretan **solo para el `trade_item_id` exacto**, se guardan como preferencias estructuradas y se
aplican mediante un **orden determinista** de las ofertas de ese producto. Todas las ofertas siguen visibles; no se
modifican precios, stock, cantidades, descuentos, impuestos, totales ni elegibilidad; no se muestran explicaciones,
justificaciones ni puntuaciones; la trazabilidad queda en `preference_order_trace` para auditoría interna. La
preferencia más reciente de la misma clase gana; una interpretación `rejected` no reactiva una anterior **ni desactiva la
vigente**; solo `suggested` y `confirmed` participan de la precedencia. Sin preferencias aplicables, el orden es
exactamente el determinista vigente. **C9/D-139 y C11/D-140 no forman parte de esta aprobación** y siguen como
propuesta en §6 y §12.3.

**Corrección de interpretación (2026-11-03; D-143):** precisa la revisión 10-A6. La elegibilidad de E5 siempre va
primero y las preferencias solo ordenan **dentro de cada grupo de elegibilidad**. `rejected`, `needs_review`,
`no_preference` y `failed` no generan efectos comerciales ni desactivan una preferencia vigente. La traza interna
completa modelo, versión del prompt y estado en `applied_preferences`, distingue interpretaciones consultadas de
aplicadas y su fallo no interrumpe `GET /articles/{id}`; en la comparación la auditoría sigue siendo obligatoria. D-143
**no implementó** el retiro de una preferencia vigente; lo incorpora D-144 (párrafo siguiente).

**Retiro explícito de preferencias (2026-11-03; D-144):** representa el retiro con
`observation_interpretation.retired_subject_kinds`, una lista de clases (`laboratory`, `brand`, `supplier`,
`availability`, `price:acceptable`, `price:maximum`). Una interpretación `suggested` o `confirmed` retira solo la clase
indicada **para su `trade_item_id` y organización**; no borra historial, no reactiva preferencias anteriores, no afecta
otras clases ni otros productos, y una preferencia posterior válida puede volver a establecerla. El retiro no se simula
con `status='rejected'`, no crea preferencias ficticias ni copia sujetos. `rejected`, `needs_review`, `failed` y
`no_preference` no retiran ni deshacen un retiro. El precio distingue `price:acceptable` de `price:maximum`. La traza
interna conserva las clases retiradas en `preference_order_trace.retired_subject_kinds`; esa información no se expone
en la API pública.

**Corrección de interfaz de Telegram (2026-11-05; D-147):** precisa §10.5 y §12.7 sin cambiar compras ni D-139. El
mismo auxiliar corrige su **cantidad solicitada** únicamente por **texto libre** (`5`, `5 cajas más`,
`en total 15 cajas`): la conversación de corrección no usa botones inline en ninguna etapa; una cifra sola se pregunta
como total o adicional por texto y no modifica nada hasta que la persona responde. `cancelar` descarta la corrección
pendiente sin cambiar la cantidad. La corrección vive en el
mismo faltante del autor, con historial, y el auxiliar nunca cambia la cantidad objetivo. Un aviso de Telegram sin
cantidad pide **1 unidad genérica** (`quantity_origin="default"`), nunca una presentación supuesta; los registros
históricos sin cantidad no se reescriben y un aviso posterior con cantidad se pregunta como total o adicional. Nunca
se suman unidades distintas: si la cantidad previa es la unidad genérica y la persona escribe `más`, se aclara por
texto qué representa antes de calcular, y sin equivalencia válida solo se ofrece fijar el total. Con el faltante en
carrito no se cambia su cantidad solicitada desde Telegram. Ningún mensaje de Telegram modifica automáticamente
`target_quantity`, tampoco con `APPLY_QUANTITY`: el texto libre deja la propuesta para compras. Los mensajes al
auxiliar no mencionan «cantidad objetivo» ni «propuesta para compras»; esos conceptos quedan en la API y en la base.
Los botones de otros flujos (selección de auxiliar y confirmación de un reporte de otra persona) se mantienen. La API expone
`requested_quantity`/`requested_unit`, `requested_quantity_origin` (y `quantity_origin` por observación) y
`quantity_proposal` separados de `target_quantity`. «Ver faltantes» muestra
todos los reportes activos, con el nombre del auxiliar que reportó cada fila, y su estado real («Pendiente de
compra»/«En carrito»), sin alterar la cobertura
implementada. El teclado usa «Ver faltantes», «Ver auxiliar activo» y «Ayuda»; «Cambiar auxiliar» aparece solo con dos
o más auxiliares y «Finalizar reportes» sale del teclado (`/salir` queda como comando oculto de compatibilidad).
El aviso de aprobación de un teléfono compartido depende de los auxiliares habilitados: con uno solo informa que los
faltantes se registran automáticamente a su nombre; con dos o más mantiene la elección de identidad (D-146, punto 12).

**Corrección de alcance (2026-11-02; enmienda a D-127):** para la demo, la compra es **todo o nada**. Se registra
completa —todos los productos y cantidades seleccionados— o no se registra; al registrarse, los faltantes cubiertos se
cierran y se liberan para nuevos reportes por Telegram. **No hay compras parciales por productos ni cantidades**; no
se implementa lógica para ellas y quedan fuera del alcance de la demo. **Nota de la revisión 10 propuesta (D-140):**
«todo o nada» se refiere a la **selección**, no a que toda unidad deba asignarse a un reporte; la compra puede incluir
unidades sin asignación como **compra adicional para inventario**, sin asignarlas automáticamente ni cerrar reportes.

**Cierre del ciclo (D-127, 2026-10-30):** la cobertura por **cotización** es **provisional** y nunca cierra; solo la
**compra confirmada** cierra, y solo cuando cubre **toda** la cantidad pedida conocida. **Compra todo o nada (enmienda a
D-127, 2026-11-02):** la compra se registra **completa** —todos los productos y cantidades seleccionados— o no se
registra; al registrarse, los faltantes cubiertos se cierran y el producto vuelve a poder reportarse. **No hay compras
parciales por productos ni cantidades en la demo.** `fulfilled` no significa recibido. **Nota de la revisión 10
propuesta (D-139):** la cobertura provisional incluye cotizaciones `draft` y `closed`; cerrar una cotización no le
quita cobertura ni obliga a reabrir para comprar.

**Decisión que lo respalda:** D-114, con las enmiendas de D0 registradas en la misma sección de decisiones, **D-130**
(perfil único `admin`; aprobación web que otorga ese perfil), que sustituye a D-128, **D-135** (reportes
independientes, asignaciones explícitas, cancelación lógica y confirmación en Telegram), **D-137** (integridad de las
asignaciones al cerrar y al reducir cantidades) y, en la revisión 10 propuesta, **D-138 a D-141** (preferencias por
producto, cotización cerrada preparada para comprar, compra adicional para inventario y auditoría interna de la IA).
**Decisiones relacionadas:** D-037 (separación de datos), D-064 a D-072 (Supabase Auth y enlaces), D-079/D-086
(permisos y `StrongActor`), D-087/D-088 (cotas y cálculo), D-091 (criterios comerciales), D-110 (`review_status`),
D-111 (idempotencia de importación), D-112 (investigación de catálogos de cadenas).
**Fuente de verdad:** HLD/LLD `docs/Farmacia_Parque_Lantano_HLD_LLD.md` (§2, §6.2, §7.1, §9, §14.1, §17.1, §22).
**Alcance:** contrato aditivo. No modifica E1–E9 en su forma congelada; agrega campos aditivos a E1 y contratos nuevos
E10–E15. No modifica roles existentes, Telegram ni Supabase Auth; los usa.

> Igual que v0: este archivo **no implementa nada**. No crea rutas, modelos, tablas ni migraciones. El OpenAPI real
> seguirá siendo el de FastAPI (`/openapi.json`); este documento no reemplaza `docs/contracts/openapi.json`.

---

## 1. Qué amplía y qué no

### 1.1 Dos excepciones declaradas a C0

C0 §1 y C0-10 dicen: «ninguna operación crea carrito, propuesta, aprobación ni ejecución». D-114 habilita **dos
excepciones nombradas**, y solo esas:

| # | Excepción | Límite exacto |
|---|---|---|
| **X1** | Crear **cotizaciones y carritos en preparación** | Son documentos de trabajo. No son propuestas aprobadas, no son órdenes y no comprometen compra. |
| **X2** | **Registrar una compra ya ocurrida** fuera del sistema | Es un **hecho documentado**, no una ejecución. El sistema no envía órdenes, no ejecuta pagos y no integra proveedores. |

`purchase_execution` sigue `blocked`. Siguen fuera: aprobaciones, propuestas formales, envío de órdenes y pagos.
**La recepción física queda fuera del MVP (D-126)**: no se registra llegada, cantidad entregada, diferencia ni saldo
pendiente de recepción. Registrar una compra confirma el **término del proceso que administra el MVP**; **no
certifica** que la mercadería haya llegado (§1.3, V-33). Si esa compra confirmada cubre **toda** la cantidad pedida,
además **cierra** el faltante (`fulfilled`); si cubre solo una parte, el ciclo sigue abierto con el saldo (§12.3,
D-127). El diseño de recepción se conserva como funcionalidad futura,
separada del MVP (§15).

### 1.2 Qué no cambia

- E1–E9 conservan forma y semántica. **E5 no cambia**: es el único motor de cálculo comercial.
- `offer_snapshot` sigue siendo inmutable y de solo agregar; `shipping_terms_snapshot` y `audit_event`, igual.
- Los precios públicos de cadenas siguen fuera de ofertas, carritos, subtotales y totales (C0-05, AC20).
- C0-07 sigue vigente: **lo parcial no tiene `total`**. v0.2 no agrega un total a un proveedor parcial; agrega un
  objeto distinto, más estricto, para la cotización completa (§5.6).
- **La IA no decide dinero, cantidades, permisos, precios, stock ni visibilidad de ofertas.** Solo interpreta texto
  libre de observaciones **del producto exacto** y, con una interpretación aplicable, **ordena** sus ofertas sin
  ocultar ninguna, sin explicar ni puntuar (§8).
- La compra real sigue **bloqueada**; registrar una compra no la ejecuta ni la autoriza.

### 1.3 Cuatro planos separados

| Plano | Quién lo produce | Dónde vive |
|---|---|---|
| **Cálculo comercial** | E5 determinista | Se calcula en vivo; no se guarda |
| **Selección** | Pao (persona) | `quote_line` (oferta + cantidad) |
| **Hecho de compra** | Registro manual posterior | `purchase` + `purchase_line` (inmutable) |
| **Observación y preferencia** | Pao escribe; la IA solo interpreta; el backend valida | `commercial_observation` + `observation_interpretation` + `commercial_preference` |
| **Orden de ofertas** | Motor determinista del backend | Se calcula en vivo; la traza queda en `preference_order_trace` |

Ningún plano escribe en otro. El carrito no copia precios; la compra copia todo porque debe sobrevivir sola; una
observación **nunca** altera precios, stock, condiciones, cantidades, elegibilidad ni la **visibilidad** de las
ofertas. La preferencia interpretada **puede ordenar** las ofertas del producto exacto; no las crea, quita, oculta ni
modifica. **Compra no es recepción** (V-33): el MVP no registra llegada física; una compra confirmada marca el término
del proceso administrado, no la entrada de mercadería (§4.2, §12.6).

### 1.4 Ampliaciones aditivas de las revisiones 3 y 5

| # | Ampliación | Superficie | Decisión |
|---|---|---|---|
| A1 | Cotizaciones, carritos y líneas | E10 | D-114 |
| A2 | Compras registradas | E11 | D-114 |
| A3 | Historial comercial | E12 | D-114 |
| A4 | Observaciones comerciales e interpretación con IA **que ordena las ofertas del producto** | E13 | D-116, D-136, D-138, D-141 |
| A5 | Acceso web con Google, solicitudes y superadministradora | E14 | D-117, D-119 |
| A6 | Control individual de Telegram (no confundir con **A6 de `ACUERDOS_FUNCIONALES`**, «Enseñar a la IA») | T1 (reusa API existente) | D-118 |
| A7 | Búsqueda de precios públicos de cadenas | E15 | D-113, D-115 |
| A8 | `relation` en E1 y `coverage_relation` en E10 | Campo aditivo + columna | D-120 |
| A9 | Cobertura de faltantes con compras vigentes, sin doble conteo | E10, campos aditivos de `GET /demand-items` | D-121, cierre por D-127 |
| A10 | ~~Recepción física, parcial y con diferencias~~ | **Retirada del MVP** | D-122, retirada por **D-126** |
| A11 | Estado por Telegram, sin duplicar necesidades; `admin` puede informar | Mensajes del bot + permiso | D-123, D-124 |
| A12 | Preferencias comerciales por producto que ordenan sus ofertas, sin explicación visible | E13, campos aditivos de oferta | D-136, D-138, D-141 |

Ninguna de estas ampliaciones cambia la forma congelada de E1–E9. E1 recibe **un campo aditivo** (`relation`); las
demás son contratos nuevos, subrecursos o reutilización de endpoints existentes. A9 y A11 agregan **campos aditivos**
a `GET /api/v1/demand-items` (`last_raw_text`, `coverage_quantity`, `quoted_quantity`, `purchased_quantity`,
`pending_quantity`, `pending_purchase_quantity`, `is_pending`, `replenishment_state`; D-121 y D-127); no crean endpoints
de lectura nuevos.

### 1.5 Separación de permisos: web, Telegram y superadministración

Son tres planos distintos. **Ninguno otorga el otro.**

| Plano | Se verifica con | No otorga |
|---|---|---|
| **Web** | JWT de Supabase válido + `app_user.auth_subject` + `app_user.active` + `web_access_expires_at` vigente + `roles` | Acceso a Telegram; roles por sí solo no habilita el bot |
| **Telegram** | `telegram_account.status = active` + identidad vigente resuelta en el servidor | Acceso web ni roles |
| **Superadministración** | `app_user.is_superadmin` | Datos operativos de otras organizaciones; solo metadatos administrativos (§9.6) |
| **Cotizador (E10–E12)** | rol **`admin`**, **y** `StrongActor` (D-130) | Nada fuera de su organización; no existe un segundo perfil funcional web |
| **Informar faltantes (Telegram)** | `telegram_account.status = active` + identidad vigente + `REPORT_SHORTAGE` | Acceso web; el rol `admin` **no** habilita el bot por sí solo |

**Sin permisos de recepción (D-126).** El MVP no tiene operación de recepción física, así que no crea permiso, pantalla
ni endpoint para confirmarla. Cotizar y confirmar compras son operaciones web de **`admin`** + `StrongActor` (D-130).

Una persona puede tener Telegram sin web y web sin Telegram. Bloquear Telegram no revoca la web; revocar la web no
bloquea Telegram. La **desactivación de la persona** (`app_user.active = false`) es una suspensión más fuerte y corta
ambos planos. El token de Telegram nunca se expone en la API web.

**Aprobar una solicitud web no concede Telegram (D-130).** No crea cuenta de Telegram, ni vínculo, ni solicitud, ni
autoriza chat alguno; la autorización del bot sigue siendo por cuenta y por chat (D-118). El selector del teléfono
compartido es identificación declarada dentro de un chat ya autorizado, no una lista de autorizados (§9.10). La
persona aprobada recibe el perfil **`admin`**, que tampoco la habilita en el bot: son planos distintos.

**`admin` también informa faltantes (D-124).** El rol `admin` conserva sus permisos de gestión y **además** obtiene
`REPORT_SHORTAGE`; no se crea un rol nuevo ni se fusionan planos. Para usar Telegram, una administradora necesita su
propia cuenta de Telegram autorizada, exactamente como cualquier auxiliar. No se agrega ninguna exclusión que le
impida reportar. La matriz actual (`app/core/permissions.py`) **no** le concede `REPORT_SHORTAGE` a `admin`: el cambio
de matriz se implementa en la fase que habilite T1/C5-A, no en D0.

### 1.6 Superficie de interfaz acordada (no se modifica)

El mockup aprobado es `docs/Pegado 30:10:2026 a la 1.33.03 p.m..png`. **No se modifica en esta fase.** La pantalla
principal mantiene este orden:

1. Búsqueda («¿Qué estás buscando?»).
2. **Precios públicos de cadenas farmacéuticas** (solo referencia; nunca ofertas).
3. **Productos faltantes (Telegram)**.
4. **Comparación de ofertas por proveedor**.
5. **Mis cotizaciones** (carritos independientes por proveedor y subtotal pendiente).

Menú lateral: Cotizador, Mis cotizaciones, Historial de cotizaciones, Proveedores, Configuración de Telegram.
Paleta: verde `#17845F`, azul petróleo `#27718A`, fondos claros; **sin tema oscuro**. Lovable consume **solo**
`/api/v1`; no contiene lógica de negocio ni acceso directo a la base.

---

## 2. Modelo de datos

Todas las tablas: `organization_id` con FK compuesta, `data_origin` (`simulated|captured|ai_derived`), sin borrado
físico desde la API, y auditoría en `audit_event` dentro de la misma transacción.

**v0.2 completa 11 tablas:** las 6 de la revisión 2 (`quote`, `quote_cart`, `quote_line`, `purchase`, `purchase_line`,
`purchase_index`), 3 de la revisión 3 (`commercial_observation`, `observation_interpretation`,
`web_access_request`) y **2 de A6/revisión 10-A6** (`commercial_preference`, `preference_order_trace`).
**Columnas nuevas respecto del esquema actual: 9**: `quote_line.demand_item_id` (ya prevista en la revisión 2),
`quote_line.coverage_relation` (nueva en la revisión 3), cinco en `app_user` (§2.10) y las dos columnas opcionales de
`offer_snapshot` (`laboratory_as_published`, `brand_as_published`, §2.7 bis). Las revisiones 4 a 7 no agregan columnas;
A6 agrega las dos de oferta. La revisión 5 no agrega tablas: retira las dos de recepción que proponía la revisión 4
(D-126). La revisión 10-A6 reemplaza la forma de `observation_interpretation` y agrega las dos tablas de preferencias y
traza.

### 2.1 `quote` — cotización general

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid PK | |
| `organization_id` | uuid | FK `organization` |
| `title` | varchar(120) | Texto libre («Reposición semanal»); nunca obligatorio para trabajar |
| `status` | varchar(10) | `draft` \| `closed` |
| `created_by`, `closed_by` | uuid | FK `app_user` de la misma organización |
| `closed_at` | timestamptz | NULL mientras `draft` |
| `data_origin` | varchar(12) | |
| `created_at`, `updated_at` | timestamptz | UTC |
| `version` | integer | Control optimista |

Sin `archive_state`: **la cotización no se archiva por política de compras** (§7.2). Una cotización `draft` o `closed`
permanece operativa indefinidamente. **No hay TTL ni borrado por inactividad** (§5.7).

### 2.2 `quote_cart` — un carrito por proveedor

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid PK | |
| `organization_id`, `quote_id` | uuid | FK compuesta a `quote(id, organization_id)` |
| `supplier_id` | uuid | FK `supplier` de la misma organización |
| `status` | varchar(10) | `active` \| `dismissed` |
| `dismissed_at`, `dismissed_by` | | Solo si es distinto de `active` |
| `data_origin` | varchar(12) | |
| `created_at`, `updated_at`, `version` | | |

`UNIQUE (quote_id, supplier_id)` — un carrito por proveedor y cotización. Se **crea solo** al agregar la primera
línea de ese proveedor; no hay endpoint de creación de carrito y Pao nunca necesita su identificador.
Además, `UNIQUE (id, supplier_id, organization_id)` para que `quote_line` pueda referenciarlo con FK compuesta y no
pueda quedar en un carrito de otro proveedor.

**Un carrito `dismissed` no se evalúa, no cuenta en el total estimado y no cubre faltantes** (F-3, §5.4). Sus líneas
siguen existiendo y se reincorporan sin recrearlas.

### 2.3 `quote_line` — producto seleccionado dentro de un carrito

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid PK | |
| `organization_id`, `quote_cart_id`, `supplier_id` | uuid | FK compuesta a `quote_cart(id, supplier_id, organization_id)`: la línea no puede pertenecer a un carrito de otro proveedor |
| `offer_snapshot_id` | uuid | FK `offer_snapshot` de la misma organización. **Ancla de precio y condiciones** |
| `trade_item_id` | uuid | Copia denormalizada del artículo de la oferta; se valida que coincida |
| `quantity` | numeric(12,3) | **> 0 y entera** (E5 exige entero positivo). No admite desconocido: es una selección explícita |
| `quantity_unit` | varchar(10) | `QuantityUnit`: `box` \| `package` \| `unit` \| `bottle` |
| `status` | varchar(10) | `active` \| `removed` |
| `removed_at`, `removed_by` | | Solo si `removed` |
| `demand_item_id` | uuid NULL | FK compuesta a `demand_item(id, organization_id)`; necesidad interna que esta línea ayuda a cubrir |
| `coverage_relation` | varchar(20) NULL | Clasificación **congelada al agregar**: `exact` \| `other_presentation` \| `combined` \| `related` \| `needs_review` (§12); CHECK de esos cinco valores o NULL |
| `pending_closed_at`, `pending_closed_by` | | Cierre del **pendiente**: Pao desiste de comprar lo que falta. No cambia `quantity` |
| `pending_closed_note` | text NULL | Motivo opcional del desistimiento |
| `data_origin` | varchar(12) | |
| `added_by`, `added_at`, `updated_at`, `version` | | |

`UNIQUE (quote_cart_id, offer_snapshot_id) WHERE status='active'` — dos selecciones de la misma oferta en el mismo
carrito son la misma línea, no una suma. Dos ofertas distintas del mismo artículo **sí** conviven (§6.3).

`demand_item_id` + `coverage_relation` son NULL cuando la línea no viene de un faltante, y van **juntos**: un CHECK
exige que ambos sean NULL o ambos no NULL. **No existe `note_text`**: las observaciones libres viven en
`commercial_observation` (§8), no en la línea.

**No guarda precio.** El precio y las condiciones se recuperan del snapshot referenciado (§3).

### 2.4 `purchase` — compra registrada (inmutable)

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid PK | |
| `organization_id`, `quote_id`, `supplier_id` | uuid | FK compuestas |
| `occurred_at` | timestamptz | Cuándo compró Pao (dato declarado, se guarda tal cual) |
| `registered_at`, `registered_by` | | Cuándo y quién lo registró |
| `currency` | char(3) | Una sola moneda por compra |
| `subtotal`, `taxes_total`, `discounts_total`, `shipping_amount`, `total` | numeric(18,4) NULL | NULL = no determinable; **nunca 0 por defecto** |
| `other_charges` | jsonb | Lista; puede ir vacía |
| `completeness` | varchar(10) | `complete` \| `partial` |
| `blockers` | jsonb | Causas de la incompletitud |
| `notes`, `evidence` | texto / jsonb | Observaciones y procedencia |
| `data_origin` | varchar(12) | |
| `idempotency_key`, `request_hash` | varchar | `UNIQUE (organization_id, idempotency_key)` |
| `supersedes_purchase_id` | uuid NULL | Registro al que este sustituye. **`UNIQUE`** (índice parcial): una compra no puede recibir dos sustituciones |
| `correction_reason` | text NULL | Obligatorio si `supersedes_purchase_id` no es NULL |
| `is_annulment` | boolean, def. `false` | Anulación de un registro hecho por error: **sin líneas**, motivo obligatorio, excluida de totales |
| `annulment_reason` | text NULL | Obligatorio si `is_annulment` |
| `archive_bundle_key`, `archive_sha256`, `archive_bytes` | | Ubicación e integridad del bundle en el almacén externo |
| `archive_status` | varchar(10), def. `none` | `none` \| `pending` \| `verified` \| `failed` |
| `archive_verified_at`, `archive_verified_by` | | Verificación del bundle contra el origen |
| `archive_restored_at`, `archive_restore_ok` | | Ensayo de restauración exitoso |
| `detail_purged_at` | timestamptz NULL | Cuándo salió el detalle de líneas de la base operativa |

**Inmutabilidad real, corregida (F-1).** El encabezado no puede ser «UPDATE siempre bloqueado» y a la vez recibir
metadatos de archivo después. Regla exacta:

1. `UPDATE` directo: **rechazado siempre** por trigger, para el rol de la aplicación.
2. **Única excepción:** función `SECURITY DEFINER` `purchase_mark_archive(...)`, propiedad de un rol dedicado, que
   solo puede escribir las columnas `archive_*` y `detail_purged_at`. La función fija en la transacción
   `app.purchase_archive_update=on`; el trigger solo admite el `UPDATE` con esa marca y solo sobre esas columnas.
3. Esa función exige que **todas las columnas de negocio permanezcan byte a byte idénticas** (comparación de fila
   completa antes/después); si algo más cambia, aborta.
4. Transición de archivo: `none → pending → verified | failed`. **`verified` es terminal**; `failed` puede reintentarse
   con una nueva ejecución auditada que vuelve a `pending`, conservando el intento fallido en `audit_event`.
5. `DELETE` sobre `purchase`: **no existe** (ni ruta, ni permiso, ni función).
6. `DELETE` sobre `purchase_line`: solo por la purga controlada de §7.4.

Una corrección **no edita**: crea un registro nuevo con `supersedes_purchase_id`. No hay columna `status`: el registro
existe porque ocurrió. **El encabezado nunca se elimina** (§7.4); solo su detalle de líneas puede salir de la base
operativa.

Restricciones de coherencia: si `supersedes_purchase_id` no es NULL se exige `correction_reason` (corrección) **o**
`annulment_reason` (anulación), según el caso; `is_annulment = true` implica `annulment_reason` y **cero** líneas de
detalle.

### 2.5 `purchase_line` — detalle histórico completo (inmutable)

Guarda lo que efectivamente se compró, **copiado** en el momento del registro: `trade_item_id`, `external_id`,
`description_as_recorded`, `presentation_as_recorded`, `laboratory_as_recorded`, `quantity` (> 0), `quantity_unit`,
`unit_price`, `currency`, `taxes` (jsonb), `discounts` (jsonb), `shipping_share`, `line_subtotal`, `line_total`,
`source_offer_snapshot_id` (procedencia), `notes`, `data_origin`, y `quote_line_id` NULL cuando la compra no
corresponde a una línea cotizada.

`UNIQUE (purchase_id, quote_line_id) WHERE quote_line_id IS NOT NULL` — una línea cotizada no se compra dos veces
en la misma compra. **Aquí sí se duplican los valores** respecto del snapshot: es la evidencia que debe sobrevivir al
archivo y a cualquier cambio posterior de precios.

Un registro de anulación (`is_annulment = true`) **no tiene líneas**: su detalle es la referencia al registro anulado
más el motivo. Se conserva la evidencia original; la anulación solo la excluye de los totales.

### 2.6 `purchase_index` — índice de búsqueda del historial (permanente)

Una fila por línea de compra. Se escribe **en la misma transacción** que la compra y **sobrevive a la purga** de
`purchase_line`, para que el historial se pueda buscar cuando el detalle ya no está en PostgreSQL.

| Columna | Notas |
|---|---|
| `organization_id`, `purchase_id` | FK compuesta a `purchase` |
| `quote_id`, `quote_line_id` NULL, `supplier_id`, `trade_item_id` | Localización y trazabilidad |
| `occurred_at` | Fecha de la compra |
| `product_label`, `presentation_label`, `laboratory_label` | **Tal como se registró**, nunca normalizado para mostrar |
| `quantity`, `quantity_unit`, `unit_price`, `currency` | Solo estos importes: no impuestos, descuentos, despacho, condiciones ni evidencia |
| `search_text` | Copia normalizada (minúsculas, sin acentos) solo para buscar |

- No tiene `is_current`: la vigencia se deriva de la cadena en `purchase` (anti-join), para que no pueda divergir.
- Un registro de anulación no genera filas (no tiene líneas).
- **No duplica el historial**: sin impuestos, descuentos, despacho, condiciones, notas ni evidencia. Unos cientos de
  bytes por línea.
- Se puebla igual para compras operativas y archivadas: el historial usa **una sola consulta** para ambos casos.

### 2.7 `commercial_observation` — observación libre, inmutable y acumulativa

Una fila por observación. **No hay un campo único de nota por línea**: cada observación es independiente y se acumula.
El texto se guarda **tal como lo escribió** Pao (`text_original`); no se normaliza para mostrar.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid PK | |
| `organization_id` | uuid | FK `organization` |
| `text_original` | text | Texto libre, sin categorías ni selectores; 1–2000 caracteres |
| `author_id` | uuid | FK `app_user` de la misma organización |
| `created_at` | timestamptz | UTC; no editable |
| `quote_id`, `quote_line_id` | uuid NULL | Contexto de la cotización y línea; NULL si se registra desde otra vista |
| `trade_item_id` | uuid | Artículo sobre el que se opina |
| `offer_snapshot_id` | uuid NULL | Oferta concreta observada |
| `supplier_id` | uuid NULL | Proveedor en cuyo contexto se observó |
| `context` | jsonb | Etiquetas **tal como se vieron**: producto, presentación, laboratorio/marca, proveedor, oferta y cotización |
| `data_origin` | varchar(12) | `captured` |

Reglas:

1. **Inmutable y de solo agregar.** Trigger rechaza `UPDATE` y `DELETE` para el rol de la aplicación. No hay edición,
   ni borrado, ni «reemplazo» de una observación.
2. La línea **nunca se borra físicamente** (§5.3), así que no hay observaciones huérfanas.
3. Una observación no se aplica a otro artículo por parecido de nombre, marca o laboratorio. La recuperación es por
   `trade_item_id` exacto (§8.4).
4. El contexto guarda lo que se vio, no una interpretación: `laboratory_as_published` ≠ laboratorio confirmado.

### 2.7 bis Columnas de oferta publicadas por la fuente

`offer_snapshot` agrega dos columnas **opcionales**:

| Columna | Tipo | Notas |
|---|---|---|
| `laboratory_as_published` | varchar(200) NULL | Laboratorio **tal como lo publica la fuente**; NULL = no informado |
| `brand_as_published` | varchar(200) NULL | Marca **tal como la publica la fuente**; NULL = no informado |

Nunca se deducen del nombre del producto, de `trade_item.laboratory` ni de `trade_item.brand`. No cambian el modelo
conceptual de un producto con varias ofertas y laboratorios: cada oferta conserva lo que su fuente publicó, y el
producto conserva sus atributos propios. Si la fuente no lo informa, queda NULL y el motor no inventa una coincidencia.

**Excepción autorizada (D-150, revisión 2026-11-10).** En las listas de precios importadas, cuando la fuente no trae
columna de laboratorio o marca, o la celda viene vacía, el importador puede reconocer el dato en la **descripción
publicada** solo si hay una marca explícita («LAB.», «LABORATORIO(S)», «MARCA») seguida de un nombre ya confirmado en
el **vocabulario controlado** de la organización (columnas del mismo archivo, catálogo propio o listas anteriores con
procedencia de columna). El valor guardado es ese nombre confirmado; el texto exacto que lo respalda y la procedencia
(`column`/`description`) quedan en `offer_snapshot.evidence` (clave `identity`), y el estado de identificación
(`published`/`extracted`/`unconfirmed`) se expone de forma **aditiva** en los endpoints de ofertas importadas. La
columna publicada siempre tiene prioridad y nunca se sobrescribe. **No hay columnas nuevas, ni migración, ni cambios en
el sobre congelado de E8**: los campos nuevos de A3 (`GET /api/v1/suppliers/{id}/offers`) y de la revisión previa
(`import-check`) son **opcionales y aditivos**, y ningún consumidor anterior deja de funcionar. El principio de esta
sección —no inventar identidad por parecido ni desde el nombre del producto— sigue vigente. El detalle completo está en
D-150.

### 2.8 `observation_interpretation` — interpretación de una observación

Una fila por **llamada o intento** de interpretación. Varias filas por observación son válidas (otro modelo, otra
versión de prompt o un reintento). El texto original no se reescribe.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid PK | |
| `organization_id`, `observation_id`, `trade_item_id` | uuid | FK compuesta a `commercial_observation(id, trade_item_id, organization_id)`; **el alcance es el artículo exacto** |
| `status` | varchar(16) | `suggested` \| `confirmed` \| `rejected` \| `failed` \| `needs_review` \| `no_preference` |
| `model`, `prompt_version` | varchar(80), varchar(40) | Trazabilidad del proveedor y del prompt |
| `raw_output` | jsonb NULL | Respuesta estructurada tal como llegó; **nunca se ejecuta ni se muestra** |
| `input_tokens`, `output_tokens`, `latency_ms` | integer NULL | Consumo y latencia informados |
| `error_code` | varchar(40) NULL | Código **interno** (`quota`, `rate_limit`, `timeout`, `unavailable`, `invalid_response`, …); nunca se muestra al usuario |
| `reviewed_by`, `reviewed_at`, `review_note` | | Solo tras revisión humana |
| `retired_subject_kinds` | jsonb | Clases que esta interpretación **retira** para su artículo: `laboratory`, `brand`, `supplier`, `availability`, `price:acceptable` o `price:maximum`; `[]` si no retira ninguna (D-144) |
| `data_origin` | varchar(12) | `ai_derived` |
| `created_at` | timestamptz | |

**Estados y aplicación.**

1. `suggested` y `confirmed` **pueden aplicarse automáticamente** al orden del producto.
2. `rejected`, `failed`, `needs_review` y `no_preference` **no se aplican**. Tampoco generan ningún efecto comercial:
   no ordenan, no desactivan una preferencia vigente ni la reactivan. La ausencia de preferencia no es un retiro.
3. Un fallo **exige** `error_code`; el código solo acompaña `failed` o `needs_review`.
4. Confirmar o rechazar exige `reviewed_by` y `reviewed_at`. `suggested → confirmed | rejected | needs_review`;
   `needs_review → confirmed | rejected`. Los estados terminales (`confirmed`, `rejected`, `failed`, `no_preference`)
   no vuelven atrás.
5. El **contenido es inmutable** (observación, artículo, modelo, prompt, salida, tokens, latencia, error, origen y
   fecha). Solo cambian el estado y los campos de revisión; `DELETE` está prohibido. Reintentar crea otra fila.
6. Si el modelo no entiende o hay ambigüedad relevante, se guarda `needs_review` o `no_preference` y **no se inventa
   una regla**.
7. **Retiro explícito (D-144).** Una interpretación `suggested` o `confirmed` puede retirar una o varias clases mediante
   `retired_subject_kinds`; el retiro vale solo para el `trade_item_id` de la interpretación. Es un evento más de la
   secuencia de la clase: un retiro posterior a una preferencia vigente la deja sin efecto y una preferencia posterior
   válida vuelve a establecerla. El estado de la interpretación sigue gobernando: una fila `rejected`, `needs_review`,
   `failed` o `no_preference` no retira, no reactiva y no deshace un retiro. Retirar una clase **no** borra las
   preferencias históricas, que se conservan como tal. Si una misma interpretación retira y establece la misma clase,
   prevalece el retiro.

### 2.8.1 `commercial_preference` — preferencia estructurada por producto

Una fila por preferencia extraída. No es texto libre ni una puntuación: es la estructura que el motor determinista
puede aplicar sin volver a llamar a la IA.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid PK | |
| `organization_id`, `interpretation_id`, `observation_id`, `trade_item_id` | uuid | FKs compuestas; la preferencia **hereda** artículo y organización de su interpretación y observación |
| `subject_kind` | varchar(16) | `laboratory` \| `brand` \| `supplier` \| `availability` \| `price` |
| `subject_key` | varchar(300) | Clave normalizada para comparar; en disponibilidad es `in_stock` |
| `subject_label` | varchar(300) | Etiqueta legible de lo interpretado |
| `subject_supplier_id` | uuid NULL | Obligatorio **si y solo si** `subject_kind = 'supplier'` |
| `effect` | varchar(12) | `prefer` \| `deprioritize` |
| `tolerance_kind` | varchar(24) | `none` \| `relative_percent` \| `absolute_amount` |
| `tolerance_percent`, `tolerance_amount`, `tolerance_currency` | numeric / char(3) NULL | Coherentes con `tolerance_kind`; moneda ISO de 3 letras |
| `tolerance_origin` | varchar(24) | `not_applicable` \| `implicit_default` \| `explicit` |
| `price_reference_kind` | varchar(16) | `none` \| `acceptable` \| `maximum` |
| `price_reference_amount`, `price_reference_currency`, `price_reference_basis` | numeric / char(3) / varchar(24) NULL | Base: `per_base_unit` \| `per_commercial_unit` \| `per_line_total` |
| `price_reference_quantity`, `price_reference_quantity_unit` | numeric / varchar(10) NULL | Obligatorias solo para `per_line_total`; unidad del dominio |
| `confidence` | numeric(3,2) | 0–1; sugerencia, no verdad |
| `status` | varchar(16) | `suggested` \| `confirmed` \| `rejected` \| `needs_review` |
| `data_origin` | varchar(12) | `ai_derived` |
| `created_at` | timestamptz | |

**Coherencia que impone la base.**

1. `supplier` exige `subject_supplier_id`; los demás sujetos lo prohíben.
2. Solo `prefer` admite tolerancia; una preferencia negativa se expresa como `deprioritize` sin tolerancia.
3. `relative_percent` exige porcentaje > 0 y ≤ 100, sin monto ni moneda; `absolute_amount` exige monto > 0 y moneda,
   sin porcentaje. Ambos exigen origen `explicit` o, solo el porcentaje implícito, `implicit_default`.
4. `price` solo usa `prefer`, exige una referencia `acceptable` o `maximum` y no lleva tolerancia.
5. `per_line_total` exige cantidad y unidad; las bases unitarias las prohíben. Una referencia de precio no se mezcla
   con un sujeto que no sea `price`.
6. `availability` solo admite `in_stock` y `prefer`.
7. La preferencia es inmutable en contenido; solo cambia `status` y `DELETE` está prohibido.
8. **El retiro no se guarda como preferencia.** Borrar o no emitir filas no retira una preferencia vigente: la última
   interpretación aplicada de la clase sigue gobernando. El retiro es una clase de la **interpretación**
   (`retired_subject_kinds`, D-144), no una fila ficticia ni un `status='rejected'`; así no se inventan sujetos ni se
   copia el historial.

**Formas comerciales que la estructura debe representar** (el prompt las interpreta; el backend valida y nunca deja
texto libre para ejecutar):

| Forma | Ejemplo | Representación |
|---|---|---|
| Laboratorio | «Prefiero Laboratorio Chile» | `laboratory` + `prefer`, tolerancia `none` |
| Tolerancia implícita | «…aunque sea un poco más caro» | `relative_percent` 10, `implicit_default` |
| Porcentaje explícito | «…hasta un 15 % más» | `relative_percent` 15, `explicit` |
| Monto adicional | «Pagaría hasta $500 más» | `absolute_amount` 500 CLP, `explicit` |
| Precio aceptable | «Si vale $9.990 me parece bien» | `price` + `acceptable`, base explícita |
| Precio máximo | «No quiero pagar más de $9.990» | `price` + `maximum`, base explícita |
| Preferencia negativa | «No me gusta este proveedor para este producto» | `supplier` + `deprioritize`, sin exclusión |
| Disponibilidad | «Prefiero proveedores que tengan stock» | `availability` + `in_stock` |

**Reglas monetarias.** Se usa `Decimal`/NUMERIC, nunca `float`. Se respeta la moneda y la base de precio; no se
comparan precio unitario con precio de caja ni con total de línea. Si la expresión monetaria es ambigua y el contexto
del producto/oferta no la resuelve con seguridad, se guarda `needs_review` y **no se aplica** una interpretación
inventada.

### 2.8.2 `preference_order_trace` — traza interna del orden

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid PK | |
| `organization_id`, `trade_item_id` | uuid | Artículo exacto |
| `context` | varchar(20) | `article_detail` \| `comparison` |
| `interpretation_ids`, `preference_ids` | jsonb | Interpretaciones **consultadas** para el artículo (incluye las no aplicadas, excepto `failed`) y preferencias vigentes |
| `applied_preferences` | jsonb | Solo preferencias aplicadas: estructura, `interpretation_status`, `preference_status`, `observation_id`, `model` y `prompt_version` congelados al aplicar; sin texto libre ejecutable |
| `retired_subject_kinds` | jsonb | Clases **retiradas vigentes** en el momento de aplicar el orden (D-144); vacío si no hay retiro |
| `offer_ids_before`, `offer_ids_after` | jsonb | Orden anterior y resultante |
| `offer_set_hash`, `preference_fingerprint` | varchar(64) | Identifican el estado; únicos por organización, artículo y contexto |
| `correlation_id` | uuid | Correlación con la petición |
| `created_at` | timestamptz | |

Es **interna y de solo agregar** (trigger rechaza `UPDATE` y `DELETE`). No se devuelve en la API pública ni se muestra
en las tarjetas. Su único fin es la auditoría AC31/D-141. Repetir la misma aplicación (mismo conjunto de ofertas y mismo
fingerprint de preferencias aplicadas **y de clases retiradas**) no duplica la fila. También se escribe cuando el único
efecto es un retiro: `preference_ids` y `applied_preferences` quedan vacíos y `retired_subject_kinds` registra la clase.
En `article_detail` la escritura es *best effort*: si falla,
se revierte solo la traza, se registra el incidente internamente y la consulta sigue; en `comparison` es obligatoria y
su fallo revierte la corrida completa (D-143).

### 2.9 `web_access_request` — solicitud de acceso web

Nombre deliberadamente distinto de `telegram_access_request` (D-118) para no confundir dos flujos.

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid PK | |
| `organization_id` | uuid | **Fijada por configuración del servidor**, nunca por el cuerpo |
| `auth_subject` | varchar | `sub` del JWT de Supabase; único por organización entre solicitudes `pending` |
| `email`, `display_name` | varchar/text | Solo desde claims verificados |
| `status` | varchar(10) | `pending` \| `approved` \| `rejected` |
| `requested_at`, `decided_at`, `decided_by` | | `decided_by` es un `admin` de la organización |
| `decision_note` | text NULL | Motivo opcional del rechazo |
| `data_origin` | varchar(12) | |
| `created_at`, `updated_at` | | |

`UNIQUE (organization_id, auth_subject) WHERE status='pending'`: una solicitud pendiente por persona y organización;
las rechazadas se conservan y permiten una nueva solicitud posterior. **La solicitud no otorga nada**: solo la
aprobación habilita. La aprobación **crea la persona con el perfil `admin`** (D-130): `roles = ['admin']`, `active` y
`auth_subject` en el mismo paso. No otorga `is_superadmin`, no crea cuenta ni vínculo de Telegram (§9.3, paso 8;
§9.10) y nunca enlaza ni sobrescribe una fila existente.

### 2.10 Columnas nuevas en `app_user`

| Columna | Tipo | Notas |
|---|---|---|
| `email` | varchar NULL | Desde el claim verificado; no se usa como identidad |
| `is_superadmin` | boolean, def. `false` | **No es un rol**; `roles` conserva su CHECK cerrado |
| `web_access_expires_at` | timestamptz NULL | NULL = sin vencimiento; se evalúa en cada petición |
| `web_access_granted_by`, `web_access_granted_at` | | Trazabilidad del último otorgamiento |

`roles` sigue siendo `ARRAY['auxiliary','purchasing','approver','admin']::text[]` con su CHECK. **No se agrega
`superadmin` a `roles`**: sería ambiguo con el modelo de permisos existente (D-119).

**Sin columnas adicionales (D-130).** Las revisiones 6 y 7 **no** agregan una bandera de reportante por persona:
`app_user` conserva exactamente estas cinco columnas. Aprobar una solicitud escribe `roles = ['admin']`; el enlace de
identidad y la asignación de roles de personas preexistentes siguen siendo operaciones separadas y auditadas, con los
comandos y endpoints ya existentes. La independencia con Telegram se resuelve por autorización de cuenta y no por una
columna nueva (§9.10).

---

## 3. Precio seleccionado, precio verificado y vigencia

La lectura de una línea devuelve tres cosas distintas, nunca fusionadas:

```json
{
  "line_id": "uuid",
  "quantity": { "value": "20", "unit": "box" },
  "selected": {
    "offer_snapshot_id": "uuid",
    "price": { "amount": "3490.00", "currency": "CLP" },
    "observed_at": "2026-10-20T12:00:00Z",
    "review_status": "accepted"
  },
  "current": {
    "offer_snapshot_id": "uuid",
    "price": { "amount": "3720.00", "currency": "CLP" },
    "observed_at": "2026-10-29T09:00:00Z"
  },
  "price_state": "requires_review",
  "causes": ["newer_observation_available"]
}
```

| `price_state` | Significado |
|---|---|
| `current` | El snapshot seleccionado es el más reciente aceptado |
| `requires_review` | Existe una observación posterior aceptada, o el snapshot seleccionado es `needs_review` |
| `stale` | `valid_to` vencido |
| `no_longer_offered` | No hay observación reciente del SKU |

Reglas:

1. **Nunca se reapunta sola.** Ni por una importación nueva, ni por pasar el tiempo, ni por abrir la cotización.
2. `current` es informativo: no reemplaza el precio de la línea ni entra en la evaluación hasta que Pao lo acepte.
3. Aceptar el precio nuevo es una **operación explícita** (`POST /quotes/{id}/lines/{line_id}/refresh`), auditada, que
   reapunta la línea a `current.offer_snapshot_id`.
4. Un precio histórico **jamás** se presenta como vigente; la antigüedad (`observed_at`) se muestra siempre.
5. El registro de compra (§6) copia los valores efectivamente registrados: ahí la evidencia es completa e inmutable.
6. **Los precios públicos de cadenas no participan de esta sección**: no son `selected`, ni `current`, ni entran en
   E5. Se muestran aparte, con su propia fecha y fuente (§11).

---

## 4. Estados y transiciones

### 4.1 Almacenados

```
quote        draft ──cerrar──► closed ──reabrir──► draft
quote_cart   active ──descartar──► dismissed ──reincorporar──► active
quote_line   active ──quitar──► removed                  (nunca se borra; solo sin compras)
             pendiente ──cerrar pendiente──► cerrado
             cerrado ──reabrir pendiente──► pendiente    (no cambia `status`)
purchase     (sin estado; existe porque ocurrió; encabezado inmutable)
             archive: none ──► pending ──► verified | failed
                                  ▲              │
                                  └── reintento ─┘ (solo desde `failed`; `verified` es terminal)

commercial_observation       (sin estado; inmutable)
observation_interpretation   suggested ──confirmar──► confirmed
                             suggested ──rechazar──► rejected   (sin retorno)
web_access_request           pending ──aprobar──► approved
                             pending ──rechazar──► rejected
demand_item                  active ──compra confirmada que cubre lo pedido──► fulfilled
                             (terminal: no se reabre; un aviso posterior crea otro ciclo)
```

El paso `active → fulfilled` de `demand_item` es la única transición nueva de v0.2 y la ejecuta el **registro o la
corrección de una compra** en la misma transacción (§6.1, §12.3); no existe una operación manual de «marcar cumplido».
`cancelled` ya existe en el modelo y no cambia.

El «cerrado» del pendiente **no es un estado almacenado**: es `pending_closed_at` en la línea. Por eso cerrar el
pendiente no altera la cantidad cotizada ni la compra histórica.

**Toda transición tiene su operación** (§5, §6, §8, §9): no quedan estados alcanzables solo por efectos laterales.

### 4.2 Derivados (se calculan, no se guardan)

| Objeto | Derivado de | Valores |
|---|---|---|
| Carrito | líneas activas + compras vigentes | `empty` \| `pending` \| `partially_purchased` \| `purchased` |
| Línea | dos dimensiones independientes: cobertura comprada y estado del resto | `purchase_state`: `pending` \| `partially_purchased` \| `purchased` \| `exceeded` · `remainder`: `open` \| `closed_by_user` \| `closed_by_quote` |
| Cotización | carritos + compras vigentes | `in_preparation` \| `partially_purchased` \| `fully_purchased` \| `closed_without_purchase` |
| Compra | existe o no un registro que la sustituya | `current` \| `superseded` (más `is_annulment`) |
| Faltante cubierto | líneas activas de cotizaciones `draft` **más compras vigentes del faltante**, sin doble conteo | `covered` \| `pending_quantity` \| `pending_purchase_quantity` \| `is_pending` (§12.3) |
| Estado de reposición de un faltante | cobertura, compras y `demand_item.status` | `pending` \| `in_quote` \| `purchased` \| `fulfilled` \| `cancelled` (§12.6) |
| Orden de ofertas | observaciones e interpretaciones **del artículo exacto** + preferencias aplicables + orden determinista de E5 | sin campo visible; el orden no se explica ni se puntúa (§8.4) |

- `exceeded`: se compró más de lo cotizado. **No se bloquea**; se advierte y se conservan ambas cantidades (§6.4).
- **Compra todo o nada (enmienda a D-127, 2026-11-02):** el flujo de compra de la demo no produce
  `partially_purchased`. Una línea queda `pending`, `purchased` o `exceeded`; un carrito o una cotización pueden
  quedar sin algunos productos **porque no se compraron**, nunca por haber dividido una compra por productos o
  cantidades. Los valores siguen definidos en el modelo y no se renombran.
- Una línea puede quedar `pending` para siempre. **No se asume que lo cotizado se compra.**
- La segunda dimensión (`remainder`) no es un `purchase_state`: `closed_by_user` = Pao desistió de lo que falta
  (§5.3); `closed_by_quote` = la cotización se cerró con productos no comprados (§6.5). Así una línea comprada a
  medias sigue diciendo cuánto se compró **y** por qué el resto ya no se espera.
- Cerrar una cotización **no** borra ni oculta líneas no compradas: quedan como cotizadas no compradas (§6.5).
- **Solo cuentan las compras `current` y no anuladas.** Un registro sustituido o una anulación nunca se suman a la
  cantidad comprada, ni a los totales, ni al `exceeded`.
- El **pendiente** de una línea es `quantity − comprado vigente`. Si Pao desiste de comprarlo, la cantidad cotizada
  queda intacta y la compra histórica no se toca: solo se registra `pending_closed_at` (§5.3).
- **Un carrito `dismissed` queda fuera de todos los derivados de dinero y de cobertura** (F-3). No se evalúa, no suma
  al total estimado y no cubre faltantes; sus líneas siguen almacenadas y se reincorporan explícitamente.
- **Cierre del faltante:** `fulfilled` solo lo produce una **compra confirmada** (`purchase_line` de compras `current`
  no anuladas) que cubra la cantidad pedida conocida. Cotizar cubre de forma provisional y **nunca** cierra (V-39,
  D-127).

---

## 5. E10 — Cotizaciones y carritos

Todas las operaciones: **rol `admin` + `StrongActor`**, conforme a C0 §6 y C0-09 (D-079/D-086, con la corrección de
**D-130**). Cualquier otro rol recibe `403 FORBIDDEN`; en una organización real con la cabecera de demostración,
`403 STRONG_AUTH_REQUIRED`. La organización sale del `Actor`, nunca del cuerpo. **No se amplían permisos a `auxiliary`
ni a `approver` en esta etapa**, y no existe un segundo perfil funcional web. Importes siempre como string + moneda
(C0 §2.2).

**Qué exige `StrongActor` (D-086/D-099, confirmado el 2026-10-30).** Se verifica, en este orden: (a) JWT de Supabase
válido —firma, `iss`, `aud`, `exp`, `sub` y `role=authenticated`— y persona `app_user.active` enlazada por
`auth_subject`; (b) rol **`admin`**; (c) organización tomada del `Actor`, nunca del cuerpo; (d)
`StrongActor` con su **semántica vigente**: rechaza la cabecera de demostración fuera de `simulation` y **no es MFA ni
una segunda autenticación**. La autenticación fuerte real (MFA o re-autenticación) queda para **H5**, como decisión
futura: G1 y el Cotizador no la implementan y no modifican ningún control de seguridad existente.

### 5.1 Cotizaciones

| Método y ruta | Para qué |
|---|---|
| `POST /api/v1/quotes` | Crear cotización. Cuerpo: `{ "title": "Reposición semanal" }` |
| `GET /api/v1/quotes` | «Mis cotizaciones». Filtros: `status`, `updated_from`, `updated_to`, `q` (título), paginación |
| `GET /api/v1/quotes/{quote_id}` | Detalle completo: carritos, líneas, evaluación E5 por carrito, total estimado y cobertura de faltantes. Las ofertas llegan **ordenadas** por las preferencias del producto; no incluye `advice` ni explicación del orden |
| `GET /api/v1/quotes/{quote_id}/summary` | Sección integrada del cotizador: por proveedor, nº de productos, subtotal o causa, estado. Sin detalle de líneas |
| `PATCH /api/v1/quotes/{quote_id}` | `title`, `status: "closed"` o `"draft"` (reabrir). Requiere `version`. Cerrar exige asignaciones consistentes (`409 QUOTE_HAS_INCONSISTENT_ALLOCATIONS`, D-137); reabrir no cambia asignaciones |

`GET /quotes/{id}/summary` responde en una sola llamada, pensado para refrescarse mientras Pao sigue buscando.

### 5.2 Agregar producto (sin `cart_id`)

`POST /api/v1/quotes/{quote_id}/lines`

```json
{
  "supplier_id": "uuid",
  "offer_snapshot_id": "uuid",
  "quantity": { "value": "20", "unit": "box" },
  "demand_item_id": "uuid | null",
  "demand_allocations": [
    { "demand_item_id": "uuid", "quantity": 12 },
    { "demand_item_id": "uuid", "quantity": 8 }
  ]
}
```

`demand_item_id` (atajo de **una** asignación por toda la cantidad) y `demand_allocations` son **excluyentes**
(`422 ALLOCATION_CONFLICT`). Con `demand_allocations` cada reporte recibe exactamente la cantidad declarada; la suma
no puede superar la cantidad de la línea (`409 ALLOCATION_EXCEEDS_LINE`), un reporte no puede repetirse
(`422 DUPLICATE_ALLOCATION`), la lista vacía se rechaza **en el alta** (`422 ALLOCATION_EMPTY`; en `PATCH`, la lista
vacía quita todas las asignaciones, §5.3) y la unidad del reporte debe coincidir
con la de la línea (`422 UNIT_MISMATCH`). **Nunca se reparte ni se suma solo:** si una línea cubre a dos personas, la
cantidad de cada una se declara.

- Crea el carrito del proveedor si no existe. Si existe y está `dismissed`, responde 409 `CART_DISMISSED`
  (hay que reincorporarlo explícitamente: no se resucita por accidente).
- Valida: la oferta pertenece al proveedor (`422 OFFER_NOT_IN_SUPPLIER`), a la organización (404), la cotización está
  `draft` (`409 QUOTE_CLOSED`), la cantidad es entera > 0 (`422 INVALID_QUANTITY`).
- **Si viene `demand_item_id` o `demand_allocations`:** valida que cada reporte pertenezca a la organización, esté
  **`active`** y tenga **cantidad pedida conocida** (`422 REPORT_QUANTITY_UNKNOWN`; un reporte sin cantidad no se puede
  asignar hasta que una persona la fije); calcula `relation` con la misma clasificación de E1 (§12) y la guarda
  **congelada por asignación**. Si no hay `relation` calculable (por ejemplo, una ficha de cadena sin artículo), guarda
  `needs_review`. No se pide una segunda confirmación: «Agregar a cotización» desde una fila de faltante ya es la
  acción explícita. La clasificación y las advertencias quedan visibles en la línea y en el resumen.
- **Reportes independientes (D-135):** dos personas pueden tener cada una su propio faltante del mismo producto. La
  línea puede cubrir a ambos solo con asignaciones explícitas; no se consolidan ni se reparten de forma automática.
  La cobertura de cada reporte es la suma de **sus** asignaciones (§12.3).
- **Si no viene ninguno de los dos:** la línea no cubre ningún faltante, aunque el producto coincida. Una búsqueda
  libre no vincula nada.
- Respuesta `201` con la línea y el carrito creado o reutilizado.
- **Idempotencia de repetir la misma petición (F-5):** con `Idempotency-Key`, se aplica la semántica de E8. Sin ella,
  una petición idéntica repetida (misma cotización, oferta y cantidad) devuelve `200` con la línea existente y
  `deduplicated: true`; **nunca suma cantidades**. Si la misma oferta se envía con **otra cantidad**, responde
  `409 LINE_EXISTS` con `details.current_quantity` y la instrucción de usar `PATCH`; no se crea otra línea ni se suma.
- **Faltante ya comprado:** agregar una línea a un faltante que ya tiene compra vigente **no se bloquea** —puede ser
  una necesidad adicional—, pero la respuesta advierte `demand_already_purchased` con las cantidades ya compradas. La
  línea nueva **no** duplica ni modifica la compra existente; la cobertura se recalcula (§12.3).

### 5.3 Cantidad, quitar y mover

| Método y ruta | Regla |
|---|---|
| `PATCH /quotes/{quote_id}/lines/{line_id}` | SET explícito de cantidad + `version`. No hay suma implícita: repetir el aviso no acumula. Si ya hay compras vigentes, la nueva cantidad no puede ser menor que lo comprado → `409 QUANTITY_BELOW_PURCHASED`. Acepta `demand_allocations` y **reemplaza** las asignaciones vigentes (auditado); la suma no puede superar la nueva cantidad de la línea. Una lista vacía **quita todas las asignaciones** (D-137); es la acción explícita para corregir una línea antes de cerrar |
| `DELETE /quotes/{quote_id}/lines/{line_id}` | Quita lógico (`removed`) con `removed_by`/`removed_at`. Solo si **no hay compras vigentes** → si las hay, `409 LINE_HAS_PURCHASES`; lo pendiente se cierra con `close-pending` |
| `POST /quotes/{quote_id}/lines/{line_id}/close-pending` | **Desistir del pendiente**: marca `pending_closed_at`. No cambia `quantity`, no toca la compra histórica y no borra la línea. La cantidad cotizada original queda visible |
| `POST /quotes/{quote_id}/lines/{line_id}/reopen-pending` | Deshace el cierre del pendiente mientras la cotización esté `draft` |
| `POST /quotes/{quote_id}/lines/{line_id}/move` | `{ "target_offer_snapshot_id": "uuid", "quantity": {...} }`. Exige oferta destino **explícita**, del **mismo artículo** (`422 ARTICLE_MISMATCH` si no) y solo si **no hay compras vigentes** (`409 LINE_HAS_PURCHASES`). Atómico: quita + agrega en una transacción y un solo evento de auditoría con antes/después. Crea el carrito destino si hace falta. **No arrastra `demand_item_id`, `demand_allocations` ni `coverage_relation`**: la nueva línea nace sin vínculo salvo que la petición lo declare |
| `POST /quotes/{quote_id}/lines/{line_id}/refresh` | Acepta el precio actual; reapunta al snapshot nuevo. Auditado. **No toca compras**; si la línea está comprada por completo, responde con advertencia de que no tiene efecto sobre lo pendiente |

**Repartir cantidades**: no es una operación nueva. Pao agrega la misma referencia en el carrito de otro proveedor
(creando otra línea). El resumen muestra, por artículo, la cantidad distribuida y la pendiente.

**Quitar una línea con vínculo a faltante** recalcula la cobertura (§12.3): si con las líneas restantes ya no se
alcanza la cantidad pedida, el saldo vuelve a `pending_quantity`. La cotización es cobertura provisional: quitarla no
cierra ni reabre el ciclo, que solo se cierra con compra confirmada (D-127). No se borra ninguna observación ni el
historial original de Telegram.

**Cancelar un reporte** (`POST /api/v1/demand-items/{item_id}/cancel`, §12.2) **no modifica las líneas**: la respuesta
informa `affected_quote_lines` (con `quote_status`) para que la administración corrija las asignaciones antes de cerrar
la cotización o registrar la compra. La cobertura del reporte cancelado deja de contar; las demás asignaciones de la
línea siguen intactas. **La cotización afectada no se puede cerrar mientras conserve esa asignación** (D-137): se quita
la asignación con `PATCH demand_allocations: []` o se ajusta a los reportes que siguen activos. Nada se corrige solo.

### 5.4 Carritos: descartar y reincorporar

| Método y ruta | Efecto |
|---|---|
| `POST /quotes/{quote_id}/carts/{supplier_id}/dismiss` | `active → dismissed`. Oculta el carrito del panel; **no** borra líneas. El carrito deja de evaluarse, de sumar al total y de cubrir faltantes (F-3) |
| `POST /quotes/{quote_id}/carts/{supplier_id}/reinstate` | `dismissed → active`. Devuelve el carrito tal como estaba y recalcula su cobertura |

Se identifican por proveedor, no por identificador de carrito.

### 5.5 Evaluación por carrito

Cada carrito **`active`** con líneas activas se evalúa con **E5** (`POST /comparisons`, invocado internamente). El
contrato de E5 no cambia; solo cambia el patrón de invocación: **una comparación por carrito**, ejecutadas en
paralelo **en proceso** (sin llamadas externas). Motivo: mínimo, múltiplo, impuestos, descuentos y despacho son
**por proveedor**, y el reparto entre proveedores exige cantidades distintas para el mismo artículo.

Consecuencia declarada: la cotización general **no** tiene una única llamada a E5. Los carritos `dismissed` **no se
evalúan** (F-3).

La respuesta por carrito incluye, sin recalcular nada: `completeness`, `subtotal`, `total` (solo si `complete`),
`lower_bound`, `blockers`, `causes`, `freshness`, `computed_at`. Todo tal como lo devuelve E5.

### 5.6 Total general estimado

Objeto nuevo, **fuera de E5**, con reglas estrictas:

```json
{
  "estimated_total": {
    "available": true,
    "kind": "complete_estimate",
    "amount": "184320.00",
    "currency": "CLP",
    "suppliers_included": ["uuid", "uuid"],
    "computed_at": "2026-10-30T15:00:00Z"
  }
}
```

```json
{
  "estimated_total": {
    "available": false,
    "kind": null,
    "amount": null,
    "currency": "CLP",
    "causes": ["cart_incomplete:supplier_x", "shipping_unknown:supplier_y", "cart_empty:supplier_z"]
  }
}
```

Se muestra **solo** si se cumplen todas:

1. Cada carrito **`active`** con líneas activas está `complete` en E5 (sin `blockers`), y
2. hay una sola moneda entre líneas, cargos y despachos, y
3. no hay ninguna causa `unknown` pendiente en los carritos activos.

Si falta cualquiera → `available: false` con causas visibles. **Nunca se muestra una cota inferior como costo total
general**, y no se suma la cota de un carrito a la de otro. Las cotas por proveedor siguen disponibles por separado,
etiquetadas como «cota inferior del proveedor», y **no** se suman entre sí.

El total estimado **no** sirve para comparar proveedores, no decide nada y no se guarda como cifra de compra.

### 5.7 Persistencia y recuperación (F-5)

Garantías exigibles, no supuestos:

1. **Sin TTL y sin borrado por inactividad.** Una cotización `draft` o `closed` permanece indefinidamente.
2. **Sobrevive a recargar la página, cerrar el navegador, cerrar sesión, reiniciar el backend y pasar días.** No vive
   en memoria del proceso ni en el navegador.
3. **Varias cotizaciones independientes** por persona y organización; ninguna se pisa con otra.
4. **El carrito nace con la primera línea** y se recupera por proveedor; no hay identificador técnico que Pao deba
   conservar.
5. **Repetir una petición no duplica ni suma** (§5.2). Las cantidades solo cambian por un `SET` explícito.
6. **La lista «Mis cotizaciones»** ordena por `updated_at` y muestra título, fecha, proveedores y estado; una
   cotización sin título se muestra por su fecha, no se oculta.

---

## 6. E11 — Compras registradas

Permisos: **rol `admin` + `StrongActor`** (§5, D-130), sin excepciones para auxiliares. Registrar una
compra sigue sin ejecutar nada: `purchase_execution` permanece `blocked`.

### 6.1 Registrar

`POST /api/v1/quotes/{quote_id}/purchases` — obliga `Idempotency-Key` (8–128 caracteres), mismo patrón que E8:
misma clave + mismo cuerpo → se devuelve el registro original; misma clave + cuerpo distinto → `409
IDEMPOTENCY_KEY_REUSED`.

```json
{
  "supplier_id": "uuid",
  "occurred_at": "2026-10-30T13:00:00Z",
  "currency": "CLP",
  "notes": "Compra en mostrador",
  "lines": [
    {
      "quote_line_id": "uuid",
      "trade_item_id": "uuid",
      "external_id": "SKU-123",
      "description_as_recorded": "Paracetamol 500 mg x 20",
      "presentation_as_recorded": "caja x 20 comprimidos",
      "laboratory_as_recorded": "Laboratorio Chile",
      "quantity": { "value": "20", "unit": "box" },
      "unit_price": { "amount": "3490.00", "currency": "CLP" },
      "taxes": [{ "kind": "iva", "rate_percent": "19", "included_in_price": false }],
      "discounts": [],
      "shipping_share": null,
      "notes": null
    }
  ]
}
```

- Cada compra corresponde a **un proveedor y un momento**. Pao puede comprar a X hoy y a Y mañana en la misma
  cotización; ninguna de las dos cierra la cotización.
- `occurred_at` es declarado y se guarda tal cual. `registered_at` lo pone el sistema.
- Los totales se calculan con el mismo motor determinista de E5 cuando los datos alcanzan; si no, `completeness =
  partial` con `blockers` y `total: null`. No se inventan importes.
- Una compra puede incluir líneas **no cotizadas** (`quote_line_id: null`); se registran igual, con su procedencia.
- Los precios de la compra son los **registrados por Pao**, no se re-leen de la oferta. No hay conciliación automática.
- **No se puede registrar una compra sobre una cotización `closed`** sin reabrirla: `409 QUOTE_CLOSED`. Reabrir es
  explícito y auditado.
- **Cierre del faltante en la misma transacción (D-127, todo o nada).** La compra se registra **completa**: cada línea
  lleva la cantidad pendiente completa del faltante y la selección entera en una sola operación; no se acepta una
  cantidad menor ni una compra dividida por productos o cantidades. Si las líneas registradas llevan `demand_item_id` y
  la suma de compras confirmadas de ese faltante alcanza su cantidad pedida conocida, la compra lo pasa a `fulfilled`
  y el producto queda **liberado** para un nuevo reporte (§12.3). Si no la alcanza, el faltante no se cierra ni se
  libera. La respuesta incluye el faltante afectado y su `replenishment_state` resultante.
- **Asignación por reporte (D-135).** Cuando C5-B se implemente (`0020`), cada línea de compra asignará explícitamente
  a uno o varios reportes con `purchase_line_allocation`, igual que la cotización. Una línea con compra vigente usa esa
  asignación y **nunca** suma su asignación de cotización (§12.3). No hay reparto automático ni consolidación entre
  reportes de distintas personas.

### 6.2 Correcciones y anulaciones

- El registro original **nunca se modifica ni se borra**.
- `POST /api/v1/purchases/{purchase_id}/corrections` crea un registro nuevo con `supersedes_purchase_id` y
  `correction_reason` obligatoria.
- **Una sola sustitución por registro**: `UNIQUE (supersedes_purchase_id)` con índice parcial. Dos correcciones
  simultáneas **no pueden** dejar dos vigentes; la segunda recibe `409 PURCHASE_ALREADY_SUPERSEDED` con el registro
  vigente en `details`. La garantía es de la base, no del código.
- Derivado `current`: un registro es vigente si **ninguno** lo sustituye. Se calcula por anti-join; no hay columna
  que actualizar y que pueda contradecirse.
- **Historial y reportes suman solo registros `current` y no anulados.** Un registro sustituido se muestra marcado y
  con enlace al vigente; sus importes no se cuentan dos veces.
- **Corregir una compra archivada está permitido.** La API sirve el bundle verificado para precargar el formulario y
  registra la corrección con normalidad; se audita como corrección sobre registro archivado. La cadena se conserva
  completa aunque el detalle ya esté fuera de la base operativa.
- **Anulación** (`POST /api/v1/purchases/{purchase_id}/annulment`): registro nuevo con `is_annulment = true`, **sin
  líneas**, `supersedes_purchase_id` y `annulment_reason` obligatorios. Excluye el original de los totales y del
  historial de importes, pero **no borra la evidencia**: el registro anulado sigue visible, marcado y enlazado a su
  anulación.
- Una compra anulada no se corrige directamente: se corrige su anulación (que es un registro más) o se registra una
  compra nueva. Nunca se «desanula» editando.
- **Anular o corregir recalcula la cobertura del faltante.** Una anulación deja de cubrir; una corrección sustituye y
  nunca suma las dos. Si el saldo vuelve a quedar sin compra confirmada, se muestra como pendiente de compra
  (§12.3, §12.4); el ciclo solo se cierra con una compra confirmada que cubra lo pedido (D-127). No hay bloqueo por
  recepción: el MVP no registra llegadas (D-126).

### 6.3 Varias ofertas del mismo artículo

Dos líneas del mismo artículo en el mismo carrito con ofertas distintas son válidas y **no se fusionan**. La respuesta
del carrito las marca con `duplicate_article: true` para que se vea la duplicación sin impedirla.

### 6.4 Cantidades compradas mayores que las cotizadas

Se permiten. La línea queda `exceeded`, con `quantity_quoted`, `quantity_purchased` y `excess` visibles, y una
`warning` en la respuesta. No se recorta ni se corrige sola. Lo mismo si la suma de compras vigentes de una línea
supera lo cotizado.

### 6.5 Cerrar con productos no comprados

`PATCH /quotes/{id}` con `status: "closed"` cierra la cotización aunque queden líneas `pending`. Esas líneas se
conservan intactas —misma `quantity` cotizada, mismo `purchase_state`— y su `remainder` pasa a `closed_by_quote`:
se muestran como **cotizadas no compradas**, visibles en el detalle y en el historial. Cerrar no borra nada y es
reversible mientras no haya compras archivadas; al reabrir, cada línea vuelve a lo que tenía: `open`, o
`closed_by_user` si Pao ya había desistido de esa línea (§5.3). La precedencia es `pending_closed_at` antes que
el cierre de la cotización.

**Efecto sobre faltantes (P-8, resuelto en la revisión 4 por D-121).** Las compras **`current` y no anuladas**
también cubren el faltante, sin doble conteo: una línea cotizada y su compra nunca se suman dos veces. Por tanto,
cerrar una cotización con líneas compradas **no devuelve el faltante a pendiente**; la cobertura se recalcula sobre
las compras vigentes (§12.3). Si la compra se anula y no queda ninguna otra cobertura, el saldo vuelve a mostrarse
como pendiente de compra; si la compra se corrige, solo cuenta el registro vigente. Una compra vigente cubre el faltante mientras nadie la anule
o corrija: el estado lo muestra como **comprado** y no afirma que llegó (§12.6). El cierre del ciclo exige que las
**compras confirmadas** cubran toda la cantidad pedida conocida, y como la compra es **todo o nada** (enmienda a
D-127), al registrarla el faltante se cierra y se libera, aunque la cotización siga abierta o se cierre después.

### 6.6 Consultas

| Método y ruta | Para qué |
|---|---|
| `GET /api/v1/quotes/{quote_id}/purchases` | Compras vigentes y sustituidas de la cotización, con su estado |
| `GET /api/v1/purchases/{purchase_id}` | Detalle inmutable completo |

Las consultas exigen rol `admin` + `StrongActor` y quedan acotadas a la organización del `Actor`. **Nunca** se listan
compras de otra organización, ni siquiera para la superadministradora (§9.7). **No hay consultas de recepción:** esa
funcionalidad está fuera del MVP (D-126).

### 6.7 Recepción física — fuera del alcance del MVP (D-126)

El MVP **no registra la recepción física** de una compra. No hay cantidades entregadas, recepciones parciales,
diferencias, productos equivocados, saldos pendientes de recepción ni cierre condicionado a la llegada.

- **Confirmar una compra marca el término del proceso que administra el MVP.** No certifica que la mercadería haya
  llegado; el sistema no afirma ni niega la llegada.
- Una compra confirmada y vigente **cubre** el faltante (§12.3) aunque la cotización se cierre después.
- El faltante pasa a `fulfilled` **solo** cuando las **compras confirmadas** cubren la cantidad pedida conocida
  (§12.3, §12.6); no se espera ninguna confirmación de entrega para cerrarlo. La compra es **todo o nada** (enmienda a
  D-127): al registrarse cubre completa, cierra el faltante y lo libera; no hay saldos nacidos de una compra parcial.
- La compra sigue siendo **inmutable** y su anulación o corrección recalcula la cobertura (§6.2, §12.4).
- El diseño completo de recepción (tablas, endpoints, reglas de diferencias y cierre) queda registrado como
  **funcionalidad futura** en el registro de decisiones (**D-122**, retirada por **D-126**) y en §15; no forma parte de
  este contrato implementable.

## 7. E12 — Historial de cotizaciones

Permisos: **rol `admin` + `StrongActor`** (§5, D-130). Leer historial archivado no cambia el registro ni
reintroduce datos.
El historial conserva la compra, sus líneas y la cadena de correcciones; el MVP no registra recepciones (D-126).

### 7.1 Operaciones

| Método y ruta | Para qué |
|---|---|
| `GET /api/v1/history/quotes` | Búsqueda: `q` (producto o presentación), `supplier_id`, `from`/`to`, `quote_id`, `status`, `has_purchases`, paginación |
| `GET /api/v1/history/quotes/{quote_id}` | Detalle completo; incluye compras en distintos momentos y líneas cotizadas no compradas |
| `GET /api/v1/history/purchases/{purchase_id}` | Detalle de una compra; si su detalle está archivado, se sirve desde el bundle verificado |

- El historial es **página independiente** en la navegación; no vive en el dashboard ni bajo los resultados del
  buscador. Esa decisión de Lovable no la implementa el backend.
- Leer un registro archivado no cambia su contenido ni su estado.
- **Ningún dato desconocido se rellena para completar el historial.** Si no se sabía, se devuelve `null` con
  `unknown` explícito.
- El historial comercial **no** se construye desde `audit_event`: la compra tiene su propio registro inmutable. La
  auditoría se conserva aparte y no reemplaza ni sustituye al historial.

### 7.2 Conservación y archivo

**Regla de negocio:**

1. Las **compras registradas** permanecen completas en la base operativa **6 meses** desde `registered_at`.
2. Después, **solo el detalle de líneas** (`purchase_line`) pasa al almacenamiento externo.
3. **El encabezado `purchase` no se elimina nunca**: sostiene la cadena de correcciones, los totales y los reportes.
   Es pequeño y no crece con las líneas.
4. **Las cotizaciones no se archivan ni se eliminan por esta política.** Una cotización `draft`, o `closed` con
   carritos pendientes, sigue operativa indefinidamente.
5. La unidad de archivo es **la compra**, no la cotización. Archivar el detalle de una compra antigua no toca su
   cotización, sus carritos ni sus líneas: el estado derivado se mantiene con `purchase` + `purchase_index`.
6. Sin copia **verificada, restaurable y consultable** no se elimina nada. No hay borrado por temporizador.
7. El archivo **no reemplaza los respaldos de PostgreSQL**; son mecanismos distintos y complementarios.

**Ciclo de una compra archivada:**

1. Bundle canónico (claves ordenadas) con compra, líneas, cadena de correcciones y
   contexto mínimo de la cotización.
2. Sellar: SHA-256 por registro y del bundle, conteos, ids mínimo/máximo, bytes.
3. Escribir en el almacén externo y **releer desde el almacén**.
4. Verificar: recomputar hashes y comparar registro a registro con el origen.
5. Ensayo de restauración en esquema de prueba y comparación fila a fila.
6. Solo entonces `archive_status = verified` y `archive_restore_ok = true`. El retiro del detalle es una operación
   **explícita y separada** (§7.4) que además exige autorización vigente.

**Recuperación:** desde el historial se localiza por `purchase` (encabezado siempre operativo) y `purchase_index`
(sin abrir objetos), y se sirve el bundle verificado. Un comando de restauración reconstruye el detalle en un esquema
de prueba para comparar; nunca sobrescribe datos operativos. Re-verificación periódica por checksum.

**Pendiente de aprobación:** proveedor de almacenamiento, costos, credenciales y política de bloqueo de retención.
Mientras no se aprueben, **no se archiva ni se purga nada**: las compras siguen completas en la base operativa aunque
superen los 6 meses.

### 7.3 Búsqueda con el detalle fuera de PostgreSQL

- El historial resuelve **producto, presentación, proveedor, fechas e identificador de cotización** con
  `purchase_index` (§2.6), **sin abrir el almacén externo**.
- El bundle se abre **solo al abrir un registro**.
- La búsqueda de texto usa `pg_trgm` + `unaccent` sobre `search_text`. **`pg_trgm` y `unaccent` son DDL**: no pueden
  crearse «sin migración». Requieren su **propia migración** (C5-C, 0021) y la verificación previa de disponibilidad
  en el PostgreSQL de destino, con autorización explícita (F-2).
- Si alguna no estuviera disponible, la alternativa es búsqueda por prefijo con `lower()` —más limitada— y requiere
  tu decisión. No se instala ninguna extensión sin autorización.
- Los filtros por proveedor y fecha usan columnas del índice. Compras operativas y archivadas se resuelven con **la
  misma consulta**, sin ramas distintas.

### 7.4 Purga controlada (diseñada, **no autorizada**)

- Función `SECURITY DEFINER` propiedad de un rol dedicado. El rol de la aplicación **no tiene `GRANT DELETE`** sobre
  `purchase` ni `purchase_line`.
- Trigger: `UPDATE` bloqueado para el rol de la aplicación **siempre** (salvo la marca de
  `purchase_mark_archive`, §2.4); `DELETE` sobre `purchase_line` solo si
  `current_setting('app.archive_purge') = 'on'` **y** la compra padre tiene `archive_status = 'verified'` **y**
  `archive_restore_ok = true` **y** `detail_purged_at IS NULL`; en ese acto se marca el padre.
- `DELETE` sobre `purchase` **no existe**: no hay ruta, no hay permiso, no hay función. La cadena de correcciones y
  las FK no pueden romperse.
- Las columnas de archivo solo se escriben por `purchase_mark_archive(...)` (§2.4), nunca por `UPDATE` directo.
- El comando `history-purge` (una compra por transacción, `--dry-run`, límite y filtro de fecha, solo `admin`,
  auditado) queda **documentado como diseño y deshabilitado**.
- **Bloqueo vigente:** no se ejecuta mientras no exista almacenamiento externo aprobado y no se hayan probado la
  integridad, la restauración y la consulta de los registros archivados. Hasta entonces las compras siguen completas
  en la base operativa aunque superen los 6 meses.

---

## 8. E13 — Observaciones comerciales y aprendizaje (O1–O4)

### 8.1 Principio

**Cualquier `admin`** escribe observaciones libres sobre **un producto concreto** («este medicamento no me gusta por
el laboratorio», «este proveedor despacha tarde», «prefiero Laboratorio Chile», «no me gusta este proveedor para este
producto»). El sistema las **guarda siempre**; la IA interpreta la intención comercial y el backend valida una
**preferencia estructurada**. La observación es de la persona; la interpretación es una sugerencia interna y el
**orden de ofertas** es el único efecto visible (§8.4).

Las observaciones **persisten** y **afectan el orden futuro de las ofertas de ese producto** (§8.4). No se exige
explicar cada orden, ni un formulario técnico, ni un paso adicional de confirmación: botón «Enseñar a la IA», texto
libre, Guardar/Cancelar (D-132). El botón vive en la tarjeta de un producto ya elegido; el backend obtiene y valida el
contexto real por `trade_item_id` y los identificadores de oferta, no solo por el texto que envía el frontend.

No se reemplaza el aprendizaje por un conjunto de reglas presentado como IA. Si no hay proveedor configurado o falla,
**la observación se guarda igual**, el orden se mantiene en el determinista de E5 y se informa un estado recuperable;
no se inventa una preferencia ni se simula una interpretación real.

### 8.2 Por qué no un `note_text` único

Un campo único por línea:

- se sobrescribe y pierde la observación anterior;
- mezcla fechas y autores;
- impide mostrar que una observación posterior **supera** a otra sin borrar la primera;
- contamina la línea con texto que no es un dato comercial.

Por eso cada observación es una fila independiente, inmutable y acumulativa (§2.7), y cada interpretación es otra fila
(§2.8).

### 8.3 Operaciones

| Método y ruta | Para qué |
|---|---|
| `POST /api/v1/commercial-observations` | Registrar observación **sin línea**, desde la tarjeta de un producto ya elegido: `{ "text": "...", "trade_item_id": "uuid", "supplier_id": "uuid?", "offer_snapshot_id": "uuid?", "quote_id": "uuid?" }`. Responde `201` con la observación y un `interpretation_status` (`applied`, `pending`, `failed`, `no_preference`, `needs_review`). **La observación se guarda aunque OpenAI falle** |
| `POST /api/v1/quotes/{quote_id}/lines/{line_id}/observations` | Atajo para registrar la observación ya asociada a una línea de cotización. Cuerpo: `{ "text": "..." }`. Sin categorías ni selectores. Responde `201` con la observación y su estado de interpretación |
| `POST /api/v1/commercial-observations/{id}/interpretations` | **Reintentar** la interpretación de una observación existente. Crea una interpretación nueva; **no duplica la observación**. Solo si no hay una interpretación aplicable vigente o si la anterior falló. Responde `202`/`200` con el estado recuperable |
| `GET /api/v1/quotes/{quote_id}/lines/{line_id}/observations` | Observaciones de la línea, más recientes primero, con sus interpretaciones y estado de revisión |
| `GET /api/v1/commercial-observations?trade_item_id=...` | Historial de observaciones **del artículo exacto**, para la vista de producto |
| `POST /api/v1/observation-interpretations/{id}/review` | Revisión humana: `{ "decision": "confirm" \| "reject", "note": "..." }`. Solo `admin` + `StrongActor`. Transición sin retorno; auditada. **Sigue siendo solo de `admin`** (P-7), que es el único perfil web (D-130) |
| `GET /api/v1/articles/{id}` | Detalle E3 con las ofertas **ordenadas** por las preferencias aplicables del artículo; mismo conjunto, mismos precios, stock y elegibilidad. **Sin** `advice`, explicación ni puntuación |
| `POST /api/v1/comparisons` | Candidatos por producto ordenados por las preferencias aplicables; el conjunto no cambia. No se llama a OpenAI para abrir o comparar |

`/api/v1/observations` **no** es esta ruta: es el registro y la bandeja de observaciones de **faltantes** (D-129).
Las observaciones comerciales viven en `/api/v1/commercial-observations` y las rutas de faltantes no cambian.

La interfaz **no cambia**: botón «Observación», campo de texto libre, Guardar/Cancelar. **Sin** categorías,
selectores, etiquetas predefinidas ni formularios adicionales.

### 8.4 O1–O4

| Etapa | Qué hace | Dónde |
|---|---|---|
| **O1 — Persistencia** | Guardar la observación con autor, fecha, texto original y contexto **del producto específico**; mostrarla siempre. **Implementado** | C5-A, migración 0015 (`commercial_observation`) |
| **O2 — Interpretación** | Enviar el texto a un modelo y guardar una interpretación estructurada **en el contexto del producto**: laboratorio, marca, proveedor, precio, disponibilidad u otro criterio comercial. Es una sugerencia interna ligada al producto, no un consejo visible ni una preferencia global. **Implementado internamente (D-145)**: servicio, adaptador de OpenAI y validación estricta; **faltan los endpoints públicos** | A6, migración **0018** (`observation_interpretation`); `app/services/observation_ai.py` |
| **O3 — Recuperación y precedencia** | Recuperar **solo** las interpretaciones del `trade_item_id` exacto; nunca por nombre, marca o laboratorio parecido. La más reciente de la misma clase para ese producto gana; las anteriores no se borran y quedan en la traza | En vivo, con `commercial_preference` |
| **O4 — Orden interno** | Aplicar las preferencias **de ese producto** para **ordenar** sus ofertas recuperadas; todas siguen visibles. No se devuelve explicación, justificación ni puntuación | En vivo, con `preference_order_trace` |

**Flujo obligatorio:**

1. El administrador registra una observación desde la tarjeta de un producto ya elegido.
2. OpenAI interpreta la intención comercial en el contexto del producto.
3. El backend valida la estructura, el alcance y los datos; **OpenAI no escribe datos comerciales**.
4. Se guarda la interpretación y, si corresponde, una o más `commercial_preference`.
5. Un **motor determinista** aplica las preferencias al orden de las ofertas.
6. Lovable muestra el orden que devuelve la API. No calcula, no reordena y no explica.

No se llama a OpenAI al abrir un artículo ni al comparar. La llamada ocurre al guardar una observación o al
reintentar una interpretación. El orden se aplica en `GET /api/v1/articles/{id}` y en `POST /api/v1/comparisons`
(sobre los candidatos de cada producto). **No** se modifica el ranking global de productos ni los totales de una
cotización.

**Precedencia (O3), exacta:**

1. **Alcance por producto.** Solo cuentan observaciones, interpretaciones y preferencias con el `trade_item_id`
   exacto. Una de laboratorio, marca o proveedor **no se aplica** a otro producto de ese mismo laboratorio, marca o
   proveedor.
2. **Sin perfiles globales.** No hay preferencias de organización, índices de reputación ni puntuaciones acumuladas.
3. **Clase de preferencia.** Para laboratorio, marca, proveedor y disponibilidad, la clase es el `subject_kind`. Para
   precio, la clase distingue `acceptable` de `maximum` (`subject_kind='price'` + `price_reference_kind`).
4. **La más reciente de la clase gana** (`created_at`, desempate por `id`). Las anteriores no se borran; quedan en el
   historial y en la traza.
5. **Solo las interpretaciones aplicadas compiten por su clase.** Una interpretación `rejected`, `needs_review` o
   `no_preference` no se aplica, no reactiva una anterior **ni desactiva la vigente**; la última `suggested`/`confirmed`
   de la clase sigue gobernando. Un intento `failed` no es una interpretación semántica y no desplaza nada. Por eso una
   ambigüedad o un rechazo nunca tienen efecto comercial.
6. **Solo se aplican** interpretaciones y preferencias en estado `suggested` o `confirmed`. `rejected`, `failed`,
   `needs_review` y `no_preference` no se aplican.
7. **Sin preferencia aplicable**, el orden es exactamente el determinista vigente de E5. No se rellena con una
   prioridad inventada.
8. **Retiro explícito (D-144).** Una interpretación `suggested`/`confirmed` puede retirar una clase mediante
   `retired_subject_kinds`. El retiro compite en la misma secuencia temporal que las preferencias de esa clase: el
   evento aplicado más reciente (preferencia o retiro) gana, con desempate por `id`. Retirar no reactiva un historial
   anterior, no afecta otras clases ni otros productos, y una preferencia posterior válida vuelve a establecerla. Una
   interpretación sin filas de la clase **no** la retira; solo lo hace un retiro explícito. `rejected`, `needs_review`,
   `failed` y `no_preference` no retiran ni deshacen un retiro. Una misma interpretación puede retirar una clase y
   establecer otra; si retira y establece la misma, prevalece el retiro.

**Orden determinista (O4).** Es lexicográfico, sin puntuaciones ni “relevancia”:

1. Se conserva primero la elegibilidad de E5; una preferencia **nunca** vuelve elegible una oferta ni hace inelegible
   una que ya lo era. Las preferencias ordenan **dentro de cada grupo de elegibilidad** (`eligible`, `needs_review`,
   `ineligible`): una oferta no elegible que cumple la preferencia no adelanta a una elegible que no la cumple.
2. Las clases se aplican en este orden: **precio → proveedor → laboratorio → marca → disponibilidad**. El precio va
   primero porque expresa un límite comercial explícito; luego la relación comercial, los atributos del producto y la
   disponibilidad.
3. Dentro de cada clase se forma un grupo que cumple (`prefer`) o que queda al final (`deprioritize`); no se asigna un
   número a la oferta.
4. Si dos ofertas empatan en todas las clases, se conserva el orden determinista anterior (desempate estable por
   `offer_id`), de modo que el resultado es reproducible.
5. **Todas las ofertas siguen visibles**; el orden no cambia el conjunto, los precios, el stock, las cantidades, los
   descuentos, los impuestos, los totales ni la elegibilidad.
6. **Tolerancia.** Una preferencia de laboratorio, marca o proveedor puede llevar una tolerancia relativa (porcentaje)
   o absoluta (monto en la moneda de la oferta). Sirve para decidir si una oferta que **no** cumple la preferencia
   puede quedar por delante cuando la diferencia de precio es pequeña. Sin tolerancia, el grupo que cumple va primero.
   La comparación usa la misma base de precio, la misma moneda y valores equivalentes; nunca compara precio unitario
   con total de línea.
7. **Precio.** `acceptable` y `maximum` se aplican sobre la base declarada (`per_base_unit`, `per_commercial_unit` o
   `per_line_total`). `maximum` no hace inelegible ni oculta una oferta que lo supera: solo la coloca después.
   `acceptable` no prohíbe precios mayores: es una referencia de conveniencia, no un límite duro.
8. **Disponibilidad.** Se usa la disponibilidad **informada** dentro de las reglas de elegibilidad existentes; una
   cantidad desconocida no se trata como cero ni como stock seguro.
9. **Preferencia negativa.** `deprioritize` coloca al final las ofertas del proveedor o atributo indicado; **no las
   excluye** y no impide seleccionarlas manualmente.

**Auditoría interna.** Cada aplicación deja una fila en `preference_order_trace` con producto, contexto,
interpretaciones **consultadas**, preferencias **aplicadas** (incluidos el estado, el modelo y la versión del prompt
vigentes al aplicar), ofertas consideradas y orden anterior/resultante. No se muestra en el cotizador ni se devuelve en
la API pública (AC31/D-141). El detalle de artículo registra la traza en modo *best effort*: un fallo de auditoría se
revierte solo (savepoint) y se registra internamente, sin interrumpir ni ocultar la lectura. La comparación mantiene la
traza dentro de su transacción: si falla, no queda una corrida a medias. Sin colas ni procesos en segundo plano (D-143).

### 8.5 Integración con OpenAI

**Responses API, invocada solo desde el backend y detrás de un adaptador desacoplado del proveedor.** Lovable nunca ve
la clave ni llama al proveedor. La salida del modelo es una **propuesta estructurada**; el backend la valida antes de
guardar o aplicar cualquier cosa.

| Configuración | Tipo | Regla |
|---|---|---|
| `OPENAI_API_KEY` | `SecretStr` | Requerida solo si A6 está habilitado; si falta, la observación se guarda y el orden queda sin cambios |
| `OPENAI_MODEL` | texto | **Sin valor por defecto inventado**; si está vacío, A6 queda deshabilitado y se informa el estado recuperable |
| `OBSERVATION_AI_ENABLED` | booleano | Interruptor de despliegue; por defecto `false` |
| `OPENAI_MAX_OUTPUT_TOKENS` | entero | Cota por solicitud |
| `OPENAI_TIMEOUT_SECONDS` | entero | Tiempo máximo; al vencer, la observación se guarda y el estado queda recuperable |
| `OPENAI_PROMPT_VERSION` | texto | Versión del prompt, guardada en cada interpretación |
| `OPENAI_MAX_CALLS_PER_MINUTE` | entero | Protección básica contra llamadas excesivas; no bloquea la operación normal de la demo |

**Diferido, no aprobado en esta revisión:** presupuesto mensual, tarifa por token, auditoría financiera del consumo y
reportes de costo. No se inventan valores ni se calculan cargos. La protección básica de llamadas no sustituye un
presupuesto.

Reglas de llamada:

1. **Salida estructurada** validada contra un esquema estricto (Pydantic). Si no valida, se registra `invalid_response`
   y **no** se guarda una interpretación ni una preferencia inventada.
2. Se guardan `model`, `prompt_version`, `raw_output`, tokens informados y `latency_ms` (§2.8). La salida cruda es
   interna y no se devuelve al frontend.
3. **Timeout y reintento acotado** (máximo un reintento). Sin bucles ni llamadas en segundo plano.
4. **La observación se guarda primero**, antes de llamar al modelo. Un fallo del proveedor no pierde texto, autor,
   fecha ni contexto.
5. **Errores internos distintos:** `quota_exceeded`, `rate_limit`, `timeout`, `unavailable`, `invalid_response` y
   `validation_error`. Se registran en la interpretación y en auditoría; nunca se muestran códigos, claves, trazas ni
   detalles del proveedor al usuario.
6. **Sin interpretación aplicable**, el orden es el determinista de E5 y la interfaz informa que la observación quedó
   guardada pero la preferencia no se aplicó.
7. **Reintento:** `POST /commercial-observations/{id}/interpretations` crea un intento nuevo; **no** duplica la
   observación ni la preferencia. La observación conserva su historia.
8. **La clave vive en `backend/.env`** (fuera de git), la carga `core/config.py` y **nunca** se escribe en logs,
   respuestas, auditoría ni frontend. Los errores se registran sin la clave ni el texto completo de la observación.
9. **Pruebas automatizadas sin llamadas reales:** el adaptador se sustituye por un doble; ninguna prueba usa la clave,
   la red ni consume cuota.

**Estado de implementación (Fase 3A, D-145).** El servicio `app/services/observation_ai.py` implementa el flujo
completo sobre una observación **ya guardada**: carga el contexto verificado del artículo, arma el prompt, llama al
proveedor detrás de `ObservationModel` y valida la propuesta con `app/interpretation/commercial.py`. El adaptador
`app/interpretation/openai_observations.py` usa la **Responses API** con `json_schema` estricto, `store=false`, un
único reintento para fallos transitorios y un límite local de llamadas por minuto; traduce los fallos a los códigos
internos del punto 5 sin exponer cuerpos ni credenciales. La configuración vive en `core/config.py` con
`OBSERVATION_AI_ENABLED=false` por defecto y `OPENAI_MODEL` **sin valor por defecto**: sin interruptor o sin
credenciales no hay llamada; con el interruptor encendido y configuración incompleta se registra un intento `failed`
recuperable (`unavailable`) y la observación permanece intacta. Con una propuesta válida, la interpretación
`suggested` y **todas** sus `commercial_preference` se escriben en la misma transacción; con retiros no se crean
preferencias y solo se registra `retired_subject_kinds`. Una interpretación ya aplicable se **reutiliza** (sin nueva
fila) y un intento fallido puede reintentarse sin duplicar la observación ni las preferencias. No se ordena ninguna
oferta en este servicio: el motor determinista sigue siendo el único que ordena E3/E5. **Los endpoints de
interpretación, Lovable y cualquier llamada real desde la suite siguen pendientes.**

**Mensaje amable sugerido ante un fallo del proveedor** (sin detalles técnicos):

> «No pudimos procesar tu preferencia. Tu observación quedó guardada, pero el servicio de inteligencia artificial no
> está disponible en este momento. Puedes seguir utilizando el cotizador y volver a intentarlo más tarde».

No se afirma que la preferencia fue aplicada si solo se guardó la observación.

### 8.6 Prohibiciones de la IA

La interpretación **no puede**:

- modificar precio, stock, condiciones, cantidad, moneda, descuentos, impuestos, totales ni elegibilidad;
- **crear, quitar u ocultar** ofertas;
- alterar el **conjunto** de ofertas recuperadas: el orden por preferencias no descarta ninguna;
- aplicar una preferencia a otro producto por compartir laboratorio, marca o proveedor, ni por parecido de nombre;
- construir perfiles globales de reputación de laboratorios, marcas o proveedores;
- otorgar o quitar permisos, roles o accesos;
- afirmar equivalencia farmacológica, dosis equivalente o sustitución;
- inventar ofertas, precios, stock, cantidades o condiciones comerciales;
- mostrarse como chatbot, explicación, justificación, recomendación textual o puntuación al usuario;
- borrar, editar o «resumir definitivamente» una observación;
- ejecutar una compra, aprobar un acceso o modificar datos comerciales.

**Sí puede**, y es su función aprobada: **ordenar** las ofertas recuperadas **de un producto** según las preferencias
comerciales aprendidas **de ese producto**, manteniendo visibles todas las demás. El backend —no el modelo— determina
el alcance, valida el dinero y aplica el orden.

### 8.7 Aceptación de la demostración

1. La interpretación se aplica **solo al producto seleccionado**; no alcanza a otros productos ni a la organización.
2. «Prefiero Laboratorio Chile» en el Losartán **no** cambia el orden de otro producto del mismo laboratorio, marca o
   proveedor. La demo muestra el contraejemplo.
3. La tolerancia implícita (10 %) se aplica de forma determinista y configurable.
4. Un porcentaje explícito (15 %) **reemplaza** la tolerancia implícita; no se mezcla con el valor por defecto.
5. Un monto adicional en CLP se interpreta con la moneda y la base correctas.
6. «Si vale $9.990 me parece bien» y «No quiero pagar más de $9.990» producen referencias distintas (`acceptable` y
   `maximum`) y no se confunden.
7. Una preferencia negativa **desprioriza pero no oculta** ni excluye al proveedor.
8. La elegibilidad E5 se conserva exactamente: ninguna preferencia vuelve elegible una oferta inelegible ni al revés.
9. Precios, stock, cantidades, descuentos, impuestos y totales permanecen **idénticos**; solo cambia el orden.
10. Una interpretación ambigua o no comprensible **no se aplica**: queda `needs_review` o `no_preference`, sin efecto
    comercial ni retiro implícito de una preferencia vigente.
11. Sin preferencias aplicables, el orden es **exactamente el determinista actual**, con el mismo desempate.
12. La preferencia más reciente de la misma clase reemplaza a la anterior según §8.4. Un **retiro explícito**
    posterior la deja sin efecto, y una preferencia posterior válida vuelve a establecerla; el retiro no afecta otras
    clases, otros productos ni el historial (D-144).
13. Una interpretación `rejected` **no reactiva** una anterior **ni desactiva la vigente**.
14. Las preferencias ordenan **dentro de cada grupo de elegibilidad**; una oferta no elegible no adelanta a una
    elegible aunque cumpla la preferencia.
15. Un fallo de OpenAI **no pierde** la observación; queda guardada y el estado es recuperable.
16. El agotamiento de cuota produce un estado recuperable y un mensaje amable, sin detalles técnicos.
17. Reintentar **no duplica** la observación ni crea preferencias duplicadas.
18. Ninguna respuesta, log, auditoría ni pantalla expone la clave, la salida cruda del modelo ni sus internals.
19. Las pruebas automatizadas **no hacen llamadas reales** a OpenAI.
20. Lovable conserva tarjetas, estilos, colores, disposición, botón «Enseñar a la IA», modal y flujo de
    búsqueda/comparación/carrito; no se muestran puntuaciones ni explicaciones.
20. El flujo completo es demostrable de extremo a extremo con datos de demo, incluido un producto no farmacéutico.

**Estado de implementación:** la fase 1 implementó el esquema, los modelos, la migración `0018` y las pruebas de
esquema; las fases 2 y 3 implementaron el motor determinista, la traza explicable (D-143), el retiro explícito
(migración `0019`, D-144) y la **interpretación interna con OpenAI** (Fase 3A, D-145, con proveedor simulado en las
pruebas). Los criterios de orden, tolerancias, retiro, inmutabilidad, errores del proveedor, no invención, aislamiento
por producto y traza tienen prueba automatizada. **Ninguno de los 20 se declara cumplido de extremo a extremo:** faltan
los **endpoints** (criterios 1, 2, 12 y 17 en su forma pública), la **demo con Lovable y el proveedor real**
(criterios 15–20); la suite nunca consume cuota ni usa la clave.

---

## 9. E14 — Acceso web con Google

### 9.1 Autenticación ≠ autorización

Google/Supabase **autentica** (quién es). Nuestra base **autoriza** (qué puede hacer). Un JWT válido no basta: debe
existir un `app_user` activo con `auth_subject` enlazado y acceso vigente.

### 9.2 Un solo perfil funcional: `admin`

| Nivel | Quién | Puede | No puede |
|---|---|---|---|
| **Superadministración técnica** | Yarella Fernández | Control técnico del sistema (código, base, infraestructura); `is_superadmin` para tareas globales | Ser un perfil funcional diferenciado; aprobar accesos en nombre de una farmacia; operar datos de otra organización |
| **`admin` (único perfil web)** | Pao, Danny, Julio y toda persona aprobada | Todo el Cotizador (cotizar, comprar, historial, observaciones) y la administración de **su** farmacia (personal, accesos, campanita) | Privilegios globales; otras organizaciones; registrar recepciones (fuera del MVP) |
| **Sin aprobación** | Personas con Google | Solicitar acceso | Cualquier función del Cotizador: Google identifica, no autoriza |

Los nombres son personas, no código. `roles` conserva sus cuatro valores por compatibilidad con Telegram y los flujos
de faltantes, pero el Cotizador web **solo** acepta el perfil `admin`. Aprobar una solicitud otorga exactamente ese
perfil. **No se crea un rol nuevo ni un segundo perfil funcional.**

### 9.3 Flujo

1. «Continuar con Google» → Supabase devuelve un JWT.
2. El backend valida firma, emisor, audiencia y expiración con el validador existente.
3. Busca `app_user.auth_subject == sub`, `active = true` y `web_access_expires_at` vigente.
4. Si existe y está vigente → entra con sus roles.
5. Si no existe o no está enlazado → `403 USER_NOT_PROVISIONED`; la pantalla ofrece **«Solicitar acceso»**.
   Si Google/Supabase Auth no está configurado → `503 ACCESS_NOT_CONFIGURED`.
6. `POST /api/v1/web-access-requests` crea una solicitud `pending` en la organización **configurada en el servidor**.
7. La campanita la ven **todas** las personas con `admin` de esa organización; cualquiera aprueba o rechaza.
8. **Aprobar otorga el único perfil funcional: `admin` (D-130).** Crea el `app_user` con `roles = ['admin']`,
   `active = true` y el `auth_subject` de la solicitud, **en el mismo paso**: la persona entra al Cotizador con acceso
   completo, sin segunda autorización ni intervención de la superadministradora. No otorga `is_superadmin` y no crea
   ni autoriza nada en Telegram (§9.10). No existe un perfil `purchasing` del Cotizador: ese rol sigue existiendo solo
   para los flujos de Telegram y faltantes.
   Si el nombre visible ya existe activo en la organización, la aprobación responde `409` y se pide renombrar;
   **nunca se vincula por nombre ni se sobrescribe** una persona existente. Si el `auth_subject` ya está vinculado a
   una persona (activa o no), la aprobación responde `409 USER_ALREADY_EXISTS`: enlazar o reactivar una fila existente
   es una operación **separada y auditada** (`link-auth-subject`, `POST /api/v1/admin/users/{user_id}/web-access`).
   La aprobación **no fija** `web_access_expires_at`: queda `NULL`, es decir, **sin vencimiento automático** (§9.8).

**Pao, Danny y Julio se habilitan previamente** con `create-admin` / `link-auth-subject` (D-071/D-072) y entran con
Google sin pasar por la campanita. La campanita es para solicitudes nuevas o excepcionales.

### 9.4 Endpoints

| Método y ruta | Quién | Para qué |
|---|---|---|
| `POST /api/v1/web-access-requests` | JWT válido, sin `app_user` | Crear solicitud. Cuerpo `{}`; organización desde configuración del servidor. Idempotente por `(organization_id, auth_subject)` pendiente |
| `GET /api/v1/web-access-requests` | `admin` de la organización **o** superadministradora | `{ "pending_count": 1, "items": [{ "id", "email", "requested_at", "organization"? }] }`; `organization` solo para la superadministradora |
| `POST /api/v1/web-access-requests/{id}/approve` | Cualquier `admin` de la organización | Aprueba; crea el `app_user` con `roles = ['admin']`, `active = true` y `auth_subject` (D-130), sin vencimiento (`web_access_expires_at = NULL`). Responde 200 |
| `POST /api/v1/web-access-requests/{id}/reject` | `admin` de la organización | Rechaza con motivo opcional; no crea usuario |
| `POST /api/v1/admin/users/{user_id}/web-access` | **Superadministradora** | Otorga o cambia roles y `expires_at`. Solo roles existentes; auditado |
| `POST /api/v1/admin/users/{user_id}/web-access/revoke` | **Superadministradora** | Revoca **solo la web** (`web_access_expires_at = now`); no toca Telegram ni `active` |
| `GET /api/v1/me` | Cualquier usuario autenticado | Identidad, organización, roles, `is_superadmin`, vencimiento |

### 9.5 Seguridad del endpoint público

- Mismo validador de JWT; sin confiar en el cuerpo para identidad ni organización.
- Del token se toman **solo** `sub`, `email` y nombre; se exige `email_verified = true`.
- La organización sale de la configuración del servidor, **nunca** del cuerpo.
- Límite de tasa por `sub` y por IP.
- **Misma respuesta** exista o no una cuenta o solicitud previa: no se revela si el correo está registrado ni a qué
  organización pertenece.
- La solicitud **no otorga nada**. Solo la aprobación habilita, y solo un `admin` de esa organización puede aprobar.
- Aprobar es transaccional; se audita en `audit_event` con actor, solicitud y decisión.

### 9.6 Campanita

- `GET /api/v1/web-access-requests` devuelve `pending_count` e `items`.
- La interfaz muestra solo **check verde** (aprobar) y **cruz roja** (rechazar). Sin formularios adicionales.
- Al decidir, la solicitud desaparece y el contador baja.
- Sin pendientes: «No hay solicitudes pendientes».
- La decisión **permanece auditada** aunque la solicitud ya no se muestre.
- La superadministradora ve las solicitudes de todas las organizaciones, con la organización indicada; una
  administradora de farmacia ve solo la suya. **La superadministradora no aprueba en nombre de una farmacia**: la
  decisión es de un `admin` de esa organización; si no existe, se usa `create-admin`/CLI para el primer acceso.

### 9.7 Superadministración

- `app_user.is_superadmin` booleano, **no** un rol. `roles` conserva su CHECK cerrado.
- **Exactamente una superadministradora activa.** No puede ser desactivada, bloqueada ni degradada por una
  administradora de farmacia; solo por ella misma o por el CLI `set-superadmin`, auditado.
- **Excepción nombrada a D-037:** puede cruzar organizaciones **solo** para metadatos administrativos
  (organizaciones, administradoras, roles, solicitudes de acceso). **Los datos operativos siguen acotados a la
  organización.** Ampliar el cruce a datos operativos exige una decisión nueva.
- `set-superadmin` es de uso único, auditado y no se ejecuta en esta fase.
- **La superadministradora no informa faltantes ni opera el cotizador en nombre de otra organización.** Cotizar y
  confirmar compras son datos operativos: solo un `admin` de la organización dueña puede hacerlo (D-130). Su capacidad
  de informar por Telegram existe **solo** si tiene una cuenta de Telegram autorizada en esa organización.

### 9.8 Vencimiento y revocación

- **La aprobación no fija vencimiento (D-130).** `web_access_expires_at` queda `NULL` = **acceso sin fecha de
  expiración**; sigue vigente hasta una revocación, una desactivación o un vencimiento fijado explícitamente por la
  superadministradora. **No hay plazo automático** (ni 90 días ni otro).
- `web_access_expires_at` se evalúa en **cada** petición; si venció → `403 WEB_ACCESS_EXPIRED`. No hay job en
  segundo plano.
- Revocar la web no borra la persona, no borra su historial y no bloquea Telegram.
- Desactivar la persona (`active = false`) es una suspensión más fuerte: corta web y Telegram.

### 9.9 Sin contraseñas ni registro público

No hay recuperación de contraseña ni registro público (D-066). El alta es por Google + solicitud aprobada, o por
`create-admin` / `link-auth-subject` para las personas ya previstas.

### 9.10 Independencia con Telegram (D-130)

**Aprobar una solicitud web no otorga ningún acceso a Telegram.** El plano del bot se autoriza por cuenta, no por
persona con roles web:

- La aprobación **no crea** `telegram_account`, **no crea** `channel_binding`, **no crea** `telegram_access_request` y
  **no autoriza** ningún chat; `resolve_access` sigue cortando en cada mensaje si el chat o la cuenta no están
  autorizados (D-118).
- En un **teléfono personal** la identidad la fija la cuenta autorizada. Un aprobado web sin cuenta personal **no puede
  reportar** por el bot, aunque tenga `REPORT_SHORTAGE` en la matriz.
- En un **teléfono compartido** la autorización es del dispositivo y la persona se **declara** eligiéndose en un
  teclado (`actor_session.identity_level = declared`). Ese selector **no es una lista de autorizados**: es una
  identificación declarada dentro de un chat ya autorizado, y por eso puede incluir a cualquier persona activa,
  disponible y con `REPORT_SHORTAGE`, incluida una aprobada por la web.
- **G1 no modifica el bot ni agrega una bandera de reportante por persona.** Excluir a alguien del selector compartido
  exigiría una señal por persona y es una decisión de **T1** (D-118), no de esta fase.
- **`CONFIRM_ATTRIBUTES` conserva su alcance actual.** No se amplía ni se rediseña la gestión de alias en G1; cualquier
  cambio de ese alcance es una decisión propia (D-015 a D-024).

---

## 10. T1 — Control individual de Telegram

### 10.1 Qué se controla

Pao puede **habilitar o deshabilitar individualmente** a qué personas se les permite usar Telegram para enviar
faltantes. **No hay un interruptor global del bot.**

### 10.2 Reutilización, sin tabla ni migración nuevas

Se reutiliza lo que ya existe:

| Método y ruta | Efecto |
|---|---|
| `GET /api/v1/telegram-accounts` | Lista de cuentas con persona, chat, tipo (`personal`/`shared_device`), estado y última modificación |
| `POST /api/v1/telegram-accounts/{id}/block` | `active → blocked`, con motivo obligatorio. Revoca sesiones (`end_reason='revoked'`), desactiva vínculos y **conserva todo el historial** |
| `POST /api/v1/telegram-accounts/{id}/reactivate` | `blocked → active`. Reutiliza la **misma fila**; recrea el vínculo privado sin nueva solicitud ni código |
| `GET /api/v1/telegram-accounts/{id}/history` | Historial de la cuenta, incluidos bloqueos y rehabilitaciones |

Requieren `MANAGE_ACCOUNTS` + `StrongActor`; en organización real, JWT de Supabase. No se crea tabla nueva.

### 10.3 Efecto inmediato y conservación

- El backend vuelve a validar el acceso en **cada** mensaje entrante (`resolve_access`); una cuenta `blocked` no
  opera aunque tenga una sesión previa.
- Bloquear **no borra** mensajes, observaciones, faltantes ni vínculos: solo impide operar.
- Rehabilitar no crea una cuenta nueva ni exige otro código; el historial anterior sigue visible.
- Se audita `channel.account_blocked` / `channel.account_reactivated` con actor, motivo y fecha.

### 10.4 Límites declarados

- El control es **por cuenta**, no por chat. Una cuenta `shared_device` se deshabilita para **todas** las personas
  que usan ese teléfono; el control individual exige cuentas `personal` (configuración actual).
- **Telegram es independiente de la web.** Una persona puede tener Telegram sin acceso web y viceversa. Los
  auxiliares solo usan Telegram; no cotizan ni entran al Cotizador web.
- **`admin` también puede reportar por Telegram (D-124).** Pao, Danny y Julio pueden informar faltantes y consultar
  estados si su cuenta de Telegram está `active` y su identidad está vigente; además pueden cotizar y confirmar
  compras en la web. Ningún permiso web se infiere de la cuenta de Telegram ni al revés. El bot **no** registra
  compras, **no** confirma llegadas y **no** aplica cantidades.
- El token del bot sigue del lado del servidor y nunca se muestra en esta pantalla.

### 10.5 Reporte repetido de un producto activo (D-131)

Mientras un faltante está **`active`**, la conversación del bot sigue estas reglas, sin IA:

1. **Identifica** quién reporta, qué producto y qué cantidad, y **conserva lo escrito** (`original_text`).
2. **El reporte es de cada persona (D-135).** Si el mismo producto lo reportó **otra persona**, el bot informa quién y
   con qué cantidad **objetivo** y **solicitada** (si existe), y pregunta si quiere **su propio reporte independiente**.
   No adjunta el aviso al faltante ajeno, no lo fusiona y no suma cantidades: cada persona conserva su registro. La
   pregunta es determinista, no de un modelo.
3. **Confirmación del otro autor:** los botones son **Confirmar mi reporte**, **Modificar cantidad** y **Cancelar**.
   - Confirmar crea un **reporte propio** (con su propia cantidad pedida, si la declaró) y deja constancia de que no se
     sumó al de la otra persona.
   - Modificar pide escribir la cantidad; el reporte se crea con la cantidad **declarada**, nunca aplicada como pedido
     hasta que una persona con `APPLY_QUANTITY` lo decida.
   - Cancelar descarta el mensaje sin crear nada; no se borra la evidencia y el mensaje queda `discarded`.
4. **Mismo autor, misma necesidad (D-147).** Si ya existe **su** faltante idéntico, el bot pregunta por **texto** si
   la cifra es **adicional (ADD)** o **total nuevo (SET)**; si no hay cantidad previa, pide la cantidad que quiere
   solicitar. Esta conversación **no usa botones inline** en ninguna etapa. El valor del `ADD` es la diferencia entre
   lo declarado y la referencia; el `SET` fija el total declarado. **Nunca se suma solo**, ni por el texto ni por
   repetir el aviso: la misma cantidad y unidad se registran como aviso sin incrementar. El auxiliar responde por
   **texto libre**: `5`, `5 cajas más`, `en total 15 cajas`, `me equivoqué, son 5` o `cancelar`. `más`/`adicional` es
   `ADD`; `total`/`en total`/corrección es `SET`; `cancelar` descarta la corrección pendiente sin cambiar la cantidad.
   Una cifra sola o una operación contradictoria es **ambigua**: se vuelve a preguntar por texto y **no se modifica
   nada** hasta que la persona responda. La corrección se registra en el **mismo faltante del autor**, como nueva
   observación; no crea otra fila.
   **Unidad predeterminada:** un aviso de Telegram nuevo sin cantidad registra **1 unidad genérica**
   (`quantity_origin="default"`), sin suponer caja, frasco ni sobre; un aviso adjunto a un faltante histórico sin
   cantidad también la recibe en una observación nueva, sin reescribir el historial. **Unidades incompatibles:** no se
   suma 1 unidad genérica con 10 cajas ni cajas con frascos. Si la referencia es la unidad genérica y la persona
   escribe `más`, se pregunta por texto qué representa esa unidad inicial («¿Es 1 caja?») y solo entonces se calcula
   el total; si no hay equivalencia válida, solo se ofrece escribir el total. No se inventan presentaciones ni
   conversiones.
   **En carrito:** mientras el faltante esté en un carrito activo (`in_quote`), un aviso o corrección no cambia su
   cantidad solicitada; el bot lo informa y sugiere pedir a compras que lo retire. Sin compra vuelve a «Pendiente de
   compra» al salir del carrito, según la cobertura vigente.
5. **Separación solicitada ≠ objetivo (D-147).** Los mensajes de Telegram **nunca** modifican automáticamente
   `demand_item.target_quantity`; una acción de texto libre **no** es autorización para cambiarla, **tampoco** cuando
   el actor tiene `APPLY_QUANTITY`. La corrección registra la **cantidad solicitada** vigente —que la API expone como
   `requested_quantity`/`requested_unit` con su procedencia `requested_quantity_origin`/`quantity_origin`— y deja una
   **propuesta** (`quantity_proposal`) para que compras la revise. Como la corrección ya no ofrece botones, **ninguna
   respuesta de esta conversación** aplica la cantidad de compra, aunque el actor tenga `APPLY_QUANTITY`. Los mensajes
   al auxiliar no mencionan la cantidad objetivo ni la propuesta: son conceptos internos y de la API. Compras ve
   siempre la solicitud, su procedencia, la propuesta y la cantidad objetivo por separado.
6. Mientras el faltante siga `active`, **no se inicia otro ciclo** para el mismo producto y la misma persona; cada
   persona puede tener **su** reporte activo. Un reporte `cancelled` deja de bloquear uno nuevo de esa persona.
7. Al **registrar una compra** que cubre la cantidad pedida, el faltante se cierra (`fulfilled`, no «recibido») y el
   producto **se libera**: puede volver a reportarse como un faltante nuevo si hace falta. La compra es **todo o nada**
   (enmienda a D-127): se registra completa o no se registra, y al registrarse libera el faltante en la misma
   transacción. No hay compras parciales que dejen un saldo abierto.
8. **Se conserva el historial**: quién reportó cada aviso, cuándo y qué dijo. Nada se borra; cancelar es lógico.

El bot **no usa OpenAI** para interpretar mensajes: producto, cantidad e identidad se resuelven con las reglas
existentes y, ante ambigüedad, con candidatos para que compras decida (D-025 a D-036).

---

## 11. E15 — Búsqueda de precios públicos de cadenas

### 11.1 Independiente de E1

La búsqueda de cadenas es un contrato **aparte** de E1. No modifica E1/E3, no exige un artículo del catálogo propio
y **no persiste** resultados. Devuelve fichas reales de las cadenas, tal como se publican, con su fuente y fecha.

### 11.2 Operación asíncrona corta (sin SSE)

| Método y ruta | Para qué |
|---|---|
| `POST /api/v1/chain-searches` | Inicia la consulta. Cuerpo: `{ "query": "losartán 50 mg", "chains": ["cruz_verde","salcobrand","ahumada"] }` (opcional). Responde **202** con `{ "search_id", "status": "pending", "expires_at" }` |
| `GET /api/v1/chain-searches/{search_id}` | Sondeo del resultado. **200** cuando está lista; **404 `SEARCH_NOT_FOUND`** si no existe o expiró |

No hay SSE ni streaming. El estado vive **en memoria del proceso**; por eso se documenta **un solo worker de API**.
Reiniciar el backend pierde las búsquedas en vuelo: el sondeo devuelve 404 y hay que repetir la consulta.

### 11.3 Forma de la respuesta

> Ejemplo **ilustrativo de forma**, no un dato real. Hoy **ninguna cadena responde**: las tres están
> `not_configured` (§11.6). No es un fixture operativo ni debe presentarse como precio vigente.

```json
{
  "search_id": "uuid",
  "status": "completed",
  "query": "losartán 50 mg",
  "created_at": "2026-10-30T15:00:00Z",
  "expires_at": "2026-10-30T15:10:00Z",
  "sources": [
    {
      "chain": "cruz_verde",
      "state": "ok",
      "outcome": "matches",
      "observed_at": "2026-10-30T15:00:03Z",
      "results": [
        {
          "chain_item_id": "string",
          "published_name": "Losartán 50 mg x 30 comprimidos",
          "laboratory_as_published": "Laboratorio Chile",
          "presentation_as_published": "30 comprimidos",
          "description": "string | null",
          "image_url": "https://... | null",
          "price": {
            "normal": { "amount": "4990", "currency": "CLP" },
            "promo": null
          },
          "conditions": "string | null",
          "source_url": "https://...",
          "observed_at": "2026-10-30T15:00:03Z",
          "provenance": {
            "source": "cruz_verde",
            "mode": "live",
            "data_origin": "captured",
            "evidence": { "url": "https://...", "fetched_at": "..." }
          },
          "match": { "trade_item_id": null, "match_status": "unmapped" }
        }
      ]
    },
    {
      "chain": "salcobrand",
      "state": "not_configured",
      "reason": "search_not_available",
      "results": []
    },
    {
      "chain": "ahumada",
      "state": "not_configured",
      "reason": "access_not_authorized",
      "results": []
    }
  ]
}
```

- `match.match_status`: `unmapped` \| `exact`. **`exact` solo con correspondencia confirmada por una persona**
  (D-112). Sin confirmación, la ficha se muestra como resultado de la cadena, no como artículo.
- `price` viaja como **string + moneda**, nunca float.
- `provenance.data_origin` es `captured`; si algún día se usa un simulador, debe decir `simulated` y **nunca** entrar
  en una organización real (D-037).

### 11.4 Estados y resultados

| `state` | `outcome` | Significado |
|---|---|---|
| `ok` | `matches` | La cadena respondió y hay al menos una ficha |
| `ok` | `no_matches` | La cadena respondió válidamente y no hay fichas |
| `stale` | — | Se conserva una observación anterior solo como referencia; **no** es el precio de esta consulta |
| `failed` | — | Error real: `blocked`, `timeout`, `network_error`, `rate_limited` |
| `not_configured` | — | No hay conector autorizado: `access_not_authorized`, `search_not_available`, `requires_javascript` |

Reglas:

1. **Un CAPTCHA o desafío es `failed`, nunca `no_matches`.** Una cadena bloqueada no es «sin resultados».
2. Una respuesta vacía válida es **200**, nunca 503.
3. Si **todas** las cadenas están `not_configured`, la búsqueda igual termina `completed` con 200 y el estado real de
   cada fuente. No se inventan resultados para «completar» la pantalla.
4. **No se usan 502 ni 504 para cadenas.** El único 503 de este contrato es el existente
   `DATABASE_NOT_CONFIGURED`.

### 11.5 Límites operativos

| Límite | Valor |
|---|---|
| Consulta bajo demanda; sin cron ni espejo de catálogo | — |
| Consultas en paralelo, una por cadena | — |
| Tiempo máximo por fuente | 4 s |
| Presupuesto global por búsqueda | 8 s |
| Vigencia del resultado en memoria (TTL) | 10 min |
| Búsquedas vivas simultáneas por proceso | 50 |
| Workers de API | **1** (estado en proceso; reinicio pierde búsquedas en vuelo) |
| Persistencia | **Ninguna**; no hay migración |

### 11.6 Estado inicial real de cada cadena

Ninguna cadena está habilitada. Así se informa, sin precios inventados:

| Cadena | Estado | Motivo | Fundamento |
|---|---|---|---|
| Cruz Verde | `not_configured` | `requires_javascript` | `robots` permite `/search`, pero los resultados se cargan por JavaScript; no hay conector autorizado |
| Salcobrand | `not_configured` | `search_not_available` | La búsqueda requiere JavaScript y `robots` no permite `/api`; no hay conector autorizado |
| Farmacias Ahumada | `not_configured` | `access_not_authorized` | Hay reCAPTCHA. Los términos (versión 13.10.23) **no autorizan ni prohíben** el acceso automatizado; la captura automatizada **no está autorizada** |

La habilitación es por variable de entorno `CHAIN_SEARCH_ENABLED` (lista de cadenas), **no** por código. `sources[]`
siempre lista las tres cadenas con su estado real, aunque estén deshabilitadas.

### 11.7 Política de captura (D-115)

- **No** se usan datos ficticios, precargados ni históricos como sustituto de una cadena no disponible.
- **No** se extrae en volumen, no se eluden CAPTCHA/MFA, no se simula un navegador para saltar protecciones.
- Si se habilita una cadena, el cliente se identifica honestamente, respeta límites de tasa y consulta **solo bajo
  demanda**.
- **Farmacias Ahumada no está autorizada.** Dos caminos, ninguno ejecutado:
  - **A1:** autorización escrita o API del titular.
  - **A2:** decisión explícita de riesgo de negocio, registrada en D-113 con fecha, fundamento jurídico, condición de
    revocación, consultas limitadas, sin extracción masiva, sin eludir protecciones, identificación honesta y límite
    de tasa.
- Sin A1 o A2, el conector de Ahumada **no se construye**. Lo mismo para cualquier cadena sin autorización.

### 11.8 Vigencia y verificación al cotizar

- `observed_at` es **de esta consulta**; un precio anterior nunca se presenta como actual.
- Al agregar o revisar una oferta en una cotización, se repite la consulta con
  `{ "purpose": "quote_verification", "query": "..." }`; **solo el precio de esa verificación** puede mostrarse como
  vigente.
- Si la fuente falla en la verificación, se muestra el fallo; no se reutiliza el precio anterior como si fuera actual.
- Los precios de cadena **nunca** entran en `offer_snapshot`, carritos, totales ni elegibilidad (C0-05).

### 11.9 Sin persistencia

- No se guarda la búsqueda ni sus resultados. `chain_product` y `reference_price` (0008) solo se usan como
  **evidencia** cuando una persona confirma una correspondencia (D-112); **no** son un espejo del catálogo de la
  cadena.
- No hay migración para E15.

### 11.10 Códigos

| HTTP | Código | Cuándo |
|---|---|---|
| 404 | `SEARCH_NOT_FOUND` | Búsqueda inexistente, expirada o perdida por reinicio |
| 429 | `RATE_LIMITED` | Demasiadas búsquedas; incluye `Retry-After` |

---

## 12. Clasificación `relation` y cobertura de faltantes

### 12.1 `relation`, junto a `match.level`

E1 agrega un campo **aditivo** `relation` a cada resultado. **`match.level` no cambia** y se conserva tal como está
congelado en C0. `relation` describe la relación comercial entre lo buscado y lo encontrado; no es un puntaje ni una
equivalencia.

| `relation` | Regla |
|---|---|
| `exact` | Misma identidad comercial: principio(s) activo(s)/marca + potencia + forma + contenido |
| `other_presentation` | Misma identidad farmacéutica, distinto contenido o envase |
| `combined` | `attributes.ingredients[]` contiene el ingrediente buscado **y al menos otro** |
| `related` | Coincidencia de texto, marca o línea, con identidad distinta |
| `needs_review` | Identidad incompleta o ambigua |

Reglas:

1. `relation` **no filtra ni oculta** resultados; solo permite ordenar y advertir. Un `related` sigue visible.
2. `combined` **nunca** se presenta como equivalente ni como sustituto.
3. Productos no farmacéuticos se clasifican por marca + línea + tamaño; no se les inventa un principio activo.
4. `demand_item.ingredient` es una cadena NOT NULL y puede contener una frase de producto; **no se muestra como
   «principio activo»** ni se modifica la columna. Para combinados se usa `attributes.ingredients[]` (JSONB
   existente, sin migración); si falta, `relation = needs_review`.
5. La misma clasificación se congela en `quote_line.coverage_relation` al agregar desde un faltante (§5.2).

### 12.2 Cobertura de un faltante (D-120)

Un faltante de Telegram se muestra en la pantalla principal, en la grilla **«Productos faltantes (Telegram)»**, con:

- nombre y descripción;
- cantidad pedida, **solo si fue declarada**;
- quién lo pidió y cuándo;
- su **estado de reposición** (§12.6): pendiente, en cotización o comprado —no un estado fijo «pendiente»—;
- botón **«Buscar»**.

**Cubierto por carrito ≠ sin gestionar.** Un faltante de 10 unidades **completamente cubierto por un carrito activo**
se muestra como **en cotización** (`in_quote`): cubierto provisionalmente, saldo por cotizar 0 y saldo por comprar 10.
No se presenta como una solicitud sin gestionar, y tampoco está cerrado: el cierre definitivo sigue exigiendo compra
confirmada (D-127). La cobertura sigue siendo reversible si la línea se quita o el carrito se descarta.

Solo se evalúan faltantes **`active`**; los `cancelled` y `fulfilled` no aparecen en la grilla ni en la cobertura.
**Cada reporte es de una persona (D-135):** dos personas pueden aparecer con el mismo producto y cada fila muestra su
autor, su cantidad declarada y su propio saldo. No hay consolidación automática ni una fila única que mezcle avisos.

**Cancelación lógica (D-135).** `POST /api/v1/demand-items/{item_id}/cancel` con `admin` + `StrongActor`,
`Idempotency-Key` y `If-Match` (versión). Cuerpo `{"reason": "texto | null"}`. Marca `status = cancelled`,
`cancelled_at`, `cancelled_by` y el motivo; **no borra** observaciones, mensajes, cantidades ni historial, y no toca
las líneas de cotización. Responde `200` con el reporte, `affected_quote_lines` (líneas activas que le asignaban
cantidad) y un mensaje; `409 VERSION_CONFLICT` si la versión no coincide, `409 REPORT_NOT_ACTIVE` si ya no está activo
y `409 REPORT_HAS_PURCHASE` cuando C5-B detecte una compra registrada. Un reporte cancelado deja de cubrirse y libera
la identidad de esa persona para un aviso nuevo. **No se puede volver a asignar** (`409 REPORT_NOT_ACTIVE`). Las
cotizaciones que lo tenían asignado no se cierran hasta corregir esas asignaciones (D-137).

**«Buscar» usa la búsqueda principal** (`POST /api/v1/searches` + búsqueda de cadenas). **No cambia el estado del
faltante.** La búsqueda principal es independiente de Telegram. El `demand_item_id` viaja en el contexto de la fila y
se envía al agregar la línea; **no se agrega un campo nuevo a E1**.

### 12.3 Fórmula de cobertura (derivada, nunca almacenada)

La cobertura suma **dos fuentes** y **nunca cuenta dos veces la misma línea** (D-121):

1. lo **cotizado** en líneas activas de carritos activos de cotizaciones `draft` que todavía **no tienen compra
   vigente**;
2. lo **comprado** en compras `current` no anuladas, esté la cotización `draft` o `closed`.

Una línea con compra vigente **sustituye** su cantidad cotizada: cuenta lo comprado, no las dos cosas. **Comprar no
implica entrega:** la cobertura mide el proceso administrativo, no la llegada física (D-126).

**Las dos fuentes no valen lo mismo al cerrar (D-127).** Lo **cotizado** cubre de forma **provisional**: depende de
que la línea siga activa en un carrito activo de una cotización `draft`, se puede quitar, mover o descartar, y **nunca**
cierra el ciclo. Lo **comprado** es el único hecho que cierra, y solo cuando la suma de compras confirmadas cubre
**toda** la cantidad pedida conocida.

```
cover_of_allocation(a) =
    CASE
      WHEN EXISTS (purchase_line_allocation pa vigente de a.quote_line) THEN pa.quantity   -- C5-B, 0020
      WHEN l.status = 'active' AND c.status = 'active' AND q.status = 'draft'
           AND item.status = 'active' THEN a.quantity
      ELSE 0
    END

covered(item) = SUM(cover_of_allocation(a))
  FROM quote_line_allocation a
  JOIN quote_line l ON l.id = a.quote_line_id
  JOIN quote_cart c ON c.id = l.quote_cart_id
  JOIN quote q      ON q.id = c.quote_id
 WHERE a.demand_item_id = item.id
   AND a.organization_id = item.organization_id

purchased_quantity(item) = SUM(pl.quantity de compras current no anuladas)

if target_quantity IS NOT NULL:
    pending_quantity          = max(target_quantity - covered, 0)
    pending_purchase_quantity = max(target_quantity - purchased_quantity, 0)
    is_pending                = pending_purchase_quantity > 0
if target_quantity IS NULL:
    pending_quantity          = NULL
    pending_purchase_quantity = NULL
    is_pending                = true                -- nunca se prueba cobertura completa

cierra(item) = item.status = 'active'
           AND item.target_quantity IS NOT NULL
           AND purchased_quantity(item) >= item.target_quantity
```

**Por qué no hay doble conteo.** Las asignaciones de una misma línea suman como máximo su cantidad (§5.2), así que
una línea no puede cubrir dos veces lo mismo. Si la línea tiene compra vigente, su asignación de compra sustituye a la
de cotización: **por asignación** cuenta lo comprado, no las dos cosas. Si la compra se corrige, solo cuenta el
registro vigente; si se anula, la asignación vuelve a su cantidad cotizada **solo si la línea sigue activa en un
carrito activo de una cotización `draft` y el reporte sigue `active`**; si la cotización ya se cerró, esa línea no
aporta cobertura y el saldo queda sin cubrir. Una compra registrada **sin vínculo con la cotización**
(`quote_line_id = null`) no cubre ningún faltante, porque no tiene asignación con el aviso. Un reporte `cancelled`
conserva sus asignaciones como historia, pero **ninguna** aporta cobertura.

- `target_quantity` y `quantity_event` siguen siendo datos **del faltante**; la cobertura solo los **lee**.
- No se escribe en `demand_item` ni en `demand_observation`. No se borra el mensaje original de Telegram.
- El cálculo se hace en vivo; no hay tabla, columna ni estado de cobertura.
- `is_pending` se calcula solo sobre faltantes `active`; un faltante `cancelled` o `fulfilled` no es pendiente (§12.6).
- **Cierre del ciclo (D-127).** Un faltante `active` pasa a `fulfilled` **solo** cuando `target_quantity` es
  **conocida** y `purchased_quantity >= target_quantity`, en la misma transacción que registra o corrige la compra. La
  cotización **no** cierra nada: cubrir el total con carritos activos deja el faltante abierto en `in_quote`.
  - Con `target_quantity` desconocida **no se cierra automáticamente** (V-06): el faltante queda abierto —`purchased`
    si hay compras, `in_quote` si solo hay cotización— y sale del ciclo únicamente por un `ADD`/`SET` explícito que
    declare la cantidad pedida (con lo que pasa a regir la regla anterior) o por una anulación explícita con motivo. No
    se inventa la cantidad ni una confirmación de recepción.
  - `fulfilled` **no borra nada** y no se reabre: un aviso posterior crea un ciclo nuevo (§12.6, §12.7).
- **Compra todo o nada (enmienda a D-127, 2026-11-02).** La compra se registra completa o no se registra: no existe
  una compra de 6 cuando se pidieron 10. Al confirmar, cada línea lleva la cantidad pendiente completa del faltante y
  la selección entera se guarda en una sola transacción; si alcanza la cantidad pedida conocida, el faltante pasa a
  `fulfilled` y el producto queda **liberado** para un nuevo reporte de Telegram. Si la compra no se confirma, no se
  registra ni se libera nada. No se crea una solicitud duplicada porque no queda saldo de una compra parcial. Las
  **compras parciales por productos o cantidades quedan fuera de la demo** y solo se evaluarán en una versión posterior
  si Pao prueba y aprueba el sistema.
- **Compra no es recepción:** el MVP no registra llegada física; la cobertura cuenta lo comprado y nada más (§6.7).

### 12.4 Reglas de reversibilidad

1. **Buscar no cambia el estado.** Solo muestra resultados.
2. **Agregar a cotización desde una fila de faltante vincula la oferta al faltante, sin confirmación adicional.**
   Aunque sea otra marca, presentación o producto relacionado, la línea se crea y la clasificación queda visible.
   No se afirma equivalencia farmacológica.
3. Si la cantidad agregada **cubre** lo pedido, el faltante deja de estar **sin cobertura** (`pending_quantity = 0`),
   pero **sigue abierto**: la cotización cubre provisionalmente y no cierra nada (D-127).
4. Si cubre **parcialmente**, el saldo queda en `pending_quantity`; el ciclo sigue abierto igual.
5. Varios proveedores **suman** cobertura provisional.
6. **Quitar una línea** resta cobertura: si ya no alcanza, el saldo vuelve a `pending_quantity` y el estado a `pending`
   cuando no queda ninguna otra fuente. No cierra ni reabre un ciclo.
7. **Descartar un carrito** resta toda su cobertura y puede devolver el saldo a pendiente. Reincorporarlo lo vuelve
   a cubrir. Tampoco cierra nada.
8. **Cambiar la cantidad** recalcula; una cantidad menor puede devolver el saldo a pendiente y una mayor no cierra el
   ciclo: el cierre solo lo produce una compra confirmada.
9. Si otro carrito activo cubre el saldo, este deja de estar sin cobertura; si ninguno lo cubre, vuelve solo. En
   ambos casos el ciclo sigue abierto.
10. **El historial original nunca se borra**: el faltante, su mensaje, su autor y su fecha se conservan. La cobertura
    es una lectura, no una edición.
11. **Registrar una compra** cubre de inmediato: lo comprado sustituye la cantidad cotizada de esa línea y no depende
    de que la cotización siga `draft`. La compra es **todo o nada** (enmienda a D-127): si cubre **toda** la cantidad
    pedida conocida, el faltante pasa a `fulfilled` en la misma transacción y queda **liberado**; si no la cubre, no
    se cierra ni se libera. No existe el caso «queda `purchased` con el saldo» por una compra parcial. Cerrar la
    cotización **no** le quita la cobertura a la compra (P-8 resuelto).
12. **Anular o corregir una compra** recalcula: la anulación deja de cubrir; la corrección sustituye, nunca suma las
    dos. Si el faltante sigue `active` y su saldo vuelve a quedar sin compra, se muestra como pendiente de compra; un
    faltante ya `fulfilled` no se reabre (§12.3): el motivo de la anulación queda en el historial y, si el producto
    vuelve a faltar, se informa un ciclo nuevo.
13. **Comprar no es recibir:** la cobertura no cambia porque la mercadería llegue o no; el MVP no registra llegadas
    y el sistema nunca afirma que el producto fue recibido (D-126).
14. **Un faltante con compra no se vuelve a pedir, pero el saldo se compra en el mismo ciclo.** Si hay compra
    confirmada, el faltante no reaparece como necesidad nueva aunque la cotización se cierre o se descarte; cualquier
    saldo se cotiza y se compra **sobre el mismo faltante**, con `demand_item_id`, sin crear una solicitud duplicada
    (D-127). Con la compra todo o nada de la demo, ese saldo solo existe si la cantidad pedida era desconocida o si
    una corrección dejó la cobertura por debajo del objetivo. Solo una anulación o corrección que deje el total
    comprado por debajo del objetivo devuelve el saldo a pendiente.
15. **Cubrir con cotización no cierra:** el faltante queda `in_quote` mientras no haya compra confirmada, aunque el
    carrito cubra el total. Si el carrito se descarta o la cotización se cierra sin compra, la necesidad sigue ahí.

### 12.5 Campos aditivos de `GET /api/v1/demand-items`

Sin tabla ni endpoint nuevos para listar:

| Campo | Tipo | Nota |
|---|---|---|
| `last_raw_text` | text | Último texto recibido, tal como se escribió |
| `coverage_quantity` | numeric \| null | Cobertura total = cotizado sin compra + compras vigentes |
| `quoted_quantity` | numeric \| null | Parte cubierta por líneas cotizadas sin compra vigente (**provisional**) |
| `purchased_quantity` | numeric \| null | Compras `current` no anuladas vinculadas al faltante (**lo único que cierra**) |
| `pending_quantity` | numeric \| null | Saldo **pendiente de cotizar**: lo que ninguna fuente cubre (ni cotización ni compra); `null` si la cantidad pedida es desconocida |
| `pending_purchase_quantity` | numeric \| null | Saldo **pendiente de comprar**: lo que las compras confirmadas no cubren; `null` si la cantidad pedida es desconocida. Es lo que decide el cierre (D-127) |
| `is_pending` | boolean | `true` mientras el ciclo siga abierto (falta compra confirmada) o si la cantidad es desconocida; **no** indica que falte cotizar y cotizar no lo apaga |
| `replenishment_state` | string | `pending` \| `in_quote` \| `purchased` \| `fulfilled` \| `cancelled` |
| `reported_by`, `reported_by_name` | uuid \| null, text \| null | Autor del reporte independiente (D-135) |
| `declared_quantity`, `declared_unit` | numeric \| null, text \| null | Cantidad **declarada**, nunca aplicada como pedido (campo conservado por compatibilidad) |
| `requested_quantity`, `requested_unit` | numeric \| null, text \| null | **Cantidad solicitada vigente** (la última declarada y confirmada, D-147). Es distinta de `target_quantity`; compras decide esta última |
| `requested_quantity_origin` | `explicit` \| `default` \| null | Procedencia de la solicitada: escrita por la persona, 1 unidad genérica predeterminada (D-147) o histórico sin cantidad. `ObservationOut.quantity_origin` da lo mismo por aviso |
| `reported_at` | datetime | Primera observación del reporte |
| `cancelled_at`, `cancelled_by_name`, `cancellation_reason` | datetime \| null, text \| null, text \| null | Cancelación lógica, con su motivo |
| `pending_only` | filtro | `true` devuelve solo los faltantes `active` con saldo **sin cubrir** (`pending_quantity > 0`); lo ya cubierto por cotización se ve en su estado `in_quote`, no como «sin gestionar» |

Los campos son aditivos: `GET /api/v1/demand-items` conserva su forma y su paginación; no hay endpoint nuevo de
listado. **No hay campos de recepción** (`received_quantity`, `pending_receipt_quantity`): esa funcionalidad está fuera
del MVP (D-126). `fulfilled` aparece cuando las **compras confirmadas** alcanzaron la cantidad pedida conocida y se
conserva como historial; no implica que la mercadería haya llegado ni que la cotización se haya cerrado.

### 12.6 Estado de reposición (derivado)

Precedencia determinista, evaluada solo sobre faltantes `active` (los `cancelled` y `fulfilled` se consultan por
`status`):

```
if status == 'cancelled':                   -> cancelled
if status == 'fulfilled':                   -> fulfilled
if purchased_quantity > 0:                  -> purchased
if quoted_quantity > 0:                     -> in_quote
                                            -> pending
```

- `purchased`: hay **compra confirmada** que no alcanza a cubrir toda la cantidad pedida, o la cantidad pedida es
  desconocida. El ciclo sigue abierto con el saldo a la vista; **no** se presenta como recibido ni como cerrado. Con
  la compra **todo o nada** de la demo (enmienda a D-127), con cantidad pedida conocida una compra registrada cubre
  completa y cierra; `purchased` aparece cuando la cantidad es desconocida o cuando una **corrección** deja la
  cobertura por debajo del objetivo.
- `in_quote`: está cubierto por cotizaciones, sin compra confirmada. Es cobertura **provisional**: el faltante sigue
  abierto y `is_pending` sigue `true`; si la cobertura no alcanza, el saldo se muestra además en `pending_quantity`.
- `pending`: no hay ninguna cobertura vigente.
- `fulfilled`: las **compras confirmadas** alcanzaron la cantidad pedida conocida y el ciclo se cerró; conserva todo su
  historial y no vuelve a la grilla activa. **No implica recepción física.**
- **Un estado no reemplaza a otro plano:** `is_pending` responde «¿sigue abierto el ciclo?»; `replenishment_state`
  responde «¿en qué etapa del ciclo está?»; `pending_purchase_quantity` dice **cuánto** falta comprar.

**Dos saldos, dos preguntas (D-127).** `pending_quantity` responde «¿falta **cotizar**?» —lo que ninguna fuente
cubre—; `pending_purchase_quantity` responde «¿falta **comprar**?» —lo que las compras confirmadas no cubren—. Un
faltante de 10 cajas completamente cubierto por un carrito activo tiene `pending_quantity = 0`,
`pending_purchase_quantity = 10` y estado `in_quote`: cubierto provisionalmente, **no** es una solicitud sin gestionar
y **no** está cerrado. Solo la compra confirmada baja el segundo saldo y cierra cuando llega a 0.

**Compra confirmada = término del proceso del MVP y, si cubre toda la cantidad pedida, cierre del ciclo.** No hay un
estado de recepción y no se inventa uno: si la cantidad pedida se conoce y las **compras confirmadas** la alcanzan, el
faltante queda `fulfilled` y se libera; la compra es todo o nada, así que no queda un saldo nacido de una compra
parcial. Si la cantidad pedida es desconocida, el faltante no se cierra automáticamente. Ningún cierre depende de que
la mercadería llegue (D-126) y ninguno se produce por cotizar (D-127).

### 12.7 Estado por Telegram (D-123)

1. **Un aviso repetido de la misma persona no crea una segunda necesidad.** Si el producto coincide con **su** propio
   faltante `active` del mismo ciclo —pendiente, en cotización o con compra registrada—, se registra la observación en
   ese faltante y la respuesta informa el estado: pendiente, en cotización, comprado (con las cantidades compradas y,
   si el ciclo sigue abierto, el saldo por comprar) o cerrado. **Si el producto lo reportó otra persona**, no se
   adjunta: se pregunta y, si confirma, se crea un reporte propio (§10.5).
2. **Si el producto ya tiene compra registrada para esa persona**, el bot lo dice explícitamente: «ya hay una compra
   registrada» y muestra las cantidades compradas y, si el ciclo sigue abierto, cuánto falta por comprar. **No** vuelve
   a crear la necesidad, **no** suma cantidad y **no** afirma que el producto fue recibido: compra y recepción son
   cosas distintas y el MVP no registra la segunda (D-126). La compra de otra persona no cubre el reporte propio.
3. **Aviso durante una compra registrada:** el mensaje informa la cobertura y no aplica ninguna cantidad. Para pedir
   **más** cantidad hace falta un comando explícito `ADD`/`SET`/`CLEAR`, con el permiso correspondiente; nunca una
   suma implícita (AC35).
4. **Consulta de estado (D-147):** los usuarios autorizados consultan con «Ver faltantes» (o `/lista`); la respuesta
   incluye **todos los reportes activos de la farmacia** y muestra la cantidad conocida y el estado real de reposición:
   **«Pendiente de compra»** o **«🛒 En carrito»** cuando la cobertura vigente lo pone en un carrito activo. Cada fila
   indica **siempre** el auxiliar que la reportó; si dos personas reportan el mismo producto se ven dos filas, cada una
   con su nombre y su cantidad. **Representación visual:** el nombre va en negrita y, si el faltante está en un carrito
   activo, **solo el nombre** se muestra tachado (`<s>`, parse_mode HTML); la fila no se oculta ni cambia de estado, y
   el tachado depende de la **misma** cobertura que define «En carrito», por faltante y no por producto. Si hay
   ambigüedad, lista candidatos en vez de adivinar. No cierra ni modifica nada y no altera D-139: la cobertura sigue
   siendo la implementada (cotizaciones `draft`).
5. **Solo lectura:** la consulta de estado no crea, no modifica y no cierra nada. El bot **no** registra compras, **no**
   aplica cantidades y **no** confirma llegadas (§10.4).
6. **Después de `fulfilled`:** un nuevo aviso del mismo producto crea un **nuevo** faltante (nuevo ciclo), con su
   propio historial; el anterior no se reabre ni se le suma cantidad.
7. **El ciclo, no el tiempo, decide si es un aviso repetido o una necesidad nueva.** Mientras **su** faltante esté
   `active`, un aviso del mismo producto de la misma persona se registra en él; solo después de `fulfilled` (o
   `cancelled`) esa persona abre un ciclo nuevo. **Otra persona** siempre tiene su propio reporte, aunque el producto y
   el ciclo coincidan. No hay plazo, ventana de tiempo ni confirmación de recepción que intervenga.
8. **Cantidad desconocida:** el bot informa que la necesidad sigue abierta y nunca la cierra ni inventa una cantidad o
   una recepción; para cerrarla hace falta un `ADD`/`SET` explícito que declare la cantidad pedida o una anulación con
   motivo (§12.3, V-06).

---

## 13. Códigos de error

Adiciones al sobre de error de C0 (`{"error": {"code", "message", "details"?}}`); no reemplazan ninguno existente.

| HTTP | Código | Cuándo |
|---|---|---|
| 404 | `SEARCH_NOT_FOUND` | Búsqueda de cadenas inexistente, expirada o perdida |
| 409 | `QUOTE_CLOSED` | Modificar líneas o registrar compras en una cotización cerrada |
| 409 | `CART_DISMISSED` | Agregar línea a un carrito descartado |
| 409 | `LINE_EXISTS` | La misma oferta ya está activa en el carrito con **otra cantidad**; usar `PATCH` |
| 409 | `LINE_HAS_PURCHASES` | Quitar o mover una línea con compras vigentes |
| 409 | `QUANTITY_BELOW_PURCHASED` | Cantidad nueva menor que lo ya comprado |
| 409 | `ALLOCATION_EXCEEDS_LINE` | La suma de asignaciones supera la cantidad de la línea |
| 409 | `QUOTE_HAS_INCONSISTENT_ALLOCATIONS` | Cerrar una cotización con asignaciones a reportes cancelados, sin cantidad o con cantidad menor a lo asignado; `details.issues[]` |
| 409 | `QUANTITY_BELOW_ALLOCATED` | La cantidad pedida quedaría por debajo de lo asignado en cotizaciones vigentes |
| 409 | `REPORT_HAS_ALLOCATIONS` | Se intenta dejar sin cantidad un reporte con asignaciones vigentes |
| 422 | `ALLOCATION_CONFLICT` | Se envían juntos `demand_item_id` y `demand_allocations` |
| 422 | `ALLOCATION_EMPTY` / `ALLOCATION_INVALID` | Lista vacía o asignación sin reporte/cantidad válidos |
| 422 | `DUPLICATE_ALLOCATION` | El mismo reporte aparece dos veces en una línea |
| 422 | `UNIT_MISMATCH` | La unidad del reporte no coincide con la de la línea |
| 422 | `REPORT_QUANTITY_UNKNOWN` | Se intenta asignar un reporte sin cantidad pedida conocida |
| 422 | `CROSS_AUTHOR_ITEM` | Se intenta adjuntar una observación al reporte de otra persona |
| 409 | `REPORT_NOT_ACTIVE` | Cancelar un reporte ya cancelado o cumplido |
| 409 | `REPORT_HAS_PURCHASE` | Cancelar un reporte con compra registrada (C5-B) |
| 409 | `PURCHASE_ALREADY_SUPERSEDED` | Ya existe una sustitución o anulación de ese registro |
| 409 | `ACCESS_REQUEST_NOT_PENDING` | Aprobar o rechazar una solicitud ya decidida |
| 409 | `INTERPRETATION_ALREADY_REVIEWED` | Revisar una interpretación ya confirmada o rechazada |
| 422 | `OFFER_NOT_IN_SUPPLIER` | La oferta no pertenece al proveedor indicado |
| 422 | `ARTICLE_MISMATCH` | Mover a una oferta de otro artículo |
| 422 | `PURCHASE_INVALID` | Compra incoherente (moneda, líneas, importes) |
| 422 | `INVALID_ANNULMENT` | Anulación sin motivo, con líneas o sobre una compra ya anulada |
| 422 | `OBSERVATION_INVALID` | Texto vacío, fuera de 1–2000 caracteres o sin artículo |
| 403 | `WEB_ACCESS_EXPIRED` | Acceso web vencido |
| 403 | `SUPERADMIN_PROTECTED` | Una administradora de farmacia intenta desactivar o degradar a la superadministradora |
| 429 | `RATE_LIMITED` | Demasiadas búsquedas de cadenas; incluye `Retry-After` |
| 503 | `ARCHIVE_UNAVAILABLE` | Almacén de archivo caído al consultar un registro archivado |
| 503 | `ACCESS_NOT_CONFIGURED` | Google/Supabase Auth no configurado para el acceso web |

**Se reutilizan, no se duplican (F-4):**

| HTTP | Código existente | Uso en v0.2 |
|---|---|---|
| 404 | `NOT_FOUND` | Cotización, línea, compra o solicitud inexistente o de otra organización |
| 409 | `VERSION_CONFLICT` | `version` desactualizada; `details.current_version` |
| 428 | `VERSION_REQUIRED` | Falta `version` o `If-Match` |
| 422 | `INVALID_QUANTITY` | Cantidad no entera, ≤ 0 o unidad inválida (ya definido en C0) |
| 422 | `INVALID_UNIT` | Unidad no reconocida (ya definido en C0) |
| 400 | `VALIDATION_ERROR` | Cuerpo mal formado |
| 403 | `FORBIDDEN` | Rol insuficiente |
| 403 | `STRONG_AUTH_REQUIRED` | Organización real sin sesión fuerte |
| 401 | `UNAUTHENTICATED` | Sin sesión o sesión inválida |
| 403 | `USER_NOT_PROVISIONED` | JWT válido sin `app_user` enlazado |
| 409 | `IDEMPOTENCY_KEY_REUSED` | Misma clave con cuerpo distinto (E8) |
| 503 | `DATABASE_NOT_CONFIGURED` | Único 503 definido en C0 |

**Retirados de la propuesta anterior:** `QUOTE_NOT_FOUND`, `QUOTE_VERSION_CONFLICT` y `INVALID_QUANTITY` como
«nuevos». Los dos primeros duplican `NOT_FOUND` y `VERSION_CONFLICT`; el tercero ya existe en C0.

**Advertencias (no son errores):** `demand_already_purchased` (se agrega una línea a un faltante con compra vigente)
viaja en `warnings[]`; nunca bloquea ni recorta cantidades. **Los códigos de recepción** (`RECEIPTS_EXIST`,
`PURCHASE_NOT_RECEIVABLE`, `PURCHASE_ALREADY_RECEIVED`, `RECEIPT_INVALID`, `RECEIPT_LINE_MISMATCH`,
`RECEIPT_UNIT_MISMATCH`) y la advertencia `OVER_RECEIPT` **se retiran con D-126**; no existen en el MVP.

---

## 14. Invariantes que v0.2 debe respetar

| # | Invariante |
|---|---|
| V-01 | C0 no se edita; X1 y X2 son las únicas excepciones, y están nombradas |
| V-02 | E5 sigue siendo el único motor de cálculo comercial; el carrito no hace aritmética propia |
| V-03 | El precio no se copia al carrito; se recupera de `offer_snapshot`. La compra **sí** copia, por ser evidencia |
| V-04 | Nunca se reapunta una línea sola a una oferta nueva |
| V-05 | Lo parcial no tiene total; el estimado general solo existe cuando todo es completo y compatible |
| V-06 | Cantidad desconocida ≠ 0; cantidad de línea siempre explícita y entera |
| V-07 | Sin fusión automática de ofertas, dosis, formatos ni líneas |
| V-08 | Sin borrado físico: quitar es `removed`, descartar es `dismissed`, corregir es un registro nuevo |
| V-09 | Las compras son inmutables; el historial y los reportes suman solo registros `current` |
| V-10 | Lo cotizado no se asume comprado; lo comprado en exceso se advierte, no se recorta |
| V-11 | Los precios públicos de cadenas no entran en carritos, subtotales ni totales |
| V-12 | Sin archivo verificado y restaurado no se elimina nada; las cotizaciones no se archivan por antigüedad |
| V-13 | Registrar una compra no ejecuta nada: `purchase_execution` sigue `blocked` |
| V-14 | Toda escritura va en `transaction()` con auditoría en la misma transacción |
| V-15 | Ninguna operación sobre una línea altera la evidencia histórica: la compra copió sus valores al registrarse |
| V-16 | Un registro no admite dos sustituciones; solo la vigente y no anulada cuenta en cantidades, totales e historial |
| V-17 | La purga del detalle exige archivo verificado y restauración probada, y está deshabilitada hasta nueva autorización |
| V-18 | La cotización nunca se archiva ni se elimina por antigüedad |
| V-19 | Desistir del pendiente o cerrar la cotización no cambia `quantity` ni crea, edita ni anula compras: solo mueve un derivado (`remainder`) |
| V-20 | Las observaciones son independientes, inmutables y acumulativas; una preferencia puede **ordenar** las ofertas del producto exacto, pero nunca altera precios, stock, condiciones, cantidades, elegibilidad ni visibilidad |
| V-21 | La cobertura de un faltante es derivada y nunca almacenada; no se escribe en `demand_item` ni se borra el historial original de Telegram |
| V-22 | Sin equivalencia automática: `coverage_relation` queda visible; `combined` nunca se presenta como equivalente |
| V-23 | Autenticación web ≠ permiso de Telegram; ningún plano otorga el otro |
| V-24 | La superadministradora solo cruza organizaciones para metadatos administrativos, de forma auditada; los datos operativos siguen acotados |
| V-25 | La búsqueda de cadenas no persiste, no usa datos ficticios y nunca presenta un precio anterior como actual |
| V-26 | `relation` es aditivo; `match.level` se conserva y ningún resultado se oculta por su clasificación |
| V-27 | Aprobar una solicitud de acceso otorga **solo** el perfil `admin` (D-130): nunca `is_superadmin` ni acceso a Telegram |
| V-28 | La IA nunca decide dinero, cantidades, permisos, elegibilidad ni visibilidad; solo interpreta y el motor determinista ordena; la clave de OpenAI vive solo en el backend |
| V-29 | Las columnas de archivo de `purchase` solo se escriben por función restringida; el encabezado nunca se elimina |
| V-30 | Un carrito `dismissed` no se evalúa, no suma al total y no cubre faltantes |
| V-31 | No se purga nada hasta que exista archivo verificado, restauración probada y autorización explícita |
| V-32 | La cobertura suma cotizado-sin-compra y compras vigentes; una misma línea nunca se cuenta dos veces |
| V-33 | Una compra registrada no certifica la llegada física: el MVP no registra recepciones y el sistema nunca afirma que el producto fue recibido |
| V-34 | El cierre del ciclo no depende de la recepción ni de la cotización: un faltante pasa a `fulfilled` solo cuando las **compras confirmadas** cubren la cantidad pedida conocida, y nunca se cierra una cantidad desconocida por suposición |
| V-35 | No hay control de recepción, cantidades entregadas, saldos pendientes de recepción ni diferencias de entrega en el MVP (D-126) |
| V-36 | Cerrar un faltante no borra nada; un nuevo faltante del mismo producto es un ciclo nuevo con su propio historial |
| V-37 | Permisos web y autorización de Telegram son independientes; `admin` también informa si su cuenta de Telegram está autorizada, sin exclusiones |
| V-38 | Telegram nunca registra compras ni confirma llegadas; cotizar y comprar son operaciones web de `admin` + `StrongActor` (D-130) |
| V-39 | Cotizar cubre de forma provisional y nunca cierra: un faltante con el total cotizado sigue abierto en `in_quote` hasta que haya compra confirmada |
| V-40 | La compra es **todo o nada** (enmienda a D-127): se registra completa —todos los productos y cantidades seleccionados— o no se registra; no hay compras parciales por productos ni cantidades. Al registrarse, el faltante cubierto se cierra y se libera para nuevos reportes |

---

## 15. Fuera de alcance de v0.2

Aprobaciones formales, envío de órdenes, pagos, **ejecución** de compras o de órdenes de compra, sincronización con
proveedores, SSE, extracción masiva o espejo de catálogos de cadenas, captura automatizada sin autorización, cambios en
Lovable, implementación del almacenamiento de archivo, ejecución de la purga, creación de roles nuevos, registro
público o recuperación de contraseña, y cualquier decisión de dinero, cantidad o permiso tomada por IA.

**La recepción física y las diferencias de entrega quedan fuera del alcance del MVP (D-126):** no se confirma
llegada, no se registran cantidades entregadas, no hay recepciones completas ni parciales, saldos pendientes de
recepción, productos equivocados, correcciones ni anulaciones de recepción, tablas `receipt`/`receipt_line`, ni
endpoints, permisos, validaciones o pantallas asociados. Tampoco se condiciona el cierre de un faltante a la recepción.
Sigue fuera **ejecutar** una orden o modificar la compra. **Las compras parciales por productos o cantidades quedan
fuera de la demo** (enmienda a D-127, 2026-11-02): la compra se registra todo o nada. Lo que tampoco existe en el MVP
son **recepciones** parciales. **No se modifica C0, el HLD ni el mockup aprobado.**

### 15.1 Funcionalidad futura — recepción física (no es parte del MVP)

El análisis completo se conserva en el registro de decisiones (**D-122**, retirada por **D-126**): tablas `receipt` y
`receipt_line` inmutables, recepciones parciales y acumulativas, diferencias (`missing`, `wrong_product`, `damaged`,
`other`), producto equivocado con `received_quantity = 0`, saldos pendientes de recepción, cierre condicionado y sus
permisos web de `admin` + `StrongActor` (regla de D-130, aplicable si la recepción vuelve al alcance).
Permanecen abiertas como preguntas **futuras**, no como bloqueos del MVP:
anulación o corrección de una compra con recepciones, compra que nunca llega o parcial definitiva, y corrección de una
recepción errónea. Nada de esto se implementa en el MVP ni se promete en la interfaz.

---

## 16. Roadmap y migraciones (vigente; `0018` creada en código, no aplicada)

La numeración sigue el **orden de ejecución** acordado en D0 y **queda ratificada** (D-125): **C4 = 0013** y
**C3 = 0014**. Sustituye la propuesta anterior de D-112/D-114 (C3 = 0012, C4 = 0013), que no contaba con G1 ni con el
head actual del código (0011).

Las tablas de **0013** (búsqueda) y **0014** (comparación) provienen del diseño ya aprobado de C4 y C3, no de §2:
las **11 tablas** de §2 son solo las que agrega v0.2. La migración **0018** fue autorizada el 2026-11-03 **solo para
desarrollo y pruebas aisladas**; no se aplica a `farmacia` ni a `farmacia_demo`. Las demás siguen sin autorización.

**Las tres tablas de búsqueda (C4).** C0 nombra dos (`search_run`, `search_source_result`) al describir la búsqueda;
el diseño aprobado de C4 usa **tres**, y la tercera no contradice C0, lo **amplía**:

| Tabla | Qué guarda |
|---|---|
| `search_run` | La consulta, su interpretación, la cobertura y el resultado global de una corrida |
| `search_source_result` | El estado, los tiempos y los fallos **por fuente** consultada |
| `search_result` | Las filas de resultado **por producto** y su oferta, normalizadas para comparar |

`search_result` es una **ampliación aditiva** de C0 (D-125): C0 no la prohíbe, no describe su forma y no se edita.
Las tres se crean en la misma migración **0013**; ninguna se implementa en D0.

| Etapa | Contenido | Migración |
|---|---|---|
| **D0** | Contrato v0.2 rev6, D-113 y D-115 a D-128, ROADMAP | — |
| **G1** | Acceso web con Google, solicitudes, campanita con aprobación que otorga `admin`, superadministradora y vencimiento; Telegram sin cambios (D-130) | **0012** (`web_access_request` + 5 columnas de `app_user`) |
| **C4** | E1 con `relation` y `attributes.ingredients[]` (E3 conserva su forma); búsqueda de cadenas E15 (sin persistencia) | **0013** (3 tablas de búsqueda) |
| **C3** | Comparación E5 | **0014** (5 tablas) |
| **C5-A + F1** | Cotizaciones, carritos, líneas, vínculo a faltante, grilla y «Buscar»; observaciones O1 | **0015** (`quote`, `quote_cart`, `quote_line`, `commercial_observation`; 2 columnas de `quote_line`: `demand_item_id`, ya previsto en rev2, y `coverage_relation`) |
| **D-135** | Reportes independientes, cancelación lógica, confirmación en Telegram y asignaciones | **0016–0017** (columnas de reportes y `quote_line_allocation`) |
| **O2–O4 / A6** | Interpretación estructurada por producto, preferencias comerciales y orden determinista con traza interna | **0018** (`observation_interpretation`, `commercial_preference`, `preference_order_trace`; 2 columnas opcionales de `offer_snapshot`) |
| **D-144** | Retiro explícito de preferencias por clase | **0019** (`retired_subject_kinds` en `observation_interpretation` y `preference_order_trace`, con `CHECK` y guardián de inmutabilidad) |
| **C5-B** | Compras registradas, inmutabilidad, correcciones, anulación y `purchase_index`; cobertura con `closed` y compra adicional para inventario (D-139/D-140, propuesta) | **0020** (`purchase`, `purchase_line`, `purchase_index` y `purchase_line_allocation`; función de archivo). **La recepción física no forma parte del MVP (D-126).** |
| **C5-C** | Historial operativo: búsqueda por índice | **0021** (`pg_trgm`/`unaccent` e índices; verificación previa) |
| **C5-D** | Archivo externo y purga controlada: diseño cerrado, **ejecución bloqueada** | **0022** (función y permisos; requiere proveedor aprobado y pruebas) |
| **C5-E** | Enganche de navegación: Mis cotizaciones, Historial, Proveedores (E6) | — |
| **C6a** | Cadenas reales: solo con autorización A1/A2 por cadena | — |
| **T1** | Pantalla «Configuración de Telegram» (control individual) | — |

**Orden y dependencias:**

- **G1 antes de C4**: las pantallas nuevas necesitan acceso web real; G1 no depende del cotizador.
- **C4 antes de C3**: el buscador y la clasificación `relation` se necesitan para probar el flujo; E5 se implementa
  después, pero **C5-A no puede empezar sin C3**, porque la evaluación por carrito es E5.
- **C5-A antes de O1–O4**: sin observaciones no hay nada que interpretar.
- **C5-B después de C5-A**: las compras referencian líneas cotizadas.
- **C5-C y C5-D al final**: la búsqueda de historial y la purga solo tienen sentido con compras archivadas.
- **C6a y T1 no crean migraciones**; C6a depende de autorización por cadena y T1 de la API de Telegram ya existente.

**Numeración ratificada (D-125):** si se exigiera C3 = 0013 y C4 = 0014, habría que ejecutar C3 antes que C4. El
orden acordado es C4 → C3; por eso se asigna **C4 = 0013** y **C3 = 0014**, confirmado el 2026-10-30. Conservar
C3 = 0013 queda descartado porque invertiría el orden de ejecución (§17).

`farmacia` sigue en **0008** (32 tablas). El código tiene hasta **0018** (A6, fase 1), pero `0018` no está aplicada a
ninguna base; `farmacia_demo` sigue en `0015` y `0016`–`0018` no se aplican sin autorización explícita. Ninguna
migración nueva se aplica sin autorización explícita.

---

## 17. Aprobaciones y pendientes

**Aprobado conceptualmente el 2026-10-30 (D0):** D-113 y D-115 a **D-128**; cobertura reversible de faltantes sin
confirmación adicional; cobertura con compras vigentes sin doble conteo (P-8 resuelto); **recepción física fuera del
alcance del MVP** (D-126); **cierre exclusivamente por compra confirmada** que cubre la cantidad pedida, con la compra
**todo o nada** de la demo (enmienda a D-127, 2026-11-02; sin compras parciales por productos ni cantidades); estado
por Telegram sin duplicar necesidades; `admin` puede informar por Telegram si su
cuenta está autorizada; `relation` junto a `match.level`; OpenAI por Responses API desde el backend; separación de
permisos web/Telegram; superadministradora global limitada a metadatos; control individual de Telegram; **once
tablas** y nueve columnas (A6 suma dos tablas y dos columnas); numeración **C4 = 0013 / C3 = 0014** ratificada; revisión
**6** con aprobación que otorga `purchasing` y Cotizador para `admin` o `purchasing` (**D-128**, superada por la
revisión **7**: perfil único `admin`, **D-130**).

**Pendiente de ratificación:**

1. Mantener X1 y X2 como las únicas excepciones a C0-10; la recepción **no** entra como excepción porque está fuera
   del MVP (§1.1, §15).
2. Aprobar el conjunto completo de **11 tablas**, **9 columnas** y migraciones **0012–0022** (§2, §16). La parte A6
   (`0018`: `observation_interpretation`, `commercial_preference`, `preference_order_trace` y 2 columnas de
   `offer_snapshot`) ya fue aprobada el 2026-11-03, y el retiro explícito (`0019`: `retired_subject_kinds`) quedó
   aprobado por D-144. Sigue pendiente C5-B (`0020`: `purchase`, `purchase_line`,
   `purchase_index` y `purchase_line_allocation`), junto con C5-C (`0021`) y C5-D (`0022`).
3. Aprobar E10–E15 y los códigos nuevos de §13 como adición a C0.
4. Confirmar el alcance de la superadministradora: **solo metadatos administrativos**; ampliar a datos operativos
   exige decisión nueva (§9.7).
5. Confirmar que el control individual de Telegram exige cuentas `personal`; en `shared_device` el bloqueo afecta a
   todas las personas del teléfono (§10.4).
6. Confirmar la regla de cierre (D-127, con su enmienda del 2026-11-02): un faltante `active` con cantidad pedida
   **conocida** pasa a `fulfilled` solo cuando las **compras confirmadas** la alcanzan; cubrir con cotización es
   provisional y **no** cierra; la compra se registra **todo o nada** y al registrarse cierra y libera el faltante;
   con cantidad desconocida no se cierra automáticamente (§12.3, §12.6).

**Bloqueos externos, sin aprobar:**

7. **Captura por cadena:** A1 (autorización escrita/API) o A2 (decisión de riesgo registrada) para Farmacias Ahumada;
   ninguna cadena está habilitada hoy (§11.6, §11.7).
8. **OpenAI:** la clave ya existe en `backend/.env`; el adaptador HTTP ya está implementado sin SDK (D-145), pero
   falta definir el **modelo** (`OPENAI_MODEL`, sin valor por defecto) y hacer una prueba real autorizada antes de
   activar llamadas. El **presupuesto mensual, la tarifa por token, la auditoría financiera y los
   reportes de costo quedan diferidos** y no bloquean la demo; se mantiene una protección básica contra llamadas
   excesivas. Sin configuración completa, A6 se deshabilita y O1 sigue operativo (§8.5).
9. **Archivo externo:** proveedor, costos, credenciales y política de retención. Sin ellos, C5-D sigue bloqueado
   (§7.2, §7.4).
10. **`pg_trgm`/`unaccent`:** verificar disponibilidad en el PostgreSQL de destino y autorizar su instalación antes de
    C5-C (§7.3).
11. **`farmacia`:** permanece en 0008; no se aplican migraciones nuevas sin autorización explícita.

**Funcionalidad futura, no bloqueante (no es parte del MVP ni de D0):**

12. **Recepción física y diferencias de entrega (D-122, retirada por D-126).** El diseño queda conservado para una
    fase futura; no condiciona el cierre del faltante, no crea tablas, endpoints, permisos ni pantallas en el MVP.
13. **Preguntas futuras de recepción:** anulación o corrección de una compra con recepciones (P-9), compra que nunca
    llega o parcial definitiva (P-10) y corrección de una recepción errónea (P-11). Dejan de ser bloqueos de D0 y de
    la demo; se retoman solo si la recepción vuelve al alcance.

**Fase documental (2026-10-30, D-128):** `CLAUDE.md`, `docs/AUTH_SUPABASE.md` y `docs/ROADMAP.md` quedan alineados con
la revisión 6, D-128 y el cierre de D0. **D-107** sigue con la fila «Propuesta» aunque el código 0009–0011 ya implementa
el perfil y las solicitudes; se anota, no se reescribe.

**Corrección de alcance (2026-11-02, enmienda a D-127):** la demo registra compras **todo o nada**, sin compras
parciales por productos ni cantidades. Se ajustan §4.2, §6.1, §6.5, §6.7, §10.5, §12.3, §12.4, §12.6, §12.7, §15,
V-40 y este §17; `CLAUDE.md` y `docs/ROADMAP.md` se alinean en la misma fecha. **No hubo cambios de código, esquema ni
migraciones**, porque C5-B aún no se implementa.