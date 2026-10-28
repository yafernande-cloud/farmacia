"""Números escritos en palabras → cifras («cincuenta mil» → 50000), conservando posiciones.

Opera sobre el texto ya normalizado (minúsculas, sin tildes). No convierte «un/una/uno» (son
artículos) ni inventa nada: si la frase no sigue la gramática, se deja tal cual.
"""

import re

_UNITS = {
    "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
    "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20,
    "veintiuno": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24, "veinticinco": 25,
    "veintiseis": 26, "veintisiete": 27, "veintiocho": 28, "veintinueve": 29,
}  # fmt: skip
_TENS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70,
         "ochenta": 80, "noventa": 90}  # fmt: skip
_HUNDREDS = {"cien": 100, "ciento": 100, "doscientos": 200, "trescientos": 300,
             "cuatrocientos": 400, "quinientos": 500, "seiscientos": 600, "setecientos": 700,
             "ochocientos": 800, "novecientos": 900}  # fmt: skip
_SMALL_UNITS = {k: v for k, v in _UNITS.items() if v < 10}
_WORDS = set(_UNITS) | set(_TENS) | set(_HUNDREDS) | {"mil", "y"}


def _below_1000(t: list[str], i: int) -> tuple[int, int] | None:
    """Parsea 1..999 desde t[i]; devuelve (valor, siguiente índice) o None."""
    total, j, got = 0, i, False
    if j < len(t) and t[j] in _HUNDREDS:
        total, j, got = _HUNDREDS[t[j]], j + 1, True
    if j < len(t) and t[j] in _UNITS:  # 2..29
        total, j, got = total + _UNITS[t[j]], j + 1, True
    elif j < len(t) and t[j] in _TENS:
        total, j, got = total + _TENS[t[j]], j + 1, True
        if j + 1 < len(t) and t[j] == "y" and t[j + 1] in _SMALL_UNITS:
            total, j = total + _SMALL_UNITS[t[j + 1]], j + 2
    return (total, j) if got else None


def _number(t: list[str], i: int) -> tuple[int, int] | None:
    total, j = 0, i
    head = _below_1000(t, j)
    if head and head[1] < len(t) and t[head[1]] == "mil":  # «cincuenta mil»
        total, j = head[0] * 1000, head[1] + 1
    elif j < len(t) and t[j] == "mil":  # «mil»
        total, j = 1000, j + 1
    else:
        return head
    tail = _below_1000(t, j)  # «mil quinientos»
    return (total + tail[0], tail[1]) if tail else (total, j)


def convert_number_words(norm: str, idx: list[int]) -> tuple[str, list[int]]:
    """Reemplaza frases numéricas por cifras. `idx` (posición original por carácter) se actualiza
    para que la cifra siga apuntando al tramo exacto que la persona escribió."""
    toks = [(m.group(), m.start(), m.end()) for m in re.finditer(r"[a-z0-9]+", norm)]
    words = [w for w, _, _ in toks]
    out_norm: list[str] = []
    out_idx: list[int] = []
    cursor = 0
    i = 0
    while i < len(toks):
        w, s, _ = toks[i]
        hit = None
        if w in _WORDS and w != "y":
            hit = _number(words, i)
        elif w.isdigit() and i + 1 < len(toks) and words[i + 1] == "mil":  # «50 mil»
            hit = (int(w) * 1000, i + 2)
        if hit:
            value, j = hit
            e = toks[j - 1][2]
            out_norm.append(norm[cursor:s])
            out_idx.extend(idx[cursor:s])
            digits = str(value)
            out_norm.append(digits)
            first, last = idx[s], idx[e - 1]
            out_idx.extend(
                [first] + [first] * (len(digits) - 2) + [last] if len(digits) > 1 else [first]
            )
            cursor, i = e, j
        else:
            i += 1
    out_norm.append(norm[cursor:])
    out_idx.extend(idx[cursor:])
    return "".join(out_norm), out_idx
