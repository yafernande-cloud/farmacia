"""E8 — `POST /api/v1/imports`: importación de una planilla de precios (C2.5).

Sobre **congelado** en el contrato C0 (`docs/contracts/cotizador-v0.md` §E8): multipart con `file`,
`supplier_id` y `source_id`, cabecera `Idempotency-Key` obligatoria, 200 siempre con resultado
parcial. Este router no tiene lógica de importación: valida el transporte y delega en
`services.imports.run_import_request`, que reutiliza el motor de `manual_import`.

No crea artículos ni vínculos, no toca precios históricos ni los precios públicos de cadena.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, UploadFile

from app.api.deps import Session, StrongActor
from app.api.v1 import schemas as s
from app.api.v1.demand import ERRORS
from app.core.errors import DomainError
from app.db.session import transaction
from app.services.imports import MAX_BYTES, run_import_request

router = APIRouter(tags=["importación de listas de precios"])

ERRORS_IMPORT = {**ERRORS, 413: {"model": s.ErrorResponse, "description": "Archivo demasiado grande"}}

IdemKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=128,
           description="Obligatoria. Reutilizarla con el mismo archivo devuelve el resultado original; "
           "con otro archivo, 409 IDEMPOTENCY_KEY_REUSED."),
]  # fmt: skip


async def _read_limited(upload: UploadFile) -> bytes:
    """Lee el archivo en bloques y corta al superar el límite, sin cargar uno enorme en memoria."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_BYTES:
            raise DomainError(
                "PAYLOAD_TOO_LARGE", "El archivo supera los 10 MB permitidos.", status_code=413
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/imports",
    response_model=s.ImportResult,
    responses=ERRORS_IMPORT,
    summary="Importar una lista de precios (XLSX) de un proveedor",
)
async def import_file(
    session: Session,
    actor: StrongActor,
    idempotency_key: IdemKey,
    file: Annotated[UploadFile, File(description="Planilla .xlsx original del proveedor")],
    supplier_id: Annotated[uuid.UUID, Form()],
    source_id: Annotated[uuid.UUID, Form()],
):
    content = await _read_limited(file)
    async with transaction(session):
        outcome = await run_import_request(
            session,
            actor,
            source_id=source_id,
            supplier_id=supplier_id,
            idempotency_key=idempotency_key,
            file_name=file.filename or "planilla.xlsx",
            content=content,
        )
    return outcome.as_response()
