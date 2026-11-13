# Prompt Maestro del Cotizador Inteligente

*Versión consolidada de los requisitos del proyecto.*

## 1. Problema que queremos resolver

En la farmacia de mi pareja, veo que cuando falta un producto, el equipo tiene que anotarlo, buscarlo en las listas de distintos proveedores y comparar las ofertas. Hoy esa información queda repartida entre mensajes y planillas. Cuando hay que retomar una cotización, cuesta saber qué productos siguen faltando, qué proveedor ofreció cada uno y cuáles ya se incluyeron en una cotización anterior.

Por eso, la idea es crear el Cotizador Inteligente, una aplicación que reúna los reportes de faltantes, las ofertas de los proveedores y las cotizaciones en un solo lugar. La primera versión se usará en la farmacia de mi pareja. Si más adelante se incorporan sucursales, cada una podrá llevar sus propios faltantes y tener sus propios usuarios.

## 2. Aplicación y rol de Claude Code

Claude Code, actúa como agente de desarrollo del proyecto. Tu tarea es convertir estos requisitos en un plan, construir la aplicación desde cero, probarla y documentar cómo ejecutarla. Antes de escribir código, revisa los archivos iniciales que existan, identifica las decisiones que requieren confirmación y presenta un plan por etapas. En cada entrega, indica qué se puede probar y muestra el resultado de las verificaciones.

Construye una aplicación web para la administradora, un bot de Telegram para las auxiliares y una API que aplique las reglas del negocio. Guarda los registros y su historial en una base de datos. La administradora revisa los faltantes, busca productos, compara ofertas, arma el carrito y genera la cotización. Las auxiliares reportan desde el teléfono de la farmacia o desde un teléfono personal autorizado.

El flujo comienza con el reporte de un faltante y termina al finalizar la cotización y liberar los faltantes que quedaron cubiertos. Cualquier ampliación del alcance debe ser aprobada por la responsable del proyecto.

## 3. Requisitos funcionales

### 3.1 Reportes por Telegram

- **Texto.** Usa un parser determinista para separar las líneas, normalizar espacios y leer los campos que estén expresados en un formato admitido. Conserva el texto de origen para poder revisar cómo se interpretó. Si una línea admite más de una lectura, pide aclaración sobre esa línea e indica cuáles ya se guardaron.
- **Foto.** Usa OpenAI Vision para transcribir los productos y las cantidades visibles. Pide una salida estructurada por línea y valida su formato en el servidor. Presenta la extracción para revisión antes de registrar las filas. Si la foto es ilegible o falta un dato necesario, pide corregirlo o enviar otra imagen. Conserva los valores dudosos como tales durante la revisión.
- **Registro.** Valida la autorización de la cuenta de Telegram antes de aceptar el reporte. Identifica quién lo envió, por qué canal y cuándo. Confirma qué se registró y qué falta aclarar. Procesa cada actualización de forma idempotente para que un reintento de Telegram produzca un solo registro. Un reporte nuevo de la misma persona se rige por la regla de faltantes repetidos.
- **Cantidades.** Usa como fuente la regla de negocio aprobada por la responsable del proyecto. Antes de programarla, deja por escrito qué hacer con una cantidad omitida o ambigua, qué unidad representa cada número, cómo se tratan reportes repetidos y cómo se mide la cobertura de un faltante. Separa la cantidad solicitada, el contenido del envase y las unidades comerciales seleccionadas. Si falta una regla, pide confirmación para ese caso y avanza con el resto del flujo. Cada regla aprobada debe quedar acompañada de un ejemplo verificable.

### 3.2 Panel web

- Muestra los faltantes activos con el producto reportado, la cantidad cuando esté definida, la fecha y el origen. Permite filtrar y buscar. Mantén un historial consultable de los faltantes liberados y de la cotización que los cubrió.
- Permite buscar productos y consultar las ofertas disponibles desde un faltante o directamente desde el buscador. Muestra nombre, presentación, proveedor, precio y cantidad disponible cuando la fuente la informe. Conserva las diferencias de presentación al comparar.
- Muestra mensajes claros cuando una búsqueda no tenga coincidencias, una oferta deje de estar disponible o se necesite aclarar el producto.

### 3.3 Proveedores y ofertas

