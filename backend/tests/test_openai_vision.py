"""Visión con OpenAI: adaptador HTTP y traducción determinista, SIN llamadas pagadas.

El transporte es `httpx.MockTransport`: se verifica el contrato de la Responses API (imagen,
esquema estricto, `store=false`) y el manejo de errores con respuestas controladas.
"""

import json

import httpx
import pytest

from app.interpretation.openai_vision import OpenAIVisionInterpreter
from app.interpretation.vision import (
    VisionExtraction,
    VisionFailed,
    VisionField,
    read_extraction,
)
from tests.tgsim import fake_jpeg


def _ok(body: dict, *, model: str = "gpt-vision-test") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(body)}],
                }
            ],
        },
    )


def _interpreter(handler, **kw):
    return OpenAIVisionInterpreter(
        api_key="clave-de-prueba",
        model="gpt-vision-test",
        base_url="https://api.test/v1",
        transport=httpx.MockTransport(handler),
        **kw,
    )


async def test_envia_la_imagen_y_devuelve_solo_lo_visible():
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        seen["auth"] = req.headers.get("authorization")
        return _ok(
            {
                "legibility": "clear",
                "fields": [
                    {"attribute": "ingredient", "text": "crema de manos", "confidence": 0.95},
                    {"attribute": "variant", "text": "Berry Warm", "confidence": 0.9},
                    {"attribute": "content", "text": "50 ml", "confidence": 0.95},
                    {"attribute": "visible_quantity", "text": "2 cajas", "confidence": 0.8},
                ],
                "warnings": [],
            }
        )

    interp = _interpreter(handler)
    ext = await interp.extract(fake_jpeg(1), "image/jpeg", "faltan 2 cajas")

    assert seen["model"] == "gpt-vision-test" and seen["store"] is False
    assert seen["auth"] == "Bearer clave-de-prueba"
    user = seen["input"][1]["content"]
    assert any(
        part.get("type") == "input_image"
        and part.get("image_url", "").startswith("data:image/jpeg;base64,")
        for part in user
    )
    assert seen["text"]["format"]["strict"] is True
    assert seen["text"]["format"]["schema"]["additionalProperties"] is False
    assert ext.mode == "live" and ext.provider == "openai" and ext.legibility == "clear"
    assert {(f.attribute, f.text) for f in ext.fields} >= {
        ("ingredient", "crema de manos"),
        ("variant", "Berry Warm"),
    }

    reading = read_extraction(ext)
    assert reading.parsed is not None
    assert reading.parsed.ingredient == "crema de manos"
    assert reading.parsed.variant is not None and reading.parsed.variant.value == "Berry Warm"
    assert "variante Berry Warm" in reading.parsed.display_name
    assert reading.parsed.content is not None and reading.parsed.content.measure.unit == "ml"
    assert reading.parsed.strengths == ()  # una cantidad visible no se inventa como dosis
    assert all(
        a.origin == "image" and a.basis.value == "inferred"
        for a in (
            reading.parsed.variant,
            reading.parsed.form,
            reading.parsed.content,
        )
        if a is not None
    )
    assert "visible_quantity_not_a_requested_quantity" in reading.ignored


async def test_timeout_se_reintenta_una_vez_y_luego_falla_sin_colgar():
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("sin respuesta", request=req)

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(2), "image/jpeg", None)
    assert exc.value.code == "timeout" and calls == 2


async def test_respuesta_invalida_no_se_reintenta_ni_se_interpreta():
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "gpt-vision-test",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"legibility": "clear"}'}],
                    }
                ],
            },
        )

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(3), "image/jpeg", None)
    assert exc.value.code == "invalid_response" and calls == 1


async def test_cuota_agotada_no_se_reintenta_y_conserva_metadatos():
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            json={"error": {"code": "insufficient_quota", "message": "sin cuota"}},
            headers={"x-request-id": "req_cuota"},
        )

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(4), "image/jpeg", None)
    failure = exc.value
    assert failure.code == "quota_exceeded" and calls == 1
    assert (failure.http_status, failure.provider_code, failure.request_id) == (
        429,
        "insufficient_quota",
        "req_cuota",
    )


