# Contrato del cotizador — v0 (fase C0)

**Estado: APROBADO Y CONGELADO el 2026-10-29** (D-093, C0-13). Escrito y revisado el 2026-10-29 con las correcciones
B1–B11, las precisiones de cantidad y cota inferior, la decisión D-091 (criterios comerciales) y los ajustes de cierre
de C0 (D-092). **No implementado:** el congelamiento fija lo que Lovable y el backend deben respetar; no crea código
ni autoriza migraciones. Un cambio de alcance posterior exige una decisión nueva y una versión nueva de este documento.
**Decisiones:** D-078 a D-093 (`docs/decisions/0001-registro-de-decisiones.md`).
**Fuente de verdad:** HLD/LLD `docs/Farmacia_Parque_Lantano_HLD_LLD.md` (§2, §5, §6.2, §7.1, §9, §13, §14.1, §14.3,
§17.1, §22).
**Alcance:** contrato aditivo. No modifica endpoints, roles, Telegram ni Supabase Auth existentes.

> Este documento **no implementa nada**: no crea rutas, modelos, tablas ni migraciones. Es el acuerdo previo que
> Lovable y el backend deben respetar. El OpenAPI real seguirá siendo el de FastAPI (`/openapi.json`); este archivo
> no reemplaza `docs/contracts/openapi.json` ni debe editarse a mano para simular rutas existentes.

---

## 1. Propósito y límites

El cotizador permite a los jefes de la farmacia buscar medicamentos, identificar su presentación comercial, consultar
ofertas de proveedores, comparar alternativas de abastecimiento y ver referencias de precios públicos de cadenas.

**Límites duros (HLD §2):**

1. No ejecuta compras, no crea carritos, propuestas ni aprobaciones. `purchase_execution` sigue `blocked`.
2. Una referencia de cadena **no** es una oferta comprable y nunca participa en «más barato» (invariante 8, AC20).
3. Información incompleta nunca produce una oferta ganadora ni un total afirmado como completo (invariante 7, AC19).
4. Fuente caída nunca se presenta como «sin ofertas» (AC17).
5. Toda respuesta declara origen, modo y momento de observación (invariante 13, §22).
6. La IA no decide precios, cantidades ni permisos. En v0 no hay IA en el cotizador.
7. Un criterio comercial registrado por una persona **nunca** modifica precios, cálculos, elegibilidad ni visibilidad
   de ofertas; solo añade anotaciones (D-091, §3.5).

### 1.1 Tres planos separados

| Plano | Quién lo produce | Qué puede cambiar | Estado en v0 |
|---|---|---|---|
| **Cálculo** | Motor determinista (C3) | precios, cantidades, totales, elegibilidad | Congelado aquí |
| **Criterios** | ADMIN, explícitos y confirmados | solo anotaciones visibles | Contrato aquí; registro en C4 |
| **Recomendación** | Motor determinista con criterios confirmados | a qué opción se sugiere ir, con explicación | Reservado; H4 |

Los tres planos no se mezclan: `lowest_verified_total` existe con o sin criterios, y una recomendación futura nunca
reemplaza al número.

---

## 2. Convenciones comunes

### 2.1 Base, formato y trazabilidad

- Base: `/api/v1`. JSON UTF-8. Fechas ISO-8601 **UTC**; la presentación en America/Santiago es del cliente.
- Toda respuesta HTTP incluye `X-Correlation-ID` (se reutiliza el del cliente si viene).
- Recursos de otra organización responden **404**, nunca 403 ni 200 vacío (no filtrar existencia).
- No se acepta `organization_id` ni identidad de actor en el cuerpo o la query.

### 2.2 Autenticación y autorización

- `Authorization: Bearer <JWT de Supabase>` validado contra el JWKS; el `sub` se resuelve en `app_user.auth_subject`.
- La organización y los roles salen de esa fila, nunca del token ni del cliente.
- Con `AUTH_MODE=demo_header`, la cabecera `X-Demo-User-Id` se acepta en el resolvedor de identidad para cualquier
  organización; **es `StrongActor` quien la rechaza fuera de `simulation`**.
- **Todas las operaciones E1–E9 exigen dos comprobaciones independientes** (D-086):
  1. **Rol `admin`** verificado en el servicio. La matriz actual (`backend/app/core/permissions.py`) no tiene un
     permiso exclusivo de `admin` semánticamente apto (`VIEW_LIST` lo tienen los cuatro roles; `MANAGE_*` son de
     personal, cuentas e identidades). Se usa un chequeo de rol, **sin crear `Permission` nuevos ni tocar `_GRANTS`**.
  2. **`StrongActor`**, cuyo significado real en el código es «no se acepta `X-Demo-User-Id` fuera de la organización
     `simulation`» (`backend/app/api/deps.py:83-98`). Con `AUTH_MODE=supabase_jwt` es un no-op: **no es MFA ni
     re-autenticación**. La autenticación fuerte para aprobar o comprar sigue en H5.
- No se crean roles ni permisos nuevos. La organización sale del `Actor`; no se acepta en el cuerpo.

### 2.3 Errores

Envelope único del HLD §14.3:

```json
{
  "error": {
    "code": "VERSION_CONFLICT",
    "message": "El recurso cambió. Revisa la versión actual.",
    "correlation_id": "uuid",
    "details": {"current_version": 4}
  }
}
```

| HTTP | `code` | Cuándo | Origen |
|---|---|---|---|
| 400 | `VALIDATION_ERROR` | Cuerpo o query inválidos | Existente |
| 400 | `INVALID_CURSOR` | Cursor de paginación ilegible | Existente |
| 401 | `UNAUTHENTICATED` | Sin sesión o usuario inactivo | Existente |
| 403 | `FORBIDDEN` | Sin rol `admin` | Existente |
| 403 | `STRONG_AUTH_REQUIRED` | Cabecera demo en organización real | Existente |
| 403 | `USER_NOT_PROVISIONED` | JWT válido sin fila enlazada | Existente |
| 404 | `NOT_FOUND` | Recurso inexistente o de otra organización | Existente |
| 409 | `IDEMPOTENCY_KEY_REUSED` | Misma clave con contenido distinto | Existente |
| 409 | `VERSION_CONFLICT` | `If-Match` desactualizado | Existente |
| 409 | `DUPLICATE_NAME` | Proveedor o fuente con nombre equivalente activo | Existente |
| 409 | `CRITERION_NOT_NOTE` | Se confirma algo que no es una nota | **Nuevo** |
| 409 | `CRITERION_NOT_ACTIVE` | Desactivar/reactivar un criterio ya en ese estado, o reconfirmar una nota ya convertida | **Nuevo** |
| 409 | `CRITERION_NOTE_SUPERSEDED` | Se reactiva una nota cuyo criterio derivado está vigente (o el criterio con su nota vigente): quedarían dos anotaciones activas | **Nuevo** |
| 413 | `PAYLOAD_TOO_LARGE` | Archivo de importación sobre el límite | Existente |
| 422 | `INVALID_UNIT` | Unidad no reconocida | Existente |
| 422 | `UNIT_MISMATCH` | Unidades incompatibles en una operación | Existente |
| 422 | `QUANTITY_UNKNOWN` | Línea sin cantidad definida | Existente |
| 422 | `INVALID_QUANTITY` | Cantidad fraccionaria o ≤ 0 en unidades comerciales | **Nuevo** |
| 422 | `CURRENCY_MISMATCH` | Monedas incompatibles en un total | **Nuevo** |
| 422 | `FIXTURE_NOT_ALLOWED` | Fuente `fixture` fuera de `simulation` | **Nuevo** |
| 422 | `SOURCE_CAPABILITY_NOT_ALLOWED` | Capacidad incompatible con el tipo de fuente o con v0 | **Nuevo** |
| 422 | `IMPORT_FORMAT_INVALID` | Archivo ilegible o cabecera ausente | **Nuevo** |
| 422 | `CRITERION_INCOMPLETE` | Criterio sin efecto, ámbito, motivo o confirmación | **Nuevo** |
| 422 | `CRITERION_EFFECT_NOT_ALLOWED` | Efecto no permitido para ese tipo de registro | **Nuevo** |
| 428 | `VERSION_REQUIRED` | Falta `If-Match` en una edición | Existente |
| 429 | `RATE_LIMITED` | Límite de consultas | **Reservado v0** (HLD §14.3) |
| 503 | `SEARCH_TIMEOUT` | Búsqueda excede el tiempo objetivo (D-075) | **Nuevo** |
| 503 | `DATABASE_NOT_CONFIGURED` / `AUTH_NOT_CONFIGURED` | Subsistema no disponible | Existente |

Las **fallas de fuente no son errores HTTP**: viajan en `sources[].status` y `errors[]` con HTTP 200 (AC17).

