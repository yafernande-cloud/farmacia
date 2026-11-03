# Acuerdos funcionales del cotizador — Cotizador Inteligente Farmacia

**Estado:** **propuesta de trabajo para tu revisión y aprobación**. No es todavía el documento de referencia oficial
del proyecto: queda en revisión hasta que lo apruebes expresamente. Registra lo ya acordado y su estado real, **no
habilita implementación** de lo pendiente ni cambia decisiones por iniciativa propia.

**Fecha:** 2026-11-02. **Rama:** `master` (sin commit, sin push, sin despliegue).
**Fuentes:** `docs/Farmacia_Parque_Lantano_HLD_LLD.md` (sin editar), `docs/ARQUITECTURA.md`, `docs/ROADMAP.md`,
`docs/contracts/cotizador-v0.2.md` (revisión 9 vigente; revisión 10 **propuesta** en
`docs/contracts/cotizador-v0.2-rev10-propuesta.md`), `docs/decisions/0001-registro-de-decisiones.md` (D-015 a D-141),
`CLAUDE.md` y el código y las pruebas de `backend/`.

**Decisiones definitivas incorporadas el 2026-11-02:** **C7 → D-138** (preferencias independientes por producto, sin
propagación), **C9 → D-139** (cerrar una cotización significa «preparada para comprar», no «comprada»), **C11 → D-140**
(cantidades compradas sin asignación quedan como compra adicional para inventario) y **AC31 → D-141** (la trazabilidad
de la IA es auditoría interna, no explicación visible). Estas cuatro decisiones **sustituyen** las observaciones que
este documento mantenía abiertas; no se presentan como pendientes de definición. Su implementación sigue **sin
autorizar** y la revisión 10 del contrato sigue **sin aprobar y sin aplicar**.

**Regla de clasificación.** Un acuerdo solo se marca **implementado** si existe código que lo ejecuta y al menos una
prueba automatizada que lo verifica. **Implementado en código no significa desplegado, aplicado a una base ni verificado
con servicios reales.** Las migraciones `0016` y `0017` existen en el código y **no están aplicadas a ninguna base**
(`farmacia`: 0008; `farmacia_demo`: 0015). El bot real de Telegram sigue sin verificar (H1-B.5).

Estados:

- **A — Aprobado e implementado** (código + prueba; se indica si depende de una migración no aplicada).
- **B — Aprobado, pendiente de implementación** (hay decisión, no hay código completo).
- **C — Pendiente de decisión** (falta una regla o hay una contradicción sin resolver; no se implementa sin orden).

**Ningún acuerdo queda en estado C.** C7, C9, C11 y AC31 fueron **decididos** el 2026-11-02 (D-138 a D-141). Lo que
sigue abierto es su **implementación** (no autorizada) y las limitaciones materiales ya declaradas: conectores reales,
credenciales, IA real, migraciones `0016`–`0017` no aplicadas y bot real sin verificar.

---

## 1. Resumen

| # | Acuerdo | Estado | Evidencia principal |
|---|---|---|---|
| A1 | Búsqueda de cualquier producto comercializado, no solo medicamentos | **B** (catálogo A; extremo a extremo B) | `trade_item` neutral al tipo de producto; pruebas con «Cepillo dental»; falta prueba de punta a punta |
| A2 | Grandes cadenas con búsqueda bajo demanda de publicaciones públicas y precios reales | **B** | E15 responde, pero las tres cadenas están sin conector autorizado; no hay precios reales; sin conexión se muestra el estado, nunca datos ficticios |
| A3 | Proveedores y cuentas configurables e independientes por organización | **B** (modelo base A; mantenedor y conexiones B) | `supplier` y `source` son por organización; no hay URL, contacto comercial, cuentas, credenciales, estado de conexión ni conectores |
| A4 | Catálogos con ofertas reales y apoyo de IA cuando corresponda | **B** (importación A; ofertas reales e IA B) | `catalog-load`/`manual_import` funcionan; el catálogo de demo es ficticio y la IA es simulada |
| A5 | Preferencias comerciales vía «Enseñar a la IA» interpretadas internamente, **solo para el producto específico** | **B** (O1 A; O2–O4 B) | La observación comercial se guarda sin IA; no hay interpretación, recuperación por producto ni orden (D-138) |
| A6 | Preferencias **del producto específico** ordenan las ofertas; todas visibles; sin explicaciones ni puntuaciones de IA | **B** | Aprobado por **D-136** y precisado por **D-138**; depende de O2–O4 (`0019`). Hoy no existe el orden ni «Enseñar a la IA» |
| A7 | Telegram: reportes independientes, advertencia de coincidencias y confirmación explícita | **A** en código (migración `0016` no aplicada; bot real pendiente) | D-135; pruebas de canal y API citadas en §3.7 |
| A8 | Cantidades sin consolidar, sumar ni modificar automáticamente | **A** | `SET`/`ADD`/`CLEAR` explícitos, idempotencia y cantidad desconocida como `NULL` |
| A9 | Cotizar no cierra el faltante; **cerrar una cotización la deja preparada para comprar** | **A parcial**: cotizar no cierra (implementado); cobertura de `closed` y compra sin reabrir (**D-139, aprobado, no implementado**) | Hoy la cobertura solo cuenta `draft`; el comportamiento actual debe corregirse cuando se autorice (C9/D-139) |
| A10 | Solo la compra confirmada y registrada cierra los reportes cubiertos | **B** | C5-B (`0018`) no existe: no hay tablas de compra ni cierre. **D-140:** la compra puede incluir cantidades sin asignación para inventario |
| A11 | Auditoría, permisos y aislamiento entre organizaciones | **A** (con brechas declaradas) | Matriz de permisos, auditoría+outbox en la misma transacción, pruebas de aislamiento y **traza interna de la IA (D-141)** |

---

## 2. Flujo funcional completo (aprobado; no operativo de punta a punta)

El flujo aprobado va desde el aviso de un auxiliar hasta el cierre del faltante por compra registrada. **Hoy solo
funciona hasta la cotización en `draft`.** Las etapas 7 (cotización preparada), 8 (compra registrada) y 9 (cobertura y
cierre) tienen decisiones aprobadas, pero **no** código que las ejecute; C5-B (`0018`) **no existe**.

El flujo integral definitivo es:

**Telegram → Reportes independientes → Revisión administrativa → Búsqueda de proveedores → Ordenamiento por
preferencias específicas del producto → Carrito → Cotización preparada → Compra registrada → Cobertura y cierre de
faltantes.**

### 2.1 Etapas y estado real

| # | Etapa | Qué ocurre | Estado |
|---|---|---|---|
| 1 | **Reporte por Telegram** | Un auxiliar autorizado informa un producto faltante. Se conservan su identidad, el mensaje original y la cantidad declarada; si no la declara, queda `NULL`. | **Implementado en código** (D-135; `0016` no aplicada; probado con simulador) |
| 2 | **Coincidencias** | Si otro auxiliar ya reportó el mismo producto, Telegram lo advierte y pide confirmación. Cada auxiliar conserva **su** reporte independiente: no hay consolidación ni suma automática. | **Implementado en código** (D-135; simulador) |
| 3 | **Revisión administrativa** | Pao o la administradora autorizada revisa los reportes individuales y decide qué cantidades se cotizan. **La cantidad informada no se convierte automáticamente en cantidad de compra.** | **Implementado en código** para cantidades (`SET/ADD/CLEAR` con `admin` + `StrongActor`); las asignaciones explícitas dependen de `0017`, **no aplicada**. **La decisión comercial es humana** |
| 4 | **Búsqueda de proveedores** | Se consultan, por separado, (a) el catálogo propio de la organización, (b) las ofertas de proveedores conectados y (c) la búsqueda pública en Cruz Verde, Salcobrand y Farmacias Ahumada. Solo se muestran datos reales de fuentes autorizadas; si una fuente no está conectada, se muestra su estado y **nunca** datos ficticios. | **Parcial:** búsqueda sobre catálogo local implementada (C2/C4); **no hay conectores de proveedor ni ofertas reales**. E15 de cadenas responde `not_configured` |
| 5 | **Ordenamiento por preferencias específicas del producto** | Las observaciones comerciales de **ese producto** se interpretan y se usan para **ordenar** sus ofertas. No se aplican a otros productos del mismo laboratorio, marca o proveedor. Todas las ofertas siguen visibles, sin explicaciones ni puntuaciones de IA. | **No implementado** (O2–O4, `0019`; D-136 y D-138). Solo O1 guarda el texto |
| 6 | **Carrito** | Se eligen productos y cantidades. Una línea comercial puede asociarse explícitamente con varios reportes, sin duplicar cantidades. | **Implementado en código** (C5-A + D-135; `0015` aplicada solo a `farmacia_demo`, `0016`–`0017` no aplicadas) |
| 7 | **Cotización preparada** | `draft` = en preparación. Al cerrar, la cotización queda **preparada para convertirse en compra**: conserva líneas, cantidades, proveedores y asignaciones, y **sigue aportando cobertura provisional**. Cerrar **no** registra una compra ni cierra reportes. Se puede comprar desde `closed` **sin reabrir**. | **`draft`/`closed` implementados; la cobertura desde `closed` y la compra sin reabrir están aprobadas (D-139) y NO implementadas.** Hoy el código solo cuenta `draft` y bloquea la compra con `409 QUOTE_CLOSED` |
| 8 | **Compra registrada** | La administradora registra la compra de la selección **completa o no la registra**. Las asignaciones explícitas determinan qué cantidad cubre cada reporte; la cantidad comprada **puede superar** lo asignado y el excedente queda como **compra adicional para inventario**, sin asignación automática. | **No implementado** (C5-B, `0018`; D-139 y D-140) |
| 9 | **Cobertura y cierre de faltantes** | Solo las compras registradas aportan cobertura **comprada**. Los reportes completamente cubiertos pasan a `fulfilled` y se liberan para un nuevo reporte; los parcialmente cubiertos conservan su cantidad pendiente. Cotizado y comprado nunca se suman dos veces. | **No implementado** (C5-B, `0018`). Hoy `fulfilled` no puede ocurrir |

