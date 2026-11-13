# Resultados de la comparación real de prompts (2026-11-13)

Corrida real con OpenAI sobre los prompts v1, v2 y v3, con los criterios fijados de antemano en
[criterios-2026-11-13.md](criterios-2026-11-13.md). Se ejecutaron **54 de 54 llamadas**, sin errores
del proveedor, con un costo de **USD 0,1173** sobre un presupuesto autorizado de USD 0,25.

- Modelo solicitado y servido: `gpt-5.4-mini` (`gpt-5.4-mini-2026-03-17`).
- Tarifas aplicadas: USD 0,75 / millón de entrada y USD 4,50 / millón de salida.
- Tokens declarados por el proveedor: 103.858 de entrada y 8.747 de salida.
- Latencia media: 1,97 s por llamada.
- Resultados completos, con la salida cruda de cada llamada:
  [resultados-openai.json](../evaluacion/resultados-openai.json). Plan previo:
  [plan-2026-11-13.json](../evaluacion/plan-2026-11-13.json).

Un acierto exige **todas** las verificaciones del golden set en esa repetición; cada caso y versión
tiene 2 repeticiones. No se eligió la mejor repetición ni se repitió ninguna llamada.

## 1. Aciertos por caso y versión

### Visión (imagen y mensaje de usuario idénticos en las tres versiones)

| Caso | v1 | v2 | v3 | Lectura |
| --- | --- | --- | --- | --- |
| V1 (Predual) | 0/2 | 0/2 | 0/2 | Empate: las tres fallan lo mismo |
| V2 (Tramadol/Paracetamol) | 0/2 | **2/2** | 1/2 | Mejora v1→v2; retroceso v2→v3 |
| V3 (envase parcial) | 0/2 | 0/2 | 0/2 | Empate: las tres fallan lo mismo |
| V4 (imagen ilegible) | 2/2 | 2/2 | 2/2 | Empate |
| V6 | 2/2 | 2/2 | 2/2 | Empate |

### Observaciones Comerciales (contexto verificado y mensaje idénticos; esquema propio de cada versión)

| Caso | v1 | v2 | v3 | Lectura |
| --- | --- | --- | --- | --- |
| O1 | 2/2 | 2/2 | 2/2 | Empate |
| O2 | 2/2 | 1/2 | 2/2 | Retroceso v1→v2; mejora v2→v3 |
| O7 | 2/2 | 2/2 | 2/2 | Empate |

### O10, informado aparte (capacidad nueva de v3)

| Versión | Aciertos | Motivo |
| --- | --- | --- |
| v1 | 0/2 | No tiene `article_operations`; el backend la rechaza |
| v2 | 0/2 | Igual que v1 |
| **v3** | **2/2** | Interpreta la preferencia relativa entre dos artículos |

O10 **no** entra en las comparaciones de mejora/retroceso: v1 y v2 no podían acertarla.

### Total de los ocho casos comparables (16 repeticiones por versión)

| Versión | Aciertos | % |
| --- | --- | --- |
| v1 | 10/16 | 63 % |
| v2 | 11/16 | 69 % |
| v3 | 11/16 | 69 % |

Si se suma O10 como caso normal, v3 sube a 13/18 frente a 10/18 de v1 y 11/18 de v2; esa suma se
muestra solo como referencia, porque mide una capacidad que v1 y v2 no tenían.

## 2. Dónde hubo mejora, empate o retroceso

- **Mejora clara:** V2 de v1 a v2. v1 devuelve el principio activo y la dosis como un solo texto
  («Tramadol clorhidrato / Paracetamol», «37,5 mg / 325 mg»); v2 los separa en dos entradas y acierta
  las dos repeticiones.
- **Mejora clara:** O2 de v2 a v3. v2 aceptó en una repetición una preferencia de proveedor sin
  código verificable; v3 rechazó y pidió aclaración en las dos.