**Códigos de fila de E8** (no son errores HTTP; viajan en `errors[]` de la importación):
`MISSING_REQUIRED_FIELD`, `INVALID_PRICE`, `INVALID_CURRENCY`, `INVALID_BASIS`, `INVALID_QUANTITY`, `UNKNOWN_UNIT`,
`DUPLICATE_ROW`, `UNMAPPED_PRODUCT`, `UNMODELLED_CONDITION`.

### 2.4 Dinero, cantidades y medidas

**Dinero** (D-081): siempre objeto, nunca número suelto. `amount` es un string decimal (`^\d+(\.\d{1,4})?$`), nunca
float. La moneda es obligatoria y no se asume CLP por configuración.

```json
{ "amount": "3490.00", "currency": "CLP" }
```

**Precio** agrega base e impuestos/descuentos:

```json
{
  "money": { "amount": "3490.00", "currency": "CLP" },
  "basis": "per_commercial_unit",
  "tax_included": "unknown",
  "discount_included": "unknown"
}
```

- `basis`: `per_commercial_unit` (por caja, frasco, etc.) o `per_base_unit` (por comprimido, mL, g). Comparar bases
  distintas no está permitido sin política explícita (HLD §9.2).
- `tax_included` y `discount_included`: `true` | `false` | `"unknown"` (tri-estado, D-081). `"unknown"` impide
  afirmar total completo.
- `discount_included=true`: el precio ya incorpora sus descuentos; **no** se restan descuentos adicionales (evita
  doble resta). `false`: se aplican. `unknown`: si hay descuentos aplicables, no hay total ni cota; si no hay
  ninguno, se trata como `false`.

**Unidades de cantidad del cotizador** (D-089): `QuantityUnit` = `box` | `package` | `unit` | `bottle`
(`backend/app/domain/quantities.py`). Es el **único** vocabulario válido para cantidades de comparación. El texto
original («caja», «caja x 10») se conserva en `original_text` y se normaliza solo para comparar; no se compara texto
sin normalizar. Una unidad nueva exige fila en `domain/units.py`/`domain/products.py` y su prueba (D-015 a D-024);
no se inventa un quinto vocabulario.

**Cantidad de comparación**:

```json
{ "value": "5", "unit": "box" }
```

- `value`: entero > 0. Fraccionario o ≤ 0 → 422 `INVALID_QUANTITY`.
- `unit`: debe coincidir con `sale_unit.code` de la oferta; si no, el candidato es `ineligible` con causa
  `quantity_unit_mismatch`. Convertir entre unidades solo con `pack.units_per_commercial_unit` explícito y confirmado.

**Cantidad comprada, mínimos y múltiplos** (D-088) — fórmula determinista:

```text
requested = cantidad pedida (entero > 0, en sale_unit)
min       = minimum.known  ? minimum.quantity (entero >= 0) : null
mult      = multiple.known ? multiple.quantity (entero >= 1) : 1
base      = min is null ? requested : max(requested, min)
purchased = ceil(base / mult) * mult          # Decimal con ROUND_CEILING
surplus   = purchased - requested
```

- `minimum` y `multiple` **ausentes no significan 0 ni 1**: significan **desconocidos** (`known=false`). Nunca se
  asume una condición comercial que el proveedor no informó.
- Si `minimum.known=false` o `multiple.known=false`, `purchased` es una **cota inferior** de la cantidad real (solo
  puede subir); la línea queda `completeness=partial` con causa `minimum_unknown` o `multiple_unknown`.
- **El stock se evalúa contra `purchased`, no contra `requested`**: un múltiplo puede volver insuficiente un stock
  que alcanzaba para lo pedido.
- Mínimo por **monto** («mínimo $50.000») queda fuera de v0: no se modela ni se aplica. Si la fuente declara uno,
  la oferta queda `needs_review` con causa `order_minimum_unmodelled`. No se «resuelve» leyendo el texto.

**Redondeo** (D-088): cantidades con `ROUND_CEILING`; montos internos con `Decimal` a 4 decimales y `ROUND_HALF_UP`
solo al presentar, con los decimales de la moneda (CLP 0, USD 2). Los totales se calculan sobre valores sin redondear
y se cuantizan una vez.

**Medidas** (`strengths`, `content`, `packaging`): se reutiliza la forma de `AttributesOut`
(`backend/app/api/v1/schemas.py:95-142`): `label`, `as_written` (con `original_text`) y `normalized` (o `null` si no
hay unidad). **Un número suelto no se interpreta como mg** (D-015 a D-024). La cantidad solicitada no forma parte del
artículo: es de la línea de comparación, no del catálogo.

### 2.5 Procedencia, frescura y modos

Todo dato comercial lleva procedencia:

```json
{
  "source": { "source_id": "uuid", "source_kind": "supplier", "name": "Proveedor X" },
  "mode": "manual_import",
  "data_origin": "captured",
  "observed_at": "2026-10-29T12:00:00Z",
  "evidence": { "kind": "file", "hash": "sha256:…", "url": null, "note": "lista octubre 2026" }
}
```

y frescura por campo:

```json
{
  "observed_at": "2026-10-29T12:00:00Z",
  "fresh_until": { "price": "2026-11-05T00:00:00Z", "stock": null, "terms": null },
  "is_stale": false,
  "stale_fields": []
}
```

- `mode`: `live` | `sandbox` | `fixture` | `manual_import` (HLD §5.2).
- `data_origin`: `captured` (persona, archivo, API), `ai_derived` (extracción por modelo), `simulated` (simulador).
  `unclassified` **no se usa** en el cotizador.
- `mode=fixture` **solo existe en la organización `simulation`**. En `real_test`/`production` solo se aceptan `live`
  y `manual_import` (D-038/D-039). E7 rechaza `fixture` fuera de simulación con `FIXTURE_NOT_ALLOWED`.
- `fixture`, `simulated` y `sandbox` son visibles en la UI tal como son (HLD §22).
- `observed_at` es del dato, no de la respuesta. La frescura se declara **por campo** (`price`, `stock`, `terms`);
  un umbral `null` significa «sin política configurada», no «fresco para siempre».
- Los umbrales concretos de `stale` por fuente se configuran en C2; la forma queda fija aquí.

### 2.6 Versión e idempotencia

- Recursos editables (`supplier`, `source`, `commercial_criterion`) exponen `version` entero. `PATCH` **y las
  transiciones de estado** (`confirm`, `deactivate`, `reactivate`) exigen `If-Match: "<version>"`; si falta, 428
  `VERSION_REQUIRED`; si no coincide, 409 `VERSION_CONFLICT` con `details.current_version`.
- `POST /imports` exige `Idempotency-Key` (8–128 caracteres). Reutilizarla con el mismo archivo devuelve el
  resultado original; con contenido distinto, 409 `IDEMPOTENCY_KEY_REUSED`.
- `POST /suppliers`, `POST /sources` y `POST /commercial-criteria` aceptan `Idempotency-Key` opcional con la misma
  regla.

---

## 3. Modelo conceptual

### 3.1 Artículo y presentación

- **Artículo** = una **presentación comercial** (`trade_item` en el HLD §6.2): principio(s) activo(s), dosis y
  denominador, forma, vía, liberación, contenido del envase, unidad comercial, marca/laboratorio y códigos verificados.
- Caja de 10 y caja de 20 del mismo producto son **artículos distintos** (HLD §7.1). No se fusionan.
- El artículo **no** incluye la cantidad solicitada ni el proveedor.
- Una identidad farmacéutica puede tener muchos artículos comerciales; el artículo es el nivel donde existen precios y
  stock.

### 3.2 Proveedor, fuente y oferta

- **Proveedor** (`supplier`): entidad a la que se le compra. Tiene nombre y estado; sus condiciones comerciales viven
  en la oferta y en los términos de despacho, no duplicadas en el proveedor. **Sin estructura de crédito en v0.**
  `known_conditions` es texto informativo y **no alimenta ningún cálculo**.
- **Fuente** (`source` / `connector_config`): conector concreto, de tipo `supplier` o `chain_reference`, con modo,
  capacidades y estado. Una fuente de proveedor pertenece a un proveedor; una fuente de cadena no.
- **Oferta** (`offer_snapshot`): observación fechada de un SKU de un proveedor para un artículo. Es inmutable y tiene
  vigencia. `offer_id` identifica el snapshot; `external_id` es el SKU del proveedor.
- La oferta incluye precio con base, unidad de venta, contenido por unidad comercial, stock (conocido/desconocido),
  mínimo, múltiplo, impuestos, descuentos, despacho, vigencia, evidencia y procedencia (§4.2).
