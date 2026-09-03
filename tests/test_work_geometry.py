"""Work points and work axes: the geometry a pattern turns about.

The operation exists for one reason, and the reason is geometric rather than
incidental: a `circular_pattern` turns about an axis perpendicular to the face
it patterns, and a sketch line lies *in* its own sketch plane, so no line drawn
on a plate's face can ever be that plate's bolt-circle axis.

What this file holds the simulator to is where the axis ends up. The COM
backend's three calls are unmeasured -- see the note above
`ComBackend._carrier_point` -- so nothing here claims anything about Inventor.
"""

from __future__ import annotations

import math

import pytest

from inventor_mcp.builder import build_part, resolve_axis
from inventor_mcp.errors import FeatureError
from inventor_mcp.schema import PartRecipe, WorkAxisOp


def plate_with(*operations: dict) -> PartRecipe:
    """A 120 x 80 x 10 plate on XY, plus whatever the test is about."""
    return PartRecipe.model_validate({
        "name": "Plate",
        "units": "mm",
        "operations": [
            {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 120, "height": 80}]},
            {"op": "extrude", "name": "Body", "sketch": "Outline", "distance": 10},
            *operations,
        ],
    })


def step(result: dict, op: str) -> dict:
    return next(entry for entry in result["operations"] if entry["op"] == op)


def failure(result: dict) -> tuple[str, str]:
    """The one error a failed build reported, as `build_part` hands it over.

    A recipe's operations are run by `build_part`, which collects what went
    wrong rather than raising -- a half-built part plus a named failure is the
    evidence, and that is what a caller is given. So these tests read the
    result, not an exception.
    """
    assert not result["ok"], "expected this build to fail"
    assert len(result["errors"]) == 1, result["errors"]
    entry = result["errors"][0]
    return entry["error"], entry.get("details") or ""


class TestNormalToPlane:
    """The bolt-circle case: perpendicular to a plane, through a point on it."""

    @pytest.mark.parametrize("plane,at,through,direction", [
        ("xy", [30, 20], [3.0, 2.0, 0.0], [0.0, 0.0, 1.0]),
        ("xz", [30, 20], [3.0, 0.0, 2.0], [0.0, 1.0, 0.0]),
        ("yz", [30, 20], [0.0, 3.0, 2.0], [1.0, 0.0, 0.0]),
    ])
    def test_the_axis_stands_normal_to_its_plane(self, session, plane, at, through, direction):
        result = build_part(session, plate_with(
            {"op": "work_axis", "name": "A", "plane": plane, "at": at},
        ))
        detail = step(result, "work_axis")["detail"]
        assert detail["through"] == pytest.approx(through)
        assert detail["direction"] == pytest.approx(direction)

    def test_the_direction_is_the_planes_own_normal(self, session):
        """Not merely unit length -- the same vector `plane_normal` reports."""
        from inventor_mcp.backend.mock.backend import plane_normal

        for plane in ("xy", "xz", "yz"):
            result = build_part(session, plate_with(
                {"op": "work_axis", "name": f"A_{plane}", "plane": plane, "at": [5, 5]},
            ))
            detail = step(result, "work_axis")["detail"]
            assert detail["direction"] == pytest.approx(plane_normal(plane))

    def test_an_axis_at_the_origin_still_has_a_direction(self, session):
        result = build_part(session, plate_with(
            {"op": "work_axis", "name": "A", "plane": "xy", "at": [0, 0]},
        ))
        detail = step(result, "work_axis")["detail"]
        assert detail["through"] == pytest.approx([0.0, 0.0, 0.0])
        assert detail["direction"] == pytest.approx([0.0, 0.0, 1.0])

    def test_it_sits_on_a_work_planes_offset_not_the_origins(self, session):
        """A work axis on a raised plane starts on that plane, not below it."""
        result = build_part(session, plate_with(
            {"op": "work_plane", "name": "Top", "kind": "offset", "base": "xy", "offset": 10},
            {"op": "work_axis", "name": "A", "plane": "Top", "at": [30, 0]},
        ))
        detail = step(result, "work_axis")["detail"]
        assert detail["through"] == pytest.approx([3.0, 0.0, 1.0])

    def test_an_unknown_plane_is_refused_by_name(self, session):
        message, hint = failure(build_part(session, plate_with(
            {"op": "work_axis", "name": "A", "plane": "Nowhere", "at": [1, 1]},
        )))
        assert "Unknown plane 'Nowhere'" in message
        assert "work plane created earlier" in hint


