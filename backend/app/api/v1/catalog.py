import uuid
from datetime import datetime

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import CurrentActor, Session
from app.api.v1.demand import ERRORS
from app.core.errors import DomainError, not_found
from app.core.permissions import Permission, require
from app.db.session import transaction
from app.models import Attachment
from app.services import catalog as catalog_svc
from app.services import demand as demand_svc
from app.services.storage import PostgresPrivateStorage

router = APIRouter(tags=["catálogo verificado y canal"])


class PharmaIdentityIn(BaseModel):
    text: str = Field(min_length=3, max_length=200, examples=["vitamina d 50000 UI cápsulas"])


class CommercialReferenceIn(BaseModel):
    brand: str = Field(min_length=1, max_length=120, examples=["Migranol"])
    laboratory: str | None = Field(
        default=None, max_length=200, examples=["Laboratorio Bagó de Chile S.A."]
    )
    pharma_identity_id: uuid.UUID | None = Field(
        default=None, description="null = principio activo aún desconocido"
    )
    form: str | None = None
    content_text: str | None = Field(default=None, examples=["10 comprimidos"])
    gtin: str | None = Field(default=None, max_length=14)
    sanitary_registration: str | None = None


class ExpressionAliasIn(BaseModel):
    phrase: str = Field(min_length=1, max_length=100, examples=["la D"])
    ingredient: str = Field(min_length=1, max_length=200, examples=["vitamina d"])


@router.post(
    "/pharma-identities",
    status_code=201,
    responses=ERRORS,
    summary="Registrar una identidad farmacéutica verificada",
)
async def create_pharma_identity(body: PharmaIdentityIn, session: Session, actor: CurrentActor):
    async with transaction(session):
        r = await catalog_svc.create_pharma_identity(session, actor, body.text)
        return {
            "id": r.id,
            "label": r.label,
            "ingredient": r.ingredient,
            "attributes": r.attributes,
        }


@router.post("/commercial-references", status_code=201, responses=ERRORS,
             summary="Registrar una referencia comercial verificada (marca + laboratorio → identidad)")  # fmt: skip
async def create_commercial_reference(
    body: CommercialReferenceIn, session: Session, actor: CurrentActor
):
    async with transaction(session):
        r = await catalog_svc.create_commercial_reference(session, actor, **body.model_dump())
        return {"id": r.id, "label": r.label, "brand_key": r.brand_key, "laboratory": r.laboratory,
                "pharma_identity_id": r.pharma_identity_id}  # fmt: skip


@router.get("/catalog", responses=ERRORS, summary="Catálogo verificado de la farmacia")
async def get_catalog(session: Session, actor: CurrentActor):
    ids, refs = await catalog_svc.list_catalog(session, actor)
    return {"pharma_identities": [{"id": i.id, "label": i.label, "ingredient": i.ingredient} for i in ids],
            "commercial_references": [{"id": r.id, "label": r.label, "brand_key": r.brand_key, "laboratory": r.laboratory,
                                       "pharma_identity_id": r.pharma_identity_id} for r in refs]}  # fmt: skip


@router.post(
    "/product-aliases",
    status_code=201,
    responses=ERRORS,
    summary="Crear un alias de expresión («la D» = «vitamina d»)",
)
async def create_alias(body: ExpressionAliasIn, session: Session, actor: CurrentActor):
    async with transaction(session):
        await demand_svc.create_expression_alias(session, actor, body.phrase, body.ingredient)
    return {"ok": True}


class AttachmentOut(BaseModel):
    id: uuid.UUID
    status: str
    extraction_mode: str | None
    vision_provider: str | None
    uncertainties: list[str]
    caption: str | None
    mime_type: str | None
    data_origin: str
    extraction_provenance: str | None = None
    created_at: datetime
    duplicate_of_id: uuid.UUID | None = None


@router.get(
    "/attachments",
    response_model=list[AttachmentOut],
    responses=ERRORS,
    summary="Fotos recibidas y su estado de procesamiento",
)
async def list_attachments(session: Session, actor: CurrentActor, status: str | None = None):
    require(actor.roles, Permission.CONFIRM_ATTRIBUTES)
    q = select(Attachment).where(Attachment.organization_id == actor.organization_id)
    if status:
        q = q.where(Attachment.status == status)
    rows = (
        (await session.execute(q.order_by(Attachment.created_at.desc()).limit(100))).scalars().all()
    )
    return [AttachmentOut.model_validate(r, from_attributes=True) for r in rows]


@router.get(
    "/attachments/{attachment_id}/content",
    responses={**ERRORS, 200: {"description": "Fotografía original (JPEG, PNG o WebP)"}},
    response_class=Response,
    summary="Ver la fotografía original de un aviso",
)
async def attachment_content(attachment_id: uuid.UUID, session: Session, actor: CurrentActor):
    """Sirve la imagen privada guardada, solo a la misma organización.

    La lectura de la lista (VIEW_LIST) ya muestra el reporte; ver su fotografía no amplía datos
    entre farmacias. El tipo se toma del archivo validado por firma, nunca del cliente.
    """
    require(actor.roles, Permission.VIEW_LIST)
    att = await session.get(Attachment, attachment_id)
    if att is None or att.organization_id != actor.organization_id:
        raise not_found("La fotografía")
    if not att.storage_key or not att.mime_type:
        raise DomainError(
            "PHOTO_NOT_AVAILABLE", "La fotografía no está disponible.", status_code=404
        )
    try:
        data = await PostgresPrivateStorage().get(att.storage_key, session=session)
    except (DomainError, SQLAlchemyError) as e:
        raise DomainError(
            "PHOTO_NOT_AVAILABLE", "La fotografía no está disponible.", status_code=404
        ) from e
    return Response(
        content=data,
        media_type=att.mime_type,
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/channel/status",
    responses=ERRORS,
    summary="Canal: quién reporta, pendientes de atribución, fotos por estado",
)
async def channel_status(session: Session, actor: CurrentActor):
    return await catalog_svc.channel_status(session, actor)


__all__ = ["not_found"]