- **Capacidades** (HLD §5.1): `search`, `product_detail`, `price`, `stock`, `terms`, `remote_cart`, `submit_order`,
  `order_status`, `cancel_order`. En v0 solo se aceptan las cinco primeras; las transaccionales responden
  `SOURCE_CAPABILITY_NOT_ALLOWED` hasta H5.

### 3.3 Cadena y referencia de precio

- **Cadena**: fuente de tipo `chain_reference`. Aporta precio público de venta, no costo de adquisición.
- **Referencia** (`reference_price`): nombre de cadena, producto exacto, presentación, precio normal/promoción,
  condiciones (membresía, medio de pago, canal), despacho **informado como condición de la referencia**, stock si se
  informa, fecha y evidencia. Esas condiciones no son la capacidad `terms` de compra ni alimentan totales.
- Nunca es comprable, nunca entra en `offers[]`, nunca se usa en totales ni en `lowest_verified_total`. Su esquema es
  distinto del de oferta. En C4 llega vacío con `status="not_configured"`; la ingesta real es C6.

### 3.4 Búsqueda y comparación

- **Búsqueda** (`search_run` + `search_source_result`): consulta, interpretación, fuentes consultadas, cobertura,
  fallos, tiempos y resultados. Es síncrona en v0 (D-075).
- **Comparación** (`comparison_run` + `comparison_candidate`): entradas, elegibilidad, causas, costo calculado y
  versión de criterios. Determinista, sin IA.
- No hay asignación entre proveedores en v0: eso es propuesta masiva (H4).

### 3.5 Criterio comercial y nota

Dos registros distintos, **un solo recurso** con `kind`, sin estados ambiguos (D-091):

| | **Nota** (`kind=note`) | **Criterio** (`kind=criterion`) |
|---|---|---|
| Qué es | Observación escrita por ADMIN | Regla comercial confirmada |
| `effect` | solo `note_only` | `prefer`, `deprioritize`, `do_not_recommend`, `requires_review` |
| Confirmación | No requiere | Exige `confirmed_by`, `confirmed_at`, `origin=explicit` |
| Efecto en el sistema | **Ninguno**; se muestra como contexto | Solo anotación; **nunca** altera números ni elegibilidad |
| Se convierte en regla | Solo si un ADMIN la confirma explícitamente | Nunca automáticamente |

- `status`: `active` | `inactive`. **No existe `proposed`**: lo no confirmado es, por definición, una nota.
- `origin`: `explicit` | `inferred`. En v0 solo se crean `explicit`; un `inferred` (futuro asistente) queda como nota
  y nunca se aplica (AC30).
- **Vigente** = `status=active` **y** dentro de `validity` (si hay ventana). «Expirado» es una propiedad derivada, no
  un estado.
- Promover una nota **crea un criterio nuevo** y desactiva la nota con motivo y vínculo (`source.note_criterion_id` +
  `source.note_version`); no se muta el registro original. Nota y criterio derivado **nunca** están activos a la vez.
- Un criterio nunca es una excepción a las validaciones técnicas ni una autorización de compra. «Siempre recomendar»
  es una preferencia comercial, no un permiso.

---

## 4. Estructuras y esquemas

### 4.1 Tipos comunes

```text
MoneyOut        { "amount": "3490.00", "currency": "CLP" }
UnitRefOut      { "code": "box", "original_text": "caja" }
QuantityOut     { "value": "5", "unit": "box" }
MeasureOut      { "label": "…", "as_written": {}, "normalized": null }   (reutiliza schemas.py:95-142)
ProvenanceOut   { "source": {…}, "mode": "…", "data_origin": "…", "observed_at": "…Z", "evidence": {…} }
FreshnessOut    { "observed_at": "…Z", "fresh_until": {…}, "is_stale": false, "stale_fields": [] }
```

### 4.2 `OfferOut` (E3; base de E5)

```json
{
  "offer_id": "uuid",
  "article_id": "uuid",
  "supplier": { "supplier_id": "uuid", "name": "Proveedor X", "status": "active" },
  "external_id": "SKU-123",
  "external_id_kind": "supplier_sku",
  "price": {
    "money": { "amount": "3490.00", "currency": "CLP" },
    "basis": "per_commercial_unit",
    "tax_included": "unknown",
    "discount_included": "unknown"
  },
  "price_per_base_unit": null,
  "sale_unit": { "code": "box", "original_text": "caja" },
  "pack": { "units_per_commercial_unit": "10", "base_unit": "unit", "original_text": "caja x 10" },
  "minimum": { "known": true, "quantity": "20", "unit": "box" },
  "multiple": { "known": true, "quantity": "7", "unit": "box" },
  "stock": { "status": "known", "quantity": "24", "unit": "box" },
  "taxes": [
    { "kind": "iva", "rate_percent": "19", "included_in_price": false }
  ],
  "discounts": [
    {
      "kind": "percent",
      "value": "10",
      "scope": "line",
      "applicability": "min_quantity",
      "threshold": { "quantity": "20", "unit": "box" },
      "condition_text": "10% desde 20 cajas"
    }
  ],
  "shipping_terms_id": "uuid",
  "validity": { "valid_from": null, "valid_to": null, "is_current": true },
  "notes": null,
  "provenance": {},
  "freshness": {}
}
```

| Campo | Oblig. | Notas |
|---|---|---|
| `offer_id`, `article_id`, `supplier`, `external_id`, `external_id_kind` | Sí | Identidad comercial |
| `price.money`, `price.basis`, `price.tax_included`, `price.discount_included` | Sí | Tri-estado obligatorio |
| `sale_unit` | Sí | `code` ∈ `QuantityUnit`; `original_text` como se dijo |
| `stock.status` | Sí | `known` \| `unknown`; `quantity` solo si `known` |
| `minimum`, `multiple` | Sí (objeto con `known`) | `known=false` = desconocido, no 0/1 |
| `taxes`, `discounts` | Sí (lista, puede ir vacía) | Solo se aplican las estructuras, nunca `condition_text` |
| `shipping_terms_id` | No | Referencia a `shipping_terms_snapshot`; sin él, despacho desconocido |
| `validity` | Sí | `is_current` calculado |
| `notes` | No | Texto del proveedor; **solo lectura** |
| `provenance`, `freshness` | Sí | §2.5 |
| `price_per_base_unit` | No | Calculado; `null` si falta contenido explícito |

`eligibility` **no** vive en la oferta: es resultado de la comparación (E5), porque depende de la cantidad pedida.

### 4.3 `ComparisonCandidateOut` (E5, por línea y oferta)

```json
{
  "candidate_id": "uuid",
  "line_id": "uuid",
  "offer_id": "uuid",
  "article_id": "uuid",
  "supplier": { "supplier_id": "uuid", "name": "Proveedor X", "status": "active" },
  "offer_price": {
    "money": { "amount": "3490.00", "currency": "CLP" },
    "basis": "per_commercial_unit",
    "tax_included": true
  },
  "quantity": {
    "requested": { "value": "20", "unit": "box" },
    "minimum": { "known": true, "quantity": "20", "unit": "box" },
    "multiple": { "known": true, "quantity": "7", "unit": "box" },
    "base": "20",
    "purchased": "21",
    "surplus": "1",
    "unit": "box",
    "adjustment": "multiple"
  },
  "line_gross": { "amount": "73290.00", "currency": "CLP" },
  "line_discounts": [
    {
      "kind": "percent",
      "value": "10",
      "amount": { "amount": "7329.00", "currency": "CLP" },
      "applicability": "min_quantity"
    }
  ],
  "line_tax_added": { "amount": "0", "currency": "CLP" },
  "line_payable": { "amount": "65961.00", "currency": "CLP" },
  "line_net": null,
  "eligibility": "eligible",
  "causes": [],
  "stock_status": "known",
  "stock_sufficient": true,
  "shipping_terms_id": "uuid",
  "discounted_is_possible": false,
  "annotations": [],
  "notes": [],
  "provenance": {},
  "freshness": {},
  "computed_at": "2026-10-29T12:00:00Z"
}
```

- El ejemplo aplica la fórmula de §2.4: 20 cajas pedidas, mínimo 20 (no eleva), múltiplo 7 → `base=20`,
  `purchased=21`, `surplus=1`, `adjustment="multiple"`. El descuento del 10% se activa porque se compran ≥ 20 cajas, y
  el stock (24) alcanza para lo **comprado** (21), no solo para lo pedido.
- `adjustment`: `none` | `minimum` | `multiple` | `minimum_and_multiple`; indica qué condición elevó la cantidad. Si
  `minimum` o `multiple` son desconocidos, la cantidad es una cota inferior y la causa correspondiente aparece en `causes`.