class TestParametersReachTheAxis:
    """`Resolved(expression, value)` all the way down, for work geometry too."""

    def test_the_expression_survives_into_the_axis(self, session):
        result = build_part(session, PartRecipe.model_validate({
            "name": "Parametric", "units": "mm",
            "parameters": [{"name": "bolt_x", "value": 30}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 120, "height": 80}]},
                {"op": "extrude", "name": "Body", "sketch": "S", "distance": 10},
                {"op": "work_axis", "name": "A", "plane": "xy", "at": ["bolt_x", 0]},
            ],
        }))
        at = step(result, "work_axis")["detail"]["at"]
        assert at[0]["expression"] == "bolt_x"
        assert at[0]["value"] == pytest.approx(3.0)

    def test_a_composite_expression_survives_too(self, session):
        result = build_part(session, PartRecipe.model_validate({
            "name": "Parametric", "units": "mm",
            "parameters": [{"name": "pcd", "value": 60}],
            "operations": [
                {"op": "sketch", "name": "S", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 120, "height": 80}]},
                {"op": "extrude", "name": "Body", "sketch": "S", "distance": 10},
                {"op": "work_point", "name": "P", "plane": "xy", "at": ["pcd / 2", 0]},
            ],
        }))
        at = step(result, "work_point")["detail"]["at"]
        assert at[0]["expression"] == "pcd / 2"
        assert at[0]["value"] == pytest.approx(3.0)


class TestTwoPoints:
    def test_the_axis_runs_from_the_first_point_to_the_second(self, session):
        result = build_part(session, plate_with(
            {"op": "work_point", "name": "P1", "plane": "xy", "at": [0, 0]},
            {"op": "work_point", "name": "P2", "plane": "xy", "at": [0, 0], "offset": 10},
            {"op": "work_axis", "name": "A", "kind": "two_points", "points": ["P1", "P2"]},
        ))
        detail = step(result, "work_axis")["detail"]
        assert detail["through"] == pytest.approx([0.0, 0.0, 0.0])
        assert detail["direction"] == pytest.approx([0.0, 0.0, 1.0])

    def test_the_direction_is_a_unit_vector(self, session):
        result = build_part(session, plate_with(
            {"op": "work_point", "name": "P1", "plane": "xy", "at": [0, 0]},
            {"op": "work_point", "name": "P2", "plane": "xy", "at": [30, 40]},
            {"op": "work_axis", "name": "A", "kind": "two_points", "points": ["P1", "P2"]},
        ))
        direction = step(result, "work_axis")["detail"]["direction"]
        assert math.sqrt(sum(c * c for c in direction)) == pytest.approx(1.0)
        assert direction == pytest.approx([0.6, 0.8, 0.0])

    def test_two_points_at_the_same_place_are_refused(self, session):
        """Rather than an axis with no direction, silently pointing nowhere."""
        message, _ = failure(build_part(session, plate_with(
            {"op": "work_point", "name": "P1", "plane": "xy", "at": [5, 5]},
            {"op": "work_point", "name": "P2", "plane": "xy", "at": [5, 5]},
            {"op": "work_axis", "name": "A", "kind": "two_points", "points": ["P1", "P2"]},
        )))
        assert "same place" in message

    def test_an_unknown_work_point_names_the_ones_that_exist(self, session):
        message, hint = failure(build_part(session, plate_with(
            {"op": "work_point", "name": "P1", "plane": "xy", "at": [0, 0]},
            {"op": "work_axis", "name": "A", "kind": "two_points", "points": ["P1", "Ghost"]},
        )))
        assert "No work point named 'Ghost'" in message
        assert "P1" in hint


