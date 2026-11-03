# Propuesta de revisión 10 — Contrato del cotizador v0.2

**Estado: propuesta, no aprobada y no aplicada.** El contrato vigente sigue siendo la **revisión 9**
(`docs/contracts/cotizador-v0.2.md`). Este documento describe, de forma identificable, qué cambiaría en la revisión 10
para alinear §4.2, §5.1, §6, §8.3–§8.7 y §12.3 con **D-136, D-138, D-139, D-140 y D-141**. **No se editó el contrato,
no se implementó O2–O4 ni C5-B, no se modificó el HLD y no se aplicó ninguna migración.**

**Fecha:** 2026-11-02. **Base:** contrato v0.2 revisión 9. **Decisiones involucradas:** D-116, D-132, D-133, D-136,
D-137, D-138 (C7), D-139 (C9), D-140 (C11) y D-141 (AC31). **Aprobación requerida:** tuya, expresa, antes de aplicar la
revisión 10.

---

## 1. Resumen de cambios propuestos

| # | Sección | Cambio | Motivo |
|---|---|---|---|
| 1 | §8.4 | O3 y O4 se redefinen: recuperación de observaciones **del producto exacto** y **orden interno** de sus ofertas; desaparece el `advice` visible | D-136 y D-138 |
| 2 | §8.4 | Se elimina el objeto `advice` con `stance`, `text`, `based_on` y `supersedes` como salida visible | D-136: sin explicaciones, justificaciones ni puntuaciones |
| 3 | §8.4 | Se fija la precedencia **solo por producto**: la más reciente para la misma clase gana; **ninguna preferencia se propaga** a otro producto del mismo laboratorio, marca o proveedor | D-138 (C7) |
| 4 | §5.1 y §8.3 | Las dos apariciones de `GET /api/v1/quotes/{quote_id}` dejan de anunciar `advice`; las ofertas llegan ordenadas, sin campo de explicación | D-136 y D-138 |
| 5 | §4.2 | La fila «Consejo de una línea → `advice`» se sustituye por «Orden de ofertas → observaciones del producto + orden determinista de E5» | D-136 y D-138 |
| 6 | §8.6 | Se permite **ordenar** las ofertas del producto por sus preferencias; se mantienen las prohibiciones de inventar, ocultar, quitar o alterar números | D-136 y D-138 |
| 7 | §8.7 | Criterios de aceptación de la demo se reescriben para ordenar sin explicar y para probar que no hay propagación | D-136 y D-138 |
| 8 | §8.4 / §16 | O2–O4 se asignan a la migración **0019**, no a la `0016` (que pertenece a D-135) | Corrección documental (C8) |
| 9 | §8.4 | Se explicita que las preferencias aplican a **cualquier producto**, no solo medicamentos | A1/A5/A6 |
| 10 | §12.3 | La cobertura **cotizada** cuenta asignaciones de cotizaciones **`draft` y `closed`**, sin doble conteo con compras | D-139 (C9) |
| 11 | §6 | Se permite registrar la compra desde una cotización **`closed` sin reabrirla**; cerrar no compra y no cierra reportes | D-139 (C9) |
| 12 | §6.1 / §12.3 | La compra puede incluir cantidades **sin asignación**; quedan como **compra adicional para inventario** y no cierran reportes | D-140 (C11) |
| 13 | §8.4 | AC31 se satisface con **traza interna** de observación, interpretación, modelo, versión y criterios; no con explicación visible | D-141 (AC31) |

**Sin cambios de esquema por esta revisión documental.** O2–O4 siguen reservadas a la migración `0019`. Las compras y
`purchase_line_allocation` pertenecen a C5-B (`0018`), que tampoco se implementa aquí. Esta propuesta no crea tablas,
columnas ni campos de respuesta.

---

## 2. Texto vigente y texto propuesto

### 2.1 §8.4 — tabla O1–O4 (reemplazo completo)

**Vigente (revisión 9):**

