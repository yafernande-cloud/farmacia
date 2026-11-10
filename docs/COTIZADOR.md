# Cotizador: faltantes, búsquedas, carrito, cotizaciones, compras e IA

Actualizado: 2026-11-09. Este documento resume el estado **real** de los módulos del cotizador y
señala qué está implementado, qué se probó localmente, qué se verificó en producción y qué sigue
pendiente. Las reglas normativas completas están en
[`contracts/cotizador-v0.2.md`](contracts/cotizador-v0.2.md); ante una diferencia de redacción, el
contrato y las decisiones registradas mandan sobre este resumen.

## 1. Estado por módulo

| Módulo | Backend | Frontend Lovable | Producción |
|---|---|---|---|
| Faltantes, observaciones, cantidades y cancelación | Implementado | Grilla real contra la API | Dashboard desplegado; acceso del profesor verificado |
| Búsqueda de catálogo E1–E3 | Implementado | Buscador real | Desplegado |
| Búsqueda de cadenas E15 | Implementado | Tarjetas por cadena | Según configuración por cadena |
| Comparación E5 / criterios E9 | Implementado | No conectado | — |
| Cotizaciones y carrito E10 | Implementado | **Demostración local**; «Agregar a cotización» deshabilitado | — |
| Asignaciones a reportes | Implementado | No conectado | — |
| Compras registradas y cierre | **No implementado** | — | — |
| Historial con búsqueda | **No implementado** | Pantalla con datos de demostración | — |
| Observaciones comerciales e IA A6/O2 | Implementado | «Enseñar a la IA» deshabilitado | — |
| Proveedores e importación | Implementado | Pantalla real | Desplegado |
| Motor de proveedores: perfiles y búsqueda en vivo | Implementado en backend; apagado por defecto | No conectado | Migración `0027` no aplicada |

La suite backend cubre estos módulos con PostgreSQL real. La suite no llama a cadenas reales, a
OpenAI ni a Telegram: usa dobles y simuladores. Las excepciones documentadas son verificaciones
manuales: el límite de 50 caracteres de Cruz Verde contra el buscador público y el caso Predual
después del despliegue, con resultados de las tres cadenas (2026-11-09).

## 2. Faltantes y reportes

### Qué hace

- Grilla compartida por organización, con cursor y detalle.
- Un aviso repetido **no suma** cantidad. La cantidad desconocida es `NULL`, nunca 0 ni 1.
- Cantidades solo con comandos explícitos `SET`/`ADD`/`CLEAR`, `Idempotency-Key` e `If-Match`.
- Observaciones de faltante y su resolución por compras.
- Reportes independientes por autor: un aviso de otra persona no se adjunta ni se suma; el bot
  pregunta y, si se confirma, crea un reporte propio.
- Cancelación lógica con versión e idempotencia; no borra historial e informa las líneas afectadas.
- Asignaciones explícitas `quote_line_allocation`: una línea cubre uno o varios reportes, con suma ≤
  línea y sin reparto automático.
- Cada reporte nuevo guarda `author_name_snapshot`; cada faltante nuevo guarda `reported_by_name`.
  Los históricos no se reescriben: si el nombre cambió antes de `0026`, la vista usa el actual como
  respaldo.
- Memoria verificada de productos (alias) revisable y desactivable.

### Endpoints principales

| Método y ruta | Uso |
|---|---|
| `GET /api/v1/demand-items` · `GET /api/v1/demand-items/{id}` | Lista y detalle con observaciones e historial |
| `POST /api/v1/observations` | Aviso de faltante (`Idempotency-Key`) |
| `GET /api/v1/observations` · `POST /api/v1/observations/{id}/resolve` | Avisos dudosos y resolución |
| `POST /api/v1/demand-items/{id}/quantity-commands` | `SET`/`ADD`/`CLEAR` |
| `POST /api/v1/demand-items/{id}/cancel` | Cancelación lógica |
| `POST /api/v1/demand-items/{id}/resolve` | Confirmar unidad, forma, contenido, etc. |
| `GET /api/v1/product-aliases` · `POST /api/v1/product-aliases/{id}/deactivate` | Memoria verificada |

La cobertura de un faltante se deriva, no se almacena. Mientras no exista compra registrada,
`purchased` es 0 porque la tabla no existe, no porque se asuma que no se compró. Cotizar cubre de
forma **provisional** y no cierra.

## 3. Búsqueda de catálogo (E1–E3)

- Busca cualquier producto comercializado, no solo medicamentos.
- Devuelve `attributes` con procedencia `explicit | inferred | confirmed`, la medida escrita junto a
  su valor normalizado y `pending_validation`.
