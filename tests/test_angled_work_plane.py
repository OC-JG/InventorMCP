"""An angled work plane, and what each half of the server can honestly say.

Defect 12: `WorkPlaneOp` has offered `kind: "angle"` since it was written, the
COM backend read `kind` only to spot `midplane` and built an **offset** plane
for everything else, and the simulator filed every plane against its base
whatever the kind -- so the rehearsal agreed with the build and neither half
could tell. It was refused on 2026-09-08 and built properly on 2026-09-09.

Two things are being tested, and they are different in kind.

The **schema** insists on an axis, because Inventor's
`AddByLinePlaneAndAngle(axis, plane, angle)` needs one and there is nothing to
default to: a plane turned about one of its own directions is a different plane
from the same plane turned about the other. Defaulting one would be defect 12
again with a tilt on it.

The **simulator** records the tilt and then declines the half it cannot do. Its
ledger holds axis-aligned prisms -- an outline in a plane's 2D coordinates plus
a near and a far along its normal -- and a sweep from a tilted plane is not one
of those. So the volume is still predicted, because area times depth does not
care how a prism is oriented, and the *placement* is declined in writing. That
distinction is the whole point: a simulator that placed the material anyway
would be wrong in the one direction nobody checks, because every later cut,
hole and pattern reads that ledger.
"""

from __future__ import annotations

import math

import pytest

from inventor_mcp.backend.com import backend as com
from inventor_mcp.builder import build_part, rehearse
from inventor_mcp.schema import PartRecipe

PLATE = [
    {"op": "sketch", "name": "S", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
    {"op": "extrude", "name": "Plate", "sketch": "S", "distance": 10},
]


def recipe(*operations: dict) -> PartRecipe:
    return PartRecipe.model_validate(
        {"name": "T", "units": "mm", "operations": PLATE + list(operations)})


TILT = {"op": "work_plane", "name": "Tilt", "kind": "angle", "base": "xy",
        "axis": "x", "angle": "30 deg"}


class TestTheAxisIsRequiredRatherThanGuessed:
    def test_an_angled_plane_needs_one(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="needs `axis`"):
            recipe({"op": "work_plane", "kind": "angle", "base": "xy",
                    "angle": "30 deg"})

    def test_and_no_other_kind_accepts_one(self):
        """Because it would mean nothing there, and a field that is silently
        ignored is how a recipe author learns the wrong lesson."""
        import pydantic

        for kind in ("offset", "midplane", "tangent"):
            with pytest.raises(pydantic.ValidationError, match="means nothing"):
                recipe({"op": "work_plane", "kind": kind, "base": "xy", "axis": "x"})

    def test_an_axis_that_names_nothing_fails_in_the_recipe(self, session):
        """Resolved through the same `resolve_axis` a pattern uses, so the
        message is in the recipe's vocabulary rather than a COM refusal."""
        out = build_part(session, recipe(
            {"op": "work_plane", "name": "Tilt", "kind": "angle", "base": "xy",
             "axis": "Nonexistent", "angle": "30 deg"}))
        assert out["ok"] is False
        assert any("Nonexistent" in str(error) for error in out["errors"]), out["errors"]


class TestTheSimulatorRecordsTheTilt:
    def test_the_plane_knows_it_is_tilted(self, session):
        out = build_part(session, recipe(TILT))
        assert out["ok"]
        plane = next(f for f in session.backend.list_features(out["document"])
                     if f.name == "Tilt")
        assert plane.detail["axis"] == "x"
        assert plane.detail["angle"]["expression"] == "30 deg"
        assert plane.detail["angle"]["value"] == pytest.approx(math.radians(30))

    def test_a_plane_offset_from_a_tilted_one_is_tilted_too(self, session):
        """Otherwise the tilt would be lost one plane along, which is the same
        defect one level of indirection away."""
        out = build_part(session, recipe(
            TILT,
            {"op": "work_plane", "name": "Higher", "kind": "offset",
             "base": "Tilt", "offset": 5}))
        assert out["ok"]
        higher = next(f for f in session.backend.list_features(out["document"])
                      if f.name == "Higher")
        assert "tilted" in higher.detail["placement"]

    def test_an_untilted_plane_says_nothing_about_placement(self, session):
        out = build_part(session, recipe(
            {"op": "work_plane", "name": "Up", "kind": "offset", "base": "xy",
             "offset": 10}))
        up = next(f for f in session.backend.list_features(out["document"])
                  if f.name == "Up")
        assert up.detail.get("placement") is None


class TestWhatTheLedgerDeclines:
    def test_a_feature_on_a_tilted_plane_predicts_its_volume(self, session):
        """Area times depth does not care how the prism is oriented, so the
        number is exact -- a 10 x 10 mm pad 5 mm deep is 0.5 cm3 on any plane."""
        out = build_part(session, recipe(
            TILT,
            {"op": "sketch", "name": "OnTilt", "plane": "Tilt", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 10, "height": 10}]},
            {"op": "extrude", "name": "Pad", "sketch": "OnTilt", "distance": 5}))
        assert out["ok"]
        volume = session.backend.mass_properties(out["document"]).volume
        assert volume == pytest.approx(24.0 + 0.5)

    def test_and_says_it_did_not_place_the_material(self, session):
        out = build_part(session, recipe(
            TILT,
            {"op": "sketch", "name": "OnTilt", "plane": "Tilt", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 10, "height": 10}]},
            {"op": "extrude", "name": "Pad", "sketch": "OnTilt", "distance": 5}))
        pad = next(f for f in session.backend.list_features(out["document"])
                   if f.name == "Pad")
        assert pad.detail["placement"].startswith("not recorded:")
        assert "30 degrees about x" in pad.detail["placement"]

    def test_a_cut_there_is_charged_its_whole_sweep_and_says_so(self, session):
        """The pre-ledger answer, honestly labelled. `_material_spans` walks one
        axis and every prism in it is square to the origin planes, so what a
        tilted sweep meets is not something it can measure -- and charging the
        whole sweep is the upper bound, which reads as an obviously wrong volume
        rather than as a cut that worked."""
        out = build_part(session, recipe(
            TILT,
            {"op": "sketch", "name": "Slot", "plane": "Tilt", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 5, "height": 5}]},
            {"op": "extrude", "name": "Cut", "sketch": "Slot", "distance": 3,
             "operation": "cut"}))
        cut = next(f for f in session.backend.list_features(out["document"])
                   if f.name == "Cut")
        assert "the whole swept prism" in cut.detail["volume_from"]
        assert "not something this can measure" in cut.detail["volume_from"]
        volume = session.backend.mass_properties(out["document"]).volume
        assert volume == pytest.approx(24.0 - 0.25 * 0.3)

    def test_an_untilted_cut_still_measures_what_it_meets(self, session):
        """The guard must not have switched the ledger off for everyone: this
        is the behaviour the enclosure's cable entry depends on."""
        out = build_part(session, recipe(
            {"op": "sketch", "name": "Slot", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 5, "height": 5}]},
            {"op": "extrude", "name": "Cut", "sketch": "Slot", "distance": 3,
             "operation": "cut"}))
        cut = next(f for f in session.backend.list_features(out["document"])
                   if f.name == "Cut")
        assert "the whole swept prism" not in (cut.detail["volume_from"] or "")


