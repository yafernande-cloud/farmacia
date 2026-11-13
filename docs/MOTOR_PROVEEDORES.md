# Motor declarativo de proveedores

Actualizado: 2026-11-09. Este documento describe la **primera etapa** del motor de proveedores:
perfiles de conexión reutilizables, estrategias por mecanismo y búsqueda en vivo de Farmaloop mediante
la estrategia Algolia. No describe el descubrimiento automático con IA ni el carrito/cotizaciones:
esas piezas siguen pendientes.

## 1. Alcance

### Implementado en código

- Perfil de conexión versionado por `source` de proveedor (`supplier_connector_profile`).
- Validación estricta y declarativa del perfil; no ejecuta código ni acepta scripts.
- Registro de estrategias por mecanismo. Hoy solo está implementada `algolia`.
- Estrategia Algolia genérica: endpoint, índice, referencias de secretos y mapeo de campos.
- Preset `farmaloop` construido sobre hallazgos verificados; **no** es un conector exclusivo.
- Búsqueda en vivo sin persistir ofertas, con caché en memoria y límite de tasa.
- Estados derivados: `not_configured`, `connected`, `error`, `requires_intervention`.
- Clasificación textual de cada resultado (`direct`, `related`, `unverified`) sin eliminar ofertas.
- Condiciones comerciales mínimas por oferta (`cart_eligible`, `cart_blockers`), sin conectar el carrito.
- Detección de cambios de estructura por huella, sin borrar la configuración aprobada.
- Actualización del estado operativo del perfil (`last_success_at`, `last_error`, huella).
- Endpoints REST y pruebas con transporte HTTP simulado y PostgreSQL local.

### Deliberadamente fuera de esta etapa

- Descubrimiento automático de portales, formularios o estructuras con IA.
- Estrategias `rest_json`, `html_form`, `html_embedded_json`, `manual_capture` o navegador.
- Persistencia de ofertas, carga a `offer_snapshot`, carrito, cotizaciones o compra. La respuesta
  informa si una oferta podría incorporarse, pero **ningún endpoint agrega al carrito**.
- Filtrado o descarte de resultados por su nombre: los relacionados se muestran, marcados.
- Modificación de las búsquedas E1–E3, las importaciones o el frontend Lovable.
- Precios de cuenta, `unitCost`, stock con unidad no confirmada o URL de ficha inventada.
- Redis, colas, microservicios, workers o navegador headless.

La migración `0027` crea la tabla del perfil. **No se aplicó a `farmacia`, `farmacia_demo` ni a
producción**; solo se usó la base aislada de pruebas.

## 2. Arquitectura

```
POST /supplier-searches
        │
        ▼
services/connectors/service.py ── lee perfil ──► supplier_connector_profile
        │                                        (configuración versionada, sin secretos)
        ├── precondition? ──► not_configured (sin llamada externa)
        │
        ├── registry.get_strategy(mechanism)
        │         └── AlgoliaStrategy
        │                   ├── resuelve secretos desde variables de entorno
        │                   ├── POST al índice Algolia
        │                   ├── normaliza con field_map (rutas/constantes/plantillas)
        │                   └── devuelve ConnectorResult
        │
        ├── caché en memoria (TTL corto) y límite de tasa
        └── estado derivado + ofertas normalizadas (sin escribir ofertas)
```

El motor separa dos planos:

1. **Configuración** (`PUT /sources/{id}/connector-profile`): una persona autorizada guarda un perfil
   validado y versionado. La edición usa `If-Match`; un cambio de configuración reinicia el estado
   operativo para no presentar como probado algo que no lo fue.
2. **Ejecución** (`POST /supplier-searches`): se lee el perfil, se elige la estrategia y se consulta.
   La búsqueda solo actualiza metadatos operativos del perfil; **no crea `offer_snapshot`, ni
   `supplier_product_mapping`, ni ofertas de ningún tipo**.

### Estrategias y perfiles

Un mecanismo nuevo se agrega implementando una estrategia y registrándola en
`app/services/connectors/registry.py`. Ni el modelo, ni la API, ni los estados, ni la tabla cambian.
Farmaloop se incorpora como **perfil** con `mechanism = "algolia"`; no hay un módulo Farmaloop.