- `line_payable` es lo que se paga por la línea (con impuestos cuando corresponde) y es `null` si no se puede
  determinar. `line_net` es sin impuestos y es `null` si los impuestos están incluidos pero la tasa no se conoce.
- `causes` **siempre** presente (puede ir vacío). Las ofertas `ineligible` o `needs_review` **se devuelven**, nunca se
  filtran (AC18).
- `annotations` y `notes` son solo lectura y no alteran ningún número (§4.5 del E9).

### 4.4 `SupplierTotalOut` (E5)

```json
{
  "supplier_id": "uuid",
  "supplier_name": "Proveedor X",
  "supplier_status": "active",
  "currency": "CLP",
  "completeness": "partial",
  "lines_covered": ["uuid"],
  "lines_missing": [],
  "subtotal": { "amount": "65961.00", "currency": "CLP" },
  "taxes_included": true,
  "taxes_added": { "amount": "0", "currency": "CLP" },
  "discounts_total": { "amount": "7329.00", "currency": "CLP" },
  "shipping": { "scheme": "free_over_threshold", "known": false, "amount": null, "terms_id": "uuid" },
  "other_charges": [],
  "total": null,
  "lower_bound": { "amount": "65961.00", "currency": "CLP" },
  "bound_status": "valid",
  "bound_invalid_reasons": [],
  "blockers": ["shipping_unknown"],
  "causes": ["shipping_unknown"],
  "annotations": [],
  "notes": [],
  "freshness": { "oldest_observed_at": "…Z", "newest_observed_at": "…Z", "is_stale": false, "stale_lines": [] },
  "computed_at": "2026-10-29T12:00:00Z"
}
```

- `total` **solo** existe con `completeness=complete`. Un proveedor parcial nunca tiene total (AC19).
- `SupplierTotalOut` **no** tiene `eligibility`: la elegibilidad es del candidato (oferta × línea). A nivel de proveedor
  solo hay `completeness` (`complete` | `partial`), `blockers[]` y `causes[]`. Aparecer en `supplier_totals` **no**
  significa ser elegible ni poder ganar.
- `lower_bound` puede ser `null`; `bound_status` = `valid` | `not_boundable`, y `bound_invalid_reasons[]` explica.
- `subtotal` = Σ `line_after_discounts` (sin impuestos añadidos). `taxes_added` = impuestos no incluidos en el
  precio. `total = subtotal + taxes_added + shipping.amount + Σ other_charges − order_discounts`.

### 4.5 `CommercialCriterionOut` (E9)

```json
{
  "criterion_id": "uuid",
  "kind": "criterion",
  "status": "active",
  "origin": "explicit",
  "effect": "do_not_recommend",
  "target": {
    "level": "supplier_article",
    "supplier_id": "uuid",
    "article_id": "uuid",
    "ingredient": null
  },
  "condition": {
    "kind": "always",
    "min_quantity": null,
    "max_price": null,
    "date_window": null
  },
  "reason_text": "Problemas de entrega repetidos en septiembre; no conviene para este producto.",
  "source": { "kind": "evaluation", "comparison_id": "uuid", "offer_id": null, "note_criterion_id": null, "note_version": null },
  "validity": { "valid_from": null, "valid_until": null },
  "version": 1,
  "created_by": "uuid",
  "created_at": "2026-10-29T12:00:00Z",
  "confirmed_by": "uuid",
  "confirmed_at": "2026-10-29T12:00:00Z",
  "deactivated_by": null,
  "deactivated_at": null,
  "deactivation_reason": null,
  "priority": null
}
```

Reglas de forma:

- `target.level`: `organization` | `ingredient` | `article` | `supplier` | `supplier_article` (exige `supplier_id` y
  `article_id` o `ingredient`). `category` queda reservado al HLD §13.2 (no hay taxonomía todavía).
- `condition.kind`: `always` | `min_quantity` | `max_price` | `date_window`. `max_price` exige `MoneyOut` con
  `basis` y moneda comparables (HLD §13.4); sin base no se confirma la regla.
- `kind=note` ⇒ `effect=note_only`, `confirmed_by/at=null`, `origin` cualquiera, `target` y `reason_text`
  obligatorios.
- `kind=criterion` ⇒ `effect` ∈ {`prefer`,`deprioritize`,`do_not_recommend`,`requires_review`}, `origin=explicit`,
  `confirmed_by/at` obligatorios, `reason_text` no vacío.
- `source.kind`: `manual` (creado directo) | `evaluation` (desde una comparación) | `note` (derivado de una nota, con
  `note_criterion_id` y `note_version`).
- `status=inactive` ⇒ `deactivated_by/at/reason` obligatorios.
- Sin `proposed`: no hay estado intermedio ambiguo.

---

## 5. Operaciones

### E1 — `POST /api/v1/searches`

Búsqueda libre. Fase C4. Rol `admin`.

**Request**

```json
{
  "query": "ibuprofeno 400",
  "filters": {
    "ingredient": null,
    "brand": "Migranol",
    "laboratory": null,
    "form": null,
    "gtin": null
  },
  "limit": 20
}
```

- `query`: 3–200 caracteres; se conserva tal cual.
- `filters`: opcional, **conjunto cerrado** de claves; una clave desconocida → 400 `VALIDATION_ERROR`. Solo
  restringen; nunca completan datos faltantes.
- `limit`: 1–50, por defecto 20.

**Response 200**

```json
{
  "search_id": "uuid",
  "status": "completed",
  "query": {
    "original_text": "ibuprofeno 400",
    "interpretation": {
      "attributes": [],
      "unresolved_terms": ["400"],
      "warnings": ["cifra sin unidad: no se asume mg"]
    }
  },
  "generated_at": "2026-10-29T12:00:00Z",
  "results": [],
  "sources": [],
  "errors": [],
  "next_cursor": null
}
```

`SearchResultOut` (cada elemento de `results`):

```json
{
  "article_id": "uuid",
  "display_name": "Ibuprofeno 400 mg comprimidos, caja 10",
  "identity_summary": "ibuprofeno 400 mg · comprimido",
  "presentation": { "commercial_unit": { "code": "box", "original_text": "caja" }, "units_per_commercial_unit": "10" },
  "match": { "level": "exact", "causes": [] },
  "offer_count": 3,
  "supplier_count": 2,
  "lowest_verified_price": { "amount": "3490.00", "currency": "CLP" },
  "sources": ["uuid"]
}
```

- `status`: `completed` (todas las fuentes habilitadas respondieron), `partial` (al menos una falló y otra entregó
  datos) o `failed` (ninguna entregó datos utilizables). Siempre HTTP 200; el detalle va en `sources` y `errors`.
- `query.interpretation` muestra lo entendido y lo **no** entendido; nunca inventa unidad ni rol.
- `sources[]`: subconjunto de `SourceOut` con su estado (§E4).
- `errors[]`: `{ "source_id": "uuid", "code": "TIMEOUT", "message": "…", "retryable": true }`.
- `next_cursor`: paginación por cursor (`created_at` + `id`, ya existente en `backend/app/services/queries.py:15-24`);
  cursor inválido → 400 `INVALID_CURSOR`.
- `202 + job_id` **no se usa en v0**; queda reservado para cuando existan conectores externos que lo justifiquen
  (D-075, HLD §14.3).
- Si la búsqueda excede el tiempo objetivo: 503 `SEARCH_TIMEOUT` (error tipado, D-075).

### E2 — `GET /api/v1/searches/{search_id}`

Recupera el resultado almacenado de una búsqueda. Fase C4. Rol `admin`.

- Misma respuesta que E1. 404 si no existe o es de otra organización.
- No reejecuta la búsqueda: devuelve el snapshot con sus `observed_at` y `fresh_until`.

### E3 — `GET /api/v1/articles/{article_id}`

Detalle de un artículo. Fase C4. Rol `admin`.

```json
{
  "article": {
    "article_id": "uuid",
    "identity": {
      "ingredient": "ibuprofeno",
      "strengths": [],
      "form": { "value": "comprimido", "basis": "explicit" },
      "route": null,
      "release": null,
      "brand": "Migranol",
      "laboratory": "Laboratorio Bagó de Chile S.A.",
      "gtin": null,
      "sanitary_registration": null
    },
    "presentation": {
      "packaging": { "value": "caja", "basis": "explicit" },
      "content": { "value": "10", "unit": "comprimido" },
      "commercial_unit": { "code": "box", "original_text": "caja" },
      "units_per_commercial_unit": "10"
    },
    "match": { "level": "exact", "causes": [] }
  },
  "offers": [],
  "offers_total": 0,
  "next_cursor": null,
  "reference_prices": [],
  "annotations": [],
  "notes": [],
  "sources": [],
  "generated_at": "2026-10-29T12:00:00Z"
}
```

