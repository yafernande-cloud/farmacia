# Autenticación web con Supabase Auth

Estado (2026-10-29): **validador JWT, enlace de identidad y comando de bootstrap implementados y probados**. El
proyecto Supabase `faltantes-farmacia` existe (creado por el usuario, con Google OAuth en modo de prueba) y su JWKS
público se verificó compatible (clave ES256/P-256). El frontend no está construido. En desarrollo local sigue
rigiendo `AUTH_MODE=demo_header`, restringido a la organización de simulación para las operaciones administrativas.
**Una identidad está enlazada**: la administradora de pruebas «Yarella Fernández» (identidad nueva, no Pao; D-071)
se creó con `create-admin` en `real_test` (2026-10-29, `app_user` `83c03aa5-…-b59f6e`, solo rol `admin`) y su `sub`
se enlazó con `link-auth-subject`. Verificado en la prueba aislada (A2/A3, fuera del repositorio): Google y
correo + contraseña entregan **el mismo `sub`** (D-064) y `GET /api/v1/me` responde **200** con rol `admin` y
organización `real_test`; sin token y con token inválido, **401**. **Pao sigue sin enlazar**: tiene fila en
`real_test`, `simulation` y `legacy_mixed` (archivo), todas con `auth_subject = NULL`; solo se enlazará la de
`real_test` y con su propia identidad (D-067).

Decisiones relacionadas: D-064 (una identidad por persona), D-065 (enlace administrativo, nunca automático),
D-066 (sin recuperación ni registro público), D-067 (Pao en `real_test`), D-068 (firma asimétrica),
D-069 (`/staff` exige sesión fuerte), D-070 (limitación multi-organización), D-071 (identidad de prueba separada
de Pao), D-072 (bootstrap de la primera administradora).

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
- Bootstrap de la primera administradora (`backend/app/services/staff.py::bootstrap_admin` + CLI `create-admin`,
  D-072): solo rol `admin`, sin enlace, prohibido en `production`, una activa por organización y auditado como
  acción del operador.
- Pruebas: JWT firmados con claves de prueba (firma inválida, `iss`/`aud` erróneos, expirado, sin `app_user`,
  usuario inactivo), resolución de permisos desde PostgreSQL, rechazo de la cabecera demo en organización real y
  el flujo completo de enlace/desenlace con sus validaciones (`backend/tests/test_auth_identity.py`) y las
  restricciones del bootstrap (`backend/tests/test_bootstrap_admin.py`: solo `admin`, sin enlace, auditoría con
  `actor_id = NULL`, negativa en producción, una sola bootstrap activa y recuperación si se desactiva).

## Bootstrap de la primera administradora (`create-admin`, D-071/D-072)
Problema que resuelve: para enlazar una identidad hace falta un administrador (`--by`), pero al principio **nadie**
puede iniciar sesión. El comando crea la primera fila administrativa sin sesión; **no** enlaza nada.

```bash
# Desde backend/, con la organización explícita y confirmación. No imprime tokens.
.venv/bin/python -m app.cli create-admin "Yarella Fernández" --org real --confirm
.venv/bin/python -m app.cli link-auth-subject <id_devuelto> --org real --by <id_devuelto>
```

Restricciones (el comando no sirve para fabricar administradoras):
- Roles **fijos** `admin`, sin `purchasing`: no reporta faltantes ni aparece en el selector del bot.
- **Solo en `simulation` y `real_test`**; producción y el archivo `legacy_mixed` se administran por la API autenticada.
- Se niega si ya hay una persona **activa con `auth_subject` enlazado** y rol `admin`: ya existe una vía de entrada.
- **Una sola administradora de bootstrap activa** por organización (se detecta por auditoría). Si se la desactiva,
  se permite recuperar el acceso; no se pueden acumular.
- Respeta la unicidad de nombre de `/staff`; no crea usuarios de Supabase, no envía correo y no toca a Pao.
- Auditoría **como operador** (`actor_id = NULL`, acción `staff.bootstrap_created`) y evento de outbox en la misma
  transacción.

La prueba aislada (A2) **no requiere escribir nada**: con `AUTH_MODE=supabase_jwt` en una segunda instancia
(puerto 8001) un JWT válido sin enlazar devuelve **403 `USER_NOT_PROVISIONED`**, y sin token o con basura, **401**.
Verificar eso primero, y solo después crear la fila y enlazar el `sub`.

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
1. ~~Crear el proyecto Supabase~~ **Hecho** (D-002 se cumplió para este proyecto): proyecto `faltantes-farmacia`, Google
   OAuth en modo de prueba, firmas asimétricas (D-068) y JWKS verificado.
2. ~~Alta de la primera administradora (D-071) y enlace de su `sub`~~ **Hecho el 2026-10-29**: `create-admin` +
   `link-auth-subject` con autorización explícita; `GET /api/v1/me` con Google devuelve 200 con rol `admin`.
3. ~~Verificar el mismo `sub` de Google y correo/contraseña (D-064)~~ **Hecho**: ambos métodos devuelven el mismo
   `sub` y el mismo 200 en la prueba aislada; con la sesión de correo/contraseña también responde 200.
4. Alta manual de la identidad de Pao cuando corresponda (correo + contraseña, correo confirmado) y enlace a su fila
   de `real_test` (D-067), sin modificar sus roles.
5. Configurar `SUPABASE_JWKS_URL`, `SUPABASE_JWT_ISSUER` y `AUTH_MODE=supabase_jwt` en el entorno desplegado;
   añadir el origen del frontend a `CORS_ORIGINS`.
6. Frontend Lovable: ingreso con Google y correo/contraseña, sin registro ni recuperación; enviar el bearer y
   tratar 401 (volver a ingresar) y 403 `USER_NOT_PROVISIONED` (cuenta no habilitada, sin ofrecer alta).

## Fuera de alcance / riesgos
- **Sin recuperación de contraseña** (D-066): si se pierden ambos accesos, el restablecimiento es operativo desde
  el panel de Supabase, no una función del producto.
- **Multi-organización (D-070):** `auth_subject` es único en toda la base. Hoy solo se enlaza `real_test`; al
  habilitar `production` habrá que decidir cómo una persona accede a más de una organización.
- Aprobar o comprar exigirá autenticación fuerte reciente (re-autenticación/MFA) en H5.
- Si Supabase deja de usarse, basta otro emisor OIDC: solo cambia el validador de JWT.
- Telegram **no** usa Supabase Auth: se identifica por cuenta autorizada + sesión de canal (H1-B).
