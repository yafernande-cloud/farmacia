import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response

from app.api.deps import CurrentActor, Session
from app.api.headers import parse_if_match
from app.api.v1 import schemas as s
from app.core.errors import DomainError
from app.core.permissions import Permission, require
from app.db.session import transaction
from app.domain.products import ConfirmPatch, ParsedProduct
from app.domain.units import Measure, build_measure
from app.models import DemandItem, DemandObservation, ProductAlias, QuantityEvent
from app.services import coverage as coverage_svc
from app.services import demand as demand_svc
from app.services import queries
from app.services.interpretation import suggestions_for_items
from app.services.provenance import org_kind
from app.services.quantities import apply_quantity_command

router = APIRouter(tags=["faltantes"])

ERRORS = {
    400: {"model": s.ErrorResponse, "description": "Entrada inválida"},
    401: {"model": s.ErrorResponse, "description": "Sin identidad"},
    403: {"model": s.ErrorResponse, "description": "Sin permiso"},
    404: {"model": s.ErrorResponse, "description": "No existe o no accesible"},
    409: {"model": s.ErrorResponse, "description": "Conflicto de versión o idempotencia"},
    422: {"model": s.ErrorResponse, "description": "Regla de negocio"},
}
IdemKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=128,
           description="Repetir la misma clave y contenido devuelve el resultado original."),
]  # fmt: skip


def _measure_out(m: Measure) -> dict:
    n = m.normalized()
    return {
        "label": m.label, "as_written": m.to_dict(),
        "normalized": n and {"dimension": n.dimension, "value": n.value, "unit": n.unit},
    }  # fmt: skip


def attributes_out(p: ParsedProduct) -> s.AttributesOut:
    def txt(a):
        return s.TextAttrOut(value=a.value, basis=a.basis.value) if a else None

    return s.AttributesOut(
        strengths=[s.StrengthOut(**_measure_out(x.measure), kind=x.kind, basis=x.basis.value,
                                 role_rule=x.role_rule) for x in p.strengths],
        content=s.ContentOut(**_measure_out(p.content.measure), basis=p.content.basis.value) if p.content else None,
        form=txt(p.form), packaging=txt(p.packaging), release=txt(p.release),
        unassigned=[s.MeasureOut(**_measure_out(m)) for m in p.unassigned],
        pending_validation=[s.PendingOut(**x) for x in p.pending_validation],
        identity_complete=p.identity_complete, identity=p.identity_summary(),
    )  # fmt: skip


def item_out(
    i: DemandItem,
    agg: dict,
    suggestions: list | None = None,
    *,
    coverage: dict | None = None,
    last_raw_text: str | None = None,
) -> s.DemandItemOut:
    count, last, reporters = agg.get(i.id, (0, None, []))
    attrs = attributes_out(demand_svc.parsed_of_item(i))
    # Sin el cálculo de cobertura (detalle, comandos) se declara lo mínimo verificable: lo pedido
    # sigue pendiente de compra y no se inventa lo cotizado.
    cov = coverage or {
        "coverage_quantity": "0",
        "quoted_quantity": "0",
        "purchased_quantity": "0",
        "pending_quantity": i.target_quantity,
        "pending_purchase_quantity": i.target_quantity,
        "is_pending": i.status == "active",
        "replenishment_state": {"cancelled": "cancelled", "fulfilled": "fulfilled"}.get(
            i.status, "pending"
        ),
    }
    return s.DemandItemOut(
        id=i.id, display_name=i.display_name, ingredient=i.ingredient, attributes=attrs,
        identity_complete=attrs.identity_complete, pending_validation=attrs.pending_validation,
        target_quantity=i.target_quantity, quantity_unit=i.quantity_unit,
        quantity_status="unknown" if i.target_quantity is None else "defined",
        needs_quantity=i.target_quantity is None, data_origin=i.data_origin, suggestions=suggestions or [],
        status=i.status, version=i.version,
        observation_count=count, reporters=reporters, last_observed_at=last, created_at=i.created_at,
        last_raw_text=last_raw_text,
        coverage_quantity=cov["coverage_quantity"],
        quoted_quantity=cov["quoted_quantity"],
        purchased_quantity=cov["purchased_quantity"],
        pending_quantity=cov["pending_quantity"],
        pending_purchase_quantity=cov["pending_purchase_quantity"],
        is_pending=cov["is_pending"],
        replenishment_state=cov["replenishment_state"],
    )  # fmt: skip