Un perfil está compuesto por:

- `mechanism`: mecanismo implementado (`algolia` en esta etapa);
- `config`: endpoint, índice, referencias de secretos, límites y opciones de normalización;
- `field_map`: cómo leer cada campo canónico del ítem;
- `enabled`: interruptor del perfil;
- `version`: control de edición optimista.

Campos canónicos del mapeo:

| Campo | Obligatorio | Uso |
|---|---|---|
| `external_id` | Sí | SKU del proveedor |
| `name` | Sí | Nombre publicado |
| `price` | Sí | Precio observado; debe ser decimal positivo |
| `currency` | Sí* | Moneda ISO de tres letras (*o `config.currency`) |
| `laboratory` | No | Laboratorio publicado |
| `presentation` | No | Presentación publicada |
| `stock` | No | Cantidad leída; solo se expone si hay unidad confirmada |
| `image_url` | No | Imagen http(s) válida |
| `product_url` | No | Ficha http(s) válida |
| `ean` | No | Código EAN publicado |

Cada campo declara **una sola fuente**: `path` (ruta JSON), `const` o `template`. Las rutas aceptan
objetos, índices y arrays (`batchs[].stock`). Las transformaciones permitidas son `first`, `sum` y
`join`. No se ejecuta código del perfil.

## 3. Estados

El estado **no se guarda**: se deriva de la configuración y del último resultado.

| Estado | Significado | Ejemplos |
|---|---|---|
| `not_configured` | Falta perfil, interruptor, secreto o la fuente no es consultable | `profile_missing`, `engine_disabled`, `source_disabled`, `source_mode_not_api`, `profile_disabled`, `secret_missing:FARMALOOP_ALGOLIA_API_KEY` |
| `connected` | La última consulta respondió y los campos obligatorios validaron | consulta con o sin resultados |
| `error` | Fallo transitorio; se puede reintentar | `timeout`, `network_error`, `rate_limited`, `provider_error`, `internal_error` |
| `requires_intervention` | Una persona debe corregir credenciales, configuración o mapeo | `credentials_rejected`, `field_mapping_failed`, `schema_changed` |

Reglas:

- Un error transitorio **no borra** `last_success_at` ni la configuración.
- Un secreto faltante es `not_configured`, no un error del proveedor.
- Una respuesta con ítems pero sin ofertas válidas es `requires_intervention`
  (`field_mapping_failed`), nunca «sin resultados».
- La huella describe la **estructura del sobre** de la respuesta (claves y tipos, con la lista de
  resultados vacía), no el contenido de cada producto. Campos opcionales ausentes, lotes vacíos o
  metadatos de coincidencia como `_highlightResult` cambian entre consultas sin que cambie el
  contrato del proveedor, y no deben marcar «requiere revisión». Un cambio real en los campos
  mapeados se detecta al normalizar (`field_mapping_failed`) y, si `item_path` deja de ser una
  lista, como `schema_changed`.
- Un cambio de huella del sobre devuelve las ofertas que sí validaron, pero deja el estado en
  `requires_intervention` (`schema_changed`) y registra la huella nueva para no alertar dos veces por
  el mismo cambio. Las huellas guardadas antes de esta corrección (`v2`) se reemplazan sin alertar.
- «Sin resultados» solo significa que la fuente respondió correctamente y no había coincidencias.

## 4. Coincidencia textual y condiciones comerciales

### Clasificación de coincidencias

Mostrar un resultado y afirmar que es el producto buscado son decisiones distintas. Cada oferta
devuelve `match_relation` y `match_reasons`; la clasificación es **textual** y responde solo si el
nombre publicado confirma la consulta:

| Relación | Significado | Ejemplo real (consulta «predual») |
|---|---|---|
| `direct` | El nombre contiene todos los términos significativos de la consulta, o la consulta completa como frase | «Predual 125 comprimidos», «Predual x 10 Comprimidos»; «Multiflora plus capsula 30» → «Multiflora Plus x 30 Capsulas» |
| `related` | El proveedor lo devolvió, pero el nombre solo confirma parte de la consulta o no la confirma | «Pregalex Pregabalina 150 mg 30 comprimidos», «Pregalin 75 mg x 30 Comprimidos» |
| `unverified` | Falta el nombre o la consulta para comparar | ítem sin `fullName` |