**No presentes este flujo como operativo.** Sin `0016`–`0017` aplicadas, ni siquiera las etapas 1, 2 y 6 corren en
`farmacia` o `farmacia_demo` con el esquema nuevo; sin `0018` no hay compra ni cierre; y la semántica de `closed`
descrita en la etapa 7 **todavía no coincide con el código** (D-139).

### 2.2 Ejemplo con Juanito y María

Producto: **paracetamol 500 mg, caja de 20 comprimidos**. Ciclo activo.

1. **Juanito reporta** por Telegram: «no hay paracetamol de 500». Declara **10 cajas**. Se crea el reporte **R1**
   (`reported_by = Juanito`, `target_quantity = 10 cajas`). El mensaje original queda guardado.
2. **María reporta** el mismo producto y declara **6 cajas**. El bot detecta que Juanito ya lo reportó y pregunta:
   **Confirmar mi reporte · Modificar cantidad · Cancelar**. María confirma.
3. Se crea el reporte **R2** (`reported_by = María`, `target_quantity = 6 cajas`). **No** se suman 10 + 6 en una fila,
   **no** se adjunta María al reporte de Juanito y **no** se crea una tercera necesidad. Hay dos reportes de 10 y 6.
4. **Pao revisa** ambos reportes y decide cotizar las cantidades completas (podría decidir otra cosa: la cantidad
   informada no es una orden de compra).
5. **Búsqueda y comparación:** aparecen las ofertas de los proveedores conectados. Una observación como «para este
   paracetamol prefiero Laboratorio Chile, aunque cueste un poco más» **ordena solo las ofertas de este paracetamol**;
   no favorece ni perjudica a Laboratorio Chile en otros productos. Todas las ofertas siguen visibles y no se muestra
   explicación ni puntuación de IA.
6. **Carrito:** Pao elige una línea comercial de **20 cajas** del proveedor X y la asigna explícitamente:
   **10 a R1** y **6 a R2**. La suma asignada (16) no supera la línea (20); las 4 restantes quedan **sin asignación** y
   no cubren ningún reporte. No hay reparto automático.
7. **Cotización preparada:** Pao cierra la cotización. Queda en `closed`, conserva líneas y asignaciones, y **sigue
   aportando cobertura provisional** a R1 y R2; cerrar no registra compra. R1 y R2 siguen activos (`is_pending = true`,
   estado `in_quote`), con `pending_purchase_quantity = 10` y `6`. **Este comportamiento está aprobado (D-139) pero el
   código actual todavía lo contradice.**
8. **Compra registrada (C5-B, no implementado):** Pao registra la compra de la línea completa de 20 **desde la
   cotización cerrada, sin reabrirla**. Se asignan **10 a R1** y **6 a R2**; las **4 restantes son compra adicional para
   inventario** y no se asignan a ningún reporte ni crean reportes ficticios (D-140). La compra se registra completa o
   no se registra; no hay compras parciales de la selección.
9. **Cobertura y cierre (C5-B, no implementado):** R1 y R2 pasan a `fulfilled` porque la compra registrada cubre sus
   10 y 6 cajas, y se liberan para un nuevo reporte. Si un reporte quedara cubierto solo en parte, **seguiría activo**
   con su cantidad pendiente. Las 4 cajas de inventario no cierran ningún reporte. `fulfilled` **no** significa que la
   mercadería haya llegado: la recepción está fuera del MVP (D-126).

**Qué debe quedar claro en la demo:** de las etapas 7 (cobertura de `closed` y compra sin reabrir), 8 y 9 **no hay
código**. El ejemplo describe el comportamiento aprobado, no algo que hoy se pueda ejecutar.

---

## 3. Detalle de los acuerdos

### A1 — Búsqueda de cualquier producto comercializado (no solo medicamentos)

**Acordado.** La búsqueda y el cotizador aceptan **cualquier producto que una farmacia comercialice**: medicamentos,
cosmética, higiene, perfumería, productos veterinarios y otros artículos. No se agregan atributos farmacéuticos
obligatorios a productos que no los necesitan. La búsqueda distingue **tres orígenes**: catálogo propio de la
organización, ofertas de proveedores conectados y búsqueda pública en Cruz Verde, Salcobrand y Farmacias Ahumada. Un
origen no se presenta como si fuera otro, y una cadena sin integración autorizada muestra su estado, no precios
inventados.

**Implementado:**

- `trade_item` es **neutral al tipo de producto**: `product_kind` e `ingredient` son nullable y nunca se infieren
  (D-094).
- `tests/test_catalog_load.py` carga «Cepillo dental suave» y verifica `(product_kind, ingredient) == (None, None)`.
- `tests/test_imports_manual.py` usa «Cepillo dental» junto a «Losartán 50 mg x 30».
- E1/E2 buscan sobre artículos y ofertas registrados sin exigir que el artículo sea un medicamento.

**Brecha de pruebas declarada:** **no existe una prueba de extremo a extremo de un producto no farmacéutico** que
recorra Telegram → reporte → búsqueda → comparación → cotización → **compra → cierre**. Hoy solo se prueba por partes, y
la compra todavía no existe. Esta brecha debe cerrarse con C5-B y con una prueba de canal que use, por ejemplo, un
cepillo dental o un shampoo.

**Pendiente:** la búsqueda **externa** de cualquier producto fuera del catálogo propio depende de A2 y no está
disponible.

### A2 — Grandes cadenas, búsqueda bajo demanda de publicaciones públicas y precios reales

**Aprobado:** D-113 (Cruz Verde, Salcobrand y Farmacias Ahumada, en vivo y bajo demanda, para cualquier producto de
farmacia, sin datos ficticios ni precargados como sustituto). Contrato §11.

**Implementado:** `POST /api/v1/chain-searches` y su sondeo existen (C4), con estado en memoria y sin persistencia. Las
pruebas exigen que **no se inventen** resultados: hoy las tres cadenas responden `not_configured` con su motivo
(`requires_javascript`, `search_not_available`, `access_not_authorized`).

**No implementado:** ningún conector autorizado; no hay precios ni fichas reales de cadena. Cualquier conector exige
autorización escrita/API del titular o una decisión explícita sobre el acceso (contrato §11.7). Mientras no exista
conexión, la cadena se muestra con su estado (`not_configured` y motivo) y **nunca** con datos ficticios, precargados
o simulados presentados como reales.

### A3 — Proveedores y cuentas configurables e independientes por organización

**Acordado.** Cada organización configura **sus propios proveedores y cuentas de acceso**. El mantenedor debe permitir:

1. **Registro del proveedor:** nombre, identificación (código o RUT si corresponde), **URL**, **datos de contacto
   comercial** y **estado de conexión**.
2. **Cuenta de acceso:** usuario y contraseña **cuando corresponda**. **No se impone OAuth como requisito universal**:
   el mecanismo depende de lo que cada proveedor realmente admita y autorice (API con token, usuario y contraseña,
   carga manual, etc.).
3. **Almacenamiento seguro de credenciales, exclusivamente en el backend**: nunca en el frontend, en respuestas HTTP, en
   logs, en auditoría legible, en git ni en capturas. Se guardan como secreto y se usan solo en el conector.
4. **Conexión y consulta:** catálogo, precios, disponibilidad y condiciones comerciales **correspondientes a esa
   cuenta**, mediante mecanismos autorizados.
5. **Aislamiento por organización:** cuentas, credenciales, ofertas, preferencias y configuraciones no se comparten ni
   se mezclan entre organizaciones.
6. **Estados claros cuando no haya conexión:** sin configurar, sin credenciales, error de conexión, acceso no
   autorizado o capacidad no soportada. Nunca se simula una respuesta ni se muestran precios inventados.

**Implementado (solo la base de datos):** `supplier` y `source` llevan `organization_id`, claves foráneas compuestas y
unicidad por organización. `source.mode` distingue `api`, `manual_import`, `fixture` y `none`; `enabled`,
`last_success_at` y `last_error` describen el estado de la fuente. La carga de catálogo y la importación manual escriben
ofertas por organización (`tests/test_org_separation.py`).

