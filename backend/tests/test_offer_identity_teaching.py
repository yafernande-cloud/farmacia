"""«Enseñar a la IA» desde una oferta en vivo: identidad técnica y reubicación verificada.

PostgreSQL real y proveedor **simulado**: ninguna prueba llama a OpenAI.

Cubre el caso de aceptación (Ibuprofeno 400 mg x 20: Andes AND-002 Cordillera, Andes AND-031
ASCEND y FarmaLoop 15391 ASCEND): enseñar desde la oferta que la persona ya está viendo, sin
elegir ni vincular nada; una sola identidad técnica `offer:<fuente>:<SKU>`; el contexto comercial
real llega al modelo; la preferencia ordena la búsqueda siguiente; y la vinculación posterior
reubica lo aprendido con su cronología, sin duplicar efectos ni mezclar alcances.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.v1.observations import get_observation_model
from app.core.config import get_settings
from app.db.session import get_session
from app.domain.products import normalize_name
from app.interpretation.openai_observations import RawCompletion
from app.main import create_app
from app.models import (
    AppUser,
    AuditEvent,
    CommercialObservation,
    CommercialPreference,
    ObservationInterpretation,
    Organization,
    OutboxEvent,
    Source,
    Supplier,
    SupplierProductMapping,
    TradeItem,
)
from app.services import offer_identities as offer_svc
from app.services import preferences as pref_svc
from app.services import quotes as quote_svc
from app.services.actors import load_actor
from app.services.connectors.service import _apply_order_keys, _resolve_trade_item_ids
from app.services.supplier_mappings import create_mapping, relink_mapping

pytestmark = pytest.mark.db

CORDILLERA = "Laboratorio Cordillera"
ASCEND = "ASCEND"
MARCA = "Marca Andes Ficticia"


def _attrs() -> dict:
    return {"schema_version": 2, "unassigned": [], "ingredient": "ibuprofeno"}


@pytest.fixture
async def ofertas(sessions):
    ids: dict = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia ofertas vivas", kind="simulation")
        other = Organization(name="Otra farmacia ofertas", kind="simulation")
        s.add_all([org, other])
        await s.flush()
        ids["org"], ids["other_org"] = org.id, other.id

        pao = AppUser(organization_id=org.id, display_name="Pao", roles=["admin"])
        yarella = AppUser(organization_id=org.id, display_name="Yarella", roles=["admin"])
        outsider = AppUser(organization_id=other.id, display_name="Externa", roles=["admin"])
        s.add_all([pao, yarella, outsider])
        await s.flush()
        ids.update(pao=pao.id, yarella=yarella.id, outsider=outsider.id)

        suppliers = {
            "andes": Supplier(organization_id=org.id, name="Andes", data_origin="simulated"),
            "farmaloop": Supplier(
                organization_id=org.id, name="FarmaLoop", data_origin="simulated"
            ),
        }
        s.add_all(suppliers.values())
        await s.flush()

        sources = {}
        for key, supplier in suppliers.items():
            source = Source(
                organization_id=org.id,
                kind="supplier",
                supplier_id=supplier.id,
                name=f"Lista {key}",
                mode="manual_import",
                capabilities=["search", "price", "stock"],
                data_origin="simulated",
            )
            s.add(source)
            sources[key] = source
        await s.flush()
        ids["sources"] = {key: source.id for key, source in sources.items()}

        canonical = TradeItem(
            organization_id=org.id,
            display_name="Ibuprofeno 400 mg x 20 comprimidos",
            identity_key="fixture-oferta-ibuprofeno-400-20",
            ingredient="ibuprofeno",
            laboratory=CORDILLERA,
            attributes=_attrs(),
            data_origin="simulated",
        )
        other_item = TradeItem(
            organization_id=org.id,
            display_name="Ibuprofeno 600 mg x 20 comprimidos",
            identity_key="fixture-oferta-ibuprofeno-600-20",
            ingredient="ibuprofeno",
            laboratory="Laboratorio Andino",
            attributes=_attrs(),
            data_origin="simulated",
        )
        s.add_all([canonical, other_item])
        await s.flush()
        ids["canonical"], ids["other_item"] = canonical.id, other_item.id
    return ids


class StubModel:
    """Doble del proveedor: respuestas en cola y prompts capturados, sin red ni cuota."""

    def __init__(self):
        self.responses: list = []
        self.calls = 0
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []

    async def complete(self, *, system: str, user: str, json_schema: dict) -> RawCompletion:
        self.calls += 1
        self.prompts.append(user)
        self.system_prompts.append(system)
        item = self.responses.pop(0)
        return RawCompletion(
            text=item if isinstance(item, str) else json.dumps(item),
            model="fake-observations",
            input_tokens=100,
            output_tokens=40,
            latency_ms=5,
        )


@pytest.fixture
def model() -> StubModel:
    return StubModel()


@pytest.fixture
async def api(sessions, ofertas, model, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    app = create_app()

    async def _session():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_observation_model] = lambda: model
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.app = app
        c.ids = ofertas
        c.sessions = sessions
        c.model = model
        yield c
    get_settings.cache_clear()


def H(api, who="pao") -> dict[str, str]:
    return {"X-Demo-User-Id": str(api.ids[who])}


def _op(kind="laboratory", label=ASCEND) -> dict:
    return {
        "subject_kind": kind,
        "subject_label": label,
        "effect": "prefer",
        "tolerance": {"kind": "none", "amount": None, "percent": None, "currency": None},
        "confidence": 0.99,
        "price_reference": None,
    }


def _response(operations=(), retire=()) -> dict:
    return {
        "operations": list(operations),
        "article_operations": [],
        "retire_subject_kinds": list(retire),
        "ambiguity": None,
    }


def _offer_body(
    api,
    *,
    source="farmaloop",
    external_id="15391",
    name="Ibuprofeno 400 mg x 20 comprimidos",
    laboratory=ASCEND,
    brand=None,
    price="990",
    currency="CLP",
    ean=None,
) -> dict:
    return {
        "source_id": str(api.ids["sources"][source]),
        "external_id": external_id,
        "name": name,
        "laboratory": laboratory,
        "brand": brand,
        "presentation": "Caja 20 comprimidos",
        "price": {"amount": price, "currency": currency} if price is not None else None,
        "price_basis": "per_commercial_unit",
        "sale_unit": "box",
        "stock_status": "in_stock",
        "stock_quantity": "10",
        "stock_unit": "box",
        "ean": ean,
    }


async def _teach(
    api,
    *,
    text: str = "Prefiero ASCEND",
    response: dict | None = None,
    offer: dict | None = None,
    trade_item_id: str | None = None,
    who: str = "pao",
    key: str | None = None,
) -> tuple[dict, dict]:
    """Guarda la observación y la interpreta, como el botón «Guardar y enseñar»."""
    api.model.responses.append(response or _response([_op()]))
    headers = H(api, who)
    if key:
        headers["Idempotency-Key"] = key
    body: dict = {"text": text}
    if offer is not None:
        body["offer"] = offer
    if trade_item_id is not None:
        body["trade_item_id"] = str(trade_item_id)
    created = await api.post("/api/v1/commercial-observations", json=body, headers=headers)
    assert created.status_code == 201, created.text
    observation = created.json()
    interpreted = await api.post(
        f"/api/v1/commercial-observations/{observation['observation_id']}/interpretations",
        headers=H(api, who),
    )
    assert interpreted.status_code == 200, interpreted.text
    return observation, interpreted.json()["observation"]


async def _run(sessions, user_id, fn, *args, **kwargs):
    """Ejecuta un servicio en su propia sesión/transacción, como la API."""
    async with sessions() as s:
        actor = await load_actor(s, user_id)
        try:
            result = await fn(s, actor, *args, **kwargs)
            await s.commit()
            return result
        except BaseException:
            await s.rollback()
            raise


def _identity_offer(api, *, brand=None) -> offer_svc.OfferViewContext:
    return offer_svc.OfferViewContext(
        source_id=api.ids["sources"]["farmaloop"],
        external_id="15391",
        name="Ibuprofeno 400 mg x 20 comprimidos",
        laboratory=ASCEND,
        brand=brand,
        presentation="Caja 20 comprimidos",
        price_amount=Decimal("990"),
        currency="CLP",
        price_basis="per_commercial_unit",
        sale_unit="box",
        stock_status="in_stock",
        stock_quantity=Decimal("10"),
        stock_unit="box",
    )


async def _identity(sessions, api) -> uuid.UUID:
    """Crea solo la identidad técnica del SKU, sin enseñar nada todavía."""
    async with sessions() as s:
        actor = await load_actor(s, api.ids["pao"])
        try:
            item, _ = await offer_svc.ensure_offer_identity(s, actor, _identity_offer(api))
            await s.commit()
            return item.id
        except BaseException:
            await s.rollback()
            raise


async def _historic_teaching(
    sessions,
    org,
    item_id,
    *,
    when: datetime,
    author_id,
    label: str | None = None,
    kind: str = "laboratory",
    retired: tuple[str, ...] = (),
) -> uuid.UUID:
    """Inserta una enseñanza anterior con su fecha real.

    La interpretación es inmutable por trigger: la cronología se simula insertando la fila, nunca
    reescribiéndola.
    """
    async with sessions() as s, s.begin():
        observation = CommercialObservation(
            organization_id=org,
            text_original=f"Prefiero {label}" if label else "Ya no lo prefiero",
            author_id=author_id,
            created_at=when,
            trade_item_id=item_id,
            context={"historic": True},
            data_origin="captured",
        )
        s.add(observation)
        await s.flush()
        interpretation = ObservationInterpretation(
            organization_id=org,
            observation_id=observation.id,
            trade_item_id=item_id,
            status="suggested",
            model="historial",
            prompt_version="v1",
            retired_subject_kinds=list(retired),
            created_at=when,
            data_origin="captured",
        )
        s.add(interpretation)
        await s.flush()
        if label is not None:
            s.add(
                CommercialPreference(
                    organization_id=org,
                    interpretation_id=interpretation.id,
                    observation_id=observation.id,
                    trade_item_id=item_id,
                    subject_kind=kind,
                    subject_key=normalize_name(label),
                    subject_label=label,
                    effect="prefer",
                    confidence=Decimal("0.99"),
                    status="suggested",
                    created_at=when,
                    data_origin="captured",
                )
            )
        return interpretation.id


async def _prefs(sessions, org, item_id):
    async with sessions() as s:
        return (await pref_svc.load_item_preferences(s, org, [item_id]))[item_id]


def _labels(prefs, kind="laboratory") -> list[str]:
    return [
        pref.subject_label
        for pref in prefs.applicable
        if pref_svc.preference_class(pref) == kind
    ]


async def _link(sessions, api, *, source, external_id, item, who="pao"):
    return await _run(
        sessions,
        api.ids[who],
        create_mapping,
        source_id=api.ids["sources"][source],
        external_id=external_id,
        external_name=None,
        trade_item_id=item,
    )


# ------------------------------------------------------------------- enseñanza inmediata


async def test_ensena_desde_una_oferta_sin_vinculo_y_crea_su_identidad(api):
    """Sin vínculo ni EAN, la oferta se enseña igual: identidad técnica propia, no un pendiente."""
    observation, detail = await _teach(api, offer=_offer_body(api))

    identity = uuid.UUID(observation["trade_item_id"])
    assert identity != api.ids["canonical"]
    assert observation["context"]["identity_resolution"] == "offer_identity_created"
    assert observation["context"]["technical_identity"] is True
    assert observation["context"]["offer_view"]["laboratory"] == ASCEND
    assert observation["context"]["offer_view"]["price"] == {"amount": "990", "currency": "CLP"}

    assert detail["interpretation_status"] == "suggested"
    applied = detail["interpretation"]["preferences"]
    assert [(p["subject_kind"], p["subject_label"]) for p in applied] == [("laboratory", ASCEND)]

    async with api.sessions() as s:
        item = await s.get(TradeItem, identity)
        assert item.status == "inactive"
        assert item.identity_key == offer_svc.offer_identity_key(
            api.ids["sources"]["farmaloop"], "15391"
        )
        assert item.attributes["origin"] == "offer_identity"
        # El vínculo verificado sigue siendo la única vía para tocar el catálogo canónico.
        mappings = (
            await s.execute(select(func.count()).select_from(SupplierProductMapping))
        ).scalar_one()
        assert mappings == 0

    # Repetir la enseñanza (reintento) reutiliza la identidad y no crea otra.
    again, _ = await _teach(api, offer=_offer_body(api), key=str(uuid.uuid4()))
    assert again["trade_item_id"] == observation["trade_item_id"]
    assert again["context"]["identity_resolution"] == "offer_identity"
    async with api.sessions() as s:
        identities = (
            await s.execute(
                select(func.count())
                .select_from(TradeItem)
                .where(TradeItem.identity_key.like(f"{offer_svc.OFFER_IDENTITY_PREFIX}%"))
            )
        ).scalar_one()
    assert identities == 1


async def test_ensenanzas_simultaneas_comparten_una_sola_identidad(api, sessions):
    """Dos peticiones a la vez serializan por (organización, fuente, SKU) y comparten la identidad."""
    offer = offer_svc.OfferViewContext(
        source_id=api.ids["sources"]["farmaloop"],
        external_id="15391",
        name="Ibuprofeno 400 mg x 20 comprimidos",
        laboratory=ASCEND,
        price_amount=Decimal("990"),
        currency="CLP",
        price_basis="per_commercial_unit",
        sale_unit="box",
        stock_status="in_stock",
    )

    async def create():
        async with sessions() as s:
            actor = await load_actor(s, api.ids["pao"])
            try:
                row = await quote_svc.create_observation(
                    s, actor, text="Prefiero ASCEND", offer_view=offer
                )
                await s.commit()
                return row.trade_item_id
            except BaseException:
                await s.rollback()
                raise

    first, second = await asyncio.gather(create(), create())
    assert first == second
    async with sessions() as s:
        identities = (
            await s.execute(
                select(func.count())
                .select_from(TradeItem)
                .where(TradeItem.identity_key.like(f"{offer_svc.OFFER_IDENTITY_PREFIX}%"))
            )
        ).scalar_one()
        created_events = (
            await s.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "trade_item.offer_identity_created")
            )
        ).scalar_one()
    assert identities == 1
    assert created_events == 1


async def test_openai_recibe_el_contexto_comercial_real_de_la_oferta(api):
    """El modelo ve la oferta seleccionada: sin esto rechazaría ASCEND por no conocerlo."""
    await _teach(api, text="Prefiero esta marca", offer=_offer_body(api))

    assert api.model.calls == 1
    prompt = api.model.prompts[0]
    head, _, _ = prompt.partition("\n\nOBSERVACIÓN DE LA ADMINISTRADORA")
    payload = json.loads(head.split("\n", 1)[1])
    selected = payload["oferta_seleccionada"]
    assert selected["external_id"] == "15391"
    assert selected["laboratory"] == ASCEND
    assert selected["price"] == {"amount": "990", "currency": "CLP"}
    assert selected["name"] == "Ibuprofeno 400 mg x 20 comprimidos"
    assert ASCEND in payload["laboratorios_conocidos_del_catalogo"]


# ------------------------------------------------------------------- búsqueda posterior


async def test_la_ensenanza_ordena_las_ofertas_de_la_busqueda_siguiente(api, sessions):
    """Caso de aceptación: al vincular los tres SKU, Andes muestra ASCEND antes que Cordillera."""
    await _teach(api, offer=_offer_body(api))
    canonical = api.ids["canonical"]
    for source, external in (
        ("andes", "AND-002"),
        ("andes", "AND-031"),
        ("farmaloop", "15391"),
    ):
        await _link(sessions, api, source=source, external_id=external, item=canonical)

    snapshot = {
        "organization_id": api.ids["org"],
        "source_id": api.ids["sources"]["andes"],
        "offers": [
            {
                "external_id": "AND-002",
                "laboratory": CORDILLERA,
                "price": {"amount": "3190", "currency": "CLP"},
                "price_basis": "per_commercial_unit",
                "sale_unit": "box",
                "stock_status": "in_stock",
                "stock_quantity": "10",
                "stock_unit": "box",
            },
            {
                "external_id": "AND-031",
                "laboratory": ASCEND,
                "price": {"amount": "2890", "currency": "CLP"},
                "price_basis": "per_commercial_unit",
                "sale_unit": "box",
                "stock_status": "in_stock",
                "stock_quantity": "10",
                "stock_unit": "box",
            },
        ],
        "published": [],
    }
    async with sessions() as s:
        resolved = await _resolve_trade_item_ids(s, snapshot)
        keyed = await _apply_order_keys(s, resolved)

    entries = {entry["external_id"]: entry for entry in keyed["offers"]}
    assert set(entries) == {"AND-002", "AND-031"}  # ambas siguen visibles
    assert entries["AND-002"]["trade_item_id"] == canonical
    assert entries["AND-031"]["trade_item_id"] == canonical
    assert tuple(entries["AND-031"]["order_key"]) < tuple(entries["AND-002"]["order_key"])

    # FarmaLoop conserva su propia sección: su oferta también resuelve al artículo canónico.
    farmaloop = {
        "organization_id": api.ids["org"],
        "source_id": api.ids["sources"]["farmaloop"],
        "offers": [
            {
                "external_id": "15391",
                "laboratory": ASCEND,
                "price": {"amount": "990", "currency": "CLP"},
                "price_basis": "per_commercial_unit",
                "sale_unit": "box",
                "stock_status": "in_stock",
            }
        ],
        "published": [],
    }
    async with sessions() as s:
        own = await _resolve_trade_item_ids(s, farmaloop)
    assert own["offers"][0]["trade_item_id"] == canonical


async def test_el_catalogo_visible_no_duplica_articulos(api, sessions):
    """La identidad técnica queda `inactive`: no aparece en el catálogo ni compite en la búsqueda."""
    await _teach(api, offer=_offer_body(api))
    canonical = api.ids["canonical"]
    await _link(sessions, api, source="farmaloop", external_id="15391", item=canonical)

    async with sessions() as s:
        visible = (
            (
                await s.execute(
                    select(TradeItem)
                    .where(
                        TradeItem.organization_id == api.ids["org"],
                        TradeItem.status == "active",
                    )
                    .order_by(TradeItem.display_name)
                )
            )
            .scalars()
            .all()
        )
        identity = (
            await s.execute(
                select(TradeItem).where(
                    TradeItem.identity_key
                    == offer_svc.offer_identity_key(api.ids["sources"]["farmaloop"], "15391")
                )
            )
        ).scalar_one()
    assert [item.id for item in visible] == [canonical, api.ids["other_item"]]
    assert identity.status == "inactive"


# ------------------------------------------------------------------- reubicación


async def test_la_vinculacion_conserva_el_aprendizaje_y_su_trazabilidad(api, sessions):
    """Se reubica el estado vigente con su fecha original; la observación original no se toca."""
    observation, detail = await _teach(api, offer=_offer_body(api))
    identity = uuid.UUID(observation["trade_item_id"])
    source_interp = detail["interpretation"]

    mapping, created, adoption = await _link(
        sessions, api, source="farmaloop", external_id="15391", item=api.ids["canonical"],
        who="yarella",
    )
    assert created is True
    assert adoption.adopted is True
    assert len(adoption.derived_observation_ids) == 1
    assert adoption.conflicts == ()

    derived_id = adoption.derived_observation_ids[0]
    async with sessions() as s:
        original = await s.get(CommercialObservation, uuid.UUID(observation["observation_id"]))
        assert original.trade_item_id == identity
        assert original.text_original == "Prefiero ASCEND"
        assert original.author_id == api.ids["pao"]
        assert original.context["identity_resolution"] == "offer_identity_created"

        derived = await s.get(CommercialObservation, derived_id)
        assert derived.trade_item_id == api.ids["canonical"]
        # La reubicación la ejecuta quien vincula; no se presenta como una enseñanza nueva de Pao.
        assert derived.author_id == api.ids["yarella"]
        assert derived.text_original == original.text_original
        assert derived.created_at == original.created_at
        assert derived.context["adopted_from_observation_id"] == str(original.id)
        assert derived.context["adopted_from_interpretation_id"] == source_interp[
            "interpretation_id"
        ]
        assert derived.context["adopted_from_trade_item_id"] == str(identity)
        assert derived.context["adoption_reason"] == "offer_identity_linked"

        interpretation = (
            await s.execute(
                select(ObservationInterpretation).where(
                    ObservationInterpretation.observation_id == derived_id
                )
            )
        ).scalar_one()
        assert interpretation.status == "suggested"
        assert interpretation.created_at == datetime.fromisoformat(
            source_interp["created_at"]
        )
        pref = (
            await s.execute(
                select(CommercialPreference).where(
                    CommercialPreference.interpretation_id == interpretation.id
                )
            )
        ).scalar_one()
        assert (pref.subject_label, pref.status) == (ASCEND, "suggested")
        assert pref.created_at == interpretation.created_at

    # Revincular el mismo SKU al mismo artículo es idempotente y no inventa un conflicto.
    _, again_created, again = await _link(
        sessions, api, source="farmaloop", external_id="15391", item=api.ids["canonical"]
    )
    assert again_created is False
    assert again.adopted is False
    assert again.skipped == "already_adopted"
    assert again.conflicts == ()
    async with sessions() as s:
        derived_count = (
            await s.execute(
                select(func.count())
                .select_from(CommercialObservation)
                .where(
                    CommercialObservation.trade_item_id == api.ids["canonical"],
                    CommercialObservation.context["adopted_from_interpretation_id"].astext
                    == source_interp["interpretation_id"],
                )
            )
        ).scalar_one()
    assert derived_count == 1
    assert mapping.trade_item_id == api.ids["canonical"]


# ------------------------------------------------------------------- política de conflictos


async def test_una_ensenanza_posterior_gana_a_la_preferencia_anterior(api, sessions):
    """El motor decide por cronología: lo recién enseñado no se descarta por antigüedad."""
    canonical = api.ids["canonical"]
    await _historic_teaching(
        sessions,
        api.ids["org"],
        canonical,
        when=datetime.now(UTC) - timedelta(hours=2),
        author_id=api.ids["pao"],
        label=CORDILLERA,
    )

    _, newer = await _teach(api, offer=_offer_body(api))  # ASCEND, ahora
    _, _, adoption = await _link(
        sessions, api, source="farmaloop", external_id="15391", item=canonical
    )
    assert adoption.adopted is True
    assert adoption.conflicts == ()

    prefs = await _prefs(sessions, api.ids["org"], canonical)
    assert _labels(prefs) == [ASCEND]
    assert len(prefs.interpretations) == 2
    # La preferencia anterior se conserva como historial; nada se borra ni se sobrescribe.
    async with sessions() as s:
        older_rows = (
            await s.execute(
                select(func.count())
                .select_from(CommercialPreference)
                .where(
                    CommercialPreference.trade_item_id == canonical,
                    CommercialPreference.subject_label == CORDILLERA,
                )
            )
        ).scalar_one()
    assert older_rows == 1
    assert newer["interpretation"]["preferences"][0]["subject_label"] == ASCEND


async def test_una_ensenanza_anterior_no_sobrescribe_la_vigente(api, sessions):
    """Lo viejo se reubica como historial: no gana solo porque se vincula después."""
    identity = await _identity(sessions, api)
    await _historic_teaching(
        sessions,
        api.ids["org"],
        identity,
        when=datetime.now(UTC) - timedelta(hours=3),
        author_id=api.ids["pao"],
        label=ASCEND,
    )
    _, current = await _teach(
        api,
        text="Prefiero Cordillera",
        response=_response([_op(label=CORDILLERA)]),
        trade_item_id=api.ids["canonical"],
    )

    _, _, adoption = await _link(
        sessions, api, source="farmaloop", external_id="15391", item=api.ids["canonical"]
    )
    assert adoption.adopted is True
    prefs = await _prefs(sessions, api.ids["org"], api.ids["canonical"])
    assert _labels(prefs) == [CORDILLERA]
    applicable_ids = {pref.id for pref in prefs.applicable}
    async with sessions() as s:
        adopted = (
            await s.execute(
                select(CommercialPreference).where(
                    CommercialPreference.id.in_(adoption.preference_ids)
                )
            )
        ).scalars().all()
    assert [pref.subject_label for pref in adopted] == [ASCEND]
    assert not (applicable_ids & {pref.id for pref in adopted})
    assert current["interpretation"]["preferences"][0]["subject_label"] == CORDILLERA
    assert identity != api.ids["canonical"]


async def test_un_empate_exacto_se_audita_y_no_sobrescribe(api, sessions):
    """Misma fecha, misma clase: no se elige por conveniencia; se conserva y se audita."""
    canonical = api.ids["canonical"]
    identity = await _identity(sessions, api)
    tie = datetime.now(UTC) - timedelta(minutes=30)
    await _historic_teaching(
        sessions, api.ids["org"], identity, when=tie, author_id=api.ids["pao"], label=ASCEND
    )
    await _historic_teaching(
        sessions, api.ids["org"], canonical, when=tie, author_id=api.ids["pao"], label=CORDILLERA
    )

    _, _, adoption = await _link(
        sessions, api, source="farmaloop", external_id="15391", item=canonical
    )
    assert adoption.adopted is False
    assert adoption.skipped == "only_conflicts"
    assert len(adoption.conflicts) == 1
    conflict = adoption.conflicts[0]
    assert conflict.subject_kind == "laboratory"
    assert conflict.reason == "same_timestamp"

    prefs = await _prefs(sessions, api.ids["org"], canonical)
    assert _labels(prefs) == [CORDILLERA]

    async with sessions() as s:
        audit = (
            await s.execute(
                select(OutboxEvent).where(
                    OutboxEvent.type == "commercial_preference.adoption_conflict"
                )
            )
        ).scalar_one()
    assert audit.payload["conflicts"][0]["reason"] == "same_timestamp"
    assert audit.payload["canonical_trade_item_id"] == str(canonical)


async def test_clases_distintas_no_se_mezclan_ni_se_sobrescriben(api, sessions):
    """Cada clase tiene su propio alcance: una marca nueva no reemplaza una preferencia de laboratorio."""
    _, canonical_detail = await _teach(
        api,
        text="Prefiero Cordillera",
        response=_response([_op(label=CORDILLERA)]),
        trade_item_id=api.ids["canonical"],
    )
    _, source_detail = await _teach(
        api,
        text="Prefiero esta marca",
        response=_response([_op(kind="brand", label=MARCA)]),
        offer=_offer_body(api, brand=MARCA),
    )

    _, _, adoption = await _link(
        sessions, api, source="farmaloop", external_id="15391", item=api.ids["canonical"]
    )
    assert adoption.adopted is True
    prefs = await _prefs(sessions, api.ids["org"], api.ids["canonical"])
    assert _labels(prefs, "laboratory") == [CORDILLERA]
    assert _labels(prefs, "brand") == [MARCA]
    assert canonical_detail["interpretation"]["preferences"][0]["subject_kind"] == "laboratory"
    assert source_detail["interpretation"]["preferences"][0]["subject_kind"] == "brand"


async def test_un_retiro_no_reactiva_la_preferencia_anterior(api, sessions):
    """El retiro se reubica como retiro: copiar solo las preferencias la reviviría."""
    canonical = api.ids["canonical"]
    identity = await _identity(sessions, api)
    now = datetime.now(UTC)
    # La oferta prefirió ASCEND, luego lo retiró; el canónico tiene Cordillera entre ambos.
    await _historic_teaching(
        sessions,
        api.ids["org"],
        identity,
        when=now - timedelta(hours=4),
        author_id=api.ids["pao"],
        label=ASCEND,
    )
    await _historic_teaching(
        sessions,
        api.ids["org"],
        identity,
        when=now - timedelta(hours=2),
        author_id=api.ids["pao"],
        retired=("laboratory",),
    )
    await _historic_teaching(
        sessions,
        api.ids["org"],
        canonical,
        when=now - timedelta(hours=3),
        author_id=api.ids["pao"],
        label=CORDILLERA,
    )

    _, _, adoption = await _link(
        sessions, api, source="farmaloop", external_id="15391", item=canonical
    )
    assert adoption.adopted is True

    prefs = await _prefs(sessions, api.ids["org"], canonical)
    assert "laboratory" in prefs.retired_subject_kinds
    assert _labels(prefs) == []
    async with sessions() as s:
        rows = (
            await s.execute(
                select(ObservationInterpretation).where(
                    ObservationInterpretation.trade_item_id == canonical
                )
            )
        ).scalars().all()
    derived = [row for row in rows if row.retired_subject_kinds]
    assert len(derived) == 1
    assert derived[0].retired_subject_kinds == ["laboratory"]


# ------------------------------------------------------------------- cambio de vínculo


async def test_cambiar_el_vinculo_a_otro_articulo_no_transfiere_preferencias(api, sessions):
    """Un SKU movido de un artículo canónico a otro no arrastra lo aprendido por el primero."""
    await _teach(api, offer=_offer_body(api))
    mapping, _, adoption = await _link(
        sessions, api, source="farmaloop", external_id="15391", item=api.ids["canonical"]
    )
    assert adoption.adopted is True

    moved, changed, none_adoption = await _run(
        sessions,
        api.ids["pao"],
        relink_mapping,
        mapping_id=mapping.id,
        trade_item_id=api.ids["other_item"],
    )
    assert changed is True
    assert none_adoption is None
    assert moved.trade_item_id == api.ids["other_item"]

    other = await _prefs(sessions, api.ids["org"], api.ids["other_item"])
    assert other.applicable == ()
    async with sessions() as s:
        skipped = (
            await s.execute(
                select(OutboxEvent).where(
                    OutboxEvent.type == "commercial_preference.adoption_skipped"
                )
            )
        ).scalar_one()
    assert skipped.payload["reason"] == "previous_target_not_technical_identity"
    assert skipped.payload["to_trade_item_id"] == str(api.ids["other_item"])

    # El artículo que sí fue enseñado conserva su preferencia.
    original = await _prefs(sessions, api.ids["org"], api.ids["canonical"])
    assert _labels(original) == [ASCEND]


# ------------------------------------------------------------------- aislamiento


async def test_otra_organizacion_no_puede_ensenar_con_la_fuente_ajena(api):
    r = await api.post(
        "/api/v1/commercial-observations",
        json={"text": "Prefiero ASCEND", "offer": _offer_body(api)},
        headers=H(api, "outsider"),
    )
    assert r.status_code == 404
    assert api.model.calls == 0
