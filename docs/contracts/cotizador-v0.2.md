# Contrato del cotizador — v0.2 (cotizaciones, compras, historial, observaciones, accesos y precios públicos)

**Estado: BORRADOR PARA REVISIÓN, 2026-10-30 (revisión 5).** No aprobado, no implementado. Este documento **no
modifica C0** (`docs/contracts/cotizador-v0.md`, congelado por D-093): lo **amplía** y declara cada ampliación de forma
explícita. Mientras no se apruebe, nada de lo aquí escrito está permitido en el sistema.

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

**Cierre del ciclo (D-127, 2026-10-30):** la cobertura por **cotización** es **provisional** y nunca cierra; solo la
**compra confirmada** cierra, y solo cuando cubre **toda** la cantidad pedida conocida. Una compra **parcial** deja el
mismo ciclo abierto con el saldo visible, sin crear una necesidad duplicada. `fulfilled` no significa recibido.

**Decisión que lo respalda:** D-114, con las enmiendas de D0 registradas en la misma sección de decisiones.
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
  libre de observaciones y produce una recomendación explicada (§8).
- La compra real sigue **bloqueada**; registrar una compra no la ejecuta ni la autoriza.

### 1.3 Cuatro planos separados

| Plano | Quién lo produce | Dónde vive |
|---|---|---|
| **Cálculo comercial** | E5 determinista | Se calcula en vivo; no se guarda |
| **Selección** | Pao (persona) | `quote_line` (oferta + cantidad) |
| **Hecho de compra** | Registro manual posterior | `purchase` + `purchase_line` (inmutable) |
| **Observación y consejo** | Pao escribe; la IA solo sugiere | `commercial_observation` + `observation_interpretation` |

Ningún plano escribe en otro. El carrito no copia precios; la compra copia todo porque debe sobrevivir sola; una
observación **nunca** altera precios, stock, condiciones, cantidades ni el orden de las ofertas. **Compra no es
recepción** (V-33): el MVP no registra llegada física; una compra confirmada marca el término del proceso
administrado, no la entrada de mercadería (§4.2, §12.6).

### 1.4 Ampliaciones aditivas de las revisiones 3 y 5

| # | Ampliación | Superficie | Decisión |
|---|---|---|---|
| A1 | Cotizaciones, carritos y líneas | E10 | D-114 |
| A2 | Compras registradas | E11 | D-114 |
| A3 | Historial comercial | E12 | D-114 |
| A4 | Observaciones comerciales e interpretación con IA | E13 | D-116 |
| A5 | Acceso web con Google, solicitudes y superadministradora | E14 | D-117, D-119 |
| A6 | Control individual de Telegram | T1 (reusa API existente) | D-118 |
| A7 | Búsqueda de precios públicos de cadenas | E15 | D-113, D-115 |
| A8 | `relation` en E1 y `coverage_relation` en E10 | Campo aditivo + columna | D-120 |
| A9 | Cobertura de faltantes con compras vigentes, sin doble conteo | E10, campos aditivos de `GET /demand-items` | D-121, cierre por D-127 |
| A10 | ~~Recepción física, parcial y con diferencias~~ | **Retirada del MVP** | D-122, retirada por **D-126** |
| A11 | Estado por Telegram, sin duplicar necesidades; `admin` puede informar | Mensajes del bot + permiso | D-123, D-124 |

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
| **Cotizador (E10–E12)** | rol `admin` **y** `StrongActor` | Nada fuera de su organización |
| **Informar faltantes (Telegram)** | `telegram_account.status = active` + identidad vigente + `REPORT_SHORTAGE` | Acceso web; el rol `admin` **no** habilita el bot por sí solo |

**Sin permisos de recepción (D-126).** El MVP no tiene operación de recepción física, así que no crea permiso, pantalla
ni endpoint para confirmarla. Cotizar y confirmar compras siguen siendo operaciones web de `admin` + `StrongActor`.

Una persona puede tener Telegram sin web y web sin Telegram. Bloquear Telegram no revoca la web; revocar la web no
bloquea Telegram. La **desactivación de la persona** (`app_user.active = false`) es una suspensión más fuerte y corta
ambos planos. El token de Telegram nunca se expone en la API web.

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

**v0.2 completa 9 tablas:** las 6 de la revisión 2 (`quote`, `quote_cart`, `quote_line`, `purchase`, `purchase_line`,
`purchase_index`) y 3 de la revisión 3 (`commercial_observation`, `observation_interpretation`,
`web_access_request`). **Columnas nuevas respecto del esquema actual: 7**: `quote_line.demand_item_id` (ya prevista en
la revisión 2), `quote_line.coverage_relation` (nueva en la revisión 3) y cinco en `app_user` (§2.10). **6 son nuevas
en la revisión 3; las revisiones 4 y 5 no agregan columnas.** La revisión 5 no agrega tablas: retira las dos de
recepción que proponía la revisión 4 (D-126).

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