**No implementado:**

- No hay **URL** ni datos de identificación del proveedor en el modelo (`supplier` solo tiene nombre, código, notas y
  estado).
- No existen **cuentas de proveedor ni credenciales**: no hay tabla, cifrado, gestor de secretos ni conector.
- No hay **consulta real** de catálogo, precios ni disponibilidad. `source` describe una fuente; no se autentica.
- No existen los estados de conexión enumerados arriba; el único estado implementado es el de E15 para cadenas
  (`not_configured` y motivos).
- **Ningún conector real está declarado ni probado.** El catálogo de demo es ficticio y está etiquetado `fixture`.

**Decisión pendiente de ejecución:** la forma concreta de almacenar secretos (gestor de secretos o cifrado en reposo)
es una decisión técnica de implementación; **no está aprobada** y no se inventa aquí.

### A4 — Catálogos con ofertas reales y apoyo de IA cuando corresponda

**Acordado.** Primero se **recuperan las ofertas reales disponibles** de los proveedores conectados a la organización;
después se aplican las preferencias comerciales aprendidas para ordenarlas antes de mostrarlas. La IA **no** reemplaza
la recuperación ni inventa ofertas.

**Implementado:**

- `catalog-load` con conectores de importación y `manual_import`, perfiles de importación, `needs_review` y estado de
  revisión de oferta (C2; `0009`–`0011`).
- Búsqueda y comparación sobre ofertas registradas (C4, C3).

**No implementado / limitado:**

- Las ofertas del entorno de demo son **ficticias** (`backend/fixtures/catalog_demo.json`, etiquetado como fixture).
- La interpretación con IA existe solo como **simulador** (`AIInterpreter`); no hay proveedor real ni visión real, y
  todo lo simulado debe decir `simulated`.
- «Apoyo de IA cuando corresponda» se concreta en A5/A6 (preferencias que ordenan), no en un chatbot ni en una capa que
  decida precios, stock o compras.

### A5 — «Enseñar a la IA»: preferencias del producto específico, interpretadas internamente

**Acordado (D-136 y D-138).** «Enseñar a la IA» permite a un administrador escribir **comentarios en lenguaje natural,
sin formularios ni categorías obligatorias**. Cada comentario se refiere a **un producto específico** y solo se aplica
a las ofertas de **ese producto**. La IA lo **interpreta internamente** en el contexto de ese producto y lo convierte
en una preferencia aplicable a sus futuras búsquedas: por ejemplo, «para este Losartán prefiero Laboratorio Chile,
aunque cueste un poco más», «para este artículo prefiero proveedores con stock» o «para este producto prioriza el
precio cuando las demás condiciones sean similares».

La IA **no** es un chatbot visible: no hay conversación, no hay campo de chat y no hay respuestas explicativas.

**Flujo aprobado:**

1. El administrador registra la observación en lenguaje natural.
2. Se conserva el **texto original**, autor, fecha y contexto del producto.
3. La IA interpreta la observación **en el contexto de ese producto**.
4. La interpretación queda disponible para futuras búsquedas **de ese producto**.
5. Se aplica a las ofertas reales recuperadas, solo para **ordenarlas**.
6. El frontend muestra el orden **sin razonamientos, justificaciones, recomendaciones textuales ni puntuaciones**.

**Independencia (regla central de C7/D-138):**

- Cada observación es **independiente** y se aplica **únicamente al producto específico** al que se refiere.
- Las preferencias positivas o negativas **no se propagan** a otros productos del mismo laboratorio, marca o
  proveedor. «No me gusta el Losartán de Laboratorio Chile» afecta las ofertas de **ese Losartán**, pero **no**
  perjudica a Laboratorio Chile en otro medicamento.
- **No** se construyen perfiles globales de reputación de laboratorios, marcas o proveedores.
- Varias observaciones sobre el mismo producto se interpretan con contexto y **precedencia** (la más reciente para la
  misma clase de preferencia); las anteriores no se borran y quedan en la traza interna.
- No se supone equivalencia por compartir laboratorio, marca o proveedor.
- Aplica también a cosmética, higiene, perfumería, veterinaria y cualquier otro producto comercializado.

**Implementado (solo O1):**

- `commercial_observation` guarda el texto libre, autor, fecha y contexto, de forma inmutable y **sin IA**
  (`tests/test_quotes_api.py::test_observacion_comercial_se_guarda_sin_ia_y_es_inmutable`).
- La ruta es `/api/v1/commercial-observations`, separada de los avisos de faltante (D-129).
- Cualquier `admin` puede registrar la observación, sin categorías ni formularios (D-132).

**No implementado:**

- **O2** (interpretación con modelo), **O3** (recuperación por producto y precedencia) y **O4** (aplicación del orden)
  **no existen en código**. No hay `observation_interpretation`, ni llamada a OpenAI, ni preferencias interpretadas.
- La etiqueta «Enseñar a la IA» no está implementada en la interfaz ni en la API: hoy el botón es «Observación».
- La observación **no afecta todavía** ninguna propuesta ni orden: solo se guarda y se muestra.
- **No hay conexiones reales a proveedores ni IA real.** Cualquier demostración con simulador debe decir `simulated`.

### A6 — Las preferencias del producto ordenan sus ofertas; la IA no muestra explicaciones, justificaciones ni puntuaciones

**Estado: aprobado (D-136, precisado por D-138), pendiente de implementación (O2–O4).**

Reglas definitivas:

1. Los administradores escriben comentarios en lenguaje natural mediante **«Enseñar a la IA»**.
2. La IA **interpreta internamente** cada comentario **en el contexto del producto específico**.
3. La preferencia se aplica **solo a las ofertas de ese producto**; no se propaga a otros productos del mismo
   laboratorio, marca o proveedor.
4. El sistema usa las preferencias para **ordenar las ofertas recuperadas de ese producto**: primero las que mejor
   cumplen.
5. **Todas las demás ofertas siguen visibles**; no se ocultan ni se eliminan.
6. **No** se muestran explicaciones, justificaciones, recomendaciones textuales ni puntuaciones generadas por IA.
7. La IA **no inventa** ofertas, precios, stock ni condiciones comerciales.
8. La IA **no modifica** precios, stock, cantidades ni decisiones de compra.
9. La IA **no** se presenta como un chatbot visible.
10. **No se construyen perfiles globales de reputación** de laboratorios, marcas o proveedores.

**Relación con A4.** El orden es la **última** etapa: primero se recuperan ofertas reales (A4), después se ordenan por
las preferencias **de ese producto** (A5/A6) y recién entonces se muestran. Si no hay ofertas, no hay nada que ordenar
y no se inventan.

**Alcance de producto.** Aplica a **cualquier producto comercializado** por la farmacia: cosmética, higiene, perfumería,
veterinaria y otros. Las preferencias pueden referirse a laboratorio, marca, precio, disponibilidad u otros criterios
comerciales, **siempre dentro del producto específico**.

**Contradicción documental.** El contrato vigente (revisión 9) todavía describe un `advice` **visible y explicable** y
prohíbe «reordenar o destacar ofertas» (§8.4, §8.6). D-136 y D-138 lo sustituyen. La **revisión 10** está preparada
como propuesta en `docs/contracts/cotizador-v0.2-rev10-propuesta.md`; no se aplicó. El HLD no se edita y AC31 se
satisface con **auditoría interna** (D-141), no con una explicación visible.

### A7 — Telegram: reportes independientes, advertencia de coincidencias y confirmación explícita

**Implementado en código (D-135; migración `0016` no aplicada):**

- Cada persona tiene su propio `demand_item` (`reported_by`), con unicidad por autor y organización.
- **La confirmación de una coincidencia no fusiona, no suma y no reemplaza** el reporte de otra persona: crea un
  reporte propio dentro del mismo ciclo. Cada reporte conserva autor, producto, cantidad declarada, cantidad objetivo,
  historial y estado propios.
- Si el producto ya lo reportó **otra persona**, el mensaje queda `pending_confirmation` con
  `confirmation_status='pending_other_report'` y **no se crea ni se adjunta nada** hasta que la persona pulse un botón.
- Botones: **Confirmar mi reporte**, **Modificar cantidad**, **Cancelar**. Cancelar deja el mensaje `discarded` sin
  crear nada y conserva el autor para trazabilidad.
- Mismo autor con cantidad conocida: **Total (SET)**, **Sumar (ADD)** (solo si lo declarado supera lo pedido), **solo
  registrar aviso** o **Cancelar**. Nunca hay suma automática.
- El auxiliar sin `APPLY_QUANTITY` deja `quantity_proposal`; no cambia la cantidad.

**Evidencia:** `tests/test_h1b_channel.py::test_luis_cambia_identidad_mismo_producto_dos_avisos_sin_sumar`,
`::test_otro_autor_modifica_su_cantidad_y_crea_su_reporte_independiente`,
`::test_otro_autor_puede_cancelar_su_aviso_sin_crear_nada`, `::test_pao_fija_el_total_y_suma_solo_con_botones`,
`::test_el_auxiliar_solo_deja_una_propuesta_y_no_aplica_la_cantidad`;
`tests/test_api_demand.py::test_dos_auxiliares_conservan_reportes_independientes`.

