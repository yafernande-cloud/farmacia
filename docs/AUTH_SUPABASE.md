# Autenticación, usuarios y permisos

Actualizado: 2026-11-09. Este documento describe el acceso web real. Distingue **implementado en
código**, **probado localmente**, **verificado en producción** y **pendiente**. No contiene
contraseñas, tokens, claves ni identificadores internos de personas.

## 1. Estado

| Pieza | Estado |
|---|---|
| Validación de JWT de Supabase contra JWKS | Implementado; probado con claves de prueba |
| Google OAuth | Implementado; verificado en la prueba aislada y en producción |
| Correo + contraseña | Implementado en el frontend; verificado por el profesor en Lovable Preview |
| Enlace `app_user.auth_subject` | Implementado por CLI y por aprobación de solicitud |
| Roles y matriz de permisos | Implementado en PostgreSQL; probado |
| Solicitud pública y aprobación web (backend) | Implementado; 16 pruebas |
| Campanita de solicitudes en Lovable | **Solo demostración**; no llama a la API |
| Recuperación de contraseña / registro público | **No existe**, por decisión |
| MFA / reautenticación fuerte | Pendiente (H5); `StrongActor` no es MFA |

**Verificado en producción:** el profesor Ahmad Armoush inició sesión con correo y contraseña en
Lovable Preview y accedió al dashboard. La prueba anterior de Google y correo+contraseña con la
administradora de pruebas confirmó que ambos métodos entregan el mismo `sub`. La suite no llama a
Supabase real.

## 2. Flujo de acceso

1. El frontend crea una sesión con **Supabase Auth**: Google o correo+contraseña. No hay registro
   público ni recuperación de contraseña.
2. Supabase entrega un JWT de acceso. El navegador lo envía como
   `Authorization: Bearer <jwt>` a `/api/v1`.
3. FastAPI valida el JWT **sin llamar a Supabase en cada petición**: firma contra el JWKS público
   (caché de 300 s), `iss`, `aud`, `exp`, `iat`, `sub` y `role=authenticated`.
4. El `sub` se busca en `app_user.auth_subject` (columna única y opcional). De esa fila salen la
   organización y los roles. **Los permisos viven en la base**, no en claims editables.
5. Sin fila enlazada: `403 USER_NOT_PROVISIONED`. Persona inactiva: `403`. Acceso vencido:
   `403 WEB_ACCESS_EXPIRED`, evaluado en cada petición.
6. Se construye el mismo `Actor` que usa el resto del backend; los servicios no cambian.

Una persona con JWT válido pero sin fila enlazada **no ve ningún dato de negocio**.

## 3. Dos métodos, una sola identidad

- Google y correo+contraseña deben ser **dos identidades del mismo usuario de Supabase**, con un
  único `sub`. Ese `sub` se enlaza a **una sola** fila `app_user`.
- Si Supabase crea dos usuarios (dos `sub`), solo se enlaza uno; el otro recibe `403`. No se enlazan
  ambos ni se duplican permisos.
- Antes de habilitar a una persona se comprueba que ambos métodos devuelvan el mismo `id`, roles y
  organización en `GET /api/v1/me`.

## 4. Inicio de sesión en el frontend

Archivos principales: `src/lib/session.tsx`, `src/lib/auth.ts`, `src/routes/login.tsx`.

- **Correo + contraseña** ocupa el formulario principal: correo, contraseña y botón verde
  «Ingresar». Después aparece el separador «o» y, como método secundario, «Continuar con Google».
- `signInWithPassword` llama a `supabase.auth.signInWithPassword`, y luego valida la sesión contra
  `GET /api/v1/me` igual que Google.
- **Dentro del iframe de Lovable Preview** el ingreso con correo+contraseña funciona sin abrir otra
  pestaña, porque no hay redirección a un tercero. Google mantiene su comportamiento: en un iframe
  abre el login en una pestaña de nivel superior para no mezclar el verificador PKCE.
- La lista de sesión se deduplica por `access_token` para no repetir `/me` cuando Supabase notifica
  el mismo inicio por dos vías.
- Errores traducidos sin revelar si el correo existe:
  - credenciales inválidas y usuario inexistente → «Correo o contraseña incorrectos»;
  - correo no confirmado → aviso de confirmación pendiente;
  - demasiados intentos (429) → pedir espera;
  - red/`status 0` y 5xx → mensajes distintos;
  - desconocido → mensaje genérico.
- No hay enlace de «crear cuenta», «olvidé mi contraseña» ni «solicitar acceso» en esta pantalla.
- El backend **no necesitó cambios** para habilitar correo+contraseña: ya aceptaba cualquier JWT
  válido de Supabase. El proveedor Email de Supabase debe estar habilitado y el correo confirmado.