### 2.8 `observation_interpretation` — interpretación sugerida por IA

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid PK | |
| `organization_id`, `observation_id` | uuid | FK compuesta; **varias interpretaciones por observación** |
| `stance` | varchar(10) | `favor` \| `caution` \| `neutral` |
| `subject_kind` | varchar(12) | `article` \| `brand` \| `supplier` |
| `subject_label` | text | Etiqueta **tal como apareció** en la observación |
| `temporality` | varchar(10) | `past` \| `present` \| `unknown` |
| `confidence` | numeric(4,3) | 0–1; sugerencia, no verdad |
| `model`, `prompt_version` | text | Trazabilidad del proveedor |
| `input_tokens`, `output_tokens` | integer | Consumo real informado por el proveedor |
| `estimated_cost_usd` | numeric(12,6) | Calculado con tarifa configurable; **no es un cobro** |
| `latency_ms` | integer | |
| `data_origin` | varchar(12) | `ai_derived` |
| `status` | varchar(10) | `suggested` \| `confirmed` \| `rejected` |
| `reviewed_by`, `reviewed_at`, `review_note` | | Solo tras revisión humana |
| `created_at` | timestamptz | |

**Contradicción resuelta (D0).** La tabla se describía como inmutable pero tenía `status` mutable. Regla final:

- **El contenido es inmutable**: `stance`, `subject_*`, `temporality`, `confidence`, `model`, `prompt_version`,
  tokens, costo y latencia no se editan ni se borran.
- Solo los **campos de revisión** (`status`, `reviewed_by`, `reviewed_at`, `review_note`) cambian, mediante una
  función restringida y auditada con transición **`suggested → confirmed | rejected`, sin retorno**.
- Varias interpretaciones por observación son válidas (otro modelo, otra versión de prompt). **La más reciente
  revisada gana**; las anteriores se muestran como «superadas», nunca se ocultan ni se borran.

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
aprobación habilita, y la aprobación **no asigna roles** (§9.5).

### 2.10 Columnas nuevas en `app_user`

| Columna | Tipo | Notas |
|---|---|---|
| `email` | varchar NULL | Desde el claim verificado; no se usa como identidad |
| `is_superadmin` | boolean, def. `false` | **No es un rol**; `roles` conserva su CHECK cerrado |
| `web_access_expires_at` | timestamptz NULL | NULL = sin vencimiento; se evalúa en cada petición |
| `web_access_granted_by`, `web_access_granted_at` | | Trazabilidad del último otorgamiento |

`roles` sigue siendo `ARRAY['auxiliary','purchasing','approver','admin']::text[]` con su CHECK. **No se agrega
`superadmin` a `roles`**: sería ambiguo con el modelo de permisos existente (D-119).

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
| Consejo de una línea | observaciones del artículo + interpretaciones revisadas | `advice` (§8.4) |

- `exceeded`: se compró más de lo cotizado. **No se bloquea**; se advierte y se conservan ambas cantidades (§6.4).
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

Todas las operaciones: **rol `admin` + `StrongActor`**, conforme a C0 §6 y C0-09 (D-079/D-086). Cualquier otro rol
recibe `403 FORBIDDEN`; en organizaciones reales sin sesión fuerte, `403 STRONG_AUTH_REQUIRED`. La organización sale
del `Actor`, nunca del cuerpo. **No se amplían permisos a auxiliares en esta etapa.** Importes siempre como string +
moneda (C0 §2.2).

### 5.1 Cotizaciones

| Método y ruta | Para qué |
|---|---|
| `POST /api/v1/quotes` | Crear cotización. Cuerpo: `{ "title": "Reposición semanal" }` |
| `GET /api/v1/quotes` | «Mis cotizaciones». Filtros: `status`, `updated_from`, `updated_to`, `q` (título), paginación |
| `GET /api/v1/quotes/{quote_id}` | Detalle completo: carritos, líneas, evaluación E5 por carrito, total estimado, cobertura de faltantes y `advice` |
| `GET /api/v1/quotes/{quote_id}/summary` | Sección integrada del cotizador: por proveedor, nº de productos, subtotal o causa, estado. Sin detalle de líneas |
| `PATCH /api/v1/quotes/{quote_id}` | `title`, `status: "closed"` o `"draft"` (reabrir). Requiere `version` |

`GET /quotes/{id}/summary` responde en una sola llamada, pensado para refrescarse mientras Pao sigue buscando.

### 5.2 Agregar producto (sin `cart_id`)

`POST /api/v1/quotes/{quote_id}/lines`

```json
{
  "supplier_id": "uuid",
  "offer_snapshot_id": "uuid",
  "quantity": { "value": "20", "unit": "box" },
  "demand_item_id": "uuid | null"
}
```

- Crea el carrito del proveedor si no existe. Si existe y está `dismissed`, responde 409 `CART_DISMISSED`
  (hay que reincorporarlo explícitamente: no se resucita por accidente).
- Valida: la oferta pertenece al proveedor (`422 OFFER_NOT_IN_SUPPLIER`), a la organización (404), la cotización está
  `draft` (`409 QUOTE_CLOSED`), la cantidad es entera > 0 (`422 INVALID_QUANTITY`).
- **Si viene `demand_item_id`:** valida que el faltante pertenezca a la organización y esté vigente; calcula
  `relation` con la misma clasificación de E1 (§12) y la guarda como `coverage_relation` **congelada**. Si no hay
  `relation` calculable (por ejemplo, una ficha de cadena sin artículo), guarda `needs_review`. No se pide una
  segunda confirmación: «Agregar a cotización» desde una fila de faltante ya es la acción explícita. La clasificación
  y las advertencias quedan visibles en la línea y en el resumen.
