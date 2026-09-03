"""A safe, dimension-aware evaluator for Inventor-style parameter expressions.

Inventor lets a dimension be driven by an expression such as ``"width / 2 -
wall"``.  We never execute those strings ourselves in the COM backend -- we
hand them to Inventor -- but we *do* need to

* reject nonsense before it reaches Inventor (where the error messages are
  famously opaque),
* check that an expression has the dimension the caller expects, and
* evaluate it offline so the mock backend can do real arithmetic.

The evaluator walks a restricted Python AST.  No attribute access, no
subscripting, no comprehensions, no names other than declared parameters and a
small whitelist of maths functions.
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass
from typing import Callable, Mapping

from .errors import ExpressionError
from .units import Dim, Quantity, dim_from_exponents, exponents_for, is_unit, lookup_unit

__all__ = [
    "Expr",
    "UnitContext",
    "evaluate",
    "validate",
    "referenced_parameters",
    "MAX_EXPRESSION_LENGTH",
]

MAX_EXPRESSION_LENGTH = 512

# Exponent triple: (length, angle, mass)
_Exponents = tuple[int, int, int]
_DIMENSIONLESS: _Exponents = (0, 0, 0)


@dataclass(frozen=True)
class _Value:
    number: float
    exponents: _Exponents = _DIMENSIONLESS

    @property
    def dim(self) -> Dim:
        return dim_from_exponents(*self.exponents)


def _as_value(quantity: Quantity) -> _Value:
    return _Value(quantity.value, exponents_for(quantity.dim))


def _as_quantity(value: _Value) -> Quantity:
    dim = value.dim
    if dim is Dim.UNKNOWN:
        raise ExpressionError(
            "Expression produced a quantity with no meaningful unit "
            f"(length^{value.exponents[0]} angle^{value.exponents[1]} mass^{value.exponents[2]})."
        )
    if not math.isfinite(value.number):
        # `**` guards its own overflow; `*` and `-` do not, so `1e308 * 1e308`
        # came through as `inf` and `inf - inf` as `nan`. Both then travelled
        # the whole way down as ordinary numbers: the build reported ok, and
        # the mass properties came back holding `NaN` and `-Infinity` -- which
        # are not JSON, so a strict client cannot even read the reply that says
        # the part is fine.
        raise ExpressionError(
            f"Expression evaluates to {value.number}, which is not a usable "
            "dimension.",
            hint="Check for an overflow, or for a subtraction of two very "
                 "large numbers.",
        )
    return Quantity(value.number, dim)


# --------------------------------------------------------------------------
# Tokenising numeric literals with units into calls the AST can carry
# --------------------------------------------------------------------------

_QUANTITY_FUNC = "__q__"

_LITERAL_RE = re.compile(
    r"""(?<![\w.])                                          # not inside a name
        (?P<number>(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)   # 12, 12.5, .5, 1e3
        (?P<gap>\s*)
        (?P<unit>[A-Za-z]+|")?                              # mm, in, deg, "
    """,
    re.VERBOSE,
)
# The lookbehind is what lets a parameter be called boss1_d or m3_clearance.
# Without it, the digits inside an identifier were read as a literal and the
# name was cut at them: `Parameter1` tokenised as `Parameter` `__q__(1,'')`,
# which is a syntax error when the pieces touch and -- far worse -- parses
# cleanly in `referenced_parameters`' AST walk with the name silently absent.
# Everything downstream of that walk then simply did not see the parameter:
# discovery dropped a wall candidate, and the freeze closure lost a dependency
# somebody had declared, both without a word. Inventor's own model parameters
# are named d0, d1, d2, so this was not an exotic spelling.


def _rewrite_literals(source: str) -> str:
    """Turn ``12.5 mm`` into ``__q__(12.5,'mm')`` so Python can parse it."""

    def replace(match: re.Match[str]) -> str:
        number = match.group("number")
        unit = match.group("unit")
        if unit and is_unit(unit):
            return f"{_QUANTITY_FUNC}({number},'{unit}')"
        # Not a unit: emit the bare number and let the identifier stand alone.
        # ``2 x`` is invalid Python anyway and will be reported as a syntax error.
        tail = (match.group("gap") or "") + (unit or "")
        return f"{_QUANTITY_FUNC}({number},'')" + tail

    return _LITERAL_RE.sub(replace, source)


# --------------------------------------------------------------------------
# Function whitelist
# --------------------------------------------------------------------------


def _require_dimensionless(name: str, value: _Value) -> float:
    if value.exponents != _DIMENSIONLESS:
        raise ExpressionError(
            f"{name}() expects a unitless argument but got a {value.dim.value} value."
        )
    return value.number


def _trig(name: str, fn: Callable[[float], float]) -> Callable[..., _Value]:
    def call(value: _Value) -> _Value:
        angle_exponents = exponents_for(Dim.ANGLE)
        if value.exponents not in (angle_exponents, _DIMENSIONLESS):
            raise ExpressionError(f"{name}() expects an angle but got a {value.dim.value} value.")
        return _Value(fn(value.number))

    return call


def _inverse_trig(name: str, fn: Callable[..., float], arity: int = 1) -> Callable[..., _Value]:
    def call(*values: _Value) -> _Value:
        numbers = [_require_dimensionless(name, v) for v in values]
        if len(numbers) != arity:
            raise ExpressionError(f"{name}() takes {arity} argument(s), got {len(numbers)}.")
        return _Value(fn(*numbers), exponents_for(Dim.ANGLE))

    return call


def _sqrt(value: _Value) -> _Value:
    if any(e % 2 for e in value.exponents):
        raise ExpressionError(
            f"sqrt() of a {value.dim.value} value has no representable unit "
            "(try sqrt of an area or of a unitless number)."
        )
    if value.number < 0:
        raise ExpressionError("sqrt() of a negative number.")
    return _Value(math.sqrt(value.number), tuple(e // 2 for e in value.exponents))  # type: ignore[arg-type]


def _same_dim_reduce(name: str, fn: Callable[..., float]) -> Callable[..., _Value]:
    def call(*values: _Value) -> _Value:
        if not values:
            raise ExpressionError(f"{name}() needs at least one argument.")
        first = values[0].exponents
        for value in values[1:]:
            if value.exponents != first:
                raise ExpressionError(f"{name}() arguments must all have the same units.")
        return _Value(fn(*(v.number for v in values)), first)

    return call


def _unary_same_dim(name: str, fn: Callable[[float], float]) -> Callable[..., _Value]:
    def call(value: _Value) -> _Value:
        return _Value(fn(value.number), value.exponents)

    return call


_FUNCTIONS: dict[str, Callable[..., _Value]] = {
    "sin": _trig("sin", math.sin),
    "cos": _trig("cos", math.cos),
    "tan": _trig("tan", math.tan),
    "asin": _inverse_trig("asin", math.asin),
    "acos": _inverse_trig("acos", math.acos),
    "atan": _inverse_trig("atan", math.atan),
    "atan2": _inverse_trig("atan2", math.atan2, arity=2),
    "sqrt": _sqrt,
    "abs": _unary_same_dim("abs", abs),
    "floor": _unary_same_dim("floor", lambda v: float(math.floor(v))),
    "ceil": _unary_same_dim("ceil", lambda v: float(math.ceil(v))),
    "round": _unary_same_dim("round", lambda v: float(round(v))),
    "min": _same_dim_reduce("min", min),
    "max": _same_dim_reduce("max", max),
    "sign": lambda value: _Value(float((value.number > 0) - (value.number < 0))),
    "ln": lambda value: _Value(math.log(_require_dimensionless("ln", value))),
    "log": lambda value: _Value(math.log10(_require_dimensionless("log", value))),
    "exp": lambda value: _Value(math.exp(_require_dimensionless("exp", value))),
}

_CONSTANTS: dict[str, _Value] = {
    "PI": _Value(math.pi),
    "pi": _Value(math.pi),
    "E": _Value(math.e),
}

#: Names a user parameter may not take because Inventor or we reserve them.
RESERVED_NAMES = frozenset(_FUNCTIONS) | frozenset(_CONSTANTS) | {"d", "ul"}


# --------------------------------------------------------------------------
# Evaluator
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitContext:
    """The units a bare number takes when it meets a dimensioned one.

    Inventor treats ``width - 16`` in a length field as "16 document units",
    and users write expressions that way constantly.  Carrying the document's
    units here lets us do the same instead of rejecting the expression.
    """

    length: str | None = None
    angle: str | None = None

    def symbol_for(self, dim: Dim) -> str | None:
        return {Dim.LENGTH: self.length, Dim.ANGLE: self.angle}.get(dim)

    def factor_for(self, dim: Dim) -> float | None:
        symbol = self.symbol_for(dim)
        return None if symbol is None else lookup_unit(symbol).factor


class _Evaluator(ast.NodeVisitor):
    def __init__(self, parameters: Mapping[str, Quantity], units: UnitContext | None = None) -> None:
        self.parameters = parameters
        self.units = units or UnitContext()
        self.referenced: set[str] = set()
        #: AST nodes whose bare number took the document's units, and which unit.
        #: Inventor's own parser is unit-strict, so the expression it is given
        #: has to say so explicitly.
        self.promoted: dict[int, str] = {}

    def _promote(self, left_node: ast.AST, left: _Value,
                 right_node: ast.AST, right: _Value) -> tuple[_Value, _Value]:
        """Give a bare number the units of the value it is being added to."""
        if left.exponents == right.exponents:
            return left, right
        for plain_node, plain, other, swap in (
            (left_node, left, right, False),
            (right_node, right, left, True),
        ):
            if plain.exponents != _DIMENSIONLESS or other.exponents == _DIMENSIONLESS:
                continue
            symbol = self.units.symbol_for(other.dim)
            if symbol is None:
                continue
            factor = lookup_unit(symbol).factor
            self.promoted[id(plain_node)] = symbol
            promoted = _Value(plain.number * factor, other.exponents)
            return (other, promoted) if swap else (promoted, other)
        return left, right

    # -- leaves ------------------------------------------------------------
    def visit_Expression(self, node: ast.Expression) -> _Value:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> _Value:
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ExpressionError(f"Unsupported literal {node.value!r} in expression.")
        return _Value(float(node.value))

    def visit_Name(self, node: ast.Name) -> _Value:
        name = node.id
        if name in self.parameters:
            self.referenced.add(name)
            return _as_value(self.parameters[name])
        if name in _CONSTANTS:
            return _CONSTANTS[name]
        known = ", ".join(sorted(self.parameters)) or "(none defined)"
        raise ExpressionError(
            f"Unknown parameter {name!r}.",
            hint=f"Declare it with `set_parameters` first. Known parameters: {known}.",
            unknown_parameter=name,
        )

    # -- operators ---------------------------------------------------------
    def visit_BinOp(self, node: ast.BinOp) -> _Value:
        left = self.visit(node.left)
        right = self.visit(node.right)
        op = node.op

        if isinstance(op, (ast.Add, ast.Sub)):
            left, right = self._promote(node.left, left, node.right, right)
            if left.exponents != right.exponents:
                raise ExpressionError(
                    f"Cannot add or subtract a {left.dim.value} and a {right.dim.value} value.",
                    hint="Both sides of + and - must have the same units. A bare number only "
                    "takes the document's units when the other side is a length or an angle.",
                )
            value = left.number + right.number if isinstance(op, ast.Add) else left.number - right.number
            return _Value(value, left.exponents)

        if isinstance(op, ast.Mult):
            return _Value(
                left.number * right.number,
                tuple(a + b for a, b in zip(left.exponents, right.exponents)),  # type: ignore[arg-type]
            )

        if isinstance(op, ast.Div):
            if right.number == 0:
                raise ExpressionError("Division by zero in expression.")
            return _Value(
                left.number / right.number,
                tuple(a - b for a, b in zip(left.exponents, right.exponents)),  # type: ignore[arg-type]
            )

        if isinstance(op, ast.Mod):
            if left.exponents != right.exponents:
                raise ExpressionError("Both sides of % must have the same units.")
            if right.number == 0:
                raise ExpressionError("Modulo by zero in expression.")
            return _Value(math.fmod(left.number, right.number), left.exponents)

        if isinstance(op, ast.Pow):
            if right.exponents != _DIMENSIONLESS:
                raise ExpressionError("The exponent of ^ must be a unitless number.")
            power = right.number
            if left.exponents != _DIMENSIONLESS and power != int(power):
                raise ExpressionError(
                    f"Cannot raise a {left.dim.value} value to a fractional power."
                )
            exponents = tuple(int(e * power) for e in left.exponents)
            try:
                number = left.number**power
            except (OverflowError, ValueError) as exc:
                raise ExpressionError(f"Cannot evaluate power: {exc}") from exc
            return _Value(float(number), exponents)  # type: ignore[arg-type]

        raise ExpressionError(f"Unsupported operator {type(op).__name__} in expression.")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> _Value:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.USub):
            return _Value(-operand.number, operand.exponents)
        if isinstance(node.op, ast.UAdd):
            return operand
        raise ExpressionError(f"Unsupported unary operator {type(node.op).__name__}.")

    def visit_Call(self, node: ast.Call) -> _Value:
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("Only direct function calls are allowed in expressions.")
        name = node.func.id
        if node.keywords:
            raise ExpressionError("Keyword arguments are not allowed in expressions.")

        if name == _QUANTITY_FUNC:
            number = node.args[0]
            unit = node.args[1]
            assert isinstance(number, ast.Constant) and isinstance(unit, ast.Constant)
            symbol = str(unit.value)
            if not symbol:
                return _Value(float(number.value))
            info = lookup_unit(symbol)
            return _Value(float(number.value) * info.factor, exponents_for(info.dim))

        fn = _FUNCTIONS.get(name)
        if fn is None:
            raise ExpressionError(
                f"Unknown function {name!r}.",
                hint="Available functions: " + ", ".join(sorted(_FUNCTIONS)),
            )
        args = [self.visit(arg) for arg in node.args]
        try:
            return fn(*args)
        except ExpressionError:
            raise
        except TypeError as exc:
            raise ExpressionError(f"Bad arguments to {name}(): {exc}") from exc
        except ValueError as exc:
            raise ExpressionError(f"{name}() is undefined for that input: {exc}") from exc

    def generic_visit(self, node: ast.AST) -> _Value:  # noqa: D102
        raise ExpressionError(
            f"{type(node).__name__} is not allowed in a parameter expression.",
            hint="Expressions may use numbers with units, parameter names, "
            "+ - * / ^ and the standard maths functions.",
        )


#: Binary operators, rendered back in Inventor's syntax (which uses ^ for power).
_OPERATORS = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
    ast.Mod: "%", ast.Pow: "^",
}


def _render(node: ast.AST, promoted: Mapping[int, str]) -> str:
    """Write an expression back out, with promoted literals carrying their unit.

    Inventor's expression parser is unit-strict: it will not add a length
    parameter to a bare number.  We accept ``flange_d - 16`` because engineers
    write it that way, so what Inventor is handed has to say ``16 mm``.
    """
    unit = promoted.get(id(node))

    if isinstance(node, ast.Expression):
        return _render(node.body, promoted)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == _QUANTITY_FUNC:
            number, symbol = node.args[0], node.args[1]
            assert isinstance(number, ast.Constant) and isinstance(symbol, ast.Constant)
            text = f"{number.value:g}"
            suffix = unit or str(symbol.value)
            return f"{text} {suffix}" if suffix else text
        rendered = f"{node.func.id}({', '.join(_render(a, promoted) for a in node.args)})"
        return f"({rendered}) * 1 {unit}" if unit else rendered
    if isinstance(node, ast.Name):
        return f"({node.id}) * 1 {unit}" if unit else node.id
    if isinstance(node, ast.Constant):
        text = f"{node.value:g}"
        return f"{text} {unit}" if unit else text
    if isinstance(node, ast.UnaryOp):
        sign = "-" if isinstance(node.op, ast.USub) else "+"
        operand = _render(node.operand, promoted)
        # Brackets for the same reason the BinOp branch below uses them, and
        # for a sharper one: without them `-(a + 2)` renders as `-a + 2`,
        # which is a different number. The BinOp branch had this right from
        # the start; this one silently handed Inventor the wrong expression
        # while the simulator kept the right value, so the two disagreed and
        # neither complained.
        if isinstance(node.operand, (ast.BinOp, ast.UnaryOp)):
            operand = f"({operand})"
        rendered = f"{sign}{operand}"
        return f"({rendered}) * 1 {unit}" if unit else rendered
    if isinstance(node, ast.BinOp):
        operator = _OPERATORS.get(type(node.op))
        if operator is None:  # pragma: no cover - guarded during evaluation
            raise ExpressionError(f"Cannot render operator {type(node.op).__name__}.")
        left, right = _render(node.left, promoted), _render(node.right, promoted)
        # Brackets round any nested operation: verbose, but never wrong, and
        # Inventor does not care.
        if isinstance(node.left, ast.BinOp):
            left = f"({left})"
        if isinstance(node.right, ast.BinOp):
            right = f"({right})"
        rendered = f"{left} {operator} {right}"
        return f"({rendered}) * 1 {unit}" if unit else rendered
    raise ExpressionError(  # pragma: no cover - the evaluator rejects these first
        f"Cannot render {type(node).__name__} back to an expression."
    )


@dataclass(frozen=True)
class Expr:
    """The result of evaluating an expression."""

    source: str
    quantity: Quantity
    referenced: frozenset[str]
    #: ``source`` with any bare number that took the document's units written
    #: out explicitly, so Inventor's unit-strict parser accepts it.
    normalised: str = ""

    @property
    def dim(self) -> Dim:
        return self.quantity.dim

    @property
    def value(self) -> float:
        """Value in Inventor database units (cm / rad / kg)."""
        return self.quantity.value


def _parse(source: str) -> ast.Expression:
    if len(source) > MAX_EXPRESSION_LENGTH:
        raise ExpressionError(
            f"Expression is too long ({len(source)} > {MAX_EXPRESSION_LENGTH} characters)."
        )
    # Inventor writes powers as ^; Python needs **.
    normalised = _rewrite_literals(source).replace("^", "**")
    try:
        return ast.parse(normalised, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(
            f"Could not parse expression {source!r}: {exc.msg}.",
            hint="Check for unbalanced brackets or a missing operator.",
        ) from exc


def evaluate(
    source: str,
    parameters: Mapping[str, Quantity] | None = None,
    units: UnitContext | None = None,
) -> Expr:
    """Evaluate *source*, returning its value in database units and dimension.

    *units* supplies the document's length and angle units so that a bare
    number added to a dimensioned one is read the way Inventor reads it.
    """
    if not source or not source.strip():
        raise ExpressionError("Expression is empty.")
    tree = _parse(source)
    evaluator = _Evaluator(parameters or {}, units)
    value = evaluator.visit(tree)
    normalised = (
        _render(tree, evaluator.promoted) if evaluator.promoted else source.strip()
    )
    return Expr(
        source.strip(), _as_quantity(value), frozenset(evaluator.referenced), normalised
    )


def referenced_parameters(source: str) -> set[str]:
    """Names referenced by *source*, without needing them to be defined."""
    tree = _parse(source)
    names: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return {name for name in names - called if name not in _CONSTANTS and name != _QUANTITY_FUNC}


def validate(
    source: str,
    parameters: Mapping[str, Quantity] | None = None,
    *,
    expected: Dim | None = None,
    positive: bool = False,
    what: str = "value",
    units: UnitContext | None = None,
) -> Expr:
    """Evaluate and additionally enforce a dimension and/or sign."""
    expr = evaluate(source, parameters, units)
    if expected is not None and expr.dim is not expected:
        if not (expected is Dim.ANGLE and expr.dim is Dim.UNITLESS):
            raise ExpressionError(
                f"{what} must be a {expected.value}, but {source!r} evaluates to a {expr.dim.value}.",
                hint=f"Add a unit, e.g. '{source} mm'." if expected is Dim.LENGTH else None,
            )
    if positive and expr.value <= 0:
        raise ExpressionError(
            f"{what} must be greater than zero, but {source!r} evaluates to {expr.value:g}."
        )
    return expr
