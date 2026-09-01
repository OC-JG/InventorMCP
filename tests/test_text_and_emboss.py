"""Text sketch entities and the emboss feature.

The numbers asserted for the mock's engraved volume are calibrated against what
Inventor itself removed -- see ``_INK_PER_EM`` -- so a change to the heuristic
that drifts away from the real thing shows up here.
"""

from __future__ import annotations

import pytest

from inventor_mcp.builder import build_part
from inventor_mcp.geometry import plan_sketch, profile_loops
from inventor_mcp.plan import PText
from inventor_mcp.resolve import Resolver
from inventor_mcp.schema import PartRecipe, SketchOp


def plan(entities):
    spec = SketchOp.model_validate({"op": "sketch", "plane": "xy", "entities": entities})
    return plan_sketch(spec, Resolver("mm", "deg"))


PLATE = [
    {"op": "sketch", "name": "Body", "plane": "xy",
     "entities": [{"type": "rectangle", "center": [0, 0], "width": 80, "height": 40}]},
    {"op": "extrude", "name": "Plate", "sketch": "Body", "distance": 6},
    {"op": "work_plane", "name": "Top", "kind": "offset", "base": "xy", "offset": 6},
]


def marked(session, entities, **emboss):
    recipe = PartRecipe.model_validate({
        "name": "Marked", "units": "mm",
        "operations": PLATE + [
            {"op": "sketch", "name": "Mark", "plane": "Top", "entities": entities},
            {"op": "emboss", "name": "Mark1", "sketch": "Mark", "depth": 0.5, **emboss},
        ],
    })
    return build_part(session, recipe)


class TestTextEntity:
    def test_it_plans_to_a_single_text_primitive(self):
        result = plan([{"type": "text", "text": "OnlyCat", "height": 10}])
        assert [type(p).__name__ for p in result.primitives] == ["PText"]

    def test_it_carries_no_constraints_or_dimensions(self):
        """Inventor owns the glyph outlines, so there is nothing here to drive."""
        result = plan([{"type": "text", "text": "OnlyCat", "height": 10}])
        assert result.constraints == []
        assert result.dimensions == []

    def test_height_reaches_the_plan_in_database_units(self):
        text: PText = plan([{"type": "text", "text": "A", "height": 10}]).primitives[0]
        assert text.height == pytest.approx(1.0)          # 10 mm -> 1 cm
        assert text.height_expression == "10 mm"

    def test_it_contributes_no_profile_loop(self):
        """A text box is not a closed loop, so a solid feature cannot use it."""
        assert profile_loops(plan([{"type": "text", "text": "A", "height": 10}])) == []

    def test_styling_survives_to_the_plan(self):
        text: PText = plan([{
            "type": "text", "text": "OnlyCat", "height": 8, "font": "Consolas",
            "bold": True, "italic": True, "align": "left", "rotation": 90,
        }]).primitives[0]
        assert (text.font, text.bold, text.italic, text.align) == ("Consolas", True, True, "left")
        assert text.rotation == pytest.approx(1.5707963, abs=1e-6)


class TestEmboss:
    def test_engraving_text_removes_material(self, session):
        out = marked(session, [{"type": "text", "text": "OnlyCat", "height": 8, "bold": True}],
                     style="engrave")
        step = out["operations"][-1]
        # Inventor removed 0.0742 cm^3 for exactly this text, size and depth.
        assert step["measured"]["volume_change_cm3"] == pytest.approx(-0.0742, abs=0.005)

    def test_raising_text_adds_material(self, session):
        out = marked(session, [{"type": "text", "text": "OnlyCat", "height": 8, "bold": True}],
                     style="raise")
        assert out["operations"][-1]["measured"]["volume_change_cm3"] > 0

    def test_bold_lays_down_more_ink_than_regular(self, session):
        bold = marked(session, [{"type": "text", "text": "OnlyCat", "height": 8, "bold": True}])
        plain = marked(session, [{"type": "text", "text": "OnlyCat", "height": 8}])
        assert (abs(bold["operations"][-1]["measured"]["volume_change_cm3"])
                > abs(plain["operations"][-1]["measured"]["volume_change_cm3"]))

    def test_a_closed_profile_embosses_by_its_real_area(self, session):
        out = marked(session, [{"type": "circle", "center": [0, 0], "diameter": 20}])
        # pi * 1 cm^2 * 0.05 cm
        assert out["operations"][-1]["measured"]["volume_change_cm3"] == pytest.approx(
            -3.14159 * 1.0 * 0.05, rel=1e-3)

    def test_an_empty_sketch_is_refused(self, session):
        """A lone line is neither text nor a closed profile, so there is nothing
        to mark the face with -- and the build says so rather than doing nothing."""
        out = marked(session, [{"type": "line", "start": [0, 0], "end": [10, 0]}])
        assert out["ok"] is False
        assert "nothing to emboss" in out["errors"][0]["error"]
