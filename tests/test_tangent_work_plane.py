"""A tangent work plane, and the two different things being checked.

`work_plane` with `kind: "tangent"` was accepted by the schema and refused by
the COM backend from 2026-09-08 to 2026-09-09, because Inventor's
`WorkPlanes.AddByPlaneAndTangent(plane, face)` wants a cylindrical face and
the recipe had no field naming one. `face` is that field, a `Selector` resolved
the way a fillet's edges are.

The **recipe** side is a rule about what a tangent plane *is*: exactly one
cylindrical face. A selector matching none, several, or a flat face describes
no plane at all, so it is refused in the recipe's own vocabulary rather than
handed to Inventor to answer with "Exception occurred". That rule lives on
`Backend`, above both implementations, so a rehearsal refuses exactly what a
live build would -- which is the whole reason for rehearsing.

The **simulator** side is a refusal of a different sort, and a sharper one than
the angled plane's. An angled plane is somewhere known and not axis-aligned. A
tangent plane's position depends on the cylinder it touches, and a face in this
ledger is a midpoint and an area -- so the offset is not something it can work
out. That refusal is deliberately written to hold whichever plane Inventor
returns: the published signature says `AddByPlaneAndTangent(plane, face)` and
nothing here has read whether the result is parallel to that plane or
perpendicular to it, and a simulator resting on the more likely reading of an
unread page is how defect 5 survived three runs.
"""

from __future__ import annotations

import pytest

from inventor_mcp.backend.base import Backend
from inventor_mcp.backend.com import backend as com
from inventor_mcp.backend.mock.backend import MockBackend
from inventor_mcp.builder import build_part, rehearse
from inventor_mcp.schema import PartRecipe