- Permite registrar proveedores e importar sus listas de ofertas. Define y documenta el formato admitido, la correspondencia de columnas y los campos requeridos. Registra el proveedor y la fecha de cada importación.
- Valida el archivo antes de aplicar los cambios. Identifica la fila y el campo de cada error. Define una clave estable para las ofertas de cada proveedor y actualízalas sin duplicarlas al reimportar la misma lista. Si falla la aplicación de cambios, conserva el catálogo anterior.
- Guarda el precio, la moneda, la presentación y la disponibilidad con el nivel de detalle entregado por cada proveedor. Conserva los datos de origen que permitan revisar una diferencia. Representa los valores desconocidos de forma explícita.

### 3.4 Preferencias comerciales y comparación

- Usa OpenAI para traducir una instrucción comercial escrita por la administradora a una estructura de preferencias admitidas. Por ejemplo, «prioriza al proveedor A» se traduce en una prioridad de proveedor si se identifica una coincidencia inequívoca.
- Valida esa estructura en la API y permite revisar la interpretación. Pide aclaración cuando una preferencia sea ambigua o exceda las opciones admitidas.
- Aplica las preferencias y ordena las ofertas con código determinista. Documenta la prioridad entre criterios y el desempate. Con los mismos datos y preferencias, devuelve el mismo orden. La administradora decide qué oferta agregar al carrito.

### 3.5 Carrito y cotización

- Guarda el carrito en la base de datos. Permite agregar, cambiar o quitar ofertas, ajustar cantidades según las reglas aprobadas y consultar los subtotales y el total. Mantén la relación entre las líneas del carrito y los faltantes que buscan cubrir.
- Antes de finalizar, vuelve a validar las ofertas y sus cantidades. Si cambió el precio o hay datos que impiden finalizar, informa qué debe revisar la administradora.
- Al generar la cotización, guarda una copia de los datos usados en el informe, incluidos el proveedor, la presentación, la cantidad, el precio y los totales. Permite consultar y descargar el informe finalizado. Los cambios posteriores de las listas de proveedores no deben modificarlo.
- Finaliza la cotización y libera los faltantes cubiertos en una sola transacción. Aplica la regla de cobertura aprobada y deja activos los faltantes sin cobertura suficiente. Si la operación falla, conserva el carrito y los faltantes para que pueda repetirse. Si se reintenta una finalización exitosa, devuelve la misma cotización.
- El alcance termina en el informe y la liberación de faltantes. La compra se realiza fuera del sistema. Excluye pagos, pedidos enviados a proveedores, confirmaciones de compra y recepción de mercadería.

### 3.6 Acceso

- Da a la administradora acceso al panel, los proveedores, el carrito y las cotizaciones. Permite a las auxiliares autorizadas reportar por Telegram.
- Usa el identificador de la cuenta de Telegram para autorizar los reportes. Verifica permisos en la API y limita cada operación a los registros de la farmacia correspondiente. La responsable del proyecto aprueba quién puede acceder.

## 4. Stack y organización técnica

Usa PostgreSQL para los datos, la API de Telegram para el bot y la API de OpenAI para imágenes y preferencias comerciales. Separa el panel web, la API, el procesamiento de Telegram y el acceso a datos. Mantén en la API las validaciones de negocio compartidas por los distintos canales.

Si los archivos iniciales traen un stack web aprobado, respétalo. Si debes elegir el framework del panel y el de la API, presenta una propuesta a la responsable antes de incorporarlos. Incluye migraciones reproducibles, datos de prueba sin información real e instrucciones para ejecutar la aplicación en local. Configura por variables de entorno la base de datos y los servicios externos. Prepara instrucciones para Heroku. El despliegue se hace exclusivamente mediante **Manual Deploy**, ejecutado por la usuaria responsable del proyecto.

## 5. IA generativa y trabajo de desarrollo

Claude Code se usa para desarrollar el software. Dentro de la aplicación, OpenAI se usa para dos tareas concretas, extraer información de fotos e interpretar preferencias comerciales. El parser de texto, las validaciones, el orden de las ofertas y la finalización de cotizaciones se implementan con código.

Guarda cada prompt de la aplicación como archivo versionado. Identifica su tarea, versión y esquema de salida. Registra la versión del prompt y el modelo utilizado para poder comparar resultados sin guardar datos sensibles en los logs. Trata el contenido de las fotos y las preferencias escritas como datos de entrada. Valida los resultados antes de usarlos y maneja respuestas incompletas, errores y tiempos de espera con mensajes que permitan reintentar o corregir.

Mantén un **golden set** con entradas de prueba y salidas esperadas. Incluye al menos estos seis casos y ejecútalos cada vez que cambie un prompt, el modelo o el esquema correspondiente.