| Etapa | Qué hace | Dónde |
|---|---|---|
| **O1 — Persistencia** | Guardar la observación con autor, fecha, texto original y contexto; mostrarla siempre | C5-A, migración 0015 (`commercial_observation`) |
| **O2 — Interpretación** | Enviar el texto a un modelo y guardar una interpretación estructurada como sugerencia | O2–O4, migración 0016 (`observation_interpretation`) |
| **O3 — Recuperación y precedencia** | Para un artículo, recuperar **solo** sus observaciones (nunca por nombre, marca o laboratorio parecido); la más reciente para el mismo sujeto y clase de consejo gana; las anteriores se muestran como superadas | En vivo, sin tabla nueva |
| **O4 — Consejo explicado** | Devolver un objeto `advice` que cita las observaciones en que se basa y las que supera | En vivo, dentro de la cotización |

**Propuesto (revisión 10):**

| Etapa | Qué hace | Dónde |
|---|---|---|
| **O1 — Persistencia** | Guardar la observación con autor, fecha, texto original y contexto **del producto específico**; mostrarla siempre. **Ya implementado** | C5-A, migración 0015 (`commercial_observation`) |
| **O2 — Interpretación** | Enviar el texto a un modelo y guardar una interpretación estructurada **en el contexto de ese producto**: laboratorio, marca, precio, disponibilidad u otro criterio comercial expresado por el administrador. Es una sugerencia interna ligada al producto, no un consejo visible ni una preferencia global | O2–O4, migración **0019** (`observation_interpretation`) |
| **O3 — Recuperación y precedencia** | Recuperar **solo** las interpretaciones del `trade_item_id` exacto consultado; nunca por nombre, marca o laboratorio parecido. La más reciente para el mismo producto y clase de preferencia gana; las anteriores no se borran y quedan solo en la traza de auditoría | En vivo, sin tabla nueva |
| **O4 — Orden interno** | Aplicar las preferencias **de ese producto** para **ordenar** sus ofertas recuperadas: primero las que mejor cumplen. **Todas las ofertas siguen visibles.** No se devuelve explicación, justificación ni puntuación | En vivo, dentro de la búsqueda y la cotización |

### 2.2 §8.4 — precedencia (reemplazo)

**Vigente (revisión 9):**

> 1. Solo observaciones con `trade_item_id` **exacto** al artículo consultado.
> 2. Para el mismo `subject_kind` y la misma clase de consejo, **gana la más reciente** (`created_at`, desempate por
>    `id`).
> 3. Las anteriores **no se borran**: se devuelven en `supersedes[]` y se muestran como «superada por la observación
>    del …».
> 4. Una observación de marca o proveedor **no se aplica** a otro artículo de la misma marca.
> 5. Si no hay observaciones, `advice` es `null`; no se rellena con texto genérico.

**Propuesto (revisión 10):**

> 1. **Alcance por producto.** Cada observación y su interpretación se recuperan **solo** para el `trade_item_id`
>    exacto al que se refieren. Una observación de laboratorio, marca o proveedor **no se aplica** a otro producto de
>    ese mismo laboratorio, marca o proveedor.
> 2. **Sin perfiles globales.** No se construyen preferencias de organización, índices de reputación ni puntuaciones
>    acumuladas por laboratorio, marca o proveedor. Una preferencia negativa sobre el Losartán de un laboratorio no
>    perjudica a ese laboratorio en otro medicamento, y una positiva no lo favorece en otro producto.
> 3. **Precedencia.** Para el mismo producto y la misma clase de preferencia, **gana la más reciente** (`created_at`,
>    desempate por `id`). Las anteriores no se borran y quedan en la traza de auditoría.
> 4. **Sin explicación visible.** No hay `advice`, `stance`, `text`, `based_on` ni `supersedes` en la respuesta. La
>    traza existe solo para auditoría interna (AC31/D-141).
> 5. **Sin preferencias aplicables**, el orden es el determinista de E5; no se rellena con texto genérico ni con una
>    prioridad inventada.
> 6. Una preferencia de marca o laboratorio **no** afirma equivalencia farmacológica, dosis equivalente ni sustitución.