Reglas deliberadas:

- **No se elimina ni se filtra ninguna oferta.** Lo que el proveedor devolvió sigue visible; los
  relacionados se muestran en una sección aparte («Resultados relacionados») y nunca se presentan
  como la oferta buscada ni habilitan el alta. Descartar en silencio escondería resultados que una
  persona podría necesitar.
- **Canonicalización conservadora.** Antes de comparar se normalizan tildes, mayúsculas y puntuación
  con `normalize_name`; «x30» y «500mg» se separan en número y palabra; los términos se reducen a su
  forma canónica con los diccionarios existentes (`FORMS` para formas y abreviaturas como «caps»,
  `PACKAGING`, `UNITS`) y con una regla conservadora de singular/plural (`capsulas` → `capsula`).
  No hay similitud difusa, prefijos ni distancia de edición: una coincidencia parcial de texto no
  demuestra que sea el mismo producto.
- **La concentración, la forma y la identidad siguen decidiendo.** La consulta solo se confirma
  (`direct`) cuando todos sus términos aparecen en el nombre, o como frase completa. Una cifra
  distinta («30» frente a «60»), una forma distinta («comprimidos» frente a «capsulas», «jarabe»
  frente a cápsulas) o un envase distinto («frasco» frente a «caja», cuando el nombre declara uno)
  dejan el resultado en `related`. Términos de envase («caja») se toleran solo cuando el nombre no
  declara ninguno.
- Comparar palabras **no demuestra equivalencia farmacéutica**: no se comparan principios activos,
  dosis ni formas más allá del texto. La clasificación no reemplaza la revisión de una persona
  autorizada ni el clasificador de `app/domain/matching.py`.
- Las causas (`match_reasons`) explican el resultado en lugar de maquillarlo.

### Respaldo con consulta reducida

Cuando el proveedor devuelve ítems pero **ninguno** es una coincidencia directa, el backend hace
**un solo** reintento con una consulta reducida: se conservan solo los términos de identidad
(producto y marca) y se descartan conectores, formas, envases, unidades y cifras. Por ejemplo,
«Multiflora plus capsula 30 caja» se reintenta como «multiflora plus».

- El reintento es **best effort**: si falla, se conserva la respuesta original; si no mejora la
  cantidad de coincidencias directas, también. Nunca hay más de una llamada extra al proveedor.
- **La clasificación siempre se hace contra la consulta original.** El respaldo recupera
  candidatos, no convierte una oferta en coincidencia: «Multiflora 30» devuelto al buscar
  «Multiflora plus capsula 30» sigue siendo `related`.
- La lógica vive en el backend (`app/services/connectors/service.py`), de modo que vale para
  búsquedas manuales y para las que nacen de un faltante de Telegram; la pantalla ya no encadena
  reintentos propios sobre proveedores en vivo.
- La caché sigue indexada por la consulta original y la respuesta informa `relaxed_query` (la
  consulta reducida usada, o `null`) sin cambiar `query`.


### Unidad de venta y base del precio

`presentation` describe la **forma o el envase publicado** («Comprimido», «Caja 30 comprimidos»); no
es la unidad de venta ni la base del precio. El preset de Farmaloop no mapea `sale_unit` ni
`price_basis` porque el índice público no los declara de forma verificable. Por eso ambos quedan en
`null` aunque la presentación diga «Comprimido» o «x 10».

**No se infiere** que «Comprimido» signifique precio por comprimido, ni que «Caja 30» signifique que
el precio es por caja. Se completa cada campo solo cuando el proveedor lo declara de manera
verificable y el mapeo lo refleja. Un dato faltante nunca se reemplaza por 0, 1 ni un texto supuesto.

### Publicaciones sin precio comercial vigente

Un producto puede existir en el catálogo del proveedor sin precio de venta: es el caso de las
publicaciones con `bestPrice = 0` que el portal sigue mostrando. Se devuelven en una colección
**separada**, `published`, y no se convierten en ofertas:

