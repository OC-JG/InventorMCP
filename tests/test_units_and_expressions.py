from __future__ import annotations

import json
import math

import pytest

from inventor_mcp.errors import ExpressionError, UnitError
from inventor_mcp.expressions import UnitContext, evaluate, referenced_parameters, validate
from inventor_mcp.units import Dim, Quantity, convert, format_quantity, parse_quantity, to_internal


class TestUnits:
    def test_length_converts_to_centimetres(self):
        assert to_internal(10, "mm").value == pytest.approx(1.0)
        assert to_internal(1, "in").value == pytest.approx(2.54)
        assert to_internal(1, "ft").value == pytest.approx(30.48)

    def test_angles_convert_to_radians(self):
        assert to_internal(180, "deg").value == pytest.approx(math.pi)

    def test_convert_between_display_units(self):
        assert convert(25.4, "mm", "in") == pytest.approx(1.0)

    def test_unknown_unit_is_reported(self):
        with pytest.raises(UnitError) as info:
            to_internal(1, "furlong")
        assert "furlong" in str(info.value)

    def test_cannot_express_a_length_in_degrees(self):
        with pytest.raises(UnitError):
            Quantity(1.0, Dim.LENGTH).as_display("deg")

    def test_parse_and_format_round_trip(self):
        quantity = parse_quantity("12.5 mm")
        assert quantity.dim is Dim.LENGTH
        assert format_quantity(quantity, "mm") == "12.5 mm"

    def test_bare_number_takes_the_default_unit(self):
        assert parse_quantity("3", default_unit="in").value == pytest.approx(7.62)


class TestExpressions:
    def test_literal_with_unit(self):
        assert evaluate("25 mm").value == pytest.approx(2.5)

    def test_no_space_before_the_unit(self):
        assert evaluate("25mm").value == pytest.approx(2.5)

    def test_arithmetic_over_parameters(self):
        params = {"w": to_internal(100, "mm"), "t": to_internal(4, "mm")}
        assert evaluate("w - 2 * t", params).value == pytest.approx(9.2)

    def test_mixed_units_in_one_expression(self):
        assert evaluate("1 in + 6 mm").value == pytest.approx(3.14)

    def test_dimensions_multiply(self):
        assert evaluate("10 mm * 10 mm").dim is Dim.AREA
        assert evaluate("10 mm * 10 mm * 10 mm").dim is Dim.VOLUME

    def test_length_over_length_is_unitless(self):
        assert evaluate("50 mm / 10 mm").dim is Dim.UNITLESS
        assert evaluate("50 mm / 10 mm").value == pytest.approx(5.0)

    def test_adding_different_dimensions_is_rejected(self):
        with pytest.raises(ExpressionError, match="add or subtract"):
            evaluate("10 mm + 5 deg")

    def test_trigonometry_needs_an_angle(self):
        assert evaluate("sin(30 deg)").value == pytest.approx(0.5)
        with pytest.raises(ExpressionError, match="expects an angle"):
            evaluate("sin(10 mm)")

    def test_sqrt_of_an_area_is_a_length(self):
        assert evaluate("sqrt(100 mm * 100 mm)").dim is Dim.LENGTH
        with pytest.raises(ExpressionError, match="no representable unit"):
            evaluate("sqrt(10 mm)")

    def test_powers_use_the_caret(self):
        assert evaluate("2 ^ 3").value == pytest.approx(8.0)

    def test_unknown_parameter_names_the_known_ones(self):
        with pytest.raises(ExpressionError) as info:
            evaluate("width * 2", {"height": to_internal(10, "mm")})
        assert "height" in str(info.value.hint)

    def test_division_by_zero(self):
        with pytest.raises(ExpressionError, match="Division by zero"):
            evaluate("10 mm / 0")

    @pytest.mark.parametrize(
        "source",
        [
            "__import__('os').system('rm -rf /')",
            "open('/etc/passwd').read()",
            "(lambda: 1)()",
            "[x for x in range(10)]",
            "width.__class__",
            "{'a': 1}",
        ],
    )
    def test_hostile_input_is_rejected(self, source):
        with pytest.raises(ExpressionError):
            evaluate(source)

    def test_expression_length_is_capped(self):
        with pytest.raises(ExpressionError, match="too long"):
            evaluate("1 + " * 400 + "1")

    def test_referenced_parameters_ignores_functions(self):
        assert referenced_parameters("sqrt(w * h) + t") == {"w", "h", "t"}

    def test_validate_enforces_dimension(self):
        with pytest.raises(ExpressionError, match="must be a length"):
            validate("30 deg", expected=Dim.LENGTH, what="thickness")

    def test_validate_enforces_positive(self):
        with pytest.raises(ExpressionError, match="greater than zero"):
            validate("-5 mm", expected=Dim.LENGTH, positive=True, what="thickness")


