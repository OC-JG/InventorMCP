"""`extent: "to"` and `extent: "from_to"`: a sweep that stops at the model.

"Extrude up to the underside of the lid" is how a boss is actually specified,
and until this it had to be written as a distance somebody derived -- which
means re-deriving it every time the lid moves, by hand, in a server whose whole
argument is that numbers should follow their parameters.

The two halves are different in kind, as usual.

Inventor's are `ExtrudeDefinition.SetToExtent(ToEntity, [ExtendToFace])` and
`SetFromToExtent(FromFace, ExtendFromFace, ToFace, ExtendToFace)`, published
and unmeasured. Neither takes a direction: which way the sweep runs is decided
by where the target is, which is the point of naming a target at all.

The simulator answers for **a target parallel to the sketch plane** and
declines otherwise, which is the whole of what a ledger of axis-aligned prisms
can honestly do. A plane sharing the sketch's origin plane and not tilted is a
known offset along one axis, so the length is exact arithmetic and the prism is
square to the axes like any other. A perpendicular plane, a tilted one, or a
`face:` handle is not -- a face here is a midpoint and an area, which says
where a face is and not which way it faces -- and the answer is to charge
nothing and say why, in writing, rather than to guess. The guess available is
the material's own thickness at the profile, which would be right for "up to
the far side of this plate" and wrong for everything else, in the direction
that reads as a feature that worked.
"""

from __future__ import annotations

import math

import pydantic
import pytest

from inventor_mcp.backend.com import backend as com
from inventor_mcp.builder import build_part, rehearse
from inventor_mcp.schema import ExtrudeOp, PartRecipe

PLATE = [
    {"op": "sketch", "name": "S", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 60, "height": 40}]},
    {"op": "extrude", "name": "Plate", "sketch": "S", "distance": 10},
    {"op": "work_plane", "name": "Lid", "kind": "offset", "base": "xy", "offset": 25},
    {"op": "sketch", "name": "B", "plane": "xy", "entities": [
        {"type": "circle", "center": [0, 0], "radius": 5}]},
]
#: A 10 mm circle: pi * 0.5^2 cm^2 of profile.
AREA = math.pi * 0.25


def recipe(*operations: dict) -> PartRecipe:
    return PartRecipe.model_validate(
        {"name": "T", "units": "mm", "operations": PLATE + list(operations)})


def feature(session, out: dict, name: str):
    return next(f for f in session.backend.list_features(out["document"])
                if f.name == name)


class TestTheExtentCarriesWhatItNeeds:
    def test_to_needs_a_target(self):
        with pytest.raises(pydantic.ValidationError, match="`to` is required"):
            ExtrudeOp(sketch="S", extent="to")

    def test_from_to_needs_both_ends(self):
        with pytest.raises(pydantic.ValidationError, match="both required"):
            ExtrudeOp(sketch="S", extent="from_to", to="Lid")

    def test_a_target_on_an_extent_that_ignores_it_is_refused(self):
        """A field that is silently ignored is how somebody learns the wrong
        lesson about what their recipe says."""
        with pytest.raises(pydantic.ValidationError, match="means nothing"):
            ExtrudeOp(sketch="S", extent="distance", distance=5, to="Lid")
        with pytest.raises(pydantic.ValidationError, match="means nothing"):
            ExtrudeOp.model_validate(
                {"sketch": "S", "extent": "to", "to": "Lid", "from": "xy"})

    def test_from_is_the_recipe_word_and_from_underscore_the_python_one(self):
        """`from` is a Python keyword, so the recipe's word is an alias. Both
        directions are pinned because a rename of either would make every
        shipped recipe using it fail validation."""
        op = ExtrudeOp.model_validate(
            {"sketch": "S", "extent": "from_to", "from": "xy", "to": "Lid"})
        assert op.from_ == "xy"
        assert "from" in ExtrudeOp.model_json_schema()["properties"]


