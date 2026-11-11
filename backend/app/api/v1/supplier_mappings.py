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


class SupplierProductMappingOut(BaseModel):
    mapping_id: uuid.UUID
    source_id: uuid.UUID
    supplier_id: uuid.UUID
    external_id: str
    external_name: str | None
    trade_item_id: uuid.UUID
    origin: Literal["explicit", "confirmed"]
    # `False` cuando el vínculo ya existía con el mismo artículo: no se duplicó nada.
    created: bool
    created_at: datetime


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
        mapping, created = await svc.create_mapping(
            session,
            actor,
            source_id=body.source_id,
            external_id=body.external_id,
            external_name=body.external_name,
            trade_item_id=body.trade_item_id,
        )
        await session.refresh(mapping)
        payload = {
            "mapping_id": mapping.id,
            "source_id": mapping.source_id,
            "supplier_id": mapping.supplier_id,
            "external_id": mapping.external_id,
            "external_name": mapping.external_name,
            "trade_item_id": mapping.trade_item_id,
            "origin": mapping.origin,
            "created": created,
            "created_at": mapping.created_at,
        }
    if not created:
        response.status_code = 200
    return payload