- el ítem necesita identidad válida (`external_id` y `name`); si no la tiene, se descarta como antes;
- `price` no existe: **el precio de lista (`reference_price`, mapeado desde `basePrice`) se muestra
  solo como «Precio de lista referencial»** y nunca se copia a `price`, al carrito, a comparaciones
  ni a totales;
- `stock_status` sale de la señal explícita del proveedor `available_for_sale`
  (`availableForSale`): `0` es `out_of_stock` («Sin stock»), ausente es `unknown` («Stock no
  informado»); no se deduce de `basePrice` ni de los lotes;
- `cart_eligible` siempre es `false`; entre los motivos aparecen `no_current_price` y, cuando
  corresponde, `not_available_for_sale`;
- no se guardan en la base de datos ni generan `offer_snapshot`.

Los descartes ya no se publican como advertencia visible: el contador `discarded` sigue en la
respuesta para diagnóstico, pero un descarte técnico no se muestra en la pantalla de cotización.

### Habilitación para carrito

`cart_eligible` es `false` mientras exista algún bloqueo en `cart_blockers`, en este orden:

1. `match_not_direct` — la oferta no es una coincidencia directa.
2. `not_available_for_sale` — el proveedor informó que no está disponible para la venta
   (`stock_status = "out_of_stock"`).
3. `no_current_price` — solo en `published`: no hay precio comercial vigente.
4. `sale_unit_unconfirmed` — no hay unidad de venta confirmada.
5. `price_basis_unconfirmed` — no hay base del precio confirmada.

En esta etapa el carrito **no está conectado**: estos campos informan la condición comercial para la
integración futura y permiten verificar que ninguna oferta de Farmaloop quedaría habilitada hoy. Una
oferta solo sería elegible si fuera directa y declarara unidad de venta y base de precio.

## 5. Persistencia y migración

Tabla `supplier_connector_profile` (migración `0027`):

- una fila por `(organization_id, source_id)`;
- FKs compuestas que impiden mezclar organizaciones, proveedores o fuentes;
- `config` y `field_map` en JSONB;
- `last_success_at`, `last_checked_at`, `last_error`, `last_error_kind`,
  `schema_fingerprint` y `version`;
- `data_origin` con los valores ya usados por el catálogo;
- sin columna de estado ni de ofertas.

No hay backfill: ninguna fuente existente gana un perfil. Las instalaciones actuales siguen igual
hasta que una persona configure un conector.

## 6. API

Todas las rutas exigen `admin` + `StrongActor`. La organización sale del `Actor`, nunca del cuerpo.

### `GET /api/v1/sources/{source_id}/connector-profile`

Devuelve el perfil y su estado. Si no existe, responde `200` con `configured: false`,
`state: "not_configured"`, `reason: "profile_missing"` y `version: 0`. Así la pantalla distingue «sin
configurar» de un error de red.

### `PUT /api/v1/sources/{source_id}/connector-profile`

Crea o actualiza. En creación se puede omitir `If-Match` o enviar `"0"`. En actualización
`If-Match` es obligatorio (`428 VERSION_REQUIRED` si falta y `409 VERSION_CONFLICT` si no coincide).
La fuente debe ser `kind = "supplier"` y `mode = "api"`; el modo no se cambia desde este endpoint.

Preset:

```json
{ "preset": "farmaloop", "enabled": true }
```

Perfil explícito (resumen):

```json
{
  "mechanism": "algolia",
  "config": {
    "endpoint": "https://{app_id}-dsn.algolia.net/1/indexes/{index}/query",
    "app_id_secret_ref": "FARMALOOP_ALGOLIA_APP_ID",
    "api_key_secret_ref": "FARMALOOP_ALGOLIA_API_KEY",
    "index": "f_products_prod",
    "currency": "CLP",
    "item_path": "hits",
    "hits_per_page": 15
  },
  "field_map": {
    "external_id": { "path": "sku", "type": "string" },
    "name": { "path": "fullName", "type": "string" },
    "price": { "path": "bestPrice", "type": "decimal" },
    "currency": { "const": "CLP", "type": "string" }
  },
  "enabled": true
}
```

La respuesta nunca incluye el valor de una clave: solo el nombre de la variable de entorno.

### `POST /api/v1/supplier-searches`