class TestBareNumbersInMixedArithmetic:
    """Inventor reads ``width - 16`` as "16 document units"; so do we."""

    def test_a_bare_number_takes_the_document_length_unit(self):
        from inventor_mcp.expressions import UnitContext

        params = {"width": to_internal(100, "mm")}
        result = evaluate("width - 16", params, UnitContext(length="mm"))
        assert result.dim is Dim.LENGTH
        assert result.value == pytest.approx(8.4)

    def test_the_document_unit_is_actually_used(self):
        from inventor_mcp.expressions import UnitContext

        params = {"width": to_internal(4, "in")}
        result = evaluate("width - 1", params, UnitContext(length="in"))
        assert result.value == pytest.approx(to_internal(3, "in").value)

    def test_it_works_from_either_side(self):
        from inventor_mcp.expressions import UnitContext

        params = {"t": to_internal(6, "mm")}
        assert evaluate("10 - t", params, UnitContext(length="mm")).value == pytest.approx(0.4)

    def test_angles_promote_too(self):
        from inventor_mcp.expressions import UnitContext

        params = {"a": to_internal(90, "deg")}
        result = evaluate("a - 30", params, UnitContext(angle="deg"))
        assert result.dim is Dim.ANGLE
        assert result.value == pytest.approx(math.radians(60))

    def test_without_a_context_the_mismatch_is_still_an_error(self):
        with pytest.raises(ExpressionError, match="add or subtract"):
            evaluate("width - 16", {"width": to_internal(100, "mm")})

    def test_areas_are_never_promoted(self):
        from inventor_mcp.expressions import UnitContext

        with pytest.raises(ExpressionError, match="add or subtract"):
            evaluate("10 mm * 10 mm - 5", {}, UnitContext(length="mm"))