#: A plate with one cylindrical boss on it, so there is exactly one cylindrical
#: face to touch and five planar ones to be refused for.
PLATE_AND_BOSS = [
    {"op": "sketch", "name": "S", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
    {"op": "extrude", "name": "Plate", "sketch": "S", "distance": 10},
    {"op": "sketch", "name": "Round", "plane": "xy", "entities": [
        {"type": "circle", "center": [0, 0], "diameter": 20}]},
    {"op": "extrude", "name": "Boss", "sketch": "Round", "distance": 25},
]

#: `near` is required for a tangent plane, and it is the proximity
#: point Inventor's own call takes: the boss is on the Z axis with a
#: 10 mm radius, so this is the +X side of it.
CYLINDER = {"kind": "face", "filter": "cylindrical", "limit": 1,
            "near": [10, 0, 20]}

TOUCH = {"op": "work_plane", "name": "Touch", "kind": "tangent", "base": "xy",
         "face": CYLINDER}


def recipe(*operations: dict) -> PartRecipe:
    return PartRecipe.model_validate(
        {"name": "T", "units": "mm", "operations": PLATE_AND_BOSS + list(operations)})


def feature(session, out: dict, name: str):
    return next(f for f in session.backend.list_features(out["document"])
                if f.name == name)


class TestTheFaceIsRequiredRatherThanGuessed:
    def test_a_tangent_plane_needs_one(self):
        import pydantic

        with pytest.raises(pydantic.ValidationError, match="needs `face`"):
            recipe({"op": "work_plane", "kind": "tangent", "base": "xy"})

    def test_and_no_other_kind_accepts_one(self):
        """A field silently ignored is how a recipe author learns the wrong
        lesson about their own recipe."""
        import pydantic

        for kind in ("offset", "midplane", "angle"):
            with pytest.raises(pydantic.ValidationError, match="means nothing"):
                recipe({"op": "work_plane", "kind": kind, "base": "xy",
                        "axis": "x", "face": CYLINDER})


class TestOneCylindricalFaceOrARefusal:
    """The rule about what a tangent plane is, checked through the simulator
    because that is where a caller meets it first."""

    def test_one_cylindrical_face_builds(self, session):
        out = build_part(session, recipe(TOUCH))
        assert out["ok"], out["errors"]
        plane = feature(session, out, "Touch")
        assert plane.detail["kind"] == "tangent"
        assert "cylindrical" in plane.detail["face_description"]

    def test_no_match_is_refused_and_says_where_to_look(self, session):
        out = build_part(session, recipe(
            {"op": "work_plane", "name": "Touch", "kind": "tangent", "base": "xy",
             "face": {"kind": "face", "filter": "cylindrical",
                      "near": [500, 500, 500], "within": 1}}))
        assert out["ok"] is False
        assert any("matched no faces" in str(e) for e in out["errors"]), out["errors"]

    def test_two_cylinders_are_refused_because_that_is_not_a_plane(self, session):
        """Two tangent planes exist and the recipe has not said which. Inventor
        would take the collection and pick; this refuses instead, because a
        plane chosen for the caller is a plane they cannot revise."""
        out = build_part(session, PartRecipe.model_validate(
            {"name": "T", "units": "mm", "operations": PLATE_AND_BOSS + [
                {"op": "sketch", "name": "Second", "plane": "xy", "entities": [
                    {"type": "circle", "center": [20, 0], "diameter": 8}]},
                {"op": "extrude", "name": "Pin", "sketch": "Second", "distance": 15},
                {"op": "work_plane", "name": "Touch", "kind": "tangent",
                 "base": "xy", "face": {"kind": "face", "filter": "cylindrical",
                                        "near": [0, 0, 20]}}]}))
        assert out["ok"] is False
        assert any("not a plane" in str(e) for e in out["errors"]), out["errors"]

    def test_a_planar_face_is_refused_with_the_geometry_it_turned_out_to_be(
            self, session):
        """The message names what was matched. "Exception occurred" from
        Inventor would not, and that is the whole reason the check is here
        rather than at the COM call."""
        out = build_part(session, recipe(
            {"op": "work_plane", "name": "Touch", "kind": "tangent", "base": "xy",
             "face": {"kind": "face", "filter": "top", "limit": 1,
                      "near": [0, 0, 10]}}))
        assert out["ok"] is False
        assert any("planar face" in str(e) and "cylindrical" in str(e)
                   for e in out["errors"]), out["errors"]

    def test_the_rule_lives_above_both_backends(self):
        """So the rehearsal refuses what the live build refuses. Two copies of
        a rule are one rule until the day one copy is edited."""
        assert MockBackend._one_cylindrical_face is Backend._one_cylindrical_face
        assert com.ComBackend._one_cylindrical_face is Backend._one_cylindrical_face


class TestTheSimulatorDeclinesToPlaceIt:
    def test_the_plane_records_that_it_cannot_be_located(self, session):
        out = build_part(session, recipe(TOUCH))
        plane = feature(session, out, "Touch")
        assert plane.detail["placement"].startswith("recorded as unlocated:")

    def test_a_feature_on_it_gets_its_volume_and_not_its_placement(self, session):
        """The volume stands: area times depth does not care how a prism is
        oriented. The placement is declined in writing, because placing it
        would be wrong in the one direction nobody checks -- every later cut,
        hole and pattern reads the ledger."""
        before = 60 * 40 * 10 / 1000 + 3.141592653589793 * 10 ** 2 * 25 / 1000
        out = build_part(session, recipe(
            TOUCH,
            {"op": "sketch", "name": "Pad", "plane": "Touch", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 8, "height": 8}]},
            {"op": "extrude", "name": "Lug", "sketch": "Pad", "distance": 5}))
        assert out["ok"], out["errors"]
        lug = feature(session, out, "Lug")
        assert lug.detail["placement"].startswith("not recorded:")
        assert "tangent to" in lug.detail["placement"]
        assert session.backend.mass_properties(out["document"]).volume == \
            pytest.approx(before + 8 * 8 * 5 / 1000, rel=1e-9)

    def test_the_reason_given_does_not_claim_an_angle(self, session):
        """An angled plane's note carries its angle; this one has none to
        carry, and inventing one would be a claim about a page nobody here has
        read."""
        out = build_part(session, recipe(TOUCH))
        plane = feature(session, out, "Touch")
        assert "degrees" not in plane.detail["placement"]
        assert plane.detail.get("angle") is None

    def test_a_cut_there_is_charged_its_whole_sweep_and_says_so(self, session):
        out = build_part(session, recipe(
            TOUCH,
            {"op": "sketch", "name": "Slot", "plane": "Touch", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 6, "height": 6}]},
            {"op": "extrude", "name": "Bore", "sketch": "Slot", "distance": 30,
             "operation": "cut", "direction": "negative"}))
        assert out["ok"], out["errors"]
        assert "the whole swept prism" in feature(session, out, "Bore").detail["volume_from"]

    def test_an_extrude_aimed_at_it_declines_rather_than_guessing(self, session):
        """Under one reading of the published call the distance would be a
        known offset plus a radius; under the other there is no single
        distance. The ledger holds neither, so it charges nothing and says
        why."""
        out = build_part(session, recipe(
            TOUCH,
            {"op": "sketch", "name": "Up", "plane": "xy", "entities": [
                {"type": "rectangle", "center": [25, 0], "width": 6, "height": 6}]},
            {"op": "extrude", "name": "Post", "sketch": "Up", "extent": "to",
             "to": "Touch"}))
        assert out["ok"], out["errors"]
        why = feature(session, out, "Post").detail["volume_from"]
        assert why.startswith("not predicted:")
        assert "tangent to" in why


class TestTheRehearsalSaysWhichHalfIsTrusted:
    def test_it_no_longer_warns_that_the_kind_does_not_work(self):
        report = rehearse(recipe(TOUCH))
        assert [w for w in report["warnings"] if "does not work" in w["warning"]] == []

    def test_a_feature_on_it_is_warned_about(self):
        report = rehearse(recipe(
            TOUCH,
            {"op": "sketch", "name": "Pad", "plane": "Touch", "entities": [
                {"type": "rectangle", "center": [0, 0], "width": 8, "height": 8}]},
            {"op": "extrude", "name": "Lug", "sketch": "Pad", "distance": 5}))
        assert any("its volume is predicted and its placement is not" in w["warning"]
                   for w in report["warnings"]), report["warnings"]


class TestTheComCallIsThePublishedOne:
    def test_it_calls_add_by_plane_and_tangent(self):
        import inspect

        source = inspect.getsource(com.ComBackend.work_plane)
        assert "WorkPlanes.AddByPlaneAndTangent(" in source
        assert "tangent" in com.ComBackend._WORK_PLANE_KINDS

    def test_the_face_is_resolved_before_the_batch_opens(self):
        """So a selector that matches nothing fails without a transaction to
        roll back, and with the recipe's own message rather than a COM one."""
        import inspect

        source = inspect.getsource(com.ComBackend.work_plane)
        resolved = source.index("_one_cylindrical_face")
        opened = source.index("with self._batch(document)")
        assert resolved < opened