def obs_out(o: DemandObservation, author: str | None = None) -> s.ObservationOut:
    try:
        attrs = attributes_out(ParsedProduct.from_dict(o.parsed))
    except (KeyError, ValueError):  # observaciones anteriores al esquema v2
        attrs = None
    return s.ObservationOut(
        id=o.id, demand_item_id=o.demand_item_id, author=author, raw_text=o.raw_text,
        declared_quantity=o.declared_quantity, declared_unit=o.declared_unit,
        match_status=o.match_status, candidates=o.candidates or [], attributes=attrs, created_at=o.created_at,
        source=o.source, identity_level=o.identity_level, data_origin=o.data_origin,
        interpretation=o.interpretation or {},
    )  # fmt: skip


def event_out(e: QuantityEvent, actor: str) -> s.QuantityEventOut:
    return s.QuantityEventOut(
        id=e.id, operation=e.operation, value=e.value, unit=e.unit, before_value=e.before_value,
        before_unit=e.before_unit, after_value=e.after_value, after_unit=e.after_unit,
        version_after=e.version_after, actor=actor, reason=e.reason, created_at=e.created_at,
    )  # fmt: skip


_MESSAGES = {
    "created_item": "Registré el faltante como una necesidad nueva. La cantidad queda pendiente.",
    "attached_existing": "Ya estaba en la lista. Registré tu aviso; la cantidad no cambió.",
    "needs_review": "Registré tu aviso, pero no puedo asegurar que sea lo mismo que algo que ya está en "
    "la lista. Quedó con candidatos para que compras lo confirme; la cantidad no cambió.",
    "resolved_manual": "Observación resuelta. La cantidad no cambió.",
}
_PENDING_TEXT = {
    "unit_not_stated": lambda p: f"la unidad de {p.value} (no la supuse)",
    "role_not_stated": lambda p: f"qué indica {p.value}",
    "inferred_needs_confirmation": lambda p: (
        f"{p.attribute} (deducido por contexto, sin confirmar)"
    ),
}


def _message(outcome: str, pending: list[s.PendingOut]) -> str:
    # Solo se menciona lo AMBIGUO. Que falte la forma o el contenido es normal en un aviso informal
    # (queda en pending_validation) y nunca se convierte en una pregunta al auxiliar.
    notes = [_PENDING_TEXT[p.reason](p) for p in pending if p.reason in _PENDING_TEXT]
    return _MESSAGES[outcome] + (f" Queda por confirmar: {'; '.join(notes)}." if notes else "")


async def _result(session, actor, r: demand_svc.ObservationResult) -> s.ObservationResultOut:
    agg = await queries.aggregates(session, actor.organization_id, [r.item.id] if r.item else [])
    sug = await suggestions_for_items(session, actor.organization_id, [r.item]) if r.item else {}
    item = item_out(r.item, agg, sug.get(r.item.id)) if r.item else None
    pending = (
        item.pending_validation
        if item
        else [s.PendingOut(**x) for x in r.parsed.pending_validation]
    )
    return s.ObservationResultOut(
        outcome=r.outcome, replayed=r.replayed, message=_message(r.outcome, pending),
        pending_validation=pending, alias_created=r.alias_created,
        observation=obs_out(r.observation, actor.display_name if not r.replayed else None), item=item,
    )  # fmt: skip


