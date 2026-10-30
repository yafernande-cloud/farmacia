# Contrato del cotizador — v0.2 (cotizaciones, carritos, compras registradas e historial)

**Estado: BORRADOR PARA REVISIÓN, 2026-10-30 (revisión 2).** No aprobado, no implementado. Este documento **no
modifica C0** (`docs/contracts/cotizador-v0.md`, congelado por D-093): lo **amplía** y declara la ampliación de forma
explícita. Mientras no se apruebe, nada de lo aquí escrito está permitido en el sistema.
**Revisión 2:** incorpora las precisiones P-1 a P-7 del 2026-10-30 (encabezado de compra permanente, purga solo del
detalle, sustitución única, anulación, desistimiento del pendiente, `purchase_index` y permisos `admin`).
**Decisión que lo respalda:** D-114 (`docs/decisions/0001-registro-de-decisiones.md`).
**Decisiones relacionadas:** D-037 (separación de datos), D-087/D-088 (cotas y cálculo), D-091 (criterios
comerciales), D-110 (`review_status`), D-111 (idempotencia de importación), D-112 (catálogos de cadenas).
**Fuente de verdad:** HLD/LLD `docs/Farmacia_Parque_Lantano_HLD_LLD.md` (§2, §6.2, §7.1, §9, §14.1, §17.1, §22).
**Alcance:** contrato aditivo. No modifica E1–E9, roles, Telegram ni Supabase Auth.

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

`purchase_execution` sigue `blocked`. Siguen fuera: aprobaciones, propuestas formales, envío de órdenes, pagos,
recepción de mercadería.

### 1.2 Qué no cambia

- E1–E9 conservan forma y semántica. **E5 no cambia**: es el único motor de cálculo comercial.
- `offer_snapshot` sigue siendo inmutable y de solo agregar; `shipping_terms_snapshot` y `audit_event`, igual.
- Los precios públicos de cadenas siguen fuera de ofertas, carritos, subtotales y totales (C0-05, AC20).
- C0-07 sigue vigente: **lo parcial no tiene `total`**. v0.2 no agrega un total a un proveedor parcial; agrega un
  objeto distinto, más estricto, para la cotización completa (§5.6).
- No hay IA en este flujo. La interpretación de observaciones libres es capacidad futura (**F**), fuera de alcance.

### 1.3 Tres planos, otra vez separados

| Plano | Quién lo produce | Dónde vive |
|---|---|---|
| **Cálculo comercial** | E5 determinista | Se calcula en vivo; no se guarda |
| **Selección** | Pao (persona) | `quote_line` (oferta + cantidad) |
| **Hecho de compra** | Registro manual posterior | `purchase` + `purchase_line` (inmutable) |

Ningún plano escribe en otro. El carrito no copia precios; la compra copia todo porque debe sobrevivir sola.

---

## 2. Modelo de datos

Todas las tablas: `organization_id` con FK compuesta, `data_origin` (`simulated|captured|ai_derived`), sin borrado
físico desde la API, y auditoría en `audit_event` dentro de la misma transacción.

### 2.1 `quote` — cotización general

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid PK | |
| `organization_id` | uuid | FK `organization` |
| `title` | varchar(120) | Texto libre («Reposición semanal»); nunca obligatorio para trabajar |
| `status` | varchar(10) | `draft` \| `closed` |
| `created_by`, `closed_by` | uuid | FK `app_user` de la misma organización |
| `closed_at` | timestamptz | NULL mientras `draft` |
| `created_at`, `updated_at` | timestamptz | UTC |
| `version` | integer | Control optimista |

Sin `archive_state`: **la cotización no se archiva por política de compras** (§7.2).

### 2.2 `quote_cart` — un carrito por proveedor

