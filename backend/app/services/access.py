"""Acceso de cuentas de Telegram: solicitud desde el bot, aprobación/bloqueo desde la API.

Reglas de seguridad:
  * Una solicitud `pending` NO crea cuenta ni vínculo: no concede ninguna función operativa.
  * El código se guarda solo como SHA-256, vence a los 30 minutos, es de un solo uso y tiene
    límites de intentos. El código en claro nunca se audita, registra ni devuelve por la API.
  * Aprobar, rechazar, bloquear y rehabilitar exigen `manage_accounts` (rol `admin`) y se validan
    en el servidor; conocer el código no autoriza nada.
  * La autorización se valida SIEMPRE en el servidor, en dos momentos del procesamiento del bot:
    antes de persistir el evento y dentro de la transacción que lo procesa.
  * Bloquear no borra nada: desactiva vínculos y cierra sesiones; el historial se conserva.

`telegram_account.access_role` (admin/operator) describe el TIPO de cuenta; no otorga permisos de
negocio. Los permisos siguen saliendo de `app_user.roles`.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import DomainError, not_found
from app.core.invitations import code_hash, generate_code, normalize_code
from app.core.permissions import Permission, require
from app.models import (
    AppUser,
    AuditEvent,
    ChannelBinding,
    Organization,
    OutboundMessage,
    TelegramAccessAttempt,
    TelegramAccessRequest,
    TelegramAccount,
)
from app.services import sessions
from app.services.actors import Actor
from app.services.events import advisory_lock, emit
from app.services.provenance import OrgKind, org_kind, validate_account, validate_binding

MODE_PERSONAL = "personal"
MODE_SHARED = "shared_device"
MODES = {MODE_PERSONAL, MODE_SHARED}
ACCESS_ROLES = {"admin", "operator"}
MAX_REASON = 200
MAX_LABEL = 100

# Decisión del usuario: los códigos de solicitud viven 30 minutos.
CODE_TTL = timedelta(minutes=30)
ATTEMPT_WINDOW = timedelta(minutes=15)
MAX_CODE_ATTEMPTS = 5
MAX_ADMIN_ATTEMPTS = 10

_INVALID_CODE = "El código no es válido, ya fue usado o expiró. Pide uno nuevo desde el bot."
_TOO_MANY = "Demasiados intentos fallidos. Espera unos minutos y vuelve a intentarlo."


@dataclass(frozen=True)
class AccountContext:
    """Lo que el flujo del bot necesita saber de la cuenta que envía (nunca la persona por sí sola)."""

    account_id: uuid.UUID
    mode: str
    app_user_id: uuid.UUID | None
    access_role: str
    display_label: str

    @property
    def personal(self) -> bool:
        return self.mode == MODE_PERSONAL


@dataclass(frozen=True)
class Access:
    binding: ChannelBinding
    account: TelegramAccount | None


@dataclass(frozen=True)
class IssuedRequest:
    request: TelegramAccessRequest
    code: str | None  # None si ya había una solicitud pendiente o era un reintento de Telegram
    created: bool


@dataclass(frozen=True)
class LookupResult:
    """Resultado de buscar un código. Los intentos fallidos se persisten aunque haya error."""

    request: TelegramAccessRequest | None = None
    existing_account: TelegramAccount | None = None
    error: DomainError | None = None


@dataclass(frozen=True)
class Approval:
    account: TelegramAccount
    binding: ChannelBinding
    request: TelegramAccessRequest


@dataclass(frozen=True)
class Block:
    account: TelegramAccount
    sessions_revoked: int
    bindings_deactivated: int


@dataclass(frozen=True)
class Reactivation:
    account: TelegramAccount
    bindings_reactivated: int


def _now() -> datetime:
    return datetime.now(UTC)


def _clean_reason(reason: str | None) -> str | None:
    return (reason or "").strip()[:MAX_REASON] or None


def _clean_label(label: str | None) -> str:
    value = (label or "").strip()
    if not value:
        raise DomainError(
            "LABEL_REQUIRED", "Indica un nombre o etiqueta para la cuenta.", status_code=400
        )
    if len(value) > MAX_LABEL:
        raise DomainError(
            "LABEL_TOO_LONG",
            f"La etiqueta no puede superar {MAX_LABEL} caracteres.",
            status_code=400,
        )
    return value


def _invalid_code() -> DomainError:
    # Un solo mensaje para código inexistente, vencido, usado o rechazado: no filtra cuál fue.
    return DomainError("INVALID_ACCESS_CODE", _INVALID_CODE, status_code=400)


def _too_many_attempts() -> DomainError:
    return DomainError("TOO_MANY_ATTEMPTS", _TOO_MANY, status_code=429)


def _account_denied(message: str) -> DomainError:
    return DomainError("ACCOUNT_NOT_AUTHORIZED", message, status_code=403)


def _validate_mode_role(mode: str, access_role: str) -> None:
    if mode not in MODES:
        raise DomainError("INVALID_MODE", "El modo debe ser personal o shared_device.", status_code=400)
    if access_role not in ACCESS_ROLES:
        raise DomainError(
            "INVALID_ACCESS_ROLE", "El rol de cuenta debe ser admin u operator.", status_code=400
        )


async def _load_person(session: AsyncSession, org: uuid.UUID, user_id: uuid.UUID) -> AppUser:
    person = (
        await session.execute(
            select(AppUser).where(AppUser.id == user_id, AppUser.organization_id == org)
        )
    ).scalar_one_or_none()
    if person is None:
        raise not_found("La persona")
    if not person.active:
        raise DomainError(
            "PERSON_INACTIVE",
            f"{person.display_name} ya no está activa en la farmacia.",
            status_code=403,
        )
    return person


async def _organization(session: AsyncSession, org_id: uuid.UUID) -> Organization:
    org = await session.get(Organization, org_id)
    if org is None:
        raise DomainError(
            "ACCESS_ORGANIZATION_NOT_CONFIGURED",
            "La organización de solicitudes de acceso no existe.",
            status_code=503,
        )
    if org.kind == OrgKind.LEGACY_MIXED:
        raise DomainError(
            "ACCESS_ORGANIZATION_NOT_AVAILABLE",
            "Esta organización no acepta solicitudes de acceso nuevas.",
            status_code=409,
        )
    return org


def _request_status(req: TelegramAccessRequest, now: datetime) -> str:
    if req.status == "pending" and req.expires_at <= now:
        return "expired"
    return req.status


async def request_access(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID | None,
    provider: str,
    external_user_id: str | None,
    external_chat_id: str,
    chat_kind: str | None,
    update_id: str,
    now: datetime | None = None,
) -> IssuedRequest:
    """Registra (o reutiliza) una solicitud desde un chat privado aún no autorizado.

    No crea cuenta ni vínculo. Devuelve el código en claro UNA vez; nunca lo persiste.
    """
    now = now or _now()
    if chat_kind != "private":
        raise DomainError(
            "ACCESS_PRIVATE_ONLY",
            "El acceso se solicita solo en un chat privado con el bot.",
            status_code=400,
        )
    if not external_user_id or str(external_user_id) != str(external_chat_id):
        raise DomainError(
            "ACCESS_PRIVATE_ONLY",
            "No se pudo identificar la cuenta que escribe en este chat.",
            status_code=400,
        )
    if organization_id is None:
        raise DomainError(
            "ACCESS_NOT_CONFIGURED",
            "Las solicitudes de acceso no están configuradas en este entorno.",
            status_code=503,
        )

    await advisory_lock(session, f"tgacct:{provider}:{external_user_id}")
    org = await _organization(session, organization_id)
    validate_binding(str(external_chat_id), org.kind)
    validate_account(str(external_user_id), org.kind)

    # Reintento de Telegram: el mismo update_id no genera otra solicitud ni otro código.
    previous = (
        await session.execute(
            select(TelegramAccessRequest)
            .where(
                TelegramAccessRequest.provider == provider,
                TelegramAccessRequest.request_update_id == str(update_id),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if previous is not None:
        if (
            previous.organization_id != org.id
            or previous.external_user_id != str(external_user_id)
        ):
            raise DomainError(
                "REQUEST_CONFLICT",
                "No se pudo registrar la solicitud. Inténtalo de nuevo.",
                status_code=409,
            )
        return IssuedRequest(previous, None, False)

    account = (
        await session.execute(
            select(TelegramAccount).where(
                TelegramAccount.provider == provider,
                TelegramAccount.external_user_id == str(external_user_id),
            )
        )
    ).scalar_one_or_none()
    if account is not None:
        if account.organization_id != org.id:
            raise DomainError(
                "ACCOUNT_NOT_AVAILABLE",
                "Esta cuenta de Telegram ya está asociada a otra organización.",
                status_code=409,
            )
        if account.status == "active":
            raise DomainError(
                "ACCOUNT_ALREADY_AUTHORIZED",
                "Esta cuenta de Telegram ya está autorizada.",
                status_code=409,
            )

    active_chat = (
        await session.execute(
            select(ChannelBinding).where(
                ChannelBinding.provider == provider,
                ChannelBinding.external_chat_id == str(external_chat_id),
                ChannelBinding.status == "active",
            )
        )
    ).scalar_one_or_none()
    if active_chat is not None:
        raise DomainError(
            "CHAT_ALREADY_AUTHORIZED",
            "Este chat ya está autorizado. Pide a un administrador que revise la cuenta.",
            status_code=409,
        )

    pending = (
        await session.execute(
            select(TelegramAccessRequest)
            .where(
                TelegramAccessRequest.provider == provider,
                TelegramAccessRequest.external_user_id == str(external_user_id),
                TelegramAccessRequest.status == "pending",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if pending is not None:
        if pending.organization_id != org.id:
            raise DomainError(
                "ACCOUNT_NOT_AVAILABLE",
                "Esta cuenta de Telegram tiene una solicitud en otra organización.",
                status_code=409,
            )
        if pending.expires_at > now:
            # Un reintento del MISMO update de Telegram no genera otro código. Un /solicitar nuevo
            # rota el código (sigue habiendo UNA sola solicitud pendiente); el anterior deja de servir.
            try:
                is_newer = int(str(update_id)) > int(pending.request_update_id)
            except ValueError:
                is_newer = str(update_id) != pending.request_update_id
            if not is_newer:
                return IssuedRequest(pending, None, False)
            code = generate_code()
            pending.code_hash = code_hash(code)
            pending.request_update_id = str(update_id)
            pending.expires_at = now + CODE_TTL
            await session.flush()
            emit(session, org_id=org.id, actor_id=None, action="telegram_access_request.code_renewed",
                 entity_type="telegram_access_request", entity_id=pending.id,
                 event_type="telegram.access_request_code_renewed", aggregate_version=None,
                 payload={"request_id": str(pending.id),
                          "external_user_id": pending.external_user_id,
                          "expires_at": pending.expires_at.isoformat()})  # fmt: skip
            return IssuedRequest(pending, code, False)
        pending.status = "expired"
        emit(session, org_id=org.id, actor_id=None, action="telegram_access_request.expired",
             entity_type="telegram_access_request", entity_id=pending.id,
             event_type="telegram.access_request_expired", aggregate_version=None,
             payload={"request_id": str(pending.id), "external_user_id": pending.external_user_id})  # fmt: skip
        await session.flush()

    code = generate_code()
    req = TelegramAccessRequest(
        organization_id=org.id,
        provider=provider,
        external_user_id=str(external_user_id),
        external_chat_id=str(external_chat_id),
        request_update_id=str(update_id),
        code_hash=code_hash(code),
        status="pending",
        created_at=now,
        expires_at=now + CODE_TTL,
    )
    session.add(req)
    await session.flush()
    emit(session, org_id=org.id, actor_id=None, action="telegram_access_request.created",
         entity_type="telegram_access_request", entity_id=req.id,
         event_type="telegram.access_requested", aggregate_version=None,
         payload={"request_id": str(req.id), "external_user_id": req.external_user_id,
                  "external_chat_id": req.external_chat_id,
                  "expires_at": req.expires_at.isoformat()})  # fmt: skip
    return IssuedRequest(req, code, True)


async def _attempts_since(
    session: AsyncSession, org: uuid.UUID, since: datetime, *, code: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> int:  # fmt: skip
    query = select(func.count()).select_from(TelegramAccessAttempt).where(
        TelegramAccessAttempt.organization_id == org,
        TelegramAccessAttempt.success.is_(False),
        TelegramAccessAttempt.attempted_at >= since,
    )  # fmt: skip
    if code is not None:
        query = query.where(TelegramAccessAttempt.code_hash == code)
    if actor_id is not None:
        query = query.where(TelegramAccessAttempt.app_user_id == actor_id)
    return (await session.execute(query)).scalar_one()


def _record_attempt(
    session: AsyncSession, org: uuid.UUID, actor_id: uuid.UUID, hashed: str, *, success: bool,
    now: datetime,
) -> None:  # fmt: skip
    session.add(
        TelegramAccessAttempt(
            organization_id=org, app_user_id=actor_id, code_hash=hashed, success=success,
            attempted_at=now,
        )
    )  # fmt: skip


async def lookup_request(
    session: AsyncSession, actor: Actor, *, code: str, now: datetime | None = None
) -> LookupResult:
    """Busca la solicitud de un código. Devuelve el error en vez de lanzarlo para que el intento
    fallido se confirme (si se lanzara, el rollback borraría el contador de intentos)."""
    require(actor.roles, Permission.MANAGE_ACCOUNTS)
    now = now or _now()
    normalized = normalize_code(code)
    if not normalized:
        raise DomainError("CODE_REQUIRED", "Ingresa el código que te compartió la persona.", status_code=400)

    org = actor.organization_id
    hashed = code_hash(normalized)
    await advisory_lock(session, f"tgattempt:{org}:{actor.id}")
    since = now - ATTEMPT_WINDOW

    if await _attempts_since(session, org, since, actor_id=actor.id) >= MAX_ADMIN_ATTEMPTS:
        return LookupResult(error=_too_many_attempts())
    if await _attempts_since(session, org, since, code=hashed) >= MAX_CODE_ATTEMPTS:
        _record_attempt(session, org, actor.id, hashed, success=False, now=now)
        await session.flush()
        return LookupResult(error=_too_many_attempts())

    req = (
        await session.execute(
            select(TelegramAccessRequest)
            .where(
                TelegramAccessRequest.organization_id == org,
                TelegramAccessRequest.code_hash == hashed,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if req is not None and req.status == "pending" and req.expires_at <= now:
        req.status = "expired"
        emit(session, org_id=org, actor_id=actor.id, action="telegram_access_request.expired",
             entity_type="telegram_access_request", entity_id=req.id,
             event_type="telegram.access_request_expired", aggregate_version=None,
             payload={"request_id": str(req.id), "external_user_id": req.external_user_id})  # fmt: skip

    valid = req is not None and req.status == "pending" and req.expires_at > now
    _record_attempt(session, org, actor.id, hashed, success=valid, now=now)
    await session.flush()
    if not valid:
        return LookupResult(error=_invalid_code())

    account = (
        await session.execute(
            select(TelegramAccount).where(
                TelegramAccount.provider == req.provider,
                TelegramAccount.external_user_id == req.external_user_id,
            )
        )
    ).scalar_one_or_none()
    return LookupResult(request=req, existing_account=account)


async def approve_request(
    session: AsyncSession,
    actor: Actor,
    request_id: uuid.UUID,
    *,
    display_label: str,
    mode: str,
    app_user_id: uuid.UUID | None = None,
    access_role: str = "operator",
    now: datetime | None = None,
) -> Approval:
    """Aprueba una solicitud, crea/reutiliza la cuenta y habilita el chat. Todo en una transacción."""
    require(actor.roles, Permission.MANAGE_ACCOUNTS)
    now = now or _now()
    label = _clean_label(display_label)
    _validate_mode_role(mode, access_role)

    req = (
        await session.execute(
            select(TelegramAccessRequest)
            .where(
                TelegramAccessRequest.id == request_id,
                TelegramAccessRequest.organization_id == actor.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if req is None:
        raise not_found("La solicitud de acceso")
    if req.status != "pending" or req.expires_at <= now:
        raise DomainError(
            "REQUEST_NOT_PENDING",
            "La solicitud ya fue aprobada, rechazada o expiró. Pide una nueva desde el bot.",
            status_code=409,
        )

    kind = await org_kind(session, actor.organization_id)
    validate_binding(req.external_chat_id, kind)
    validate_account(req.external_user_id, kind)

    if mode == MODE_PERSONAL:
        if app_user_id is None:
            raise DomainError(
                "PERSON_REQUIRED",
                "Una cuenta personal debe indicar la persona que quedará ligada.",
                status_code=400,
            )
        person = await _load_person(session, actor.organization_id, app_user_id)
        label = label or person.display_name
    else:
        if app_user_id is not None:
            raise DomainError(
                "SHARED_WITHOUT_PERSON",
                "Un teléfono compartido no se liga a una persona: la identidad se elige en cada sesión.",
                status_code=400,
            )

    await advisory_lock(session, f"tgacct:{req.provider}:{req.external_user_id}")
    account = (
        await session.execute(
            select(TelegramAccount)
            .where(
                TelegramAccount.provider == req.provider,
                TelegramAccount.external_user_id == req.external_user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if account is not None:
        if account.organization_id != actor.organization_id:
            raise DomainError(
                "ACCOUNT_NOT_AVAILABLE",
                "Esta cuenta de Telegram ya está asociada a otra organización.",
                status_code=409,
            )
        if account.status == "active":
            raise DomainError(
                "ACCOUNT_ALREADY_AUTHORIZED",
                "Esta cuenta de Telegram ya está autorizada.",
                status_code=409,
            )

    binding = (
        await session.execute(
            select(ChannelBinding)
            .where(
                ChannelBinding.provider == req.provider,
                ChannelBinding.external_chat_id == req.external_chat_id,
                ChannelBinding.status == "active",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if binding is not None:
        raise DomainError(
            "CHAT_ALREADY_AUTHORIZED",
            "Este chat ya está autorizado por otra cuenta. Bloquea esa cuenta primero.",
            status_code=409,
        )

    if account is None:
        account = TelegramAccount(
            organization_id=actor.organization_id,
            provider=req.provider,
            external_user_id=req.external_user_id,
            display_label=label,
            mode=mode,
            access_role=access_role,
            app_user_id=app_user_id,
            status="active",
            origin="access_request",
            created_from_request_id=req.id,
            created_at=now,
            created_by=actor.id,
        )
        session.add(account)
    else:  # cuenta bloqueada: se reutiliza la fila para conservar su historial
        account.display_label = label
        account.mode, account.access_role = mode, access_role
        account.app_user_id, account.status = app_user_id, "active"
        account.blocked_at, account.blocked_by, account.block_reason = None, None, None
        account.reactivated_at, account.reactivated_by = now, actor.id
    await session.flush()

    binding = ChannelBinding(
        organization_id=actor.organization_id,
        provider=req.provider,
        external_chat_id=req.external_chat_id,
        chat_kind="private",
        mode=mode,
        account_id=account.id,
        status="active",
    )
    session.add(binding)
    req.status = "approved"
    req.approved_at, req.approved_by, req.approved_account_id = now, actor.id, account.id
    await session.flush()

    emit(session, org_id=actor.organization_id, actor_id=actor.id,
         action="telegram_account.authorized", entity_type="telegram_account",
         entity_id=account.id, event_type="telegram.account_authorized", aggregate_version=None,
         payload={"account_id": str(account.id), "binding_id": str(binding.id), "mode": mode,
                  "access_role": access_role,
                  "app_user_id": str(app_user_id) if app_user_id else None,
                  "request_id": str(req.id), "origin": account.origin},
         after={"display_label": label, "mode": mode, "access_role": access_role,
                "status": "active"})  # fmt: skip
    emit(session, org_id=actor.organization_id, actor_id=actor.id,
         action="telegram_access_request.approved", entity_type="telegram_access_request",
         entity_id=req.id, event_type="telegram.access_request_approved", aggregate_version=None,
         payload={"request_id": str(req.id), "account_id": str(account.id),
                  "binding_id": str(binding.id), "mode": mode, "access_role": access_role})  # fmt: skip
    _notify(
        session, binding,
        (
            f"Tu acceso quedó autorizado. Desde ahora reportas como {account.display_label}."
            if mode == MODE_PERSONAL
            else "Este teléfono quedó autorizado. Al reportar, elige quién escribe."
        ),
    )
    await session.flush()
    return Approval(account, binding, req)


async def reject_request(
    session: AsyncSession,
    actor: Actor,
    request_id: uuid.UUID,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> TelegramAccessRequest:
    require(actor.roles, Permission.MANAGE_ACCOUNTS)
    now = now or _now()
    req = (
        await session.execute(
            select(TelegramAccessRequest)
            .where(
                TelegramAccessRequest.id == request_id,
                TelegramAccessRequest.organization_id == actor.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if req is None:
        raise not_found("La solicitud de acceso")
    if req.status != "pending" or req.expires_at <= now:
        raise DomainError(
            "REQUEST_NOT_PENDING", "La solicitud ya no está pendiente.", status_code=409
        )
    req.status = "rejected"
    req.rejected_at, req.rejected_by = now, actor.id
    req.reject_reason = _clean_reason(reason)
    emit(session, org_id=actor.organization_id, actor_id=actor.id,
         action="telegram_access_request.rejected", entity_type="telegram_access_request",
         entity_id=req.id, event_type="telegram.access_request_rejected", aggregate_version=None,
         payload={"request_id": str(req.id), "reason": req.reject_reason})  # fmt: skip
    await session.flush()
    return req


def _notify(session: AsyncSession, binding: ChannelBinding, text: str) -> None:
    """Mensaje de confirmación en la cola del bot. Nunca contiene códigos."""
    session.add(
        OutboundMessage(
            organization_id=binding.organization_id,
            channel_binding_id=binding.id,
            inbound_event_id=None,
            seq=0,
            method="sendMessage",
            payload={"text": text, "buttons": [], "keyboard": None},
        )
    )


async def resolve_access(
    session: AsyncSession, provider: str, chat_id: str, sender_id: str | None
) -> Access:
    """¿Puede esta cuenta enviar mensajes a este chat? Se valida en el servidor, no con botones."""
    binding = await sessions.get_binding(session, provider, chat_id)
    if binding is None:
        raise DomainError(
            "CHAT_NOT_AUTHORIZED",
            f"Este chat no está autorizado para la farmacia. ID del chat: {chat_id}. "
            "Si necesitas acceso, escribe /solicitar.",
            status_code=403,
        )
    account = None
    if sender_id:
        account = (
            await session.execute(
                select(TelegramAccount).where(
                    TelegramAccount.provider == provider,
                    TelegramAccount.external_user_id == str(sender_id),
                )
            )
        ).scalar_one_or_none()

    if binding.account_id is not None:
        if account is None or account.id != binding.account_id:
            raise _account_denied(
                f"Esta cuenta de Telegram no está autorizada para este chat. ID del chat: {chat_id}. "
                "Pide a un administrador que revise la autorización."
            )
        if account.status != "active":
            raise _account_denied(
                "La autorización de esta cuenta de Telegram fue bloqueada. "
                "Si necesitas acceso, escribe /solicitar."
            )
        if binding.mode == MODE_PERSONAL and account.app_user_id is None:
            raise _account_denied("Esta cuenta personal no tiene una persona vinculada.")
        return Access(binding, account)

    # Vínculo legado (grupo o chat anterior a la multicuenta): se mantiene, salvo que la cuenta
    # que envía esté bloqueada o pertenezca a otra organización.
    if account is not None:
        if account.organization_id != binding.organization_id:
            raise _account_denied("Esta cuenta de Telegram no pertenece a esta organización.")
        if account.status != "active":
            raise _account_denied(
                "La autorización de esta cuenta de Telegram fue bloqueada. "
                "Si necesitas acceso, escribe /solicitar."
            )
    return Access(binding, account)


async def block_account(
    session: AsyncSession,
    actor: Actor,
    account_id: uuid.UUID,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> Block:
    """Bloquea la cuenta, desactiva sus chats y cierra sus sesiones. NO borra historial."""
    require(actor.roles, Permission.MANAGE_ACCOUNTS)
    now = now or _now()
    account = (
        await session.execute(
            select(TelegramAccount)
            .where(
                TelegramAccount.id == account_id,
                TelegramAccount.organization_id == actor.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise not_found("La cuenta de Telegram")
    if account.status == "blocked":
        return Block(account, 0, 0)

    account.status = "blocked"
    account.blocked_at = now
    account.blocked_by = actor.id
    account.block_reason = _clean_reason(reason)
    bindings = list(
        (
            await session.execute(
                select(ChannelBinding)
                .where(
                    ChannelBinding.organization_id == actor.organization_id,
                    ChannelBinding.account_id == account.id,
                    ChannelBinding.status == "active",
                )
                .with_for_update()
            )
        ).scalars()
    )
    sessions_revoked = 0
    for binding in bindings:
        sessions_revoked += await sessions.revoke_binding_sessions(session, binding, now, by=actor.id)
        binding.status = "inactive"
        emit(session, org_id=actor.organization_id, actor_id=actor.id,
             action="channel_binding.revoked", entity_type="channel_binding",
             entity_id=binding.id, event_type="channel.binding_revoked", aggregate_version=None,
             payload={"binding_id": str(binding.id), "account_id": str(account.id)})  # fmt: skip
    await session.flush()
    emit(session, org_id=actor.organization_id, actor_id=actor.id,
         action="telegram_account.blocked", entity_type="telegram_account",
         entity_id=account.id, event_type="telegram.account_blocked", aggregate_version=None,
         payload={"account_id": str(account.id), "sessions_revoked": sessions_revoked,
                  "bindings_deactivated": len(bindings), "reason": account.block_reason},
         before={"status": "active"}, after={"status": "blocked"})  # fmt: skip
    await session.flush()
    return Block(account, sessions_revoked, len(bindings))


async def reactivate_account(
    session: AsyncSession,
    actor: Actor,
    account_id: uuid.UUID,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> Reactivation:
    """Rehabilita una cuenta bloqueada recreando sus chats privados. Conserva todo el historial."""
    require(actor.roles, Permission.MANAGE_ACCOUNTS)
    now = now or _now()
    account = (
        await session.execute(
            select(TelegramAccount)
            .where(
                TelegramAccount.id == account_id,
                TelegramAccount.organization_id == actor.organization_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        raise not_found("La cuenta de Telegram")
    if account.status == "active":
        return Reactivation(account, 0)
    if account.mode == MODE_PERSONAL:
        assert account.app_user_id is not None
        await _load_person(session, actor.organization_id, account.app_user_id)

    previous = list(
        (
            await session.execute(
                select(ChannelBinding)
                .where(
                    ChannelBinding.organization_id == actor.organization_id,
                    ChannelBinding.account_id == account.id,
                    ChannelBinding.status == "inactive",
                    ChannelBinding.chat_kind == "private",
                )
                .order_by(ChannelBinding.created_at.desc())
            )
        ).scalars()
    )
    chats: dict[str, ChannelBinding] = {}
    for b in previous:
        chats.setdefault(b.external_chat_id, b)
    if not chats:
        raise DomainError(
            "ACCOUNT_WITHOUT_CHAT",
            "La cuenta no tiene un chat privado previo que pueda rehabilitarse. "
            "Pide una solicitud nueva desde el bot.",
            status_code=409,
        )

    for chat_id in chats:
        conflict = (
            await session.execute(
                select(ChannelBinding)
                .where(
                    ChannelBinding.provider == account.provider,
                    ChannelBinding.external_chat_id == chat_id,
                    ChannelBinding.status == "active",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if conflict is not None:
            raise DomainError(
                "CHAT_ALREADY_AUTHORIZED",
                f"El chat {chat_id} está autorizado por otra cuenta. Bloquea esa cuenta primero.",
                status_code=409,
            )

    account.status = "active"
    account.blocked_at, account.blocked_by, account.block_reason = None, None, None
    account.reactivated_at, account.reactivated_by = now, actor.id
    created: list[ChannelBinding] = []
    for old in chats.values():
        binding = ChannelBinding(
            organization_id=actor.organization_id,
            provider=account.provider,
            external_chat_id=old.external_chat_id,
            chat_kind="private",
            mode=account.mode,
            account_id=account.id,
            status="active",
        )
        session.add(binding)
        created.append(binding)
    await session.flush()
    for binding in created:
        emit(session, org_id=actor.organization_id, actor_id=actor.id,
             action="channel_binding.reactivated", entity_type="channel_binding",
             entity_id=binding.id, event_type="channel.binding_reactivated", aggregate_version=None,
             payload={"binding_id": str(binding.id), "account_id": str(account.id)})  # fmt: skip
        _notify(
            session, binding,
            "Tu acceso a Faltantes Farmacia fue rehabilitado. Ya puedes volver a reportar.",
        )
    emit(session, org_id=actor.organization_id, actor_id=actor.id,
         action="telegram_account.reactivated", entity_type="telegram_account",
         entity_id=account.id, event_type="telegram.account_reactivated", aggregate_version=None,
         payload={"account_id": str(account.id), "bindings_reactivated": len(created),
                  "reason": _clean_reason(reason)},
         before={"status": "blocked"}, after={"status": "active"})  # fmt: skip
    await session.flush()
    return Reactivation(account, len(created))


async def list_accounts(session: AsyncSession, actor: Actor) -> list[dict]:
    require(actor.roles, Permission.MANAGE_ACCOUNTS)
    org = actor.organization_id
    person, creator = aliased(AppUser), aliased(AppUser)
    blocker, reactivator = aliased(AppUser), aliased(AppUser)
    rows = (
        await session.execute(
            select(
                TelegramAccount,
                person,
                creator.display_name,
                blocker.display_name,
                reactivator.display_name,
            )
            .outerjoin(
                person,
                (person.id == TelegramAccount.app_user_id)
                & (person.organization_id == TelegramAccount.organization_id),
            )
            .outerjoin(
                creator,
                (creator.id == TelegramAccount.created_by)
                & (creator.organization_id == TelegramAccount.organization_id),
            )
            .outerjoin(
                blocker,
                (blocker.id == TelegramAccount.blocked_by)
                & (blocker.organization_id == TelegramAccount.organization_id),
            )
            .outerjoin(
                reactivator,
                (reactivator.id == TelegramAccount.reactivated_by)
                & (reactivator.organization_id == TelegramAccount.organization_id),
            )
            .where(TelegramAccount.organization_id == org)
            .order_by(TelegramAccount.status, TelegramAccount.created_at)
        )
    ).all()
    bindings = list(
        (
            await session.execute(
                select(ChannelBinding)
                .where(ChannelBinding.organization_id == org, ChannelBinding.account_id.is_not(None))
                .order_by(ChannelBinding.created_at)
            )
        ).scalars()
    )
    by_account: dict[uuid.UUID, list[dict]] = {}
    for b in bindings:
        by_account.setdefault(b.account_id, []).append(
            {
                "id": b.id,
                "external_chat_id": b.external_chat_id,
                "mode": b.mode,
                "status": b.status,
                "created_at": b.created_at,
            }
        )
    return [
        {
            "id": a.id,
            "external_user_id": a.external_user_id,
            "display_label": a.display_label,
            "mode": a.mode,
            "access_role": a.access_role,
            "status": a.status,
            "origin": a.origin,
            "created_from_request_id": a.created_from_request_id,
            "app_user_id": a.app_user_id,
            "app_user_name": p.display_name if p else None,
            "person_active": p.active if p else None,
            "person_available": p.available if p else None,
            "bindings": by_account.get(a.id, []),
            "created_at": a.created_at,
            "created_by_name": creator_name,
            "blocked_at": a.blocked_at,
            "blocked_by_name": blocker_name,
            "block_reason": a.block_reason,
            "reactivated_at": a.reactivated_at,
            "reactivated_by_name": reactivator_name,
        }
        for a, p, creator_name, blocker_name, reactivator_name in rows
    ]


def _request_view(
    req: TelegramAccessRequest,
    now: datetime,
    *,
    approved_by_name: str | None = None,
    rejected_by_name: str | None = None,
) -> dict:
    return {
        "id": req.id,
        "external_user_id": req.external_user_id,
        "external_chat_id": req.external_chat_id,
        "status": _request_status(req, now),
        "created_at": req.created_at,
        "expires_at": req.expires_at,
        "approved_at": req.approved_at,
        "approved_account_id": req.approved_account_id,
        "approved_by_name": approved_by_name,
        "rejected_at": req.rejected_at,
        "reject_reason": req.reject_reason,
        "rejected_by_name": rejected_by_name,
    }


async def list_requests(
    session: AsyncSession, actor: Actor, *, status: str | None = None, now: datetime | None = None
) -> list[dict]:
    require(actor.roles, Permission.MANAGE_ACCOUNTS)
    now = now or _now()
    approver, rejecter = aliased(AppUser), aliased(AppUser)
    rows = (
        await session.execute(
            select(TelegramAccessRequest, approver.display_name, rejecter.display_name)
            .outerjoin(
                approver,
                (approver.id == TelegramAccessRequest.approved_by)
                & (approver.organization_id == TelegramAccessRequest.organization_id),
            )
            .outerjoin(
                rejecter,
                (rejecter.id == TelegramAccessRequest.rejected_by)
                & (rejecter.organization_id == TelegramAccessRequest.organization_id),
            )
            .where(TelegramAccessRequest.organization_id == actor.organization_id)
            .order_by(TelegramAccessRequest.created_at.desc())
            .limit(500)
        )
    ).all()
    views = [
        _request_view(r, now, approved_by_name=approved, rejected_by_name=rejected)
        for r, approved, rejected in rows
    ]
    return [v for v in views if status is None or v["status"] == status]


async def get_request(
    session: AsyncSession, actor: Actor, request_id: uuid.UUID, *, now: datetime | None = None
) -> dict:
    require(actor.roles, Permission.MANAGE_ACCOUNTS)
    now = now or _now()
    req = (
        await session.execute(
            select(TelegramAccessRequest).where(
                TelegramAccessRequest.id == request_id,
                TelegramAccessRequest.organization_id == actor.organization_id,
            )
        )
    ).scalar_one_or_none()
    if req is None:
        raise not_found("La solicitud de acceso")
    approver = await session.get(AppUser, req.approved_by) if req.approved_by else None
    rejecter = await session.get(AppUser, req.rejected_by) if req.rejected_by else None
    return _request_view(
        req, now,
        approved_by_name=approver.display_name if approver else None,
        rejected_by_name=rejecter.display_name if rejecter else None,
    )  # fmt: skip


async def account_history(
    session: AsyncSession, actor: Actor, account_id: uuid.UUID, *, limit: int = 200
) -> list[dict]:
    """Historial de la cuenta y de las solicitudes que la aprobaron (sin códigos ni hashes)."""
    require(actor.roles, Permission.MANAGE_ACCOUNTS)
    account = (
        await session.execute(
            select(TelegramAccount).where(
                TelegramAccount.id == account_id,
                TelegramAccount.organization_id == actor.organization_id,
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise not_found("La cuenta de Telegram")
    request_ids = select(TelegramAccessRequest.id).where(
        TelegramAccessRequest.organization_id == actor.organization_id,
        TelegramAccessRequest.approved_account_id == account.id,
    )
    rows = (
        await session.execute(
            select(AuditEvent, AppUser.display_name)
            .outerjoin(
                AppUser,
                (AppUser.id == AuditEvent.actor_id)
                & (AppUser.organization_id == AuditEvent.organization_id),
            )
            .where(
                AuditEvent.organization_id == actor.organization_id,
                (
                    ((AuditEvent.entity_type == "telegram_account") & (AuditEvent.entity_id == account.id))
                    | (
                        (AuditEvent.entity_type == "telegram_access_request")
                        & (AuditEvent.entity_id.in_(request_ids))
                    )
                ),
            )
            .order_by(AuditEvent.created_at, AuditEvent.id)
            .limit(limit)
        )
    ).all()
    return [
        {
            "action": event.action,
            "actor_name": actor_name,
            "created_at": event.created_at,
            "before": event.before,
            "after": event.after,
        }
        for event, actor_name in rows
    ]