**Nota:** este texto **elimina** la ambigüedad de la redacción anterior de A5/A6, que hablaba de «preferencias
reutilizables de la organización». La decisión C7/D-138 es que **no existen** esas preferencias globales: todo
ordenamiento es por producto específico.

### 2.3 §8.4 — objeto `advice` (se elimina)

**Vigente (revisión 9):** se documenta un objeto JSON `advice` con `stance` (`favor`/`caution`/`neutral`), `text`,
`based_on[]`, `supersedes[]` e `interpretation_status`, y se declara «información adicional, visible y explicable».

**Propuesto (revisión 10):** **se elimina del contrato de respuesta.** En su lugar:

> El efecto visible de O2–O4 es **solo el orden de las ofertas del producto**. La interpretación, la preferencia
> aplicada, la observación de origen, el modelo y la versión del prompt se guardan en `observation_interpretation` para
> **auditoría interna (AC31/D-141)**. Ningún endpoint devuelve una explicación, justificación, recomendación textual o
> puntuación de la IA. La revisión humana de interpretaciones sigue siendo de `admin` (P-7, D-130) y no se muestra al
> auxiliar ni al comprador.

### 2.3 bis §8.5 — fallo del proveedor

**Vigente (revisión 9), regla 5:**

> **Fallo del proveedor**: la observación se guarda igual; la respuesta incluye `advice_status: "sin_interpretacion"`
> y un motivo visible; nunca se sustituye por una regla fingida.

**Propuesto (revisión 10):**

> **Fallo del proveedor**: la observación se guarda igual y **el orden queda en el determinista de E5**. No se devuelve
> `advice_status` ni un motivo visible al usuario; el fallo se registra en auditoría interna. Nunca se sustituye por una
> regla fingida ni se inventa una preferencia.

### 2.4 §5.1 y §8.3 — `GET /api/v1/quotes/{quote_id}`

**Vigente (revisión 9), en §5.1:**

> Detalle completo: carritos, líneas, evaluación E5 por carrito, total estimado, cobertura de faltantes y `advice`.

**Vigente (revisión 9), en §8.3:**

> `GET /api/v1/quotes/{quote_id}` | Incluye `advice` por línea (§8.4)

**Propuesto (revisión 10):**

> Detalle completo: carritos, líneas, evaluación E5 por carrito, total estimado y cobertura de faltantes. Las ofertas
> llegan **ordenadas** por las preferencias del producto; **no** incluye `advice` ni ningún campo que explique el orden.

### 2.4 bis §4.2 — tabla de derivados

**Vigente (revisión 9):**

| Objeto | Derivado de | Valores |
|---|---|---|
| Consejo de una línea | observaciones del artículo + interpretaciones revisadas | `advice` (§8.4) |

**Propuesto (revisión 10):**

| Objeto | Derivado de | Valores |
|---|---|---|
| Orden de ofertas | observaciones e interpretaciones **del producto exacto** + orden determinista de E5 | sin campo visible; el orden no se explica ni se puntúa |

**Sin cambios de cálculo de dinero.** Ordenar no altera precios, totales, elegibilidad ni cantidades (D-133).

### 2.5 §8.6 — Prohibiciones de la IA

**Vigente (revisión 9):**

> La interpretación **no puede**:
> - modificar precio, stock, condiciones, cantidad, moneda, totales ni elegibilidad;
> - crear, quitar, ocultar, reordenar o destacar ofertas;
> - otorgar o quitar permisos, roles o accesos;
> - afirmar equivalencia farmacológica, dosis equivalente o sustitución;
> - aplicar una observación a otro artículo por parecido de nombre, marca o laboratorio;
> - borrar, editar o «resumir definitivamente» una observación.

**Propuesto (revisión 10):**

