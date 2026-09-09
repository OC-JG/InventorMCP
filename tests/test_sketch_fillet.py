"""`corners` on a rectangle or a polyline: the profile rounded, not the solid.

The oldest bullet in `docs/FEATURE_COVERAGE.md`'s gap list. A `fillet` rounds
the *edges of a solid* after the fact; this rounds the **profile**, so the
rounding is part of the shape being swept and survives whatever the profile is
used for -- a sweep, a loft's section, a cut.

It is written as geometry rather than through Inventor's own
`SketchArcs.AddByFillet`, which the roadmap named as context and which is
published and unmeasured. A line-and-arc outline with tangencies is what
`_plan_slot` already builds and what a seat has built since the slot shipped,
and writing it out means the simulator gets the real outline: a rounded
rectangle's area is `w * h - (4 - pi) * r^2` by its own arithmetic rather than
by a special case. `docs/DECISIONS.md` is where that preference is written
down.

The arithmetic below is checked against the closed form, because the one bug
this had was in the arithmetic and it was invisible: an arc whose sweep crossed
`atan2`'s seam at pi came out going the long way round its own centre, and the
profile area was 22.99991 cm^2 against the 23.785398 the shape has -- the
difference being exactly the four quarter-discs it had carved out of the
corners instead of rounding them. Nothing raised; the sketch closed; the loop
walked. Only the number said so.
"""

from __future__ import annotations

import math

import pytest

from inventor_mcp.geometry import loop_points, plan_sketch, profile_loops
from inventor_mcp.plan import PArc, PLine
from inventor_mcp.resolve import Resolver
from inventor_mcp.schema import SketchOp

#: The sampled outline is a polygon through points on each arc, so it comes in
#: a hair under the closed form. A tenth of a percent is far tighter than the
#: 22.99991-against-23.785398 the seam bug produced, and far looser than the
#: sampling.
SAMPLING = 1e-3


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


class TestARoundedRectangleHasTheAreaItShould:
    @pytest.mark.parametrize("width, height, radius", [
        (60, 40, 5), (60, 40, 10), (20, 20, 2), (100, 8, 4),
    ])
    def test_against_the_closed_form(self, width, height, radius):
        """`w * h - (4 - pi) * r^2`: the four corner squares, less the quarter
        discs that replace them."""
        got = area_of({"type": "rectangle", "center": [0, 0], "width": width,
                       "height": height, "corners": radius})
        exact = (width / 10) * (height / 10) - (4 - math.pi) * (radius / 10) ** 2
        assert got == pytest.approx(exact, rel=SAMPLING)

    def test_the_radius_at_its_largest_is_a_stadium(self, width=40, height=20):
        """A radius of half the short side rounds the two ends away entirely,
        which is a slot's shape -- `2 * r * (w - 2r) + pi * r^2`."""
        got = area_of({"type": "rectangle", "center": [0, 0], "width": width,
                       "height": height, "corners": height / 2})
        r = height / 20
        exact = 2 * r * (width / 10 - 2 * r) + math.pi * r ** 2
        assert got == pytest.approx(exact, rel=SAMPLING)

    def test_a_square_rectangle_is_unchanged(self):
        """The unrounded path is untouched, which matters more than the new one:
        every shipped example goes through it."""
        assert area_of({"type": "rectangle", "center": [0, 0],
                        "width": 60, "height": 40}) == pytest.approx(24.0)