- **Retroceso descriptivo:** V2 de v2 a v3 (2/2 → 1/2). En una repetición v3 volvió a unir la dosis
  en un texto. Es **una** repetición de diferencia; no se afirma que v3 sea peor en general.
- **Retroceso descriptivo:** O2 de v1 a v2 (2/2 → 1/2), por la repetición ya descrita. v1 y v3
  empatan en ese caso.
- **Empates:** V1, V3, V4, V6, O1 y O7. En la mayoría de los empates las tres versiones aciertan;
  en V1 y V3 fallan las tres por el mismo motivo, descrito abajo.
- **Capacidad nueva:** O10 solo es posible en v3, y v3 la logra 2/2.

## 3. Por qué fallan V1 y V3 en las tres versiones

No es una diferencia entre prompts: las tres versiones producen el mismo tipo de respuesta y fallan
las mismas verificaciones.

- **V1:** el modelo devuelve `PREDUAL` y el golden set espera exactamente `Predual` (dos
  verificaciones, en el nombre y en la consulta de búsqueda); además no marca
  `variant_not_commercial`. La primera parte es una comparación sensible a mayúsculas del validador,
  no un error de lectura. La segunda sí es una omisión del modelo, igual en las tres versiones.
- **V3:** el modelo califica la imagen como `clear` cuando el golden set espera `partial`, escribe
  varias advertencias cuando se espera exactamente una y usa `strength_not_visible` donde el golden
  set pide `active_ingredient_not_visible`. Son desacuerdos entre lo que el modelo considera
  correcto y lo que fija el caso, presentes en las tres versiones.

## 4. Criterio ampliado de V4

Las seis lecturas de V4 quedaron `unreadable`, en revisión (`needs_review`) y sin campos, que es
exactamente la conducta que el criterio ampliado premiaba. Además cumplen la verificación estricta.

## 5. Costo

| Concepto | Valor |
| --- | --- |
| Llamadas ejecutadas | 54 de 54 |
| Costo real estimado | **USD 0,1173** |
| Presupuesto autorizado | USD 0,25 (47 % usado) |
| Estimación previa | USD 0,1647 |
| Reserva teórica máxima | USD 0,3309 (no se alcanzó) |
| Errores del proveedor | 0 |

El evaluador comprobó el plan antes de llamar, reservó el peor costo razonable de cada llamada y
sumó el costo real con los tokens declarados; no necesitó detenerse. El costo es una estimación con
las tarifas del repositorio, no la factura de OpenAI.

## 6. Limitaciones

- **Muestra pequeña:** 9 casos y 2 repeticiones. Las diferencias de una repetición son descriptivas,
  no significativas.
- **Imágenes sintéticas:** no representan fotos reales de la farmacia.
- **El validador es el vigente:** v1 y v2 de Observaciones respetan su esquema, pero las juzga el
  backend actual. La comparación de Visión usa el mismo mensaje de usuario generado por el código
  de hoy, así que aísla el prompt de sistema, no todo el sistema de cada época.
- **Rigidez del golden set:** la igualdad exacta sensible a mayúsculas (V1) y las expectativas de
  `legibility`/`warnings` (V3) hacen que las tres versiones fallen aunque la lectura sea razonable.
  Cambiar esos criterios después de ver los resultados habría invalidado la comparación; se dejan
  como están y se documentan.
- **O10 no es comparable entre versiones:** mide una capacidad que solo v3 tiene.
- **Un solo modelo:** los resultados valen para `gpt-5.4-mini`, no para otros modelos.
- **Sin reintentos:** una llamada fallida se habría contado como fallo; no hubo ninguna.

## 7. Conclusión en una frase

Con este modelo, esta muestra y estos criterios, v2 y v3 empatan en aciertos (11/16) y superan
levemente a v1 (10/16); la única capacidad nueva de v3, O10, funciona las dos veces, pero v3 no
mejora de forma consistente a v2 en los casos que ambos podían resolver.