- **Si no viene `demand_item_id`:** la línea no cubre ningún faltante, aunque el producto coincida. Una búsqueda
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
| `PATCH /quotes/{quote_id}/lines/{line_id}` | SET explícito de cantidad + `version`. No hay suma implícita: repetir el aviso no acumula. Si ya hay compras vigentes, la nueva cantidad no puede ser menor que lo comprado → `409 QUANTITY_BELOW_PURCHASED` |
| `DELETE /quotes/{quote_id}/lines/{line_id}` | Quita lógico (`removed`) con `removed_by`/`removed_at`. Solo si **no hay compras vigentes** → si las hay, `409 LINE_HAS_PURCHASES`; lo pendiente se cierra con `close-pending` |
| `POST /quotes/{quote_id}/lines/{line_id}/close-pending` | **Desistir del pendiente**: marca `pending_closed_at`. No cambia `quantity`, no toca la compra histórica y no borra la línea. La cantidad cotizada original queda visible |
| `POST /quotes/{quote_id}/lines/{line_id}/reopen-pending` | Deshace el cierre del pendiente mientras la cotización esté `draft` |
| `POST /quotes/{quote_id}/lines/{line_id}/move` | `{ "target_offer_snapshot_id": "uuid", "quantity": {...} }`. Exige oferta destino **explícita**, del **mismo artículo** (`422 ARTICLE_MISMATCH` si no) y solo si **no hay compras vigentes** (`409 LINE_HAS_PURCHASES`). Atómico: quita + agrega en una transacción y un solo evento de auditoría con antes/después. Crea el carrito destino si hace falta. **No arrastra `demand_item_id` ni `coverage_relation`**: la nueva línea nace sin vínculo salvo que la petición lo declare |
| `POST /quotes/{quote_id}/lines/{line_id}/refresh` | Acepta el precio actual; reapunta al snapshot nuevo. Auditado. **No toca compras**; si la línea está comprada por completo, responde con advertencia de que no tiene efecto sobre lo pendiente |

**Repartir cantidades**: no es una operación nueva. Pao agrega la misma referencia en el carrito de otro proveedor
(creando otra línea). El resumen muestra, por artículo, la cantidad distribuida y la pendiente.

**Quitar una línea con vínculo a faltante** recalcula la cobertura (§12.3): si con las líneas restantes ya no se
alcanza la cantidad pedida, el saldo vuelve a `pending_quantity`. La cotización es cobertura provisional: quitarla no
cierra ni reabre el ciclo, que solo se cierra con compra confirmada (D-127). No se borra ninguna observación ni el
historial original de Telegram.

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

Permisos: **rol `admin` + `StrongActor`** (§5), sin excepciones para auxiliares.

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
- **Cierre del faltante en la misma transacción (D-127).** Si las líneas registradas llevan `demand_item_id` y la suma
  de compras confirmadas de ese faltante alcanza su cantidad pedida conocida, la compra lo pasa a `fulfilled` (§12.3).
  Si no la alcanza, el faltante sigue `purchased` con el saldo, y esa misma compra puede completarse más adelante con
  una compra posterior sobre el **mismo** faltante, sin crear otra solicitud. La respuesta incluye el faltante
  afectado y su `replenishment_state` resultante.

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
**compras confirmadas** cubran toda la cantidad pedida conocida; una compra parcial mantiene el faltante abierto con el
saldo, aunque la cotización se cierre (D-127).

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
  (§12.3, §12.6); no se espera ninguna confirmación de entrega para cerrarlo. Una compra **parcial** deja el ciclo
  abierto con el saldo, sin duplicar la necesidad (D-127).
- La compra sigue siendo **inmutable** y su anulación o corrección recalcula la cobertura (§6.2, §12.4).
- El diseño completo de recepción (tablas, endpoints, reglas de diferencias y cierre) queda registrado como
  **funcionalidad futura** en el registro de decisiones (**D-122**, retirada por **D-126**) y en §15; no forma parte de
  este contrato implementable.

## 7. E12 — Historial de cotizaciones

Permisos: **rol `admin` + `StrongActor`** (§5). Leer historial archivado no cambia el registro ni reintroduce datos.
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
  crearse «sin migración». Requieren su **propia migración** (C5-C, 0018) y la verificación previa de disponibilidad
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

Pao escribe observaciones libres sobre un producto, una marca o un proveedor («este medicamento no me gusta por el
laboratorio», «este proveedor despacha tarde»). El sistema las **guarda siempre** y la IA **solo sugiere** una
interpretación y un consejo. **La observación es de Pao; el consejo es una sugerencia marcada como tal.**

