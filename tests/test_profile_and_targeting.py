"""Closed profiles that mix arcs with lines, variable fillets, body targeting.

Every expected volume here is what Inventor actually produced for the same
recipe, so a change that drifts away from the real thing fails here instead of
in a part someone has already sent to a factory.
"""

from __future__ import annotations

import math

import pytest

from inventor_mcp.builder import build_part
from inventor_mcp.geometry import _stitch_endpoints, plan_sketch, profile_loops
from inventor_mcp.resolve import Resolver
from inventor_mcp.schema import PartRecipe, SketchOp

#: A stadium drawn as four separate entities: 20 between the arc centres,
#: radius 5, so 20 x 10 of straight plus a circle's worth of ends.
STADIUM = [
    {"type": "line", "start": [-10, 5], "end": [10, 5]},
    {"type": "arc", "center": [10, 0], "radius": 5, "start_angle": 90, "end_angle": -90},
    {"type": "line", "start": [10, -5], "end": [-10, -5]},
    {"type": "arc", "center": [-10, 0], "radius": 5, "start_angle": -90, "end_angle": -270},
]


def sketch(entities):
    return plan_sketch(
        SketchOp.model_validate(
            {"op": "sketch", "name": "P", "plane": "xy", "entities": entities}),
        Resolver("mm", "deg"),
    )


def build(session, ops, **kwargs):
    recipe = PartRecipe.model_validate({"name": "T", "units": "mm", "operations": ops})
    return build_part(session, recipe, **kwargs)


def volume(out):
    return out["operations"][-1]["measured"]["volume_cm3"]