> La interpretación **no puede**:
> - modificar precio, stock, condiciones, cantidad, moneda, totales ni elegibilidad;
> - **crear, quitar u ocultar** ofertas;
> - **alterar el conjunto** de ofertas recuperadas: el orden por preferencias **no** descarta ninguna;
> - **aplicar una preferencia a otro producto** por compartir laboratorio, marca o proveedor, ni por parecido de nombre;
> - **construir perfiles globales de reputación** de laboratorios, marcas o proveedores;
> - otorgar o quitar permisos, roles o accesos;
> - afirmar equivalencia farmacológica, dosis equivalente o sustitución;
> - inventar ofertas, precios, stock o condiciones comerciales;
> - mostrarse como chatbot, explicación, justificación, recomendación textual o puntuación al usuario;
> - borrar, editar o «resumir definitivamente» una observación.
>
> **Sí puede**, y es su función aprobada: **ordenar** las ofertas recuperadas **de un producto** según las preferencias
> comerciales aprendidas **de ese producto**, manteniendo visibles todas las demás.

### 2.6 §8.7 — Aceptación de la demostración

**Vigente (revisión 9):**

> 1. Dos observaciones contradictorias sobre el mismo artículo, en fechas distintas.
> 2. La cotización posterior muestra un consejo que cita **la más reciente** y marca la anterior como **superada**, sin
>    borrarla.
> 3. El consejo explica el motivo con el **texto original visible**.
> 4. La cotización devuelve **exactamente las mismas ofertas, precios, stock y totales** que sin observaciones.
> 5. Una observación sobre un artículo **no** se aplica a otro artículo de la misma marca.
> 6. Si la IA está deshabilitada o falla, la observación sigue guardada y el consejo dice «sin interpretación».

**Propuesto (revisión 10):**

> 1. Dos observaciones contradictorias sobre el mismo producto, en fechas distintas.
> 2. La lista posterior aparece **ordenada** por la preferencia más reciente de ese producto; la anterior no se borra y
>    queda en la traza de auditoría.
> 3. **No** se muestra explicación, justificación, recomendación textual ni puntuación de la IA en ninguna pantalla ni
>    respuesta.
> 4. La lista devuelve **exactamente las mismas ofertas, precios, stock y totales** que sin preferencias; solo cambia el
>    orden.
> 5. Una observación sobre el Losartán de un laboratorio **no** cambia el orden de las ofertas de otro producto de ese
>    mismo laboratorio, marca o proveedor. La demo debe mostrar explícitamente este contraejemplo.
> 6. Si la IA está deshabilitada o falla, la observación sigue guardada y el orden es el determinista de E5; no se
>    simula una preferencia ni se inventa una prioridad.
> 7. La demostración debe incluir al menos un producto **no farmacéutico** (por ejemplo, un cepillo dental), sin exigir
>    atributos como ingrediente, potencia o forma.

### 2.7 Corrección de la migración de O2–O4

**Vigente (revisión 9):** O2–O4 → migración **0016** (`observation_interpretation`).

**Propuesto (revisión 10):** O2–O4 → migración **0019**. La `0016` es de D-135 (reportes independientes, cancelación y
confirmación) y la `0017` es de asignaciones; la numeración vigente está ratificada en el roadmap y en las decisiones.
No se renumera nada en silencio: esta propuesta deja constancia del cambio.

### 2.8 §12.3 — cobertura cotizada con cotizaciones cerradas (C9/D-139)

**Vigente (revisión 9), en lo pertinente:**

> La cobertura cotizada se calcula con asignaciones de líneas activas de carritos activos de cotizaciones `draft`.
> Una cotización cerrada deja de aportar cobertura provisional. La compra solo se registra sobre una cotización
> reabierta.

**Propuesto (revisión 10):**

> **Cobertura provisional.** La cobertura cotizada suma las asignaciones explícitas de líneas activas de carritos
> activos de cotizaciones **`draft` y `closed`**. Una cotización cerrada **conserva** su cobertura provisional porque
> sigue siendo una preparación válida para comprar. Una cotización anulada o descartada **no** aporta cobertura.
>
> **Sin doble conteo.** Cuando existe una compra registrada para el mismo reporte, la cantidad comprada **sustituye** a
> la cotizada; nunca se suman ambas. La cobertura comprada proviene **solo** de compras registradas.
>
> **Cierre.** Solo la compra registrada que cubre toda la cantidad pedida conocida cierra el reporte. Cotizar —en
> `draft` o en `closed`— **nunca** cierra ni pasa a `fulfilled`.

