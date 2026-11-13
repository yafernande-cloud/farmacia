"""Ejecuta el golden set contra OpenAI real y evalúa la salida con el backend.

Este archivo NO se ejecutó para preparar el entregable: hacerlo consume cuota de OpenAI.
Requiere autorización explícita de la responsable del proyecto. Sin `--confirmar-gasto` se
niega a llamar al proveedor y solo muestra el plan de llamadas y el costo estimado.

Qué mide:
* envía el mismo caso, con el mismo modelo y la misma imagen, a dos versiones del prompt;
* guarda la salida cruda del modelo, los tokens y la latencia;
* aplica los validadores reales del backend y las verificaciones declaradas en `casos.json`;
* informa aciertos por caso y por versión. No calcula una «mejora» sin que existan resultados.

Qué NO hace:
* no cambia los prompts de la aplicación (lee las versiones históricas de `prompts/`);
* no escribe en PostgreSQL ni toca producción;
* no reintenta silenciosamente: cada llamada es una llamada cobrada.

Uso previsto (tras autorización):
    export OPENAI_API_KEY=...
    backend/.venv/bin/python prompts/evaluacion/evaluar_openai.py --plan
    backend/.venv/bin/python prompts/evaluacion/evaluar_openai.py \
        --confirmar-gasto --max-llamadas 60 --repeticiones 3

Variables:
    OPENAI_MODEL          modelo para observaciones (sin valor por defecto en la app)
    OPENAI_VISION_MODEL   modelo para visión (sin valor por defecto en la app)
    OPENAI_BASE_URL       por defecto https://api.openai.com/v1
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "prompts" / "evaluacion"))

from app.interpretation.commercial import (  # noqa: E402
    OUTPUT_SCHEMA_NAME,
    ObservationProposal,
    build_prompt,
)
from app.interpretation.openai_vision import _schema  # noqa: E402
from evaluar_validacion import _observacion, _vision  # noqa: E402

CASOS = REPO / "prompts" / "golden-set" / "casos.json"
IMAGENES = REPO / "prompts" / "golden-set" / "imagenes"
SALIDA = REPO / "prompts" / "evaluacion" / "resultados-openai.json"

# Tarifas del repositorio (tests/test_observation_ai.py), por millón de tokens.
# Si la tarifa real del modelo elegido difiere, se ajusta con las variables de entorno.
USD_ENTRADA = Decimal(os.getenv("OPENAI_USD_INPUT_PER_MILLION", "0.75"))
USD_SALIDA = Decimal(os.getenv("OPENAI_USD_OUTPUT_PER_MILLION", "4.50"))
TOKENS_ENTRADA_ESTIMADOS = int(os.getenv("OPENAI_INPUT_TOKENS_ESTIMATE", "2000"))
TOKENS_SALIDA_ESTIMADOS = int(os.getenv("OPENAI_OUTPUT_TOKENS_ESTIMATE", "350"))
TOKENS_IMAGEN_ESTIMADOS = int(os.getenv("OPENAI_IMAGE_TOKENS_ESTIMATE", "1000"))

# Matriz mínima para una comparación defendible: casos que discriminan cada cambio.
# Se puede ampliar con --casos; el plan siempre muestra el total antes de llamar.
MATRIZ = {
    "vision-v1_vs_vision-v3": {
        "tipo": "vision",
        "versiones": ["vision-v1", "vision-v3"],
        "casos": ["V1", "V2", "V3", "V4", "V6"],
    },
    "observaciones-v1_vs_v2": {
        "tipo": "observaciones",
        "versiones": ["observaciones-v1", "observaciones-v2"],
        "casos": ["O2", "O7"],
    },
    "observaciones-v2_vs_v3": {
        "tipo": "observaciones",
        "versiones": ["observaciones-v2", "observaciones-v3"],
        "casos": ["O1", "O10"],
    },
}

PROMPTS = {
    "vision-v1": REPO / "prompts" / "vision" / "prompt-v1.md",
    "vision-v2": REPO / "prompts" / "vision" / "prompt-v2.md",
    "vision-v3": REPO / "prompts" / "vision" / "prompt-v3.md",
    "observaciones-v1": REPO / "prompts" / "observaciones" / "prompt-v1.md",
    "observaciones-v2": REPO / "prompts" / "observaciones" / "prompt-v2.md",
    "observaciones-v3": REPO / "prompts" / "observaciones" / "prompt-v3.md",
}


def _prompt(version: str) -> str:
    texto = PROMPTS[version].read_text()
    match = re.search(r"```text\n(.*?)\n```", texto, re.DOTALL)
    if not match:
        raise SystemExit(f"No se encontró el bloque ```text``` en {PROMPTS[version]}")
    return match.group(1)


def _casos() -> dict[str, list[dict[str, Any]]]:
    return json.loads(CASOS.read_text())


def _seleccionados(
    datos: dict[str, list[dict[str, Any]]],
    tipo: str,
    ids: list[str],
) -> list[dict[str, Any]]:
    indice = {c["id"]: c for c in datos[tipo]}
    faltan = [i for i in ids if i not in indice]
    if faltan:
        raise SystemExit(f"Casos inexistentes en {tipo}: {', '.join(faltan)}")
    return [indice[i] for i in ids]


def _trabajos(
    matrices: list[str],
    repeticiones: int,
    solo_casos: list[str] | None,
) -> list[dict[str, Any]]:
    datos = _casos()
    trabajos: list[dict[str, Any]] = []
    for nombre in matrices:
        matriz = MATRIZ[nombre]
        ids = solo_casos or matriz["casos"]
        casos = _seleccionados(datos, matriz["tipo"], ids)
        for version in matriz["versiones"]:
            for caso in casos:
                for repeticion in range(1, repeticiones + 1):
                    trabajos.append(
                        {
                            "matriz": nombre,
                            "tipo": matriz["tipo"],
                            "version": version,
                            "caso": caso,
                            "repeticion": repeticion,
                        }
                    )
    return trabajos


def _estimacion(trabajos: list[dict[str, Any]]) -> dict[str, Any]:
    por_tipo: dict[str, int] = {}
    entrada = Decimal(0)
    salida = Decimal(0)
    for trabajo in trabajos:
        tipo = trabajo["tipo"]
        por_tipo[tipo] = por_tipo.get(tipo, 0) + 1
        tokens_in = TOKENS_ENTRADA_ESTIMADOS + (
            TOKENS_IMAGEN_ESTIMADOS if tipo == "vision" else 0
        )
        entrada += Decimal(tokens_in)
        salida += Decimal(TOKENS_SALIDA_ESTIMADOS)
    costo = (entrada * USD_ENTRADA + salida * USD_SALIDA) / Decimal(1_000_000)
    return {
        "llamadas": len(trabajos),
        "por_tipo": por_tipo,
        "tokens_entrada_estimados": int(entrada),
        "tokens_salida_estimados": int(salida),
        "usd_entrada_por_millon": str(USD_ENTRADA),
        "usd_salida_por_millon": str(USD_SALIDA),
        "costo_estimado_usd": str(costo.quantize(Decimal("0.0001"))),
        "advertencia": (
            "Estimación con tarifas y tokens supuestos; el costo real depende del modelo, "
            "del tamaño de la imagen y de la longitud de la salida."
        ),
    }


async def _post(
    cliente: httpx.AsyncClient,
    *,
    modelo: str,
    system: str,
    user: str,
    schema: dict,
    imagen_png: bytes | None = None,
) -> dict[str, Any]:
    contenido_usuario: list[dict[str, Any]] = [
        {"type": "input_text", "text": user}
    ]
    if imagen_png is not None:
        contenido_usuario.append(
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,"
                + base64.b64encode(imagen_png).decode("ascii"),
                "detail": "auto",
            }
        )
    payload = {
        "model": modelo,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": system}]},
            {"role": "user", "content": contenido_usuario},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "vision_extraction" if imagen_png is not None else OUTPUT_SCHEMA_NAME,
                "schema": schema,
                "strict": True,
            }
        },
        "max_output_tokens": 2500,
        "reasoning": {"effort": "low"},
        "store": False,
    }
    inicio = time.monotonic()
    respuesta = await cliente.post("/responses", json=payload)
    latencia = int((time.monotonic() - inicio) * 1000)
    if respuesta.status_code >= 400:
        codigo = None
        try:
            cuerpo = respuesta.json()
            error = cuerpo.get("error") if isinstance(cuerpo, dict) else None
            if isinstance(error, dict):
                codigo = error.get("code") or error.get("type")
        except ValueError:
            pass
        return {
            "error": f"http_{respuesta.status_code}",
            "provider_code": codigo,
            "latencia_ms": latencia,
        }
    datos = respuesta.json()
    if not isinstance(datos, dict):
        return {"error": "respuesta_no_objeto", "latencia_ms": latencia}
    textos = []
    for item in datos.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for bloque in item.get("content") or []:
            if isinstance(bloque, dict) and bloque.get("type") == "output_text":
                textos.append(bloque.get("text") or "")
    uso = datos.get("usage") or {}
    return {
        "texto": "\n".join(textos).strip(),
        "estado": datos.get("status"),
        "modelo": datos.get("model") or modelo,
        "tokens_entrada": uso.get("input_tokens"),
        "tokens_salida": uso.get("output_tokens"),
        "latencia_ms": latencia,
    }


def _schema_observaciones(version: str) -> dict:
    schema = ObservationProposal.model_json_schema()
    if version in {"observaciones-v1", "observaciones-v2"}:
        # v1/v2 no conocían `article_operations`: se excluye también del esquema enviado.
        schema["properties"].pop("article_operations", None)
        schema["required"] = [
            campo for campo in schema.get("required", []) if campo != "article_operations"
        ]
    return schema


def _normalizar(version: str, crudo: dict[str, Any]) -> dict[str, Any]:
    if version in {"observaciones-v1", "observaciones-v2"}:
        crudo.setdefault("article_operations", [])
    return crudo


def _evaluar(trabajo: dict[str, Any], salida: dict[str, Any]) -> dict[str, Any]:
    caso = dict(trabajo["caso"])
    if "error" in salida or not salida.get("texto"):
        return {
            "estado": "error_proveedor",
            "detalle": salida.get("error") or salida.get("estado"),
            "verificaciones": [],
            "cumple": False,
        }
    try:
        crudo = json.loads(salida["texto"])
    except json.JSONDecodeError:
        return {
            "estado": "salida_no_json",
            "detalle": salida["texto"][:500],
            "verificaciones": [],
            "cumple": False,
        }
    if not isinstance(crudo, dict):
        return {
            "estado": "salida_no_objeto",
            "detalle": str(crudo)[:500],
            "verificaciones": [],
            "cumple": False,
        }
    if trabajo["tipo"] == "vision":
        caso["respuesta_modelo"] = crudo
        resultado = _vision(caso)
    else:
        caso["respuesta_modelo"] = _normalizar(trabajo["version"], crudo)
        resultado = _observacion(caso)
    checks = resultado.get("verificaciones", [])
    return {
        "estado": resultado.get("estado"),
        "resultado_backend": resultado.get("resultado", resultado),
        "verificaciones": checks,
        "cumple": bool(checks) and all(c["cumple"] for c in checks),
    }


async def _ejecutar(
    trabajos: list[dict[str, Any]],
    *,
    modelo_observaciones: str,
    modelo_vision: str,
    api_key: str,
    base_url: str,
) -> list[dict[str, Any]]:
    resultados: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=60.0,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as cliente:
        for numero, trabajo in enumerate(trabajos, 1):
            caso = trabajo["caso"]
            version = trabajo["version"]
            es_vision = trabajo["tipo"] == "vision"
            sistema = _prompt(version)
            if es_vision:
                png = (IMAGENES / f"{caso['id']}.png").read_bytes()
                usuario = "Lee la fotografía y extrae los datos visibles del producto."
                salida = await _post(
                    cliente,
                    modelo=modelo_vision,
                    system=sistema,
                    user=usuario,
                    schema=_schema(),
                    imagen_png=png,
                )
                hash_imagen = hashlib.sha256(png).hexdigest()
            else:
                sistema_actual, usuario = build_prompt(
                    _contexto_para_prompt(caso["contexto"]), caso["texto"]
                )
                del sistema_actual  # se reemplaza por la versión histórica
                salida = await _post(
                    cliente,
                    modelo=modelo_observaciones,
                    system=sistema,
                    user=usuario,
                    schema=_schema_observaciones(version),
                )
                hash_imagen = None
            evaluacion = _evaluar(trabajo, salida)
            resultados.append(
                {
                    "numero": numero,
                    "matriz": trabajo["matriz"],
                    "tipo": trabajo["tipo"],
                    "caso": caso["id"],
                    "version_prompt": version,
                    "repeticion": trabajo["repeticion"],
                    "modelo_solicitado": (
                        modelo_vision if es_vision else modelo_observaciones
                    ),
                    "modelo_respuesta": salida.get("modelo"),
                    "hash_prompt": hashlib.sha256(sistema.encode()).hexdigest(),
                    "hash_imagen": hash_imagen,
                    "tokens_entrada": salida.get("tokens_entrada"),
                    "tokens_salida": salida.get("tokens_salida"),
                    "latencia_ms": salida.get("latencia_ms"),
                    "salida_cruda": salida.get("texto"),
                    "error_proveedor": salida.get("error"),
                    **evaluacion,
                }
            )
            print(
                f"{numero:02}/{len(trabajos)} {caso['id']:4} {version:20} "
                f"{'OK' if evaluacion['cumple'] else 'NO'} {evaluacion['estado']}",
                flush=True,
            )
    return resultados


def _contexto_para_prompt(datos: dict[str, Any]) -> Any:
    """Reutiliza el contexto del evaluador offline, con UUID ficticios."""
    from evaluar_validacion import _contexto

    return _contexto(datos)[0]


def _resumen(resultados: list[dict[str, Any]]) -> dict[str, Any]:
    resumen: dict[str, Any] = {}
    for clave in ("version_prompt", "caso", "matriz"):
        agrupado: dict[str, dict[str, int]] = {}
        for r in resultados:
            grupo = agrupado.setdefault(str(r[clave]), {"total": 0, "cumple": 0})
            grupo["total"] += 1
            grupo["cumple"] += int(r["cumple"])
        resumen[clave] = agrupado
    return resumen


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matriz", action="append", choices=sorted(MATRIZ))
    parser.add_argument("--casos", help="ids separados por coma; reemplaza los de la matriz")
    parser.add_argument("--repeticiones", type=int, default=3)
    parser.add_argument("--max-llamadas", type=int, default=60)
    parser.add_argument("--modelo", default=os.getenv("OPENAI_MODEL"))
    parser.add_argument("--vision-modelo", default=os.getenv("OPENAI_VISION_MODEL"))
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--salida", type=Path, default=SALIDA)
    parser.add_argument("--plan", action="store_true", help="solo muestra el plan")
    parser.add_argument("--confirmar-gasto", action="store_true")
    args = parser.parse_args()

    matrices = args.matriz or list(MATRIZ)
    solo_casos = [c.strip() for c in args.casos.split(",")] if args.casos else None
    trabajos = _trabajos(matrices, args.repeticiones, solo_casos)
    estimacion = _estimacion(trabajos)

    print(json.dumps(estimacion, ensure_ascii=False, indent=2))
    if args.plan:
        return 0
    if not args.confirmar_gasto:
        print("\nSin --confirmar-gasto no se llama a OpenAI.", file=sys.stderr)
        return 2
    if not args.modelo or not args.vision_modelo:
        raise SystemExit(
            "Faltan OPENAI_MODEL y/o OPENAI_VISION_MODEL. La aplicación no les da valor "
            "por defecto; indícalos con --modelo y --vision-modelo."
        )
    if len(trabajos) > args.max_llamadas:
        raise SystemExit(
            f"El plan son {len(trabajos)} llamadas y el límite es {args.max_llamadas}. "
            "Ajusta --max-llamadas solo con autorización."
        )
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Falta OPENAI_API_KEY en el entorno.")

    resultados = await _ejecutar(
        trabajos,
        modelo_observaciones=args.modelo,
        modelo_vision=args.vision_modelo,
        api_key=api_key,
        base_url=args.base_url,
    )
    informe = {
        "generado_en": datetime.now(UTC).isoformat(timespec="seconds"),
        "tipo": "llamadas_reales_a_openai",
        "advertencia": (
            "Resultados reales del modelo elegido. No se extrapolan a otros modelos ni a "
            "producción. Cada repetición es una llamada cobrada."
        ),
        "estimacion_previa": estimacion,
        "repeticiones": args.repeticiones,
        "resultados": resultados,
        "resumen": _resumen(resultados),
    }
    args.salida.parent.mkdir(parents=True, exist_ok=True)
    args.salida.write_text(json.dumps(informe, ensure_ascii=False, indent=2) + "\n")
    print(f"\nInforme: {args.salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