@router.get("/me", response_model=s.MeOut, summary="Identidad activa")
async def me(actor: CurrentActor, session: Session):
    return s.MeOut(id=actor.id, display_name=actor.display_name, roles=actor.roles,
                   organization_id=actor.organization_id,
                   organization_kind=await org_kind(session, actor.organization_id),
                   is_superadmin=actor.is_superadmin,
                   web_access_expires_at=actor.web_access_expires_at)  # fmt: skip


@router.get("/demand-items", response_model=s.DemandItemList, responses=ERRORS,
            summary="Lista compartida de faltantes (paginada por cursor)")  # fmt: skip
async def list_demand_items(
    session: Session, actor: CurrentActor,
    status: Annotated[str, Query(pattern="^(active|cancelled|fulfilled)$")] = "active",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    pending_only: Annotated[
        bool,
        Query(description="Solo faltantes activos con saldo sin cubrir (`pending_quantity > 0`)."),
    ] = False,
):  # fmt: skip
    require(actor.roles, Permission.VIEW_LIST)
    items, agg, nxt, pending = await queries.list_items(
        session, actor, status=status, limit=limit, cursor=cursor, pending_only=pending_only
    )
    sug = await suggestions_for_items(session, actor.organization_id, items)
    ids = [i.id for i in items]
    cov = await coverage_svc.coverage_for_items(session, actor.organization_id, items)
    texts = await queries.last_texts(session, actor.organization_id, ids)
    return s.DemandItemList(
        items=[
            item_out(i, agg, sug.get(i.id), coverage=cov.get(i.id), last_raw_text=texts.get(i.id))
            for i in items
        ],
        next_cursor=nxt,
        pending_review_count=pending,
    )


@router.get("/demand-items/{item_id}", response_model=s.DemandItemDetail, responses=ERRORS,
            summary="Detalle: observaciones e historial de cantidad")  # fmt: skip
async def get_demand_item(
    item_id: uuid.UUID, session: Session, actor: CurrentActor, response: Response
):
    require(actor.roles, Permission.VIEW_LIST)
    item, agg, obs, evs = await queries.get_item_detail(session, actor, item_id)
    response.headers["ETag"] = f'"{item.version}"'
    sug = await suggestions_for_items(session, actor.organization_id, [item])
    base = item_out(item, agg, sug.get(item.id)).model_dump()
    return s.DemandItemDetail(**base, observations=[obs_out(o, a) for o, a in obs],
                              quantity_events=[event_out(e, a) for e, a in evs])  # fmt: skip


@router.post("/observations", response_model=s.ObservationResultOut, status_code=201, responses=ERRORS,
             summary="Registrar un aviso de faltante (deduplica; nunca suma cantidades)")  # fmt: skip
async def create_observation(body: s.ObservationIn, session: Session, actor: CurrentActor,
                             idempotency_key: IdemKey, response: Response):  # fmt: skip
    async with transaction(session):
        r = await demand_svc.record_observation(
            session, actor, text=body.text, idempotency_key=idempotency_key,
            declared_quantity=body.declared_quantity, declared_unit=body.declared_unit and body.declared_unit.value,
        )  # fmt: skip
        out = await _result(session, actor, r)
    if r.replayed:
        response.status_code = 200
    return out


@router.get("/observations", response_model=list[s.ObservationOut], responses=ERRORS,
            summary="Avisos pendientes de revisión")  # fmt: skip
async def pending_observations(session: Session, actor: CurrentActor,
                               limit: Annotated[int, Query(ge=1, le=200)] = 50):  # fmt: skip
    require(actor.roles, Permission.RESOLVE_OBSERVATION)
    return [
        obs_out(o, a) for o, a in await queries.list_pending_observations(session, actor, limit)
    ]


@router.post("/observations/{observation_id}/resolve", response_model=s.ObservationResultOut,
             responses=ERRORS, summary="Resolver una coincidencia dudosa (nunca une productos incompatibles)")  # fmt: skip