Commits del frontend: `fb327ea` (método y pruebas) y `3151f07` (orden del formulario). La última
suite frontend informada quedó en 238 pruebas aprobadas.

## 5. Roles y permisos

| Rol | Permisos |
|---|---|
| `auxiliary` | Ver lista; reportar faltantes |
| `purchasing` | Ver lista; reportar; aplicar cantidades; resolver observaciones; confirmar atributos |
| `approver` | Ver lista |
| `admin` | Ver lista; administrar personal, cuentas de Telegram, identidades y accesos web |

- **Único perfil funcional web: `admin`.** `purchasing`, `auxiliary` y `approver` son roles de
  negocio/Telegram; no habilitan el Cotizador por sí solos.
- `admin` **no** tiene `REPORT_SHORTAGE`: no aparece en el selector del bot ni reporta faltantes.
  D-124 (ampliarlo) sigue aprobada y sin implementar.
- Las rutas administrativas (`/staff`, cuentas y solicitudes de Telegram, identidades, observaciones,
  cotizaciones y fotos de mantenimiento) exigen `StrongActor`. En `AUTH_MODE=demo_header` solo se
  aceptan en la organización de simulación; en organizaciones reales, `403 STRONG_AUTH_REQUIRED`.
- `StrongActor` significa JWT válido + persona activa + rol/organización, **no** MFA ni una segunda
  autenticación.
- `admin` es también el rol que exige el Cotizador (E10–E15).

## 6. Solicitud y aprobación de acceso web (G1)

### Lo que hace el backend

| Método y ruta | Comportamiento |
|---|---|
| `POST /api/v1/web-access-requests` | Público con JWT de Supabase y **correo verificado**. Cuerpo `{}`. Responde siempre `202 {"received": true}`. Organización desde `WEB_ACCESS_ORGANIZATION_ID`. Sin configuración: `503 ACCESS_NOT_CONFIGURED`. Límite: 5/min por `sub` y 20/min por IP (`429 RATE_LIMITED`). No otorga nada. |
| `GET /api/v1/web-access-requests` | Campanita: cualquier `admin` ve las pendientes de su organización; la superadministradora ve todas, con el nombre de la organización. |
| `POST /api/v1/web-access-requests/{id}/approve` | Crea `app_user` con `roles=['admin']`, `active`, `available`, `auth_subject` y `web_access_expires_at=NULL`, en un solo paso. No enlaza ni sobrescribe una fila existente: `409 USER_ALREADY_EXISTS` / `409 DUPLICATE_NAME` (la solicitud queda pendiente). |
| `POST /api/v1/web-access-requests/{id}/reject` | Rechaza con motivo opcional ≤ 500 caracteres; no crea persona. |
| `POST /api/v1/admin/users/{id}/web-access` | **Solo superadministración**: otorga/ajusta roles y vencimiento. |
| `POST /api/v1/admin/users/{id}/web-access/revoke` | **Solo superadministración**: revoca el acceso web sin tocar Telegram ni `active`. `SUPERADMIN_PROTECTED` impide modificar a la superadministradora. |

Reglas adicionales:

- Aprobar una solicitud **no crea cuenta ni vínculo de Telegram**. Los planos son independientes.
- La solicitud es idempotente por `(organización, auth_subject)`.
- La organización nunca viaja en el cuerpo.
- Una sola superadministradora activa en toda la base. Se marca una vez con
  `set-superadmin <app_user_id>` (CLI, auditado con actor nulo). Es un control técnico de la
  propietaria: ve todas las organizaciones y otorga/revoca acceso, pero no aprueba en nombre de una
  farmacia.
- El acceso aprobado no vence automáticamente; vence por revocación, desactivación o fecha explícita.

### Lo que hace el frontend

- Existe una campanita (`src/components/SolicitudesAcceso.tsx`) montada en la cabecera, pero usa
  `solicitudesDemo` y estado local: **no llama a `/web-access-requests`** y sus botones solo simulan
  aprobar/rechazar («demo, sin guardar en el servidor»).
- La pantalla de login, ante `403`, muestra «Acceso no habilitado» y sugiere contactar a la
  administradora. **No ofrece un botón para solicitar acceso.**
- Conclusión: el backend de solicitudes está completo y probado, pero **el flujo Google → solicitud →
  campanita → aprobación no está disponible ni verificado de punta a punta desde Lovable**. Habilitar
  a una persona hoy requiere el procedimiento operativo de §7.

## 7. Alta y enlace de una persona (procedimiento real)

El caso usado para habilitar al profesor **Ahmad Armoush** fue:

1. **Crear la cuenta en Supabase Auth** (panel de Supabase): correo y contraseña, correo confirmado.
   No se crea desde el backend.