- E3 entrega el detalle del artículo y sus ofertas; las preferencias comerciales pueden cambiar el
  orden, pero todas las ofertas siguen visibles.
- Endpoints: `POST /api/v1/searches`, `GET /api/v1/searches/{id}`, `GET /api/v1/articles/{id}`.
- El frontend usa estos endpoints en la pantalla principal.

### Nombre comercial y caso Predual

El commit `d7b963a` corrigió la prioridad del nombre comercial:

- `product_name` se conserva **aparte** de la marca y del laboratorio.
- El nombre visible y la consulta a cadenas usan `product_name` cuando existe; si no, marca; si no,
  principio activo + dosis/contenido.
- La consulta (`search_query`) es corta: no concatena una descripción farmacológica larga cuando hay
  un nombre comercial. Eso era lo que hacía fallar la búsqueda de «Predual».
- Las indicaciones terapéuticas («analgésico femenino», «antigripal», etc.) se filtran como
  variantes comerciales; no contaminan el nombre visible ni la búsqueda.
- El cálculo se hace al leer los atributos guardados, **sin migración**: un faltante histórico que ya
  trae el nombre comercial se muestra corregido sin reescribir la base.
- La visión prioriza `product_name` sobre `brand` y conserva ambos; si no hay marca explícita, el
  nombre comercial también sirve de marca para la deduplicación existente.

Estado: implementado y probado localmente (dominio, visión y API). **Verificación manual informada
después del despliegue:** el faltante histórico muestra «Predual», el botón Buscar utiliza «Predual» y
aparecen resultados de Cruz Verde, Salcobrand y Ahumada. La fotografía asociada permaneció visible
después de un nuevo despliegue. Esta verificación es del caso Predual y **no** demuestra que el
reconocimiento general de fotografías con OpenAI interprete correctamente todos los productos.

## 4. Cadenas (E15)

### Diseño

- Cada cadena tiene su conector en `backend/app/services/chains/`: Ahumada, Salcobrand y Cruz Verde.
- `chain_search` las consulta en paralelo y publica un estado por cadena. Una cadena caída o que
  rechaza la consulta **no** se convierte en «sin resultados» ni afecta a las demás.
- Los resultados viven en memoria (TTL 10 min, 50 búsquedas vivas) y **no se persisten**. Reiniciar
  el backend pierde las búsquedas en vuelo; el sondeo devuelve `404 SEARCH_NOT_FOUND`.
- El frontend recupera ese caso: ante `SEARCH_NOT_FOUND`, reintenta el sondeo una vez y, si el
  identificador desapareció, reinicia **una sola vez** la búsqueda de esa cadena con un POST nuevo.
  No hay bucles. Esto evita mostrar «sin resultados» cuando en realidad el proceso perdió el estado
  (corrección `396061d` del frontend).
- Cada cadena se habilita con su interruptor y su configuración. Sin eso informa `not_configured` con
  un motivo real; jamás se inventan fichas ni precios.
- Una referencia de cadena **no** es una oferta comprable y nunca alimenta el carrito de proveedores.

### Cruz Verde: consultas largas y clasificación (D-149)

El buscador público de Cruz Verde rechaza con HTTP 400 una consulta `q` de más de **50 caracteres**.
La corrección (`582d7b5`) hace lo siguiente:

1. La consulta original **nunca** se reemplaza ni se recorta en E15; se conserva en
   `provenance.query_original`.
2. Si supera 50 caracteres, se hacen hasta dos consultas internas:
   - `fit_query`: corta en el último espacio anterior al carácter 50, sin partir palabras;
   - `essential_query`: principio activo + primera concentración, descartando la presentación pedida.
   Si no hay concentración, no se inventa una variante; si ambas coinciden, se hace una sola.
3. Un 400 se tipa como `CruzVerdeQueryRejected` y se traduce a `ChainUnavailable("query_rejected")`.
   `chain_search` publica `state="failed"`, `reason="query_rejected"` y resultados vacíos **solo para
   Cruz Verde**. Un 400 de la ficha de un producto sigue siendo un error de fuente.
4. Un tiempo de espera, bloqueo o error de red **no** amplía ni cambia la consulta; solo se reintenta
   más corto cuando el buscador rechazó la consulta.
5. Las fichas se clasifican por su **nombre publicado**, no por la consulta usada:
   `exact`, `unverified`, `partial` (otra presentación), `alternative` (otra concentración). Una
   concentración distinta tiene precedencia; un nombre que no declara el principio activo no se
   presenta como coincidencia.