async def resolve_observation(
    observation_id: uuid.UUID, body: s.ResolveIn, session: Session, actor: CurrentActor
):
    async with transaction(session):
        r = await demand_svc.resolve_observation(
            session,
            actor,
            observation_id,
            demand_item_id=body.demand_item_id,
            create_new=body.create_new,
            apply_observed_attributes=body.apply_observed_attributes,
            remember_alias=body.remember_alias,
        )
        return await _result(session, actor, r)


def _parse_if_match(v: str | None) -> int:
    return parse_if_match(v, what="la necesidad")


@router.post("/demand-items/{item_id}/quantity-commands", response_model=s.QuantityResultOut, responses=ERRORS,
             summary="SET / ADD / CLEAR con If-Match e Idempotency-Key")  # fmt: skip
async def quantity_command(
    item_id: uuid.UUID, body: s.QuantityCommandIn, session: Session, actor: CurrentActor,
    idempotency_key: IdemKey, response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match", description='Versión actual, p. ej. "7"')] = None,
):  # fmt: skip
    version = _parse_if_match(if_match)
    async with transaction(session):
        r = await apply_quantity_command(
            session, actor, item_id, operation=body.operation, value=body.value, unit=body.unit,
            reason=body.reason, idempotency_key=idempotency_key, expected_version=version,
        )  # fmt: skip
        agg = await queries.aggregates(session, actor.organization_id, [r.item.id])
        out = s.QuantityResultOut(
            replayed=r.replayed, previous_quantity=r.event.before_value, previous_unit=r.event.before_unit,
            item=item_out(r.item, agg), event=event_out(r.event, actor.display_name),
        )  # fmt: skip
    response.headers["ETag"] = f'"{r.item.version}"'
    return out


def _measure_in(m: s.MeasureIn) -> Measure:
    try:
        return build_measure(m.value, m.unit)
    except ValueError as e:
        raise DomainError("INVALID_UNIT", str(e), status_code=400) from None


@router.post("/demand-items/{item_id}/resolve", response_model=s.DemandItemOut, responses=ERRORS,
             summary="Confirmar unidad, forma, contenido… de una necesidad (If-Match)")  # fmt: skip
async def confirm_attributes(
    item_id: uuid.UUID, body: s.ConfirmAttributesIn, session: Session, actor: CurrentActor, response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match", description='Versión actual, p. ej. "7"')] = None,
):  # fmt: skip
    version = _parse_if_match(if_match)
    patch = ConfirmPatch(
        ingredient=body.ingredient,
        strengths=[_measure_in(m) for m in body.strengths] if body.strengths is not None else None,
        form=body.form, release=body.release, packaging=body.packaging,
        content=_measure_in(body.content) if body.content else None,
        confirm_inferred=set(body.confirm_inferred),
    )  # fmt: skip
    async with transaction(session):
        item = await demand_svc.confirm_item_attributes(
            session,
            actor,
            item_id,
            expected_version=version,
            patch=patch,
            remember_alias=body.remember_alias,
        )
        agg = await queries.aggregates(session, actor.organization_id, [item.id])
        out = item_out(item, agg)
    response.headers["ETag"] = f'"{item.version}"'
    return out


def _alias_out(a: ProductAlias) -> s.AliasOut:
    return s.AliasOut(id=a.id, ingredient=a.ingredient, source_value=a.source_value,
                      assignment=a.assignment, status=a.status, created_at=a.created_at)  # fmt: skip


@router.get("/product-aliases", response_model=list[s.AliasOut], responses=ERRORS, tags=["memoria verificada"],
            summary="Aliases confirmados por una persona (revisables)")  # fmt: skip
async def product_aliases(session: Session, actor: CurrentActor):
    return [_alias_out(a) for a in await demand_svc.list_aliases(session, actor)]


@router.post("/product-aliases/{alias_id}/deactivate", response_model=s.AliasOut, responses=ERRORS,
             tags=["memoria verificada"], summary="Desactivar un alias")  # fmt: skip
async def deactivate_alias(alias_id: uuid.UUID, session: Session, actor: CurrentActor):
    async with transaction(session):
        return _alias_out(await demand_svc.deactivate_alias(session, actor, alias_id))
