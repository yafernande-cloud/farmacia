# Visión: lectura de fotos de envases

## 1. Tarea

Una auxiliar envía una foto por Telegram. El adaptador de visión devuelve **solo lo que se ve**:
atributos del producto, legibilidad y advertencias. No decide si el producto existe, no registra
faltantes y no completa datos por suposición. La traducción a producto y la revisión posterior son
deterministas.

- **Código:** `backend/app/interpretation/openai_vision.py` (adaptador),
  `backend/app/interpretation/vision.py` (contrato y traducción).
- **Prompt vigente:** `prompt-v3.md`, commit `d7b963a` (2026-11-09).
- **Modelo:** `OPENAI_VISION_MODEL`, **sin valor por defecto**. Si falta, la visión no está
  disponible; no se elige un modelo en silencio.
- **Proveedor:** se habilita con `VISION_PROVIDER=openai`; `none` y `simulated` no llaman a OpenAI.

## 2. Qué se envía

| Parte | Contenido |
| --- | --- |
| Sistema | Texto exacto de la versión del prompt |
| Usuario | «Lee la fotografía y extrae los datos visibles del producto.» y, si existe, el pie de foto como contexto |
| Imagen | `data:` URI en base64, `detail: auto` |
| Formato | Responses API, `json_schema` estricto, nombre `vision_extraction` |
| Almacenamiento | `store=false`: la foto no queda guardada en el proveedor |
| Reintentos | Uno solo, y solo para timeout, `429`, `5xx` o proveedor no disponible |

El pie de foto se marca como contexto y no como texto leído. La clave y la imagen nunca se
registran en logs; los errores se guardan con códigos internos saneados.

## 3. Salida esperada

```json
{
  "legibility": "clear",
  "fields": [
    {"attribute": "product_name", "text": "Predual", "confidence": 0.95},
    {"attribute": "ingredient", "text": "Paracetamol", "confidence": 0.9},
    {"attribute": "strength", "text": "500 mg", "confidence": 0.9}
  ],
  "warnings": []
}
```

- `legibility`: `clear`, `partial`, `blurry` o `unreadable`.
- `fields`: hasta 40 campos. `attribute` debe ser uno de los atributos admitidos; `text` se recorta
  a 200 caracteres; `confidence` debe ser un número entre 0 y 1.
- `warnings`: notas breves de lo que no se pudo leer.

Atributos admitidos: `ingredient`, `product_name`, `variant`, `strength`, `concentration`,
`content`, `form`, `packaging`, `release`, los campos comerciales y `visible_quantity`.

## 4. Validaciones del backend

| Regla | Efecto |
| --- | --- |
| `legibility` fuera del enum | Se rechaza la respuesta completa |
| `fields` o `warnings` con tipo incorrecto | Se rechaza la respuesta completa |
| Más de 40 campos | Se rechaza la respuesta completa |
| Atributo desconocido | Se rechaza la respuesta completa |
| `text` vacío o demasiado largo | Se recorta; un texto vacío no aporta el dato |
| `confidence` fuera de `0..1` | Se rechaza la respuesta completa |
| Atributo único repetido con valores distintos | Se rechaza con `duplicate_attribute:<atributo>` |
| `ingredient`, `strength` o `concentration` repetidos | Se permiten: un combinado tiene varios componentes |

La validación de esquema es deliberadamente estricta: ante una contradicción no se elige un valor
al azar. El resultado es una lectura en revisión, no un producto inventado.

## 5. Traducción a producto

- `blurry` o `unreadable` → `NEEDS_REVIEW`, sin producto. Se pide otra imagen.
- `partial` → el producto puede identificarse, pero la lectura queda en `NEEDS_REVIEW`
  (`image_partially_legible`) para que la persona complete o confirme lo que falta.
- `product_name` es el ancla de la búsqueda; nunca se sustituye por el principio activo ni por el
  laboratorio. Si solo se ve el nombre comercial, se usa como ancla sin afirmar el principio activo.
- Una indicación terapéutica o frase promocional no es `variant`; si el modelo la propone, el
  backend la deja como incertidumbre (`variant_not_commercial`).
- `visible_quantity` se ignora como cantidad solicitada
  (`visible_quantity_not_a_requested_quantity`): la cantidad para comprar solo la dice la persona.
- Los campos con confianza menor que `0.50` pasan a incertidumbres y no se usan.
- Todo lo leído queda como `inferred` con origen `image`: un modelo nunca confirma un atributo.
- Si no hay principio activo visible, se registra `active_ingredient_not_visible` y se conserva el
  nombre comercial como ancla; no se adivina el principio por el aspecto.

## 6. Límites conocidos

- **Un producto por foto.** Para reportar varios productos, se envía una foto por cada uno.
  V7 registra el escenario de varios productos en una foto como fuera de alcance y sin ejecutar.
- **No hay versión del prompt en la traza.** La visión se identifica por commit. Las
  observaciones comerciales sí guardan `a6-observations-v3`.
- **Las imágenes del golden set son sintéticas.** Sirven para comparar versiones, no para afirmar
  cómo se comporta el modelo con fotos reales de la farmacia.
- **No se ha llamado al proveedor real para este entregable.** Las pruebas usan
  `httpx.MockTransport`; el modelo real sigue pendiente de autorización.

## 7. Versiones

| Versión | Commit | Qué cambió |
| --- | --- | --- |
| v1 | `03a75d8` | Primera versión |
| v2 | `9921311` | Combinados con varios principios y dosis; prohibición de elegir entre valores únicos |
| v3 | `d7b963a` | Prioridad del nombre comercial; el laboratorio no es el nombre; indicaciones fuera de `variant` |

El texto completo e inmutable de cada versión está en `prompt-v1.md`, `prompt-v2.md` y
`prompt-v3.md`. La comparación con las mismas imágenes está descrita en
`../comparacion/README.md`.
