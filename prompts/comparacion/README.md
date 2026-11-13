# Comparación entre versiones de prompts

## 1. Estado

Existen versiones históricas reales y comparables de los **prompts de sistema**, recuperadas de
Git. **Todavía no existe una comparación medida con OpenAI real.** Este documento describe qué se
puede comparar, con qué casos, cuántas llamadas hacen falta y cuánto costaría. No contiene
porcentajes de mejora porque no se han obtenido.

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

Matriz mínima preparada en `evaluacion/evaluar_openai.py`:

| Comparación | Casos | Versiones | Repeticiones | Llamadas |
| --- | --- | --- | --- | --- |
| Visión v1 vs v3 | V1, V2, V3, V4, V6 | 2 | 3 | 30 |
| Observaciones v1 vs v2 | O2, O7 | 2 | 3 | 12 |
| Observaciones v2 vs v3 | O1, O10 | 2 | 3 | 12 |
| **Total** | | | | **54** |

Estimación: **USD 0,19** con las tarifas del repositorio y tokens supuestos. Es una estimación
gruesa; el modelo real puede cambiar el costo. El arnés muestra el plan antes de llamar y se
niega a superar `--max-llamadas`.

Si se quisiera una comparación más amplia —por ejemplo, los 10 casos de observaciones en las tres
versiones— el número sube. No se propone hacerlo sin una autorización de gasto separada.

## 7. Qué falta para demostrar una mejora medible

1. Autorización de la responsable para gastar cuota de OpenAI.
2. Elegir y confirmar los modelos reales (`OPENAI_MODEL`, `OPENAI_VISION_MODEL`).
3. Ejecutar `evaluar_openai.py --plan` y revisar las 54 llamadas.
4. Ejecutar con `--confirmar-gasto --max-llamadas 54`.
5. Guardar `resultados-openai.json` y comparar versión por versión.
6. Redactar el informe con aciertos, fallos y límites. Solo entonces se podrá decir si hubo
   mejora y de qué tamaño; hoy no se puede.

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
- El golden set no cubre dos productos por foto (V7): esa brecha queda fuera de la comparación
  hasta que cambie el esquema.