@pytest.mark.parametrize(
    ("status", "provider_code", "expected"),
    [
        (400, "invalid_value", "http_400"),
        (401, "invalid_api_key", "http_401"),
        (403, "permission_denied", "http_403"),
        (404, "model_not_found", "http_404"),
    ],
)
async def test_cada_error_http_se_distingue_y_registra_sin_filtrar_el_cuerpo(
    status, provider_code, expected
):
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": provider_code,
                    "type": "invalid_request_error",
                    "message": "detalle interno que no debe guardarse",
                }
            },
            headers={"x-request-id": f"req_{status}"},
        )

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(10 + status % 10), "image/jpeg", None)
    failure = exc.value
    assert failure.code == expected and calls == 1  # un 4xx de configuración no se reintenta
    assert (failure.http_status, failure.provider_code, failure.request_id) == (
        status,
        provider_code,
        f"req_{status}",
    )
    # `str()` solo expone el código interno: ni el mensaje del proveedor ni la clave.
    assert str(failure) == expected
    assert "detalle interno" not in repr(failure.__dict__)


async def test_429_de_tasa_se_reintenta_una_vez_y_conserva_el_request_id():
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            json={"error": {"code": "rate_limit_exceeded"}},
            headers={"x-request-id": "req_tasa"},
        )

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(20), "image/jpeg", None)
    failure = exc.value
    assert failure.code == "http_429" and calls == 2
    assert (failure.http_status, failure.provider_code, failure.request_id) == (
        429,
        "rate_limit_exceeded",
        "req_tasa",
    )


async def test_5xx_se_reintenta_y_no_se_confunde_con_un_4xx():
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            json={"error": {"code": "server_error"}},
            headers={"x-request-id": "req_5xx"},
        )

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(21), "image/jpeg", None)
    assert exc.value.code == "http_5xx" and calls == 2
    assert exc.value.http_status == 503 and exc.value.request_id == "req_5xx"


async def test_error_de_conectividad_no_se_presenta_como_error_http():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=req)

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(22), "image/jpeg", None)
    assert exc.value.code == "unavailable"
    assert exc.value.http_status is None and exc.value.request_id is None


async def test_respuesta_failed_del_proveedor_conserva_su_codigo_y_request_id():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "failed", "error": {"code": "server_error", "message": "no"}},
            headers={"x-request-id": "req_failed"},
        )

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(23), "image/jpeg", None)
    failure = exc.value
    assert failure.code == "provider_failed" and failure.http_status == 200
    assert failure.provider_code == "server_error" and failure.request_id == "req_failed"


async def test_metadatos_del_proveedor_se_sanean_antes_de_registrarse():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "model not found <script>"}},
            headers={"x-request-id": "req 1 <script>"},
        )

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(24), "image/jpeg", None)
    assert exc.value.provider_code == "modelnotfoundscript"
    assert exc.value.request_id == "req1script"


async def test_salida_truncada_nunca_se_parsea():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
        )

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(5), "image/jpeg", None)
    assert exc.value.code == "output_truncated"


def test_un_atributo_unico_repetido_con_valores_distintos_es_salida_ambigua():
    from app.interpretation.openai_vision import _extraction

    body = {
        "legibility": "clear",
        "fields": [
            {"attribute": "content", "text": "20 comprimidos", "confidence": 0.9},
            {"attribute": "content", "text": "30 comprimidos", "confidence": 0.9},
        ],
        "warnings": [],
    }
    with pytest.raises(VisionFailed) as exc:
        _extraction(body, {"model": "gpt-vision-test"}, "gpt-vision-test")
    assert exc.value.detail == "duplicate_attribute:content"


async def test_medicamento_combinado_conserva_los_dos_principios_y_sus_dosis():
    """Tramadol/paracetamol 37,5/325 mg: dos `ingredient` y dos `strength` son legítimos."""

    def handler(req: httpx.Request) -> httpx.Response:
        return _ok(
            {
                "legibility": "clear",
                "fields": [
                    {"attribute": "product_name", "text": "Tramadol/Paracetamol", "confidence": 0.95},
                    {"attribute": "ingredient", "text": "tramadol clorhidrato", "confidence": 0.9},
                    {"attribute": "strength", "text": "37,5 mg", "confidence": 0.9},
                    {"attribute": "ingredient", "text": "paracetamol", "confidence": 0.9},
                    {"attribute": "strength", "text": "325 mg", "confidence": 0.9},
                    {"attribute": "form", "text": "comprimidos", "confidence": 0.95},
                    {"attribute": "content", "text": "30 comprimidos", "confidence": 0.95},
                ],
                "warnings": [],
            }
        )

    ext = await _interpreter(handler).extract(fake_jpeg(33), "image/jpeg", None)
    ingredients = [f.text for f in ext.fields if f.attribute == "ingredient"]
    strengths = [f.text for f in ext.fields if f.attribute == "strength"]
    assert ingredients == ["tramadol clorhidrato", "paracetamol"]
    assert strengths == ["37,5 mg", "325 mg"]

    reading = read_extraction(ext)
    parsed = reading.parsed
    assert parsed is not None and reading.status.value == "identified"
    assert parsed.ingredient == "tramadol clorhidrato paracetamol"
    assert [(s.measure.value, s.measure.unit) for s in parsed.strengths] == [
        (37.5, "mg"),
        (325, "mg"),
    ]
    assert parsed.form.value == "comprimido" and parsed.content.measure.value == 30
    # El nombre comercial que se lee se conserva como marca (no se deduce nada más).
    assert parsed.brand.value == "Tramadol/Paracetamol"