6. El backend ordena `exact`, `unverified`, `partial`, `alternative` antes del límite; el frontend
   aplica el mismo orden y muestra «Otra presentación» y «Alternativa». El mensaje de rechazo es
   «Cruz Verde no aceptó esta búsqueda…», distinto de «no respondió».
7. Aplica **solo a Cruz Verde**: Ahumada y Salcobrand no cambian. Sin migración ni variables nuevas.

Estado: implementado; `test_cruzverde.py` con 68 pruebas aprobadas, suite frontend 238, `tsc` y `ruff`
limpios. La última suite backend informada quedó en 1044 aprobadas y 8 omitidas. El límite de
50 caracteres se verificó manualmente contra el buscador público el 2026-11-09; las pruebas
automatizadas usan dobles.

## 5. Comparación (E5) y criterios (E9)

- Elegibilidad por candidato antes de ordenar.
- Totales solo cuando están completos; si falta un componente, se informa una cota inferior, no un
  total inventado.
- Criterios comerciales que **anotan**, no cambian precios ni stock.
- Preferencias comerciales del producto específico pueden ordenar las ofertas; la elegibilidad manda
  primero y un estado no aplicado no retira preferencias.
- Endpoints: `POST /api/v1/comparisons`, `GET/POST/PATCH /api/v1/commercial-criteria`,
  `POST /api/v1/commercial-criteria/{id}/confirm|deactivate|reactivate`.
- El frontend de Lovable aún no consume la comparación E5.

## 6. Carrito y cotizaciones (E10)

### Backend implementado

- Cotizaciones con carritos por proveedor y líneas sin copia de precio.
- Operaciones: agregar/actualizar/retirar línea, `close-pending`, `reopen-pending`, mover entre
  proveedores, refrescar oferta y descartar/reincorporar carrito.
- Evaluación E5 por carrito; total general solo si todos los carritos están completos.
- Cobertura provisional por asignaciones; no cierra el faltante.
- Integridad: no se cierra una cotización con asignaciones inconsistentes
  (`409 QUOTE_HAS_INCONSISTENT_ALLOCATIONS`); la cantidad pedida no baja de lo asignado
  (`409 QUANTITY_BELOW_ALLOCATED`); no se borra un reporte con asignaciones vigentes
  (`409 REPORT_HAS_ALLOCATIONS`).
- Endpoints principales: `POST/GET /api/v1/quotes`, `GET /api/v1/quotes/{id}`,
  `GET /api/v1/quotes/{id}/summary`, `PATCH /api/v1/quotes/{id}`,
  `POST/PATCH/DELETE /api/v1/quotes/{id}/lines`, y las acciones por línea y carrito.

### Frontend

- La pantalla principal mantiene un carrito **local de demostración** (`carrito-demo`) y el botón
  «Agregar a cotización» está deshabilitado («Disponible próximamente»).
- «Mis cotizaciones» e «Historial» usan datos ficticios de demostración.
- **No hay conexión entre la UI y `/api/v1/quotes`.** El backend está listo y probado; la integración
  visual es trabajo pendiente.

### Cierre y compra

- Cerrar una cotización significa «preparada para comprar», no «comprada» (D-139, aprobada y **no
  implementada**). El código actual todavía cuenta solo `draft` y bloquea comprar con
  `409 QUOTE_CLOSED`; esa contradicción se corrige cuando se autorice C5-B.
- No hay compras parciales de la selección en la demo.

## 7. Compras registradas y cierre (no implementado)

- No existen tablas `purchase`, `purchase_line` ni `purchase_line_allocation`; la migración `0021`
  está reservada y no se ha creado.
- No hay endpoints de compra, historial de compras ni cierre de faltantes por compra.
- `purchase_execution` es `blocked` en todos los entornos y no depende de una variable.
- `fulfilled` no significa «recibido». La recepción física y las diferencias de entrega están fuera
  del MVP (D-126).
- D-139 (cotización cerrada comprable sin reabrir) y D-140 (compra adicional para inventario) están
  aprobadas y sin implementar.
- La migración `0022` (historial con `pg_trgm`/`unaccent`) y `0023` (archivo/purga) tampoco existen.

## 8. Observaciones comerciales e IA (A6/O2)

### Backend

- `POST /api/v1/commercial-observations` guarda el texto libre de una observación del artículo.
- `POST /api/v1/commercial-observations/{id}/interpretations` interpreta con OpenAI Responses API y
  salida estructurada estricta; devuelve el intento ya procesado si se repite (`replayed: true`).