**Límites que no se deben ocultar:**

- Todo se probó con el **simulador** de Telegram (`tests/tgsim.py`), no con el bot real (H1-B.5 sigue abierta).
- La migración `0016` **no está aplicada**: en `farmacia` y `farmacia_demo` el comportamiento operativo sigue siendo
  el anterior (un faltante compartido) hasta que se aplique con autorización.
- `admin` **no** puede reportar por Telegram: la matriz vigente no le da `REPORT_SHORTAGE` (D-124 aprobada, no
  implementada). Las pruebas usan `auxiliary` y `purchasing`.

**Escenarios no cubiertos:** tres o más autores sobre el mismo producto; dos personas pulsando el botón a la vez;
vencimiento de una confirmación pendiente; producto no farmacéutico; bot real.

### A8 — Cantidades sin consolidar, sumar ni modificar automáticamente

**Implementado:**

- Solo `SET`, `ADD` y `CLEAR` explícitos (`backend/app/services/quantities.py`); cantidad desconocida = `NULL`.
- Idempotencia por clave y contenido: repetir no suma dos veces; la misma clave con otro contenido da
  `409 IDEMPOTENCY_KEY_REUSED`.
- Control de versión optimista (`If-Match`) y conflicto con el estado actual.
- El aviso repetido no toca la cantidad; el valor declarado queda como `declared_quantity`.
- La historia completa de cambios se conserva y se expone en el detalle (`quantity_events`).
- **La cantidad reportada no es cantidad de compra.** La compra se decide y registra aparte, con asignaciones
  explícitas.
- **D-137:** con asignaciones vigentes, la cantidad no puede bajar por debajo de lo asignado ni quedar sin definir.

**Evidencia:** `tests/test_demand_services.py::test_aviso_repetido_no_aumenta_la_cantidad`,
`::test_cantidad_desconocida_permanece_nula`, `::test_add_repetido_con_misma_clave_no_suma_dos_veces`,
`::test_mismo_evento_entregado_varias_veces_una_sola_observacion`,
`::test_add_concurrentes_con_la_misma_version_solo_uno_gana`; `tests/test_api_demand.py::test_flujo_cantidades_por_http`,
`::test_reenvio_idempotente_devuelve_200_y_no_duplica`;
`tests/test_h1b_channel.py::test_saludos_preguntas_y_ediciones_no_crean_ni_cambian_nada`,
`::test_el_bot_nunca_aplica_cantidades`; `tests/test_quotes_api.py::test_no_se_puede_bajar_la_cantidad_pedida_por_debajo_de_lo_asignado`.

### A9 — Cotizar no cierra el faltante; cerrar una cotización la deja preparada para comprar

**Regla aprobada (D-139/C9).**

1. **`draft`** significa que la cotización está **en preparación**: se busca, se compara y se eligen cantidades.
2. **`closed`** significa que la cotización **terminó de prepararse y puede convertirse posteriormente en una compra**.
   No significa comprada, descartada ni anulada.
3. **Cerrar no registra una compra.** La compra se registra después, en un acto separado.
4. Una cotización cerrada **conserva** líneas, cantidades, proveedores y asignaciones.
5. **No debe ser necesario reabrir** la cotización para comprar.
6. Las asignaciones de cotizaciones **`draft` y `closed`** cuentan para la **cobertura provisional**, sin duplicar.
7. Una cotización **anulada o descartada no aporta** cobertura.
8. La **cobertura comprada** proviene **solo de compras registradas**.
9. **Nunca se cuenta dos veces** lo cotizado y lo comprado.
10. Los reportes se cierran **solo por una compra registrada** que cubra sus cantidades.
11. Las cotizaciones cerradas sin compra **permanecen en el historial**.
12. El frontend distingue **«Cotización preparada»** de **«Compra registrada»**.

**Qué está implementado hoy:**

- `coverage.py` calcula `quoted_quantity` desde `quote_line_allocation` de líneas activas en carritos activos de
  cotizaciones **`draft`**. `pending_quantity` es lo que falta cotizar. `is_pending` sigue `true` mientras falte
  compra registrada; `replenishment_state` puede ser `in_quote` sin cerrar.
- Agregar una línea nunca suma cantidades: la misma oferta con otra cantidad da `409 LINE_EXISTS`.
- Cerrar y reabrir existen, con `closed_at`/`closed_by`, auditoría y validación de asignaciones (D-137).

**Qué está aprobado y NO implementado (D-139):**

- **Cambiar el cálculo de cobertura provisional para incluir cotizaciones `closed`** y no solo `draft`, sin doble
  conteo con compras.
- **Permitir registrar la compra desde una cotización `closed` sin reabrirla.** Hoy `_ensure_draft()` responde
  `409 QUOTE_CLOSED` y el contrato prohíbe comprar sin reabrir; ese comportamiento **debe corregirse** cuando se
  autorice la implementación de C5-B.
- **Distinguir en la API y el frontend «Cotización preparada» de «Compra registrada».**

**Estado del código frente a C9: desalineado, no corregido.** El comportamiento actual —`closed` deja de aportar
cobertura y exige reabrir para comprar— **contradice D-139**. Se documenta como deuda aprobada; **no se modificó
código** porque la última orden fue solo documental. La corrección pertenece a C5-B y debe incluir pruebas nuevas.

**Evidencia de lo implementado:** `tests/test_quotes_api.py::test_vinculo_congela_relacion_y_cubre_sin_cerrar`,
`::test_descartar_carrito_resta_cobertura_y_reincorporar_la_devuelve`,
`::test_quitar_linea_resta_cobertura_sin_cerrar_la_cotizacion`,
`::test_agregar_linea_crea_carrito_y_nunca_suma`.

#### A9.1 Qué significa `closed` en el modelo actual (y qué significará)

**`quote.status` tiene solo dos valores: `draft` y `closed`.**

- Al cerrar se guardan `closed_at` y `closed_by`; la restricción de base exige que ambos existan si y solo si
  `status = 'closed'`.
- Cerrar **no** modifica líneas, cantidades, asignaciones, precios ni compras.
- Cerrar exige que las asignaciones sean consistentes (D-137); si no lo son, responde
  `409 QUOTE_HAS_INCONSISTENT_ALLOCATIONS` y la cotización sigue `draft`.
- Reabrir (`status: "draft"`) limpia `closed_by`/`closed_at` y **no** cambia las asignaciones ni las líneas. Reabrir
  deja de ser un requisito para comprar (D-139).
- **Hoy no se puede modificar una cotización `closed`**: cualquier edición da `409 QUOTE_CLOSED` hasta reabrirla. Esto
  se mantiene para la *preparación*; la **compra** es un acto posterior permitido sobre `closed` (D-139).
- Una cotización `closed` **no se archiva ni se borra** por política (contrato §7.2, §9).

**Efecto actual sobre la cobertura (a corregir):** al cerrar, sus líneas dejan de aportar cobertura **cotizada**,
porque el cálculo solo cuenta cotizaciones `draft`. El `quoted_quantity` del reporte baja y `pending_quantity` puede
subir; el reporte puede volver de `in_quote` a `pending`. **D-139 dispone lo contrario:** una cotización cerrada
conserva su cobertura provisional hasta que se descarte expresamente o se registre la compra. Las **compras
registradas**, cuando existan (C5-B), cubren y sustituyen la cobertura cotizada sin doble conteo (contrato §12.3).

#### A9.2 Qué ocurre con el historial y las líneas

- La cotización conserva **todas** sus líneas, con su `quantity` cotizada, su estado (`active`/`removed`), su
  `purchase_state` y su `remainder`.
- Las líneas pendientes de una cotización cerrada pasan a `remainder = closed_by_quote` (o `closed_by_user` si Pao ya
  había desistido de esa línea). **No se borran.** Esto sigue siendo válido: el cierre no es una compra.
- Los carritos, las evaluaciones y las observaciones se conservan.
- La auditoría y el outbox registran el cierre y la reapertura.
- **D-139:** una cotización cerrada sin compra sigue en el historial como **«Cotización preparada»**, no como compra ni
  como descarte.

#### A9.3 Las cuatro cantidades

| Cantidad | Qué mide | Dónde vive | Estado |
|---|---|---|---|
| **Reportada** (`target_quantity`) | Lo que la persona declaró que falta. Puede ser `NULL`. | `demand_item` | Implementado |
| **Cotizada** (`quoted_quantity`) | Lo asignado explícitamente desde líneas activas de carritos activos de cotizaciones **`draft` y `closed`**. Es **provisional**. | Derivada de `quote_line_allocation` | **Parcial:** hoy solo cuenta `draft`; incluir `closed` es D-139, no implementado |
| **Comprada** (`purchased_quantity`) | Lo que una compra registrada y vigente asigna al reporte. Es el único hecho que cierra. | Derivada de `purchase_line_allocation` | **No implementado** (C5-B); hoy siempre 0 |
| **Pendiente de comprar** (`pending_purchase_quantity`) | `target_quantity − purchased_quantity`, nunca negativo. | Derivada | Implementado el cálculo; sin compras, hoy es igual a la cantidad pedida conocida |

