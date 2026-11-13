"""Evalúa el golden set contra los validadores reales, sin llamar a OpenAI.

Qué hace y qué NO hace:
* Lee `prompts/golden-set/casos.json`.
* Para visión, aplica `_extraction` y `read_extraction` del backend a la salida que se
  entrega en el caso (una salida de modelo pregrabada, no una llamada real).
* Para observaciones, aplica `validate_proposal` a la propuesta pregrabada.
* Comprueba el contrato de salida y las reglas deterministas del backend.

NO mide la calidad del modelo ni del prompt: la salida ya viene escrita en el caso. Sirve
para demostrar que el backend acepta, rechaza y transforma exactamente lo que dice el
diseño. La evaluación real del prompt requiere llamar a OpenAI (ver `evaluar_openai.py`).

Uso:
    backend/.venv/bin/python prompts/evaluacion/evaluar_validacion.py
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from app.interpretation.commercial import (  # noqa: E402
    ArticleResolution,
    CommercialContext,
    ContextArticle,
    ContextSupplier,
    ObservationProposal,
    ProposalRejected,
    build_prompt,
    validate_proposal,
)
from app.interpretation.openai_vision import _extraction  # noqa: E402
from app.interpretation.vision import VisionFailed, read_extraction  # noqa: E402

CASOS = REPO / "prompts" / "golden-set" / "casos.json"
SALIDA = REPO / "prompts" / "evaluacion" / "resultados-validacion.json"


def _vision(caso: dict[str, Any]) -> dict[str, Any]:
    if caso.get("respuesta_modelo") is None:
        return {
            "id": caso["id"],
            "titulo": caso["titulo"],
            "estado": "brecha",
            "detalle": caso.get("brecha", "Caso sin salida de modelo definida."),
            "verificaciones": [],
        }
    try:
        ext = _extraction(caso["respuesta_modelo"], {"model": "modelo-simulado"}, "modelo-simulado")
    except VisionFailed as exc:
        return {
            "id": caso["id"],
            "titulo": caso["titulo"],
            "estado": "rechazada",
            "codigo": exc.code,
            "detalle": exc.detail,
            "verificaciones": _verificar(caso, _base_vision(None, exc)),
        }
    reading = read_extraction(ext)
    parsed = reading.parsed
    campos: dict[str, list[str]] = {}
    for field in ext.fields:
        campos.setdefault(field.attribute, []).append(field.text)
    result = _base_vision(ext, None)
    result.update(
        {
            "campos": campos,
            "warnings": list(ext.warnings),
            "reading": {
                "status": reading.status.value,
                "display_name": parsed.display_name if parsed else None,
                "search_query": parsed.search_query if parsed else None,
                "variant": (
                    parsed.variant.value if parsed and parsed.variant is not None else None
                ),
                "uncertainties": list(reading.uncertainties),
                "ignored": list(reading.ignored),
            },
        }
    )
    return {
        "id": caso["id"],
        "titulo": caso["titulo"],
        "estado": "aceptada",
        "verificaciones": _verificar(caso, result),
        "resultado": result,
    }


def _base_vision(ext: Any, error: VisionFailed | None) -> dict[str, Any]:
    if error is not None:
        return {"estado": "rechazada", "codigo": error.code, "detalle": error.detail}
    return {
        "estado": "aceptada",
        "legibility": ext.legibility,
        "campos": {},
        "warnings": [],
        "reading": {},
    }


def _contexto(datos: dict[str, Any]) -> tuple[CommercialContext, Any]:
    trade_item = uuid.uuid4()
    suppliers = tuple(
        ContextSupplier(uuid.uuid4(), nombre) for nombre in datos.get("suppliers", [])
    )
    ctx = CommercialContext(
        trade_item_id=trade_item,
        display_name=datos["display_name"],
        ingredient=datos.get("ingredient"),
        item_laboratory=None,
        item_brand=None,
        suppliers=suppliers,
        laboratories=tuple(datos.get("laboratories", [])),
        brands=tuple(datos.get("brands", [])),
        currencies=tuple(datos.get("currencies", ["CLP"])),
        known_laboratories=tuple(datos.get("known_laboratories", [])),
        known_brands=tuple(datos.get("known_brands", [])),
    )
    otro = uuid.uuid4()

    def resolver(label: str) -> ArticleResolution:
        limpio = label.strip().lower()
        if limpio in {"este producto", datos["display_name"].lower()}:
            return ArticleResolution(
                "resolved", ContextArticle(trade_item, datos["display_name"])
            )
        if limpio == "migranol de caja 20":
            return ArticleResolution("resolved", ContextArticle(otro, "Migranol caja 20"))
        return ArticleResolution("not_found")

    return ctx, resolver


def _observacion(caso: dict[str, Any]) -> dict[str, Any]:
    ctx, resolver = _contexto(caso["contexto"])
    propuesta = ObservationProposal.model_validate(caso["respuesta_modelo"])
    _, user = build_prompt(ctx, caso["texto"])
    neutralizado = "<<<_OBSERVACION" in user and "OBSERVACION_>>>" in user
    try:
        validada = validate_proposal(propuesta, ctx, caso["texto"], resolve_article=resolver)
    except ProposalRejected as exc:
        result = {
            "estado": "rechazada",
            "codigo": exc.code,
            "aclaracion": exc.clarification is not None,
            "preferencias": [],
            "preferencias_articulo": [],
            "retiros": [],
            "prompt_neutralizado": neutralizado,
        }
        return {
            "id": caso["id"],
            "titulo": caso["titulo"],
            "estado": "rechazada",
            "verificaciones": _verificar(caso, result),
            "resultado": result,
        }
    result = {
        "estado": "aceptada",
        "preferencias": [_pref(p) for p in validada.preferences],
        "preferencias_articulo": [_pref_articulo(p) for p in validada.article_preferences],
        "retiros": list(validada.retired_subject_kinds),
        "prompt_neutralizado": neutralizado,
    }
    return {
        "id": caso["id"],
        "titulo": caso["titulo"],
        "estado": "aceptada",
        "verificaciones": _verificar(caso, result),
        "resultado": result,
    }


def _pref(p: Any) -> dict[str, Any]:
    return {
        "subject_kind": p.subject_kind,
        "subject_label": p.subject_label,
        "effect": p.effect,
        "tolerance_kind": p.tolerance_kind,
        "price_reference_kind": p.price_reference_kind,
        "price_reference_amount": _numero(p.price_reference_amount),
        "confidence": _numero(p.confidence),
    }


def _pref_articulo(p: Any) -> dict[str, Any]:
    return {
        "article_label": p.article_label,
        "relative_to_article_label": p.relative_to_article_label,
        "effect": p.effect,
        "confidence": _numero(p.confidence),
    }


def _numero(valor: Decimal | None) -> float | None:
    return float(valor) if valor is not None else None


def _obtener(datos: Any, ruta: str) -> Any:
    actual = datos
    for parte in ruta.split("."):
        if isinstance(actual, dict):
            actual = actual.get(parte)
        elif isinstance(actual, list):
            try:
                actual = actual[int(parte)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if actual is None:
            return None
    return actual


def _verificar(caso: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    salida = []
    for chequeo in caso.get("verificar", []):
        obtenido = _obtener(result, chequeo["ruta"])
        if "igual" in chequeo:
            ok = obtenido == chequeo["igual"]
        elif chequeo.get("es_null"):
            ok = obtenido is None
        elif "contiene" in chequeo:
            esperado = chequeo["contiene"]
            ok = esperado in obtenido if isinstance(obtenido, (list, tuple, str)) else False
        elif "no_contiene" in chequeo:
            esperado = chequeo["no_contiene"]
            ok = (
                esperado not in obtenido
                if isinstance(obtenido, (list, tuple, str))
                else True
            )
        elif "longitud" in chequeo:
            try:
                ok = len(obtenido) == chequeo["longitud"]
            except TypeError:
                ok = False
        else:
            ok = False
        salida.append(
            {
                "ruta": chequeo["ruta"],
                "esperado": {
                    k: v for k, v in chequeo.items() if k != "ruta"
                },
                "obtenido": obtenido,
                "cumple": ok,
            }
        )
    return salida


def main() -> int:
    datos = json.loads(CASOS.read_text())
    resultados = []
    for caso in datos["vision"]:
        resultados.append(_vision(caso))
    for caso in datos["observaciones"]:
        resultados.append(_observacion(caso))

    total_checks = sum(len(r["verificaciones"]) for r in resultados)
    checks_ok = sum(
        1 for r in resultados for c in r["verificaciones"] if c["cumple"]
    )
    casos_ok = sum(
        1
        for r in resultados
        if r["verificaciones"] and all(c["cumple"] for c in r["verificaciones"])
    )
    casos_evaluados = sum(1 for r in resultados if r["verificaciones"])

    informe = {
        "generado_en": datetime.now(UTC).isoformat(timespec="seconds"),
        "tipo": "validacion_offline_sin_openai",
        "advertencia": (
            "Las salidas del modelo están pregrabadas en casos.json. Esto valida el contrato "
            "y las reglas del backend, no la calidad del prompt ni del modelo real."
        ),
        "resumen": {
            "casos": len(resultados),
            "casos_con_verificaciones": casos_evaluados,
            "casos_cumplen_todo": casos_ok,
            "verificaciones": total_checks,
            "verificaciones_cumplen": checks_ok,
        },
        "resultados": resultados,
    }
    SALIDA.write_text(json.dumps(informe, ensure_ascii=False, indent=2) + "\n")

    print(f"{'Caso':4} {'Estado':11} {'Checks':>7}  Título")
    for r in resultados:
        checks = r["verificaciones"]
        estado = "OK" if checks and all(c["cumple"] for c in checks) else r["estado"]
        if r["estado"] == "brecha":
            estado = "BRECHA"
        print(f"{r['id']:4} {estado:11} {sum(c['cumple'] for c in checks):>3}/{len(checks):<3}  {r['titulo']}")
    print()
    print(f"Casos que cumplen todo: {casos_ok}/{casos_evaluados} evaluados")
    print(f"Verificaciones: {checks_ok}/{total_checks}")
    print(f"Informe: {SALIDA.relative_to(REPO)}")
    return 0 if checks_ok == total_checks else 1


if __name__ == "__main__":
    raise SystemExit(main())