class TestTheRehearsalSaysWhichHalfIsTrusted:
    def test_a_feature_on_a_tilted_plane_is_warned_about(self):
        report = rehearse(recipe(
            TILT,
            {"op": "sketch", "name": "OnTilt", "plane": "Tilt", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 10, "height": 10}]},
            {"op": "extrude", "name": "Pad", "sketch": "OnTilt", "distance": 5}))
        assert report["ok"]
        assert any("predicts its volume and not its placement" in w["warning"]
                   for w in report["warnings"]), report["warnings"]

    def test_a_cut_there_gets_the_sharper_one(self):
        report = rehearse(recipe(
            TILT,
            {"op": "sketch", "name": "Slot", "plane": "Tilt", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 5, "height": 5}]},
            {"op": "extrude", "name": "Cut", "sketch": "Slot", "distance": 3,
             "operation": "cut"}))
        assert any("charged its whole swept prism" in w["warning"]
                   for w in report["warnings"]), report["warnings"]

    def test_the_kind_itself_is_no_longer_warned_about(self):
        """It was, until the backend could build it. A warning about something
        that works teaches a caller to avoid the thing they wanted."""
        report = rehearse(recipe(TILT))
        assert [w for w in report["warnings"]
                if "work_plane.kind" in w["warning"]] == []

    def test_tangent_still_is(self):
        report = rehearse(recipe(
            {"op": "work_plane", "name": "Round", "kind": "tangent", "base": "xy"}))
        assert any("`work_plane.kind` set to 'tangent' does not work" in w["warning"]
                   for w in report["warnings"]), report["warnings"]


class TestTheComCallIsThePublishedOne:
    def test_it_calls_add_by_line_plane_and_angle(self):
        import inspect

        source = inspect.getsource(com.ComBackend.work_plane)
        assert "WorkPlanes.AddByLinePlaneAndAngle(" in source
        assert "angle" in com.ComBackend._WORK_PLANE_KINDS

    def test_the_angle_goes_in_as_a_value_and_then_as_an_expression(self):
        """The pattern the offset already uses, and in that order: the value
        makes the geometry right whatever happens next, and the expression is
        what keeps it parametric. Created at zero instead, a failure to set the
        expression would leave a plane flat against its base -- which is
        defect 12's own symptom."""
        import inspect

        source = inspect.getsource(com.ComBackend.work_plane)
        assert "axis, base, request.angle.value)" in source
        assert "plane.Definition.Angle.Expression = request.angle.expression" in source

    def test_tangent_is_refused_with_the_call_it_would_need(self):
        import inspect

        source = inspect.getsource(com.ComBackend.work_plane)
        assert "AddByPlaneAndTangent" in source
        assert "tangent" not in com.ComBackend._WORK_PLANE_KINDS
