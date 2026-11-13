# Observaciones comerciales: «Enseñar a la IA»

## 1. Tarea

La administradora escribe, en lenguaje natural, una preferencia sobre **un producto concreto**.
El modelo la convierte en operaciones comerciales estructuradas. El backend valida esas
operaciones y, si son aplicables, un motor determinista ordena las ofertas. El modelo no ordena,
no puntúa, no recomienda y no cambia precios, cantidades ni stock.

- **Código:** `backend/app/interpretation/commercial.py` (prompt y validación),
  `backend/app/interpretation/openai_observations.py` (adaptador),
  `backend/app/services/observation_ai.py` (servicio) y
  `backend/app/services/preferences.py` (orden determinista).
- **Prompt vigente:** `prompt-v3.md`, commit `641ec93` (2026-11-11).
- **Identificador:** `PROMPT_VERSION = "a6-observations-v3"`; es también el valor por defecto de
  `OPENAI_PROMPT_VERSION`.
- **Modelo:** `OPENAI_MODEL`, **sin valor por defecto**.
- **Activación:** `OBSERVATION_AI_ENABLED`; por defecto está desactivado.

## 2. Qué se envía

| Parte | Contenido |
| --- | --- |
| Sistema | Texto exacto de la versión del prompt |
| Usuario | Contexto verificado del artículo (nombre, principio activo, proveedores, laboratorios, marcas y monedas) + el texto de la persona entre delimitadores |
| Formato | Responses API, `json_schema` estricto, nombre `commercial_observation` |
| Almacenamiento | `store=false`: la observación no queda guardada en el proveedor |
| Reintentos | Uno solo, y solo para fallos transitorios; una salida truncada no se reintenta ni se parsea |

El texto de la persona se envuelve con delimitadores y cualquier `<<<OBSERVACION` o
`OBSERVACION>>>` escrito por ella se neutraliza. Así el contenido se trata como **dato**, no como
una instrucción para el modelo.

## 3. Salida esperada

```json
{
  "operations": [
    {
      "subject_kind": "supplier",
      "subject_label": "Proveedor Norte",
      "effect": "prefer",
      "tolerance": {"kind": "none", "percent": null, "amount": null, "currency": null},
      "price_reference": null,
      "confidence": 0.93
    }
  ],
  "article_operations": [],
  "retire_subject_kinds": [],
  "ambiguity": null
}
```

Los cuatro campos de primer nivel son obligatorios:

- `operations`: preferencias o bajas de prioridad por laboratorio, marca, proveedor,
  disponibilidad o precio.
- `article_operations`: preferencias, bajas o retiros entre **artículos distintos**. No declaran
  equivalencias ni fusiones.
- `retire_subject_kinds`: clases cuya preferencia anterior se deja sin efecto.
- `ambiguity`: `unclear_referent`, `conflicting`, `insufficient_context` o `not_commercial`, con
  una nota. Si hay ambigüedad, `operations` debe quedar vacío.

## 4. Validaciones del backend

La validación es **todo o nada**. Si una operación no se puede verificar, se rechaza la propuesta
completa y no se aplica ninguna preferencia.

| Regla | Efecto |
| --- | --- |
| Campos faltantes o adicionales | Se rechaza el esquema |
| Laboratorio, marca o proveedor que no existe en el contexto verificado | Se rechaza o se pide aclaración |
| Precio, tolerancia o cantidad que no aparece en el texto | Se rechaza (`validation_error`) |
| Precio con moneda no admitida | Se rechaza |
| Tolerancia en una preferencia negativa o en precio | Se rechaza |
| Confianza menor que `0.50` | Se rechaza con `low_confidence` y se pide aclaración |
| Operación sin artículo válido | Se rechaza |
| Preferir un artículo sin nombrar la alternativa | Se rechaza |
| Un artículo comparado consigo mismo | Se rechaza |
| Dos operaciones que compiten por la misma relación | Se rechaza |
| Nombre de artículo ambiguo o inexistente | Se pide aclaración, no se elige |
| Retiro de una clase no permitida | Se rechaza |

El resultado puede ser: preferencia validada, propuesta rechazada o aclaración solicitada. Los
códigos internos son `ambiguous`, `validation_error` y `low_confidence`.

## 5. Separación entre IA y orden

El modelo solo produce la estructura. El orden de las ofertas lo hace
`app/services/preferences.py` con reglas deterministas: precedencia por clase, tolerancias,
precios aceptables o máximos y desempate estable. Con los mismos datos y preferencias, el orden es
el mismo. Las preferencias son **del producto específico** y no se propagan a otros productos del
mismo laboratorio, marca o proveedor.

## 6. Límites conocidos

- El modelo puede proponer un valor que no existe; el backend lo rechaza. Eso es una protección,
  no una garantía de que el modelo acierte siempre.
- La salida estructurada reduce el formato libre, pero no elimina la necesidad de validar.
- La evaluación offline de `../evaluacion/` usa **salidas pregrabadas** en `casos.json` y los
  validadores reales del backend; no usa `FakeModel` ni llama a OpenAI. Las pruebas de
  `backend/tests` sí usan `FakeModel` para el servicio y la persistencia. Ninguna de las dos
  demuestra la calidad del modelo real: eso queda pendiente de la comparación con OpenAI.
- La versión v3 agrega `article_operations`: un modelo anterior no podía expresar esa relación.
  Comparar v3 con v1 o v2 mide una capacidad nueva, no solo una mejora de redacción.

## 7. Versiones

| Versión | Commit | Esquema | Qué cambió |
| --- | --- | --- | --- |
| v1 | `9783379` | Sin `article_operations` | Primera versión |
| v2 | `e1176d2` | Sin `article_operations` | Laboratorio y marca del catálogo aunque no tengan oferta; proveedor activo sin oferta |
| v3 | `641ec93` | Con `article_operations` | Preferencias entre artículos y prohibición de equivalencias o fusiones |

v1 y v2 comparten esquema y son comparables directamente. v3 se compara como ampliación. El texto
completo está en `prompt-v1.md`, `prompt-v2.md` y `prompt-v3.md`; el protocolo está en
`../comparacion/README.md`.
