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

    def test_a_polyline_with_no_corner_at_all(self):
        from inventor_mcp.errors import SketchError

        with pytest.raises(SketchError, match="no corner to ease"):
            area_of({"type": "polyline", "corners": 3, "closed": False,
                     "points": [[0, 0], [60, 0]]})

    def test_a_straight_or_doubled_back_corner(self):
        """Three collinear points have no corner between them to round, and a
        segment that doubles back on itself has no side for the arc to sit on.
        Both come out of the same cross product being zero."""
        from inventor_mcp.errors import SketchError

        with pytest.raises(SketchError, match="straight or doubled-back"):
            area_of({"type": "polyline", "corners": 3, "closed": False,
                     "points": [[0, 0], [30, 0], [60, 0], [60, 40]]})


def rounding_change(points: list[list[float]], radius: float) -> float:
    """What rounding every corner of *points* by *radius* does to its area.

    The closed form, in cm^2 to match the planner's own units. Rounding a
    corner that turns by `phi` changes the area by
    `-sign(phi) * r^2 * (tan(|phi|/2) - |phi|/2)` -- negative at an outward
    corner, which loses the sliver outside the arc, and **positive at an
    inward one**, which gains it. A right angle gives the familiar
    `(1 - pi/4) r^2` per corner, and four of them the `(4 - pi) r^2` a rounded
    rectangle loses.
    """
    corners = [(x / 10, y / 10) for x, y in points]
    count = len(corners)
    change = 0.0
    for index in range(count):
        before, vertex, after = (corners[index - 1], corners[index],
                                 corners[(index + 1) % count])
        incoming = (vertex[0] - before[0], vertex[1] - before[1])
        outgoing = (after[0] - vertex[0], after[1] - vertex[1])
        first = math.hypot(*incoming)
        second = math.hypot(*outgoing)
        incoming = (incoming[0] / first, incoming[1] / first)
        outgoing = (outgoing[0] / second, outgoing[1] / second)
        turn = math.atan2(incoming[0] * outgoing[1] - incoming[1] * outgoing[0],
                          incoming[0] * outgoing[0] + incoming[1] * outgoing[1])
        change -= math.copysign(1.0, turn) * (radius / 10) ** 2 * (
            math.tan(abs(turn) / 2) - abs(turn) / 2)
    return change


def sharp_area(points: list[list[float]]) -> float:
    corners = [(x / 10, y / 10) for x, y in points]
    count = len(corners)
    return 0.5 * abs(sum(
        corners[index][0] * corners[(index + 1) % count][1]
        - corners[(index + 1) % count][0] * corners[index][1]
        for index in range(count)))


class TestAnInwardCornerIsRoundedToo:
    """The roadmap item this was split into on 2026-09-09, closed the same day.

    A notch turns the other way, so its arc sweeps *clockwise* from the
    incoming edge to the outgoing one -- and every arc in this planner sweeps
    anticlockwise, as Inventor's `AddByCenterStartEndPoint` does. Rounding it
    the outward way would have closed a loop nobody asked for, so it was
    refused for a day; the fix is to emit the arc from the outgoing tangent
    point back to the incoming one, with the two coincidences swapping ends.

    The check the item said was needed first: **both loop walkers are
    direction-agnostic.** `profile_loops` matches a segment on either endpoint
    and `loop_points` reverses whichever segment starts further from the
    cursor, so a reversed arc walks the same as any other. A walker that had
    trusted the stored direction would have produced a self-crossing polygon
    and a nonsense area, silently.
    """

    #: An L-bracket, anticlockwise, with the notch at [20, 20]. The common case
    #: the roadmap named for wanting this.
    L_BRACKET = [[0, 0], [60, 0], [60, 20], [20, 20], [20, 40], [0, 40]]

    #: A chevron: the inward corner is oblique, and so are two of the outward
    #: ones, which is what says the arithmetic is general rather than
    #: right-angle-shaped.
    CHEVRON = [[0, 0], [60, 0], [60, 40], [30, 20], [0, 40]]

    @pytest.mark.parametrize("outline", ["L_BRACKET", "CHEVRON"])
    def test_the_area_matches_the_closed_form(self, outline):
        points = getattr(self, outline)
        want = sharp_area(points) + rounding_change(points, 3)
        assert area_of({"type": "polyline", "corners": 3, "closed": True,
                        "points": points}) == pytest.approx(want, rel=SAMPLING)

    def test_the_notch_gains_area_where_a_corner_loses_it(self):
        """The sign is the whole reading: rounding an outward corner cuts a
        sliver off, and rounding an inward one fills one in. An arc placed on
        the wrong side of a notch would take area away instead, and the
        closed-form check above would then be the only thing to notice."""
        assert rounding_change(self.L_BRACKET, 3) == pytest.approx(
            -4 * (1 - math.pi / 4) * 0.09, rel=1e-12), \
            "five outward corners and one inward, all square: the inward one " \
            "cancels one of the five"

    def test_the_loop_still_closes_and_is_one_profile(self):
        """Which is what a reversed arc could have broken."""
        plan = plan_of({"type": "polyline", "corners": 3, "closed": True,
                        "points": self.L_BRACKET})
        loops = profile_loops(plan)
        assert len(loops) == 1
        assert len(loops[0]) == 12  # six edges and six arcs

    def test_one_arc_is_emitted_the_other_way_round(self):
        """Not an implementation detail: it is the fix. Every other arc's
        `start_angle` is below its `end_angle` because the outline is walked
        anticlockwise; the notch's is the one whose ends are swapped so that
        its own sweep stays anticlockwise."""
        plan = plan_of({"type": "polyline", "corners": 3, "closed": True,
                        "points": self.L_BRACKET})
        arcs = [p for p in plan.primitives if isinstance(p, PArc)]
        assert len(arcs) == 6
        sweeps = [arc.end_angle - arc.start_angle for arc in arcs]
        assert all(sweep > 0 for sweep in sweeps), sweeps
        assert sum(1 for sweep in sweeps
                   if abs(sweep - math.pi / 2) < 1e-9) == 6, sweeps

    def test_it_builds_fully_constrained(self, session):
        """A reversed arc is coincident with its neighbours at the other end,
        and getting that wrong leaves the sketch loose rather than refusing."""
        from inventor_mcp.builder import build_part
        from inventor_mcp.schema import PartRecipe

        out = build_part(session, PartRecipe.model_validate(
            {"name": "L", "units": "mm", "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "polyline", "corners": 3, "closed": True,
                     "points": self.L_BRACKET}]},
                {"op": "extrude", "name": "E", "sketch": "S", "distance": 10}]}))
        assert out["ok"], out["errors"]
        sketch = session.backend.list_sketches(out["document"])[-1]
        assert sketch.fully_constrained is True
        assert sketch.degrees_of_freedom == 0
