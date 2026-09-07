"""Defect 7: a circular pattern about an axis lying flat in the patterned face.

A `circular_pattern` turns about an axis perpendicular to the face it patterns.
A sketch line lies *in* its own sketch plane. So a plate sketched on XY whose
pattern axis is a line drawn on XY asks Inventor to revolve the holes about an
axis lying flat in the plate -- and nothing caught it. `check_recipe` passes it,
`validate_recipe` passes it, and the simulator returns `ok: true` with a
plausible volume, because `_repeat` multiplies the seed's volume delta by the
occurrence count and never reads the axis at all.

It is a **warning and not a finding**, which is the honest limit of a static
check: the same recipe is meaningless as a bolt circle and a legitimate way to
write a 180-degree flip, and nothing here can tell which was meant. So these
tests run in both directions, and the quiet half is the larger half -- a warning
that fires on a correct recipe teaches the reader to ignore the field, which is
the one thing this repository refuses to do.

The three unknowable cases are tested as carefully as the wrong ones, because
each is a place a guess would have been easy and wrong: an angled work plane is
not parallel to its base (the *simulator* thinks it is), a revolve's geometry
does not sit in its sketch plane, and a `two_points` work axis points wherever
its points put it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inventor_mcp.rehearsal import rehearse
from inventor_mcp.schema import PartRecipe

ROOT = Path(__file__).resolve().parent.parent

#: A 120 x 80 x 10 plate on XY. Every case below patterns something on it, so
#: the face being patterned is parallel to XY and the only correct axis is Z.
PLATE = [
    {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 120, "height": 80}]},
    {"op": "extrude", "name": "Body", "sketch": "Outline", "distance": 10},
]

#: One bolt hole to pattern, drilled from a sketch on the plate's own plane.
BOLT = [
    {"op": "sketch", "name": "Pilot", "plane": "xy", "entities": [
        {"type": "point", "position": ["pcd / 2", 0]}]},
    {"op": "hole", "name": "Bolt1", "sketch": "Pilot", "diameter": 5,
     "through_all": True, "direction": "negative"},
]


def report_for(operations: list[dict]) -> dict:
    recipe = PartRecipe.model_validate({
        "name": "T", "units": "mm",
        "parameters": [{"name": "pcd", "value": 60}],
        "operations": operations,
    })
    out = rehearse(recipe)
    assert out["ok"], out["findings"]
    return out


def complaints(operations: list[dict]) -> list[str]:
    """Only this check's warnings, so an unrelated one cannot pass a test."""
    return [entry["warning"] for entry in report_for(operations)["warnings"]
            if "pattern axis" in entry["warning"]]