| Columna | Tipo | Notas |
|---|---|---|
| `id` | uuid PK | |
| `organization_id`, `quote_id` | uuid | FK compuesta a `quote(id, organization_id)` |
| `supplier_id` | uuid | FK `supplier` de la misma organización |
| `status` | varchar(10) | `active` \| `dismissed` |
| `dismissed_at`, `dismissed_by` | | Solo si es distinto de `active` |
| `created_at`, `updated_at`, `version` | | |

`UNIQUE (quote_id, supplier_id)` — un carrito por proveedor y cotización. Se **crea solo** al agregar la primera
línea de ese proveedor; no hay endpoint de creación de carrito y Pao nunca necesita su identificador.
Además, `UNIQUE (id, supplier_id, organization_id)` para que `quote_line` pueda referenciarlo con FK compuesta y no
pueda quedar en un carrito de otro proveedor.

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
| `demand_item_id` | uuid NULL | Necesidad interna que esta línea ayuda a cubrir |
| `pending_closed_at`, `pending_closed_by` | | Cierre del **pendiente**: Pao desiste de comprar lo que falta. No cambia `quantity` |
| `pending_closed_note` | text NULL | Motivo opcional del desistimiento |
| `added_by`, `added_at`, `updated_at`, `version` | | |

`UNIQUE (quote_cart_id, offer_snapshot_id) WHERE status='active'` — dos selecciones de la misma oferta en el mismo
carrito son la misma línea, no una suma. Dos ofertas distintas del mismo artículo **sí** conviven (§6.3).

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

**Inmutabilidad real:** trigger que rechaza `UPDATE` **siempre** y `DELETE` salvo la purga controlada de §7.4, como en
`import_request`. Una corrección **no edita**: crea un registro nuevo con `supersedes_purchase_id`. No hay columna
`status`: el registro existe porque ocurrió. **El encabezado nunca se elimina** (§7.4); solo su detalle de líneas
puede salir de la base operativa.

Restricciones de coherencia: si `supersedes_purchase_id` no es NULL se exige `correction_reason` (corrección) **o**
`annulment_reason` (anulación), según el caso; `is_annulment = true` implica `annulment_reason` y **cero** líneas de
detalle.

### 2.5 `purchase_line` — detalle histórico completo (inmutable)

Guarda lo que efectivamente se compró, **copiado** en el momento del registro: `trade_item_id`, `external_id`,
`description_as_recorded`, `presentation_as_recorded`, `laboratory_as_recorded`, `quantity` (> 0), `quantity_unit`,
`unit_price`, `currency`, `taxes` (jsonb), `discounts` (jsonb), `shipping_share`, `line_subtotal`, `line_total`,
`source_offer_snapshot_id` (procedencia), `notes`, y `quote_line_id` NULL cuando la compra no corresponde a una línea
cotizada.

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

---

## 4. Estados y transiciones

### 4.1 Almacenados

```
quote        draft ──cerrar──► closed ──reabrir──► draft
quote_cart   active ──descartar──► dismissed ──reincorporar──► active
quote_line   active ──quitar──► removed                  (nunca se borra; solo sin compras)
             pendiente ──cerrar pendiente──► cerrado
             cerrado ──reabrir pendiente──► pendiente    (no cambia `status`)
purchase     (sin estado; existe porque ocurrió; inmutable)
```

El «cerrado» del pendiente **no es un estado almacenado**: es `pending_closed_at` en la línea. Por eso cerrar el
pendiente no altera la cantidad cotizada ni la compra histórica.

**Toda transición tiene su operación** (§5, §6): no quedan estados alcanzables solo por efectos laterales.

### 4.2 Derivados (se calculan, no se guardan)