Además, **`pending_quantity`** = `target_quantity − (cotizado + comprado)`: es lo que falta **cotizar**, no lo que falta
comprar. Con cotización completa y sin compra, `pending_quantity = 0` y `pending_purchase_quantity = target_quantity`.
**No se suman dos veces** cotizado y comprado: cuando existe compra registrada, esa cantidad sustituye a la cotizada
para el mismo reporte.

#### A9.4 Cómo evitar que una cotización cerrada se interprete como una compra

Reglas de presentación y contrato (implementadas en las respuestas, pendientes de respaldo visual en Lovable):

1. El estado de la **cotización** no es el estado del **faltante**. `closed` describe la cotización; el faltante sigue
   `active` con `is_pending = true` hasta que exista compra registrada que lo cubra.
2. El estado derivado de la cotización distingue `in_preparation`, `partially_purchased`, `fully_purchased` y
   **`closed_without_purchase`**. Una cotización cerrada sin compras se muestra como **«Cotización preparada»** (no
   «comprada»). **Hoy el código solo puede devolver `in_preparation` y `closed_without_purchase`**, porque no hay
   compras registradas; los otros dos estados llegan con C5-B.
3. **«Cotización preparada»** y **«Compra registrada»** son etiquetas distintas y no se sustituyen entre sí. Cerrar
   produce la primera; solo el registro de la compra produce la segunda.
4. Nunca se usa un check verde ni la palabra «comprado» si `purchased_quantity = 0`.
5. `fulfilled` solo aparece cuando existe compra registrada que cubre la cantidad pedida conocida; hoy **no puede
   ocurrir**.
6. La interfaz no debe inferir compra de un total cotizado: muestra por separado cotizado y comprado.

#### A9.5 Qué debe mostrar el frontend a Pao

Para cada **reporte** (no solo para la cotización):

- Producto, autor del reporte y fecha.
- **Cantidad reportada** (o «sin cantidad declarada», nunca 0 ni 1).
- **Cantidad cotizada** marcada como **provisional**; incluye cotizaciones `draft` y `closed` (D-139).
- **Cantidad comprada** marcada como **confirmada**, con la leyenda de que **no significa recibido**.
- **Pendiente de cotizar** y **pendiente de comprar**, como dos saldos distintos.
- `is_pending` («¿sigue abierto?») y `replenishment_state` («¿en qué etapa está?»: pendiente, en cotización, comprado,
  cumplido, cancelado).
- Para la cotización: su estado (`draft`/`closed`) y su estado derivado, con **«Cotización preparada»** cuando esté
  cerrada sin compra, y **«Compra registrada»** solo cuando exista una compra.
- Las ofertas ordenadas por las preferencias **del producto**, sin explicación ni puntuación de la IA.
- Las ofertas y el carrito con su cobertura explícita por reporte; nunca un total agregado que oculte asignaciones.

#### A9.6 Decisión ya tomada (antes observación C9)

**Cerrar una cotización ya no se considera un callejón sin salida.** D-139 dispone que la cotización cerrada conserva
su cobertura provisional y puede comprarse **sin reabrir**. La advertencia al cerrar y la etiqueta «Cotización
preparada» siguen siendo recomendables como experiencia de usuario, pero **no** cambian la regla: cerrar no compra, no
descarta y no cierra reportes.

**Lo que falta es implementación, no decisión.** El cambio afecta `coverage.py`, la validación de compra en
`quotes.py`, el contrato §12.3 y las pruebas citadas en §8.

### A10 — Solo la compra registrada cierra los reportes cubiertos; puede incluir compra adicional para inventario

**No implementado.** C5-B (`0018`) no existe.

**Reglas aprobadas (D-127 enmendada, D-139 y D-140):**

1. La compra se registra **completa o no se registra**: todos los productos y cantidades seleccionados. **No hay
   compras parciales de la selección.**
2. La compra se registra **desde la cotización cerrada, sin reabrirla** (D-139).
3. Las **asignaciones son explícitas**: una compra puede cubrir uno o varios reportes solo mediante
   `purchase_line_allocation` con cantidades declaradas.
4. **La compra puede superar lo requerido.** No es obligatorio asignar toda la línea. La suma asignada **≤ cantidad
   comprada**.
5. La cantidad **sin asignar** queda como **compra adicional para inventario**. No se asigna automáticamente, no crea
   reportes ficticios y **no cierra ningún reporte**.
6. **No se modifica automáticamente** la cantidad de ningún reporte por una compra.
7. Un reporte pasa a `fulfilled` **solo** cuando una compra registrada cubre **toda** su cantidad pedida conocida.
   Si queda parcialmente cubierto, **sigue activo** con su cantidad pendiente.
8. Se conserva la **trazabilidad** de qué compra, línea y asignación cubrió qué reporte, y qué cantidad quedó como
   inventario.
9. Cotizado y comprado **no se suman dos veces**; la cobertura comprada sustituye a la provisional para el mismo
   reporte.

**Estado real del código:**

- No hay tabla de compras, ni `purchase_line_allocation`, ni endpoint de registro de compra.
- `coverage._purchased_totals` devuelve `{}` y `demand._has_registered_purchase` devuelve `False`, ambos con el
  comentario de que C5-B los conectará. Por eso `409 REPORT_HAS_PURCHASE` existe en el código pero **no puede ocurrir**
  hoy.
- `LINE_HAS_PURCHASES` y `QUANTITY_BELOW_PURCHASED` **no están implementados**: aparecen solo como comentarios en
  `quotes.py` y como texto en el contrato.
- El cierre `fulfilled`, la liberación del producto y la compra adicional para inventario tampoco existen.
- **Nada de esto se presenta como operativo.**

### A11 — Auditoría, permisos y aislamiento entre organizaciones

**Implementado:**

- Matriz de permisos verificada en los servicios (`backend/app/core/permissions.py`): `admin` = `VIEW_LIST`,
  `MANAGE_STAFF`, `MANAGE_ACCOUNTS`, `MANAGE_IDENTITIES`, `MANAGE_WEB_ACCESS`; **no** tiene `REPORT_SHORTAGE` ni
  `APPLY_QUANTITY`. `purchasing` aplica cantidades; `auxiliary` solo reporta y propone.
- Cotizar, comprar y cancelar exigen `admin` + `StrongActor` (D-130).
- Auditoría y outbox se escriben en la misma transacción (`services/events.emit`); si falla la operación no queda
  auditoría ni evento.
- Aislamiento por organización en cotizaciones, faltantes, búsqueda, accesos web y catálogo.
- **D-137:** una cotización no se cierra con asignaciones inconsistentes; la cantidad pedida no baja por debajo de lo
  asignado. Nada se corrige ni se reasigna solo.
- **D-141/AC31:** la trazabilidad de la IA se conserva **internamente**: qué observaciones se interpretaron, cuáles se
  aplicaron y qué versiones, modelo y criterios participaron en el ordenamiento. **No** se muestran explicaciones,
  recomendaciones textuales ni puntuaciones en las tarjetas. La traza es para revisión administrativa y auditoría, no
  para el usuario final. D-116, D-132, D-133 y AC31 se mantienen; D-141 precisa qué parte de AC31 queda satisfecha por
  auditoría interna en lugar de por explicación visible.

**Evidencia:** `tests/test_quotes_api.py::test_solo_admin_y_solo_su_organizacion`,
`::test_el_evento_de_auditoria_y_el_outbox_se_escriben_juntos`,
`::test_cerrar_bloquea_asignaciones_a_reportes_cancelados_y_permite_corregir`,
`::test_cerrar_bloquea_cantidades_de_reporte_que_ya_no_cubren_lo_asignado`;
`tests/test_api_demand.py::test_aislamiento_entre_organizaciones_por_http`;
`tests/test_demand_services.py::test_auditoria_y_outbox_se_escriben_con_el_cambio`,
`::test_si_falla_la_operacion_no_queda_auditoria_ni_evento`;
`tests/test_org_separation.py`; `tests/test_web_access_api.py::test_campanita_solo_para_admin_y_acotada_a_su_organizacion`,
`::test_otra_organizacion_no_puede_aprobar`.

**Brechas declaradas:**

1. `admin` no puede reportar por Telegram hasta que se implemente D-124 (§4-C4).
2. Las protecciones frente a compras (`REPORT_HAS_PURCHASE`, `LINE_HAS_PURCHASES`, `QUANTITY_BELOW_PURCHASED`)
   pertenecen a C5-B y no existen todavía.

---

## 4. Contradicciones y observaciones para decisión

