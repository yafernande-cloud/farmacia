# Comparación entre versiones de prompts

## 1. Estado

El Prompt Maestro plantea dos tareas para la IA dentro del Cotizador: leer los datos visibles de
un envase e interpretar preferencias comerciales. En Visión, las versiones incorporan reglas
para medicamentos combinados y afinan la identificación por nombre comercial. En Observaciones
Comerciales, amplían primero el contexto de catálogo y después las preferencias entre artículos.

Los commits de las tablas identifican las versiones del código. El protocolo se ejecutó con OpenAI
el **13 de noviembre de 2026**: 54 de 54 llamadas, costo estimado USD 0,1173. Los criterios se
fijaron antes de llamar en [criterios-2026-11-13.md](criterios-2026-11-13.md) y los resultados
están en [resultados-2026-11-13.md](resultados-2026-11-13.md). Se conserva la
[evaluación offline](../evaluacion/README.md) como evidencia separada.

## 2. Mapa de versiones

### Visión

| Versión | Commit | Esquema | Cambio verificable |
| --- | --- | --- | --- |
| v1 | `03a75d8` | Igual | Primera versión |
| v2 | `9921311` | Igual | Agrega regla para medicamentos combinados; prohíbe elegir entre dos valores de un atributo único |
| v3 | `d7b963a` | Igual | `product_name` manda sobre principio activo y laboratorio; el laboratorio no es el nombre; indicaciones y frases promocionales no son `variant` |

Las tres versiones usan el mismo esquema de salida y el mismo formato de petición. **Son
directamente comparables** con las mismas imágenes, el mismo modelo y los mismos casos.

### Observaciones comerciales

| Versión | Commit | Esquema | Cambio verificable |
| --- | --- | --- | --- |
| v1 | `9783379` | Sin `article_operations` | Primera versión |
| v2 | `e1176d2` | Sin `article_operations` | Laboratorio y marca del catálogo conocido aunque no tengan oferta; proveedor activo sin oferta |
| v3 | `641ec93` | Con `article_operations` | Preferencias entre artículos; prohíbe equivalencias y fusiones |

v1 y v2 comparten esquema: **son comparables directamente**. v3 agrega un campo obligatorio y una
capacidad nueva. Comparar v3 con v2 en los casos comunes sigue siendo posible si cada versión usa
su propio esquema; el caso O10 solo puede cumplirlo v3, así que se informa como **capacidad
nueva**, no como mejora de redacción.

Commits posteriores a la versión vigente no cambiaron el texto:

- visión: no hay cambios después de `d7b963a`;
- observaciones: `c0109f1` y `5b35a6a` cambiaron validación o flujo, no el prompt.

## 3. Qué casos discriminan cada cambio

| Comparación | Casos que discriminan | Por qué |
| --- | --- | --- |
| Visión v1 vs v2 | V2 | v1 no indica cómo tratar dos principios y dos dosis |
| Visión v2 vs v3 | V1 | v3 prioriza el nombre comercial y excluye la indicación de `variant` |
| Visión v1 vs v3 | V1, V2 | Acumula los dos cambios anteriores |
| Visión, control | V3, V4, V6 | Comprueban que una mejora no rompió lo parcial, lo ilegible ni la cantidad visible |
| Observaciones v1 vs v2 | O7 | v1 no permite un laboratorio del catálogo sin oferta; v2 sí |
| Observaciones, control | O2 | Comprueba que ambas versiones siguen pidiendo aclaración ante ambigüedad |
| Observaciones v2 vs v3 | O10 | v2 no puede expresar una preferencia entre artículos |
| Observaciones, control | O1 | Comprueba que v3 no rompió una preferencia simple |

Los casos de control son importantes: una versión que «mejora» un caso y empeora otro no es una
mejora demostrada.

## 4. Protocolo de comparación controlada

Para que la diferencia se pueda atribuir al prompt y no a otra cosa:

1. **Mismo modelo.** Usar el mismo `OPENAI_MODEL` y `OPENAI_VISION_MODEL` en todas las versiones.
   Si cambia el modelo, se informa como variable no controlada.
2. **Mismos casos.** Las mismas imágenes, el mismo texto, el mismo contexto verificado y el mismo
   número de repeticiones. No se agregan casos después de ver los resultados.
3. **Mismo esquema por versión.** v1 y v2 de observaciones no reciben `article_operations`; v3 sí.
   Si se enviara el esquema de v3 a v2, la comparación mediría otra cosa.
