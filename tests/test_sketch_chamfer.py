"""`chamfers` on a rectangle or a polyline: the corner cut off, not rounded.

The other half of the roadmap item the sketch fillet was split into. It is the
same shape of work with a line instead of an arc, and the reason it could
land without reading a published call is that this planner writes the geometry
either way -- `SketchArcs.AddByFillet` was not used for the radius, so there
is nothing to look up for the chamfer.

**Where it is exact and where it is refused** is the decision worth reading.
A chamfered corner has two degrees of freedom -- each end of the chamfer line
can slide along the edge it meets -- so it takes two dimensions, and the two
this writes are the chamfer line's horizontal and vertical spans. On a square
corner between axis-aligned edges those are each the recipe's own expression,
exactly, and the chamfer follows its parameter with no trigonometry anywhere.
On an oblique corner they would be that expression times a cosine, which bakes
in the angle the corner happens to have now and stops being a `d` setback the
moment the outline is revised -- so an oblique corner is refused with the
reason. A chord length plus an angle is the drafting answer there, and nothing
here writes one.

Note what is *not* shared between corners: the radius version gives one
`radius` dimension and carries it with `equal_radius`, and the chamfer cannot,
because a chord length alone does not say a chamfer is symmetric.
"""

from __future__ import annotations

import math

import pytest

from inventor_mcp.builder import build_part
from inventor_mcp.geometry import loop_points, plan_sketch, profile_loops
from inventor_mcp.plan import PArc, PLine
from inventor_mcp.resolve import Resolver
from inventor_mcp.schema import PartRecipe, SketchOp
from inventor_mcp.units import Dim, Quantity

#: An L-bracket, anticlockwise, with the notch at [20, 20] -- so the inward
#: corner is chamfered too, and gains the triangle the outward ones lose.
L_BRACKET = [[0, 0], [60, 0], [60, 20], [20, 20], [20, 40], [0, 40]]


def plan_of(entity: dict):
    return plan_sketch(
        SketchOp.model_validate(
            {"op": "sketch", "name": "S", "plane": "xy", "entities": [entity]}),
        Resolver("mm", "deg"))


def area_of(entity: dict) -> float:
    plan = plan_of(entity)
    points = loop_points(plan, profile_loops(plan)[0])
    return 0.5 * abs(sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))))


class TestTheAreaIsExact:
    """No sampling anywhere: a chamfered outline is straight lines, so the
    shoelace area is the shape's own and the comparison is to the digit. That
    is a stricter check than the fillet gets, and it is free."""

    def test_a_chamfered_rectangle(self):
        """Each corner loses a right triangle with legs `d`, so four of them
        lose `2 * d^2`."""
        assert area_of({"type": "rectangle", "center": [0, 0], "width": 60,
                        "height": 40, "chamfers": 5}) == pytest.approx(
            (60 * 40 - 2 * 5 ** 2) / 100, rel=1e-12)

    def test_the_inward_corner_gains_what_an_outward_one_loses(self):
        """Five outward corners and one inward, all square, so the net is four
        triangles rather than six. The sign is the reading: a chamfer laid
        across a notch the outward way would cut into the material instead of
        filling the notch, and the area is what says which happened."""
        assert area_of({"type": "polyline", "chamfers": 3, "closed": True,
                        "points": L_BRACKET}) == pytest.approx(
            (60 * 20 + 20 * 20 - 4 * 3 ** 2 / 2) / 100, rel=1e-12)

    def test_it_scales_with_the_square_of_the_distance(self):
        """Which is what says the two legs are both `d` rather than one of
        them being `d` and the other whatever the arithmetic left."""
        small = area_of({"type": "rectangle", "center": [0, 0], "width": 60,
                         "height": 40, "chamfers": 2})
        large = area_of({"type": "rectangle", "center": [0, 0], "width": 60,
                         "height": 40, "chamfers": 4})
        assert (24.0 - small) * 4 == pytest.approx(24.0 - large, rel=1e-12)