No se reemplaza el aprendizaje por un conjunto de reglas presentado como IA. Si no hay proveedor de IA configurado o
falla, la observación se guarda igual y el consejo dice **«sin interpretación»**; no se inventa una recomendación ni
se simula una interpretación real.

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
| `POST /api/v1/observations` | Registrar observación **sin línea**, desde la tarjeta de una oferta o una ficha: `{ "text": "...", "trade_item_id": "uuid", "supplier_id": "uuid?", "offer_snapshot_id": "uuid?", "quote_id": "uuid?" }`. Es la ruta que usa el botón «Observación» del buscador. Responde `201` |
| `POST /api/v1/quotes/{quote_id}/lines/{line_id}/observations` | Atajo para registrar la observación ya asociada a una línea de cotización. Cuerpo: `{ "text": "..." }`. Sin categorías ni selectores. Responde `201` con la observación y, si la IA está habilitada, con `interpretation_status` |
| `GET /api/v1/quotes/{quote_id}/lines/{line_id}/observations` | Observaciones de la línea, más recientes primero, con sus interpretaciones y estado de revisión |
| `GET /api/v1/observations?trade_item_id=...` | Historial de observaciones **del artículo exacto**, para la vista de producto |
| `POST /api/v1/observation-interpretations/{id}/review` | Revisión humana: `{ "decision": "confirm" \| "reject", "note": "..." }`. Solo `admin` + `StrongActor`. Transición sin retorno; auditada |
| `GET /api/v1/quotes/{quote_id}` | Incluye `advice` por línea (§8.4) |

La interfaz **no cambia**: botón «Observación», campo de texto libre, Guardar/Cancelar. **Sin** categorías,
selectores, etiquetas predefinidas ni formularios adicionales.

### 8.4 O1–O4

| Etapa | Qué hace | Dónde |
|---|---|---|
| **O1 — Persistencia** | Guardar la observación con autor, fecha, texto original y contexto; mostrarla siempre | C5-A, migración 0015 (`commercial_observation`) |
| **O2 — Interpretación** | Enviar el texto a un modelo y guardar una interpretación estructurada como sugerencia | O2–O4, migración 0016 (`observation_interpretation`) |
| **O3 — Recuperación y precedencia** | Para un artículo, recuperar **solo** sus observaciones (nunca por nombre, marca o laboratorio parecido); la más reciente para el mismo sujeto y clase de consejo gana; las anteriores se muestran como superadas | En vivo, sin tabla nueva |
| **O4 — Consejo explicado** | Devolver un objeto `advice` que cita las observaciones en que se basa y las que supera | En vivo, dentro de la cotización |

**Precedencia (O3), exacta:**

1. Solo observaciones con `trade_item_id` **exacto** al artículo consultado.
2. Para el mismo `subject_kind` y la misma clase de consejo, **gana la más reciente** (`created_at`, desempate por
   `id`).
3. Las anteriores **no se borran**: se devuelven en `supersedes[]` y se muestran como «superada por la observación
   del …».
4. Una observación de marca o proveedor **no se aplica** a otro artículo de la misma marca.
5. Si no hay observaciones, `advice` es `null`; no se rellena con texto genérico.

**Objeto `advice`:**

```json
{
  "advice": {
    "stance": "caution",
    "text": "La observación más reciente sobre este artículo advierte sobre el laboratorio.",
    "based_on": [
      { "observation_id": "uuid", "created_at": "2026-10-29T10:00:00Z", "quote_id": "uuid" }
    ],
    "supersedes": [
      { "observation_id": "uuid", "created_at": "2026-09-15T10:00:00Z" }
    ],
    "interpretation_status": "confirmed"
  }
}
```

- `stance`: `favor` \| `caution` \| `neutral`.
- `text`: redactado a partir de las observaciones citadas; **no** afirma un hecho objetivo ni una equivalencia.
- `based_on` y `supersedes` permiten auditar el consejo sin abrir la IA.
- `interpretation_status`: `suggested` \| `confirmed` \| `rejected` \| `sin_interpretacion`.
- `advice` **no** es un criterio comercial de E9, **no** entra en E5 y **no** altera precios, stock, condiciones,
  cantidades, elegibilidad ni el orden de las ofertas. Es información adicional, visible y explicable.

### 8.5 Integración con OpenAI

**Responses API, invocada solo desde el backend.** Lovable nunca ve la clave ni llama al proveedor.

| Configuración | Tipo | Regla |
|---|---|---|
| `OPENAI_API_KEY` | `SecretStr` | Obligatoria para O2–O4; si falta, O1 sigue operativo y el consejo dice «sin interpretación» |
| `OPENAI_MODEL` | texto | **Sin valor por defecto inventado**; si está vacío, O2–O4 quedan deshabilitados |
| `OBSERVATION_AI_ENABLED` | booleano | Interruptor de despliegue; por defecto `false` |
| `OPENAI_MAX_OUTPUT_TOKENS` | entero | Cota por solicitud |
| `OPENAI_TIMEOUT_SECONDS` | entero | Tiempo máximo; al vencer, la observación se guarda sin interpretación |
| `OPENAI_MONTHLY_BUDGET_USD` | decimal | Tope de gasto mensual; al alcanzarlo se deja de llamar y se informa |
| `OPENAI_PROMPT_VERSION` | texto | Versión del prompt, guardada en cada interpretación |
| `OPENAI_INPUT_USD_PER_1M`, `OPENAI_OUTPUT_USD_PER_1M` | decimal | Tarifa configurable para **estimar** el costo; no se hardcodea un precio |

Reglas de llamada:

1. **Salida estructurada** validada contra un JSON Schema estricto (Pydantic). Si no valida, se descarta y se registra
   el fallo; no se guarda una interpretación inventada.