- `identity.strengths` reutiliza `StrengthOut`; `content` reutiliza `ContentOut`; `form`, `packaging`, `release`
  reutilizan `TextAttrOut` (`schemas.py:95-142`).
- `match.level`: `exact` | `equivalent_authorized` | `needs_review`. En v0 no hay equivalencias autorizadas
  configuradas: todo lo no exacto es `needs_review` con causas.
- `offers`: últimas ofertas por fuente, máximo 50 por página; `offers_total` + `next_cursor`.
- `reference_prices`: reservado; en C4 vacío con la fuente en `not_configured`.
- `annotations` / `notes`: criterios y notas **vigentes** cuyo ámbito alcanza a este artículo (§E9).

### E4 — `GET /api/v1/sources`

Fuentes de la organización y su estado. Fase C2. Rol `admin`. Equivale a `GET /connectors/health` del HLD §14.1.

```json
{
  "sources": [
    {
      "source_id": "uuid",
      "name": "Lista proveedor X",
      "kind": "supplier",
      "supplier_id": "uuid",
      "mode": "manual_import",
      "capabilities": ["search", "price", "stock", "terms"],
      "enabled": true,
      "status": "ok",
      "has_secret": false,
      "last_success_at": "2026-10-29T11:00:00Z",
      "last_error": null
    }
  ]
}
```

- `status`: `ok` | `stale` | `failed` | `not_configured`.
  - `not_configured`: la fuente no tiene credenciales ni configuración para consultar.
  - `stale`: la última observación existe pero superó su umbral de frescura.
  - `failed`: el último intento falló.
  - `ok`: hay observación vigente y el último intento no falló.
- Nunca devuelve secretos, tokens ni credenciales; solo `has_secret: true|false`.

### E5 — `POST /api/v1/comparisons`

Comparación determinista por línea. Fase C3. Rol `admin`.

**Request**

```json
{
  "lines": [
    { "article_id": "uuid", "quantity": { "value": "20", "unit": "box" } }
  ],
  "source_search_id": "uuid"
}
```

- 1–50 líneas. `quantity.value` entero > 0; si falta o es fraccionario → 422 `QUANTITY_UNKNOWN` /
  `INVALID_QUANTITY`.
- `source_search_id` **opcional a nivel de la comparación** (no por línea): pista para reutilizar ofertas de una
  búsqueda reciente. No omite ninguna validación ni la verificación de frescura.

**Tres niveles de resultado (no confundir):**

| Nivel | Qué es | Estado | ¿Tiene `total`? | ¿Puede ganar? |
|---|---|---|---|---|
| **Candidato** | Un par (línea, oferta) | `eligibility`: `eligible` \| `needs_review` \| `ineligible` + `causes[]` | — (solo `line_payable`) | — |
| **Proveedor con total completo verificable** | Toda línea cubierta por un candidato `eligible` y todos los costos determinados | `completeness=complete`, `blockers=[]` | Sí | Sí, si es el menor total verificado |
| **Proveedor con total parcial** | Al menos una línea cubierta, pero falta cobertura o un costo determinable | `completeness=partial`, `blockers≠[]` | **No** (`total=null`) | **No** |

- `supplier_totals[]` contiene a los dos tipos de proveedor. **Estar ahí no es ser elegible**: la elegibilidad es por
  candidato; la completitud y el total son del proveedor. C0-06 no se contradice: solo candidatos `eligible` pueden
  componer un total `complete`, y solo un proveedor `complete` es verificable (AC19).
- Un proveedor con un candidato `needs_review` no puede ser `complete`; queda `partial` con la causa correspondiente.
- `discarded_suppliers[]`: proveedores cuyos candidatos son **todos** `ineligible` por causa dura; no tienen líneas
  cubiertas, no tienen cota y no participan de nada.
- Los proveedores parciales solo pueden aparecer en `could_be_beaten_by` o `unknown_relation`; nunca en
  `lowest_verified_total`.

**Reglas de cálculo** (D-088; todas deterministas):

```text
qty = purchased (ver §2.4)

gross = qty × price.amount                                        (per_commercial_unit)
gross = qty × pack.units_per_commercial_unit × price.amount       (per_base_unit; pack explícito requerido)

line_discounts = Σ descuentos aplicables
    percent: gross × value / 100
    amount (scope=line): value
    solo applicability=always o min_quantity cumplido
line_after_discounts = gross − line_discounts

tax_included = true:   line_tax_added = 0;  line_net = rate conocida ? after / (1+rate/100) : null
tax_included = false:  line_tax_added = Σ after × rate/100 (todas las tasas conocidas); line_net = after
tax_included = unknown: line_payable = null → partial (taxes_unknown)

subtotal       = Σ line_after_discounts
taxes_added    = Σ line_tax_added
shipping_base  = subtotal + taxes_added + Σ other_charges − order_discounts
shipping_amount= según scheme evaluado sobre shipping_base (free_over_threshold compara contra shipping_base)
total          = subtotal + taxes_added + Σ other_charges + shipping_amount − order_discounts
```

Reglas adicionales:

- `discount_included=true` ⇒ no se restan descuentos (están en el precio); `unknown` con descuentos aplicables ⇒
  parcial (`discount_inclusion_unknown`); `applicability=unknown` ⇒ parcial (`discount_conditions_unknown`).
- `free_over_threshold`: si `shipping_base >= threshold` → 0; si no → `flat_amount`. Un umbral no evaluable ⇒
  `shipping_unknown`.
- Monedas distintas entre líneas, despacho u otros cargos ⇒ 422 `CURRENCY_MISMATCH` y total parcial.
- Una condición comercial no modelada que afecte la compra ⇒ `needs_review` con `order_minimum_unmodelled` o
  `unmodelled_condition`. Nunca se interpreta texto para resolverla.

**Cota inferior** (`lower_bound`, D-087). Es válida solo si se cumplen **todas**:

| # | Condición | Si falla |
|---|---|---|
| LB-1 | Una sola moneda en todos los componentes | `not_boundable` |
| LB-2 | Cada línea cubierta por una oferta sin causa dura; precio conocido; base resoluble (`per_base_unit` exige `pack`) | `not_boundable` |
| LB-3 | Cantidad determinada; `minimum`/`multiple` desconocidos solo pueden aumentar la cantidad | válida salvo LB-6 |
| LB-4 | `tax_included=unknown` se trata como incluido; impuesto no incluido y sin tasa suma 0 | válida |
| LB-5 | Ningún descuento desconocido: `discount_included ≠ unknown` si hay descuentos aplicables; ningún `applicability=unknown` | `not_boundable` |
| LB-6 | Si `minimum` o `multiple` es desconocido, **no** existe descuento por umbral (podría activarse y bajar el total) | `not_boundable` |
| LB-7 | Descuentos restados solo si están cumplidos y no doble-contados | `not_boundable` |
| LB-8 | La cota es sobre el **snapshot observado** (fecha), no sobre el mercado de hoy; `stale` se muestra | siempre válida |

```text
lower_bound = subtotal_mínimo + cargos_conocidos + despacho_conocido − descuentos_de_pedido_conocidos
```

Los impuestos, cargos y descuentos **desconocidos** se tratan así: impuestos y cargos → 0 (solo suman); descuentos →
**no** se asumen 0, porque restan (LB-5/LB-6). Esa es la diferencia que hace válida la cota.

**Response 200**

```json
{
  "comparison_id": "uuid",
  "status": "partial_coverage",
  "scope": {
    "suppliers_evaluated": 4,
    "suppliers_complete": 2,
    "suppliers_incomplete": 2,
    "sources_consulted": 5,
    "sources_failed": ["uuid"],
    "criteria_evaluated_at": "2026-10-29T12:00:00Z"
  },
  "lines": [
    {
      "line_id": "uuid",
      "article_id": "uuid",
      "quantity": { "value": "20", "unit": "box" },
      "candidates": [],
      "completeness": "complete",
      "blockers": []
    }
  ],
  "supplier_totals": [],
  "discarded_suppliers": [],
  "lowest_verified_total": {
    "supplier_id": "uuid",
    "total": { "amount": "48200.00", "currency": "CLP" },
    "is_global_claim": false,
    "claim": "lowest_verified_total_among_evaluated"
  },
  "could_be_beaten_by": [
    { "supplier_id": "uuid", "lower_bound": { "amount": "41000.00", "currency": "CLP" }, "blockers": ["shipping_unknown"] }
  ],
  "unknown_relation": [
    { "supplier_id": "uuid", "blockers": ["discount_conditions_unknown"] }
  ],
  "recommendation": null,
  "recommendation_status": "not_evaluated",
  "annotations": [],
  "notes": [],
  "sources": [],
  "generated_at": "2026-10-29T12:00:00Z"
}
```