class TestSketchLine:
    def test_the_axis_lies_along_the_named_line(self, session):
        result = build_part(session, plate_with(
            {"op": "sketch", "name": "Ax", "plane": "xz", "entities": [
                {"type": "line", "name": "Spin", "start": [30, 0], "end": [30, 10]}]},
            {"op": "work_axis", "name": "A", "kind": "sketch_line",
             "sketch": "Ax", "line": "Spin"},
        ))
        detail = step(result, "work_axis")["detail"]
        assert detail["through"] == pytest.approx([3.0, 0.0, 0.0])
        assert detail["direction"] == pytest.approx([0.0, 0.0, 1.0])

    def test_the_sketch_defaults_to_the_most_recent(self, session):
        result = build_part(session, plate_with(
            {"op": "sketch", "name": "Ax", "plane": "xz", "entities": [
                {"type": "line", "name": "Spin", "start": [0, 0], "end": [0, 10]}]},
            {"op": "work_axis", "name": "A", "kind": "sketch_line", "line": "Spin"},
        ))
        assert step(result, "work_axis")["detail"]["sketch"] == "Ax"

    def test_an_unknown_line_names_the_entities_that_exist(self, session):
        message, hint = failure(build_part(session, plate_with(
            {"op": "sketch", "name": "Ax", "plane": "xz", "entities": [
                {"type": "line", "name": "Spin", "start": [0, 0], "end": [0, 10]}]},
            {"op": "work_axis", "name": "A", "kind": "sketch_line",
             "sketch": "Ax", "line": "Ghost"},
        )))
        assert "no line named 'Ghost'" in message
        assert "Spin" in hint


class TestTheSchemaRefusesAKindWithoutItsGeometry:
    """A kind given without what it names would fall back to the default plane
    and build an axis somewhere nobody asked for. That is the quiet wrong
    answer this schema exists to refuse."""

    @pytest.mark.parametrize("payload,message", [
        ({"kind": "two_points", "points": []}, "exactly two"),
        ({"kind": "two_points", "points": ["P1"]}, "exactly two"),
        ({"kind": "two_points", "points": ["P1", "P2", "P3"]}, "exactly two"),
        ({"kind": "sketch_line"}, "needs `line`"),
        ({"kind": "normal_to_plane", "points": ["P1", "P2"]}, "only means something"),
        ({"kind": "normal_to_plane", "line": "L"}, "only means something"),
        ({"kind": "two_points", "points": ["P1", "P2"], "line": "L"}, "only means something"),
    ])
    def test_refused(self, payload, message):
        with pytest.raises(ValueError, match=message):
            WorkAxisOp.model_validate({"op": "work_axis", **payload})

    def test_the_default_kind_needs_nothing_extra(self):
        axis = WorkAxisOp.model_validate({"op": "work_axis"})
        assert axis.kind == "normal_to_plane"
        assert axis.plane == "xy"


class TestWorkPoints:
    def test_the_offset_lifts_it_along_the_planes_normal(self, session):
        result = build_part(session, plate_with(
            {"op": "work_point", "name": "P", "plane": "xy", "at": [30, 20], "offset": 5},
        ))
        detail = step(result, "work_point")["detail"]
        assert detail["position_cm"] == pytest.approx([3.0, 2.0, 0.5])

    def test_a_work_planes_offset_adds_to_the_points_own(self, session):
        result = build_part(session, plate_with(
            {"op": "work_plane", "name": "Top", "kind": "offset", "base": "xy", "offset": 10},
            {"op": "work_point", "name": "P", "plane": "Top", "at": [0, 0], "offset": 5},
        ))
        detail = step(result, "work_point")["detail"]
        assert detail["position_cm"] == pytest.approx([0.0, 0.0, 1.5])