2. Se guardan `model`, `prompt_version`, tokens de entrada/salida, costo estimado y latencia (§2.8).
3. **Timeout y reintento acotado** (máximo un reintento). Sin bucles ni llamadas en segundo plano.
4. **Presupuesto mensual y tope por lote**: antes de cada llamada se verifica el acumulado; si se agotó, no se llama
   y el consejo queda «sin interpretación».
5. **Fallo del proveedor**: la observación se guarda igual; la respuesta incluye `advice_status: "sin_interpretacion"`
   y un motivo visible; nunca se sustituye por una regla fingida.
6. **La clave vive en `backend/.env`** (fuera de git), la carga `core/config.py` y **nunca** se escribe en logs,
   respuestas, auditoría ni frontend. Los errores del proveedor se registran sin la clave ni el texto completo.
7. **Costo y proveedor no están aprobados todavía** (§17). Sin autorización, O2–O4 no se activan.

### 8.6 Prohibiciones de la IA

La interpretación **no puede**:

- modificar precio, stock, condiciones, cantidad, moneda, totales ni elegibilidad;
- crear, quitar, ocultar, reordenar o destacar ofertas;
- otorgar o quitar permisos, roles o accesos;
- afirmar equivalencia farmacológica, dosis equivalente o sustitución;
- aplicar una observación a otro artículo por parecido de nombre, marca o laboratorio;
- borrar, editar o «resumir definitivamente» una observación.

### 8.7 Aceptación de la demostración

1. Dos observaciones contradictorias sobre el mismo artículo, en fechas distintas.
2. La cotización posterior muestra un consejo que cita **la más reciente** y marca la anterior como **superada**, sin
   borrarla.
3. El consejo explica el motivo con el **texto original visible**.
4. La cotización devuelve **exactamente las mismas ofertas, precios, stock y totales** que sin observaciones.
5. Una observación sobre un artículo **no** se aplica a otro artículo de la misma marca.
6. Si la IA está deshabilitada o falla, la observación sigue guardada y el consejo dice «sin interpretación».

---

## 9. E14 — Acceso web con Google

### 9.1 Autenticación ≠ autorización

Google/Supabase **autentica** (quién es). Nuestra base **autoriza** (qué puede hacer). Un JWT válido no basta: debe
existir un `app_user` activo con `auth_subject` enlazado y acceso vigente.

### 9.2 Tres niveles

| Nivel | Quién | Puede | No puede |
|---|---|---|---|
| **Superadministradora** | Yarella Fernández | Administración global del sistema, organizaciones, administradoras y permisos globales | Ser revocada o degradada por una administradora de farmacia |
| **Administradora de farmacia** | Pao, Danny y Julio | Gestionar usuarios autorizados de **su** farmacia; aprobar, rechazar y revocar accesos; usar el cotizador (cotizar y confirmar compras) | Privilegios globales; otras organizaciones; registrar recepciones (fuera del MVP) |
| **Usuario autorizado** | Personas con Google | Solo las funciones permitidas por sus roles | Administración automática por tener cuenta Google |

Los nombres son personas, no código. Los roles siguen siendo los cuatro existentes; **no se crea un rol nuevo**.

### 9.3 Flujo

1. «Continuar con Google» → Supabase devuelve un JWT.
2. El backend valida firma, emisor, audiencia y expiración con el validador existente.
3. Busca `app_user.auth_subject == sub`, `active = true` y `web_access_expires_at` vigente.
4. Si existe y está vigente → entra con sus roles.
5. Si no existe o no está enlazado → `403 USER_NOT_PROVISIONED`; la pantalla ofrece **«Solicitar acceso»**.
   Si Google/Supabase Auth no está configurado → `503 ACCESS_NOT_CONFIGURED`.
6. `POST /api/v1/web-access-requests` crea una solicitud `pending` en la organización **configurada en el servidor**.
7. La campanita de Pao la muestra. Aprobar o rechazar es de un `admin` de esa organización.
8. **Aprobar no asigna roles.** Crea o enlaza el `app_user` con `roles = []` y `active = true`; el acceso queda
   limitado a lo que un usuario sin roles puede ver. Los roles se otorgan después, por la superadministradora.
   Si el nombre visible ya existe activo en la organización, la aprobación responde `409` y se pide renombrar;
   **nunca se vincula por nombre ni se sobrescribe** una persona existente.

**Pao, Danny y Julio se habilitan previamente** con `create-admin` / `link-auth-subject` (D-071/D-072) y entran con
Google sin pasar por la campanita. La campanita es para solicitudes nuevas o excepcionales.

### 9.4 Endpoints

| Método y ruta | Quién | Para qué |
|---|---|---|
| `POST /api/v1/web-access-requests` | JWT válido, sin `app_user` | Crear solicitud. Cuerpo `{}`; organización desde configuración del servidor. Idempotente por `(organization_id, auth_subject)` pendiente |
| `GET /api/v1/web-access-requests` | `admin` de la organización **o** superadministradora | `{ "pending_count": 1, "items": [{ "id", "email", "requested_at", "organization"? }] }`; `organization` solo para la superadministradora |
| `POST /api/v1/web-access-requests/{id}/approve` | `admin` de la organización | Aprueba; crea/enlaza `app_user` con `roles = []`, `active = true`. Responde 200 |
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
  confirmar compras son datos operativos: solo un `admin` de la organización dueña puede hacerlo. Su capacidad de
  informar por Telegram existe **solo** si tiene una cuenta de Telegram autorizada en esa organización.