```json
{ "source_id": "<uuid>", "query": "predual", "limit": 10 }
```

Devuelve `search_id`, estado, motivo, `query` (la búsqueda original), `relaxed_query` (la consulta
reducida con la que se recuperaron candidatos, o `null`), ofertas normalizadas, `published`,
`discarded`, `warnings` y `cached`. Cada oferta incluye `external_id`, `name`, `laboratory`,
`presentation`,
`price.amount`/`price.currency`, `price_basis`, `sale_unit`, `stock_status`, `stock_quantity`,
`stock_unit`, `image_url`, `product_url`, `ean`, `match_relation`, `match_reasons`, `cart_eligible`
y `cart_blockers`.

`published` es una colección aparte, con la misma identidad y clasificación textual pero **sin
`price`**: incluye `reference_price` (`null` si el proveedor no informó precio de lista),
`stock_status` derivado de `available_for_sale`, `cart_eligible = false` y sus `cart_blockers`. Un
publicado nunca se mezcla con `offers` ni se puede cotizar.

Los campos no informados quedan en `null`:
`price_basis`, `sale_unit`, `stock_quantity`, `stock_unit`, `product_url`, etc. **No se completa un
dato faltante con 0, 1 ni un texto inventado.**

### `GET /api/v1/supplier-searches/{search_id}`

Recupera el resultado mientras siga en la caché en memoria (TTL de `SUPPLIER_CONNECTOR_CACHE_SECONDS`,
60 s por defecto). Reiniciar el backend pierde las búsquedas. Un `search_id` de otra organización
responde `404`, igual que uno inexistente.

## 7. Configuración por entorno

| Variable | Efecto | Valor por defecto |
|---|---|---|
| `SUPPLIER_CONNECTOR_ENABLED` | Habilita las consultas externas | `false` |
| `SUPPLIER_CONNECTOR_CACHE_SECONDS` | TTL de la caché en memoria | `60` |
| `SUPPLIER_CONNECTOR_MAX_LIVE` | Máximo de búsquedas vivas | `50` |
| `SUPPLIER_CONNECTOR_MAX_RESULTS` | Tope de ofertas por consulta | `20` |
| `SUPPLIER_CONNECTOR_RATE_LIMIT_PER_MINUTE` | Límite por actor | `20` |
| `FARMALOOP_ALGOLIA_APP_ID` | App ID de Algolia para el preset | sin valor |
| `FARMALOOP_ALGOLIA_API_KEY` | Clave de búsqueda pública del sitio | sin valor |

La clave se lee del entorno en cada consulta y **no se guarda en la base, ni en el repositorio, ni en
la respuesta HTTP**. Un perfil también puede declarar sus propias referencias (`*_secret_ref`); el
nombre debe ser un identificador de variable de entorno.

Para la validación local, esas dos variables deben estar **exportadas en el proceso** que ejecuta el
backend (por ejemplo, `export FARMALOOP_ALGOLIA_APP_ID=…` antes de `scripts/dev.sh`) o configuradas
como *config vars* en Heroku. Escribirlas solo en `backend/.env` no basta: el motor resuelve la
referencia directamente del entorno del proceso, no del objeto de configuración de Pydantic. Esto es
deliberado: una referencia define **qué** variable leer, sin abrir la puerta a leer cualquier campo de
configuración como secreto.

## 8. Farmaloop: qué se verificó y qué no

Verificado manualmente el 2026-11-09 contra el índice público:

- índice `f_products_prod` y búsqueda pública sin autenticación de cuenta;
- campos `sku`, `fullName`, `laboratoryName`, `presentation`, `bestPrice`, `basePrice`,
  `photoURL`, `ean`, `slug`, `batchs[].stock`, `availableForSale`;
- consultas de «Predual» devolvieron, entre otras, ofertas de $12.990 y $1.990. Es una observación
  puntual, no una promesa de precio ni una generalización a otros productos.

El preset **no** declara:

- `sale_unit` ni `price_basis`, porque no están confirmados;
- `stock_unit`, porque la unidad de `batchs[].stock` no está confirmada; la cantidad no se expone
  aunque la ruta esté mapeada, y el estado sale de `available_for_sale`;
