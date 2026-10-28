"""Intención y segmentación de mensajes de chat (determinista).

Un mensaje puede traer varios productos (una línea o coma por producto), un saludo o una pregunta.
Solo lo que parece un faltante se interpreta; lo demás no crea necesidades.
"""

import re
import unicodedata
from enum import StrEnum

from app.core.errors import DomainError
from app.domain.products import STOPWORDS, parse_shortage_text

MAX_SEGMENTS = 10
CHITCHAT = {"hola", "holi", "buenas", "buenos", "dias", "tardes", "noches", "gracias", "ok", "oka", "vale",
            "listo", "chao", "adios", "si", "saludos", "buen", "dia", "bien", "ayuda", "hey", "q", "que", "tal",
            "como", "estas", "estan"}  # fmt: skip


class Intent(StrEnum):
    SHORTAGE = "shortage_report"
    CHITCHAT = "chitchat"
    QUESTION = "question"
    EMPTY = "empty"


def _plain(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def classify_intent(text: str) -> Intent:
    t = text.strip()
    if not t:
        return Intent.EMPTY
    if "?" in t or "¿" in t:  # una pregunta no es un faltante
        return Intent.QUESTION
    words = re.findall(r"[a-z0-9]+", _plain(t))
    if not words or all(w in CHITCHAT or w in STOPWORDS for w in words):
        return Intent.CHITCHAT
    return Intent.SHORTAGE


def split_segments(text: str) -> tuple[list[str], bool]:
    """Divide en un producto por línea, «;» o coma (no la decimal). Devuelve (segmentos, truncado).

    Un trozo que por sí solo no identifica un producto («20 comprimidos») se pega al anterior.
    No se divide por «y»: «paracetamol y codeína» puede ser una sola combinación.
    """
    raw = [s.strip() for s in re.split(r"[\n;]|(?<!\d),|,(?!\d)", text) if s.strip()]
    merged: list[str] = []
    for part in raw:
        try:
            parse_shortage_text(part)
            ok = True
        except DomainError as e:
            ok = e.code != "PRODUCT_NOT_RECOGNIZED"  # otros errores se reportan por segmento
        if not ok and merged:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged[:MAX_SEGMENTS], len(merged) > MAX_SEGMENTS