### 9.8 Vencimiento y revocación

- `web_access_expires_at` se evalúa en **cada** petición; si venció → `403 WEB_ACCESS_EXPIRED`. No hay job en
  segundo plano.
- Revocar la web no borra la persona, no borra su historial y no bloquea Telegram.
- Desactivar la persona (`active = false`) es una suspensión más fuerte: corta web y Telegram.

### 9.9 Sin contraseñas ni registro público

No hay recuperación de contraseña ni registro público (D-066). El alta es por Google + solicitud aprobada, o por
`create-admin` / `link-auth-subject` para las personas ya previstas.

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
line_cover(l) =
    CASE
      WHEN EXISTS (purchase_line pl vigente de l) THEN SUM(pl.quantity de compras current no anuladas)
      WHEN l.status = 'active' AND c.status = 'active' AND q.status = 'draft' THEN l.quantity
      ELSE 0
    END

covered(item) = SUM(line_cover(l))
  FROM quote_line l
  JOIN quote_cart c ON c.id = l.quote_cart_id
  JOIN quote q      ON q.id = c.quote_id
 WHERE l.demand_item_id = item.id
   AND l.organization_id = item.organization_id

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

**Por qué no hay doble conteo.** `purchase_line.quote_line_id` apunta a la línea cotizada. Si esa línea tiene compra
vigente, `line_cover` toma **solo** la cantidad comprada; la cantidad cotizada deja de sumarse. Si la compra se
corrige, solo cuenta el registro vigente; si se anula, la línea vuelve a su cantidad cotizada **solo si sigue activa
en un carrito activo de una cotización `draft`**; si la cotización ya se cerró, esa línea no aporta cobertura y el
saldo queda sin cubrir. Una compra registrada **sin línea cotizada** (`quote_line_id = null`) no cubre ningún
faltante, porque no tiene vínculo con el aviso.

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
- **Compra parcial (D-127).** Si se pidieron 10 y se compraron 6, las 6 quedan cubiertas y las 4 restantes siguen
  siendo necesidad abierta **en el mismo faltante**: `purchased_quantity = 6`, `pending_purchase_quantity = 4`,
  `is_pending = true`, estado `purchased`. Cotizar y comprar esas 4 no crea otra solicitud: se agregan con
  `demand_item_id` al mismo faltante y, al cubrirse las 10, el faltante cierra.
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
    de que la cotización siga `draft`. Si la compra confirmada cubre **toda** la cantidad pedida conocida, el faltante
    pasa a `fulfilled` en la misma transacción; si cubre una parte, queda `purchased` con el saldo. Cerrar la
    cotización **no** le quita la cobertura a la compra (P-8 resuelto).
12. **Anular o corregir una compra** recalcula: la anulación deja de cubrir; la corrección sustituye, nunca suma las
    dos. Si el faltante sigue `active` y su saldo vuelve a quedar sin compra, se muestra como pendiente de compra; un
    faltante ya `fulfilled` no se reabre (§12.3): el motivo de la anulación queda en el historial y, si el producto
    vuelve a faltar, se informa un ciclo nuevo.
13. **Comprar no es recibir:** la cobertura no cambia porque la mercadería llegue o no; el MVP no registra llegadas
    y el sistema nunca afirma que el producto fue recibido (D-126).
14. **Un faltante con compra no se vuelve a pedir, pero el saldo se compra en el mismo ciclo.** Si hay compra
    confirmada, el faltante no reaparece como necesidad nueva aunque la cotización se cierre o se descarte; el saldo
    se cotiza y se compra **sobre el mismo faltante**, con `demand_item_id`, sin crear una solicitud duplicada
    (D-127). Solo una anulación o corrección que deje el total comprado por debajo del objetivo devuelve el saldo a
    pendiente.
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
  desconocida. El ciclo sigue abierto con el saldo a la vista; **no** se presenta como recibido ni como cerrado. El
  saldo se cotiza y se compra sobre este mismo faltante, sin duplicar la necesidad (D-127).
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
faltante queda `fulfilled`; si cubren una parte, sigue `purchased` con el saldo visible y el mismo ciclo abierto.
Ningún cierre depende de que la mercadería llegue (D-126) y ninguno se produce por cotizar (D-127).

### 12.7 Estado por Telegram (D-123)

1. **Un aviso repetido no crea una segunda necesidad.** Si el producto coincide con un faltante `active` del mismo
   ciclo —pendiente, en cotización o con compra parcial—, se registra la observación en ese faltante y la respuesta
   informa el estado: pendiente, en cotización, comprado (con las cantidades compradas y, si no alcanzan, el saldo por
   comprar) o cerrado.
2. **Si el producto ya tiene compra registrada**, el bot lo dice explícitamente: «ya hay una compra registrada» y
   muestra las cantidades compradas y, si el ciclo sigue abierto, cuánto falta por comprar. **No** vuelve a crear la
   necesidad, **no** suma cantidad y **no** afirma que el producto fue recibido: compra y recepción son cosas
   distintas y el MVP no registra la segunda (D-126).
