"""Unit handling.

Autodesk Inventor's API always speaks *database units*: centimetres for
length, radians for angle, kilograms for mass.  Everything the user sees is a
display unit (mm, in, deg, ...).  This module is the single place where that
conversion happens, plus a tiny dimensional algebra used by the expression
evaluator so that ``10 mm * 3`` is a length but ``10 mm * 10 mm`` is an area.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .errors import UnitError

# --------------------------------------------------------------------------
# Dimensions
# --------------------------------------------------------------------------


class Dim(str, Enum):
    """Physical dimension of a quantity, tracked as (length, angle) exponents."""

    UNITLESS = "unitless"
    LENGTH = "length"
    AREA = "area"
    VOLUME = "volume"
    ANGLE = "angle"
    MASS = "mass"
    UNKNOWN = "unknown"


# (length_exponent, angle_exponent, mass_exponent) -> Dim
_EXPONENTS_TO_DIM = {
    (0, 0, 0): Dim.UNITLESS,
    (1, 0, 0): Dim.LENGTH,
    (2, 0, 0): Dim.AREA,
    (3, 0, 0): Dim.VOLUME,
    (0, 1, 0): Dim.ANGLE,
    (0, 0, 1): Dim.MASS,
}
_DIM_TO_EXPONENTS = {v: k for k, v in _EXPONENTS_TO_DIM.items()}


def dim_from_exponents(length: int, angle: int, mass: int) -> Dim:
    return _EXPONENTS_TO_DIM.get((length, angle, mass), Dim.UNKNOWN)


def exponents_for(dim: Dim) -> tuple[int, int, int]:
    try:
        return _DIM_TO_EXPONENTS[dim]
    except KeyError:  # pragma: no cover - guarded by callers
        raise UnitError(f"Dimension {dim.value!r} has no canonical exponents.")


# --------------------------------------------------------------------------
# Unit table.  Factors convert *display unit* -> *Inventor database unit*.
# --------------------------------------------------------------------------

_LENGTH_UNITS: dict[str, float] = {
    "mm": 0.1,
    "millimeter": 0.1,
    "millimetre": 0.1,
    "cm": 1.0,
    "centimeter": 1.0,
    "centimetre": 1.0,
    "m": 100.0,
    "meter": 100.0,
    "metre": 100.0,
    "micron": 1.0e-4,
    "um": 1.0e-4,
    "in": 2.54,
    "inch": 2.54,
    '"': 2.54,
    "ft": 30.48,
    "foot": 30.48,
    "feet": 30.48,
    "mil": 2.54e-3,
    "thou": 2.54e-3,
}

_ANGLE_UNITS: dict[str, float] = {
    "deg": math.pi / 180.0,
    "degree": math.pi / 180.0,
    "degrees": math.pi / 180.0,
    "rad": 1.0,
    "radian": 1.0,
    "radians": 1.0,
    "grad": math.pi / 200.0,
}

_MASS_UNITS: dict[str, float] = {
    "kg": 1.0,
    "g": 1.0e-3,
    "lbmass": 0.45359237,
    "lb": 0.45359237,
}

_UNITLESS_UNITS: dict[str, float] = {"ul": 1.0, "": 1.0}

#: Units Inventor accepts verbatim in an expression string, keyed by our alias.
_INVENTOR_SYMBOL = {
    **{k: "mm" for k in ("mm", "millimeter", "millimetre")},
    **{k: "cm" for k in ("cm", "centimeter", "centimetre")},
    **{k: "m" for k in ("m", "meter", "metre")},
    **{k: "micron" for k in ("micron", "um")},
    **{k: "in" for k in ("in", "inch", '"')},
    **{k: "ft" for k in ("ft", "foot", "feet")},
    **{k: "mil" for k in ("mil", "thou")},
    **{k: "deg" for k in ("deg", "degree", "degrees")},
    **{k: "rad" for k in ("rad", "radian", "radians")},
    "grad": "grad",
    "kg": "kg",
    "g": "g",
    **{k: "lbmass" for k in ("lb", "lbmass")},
    "ul": "ul",
}


@dataclass(frozen=True)
class UnitInfo:
    """A resolved unit: its conversion factor and the dimension it belongs to."""

    alias: str
    symbol: str
    factor: float
    dim: Dim


def _build_unit_table() -> dict[str, UnitInfo]:
    table: dict[str, UnitInfo] = {}
    for source, dim in (
        (_LENGTH_UNITS, Dim.LENGTH),
        (_ANGLE_UNITS, Dim.ANGLE),
        (_MASS_UNITS, Dim.MASS),
        (_UNITLESS_UNITS, Dim.UNITLESS),
    ):
        for alias, factor in source.items():
            table[alias] = UnitInfo(alias, _INVENTOR_SYMBOL.get(alias, alias), factor, dim)
    return table


_UNITS = _build_unit_table()

#: Units a caller may pick as the default for a document or recipe.
LENGTH_UNIT_NAMES: tuple[str, ...] = ("mm", "cm", "m", "in", "ft")
ANGLE_UNIT_NAMES: tuple[str, ...] = ("deg", "rad")


def lookup_unit(symbol: str) -> UnitInfo:
    """Resolve a unit symbol, case-insensitively, to its :class:`UnitInfo`."""
    key = symbol.strip()
    info = _UNITS.get(key) or _UNITS.get(key.lower())
    if info is None:
        raise UnitError(
            f"Unknown unit {symbol!r}.",
            hint="Supported units: " + ", ".join(sorted(set(_UNITS) - {""})),
        )
    return info


def is_unit(symbol: str) -> bool:
    key = symbol.strip()
    return key in _UNITS or key.lower() in _UNITS


# --------------------------------------------------------------------------
# Quantities
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Quantity:
    """A scalar in Inventor database units together with its dimension."""

    value: float
    dim: Dim = Dim.UNITLESS

    def as_display(self, unit: str) -> float:
        """Convert to *unit*, which must match this quantity's dimension."""
        info = lookup_unit(unit)
        if info.dim is not self.dim and self.dim is not Dim.UNITLESS:
            raise UnitError(
                f"Cannot express a {self.dim.value} quantity in {unit!r} "
                f"(a {info.dim.value} unit)."
            )
        return self.value / info.factor

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Quantity({self.value:g}, {self.dim.value})"


