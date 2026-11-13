# Evaluación

Este directorio separa dos cosas que no deben confundirse:

1. **Evaluación offline:** comprueba el contrato y las reglas del backend con salidas pregrabadas.
   No llama a OpenAI y no mide la calidad del prompt.
2. **Evaluación real:** envía los casos a OpenAI, guarda la respuesta y la evalúa con los mismos
   validadores. Está preparada pero **no se ejecutó** porque consume cuota.

## 1. Evaluación offline

**Archivo:** `evaluar_validacion.py`
**Resultado:** `resultados-validacion.json`

```bash
backend/.venv/bin/python prompts/evaluacion/evaluar_validacion.py
```

Qué hace:

- lee `../golden-set/casos.json`;
- para visión, aplica `_extraction` y `read_extraction` del backend a la salida pregrabada;
- para observaciones, aplica `ObservationProposal`, `build_prompt` y `validate_proposal`;
- comprueba las rutas declaradas en `verificar`;
- escribe el informe y devuelve código de salida distinto de cero si alguna comprobación falla.

Resultado actual (2026-11-13):

| Métrica | Valor |
| --- | --- |
| Casos | 17 |
| Casos con verificaciones | 16 |
| Casos que cumplen todo | 16/16 |
| Comprobaciones | 60/60 |
| Fuera de alcance | V7 (dos productos en una foto, sin ejecutar) |

V7 se conserva como referencia. La regla es una foto por producto y V7 queda fuera del
denominador de 16 casos evaluables.

### Qué demuestra y qué no

**Sí demuestra:** que el backend acepta, rechaza y transforma exactamente lo que el diseño dice;
que los cinco casos del Prompt Maestro que ya están implementados tienen una comprobación
reproducible; que una salida ambigua no se completa por suposición.

**No demuestra:** que el modelo real produzca esas salidas; que una versión del prompt sea mejor
que otra; ni una mejora porcentual. Las salidas están escritas de antemano. Presentar este
resultado como «calidad del prompt» sería incorrecto.

## 2. Evaluación real

**Archivo:** `evaluar_openai.py` (ejecutado el 2026-11-13; ver
[resultados](../comparacion/resultados-2026-11-13.md))

El arnés:

- lee el texto exacto del **prompt de sistema** de cada versión desde los `.md` de `prompts/`;
- envía la misma imagen y el mismo contexto a cada versión;
- usa la Responses API con esquema estricto, `store=false` y sin SDK;
- guarda salida cruda, tokens, latencia, modelo, hash del prompt y hash de la imagen;
- evalúa con los mismos validadores del backend;
- escribe `resultados-openai.json` sin tocar `resultados-validacion.json`.

El mensaje de usuario se construye con el `build_prompt` actual del backend y no está versionado.
Por eso la comparación aísla el prompt de sistema y no reproduce toda la petición histórica.

### Matriz ejecutada

El evaluador ejecuta la [matriz acordada](../comparacion/README.md#6-plan-de-llamadas-y-costo):
54 llamadas, dos repeticiones por cruce y las tres versiones en ambas tareas (Visión V1, V2, V3, V4
y V6; Observaciones O1, O2, O7 y O10). Usa el esquema propio de cada versión, comparte entradas
dentro de cada tarea y aplica un control de presupuesto: reserva el peor costo de cada llamada, se
detiene antes de superar el tope y guarda el informe tras cada llamada. Las pruebas locales del
evaluador están en `test_evaluar_openai.py` y no llaman a OpenAI.

La corrida real se hizo el 13 de noviembre de 2026: 54/54 llamadas, USD 0,1173 de un tope de
USD 0,25. `resultados-openai.json` guarda las salidas crudas y el detalle por llamada;
`resultados-validacion.json` (offline) no se modificó.

### Cómo se leería el resultado

Para cada versión se informa: casos que cumplen todas las verificaciones, casos con error de
proveedor, salidas no JSON y casos rechazados por el backend. La comparación solo es válida si:

- se usaron los mismos casos, imágenes, modelo y número de repeticiones;
- se guardaron las salidas crudas;
- no se editó ningún prompt después de ver los resultados;
- se informa el resultado completo, no solo los casos favorables.

Con 3 repeticiones por caso se puede describir estabilidad, pero no se debe afirmar significancia
estadística. La muestra es pequeña y el objetivo es académico: mostrar una diferencia medida y
reproducible, no generalizar a producción.

## 3. Pruebas existentes con dobles

Estas pruebas no llaman a OpenAI y ya se ejecutaron:

| Suite | Resultado | Qué cubre |
| --- | --- | --- |
| `test_openai_vision.py`, `test_observation_ai_provider.py`, `test_observation_clarifications.py` | 89 aprobadas | Contrato, adaptadores y validaciones con `httpx.MockTransport` |
| `test_observation_ai.py` con `REQUIRE_DB=1` | 38 aprobadas, 6 omitidas | Servicio y persistencia con `FakeModel` |

Las 6 omitidas son `test_live_*` y solo corren con `OPENAI_LIVE=1`. Tienen un límite propio de 8
llamadas y USD 0.25. No se ejecutaron.

## 4. Regla de honestidad

En este directorio:

- «aprobado» significa que una comprobación declarada se cumplió;
- «omitido» significa que no se ejecutó;
- «pendiente» significa que requiere una llamada real;
- El resultado archivado etiqueta V7 como «brecha». En el alcance vigente, V7 queda fuera de la evaluación.

No se usa «mejoró» sin dos mediciones comparables. Hoy no existen.
