# Base de datos

PostgreSQL es la única fuente de verdad transaccional. El esquema vive en las migraciones
Alembic de `backend/migrations/` (no hay SQL suelto aquí).

Local: `docker compose --env-file ../.env up -d` y luego, desde `backend/`, `alembic upgrade head`.
Demo/producción: PostgreSQL administrado (ver `docs/decisions/0001-registro-de-decisiones.md, D-002`).