> Ejemplo abreviado: `lines[].candidates`, `supplier_totals`, `discarded_suppliers`, `sources`, `annotations` y `notes`
> se muestran vacíos por brevedad. En una respuesta real `supplier_totals` incluye los cuatro proveedores que declara
> `scope` (dos completos y dos parciales) y `lines[].candidates` trae las ofertas con sus causas.

Reglas de respuesta:

- `status`: `failed` (ninguna fuente entregó datos) > `no_complete_total` (hay datos pero ningún total completo
  elegible) > `partial_coverage` (hay al menos un total completo y algún proveedor o fuente incompleta) > `complete`
  (todos los proveedores evaluados completos y todas las fuentes respondieron). El orden es precedencia.
- `lines[].completeness`: `complete` (algún candidato `eligible` sin causas bloqueantes), `partial` (solo candidatos
  `needs_review`) o `unavailable` (solo `ineligible`). `lines[].blockers[]` reúne las causas que impiden el total
  completo de esa línea; es `[]` cuando es `complete`.
- `supplier_totals`: proveedores no descartados, con cobertura total o parcial. Incluye parciales para que la UI
  muestre cobertura.
- `discarded_suppliers`: proveedores con causa **dura** (`identity_mismatch`, `presentation_mismatch`,
  `supplier_inactive`), con sus causas (AC18). No participan de totales ni de cotas.
- `lowest_verified_total`: el menor `total` entre proveedores `completeness=complete` (`blockers` vacío);
  `null` solo si no hay ninguno. `is_global_claim` es siempre `false`: no se afirma «el más barato del mercado» ni
  «el menor costo total» (AC19).
- `could_be_beaten_by`: proveedores con `bound_status=valid` y `lower_bound` menor que el total ganador (la cota y los
  `blockers` se toman de su entrada en `supplier_totals`). `unknown_relation`: proveedores incompletos sin cota válida,
  que por lo tanto no se pueden descartar. Ninguno de los dos incluye proveedores de `discarded_suppliers`.
- `recommendation` queda `null` y `recommendation_status="not_evaluated"` en v0; el campo está reservado para H4.
- `annotations[]` (criterios confirmados vigentes) y `notes[]` (notas vigentes) se adjuntan por candidato y por
  proveedor, y también a nivel de comparación. No alteran **ningún** número ni el orden (D-091, C0-18).

**Causas tipadas** (vocabulario cerrado, usado en `causes`, `blockers` y `bound_invalid_reasons`):
`identity_mismatch`, `presentation_mismatch`, `quantity_unit_mismatch`, `price_stale`, `taxes_unknown`,
`price_basis_unknown`, `discount_inclusion_unknown`, `discount_conditions_unknown`, `stock_unknown`,
`stock_insufficient`, `minimum_unknown`, `minimum_not_met`, `multiple_unknown`, `multiple_not_met`,
`shipping_unknown`, `order_minimum_unmodelled`, `unmodelled_condition`, `currency_mismatch`, `source_failed`,
`supplier_inactive`.

### E6 — Proveedores

Fase C2. Rol `admin`. Entidades administradas por el mismo jefe que usa el cotizador.

| Operación | Request | Response |
|---|---|---|
| `GET /api/v1/suppliers` | — | `{ "suppliers": [SupplierOut] }` |
| `GET /api/v1/suppliers/{supplier_id}` | — | `SupplierOut` |
| `POST /api/v1/suppliers` | `{ "name": "..." }` (`Idempotency-Key` opcional) | 201 `SupplierOut` |
| `PATCH /api/v1/suppliers/{supplier_id}` | `{ "name"?: "...", "status"?: "active"\|"inactive" }` + `If-Match` | `SupplierOut` |

```json
{
  "supplier_id": "uuid",
  "name": "Proveedor X",
  "status": "active",
  "version": 1,
  "sources": ["uuid"],
  "known_conditions": [
    { "text": "despacho gratis sobre 10 cajas", "observed_at": "2026-10-29T11:00:00Z", "source_id": "uuid" }
  ],
  "annotations": [],
  "notes": []
}
```

- `known_conditions` es texto **como se dijo**, con fecha y fuente; **no alimenta ningún cálculo** (el despacho se
  calcula solo con `shipping_terms_snapshot` tipado).
- `annotations` / `notes`: criterios y notas vigentes cuyo ámbito es este proveedor.
- Nombre único normalizado por organización; duplicado activo → 409 `DUPLICATE_NAME`.
- Desactivar un proveedor no borra ofertas ni historial.

### E7 — Fuentes

Fase C2. Rol `admin`.

| Operación | Request | Response |
|---|---|---|
| `POST /api/v1/sources` | `{ "name", "kind": "supplier"\|"chain_reference", "supplier_id"?, "mode", "capabilities": [] }` | 201 `SourceOut` |
| `PATCH /api/v1/sources/{source_id}` | `{ "name"?, "mode"?, "capabilities"?, "enabled"? }` + `If-Match` | `SourceOut` |

Validaciones:

- `kind=supplier` exige `supplier_id`; `kind=chain_reference` lo prohíbe.
- `mode=fixture` solo en `simulation`; si no, 422 `FIXTURE_NOT_ALLOWED`.
- Capacidades permitidas en v0: `search`, `product_detail`, `price`, `stock`, `terms`. Cualquier capacidad transaccional
  (`remote_cart`, `submit_order`, `order_status`, `cancel_order`) responde 422 `SOURCE_CAPABILITY_NOT_ALLOWED`
  (HLD §5.1, compra bloqueada).
- Una fuente de cadena no puede declarar `terms` de compra ni capacidades transaccionales. El despacho que informa
  una cadena viaja como condición de la **referencia**, no como capacidad de compra.
- `SourceOut` incluye `has_secret` booleano y nunca el secreto.

### E8 — `POST /api/v1/imports`

Importación de lista de precios Excel/CSV. Fase C2. Rol `admin`. **Sobre congelado; columnas y mapeo pendientes de
D-077** (D-090).

**Congelado en v0:**

- `Content-Type: multipart/form-data`; campos `file` (obligatorio), `supplier_id` (obligatorio), `source_id`
  (**obligatorio**; no se crea ninguna fuente de forma implícita).
- Cabecera `Idempotency-Key` obligatoria, 8–128 caracteres. Misma clave + mismo hash de archivo → resultado original;
  contenido distinto → 409 `IDEMPOTENCY_KEY_REUSED`.
- Límites: 10 MB, 20.000 filas; si excede, 413 `PAYLOAD_TOO_LARGE`.
- Respuesta **siempre 200** con resultado parcial; 422 `IMPORT_FORMAT_INVALID` solo si el archivo no se puede abrir o
  no tiene cabecera.
- `status`: `completed` | `completed_with_errors` | `failed`.
- Las ofertas importadas llevan `mode="manual_import"`, `data_origin="captured"` y `observed_at` del archivo si lo
  trae, o del servidor si no.
- Importar **no** confirma identidad farmacéutica, no crea artículos por inferencia y no autoriza compra.
- Una fila rechazada nunca se descarta en silencio; se informa con código y número de fila.
- Filas cuyo producto no está mapeado se rechazan con `UNMAPPED_PRODUCT` (v0 no inventa identidad de artículo).
- Clave duplicada (`source_id` + `external_id`) dentro del archivo: se rechaza la segunda (`DUPLICATE_ROW`), no se
  sobrescribe.
- Condición comercial no modelada en el archivo → fila aceptada pero oferta `needs_review` con
  `UNMODELLED_CONDITION`.

```json
{
  "import_id": "uuid",
  "status": "completed_with_errors",
  "rows_total": 120,
  "rows_accepted": 118,
  "rows_rejected": 2,
  "errors": [
    { "row": 14, "code": "INVALID_PRICE", "message": "La fila no trae un precio decimal válido." }
  ],
  "observed_at": "2026-10-29T12:00:00Z"
}
```

**Diferido a D-077:** nombres y orden de columnas, campos obligatorios, identificación del producto (SKU, nombre,
GTIN), si el precio incluye IVA, descuentos y despacho en la planilla, vocabulario de unidades del archivo, formato
(`.xlsx`/`.csv`), hojas múltiples, separador decimal, codificación, formatos de fecha y regla de creación de
`supplier_product_mapping`.

### E9 — Criterios comerciales y notas

Fase C4 (modelo en C1, anotación en C3). Rol `admin`. Contrato de D-091.