class TestResolvingAnAxisReference:
    def test_a_work_axis_is_found_by_name(self, session):
        build_part(session, plate_with(
            {"op": "work_axis", "name": "BoltAxis", "plane": "xy", "at": [30, 0]},
        ))
        context = session.context()
        spec = resolve_axis(context, "BoltAxis")
        assert (spec.kind, spec.value) == ("work_axis", "BoltAxis")

    def test_an_origin_axis_still_resolves_to_itself(self, session):
        build_part(session, plate_with())
        spec = resolve_axis(session.context(), "z")
        assert (spec.kind, spec.value) == ("work_axis", "z")

    def test_a_work_axis_wins_over_a_sketch_line_of_the_same_name(self, session):
        """The work axis is an explicit reference; a sketch label is a search."""
        build_part(session, plate_with(
            {"op": "sketch", "name": "Ax", "plane": "xz", "entities": [
                {"type": "line", "name": "Shared", "start": [0, 0], "end": [0, 10]}]},
            {"op": "work_axis", "name": "Shared", "kind": "sketch_line",
             "sketch": "Ax", "line": "Shared"},
        ))
        spec = resolve_axis(session.context(), "Shared")
        assert spec.kind == "work_axis"

    def test_an_unresolvable_axis_lists_the_work_axes_that_exist(self, session):
        build_part(session, plate_with(
            {"op": "work_axis", "name": "BoltAxis", "plane": "xy", "at": [30, 0]},
        ))
        with pytest.raises(FeatureError, match="Cannot resolve 'Ghost'") as raised:
            resolve_axis(session.context(), "Ghost")
        assert "BoltAxis" in (raised.value.hint or "")


class TestTheBoltCircleItWasBuiltFor:
    def test_a_pattern_turns_about_a_created_work_axis(self, session):
        """The whole point: six holes about a centre that is not the origin."""
        result = build_part(session, PartRecipe.model_validate({
            "name": "OffCentreBoltCircle", "units": "mm",
            "parameters": [{"name": "bolt_x", "value": 30}, {"name": "pcd", "value": 30}],
            "operations": [
                {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
                    {"type": "rectangle", "center": [0, 0], "width": 120, "height": 80}]},
                {"op": "extrude", "name": "Body", "sketch": "Outline", "distance": 10},
                {"op": "work_axis", "name": "BoltAxis", "plane": "xy", "at": ["bolt_x", 0]},
                {"op": "sketch", "name": "Pilot", "plane": "xy", "entities": [
                    {"type": "point", "position": ["bolt_x + pcd / 2", 0]}]},
                {"op": "hole", "name": "Bolt1", "sketch": "Pilot", "diameter": 5,
                 "through_all": True, "direction": "negative"},
                {"op": "circular_pattern", "name": "BoltCircle", "features": ["Bolt1"],
                 "axis": "BoltAxis", "count": 6, "angle": "360 deg"},
            ],
        }))
        assert result["ok"], result["errors"]
        pattern = step(result, "circular_pattern")
        assert pattern["detail"]["count"] == 6
        # Six holes of 5 mm through 10 mm of plate, one of them the seed.
        bore = math.pi * 0.25 ** 2 * 1.0
        assert step(result, "hole")["detail"]["diameter"]["value"] == pytest.approx(0.25 * 2)
        assert pattern["detail"]["volume_note"].startswith("5 more occurrence(s)")
        assert result["mass_properties"]["volume"] == pytest.approx(
            120 * 80 * 10 / 1000 - 6 * bore, rel=1e-6
        )

    def test_a_work_axis_is_a_feature_in_its_own_right(self, session):
        """It appears in the tree, so it can be suppressed, renamed and found."""
        build_part(session, plate_with(
            {"op": "work_axis", "name": "BoltAxis", "plane": "xy", "at": [30, 0]},
            {"op": "work_point", "name": "Datum", "plane": "xy", "at": [10, 10]},
        ))
        kinds = {f.name: f.kind for f in session.backend.list_features(session.context().doc_id)}
        assert kinds["BoltAxis"] == "work_axis"
        assert kinds["Datum"] == "work_point"