- `product_url`, porque no se verificó una plantilla de ficha estable;
- `unitCost`, que es un dato de cuenta y no se mapea ni se registra.

Riesgos conocidos: la clave pública del sitio puede rotar; Algolia puede cambiar el esquema; el uso
de la fuente debe contar con la autorización que corresponda. Un cambio de huella o un
`field_mapping_failed` obliga a revisar el perfil antes de confiar en los datos.

### Validación real del 10 de noviembre de 2026

Se ejecutó **una** búsqueda real contra el índice público, en la base desechable
`farmacia_0026_test` (migración `0027` aplicada solo allí; `farmacia`, `farmacia_demo` y producción
no se tocaron). Las credenciales de búsqueda se cargaron únicamente como variables del proceso local,
sin imprimirlas, registrarlas ni guardarlas en archivos. No se modifica ni se publica nada por esta
validación.

Resultado observado (registro `2026-11-10T01:20:56Z`, consulta «predual»):

- respuesta HTTP correcta, estado `connected`, sin error de estructura;
- **5 ofertas válidas y 5 publicaciones sin precio de venta** (`bestPrice = 0`,
  `availableForSale = 0`, sin lotes ni listas de precio); con el criterio vigente ya no se cuentan
  como descartes;
- dos SKU que sí corresponden a Predual: `140429` («Predual 125 comprimidos», $12.990) y `105469`
  («Predual x 10 Comprimidos», $1.990), ambos con moneda CLP, presentación, imagen y laboratorio;
- **`140640` («Predual Pamabron 25 mg 12 Comprimidos», Maver)**: coincidencia directa sin precio
  vigente, con precio de lista referencial $2.290 y «Sin stock» informado por el proveedor;
- las otras cuatro publicaciones son `related` (Pregalin 150, Pedialyte 60 y dos Reduclim);
- tres productos de **pregabalina** (Pregalex y Pregalin) devueltos por el buscador del proveedor:
  quedaron como `related`, no como coincidencias;
- `sale_unit` y `price_basis` en `null` en todas las ofertas; `cart_eligible = false` en ofertas y
  publicaciones;
- **`offer_snapshot` permaneció vacío** (0 filas): la búsqueda no persiste ofertas.

Limitaciones de esa evidencia:

- es **una sola consulta**, en un momento dado; no generaliza precios, stock ni catálogo;
- el índice público no es una cotización ni una condición de compra acordada;
- no se validó ficha por ficha contra el portal, ni la cuenta, ni precios de cuenta (`unitCost`);
- no se confirmó unidad de venta, base de precio ni unidad de stock;
- la clasificación es textual: `related` no afirma que Pregalex/Pregalin sean equivalentes a
  Predual, solo que su nombre no confirma la consulta;
- no se probó desde Lovable, ni con el carrito, ni con cotizaciones.

## 9. Pruebas

La suite no hace llamadas externas:

- `tests/test_supplier_matching.py` prueba la clasificación textual (directo, relacionado, sin
  nombre, normalización de tildes/mayúsculas, consulta parcial, singular/plural, abreviaturas y
  cantidades como «x30», concentraciones, formas y envases distintos, y la consulta reducida del
  respaldo) y el bloqueo comercial del carrito;
- `tests/test_supplier_connectors.py` prueba la estrategia Algolia con `httpx.MockTransport`
  (normalización, descarte de ítems inválidos, 401/403/429/4xx/5xx, redirecciones, secreto faltante,
  tamaño máximo, stock sin unidad, plantillas de URL, huella de estructura y tipos canónicos);
- `tests/test_supplier_connectors_api.py` usa PostgreSQL real y una estrategia doble para probar
  configuración, `If-Match`, estados, caché, límite de tasa, aislamiento por organización, la
  clasificación de Predual frente a Pregalex/Pregalin, el respaldo con consulta reducida (una sola
  llamada extra, solo sin coincidencias directas, sin convertir un relacionado en directo y
  conservando la respuesta original si falla) y la ausencia de `offer_snapshot`;
- `tests/test_db_schema.py` verifica que el modelo y la migración `0027` estén sincronizados.

## 10. Cómo validar la búsqueda de Predual antes de conectar Lovable