class TestItIsGeometryAndNotAFeature:
    def test_the_corner_is_a_line(self):
        plan = plan_of({"type": "rectangle", "center": [0, 0], "width": 60,
                        "height": 40, "chamfers": 5})
        assert [p for p in plan.primitives if isinstance(p, PArc)] == []
        drawn = [p for p in plan.primitives
                 if isinstance(p, PLine) and not p.construction]
        assert len(drawn) == 8  # four shortened edges and four chamfers

    def test_the_chamfer_lines_are_not_constrained_tangent(self):
        """A fillet is tangent to both edges, which is what makes its radius
        the only thing left to say. A chamfer is not tangent to anything, and
        asking Inventor for a tangency it cannot satisfy is a refused
        constraint and a loose sketch."""
        plan = plan_of({"type": "rectangle", "center": [0, 0], "width": 60,
                        "height": 40, "chamfers": 5})
        assert [c for c in plan.constraints if c.kind == "tangent"] == []

    def test_both_spans_are_the_recipe_expression_itself(self):
        """The point of restricting this to square corners: no cosine reaches
        the model, so the chamfer follows `chamfer_size` exactly."""
        plan = plan_sketch(
            SketchOp.model_validate({
                "op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 60,
                     "height": 40, "chamfers": "chamfer_size"}]}),
            Resolver("mm", "deg",
                     parameters={"chamfer_size": Quantity(0.5, Dim.LENGTH)}))
        spans = [d for d in plan.dimensions if d.expression == "chamfer_size"]
        assert len(spans) == 8, [d.expression for d in plan.dimensions]
        assert {d.kind for d in spans} == {"horizontal", "vertical"}
        assert all(d.value == pytest.approx(0.5) for d in spans)

    def test_it_builds_fully_constrained(self, session):
        """Two dimensions per chamfer and two sliding degrees of freedom per
        chamfer. One dimension each would leave the sketch loose, and Inventor
        does not complain about a loose sketch -- it just solves it somewhere."""
        out = build_part(session, PartRecipe.model_validate(
            {"name": "C", "units": "mm", "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "polyline", "chamfers": 3, "closed": True,
                     "points": L_BRACKET}]},
                {"op": "extrude", "name": "E", "sketch": "S", "distance": 10}]}))
        assert out["ok"], out["errors"]
        sketch = session.backend.list_sketches(out["document"])[-1]
        assert sketch.fully_constrained is True
        assert sketch.degrees_of_freedom == 0

    def test_the_size_reaches_the_model_as_a_parameter(self, session):
        """The whole thesis, on this operation: change the parameter and the
        chamfers change. A number that had been resolved and thrown away would
        build the right part once and never move."""
        recipe = PartRecipe.model_validate(
            {"name": "C", "units": "mm",
             "parameters": [{"name": "chamfer_size", "value": 5}],
             "operations": [
                 {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                     {"type": "rectangle", "center": [0, 0], "width": 60,
                      "height": 40, "chamfers": "chamfer_size"}]},
                 {"op": "extrude", "name": "E", "sketch": "S", "distance": 10}]})
        out = build_part(session, recipe)
        assert out["ok"], out["errors"]
        first = session.backend.mass_properties(out["document"]).volume
        assert first == pytest.approx((60 * 40 - 2 * 5 ** 2) * 10 / 1000, rel=1e-9)


class TestWhatItRefuses:
    def test_an_oblique_corner(self):
        """The chevron's notch is oblique, and so are two of its outward
        corners. Refused by name, with what a chamfer there would need."""
        from inventor_mcp.errors import SketchError

        with pytest.raises(SketchError, match="oblique corner"):
            area_of({"type": "polyline", "chamfers": 3, "closed": True,
                     "points": [[0, 0], [60, 0], [60, 40], [30, 20], [0, 40]]})

    def test_a_distance_that_does_not_fit(self):
        from inventor_mcp.errors import SketchError

        with pytest.raises(SketchError, match="does not fit the corner"):
            area_of({"type": "rectangle", "center": [0, 0], "width": 10,
                     "height": 10, "chamfers": 8})

    def test_both_a_radius_and_a_chamfer(self):
        """A corner is rounded or cut off. Offering both and silently
        preferring one is how somebody learns the wrong lesson about their own
        recipe -- the same reason `axis` is refused on a plane that does not
        turn about one."""
        import pydantic

        for entity in ({"type": "rectangle", "center": [0, 0], "width": 60,
                        "height": 40, "corners": 5, "chamfers": 5},
                       {"type": "polyline", "closed": True, "corners": 3,
                        "chamfers": 3, "points": L_BRACKET}):
            with pytest.raises(pydantic.ValidationError, match="not both"):
                SketchOp.model_validate(
                    {"op": "sketch", "name": "S", "plane": "xy",
                     "entities": [entity]})

    def test_a_negative_or_zero_distance(self):
        from inventor_mcp.errors import ExpressionError, SketchError

        with pytest.raises((SketchError, ExpressionError)):
            area_of({"type": "rectangle", "center": [0, 0], "width": 60,
                     "height": 40, "chamfers": 0})


def test_a_chamfered_and_a_rounded_rectangle_differ_by_the_arc(session):
    """Both take material off the same four corners, and the chamfer takes
    more: `2 * d^2` against `(4 - pi) * r^2`, which is 2 against 0.858 for the
    same size. Worth pinning, because a chamfer that came out equal to a
    fillet would mean one of the two was building the other."""
    def volume(entity: dict) -> float:
        out = build_part(session, PartRecipe.model_validate(
            {"name": "P", "units": "mm", "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [entity]},
                {"op": "extrude", "name": "E", "sketch": "S", "distance": 10}]}))
        assert out["ok"], out["errors"]
        return session.backend.mass_properties(out["document"]).volume

    base = {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}
    cut = volume({**base, "chamfers": 5})
    rounded = volume({**base, "corners": 5})
    assert 24.0 - cut == pytest.approx(2 * 0.5 ** 2 * 1.0, rel=1e-9)
    # A wider tolerance than the fillet's own tests use, and for a reason
    # worth stating: this compares the *difference* of two volumes, so the
    # arc's sampling error lands on a quantity a hundredth the size of the
    # part and is amplified with it.
    assert 24.0 - rounded == pytest.approx(
        (4 - math.pi) * 0.5 ** 2 * 1.0, rel=1e-2)
    assert cut < rounded