3. **Aviso durante una compra registrada:** el mensaje informa la cobertura y no aplica ninguna cantidad. Para pedir
   **más** cantidad hace falta un comando explícito `ADD`/`SET`/`CLEAR`, con el permiso correspondiente; nunca una
   suma implícita (AC35).
4. **Consulta de estado:** los usuarios autorizados pueden consultar con `estado <texto>` (determinista, sin IA) o
   desde «Ver lista». La respuesta muestra el estado de reposición y las cantidades conocidas; si hay ambigüedad,
   lista candidatos en vez de adivinar.
5. **Solo lectura:** la consulta de estado no crea, no modifica y no cierra nada. El bot **no** registra compras, **no**
   aplica cantidades y **no** confirma llegadas (§10.4).
6. **Después de `fulfilled`:** un nuevo aviso del mismo producto crea un **nuevo** faltante (nuevo ciclo), con su
   propio historial; el anterior no se reabre ni se le suma cantidad.
7. **El ciclo, no el tiempo, decide si es un aviso repetido o una necesidad nueva.** Mientras el faltante esté
   `active`, todo aviso del mismo producto se registra en él; solo después de `fulfilled` (o `cancelled`) un aviso
   abre un ciclo nuevo. No hay plazo, ventana de tiempo ni confirmación de recepción que intervenga.
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
| V-20 | Las observaciones son independientes, inmutables y acumulativas; el consejo nunca altera precios, stock, condiciones, cantidades, elegibilidad ni visibilidad |
| V-21 | La cobertura de un faltante es derivada y nunca almacenada; no se escribe en `demand_item` ni se borra el historial original de Telegram |
| V-22 | Sin equivalencia automática: `coverage_relation` queda visible; `combined` nunca se presenta como equivalente |
| V-23 | Autenticación web ≠ permiso de Telegram; ningún plano otorga el otro |
| V-24 | La superadministradora solo cruza organizaciones para metadatos administrativos, de forma auditada; los datos operativos siguen acotados |
| V-25 | La búsqueda de cadenas no persiste, no usa datos ficticios y nunca presenta un precio anterior como actual |
| V-26 | `relation` es aditivo; `match.level` se conserva y ningún resultado se oculta por su clasificación |
| V-27 | Aprobar una solicitud de acceso nunca asigna un rol automáticamente |
| V-28 | La IA nunca decide dinero, cantidades, permisos ni visibilidad; la clave de OpenAI vive solo en el backend |
| V-29 | Las columnas de archivo de `purchase` solo se escriben por función restringida; el encabezado nunca se elimina |
| V-30 | Un carrito `dismissed` no se evalúa, no suma al total y no cubre faltantes |
| V-31 | No se purga nada hasta que exista archivo verificado, restauración probada y autorización explícita |
| V-32 | La cobertura suma cotizado-sin-compra y compras vigentes; una misma línea nunca se cuenta dos veces |
| V-33 | Una compra registrada no certifica la llegada física: el MVP no registra recepciones y el sistema nunca afirma que el producto fue recibido |
| V-34 | El cierre del ciclo no depende de la recepción ni de la cotización: un faltante pasa a `fulfilled` solo cuando las **compras confirmadas** cubren la cantidad pedida conocida, y nunca se cierra una cantidad desconocida por suposición |
| V-35 | No hay control de recepción, cantidades entregadas, saldos pendientes de recepción ni diferencias de entrega en el MVP (D-126) |
| V-36 | Cerrar un faltante no borra nada; un nuevo faltante del mismo producto es un ciclo nuevo con su propio historial |
| V-37 | Permisos web y autorización de Telegram son independientes; `admin` también informa si su cuenta de Telegram está autorizada, sin exclusiones |
| V-38 | Telegram nunca registra compras ni confirma llegadas; cotizar y comprar son operaciones web de `admin` + `StrongActor` |
| V-39 | Cotizar cubre de forma provisional y nunca cierra: un faltante con el total cotizado sigue abierto en `in_quote` hasta que haya compra confirmada |
| V-40 | Una compra parcial mantiene abierto el mismo ciclo con el saldo visible; el saldo se cotiza y se compra sobre ese faltante, sin crear una solicitud duplicada |

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
Sigue fuera **ejecutar** una orden o modificar la compra. Las **compras parciales** sí forman parte del MVP y se
documentan en §12.3 (D-127); lo que no existe en el MVP son **recepciones** parciales. **No se modifica C0, el HLD ni
el mockup aprobado.**

### 15.1 Funcionalidad futura — recepción física (no es parte del MVP)

El análisis completo se conserva en el registro de decisiones (**D-122**, retirada por **D-126**): tablas `receipt` y
`receipt_line` inmutables, recepciones parciales y acumulativas, diferencias (`missing`, `wrong_product`, `damaged`,
`other`), producto equivocado con `received_quantity = 0`, saldos pendientes de recepción, cierre condicionado y sus
permisos web de `admin` + `StrongActor`. Permanecen abiertas como preguntas **futuras**, no como bloqueos del MVP:
anulación o corrección de una compra con recepciones, compra que nunca llega o parcial definitiva, y corrección de una
recepción errónea. Nada de esto se implementa en el MVP ni se promete en la interfaz.

---

## 16. Roadmap y migraciones (propuesta, sin ejecutar)

