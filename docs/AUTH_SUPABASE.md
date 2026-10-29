# Autenticación web con Supabase Auth (validador JWT implementado; proyecto Supabase NO creado)

Estado (2026-10-28): el **validador de JWT está implementado y probado** (`app/core/supabase_auth.py`,
`AUTH_MODE=supabase_jwt`) y las operaciones administrativas de accesos de Telegram ya lo exigen en organizaciones reales
(`StrongActor`). El **proyecto Supabase todavía no se crea** (D-002) y el frontend no está construido; hoy, en desarrollo,
rige `AUTH_MODE=demo_header`, restringido a la organización de simulación para esas operaciones.

## Flujo
1. Lovable inicia sesión con Supabase Auth (email+contraseña o magic link; MFA donde se habilite).
2. El navegador recibe un JWT de acceso y lo envía en `Authorization: Bearer <jwt>` a `/api/v1`.
3. FastAPI valida el JWT **sin llamar a Supabase en cada petición**: firma contra el JWKS del proyecto
   (clave asimétrica, caché con renovación), y comprueba `iss`, `aud`, `exp` y `role=authenticated`.
4. El `sub` del JWT se busca en `app_user.auth_subject` (columna ya creada, única). De ahí salen
   `organization_id` y `roles`. **Los permisos viven en nuestra base, no en claims editables** del token;
   un usuario válido en Supabase sin fila en `app_user` recibe 403.
5. Se construye el mismo `Actor` que hoy produce `get_actor` → los servicios no cambian.

## Qué está implementado
- `AUTH_MODE=supabase_jwt` con `get_actor` resolviendo el `sub`; variables `SUPABASE_JWKS_URL`,
  `SUPABASE_JWT_ISSUER` y `SUPABASE_JWT_AUDIENCE` (sin secretos: se usa el JWKS público). El arranque exige JWKS e
  issuer cuando el modo es JWT.
- Validación local contra JWKS (caché de 300 s), algoritmos RS256/ES256, `iss`, `aud`, `exp` y `role=authenticated`.
- **Operaciones administrativas reales** (`/api/v1/telegram-accounts`, `/api/v1/telegram-access-requests`) exigen
  `StrongActor`: con `AUTH_MODE=demo_header` solo se aceptan en la organización de simulación; en `real_test` y
  `production` responden 403 `STRONG_AUTH_REQUIRED`. La cabecera `X-Demo-User-Id` nunca autoriza esas rutas reales.
- Pruebas con JWT firmados por una clave de prueba: firma inválida, `iss`/`aud` erróneos, expirado, sin `app_user`,
  usuario inactivo, resolución de permisos desde PostgreSQL y rechazo de la cabecera demo en organización real.
- Telegram **no** usa Supabase Auth: se identifica por cuenta autorizada + sesión de canal (H1-B).

## Qué falta para activarla
- Crear el proyecto Supabase (**no autorizado todavía**, D-002) y configurar las tres variables anteriores.
- Alta de usuarios: la encargada/administrador crea la fila `app_user` y enlaza `auth_subject`
  (invitación desde Supabase). Sin auto-registro abierto. Pao ya existe con rol `admin`; solo falta enlazar su
  `auth_subject`.

## Fuera de alcance / riesgos
- Aprobar o comprar exigirá autenticación fuerte reciente (re-autenticación/MFA) en H5.
- Si Supabase deja de usarse, basta otro emisor OIDC: solo cambia el validador de JWT.
