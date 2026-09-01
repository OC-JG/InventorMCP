"""Draft, combine, split and boss.

The expected volumes are what Inventor actually produced for the same recipes,
so a change that drifts away from the real thing fails here rather than in a
part someone has already sent to a factory.
"""

from __future__ import annotations

import math

import pytest

from inventor_mcp.builder import build_part
from inventor_mcp.schema import PartRecipe

PLATE = [
    {"op": "sketch", "name": "Body", "plane": "xy", "entities": [
        {"type": "rectangle", "center": [0, 0], "width": 80, "height": 40}]},
    {"op": "extrude", "name": "Plate", "sketch": "Body", "distance": 6},
]


def build(session, ops, **kwargs):
    recipe = PartRecipe.model_validate({"name": "T", "units": "mm", "operations": PLATE + ops})
    return build_part(session, recipe, **kwargs)


def volume(out):
    return out["operations"][-1]["measured"]["volume_cm3"]


class TestDraft:
    def test_it_removes_a_wedge_off_each_wall(self, session):
        """Inventor removed 0.1505 cm^3 drafting this plate's four walls by 2
        degrees about XY. The estimate is A*h*tan(theta)/2 over the four walls."""
        out = build(session, [{"op": "draft", "name": "Walls",
                               "faces": {"kind": "face", "filter": "vertical"},
                               "plane": "xy", "angle": "2 deg"}])
        expected = 19.2 - 0.5 * 14.4 * 0.6 * math.tan(math.radians(2))
        assert volume(out) == pytest.approx(expected, abs=0.005)

    def test_a_selector_matching_nothing_is_refused(self, session):
        out = build(session, [{"op": "draft", "faces": {"kind": "face", "feature": "Nope"},
                               "plane": "xy", "angle": "2 deg"}], stop_on_error=True)
        assert out["ok"] is False
        assert "nothing to draft" in out["errors"][0]["error"]


class TestCombine:
    PEG = [
        {"op": "work_plane", "name": "Top", "kind": "offset", "base": "xy", "offset": 6},
        {"op": "sketch", "name": "Peg", "plane": "Top", "entities": [
            {"type": "circle", "center": [0, 0], "diameter": 20}]},
        {"op": "extrude", "name": "PegBody", "sketch": "Peg", "distance": 20,
         "operation": "new_body"},
    ]

    def test_joining_a_second_body_adds_its_volume(self, session):
        """Inventor measured 25.48318 cm^3 for exactly this."""
        out = build(session, self.PEG + [
            {"op": "combine", "name": "Merge", "base": 1, "tools": [2], "operation": "join"}])
        assert volume(out) == pytest.approx(19.2 + math.pi * 1.0 * 2.0, rel=1e-4)

    def test_cutting_with_a_second_body_removes_it(self, session):
        out = build(session, self.PEG + [
            {"op": "combine", "base": 1, "tools": [2], "operation": "cut"}])
        assert volume(out) < 19.2

    def test_a_body_that_is_not_there_is_refused(self, session):
        out = build(session, self.PEG + [
            {"op": "combine", "base": 1, "tools": [7], "operation": "join"}])
        assert out["ok"] is False
        assert "no body 7" in out["errors"][0]["error"]


class TestSplit:
    def test_trimming_at_the_midplane_halves_the_part(self, session):
        out = build(session, [
            {"op": "work_plane", "name": "Mid", "kind": "offset", "base": "xy", "offset": 3},
            {"op": "split", "name": "Half", "tool": "Mid", "style": "trim",
             "remove_positive": True}])
        assert volume(out) == pytest.approx(9.6, rel=1e-3)

    def test_splitting_leaves_two_bodies(self, session):
        out = build(session, [
            {"op": "work_plane", "name": "Mid", "kind": "offset", "base": "xy", "offset": 3},
            {"op": "split", "tool": "Mid", "style": "split"}])
        assert out["operations"][-1]["detail"]["style"] == "split"