4. **Sin ediciones posteriores.** El prompt de sistema se lee de los `.md` versionados. No se
   ajusta después de ver una respuesta. El mensaje de usuario se genera con el `build_prompt`
   actual del backend y no está versionado: la comparación aísla el prompt de sistema, no
   reproduce toda la petición histórica.
5. **Repeticiones.** Tres por caso y versión. Se informa el resultado de cada repetición, no solo
   el mejor.
6. **Salida cruda.** Se guarda el JSON completo del modelo, los tokens, la latencia y los hashes
   del prompt y de la imagen.
7. **Evaluación ciega al origen.** Las mismas verificaciones de `casos.json` se aplican a todas
   las versiones.
8. **Reporte completo.** Se publican aciertos, fallos, errores de proveedor y salidas no JSON.
   No se eliminan casos incómodos.

### Métrica principal

**Cumplimiento estricto por caso:** el caso cuenta como aprobado solo si todas sus verificaciones
se cumplen. Se informa además el detalle por verificación. Con 3 repeticiones se describe la
estabilidad, pero no se afirma significancia estadística.

## 5. Hipótesis, no resultados

Antes de medir, se puede formular qué debería pasar. Son **hipótesis**, no hallazgos:

- v3 debería reducir los errores de nombre comercial en V1 frente a v1 y v2;
- v2 debería aceptar O7, que v1 rechazaría;
- v3 debería poder cumplir O10, que v1 y v2 no pueden expresar;
- ninguna versión debería romper V4 (imagen ilegible) ni O2 (proveedor ambiguo).

Si los datos reales contradicen una hipótesis, se informa la contradicción. No se cambia el
protocolo para salvarla.

## 6. Plan de llamadas y costo

La matriz propuesta aplica los mismos casos a las tres versiones de cada tarea.

| Tarea | Casos | Versiones | Repeticiones | Llamadas |
| --- | --- | --- | --- | --- |
| Visión | V1, V2, V3, V4, V6 | v1, v2, v3 | 2 | 30 |
| Observaciones comerciales | O1, O2, O7, O10 | v1, v2, v3 | 2 | 24 |
| **Total** | **9 casos** | | | **54** |

O10 se informa aparte como capacidad incorporada en v3. V5 prueba el rechazo de una salida
contradictoria en el backend. V7 queda fuera de alcance. Se usa una foto por producto. O4 y O9 pueden servir como diagnósticos y quedan fuera de esta matriz.

El plan se estimó en USD 0,1647 con los supuestos y tarifas del repositorio, dentro del tope de
**USD 0,25**. El evaluador incorpora ahora un control de gasto: reserva el peor costo razonable de
cada llamada, imputa el costo real con los tokens declarados y se detiene antes de superar el tope,
guardando el informe tras cada llamada. En la corrida real el gasto fue **USD 0,1173**.

## 7. Antes de ejecutar la comparación

1. ~~Ajustar el evaluador~~: hecho; cubre la matriz, usa el esquema de cada versión y comparte
   entradas dentro de cada tarea.
2. ~~Definir los criterios sustantivos antes de la corrida~~: hecho en
   [criterios-2026-11-13.md](criterios-2026-11-13.md); se conservan las comprobaciones offline.
   Para V4 se aceptaron `blurry` o `unreadable` si la imagen queda en revisión y no genera filas.
3. ~~Confirmar modelos, tarifas y presupuesto y revisar el plan sin hacer llamadas~~: hecho;
   `plan-2026-11-13.json` guarda el plan revisado.
4. ~~Registrar resultados por caso, versión y repetición, con sus límites~~: hecho en
   [resultados-2026-11-13.md](resultados-2026-11-13.md).

## 8. Limitaciones del diseño

- Las imágenes son sintéticas; una diferencia con ellas no garantiza el mismo resultado con fotos
  reales.
- La muestra es pequeña y el objetivo es académico.
- El modelo puede variar entre llamadas; por eso se repite y se reporta la dispersión.
- La comparación de observaciones v2/v3 mezcla mejora de redacción con capacidad nueva. Se
  separan los casos comunes del caso O10 para no atribuir a «mejor prompt» lo que es «más
  esquema».
- Solo el prompt de sistema está versionado. El mensaje de usuario —contexto verificado y texto
  de la persona— lo genera el código actual; una versión histórica no se reconstruye completa.
- La comparación de Visión sigue la regla de una foto por producto. V7 queda fuera de la corrida.
