"""Matriz de permisos (HLD §17.1). Se verifica en los servicios, no solo en la API."""

from enum import StrEnum

from app.core.errors import DomainError


class Role(StrEnum):
    AUXILIARY = "auxiliary"
    PURCHASING = "purchasing"
    APPROVER = "approver"
    ADMIN = "admin"


class Permission(StrEnum):
    VIEW_LIST = "view_list"
    REPORT_SHORTAGE = "report_shortage"
    APPLY_QUANTITY = "apply_quantity"
    RESOLVE_OBSERVATION = "resolve_observation"
    MANAGE_STAFF = (
        "manage_staff"  # personal: crear, renombrar, activar, disponibilidad (rol administrador)
    )
    MANAGE_ACCOUNTS = (
        "manage_accounts"  # aprobar solicitudes y bloquear/rehabilitar cuentas de Telegram (rol admin)
    )
    MANAGE_IDENTITIES = (
        "manage_identities"  # vincular/desvincular la identidad web (sub de Supabase) de una persona
    )
    CONFIRM_ATTRIBUTES = "confirm_attributes"  # confirmar unidad/forma/contenido; gestionar alias


# Por defecto el auxiliar propone (observación con cantidad declarada), la encargada aplica.
_GRANTS: dict[Role, set[Permission]] = {
    Role.AUXILIARY: {Permission.VIEW_LIST, Permission.REPORT_SHORTAGE},
    Role.PURCHASING: {
        Permission.VIEW_LIST,
        Permission.REPORT_SHORTAGE,
        Permission.APPLY_QUANTITY,
        Permission.RESOLVE_OBSERVATION,
        Permission.CONFIRM_ATTRIBUTES,
    },
    Role.APPROVER: {Permission.VIEW_LIST},
    Role.ADMIN: {
        Permission.VIEW_LIST,
        Permission.MANAGE_STAFF,
        Permission.MANAGE_ACCOUNTS,
        Permission.MANAGE_IDENTITIES,
    },  # no implica permisos de compras
}


def require(roles: list[str], permission: Permission) -> None:
    granted = set()
    for r in roles:
        try:
            granted |= _GRANTS[Role(r)]
        except (ValueError, KeyError):
            continue
    if permission not in granted:
        raise DomainError("FORBIDDEN", "No tienes permiso para esta acción.", status_code=403)


def can_report(roles: list[str]) -> bool:
    """¿Alguno de sus roles permite reportar faltantes? (auxiliar o compras)."""
    try:
        require(roles, Permission.REPORT_SHORTAGE)
        return True
    except DomainError:
        return False


def require_admin(roles: list[str]) -> None:
    """Chequeo de rol del cotizador (C0 §6): `admin`, sin crear permisos nuevos."""
    if Role.ADMIN.value not in (roles or []):
        raise DomainError(
            "FORBIDDEN", "Esta operación es solo para la administración.", status_code=403
        )
