"""Catálogo verificado mínimo: identidades farmacéuticas y referencias comerciales (embrión de H3).

Las crea una persona autorizada (compras): al hacerlo, lo que declara queda `confirmed`. Sirven para
PROPONER candidatos; nunca confirman un aviso por sí solas.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, not_found
from app.core.permissions import Permission, require
from app.domain.products import Basis, normalize_name, parse_shortage_text
from app.domain.units import Measure
from app.models import (
    ActorSession,
    Attachment,
    ChannelBinding,
    ChannelMessage,
    CommercialReference,
    PharmaIdentity,
)
from app.services.actors import Actor
from app.services.events import emit
from app.services.provenance import org_kind, origin_for


async def create_pharma_identity(
    session: AsyncSession, actor: Actor, text: str, source: str = "manual"
) -> PharmaIdentity:
    """«vitamina d 50000 UI cápsulas» → identidad verificada (principio + potencia [+ forma])."""
    require(actor.roles, Permission.CONFIRM_ATTRIBUTES)
    p = parse_shortage_text(text)
    if (
        p.name_anchor
        or not p.strengths
        or p.unassigned
        or any(s.basis is Basis.INFERRED for s in p.strengths)
    ):
        raise DomainError(
            "INCOMPLETE_IDENTITY",
            "Una identidad verificada necesita principio activo y potencia con unidad explícita.",
        )
    attrs = {
        "schema_version": 2, "ingredient": p.ingredient,
        "strengths": [{"measure": s.measure.to_dict(), "kind": s.kind, "basis": "confirmed", "role_rule": "confirmed",
                       "origin": "confirmation"} for s in p.strengths],
        "unassigned": [],
    }  # fmt: skip
    if p.form:
        attrs["form"] = {"value": p.form.value, "basis": "confirmed", "origin": "confirmation"}
    if p.release:
        attrs["release"] = {
            "value": p.release.value,
            "basis": "confirmed",
            "origin": "confirmation",
        }
    label = " ".join(
        [
            p.ingredient.title(),
            *[s.measure.label for s in p.strengths],
            *([p.form.value] if p.form else []),
        ]
    )
    row = PharmaIdentity(organization_id=actor.organization_id, label=label, ingredient=p.ingredient,
                         attributes=attrs, source=source, created_by=actor.id,
                         data_origin=origin_for(await org_kind(session, actor.organization_id)))  # fmt: skip
    session.add(row)
    await session.flush()
    emit(session, org_id=actor.organization_id, actor_id=actor.id, action="pharma_identity.created",
         entity_type="pharma_identity", entity_id=row.id, event_type="catalog.pharma_identity_created",
         aggregate_version=None, payload={"id": str(row.id)}, after={"label": label})  # fmt: skip
    return row


async def create_commercial_reference(
    session: AsyncSession, actor: Actor, *, brand: str, laboratory: str | None = None,
    pharma_identity_id: uuid.UUID | None = None, form: str | None = None, content_text: str | None = None,
    gtin: str | None = None, sanitary_registration: str | None = None, source: str = "manual",
) -> CommercialReference:  # fmt: skip
    require(actor.roles, Permission.CONFIRM_ATTRIBUTES)
    org = actor.organization_id
    key = normalize_name(brand)
    if not key:
        raise DomainError("INVALID_BRAND", "Falta el nombre comercial.", status_code=400)
    if pharma_identity_id is not None:
        ok = (await session.execute(select(PharmaIdentity.id).where(PharmaIdentity.id == pharma_identity_id,
                                                                    PharmaIdentity.organization_id == org))).first()  # fmt: skip
        if not ok:
            raise not_found("La identidad farmacéutica")
    content = None
    if content_text:
        c = parse_shortage_text(f"x {content_text}", allow_no_product=True).content
        content = c.measure.to_dict() if c else None
    row = CommercialReference(organization_id=org, label=f"{brand.strip()}" + (f" · {laboratory.strip()}" if laboratory else ""),
                              brand_key=key, laboratory=laboratory, gtin=gtin, sanitary_registration=sanitary_registration,
                              form=form, content=content, pharma_identity_id=pharma_identity_id, source=source,
                              created_by=actor.id, data_origin=origin_for(await org_kind(session, org)))  # fmt: skip
    session.add(row)
    await session.flush()
    emit(session, org_id=org, actor_id=actor.id, action="commercial_reference.created", entity_type="commercial_reference",
         entity_id=row.id, event_type="catalog.commercial_reference_created", aggregate_version=None,
         payload={"id": str(row.id)}, after={"label": row.label})  # fmt: skip
    return row


async def list_catalog(session: AsyncSession, actor: Actor):
    require(actor.roles, Permission.CONFIRM_ATTRIBUTES)
    org = actor.organization_id
    ids = (await session.execute(select(PharmaIdentity).where(PharmaIdentity.organization_id == org, PharmaIdentity.status == "active").order_by(PharmaIdentity.label))).scalars().all()  # fmt: skip
    refs = (await session.execute(select(CommercialReference).where(CommercialReference.organization_id == org, CommercialReference.status == "active").order_by(CommercialReference.label))).scalars().all()  # fmt: skip
    return list(ids), list(refs)


async def channel_status(session: AsyncSession, actor: Actor) -> dict:
    """Quién está reportando, mensajes sin atribuir y fotos por estado (solo lectura, compras)."""
    require(actor.roles, Permission.CONFIRM_ATTRIBUTES)
    from sqlalchemy import func

    org = actor.organization_id
    binds = (
        (await session.execute(select(ChannelBinding).where(ChannelBinding.organization_id == org)))
        .scalars()
        .all()
    )
    out = []
    for b in binds:
        act = (await session.execute(select(ActorSession).where(ActorSession.channel_binding_id == b.id, ActorSession.ended_at.is_(None)))).scalar_one_or_none()  # fmt: skip
        pend = (await session.execute(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.channel_binding_id == b.id, ChannelMessage.attribution_status == "pending_attribution"))).scalar_one()  # fmt: skip
        out.append({"binding_id": str(b.id), "provider": b.provider, "chat_kind": b.chat_kind, "status": b.status,
                    "active_auxiliary_id": str(act.auxiliary_id) if act else None,
                    "session_expires_at": act.expires_at.isoformat() if act else None,
                    "identity_level": act.identity_level if act else None, "pending_messages": pend})  # fmt: skip
    photos = dict((await session.execute(select(Attachment.status, func.count()).where(Attachment.organization_id == org).group_by(Attachment.status))).all())  # fmt: skip
    return {"bindings": out, "photos_by_status": photos}


__all__ = ["Measure"]