### 2.9 §6 — compra desde una cotización cerrada, sin reabrir (C9/D-139)

**Vigente (revisión 9), en lo pertinente:**

> Cerrar una cotización impide editarla y registrar compras sobre ella; para comprar hay que reabrirla.

**Propuesto (revisión 10):**

> **`draft`** = cotización en preparación. **`closed`** = cotización preparada y disponible para convertirse en compra.
> Cerrar **no** registra una compra, no descarta la cotización y no cierra reportes.
>
> La compra se registra **desde una cotización `closed`, sin reabrirla**. Una cotización cerrada conserva
> líneas, cantidades, proveedores y asignaciones. Editar la preparación de una cotización `closed` sigue exigiendo
> reabrirla; **comprar no**.
>
> El frontend distingue **«Cotización preparada»** (`closed` sin compra) de **«Compra registrada»** (existe una compra).
> Una cotización cerrada sin compra permanece en el historial como preparación, no como compra ni como descarte.

### 2.10 §6.1 y §12.3 — compra adicional para inventario (C11/D-140)

**Vigente (revisión 9):** una línea de compra se asigna a reportes; no se define qué ocurre con la cantidad no asignada.

**Propuesto (revisión 10):**

> La compra registra la cantidad efectivamente comprada y puede **superar** lo requerido por los reportes.
>
> - Las asignaciones a reportes son **explícitas** y su suma **no supera** la cantidad comprada.
> - **No es obligatorio** asignar toda la línea. Las unidades sin asignación quedan como **compra adicional para
>   inventario**.
> - La compra adicional **no se asigna automáticamente**, **no crea reportes ficticios** y **no cierra ningún reporte**.
> - **No se modifica automáticamente** la cantidad de ningún reporte.
> - Se conserva la **trazabilidad** de la compra, la línea, las asignaciones y el excedente de inventario.
>
> Ejemplo normativo: Juanito reporta 10 y María 6; se compran 20; se asignan 10 y 6; las 4 restantes son compra
> adicional para inventario. Solo 16 cubren reportes.
>
> La compra sigue siendo **todo o nada respecto de la selección**: se registra completa —incluidas las unidades de
> inventario— o no se registra. No hay compras parciales de la selección.

### 2.11 §8.4 — AC31 como auditoría interna (D-141)

**Texto propuesto (nuevo párrafo):**

> **Trazabilidad interna (AC31).** Para cada ordenamiento se conserva internamente la observación de origen, su
> interpretación, el modelo y la versión del prompt, los criterios aplicados y las ofertas consideradas. Esta traza es
> accesible para revisión administrativa y auditoría, y **no** se expone como explicación, justificación, recomendación
> textual ni puntuación en la interfaz ni en la API pública. AC31 se satisface con esta traza interna.

---

## 3. Trazabilidad de decisiones

