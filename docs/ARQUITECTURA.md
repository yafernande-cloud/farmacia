# Arquitectura inicial (vigente)

Complementa el HLD/LLD original (`Farmacia_Parque_Lantano_HLD_LLD.md`, no se modifica). Donde difieren,
rige este documento y las decisiones en `docs/decisions/`.

## Stack

| Capa | Elección | Motivo |
|---|---|---|
| Backend | Python 3.12+ (probado en 3.14) + FastAPI, SQLAlchemy 2 async, psycopg 3, Alembic, Pydantic v2 | Decisión del prompt maestro; OpenAPI gratis; ecosistema IA/OCR |
| Base | PostgreSQL (único almacén transaccional) | Unicidad parcial, bloqueos y outbox transaccional exigidos por HLD §6.4, §7.4, §15 |
| Frontend | Lovable (React) consumiendo la API HTTPS | Velocidad de diseño; sin segundo frontend |
| Tiempo real | SSE desde el backend con cursor (HLD D11) | Más simple que WebSocket; no depende de Supabase Realtime |
| Trabajos | Tabla `job`/`outbox_event` en PostgreSQL + worker propio (fase H1) | Sin Redis ni colas pagas al inicio |
| IA | Adaptador a Claude con salida estructurada (fase H1/H2) | Solo para interpretar texto/foto; nunca para operaciones deterministas |

Monolito modular: `backend/app/` se divide por módulo del HLD §4.1 (ingreso, demanda, catálogo, fuentes,
comparador, planificador, carritos, reglas, ejecución, auditoría) a medida que cada fase los necesite.
No se crean paquetes vacíos por adelantado.

## Cómo se conecta Lovable

```
Lovable (navegador) --HTTPS/JSON + SSE--> FastAPI --SQL--> PostgreSQL
Telegram -----------webhook HTTPS--------> FastAPI
```

- El frontend **solo** habla con `/api/v1`. No accede a tablas de la base directamente (ni vía cliente Supabase).
  Así toda regla de negocio, permiso y auditoría vive en un solo lugar y Lovable es reemplazable.
- Contrato: el OpenAPI de FastAPI (`/openapi.json`) es la fuente; se exportará a `docs/contracts/` y se
  pegará en los prompts de Lovable. Los prompts específicos por pantalla irán en `frontend/lovable-prompts/`.
- CORS por `CORS_ORIGINS` (dominio de Lovable + localhost).
- Autenticación web: **pendiente de decisión** (ver D-005). Hasta entonces la API no expone datos de negocio.
- `frontend/` se crea cuando se sincronice el repo de Lovable (no se crea vacío ahora).

## PostgreSQL y entornos

- `APP_ENV`: `local | demo | staging | production`. Cada entorno tiene su propia base y sus propios secretos.
- Local: Docker (`database/docker-compose.yml`) — requiere Docker Desktop en marcha.
- Demo: PostgreSQL administrado. Recomendación: **Supabase solo como Postgres gestionado** (plan gratuito
  alcanza para demo; conexión vía pooler). El esquema lo gobierna Alembic, no el panel de Supabase.
  Implica un servicio externo: se confirma contigo antes de crearlo (D-002).
- Migraciones: `backend/migrations/` (Alembic, URL desde `DATABASE_URL`). Migrar de demo a producción =
  apuntar `DATABASE_URL` a otra instancia; no hay datos ni lógica fuera de Postgres.
- Los fixtures de demo viven separados y etiquetados (`fixture`), nunca mezclados con datos reales (HLD §27.2).

## Evitar dependencia de Lovable

1. Contrato OpenAPI propio, probado sin frontend.
2. Cero lógica de negocio ni acceso a datos en el cliente.
3. Autenticación basada en estándares (JWT/OIDC), no en SDK propietario.
4. Componentes de Lovable tratados como código desechable: si se cambia, solo se rehace la capa de vista.

## Evolución a producción

Mismo código, distinta configuración: base administrada con backups/PITR, secretos en gestor, despliegue del
backend en un PaaS con contenedor (Dockerfile se agrega al desplegar, con tu autorización), observabilidad
(HLD §18). La ejecución real de compras permanece **bloqueada por diseño**: `purchase_execution == "blocked"` no
depende de una variable de entorno (HLD §22).
