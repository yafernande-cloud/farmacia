"""A6 Fase 3B — endpoints REST de observaciones comerciales e interpretación (O2–O4).

PostgreSQL real y proveedor **simulado**: ninguna prueba llama a OpenAI. Cubre permisos,
aislamiento por organización y artículo, idempotencia, ambigüedad, fallo del proveedor y las
lecturas de historial, detalle y preferencias vigentes.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.v1.observations import get_observation_model
from app.core.config import get_settings
from app.db.session import get_session
from app.interpretation.openai_observations import ModelError, RawCompletion
from app.main import create_app
from app.models import (
    AppUser,
    CommercialObservation,
    OfferSnapshot,
    Organization,
    Source,
    Supplier,
    TradeItem,
)

pytestmark = pytest.mark.db

NOW = datetime.now(UTC) - timedelta(minutes=5)


def _attrs(ingredient: str, content: str) -> dict:
    return {
        "schema_version": 2,
        "unassigned": [],
        "ingredient": ingredient,
        "form": {"value": "comprimido", "basis": "explicit", "origin": "text"},
        "content": {
            "basis": "explicit",
            "origin": "text",
            "measure": {"value": content, "unit": "un"},
        },
    }


@pytest.fixture
async def a6(sessions):
    ids: dict = {}
    async with sessions() as s, s.begin():
        org = Organization(name="Farmacia A6 API", kind="simulation")
        other = Organization(name="Otra A6 API", kind="simulation")
        s.add_all([org, other])
        await s.flush()
        ids["org"], ids["other_org"] = org.id, other.id

        users = {
            "admin": AppUser(organization_id=org.id, display_name="Admin", roles=["admin"]),
            "aux": AppUser(organization_id=org.id, display_name="Auxiliar", roles=["auxiliary"]),
            "outsider": AppUser(
                organization_id=other.id, display_name="Externo", roles=["admin"]
            ),
        }
        s.add_all(users.values())
        await s.flush()
        ids.update({key: user.id for key, user in users.items()})

        suppliers = {
            name: Supplier(
                organization_id=org.id, name=f"Proveedor {name}", data_origin="simulated"
            )
            for name in ("Norte", "Sur")
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
                last_success_at=NOW,
                data_origin="simulated",
            )
            s.add(source)
            sources[key] = source
        await s.flush()

        items = {
            "a": TradeItem(
                organization_id=org.id,
                display_name="Losartán 50 mg x 20 comprimidos",
                identity_key="fixture-a6-api-losartan-50-20",
                ingredient="losartán",
                attributes=_attrs("losartán", "20"),
                data_origin="simulated",
            ),
            "b": TradeItem(
                organization_id=org.id,
                display_name="Losartán 100 mg x 20 comprimidos",
                identity_key="fixture-a6-api-losartan-100-20",
                ingredient="losartán",
                attributes=_attrs("losartán", "20"),
                data_origin="simulated",
            ),
        }
        s.add_all(items.values())
        await s.flush()

        def _offer(supplier, source, item, external_id, price, laboratory, brand):
            return OfferSnapshot(
                organization_id=org.id,
                supplier_id=supplier.id,
                source_id=source.id,
                trade_item_id=item.id,
                external_id=external_id,
                price_amount=price,
                currency="CLP",
                price_basis="per_commercial_unit",
                sale_unit="box",
                sale_unit_text="caja",
                pack_units_per_commercial_unit="20",
                pack_base_unit="un",
                pack_text="caja x 20",
                stock_status="known",
                stock_quantity="90",
                stock_unit="box",
                tax_included=True,
                discount_included=True,
                minimum_known=True,
                minimum_quantity="1",
                minimum_unit="box",
                multiple_known=True,
                multiple_quantity="1",
                multiple_unit="box",
                observed_at=NOW,
                laboratory_as_published=laboratory,
                brand_as_published=brand,
                data_origin="simulated",
            )

        s.add_all(
            [
                _offer(
                    suppliers["Norte"],
                    sources["Norte"],
                    items["a"],
                    "A-N",
                    "2000.00",
                    "Laboratorio Chile",
                    "Marca Alfa",
                ),
                _offer(
                    suppliers["Sur"],
                    sources["Sur"],
                    items["a"],
                    "A-S",
                    "2250.00",
                    "Laboratorio Andino",
                    "Marca Beta",
                ),
            ]
        )
        await s.flush()
        ids["items"] = {key: item.id for key, item in items.items()}
    return ids


class StubModel:
    """Doble del proveedor: respuestas en cola, sin red ni cuota."""

    def __init__(self):
        self.responses: list = []
        self.calls = 0

    async def complete(self, *, system: str, user: str, json_schema: dict) -> RawCompletion:
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
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
async def api(sessions, a6, model, monkeypatch):
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
        c.ids = a6
        c.sessions = sessions
        c.model = model
        yield c
    get_settings.cache_clear()


def H(api, who="admin") -> dict[str, str]:
    return {"X-Demo-User-Id": str(api.ids[who])}


def _response(operations=(), retire=(), ambiguity=None, article_ops=()) -> dict:
    return {
        "operations": list(operations),
        "article_operations": list(article_ops),
        "retire_subject_kinds": list(retire),
        "ambiguity": ambiguity,
    }


def _op(
    kind="laboratory",
    label="Laboratorio Chile",
    effect="prefer",
    confidence=0.99,
    tolerance=None,
) -> dict:
    return {
        "subject_kind": kind,
        "subject_label": label,
        "effect": effect,
        "tolerance": tolerance
        or {"kind": "none", "amount": None, "percent": None, "currency": None},
        "confidence": confidence,
        "price_reference": None,
    }


async def _create(api, text: str, item="a", who="admin", key: str | None = None):
    headers = H(api, who)
    if key:
        headers["Idempotency-Key"] = key
    return await api.post(
        "/api/v1/commercial-observations",
        json={"text": text, "trade_item_id": str(api.ids["items"][item])},
        headers=headers,
    )


async def _interpret(api, observation_id, who="admin"):
    return await api.post(
        f"/api/v1/commercial-observations/{observation_id}/interpretations",
        headers=H(api, who),
    )


async def _observations(api, item="a") -> list[dict]:
    r = await api.get(
        "/api/v1/commercial-observations",
        params={"trade_item_id": str(api.ids["items"][item])},
        headers=H(api),
    )
    assert r.status_code == 200, r.text
    return r.json()["observations"]


# --------------------------------------------------------------------------- registro


async def test_administrador_registra_y_lee_su_observacion(api):
    r = await _create(api, "Prefiero Laboratorio Chile aunque cueste un poco más.")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["trade_item_id"] == str(api.ids["items"]["a"])
    assert body["text"] == "Prefiero Laboratorio Chile aunque cueste un poco más."
    assert body["interpretation_status"] == "sin_interpretacion"
    assert body["interpretation"] is None
    assert body["author_id"] == str(api.ids["admin"])

    r = await api.get(
        f"/api/v1/commercial-observations/{body['observation_id']}",
        headers=H(api),
    )
    assert r.status_code == 200
    assert r.json()["text"] == body["text"]
    assert r.json()["interpretations"] == []
    assert r.json()["retired_subject_kinds"] == []


async def test_rechaza_usuario_sin_permisos(api):
    r = await _create(api, "Prefiero Laboratorio Chile.", who="aux")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


async def test_aislamiento_entre_organizaciones(api):
    item = api.ids["items"]["a"]
    r = await api.post(
        "/api/v1/commercial-observations",
        json={"text": "Prefiero Laboratorio Chile.", "trade_item_id": str(item)},
        headers=H(api, "outsider"),
    )
    assert r.status_code == 404

    r = await api.get(
        "/api/v1/commercial-observations",
        params={"trade_item_id": str(item)},
        headers=H(api, "outsider"),
    )
    assert r.status_code == 404

    r = await api.get(f"/api/v1/articles/{item}/preferences", headers=H(api, "outsider"))
    assert r.status_code == 404

    # La observación real sigue invisible para la otra organización.
    own = await _create(api, "Prefiero Laboratorio Chile.")
    observation_id = own.json()["observation_id"]
    r = await api.get(
        f"/api/v1/commercial-observations/{observation_id}",
        headers=H(api, "outsider"),
    )
    assert r.status_code == 404
    r = await _interpret(api, observation_id, who="outsider")
    assert r.status_code == 404
    assert api.model.calls == 0


async def test_texto_vacio_invalido_y_demasiado_largo(api):
    item = api.ids["items"]["a"]
    payload = {"trade_item_id": str(item)}
    r = await api.post(
        "/api/v1/commercial-observations", json={"text": "", **payload}, headers=H(api)
    )
    assert r.status_code == 400

    r = await api.post(
        "/api/v1/commercial-observations", json={"text": "   ", **payload}, headers=H(api)
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OBSERVATION_INVALID"

    r = await api.post(
        "/api/v1/commercial-observations",
        json={"text": "x" * 2001, **payload},
        headers=H(api),
    )
    assert r.status_code == 400

    # El cliente no puede enviar preferencias, estados ni sujetos interpretados.
    r = await api.post(
        "/api/v1/commercial-observations",
        json={"text": "Prefiero Laboratorio Chile.", "subject_kind": "laboratory", **payload},
        headers=H(api),
    )
    assert r.status_code == 400


async def test_idempotencia_de_la_observacion(api):
    key = "obs-a6-api-0001"
    first = await _create(api, "Prefiero Laboratorio Chile.", key=key)
    second = await _create(api, "Prefiero Laboratorio Chile.", key=key)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["observation_id"] == second.json()["observation_id"]
    assert len(await _observations(api)) == 1

    reused = await _create(api, "Ahora prefiero Laboratorio Andino.", key=key)
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


# --------------------------------------------------------------------------- interpretación


async def test_interpretacion_exitosa_con_proveedor_simulado(api):
    created = await _create(api, "Prefiero Laboratorio Chile aunque cueste un poco más.")
    observation_id = created.json()["observation_id"]
    api.model.responses = [_response([_op()])]

    r = await _interpret(api, observation_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replayed"] is False
    observation = body["observation"]
    assert observation["interpretation_status"] == "suggested"
    interpretation = observation["interpretation"]
    assert interpretation["status"] == "suggested"
    assert interpretation["error_code"] is None
    assert [p["subject_label"] for p in interpretation["preferences"]] == ["Laboratorio Chile"]
    assert interpretation["preferences"][0]["subject_kind"] == "laboratory"
    assert len(observation["interpretations"]) == 1
    assert api.model.calls == 1

    # El listado y las preferencias vigentes reflejan lo aplicado.
    rows = await _observations(api)
    assert len(rows) == 1
    assert rows[0]["interpretation_status"] == "suggested"

    r = await api.get(f"/api/v1/articles/{api.ids['items']['a']}/preferences", headers=H(api))
    assert r.status_code == 200
    prefs = r.json()
    assert [p["subject_label"] for p in prefs["preferences"]] == ["Laboratorio Chile"]
    assert prefs["retired_subject_kinds"] == []
    assert prefs["history"][0]["is_active"] is True
    assert prefs["history"][0]["subject_class"] == "laboratory"


async def test_reintento_no_duplica_la_interpretacion(api):
    created = await _create(api, "Prefiero Laboratorio Chile.")
    observation_id = created.json()["observation_id"]
    api.model.responses = [_response([_op()])]

    first = await _interpret(api, observation_id)
    second = await _interpret(api, observation_id)
    assert first.status_code == second.status_code == 200
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert second.json()["observation"]["interpretation_status"] == "suggested"
    assert len(second.json()["observation"]["interpretations"]) == 1
    assert api.model.calls == 1

    async with api.sessions() as s:
        rows = (
            await s.execute(
                select(CommercialObservation).where(
                    CommercialObservation.id == uuid.UUID(observation_id)
                )
            )
        ).scalars().all()
    assert len(rows) == 1


async def test_ambiguedad_no_modifica_preferencias(api):
    first = await _create(api, "Prefiero Laboratorio Chile.")
    first_id = first.json()["observation_id"]
    api.model.responses = [_response([_op()])]
    assert (await _interpret(api, first_id)).json()["observation"]["interpretation_status"] == (
        "suggested"
    )

    second = await _create(api, "Prefiero ese laboratorio que te dije el otro día.")
    second_id = second.json()["observation_id"]
    api.model.responses = [
        _response(
            ambiguity={
                "code": "unclear_referent",
                "note": "No se identifica el laboratorio.",
            }
        )
    ]
    r = await _interpret(api, second_id)
    assert r.status_code == 200
    observation = r.json()["observation"]
    assert observation["interpretation_status"] == "needs_review"
    assert observation["interpretation"]["error_code"] == "ambiguous"
    assert observation["interpretation"]["preferences"] == []
    assert api.model.calls == 2

    r = await api.get(f"/api/v1/articles/{api.ids['items']['a']}/preferences", headers=H(api))
    assert [p["subject_label"] for p in r.json()["preferences"]] == ["Laboratorio Chile"]


async def test_fallo_del_proveedor_no_pierde_la_observacion(api):
    first = await _create(api, "Prefiero Laboratorio Chile.")
    api.model.responses = [_response([_op()])]
    await _interpret(api, first.json()["observation_id"])

    second = await _create(api, "Ahora prefiero Laboratorio Andino.")
    second_id = second.json()["observation_id"]
    api.model.responses = [ModelError("timeout")]
    r = await _interpret(api, second_id)
    assert r.status_code == 200
    observation = r.json()["observation"]
    assert observation["text"] == "Ahora prefiero Laboratorio Andino."
    assert observation["interpretation_status"] == "failed"
    assert observation["interpretation"]["error_code"] == "timeout"
    assert observation["interpretation"]["preferences"] == []

    # La observación sigue en el historial y la preferencia anterior sigue vigente.
    rows = await _observations(api)
    assert {row["text"] for row in rows} == {
        "Prefiero Laboratorio Chile.",
        "Ahora prefiero Laboratorio Andino.",
    }
    r = await api.get(f"/api/v1/articles/{api.ids['items']['a']}/preferences", headers=H(api))
    assert [p["subject_label"] for p in r.json()["preferences"]] == ["Laboratorio Chile"]


async def test_retiro_explicito_y_historial_de_preferencias(api):
    first = await _create(api, "Prefiero Laboratorio Chile.")
    api.model.responses = [_response([_op()])]
    await _interpret(api, first.json()["observation_id"])

    second = await _create(api, "Olvida lo del laboratorio; da lo mismo cuál sea.")
    api.model.responses = [_response([], retire=["laboratory"])]
    r = await _interpret(api, second.json()["observation_id"])
    assert r.status_code == 200
    assert r.json()["observation"]["retired_subject_kinds"] == ["laboratory"]

    r = await api.get(f"/api/v1/articles/{api.ids['items']['a']}/preferences", headers=H(api))
    prefs = r.json()
    assert prefs["preferences"] == []
    assert prefs["retired_subject_kinds"] == ["laboratory"]
    assert [(p["subject_label"], p["is_active"]) for p in prefs["history"]] == [
        ("Laboratorio Chile", False)
    ]
    assert [(x["subject_kind"], x["is_active"]) for x in prefs["retirements"]] == [
        ("laboratory", True)
    ]


# --------------------------------------------------------------------------- lecturas y límites


async def test_historial_paginado_y_detalle(api):
    for text in ("Primera.", "Segunda.", "Tercera."):
        assert (await _create(api, text)).status_code == 201

    item = str(api.ids["items"]["a"])
    r = await api.get(
        "/api/v1/commercial-observations",
        params={"trade_item_id": item, "limit": 2},
        headers=H(api),
    )
    assert r.status_code == 200
    page = r.json()
    assert [row["text"] for row in page["observations"]] == ["Tercera.", "Segunda."]
    assert page["next_cursor"]

    r = await api.get(
        "/api/v1/commercial-observations",
        params={"trade_item_id": item, "limit": 2, "cursor": page["next_cursor"]},
        headers=H(api),
    )
    assert r.status_code == 200
    assert [row["text"] for row in r.json()["observations"]] == ["Primera."]
    assert r.json()["next_cursor"] is None

    observation_id = page["observations"][0]["observation_id"]
    r = await api.get(
        f"/api/v1/commercial-observations/{observation_id}",
        headers=H(api),
    )
    assert r.status_code == 200
    detail = r.json()
    assert detail["text"] == "Tercera."
    assert detail["interpretation_status"] == "sin_interpretacion"
    assert detail["interpretations"] == []


async def test_no_accede_a_observaciones_de_otro_articulo(api):
    created = await _create(api, "Prefiero Laboratorio Chile.", item="b")
    observation_id = created.json()["observation_id"]

    # El historial del artículo A no incluye la observación del artículo B.
    assert await _observations(api, item="a") == []
    assert [row["text"] for row in await _observations(api, item="b")] == [
        "Prefiero Laboratorio Chile."
    ]

    # Un artículo inexistente o de otra organización responde 404, no una lista vacía.
    r = await api.get(
        "/api/v1/commercial-observations",
        params={"trade_item_id": str(uuid.uuid4())},
        headers=H(api),
    )
    assert r.status_code == 404

    # La observación de B sigue siendo visible solo para su organización.
    r = await api.get(
        f"/api/v1/commercial-observations/{observation_id}", headers=H(api, "outsider")
    )
    assert r.status_code == 404
    r = await _interpret(api, observation_id, who="outsider")
    assert r.status_code == 404
    assert api.model.calls == 0


async def test_interpretacion_sin_proveedor_configurado(api):
    created = await _create(api, "Prefiero Laboratorio Chile.")
    # Sin doble inyectado y con OBSERVATION_AI_ENABLED apagado, no hay llamada ni fila.
    api.app.dependency_overrides.pop(get_observation_model, None)
    r = await _interpret(api, created.json()["observation_id"])
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "OBSERVATION_AI_DISABLED"
    assert (await _observations(api))[0]["interpretation_status"] == "sin_interpretacion"
