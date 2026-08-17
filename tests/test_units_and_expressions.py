from __future__ import annotations

import math

import pytest

from inventor_mcp.errors import ExpressionError, UnitError
from inventor_mcp.expressions import evaluate, referenced_parameters, validate
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