class TestNormalisedForInventor:
    """Inventor's expression parser is unit-strict; ours is forgiving.

    We accept `flange_d - 16` because engineers write it that way, so the
    expression handed to Inventor has to spell the unit out or it is refused.
    """

    def context(self):
        from inventor_mcp.expressions import UnitContext

        return UnitContext(length="mm", angle="deg")

    def test_a_promoted_literal_gains_its_unit(self):
        params = {"flange_d": to_internal(80, "mm")}
        result = evaluate("flange_d - 16", params, self.context())
        assert result.normalised == "flange_d - 16 mm"
        assert result.value == pytest.approx(6.4)

    def test_an_untouched_expression_is_passed_through_verbatim(self):
        params = {"w": to_internal(100, "mm"), "t": to_internal(4, "mm")}
        result = evaluate("w - 2 * t", params, self.context())
        assert result.normalised == "w - 2 * t"

    def test_a_scaling_factor_is_not_given_units(self):
        """`w / 2` divides by a count; only +/- operands get promoted."""
        params = {"w": to_internal(100, "mm")}
        result = evaluate("w / 2 - 5", params, self.context()).normalised
        assert "2 mm" not in result
        assert "5 mm" in result

    def test_it_survives_a_round_trip(self):
        params = {"flange_d": to_internal(80, "mm")}
        once = evaluate("flange_d - 16", params, self.context())
        twice = evaluate(once.normalised, params, self.context())
        assert twice.value == pytest.approx(once.value)
        assert twice.normalised == once.normalised

    def test_nested_arithmetic_keeps_its_meaning(self):
        params = {"a": to_internal(50, "mm"), "b": to_internal(10, "mm")}
        expression = evaluate("(a - 5) * 2 - b", params, self.context())
        assert expression.value == pytest.approx(evaluate(
            "(a - 5 mm) * 2 - b", params, self.context()).value)
        assert evaluate(expression.normalised, params, self.context()).value == pytest.approx(
            expression.value)

    def test_angles_are_normalised_too(self):
        params = {"draft": to_internal(30, "deg")}
        result = evaluate("draft + 15", params, self.context())
        assert result.normalised == "draft + 15 deg"
        assert result.value == pytest.approx(math.radians(45))

    def test_a_promoted_name_is_scaled_rather_than_suffixed(self):
        """A unitless *parameter* cannot take a suffix, so it is multiplied."""
        params = {"w": to_internal(100, "mm"), "count": to_internal(3, "ul")}
        result = evaluate("w - count", params, self.context())
        assert result.normalised == "w - (count) * 1 mm"
        assert evaluate(result.normalised, params, self.context()).value == pytest.approx(
            result.value)


class TestNegationSurvivesNormalisation:
    """What Inventor is handed has to be the same number the recipe meant.

    `normalised` exists because Inventor's parser will not add a length
    parameter to a bare number, so `flange_d - 16` has to go out as
    `flange_d - 16 mm`. Rewriting it must not change its value -- and under a
    unary minus it did: `-(a + 2)` came out as `-a + 2`, four millimetres
    different, with the simulator keeping the right value and neither side
    complaining.
    """

    def check(self, source, params=None):
        from inventor_mcp.expressions import UnitContext, evaluate
        from inventor_mcp.units import to_internal

        table = {name: to_internal(value, "mm")
                 for name, value in (params or {"a": 100.0}).items()}
        units = UnitContext(length="mm", angle="deg")
        original = evaluate(source, table, units)
        again = evaluate(original.normalised, table, units)
        return original, again

    @pytest.mark.parametrize("source", [
        "-(a + 2)",
        "-(a - 2)",
        "-(2 + a)",
        "-(a * 2 + 1)",
        "-(-(a + 2))",
        "-(a + 2) * 2",
        "2 * -(a + 2)",
        "-a + 2",
        "-a",
        "(a + 2) * 2",
        "a / (a + 2)",
    ])
    def test_rewriting_never_changes_the_value(self, source):
        original, again = self.check(source)
        assert again.value == pytest.approx(original.value), (
            f"{source!r} normalised to {original.normalised!r}")

    def test_the_brackets_are_actually_kept(self):
        original, _ = self.check("-(a + 2)")
        assert original.normalised == "-(a + 2 mm)"

    def test_a_bare_negation_needs_no_brackets(self):
        original, _ = self.check("-a + 2")
        assert original.normalised == "-a + 2 mm"