def to_internal(value: float, unit: str) -> Quantity:
    """Convert a display value to Inventor database units."""
    info = lookup_unit(unit)
    return Quantity(value * info.factor, info.dim)


def from_internal(value: float, unit: str) -> float:
    """Convert an Inventor database value to *unit*."""
    return value / lookup_unit(unit).factor


def convert(value: float, from_unit: str, to_unit: str) -> float:
    src = lookup_unit(from_unit)
    dst = lookup_unit(to_unit)
    if src.dim is not dst.dim:
        raise UnitError(f"Cannot convert {from_unit!r} ({src.dim.value}) to {to_unit!r} ({dst.dim.value}).")
    return value * src.factor / dst.factor


def inventor_symbol(unit: str) -> str:
    """The spelling Inventor itself understands inside an expression string."""
    return lookup_unit(unit).symbol


#: Inventor's own spelling of a unit, back to the short name this project uses.
#: Built from the unit table rather than written out, so a unit added there is
#: readable here for free, and the shortest alias wins -- "mm" rather than
#: "millimetre", which is what the rest of this project calls it.
def _preference(alias: str, symbol: str) -> tuple[int, int, str]:
    """How good a name *alias* is for the unit Inventor spells *symbol*.

    A name a caller may actually pass wins; then the one that matches Inventor's
    own spelling; then the shortest. Ordering by length alone chose `"` for the
    inch, which is a unit nobody can type into a tool argument.
    """
    canonical = alias in LENGTH_UNIT_NAMES or alias in ANGLE_UNIT_NAMES
    return (0 if canonical else 1, 0 if alias == symbol else 1, alias)


_FROM_INVENTOR: dict[str, str] = {}
for _alias, _info in _UNITS.items():
    if _info.dim not in (Dim.LENGTH, Dim.ANGLE):
        continue
    _key = _info.symbol.lower()
    _held = _FROM_INVENTOR.get(_key)
    if _held is None or _preference(_alias, _key) < _preference(_held, _key):
        _FROM_INVENTOR[_key] = _alias


def unit_from_inventor(symbol: str) -> str | None:
    """Our short name for the unit Inventor calls *symbol*, or ``None``.

    ``None`` rather than a default, because a caller reading a document's units
    needs to tell "this part is in millimetres" from "this part would not say".
    Treating the second as the first is how an inch-authored part gets driven a
    twenty-fifth of the size it should be.
    """
    if not symbol:
        return None
    cleaned = str(symbol).strip().lower()
    found = _FROM_INVENTOR.get(cleaned)
    if found is not None:
        return found
    # Inventor spells some of them out. Resolve the spelling to a unit, then take
    # that unit's short name, so "Millimeter" comes back as "mm".
    known = _UNITS.get(cleaned)
    if known is not None and known.dim in (Dim.LENGTH, Dim.ANGLE):
        return _FROM_INVENTOR.get(known.symbol.lower(), cleaned)
    return None


def default_unit_for(dim: Dim, length_unit: str = "mm", angle_unit: str = "deg") -> str:
    if dim is Dim.LENGTH:
        return length_unit
    if dim is Dim.ANGLE:
        return angle_unit
    if dim is Dim.MASS:
        return "kg"
    return "ul"


# --------------------------------------------------------------------------
# Literal parsing: "12.5 mm", "-3in", "45", "1e3 mm"
# --------------------------------------------------------------------------

_NUMBER_WITH_UNIT = re.compile(
    r"""^\s*
        (?P<number>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)
        \s*
        (?P<unit>[A-Za-z"]*)
        \s*$""",
    re.VERBOSE,
)


def parse_quantity(text: str, *, default_unit: str = "") -> Quantity:
    """Parse a bare literal such as ``"12.5 mm"`` into a :class:`Quantity`.

    This only handles a single number with an optional unit; anything more
    complex belongs to :mod:`inventor_mcp.expressions`.
    """
    match = _NUMBER_WITH_UNIT.match(text)
    if not match:
        raise UnitError(f"{text!r} is not a plain number-with-unit literal.")
    unit = match.group("unit") or default_unit
    return to_internal(float(match.group("number")), unit)


def format_quantity(quantity: Quantity, unit: str, *, decimals: int = 4) -> str:
    """Render a quantity as an Inventor-friendly expression, e.g. ``"12.5 mm"``."""
    value = quantity.as_display(unit)
    text = f"{round(value, decimals):g}"
    symbol = inventor_symbol(unit)
    return text if symbol == "ul" else f"{text} {symbol}"


def describe_units() -> dict[str, Iterable[str]]:  # pragma: no cover - documentation helper
    grouped: dict[str, list[str]] = {}
    for info in _UNITS.values():
        grouped.setdefault(info.dim.value, []).append(info.alias)
    return {k: sorted(set(v)) for k, v in grouped.items()}
