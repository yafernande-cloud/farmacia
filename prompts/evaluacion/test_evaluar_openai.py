"""Pruebas locales del evaluador de la comparación de prompts. No llaman a OpenAI.

Comprueban, antes de autorizar una corrida real:
* la matriz autorizada (54 llamadas, 2 repeticiones, 3 versiones por tarea);
* que cada versión use su propio esquema y que las tres compartan entradas;
* que el plan quepa en el presupuesto y la reserva por llamada sea mayor que el costo esperado;
* que el evaluador se detenga por presupuesto (guardando lo hecho) y por errores seguidos;
* el criterio estricto y el criterio ampliado de V4, y la separación de O10.

Uso:
    backend/.venv/bin/pytest prompts/evaluacion/test_evaluar_openai.py -q
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import evaluar_openai as ev
import pytest

TARIFAS = (Decimal("0.75"), Decimal("4.50"))


def _trabajos() -> list[dict[str, Any]]:
    return ev._trabajos(["vision", "observaciones"], 2, None)


def test_la_matriz_autorizada_son_54_llamadas() -> None:
    trabajos = _trabajos()
    assert len(trabajos) == 54
    por_tarea = {t: [j for j in trabajos if j["tarea"] == t] for t in ("vision", "observaciones")}
    assert len(por_tarea["vision"]) == 30
    assert len(por_tarea["observaciones"]) == 24
    assert {j["caso"]["id"] for j in por_tarea["vision"]} == {"V1", "V2", "V3", "V4", "V6"}
    assert {j["caso"]["id"] for j in por_tarea["observaciones"]} == {"O1", "O2", "O7", "O10"}
    for tarea, trabajos_tarea in por_tarea.items():
        versiones = {j["version"] for j in trabajos_tarea}
        assert len(versiones) == 3, tarea
        for caso in {j["caso"]["id"] for j in trabajos_tarea}:
            del_caso = [j for j in trabajos_tarea if j["caso"]["id"] == caso]
            assert len(del_caso) == 6  # 3 versiones × 2 repeticiones
            assert sorted(j["repeticion"] for j in del_caso) == [1, 1, 1, 2, 2, 2]


def test_cada_version_usa_su_esquema_de_observaciones() -> None:
    v1 = ev._schema_observaciones("observaciones-v1")
    v2 = ev._schema_observaciones("observaciones-v2")
    v3 = ev._schema_observaciones("observaciones-v3")
    assert "article_operations" not in v1["properties"]
    assert "article_operations" not in v2["properties"]
    assert "article_operations" not in v1["required"]
    assert "article_operations" in v3["properties"]
    assert "article_operations" in v3["required"]
    # Las otras claves no cambian: v1 y v2 son comparables entre sí.
    assert set(v1["properties"]) == set(v2["properties"])
    assert set(v1["properties"]) | {"article_operations"} == set(v3["properties"])


def test_las_tres_versiones_reciben_las_mismas_entradas() -> None:
    trabajos = _trabajos()
    for tarea in ("vision", "observaciones"):
        casos = {j["caso"]["id"] for j in trabajos if j["tarea"] == tarea}
        for caso_id in casos:
            del_caso = [j for j in trabajos if j["tarea"] == tarea and j["caso"]["id"] == caso_id]
            cargas = [ev._carga(j) for j in del_caso]
            assert len({c["user"] for c in cargas}) == 1
            assert len({c["hash_imagen"] for c in cargas}) == 1
            assert len({c["schema_name"] for c in cargas}) == 1
            assert len({c["system"] for c in cargas}) == 3
            assert len({c["hash_imagen"] for c in cargas if c["hash_imagen"]}) <= 1
    # Observaciones: v1 y v2 comparten esquema; v3 agrega la capacidad.
    obs = [j for j in trabajos if j["tarea"] == "observaciones" and j["caso"]["id"] == "O1"]
    esquemas = {j["version"]: ev._carga(j)["schema"] for j in obs}
    assert esquemas["observaciones-v1"] == esquemas["observaciones-v2"]
    assert esquemas["observaciones-v3"] != esquemas["observaciones-v2"]


def test_los_seis_prompts_historicos_se_leen() -> None:
    for version in ev.PROMPTS:
        texto = ev._prompt(version)
        assert len(texto) > 500, version
    assert len(ev._prompt("vision-v3")) > len(ev._prompt("vision-v1"))


def test_el_plan_cabe_en_el_presupuesto_autorizado() -> None:
    estimacion = ev._estimacion(_trabajos(), TARIFAS)
    assert estimacion["llamadas"] == 54
    assert Decimal(estimacion["costo_esperado_usd"]) <= Decimal("0.25")
    assert Decimal(estimacion["costo_reserva_usd"]) > Decimal(estimacion["costo_esperado_usd"])


def test_la_reserva_por_llamada_es_conservadora() -> None:
    carga = ev._carga(_trabajos()[0])
    reserva = ev._reserva(carga, TARIFAS)
    esperado = ev._costo(ev._tokens_plan(carga), ev.TOKENS_SALIDA_ESPERADOS, TARIFAS)
    assert reserva > esperado


def test_el_presupuesto_no_admite_una_llamada_que_no_cabe() -> None:
    presupuesto = ev.Presupuesto(Decimal("0.01"), TARIFAS)
    presupuesto.gastado = Decimal("0.008")
    assert presupuesto.cabe(Decimal("0.001")) is True
    assert presupuesto.cabe(Decimal("0.0025")) is False


def test_un_error_sin_uso_del_proveedor_imputa_la_reserva() -> None:
    presupuesto = ev.Presupuesto(Decimal("1"), TARIFAS)
    costo, origen = presupuesto.registrar({"error": "http_500"}, Decimal("0.005"))
    assert costo == Decimal("0.005")
    assert origen == "reserva"
    assert presupuesto.llamadas == 1


def _ejecutar(
    trabajos: list[dict[str, Any]],
    *,
    salida,
    presupuesto: ev.Presupuesto,
    monkeypatch: pytest.MonkeyPatch,
    respuesta: dict[str, Any],
) -> list[dict[str, Any]]:
    async def falso_post(cliente, **kwargs):  # noqa: ARG001
        return dict(respuesta)

    monkeypatch.setattr(ev, "_post", falso_post)
    plan = ev._plan(trabajos, 2, {"vision": "gpt-5.4-mini"}, TARIFAS)
    return asyncio.run(
        ev._ejecutar(
            trabajos,
            modelo_observaciones="gpt-5.4-mini",
            modelo_vision="gpt-5.4-mini",
            api_key="prueba",
            base_url="https://example.invalid/v1",
            tarifas=TARIFAS,
            presupuesto=presupuesto,
            salida_path=salida,
            plan=plan,
            estimacion=ev._estimacion(trabajos, TARIFAS),
            repeticiones=2,
        )
    )


def test_se_detiene_por_presupuesto_y_guarda_lo_hecho(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trabajos = _trabajos()[:4]
    carga = ev._carga(trabajos[0])
    reserva = ev._reserva(carga, TARIFAS)
    # Alcanza para una llamada (costo simulado 0.0012) y no para la segunda reserva.
    presupuesto = ev.Presupuesto(reserva + Decimal("0.001"), TARIFAS)
    salida = tmp_path / "resultados.json"

    resultados = _ejecutar(
        trabajos,
        salida=salida,
        presupuesto=presupuesto,
        monkeypatch=monkeypatch,
        respuesta={
            "texto": '{"legibility": "unreadable", "fields": [], "warnings": []}',
            "modelo": "gpt-5.4-mini",
            "tokens_entrada": 1000,
            "tokens_salida": 100,
            "latencia_ms": 5,
        },
    )

    assert len(resultados) == 1
    assert presupuesto.detenido_por == "presupuesto"
    guardado = json.loads(salida.read_text())
    assert guardado["completado"] is False
    assert guardado["presupuesto"]["detenido_por"] == "presupuesto"
    assert len(guardado["resultados"]) == 1


def test_tres_errores_seguidos_detienen_la_corrida(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trabajos = _trabajos()[:6]
    presupuesto = ev.Presupuesto(Decimal("1"), TARIFAS)
    salida = tmp_path / "resultados.json"

    resultados = _ejecutar(
        trabajos,
        salida=salida,
        presupuesto=presupuesto,
        monkeypatch=monkeypatch,
        respuesta={"error": "http_401", "provider_code": "invalid_api_key", "latencia_ms": 2},
    )

    assert len(resultados) == ev.MAX_ERRORES_SEGUIDOS
    assert presupuesto.detenido_por == "errores_proveedor"
    assert all(r["cumple"] is False for r in resultados)
    assert all(r["costo_origen"] == "reserva" for r in resultados)


def _resultado(caso: str, version: str, cumple: bool) -> dict[str, Any]:
    return {
        "caso": caso,
        "version_prompt": version,
        "cumple": cumple,
        "verificaciones": [{"cumple": cumple}],
    }


def test_resumen_compara_pares_y_separa_o10() -> None:
    resultados = [
        _resultado("V1", "vision-v1", True),
        _resultado("V1", "vision-v2", True),
        _resultado("V1", "vision-v3", False),
        _resultado("O10", "observaciones-v1", False),
        _resultado("O10", "observaciones-v2", False),
        _resultado("O10", "observaciones-v3", True),
    ]
    resumen = ev._resumen(resultados)
    pares = {(c["caso"], c["desde"], c["hasta"]): c["resultado"] for c in resumen["comparaciones"]}
    assert pares[("V1", "vision-v1", "vision-v2")] == "empate"
    assert pares[("V1", "vision-v2", "vision-v3")] == "retroceso"
    assert pares[("V1", "vision-v1", "vision-v3")] == "retroceso"
    assert all(c["caso"] != "O10" for c in resumen["comparaciones"])
    assert resumen["o10"]["por_version"]["observaciones-v3"]["aciertos"] == 1


def test_criterio_ampliado_de_v4() -> None:
    en_revision = {
        "estado": "aceptada",
        "legibility": "unreadable",
        "campos": {},
        "reading": {"status": "needs_review", "display_name": None},
    }
    assert ev._v4_ampliado(en_revision) is True
    assert ev._v4_ampliado({**en_revision, "legibility": "blurry"}) is True
    assert ev._v4_ampliado({**en_revision, "legibility": "clear"}) is False
    assert ev._v4_ampliado({**en_revision, "campos": {"product_name": ["X"]}}) is False
    assert (
        ev._v4_ampliado({**en_revision, "reading": {"status": "ready", "display_name": "X"}})
        is False
    )