class TestTheAxisThatLiesInTheFace:
    """The recipes defect 7 is about. Each one builds and reports `ok: true`."""

    def test_a_sketch_line_on_the_patterned_plane_is_warned_about(self):
        """The reproduction, verbatim from `FEATURE_COVERAGE.md`."""
        told = complaints(PLATE + [
            {"op": "sketch", "name": "Aim", "plane": "xy", "entities": [
                {"type": "line", "name": "Spoke", "start": [0, 0], "end": [30, 0]}]},
        ] + BOLT + [
            {"op": "circular_pattern", "name": "Ring", "features": ["Bolt1"],
             "axis": "Spoke", "count": 6}])
        assert len(told) == 1, told
        assert "lies in xy" in told[0]

    def test_an_origin_axis_in_the_patterned_plane_is_too(self):
        """X and Y both lie in XY, so both are wrong for a plate sketched on it.
        This is the cheapest way to make the mistake -- `axis` defaults to `"z"`,
        and changing it to `"x"` reads like a direction rather than an error.
        """
        told = complaints(PLATE + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "x", "count": 6}])
        assert len(told) == 1, told
        assert "runs along X" in told[0]

    def test_a_work_axis_built_from_such_a_line_is_no_better(self):
        """`work_axis` makes the right thing reachable; it does not stop the
        wrong one being asked for. `kind: "sketch_line"` inherits the defect."""
        told = complaints(PLATE + [
            {"op": "sketch", "name": "Aim", "plane": "xy", "entities": [
                {"type": "line", "name": "Spoke", "start": [0, 0], "end": [30, 0]}]},
            {"op": "work_axis", "name": "BadAxis", "kind": "sketch_line",
             "sketch": "Aim", "line": "Spoke"},
        ] + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "BadAxis",
             "count": 6}])
        assert len(told) == 1, told

    def test_a_parallel_work_plane_is_still_the_same_plane(self):
        """Drawing the axis line on a plane *above* the plate feels different and
        is not: an offset work plane is parallel to its base, so the line is
        still flat in the face. The chain is followed however long it is."""
        told = complaints(PLATE + [
            {"op": "work_plane", "name": "Above", "kind": "offset",
             "base": "xy", "offset": 30},
            {"op": "work_plane", "name": "Higher", "kind": "offset",
             "base": "Above", "offset": 10},
            {"op": "sketch", "name": "Aim", "plane": "Higher", "entities": [
                {"type": "line", "name": "Spoke", "start": [0, 0], "end": [30, 0]}]},
        ] + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "Spoke",
             "count": 6}])
        assert len(told) == 1, told
        assert "parallel to xy" in told[0]

    def test_an_omitted_feature_list_still_names_a_seed(self):
        """`features: []` means the previous feature, so the seed is known."""
        told = complaints(PLATE + BOLT + [
            {"op": "circular_pattern", "axis": "y", "count": 6}])
        assert len(told) == 1, told

    def test_the_warning_names_the_operation_that_carries_the_axis(self):
        report = report_for(PLATE + BOLT + [
            {"op": "circular_pattern", "name": "Ring", "features": ["Bolt1"],
             "axis": "x", "count": 6}])
        entry = next(e for e in report["warnings"] if "pattern axis" in e["warning"])
        assert "Ring" in entry["where"]

    def test_the_warning_says_the_volume_will_look_right(self):
        """The reason this needed saying at all: every number agrees with a
        correct build, so a reader checking volumes learns nothing."""
        report = report_for(PLATE + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "x", "count": 6}])
        why = next(e["why"] for e in report["warnings"] if "pattern axis" in e["warning"])
        assert "never reads the axis" in why

    def test_the_warning_names_the_operation_that_does_work(self):
        report = report_for(PLATE + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "x", "count": 6}])
        instead = next(e["instead"] for e in report["warnings"]
                       if "pattern axis" in e["warning"])
        assert "normal_to_plane" in instead and "mirror" in instead


class TestTheRecipesThatMustStayQuiet:
    """Correct patterns, including the two the work-axis branch was built for."""

    def test_the_off_centre_bolt_circle_says_nothing(self):
        """The recipe `work_axis` exists for: `normal_to_plane` on the plane the
        feature was built on is perpendicular to it by construction."""
        assert complaints(PLATE + [
            {"op": "work_axis", "name": "BoltAxis", "plane": "xy", "at": [30, 0]},
            {"op": "sketch", "name": "Pilot", "plane": "xy", "entities": [
                {"type": "point", "position": ["30 + pcd / 2", 0]}]},
            {"op": "hole", "name": "Bolt1", "sketch": "Pilot", "diameter": 5,
             "through_all": True, "direction": "negative"},
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "BoltAxis",
             "count": 6}]) == []

    def test_the_plain_origin_axis_case_says_nothing(self):
        assert complaints(PLATE + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "z",
             "count": 6}]) == []

    def test_the_workaround_on_a_perpendicular_plane_says_nothing(self):
        """The route that worked before `work_axis`, and which the branch
        measured before writing anything: a line on a plane perpendicular to the
        face can be the axis, and this check must not call it a fault."""
        assert complaints(PLATE + [
            {"op": "sketch", "name": "Aim", "plane": "xz", "entities": [
                {"type": "line", "name": "Spoke", "start": [30, 0], "end": [30, 40]}]},
        ] + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "Spoke",
             "count": 6}]) == []

    def test_a_seed_on_an_offset_plane_patterned_about_z_says_nothing(self):
        assert complaints(PLATE + [
            {"op": "work_plane", "name": "Above", "kind": "offset",
             "base": "xy", "offset": 10},
            {"op": "sketch", "name": "Pilot", "plane": "Above", "entities": [
                {"type": "point", "position": ["pcd / 2", 0]}]},
            {"op": "hole", "name": "Bolt1", "sketch": "Pilot", "diameter": 5,
             "through_all": True, "direction": "negative"},
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "z",
             "count": 6}]) == []

    def test_a_rectangular_pattern_is_not_this_check_s_business(self):
        """It translates rather than turning, so an in-plane axis is the norm."""
        assert complaints(PLATE + BOLT + [
            {"op": "rectangular_pattern", "features": ["Bolt1"], "axis1": "x",
             "count1": 3, "spacing1": 20}]) == []

    def test_no_shipped_example_triggers_it(self):
        """The belt pulley is the only one that patterns anything -- a hole from
        a sketch on XY, about Z -- and it is correct. A false positive on the
        shipped set would be this warning's own undoing.
        """
        for path in sorted(ROOT.glob("examples/*.json")):
            report = rehearse(PartRecipe.model_validate(json.loads(path.read_text())))
            offending = [e for e in report["warnings"] if "pattern axis" in e["warning"]]
            assert offending == [], f"{path.name}: {offending}"

    def test_no_calibration_fixture_triggers_it_either(self):
        for path in sorted(ROOT.glob("examples/calibration/*.json")):
            report = rehearse(PartRecipe.model_validate(json.loads(path.read_text())))
            offending = [e for e in report["warnings"] if "pattern axis" in e["warning"]]
            assert offending == [], f"{path.name}: {offending}"


