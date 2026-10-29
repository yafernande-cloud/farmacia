# Autenticación web con Supabase Auth

Estado (2026-10-28): **validador JWT y enlace de identidad implementados y probados**; el **proyecto Supabase
todavía no se crea** (D-002) y el frontend no está construido. En desarrollo local sigue rigiendo
`AUTH_MODE=demo_header`, restringido a la organización de simulación para las operaciones administrativas.
**Ninguna cuenta está vinculada todavía**: Pao tiene fila en `real_test`, `simulation` y `legacy_mixed` (archivo), todas
con `auth_subject = NULL`; solo se enlazará la de `real_test` (D-067).

Decisiones relacionadas: D-064 (una identidad por persona), D-065 (enlace administrativo, nunca automático),
D-066 (sin recuperación ni registro público), D-067 (Pao en `real_test`), D-068 (firma asimétrica),
D-069 (`/staff` exige sesión fuerte), D-070 (limitación multi-organización).

## Flujo
1. El frontend (Lovable) inicia sesión con Supabase Auth: **Google** o **correo + contraseña**. No hay registro
   público ni recuperación de contraseña (D-066).
2. El navegador recibe un JWT de acceso y lo envía en `Authorization: Bearer <jwt>` a `/api/v1`.
3. FastAPI valida el JWT **sin llamar a Supabase en cada petición**: firma contra el JWKS del proyecto
   (clave asimétrica, caché de 300 s), y comprueba `iss`, `aud`, `exp`, `iat`, `sub` y `role=authenticated`.
4. El `sub` se busca en `app_user.auth_subject` (columna única, opcional) y de esa fila salen
   `organization_id` y `roles`. **Los permisos viven en nuestra base, no en claims editables** del token; un
   usuario válido en Supabase sin fila enlazada recibe **403 `USER_NOT_PROVISIONED`**.
5. Se construye el mismo `Actor` que produce `get_actor` → los servicios no cambian.

## Una sola identidad por persona (D-064)
- Google y correo/contraseña deben ser **dos identidades del mismo usuario de Supabase**, que tiene **un único
  `sub`**. Ese `sub` se enlaza a **una sola** fila `app_user`: un conjunto de permisos, sin cuentas internas
  duplicadas ni cambios de esquema.
- Si Supabase termina creando **dos usuarios** (dos `sub`), solo se enlaza uno y el otro queda en 403. El fallo es
  seguro; no concede permisos. **No se enlazan ambos.**
- **Verificación obligatoria antes de habilitar permisos:** iniciar sesión con Google y con correo/contraseña y
  comprobar que `GET /api/v1/me` devuelve el **mismo `id`**, los mismos `roles` y la misma organización. Solo
  entonces se considera lista la identidad.

## Qué está implementado
- `AUTH_MODE=supabase_jwt` con `get_actor` resolviendo el `sub` (`backend/app/api/deps.py`); variables
  `SUPABASE_JWKS_URL`, `SUPABASE_JWT_ISSUER` y `SUPABASE_JWT_AUDIENCE` (sin secretos: se usa el JWKS público).
  El arranque exige JWKS e issuer cuando el modo es JWT.
- Validación local contra JWKS, algoritmos RS256/ES256, `iss`, `aud`, `exp`, `iat`, `sub`, `role=authenticated`
  (`backend/app/core/supabase_auth.py`).
- `StrongActor` en las operaciones administrativas de Telegram (`/telegram-accounts`,
  `/telegram-access-requests`) **y del personal (`/staff`, D-069)**: con `AUTH_MODE=demo_header` solo se aceptan
  en la organización de simulación; en `real_test`/`production` responden 403 `STRONG_AUTH_REQUIRED`.
- Enlace de identidad (`backend/app/services/identity.py` + CLI), permiso `manage_identities` (rol `admin`),
  auditoría `auth_subject.linked`/`unlinked` y evento de outbox en la misma transacción.
- Pruebas: JWT firmados con claves de prueba (firma inválida, `iss`/`aud` erróneos, expirado, sin `app_user`,
  usuario inactivo), resolución de permisos desde PostgreSQL, rechazo de la cabecera demo en organización real y
  el flujo completo de enlace/desenlace con sus validaciones (`backend/tests/test_auth_identity.py`).

## Cómo se enlaza una identidad (solo CLI, D-065)
Desde `backend/`, con un administrador activo de la organización (`--by`) y la organización explícita:

```bash
# Estado del enlace (no incluye otras organizaciones; no imprime correos)
.venv/bin/python -m app.cli list-auth-subjects --org real --by <app_user_id_del_admin>

# Vincular. Sin --subject, el sub se escribe por consola y no queda en el historial del shell.
.venv/bin/python -m app.cli link-auth-subject <app_user_id_de_la_persona> --org real --by <admin_id>

# Desvincular (exige --confirm). No borra a la persona ni su historial; pierde el acceso web.
.venv/bin/python -m app.cli unlink-auth-subject <app_user_id> --org real --by <admin_id> --confirm
```

Reglas del enlace:
- Solo personas **activas** de la organización del administrador; las de otra organización responden `NOT_FOUND`
  (sin filtrar su existencia).
- Un `sub` ya usado por otra persona responde `AUTH_SUBJECT_IN_USE` sin decir de quién es.
- No se reemplaza un enlace existente: primero se desvincula (`AUTH_SUBJECT_ALREADY_SET`).
- Enlazar el mismo `sub` dos veces no duplica auditoría (`changed: false`).
- El `sub` es un **identificador**, no una credencial: la CLI no imprime tokens ni contraseñas.

## Qué falta para activarla
1. Crear el proyecto Supabase (**no autorizado todavía**, D-002), con firmas asimétricas (D-068) y registro
   cerrado (D-066).
2. Alta manual del usuario de Pao (correo + contraseña, correo confirmado) y vinculación de Google dentro de
   Supabase con el mismo correo.
3. Verificar el mismo `sub` (D-064) y enlazarlo a la fila de `real_test` con `link-auth-subject` (D-067).
4. Configurar `SUPABASE_JWKS_URL`, `SUPABASE_JWT_ISSUER` y `AUTH_MODE=supabase_jwt` en el entorno desplegado;
   añadir el origen del frontend a `CORS_ORIGINS`.
5. Frontend Lovable: ingreso con Google y correo/contraseña, sin registro ni recuperación; enviar el bearer y
   tratar 401 (volver a ingresar) y 403 `USER_NOT_PROVISIONED` (cuenta no habilitada, sin ofrecer alta).

## Fuera de alcance / riesgos
- **Sin recuperación de contraseña** (D-066): si se pierden ambos accesos, el restablecimiento es operativo desde
  el panel de Supabase, no una función del producto.
- **Multi-organización (D-070):** `auth_subject` es único en toda la base. Hoy solo se enlaza `real_test`; al
  habilitar `production` habrá que decidir cómo una persona accede a más de una organización.
- Aprobar o comprar exigirá autenticación fuerte reciente (re-autenticación/MFA) en H5.
- Si Supabase deja de usarse, basta otro emisor OIDC: solo cambia el validador de JWT.
- Telegram **no** usa Supabase Auth: se identifica por cuenta autorizada + sesión de canal (H1-B).
