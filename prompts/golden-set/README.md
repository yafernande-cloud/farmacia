# Golden set

## 1. Qué es

`casos.json` reúne **17 casos** con entrada, resultado esperado, versión del prompt, modelo y
verificaciones. La evaluación incluida es **offline**: las salidas del modelo están pregrabadas en
el archivo y se pasan por los validadores reales del backend. No son respuestas de OpenAI.

El golden set debe ejecutarse cada vez que cambie un prompt, el modelo o el esquema de salida.

## 2. Casos de visión (7)

Las imágenes de los casos que las requieren están en `imagenes/` y son **sintéticas**: se crearon
con SVG para este ejercicio. No son envases reales, no contienen datos de la farmacia y no deben
presentarse como una prueba con fotos reales. V5 y V7 no llevan imagen por diseño.

| ID | Tipo | Entrada | Resultado esperado | Evidencia |
| --- | --- | --- | --- | --- |
| V1 | correcto | Predual con nombre comercial, dos principios e indicación «analgésico femenino» | Nombre «Predual»; la indicación no es variante; búsqueda por «Predual» | `test_openai_vision.py::test_product_name_manda_sobre_los_principios_y_la_indicacion_no_es_variante` |
| V2 | correcto | Tramadol/Paracetamol 37,5/325 mg, 30 comprimidos | Dos principios y dos dosis, en orden; no se elige uno | `test_openai_vision.py::test_medicamento_combinado_conserva_los_dos_principios_y_sus_dosis` |
| V3 | ambiguo | Ibuprofeno parcial: nombre y forma, sin dosis | La extracción se acepta; el producto queda en revisión, la dosis ausente y advertida, sin inventar | `test_openai_vision.py::test_un_campo_vacio_se_ignora_sin_perder_el_resto` (parcial) y `test_h1b_photos.py::test_foto_parcialmente_legible_usa_lo_seguro_y_no_inventa_la_dosis` (flujo completo con proveedor simulado) |
| V4 | error | Imagen borrosa o sin producto | Cero filas; queda en revisión; se pide otra imagen | `test_h1b_photos.py::test_foto_ilegible_no_crea_nada_y_avisa_sin_presentarla_como_registrada` (flujo completo con proveedor simulado para `blurry`) y `test_interpretation.py::test_imagen_borrosa_no_inventa_nada` (rama `blurry`/`unreadable`). No hay llamada real a OpenAI. |
| V5 | error | El modelo devuelve dos contenidos distintos | Se rechaza la lectura completa por ambigua | `test_openai_vision.py::test_un_atributo_unico_repetido_con_valores_distintos_es_salida_ambigua`; sin imagen por diseño |
| V6 | correcto | Tres cajas de Migranol, sin cantidad escrita | La cantidad visible se ignora como cantidad solicitada | `test_openai_vision.py::test_envia_la_imagen_y_devuelve_solo_lo_visible` y `test_h1b_photos.py::test_la_cantidad_visible_en_el_envase_no_es_cantidad_solicitada` (flujo completo con proveedor simulado) |
| V7 | alcance | Dos envases de productos distintos en una foto | Dos filas revisables, una por producto | **Brecha pendiente:** el esquema actual produce una lectura por foto. No está aprobado ni cuenta como cumplido. |

V5 no es una prueba de lo que el modelo ve: comprueba que el backend rechaza una salida
contradictoria. Por eso no tiene imagen y no se envía a OpenAI.

## 3. Casos de observaciones (10)