| # | Tema | Estado |
|---|---|---|
| C1 | Preferencias ordenan ofertas y sin explicaciones visibles vs. contrato §8.4/§8.6, D-116, D-132, D-133 y AC31 | **Decidida** por D-136 y D-138; revisión 10 **propuesta** y no aplicada. No requiere nueva decisión funcional |
| C2 | HLD §7.3/§7.4: «misma necesidad compatible» / «se adjunta al existente» vs. D-135 | **Resuelta**: D-135 prevalece; constancia en D-135 y D-131. El HLD no se edita |
| C3 | D-131, punto 3 y estado («solo documental, sin código») | **Resuelta**: nota de vigencia; punto 3 sustituido por D-135; estado corregido |
| C4 | D-124 aprobada y no implementada, pero algunos textos la presentan como disponible | **Revisada**: sigue sin implementar; alcance y efecto documentados |
| C5 | «Ofertas reales» vs. catálogo ficticio y ausencia de conectores | **Abierta como limitación**, no como contradicción: los fixtures están etiquetados |
| C6 | Cierre de cotización sin validación de asignaciones | **Resuelta e implementada** por D-137 |
| C7 | Alcance de las preferencias: ¿por producto o por organización? | **Resuelta por D-138**: solo el producto específico; sin propagación ni perfiles globales |
| C8 | El contrato asigna O2–O4 a la migración `0016`; el roadmap y las decisiones fijan `0019` | **Corrección documental** en la revisión 10 propuesta; no requiere decisión funcional |
| C9 | Cerrar una cotización quita su cobertura provisional y no se puede comprar sin reabrir | **Resuelta por D-139**: `closed` = preparada para comprar, conserva cobertura provisional y se compra sin reabrir. **El código actual la contradice; corrección no implementada** |
| C10 | `advice` visible en `GET /quotes/{id}` y «no altera el orden» vs. D-136 | Forma parte de C1; se corrige en la revisión 10 propuesta |
| C11 | Una línea de 20 asignada 10/6 deja 4 sin reporte: ¿qué cantidad registra la compra? | **Resuelta por D-140**: la compra registra las 20; 16 cubren reportes y 4 son compra adicional para inventario. No implementado |
| C12 | AC31 exige ver reglas, ofertas y versiones que originaron una recomendación | **Resuelta por D-141**: se satisface con auditoría interna, no con explicación visible |

### C7 — Preferencias independientes por producto (resuelta por D-138)

**Decisión definitiva.** Cada observación comercial se aplica **únicamente al producto específico** al que se refiere.
No hay preferencias de organización que ordenen todos los productos, ni propagación por laboratorio, marca o
proveedor.

Ejemplo normativo: «No me gusta el Losartán de Laboratorio Chile» afecta las ofertas de **ese Losartán**, pero **no**
perjudica a Laboratorio Chile en otro medicamento. Del mismo modo, «prefiero Laboratorio Chile para este paracetamol»
no favorece a ese laboratorio en ningún otro producto.

Reglas:

1. Se conserva el texto original, autor, fecha y contexto.
2. Cada observación es independiente.
3. La interpretación respeta el producto específico.
4. Las preferencias positivas o negativas **no se propagan** a otros productos del mismo laboratorio, marca o
   proveedor.
5. **No** se construyen perfiles globales de reputación.
6. Varias observaciones del mismo producto se interpretan con contexto y precedencia.
7. La IA puede **ordenar** las ofertas de ese producto según esas observaciones.
8. Todas las ofertas siguen visibles.
9. Sin explicaciones, justificaciones, recomendaciones textuales ni puntuaciones de IA.
10. La IA no modifica precios, stock, cantidades ni decisiones de compra.

**Efecto documental:** se elimina toda referencia a **preferencias reutilizables de la organización** o a propagación
automática entre productos en A5, A6, O2–O4 y la propuesta de revisión 10. La revisión 9 del contrato ya exigía
observaciones por artículo exacto y prohibía aplicar una observación de marca o laboratorio a otro artículo; D-138
**confirma** esa prohibición y elimina la ambigüedad que introducía la redacción anterior de A5/A6. Aplica también a
cosmética, higiene, perfumería, veterinaria y otros productos.

### C4 — Revisión de D-124

**Qué dice:** `admin` también informa faltantes por Telegram; se agrega `REPORT_SHORTAGE` a su rol y puede reportar si
tiene cuenta de Telegram `active` e identidad vigente.

**Estado real:** **no implementada**. `permissions.py` no incluye `REPORT_SHORTAGE` en `admin` y
`sessions.list_reporters` lo excluye del selector. Aprobar un acceso web no crea cuenta ni vínculo de Telegram.

**Efecto si se implementara:** solo agrega `REPORT_SHORTAGE` a `admin` y lo incluye en el selector; no crea roles, no
amplía `APPLY_QUANTITY` (la encargada `purchasing` sigue aplicando) y no toca D-130. Debe hacerse en la fase que
habilite T1, con pruebas propias y sin cambios «de paso» en la matriz.

**Decisión pendiente:** solo la oportunidad (fase), no el contenido.

### C9 — Cierre de cotización (resuelta por D-139)

**Decisión definitiva.** Cerrar una cotización significa que **terminó de prepararse y puede convertirse
posteriormente en una compra**. No significa comprada, descartada ni anulada.

Flujo aprobado de seis etapas:

1. **Preparación:** buscar, comparar, seleccionar cantidades.
2. **Carrito:** agregar ofertas; conserva proveedor, producto, presentación, cantidad, precio y condiciones.
3. **Revisión:** agrupada por proveedor, con cantidades, subtotales y condiciones.
4. **Cierre:** `draft` → `closed`; conserva líneas, cantidades, proveedores y asignaciones.
5. **Registro de compra:** desde la cotización cerrada, **sin reabrirla**.
6. **Actualización de faltantes:** solo lo comprado y asignado genera cobertura; los reportes completamente cubiertos
   se cierran; los parcialmente cubiertos conservan pendiente.

Reglas obligatorias:

1. `draft` = en preparación.
2. `closed` = preparada y disponible para compra.
3. Cerrar no registra compra.
4. Cerrada conserva líneas y asignaciones.
5. **No debe ser necesario reabrir para comprar.**
6. Asignaciones de cotizaciones `draft` **y `closed`** cuentan para cobertura provisional, sin duplicar.
7. Cotización anulada o descartada no aporta cobertura.
8. Cobertura comprada solo desde compras registradas.
9. No contar dos veces cotizado y comprado.
10. Reportes se cierran solo por compra registrada que cubra sus cantidades.
11. Cotizaciones cerradas sin compra permanecen en historial.
12. Frontend distingue «Cotización preparada» de «Compra registrada».

**Contradicción real que queda documentada.** El contrato vigente (revisión 9) §12.3 cuenta la cobertura cotizada solo
desde `draft`, y §6 impide registrar compras sobre una cotización cerrada sin reabrir. **D-139 los sustituye.** D-121 ya
había fijado que «cerrar la cotización no quita cobertura»; el código de C5-A implementó solo `draft`, así que la brecha
es de implementación. Hasta que se autorice la corrección, el código conserva el comportamiento anterior y **no debe
presentarse como alineado con C9**. La corrección afecta `coverage.py`, `quotes.py`, el contrato §12.3 y las pruebas de
cobertura al cerrar.

### C11 — Cantidades compradas sin asignación (resuelta por D-140)

**Decisión definitiva.** Se permite comprar cantidades adicionales para inventario. La compra registra la cantidad
comprada; solo las asignaciones explícitas cubren reportes; las unidades sin asignar quedan como **compra adicional
para inventario**.

Ejemplo normativo: Juanito reporta 10 y María 6. Pao cotiza 20 y asigna 10 a Juanito y 6 a María. Las **4 restantes**
se compran sin reporte y **no** se asignan automáticamente ni generan reportes ficticios. La compra registra 20; solo
16 cubren reportes.

Reglas:

1. La compra puede **superar** lo requerido.
2. **No es obligatorio** asignar toda la línea.
3. Las asignaciones son **explícitas**.
4. La suma asignada **≤ cantidad comprada**.
5. Las unidades sin asignar quedan como **compra adicional para inventario**.
6. **No se modifica automáticamente** la cantidad de ningún reporte.
7. **No se cierran reportes** por cantidades no asignadas y compradas.
8. Se conserva la **trazabilidad** de la compra, la línea, las asignaciones y el excedente de inventario.

**Efecto técnico:** `purchase_line_allocation` (C5-B, `0018`) debe permitir que la suma asignada sea **menor** que la
cantidad comprada y debe registrar el excedente sin asignación. No se implementa todavía. La compra sigue siendo
**todo o nada** respecto de la selección: se registra completa —incluidas las unidades de inventario— o no se registra.

### C12 — AC31: auditoría interna de la IA (resuelta por D-141)

**Decisión definitiva.** La trazabilidad de la IA se conserva **internamente**: qué observaciones se interpretaron,
cuáles se aplicaron y qué versiones, modelo y criterios participaron en el ordenamiento. **No** se muestran
explicaciones, recomendaciones textuales ni puntuaciones en las tarjetas.

- La traza es para revisión administrativa y auditoría; no es una explicación al usuario final.
- D-116, D-132, D-133 y AC31 se mantienen vigentes.
- Se deja constancia de qué reglas históricas quedaron sustituidas: el `advice` visible de D-116 y la prohibición de
  reordenar del contrato §8.6 son sustituidos por D-136/D-138; AC31 se satisface por auditoría interna, no por
  explicación visible.
- El HLD no se edita.

---

## 5. Matriz de validación de D-135