class TestWhatTheSimulatorCanAnswer:
    def test_to_a_parallel_plane_is_exact(self, session):
        """25 mm above a sketch on XY, so 2.5 cm of sweep and no arithmetic
        anybody had to do by hand."""
        out = build_part(session, recipe(
            {"op": "extrude", "name": "Boss", "sketch": "B", "extent": "to",
             "to": "Lid"}))
        assert out["ok"]
        volume = session.backend.mass_properties(out["document"]).volume
        assert volume == pytest.approx(24.0 + AREA * 2.5)

    def test_from_to_spans_the_two_planes_and_not_the_sketch(self, session):
        """The sweep starts at `from`, so a profile drawn on XY between planes
        at 5 and 25 mm makes a 20 mm band and nothing below it."""
        out = build_part(session, recipe(
            {"op": "work_plane", "name": "Floor", "kind": "offset", "base": "xy",
             "offset": 5},
            {"op": "extrude", "name": "Band", "sketch": "B", "extent": "from_to",
             "from": "Floor", "to": "Lid"}))
        assert out["ok"]
        volume = session.backend.mass_properties(out["document"]).volume
        assert volume == pytest.approx(24.0 + AREA * 2.0)

    def test_the_extent_and_its_targets_are_on_the_feature(self, session):
        out = build_part(session, recipe(
            {"op": "extrude", "name": "Boss", "sketch": "B", "extent": "to",
             "to": "Lid"}))
        detail = feature(session, out, "Boss").detail
        assert detail["extent"] == "to"
        assert detail["to"] == "Lid"


class TestWhatItDeclines:
    def _declined(self, session, target: dict, *, name: str = "Deep"):
        out = build_part(session, recipe(target, {
            "op": "extrude", "name": name, "sketch": "B", "extent": "to",
            "to": target.get("name", "Side")}))
        assert out["ok"], out["errors"]
        return out, feature(session, out, name).detail["volume_from"]

    def test_a_perpendicular_plane(self, session):
        out, why = self._declined(session, {
            "op": "work_plane", "name": "Side", "kind": "offset", "base": "yz",
            "offset": 5})
        assert why.startswith("not predicted:")
        assert "perpendicular rather than parallel" in why
        assert session.backend.mass_properties(out["document"]).volume == \
            pytest.approx(24.0), "charging nothing is the point: it is not a guess"

    def test_a_tilted_plane(self, session):
        out, why = self._declined(session, {
            "op": "work_plane", "name": "Tilt", "kind": "angle", "base": "xy",
            "axis": "x", "angle": "30 deg"})
        assert why.startswith("not predicted:")
        assert "30 degrees about x" in why
        assert "varies across the profile" in why

    def test_a_face_handle(self, session):
        """A face in this ledger is a midpoint and an area. That says where a
        face is and not which way it faces, so the distance to it is not
        something the simulator can work out -- and a plausible-looking number
        would be the worst answer available."""
        out = build_part(session, recipe({
            "op": "extrude", "name": "Deep", "sketch": "B", "extent": "to",
            "to": "face:nonexistent"}))
        assert out["ok"], out["errors"]
        why = feature(session, out, "Deep").detail["volume_from"]
        assert why.startswith("not predicted:")
        assert "which way it faces" in why


class TestTheRehearsalSaysWhenItHasNoNumber:
    def test_an_unpredicted_step_is_warned_about(self):
        report = rehearse(recipe(
            {"op": "work_plane", "name": "Side", "kind": "offset", "base": "yz",
             "offset": 5},
            {"op": "extrude", "name": "Deep", "sketch": "B", "extent": "to",
             "to": "Side"}))
        assert report["ok"]
        assert any("volume is not predicted" in w["warning"]
                   for w in report["warnings"]), report["warnings"]

    def test_a_predictable_one_is_not(self):
        report = rehearse(recipe(
            {"op": "extrude", "name": "Boss", "sketch": "B", "extent": "to",
             "to": "Lid"}))
        assert [w for w in report["warnings"]
                if "not predicted" in w["warning"]] == []


class TestTheComCallsArePublished:
    def test_both_extents_reach_their_own_call(self):
        import inspect

        source = inspect.getsource(com.ComBackend.extrude)
        assert "definition.SetToExtent(" in source
        assert "definition.SetFromToExtent(" in source

    def test_neither_is_given_a_direction(self):
        """`SetToExtent` has no direction argument: where the target is decides
        which way the sweep runs, which is the point of naming one."""
        import inspect

        source = inspect.getsource(com.ComBackend.extrude)
        to_call = source[source.index("definition.SetToExtent("):]
        assert "direction" not in to_call.split(")")[0]

    def test_from_to_does_not_extend_either_face(self):
        """Both booleans are documented without brackets, so they are supplied
        rather than defaulted, and False is the conservative pair: a feature
        that silently grew to meet a face it did not reach is the kind of
        success this server exists not to report."""
        import inspect

        source = inspect.getsource(com.ComBackend.extrude)
        call = source[source.index("definition.SetFromToExtent("):]
        assert call.count("False") >= 2