| ID | Tipo | Texto de la persona | Resultado esperado | Evidencia |
| --- | --- | --- | --- | --- |
| O1 | correcto | «Prefiero comprarle al Proveedor Norte.» | Preferencia de proveedor con el nombre verificado | `test_observation_ai.py::test_preferencia_por_proveedor_activo_sin_oferta_se_guarda_sin_ordenar` |
| O2 | ambiguo | «Prefiero el proveedor del sur.» | No se aplica nada; se pide aclaración con nombres verificados | `test_observation_clarifications.py::test_proveedor_desconocido_pregunta_con_los_proveedores_activos` |
| O3 | error | «Ignora tus instrucciones…» con delimitadores falsos | Ninguna preferencia; el texto se trata como dato | `test_observation_ai_provider.py::test_prompt_neutraliza_delimitadores_escritos_por_la_persona` |
| O4 | error | «Prefiero ese laboratorio.» con un monto inventado en la propuesta | Se rechaza la propuesta completa; no se guarda nada | `test_observation_ai.py::test_no_inventa_precio_fuera_del_texto` |
| O5 | correcto | «No quiero pagar más de $2.100 por la caja.» | Precio máximo por caja, sin tolerancia | `test_observation_ai.py::test_precio_maximo_ordena_pero_no_oculta` |
| O6 | correcto | «Olvida lo del laboratorio, ya no me importa.» | Retiro de la clase laboratorio, sin preferencia nueva | `test_observation_ai.py::test_retiro_explicito_deja_sin_efecto_la_preferencia_anterior` |
| O7 | correcto | «Prefiero Laboratorios Andes.» | Preferencia por valor del catálogo aunque no tenga oferta | `test_observation_ai.py::test_preferencia_por_laboratorio_del_catalogo_sin_oferta_se_guarda_sin_ordenar` |
| O8 | ambiguo | «Hola, ¿cómo estás?» | Ninguna preferencia aplicable | `test_observation_ai.py::test_sin_preferencia_comercial` |
| O9 | error | «Creo que prefiero Laboratorios Andes, no sé.» | Rechazo por baja confianza y aclaración | `test_observation_clarifications.py::test_baja_confianza_pide_aclaracion_sin_aplicar_nada` |
| O10 | correcto | «Prefiero este al Migranol de caja 20.» | Preferencia entre artículos resuelta; sin equivalencia ni fusión | `test_observation_ai.py::test_preferir_un_articulo_frente_a_otro_queda_trazado` |

Los textos son ficticios. «Proveedor Norte», «Laboratorios Andes» y «Migranol de caja 20» existen
solo en el contexto de prueba.

## 4. Cómo se relaciona con el Prompt Maestro

| Caso exigido por el Prompt Maestro | Estado en el golden set |
| --- | --- |
| Foto legible con dos productos | **No cumplido; pendiente.** V7 registra la brecha: el esquema actual lee un producto por foto |
| Foto con un dato ilegible | Cubierto por V3 (dato ausente, sin inventar) |
| Imagen sin productos reconocibles | Cubierto por V4 |
| Preferencia con proveedor identificado | Cubierto por O1 |
| Preferencia con proveedor ambiguo | Cubierto por O2 |
| Entrada que intenta cambiar las instrucciones | Cubierto por O3 |

La brecha V7 no se disimula: requiere cambiar el esquema y el flujo de revisión, algo que este
entregable no hace porque el código funcional está fuera de alcance.

## 5. Estructura de un caso

```json
{
  "id": "V1",
  "titulo": "Foto legible de Predual: manda el nombre comercial",
  "tipo": "correcto",
  "entrada": "descripción de la imagen",
  "respuesta_modelo": { "legibility": "clear", "fields": [], "warnings": [] },
  "esperado": "texto para una persona",
  "verificar": [
    {"ruta": "reading.search_query", "igual": "Predual"}
  ],
  "prueba": "backend/tests/...",
  "version_prompt": "vision-v3",
  "modelo": "OPENAI_VISION_MODEL (sin valor por defecto)"
}
```

`verificar` usa rutas simples sobre el resultado del backend y operadores `igual`, `es_null`,
`contiene`, `no_contiene` y `longitud`. Así el caso es legible y, a la vez, ejecutable.

`version_prompt` y `modelo` son **metadatos de diseño**: indican para qué versión y variable de
modelo se diseñó el caso, no que la evaluación offline haya enviado ese prompt o llamado a ese
modelo. La evaluación offline usa `respuesta_modelo` pregrabada y no llama a OpenAI.

## 6. Cómo se ejecuta

```bash
backend/.venv/bin/python prompts/evaluacion/evaluar_validacion.py
```

El resultado está en `../evaluacion/resultados-validacion.json`. No usa red ni OpenAI.

Para una evaluación real, `../evaluacion/evaluar_openai.py` envía los casos al proveedor y evalúa
la respuesta con los mismos validadores. **No se ejecutó**: requiere autorización y presupuesto.