| # | Punto a validar | Estado | Evidencia | Escenarios que faltan |
|---|---|---|---|---|
| 1 | Reportes de auxiliares distintos son independientes aunque sea el mismo producto | **Cumplido** | `test_api_demand.py::test_dos_auxiliares_conservan_reportes_independientes`; `test_demand_services.py::test_dos_auxiliares_conservan_dos_reportes_independientes`; `test_h1b_channel.py::test_luis_cambia_identidad_mismo_producto_dos_avisos_sin_sumar` | Tres o más autores; producto no farmacéutico; bot real |
| 2 | Telegram exige confirmación cuando otro auxiliar ya reportó | **Cumplido** (simulador) | `test_luis_cambia_identidad_mismo_producto_dos_avisos_sin_sumar`; `test_otro_autor_modifica_su_cantidad_y_crea_su_reporte_independiente` | Dos clics simultáneos; confirmación vencida; reintento mientras está pendiente; bot real |
| 3 | El mismo auxiliar actualiza o complementa sin sumas automáticas ni pérdida de historial | **Cumplido** | `test_pao_fija_el_total_y_suma_solo_con_botones`; `test_demand_services.py::test_add_repetido_con_misma_clave_no_suma_dos_veces`, `::test_set_no_suma_y_clear_vuelve_a_desconocida`; detalle con `quantity_events` | Prueba explícita de que `quantity_events` conserva **todas** las operaciones tras varios `SET`/`ADD`; `CLEAR` desde Telegram |
| 4 | Las asignaciones permiten cotizar varios reportes sin duplicar cantidades | **Cumplido** | `test_quotes_api.py::test_una_linea_cubre_dos_reportes_independientes`; `::test_patch_reemplaza_las_asignaciones_sin_tocar_el_resto` | Tres o más reportes en una línea; un reporte repartido en dos líneas; asignación desde otra organización (hoy `404`, sin prueba dedicada) |
| 5 | Cancelar o modificar un reporte no permite cerrar una cotización con asignaciones inconsistentes | **Cumplido (D-137)** | `::test_cerrar_bloquea_asignaciones_a_reportes_cancelados_y_permite_corregir`; `::test_cerrar_bloquea_cantidades_de_reporte_que_ya_no_cubren_lo_asignado`; `::test_no_se_puede_bajar_la_cantidad_pedida_por_debajo_de_lo_asignado` | Cancelar un reporte de una cotización **ya cerrada** (hoy se permite; hay que reabrir y corregir); dos cotizaciones con el mismo reporte; reporte cancelado con carrito descartado |
| 6 | Agregar al carrito o cotizar no cierra faltantes | **Cumplido** | `test_vinculo_congela_relacion_y_cubre_sin_cerrar`; `test_quitar_linea_resta_cobertura_sin_cerrar_la_cotizacion`; `test_agregar_linea_crea_carrito_y_nunca_suma` | **C9/D-139 aprobado y sin prueba:** una cotización `closed` debe seguir aportando cobertura provisional y poder comprarse sin reabrir. El código actual hace lo contrario; la prueba debe escribirse al implementar |
| 7 | Permisos y separación entre organizaciones se mantienen | **Cumplido** | `test_solo_admin_y_solo_su_organizacion`; `test_aislamiento_entre_organizaciones_por_http`; `test_org_separation.py`; `permissions.py` | Asignación de un reporte de otra organización (hoy `404`, sin prueba dedicada); `admin` reportando por Telegram (bloqueado por D-124) |
| 8 | Protecciones de C5-B identificadas y no presentadas como implementadas | **Cumplido como declaración** | `coverage._purchased_totals` devuelve `{}`; `demand._has_registered_purchase` devuelve `False`; no hay tablas de compra; `LINE_HAS_PURCHASES` y `QUANTITY_BELOW_PURCHASED` solo en comentarios | Implementar C5-B (`0018`) y probar compra todo o nada, cierre, liberación **y compra adicional para inventario (D-140)**: línea de 20, 16 asignadas y 4 sin reporte |

### Escenario de Ana (punto 5), tal como quedó probado

1. Pao agrega una línea de 8 y asigna 5 al reporte de Ana y 3 al de otra persona.
2. Pao cancela el reporte de Ana: `200`, con `affected_quote_lines` y `quote_status: "draft"`.
3. Pao intenta cerrar: `409 QUOTE_HAS_INCONSISTENT_ALLOCATIONS`, `details.issues = [{code: REPORT_NOT_ACTIVE, ...}]`.
   La cotización sigue en borrador y las asignaciones siguen intactas.
4. Pao ajusta la línea a la asignación del reporte activo y cierra: `200`.
5. Si en cambio baja la cantidad pedida de Ana por debajo de 5: `409 QUANTITY_BELOW_ALLOCATED` con
   `details.allocated`, `requested` y la lista de líneas afectadas. Para dejarla en 4, primero quita o ajusta la
   asignación; `PATCH demand_allocations: []` la quita.

---

## 6. Protecciones reservadas a C5-B (no implementadas)

| Protección | Dónde se nombra | Estado real |
|---|---|---|
| `REPORT_HAS_PURCHASE` al cancelar un reporte comprado | `demand.cancel_item` | El código existe, pero `_has_registered_purchase` siempre devuelve `False`; **no puede ocurrir** |
| `LINE_HAS_PURCHASES` al quitar o mover una línea comprada | Contrato §5.3; comentarios en `quotes.py` | **No implementado** |
| `QUANTITY_BELOW_PURCHASED` al bajar una cantidad comprada | Contrato §5.3; comentarios en `quotes.py` | **No implementado** |
| Compra todo o nada y cierre de los reportes cubiertos | D-127 enmendada; contrato §6 y §12 | **No implementado** |
| Compra desde una cotización `closed` sin reabrir | D-139; contrato §6 y §12.3 | **No implementado**: hoy `_ensure_draft()` responde `409 QUOTE_CLOSED` |
| Cobertura provisional de cotizaciones `closed` | D-139; contrato §12.3 | **No implementado**: hoy solo cuenta `draft` |
| Compra adicional para inventario (unidades sin asignación) | D-140; `purchase_line_allocation` | **No implementado** |
| Liberación del producto para un nuevo reporte tras la compra | D-131/D-135 | **No implementado** |
| `purchase_line_allocation` y sustitución de la cobertura de cotización | Contrato §6.1 y §12.3 | **No implementado** |
| Traza interna de la IA (observación, interpretación, versión y criterios de orden) | D-141/AC31 | **No implementado**: O1 guarda el texto, pero no hay interpretación ni orden que auditar |
| Mantenedor de proveedores, cuentas, credenciales y conexiones | A3 | **No implementado** |

Ninguna de estas filas debe presentarse como una garantía del sistema actual.

---

## 7. Migración `0016`: autores históricos, reversión y recomendación

### Autores históricos ambiguos

La 0016 rellena `demand_item.reported_by` **solo** cuando todas las observaciones del faltante tienen el mismo autor.
Si hubo varios autores históricos, la fila queda `NULL` y se trata como **legado consolidado**: no se inventa un autor
ni se divide la fila automáticamente. El índice único nuevo excluye `reported_by IS NULL`, de modo que esos legados no
bloquean la migración ni impiden crear reportes nuevos.

**Verificación previa recomendada:**

```sql
-- Faltantes activos sin autor histórico único (legado)
SELECT count(*) FROM demand_item WHERE status = 'active' AND reported_by IS NULL;

-- Faltantes cancelados sin fecha de cancelación (deberían ser 0 tras el backfill)
SELECT count(*) FROM demand_item WHERE status = 'cancelled' AND cancelled_at IS NULL;
```

Si el segundo conteo no es 0, la migración fallará en `ck_demand_item_cancelacion_coherente`; hay que revisar esos
datos antes.

### Reversión

El `downgrade` de la 0016 **se detiene con un error explícito** si existen dos o más reportes activos con la misma
identidad en el mismo ciclo, porque el esquema anterior exige uno solo. El mensaje indica organización, ciclo,
identidad y cantidad de reportes, y no borra nada.

El `downgrade` de la 0017 **elimina** `quote_line_allocation` y **pierde** las asignaciones múltiples; solo sobrevive el
atajo `quote_line.demand_item_id` cuando la línea tenía una sola. Hay que exportar o respaldar las asignaciones antes
de revertir.

### Recomendación para probar `0016`–`0017` en `farmacia_demo`

**No se aplicó nada.** Requiere autorización explícita. Solo `farmacia_demo` (organización `simulation`); `farmacia`
sigue en `0008` y no se toca.

**Antes de aplicar:**

1. Confirmar `alembic current` = `0015` y **47 tablas**.
2. Respaldo `pg_dump` de `farmacia_demo` y verificación restaurando en una base temporal con conteos idénticos.
3. Ensayo previo en una base desechable: `upgrade 0016`, `upgrade 0017`, `downgrade 0015` y `upgrade head`.
4. Ejecutar las dos consultas de la sección anterior y revisar los resultados.
5. No crear reportes duplicados por persona antes de ensayar la reversión, o resolverlos a mano después.

**Secuencia propuesta:**

