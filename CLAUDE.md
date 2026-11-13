# Reglas de trabajo del Cotizador Inteligente

Este archivo orienta al agente de código. El [README](README.md) explica el proyecto y el
[Prompt Maestro](PROMPT_MAESTRO.md) define el alcance de la entrega. Para entender una decisión
anterior, consulta el [registro de decisiones](docs/decisions/0001-registro-de-decisiones.md).
Conserva la [especificación original](docs/Farmacia_Parque_Lantano_HLD_LLD.md) como referencia
y registra los cambios de alcance en `docs/decisions/`.

## Reglas del producto

- El flujo va desde el reporte de faltantes hasta la cotización finalizada. Al finalizar, se guarda
  el informe y se liberan los faltantes con cantidad conocida y cobertura suficiente. La compra
  queda fuera del sistema.
- Por Telegram se envía una foto por producto. Lo que se lee del envase queda sujeto a revisión.
  La cantidad visible del envase y la cantidad solicitada son datos distintos.
- Un aviso repetido no suma cantidad. Una cantidad desconocida se conserva como desconocida.
  Mantén separados los productos con dosis o presentaciones distintas.
- Las preferencias comerciales se aplican al producto específico. La IA interpreta observaciones
  y lee fotos. El backend valida las respuestas y decide el orden de las ofertas mediante reglas.
- Respeta los permisos y el aislamiento entre organizaciones. Conserva las reglas vigentes del
  [contrato](docs/contracts/cotizador-v0.2.md) en los flujos que cubre.

## Cómo trabajar

- Revisa el estado de los repositorios y conserva los cambios locales. El backend usa `master`
  y el frontend de Lovable usa `main`.
- Presenta el plan antes de cambiar código. Comprueba el resultado con los casos pertinentes y
  registra qué se probó, en qué entorno y con qué resultado.
- Usa bases de prueba aisladas y datos ficticios. Mantén las credenciales en variables de entorno.
  Solicita autorización antes de usar producción, servicios externos o pruebas con gasto.
- La responsable del proyecto hace el **Manual Deploy en Heroku**. Commit, push y publicación
  requieren autorización explícita.
- Conserva los prompts de sistema versionados y su [golden set](prompts/golden-set/README.md).
  Documenta por separado la validación local con respuestas preparadas y las corridas con OpenAI.

Consulta [Ejecución local](docs/EJECUCION_LOCAL.md) para instalar y probar, y la
[guía de despliegue](docs/DEPLOY_HEROKU.md) para identificar la versión publicada.
