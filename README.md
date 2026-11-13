# Cotizador Inteligente de Farmacia

**[Abrir el Cotizador Inteligente](https://pixel-perfect-replay-13689.lovable.app/)**

En la farmacia de mi pareja, cuando falta un producto, el equipo lo anota y después tiene que
buscarlo en varias listas de precios. El Cotizador reúne los faltantes, las ofertas y las
cotizaciones en un solo lugar para que la administradora pueda preparar su compra.

La IA ayuda en dos momentos. La auxiliar puede enviar una foto para que el sistema lea los datos
visibles del envase y los deje listos para revisar. La administradora puede escribir una preferencia
de compra con sus propias palabras y el sistema la interpreta para ordenar las ofertas del producto.

## Cómo se usa

1. La auxiliar reporta un faltante por Telegram. Si envía fotos, manda una por producto.
2. La administradora revisa los faltantes y busca el producto en el catálogo, los proveedores
   y las cadenas disponibles.
3. Compara las ofertas y agrega las que quiere cotizar. Con «Enseñar a la IA» puede registrar
   preferencias comerciales para ese producto.
4. Al finalizar, el sistema guarda el informe y libera los faltantes con cantidad conocida y
   cobertura suficiente. El historial permite volver a consultar el informe. La compra se
   realiza fuera del sistema.

El panel publicado se abre en Lovable con una cuenta autorizada. Para levantar la API en tu
equipo, sigue la [guía de ejecución local](docs/EJECUCION_LOCAL.md). Incluye los pasos para
construir la imagen con el [Dockerfile](backend/Dockerfile) y comprobar que responde.

## Cómo lo construí

El [Prompt Maestro](PROMPT_MAESTRO.md) reúne las instrucciones que guiaron el desarrollo, desde el
problema de la farmacia hasta las pruebas esperadas. [CLAUDE.md](CLAUDE.md) contiene las reglas de
trabajo para el agente de código.

Trabajé con Claude Code a partir del Prompt Maestro. La forma de trabajo fue por etapas: pedir un
plan, revisar y aprobar el alcance, implementar y probar. En la revisión final, usé DeepSeek para
investigar los problemas que encontré al probar el Cotizador, corregirlos y ejecutar las pruebas
locales.

El panel de Lovable se comunica con una API en Heroku que guarda los datos en PostgreSQL.
Telegram es el canal para reportar faltantes. OpenAI se usa en dos tareas de la aplicación: leer
los datos visibles de un envase e interpretar observaciones comerciales. El backend valida esas
respuestas y calcula el orden de las ofertas. [Ver arquitectura](docs/ARQUITECTURA.md).

## Prompts y pruebas

- [Prompts de la aplicación](prompts/README.md) explica qué cambió en las versiones de Visión y
  Observaciones Comerciales.
- [Golden set](prompts/golden-set/README.md) reúne situaciones del flujo de trabajo, como una foto
  parcial o una preferencia que necesita aclaración. Para probarlas se usan imágenes sintéticas
  y textos ficticios.
- [Evaluación](prompts/evaluacion/README.md) documenta **16 de 16 casos evaluables y 60 de 60
  comprobaciones** con respuestas preparadas.
- En la [comparación con OpenAI](prompts/comparacion/resultados-2026-11-13.md) hicimos 54 llamadas.
  En los ocho casos comparables, v1 acertó **10/16** y v2 y v3 acertaron **11/16** cada una.
  En la prueba de medicamentos combinados, v2 pasó de 0/2 a 2/2 frente a v1. La muestra es pequeña
  y v3 empató con v2 en el total.

## Dónde encontrar cada cosa

| Si quieres ver… | Abre… |
| --- | --- |
| Funciones del sistema | [Guía del Cotizador](docs/COTIZADOR.md) |
| Instalación, Docker y comandos de prueba | [Ejecución local](docs/EJECUCION_LOCAL.md) |
| Cómo se publica y qué versión se desplegó | [Guía de Heroku y Lovable](docs/DEPLOY_HEROKU.md) |
| Resultados de pruebas | [Registro de pruebas](docs/PRUEBAS.md) |
| Código del backend y documentación | [Repositorio del backend](https://github.com/yafernande-cloud/farmacia) |
| Código de la interfaz | [Repositorio del frontend](https://github.com/yafernande-cloud/screenshot-exact-match) |
| Planes y decisiones anteriores | [Roadmap histórico](docs/ROADMAP.md) y [registro de decisiones](docs/decisions/0001-registro-de-decisiones.md) |
