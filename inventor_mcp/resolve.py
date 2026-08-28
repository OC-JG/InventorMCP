"""Turning recipe values into Inventor expressions plus evaluated numbers.

A recipe field may be a bare number (``40``) meaning "40 of the recipe's
units", or a string (``"width / 2"``, ``"1.5 in"``) meaning an expression.
Both must end up as

* an expression string Inventor will store on the dimension, and
* a value in database units so we can place geometry and run the mock backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .errors import ExpressionError
from .expressions import UnitContext, evaluate
from .units import Dim, Quantity, format_quantity, inventor_symbol, lookup_unit, to_internal


@dataclass(frozen=True)
class Resolved:
    """An expression paired with its evaluated value in database units."""

    expression: str
    value: float
    dim: Dim

    def __float__(self) -> float:
        return self.value


class Resolver:
    """Resolves :data:`~inventor_mcp.schema.ValueSpec` values in a unit context."""

    def __init__(
        self,
        length_unit: str = "mm",
        angle_unit: str = "deg",
        parameters: Mapping[str, Quantity] | None = None,
    ) -> None:
        self.length_unit = length_unit
        self.angle_unit = angle_unit
        self.parameters: dict[str, Quantity] = dict(parameters or {})

    def _units(self) -> UnitContext:
        return UnitContext(length=self.length_unit, angle=self.angle_unit)

    # -- parameter table ---------------------------------------------------
    def declare(self, name: str, quantity: Quantity) -> None:
        self.parameters[name] = quantity

    def known(self) -> dict[str, Quantity]:
        return dict(self.parameters)

    # -- resolution --------------------------------------------------------
    def _resolve(self, spec: float | int | str, unit: str, expected: Dim, what: str) -> Resolved:
        if isinstance(spec, bool):  # bool is an int subclass; never meaningful here
            raise ExpressionError(f"{what} must be a number or an expression, not a boolean.")
        if isinstance(spec, (int, float)):
            quantity = to_internal(float(spec), unit)
            return Resolved(format_quantity(quantity, unit), quantity.value, expected)

        expr = evaluate(spec, self.parameters, self._units())
        if expr.dim is expected:
            return Resolved(expr.normalised, expr.value, expected)

        if expr.dim is Dim.UNITLESS:
            # Bare numbers take the context unit, matching Inventor's own behaviour.
            symbol = inventor_symbol(unit)
            factor = to_internal(1.0, unit).value
            source = expr.source
            wrapped = source if _is_simple(source) else f"({source})"
            return Resolved(f"{wrapped} * 1 {symbol}", expr.value * factor, expected)

        raise ExpressionError(
            f"{what} must be a {expected.value}, but {spec!r} evaluates to a {expr.dim.value}.",
            hint="Check the units on the parameters used in this expression.",
        )

    def length(self, spec: float | int | str, what: str = "length", *, positive: bool = False) -> Resolved:
        resolved = self._resolve(spec, self.length_unit, Dim.LENGTH, what)
        if positive and resolved.value <= 0:
            raise ExpressionError(f"{what} must be greater than zero (got {spec!r}).")
        return resolved

    def angle(self, spec: float | int | str, what: str = "angle") -> Resolved:
        return self._resolve(spec, self.angle_unit, Dim.ANGLE, what)

    def unitless(self, spec: float | int | str, what: str = "value") -> Resolved:
        return self._resolve(spec, "ul", Dim.UNITLESS, what)

    def count(self, spec: float | int | str, what: str = "count", *,
              minimum: int = 1, maximum: int = 1000) -> int:
        """A whole number, which may be written as an expression.

        Counts used to be plain integers, so "one hole per 30 mm of pitch
        circle" could not be said and a pattern's count could not be revised
        the way its spacing could. Inventor allows an expression there, so the
        recipe should too -- but a count of 4.5 holes is a mistake rather than
        something to round, so a fractional answer is refused.
        """
        resolved = self.unitless(spec, what)
        nearest = round(resolved.value)
        if abs(resolved.value - nearest) > 1e-9:
            raise ExpressionError(
                f"{what} must be a whole number, but {spec!r} is {resolved.value:g}.",
                hint="Counts cannot be fractional. Use an expression that divides "
                "exactly, or round it yourself.",
            )
        if not minimum <= nearest <= maximum:
            raise ExpressionError(
                f"{what} must be between {minimum} and {maximum}, but {spec!r} "
                f"is {nearest}."
            )
        return nearest

    def in_unit(self, spec: float | int | str, unit: str, what: str = "value") -> Resolved:
        """Resolve against an explicitly chosen unit (and therefore dimension)."""
        return self._resolve(spec, unit, lookup_unit(unit).dim, what)

    def auto(self, spec: float | int | str, what: str = "value") -> Resolved:
        """Resolve without forcing a dimension -- used for user parameters."""
        if isinstance(spec, (int, float)) and not isinstance(spec, bool):
            quantity = to_internal(float(spec), self.length_unit)
            return Resolved(format_quantity(quantity, self.length_unit), quantity.value, Dim.LENGTH)
        expr = evaluate(str(spec), self.parameters, self._units())
        return Resolved(expr.normalised, expr.value, expr.dim)

    # -- coordinates -------------------------------------------------------
    def coordinates(self, point: Sequence[float | int | str], what: str = "position") -> tuple[Resolved, ...]:
        """Resolve each component of a point, keeping its expression."""
        axes = "xyz"
        return tuple(
            self._resolve(component, self.length_unit, Dim.LENGTH, f"{what} {axes[index]}")
            for index, component in enumerate(point)
        )

    def point2d(self, point: Sequence[float | int | str]) -> tuple[float, float]:
        """Convert a 2D placement point from recipe units to centimetres."""
        x, y = self.coordinates(point)
        return (x.value, y.value)

    def point3d(self, point: Sequence[float | int | str]) -> tuple[float, float, float]:
        x, y, z = self.coordinates(point)
        return (x.value, y.value, z.value)

    def scalar_length(self, value: float) -> float:
        return to_internal(float(value), self.length_unit).value

    def degrees(self, value: float) -> float:
        """A plain angle in degrees -> radians (used for placement, not dimensions)."""
        return to_internal(float(value), "deg").value

    def literal_length(self, value_cm: float) -> Resolved:
        """Build an expression for an already-internal length, e.g. a placement offset."""
        quantity = Quantity(value_cm, Dim.LENGTH)
        return Resolved(format_quantity(quantity, self.length_unit), value_cm, Dim.LENGTH)

    def literal_angle(self, value_rad: float) -> Resolved:
        quantity = Quantity(value_rad, Dim.ANGLE)
        return Resolved(format_quantity(quantity, self.angle_unit), value_rad, Dim.ANGLE)


def _is_simple(source: str) -> bool:
    """True when *source* needs no brackets before a trailing ``* 1 mm``."""
    stripped = source.strip()
    return stripped.replace(".", "", 1).isdigit() or stripped.isidentifier()