class TestUnsignedDimensions:
    """A driving dimension is a magnitude, so a negative one has to be flipped.

    Flipping used to mean deleting a leading minus, which is the negation only
    when that minus governs the whole expression. `-a` negates to `a`; `-a + 2`
    does not negate to `a + 2`. With a = 100 mm that shipped a dimension of
    +102 mm for a distance the recipe put at -98 mm.
    """

    def resolver(self):
        from inventor_mcp.resolve import Resolver
        from inventor_mcp.units import to_internal

        resolver = Resolver("mm", "deg")
        resolver.declare("a", to_internal(100.0, "mm"))
        return resolver

    @pytest.mark.parametrize("source", [
        "-a + 2", "-(a + 2)", "2 - a", "-a", "-a - 5", "-a * 2", "-(a * 2 + 1)",
    ])
    def test_the_emitted_dimension_is_the_magnitude(self, source):
        from inventor_mcp.geometry import _magnitude

        resolver = self.resolver()
        signed = resolver.length(source)
        assert signed.value < 0, "fixture should be negative"
        unsigned = _magnitude(signed)
        assert unsigned.value == pytest.approx(-signed.value)
        # And the expression Inventor evaluates has to agree with that value,
        # which is the half that was wrong.
        assert resolver.length(unsigned.expression).value == pytest.approx(unsigned.value), (
            f"{source!r} emitted {unsigned.expression!r}")

    def test_a_positive_value_is_left_exactly_as_written(self):
        from inventor_mcp.geometry import _magnitude

        resolved = self.resolver().length("a + 2")
        assert _magnitude(resolved) is resolved

    def test_a_whole_expression_negation_stays_readable(self):
        """No gratuitous brackets where deleting the minus is genuinely right."""
        from inventor_mcp.geometry import _magnitude

        assert _magnitude(self.resolver().length("-a")).expression == "a"
        assert _magnitude(self.resolver().length("-(a + 2)")).expression == "(a + 2 mm)"

    def test_abs_is_no_longer_emitted(self):
        """Nothing ever confirmed Inventor's parser accepts abs()."""
        from inventor_mcp.geometry import _magnitude

        for source in ("2 - a", "-a + 2", "-a * 2"):
            assert "abs" not in _magnitude(self.resolver().length(source)).expression


class TestParametricCounts:
    """A count should be revisable the way a length is.

    Counts were plain integers, so "one bolt per 60 mm of pitch circle" could
    not be said and a pattern's count could not be driven by a parameter while
    its spacing could. Inventor allows an expression there, so the recipe does
    too -- but 4.5 holes is a mistake rather than something to round.
    """

    def resolver(self, **parameters):
        from inventor_mcp.expressions import Dim, Quantity
        from inventor_mcp.resolve import Resolver

        resolver = Resolver("mm", "deg")
        for name, value in parameters.items():
            resolver.declare(name, Quantity(float(value), Dim.UNITLESS))
        return resolver

    @pytest.mark.parametrize("spec,expected", [
        (5, 5), ("5", 5), (5.0, 5), ("n", 6), ("n + 2", 8), ("n * 2", 12),
        ("n / 2", 3), ("n - 5", 1),
    ])
    def test_a_whole_number_resolves(self, spec, expected):
        assert self.resolver(n=6).count(spec) == expected

    def test_a_fraction_is_refused_rather_than_rounded(self):
        from inventor_mcp.errors import ExpressionError

        with pytest.raises(ExpressionError, match="whole number"):
            self.resolver(n=5).count("n / 2")

    def test_the_bounds_are_checked_after_resolution(self):
        """A string cannot be range-checked before its parameters are known."""
        from inventor_mcp.errors import ExpressionError

        resolver = self.resolver(n=500)
        with pytest.raises(ExpressionError, match="between 1 and 200"):
            resolver.count("n", maximum=200)
        with pytest.raises(ExpressionError, match="between 3 and 120"):
            self.resolver(n=2).count("n", minimum=3, maximum=120)

    def test_floating_point_slack_is_tolerated(self):
        """0.1 + 0.2 is not 0.3, and a count derived that way is still whole."""
        assert self.resolver(n=10).count("n / 10 * 3 * 10 / 3") == 10


