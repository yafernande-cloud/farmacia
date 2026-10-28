# CLAUDE.md — Farmacia Parque Lantaño

Asistente de compras con IA (demo → producción). Idioma de trabajo y UI: español de Chile.

## Fuentes de verdad
- `docs/Farmacia_Parque_Lantano_HLD_LLD.md`: requisitos, invariantes (§2), AC01–AC40. **No editar**; registrar
  cambios en `docs/decisions/`. Si el prompt del usuario contradice el HLD, rige el usuario y se registra.
- `docs/ARQUITECTURA.md`, `docs/ROADMAP.md`, `docs/decisions/0001-registro-de-decisiones.md`: estado vigente.

## Reglas permanentes
- **Git:** rama `master`. NO ejecutar commit, push, init, remote add ni renombrar ramas sin orden explícita.
  Nada de GitHub ni despliegues sin autorización.
- Consultar antes de: servicios externos, costos, credenciales, publicar. Nunca inventar secretos, proveedores,
  APIs, precios, stock ni enlaces. Mocks/fixtures solo en pruebas o etiquetados `fixture`.
- Invariantes críticos: un aviso repetido no suma cantidad; cantidad desconocida = NULL (no 0/1); sin fusión
  automática de dosis/formatos distintos; solo `SET/ADD/CLEAR` explícitos; aprobaciones ligadas a snapshot;
  referencia de cadena ≠ oferta comprable; compra real **bloqueada**; el LLM no decide dinero, cantidades ni permisos.
- **Productos (D-015 a D-024):** jamás asumir una unidad ni un rol (un número suelto NO es mg). Separar potencia,
  contenido, presentación y cantidad solicitada. Cada atributo lleva procedencia `explicit|inferred|confirmed`;
  solo una persona autorizada o un alias verificado confirma. Guardar siempre lo escrito (`original_text`) y normalizar
  solo para comparar. Registrar el aviso siempre; ante ambigüedad, candidatos+motivos para compras, nunca una pregunta
  obligada al auxiliar. Toda unidad/forma nueva = fila en `domain/units.py`/`domain/products.py` + prueba.
- **Canal/fotos (D-025 a D-036):** sin identidad vigente el mensaje queda pendiente y NUNCA se atribuye solo; el bot no aplica
  cantidades ni compra. Lo leído de una imagen es `inferred` con `origin=image`; la cantidad visible no es cantidad solicitada;
  laboratorio/fabricante/titular/comercializador son entidades distintas y desconocidas hasta que alguien las diga. La IA solo
  sugiere (`AIInterpreter`) y no hay proveedor de visión real: todo lo simulado debe decir `simulated`. No declarar que algo se
  probó con Telegram real o con visión real si solo se probó con simulador. El token de Telegram nunca en chat/logs/git.
- **Separación de datos (D-037 a D-047):** simulaciones y pruebas automatizadas SOLO en la organización `simulation` (chats `555000x`); el bot real
  en `real_test`. Nunca usar la organización real por defecto ni vaciar tablas en demos. `data_origin` (simulated/captured/ai_derived) NO es confianza.
  Una lectura simulada jamás entra en una organización real. El personal no se define en el código (archivo de datos → PostgreSQL), no se elimina, y solo
  personas activas + disponibles pueden ser identidad; PIN no se exige en el demo. **No aplicar migraciones 0004/0005/0006 a `farmacia` sin autorización explícita.**
- Dinero con `Decimal`/NUMERIC, nunca float. Fechas UTC; presentar en America/Santiago.
- No llamar al LLM para lo determinista. Rendimiento es requisito (dashboard <2 s, resultados progresivos).
- Frontend (Lovable) solo consume `/api/v1`; sin lógica de negocio ni acceso directo a la BD.

## Comandos (desde `backend/`)
- Arrancar: `scripts/dev.sh` · Pruebas: `.venv/bin/pytest` · Lint: `.venv/bin/ruff check .`
- Migraciones: `.venv/bin/alembic revision --autogenerate -m "..."` / `upgrade head` (requiere `DATABASE_URL`)

## Metodología
Por fases con informe (objetivo, alcance, archivos, implementación, pruebas, aceptación, riesgos, continuar).
Hecho: F0, **H1-A**, corrección del modelo de productos y **H1-B** con simuladores (ver `docs/H1B_TELEGRAM.md`). Siguiente: **H1-B.5** (verificar con el bot real; el usuario crea el bot y guarda el token en `.env`) y H1-C (SSE + dashboard Lovable).
No declarar pruebas de BD como exitosas si se omitieron: validar con `REQUIRE_DB=1 .venv/bin/pytest` (PostgreSQL local por Docker; `open -a Docker`).
Supabase: aún NO se crea proyecto (D-002). Auth web: solo diseñada (`docs/AUTH_SUPABASE.md`).
Patrón de servicios: `services/*` reciben `Actor` + sesión, verifican permisos, no hacen commit (la API usa `transaction()`); auditoría+outbox en la misma transacción (`services/events.emit`).
Cambios de esquema: migración Alembic + modelo; el test `test_modelos_y_migracion_estan_sincronizados` debe seguir pasando.
