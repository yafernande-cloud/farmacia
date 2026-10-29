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
  personas activas + disponibles pueden ser identidad; PIN no se exige en el demo. **Estado real de `farmacia`: 0004, 0005, 0007 y 0008 aplicadas y script de datos 0006 ejecutado (`alembic current` = 0008, 32 tablas). La 0008 (catálogo comercial y ofertas, 10 tablas) se aplicó el 2026-10-29 con respaldo `pg_dump` verificado (restaurado y comparado fila a fila), ensayo previo de subida y de reversión, y verificación posterior por catálogo: 219 registros intactos y ningún objeto previo alterado (D-098). No aplicar migraciones nuevas a `farmacia` sin autorización explícita.**
- Dinero con `Decimal`/NUMERIC, nunca float. Fechas UTC; presentar en America/Santiago.
- No llamar al LLM para lo determinista. Rendimiento es requisito (dashboard <2 s, resultados progresivos).
- Frontend (Lovable) solo consume `/api/v1`; sin lógica de negocio ni acceso directo a la BD.

## Comandos (desde `backend/`)
- Arrancar: `scripts/dev.sh` · Pruebas: `.venv/bin/pytest` · Lint: `.venv/bin/ruff check .`
- Migraciones: `.venv/bin/alembic revision --autogenerate -m "..."` / `upgrade head` (requiere `DATABASE_URL`)

## Metodología
Por fases con informe (objetivo, alcance, archivos, implementación, pruebas, aceptación, riesgos, continuar).
Hecho: F0, **H1-A**, corrección del modelo de productos, **H1-B** con simuladores y **H1-B.4** (solicitud/aprobación/bloqueo de accesos de Telegram; ver `docs/H1B_TELEGRAM.md`); migraciones `0007` y `0008` aplicadas; preparación de Supabase Auth (D-064 a D-072: enlace único por `sub`, CLI `link-auth-subject`, `/staff` con `StrongActor`, bootstrap `create-admin`). **C1 cerrado (2026-10-29):** la migración `0008` está aplicada a `farmacia` y el esquema del cotizador existe; el siguiente paso es **C2** (`catalog-load` + conector `manual_import`, contrato de Excel D-077 aún abierto), sin endpoints nuevos todavía. También siguen abiertas: **H1-B.5** (verificar con el bot real; el usuario crea el bot y guarda el token en `.env`) y H1-C (SSE + dashboard Lovable).
No declarar pruebas de BD como exitosas si se omitieron: validar con `REQUIRE_DB=1 .venv/bin/pytest` (PostgreSQL local por Docker; `open -a Docker`).
Supabase: proyecto `faltantes-farmacia` creado por el usuario (2026-10-29) con Google OAuth y JWKS asimétrico verificado; `AUTH_MODE=demo_header` se mantiene. Auth web: validador JWT implementado; enlace `auth_subject` por CLI (solo rol `admin`, auditado) y bootstrap `create-admin` (solo `admin`, sin enlace, solo `real_test`/`simulation`, una activa por organización; D-071/D-072); `/staff` y accesos de Telegram exigen `StrongActor`. **La administradora de pruebas «Yarella Fernández» existe en `real_test` (`83c03aa5-6496-4bef-8f96-b92cbbd59f6e`, solo `admin`, separada de Pao) y su identidad está enlazada**; A2/A3 verificaron que Google y correo+contraseña dan el mismo `sub` y que `/api/v1/me` responde 200 (`docs/AUTH_SUPABASE.md`, D-064 a D-072). Pao sigue sin `auth_subject` en `real_test`, `simulation` y `legacy_mixed`; se enlazará con su propia identidad más adelante (D-067).
Patrón de servicios: `services/*` reciben `Actor` + sesión, verifican permisos, no hacen commit (la API usa `transaction()`); auditoría+outbox en la misma transacción (`services/events.emit`).
Cambios de esquema: migración Alembic + modelo; el test `test_modelos_y_migracion_estan_sincronizados` debe seguir pasando.
