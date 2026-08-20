from __future__ import annotations

import math

import pytest

from inventor_mcp.errors import SketchError
from inventor_mcp.geometry import loop_area, plan_bounds, plan_sketch, profile_loops
from inventor_mcp.plan import ORIGIN, PArc, PCircle, PLine, PPoint
from inventor_mcp.schema import SketchOp


def build(entities, **kwargs):
    from inventor_mcp.resolve import Resolver

    spec = SketchOp.model_validate({"op": "sketch", "plane": "xy", "entities": entities, **kwargs})
    return plan_sketch(spec, Resolver("mm", "deg"))


def kinds(plan):
    return [type(p).__name__ for p in plan.primitives]


class TestRectangle:
    def test_four_lines_and_a_closed_loop(self):
        plan = build([{"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}])
        lines = [p for p in plan.primitives if isinstance(p, PLine) and not p.construction]
        assert len(lines) == 4
        loops = profile_loops(plan)
        assert len(loops) == 1
        assert loop_area(plan, loops[0]) == pytest.approx(4.0 * 2.0)

    def test_it_is_constrained_as_a_rectangle(self):
        plan = build([{"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}])
        counts = {}
        for constraint in plan.constraints:
            counts[constraint.kind] = counts.get(constraint.kind, 0) + 1
        assert counts["coincident"] == 7  # four corners, the diagonal's ends, the centre
        assert counts["horizontal"] == 2
        assert counts["vertical"] == 2
        assert counts["midpoint"] == 1  # the diagonal's midpoint pins the centre point

    def test_dimensions_carry_the_expression_not_the_number(self):
        from inventor_mcp.resolve import Resolver
        from inventor_mcp.units import to_internal

        resolver = Resolver("mm", "deg", {"w": to_internal(40, "mm")})
        spec = SketchOp.model_validate({
            "op": "sketch", "plane": "xy",
            "entities": [{"type": "rectangle", "center": [0, 0], "width": "w", "height": "w / 2"}],
        })
        plan = plan_sketch(spec, resolver)
        expressions = {d.expression for d in plan.dimensions}
        assert expressions == {"w", "w / 2"}
        assert next(d for d in plan.dimensions if d.expression == "w / 2").value == pytest.approx(2.0)

    def test_an_undefined_parameter_fails_loudly(self):
        with pytest.raises(Exception, match="Unknown parameter"):
            build([{"type": "rectangle", "center": [0, 0], "width": "nope", "height": 10}])

    def test_corner_anchor_positions_the_geometry(self):
        plan = build([{"type": "rectangle", "corner": [0, 0], "width": 40, "height": 20}])
        assert plan_bounds(plan) == pytest.approx((0.0, 0.0, 4.0, 2.0))

    def test_the_centre_point_is_the_thing_constrained_not_the_origin(self):
        """A midpoint constraint moves the point onto the line.

        The sketch origin is grounded and cannot move, so it must never be the
        point in a midpoint constraint -- Inventor rejects that outright.
        """
        plan = build([{"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}])
        midpoints = [c for c in plan.constraints if c.kind == "midpoint"]
        assert len(midpoints) == 1
        assert midpoints[0].refs[0] != ORIGIN
        assert any(
            c.kind == "coincident" and ORIGIN in c.refs for c in plan.constraints
        )

    def test_centre_away_from_the_origin_is_dimensioned(self):
        plan = build([{"type": "rectangle", "center": [30, 0], "width": 40, "height": 20}])
        horizontal = [d for d in plan.dimensions if d.kind == "horizontal"]
        assert any(d.value == pytest.approx(3.0) for d in horizontal)

    def test_exactly_one_anchor_is_required(self):
        with pytest.raises(Exception):
            build([{"type": "rectangle", "center": [0, 0], "corner": [0, 0],
                    "width": 10, "height": 10}])


class TestCircleAndArc:
    def test_diameter_becomes_a_diameter_dimension(self):
        plan = build([{"type": "circle", "center": [0, 0], "diameter": 20}])
        circle = next(p for p in plan.primitives if isinstance(p, PCircle))
        assert circle.radius == pytest.approx(1.0)
        dimension = next(d for d in plan.dimensions if d.kind == "diameter")
        assert dimension.expression == "20 mm"

    def test_radius_is_also_accepted(self):
        plan = build([{"type": "circle", "radius": 10}])
        assert next(d for d in plan.dimensions if d.kind == "radius").value == pytest.approx(1.0)

    def test_circle_needs_exactly_one_size(self):
        with pytest.raises(Exception):
            build([{"type": "circle", "diameter": 10, "radius": 5}])

    def test_circle_at_the_origin_is_coincident_not_dimensioned(self):
        plan = build([{"type": "circle", "diameter": 20}])
        assert any(c.kind == "coincident" for c in plan.constraints)
        assert not [d for d in plan.dimensions if d.kind in ("horizontal", "vertical")]

    def test_arc_sweep_must_be_non_zero(self):
        with pytest.raises(SketchError, match="non-zero sweep"):
            build([{"type": "arc", "radius": 10, "start_angle": 30, "end_angle": 30}])

    def test_arc_area_approximation(self):
        plan = build([
            {"type": "arc", "center": [0, 0], "radius": 10, "start_angle": 0, "end_angle": 180},
            {"type": "line", "start": [-10, 0], "end": [10, 0]},
        ])
        loops = profile_loops(plan)
        assert len(loops) == 1
        assert loop_area(plan, loops[0]) == pytest.approx(math.pi * 1.0**2 / 2, rel=0.01)


class TestSlot:
    def test_geometry_and_tangency(self):
        plan = build([{"type": "slot", "center": [0, 0], "length": 30, "width": 8}])
        arcs = [p for p in plan.primitives if isinstance(p, PArc)]
        lines = [p for p in plan.primitives if isinstance(p, PLine) and not p.construction]
        assert len(arcs) == 2 and len(lines) == 2
        assert sum(1 for c in plan.constraints if c.kind == "tangent") == 4
        # The end arcs match on radius, which is a different constraint to length.
        assert any(c.kind == "equal_radius" for c in plan.constraints)
        assert not any(c.kind == "equal_length" for c in plan.constraints)

    def test_the_origin_is_never_the_moved_point(self):
        plan = build([{"type": "slot", "center": [0, 0], "length": 30, "width": 8}])
        for constraint in plan.constraints:
            if constraint.kind == "midpoint":
                assert constraint.refs[0] != ORIGIN

    def test_it_closes_into_one_profile(self):
        plan = build([{"type": "slot", "center": [0, 0], "length": 30, "width": 8}])
        loops = profile_loops(plan)
        assert len(loops) == 1
        # 30 x 8 slot: a 30 x 8 rectangle plus a circle of diameter 8.
        expected = 3.0 * 0.8 + math.pi * 0.4**2
        assert loop_area(plan, loops[0]) == pytest.approx(expected, rel=0.01)


class TestPolygon:
    def test_inscribed_hexagon_across_corners(self):
        plan = build([{"type": "polygon", "sides": 6, "size": 20, "fit": "inscribed"}])
        lines = [p for p in plan.primitives if isinstance(p, PLine)]
        assert len(lines) == 6
        assert plan_bounds(plan)[2] == pytest.approx(1.0)  # across corners = 20 mm

    def test_circumscribed_hexagon_across_flats(self):
        plan = build([{"type": "polygon", "sides": 6, "size": 20, "fit": "circumscribed"}])
        vertex_radius = 1.0 / math.cos(math.pi / 6)
        assert plan_bounds(plan)[2] == pytest.approx(vertex_radius)

    def test_equal_constraints_leave_only_rotation_free(self):
        plan = build([{"type": "polygon", "sides": 6, "size": 20}])
        equal = [c for c in plan.constraints if c.kind == "equal_length"]
        assert len(equal) == 5  # sides - 1

    def test_edges_are_measured_against_the_first_not_chained(self):
        """Chaining round the loop puts a constraint on the closing pair.

        Inventor's redundancy detection objects to that one; measuring every
        edge against the first is the same count and avoids it.
        """
        plan = build([{"type": "polygon", "sides": 6, "size": 20}])
        equal = [c for c in plan.constraints if c.kind == "equal_length"]
        assert len(equal) == 5
        assert {c.refs[0].entity for c in equal} == {"line1"}
        assert {c.refs[1].entity for c in equal} == {"line2", "line3", "line4", "line5", "line6"}

    def test_edges_are_equal_by_length_not_radius(self):
        """Inventor has no single 'equal': lines match on length, curves on radius."""
        plan = build([{"type": "polygon", "sides": 5, "size": 20}])
        kinds = {c.kind for c in plan.constraints}
        assert "equal_length" in kinds
        assert "equal_radius" not in kinds
        assert "equal" not in kinds

    def test_polygon_closes(self):
        plan = build([{"type": "polygon", "sides": 5, "size": 20}])
        assert len(profile_loops(plan)) == 1


class TestPoints:
    def test_grid_produces_hole_centres(self):
        plan = build([{"type": "point_grid", "columns": 3, "rows": 2,
                       "x_spacing": 20, "y_spacing": 10}])
        points = [p for p in plan.primitives if isinstance(p, PPoint)]
        assert len(points) == 6
        assert len(plan.hole_centers) == 6
        assert plan_bounds(plan) == pytest.approx((-2.0, -0.5, 2.0, 0.5))

    def test_grid_spacing_is_dimensioned_from_the_expression(self):
        plan = build([{"type": "point_grid", "columns": 2, "rows": 2,
                       "x_spacing": "40 mm", "y_spacing": "20 mm"}])
        expressions = {d.expression for d in plan.dimensions}
        assert "40 mm" in expressions and "20 mm" in expressions

    def test_bolt_circle_places_points_on_the_pitch_circle(self):
        plan = build([{"type": "bolt_circle", "diameter": 60, "count": 6}])
        points = [p for p in plan.primitives if isinstance(p, PPoint)]
        assert len(points) == 6
        for point in points:
            assert math.hypot(*point.position) == pytest.approx(3.0)
        assert len(plan.hole_centers) == 6

    def test_bolt_circle_angular_steps_are_dimensioned(self):
        plan = build([{"type": "bolt_circle", "diameter": 60, "count": 4}])
        angles = [d for d in plan.dimensions if d.kind == "angle"]
        assert len(angles) == 3
        assert all(d.value == pytest.approx(math.pi / 2) for d in angles)


class TestNamingAndProfiles:
    def test_named_entities_are_addressable(self):
        plan = build([{"type": "line", "start": [0, 0], "end": [50, 0], "name": "axis",
                       "construction": True}])
        assert "axis" in plan.labels
        assert isinstance(plan.resolve_label("axis")[0], PLine)

    def test_construction_geometry_is_not_a_profile(self):
        plan = build([{"type": "circle", "diameter": 20, "construction": True}])
        assert profile_loops(plan) == []

    def test_two_loops_are_reported_separately(self):
        plan = build([
            {"type": "rectangle", "center": [0, 0], "width": 40, "height": 20},
            {"type": "circle", "center": [0, 0], "diameter": 10},
        ])
        assert len(profile_loops(plan)) == 2

    def test_a_recipe_equal_resolves_by_entity_type(self):
        lines = build(
            [
                {"type": "line", "start": [0, 0], "end": [40, 0], "name": "a", "locate": "none"},
                {"type": "line", "start": [0, 10], "end": [40, 10], "name": "b", "locate": "none"},
            ],
            constraints=[{"type": "equal", "entities": ["a", "b"]}],
        )
        assert any(c.kind == "equal_length" for c in lines.constraints)

        circles = build(
            [
                {"type": "circle", "center": [0, 0], "diameter": 10, "name": "a", "locate": "none"},
                {"type": "circle", "center": [30, 0], "diameter": 10, "name": "b", "locate": "none"},
            ],
            constraints=[{"type": "equal", "entities": ["a", "b"]}],
        )
        assert any(c.kind == "equal_radius" for c in circles.constraints)

    def test_explicit_constraints_reference_names(self):
        plan = build(
            [
                {"type": "line", "start": [0, 0], "end": [40, 0], "name": "a", "locate": "none"},
                {"type": "line", "start": [40, 0], "end": [40, 30], "name": "b", "locate": "none"},
            ],
            constraints=[{"type": "perpendicular", "entities": ["a", "b"]}],
        )
        assert any(c.kind == "perpendicular" for c in plan.constraints)

    def test_unknown_name_in_a_constraint_is_reported(self):
        with pytest.raises(SketchError, match="No sketch entity named"):
            build(
                [{"type": "line", "start": [0, 0], "end": [40, 0], "name": "a"}],
                constraints=[{"type": "parallel", "entities": ["a", "ghost"]}],
            )

    def test_polyline_rejects_zero_length_segments(self):
        with pytest.raises(SketchError, match="zero-length"):
            build([{"type": "polyline", "points": [[0, 0], [0, 0], [10, 10]], "closed": False}])


class TestSharedPoints:
    """Coincident endpoints are built as one point, not two plus a constraint.

    Inventor infers coincidence from the coordinates as geometry is created and
    then rejects an explicit duplicate, so the backend needs to know which
    endpoints are meant to be the same point before it creates them.
    """

    def test_a_rectangle_has_four_corner_groups(self):
        plan = build([{"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}])
        groups = plan.shared_point_groups()
        # Eight line endpoints plus the diagonal's two, collapsing to four corners.
        assert len(set(groups.values())) == 4
        assert len(groups) == 10

    def test_each_corner_joins_the_lines_that_meet_there(self):
        plan = build([{"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}])
        groups = plan.shared_point_groups()
        assert groups[("line1", "end")] == groups[("line2", "start")]
        assert groups[("line4", "end")] == groups[("line1", "start")]

    def test_the_construction_diagonal_reuses_two_of_them(self):
        plan = build([{"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}])
        groups = plan.shared_point_groups()
        assert groups[("cline1", "start")] == groups[("line1", "start")]
        assert groups[("cline1", "end")] == groups[("line3", "start")]

    def test_the_origin_is_never_grouped(self):
        """The origin is a projected point; it cannot be shared as an endpoint."""
        plan = build([{"type": "rectangle", "center": [0, 0], "width": 40, "height": 20}])
        groups = plan.shared_point_groups()
        assert not any(entity == "__origin__" for entity, _ in groups)

    def test_a_slot_joins_its_lines_to_its_arcs(self):
        plan = build([{"type": "slot", "center": [0, 0], "length": 30, "width": 8}])
        groups = plan.shared_point_groups()
        assert len(set(groups.values())) == 4  # two per end arc

    def test_a_lone_circle_needs_no_groups(self):
        plan = build([{"type": "circle", "diameter": 20}])
        assert plan.shared_point_groups() == {}

    def test_a_polyline_chains_through_every_vertex(self):
        plan = build([{"type": "polyline", "closed": True,
                       "points": [[0, 0], [40, 0], [40, 20], [0, 20]]}])
        groups = plan.shared_point_groups()
        assert len(set(groups.values())) == 4


class TestPolylineDimensions:
    """An outline has to be driven by the expressions it was written with.

    A polyline used to emit constraints and no dimensions, so an L-section's
    base_len and upright_h were evaluated once and discarded: the profile could
    not be revised, which is the only thing a parametric model is for.

    The count has to be exact. A dimension per vertex repeats what the
    horizontal and vertical constraints already say, and Inventor refuses a
    redundant dimension -- which used to kill the whole sketch. So the rule
    counts what is genuinely free: a *rail* is a set of vertices that
    constraints already force to share a coordinate, and every rail after the
    first on each axis is one free coordinate and gets one dimension.
    """

    def plan_for(self, points, closed=True, locate="origin", parameters=None, **extra):
        from inventor_mcp.geometry import plan_sketch
        from inventor_mcp.resolve import Resolver
        from inventor_mcp.schema import SketchOp
        from inventor_mcp.units import to_internal

        resolver = Resolver("mm", "deg")
        for name, value in (parameters or {}).items():
            resolver.declare(name, to_internal(value, "mm"))
        entity = {"type": "polyline", "points": points, "closed": closed,
                  "locate": locate, **extra}
        return plan_sketch(
            SketchOp.model_validate({"op": "sketch", "name": "S", "plane": "xy",
                                     "entities": [entity]}), resolver)

    BRACKET = [[0, 0], ["base_len", 0], ["base_len", "thk"],
               ["thk", "thk"], ["thk", "upright_h"], [0, "upright_h"]]
    BRACKET_PARAMS = {"base_len": 90, "upright_h": 70, "thk": 6}

    def test_the_bracket_section_gets_four_driving_dimensions(self):
        plan = self.plan_for(self.BRACKET, parameters=self.BRACKET_PARAMS)
        assert len(plan.dimensions) == 4

    def test_each_one_carries_the_recipe_expression_verbatim(self):
        """Verbatim, because that is what makes a parameter edit work."""
        plan = self.plan_for(self.BRACKET, parameters=self.BRACKET_PARAMS)
        assert [(d.kind, d.expression) for d in plan.dimensions] == [
            ("horizontal", "base_len"),
            ("horizontal", "thk"),
            ("vertical", "thk"),
            ("vertical", "upright_h"),
        ]

    def test_the_values_are_right_too(self):
        plan = self.plan_for(self.BRACKET, parameters=self.BRACKET_PARAMS)
        assert [d.value for d in plan.dimensions] == pytest.approx([9.0, 0.6, 0.6, 7.0])

    def test_every_parameter_reaches_a_dimension(self):
        from inventor_mcp.expressions import referenced_parameters

        plan = self.plan_for(self.BRACKET, parameters=self.BRACKET_PARAMS)
        driven = set()
        for dimension in plan.dimensions:
            driven |= referenced_parameters(dimension.expression)
        assert driven == {"base_len", "upright_h", "thk"}

    def test_thk_drives_two_dimensions_because_an_l_section_means_that(self):
        plan = self.plan_for(self.BRACKET, parameters=self.BRACKET_PARAMS)
        assert [d.expression for d in plan.dimensions].count("thk") == 2

    def test_they_are_measured_along_a_single_lines_own_axis(self):
        """The shape _plan_rectangle already ships, which Inventor accepts."""
        plan = self.plan_for(self.BRACKET, parameters=self.BRACKET_PARAMS)
        for dimension in plan.dimensions:
            entities = {ref.entity for ref in dimension.refs}
            assert len(entities) == 1, f"{dimension.expression} spans two entities"

    def test_they_are_all_optional(self):
        """So Inventor refusing one leaves the sketch, not kills it."""
        plan = self.plan_for(self.BRACKET, parameters=self.BRACKET_PARAMS)
        assert all(d.optional for d in plan.dimensions)

    def test_the_recipes_own_dimensions_are_not_optional(self):
        from inventor_mcp.geometry import plan_sketch
        from inventor_mcp.resolve import Resolver
        from inventor_mcp.schema import SketchOp

        plan = plan_sketch(SketchOp.model_validate({
            "op": "sketch", "name": "S", "plane": "xy",
            "entities": [{"type": "rectangle", "center": [0, 0],
                          "width": 40, "height": 20}]}), Resolver("mm", "deg"))
        assert plan.dimensions and not any(d.optional for d in plan.dimensions)

    @pytest.mark.parametrize("points,closed,expected", [
        # A rectangle: 2 X rails, 2 Y rails -> 1 + 1.
        ([[0, 0], [40, 0], [40, 20], [0, 20]], True, 2),
        # The L: 3 X rails, 3 Y rails -> 2 + 2.
        ([[0, 0], [90, 0], [90, 6], [6, 6], [6, 70], [0, 70]], True, 4),
        # An open three-point L: 2 X rails, 2 Y rails.
        ([[0, 0], [40, 0], [40, 20]], False, 2),
        # A C-channel: 4 X rails, 4 Y rails -> 3 + 3.
        ([[0, 0], [40, 0], [40, 10], [10, 10], [10, 30], [40, 30],
          [40, 40], [0, 40]], True, 6),
        # A right triangle: the hypotenuse is oblique so it frees nothing.
        ([[0, 0], [40, 0], [0, 20]], True, 2),
    ])
    def test_the_count_matches_the_free_coordinates(self, points, closed, expected):
        plan = self.plan_for(points, closed=closed)
        emitted = len(plan.dimensions) + sum(
            1 for c in plan.constraints
            if c.kind in ("horizontal_align", "vertical_align")
            and all(r.entity != "__origin__" for r in c.refs))
        assert emitted == expected

    def test_the_simulator_calls_the_bracket_fully_constrained(self):
        from inventor_mcp.builder import build_part
        from inventor_mcp.schema import PartRecipe
        from inventor_mcp.session import Session

        session = Session(backend_kind="mock")
        session.ensure_backend().connect()
        build_part(session, PartRecipe.model_validate({
            "name": "L", "units": "mm",
            "parameters": [{"name": k, "value": v} for k, v in self.BRACKET_PARAMS.items()],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "polyline", "points": self.BRACKET, "closed": True}]},
                {"op": "extrude", "sketch": "S", "distance": 50},
            ]}))
        [info] = [s for s in session.backend.list_sketches(session.active)]
        assert info.degrees_of_freedom == 0
        assert sorted(info.driven_parameters) == ["base_len", "thk", "upright_h"]

    def test_a_centred_outline_subtracts_signed_coordinates(self):
        """The sign trap: -w/2 must not become +w/2 on the way to a dimension."""
        from inventor_mcp.expressions import UnitContext, evaluate
        from inventor_mcp.units import to_internal

        plan = self.plan_for(
            [["-w/2", "-h/2"], ["w/2", "-h/2"], ["w/2", "h/2"], ["-w/2", "h/2"]],
            parameters={"w": 40, "h": 20})
        table = {"w": to_internal(40, "mm"), "h": to_internal(20, "mm")}
        units = UnitContext(length="mm", angle="deg")
        for dimension in plan.dimensions:
            assert evaluate(dimension.expression, table, units).value == pytest.approx(
                dimension.value), f"{dimension.expression!r} does not mean {dimension.value}"
        # Four in total: two rails spanning the outline, and the two `_locate`
        # emits to position a first vertex that is not at the origin.
        rails = [d for d in plan.dimensions if d.optional]
        assert [(d.kind, d.expression, round(d.value, 6)) for d in rails] == [
            ("horizontal", "w/2 - (-w/2)", 4.0),
            ("vertical", "h/2 - (-h/2)", 2.0),
        ]

    def test_a_rail_at_the_datum_coordinate_is_aligned_not_dimensioned_to_zero(self):
        """A comb: the far teeth return to the datum's own X."""
        plan = self.plan_for(
            [[0, 0], [0, 10], [20, 10], [20, 0], [40, 0], [40, 10],
             [60, 10], [60, 0]], closed=True)
        assert all(d.value > 1e-4 for d in plan.dimensions), "no zero dimensions"

    def test_a_dependent_direction_constraint_frees_nothing(self):
        """Three collinear vertical points: one X rail, so no X dimension.

        The two vertical constraints put all three vertices on one X rail, so
        that axis is fully determined and contributes nothing. The three Y
        coordinates are genuinely independent, so two of them are dimensioned.
        """
        plan = self.plan_for([[0, 0], [0, 10], [0, 20]], closed=False)
        rails = [d for d in plan.dimensions if d.optional]
        assert [d.kind for d in rails] == ["vertical", "vertical"]
        assert [round(d.value, 6) for d in rails] == [1.0, 2.0]

    def test_locate_none_leaves_the_outline_free_to_move(self):
        plan = self.plan_for(self.BRACKET, locate="none",
                             parameters=self.BRACKET_PARAMS)
        assert len(plan.dimensions) == 4
        assert not any(r.entity == "__origin__"
                       for c in plan.constraints for r in c.refs)

    def test_asking_for_no_dimensions_still_means_no_dimensions(self):
        plan = self.plan_for(self.BRACKET, parameters=self.BRACKET_PARAMS,
                             dimension=False)
        assert plan.dimensions == []