1. `alembic upgrade 0016` → verificar: **7 columnas nuevas** (4 en `demand_item`, 2 en `channel_message` y 1 en
   `demand_observation`), índices y restricciones, tablas siguen en **47**, `alembic current` = `0016`.
2. `alembic upgrade 0017` → verificar: **48 tablas** (aparece `quote_line_allocation`), backfill de las líneas con
   `demand_item_id`, `alembic current` = `0017`.
3. Ejecutar la suite contra esa base con `REQUIRE_DB=1`.
4. Prueba funcional mínima: dos auxiliares reportan el mismo producto → dos reportes y confirmación explícita; Pao fija
   cantidades y asigna 5+3 a una línea de 8; cancelar un reporte asignado y comprobar `409
   QUOTE_HAS_INCONSISTENT_ALLOCATIONS`; corregir las asignaciones y cerrar.
5. Reversión ensayada según los dos riesgos anteriores.

**Condiciones:** autorización explícita; ventana sin uso de la demo; ninguna otra migración; nada de commit, push ni
despliegue.

---

## 8. Matriz de impacto técnico previsto (no implementado)

Esta matriz identifica qué cambiaría **cuando se autorice** implementar C7/D-138, C9/D-139, C11/D-140 y AC31/D-141.
**No se modificó código, no se crearon migraciones nuevas y no se aplicó ninguna migración** al preparar este
documento.

| Decisión | Archivo / módulo | Cambio previsto | Migración | Pruebas que se agregarán o ajustarán |
|---|---|---|---|---|
| **C7/D-138** | `backend/app/models/quote.py` | `observation_interpretation` guarda la interpretación ligada al **producto exacto** (`trade_item_id`), con modelo, prompt, versión, tokens, costo y latencia; **sin** tabla de preferencias globales por organización | `0019` | `test_la_interpretacion_queda_ligada_al_producto`; `test_no_existe_preferencia_global_por_laboratorio` |
| **C7/D-138** | `backend/app/services/interpretation.py` y un servicio nuevo de preferencias comerciales | Interpretar el texto **en el contexto del producto**; recuperar solo observaciones de ese `trade_item_id`; precedencia de la más reciente para la misma clase; ninguna propagación | — | `test_preferencia_de_losartan_no_afecta_otro_producto_del_mismo_laboratorio`; `test_preferencia_de_marca_no_se_propaga_a_otro_producto`; `test_dos_observaciones_del_mismo_producto_usan_la_mas_reciente` |
| **C7/D-138** | `backend/app/services/search.py`, `backend/app/services/queries.py`, `backend/app/services/quotes.py` | Ordenar las ofertas del producto según sus preferencias; **no** cambiar el conjunto, precios, stock, elegibilidad ni totales; sin campo de explicación ni puntuación | — | `test_orden_no_oculta_ofertas_ni_cambia_precios_ni_stock`; `test_sin_preferencias_el_orden_es_el_determinista_de_e5` |
| **C7/D-138** | `backend/app/api/v1/search.py`, `backend/app/api/v1/quotes.py`, `backend/app/api/v1/schemas.py` | Exponer las ofertas ordenadas; **no** devolver `advice`, `stance`, `based_on`, `supersedes` ni puntuaciones | — | `test_la_respuesta_no_expone_explicaciones_ni_puntuaciones` |
| **C9/D-139** | `backend/app/services/coverage.py` | Contar cobertura provisional de cotizaciones **`draft` y `closed`** en `quoted_totals_subquery` y `_quoted_totals`; al existir compra registrada, esta sustituye la cobertura cotizada sin doble conteo | — | `test_cotizacion_cerrada_sigue_cubriendo_sin_cerrar_el_reporte`; `test_compra_registrada_sustituye_la_cobertura_cotizada_sin_sumar_dos_veces` |
| **C9/D-139** | `backend/app/services/quotes.py` | `_ensure_draft()` deja de bloquear la **compra** sobre `closed`; se mantiene para editar la preparación. El registro de compra se permite desde `closed` sin reabrir | `0018` (compras) | `test_se_compra_desde_cotizacion_cerrada_sin_reabrir`; `test_editar_una_cotizacion_cerrada_sigue_dando_quote_closed` |
| **C9/D-139** | `backend/app/api/v1/quotes.py`, `backend/app/api/v1/schemas.py` | Distinguir en la respuesta **«Cotización preparada»** (`closed` sin compra) de **«Compra registrada»**; conservar `closed_without_purchase` como estado derivado, sin llamarlo «comprada» | — | `test_etiqueta_cotizacion_preparada_no_es_compra_registrada` |
| **C9/D-139** | `docs/contracts/cotizador-v0.2.md` §6, §12.3 | Revisión 10 propuesta: cobertura desde `draft` y `closed`; compra sin reabrir; cierre solo por compra registrada | — | Revisión documental; no aplicada |
| **C11/D-140** | `backend/app/models/purchase.py` (nuevo) y `backend/migrations/versions/0018_*` | `purchase`, `purchase_line` y `purchase_line_allocation`; la suma asignada puede ser **menor** que la cantidad comprada; el excedente queda sin asignación (inventario) | `0018` | `test_modelos_y_migracion_estan_sincronizados`; `test_asignaciones_no_superan_la_cantidad_comprada` |
| **C11/D-140** | `backend/app/services/purchases.py` (nuevo) | Registrar la compra completa o nada; asignaciones explícitas; no asignar automáticamente; no crear reportes; no cerrar reportes por unidades no asignadas; conservar trazabilidad del excedente | `0018` | `test_compra_de_20_con_16_asignadas_deja_4_de_inventario`; `test_compra_adicional_no_cierra_ni_crea_reportes`; `test_compra_parcial_de_la_seleccion_se_rechaza` |
| **C11/D-140** | `backend/app/services/coverage.py`, `backend/app/services/demand.py` | `_purchased_totals` real; cierre `fulfilled` solo por compra asignada que cubra la cantidad pedida; reporte parcialmente cubierto sigue activo | `0018` | `test_reporte_parcialmente_cubierto_sigue_activo`; `test_reporte_completamente_cubierto_pasa_a_fulfilled`; `test_unidades_de_inventario_no_cierran_reportes` |
| **C11/D-140** | `backend/app/api/v1/purchases.py` (nuevo), `backend/app/api/v1/schemas.py` | Endpoint de registro de compra con `admin` + `StrongActor`, idempotencia, versión y montos `Decimal` | `0018` | `test_compra_exige_admin_y_strong_actor`; `test_compra_es_idempotente`; `test_compra_no_acepta_float_ni_cantidades_negativas` |
| **AC31/D-141** | `backend/app/models/quote.py`, `backend/app/services/interpretation.py`, auditoría | Guardar traza interna: observación, interpretación, modelo, versión del prompt, criterios y orden aplicado; **no** exponerla en la API pública | `0019` | `test_la_traza_interna_registra_interpretacion_y_orden`; `test_la_traza_no_se_expone_en_la_respuesta_de_ofertas` |
| **AC31/D-141** | Frontend Lovable | Mostrar solo el orden; sin explicaciones, justificaciones, recomendaciones textuales ni puntuaciones | — | Verificación visual/manual; no hay prueba automatizada de frontend en este repositorio |

**Orden de implementación previsto, si se autoriza:** `0018` (C5-B con C9 y C11, porque la compra y la cobertura
dependen de la misma semántica) → `0019` (O2–O4 con C7 y AC31) → `0020` (C5-C) → `0021` (C5-D, bloqueado). La
migración `0016`–`0017` sigue pendiente de aplicación y es requisito para probar el flujo completo.

**Riesgos declarados:**

1. **C9 cambia una regla ya implementada.** No es una migración: es un cambio de semántica en `coverage.py` y en la
   validación de compra. Debe hacerse con pruebas que demuestren que no hay doble conteo.
2. **C11 amplía el alcance de C5-B.** La compra adicional para inventario no debe interpretarse como permiso para
   compras parciales de la selección ni para asignar automáticamente.
3. **C7 no admite atajos de «reputación».** Cualquier índice, caché o perfil por laboratorio/marca/proveedor que se
   construya para ordenar **incumple D-138**.
4. **AC31 no autoriza explicaciones visibles.** La traza interna es para auditoría; la interfaz no la muestra como
   justificación.
5. **Nada de esto es código hoy.** No debe declararse implementado, probado con proveedores reales ni desplegado.

---

## 9. Lo que este documento no hace

- No implementa A1–A6 ni A10 ni el mantenedor de proveedores.
- No implementa C7/D-138, C9/D-139, C11/D-140 ni AC31/D-141.
- No aplica `0016`–`0017`.
- No modifica el HLD. D-131 y D-124 quedaron anotadas en el registro de decisiones; el contrato sigue en revisión 9 y la
  revisión 10 está **propuesta**, no aplicada.
- No inicia C5-B (`0018`), O2–O4 (`0019`), C5-C (`0020`) ni C5-D (`0021`).
- No declara probado con Telegram real ni con proveedores reales lo que solo se probó con simuladores.
- No se declara aprobado como documento de referencia hasta que lo apruebes expresamente.
