"""API HTTP sobre PostgreSQL real: contratos, errores y los escenarios del demo."""

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.session import get_session
from app.main import create_app
from tests.conftest import new_key

pytestmark = pytest.mark.db


@pytest.fixture
async def api(sessions, seed, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    app = create_app()

    async def _session():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.seed = seed
        yield c


def H(api, who, **extra):
    return {"X-Demo-User-Id": str(api.seed[who]), **extra}


async def report(api, who, text, key=None, **body):
    return await api.post("/api/v1/observations", json={"text": text, **body},
                          headers=H(api, who, **{"Idempotency-Key": key or new_key()}))  # fmt: skip


async def command(api, item_id, version, op, value=None, unit=None, key=None, who="pao"):
    body = {"operation": op, "value": value, "unit": unit, "reason": "demo"}
    return await api.post(f"/api/v1/demand-items/{item_id}/quantity-commands", json=body,
                          headers=H(api, who, **{"Idempotency-Key": key or new_key(), "If-Match": f'"{version}"'}))  # fmt: skip


async def lst(api, who="pao"):
    return (await api.get("/api/v1/demand-items", headers=H(api, who))).json()


async def test_api_expone_cantidad_solicitada_y_propuesta_sin_confundirlas_con_la_objetivo(
    api, ctx
):
    """Pao ve lo que pidió el auxiliar y su propuesta; la cantidad objetivo sigue aparte."""
    r = await ctx.report(
        "ana",
        "falta ibuprofeno 400 mg",
        declared_quantity=Decimal(8),
        declared_unit="box",
        quantity_proposal={"operation": "SET", "value": "8", "unit": "box"},
    )
    body = (
        await api.get(f"/api/v1/demand-items/{r.item.id}", headers=H(api, "pao"))
    ).json()
    assert body["requested_quantity"] == "8" and body["requested_unit"] == "box"
    assert body["declared_quantity"] == "8"  # compatibilidad con el campo anterior
    assert body["target_quantity"] is None  # Pao todavía no decidió cuánto comprar
    (obs,) = body["observations"]
    assert obs["quantity_proposal"] == {"operation": "SET", "value": "8", "unit": "box"}


async def test_dos_auxiliares_conservan_reportes_independientes(api):
    r1 = await report(api, "ana", "falta ibuprofeno de 400 mg")
    r2 = await report(api, "luis", "no queda ibuprofeno 400 mg")
    assert (r1.status_code, r2.status_code) == (201, 201)
    # Modelo A: cada persona tiene su propio reporte; nunca se adjunta al de otra persona.
    assert r1.json()["outcome"] == "created_item" and r2.json()["outcome"] == "created_item"
    assert r1.json()["quantity_changed"] is False and r2.json()["quantity_changed"] is False
    data = await lst(api)
    assert len(data["items"]) == 2
    by_author = {i["reported_by_name"]: i for i in data["items"]}
    assert set(by_author) == {"Ana", "Luis"}
    for item in data["items"]:
        assert item["observation_count"] == 1 and item["target_quantity"] is None
        assert item["quantity_status"] == "unknown" and item["needs_quantity"] is True
        assert {p["attribute"] for p in item["pending_validation"]} == {"form", "content"}
    detail = (
        await api.get(
            f"/api/v1/demand-items/{by_author['Ana']['id']}", headers=H(api, "pao")
        )
    ).json()
    assert [o["author"] for o in detail["observations"]] == ["Ana"]


async def test_flujo_cantidades_por_http(api):
    item = (await report(api, "ana", "falta ibuprofeno 400")).json()["item"]
    r = await command(api, item["id"], 1, "SET", "5", "box")
    assert r.status_code == 200 and r.headers["etag"] == '"2"'
    assert r.json()["item"]["target_quantity"] == "5"
    r = await report(api, "luis", "falta ibuprofeno 400")  # reporte de otra persona
    assert r.json()["item"]["id"] != item["id"] and r.json()["item"]["target_quantity"] is None
    mine = next(i for i in (await lst(api))["items"] if i["id"] == item["id"])
    assert mine["target_quantity"] == "5"  # el aviso de Luis no tocó la cantidad de Ana

    add = await command(api, item["id"], 2, "ADD", "5", "box", key="add-key-1")
    again = await command(api, item["id"], 2, "ADD", "5", "box", key="add-key-1")
    assert add.json()["item"]["target_quantity"] == "10"
    assert again.json()["replayed"] is True and again.json()["item"]["target_quantity"] == "10"
    mine = next(i for i in (await lst(api))["items"] if i["id"] == item["id"])
    assert mine["target_quantity"] == "10"  # sumó una vez


async def test_400_y_600_separados_por_http(api):
    await report(api, "ana", "falta ibuprofeno 400 mg")
    await report(api, "ana", "falta ibuprofeno 600 mg")
    names = sorted(i["display_name"] for i in (await lst(api))["items"])
    assert names == ["Ibuprofeno 400 mg", "Ibuprofeno 600 mg"]


async def test_reenvio_idempotente_devuelve_200_y_no_duplica(api):
    a = await report(api, "ana", "falta ibuprofeno 400", key="evento-123")
    b = await report(api, "ana", "falta ibuprofeno 400", key="evento-123")
    assert (a.status_code, b.status_code) == (201, 200) and b.json()["replayed"] is True
    assert (await lst(api))["items"][0]["observation_count"] == 1
    c = await report(api, "ana", "falta paracetamol 500", key="evento-123")
    assert c.status_code == 409 and c.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_revision_y_resolucion_por_http(api):
    base = (await report(api, "ana", "falta ibuprofeno 400 mg")).json()["item"]
    # Misma persona: solo sus propios reportes pueden quedar en revisión y asociarse (D-135).
    rev = (await report(api, "ana", "falta ibuprofeno 400 mg comprimidos x 20")).json()
    assert rev["outcome"] == "needs_review" and rev["item"] is None
    assert (await lst(api))["pending_review_count"] == 1
    pend = (await api.get("/api/v1/observations", headers=H(api, "pao"))).json()
    assert len(pend) == 1 and pend[0]["candidates"][0]["demand_item_id"] == base["id"]
    r = await api.post(f"/api/v1/observations/{pend[0]['id']}/resolve", headers=H(api, "pao"),
                       json={"demand_item_id": base["id"]})  # fmt: skip
    assert r.status_code == 200 and r.json()["outcome"] == "resolved_manual"
    assert (await lst(api))["pending_review_count"] == 0


async def test_errores_con_contrato_y_codigos_http(api):
    uid = str(api.seed["ana"])
    r = await api.get("/api/v1/demand-items")
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"
    assert (
        await api.get("/api/v1/demand-items", headers={"X-Demo-User-Id": "no-es-uuid"})
    ).status_code == 401
    r = await api.post(
        "/api/v1/observations", json={"text": "falta ibuprofeno"}, headers={"X-Demo-User-Id": uid}
    )
    assert (
        r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    )  # sin Idempotency-Key
    assert (await report(api, "ana", "falta")).status_code == 422
    item = (await report(api, "ana", "falta ibuprofeno 400")).json()["item"]
    r = await api.post(
        f"/api/v1/demand-items/{item['id']}/quantity-commands",
        headers=H(api, "pao", **{"Idempotency-Key": new_key()}),
        json={"operation": "SET", "value": "5", "unit": "box"},
    )
    assert r.status_code == 428
    r = await command(api, item["id"], 1, "SET", "5", "box", who="ana")
    assert r.status_code == 403
    r = await command(api, item["id"], 99, "SET", "5", "box")
    assert r.status_code == 409 and r.json()["error"]["details"]["current_version"] == 1
    r = await command(api, item["id"], 1, "ADD", "5", "box")
    assert r.status_code == 422 and r.json()["error"]["code"] == "QUANTITY_UNKNOWN"
    r = await command(api, item["id"], 1, "SET", "-3", "box")
    assert r.status_code == 400
    assert (
        "correlation_id" in r.json()["error"]
        and r.headers["x-correlation-id"] == r.json()["error"]["correlation_id"]
    )


async def test_aislamiento_entre_organizaciones_por_http(api):
    item = (await report(api, "ana", "falta ibuprofeno 400")).json()["item"]
    assert (await lst(api, "intruso"))["items"] == []
    r = await api.get(f"/api/v1/demand-items/{item['id']}", headers=H(api, "intruso"))
    assert r.status_code == 404


async def test_paginacion_por_cursor(api):
    for n in (400, 500, 600, 700, 800):
        await report(api, "ana", f"falta ibuprofeno {n} mg")
    p1 = (await api.get("/api/v1/demand-items?limit=2", headers=H(api, "pao"))).json()
    p2 = (
        await api.get(
            f"/api/v1/demand-items?limit=2&cursor={p1['next_cursor']}", headers=H(api, "pao")
        )
    ).json()
    p3 = (
        await api.get(
            f"/api/v1/demand-items?limit=2&cursor={p2['next_cursor']}", headers=H(api, "pao")
        )
    ).json()
    ids = [i["id"] for p in (p1, p2, p3) for i in p["items"]]
    assert len(ids) == 5 == len(set(ids)) and p3["next_cursor"] is None


async def test_auth_deshabilitada_responde_503(client):
    r = await client.get("/api/v1/me", headers={"X-Demo-User-Id": "x"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "AUTH_NOT_CONFIGURED"


async def test_sin_base_de_datos_responde_503_no_500(client, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "demo_header")
    get_settings.cache_clear()
    r = await client.get("/api/v1/me", headers={"X-Demo-User-Id": "x"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "DATABASE_NOT_CONFIGURED"


async def test_openapi_documenta_los_endpoints(api):
    spec = (await api.get("/openapi.json")).json()
    paths = spec["paths"]
    for p in ["/api/v1/demand-items", "/api/v1/demand-items/{item_id}", "/api/v1/observations",
              "/api/v1/observations/{observation_id}/resolve",
              "/api/v1/demand-items/{item_id}/quantity-commands", "/api/v1/me"]:  # fmt: skip
        assert p in paths
    cmd = paths["/api/v1/demand-items/{item_id}/quantity-commands"]["post"]
    names = {p["name"] for p in cmd["parameters"]}
    assert {"Idempotency-Key", "If-Match"} <= names


# ---------- modelo de producto: contrato HTTP ----------


async def test_respuesta_separa_potencia_contenido_y_conserva_lo_escrito(api):
    r = await report(api, "ana", "falta amoxicilina 250 mg/5 ml jarabe 120 ml 5 cajas")
    item = r.json()["item"]
    (s,) = item["attributes"]["strengths"]
    assert s["label"] == "250 mg/5 ml" and s["kind"] == "concentration" and s["basis"] == "explicit"
    assert s["as_written"] == {"value": "250", "unit": "mg", "per_value": "5", "per_unit": "ml",
                               "original_text": "250 mg/5 ml"}  # fmt: skip
    assert s["normalized"] == {"dimension": "mass/volume", "value": "50", "unit": "mg/ml"}
    assert item["attributes"]["content"]["label"] == "120 ml"
    assert item["attributes"]["form"] == {"value": "jarabe", "basis": "explicit"}
    assert r.json()["observation"]["declared_quantity"] == "5"  # cantidad solicitada, aparte
    assert item["target_quantity"] is None


async def test_sin_unidad_se_registra_sin_preguntar_y_sin_suponer(api):
    r = (await report(api, "ana", "falta ibuprofeno de 400")).json()
    assert r["outcome"] == "created_item"
    assert "?" not in r["message"] and "¿" not in r["message"]  # no termina en pregunta
    assert "no la supuse" in r["message"]
    assert r["item"]["attributes"]["strengths"] == []
    assert r["item"]["attributes"]["unassigned"][0]["label"] == "400"
    reasons = {(p["attribute"], p["reason"]) for p in r["pending_validation"]}
    assert ("measure", "unit_not_stated") in reasons and r["item"]["identity_complete"] is False


async def test_flujo_completo_confirmar_recordar_y_no_volver_a_preguntar(api):
    base = (await report(api, "ana", "falta ibuprofeno de 400")).json()["item"]
    dudoso = (await report(api, "ana", "no queda ibuprofeno 400 mg")).json()
    assert dudoso["outcome"] == "needs_review"
    assert "unit_unconfirmed" in dudoso["observation"]["candidates"][0]["reasons"]
    assert "?" not in dudoso["message"]
    obs_id = dudoso["observation"]["id"]
    r = await api.post(f"/api/v1/observations/{obs_id}/resolve", headers=H(api, "pao"),
                       json={"demand_item_id": base["id"], "apply_observed_attributes": True, "remember_alias": True})  # fmt: skip
    assert r.status_code == 200 and r.json()["alias_created"] is True
    item = r.json()["item"]
    assert item["attributes"]["strengths"][0]["basis"] == "confirmed" and item["version"] == 2

    again = (
        await report(api, "ana", "falta ibuprofeno 400")
    ).json()  # ya no hace falta que nadie decida
    assert (
        again["outcome"] == "attached_existing"
        and again["observation"]["attributes"]["strengths"][0]["role_rule"] == "verified_alias"
    )

    aliases = (await api.get("/api/v1/product-aliases", headers=H(api, "pao"))).json()
    assert len(aliases) == 1 and aliases[0]["assignment"] == {"role": "strength", "unit_expr": "mg"}
    assert (await api.get("/api/v1/product-aliases", headers=H(api, "ana"))).status_code == 403
    off = await api.post(
        f"/api/v1/product-aliases/{aliases[0]['id']}/deactivate", headers=H(api, "pao")
    )
    assert off.json()["status"] == "inactive"
    assert (await api.get("/api/v1/product-aliases", headers=H(api, "pao"))).json() == []


async def test_endpoint_confirmar_atributos(api):
    item = (await report(api, "ana", "falta ibuprofeno 400")).json()["item"]
    url = f"/api/v1/demand-items/{item['id']}/resolve"
    body = {
        "strengths": [{"value": "400", "unit": "mg"}],
        "form": "comprimido",
        "content": {"value": "20", "unit": "un"},
    }
    assert (
        await api.post(url, json=body, headers=H(api, "pao"))
    ).status_code == 428  # sin If-Match
    assert (
        await api.post(url, json=body, headers=H(api, "ana", **{"If-Match": '"1"'}))
    ).status_code == 403
    bad = await api.post(
        url,
        json={"strengths": [{"value": "400", "unit": "xyz"}]},
        headers=H(api, "pao", **{"If-Match": '"1"'}),
    )
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_UNIT"
    ok = await api.post(url, json=body, headers=H(api, "pao", **{"If-Match": '"1"'}))
    assert ok.status_code == 200 and ok.headers["etag"] == '"2"'
    a = ok.json()["attributes"]
    assert a["identity_complete"] is True and a["unassigned"] == []
    assert a["strengths"][0]["basis"] == "confirmed" and a["form"]["basis"] == "confirmed"
    assert ok.json()["target_quantity"] is None  # confirmar identidad no toca la cantidad
    stale = await api.post(url, json=body, headers=H(api, "pao", **{"If-Match": '"1"'}))
    assert stale.status_code == 409 and stale.json()["error"]["details"]["current_version"] == 2


async def test_openapi_incluye_confirmacion_y_alias(api):
    spec = (await api.get("/openapi.json")).json()
    for p in ["/api/v1/demand-items/{item_id}/resolve", "/api/v1/product-aliases",
              "/api/v1/product-aliases/{alias_id}/deactivate"]:  # fmt: skip
        assert p in spec["paths"]
    props = spec["components"]["schemas"]["AttributesOut"]["properties"]
    assert {
        "strengths",
        "content",
        "form",
        "packaging",
        "release",
        "unassigned",
        "pending_validation",
    } <= set(props)


async def test_api_expone_el_origen_de_la_cantidad_solicitada(api, ctx):
    """D-147: Pao distingue una cantidad informada de la unidad predeterminada del bot."""
    default = await ctx.report(
        "ana", "falta loratadina 10 mg", confirmation_mode="telegram"
    )
    explicit = await ctx.report(
        "ana", "falta omeprazol 20 mg", declared_quantity=Decimal(2),
        declared_unit="box", confirmation_mode="telegram",
    )  # fmt: skip
    for result, expected in ((default, ("1", "unit", "default")),
                             (explicit, ("2", "box", "explicit"))):  # fmt: skip
        body = (
            await api.get(f"/api/v1/demand-items/{result.item.id}", headers=H(api, "pao"))
        ).json()
        assert (
            body["requested_quantity"], body["requested_unit"],
            body["requested_quantity_origin"],
        ) == expected  # fmt: skip
        assert body["target_quantity"] is None  # solicitud del auxiliar ≠ cantidad de compra
        (obs,) = body["observations"]
        assert obs["quantity_origin"] == expected[2]
