"""Ejecuta la comparación real de los prompts contra OpenAI, con tope de gasto.

Qué hace:
* envía los mismos casos, con el mismo modelo y las mismas entradas a las tres versiones de
  cada tarea (Visión y Observaciones Comerciales);
* guarda la salida cruda del modelo, los tokens, la latencia, los hashes del prompt y de la
  imagen, y el costo estimado de cada llamada;
* aplica los validadores reales del backend y las verificaciones declaradas en `casos.json`;
* informa aciertos por caso y por versión, y compara pares de versiones.

Qué NO hace:
* no cambia los prompts de la aplicación (lee las versiones históricas de `prompts/`);
* no escribe en PostgreSQL ni toca producción;
* no reintenta: cada llamada es una llamada cobrada.

Control de gasto:
* antes de llamar comprueba que el costo estimado del plan quepa en `--presupuesto-usd`;
* antes de cada llamada reserva el peor costo razonable de esa llamada y se detiene si la
  reserva no cabe en lo que queda del presupuesto;
* después de cada llamada suma el costo real declarado por el proveedor (tokens × tarifas);
* guarda el informe después de cada llamada, para que una interrupción no pierda resultados.

Uso previsto (tras autorización):
    export OPENAI_API_KEY=...
    backend/.venv/bin/python prompts/evaluacion/evaluar_openai.py --plan
    backend/.venv/bin/python prompts/evaluacion/evaluar_openai.py \
        --confirmar-gasto --repeticiones 2 --max-llamadas 54 --presupuesto-usd 0.25

Variables:
    OPENAI_API_KEY        obligatoria para ejecutar
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
import math
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

from evaluar_validacion import _observacion, _vision  # noqa: E402

from app.interpretation.commercial import (  # noqa: E402
    OUTPUT_SCHEMA_NAME,
    ObservationProposal,
    build_prompt,
)
from app.interpretation.openai_vision import _schema  # noqa: E402

CASOS = REPO / "prompts" / "golden-set" / "casos.json"
IMAGENES = REPO / "prompts" / "golden-set" / "imagenes"
SALIDA = REPO / "prompts" / "evaluacion" / "resultados-openai.json"

# Tarifas publicadas del modelo autorizado, por millón de tokens. Si se usa otro modelo hay que
# declarar sus tarifas con --tarifa-entrada-usd / --tarifa-salida-usd; el plan lo muestra antes
# de llamar.
TARIFAS_CONOCIDAS = {
    "gpt-5.4-mini": (Decimal("0.75"), Decimal("4.50")),
}
PRESUPUESTO_POR_DEFECTO = Decimal("0.25")
MAX_LLAMADAS_POR_DEFECTO = 54
REPETICIONES_POR_DEFECTO = 2
# Tope de salida por llamada. Deja margen para el razonamiento de esfuerzo bajo y el JSON.
MAX_TOKENS_SALIDA = {"vision": 800, "observaciones": 800}
# Tokens de imagen que se reservan antes de conocer el uso real (detail=auto, ~1000 px).
TOKENS_IMAGEN_RESERVA = 2000
# El plan usa una estimación de ~4 caracteres por token; la reserva de seguridad, 2,5.
CHARS_POR_TOKEN_PLAN = 4
CHARS_POR_TOKEN_RESERVA = Decimal("2.5")
# Una salida esperada ronda los 250–400 tokens.
TOKENS_SALIDA_ESPERADOS = 350
# Tres errores seguidos del proveedor detienen la corrida (clave, modelo o red).
MAX_ERRORES_SEGUIDOS = 3

MATRIZ = {
    "vision": {
        "tipo": "vision",
        "versiones": ["vision-v1", "vision-v2", "vision-v3"],
        "casos": ["V1", "V2", "V3", "V4", "V6"],
    },
    "observaciones": {
        "tipo": "observaciones",
        "versiones": ["observaciones-v1", "observaciones-v2", "observaciones-v3"],
        "casos": ["O1", "O2", "O7", "O10"],
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

USUARIO_VISION = "Lee la fotografía y extrae los datos visibles del producto."


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
    tareas: list[str],
    repeticiones: int,
    solo_casos: list[str] | None,
) -> list[dict[str, Any]]:
    """Orden: caso → repetición → versión.

    Así, si la corrida se detuviera por presupuesto, quedaría completa la comparación de los
    casos alcanzados en vez de una versión entera sin sus pares.
    """
    datos = _casos()
    trabajos: list[dict[str, Any]] = []
    for nombre in tareas:
        matriz = MATRIZ[nombre]
        ids = solo_casos or matriz["casos"]
        casos = _seleccionados(datos, matriz["tipo"], ids)
        for caso in casos:
            for repeticion in range(1, repeticiones + 1):
                for version in matriz["versiones"]:
                    trabajos.append(
                        {
                            "tarea": nombre,
                            "tipo": matriz["tipo"],
                            "version": version,
                            "caso": caso,
                            "repeticion": repeticion,
                        }
                    )
    return trabajos


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


def _contexto_para_prompt(datos: dict[str, Any]) -> Any:
    """Reutiliza el contexto del evaluador offline, con UUID ficticios."""
    from evaluar_validacion import _contexto

    return _contexto(datos)[0]


def _carga(trabajo: dict[str, Any]) -> dict[str, Any]:
    """Materializa la petición exacta de un trabajo, sin llamar al proveedor.

    Las tres versiones de una tarea comparten caso, repetición, modelo y mensaje de usuario; solo
    cambia el prompt de sistema (y, en observaciones, el esquema propio de la versión).
    """
    caso = trabajo["caso"]
    version = trabajo["version"]
    sistema = _prompt(version)
    if trabajo["tipo"] == "vision":
        png = (IMAGENES / f"{caso['id']}.png").read_bytes()
        return {
            "system": sistema,
            "user": USUARIO_VISION,
            "schema": _schema(),
            "schema_name": "vision_extraction",
            "imagen_png": png,
            "hash_imagen": hashlib.sha256(png).hexdigest(),
            "max_output_tokens": MAX_TOKENS_SALIDA["vision"],
        }
    _, usuario = build_prompt(_contexto_para_prompt(caso["contexto"]), caso["texto"])
    schema = _schema_observaciones(version)
    return {
        "system": sistema,
        "user": usuario,
        "schema": schema,
        "schema_name": OUTPUT_SCHEMA_NAME,
        "imagen_png": None,
        "hash_imagen": None,
        "max_output_tokens": MAX_TOKENS_SALIDA["observaciones"],
    }


def _tokens_plan(carga: dict[str, Any]) -> int:
    texto = carga["system"] + carga["user"] + json.dumps(carga["schema"], sort_keys=True)
    tokens = math.ceil(len(texto) / CHARS_POR_TOKEN_PLAN)
    if carga["imagen_png"] is not None:
        tokens += TOKENS_IMAGEN_RESERVA // 2
    return tokens


def _reserva(carga: dict[str, Any], tarifas: tuple[Decimal, Decimal]) -> Decimal:
    """Costo máximo razonable de una llamada, para no pasarse del presupuesto.

    Se cuenta la entrada con 2,5 caracteres por token (peor caso del español y del esquema) y la
    salida con el tope declarado. Si una llamada real superara la reserva, el acumulado la
    detectaría en la siguiente comprobación.
    """
    texto = carga["system"] + carga["user"] + json.dumps(carga["schema"], sort_keys=True)
    entrada = math.ceil(Decimal(len(texto)) / CHARS_POR_TOKEN_RESERVA)
    if carga["imagen_png"] is not None:
        entrada += TOKENS_IMAGEN_RESERVA
    return _costo(entrada, carga["max_output_tokens"], tarifas)


def _estimacion(
    trabajos: list[dict[str, Any]],
    tarifas: tuple[Decimal, Decimal],
) -> dict[str, Any]:
    entrada_esperada = 0
    salida_esperada = 0
    entrada_reserva = 0
    salida_reserva = 0
    por_tarea: dict[str, int] = {}
    for trabajo in trabajos:
        carga = _carga(trabajo)
        por_tarea[trabajo["tarea"]] = por_tarea.get(trabajo["tarea"], 0) + 1
        entrada_esperada += _tokens_plan(carga)
        salida_esperada += TOKENS_SALIDA_ESPERADOS
        entrada_reserva += math.ceil(
            Decimal(
                len(carga["system"] + carga["user"] + json.dumps(carga["schema"], sort_keys=True))
            )
            / CHARS_POR_TOKEN_RESERVA
        ) + (TOKENS_IMAGEN_RESERVA if carga["imagen_png"] is not None else 0)
        salida_reserva += carga["max_output_tokens"]
    costo_esperado = _costo(entrada_esperada, salida_esperada, tarifas)
    costo_reserva = _costo(entrada_reserva, salida_reserva, tarifas)
    return {
        "llamadas": len(trabajos),
        "por_tarea": por_tarea,
        "tokens_entrada_esperados": entrada_esperada,
        "tokens_salida_esperados": salida_esperada,
        "costo_esperado_usd": str(costo_esperado.quantize(Decimal("0.0001"))),
        "costo_reserva_usd": str(costo_reserva.quantize(Decimal("0.0001"))),
        "advertencia": (
            "El costo esperado usa la longitud real de los prompts y ~350 tokens de salida. "
            "La reserva usa 2,5 caracteres por token, 2000 tokens de imagen por foto y el tope "
            "de salida por llamada: es el techo a partir del cual el evaluador se detendría."
        ),
    }


def _costo(entrada: int, salida: int, tarifas: tuple[Decimal, Decimal]) -> Decimal:
    entrada_usd, salida_usd = tarifas
    return (Decimal(entrada) * entrada_usd + Decimal(salida) * salida_usd) / Decimal(1_000_000)


async def _post(
    cliente: httpx.AsyncClient,
    *,
    modelo: str,
    system: str,
    user: str,
    schema: dict,
    schema_name: str,
    max_output_tokens: int,
    imagen_png: bytes | None = None,
) -> dict[str, Any]:
    contenido_usuario: list[dict[str, Any]] = [{"type": "input_text", "text": user}]
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
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
        "max_output_tokens": max_output_tokens,
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
    detalle = {
        "estado": resultado.get("estado"),
        "resultado_backend": resultado.get("resultado", resultado),
        "verificaciones": checks,
        "cumple": bool(checks) and all(c["cumple"] for c in checks),
    }
    if trabajo["caso"]["id"] == "V4":
        # Criterio ampliado fijado antes de la corrida: una imagen sin producto puede quedar
        # «blurry» o «unreadable» si no genera filas y queda en revisión. Es informativo; la
        # métrica principal sigue siendo la estricta de `casos.json`.
        detalle["v4_ampliado"] = _v4_ampliado(detalle["resultado_backend"])
    return detalle


def _v4_ampliado(resultado: dict[str, Any]) -> bool:
    if not isinstance(resultado, dict):
        return False
    reading = resultado.get("reading") or {}
    campos = resultado.get("campos") or {}
    return bool(
        resultado.get("estado") == "aceptada"
        and resultado.get("legibility") in {"blurry", "unreadable"}
        and reading.get("status") == "needs_review"
        and reading.get("display_name") is None
        and not campos
    )


class Presupuesto:
    """Acumula el gasto real y decide si la siguiente llamada cabe en el tope."""

    def __init__(self, tope: Decimal, tarifas: tuple[Decimal, Decimal]) -> None:
        self.tope = tope
        self.tarifas = tarifas
        self.gastado = Decimal("0")
        self.llamadas = 0
        self.tokens_entrada = 0
        self.tokens_salida = 0
        self.detenido_por: str | None = None

    def cabe(self, reserva: Decimal) -> bool:
        return self.gastado + reserva <= self.tope

    def registrar(self, salida: dict[str, Any], reserva: Decimal) -> tuple[Decimal, str]:
        entrada = salida.get("tokens_entrada")
        salida_tokens = salida.get("tokens_salida")
        if isinstance(entrada, int) and isinstance(salida_tokens, int):
            costo = _costo(entrada, salida_tokens, self.tarifas)
            self.tokens_entrada += entrada
            self.tokens_salida += salida_tokens
            origen = "uso"
        else:
            # Sin uso declarado (error de proveedor): se imputa la reserva para no subestimar.
            costo = reserva
            origen = "reserva"
        self.llamadas += 1
        self.gastado += costo
        return costo, origen

    def resumen(self) -> dict[str, Any]:
        return {
            "tope_usd": str(self.tope),
            "gastado_usd": str(self.gastado.quantize(Decimal("0.000001"))),
            "llamadas": self.llamadas,
            "tokens_entrada": self.tokens_entrada,
            "tokens_salida": self.tokens_salida,
            "detenido_por": self.detenido_por,
        }


def _resumen(resultados: list[dict[str, Any]]) -> dict[str, Any]:
    por_caso_version: dict[str, dict[str, dict[str, Any]]] = {}
    for r in resultados:
        caso = r["caso"]
        version = r["version_prompt"]
        grupo = por_caso_version.setdefault(caso, {}).setdefault(
            version,
            {"repeticiones": 0, "aciertos": 0, "checks": 0, "checks_cumplen": 0},
        )
        grupo["repeticiones"] += 1
        grupo["aciertos"] += int(bool(r["cumple"]))
        grupo["checks"] += len(r["verificaciones"])
        grupo["checks_cumplen"] += sum(1 for c in r["verificaciones"] if c["cumple"])

    comparaciones = []
    for caso, versiones in por_caso_version.items():
        if caso == "O10":
            continue  # capacidad nueva de v3: se informa aparte
        orden = ["vision-v1", "vision-v2", "vision-v3"]
        if not caso.startswith("V"):
            orden = ["observaciones-v1", "observaciones-v2", "observaciones-v3"]
        pares = [(orden[0], orden[1]), (orden[1], orden[2]), (orden[0], orden[2])]
        for desde, hasta in pares:
            if desde not in versiones or hasta not in versiones:
                continue
            a, b = versiones[desde]["aciertos"], versiones[hasta]["aciertos"]
            comparaciones.append(
                {
                    "caso": caso,
                    "desde": desde,
                    "hasta": hasta,
                    "aciertos_desde": a,
                    "aciertos_hasta": b,
                    "resultado": "mejora" if b > a else "retroceso" if b < a else "empate",
                }
            )

    o10 = por_caso_version.get("O10", {})
    return {
        "por_caso_version": por_caso_version,
        "comparaciones": comparaciones,
        "o10": {
            "nota": (
                "O10 depende de `article_operations`, que solo existe en v3. Los fallos de v1 y "
                "v2 no se cuentan como retroceso: la capacidad no estaba disponible."
            ),
            "por_version": o10,
        },
    }


def _plan(
    trabajos: list[dict[str, Any]],
    repeticiones: int,
    modelos: dict[str, str],
    tarifas: tuple[Decimal, Decimal],
) -> dict[str, Any]:
    return {
        "tareas": list({t["tarea"] for t in trabajos}),
        "casos": {
            t: sorted({j["caso"]["id"] for j in trabajos if j["tarea"] == t})
            for t in {j["tarea"] for j in trabajos}
        },
        "versiones": {
            t: sorted({j["version"] for j in trabajos if j["tarea"] == t})
            for t in {j["tarea"] for j in trabajos}
        },
        "repeticiones": repeticiones,
        "llamadas": len(trabajos),
        "modelos": modelos,
        "tarifas_usd_por_millon": {
            "entrada": str(tarifas[0]),
            "salida": str(tarifas[1]),
        },
        "max_tokens_salida": MAX_TOKENS_SALIDA,
        "criterio": (
            "Acierto estricto por repetición: se cumplen todas las verificaciones de "
            "`casos.json`. O10 se informa aparte como capacidad nueva de v3."
        ),
    }


def _escribir(
    ruta: Path,
    *,
    plan: dict[str, Any],
    estimacion: dict[str, Any],
    presupuesto: Presupuesto,
    resultados: list[dict[str, Any]],
    repeticiones: int,
    completado: bool,
) -> None:
    informe = {
        "generado_en": datetime.now(UTC).isoformat(timespec="seconds"),
        "tipo": "llamadas_reales_a_openai",
        "advertencia": (
            "Resultados reales del modelo elegido. No se extrapolan a otros modelos ni a "
            "producción. Cada repetición es una llamada cobrada."
        ),
        "plan": plan,
        "estimacion_previa": estimacion,
        "repeticiones": repeticiones,
        "completado": completado,
        "presupuesto": presupuesto.resumen(),
        "resultados": resultados,
        "resumen": _resumen(resultados),
    }
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(informe, ensure_ascii=False, indent=2) + "\n")


async def _ejecutar(
    trabajos: list[dict[str, Any]],
    *,
    modelo_observaciones: str,
    modelo_vision: str,
    api_key: str,
    base_url: str,
    tarifas: tuple[Decimal, Decimal],
    presupuesto: Presupuesto,
    salida_path: Path,
    plan: dict[str, Any],
    estimacion: dict[str, Any],
    repeticiones: int,
) -> list[dict[str, Any]]:
    resultados: list[dict[str, Any]] = []
    errores_seguidos = 0
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=60.0,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as cliente:
        for numero, trabajo in enumerate(trabajos, 1):
            carga = _carga(trabajo)
            reserva = _reserva(carga, tarifas)
            if not presupuesto.cabe(reserva):
                presupuesto.detenido_por = "presupuesto"
                print(
                    f"\nDetenido por presupuesto: la llamada {numero} reservaría "
                    f"USD {reserva:.4f} y quedan USD {presupuesto.tope - presupuesto.gastado:.4f}.",
                    file=sys.stderr,
                )
                break
            es_vision = trabajo["tipo"] == "vision"
            salida = await _post(
                cliente,
                modelo=modelo_vision if es_vision else modelo_observaciones,
                system=carga["system"],
                user=carga["user"],
                schema=carga["schema"],
                schema_name=carga["schema_name"],
                max_output_tokens=carga["max_output_tokens"],
                imagen_png=carga["imagen_png"],
            )
            costo, costo_origen = presupuesto.registrar(salida, reserva)
            evaluacion = _evaluar(trabajo, salida)
            fallo = "error" in salida or not salida.get("texto")
            errores_seguidos = errores_seguidos + 1 if fallo else 0
            resultados.append(
                {
                    "numero": numero,
                    "tarea": trabajo["tarea"],
                    "caso": trabajo["caso"]["id"],
                    "version_prompt": trabajo["version"],
                    "repeticion": trabajo["repeticion"],
                    "modelo_solicitado": (modelo_vision if es_vision else modelo_observaciones),
                    "modelo_respuesta": salida.get("modelo"),
                    "hash_prompt": hashlib.sha256(carga["system"].encode()).hexdigest(),
                    "hash_imagen": carga["hash_imagen"],
                    "tokens_entrada": salida.get("tokens_entrada"),
                    "tokens_salida": salida.get("tokens_salida"),
                    "latencia_ms": salida.get("latencia_ms"),
                    "costo_estimado_usd": str(costo.quantize(Decimal("0.000001"))),
                    "costo_origen": costo_origen,
                    "salida_cruda": salida.get("texto"),
                    "error_proveedor": salida.get("error"),
                    **evaluacion,
                }
            )
            _escribir(
                salida_path,
                plan=plan,
                estimacion=estimacion,
                presupuesto=presupuesto,
                resultados=resultados,
                repeticiones=repeticiones,
                completado=False,
            )
            print(
                f"{numero:02}/{len(trabajos)} {trabajo['caso']['id']:4} "
                f"{trabajo['version']:20} rep {trabajo['repeticion']} "
                f"{'OK' if evaluacion['cumple'] else 'NO'} {evaluacion['estado']} "
                f"USD {costo:.6f} (acum. {presupuesto.gastado:.6f})",
                flush=True,
            )
            if errores_seguidos >= MAX_ERRORES_SEGUIDOS:
                presupuesto.detenido_por = "errores_proveedor"
                print(
                    f"\nDetenido tras {errores_seguidos} errores seguidos del proveedor.",
                    file=sys.stderr,
                )
                break
    _escribir(
        salida_path,
        plan=plan,
        estimacion=estimacion,
        presupuesto=presupuesto,
        resultados=resultados,
        repeticiones=repeticiones,
        completado=presupuesto.detenido_por is None and len(resultados) == len(trabajos),
    )
    return resultados


def _cargar_clave(env_file: Path | None) -> str | None:
    if env_file is not None:
        from dotenv import dotenv_values

        valores = dotenv_values(env_file)
        if valores.get("OPENAI_API_KEY"):
            return valores["OPENAI_API_KEY"]
    return os.getenv("OPENAI_API_KEY")


def _tarifas(args: argparse.Namespace, modelos: dict[str, str]) -> tuple[Decimal, Decimal]:
    if args.tarifa_entrada_usd is not None and args.tarifa_salida_usd is not None:
        return args.tarifa_entrada_usd, args.tarifa_salida_usd
    distinct = set(modelos.values())
    if len(distinct) != 1:
        raise SystemExit(
            "Las tarifas conocidas son por modelo y se están usando modelos distintos "
            f"({', '.join(sorted(distinct))}). Declara --tarifa-entrada-usd y --tarifa-salida-usd."
        )
    modelo = next(iter(distinct))
    if modelo not in TARIFAS_CONOCIDAS:
        raise SystemExit(
            f"No hay tarifas verificadas para «{modelo}». Declara --tarifa-entrada-usd y "
            "--tarifa-salida-usd antes de ejecutar."
        )
    return TARIFAS_CONOCIDAS[modelo]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tarea", action="append", choices=sorted(MATRIZ))
    parser.add_argument("--casos", help="ids separados por coma; reemplaza los de la matriz")
    parser.add_argument("--repeticiones", type=int, default=REPETICIONES_POR_DEFECTO)
    parser.add_argument("--max-llamadas", type=int, default=MAX_LLAMADAS_POR_DEFECTO)
    parser.add_argument("--presupuesto-usd", type=Decimal, default=PRESUPUESTO_POR_DEFECTO)
    parser.add_argument("--modelo", default=os.getenv("OPENAI_MODEL"))
    parser.add_argument("--vision-modelo", default=os.getenv("OPENAI_VISION_MODEL"))
    parser.add_argument("--tarifa-entrada-usd", type=Decimal)
    parser.add_argument("--tarifa-salida-usd", type=Decimal)
    parser.add_argument(
        "--base-url", default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    parser.add_argument("--salida", type=Path, default=SALIDA)
    parser.add_argument("--env-file", type=Path, help="solo para leer OPENAI_API_KEY")
    parser.add_argument("--plan", action="store_true", help="solo muestra el plan")
    parser.add_argument("--confirmar-gasto", action="store_true")
    args = parser.parse_args()

    tareas = args.tarea or list(MATRIZ)
    solo_casos = [c.strip() for c in args.casos.split(",")] if args.casos else None
    if not args.modelo or not args.vision_modelo:
        raise SystemExit(
            "Faltan OPENAI_MODEL y/o OPENAI_VISION_MODEL. La aplicación no les da valor "
            "por defecto; indícalos con --modelo y --vision-modelo."
        )
    if args.repeticiones < 1:
        raise SystemExit("--repeticiones debe ser al menos 1.")
    modelos = {"observaciones": args.modelo, "vision": args.vision_modelo}
    tarifas = _tarifas(args, modelos)
    trabajos = _trabajos(tareas, args.repeticiones, solo_casos)
    estimacion = _estimacion(trabajos, tarifas)
    plan = _plan(trabajos, args.repeticiones, modelos, tarifas)

    print(json.dumps({"plan": plan, "estimacion": estimacion}, ensure_ascii=False, indent=2))
    if args.plan:
        return 0
    if not args.confirmar_gasto:
        print("\nSin --confirmar-gasto no se llama a OpenAI.", file=sys.stderr)
        return 2
    if len(trabajos) > args.max_llamadas:
        raise SystemExit(
            f"El plan son {len(trabajos)} llamadas y el límite es {args.max_llamadas}. "
            "Ajusta --max-llamadas solo con autorización."
        )
    esperado = _costo(
        estimacion["tokens_entrada_esperados"],
        estimacion["tokens_salida_esperados"],
        tarifas,
    )
    if esperado > args.presupuesto_usd:
        raise SystemExit(
            f"El costo esperado (USD {esperado:.4f}) supera el presupuesto "
            f"(USD {args.presupuesto_usd}). No se llama al proveedor."
        )
    api_key = _cargar_clave(args.env_file)
    if not api_key:
        raise SystemExit("Falta OPENAI_API_KEY en el entorno o en --env-file.")

    presupuesto = Presupuesto(args.presupuesto_usd, tarifas)
    resultados = await _ejecutar(
        trabajos,
        modelo_observaciones=args.modelo,
        modelo_vision=args.vision_modelo,
        api_key=api_key,
        base_url=args.base_url,
        tarifas=tarifas,
        presupuesto=presupuesto,
        salida_path=args.salida,
        plan=plan,
        estimacion=estimacion,
        repeticiones=args.repeticiones,
    )
    print(
        f"\nCompletadas {len(resultados)}/{len(trabajos)} llamadas · "
        f"USD {presupuesto.gastado:.6f} de USD {presupuesto.tope}"
    )
    print(f"Informe: {args.salida}")
    return 0 if len(resultados) == len(trabajos) else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
