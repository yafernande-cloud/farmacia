# Prompts del Cotizador Inteligente

Este directorio reúne los prompts reales que la aplicación envía a OpenAI, sus versiones, el
golden set y la forma de evaluarlo. Sigue el [Prompt Maestro](../PROMPT_MAESTRO.md) y no cambia
el código del Cotizador ni los prompts del código actual. No afirma qué commit está desplegado en
producción: las versiones documentadas son las del repositorio.

## 1. Dónde usa la aplicación un modelo

La aplicación usa OpenAI en dos tareas concretas y separadas:

| Tarea | Cuándo se ejecuta | Prompt | Modelo | Versión en la traza |
| --- | --- | --- | --- | --- |
| Leer una foto de un envase | Una auxiliar envía una foto por Telegram | `vision/prompt-v3.md` (vigente) | `OPENAI_VISION_MODEL`, sin valor por defecto | No guarda versión; se identifica por commit |
| Interpretar una preferencia comercial | La administradora usa «Enseñar a la IA» | `observaciones/prompt-v3.md` (vigente) | `OPENAI_MODEL`, sin valor por defecto | `a6-observations-v3` |

Todo lo demás es código determinista: el parser de texto de Telegram, la validación de la salida,
la traducción de una lectura a un producto, el orden de las ofertas y la finalización de la
cotización. El modelo **no** decide precios, cantidades, stock ni permisos.

## 2. Estructura

```text
prompts/
├── README.md                     este documento
├── vision/
│   ├── README.md                 contrato, validaciones y límites de la lectura de fotos
│   └── prompt-v1.md … v3.md      texto exacto de cada versión, extraído de Git
├── observaciones/
│   ├── README.md                 contrato, validaciones y límites de las preferencias
│   └── prompt-v1.md … v3.md      texto exacto de cada versión, extraído de Git
├── golden-set/
│   ├── README.md                 qué es cada caso y qué evidencia lo respalda
│   ├── casos.json                17 casos con entrada, salida esperada y verificaciones
│   └── imagenes/                 fotos sintéticas de V1–V4 y V6 (V5 y V7 no llevan imagen)
├── evaluacion/
│   ├── README.md                 cómo se evalúa y qué se puede afirmar con cada método
│   ├── evaluar_validacion.py     evaluación offline contra los validadores reales
│   ├── resultados-validacion.json
│   └── evaluar_openai.py         arnés para llamadas reales (no ejecutado)
└── comparacion/
    └── README.md                 versiones comparables, protocolo y costo pendiente
```

## 3. Versiones reales

Los textos se extrajeron del código con `git show` y análisis del AST, no se reescribieron.

### Visión

| Versión | Commit | Estado | Cambio principal |
| --- | --- | --- | --- |
| v1 | `03a75d8` (2026-11-06) | histórica | Primera lectura de envases |
| v2 | `9921311` (2026-11-06) | histórica | Admite medicamentos combinados y prohíbe elegir entre dos valores de un atributo único |
| v3 | `d7b963a` (2026-11-09) | vigente | El nombre comercial manda; la indicación terapéutica no es `variant` |

Las tres versiones comparten el mismo esquema de salida. Eso permite compararlas con las mismas
imágenes y el mismo modelo.

### Observaciones comerciales

| Versión | Commit | Estado | Cambio principal |
| --- | --- | --- | --- |
| v1 | `9783379` (2026-11-03) | histórica | Primera interpretación de preferencias |
| v2 | `e1176d2` (2026-11-03) | histórica | Laboratorio y marca del catálogo aunque no tengan oferta |
| v3 | `641ec93` (2026-11-11) | vigente | Agrega `article_operations` para preferir un artículo frente a otro |

v1 y v2 tienen el mismo esquema y son comparables directamente. v3 agrega una capacidad nueva y
cambia el esquema: se compara como ampliación, no como simple mejora de redacción. El detalle está
en [comparacion/README.md](comparacion/README.md).

## 4. Contrato de salida y validaciones

Ambos adaptadores usan la **Responses API** con `json_schema` estricto (`strict=true`),
`store=false` y reintento único solo para fallos transitorios.