| Decisión / criterio | Qué se conserva | Qué cambia | Cómo queda en la revisión 10 |
|---|---|---|---|
| **D-116** | Observación inmutable, acumulativa y separada; `observation_interpretation` con modelo, prompt, tokens, costo y latencia; revisión humana solo `admin`; si falla la IA, la observación se guarda | El **`advice` visible** con `based_on`/`supersedes` deja de ser salida; la IA no decide dinero, cantidades ni visibilidad | Se mantiene todo salvo la visibilidad del consejo. D-116 no se borra: se anota que el `advice` visible queda sustituido por D-136/D-138 y que la traza pasa a AC31/D-141 |
| **D-132** | Cualquier `admin` registra la observación con texto libre, sin categorías ni pasos extra; la observación persiste y afecta las propuestas futuras **del producto** | «Sin fricción» se mantiene: tampoco se agrega un paso para ver justificaciones, porque no se muestran | Sin cambios de flujo; el botón pasa a llamarse «Enseñar a la IA» en O2–O4 |
| **D-133** | Ningún criterio comercial altera un número: precios, totales, elegibilidad y cantidades no cambian | El **orden** no es un número; ordenar no altera precios, totales ni elegibilidad | Se mantiene la regla y se agrega que el orden por preferencias tampoco cambia la composición de la lista |
| **AC31 (HLD)** | Toda recomendación permite ver reglas, ofertas y versiones que la originaron | D-136/D-138 prohíben mostrar explicaciones o puntuaciones en la interfaz | **D-141:** AC31 se satisface con **auditoría interna** (observación, interpretación, modelo, versión, criterios y ofertas), accesible para revisión administrativa, **no** como explicación al usuario. El HLD no se edita |
| **D-136** | Preferencias interpretadas internamente; ordenan ofertas; todas visibles; sin explicaciones ni puntuaciones | Se precisa el alcance | **D-138** elimina las «preferencias de organización» y fija el producto específico |
| **D-138 (C7)** | Texto original, autor, fecha, contexto; observaciones independientes; precedencia por producto | No se propaga a otros productos del mismo laboratorio, marca o proveedor; no hay perfiles globales | Se aplica en O2–O4, §8.4, §8.6 y §8.7 |
| **D-139 (C9)** | Cotizar no cierra el faltante; solo la compra registrada cierra | `closed` = preparada para comprar; conserva cobertura provisional; se compra sin reabrir | Se aplica en §6 y §12.3 |
| **D-140 (C11)** | Asignaciones explícitas; suma ≤ cantidad comprada | La compra puede incluir unidades sin asignación para inventario; no cierran reportes | Se aplica en §6.1 y §12.3 |
| **D-141 (AC31)** | Trazabilidad de la IA | La traza es interna, no una explicación visible | Se aplica en §8.4 |
| **A1/A5/A6** | Cualquier producto comercializado; preferencias por producto; todas las ofertas visibles | — | Se explicita el alcance no farmacéutico y la no propagación |
| **A3/A4** | Primero se recuperan ofertas reales; después se ordenan; sin conectores reales no se inventan datos | — | Sin cambios: la IA no sustituye la recuperación |

**No se elimina ninguna decisión anterior.** D-116, D-132 y D-133 siguen en el registro; la revisión 10 solo anota qué
cláusulas quedan sustituidas y por qué.

---

## 4. Lo que esta propuesta no hace

- No modifica `docs/contracts/cotizador-v0.2.md` (sigue en revisión 9).
- No modifica el HLD ni borra decisiones.
- No implementa O2–O4, «Enseñar a la IA», orden por preferencias ni mantenedor de proveedores.
- No implementa C9/D-139 ni C11/D-140: no cambia `coverage.py`, no permite comprar desde `closed` y no crea tablas de
  compra.
- No cambia el esquema ni crea la migración `0019`.
- No crea la migración `0018` de compras.
- No inicia C5-B, O2–O4, C5-C ni C5-D.
- No declara aprobada la revisión 10 hasta que la apruebes.

---

## 5. Puntos que requieren tu decisión antes de aplicar la revisión 10

1. **Aprobar la revisión 10 completa.** C7, C9, C11 y AC31 ya están decididos (D-138 a D-141); lo que falta es tu
   aprobación expresa del texto contractual que los incorpora.
2. **Almacenamiento de secretos de proveedores (A3).** Sigue pendiente como decisión **técnica**: cifrado en reposo o
   gestor de secretos. No afecta el texto de esta revisión y no se implementa sin definirlo.
3. **Oportunidad de implementación.** Autorizar o no C5-B (`0018`), O2–O4 (`0019`) y la aplicación de `0016`–`0017` en
   `farmacia_demo`. El contenido funcional ya está decidido; la oportunidad es tuya.

Nada de esto se implementa sin tu orden expresa.