class TestWhatItRefusesToGuess:
    """Three cases where an answer was available and would have been wrong."""

    def test_an_angled_work_plane_is_left_alone(self):
        """And the simulator disagrees, which is the point. `mock.work_plane`
        records every work plane against an origin base whatever its `kind`, so
        it believes an angled plane is parallel to its base. Inheriting that
        would report a correct angled-plane recipe as a fault.
        """
        assert complaints(PLATE + [
            {"op": "work_plane", "name": "Tilt", "kind": "angle", "base": "xy",
             "angle": "30 deg"},
            {"op": "sketch", "name": "Aim", "plane": "Tilt", "entities": [
                {"type": "line", "name": "Spoke", "start": [0, 0], "end": [30, 0]}]},
        ] + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "Spoke",
             "count": 6}]) == []

    def test_a_revolved_seed_is_left_alone(self):
        """A revolve's geometry does not sit in its sketch plane: the pulley's
        section is drawn on XZ and swept about Z. Treating a sketch plane as a
        face plane here would report the shipped example as a fault.
        """
        recipe = PartRecipe.model_validate({
            "name": "R", "units": "mm", "operations": [
                {"op": "sketch", "name": "Section", "plane": "xz", "entities": [
                    {"type": "rectangle", "center": [20, 5], "width": 20, "height": 10}]},
                {"op": "revolve", "name": "Blank", "sketch": "Section", "axis": "z"},
                {"op": "circular_pattern", "features": ["Blank"], "axis": "z",
                 "count": 4}]})
        out = rehearse(recipe)
        assert [e for e in out["warnings"] if "pattern axis" in e["warning"]] == []

    def test_a_two_point_work_axis_is_left_alone(self):
        """Where it points depends on where its points sit, and this check does
        not reason about that. A guess would fire on correct recipes.
        """
        assert complaints(PLATE + [
            {"op": "work_point", "name": "A", "plane": "xy", "at": [0, 0]},
            {"op": "work_point", "name": "B", "plane": "xy", "at": [30, 0]},
            {"op": "work_axis", "name": "Through", "kind": "two_points",
             "points": ["A", "B"]},
        ] + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "Through",
             "count": 6}]) == []

    def test_a_pattern_whose_axis_is_right_for_one_seed_is_left_alone(self):
        """Two seeds on perpendicular planes: Z is correct for the XY one and
        wrong for the XZ one, and which was meant is the caller's to say.
        """
        assert complaints([
            {"op": "sketch", "name": "Outline", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 120, "height": 80}]},
            {"op": "extrude", "name": "Body", "sketch": "Outline", "distance": 10},
            {"op": "sketch", "name": "Upright", "plane": "xz", "entities": [
                {"type": "rectangle", "center": [0, 20], "width": 40, "height": 40}]},
            {"op": "extrude", "name": "Wall", "sketch": "Upright", "distance": 8},
        ] + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1", "Wall"], "axis": "z",
             "count": 4}]) == []

    def test_an_unresolvable_axis_is_left_to_the_builder_to_report(self):
        """The check must not turn a bad reference into a worse message. The
        builder resolves the same name moments later and lists the candidates.
        """
        recipe = PartRecipe.model_validate({
            "name": "T", "units": "mm",
            "parameters": [{"name": "pcd", "value": 60}],
            "operations": PLATE + BOLT + [
                {"op": "circular_pattern", "features": ["Bolt1"], "axis": "Ghost",
                 "count": 6}]})
        out = rehearse(recipe)
        assert not out["ok"]
        assert any("Ghost" in f["error"] for f in out["findings"]), out["findings"]
        assert [e for e in out["warnings"] if "pattern axis" in e["warning"]] == []