| Caso | Resultado esperado |
| --- | --- |
| Foto legible de un producto | Una fila con los datos visibles, revisable antes de guardar (V1) |
| Foto con un dato ilegible | La fila queda en revisión sin completar el dato por suposición |
| Imagen sin productos reconocibles | Se solicita otra imagen y se guardan cero faltantes |
| Preferencia con proveedor identificado | Se obtiene una prioridad válida con su identificador |
| Preferencia con proveedor ambiguo | Se pide aclaración sin aplicar un orden nuevo |
| Entrada que intenta cambiar las instrucciones del modelo | La aplicación mantiene el esquema y las operaciones permitidas |

Prueba el parser de texto por separado con ejemplos de los formatos admitidos y de las reglas de cantidad aprobadas. Conserva los resultados esperados junto a los ejemplos para poder detectar cambios de comportamiento.

## 6. Reglas de trabajo y seguridad

- Mantén las claves de Telegram, OpenAI y PostgreSQL fuera del código y de los logs. Usa datos ficticios o anonimizados en pruebas y demostraciones. Evita enviar a servicios externos información que la operación no necesita.
- Valida las entradas en el servidor. Rechaza archivos o solicitudes que excedan los límites definidos para la aplicación y protege las rutas de administración. Evita guardar fotos originales si basta con el registro revisado para la finalidad aprobada.
- Usa transacciones y restricciones de base de datos en operaciones que puedan repetirse o ejecutarse al mismo tiempo. Mantén el historial de importaciones, cotizaciones y liberaciones.
- Solicita autorización a la responsable del proyecto antes de cambiar reglas de negocio, habilitar servicios externos en producción o ejecutar acciones destructivas. El despliegue manual queda a cargo de la usuaria. Mantén los cambios de alcance y las decisiones aprobadas por escrito.

## 7. Fases de desarrollo

1. Confirma las reglas de cantidad y cobertura, los formatos de proveedores y el stack web. Define los datos, los permisos y los casos de prueba.
2. Construye PostgreSQL, las migraciones y la API base. Implementa proveedores, importación y búsqueda de ofertas.
3. Conecta Telegram. Prueba primero el reporte por texto y después la extracción y revisión de fotos. Verifica el acceso con cuentas autorizadas.
4. Construye el panel, la comparación de ofertas, las preferencias comerciales y el carrito persistente.
5. Implementa la finalización, el informe y la liberación de faltantes. Prueba fallos y reintentos.
6. Ejecuta las pruebas integradas y el golden set. Documenta la instalación, la operación y el procedimiento de Manual Deploy para que la usuaria pueda ejecutarlo.

## 8. Criterios de aceptación

| Función | Comprobación |
| --- | --- |
| Telegram por texto | Un mensaje válido de dos líneas produce dos faltantes. Reenviar la misma actualización desde Telegram conserva esos dos registros. Una cuenta sin permiso produce cero registros. |
| Telegram por foto | Se envía una foto por producto. Una foto legible muestra una fila para revisión. Se registra solo al confirmarla. Si la imagen es ilegible, se solicita otra foto. |
| Cantidades | Cada regla aprobada tiene una prueba con entrada y resultado esperado. Los casos incluyen las decisiones sobre cantidad omitida, presentación, reportes repetidos y cobertura. |
| Proveedores | Importar dos veces el mismo archivo conserva la cantidad de ofertas. Un archivo inválido indica la fila y el campo, y conserva el catálogo anterior. |
| Comparación | Repetir una búsqueda con las mismas ofertas y preferencias devuelve el mismo orden, incluso si hay empate. |
| Carrito | Agregar una oferta, cambiar su cantidad y recargar el panel conserva la selección y el total calculado por la API. |
| Cotización | Con un faltante cubierto y otro sin cobertura suficiente, finalizar genera un informe descargable y libera solo el primero. Un cambio posterior en la lista del proveedor deja intacto el informe. |
| Fallos y reintentos | Un fallo inducido durante la finalización deja cero cotizaciones nuevas y cero faltantes liberados. Repetir una finalización exitosa conserva un único informe. |
| Acceso y alcance | Una solicitud sin sesión válida o sin permiso no puede operar sobre datos protegidos. Finalizar una cotización no genera cobros ni envía pedidos a proveedores. |
| IA y entrega | Los seis casos del golden set cumplen el resultado esperado. Verifica además texto y foto desde Telegram real con una cuenta de prueba autorizada y documenta la evidencia. Entrega instrucciones de ejecución local y Manual Deploy en Heroku. |
