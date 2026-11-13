# Criterios de la comparación real de prompts (fijados antes de llamar)

Fecha: 2026-11-13. Estos criterios se fijaron **antes** de ejecutar la corrida con OpenAI y no se
cambian después de ver los resultados. El protocolo general está en
[README.md](README.md).

## 1. Alcance autorizado

| Tarea | Casos | Versiones | Repeticiones | Llamadas |
| --- | --- | --- | --- | --- |
| Visión | V1, V2, V3, V4, V6 | v1, v2, v3 | 2 | 30 |
| Observaciones Comerciales | O1, O2, O7, O10 | v1, v2, v3 | 2 | 24 |
| **Total** | **9 casos** | | | **54** |

Dentro de cada tarea, las tres versiones usan **el mismo modelo** y **las mismas entradas**:
misma imagen y mismo mensaje de usuario en Visión; mismo contexto verificado y mismo texto de la
persona en Observaciones. Solo cambia el prompt de sistema y, en Observaciones, el esquema propio
de cada versión (v1 y v2 no reciben `article_operations`).

- **Modelo:** `gpt-5.4-mini` para ambas tareas (el modelo que el repositorio documenta para la
  prueba real, con razonamiento de esfuerzo bajo). No hay un modelo distinto sugerido para visión;
  si `gpt-5.4-mini` no aceptara imágenes, las llamadas fallarían con error del proveedor y se
  informaría así, sin sustituir el modelo a mitad de la corrida.
- **Tarifas:** USD 0,75 por millón de tokens de entrada y USD 4,50 por millón de salida.
- **Presupuesto de la evaluación:** USD 0,25. El límite mensual del proyecto (USD 5,00, con corte
  de gasto activo en el panel) se mantiene.
- **Control de gasto:** el evaluador verifica el plan antes de llamar y, durante la corrida,
  reserva el peor costo razonable de cada llamada, suma el costo real con los tokens declarados y
  se detiene antes de superar el tope. Guarda el informe después de cada llamada.
- **Repeticiones:** dos por caso y versión. No se reintenta una llamada fallida.

## 2. Qué cuenta como acierto

**Métrica principal (estricta):** una repetición acierta solo si se cumplen **todas** las
verificaciones que `golden-set/casos.json` declara para ese caso, aplicadas con los validadores
reales del backend. Un caso y versión se informa como «aciertos / 2 repeticiones». No se elige la
mejor repetición ni se descarta una salida incómoda.

| Caso | Verificaciones estrictas |
| --- | --- |
| V1 | 5 |
| V2 | 5 |
| V3 | 5 |
| V4 | 5 |
| V6 | 3 |
| O1 | 4 |
| O2 | 4 |
| O7 | 3 |
| O10 | 4 |

**Criterio ampliado de V4 (informativo, no reemplaza la métrica):** además de la verificación
estricta, se registra si la lectura quedó como `blurry` o `unreadable`, sin campos y en revisión.
El protocolo ya preveía esa tolerancia para una imagen sin producto; se informa aparte para no
cambiar `casos.json` ni la métrica principal.

## 3. Cómo se compara

- Se compara el número de repeticiones acertadas por caso y por par de versiones
  (v1→v2, v2→v3, v1→v3): **mejora** si la versión nueva acierta más, **retroceso** si acierta
  menos y **empate** si acierta lo mismo.
- Con dos repeticiones la diferencia es descriptiva; **no** se afirma significancia estadística.
- Una versión que mejora un caso y empeora otro no se presenta como mejora general.
- **O10 se informa aparte.** Depende de `article_operations`, que solo existe en v3. Los fallos de
  v1 y v2 en O10 no se cuentan como retroceso: la capacidad no estaba disponible.

## 4. Condiciones de detención

La corrida se detiene y guarda lo hecho si:

1. la siguiente llamada no cabe en el presupuesto restante;
2. hay tres errores seguidos del proveedor (clave, modelo o red).

Los casos no ejecutados se informan como no evaluados; no se rellenan ni se estiman.

## 5. Salidas

- Resultados completos, con salida cruda, tokens, latencia, hashes y costo por llamada:
  `prompts/evaluacion/resultados-openai.json`.
- Resumen en palabras simples, con aciertos por caso y versión, costo y limitaciones:
  `prompts/comparacion/resultados-2026-11-13.md`.

## 6. Limitaciones declaradas de antemano

- Las imágenes del golden set son sintéticas; no representan fotos reales de la farmacia.
- La muestra es pequeña (9 casos, 2 repeticiones) y el objetivo es académico.
- El mensaje de usuario lo genera el código actual, no una versión histórica: la comparación
  aísla el prompt de sistema.
- v1 y v2 de Observaciones se comparan con el validador actual; el esquema propio de cada versión
  se respeta, pero el backend que juzga es el vigente.
- El costo es una estimación a partir de los tokens declarados por el proveedor y las tarifas del
  repositorio; no reemplaza la facturación de OpenAI.
