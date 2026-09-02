"""The coil operation: springs, threads and a drill's flutes.

The expected volumes are what Inventor actually produced for these recipes, so
a change that drifts away from the real thing fails here rather than in a part
someone has already sent to a factory.
"""

from __future__ import annotations

import math

import pytest

from inventor_mcp.builder import build_part
from inventor_mcp.schema import PartRecipe

#: A spring: O6 wire on a O50 mean coil, 10 mm pitch, 100 tall -- 10 turns.
SPRING = [
    {"op": "sketch", "name": "Wire", "plane": "xz", "entities": [
        {"type": "circle", "center": [25, 0], "diameter": 6}]},
    {"op": "coil", "name": "Spring", "sketch": "Wire", "axis": "z",
     "pitch": 10, "height": 100},
]


def build(session, ops, **kwargs):
    recipe = PartRecipe.model_validate({"name": "T", "units": "mm",
                                        "operations": ops})
    return build_part(session, recipe, **kwargs)


def volume(out):
    return out["operations"][-1]["measured"]["volume_cm3"]


class TestCoil:
    def test_a_spring_measures_its_helix_length(self, session):
        """Inventor built 44.4132 cm^3 from this recipe.

        One turn of a helix is sqrt((2 pi r)^2 + p^2) long, so the swept volume
        is the wire's area times that times the turns: 44.5031. The 0.2%
        remainder is the helix's section not being perpendicular to its path,
        which the ideal figure ignores.
        """
        out = build(session, SPRING)
        assert out["ok"] is True
        turns = 100.0 / 10.0
        per_turn = math.hypot(2 * math.pi * 25.0, 10.0)
        ideal = math.pi * 9.0 * per_turn * turns / 1000.0
        assert volume(out) == pytest.approx(ideal, rel=0.01)

    def test_it_reports_the_helix_it_swept(self, session):
        out = build(session, SPRING)
        detail = out["operations"][-1]["detail"]
        assert detail["turns"] == pytest.approx(10.0)
        assert detail["helix_radius"] == pytest.approx(2.5)      # cm
        assert detail["pitch"]["value"] == pytest.approx(1.0)    # cm

    def test_pitch_and_revolutions_is_the_same_coil(self, session):
        """The three extents are three ways of saying one thing."""
        by_height = build(session, SPRING)
        by_turns = build(session, [
            SPRING[0],
            {"op": "coil", "sketch": "Wire", "axis": "z", "pitch": 10,
             "revolutions": 10},
        ])
        assert volume(by_turns) == pytest.approx(volume(by_height), rel=1e-6)

    def test_revolutions_may_be_fractional(self, session):
        """A coil of 1.75 turns is ordinary; `count` would refuse the fraction."""
        out = build(session, [
            SPRING[0],
            {"op": "coil", "sketch": "Wire", "axis": "z", "pitch": 10,
             "revolutions": 1.75},
        ])
        assert out["ok"] is True
        assert volume(out) == pytest.approx(volume(build(session, SPRING)) * 0.175,
                                            rel=0.01)

    def test_a_cut_coil_removes_material(self, session):
        """A drill flute and a thread are both coil cuts."""
        out = build(session, [
            {"op": "sketch", "name": "Rod", "plane": "xy", "entities": [
                {"type": "polyline", "closed": True, "locate": "none",
                 "points": [[0, 0], [60, 0], [60, 10], [0, 10]]}]},
            {"op": "revolve", "name": "Body", "sketch": "Rod", "axis": "x"},
            {"op": "sketch", "name": "Groove", "plane": "xy", "entities": [
                {"type": "polyline", "closed": True, "locate": "none",
                 "points": [[9, 10.2], [11, 10.2], [10, 8.9]]}]},
            {"op": "coil", "name": "Thread", "sketch": "Groove", "axis": "x",
             "pitch": 2, "height": 40, "operation": "cut"},
        ])
        assert out["ok"] is True
        rod = math.pi * 100.0 * 60.0 / 1000.0
        assert volume(out) < rod

    def test_two_of_the_three_extents_are_required(self):
        for bad in ({"pitch": 10}, {"pitch": 10, "height": 100,
                                    "revolutions": 10}, {}):
            with pytest.raises(Exception):
                PartRecipe.model_validate({
                    "name": "T", "units": "mm",
                    "operations": [SPRING[0],
                                   dict({"op": "coil", "sketch": "Wire",
                                         "axis": "z"}, **bad)]})

    def test_a_spiral_needs_pitch_and_revolutions(self):
        with pytest.raises(Exception):
            PartRecipe.model_validate({
                "name": "T", "units": "mm",
                "operations": [SPRING[0],
                               {"op": "coil", "sketch": "Wire", "axis": "z",
                                "spiral": True, "pitch": 4}]})
        ok = PartRecipe.model_validate({
            "name": "T", "units": "mm",
            "operations": [SPRING[0],
                           {"op": "coil", "sketch": "Wire", "axis": "z",
                            "spiral": True, "pitch": 4, "revolutions": 5}]})
        assert ok.operations[-1].spiral is True