| Operación | Request | Response |
|---|---|---|
| `GET /api/v1/commercial-criteria` | `?kind=&status=&target_level=&supplier_id=&article_id=&include_inactive=` | `{ "criteria": [CommercialCriterionOut] }` |
| `GET /api/v1/commercial-criteria/{criterion_id}` | — | `CommercialCriterionOut` |
| `POST /api/v1/commercial-criteria` | `{ kind, confirm?, target, reason_text, effect?, condition?, validity?, source? }` (`source` solo `comparison_id`/`offer_id`) | 201 `CommercialCriterionOut` |
| `PATCH /api/v1/commercial-criteria/{criterion_id}` | `{ target?, reason_text?, condition?, validity? }` + `If-Match` | `CommercialCriterionOut` |
| `POST /api/v1/commercial-criteria/{criterion_id}/confirm` | `{ effect, target?, reason_text?, condition?, validity? }` + `If-Match` | 201 `CommercialCriterionOut` (criterio nuevo) |
| `POST /api/v1/commercial-criteria/{criterion_id}/deactivate` | `{ reason }` + `If-Match` | `CommercialCriterionOut` |
| `POST /api/v1/commercial-criteria/{criterion_id}/reactivate` | — + `If-Match` | `CommercialCriterionOut` |

Validaciones:

- `kind=note` → `effect` solo `note_only`; si otro, 422 `CRITERION_EFFECT_NOT_ALLOWED`.
- `kind=criterion` → `effect` ∈ {`prefer`,`deprioritize`,`do_not_recommend`,`requires_review`}, `origin=explicit`,
  `reason_text` y `target` obligatorios, y el servidor escribe `confirmed_by/at` del actor. Si falta algo → 422
  `CRITERION_INCOMPLETE`.
- `confirm` solo aplica a `kind=note` y `status=active`; si no, 409 `CRITERION_NOT_NOTE` / `CRITERION_NOT_ACTIVE`.
  Crea un **registro nuevo** `kind=criterion` con `source.note_criterion_id` + `source.note_version`, y deja la nota
  `inactive` con `deactivation_reason="convertida en criterio <id>"`. No muta el texto original.
- `deactivate` exige `reason`, y `reactivate` solo sobre `status=inactive`. Redundantes → 409 `CRITERION_NOT_ACTIVE`.
- `PATCH` sobre un criterio confirmado **cambia versión** (`version+1`) y se audita; no se muta en silencio porque
  afecta recomendaciones futuras (AC39 por coherencia).
- `condition.kind=max_price` exige `MoneyOut` con `basis` y moneda; sin ellos no se confirma la regla.
- Toda alta, edición, confirmación y desactivación emite auditoría y evento de outbox en la misma transacción.

**Creación directa de un criterio (sin nota previa):**

- `POST` con `kind=criterion` es una acción **explícita y consciente del ADMIN**: además de `effect`, `target`,
  `reason_text` y `condition`, el cuerpo debe incluir `"confirm": true`. Sin `confirm: true` → 422
  `CRITERION_INCOMPLETE`. No se crea un criterio por defecto ni se convierte una nota en criterio sin `confirm`.
- El servidor fija `origin=explicit`, `created_by/at`, `confirmed_by/at` del actor y `source.kind` (`evaluation` si el
  cuerpo trae `source.comparison_id` u `source.offer_id`; `manual` si no). El cliente no puede enviar `source.kind`,
  `confirmed_by/at`, `status` ni `version`.
- `kind=note` no acepta `confirm` y nunca crea un criterio.

**Concurrencia (coherente con §2.6):**

- `PATCH`, `confirm`, `deactivate` y `reactivate` exigen `If-Match: "<version>"`; si falta, 428 `VERSION_REQUIRED`; si
  no coincide, 409 `VERSION_CONFLICT` con `details.current_version`. Toda transición sube `version` en 1.
- Las transiciones son **condicionales al estado y a la versión esperados** dentro de la transacción. Dos solicitudes
  simultáneas no pueden confirmar ni desactivar dos veces el mismo registro: la segunda falla con 409 y no deja
  auditoría duplicada.
- `POST` crea siempre `version=1`. Una `Idempotency-Key` repetida con el mismo cuerpo devuelve el registro original; con
  cuerpo distinto, 409 `IDEMPOTENCY_KEY_REUSED`.

**Trazabilidad de una nota convertida en criterio:**

- Confirmar una nota **no la borra ni la muta**: conserva texto, autor, fechas y versión, y sigue consultable con
  `include_inactive=true`.
- El criterio guarda `source.note_criterion_id` y `source.note_version` (la versión exacta convertida); la nota guarda
  en `deactivation_reason` la referencia al criterio. Ambos registros comparten la auditoría `criterion.confirmed` en la
  misma transacción, con el actor y el antes/después.
- **Un criterio derivado por nota** (restricción única sobre `source.note_criterion_id`). Un segundo `confirm` sobre la
  misma nota responde 409 `CRITERION_NOT_ACTIVE`.
- **Nunca hay dos anotaciones activas del mismo contenido**: una nota y su criterio derivado no pueden estar ambos
  `active`. Reactivar la nota con el criterio derivado vigente —o reactivar el criterio con la nota reactivada—
  responde 409 `CRITERION_NOTE_SUPERSEDED`; primero se desactiva el otro registro.
- Desactivar un criterio **no** reactiva la nota automáticamente. Reactivar la nota con su criterio derivado inactivo la
  devuelve a `notes[]` (el criterio inactivo no aparece en `annotations[]`), conservando el vínculo para el historial.

**Alcance del ámbito (nunca más amplio de lo confirmado):**

| `target.level` | Aplica a | Nunca aplica a |
|---|---|---|
| `organization` | todos los candidatos de la organización | — |
| `ingredient` | artículos cuyo principio activo coincida exactamente y cuya procedencia sea `explicit` o `confirmed` | artículos con principio `inferred` o desconocido (se muestra `condition_status="unknown"`, no como aplicado); no se extiende por marca, laboratorio ni sinónimo no verificado |
| `article` | exactamente ese `article_id` | otra presentación del mismo producto (otro envase, dosis o contenido), aunque comparta principio y marca |
| `supplier` | todos los candidatos de ese proveedor | cualquier otro proveedor, aunque venda el mismo artículo |
| `supplier_article` | solo el par (`supplier_id`, `article_id`) | el mismo proveedor con otros artículos, o el mismo artículo con otros proveedores |

- En este contrato **artículo = presentación comercial** (D-080): no existe un nivel «presentación» separado. Un criterio
  de presentación se expresa con `article` sobre el `article_id` exacto; nunca se compara texto de envase, dosis o contenido.
- No hay herencia entre ámbitos: un criterio de `ingredient` no se hereda a `article`, ni uno de `supplier` a
  `supplier_article`.
- `condition` se evalúa **además** del ámbito (AND); nunca lo amplía. Si no es evaluable, `condition_status="unknown"` y
  no se considera cumplido.
- `category` no se acepta en v0: 422 `VALIDATION_ERROR` (no hay taxonomía).

**Aplicación (solo anotación):**

- Un criterio aplica solo si su `target` coincide según la tabla de alcance anterior y su `condition` es evaluable y
  se cumple.
- `annotations[]` de E3/E5/E6:
  `{ criterion_id, effect, reason_text, target_level, condition, condition_met, condition_status, confirmed_by, confirmed_at, version }`.
  `condition_met` es `null` con `condition_status="unknown"` cuando no se puede evaluar.
- `notes[]`: `{ criterion_id, origin, reason_text, target_level, created_by, created_at, version }`.
- **Ningún** criterio altera `price`, `line_payable`, `subtotal`, `total`, `lower_bound`, `eligibility`, `causes`,
  `lowest_verified_total`, `offers_total` ni el orden de los candidatos. Ninguna anotación elimina una oferta.
- La recomendación que sí los usa queda en H4 (`recommendation`, `recommendation_status`), con explicación y
  `criteria_applied[]`; el campo se reserva vacío aquí.

---

## 6. Seguridad y permisos (ADMIN único)

Decisión D-079 y D-086: el cotizador es una herramienta **interna** usada solo por los jefes de la farmacia.

| Operación | Rol | Sesión |
|---|---|---|
| E1–E5 (lectura y cálculo) | `admin` | `StrongActor` |
| E6 (lectura y escritura) | `admin` | `StrongActor` |
| E7 (lectura y escritura) | `admin` | `StrongActor` |
| E8 (importación) | `admin` | `StrongActor` |
| E9 (criterios y notas) | `admin` | `StrongActor` |

- Dos comprobaciones independientes: rol `admin` (chequeo de rol, sin permisos nuevos) y `StrongActor` (§2.2).
- No se crea rol `purchasing` para el cotizador ni permisos diferenciados de lectura/escritura. Cualquier otro rol
  recibe 403 `FORBIDDEN`.
- La organización sale del `Actor`. No se acepta en el cuerpo.
- `real_test`/`production` exigen JWT de Supabase (no `X-Demo-User-Id`).
- Los roles de otros módulos (Telegram, faltantes, personal) **no cambian**.