class TestCountsInARecipe:
    def build(self, spokes):
        from inventor_mcp.builder import build_part
        from inventor_mcp.schema import PartRecipe
        from inventor_mcp.session import Session

        session = Session(backend_kind="mock")
        session.ensure_backend().connect()
        build_part(session, PartRecipe.model_validate({
            "name": "Counts", "units": "mm",
            "parameters": [{"name": "spokes", "value": spokes, "unit": "ul"},
                           {"name": "pcd", "value": 100}],
            "operations": [
                {"op": "sketch", "name": "Disc", "plane": "xy", "entities": [
                    {"type": "circle", "diameter": 120}]},
                {"op": "extrude", "sketch": "Disc", "distance": 8},
                {"op": "sketch", "name": "Bolts", "plane": "xy", "entities": [
                    {"type": "bolt_circle", "diameter": "pcd", "count": "spokes"}]},
                {"op": "sketch", "name": "Hex", "plane": "xy", "entities": [
                    {"type": "polygon", "sides": "spokes", "size": 30,
                     "fit": "circumscribed"}]},
                {"op": "sketch", "name": "Grid", "plane": "xy", "entities": [
                    {"type": "point_grid", "columns": "spokes", "rows": 2,
                     "x_spacing": 10, "y_spacing": 10}]},
            ]}))
        return {s.name: s.plan for s in session.backend._doc(session.active).sketches}

    @pytest.mark.parametrize("spokes", [3, 6, 8])
    def test_a_bolt_circles_count_follows_its_parameter(self, spokes):
        assert len(self.build(spokes)["Bolts"].hole_centers) == spokes

    @pytest.mark.parametrize("spokes", [3, 6, 8])
    def test_a_polygons_sides_do_too(self, spokes):
        from inventor_mcp.plan import PLine

        sides = [p for p in self.build(spokes)["Hex"].primitives
                 if isinstance(p, PLine) and not p.construction]
        assert len(sides) == spokes

    @pytest.mark.parametrize("spokes", [3, 6, 8])
    def test_and_a_point_grids_columns(self, spokes):
        assert len(self.build(spokes)["Grid"].hole_centers) == spokes * 2

    def test_a_pattern_count_may_be_an_expression(self):
        from inventor_mcp.builder import rehearse
        from inventor_mcp.schema import PartRecipe

        report = rehearse(PartRecipe.model_validate({
            "name": "Patterned", "units": "mm",
            "parameters": [{"name": "ribs", "value": 4, "unit": "ul"},
                           {"name": "pitch", "value": 20}],
            "operations": [
                {"op": "sketch", "name": "Rib", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 5, "height": 40}]},
                {"op": "extrude", "name": "First", "sketch": "Rib", "distance": 6},
                {"op": "rectangular_pattern", "features": ["First"], "axis1": "x",
                 "count1": "ribs + 1", "spacing1": "pitch"},
            ]}))
        assert report["ok"], report["findings"]
        pattern = report["steps"][-1]
        assert pattern["op"] == "rectangular_pattern"

    def test_a_fractional_count_fails_validation_not_the_build(self):
        from inventor_mcp.builder import rehearse
        from inventor_mcp.schema import PartRecipe

        report = rehearse(PartRecipe.model_validate({
            "name": "Bad", "units": "mm",
            "parameters": [{"name": "n", "value": 5, "unit": "ul"}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "bolt_circle", "diameter": 60, "count": "n / 2"}]}]}))
        assert report["ok"] is False
        assert "whole number" in report["findings"][0]["error"]


