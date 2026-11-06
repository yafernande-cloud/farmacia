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


async def test_cuota_agotada_no_se_reintenta():
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            json={"error": {"code": "insufficient_quota", "message": "sin cuota"}},
        )

    with pytest.raises(VisionFailed) as exc:
        await _interpreter(handler).extract(fake_jpeg(4), "image/jpeg", None)
    assert exc.value.code == "quota_exceeded" and calls == 1


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


def test_un_atributo_repetido_es_salida_ambigua():
    from app.interpretation.openai_vision import _extraction

    body = {
        "legibility": "clear",
        "fields": [
            {"attribute": "strength", "text": "500 mg", "confidence": 0.9},
            {"attribute": "strength", "text": "600 mg", "confidence": 0.9},
        ],
        "warnings": [],
    }
    with pytest.raises(VisionFailed):
        _extraction(body, {"model": "gpt-vision-test"}, "gpt-vision-test")


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