1. Aplicar la migración `0027` **solo** en una base de pruebas o demo, con autorización y respaldo.
   No aplicar a `farmacia` ni a producción en esta etapa.
2. Configurar en el entorno del backend `SUPPLIER_CONNECTOR_ENABLED=true`,
   `FARMALOOP_ALGOLIA_APP_ID` y `FARMALOOP_ALGOLIA_API_KEY`. Verificar que las variables existen sin
   imprimir sus valores.
3. Con una cuenta `admin` y `StrongActor`, crear o reutilizar un proveedor y una fuente
   `kind="supplier"`, `mode="api"`.
4. `PUT /api/v1/sources/{source_id}/connector-profile` con `{"preset":"farmaloop"}` e `If-Match: "0"`.
   Confirmar `state="not_configured"` y `reason="not_tested"`.
5. `POST /api/v1/supplier-searches` con `{"source_id":"…","query":"predual","limit":10}`.
   Confirmar `state="connected"` y revisar cada oferta: nombre, laboratorio, presentación, precio,
   moneda e imagen; la URL de ficha solo aparece si el perfil declara una plantilla verificada (el
   preset de Farmaloop no la declara). Confirmar que `stock_status` refleja `availableForSale`
   (`in_stock`/`out_of_stock`, `unknown` si no viene), que `sale_unit=null`, `price_basis=null` y
   `cart_eligible=false` mientras no estén confirmados, y que Predual aparezca como `direct`
   mientras Pregalex/Pregalin quedan como `related` **sin desaparecer** de la lista. Revisar
   `published`: `140640` debe aparecer como directa con `out_of_stock` y `reference_price` $2.290,
   sin `price` y sin entrar al carrito.
6. Contrastar manualmente los precios y presentaciones con la ficha pública. Registrar la fecha, la
   consulta y el `search_id`; no convertir esa observación en una oferta persistida.
7. Si aparece `requires_intervention`, corregir el mapeo o las credenciales y repetir. Si aparece
   `error`, distinguir el fallo transitorio y reintentar; no cambiar la configuración a ciegas.
8. Solo después de esa validación conectar Lovable. El frontend ya muestra las publicaciones sin
   precio vigente en una tarjeta aparte, con el precio de lista como «referencial» y el carrito
   deshabilitado; el endpoint sigue sin alimentar el carrito ni las cotizaciones.

### Perfiles ya guardados

El perfil guardado en `source_connector_profile` conserva su propia copia de `config` y `field_map`;
por eso un despliegue del backend **no** activa por sí solo los mapeos nuevos
(`available_for_sale`, `reference_price`). Sin ellos la consulta sigue funcionando y las publicaciones
directas **sí aparecen** en `published` —`published` solo necesita identidad, nombre y moneda—, pero
sin los datos que añaden esos dos mapeos: `stock_status` queda `unknown` y `reference_price` queda
`null`. Reaplicar el preset es una mejora (disponibilidad y precio de lista), no un requisito para
que los productos publicados se muestren. Para activar los mapeos nuevos hay que reaplicar el preset:

```
PUT /api/v1/sources/{source_id}/connector-profile  {"preset":"farmaloop"}
```

Eso sube `version` y borra `last_success_at`: el estado vuelve a «not_configured / not_tested» y el
frontend muestra «Sin conexión» hasta la primera consulta correcta. Es un cambio de configuración de
producción y requiere autorización expresa.

## 11. Siguiente etapa

El diseño aprobado contempla, sin implementarlo todavía:

- banco de descubrimiento con capturas y propuestas de mapeo;
- interpretación de estructuras desconocidas por IA **fuera de línea**, con aprobación humana;
- perfiles versionados con origen del dato (`captured`, `ai_derived`, etc.);
- estrategias para REST/JSON, HTML estático, JSON embebido y captura manual;
- validación de unidad de venta, base de precio y stock antes de materializar ofertas;
- revisión de coincidencias con identidades farmacéuticas verificadas, además de la clasificación
  textual actual, antes de comparar o comprar;
- integración con E10 mediante materialización explícita y revisión, nunca automática;
- conexión de Lovable a la búsqueda en vivo, una vez acordado el contrato de estados y bloqueos.

Nada de esto se declara implementado en esta etapa.
