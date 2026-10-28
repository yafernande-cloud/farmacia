"""Registro extensible de unidades y medidas (HLD §7.1, §9.2).

Una medida conserva SIEMPRE lo escrito (valor, unidad, denominador, texto original). La
normalización (p. ej. g→mg, mg/5 mL→mg/mL) se calcula aparte y solo sirve para comparar:
nunca reemplaza la representación original ni cambia el significado del atributo.

Extender: agregar una fila en `_UNITS` (y alias en `ALIASES`). Las unidades compuestas se forman
automáticamente si el numerador y el denominador son de dimensiones permitidas.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Dimension(StrEnum):
    MASS = "mass"
    VOLUME = "volume"
    IU = "iu"
    PERCENT = "percent"
    SUBSTANCE = "substance"  # mmol
    EQUIVALENT = "equivalent"  # mEq
    DOSE = "dose"  # dosis de inhalador, etc.
    COUNT = "count"  # unidades de la forma (comprimidos, ampollas…)


@dataclass(frozen=True)
class UnitDef:
    symbol: str
    dimension: Dimension
    factor: Decimal  # hacia la unidad base de su dimensión
    base: str


_UNITS = [
    UnitDef("mg", Dimension.MASS, Decimal("1"), "mg"),
    UnitDef("g", Dimension.MASS, Decimal("1000"), "mg"),
    UnitDef("mcg", Dimension.MASS, Decimal("0.001"), "mg"),
    UnitDef("ml", Dimension.VOLUME, Decimal("1"), "ml"),
    UnitDef("l", Dimension.VOLUME, Decimal("1000"), "ml"),
    UnitDef("ui", Dimension.IU, Decimal("1"), "ui"),
    UnitDef("%", Dimension.PERCENT, Decimal("1"), "%"),
    UnitDef("mmol", Dimension.SUBSTANCE, Decimal("1"), "mmol"),
    UnitDef("meq", Dimension.EQUIVALENT, Decimal("1"), "meq"),
    UnitDef("dosis", Dimension.DOSE, Decimal("1"), "dosis"),
    UnitDef("un", Dimension.COUNT, Decimal("1"), "un"),
]
UNITS: dict[str, UnitDef] = {u.symbol: u for u in _UNITS}
ALIASES = {
    "gr": "g", "grs": "g", "gramo": "g", "gramos": "g",
    "miligramo": "mg", "miligramos": "mg",
    "ug": "mcg", "microgramo": "mcg", "microgramos": "mcg",
    "cc": "ml", "mililitro": "ml", "mililitros": "ml",
    "lt": "l", "lts": "l", "litro": "l", "litros": "l",
    "iu": "ui", "dosis": "dosis", "unidad": "un", "unidades": "un",
}  # fmt: skip

NUMERATOR_DIMS = {Dimension.MASS, Dimension.IU, Dimension.SUBSTANCE, Dimension.EQUIVALENT}
DENOMINATOR_DIMS = {Dimension.VOLUME, Dimension.MASS, Dimension.DOSE}

# Las expresiones regulares de otros módulos usan esta alternancia (más larga primero).
UNIT_TOKENS = sorted(set(UNITS) | set(ALIASES), key=len, reverse=True)
UNIT_ALT = "|".join(re.escape(t) for t in UNIT_TOKENS)
NUM = r"\d+(?:[.,]\d+)*"


def canonical_unit(token: str) -> str | None:
    t = token.strip().lower().replace("µ", "u").replace("μ", "u")
    t = ALIASES.get(t, t)
    return t if t in UNITS else None


def parse_number(s: str) -> Decimal:
    """'2,5' → 2.5; '1.000' → 1000 (separador de miles chileno); '1.5' → 1.5."""
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        return Decimal(s.replace(".", ""))
    return Decimal(s.replace(",", "."))


def fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")


@dataclass(frozen=True)
class Normalized:
    dimension: str
    value: Decimal
    unit: str

    @property
    def key(self) -> str:
        return f"{self.dimension}:{fmt(self.value)}{self.unit}"


@dataclass(frozen=True)
class Measure:
    """Valor + unidad tal como se escribió. `unit=None` = unidad no indicada."""

    value: Decimal
    unit: str | None = None
    per_value: Decimal | None = None  # None = 1 implícito (mg/mL)
    per_unit: str | None = None
    original_text: str = ""

    @property
    def is_compound(self) -> bool:
        return self.per_unit is not None

    def validate(self) -> None:
        if self.unit is None:
            if self.per_unit is not None:
                raise ValueError("unidad compuesta sin numerador")
            return
        if self.unit not in UNITS:
            raise ValueError(f"unidad desconocida: {self.unit}")
        if self.per_unit is None:
            return
        if self.per_unit not in UNITS:
            raise ValueError(f"unidad desconocida: {self.per_unit}")
        if (
            UNITS[self.unit].dimension not in NUMERATOR_DIMS
            or UNITS[self.per_unit].dimension not in DENOMINATOR_DIMS
        ):
            raise ValueError(f"unidad compuesta no soportada: {self.unit}/{self.per_unit}")
        if self.per_value is not None and self.per_value <= 0:
            raise ValueError("el denominador debe ser positivo")

    def normalized(self) -> Normalized | None:
        if self.unit is None:
            return None
        n = UNITS[self.unit]
        v = self.value * n.factor
        if not self.is_compound:
            return Normalized(n.dimension.value, v.quantize(Decimal("0.000001")), n.base)
        d = UNITS[self.per_unit or ""]
        v = v / ((self.per_value or Decimal(1)) * d.factor)
        return Normalized(
            f"{n.dimension.value}/{d.dimension.value}",
            v.quantize(Decimal("0.000001")),
            f"{n.base}/{d.base}",
        )

    @property
    def label(self) -> str:
        if self.unit is None:
            return fmt(self.value)
        s = f"{fmt(self.value)} {self.unit}"
        if self.per_unit:
            s += (
                f"/{fmt(self.per_value) + ' ' if self.per_value is not None else ''}{self.per_unit}"
            )
        return s

    @property
    def unit_expr(self) -> str | None:
        """La unidad como se vuelve a escribir: 'mg', 'mg/ml', 'mg/5 ml'."""
        if self.unit is None:
            return None
        if not self.per_unit:
            return self.unit
        return f"{self.unit}/{fmt(self.per_value) if self.per_value is not None else ''}{self.per_unit}"

    def to_dict(self) -> dict:
        d = {"value": fmt(self.value), "unit": self.unit, "per_value": self.per_value and fmt(self.per_value),
             "per_unit": self.per_unit, "original_text": self.original_text}  # fmt: skip
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict) -> "Measure":
        m = cls(Decimal(d["value"]), d.get("unit"), Decimal(d["per_value"]) if d.get("per_value") else None,
                d.get("per_unit"), d.get("original_text", ""))  # fmt: skip
        m.validate()
        return m


def parse_unit_expr(expr: str) -> tuple[str, str | None, str | None]:
    """'mg' | 'mg/ml' | 'mg/5ml' | '%' → (unidad, denominador numérico, unidad del denominador)."""
    m = re.fullmatch(
        rf"\s*({UNIT_ALT})\s*(?:/\s*({NUM})?\s*({UNIT_ALT}))?\s*", expr.lower().replace("µ", "u")
    )
    if not m:
        raise ValueError(f"unidad no reconocida: {expr!r}")
    unit = canonical_unit(m.group(1))
    per = canonical_unit(m.group(3)) if m.group(3) else None
    assert unit
    return unit, m.group(2), per


def build_measure(value: Decimal, unit_expr: str, original_text: str = "") -> Measure:
    unit, per_n, per_u = parse_unit_expr(unit_expr)
    m = Measure(
        value,
        unit,
        parse_number(per_n) if per_n else None,
        per_u,
        original_text or f"{fmt(value)} {unit_expr}",
    )
    m.validate()
    return m