- `GET /api/v1/commercial-observations/{id}` y `GET /api/v1/commercial-observations?trade_item_id=…`
  devuelven detalle e historial paginado.
- `GET /api/v1/articles/{id}/preferences` devuelve preferencias vigentes, retiros e historial.
- Estados: `sin_interpretacion`, `suggested`, `no_preference`, `needs_review`, `failed`, `rejected`,
  `confirmed`. Solo `failed` se puede reintentar.
- Un fallo del proveedor no lanza 5xx: persiste un intento `failed` con código cerrado (`timeout`,
  `ambiguous`, `provider_error`, `invalid_output`, …) y las preferencias anteriores siguen vigentes.
- `503 OBSERVATION_AI_DISABLED` si no hay proveedor configurado.
- Todas las rutas exigen `StrongActor` + `admin`; la organización sale del `Actor`; un artículo o
  observación de otra organización responde `404`.

### Reglas de negocio

- Las preferencias son **del producto específico**; no se propagan por laboratorio, marca o
  proveedor ni construyen perfiles globales.
- Ordenan las ofertas; todas siguen visibles.
- La IA no inventa precios, stock ni productos, no modifica cantidades y no decide dinero.
- No se muestran explicaciones, justificaciones ni puntuaciones. La traza (modelo, prompt, estado) es
  auditoría interna (D-141).
- El retiro explícito por clase (`retired_subject_kinds`) está implementado; una preferencia
  posterior válida puede volver a establecerla.

### Frontend

- El botón «Enseñar a la IA» está **deshabilitado** con «Disponible próximamente».
- Ninguna pantalla llama a `/commercial-observations` ni a `/articles/{id}/preferences`.
- La suite usa un doble del proveedor: **ninguna prueba llama a OpenAI ni consume cuota**.
- Falta conectar Lovable y hacer una prueba real con presupuesto aprobado.

## 9. Proveedores, fuentes e importación

- Proveedores con URL de portal, usuario y contraseña **cifrada en reposo** con Fernet
  (`SUPPLIER_CREDENTIALS_KEY`). La contraseña nunca se devuelve por la API ni se registra en logs.
- Guardar una credencial **no** conecta ni prueba el portal: no existe conector de portal. El estado
  público máximo es `credentials_saved`, nunca `connected`.
- Fuentes por organización, perfiles de importación y vista previa.
- Importación de planillas `.xlsx`, `.xls` (BIFF, `xlrd`) y `.csv`; estado de revisión de ofertas.
- Endpoints: `GET/POST/PATCH /api/v1/suppliers`, `GET/POST/PATCH /api/v1/sources`,
  `GET/PUT /api/v1/sources/{id}/import-profile`, `POST /api/v1/sources/{id}/import-preview`,
  `POST /api/v1/imports`, `GET /api/v1/suppliers/{id}/offers`.
- El frontend de proveedores consume la API real. El catálogo ficticio sigue siendo solo de simulación.

## 10. Fotografías

El detalle operativo está en [`H1B_TELEGRAM.md`](H1B_TELEGRAM.md) §5. Resumen:

- Binario en PostgreSQL (`temp_photos`, `BYTEA`, SHA-256), no en disco efímero.
- Miniatura autenticada: `GET /api/v1/attachments/{id}/content`; la API expone `photo_url`, nunca un
  `<img src>` público.
- La cantidad visible en la foto **no** es la cantidad solicitada.
- La lectura de imagen es `inferred` con `origin="image"`; si contradice el texto, va a revisión.
- Mantenimiento: `POST /api/v1/photos/maintenance/collect` y `/recover` (este último apagado por
  defecto).
- Persistencia comprobada en producción tras un redespliegue.

## 11. Pendientes ordenados

1. C5-B: compras registradas, cierre por compra, D-139 y D-140 (migración `0021`).
2. Conectar carrito/cotizaciones de Lovable a `/api/v1/quotes`.
3. Conectar «Enseñar a la IA» a `/commercial-observations` y hacer la prueba real con OpenAI.
4. Conectar la campanita de solicitudes web al backend (ver [`AUTH_SUPABASE.md`](AUTH_SUPABASE.md) §6).
5. C5-C: historial con `pg_trgm`/`unaccent` (`0022`).
6. C5-D: archivo/purga (`0023`, ejecución bloqueada).
7. C6a: habilitación autorizada de cadenas reales.
8. Validar de forma general el reconocimiento de fotografías con OpenAI; el caso Predual ya se
   verificó manualmente, pero no demuestra que todos los productos se interpreten correctamente.