2. **Crear la persona en el backend** con el servicio auditado `create_staff`, roles `["admin"]`, en
   la organización real y con un administrador activo como actor. Como todavía no había sesión web
   para esa persona, se ejecutó desde **Heroku Run Console** con una llamada puntual al servicio; no
   se usó SQL directo ni `create-admin`. El servicio exige `MANAGE_STAFF`, valida el nombre único y
   registra `staff.created` en la misma transacción.
3. **Enlazar el `sub`** de Supabase con la CLI:
   ```bash
   cd backend
   .venv/bin/python -m app.cli link-auth-subject <APP_USER_ID> --org real --by <ADMIN_ID> --subject <SUB>
   ```
   Sin `--subject`, la CLI lo pide por consola para no dejarlo en el historial del shell. Reglas:
   persona activa, misma organización, sin reemplazo y `AUTH_SUBJECT_IN_USE` si ya pertenece a otra
   persona.
4. **Verificar**: iniciar sesión y comprobar `GET /api/v1/me` → `200`, rol `admin`, organización
   correcta. En la prueba del profesor, el dashboard abrió correctamente desde Lovable Preview con
   correo y contraseña.

Comprobaciones útiles:

```bash
.venv/bin/python -m app.cli list-auth-subjects --org real --by <ADMIN_ID>
.venv/bin/python -m app.cli unlink-auth-subject <APP_USER_ID> --org real --by <ADMIN_ID> --confirm
```

`unlink-auth-subject` retira el acceso web sin borrar a la persona ni su historial.

### Bootstrap (`create-admin`)

Existe solo para el primer acceso cuando **nadie** puede iniciar sesión:

```bash
.venv/bin/python -m app.cli create-admin "Nombre" --org real --confirm
```

Restricciones: solo `admin`, solo `simulation`/`real_test`, prohibido en producción, una sola
administradora de bootstrap activa por organización, y se niega si ya hay una persona activa con rol
`admin` y `auth_subject` enlazado. **No crea usuarios de Supabase ni enlaza identidad.** En la
operación actual ya existe una vía de acceso, por lo que no es el mecanismo para altas nuevas.

## 8. Desactivación y revocación

| Acción | Efecto | Quién |
|---|---|---|
| `POST /api/v1/staff/{id}/deactivate` | `active=false`: pierde acceso web y deja de ser elegible en Telegram; conserva historial | `MANAGE_STAFF` + `StrongActor` |
| `POST /api/v1/staff/{id}/activate` | Reactiva la pertenencia | `MANAGE_STAFF` + `StrongActor` |
| `POST /api/v1/admin/users/{id}/web-access/revoke` | Fija el vencimiento del acceso web; no toca Telegram ni `active` | Solo superadministración |
| `POST /api/v1/staff/{id}/delete-auxiliary` | Baja **terminal como auxiliar de Telegram**: revoca sesiones y vínculos del bot, conserva autoría, **no toca** `active`, roles, `auth_subject` ni acceso web | `MANAGE_STAFF` + `StrongActor` |
| `unlink-auth-subject` | Retira el enlace web; la persona y su historial permanecen | CLI administrativa |

Una administradora eliminada del listado de auxiliares de Telegram **sigue entrando al Cotizador**.
Para reincorporar a una persona dada de baja como auxiliar se crea una persona nueva con UUID nuevo;
el nombre queda liberado.

## 9. Pruebas

- `backend/tests/test_auth_identity.py`: JWT firmados con claves de prueba (firma inválida,
  `iss`/`aud` erróneos, expirado, sin `app_user`, inactivo), resolución de permisos y enlace/desenlace.
- `backend/tests/test_bootstrap_admin.py`: restricciones de `create-admin`.
- `backend/tests/test_web_access_api.py` (16): solicitud, idempotencia, campanita por organización,
  aprobación que otorga `admin`, entrada al Cotizador, rechazo, duplicados, superadministración,
  vencimiento, límite de tasa y falta de organización.
- Frontend: `auth.test.ts` (rutas, protección y mensajes), `password-login.test.tsx` (7 casos,
  incluido el orden visual), `session-password.test.tsx` (2 casos del proveedor).

La suite no crea usuarios reales en Supabase ni usa tokens de producción.

## 10. Límites y pendientes

- **Sin registro público ni recuperación de contraseña.** Si se pierden ambos accesos, el
  restablecimiento es operativo desde el panel de Supabase.
- **Multi-organización:** `auth_subject` es único en toda la base. Hoy el acceso web se usa en una
  organización; falta decidir cómo una persona accede a varias.
- **Campanita real en Lovable:** pendiente de conectar; hoy es demostración.
- **MFA:** pendiente (H5).
- **D-124:** `admin` no reporta faltantes por Telegram; sigue aprobada y sin implementar.
- La cuenta de Pao en `real_test` continúa sin `auth_subject`; se enlazará con su propia identidad
  cuando corresponda.