class TestStitchedProfiles:
    """Separate line and arc entities have to be joined into one loop.

    Inventor infers nothing from coordinates alone here: without a coincidence
    on each pair of touching ends it sees four loose curves, offers no profile,
    and the extrude fails with a bare "Exception occurred."
    """

    def test_touching_ends_are_made_coincident(self):
        plan = sketch(STADIUM)
        coincident = [c for c in plan.constraints if c.kind == "coincident"]
        assert len(coincident) == 4
        # One point per corner, not two points constrained together.
        assert len(set(plan.shared_point_groups().values())) == 4

    def test_the_pieces_become_one_closed_loop(self):
        assert profile_loops(sketch(STADIUM)) == [["line1", "arc1", "line2", "arc2"]]

    def test_a_real_gap_is_left_alone(self):
        """Stitching records a coincidence the recipe drew; it never closes a gap."""
        gapped = list(STADIUM)
        gapped[0] = {"type": "line", "start": [-10, 5], "end": [9, 5]}
        plan = sketch(gapped)
        assert len([c for c in plan.constraints if c.kind == "coincident"]) == 3
        assert profile_loops(plan) == []

    def test_a_polyline_is_not_stitched_twice(self):
        """Its own planner already constrains its corners, so the stitch has
        nothing left to join and adds no second constraint on the same pair."""
        plan = sketch([{"type": "polyline", "closed": True,
                        "points": [[0, 0], [10, 0], [10, 10], [0, 10]]}])
        corners = [c for c in plan.constraints if c.kind == "coincident"
                   and not any(str(ref) == "__origin__" for ref in c.refs)]
        assert len(corners) == 4
        before = len(plan.constraints)
        assert _stitch_endpoints(plan) == set()
        assert len(plan.constraints) == before

    def test_a_stitched_entitys_own_dimensions_can_yield(self):
        """Joining the loop spends degrees of freedom the planners already spent.

        Inventor takes a redundant dimension without complaint and the sketch is
        then unusable, so these have to be marked as able to give way.
        """
        assert all(d.optional for d in sketch(STADIUM).dimensions)

    def test_an_unstitched_entity_keeps_its_dimensions(self):
        """A rectangle's width is what the author asked for, not a spare."""
        plan = sketch([{"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}])
        assert plan.dimensions and not any(d.optional for d in plan.dimensions)

    def test_a_lone_arc_keeps_its_radius_dimension(self):
        """Nothing was joined, so no degree of freedom was taken away."""
        plan = sketch([{"type": "arc", "center": [20, 0], "radius": 5,
                        "start_angle": 0, "end_angle": 180}])
        assert plan.dimensions and not any(d.optional for d in plan.dimensions)

    def test_it_extrudes_to_the_right_volume(self, session):
        """Inventor built 1.114159 cm^3 from this recipe."""
        out = build(session, [
            {"op": "sketch", "name": "P", "plane": "xy", "entities": STADIUM},
            {"op": "extrude", "name": "E", "sketch": "P", "distance": 4}])
        assert out["ok"] is True
        assert volume(out) == pytest.approx((20 * 10 + math.pi * 25) * 4 / 1000, abs=0.002)


BLOCK = [
    {"op": "sketch", "name": "Body", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}]},
    {"op": "extrude", "name": "Block", "sketch": "Body", "distance": 10},
]
VERTICALS = {"kind": "edge", "filter": "vertical"}


class TestVariableFillet:
    def test_it_removes_the_cubic_transitions_worth(self, session):
        """Inventor built 7.714333 cm^3: a 40x20x10 block with its four vertical
        edges filleted from 3 to 8.  The radius moves on a smooth cubic, so the
        mean of r^2 along each edge is a^2 + a(b-a) + 13/35 (b-a)^2, which is
        0.071432 cm^3 an edge against the 0.069388 a straight ramp would give.
        """
        out = build(session, BLOCK + [
            {"op": "fillet", "name": "F", "edges": VERTICALS,
             "radius": 3, "radius_end": 8}])
        assert out["ok"] is True
        assert volume(out) == pytest.approx(7.714333, abs=0.002)

    def test_equal_radii_match_a_constant_fillet(self, session):
        """The cubic law has to reduce to r^2 when the two radii are the same."""
        varying = build(session, BLOCK + [
            {"op": "fillet", "edges": VERTICALS, "radius": 3, "radius_end": 3}])
        constant = build(session, BLOCK + [
            {"op": "fillet", "edges": VERTICALS, "radius": 3}])
        assert volume(varying) == pytest.approx(volume(constant), abs=1e-9)

    def test_the_end_radius_reaches_the_feature(self, session):
        out = build(session, BLOCK + [
            {"op": "fillet", "name": "F", "edges": VERTICALS,
             "radius": 3, "radius_end": 8}])
        detail = out["operations"][-1]["detail"]
        assert detail["radius_end"]["value"] == pytest.approx(0.8)

    def test_a_constant_fillet_says_nothing_about_an_end_radius(self, session):
        out = build(session, BLOCK + [
            {"op": "fillet", "edges": VERTICALS, "radius": 3}])
        assert "radius_end" not in out["operations"][-1]["detail"]


TWO_BODIES = [
    {"op": "sketch", "name": "A", "plane": "xy", "entities": [
        {"type": "rectangle", "corner": [0, 0], "width": 20, "height": 20}]},
    {"op": "extrude", "name": "BodyA", "sketch": "A", "distance": 10},
    {"op": "sketch", "name": "B", "plane": "xy", "entities": [
        {"type": "rectangle", "corner": [40, 0], "width": 20, "height": 20}]},
    {"op": "extrude", "name": "BodyB", "sketch": "B", "distance": 10,
     "operation": "new_body"},
    {"op": "sketch", "name": "Cut", "plane": "xy", "entities": [
        {"type": "circle", "center": [50, 10], "diameter": 8}]},
]
BORE = {"op": "extrude", "name": "CutB", "sketch": "Cut", "operation": "cut",
        "extent": "through_all"}


class TestBodyTargeting:
    def test_a_cut_can_be_aimed_at_the_second_body(self, session):
        """Inventor built 7.497345 cm^3: two 20x20x10 blocks with an 8 bore
        through the second.  Without `bodies` the cut lands on the first body,
        where this profile is over thin air and removes nothing.
        """
        out = build(session, TWO_BODIES + [dict(BORE, bodies=[2])])
        assert out["ok"] is True
        expected = (2 * 20 * 20 * 10 - math.pi * 16 * 10) / 1000
        assert volume(out) == pytest.approx(expected, abs=0.002)
        assert out["operations"][-1]["detail"]["bodies"] == [2]

    def test_a_body_that_does_not_exist_is_refused(self, session):
        out = build(session, TWO_BODIES + [dict(BORE, bodies=[3])], stop_on_error=True)
        assert out["ok"] is False
        assert "no body 3" in out["errors"][0]["error"]

    def test_no_bodies_given_leaves_inventors_default(self, session):
        out = build(session, TWO_BODIES + [dict(BORE)], stop_on_error=True)
        assert out["operations"][-1]["detail"]["bodies"] is None