**Visión.** Devuelve `legibility`, una lista de `fields` (`attribute`, `text`, `confidence`) y
`warnings`. El backend rechaza la lectura completa si un atributo único aparece con dos valores
distintos, si la confianza está fuera de `0..1` o si el atributo no está permitido. Después traduce
la lectura a un producto: `product_name` es el ancla de la búsqueda, `visible_quantity` se ignora
como cantidad solicitada y todo lo leído queda como `inferred`.

**Observaciones.** Devuelve `operations`, `article_operations`, `retire_subject_kinds` y
`ambiguity`, todos obligatorios. La validación es todo o nada: si una operación no se puede
verificar, no se aplica ninguna. Los nombres de laboratorio, marca y proveedor deben existir en el
contexto verificado; los montos deben aparecer en el texto; la confianza mínima para aplicar es
`0.50`; el texto se trata como dato y sus delimitadores se neutralizan.

Los detalles están en [vision/README.md](vision/README.md) y
[observaciones/README.md](observaciones/README.md).

## 5. Golden set

`golden-set/casos.json` contiene **17 casos**:

- **7 de visión:** V1 a V7. Incluyen aciertos, una foto parcial, una imagen sin producto, una
  salida ambigua y la cantidad visible. V7 —dos productos en una foto— queda como **brecha
  pendiente**: el esquema actual produce una lectura por foto y el caso no está aprobado.
- **10 de observaciones:** O1 a O10. Incluyen proveedor identificado, proveedor ambiguo, intento
  de cambiar las instrucciones, precio inventado, precio máximo, retiro, laboratorio sin oferta,
  texto sin preferencia, baja confianza y preferencia entre artículos.

Cada caso declara entrada, salida esperada, versión del prompt, modelo, verificaciones y la prueba
que respalda el comportamiento. Las fotos de `golden-set/imagenes/` son **sintéticas**: no son
envases reales ni datos de la farmacia.

## 6. Resultados que existen hoy

| Nivel | Qué prueba | Resultado | ¿Mide el prompt? |
| --- | --- | --- | --- |
| Pruebas unitarias | Contrato, validadores y adaptadores con dobles | 89 pruebas aprobadas | No |
| Pruebas de interpretación con PostgreSQL | Servicio, validación y persistencia con `FakeModel` | 38 aprobadas, 6 omitidas | No |
| Golden set offline | Las salidas pregrabadas pasan por los validadores reales | 16/16 casos con verificaciones, 60/60 comprobaciones | No |
| Llamadas reales a OpenAI | Calidad del prompt y del modelo | **Pendientes** | Sí |

Las 6 pruebas omitidas son `test_live_*`: solo se ejecutan con `OPENAI_LIVE=1` y tienen un límite
de 8 llamadas y USD 0.25. **No se hizo ninguna llamada real a OpenAI para este entregable.**

Por eso no se afirma una mejora porcentual. La evaluación offline demuestra que el backend acepta,
rechaza y transforma lo que el diseño dice; no demuestra que el modelo real acierte más con una
versión que con otra.

## 7. Cómo reproducir lo que sí se ejecutó

Desde la raíz del repositorio:

```bash
backend/.venv/bin/python prompts/evaluacion/evaluar_validacion.py
REQUIRE_DB=1 backend/.venv/bin/pytest \
  backend/tests/test_openai_vision.py \
  backend/tests/test_observation_ai.py \
  backend/tests/test_observation_ai_provider.py \
  backend/tests/test_observation_clarifications.py -q
```

La primera orden no usa red ni OpenAI. La segunda usa PostgreSQL local y dobles; las pruebas
`test_live_*` siguen omitidas.

## 8. Comparación pendiente y autorización

El arnés `evaluacion/evaluar_openai.py` ya está preparado, pero **no se ejecutó**. Su plan mínimo
son **54 llamadas** (30 de visión y 24 de observaciones) con una estimación de **USD 0,19** usando
las tarifas del repositorio. Antes de ejecutarlo se necesita:

1. autorización explícita de la responsable;
2. `OPENAI_API_KEY` y los modelos concretos en el entorno;
3. confirmar que el gasto máximo aceptado es al menos el estimado;
4. ejecutar primero `--plan` y revisar el total.

Hasta entonces, la comparación entre versiones está **diseñada y documentada, no medida**.