La numeración sigue el **orden de ejecución** acordado en D0 y **queda ratificada** (D-125): **C4 = 0013** y
**C3 = 0014**. Sustituye la propuesta anterior de D-112/D-114 (C3 = 0012, C4 = 0013), que no contaba con G1 ni con el
head actual del código (0011).

Las tablas de **0013** (búsqueda) y **0014** (comparación) provienen del diseño ya aprobado de C4 y C3, no de §2:
las **9 tablas** de §2 son solo las que agrega v0.2. Ninguna de estas migraciones está autorizada todavía.

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
| **D0** | Contrato v0.2 rev5, D-113 y D-115 a D-127, ROADMAP | — |
| **G1** | Acceso web con Google, solicitudes, campanita, superadministradora y vencimiento | **0012** (`web_access_request` + 5 columnas de `app_user`) |
| **C4** | E1 con `relation` y `attributes.ingredients[]` (E3 conserva su forma); búsqueda de cadenas E15 (sin persistencia) | **0013** (3 tablas de búsqueda) |
| **C3** | Comparación E5 | **0014** (5 tablas) |
| **C5-A + F1** | Cotizaciones, carritos, líneas, vínculo a faltante, grilla y «Buscar»; observaciones O1 | **0015** (`quote`, `quote_cart`, `quote_line`, `commercial_observation`; 2 columnas de `quote_line`: `demand_item_id`, ya previsto en rev2, y `coverage_relation`) |
| **O1–O4** | Interpretación, recuperación, precedencia y consejo con OpenAI | **0016** (`observation_interpretation`) |
| **C5-B** | Compras registradas, inmutabilidad, correcciones, anulación y `purchase_index` | **0017** (`purchase`, `purchase_line`, `purchase_index`; función de archivo). **La recepción física no forma parte del MVP (D-126).** |
| **C5-C** | Historial operativo: búsqueda por índice | **0018** (`pg_trgm`/`unaccent` e índices; verificación previa) |
| **C5-D** | Archivo externo y purga controlada: diseño cerrado, **ejecución bloqueada** | **0019** (función y permisos; requiere proveedor aprobado y pruebas) |
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

`farmacia` sigue en **0008** (32 tablas). El código tiene hasta **0011** (C2 implementado, no aplicado a `farmacia`).
Ninguna migración nueva se aplica sin autorización explícita.

---

## 17. Aprobaciones y pendientes

**Aprobado conceptualmente el 2026-10-30 (D0):** D-113 y D-115 a **D-127**; cobertura reversible de faltantes sin
confirmación adicional; cobertura con compras vigentes sin doble conteo (P-8 resuelto); **recepción física fuera del
alcance del MVP** (D-126); **cierre exclusivamente por compra confirmada** que cubre la cantidad pedida, con compras
parciales en el mismo ciclo (D-127); estado por Telegram sin duplicar necesidades; `admin` puede informar por Telegram si su
cuenta está autorizada; `relation` junto a `match.level`; OpenAI por Responses API desde el backend; separación de
permisos web/Telegram; superadministradora global limitada a metadatos; control individual de Telegram; **nueve
tablas** y siete columnas (seis nuevas en la revisión 3); numeración **C4 = 0013 / C3 = 0014** ratificada.

**Pendiente de ratificación:**

1. Mantener X1 y X2 como las únicas excepciones a C0-10; la recepción **no** entra como excepción porque está fuera
   del MVP (§1.1, §15).
2. Aprobar las **9 tablas**, las **7 columnas** (6 nuevas en la revisión 3) y las migraciones **0012–0019** (§2, §16).
   **0017 tiene 3 tablas**: `purchase`, `purchase_line` y `purchase_index`.
3. Aprobar E10–E15 y los códigos nuevos de §13 como adición a C0.
4. Confirmar el alcance de la superadministradora: **solo metadatos administrativos**; ampliar a datos operativos
   exige decisión nueva (§9.7).
5. Confirmar que el control individual de Telegram exige cuentas `personal`; en `shared_device` el bloqueo afecta a
   todas las personas del teléfono (§10.4).
6. Confirmar la regla de cierre (D-127): un faltante `active` con cantidad pedida **conocida** pasa a `fulfilled`
   solo cuando las **compras confirmadas** la alcanzan; cubrir con cotización es provisional y **no** cierra; una
   compra parcial mantiene el ciclo abierto con el saldo; con cantidad desconocida no se cierra automáticamente
   (§12.3, §12.6).

**Bloqueos externos, sin aprobar:**

7. **Captura por cadena:** A1 (autorización escrita/API) o A2 (decisión de riesgo registrada) para Farmacias Ahumada;
   ninguna cadena está habilitada hoy (§11.6, §11.7).
8. **OpenAI:** clave, modelo, presupuesto mensual, tarifa y autorización de costo. Sin ellos, O2–O4 quedan
   deshabilitados; O1 sigue operativo (§8.5).
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

**Fuera de D0:** actualizar `CLAUDE.md` y `docs/AUTH_SUPABASE.md`, que todavía describen C2 como no implementado y no
incluyen G1/D-115 a D-127. **D-107** sigue con la fila «Propuesta» aunque el código 0009–0011 ya implementa el perfil
y las solicitudes; se anota, no se reescribe. Se hará en una fase documental posterior, con autorización.