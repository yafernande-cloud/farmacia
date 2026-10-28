"""Errores de negocio con el contrato de error del HLD §14.3."""

from contextvars import ContextVar
from typing import Any

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


class DomainError(Exception):
    status_code = 422

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code

    def to_body(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "correlation_id": correlation_id_var.get(),
                "details": self.details,
            }
        }


def not_found(what: str) -> DomainError:
    return DomainError("NOT_FOUND", f"{what} no existe o no es accesible.", status_code=404)