async def test_un_texto_largo_valido_se_recorta_y_no_anula_la_lectura():
    """El esquema no limita el largo de `text`: un dato largo no puede perder toda la foto."""
    large = "aqua glycerin cetearyl alcohol " * 20  # > 200 caracteres
    assert len(large) > 200

    def handler(req: httpx.Request) -> httpx.Response:
        return _ok(
            {
                "legibility": "clear",
                "fields": [
                    {"attribute": "ingredient", "text": large, "confidence": 0.9},
                    {"attribute": "content", "text": "50 ml", "confidence": 0.95},
                ],
                "warnings": [],
            }
        )

    ext = await _interpreter(handler).extract(fake_jpeg(25), "image/jpeg", None)
    fields = {f.attribute: f.text for f in ext.fields}
    assert len(fields["ingredient"]) == 200 and fields["content"] == "50 ml"


async def test_una_repeticion_identica_se_colapsa_y_no_anula_la_lectura():
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok(
            {
                "legibility": "clear",
                "fields": [
                    {"attribute": "content", "text": "50 ml", "confidence": 0.9},
                    {"attribute": "content", "text": " 50 ml ", "confidence": 0.8},
                ],
                "warnings": [],
            }
        )

    ext = await _interpreter(handler).extract(fake_jpeg(26), "image/jpeg", None)
    assert [(f.attribute, f.text) for f in ext.fields] == [("content", "50 ml")]


async def test_un_campo_vacio_se_ignora_sin_perder_el_resto():
    def handler(req: httpx.Request) -> httpx.Response:
        return _ok(
            {
                "legibility": "partial",
                "fields": [
                    {"attribute": "product_name", "text": "   ", "confidence": 0.9},
                    {"attribute": "variant", "text": "Berry Warm", "confidence": 0.9},
                ],
                "warnings": [],
            }
        )

    ext = await _interpreter(handler).extract(fake_jpeg(27), "image/jpeg", None)
    assert [(f.attribute, f.text) for f in ext.fields] == [("variant", "Berry Warm")]


async def test_respuesta_200_sin_mensaje_registra_la_rama_no_message():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "completed", "output": []},
            headers={"x-request-id": "req_sin_msg"},
        )

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(28), "image/jpeg", None)
    failure = exc.value
    assert failure.code == "invalid_response" and failure.detail == "no_message"
    assert (failure.http_status, failure.request_id) == (200, "req_sin_msg")


async def test_una_negativa_del_modelo_se_registra_sin_guardar_su_texto():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "no puedo ayudarte"}],
                    }
                ],
            },
        )

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(29), "image/jpeg", None)
    assert exc.value.code == "invalid_response" and exc.value.detail == "refusal"
    assert "no puedo ayudarte" not in repr(exc.value.__dict__)


async def test_salida_que_no_es_json_registra_la_rama_output_not_json():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "no es json"}],
                    }
                ],
            },
        )

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(30), "image/jpeg", None)
    assert exc.value.code == "invalid_response" and exc.value.detail == "output_not_json"


async def test_simulacion_sigue_marcada_y_no_se_confunde_con_lectura_real():
    from app.interpretation.vision import SimulatedVisionInterpreter

    ext = VisionExtraction(
        "simulated",
        "simulator",
        "simulated-v0",
        "clear",
        (VisionField("ingredient", "ibuprofeno", 0.9),),
    )
    out = await SimulatedVisionInterpreter({"*": ext}).extract(
        fake_jpeg(9), "image/jpeg", None
    )
    assert out.mode == "simulated" and out.provider == "simulator"
    assert out.to_dict()["mode"] == "simulated"