class TestNamesWithDigitsInThem:
    """`boss1_d`, `m3_clearance`, Inventor's own d0/d1 model parameters.

    The literal tokeniser used to split an identifier at its digits:
    `Parameter1` became `Parameter __q__(1,'')`, a syntax error when the pieces
    touch and -- far worse -- a clean parse in `referenced_parameters`, with the
    name silently absent. Discovery then dropped a wall candidate and the freeze
    closure lost a declared dependency, both without a word.
    """

    @pytest.mark.parametrize("source, names", [
        ("Parameter1 * 2", {"Parameter1"}),
        ("wall2 - 1", {"wall2"}),
        ("d0 / 2", {"d0"}),
        ("boss1_d + wall", {"boss1_d", "wall"}),
        ("m3_clearance", {"m3_clearance"}),
    ])
    def test_they_are_seen(self, source, names):
        assert referenced_parameters(source) == names

    def test_and_they_evaluate(self):
        context = UnitContext("mm", "deg")
        two = Quantity(2.0, Dim.LENGTH)
        assert evaluate("boss1_d / 2", {"boss1_d": two}, context).value == 1.0

    @pytest.mark.parametrize("source", ["12.5 mm", ".5 in", "1e3 mm", "2 * 3"])
    def test_a_literal_is_still_a_literal(self, source):
        assert referenced_parameters(source) == set()

    def test_the_freeze_closure_follows_a_digit_named_dependency(self):
        """The consequence that made this worth a class of its own: freezing
        `seal_face = boss1_d - 0.4` must protect boss1_d."""
        from inventor_mcp.dfm.freeze import FreezeGuard
        guard = FreezeGuard(["seal_face"], expressions={
            "boss1_d": "8", "seal_face": "boss1_d - 0.4"})
        assert guard.check("boss1_d") is not None


class TestAValueThatIsNotANumber:
    """`inf` and `nan` are numbers to Python and not to JSON.

    `**` guards its own overflow and `*` does not, so `1e308 * 1e308` evaluated
    to `inf` and `inf - inf` to `nan`, and both then travelled the whole way
    down as ordinary values. The build reported `ok: true` with no errors and
    handed back mass properties holding `NaN` and `-Infinity` -- which are not
    JSON, so a strict client cannot read even the reply claiming the part is
    fine. Refused at the evaluator, which every value passes through.
    """

    @pytest.mark.parametrize("source", [
        "1e308 * 1e308",
        "1e308 * 1e308 - 1e308 * 1e308",
        "0 * (1e308 * 1e308)",
        "w * 1e308 * 1e308",
    ])
    def test_it_is_refused(self, source):
        with pytest.raises(ExpressionError) as caught:
            evaluate(source, {"w": Quantity(10.0, Dim.LENGTH)})
        assert "not a usable dimension" in str(caught.value)

    @pytest.mark.parametrize("source", ["w * 2", "sqrt(2) * w", "1e300", "w / 3"])
    def test_a_large_but_finite_value_still_passes(self, source):
        """The guard must not start refusing arithmetic that works."""
        assert math.isfinite(evaluate(source, {"w": Quantity(10.0, Dim.LENGTH)}).value)

    def test_the_build_says_so_instead_of_reporting_a_part(self):
        from inventor_mcp.builder import build_part
        from inventor_mcp.schema import PartRecipe
        from inventor_mcp.session import Session

        session = Session(backend_kind="mock")
        session.ensure_backend().connect()
        out = build_part(session, PartRecipe.model_validate({
            "name": "Overflow", "units": "mm",
            "parameters": [{"name": "w", "value": "1e308 * 1e308 * 1 mm"}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": "w", "height": 10}]},
                {"op": "extrude", "sketch": "S", "distance": 5}]}))
        assert out["ok"] is False
        assert "not a usable dimension" in out["errors"][0]["error"]

    def test_and_the_reply_is_json_a_strict_parser_will_read(self):
        """`json.dumps` emits bare NaN and Infinity quite happily. Nothing else does."""
        from inventor_mcp.builder import build_part
        from inventor_mcp.schema import PartRecipe
        from inventor_mcp.session import Session

        session = Session(backend_kind="mock")
        session.ensure_backend().connect()
        out = build_part(session, PartRecipe.model_validate({
            "name": "Overflow", "units": "mm",
            "parameters": [{"name": "w", "value": "1e308 * 1e308 * 1 mm"}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": "w", "height": 10}]},
                {"op": "extrude", "sketch": "S", "distance": 5}]}))

        def reject(token):
            raise AssertionError(f"the reply carries {token!r}, which is not JSON")

        json.loads(json.dumps(out), parse_constant=reject)