| Objeto | Derivado de | Valores |
|---|---|---|
| Carrito | líneas activas + compras vigentes | `empty` \| `pending` \| `partially_purchased` \| `purchased` |
| Línea | dos dimensiones independientes: cobertura comprada y estado del resto | `purchase_state`: `pending` \| `partially_purchased` \| `purchased` \| `exceeded` · `remainder`: `open` \| `closed_by_user` \| `closed_by_quote` |
| Cotización | carritos + compras vigentes | `in_preparation` \| `partially_purchased` \| `fully_purchased` \| `closed_without_purchase` |
| Compra | existe o no un registro que la sustituya | `current` \| `superseded` (más `is_annulment`) |

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
| `GET /api/v1/quotes/{quote_id}` | Detalle completo: carritos, líneas, evaluación E5 por carrito, total estimado |
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
  "demand_item_id": null
}
```

- Crea el carrito del proveedor si no existe. Si existe y está `dismissed`, responde 409 `CART_DISMISSED`
  (hay que reincorporarlo explícitamente: no se resucita por accidente).
- Valida: la oferta pertenece al proveedor (`422 OFFER_NOT_IN_SUPPLIER`), a la organización (404), la cotización está
  `draft` (`409 QUOTE_CLOSED`), la cantidad es entera > 0 (`422 INVALID_QUANTITY`).
- Respuesta `201` con la línea y el carrito creado o reutilizado.

### 5.3 Cantidad, quitar y mover

| Método y ruta | Regla |
|---|---|
| `PATCH /quotes/{quote_id}/lines/{line_id}` | SET explícito de cantidad + `version`. No hay suma implícita: repetir el aviso no acumula. Si ya hay compras vigentes, la nueva cantidad no puede ser menor que lo comprado → `409 QUANTITY_BELOW_PURCHASED` |
| `DELETE /quotes/{quote_id}/lines/{line_id}` | Quita lógico (`removed`) con `removed_by`/`removed_at`. Solo si **no hay compras vigentes** → si las hay, `409 LINE_HAS_PURCHASES`; lo pendiente se cierra con `close-pending` |
| `POST /quotes/{quote_id}/lines/{line_id}/close-pending` | **Desistir del pendiente**: marca `pending_closed_at`. No cambia `quantity`, no toca la compra histórica y no borra la línea. La cantidad cotizada original queda visible |
| `POST /quotes/{quote_id}/lines/{line_id}/reopen-pending` | Deshace el cierre del pendiente mientras la cotización esté `draft` |
| `POST /quotes/{quote_id}/lines/{line_id}/move` | `{ "target_offer_snapshot_id": "uuid", "quantity": {...} }`. Exige oferta destino **explícita**, del **mismo artículo** (`422 ARTICLE_MISMATCH` si no) y solo si **no hay compras vigentes** (`409 LINE_HAS_PURCHASES`). Atómico: quita + agrega en una transacción y un solo evento de auditoría con antes/después. Crea el carrito destino si hace falta |
| `POST /quotes/{quote_id}/lines/{line_id}/refresh` | Acepta el precio actual; reapunta al snapshot nuevo. Auditado. **No toca compras**; si la línea está comprada por completo, responde con advertencia de que no tiene efecto sobre lo pendiente |

**Repartir cantidades**: no es una operación nueva. Pao agrega la misma referencia en el carrito de otro proveedor
(creando otra línea). El resumen muestra, por artículo, la cantidad distribuida y la pendiente.

### 5.4 Carritos: descartar y reincorporar

| Método y ruta | Efecto |
|---|---|
| `POST /quotes/{quote_id}/carts/{supplier_id}/dismiss` | `active → dismissed`. Oculta el carrito del panel; **no** borra líneas |
| `POST /quotes/{quote_id}/carts/{supplier_id}/reinstate` | `dismissed → active`. Devuelve el carrito tal como estaba |

Se identifican por proveedor, no por identificador de carrito.

### 5.5 Evaluación por carrito

Cada carrito con líneas activas se evalúa con **E5** (`POST /comparisons`, invocado internamente). El contrato de E5
no cambia; solo cambia el patrón de invocación: **una comparación por carrito**, ejecutadas en paralelo **en proceso**
(sin llamadas externas). Motivo: mínimo, múltiplo, impuestos, descuentos y despacho son **por proveedor**, y el reparto
entre proveedores exige cantidades distintas para el mismo artículo.

Consecuencia declarada: la cotización general **no** tiene una única llamada a E5.

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

1. Cada carrito con líneas activas está `complete` en E5 (sin `blockers`), y
2. hay una sola moneda entre líneas, cargos y despachos, y
3. no hay ninguna causa `unknown` pendiente en los carritos activos.

Si falta cualquiera → `available: false` con causas visibles. **Nunca se muestra una cota inferior como costo total
general**, y no se suma la cota de un carrito a la de otro. Las cotas por proveedor siguen disponibles por separado,
etiquetadas como «cota inferior del proveedor», y **no** se suman entre sí.

El total estimado **no** sirve para comparar proveedores, no decide nada y no se guarda como cifra de compra.

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

### 6.6 Consultas

| Método y ruta | Para qué |
|---|---|
| `GET /api/v1/quotes/{quote_id}/purchases` | Compras vigentes y sustituidas de la cotización, con su estado |
| `GET /api/v1/purchases/{purchase_id}` | Detalle inmutable completo |

---

## 7. E12 — Historial de cotizaciones

Permisos: **rol `admin` + `StrongActor`** (§5). Leer historial archivado no cambia el registro ni reintroduce datos.

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

1. Bundle canónico (claves ordenadas) con compra, líneas, cadena de correcciones y contexto mínimo de la cotización.
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
- La búsqueda de texto usa `pg_trgm` + `unaccent` sobre `search_text`. Su **disponibilidad se verifica en el
  PostgreSQL de destino antes de implementar** (C5-C); no se instala ninguna extensión sin autorización.
- Si alguna no estuviera disponible, la alternativa es búsqueda por prefijo con `lower()` —más limitada— y requiere
  tu decisión.
- Los filtros por proveedor y fecha usan columnas del índice. Compras operativas y archivadas se resuelven con **la
  misma consulta**, sin ramas distintas.

### 7.4 Purga controlada (diseñada, **no autorizada**)

- Función `SECURITY DEFINER` propiedad de un rol dedicado. El rol de la aplicación **no tiene `GRANT DELETE`** sobre
  `purchase` ni `purchase_line`.
- Trigger: `UPDATE` bloqueado **siempre**; `DELETE` sobre `purchase_line` solo si
  `current_setting('app.archive_purge') = 'on'` **y** la compra padre tiene `archive_status = 'verified'` **y**
  `archive_restore_ok = true` **y** `detail_purged_at IS NULL`; en ese acto se marca el padre.
- `DELETE` sobre `purchase` **no existe**: no hay ruta, no hay permiso, no hay función. La cadena de correcciones y
  las FK no pueden romperse.
- El comando `history-purge` (una compra por transacción, `--dry-run`, límite y filtro de fecha, solo `admin`,
  auditado) queda **documentado como diseño y deshabilitado**.
- **Bloqueo vigente:** no se ejecuta mientras no exista almacenamiento externo aprobado y no se hayan probado la
  integridad, la restauración y la consulta de los registros archivados. Hasta entonces las compras siguen completas
  en la base operativa aunque superen los 6 meses.

---

## 8. Códigos de error nuevos

Adiciones al sobre de error de C0 (`{"error": {"code", "message", "details"?}}`); no reemplazan ninguno existente.

| HTTP | Código | Cuándo |
|---|---|---|
| 404 | `QUOTE_NOT_FOUND` | Cotización inexistente o de otra organización |
| 409 | `QUOTE_CLOSED` | Modificar líneas de una cotización cerrada |
| 409 | `QUOTE_VERSION_CONFLICT` | `version` desactualizada (dos pestañas, dos personas) |
| 409 | `CART_DISMISSED` | Agregar línea a un carrito descartado |
| 409 | `IDEMPOTENCY_KEY_REUSED` | Ya existe (E8); se reutiliza la semántica |
| 422 | `OFFER_NOT_IN_SUPPLIER` | La oferta no pertenece al proveedor indicado |
| 422 | `ARTICLE_MISMATCH` | Mover a una oferta de otro artículo |
| 422 | `INVALID_QUANTITY` | Cantidad no entera, ≤ 0 o unidad inválida |
| 422 | `PURCHASE_INVALID` | Compra incoherente (moneda, líneas, importes) |
| 409 | `PURCHASE_ALREADY_SUPERSEDED` | Ya existe una sustitución o anulación de ese registro |
| 409 | `LINE_HAS_PURCHASES` | Quitar o mover una línea con compras vigentes |
| 409 | `QUANTITY_BELOW_PURCHASED` | Cantidad nueva menor que lo ya comprado |
| 422 | `INVALID_ANNULMENT` | Anulación sin motivo, con líneas o sobre una compra ya anulada |
| 503 | `ARCHIVE_UNAVAILABLE` | Almacén de archivo caído al consultar un registro archivado |

Un carrito incompleto, una línea `pending` o una cotización sin compras **no son errores**: son estados válidos.

---

## 9. Invariantes que v0.2 debe respetar

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

---

## 10. Fuera de alcance de v0.2

Aprobaciones formales, envío de órdenes, pagos, recepción de mercadería, sincronización con proveedores, SSE,
búsqueda federada de cadenas (D-112/D-113, contrato aparte), interpretación de observaciones libres con IA, cambios en
Lovable, y la implementación del almacenamiento de archivo.

---

## 11. Roadmap y migraciones (propuesta, sin ejecutar)

| Etapa | Contenido | Migración |
|---|---|---|
| C0 | Contrato congelado | — |
| C3 | Comparación E5 | 0012 |
| C4 | Criterios comerciales | 0013 |
| **C5-A** | Cotizaciones, carritos, líneas, E5 por carrito, resumen, versiones optimistas | **0014** |
| **C5-B** | Compras registradas, inmutabilidad, correcciones, anulación e `purchase_index` | **0015** |
| **C5-C** | Historial operativo: búsqueda por índice (sin tablas nuevas; exige verificar `pg_trgm`/`unaccent`) | — |
| **C5-D** | Archivo externo y purga controlada: diseño cerrado, **ejecución bloqueada** | **0016** (función y permisos; requiere proveedor aprobado y pruebas de integridad, restauración y consulta) |
| **C5-E** | Enganche de navegación: Mis cotizaciones, Historial, Proveedores (E6) | — |

`farmacia` sigue en 0008. Ninguna de estas migraciones se aplica sin autorización explícita.

---

## 12. Aprobaciones y pendientes

**Aprobado conceptualmente el 2026-10-30 (P-1 a P-7):** encabezado `purchase` permanente y `archive_record`
sustituido por metadatos de archivo + `purchase_index`; `UNIQUE (supersedes_purchase_id)` con correcciones
inmutables y compras archivadas corregibles; anulación con motivo y exclusión de totales; protección de compras
registradas con desistimiento del pendiente; `purchase_index` con `pg_trgm`/`unaccent` sujeto a verificación;
`admin` + `StrongActor` en E10/E11/E12; purga diseñada pero **no ejecutable**.

**Pendiente:**

1. Ratificar el texto final de X1 y X2 como excepciones a C0-10 (§1.1).
2. Aprobar las 6 tablas de §2 y las migraciones 0014–0016 (§11).
3. Aprobar E10/E11/E12 y los códigos nuevos de §8 como adición a C0.
4. Verificar la disponibilidad de `pg_trgm` y `unaccent` en el PostgreSQL de destino **antes** de C5-C (no se
   instalan todavía).
5. Elegir proveedor de almacenamiento externo, costos, credenciales y política de bloqueo de retención, antes de
   habilitar C5-D.
6. Autorizar expresamente la ejecución de la purga, una vez cumplidos 4 y 5 y probadas integridad, restauración y
   consulta de los registros archivados.