class TestTheGeometryIsLinesAndTangentArcs:
    def test_four_lines_and_four_arcs(self):
        plan = plan_of({"type": "rectangle", "center": [0, 0], "width": 60,
                        "height": 40, "corners": 5})
        arcs = [p for p in plan.primitives if isinstance(p, PArc)]
        lines = [p for p in plan.primitives
                 if isinstance(p, PLine) and not p.construction]
        assert len(arcs) == 4 and len(lines) == 4

    def test_every_arc_sweeps_the_short_way(self):
        """The seam bug in one assertion: a rounded corner's sweep is its turn,
        which for a rectangle is 90 degrees. An arc that came back at 270 was
        going round the outside of its own centre."""
        plan = plan_of({"type": "rectangle", "center": [0, 0], "width": 60,
                        "height": 40, "corners": 5})
        for arc in (p for p in plan.primitives if isinstance(p, PArc)):
            sweep = arc.end_angle - arc.start_angle
            assert sweep == pytest.approx(math.pi / 2), arc.id

    def test_each_arc_is_tangent_to_both_its_lines(self):
        plan = plan_of({"type": "rectangle", "center": [0, 0], "width": 60,
                        "height": 40, "corners": 5})
        tangents = [c for c in plan.constraints if c.kind == "tangent"]
        assert len(tangents) == 8, "four corners, two lines each"

    def test_one_radius_dimension_carried_by_equal_radius(self):
        """How a drafter writes it, and what keeps the sketch from being
        over-dimensioned: Inventor refuses a redundant dimension readily, as
        the hexagon's closing equality shows."""
        plan = plan_of({"type": "rectangle", "center": [0, 0], "width": 60,
                        "height": 40, "corners": 5})
        radii = [d for d in plan.dimensions if d.kind == "radius"]
        equals = [c for c in plan.constraints if c.kind == "equal_radius"]
        assert len(radii) == 1 and len(equals) == 3

    def test_the_edges_stay_square_to_the_axes(self):
        """A rounded rectangle's straight parts are still a rectangle's."""
        plan = plan_of({"type": "rectangle", "center": [0, 0], "width": 60,
                        "height": 40, "corners": 5})
        kinds = [c.kind for c in plan.constraints]
        assert kinds.count("horizontal") == 2 and kinds.count("vertical") == 2

    def test_the_radius_keeps_its_expression(self):
        """Which is the point of putting it in the profile: the corners follow
        the parameter that sets them, so a part revised through `corner_r`
        rounds differently without the profile being rewritten."""
        from inventor_mcp.units import Dim, Quantity

        resolver = Resolver("mm", "deg")
        resolver.declare("corner_r", Quantity(0.5, Dim.LENGTH))
        plan = plan_sketch(
            SketchOp.model_validate({
                "op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 60,
                     "height": 40, "corners": "corner_r"}]}),
            resolver)
        radius = next(d for d in plan.dimensions if d.kind == "radius")
        assert radius.expression == "corner_r"
        assert radius.value == pytest.approx(0.5)


class TestAPolyline:
    def test_a_rounded_triangle_loses_area_at_every_corner(self):
        square = area_of({"type": "polyline",
                          "points": [[0, 0], [60, 0], [30, 40]], "closed": True})
        rounded = area_of({"type": "polyline", "corners": 3,
                           "points": [[0, 0], [60, 0], [30, 40]], "closed": True})
        assert square == pytest.approx(12.0)
        assert rounded < square

    def test_an_open_polyline_rounds_its_middle_and_not_its_ends(self):
        """There is no corner at an end, so there is nothing to round there."""
        plan = plan_of({"type": "polyline", "corners": 2, "closed": False,
                        "points": [[0, 0], [40, 0], [40, 30]]})
        arcs = [p for p in plan.primitives if isinstance(p, PArc)]
        assert len(arcs) == 1


class TestWhatItRefuses:
    def test_a_radius_that_does_not_fit(self):
        """Two fillets that overlap are not a shape. The message says which
        corner and by how much, in millimetres, because a radius in a recipe is
        in the recipe's units and the failure is arithmetic."""
        from inventor_mcp.errors import SketchError

        with pytest.raises(SketchError, match="does not fit the corner"):
            area_of({"type": "rectangle", "center": [0, 0], "width": 10,
                     "height": 10, "corners": 8})

    def test_an_inward_corner(self):
        """A notch is a different arc -- it sweeps the other way round its
        centre -- and rounding it the outward way would close a loop nobody
        asked for. Refused by name rather than rounded wrongly."""
        from inventor_mcp.errors import SketchError

        with pytest.raises(SketchError, match="turns inward"):
            area_of({"type": "polyline", "corners": 3, "closed": True,
                     "points": [[0, 0], [60, 0], [60, 40], [30, 20], [0, 40]]})

    def test_a_polyline_with_no_corner_at_all(self):
        from inventor_mcp.errors import SketchError

        with pytest.raises(SketchError, match="no corner to round"):
            area_of({"type": "polyline", "corners": 3, "closed": False,
                     "points": [[0, 0], [60, 0]]})