class TestTheLabelIsResolvedAgainstWhatExistedThen:
    def test_a_line_created_after_the_pattern_cannot_claim_the_label(self):
        """Two sketches name a line `Spoke`, and only the earlier one existed
        when the pattern ran.

        `resolve_axis` searches sketches in reverse creation order, so resolving
        against the *finished* document would pick the later XZ line -- a
        correct axis -- and report nothing about a pattern that really is turning
        about the earlier XY one. The narrowed context is what makes the warning
        describe the build that will actually happen.
        """
        told = complaints(PLATE + [
            {"op": "sketch", "name": "Early", "plane": "xy", "entities": [
                {"type": "line", "name": "Spoke", "start": [0, 0], "end": [30, 0]}]},
        ] + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "Spoke",
             "count": 6},
            {"op": "sketch", "name": "Late", "plane": "xz", "entities": [
                {"type": "line", "name": "Spoke", "start": [30, 0], "end": [30, 40]}]},
        ])
        assert len(told) == 1, told
        assert "lies in xy" in told[0]


class TestTheSeedTheCheckThinksItIsJudging:
    """Two mistakes this check made in its first draft, both false positives."""

    def test_an_unnamed_feature_in_between_clears_the_seed(self):
        """`features: []` means the previous feature, and an unnamed operation is
        one -- the backend invents its name. Carrying the last *named* feature
        forward instead judged the pattern against a seed on a different plane:
        here the XZ upright is the previous feature and X is a correct axis for
        it, while the named XY hole two operations back would have read as a
        fault.
        """
        assert complaints(PLATE + BOLT + [
            {"op": "sketch", "name": "Upright", "plane": "xz", "entities": [
                {"type": "rectangle", "center": [0, 20], "width": 40, "height": 40}]},
            {"op": "extrude", "sketch": "Upright", "distance": 8},
            {"op": "circular_pattern", "axis": "x", "count": 4}]) == []

    def test_a_work_axis_takes_the_sketch_that_was_current_when_it_was_made(self):
        """A `sketch_line` work axis with no `sketch` takes the most recent one at
        the moment the axis is created. Reading that at the pattern instead would
        pick up `Later` -- an XY line, and a fault -- for an axis that really
        lies along the XZ line it was built from, which is correct.
        """
        assert complaints(PLATE + [
            {"op": "sketch", "name": "Aim", "plane": "xz", "entities": [
                {"type": "line", "name": "Upright", "start": [30, 0], "end": [30, 40]}]},
            {"op": "work_axis", "name": "TurnAbout", "kind": "sketch_line",
             "line": "Upright"},
            {"op": "sketch", "name": "Later", "plane": "xy", "entities": [
                {"type": "line", "name": "Flat", "start": [0, 0], "end": [30, 0]}]},
        ] + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "TurnAbout",
             "count": 6}]) == []

    def test_moving_that_axis_after_the_xy_sketch_makes_it_a_fault(self):
        """The counterpart, and what stops the test above from passing for the
        wrong reason. Same recipe, same axis, same label -- only the work axis is
        created one operation later, so the most recent sketch is now the XY one
        and the axis really is flat in the face.
        """
        told = complaints(PLATE + [
            {"op": "sketch", "name": "Aim", "plane": "xz", "entities": [
                {"type": "line", "name": "Upright", "start": [30, 0], "end": [30, 40]}]},
            {"op": "sketch", "name": "Later", "plane": "xy", "entities": [
                {"type": "line", "name": "Upright", "start": [0, 0], "end": [30, 0]}]},
            {"op": "work_axis", "name": "TurnAbout", "kind": "sketch_line",
             "line": "Upright"},
        ] + BOLT + [
            {"op": "circular_pattern", "features": ["Bolt1"], "axis": "TurnAbout",
             "count": 6}])
        assert len(told) == 1, told
        assert "lies in xy" in told[0]
