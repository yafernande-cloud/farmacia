# Diseño: autenticación web con Supabase Auth (NO activa todavía)

Estado: diseño aprobado en intención (D-005). Hoy rige `AUTH_MODE=demo_header` (solo local/demo).

## Flujo
1. Lovable inicia sesión con Supabase Auth (email+contraseña o magic link; MFA donde se habilite).
2. El navegador recibe un JWT de acceso y lo envía en `Authorization: Bearer <jwt>` a `/api/v1`.
3. FastAPI valida el JWT **sin llamar a Supabase en cada petición**: firma contra el JWKS del proyecto
   (clave asimétrica, caché con renovación), y comprueba `iss`, `aud`, `exp` y `role=authenticated`.
4. El `sub` del JWT se busca en `app_user.auth_subject` (columna ya creada, única). De ahí salen
   `organization_id` y `roles`. **Los permisos viven en nuestra base, no en claims editables** del token;
   un usuario válido en Supabase sin fila en `app_user` recibe 403.
5. Se construye el mismo `Actor` que hoy produce `get_actor` → los servicios no cambian.

## Qué se implementa al activarla
- Dependencia `get_actor` alternativa (`AUTH_MODE=supabase_jwt`) y variables `SUPABASE_URL`/`SUPABASE_JWT_AUD`
  (sin secretos: se usa JWKS público). Validador rechaza `AUTH_MODE=demo_header` fuera de local/demo (ya hecho).
- Alta de usuarios: la encargada/administrador crea la fila `app_user` y enlaza `auth_subject`
  (invitación desde Supabase). Sin auto-registro abierto.
- Pruebas con JWT firmados por una clave de prueba: expirado, `aud` errónea, firma inválida, sin `app_user`,
  usuario inactivo, organización ajena.
- Telegram **no** usa Supabase Auth: se identifica por sesión de canal + auxiliar (H1-B).

## Fuera de alcance / riesgos
- Aprobar o comprar exigirá autenticación fuerte reciente (re-autenticación/MFA) en H5.
- Si Supabase deja de usarse, basta otro emisor OIDC: solo cambia el validador de JWT.