---

## 7. Reglas no negociables aplicadas al contrato

| Invariante / AC | Cómo se cumple en el contrato |
|---|---|
| Invariante 7, AC18 | `eligibility` con causas **por candidato**; solo un candidato `eligible` compone un total `complete`, y solo un proveedor `complete` puede ganar |
| Invariante 8, AC20 | `offers[]` y `reference_prices[]` separados; cadenas fuera de totales y del ganador |
| AC17 | `sources[].status` y `errors[]`; `failed`/`partial` explícitos; nunca «sin ofertas» por caída |
| AC19 | Parcial sin `total`; `lowest_verified_total` acotado y `is_global_claim=false`; `lower_bound` solo con LB-1…LB-8 |
| AC29 | `condition` estructurada con base y moneda; sin base no se confirma |
| AC30 | `origin=inferred` queda como nota y nunca se aplica; confirmación explícita |
| AC31 | Toda anotación lleva `criterion_id` + `version`; la comparación registra `criteria_evaluated_at` |
| AC31 / D-091 (trazabilidad) | Nota → criterio con `source.note_criterion_id` + `source.note_version`; la nota se conserva; una sola anotación activa por contenido; toda transición con `If-Match` |
| HLD §7.1 | Artículo = presentación; no se fusionan dosis, formas ni envases |
| HLD §9.2 | Descuentos sin doble resta; despacho a nivel pedido; sobrante visible; total parcial si falta un costo |
| HLD §13.2 | Criterios con condición, efecto, ámbito, vigencia, responsable y origen; no anulan controles |
| HLD §22 | `mode`, `data_origin` y `observed_at` por dato; `fixture`/`simulated` visibles y restringidos a `simulation` |
| HLD §2 (compra) | Ninguna operación crea carrito, propuesta, aprobación ni ejecución; un criterio no autoriza compra |

---

## 8. Matriz de trazabilidad

| Operación | HLD | AC | Fase | Rol |
|---|---|---|---|---|
| E1 `POST /searches` | §5.2, §5.3, §14.1 | AC16, AC17 | C4 | admin |
| E2 `GET /searches/{id}` | §5.2, §6.2, §14.1 | AC16, AC17 | C4 | admin |
| E3 `GET /articles/{id}` | §6.2 `trade_item`, §7.1 | AC18, AC31 | C4 | admin |
| E4 `GET /sources` | §5.1, §5.2, §14.1 (`/connectors/health`) | AC16, AC17 | C2 | admin |
| E5 `POST /comparisons` | §9.1–§9.4, §14.1 | AC18, AC19, AC31 (base) | C3 | admin |
| E6 proveedores | §6.2 `supplier`, §17.1 | AC16, AC31 | C2 | admin |
| E7 fuentes | §5.1, §5.2, §6.2 `source` | AC16, AC17 | C2 | admin |
| E8 importación | D-077 (no en el HLD) | AC16 | C2 | admin |
| E9 criterios y notas | §13.1–§13.3, §6.2 `memory_fact`, §14.1 (`/rules`, `/rule-proposals`) | AC29, AC30, AC31 | C4 (modelo C1, anotación C3) | admin |

**Campos y su origen**

| Campo | HLD |
|---|---|
| `price.basis`, `tax_included`, `discount_included` | §9.2 (430) |
| `stock.status` conocido/desconocido | §9.1 (415) |
| `fresh_until`, `observed_at` | §5.4 (267) |
| `eligibility.level` y causas | §9.1 (420) |
| `subtotal`, `total`, parciales, sobrante | §9.2 (426-434) |
| Condiciones de cadena | §9.3 (438-440) |
| `mode`, `data_origin`, evidencia | §5.2 (255), §22 (879) |
| `criterion.kind`, `effect`, `target`, `validity` | §13.2 (521-527) |
| Nota vs regla confirmada | §13.3 (533-535), AC30 |

---

## 9. Fuera de v0 (no se define aquí)

- Propuestas masivas, asignaciones, carritos, aprobaciones, ejecución, recepción (H4/H5).
- **Aplicación** de criterios: ordenamiento por preferencia, reglas duras, recomendación explicada, memoria comercial
  y aprendizaje conversacional (H4, HLD §13.3). En v0 solo registro y anotación.
- Rangos de precio y presupuestos (H4).
- Crédito y cupos (D-085).
- Margen y precio de venta propio (HLD §9.3).
- Ingesta real de cadenas (C6): el contrato de `reference_prices` queda reservado.
- SSE y tiempo real (H1-C).
- IA, visión y OCR en el cotizador.
- Conectores `live` con portales privados o APIs de proveedores.
- Roles distintos de `admin` para el cotizador.

---

## 10. Criterios de aceptación de C0

C0 se cierra por **revisión del documento**, no por pruebas. Criterios C0-01 a C0-19 (también en `docs/ROADMAP.md`).
**Los 19 criterios se verificaron y el usuario aprobó el contrato el 2026-10-29: C0 queda cerrada y el contrato
congelado para Lovable (C0-13, D-093).**

| # | Criterio | Verificación |
|---|---|---|
| C0-01 | E1–E9 tienen método, ruta, request, response, errores y fase | Revisión §5 y §8 |
| C0-02 | Cada operación y campo declara origen en el HLD o se marca como derivado | §8 |
| C0-03 | Todo dato comercial lleva `source`, `mode`, `data_origin`, `observed_at`, evidencia y frescura | §2.5, §4.1 |
| C0-04 | Dinero como string decimal con moneda, base, impuestos y descuentos; nunca float | §2.4 |
| C0-05 | `offers[]` y `reference_prices[]` separados; sin ranking cruzado | §3.2, §3.3, §7 |
| C0-06 | Elegibilidad tri-estado **por candidato** con causas; solo `eligible` compone un total `complete`; aparecer en `supplier_totals` no implica elegibilidad | §E5, §7 |
| C0-07 | Parcial nunca tiene `total` ni puede ganar; `lowest_verified_total` acotado con `is_global_claim=false` y cotas LB-1…LB-8 | §E5 |
| C0-08 | Estados `ok/stale/failed/not_configured`; caída ≠ sin ofertas | §E1, §E4 |
| C0-09 | Seguridad declarada: Bearer, organización del `sub`, 404 entre organizaciones, rol `admin` + `StrongActor` | §2.2, §6 |
| C0-10 | Ninguna operación crea carrito, propuesta, aprobación ni ejecución | §1, §7 |
| C0-11 | «Fuera de v0» explícito | §9 |
| C0-12 | Decisiones D-078 a D-093 registradas; D-076 y D-077 diferidas | `docs/decisions/` |
| C0-13 | Revisión y aprobación final del usuario; solo entonces se congela para Lovable | **Cumplido el 2026-10-29**: aprobación formal y congelamiento (D-093) |
| C0-14 | Unidades y cantidades: `QuantityUnit` único, `requested` entero, `minimum`/`multiple` con `known`, stock contra `purchased`, sobrante y `adjustment` explícito | §2.4, §4.3 |
| C0-15 | Aritmética determinista: impuestos, descuentos sin doble resta, despacho por esquema, otros cargos, moneda y redondeo | §E5 |
| C0-16 | E8: sobre congelado (transporte, idempotencia, códigos de fila, parciales); columnas diferidas a D-077 | §E8 |
| C0-17 | Contrato de criterios completo: esquema, ámbitos, efectos, ciclo de vida, **trazabilidad nota → criterio**, **concurrencia por `If-Match`** y creación directa con `confirm: true` | §3.5, §4.5, §E9 |
| C0-18 | No interferencia comprobable: un criterio no altera precios, totales, elegibilidad ni visibilidad | §3.5, §E9, §7 |
| C0-19 | Nota vs criterio confirmado sin estados ambiguos: no existe `proposed`; nada se auto-promueve y **una nota y su criterio nunca están activos a la vez** | §3.5, §E9 |

---

## 11. Pendientes que no bloquearon la congelación (siguen abiertos)

- **D-076:** esquema definitivo de artículo/proveedor/mapping y migración 0008 (no autorizada).
- **D-077:** formato de la planilla Excel, columnas, unidades y mapeo; E8 congela solo el sobre.
- **C6:** mecanismo de obtención de precios públicos de cadenas.
- **H4:** aplicación de criterios, recomendación explicada, memoria comercial y aprendizaje desde evaluaciones.
- Conectores reales y acceso comercial de proveedores (HLD §25, V).
- Límites de tasa (429) y tiempos objetivo por fuente; umbrales de frescura por fuente (C2).
- Retención de `search_id`/`comparison_id` y formato exacto de `Idempotency-Key` más allá de 8–128.
