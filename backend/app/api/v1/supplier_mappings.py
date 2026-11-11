"""Vínculo explícito de un producto en vivo con un artículo del catálogo interno.

Solo `admin` + `StrongActor` (D-130). El vínculo no crea artículos ni se deduce por nombres: la
persona elige un artículo existente y confirma. Un reintento con el mismo destino es idempotente;
uno con otro destino no sobrescribe y responde 409.
"""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Session, StrongActor
from app.api.v1.demand import ERRORS
from app.db.session import transaction
from app.services import supplier_mappings as svc

router = APIRouter(tags=["motor de proveedores"])


class SupplierProductMappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: uuid.UUID
    external_id: str = Field(min_length=1, max_length=120)
    external_name: str | None = Field(default=None, max_length=300)
    trade_item_id: uuid.UUID


class SupplierProductMappingPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trade_item_id: uuid.UUID
    version: int | None = Field(
        default=None,
        description="Versión leída. Si se envía y no coincide, responde 409.",
    )


class SupplierProductMappingOut(BaseModel):
    mapping_id: uuid.UUID
    source_id: uuid.UUID
    supplier_id: uuid.UUID
    external_id: str
    external_name: str | None
    trade_item_id: uuid.UUID
    origin: Literal["explicit", "confirmed"]
    version: int = 1
    # `False` cuando el vínculo ya existía con el mismo artículo: no se duplicó nada.
    created: bool
    created_at: datetime
    # Reubicación de lo aprendido desde la oferta: conteos, nunca identificadores internos.
    adopted_preferences: int = 0
    adoption_conflicts: int = 0
    adoption_skipped: str | None = None


def _mapping_payload(mapping, *, created: bool, adoption) -> dict:
    return {
        "mapping_id": mapping.id,
        "source_id": mapping.source_id,
        "supplier_id": mapping.supplier_id,
        "external_id": mapping.external_id,
        "external_name": mapping.external_name,
        "trade_item_id": mapping.trade_item_id,
        "origin": mapping.origin,
        "version": mapping.version,
        "created": created,
        "created_at": mapping.created_at,
        "adopted_preferences": len(adoption.preference_ids) if adoption is not None else 0,
        "adoption_conflicts": len(adoption.conflicts) if adoption is not None else 0,
        "adoption_skipped": adoption.skipped if adoption is not None else None,
    }


@router.post(
    "/supplier-product-mappings",
    response_model=SupplierProductMappingOut,
    status_code=201,
    responses={
        **ERRORS,
        200: {"description": "El vínculo ya existía con el mismo artículo"},
        409: {"description": "El producto ya está vinculado a otro artículo"},
    },
    summary="Vincular explícitamente un producto de una fuente con un artículo del catálogo",
)
async def create_supplier_product_mapping(
    body: SupplierProductMappingIn,
    session: Session,
    actor: StrongActor,
    response: Response,
):
    async with transaction(session):
        mapping, created, adoption = await svc.create_mapping(
            session,
            actor,
            source_id=body.source_id,
            external_id=body.external_id,
            external_name=body.external_name,
            trade_item_id=body.trade_item_id,
        )
        await session.refresh(mapping)
        payload = _mapping_payload(mapping, created=created, adoption=adoption)
    if not created:
        response.status_code = 200
    return payload


@router.put(
    "/supplier-product-mappings/{mapping_id}",
    response_model=SupplierProductMappingOut,
    responses={
        **ERRORS,
        409: {"description": "El vínculo cambió de versión o el artículo está inactivo"},
    },
    summary="Corregir el artículo de un vínculo existente, conservando la trazabilidad",
)
async def relink_supplier_product_mapping(
    mapping_id: uuid.UUID,
    body: SupplierProductMappingPatchIn,
    session: Session,
    actor: StrongActor,
):
    async with transaction(session):
        mapping, changed, adoption = await svc.relink_mapping(
            session,
            actor,
            mapping_id=mapping_id,
            trade_item_id=body.trade_item_id,
            expected_version=body.version,
        )
        await session.refresh(mapping)
        return _mapping_payload(mapping, created=changed, adoption=adoption)