class TestBoss:
    """A boss is not Inventor's Boss feature -- that collection is read-only --
    so it expands into the features that build the same geometry."""

    def _expanded(self, **boss):
        recipe = PartRecipe.model_validate({
            "name": "T", "units": "mm",
            "operations": PLATE + [{"op": "boss", "name": "Post", **boss}],
        })
        return [(op.op, getattr(op, "name", None)) for op in recipe.operations]

    def test_it_expands_into_real_features(self):
        assert self._expanded(positions=[[0, 0]], diameter=6, height=10,
                              hole_diameter=2.5)[2:] == [
            ("sketch", "PostProfiles"), ("extrude", "Post"), ("work_plane", "PostTop"),
            ("sketch", "PostPilots"), ("hole", "PostHoles"),
        ]

    def test_a_solid_post_needs_no_hole_features(self):
        assert self._expanded(positions=[[0, 0]], diameter=6, height=10)[2:] == [
            ("sketch", "PostProfiles"), ("extrude", "Post"),
        ]

    def test_the_pilot_defaults_to_four_fifths_of_the_post(self):
        recipe = PartRecipe.model_validate({
            "name": "T", "units": "mm",
            "operations": PLATE + [{"op": "boss", "name": "P", "height": 10,
                                    "hole_diameter": 2.5}],
        })
        hole = [op for op in recipe.operations if op.op == "hole"][0]
        assert hole.depth == pytest.approx(8.0)

    def test_an_expression_height_keeps_the_pilot_parametric(self):
        recipe = PartRecipe.model_validate({
            "name": "T", "units": "mm",
            "parameters": [{"name": "post_h", "value": 10}],
            "operations": PLATE + [{"op": "boss", "name": "P", "height": "post_h",
                                    "hole_diameter": 2.5}],
        })
        hole = [op for op in recipe.operations if op.op == "hole"][0]
        assert hole.depth == "(post_h) * 0.8"

    def test_two_bosses_build_two_posts(self, session):
        """Inventor measured 19.68950 cm^3 for two d6 x 10 posts, tapped M3 8 deep."""
        out = build(session, [
            {"op": "work_plane", "name": "Top", "kind": "offset", "base": "xy", "offset": 6},
            {"op": "boss", "name": "Post", "positions": [[-20, 0], [20, 0]], "plane": "Top",
             "diameter": 6, "height": 10, "hole_diameter": 2.5, "tap": "M3x0.5"}])
        assert volume(out) == pytest.approx(19.6895, abs=0.02)


class TestRib:
    """A rib is not Inventor's Rib feature either -- `RibFeatures.Add` refuses
    every definition the API can build -- so it is a silhouette and a symmetric
    extrude. The volumes are what Inventor produced for these exact recipes."""

    def _ops(self, **rib):
        return [{"op": "rib", "name": "Web", "plane": "xz", "root": 6,
                 "thickness": 2, **rib}]

    def test_it_expands_into_a_silhouette_and_an_extrude(self):
        recipe = PartRecipe.model_validate({
            "name": "T", "units": "mm",
            "operations": PLATE + self._ops(start=[-30, 20], end=[30, 20]),
        })
        assert [(op.op, getattr(op, "name", None)) for op in recipe.operations][2:] == [
            ("sketch", "WebProfile"), ("extrude", "Web"),
        ]

    def test_the_extrude_is_symmetric_about_the_plane(self):
        recipe = PartRecipe.model_validate({
            "name": "T", "units": "mm",
            "operations": PLATE + self._ops(start=[-30, 20], end=[30, 20]),
        })
        assert recipe.operations[-1].direction == "symmetric"

    def test_a_flat_topped_rib_is_its_silhouette_times_its_thickness(self, session):
        """Inventor measured 20.88000 cm^3: 60 x 14 mm silhouette, 2 mm thick."""
        out = build(session, self._ops(start=[-30, 20], end=[30, 20]))
        assert volume(out) == pytest.approx(19.2 + 1.68, rel=1e-4)

    def test_a_sloped_top_gives_a_trapezoid(self, session):
        """Inventor measured 20.40000 cm^3 for a top falling from 20 to 12."""
        out = build(session, self._ops(start=[-30, 20], end=[30, 12]))
        expected = 19.2 + (60 * (14 + 6) / 2) * 2 / 1000
        assert volume(out) == pytest.approx(expected, rel=1e-4)

    def test_there_is_no_draft_knob(self):
        """An extrude's taper drafts across the rib's thickness, which measurably
        adds material instead of releasing the rib, so it is deliberately absent."""
        from inventor_mcp.schema import RibOp
        assert "taper" not in RibOp.model_fields
