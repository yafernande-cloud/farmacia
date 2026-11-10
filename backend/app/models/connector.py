"""Perfiles de conexión de proveedores (motor declarativo de conectores).

Un perfil describe **cómo consultar** una fuente de proveedor, no qué proveedor es. La estrategia
(`algolia`, `rest_json`, …) interpreta `config` y `field_map`; no hay código por proveedor.

El estado visible (`not_configured`, `connected`, `error`, `requires_intervention`) **no se guarda**:
se deriva de `enabled`, `last_success_at` y `last_error_kind`. Así una búsqueda fallida no reescribe
la configuración aprobada ni convierte un error transitorio en una pérdida de configuración.

`config` y `field_map` no contienen secretos: solo nombres de variables de entorno o referencias.
La búsqueda en vivo no persiste ofertas; este perfil es lo único que se conserva entre consultas.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

UUID_PK = dict(primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid())
_NOW = dict(server_default=func.now())

ORIGINS = "'simulated','captured','ai_derived','unclassified'"
MECHANISMS = "'algolia','rest_json','html_form','html_embedded_json','manual_capture','browser_required','unsupported'"
ERROR_KINDS = "'transient','intervention','configuration'"


class SupplierConnectorProfile(Base):
    """Configuración versionada de una fuente de proveedor.

    Una fila por fuente. `version` controla la edición optimista; una búsqueda no la modifica.
    Los secretos viven fuera de esta tabla (variable de entorno referenciada por nombre) y nunca
    se devuelven por la API.
    """

    __tablename__ = "supplier_connector_profile"
    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("organization_id", "source_id"),
        ForeignKeyConstraint(["organization_id"], ["organization.id"]),
        ForeignKeyConstraint(
            ["supplier_id", "organization_id"],
            ["supplier.id", "supplier.organization_id"],
        ),
        ForeignKeyConstraint(
            ["source_id", "supplier_id", "organization_id"],
            ["source.id", "source.supplier_id", "source.organization_id"],
        ),
        ForeignKeyConstraint(
            ["created_by", "organization_id"],
            ["app_user.id", "app_user.organization_id"],
        ),
        CheckConstraint(f"mechanism IN ({MECHANISMS})", name="mecanismo_valido"),
        CheckConstraint(
            f"last_error_kind IS NULL OR last_error_kind IN ({ERROR_KINDS})",
            name="tipo_error_valido",
        ),
        # Un error siempre dice de qué tipo es; un perfil sano no conserva un tipo huérfano.
        CheckConstraint(
            "(last_error IS NULL) = (last_error_kind IS NULL)",
            name="error_coherente",
        ),
        CheckConstraint("jsonb_typeof(config) = 'object'", name="config_objeto"),
        CheckConstraint("jsonb_typeof(field_map) = 'object'", name="mapeo_objeto"),
        CheckConstraint("version >= 1", name="version_valida"),
        CheckConstraint(f"data_origin IN ({ORIGINS})", name="origen_dato_valido"),
        Index(
            "ix_supplier_connector_profile_supplier",
            "organization_id",
            "supplier_id",
        ),
    )  # fmt: skip
    id: Mapped[uuid.UUID] = mapped_column(**UUID_PK)
    organization_id: Mapped[uuid.UUID] = mapped_column()
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    source_id: Mapped[uuid.UUID] = mapped_column()
    mechanism: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    field_map: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Mensaje acotado y sin secretos: código de motivo, nunca el cuerpo del proveedor.
    last_error: Mapped[str | None] = mapped_column(String(300))
    last_error_kind: Mapped[str | None] = mapped_column(String(16))
    # Huella de la estructura observada (no de los valores). Se usa para detectar cambios.
    schema_fingerprint: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    data_origin: Mapped[str] = mapped_column(String(12), server_default="unclassified")
    created_by: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
